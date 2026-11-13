"""Allocation engine: locked assignments plus max-flow coverage."""

from .flow import Dinic
from .model import Assignment, Collector, Plan, Source, StoppedSource


class InvalidLockError(ValueError):
    """A locked assignment is incompatible with the current declarations."""

    def __init__(self, violations):
        self.violations = violations
        detail = '; '.join(violations)
        super(InvalidLockError, self).__init__(
            'Invalid locked assignments: %s' % detail
        )


def _check_locks(sources, collectors):
    collector_by_id = {collector.id: collector for collector in collectors}
    violations = []
    locked = {}
    lock_loads = {collector.id: 0 for collector in collectors}

    for source in sources:
        if not source.locked_collector:
            continue
        collector = collector_by_id.get(source.locked_collector)
        if collector is None:
            violations.append(
                'source %r locked to unknown collector %r'
                % (source.id, source.locked_collector)
            )
            continue
        if source.id not in collector.can_read:
            violations.append(
                'source %r locked to %r but it is not in its can_read list'
                % (source.id, collector.id)
            )
            continue
        if not collector.spec.match(source.version):
            violations.append(
                'source %r (%s) locked to %r but NpmSpec %r does not match '
                '(pre-release contracts need an explicit pre-release range)'
                % (source.id, source.contract_version,
                   collector.id, collector.npm_spec)
            )
            continue
        locked[source.id] = collector.id
        lock_loads[collector.id] += 1

    for collector in collectors:
        if lock_loads[collector.id] > collector.capacity:
            violations.append(
                'collector %r capacity %d exceeded by %d locked sources'
                % (collector.id, collector.capacity, lock_loads[collector.id])
            )

    if violations:
        raise InvalidLockError(violations)
    return locked, lock_loads


def plan_allocation(sources, collectors):
    """Compute a maximum-coverage allocation.

    Locked sources stay on their declared collector. Every other source is
    assigned to at most one compatible collector via maximum flow, so the
    result covers the largest feasible number of sources.
    """
    sources = list(sources)
    collectors = list(collectors)

    locked, lock_loads = _check_locks(sources, collectors)

    assignments = [
        Assignment(source.id, locked[source.id], locked=True)
        for source in sources if source.id in locked
    ]

    free_sources = [source for source in sources if source.id not in locked]
    candidates = {}
    for source in free_sources:
        candidates[source.id] = [
            collector for collector in collectors
            if collector.can_handle(source)
        ]

    source_index = {source.id: index for index, source in enumerate(free_sources)}
    collector_index = {
        collector.id: len(free_sources) + index
        for index, collector in enumerate(collectors)
    }
    source_node = len(free_sources) + len(collectors)
    sink_node = source_node + 1
    network = Dinic(sink_node + 1)

    edge_refs = {}
    for source in free_sources:
        network.add_edge(source_node, source_index[source.id], 1)
        for collector in candidates[source.id]:
            edge = network.add_edge(
                source_index[source.id],
                collector_index[collector.id],
                1,
            )
            edge_refs[(source.id, collector.id)] = edge

    for collector in collectors:
        remaining = collector.capacity - lock_loads[collector.id]
        network.add_edge(collector_index[collector.id], sink_node, remaining)

    network.max_flow(source_node, sink_node)

    assigned_pairs = set()
    for (source_id, collector_id), edge in edge_refs.items():
        if edge[1] == 0:
            assigned_pairs.add((source_id, collector_id))

    assigned_source_ids = {source_id for source_id, _ in assigned_pairs}
    for source_id, collector_id in sorted(assigned_pairs):
        assignments.append(Assignment(source_id, collector_id, locked=False))

    stopped = []
    for source in free_sources:
        if source.id in assigned_source_ids:
            continue
        if not candidates[source.id]:
            if source.is_prerelease:
                reason = (
                    'no collector declares a pre-release NpmSpec covering %s '
                    'and lists this source' % source.contract_version
                )
            else:
                reason = (
                    'no compatible collector (allow-list and NpmSpec %s)'
                    % source.contract_version
                )
        else:
            reason = (
                'all compatible collectors at capacity: %s'
                % ', '.join(sorted(c.id for c in candidates[source.id]))
            )
        stopped.append(StoppedSource(
            source.id, source.contract_version, reason,
        ))

    assignments.sort(key=lambda item: item.source_id)
    stopped.sort(key=lambda item: item.source_id)
    return Plan(
        assignments=assignments,
        stopped=stopped,
        collectors=collectors,
        sources=sources,
        locked_assignments=locked,
    )
