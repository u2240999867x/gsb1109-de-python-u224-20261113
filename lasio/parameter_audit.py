"""Audit trail for parameters repeated across handovers.

When a well is delivered in more than one LAS handover (e.g. a *spud /
opening-up* version followed by a *completion* version), the same mnemonic
can appear several times:

* repeated in a later logging batch because the value was actually
  corrected, or
* restated with an equivalent value after only the display unit changed.

The ordinary :class:`lasio.SectionItems` accessors return a single item and
so cannot tell those two situations apart - taking "the last value with the
same name" silently discards the earlier handover. This module keeps every
occurrence in source order together with the provenance needed to decide
which one actually applies to a curve sample, and provides a small,
explicitly documented unit-conversion table so that "same value, different
unit" can be verified rather than guessed.

The module is deliberately independent of the parsing machinery: the reader
feeds it :class:`lasio.HeaderItem` objects and it stores plain
:class:`ParameterOccurrence` records.
"""

import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from .exceptions import LASParameterHandoffError, LASUnknownUnitError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verifiable unit conversions
# ---------------------------------------------------------------------------

# Factor means: multiply a value expressed in unit A to express it in unit
# B, i.e. value_in_B = value_in_A * FACTORS[(canon(A), canon(B))].
# Inverse factors are generated automatically below; affine temperature
# conversions are handled separately.
_FACTORS = {
    # length / depth
    ("FT", "M"): 0.3048,
    ("IN", "M"): 0.0254,
    ("IN", "FT"): 1.0 / 12.0,
    ("CM", "M"): 0.01,
    ("MM", "M"): 0.001,
    ("MM", "IN"): 1.0 / 25.4,
    (".1IN", "IN"): 0.1,
    ("KM", "M"): 1000.0,
    # pressure
    ("PSI", "KPA"): 6.894757293168,
    ("PSI", "BAR"): 0.06894757293168,
    ("BAR", "KPA"): 100.0,
    ("ATM", "PSI"): 14.6959487755,
    ("MPA", "PSI"): 145.03773773,
    ("MPA", "KPA"): 1000.0,
    ("MPA", "BAR"): 10.0,
    ("KPA", "PA"): 1000.0,
    ("PSI", "PA"): 6894.757293168,
    # density
    ("GCM3", "KGM3"): 1000.0,
    ("LBMGAL", "KGM3"): 119.8264273169,
    ("LBMFT3", "KGM3"): 16.018463374,
    ("LBMGAL", "GCM3"): 0.1198264273169,
    # volume
    ("FT3", "M3"): 0.028316846592,
    ("BBL", "M3"): 0.158987294928,
    ("GAL", "L"): 3.785411784,
    ("L", "M3"): 0.001,
    ("BBL", "GAL"): 42.0,
}

# Spelling variants which reduce to the same canonical unit after the
# non-alphanumeric characters are stripped.
_UNIT_ALIASES = {
    "KM3": "KGM3",          # lasio example files write kg/m3 as K/M3
    "GCC": "GCM3",
    "PPG": "LBMGAL",        # pounds per US gallon, mud weight
    "LBFIN2": "PSI",
    "CELSIUS": "DEGC",
    "FAHRENHEIT": "DEGF",
    "KELVIN": "K",
    "FOOT": "FT",
    "FEET": "FT",
    "METER": "M",
    "METERS": "M",
    "METRE": "M",
    "METRES": "M",
}

_TEMPERATURE_UNITS = {"DEGC", "DEGF", "K"}


def canonical_unit(unit):
    """Return the canonical spelling used inside the conversion table."""
    if unit is None:
        return ""
    canon = re.sub(r"[^A-Z0-9.]", "", str(unit).upper())
    return _UNIT_ALIASES.get(canon, canon)


def _build_factor_table():
    table = dict(_FACTORS)
    for (unit_a, unit_b), factor in list(_FACTORS.items()):
        table[(unit_b, unit_a)] = 1.0 / factor
    return table


_CONVERSIONS = _build_factor_table()


def convert_value(value, from_unit, to_unit):
    """Convert *value* between two declared units.

    Arguments:
        value (int or float): value expressed in *from_unit*
        from_unit (str): unit declared on the source item
        to_unit (str): unit to express the result in

    Returns:
        tuple: ``(converted_value, basis)`` where *basis* is a short string
        documenting the rule used.

    Raises:
        lasio.LASUnknownUnitError: if no verifiable conversion rule exists
            (including non-numeric values for affine temperature conversions)

    Only rules from the explicit table above are used; unknown unit pairs
    are reported rather than guessed at.
    """
    source = canonical_unit(from_unit)
    target = canonical_unit(to_unit)
    if source == target:
        return value, "identical unit (%s)" % (from_unit or "none")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise LASUnknownUnitError(
            "Cannot convert non-numeric value %r between units %r and %r"
            % (value, from_unit, to_unit)
        )
    if source in _TEMPERATURE_UNITS or target in _TEMPERATURE_UNITS:
        return _convert_temperature(numeric, source, target, from_unit, to_unit)
    try:
        factor = _CONVERSIONS[(source, target)]
    except KeyError:
        raise LASUnknownUnitError(
            "No verifiable conversion rule from unit %r to %r"
            % (from_unit, to_unit)
        )
    basis = "%s -> %s, multiply by %.12g" % (from_unit, to_unit, factor)
    return numeric * factor, basis


def _convert_temperature(value, source, target, from_unit, to_unit):
    # Route every affine conversion through degrees Celsius.
    if source == "DEGC":
        celsius = value
    elif source == "DEGF":
        celsius = (value - 32.0) * 5.0 / 9.0
    elif source == "K":
        celsius = value - 273.15
    else:
        raise LASUnknownUnitError(
            "No verifiable temperature conversion from unit %r" % from_unit
        )
    if target == "DEGC":
        result = celsius
    elif target == "DEGF":
        result = celsius * 9.0 / 5.0 + 32.0
    elif target == "K":
        result = celsius + 273.15
    else:
        raise LASUnknownUnitError(
            "No verifiable temperature conversion to unit %r" % to_unit
        )
    return result, "affine temperature scale %s -> %s" % (from_unit, to_unit)


def values_equivalent(value, unit, other_value, other_unit,
                      rel_tol=1e-9, abs_tol=1e-12):
    """Return ``(equivalent, converted_or_none, basis)`` for two records.

    The comparison is done in the unit of the second record so that a unit
    change cannot mask a real parameter correction.
    """
    try:
        converted, basis = convert_value(value, unit, other_unit)
    except LASUnknownUnitError:
        return False, None, "no verifiable unit conversion available"
    try:
        target = float(other_value)
    except (TypeError, ValueError):
        return False, converted, basis + "; target value is non-numeric"
    equivalent = math.isclose(
        converted, target, rel_tol=rel_tol, abs_tol=abs_tol
    )
    return equivalent, converted, basis


# ---------------------------------------------------------------------------
# Provenance parsing
# ---------------------------------------------------------------------------

_BATCH_RE = re.compile(
    r"(?:(?<![A-Za-z])(?:batch|logging\s+run|run|pass|job)(?![A-Za-z])"
    r"|批次|趟)[\s:#\-]*([A-Za-z0-9_][A-Za-z0-9_\-]*)",
    re.IGNORECASE,
)
_SEGMENT_RE = re.compile(
    r"(?:(?<![A-Za-z])(?:segment|interval|zone|section)(?![A-Za-z])"
    r"|段|层段|井段)[\s:#\-]*([A-Za-z0-9_][A-Za-z0-9_\-]*)",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(m|ft|feet|foot|meter|metre|km|cm|mm|in|inch|\.1in)?\s*"
    r"(?:-|–|to)\s*"
    r"(\d+(?:\.\d+)?)\s*"
    r"(m|ft|feet|foot|meter|metre|km|cm|mm|in|inch|\.1in)?",
    re.IGNORECASE,
)


def parse_provenance(descr, section_title=""):
    """Extract batch, segment and depth-range hints from descriptive text.

    Everything is optional: the conventions recognised here are

    * a batch/run marker such as ``BATCH 2`` or ``RUN-1``,
    * a segment marker such as ``SEGMENT A``,
    * a depth interval such as ``1200.0 - 1450.5 M``.

    Returns a dict with keys ``batch``, ``segment``, ``top``, ``bottom`` and
    ``range_unit``; unrecognised fields are ``None``.
    """
    text = str(descr or "")
    batch_match = _BATCH_RE.search(text)
    segment_match = _SEGMENT_RE.search(text)
    range_match = _RANGE_RE.search(text)
    result = {
        "batch": batch_match.group(1) if batch_match else None,
        "segment": segment_match.group(1) if segment_match else None,
        "top": None,
        "bottom": None,
        "range_unit": None,
    }
    if range_match:
        result["top"] = float(range_match.group(1))
        result["bottom"] = float(range_match.group(3))
        result["range_unit"] = range_match.group(2) or range_match.group(4)
    return result


# ---------------------------------------------------------------------------
# Occurrence records and registry
# ---------------------------------------------------------------------------

@dataclass
class ParameterOccurrence:
    """One appearance of a parameter mnemonic in source order."""

    mnemonic: str
    unit: str
    value: object
    descr: str
    source_section: str
    source_section_title: str
    source_section_index: int
    source_order: int
    source_line_no: Optional[int] = None
    section_comments: tuple = ()
    batch: Optional[str] = None
    segment: Optional[str] = None
    top: Optional[float] = None
    bottom: Optional[float] = None
    range_unit: Optional[str] = None

    @property
    def handover_key(self):
        """Grouping key for "which handover" produced this occurrence."""
        return self.batch if self.batch is not None else self.source_section_index

    def as_dict(self):
        return {
            "mnemonic": self.mnemonic,
            "unit": self.unit,
            "value": self.value,
            "descr": self.descr,
            "batch": self.batch,
            "segment": self.segment,
            "top": self.top,
            "bottom": self.bottom,
            "range_unit": self.range_unit,
            "source_section": self.source_section,
            "source_section_title": self.source_section_title,
            "source_section_index": self.source_section_index,
            "source_order": self.source_order,
            "source_line_no": self.source_line_no,
            "section_comments": list(self.section_comments),
        }


@dataclass
class ParameterLookup:
    """Result of resolving the parameter applicable at one depth."""

    occurrence: ParameterOccurrence
    basis: str
    depth: float
    depth_unit: Optional[str] = None
    fallback: bool = False


class ParameterRegistry:
    """Ordered, provenance-aware collection of parameter occurrences."""

    def __init__(self, occurrences=None):
        self.occurrences = list(occurrences or [])
        # Optional externally supplied coverage: handover_key -> (top,
        # bottom, unit). Used when the LAS text carries no depth ranges.
        self.batch_intervals = {}

    def add(self, occurrence):
        self.occurrences.append(occurrence)

    def for_mnemonic(self, mnemonic, case_sensitive=False):
        """Return all occurrences of *mnemonic* in source order."""
        if case_sensitive:
            return [o for o in self.occurrences if o.mnemonic == mnemonic]
        return [
            o for o in self.occurrences
            if str(o.mnemonic).upper() == str(mnemonic).upper()
        ]

    def mnemonics(self):
        seen = OrderedDict()
        for occurrence in self.occurrences:
            seen.setdefault(occurrence.mnemonic, None)
        return list(seen.keys())

    def timeline(self, mnemonic=None):
        """Return occurrences (optionally for one mnemonic) in source order."""
        if mnemonic is None:
            return list(self.occurrences)
        return self.for_mnemonic(mnemonic)

    # -- difference / classification ----------------------------------------

    def classify_pair(self, earlier, later):
        """Decide whether *later* corrects *earlier* or merely restates it."""
        result = {
            "mnemonic": later.mnemonic,
            "earlier": earlier,
            "later": later,
            "classification": "indeterminate",
            "basis": "",
            "converted_value": None,
            "value_delta": None,
        }
        same_unit = canonical_unit(earlier.unit) == canonical_unit(later.unit)
        try:
            earlier_number = float(earlier.value)
            later_number = float(later.value)
        except (TypeError, ValueError):
            result["basis"] = (
                "at least one value is non-numeric; manual review required"
            )
            return result
        if math.isnan(earlier_number) or math.isnan(later_number):
            result["basis"] = "at least one value is NULL; manual review required"
            return result
        if same_unit:
            result["converted_value"] = earlier_number
            if math.isclose(earlier_number, later_number,
                            rel_tol=1e-9, abs_tol=1e-12):
                result["classification"] = "equivalent_restatement"
                result["basis"] = "identical value and unit (%s)" % (
                    earlier.unit or "none"
                )
            else:
                result["classification"] = "parameter_correction"
                result["value_delta"] = later_number - earlier_number
                result["basis"] = (
                    "same unit %r but numeric value changed %s -> %s"
                    % (earlier.unit or "none", earlier.value, later.value)
                )
            return result
        equivalent, converted, basis = values_equivalent(
            earlier.value, earlier.unit, later.value, later.unit
        )
        result["converted_value"] = converted
        if equivalent:
            result["classification"] = "equivalent_restatement_unit_change"
            result["basis"] = (
                "value unchanged after verified conversion (%s)" % basis
            )
        else:
            result["classification"] = "parameter_correction"
            try:
                result["value_delta"] = later_number - float(converted)
            except (TypeError, ValueError):
                result["value_delta"] = None
            result["basis"] = (
                "value still differs after verified conversion (%s)" % basis
            )
        return result

    def diff(self, mnemonic=None):
        """Classify every adjacent repeat of a mnemonic in source order."""
        names = [mnemonic] if mnemonic is not None else self.mnemonics()
        differences = []
        for name in names:
            occurrences = self.for_mnemonic(name)
            for earlier, later in zip(occurrences, occurrences[1:]):
                differences.append(self.classify_pair(earlier, later))
        return differences

    # -- applicability lookup -----------------------------------------------

    def applicable_at(self, mnemonic, depth, depth_unit=None,
                      case_sensitive=False, fallback="raise"):
        """Resolve the occurrence of *mnemonic* applicable at *depth*.

        Arguments:
            mnemonic (str): parameter to look up
            depth (float): curve index value (depth/time) of the sample
            depth_unit (str): unit of *depth*, needed when occurrences
                declare depth ranges in another unit
            fallback (str): ``"raise"`` (default) raises
                :class:`lasio.LASParameterHandoffError` whenever the
                applicable handover cannot be proven; ``"last"`` returns the
                final occurrence instead, flagged as an unverified fallback.

        The returned :class:`ParameterLookup` always carries a *basis*
        string explaining why the occurrence was chosen.
        """
        occurrences = self.for_mnemonic(mnemonic, case_sensitive=case_sensitive)
        if not occurrences:
            raise LASParameterHandoffError(
                "Parameter %r has no occurrences on record" % mnemonic,
                mnemonic=mnemonic,
                reason="not_found",
                missing="a handover declaring parameter %r" % mnemonic,
            )
        if len(occurrences) == 1:
            return ParameterLookup(
                occurrences[0],
                "only declaration of %r on record" % mnemonic,
                depth,
                depth_unit,
            )

        ranged = [o for o in occurrences if o.top is not None]
        if ranged:
            return self._resolve_by_declared_range(
                mnemonic, occurrences, ranged, depth, depth_unit
            )

        interval_hits = self._resolve_by_batch_intervals(
            mnemonic, occurrences, depth, depth_unit
        )
        if interval_hits is not None:
            return interval_hits

        groups = self._group_by_handover(occurrences)
        if len(groups) == 1 and len(occurrences) > 1:
            error = LASParameterHandoffError(
                "Parameter %r is set %d times within handover %r and no "
                "segment/depth declaration distinguishes them; taking the "
                "last value would be a guess"
                % (mnemonic, len(occurrences), occurrences[0].handover_key),
                mnemonic=mnemonic,
                occurrences=occurrences,
                reason="ambiguous_occurrence",
                missing=(
                    "a segment or depth-range declaration saying which %r "
                    "setting applies at this depth" % mnemonic
                ),
            )
            if fallback == "last":
                return self._fallback_last(occurrences[-1], depth, depth_unit, error)
            raise error

        if fallback == "last":
            return self._fallback_last(
                occurrences[-1], depth, depth_unit,
                LASParameterHandoffError(
                    "no depth coverage declared for any handover of %r"
                    % mnemonic,
                    mnemonic=mnemonic, occurrences=occurrences,
                    reason="coverage_gap",
                    missing=(
                        "the depth interval covered by each handover of %r"
                        % mnemonic
                    ),
                ),
            )
        raise LASParameterHandoffError(
            "Cannot prove which handover of %r applies at depth %s: none of "
            "the %d occurrences declares a depth range"
            % (mnemonic, depth, len(occurrences)),
            mnemonic=mnemonic,
            occurrences=occurrences,
            reason="coverage_gap",
            missing=(
                "the handover (batch/segment depth interval) covering depth "
                "%s for parameter %r" % (depth, mnemonic)
            ),
        )

    def _resolve_by_declared_range(self, mnemonic, occurrences, ranged,
                                   depth, depth_unit):
        matches = []
        for occurrence in ranged:
            try:
                if self._covers(occurrence, depth, depth_unit):
                    matches.append(occurrence)
            except LASUnknownUnitError as error:
                raise LASParameterHandoffError(
                    "Cannot compare depth %s %r against the interval "
                    "declared for %r: %s"
                    % (depth, depth_unit, mnemonic, error),
                    mnemonic=mnemonic,
                    occurrences=occurrences,
                    reason="coverage_gap",
                    missing="a declared unit for the depth interval",
                )
        if len(matches) == 1:
            occurrence = matches[0]
            basis = (
                "depth %s falls inside interval %s-%s %s declared on the "
                "occurrence from section #%d (order %d)"
                % (
                    depth,
                    occurrence.top,
                    occurrence.bottom,
                    occurrence.range_unit or "",
                    occurrence.source_section_index,
                    occurrence.source_order,
                )
            )
            return ParameterLookup(occurrence, basis, depth, depth_unit)
        if len(matches) > 1:
            # Adjacent segments legitimately share a boundary depth. At the
            # boundary the convention is to hand over to the segment which
            # starts at that depth. Genuine overlaps (two segments both
            # extending past the depth) remain an error.
            boundary_matches = []
            for occurrence in matches:
                boundary_depth = depth
                if depth_unit and occurrence.range_unit and canonical_unit(
                    depth_unit
                ) != canonical_unit(occurrence.range_unit):
                    boundary_depth, _ = convert_value(
                        depth, depth_unit, occurrence.range_unit
                    )
                if min(occurrence.top, occurrence.bottom) == boundary_depth:
                    boundary_matches.append(occurrence)
            if len(boundary_matches) == 1:
                occurrence = boundary_matches[0]
                basis = (
                    "depth %s is the shared boundary; applying the segment "
                    "which starts there (interval %s-%s %s, section #%d, "
                    "order %d)"
                    % (
                        depth,
                        occurrence.top,
                        occurrence.bottom,
                        occurrence.range_unit or "",
                        occurrence.source_section_index,
                        occurrence.source_order,
                    )
                )
                return ParameterLookup(occurrence, basis, depth, depth_unit)
            raise LASParameterHandoffError(
                "Depth %s is covered by %d declared intervals for %r; the "
                "segment declarations overlap" % (depth, len(matches), mnemonic),
                mnemonic=mnemonic,
                occurrences=matches,
                reason="ambiguous_occurrence",
                missing="non-overlapping segment declarations for %r" % mnemonic,
            )
        covered = [
            (o.top, o.bottom, o.range_unit, o.source_section_index)
            for o in ranged
        ]
        raise LASParameterHandoffError(
            "Depth %s is outside every declared interval for %r (%r); no "
            "handover covers this depth" % (depth, mnemonic, covered),
            mnemonic=mnemonic,
            occurrences=occurrences,
            reason="no_coverage",
            missing=(
                "a handover of %r whose declared interval covers depth %s"
                % (mnemonic, depth)
            ),
        )

    def _resolve_by_batch_intervals(self, mnemonic, occurrences, depth,
                                    depth_unit):
        if not self.batch_intervals:
            return None
        matches = []
        for occurrence in occurrences:
            interval = self.batch_intervals.get(occurrence.handover_key)
            if interval is None:
                continue
            top, bottom, unit = interval
            probe = ParameterOccurrence(
                mnemonic=occurrence.mnemonic,
                unit=occurrence.unit,
                value=occurrence.value,
                descr=occurrence.descr,
                source_section=occurrence.source_section,
                source_section_title=occurrence.source_section_title,
                source_section_index=occurrence.source_section_index,
                source_order=occurrence.source_order,
                top=top,
                bottom=bottom,
                range_unit=unit,
            )
            if self._covers(probe, depth, depth_unit):
                matches.append(occurrence)
        if not matches:
            return None
        if len(matches) > 1:
            raise LASParameterHandoffError(
                "Batch intervals overlap at depth %s for %r" % (depth, mnemonic),
                mnemonic=mnemonic,
                occurrences=matches,
                reason="ambiguous_occurrence",
                missing="non-overlapping batch intervals for %r" % mnemonic,
            )
        occurrence = matches[0]
        basis = (
            "depth %s lies in the externally declared interval of handover "
            "%r (section #%d, order %d)"
            % (
                depth,
                occurrence.handover_key,
                occurrence.source_section_index,
                occurrence.source_order,
            )
        )
        return ParameterLookup(occurrence, basis, depth, depth_unit)

    @staticmethod
    def _covers(occurrence, depth, depth_unit):
        top, bottom = occurrence.top, occurrence.bottom
        if depth_unit and occurrence.range_unit and canonical_unit(
            depth_unit
        ) != canonical_unit(occurrence.range_unit):
            depth, _ = convert_value(
                depth, depth_unit, occurrence.range_unit
            )
        return min(top, bottom) <= depth <= max(top, bottom)

    @staticmethod
    def _group_by_handover(occurrences):
        groups = OrderedDict()
        for occurrence in occurrences:
            groups.setdefault(occurrence.handover_key, []).append(occurrence)
        return groups

    @staticmethod
    def _fallback_last(occurrence, depth, depth_unit, suppressed_error):
        logger.warning(
            "Returning last occurrence of %r without provenance; %s",
            occurrence.mnemonic,
            suppressed_error,
        )
        return ParameterLookup(
            occurrence,
            "unverified last-value fallback; %s" % suppressed_error.missing,
            depth,
            depth_unit,
            fallback=True,
        )


# ---------------------------------------------------------------------------
# Section merging
# ---------------------------------------------------------------------------

def _resolve_provenance(descr, comments, section_title):
    """Merge hints from the description, section comments and title.

    The item description is the most specific source; the comment block of
    the section (often naming the handover/batch) and the raw section title
    are used as fallbacks for any hint the description does not provide.
    """
    sources = [str(descr or "")]
    if comments:
        sources.append(" ".join(str(c) for c in comments))
    sources.append(str(section_title or ""))
    merged = {
        "batch": None,
        "segment": None,
        "top": None,
        "bottom": None,
        "range_unit": None,
    }
    for source in sources:
        hints = parse_provenance(source)
        for key in merged:
            if merged[key] is None and hints[key] is not None:
                merged[key] = hints[key]
    return merged


def _stamp_item(item, section_name, section_title, section_index, order,
                registry=None):
    """Attach provenance attributes to one header item."""
    line_no = getattr(item, "_source_line_no", None)
    comments = tuple(getattr(item, "_section_comments", ()) or ())
    item.source_order = order
    item.source_section = section_name
    item.source_section_title = section_title
    item.source_section_index = section_index
    item.source_line_no = line_no
    if registry is not None and section_name == "Parameter":
        hints = _resolve_provenance(item.descr, comments, section_title)
        registry.add(
            ParameterOccurrence(
                # The session mnemonic may already carry a :1/:2 suffix from
                # duplicate handling inside the parsed section. The audit
                # trail keys on the original mnemonic so every repeat is
                # found under the name written in the LAS file.
                mnemonic=item.original_mnemonic,
                unit=item.unit,
                value=item.value,
                descr=item.descr,
                source_section=section_name,
                source_section_title=section_title,
                source_section_index=section_index,
                source_order=order,
                source_line_no=line_no,
                section_comments=comments,
                batch=hints["batch"],
                segment=hints["segment"],
                top=hints["top"],
                bottom=hints["bottom"],
                range_unit=hints["range_unit"],
            )
        )


def stamp_section(section, section_name, section_title, section_index,
                  start_order, registry=None):
    """Stamp every item of a section read for the first time.

    Returns the next source order counter.
    """
    order = start_order
    for item in section:
        order += 1
        _stamp_item(
            item, section_name, section_title, section_index, order, registry
        )
    section.assign_duplicate_suffixes()
    return order


def merge_section_items(section, incoming, section_name, section_title,
                        section_index, start_order, registry=None):
    """Append *incoming* items to *section*, preserving every occurrence.

    Unlike a plain dict assignment (which replaces the whole section when a
    handover repeats a ``~P`` or ``~C`` title), this keeps the source order
    and stamps each item with provenance attributes. Duplicate mnemonic
    suffixes (``:1``, ``:2`` ...) are renumbered across the merged result.

    Arguments:
        section (lasio.SectionItems): accumulated section, extended in place
        incoming (lasio.SectionItems): items parsed from the new section
        section_name (str): canonical section name, e.g. ``"Parameter"``
        section_title (str): raw title line of the new section
        section_index (int): zero-based index of sections with this name
        start_order (int): source order counter before this section
        registry (ParameterRegistry): optional registry to record
            Parameter occurrences in

    Returns:
        int: the next source order counter
    """
    order = stamp_section(
        incoming,
        section_name,
        section_title,
        section_index,
        start_order,
        registry,
    )
    section.extend(incoming)
    section.assign_duplicate_suffixes()
    return order
