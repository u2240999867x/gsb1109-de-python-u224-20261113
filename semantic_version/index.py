# -*- coding: utf-8 -*-
# Copyright (c) The python-semanticversion project
# This code is distributed under the two-clause BSD License.

"""Indexing of versioned package artifacts.

An artifact index keeps two concerns explicitly separate:

- *precedence*, the semver ordering used when selecting the newest compatible
  release; build metadata never participates in that ordering, so
  ``2.4.0+cn`` and ``2.4.0+eu`` are tied rather than ranked;
- *identity*, the exact ``(version, digest)`` pairs observed and the mirror(s)
  each pair was seen on, kept around for traceability.

Observations carrying the same digest are merged by source, while a single
version published with two different digests keeps both entries visible.
Selection of the newest compatible release is deterministic and never relies
on insertion (download) order: when several releases share the highest
precedence, all of them -- together with their content differences -- are
returned instead of picking one arbitrarily.
"""

from .base import BaseSpec, SimpleSpec, Version


def _coerce_version(version):
    """Return a :class:`Version`, parsing plain strings when needed."""
    if isinstance(version, Version):
        return version
    if isinstance(version, str):
        return Version(version)
    raise TypeError(
        "Expected a Version or version string, got %r" % (version,))


def _coerce_spec(spec):
    """Return a :class:`BaseSpec`, parsing plain strings with the simple syntax."""
    if isinstance(spec, BaseSpec):
        return spec
    if isinstance(spec, str):
        return SimpleSpec(spec)
    raise TypeError(
        "Expected a BaseSpec or spec string, got %r" % (spec,))


class ArtifactRecord(object):
    """A single artifact observation.

    Records that a content :attr:`digest` of :attr:`version` was seen on the
    :attr:`source` mirror (if any). The same digest observed on several
    mirrors produces several records that the index later merges.
    """

    __slots__ = ('version', 'digest', 'source')

    def __init__(self, version, digest, source=None):
        self.version = _coerce_version(version)
        if not isinstance(digest, str) or not digest:
            raise ValueError(
                "Artifact digest must be a non-empty string, got %r" % (digest,))
        if source is not None and not isinstance(source, str):
            raise TypeError(
                "Artifact source must be a string or None, got %r" % (source,))
        self.digest = digest
        self.source = source

    def __eq__(self, other):
        if not isinstance(other, ArtifactRecord):
            return NotImplemented
        return (
            self.version == other.version
            and self.digest == other.digest
            and self.source == other.source
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.version, self.digest, self.source))

    def __repr__(self):
        source = '' if self.source is None else ', %r' % self.source
        return 'ArtifactRecord(%r, %r%s)' % (
            str(self.version), self.digest, source)


class PackageRelease(object):
    """Every artifact known for one exact :class:`Version`.

    A release aggregates the digests observed for that version and, for each
    digest, the sources it was seen on. When the same version was published
    with different contents, every digest is retained and
    :attr:`has_divergent_digests` is true.
    """

    __slots__ = ('version', '_digest_sources')

    def __init__(self, version, digest_sources=None):
        self.version = _coerce_version(version)
        self._digest_sources = {}
        if digest_sources is not None:
            for digest, sources in digest_sources.items():
                if not isinstance(digest, str) or not digest:
                    raise ValueError(
                        "Artifact digest must be a non-empty string, got %r" % (digest,))
                self._digest_sources[digest] = set(sources or ())

    def _add(self, digest, source=None):
        self._digest_sources.setdefault(digest, set())
        if source is not None:
            self._digest_sources[digest].add(source)

    @property
    def digests(self):
        """Frozenset of every distinct content digest for this version."""
        return frozenset(self._digest_sources)

    @property
    def sources(self):
        """Frozenset of every mirror any digest was observed on."""
        sources = set()
        for digest_sources in self._digest_sources.values():
            sources.update(digest_sources)
        return frozenset(sources)

    @property
    def has_divergent_digests(self):
        """Whether the same version was published with several contents."""
        return len(self._digest_sources) > 1

    def sources_for(self, digest):
        """Mirrors the given digest was observed on (empty set if unknown)."""
        return frozenset(self._digest_sources.get(digest, ()))

    @property
    def artifacts(self):
        """One :class:`ArtifactRecord` per observed ``(digest, source)`` pair."""
        records = []
        for digest in sorted(self._digest_sources):
            sources = self._digest_sources[digest]
            if not sources:
                records.append(ArtifactRecord(self.version, digest))
            else:
                for source in sorted(sources):
                    records.append(ArtifactRecord(self.version, digest, source))
        return tuple(records)

    def __eq__(self, other):
        if not isinstance(other, PackageRelease):
            return NotImplemented
        return (
            self.version == other.version
            and self._digest_sources == other._digest_sources
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        digest_sources = frozenset(
            (digest, frozenset(sources))
            for digest, sources in self._digest_sources.items()
        )
        return hash((self.version, digest_sources))

    def __repr__(self):
        return 'PackageRelease(%r, %r)' % (
            str(self.version), self._digest_sources)


class Selection(object):
    """Result of querying an index for the newest compatible release.

    :attr:`candidates` holds every release sharing the highest matching
    precedence, ordered deterministically (never by insertion order). A
    unique winner is available through :attr:`chosen`; on a tie it is
    :data:`None` and :attr:`content_differs` / :attr:`digests` describe how
    the candidate contents differ.
    """

    __slots__ = ('candidates',)

    def __init__(self, candidates=()):
        self.candidates = tuple(candidates)

    @property
    def chosen(self):
        """The sole candidate, or :data:`None` when precedence is tied."""
        if len(self.candidates) == 1:
            return self.candidates[0]
        return None

    @property
    def is_tie(self):
        """Whether several releases share the highest matching precedence."""
        return len(self.candidates) > 1

    @property
    def digests(self):
        """Frozenset of every digest offered by the candidates."""
        digests = set()
        for candidate in self.candidates:
            digests.update(candidate.digests)
        return frozenset(digests)

    @property
    def content_differs(self):
        """Whether candidates expose more than one distinct content digest."""
        return len(self.digests) > 1

    def __bool__(self):
        return bool(self.candidates)

    __nonzero__ = __bool__

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self):
        return len(self.candidates)

    def __eq__(self, other):
        if not isinstance(other, Selection):
            return NotImplemented
        return self.candidates == other.candidates

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash(self.candidates)

    def __repr__(self):
        return 'Selection(%r)' % (self.candidates,)


class PackageIndex(object):
    """In-memory index of package artifacts.

    The index stores, for every exact version, the :class:`PackageRelease`
    describing its digests and mirror sources -- the traceable identity of
    the artifacts -- while version precedence remains available through the
    :class:`Version` objects themselves for selection.
    """

    def __init__(self, artifacts=()):
        self._releases = {}
        for artifact in artifacts:
            self.add_record(artifact)

    def add(self, version, digest, source=None):
        """Record a ``(version, digest[, source])`` observation and return self."""
        return self.add_record(ArtifactRecord(version, digest, source))

    def add_record(self, artifact):
        """Merge an :class:`ArtifactRecord` into the index and return self."""
        if not isinstance(artifact, ArtifactRecord):
            raise TypeError(
                "Expected an ArtifactRecord, got %r" % (artifact,))
        release = self._releases.get(artifact.version)
        if release is None:
            release = PackageRelease(artifact.version)
            self._releases[artifact.version] = release
        release._add(artifact.digest, artifact.source)
        return self

    def get(self, version):
        """Return the :class:`PackageRelease` for a version, or :data:`None`."""
        return self._releases.get(_coerce_version(version))

    @property
    def releases(self):
        """All indexed releases, ordered by version precedence."""
        return tuple(sorted(
            self._releases.values(),
            key=lambda release: release.version.precedence_key,
        ))

    def select(self, spec):
        """Select the newest compatible release(s) for a specification.

        Versions matching ``spec`` are ranked by semver precedence, ignoring
        build metadata. Every release sharing the top precedence is returned
        in a :class:`Selection`; download/insertion order is never used to
        break a tie.
        """
        spec = _coerce_spec(spec)
        matching = [
            release for release in self._releases.values()
            if spec.match(release.version)
        ]
        if not matching:
            return Selection()

        precedence_groups = {}
        for release in matching:
            # truncate('prerelease') strips build metadata, leaving the
            # semver precedence on which releases may be tied; truncated
            # Version objects stay hashable, unlike raw precedence keys.
            precedence = release.version.truncate('prerelease')
            precedence_groups.setdefault(precedence, []).append(release)

        top_precedence = max(precedence_groups)
        tied = precedence_groups[top_precedence]
        # Deterministic, content-based ordering: independent of insertion order.
        tied = sorted(
            tied,
            key=lambda release: (
                str(release.version),
                sorted(release.digests),
            ),
        )
        return Selection(tuple(tied))

    def __contains__(self, version):
        return _coerce_version(version) in self._releases

    def __len__(self):
        return len(self._releases)

    def __iter__(self):
        return iter(self.releases)

    def __repr__(self):
        return 'PackageIndex(%r)' % (self.releases,)
