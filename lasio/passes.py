"""Split a depth index into measurement passes.

LAS files store samples in acquisition order.  A logging tool is usually run
into the hole and then pulled out again, so the depth index contains a down
pass followed by an up pass.  Sorting that data by depth would interleave the
two passes.  The functions in this module identify the passes without sorting
or removing any samples.

Small movements against the current direction are treated as wobble until the
tool has backed off further than ``min_reversal`` from the furthest point
reached in that direction.  Only then is a new pass started.  This also means
duplicate depths are preserved: they can be separate samples in one pass, or
readings from different passes.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

import numpy as np

DOWN = "down"
UP = "up"


def _as_las(value):
    """Return a LASFile if *value* is one, otherwise None."""
    if hasattr(value, "index") and hasattr(value, "curves") and hasattr(value, "data"):
        return value
    return None


def _resolve_reversal(
    min_reversal: Optional[float],
    reversal_threshold: Optional[float],
    min_reversal_distance: Optional[float],
    reversal: Optional[float],
    threshold: Optional[float],
    min_reversal_depth: Optional[float] = None,
    reversal_distance: Optional[float] = None,
) -> float:
    """Return the first explicitly supplied reversal distance."""
    for value in (
        min_reversal,
        reversal_threshold,
        min_reversal_distance,
        min_reversal_depth,
        reversal_distance,
        reversal,
        threshold,
    ):
        if value is not None:
            value = float(value)
            if not np.isfinite(value):
                raise ValueError("reversal distance must be finite")
            if value < 0:
                raise ValueError("reversal distance must not be negative")
            return value
    return 0.0


def _direction_for_step(step: float) -> str:
    return DOWN if step > 0 else UP


def _step_for_direction(direction: Optional[str]) -> Optional[int]:
    if direction == DOWN:
        return 1
    if direction == UP:
        return -1
    return None


def _extreme_in(depths: np.ndarray, start: int, end: int, direction: str):
    """Return (index, depth) of the furthest sample in *direction*.

    Duplicate depths resolve to the last sample, so repeated readings at the
    turnaround remain part of the current pass.  Non-finite depths are
    ignored unless there is no usable sample.
    """
    values = depths[start : end + 1]
    finite = np.isfinite(values)
    if not finite.any():
        return end, float(depths[end])
    candidates = np.flatnonzero(finite)
    if direction == DOWN:
        best = np.nanmax(values[finite])
    else:
        best = np.nanmin(values[finite])
    matches = candidates[values[finite] == best]
    index = start + int(matches[-1])
    return index, float(depths[index])


class _SegmentBase:
    """Common sequence and depth-range behaviour for pass segments."""

    def __init__(self, segmentation: "PassSegmentation", start_index: int, end_index: int):
        self._segmentation = segmentation
        self.start_index = int(start_index)
        self.end_index = int(end_index)

    @property
    def first_index(self) -> int:
        return self.start_index

    @property
    def last_index(self) -> int:
        return self.end_index

    @property
    def stop_index(self) -> int:
        return self.end_index

    @property
    def sample_indices(self) -> np.ndarray:
        return np.arange(self.start_index, self.end_index + 1, dtype=int)

    @property
    def indices(self) -> np.ndarray:
        return self.sample_indices

    @property
    def sample_positions(self) -> np.ndarray:
        return self.sample_indices

    @property
    def first_index(self) -> int:
        return self.start_index

    @property
    def last_index(self) -> int:
        return self.end_index

    @property
    def index_start(self) -> int:
        return self.start_index

    @property
    def index_end(self) -> int:
        return self.end_index

    @property
    def depth_start(self) -> float:
        return self.start_depth

    @property
    def depth_stop(self) -> float:
        return self.stop_depth

    @property
    def end(self) -> float:
        return self.end_depth

    @property
    def first_depth(self) -> float:
        return self.start_depth

    @property
    def last_depth(self) -> float:
        return self.end_depth

    @property
    def top(self) -> float:
        return self.max_depth

    @property
    def bottom(self) -> float:
        return self.min_depth

    @property
    def depths(self) -> np.ndarray:
        return self._segmentation.depth[self.start_index : self.end_index + 1]

    @property
    def depth(self) -> np.ndarray:
        return self.depths

    @property
    def start_depth(self) -> float:
        return float(self.depths[0])

    @property
    def end_depth(self) -> float:
        return float(self.depths[-1])

    @property
    def stop_depth(self) -> float:
        return self.end_depth

    @property
    def start(self) -> float:
        return self.start_depth

    @property
    def stop(self) -> float:
        return self.stop_depth

    @property
    def min_depth(self) -> float:
        return float(np.nanmin(self.depths))

    @property
    def max_depth(self) -> float:
        return float(np.nanmax(self.depths))

    @property
    def n_samples(self) -> int:
        return len(self)

    @property
    def sample_count(self) -> int:
        return len(self)

    @property
    def data(self) -> Optional[np.ndarray]:
        if self._segmentation.data is None:
            return None
        return self._segmentation.data[self.start_index : self.end_index + 1, :]

    def to_dict(self) -> Dict[str, object]:
        """Return a plain description of this segment."""
        return self.as_dict()

    @property
    def curves(self) -> Dict[str, np.ndarray]:
        values = self.data
        if values is None:
            return {}
        return {
            mnemonic: values[:, i].copy()
            for i, mnemonic in enumerate(self._segmentation.mnemonics)
        }

    def __len__(self) -> int:
        return self.end_index - self.start_index + 1

    def __contains__(self, item: object) -> bool:
        if isinstance(item, (int, np.integer)):
            return self.start_index <= int(item) <= self.end_index
        return False

    def __getitem__(self, key):
        if isinstance(key, str):
            values = self.curves
            if key in values:
                return values[key]
            metadata = self.as_dict()
            if key in metadata:
                return metadata[key]
            raise KeyError(key)
        values = self.depths
        return values[key]

    def as_dict(self) -> Dict[str, object]:
        """Return a plain description of this segment."""
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start": self.start_depth,
            "stop": self.stop_depth,
            "start_depth": self.start_depth,
            "stop_depth": self.stop_depth,
            "min_depth": self.min_depth,
            "max_depth": self.max_depth,
            "direction": getattr(self, "direction", None),
            "pass_index": getattr(self, "pass_index", None),
            "n_samples": len(self),
        }

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(start_index={self.start_index}, "
            f"end_index={self.end_index}, direction={self.direction!r}, "
            f"depths=({self.start_depth:g}, {self.end_depth:g}))"
        )


class Pass(_SegmentBase):
    """A contiguous run of samples acquired in one direction.

    ``start`` and ``stop`` are the first and last measured depths.  The sample
    positions in the original LAS file are available as ``start_index`` and
    ``end_index`` (inclusive).
    """

    def __init__(
        self,
        segmentation: "PassSegmentation",
        pass_index: int,
        start_index: int,
        end_index: int,
        direction: str,
    ):
        super().__init__(segmentation, start_index, end_index)
        self.pass_index = int(pass_index)
        self.direction = direction

    @property
    def pass_number(self) -> int:
        return self.pass_index + 1

    @property
    def number(self) -> int:
        return self.pass_number

    @property
    def index(self) -> int:
        return self.pass_index

    @property
    def step(self) -> int:
        return _step_for_direction(self.direction)

    @property
    def direction_code(self) -> int:
        return self.step

    @property
    def is_down(self) -> bool:
        return self.direction == DOWN

    @property
    def is_up(self) -> bool:
        return self.direction == UP

    @property
    def is_increasing(self) -> bool:
        return self.direction == DOWN

    @property
    def is_decreasing(self) -> bool:
        return self.direction == UP

    def as_las(self, source):
        """Return a copy of *source* containing only this pass's samples."""
        return self._segmentation._subset_las(source, self)

    def __repr__(self) -> str:
        return (
            f"Pass(pass_index={self.pass_index}, direction={self.direction!r}, "
            f"start_index={self.start_index}, end_index={self.end_index}, "
            f"start={self.start_depth:g}, stop={self.stop_depth:g})"
        )


class UnresolvedSegment(_SegmentBase):
    """A trailing reversal too small to establish a new direction.

    Such a segment is kept in acquisition order.  ``direction`` is ``None``
    because the data does not (yet) contain enough movement to distinguish a
    genuine new pass from wobble.
    """

    def __init__(
        self,
        segmentation: "PassSegmentation",
        start_index: int,
        end_index: int,
        previous_direction: Optional[str] = None,
        candidate_direction: Optional[str] = None,
        excursion: Optional[float] = None,
    ):
        super().__init__(segmentation, start_index, end_index)
        self.previous_direction = previous_direction
        self.candidate_direction = candidate_direction
        self.direction = None
        self.excursion = excursion

    @property
    def step(self) -> None:
        return None

    @property
    def is_unresolved(self) -> bool:
        return True

    @property
    def start(self) -> float:
        return self.start_depth

    @property
    def stop(self) -> float:
        return self.stop_depth

    def as_las(self, source):
        """Return a copy of *source* containing only this segment's samples."""
        return self._segmentation._subset_las(source, self)

    def __repr__(self) -> str:
        return (
            f"UnresolvedSegment(start_index={self.start_index}, "
            f"end_index={self.end_index}, "
            f"depths=({self.start_depth:g}, {self.end_depth:g}), "
            f"candidate_direction={self.candidate_direction!r})"
        )


class PassSegmentation:
    """Result of splitting a depth index into measurement passes.

    Attributes:
        depth: the original index, unchanged.
        min_reversal: the configured reversal distance.
        passes: tuple of :class:`Pass` objects.
        unresolved: tuple of trailing :class:`UnresolvedSegment` objects.
        segments: passes and unresolved segments in acquisition order.
    """

    def __init__(
        self,
        depth: Sequence[float],
        passes: Iterable[Pass],
        unresolved: Iterable[UnresolvedSegment],
        min_reversal: float,
        data: Optional[Sequence[Sequence[float]]] = None,
        mnemonics: Optional[Sequence[str]] = None,
    ):
        self.depth = np.asarray(depth, dtype=float)
        self.min_reversal = float(min_reversal)
        self.passes = tuple(passes)
        self.unresolved = tuple(unresolved)
        self.segments = tuple(
            sorted(
                list(self.passes) + list(self.unresolved),
                key=lambda segment: segment.start_index,
            )
        )
        if data is None:
            self.data = None
        else:
            self.data = np.asarray(data)
        if mnemonics is None:
            if self.data is not None:
                mnemonics = ["C{}".format(i) for i in range(self.data.shape[1])]
            else:
                mnemonics = []
        self.mnemonics = list(mnemonics)

        self._pass_for_sample = np.full(self.depth.shape[0], -1, dtype=int)
        for segment in self.passes:
            self._pass_for_sample[segment.start_index : segment.end_index + 1] = (
                segment.pass_index
            )

    @property
    def directions(self) -> List[str]:
        return [segment.direction for segment in self.passes]

    @property
    def pass_ranges(self) -> List[Tuple[float, float]]:
        """Start and stop depth of each pass, in acquisition order."""
        return [(segment.start_depth, segment.end_depth) for segment in self.passes]

    @property
    def intervals(self) -> List[Tuple[float, float]]:
        """Alias for :attr:`PassSegmentation.pass_ranges`."""
        return self.pass_ranges

    def pass_intervals(self) -> List[Dict[str, object]]:
        """Return start/stop metadata for each pass."""
        return [
            {
                "pass_index": segment.pass_index,
                "pass_number": segment.pass_number,
                "direction": segment.direction,
                "start": segment.start_depth,
                "stop": segment.end_depth,
                "start_index": segment.start_index,
                "end_index": segment.end_index,
            }
            for segment in self.passes
        ]

    def summary(self) -> List[Dict[str, object]]:
        """Return one dict per pass and unresolved segment."""
        rows = []
        for segment in self.segments:
            row = segment.as_dict()
            row["unresolved"] = isinstance(segment, UnresolvedSegment)
            if row["unresolved"]:
                row["previous_direction"] = segment.previous_direction
                row["candidate_direction"] = segment.candidate_direction
            rows.append(row)
        return rows

    def __call__(
        self,
        min_reversal: Optional[float] = None,
        **kwargs,
    ) -> "PassSegmentation":
        """Re-run detection with a different reversal distance.

        This makes ``las.passes`` usable both as a result and as a callable.
        """
        if min_reversal is None and not kwargs:
            return self
        if min_reversal is None:
            min_reversal = self.min_reversal
        return detect_passes(
            self.depth,
            min_reversal,
            data=self.data,
            mnemonics=self.mnemonics,
            **kwargs,
        )

    def as_dicts(self) -> List[Dict[str, object]]:
        """Alias for :meth:`PassSegmentation.summary`."""
        return self.summary()

    @property
    def n_passes(self) -> int:
        return len(self.passes)

    def __len__(self) -> int:
        return len(self.passes)

    def __iter__(self) -> Iterator[Pass]:
        return iter(self.passes)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self.passes[index]
        return self.passes[index]

    def pass_for_sample(self, sample_index: int) -> Optional[Pass]:
        """Return the pass containing a sample position, or None if unresolved."""
        sample_index = int(sample_index)
        if sample_index < 0:
            sample_index += self.depth.shape[0]
        if not 0 <= sample_index < self.depth.shape[0]:
            raise IndexError("sample index out of range")
        pass_index = self._pass_for_sample[sample_index]
        if pass_index < 0:
            return None
        return self.passes[pass_index]

    def segment_for_sample(self, sample_index: int):
        """Return the pass or unresolved segment containing a sample position."""
        sample_index = int(sample_index)
        if sample_index < 0:
            sample_index += self.depth.shape[0]
        for segment in self.segments:
            if segment.start_index <= sample_index <= segment.end_index:
                return segment
        raise IndexError("sample index out of range")

    def split(self, source=None, include_unresolved: bool = False):
        """Return one LASFile per pass.

        If *source* is omitted, a LASFile is rebuilt from the depth and curve
        data supplied to :func:`detect_passes` (if any).  Without curve data
        the return value is a list of depth arrays.  Supplying an existing
        LASFile keeps its header and other metadata.
        """
        segments = list(self.passes)
        if include_unresolved:
            segments.extend(self.unresolved)
        segments.sort(key=lambda segment: segment.start_index)
        if source is None:
            source = self._source_las()
        if source is None:
            return [
                self.depth[segment.start_index : segment.end_index + 1].copy()
                for segment in segments
            ]
        if _as_las(source) is None:
            # An array was supplied as the source; use its rows as curve data
            # and rebuild a LASFile around this segmentation's index.
            values = np.asarray(source)
            if values.shape[0] != self.depth.shape[0]:
                raise ValueError("source and depth must have the same number of rows")
            if self.data is None:
                self.data = values
            source = self._source_las()
            if source is None:
                return [
                    self.depth[segment.start_index : segment.end_index + 1].copy()
                    for segment in segments
                ]
        return [self._subset_las(source, segment) for segment in segments]

    def _source_las(self):
        if self.data is None or not self.depth.size:
            return None
        from .las import LASFile

        las = LASFile()
        las.append_curve("DEPT", self.depth.copy())
        for i in range(self.data.shape[1]):
            mnemonic = self.mnemonics[i] if i < len(self.mnemonics) else "C{}".format(i)
            las.append_curve(mnemonic, self.data[:, i].copy())
        return las

    def _subset_las(self, source, segment):
        """Copy *source* and retain only the rows covered by *segment*."""
        result = copy.deepcopy(source)
        result.set_data(
            result.data[segment.start_index : segment.end_index + 1, :].copy()
        )
        result.update_start_stop_step()
        result.pass_index = getattr(segment, "pass_index", None)
        result.direction = getattr(segment, "direction", None)
        result.pass_segment = segment
        result.index_initial = result.index.copy()
        return result

    def __repr__(self) -> str:
        return (
            f"PassSegmentation(min_reversal={self.min_reversal:g}, "
            f"passes={len(self.passes)}, unresolved={len(self.unresolved)})"
        )


@dataclass(frozen=True)
class Reading:
    """One sample returned by :func:`depth_history`."""

    depth: float
    sample_index: int
    pass_index: Optional[int]
    direction: Optional[str]
    curves: Mapping[str, object]
    unresolved: bool = False
    previous_direction: Optional[str] = None
    candidate_direction: Optional[str] = None

    @property
    def index(self) -> int:
        return self.sample_index

    @property
    def pass_number(self) -> Optional[int]:
        if self.pass_index is None:
            return None
        return self.pass_index + 1

    def __getitem__(self, key):
        if key in self.curves:
            return self.curves[key]
        raise KeyError(key)


class _ReadingGroup:
    """Common behaviour for readings grouped by pass or unresolved segment."""

    def __init__(self, segment, readings: Sequence[Reading], mnemonics: Sequence[str]):
        self.segment = segment
        self.readings = tuple(readings)
        self._mnemonics = list(mnemonics)

    @property
    def direction(self):
        return self.segment.direction

    @property
    def sample_indices(self) -> np.ndarray:
        return np.array([reading.sample_index for reading in self.readings], dtype=int)

    @property
    def depths(self) -> np.ndarray:
        return np.array([reading.depth for reading in self.readings], dtype=float)

    @property
    def curves(self) -> Dict[str, np.ndarray]:
        return {
            mnemonic: np.array(
                [reading.curves.get(mnemonic) for reading in self.readings]
            )
            for mnemonic in self._mnemonics
        }

    @property
    def values(self) -> Dict[str, np.ndarray]:
        return self.curves

    def curve(self, mnemonic: str) -> np.ndarray:
        if mnemonic not in self._mnemonics:
            raise KeyError(mnemonic)
        return self.curves[mnemonic]

    def __len__(self) -> int:
        return len(self.readings)

    def __iter__(self) -> Iterator[Reading]:
        return iter(self.readings)

    def __getitem__(self, index):
        return self.readings[index]

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(direction={self.direction!r}, "
            f"readings={len(self.readings)}, "
            f"depths={self.depths.tolist()})"
        )


class PassReadings(_ReadingGroup):
    """All readings at a queried depth that belong to one pass."""

    @property
    def pass_index(self) -> int:
        return self.segment.pass_index

    @property
    def pass_number(self) -> int:
        return self.segment.pass_number


class UnresolvedReadings(_ReadingGroup):
    """Readings at a queried depth from an unresolved wobble segment."""

    @property
    def is_unresolved(self) -> bool:
        return True

    @property
    def previous_direction(self):
        return self.segment.previous_direction

    @property
    def candidate_direction(self):
        return self.segment.candidate_direction


class DepthHistory:
    """Result of :func:`depth_history`.

    ``passes`` contains one :class:`PassReadings` entry per measurement pass
    that crossed the requested depth.  ``unresolved`` contains readings from a
    trailing reversal whose direction could not be established.  ``segments``
    and ``readings`` are in original acquisition order.
    """

    def __init__(
        self,
        depth: float,
        tolerance: Optional[float],
        passes: Sequence["PassReadings"],
        unresolved: Sequence["UnresolvedReadings"],
        readings: Sequence["Reading"],
        mnemonics: Sequence[str],
    ) -> None:
        self.depth = float(depth)
        self.tolerance = tolerance
        self.passes = tuple(passes)
        self.unresolved = tuple(unresolved)
        self.segments = tuple(
            sorted(
                list(self.passes) + list(self.unresolved),
                key=lambda group: group.readings[0].sample_index,
            )
        )
        self.readings = tuple(readings)
        self._mnemonics = list(mnemonics)

    @property
    def n_passes(self) -> int:
        return len(self.passes)

    @property
    def directions(self) -> List[str]:
        return [group.direction for group in self.passes]

    def summary(self) -> List[Dict[str, object]]:
        """Return one dict per pass and unresolved segment at this depth."""
        rows = []
        for group in self.segments:
            rows.append(
                {
                    "depth": self.depth,
                    "direction": group.direction,
                    "unresolved": isinstance(group, UnresolvedReadings),
                    "pass_index": getattr(group, "pass_index", None),
                    "sample_indices": group.sample_indices.tolist(),
                    "depths": group.depths.tolist(),
                    "values": {
                        name: values.tolist() for name, values in group.curves.items()
                    },
                    "previous_direction": getattr(group, "previous_direction", None),
                    "candidate_direction": getattr(group, "candidate_direction", None),
                }
            )
        return rows

    def as_dicts(self) -> List[Dict[str, object]]:
        """Alias for :meth:`DepthHistory.summary`."""
        return self.summary()

    @property
    def ambiguous(self) -> bool:
        """True when the depth also has unresolved or multiple-pass readings."""
        return bool(self.unresolved) or len(self.passes) > 1

    def values(self, mnemonic: Optional[str] = None):
        """Return all matched readings as arrays.

        With a mnemonic, return that curve's values in acquisition order.
        Without one, return a dict of all curves.
        """
        if mnemonic is not None:
            if mnemonic not in self._mnemonics:
                raise KeyError(mnemonic)
            return np.array(
                [reading.curves.get(mnemonic) for reading in self.readings]
            )
        return {
            name: np.array([reading.curves.get(name) for reading in self.readings])
            for name in self._mnemonics
        }

    def __len__(self) -> int:
        return len(self.readings)

    def __iter__(self) -> Iterator[Reading]:
        return iter(self.readings)

    def __getitem__(self, index):
        return self.readings[index]

    def __bool__(self) -> bool:
        return bool(self.readings)

    def __repr__(self) -> str:
        return (
            f"DepthHistory(depth={self.depth:g}, passes={len(self.passes)}, "
            f"unresolved={len(self.unresolved)}, readings={len(self.readings)})"
        )


def detect_passes(
    depth: Sequence[float],
    min_reversal: Optional[float] = None,
    *,
    reversal_threshold: Optional[float] = None,
    min_reversal_distance: Optional[float] = None,
    min_reversal_depth: Optional[float] = None,
    reversal_distance: Optional[float] = None,
    reversal: Optional[float] = None,
    threshold: Optional[float] = None,
    data: Optional[Sequence[Sequence[float]]] = None,
    mnemonics: Optional[Sequence[str]] = None,
) -> PassSegmentation:
    """Detect measurement passes in a depth index.

    Arguments:
        depth (array-like): depth (or other index) values in acquisition order.
        min_reversal (float): distance the index must retreat from the furthest
            point reached in the current direction before a reversal is
            accepted.  Movements smaller than this are kept in the current
            pass in their original sampling order.  The default is 0, which
            starts a new pass at the first movement against the current
            direction.

    Keyword Arguments:
        reversal_threshold, min_reversal_distance, reversal, threshold (float):
            aliases for ``min_reversal``.
        data (array-like, optional): curve values with one row per depth.  When
            supplied, passes expose the corresponding ``data`` and ``curves``.
        mnemonics (sequence of str, optional): curve names for ``data``.

    Returns:
        :class:`PassSegmentation`
    """
    las = _as_las(depth)
    if las is not None:
        if data is None:
            data = las.data[:, 1:]
        if mnemonics is None:
            mnemonics = las.keys()[1:]
        depth = las.index
    depths = np.asarray(depth, dtype=float)
    if depths.ndim != 1:
        raise ValueError("depth must be one-dimensional")
    distance = _resolve_reversal(
        min_reversal,
        reversal_threshold,
        min_reversal_distance,
        reversal,
        threshold,
        min_reversal_depth=min_reversal_depth,
        reversal_distance=reversal_distance,
    )
    if data is not None:
        data = np.asarray(data)
        if data.ndim != 2:
            raise ValueError("data must be two-dimensional")
        if data.shape[0] != depths.shape[0]:
            raise ValueError("data and depth must have the same number of rows")
        if mnemonics is not None and len(mnemonics) != data.shape[1]:
            raise ValueError("mnemonics must match the number of data columns")

    n = depths.shape[0]
    if n == 0:
        return PassSegmentation(depths, [], [], distance, data, mnemonics)

    passes: List[Pass] = []
    unresolved: List[UnresolvedSegment] = []

    # Find the first movement between finite samples to establish an initial
    # direction.  Duplicate samples before that movement stay at the start of
    # the same pass.
    first_move = None
    previous_finite = None
    for i in range(n):
        if not np.isfinite(depths[i]):
            continue
        if previous_finite is not None and depths[i] != depths[previous_finite]:
            first_move = i
            break
        previous_finite = i

    if first_move is None:
        unresolved.append(
            UnresolvedSegment(
                None,  # replaced below
                0,
                n - 1,
                previous_direction=None,
                candidate_direction=None,
                excursion=0.0,
            )
        )
        result = PassSegmentation(depths, passes, unresolved, distance, data, mnemonics)
        unresolved[0]._segmentation = result
        return result

    direction = _direction_for_step(depths[first_move] - depths[previous_finite])
    pass_start = 0
    trend_extreme_index = first_move
    trend_extreme = float(depths[first_move])
    reversal_extreme = None

    for i in range(first_move + 1, n):
        depth_i = float(depths[i])
        if not np.isfinite(depth_i):
            continue
        step = _step_for_direction(direction)
        along = (depth_i - trend_extreme) * step
        if along >= 0:
            # Equal depths are additional samples in the current pass, not a
            # duplicate measurement from another pass.  Keeping the later
            # sample as the extreme keeps repeated readings at a turnaround
            # with the pass they were acquired in.
            trend_extreme = depth_i
            trend_extreme_index = i
            reversal_extreme = None
            continue

        # The tool is moving against the established direction.  Remember the
        # furthest such point; the pass only reverses if the retreat from the
        # trend extreme becomes larger than the configured distance.
        if reversal_extreme is None:
            reversal_extreme = depth_i
        elif direction == DOWN:
            # Downwards means increasing index; a retreat is decreasing depth,
            # so the furthest retreat is the minimum.
            reversal_extreme = min(reversal_extreme, depth_i)
        else:
            reversal_extreme = max(reversal_extreme, depth_i)

        retreat = abs(reversal_extreme - trend_extreme)
        if retreat <= distance:
            continue

        # Confirmed reversal.  The old pass ends at its furthest sample and
        # the new pass begins at the turnaround, including any samples that
        # first looked like wobble.  All samples remain in acquisition order.
        pass_index = len(passes)
        passes.append(
            Pass(None, pass_index, pass_start, trend_extreme_index, direction)
        )
        direction = UP if direction == DOWN else DOWN
        pass_start = trend_extreme_index + 1
        trend_extreme_index, trend_extreme = _extreme_in(
            depths, pass_start, i, direction
        )
        reversal_extreme = None

    trailing_start = trend_extreme_index + 1
    if trailing_start < n:
        pass_index = len(passes)
        passes.append(
            Pass(None, pass_index, pass_start, trend_extreme_index, direction)
        )
        # Everything after the furthest point is a retreat smaller than the
        # configured distance.  Direction cannot be determined yet.
        unresolved.append(
            UnresolvedSegment(
                None,  # replaced below
                trailing_start,
                n - 1,
                previous_direction=direction,
                candidate_direction=UP if direction == DOWN else DOWN,
                excursion=float(
                    abs(float(depths[-1]) - trend_extreme)
                    if np.isfinite(depths[-1])
                    else 0.0
                ),
            )
        )
    else:
        pass_index = len(passes)
        passes.append(Pass(None, pass_index, pass_start, n - 1, direction))

    result = PassSegmentation(depths, passes, unresolved, distance, data, mnemonics)
    for segment in result.segments:
        segment._segmentation = result
    return result


def _depth_mask(depths: np.ndarray, query_depth: float, tolerance) -> np.ndarray:
    if tolerance is None:
        difference = np.abs(depths - query_depth)
        finite = np.isfinite(difference)
        if not finite.any():
            return np.zeros(depths.shape, dtype=bool)
        minimum = np.nanmin(difference[finite])
        # Treat differences that are equal to floating-point precision as a
        # tie, so two samples either side of a query are both returned.
        allowance = max(1e-12, abs(minimum) * 1e-9)
        return finite & (difference <= minimum + allowance)
    tolerance = float(tolerance)
    if tolerance < 0:
        raise ValueError("tolerance must not be negative")
    # Data read from LAS text is decimal, so allow a tiny floating-point
    # rounding allowance at the edge of the requested window.
    allowance = max(1e-12, abs(tolerance) * 1e-9, abs(query_depth) * 1e-12)
    return np.isfinite(depths) & (np.abs(depths - query_depth) <= tolerance + allowance)


def depth_history(
    depth: Sequence[float],
    query_depth: float,
    tolerance: Optional[float] = 0.0,
    min_reversal: Optional[float] = None,
    *,
    traversal: Optional[PassSegmentation] = None,
    passes: Optional[PassSegmentation] = None,
    data: Optional[Sequence[Sequence[float]]] = None,
    mnemonics: Optional[Sequence[str]] = None,
    reversal_threshold: Optional[float] = None,
    min_reversal_distance: Optional[float] = None,
    min_reversal_depth: Optional[float] = None,
    reversal_distance: Optional[float] = None,
    reversal: Optional[float] = None,
    threshold: Optional[float] = None,
    atol: Optional[float] = None,
) -> DepthHistory:
    """Return all readings at *query_depth*, separated by measurement pass.

    A depth may occur more than once without being a duplicate measurement:
    the down and up passes can both cross it, and a small wobble can revisit
    it within one pass.  Each pass is reported separately in
    ``history.passes``; a trailing unresolved retreat is reported separately
    in ``history.unresolved``.  No samples are removed.

    Arguments:
        depth (array-like): depth values in acquisition order.
        query_depth (float): depth to look up.
        tolerance (float or None): absolute depth tolerance.  ``0`` requires an
            exact match.  ``None`` returns the nearest-depth sample(s).  The
            ``atol`` keyword is an alias.

    Other keyword arguments are as for :func:`detect_passes`.  A pre-computed
    segmentation may be supplied as ``traversal`` or ``passes``.
    """
    las = _as_las(depth)
    if las is not None:
        if data is None:
            data = las.data[:, 1:]
        if mnemonics is None:
            mnemonics = las.keys()[1:]
        depth = las.index
    depths = np.asarray(depth, dtype=float)
    segmentation = traversal if traversal is not None else passes
    if segmentation is None:
        segmentation = detect_passes(
            depths,
            min_reversal,
            reversal_threshold=reversal_threshold,
            min_reversal_distance=min_reversal_distance,
            min_reversal_depth=min_reversal_depth,
            reversal_distance=reversal_distance,
            reversal=reversal,
            threshold=threshold,
            data=data,
            mnemonics=mnemonics,
        )
    elif data is not None and segmentation.data is None:
        segmentation = detect_passes(
            depths,
            segmentation.min_reversal,
            data=data,
            mnemonics=mnemonics,
        )

    if atol is not None:
        if tolerance not in (None, 0.0) and tolerance != atol:
            raise ValueError("tolerance and atol disagree")
        tolerance = atol

    query_depth = float(query_depth)
    mask = _depth_mask(segmentation.depth, query_depth, tolerance)
    selected = np.flatnonzero(mask)

    if data is None:
        data = segmentation.data
    if mnemonics is None:
        mnemonics = segmentation.mnemonics
    else:
        mnemonics = list(mnemonics)

    def make_reading(sample_index: int) -> Reading:
        segment = segmentation.segment_for_sample(sample_index)
        values: Dict[str, object] = {}
        if data is not None:
            row = np.asarray(data)[sample_index, :]
            for i, name in enumerate(mnemonics):
                values[name] = row[i].item() if hasattr(row[i], "item") else row[i]
        if isinstance(segment, Pass):
            return Reading(
                depth=float(segmentation.depth[sample_index]),
                sample_index=int(sample_index),
                pass_index=segment.pass_index,
                direction=segment.direction,
                curves=values,
            )
        return Reading(
            depth=float(segmentation.depth[sample_index]),
            sample_index=int(sample_index),
            pass_index=None,
            direction=None,
            curves=values,
            unresolved=True,
            previous_direction=segment.previous_direction,
            candidate_direction=segment.candidate_direction,
        )

    readings_by_pass: Dict[int, List[Reading]] = {}
    readings_by_unresolved: Dict[int, List[Reading]] = {}
    all_readings: List[Reading] = []
    for sample_index in selected:
        segment = segmentation.segment_for_sample(int(sample_index))
        reading = make_reading(int(sample_index))
        all_readings.append(reading)
        if isinstance(segment, Pass):
            readings_by_pass.setdefault(segment.pass_index, []).append(reading)
        else:
            readings_by_unresolved.setdefault(segment.start_index, []).append(reading)

    pass_groups = [
        PassReadings(segmentation.passes[pass_index], readings, mnemonics)
        for pass_index, readings in sorted(readings_by_pass.items())
    ]
    unresolved_groups = []
    for start_index, readings in sorted(readings_by_unresolved.items()):
        segment = next(
            item for item in segmentation.unresolved if item.start_index == start_index
        )
        unresolved_groups.append(UnresolvedReadings(segment, readings, mnemonics))

    return DepthHistory(
        query_depth,
        tolerance,
        pass_groups,
        unresolved_groups,
        all_readings,
        mnemonics,
    )


# Readable aliases for the same operations.
find_passes = detect_passes
split_passes = detect_passes
history_at_depth = depth_history
get_depth_history = depth_history
