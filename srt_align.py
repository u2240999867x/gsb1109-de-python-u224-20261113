#!/usr/bin/env python
# coding=utf8

"""Align two subtitle tracks into a bilingual table.

This is intended for material (for example, interviews) whose two language
tracks were segmented independently.  One subtitle in the source track often
corresponds to two or more consecutive subtitles in the translation track, and
a row may legitimately have no counterpart on the other side.

Alignment is monotonic and is performed independently between manually
confirmed pairs.  Those pairs are hard boundaries: an item on one side of a
confirmed pair can never be matched with an item on the other side.  When a
small span has two equally plausible layouts, the rows there are marked as
candidates instead of being guessed at; the rest of the table is still
delivered normally.
"""

from __future__ import unicode_literals

import codecs
import csv
import io
import json
import logging
import os
import re
from datetime import timedelta

import srt

log = logging.getLogger(__name__)

__all__ = [
    "AlignmentError",
    "AlignmentRow",
    "Candidate",
    "CandidateOption",
    "Alignment",
    "align_subtitles",
    "align_srt",
    "align_files",
    "align",
    "parse_anchor",
    "parse_reference",
    "load_anchors",
    "render_table",
    "format_table",
]

# Scoring constants.  Only their relative sizes matter.  A match is worth much
# more than leaving both sides unmatched; overlap and boundary agreement decide
# which match wins; a penalty per extra grouped subtitle discourages a large
# group from swallowing content that belongs to its neighbour.
MATCH_BASE = 6.0
DICE_WEIGHT = 12.0
BOUNDARY_WEIGHT = 2.0
PROXIMITY_WEIGHT = 5.0
MATCH_PENALTY = 18.0
GROUP_PENALTY = 2.0
SKIP_PENALTY = 3.0
SCORE_EPSILON = 1e-9

DEFAULT_MAX_GAP_MS = 500
DEFAULT_MAX_GROUP = 4
DEFAULT_AMBIGUITY_TOLERANCE = 0.5

ROW_ALIGNED = "aligned"
ROW_SOURCE_ONLY = "untranslated"
ROW_TRANSLATION_ONLY = "no_source"
ROW_CANDIDATE = "candidate"

TS_RE = re.compile(
    r"^\s*(\d+)[,.:，．。：](\d+)[,.:，．。：](\d+)[,.:，．。：]?(\d*)\s*$"
)


class AlignmentError(ValueError):
    """Raised when the inputs or the manual anchors cannot be used."""


def _to_ms(delta):
    """Convert a :class:`datetime.timedelta` to integer milliseconds."""
    return (
        (delta.days * 86400 + delta.seconds) * 1000
        + delta.microseconds // 1000
    )


def _group_span(subs):
    return (
        min(_to_ms(sub.start) for sub in subs),
        max(_to_ms(sub.end) for sub in subs),
    )


def _overlap(a, b):
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def _match_score(source_subs, translation_subs, max_gap_ms):
    """Score a (possibly one-to-many) subtitle match.

    ``None`` means the two groups are too far apart to be considered a match.
    """
    source_span = _group_span(source_subs)
    translation_span = _group_span(translation_subs)
    source_len = max(0, source_span[1] - source_span[0])
    translation_len = max(0, translation_span[1] - translation_span[0])
    denom = source_len + translation_len
    extra_items = len(source_subs) - 1 + len(translation_subs) - 1

    if denom <= 0:
        if source_span == translation_span:
            return (
                MATCH_BASE
                + DICE_WEIGHT
                + BOUNDARY_WEIGHT
                - MATCH_PENALTY
                - GROUP_PENALTY * extra_items
            )
        dice = 0.0
        boundary = 0.0
        proximity = 0.0
    else:
        inter = _overlap(source_span, translation_span)
        dice = (2.0 * inter) / denom
        boundary = 1.0 - (
            abs(source_span[0] - translation_span[0])
            + abs(source_span[1] - translation_span[1])
        ) / float(denom)
        boundary = max(0.0, boundary)

        if inter <= 0:
            gap = max(
                source_span[0] - translation_span[1],
                translation_span[0] - source_span[1],
            )
            if gap > max_gap_ms:
                return None
            proximity = 1.0 - (float(gap) / max(1, max_gap_ms))
        else:
            proximity = 0.0

    return (
        MATCH_BASE
        + DICE_WEIGHT * dice
        + BOUNDARY_WEIGHT * boundary
        + PROXIMITY_WEIGHT * proximity
        - MATCH_PENALTY
        - GROUP_PENALTY * extra_items
    )


def _score_spans(source_span, translation_span, extra_items, max_gap_ms):
    """Score two already-computed (start_ms, end_ms) spans."""
    source_len = max(0, source_span[1] - source_span[0])
    translation_len = max(0, translation_span[1] - translation_span[0])
    denom = source_len + translation_len

    if denom <= 0:
        if source_span == translation_span:
            return (
                MATCH_BASE
                + DICE_WEIGHT
                + BOUNDARY_WEIGHT
                - MATCH_PENALTY
                - GROUP_PENALTY * extra_items
            )
        dice = 0.0
        boundary = 0.0
        proximity = 0.0
    else:
        inter = _overlap(source_span, translation_span)
        dice = (2.0 * inter) / denom
        boundary = 1.0 - (
            abs(source_span[0] - translation_span[0])
            + abs(source_span[1] - translation_span[1])
        ) / float(denom)
        boundary = max(0.0, boundary)

        if inter <= 0:
            gap = max(
                source_span[0] - translation_span[1],
                translation_span[0] - source_span[1],
            )
            if gap > max_gap_ms:
                return None
            proximity = 1.0 - (float(gap) / max(1, max_gap_ms))
        else:
            proximity = 0.0

    return (
        MATCH_BASE
        + DICE_WEIGHT * dice
        + BOUNDARY_WEIGHT * boundary
        + PROXIMITY_WEIGHT * proximity
        - MATCH_PENALTY
        - GROUP_PENALTY * extra_items
    )


def _group_spans(spans, max_group):
    """Precompute spans for each starting index and group size."""
    result = []
    total = len(spans)
    for idx in range(total):
        per_size = []
        furthest_end = spans[idx][1]
        for size in range(1, max_group + 1):
            if idx + size > total:
                break
            if spans[idx + size - 1][1] > furthest_end:
                furthest_end = spans[idx + size - 1][1]
            per_size.append((spans[idx][0], furthest_end))
        result.append(per_size)
    return result


def _span_score(source_spans, translation_spans, max_gap_ms):
    """Score a match from precomputed millisecond spans."""
    source_span = (
        min(span[0] for span in source_spans),
        max(span[1] for span in source_spans),
    )
    translation_span = (
        min(span[0] for span in translation_spans),
        max(span[1] for span in translation_spans),
    )
    return _score_spans(
        source_span,
        translation_span,
        len(source_spans) - 1 + len(translation_spans) - 1,
        max_gap_ms,
    )


def _content(subs):
    return "\n".join(sub.content for sub in subs)


class AlignmentRow(object):
    """A single row of the bilingual table.

    ``source``/``translation`` are strings; several subtitles are joined with
    newlines.  ``status`` is one of :data:`ROW_ALIGNED`,
    :data:`ROW_SOURCE_ONLY`, :data:`ROW_TRANSLATION_ONLY` or
    :data:`ROW_CANDIDATE`.
    """

    def __init__(
        self,
        start,
        end,
        source="",
        translation="",
        status=ROW_ALIGNED,
        source_subs=(),
        translation_subs=(),
        candidates=(),
        index=None,
    ):
        self.start = start
        self.end = end
        self.source = source
        self.translation = translation
        self.status = status
        self.source_subs = tuple(source_subs)
        self.translation_subs = tuple(translation_subs)
        self.candidates = tuple(candidates)
        self.index = index

    @property
    def chinese(self):
        return self.source

    @property
    def english(self):
        return self.translation

    def to_dict(self):
        return {
            "index": self.index,
            "start": srt.timedelta_to_srt_timestamp(self.start),
            "end": srt.timedelta_to_srt_timestamp(self.end),
            "source": self.source,
            "translation": self.translation,
            "chinese": self.source,
            "english": self.translation,
            "status": self.status,
            "source_subtitles": [sub.index for sub in self.source_subs],
            "translation_subtitles": [
                sub.index for sub in self.translation_subs
            ],
            "candidates": [option.to_dict() for option in self.candidates],
        }

    def __repr__(self):
        return (
            "AlignmentRow(index={0!r}, start={1!r}, end={2!r}, "
            "source={3!r}, translation={4!r}, status={5!r})"
        ).format(
            self.index,
            self.start,
            self.end,
            self.source,
            self.translation,
            self.status,
        )


class CandidateOption(object):
    """One possible layout for a small ambiguous span.

    ``rows`` contains :class:`AlignmentRow` objects with their own source and
    translation text, so a caller can show exactly where the competing split
    falls.
    """

    def __init__(self, rows, score=None):
        self.rows = tuple(rows)
        self.score = score

    @property
    def source(self):
        return "\n".join(row.source for row in self.rows if row.source)

    @property
    def translation(self):
        return "\n".join(
            row.translation for row in self.rows if row.translation
        )

    def to_dict(self):
        return {
            "source": self.source,
            "translation": self.translation,
            "score": self.score,
            "rows": [
                {
                    "start": srt.timedelta_to_srt_timestamp(row.start),
                    "end": srt.timedelta_to_srt_timestamp(row.end),
                    "source": row.source,
                    "translation": row.translation,
                }
                for row in self.rows
            ],
        }

    def __repr__(self):
        return "CandidateOption(source={0!r}, translation={1!r})".format(
            self.source, self.translation
        )


class Candidate(object):
    """A small span where two or more alignments are equally plausible."""

    def __init__(self, start, end, chosen, alternatives, chosen_score=None):
        self.start = start
        self.end = end
        self.chosen = CandidateOption(chosen, chosen_score)
        self.alternatives = tuple(
            CandidateOption(rows, score)
            for rows, score in alternatives
        )

    def to_dict(self):
        return {
            "start": srt.timedelta_to_srt_timestamp(self.start),
            "end": srt.timedelta_to_srt_timestamp(self.end),
            "chosen": self.chosen.to_dict(),
            "alternatives": [
                alternative.to_dict() for alternative in self.alternatives
            ],
        }

    def __repr__(self):
        return "Candidate(start={0!r}, end={1!r}, alternatives={2})".format(
            self.start, self.end, len(self.alternatives)
        )


class Alignment(object):
    """Result of :func:`align_subtitles`."""

    def __init__(self, rows, candidates=(), anchors=()):
        self.rows = list(rows)
        self.candidates = list(candidates)
        self.anchors = list(anchors)

        for idx, row in enumerate(self.rows, start=1):
            row.index = idx

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, item):
        return self.rows[item]

    def to_dict(self):
        return {
            "rows": [row.to_dict() for row in self.rows],
            "candidates": [
                candidate.to_dict() for candidate in self.candidates
            ],
            "anchors": [
                [source.index, translation.index]
                for source, translation in self.anchors
            ],
        }

    def render(
        self,
        format="tsv",
        header=True,
        source_header="source",
        translation_header="translation",
    ):
        if (format or "").lower() in ("json", "js"):
            return json.dumps(
                self.to_dict(), ensure_ascii=False, indent=2
            ) + "\n"
        return render_table(
            self.rows,
            format=format,
            header=header,
            source_header=source_header,
            translation_header=translation_header,
        )


class _PathRow(object):
    """Internal representation of one alignment decision."""

    __slots__ = ("z0", "z1", "e0", "e1", "score")

    def __init__(self, z0, z1, e0, e1, score):
        self.z0 = z0
        self.z1 = z1
        self.e0 = e0
        self.e1 = e1
        self.score = score

    def key(self):
        return (self.z0, self.z1, self.e0, self.e1)


def _allowed_transitions(max_group):
    """Yield ``(source_items, translation_items)`` group sizes."""
    for size in range(1, max_group + 1):
        yield (1, size)
        if size > 1:
            yield (size, 1)


def _dynamic_program(source_subs, translation_subs, max_gap_ms, max_group):
    """Return the best monotonic alignment path.

    ``f[i][j]`` is the best score for aligning ``source[:i]`` with
    ``translation[:j]``.
    """
    n = len(source_subs)
    m = len(translation_subs)
    transitions = list(_allowed_transitions(max_group))
    neg_inf = float("-inf")

    # Convert timestamps once instead of on every candidate evaluation.
    source_spans = [
        (_to_ms(sub.start), _to_ms(sub.end)) for sub in source_subs
    ]
    translation_spans = [
        (_to_ms(sub.start), _to_ms(sub.end)) for sub in translation_subs
    ]

    source_groups = _group_spans(source_spans, max_group)
    translation_groups = _group_spans(translation_spans, max_group)

    def span_score(z0, z1, e0, e1):
        return _score_spans(
            source_groups[z0][z1 - z0 - 1],
            translation_groups[e0][e1 - e0 - 1],
            (z1 - z0 - 1) + (e1 - e0 - 1),
            max_gap_ms,
        )

    f = [[neg_inf] * (m + 1) for _ in range(n + 1)]
    choice = [[None] * (m + 1) for _ in range(n + 1)]
    f[0][0] = 0.0

    for i in range(n + 1):
        for j in range(m + 1):
            current = f[i][j]
            if current == neg_inf:
                continue

            if i < n:
                candidate = current - SKIP_PENALTY
                if candidate > f[i + 1][j] + SCORE_EPSILON:
                    f[i + 1][j] = candidate
                    choice[i + 1][j] = (i, j, None)

            if j < m:
                candidate = current - SKIP_PENALTY
                if candidate > f[i][j + 1] + SCORE_EPSILON:
                    f[i][j + 1] = candidate
                    choice[i][j + 1] = (i, j, None)

            for z_size, e_size in transitions:
                ni = i + z_size
                nj = j + e_size
                if ni > n or nj > m:
                    continue
                score = span_score(i, ni, j, nj)
                if score is None:
                    continue
                candidate = current + score
                if candidate > f[ni][nj] + SCORE_EPSILON:
                    f[ni][nj] = candidate
                    choice[ni][nj] = (i, j, (z_size, e_size, score))

    path = []
    i, j = n, m
    while i > 0 or j > 0:
        previous = choice[i][j]
        if previous is None:  # pragma: no cover - defensive only
            break
        pi, pj, transition = previous
        if transition is None:
            path.append(_PathRow(pi, i, pj, j, -SKIP_PENALTY))
        else:
            path.append(_PathRow(pi, i, pj, j, transition[2]))
        i, j = pi, pj
    path.reverse()
    return path


def _path_score(path):
    return sum(row.score for row in path)


def _path_rows(source_subs, translation_subs, path):
    rows = []
    for item in path:
        rows.append(
            _make_row(
                source_subs[item.z0:item.z1],
                translation_subs[item.e0:item.e1],
            )
        )
    return rows


def _make_row(source_group, translation_group):
    all_subs = list(source_group) + list(translation_group)
    if all_subs:
        start = min(sub.start for sub in all_subs)
        end = max(sub.end for sub in all_subs)
    else:  # pragma: no cover - defensive only
        start = end = timedelta(0)

    if source_group and translation_group:
        status = ROW_ALIGNED
    elif source_group:
        status = ROW_SOURCE_ONLY
    else:
        status = ROW_TRANSLATION_ONLY

    return AlignmentRow(
        start=start,
        end=end,
        source=_content(source_group),
        translation=_content(translation_group),
        status=status,
        source_subs=source_group,
        translation_subs=translation_group,
    )


def _enumerate_coverings(
    source_subs, translation_subs, max_gap_ms, max_group, limit=50000
):
    """Enumerate layouts that pair every item without dropping any.

    Dropping an item changes which content the span represents, so a layout
    with skips is not a competing pairing of the same span.  ``None`` means
    enumeration was cut short by ``limit``.
    """
    n = len(source_subs)
    m = len(translation_subs)
    transitions = list(_allowed_transitions(max_group))
    results = []
    seen = set()
    counter = [0]

    def recurse(i, j, rows):
        counter[0] += 1
        if counter[0] > limit:
            return False
        if i == n and j == m:
            key = tuple(sorted(row.key() for row in rows))
            if key not in seen:
                seen.add(key)
                results.append(list(rows))
            return True

        for z_size, e_size in transitions:
            ni = i + z_size
            nj = j + e_size
            if ni > n or nj > m:
                continue
            score = _match_score(
                source_subs[i:ni], translation_subs[j:nj], max_gap_ms
            )
            if score is None:
                continue
            if recurse(ni, nj, rows + [_PathRow(i, ni, j, nj, score)]) is False:
                return False
        return True

    if recurse(0, 0, []) is False:
        return None
    return results


def _find_candidate_windows(
    source_subs,
    translation_subs,
    path,
    max_gap_ms,
    max_group,
    tolerance,
    max_window_rows=2,
    max_items=8,
):
    """Find small all-matched spans with two equally plausible layouts.

    Every non-overlapping ambiguous span is reported, so independent
    ambiguities elsewhere in the segment still reach the editor.  Spans that
    contain an unmatched row are skipped: a stray subtitle with no
    counterpart cannot make its neighbours ambiguous.
    """
    if len(path) < 2:
        return []

    edges = [(0, 0)]
    for item in path:
        edges.append((item.z1, item.e1))

    def is_aligned(item):
        return item.z1 > item.z0 and item.e1 > item.e0

    def candidates_for(start, end):
        z0, e0 = edges[start]
        z1, e1 = edges[end]
        if z1 - z0 > max_items or e1 - e0 > max_items:
            return None
        if z1 - z0 < 2 or e1 - e0 < 2:
            return None

        tilings = _enumerate_coverings(
            source_subs[z0:z1], translation_subs[e0:e1],
            max_gap_ms, max_group,
        )
        if not tilings or len(tilings) < 2:
            return None

        chosen_key = tuple(sorted(item.key() for item in path[start:end]))
        chosen_score = _path_score(path[start:end])

        alternatives = []
        for tiling in tilings:
            translated = [
                _PathRow(
                    z0 + item.z0,
                    z0 + item.z1,
                    e0 + item.e0,
                    e0 + item.e1,
                    item.score,
                )
                for item in tiling
            ]
            key = tuple(sorted(item.key() for item in translated))
            if key == chosen_key:
                continue
            score = _path_score(tiling)
            if score + tolerance + SCORE_EPSILON >= chosen_score:
                alternatives.append((translated, score))
        if not alternatives:
            return None

        best = max(score for _, score in alternatives)
        if best + tolerance + SCORE_EPSILON < chosen_score:
            return None

        alternatives.sort(key=lambda pair: pair[1], reverse=True)
        return (
            start,
            end,
            list(path[start:end]),
            alternatives[:5],
            chosen_score,
        )

    windows = []
    start = 0
    while start < len(path) - 1:
        # Prefer the smallest ambiguous span at this position.
        found = None
        for width in range(2, max_window_rows + 1):
            end = start + width
            if end > len(path):
                break
            if not all(is_aligned(path[idx]) for idx in range(start, end)):
                continue
            found = candidates_for(start, end)
            if found is not None:
                break

        if found is None:
            start += 1
        else:
            windows.append(found)
            start = found[1]

    return windows


def _merge_windows(windows):
    """Merge candidate windows that overlap in path-row coordinates."""
    if not windows:
        return []
    windows = sorted(windows, key=lambda item: (item[0], item[1]))
    merged = [windows[0]]
    for window in windows[1:]:
        last = merged[-1]
        if window[0] < last[1]:
            existing = set(
                tuple(sorted(item.key() for item in alternative))
                for alternative, _ in last[3]
            )
            alternatives = list(last[3])
            for alternative in window[3]:
                key = tuple(sorted(item.key() for item in alternative[0]))
                if key not in existing:
                    alternatives.append(alternative)
                    existing.add(key)
            merged[-1] = (
                last[0],
                max(last[1], window[1]),
                last[2],
                alternatives,
                last[4],
            )
        else:
            merged.append(window)
    return merged


def _build_candidates(source_subs, translation_subs, path, windows):
    """Create Candidate objects and note which path rows they cover."""
    candidates = []
    candidate_ranges = []
    for start, end, chosen_path, alternatives, chosen_score in windows:
        chosen_rows = _path_rows(source_subs, translation_subs, chosen_path)
        alternative_rows = [
            (_path_rows(source_subs, translation_subs, alternative), score)
            for alternative, score in alternatives
        ]
        all_rows = list(chosen_rows)
        for rows, _ in alternative_rows:
            all_rows.extend(rows)
        start_time = min(row.start for row in all_rows)
        end_time = max(row.end for row in all_rows)

        candidates.append(
            Candidate(
                start=start_time,
                end=end_time,
                chosen=chosen_rows,
                alternatives=alternative_rows,
                chosen_score=chosen_score,
            )
        )
        candidate_ranges.append((start, end))

    return candidates, candidate_ranges


def _resolve_reference(reference, subtitles, label):
    """Resolve an anchor reference to a zero-based position."""
    if isinstance(reference, str):
        reference = _parse_reference(reference)

    if isinstance(reference, int):
        kind = "index"
        value = reference
    elif isinstance(reference, tuple) and len(reference) == 2:
        kind, value = reference
    else:
        raise AlignmentError(
            "Invalid {0} anchor reference: {1!r}".format(label, reference)
        )

    if kind == "position":
        position = int(value)
        if position < 1 or position > len(subtitles):
            raise AlignmentError(
                "{0} position {1} is outside the subtitle range (1..{2})"
                .format(label, position, len(subtitles))
            )
        return position - 1

    if kind == "index":
        wanted = int(value)
        matches = [
            idx
            for idx, sub in enumerate(subtitles)
            if sub.index is not None and sub.index == wanted
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AlignmentError(
                "{0} subtitle index {1} is not unique; use a @position "
                "anchor instead".format(label, wanted)
            )
        # Editors often number rows from one even when the SRT indexes are
        # absent or non-contiguous, so fall back to that interpretation.
        if 1 <= wanted <= len(subtitles):
            return wanted - 1
        raise AlignmentError(
            "{0} subtitle index {1} was not found".format(label, wanted)
        )

    if kind == "time":
        timestamp = value
        exact = [
            idx
            for idx, sub in enumerate(subtitles)
            if sub.start == timestamp
        ]
        if len(exact) == 1:
            return exact[0]
        containing = [
            idx
            for idx, sub in enumerate(subtitles)
            if sub.start <= timestamp <= sub.end
        ]
        if len(containing) == 1:
            return containing[0]
        if containing:
            return min(
                containing,
                key=lambda idx: abs(subtitles[idx].start - timestamp),
            )
        if not subtitles:
            raise AlignmentError(
                "{0} timestamp {1} does not match any subtitle".format(
                    label, srt.timedelta_to_srt_timestamp(timestamp)
                )
            )
        return min(
            range(len(subtitles)),
            key=lambda idx: abs(subtitles[idx].start - timestamp),
        )

    raise AlignmentError("Unknown anchor reference kind: {0}".format(kind))


def _parse_timestamp(text):
    match = TS_RE.match(text)
    if not match:
        return None
    hours, minutes, seconds, msecs = match.groups()
    msecs = (msecs or "0")[:3].ljust(3, "0")
    return timedelta(
        hours=int(hours),
        minutes=int(minutes),
        seconds=int(seconds),
        milliseconds=int(msecs),
    )


def _parse_reference(text):
    """Parse one anchor reference string.

    ``#12`` and a bare ``12`` are SRT indexes; ``@12`` is a one-based
    subtitle position.  ``index:``, ``position:`` and ``time:`` prefixes are
    accepted too, as are bare timestamps.
    """
    text = text.strip()
    if not text:
        raise AlignmentError("Empty anchor reference")

    lowered = text.lower()
    for prefix, kind in (
        ("index:", "index"),
        ("position:", "position"),
        ("time:", "time"),
    ):
        if lowered.startswith(prefix):
            value = text[len(prefix):].strip()
            if kind == "time":
                parsed = _parse_timestamp(value)
                if parsed is None:
                    raise AlignmentError(
                        "Invalid anchor timestamp: {0}".format(text)
                    )
                return ("time", parsed)
            try:
                return (kind, int(value))
            except ValueError:
                raise AlignmentError(
                    "Invalid anchor reference: {0}".format(text)
                )

    if text.startswith("@"):
        try:
            return ("position", int(text[1:]))
        except ValueError:
            pass
    if text.startswith("#"):
        try:
            return ("index", int(text[1:]))
        except ValueError:
            pass

    timestamp = _parse_timestamp(text)
    if timestamp is not None:
        return ("time", timestamp)

    try:
        return ("index", int(text))
    except ValueError:
        raise AlignmentError("Invalid anchor reference: {0}".format(text))


parse_reference = _parse_reference


def _split_anchor_pair(line):
    """Split one line/spec into two anchor reference strings."""
    line = line.strip()
    if not line:
        raise AlignmentError("Empty anchor pair")

    for delimiter in ("\t", "|", ";", "->"):
        if delimiter in line:
            parts = [part.strip() for part in line.split(delimiter, 1)]
            if len(parts) == 2 and all(parts):
                return parts[0], parts[1]

    if "," in line:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2:
            return parts[0], parts[1]

    parts = line.split()
    if len(parts) == 2:
        return parts[0], parts[1]

    # "1:2" is a convenient index shorthand, but only when both sides are
    # plain numbers (timestamps have more than one colon).
    if line.count(":") == 1:
        left, right = [part.strip() for part in line.split(":", 1)]
        try:
            int(left)
            int(right)
        except ValueError:
            pass
        else:
            return left, right

    raise AlignmentError(
        "Could not parse anchor pair {0!r}; expected 'SOURCE TARGET'".format(
            line
        )
    )


def parse_anchor(spec):
    """Parse one anchor specification into two reference tuples.

    Examples: ``"12 14"``, ``"12,14"``, ``"12:14"``, ``"#12 #14"``,
    ``"@12 @14"`` and ``"00:00:12,000<TAB>00:00:13,500"``.
    """
    left, right = _split_anchor_pair(spec)
    return _parse_reference(left), _parse_reference(right)


def load_anchors(path, encoding="utf-8-sig"):
    """Load anchor pairs from a text file.

    Blank lines and lines starting with ``#`` or ``;`` are ignored.  A header
    row (for example ``source<TAB>translation``) is skipped.
    """
    anchors = []
    with codecs.open(path, "r", encoding=encoding) as anchor_file:
        for line_number, line in enumerate(anchor_file, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(";"):
                continue
            try:
                anchors.append(parse_anchor(stripped))
            except AlignmentError:
                lowered = stripped.lower()
                if line_number == 1 and any(
                    word in lowered
                    for word in (
                        "source",
                        "target",
                        "translation",
                        "chinese",
                        "english",
                    )
                ):
                    continue
                raise
    return anchors


def _is_reference(value):
    """Return True if ``value`` is a single reference, not a pair."""
    if not isinstance(value, tuple) or len(value) != 2:
        return False
    return value[0] in ("index", "position", "time") and not isinstance(
        value[1], tuple
    )


def _normalise_anchors(anchors, source_subs, translation_subs):
    """Resolve anchors to positions, validate, and sort by source position."""
    resolved = []
    for anchor in anchors:
        if isinstance(anchor, str):
            source_ref, translation_ref = parse_anchor(anchor)
        elif _is_reference(anchor):
            raise AlignmentError(
                "Each anchor must contain a source and a translation "
                "reference: {0!r}".format(anchor)
            )
        else:
            try:
                if len(anchor) != 2:
                    raise ValueError("not a pair")
                source_ref, translation_ref = anchor
            except (TypeError, ValueError):
                raise AlignmentError(
                    "Each anchor must be a (source, translation) pair: "
                    "{0!r}".format(anchor)
                )
        source_pos = _resolve_reference(source_ref, source_subs, "Source")
        translation_pos = _resolve_reference(
            translation_ref, translation_subs, "Translation"
        )
        resolved.append((source_pos, translation_pos))

    resolved.sort()
    seen_source = set()
    seen_translation = set()
    last_source = -1
    last_translation = -1
    for source_pos, translation_pos in resolved:
        if source_pos in seen_source:
            raise AlignmentError(
                "Source subtitle is used by more than one anchor: {0}"
                .format(source_subs[source_pos].index)
            )
        if translation_pos in seen_translation:
            raise AlignmentError(
                "Translation subtitle is used by more than one anchor: {0}"
                .format(translation_subs[translation_pos].index)
            )
        if source_pos <= last_source or translation_pos <= last_translation:
            raise AlignmentError(
                "Anchors must be strictly increasing on both sides; check "
                "the manually confirmed pairs"
            )
        seen_source.add(source_pos)
        seen_translation.add(translation_pos)
        last_source = source_pos
        last_translation = translation_pos

    return resolved


def _align_segment(
    source_subs,
    translation_subs,
    max_gap_ms,
    max_group,
    ambiguity_tolerance,
    collect_candidates=True,
):
    if not source_subs and not translation_subs:
        return [], []

    if not source_subs:
        return [_make_row([], [sub]) for sub in translation_subs], []
    if not translation_subs:
        return [_make_row([sub], []) for sub in source_subs], []

    path = _dynamic_program(
        source_subs, translation_subs, max_gap_ms, max_group
    )

    windows = []
    if collect_candidates:
        windows = _find_candidate_windows(
            source_subs,
            translation_subs,
            path,
            max_gap_ms,
            max_group,
            ambiguity_tolerance,
        )

    windows = _merge_windows(windows)
    candidates, candidate_ranges = _build_candidates(
        source_subs, translation_subs, path, windows
    )

    rows = _path_rows(source_subs, translation_subs, path)

    for candidate, (start, end) in zip(candidates, candidate_ranges):
        options = tuple([candidate.chosen] + list(candidate.alternatives))
        for idx in range(start, min(end, len(rows))):
            rows[idx].status = ROW_CANDIDATE
            rows[idx].candidates = options

    return rows, candidates


def align_subtitles(
    source_subs,
    translation_subs,
    anchors=(),
    max_gap_ms=DEFAULT_MAX_GAP_MS,
    max_group=DEFAULT_MAX_GROUP,
    ambiguity_tolerance=DEFAULT_AMBIGUITY_TOLERANCE,
):
    """Align two iterables of :class:`srt.Subtitle` objects.

    :param source_subs: Subtitles in the source language (usually Chinese).
    :param translation_subs: Subtitles in the translation language.
    :param anchors: Iterable of ``(source_ref, translation_ref)`` pairs, or
                    strings accepted by :func:`parse_anchor`.  References are
                    ``("index", n)``, ``("position", n)`` or
                    ``("time", timedelta)``.
    :param max_gap_ms: Maximum gap between non-overlapping subtitles that may
                       still be considered a match.
    :param max_group: Largest number of subtitles on one side that may be
                      grouped with a single subtitle on the other side.
    :param ambiguity_tolerance: Score difference within which two local
                                alignments are considered equally plausible.
    :returns: :class:`Alignment`.
    """
    source_subs = sorted(source_subs)
    translation_subs = sorted(translation_subs)

    resolved = _normalise_anchors(anchors, source_subs, translation_subs)

    rows = []
    candidates = []
    source_cursor = 0
    translation_cursor = 0
    anchor_objects = []

    for source_pos, translation_pos in resolved:
        segment_rows, segment_candidates = _align_segment(
            source_subs[source_cursor:source_pos],
            translation_subs[translation_cursor:translation_pos],
            max_gap_ms,
            max_group,
            ambiguity_tolerance,
        )
        rows.extend(segment_rows)
        candidates.extend(segment_candidates)

        anchor_source = source_subs[source_pos]
        anchor_translation = translation_subs[translation_pos]
        rows.append(_make_row([anchor_source], [anchor_translation]))
        anchor_objects.append((anchor_source, anchor_translation))

        source_cursor = source_pos + 1
        translation_cursor = translation_pos + 1

    segment_rows, segment_candidates = _align_segment(
        source_subs[source_cursor:],
        translation_subs[translation_cursor:],
        max_gap_ms,
        max_group,
        ambiguity_tolerance,
    )
    rows.extend(segment_rows)
    candidates.extend(segment_candidates)

    # Rows stay in the order produced by the monotonic paths, which is the
    # order an editor reads them in.  Anchors can have slightly different
    # timestamps on each side, so sorting by time would scramble them.
    return Alignment(rows, candidates, anchor_objects)


def align_srt(
    source_srt, translation_srt, anchors=(), ignore_errors=False, **kwargs
):
    """Convenience wrapper for :func:`align_subtitles` accepting SRT text."""
    return align_subtitles(
        srt.parse(source_srt, ignore_errors=ignore_errors),
        srt.parse(translation_srt, ignore_errors=ignore_errors),
        anchors=anchors,
        **kwargs
    )


def align_files(
    source_path, translation_path, anchors=(), encoding="utf-8-sig", **kwargs
):
    """Align two SRT files on disk."""
    with codecs.open(source_path, "r", encoding=encoding) as source_file:
        source_text = source_file.read()
    with codecs.open(
        translation_path, "r", encoding=encoding
    ) as translation_file:
        translation_text = translation_file.read()
    return align_srt(
        source_text, translation_text, anchors=anchors, **kwargs
    )


align = align_subtitles


def render_table(
    rows,
    format="tsv",
    header=True,
    source_header="source",
    translation_header="translation",
):
    """Render alignment rows as TSV, CSV, JSON, Markdown or HTML."""
    format = (format or "tsv").lower()
    if format in ("tsv", "tab"):
        return _render_delimited(
            rows, "\t", header, source_header, translation_header
        )
    if format == "csv":
        return _render_delimited(
            rows, ",", header, source_header, translation_header
        )
    if format == "json":
        return json.dumps(
            [row.to_dict() for row in rows], ensure_ascii=False, indent=2
        ) + "\n"
    if format in ("markdown", "md"):
        return _render_markdown(
            rows, header, source_header, translation_header
        )
    if format == "html":
        return _render_html(rows, header, source_header, translation_header)
    raise AlignmentError(
        "Unsupported output format {0!r}; choose tsv, csv, json, markdown "
        "or html".format(format)
    )


format_table = render_table


def _candidate_text(row):
    if not row.candidates:
        return ""
    return json.dumps(
        [option.to_dict() for option in row.candidates], ensure_ascii=False
    )


def _render_delimited(
    rows, delimiter, header, source_header, translation_header
):
    output = io.StringIO()
    writer = csv.writer(
        output,
        delimiter=delimiter,
        lineterminator=os.linesep,
        quoting=csv.QUOTE_MINIMAL,
    )
    if header:
        writer.writerow(
            [
                "index",
                "start",
                "end",
                source_header,
                translation_header,
                "status",
                "candidates",
            ]
        )
    for row in rows:
        writer.writerow(
            [
                row.index if row.index is not None else "",
                srt.timedelta_to_srt_timestamp(row.start),
                srt.timedelta_to_srt_timestamp(row.end),
                row.source,
                row.translation,
                row.status,
                _candidate_text(row),
            ]
        )
    return output.getvalue()


def _markdown_escape(text):
    return text.replace("|", "\\|").replace("\n", "<br>")


def _render_markdown(rows, header, source_header, translation_header):
    lines = []
    if header:
        lines.append(
            "| # | Start | End | {0} | {1} | Status |".format(
                source_header, translation_header
            )
        )
        lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                row.index if row.index is not None else "",
                srt.timedelta_to_srt_timestamp(row.start),
                srt.timedelta_to_srt_timestamp(row.end),
                _markdown_escape(row.source),
                _markdown_escape(row.translation),
                row.status,
            )
        )
    return "\n".join(lines) + "\n"


def _html_escape(text):
    try:
        from html import escape
    except ImportError:  # pragma: no cover - Python 2
        from cgi import escape
    return escape(text, quote=True)


def _render_html(rows, header, source_header, translation_header):
    lines = ["<table>"]
    if header:
        lines.append(
            "<thead><tr><th>#</th><th>Start</th><th>End</th>"
            "<th>{0}</th><th>{1}</th><th>Status</th></tr></thead>".format(
                _html_escape(source_header),
                _html_escape(translation_header),
            )
        )
    lines.append("<tbody>")
    for row in rows:
        lines.append(
            '<tr class="{0}"><td>{1}</td><td>{2}</td><td>{3}</td>'
            "<td>{4}</td><td>{5}</td><td>{6}</td></tr>".format(
                _html_escape(row.status),
                row.index if row.index is not None else "",
                srt.timedelta_to_srt_timestamp(row.start),
                srt.timedelta_to_srt_timestamp(row.end),
                _html_escape(row.source).replace("\n", "<br>"),
                _html_escape(row.translation).replace("\n", "<br>"),
                _html_escape(row.status),
            )
        )
    lines.append("</tbody></table>")
    return "\n".join(lines) + "\n"
