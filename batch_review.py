"""Chunked batch quality review.

Files are collected chunk by chunk.  A batch declares its scope up front
as a manifest of expected chunks; the release decision is only made once
every declared chunk has arrived.

Quality is measured over *unique business rows* inside the declared
scope: the share of bad rows must not exceed ``max_bad_ratio``
(1/1000 by default).  The accounting rules are:

* Re-deliveries of the identical row are de-duplicated and never consume
  the bad-row budget twice.
* A row violating several rules at once still counts as a single bad row.
* Two deliveries sharing a business identity but carrying different
  content are a *conflict*: the identity counts as one business record
  and one bad row, and the contradictory versions are stored separately
  for manual review.

Because all state is kept as sets keyed by business identity, neither
the chunk boundaries nor the arrival order can change the final
denominator, the bad-row count, or the release decision.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, FrozenSet, Hashable, Iterable, Mapping, Optional, Tuple

__all__ = [
    "DEFAULT_MAX_BAD_RATIO",
    "UnknownChunkError",
    "Row",
    "Version",
    "ChunkReceipt",
    "Decision",
    "BatchReview",
]

DEFAULT_MAX_BAD_RATIO = Fraction(1, 1000)


class UnknownChunkError(KeyError):
    """Raised when a chunk arrives that was not declared in the manifest."""


@dataclass(frozen=True)
class Version:
    """One distinct incarnation of a business record."""

    content: Hashable
    violations: FrozenSet[Hashable] = frozenset()

    @property
    def is_bad(self) -> bool:
        """A version is bad if it breaks at least one rule."""
        return bool(self.violations)


@dataclass(frozen=True)
class Row:
    """One delivered line.

    ``identity`` is the business key, ``content`` the payload, and
    ``violations`` the ids of every rule the row breaks.  Two deliveries
    are the same row iff identity, content and violations all match.
    """

    identity: Hashable
    content: Hashable
    violations: FrozenSet[Hashable] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "violations", frozenset(self.violations))

    @property
    def version(self) -> Version:
        return Version(self.content, self.violations)


@dataclass(frozen=True)
class ChunkReceipt:
    """Acknowledgement returned for each submitted chunk."""

    chunk_id: Hashable
    delivered_rows: int
    unique_rows: int
    bad_rows: int


@dataclass(frozen=True)
class Decision:
    """Release decision, or a pending status while chunks are missing.

    ``approved`` is ``None`` while the declared scope is incomplete; in
    that case ``definitely_over_limit`` tells whether the batch is
    already guaranteed to exceed the ratio no matter what the missing
    chunks contain (i.e. even if every remaining declared row were
    unique and clean, the limit would still be broken).
    """

    complete: bool
    approved: Optional[bool]
    unique_rows: int
    bad_rows: int
    bad_ratio: Optional[Fraction]
    max_bad_ratio: Fraction
    missing_chunks: Tuple[Hashable, ...]
    definitely_over_limit: bool


def _version_sort_key(version: Version) -> Tuple[str, Tuple[str, ...]]:
    return (repr(version.content), tuple(sorted(map(repr, version.violations))))


class BatchReview:
    """Accumulates chunks of one batch and gates its release.

    ``expected_chunks`` maps each declared chunk id to the number of raw
    rows that chunk will deliver.  The declared totals make it possible
    to tell, while chunks are still missing, whether the batch is
    already certainly over the limit.
    """

    def __init__(
        self,
        expected_chunks: Mapping[Hashable, int],
        max_bad_ratio: Fraction = DEFAULT_MAX_BAD_RATIO,
    ) -> None:
        if any(count < 0 for count in expected_chunks.values()):
            raise ValueError("declared row counts must be non-negative")
        self._expected: Dict[Hashable, int] = dict(expected_chunks)
        self._max_bad_ratio = Fraction(max_bad_ratio)
        self._received: Dict[Hashable, int] = {}
        self._versions: Dict[Hashable, set] = {}

    def submit_chunk(self, chunk_id: Hashable, rows: Iterable[Row]) -> ChunkReceipt:
        """Ingest one chunk.  Re-submission of identical rows is a no-op."""
        if chunk_id not in self._expected:
            raise UnknownChunkError(chunk_id)
        delivered = 0
        for row in rows:
            self._versions.setdefault(row.identity, set()).add(row.version)
            delivered += 1
        self._received[chunk_id] = self._received.get(chunk_id, 0) + delivered
        return ChunkReceipt(
            chunk_id=chunk_id,
            delivered_rows=delivered,
            unique_rows=self.unique_rows,
            bad_rows=self.bad_rows,
        )

    @property
    def missing_chunks(self) -> Tuple[Hashable, ...]:
        """Declared chunks not yet received, in declaration order."""
        return tuple(c for c in self._expected if c not in self._received)

    @property
    def is_complete(self) -> bool:
        return not self.missing_chunks

    @property
    def unique_rows(self) -> int:
        """Denominator: distinct business identities seen so far."""
        return len(self._versions)

    @property
    def bad_rows(self) -> int:
        """Identities that are conflicting or whose only version is bad."""
        return sum(1 for versions in self._versions.values() if self._is_bad(versions))

    @property
    def conflicts(self) -> Dict[Hashable, Tuple[Version, ...]]:
        """Contradictory versions stored separately, keyed by identity."""
        return {
            identity: tuple(sorted(versions, key=_version_sort_key))
            for identity, versions in self._versions.items()
            if len(versions) > 1
        }

    @staticmethod
    def _is_bad(versions: set) -> bool:
        if len(versions) > 1:
            return True
        return next(iter(versions)).is_bad

    def decide(self) -> Decision:
        """Return the release decision for the current state.

        While declared chunks are missing the decision stays pending and
        only reports whether the limit is already certainly exceeded.
        """
        missing = self.missing_chunks
        unique = self.unique_rows
        bad = self.bad_rows
        if missing:
            declared_total = sum(self._expected.values())
            # Best possible final ratio: no more bad rows arrive and every
            # declared row turns out to be a distinct business identity.
            definitely_over = Fraction(bad) > declared_total * self._max_bad_ratio
            return Decision(
                complete=False,
                approved=None,
                unique_rows=unique,
                bad_rows=bad,
                bad_ratio=None,
                max_bad_ratio=self._max_bad_ratio,
                missing_chunks=missing,
                definitely_over_limit=definitely_over,
            )
        ratio = Fraction(bad, unique) if unique else Fraction(0)
        approved = ratio <= self._max_bad_ratio
        return Decision(
            complete=True,
            approved=approved,
            unique_rows=unique,
            bad_rows=bad,
            bad_ratio=ratio,
            max_bad_ratio=self._max_bad_ratio,
            missing_chunks=(),
            definitely_over_limit=not approved,
        )
