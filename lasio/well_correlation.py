"""Correlate two wells by mapping control-well depths onto a master well.

The module performs an order-preserving well-to-well depth tie based on
stratigraphic interface markers (formation tops) picked in both wells.
A formation name may occur more than once in a well (e.g. repeated beds);
such repeats are tracked as separate, depth-ordered occurrences and may
never be crossed to make a same-name tie.

The mapping is constrained by:

* anchor pairs (a control-well interface tied to a master-well interface),
  each with an uncertainty tolerance;
* per-layer thickness stretch/squeeze limits, given as multiplicative
  factors of the control-well layer thickness;
* strict stratigraphic order - mapped interfaces may not cross confirmed
  interfaces.

When a unique depth cannot be derived for a control-well sample, the
feasible master-depth interval is returned instead.  If the observations
are mutually incompatible, the conflicting interface observations are
reported rather than silently producing a tie.
"""

from dataclasses import dataclass, field
import math

import numpy as np


class CorrelationError(ValueError):
    """Base class for well-correlation problems."""


class MarkerSpecificationError(CorrelationError):
    """The marker/anchor specification is internally inconsistent."""


class CorrelationConflictError(CorrelationError):
    """Raised when the interface observations cannot be satisfied.

    The individual contradictions are available on :attr:`conflicts`.
    """

    def __init__(self, message, conflicts):
        super().__init__(message)
        self.conflicts = conflicts


@dataclass
class Marker:
    """A confirmed stratigraphic interface (formation top) in one well.

    Attributes:
        name: formation/interface name.
        depth: depth of the interface in the well's own depth frame.
    """

    name: str
    depth: float


@dataclass
class Anchor:
    """A tie between one interface occurrence in the two wells.

    Attributes:
        name: formation/interface name.
        master_depth: depth of the interface in the master well.
        control_depth: depth of the interface in the control well.
        master_occurrence: zero-based occurrence index in the master well
            when the name is repeated.
        control_occurrence: zero-based occurrence index in the control
            well when the name is repeated.
    """

    name: str
    master_depth: float
    control_depth: float
    master_occurrence: int = 0
    control_occurrence: int = 0

    @property
    def master_key(self):
        return self.name, self.master_occurrence

    @property
    def control_key(self):
        return self.name, self.control_occurrence


@dataclass
class StretchLimits:
    """Allowed control-to-master thickness scaling for each layer.

    Factors multiply the control-well layer thickness to bound its mapped
    thickness in the master well.  A factor of 1.0 means the thickness is
    preserved; ``(0.8, 1.2)`` allows the layer to be squeezed by 20% or
    stretched by 20%.  Exact anchors whose implied scale falls outside
    these limits produce a thickness conflict.
    """

    default: tuple = (1.0, 1.0)
    by_name: dict = field(default_factory=dict)

    def __post_init__(self):
        self._validate(self.default)
        for limits in self.by_name.values():
            self._validate(limits)

    def for_layer(self, upper_name, lower_name):
        if (upper_name, lower_name) in self.by_name:
            return self._validate(self.by_name[(upper_name, lower_name)])
        if lower_name in self.by_name:
            return self._validate(self.by_name[lower_name])
        return self._validate(self.default)

    @staticmethod
    def _validate(limits):
        low, high = float(limits[0]), float(limits[1])
        if not (0.0 < low <= high):
            raise MarkerSpecificationError(
                "stretch limits must satisfy 0 < low <= high, got {}".format(
                    limits
                )
            )
        return low, high


@dataclass
class Conflict:
    """A mutually contradictory pair of interface observations."""

    kind: str
    message: str
    markers: tuple = ()
    anchors: tuple = ()


@dataclass
class IntervalResult:
    """Mapping outcome for one control-well sample depth."""

    control_depth: float
    nominal: float
    lower: float
    upper: float
    unique: bool
    status: str
    reason: str = ""


class _Node:
    """A control-space interface vertex used by the constraint solver."""

    def __init__(self, name, master_occurrence, control_occurrence,
                 control_depth, master_depth, anchor=None):
        self.name = name
        self.master_occurrence = master_occurrence
        self.control_occurrence = control_occurrence
        self.control_depth = float(control_depth)
        self.master_depth = float(master_depth)
        self.anchor = anchor

    @property
    def label(self):
        if self.master_occurrence or self.control_occurrence:
            return "{}#m{}/c{}".format(
                self.name,
                self.master_occurrence + 1,
                self.control_occurrence + 1,
            )
        return self.name


def _as_markers(markers):
    normalized = []
    for marker in markers:
        if isinstance(marker, Marker):
            normalized.append(Marker(marker.name, float(marker.depth)))
        else:
            name, depth = marker[0], marker[1]
            normalized.append(Marker(name, float(depth)))
    return normalized


def _occurrence_map(markers):
    """Group markers by name, assigning depth-ordered occurrence indices."""
    grouped = {}
    for marker in sorted(markers, key=lambda item: item.depth):
        grouped.setdefault(marker.name, []).append(marker)
    occurrences = {}
    for name, picks in grouped.items():
        for index, marker in enumerate(picks):
            occurrences[(name, index)] = marker
    return occurrences


class CorrelationEngine:
    """Build and solve an order-preserving two-well depth correlation.

    Args:
        master_depths: sample depths of the master-well log.
        control_depths: sample depths of the control-well log.  These are
            the depths that will be mapped into the master-well frame.
        master_markers: interfaces picked in the master well, as
            :class:`Marker` objects or ``(name, depth)`` tuples.
        control_markers: interfaces picked in the control well.
        anchors: explicit :class:`Anchor` ties.  Names not listed here are
            paired automatically by their depth-ordered occurrence.
        anchor_tolerance: maximum accepted mismatch of an anchor, in
            master-depth units.  Anchor images may move within
            ``depth +/- tolerance``.
        stretch: :class:`StretchLimits`, a ``(low, high)`` tuple used for
            every layer, or a dict keyed by lower marker name /
            ``(upper, lower)`` name tuples.
        extrapolate: ``(low, high)`` stretch factors for the partial
            intervals above the first and below the last tied interface.
            ``None`` leaves out-of-range samples unresolved.
        unique_tolerance: depth band width at or below which a sample is
            reported as a unique depth.  Defaults to ``anchor_tolerance``.

    Interfaces picked in only one of the two wells have no counterpart to
    tie to, so they are not part of the constraint network; every shared
    interface is either anchored explicitly or paired by its depth-ordered
    occurrence.
    """

    def __init__(self, master_depths, control_depths, master_markers,
                 control_markers, anchors=None, anchor_tolerance=0.0,
                 stretch=None, extrapolate=None, unique_tolerance=None):
        self.master_depths = np.asarray(master_depths, dtype=float)
        self.control_depths = np.asarray(control_depths, dtype=float)
        self._validate_depths(self.master_depths, "master")
        self._validate_depths(self.control_depths, "control")

        self.master_markers = _as_markers(master_markers)
        self.control_markers = _as_markers(control_markers)
        self.anchors = list(anchors) if anchors else []

        if anchor_tolerance < 0:
            raise MarkerSpecificationError(
                "anchor_tolerance must be non-negative"
            )
        self.anchor_tolerance = float(anchor_tolerance)
        self.unique_tolerance = (
            float(unique_tolerance) if unique_tolerance is not None
            else self.anchor_tolerance
        )

        self.stretch = self._coerce_stretch(stretch)
        self.extrapolate = (
            None if extrapolate is None
            else StretchLimits._validate(extrapolate)
        )

        self.conflicts = []
        self.nodes = []
        self.edge_limits = []
        self.lower_bounds = None
        self.upper_bounds = None
        self._build()

    @property
    def feasible(self):
        """True when no conflicting interface observations were found."""
        return not self.conflicts

    @staticmethod
    def _validate_depths(depths, label):
        if depths.ndim != 1 or depths.size < 2:
            raise MarkerSpecificationError(
                "{} depths must be a 1-D array with at least two samples".format(
                    label
                )
            )
        if np.any(np.diff(depths) <= 0):
            raise MarkerSpecificationError(
                "{} depths must be strictly increasing".format(label)
            )

    @staticmethod
    def _coerce_stretch(stretch):
        if stretch is None:
            return StretchLimits()
        if isinstance(stretch, StretchLimits):
            return stretch
        if isinstance(stretch, dict):
            return StretchLimits(default=(1.0, 1.0), by_name=stretch)
        return StretchLimits(default=stretch)

    def _build(self):
        master_occ = _occurrence_map(self.master_markers)
        control_occ = _occurrence_map(self.control_markers)
        self._check_anchor_inputs(master_occ, control_occ)
        self.nodes = self._build_nodes(master_occ, control_occ)
        if self._check_node_order():
            self._build_edges()
            self._propagate_bounds()
            self._detect_conflicts()

    def _check_anchor_inputs(self, master_occ, control_occ):
        anchor_master_keys = set()
        anchor_control_keys = set()
        for anchor in self.anchors:
            master_key = anchor.master_key
            control_key = anchor.control_key
            if master_key in anchor_master_keys:
                raise MarkerSpecificationError(
                    "duplicate anchor for master interface {}".format(
                        anchor.master_key
                    )
                )
            if control_key in anchor_control_keys:
                raise MarkerSpecificationError(
                    "duplicate anchor for control interface {}".format(
                        anchor.control_key
                    )
                )
            anchor_master_keys.add(master_key)
            anchor_control_keys.add(control_key)

            if master_key not in master_occ:
                raise MarkerSpecificationError(
                    "anchor references unknown master interface {}".format(
                        master_key
                    )
                )
            if control_key not in control_occ:
                raise MarkerSpecificationError(
                    "anchor references unknown control interface {}".format(
                        control_key
                    )
                )
            master_pick = master_occ[master_key]
            if (abs(master_pick.depth - anchor.master_depth)
                    > self.anchor_tolerance):
                raise MarkerSpecificationError(
                    "anchor master depth {} for {} disagrees with picked "
                    "depth {} by more than tolerance {}".format(
                        anchor.master_depth, master_key,
                        master_pick.depth, self.anchor_tolerance,
                    )
                )
            control_pick = control_occ[control_key]
            if (abs(control_pick.depth - anchor.control_depth)
                    > self.anchor_tolerance):
                raise MarkerSpecificationError(
                    "anchor control depth {} for {} disagrees with picked "
                    "depth {} by more than tolerance {}".format(
                        anchor.control_depth, control_key,
                        control_pick.depth, self.anchor_tolerance,
                    )
                )

        names = {m.name for m in self.master_markers} | {
            m.name for m in self.control_markers
        }
        for name in sorted(names):
            master_keys = sorted(
                key for key in master_occ if key[0] == name
            )
            control_keys = sorted(
                key for key in control_occ if key[0] == name
            )
            if not master_keys or not control_keys:
                continue
            if len(master_keys) != len(control_keys):
                anchored = [
                    anchor for anchor in self.anchors
                    if anchor.name == name
                ]
                if not anchored:
                    raise MarkerSpecificationError(
                        "formation '{}' occurs {} time(s) in the master well "
                        "but {} time(s) in the control well; anchors are "
                        "required to identify the corresponding "
                        "occurrences".format(
                            name, len(master_keys), len(control_keys)
                        )
                    )

    def _build_nodes(self, master_occ, control_occ):
        pairs = []
        used_master = set()
        used_control = set()

        for anchor in self.anchors:
            pairs.append(
                (anchor.master_key, anchor.control_key, anchor)
            )
            used_master.add(anchor.master_key)
            used_control.add(anchor.control_key)

        names = {key[0] for key in master_occ} | {
            key[0] for key in control_occ
        }
        for name in sorted(names):
            master_keys = sorted(
                key for key in master_occ if key[0] == name
            )
            control_keys = sorted(
                key for key in control_occ if key[0] == name
            )
            if not master_keys or not control_keys:
                continue
            if len(master_keys) == len(control_keys):
                for master_key, control_key in zip(
                        master_keys, control_keys):
                    if (master_key in used_master
                            or control_key in used_control):
                        continue
                    pairs.append((master_key, control_key, None))
                    used_master.add(master_key)
                    used_control.add(control_key)
            else:
                smaller_keys = (
                    master_keys if len(master_keys) < len(control_keys)
                    else control_keys
                )
                used = (
                    used_master if smaller_keys is master_keys
                    else used_control
                )
                if any(key not in used for key in smaller_keys):
                    raise MarkerSpecificationError(
                        "formation '{}' has unequal occurrence counts; "
                        "anchor every occurrence on the well where it is "
                        "less repeated".format(name)
                    )

        pairs.sort(key=lambda item: control_occ[item[1]].depth)
        nodes = []
        for master_key, control_key, anchor in pairs:
            master_pick = master_occ[master_key]
            control_pick = control_occ[control_key]
            nodes.append(
                _Node(
                    master_key[0],
                    master_key[1],
                    control_key[1],
                    control_pick.depth,
                    master_pick.depth,
                    anchor,
                )
            )

        if len(nodes) < 2:
            raise MarkerSpecificationError(
                "at least two shared confirmed interfaces are required"
            )
        control_depths = [node.control_depth for node in nodes]
        if len(set(control_depths)) != len(control_depths):
            raise MarkerSpecificationError(
                "control interfaces share a depth; order cannot be "
                "established"
            )
        self.nodes = nodes
        return nodes

    def _check_node_order(self):
        """Record conflicts where a tie would cross a confirmed interface."""
        inverted = False
        for upper, lower in zip(self.nodes, self.nodes[1:]):
            if lower.master_depth <= upper.master_depth:
                inverted = True
                if upper.anchor is not None and lower.anchor is not None:
                    self.conflicts.append(
                        Conflict(
                            "order",
                            "anchors {} and {} are in depth order in the "
                            "control well but reversed in the master well; "
                            "a same-formation tie would cross confirmed "
                            "interfaces".format(upper.label, lower.label),
                            markers=(upper.label, lower.label),
                            anchors=(upper.label, lower.label),
                        )
                    )
                else:
                    self.conflicts.append(
                        Conflict(
                            "order",
                            "pairing {} and {} would cross confirmed "
                            "master interfaces; supply explicit anchors to "
                            "resolve the repeated formation".format(
                                upper.label, lower.label
                            ),
                            markers=(upper.label, lower.label),
                        )
                    )
        return not inverted

    def _build_edges(self):
        self.edge_limits = []
        for upper, lower in zip(self.nodes, self.nodes[1:]):
            control_thickness = lower.control_depth - upper.control_depth
            master_thickness = lower.master_depth - upper.master_depth
            if control_thickness <= 0 or master_thickness <= 0:
                raise MarkerSpecificationError(
                    "layer between {} and {} has non-positive thickness".format(
                        upper.label, lower.label
                    )
                )
            low_factor, high_factor = self.stretch.for_layer(
                upper.name, lower.name
            )
            self.edge_limits.append(
                (
                    low_factor * control_thickness,
                    high_factor * control_thickness,
                    control_thickness,
                )
            )

    def _propagate_bounds(self):
        count = len(self.nodes)
        lower = np.empty(count)
        upper = np.empty(count)
        for index, node in enumerate(self.nodes):
            if node.anchor is not None:
                center = node.anchor.master_depth
                lower[index] = center - self.anchor_tolerance
                upper[index] = center + self.anchor_tolerance
            else:
                lower[index] = upper[index] = node.master_depth
        edge_low = np.array([edge[0] for edge in self.edge_limits])
        edge_high = np.array([edge[1] for edge in self.edge_limits])
        for _ in range(count):
            previous_lower = lower.copy()
            previous_upper = upper.copy()
            for index in range(count - 1):
                lower[index + 1] = max(
                    lower[index + 1], lower[index] + edge_low[index]
                )
                upper[index] = min(
                    upper[index], upper[index + 1] - edge_low[index]
                )
            for index in range(count - 2, -1, -1):
                lower[index] = max(
                    lower[index], lower[index + 1] - edge_high[index]
                )
                upper[index + 1] = min(
                    upper[index + 1], upper[index] + edge_high[index]
                )
            if (np.array_equal(lower, previous_lower)
                    and np.array_equal(upper, previous_upper)):
                break
        self.lower_bounds = lower
        self.upper_bounds = upper

    def _detect_conflicts(self):
        edge_conflicts = [False] * len(self.edge_limits)
        for index, (low, high, _) in enumerate(self.edge_limits):
            upper_node = self.nodes[index]
            lower_node = self.nodes[index + 1]
            available_min = (
                self.lower_bounds[index + 1] - self.upper_bounds[index]
            )
            available_max = (
                self.upper_bounds[index + 1] - self.lower_bounds[index]
            )
            if low > available_max or high < available_min:
                edge_conflicts[index] = True
                self.conflicts.append(
                    Conflict(
                        "thickness",
                        "layer {} -> {} requires master thickness within "
                        "[{:.4g}, {:.4g}] but confirmed interfaces only "
                        "allow [{:.4g}, {:.4g}]".format(
                            upper_node.label, lower_node.label,
                            low, high, available_min, available_max,
                        ),
                        markers=(upper_node.label, lower_node.label),
                    )
                )

        for index, node in enumerate(self.nodes):
            if self.lower_bounds[index] <= self.upper_bounds[index]:
                continue
            explained_by_edge = any(
                edge_conflicts[edge]
                for edge in (index - 1, index)
                if 0 <= edge < len(edge_conflicts)
            )
            if explained_by_edge:
                continue
            if node.anchor:
                self.conflicts.append(
                    Conflict(
                        "anchor",
                        "anchor {} (control {:.4g} -> master {:.4g}) "
                        "cannot be honoured without violating order or "
                        "thickness limits".format(
                            node.label, node.control_depth,
                            node.master_depth,
                        ),
                        markers=(node.label,),
                        anchors=(node.label,),
                    )
                )
            else:
                self.conflicts.append(
                    Conflict(
                        "order",
                        "interface {} has no feasible master depth".format(
                            node.label
                        ),
                        markers=(node.label,),
                    )
                )

    def assess(self):
        """Return the list of :class:`Conflict` objects (empty if feasible)."""
        return list(self.conflicts)

    def raise_if_infeasible(self):
        """Raise :class:`CorrelationConflictError` if conflicts exist."""
        if self.conflicts:
            raise CorrelationConflictError(
                "{} conflicting interface observation(s); "
                "see the conflicts attribute for details".format(
                    len(self.conflicts)
                ),
                list(self.conflicts),
            )

    def map_samples(self, sample_depths=None):
        """Map control-well depths onto the master-well frame.

        Args:
            sample_depths: depths to map.  Defaults to the control-well
                sample depths supplied to the engine.

        Returns:
            dict with ``nominal``, ``lower``, ``upper`` numpy arrays, a
            boolean ``unique`` array, a string ``status`` array
            (``"unique"``, ``"interval"`` or ``"unresolved"``) and a
            ``reasons`` dict keyed by sample index.
        """
        if sample_depths is None:
            sample_depths = self.control_depths
        sample_depths = np.asarray(sample_depths, dtype=float)
        self.raise_if_infeasible()

        nominal = np.full(sample_depths.shape, np.nan)
        lower = np.full(sample_depths.shape, np.nan)
        upper = np.full(sample_depths.shape, np.nan)
        unique = np.zeros(sample_depths.shape, dtype=bool)
        status = np.full(sample_depths.shape, "", dtype=object)
        reasons = {}

        node_control = np.array([n.control_depth for n in self.nodes])
        node_master = np.array([n.master_depth for n in self.nodes])

        for sample_index, depth in enumerate(sample_depths):
            position = np.searchsorted(node_control, depth)
            if depth < node_control[0] or depth > node_control[-1]:
                result = self._map_extrapolation(depth, position)
            elif position == 0:
                result = self._map_within_layer(depth, 0, node_master)
            elif position == len(self.nodes):
                result = self._map_within_layer(
                    depth, len(self.nodes) - 2, node_master
                )
            else:
                result = self._map_within_layer(
                    depth, position - 1, node_master
                )
            nominal[sample_index] = result.nominal
            lower[sample_index] = result.lower
            upper[sample_index] = result.upper
            unique[sample_index] = result.unique
            status[sample_index] = result.status
            if result.reason:
                reasons[sample_index] = result.reason

        return {
            "nominal": nominal,
            "lower": lower,
            "upper": upper,
            "unique": unique,
            "status": status,
            "reasons": reasons,
        }

    def _map_within_layer(self, depth, index, node_master):
        upper = self.nodes[index]
        lower = self.nodes[index + 1]
        edge_low, _, control_thickness = self.edge_limits[index]
        fraction = (depth - upper.control_depth) / control_thickness

        low_upper_node = self.lower_bounds[index]
        low_lower_node = max(
            self.lower_bounds[index + 1], low_upper_node + edge_low
        )
        band_low = (
            (1.0 - fraction) * low_upper_node + fraction * low_lower_node
        )

        high_lower_node = self.upper_bounds[index + 1]
        high_upper_node = min(
            self.upper_bounds[index], high_lower_node - edge_low
        )
        band_high = (
            (1.0 - fraction) * high_upper_node
            + fraction * high_lower_node
        )

        nominal_depth = (
            node_master[index]
            + fraction * (node_master[index + 1] - node_master[index])
        )
        return self._build_result(depth, nominal_depth, band_low, band_high)

    def _map_extrapolation(self, depth, position):
        if self.extrapolate is None:
            return IntervalResult(
                depth, np.nan, np.nan, np.nan, False, "unresolved",
                "depth lies outside the tied interface range",
            )

        if position == 0:
            node = self.nodes[0]
            node_index = 0
            extension = node.control_depth - depth
            sign = -1.0
        else:
            node = self.nodes[-1]
            node_index = len(self.nodes) - 1
            extension = depth - node.control_depth
            sign = 1.0

        low_factor, high_factor = self.extrapolate
        extra_low = low_factor * extension
        extra_high = high_factor * extension
        if sign > 0:
            band_low = self.lower_bounds[node_index] + extra_low
            band_high = self.upper_bounds[node_index] + extra_high
        else:
            band_low = self.lower_bounds[node_index] - extra_high
            band_high = self.upper_bounds[node_index] - extra_low
        nominal_depth = (
            node.master_depth
            + sign * (extra_low + extra_high) / 2.0
        )
        return self._build_result(depth, nominal_depth, band_low, band_high)

    def _build_result(self, depth, nominal_depth, band_low, band_high):
        if band_high < band_low and not math.isclose(
                band_high, band_low, rel_tol=0.0, abs_tol=1e-12):
            return IntervalResult(
                depth, np.nan, np.nan, np.nan, False, "unresolved",
                "no feasible depth under the stated constraints",
            )
        point = min(max(nominal_depth, band_low), band_high)
        is_unique = (band_high - band_low) <= self.unique_tolerance
        return IntervalResult(
            depth,
            point,
            band_low,
            band_high,
            is_unique,
            "unique" if is_unique else "interval",
        )

    def intervals(self, sample_depths=None):
        """Return per-sample :class:`IntervalResult` objects."""
        mapping = self.map_samples(sample_depths)
        samples = (
            self.control_depths if sample_depths is None
            else np.asarray(sample_depths, dtype=float)
        )
        results = []
        for index, depth in enumerate(samples):
            results.append(
                IntervalResult(
                    float(depth),
                    mapping["nominal"][index],
                    mapping["lower"][index],
                    mapping["upper"][index],
                    bool(mapping["unique"][index]),
                    mapping["status"][index],
                    mapping["reasons"].get(index, ""),
                )
            )
        return results


def correlate_wells(master_well, control_well, master_markers,
                    control_markers, **kwargs):
    """Convenience entry point using two in-memory LAS files.

    Args:
        master_well: the master :class:`lasio.LASFile` (or a 1-D depth
            array).
        control_well: the control :class:`lasio.LASFile` (or a 1-D depth
            array).
        master_markers, control_markers: interface picks accepted by
            :class:`CorrelationEngine`.
        **kwargs: further keyword arguments for :class:`CorrelationEngine`.

    Returns:
        the mapping dict produced by
        :meth:`CorrelationEngine.map_samples`.
    """
    master_depths = _well_depths(master_well)
    control_depths = _well_depths(control_well)
    engine = CorrelationEngine(
        master_depths, control_depths,
        master_markers, control_markers, **kwargs
    )
    return engine.map_samples()


def _well_depths(well):
    if isinstance(well, np.ndarray):
        return well
    if hasattr(well, "index_unit") and well.index_unit and hasattr(
            well, "depth_m"):
        try:
            return well.depth_m
        except Exception:
            pass
    return well.index
