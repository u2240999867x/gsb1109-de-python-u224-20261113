# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Migration of loosely-collected version records into strict SemVer.

Legacy collection records are inconsistent: some only store the
``major.minor`` (or even just ``major``) components, while others hold an
exact ``X.Y.Z`` version.  Upgrading them to strict :class:`~semantic_version.Version`
objects must rely solely on evidence from the release catalogue:

* the releases actually available in the release directory at the
  collection date, and
* the channel on which every artifact was published.

A record is only upgraded to an exact version when a single published
artifact is compatible with it.  Missing components are never silently
filled with zeroes, and an artifact that never reached the record's
channel is never selected, even when its version matches.  Every
compatible artifact ruled out by those constraints is retained on the
migration result with the reason it was excluded, so each completion can
be audited afterwards.
"""

import datetime
import warnings

from .base import Version


# Exclusion reasons, exposed for programmatic auditing.
REASON_CHANNEL = 'channel'
REASON_CUTOFF = 'cutoff'


def _parse_date(value):
    if isinstance(value, datetime.date):
        return value
    return datetime.datetime.strptime(value, '%Y-%m-%d').date()


def _parse_record_version(version_text):
    # Partial versions are a deprecated parsing mode inside the library;
    # the deprecation is about long-term Version usage, not about reading
    # the legacy records this module is built to handle.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        return Version(version_text, partial=True)


class Release(object):
    """An artifact published to a channel on a given day."""

    __slots__ = ['version', 'channel', 'released_on']

    def __init__(self, version, channel, released_on):
        self.version = version if isinstance(version, Version) else Version(version)
        self.channel = channel
        self.released_on = _parse_date(released_on)

    def __eq__(self, other):
        if not isinstance(other, Release):
            return NotImplemented
        return (
            self.version == other.version
            and self.channel == other.channel
            and self.released_on == other.released_on
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.version, self.channel, self.released_on))

    def __lt__(self, other):
        if not isinstance(other, Release):
            return NotImplemented
        return (
            self.version.precedence_key,
            self.released_on,
            self.channel,
        ) < (
            other.version.precedence_key,
            other.released_on,
            other.channel,
        )

    def __repr__(self):
        return 'Release(%r, %r, %r)' % (
            str(self.version),
            self.channel,
            self.released_on.isoformat(),
        )


class ExcludedRelease(object):
    """A compatible release that could not be chosen for a record."""

    __slots__ = ['release', 'reason']

    def __init__(self, release, reason):
        self.release = release
        self.reason = reason

    def __eq__(self, other):
        if not isinstance(other, ExcludedRelease):
            return NotImplemented
        return self.release == other.release and self.reason == other.reason

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.release, self.reason))

    def __repr__(self):
        return 'ExcludedRelease(%r, %r)' % (self.release, self.reason)


class LegacyRecord(object):
    """A version record collected before strict SemVer enforcement.

    Args:
        identifier: stable identifier of the collected item.
        version_text: the recorded version; an exact ``X.Y.Z`` text or a
            partial ``X`` / ``X.Y`` record.
        channel: channel the item was collected from.
        collected_on: collection date (a :class:`datetime.date` or an
            ISO ``YYYY-MM-DD`` string); releases published after it are
            not part of the release directory.
    """

    __slots__ = ['identifier', 'version_text', 'channel', 'collected_on']

    def __init__(self, identifier, version_text, channel, collected_on):
        self.identifier = identifier
        self.version_text = version_text
        self.channel = channel
        self.collected_on = _parse_date(collected_on)

    def __repr__(self):
        return 'LegacyRecord(%r, %r, %r, %r)' % (
            self.identifier,
            self.version_text,
            self.channel,
            self.collected_on.isoformat(),
        )


class ReleaseCatalogue(object):
    """The release directory: every published artifact, channel and date."""

    def __init__(self, releases, as_of=None):
        # De-duplicate identical catalog entries while keeping order.
        seen = set()
        unique = []
        for release in releases:
            if not isinstance(release, Release):
                release = Release(*release)
            if release not in seen:
                seen.add(release)
                unique.append(release)
        self._releases = tuple(unique)
        self.as_of = _parse_date(as_of) if as_of is not None else None

    @classmethod
    def from_directories(cls, directories, as_of=None):
        """Build a catalogue from release directories laid out by channel.

        Args:
            directories: mapping ``{channel: {released_on: [version, ...]}}``,
                as cut off at the collection date.
            as_of: optional catalogue-wide cutoff date.
        """
        releases = []
        for channel, dated in directories.items():
            for released_on, versions in dated.items():
                for version in versions:
                    releases.append(Release(version, channel, released_on))
        return cls(releases, as_of=as_of)

    @property
    def releases(self):
        return self._releases

    def classify(self, record):
        """Split compatible releases into candidates and exclusions.

        Returns a ``(candidates, excluded)`` tuple.  ``candidates`` are
        releases matching the record's version and available on its
        channel at the collection date; every other version-compatible
        release is returned as an :class:`ExcludedRelease` carrying the
        constraint that ruled it out.
        """
        parsed = _parse_record_version(record.version_text)
        exact = parsed.minor is not None and parsed.patch is not None
        cutoff = record.collected_on

        candidates = []
        excluded = []
        for release in self._releases:
            if not self._version_matches(parsed, exact, release.version):
                continue
            if release.channel != record.channel:
                excluded.append(ExcludedRelease(release, REASON_CHANNEL))
            elif cutoff is not None and release.released_on > cutoff:
                excluded.append(ExcludedRelease(release, REASON_CUTOFF))
            elif self.as_of is not None and release.released_on > self.as_of:
                excluded.append(ExcludedRelease(release, REASON_CUTOFF))
            else:
                candidates.append(release)

        return tuple(sorted(candidates)), tuple(sorted(
            excluded, key=lambda item: item.release,
        ))

    @staticmethod
    def _version_matches(parsed, exact, version):
        if exact:
            return version == parsed
        if parsed.minor is None:
            return version.major == parsed.major
        return version.major == parsed.major and version.minor == parsed.minor


class MigrationResult(object):
    """Outcome of migrating one :class:`LegacyRecord`."""

    __slots__ = ['record', 'resolved', 'version', 'candidates', 'excluded', 'reason']

    def __init__(self, record, resolved, version, candidates, excluded, reason=None):
        self.record = record
        self.resolved = resolved
        self.version = version
        self.candidates = candidates
        self.excluded = excluded
        self.reason = reason

    @property
    def feasible_versions(self):
        """Versions still viable for an unresolved record."""
        versions = []
        seen = set()
        for release in self.candidates:
            if release.version not in seen:
                seen.add(release.version)
                versions.append(release.version)
        return tuple(versions)

    def __repr__(self):
        if self.resolved:
            return 'MigrationResult(%r, resolved=%r)' % (
                self.record.identifier, str(self.version),
            )
        return 'MigrationResult(%r, candidates=%d)' % (
            self.record.identifier, len(self.candidates),
        )


def migrate_record(record, catalogue):
    """Migrate a single record against a release catalogue."""
    candidates, excluded = catalogue.classify(record)
    parsed = _parse_record_version(record.version_text)
    exact = parsed.minor is not None and parsed.patch is not None

    if len(candidates) == 1:
        return MigrationResult(
            record, True, candidates[0].version, candidates, excluded,
        )

    if len(candidates) > 1:
        if exact:
            reason = (
                "Several builds of %s reached channel %r before %s; the record "
                "does not identify a unique artifact." % (
                    record.version_text, record.channel,
                    record.collected_on.isoformat(),
                )
            )
        else:
            reason = (
                "Several releases match %r on channel %r before %s; keeping the "
                "feasible set instead of guessing missing components." % (
                    record.version_text, record.channel,
                    record.collected_on.isoformat(),
                )
            )
        return MigrationResult(record, False, None, candidates, excluded, reason)

    cutoff_exclusions = [
        item for item in excluded if item.reason == REASON_CUTOFF
    ]
    channel_exclusions = [
        item for item in excluded if item.reason == REASON_CHANNEL
    ]
    if cutoff_exclusions:
        reason = (
            "No release matching %r was available on channel %r by %s; "
            "matching artifacts were only published later." % (
                record.version_text, record.channel,
                record.collected_on.isoformat(),
            )
        )
    elif channel_exclusions:
        channels = sorted({item.release.channel for item in channel_exclusions})
        reason = (
            "No artifact matching %r was ever published to channel %r; it only "
            "appeared on %s." % (
                record.version_text, record.channel, ', '.join(channels),
            )
        )
    else:
        reason = "No published release matches %r." % record.version_text
    return MigrationResult(record, False, None, candidates, excluded, reason)


def migrate(records, catalogue_or_releases, as_of=None):
    """Migrate an iterable of records against a release catalogue.

    Args:
        records: iterable of :class:`LegacyRecord`.
        catalogue_or_releases: a :class:`ReleaseCatalogue`, or an iterable
            of :class:`Release` objects / ``(version, channel, released_on)``
            tuples from which one is built.
        as_of: optional catalogue-wide cutoff, used when building a
            catalogue from raw releases.
    """
    if isinstance(catalogue_or_releases, ReleaseCatalogue):
        catalogue = catalogue_or_releases
    else:
        catalogue = ReleaseCatalogue(catalogue_or_releases, as_of=as_of)
    return [migrate_record(record, catalogue) for record in records]
