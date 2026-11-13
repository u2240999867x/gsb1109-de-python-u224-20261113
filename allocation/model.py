"""Domain objects for sources and collectors."""

from semantic_version import NpmSpec, Version


class Source(object):
    """A reporting source bound to one data-contract version.

    ``contract_version`` is the exact semver the source reports; pre-release
    identifiers (e.g. ``2.0.0-rc.1``) are preserved.
    """

    __slots__ = ('id', 'contract_version', 'version', 'locked_collector')

    def __init__(self, id, contract_version, locked_collector=None):
        self.id = id
        self.contract_version = contract_version
        self.version = Version(contract_version)
        self.locked_collector = locked_collector

    @property
    def is_prerelease(self):
        return bool(self.version.prerelease)

    def __repr__(self):
        return 'Source(%r, %r%s)' % (
            self.id,
            self.contract_version,
            ', locked=%r' % self.locked_collector if self.locked_collector else '',
        )


class Collector(object):
    """A collector declaring its NpmSpec, concurrency capacity and sources."""

    __slots__ = ('id', 'npm_spec', 'spec', 'capacity', 'can_read')

    def __init__(self, id, npm_spec, capacity, can_read):
        self.id = id
        self.npm_spec = npm_spec
        self.spec = NpmSpec(npm_spec)
        self.capacity = int(capacity)
        if self.capacity < 0:
            raise ValueError("Collector %r has negative capacity" % id)
        self.can_read = frozenset(can_read)

    def can_handle(self, source):
        """Compatibility requires both the allow-list entry and NpmSpec match.

        NpmSpec matching follows the npm pre-release rule: a range without an
        explicit pre-release boundary never matches a pre-release contract,
        even if its numeric range would otherwise cover it.
        """
        if source.id not in self.can_read:
            return False
        return self.spec.match(source.version)

    def __repr__(self):
        return 'Collector(%r, %r, capacity=%d, %d sources)' % (
            self.id, self.npm_spec, self.capacity, len(self.can_read),
        )


class Assignment(object):
    """A source assigned to a compatible collector."""

    __slots__ = ('source_id', 'collector_id', 'locked')

    def __init__(self, source_id, collector_id, locked):
        self.source_id = source_id
        self.collector_id = collector_id
        self.locked = locked

    def __eq__(self, other):
        if not isinstance(other, Assignment):
            return NotImplemented
        return (
            self.source_id == other.source_id
            and self.collector_id == other.collector_id
            and self.locked == other.locked
        )

    def __repr__(self):
        return 'Assignment(%r -> %r%s)' % (
            self.source_id, self.collector_id, ' locked' if self.locked else '',
        )


class StoppedSource(object):
    """A source that no feasible collector can keep collecting."""

    __slots__ = ('source_id', 'contract_version', 'reason')

    def __init__(self, source_id, contract_version, reason):
        self.source_id = source_id
        self.contract_version = contract_version
        self.reason = reason

    def __repr__(self):
        return 'StoppedSource(%r, %r, %r)' % (
            self.source_id, self.contract_version, self.reason,
        )


class Plan(object):
    """Result of an allocation run."""

    def __init__(self, assignments, stopped, collectors, sources,
                 locked_assignments, invalid_locks=None):
        self.assignments = assignments
        self.stopped = stopped
        self.collectors = collectors
        self.sources = sources
        self.locked_assignments = locked_assignments
        self.invalid_locks = invalid_locks or []

    @property
    def covered_count(self):
        return len(self.assignments)

    @property
    def stopped_count(self):
        return len(self.stopped)

    def load_by_collector(self):
        loads = {collector.id: 0 for collector in self.collectors}
        for assignment in self.assignments:
            loads[assignment.collector_id] += 1
        return loads

    def as_dict(self):
        return {
            'covered': self.covered_count,
            'stopped_count': self.stopped_count,
            'assignments': [
                {
                    'source': item.source_id,
                    'collector': item.collector_id,
                    'locked': item.locked,
                }
                for item in self.assignments
            ],
            'stopped': [
                {
                    'source': item.source_id,
                    'contract_version': item.contract_version,
                    'reason': item.reason,
                }
                for item in self.stopped
            ],
            'invalid_locks': list(self.invalid_locks),
        }
