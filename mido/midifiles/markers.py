# SPDX-FileCopyrightText: 2026 The mido contributors
#
# SPDX-License-Identifier: MIT

"""
Marker tables and de-duplication across tracks.

In multi-track MIDI files the same marker (the ``marker`` meta
message) is sometimes copied onto several tracks by the director's
software, while at other times two markers with the same name really
are separate entries written for different scenes. This module imports
the marker tables of the tracks and merges the copies back together.

The merge is deliberately non-destructive:

* every marker keeps its original position (the absolute tick of the
  marker on its own track, copies are never snapped to the primary);
* when a copied marker could equally well correspond to either of two
  adjacent markers on the primary track both interpretations are
  retained as an *unresolved* entry instead of guessing;
* no interpretation is allowed to collapse two markers that live on
  the same track into one, so distinct markers always stay distinct.
"""

from numbers import Integral


class Marker:
    """A marker observation on one track.

    Attributes:
        name: marker text (the ``text`` attribute of the ``marker``
            meta message).
        time: position in absolute ticks on the track.
        track: index of the track the marker was read from.
    """

    __slots__ = ('name', 'time', 'track')

    def __init__(self, name, time, track):
        self.name = name
        self.time = time
        self.track = track

    def __eq__(self, other):
        if not isinstance(other, Marker):
            return NotImplemented
        return (self.name == other.name
                and self.time == other.time
                and self.track == other.track)

    def __hash__(self):
        return hash((self.name, self.time, self.track))

    def __repr__(self):
        return 'Marker(name={!r}, time={}, track={})'.format(
            self.name, self.time, self.track)


class MarkerGroup:
    """One row of the merged marker table.

    A group gathers the markers on tracks that were declared replicas
    of one another and which are believed to be copies of the same
    scene marker.

    Attributes:
        primary: the :class:`Marker` observed on the primary track.
        copies: tuple of :class:`Marker` observations on the replica
            tracks, each kept at its original position.
        candidates: tuple of alternative primary :class:`Marker`
            objects. It is non-empty for unresolved groups, meaning a
            copy could equally well correspond to another adjacent
            marker on the primary track.
    """

    __slots__ = ('primary', 'copies', 'candidates')

    def __init__(self, primary, copies=(), candidates=()):
        self.primary = primary
        self.copies = tuple(copies)
        self.candidates = tuple(candidates)

    @property
    def resolved(self):
        """True when there is exactly one possible interpretation."""
        return not self.candidates

    @property
    def time(self):
        """Position of the primary marker, in absolute ticks."""
        return self.primary.time

    @property
    def name(self):
        """Name of the primary marker."""
        return self.primary.name

    @property
    def tracks(self):
        """Tuple of every track index represented in the group."""
        tracks = [self.primary.track]
        tracks.extend(marker.track for marker in self.copies)
        return tuple(tracks)

    def interpretations(self):
        """Yield every possible primary marker, one per interpretation.

        The first value is the primary marker itself; any remaining
        values come from :attr:`candidates` and represent alternative
        interpretations kept for unresolved groups.
        """
        yield self.primary
        yield from self.candidates

    def __eq__(self, other):
        if not isinstance(other, MarkerGroup):
            return NotImplemented
        return (self.primary == other.primary
                and self.copies == other.copies
                and self.candidates == other.candidates)

    def __hash__(self):
        return hash((self.primary, self.copies, self.candidates))

    def __repr__(self):
        if self.resolved:
            return 'MarkerGroup(primary={!r}, copies={!r})'.format(
                self.primary, list(self.copies))
        return ('MarkerGroup(primary={!r}, copies={!r}, '
                'candidates={!r})').format(
                    self.primary, list(self.copies),
                    list(self.candidates))


def _track_markers(track, track_index):
    """Return the markers of a track with absolute tick positions."""
    markers = []
    now = 0
    for message in track:
        now += message.time
        if message.type == 'marker':
            markers.append(Marker(message.text, now, track_index))
    return markers


def _normalize_replica_groups(replica_groups, num_tracks):
    """Turn the replica group argument into ordered index lists.

    The lowest index of each group is returned first and becomes the
    primary track.
    """
    groups = []
    seen = set()
    for group in replica_groups:
        indices = list(group)
        if len(set(indices)) != len(indices):
            raise ValueError(
                'a track may only appear once in a replica group: '
                '{}'.format(indices))
        for index in indices:
            if not isinstance(index, Integral) or index < 0:
                raise ValueError(
                    'replica group tracks must be non-negative track '
                    'indices: {!r}'.format(index))
            if index >= num_tracks:
                raise ValueError(
                    'replica group references unknown track {}'.format(index))
            if index in seen:
                raise ValueError(
                    'track {} belongs to more than one replica '
                    'group'.format(index))
            seen.add(index)
        groups.append(sorted(indices))
    return groups


def _dedupe(markers):
    """Drop duplicate markers while preserving the first occurrence."""
    result = []
    for marker in markers:
        if marker not in result:
            result.append(marker)
    return result


def merge_markers(tracks, replica_groups=(), tolerance=0):
    """Merge the marker tables of several tracks.

    Args:
        tracks: iterable of tracks, each a list of messages whose
            ``time`` is delta time in ticks (as in :class:`MidiTrack`).
        replica_groups: iterable of track index groups whose markers
            are known copies of each other, for example
            ``[(0, 1), (2, 3)]`` declares tracks 0 and 1 as replicas
            and tracks 2 and 3 as replicas. The lowest index in each
            group is the primary track; the others are replica tracks.
        tolerance: maximum distance in ticks between a replica marker
            and a primary marker for the two to be considered copies.

    Returns:
        A :class:`MarkerTable` holding the merged marker table.

    Markers are only merged when they share a name and their positions
    differ by at most ``tolerance`` ticks. A replica marker with no
    matching primary marker is kept as a marker of its own track rather
    than being dropped, and every marker stays at its original
    position.

    When a replica marker is within tolerance of more than one primary
    marker every possibility is retained: the row is flagged as
    unresolved and the alternative primary markers are listed in its
    ``candidates``. Matching is one-to-one per pair of tracks, so two
    markers on the same track can never be collapsed into one.
    """
    if not isinstance(tolerance, Integral) or tolerance < 0:
        raise ValueError('tolerance must be a non-negative integer')

    track_list = list(tracks)
    marker_lists = [
        _track_markers(track, index)
        for index, track in enumerate(track_list)
    ]
    normalized_groups = _normalize_replica_groups(
        replica_groups, len(track_list))

    involved_tracks = set()
    rows = []

    for indices in normalized_groups:
        primary_index = indices[0]
        replica_indices = indices[1:]
        involved_tracks.update(indices)

        primary_markers = marker_lists[primary_index]
        assigned = set()

        for primary in primary_markers:
            copies = []
            candidates = []
            for replica_index in replica_indices:
                matches = []
                for marker in marker_lists[replica_index]:
                    if id(marker) in assigned:
                        continue
                    if (marker.name == primary.name
                            and abs(marker.time - primary.time) <= tolerance):
                        matches.append(marker)
                if not matches:
                    continue

                best = min(
                    matches,
                    key=lambda marker: (
                        abs(marker.time - primary.time), marker.time))
                copies.append(best)
                assigned.add(id(best))

                for other in primary_markers:
                    if other is primary or other.name != primary.name:
                        continue
                    if abs(best.time - other.time) <= tolerance:
                        candidates.append(other)

            rows.append(MarkerGroup(primary, copies, _dedupe(candidates)))

        # Replica markers that were not assigned to a primary marker
        # remain rows of their own track. If a primary marker is still
        # within tolerance the row is unresolved instead of pretending
        # the ambiguity does not exist.
        for replica_index in replica_indices:
            for marker in marker_lists[replica_index]:
                if id(marker) in assigned:
                    continue
                alternatives = [
                    primary for primary in primary_markers
                    if (primary.name == marker.name
                        and abs(primary.time - marker.time) <= tolerance)
                ]
                rows.append(MarkerGroup(marker, candidates=alternatives))

    # Markers on tracks outside every replica group are never merged.
    for track_index, markers in enumerate(marker_lists):
        if track_index in involved_tracks:
            continue
        for marker in markers:
            rows.append(MarkerGroup(marker))

    rows.sort(key=lambda group: (group.time, group.primary.track))
    return MarkerTable(rows)


class MarkerTable:
    """The result of merging marker tables.

    Iterating over the table yields :class:`MarkerGroup` rows in
    chronological order. The settlement counts (certain rows versus
    rows still carrying alternative interpretations) are available
    through :attr:`resolved_count`, :attr:`unresolved_count` and
    :attr:`counts`.
    """

    def __init__(self, groups=()):
        self._groups = list(groups)

    def __iter__(self):
        return iter(self._groups)

    def __len__(self):
        return len(self._groups)

    def __eq__(self, other):
        if not isinstance(other, MarkerTable):
            return NotImplemented
        return self._groups == other._groups

    @property
    def groups(self):
        """Tuple of all :class:`MarkerGroup` rows."""
        return tuple(self._groups)

    @property
    def resolved_count(self):
        """Number of rows with a single, certain interpretation."""
        return sum(1 for group in self._groups if group.resolved)

    @property
    def unresolved_count(self):
        """Number of rows keeping two or more possible interpretations."""
        return sum(1 for group in self._groups if not group.resolved)

    @property
    def counts(self):
        """``(resolved_count, unresolved_count)`` as a tuple."""
        return self.resolved_count, self.unresolved_count

    def resolved(self):
        """Return the groups that have a single interpretation."""
        return [group for group in self._groups if group.resolved]

    def unresolved(self):
        """Return the groups that still carry alternative interpretations."""
        return [group for group in self._groups if not group.resolved]

    def markers(self):
        """Return every observation, including copies, as :class:`Marker`.

        Original positions are untouched. Unresolved groups contribute
        one marker per possible interpretation so that no possibility
        is hidden.
        """
        markers = []
        for group in self._groups:
            markers.append(group.primary)
            markers.extend(group.copies)
            markers.extend(group.candidates)
        return markers

    def __repr__(self):
        return 'MarkerTable(groups={!r})'.format(self._groups)
