#!/usr/bin/env python
# coding=utf8

"""Split a subtitle corpus into training and verification sets.

A corpus is split by whole interviews.  Segments which are known to belong to
the same interview, or which are confirmed copies of the same recording, are
indivisible and always land in the same set.  A short excerpt is never dropped
just to make the duration ratio come out neatly.

Manifest format (JSON; all times may be milliseconds or SRT timestamps)::

    {
      "ratio": [4, 1],
      "tolerance_ms": 60000,
      "budgets_ms": {"train": 28800000, "verification": 7200000},
      "group_by_title": true,
      "segments": [
        {
          "id": "ep1",
          "interview": "nightline-2024-01-01",
          "source": "broadcast",
          "title": "Nightline",
          "start": "00:00:10,000",
          "end": "00:08:00,000",
          "speaking_ms": 450000
        },
        {
          "id": "ep2",
          "interview": "nightline-2024-01-01",
          "source": "rebroadcast",
          "title": "Nightline",
          "duration_ms": 120000
        }
      ],
      "source_correspondences": [["broadcast", "rebroadcast"]],
      "must_link": [["ep1", "ep2"]],
      "independent": [["other-a", "other-b"]],
      "locks": [
        {"segments": ["ep1"], "set": "train"},
        {"interview": "nightline-2024-01-01", "set": "verification"}
      ]
    }

Semantics
---------

``segments``
    The subtitle segments.  ``id`` is required and unique.  ``interview`` and
    ``source`` are identity keys: all segments sharing one are one interview.
    ``title`` is only a display name unless ``group_by_title`` is true.
    ``speaking_ms`` (or ``duration_ms``) is the known speaking time.  When it
    is absent, ``end - start`` is used.  A segment may instead point at an SRT
    file with ``"path": "..."``; its subtitle spans supply the times.

``source_correspondences``
    Pairs of source (or segment/interview) ids confirmed to be the same
    underlying recording.  These links are transitive.

``must_link``
    Additional pairs which must stay together.

``independent``
    Pairs of same-named programs which are confirmed to be distinct.  This
    prevents title-based grouping from joining them.  Confirmed source
    correspondences still win, because they are explicit evidence.

``locks``
    Human decisions pinning a segment, source, interview, or group to
    ``train`` or ``verification``.  A lock may also be written inline on a
    segment with a ``set`` field.

If transitive source links make two different human locks land in the same
indivisible group, no split is emitted.  Instead ``conflict_chains`` contains
the shortest evidence chain between the two locks, so the contradiction can be
fixed rather than hidden by deleting short subtitles.
"""

from __future__ import division

import argparse
import collections
import io
import json
import os
import sys

try:
    from collections.abc import Mapping
except ImportError:  # Python 2
    from collections import Mapping

try:
    string_types = basestring  # noqa: F821  (Python 2)
except NameError:
    string_types = str


TRAIN = "train"
VERIFICATION = "verification"
SET_NAMES = (TRAIN, VERIFICATION)
SET_ALIASES = {
    "train": TRAIN,
    "training": TRAIN,
    "fit": TRAIN,
    "verification": VERIFICATION,
    "verify": VERIFICATION,
    "validation": VERIFICATION,
    "dev": VERIFICATION,
    "check": VERIFICATION,
    "audit": VERIFICATION,
    "heldout": VERIFICATION,
    "held-out": VERIFICATION,
}

SEGMENT = "segment"
SOURCE = "source"
INTERVIEW = "interview"
TITLE = "title"

MISSING = object()


class SplitError(ValueError):
    """Raised when the manifest is malformed or cannot be interpreted."""


# ---------------------------------------------------------------------------
# Time and value parsing
# ---------------------------------------------------------------------------


def parse_time_ms(value, field="time", owner="<manifest>", units="ms"):
    """Parse milliseconds, seconds as a number, or an SRT-style timestamp.

    ``units`` applies to bare numbers: ``"ms"`` (default) or ``"s"``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise SplitError("%s for %r is not a timestamp" % (field, owner))
    if isinstance(value, (int, float)):
        if value < 0:
            raise SplitError("%s for %r must not be negative" % (field, owner))
        if units == "s":
            return int(round(value * 1000))
        return int(round(value))
    if not isinstance(value, string_types):
        raise SplitError("%s for %r is not a timestamp" % (field, owner))

    text = value.strip()
    if not text:
        raise SplitError("%s for %r is empty" % (field, owner))
    if text.startswith("-"):
        raise SplitError("%s for %r must not be negative" % (field, owner))
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        if number < 0:
            raise SplitError("%s for %r must not be negative" % (field, owner))
        if units == "s":
            return int(round(number * 1000))
        return int(round(number))

    # SRT timestamps use a comma, but some manifests use a dot or full-width
    # punctuation.  Accept all of them.
    normalised = (
        text.replace("，", ",")
        .replace("．", ".")
        .replace("。", ".")
        .replace("：", ":")
        .replace(".", ",")
    )
    parts = normalised.split(":")
    if len(parts) == 3:
        hours, minutes, rest = parts
    elif len(parts) == 2:
        hours, minutes, rest = "0", parts[0], parts[1]
    elif len(parts) == 1:
        hours, minutes, rest = "0", "0", parts[0]
    else:
        raise SplitError("%s for %r is not a timestamp" % (field, owner))

    if "," in rest:
        seconds, fraction = rest.split(",", 1)
    else:
        seconds, fraction = rest, ""
    try:
        hours_i = int(hours or 0)
        minutes_i = int(minutes or 0)
        seconds_i = int(seconds or 0)
        fraction_digits = (fraction + "000")[:3]
        millis_i = int(fraction_digits) if fraction_digits else 0
    except ValueError:
        raise SplitError("%s for %r is not a timestamp" % (field, owner))
    if minutes_i >= 60 or seconds_i >= 60:
        # SRT can technically contain these, but they are almost certainly
        # data errors.  Keep accepting them as base-60 like srt does not, to
        # avoid silently changing a budget.
        raise SplitError("%s for %r has an impossible timestamp" % (field, owner))
    return ((hours_i * 60 + minutes_i) * 60 + seconds_i) * 1000 + millis_i


def _normalise_set(value, field="set"):
    if value is None or value is MISSING:
        return None
    if not isinstance(value, string_types):
        raise SplitError("%s must be a string" % field)
    key = value.strip().lower().replace("_", "-")
    if key not in SET_ALIASES:
        raise SplitError(
            "unknown %s %r (expected train or verification)" % (field, value)
        )
    return SET_ALIASES[key]


def _normalise_key(value):
    if value is None:
        return None
    if not isinstance(value, string_types):
        raise SplitError("identity keys must be strings, got %r" % (value,))
    value = value.strip()
    return value or None


def _as_list(value, field):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, Mapping):
        # Mapping shorthand: each key/value pair is a relation.  This is
        # useful for {"source_a": "source_b"} correspondences.
        return [[key, item] for key, item in value.items()]
    if isinstance(value, string_types):
        return [value]
    raise SplitError("%s must be a list" % field)


def _get_first(mapping, names, default=MISSING):
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _segments_from_interviews(raw_interviews):
    """Flatten interview objects which carry nested ``segments`` lists."""
    if raw_interviews is None:
        return MISSING
    if isinstance(raw_interviews, Mapping):
        items = []
        for interview_id, interview_data in raw_interviews.items():
            if isinstance(interview_data, Mapping):
                item = dict(interview_data)
            else:
                item = {"segments": interview_data}
            item.setdefault("id", interview_id)
            items.append(item)
    else:
        items = list(raw_interviews)
    flattened = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        members = raw.get("segments", raw.get("clips", raw.get("items")))
        if not isinstance(members, (list, tuple)):
            continue
        for member in members:
            if isinstance(member, string_types):
                member_data = {"id": member}
            elif isinstance(member, Mapping):
                member_data = dict(member)
            else:
                continue
            member_data.setdefault("interview", raw.get("id", raw.get("interview")))
            if "source" not in member_data and raw.get("source"):
                member_data["source"] = raw["source"]
            if "title" not in member_data and raw.get("title"):
                member_data["title"] = raw["title"]
            flattened.append(member_data)
    return flattened or MISSING


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------


class Segment(object):
    def __init__(self, segment_id, order):
        self.id = segment_id
        self.order = order
        self.source = None
        self.interview = None
        self.title = None
        self.start_ms = None
        self.end_ms = None
        self.speaking_ms = None
        self.known = False
        self.path = None

    @property
    def duration_ms(self):
        if self.known:
            return int(self.speaking_ms or 0)
        return 0

    def __repr__(self):
        return "Segment(%r, duration_ms=%r)" % (self.id, self.duration_ms)


class Lock(object):
    def __init__(self, segments, target_set, label, index):
        self.segments = tuple(segments)
        self.set = target_set
        self.label = label
        self.index = index

    def as_dict(self):
        return {
            "target": self.label,
            "segments": list(self.segments),
            "set": self.set,
        }

    def __repr__(self):
        return "Lock(%r, %r)" % (self.label, self.set)


class Edge(object):
    def __init__(self, left, right, reason, kind):
        self.left = left
        self.right = right
        self.reason = reason
        self.kind = kind


class Manifest(object):
    def __init__(self):
        self.segments = collections.OrderedDict()
        self.sources = collections.OrderedDict()
        self.interviews = collections.OrderedDict()
        self.must_link = []
        self.source_links = []
        self.independent = []
        self.locks = []
        self.ratio = (1.0, 1.0)
        self.tolerance_ms = 0
        self.budgets = {}
        self.group_by_title = False
        self.warnings = []

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, (list, tuple)):
            # A bare list of segments is a valid shorthand for a 1:1 split.
            data = {"segments": list(data)}
        if not isinstance(data, Mapping):
            raise SplitError("manifest must be a JSON object")

        # Also accept a wrapper object such as {"manifest": {...}} or
        # {"corpus": {...}}.
        for key in ("manifest", "corpus", "dataset", "data"):
            inner = data.get(key)
            if isinstance(inner, Mapping) and any(
                name in inner
                for name in ("segments", "atoms", "items", "clips")
            ):
                merged = dict(inner)
                for extra_key, extra_value in data.items():
                    if extra_key not in merged and extra_key != key:
                        merged[extra_key] = extra_value
                data = merged
                break

        manifest = cls()
        manifest._load_metadata(data)
        manifest._load_sources(data.get("sources"))
        manifest._load_interviews(data.get("interviews"))
        raw_segments = _get_first(
            data, ("segments", "atoms", "items", "clips"), MISSING
        )
        if raw_segments is MISSING:
            # Interview-centric input: each interview lists its own segments.
            raw_segments = _segments_from_interviews(data.get("interviews"))
        if raw_segments is MISSING:
            raise SplitError("manifest needs a non-empty segments list")
        raw_segments = _as_list(raw_segments, "segments")
        if not raw_segments:
            raise SplitError("manifest needs a non-empty segments list")
        for order, raw in enumerate(raw_segments):
            manifest._load_segment(raw, order, data)
        manifest._load_links(data)
        manifest._load_locks(data)
        manifest._validate()
        return manifest

    @classmethod
    def from_json_file(cls, path):
        with io.open(path, "r", encoding="utf-8-sig") as handle:
            return cls.from_dict(json.load(handle))

    def _load_metadata(self, data):
        ratio = _get_first(data, ("ratio", "target_ratio"), (1, 1))
        if isinstance(ratio, Mapping):
            ratio = (ratio.get(TRAIN, ratio.get("training", 1)),
                     ratio.get(VERIFICATION, ratio.get("verify", 1)))
        if not isinstance(ratio, (list, tuple)) or len(ratio) != 2:
            raise SplitError("ratio must be a pair like [4, 1]")
        try:
            ratio_values = [float(value) for value in ratio]
        except (TypeError, ValueError):
            raise SplitError("ratio must contain two numbers")
        if any(value < 0 for value in ratio_values) or sum(ratio_values) <= 0:
            raise SplitError("ratio must be non-negative and not both zero")
        self.ratio = tuple(ratio_values)

        self.units = self._parse_units(data.get("units", data.get("time_units")))
        tolerance = _get_first(
            data, ("tolerance_ms", "tolerance", "ratio_tolerance_ms"), 0
        )
        if "tolerance_ms" in data:
            tolerance_units = "ms"
        else:
            tolerance_units = self.units
        self.tolerance_ms = parse_time_ms(
            tolerance, "tolerance_ms", "<manifest>", tolerance_units
        )

        budgets = _get_first(
            data, ("budgets_ms", "budgets", "budget_ms", "budget"), None
        )
        if budgets is not None:
            budget_units = "ms" if "budgets_ms" in data or "budget_ms" in data else self.units
            self.budgets = self._parse_budgets(budgets, budget_units)

        group_by_title = data.get("group_by_title", data.get("group_same_title"))
        if group_by_title is None:
            # Same-titled segments are assumed to be one programme unless the
            # manifest explicitly marks two of them independent.  A manifest
            # which never wants title inference can pass group_by_title=false.
            group_by_title = True
        self.group_by_title = bool(group_by_title)

    @staticmethod
    def _parse_units(value):
        if value is None:
            return "ms"
        if not isinstance(value, string_types):
            raise SplitError("units must be 'ms' or 's'")
        key = value.strip().lower()
        if key in ("ms", "millisecond", "milliseconds"):
            return "ms"
        if key in ("s", "sec", "secs", "second", "seconds"):
            return "s"
        raise SplitError("unknown units %r (expected ms or s)" % value)

    @staticmethod
    def _parse_budgets(budgets, units="ms"):
        if isinstance(budgets, (list, tuple)):
            if len(budgets) != 2:
                raise SplitError("budgets must have train and verification values")
            budgets = {TRAIN: budgets[0], VERIFICATION: budgets[1]}
        if not isinstance(budgets, Mapping):
            raise SplitError("budgets must be an object keyed by set")
        parsed = {}
        for name, value in budgets.items():
            target = _normalise_set(name, "budget key")
            parsed[target] = parse_time_ms(
                value, "budget for %s" % name, "<manifest>", units
            )
        return parsed

    def _load_sources(self, raw_sources):
        if raw_sources is None:
            return
        if isinstance(raw_sources, Mapping):
            items = []
            for source_id, source_data in raw_sources.items():
                if isinstance(source_data, Mapping):
                    item = dict(source_data)
                elif isinstance(source_data, (list, tuple)):
                    item = {"segments": source_data}
                else:
                    item = {"title": source_data}
                item["id"] = source_id
                items.append(item)
        else:
            items = _as_list(raw_sources, "sources")
        for raw in items:
            if not isinstance(raw, Mapping):
                raise SplitError("each source must be an object")
            source_id = _normalise_key(_get_first(raw, ("id", "source", "name")))
            if not source_id:
                raise SplitError("every source needs an id")
            if source_id in self.sources:
                raise SplitError("duplicate source id %r" % source_id)
            self.sources[source_id] = {
                "segments": [
                    _normalise_key(value)
                    if isinstance(value, string_types)
                    else _normalise_key(value.get("id"))
                    for value in _as_list(raw.get("segments"), "source segments")
                ],
                "title": _normalise_key(raw.get("title")),
                "locked_set": _normalise_set(
                    _get_first(raw, ("set", "split", "lock")), "source set"
                ),
            }

    def _load_interviews(self, raw_interviews):
        if raw_interviews is None:
            return
        if isinstance(raw_interviews, Mapping):
            items = []
            for interview_id, interview_data in raw_interviews.items():
                if isinstance(interview_data, Mapping):
                    item = dict(interview_data)
                elif isinstance(interview_data, (list, tuple)):
                    item = {"segments": interview_data}
                else:
                    item = {"title": interview_data}
                item["id"] = interview_id
                items.append(item)
        else:
            items = _as_list(raw_interviews, "interviews")
        for raw in items:
            if not isinstance(raw, Mapping):
                raise SplitError("each interview must be an object")
            interview_id = _normalise_key(_get_first(raw, ("id", "interview", "name")))
            if not interview_id:
                raise SplitError("every interview needs an id")
            if interview_id in self.interviews:
                raise SplitError("duplicate interview id %r" % interview_id)
            self.interviews[interview_id] = {
                "segments": [
                    _normalise_key(value)
                    if isinstance(value, string_types)
                    else _normalise_key(value.get("id"))
                    for value in _as_list(raw.get("segments"), "interview segments")
                ],
                "title": _normalise_key(raw.get("title")),
                "locked_set": _normalise_set(
                    _get_first(raw, ("set", "split", "lock")), "interview set"
                ),
            }

    def _load_segment(self, raw, order, manifest_data):
        if not isinstance(raw, Mapping):
            raise SplitError("segment %d must be an object" % order)
        segment_id = _normalise_key(
            _get_first(raw, ("id", "segment", "name", "file"))
        )
        if not segment_id:
            raise SplitError("segment %d needs a non-empty id" % order)
        if segment_id in self.segments:
            raise SplitError("duplicate segment id %r" % segment_id)
        segment = Segment(segment_id, order)
        segment.source = _normalise_key(
            _get_first(raw, ("source", "source_id", "recording"), None)
        )
        segment.interview = _normalise_key(
            _get_first(raw, ("interview", "interview_id", "episode", "group"), None)
        )
        segment.title = _normalise_key(
            _get_first(raw, ("title", "program", "show", "programme"), None)
        )
        segment.path = _normalise_key(_get_first(raw, ("path", "srt"), None))

        start = _get_first(raw, ("start_ms", "start"), None)
        end = _get_first(raw, ("end_ms", "end"), None)
        start_units = "ms" if "start_ms" in raw else self.units
        end_units = "ms" if "end_ms" in raw else self.units
        segment.start_ms = parse_time_ms(start, "start", segment_id, start_units)
        segment.end_ms = parse_time_ms(end, "end", segment_id, end_units)
        if (
            segment.start_ms is not None
            and segment.end_ms is not None
            and segment.end_ms < segment.start_ms
        ):
            raise SplitError("segment %r ends before it starts" % segment_id)

        speaking = _get_first(
            raw,
            (
                "speaking_ms",
                "known_ms",
                "talk_ms",
                "duration_ms",
                "duration",
                "usable_ms",
                "speaking_s",
            ),
            MISSING,
        )
        if speaking is MISSING:
            speaking_units = self.units
        elif "speaking_ms" in raw or "known_ms" in raw or "talk_ms" in raw or (
            "duration_ms" in raw or "usable_ms" in raw
        ):
            speaking_units = "ms"
        else:
            speaking_units = self.units
        segment_budget = _get_first(
            raw,
            ("budget_ms", "segment_budget_ms", "max_duration_ms", "budget_s"),
            MISSING,
        )
        budget_units = (
            "ms"
            if "budget_ms" in raw or "segment_budget_ms" in raw or "max_duration_ms" in raw
            else self.units
        )
        span_ms = None
        if segment.start_ms is not None and segment.end_ms is not None:
            span_ms = segment.end_ms - segment.start_ms
        if speaking is not MISSING:
            speaking_ms = parse_time_ms(
                speaking, "speaking_ms", segment_id, speaking_units
            )
            if segment_budget is not MISSING:
                speaking_ms = min(
                    speaking_ms,
                    parse_time_ms(
                        segment_budget, "budget_ms", segment_id, budget_units
                    ),
                )
            segment.speaking_ms = speaking_ms
            segment.known = True
        elif segment_budget is not MISSING:
            # A per-segment budget can stand in for speaking time when the
            # manifest has no explicit speaking duration.  It caps the span,
            # so a long clip cannot claim more budget than it has.
            budget_ms = parse_time_ms(
                segment_budget, "budget_ms", segment_id, budget_units
            )
            segment.speaking_ms = (
                min(span_ms, budget_ms) if span_ms is not None else budget_ms
            )
            segment.known = True
        elif span_ms is not None:
            segment.speaking_ms = span_ms
            segment.known = True
        elif segment.path:
            duration, start_ms, end_ms = _duration_from_srt(segment.path)
            segment.speaking_ms = duration
            segment.known = True
            if segment.start_ms is None:
                segment.start_ms = start_ms
            if segment.end_ms is None:
                segment.end_ms = end_ms
        else:
            segment.known = False
            segment.speaking_ms = None

        locked = _normalise_set(
            _get_first(raw, ("set", "split", "lock")), "segment set"
        )
        if locked:
            self.locks.append(
                Lock([segment_id], locked, "segment %s" % segment_id, len(self.locks))
            )

        if segment.source:
            self.sources.setdefault(
                segment.source,
                {"segments": [], "title": segment.title, "locked_set": None},
            )
            if segment_id not in self.sources[segment.source]["segments"]:
                self.sources[segment.source]["segments"].append(segment_id)
        if segment.interview:
            self.interviews.setdefault(
                segment.interview,
                {"segments": [], "title": segment.title, "locked_set": None},
            )
            if segment_id not in self.interviews[segment.interview]["segments"]:
                self.interviews[segment.interview]["segments"].append(segment_id)
        self.segments[segment_id] = segment

    def _resolve_identity(self, value, field):
        """Resolve an id to a segment/source/interview node."""
        if not isinstance(value, string_types) or not value:
            raise SplitError("%s must be a non-empty id" % field)
        if value in self.segments:
            return (SEGMENT, value)
        if value in self.sources:
            return (SOURCE, value)
        if value in self.interviews:
            return (INTERVIEW, value)
        for title, members in self._title_groups().items():
            if value == title:
                return (TITLE, title)
        raise SplitError("unknown id %r in %s" % (value, field))

    def _title_groups(self):
        groups = collections.OrderedDict()
        for segment in self.segments.values():
            if segment.title:
                groups.setdefault(segment.title, []).append(segment.id)
        return groups

    def _load_links(self, data):
        raw_links = _get_first(
            data,
            ("must_link", "must_group", "same_group", "links", "groups"),
            [],
        )
        for index, raw in enumerate(_as_list(raw_links, "must_link")):
            members = self._parse_group(raw, "must_link[%d]" % index)
            # A group of N ids is equivalent to N-1 pairwise links.
            for member in members[1:]:
                self.must_link.append(
                    (
                        self._resolve_identity(members[0], "must_link"),
                        self._resolve_identity(member, "must_link"),
                    )
                )

        raw_sources = _get_first(
            data,
            (
                "source_correspondences",
                "source_correspondence",
                "correspondences",
                "confirmed_sources",
                "source_links",
                "syndication",
            ),
            [],
        )
        for index, raw in enumerate(_as_list(raw_sources, "source_correspondences")):
            left, right = self._parse_pair(
                raw, "source_correspondences[%d]" % index
            )
            self.source_links.append(
                (
                    self._resolve_identity(left, "source_correspondences"),
                    self._resolve_identity(right, "source_correspondences"),
                )
            )

        raw_independent = _get_first(
            data, ("independent", "distinct", "independent_programs"), []
        )
        for index, raw in enumerate(_as_list(raw_independent, "independent")):
            left, right = self._parse_pair(raw, "independent[%d]" % index)
            # Resolve now so a typo is not silently ignored.  Store the
            # expanded segment pairs, since title grouping works on segments.
            left_node = self._resolve_identity(left, "independent")
            right_node = self._resolve_identity(right, "independent")
            left_segments = set(self._node_segments(left_node))
            right_segments = set(self._node_segments(right_node))
            for left_id in left_segments:
                for right_id in right_segments:
                    if left_id != right_id:
                        self.independent.append((left_id, right_id))

    @staticmethod
    def _parse_group(raw, field):
        if isinstance(raw, Mapping):
            members = _get_first(raw, ("ids", "segments", "group", "members"))
            if members is MISSING:
                raise SplitError("%s must be a list of ids" % field)
            members = _as_list(members, field)
        elif isinstance(raw, (list, tuple)):
            members = list(raw)
        elif isinstance(raw, string_types):
            # A single id is accepted as a one-member group by callers who
            # use groups uniformly; it imposes no constraint.
            return [raw]
        else:
            raise SplitError("%s must be a list of ids" % field)
        if len(members) < 2:
            raise SplitError("%s needs at least two ids" % field)
        for member in members:
            if not isinstance(member, string_types):
                raise SplitError("%s must contain string ids" % field)
        return members

    @staticmethod
    def _parse_pair(raw, field):
        if isinstance(raw, Mapping):
            left = _get_first(raw, ("a", "left", "from", "source"))
            right = _get_first(raw, ("b", "right", "to", "target"))
        elif isinstance(raw, (list, tuple)):
            if len(raw) != 2:
                raise SplitError("%s must contain exactly two ids" % field)
            left, right = raw
        else:
            raise SplitError("%s must be a pair of ids" % field)
        if not isinstance(left, string_types) or not isinstance(right, string_types):
            raise SplitError("%s must contain two string ids" % field)
        return left, right

    def _load_locks(self, data):
        raw_locks = _get_first(data, ("locks", "locked", "locked_groups"), [])
        for index, raw in enumerate(_as_list(raw_locks, "locks")):
            lock = self._parse_lock(raw, index)
            if lock is not None:
                self.locks.append(lock)
        # Source/interview-level set fields are locks too.
        for source_id, source in self.sources.items():
            if source["locked_set"]:
                self.locks.append(
                    Lock(
                        self._source_segments(source_id),
                        source["locked_set"],
                        "source %s" % source_id,
                        len(self.locks),
                    )
                )
        for interview_id, interview in self.interviews.items():
            if interview["locked_set"]:
                self.locks.append(
                    Lock(
                        self._interview_segments(interview_id),
                        interview["locked_set"],
                        "interview %s" % interview_id,
                        len(self.locks),
                    )
                )

    def _parse_lock(self, raw, index):
        if isinstance(raw, (list, tuple)):
            # [target, set], [id1, id2, ..., set], or [[ids...], set].
            if len(raw) < 2 or not isinstance(raw[-1], string_types):
                raise SplitError(
                    "locks[%d] must be an object, [target, set], or "
                    "[id, ..., set]" % index
                )
            target_set = _normalise_set(raw[-1], "locks[%d] set" % index)
            if target_set is None:
                raise SplitError("locks[%d] needs a valid set" % index)
            targets = raw[:-1]
            segment_ids = []
            for target in targets:
                segment_ids.extend(self._lock_segments(target, "locks[%d]" % index))
            if not segment_ids:
                raise SplitError("locks[%d] has an empty group" % index)
            label = (
                str(targets[0])
                if len(targets) == 1
                else "group {%s}" % ", ".join(str(target) for target in targets)
            )
            return Lock(segment_ids, target_set, label, index)
        if isinstance(raw, Mapping):
            # A mapping from set name to group is also accepted:
            # {"train": ["a", "b"], "verification": "c"}.
            set_keys = [
                key
                for key in raw
                if isinstance(key, string_types)
                and key.strip().lower().replace("_", "-") in SET_ALIASES
            ]
            if set_keys:
                if len(set_keys) != 1:
                    raise SplitError(
                        "locks[%d] must name exactly one set" % index
                    )
                target_set = _normalise_set(set_keys[0], "locks[%d] set" % index)
                members = _as_list(raw[set_keys[0]], "locks[%d] segments" % index)
                if not members:
                    raise SplitError("locks[%d] has an empty group" % index)
                segment_ids = []
                for member in members:
                    segment_ids.extend(
                        self._lock_segments(member, "locks[%d]" % index)
                    )
                label = "group {%s}" % ", ".join(str(m) for m in members)
                return Lock(segment_ids, target_set, label, index)
        if not isinstance(raw, Mapping):
            raise SplitError("locks[%d] must be an object" % index)
        target_set = _normalise_set(
            _get_first(raw, ("set", "split", "locked_set", "target_set"), None),
            "locks[%d] set" % index,
        )
        if target_set is None:
            raise SplitError("locks[%d] needs a set" % index)
        members = _get_first(
            raw,
            ("segments", "atoms", "group", "ids", "members", "items"),
            MISSING,
        )
        if members is not MISSING:
            members = _as_list(members, "locks[%d] segments" % index)
            if not members:
                raise SplitError("locks[%d] has an empty group" % index)
            segment_ids = []
            for member in members:
                segment_ids.extend(
                    self._lock_segments(member, "locks[%d]" % index)
                )
            label = _get_first(raw, ("id", "label", "name"), None)
            if not label:
                if len(members) == 1:
                    label = "segment %s" % members[0]
                else:
                    label = "group {%s}" % ", ".join(str(m) for m in members)
            return Lock(segment_ids, target_set, str(label), index)
        target = _get_first(
            raw, ("segment", "source", "interview", "target", "id"), MISSING
        )
        if target is MISSING:
            raise SplitError("locks[%d] needs a segment, source, interview or group" % index)
        return Lock(
            self._lock_segments(target, "locks[%d]" % index),
            target_set,
            str(target),
            index,
        )

    def _lock_segments(self, target, field):
        if isinstance(target, (list, tuple)):
            segment_ids = []
            for item in target:
                segment_ids.extend(self._lock_segments(item, field))
            return segment_ids
        node = self._resolve_identity(target, field)
        return self._node_segments(node)

    def _node_segments(self, node):
        kind, value = node
        if kind == SEGMENT:
            return [value]
        if kind == SOURCE:
            return self._source_segments(value)
        if kind == INTERVIEW:
            return self._interview_segments(value)
        if kind == TITLE:
            return list(self._title_groups().get(value, []))
        raise SplitError("unknown node type %r" % (kind,))

    def _source_segments(self, source_id):
        source = self.sources.get(source_id)
        if not source:
            return []
        result = []
        for segment_id in source["segments"]:
            if segment_id in self.segments and segment_id not in result:
                result.append(segment_id)
        for segment in self.segments.values():
            if segment.source == source_id and segment.id not in result:
                result.append(segment.id)
        return result

    def _interview_segments(self, interview_id):
        interview = self.interviews.get(interview_id)
        if not interview:
            return []
        result = []
        for segment_id in interview["segments"]:
            if segment_id in self.segments and segment_id not in result:
                result.append(segment_id)
        for segment in self.segments.values():
            if segment.interview == interview_id and segment.id not in result:
                result.append(segment.id)
        return result

    def _validate(self):
        independent_pairs = {frozenset(pair) for pair in self.independent}
        for source_id, source in self.sources.items():
            for segment_id in source["segments"]:
                if segment_id not in self.segments:
                    raise SplitError(
                        "source %r references unknown segment %r"
                        % (source_id, segment_id)
                    )
        for interview_id, interview in self.interviews.items():
            for segment_id in interview["segments"]:
                if segment_id not in self.segments:
                    raise SplitError(
                        "interview %r references unknown segment %r"
                        % (interview_id, segment_id)
                    )
        for lock in self.locks:
            if not lock.segments:
                raise SplitError("lock %r has no segments" % lock.label)
            for segment_id in lock.segments:
                if segment_id not in self.segments:
                    raise SplitError(
                        "lock %r references unknown segment %r"
                        % (lock.label, segment_id)
                    )
        # Explicit independence must agree with the identity keys.  Two
        # "independent" segments cannot share a confirmed source or interview;
        # that is a data contradiction, not something to paper over.
        for left_id, right_id in self.independent:
            left = self.segments[left_id]
            right = self.segments[right_id]
            if left.interview and left.interview == right.interview:
                raise SplitError(
                    "independent pair %r/%r shares interview %r"
                    % (left_id, right_id, left.interview)
                )
            if left.source and left.source == right.source:
                raise SplitError(
                    "independent pair %r/%r shares source %r"
                    % (left_id, right_id, left.source)
                )
        for left_node, right_node in self.must_link:
            left_segments = set(self._node_segments(left_node))
            right_segments = set(self._node_segments(right_node))
            for left_id in left_segments:
                for right_id in right_segments:
                    if frozenset((left_id, right_id)) in independent_pairs:
                        raise SplitError(
                            "independent pair %r/%r is also must-linked"
                            % (left_id, right_id)
                        )
        # Same-named programmes which were declared independent are excluded
        # from title merging (see build_graph); note it so the report explains
        # why those segments were not joined.
        if self.group_by_title:
            declared = set()
            for pair in independent_pairs:
                declared.update(pair)
            for title, segment_ids in self._title_groups().items():
                if any(segment_id in declared for segment_id in segment_ids) and len(
                    segment_ids
                ) > len(declared.intersection(segment_ids)):
                    self.warnings.append(
                        "title %r contains independently declared programmes; "
                        "they are excluded from title grouping" % title
                    )

    def lock_map(self):
        return {lock.index: lock for lock in self.locks}


# ---------------------------------------------------------------------------
# SRT duration helper
# ---------------------------------------------------------------------------


def _duration_from_srt(path):
    try:
        import srt
    except ImportError:  # pragma: no cover - package is normally importable
        raise SplitError("reading %r needs the srt module" % path)
    if not os.path.isfile(path):
        raise SplitError("SRT file not found: %s" % path)
    with io.open(path, "r", encoding="utf-8-sig") as handle:
        subtitles = list(srt.parse(handle.read()))
    if not subtitles:
        return 0, None, None
    duration = sum(
        max(0, int((sub.end - sub.start).total_seconds() * 1000))
        for sub in subtitles
    )
    start_ms = int(subtitles[0].start.total_seconds() * 1000)
    end_ms = int(subtitles[-1].end.total_seconds() * 1000)
    return duration, start_ms, end_ms


# ---------------------------------------------------------------------------
# Constraint graph and components
# ---------------------------------------------------------------------------


def _node_label(node):
    kind, value = node
    if kind == SEGMENT:
        return value
    return "%s %s" % (kind, value)


class ConstraintGraph(object):
    def __init__(self):
        self.nodes = collections.OrderedDict()
        self.adjacency = collections.defaultdict(list)
        self.edges = []
        self._edge_keys = set()

    def add_node(self, node):
        self.nodes.setdefault(node, len(self.nodes))

    def add_edge(self, left, right, reason, kind="constraint"):
        if left == right:
            return
        self.add_node(left)
        self.add_node(right)
        key = tuple(sorted((left, right))) + (reason,)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        edge = Edge(left, right, reason, kind)
        self.edges.append(edge)
        self.adjacency[left].append((right, edge))
        self.adjacency[right].append((left, edge))

    def shortest_path(self, start, goal, allowed=None):
        if start == goal:
            return []
        queue = collections.deque([start])
        previous = {start: None}
        while queue:
            current = queue.popleft()
            for neighbour, edge in self.adjacency.get(current, ()):
                if neighbour in previous:
                    continue
                if allowed is not None and neighbour not in allowed:
                    continue
                previous[neighbour] = (current, edge)
                if neighbour == goal:
                    path = []
                    cursor = goal
                    while previous[cursor] is not None:
                        prev, path_edge = previous[cursor]
                        path.append((prev, cursor, path_edge))
                        cursor = prev
                    path.reverse()
                    return path
                queue.append(neighbour)
        return None


def _edge_reason_for_group(kind, group_id, segment_id):
    if kind == SOURCE:
        return "%s is part of source %s" % (segment_id, group_id)
    if kind == INTERVIEW:
        return "%s is part of interview %s" % (segment_id, group_id)
    if kind == TITLE:
        return "%s shares the title %r" % (segment_id, group_id)
    return "%s is linked to %s" % (segment_id, group_id)


def build_graph(manifest):
    graph = ConstraintGraph()
    segment_nodes = {}
    for segment in manifest.segments.values():
        node = (SEGMENT, segment.id)
        segment_nodes[segment.id] = node
        graph.add_node(node)

    for source_id, source in manifest.sources.items():
        source_node = (SOURCE, source_id)
        graph.add_node(source_node)
        segment_ids = manifest._source_segments(source_id)
        for segment_id in segment_ids:
            graph.add_edge(
                segment_nodes[segment_id],
                source_node,
                _edge_reason_for_group(SOURCE, source_id, segment_id),
                "source",
            )

    for interview_id, interview in manifest.interviews.items():
        interview_node = (INTERVIEW, interview_id)
        graph.add_node(interview_node)
        segment_ids = manifest._interview_segments(interview_id)
        for segment_id in segment_ids:
            graph.add_edge(
                segment_nodes[segment_id],
                interview_node,
                _edge_reason_for_group(INTERVIEW, interview_id, segment_id),
                "interview",
            )

    for left, right in manifest.source_links:
        left_node = _promote_link_node(manifest, left)
        right_node = _promote_link_node(manifest, right)
        graph.add_edge(
            left_node,
            right_node,
            "confirmed source correspondence between %s and %s"
            % (_node_label(left_node), _node_label(right_node)),
            "source_correspondence",
        )

    for left, right in manifest.must_link:
        left_node = _promote_link_node(manifest, left)
        right_node = _promote_link_node(manifest, right)
        graph.add_edge(
            left_node,
            right_node,
            "explicit must-link between %s and %s"
            % (_node_label(left_node), _node_label(right_node)),
            "must_link",
        )

    # A manual lock which names several segments also declares that those
    # segments form one indivisible group.  Treating them as must-linked lets
    # the conflict report explain a collision with a source link, rather than
    # failing with an opaque "lock spans groups" message.
    for lock in manifest.locks:
        anchor = (SEGMENT, lock.segments[0])
        for segment_id in lock.segments[1:]:
            graph.add_edge(
                anchor,
                (SEGMENT, segment_id),
                "manual lock %r groups %s with %s"
                % (lock.label, lock.segments[0], segment_id),
                "lock_group",
            )

    if manifest.group_by_title:
        independent = {frozenset(pair) for pair in manifest.independent}
        # A segment named in an independence declaration must not be joined
        # by title at all: even if it is not independent from a third
        # same-titled segment, that third segment would transitively join the
        # declared-independent pair.
        declared = set()
        for pair in independent:
            declared.update(pair)
        for title, segment_ids in manifest._title_groups().items():
            members = [segment_id for segment_id in segment_ids
                       if segment_id not in declared]
            if len(members) < 2:
                continue
            title_node = (TITLE, title)
            graph.add_node(title_node)
            for segment_id in members:
                graph.add_edge(
                    segment_nodes[segment_id],
                    title_node,
                    _edge_reason_for_group(TITLE, title, segment_id),
                    "title",
                )
    return graph


def _promote_link_node(manifest, node):
    """Links between groups are represented at the group node itself."""
    kind, value = node
    if kind == SEGMENT:
        return (SEGMENT, value)
    if kind == SOURCE:
        return (SOURCE, value)
    if kind == INTERVIEW:
        return (INTERVIEW, value)
    if kind == TITLE:
        return (TITLE, value)
    return node


class Component(object):
    def __init__(self, segment_ids, duration_ms, order):
        self.segment_ids = tuple(segment_ids)
        self.duration_ms = int(duration_ms)
        self.order = order

    @property
    def key(self):
        return self.segment_ids

    def __repr__(self):
        return "Component(%r, duration_ms=%r)" % (
            self.segment_ids,
            self.duration_ms,
        )


def build_components(manifest, graph):
    parent = {node: node for node in graph.nodes}

    def find(node):
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for edge in graph.edges:
        union(edge.left, edge.right)

    grouped = collections.OrderedDict()
    for segment in manifest.segments.values():
        node = (SEGMENT, segment.id)
        grouped.setdefault(find(node), []).append(segment)

    components = []
    for order, segments in enumerate(grouped.values()):
        segment_ids = tuple(segment.id for segment in segments)
        duration = sum(segment.duration_ms for segment in segments)
        components.append(Component(segment_ids, duration, order))
    components.sort(key=lambda component: component.order)
    return components


def component_for_segments(components, segment_ids):
    wanted = set(segment_ids)
    for component in components:
        if wanted <= set(component.segment_ids):
            return component
    return None


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def _lock_chain(lock, component, start=None):
    """Serialise one lock declaration for a conflict report."""
    return {
        "target": lock.label,
        "set": lock.set,
        "segments": list(lock.segments),
        "anchor": start or lock.segments[0],
        "component": list(component.segment_ids),
    }


def find_conflicts(manifest, graph, components):
    conflicts = []
    segment_to_component = {}
    for component in components:
        for segment_id in component.segment_ids:
            segment_to_component[segment_id] = component

    lock_components = collections.OrderedDict()
    for lock in manifest.locks:
        roots = set()
        for segment_id in lock.segments:
            roots.add(segment_to_component[segment_id].key)
        if len(roots) != 1:
            raise SplitError(
                "lock %r spans independent groups; add must_link between them"
                % lock.label
            )
        component = segment_to_component[lock.segments[0]]
        lock_components.setdefault(component.key, []).append(lock)

    for component in components:
        locks = lock_components.get(component.key, [])
        if not locks:
            continue
        train_locks = [lock for lock in locks if lock.set == TRAIN]
        verification_locks = [lock for lock in locks if lock.set == VERIFICATION]
        if not train_locks or not verification_locks:
            continue

        best = None
        for train_lock in train_locks:
            for verification_lock in verification_locks:
                for start in train_lock.segments:
                    for goal in verification_lock.segments:
                        path = graph.shortest_path(
                            (SEGMENT, start),
                            (SEGMENT, goal),
                            allowed=_component_nodes(graph, component),
                        )
                        if path is None:
                            continue
                        candidate = (len(path), start, goal, train_lock,
                                     verification_lock, path)
                        if best is None or candidate[:3] < best[:3]:
                            best = candidate
        if best is None:
            # The two locks are in the same connected component but there is
            # no segment-to-segment path, which should be impossible.
            raise SplitError(
                "internal error: cannot explain conflicting locks in %r"
                % (component.segment_ids,)
            )

        _, start, goal, train_lock, verification_lock, path = best
        steps = []
        for left, right, edge in path:
            steps.append(
                {
                    "from": _display_node(left, start, goal, train_lock,
                                           verification_lock),
                    "to": _display_node(right, start, goal, train_lock,
                                         verification_lock),
                    "relation": edge.reason,
                    "kind": edge.kind,
                }
            )
        path_segments = []
        for _, _, edge in path:
            for node in (edge.left, edge.right):
                if node[0] == SEGMENT and node[1] not in path_segments:
                    path_segments.append(node[1])
        chain = {
            "atoms": path_segments,
            "path": [_display_node(node, start, goal, train_lock,
                                    verification_lock)
                     for node in _path_nodes(path, (SEGMENT, start), (SEGMENT, goal))],
            "steps": steps,
            "locks": [
                _lock_chain(train_lock, component, start),
                _lock_chain(verification_lock, component, goal),
            ],
            "component": list(component.segment_ids),
            "message": _format_chain_message(
                path, start, goal, train_lock, verification_lock
            ),
        }
        conflicts.append(chain)

    # Deduplicate chains which differ only because several segments carry the
    # same two locks.
    unique = []
    seen = set()
    for chain in conflicts:
        key = (
            frozenset(chain["component"]),
            tuple(sorted((chain["locks"][0]["target"], chain["locks"][0]["set"],
                          chain["locks"][1]["target"], chain["locks"][1]["set"]))),
        )
        if key not in seen:
            seen.add(key)
            unique.append(chain)
    return unique


def _component_nodes(graph, component):
    members = set(component.segment_ids)
    allowed = set()
    for node in graph.nodes:
        if node[0] == SEGMENT and node[1] in members:
            allowed.add(node)
    # Include source/interview/title hubs which touch these segments.  The
    # BFS will naturally stay inside the connected component.
    for node in graph.nodes:
        if node[0] == SEGMENT:
            continue
        for segment_id in _hub_segments(graph, node):
            if segment_id in members:
                allowed.add(node)
                break
    return allowed


def _hub_segments(graph, node):
    result = set()
    for neighbour, _ in graph.adjacency.get(node, ()):
        if neighbour[0] == SEGMENT:
            result.add(neighbour[1])
    return result


def _path_nodes(path, start, goal):
    nodes = [start]
    for _, right, _ in path:
        nodes.append(right)
    return nodes


def _display_node(node, start, goal, train_lock, verification_lock):
    if node == (SEGMENT, start):
        return "%s [locked %s]" % (start, train_lock.set)
    if node == (SEGMENT, goal):
        return "%s [locked %s]" % (goal, verification_lock.set)
    return _node_label(node)


def _format_chain_message(path, start, goal, train_lock, verification_lock):
    if not path:
        # Both locks name the same segment directly.
        return (
            "%s [locked %s by %r] is also covered by the group lock %r "
            "for %s"
            % (
                start,
                train_lock.set,
                train_lock.label,
                verification_lock.label,
                verification_lock.set,
            )
        )
    pieces = ["%s [locked %s]" % (start, train_lock.set)]
    for _, right, edge in path:
        pieces.append(
            "--(%s)--> %s"
            % (
                edge.reason,
                "%s [locked %s]" % (goal, verification_lock.set)
                if right == (SEGMENT, goal)
                else _node_label(right),
            )
        )
    return " ".join(pieces)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def _gcd(left, right):
    while right:
        left, right = right, left % right
    return abs(left)


def _gcd_many(values):
    result = 0
    for value in values:
        result = _gcd(result, value)
    return result


def _ceil_div(value, divisor):
    if value <= 0:
        return 0
    return -((-value) // divisor)


def _choose_subset(components, fixed_train, total, target_train, budgets):
    """Choose components for train, closest to ``target_train``.

    Returns ``(chosen, budget_feasible)``.  ``chosen`` is a list of
    components.  Budgets are upper bounds on the known speaking duration of
    each set.

    Small corpora are solved exactly with a gcd-compressed subset-sum DP.
    Larger corpora use a deterministic local search over the same objective.
    No component (and therefore no short subtitle) is ever discarded:
    infeasible inputs return an assignment plus ``budget_feasible=False``.
    """
    budget_train = budgets.get(TRAIN)
    budget_verification = budgets.get(VERIFICATION)
    low = 0
    high = total
    if budget_verification is not None:
        low = max(low, total - budget_verification)
    if budget_train is not None:
        high = min(high, budget_train)

    nonzero = [component for component in components if component.duration_ms > 0]
    if not nonzero:
        return [], low <= fixed_train <= high

    # Bounds on the sum contributed by free (unlocked) train components.
    low_free = low - fixed_train
    high_free = high - fixed_train
    target_free = target_train - fixed_train

    gross_gcd = _gcd_many([component.duration_ms for component in nonzero])
    if gross_gcd <= 0:
        gross_gcd = 1
    high_index = max(0, high_free // gross_gcd)
    if len(nonzero) * (high_index + 1) <= 5000000:
        return _exact_subset(
            nonzero, gross_gcd, target_free, low_free, high_free
        )
    return _local_search_subset(nonzero, target_free, low_free, high_free)


def _score(total, target, low, high, penalty):
    if total < low:
        return penalty + (low - total) + abs(total - target) * 0.001
    if total > high:
        return penalty + (total - high) + abs(total - target) * 0.001
    return abs(total - target)


def _exact_subset(nonzero, scale, target_free, low_free, high_free):
    """Exact subset sum closest to ``target_free`` within ``[low_free, high_free]``."""
    desired = target_free
    low_index = max(0, _ceil_div(low_free, scale))
    high_index = max(0, high_free // scale)
    total_index = sum(component.duration_ms // scale for component in nonzero)
    if high_index > total_index:
        high_index = total_index

    ordered = sorted(
        nonzero, key=lambda component: (-component.duration_ms, component.segment_ids)
    )
    parents = {0: None}
    frontier = [0]
    for index, component in enumerate(ordered):
        step = component.duration_ms // scale
        additions = []
        for current in tuple(frontier):
            new_sum = current + step
            if new_sum > high_index:
                continue
            if new_sum not in parents:
                parents[new_sum] = (current, index)
                additions.append(new_sum)
        frontier.extend(additions)
        if len(parents) > 1500000:
            return _local_search_subset(
                nonzero, desired, low_free, high_free
            )

    feasible = [
        value for value in parents if low_index <= value <= high_index
    ]
    if high_free < 0:
        # The locked train duration alone already busts the budget, so no
        # amount of free components can fit.
        feasible = []
    budget_feasible = bool(feasible)
    if not feasible:
        # Rank infeasible candidates by total budget overage first, then by
        # distance to the requested ratio.  A split is still emitted.
        def rank(value):
            actual = value * scale
            overage = max(0, low_free - actual) + max(0, actual - high_free)
            return (overage, abs(desired - actual), -value)

        best_sum = min(parents, key=rank)
    else:
        best_sum = min(
            feasible, key=lambda value: (abs(desired - value * scale), -value)
        )

    chosen = []
    cursor = best_sum
    while parents.get(cursor) is not None:
        previous, index = parents[cursor]
        chosen.append(ordered[index])
        cursor = previous
    return chosen, budget_feasible


def _local_search_subset(components, target_free, low_free, high_free):
    """Deterministic 1-opt/2-opt search for larger corpora.

    Moves never exclude a component from the corpus; they only decide which
    side it belongs to.  Budget violations are strongly penalised but still
    produce a complete assignment.
    """
    penalty = max(1, abs(low_free) + abs(high_free) + abs(target_free)) * 10
    ordered = sorted(
        components, key=lambda component: (-component.duration_ms, component.segment_ids)
    )
    selected = set()
    current = 0

    # Greedy insertion: add the component which most improves the score.
    candidates = list(ordered)
    while candidates:
        best_gain = 0.0
        best_item = None
        best_total = current
        for item in candidates:
            new_total = current + item.duration_ms
            gain = _score(current, target_free, low_free, high_free, penalty) - _score(
                new_total, target_free, low_free, high_free, penalty
            )
            if gain > best_gain:
                best_gain = gain
                best_item = item
                best_total = new_total
        if best_item is None:
            break
        selected.add(best_item.segment_ids)
        current = best_total
        candidates.remove(best_item)

    # 1-opt and 2-opt hill climbing.
    improved = True
    while improved:
        improved = False
        best_gain = 0.0
        best_move = None

        for item in ordered:
            if item.segment_ids in selected:
                new_total = current - item.duration_ms
                move = ("remove", item, None, new_total)
            else:
                new_total = current + item.duration_ms
                move = ("add", item, None, new_total)
            gain = _score(current, target_free, low_free, high_free, penalty) - _score(
                new_total, target_free, low_free, high_free, penalty
            )
            if gain > best_gain:
                best_gain = gain
                best_move = move

        for outgoing in ordered:
            if outgoing.segment_ids not in selected:
                continue
            for incoming in ordered:
                if incoming.segment_ids in selected:
                    continue
                new_total = current - outgoing.duration_ms + incoming.duration_ms
                gain = _score(
                    current, target_free, low_free, high_free, penalty
                ) - _score(new_total, target_free, low_free, high_free, penalty)
                if gain > best_gain:
                    best_gain = gain
                    best_move = ("swap", outgoing, incoming, new_total)

        if best_move is not None:
            kind, first, second, new_total = best_move
            if kind == "remove":
                selected.discard(first.segment_ids)
            elif kind == "add":
                selected.add(first.segment_ids)
            else:
                selected.discard(first.segment_ids)
                selected.add(second.segment_ids)
            current = new_total
            improved = True

    chosen = [component for component in ordered if component.segment_ids in selected]
    feasible = low_free <= current <= high_free
    return chosen, feasible


def assign(manifest):
    graph = build_graph(manifest)
    components = build_components(manifest, graph)
    conflicts = find_conflicts(manifest, graph, components)

    if conflicts:
        return SplitResult(
            manifest=manifest,
            components=components,
            assignment={},
            durations=collections.OrderedDict((name, 0) for name in SET_NAMES),
            target_durations=collections.OrderedDict((name, 0) for name in SET_NAMES),
            conflict_chains=conflicts,
            warnings=list(manifest.warnings)
            + ["conflicting locks; no split was produced"],
            budget_feasible=False,
            within_tolerance=False,
            unassigned=[component.segment_ids for component in components],
        )

    locked_components = {}
    for lock in manifest.locks:
        component = component_for_segments(components, lock.segments)
        if component is None:
            # find_conflicts already raises for this, but keep a defensive
            # guard for direct callers.
            raise SplitError("lock %r spans independent groups" % lock.label)
        existing = locked_components.get(component.key)
        if existing is not None and existing != lock.set:
            # find_conflicts should have caught this; preserve a clear error
            # rather than guessing.
            raise SplitError(
                "conflicting locks on %r: %s and %s"
                % (component.key, existing, lock.set)
            )
        locked_components[component.key] = lock.set

    fixed_train = sum(
        component.duration_ms
        for component in components
        if locked_components.get(component.key) == TRAIN
    )
    total_duration = sum(component.duration_ms for component in components)
    ratio_total = manifest.ratio[0] + manifest.ratio[1]
    target_train = total_duration * manifest.ratio[0] / ratio_total
    target_verification = total_duration - target_train

    free = [
        component
        for component in components
        if component.key not in locked_components
    ]
    free_nonzero = [c for c in free if c.duration_ms > 0]
    free_zero = [c for c in free if c.duration_ms == 0]
    chosen, _ = _choose_subset(
        free_nonzero,
        fixed_train,
        total_duration,
        target_train,
        manifest.budgets,
    )
    chosen_keys = {component.key for component in chosen}
    zero_keys = {component.key for component in free_zero}

    assignment = {}
    for component in components:
        if component.key in zero_keys:
            # Unknown-duration groups are placed by count once the duration
            # split is fixed; leave them out of the duration totals.
            continue
        locked = locked_components.get(component.key)
        if locked:
            assignment[component.key] = locked
        elif component.key in chosen_keys:
            assignment[component.key] = TRAIN
        else:
            assignment[component.key] = VERIFICATION

    train_count = sum(
        1
        for component in components
        if assignment.get(component.key) == TRAIN
    )
    verification_count = sum(
        1
        for component in components
        if assignment.get(component.key) == VERIFICATION
    )
    for component in free_zero:
        total_count = train_count + verification_count + 1
        # Place unknown-duration groups by the requested ratio, but never
        # override the known-duration balance which was chosen above.
        if train_count + 1 <= total_count * (manifest.ratio[0] / ratio_total):
            assignment[component.key] = TRAIN
            train_count += 1
        else:
            assignment[component.key] = VERIFICATION
            verification_count += 1

    durations = collections.OrderedDict((name, 0) for name in SET_NAMES)
    for component in components:
        durations[assignment[component.key]] += component.duration_ms

    actual_train = durations[TRAIN]
    deviation = abs(actual_train - target_train)
    within_tolerance = deviation <= manifest.tolerance_ms
    budget_feasible = True
    for name in SET_NAMES:
        budget = manifest.budgets.get(name)
        if budget is not None and durations[name] > budget:
            budget_feasible = False
    warnings = list(manifest.warnings)
    if not within_tolerance:
        warnings.append(
            "closest achievable split deviates %d ms from the target "
            "(tolerance is %d ms); indivisible groups prevent a closer match"
            % (deviation, manifest.tolerance_ms)
        )
    if not budget_feasible:
        warnings.append(
            "duration budgets cannot be met without breaking locks or "
            "dropping segments; closest assignment used"
        )
    for name in SET_NAMES:
        budget = manifest.budgets.get(name)
        if budget is not None and durations[name] > budget:
            warnings.append(
                "%s exceeds its %d ms budget by %d ms"
                % (name, budget, durations[name] - budget)
            )
    if total_duration == 0:
        warnings.append(
            "no known speaking durations; sets were balanced by segment count"
        )

    return SplitResult(
        manifest=manifest,
        components=components,
        assignment=assignment,
        durations=durations,
        target_durations=collections.OrderedDict(
            [(TRAIN, target_train), (VERIFICATION, target_verification)]
        ),
        conflict_chains=[],
        warnings=warnings,
        budget_feasible=budget_feasible,
        within_tolerance=within_tolerance,
        unassigned=[],
    )


class SplitResult(object):
    def __init__(
        self,
        manifest,
        components,
        assignment,
        durations,
        target_durations,
        conflict_chains,
        warnings,
        budget_feasible,
        within_tolerance,
        unassigned,
    ):
        self.manifest = manifest
        self.components = components
        self.assignment = assignment
        self.durations = durations
        self.target_durations = target_durations
        self.conflict_chains = conflict_chains
        self.warnings = warnings
        self.budget_feasible = budget_feasible
        self.within_tolerance = within_tolerance
        self.unassigned = unassigned

    @property
    def ok(self):
        return not self.conflict_chains and not self.unassigned

    @property
    def train(self):
        return self.sets[TRAIN]

    @property
    def verification(self):
        return self.sets[VERIFICATION]

    @property
    def groups(self):
        return collections.OrderedDict(
            (name, list(self.sets[name])) for name in SET_NAMES
        )

    @property
    def conflict_chain(self):
        """The first conflict chain, or None when the split is consistent."""
        return self.conflict_chains[0] if self.conflict_chains else None

    @property
    def sets(self):
        result = collections.OrderedDict((name, []) for name in SET_NAMES)
        for component in self.components:
            target = self.assignment.get(component.key)
            if target:
                result[target].extend(component.segment_ids)
        return result

    @property
    def ratio_actual(self):
        total = sum(self.durations.values())
        if total <= 0:
            return [0.0, 0.0]
        return [
            round(self.durations[name] / total, 6) for name in SET_NAMES
        ]

    @property
    def ratio_requested(self):
        total = sum(self.manifest.ratio)
        return [round(value / total, 6) for value in self.manifest.ratio]

    def as_dict(self):
        groups = []
        for component in self.components:
            target = self.assignment.get(component.key)
            groups.append(
                {
                    "segments": list(component.segment_ids),
                    "duration_ms": component.duration_ms,
                    "set": target,
                    "locked": bool(target and self._is_locked(component)),
                }
            )
        return {
            "ok": self.ok,
            "ratio_requested": self.ratio_requested,
            "ratio_actual": self.ratio_actual,
            "target_duration_ms": dict(self.target_durations),
            "duration_ms": dict(self.durations),
            "deviation_ms": abs(
                self.durations[TRAIN] - self.target_durations[TRAIN]
            ),
            "within_tolerance": self.within_tolerance,
            "budget_feasible": self.budget_feasible,
            "budgets_ms": {
                name: self.manifest.budgets.get(name) for name in SET_NAMES
            },
            "sets": self.sets,
            "groups": groups,
            "conflict_chains": self.conflict_chains,
            "unassigned": [list(group) for group in self.unassigned],
            "warnings": self.warnings,
        }

    def _is_locked(self, component):
        if not self.manifest.locks:
            return False
        members = set(component.segment_ids)
        for lock in self.manifest.locks:
            if set(lock.segments) <= members:
                return True
        return False


def split(data=None, **kwargs):
    """Split a manifest dictionary (or return an existing result).

    Keyword arguments are accepted as a shorthand for the common manifest
    fields, for example ``split(segments, ratio=..., must_link=...)``.
    """
    if isinstance(data, SplitResult):
        return data
    if isinstance(data, Manifest):
        manifest = data
    else:
        if data is None:
            data = {}
        if isinstance(data, (list, tuple)):
            data = {"segments": list(data)}
        elif not isinstance(data, Mapping):
            raise SplitError("manifest must be a JSON object or segment list")
        else:
            data = dict(data)
        for key, value in kwargs.items():
            data.setdefault(key, value)
        manifest = Manifest.from_dict(data)
    return assign(manifest)


def split_corpus(data):
    """Alias for :func:`split`, for callers which prefer an explicit name."""
    return split(data)


load_manifest = Manifest.from_json_file


def validate(data):
    """Build the manifest and check its links without producing a split."""
    if isinstance(data, Manifest):
        manifest = data
    else:
        manifest = Manifest.from_dict(data)
    assign(manifest)
    return manifest


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------


def format_text(result):
    lines = []
    if result.conflict_chains:
        lines.append("No split produced: conflicting locks.")
        for index, chain in enumerate(result.conflict_chains, 1):
            lines.append("Conflict %d:" % index)
            lines.append("  " + chain["message"])
        lines.append(
            "All %d groups remain unassigned; no subtitles were dropped."
            % len(result.unassigned)
        )
        return "\n".join(lines)

    requested = result.ratio_requested
    actual = result.ratio_actual
    lines.append(
        "Ratio: requested %.4f/%.4f, actual %.4f/%.4f, deviation %d ms"
        % (
            requested[0],
            requested[1],
            actual[0],
            actual[1],
            abs(result.durations[TRAIN] - result.target_durations[TRAIN]),
        )
    )
    for name in SET_NAMES:
        lines.append(
            "%s: %d groups, %d ms"
            % (
                name,
                sum(
                    1
                    for component in result.components
                    if result.assignment.get(component.key) == name
                ),
                result.durations[name],
            )
        )
        for component in result.components:
            if result.assignment.get(component.key) != name:
                continue
            lines.append(
                "  - [%s] %d ms"
                % (", ".join(component.segment_ids), component.duration_ms)
            )
    for warning in result.warnings:
        lines.append("warning: %s" % warning)
    return "\n".join(lines)


def _time_arg(value):
    return parse_time_ms(value, "--tolerance", "command line")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="srt-split",
        description=(
            "Split a subtitle corpus into train and verification sets by "
            "whole interviews."
        ),
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        default="-",
        help="manifest JSON file, or - for stdin (default: -)",
    )
    parser.add_argument(
        "-o", "--output", help="write the result here instead of stdout"
    )
    parser.add_argument(
        "--ratio",
        metavar="TRAIN:VERIFICATION",
        help="override the manifest ratio, for example 4:1",
    )
    parser.add_argument(
        "--budget",
        action="append",
        default=[],
        metavar="SET=MILLISECONDS",
        help="override a duration budget; may be repeated",
    )
    parser.add_argument(
        "--tolerance",
        type=_time_arg,
        help="acceptable absolute deviation from the target, in milliseconds",
    )
    parser.add_argument(
        "--text", action="store_true", help="human-readable output instead of JSON"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only validate the manifest; do not print a split",
    )
    args = parser.parse_args(argv)

    try:
        if args.manifest == "-":
            data = json.load(sys.stdin)
        else:
            with io.open(args.manifest, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        manifest = Manifest.from_dict(data)
        if args.ratio:
            parts = args.ratio.split(":")
            if len(parts) != 2:
                raise SplitError("--ratio must look like TRAIN:VERIFICATION")
            manifest.ratio = (float(parts[0]), float(parts[1]))
            if any(value < 0 for value in manifest.ratio) or not sum(manifest.ratio):
                raise SplitError("--ratio values must be non-negative")
        for item in args.budget:
            if "=" not in item:
                raise SplitError("--budget must look like SET=MILLISECONDS")
            name, value = item.split("=", 1)
            manifest.budgets[_normalise_set(name, "--budget")] = parse_time_ms(
                value, "--budget", "command line"
            )
        if args.tolerance is not None:
            manifest.tolerance_ms = args.tolerance
        result = assign(manifest)
    except (OSError, ValueError) as exc:
        sys.stderr.write("srt-split: %s\n" % exc)
        return 2

    if args.check:
        if result.ok:
            sys.stdout.write("manifest ok\n")
            return 0
        sys.stdout.write(format_text(result) + "\n")
        return 1

    payload = format_text(result) if args.text else json.dumps(
        result.as_dict(), indent=2, ensure_ascii=False
    )
    if args.output:
        with io.open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
