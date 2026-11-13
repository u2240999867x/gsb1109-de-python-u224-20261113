# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Top-level access to the timeline API."""

from .midifiles.timeline import (
    DEFAULT_TEMPO,
    DEFAULT_TICKS_PER_BEAT,
    TempoChange,
    TempoConflict,
    TempoConflictError,
    TempoMap,
    TempoSegment,
    Timeline,
    TimelineEvent,
    TrackTimeline,
    build_tempo_map,
    build_timeline,
    export_event_table,
)

__all__ = [
    "DEFAULT_TEMPO",
    "DEFAULT_TICKS_PER_BEAT",
    "TempoChange",
    "TempoConflict",
    "TempoConflictError",
    "TempoMap",
    "TempoSegment",
    "Timeline",
    "TimelineEvent",
    "TrackTimeline",
    "build_tempo_map",
    "build_timeline",
    "export_event_table",
]
