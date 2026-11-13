import itertools

import pytest

import mido
from mido.performance import find_performances, segment_performances


def test_finds_all_complete_non_overlapping_performances():
    template = [0, 1, 2]
    onsets = [
        0, 1, 2,
        10, 12, 14,
        20, 21,
    ]

    result = find_performances(template, onsets, rate_range=(0.8, 2.2))

    assert result.max_count == 2
    assert [(group.start_index, group.end_index)
            for group in result.performances] == [(0, 2), (3, 5)]
    assert [group.rate for group in result.performances] == [1.0, 2.0]
    assert result.ambiguity[0].possible_groups == frozenset({0})
    assert result.ambiguity[5].possible_groups == frozenset({1})
    assert result.ambiguity[6].possible_groups == frozenset()
    assert result.ambiguity[6].can_be_unused is True


def test_rejects_incomplete_prefix_and_rate_outside_range():
    template = [0, 1, 2]

    too_fast = find_performances(template, [0, 2, 4], rate_range=(0.5, 1.5))
    assert too_fast.max_count == 0
    assert too_fast.all_optimal_performances == ()

    incomplete = find_performances(template, [0, 1, 5],
                                   rate_range=(0.5, 1.5))
    assert incomplete.max_count == 0
    assert all(entry.can_be_unused for entry in incomplete.ambiguity)


def test_forbidden_region_rejects_candidate():
    template = [0, 1, 2]
    onsets = [0, 1, 2, 10, 11, 12]

    result = find_performances(
        template, onsets,
        rate_range=(0.9, 1.1),
        forbidden_regions=[(1.5, 2.5)],
    )

    assert result.max_count == 1
    assert result.performances[0].start_index == 3


def test_reports_every_group_in_each_optimal_solution():
    template = [0, 1]
    onsets = [0, 1, 2]

    result = find_performances(template, onsets, rate_range=1.0)

    assert result.max_count == 1
    assert len(result.all_optimal_performances) == 2
    assert set(result.ambiguity[1].possible_groups) == {0, 1}
    assert result.ambiguity[1].ambiguous is True
    assert result.ambiguity[1].can_be_unused is False
    assert result.ambiguity[0].can_be_unused is True
    assert result.ambiguity[2].can_be_unused is True

    solutions = result.optimal_solutions()
    assert len(solutions) == 2
    assert [solution[0].start_index for solution in solutions] == [0, 1]


def test_interval_template_and_tolerance():
    result = find_performances(
        [1, 1],
        [0, 1.02, 2.01, 10, 11, 12],
        rate_range=1.0,
        tolerance=0.05,
    )

    assert result.max_count == 2
    assert result.ambiguity[1].possible_positions == (1,)


def test_accepts_midi_messages_with_delta_times():
    messages = [
        mido.Message('note_on', note=60, time=0),
        mido.Message('note_off', note=60, time=1),
        mido.Message('note_on', note=62, time=0),
        mido.Message('note_off', note=62, time=1),
        mido.Message('note_on', note=64, time=0),
        mido.Message('note_off', note=64, time=1),
    ]

    result = segment_performances(
        messages,
        [mido.Message('note_on', note=60, time=0),
         mido.Message('note_on', note=62, time=1),
         mido.Message('note_on', note=64, time=1)],
        rate_range=1.0,
    )

    assert result.max_count == 1
    assert result.performances[0].note_indices == range(0, 3)


def test_alias_arguments_and_serialization():
    result = find_performances(
        template={'onsets': [0, 1], 'durations': [1, 1]},
        onsets=[(0, 1), (1, 1), (5, 1), (6, 1)],
        min_rate=0.9,
        max_rate=1.1,
        forbidden=[],
    )

    assert result.max_count == 2
    encoded = result.to_dict()
    assert encoded['max_count'] == 2
    assert encoded['ambiguity']['0']['possible_groups'] == [0]
    assert tuple(result) == (result.performances, result.ambiguity)


def test_ambiguity_matches_brute_force_optimal_solutions():
    onsets = [0, 2, 3, 5, 6, 7, 9, 11, 12, 14]

    result = find_performances([0, 1], onsets, rate_range=(0.99, 1.01))
    brute_candidates = [
        (index, index + 1) for index in range(len(onsets) - 1)
        if onsets[index + 1] - onsets[index] == 1
    ]
    optimal_solutions = []
    for size in range(len(brute_candidates), -1, -1):
        for subset in itertools.combinations(brute_candidates, size):
            if all(first[1] < second[0]
                   for first, second in zip(subset, subset[1:])):
                optimal_solutions.append(subset)
        if optimal_solutions:
            break

    assert result.max_count == len(optimal_solutions[0])
    assert len(result.all_optimal_performances) == len(
        set(itertools.chain.from_iterable(optimal_solutions)))

    group_by_interval = {
        (performance.start_index, performance.end_index):
        performance.group_id
        for performance in result.all_optimal_performances
    }
    expected_groups = [set() for _ in onsets]
    expected_ordinals = [set() for _ in onsets]
    expected_unused = [False] * len(onsets)

    for solution in optimal_solutions:
        covered = set()
        for ordinal, interval in enumerate(solution, start=1):
            group_id = group_by_interval[interval]
            for index in range(interval[0], interval[1] + 1):
                expected_groups[index].add(group_id)
                expected_ordinals[index].add(ordinal)
                covered.add(index)
        for index in set(range(len(onsets))) - covered:
            expected_unused[index] = True

    for index in range(len(onsets)):
        entry = result.ambiguity[index]
        assert set(entry.possible_groups) == expected_groups[index]
        assert set(entry.possible_group_ordinals) == expected_ordinals[index]
        assert entry.can_be_unused == expected_unused[index]


def test_natural_keyword_aliases():
    result = find_performances(
        template=[0, 1],
        keystrokes=[0, 1, 5, 6],
        tempo_range=(0.9, 1.1),
        forbidden_zones=[],
    )
    alias_result = find_performances(
        template=[0, 1],
        notes=[0, 1, 5, 6],
        min_tempo=0.9,
        max_tempo=1.1,
    )

    assert result.max_count == 2
    assert alias_result.max_count == 2
    assert result.all_groups[0].group_number == 1
    assert result.all_groups[0].tempo == result.all_groups[0].rate
    assert result.ambiguity[0].possible_group_numbers == (1,)
    assert result.ambiguity[-1] == result.ambiguity[3]
    encoded = result.to_dict()
    assert encoded['ambiguity']['0']['possible_group_numbers'] == [1]
    assert encoded['performances'][0]['group_number'] == 1
    assert encoded['performances'][0]['tempo'] == 1.0


def test_absolute_time_messages_and_single_note_rate():
    messages = [
        mido.Message('note_on', note=60, time=2),
        mido.Message('note_on', note=62, time=5),
    ]

    absolute_template = find_performances(
        messages, messages, rate_range=(0.9, 1.1),
        times_are_absolute=True,
    )
    assert absolute_template.max_count == 1
    assert absolute_template.performances[0].rate == 1.0

    single_note = find_performances(
        [0], messages, rate_range=(3.0, 4.0),
        times_are_absolute=True,
    )
    assert single_note.max_count == 2
    assert all(group.rate == 3.5 for group in single_note.performances)

    exact_single_note = find_performances(
        [0], messages, rate_range=2.0,
        times_are_absolute=True,
    )
    assert all(group.rate == 2.0
               for group in exact_single_note.performances)
    with pytest.raises(IndexError):
        single_note.ambiguity[-3]


def test_conflicting_aliases_raise():
    with pytest.raises(ValueError):
        find_performances([0, 1], note_onsets=[0, 1],
                          onsets=[0, 1], rate_range=1.0)
    with pytest.raises(ValueError):
        find_performances([0, 1], [0, 1],
                          rate_range=1.0, tempo_range=1.0)
    with pytest.raises(ValueError):
        find_performances([0, 1], [0, 1], rate_range=1.0,
                          forbidden=[], forbidden_zones=[])


def test_validates_inputs():
    with pytest.raises(ValueError):
        find_performances([0, 2, 1], [0, 2, 1], rate_range=1.0)
    with pytest.raises(ValueError):
        find_performances([0, 1], [0, 1], rate_range=(2, 1))
    with pytest.raises(ValueError):
        find_performances([0, 1], [0, 1], rate_range=1.0,
                          forbidden_regions=[(2, 1)])
    with pytest.raises(ValueError):
        find_performances([0, 1], [0, 1], rate_range=0)
    with pytest.raises(ValueError):
        find_performances([0, 1], [0, 1], rate_range=(0, 1))
    with pytest.raises(ValueError):
        find_performances([0, 1], [(0, -1), (1, 1)], rate_range=1.0)
    with pytest.raises(ValueError):
        find_performances([(0, -1), (1, 1)], [(0, -1), (1, 1)],
                          rate_range=1.0)
    with pytest.raises(TypeError):
        find_performances()
