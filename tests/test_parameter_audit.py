"""Tests for handover-aware parameter provenance and lookups.

Covers the situation where a well is delivered in more than one LAS
handover: repeated ~P sections must keep every occurrence in source order,
unit-only restatements must be distinguishable from real corrections, and a
curve-sample lookup must return the applicable parameter with its basis or
state exactly which handover is missing.
"""

import os

import numpy as np
import pytest

import lasio
from lasio import (
    LASParameterHandoffError,
    LASUnknownUnitError,
    ParameterRegistry,
    canonical_unit,
    convert_value,
    parse_provenance,
    values_equivalent,
)
from lasio.las_items import HeaderItem, SectionItems
from lasio.parameter_audit import (
    ParameterOccurrence,
    merge_section_items,
    stamp_section,
)

test_dir = os.path.dirname(__file__)


def egfn(fn):
    return os.path.join(test_dir, "examples", fn)


@pytest.fixture()
def handoff_las():
    return lasio.read(egfn("handoff_duplicate_params.las"))


# --------------------------------------------------------------------------
# Source-order preservation and provenance
# --------------------------------------------------------------------------

def test_repeated_parameter_sections_are_merged_not_overwritten(handoff_las):
    # Every occurrence survives; nothing from the opening handover is lost.
    assert len(handoff_las.params) == 9
    keys = handoff_las.params.keys()
    assert keys.count("PRE:1") + keys.count("PRE:2") + keys.count("PRE:3") == 3
    assert [item.original_mnemonic for item in handoff_las.params] == [
        "BHT",
        "PRE",
        "PRE",
        "FD",
        "RMR",
        "BHT",
        "PRE",
        "FD",
        "RMR",
    ]


def test_occurrences_keep_source_order_and_provenance(handoff_las):
    occurrences = handoff_las.parameter_occurrences("PRE")
    assert len(occurrences) == 3
    assert [o.value for o in occurrences] == [100.0, 110.0, 120.0]
    assert [o.source_section_index for o in occurrences] == [0, 0, 1]
    assert [o.source_order for o in occurrences] == [2, 3, 7]
    assert all(o.source_section == "Parameter" for o in occurrences)
    assert all(o.source_line_no is not None for o in occurrences)
    # Registry keys on the original LAS mnemonic, even though the section
    # uses PRE:1/PRE:2/PRE:3 session names.
    assert all(o.mnemonic == "PRE" for o in occurrences)


def test_find_all_returns_every_occurrence(handoff_las):
    items = handoff_las.params.find_all("PRE")
    assert len(items) == 3
    assert [i.value for i in items] == [100.0, 110.0, 120.0]
    assert handoff_las.params.find_all("DOES-NOT-EXIST") == []


def test_remark_timeline_is_distinct_from_named_lookup(handoff_las):
    remarks = handoff_las.parameter_occurrences("RMR")
    assert [r.value for r in remarks] == ["ABC", "XYZ"]
    assert [r.source_section_index for r in remarks] == [0, 1]


def test_provenance_parse_batch_segment_and_range():
    hints = parse_provenance(
        "PRESSURE SETTING BATCH 1 SEGMENT A 1000.0 - 1120.0 M"
    )
    assert hints["batch"] == "1"
    assert hints["segment"] == "A"
    assert hints["top"] == 1000.0
    assert hints["bottom"] == 1120.0
    assert hints["range_unit"].upper() == "M"
    assert parse_provenance("plain description") == {
        "batch": None,
        "segment": None,
        "top": None,
        "bottom": None,
        "range_unit": None,
    }


def test_provenance_parser_avoids_fragment_matches():
    # Ordinary words merely starting with a keyword must not be treated
    # as batch markers, but compact forms like BATCH2 or 批次3 still work.
    assert parse_provenance("TOOL RUNNING NORMALLY")["batch"] is None
    assert parse_provenance("PASSIVE MODE")["batch"] is None
    assert parse_provenance("BATCH2 SETTING")["batch"] == "2"
    assert parse_provenance("批次3 参数")["batch"] == "3"
    assert parse_provenance("RUN-1 LOGGING")["batch"] == "1"


# --------------------------------------------------------------------------
# Correction vs equivalent restatement
# --------------------------------------------------------------------------

def test_same_unit_value_change_is_a_correction(handoff_las):
    changes = handoff_las.parameter_changes("BHT")
    assert len(changes) == 1
    change = changes[0]
    assert change["classification"] == "parameter_correction"
    assert change["value_delta"] == pytest.approx(4.5)
    assert "same unit" in change["basis"]


def test_unit_only_change_with_equal_value_is_restatement(handoff_las):
    # 1000 kg/m3 == 1.0 g/cm3: the completion handover only changed units.
    changes = handoff_las.parameter_changes("FD")
    assert len(changes) == 1
    change = changes[0]
    assert change["classification"] == "equivalent_restatement_unit_change"
    assert change["converted_value"] == pytest.approx(1.0)


def test_unit_change_with_different_value_is_still_a_correction():
    registry = ParameterRegistry()
    registry.add(
        ParameterOccurrence(
            mnemonic="PRE", unit="PSI", value=100.0, descr="",
            source_section="Parameter", source_section_title="~P",
            source_section_index=0, source_order=1,
        )
    )
    registry.add(
        ParameterOccurrence(
            mnemonic="PRE", unit="KPA", value=500.0, descr="",
            source_section="Parameter", source_section_title="~P",
            source_section_index=1, source_order=2,
        )
    )
    change = registry.diff("PRE")[0]
    assert change["classification"] == "parameter_correction"
    # 100 PSI = 689.48 kPa, so 500 kPa is a real reduction.
    assert change["converted_value"] == pytest.approx(689.4757293168)


# --------------------------------------------------------------------------
# Verifiable unit conversions
# --------------------------------------------------------------------------

def test_known_conversions_are_documented():
    value, basis = convert_value(100, "PSI", "KPA")
    assert value == pytest.approx(689.4757293168)
    assert "multiply" in basis
    value, basis = convert_value(1, "FT", "M")
    assert value == pytest.approx(0.3048)
    value, basis = convert_value(0, "DEGC", "DEGF")
    assert value == pytest.approx(32.0)
    assert "temperature" in basis


def test_equivalence_check_uses_conversion():
    equivalent, converted, _ = values_equivalent(
        1000, "K/M3", 1.0, "G/CM3"
    )
    assert equivalent is True
    assert converted == pytest.approx(1.0)
    equivalent, _, _ = values_equivalent(100, "PSI", 100, "KPA")
    assert equivalent is False


def test_unknown_unit_pair_is_reported_not_guessed():
    with pytest.raises(LASUnknownUnitError):
        convert_value(1, "PSI", "FROBNIC")
    assert canonical_unit("kg/m3") == "KGM3"
    assert canonical_unit("G/CM3") == "GCM3"


# --------------------------------------------------------------------------
# Applicability lookup for a curve sample
# --------------------------------------------------------------------------

def test_applicable_parameter_inside_segment(handoff_las):
    lookup = handoff_las.parameter_applicable_at("PRE", 1050.0)
    assert lookup.occurrence.value == 100.0
    assert lookup.occurrence.segment == "A"
    assert "falls inside interval" in lookup.basis


def test_applicable_parameter_at_shared_boundary_hands_over(handoff_las):
    lookup = handoff_las.parameter_applicable_at("PRE", 1120.0)
    assert lookup.occurrence.value == 110.0
    assert lookup.occurrence.segment == "B"
    assert "shared boundary" in lookup.basis


def test_applicable_parameter_handles_depth_unit_conversion(handoff_las):
    # 3608.92 ft is approximately 1100 m, inside segment A.
    depth_ft = 1100.0 / 0.3048
    lookup = handoff_las.parameter_applicable_at(
        "PRE", depth_ft, depth_unit="FT"
    )
    assert lookup.occurrence.value == 100.0


def test_depth_outside_all_handovers_identifies_missing_handover(handoff_las):
    with pytest.raises(LASParameterHandoffError) as excinfo:
        handoff_las.parameter_applicable_at("PRE", 1260.0)
    error = excinfo.value
    assert error.reason == "no_coverage"
    assert "covers depth 1260" in error.missing
    assert len(error.occurrences) == 3


def test_missing_parameter_names_the_required_handover(handoff_las):
    with pytest.raises(LASParameterHandoffError) as excinfo:
        handoff_las.parameter_applicable_at("ABSENT", 1050.0)
    assert excinfo.value.reason == "not_found"


def test_last_value_fallback_is_flagged_unverified(handoff_las):
    # BHT has no depth ranges, so the applicable handover cannot be proven.
    with pytest.raises(LASParameterHandoffError):
        handoff_las.parameter_applicable_at("BHT", 1050.0)
    lookup = handoff_las.parameter_applicable_at(
        "BHT", 1050.0, fallback="last"
    )
    assert lookup.fallback is True
    assert lookup.occurrence.value == 40.0
    assert "unverified" in lookup.basis


def test_two_settings_in_one_batch_without_segment_is_ambiguous():
    las_text = """~V
VERS. 2.0:
WRAP. NO:
~W
STRT.M 1000:
STOP.M 1002:
STEP.M 1:
NULL. -999:
~PARAMETER INFORMATION
PRE .PSI 100.0 : FIRST PRESSURE SETTING BATCH 7
NOTE.      X : REMARK SANDWICHED BETWEEN THE TWO SETTINGS
PRE .PSI 120.0 : SECOND PRESSURE SETTING BATCH 7
~C
DEPT.M :
~A
1000
1001
1002
"""
    las = lasio.read(las_text)
    with pytest.raises(LASParameterHandoffError) as excinfo:
        las.parameter_applicable_at("PRE", 1001.0)
    error = excinfo.value
    assert error.reason == "ambiguous_occurrence"
    assert "segment or depth-range" in error.missing
    assert [o.value for o in error.occurrences] == [100.0, 120.0]


def test_external_batch_intervals_resolve_applicability():
    registry = ParameterRegistry()
    for index, (batch, value) in enumerate([("1", 100.0), ("2", 120.0)]):
        registry.add(
            ParameterOccurrence(
                mnemonic="PRE", unit="PSI", value=value, descr="",
                source_section="Parameter",
                source_section_title="~P",
                source_section_index=index,
                source_order=index + 1,
                batch=batch,
            )
        )
    registry.batch_intervals = {
        "1": (1000.0, 1100.0, "M"),
        "2": (1100.0, 1200.0, "M"),
    }
    lookup = registry.applicable_at("PRE", 1150.0)
    assert lookup.occurrence.value == 120.0
    assert "externally declared interval" in lookup.basis


# --------------------------------------------------------------------------
# SectionItems integration
# --------------------------------------------------------------------------

def test_merge_preserves_items_and_renumbers_suffixes():
    first = SectionItems(
        [HeaderItem("PRE", "PSI", 100.0, "first"),
         HeaderItem("NOTE", "", "ABC", "remark")]
    )
    second = SectionItems(
        [HeaderItem("PRE", "PSI", 120.0, "second"),
         HeaderItem("NOTE", "", "XYZ", "remark")]
    )
    registry = ParameterRegistry()
    order = stamp_section(
        first, "Parameter", "~PARAMETER INFORMATION", 0, 0, registry
    )
    assert order == 2
    order = merge_section_items(
        first, second, "Parameter", "~PARAMETER INFORMATION", 1, order, registry
    )
    assert order == 4
    assert first.keys() == ["PRE:1", "NOTE:1", "PRE:2", "NOTE:2"]
    assert [i.original_mnemonic for i in first] == ["PRE", "NOTE", "PRE", "NOTE"]
    assert [i.source_section_index for i in first] == [0, 0, 1, 1]
    assert [o.value for o in registry.for_mnemonic("PRE")] == [100.0, 120.0]
    assert [o.value for o in registry.for_mnemonic("NOTE")] == ["ABC", "XYZ"]


def test_single_section_files_keep_ordinary_access(handoff_las):
    # Ordinary single-mnemonic access must still work for unique names.
    assert handoff_las.params["BHT:1"].value == 35.5
    assert "BHT:1" in handoff_las.params


def test_ordinary_single_section_las_is_unaffected():
    las = lasio.read(egfn("sample.las"))
    assert las.params["BHT"].value == 35.5
    for item in las.params:
        assert item.source_section_index == 0
        assert item.source_order is not None
    assert len(las.parameter_occurrences("BHT")) == 1
    lookup = las.parameter_applicable_at("BHT", 1669.0)
    assert lookup.occurrence.value == 35.5
    assert "only declaration" in lookup.basis


def test_reading_a_second_file_resets_the_audit_trail(handoff_las):
    assert len(handoff_las.parameter_occurrences("PRE")) == 3
    handoff_las.read(egfn("sample.las"))
    assert handoff_las.parameter_occurrences("PRE") == []
    assert len(handoff_las.parameter_occurrences("BHT")) == 1
    assert handoff_las.params[0].source_section_index == 0


def test_batch_can_be_declared_in_section_comments():
    las_text = """~V
VERS. 2.0:
WRAP. NO:
~W
STRT.M 1000:
STOP.M 1002:
STEP.M 1:
NULL. -999:
~PARAMETER INFORMATION
# COMPLETION HANDOVER BATCH 2
PRE .PSI 100.0 : PRESSURE SETTING
~PARAMETER INFORMATION
# OPENING HANDOVER BATCH 1
PRE .PSI 90.0 : PRESSURE SETTING
~C
DEPT.M :
~A
1000
1001
1002
"""
    las = lasio.read(las_text)
    occurrences = las.parameter_occurrences("PRE")
    assert [o.batch for o in occurrences] == ["2", "1"]
    assert all(o.section_comments for o in occurrences)
    assert "BATCH 2" in occurrences[0].section_comments[0]
