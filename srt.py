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
MILLISECONDS_IN_SECOND = 1000

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


def _total_microseconds(timedelta_timestamp):
    """Return the exact number of microseconds represented by a timedelta."""
    return (
        timedelta_timestamp.days * SECONDS_IN_HOUR * HOURS_IN_DAY
        + timedelta_timestamp.seconds
    ) * (
        MICROSECONDS_IN_MILLISECOND * MILLISECONDS_IN_SECOND
    ) + timedelta_timestamp.microseconds


def _nearest_milliseconds(timedelta_timestamp):
    """
    Round a timedelta to the nearest whole millisecond.

    Ties are resolved away from zero so that the absolute rounding error never
    exceeds 0.5 ms for a boundary considered on its own.
    """
    microseconds = _total_microseconds(timedelta_timestamp)
    sign = -1 if microseconds < 0 else 1
    rounded, remainder = divmod(abs(microseconds), MICROSECONDS_IN_MILLISECOND)
    if remainder * 2 >= MICROSECONDS_IN_MILLISECOND:
        rounded += 1
    return sign * rounded


def _as_milliseconds(value):
    """Normalise a timedelta or a raw count to a whole number of milliseconds."""
    if isinstance(value, timedelta):
        return _nearest_milliseconds(value)
    return int(value)


def _ceil_div(numerator, denominator):
    """Ceiling of integer division, also for negative numerators."""
    return -((-numerator) // denominator)


def _boundary_descriptor(subtitle_index, kind):
    """A human readable reference to one end of a subtitle."""
    return (subtitle_index + 1, "start" if kind == 0 else "end")


def _group_boundaries(subtitles):
    """
    Build one node per distinct instant, sharing a node across cutpoints.

    A boundary is ``(subtitle_index, kind)`` where kind 0 is a start and kind 1
    is an end. Boundaries pointing at the same timedelta are the same instant --
    notably an end shared with the next subtitle's start -- so they map to one
    node and must later be written to exactly one millisecond. Nodes are numbered
    in time order, so every subtitle's start node precedes its end node.

    :returns: ``(boundary_node, origin_us)`` where ``boundary_node`` maps a
              boundary to its node id and ``origin_us`` maps a node to the
              original instant in whole microseconds
    """
    instants = set()
    for subtitle in subtitles:
        instants.add(subtitle.start)
        instants.add(subtitle.end)

    ordered_instants = sorted(instants)
    node_of_instant = dict(
        (when, node) for node, when in enumerate(ordered_instants)
    )
    origin_us = dict(
        (node, _total_microseconds(when))
        for node, when in enumerate(ordered_instants)
    )

    boundary_node = {}
    for sub_index, subtitle in enumerate(subtitles):
        boundary_node[(sub_index, 0)] = node_of_instant[subtitle.start]
        boundary_node[(sub_index, 1)] = node_of_instant[subtitle.end]
    return boundary_node, origin_us


def _propagate_intervals(node_count, edges, pins, low_cap, high_cap):
    """
    Tighten integer intervals under difference constraints until stable.

    Every edge ``(start, end, gap)`` states ``end - start >= gap``; every pin
    fixes a node to one value. ``low_cap`` and ``high_cap`` give the outer
    domain of each node. On success the returned lower bounds form a feasible
    assignment, because each edge's lower bound is satisfied.

    :returns: ``(lower, upper)`` or ``None`` when the constraints are infeasible
    """
    lower = dict(low_cap)
    upper = dict(high_cap)
    for node, value in pins.items():
        lower[node] = max(lower[node], value)
        upper[node] = min(upper[node], value)

    # Edges follow temporal order, so the longest dependency chain is shorter
    # than the node count. A node count of passes therefore always reaches the
    # fixpoint; any remaining contradiction shows up as lower > upper below.
    for _ in range(node_count + 1):
        changed = False
        for start, end, gap in edges:
            if lower[start] + gap > lower[end]:
                lower[end] = lower[start] + gap
                changed = True
            if upper[end] != float("inf") and upper[end] - gap < upper[start]:
                upper[start] = upper[end] - gap
                changed = True
        for node, value in pins.items():
            if lower[node] != value or upper[node] != value:
                lower[node] = max(lower[node], value)
                upper[node] = min(upper[node], value)
                changed = True
        if not changed:
            break

    if any(lower[node] > upper[node] for node in range(node_count)):
        return None

    return lower, upper


def _raise_grid_conflict(conflict, boundaries, detail):
    raise TimestampQuantizationError(
        conflict,
        boundaries,
        "Cannot quantise subtitles to whole milliseconds: {}".format(detail),
    )


def quantize_to_milliseconds(
    subtitles,
    start_limit=None,
    end_limit=None,
    min_duration=None,
    in_place=False,
):
    r"""
    Snap subtitle timing onto the integer millisecond grid that SRT requires.

    Subtitle data is held at microsecond resolution, but an SRT timestamp can
    only name whole milliseconds. This function yields the same subtitles with
    ``start`` and ``end`` landing exactly on a millisecond, while honouring the
    constraints a batch exporter needs:

    - Boundaries that refer to the same instant -- including an end time shared
      with the next subtitle's start -- are merged onto exactly one grid
      position. They are never rounded to two neighbouring milliseconds.
    - Among all feasible grids, the one minimising the largest absolute rounding
      error against the original timing is chosen. Error is measured from the
      true (fractional) instant, not from a pre-rounded value.
    - ``start_limit`` and ``end_limit`` lock the opening boundary of the first
      subtitle and the closing boundary of the last subtitle to a fixed
      millisecond value.
    - ``min_duration`` guarantees every subtitle stays visible for at least that
      long on the grid. Every subtitle always keeps the 1 ms minimum an SRT
      block needs.

    Constraints are checked before output. If they cannot all be satisfied,
    :py:class:`TimestampQuantizationError` names the first conflicting pair of
    boundaries. A subtitle is never silently dropped to make the grid look
    valid.

    .. doctest::

        >>> from datetime import timedelta
        >>> subs = [
        ...     Subtitle(1, timedelta(seconds=1, microseconds=400),
        ...              timedelta(seconds=2, microseconds=600), 'a'),
        ... ]
        >>> quantized = list(quantize_to_milliseconds(subs))
        >>> quantized[0].start
        datetime.timedelta(seconds=1)
        >>> quantized[0].end
        datetime.timedelta(seconds=2, microseconds=1000)

    :param subtitles: :py:class:`Subtitle` objects, ideally in time order
    :type subtitles: iterable of :py:class:`Subtitle`
    :param start_limit: Fixed grid time, as a timedelta or whole milliseconds,
                        for the first subtitle's start, or ``None`` to leave it
                        free
    :param end_limit: Fixed grid time, as a timedelta or whole milliseconds,
                      for the last subtitle's end, or ``None`` to leave it free
    :param min_duration: Minimum on-screen time, as a timedelta or whole
                         milliseconds, for every subtitle, or ``None``
    :param bool in_place: Whether to adjust the input subtitles in place
    :returns: The same subtitles with millisecond-aligned ``start`` and ``end``
    :rtype: :term:`generator` of :py:class:`Subtitle` objects
    :raises TimestampQuantizationError: If the constraints are infeasible on
                                        the millisecond grid
    """
    ordered = sorted(subtitles)
    if not ordered:
        return

    if not in_place:
        ordered = [Subtitle(**vars(subtitle)) for subtitle in ordered]

    locked_start = None if start_limit is None else _as_milliseconds(start_limit)
    locked_end = None if end_limit is None else _as_milliseconds(end_limit)
    requested_minimum = 0 if min_duration is None else _as_milliseconds(min_duration)
    # The smallest valid SRT block spans one millisecond (end strictly after
    # start). A caller-supplied minimum can only raise this floor.
    minimum = max(1, requested_minimum)

    boundary_node, origin_us = _group_boundaries(ordered)
    node_count = len(origin_us)

    edges = []
    for sub_index, _subtitle in enumerate(ordered):
        start_node = boundary_node[(sub_index, 0)]
        end_node = boundary_node[(sub_index, 1)]
        if start_node == end_node:
            _raise_grid_conflict(
                "zero_length",
                [
                    _boundary_descriptor(sub_index, 0),
                    _boundary_descriptor(sub_index, 1),
                ],
                "subtitle %d starts and ends on the same boundary but an SRT "
                "subtitle must span at least %d ms" % (sub_index + 1, minimum),
            )
        edges.append((start_node, end_node, minimum))

    head_node = boundary_node[(0, 0)]
    tail_node = boundary_node[(len(ordered) - 1, 1)]

    pins = {}
    if locked_start is not None:
        pins[head_node] = locked_start
    if locked_end is not None:
        pins[tail_node] = locked_end
    if (
        locked_start is not None
        and locked_end is not None
        and locked_end < locked_start
    ):
        _raise_grid_conflict(
            "locked_order",
            [
                _boundary_descriptor(0, 0),
                _boundary_descriptor(len(ordered) - 1, 1),
            ],
            "locked end %d ms precedes locked start %d ms"
            % (locked_end, locked_start),
        )

    assignment = _solve_millisecond_grid(node_count, edges, pins, origin_us)

    for sub_index, subtitle in enumerate(ordered):
        start_ms = assignment[boundary_node[(sub_index, 0)]]
        end_ms = assignment[boundary_node[(sub_index, 1)]]
        subtitle.start = timedelta(milliseconds=start_ms)
        subtitle.end = timedelta(milliseconds=end_ms)
        yield subtitle


def _solve_millisecond_grid(node_count, edges, pins, origin_us):
    """
    Assign a feasible, non-negative whole-millisecond value to every node.

    The error budget is searched in whole microseconds: for budget ``b`` a node
    whose origin is ``O`` microseconds may take any integer millisecond ``x``
    with ``|x*1000 - O| <= b``. Feasibility is monotone in ``b``, so an integer
    binary search yields the exact minimax error without floating point.
    """
    infinity = float("inf")
    nodes = list(range(node_count))

    base_low = dict((node, 0) for node in nodes)
    base_high = dict((node, infinity) for node in nodes)
    base = _propagate_intervals(node_count, edges, pins, base_low, base_high)
    if base is None:
        _raise_first_conflict(nodes, edges, pins)
    feasible_lower, _upper = base

    def feasible_within(budget_us):
        low_cap = {}
        high_cap = {}
        for node in nodes:
            origin = origin_us[node]
            low_cap[node] = max(
                0, _ceil_div(origin - budget_us, MICROSECONDS_IN_MILLISECOND)
            )
            high_cap[node] = (origin + budget_us) // MICROSECONDS_IN_MILLISECOND
        return _propagate_intervals(node_count, edges, pins, low_cap, high_cap)

    # The zero-floor assignment is always feasible, so it bounds the search.
    max_budget = max(
        abs(feasible_lower[node] * MICROSECONDS_IN_MILLISECOND - origin_us[node])
        for node in nodes
    )

    lo_budget, hi_budget = 0, max_budget
    while lo_budget < hi_budget:
        mid_budget = (lo_budget + hi_budget) // 2
        if feasible_within(mid_budget) is None:
            lo_budget = mid_budget + 1
        else:
            hi_budget = mid_budget

    # Feasibility is monotone, so the smallest budget in range is feasible.
    return feasible_within(lo_budget)[0]


def _raise_first_conflict(nodes, edges, pins):
    """
    Localise the first boundary group that cannot satisfy the constraints.

    Feasibility fails only when an edge's required gap cannot be met, so the
    earliest such subtitle (in time order) is reported directly instead of
    failing the whole batch anonymously.
    """
    infinity = float("inf")
    earliest = dict((node, pins.get(node, 0)) for node in nodes)
    latest = dict((node, pins.get(node, infinity)) for node in nodes)

    for _ in range(len(nodes) + 1):
        changed = False
        for start, end, gap in edges:
            if earliest[start] + gap > earliest[end]:
                earliest[end] = earliest[start] + gap
                changed = True
        if not changed:
            break

    for _ in range(len(nodes) + 1):
        changed = False
        for start, end, gap in edges:
            if latest[end] != infinity and latest[end] - gap < latest[start]:
                latest[start] = latest[end] - gap
                changed = True
        if not changed:
            break

    for sub_index, (start_node, end_node, gap) in enumerate(edges):
        if latest[end_node] - earliest[start_node] < gap:
            required_end = earliest[start_node] + gap
            _raise_grid_conflict(
                "minimum_duration",
                [
                    _boundary_descriptor(sub_index, 0),
                    _boundary_descriptor(sub_index, 1),
                ],
                "subtitle %d is the first boundary group that cannot fit: its "
                "start cannot be earlier than %d ms, so its end would need to "
                "reach %d ms to span %d ms, but its end is bounded to at most "
                "%d ms"
                % (
                    sub_index + 1,
                    earliest[start_node],
                    required_end,
                    gap,
                    latest[end_node],
                ),
            )

    # Every infeasible system on this DAG violates at least one edge gap, so
    # this terminal raise is only reachable if the reasoning above is wrong.
    _raise_grid_conflict(  # pragma: no cover
        "boundary_order",
        [],
        "the ordering and lock constraints have no millisecond solution",
    )


def export(
    subtitles,
    start_limit=None,
    end_limit=None,
    min_duration=None,
    reindex=True,
    start_index=1,
    strict=True,
    eol=None,
    in_place=False,
):
    r"""
    Compose subtitles to an SRT string after locking them to whole milliseconds.

    This is :py:func:`compose` for callers whose source timing is finer than an
    SRT timestamp. Every boundary is quantised with
    :py:func:`quantize_to_milliseconds`, honouring optional head/tail locks and
    a minimum display duration, and only then rendered. If the constraints
    cannot all be met, :py:class:`TimestampQuantizationError` is raised and no
    subtitle is dropped to hide that fact.

    .. doctest::

        >>> from datetime import timedelta
        >>> subs = [
        ...     Subtitle(1, timedelta(milliseconds=10), timedelta(milliseconds=90),
        ...              'a'),
        ... ]
        >>> export(subs, start_limit=timedelta(0),
        ...        min_duration=timedelta(milliseconds=100))  # doctest: +ELLIPSIS
        '1\n00:00:00,000 --> 00:00:00,100\na\n\n'

    :param subtitles: :py:class:`Subtitle` objects to export
    :param start_limit: Fixed grid time for the first subtitle's start
    :param end_limit: Fixed grid time for the last subtitle's end
    :param min_duration: Minimum on-screen time for every subtitle
    :param bool reindex: Whether to reindex subtitles based on start time
    :param int start_index: If reindexing, the index to start reindexing from
    :param bool strict: Whether to enable strict content mode
    :param str eol: The end of line string to use (default "\\n")
    :param bool in_place: Whether to adjust subtitles in place
    :returns: A single SRT formatted string on the millisecond grid
    :rtype: str
    :raises TimestampQuantizationError: If the constraints are infeasible
    """
    quantized = quantize_to_milliseconds(
        subtitles,
        start_limit=start_limit,
        end_limit=end_limit,
        min_duration=min_duration,
        in_place=in_place,
    )
    return compose(
        quantized,
        reindex=reindex,
        start_index=start_index,
        strict=strict,
        eol=eol,
        in_place=in_place,
    )


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


class TimestampQuantizationError(ValueError):
    """
    Raised when subtitle timing cannot be represented on the integer
    millisecond grid without violating a requested constraint.

    This is never raised merely because a value had to be rounded -- rounding
    alone is always possible. It is raised only for genuine infeasibility, for
    example because two subtitles sharing a boundary cannot be kept ordered, or
    because honouring the minimum display duration would require moving a
    boundary past a locked boundary or past the start or end of the sequence.

    Subtitles are never silently dropped to make a grid "fit".

    :param conflict: A short, machine-readable token naming the kind of
                     constraint that could not be satisfied
    :param boundaries: The first boundary group that conflicts, as
                       ``(subtitle_number, "start"|"end")`` descriptors
    :param str message: A human readable description of the conflict
    """

    def __init__(self, conflict, boundaries, message):
        super(TimestampQuantizationError, self).__init__(message)
        self.conflict = conflict
        self.boundaries = list(boundaries)
        self.message = message


class _ShouldSkipException(Exception):
    """
    Raised when a subtitle should be skipped.
    """
