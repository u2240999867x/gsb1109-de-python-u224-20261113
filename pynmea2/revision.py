"""
Firmware-revision aware parsing for proprietary sentences.

A manufacturer may ship a new firmware that reorders the columns of an
otherwise unchanged proprietary sentence: the sentence keeps the same name,
the same number of fields and the same XOR checksum (XOR is invariant under
permutation of the bytes), so the raw message alone can no longer tell which
field definition applies.

The classes in this module resolve that ambiguity by keeping every published
field definition (a "revision") together with the time interval for which
there is evidence that it was active on a given device.  When the exact
switch time is not known the overlapping interval keeps *all* still possible
readings instead of guessing; once a late firmware announcement supplies the
evidence, the original raw message can be re-parsed with the correct
definition.

Timelines are scoped per (manufacturer, sentence, device id), so an
announcement for one device never leaks onto another device.
"""

import threading
from datetime import date, datetime, time, timezone

from . import nmea


# ---------------------------------------------------------------------------
# parse() context: carries registry / time / device / forced revision from
# NMEASentence.parse() to the VersionedProprietarySentence constructor without
# changing the historical (manufacturer, data) constructor call convention.
# ---------------------------------------------------------------------------

_local = threading.local()


class _RevisionContext(object):
    def __init__(self, registry, at, device, force_revision, line, checksums):
        self.registry = registry
        self.at = at
        self.device = device
        self.force_revision = force_revision
        self.line = line
        self.checksums = checksums


def _context_stack():
    stack = getattr(_local, 'stack', None)
    if stack is None:
        stack = []
        _local.stack = stack
    return stack


def push_revision_context(registry=None, at=None, device=None,
                          force_revision=None, line=None,
                          checksums='check'):
    """Install revision resolution state for the current parse call."""
    token = _RevisionContext(
        registry=registry, at=at, device=device,
        force_revision=force_revision, line=line, checksums=checksums)
    _context_stack().append(token)
    return token


def pop_revision_context(token):
    """Remove the revision resolution state installed by push_*."""
    stack = _context_stack()
    if stack and stack[-1] is token:
        stack.pop()


def current_context():
    stack = _context_stack()
    return stack[-1] if stack else None


# ---------------------------------------------------------------------------
# time handling: datetime / date / numeric epoch are all accepted
# ---------------------------------------------------------------------------

def _time_key(value):
    """Normalise a time bound to a naive datetime for ordered comparison."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value, timezone.utc)
    else:
        raise TypeError(
            'unsupported time value %r (expected datetime, date or number)'
            % (value,))
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _interval_contains(record, moment):
    start = _time_key(record.get('effective_from'))
    if start is not None and moment < start:
        return False
    end = _time_key(record.get('effective_until'))
    if end is not None and moment >= end:
        return False
    return True


# ---------------------------------------------------------------------------
# field definition diffing
# ---------------------------------------------------------------------------

def diff_fields(old_fields, new_fields):
    """
    Compare two published field definitions.

    Returns a dict describing every published field that the announcement
    changed: fields that were added, removed, moved to another column (the
    column-swap case), relabelled, or whose converter changed.
    """
    old_index = dict((field[1], index)
                     for index, field in enumerate(old_fields))
    new_index = dict((field[1], index)
                     for index, field in enumerate(new_fields))
    old_by_name = dict((field[1], field) for field in old_fields)

    added = [field[1] for field in new_fields if field[1] not in old_index]
    removed = [field[1] for field in old_fields
               if field[1] not in new_index]

    moved = []
    relabeled = []
    type_changed = []
    for index, field in enumerate(new_fields):
        name = field[1]
        if name not in old_index:
            continue
        previous_index = old_index[name]
        if previous_index != index:
            moved.append((name, previous_index, index))
        old_field = old_by_name[name]
        if old_field[0] != field[0]:
            relabeled.append((name, old_field[0], field[0]))
        old_converter = old_field[2] if len(old_field) >= 3 else None
        new_converter = field[2] if len(field) >= 3 else None
        if old_converter is not new_converter:
            type_changed.append((name, old_converter, new_converter))

    changed = []
    for name in added + removed:
        if name not in changed:
            changed.append(name)
    for name, _, _ in moved:
        if name not in changed:
            changed.append(name)
    for name, _, _ in relabeled:
        if name not in changed:
            changed.append(name)
    for name, _, _ in type_changed:
        if name not in changed:
            changed.append(name)

    return {
        'added': added,
        'removed': removed,
        'moved': moved,
        'relabeled': relabeled,
        'type_changed': type_changed,
        'changed_fields': changed,
    }


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

class FirmwareRevisionRegistry(object):
    """
    Holds the evidenced active intervals of proprietary sentence revisions.

    Records are scoped by ``(manufacturer, sentence, device)``.  A record
    stored with ``device=None`` is manufacturer-wide; a record stored with an
    explicit device id only applies to that device, so replacing the device
    can never inherit the previous device's version timeline.
    """

    def __init__(self):
        # (manufacturer, sentence, device) -> list of revision records
        self._records = {}
        # (manufacturer, sentence, device) -> announcement history
        self._history = {}
        self._counter = 0
        self._lock = threading.RLock()

    def _next_seq(self):
        self._counter += 1
        return self._counter

    def announce(self, manufacturer, sentence, revision, fields=None,
                 effective_from=None, effective_until=None, device=None,
                 replaces=None, previous_fields=None):
        """
        Record a firmware announcement.

        `revision` is the revision name.  `fields` is the published field
        definition tuple; it may be omitted when the revision was already
        known and only its interval is being refined.  `effective_from` /
        `effective_until` bound the interval for which evidence exists; either
        bound may be None while it is still undetermined.

        When `replaces` is given together with a pinned `effective_from`, the
        replaced revision's open interval is closed at that same instant.
        `previous_fields` may supply the published definition of the replaced
        revision purely for diffing when it was never separately announced.

        Returns the diff of the published fields this announcement actually
        changed.
        """
        with self._lock:
            key = (manufacturer, sentence, device)
            records = self._records.setdefault(key, [])

            old_fields = None
            if replaces is not None:
                previous = self._find_record(manufacturer, sentence,
                                             device, replaces)
                if previous is not None:
                    old_fields = previous['fields']
            same_name = next((r for r in records
                              if r['revision'] == revision), None)
            if old_fields is None and same_name is not None:
                old_fields = same_name['fields']
            if old_fields is None and previous_fields is not None:
                old_fields = tuple(tuple(field)
                                   for field in previous_fields)

            if fields is None:
                if same_name is not None:
                    fields = same_name['fields']
                else:
                    raise ValueError(
                        'fields are required for the new revision %r'
                        % (revision,))
            fields = tuple(tuple(field) for field in fields)

            if replaces is not None and effective_from is not None:
                for record in records:
                    if (record['revision'] == replaces
                            and record['effective_until'] is None):
                        record['effective_until'] = effective_from

            entry = {
                'revision': revision,
                'fields': fields,
                'effective_from': effective_from,
                'effective_until': effective_until,
                'announced_at': datetime.now(timezone.utc),
                'seq': self._next_seq(),
            }
            records[:] = [r for r in records
                          if r['revision'] != revision] + [entry]

            if old_fields is None:
                # first piece of evidence for a definition is not itself a
                # change of any published field
                diff = diff_fields((), ())
            else:
                diff = diff_fields(old_fields, fields)
            history = self._history.setdefault(key, [])
            history.append({
                'revision': revision,
                'replaces': replaces,
                'effective_from': effective_from,
                'effective_until': effective_until,
                'announced_at': entry['announced_at'],
                'diff': diff,
            })
            return diff

    def _find_record(self, manufacturer, sentence, device, revision):
        for scoped_device in (device, None):
            for record in self._records.get(
                    (manufacturer, sentence, scoped_device), ()):
                if record['revision'] == revision:
                    return record
        return None

    def announcements(self, manufacturer, sentence, device=None):
        """Return the announcement history for one sentence/device scope."""
        with self._lock:
            history = self._history.get((manufacturer, sentence, device), [])
            return [dict(item) for item in history]

    def reset(self):
        """Remove every announcement (used by tests and device swaps)."""
        with self._lock:
            self._records.clear()
            self._history.clear()
            self._counter = 0

    def _effective_records(self, manufacturer, sentence, device,
                           base_revisions):
        effective = {}
        for record in self._records.get((manufacturer, sentence, None), ()):
            effective[record['revision']] = dict(record)
        if device is not None:
            for record in self._records.get(
                    (manufacturer, sentence, device), ()):
                effective[record['revision']] = dict(record)
        for name, definition in base_revisions.items():
            if name not in effective:
                effective[name] = {
                    'revision': name,
                    'fields': tuple(tuple(f) for f in definition),
                    'effective_from': None,
                    'effective_until': None,
                    'seq': self._next_seq(),
                }
        return sorted(
            effective.values(),
            key=lambda r: (_time_key(r['effective_from']) or datetime.min,
                           r['seq']))

    def select(self, manufacturer, sentence, base_revisions,
               device=None, at=None, force_revision=None):
        """
        Choose the reading(s) applicable at time `at` on `device`.

        Returns `(chosen_revision, candidate_records)`.  `chosen_revision`
        is the unique applicable revision name, or None while the switch
        instant is undetermined and several readings must be retained.
        `candidate_records` is the ordered list of all still possible
        revisions, each carrying its resolved field definition.
        """
        with self._lock:
            records = self._effective_records(
                manufacturer, sentence, device, base_revisions)

            if force_revision is not None:
                for record in records:
                    if record['revision'] == force_revision:
                        return force_revision, [record]
                raise ValueError(
                    'unknown revision %r for %s%s'
                    % (force_revision, manufacturer, sentence))

            moment = _time_key(at)
            if moment is None:
                candidates = list(records)
            else:
                candidates = [r for r in records
                              if _interval_contains(r, moment)]
                if not candidates:
                    # evidenced gap: nothing claims this instant, so no
                    # single reading may be picked authoritatively
                    candidates = list(records)

            chosen = (candidates[0]['revision']
                      if len(candidates) == 1 else None)
            return chosen, candidates


# Default process-wide registry.
firmware_registry = FirmwareRevisionRegistry()


# ---------------------------------------------------------------------------
# versioned proprietary sentence base class
# ---------------------------------------------------------------------------

class VersionedProprietarySentence(nmea.ProprietarySentence):
    """
    Base class for proprietary sentences whose column layout changes between
    firmware revisions.

    Concrete subclasses declare a class-level ``revisions`` mapping of
    revision name to the published field definition tuple, and may override
    ``sentence_data_index`` (default 0) to point at the data element that
    identifies the sentence subtype.
    """

    fields = ()
    revisions = {}
    sentence_data_index = 0

    def _sentence_id(self, data):
        return data[self.sentence_data_index]

    def __init__(self, manufacturer, data):
        super(VersionedProprietarySentence, self).__init__(manufacturer, data)
        self.sentence_type = manufacturer + self._sentence_id(data)

        context = current_context()
        self.registry = (context and context.registry) or firmware_registry
        self.device_id = context.device if context else None
        self.observed_at = context.at if context else None
        self.checksum_mode = (context.checksums if context
                              else 'check')
        self.raw_sentence = (context and context.line) or self.render()

        revisions = type(self).revisions
        if not revisions:
            self.fields = type(self).fields
            self.name_to_idx = self._build_name_to_idx(self.fields)
            self.revision = None
            self.revision_candidates = []
            self.interpretation_records = []
            return

        chosen, candidates = self.registry.select(
            manufacturer,
            self._sentence_id(data),
            revisions,
            device=self.device_id,
            at=self.observed_at,
            force_revision=context.force_revision if context else None)

        self.revision = chosen
        self.revision_candidates = [r['revision'] for r in candidates]
        self.interpretation_records = candidates
        if chosen is None:
            record = candidates[0]
        else:
            record = next(r for r in candidates
                          if r['revision'] == chosen)
        self.fields = record['fields']
        self.name_to_idx = self._build_name_to_idx(self.fields)

    @staticmethod
    def _build_name_to_idx(fields):
        return dict((field[1], index)
                    for index, field in enumerate(fields))

    @property
    def ambiguous(self):
        """True while more than one reading must be retained."""
        return self.revision is None and bool(self.revision_candidates)

    def interpretations(self):
        """
        Return one parsed object per still-possible reading.

        Re-queries the registry on every call, so while the switch instant is
        undetermined this yields every retained definition and, once the
        timeline is pinned, it yields just the single applicable reading.
        """
        if not type(self).revisions:
            return [self]
        context = current_context()
        registry = ((context and context.registry) or self.registry)
        at = context.at if context is not None else self.observed_at
        device = context.device if context is not None else self.device_id
        forced = context.force_revision if context else None
        _, candidates = registry.select(
            self.manufacturer,
            self._sentence_id(self.data),
            type(self).revisions,
            device=device,
            at=at,
            force_revision=forced)
        return [self.reinterpret(revision=record['revision'])
                for record in candidates]

    def reinterpret(self, revision=None, at=None, device=None,
                    registry=None):
        """Re-parse the original raw message, optionally forcing a reading."""
        context = current_context()
        if registry is None:
            registry = (context and context.registry) or self.registry
        if at is None:
            at = (context.at if context is not None
                  else self.observed_at)
        if device is None:
            device = (context.device if context is not None
                      else self.device_id)
        checksums = self.checksum_mode

        token = push_revision_context(
            registry=registry, at=at, device=device,
            force_revision=revision, line=self.raw_sentence,
            checksums=checksums)
        try:
            return nmea.NMEASentence._parse(self.raw_sentence, checksums)
        finally:
            pop_revision_context(token)

    def apply_announcement(self, revision, fields=None, effective_from=None,
                           effective_until=None, replaces=None,
                           registry=None, at=None, device=None):
        """
        Accept a (possibly late) firmware announcement and re-interpret the
        original message under the resulting timeline.
        """
        target_registry = registry or self.registry
        target_device = device if device is not None else self.device_id
        published = type(self).revisions
        target_registry.announce(
            self.manufacturer,
            self._sentence_id(self.data),
            revision,
            fields=fields or published.get(revision),
            effective_from=effective_from,
            effective_until=effective_until,
            device=target_device,
            replaces=replaces,
            previous_fields=(published.get(replaces)
                             if replaces is not None else None))
        return self.reinterpret(
            registry=target_registry,
            at=self.observed_at if at is None else at,
            device=target_device)

    def __getattr__(self, name):
        # Only called when normal instance lookup fails; read everything via
        # __dict__ so the per-instance field definition is honoured.
        internal = self.__dict__
        index = internal.get('name_to_idx', {}).get(name)
        if index is None:
            raise AttributeError(name)
        field = internal['fields'][index]
        data = internal['data']
        value = data[index] if index < len(data) else ''
        if len(field) >= 3:
            if value == '':
                return None
            try:
                return field[2](value)
            except Exception:
                return value
        return value

    def __setattr__(self, name, value):
        index = self.__dict__.get('name_to_idx', {}).get(name)
        if index is None:
            object.__setattr__(self, name, value)
        else:
            self.data[index] = str(value)

    def __repr__(self):
        rendered = []
        for index, value in enumerate(self.data):
            if index >= len(self.fields):
                continue
            name = self.fields[index][1]
            rendered.append('%s=%r' % (name, getattr(self, name)))
        extra = [value for index, value in enumerate(self.data)
                 if index >= len(self.fields)]
        return '<%s(%s)%s>' % (
            type(self).__name__,
            ', '.join(rendered),
            extra and ' data=%r' % extra or '')
