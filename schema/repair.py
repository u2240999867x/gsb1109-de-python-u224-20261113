"""Cost-aware repair planning for records rejected by a field contract.

A contract declares, for every field, the values it accepts, the supplier cost
of pulling the original document for that field, and the relations that must
hold between fields.  One record can break several relations at once.

``RepairPlanner`` selects the cheapest group of fields whose amendment makes the
record pass the *same* contract while every other value stays fixed.  Fields
declared as already verified are frozen.  The planner never edits the record: it
returns the amendment as a review lead, and the caller decides what to do with
it.  When two cheapest groups tie on cost but draw on different sources, both
are returned for the duty officer to choose between.
"""

import heapq
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Sequence,
    Set,
    Tuple,
    Union,
)


class RepairSearchLimitError(RuntimeError):
    """Raised when the bounded repair search cannot settle a question."""


_MISSING = object()


class FieldSpec(object):
    """Allowed values, review cost and supplier source for one contract field."""

    def __init__(
        self,
        allowed: Iterable[Any],
        cost: Union[int, float] = 1,
        source: Union[str, None] = None,
    ) -> None:
        if isinstance(allowed, (str, bytes)):
            raise TypeError("'allowed' must be a collection of values, not text")
        if cost < 0:
            raise ValueError("field review cost must not be negative")
        self.allowed: Tuple[Any, ...] = tuple(allowed)
        self.cost = cost
        self.source = source

    def allows(self, value: Any) -> bool:
        return any(value == candidate for candidate in self.allowed)

    def __repr__(self) -> str:
        return "FieldSpec(allowed=%r, cost=%r, source=%r)" % (
            self.allowed,
            self.cost,
            self.source,
        )


class Relation(object):
    """A named predicate that must hold between named fields."""

    def __init__(
        self,
        name: str,
        fields: Iterable[str],
        check: Callable[[Mapping[str, Any]], bool],
    ) -> None:
        self.name = name
        self.fields: Tuple[str, ...] = tuple(fields)
        if not self.fields:
            raise ValueError("relation %r must reference at least one field" % name)
        self.check = check

    def holds(self, record: Mapping[str, Any]) -> bool:
        return bool(self.check(record))

    def __repr__(self) -> str:
        return "Relation(%r, %r)" % (self.name, self.fields)


class Failure(object):
    """One violated field relation.

    ``kind`` is ``value`` (outside the allowed values), ``missing`` (required
    field absent), ``unknown`` (field not declared by the contract) or
    ``relation`` (a cross-field relation does not hold).  ``fields`` are the
    original fields the failure is about and ``allowed`` their allowed values.
    """

    def __init__(
        self,
        kind: str,
        fields: Iterable[str],
        allowed: Mapping[str, Iterable[Any]],
        message: str,
    ) -> None:
        self.kind = kind
        self.fields: Tuple[str, ...] = tuple(fields)
        self.allowed: Dict[str, Tuple[Any, ...]] = {
            name: tuple(values) for name, values in allowed.items()
        }
        self.message = message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "fields": list(self.fields),
            "allowed": {name: list(values) for name, values in self.allowed.items()},
            "message": self.message,
        }

    def __repr__(self) -> str:
        return "Failure(%r, %r, %r)" % (self.kind, self.fields, self.message)


class RepairOption(object):
    """One cheapest feasible group of fields and the amendment it admits."""

    def __init__(
        self,
        fields: Sequence[str],
        total_cost: Union[int, float],
        sources: Sequence[str],
        amendments: Mapping[str, Any],
        amended_record: Mapping[str, Any],
    ) -> None:
        self.fields: Tuple[str, ...] = tuple(fields)
        self.total_cost = total_cost
        self.sources: Tuple[str, ...] = tuple(sources)
        self.amendments: Dict[str, Any] = dict(amendments)
        self.amended_record: Dict[str, Any] = dict(amended_record)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fields": list(self.fields),
            "total_cost": self.total_cost,
            "sources": list(self.sources),
            "amendments": dict(self.amendments),
            "amended_record": dict(self.amended_record),
        }

    def __repr__(self) -> str:
        return "RepairOption(fields=%r, total_cost=%r, sources=%r)" % (
            self.fields,
            self.total_cost,
            self.sources,
        )


class RepairPlan(object):
    """Outcome of a planning run; the input record is never modified."""

    STATUS_OK = "ok"
    STATUS_REPAIRABLE = "repairable"
    STATUS_REQUIRES_ARBITRATION = "requires_arbitration"
    STATUS_UNREPAIRABLE = "unrepairable"

    def __init__(
        self,
        record: Mapping[str, Any],
        verified: Iterable[str],
        failures: Sequence[Failure],
        options: Sequence[RepairOption],
        status: str,
        reason: Union[str, None],
        blocked_verified: Sequence[str],
        costs: Mapping[str, Union[int, float]],
        sources: Mapping[str, Union[str, None]],
    ) -> None:
        self.record = record
        self.verified: Tuple[str, ...] = tuple(sorted(verified))
        self.failures: Tuple[Failure, ...] = tuple(failures)
        self.options: Tuple[RepairOption, ...] = tuple(options)
        self.status = status
        self.reason = reason
        self.blocked_verified: Tuple[str, ...] = tuple(blocked_verified)
        self.costs = dict(costs)
        self.sources = dict(sources)

    @property
    def best_cost(self) -> Union[int, float, None]:
        return self.options[0].total_cost if self.options else None

    @property
    def requires_arbitration(self) -> bool:
        return self.status == self.STATUS_REQUIRES_ARBITRATION

    @property
    def options_by_source(self) -> Dict[Tuple[str, ...], List[RepairOption]]:
        """Cheapest options grouped by the sources they would query."""
        grouped: Dict[Tuple[str, ...], List[RepairOption]] = {}
        for option in self.options:
            grouped.setdefault(option.sources, []).append(option)
        return grouped

    @property
    def failure_leads(self) -> List[Dict[str, Any]]:
        """Every failure as a review lead: fields, allowed values, cost, source."""
        leads = []
        for failure in self.failures:
            leads.append(
                {
                    "kind": failure.kind,
                    "fields": list(failure.fields),
                    "allowed": {
                        name: list(values) for name, values in failure.allowed.items()
                    },
                    "costs": {name: self.costs.get(name) for name in failure.fields},
                    "sources": {
                        name: self.sources.get(name) for name in failure.fields
                    },
                    "message": failure.message,
                }
            )
        return leads

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "requires_arbitration": self.requires_arbitration,
            "reason": self.reason,
            "best_cost": self.best_cost,
            "verified": list(self.verified),
            "blocked_verified": list(self.blocked_verified),
            "failures": self.failure_leads,
            "options": [option.to_dict() for option in self.options],
        }

    def __repr__(self) -> str:
        return "RepairPlan(status=%r, best_cost=%r, options=%d)" % (
            self.status,
            self.best_cost,
            len(self.options),
        )


class Contract(object):
    """Field rules plus relations that a record has to satisfy."""

    def __init__(
        self,
        fields: Mapping[str, Union[FieldSpec, Iterable[Any]]],
        relations: Iterable[Relation] = (),
        required: Iterable[str] = (),
    ) -> None:
        self.fields: Dict[str, FieldSpec] = {}
        for name, spec in fields.items():
            self.fields[name] = spec if isinstance(spec, FieldSpec) else FieldSpec(spec)
        self.relations: Tuple[Relation, ...] = tuple(relations)
        self.required: Tuple[str, ...] = tuple(required)

        for relation in self.relations:
            for name in relation.fields:
                if name not in self.fields:
                    raise ValueError(
                        "relation %r references undeclared field %r"
                        % (relation.name, name)
                    )
        for name in self.required:
            if name not in self.fields:
                raise ValueError("required field %r is not declared" % name)

    def cost_of(self, field: str) -> Union[int, float]:
        return self.fields[field].cost

    def source_of(self, field: str) -> Union[str, None]:
        return self.fields[field].source

    def failures(self, record: Mapping[str, Any]) -> List[Failure]:
        """Return every violation at once, not just the first one."""
        if not isinstance(record, Mapping):
            raise TypeError("record must be a mapping of field names to values")

        failures: List[Failure] = []

        for name in sorted(record):
            if name not in self.fields:
                failures.append(
                    Failure(
                        "unknown",
                        (name,),
                        {},
                        "field %r is not declared by the contract" % name,
                    )
                )

        for name in sorted(self.required):
            if name not in record:
                failures.append(
                    Failure(
                        "missing",
                        (name,),
                        {name: self.fields[name].allowed},
                        "required field %r is missing" % name,
                    )
                )

        for name in sorted(record):
            if name not in self.fields:
                continue
            spec = self.fields[name]
            if not spec.allows(record[name]):
                failures.append(
                    Failure(
                        "value",
                        (name,),
                        {name: spec.allowed},
                        "value %r is not allowed for field %r" % (record[name], name),
                    )
                )

        for relation in self.relations:
            try:
                holds = relation.holds(record)
                raised = None
            except Exception as exc:
                holds = False
                raised = exc
            if holds:
                continue
            allowed = {name: self.fields[name].allowed for name in relation.fields}
            if raised is not None:
                message = "relation %r raised %r" % (relation.name, raised)
            else:
                message = "relation %r does not hold on fields %r" % (
                    relation.name,
                    relation.fields,
                )
            failures.append(Failure("relation", relation.fields, allowed, message))

        return failures

    def is_valid(self, record: Mapping[str, Any]) -> bool:
        return not self.failures(record)

    def repair(
        self,
        record: Mapping[str, Any],
        verified: Iterable[str] = (),
        max_subsets: int = 100000,
        max_attempts: int = 1000000,
    ) -> RepairPlan:
        return RepairPlanner(
            self, max_subsets=max_subsets, max_attempts=max_attempts
        ).plan(record, verified=verified)

    def __repr__(self) -> str:
        return "Contract(fields=%r, relations=%r)" % (
            tuple(sorted(self.fields)),
            self.relations,
        )


class RepairPlanner(object):
    """Select the cheapest group of fields admitting a passing amendment."""

    def __init__(
        self,
        contract: Contract,
        max_subsets: int = 100000,
        max_attempts: int = 1000000,
    ) -> None:
        self.contract = contract
        self.max_subsets = max_subsets
        self.max_attempts = max_attempts

    def plan(
        self, record: Mapping[str, Any], verified: Iterable[str] = ()
    ) -> RepairPlan:
        failures = self.contract.failures(record)
        costs = {name: spec.cost for name, spec in self.contract.fields.items()}
        sources = {name: spec.source for name, spec in self.contract.fields.items()}
        verified_set: FrozenSet[str] = frozenset(verified)

        if not failures:
            return RepairPlan(
                record,
                verified_set,
                failures,
                (),
                RepairPlan.STATUS_OK,
                None,
                (),
                costs,
                sources,
            )

        unknown = [failure for failure in failures if failure.kind == "unknown"]
        if unknown:
            names = sorted(failure.fields[0] for failure in unknown)
            return RepairPlan(
                record,
                verified_set,
                failures,
                (),
                RepairPlan.STATUS_UNREPAIRABLE,
                "record carries fields outside the contract: %s" % ", ".join(names),
                (),
                costs,
                sources,
            )

        blocked = sorted(
            {
                name
                for failure in failures
                if failure.kind in ("value", "missing")
                for name in failure.fields
                if name in verified_set
            }
        )
        if blocked:
            return RepairPlan(
                record,
                verified_set,
                failures,
                (),
                RepairPlan.STATUS_UNREPAIRABLE,
                "verified values cannot be amended: %s" % ", ".join(blocked),
                blocked,
                costs,
                sources,
            )

        pool = self._candidate_fields(failures) - verified_set
        if not pool:
            return RepairPlan(
                record,
                verified_set,
                failures,
                (),
                RepairPlan.STATUS_UNREPAIRABLE,
                "no amendable field: every failing field is verified",
                sorted(self._candidate_fields(failures) & verified_set),
                costs,
                sources,
            )

        options = self._search(record, tuple(sorted(pool)))
        if not options:
            return RepairPlan(
                record,
                verified_set,
                failures,
                (),
                RepairPlan.STATUS_UNREPAIRABLE,
                "no group of amendable fields admits an amendment that passes "
                "the contract",
                sorted(self._candidate_fields(failures) & verified_set),
                costs,
                sources,
            )

        source_sets: Set[FrozenSet[str]] = {
            frozenset(option.sources) for option in options
        }
        status = (
            RepairPlan.STATUS_REQUIRES_ARBITRATION
            if len(source_sets) > 1
            else RepairPlan.STATUS_REPAIRABLE
        )
        return RepairPlan(
            record,
            verified_set,
            failures,
            options,
            status,
            None,
            (),
            costs,
            sources,
        )

    def _candidate_fields(self, failures: Sequence[Failure]) -> Set[str]:
        """Fields that could take part in an amendment.

        Starts from the fields every failure reports and closes over relations,
        because changing one field can force another field of a relation to
        move as well.
        """
        pool: Set[str] = set()
        for failure in failures:
            pool.update(failure.fields)
        changed = True
        while changed:
            changed = False
            for relation in self.contract.relations:
                if pool.intersection(relation.fields):
                    before = len(pool)
                    pool.update(relation.fields)
                    changed = changed or len(pool) != before
        return pool

    def _search(
        self, record: Mapping[str, Any], candidates: Sequence[str]
    ) -> List[RepairOption]:
        order = sorted(
            candidates, key=lambda name: (self.contract.fields[name].cost, name)
        )
        spec_cost = [self.contract.fields[name].cost for name in order]
        spec_source = [self.contract.fields[name].source for name in order]

        heap: List[Tuple[Union[int, float], Tuple[int, ...]]] = [(0, ())]
        best_cost: Union[int, float, None] = None
        found: List[RepairOption] = []
        subsets = 0

        while heap:
            cost, indices = heapq.heappop(heap)
            if best_cost is not None and cost > best_cost:
                break

            subsets += 1
            if subsets > self.max_subsets:
                raise RepairSearchLimitError(
                    "repair search exceeded %d field groups" % self.max_subsets
                )

            fields = tuple(order[index] for index in indices)
            witness = self._find_witness(record, fields)
            if witness is not None:
                best_cost = cost
                amended = dict(record)
                amended.update(witness)
                found.append(
                    RepairOption(
                        fields,
                        cost,
                        sorted(
                            {
                                spec_source[index]
                                for index in indices
                                if spec_source[index] is not None
                            }
                        ),
                        witness,
                        amended,
                    )
                )

            start = indices[-1] + 1 if indices else 0
            for index in range(start, len(order)):
                heapq.heappush(heap, (cost + spec_cost[index], indices + (index,)))

        found.sort(key=lambda option: (option.total_cost, option.fields))
        return found

    def _find_witness(
        self, record: Mapping[str, Any], fields: Sequence[str]
    ) -> Union[Dict[str, Any], None]:
        """Return the first amendment of exactly ``fields`` that passes.

        A field whose value is absent from the record may be filled in without
        counting as a change; a present field has to move to another value,
        otherwise a narrower group would already have been feasible and cheaper.
        """
        chosen = tuple(sorted(fields))
        decided: Dict[str, Any] = dict(record)
        assigned: Set[str] = set()
        attempts = [0]

        def relations_hold() -> bool:
            for relation in self.contract.relations:
                if not all(name in assigned for name in relation.fields):
                    continue
                try:
                    if not relation.holds(decided):
                        return False
                except Exception:
                    return False
            return True

        def assign(position: int) -> Union[Dict[str, Any], None]:
            attempts[0] += 1
            if attempts[0] > self.max_attempts:
                raise RepairSearchLimitError(
                    "repair search exceeded %d value assignments" % self.max_attempts
                )

            if position == len(chosen):
                amended = dict(record)
                amended.update({name: decided[name] for name in chosen})
                if not self.contract.failures(amended):
                    return {name: decided[name] for name in chosen}
                return None

            name = chosen[position]
            current = record.get(name, _MISSING)
            for value in self.contract.fields[name].allowed:
                if value == current:
                    continue
                decided[name] = value
                assigned.add(name)
                if relations_hold():
                    witness = assign(position + 1)
                    if witness is not None:
                        return witness
                assigned.discard(name)
                del decided[name]
            return None

        return assign(0)
