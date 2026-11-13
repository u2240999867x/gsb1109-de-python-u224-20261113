"""Audit numeric values that may be either a null sentinel or a real reading.

A LAS file can use a numeric value such as ``-99`` for missing samples while a
sensor whose calibrated range includes that value can also record it for real.
The audit in this module decides per curve and per sample:

* ``missing`` -- the real-measurement explanation is excluded, so the sample is
  replaced with ``numpy.nan`` (when ``apply=True``);
* ``measured`` -- the value is inside the curve's calibrated range and, when a
  backup is available, agrees with it within the stated error;
* ``ambiguous`` -- both explanations remain possible. The original value is
  kept and the sample is reported as a doubt.

Evidence is read from the curve declaration, from curve-specific entries in the
``~P`` section, and can also be supplied explicitly with ``specs``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

MISSING = "missing"
MEASURED = "measured"
AMBIGUOUS = "ambiguous"

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER_RE = re.compile(_NUMBER)

_TEMPERATURE_UNITS = {
    "C",
    "CELSIUS",
    "DEGC",
    "DEG C",
    "DEG. C",
    "DEGC.",
    "F",
    "FAHRENHEIT",
    "DEGF",
    "DEG F",
    "DEG. F",
    "K",
    "KELVIN",
}

_TEMPERATURE_TOKENS = (
    "TEMP",
    "TEMPERATURE",
    "BHT",
    "BOTTOM HOLE TEMP",
    "DHT",
    "DOWNHOLE TEMP",
    "RMT",
    "MUD TEMP",
    "TMAX",
    "TVDT",
    "THERM",
)

_RANGE_KEYWORDS = (
    "RANGE",
    "VALID",
    "CAL",
    "CALIB",
    "CALIBRATION",
    "LIMIT",
    "SCALE",
    "SPAN",
)

_RANGE_SUFFIXES = (
    "_CAL",
    "_CALIB",
    "_CALIBRATION",
    "_CALRANGE",
    "_CALRNG",
    "_RANGE",
    "_RNG",
    "_VALID",
    "_VALIDRANGE",
    "_VALID_RANGE",
    "_VALIDRNG",
    "_LIMIT",
    "_LIMITS",
    "_SCALE",
)

_RANGE_PREFIXES = (
    "CAL_",
    "CALIB_",
    "CALIBRATION_",
    "CALRANGE_",
    "CALRNG_",
    "RANGE_",
    "VALID_",
    "LIMIT_",
    "SCALE_",
)

_MINMAX_SUFFIXES = {
    "min": (
        "_MIN",
        "_MINV",
        "_MINVAL",
        "_MINIMUM",
        "_LOW",
        "_LO",
        "_CAL_MIN",
        "_CAL_LOW",
        "_VALID_MIN",
        "_RANGE_MIN",
    ),
    "max": (
        "_MAX",
        "_MAXV",
        "_MAXVAL",
        "_MAXIMUM",
        "_HIGH",
        "_HI",
        "_CAL_MAX",
        "_CAL_HIGH",
        "_VALID_MAX",
        "_RANGE_MAX",
    ),
}

_MINMAX_PREFIXES = {
    "min": ("MIN_", "MINV_", "LOW_", "LO_", "CAL_MIN_", "VALID_MIN_", "RANGE_MIN_"),
    "max": ("MAX_", "MAXV_", "HIGH_", "HI_", "CAL_MAX_", "VALID_MAX_", "RANGE_MAX_"),
}

_BACKUP_SUFFIXES = (
    "_BACKUP",
    "_BUP",
    "_BK",
    "_BKP",
    "_SPARE",
    "_REDUNDANT",
    "_REDUNDANCY",
    "_DUAL",
    "_PAIR",
    "_PAIRED",
)

_BACKUP_PREFIXES = (
    "BACKUP_",
    "BUP_",
    "BK_",
    "BKP_",
    "SPARE_",
    "REDUNDANT_",
    "DUAL_",
    "PAIR_",
)

_ERROR_SUFFIXES = (
    "_ERR",
    "_ERROR",
    "_ACC",
    "_ACCURACY",
    "_TOL",
    "_TOLERANCE",
    "_PRECISION",
    "_UNC",
    "_UNCERTAINTY",
)

_ERROR_PREFIXES = (
    "ERR_",
    "ERROR_",
    "ACC_",
    "ACCURACY_",
    "TOL_",
    "TOLERANCE_",
)

_RUN_SUFFIXES = ("_RUN", "_PASS")
_RUN_PREFIXES = ("RUN_", "PASS_")

_SENTINEL_SUFFIXES = ("_NULL", "_NULLVALUE", "_SENTINEL", "_MISSING")
_SENTINEL_PREFIXES = ("NULL_", "SENTINEL_", "MISSING_")


def _norm(value):
    return str(value if value is not None else "").strip().upper()


def _strip_duplicate_suffix(mnemonic):
    mnemonic = _norm(mnemonic)
    return mnemonic.split(":", 1)[0]


def _mnemonic_variants(mnemonic):
    base = _norm(mnemonic)
    variants = {base, _strip_duplicate_suffix(base)}
    variants.add(base.replace(":", "_"))
    variants.add(_strip_duplicate_suffix(base).replace(" ", "_"))
    return {v for v in variants if v}


def _is_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _as_float(value):
    if isinstance(value, (int, float, np.integer, np.floating)):
        if np.isfinite(value):
            return float(value)
        return None
    match = _NUMBER_RE.search(str(value or ""))
    if match:
        return float(match.group(0))
    return None


def _numbers(text):
    return [float(match) for match in _NUMBER_RE.findall(str(text or ""))]


def _parse_range(value):
    """Return a ``(low, high)`` tuple from common range representations."""
    if value is None:
        return None
    if isinstance(value, dict):
        low = value.get("min", value.get("low", value.get("lo")))
        high = value.get("max", value.get("high", value.get("hi")))
        return _ordered_range(_as_float(low), _as_float(high))
    if isinstance(value, (tuple, list, np.ndarray)):
        values = list(value)
        if len(values) >= 2:
            return _ordered_range(_as_float(values[0]), _as_float(values[1]))
        return None

    text = str(value).strip()
    if not text:
        return None

    explicit_min = re.search(
        r"(?:MIN|LOW|LO)\s*[:=]?\s*(" + _NUMBER + r")", text, re.IGNORECASE
    )
    explicit_max = re.search(
        r"(?:MAX|HIGH|HI)\s*[:=]?\s*(" + _NUMBER + r")", text, re.IGNORECASE
    )
    if explicit_min and explicit_max:
        return _ordered_range(float(explicit_min.group(1)), float(explicit_max.group(1)))

    plain_bounds = re.match(
        r"^\s*(" + _NUMBER + r")\s*[–—-]\s*(" + _NUMBER + r")\s*$", text
    )
    if plain_bounds:
        return _ordered_range(
            float(plain_bounds.group(1)), float(plain_bounds.group(2))
        )

    parsed = _range_from_text(text)
    if parsed:
        return parsed

    parts = re.split(
        r"\s*(?:\.\.|,|;|\bTO\b|\bTHROUGH\b)\s*",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    if len(parts) == 2:
        low = _as_float(parts[0])
        high = _as_float(parts[1])
        if low is not None and high is not None:
            return _ordered_range(low, high)

    numbers = _numbers(text)
    if len(numbers) == 2:
        return _ordered_range(numbers[0], numbers[1])
    return None


def _ordered_range(low, high):
    if low is None or high is None:
        return None
    if low > high:
        logger.warning("Calibration range %s..%s has reversed bounds", low, high)
        low, high = high, low
    return (float(low), float(high))


def _parse_tolerance(value):
    """Return a non-negative absolute tolerance."""
    if value is None:
        return None
    if isinstance(value, (tuple, list, np.ndarray)):
        numbers = [_as_float(item) for item in value]
        numbers = [abs(number) for number in numbers if number is not None]
        if not numbers:
            return None
        if len(numbers) >= 2:
            return max(abs(numbers[1] - numbers[0]) / 2.0, max(numbers))
        return numbers[0]

    text = str(value).strip()
    if not text:
        return None
    numbers = _numbers(text)
    if not numbers:
        return None
    if "±" in text or "+/-" in text.lower():
        return abs(numbers[0])
    if len(numbers) >= 2:
        return max(abs(numbers[1] - numbers[0]) / 2.0, max(abs(n) for n in numbers))
    return abs(numbers[0])


def _parse_run(value):
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"RUN\s*[\[\(]?\s*([A-Za-z0-9_.-]+)", text, re.IGNORECASE)
    if match:
        return _norm(match.group(1))
    match = re.search(r"(" + _NUMBER + r")", text)
    if match:
        return _norm(match.group(1))
    return _norm(text) or None


def _runs_match(one, two):
    if one is None or two is None:
        return None
    return _norm(one) == _norm(two)


def _unit_is_temperature(unit):
    return _norm(unit).replace(".", "").strip() in _TEMPERATURE_UNITS


def _classify_curve(curve):
    mnemonic = _norm(curve.mnemonic)
    original = _norm(curve.original_mnemonic)
    descr = _norm(curve.descr)
    unit = _norm(curve.unit)
    text = " ".join((mnemonic, original, descr))
    if _unit_is_temperature(unit) or any(token in text for token in _TEMPERATURE_TOKENS):
        return "temperature"
    for kind, tokens in (
        ("gamma", ("GAMMA", "GR ", "GSGR")),
        ("caliper", ("CALIPER", "CALI", "DLCL", "ACCL")),
        ("density", ("DENSITY", "RHOB", "DLDN")),
        ("neutron", ("NEUTRON", "NPHI", "NCNPL")),
        ("resistivity", ("RESIST", "OHM", "ILD", "ILM", "SFLU")),
        ("porosity", ("POROSITY", "PHI")),
    ):
        if any(token in text for token in tokens):
            return kind
    return "unknown"


def _range_from_text(text):
    if not text:
        return None
    text = str(text)

    brace = re.search(
        r"\{\s*(?:RANGE|VALID|CAL|CALIB|CALIBRATION|LIMIT|SPAN)\b\s*[:=]?\s*"
        r"(" + _NUMBER + r")\s*(?:,|\.\.|TO|THROUGH|-|–|—)\s*(" + _NUMBER + r")",
        text,
        re.IGNORECASE,
    )
    if brace:
        return _ordered_range(float(brace.group(1)), float(brace.group(2)))

    keyword = re.search(
        r"(?:RANGE|VALID|CAL|CALIB|CALIBRATION|LIMIT|SCALE|SPAN)\b\s*[:=]?\s*"
        r"[\[\(]?\s*(" + _NUMBER + r")\s*(?:,|\.\.|TO|THROUGH|-|–|—)\s*"
        r"(" + _NUMBER + r")",
        text,
        re.IGNORECASE,
    )
    if keyword:
        return _ordered_range(float(keyword.group(1)), float(keyword.group(2)))

    minmax = re.search(
        r"(?:MIN|LOW|LO)\s*[:=]?\s*(" + _NUMBER + r").*?"
        r"(?:MAX|HIGH|HI)\s*[:=]?\s*(" + _NUMBER + r")",
        text,
        re.IGNORECASE,
    )
    if minmax:
        return _ordered_range(float(minmax.group(1)), float(minmax.group(2)))
    return None


def _backup_from_text(text):
    if not text:
        return None
    patterns = (
        r"(?:BACKUP|BUP|SPARE|REDUNDANT|PAIRED?)\s*(?:PROBE|SENSOR|CURVE|CHANNEL)?\s*[:=]\s*"
        r"([A-Za-z0-9_.:\[\]-]+)",
        r"(?:PAIRED?\s+WITH|DUAL\s+OF)\s+([A-Za-z0-9_.:\[\]-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, str(text), re.IGNORECASE)
        if match:
            return _norm(match.group(1))
    return None


def _backup_target_from_text(text):
    if not text:
        return None
    match = re.search(
        r"(?:BACKUP|BUP|SPARE|REDUNDANT)\s+FOR\s+([A-Za-z0-9_.:\[\]-]+)",
        str(text),
        re.IGNORECASE,
    )
    return _norm(match.group(1)) if match else None


def _tolerance_from_text(text):
    if not text:
        return None
    match = re.search(
        r"(?:ERROR|ERR|ACCURACY|ACC|TOLERANCE|TOL|PRECISION)\s*[:=]?\s*"
        r"(?:±|\+/-)?\s*(" + _NUMBER + r")",
        str(text),
        re.IGNORECASE,
    )
    if match:
        return abs(float(match.group(1)))
    return None


def _run_from_text(text):
    if not text:
        return None
    match = re.search(
        r"\bRUN\s*[\[\(=:]?\s*([A-Za-z0-9_.-]+)\s*[\]\)]?", str(text), re.IGNORECASE
    )
    return _norm(match.group(1)) if match else None


def _sentinel_from_text(text):
    if not text:
        return None
    match = re.search(
        r"(?:NULL|SENTINEL|MISSING)\s*(?:VALUE)?\s*[:=]?\s*(" + _NUMBER + r")",
        str(text),
        re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


@dataclass
class CurveSentinelSpec:
    """Evidence used to audit one curve.

    Attributes:
        curve (str): session mnemonic of the curve.
        calibration_range (tuple): inclusive ``(low, high)`` calibrated range.
        backup (str): mnemonic of the same-run backup probe.
        backup_error (float): absolute error of the backup probe.
        curve_error (float): absolute error of the audited curve.
        run (str): run/pass identifier.
        sentinel (float): per-curve sentinel value, if different from the
            audit's default.
        declared (str): declared sensor type e.g. ``"temperature"``.
        sources (dict): where each piece of evidence came from.
    """

    curve: str
    calibration_range: tuple | None = None
    backup: str | None = None
    backup_error: float | None = None
    curve_error: float | None = None
    run: str | None = None
    sentinel: float | None = None
    declared: str | None = None
    sources: dict = field(default_factory=dict)

    @property
    def tolerance(self):
        return (self.backup_error or 0.0) + (self.curve_error or 0.0)

    @property
    def has_range(self):
        return self.calibration_range is not None

    def range_contains(self, value):
        if self.calibration_range is None:
            return None
        low, high = self.calibration_range
        return bool(low <= value <= high)

    def to_dict(self):
        return {
            "curve": self.curve,
            "calibration_range": list(self.calibration_range)
            if self.calibration_range
            else None,
            "backup": self.backup,
            "backup_error": self.backup_error,
            "curve_error": self.curve_error,
            "tolerance": self.tolerance,
            "run": self.run,
            "sentinel": self.sentinel,
            "declared": self.declared,
            "sources": dict(self.sources),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            curve=data.get("curve", ""),
            calibration_range=_parse_range(data.get("calibration_range")),
            backup=data.get("backup"),
            backup_error=_parse_tolerance(data.get("backup_error")),
            curve_error=_parse_tolerance(data.get("curve_error")),
            run=_parse_run(data.get("run")),
            sentinel=_as_float(data.get("sentinel")),
            declared=data.get("declared"),
            sources=dict(data.get("sources") or {}),
        )


@dataclass(frozen=True)
class SentinelRecord:
    """Decision for one sample equal to the audited sentinel."""

    curve: str
    index: int
    depth: float | None
    raw_value: float
    decision: str
    reasons: tuple
    evidence: dict

    @property
    def doubt(self):
        return self.decision == AMBIGUOUS

    @property
    def original(self):
        return self.raw_value

    def to_dict(self):
        return {
            "curve": self.curve,
            "index": self.index,
            "depth": self.depth,
            "raw_value": self.raw_value,
            "decision": self.decision,
            "doubt": self.doubt,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


class SentinelAudit:
    """Result of a per-curve sentinel audit.

    The audit keeps a copy of every audited curve's original values, so samples
    that were replaced with ``numpy.nan`` remain queryable through
    :meth:`original_data` and through each :class:`SentinelRecord`.
    """

    def __init__(
        self,
        value,
        records,
        specs,
        originals,
        modified,
        applied,
        include_index=False,
        restored=None,
    ):
        self.value = float(value)
        self.records = list(records)
        self.specs = dict(specs)
        self._originals = {name: np.array(data, copy=True) for name, data in originals.items()}
        self.modified = {name: list(indices) for name, indices in modified.items()}
        self.restored = {name: list(indices) for name, indices in (restored or {}).items()}
        self.applied = bool(applied)
        self.include_index = bool(include_index)

    def __len__(self):
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    def for_curve(self, mnemonic):
        mnemonic = _norm(mnemonic)
        return [record for record in self.records if _norm(record.curve) == mnemonic]

    def at_depth(self, depth, atol=1e-9):
        try:
            depth = float(depth)
        except (TypeError, ValueError):
            return []
        return [
            record
            for record in self.records
            if record.depth is not None
            and abs(float(record.depth) - depth) <= atol
        ]

    @property
    def doubts(self):
        return [record for record in self.records if record.doubt]

    @property
    def ambiguous(self):
        return self.doubts

    @property
    def resolved_missing(self):
        return [record for record in self.records if record.decision == MISSING]

    @property
    def kept(self):
        return [record for record in self.records if record.decision != MISSING]

    @property
    def restored_records(self):
        return [record for record in self.records if record.decision != MISSING]

    def records_for(self, mnemonic):
        return self.for_curve(mnemonic)

    def doubts_for(self, mnemonic):
        return [record for record in self.for_curve(mnemonic) if record.doubt]

    @property
    def measured(self):
        return [record for record in self.records if record.decision == MEASURED]

    @property
    def raw_data(self):
        return {name: np.array(data, copy=True) for name, data in self._originals.items()}

    def original_data(self, mnemonic):
        mnemonic = _norm(mnemonic)
        for name, data in self._originals.items():
            if _norm(name) == mnemonic:
                return np.array(data, copy=True)
        raise KeyError("{} was not audited".format(mnemonic))

    original = original_data

    def summary(self):
        return {
            "sentinel": self.value,
            "applied": self.applied,
            "curves": len(self._originals),
            "samples": len(self.records),
            "missing": len(self.resolved_missing),
            "measured": len(self.measured),
            "ambiguous": len(self.doubts),
            "replaced": sum(len(indices) for indices in self.modified.values()),
            "restored": sum(len(indices) for indices in self.restored.values()),
        }

    def to_dict(self):
        return {
            "summary": self.summary(),
            "specs": {
                name: spec.to_dict() for name, spec in self.specs.items()
            },
            "records": [record.to_dict() for record in self.records],
        }

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True)

    def __repr__(self):
        summary = self.summary()
        return (
            "SentinelAudit(sentinel={sentinel}, samples={samples}, "
            "missing={missing}, measured={measured}, ambiguous={ambiguous})"
        ).format(**summary)


def _curve_lookup(las):
    lookup = {}
    for position, curve in enumerate(las.curves):
        for name in (curve.mnemonic, curve.original_mnemonic):
            if name:
                lookup[_norm(name)] = (position, curve)
    return lookup


def _find_item(mnemonic, variants, suffixes=(), prefixes=()):
    mnemonic = _norm(mnemonic)
    for variant in variants:
        for suffix in suffixes:
            key = variant + suffix
            if key in mnemonic:
                return True
        for prefix in prefixes:
            key = prefix + variant
            if key in mnemonic:
                return True
    return False


def _matching_item(items, variants, suffixes, prefixes):
    for item in items:
        if _find_item(_norm(item.mnemonic), variants, suffixes, prefixes):
            return item
    return None


def _parameter_specs(las, curve):
    """Collect curve-specific evidence from the ~P section."""
    variants = _mnemonic_variants(curve.mnemonic)
    variants |= _mnemonic_variants(curve.original_mnemonic)
    items = list(las.params)
    result = {}

    range_item = _matching_item(items, variants, _RANGE_SUFFIXES, _RANGE_PREFIXES)
    if range_item is not None:
        parsed = _parse_range(range_item.value)
        if parsed:
            result["calibration_range"] = (
                parsed,
                "parameter {}".format(range_item.mnemonic),
            )

    if "calibration_range" not in result:
        lower = _matching_item(
            items, variants, _MINMAX_SUFFIXES["min"], _MINMAX_PREFIXES["min"]
        )
        upper = _matching_item(
            items, variants, _MINMAX_SUFFIXES["max"], _MINMAX_PREFIXES["max"]
        )
        if lower is not None and upper is not None:
            parsed = _ordered_range(_as_float(lower.value), _as_float(upper.value))
            if parsed:
                result["calibration_range"] = (
                    parsed,
                    "parameters {} / {}".format(lower.mnemonic, upper.mnemonic),
                )

    backup_item = _matching_item(
        items, variants, _BACKUP_SUFFIXES, _BACKUP_PREFIXES
    )
    if backup_item is not None:
        backup = _norm(backup_item.value)
        if backup:
            result["backup"] = (backup, "parameter {}".format(backup_item.mnemonic))

    error_item = _matching_item(items, variants, _ERROR_SUFFIXES, _ERROR_PREFIXES)
    if error_item is not None:
        tolerance = _parse_tolerance(error_item.value)
        if tolerance is not None:
            result["backup_error"] = (
                tolerance,
                "parameter {}".format(error_item.mnemonic),
            )

    run_item = _matching_item(items, variants, _RUN_SUFFIXES, _RUN_PREFIXES)
    if run_item is not None:
        run = _parse_run(run_item.value) or _run_from_text(run_item.descr)
        if run:
            result["run"] = (run, "parameter {}".format(run_item.mnemonic))

    sentinel_item = _matching_item(
        items, variants, _SENTINEL_SUFFIXES, _SENTINEL_PREFIXES
    )
    if sentinel_item is not None:
        sentinel = _as_float(sentinel_item.value)
        if sentinel is not None:
            result["sentinel"] = (
                sentinel,
                "parameter {}".format(sentinel_item.mnemonic),
            )
    return result


def _description_specs(curve):
    result = {}
    text = _norm(curve.descr)
    if not text:
        return result

    parsed_range = _range_from_text(curve.descr)
    if parsed_range:
        result["calibration_range"] = (parsed_range, "curve description")

    backup = _backup_from_text(curve.descr)
    if backup:
        result["backup"] = (backup, "curve description")

    tolerance = _tolerance_from_text(curve.descr)
    if tolerance is not None:
        result["backup_error"] = (tolerance, "curve description")

    run = _run_from_text(curve.descr)
    if run:
        result["run"] = (run, "curve description")

    sentinel = _sentinel_from_text(curve.descr)
    if sentinel is not None:
        result["sentinel"] = (sentinel, "curve description")
    return result


def _normalize_spec(value, curve_name):
    if value is None:
        return {}
    if isinstance(value, CurveSentinelSpec):
        return value
    if isinstance(value, dict):
        spec = dict(value)
    elif isinstance(value, (tuple, list)) and len(value) >= 2 and all(
        _as_float(item) is not None for item in value[:2]
    ):
        spec = {"calibration_range": (value[0], value[1])}
    elif _is_number(value):
        spec = {"sentinel": float(value)}
    else:
        spec = {"calibration_range": value}

    result = {}
    aliases = {
        "curve": ("curve", "mnemonic", "name"),
        "calibration_range": (
            "calibration_range",
            "range",
            "valid_range",
            "calibrated_range",
            "cal",
            "calibration",
        ),
        "backup": ("backup", "backup_curve", "spare", "redundant", "pair"),
        "backup_error": (
            "backup_error",
            "error",
            "tolerance",
            "accuracy",
            "backup_tolerance",
        ),
        "curve_error": ("curve_error", "sensor_error", "primary_error"),
        "run": ("run", "pass", "run_id"),
        "sentinel": ("sentinel", "null", "null_value", "missing_value"),
        "declared": ("declared", "sensor", "kind", "type"),
    }
    for target, names in aliases.items():
        for name in names:
            if name in spec and spec[name] is not None:
                result[target] = spec[name]
                break

    if "calibration_range" in result:
        result["calibration_range"] = _parse_range(result["calibration_range"])
    if "backup_error" in result:
        result["backup_error"] = _parse_tolerance(result["backup_error"])
    if "curve_error" in result:
        result["curve_error"] = _parse_tolerance(result["curve_error"])
    if "sentinel" in result:
        result["sentinel"] = _as_float(result["sentinel"])
    if "run" in result:
        result["run"] = _parse_run(result["run"])
    return result


def extract_specs(las, curves=None):
    """Extract per-curve audit evidence from the LAS header.

    Arguments:
        las (LASFile): file to inspect.
        curves (iterable): optional curve mnemonics to restrict the result.

    Returns:
        dict mapping curve mnemonics to :class:`CurveSentinelSpec`.
    """
    selected = None
    if curves is not None:
        selected = {_norm(name) for name in curves}

    detected = {}
    backup_for = {}

    for curve in las.curves:
        if selected is not None and _norm(curve.mnemonic) not in selected:
            continue
        evidence = {}
        evidence.update(_description_specs(curve))
        params = _parameter_specs(las, curve)
        for key, value in params.items():
            evidence.setdefault(key, value)

        target = _backup_target_from_text(curve.descr)
        if target:
            backup_for.setdefault(target, []).append(curve.mnemonic)

        spec = CurveSentinelSpec(
            curve=curve.mnemonic,
            declared=_classify_curve(curve),
        )
        sources = {}
        if "calibration_range" in evidence:
            spec.calibration_range, sources["calibration_range"] = evidence["calibration_range"]
        if "backup" in evidence:
            spec.backup, sources["backup"] = evidence["backup"]
        if "backup_error" in evidence:
            spec.backup_error, sources["backup_error"] = evidence["backup_error"]
        if "curve_error" in evidence:
            spec.curve_error, sources["curve_error"] = evidence["curve_error"]
        if "run" in evidence:
            spec.run, sources["run"] = evidence["run"]
        if "sentinel" in evidence:
            spec.sentinel, sources["sentinel"] = evidence["sentinel"]
        spec.sources = sources
        detected[curve.mnemonic] = spec

    for target, backups in backup_for.items():
        spec = _spec_for_name(detected, target)
        if spec is not None and spec.backup is None and backups:
            spec.backup = backups[0]
            spec.sources["backup"] = "backup curve description"

    return detected


def _spec_for_name(specs, name):
    name = _norm(name)
    for mnemonic, spec in specs.items():
        if _norm(mnemonic) == name or _norm(spec.curve) == name:
            return spec
    for mnemonic, spec in specs.items():
        if _strip_duplicate_suffix(mnemonic) == _strip_duplicate_suffix(name):
            return spec
    return None


def _merge_specs(detected, explicit):
    specs = {name: spec for name, spec in detected.items()}
    if not explicit:
        return specs
    if isinstance(explicit, CurveSentinelSpec):
        explicit = {explicit.curve: explicit}
    if isinstance(explicit, (list, tuple)):
        explicit = {
            (item.get("curve") if isinstance(item, dict) else item.curve): item
            for item in explicit
        }
    for name, value in explicit.items():
        normalized = _normalize_spec(value, name)
        if isinstance(normalized, CurveSentinelSpec):
            specs[name] = normalized
            continue
        existing = _spec_for_name(specs, name)
        if existing is None:
            existing = CurveSentinelSpec(curve=name, declared=None)
            specs[name] = existing
        for key, item in normalized.items():
            if key in ("curve", "mnemonic", "name"):
                continue
            setattr(existing, key, item)
            existing.sources[key] = "explicit specification"
    return specs


def _backup_array(las, spec):
    if not spec.backup:
        return None, None
    lookup = _curve_lookup(las)
    found = lookup.get(_norm(spec.backup))
    if found is None:
        return None, None
    return found[1], found[0]


def _evaluate(las, curve, spec, array, index, sentinel, specs_by_name=None, null_originals=None):
    raw_value = float(array[index])
    evidence = {
        "curve": curve.mnemonic,
        "unit": curve.unit,
        "declared": spec.declared,
        "calibration_range": list(spec.calibration_range)
        if spec.calibration_range
        else None,
        "backup": spec.backup,
        "backup_error": spec.backup_error,
        "curve_error": spec.curve_error,
        "tolerance": spec.tolerance,
        "run": spec.run,
    }
    reasons = []

    range_state = "unknown"
    if spec.calibration_range is not None:
        if spec.range_contains(sentinel):
            range_state = "includes"
        else:
            range_state = "excludes"
            low, high = spec.calibration_range
            reasons.append(
                "outside calibration range [{:g}, {:g}]".format(low, high)
            )
    evidence["range_state"] = range_state

    backup_state = "unavailable"
    backup_curve, backup_position = _backup_array(las, spec)
    if backup_curve is not None and backup_position != 0:
        evidence["backup_value"] = None
        backup_data = np.asarray(backup_curve.data)
        if null_originals:
            for mnemonic, values in null_originals.items():
                if _norm(mnemonic) == _norm(backup_curve.mnemonic):
                    backup_data = np.where(np.isnan(backup_data.astype(float)), values, backup_data)
                    break
        if len(backup_data) > index:
            value = backup_data[index]
            if _is_number(value):
                value = float(value)
                backup_spec = (
                    _spec_for_name(specs_by_name, backup_curve.mnemonic)
                    if specs_by_name
                    else None
                )
                backup_sentinel = sentinel
                if backup_spec is not None and backup_spec.sentinel is not None:
                    backup_sentinel = float(backup_spec.sentinel)
                evidence["backup_value"] = value
                if np.isclose(value, backup_sentinel, rtol=0.0, atol=1e-9):
                    backup_state = "sentinel"
                    reasons.append(
                        "backup {} is also the sentinel; no independent evidence".format(
                            backup_curve.mnemonic
                        )
                    )
                else:
                    runs_match = _runs_match(
                        spec.run,
                        backup_spec.run if backup_spec is not None else None,
                    )
                    if runs_match is False:
                        backup_state = "run_mismatch"
                        evidence["backup_run"] = (
                            backup_spec.run if backup_spec is not None else None
                        )
                        reasons.append(
                            "backup {} is from a different run".format(
                                backup_curve.mnemonic
                            )
                        )
                    else:
                        difference = abs(value - sentinel)
                        evidence["backup_difference"] = difference
                        tolerance = spec.tolerance
                        if spec.backup_error is None and spec.curve_error is None:
                            backup_state = "no_tolerance"
                            reasons.append(
                                "backup {} has no stated error range".format(
                                    backup_curve.mnemonic
                                )
                            )
                        elif difference <= tolerance:
                            backup_state = "supports"
                            reasons.append(
                                "backup {} agrees within {:g} (difference {:g})".format(
                                    backup_curve.mnemonic, tolerance, difference
                                )
                            )
                        else:
                            backup_state = "contradicts"
                            reasons.append(
                                "backup {} differs by {:g}, beyond tolerance {:g}".format(
                                    backup_curve.mnemonic, difference, tolerance
                                )
                            )
            else:
                reasons.append(
                    "backup {} has no numeric value at this depth".format(
                        backup_curve.mnemonic
                    )
                )
        else:
            reasons.append(
                "backup {} has no sample at this depth".format(backup_curve.mnemonic)
            )
    elif spec.backup:
        reasons.append("backup {} was not found in the LAS file".format(spec.backup))

    evidence["backup_state"] = backup_state

    if range_state == "excludes":
        if backup_state == "supports":
            decision = AMBIGUOUS
            reasons.append(
                "conflicting evidence: range excludes the sentinel but the backup supports it"
            )
        else:
            decision = MISSING
    elif range_state == "includes":
        if backup_state == "contradicts":
            decision = MISSING
        elif backup_state == "supports":
            decision = MEASURED
        else:
            decision = AMBIGUOUS
            reasons.append(
                "within calibration range and no usable backup; real measurement cannot be excluded"
            )
    else:
        if backup_state == "contradicts":
            decision = MISSING
        elif backup_state == "supports":
            decision = MEASURED
        else:
            decision = AMBIGUOUS
            reasons.append(
                "no calibration range or usable backup; real measurement cannot be excluded"
            )

    depth = None
    if len(las.curves) > 0 and len(las.curves[0].data) > index:
        depth_value = np.asarray(las.curves[0].data)[index]
        if _is_number(depth_value):
            depth = float(depth_value)

    if not reasons:
        reasons.append("insufficient evidence")

    return SentinelRecord(
        curve=curve.mnemonic,
        index=int(index),
        depth=depth,
        raw_value=raw_value,
        decision=decision,
        reasons=tuple(reasons),
        evidence=evidence,
    )


def audit_sentinels(
    las,
    value=-99.0,
    specs=None,
    apply=True,
    curves=None,
    include_index=False,
    null_originals=None,
):
    """Audit samples equal to a numeric sentinel, curve by curve.

    Arguments:
        las (LASFile): file to audit.
        value (float): sentinel value to examine, e.g. ``-99``.
        specs (dict): optional explicit per-curve evidence. Keys are curve
            mnemonics; values can be :class:`CurveSentinelSpec`, dicts with
            keys such as ``calibration_range``, ``backup``, ``backup_error``,
            ``curve_error``, ``run``, ``sentinel``, or a ``(low, high)``
            calibration range.
        apply (bool): replace samples whose real-measurement explanation is
            excluded with ``numpy.nan``. Defaults to True.
        curves (iterable): optional curve mnemonics to restrict the audit.
        include_index (bool): also audit the first (index) curve. Defaults to
            False.
        null_originals (dict): data columns before file-level NULL
            substitution, keyed by mnemonic. When given, samples that the
            reader already replaced with NaN but whose original value equals
            the sentinel are also audited; ambiguous samples have the original
            value restored.

    Returns:
        :class:`SentinelAudit`
    """
    if value is None:
        value = -99.0
    value = _as_float(value)
    if value is None:
        raise ValueError("sentinel value must be numeric")

    selected = None
    if curves is not None:
        selected = {_norm(name) for name in curves}

    detected = extract_specs(las, curves=curves)
    specs_by_name = _merge_specs(detected, specs)
    referenced_as_backup = {
        _norm(spec.backup)
        for spec in specs_by_name.values()
        if spec.backup
    }
    backup_names = set()
    for name in referenced_as_backup:
        own = _spec_for_name(specs_by_name, name)
        if own is None or not own.has_range:
            backup_names.add(name)

    def lookup(collection, mnemonic):
        for name, values in (collection or {}).items():
            if _norm(name) == _norm(mnemonic):
                return values
        return None

    records = []
    originals = {}
    modified = {}
    restored_indices = {}

    for position, curve in enumerate(las.curves):
        if position == 0 and not include_index:
            continue
        if selected is not None and _norm(curve.mnemonic) not in selected:
            continue
        if selected is None and _norm(curve.mnemonic) in backup_names:
            continue

        array = np.asarray(curve.data)
        if array.ndim != 1 or array.dtype.kind not in "fiu":
            continue

        restored_source = lookup(null_originals, curve.mnemonic)
        source = restored_source if restored_source is not None else array

        spec = _spec_for_name(specs_by_name, curve.mnemonic)
        curve_sentinel = value
        if spec is not None and spec.sentinel is not None:
            curve_sentinel = float(spec.sentinel)

        matches = np.flatnonzero(
            np.isclose(source.astype(float), curve_sentinel, rtol=0.0, atol=1e-9)
        )
        if restored_source is not None:
            matches = matches[np.isnan(array.astype(float))[matches]]

        if len(matches) == 0 and spec is None:
            continue
        if spec is None:
            spec = CurveSentinelSpec(
                curve=curve.mnemonic,
                declared=_classify_curve(curve),
            )
            specs_by_name[curve.mnemonic] = spec
        elif (matches.size > 0 and not spec.has_range and not spec.backup
              and spec.declared != "temperature"):
            continue

        original = np.array(source, copy=True)
        originals[curve.mnemonic] = original

        curve_modified = []
        curve_restored = []
        for index in matches:
            index = int(index)
            record = _evaluate(
                las,
                curve,
                spec,
                original,
                index,
                curve_sentinel,
                specs_by_name,
                null_originals=null_originals,
            )
            records.append(record)

            if apply and restored_source is not None and record.decision != MISSING:
                data = np.asarray(curve.data).astype(float)
                data[index] = original[index]
                curve.data = data
                curve_restored.append(index)

            if apply and record.decision == MISSING:
                data = np.asarray(curve.data)
                if data.dtype.kind in "iu":
                    data = data.astype(float)
                    curve.data = data
                data[index] = np.nan
                curve_modified.append(index)

        if curve_modified:
            modified[curve.mnemonic] = curve_modified
        if curve_restored:
            restored_indices[curve.mnemonic] = curve_restored

    audit = SentinelAudit(
        value=value,
        records=records,
        specs=specs_by_name,
        originals=originals,
        modified=modified,
        restored=restored_indices,
        applied=apply,
        include_index=include_index,
    )
    audit.las = las
    return audit
