# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Reconcile re-published (same-name, re-built) releases against correction notices.

A release is identified by its semantic version *name*.  When the same version
name is re-published, the underlying artifact may be rebuilt: the name does not
change but its content digest does.  Different *sources* (mirrors) react
differently to a re-publication:

* some roll back to the original build,
* others keep serving the new build.

A :class:`CorrectionNotice` only applies to a bounded version *range* and to a
set of *fields* (e.g. the build metadata).  This module turns a collection of
daily :class:`source records <SourceRecord>` plus one or more notices into an
actionable report:

* which (source, date) snapshots must be re-fetched,
* which snapshots can be reused as-is,
* which legacy batches are missing an artifact summary and must be confirmed
  manually because the two builds would clean differently,
while guaranteeing that importing the very same notice twice never schedules a
second, redundant re-fetch round.
"""

from .base import SimpleSpec, Version


# The fields a notice may correct.  They map onto the components of a Version;
# 'build' is the field affected by a re-built artifact.
KNOWN_FIELDS = frozenset(['major', 'minor', 'patch', 'prerelease', 'build'])


class Build(object):
    """One of the builds ever published under a single version name."""

    __slots__ = ['label', 'digest']

    def __init__(self, label, digest):
        self.label = label
        self.digest = digest

    def __repr__(self):
        return 'Build(label=%r, digest=%r)' % (self.label, self.digest)

    def __eq__(self, other):
        if not isinstance(other, Build):
            return NotImplemented
        return self.label == other.label and self.digest == other.digest

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.label, self.digest))


class ArtifactDigest(object):
    """Summary of a collected artifact.

    ``digest`` is ``None`` for legacy batches that only recorded the version
    name without keeping an artifact summary.
    """

    __slots__ = ['build_label', 'digest']

    def __init__(self, build_label, digest=None):
        self.build_label = build_label
        self.digest = digest

    @property
    def has_summary(self):
        """Whether the batch kept a content digest."""
        return self.digest is not None

    def __repr__(self):
        return 'ArtifactDigest(build_label=%r, digest=%r)' % (
            self.build_label, self.digest)

    def __eq__(self, other):
        if not isinstance(other, ArtifactDigest):
            return NotImplemented
        return (
            self.build_label == other.build_label
            and self.digest == other.digest
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.build_label, self.digest))


class SourceRecord(object):
    """A single daily collection snapshot for one (source, version)."""

    __slots__ = ['source', 'date', 'version', 'artifact']

    def __init__(self, source, date, version, artifact):
        self.source = source
        self.date = date
        if isinstance(version, Version):
            self.version = version
        else:
            self.version = Version(version)
        if isinstance(artifact, ArtifactDigest):
            self.artifact = artifact
        else:
            build_label = artifact[0]
            digest = artifact[1] if len(artifact) > 1 else None
            self.artifact = ArtifactDigest(build_label, digest)

    @property
    def key(self):
        return (self.source, self.date)

    def __repr__(self):
        return 'SourceRecord(source=%r, date=%r, version=%r, artifact=%r)' % (
            self.source, self.date, self.version, self.artifact)


class CorrectionNotice(object):
    """A scoped correction.

    Args:
        notice_id: stable identifier used to make re-import idempotent.
        version_range: a :class:`~semantic_version.base.SimpleSpec` expression;
            only versions matching it are affected.
        fields: the corrected fields; only snapshots carrying those fields are
            affected.
        expected_build: the build the corrected artifact should carry.
        builds: every known :class:`Build` for the affected version name(s).
        affected_versions: explicit version name(s) the notice applies to. When
            omitted, the range alone decides scope.
    """

    def __init__(self, notice_id, version_range, fields, expected_build,
                 builds, affected_versions=None):
        self.notice_id = notice_id
        if isinstance(version_range, SimpleSpec):
            self.spec = version_range
        else:
            self.spec = SimpleSpec(version_range)
        self.fields = frozenset(fields)
        unknown = self.fields - KNOWN_FIELDS
        if unknown:
            raise ValueError("Unknown corrected field(s): %r" % sorted(unknown))
        self.expected_build = expected_build
        self.builds = tuple(builds)
        if affected_versions is None:
            self.affected_versions = None
        else:
            self.affected_versions = frozenset(
                v if isinstance(v, Version) else Version(v)
                for v in affected_versions
            )

    def applies_to(self, version):
        """Whether the notice's version range (and explicit list) covers a version."""
        if not self.spec.match(version):
            return False
        if self.affected_versions is not None and version not in self.affected_versions:
            return False
        return True

    def touches(self, version):
        """Whether a record of this version carries any of the corrected fields.

        The core components (``major``/``minor``/``patch``) and the artifact
        ``build`` field are present on every release record.  A ``prerelease``
        correction, though, only concerns versions that actually carry a
        prerelease component.
        """
        if self.fields - {'prerelease'}:
            return True
        if 'prerelease' in self.fields:
            return bool(version.prerelease)
        return False

    def in_scope(self, version):
        return self.applies_to(version) and self.touches(version)

    def build_for_label(self, label):
        for build in self.builds:
            if build.label == label:
                return build
        return None

    def builds_clean_differently(self):
        """Whether the known builds would yield different cleaned output.

        Two builds are distinguishable when their content digests differ; the
        corrected field is then ambiguous for a batch that recorded no digest.
        """
        digests = {build.digest for build in self.builds}
        return len(digests) > 1

    def __repr__(self):
        return 'CorrectionNotice(notice_id=%r, spec=%r, fields=%r)' % (
            self.notice_id, self.spec.expression, sorted(self.fields))


class ReconciliationReport(object):
    """Result of reconciling source records against correction notices."""

    def __init__(self):
        # (source, date) -> SourceRecord that must be re-fetched.
        self.refetch = {}
        # (source, date) -> SourceRecord that is already correct / reusable.
        self.reusable = {}
        # (source, date) -> SourceRecord lacking a digest, awaiting confirmation.
        self.unconfirmed = {}
        # Records outside every notice's scope, kept for completeness.
        self.out_of_scope = {}
        # Notice ids that were actually processed (de-duplicated).
        self.processed_notice_ids = []

    @staticmethod
    def _sorted(records):
        return [records[key] for key in sorted(records)]

    @property
    def refetch_records(self):
        return self._sorted(self.refetch)

    @property
    def reusable_records(self):
        return self._sorted(self.reusable)

    @property
    def unconfirmed_records(self):
        return self._sorted(self.unconfirmed)

    @property
    def out_of_scope_records(self):
        return self._sorted(self.out_of_scope)

    def refetch_dates(self, source=None):
        """Sorted list of dates requiring a re-fetch, optionally for one source."""
        keys = sorted(self.refetch)
        if source is not None:
            return [date for src, date in keys if src == source]
        return [date for _src, date in keys]

    def __repr__(self):
        return (
            'ReconciliationReport(refetch=%d, reusable=%d, unconfirmed=%d, '
            'out_of_scope=%d)' % (
                len(self.refetch),
                len(self.reusable),
                len(self.unconfirmed),
                len(self.out_of_scope),
            )
        )


class Reconciler(object):
    """Apply correction notices to collected source records.

    A reconciler is stateful on purpose: feeding the same notice in again (a
    re-import) is a no-op, so no extra re-fetch round is ever scheduled.
    """

    def __init__(self):
        self._seen_notice_ids = set()
        self._notices = []

    def add_notice(self, notice):
        """Register a notice; return False if it was already imported."""
        if notice.notice_id in self._seen_notice_ids:
            return False
        self._seen_notice_ids.add(notice.notice_id)
        self._notices.append(notice)
        return True

    def _notice_for(self, version):
        """Return the first in-scope notice for a version, or None."""
        for notice in self._notices:
            if notice.in_scope(version):
                return notice
        return None

    def reconcile(self, records):
        report = ReconciliationReport()
        # Preserve notice processing order, without re-processing duplicates.
        report.processed_notice_ids = [
            notice.notice_id for notice in self._notices
        ]

        for record in records:
            notice = self._notice_for(record.version)
            if notice is None:
                report.out_of_scope[record.key] = record
                continue

            artifact = record.artifact
            if not artifact.has_summary:
                # Legacy batch: only the version name was kept.  It can only be
                # classified when the competing builds clean identically.
                if notice.builds_clean_differently():
                    report.unconfirmed[record.key] = record
                else:
                    report.reusable[record.key] = record
                continue

            observed = notice.build_for_label(artifact.build_label)
            if observed is None:
                # Unknown build label: cannot decide, require confirmation.
                report.unconfirmed[record.key] = record
            elif observed.digest == notice.expected_build.digest:
                report.reusable[record.key] = record
            else:
                report.refetch[record.key] = record

        return report


def reconcile(records, notices):
    """Convenience wrapper: build a :class:`Reconciler`, import notices, run once."""
    reconciler = Reconciler()
    for notice in notices:
        reconciler.add_notice(notice)
    return reconciler.reconcile(records)
