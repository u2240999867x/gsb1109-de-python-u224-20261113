# SPDX-FileCopyrightText: 2026 mido contributors
#
# SPDX-License-Identifier: MIT

"""Velocity-to-force calibration tables.

A manufacturer calibration table is not a response curve. It is a set of
records, each pairing a force range with a MIDI velocity that may be
reported when the key is struck with a force inside that range. Several
records may share a velocity, and force ranges are allowed to overlap.

For a given device the encoding must be monotonic: when one force range
lies strictly above another, the velocities reachable from the stronger
range must not be smaller than those reachable from the weaker one.

This module turns such a table into answers that can be defended from the
records alone:

* :meth:`VelocityTable.force_range_for` restores the provable force range
  of a sampled key. Values that cannot be told apart stay an interval:
  when several records cover one velocity, their ranges are merged into
  the smallest closed interval containing all of them.
* :meth:`VelocityTable.find_conflict` locates a pair of records that
  cannot both hold under the monotonicity rule, and
  :meth:`VelocityTable.check_monotonic` raises it as
  :class:`CalibrationConflict`.
* :func:`compare_velocity` decides which of two devices was struck
  harder only when the two provable force ranges are strictly separated.
  Overlapping or touching ranges are reported as indistinguishable.
"""

from numbers import Integral, Real
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

MIN_VELOCITY = 0
MAX_VELOCITY = 127


class ForceRange:
    """A closed force interval ``[low, high]``.

    Both endpoints are included. Instances are immutable and hashable.
    """

    __slots__ = ('low', 'high')

    def __init__(self, low: Real, high: Real):
        for name, value in (('low', low), ('high', high)):
            if not isinstance(value, Real):
                raise TypeError(
                    'force range {} must be int or float'.format(name))
        if low > high:
            raise ValueError(
                'force range low ({}) must not be above high ({})'.format(
                    low, high))
        object.__setattr__(self, 'low', low)
        object.__setattr__(self, 'high', high)

    def __setattr__(self, name, value):
        raise ValueError('ForceRange is immutable')

    def __contains__(self, force: Real) -> bool:
        return self.low <= force <= self.high

    def __eq__(self, other) -> bool:
        if not isinstance(other, ForceRange):
            return NotImplemented
        return self.low == other.low and self.high == other.high

    def __hash__(self) -> int:
        return hash((self.low, self.high))

    def __repr__(self) -> str:
        return 'ForceRange(low={!r}, high={!r})'.format(self.low, self.high)

    def intersection(self, other: 'ForceRange') -> Optional['ForceRange']:
        """Return the overlap of the two ranges, or ``None``.

        Ranges that only touch at an endpoint intersect at that single
        point, so a one-point range is returned.
        """
        low = max(self.low, other.low)
        high = min(self.high, other.high)
        if low > high:
            return None
        return ForceRange(low, high)

    def union_hull(self, other: 'ForceRange') -> 'ForceRange':
        """Return the smallest closed range containing both ranges."""
        return ForceRange(
            min(self.low, other.low), max(self.high, other.high))

    def is_strictly_above(self, other: 'ForceRange') -> bool:
        """Return True if every point in this range exceeds every point
        in ``other``. Ranges that touch at an endpoint are not strictly
        separated because that force is reachable from both of them."""
        return other.high < self.low


class CalibrationRecord(NamedTuple):
    """One manufacturer record: a force range and a reportable velocity."""

    force_range: ForceRange
    velocity: int


class CalibrationConflict(ValueError):
    """Raised when calibration records break the monotonic encoding rule.

    The two records cannot both hold: ``weaker`` covers a strictly lower
    force range than ``stronger`` while carrying the larger velocity, so
    the encoding would decrease as the force increases.
    """

    def __init__(self, weaker: CalibrationRecord,
                 stronger: CalibrationRecord):
        self.weaker = weaker
        self.stronger = stronger
        message = (
            'calibration conflict: velocity {} is reported for {} but the '
            'strictly stronger range {} only reaches velocity {}; encoding '
            'must not decrease as force increases'.format(
                weaker.velocity, weaker.force_range,
                stronger.force_range, stronger.velocity))
        super().__init__(message)


def _check_velocity(velocity: int) -> None:
    if not isinstance(velocity, Integral):
        raise TypeError('velocity must be int')
    if not MIN_VELOCITY <= velocity <= MAX_VELOCITY:
        raise ValueError(
            'velocity must be in range {}..{}'.format(
                MIN_VELOCITY, MAX_VELOCITY))


def _as_record(item) -> CalibrationRecord:
    if isinstance(item, CalibrationRecord):
        return item
    try:
        force_range, velocity = item
    except (TypeError, ValueError) as err:
        raise TypeError(
            'each record must be a (ForceRange, velocity) pair') from err
    if not isinstance(force_range, ForceRange):
        raise TypeError('force range must be a ForceRange instance')
    _check_velocity(velocity)
    return CalibrationRecord(force_range, velocity)


class VelocityTable:
    """A manufacturer table mapping force ranges to reportable velocities.

    Records can be passed to the constructor as ``(ForceRange, velocity)``
    pairs (or :class:`CalibrationRecord` objects) and added later with
    :meth:`add`. Construction never rejects a conflicting table; use
    :meth:`check_monotonic` or :meth:`find_conflict` to inspect it.
    """

    def __init__(self, records: Iterable = ()):
        self._records = []  # type: List[CalibrationRecord]
        self._ranges = {}   # type: Dict[int, List[ForceRange]]
        for item in records:
            record = _as_record(item)
            self._records.append(record)
            self._ranges.setdefault(record.velocity, []).append(
                record.force_range)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    def __contains__(self, velocity: int) -> bool:
        return velocity in self._ranges

    def add(self, force_range: ForceRange, velocity: int) -> None:
        """Add one ``(force_range, velocity)`` calibration record."""
        if not isinstance(force_range, ForceRange):
            raise TypeError('force range must be a ForceRange instance')
        _check_velocity(velocity)
        record = CalibrationRecord(force_range, velocity)
        self._records.append(record)
        self._ranges.setdefault(velocity, []).append(force_range)

    @property
    def records(self) -> Tuple[CalibrationRecord, ...]:
        """All records in insertion order."""
        return tuple(self._records)

    @property
    def velocities(self) -> Tuple[int, ...]:
        """All velocities that have at least one record, sorted."""
        return tuple(sorted(self._ranges))

    def ranges_for(self, velocity: int) -> Tuple[ForceRange, ...]:
        """Return the raw force ranges recorded for ``velocity``.

        Overlapping ranges are kept separate so no manufacturer
        information is discarded.
        """
        try:
            return tuple(self._ranges[velocity])
        except KeyError as err:
            raise KeyError(
                'no calibration record for velocity {}'.format(
                    velocity)) from err

    def force_range_for(self, velocity: int) -> ForceRange:
        """Return the provable force range of a key reporting ``velocity``.

        Every recorded range for the velocity is possible, so the result
        is the smallest closed interval covering all of them. A key whose
        exact force cannot be pinned down is therefore returned as an
        interval rather than a single value.
        """
        ranges = self.ranges_for(velocity)
        return ForceRange(
            min(force_range.low for force_range in ranges),
            max(force_range.high for force_range in ranges))

    def velocities_for(self, force: Real) -> Tuple[int, ...]:
        """Return every velocity reportable at ``force``, sorted."""
        if not isinstance(force, Real):
            raise TypeError('force must be int or float')
        result = [
            velocity for velocity, ranges in self._ranges.items()
            if any(force in force_range for force_range in ranges)
        ]
        return tuple(sorted(result))

    def find_conflict(
            self) -> Optional[Tuple[CalibrationRecord,
                                    CalibrationRecord]]:
        """Return a conflicting record pair, or ``None`` when monotonic.

        The returned pair is ``(weaker, stronger)``: ``weaker`` has a
        strictly lower force range but the larger velocity, which the
        monotonic encoding rule forbids. Overlapping ranges never
        conflict because neither force can be ruled out.
        """
        ordered = sorted(
            self._records,
            key=lambda record: (
                record.force_range.low, record.force_range.high))
        for index, weaker in enumerate(ordered):
            for stronger in ordered[index + 1:]:
                if stronger.force_range.is_strictly_above(
                        weaker.force_range) and \
                        weaker.velocity > stronger.velocity:
                    return weaker, stronger
        return None

    def check_monotonic(self) -> None:
        """Raise :class:`CalibrationConflict` if the table is not
        monotonic. Do nothing when every record can hold simultaneously."""
        conflict = self.find_conflict()
        if conflict is not None:
            raise CalibrationConflict(conflict[0], conflict[1])

    @property
    def is_monotonic(self) -> bool:
        """True when no pair of records violates monotonic encoding."""
        return self.find_conflict() is None


def compare_velocity(velocity_a: int, table_a: VelocityTable,
                     velocity_b: int, table_b: VelocityTable) -> int:
    """Compare the provable force behind two velocity reports.

    Each report is reduced to the force range provable from its own
    device table. A verdict is only returned when the two ranges are
    strictly separated:

    * ``1`` if device A was provably struck harder,
    * ``-1`` if device B was provably struck harder,
    * ``0`` when the ranges overlap or touch and neither verdict can be
      proven, including when both devices report the same velocity.
    """
    if not isinstance(table_a, VelocityTable) or \
            not isinstance(table_b, VelocityTable):
        raise TypeError('tables must be VelocityTable instances')
    range_a = table_a.force_range_for(velocity_a)
    range_b = table_b.force_range_for(velocity_b)
    if range_a.is_strictly_above(range_b):
        return 1
    if range_b.is_strictly_above(range_a):
        return -1
    return 0
