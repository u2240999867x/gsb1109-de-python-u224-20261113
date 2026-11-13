#!/usr/bin/env python
# coding=utf8

"""A tiny library for parsing, modifying, and composing SRT files."""

from __future__ import unicode_literals
import difflib
import functools
import re
from datetime import timedelta
import logging
import io


LOG = logging.getLogger(__name__)

# A speaker marker is a content line consisting solely of a (possibly
# whitespace-prefixed) name followed by ":" or "：", for example "ALICE:" or
# "张三：". These lines delimit spoken paragraphs and must stay locatable when
# paragraph breaks are restored.
SPEAKER_MARKER_REGEX = re.compile(r"^\s*[^\s:：][^:：]{0,30}[:：]\s*$")

# Manifest format version, bumped if the on-disk record shape changes.
MANIFEST_VERSION = 1

# Minimum difflib line similarity at which an edited anchor is considered the
# same line edited in place rather than a removed boundary.
ANCHOR_SIMILARITY_THRESHOLD = 0.5

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

try:
    FILE_TYPES = (file, io.IOBase)  # pytype: disable=name-error
except NameError:  # `file` doesn't exist in Python 3
    FILE_TYPES = (io.IOBase,)

try:  # Python 2
    TEXT_TYPES = (str, unicode)  # noqa: F821  pytype: disable=name-error
except NameError:  # Python 3
    TEXT_TYPES = (str,)


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


def _gap_record(blank_line_count):
    """
    Build the manifest entry describing how two adjacent anchor lines were
    joined in the original content. ``blank_line_count`` is the number of
    empty lines found between the two anchors.
    """
    if blank_line_count:
        # One newline to terminate the anchor, plus one per blank line.
        return {"type": "blank", "eol": "\n" * (blank_line_count + 1)}
    return {"type": "newline", "eol": "\n"}


def make_legal_content_with_record(content):
    r"""
    Legalise content for strict SRT output while recording everything that was
    removed, so that the original paragraph structure can be reconstructed
    later from an edited copy of the strict output.

    The returned manifest record stores:

    * ``anchors``: every non-empty physical line kept by strict legalisation,
      in order. Whitespace-only lines and speaker marker lines (such as
      ``"ALICE:"``) are kept verbatim here, since strict mode does not remove
      them.
    * ``separators``: one entry per gap between two adjacent anchors, holding
      the exact newline run that joined them. ``blank`` entries mark paragraph
      boundaries collapsed by strict mode.
    * ``leading``/``trailing``: the exact newline runs stripped from the start
      and the end of the content.

    .. doctest::

        >>> legal, record = make_legal_content_with_record(
        ...     'ALICE:\nHello.\n\nBOB:\n   \nHi.\n'
        ... )
        >>> legal
        'ALICE:\nHello.\nBOB:\n   \nHi.'
        >>> record["anchors"]
        ['ALICE:', 'Hello.', 'BOB:', '   ', 'Hi.']
        >>> record["speaker_anchors"]
        [0, 2]
        >>> record["separators"][1]
        {'type': 'blank', 'eol': '\n\n'}
        >>> record["trailing"]
        '\n'

    :param str content: The content to make legal
    :returns: A tuple of the legalised content and a manifest record (or
              ``None`` when strict legalisation changed nothing)
    :rtype: tuple(str, dict or None)
    """
    if content and content[0] != "\n" and "\n\n" not in content:
        return content, None

    legal_content = make_legal_content(content)

    # Content made only of newlines has no anchors at all. Keep the exact
    # original text as the leading run so it can be put back verbatim.
    if not legal_content:
        return legal_content, {
            "anchors": [],
            "speaker_anchors": [],
            "whitespace_anchors": [],
            "leading": content,
            "trailing": "",
            "separators": [],
        }

    lines = content.split("\n")

    leading_count = 0
    while lines and lines[0] == "":
        leading_count += 1
        lines.pop(0)

    trailing_count = 0
    while lines and lines[-1] == "":
        trailing_count += 1
        lines.pop()

    anchors = []
    separators = []
    speaker_anchors = []
    whitespace_anchors = []
    blank_line_count = 0

    for line in lines:
        if line == "":
            blank_line_count += 1
            continue

        if anchors:
            separators.append(_gap_record(blank_line_count))
        blank_line_count = 0

        anchor_index = len(anchors)
        anchors.append(line)
        if line.isspace():
            whitespace_anchors.append(anchor_index)
        if SPEAKER_MARKER_REGEX.match(line):
            speaker_anchors.append(anchor_index)

    return legal_content, {
        "anchors": anchors,
        "speaker_anchors": speaker_anchors,
        "whitespace_anchors": whitespace_anchors,
        "leading": "\n" * leading_count,
        "trailing": "\n" * trailing_count,
        "separators": separators,
    }


def compose_strict_with_manifest(
    subtitles, reindex=True, start_index=1, eol=None, in_place=False
):
    r"""
    Compose subtitles into strict SRT output and a manifest describing the
    paragraph breaks that strict legalisation removed.

    The manifest is the rewrite record needed by :func:`restore_paragraphs`.
    It only contains entries for subtitles whose content was actually changed
    by legalisation. Manifest indexes always match the indexes rendered in the
    returned SRT output.

    .. doctest::

        >>> from datetime import timedelta
        >>> sub = Subtitle(
        ...     index=1,
        ...     start=timedelta(seconds=1),
        ...     end=timedelta(seconds=2),
        ...     content='ALICE:\nHi.\n\nBOB:\nHello.\n',
        ... )
        >>> strict_srt, manifest = compose_strict_with_manifest([sub])
        >>> '\\n\\nBOB' not in strict_srt
        True
        >>> manifest["records"][0]["index"]
        1
        >>> manifest["records"][0]["anchors"][0]
        'ALICE:'

    :param subtitles: :py:class:`Subtitle` objects in any order
    :param bool reindex: Whether to reindex subtitles based on start time
    :param int start_index: If reindexing, the index to start reindexing from
    :param str eol: The end of line string to use in the SRT output
    :param bool in_place: Whether to reindex subs in-place for performance
    :returns: A tuple of the strict SRT string and the manifest dict
    :rtype: tuple(str, dict)
    """
    if reindex:
        subtitles = sort_and_reindex(
            subtitles, start_index=start_index, in_place=in_place
        )

    records = []
    blocks = []

    for subtitle in subtitles:
        legal_content, record = make_legal_content_with_record(subtitle.content)
        if record is not None:
            record = dict(record)
            record["index"] = subtitle.index or 0
            records.append(record)

        legal_subtitle = Subtitle(
            index=subtitle.index,
            start=subtitle.start,
            end=subtitle.end,
            content=legal_content,
            proprietary=subtitle.proprietary,
        )
        blocks.append(legal_subtitle.to_srt(strict=True, eol=eol))

    manifest = {"version": MANIFEST_VERSION, "records": records}
    return "".join(blocks), manifest


def _locate_anchors(edited_lines, record):
    """
    Find each recorded anchor line in the (possibly edited) strict content,
    preserving line order.

    Exact matches are preferred so unrelated edits are never confused with a
    boundary change. If an anchor cannot be found exactly, a fuzzy line match
    above :data:`ANCHOR_SIMILARITY_THRESHOLD` is accepted as an in-place edit
    of the boundary line. Matching is scoped to this subtitle's lines only, so
    a break can never be inserted into a different subtitle.

    :raises ParagraphRestoreError: If an anchor cannot be located confidently
    """
    positions = []
    search_from = 0
    speaker_anchors = set(record.get("speaker_anchors", []))

    for anchor_index, anchor in enumerate(record["anchors"]):
        try:
            position = edited_lines.index(anchor, search_from)
        except ValueError:
            candidates = [
                (
                    difflib.SequenceMatcher(
                        None, anchor, edited_lines[candidate]
                    ).ratio(),
                    candidate,
                )
                for candidate in range(search_from, len(edited_lines))
            ]

            if not candidates:
                raise ParagraphRestoreError(
                    record.get("index"),
                    anchor,
                    anchor_index,
                    anchor_index in speaker_anchors,
                    "no lines remain in this subtitle after the anchor",
                )

            best_ratio, best_position = max(candidates)
            if best_ratio < ANCHOR_SIMILARITY_THRESHOLD:
                raise ParagraphRestoreError(
                    record.get("index"),
                    anchor,
                    anchor_index,
                    anchor_index in speaker_anchors,
                    "no remaining line matches the original boundary "
                    "(best similarity %.2f)" % best_ratio,
                )
            position = best_position
            LOG.info(
                "Matched edited anchor %r to %r (similarity %.2f)",
                anchor,
                edited_lines[position],
                best_ratio,
            )

        positions.append(position)
        search_from = position + 1

    return positions


def _restore_one_subtitle(subtitle, record):
    """
    Rebuild the original paragraph structure inside one subtitle while keeping
    any edits made to non-boundary text.
    """
    anchors = record["anchors"]

    if not anchors:
        if subtitle.content:
            raise ParagraphRestoreError(
                record.get("index"),
                "",
                0,
                False,
                "subtitle originally contained only blank lines but now "
                "has text content",
            )
        return record.get("leading", "") + record.get("trailing", "")

    edited_lines = subtitle.content.split("\n")
    positions = _locate_anchors(edited_lines, record)

    parts = [record.get("leading", "")]
    cursor = 0

    for anchor_index, position in enumerate(positions):
        # Keep lines an editor inserted before this anchor, joined normally.
        while cursor < position:
            parts.append(edited_lines[cursor])
            parts.append("\n")
            cursor += 1

        # Use the edited line text verbatim so unrelated edits are preserved.
        parts.append(edited_lines[position])
        cursor = position + 1

        if anchor_index < len(positions) - 1:
            parts.append(record["separators"][anchor_index]["eol"])

    # Trailing lines added by an editor are kept before the original suffix.
    while cursor < len(edited_lines):
        parts.append("\n")
        parts.append(edited_lines[cursor])
        cursor += 1

    parts.append(record.get("trailing", ""))
    return "".join(parts)


def restore_paragraphs(srt_content, manifest, on_conflict="raise", eol=None):
    r"""
    Restore paragraph breaks removed by strict legalisation into an edited
    strict SRT document, using the manifest produced by
    :func:`compose_strict_with_manifest`.

    Each manifest record is matched to a subtitle by its rendered index, and
    only inside that subtitle's own lines. Edits made away from the original
    paragraph boundaries are preserved. If an original boundary line can no
    longer be located, a :class:`ParagraphRestoreError` is raised (or collected
    when ``on_conflict`` is ``"skip"``) instead of guessing — the blank line is
    never inserted into another subtitle, including one with the same index or
    speaker marker.

    .. doctest::

        >>> from datetime import timedelta
        >>> original = Subtitle(
        ...     index=1,
        ...     start=timedelta(seconds=1),
        ...     end=timedelta(seconds=2),
        ...     content='ALICE:\nHi.\n\nBOB:\nHello.\n',
        ... )
        >>> strict_srt, manifest = compose_strict_with_manifest([original])
        >>> edited = strict_srt.replace('Hello.', 'Hello there.')
        >>> restored, conflicts = restore_paragraphs(
        ...     edited, manifest, on_conflict='skip'
        ... )
        >>> conflicts
        []
        >>> list(parse(restored))[0].content
        'ALICE:\nHi.\n\nBOB:\nHello there.\n'

    :param srt_content: Edited strict SRT text, or an iterable of
                        :py:class:`Subtitle` objects
    :param dict manifest: The manifest produced at strict export time
    :param str on_conflict: ``"raise"`` (default) to raise on the first
                            unlocatable boundary, or ``"skip"`` to leave that
                            subtitle untouched and collect the conflict
    :param str eol: The end of line string to use in the restored output
    :returns: The restored SRT string. With ``on_conflict="skip"``, a tuple of
              the SRT string and a list of :class:`ParagraphRestoreError`
    :rtype: str or tuple(str, list)
    :raises ParagraphRestoreError: If a paragraph boundary cannot be located
                                   and ``on_conflict`` is ``"raise"``
    :raises ValueError: If the manifest version is unsupported
    """
    if on_conflict not in ("raise", "skip"):
        raise ValueError("on_conflict must be 'raise' or 'skip'")

    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError(
            "Unsupported manifest version %r, expected %d"
            % (manifest.get("version"), MANIFEST_VERSION)
        )

    if isinstance(srt_content, TEXT_TYPES):
        subtitles = list(parse(srt_content))
    else:
        subtitles = [Subtitle(**vars(subtitle)) for subtitle in srt_content]

    subtitles_by_index = {}
    duplicate_indexes = set()
    for subtitle in subtitles:
        if subtitle.index in subtitles_by_index:
            duplicate_indexes.add(subtitle.index)
        subtitles_by_index[subtitle.index] = subtitle

    conflicts = []

    for record in manifest["records"]:
        index = record.get("index")
        conflict = None

        if index in duplicate_indexes:
            conflict = ParagraphRestoreError(
                index, "", 0, False, "duplicate subtitle index in edited SRT"
            )
        elif index not in subtitles_by_index:
            conflict = ParagraphRestoreError(
                index, "", 0, False, "subtitle index missing from edited SRT"
            )
        else:
            subtitle = subtitles_by_index[index]
            try:
                subtitle.content = _restore_one_subtitle(subtitle, record)
            except ParagraphRestoreError as thrown:
                conflict = thrown

        if conflict is not None:
            if on_conflict == "raise":
                raise conflict
            conflicts.append(conflict)

    output = compose(subtitles, reindex=False, strict=False, eol=eol)

    if on_conflict == "skip":
        return output, conflicts
    return output


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


class ParagraphRestoreError(Exception):
    """
    Raised when an original paragraph boundary recorded in a manifest cannot
    be located in the edited strict SRT document.

    The conflicting subtitle is left untouched, and no separator is inserted
    into any other subtitle — even one with the same index or the same speaker
    marker — so paragraph retrieval for later subtitles stays reliable.

    :param int index: The index of the subtitle whose boundary could not be
                      located (``None`` if the manifest record had none)
    :param str anchor: The original anchor line that could not be found
    :param int anchor_index: The position of ``anchor`` within the manifest's
                             anchor list
    :param bool speaker_anchor: Whether the missing anchor was a speaker
                                marker line
    :param str reason: A human readable explanation of the conflict
    """

    def __init__(self, index, anchor, anchor_index, speaker_anchor, reason):
        self.index = index
        self.anchor = anchor
        self.anchor_index = anchor_index
        self.speaker_anchor = speaker_anchor
        self.reason = reason

        message = (
            "Cannot restore paragraph boundary in subtitle index %r at "
            "anchor %r (anchor #%d%s): %s"
            % (
                index,
                anchor,
                anchor_index,
                ", speaker marker" if speaker_anchor else "",
                reason,
            )
        )
        super(ParagraphRestoreError, self).__init__(message)


class _ShouldSkipException(Exception):
    """
    Raised when a subtitle should be skipped.
    """
