"""Compatibility of two depth-windowed probe logs with one piecewise-constant bed model.

The two probes are assumed to measure a thickness-weighted average of the
formation values inside a depth response window around each sample depth.
A narrow probe can resolve a thin bed as a sharp peak while a wide probe
averages the same bed into near-background values, so comparing the two
curves on a common resampled grid is meaningless.  Instead every valid
observation is converted into a linear constraint on the unknown bed
values

    low_i <= sum_j(w_ij / h_i) * x_j <= high_i

where ``w_ij`` is the thickness of probe window *i* overlapping bed *j*,
``h_i = sum_j w_ij`` and ``x_j`` is the (constant) value of bed *j*.
Feasibility of the resulting linear program answers the question whether
a single set of bed values can reproduce both logs within tolerance.

Example
-------

A 1 m thick bed ``B`` (value 50) between two background beds ``A`` and
``C`` (value 10).  A thin probe (half window 0.5 m) sees the bed as a
50 peak; a thick probe (half window 2 m) over the same depth averages
1.5 m of A + 1 m of B + 1.5 m of C over 4 m and reads 20.

>>> thin = WindowSpec.symmetric("thin", 0.5, 0.5)
>>> thick = WindowSpec.symmetric("thick", 2.0, 0.5)
>>> beds = [(100.0, 104.5, "A"), (104.5, 105.5, "B"),
...         (105.5, 110.0, "C")]
>>> observations = [
...     Observation("thin", 105.0, 50.0),
...     Observation("thick", 105.0, 20.0),
... ]
>>> report = check_bed_compatibility(
...     beds, {"thin": thin, "thick": thick}, observations,
...     value_bounds=(0.0, 100.0), compute_ranges=False)
>>> report.feasible
True
>>> report.coverage[0].weights
{'B': 1.0}
>>> report.coverage[1].weights == {"A": 1.5, "B": 1.0, "C": 1.5}
True

The two curves are never placed on a common grid: each observation keeps
its own window geometry and contributes thickness-weighted constraints.
Missing samples and windows crossing the known bed boundaries are listed
separately instead of being silently interpolated.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from typing import Optional

import numpy as np


class BedCompatibilityError(ValueError):
    """Raised when the supplied bed or window geometry is inconsistent."""


@dataclass(frozen=True)
class WindowSpec:
    """Depth response window ``[depth + top_offset, depth + bottom_offset]``."""

    name: str
    top_offset: float
    bottom_offset: float
    tolerance: float

    def __post_init__(self):
        if self.top_offset > 0 or self.bottom_offset < 0:
            raise BedCompatibilityError(
                f"window {self.name!r}: top_offset must be <= 0 and "
                "bottom_offset >= 0"
            )
        if self.top_offset == self.bottom_offset:
            raise BedCompatibilityError(
                f"window {self.name!r} has zero thickness"
            )
        if self.tolerance < 0:
            raise BedCompatibilityError(
                f"window {self.name!r}: tolerance must be non-negative"
            )

    @classmethod
    def symmetric(cls, name, half_height, tolerance):
        return cls(name, -abs(half_height), abs(half_height), tolerance)

    def span(self, depth):
        return depth + self.top_offset, depth + self.bottom_offset


@dataclass(frozen=True)
class Observation:
    """One logged sample; ``value`` of ``None``/NaN marks a missing sample."""

    probe: str
    depth: float
    value: Optional[float]
    tolerance: Optional[float] = None

    @property
    def identifier(self):
        return f"{self.probe}@{self.depth:g}"


@dataclass
class CoverageRow:
    """Thickness accounting for one observation window."""

    observation: str
    probe: str
    depth: float
    window_top: float
    window_bottom: float
    weights: dict
    supported_thickness: float
    status: str
    detail: str

    def as_dict(self):
        return {
            "observation": self.observation,
            "probe": self.probe,
            "depth": self.depth,
            "window_top": self.window_top,
            "window_bottom": self.window_bottom,
            "weights": dict(self.weights),
            "supported_thickness": self.supported_thickness,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class CompatibilityReport:
    """Result of :func:`check_bed_compatibility`.

    ``status`` is one of ``"feasible"``, ``"infeasible"`` or
    ``"indeterminate"`` (no usable observation survived screening).
    ``feasible`` is the boolean shortcut for the feasible status.
    """

    feasible: bool
    status: str
    bed_ids: list
    bed_tops: list
    bed_bottoms: list
    bed_values: Optional[dict]
    bed_value_ranges: Optional[dict]
    coverage: list
    missing: list
    out_of_bounds: list
    conflicts: list
    predictions: list
    solver_tolerance: float
    detail: str

    def as_dict(self):
        ranges = None
        if self.bed_value_ranges is not None:
            ranges = {
                bid: [
                    None if not math.isfinite(low) else low,
                    None if not math.isfinite(high) else high,
                ]
                for bid, (low, high) in self.bed_value_ranges.items()
            }
        return {
            "feasible": self.feasible,
            "status": self.status,
            "beds": [
                {"id": bid, "top": top, "bottom": bottom}
                for bid, top, bottom in zip(
                    self.bed_ids, self.bed_tops, self.bed_bottoms
                )
            ],
            "bed_values": self.bed_values,
            "bed_value_ranges": ranges,
            "coverage": [row.as_dict() for row in self.coverage],
            "missing": self.missing,
            "out_of_bounds": self.out_of_bounds,
            "conflicts": self.conflicts,
            "predictions": self.predictions,
            "solver_tolerance": self.solver_tolerance,
            "detail": self.detail,
        }

    def to_json(self, indent=2):
        return json.dumps(self.as_dict(), indent=indent, allow_nan=False)


def _normalise_beds(beds):
    ids, tops, bottoms = [], [], []
    for index, bed in enumerate(beds):
        if len(bed) == 2:
            top, bottom = bed
            bid = f"bed{index + 1}"
        else:
            top, bottom, bid = bed[0], bed[1], str(bed[2])
        if not bottom > top:
            raise BedCompatibilityError(
                f"bed {bid!r}: bottom {bottom} must be below top {top}"
            )
        ids.append(bid)
        tops.append(float(top))
        bottoms.append(float(bottom))
    for index in range(1, len(ids)):
        if not math.isclose(tops[index], bottoms[index - 1], abs_tol=1e-9):
            raise BedCompatibilityError(
                f"beds must be contiguous: {ids[index - 1]!r} ends at "
                f"{bottoms[index - 1]} but {ids[index]!r} starts at {tops[index]}"
            )
    if len(set(ids)) != len(ids):
        raise BedCompatibilityError("bed identifiers must be unique")
    return ids, np.array(tops), np.array(bottoms)


def _window_weights(window_top, window_bottom, bed_tops, bed_bottoms):
    overlap = np.minimum(bed_bottoms, window_bottom) - np.maximum(
        bed_tops, window_top
    )
    return np.maximum(overlap, 0.0)


def _as_bounds(value_bounds, bed_count, bed_ids):
    if value_bounds is None:
        return [(None, None)] * bed_count
    if isinstance(value_bounds, dict):
        return [
            (
                _finite_or_none(value_bounds.get(bid, (None, None))[0]),
                _finite_or_none(value_bounds.get(bid, (None, None))[1]),
            )
            for bid in bed_ids
        ]
    if (
        isinstance(value_bounds, (tuple, list))
        and len(value_bounds) == 2
        and all(not isinstance(value, (tuple, list)) for value in value_bounds)
    ):
        low, high = value_bounds
        return [(_finite_or_none(low), _finite_or_none(high))] * bed_count
    if len(value_bounds) != bed_count:
        raise BedCompatibilityError(
            "value_bounds must have one entry per bed or be a single (low, high)"
        )
    return [
        (
            _finite_or_none(pair[0] if pair is not None else None),
            _finite_or_none(pair[1] if pair is not None else None),
        )
        for pair in value_bounds
    ]


def _finite_or_none(value):
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def check_bed_compatibility(
    beds,
    windows,
    observations,
    value_bounds=None,
    strict_support=True,
    compute_ranges=True,
    solver_tolerance=1e-9,
):
    """Test whether one piecewise-constant bed model fits both probe logs.

    Parameters
    ----------
    beds:
        Sequence of ``(top, bottom)`` or ``(top, bottom, bed_id)`` tuples.
        Beds must be contiguous and ordered from shallow to deep.
    windows:
        Mapping of probe name to :class:`WindowSpec`.
    observations:
        Sequence of :class:`Observation`.  A NaN/``None`` value is reported
        as missing and excluded from the feasibility system.
    value_bounds:
        Optional prior limits on bed values.  Either one ``(low, high)``
        pair applied to every bed, one pair per bed (``None`` entry for an
        unbounded side), or a mapping keyed by bed id.
    strict_support:
        If ``True`` (default), any window reaching outside the known bed
        interval is listed under ``out_of_bounds`` and excluded, because the
        formation beyond the known beds is unconstrained.  If ``False`` the
        window is clipped to the known interval and the remaining thickness
        is renormalised.
    compute_ranges:
        When feasible, additionally solve for the min/max value each bed can
        take while still satisfying every observation.
    solver_tolerance:
        Numerical tolerance for the two-phase simplex solver.

    Returns
    -------
    CompatibilityReport
    """

    bed_ids, bed_tops, bed_bottoms = _normalise_beds(beds)
    bed_count = len(bed_ids)
    bounds = _as_bounds(value_bounds, bed_count, bed_ids)
    domain_top, domain_bottom = bed_tops[0], bed_bottoms[-1]
    windows = dict(windows)

    coverage_rows = []
    missing = []
    out_of_bounds = []
    valid = []
    seen_identifiers = {}

    for observation in observations:
        spec = windows.get(observation.probe)
        if spec is None:
            raise BedCompatibilityError(
                f"no window specification registered for probe "
                f"{observation.probe!r}"
            )
        window_top, window_bottom = spec.span(observation.depth)
        base_identifier = observation.identifier
        occurrence = seen_identifiers.get(base_identifier, 0) + 1
        seen_identifiers[base_identifier] = occurrence
        identifier = (
            base_identifier
            if occurrence == 1
            else f"{base_identifier}#{occurrence}"
        )
        tolerance = (
            observation.tolerance
            if observation.tolerance is not None
            else spec.tolerance
        )

        if observation.value is None or (
            isinstance(observation.value, float)
            and math.isnan(observation.value)
        ):
            missing.append(
                {
                    "observation": identifier,
                    "probe": observation.probe,
                    "depth": observation.depth,
                    "reason": "missing sample value",
                }
            )
            continue

        weights = _window_weights(
            window_top, window_bottom, bed_tops, bed_bottoms
        )
        supported = float(weights.sum())
        window_thickness = window_bottom - window_top
        fully_inside = (
            window_top >= domain_top - 1e-12
            and window_bottom <= domain_bottom + 1e-12
        )

        if not fully_inside:
            record = {
                "observation": identifier,
                "probe": observation.probe,
                "depth": observation.depth,
                "window_top": window_top,
                "window_bottom": window_bottom,
                "known_domain": [domain_top, domain_bottom],
                "supported_thickness": supported,
                "window_thickness": window_thickness,
            }
            if strict_support or supported <= solver_tolerance:
                record["reason"] = (
                    "window reaches outside the known beds; no bed values "
                    "exist for the unsupported thickness"
                )
                out_of_bounds.append(record)
                coverage_rows.append(
                    CoverageRow(
                        identifier,
                        observation.probe,
                        observation.depth,
                        window_top,
                        window_bottom,
                        {
                            bed_ids[j]: float(weights[j])
                            for j in range(bed_count)
                            if weights[j] > 0.0
                        },
                        supported,
                        "out_of_bounds",
                        record["reason"],
                    )
                )
                continue

        if strict_support:
            normalised = weights / window_thickness
            effective_thickness = window_thickness
        else:
            normalised = weights / supported
            effective_thickness = supported

        row_weights = {
            bed_ids[j]: float(weights[j])
            for j in range(bed_count)
            if weights[j] > 0.0
        }
        coverage_rows.append(
            CoverageRow(
                identifier,
                observation.probe,
                observation.depth,
                window_top,
                window_bottom,
                row_weights,
                supported if not strict_support else effective_thickness,
                "valid",
                "window fully inside known beds"
                if strict_support
                else "window clipped to known beds and renormalised",
            )
        )
        valid.append(
            {
                "observation": observation,
                "identifier": identifier,
                "normalised_weights": normalised,
                "thickness": effective_thickness,
                "tolerance": float(tolerance),
            }
        )

    if not valid:
        return CompatibilityReport(
            feasible=False,
            status="indeterminate",
            bed_ids=bed_ids,
            bed_tops=list(bed_tops),
            bed_bottoms=list(bed_bottoms),
            bed_values=None,
            bed_value_ranges=None,
            coverage=coverage_rows,
            missing=missing,
            out_of_bounds=out_of_bounds,
            conflicts=[],
            predictions=[],
            solver_tolerance=solver_tolerance,
            detail=(
                "no usable observations remain after screening; "
                "compatibility cannot be decided"
            ),
        )

    constraint_rows = []
    for item in valid:
        observation = item["observation"]
        weights = item["normalised_weights"]
        half_tol = item["tolerance"]
        centre = float(observation.value)
        constraint_rows.append(
            (item["identifier"], weights.copy(), centre + half_tol,
             centre - half_tol)
        )

    bound_row_count = sum(low is not None for low, _ in bounds) + sum(
        high is not None for _, high in bounds
    )
    matrix = np.zeros((len(valid) * 2 + bound_row_count, bed_count))
    rhs = np.zeros(matrix.shape[0])
    row_tags = []
    row_index = 0
    for identifier, weights, high, low in constraint_rows:
        for bed_index, coefficient in enumerate(weights):
            if coefficient == 0.0:
                continue
            matrix[row_index, bed_index] = -coefficient
        rhs[row_index] = -high
        row_tags.append(("observation", identifier))
        row_index += 1
        for bed_index, coefficient in enumerate(weights):
            if coefficient == 0.0:
                continue
            matrix[row_index, bed_index] = coefficient
        rhs[row_index] = low
        row_tags.append(("observation", identifier))
        row_index += 1
    for bed_index, (low, high) in enumerate(bounds):
        if low is not None:
            matrix[row_index, bed_index] = 1.0
            rhs[row_index] = low
            row_tags.append(("bound", bed_ids[bed_index]))
            row_index += 1
        if high is not None:
            matrix[row_index, bed_index] = -1.0
            rhs[row_index] = -high
            row_tags.append(("bound", bed_ids[bed_index]))
            row_index += 1

    try:
        solution = _linprog_feasible(matrix, rhs, solver_tolerance)
    except _NumericalInfeasible:
        solution = None

    if solution is None:
        conflict_identifiers = _locate_conflicts(
            matrix, rhs, row_tags, solver_tolerance
        )
        valid_by_identifier = {item["identifier"]: item for item in valid}
        conflict_records = [
            {
                "observation": identifier,
                "probe": valid_by_identifier[identifier]["observation"].probe,
                "depth": valid_by_identifier[identifier]["observation"].depth,
            }
            for identifier in conflict_identifiers
        ]
        return CompatibilityReport(
            feasible=False,
            status="infeasible",
            bed_ids=bed_ids,
            bed_tops=list(bed_tops),
            bed_bottoms=list(bed_bottoms),
            bed_values=None,
            bed_value_ranges=None,
            coverage=coverage_rows,
            missing=missing,
            out_of_bounds=out_of_bounds,
            conflicts=conflict_records,
            predictions=[],
            solver_tolerance=solver_tolerance,
            detail=(
                "constraint system infeasible; the listed observations cannot "
                "be reproduced by any piecewise-constant bed values shared by "
                "both probes within the stated tolerances"
            ),
        )

    bed_values = {
        bed_ids[j]: float(solution[j]) for j in range(bed_count)
    }
    predictions = _predict(valid, bed_values, bed_ids)
    bed_value_ranges = None
    if compute_ranges:
        bed_value_ranges = {}
        for bed_index, bid in enumerate(bed_ids):
            objective = np.zeros(bed_count)
            objective[bed_index] = 1.0
            low_result = _linprog(objective, matrix, rhs, solver_tolerance)
            high_result = _linprog(-objective, matrix, rhs, solver_tolerance)
            if low_result is not None:
                low_value = float(low_result[bed_index])
            else:
                low_value = float("-inf")
            if high_result is not None:
                high_value = float(high_result[bed_index])
            else:
                high_value = float("inf")
            bed_value_ranges[bid] = [low_value, high_value]

    return CompatibilityReport(
        feasible=True,
        status="feasible",
        bed_ids=bed_ids,
        bed_tops=list(bed_tops),
        bed_bottoms=list(bed_bottoms),
        bed_values=bed_values,
        bed_value_ranges=bed_value_ranges,
        coverage=coverage_rows,
        missing=missing,
        out_of_bounds=out_of_bounds,
        conflicts=[],
        predictions=predictions,
        solver_tolerance=solver_tolerance,
        detail=(
            "a single piecewise-constant bed model reproduces every valid "
            "observation within tolerance"
        ),
    )


def _predict(valid_items, bed_values, bed_ids):
    values_by_index = np.array([bed_values[bid] for bid in bed_ids])
    predictions = []
    for item in valid_items:
        predicted = float(item["normalised_weights"] @ values_by_index)
        observed = float(item["observation"].value)
        predictions.append(
            {
                "observation": item["identifier"],
                "probe": item["observation"].probe,
                "depth": item["observation"].depth,
                "observed": observed,
                "predicted": float(predicted),
                "residual": float(observed - predicted),
                "tolerance": item["tolerance"],
                "within_tolerance": abs(observed - predicted)
                <= item["tolerance"] + 1e-9,
            }
        )
    return predictions


def _locate_conflicts(matrix, rhs, row_tags, tolerance):
    """Return an irreducible infeasible set of observation identifiers.

    Deletion filtering: starting from every observation, tentatively remove
    one observation; if the remaining constraint system is still
    infeasible the observation is not needed to explain the conflict and
    stays removed, otherwise it is restored.  The surviving observations
    form a minimal mutually incompatible group.  Bound rows are never
    removed.
    """

    identifiers = []
    for tag in row_tags:
        if tag[0] == "observation" and tag[1] not in identifiers:
            identifiers.append(tag[1])

    retained = set(identifiers)
    for identifier in identifiers:
        trial = retained - {identifier}
        kept_rows = [
            index
            for index, tag in enumerate(row_tags)
            if tag[0] == "bound" or tag[1] in trial
        ]
        try:
            _linprog_feasible(matrix[kept_rows], rhs[kept_rows], tolerance)
        except _NumericalInfeasible:
            retained = trial
    return sorted(retained)


class _NumericalInfeasible(RuntimeError):
    pass


class _Unbounded(RuntimeError):
    pass


def _split_free_variables(matrix):
    """Replace every free column x_j by x_j = p_j - q_j with p, q >= 0."""

    row_count, bed_count = matrix.shape
    expanded = np.zeros((row_count, 2 * bed_count))
    expanded[:, :bed_count] = matrix
    expanded[:, bed_count:] = -matrix
    return expanded, bed_count


def _linprog_feasible(matrix, rhs, tolerance):
    expanded, bed_count = _split_free_variables(matrix)
    point = _two_phase_simplex(expanded, rhs, None, tolerance)
    return point[:bed_count] - point[bed_count:]


def _linprog(objective, matrix, rhs, tolerance):
    expanded, bed_count = _split_free_variables(matrix)
    expanded_objective = np.concatenate([objective, -objective])
    try:
        point = _two_phase_simplex(
            expanded, rhs, expanded_objective, tolerance
        )
    except (_NumericalInfeasible, _Unbounded):
        return None
    return point[:bed_count] - point[bed_count:]


def _two_phase_simplex(matrix, rhs, objective, tolerance):
    """Two-phase simplex for ``matrix @ x >= rhs`` with free ``x``.

    Each row is put in standard form with a non-negative basic value:

    * ``a.x >= b`` with ``b >= 0`` -> ``a.x - s + w = b`` (surplus ``s``
      and artificial ``w``, ``w`` initially basic);
    * ``a.x >= b`` with ``b < 0`` -> flipped to ``(-a).x + s = -b``
      (slack ``s`` initially basic, no artificial needed).

    Phase I minimises the sum of the artificials; a positive phase-I
    optimum proves infeasibility.  Phase II then optimises ``objective``
    with the artificial columns removed.
    """

    row_count, original_count = matrix.shape
    first_slack = original_count
    artificial_rows = np.flatnonzero(rhs >= 0.0)
    first_artificial = original_count + row_count
    total_columns = first_artificial + len(artificial_rows)
    objective_row = row_count
    tableau = np.zeros((row_count + 1, total_columns + 1))
    basis = []

    for row_index in range(row_count):
        slack_column = first_slack + row_index
        if rhs[row_index] >= 0.0:
            tableau[row_index, :original_count] = matrix[row_index]
            tableau[row_index, slack_column] = -1.0
            position = int(np.searchsorted(artificial_rows, row_index))
            artificial_column = first_artificial + position
            tableau[row_index, artificial_column] = 1.0
            tableau[row_index, -1] = rhs[row_index]
            basis.append(artificial_column)
        else:
            tableau[row_index, :original_count] = -matrix[row_index]
            tableau[row_index, slack_column] = 1.0
            tableau[row_index, -1] = -rhs[row_index]
            basis.append(slack_column)

    if len(artificial_rows):
        phase_one_cost = np.zeros(total_columns)
        phase_one_cost[first_artificial:] = 1.0
        tableau[objective_row, :total_columns] = phase_one_cost
        for row_index in artificial_rows:
            tableau[objective_row, :] -= tableau[row_index, :]

    pivot_tolerance = tolerance * max(
        1.0, np.max(np.abs(tableau[:-1, :-1]))
    )
    _optimise(
        tableau,
        basis,
        pivot_tolerance,
        allow_unbounded=False,
        entering_limit=first_artificial,
    )

    if len(artificial_rows) and abs(tableau[objective_row, -1]) > max(
        pivot_tolerance, 1e-8
    ):
        raise _NumericalInfeasible("phase-I objective stays positive")

    for row_index in artificial_rows:
        position = int(np.searchsorted(artificial_rows, row_index))
        artificial_column = first_artificial + position
        if basis[row_index] != artificial_column:
            continue
        candidate = next(
            (
                column
                for column in range(first_artificial)
                if abs(tableau[row_index, column]) > pivot_tolerance
            ),
            None,
        )
        if candidate is None:
            tableau[row_index, :] = 0.0
            basis[row_index] = -1
            continue
        _pivot(tableau, basis, row_index, candidate)

    tableau[:, first_artificial:total_columns] = 0.0

    if objective is None:
        point = np.zeros(original_count)
        for row_index, basic_column in enumerate(basis):
            if 0 <= basic_column < original_count:
                point[basic_column] = tableau[row_index, -1]
        return point

    phase_two_cost = np.zeros(total_columns)
    phase_two_cost[:original_count] = objective
    tableau[objective_row, :total_columns] = phase_two_cost
    for row_index, basic_column in enumerate(basis):
        if 0 <= basic_column < original_count:
            tableau[objective_row, :] -= (
                objective[basic_column] * tableau[row_index, :]
            )
    _optimise(
        tableau,
        basis,
        pivot_tolerance,
        allow_unbounded=True,
        entering_limit=first_artificial,
    )

    point = np.zeros(original_count)
    for row_index, basic_column in enumerate(basis):
        if 0 <= basic_column < original_count:
            point[basic_column] = tableau[row_index, -1]
    return point


def _optimise(tableau, basis, tolerance, allow_unbounded, entering_limit):
    row_count = tableau.shape[0] - 1
    objective_row = row_count
    while True:
        reduced_costs = tableau[objective_row, :entering_limit]
        entering = next(
            (column for column in range(entering_limit)
             if reduced_costs[column] < -tolerance),
            None,
        )
        if entering is None:
            return
        ratios = []
        for row_index in range(row_count):
            coefficient = tableau[row_index, entering]
            if coefficient > tolerance:
                ratios.append(
                    (tableau[row_index, -1] / coefficient, row_index)
                )
        if not ratios:
            if allow_unbounded:
                raise _Unbounded("objective is unbounded")
            return
        ratios.sort(key=lambda item: (item[0], basis[item[1]]))
        _pivot(tableau, basis, ratios[0][1], entering)


def _pivot(tableau, basis, leaving, entering):
    tableau[leaving, :] /= tableau[leaving, entering]
    for row_index in range(tableau.shape[0]):
        if row_index == leaving:
            continue
        factor = tableau[row_index, entering]
        if factor != 0.0:
            tableau[row_index, :] -= factor * tableau[leaving, :]
    basis[leaving] = entering


def observations_from_las(las_file, probe_windows, depth_mnemonic="DEPT"):
    """Build :class:`Observation` lists from curves of a LAS file.

    Each curve mnemonic in ``probe_windows`` is treated as one probe.  LAS
    NULL values are returned as NaN by lasio and surface here as missing
    observations instead of being treated as measurements.
    """

    import lasio

    log = lasio.read(las_file) if isinstance(las_file, str) else las_file
    if depth_mnemonic not in log.curves.keys():
        raise BedCompatibilityError(
            f"depth curve {depth_mnemonic!r} not found in LAS file"
        )
    depths = np.asarray(log.index, dtype=float)

    grouped = {}
    for probe, spec in probe_windows.items():
        if probe not in log.curves.keys():
            raise BedCompatibilityError(
                f"probe curve {probe!r} not found in LAS file"
            )
        values = np.asarray(log[probe], dtype=float)
        grouped[probe] = [
            Observation(
                probe=probe,
                depth=float(depth),
                value=None if math.isnan(value) else float(value),
                tolerance=spec.tolerance,
            )
            for depth, value in zip(depths, values)
        ]
    return grouped


def _load_json_config(path):
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    windows = {}
    for probe, settings in config["probes"].items():
        if "half_height" in settings:
            windows[probe] = WindowSpec.symmetric(
                probe,
                settings["half_height"],
                settings["tolerance"],
            )
        else:
            windows[probe] = WindowSpec(
                probe,
                settings["top_offset"],
                settings["bottom_offset"],
                settings["tolerance"],
            )
    observations = [
        Observation(
            probe=entry["probe"],
            depth=float(entry["depth"]),
            value=(None if entry.get("value") is None else float(entry["value"])),
            tolerance=(
                None
                if entry.get("tolerance") is None
                else float(entry["tolerance"])
            ),
        )
        for entry in config["observations"]
    ]
    return config, windows, observations


def main(argv=None):
    """Command line entry point: ``python -m lasio.bed_compatibility config.json``."""

    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m lasio.bed_compatibility <config.json>", file=sys.stderr)
        return 2
    config, windows, observations = _load_json_config(argv[0])
    report = check_bed_compatibility(
        beds=config["beds"],
        windows=windows,
        observations=observations,
        value_bounds=config.get("value_bounds"),
        strict_support=config.get("strict_support", True),
    )
    print(report.to_json())
    if report.status == "feasible":
        return 0
    if report.status == "infeasible":
        return 1
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
