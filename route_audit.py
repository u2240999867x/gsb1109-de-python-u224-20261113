# -*- coding: utf-8 -*-
"""Audit tool for priority-ordered version routing rules.

Each route binds a version spec to a "resolution contract". Routes are
evaluated in priority order (first match wins), so the admin may assume
the rules are mutually exclusive -- this tool checks whether that
assumption holds.

For a given rule set it reports:

* Pairs of routes whose specs overlap *and* resolve to different
  contracts, each with a concrete, verifiable example version.
* Whether the overlap is *effective* (actual routing lets the two rules
  compete for that version) or *shadowed* (a higher-priority rule
  captures every sample version before the pair can compete).
* Routes that are fully shadowed (dead) or match nothing at all.

Notes on semantics:

* Intersections are computed per combination, never assumed transitive:
  A and B each overlapping C does not imply A, B, C share a version.
  `RouteSet.common_witness()` checks an arbitrary combination directly.
* Every reported witness version can be fed back into
  `RouteSet.replay()` to re-run the actual routing decision and see
  exactly which two rules are contending.

Run ``python route_audit.py`` for a self-contained demo.
"""

from semantic_version import SimpleSpec, Version
from semantic_version.base import AnyOf, AllOf, BaseSpec, Range

__all__ = [
    'Route',
    'RouteSet',
    'Replay',
    'Conflict',
    'DeadRoute',
    'AuditReport',
]


def _coerce_version(version):
    if isinstance(version, Version):
        return version
    return Version(version)


def _iter_ranges(clause):
    """Yield every Range matcher inside a parsed spec clause."""
    if isinstance(clause, Range):
        yield clause
    elif isinstance(clause, (AnyOf, AllOf)):
        for subclause in clause.clauses:
            for rng in _iter_ranges(subclause):
                yield rng


def _candidate_versions(clauses):
    """Build candidate versions at every clause boundary.

    A spec's match set is a union of precedence intervals whose edges sit
    at (or just past) the Range targets, so sampling each target, its
    release/prerelease neighbours and its next patch/minor/major bumps
    hits every region a conjunction of such specs can carve out.
    """
    seeds = [
        Version('0.0.0'),
        Version(major=0, minor=0, patch=0, prerelease=('0',)),
    ]
    for clause in clauses:
        for rng in _iter_ranges(clause):
            target = rng.target
            release = Version(
                major=target.major, minor=target.minor, patch=target.patch)
            seeds.append(target)
            seeds.append(release)
            seeds.append(Version(
                major=release.major, minor=release.minor,
                patch=release.patch, prerelease=('0',)))
            if target.prerelease:
                seeds.append(Version(
                    major=target.major, minor=target.minor,
                    patch=target.patch,
                    prerelease=tuple(target.prerelease) + ('0',)))
            for bumped in (
                    release.next_patch(),
                    release.next_minor(),
                    release.next_major()):
                seeds.append(bumped)
                seeds.append(Version(
                    major=bumped.major, minor=bumped.minor,
                    patch=bumped.patch, prerelease=('0',)))
    seen = set()
    candidates = []
    for version in seeds:
        if version not in seen:
            seen.add(version)
            candidates.append(version)
    return candidates


class Route(object):
    """One routing rule: a version spec bound to a resolution contract."""

    def __init__(self, name, spec, contract):
        if not isinstance(spec, BaseSpec):
            spec = SimpleSpec(spec)
        self.name = name
        self.spec = spec
        self.contract = contract

    def matches(self, version):
        return self.spec.match(version)

    def __repr__(self):
        return 'Route(%r, %r, %r)' % (self.name, str(self.spec), self.contract)


class Replay(object):
    """The recorded routing decision for one version."""

    def __init__(self, routes, version):
        self.version = version
        self.steps = [(route, route.matches(version)) for route in routes]
        self.matched = [route for route, hit in self.steps if hit]
        self.winner = self.matched[0] if self.matched else None
        self.dispute = None
        if self.winner is not None:
            for other in self.matched[1:]:
                if other.contract != self.winner.contract:
                    self.dispute = (self.winner, other)
                    break

    def format(self):
        lines = ['Routing replay for version %s:' % self.version]
        for position, (route, hit) in enumerate(self.steps, 1):
            if not hit:
                verdict = 'no match'
            elif route is self.winner:
                verdict = 'MATCH  <- winner (highest priority)'
            else:
                verdict = 'MATCH  <- masked by %s' % self.winner.name
            lines.append('  %d. %-12s spec=%-24s contract=%-14s %s' % (
                position, route.name, str(route.spec), route.contract, verdict))
        if self.winner is None:
            lines.append('  -> no route matches; version is unrouted')
        elif self.dispute is not None:
            lines.append('  -> dispute: %s (wins) vs %s (contracts %r != %r)' % (
                self.dispute[0].name, self.dispute[1].name,
                self.dispute[0].contract, self.dispute[1].contract))
        else:
            lines.append('  -> no dispute: %s wins unopposed' % self.winner.name)
        return '\n'.join(lines)


class Conflict(object):
    """An overlap between two routes that emit different contracts.

    ``shadowed_by`` lists the routes with higher priority than the pair
    that capture the witness version; when empty the conflict is
    *effective* and replaying ``version`` shows the two routes fighting.
    """

    def __init__(self, higher, lower, version, shadowed_by):
        self.higher = higher
        self.lower = lower
        self.version = version
        self.shadowed_by = tuple(shadowed_by)

    @property
    def effective(self):
        return not self.shadowed_by

    def __repr__(self):
        return 'Conflict(%r vs %r at %s, shadowed_by=%r)' % (
            self.higher.name, self.lower.name, self.version,
            [route.name for route in self.shadowed_by])


class DeadRoute(object):
    """A route that never wins: either matches nothing or is covered."""

    KIND_EMPTY = 'empty'
    KIND_SHADOWED = 'shadowed'

    def __init__(self, route, kind, version=None, shadowed_by=()):
        self.route = route
        self.kind = kind
        self.version = version
        self.shadowed_by = tuple(shadowed_by)

    def __repr__(self):
        return 'DeadRoute(%r, %r)' % (self.route.name, self.kind)


class AuditReport(object):
    """Result of auditing a RouteSet."""

    def __init__(self, routeset, conflicts, dead_routes):
        self.routeset = routeset
        self.conflicts = list(conflicts)
        self.dead_routes = list(dead_routes)

    @property
    def ok(self):
        return not self.conflicts and not self.dead_routes

    def verify(self):
        """Re-check every reported witness against the actual specs."""
        routes = self.routeset.routes
        for conflict in self.conflicts:
            version = conflict.version
            if not (conflict.higher.matches(version)
                    and conflict.lower.matches(version)):
                return False
            lower_index = routes.index(conflict.lower)
            interferers = [
                route for route in routes[:lower_index]
                if route is not conflict.higher and route.matches(version)
            ]
            if conflict.effective:
                if interferers:
                    return False
            elif list(interferers) != list(conflict.shadowed_by):
                return False
        for dead in self.dead_routes:
            if dead.kind == DeadRoute.KIND_SHADOWED:
                if not dead.route.matches(dead.version):
                    return False
                if not all(route.matches(dead.version)
                           for route in dead.shadowed_by):
                    return False
        return True

    def format(self):
        routes = self.routeset.routes
        lines = [
            'Route audit: %d route(s), priority: %s' % (
                len(routes), ' > '.join(route.name for route in routes)),
        ]
        effective = [c for c in self.conflicts if c.effective]
        shadowed = [c for c in self.conflicts if not c.effective]
        lines.append('')
        lines.append('Effective conflicts (%d):' % len(effective))
        for conflict in effective:
            lines.append(
                '  %s (%s -> %r) vs %s (%s -> %r)' % (
                    conflict.higher.name, conflict.higher.spec,
                    conflict.higher.contract,
                    conflict.lower.name, conflict.lower.spec,
                    conflict.lower.contract))
            lines.append(
                '      witness %s: both match; replay shows %s winning, '
                '%s contending' % (
                    conflict.version, conflict.higher.name,
                    conflict.lower.name))
        lines.append('')
        lines.append('Shadowed overlaps (%d):' % len(shadowed))
        for conflict in shadowed:
            lines.append(
                '  %s vs %s overlap at %s, but %s captures it first' % (
                    conflict.higher.name, conflict.lower.name,
                    conflict.version,
                    ', '.join(route.name for route in conflict.shadowed_by)))
        lines.append('')
        lines.append('Dead routes (%d):' % len(self.dead_routes))
        for dead in self.dead_routes:
            if dead.kind == DeadRoute.KIND_EMPTY:
                lines.append('  %s: spec %s matches no sampled version' % (
                    dead.route.name, dead.route.spec))
            else:
                lines.append(
                    '  %s: never wins; e.g. %s is captured by %s' % (
                        dead.route.name, dead.version,
                        ', '.join(route.name for route in dead.shadowed_by)))
        return '\n'.join(lines)


class RouteSet(object):
    """An ordered set of routes; earlier routes have higher priority."""

    def __init__(self, routes):
        self.routes = list(routes)
        names = [route.name for route in self.routes]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate route names: %r' % names)

    def _route(self, name):
        for route in self.routes:
            if route.name == name:
                return route
        raise KeyError('Unknown route: %r' % name)

    def resolve(self, version):
        """Return the route that actually serves ``version`` (or None)."""
        version = _coerce_version(version)
        for route in self.routes:
            if route.matches(version):
                return route
        return None

    def replay(self, version):
        """Record the full routing decision for ``version``."""
        return Replay(self.routes, _coerce_version(version))

    def common_witness(self, *names):
        """Find one version matched by every named route, or None.

        Intersections are evaluated directly on the combination, so
        pairwise overlaps with a third rule never imply a triple hit.
        """
        routes = [self._route(name) for name in names]
        candidates = _candidate_versions(
            [route.spec.clause for route in routes])
        for candidate in candidates:
            if all(route.matches(candidate) for route in routes):
                return candidate
        return None

    def audit(self):
        """Check the rule set for cross-contract conflicts and dead rules."""
        candidates = _candidate_versions(
            [route.spec.clause for route in self.routes])
        conflicts = []
        for index, higher in enumerate(self.routes):
            for lower in self.routes[index + 1:]:
                if higher.contract == lower.contract:
                    continue
                lower_index = self.routes.index(lower)
                interferers = [
                    route for route in self.routes[:lower_index]
                    if route is not higher
                ]
                shadowed_hit = None
                for candidate in candidates:
                    if not (higher.matches(candidate)
                            and lower.matches(candidate)):
                        continue
                    covering = tuple(
                        route for route in interferers
                        if route.matches(candidate))
                    if not covering:
                        conflicts.append(Conflict(higher, lower, candidate, ()))
                        break
                    elif shadowed_hit is None:
                        shadowed_hit = Conflict(higher, lower, candidate, covering)
                else:
                    if shadowed_hit is not None:
                        conflicts.append(shadowed_hit)
        dead_routes = []
        for index, route in enumerate(self.routes):
            above = self.routes[:index]
            shadowed_hit = None
            for candidate in candidates:
                if not route.matches(candidate):
                    continue
                covering = tuple(
                    other for other in above if other.matches(candidate))
                if not covering:
                    break
                elif shadowed_hit is None:
                    shadowed_hit = (candidate, covering)
            else:
                if shadowed_hit is not None:
                    dead_routes.append(DeadRoute(
                        route, DeadRoute.KIND_SHADOWED,
                        version=shadowed_hit[0], shadowed_by=shadowed_hit[1]))
                else:
                    dead_routes.append(DeadRoute(route, DeadRoute.KIND_EMPTY))
        return AuditReport(self, conflicts, dead_routes)


def _demo():
    routeset = RouteSet([
        Route('legacy', '<1.0.0', 'contract-legacy'),
        Route('stable', '>=1.0.0,<2.0.0', 'contract-a'),
        Route('lts', '>=1.5.0,<2.5.0', 'contract-b'),
        Route('beta', '>=2.0.0,<3.0.0', 'contract-c'),
        Route('drift', '>=1.6.0,<1.9.0', 'contract-d'),
        Route('catchall', '*', 'contract-a'),
    ])
    report = routeset.audit()
    print(report.format())
    print()
    print('Report verifies against actual specs: %s' % report.verify())
    print()
    print('Pairwise vs triple intersection:')
    print('  stable & lts   ->', routeset.common_witness('stable', 'lts'))
    print('  beta & lts     ->', routeset.common_witness('beta', 'lts'))
    print('  stable & beta  ->', routeset.common_witness('stable', 'beta'))
    print('  all three      ->',
          routeset.common_witness('stable', 'beta', 'lts'))
    print()
    for conflict in report.conflicts:
        if conflict.effective:
            print(routeset.replay(conflict.version).format())
            break


if __name__ == '__main__':
    _demo()
