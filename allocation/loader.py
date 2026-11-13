"""Input loading and validation for allocation plans."""

import json

from .model import Collector, Source


class LoadError(ValueError):
    """Raised when the allocation input document is malformed."""


def _require(mapping, key, context):
    if key not in mapping:
        raise LoadError('missing %r in %s' % (key, context))
    return mapping[key]


def load_json(text):
    """Parse an allocation document from a JSON string."""
    return load_input(json.loads(text))


def load_input(data):
    """Build :class:`Source` and :class:`Collector` objects from raw data."""
    if not isinstance(data, dict):
        raise LoadError('top-level document must be a JSON object')

    raw_sources = _require(data, 'sources', 'document')
    raw_collectors = _require(data, 'collectors', 'document')
    if not isinstance(raw_sources, list) or not isinstance(raw_collectors, list):
        raise LoadError('"sources" and "collectors" must be lists')

    sources = []
    seen_source_ids = set()
    for index, raw in enumerate(raw_sources):
        context = 'sources[%d]' % index
        if not isinstance(raw, dict):
            raise LoadError('%s must be an object' % context)
        source_id = _require(raw, 'id', context)
        contract = _require(raw, 'contract_version', context)
        if source_id in seen_source_ids:
            raise LoadError('duplicate source id %r' % source_id)
        seen_source_ids.add(source_id)
        try:
            sources.append(Source(
                id=source_id,
                contract_version=contract,
                locked_collector=raw.get('locked_collector'),
            ))
        except Exception as error:
            raise LoadError(
                'source %r has invalid contract_version %r: %s'
                % (source_id, contract, error)
            )

    collectors = []
    seen_collector_ids = set()
    for index, raw in enumerate(raw_collectors):
        context = 'collectors[%d]' % index
        if not isinstance(raw, dict):
            raise LoadError('%s must be an object' % context)
        collector_id = _require(raw, 'id', context)
        npm_spec = _require(raw, 'npm_spec', context)
        capacity = _require(raw, 'capacity', context)
        can_read = _require(raw, 'can_read', context)
        if collector_id in seen_collector_ids:
            raise LoadError('duplicate collector id %r' % collector_id)
        seen_collector_ids.add(collector_id)
        if not isinstance(can_read, list):
            raise LoadError('%s.can_read must be a list' % context)
        unknown = set(can_read) - seen_source_ids
        if unknown:
            raise LoadError(
                'collector %r lists unknown sources: %s'
                % (collector_id, sorted(unknown))
            )
        try:
            collectors.append(Collector(
                id=collector_id,
                npm_spec=npm_spec,
                capacity=capacity,
                can_read=can_read,
            ))
        except Exception as error:
            raise LoadError(
                'collector %r has invalid declaration: %s'
                % (collector_id, error)
            )

    for source in sources:
        if (source.locked_collector
                and source.locked_collector not in seen_collector_ids):
            raise LoadError(
                'source %r locked to unknown collector %r'
                % (source.id, source.locked_collector)
            )

    return sources, collectors
