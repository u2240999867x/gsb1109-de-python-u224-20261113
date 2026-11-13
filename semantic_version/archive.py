# -*- coding: utf-8 -*-
"""Calendar planning for monthly archives and other long-lived artifacts.

The planner answers a deceptively simple question: on each day of a calendar
window, which *exact* artifact can be used by every consumer?

A consumer does not necessarily have one permanent version range.  It publishes
dated support declarations, and a declaration only applies during its own
validity interval.  The release catalog has the mirror problem: a build may be
published later than the range starts, and it may be withdrawn before the range
ends.  Consequently, the fact that a candidate exists at the beginning AND at
the end of a window does not mean that the same candidate exists throughout the
window.  The planner therefore intersects candidates for every day (or every
calendar month), never just at the endpoints.

The public entry points are:

``SupportDeclaration``
    A consumer's dated support range.
``Artifact``
    One exact build in the release catalog.
``Withdrawal``
    A dated catalog withdrawal that may cover several builds.
``ArchivePlanner``
    The planner itself.
``plan_archive``
    A convenience wrapper around ``ArchivePlanner.from_config(...).plan()``.

A JSON configuration can be planned from the command line with::

    python -m semantic_version.archive --input plan.json --format text
"""

import datetime
import json
import sys

from .base import BaseSpec, SimpleSpec, Version


_DAY = datetime.timedelta(days=1)
_MIN_DATE = datetime.date.min
_MAX_DATE = datetime.date.max


def _parse_date(value, field='date', month_boundary='start'):
    """Convert a date-like value to :class:`datetime.date`.

    ``month_boundary`` controls how a ``YYYY-MM`` value is expanded:

    ``'start'``
        The first day of the month.
    ``'end'``
        The last day of the month.
    ``'exclusive'``
        The first day of the following month, useful for exclusive bounds.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        raise TypeError("Invalid %s value: %r" % (field, value))

    text = value.strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    for fmt in ('%Y-%m', '%Y/%m'):
        try:
            parsed = datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if month_boundary == 'end':
            return _month_end(parsed)
        if month_boundary == 'exclusive':
            return _month_end(parsed) + _DAY
        return parsed
    raise ValueError("Invalid %s value: %r" % (field, value))


def _month_end(value):
    if value.month == 12:
        following = datetime.date(value.year + 1, 1, 1)
    else:
        following = datetime.date(value.year, value.month + 1, 1)
    return following - _DAY


def _month_start(value):
    return datetime.date(value.year, value.month, 1)


def _iso(value):
    if value is None or value == _MIN_DATE or value == _MAX_DATE:
        return None
    return value.isoformat()


def _days(start, end):
    current = start
    while current <= end:
        yield current
        current += _DAY


def _as_tuple(value, field):
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    try:
        result = tuple(value)
    except TypeError:
        raise TypeError("Invalid %s value: %r" % (field, value))
    if not result:
        return None
    return result


def _format_list(value):
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(value)


class _AnySpec(object):
    """A matcher for declarations that impose no version restriction."""

    def match(self, version):
        return True

    def __str__(self):
        return '*'

    def __repr__(self):
        return 'AnySpec()'


class SupportDeclaration(object):
    """A consumer's dated support declaration.

    ``start`` is inclusive, ``end`` is exclusive.  When ``end`` is omitted the
    declaration remains valid until the next declaration for the same consumer
    starts.  Several overlapping declarations from one consumer are treated as
    a union: the consumer supports an artifact if at least one active
    declaration matches it.
    """

    __slots__ = [
        'consumer', 'spec', 'syntax', 'start', 'end', 'formats',
        'declaration_id', 'label',
    ]

    def __init__(
            self,
            consumer,
            spec='*',
            syntax='simple',
            start=None,
            end=None,
            formats=None,
            declaration_id=None,
            label=None):
        if not consumer:
            raise ValueError("A support declaration needs a consumer name.")
        self.consumer = str(consumer)
        self.spec, self.syntax = self._build_spec(spec, syntax)
        self.start = _parse_date(start, 'start') if start is not None else _MIN_DATE
        self.end = _parse_date(end, 'end', month_boundary='exclusive') if end is not None else None
        if self.end is not None and self.end <= self.start:
            raise ValueError("A declaration must end after it starts.")
        self.formats = _format_list(formats)
        self.declaration_id = str(declaration_id) if declaration_id is not None else None
        self.label = label

    @staticmethod
    def _build_spec(spec, syntax):
        if isinstance(spec, BaseSpec):
            return spec, getattr(spec, 'SYNTAX', syntax or 'simple')
        if spec is None:
            return _AnySpec(), syntax or 'simple'
        if isinstance(spec, dict):
            syntax = str(spec.get('syntax') or syntax or 'simple')
            spec = spec.get('expression', spec.get('spec', '*'))
            if isinstance(spec, BaseSpec):
                return spec, syntax
        if spec == '*':
            return _AnySpec(), syntax or 'simple'
        syntax = str(syntax or 'simple')
        if syntax == 'auto':
            try:
                return SimpleSpec(spec), 'simple'
            except ValueError:
                return BaseSpec.parse(spec, 'npm'), 'npm'
        if syntax not in BaseSpec.SYNTAXES:
            raise ValueError("Unknown spec syntax %r" % syntax)
        return BaseSpec.parse(spec, syntax), syntax

    def matches(self, artifact):
        if self.formats and artifact.format not in self.formats:
            return False
        return bool(self.spec.match(artifact.version))

    def applies_on(self, day, effective_end=None):
        end = self.end if self.end is not None else effective_end
        if day < self.start:
            return False
        if end is not None and day >= end:
            return False
        return True

    def as_dict(self):
        return {
            'id': self.declaration_id,
            'consumer': self.consumer,
            'spec': str(self.spec),
            'syntax': self.syntax,
            'start': _iso(self.start),
            'end': _iso(self.end),
            'formats': list(self.formats) if self.formats else None,
            'label': self.label,
        }

    def __repr__(self):
        return 'SupportDeclaration(%r, %r, start=%r, end=%r)' % (
            self.consumer, str(self.spec), self.start, self.end,
        )


class Artifact(object):
    """One exact build in the release catalog."""

    __slots__ = [
        'version', 'artifact_id', 'format', 'published', 'withdrawn',
        'metadata',
    ]

    def __init__(
            self,
            version,
            artifact_id=None,
            format=None,
            published=None,
            withdrawn=None,
            **metadata):
        if isinstance(version, Version):
            self.version = version
        else:
            self.version = Version(str(version))
        self.artifact_id = str(artifact_id) if artifact_id is not None else None
        self.format = format
        self.published = _parse_date(published, 'published') if published is not None else _MIN_DATE
        if isinstance(withdrawn, bool):
            if withdrawn and metadata.get('withdrawn_at') is None:
                raise ValueError("withdrawn=True requires withdrawn_at.")
            withdrawn = metadata.pop('withdrawn_at', None) if withdrawn else None
        self.withdrawn = _parse_date(withdrawn, 'withdrawn') if withdrawn is not None else None
        self.metadata = metadata

    @property
    def key(self):
        if self.artifact_id is not None:
            return ('id', self.artifact_id)
        return ('version', str(self.version), self.format or '')

    @property
    def key_text(self):
        if self.artifact_id is not None:
            return self.artifact_id
        suffix = ('/' + str(self.format)) if self.format else ''
        return str(self.version) + suffix

    @property
    def display(self):
        if self.artifact_id is not None:
            return '%s (%s)' % (self.artifact_id, self.version)
        if self.format:
            return '%s/%s' % (self.version, self.format)
        return str(self.version)

    def as_dict(self):
        result = {
            'id': self.artifact_id,
            'key': self.key_text,
            'version': str(self.version),
            'format': self.format,
            'published': _iso(self.published),
            'withdrawn': _iso(self.withdrawn),
        }
        result.update(self.metadata)
        return result

    def __repr__(self):
        return 'Artifact(%r, id=%r, format=%r)' % (
            str(self.version), self.artifact_id, self.format,
        )


class Withdrawal(object):
    """A dated release-catalog withdrawal.

    A withdrawal may target exact artifact ids, version ranges, formats, or a
    combination of those.  ``effective`` is inclusive and ``until`` (when
    present) is exclusive.  Withdrawals are permanent by default.
    """

    __slots__ = [
        'effective', 'until', 'spec', 'syntax', 'formats', 'artifact_ids',
        'withdrawal_id', 'reason',
    ]

    def __init__(
            self,
            effective,
            spec=None,
            syntax='simple',
            formats=None,
            artifact_ids=None,
            until=None,
            withdrawal_id=None,
            reason=None):
        self.effective = _parse_date(effective, 'effective')
        self.until = _parse_date(until, 'until', month_boundary='exclusive') if until is not None else None
        if until is not None and self.until <= self.effective:
            raise ValueError("A withdrawal must end after it starts.")
        if spec is None:
            self.spec = None
            self.syntax = None
        else:
            self.spec, self.syntax = SupportDeclaration._build_spec(spec, syntax)
        self.formats = _format_list(formats)
        self.artifact_ids = _as_tuple(artifact_ids, 'artifact_ids')
        self.withdrawal_id = str(withdrawal_id) if withdrawal_id is not None else None
        self.reason = reason

    def applies_on(self, day):
        if day < self.effective:
            return False
        if self.until is not None and day >= self.until:
            return False
        return True

    def matches(self, artifact):
        if self.artifact_ids is not None:
            candidates = (artifact.artifact_id, artifact.key_text)
            if not any(item in self.artifact_ids for item in candidates if item is not None):
                return False
        if self.formats and artifact.format not in self.formats:
            return False
        if self.spec is not None and not self.spec.match(artifact.version):
            return False
        return True

    def as_dict(self):
        return {
            'id': self.withdrawal_id,
            'effective': _iso(self.effective),
            'until': _iso(self.until),
            'spec': str(self.spec) if self.spec is not None else None,
            'syntax': self.syntax,
            'formats': list(self.formats) if self.formats else None,
            'artifact_ids': list(self.artifact_ids) if self.artifact_ids else None,
            'reason': self.reason,
        }

    def __repr__(self):
        return 'Withdrawal(%r, id=%r, spec=%r)' % (
            self.effective, self.withdrawal_id,
            str(self.spec) if self.spec is not None else None,
        )


class Window(object):
    """A maximal interval during which one exact artifact can be reused."""

    __slots__ = ['start', 'end', 'artifact']

    def __init__(self, start, end, artifact):
        self.start = start
        self.end = end
        self.artifact = artifact

    @property
    def days(self):
        return (self.end - self.start).days + 1

    def as_dict(self):
        return {
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'days': self.days,
            'artifact': self.artifact.as_dict(),
        }

    def __repr__(self):
        return 'Window(%r, %r, %r)' % (self.start, self.end, self.artifact.key_text)


class Gap(object):
    """A period with no single artifact usable throughout it."""

    __slots__ = ['date', 'period_start', 'period_end', 'reason', 'blockers',
                 'active_declarations', 'lost_artifacts']

    def __init__(self, date, reason, blockers=None, period_start=None,
                 period_end=None, active_declarations=None, lost_artifacts=None):
        self.date = date
        self.period_start = period_start if period_start is not None else date
        self.period_end = period_end if period_end is not None else date
        self.reason = reason
        self.blockers = list(blockers or ())
        self.active_declarations = list(active_declarations or ())
        self.lost_artifacts = list(lost_artifacts or ())

    @property
    def days(self):
        return (self.period_end - self.period_start).days + 1

    def as_dict(self):
        return {
            'date': self.date.isoformat(),
            'period_start': self.period_start.isoformat(),
            'period_end': self.period_end.isoformat(),
            'days': self.days,
            'reason': self.reason,
            'blockers': self.blockers,
            'active_declarations': self.active_declarations,
            'lost_artifacts': self.lost_artifacts,
        }

    def __repr__(self):
        return 'Gap(%r, %r)' % (self.date, self.reason)


class ArchivePlan(object):
    """The result of :meth:`ArchivePlanner.plan`."""

    __slots__ = ['start', 'end', 'granularity', 'windows', 'gaps']

    def __init__(self, start, end, granularity, windows, gaps):
        self.start = start
        self.end = end
        self.granularity = granularity
        self.windows = list(windows)
        self.gaps = list(gaps)

    @property
    def changes(self):
        return sum(
            1 for previous, current in zip(self.windows, self.windows[1:])
            if previous.artifact.key != current.artifact.key
        )

    def as_dict(self):
        return {
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'granularity': self.granularity,
            'changes': self.changes,
            'windows': [window.as_dict() for window in self.windows],
            'gaps': [gap.as_dict() for gap in self.gaps],
        }

    def to_json(self, **kwargs):
        kwargs.setdefault('ensure_ascii', False)
        kwargs.setdefault('sort_keys', True)
        return json.dumps(self.as_dict(), **kwargs)

    def format_text(self):
        lines = [
            'Archive plan %s .. %s (%s)' % (
                self.start.isoformat(), self.end.isoformat(), self.granularity,
            ),
        ]
        if not self.windows and not self.gaps:
            lines.append('  (empty calendar)')
            return '\n'.join(lines)

        for window in self.windows:
            lines.append(
                '  %s .. %s  %s  (%d day%s)' % (
                    window.start.isoformat(), window.end.isoformat(),
                    window.artifact.display, window.days,
                    '' if window.days == 1 else 's',
                )
            )
        if self.gaps:
            lines.append('Gaps:')
            for gap in self.gaps:
                details = self._format_blockers(gap.blockers)
                if gap.period_start == gap.period_end:
                    span = gap.period_start.isoformat()
                else:
                    span = '%s .. %s (blocked on %s)' % (
                        gap.period_start.isoformat(), gap.period_end.isoformat(),
                        gap.date.isoformat(),
                    )
                lines.append('  %s  %s  %s' % (span, gap.reason, details))
        lines.append('Version changes: %d' % self.changes)
        return '\n'.join(lines)

    @staticmethod
    def _format_blockers(blockers):
        parts = []
        for blocker in blockers:
            kind = blocker.get('kind')
            if kind == 'declaration':
                text = '%s:%s' % (
                    blocker.get('consumer'),
                    blocker.get('id') or blocker.get('spec'),
                )
                status = blocker.get('status')
                if status == 'expired':
                    text += '(expired %s)' % blocker.get('expired_on')
                elif status == 'pending':
                    text += '(pending %s)' % blocker.get('start')
            elif kind == 'withdrawal':
                text = 'withdrawal:%s' % (
                    blocker.get('id') or blocker.get('effective') or 'catalog'
                )
                artifacts = blocker.get('artifacts') or ()
                if artifacts:
                    text += '[' + ', '.join(artifacts) + ']'
            elif kind == 'not-published':
                text = 'not-published:%s' % (
                    blocker.get('artifact') or
                    ', '.join(blocker.get('artifacts') or ())
                )
            elif kind == 'missing-declaration':
                text = 'missing-declaration:%s' % blocker.get('consumer')
            elif kind == 'empty-catalog':
                text = 'empty-catalog'
            else:
                text = str(kind)
            if text not in parts:
                parts.append(text)
        return '; '.join(parts)

    def __repr__(self):
        return 'ArchivePlan(%r, %r, windows=%d, gaps=%d)' % (
            self.start, self.end, len(self.windows), len(self.gaps),
        )


class _Period(object):
    __slots__ = ['start', 'end']

    def __init__(self, start, end):
        self.start = start
        self.end = end

    def __repr__(self):
        return '_Period(%r, %r)' % (self.start, self.end)


class ArchivePlanner(object):
    """Plan exact-artifact reuse across a calendar window."""

    def __init__(
            self,
            start,
            end,
            artifacts,
            declarations=None,
            withdrawals=None,
            granularity='day'):
        self.start = _parse_date(start, 'start')
        self.end = _parse_date(end, 'end', month_boundary='end')
        if self.end is None:
            self.end = self.start
        if self.end < self.start:
            raise ValueError("The calendar end precedes its start.")
        self.granularity = self._normalise_granularity(granularity)

        self.artifacts = [self._coerce_artifact(item) for item in (artifacts or ())]
        self.artifacts.sort(key=lambda item: (str(item.version), item.format or '', item.key_text))
        self._by_key = {}
        for artifact in self.artifacts:
            if artifact.key in self._by_key:
                raise ValueError("Duplicate artifact key: %s" % artifact.key_text)
            self._by_key[artifact.key] = artifact

        self.withdrawals = [
            self._coerce_withdrawal(item) for item in (withdrawals or ())
        ]
        self.withdrawals.sort(
            key=lambda item: (item.effective, item.withdrawal_id or '', str(item.spec or ''))
        )
        self.declarations = [
            self._coerce_declaration(item) for item in (declarations or ())
        ]
        self._prepare_declarations()
        self._state_cache = {}

    @staticmethod
    def _normalise_granularity(value):
        value = str(value or 'day').lower()
        aliases = {
            'daily': 'day',
            'day': 'day',
            'monthly': 'month',
            'month': 'month',
        }
        if value not in aliases:
            raise ValueError("granularity must be 'day' or 'month'")
        return aliases[value]

    @staticmethod
    def _coerce_artifact(value):
        if isinstance(value, Artifact):
            return value
        if not isinstance(value, dict):
            raise TypeError("An artifact must be an Artifact or a mapping: %r" % (value,))
        data = dict(value)
        version = data.pop('version', None)
        if version is None:
            raise ValueError("An artifact needs a version: %r" % (value,))
        artifact_id = data.pop('artifact_id', data.pop('id', None))
        artifact_format = data.pop('format', data.pop('artifact_format', data.pop('kind', None)))
        published = data.pop('published', data.pop('publish_date', data.pop('introduced', None)))
        withdrawn = data.pop('withdrawn', data.pop('withdrawn_on', data.pop('yanked', None)))
        withdrawn_at = data.pop('withdrawn_at', None)
        if withdrawn is True:
            withdrawn = withdrawn_at
        elif withdrawn is False:
            withdrawn = None
        return Artifact(
            version=version,
            artifact_id=artifact_id,
            format=artifact_format,
            published=published,
            withdrawn=withdrawn,
            **data
        )

    @staticmethod
    def _coerce_withdrawal(value):
        if isinstance(value, Withdrawal):
            return value
        if not isinstance(value, dict):
            raise TypeError("A withdrawal must be a Withdrawal or a mapping: %r" % (value,))
        data = dict(value)
        effective = data.pop(
            'effective', data.pop('date', data.pop('from', data.pop('start', None))),
        )
        if effective is None:
            raise ValueError("A withdrawal needs an effective date: %r" % (value,))
        withdrawal_id = data.pop('withdrawal_id', data.pop('id', None))
        artifact_ids = data.pop(
            'artifact_ids', data.pop('artifacts', data.pop('builds', None)),
        )
        return Withdrawal(
            effective=effective,
            spec=data.pop('spec', data.pop('expression', None)),
            syntax=data.pop('syntax', 'simple'),
            formats=data.pop('formats', data.pop('format', None)),
            artifact_ids=artifact_ids,
            until=data.pop('until', data.pop('end', None)),
            withdrawal_id=withdrawal_id,
            reason=data.pop('reason', None),
        )

    @staticmethod
    def _coerce_declaration(value):
        if isinstance(value, SupportDeclaration):
            return value
        if not isinstance(value, dict):
            raise TypeError("A declaration must be a SupportDeclaration or a mapping: %r" % (value,))
        data = dict(value)
        consumer = data.pop('consumer', data.pop('client', data.pop('endpoint', None)))
        if consumer is None:
            raise ValueError("A declaration needs a consumer: %r" % (value,))
        declaration_id = data.pop('declaration_id', data.pop('id', None))
        start = data.pop(
            'start', data.pop('effective', data.pop('from', None)),
        )
        end = data.pop('end', data.pop('until', data.pop('expires', None)))
        formats = data.pop('formats', data.pop('format', None))
        return SupportDeclaration(
            consumer=consumer,
            spec=data.pop('spec', data.pop('expression', '*')),
            syntax=data.pop('syntax', 'simple'),
            start=start,
            end=end,
            formats=formats,
            declaration_id=declaration_id,
            label=data.pop('label', None),
        )

    def _prepare_declarations(self):
        grouped = {}
        for declaration in self.declarations:
            grouped.setdefault(declaration.consumer, []).append(declaration)
        for items in grouped.values():
            items.sort(key=lambda item: (item.start, item.declaration_id or '', str(item.spec)))
        self._declarations_by_consumer = grouped
        self._consumer_names = sorted(grouped)

    @classmethod
    def from_config(cls, config):
        """Build a planner from a JSON-compatible mapping."""
        if not isinstance(config, dict):
            raise TypeError("The planner configuration must be a mapping.")

        start = config.get('start', config.get('from'))
        end = config.get('end', config.get('to'))
        if start is None and config.get('month'):
            start = str(config['month']) + '-01'
            if end is None:
                end = str(config['month'])
        if start is None or end is None:
            raise ValueError("A planner configuration needs start and end dates.")

        declarations = []
        consumers = config.get('consumers', config.get('clients'))
        if consumers is not None:
            if not isinstance(consumers, dict):
                raise TypeError("'consumers' must map consumer names to declarations.")
            for consumer, items in consumers.items():
                if isinstance(items, (dict, str, SupportDeclaration)):
                    items = [items]
                for item in items or ():
                    if isinstance(item, dict):
                        item = dict(item)
                        item.setdefault('consumer', consumer)
                    elif not isinstance(item, SupportDeclaration):
                        item = {'consumer': consumer, 'spec': item}
                    declarations.append(item)
        declared = config.get('declarations', config.get('support'))
        if declared:
            declarations.extend(declared)

        artifacts = config.get('artifacts', config.get('releases', config.get('catalog', ())))
        withdrawals = config.get(
            'withdrawals',
            config.get('withdrawn', config.get('withdrawal_catalog', ())),
        )
        granularity = config.get('granularity', config.get('period', 'day'))
        return cls(
            start=start,
            end=end,
            artifacts=artifacts,
            declarations=declarations,
            withdrawals=withdrawals,
            granularity=granularity,
        )

    def _effective_end(self, declaration, index):
        if declaration.end is not None:
            return declaration.end
        items = self._declarations_by_consumer[declaration.consumer]
        for later in items[index + 1:]:
            if later.start > declaration.start:
                return later.start
        return _MAX_DATE

    def _active_declarations(self, day):
        result = {}
        for consumer in self._consumer_names:
            active = []
            items = self._declarations_by_consumer[consumer]
            for index, declaration in enumerate(items):
                if declaration.applies_on(day, self._effective_end(declaration, index)):
                    active.append(declaration)
            if active:
                result[consumer] = active
        return result

    def _periods(self):
        periods = []
        if self.granularity == 'day':
            for day in _days(self.start, self.end):
                periods.append(_Period(day, day))
            return periods

        cursor = self.start
        while cursor <= self.end:
            month_start = _month_start(cursor)
            month_end = _month_end(month_start)
            period_start = max(self.start, month_start)
            period_end = min(self.end, month_end)
            periods.append(_Period(period_start, period_end))
            cursor = period_end + _DAY
        return periods

    def _artifact_available(self, artifact, day):
        if day < artifact.published:
            return False, ('not-published', artifact)
        if artifact.withdrawn is not None and day >= artifact.withdrawn:
            return False, ('artifact-withdrawal', artifact)
        for withdrawal in self.withdrawals:
            if withdrawal.applies_on(day) and withdrawal.matches(artifact):
                return False, ('withdrawal', withdrawal)
        return True, None

    def _state(self, day):
        cached = self._state_cache.get(day)
        if cached is not None:
            return cached

        active = self._active_declarations(day)
        missing = [name for name in self._consumer_names if name not in active]

        support = {}
        for consumer in self._consumer_names:
            declarations = active.get(consumer, ())
            support[consumer] = set(
                artifact.key
                for artifact in self.artifacts
                if any(declaration.matches(artifact) for declaration in declarations)
            )

        if missing:
            all_support = set()
        elif not self._consumer_names:
            all_support = set(artifact.key for artifact in self.artifacts)
        else:
            sets = [support[name] for name in self._consumer_names]
            all_support = set(sets[0])
            for values in sets[1:]:
                all_support &= values
                if not all_support:
                    break

        candidates = set()
        availability = {}
        for key in all_support:
            artifact = self._by_key[key]
            is_available, blocker = self._artifact_available(artifact, day)
            availability[key] = blocker
            if is_available:
                candidates.add(key)

        state = {
            'active': active,
            'missing': missing,
            'support': support,
            'all_support': all_support,
            'candidates': candidates,
            'availability': availability,
        }
        self._state_cache[day] = state
        return state

    def _missing_declaration_blockers(self, consumer, day):
        items = self._declarations_by_consumer[consumer]
        expired = None
        pending = None
        for index, declaration in enumerate(items):
            effective_end = self._effective_end(declaration, index)
            if effective_end <= day:
                expired = (declaration, effective_end)
            elif declaration.start > day and pending is None:
                pending = declaration

        blockers = []
        if expired is not None:
            declaration, effective_end = expired
            ref = self._declaration_ref(declaration)
            ref['status'] = 'expired'
            ref['expired_on'] = _iso(effective_end)
            blockers.append(ref)
        if pending is not None:
            ref = self._declaration_ref(pending)
            ref['status'] = 'pending'
            blockers.append(ref)
        if not blockers:
            blockers.append({
                'kind': 'missing-declaration',
                'consumer': consumer,
            })
        return blockers

    def _declaration_ref(self, declaration):
        return {
            'kind': 'declaration',
            'id': declaration.declaration_id,
            'consumer': declaration.consumer,
            'spec': str(declaration.spec),
            'syntax': declaration.syntax,
            'start': _iso(declaration.start),
            'end': _iso(declaration.end),
            'formats': list(declaration.formats) if declaration.formats else None,
        }

    def _withdrawal_ref(self, withdrawal, artifacts=None, source=None):
        result = {
            'kind': 'withdrawal',
            'id': withdrawal.withdrawal_id,
            'effective': _iso(withdrawal.effective),
            'until': _iso(withdrawal.until),
            'spec': str(withdrawal.spec) if withdrawal.spec is not None else None,
            'syntax': withdrawal.syntax,
            'formats': list(withdrawal.formats) if withdrawal.formats else None,
            'artifact_ids': list(withdrawal.artifact_ids) if withdrawal.artifact_ids else None,
            'reason': withdrawal.reason,
        }
        if source:
            result['source'] = source
        if artifacts:
            result['artifacts'] = artifacts
        return result

    @staticmethod
    def _intersection(sets):
        if not sets:
            return set()
        result = set(sets[0])
        for values in sets[1:]:
            result &= values
            if not result:
                break
        return result

    def _declaration_union(self, declarations):
        return set(
            artifact.key
            for artifact in self.artifacts
            if any(declaration.matches(artifact) for declaration in declarations)
        )

    def _minimal_conflict(self, state):
        consumers = [name for name in self._consumer_names if name in state['active']]
        if not consumers:
            return [], []

        core = list(consumers)
        index = 0
        while index < len(core):
            trial = core[:index] + core[index + 1:]
            if trial and not self._intersection([state['support'][name] for name in trial]):
                core = trial
            else:
                index += 1

        selected = {name: list(state['active'][name]) for name in core}
        changed = True
        while changed:
            changed = False
            for consumer in core:
                if len(selected[consumer]) <= 1:
                    continue
                for declaration in list(selected[consumer]):
                    trial = {}
                    for name in core:
                        trial[name] = [
                            item for item in selected[name]
                            if not (name == consumer and item is declaration)
                        ]
                    if any(not values for values in trial.values()):
                        continue
                    sets = [self._declaration_union(values) for values in trial.values()]
                    if not self._intersection(sets):
                        selected = trial
                        changed = True
                        break
                if changed:
                    break

        declarations = []
        for consumer in core:
            declarations.extend(selected[consumer])
        unsupported = any(
            not self._declaration_union(selected[consumer]) for consumer in core
        )
        return declarations, ('consumer-unsupported' if unsupported else 'declaration-conflict')

    def _artifact_support_blockers(self, day, artifact):
        state = self._state(day)
        blockers = []
        for consumer in self._consumer_names:
            active = state['active'].get(consumer, ())
            if not active:
                blockers.append({
                    'kind': 'missing-declaration',
                    'consumer': consumer,
                    'artifact': artifact.key_text,
                })
                continue
            if not any(declaration.matches(artifact) for declaration in active):
                for declaration in active:
                    ref = self._declaration_ref(declaration)
                    ref['artifact'] = artifact.key_text
                    blockers.append(ref)
        return blockers

    def _artifact_available_blockers(self, day, artifact):
        is_available, blocker = self._artifact_available(artifact, day)
        if is_available:
            return []
        kind, value = blocker
        if kind == 'not-published':
            return [{
                'kind': 'not-published',
                'artifact': artifact.key_text,
                'published': _iso(artifact.published),
            }]
        if kind == 'artifact-withdrawal':
            return [{
                'kind': 'withdrawal',
                'id': 'withdrawn-artifact',
                'source': 'artifact',
                'effective': _iso(artifact.withdrawn),
                'artifacts': [artifact.key_text],
            }]
        return [self._withdrawal_ref(value, [artifact.key_text])]

    def _unavailable_support_blockers(self, day, keys):
        grouped = {}
        for key in sorted(keys):
            artifact = self._by_key[key]
            blockers = self._artifact_available_blockers(day, artifact)
            for blocker in blockers:
                group_key = (
                    blocker.get('kind'), blocker.get('id'), blocker.get('consumer'),
                    blocker.get('artifact'), blocker.get('effective'),
                )
                if group_key not in grouped:
                    item = dict(blocker)
                    if 'artifacts' not in item:
                        item['artifacts'] = []
                    grouped[group_key] = item
                if artifact.key_text not in grouped[group_key]['artifacts']:
                    grouped[group_key]['artifacts'].append(artifact.key_text)

        entries = []
        for blocker in grouped.values():
            covered = set()
            for key in keys:
                artifact = self._by_key[key]
                if artifact.key_text in blocker.get('artifacts', ()):
                    covered.add(key)
            entries.append([blocker, covered])

        changed = True
        while changed:
            changed = False
            for index in range(len(entries)):
                trial = entries[:index] + entries[index + 1:]
                if not trial:
                    continue
                covered = set()
                for _blocker, values in trial:
                    covered |= values
                if covered == set(keys):
                    entries = trial
                    changed = True
                    break

        return [blocker for blocker, _covered in entries]

    def _candidate_declaration_blockers(self, state, keys):
        """Declarations that made the unavailable candidates eligible."""
        if not keys:
            return []

        pairs = set(
            (consumer, key)
            for consumer in self._consumer_names
            for key in keys
            if any(
                declaration.matches(self._by_key[key])
                for declaration in state['active'].get(consumer, ())
            )
        )
        entries = []
        for consumer in self._consumer_names:
            for declaration in state['active'].get(consumer, ()):
                covered = set(
                    (consumer, key) for key in keys
                    if declaration.matches(self._by_key[key])
                )
                if covered:
                    entries.append((declaration, covered))

        changed = True
        while changed:
            changed = False
            for index in range(len(entries)):
                covered = set()
                for _declaration, values in entries[:index] + entries[index + 1:]:
                    covered |= values
                if covered == pairs:
                    entries = entries[:index] + entries[index + 1:]
                    changed = True
                    break

        return [
            self._declaration_ref(declaration)
            for declaration, _covered in entries
        ]

    def _explain_day(self, day, period=None):
        state = self._state(day)
        active_refs = []
        for consumer in self._consumer_names:
            for declaration in state['active'].get(consumer, ()):
                active_refs.append(self._declaration_ref(declaration))

        if not self.artifacts:
            return Gap(
                day, 'catalog-empty',
                blockers=[{'kind': 'empty-catalog'}],
                period_start=period.start if period else day,
                period_end=period.end if period else day,
                active_declarations=active_refs,
            )

        if state['missing']:
            blockers = []
            for consumer in state['missing']:
                blockers.extend(self._missing_declaration_blockers(consumer, day))
            return Gap(
                day, 'missing-declaration',
                blockers=blockers,
                period_start=period.start if period else day,
                period_end=period.end if period else day,
                active_declarations=active_refs,
            )

        if not state['all_support']:
            declarations, reason = self._minimal_conflict(state)
            blockers = [self._declaration_ref(item) for item in declarations]
            return Gap(
                day, reason,
                blockers=blockers,
                period_start=period.start if period else day,
                period_end=period.end if period else day,
                active_declarations=active_refs,
            )

        blockers = self._unavailable_support_blockers(day, state['all_support'])
        blockers.extend(
            self._candidate_declaration_blockers(state, state['all_support'])
        )
        kinds = set(item.get('kind') for item in blockers)
        if 'withdrawal' in kinds:
            reason = 'catalog-withdrawal'
        elif 'not-published' in kinds:
            reason = 'catalog-not-published'
        else:
            reason = 'catalog-unavailable'
        return Gap(
            day, reason,
            blockers=blockers,
            period_start=period.start if period else day,
            period_end=period.end if period else day,
            active_declarations=active_refs,
        )

    def _explain_period(self, period):
        days = list(_days(period.start, period.end))
        cumulative = None
        for day in days:
            candidates = self._state(day)['candidates']
            if cumulative is None:
                cumulative = set(candidates)
                if not cumulative:
                    return self._explain_day(day, period)
                continue

            previous = set(cumulative)
            cumulative &= candidates
            if cumulative:
                continue

            gap = self._explain_day(day, period)
            if day == period.start:
                return gap

            previous &= self._state(day)['all_support']
            gap.reason = 'no-spanning-artifact'
            gap.lost_artifacts = [
                self._by_key[key].key_text for key in sorted(previous)
            ]
            return gap

        return None

    @staticmethod
    def _gap_signature(gap):
        blockers = []
        for blocker in gap.blockers:
            if blocker.get('kind') == 'not-published':
                blockers.append((
                    'not-published',
                    blocker.get('published'),
                    tuple(sorted(blocker.get('artifacts') or ())),
                ))
            else:
                blockers.append((
                    blocker.get('kind'), blocker.get('id'), blocker.get('consumer'),
                    blocker.get('spec'), blocker.get('status'),
                    tuple(sorted(blocker.get('artifacts') or ())),
                ))
        return (gap.reason, tuple(blockers))

    @staticmethod
    def _dedupe_blockers(blockers):
        result = []
        seen = set()
        for blocker in blockers:
            key = (
                blocker.get('kind'), blocker.get('id'), blocker.get('consumer'),
                blocker.get('artifact'), blocker.get('effective'),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(blocker)
        return result

    def _period_candidates(self, periods):
        result = []
        for period in periods:
            values = None
            for day in _days(period.start, period.end):
                candidates = self._state(day)['candidates']
                if values is None:
                    values = set(candidates)
                else:
                    values &= candidates
                if not values:
                    break
            result.append(values or set())
        return result

    def _artifact_rank(self, artifact):
        return (
            artifact.version.precedence_key,
            artifact.key_text,
        )

    def plan(self):
        if self.start is None or self.end is None:
            raise ValueError("A calendar start and end are required.")

        periods = self._periods()
        candidates = self._period_candidates(periods)

        windows = []
        gaps = []

        index = 0
        while index < len(periods):
            if not candidates[index]:
                gap = self._explain_period(periods[index])
                signature = self._gap_signature(gap)
                last = index
                while last + 1 < len(periods) and not candidates[last + 1]:
                    next_gap = self._explain_period(periods[last + 1])
                    if self._gap_signature(next_gap) != signature:
                        break
                    last += 1
                gap.period_start = periods[index].start
                gap.period_end = periods[last].end
                gaps.append(gap)
                index = last + 1
                continue

            while index < len(periods) and candidates[index]:
                start_index = index
                options = candidates[index]
                best_key = None
                best_reach = -1
                best_rank = None
                for key in options:
                    reach = index
                    while reach + 1 < len(periods) and key in candidates[reach + 1]:
                        reach += 1
                    rank = self._artifact_rank(self._by_key[key])
                    if (reach > best_reach or
                            (reach == best_reach and rank > best_rank)):
                        best_key = key
                        best_reach = reach
                        best_rank = rank

                windows.append(Window(
                    periods[start_index].start,
                    periods[best_reach].end,
                    self._by_key[best_key],
                ))
                index = best_reach + 1

        return ArchivePlan(
            self.start, self.end, self.granularity, windows, gaps,
        )


def plan_archive(config):
    """Plan a calendar from a JSON-compatible configuration mapping."""
    return ArchivePlanner.from_config(config).plan()


def _load_config(path):
    if path == '-':
        return json.load(sys.stdin)
    with open(path, 'r') as handle:
        return json.load(handle)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description='Plan exact-artifact reuse across a calendar.',
    )
    parser.add_argument(
        '--input', '-i', default='-',
        help='JSON configuration file (default: stdin)',
    )
    parser.add_argument(
        '--format', '-f', default='text', choices=('text', 'json'),
        help='Output format (default: text)',
    )
    parser.add_argument(
        '--granularity', '-g', default=None,
        choices=('day', 'month'),
        help='Override the configuration granularity',
    )
    args = parser.parse_args(argv)

    config = _load_config(args.input)
    if args.granularity:
        config = dict(config)
        config['granularity'] = args.granularity
    plan = plan_archive(config)
    if args.format == 'json':
        print(plan.to_json(indent=2))
    else:
        print(plan.format_text())
    return 0


if __name__ == '__main__':
    sys.exit(main())
