import os

import pytest

import lasio
from lasio.curve_identity import (
    CurveObservation,
    format_report,
    observations_from_las,
    parse_description,
    positions_compatible,
    resolve_identities,
    resolve_las_files,
    units_compatible,
)

test_dir = os.path.dirname(__file__)


def egfn(fn):
    return os.path.join(test_dir, "examples", fn)


TRIP_FILES = {
    "trip1": egfn("gr_identity/trip1.las"),
    "trip2": egfn("gr_identity/trip2.las"),
    "trip3": egfn("gr_identity/trip3.las"),
}


def load_observations():
    observations = []
    for trip_id, path in TRIP_FILES.items():
        las = lasio.read(path)
        observations.extend(observations_from_las(trip_id, las))
    return observations


def find_identity(identities, label):
    for identity in identities:
        if any(member.label == label for member in identity.members):
            return identity
    raise KeyError(label)


def test_description_tags_parsed():
    assert parse_description("Gamma Ray; POS=UPPER; SN=GR-101") == (
        "UPPER",
        "GR-101",
    )
    assert parse_description("plain description") == ("", "")


def test_unit_convertibility_rules():
    assert units_compatible("gAPI", "CPS")
    assert units_compatible("GAPI", "gapi")
    assert not units_compatible("gAPI", "MV")
    # An unrecorded unit never blocks a match by itself.
    assert units_compatible("", "gAPI")


def test_toolstring_position_rules():
    assert positions_compatible("UPPER", "UPPER")
    assert not positions_compatible("UPPER", "LOWER")
    assert positions_compatible("", "LOWER")


def test_three_trip_resolution():
    identities, pending, solution_count = resolve_identities(load_observations())

    assert solution_count == 2
    assert pending == ["trip2/GR[2]", "trip3/GR[1]"]

    confirmed = find_identity(identities, "trip1/GR[1]")
    assert confirmed.confirmed
    assert confirmed.serial == "GR-101"
    assert {member.label for member in confirmed.members} == {
        "trip1/GR[1]",
        "trip2/GR[1]",
    }

    # The two probes of trip 2 must never be one identity.
    upper = find_identity(identities, "trip2/GR[1]")
    lower = find_identity(identities, "trip2/GR[2]")
    assert upper is not lower
    for identity in identities:
        trips = [member.trip_id for member in identity.members]
        assert len(trips) == len(set(trips))

    # The mV curve cannot join either gamma identity: voltage is not
    # convertible into gAPI/CPS, so it forms its own single-trip identity.
    voltage = find_identity(identities, "trip3/GR[2]")
    assert voltage.members[0].unit == "MV"
    assert len(voltage.members) == 1
    assert not voltage.confirmed

    # The CPS curve is the ambiguous one: it can join the LOWER probe or
    # stand alone, hence it is pending.
    assert "trip3/GR[1]" in pending


def test_same_trip_probes_never_merge_even_without_serials():
    observations = [
        CurveObservation("t1", "GR[1]", "GR", "gAPI", "UPPER"),
        CurveObservation("t2", "GR[1]", "GR", "gAPI", "UPPER"),
        CurveObservation("t2", "GR[2]", "GR", "gAPI", "LOWER"),
    ]
    identities, pending, solution_count = resolve_identities(observations)
    assert solution_count == 1
    assert pending == []
    labels = sorted(
        member.label for identity in identities for member in identity.members
    )
    assert labels == ["t1/GR[1]", "t2/GR[1]", "t2/GR[2]"]
    identity_a = find_identity(identities, "t1/GR[1]")
    identity_b = find_identity(identities, "t2/GR[2]")
    assert identity_a is not identity_b
    assert {member.label for member in identity_a.members} == {
        "t1/GR[1]",
        "t2/GR[1]",
    }


def test_conflicting_serials_force_distinct_identities():
    observations = [
        CurveObservation("t1", "GR[1]", "GR", "gAPI", "UPPER", "SN-1"),
        CurveObservation("t2", "GR[1]", "GR", "gAPI", "UPPER", "SN-1"),
        CurveObservation("t2", "GR[2]", "GR", "gAPI", "LOWER", "SN-2"),
        CurveObservation("t3", "GR[1]", "GR", "gAPI", "", ""),
    ]
    identities, pending, solution_count = resolve_identities(observations)
    serials = sorted(identity.serial for identity in identities if identity.serial)
    assert serials == ["SN-1", "SN-2"]
    # Trip 3 has no position: it could be either probe, so it is pending.
    assert solution_count == 2
    assert pending == ["t3/GR[1]"]


def test_unique_pairing_has_no_pending_curves():
    observations = [
        CurveObservation("t1", "GR[1]", "GR", "gAPI", "UPPER", "SN-1"),
        CurveObservation("t2", "GR[1]", "GR", "gAPI", "UPPER", "SN-1"),
        CurveObservation("t2", "GR[2]", "GR", "gAPI", "LOWER", "SN-2"),
        CurveObservation("t3", "GR[1]", "GR", "gAPI", "LOWER", "SN-2"),
    ]
    identities, pending, solution_count = resolve_identities(observations)
    assert solution_count == 1
    assert pending == []
    assert find_identity(identities, "t1/GR[1]").serial == "SN-1"
    lower = find_identity(identities, "t2/GR[2]")
    assert lower.serial == "SN-2"
    assert {member.label for member in lower.members} == {
        "t2/GR[2]",
        "t3/GR[1]",
    }


def test_unit_mismatch_blocks_identity_despite_same_mnemonic():
    observations = [
        CurveObservation("t1", "GR[1]", "GR", "gAPI", "UPPER"),
        CurveObservation("t2", "GR[1]", "GR", "MV", "UPPER"),
    ]
    identities, pending, solution_count = resolve_identities(observations)
    assert solution_count == 1
    assert len(identities) == 2


def test_confirmed_serial_on_incompatible_pair_raises():
    observations = [
        CurveObservation("t1", "GR[1]", "GR", "gAPI", "UPPER", "SN-9"),
        CurveObservation("t2", "GR[1]", "GR", "MV", "UPPER", "SN-9"),
    ]
    with pytest.raises(ValueError):
        resolve_identities(observations)


def test_resolve_las_files_and_report():
    identities, pending, solution_count = resolve_las_files(TRIP_FILES)
    report = format_report(identities, pending, solution_count)
    assert "GR-101" in report
    assert "trip2/GR[2]" in report
    assert "trip3/GR[1]" in report
    assert "待核曲线" in report
