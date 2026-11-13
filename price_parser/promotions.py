"""Stacking-aware promotion settlement for a shopping basket.

This module computes the lowest payable amount for a basket when several
promotions (满额券 threshold coupons, 会员折扣 member discounts and
限品类券 category-limited coupons) hang off the same original price.

Semantics implemented here:

* Stacking is never assumed. Two promotions are only ever combined when a
  :class:`StackingGroup` explicitly declares them together; the group also
  fixes the trigger order in which the promotions are applied.
* Any subsequence of a declared group is a candidate settlement plan (a
  shopper may skip a coupon), as is every promotion on its own. Plans that
  were never declared are never tried.
* A threshold may look at the original price (:attr:`ThresholdBasis.ORIGINAL`)
  or at the amount left by the promotions triggered before it
  (:attr:`ThresholdBasis.AFTER_PREVIOUS`).
* Every fixed-amount discount is allocated back to the item lines
  proportionally to their current amounts, so each line keeps a trace of
  the discounts it absorbed.
* The result does not depend on the display order of the input: the same
  promotions and groups listed in any order produce the identical optimal
  settlement.
"""

from __future__ import annotations

import enum
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from itertools import combinations
from typing import TYPE_CHECKING

import attr

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _as_str_tuple(value: Iterable[str]) -> tuple[str, ...]:
    return tuple(value)


class PromotionKind(enum.Enum):
    """The kind of a promotion."""

    THRESHOLD_COUPON = "threshold_coupon"  # 满额券: spend X, get Y off
    MEMBER_DISCOUNT = "member_discount"  # 会员折扣: pay a fraction of the price
    CATEGORY_COUPON = "category_coupon"  # 限品类券: discount limited to categories


class ThresholdBasis(enum.Enum):
    """Which amount a promotion threshold is checked against."""

    ORIGINAL = "original"  # the original price, before any discount
    AFTER_PREVIOUS = "after_previous"  # the amount left by earlier promotions


def _money(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _cent_money(value: object) -> Decimal:
    return _money(value).quantize(CENT)


def _optional_money(value: object) -> Decimal | None:
    return None if value is None else _cent_money(value)


def _optional_frozenset(value: Iterable[str] | None) -> frozenset[str] | None:
    return None if value is None else frozenset(value)


@attr.s(frozen=True, auto_attribs=True)
class Item:
    """One basket line: ``quantity`` units of a product."""

    item_id: str
    category: str
    unit_price: Decimal = attr.ib(converter=_money)
    quantity: int = 1

    def __attrs_post_init__(self) -> None:
        if self.quantity < 1:
            msg = f"quantity must be positive, got {self.quantity!r}"
            raise ValueError(msg)

    @property
    def line_total(self) -> Decimal:
        """Original line amount (unit price times quantity)."""
        return (self.unit_price * self.quantity).quantize(CENT)


@attr.s(frozen=True, auto_attribs=True)
class Promotion:
    """A single promotion hanging off the original price.

    Exactly one of ``rate`` (pay a fraction of the amount) or ``amount_off``
    (subtract a fixed amount) must be given. ``threshold`` is the minimum
    amount required to trigger the promotion; ``threshold_basis`` decides
    whether it is checked against the original price or against the amount
    left by previously triggered promotions. ``categories`` restricts the
    promotion to item lines of the given categories.
    """

    promotion_id: str
    kind: PromotionKind = attr.ib(converter=PromotionKind)
    rate: Decimal | None = attr.ib(default=None, converter=_optional_money)
    amount_off: Decimal | None = attr.ib(default=None, converter=_optional_money)
    threshold: Decimal | None = attr.ib(default=None, converter=_optional_money)
    threshold_basis: ThresholdBasis = attr.ib(
        default=ThresholdBasis.AFTER_PREVIOUS, converter=ThresholdBasis
    )
    categories: frozenset[str] | None = attr.ib(
        default=None, converter=_optional_frozenset
    )

    def __attrs_post_init__(self) -> None:
        if (self.rate is None) == (self.amount_off is None):
            msg = "exactly one of rate or amount_off must be set"
            raise ValueError(msg)
        if self.rate is not None and not ZERO < self.rate <= Decimal(1):
            msg = f"rate must be in (0, 1], got {self.rate!r}"
            raise ValueError(msg)
        if self.amount_off is not None and self.amount_off < ZERO:
            msg = f"amount_off must not be negative, got {self.amount_off!r}"
            raise ValueError(msg)
        if self.threshold is not None and self.threshold < ZERO:
            msg = f"threshold must not be negative, got {self.threshold!r}"
            raise ValueError(msg)
        if self.kind is PromotionKind.MEMBER_DISCOUNT:
            if self.rate is None:
                msg = "a member discount must define a rate"
                raise ValueError(msg)
            if self.categories is not None:
                msg = "a member discount applies to the whole basket"
                raise ValueError(msg)
        if self.kind is PromotionKind.THRESHOLD_COUPON and (
            self.amount_off is None or self.threshold is None
        ):
            msg = "a threshold coupon must define amount_off and threshold"
            raise ValueError(msg)
        if self.kind is PromotionKind.CATEGORY_COUPON and not self.categories:
            msg = "a category coupon must list at least one category"
            raise ValueError(msg)

    @classmethod
    def threshold_coupon(
        cls,
        promotion_id: str,
        threshold: object,
        amount_off: object,
        *,
        basis: ThresholdBasis = ThresholdBasis.AFTER_PREVIOUS,
    ) -> Promotion:
        """满额券: subtract ``amount_off`` once the spend reaches ``threshold``."""
        return cls(
            promotion_id=promotion_id,
            kind=PromotionKind.THRESHOLD_COUPON,
            amount_off=amount_off,
            threshold=threshold,
            threshold_basis=basis,
        )

    @classmethod
    def member_discount(cls, promotion_id: str, rate: object) -> Promotion:
        """会员折扣: pay ``rate`` (e.g. 0.9) of every line in the basket."""
        return cls(
            promotion_id=promotion_id,
            kind=PromotionKind.MEMBER_DISCOUNT,
            rate=rate,
        )

    @classmethod
    def category_coupon(
        cls,
        promotion_id: str,
        categories: Iterable[str],
        *,
        rate: object = None,
        amount_off: object = None,
        threshold: object = None,
        basis: ThresholdBasis = ThresholdBasis.AFTER_PREVIOUS,
    ) -> Promotion:
        """限品类券: a rate or fixed-amount discount limited to ``categories``."""
        return cls(
            promotion_id=promotion_id,
            kind=PromotionKind.CATEGORY_COUPON,
            rate=rate,
            amount_off=amount_off,
            threshold=threshold,
            threshold_basis=basis,
            categories=categories,
        )


@attr.s(frozen=True, auto_attribs=True)
class StackingGroup:
    """Promotions explicitly declared as stackable, in trigger order.

    ``promotion_ids`` lists the promotions that may be combined; when they
    are combined they trigger in exactly this order, no matter in which
    order they were displayed on the page.
    """

    promotion_ids: tuple[str, ...] = attr.ib(converter=_as_str_tuple)


@attr.s(frozen=True, auto_attribs=True)
class Allocation:
    """The discount amount one promotion contributed to one item line."""

    promotion_id: str
    amount: Decimal


def _as_allocation_tuple(value: Iterable[Allocation]) -> tuple[Allocation, ...]:
    return tuple(value)


@attr.s(frozen=True, auto_attribs=True)
class LineSettlement:
    """How a single basket line ends up after the chosen promotions."""

    item_id: str
    category: str
    quantity: int
    original: Decimal
    payable: Decimal
    allocations: tuple[Allocation, ...] = attr.ib(converter=_as_allocation_tuple)

    @property
    def discount(self) -> Decimal:
        """Total discount absorbed by this line."""
        return self.original - self.payable


def _as_line_tuple(value: Iterable[LineSettlement]) -> tuple[LineSettlement, ...]:
    return tuple(value)


@attr.s(frozen=True, auto_attribs=True)
class Settlement:
    """The outcome of settling a basket with an ordered promotion plan."""

    lines: tuple[LineSettlement, ...] = attr.ib(converter=_as_line_tuple)
    applied_promotions: tuple[str, ...] = attr.ib(converter=_as_str_tuple)

    @property
    def total_original(self) -> Decimal:
        """Basket total before any promotion."""
        return sum((line.original for line in self.lines), ZERO)

    @property
    def total_payable(self) -> Decimal:
        """Basket total after the applied promotions."""
        return sum((line.payable for line in self.lines), ZERO)

    @property
    def total_discount(self) -> Decimal:
        """Total discount granted by the applied promotions."""
        return self.total_original - self.total_payable

    def allocation_by_item(self, promotion_id: str) -> dict[str, Decimal]:
        """Return ``{item_id: amount}`` for one promotion's allocations."""
        return {
            line.item_id: allocation.amount
            for line in self.lines
            for allocation in line.allocations
            if allocation.promotion_id == promotion_id
        }

    def explain(self) -> str:
        """Return a human-readable breakdown of the settlement."""
        lines = [
            f"basket original total: {self.total_original}",
            "applied promotions (trigger order): "
            + (" -> ".join(self.applied_promotions) or "(none)"),
        ]
        for line in self.lines:
            parts = ", ".join(
                f"{allocation.promotion_id} -{allocation.amount}"
                for allocation in line.allocations
            )
            lines.append(
                f"  {line.item_id} ({line.category}): "
                f"{line.original} -> {line.payable}" + (f"  [{parts}]" if parts else "")
            )
        lines.append(
            f"total payable: {self.total_payable} (discount {self.total_discount})"
        )
        return "\n".join(lines)


def evaluate_plan(
    items: Iterable[Item],
    promotions: Iterable[Promotion],
    plan: Sequence[str],
) -> Settlement:
    """Settle ``items`` with the promotions listed in ``plan``, in that order.

    Promotions whose threshold is not met, or which match no item line, are
    skipped and do not appear in :attr:`Settlement.applied_promotions`.
    This function applies exactly what it is told to; use
    :func:`best_settlement` to only combine promotions whose stacking was
    explicitly declared.
    """
    return _evaluate(tuple(items), _index_promotions(promotions), tuple(plan))


def best_settlement(
    items: Iterable[Item],
    promotions: Iterable[Promotion],
    stacking_groups: Iterable[StackingGroup] = (),
) -> Settlement:
    """Return the settlement with the lowest payable amount.

    Candidate plans are every subsequence of every declared
    :class:`StackingGroup` (trigger order fixed by the group) plus every
    promotion on its own; promotions never declared together are never
    combined. The result is independent of the order in which items,
    promotions and groups are passed in.
    """
    items = tuple(items)
    if not items:
        msg = "items must not be empty"
        raise ValueError(msg)
    item_ids = [item.item_id for item in items]
    if len(set(item_ids)) != len(item_ids):
        msg = f"duplicate item ids: {sorted(item_ids)}"
        raise ValueError(msg)
    promos = _index_promotions(promotions)
    best: Settlement | None = None
    for plan in sorted(_candidate_plans(promos, stacking_groups)):
        settlement = _evaluate(items, promos, plan)
        if best is None or settlement.total_payable < best.total_payable:
            best = settlement
    assert best is not None
    return best


def _index_promotions(promotions: Iterable[Promotion]) -> dict[str, Promotion]:
    promos: dict[str, Promotion] = {}
    for promo in promotions:
        if promo.promotion_id in promos:
            msg = f"duplicate promotion id: {promo.promotion_id!r}"
            raise ValueError(msg)
        promos[promo.promotion_id] = promo
    return promos


def _candidate_plans(
    promos: dict[str, Promotion], stacking_groups: Iterable[StackingGroup]
) -> set[tuple[str, ...]]:
    plans: set[tuple[str, ...]] = {()}
    for group in stacking_groups:
        ids = tuple(group.promotion_ids)
        if len(set(ids)) != len(ids):
            msg = f"duplicate promotion in stacking group: {list(ids)}"
            raise ValueError(msg)
        unknown = [pid for pid in ids if pid not in promos]
        if unknown:
            msg = f"stacking group references unknown promotions: {unknown}"
            raise ValueError(msg)
        for size in range(1, len(ids) + 1):
            plans.update(combinations(ids, size))
    plans.update((pid,) for pid in promos)
    return plans


def _evaluate(
    items: tuple[Item, ...], promos: dict[str, Promotion], plan: tuple[str, ...]
) -> Settlement:
    currents = [item.line_total for item in items]
    originals = list(currents)
    per_line_allocations: list[list[Allocation]] = [[] for _ in items]
    applied: list[str] = []
    for promotion_id in plan:
        try:
            promo = promos[promotion_id]
        except KeyError:
            msg = f"unknown promotion in plan: {promotion_id!r}"
            raise ValueError(msg) from None
        scope = [
            index
            for index, item in enumerate(items)
            if promo.categories is None or item.category in promo.categories
        ]
        if not scope:
            continue
        if promo.threshold is not None:
            if promo.threshold_basis is ThresholdBasis.ORIGINAL:
                basis_amount = sum((originals[i] for i in scope), ZERO)
            else:
                basis_amount = sum((currents[i] for i in scope), ZERO)
            if basis_amount < promo.threshold:
                continue
        if promo.rate is not None:
            for i in scope:
                new_amount = (currents[i] * promo.rate).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                if new_amount != currents[i]:
                    per_line_allocations[i].append(
                        Allocation(promotion_id, currents[i] - new_amount)
                    )
                    currents[i] = new_amount
        else:
            assert promo.amount_off is not None
            scope_total = sum((currents[i] for i in scope), ZERO)
            off = min(promo.amount_off, scope_total)
            shares = _allocate(
                off,
                [currents[i] for i in scope],
                [items[i].item_id for i in scope],
            )
            for i, share in zip(scope, shares):
                if share:
                    per_line_allocations[i].append(Allocation(promotion_id, share))
                    currents[i] -= share
        applied.append(promotion_id)
    lines = tuple(
        LineSettlement(
            item_id=item.item_id,
            category=item.category,
            quantity=item.quantity,
            original=originals[index],
            payable=currents[index],
            allocations=tuple(per_line_allocations[index]),
        )
        for index, item in enumerate(items)
    )
    return Settlement(lines=lines, applied_promotions=tuple(applied))


def _allocate(
    amount: Decimal, bases: list[Decimal], tie_break_ids: list[str]
) -> list[Decimal]:
    """Split ``amount`` proportionally to ``bases`` into cent-sized shares.

    Shares are floored to cents and the leftover cents go to the lines with
    the largest fractional remainders; ties are broken by item id so the
    result does not depend on the order lines were listed in.
    """
    total = sum(bases, ZERO)
    if amount <= ZERO or total <= ZERO:
        return [ZERO] * len(bases)
    raw = [amount * base / total for base in bases]
    shares = [value.quantize(CENT, rounding=ROUND_FLOOR) for value in raw]
    leftover = int((amount - sum(shares, ZERO)) / CENT)
    order = sorted(
        range(len(bases)),
        key=lambda i: (-(raw[i] - shares[i]), tie_break_ids[i]),
    )
    for i in order[:leftover]:
        shares[i] += CENT
    return shares
