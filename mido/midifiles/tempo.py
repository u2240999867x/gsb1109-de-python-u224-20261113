# SPDX-FileCopyrightText: 2026 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Compatibility module exposing the tempo map API."""

from .timeline import (
    DEFAULT_TEMPO,
    DEFAULT_TICKS_PER_BEAT,
    TempoChange,
    TempoConflict,
    TempoConflictError,
    TempoMap,
    TempoSegment,
    build_tempo_map,
)

__all__ = [
    "DEFAULT_TEMPO",
    "DEFAULT_TICKS_PER_BEAT",
    "TempoChange",
    "TempoConflict",
    "TempoConflictError",
    "TempoMap",
    "TempoSegment",
    "build_tempo_map",
]
