"""Merge a re-logged (supplement) LAS into a base LAS with explicit coverage.

The upstream crew re-logs a depth interval and delivers a supplement LAS that
contains only a handful of curves.  This module merges such a supplement into
the original (base) survey according to an explicit per-curve coverage
manifest, and keeps track of *why* every merged value looks the way it does.

Coverage actions (the manifest values)
--------------------------------------
``"relog"``
    The curve was re-logged; supplement values supersede the base values
    inside the re-log interval.
``"not-acquired"``
    The curve was not part of the re-log; the base values stand.
``"withdrawn"``
    The curve has been officially withdrawn for the interval; any previous
    values are retracted (reported as null).

Provenance labels (reported by queries)
---------------------------------------
``"base"``
    Depth is outside every re-log interval; value comes from the base survey.
``"relog"``
    Value comes from the supplement (valid re-log measurement).
``"measured-empty"``
    The curve was re-logged but the delivered file contains only nulls for
    the whole interval (实测为空).  Value is null.
``"not-acquired"``
    The curve was not acquired by the re-log (未采); the base value stands.
``"withdrawn"``
    The curve was officially withdrawn for the interval (正式撤回).
    Value is null.
``"pending"``
    The re-log curve has an internal gap (acquisition paused temporarily).
    The interval is undecided (未决); the base value is kept until the gap
    is resolved.

Boundary rule
-------------
Only samples whose depth falls inside ``[start, stop]`` (inclusive) are
affected by a re-log layer.  If the re-log boundary falls between two base
samples, the samples outside the interval keep their base value and
provenance -- the rest of the curve is never blanked out.

Idempotency
-----------
Ingesting the same supplement (same interval, manifest and curve data) more
than once is a no-op: values, provenance and pending intervals are unchanged.
"""

import copy
import logging
from collections import namedtuple

import numpy as np

logger = logging.getLogger(__name__)

__all__ = [
    "Coverage",
    "Provenance",
    "QueryResult",
    "RelogLayer",
    "RelogMerge",
    "merge_relog",
]


class Coverage(object):
    """Coverage actions allowed in a merge manifest."""

    RELOG = "relog"
    NOT_ACQUIRED = "not-acquired"
    WITHDRAWN = "withdrawn"


class Provenance(object):
    """Provenance labels attached to merged values."""

    BASE = "base"
    RELOG = "relog"
    MEASURED_EMPTY = "measured-empty"
    NOT_ACQUIRED = "not-acquired"
    WITHDRAWN = "withdrawn"
    PENDING = "pending"


_COVERAGE_ALIASES = {
    Coverage.RELOG: Coverage.RELOG,
    "replace": Coverage.RELOG,
    "supersede": Coverage.RELOG,
    Coverage.NOT_ACQUIRED: Coverage.NOT_ACQUIRED,
    "not_acquired": Coverage.NOT_ACQUIRED,
    "keep": Coverage.NOT_ACQUIRED,
    "skip": Coverage.NOT_ACQUIRED,
    Coverage.WITHDRAWN: Coverage.WITHDRAWN,
    "retract": Coverage.WITHDRAWN,
    "retracted": Coverage.WITHDRAWN,
}

#: Result of querying the merge at one curve/depth: value, provenance, source.
QueryResult = namedtuple("QueryResult", ["value", "provenance", "source"])


def _null_mask(data):
    """Boolean mask of null (NaN) samples; non-numeric data is never null."""
    arr = np.asarray(data)
    if arr.dtype.kind in "fc":
        return np.isnan(arr)
    return np.zeros(arr.shape, dtype=bool)


def _normalize_action(action):
    try:
        return _COVERAGE_ALIASES[str(action).strip().lower()]
    except KeyError:
        raise ValueError(
            "unknown coverage action %r (expected one of %s)"
            % (action, sorted(_COVERAGE_ALIASES))
        )


def _header_float(las, key):
    try:
        return float(las.well[key].value)
    except (KeyError, TypeError, ValueError):
        return None


class RelogLayer(object):
    """One ingested supplement: a re-log interval plus a coverage manifest.

    Arguments:
        supplement (:class:`lasio.LASFile`): the re-logged LAS file.
        interval (tuple): ``(start, stop)`` depths covered by the re-log.
            Defaults to the supplement's STRT/STOP well header values.
        manifest (dict): mapping of curve mnemonic to coverage action
            (``"relog"``, ``"not-acquired"`` or ``"withdrawn"``).  Curves
            present in the supplement default to ``"relog"``; every other
            curve defaults to ``"not-acquired"``.
    """

    def __init__(self, supplement, interval=None, manifest=None):
        self.supplement = supplement
        if interval is None:
            start = _header_float(supplement, "STRT")
            stop = _header_float(supplement, "STOP")
            if start is None or stop is None:
                raise ValueError(
                    "interval must be given explicitly: supplement has no "
                    "usable STRT/STOP well header values"
                )
            interval = (start, stop)
        start, stop = float(interval[0]), float(interval[1])
        self.interval = (min(start, stop), max(start, stop))
        self.manifest = {}
        for mnemonic, action in (manifest or {}).items():
            self.manifest[mnemonic] = _normalize_action(action)

    @property
    def start(self):
        return self.interval[0]

    @property
    def stop(self):
        return self.interval[1]

    def covers_depth(self, depth):
        """True if *depth* lies inside the re-log interval (inclusive)."""
        return self.start <= depth <= self.stop

    def coverage(self, mnemonic):
        """Coverage action for *mnemonic* (defaults applied)."""
        if mnemonic in self.manifest:
            return self.manifest[mnemonic]
        if mnemonic in self.supplement.curvesdict:
            return Coverage.RELOG
        return Coverage.NOT_ACQUIRED

    def _supplement_curve(self, mnemonic):
        if mnemonic in self.supplement.curvesdict:
            return self.supplement.curvesdict[mnemonic]
        return None

    def _interval_samples(self, mnemonic):
        """(depths, values, null mask) of supplement samples in-interval."""
        curve = self._supplement_curve(mnemonic)
        if curve is None:
            return None
        depths = np.asarray(self.supplement.index, dtype=float)
        values = np.asarray(curve.data)
        mask = (depths >= self.start) & (depths <= self.stop)
        return depths[mask], values[mask], _null_mask(values)[mask]

    def is_measured_empty(self, mnemonic):
        """True if the re-log delivered only nulls for this curve."""
        samples = self._interval_samples(mnemonic)
        if samples is None:
            return False
        _, _, nulls = samples
        return len(nulls) > 0 and bool(nulls.all())

    def pending_intervals(self, mnemonic):
        """Undecided sub-intervals (temporary acquisition stops).

        A contiguous run of null supplement samples inside the re-log
        interval is *pending* when valid re-log data exists elsewhere in
        the interval.  The run is extended to the midpoints towards the
        neighbouring valid samples (or to the interval boundary at the
        edges), so that any depth whose nearest supplement sample is null
        resolves to ``pending``.
        """
        if self.coverage(mnemonic) != Coverage.RELOG:
            return []
        samples = self._interval_samples(mnemonic)
        if samples is None:
            return []
        depths, _, nulls = samples
        if len(depths) == 0 or nulls.all() or not nulls.any():
            return []
        intervals = []
        idx = 0
        n = len(depths)
        while idx < n:
            if not nulls[idx]:
                idx += 1
                continue
            end = idx
            while end + 1 < n and nulls[end + 1]:
                end += 1
            left = self.start if idx == 0 else (depths[idx - 1] + depths[idx]) / 2.0
            right = self.stop if end == n - 1 else (depths[end] + depths[end + 1]) / 2.0
            intervals.append((float(left), float(right)))
            idx = end + 1
        return intervals

    def is_pending(self, mnemonic, depth):
        return any(
            start <= depth <= stop
            for start, stop in self.pending_intervals(mnemonic)
        )

    def supplement_value(self, mnemonic, depth):
        """Nearest valid (non-null) in-interval supplement value at *depth*."""
        samples = self._interval_samples(mnemonic)
        if samples is None:
            return np.nan
        depths, values, nulls = samples
        valid = ~nulls
        if not valid.any():
            return np.nan
        pos = int(np.argmin(np.abs(depths[valid] - depth)))
        return values[valid][pos]

    def same_as(self, other):
        """True if *other* is an identical supplement delivery."""
        if not isinstance(other, RelogLayer):
            return False
        if self.interval != other.interval or self.manifest != other.manifest:
            return False
        mine = self.supplement.curvesdict
        theirs = other.supplement.curvesdict
        if set(mine) != set(theirs):
            return False
        for mnemonic in mine:
            a = np.asarray(mine[mnemonic].data)
            b = np.asarray(theirs[mnemonic].data)
            if a.shape != b.shape or not np.array_equal(a, b, equal_nan=True):
                return False
        return np.array_equal(
            np.asarray(self.supplement.index, dtype=float),
            np.asarray(other.supplement.index, dtype=float),
            equal_nan=True,
        )


class RelogMerge(object):
    """A base LAS plus an ordered stack of ingested re-log layers.

    Later layers take precedence over earlier ones inside their interval.
    Re-ingesting an identical supplement is a no-op, so values, provenance
    and pending intervals are stable under repeated delivery.
    """

    def __init__(self, base):
        self.base = base
        self.layers = []

    def ingest(self, supplement, interval=None, manifest=None):
        """Ingest a supplement LAS; returns the (possibly existing) layer.

        Ingesting the same supplement again (identical interval, manifest
        and curve data) does not change the merge.
        """
        layer = RelogLayer(supplement, interval=interval, manifest=manifest)
        if self.layers and self.layers[-1].same_as(layer):
            logger.info("re-ingest of identical supplement ignored (no-op)")
            return self.layers[-1]
        self.layers.append(layer)
        return layer

    # ------------------------------------------------------------------
    # queries

    def _base_value(self, mnemonic, depth):
        if mnemonic not in self.base.curvesdict:
            return np.nan
        depths = np.asarray(self.base.index, dtype=float)
        if len(depths) == 0:
            return np.nan
        pos = int(np.argmin(np.abs(depths - depth)))
        return self.base.curvesdict[mnemonic].data[pos]

    def _query(self, mnemonic, depth, layers):
        """Resolve (value, provenance, source) through *layers* then base."""
        saw_not_acquired = False
        for layer_index in range(len(layers) - 1, -1, -1):
            layer = layers[layer_index]
            if not layer.covers_depth(depth):
                continue
            action = layer.coverage(mnemonic)
            if action == Coverage.NOT_ACQUIRED:
                saw_not_acquired = True
                continue
            if action == Coverage.WITHDRAWN:
                return QueryResult(np.nan, Provenance.WITHDRAWN, None)
            # action == "relog"
            if layer._supplement_curve(mnemonic) is None:
                saw_not_acquired = True
                continue
            if layer.is_measured_empty(mnemonic):
                return QueryResult(np.nan, Provenance.MEASURED_EMPTY, None)
            if layer.is_pending(mnemonic, depth):
                older = self._query(mnemonic, depth, layers[:layer_index])
                return QueryResult(older.value, Provenance.PENDING, older.source)
            return QueryResult(
                layer.supplement_value(mnemonic, depth),
                Provenance.RELOG,
                "supplement",
            )
        provenance = Provenance.NOT_ACQUIRED if saw_not_acquired else Provenance.BASE
        return QueryResult(self._base_value(mnemonic, depth), provenance, "base")

    def query(self, mnemonic, depth):
        """Query the merge by curve mnemonic and depth.

        Returns a :class:`QueryResult` with the merged ``value``, its
        ``provenance`` label and the ``source`` the value was taken from
        (``"base"``, ``"supplement"`` or ``None`` for null results).
        """
        return self._query(mnemonic, float(depth), self.layers)

    def provenance(self, mnemonic, depth):
        """Provenance label for *mnemonic* at *depth*."""
        return self.query(mnemonic, depth).provenance

    def pending_intervals(self, mnemonic):
        """Undecided (pending) depth intervals for *mnemonic*.

        These come from the newest layer that re-logs the curve; older
        layers underneath are shadowed there.
        """
        for layer in reversed(self.layers):
            if layer.coverage(mnemonic) == Coverage.RELOG:
                return layer.pending_intervals(mnemonic)
        return []

    # ------------------------------------------------------------------
    # materialisation

    def depth_grid(self):
        """Sorted union of base depths and in-interval supplement depths."""
        grids = [np.asarray(self.base.index, dtype=float)]
        for layer in self.layers:
            depths = np.asarray(layer.supplement.index, dtype=float)
            mask = (depths >= layer.start) & (depths <= layer.stop)
            grids.append(depths[mask])
        grid = np.concatenate(grids)
        grid = grid[~np.isnan(grid)]
        return np.unique(grid)

    def curve_mnemonics(self):
        """All curve mnemonics known to the merge (base then supplements)."""
        mnemonics = [c.mnemonic for c in self.base.curves[1:]]
        for layer in self.layers:
            for curve in layer.supplement.curves[1:]:
                if curve.mnemonic not in mnemonics:
                    mnemonics.append(curve.mnemonic)
        return mnemonics

    def merged_curve(self, mnemonic):
        """Merged curve on :meth:`depth_grid`.

        Returns ``(depths, values, provenance)`` where ``provenance`` is an
        object array of per-sample provenance labels.
        """
        grid = self.depth_grid()
        values = np.empty(grid.shape, dtype=float)
        provenance = np.empty(grid.shape, dtype=object)
        for row, depth in enumerate(grid):
            result = self.query(mnemonic, depth)
            values[row] = result.value
            provenance[row] = result.provenance
        return grid, values, provenance

    def to_las(self):
        """Materialise the merge as a new :class:`lasio.LASFile`.

        The merged file uses :meth:`depth_grid` and the merged values for
        every known curve.  Provenance is not stored in the LAS; use
        :meth:`query`/:meth:`merged_curve` for it.
        """
        out = copy.deepcopy(self.base)
        index_mnemonic = self.base.curves[0].mnemonic
        index_unit = self.base.curves[0].unit
        for curve in list(out.curves):
            out.delete_curve(curve.mnemonic)
        grid = self.depth_grid()
        out.append_curve(index_mnemonic, grid, unit=index_unit)
        for mnemonic in self.curve_mnemonics():
            _, values, _ = self.merged_curve(mnemonic)
            unit = ""
            if mnemonic in self.base.curvesdict:
                unit = self.base.curvesdict[mnemonic].unit
            else:
                for layer in self.layers:
                    curve = layer._supplement_curve(mnemonic)
                    if curve is not None:
                        unit = curve.unit
                        break
            out.append_curve(mnemonic, values, unit=unit)
        try:
            out.update_start_stop_step(STRT=grid[0], STOP=grid[-1])
        except (KeyError, IndexError):
            pass
        return out


def merge_relog(base, supplement, interval=None, manifest=None):
    """Merge a re-log supplement into a base LAS; returns a RelogMerge.

    Convenience wrapper around :class:`RelogMerge`::

        merge = RelogMerge(base)
        merge.ingest(supplement, interval, manifest)
    """
    merge = RelogMerge(base)
    merge.ingest(supplement, interval=interval, manifest=manifest)
    return merge
