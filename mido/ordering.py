# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Guaranteed ordering of events recorded on independent MIDI ports.

Each input port timestamps events with its own local clock. Ports are only
synchronized at a few points in time (for instance a sync signal recorded at
the opening and during the intermission). This module turns those sparse
sync observations into *guaranteed* ordering relations between key presses.

Guarantees
----------
* Events recorded on the same port within the same clock segment keep their
  send order unconditionally.
* Events on different ports are only ordered when their calibrated global
  time intervals do not overlap. If the calibration error is too large to
  tell them apart, both permutations are reported as possible.
* A disconnect/reconnect opens a new, independent clock segment. Events in
  different segments can never be ordered relative to each other.
* Every reported cross-port order can be traced back to the sync
  observations that support it.

Calibration model
------------------
For every ``(port, segment)`` the sync points are sorted on the local clock.
Between two consecutive sync points the clock rate is assumed constant. The
timestamp uncertainty of both endpoints widens the rate bracket, so every
event local time maps to a closed ``[lo, hi]`` interval on the shared global
time axis rather than a single point. Event ``a`` is guaranteed to happen
before event ``b`` only when ``interval(a).hi < interval(b).lo``.

Events that fall outside the range bracketed by sync points cannot be
calibrated and therefore never participate in cross-port ordering.
"""

from dataclasses import dataclass, field
from typing import Dict, Hashable, Iterable, Iterator, List, Optional, Tuple

from .messages import BaseMessage

RELATION_BEFORE = 'before'
RELATION_AFTER = 'after'
RELATION_INDETERMINATE = 'indeterminate'
RELATION_DISCONNECTED = 'disconnected'

EVIDENCE_SAME_PORT = 'same_port'
EVIDENCE_SYNC_CALIBRATION = 'sync_calibration'


class CalibrationError(ValueError):
    """Raised when sync observations cannot support a calibration."""


@dataclass(frozen=True)
class KeyEvent:
    """A single key press captured on one input port.

    ``segment`` identifies the clock segment the local timestamp belongs to.
    Every disconnect/reconnect must use a new segment id.
    """

    event_id: str
    port: str
    segment: Hashable
    local_time: float
    message: Optional[BaseMessage] = None


@dataclass(frozen=True)
class SyncPoint:
    """One observation of a shared sync signal on one port.

    Sync points carrying the same ``label`` describe the same physical
    instant. ``uncertainty`` is the symmetric timestamp error in seconds
    (the true observation lies within ``local_time +/- uncertainty``).
    ``global_time`` is optional; when omitted, labels receive integer global
    coordinates in the order in which they are first seen.
    """

    label: str
    port: str
    segment: Hashable
    local_time: float
    uncertainty: float = 0.0
    global_time: Optional[float] = None


@dataclass(frozen=True)
class TimeInterval:
    """A closed interval of possible global time values."""

    lo: float
    hi: float

    @property
    def width(self) -> float:
        return self.hi - self.lo

    def definitely_before(self, other: 'TimeInterval') -> bool:
        return self.hi < other.lo


@dataclass(frozen=True)
class OrderEvidence:
    """The observations backing one guaranteed ordering edge.

    ``sync_labels`` is empty for same-port send order and contains the
    bracketing sync labels for a cross-port calibration edge.
    """

    kind: str
    earlier_id: str
    later_id: str
    sync_labels: Tuple[str, ...] = field(default_factory=tuple)
    detail: str = ''


@dataclass(frozen=True)
class _Bracket:
    lo_local: float
    hi_local: float
    lo_global: float
    hi_global: float
    rate_lo: float
    rate_hi: float
    lo_uncertainty: float
    lo_label: str
    hi_label: str

    def contains(self, local_time: float) -> bool:
        return self.lo_local <= local_time <= self.hi_local

    def interval(self, local_time: float) -> TimeInterval:
        offset = local_time - self.lo_local
        lo = self.lo_global - self.lo_uncertainty + self.rate_lo * offset
        hi = self.lo_global + self.lo_uncertainty + self.rate_hi * offset
        return TimeInterval(lo=lo, hi=hi)


class EventOrderer:
    """Builds and queries the guaranteed partial order of key events."""

    def __init__(
        self,
        events: Iterable[KeyEvent],
        sync_points: Iterable[SyncPoint],
    ) -> None:
        self._events: List[KeyEvent] = list(events)
        self._sync_points: List[SyncPoint] = list(sync_points)
        self._by_id: Dict[str, KeyEvent] = {}
        self._index: Dict[str, int] = {}
        self._intervals: Dict[str, Optional[TimeInterval]] = {}
        self._brackets: Dict[Tuple[str, Hashable], List[_Bracket]] = {}
        self._edges: Dict[str, set] = {}
        self._edge_evidence: Dict[Tuple[str, str], OrderEvidence] = {}
        self._reachable: Dict[str, set] = {}

        self._validate_events()
        global_coords = self._global_coordinates()
        self._brackets = self._build_brackets(global_coords)
        self._calibrate_events()
        self._build_graph()

    def _validate_events(self) -> None:
        seen = set()
        for index, event in enumerate(self._events):
            if event.event_id in seen:
                raise CalibrationError(
                    f'duplicate event id: {event.event_id!r}'
                )
            seen.add(event.event_id)
            self._by_id[event.event_id] = event
            self._index[event.event_id] = index
            self._edges[event.event_id] = set()
            self._reachable[event.event_id] = {event.event_id}
        for point in self._sync_points:
            if point.uncertainty < 0.0:
                raise CalibrationError(
                    f'sync point {point.label!r} has negative uncertainty'
                )

    def _global_coordinates(self) -> Dict[str, float]:
        coordinates: Dict[str, float] = {}
        for point in self._sync_points:
            if point.label in coordinates:
                if point.global_time is not None and (
                    coordinates[point.label] != point.global_time
                ):
                    raise CalibrationError(
                        f'conflicting global_time for label {point.label!r}'
                    )
                continue
            if point.global_time is None:
                coordinates[point.label] = float(len(coordinates))
            else:
                coordinates[point.label] = point.global_time
        return coordinates

    def _build_brackets(
        self, global_coords: Dict[str, float]
    ) -> Dict[Tuple[str, Hashable], List[_Bracket]]:
        grouped: Dict[Tuple[str, Hashable], List[SyncPoint]] = {}
        for point in self._sync_points:
            grouped.setdefault((point.port, point.segment), []).append(point)

        segment_ports: Dict[Hashable, set] = {}
        for port, segment in grouped:
            segment_ports.setdefault(segment, set()).add(port)

        brackets: Dict[Tuple[str, Hashable], List[_Bracket]] = {}
        for segment, ports in segment_ports.items():
            common_labels = None
            for port in ports:
                labels = {
                    point.label for point in grouped[(port, segment)]
                }
                if common_labels is None:
                    common_labels = labels
                else:
                    common_labels &= labels
            if not common_labels or len(common_labels) < 2:
                continue

            for port in ports:
                points = sorted(
                    (
                        point
                        for point in grouped[(port, segment)]
                        if point.label in common_labels
                    ),
                    key=lambda point: point.local_time,
                )
                self._check_points(points, port, segment)
                port_brackets = []
                for earlier, later in zip(points, points[1:]):
                    duration = later.local_time - earlier.local_time
                    error = earlier.uncertainty + later.uncertainty
                    if duration - error <= 0.0:
                        raise CalibrationError(
                            f'sync points {earlier.label!r} and '
                            f'{later.label!r} on port {port!r} are not '
                            'separable given their uncertainty'
                        )
                    global_span = (
                        global_coords[later.label]
                        - global_coords[earlier.label]
                    )
                    if global_span <= 0.0:
                        raise CalibrationError(
                            f'sync labels {earlier.label!r} and '
                            f'{later.label!r} must have increasing global '
                            'times'
                        )
                    port_brackets.append(
                        _Bracket(
                            lo_local=earlier.local_time,
                            hi_local=later.local_time,
                            lo_global=global_coords[earlier.label],
                            hi_global=global_coords[later.label],
                            rate_lo=global_span / (duration + error),
                            rate_hi=global_span / (duration - error),
                            lo_uncertainty=earlier.uncertainty,
                            lo_label=earlier.label,
                            hi_label=later.label,
                        )
                    )
                brackets[(port, segment)] = port_brackets
        return brackets

    @staticmethod
    def _check_points(
        points: List[SyncPoint], port: str, segment: Hashable
    ) -> None:
        seen_labels = set()
        previous_time = None
        for point in points:
            if point.label in seen_labels:
                raise CalibrationError(
                    f'duplicate sync label {point.label!r} on port '
                    f'{port!r} segment {segment!r}'
                )
            seen_labels.add(point.label)
            if (
                previous_time is not None
                and point.local_time <= previous_time
            ):
                raise CalibrationError(
                    f'sync points on port {port!r} segment {segment!r} '
                    'must have strictly increasing local times'
                )
            previous_time = point.local_time

    def _calibrate_events(self) -> None:
        for event in self._events:
            interval = None
            for bracket in self._brackets.get((event.port, event.segment), ()):
                if bracket.contains(event.local_time):
                    interval = bracket.interval(event.local_time)
                    break
            self._intervals[event.event_id] = interval

    def _add_edge(self, earlier: str, later: str, evidence: OrderEvidence) -> None:
        if later not in self._edges[earlier]:
            self._edges[earlier].add(later)
            self._edge_evidence[(earlier, later)] = evidence

    def _build_graph(self) -> None:
        groups: Dict[Tuple[str, Hashable], List[KeyEvent]] = {}
        for event in self._events:
            groups.setdefault((event.port, event.segment), []).append(event)

        for grouped_events in groups.values():
            ordered = sorted(
                grouped_events,
                key=lambda event: (
                    event.local_time,
                    self._index[event.event_id],
                ),
            )
            for earlier, later in zip(ordered, ordered[1:]):
                self._add_edge(
                    earlier.event_id,
                    later.event_id,
                    OrderEvidence(
                        kind=EVIDENCE_SAME_PORT,
                        earlier_id=earlier.event_id,
                        later_id=later.event_id,
                        detail=(
                            f'send order on port {earlier.port!r} segment '
                            f'{earlier.segment}'
                        ),
                    ),
                )

        segments: Dict[Hashable, List[KeyEvent]] = {}
        for event in self._events:
            segments.setdefault(event.segment, []).append(event)

        for grouped_events in segments.values():
            for index, first in enumerate(grouped_events):
                for second in grouped_events[index + 1:]:
                    if first.port == second.port:
                        continue
                    self._connect_calibrated_pair(first, second)

        self._compute_closure()

    def _connect_calibrated_pair(
        self, first: KeyEvent, second: KeyEvent
    ) -> None:
        first_interval = self._intervals[first.event_id]
        second_interval = self._intervals[second.event_id]
        if first_interval is None or second_interval is None:
            return

        if first_interval.definitely_before(second_interval):
            earlier, later, earlier_event, later_event = (
                first_interval,
                second_interval,
                first,
                second,
            )
        elif second_interval.definitely_before(first_interval):
            earlier, later, earlier_event, later_event = (
                second_interval,
                first_interval,
                second,
                first,
            )
        else:
            return

        labels = self._supporting_labels(earlier_event) + (
            self._supporting_labels(later_event)
        )
        unique_labels = tuple(
            sorted(set(labels), key=lambda label: labels.index(label))
        )
        self._add_edge(
            earlier_event.event_id,
            later_event.event_id,
            OrderEvidence(
                kind=EVIDENCE_SYNC_CALIBRATION,
                earlier_id=earlier_event.event_id,
                later_id=later_event.event_id,
                sync_labels=unique_labels,
                detail=(
                    f'calibrated intervals '
                    f'[{earlier.lo:.6f}, {earlier.hi:.6f}] end before '
                    f'[{later.lo:.6f}, {later.hi:.6f}] begins'
                ),
            ),
        )

    def _supporting_labels(self, event: KeyEvent) -> Tuple[str, ...]:
        for bracket in self._brackets.get((event.port, event.segment), ()):
            if bracket.contains(event.local_time):
                return bracket.lo_label, bracket.hi_label
        return ()

    def _compute_closure(self) -> None:
        changed = True
        while changed:
            changed = False
            for node in self._reachable:
                updated = set(self._reachable[node])
                for successor in self._edges[node]:
                    updated |= self._reachable[successor]
                if updated != self._reachable[node]:
                    self._reachable[node] = updated
                    changed = True

    def interval(self, event_id: str) -> Optional[TimeInterval]:
        """Return the calibrated global interval, or ``None`` if uncovered."""
        return self._intervals[event_id]

    def definitely_before(self, earlier_id: str, later_id: str) -> bool:
        """Return whether ``earlier_id`` is guaranteed to come first."""
        return later_id in self._reachable[earlier_id]

    def relation(self, first_id: str, second_id: str) -> str:
        """Return the guaranteed relation between two events."""
        if self.definitely_before(first_id, second_id):
            return RELATION_BEFORE
        if self.definitely_before(second_id, first_id):
            return RELATION_AFTER
        first = self._by_id[first_id]
        second = self._by_id[second_id]
        if first.segment != second.segment:
            return RELATION_DISCONNECTED
        return RELATION_INDETERMINATE

    def evidence(
        self, earlier_id: str, later_id: str
    ) -> List[OrderEvidence]:
        """Return observations supporting the claimed order.

        Returns an empty list when the order cannot be guaranteed.
        """
        if not self.definitely_before(earlier_id, later_id):
            return []
        predecessors = {earlier_id}
        came_from: Dict[str, Tuple[str, OrderEvidence]] = {}
        queue = [earlier_id]
        while queue:
            node = queue.pop(0)
            if node == later_id:
                break
            for successor in self._edges[node]:
                if successor not in predecessors:
                    predecessors.add(successor)
                    came_from[successor] = (
                        node,
                        self._edge_evidence[(node, successor)],
                    )
                    queue.append(successor)

        path = []
        node = later_id
        while node != earlier_id:
            previous, edge_evidence = came_from[node]
            path.append(edge_evidence)
            node = previous
        path.reverse()
        return path

    def guaranteed_pairs(self) -> List[Tuple[str, str]]:
        """Return every ``(earlier, later)`` pair with a guaranteed order."""
        return [
            (earlier, later)
            for earlier, reachable in self._reachable.items()
            for later in sorted(reachable)
            if later != earlier
        ]

    def ambiguous_pairs(self) -> List[Tuple[str, str]]:
        """Return event pairs for which neither order can be guaranteed."""
        ids = list(self._by_id)
        pairs = []
        for index, first in enumerate(ids):
            for second in ids[index + 1:]:
                if not self.definitely_before(first, second) and not (
                    self.definitely_before(second, first)
                ):
                    pairs.append((first, second))
        return pairs

    def linearizations(
        self, limit: Optional[int] = None
    ) -> Iterator[Tuple[str, ...]]:
        """Yield total orderings consistent with every guaranteed edge."""
        indegree = {event_id: 0 for event_id in self._by_id}
        for successors in self._edges.values():
            for successor in successors:
                indegree[successor] += 1

        produced = 0

        def extend(
            available: set, prefix: List[str], remaining: Dict[str, int]
        ) -> Iterator[Tuple[str, ...]]:
            nonlocal produced
            if limit is not None and produced >= limit:
                return
            if not available:
                produced += 1
                yield tuple(prefix)
                return
            for event_id in sorted(available):
                available.remove(event_id)
                prefix.append(event_id)
                unlocked = []
                for successor in self._edges[event_id]:
                    remaining[successor] -= 1
                    if remaining[successor] == 0:
                        available.add(successor)
                        unlocked.append(successor)
                yield from extend(available, prefix, remaining)
                for successor in unlocked:
                    available.remove(successor)
                for successor in self._edges[event_id]:
                    remaining[successor] += 1
                prefix.pop()
                available.add(event_id)

        available = {
            event_id
            for event_id, degree in indegree.items()
            if degree == 0
        }
        yield from extend(available, [], dict(indegree))

    def example_orderings(
        self, first_id: str, second_id: str
    ) -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
        """Return one linearization with each event first.

        Returns ``None`` when one of the directions is not possible, i.e.
        when a guaranteed ordering exists between the two events.
        """
        if (
            self.definitely_before(first_id, second_id)
            or self.definitely_before(second_id, first_id)
        ):
            return None
        # The events are incomparable, so forcing one direction with a
        # temporary edge cannot introduce a cycle.
        first_leading = self._toposort_with_edge((first_id, second_id))
        second_leading = self._toposort_with_edge((second_id, first_id))
        return first_leading, second_leading

    def _toposort_with_edge(
        self, extra_edge: Tuple[str, str]
    ) -> Tuple[str, ...]:
        extra_from, extra_to = extra_edge
        indegree = {event_id: 0 for event_id in self._by_id}
        for successors in self._edges.values():
            for successor in successors:
                indegree[successor] += 1
        indegree[extra_to] += 1
        available = {
            event_id for event_id, degree in indegree.items() if degree == 0
        }
        ordering = []
        while available:
            chosen = min(available)
            ordering.append(chosen)
            available.remove(chosen)
            successors = set(self._edges[chosen])
            if chosen == extra_from:
                successors.add(extra_to)
            for successor in successors:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    available.add(successor)
        return tuple(ordering)

    def is_consistent(self, ordering: Iterable[str]) -> bool:
        """Check whether a total ordering respects every guaranteed edge."""
        positions: Dict[str, int] = {}
        for position, event_id in enumerate(ordering):
            positions[event_id] = position
        if set(positions) != set(self._by_id):
            return False
        for earlier, later in self.guaranteed_pairs():
            if positions[earlier] >= positions[later]:
                return False
        return True
