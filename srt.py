#!/usr/bin/env python
# coding=utf8

"""A tiny library for parsing, modifying, and composing SRT files."""

from __future__ import unicode_literals
import difflib
import functools
import json
import re
import unicodedata
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

try:
    FILE_TYPES = (file, io.IOBase)  # pytype: disable=name-error
except NameError:  # `file` doesn't exist in Python 3
    FILE_TYPES = (io.IOBase,)

try:  # Python 2
    STRING_TYPES = (str, unicode)  # pytype: disable=name-error
except NameError:  # `unicode` doesn't exist in Python 3
    STRING_TYPES = (str,)


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


def _clone_subtitle(subtitle):
    """
    Copy a subtitle, preserving its concrete type where possible.

    ``Subtitle`` subclasses such as :py:class:`Utterance` carry extra
    attributes, so reconstructing them as plain ``Subtitle`` objects would
    lose data.
    """
    try:
        return type(subtitle)(**vars(subtitle))
    except TypeError:
        return Subtitle(**vars(subtitle))


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
            subtitle = _clone_subtitle(subtitle)

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


DEFAULT_MAX_GAP = timedelta(milliseconds=1000)
DEFAULT_MIN_SHARED = 2
DEFAULT_MIN_COVERAGE = 0.6
PROVISIONAL_MARKER = " 〔未定稿〕"


def _significant_char(char):
    """
    Whether a character carries textual meaning for the purpose of comparing
    rolling caption snapshots.

    Whitespace, punctuation, and symbols are ignored when looking for
    carry-over evidence, so that "下午，三点" and "下午三点" still overlap.
    """
    return unicodedata.category(char)[0] in ("L", "N", "M")


def _normalise_text(text):
    """
    Return a comparison form of ``text`` and a map from each normalised
    character back to its position in the original text.

    This lets us match case-insensitively and punctuation-insensitively while
    still emitting the original spelling in revisions and output. Runs of
    whitespace collapse to a single space so that line breaks in multi-line
    captions do not make words run together.
    """
    normalised = []
    positions = []
    in_whitespace = False

    for position, char in enumerate(text):
        if _significant_char(char):
            normalised.append(char.lower())
            positions.append(position)
            in_whitespace = False
        elif char.isspace():
            if not in_whitespace:
                normalised.append(" ")
                positions.append(position)
                in_whitespace = True

    return "".join(normalised), positions


def _original_span(text, positions, start, end):
    """
    Map a half-open normalised span back to the original text.

    Only the span's own characters are returned, so a revision reads as the
    words that actually changed rather than duplicating adjacent punctuation.
    """
    if start >= end:
        return ""

    return text[positions[start] : positions[end - 1] + 1].strip()


def _diff_changes(before_text, after_text):
    """
    Describe how ``after_text`` differs from ``before_text``.

    Changes are expressed against the original spelling, not the normalised
    comparison form. An unchanged snapshot produces a single ``stable``
    change so that every observation remains visible in the revision ledger.
    """
    if before_text == after_text:
        return [CaptionChange("stable")]

    if not before_text:
        return [CaptionChange("append", added=after_text)]

    if not after_text:
        return [CaptionChange("retract", removed=before_text)]

    before_norm, before_pos = _normalise_text(before_text)
    after_norm, after_pos = _normalise_text(after_text)

    if before_norm == after_norm:
        # Only punctuation or whitespace changed. Keep the new spelling but
        # record it, rather than reporting an empty revision.
        return [CaptionChange("punctuate", removed=before_text, added=after_text)]

    matcher = difflib.SequenceMatcher(
        None, before_norm, after_norm, autojunk=False
    )
    changes = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        removed = _original_span(before_text, before_pos, i1, i2)
        added = _original_span(after_text, after_pos, j1, j2)

        if tag == "delete":
            kind = "retract" if i2 == len(before_norm) else "delete"
        elif tag == "insert":
            if j1 == 0:
                kind = "prepend"
            elif j2 == len(after_norm):
                kind = "append"
            else:
                kind = "insert"
        else:
            kind = "replace"

        changes.append(CaptionChange(kind, removed=removed, added=added))

    return changes or [CaptionChange("stable")]


def _join_head_and_tail(head, tail):
    """Join a retained head to a newly scrolled-in tail."""
    if not head:
        return tail
    if not tail:
        return head

    # Avoid "发车。，快点" when punctuation from both slices meets at the seam.
    if head.rstrip() and head.rstrip()[-1] in "，,。.！!？?；;：:":
        tail = tail.lstrip(" \t，,。.！!？?；;：:")

    return head + tail


def _transition(before_text, after_text, min_shared, min_coverage):
    """
    Find carry-over evidence between two rolling caption snapshots.

    Evidence requires both a meaningful block of shared text and that block
    being anchored at an edge of each snapshot (as happens when a rolling
    window scrolls, grows, or is corrected). A merely similar sentence with
    unrelated text on both sides does not qualify.

    Returns ``(merged_text, anchor)`` or ``None`` when there is insufficient
    evidence of continuation.
    """
    before_norm = _normalise_text(before_text)[0]
    after_norm, after_pos = _normalise_text(after_text)

    if not before_norm or not after_norm:
        return None

    matcher = difflib.SequenceMatcher(None, before_norm, after_norm, autojunk=False)
    blocks = matcher.get_matching_blocks()
    total_shared = sum(block.size for block in blocks)

    anchor = matcher.find_longest_match(0, len(before_norm), 0, len(after_norm))

    if before_norm == after_norm:
        # Equality alone is not carry-over evidence: two people can say the
        # same sentence one after the other. The caller decides whether this
        # is a refresh of the same on-screen caption (overlapping times) or a
        # genuine second utterance (sequential times).
        return None

    # A prefix in either direction is the strongest evidence: the snapshot
    # either extends the sentence or takes words back from its tail.
    if after_norm.startswith(before_norm) or before_norm.startswith(after_norm):
        return after_text, anchor

    if anchor.size < min_shared:
        return None

    coverage = total_shared / float(min(len(before_norm), len(after_norm)))
    if coverage < min_coverage:
        return None

    before_edge = anchor.a == 0 or anchor.a + anchor.size == len(before_norm)
    after_edge = anchor.b == 0 or anchor.b + anchor.size == len(after_norm)
    if not (before_edge and after_edge):
        return None

    # Otherwise this is a same-slot correction. Demand a closer match than
    # for growth, so that two different sentences that merely share an opening
    # phrase are not collapsed into one.
    if coverage < max(min_coverage, 0.75):
        return None

    before_head = before_norm[: anchor.a]
    after_head = after_norm[: anchor.b]
    after_tail = after_norm[anchor.b + anchor.size :]

    # A rolling window normally drops old text from its head. That is not a
    # retraction: the words were still spoken. Retain that head and append
    # only the genuinely new tail from the new snapshot.
    #
    # The anchor must cover most of the new snapshot for this to be a scroll
    # rather than a correction that happens to reuse an opening phrase.
    anchor_covers_after = anchor.size >= min_coverage * len(after_norm)
    if before_head and not after_head and anchor_covers_after:
        if after_tail:
            tail_start = after_pos[anchor.b + anchor.size]
            merged = _join_head_and_tail(before_text, after_text[tail_start:])
        else:
            # Pure scroll: no new words, keep what has already been said.
            merged = before_text
        return merged, anchor

    return after_text, anchor


class CaptionChange(object):
    """
    A single textual change observed in a rolling caption snapshot.

    :param str kind: ``append``, ``prepend``, ``insert``, ``delete``,
                     ``retract``, ``replace``, ``punctuate``, or ``stable``
    :param str removed: Text removed by this change
    :param str added: Text added by this change
    """

    def __init__(self, kind, removed="", added=""):
        self.kind = kind
        self.removed = removed
        self.added = added

    def __repr__(self):
        return "CaptionChange(kind=%r, removed=%r, added=%r)" % (
            self.kind,
            self.removed,
            self.added,
        )

    def to_dict(self):
        return {
            "kind": self.kind,
            "removed": self.removed,
            "added": self.added,
        }


class CaptionRevision(object):
    """
    One observation of an utterance in the rolling caption stream.

    Revisions retain both sides of every change, so word retractions and
    corrections are auditable even after the final text has moved on.

    :param int snapshot: The original subtitle index (or 0 if absent)
    :param start: Start time of this snapshot
    :type start: :py:class:`datetime.timedelta`
    :param end: End time of this snapshot
    :type end: :py:class:`datetime.timedelta`
    :param str before: Text of the utterance before this snapshot
    :param str after: Text of the utterance after this snapshot
    :param changes: Changes made by this snapshot
    :type changes: list of :py:class:`CaptionChange`
    """

    def __init__(self, snapshot, start, end, before, after, changes):
        self.snapshot = snapshot
        self.start = start
        self.end = end
        self.before = before
        self.after = after
        self.changes = changes

    @property
    def changed(self):
        return self.before != self.after

    @property
    def removed(self):
        return "".join(change.removed for change in self.changes)

    @property
    def added(self):
        return "".join(change.added for change in self.changes)

    @property
    def kind(self):
        kinds = []
        for change in self.changes:
            if change.kind not in kinds:
                kinds.append(change.kind)
        return "+".join(kinds)

    @property
    def history(self):
        return self.changes

    def __repr__(self):
        return "CaptionRevision(snapshot=%r, kind=%r, before=%r, after=%r)" % (
            self.snapshot,
            self.kind,
            self.before,
            self.after,
        )

    def to_dict(self):
        return {
            "snapshot": self.snapshot,
            "start": timedelta_to_srt_timestamp(self.start),
            "end": timedelta_to_srt_timestamp(self.end),
            "kind": self.kind,
            "changed": self.changed,
            "before": self.before,
            "after": self.after,
            "removed": self.removed,
            "added": self.added,
            "changes": [change.to_dict() for change in self.changes],
        }


class Utterance(Subtitle):
    """
    A reconstructed spoken increment.

    This is a :py:class:`Subtitle` subclass, so it can be composed directly,
    with two additions: ``revisions`` holds the rolling-caption history and
    ``final`` says whether a later snapshot has confirmed the tail.

    The final utterance in a stream is normally provisional (``final`` is
    ``False``), because another snapshot may still extend or correct it.
    """

    def __init__(
        self,
        index,
        start,
        end,
        content,
        proprietary="",
        revisions=None,
        final=True,
    ):
        super(Utterance, self).__init__(
            index=index,
            start=start,
            end=end,
            content=content,
            proprietary=proprietary,
        )
        self.revisions = revisions or []
        self.final = final

    @property
    def text(self):
        return self.content

    @property
    def provisional(self):
        """Whether a later snapshot may still change this utterance."""
        return not self.final

    def __hash__(self):
        # revisions is a list, so the Subtitle implementation (which hashes
        # vars()) would raise TypeError here.
        return hash(
            (
                self.index,
                self.start,
                self.end,
                self.content,
                self.proprietary,
                self.final,
            )
        )

    def __eq__(self, other):
        return Subtitle.__eq__(self, other)

    def __repr__(self):
        return "Utterance(index=%r, content=%r, final=%r, revisions=%d)" % (
            self.index,
            self.content,
            self.final,
            len(self.revisions),
        )

    def to_srt(self, strict=True, eol="\n", provisional_marker=None):
        content = self.content
        if not self.final and provisional_marker:
            content += provisional_marker

        rendered = Subtitle(
            index=self.index,
            start=self.start,
            end=self.end,
            content=content,
            proprietary=self.proprietary,
        )
        return rendered.to_srt(strict=strict, eol=eol)

    def to_dict(self):
        return {
            "index": self.index,
            "text": self.content,
            "start": timedelta_to_srt_timestamp(self.start),
            "end": timedelta_to_srt_timestamp(self.end),
            "start_ms": int(
                self.start.total_seconds() * MICROSECONDS_IN_MILLISECOND
            ),
            "end_ms": int(self.end.total_seconds() * MICROSECONDS_IN_MILLISECOND),
            "final": self.final,
            "revisions": [revision.to_dict() for revision in self.revisions],
        }


class Reconstruction(list):
    """
    The result of reconstructing rolling captions.

    It is a list of :py:class:`Utterance` objects, with convenience accessors
    for the revision ledger and provisional entries.
    """

    @property
    def utterances(self):
        return self

    @property
    def unfinalized(self):
        return [utterance for utterance in self if not utterance.final]

    @property
    def provisional(self):
        return self.unfinalized

    @property
    def texts(self):
        return [utterance.content for utterance in self]

    @property
    def lines(self):
        return self.texts

    @property
    def ledger(self):
        return [utterance.to_dict() for utterance in self]

    def to_srt(self, provisional_marker=None, strict=True, eol="\n"):
        return "".join(
            utterance.to_srt(
                strict=strict, eol=eol, provisional_marker=provisional_marker
            )
            for utterance in self
        )

    def to_json(self, indent=2):
        import json

        return json.dumps(
            self.ledger, ensure_ascii=False, indent=indent, sort_keys=True
        )


class RollingCaptionReconstructor(object):
    """
    Incrementally reconstruct utterances from rolling caption snapshots.

    Snapshots are merged only when both conditions hold:

    * their times are continuous (``next.start <= previous.end + max_gap``),
      and
    * there is textual carry-over evidence (a shared block anchored at an edge
      of both snapshots, covering at least ``min_coverage`` of the shorter
      one).

    Identical sentences separated by a gap therefore remain separate
    utterances, while a growing or corrected rolling window becomes one
    utterance with a revision ledger.

    :param max_gap: Maximum allowed gap between consecutive snapshots
    :type max_gap: :py:class:`datetime.timedelta`
    :param int min_shared: Minimum length of the shared anchor in significant
                           characters
    :param float min_coverage: Minimum fraction of the shorter snapshot that
                               must be shared
    :param bool finalize_last: Mark the final utterance final on
                               :py:meth:`finish` (useful for complete files)
    """

    def __init__(
        self,
        max_gap=DEFAULT_MAX_GAP,
        min_shared=DEFAULT_MIN_SHARED,
        min_coverage=DEFAULT_MIN_COVERAGE,
        finalize_last=False,
    ):
        self.max_gap = max_gap
        self.min_shared = min_shared
        self.min_coverage = min_coverage
        self.finalize_last = finalize_last
        self._utterances = []
        self._current = None

    @property
    def utterances(self):
        return self._utterances

    @property
    def current(self):
        return self._current

    @property
    def result(self):
        return Reconstruction(self._utterances)

    def _start_utterance(self, subtitle):
        utterance = Utterance(
            index=len(self._utterances) + 1,
            start=subtitle.start,
            end=subtitle.end,
            content=subtitle.content,
            proprietary=subtitle.proprietary,
            final=False,
        )
        utterance.revisions.append(
            CaptionRevision(
                snapshot=subtitle.index or 0,
                start=subtitle.start,
                end=subtitle.end,
                before="",
                after=subtitle.content,
                changes=_diff_changes("", subtitle.content),
            )
        )
        self._utterances.append(utterance)
        self._current = utterance
        return utterance

    def _merge_snapshot(self, subtitle, merged_text):
        utterance = self._current
        before = utterance.content
        changes = _diff_changes(before, merged_text)
        utterance.content = merged_text
        utterance.end = max(utterance.end, subtitle.end)
        if subtitle.proprietary:
            utterance.proprietary = subtitle.proprietary
        utterance.revisions.append(
            CaptionRevision(
                snapshot=subtitle.index or 0,
                start=subtitle.start,
                end=subtitle.end,
                before=before,
                after=merged_text,
                changes=changes,
            )
        )
        return utterance

    def feed(self, subtitle):
        """
        Add one snapshot and return the utterance it belongs to.

        If the snapshot starts a new utterance, the previous one is marked
        final. The returned current utterance remains provisional until a
        later snapshot closes it.

        A blank snapshot clears the screen: it closes the current utterance
        without creating an empty one, but still records that the on-screen
        text was taken down.
        """
        if not subtitle.content.strip():
            if self._current is not None:
                current = self._current
                current.revisions.append(
                    CaptionRevision(
                        snapshot=subtitle.index or 0,
                        start=subtitle.start,
                        end=subtitle.end,
                        before=current.content,
                        after="",
                        changes=[CaptionChange("clear")],
                    )
                )
                current.final = True
                self._current = None
            return None

        if self._current is None:
            self._start_utterance(subtitle)
            return self._current

        continuous = subtitle.start <= self._current.end + self.max_gap
        refresh = subtitle.start < self._current.end
        same_text = _normalise_text(subtitle.content)[0] == _normalise_text(
            self._current.content
        )[0]
        merged_text = None

        if continuous:
            if same_text:
                # Identical text is only the same utterance when the caption
                # is simply being refreshed while still on screen. Two people
                # saying the same words in sequence remain two utterances.
                if refresh:
                    merged_text = subtitle.content
            else:
                transition = _transition(
                    self._current.content,
                    subtitle.content,
                    min_shared=self.min_shared,
                    min_coverage=self.min_coverage,
                )
                if transition is not None:
                    merged_text, _ = transition

        if merged_text is None:
            self._current.final = True
            self._start_utterance(subtitle)
        else:
            self._merge_snapshot(subtitle, merged_text)

        return self._current

    def finish(self):
        """
        Finish the stream and return a :py:class:`Reconstruction`.

        The last utterance stays provisional unless ``finalize_last`` was
        requested, since an unseen next snapshot could still change it.
        """
        if self.finalize_last and self._current is not None:
            self._current.final = True
        return self.result


def reconstruct_increments(
    subtitles,
    max_gap=DEFAULT_MAX_GAP,
    min_shared=DEFAULT_MIN_SHARED,
    min_coverage=DEFAULT_MIN_COVERAGE,
    finalize_last=False,
):
    """
    Reconstruct spoken increments from timestamped rolling caption snapshots.

    .. code::

        >>> from datetime import timedelta
        >>> snapshots = [
        ...     Subtitle(1, timedelta(seconds=0), timedelta(seconds=1), "下午"),
        ...     Subtitle(2, timedelta(seconds=1), timedelta(seconds=2), "下午三点"),
        ...     Subtitle(3, timedelta(seconds=2), timedelta(seconds=3), "下午三点发车"),
        ... ]
        >>> result = reconstruct_increments(snapshots)
        >>> [utterance.content for utterance in result]
        ['下午三点发车']
        >>> result[-1].final
        False

    :param subtitles: Rolling caption snapshots in :py:class:`Subtitle` form,
                      or SRT text, or a file-like object
    :param max_gap: Maximum gap between snapshots that still counts as
                    continuous
    :type max_gap: :py:class:`datetime.timedelta`
    :param int min_shared: Minimum shared anchor length
    :param float min_coverage: Minimum shared fraction of the shorter snapshot
    :param bool finalize_last: Whether to mark the final utterance final
    :returns: Reconstructed utterances and their revision ledgers
    :rtype: :py:class:`Reconstruction`
    """
    if isinstance(subtitles, FILE_TYPES) or isinstance(subtitles, STRING_TYPES):
        subtitles = parse(subtitles)

    reconstructor = RollingCaptionReconstructor(
        max_gap=max_gap,
        min_shared=min_shared,
        min_coverage=min_coverage,
        finalize_last=finalize_last,
    )

    ordered = sorted(
        subtitles,
        key=lambda subtitle: (subtitle.start, subtitle.end, subtitle.index or 0),
    )

    for subtitle in ordered:
        reconstructor.feed(subtitle)

    return reconstructor.finish()


# Descriptive aliases for callers who think in terms of captions rather than
# increments.
reconstruct_utterances = reconstruct_increments
merge_rolling_subtitles = reconstruct_increments
merge_rolling_captions = reconstruct_increments
deduplicate_rolling = reconstruct_increments
RollingReconstructor = RollingCaptionReconstructor
IncrementalCaptionReconstructor = RollingCaptionReconstructor


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
