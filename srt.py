#!/usr/bin/env python
# coding=utf8

"""A tiny library for parsing, modifying, and composing SRT files."""

from __future__ import unicode_literals
import functools
import itertools
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

TS_REGEX = re.compile(RGX_TIMESTAMP_PARSEABLE)
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

# A line that looks like a timestamp line, optionally preceded by an index
# line. Used by the ambiguous split logic to find plausible block boundaries
# inside raw text that may contain blank lines and quoted SRT data.
TIMESTAMP_LINE_REGEX = re.compile(
    r"^({ts}) *-[ -] *> *({ts}) ?({proprietary})$".format(
        ts=RGX_TIMESTAMP, proprietary=RGX_PROPRIETARY
    ),
    re.MULTILINE,
)
BLOCK_HEADER_REGEX = re.compile(
    r"^(?:{idx}{eof})?{ts} *-[ -] *> *{ts} ?{proprietary}$".format(
        idx=RGX_INDEX,
        ts=RGX_TIMESTAMP,
        proprietary=RGX_PROPRIETARY,
        eof=RGX_POSSIBLE_CRLF,
    ),
    re.MULTILINE,
)

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


def _normalise_split_input(srt_content):
    """
    Read a file-like object if needed, and normalise CRLF line endings to LF,
    as :func:`parse` does for content.
    """
    if isinstance(srt_content, FILE_TYPES):
        srt_content = srt_content.read()
    return srt_content.replace("\r\n", "\n")


def _coerce_subtitle(subtitle):
    """
    Accept either a Subtitle object or raw SRT block text (and file-like
    objects), and return a Subtitle.
    """
    if subtitle is None or isinstance(subtitle, Subtitle):
        return subtitle
    if isinstance(subtitle, FILE_TYPES):
        subtitle = subtitle.read()
    return next(iter(parse(subtitle)))


def parse_single_block(block):
    r"""
    Parse one raw SRT block into a single :class:`Subtitle`, treating every
    line after the block header as content.

    Unlike :func:`parse`, this never re-splits on timestamp-looking lines, so
    blank lines, numbers and timecodes quoted inside the content are retained
    verbatim. Use this on the blocks returned by
    :func:`resolve_ambiguous_split`, whose boundaries have already been
    determined.

    .. doctest::

        >>> block = (
        ...     "7\n"
        ...     "00:00:10,000 --> 00:00:12,000\n"
        ...     "证人引用了另一份字幕：\n"
        ...     "\n"
        ...     "2\n"
        ...     "00:00:01,000 --> 00:00:02,000\n"
        ...     "这是被引用的原话\n"
        ... )
        >>> sub = parse_single_block(block)
        >>> sub.index
        7
        >>> sub.content.splitlines()
        ['证人引用了另一份字幕：', '', '2', '00:00:01,000 --> 00:00:02,000', '这是被引用的原话']

    :param str block: One verbatim SRT block
    :rtype: Subtitle
    :raises SRTParseError: If the block does not start with a valid header
    """
    block = block.replace("\r\n", "\n")
    header_match = BLOCK_HEADER_REGEX.match(block)
    if header_match is None:
        raise SRTParseError(0, len(block), block)

    # Split on \n only: str.splitlines() also treats characters like the
    # U+001E record separator as line boundaries, which may legally appear in
    # proprietary metadata and would otherwise be silently dropped.
    header_lines = block[: header_match.end()].split("\n")
    timestamp_match = TIMESTAMP_LINE_REGEX.match(header_lines[0])
    if timestamp_match:
        raw_index = None
        raw_start, raw_end, proprietary = timestamp_match.groups()
    else:
        raw_index = header_lines[0].strip()
        try:
            raw_index = int(raw_index)
        except ValueError:
            # Index 123.4, same handling as in parse().
            raw_index = int(raw_index.split(".")[0])
        raw_start, raw_end, proprietary = TIMESTAMP_LINE_REGEX.match(
            header_lines[1]
        ).groups()

    content = block[header_match.end():]
    content = content.lstrip("\n").rstrip("\n")

    return Subtitle(
        index=raw_index,
        start=srt_timestamp_to_timedelta(raw_start),
        end=srt_timestamp_to_timedelta(raw_end),
        content=content,
        proprietary=proprietary,
    )


def _find_block_headers(srt_content):
    """
    Find every plausible SRT block header in ``srt_content``. A header is an
    optional index line followed by a timestamp line.

    :returns: ``(start, end, index)`` tuples, where ``index`` is None when no
              index line was present.
    """
    headers = []
    for match in BLOCK_HEADER_REGEX.finditer(srt_content):
        # Split on \n only: str.splitlines() also treats characters like the
        # U+001E record separator as line boundaries, which may legally appear
        # in proprietary metadata and would otherwise be silently dropped.
        header_lines = match.group(0).split("\n")
        if TIMESTAMP_LINE_REGEX.match(header_lines[0]):
            index = None
        else:
            raw_index = header_lines[0].strip()
            try:
                index = int(raw_index)
            except ValueError:
                # Index 123.4, same handling as in parse().
                index = int(raw_index.split(".")[0])
        headers.append((match.start(), match.end(), index))
    return headers


def enumerate_split_candidates(srt_content):
    r'''
    Enumerate every structurally plausible way to split raw SRT text into
    subtitle blocks.

    A line that looks like a timestamp (optionally preceded by an index line)
    may either start a real subtitle block, or be part of a subtitle's quoted
    content. Every subset of the plausible headers found after the first one
    is therefore treated as a possible set of block boundaries.

    The number of candidates grows exponentially with the number of plausible
    headers, so pass the localised excerpt under review rather than a whole
    subtitle file.

    :param srt_content: Subtitles in SRT format
    :type srt_content: str or a file-like object
    :returns: Each candidate as a tuple of raw, verbatim SRT block strings
    :rtype: :term:`generator` of tuples of str
    :raises SRTParseError: If no block header can be found, or text precedes
                           the first header
    '''
    srt_content = _normalise_split_input(srt_content)
    headers = _find_block_headers(srt_content)

    if not headers:
        raise SRTParseError(0, len(srt_content), srt_content)

    first_start = headers[0][0]
    if srt_content[:first_start].strip():
        raise SRTParseError(0, first_start, srt_content[:first_start])

    header_starts = [start for start, _, _ in headers]
    interior_starts = header_starts[1:]

    for width in range(len(interior_starts) + 1):
        for chosen in itertools.combinations(interior_starts, width):
            boundaries = [header_starts[0]]
            boundaries.extend(chosen)
            yield tuple(
                srt_content[boundaries[cur]:boundaries[cur + 1]]
                if cur + 1 < len(boundaries)
                else srt_content[boundaries[cur]:]
                for cur in range(len(boundaries))
            )


def _blocks_for_boundaries(srt_content, boundaries):
    return tuple(
        srt_content[boundaries[cur]:boundaries[cur + 1]]
        if cur + 1 < len(boundaries)
        else srt_content[boundaries[cur]:]
        for cur in range(len(boundaries))
    )


def _boundaries_match_anchors(
    boundaries, index_at, preceding, following, contiguous_indices
):
    if contiguous_indices is None:
        return True

    indices = [index_at[start] for start in boundaries]
    if any(index is None for index in indices):
        return False

    is_contiguous = all(
        indices[cur] == indices[cur - 1] + 1 for cur in range(1, len(indices))
    )

    if contiguous_indices:
        if not is_contiguous:
            return False
        if (
            preceding is not None
            and preceding.index is not None
            and indices[0] != preceding.index + 1
        ):
            return False
        if (
            following is not None
            and following.index is not None
            and indices[-1] != following.index - 1
        ):
            return False
        return True

    # The indices were verified as NOT contiguous.
    if is_contiguous:
        return False
    if (
        preceding is not None
        and preceding.index is not None
        and indices[0] <= preceding.index
    ):
        return False
    if (
        following is not None
        and following.index is not None
        and indices[-1] >= following.index
    ):
        return False
    return True


def _ambiguity_span(boundary_sets, total_len):
    """
    Find the shortest span that all surviving segmentations disagree on.
    Boundary positions agreed on by every candidate delimit an unambiguous
    prefix and suffix; everything in between is the ambiguous range.
    """
    min_len = min(len(boundaries) for boundaries in boundary_sets)

    prefix_len = 0
    while prefix_len < min_len and all(
        boundaries[prefix_len] == boundary_sets[0][prefix_len]
        for boundaries in boundary_sets
    ):
        prefix_len += 1

    suffix_len = 0
    while suffix_len < min_len - prefix_len and all(
        boundaries[len(boundaries) - 1 - suffix_len]
        == boundary_sets[0][len(boundary_sets[0]) - 1 - suffix_len]
        for boundaries in boundary_sets
    ):
        suffix_len += 1

    disputed_starts = [
        boundaries[prefix_len]
        for boundaries in boundary_sets
        if len(boundaries) > prefix_len
    ]
    ambiguity_start = min(disputed_starts)

    if suffix_len:
        reference = boundary_sets[0]
        ambiguity_end = reference[len(reference) - suffix_len]
    else:
        ambiguity_end = total_len

    return ambiguity_start, ambiguity_end


def resolve_ambiguous_split(
    srt_content, preceding=None, following=None, contiguous_indices=None
):
    r'''
    Resolve how ambiguous raw SRT text should be split into subtitle blocks,
    using verified neighbouring subtitles as anchors.

    Loose parsing splits at every blank line, so a subtitle whose content
    quotes another subtitle verbatim (blank lines, numbers and a timecode line
    included) gets torn apart. Strict parsing instead discards or chokes on
    the text following the blank line. This helper enumerates every
    structurally plausible split and keeps those consistent with the anchors.

    ``preceding`` and ``following`` are the verified subtitles immediately
    before and after the excerpt, given as :class:`Subtitle` objects, raw SRT
    block text, or file-like objects. ``contiguous_indices`` verifies whether
    the block indices are strictly consecutive: True to require it, False to
    require a gap, or None not to check indices at all.

    .. doctest::

        >>> raw = (
        ...     "7\n"
        ...     "00:00:10,000 --> 00:00:12,000\n"
        ...     "证人引用了另一份字幕：\n"
        ...     "\n"
        ...     "2\n"
        ...     "00:00:01,000 --> 00:00:02,000\n"
        ...     "这是被引用的原话\n"
        ...     "\n"
        ...     "8\n"
        ...     "00:00:13,000 --> 00:00:15,000\n"
        ...     "后文证词\n"
        ... )
        >>> preceding = next(iter(parse(
        ...     "6\n00:00:08,000 --> 00:00:09,000\n前文\n"
        ... )))
        >>> following = next(iter(parse(
        ...     "9\n00:00:16,000 --> 00:00:17,000\n再后文\n"
        ... )))
        >>> resolution = resolve_ambiguous_split(
        ...     raw, preceding, following, contiguous_indices=True
        ... )
        >>> resolution.unique
        True
        >>> len(resolution.blocks)
        2
        >>> subs = resolution.to_subtitles()
        >>> [sub.index for sub in subs]
        [7, 8]
        >>> subs[0].content.splitlines()[-1]
        '这是被引用的原话'

    With no anchors, several splits remain plausible, and the shortest
    ambiguous span is reported instead of guessing:

    .. doctest::

        >>> resolution = resolve_ambiguous_split(raw)
        >>> resolution.unique
        False
        >>> raw[resolution.ambiguity_start:].startswith("2\n")
        True
        >>> resolution.ambiguity_end == len(raw)
        True

    :param srt_content: The ambiguous excerpt in SRT format
    :type srt_content: str or a file-like object
    :param preceding: The verified subtitle immediately before the excerpt
    :param following: The verified subtitle immediately after the excerpt
    :param bool contiguous_indices: Whether block indices are verified to be
                                    strictly consecutive
    :returns: A :class:`SplitResolution` describing the surviving splits
    :rtype: SplitResolution
    :raises SRTParseError: If no structurally valid header exists, or no
                           candidate split is consistent with the anchors
    '''
    srt_content = _normalise_split_input(srt_content)
    headers = _find_block_headers(srt_content)

    if not headers:
        raise SRTParseError(0, len(srt_content), srt_content)

    first_start = headers[0][0]
    if srt_content[:first_start].strip():
        raise SRTParseError(0, first_start, srt_content[:first_start])

    preceding = _coerce_subtitle(preceding)
    following = _coerce_subtitle(following)

    index_at = {start: index for start, _, index in headers}
    header_starts = [start for start, _, _ in headers]
    interior_starts = header_starts[1:]

    surviving_boundaries = []
    for width in range(len(interior_starts) + 1):
        for chosen in itertools.combinations(interior_starts, width):
            boundaries = [header_starts[0]]
            boundaries.extend(chosen)
            if _boundaries_match_anchors(
                boundaries, index_at, preceding, following, contiguous_indices
            ):
                surviving_boundaries.append(tuple(boundaries))

    if not surviving_boundaries:
        raise SRTParseError(0, len(srt_content), srt_content)

    surviving_candidates = [
        _blocks_for_boundaries(srt_content, boundaries)
        for boundaries in surviving_boundaries
    ]

    if len(surviving_candidates) == 1:
        return SplitResolution(srt_content, (surviving_candidates[0],))

    ambiguity_start, ambiguity_end = _ambiguity_span(
        surviving_boundaries, len(srt_content)
    )
    return SplitResolution(
        srt_content,
        tuple(surviving_candidates),
        ambiguity_start,
        ambiguity_end,
    )


class SplitResolution(object):
    """
    The result of :func:`resolve_ambiguous_split`.

    :param str srt_content: The normalised excerpt that was examined
    :param candidates: Every surviving split, each a tuple of raw SRT block
                       strings
    :param int ambiguity_start: Char offset where the shortest ambiguous span
                                begins (None when the split is unique)
    :param int ambiguity_end: Char offset where the shortest ambiguous span
                              ends
    """

    def __init__(
        self, srt_content, candidates, ambiguity_start=None, ambiguity_end=None
    ):
        self.srt_content = srt_content
        self.candidates = tuple(candidates)
        self.unique = len(self.candidates) == 1
        self.blocks = self.candidates[0] if self.unique else None
        self.ambiguity_start = ambiguity_start
        self.ambiguity_end = ambiguity_end

    @property
    def ambiguity_content(self):
        """The raw text covered by the shortest ambiguous span."""
        if self.ambiguity_start is None:
            return None
        return self.srt_content[self.ambiguity_start:self.ambiguity_end]

    def to_subtitles(self):
        """
        Parse the blocks of a unique split into :class:`Subtitle` objects.

        Quoted SRT data inside a block is preserved as content rather than
        being split again.

        :raises AmbiguousSRTParseError: If more than one split remains
        """
        if not self.unique:
            raise AmbiguousSRTParseError(self)
        return [parse_single_block(block) for block in self.blocks]


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


class AmbiguousSRTParseError(SRTParseError):
    """
    Raised when an excerpt can be split into subtitle blocks in more than one
    way consistent with the supplied anchors.

    :param SplitResolution resolution: The surviving splits and the shortest
                                       ambiguous span, for manual review
    """

    def __init__(self, resolution):
        super(AmbiguousSRTParseError, self).__init__(
            resolution.ambiguity_start,
            resolution.ambiguity_end,
            resolution.ambiguity_content,
        )
        self.resolution = resolution


class _ShouldSkipException(Exception):
    """
    Raised when a subtitle should be skipped.
    """
