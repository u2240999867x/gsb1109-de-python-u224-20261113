# -*- coding: utf-8 -*-
"""Plan the cheapest upgrade path through a set of record transformations.

A cold archive stores an old record at some version.  Each transformation
declares the versions it accepts, the version it produces, the fields it
permanently drops, and its cost.  Two intermediate states with the same
version are *not* interchangeable when they retain different fields, so the
search state is ``(version, retained_fields)`` rather than the version alone.

Re-reading the source record is expensive.  The planner therefore relies only
on transformation metadata and on the field set carried by each state.
"""

import argparse
import heapq
import io
import json
import sys

from .base import BaseSpec, SimpleSpec, Version


class UpgradeError(ValueError):
    """Base class for failures raised by the upgrade planner."""

    reason = 'upgrade_failed'

    def __init__(self, message, required_fields=()):
        super(UpgradeError, self).__init__(message)
        self.required_fields = frozenset(required_fields)


class VersionGapError(UpgradeError):
    """No chain of transformations connects the start to the target version."""

    reason = 'version_gap'


class RequiredFieldLostError(UpgradeError):
    """The version is reachable, but every path loses a required field."""

    reason = 'required_field_lost'


class UpgradePath(object):
    """A cheapest sequence of transformations.

    :ivar start_version: Version the archived record is read at.
    :ivar target_version: Contract version the record must reach.
    :ivar required_fields: Fields that must still be present at the end.
    :ivar steps: Accepted :class:`Transformation` objects, in order.
    :ivar cost: Sum of the costs of ``steps``.
    """

    def __init__(self, start_version, target_version, required_fields, steps):
        self.start_version = start_version
        self.target_version = target_version
        self.required_fields = frozenset(required_fields)
        self.steps = tuple(steps)
        self.cost = sum(step.cost for step in self.steps)

    @property
    def versions(self):
        """Versions visited, starting with the archived version."""
        return (self.start_version,) + tuple(
            step.output_version for step in self.steps)

    def __repr__(self):
        return '%s(%r -> %r, %d steps, cost=%r)' % (
            self.__class__.__name__, str(self.start_version),
            str(self.target_version), len(self.steps), self.cost,
        )

    def describe(self):
        """Return a human-readable summary of the path."""
        if not self.steps:
            return 'already at %s; no transformation needed' % (
                str(self.target_version),)
        parts = ['%s' % (str(self.start_version),)]
        for step in self.steps:
            parts.append('%s [cost=%s, drops=%s] -> %s' % (
                step.name, step.cost,
                ','.join(sorted(step.drops_fields)) or '-',
                str(step.output_version),
            ))
        return ' | '.join(parts) + ' (total cost=%s)' % (self.cost,)

    def as_dict(self):
        """Return a JSON-serialisable representation."""
        return {
            'status': 'ok',
            'start_version': str(self.start_version),
            'target_version': str(self.target_version),
            'required_fields': sorted(self.required_fields),
            'cost': self.cost,
            'versions': [str(version) for version in self.versions],
            'steps': [step.as_dict() for step in self.steps],
        }


def _coerce_fields(value):
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset((value,))
    return frozenset(value)


class Transformation(object):
    """A single upgrade step.

    :param name: Identifier used in reports and errors.
    :param input_versions: Versions this step accepts; a version string, a
        :class:`~semantic_version.Version`, or an iterable of either.
    :param output_version: Version produced by the step.
    :param cost: Non-negative cost; defaults to ``1``.
    :param drops_fields: Fields permanently lost by the step.
    :param accepts: A SemVer range (``BaseSpec`` or string) accepted by the
        step, in addition to ``input_versions``.  Every transformation output
        is a concrete version, so ranges keep the search space finite.
    """

    def __init__(self, name, input_versions=None, output_version=None,
                 cost=1, drops_fields=(), accepts=None):
        if not isinstance(name, str) or not name:
            raise ValueError('Transformation name must be a non-empty string')
        if input_versions is None:
            input_versions = ()
        elif isinstance(input_versions, (str, Version)):
            input_versions = (input_versions,)

        accepted = []
        for version in input_versions:
            version = _coerce_version(version)
            if version not in accepted:
                accepted.append(version)
        if accepts is not None:
            accepts = _coerce_spec(accepts)
        if not accepted and accepts is None:
            raise ValueError(
                'Transformation %r needs accepted input version(s) or range'
                % (name,))

        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ValueError(
                'Transformation %r cost must be a number, not %r' % (
                    name, cost))
        if cost < 0:
            raise ValueError(
                'Transformation %r cost must not be negative, got %r' % (
                    name, cost))

        self.name = name
        self.input_versions = tuple(accepted)
        self.accepts = accepts
        self.output_version = _coerce_version(output_version)
        self.cost = cost
        self.drops_fields = _coerce_fields(drops_fields)

    @classmethod
    def from_dict(cls, data):
        """Build a transformation from a mapping, tolerating key aliases."""
        name = data.get('name', data.get('id'))
        inputs = data.get(
            'input_versions',
            data.get('inputs', data.get('accepted_versions')))
        accepts = data.get('accepts', data.get('input_spec'))
        output = data.get(
            'output_version',
            data.get('output', data.get('produces')))
        cost = data.get('cost', data.get('weight', 1))
        drops = data.get(
            'drops_fields',
            data.get('drops', data.get('lost_fields', ())))
        return cls(name, inputs, output, cost, drops, accepts)

    def applies_to(self, version):
        """Whether this step accepts ``version``."""
        if version in self.input_versions:
            return True
        return self.accepts is not None and self.accepts.match(version)

    def as_dict(self):
        result = {
            'name': self.name,
            'input_versions': [str(version)
                               for version in self.input_versions],
            'output_version': str(self.output_version),
            'cost': self.cost,
            'drops_fields': sorted(self.drops_fields),
        }
        if self.accepts is not None:
            result['accepts'] = str(self.accepts)
        return result

    def __repr__(self):
        return '%s(%r, %r -> %r, cost=%r, drops=%r)' % (
            self.__class__.__name__, self.name, self.input_versions,
            str(self.output_version), self.cost, sorted(self.drops_fields),
        )


def _coerce_version(version):
    if isinstance(version, Version):
        return version
    if isinstance(version, str):
        try:
            return Version(version)
        except ValueError as error:
            raise ValueError('Invalid version %r: %s' % (version, error))
    raise ValueError(
        'Expected a version string or Version, got %r' % (version,))


def _coerce_spec(spec):
    if isinstance(spec, BaseSpec):
        return spec
    if isinstance(spec, str):
        try:
            return SimpleSpec(spec)
        except ValueError as error:
            raise ValueError('Invalid version range %r: %s' % (spec, error))
    raise ValueError(
        'Expected a version range string or BaseSpec, got %r' % (spec,))


def _state_key(version, fields):
    """A total, hashable ordering key for a search state."""
    return (version.major, version.minor, version.patch,
            version.prerelease, version.build, tuple(sorted(fields)))


def _version_from_key(key):
    return Version(major=key[0], minor=key[1], patch=key[2],
                   prerelease=key[3], build=key[4])


def _check_source(required_fields, source_fields):
    missing = required_fields - source_fields
    if missing:
        raise RequiredFieldLostError(
            'required field(s) already lost in the archived record: %s' % (
                ', '.join(sorted(missing)),),
            required_fields=missing)


def plan_upgrade(start_version, target_version, transformations,
                 required_fields=(), source_fields=None):
    """Return the cheapest :class:`UpgradePath`.

    :param start_version: Version of the archived record.
    :param target_version: Contract version the record must reach.
    :param transformations: Iterable of :class:`Transformation` (mappings are
        accepted and converted).
    :param required_fields: Fields that must still be present at the end.
    :param source_fields: Fields present in the archived record.  Defaults to
        ``required_fields``: the planner assumes the source still holds every
        field that must survive, and never needs to read the source file.
    :raises VersionGapError: when no chain of transformations leads from the
        start version to the target version.
    :raises RequiredFieldLostError: when the target version is reachable, but
        every path to it permanently drops a required field (or the source
        already lacks one).
    """
    start = _coerce_version(start_version)
    target = _coerce_version(target_version)
    required = _coerce_fields(required_fields)
    if source_fields is None:
        initial_fields = required
    else:
        initial_fields = _coerce_fields(source_fields)
    _check_source(required, initial_fields)

    steps = [step if isinstance(step, Transformation)
             else Transformation.from_dict(step)
             for step in transformations]

    if start == target:
        return UpgradePath(start, target, required, ())

    if not _version_is_reachable(start, target, steps):
        raise VersionGapError(
            'target version %s is unreachable: no chain of transformations '
            'leads from %s to it' % (str(target), str(start)))

    # Dijkstra over (version, retained fields).  Keeping the field set in the
    # key is essential: two states at the same version with different fields
    # are different, and a cheaper path that lost a required field is unusable.
    start_key = _state_key(start, initial_fields)
    distances = {start_key: 0}
    previous = {}
    queue = [(0, 0, start_key)]
    counter = 1

    while queue:
        distance, _, key = heapq.heappop(queue)
        if distance != distances.get(key):
            continue  # Stale entry superseded by a cheaper route.
        version = _version_from_key(key)
        fields = frozenset(key[5])

        if version == target:
            return _rebuild_path(start, target, required, previous, key)

        for step in steps:
            if not step.applies_to(version):
                continue
            remaining = fields - step.drops_fields
            if not required <= remaining:
                continue  # This step would drop a field that must survive.

            next_key = _state_key(step.output_version, remaining)
            next_distance = distance + step.cost
            if next_distance < distances.get(next_key, float('inf')):
                distances[next_key] = next_distance
                previous[next_key] = (key, step)
                heapq.heappush(
                    queue, (next_distance, counter, next_key))
                counter += 1

    raise RequiredFieldLostError(
        'target version %s is reachable, but every path permanently drops '
        'at least one required field from: %s' % (
            str(target), ', '.join(sorted(required))),
        required_fields=required)


def _version_is_reachable(start, target, transformations):
    """Whether transformations alone connect ``start`` to ``target``.

    Fields are ignored here; this only distinguishes a version gap from a
    required-field loss.
    """
    seen = {start}
    queue = [start]
    while queue:
        version = queue.pop()
        for step in transformations:
            if not step.applies_to(version):
                continue
            neighbour = step.output_version
            if neighbour == target:
                return True
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return False


def _rebuild_path(start, target, required, previous, key):
    steps = []
    while key in previous:
        key, step = previous[key]
        steps.append(step)
    steps.reverse()
    return UpgradePath(start, target, required, steps)


#: Backwards-friendly aliases for callers that prefer a descriptive name.
find_upgrade_path = plan_upgrade


def _read_document(path):
    if path == '-':
        return json.load(sys.stdin)
    with io.open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


_MISSING = object()


def _document_value(document, *names, **kwargs):
    for name in names:
        if name in document:
            return document[name]
    default = kwargs.get('default', _MISSING)
    if default is not _MISSING:
        return default
    raise ValueError('Missing field: %s' % (names[0],))


def _plan_from_document(document):
    start = _document_value(
        document, 'start_version', 'start', 'source_version', 'source')
    target = _document_value(
        document, 'target_version', 'target', 'contract_version', 'contract')
    required = _document_value(
        document, 'required_fields', 'required', 'must_keep', default=())
    source_fields = _document_value(
        document, 'source_fields', 'present_fields', default=None)
    transformations = _document_value(
        document, 'transformations', 'upgrades', 'steps')
    return plan_upgrade(start, target, transformations, required,
                        source_fields)


def _write_document(document, path):
    text = json.dumps(document, ensure_ascii=False, indent=2,
                      sort_keys=False) + '\n'
    if path == '-':
        sys.stdout.write(text)
    else:
        with io.open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m semantic_version.upgrade',
        description='Find the cheapest upgrade path between record versions.')
    parser.add_argument(
        'input', nargs='?', default='-',
        help='JSON input file, or - for stdin (default)')
    parser.add_argument(
        '--output', default='-',
        help='JSON output file, or - for stdout (default)')
    args = parser.parse_args(argv)

    try:
        document = _read_document(args.input)
    except (IOError, OSError, ValueError) as error:
        _write_document({'status': 'error', 'reason': 'invalid_input',
                         'detail': str(error)}, args.output)
        return 1

    try:
        path = _plan_from_document(document)
    except UpgradeError as error:
        result = {
            'status': 'no_path',
            'reason': error.reason,
            'detail': str(error),
        }
        if error.required_fields:
            result['fields'] = sorted(error.required_fields)
        _write_document(result, args.output)
        return 2
    except (KeyError, TypeError, ValueError) as error:
        _write_document({'status': 'error', 'reason': 'invalid_input',
                         'detail': str(error)}, args.output)
        return 1

    _write_document(path.as_dict(), args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
