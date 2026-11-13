"""Tests for the collector allocation engine."""

import itertools
import json
import random

import pytest

from allocation import (
    Collector,
    InvalidLockError,
    Source,
    load_json,
    plan_allocation,
)
from allocation.flow import Dinic


def make(plan_data):
    sources = [
        Source(
            id=item['id'],
            contract_version=item['v'],
            locked_collector=item.get('lock'),
        )
        for item in plan_data['sources']
    ]
    collectors = [
        Collector(
            id=item['id'],
            npm_spec=item['spec'],
            capacity=item['cap'],
            can_read=item['read'],
        )
        for item in plan_data['collectors']
    ]
    return sources, collectors


BASIC = {
    'sources': [
        {'id': 's1', 'v': '1.2.3'},
        {'id': 's2', 'v': '1.5.0'},
        {'id': 's3', 'v': '2.1.0'},
        {'id': 's4', 'v': '2.0.0-rc.4'},
    ],
    'collectors': [
        {'id': 'c-old', 'spec': '^1.0.0', 'cap': 2,
         'read': ['s1', 's2', 's3', 's4']},
        {'id': 'c-new', 'spec': '>=2.0.0-rc.1 <3.0.0', 'cap': 1,
         'read': ['s3', 's4']},
    ],
}


def test_basic_maximum_coverage_and_capacity():
    sources, collectors = make(BASIC)
    plan = plan_allocation(sources, collectors)

    assert plan.covered_count == 3
    assignment = {item.source_id: item.collector_id for item in plan.assignments}
    assert assignment['s1'] == 'c-old'
    assert assignment['s2'] == 'c-old'
    assert assignment['s3'] == 'c-new'

    loads = plan.load_by_collector()
    assert loads['c-old'] <= 2
    assert loads['c-new'] <= 1
    assert len(set(assignment)) == len(assignment)


def test_prerelease_contract_not_treated_as_release():
    data = {
        'sources': [
            {'id': 'rc', 'v': '2.0.0-rc.4'},
        ],
        'collectors': [
            # Stable range: numeric range covers 2.0.0 but must not match rc.
            {'id': 'stable', 'spec': '^2.0.0', 'cap': 5, 'read': ['rc']},
        ],
    }
    plan = plan_allocation(*make(data))
    assert plan.covered_count == 0
    assert plan.stopped[0].source_id == 'rc'
    assert 'pre-release' in plan.stopped[0].reason


def test_explicit_prerelease_range_covers_prerelease_contract():
    data = {
        'sources': [
            {'id': 'rc', 'v': '2.0.0-rc.4'},
        ],
        'collectors': [
            {'id': 'beta', 'spec': '>=2.0.0-rc.1 <3.0.0', 'cap': 5,
             'read': ['rc']},
        ],
    }
    plan = plan_allocation(*make(data))
    assert plan.covered_count == 1
    assert plan.assignments[0].collector_id == 'beta'


def test_allow_list_gates_compatibility():
    data = {
        'sources': [
            {'id': 's1', 'v': '1.0.0'},
        ],
        'collectors': [
            {'id': 'c1', 'spec': '*', 'cap': 5, 'read': []},
        ],
    }
    plan = plan_allocation(*make(data))
    assert plan.covered_count == 0
    assert plan.stopped[0].reason.startswith('no compatible collector')


def test_locked_assignment_is_immovable():
    data = {
        'sources': [
            {'id': 's1', 'v': '1.0.0', 'lock': 'c-small'},
            {'id': 's2', 'v': '1.0.0'},
        ],
        'collectors': [
            {'id': 'c-small', 'spec': '^1.0.0', 'cap': 1,
             'read': ['s1', 's2']},
            {'id': 'c-big', 'spec': '^1.0.0', 'cap': 5,
             'read': ['s1', 's2']},
        ],
    }
    plan = plan_allocation(*make(data))
    assignment = {item.source_id: item for item in plan.assignments}
    assert assignment['s1'].collector_id == 'c-small'
    assert assignment['s1'].locked is True
    assert assignment['s2'].collector_id == 'c-big'
    assert plan.load_by_collector()['c-small'] == 1


def test_locked_source_consumes_capacity_and_forces_stoppage():
    data = {
        'sources': [
            {'id': 's1', 'v': '1.0.0', 'lock': 'c1'},
            {'id': 's2', 'v': '1.0.0'},
        ],
        'collectors': [
            {'id': 'c1', 'spec': '^1.0.0', 'cap': 1,
             'read': ['s1', 's2']},
        ],
    }
    plan = plan_allocation(*make(data))
    assert plan.covered_count == 1
    assert [item.source_id for item in plan.stopped] == ['s2']
    assert 'at capacity' in plan.stopped[0].reason


def test_invalid_lock_npm_spec_mismatch_raises():
    data = {
        'sources': [
            {'id': 'rc', 'v': '2.0.0-rc.1', 'lock': 'c1'},
        ],
        'collectors': [
            {'id': 'c1', 'spec': '^2.0.0', 'cap': 5, 'read': ['rc']},
        ],
    }
    with pytest.raises(InvalidLockError) as excinfo:
        plan_allocation(*make(data))
    assert 'rc' in str(excinfo.value)


def test_invalid_lock_capacity_overflow_raises():
    data = {
        'sources': [
            {'id': 's1', 'v': '1.0.0', 'lock': 'c1'},
            {'id': 's2', 'v': '1.0.0', 'lock': 'c1'},
        ],
        'collectors': [
            {'id': 'c1', 'spec': '^1.0.0', 'cap': 1,
             'read': ['s1', 's2']},
        ],
    }
    with pytest.raises(InvalidLockError):
        plan_allocation(*make(data))


def test_invalid_lock_not_in_allow_list_raises():
    data = {
        'sources': [
            {'id': 's1', 'v': '1.0.0', 'lock': 'c1'},
        ],
        'collectors': [
            {'id': 'c1', 'spec': '^1.0.0', 'cap': 5, 'read': []},
        ],
    }
    with pytest.raises(InvalidLockError):
        plan_allocation(*make(data))


def test_dinic_basic_max_flow():
    network = Dinic(4)
    network.add_edge(0, 1, 3)
    network.add_edge(0, 2, 2)
    network.add_edge(1, 3, 2)
    network.add_edge(2, 3, 3)
    assert network.max_flow(0, 3) == 4


def test_load_json_document():
    text = json.dumps({
        'sources': [{'id': 's1', 'contract_version': '1.2.3'}],
        'collectors': [
            {'id': 'c1', 'npm_spec': '^1.0.0', 'capacity': 2,
             'can_read': ['s1']},
        ],
    })
    sources, collectors = load_json(text)
    plan = plan_allocation(sources, collectors)
    assert plan.covered_count == 1
    assert plan.as_dict()['covered'] == 1


def _brute_force_optimum(sources, collectors):
    """Exhaustive maximum coverage, respecting locks and capacities."""
    compatible = {}
    for source in sources:
        compatible[source.id] = {
            collector.id for collector in collectors
            if collector.can_handle(source)
        }
    locked = {
        source.id: source.locked_collector
        for source in sources if source.locked_collector
    }
    for source_id, collector_id in locked.items():
        if collector_id not in compatible[source_id]:
            return None
    free_sources = [source.id for source in sources if source.id not in locked]
    collector_ids = [collector.id for collector in collectors]
    capacity = {collector.id: collector.capacity for collector in collectors}

    best = -1
    for option in itertools.product(
            [None] + collector_ids, repeat=len(free_sources)):
        loads = {collector_id: 0 for collector_id in collector_ids}
        for collector_id in locked.values():
            loads[collector_id] += 1
        if any(loads[cid] > capacity[cid] for cid in collector_ids):
            return None
        covered = len(locked)
        feasible = True
        for source_id, collector_id in zip(free_sources, option):
            if collector_id is None:
                continue
            if collector_id not in compatible[source_id]:
                feasible = False
                break
            loads[collector_id] += 1
            covered += 1
        if not feasible:
            continue
        if any(loads[cid] > capacity[cid] for cid in collector_ids):
            continue
        best = max(best, covered)
    return best


@pytest.mark.parametrize('seed', range(40))
def test_flow_matches_brute_force(seed):
    rng = random.Random(seed)
    source_count = rng.randint(1, 6)
    collector_count = rng.randint(1, 4)
    versions = ['1.0.0', '1.4.2', '2.0.0', '2.1.0-rc.3', '3.0.0']
    specs = ['^1.0.0', '^2.0.0', '>=2.0.0-rc.1 <3.0.0', '*', '~1.4.0']

    sources = []
    for index in range(source_count):
        sources.append(Source(
            id='s%d' % index,
            contract_version=rng.choice(versions),
        ))
    collectors = []
    for index in range(collector_count):
        readable = [
            source.id for source in sources if rng.random() < 0.7
        ]
        collectors.append(Collector(
            id='c%d' % index,
            npm_spec=rng.choice(specs),
            capacity=rng.randint(1, 3),
            can_read=readable,
        ))

    # Sprinkle valid locks on some runs.
    if seed % 2 == 0:
        for source in sources:
            options = [
                collector.id for collector in collectors
                if collector.can_handle(source)
            ]
            if options and rng.random() < 0.4:
                source.locked_collector = rng.choice(options)

    try:
        plan = plan_allocation(sources, collectors)
    except InvalidLockError:
        pytest.skip('randomized locks made the instance infeasible')

    expected = _brute_force_optimum(sources, collectors)
    if expected is None:
        pytest.fail('planner accepted an infeasible lock set')
    assert plan.covered_count == expected
    loads = plan.load_by_collector()
    for collector in collectors:
        assert loads[collector.id] <= collector.capacity
    assigned = [item.source_id for item in plan.assignments]
    assert len(assigned) == len(set(assigned))
