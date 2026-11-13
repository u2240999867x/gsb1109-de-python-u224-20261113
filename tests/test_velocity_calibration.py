# SPDX-FileCopyrightText: 2026 mido contributors
#
# SPDX-License-Identifier: MIT

from pytest import raises

from mido.velocity_calibration import (
    CalibrationConflict,
    CalibrationRecord,
    ForceRange,
    VelocityTable,
    compare_velocity,
)


def make_table():
    # Overlapping force ranges are allowed, including for one velocity.
    return VelocityTable([
        (ForceRange(0, 20), 40),
        (ForceRange(15, 40), 64),
        (ForceRange(35, 70), 64),
        (ForceRange(60, 100), 100),
    ])


def test_force_range_contains_endpoints():
    force_range = ForceRange(10, 20)
    assert 10 in force_range
    assert 15 in force_range
    assert 20 in force_range
    assert 9 not in force_range
    assert 21 not in force_range


def test_force_range_validation():
    with raises(ValueError):
        ForceRange(5, 4)
    with raises(TypeError):
        ForceRange('1', 2)


def test_force_range_is_immutable():
    force_range = ForceRange(1, 2)
    with raises(ValueError):
        force_range.low = 3
    assert hash(force_range) == hash(ForceRange(1, 2))
    assert force_range == ForceRange(1, 2)
    assert force_range != ForceRange(1, 3)


def test_intersection_and_hull():
    assert ForceRange(0, 10).intersection(ForceRange(5, 15)) == \
        ForceRange(5, 10)
    # Touching endpoints intersect at a single point.
    assert ForceRange(0, 10).intersection(ForceRange(10, 20)) == \
        ForceRange(10, 10)
    assert ForceRange(0, 10).intersection(ForceRange(11, 20)) is None
    assert ForceRange(0, 10).union_hull(ForceRange(8, 20)) == \
        ForceRange(0, 20)


def test_strict_separation():
    assert ForceRange(20, 30).is_strictly_above(ForceRange(0, 10))
    # Ranges sharing an endpoint are not strictly separated.
    assert not ForceRange(10, 30).is_strictly_above(ForceRange(0, 10))
    assert not ForceRange(5, 30).is_strictly_above(ForceRange(0, 10))


def test_table_basic_access():
    table = make_table()
    assert len(table) == 4
    assert 64 in table
    assert 65 not in table
    assert table.velocities == (40, 64, 100)
    assert table.records[1] == CalibrationRecord(ForceRange(15, 40), 64)


def test_ranges_for_keeps_overlapping_records():
    table = make_table()
    assert table.ranges_for(64) == (
        ForceRange(15, 40), ForceRange(35, 70))
    with raises(KeyError):
        table.ranges_for(99)


def test_force_range_for_merges_into_interval():
    table = make_table()
    # The two ranges for 64 cannot be distinguished, so the whole span
    # between the lowest and highest provable force is returned.
    assert table.force_range_for(64) == ForceRange(15, 70)
    assert table.force_range_for(40) == ForceRange(0, 20)


def test_velocities_for_includes_overlapping_reports():
    table = make_table()
    # Both records covering force 38 report the same velocity 64.
    assert table.velocities_for(38) == (64,)
    # Force 18 is covered by two records with different velocities.
    assert table.velocities_for(18) == (40, 64)
    assert table.velocities_for(65) == (64, 100)
    assert table.velocities_for(101) == ()


def test_add_record():
    table = VelocityTable()
    table.add(ForceRange(0, 10), 50)
    assert len(table) == 1
    assert table.force_range_for(50) == ForceRange(0, 10)
    with raises(ValueError):
        table.add(ForceRange(0, 1), 128)
    with raises(TypeError):
        table.add((0, 1), 50)


def test_invalid_record_inputs():
    with raises(TypeError):
        VelocityTable([(ForceRange(0, 1), '64')])
    with raises(ValueError):
        VelocityTable([(ForceRange(0, 1), -1)])
    with raises(TypeError):
        VelocityTable([42])


def test_monotonic_table_passes():
    table = make_table()
    assert table.is_monotonic
    table.check_monotonic()  # must not raise


def test_conflicting_table_is_detected():
    table = VelocityTable([
        (ForceRange(0, 20), 64),
        (ForceRange(40, 60), 40),
    ])
    assert not table.is_monotonic
    weaker, stronger = table.find_conflict()
    assert weaker == CalibrationRecord(ForceRange(0, 20), 64)
    assert stronger == CalibrationRecord(ForceRange(40, 60), 40)
    with raises(CalibrationConflict) as info:
        table.check_monotonic()
    assert info.value.weaker.velocity == 64
    assert info.value.stronger.velocity == 40


def test_overlapping_ranges_never_conflict():
    # The stronger range is not strictly above the weaker one, so even
    # inverted velocities can both be true depending on the actual force.
    table = VelocityTable([
        (ForceRange(0, 30), 80),
        (ForceRange(20, 50), 20),
    ])
    assert table.is_monotonic
    # Touching endpoints overlap at one force and do not conflict either.
    touching = VelocityTable([
        (ForceRange(0, 30), 80),
        (ForceRange(30, 50), 20),
    ])
    assert touching.is_monotonic


def test_conflict_among_three_records():
    table = VelocityTable([
        (ForceRange(0, 10), 50),
        (ForceRange(20, 30), 70),
        (ForceRange(40, 50), 60),
    ])
    weaker, stronger = table.find_conflict()
    # The 70 record is strictly above the 50 one but above the 60 record
    # with a higher velocity, so the first violation is 70 vs 60.
    assert weaker.velocity == 70
    assert stronger.velocity == 60


def test_compare_same_velocity_separated_ranges():
    table_a = VelocityTable([(ForceRange(60, 90), 64)])
    table_b = VelocityTable([(ForceRange(10, 30), 64)])
    # Both report 64, but the provable ranges do not overlap.
    assert compare_velocity(64, table_a, 64, table_b) == 1
    assert compare_velocity(64, table_b, 64, table_a) == -1


def test_compare_same_velocity_overlapping_ranges_unknown():
    table_a = VelocityTable([(ForceRange(20, 70), 64)])
    table_b = VelocityTable([(ForceRange(40, 80), 64)])
    assert compare_velocity(64, table_a, 64, table_b) == 0


def test_compare_touching_ranges_unknown():
    table_a = VelocityTable([(ForceRange(30, 60), 64)])
    table_b = VelocityTable([(ForceRange(0, 30), 64)])
    assert compare_velocity(64, table_a, 64, table_b) == 0


def test_compare_different_velocities_with_overlap_unknown():
    # Higher code does not prove a harder strike when ranges overlap.
    table_a = VelocityTable([(ForceRange(10, 50), 80)])
    table_b = VelocityTable([(ForceRange(40, 90), 60)])
    assert compare_velocity(80, table_a, 60, table_b) == 0


def test_compare_different_velocities_separated():
    table_a = VelocityTable([(ForceRange(70, 100), 100)])
    table_b = VelocityTable([(ForceRange(0, 30), 40)])
    assert compare_velocity(100, table_a, 40, table_b) == 1
    assert compare_velocity(40, table_b, 100, table_a) == -1


def test_compare_with_merged_intervals():
    table_a = VelocityTable([
        (ForceRange(15, 40), 64),
        (ForceRange(35, 70), 64),
    ])
    table_b = VelocityTable([(ForceRange(80, 100), 64)])
    # Device A's provable interval is the hull of its overlapping rows.
    assert compare_velocity(64, table_b, 64, table_a) == 1
    assert compare_velocity(64, table_a, 64, table_b) == -1


def test_compare_validates_tables():
    table = VelocityTable([(ForceRange(0, 10), 64)])
    with raises(TypeError):
        compare_velocity(64, table, 64, object())
