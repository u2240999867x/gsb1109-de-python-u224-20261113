"""End-of-day reconciliation between incoming payments and open orders.

The single-row contract validation (bad-field filtering) is handled with the
:mod:`schema` library.  Rows that survive validation are then matched so that
every payment is claimed by at most one order and every order is claimed by at
most one payment.

Matching is treated as a bipartite matching problem:

* an edge exists between a payment and an order when they share a currency,
  their dates fall inside ``date_window_days`` and their amounts differ by no
  more than the configured tolerance;
* connected components of that graph are independent and solved separately;
* each component is classified as **certain** (exactly one legal perfect
  matching), **ambiguous** (several legal perfect matchings) or
  **unmatchable** (no perfect matching, or unequal numbers on each side).

A payment that locally has several candidates can therefore be forced to a
single order once the constraints imposed by the other payments and orders are
taken into account, because the decision is made on full perfect matchings
rather than on per-row candidate counts.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from schema import And, Schema, SchemaError, Use

__all__ = [
    "Order",
    "Payment",
    "MatchConfig",
    "Candidate",
    "Pair",
    "Allocation",
    "AmbiguousGroup",
    "UnmatchableGroup",
    "ReconcileResult",
    "BatchReconcileResult",
    "ORDER_RECORD_SCHEMA",
    "PAYMENT_RECORD_SCHEMA",
    "InvalidRecord",
    "load_orders",
    "load_payments",
    "filter_orders",
    "filter_payments",
    "reconcile",
    "reconcile_records",
    "reconcile_valid_records",
    "REASON_UNEQUAL_PARTIES",
    "REASON_NO_PERFECT_MATCHING",
    "REASON_NO_CANDIDATE",
]

REASON_UNEQUAL_PARTIES = "unequal_parties"
REASON_NO_PERFECT_MATCHING = "no_perfect_matching"
REASON_NO_CANDIDATE = "no_candidate"


def _to_decimal(value: Any) -> Decimal:
    """Normalize int/float/string money input to an exact ``Decimal``.

    Going through ``str`` avoids the binary expansion produced by
    ``Decimal(float)``.
    """
    return Decimal(str(value))


ORDER_RECORD_SCHEMA = Schema(
    {
        "id": str,
        "currency": And(str, Use(str.strip), Use(str.upper)),
        "amount": Use(_to_decimal),
        "order_date": Use(date.fromisoformat),
    },
    ignore_extra_keys=True,
)

PAYMENT_RECORD_SCHEMA = Schema(
    {
        "id": str,
        "currency": And(str, Use(str.strip), Use(str.upper)),
        "amount": Use(_to_decimal),
        "payment_date": Use(date.fromisoformat),
    },
    ignore_extra_keys=True,
)


@dataclass(frozen=True)
class Order:
    """An open order expecting one payment."""

    id: str
    currency: str
    amount: Decimal
    order_date: date


@dataclass(frozen=True)
class Payment:
    """An incoming payment that must be linked to one order."""

    id: str
    currency: str
    amount: Decimal
    payment_date: date


@dataclass(frozen=True)
class MatchConfig:
    """Tuning knobs for candidate generation.

    :param date_window_days: maximum inclusive gap between payment and order
        dates, expressed in days.
    :param amount_tolerance: maximum absolute amount difference accepted.
    :param amount_tolerance_rate: maximum difference as a fraction of the
        order amount; the effective tolerance is the maximum of the absolute
        and relative allowances.
    :param max_solutions: cap on the number of legal allocations retained per
        ambiguous component.  Two are always enough to demonstrate that the
        component is genuinely ambiguous.
    """

    date_window_days: int = 0
    amount_tolerance: Decimal = Decimal("0")
    amount_tolerance_rate: Decimal = Decimal("0")
    max_solutions: int = 2


@dataclass(frozen=True)
class InvalidRecord:
    """A raw row rejected by single-row validation, kept for quarantine."""

    record: Mapping[str, Any]
    error: str
    index: int


@dataclass(frozen=True)
class Candidate:
    """An order that passes the candidate filters for one payment."""

    order_id: str
    amount_diff: Decimal
    date_diff_days: int


@dataclass(frozen=True)
class Pair:
    """A matched payment/order pair."""

    payment_id: str
    order_id: str


@dataclass(frozen=True)
class Allocation:
    """A complete legal assignment for one component.

    Every payment and every order of the component appears exactly once.
    """

    pairs: Tuple[Pair, ...]


@dataclass(frozen=True)
class AmbiguousGroup:
    """A component admitting at least two distinct legal allocations."""

    payments: Tuple[Payment, ...]
    orders: Tuple[Order, ...]
    candidates: Mapping[str, Tuple[Candidate, ...]]
    allocations: Tuple[Allocation, ...]
    truncated: bool

    @property
    def currency(self) -> str:
        return self.payments[0].currency


@dataclass(frozen=True)
class UnmatchableGroup:
    """A connected component that cannot be fully paired."""

    payments: Tuple[Payment, ...]
    orders: Tuple[Order, ...]
    candidates: Mapping[str, Tuple[Candidate, ...]]
    reason: str

    @property
    def currency(self) -> str:
        return self.payments[0].currency if self.payments else self.orders[0].currency


@dataclass(frozen=True)
class ReconcileResult:
    """Full three-way classification of a reconciliation run."""

    certain_pairs: Tuple[Pair, ...]
    ambiguous_groups: Tuple[AmbiguousGroup, ...]
    unmatchable_groups: Tuple[UnmatchableGroup, ...]
    unmatched_payments: Tuple[Payment, ...]
    unmatched_orders: Tuple[Order, ...]
    config: MatchConfig

    def full_allocations(self, limit: int = 2) -> Tuple[Tuple[Pair, ...], ...]:
        """Reconstruct complete, legal allocations for the whole batch.

        The certain pairs are present in every allocation; each ambiguous
        group contributes one of its legal assignments.  Two returned
        allocations differ on at least one ambiguous group and are both fully
        valid (bijective) overall.
        """
        base = tuple(sorted(self.certain_pairs, key=lambda pair: pair.payment_id))
        combinations: List[Tuple[Pair, ...]] = [()]
        for group in self.ambiguous_groups:
            options = group.allocations[:limit]
            combinations = [
                existing + allocation.pairs
                for existing in combinations
                for allocation in options
            ][:limit]
        result = []
        for extra in combinations:
            result.append(tuple(sorted(base + extra, key=lambda pair: pair.payment_id)))
        return tuple(result)


@dataclass(frozen=True)
class BatchReconcileResult:
    """A reconciliation run together with the rows rejected up front."""

    result: ReconcileResult
    invalid_orders: Tuple[InvalidRecord, ...]
    invalid_payments: Tuple[InvalidRecord, ...]

    @property
    def has_invalid_records(self) -> bool:
        return bool(self.invalid_orders or self.invalid_payments)


def _ensure_unique(ids: Sequence[str], kind: str) -> None:
    seen = set()
    for identifier in ids:
        if identifier in seen:
            raise ValueError(f"duplicate {kind} id: {identifier!r}")
        seen.add(identifier)


def load_orders(records: Sequence[Mapping[str, Any]]) -> List[Order]:
    """Validate raw order mappings and return ``Order`` objects."""
    orders = []
    for record in records:
        data = ORDER_RECORD_SCHEMA.validate(record)
        orders.append(
            Order(
                id=data["id"],
                currency=data["currency"],
                amount=data["amount"],
                order_date=data["order_date"],
            )
        )
    _ensure_unique([order.id for order in orders], "order")
    return orders


def load_payments(records: Sequence[Mapping[str, Any]]) -> List[Payment]:
    """Validate raw payment mappings and return ``Payment`` objects."""
    payments = []
    for record in records:
        data = PAYMENT_RECORD_SCHEMA.validate(record)
        payments.append(
            Payment(
                id=data["id"],
                currency=data["currency"],
                amount=data["amount"],
                payment_date=data["payment_date"],
            )
        )
    _ensure_unique([payment.id for payment in payments], "payment")
    return payments


def filter_orders(
    records: Sequence[Mapping[str, Any]],
) -> Tuple[List[Order], List[InvalidRecord]]:
    """Split raw order rows into valid orders and quarantined bad rows."""
    orders: List[Order] = []
    invalid: List[InvalidRecord] = []
    for index, record in enumerate(records):
        try:
            data = ORDER_RECORD_SCHEMA.validate(record)
        except SchemaError as exc:
            invalid.append(InvalidRecord(record=record, error=exc.code, index=index))
        else:
            orders.append(
                Order(
                    id=data["id"],
                    currency=data["currency"],
                    amount=data["amount"],
                    order_date=data["order_date"],
                )
            )
    _ensure_unique([order.id for order in orders], "order")
    return orders, invalid


def filter_payments(
    records: Sequence[Mapping[str, Any]],
) -> Tuple[List[Payment], List[InvalidRecord]]:
    """Split raw payment rows into valid payments and quarantined bad rows."""
    payments: List[Payment] = []
    invalid: List[InvalidRecord] = []
    for index, record in enumerate(records):
        try:
            data = PAYMENT_RECORD_SCHEMA.validate(record)
        except SchemaError as exc:
            invalid.append(InvalidRecord(record=record, error=exc.code, index=index))
        else:
            payments.append(
                Payment(
                    id=data["id"],
                    currency=data["currency"],
                    amount=data["amount"],
                    payment_date=data["payment_date"],
                )
            )
    _ensure_unique([payment.id for payment in payments], "payment")
    return payments, invalid


def _effective_tolerance(order: Order, config: MatchConfig) -> Decimal:
    relative = config.amount_tolerance_rate * abs(order.amount)
    return max(config.amount_tolerance, relative)


def _build_edges(
    orders: Sequence[Order], payments: Sequence[Payment], config: MatchConfig
) -> Tuple[Dict[str, List[str]], Dict[str, Tuple[Candidate, ...]]]:
    """Return adjacency (payment -> order ids) and descriptive candidates."""
    sorted_orders = sorted(orders, key=lambda order: order.id)
    edges: Dict[str, List[str]] = {}
    candidates: Dict[str, Tuple[Candidate, ...]] = {}
    for payment in sorted(payments, key=lambda item: item.id):
        local_candidates: List[Candidate] = []
        for order in sorted_orders:
            if payment.currency != order.currency:
                continue
            date_diff = abs((payment.payment_date - order.order_date).days)
            if date_diff > config.date_window_days:
                continue
            amount_diff = abs(payment.amount - order.amount)
            if amount_diff <= _effective_tolerance(order, config):
                local_candidates.append(
                    Candidate(
                        order_id=order.id,
                        amount_diff=amount_diff,
                        date_diff_days=date_diff,
                    )
                )
        ordered_candidates = tuple(
            sorted(local_candidates, key=lambda candidate: candidate.order_id)
        )
        candidates[payment.id] = ordered_candidates
        edges[payment.id] = [candidate.order_id for candidate in ordered_candidates]
    return edges, candidates


def _connected_components(
    edges: Mapping[str, Sequence[str]],
) -> List[Tuple[List[str], List[str]]]:
    """Split the bipartite graph into connected components.

    Returns one ``(payment_ids, order_ids)`` tuple per component that has at
    least one edge.
    """
    adjacency: Dict[Tuple[str, str], set] = defaultdict(set)
    for payment_id, neighbor_order_ids in edges.items():
        payment_node = ("p", payment_id)
        for order_id in neighbor_order_ids:
            order_node = ("o", order_id)
            adjacency[payment_node].add(order_node)
            adjacency[order_node].add(payment_node)

    seen: Set[Tuple[str, str]] = set()
    components: List[Tuple[List[str], List[str]]] = []
    for start in adjacency:
        if start in seen:
            continue
        stack = [start]
        payment_ids: List[str] = []
        component_order_ids: List[str] = []
        while stack:
            kind, identifier = stack.pop()
            if (kind, identifier) in seen:
                continue
            seen.add((kind, identifier))
            if kind == "p":
                payment_ids.append(identifier)
            else:
                component_order_ids.append(identifier)
            stack.extend(adjacency[(kind, identifier)] - seen)
        components.append((sorted(payment_ids), sorted(component_order_ids)))
    return components


def _perfect_matchings(
    edges: Mapping[str, Sequence[str]],
    payment_ids: Sequence[str],
    order_ids: Sequence[str],
    limit: int,
) -> Tuple[Tuple[Tuple[Pair, ...], ...], bool]:
    """Enumerate perfect matchings of one equal-sized component.

    Uses minimum-remaining-values branching with forward checking.  Enumeration
    stops once ``limit`` matchings are found; ``truncated`` reports whether
    additional legal matchings may exist.
    """
    solutions: List[Tuple[Pair, ...]] = []
    target = set(order_ids)

    def backtrack(remaining: set, used_orders: set, assignment: List[Pair]) -> None:
        if len(solutions) >= limit:
            return
        if not remaining:
            if used_orders == target:
                solutions.append(tuple(assignment))
            return

        available = {
            payment_id: [
                order_id
                for order_id in edges[payment_id]
                if order_id not in used_orders
            ]
            for payment_id in remaining
        }
        if any(not options for options in available.values()):
            return

        payment_id = min(remaining, key=lambda item: (len(available[item]), item))
        for order_id in available[payment_id]:
            backtrack(
                remaining - {payment_id},
                used_orders | {order_id},
                assignment + [Pair(payment_id, order_id)],
            )

    backtrack(set(payment_ids), set(), [])
    truncated = len(solutions) >= limit and _has_more_matchings(
        edges, payment_ids, order_ids, known=solutions
    )
    return tuple(solutions), truncated


def _has_more_matchings(
    edges: Mapping[str, Sequence[str]],
    payment_ids: Sequence[str],
    order_ids: Sequence[str],
    known: Sequence[Tuple[Pair, ...]],
) -> bool:
    """Check whether more than ``len(known)`` perfect matchings exist.

    Continues the search while forbidding the already known solutions so the
    truncation flag is exact rather than pessimistic.
    """
    known_set = set(known)
    target = set(order_ids)

    def backtrack(remaining: set, used_orders: set, assignment: List[Pair]) -> bool:
        if not remaining:
            return used_orders == target and tuple(assignment) not in known_set
        available = {
            payment_id: [
                order_id
                for order_id in edges[payment_id]
                if order_id not in used_orders
            ]
            for payment_id in remaining
        }
        if any(not options for options in available.values()):
            return False
        payment_id = min(remaining, key=lambda item: (len(available[item]), item))
        for order_id in available[payment_id]:
            if backtrack(
                remaining - {payment_id},
                used_orders | {order_id},
                assignment + [Pair(payment_id, order_id)],
            ):
                return True
        return False

    return backtrack(set(payment_ids), set(), [])


def reconcile(
    orders: Sequence[Order],
    payments: Sequence[Payment],
    config: MatchConfig = MatchConfig(),
) -> ReconcileResult:
    """Match payments against orders and classify every record."""
    _ensure_unique([order.id for order in orders], "order")
    _ensure_unique([payment.id for payment in payments], "payment")
    orders_by_id = {order.id: order for order in orders}
    payments_by_id = {payment.id: payment for payment in payments}
    edges, candidates = _build_edges(orders, payments, config)

    certain_pairs: List[Pair] = []
    ambiguous_groups: List[AmbiguousGroup] = []
    unmatchable_groups: List[UnmatchableGroup] = []

    matched_payment_ids = set()
    matched_order_ids = set()

    for payment_ids, order_ids in _connected_components(edges):
        matched_payment_ids.update(payment_ids)
        matched_order_ids.update(order_ids)
        component_candidates = MappingProxyType(
            {payment_id: candidates[payment_id] for payment_id in payment_ids}
        )
        component_payments = tuple(payments_by_id[pid] for pid in payment_ids)
        component_orders = tuple(orders_by_id[oid] for oid in order_ids)

        if len(payment_ids) != len(order_ids):
            unmatchable_groups.append(
                UnmatchableGroup(
                    payments=component_payments,
                    orders=component_orders,
                    candidates=component_candidates,
                    reason=REASON_UNEQUAL_PARTIES,
                )
            )
            continue

        matchings, truncated = _perfect_matchings(
            edges, payment_ids, order_ids, config.max_solutions
        )
        if not matchings:
            unmatchable_groups.append(
                UnmatchableGroup(
                    payments=component_payments,
                    orders=component_orders,
                    candidates=component_candidates,
                    reason=REASON_NO_PERFECT_MATCHING,
                )
            )
        elif len(matchings) == 1 and not truncated:
            certain_pairs.extend(matchings[0])
        else:
            ambiguous_groups.append(
                AmbiguousGroup(
                    payments=component_payments,
                    orders=component_orders,
                    candidates=component_candidates,
                    allocations=tuple(Allocation(pairs=pairs) for pairs in matchings),
                    truncated=truncated,
                )
            )

    # Records without any edge form isolated components; they cannot be paired
    # and therefore belong to the unmatchable bucket as well, keeping the three
    # buckets a complete partition of the input records.
    isolated_payments = [
        payment for payment in payments if payment.id not in matched_payment_ids
    ]
    isolated_orders = [order for order in orders if order.id not in matched_order_ids]
    for payment in sorted(isolated_payments, key=lambda item: item.id):
        unmatchable_groups.append(
            UnmatchableGroup(
                payments=(payment,),
                orders=(),
                candidates=MappingProxyType({payment.id: ()}),
                reason=REASON_NO_CANDIDATE,
            )
        )
    for order in sorted(isolated_orders, key=lambda item: item.id):
        unmatchable_groups.append(
            UnmatchableGroup(
                payments=(),
                orders=(order,),
                candidates=MappingProxyType({}),
                reason=REASON_NO_CANDIDATE,
            )
        )

    unmatched_payments = tuple(
        sorted(
            (payment for group in unmatchable_groups for payment in group.payments),
            key=lambda payment: payment.id,
        )
    )
    unmatched_orders = tuple(
        sorted(
            (order for group in unmatchable_groups for order in group.orders),
            key=lambda order: order.id,
        )
    )

    def _group_sort_key(group: UnmatchableGroup) -> str:
        if group.payments:
            return group.payments[0].id
        return group.orders[0].id

    return ReconcileResult(
        certain_pairs=tuple(sorted(certain_pairs, key=lambda pair: pair.payment_id)),
        ambiguous_groups=tuple(
            sorted(ambiguous_groups, key=lambda group: group.payments[0].id)
        ),
        unmatchable_groups=tuple(sorted(unmatchable_groups, key=_group_sort_key)),
        unmatched_payments=unmatched_payments,
        unmatched_orders=unmatched_orders,
        config=config,
    )


def reconcile_records(
    order_records: Sequence[Mapping[str, Any]],
    payment_records: Sequence[Mapping[str, Any]],
    config: MatchConfig = MatchConfig(),
) -> ReconcileResult:
    """Validate raw mappings and reconcile them in a single call."""
    orders = load_orders(order_records)
    payments = load_payments(payment_records)
    return reconcile(orders, payments, config)


def reconcile_valid_records(
    order_records: Sequence[Mapping[str, Any]],
    payment_records: Sequence[Mapping[str, Any]],
    config: MatchConfig = MatchConfig(),
) -> BatchReconcileResult:
    """Filter out bad rows, then reconcile the valid ones.

    Rows rejected by single-row validation are returned alongside the
    reconciliation result instead of aborting the whole day-end batch.
    """
    orders, invalid_orders = filter_orders(order_records)
    payments, invalid_payments = filter_payments(payment_records)
    result = reconcile(orders, payments, config)
    return BatchReconcileResult(
        result=result,
        invalid_orders=tuple(invalid_orders),
        invalid_payments=tuple(invalid_payments),
    )
