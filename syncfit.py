"""
Fit old-print vs. remastered-print subtitle timing from confirmed line-up
points (anchor points), and decide whether the drift is explained by a
uniform speed difference or by mid-roll insertions ("片花") that cause a
time jump.

Two models are supported:

* A **linear** model assumes one constant play speed over the whole film::

      remastered = slope * old + intercept

* A **piecewise** model assumes the play speed is uniform inside each
  segment, but an insertion of ``d`` seconds at an old-print position
  ``p`` shifts every later subtitle by ``d``::

      remastered = slope * old + intercept
                  + sum(d for p, d in inserts if p < old)

Every anchor point carries a measurement tolerance.  A model is accepted
when every point's residual is within its tolerance, and its slope stays
inside the speed range given by the archivist.  All times are handled in
milliseconds unless noted.

The feasibility problems are linear programs, solved by a small two-phase
simplex implementation in this module, so the standard library alone is
required.
"""

from __future__ import division

import itertools
import re

EPS = 1e-9


class Anchor(object):
    """A confirmed line-up point between the two prints.

    ``old`` and ``remastered`` are the same subtitle's times in the old
    print and the remastered print, in milliseconds.  ``tolerance`` is the
    measurement error in milliseconds: the modelled remastered time must
    lie within ``tolerance`` of the measured one.  ``label`` optionally
    identifies the subtitle (its SRT index or first line).
    """

    __slots__ = ("old", "remastered", "tolerance", "label")

    def __init__(self, old, remastered, tolerance, label=""):
        self.old = float(old)
        self.remastered = float(remastered)
        self.tolerance = float(tolerance)
        self.label = label

    def __repr__(self):
        return "Anchor(old={!r}, remastered={!r}, tolerance={!r}, label={!r})".format(
            self.old, self.remastered, self.tolerance, self.label
        )


class FitResult(object):
    """The feasible parameter range for one model.

    For the linear model ``inserts`` is empty.  For a piecewise model each
    required insert is a ``(position, (duration_low, duration_high))``
    pair whose minimum length is positive.  ``optional_gaps`` lists other
    anchor gaps, as ``(position, max_duration)`` pairs, where an extra
    insertion of up to ``max_duration`` milliseconds would still fit every
    anchor alongside the required inserts; these are the positions at which
    the two explanations are observationally indistinguishable.  When no
    parameter combination satisfies every anchor, ``feasible`` is
    ``False`` and the range fields are ``None``.
    """

    __slots__ = (
        "kind",
        "feasible",
        "slope_range",
        "intercept_range",
        "inserts",
        "optional_gaps",
        "anchors",
        "_rows",
        "_n_inserts",
        "scale",
    )

    def __init__(
        self, kind, feasible, slope_range=None, intercept_range=None,
        inserts=None, optional_gaps=None, anchors=None, rows=None,
        n_inserts=0, scale=1.0,
    ):
        self.kind = kind
        self.feasible = feasible
        self.slope_range = slope_range
        self.intercept_range = intercept_range
        self.inserts = inserts or ()
        self.optional_gaps = optional_gaps or ()
        self.anchors = anchors or ()
        self._rows = rows
        self._n_inserts = n_inserts
        self.scale = scale

    def __repr__(self):
        return (
            "FitResult(kind={!r}, feasible={!r}, slope_range={!r}, "
            "intercept_range={!r}, inserts={!r})".format(
                self.kind, self.feasible, self.slope_range,
                self.intercept_range, self.inserts,
            )
        )

    def objective_interval(self, coefficients):
        """Exact ``(low, high)`` of a linear objective over the feasible set.

        ``coefficients`` has one entry per model variable in millisecond
        units: slope, intercept, then one per insertion duration.  The
        internal solver works on scaled, order-one variables, so the
        intercept/duration coefficients are converted here.
        """
        if not self.feasible:
            raise ValueError("cannot evaluate an infeasible fit")
        coefficients = list(coefficients)
        scale = self.scale
        scaled = [coefficients[0]] + [
            coef * scale for coef in coefficients[1:]
        ]
        low = simplex(self._rows, scaled, want_max=False)
        high = simplex(self._rows, scaled, want_max=True)
        return low, high


def _normalise(anchors):
    """Sort anchors by old-print time and reject unusable input."""
    anchors = sorted(anchors, key=lambda anchor: anchor.old)
    if len(anchors) < 2:
        raise ValueError("at least two anchor points are required")
    for anchor in anchors:
        if anchor.tolerance < 0:
            raise ValueError("tolerance must be non-negative: %r" % anchor)
    previous = None
    for anchor in anchors:
        if previous is not None and anchor.old <= previous.old:
            raise ValueError(
                "anchor points must have distinct, ordered old-print times"
            )
        previous = anchor
    return anchors


def _time_scale(anchors):
    """Order-one divisor that keeps tableau coefficients well conditioned.

    Times enter in milliseconds and span tens of thousands, while the
    intercept is naturally small.  Dividing every time-like quantity by
    this scale keeps the matrix entries near one without changing the
    slope variable.
    """
    span = max(anchor.old for anchor in anchors)
    return span if span > 1.0 else 1.0


def _constraint_rows(anchors, slope_limits, structure=(), scale=1.0):
    """Build the ``A x <= b`` system for one model, in scaled units.

    Variable order: slope, scaled intercept, then one scaled insertion
    duration per insert.  Each anchor contributes a lower and an upper
    bound; the archivist's speed range adds two explicit slope
    constraints.
    """
    rows = []
    n_inserts = len(structure)
    for anchor in anchors:
        old = anchor.old / scale
        target = anchor.remastered / scale
        tolerance = anchor.tolerance / scale
        active = [
            idx
            for idx, (position, _bounds) in enumerate(structure)
            if position < anchor.old
        ]
        base = [old, 1.0] + [
            1.0 if idx in active else 0.0 for idx in range(n_inserts)
        ]
        rows.append((list(base), target + tolerance))
        rows.append(
            ([-coef for coef in base], tolerance - target)
        )
    slope_low, slope_high = slope_limits
    slope_row = [1.0, 0.0] + [0.0] * n_inserts
    rows.append((list(slope_row), slope_high))
    rows.append(([-coef for coef in slope_row], -slope_low))
    # Insertion durations represent added material, so they are non-negative.
    for idx in range(n_inserts):
        non_negative = [0.0] * (2 + n_inserts)
        non_negative[2 + idx] = -1.0
        rows.append((non_negative, 0.0))
    return rows


def simplex(rows, objective, want_max=False):
    """Two-phase simplex for ``A x <= b`` with free variables.

    Returns the optimum value, ``None`` when the feasible region is empty,
    or a signed infinity when the objective is unbounded.  ``rows`` is a
    sequence of ``(coefficients, bound)`` pairs.
    """
    solution = _simplex_solution(rows, objective, want_max)
    if solution is None or isinstance(solution, _Unbounded):
        if isinstance(solution, _Unbounded):
            return float("inf") if want_max else float("-inf")
        return None
    return sum(coef * val for coef, val in zip(objective, solution))


def _simplex_solution(rows, objective, want_max=False):
    """Like :func:`simplex`, but returns the optimal variable vector.

    Returns ``None`` for an infeasible region and :data:`UNBOUNDED` when
    the objective is unbounded.
    """
    n_vars = len(objective)
    # Minimisation is maximisation of the negated objective.
    cost = [coef if want_max else -coef for coef in objective]

    # Free variables are split into x+ - x-, both non-negative.
    n_split = 2 * n_vars
    m_rows = len(rows)
    artificials = []
    tableau = []
    basis = []

    for row_idx, (coeffs, bound) in enumerate(rows):
        line = [0.0] * (n_split + 2 * m_rows + 1)
        for idx, coef in enumerate(coeffs):
            line[2 * idx] = coef
            line[2 * idx + 1] = -coef
        slack_col = n_split + row_idx
        line[slack_col] = 1.0
        if bound < -EPS:
            # Normalise to a positive RHS and introduce an artificial.
            line = [-value for value in line]
            artificial_col = n_split + m_rows + len(artificials)
            line[artificial_col] = 1.0
            artificials.append(artificial_col)
            basis.append((artificial_col, row_idx))
        else:
            basis.append((slack_col, row_idx))
        line[-1] = abs(bound)
        tableau.append(line)

    n_cols = n_split + m_rows + len(artificials)
    for line in tableau:
        del line[n_cols:-1]

    if artificials:
        # Maximise -sum(artificial); optimum 0 means a feasible start.
        phase_one = [0.0] * (n_cols + 1)
        for artificial_col, row_idx in basis:
            if artificial_col in artificials:
                for col in range(n_cols + 1):
                    phase_one[col] -= tableau[row_idx][col]
        _pivot_to_optimal(tableau, basis, phase_one, artificials, n_cols)
        if phase_one[-1] < -1e-7 * _scale(tableau):
            return None
        _drive_artificials_out(tableau, basis, artificials, n_cols)

    # Objective row in z - c.x = 0 form: variable columns hold -cost, so a
    # negative coefficient marks an improving direction for maximisation.
    objective_row = [0.0] * (n_cols + 1)
    for idx, coef in enumerate(cost):
        objective_row[2 * idx] = -coef
        objective_row[2 * idx + 1] = coef
    for col, row_idx in basis:
        if col >= n_split:
            continue
        pivot_value = objective_row[col]
        if pivot_value:
            for jdx in range(n_cols + 1):
                objective_row[jdx] -= pivot_value * tableau[row_idx][jdx]

    if _pivot_to_optimal(
        tableau, basis, objective_row, artificials, n_cols
    ) is UNBOUNDED:
        return UNBOUNDED

    values = [0.0] * n_split
    for col, row_idx in basis:
        if col < n_split:
            values[col] = tableau[row_idx][-1]
    return [
        values[2 * idx] - values[2 * idx + 1] for idx in range(n_vars)
    ]


def _scale(tableau):
    return max(
        (abs(value) for line in tableau for value in line),
        default=1.0,
    ) or 1.0


def _drive_artificials_out(tableau, basis, artificials, n_cols):
    """Replace zero-valued basic artificials with real variables.

    At a degenerate optimum an artificial can remain basic with value zero.
    Pivoting on any non-artificial, non-zero entry of its row preserves
    feasibility (the RHS stays zero) and yields a basis without
    artificials, which phase two requires.  A row that is all zero outside
    the artificial column is redundant and is left untouched.
    """
    artificial_set = set(artificials)
    for row_idx, (basic_col, _row) in enumerate(list(basis)):
        if basic_col not in artificial_set or tableau[row_idx][-1] > EPS:
            continue
        entering = next(
            (
                col
                for col in range(n_cols)
                if col not in artificial_set
                and abs(tableau[row_idx][col]) > EPS
            ),
            None,
        )
        if entering is not None:
            _pivot(
                tableau,
                basis,
                [0.0] * (n_cols + 1),
                entering,
                row_idx,
                n_cols,
            )


def _pivot_to_optimal(tableau, basis, objective_row, artificials, n_cols):
    """Bland-rule pivoting.  Returns :data:`UNBOUNDED` when unbounded."""
    tolerance = 1e-9 * max(1.0, _scale(tableau))
    while True:
        entering = None
        for col in range(n_cols):
            if col in artificials:
                continue
            if objective_row[col] < -tolerance:
                entering = col
                break
        if entering is None:
            return None
        leaving = None
        best_ratio = float("inf")
        best_basic_col = float("inf")
        for row_idx, line in enumerate(tableau):
            if line[entering] <= tolerance:
                continue
            ratio = line[-1] / line[entering]
            basic_col = basis[row_idx][0]
            if ratio < best_ratio - tolerance or (
                abs(ratio - best_ratio) <= tolerance
                and basic_col < best_basic_col
            ):
                best_ratio = ratio
                best_basic_col = basic_col
                leaving = row_idx
        if leaving is None:
            return UNBOUNDED
        _pivot(tableau, basis, objective_row, entering, leaving, n_cols)


class _Unbounded(object):
    """Sentinel returned when an objective is unbounded."""


UNBOUNDED = _Unbounded()


def _pivot(tableau, basis, objective_row, entering, leaving, n_cols):
    pivot_value = tableau[leaving][entering]
    for col in range(n_cols + 1):
        tableau[leaving][col] /= pivot_value
    for row_idx, line in enumerate(tableau):
        if row_idx == leaving:
            continue
        factor = line[entering]
        if abs(factor) < EPS:
            continue
        for col in range(n_cols + 1):
            line[col] -= factor * tableau[leaving][col]
    factor = objective_row[entering]
    for col in range(n_cols + 1):
        objective_row[col] -= factor * tableau[leaving][col]
    basis[leaving] = (entering, leaving)


def solve_linear_system(matrix, rhs):
    """Gaussian elimination with partial pivoting.

    Solves ``matrix x = rhs``; returns ``None`` when the matrix is
    singular.  Exposed mainly for cross-checking :func:`simplex` against
    direct vertex enumeration in tests.
    """
    n_vars = len(rhs)
    augmented = [list(matrix[row]) + [rhs[row]] for row in range(n_vars)]
    for col in range(n_vars):
        pivot = max(range(col, n_vars), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < EPS:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        for row in range(col + 1, n_vars):
            factor = augmented[row][col] / pivot_value
            if factor:
                for jdx in range(col, n_vars + 1):
                    augmented[row][jdx] -= factor * augmented[col][jdx]
    solution = [0.0] * n_vars
    for row in range(n_vars - 1, -1, -1):
        total = augmented[row][n_vars] - sum(
            augmented[row][col] * solution[col]
            for col in range(row + 1, n_vars)
        )
        solution[row] = total / augmented[row][row]
    return solution


def _ranges_for_structure(rows, n_inserts):
    """Feasible scaled ``(low, high)`` of every model variable."""
    n_vars = 2 + n_inserts
    lows = []
    highs = []
    for idx in range(n_vars):
        objective = [0.0] * n_vars
        objective[idx] = 1.0
        low = simplex(rows, objective, want_max=False)
        high = simplex(rows, objective, want_max=True)
        if low is None or high is None:
            return None
        lows.append(low)
        highs.append(high)
    return lows, highs


def _fit_structure_scaled(anchors, slope_limits, structure):
    """Build and solve one structure, returning millisecond-scale results.

    Returns ``None`` when infeasible, otherwise a
    ``(rows, scale, lows_ms, highs_ms)`` tuple where variable zero is the
    slope (dimensionless) and the remaining variables are in milliseconds.
    """
    scale = _time_scale(anchors)
    rows = _constraint_rows(anchors, slope_limits, structure, scale)
    ranges = _ranges_for_structure(rows, len(structure))
    if ranges is None:
        return None
    lows, highs = ranges
    lows_ms = [lows[0]] + [value * scale for value in lows[1:]]
    highs_ms = [highs[0]] + [value * scale for value in highs[1:]]
    return rows, scale, lows_ms, highs_ms


def solve_linear(anchors, slope_limits):
    """Return the feasible ``(slope, intercept)`` range for a uniform fit.

    ``slope_limits`` is the allowed ``(low, high)`` play-speed ratio
    (remastered milliseconds per old millisecond).  The result is a
    :class:`FitResult` whose ``slope_range`` and ``intercept_range`` give
    the exact feasible interval of each parameter.
    """
    anchors = _normalise(anchors)
    fitted = _fit_structure_scaled(anchors, slope_limits, ())
    if fitted is None:
        rows = _constraint_rows(
            anchors, slope_limits, (), _time_scale(anchors)
        )
        return FitResult(
            "linear", False, anchors=tuple(anchors), rows=rows,
            scale=_time_scale(anchors),
        )
    rows, scale, lows, highs = fitted
    return FitResult(
        "linear",
        True,
        (lows[0], highs[0]),
        (lows[1], highs[1]),
        anchors=tuple(anchors),
        rows=rows,
        scale=scale,
    )


def _candidate_positions(anchors):
    """Insertion positions that change which anchors are affected.

    A jump only matters at an old-print time inside a gap between two
    consecutive anchors; every position in one open gap produces the same
    feasible region, so the gap midpoint represents it.
    """
    return [
        (left.old + right.old) / 2
        for left, right in zip(anchors, anchors[1:])
    ]


def solve_structure(anchors, slope_limits, gap_positions):
    """Fit one explicit insertion structure.

    ``gap_positions`` is a sequence of old-print positions (milliseconds)
    at which an insertion is assumed.  Returns a :class:`FitResult` whose
    inserts carry the full feasible duration range of each gap, or
    ``None`` when the structure cannot satisfy every anchor.
    """
    anchors = _normalise(anchors)
    structure = tuple((position, None) for position in gap_positions)
    fitted = _fit_structure_scaled(anchors, slope_limits, structure)
    if fitted is None:
        return None
    rows, scale, lows, highs = fitted
    inserts = tuple(
        (structure[idx][0], (lows[2 + idx], highs[2 + idx]))
        for idx in range(len(structure))
    )
    return FitResult(
        "piecewise",
        True,
        (lows[0], highs[0]),
        (lows[1], highs[1]),
        inserts=inserts,
        anchors=tuple(anchors),
        rows=rows,
        n_inserts=len(structure),
        scale=scale,
    )


def solve_piecewise(anchors, slope_limits, max_inserts):
    """Find distinct mid-roll insertion explanations, up to ``max_inserts``.

    Every gap between consecutive anchors is tried as an insertion site.
    Results are grouped by the set of **required** insertions, i.e. gaps
    whose feasible minimum duration is positive; a gap whose minimum is
    zero does not define a separate explanation, because the no-jump
    solution still fits.  Each result additionally lists
    ``optional_gaps``: sites where an insertion (up to the reported
    duration) could be added without breaking any anchor, which are the
    points at which the speed-only and insertion explanations are
    indistinguishable.

    The first result always has no required inserts and corresponds to the
    uniform-speed explanation.  The search is combinatorial in
    ``max_inserts``, which is intended to stay small (the archivist's
    expected number of interruptions).
    """
    anchors = _normalise(anchors)
    if max_inserts < 0:
        raise ValueError("max_inserts must be non-negative")

    positions = _candidate_positions(anchors)
    limit = min(max_inserts, len(positions))

    # Cache the feasible ranges (milliseconds) of every gap subset.
    cache = {}
    for count in range(limit + 1):
        for chosen in itertools.combinations(range(len(positions)), count):
            structure = tuple((positions[idx], None) for idx in chosen)
            fitted = _fit_structure_scaled(anchors, slope_limits, structure)
            if fitted is None:
                cache[chosen] = None
            else:
                _rows, _scale, lows_ms, highs_ms = fitted
                cache[chosen] = (lows_ms, highs_ms)

    required_sets = set()
    for chosen, ranges in cache.items():
        if ranges is None:
            continue
        lows, _highs = ranges
        required = tuple(
            gap_idx
            for var_idx, gap_idx in enumerate(chosen)
            if lows[2 + var_idx] > EPS
        )
        required_sets.add(required)

    results = []
    for required in sorted(required_sets, key=lambda key: (len(key), key)):
        result = solve_structure(
            anchors, slope_limits, [positions[idx] for idx in required]
        )
        optional_gaps = []
        for gap_idx, position in enumerate(positions):
            if gap_idx in required:
                continue
            candidate = tuple(sorted(required + (gap_idx,)))
            if len(candidate) > limit or cache.get(candidate) is None:
                continue
            candidate_ranges = cache[candidate]
            var_idx = candidate.index(gap_idx)
            max_duration = candidate_ranges[1][2 + var_idx]
            if max_duration > EPS:
                optional_gaps.append((position, max_duration))
        result.optional_gaps = tuple(optional_gaps)
        results.append(result)
    return results


def evaluate_models(anchors, slope_limits, max_inserts):
    """Fit both models and return ``(linear_result, piecewise_results)``."""
    return (
        solve_linear(anchors, slope_limits),
        solve_piecewise(anchors, slope_limits, max_inserts),
    )


def insertion_alternatives(
    anchors, slope_limits, max_inserts, min_insert_ms=0.0
):
    """Feasible fits that contain at least one real insertion.

    Enumerates gap subsets up to ``max_inserts`` and returns one
    :class:`FitResult` per subset whose feasible region permits every
    chosen insertion a length of at least ``min_insert_ms``.  These are
    the concrete "insertion" explanations to compare against a surviving
    uniform-speed explanation.
    """
    anchors = _normalise(anchors)
    positions = _candidate_positions(anchors)
    limit = min(max_inserts, len(positions))
    alternatives = []
    for count in range(1, limit + 1):
        for chosen in itertools.combinations(range(len(positions)), count):
            result = solve_structure(
                anchors,
                slope_limits,
                [positions[idx] for idx in chosen],
            )
            if result is None:
                continue
            if all(
                duration_range[1] >= min_insert_ms - EPS
                for _position, duration_range in result.inserts
            ):
                alternatives.append(result)
    return alternatives


def prediction_interval(result, old_time):
    """Exact feasible ``(low, high)`` remastered-time interval (ms).

    The interval is obtained by minimising/maximising the prediction as a
    linear objective over the model's feasible region, so it stays exact
    even when the parameters are correlated.
    """
    coefficients = [old_time, 1.0]
    for position, _bounds in result.inserts:
        coefficients.append(1.0 if position < old_time else 0.0)
    return result.objective_interval(coefficients)


def predict(result, old_time):
    """Midpoint of :func:`prediction_interval`, in milliseconds."""
    low, high = prediction_interval(result, old_time)
    return (low + high) / 2


def insertion_displacement(result, old_time):
    """Feasible ``(low, high)`` jump displacement at an old-print time.

    This is the sum of insertion durations active at ``old_time`` (inserts
    positioned before it), independent of slope/intercept.  It is zero for
    subtitles that precede every insertion.
    """
    active = [
        duration_range
        for position, duration_range in result.inserts
        if position < old_time
    ]
    return (
        sum(bounds[0] for bounds in active),
        sum(bounds[1] for bounds in active),
    )


def diverging_subtitles(
    linear_result, piecewise_result, subtitles, min_gap=0.0
):
    """Subtitles placed differently by the speed-only and insertion fits.

    When both explanations pass the anchors, the speed-only solution is one
    point of the insertion model's feasible region, so the two prediction
    intervals always overlap.  The substantive disagreement is therefore
    the *extra* displacement the insertion explanation allows on subtitles
    after its jump points: :func:`insertion_displacement`.  A subtitle is
    yielded when that extra displacement exceeds ``min_gap`` milliseconds.

    ``subtitles`` is an iterable of ``(old_start_ms, label)`` pairs.
    Yields ``(label, old_time, linear_interval, piecewise_interval,
    extra_displacement)``; ``linear_interval`` is ``None`` when the
    speed-only model is itself infeasible.
    """
    for old_time, label in subtitles:
        piece_interval = prediction_interval(piecewise_result, old_time)
        extra_low, extra_high = insertion_displacement(
            piecewise_result, old_time
        )
        if extra_high <= min_gap + EPS:
            continue
        if linear_result.feasible:
            linear_interval = prediction_interval(linear_result, old_time)
        else:
            linear_interval = None
        yield (
            label,
            old_time,
            linear_interval,
            piece_interval,
            (extra_low, extra_high),
        )


def anchor_residuals(result):
    """``(anchor, residual_low, residual_high)`` against the feasible set.

    Both bounds are zero-width-compatible residuals: the smallest absolute
    distance achievable at each anchor inside the feasible region, split by
    sign.  Values within ``[-tolerance, tolerance]`` mean the anchor is
    satisfied, which holds by construction for any feasible result.
    """
    for anchor in result.anchors:
        low, high = prediction_interval(result, anchor.old)
        yield anchor, low - anchor.remastered, high - anchor.remastered


def _parse_time_value(token):
    """Parse a time as milliseconds.

    Accepts a bare number (interpreted as seconds) or an SRT-style
    ``HH:MM:SS,mmm`` timestamp when the :mod:`srt` library is importable.
    """
    token = token.strip()
    if ":" in token:
        try:
            import srt
        except ImportError:
            raise ValueError(
                "SRT timestamps require the srt library: %r" % token
            )
        delta = srt.srt_timestamp_to_timedelta(token)
        return (
            delta.days * 86400000
            + delta.seconds * 1000
            + delta.microseconds / 1000.0
        )
    return float(token) * 1000.0


def parse_anchor_lines(lines):
    """Parse anchor points from tab/comma/whitespace separated lines.

    Each non-empty, non-comment line is
    ``old  remastered  tolerance  [label]``.  Times may be seconds or SRT
    timestamps (``HH:MM:SS,mmm``); tolerance is always in milliseconds.
    Blank lines and lines starting with ``#`` are ignored.
    """
    anchors = []
    timestamp = re.compile(r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}")
    token_pattern = re.compile(r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\S+")
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Commas separate columns, but an SRT timestamp's own comma must be
        # preserved, so whole timestamps are matched before generic splitting.
        fields = []
        for token in token_pattern.findall(line.replace("\t", " ")):
            if timestamp.fullmatch(token):
                fields.append(token)
            else:
                fields.extend(part for part in token.split(",") if part)
        if len(fields) < 3:
            raise ValueError(
                "line %d needs old, remastered and tolerance columns: %r"
                % (line_number, raw_line)
            )
        old = _parse_time_value(fields[0])
        remastered = _parse_time_value(fields[1])
        tolerance = float(fields[2])
        label = " ".join(fields[3:]) if len(fields) > 3 else "anchor-%d" % (
            line_number
        )
        anchors.append(Anchor(old, remastered, tolerance, label))
    return anchors


def divergence_by_model(
    linear_result, insertion_models, subtitles, min_insert_ms=0.0
):
    """Per insertion-model list of subtitles it moves relative to speed-only.

    Returns a list of ``(model, affected_rows)`` pairs, where each row is a
    ``(label, old_time, linear_interval, piecewise_interval,
    extra_displacement)`` tuple from :func:`diverging_subtitles`, keeping
    only models that move at least one subtitle by ``min_insert_ms`` or
    more.  Models with the same single jump position and identical effect
    are de-duplicated.
    """
    seen = set()
    blocks = []
    for model in insertion_models:
        rows = list(
            diverging_subtitles(
                linear_result, model, subtitles, min_insert_ms
            )
        )
        if not rows:
            continue
        signature = tuple(
            (round(position, 6), tuple(label for label, *_ in rows))
            for position, _bounds in model.inserts
        )
        if signature in seen:
            continue
        seen.add(signature)
        blocks.append((model, rows))
    return blocks


def diagnose(
    anchors,
    slope_limits,
    max_inserts,
    subtitles=None,
    min_insert_ms=0.0,
):
    """Run the full speed-vs-insertion comparison.

    ``min_insert_ms`` is the smallest insertion length (milliseconds) that
    counts as a real insertion rather than measurement noise.  Returns a
    dict with keys:

    * ``linear``: the uniform-speed :class:`FitResult`;
    * ``explanations``: distinct piecewise :class:`FitResult` objects;
    * ``insertion_models``: fits supporting an insertion of at least
      ``min_insert_ms``;
    * ``ambiguous``: ``True`` when both the speed-only model and at least
      one such insertion model satisfy every anchor;
    * ``diverging``: subtitles the two surviving explanations place at
      different times, when ``subtitles`` is supplied.
    """
    linear = solve_linear(anchors, slope_limits)
    explanations = solve_piecewise(anchors, slope_limits, max_inserts)
    insertion_models = insertion_alternatives(
        anchors, slope_limits, max_inserts, min_insert_ms
    )
    ambiguous = linear.feasible and bool(insertion_models)
    divergence_blocks = []
    if subtitles is not None and insertion_models:
        divergence_blocks = divergence_by_model(
            linear, insertion_models, subtitles, min_insert_ms
        )
    return {
        "linear": linear,
        "explanations": explanations,
        "insertion_models": insertion_models,
        "ambiguous": ambiguous,
        "diverging": divergence_blocks[0][1] if divergence_blocks else [],
        "divergence_blocks": divergence_blocks,
    }
