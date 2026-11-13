"""Tests for lasio.relog: merging a re-log supplement LAS into a base LAS."""

import io

import numpy as np
import pytest

import lasio
from lasio.relog import Coverage, Provenance, RelogMerge, merge_relog


BASE_LAS = """~VERSION INFORMATION
 VERS.                  2.0 :   CWLS LOG ASCII STANDARD -VERSION 2.0
 WRAP.                  NO  :   ONE LINE PER DEPTH STEP
~WELL INFORMATION
 STRT.M         2500.0 :
 STOP.M         2504.0 :
 STEP.M            0.5 :
 NULL.          -999.25 :
 WELL.          BASE-1 :
~CURVE INFORMATION
 DEPT.M                 : DEPTH
 GR.GAPI                : GAMMA RAY
 RHOB.K/M3              : BULK DENSITY
 NPHI.V/V               : NEUTRON POROSITY
 CALI.MM                : CALIPER
 SP.MV                  : SPONTANEOUS POTENTIAL
~A  DEPT     GR     RHOB    NPHI    CALI    SP
2500.0   10.0    2.10    0.30    100.0   -50.0
2500.5   11.0    2.11    0.31    101.0   -51.0
2501.0   12.0    2.12    0.32    102.0   -52.0
2501.5   13.0    2.13    0.33    103.0   -53.0
2502.0   14.0    2.14    0.34    104.0   -54.0
2502.5   15.0    2.15    0.35    105.0   -55.0
2503.0   16.0    2.16    0.36    106.0   -56.0
2503.5   17.0    2.17    0.37    107.0   -57.0
2504.0   18.0    2.18    0.38    108.0   -58.0
"""

# Re-log of 2501.2-2503.7.  Boundaries fall between base samples.
# GR: valid re-log.  RHOB: temporary stop (nulls at 2502.2/2502.7).
# NPHI: measured but delivered empty.  DRHO: new curve, only in supplement.
# SP: not acquired by the re-log.  CALI: officially withdrawn (manifest).
SUPPLEMENT_LAS = """~VERSION INFORMATION
 VERS.                  2.0 :   CWLS LOG ASCII STANDARD -VERSION 2.0
 WRAP.                  NO  :   ONE LINE PER DEPTH STEP
~WELL INFORMATION
 STRT.M         2501.2 :
 STOP.M         2503.7 :
 STEP.M            0.5 :
 NULL.          -999.25 :
 WELL.          BASE-1 :
~CURVE INFORMATION
 DEPT.M                 : DEPTH
 GR.GAPI                : GAMMA RAY (RELOG)
 RHOB.K/M3              : BULK DENSITY (RELOG)
 NPHI.V/V               : NEUTRON POROSITY (RELOG)
 DRHO.K/M3              : DENSITY CORRECTION (RELOG)
~A  DEPT     GR     RHOB    NPHI    DRHO
2501.2  100.0    3.10    -999.25  0.01
2501.7  101.0    3.11    -999.25  0.02
2502.2  102.0    -999.25 -999.25  0.03
2502.7  103.0    -999.25 -999.25  0.04
2503.2  104.0    3.14    -999.25  0.05
2503.7  105.0    3.15    -999.25  0.06
"""

MANIFEST = {
    "GR": "relog",
    "RHOB": "relog",
    "NPHI": "relog",
    "DRHO": "relog",
    "CALI": "withdrawn",
}


def read(text):
    return lasio.read(io.StringIO(text))


@pytest.fixture()
def base():
    return read(BASE_LAS)


@pytest.fixture()
def supplement():
    return read(SUPPLEMENT_LAS)


@pytest.fixture()
def merge(base, supplement):
    return merge_relog(base, supplement, manifest=MANIFEST)


def test_interval_defaults_to_supplement_header(merge):
    layer = merge.layers[-1]
    assert layer.interval == (2501.2, 2503.7)


def test_relog_replaces_values_inside_interval(merge):
    result = merge.query("GR", 2501.7)
    assert result.value == 101.0
    assert result.provenance == Provenance.RELOG
    assert result.source == "supplement"


def test_boundary_between_base_samples_keeps_old_curve(merge):
    # Interval start 2501.2 falls between base samples 2501.0 and 2501.5;
    # interval stop 2503.7 falls between 2503.5 and 2504.0.
    assert merge.query("GR", 2501.0) == (12.0, Provenance.BASE, "base")
    assert merge.query("GR", 2504.0) == (18.0, Provenance.BASE, "base")
    # Samples just inside the boundary are re-logged...
    assert merge.query("GR", 2501.5).provenance == Provenance.RELOG
    assert merge.query("GR", 2503.5).provenance == Provenance.RELOG
    # ...but the old curve is not blanked out as a whole.
    _, values, provenance = merge.merged_curve("GR")
    assert not np.isnan(values).any()
    assert (provenance == Provenance.BASE).sum() == 4
    outside = values[provenance == Provenance.BASE]
    assert set(outside) == {10.0, 11.0, 12.0, 18.0}


def test_measured_empty(merge):
    result = merge.query("NPHI", 2502.0)
    assert np.isnan(result.value)
    assert result.provenance == Provenance.MEASURED_EMPTY
    assert result.source is None
    # Outside the interval the base value stands.
    assert merge.query("NPHI", 2500.5) == (0.31, Provenance.BASE, "base")


def test_not_acquired(merge):
    result = merge.query("SP", 2502.0)
    assert result.value == -54.0
    assert result.provenance == Provenance.NOT_ACQUIRED
    assert result.source == "base"
    assert merge.query("SP", 2500.0).provenance == Provenance.BASE


def test_withdrawn(merge):
    result = merge.query("CALI", 2502.0)
    assert np.isnan(result.value)
    assert result.provenance == Provenance.WITHDRAWN
    assert result.source is None
    # Withdrawal is limited to the re-log interval.
    assert merge.query("CALI", 2500.0) == (100.0, Provenance.BASE, "base")
    assert merge.query("CALI", 2504.0) == (108.0, Provenance.BASE, "base")


def test_pending_interval_from_temporary_stop(merge):
    assert merge.pending_intervals("RHOB") == [(2501.95, 2502.95)]
    # Inside the gap the base value is kept and flagged as undecided.
    result = merge.query("RHOB", 2502.5)
    assert result.value == 2.15
    assert result.provenance == Provenance.PENDING
    assert result.source == "base"
    # Either side of the gap the re-log values apply.
    assert merge.query("RHOB", 2501.7) == (3.11, Provenance.RELOG, "supplement")
    assert merge.query("RHOB", 2503.7) == (3.15, Provenance.RELOG, "supplement")
    # Curves without gaps report no pending intervals.
    assert merge.pending_intervals("GR") == []
    assert merge.pending_intervals("SP") == []


def test_supplement_only_curve(merge):
    assert merge.query("DRHO", 2502.2) == (0.03, Provenance.RELOG, "supplement")
    result = merge.query("DRHO", 2500.0)
    assert np.isnan(result.value)
    assert result.provenance == Provenance.BASE


def _snapshot(merge, mnemonics, depths):
    return {
        (m, round(float(d), 4)): merge.query(m, d) for m in mnemonics for d in depths
    }


def test_reingest_is_noop(base, supplement, merge):
    mnemonics = ["GR", "RHOB", "NPHI", "CALI", "SP", "DRHO"]
    depths = np.arange(2500.0, 2504.01, 0.1)
    before = _snapshot(merge, mnemonics, depths)
    pending_before = {m: merge.pending_intervals(m) for m in mnemonics}
    layer_count = len(merge.layers)

    # Deliver the exact same supplement file a second time.
    merge.ingest(read(SUPPLEMENT_LAS), manifest=MANIFEST)

    assert len(merge.layers) == layer_count
    assert {m: merge.pending_intervals(m) for m in mnemonics} == pending_before
    after = _snapshot(merge, mnemonics, depths)
    assert set(after) == set(before)
    for key in before:
        old, new = before[key], after[key]
        if np.isnan(old.value):
            assert np.isnan(new.value)
        else:
            assert old.value == new.value
        assert old.provenance == new.provenance
        assert old.source == new.source


def test_merge_is_idempotent_through_materialisation(base, merge, supplement):
    # Merging the supplement into the already-merged LAS changes nothing.
    remerged = merge_relog(merge.to_las(), supplement, manifest=MANIFEST)
    for mnemonic in ["GR", "RHOB", "NPHI", "CALI", "SP", "DRHO"]:
        grid, values, provenance = merge.merged_curve(mnemonic)
        grid2, values2, provenance2 = remerged.merged_curve(mnemonic)
        assert np.array_equal(grid, grid2)
        assert np.array_equal(values, values2, equal_nan=True)
        assert (provenance == provenance2).all()
        assert remerged.pending_intervals(mnemonic) == merge.pending_intervals(mnemonic)


def test_to_las_matches_queries(merge):
    out = merge.to_las()
    assert "DRHO" in out.curvesdict
    for mnemonic in ["GR", "RHOB", "NPHI", "CALI", "SP", "DRHO"]:
        grid, values, _ = merge.merged_curve(mnemonic)
        assert np.array_equal(out[mnemonic], values, equal_nan=True)
        assert np.array_equal(out.index, grid)


def test_manifest_defaults(base, supplement):
    # No manifest: supplement curves default to relog, the rest not-acquired.
    merge = merge_relog(base, supplement)
    assert merge.query("GR", 2501.7).provenance == Provenance.RELOG
    assert merge.query("SP", 2502.0).provenance == Provenance.NOT_ACQUIRED
    assert merge.query("CALI", 2502.0).provenance == Provenance.NOT_ACQUIRED


def test_unknown_coverage_action_rejected(base, supplement):
    with pytest.raises(ValueError):
        merge_relog(base, supplement, manifest={"GR": "bogus"})
