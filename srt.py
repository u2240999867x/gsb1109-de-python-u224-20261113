#!/usr/bin/env python
# coding=utf8

"""A tiny library for parsing, modifying, and composing SRT files."""

from __future__ import unicode_literals
import functools
import re
from datetime import timedelta
import logging
import io


LOG = logging.getLogger(__name__)

# "." is not technically valid as a delimiter, but many editors create SRT
# files with this delimiter for whatever reason. Many editors and players
# accept it, so we do too.
RGX_TIMESTAMP_MAGNITUDE_DELIM = r"[,.:，．。：]"
RGX_TIMESTAMP_FIELD = r"[0-9]+"
RGX_TIMESTAMP_FIELD_OPTIONAL = r"[0-9]*"
RGX_TIMESTAMP = "".join(
    [
        RGX_TIMESTAMP_MAGNITUDE_DELIM.join([RGX_TIMESTAMP_FIELD] * 3),
        RGX_TIMESTAMP_MAGNITUDE_DELIM,
        "?",
        RGX_TIMESTAMP_FIELD_OPTIONAL,
    ]
)
RGX_TIMESTAMP_PARSEABLE = r"^{}$".format(
    "".join(
        [
            RGX_TIMESTAMP_MAGNITUDE_DELIM.join(["(" + RGX_TIMESTAMP_FIELD + ")"] * 3),
            RGX_TIMESTAMP_MAGNITUDE_DELIM,
            "?",
            "(",
            RGX_TIMESTAMP_FIELD_OPTIONAL,
            ")",
        ]
    )
)
RGX_INDEX = r"-?[0-9]+\.?[0-9]*"
RGX_PROPRIETARY = r"[^\r\n]*"
RGX_CONTENT = r".*?"
RGX_POSSIBLE_CRLF = r"\r?\n"

# The default speaker marker rule. A content line is considered to carry a
# speaker name when it is shaped like "Name: text" (a half or full width
# colon is accepted). The name may not itself contain a colon or start with
# whitespace. Sources with a different marker rule can pass their own
# compiled pattern to split_speaker_tracks.
RGX_SPEAKER_PREFIX = r"^([^:\s：][^:：]*?)\s*[:：]\s*(.*)$"

TS_REGEX = re.compile(RGX_TIMESTAMP_PARSEABLE)
SPEAKER_PREFIX_REGEX = re.compile(RGX_SPEAKER_PREFIX)
MULTI_WS_REGEX = re.compile(r"\n\n+")
SRT_REGEX = re.compile(
    r"\s*(?:({idx})\s*{eof})?({ts}) *-[ -] *> *({ts}) ?({proprietary})(?:{eof}|\Z)({content})"
    # Many sub editors don't add a blank line to the end, and many editors and
    # players accept that. We allow it to be missing in input.
    #
    # We also allow subs that are missing a double blank newline. This often
    # happens on subs which were first created as a mixed language subtitle,
    # for example chs/eng, and then were stripped using naive methods (such as
    # ed/sed) that don't understand newline preservation rules in SRT files.
    #
    # This means that when you are, say, only keeping chs, and the line only
    # contains english, you end up with not only no content, but also all of
    # the content lines are stripped instead of retaining a newline.
    r"(?:{eof}|\Z)(?:{eof}|\Z|(?=(?:{idx}\s*{eof}{ts})))"
    # Some SRT blocks, while this is technically invalid, have blank lines
    # inside the subtitle content. We look ahead a little to check that the
    # next lines look like an index and a timestamp as a best-effort
    # solution to work around these.
    r"(?=(?:(?:{idx}\s*{eof})?{ts}|\Z))".format(
        idx=RGX_INDEX,
        ts=RGX_TIMESTAMP,
        proprietary=RGX_PROPRIETARY,
        content=RGX_CONTENT,
        eof=RGX_POSSIBLE_CRLF,
    ),
    re.DOTALL,
)

ZERO_TIMEDELTA = timedelta(0)

# Info message if truthy return -> Function taking a Subtitle, skip if True
SUBTITLE_SKIP_CONDITIONS = (
    ("No content", lambda sub: not sub.content.strip()),
    ("Start time < 0 seconds", lambda sub: sub.start < ZERO_TIMEDELTA),
    ("Subtitle start time >= end time", lambda sub: sub.start >= sub.end),
)

SECONDS_IN_HOUR = 3600
SECONDS_IN_MINUTE = 60
HOURS_IN_DAY = 24
MICROSECONDS_IN_MILLISECOND = 1000

try:
    FILE_TYPES = (file, io.IOBase)  # pytype: disable=name-error
except NameError:  # `file` doesn't exist in Python 3
    FILE_TYPES = (io.IOBase,)


@functools.total_ordering
class Subtitle(object):
    r"""
    The metadata relating to a single subtitle. Subtitles are sorted by start
    time by default. If no index was provided, index 0 will be used on writing
    an SRT block.

    :param index: The SRT index for this subtitle
    :type index: int or None
    :param start: The time that the subtitle should start being shown
    :type start: :py:class:`datetime.timedelta`
    :param end: The time that the subtitle should stop being shown
    :type end: :py:class:`datetime.timedelta`
    :param str proprietary: Proprietary metadata for this subtitle
    :param str content: The subtitle content. Should not contain OS-specific
                        line separators, only \\n. This is taken care of
                        already if you use :py:func:`srt.parse` to generate
                        Subtitle objects.
    """

    # pylint: disable=R0913
    def __init__(self, index, start, end, content, proprietary=""):
        self.index = index
        self.start = start
        self.end = end
        self.content = content
        self.proprietary = proprietary

    def __hash__(self):
        return hash(frozenset(vars(self).items()))

    def __eq__(self, other):
        return isinstance(other, Subtitle) and vars(self) == vars(other)

    def __lt__(self, other):
        return (self.start, self.end, self.index) < (
            other.start,
            other.end,
            other.index,
        )

    def __repr__(self):
        # Python 2/3 cross compatibility
        var_items = getattr(vars(self), "iteritems", getattr(vars(self), "items"))
        item_list = ", ".join("%s=%r" % (k, v) for k, v in var_items())
        return "%s(%s)" % (type(self).__name__, item_list)

    def to_srt(self, strict=True, eol="\n"):
        r"""
        Convert the current :py:class:`Subtitle` to an SRT block.

        :param bool strict: If disabled, will allow blank lines in the content
                            of the SRT block, which is a violation of the SRT
                            standard and may cause your media player to explode
        :param str eol: The end of line string to use (default "\\n")
        :returns: The metadata of the current :py:class:`Subtitle` object as an
                  SRT formatted subtitle block
        :rtype: str
        """
        output_content = self.content
        output_proprietary = self.proprietary

        if output_proprietary:
            # output_proprietary is output directly next to the timestamp, so
            # we need to add the space as a field delimiter.
            output_proprietary = " " + output_proprietary

        if strict:
            output_content = make_legal_content(output_content)

        if eol is None:
            eol = "\n"
        elif eol != "\n":
            output_content = output_content.replace("\n", eol)

        template = "{idx}{eol}{start} --> {end}{prop}{eol}{content}{eol}{eol}"
        return template.format(
            idx=self.index or 0,
            start=timedelta_to_srt_timestamp(self.start),
            end=timedelta_to_srt_timestamp(self.end),
            prop=output_proprietary,
            content=output_content,
            eol=eol,
        )


def make_legal_content(content):
    r"""
    Remove illegal content from a content block. Illegal content includes:

    * Blank lines
    * Starting or ending with a blank line

    .. doctest::

        >>> make_legal_content('\nfoo\n\nbar\n')
        'foo\nbar'

    :param str content: The content to make legal
    :returns: The legalised content
    :rtype: srt
    """
    # Optimisation: Usually the content we get is legally valid. Do a quick
    # check to see if we really need to do anything here. This saves time from
    # generating legal_content by about 50%.
    if content and content[0] != "\n" and "\n\n" not in content:
        return content

    legal_content = MULTI_WS_REGEX.sub("\n", content.strip("\n"))
    LOG.info("Legalised content %r to %r", content, legal_content)
    return legal_content


def timedelta_to_srt_timestamp(timedelta_timestamp):
    r"""
    Convert a :py:class:`~datetime.timedelta` to an SRT timestamp.

    .. doctest::

        >>> import datetime
        >>> delta = datetime.timedelta(hours=1, minutes=23, seconds=4)
        >>> timedelta_to_srt_timestamp(delta)
        '01:23:04,000'

    :param datetime.timedelta timedelta_timestamp: A datetime to convert to an
                                                   SRT timestamp
    :returns: The timestamp in SRT format
    :rtype: str
    """

    hrs, secs_remainder = divmod(timedelta_timestamp.seconds, SECONDS_IN_HOUR)
    hrs += timedelta_timestamp.days * HOURS_IN_DAY
    mins, secs = divmod(secs_remainder, SECONDS_IN_MINUTE)
    msecs = timedelta_timestamp.microseconds // MICROSECONDS_IN_MILLISECOND
    return "%02d:%02d:%02d,%03d" % (hrs, mins, secs, msecs)


def srt_timestamp_to_timedelta(timestamp):
    r"""
    Convert an SRT timestamp to a :py:class:`~datetime.timedelta`.

    .. doctest::

        >>> srt_timestamp_to_timedelta('01:23:04,000')
        datetime.timedelta(seconds=4984)

    :param str timestamp: A timestamp in SRT format
    :returns: The timestamp as a :py:class:`~datetime.timedelta`
    :rtype: datetime.timedelta
    :raises TimestampParseError: If the timestamp is not parseable
    """

    match = TS_REGEX.match(timestamp)
    if match is None:
        raise TimestampParseError("Unparseable timestamp: {}".format(timestamp))
    hrs, mins, secs, msecs = [int(m) if m else 0 for m in match.groups()]
    return timedelta(hours=hrs, minutes=mins, seconds=secs, milliseconds=msecs)


def sort_and_reindex(subtitles, start_index=1, in_place=False, skip=True):
    """
    Reorder subtitles to be sorted by start time order, and rewrite the indexes
    to be in that same order. This ensures that the SRT file will play in an
    expected fashion after, for example, times were changed in some subtitles
    and they may need to be resorted.

    If skip=True, subtitles will also be skipped if they are considered not to
    be useful. Currently, the conditions to be considered "not useful" are as
    follows:

    - Content is empty, or only whitespace
    - The start time is negative
    - The start time is equal to or later than the end time

    .. doctest::

        >>> from datetime import timedelta
        >>> one = timedelta(seconds=1)
        >>> two = timedelta(seconds=2)
        >>> three = timedelta(seconds=3)
        >>> subs = [
        ...     Subtitle(index=999, start=one, end=two, content='1'),
        ...     Subtitle(index=0, start=two, end=three, content='2'),
        ... ]
        >>> list(sort_and_reindex(subs))  # doctest: +ELLIPSIS
        [Subtitle(...index=1...), Subtitle(...index=2...)]

    :param subtitles: :py:class:`Subtitle` objects in any order
    :param int start_index: The index to start from
    :param bool in_place: Whether to modify subs in-place for performance
                          (version <=1.0.0 behaviour)
    :param bool skip: Whether to skip subtitles considered not useful (see
                      above for rules)
    :returns: The sorted subtitles
    :rtype: :term:`generator` of :py:class:`Subtitle` objects
    """
    skipped_subs = 0
    for sub_num, subtitle in enumerate(sorted(subtitles), start=start_index):
        if not in_place:
            subtitle = Subtitle(**vars(subtitle))

        if skip:
            try:
                _should_skip_sub(subtitle)
            except _ShouldSkipException as thrown_exc:
                if subtitle.index is None:
                    LOG.info("Skipped subtitle with no index: %s", thrown_exc)
                else:
                    LOG.info(
                        "Skipped subtitle at index %d: %s", subtitle.index, thrown_exc
                    )
                skipped_subs += 1
                continue

        subtitle.index = sub_num - skipped_subs

        yield subtitle


def _should_skip_sub(subtitle):
    """
    Check if a subtitle should be skipped based on the rules in
    SUBTITLE_SKIP_CONDITIONS.

    :param subtitle: A :py:class:`Subtitle` to check whether to skip
    :raises _ShouldSkipException: If the subtitle should be skipped
    """
    for info_msg, sub_skipper in SUBTITLE_SKIP_CONDITIONS:
        if sub_skipper(subtitle):
            raise _ShouldSkipException(info_msg)


def parse(srt, ignore_errors=False):
    r'''
    Convert an SRT formatted string (in Python 2, a :class:`unicode` object) to
    a :term:`generator` of Subtitle objects.

    This function works around bugs present in many SRT files, most notably
    that it is designed to not bork when presented with a blank line as part of
    a subtitle's content.

    .. doctest::

        >>> subs = parse("""\
        ... 422
        ... 00:31:39,931 --> 00:31:41,931
        ... Using mainly spoons,
        ...
        ... 423
        ... 00:31:41,933 --> 00:31:43,435
        ... we dig a tunnel under the city and release it into the wild.
        ...
        ... """)
        >>> list(subs)  # doctest: +ELLIPSIS
        [Subtitle(...index=422...), Subtitle(...index=423...)]

    :param srt: Subtitles in SRT format
    :type srt: str or a file-like object
    :param ignore_errors: If True, garbled SRT data will be ignored, and we'll
                          continue trying to parse the rest of the file,
                          instead of raising :py:class:`SRTParseError` and
                          stopping execution.
    :returns: The subtitles contained in the SRT file as :py:class:`Subtitle`
              objects
    :rtype: :term:`generator` of :py:class:`Subtitle` objects
    :raises SRTParseError: If the matches are not contiguous and
                           ``ignore_errors`` is False.
    '''

    expected_start = 0

    # Transparently read files -- the whole thing is needed for regex's
    # finditer
    if isinstance(srt, FILE_TYPES):
        srt = srt.read()

    for match in SRT_REGEX.finditer(srt):
        actual_start = match.start()
        _check_contiguity(srt, expected_start, actual_start, ignore_errors)
        raw_index, raw_start, raw_end, proprietary, content = match.groups()

        # pytype sees that this is Optional[str] and thus complains that they
        # can be None, but they can't realistically be None, since we're using
        # finditer and all match groups are mandatory in the regex.
        content = content.replace("\r\n", "\n")  # pytype: disable=attribute-error

        try:
            raw_index = int(raw_index)
        except ValueError:
            # Index 123.4. Handled separately, since it's a rare case and we
            # don't want to affect general performance.
            #
            # The pytype disable is for the same reason as content, above.
            raw_index = int(raw_index.split(".")[0])  # pytype: disable=attribute-error
        except TypeError:
            # There's no index, so raw_index is already set to None. We'll
            # handle this when rendering the subtitle with to_srt.
            pass

        yield Subtitle(
            index=raw_index,
            start=srt_timestamp_to_timedelta(raw_start),
            end=srt_timestamp_to_timedelta(raw_end),
            content=content,
            proprietary=proprietary,
        )

        expected_start = match.end()

    _check_contiguity(srt, expected_start, len(srt), ignore_errors)


def _check_contiguity(srt, expected_start, actual_start, warn_only):
    """
    If ``warn_only`` is False, raise :py:class:`SRTParseError` with diagnostic
    info if expected_start does not equal actual_start. Otherwise, log a
    warning.

    :param str srt: The data being matched
    :param int expected_start: The expected next start, as from the last
                               iteration's match.end()
    :param int actual_start: The actual start, as from this iteration's
                             match.start()
    :raises SRTParseError: If the matches are not contiguous and ``warn_only``
                           is False
    """
    if expected_start != actual_start:
        unmatched_content = srt[expected_start:actual_start]

        if expected_start == 0 and (
            unmatched_content.isspace() or unmatched_content == "\ufeff"
        ):
            # #50: Leading whitespace has nowhere to be captured like in an
            # intermediate subtitle
            return

        if warn_only:
            LOG.warning("Skipped unparseable SRT data: %r", unmatched_content)
        else:
            raise SRTParseError(expected_start, actual_start, unmatched_content)


def compose(
    subtitles, reindex=True, start_index=1, strict=True, eol=None, in_place=False
):
    r"""
    Convert an iterator of :py:class:`Subtitle` objects to a string of joined
    SRT blocks.

    .. doctest::

        >>> from datetime import timedelta
        >>> start = timedelta(seconds=1)
        >>> end = timedelta(seconds=2)
        >>> subs = [
        ...     Subtitle(index=1, start=start, end=end, content='x'),
        ...     Subtitle(index=2, start=start, end=end, content='y'),
        ... ]
        >>> compose(subs)  # doctest: +ELLIPSIS
        '1\n00:00:01,000 --> 00:00:02,000\nx\n\n2\n00:00:01,000 --> ...'

    :param subtitles: The subtitles to convert to SRT blocks
    :type subtitles: :term:`iterator` of :py:class:`Subtitle` objects
    :param bool reindex: Whether to reindex subtitles based on start time
    :param int start_index: If reindexing, the index to start reindexing from
    :param bool strict: Whether to enable strict mode, see
                        :py:func:`Subtitle.to_srt` for more information
    :param str eol: The end of line string to use (default "\\n")
    :returns: A single SRT formatted string, with each input
              :py:class:`Subtitle` represented as an SRT block
    :param bool in_place: Whether to reindex subs in-place for performance
                          (version <=1.0.0 behaviour)
    :rtype: str
    """
    if reindex:
        subtitles = sort_and_reindex(
            subtitles, start_index=start_index, in_place=in_place
        )

    return "".join(subtitle.to_srt(strict=strict, eol=eol) for subtitle in subtitles)


def parse_speaker_line(line, marker_regex=SPEAKER_PREFIX_REGEX):
    """
    Split a single content line into ``(speaker, text)`` when the line carries
    a speaker marker, otherwise return ``(None, line)``.

    The default marker rule matches lines shaped like ``"Alice: hello"``
    (a half or full width colon is accepted). A different rule from the
    source data can be supplied as a compiled pattern whose first group is
    the displayed speaker name and whose second group is the spoken text.

    :param str line: One line of subtitle content (no line separator)
    :param marker_regex: A compiled regex describing the marker rule
    :returns: ``(speaker name, spoken text)`` or ``(None, original line)``
    :rtype: tuple
    """
    match = marker_regex.match(line)
    if match is None:
        return None, line
    return match.group(1), match.group(2)


class SpeakerAliasTable(object):
    """
    Map display names that change over time back to a canonical speaker.

    An alias has an optional validity window, expressed in subtitle time. A
    window is a half-open interval: an alias applies at time ``t`` when
    ``start <= t < end``. A window edge of ``None`` means unbounded. When
    several entries match the same name at the same time, the entry added
    last wins.

    Alias entries are accepted as three-tuples
    ``(canonical, display_name, window)`` or two-tuples
    ``(canonical, display_name)`` for an alias that is always valid. The
    window itself may be a two-tuple ``(start, end)`` of
    :py:class:`~datetime.timedelta` (either may be ``None``).
    """

    def __init__(self, aliases=()):
        # Most recently added entries take precedence, hence reversed order.
        self._aliases = []
        for entry in aliases:
            self.add_alias(*entry)

    @staticmethod
    def _normalise_window(window):
        if window is None:
            return None, None
        start, end = window
        if start is not None and end is not None and start > end:
            raise ValueError(
                "Alias window start (%r) is after its end (%r)" % (start, end)
            )
        return start, end

    def add_alias(self, canonical, display_name, window=None):
        """
        Record that ``display_name`` refers to ``canonical`` while ``window``
        is in effect.

        :param str canonical: The canonical speaker identifier
        :param str display_name: The name as shown on a subtitle line
        :param window: ``(start, end)`` timedeltas, either may be ``None``
        :returns: This table, so calls can be chained
        :rtype: SpeakerAliasTable
        """
        start, end = self._normalise_window(window)
        self._aliases.append((canonical, display_name, start, end))
        return self

    def resolve(self, display_name, at):
        """
        Return the canonical speaker for ``display_name`` at time ``at``.

        :param str display_name: The name shown on a subtitle line
        :param datetime.timedelta at: The subtitle time used to pick a window
        :returns: The canonical name, or the display name itself when no
                  alias matches
        :rtype: str
        """
        for canonical, alias_name, start, end in reversed(self._aliases):
            if alias_name != display_name:
                continue
            if start is not None and at < start:
                continue
            if end is not None and at >= end:
                continue
            return canonical
        return display_name

    def __len__(self):
        return len(self._aliases)

    def __eq__(self, other):
        return (
            isinstance(other, SpeakerAliasTable)
            and self._aliases == other._aliases
        )


class SpeakerTracks(object):
    """
    The independent speaker tracks produced by
    :py:func:`split_speaker_tracks`.

    ``tracks`` maps each canonical speaker to the
    :py:class:`Subtitle` objects attributed to that speaker, in the order in
    which they appeared in the source SRT. ``unattributed`` holds the
    subtitles which could not be attributed to any speaker, also in source
    order. Every value is a list, so any moment in time can be rebuilt by
    filtering on start/end time.

    ``sequence`` records every produced fragment as
    ``(speaker_or_None, Subtitle)`` pairs in exact source order, including
    fragments that share a timestamp. It is what :py:meth:`active_at` uses so
    that interleaved or repeated lines keep their original ordering.
    """

    def __init__(self, tracks=None, unattributed=None, sequence=None):
        self.tracks = tracks if tracks is not None else {}
        self.unattributed = unattributed if unattributed is not None else []
        self.sequence = sequence if sequence is not None else []

    def speakers(self):
        """
        :returns: The canonical speakers that own a track, in first-appearance
                  order
        :rtype: list
        """
        return list(self.tracks)

    def active_at(self, when):
        """
        Rebuild the list of utterances on screen at a given time.

        Both attributed tracks and unattributed fragments are considered.
        Results stay in source appearance order, matching the order in which
        the lines occurred in the original SRT, including several speakers
        sharing one timestamp.

        :param datetime.timedelta when: The time to inspect
        :returns: ``(speaker, Subtitle)`` pairs active at ``when``; speaker
                  is ``None`` for unattributed fragments
        :rtype: list
        """
        return [
            (speaker, subtitle)
            for speaker, subtitle in self.sequence
            if subtitle.start <= when < subtitle.end
        ]

    def compose_track(self, speaker, **compose_kwargs):
        """
        Compose one speaker's track back to an SRT string.

        :param str speaker: The canonical speaker whose track is wanted
        :param compose_kwargs: Passed through to :py:func:`compose`
        :returns: The track as an SRT string
        :rtype: str
        """
        return compose(list(self.tracks[speaker]), reindex=False, **compose_kwargs)

    def compose_unattributed(self, **compose_kwargs):
        """
        Compose the unattributed fragments back to an SRT string.

        :param compose_kwargs: Passed through to :py:func:`compose`
        :returns: The unattributed fragments as an SRT string
        :rtype: str
        """
        return compose(list(self.unattributed), reindex=False, **compose_kwargs)

    def __eq__(self, other):
        return (
            isinstance(other, SpeakerTracks)
            and self.tracks == other.tracks
            and self.unattributed == other.unattributed
            and self.sequence == other.sequence
        )

    def __repr__(self):
        return "SpeakerTracks(tracks=%r, unattributed=%r)" % (
            self.tracks,
            self.unattributed,
        )


def _fragments_for_subtitle(subtitle, marker_regex, aliases):
    """
    Split one subtitle's content into ordered attributed/unattributed
    fragments. Each fragment is ``(speaker_or_None, content)``. Consecutive
    unmarked lines are merged into one fragment, preserving every source
    line as a newline inside the fragment content.
    """
    fragments = []
    current_speaker = None
    current_lines = []

    def flush():
        if current_lines:
            fragments.append((current_speaker, "\n".join(current_lines)))
            current_lines[:] = []

    for line in subtitle.content.split("\n"):
        speaker, text = parse_speaker_line(line, marker_regex)
        if speaker is None:
            # A continuation line: before any marker it stays unattributed,
            # afterwards it belongs to the most recently named speaker.
            current_lines.append(line)
            continue

        flush()
        current_speaker = (
            aliases.resolve(speaker, subtitle.start) if aliases else speaker
        )
        current_lines = [text]

    flush()
    return fragments


def split_speaker_tracks(
    subtitles,
    aliases=None,
    marker_regex=SPEAKER_PREFIX_REGEX,
    reindex=True,
):
    r"""
    Split a mixed SRT into one independent track per speaker.

    Each content line is examined with the source's speaker marker rule.
    Lines like ``"Alice: hello"`` are attributed to Alice, while plain
    continuation lines are attached to the most recently named speaker in
    the same subtitle. A subtitle in which two marked lines name different
    speakers is split into several subtitles sharing the original timing.
    Lines that cannot be attributed to anyone are kept, unchanged, in their
    original position on the ``unattributed`` track.

    Display names that changed over time are mapped back to a canonical
    speaker with ``aliases`` (see :py:class:`SpeakerAliasTable`).

    The total number of output lines, and their order, match the input:
    each source line survives exactly once (newlines inside a fragment
    included), and :py:meth:`SpeakerTracks.active_at` reproduces the
    original utterance order, including repeated and interleaved lines.

    :param subtitles: :py:class:`Subtitle` objects, typically from
                      :py:func:`parse`
    :param SpeakerAliasTable aliases: Time-bounded display name mappings
    :param marker_regex: Compiled regex for the source's marker rule; its
                         first group is the name and second group the text
    :param bool reindex: Whether to renumber each track from 1, preserving
                         source order within the track
    :returns: The separated speaker tracks
    :rtype: SpeakerTracks
    """
    tracks = SpeakerTracks()
    track_indexes = {}

    def store(speaker, fragment):
        tracks.sequence.append((speaker, fragment))
        if speaker is None:
            tracks.unattributed.append(fragment)
            return
        track = tracks.tracks.setdefault(speaker, [])
        if reindex:
            track_indexes[speaker] = track_indexes.get(speaker, 0) + 1
            fragment.index = track_indexes[speaker]
        track.append(fragment)

    for subtitle in subtitles:
        for speaker, content in _fragments_for_subtitle(
            subtitle, marker_regex, aliases
        ):
            fragment = Subtitle(
                index=subtitle.index,
                start=subtitle.start,
                end=subtitle.end,
                content=content,
                proprietary=subtitle.proprietary,
            )
            store(speaker, fragment)

    return tracks


class SRTParseError(Exception):
    """
    Raised when part of an SRT block could not be parsed.

    :param int expected_start: The expected contiguous start index
    :param int actual_start: The actual non-contiguous start index
    :param str unmatched_content: The content between the expected start index
                                  and the actual start index
    """

    def __init__(self, expected_start, actual_start, unmatched_content):
        message = (
            "Expected contiguous start of match or end of input at char %d, "
            "but started at char %d (unmatched content: %r)"
            % (expected_start, actual_start, unmatched_content)
        )
        super(SRTParseError, self).__init__(message)

        self.expected_start = expected_start
        self.actual_start = actual_start
        self.unmatched_content = unmatched_content


class TimestampParseError(ValueError):
    """
    Raised when an SRT timestamp could not be parsed.
    """


class _ShouldSkipException(Exception):
    """
    Raised when a subtitle should be skipped.
    """
