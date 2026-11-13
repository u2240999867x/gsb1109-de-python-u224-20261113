# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Karaoke lyric indexing for MIDI files."""

from .indexing import (
    LookupResult,
    LyricEvent,
    LyricIndex,
    LyricSegment,
    Note,
    SyllableEntry,
    build_lyric_index,
    extract_lyric_events,
    extract_notes,
)

__all__ = [
    "LookupResult",
    "LyricEvent",
    "LyricIndex",
    "LyricSegment",
    "Note",
    "SyllableEntry",
    "build_lyric_index",
    "extract_lyric_events",
    "extract_notes",
]
