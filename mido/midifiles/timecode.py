# SPDX-FileCopyrightText: 2026 Mido contributors
#
# SPDX-License-Identifier: MIT

"""SMPTE time code and reel address conversion.

A SMPTE time code labels a frame with a wall clock position::

    hours:minutes:seconds:frames

The *frame rate* of a label and the rate at which frames are actually
recorded are not always the same.  The NTSC derived rates 23.976, 29.97
and 59.94 fps are nominally 24, 30 and 60 fps but run slightly slower.
Drop-frame numbering compensates by skipping frame numbers at the start
of most minutes; it is defined for 29.97 and 59.94 fps.  Those skipped
labels never exist on tape and cannot be rounded to a nearby frame.

This module converts in both directions using exact rational numbers:

* :func:`frames_to_timecode` -- continuous frame count to label
* :func:`timecode_to_frames` -- label to continuous frame count

:class:`Timecode` is a label that also carries a sub-frame position and
its source frame rate.  :class:`ReelTable` maps labels of one or more
reels to a single continuous frame count, including midnight wrap and
gaps in the numbering of a single reel.

The 29.97, 23.976 and 59.94 rates are interpreted as their exact NTSC
values (30000/1001, 24000/1001 and 60000/1001), not as the decimal
number that happens to be written down.
"""

import re
from bisect import bisect_right
from fractions import Fraction

__all__ = [
    'DroppedFrameError',
    'InvalidTimecodeError',
    'Reel',
    'ReelTable',
    'Timecode',
    'TimecodeError',
    'TimecodeGapError',
    'TimecodeTable',
    'frames2tc',
    'drop_frames_per_minute',
    'format_timecode',
    'frame_rate_fraction',
    'frames_per_day',
    'frames_to_timecode',
    'is_drop_frame_rate',
    'midnight_wrap',
    'nominal_frame_rate',
    'parse_timecode',
    'seconds_to_timecode',
    'smpte_offset_to_frames',
    'frames_to_smpte_offset',
    'tc2frames',
    'timecode_to_frames',
    'timecode_to_seconds',
]

# Exact NTSC rates.  Written as decimal strings because that is how the
# rates are named in the SMPTE world.
_NTSC_RATES = {
    '23.976': Fraction(24000, 1001),
    '29.97': Fraction(30000, 1001),
    '59.94': Fraction(60000, 1001),
}

# How many frames are skipped at the start of a non-tenth minute when
# drop-frame numbering is used.  Only these rates define drop-frame.
_DROP_FRAMES = {
    Fraction(30000, 1001): 2,
    Fraction(60000, 1001): 4,
}

_SUBFRAMES_PER_FRAME = 100

_TIMECODE_RE = re.compile(
    r'^\s*'
    r'(?P<hours>\d{1,2})\s*:\s*'
    r'(?P<minutes>\d{1,2})\s*:\s*'
    r'(?P<seconds>\d{1,2})'
    r'\s*(?P<separator>[:;.])\s*'
    r'(?P<frames>\d{1,3})'
    r'(?:\s*[.,]\s*(?P<subframes>\d{1,2}))?'
    r'\s*$')


class TimecodeError(ValueError):
    """Base class for time code conversion errors."""


class InvalidTimecodeError(TimecodeError):
    """A time code label that does not exist.

    ``parts`` is the offending ``(hours, minutes, seconds, frames)``
    tuple so callers can point at the exact position that is missing.
    ``label`` is the same position formatted as ``HH:MM:SS:FF``.
    """

    def __init__(self, parts, message=None):
        self.parts = tuple(parts)
        self.label = format_timecode(self.parts)
        if message is None:
            message = f'time code {self.label} does not exist'
        super().__init__(message)


class DroppedFrameError(InvalidTimecodeError):
    """A label that drop-frame numbering skips."""

    def __init__(self, parts, frame_rate, drop_frames):
        self.frame_rate = frame_rate
        self.drop_frames = drop_frames
        self.first_dropped = 0
        self.last_dropped = drop_frames - 1
        super().__init__(
            parts,
            'time code {} does not exist: frame numbers '
            '{:02d}..{:02d} are dropped at the start of minute {:d} '
            '({:g} fps drop-frame)'.format(
                format_timecode(parts), self.first_dropped,
                self.last_dropped, parts[1], float(frame_rate)))


class TimecodeGapError(InvalidTimecodeError):
    """A label that lies in a numbering gap of a reel."""

    def __init__(self, parts, reel, frame_rate):
        self.reel = reel
        self.frame_rate = frame_rate
        super().__init__(
            parts,
            'time code {} does not belong to reel {!r}: it falls in a '
            'gap in the numbering ({:g} fps)'.format(
                format_timecode(parts), reel, float(frame_rate)))


def _clamp_zero(value):
    # A tiny negative float would otherwise turn into a huge positive
    # integer when passed through Fraction(str(value)).
    return 0 if value == 0 else value


def _match_ntsc(rate):
    """Return the exact NTSC rate for a decimal approximation.

    ``29.97``, ``29.97002997`` and ``30 / 1.001`` all mean the same
    broadcast rate.  The comparison is deliberately tight; rates that
    are merely close (for example 30.0) are not affected.
    """
    for exact in _NTSC_RATES.values():
        if abs(float(rate) - float(exact)) < 1e-3:
            return exact
    return None


def frame_rate_fraction(frame_rate):
    """Return *frame_rate* as an exact :class:`~fractions.Fraction`.

    Accepts a number, a :class:`~fractions.Fraction` or one of the
    string names ``'23.976'``, ``'24'``, ``'25'``, ``'29.97'``,
    ``'30'`` and ``'59.94'``.

    The decimal NTSC names are converted to their exact rational values
    (``29.97`` becomes ``30000/1001``), because that is the rate at
    which the frames are really recorded.
    """
    if isinstance(frame_rate, str):
        text = frame_rate.strip()
        if text in _NTSC_RATES:
            return _NTSC_RATES[text]
        try:
            rate = Fraction(text)
        except (ValueError, ZeroDivisionError) as err:
            raise ValueError(
                f'invalid frame rate {frame_rate!r}') from err
    elif isinstance(frame_rate, Fraction):
        rate = frame_rate
    else:
        try:
            rate = Fraction(str(_clamp_zero(frame_rate)))
        except (TypeError, ValueError, ZeroDivisionError) as err:
            raise TypeError('frame_rate must be a number') from err

    if rate <= 0:
        raise ValueError('frame_rate must be positive')

    ntsc = _match_ntsc(rate)
    if ntsc is not None:
        return ntsc
    return rate


def nominal_frame_rate(frame_rate):
    """Return the integer rate used to write labels.

    The nominal rate is the number of frame *labels* per second: 30 for
    29.97 fps, 60 for 59.94 fps and so on.
    """
    rate = frame_rate_fraction(frame_rate)
    if rate in _NTSC_RATES.values():
        # 24000/1001 -> 24, 30000/1001 -> 30, 60000/1001 -> 60.
        return int(rate) + 1
    if rate.denominator != 1:
        raise ValueError(
            f'{float(rate):g} fps is not a supported frame rate')
    return int(rate)


def is_drop_frame_rate(frame_rate):
    """Return True if drop-frame numbering exists for *frame_rate*.

    This only says whether the rate *can* use drop-frame numbering.  To
    select it explicitly pass ``drop_frame=True`` to the conversion
    functions.
    """
    return frame_rate_fraction(frame_rate) in _DROP_FRAMES


def drop_frames_per_minute(frame_rate, drop_frame=None):
    """Return how many frame numbers drop-frame numbering skips.

    Returns 0 for non-drop rates.  If ``drop_frame`` is None the
    default is drop-frame for the NTSC rates that define it.
    """
    rate = frame_rate_fraction(frame_rate)
    drop = _DROP_FRAMES.get(rate, 0)
    if drop_frame is None:
        return drop
    if drop_frame and not drop:
        raise ValueError(
            f'drop-frame numbering is not defined for {float(rate):g} fps')
    return drop if drop_frame else 0


def _resolve_drop_frame(rate, drop_frame):
    if drop_frame is None:
        return rate in _DROP_FRAMES
    if drop_frame and rate not in _DROP_FRAMES:
        raise ValueError(
            f'drop-frame numbering is not defined for {float(rate):g} fps')
    return bool(drop_frame)


def _frames_per_10_minutes(nominal, drop):
    return nominal * 600 - drop * 9


def _frames_per_minute(nominal, drop):
    return nominal * 60 - drop


def frames_per_day(frame_rate, drop_frame=None):
    """Return the number of frames in 24 hours.

    This is the count at which a continuous frame number wraps back to
    ``00:00:00:00``.
    """
    rate = frame_rate_fraction(frame_rate)
    drop = drop_frames_per_minute(rate, drop_frame)
    nominal = nominal_frame_rate(rate)
    # Nine minutes of every ten drop `drop` labels, 24 hours a day.
    return nominal * 60 * 60 * 24 - drop * 9 * 6 * 24


def _label_frame_number(parts, nominal, drop):
    hours, minutes, seconds, frames = parts
    total_minutes = 60 * hours + minutes
    return (nominal * (3600 * hours + 60 * minutes + seconds) + frames
            - drop * (total_minutes - total_minutes // 10))


def _frame_number_label(frame_number, nominal, drop):
    """Turn a frame number in ``0..frames_per_day`` into a label."""
    if drop:
        tens, remainder = divmod(
            frame_number, _frames_per_10_minutes(nominal, drop))
        if remainder < drop:
            # The first frame after a dropped block keeps the label
            # immediately following the previous existent label.
            adjusted = frame_number + drop * 9 * tens
        else:
            adjusted = (frame_number + drop * 9 * tens
                        + drop * ((remainder - drop)
                                  // _frames_per_minute(nominal, drop)))
    else:
        adjusted = frame_number

    hours, remainder = divmod(adjusted, nominal * 3600)
    minutes, remainder = divmod(remainder, nominal * 60)
    seconds, frames = divmod(remainder, nominal)
    return hours, minutes, seconds, frames


def _check_parts(parts, nominal, drop, frame_rate=None):
    hours, minutes, seconds, frames = parts
    if hours < 0 or minutes < 0 or seconds < 0 or frames < 0:
        raise InvalidTimecodeError(
            parts, 'time code {} has a negative field'.format(
                format_timecode(parts)))
    if hours > 23:
        raise InvalidTimecodeError(
            parts, 'time code {} has hours out of range'.format(
                format_timecode(parts)))
    if minutes > 59:
        raise InvalidTimecodeError(
            parts, 'time code {} has minutes out of range'.format(
                format_timecode(parts)))
    if seconds > 59:
        raise InvalidTimecodeError(
            parts, 'time code {} has seconds out of range'.format(
                format_timecode(parts)))
    if frames >= nominal:
        raise InvalidTimecodeError(
            parts,
            'time code {} does not exist: frame {:02d} is out of range '
            'for {:d} fps labels'.format(
                format_timecode(parts), frames, nominal))
    if (drop and seconds == 0 and minutes % 10 != 0 and frames < drop):
        if frame_rate is None:
            frame_rate = nominal
        raise DroppedFrameError(parts, frame_rate, drop)


def parse_timecode(timecode):
    """Parse a SMPTE label into ``(hours, minutes, seconds, frames)``.

    String labels may use ``:``, ``;`` or ``.`` before the frame field,
    for example ``'01:02:03:04'`` or ``'01:02:03;04'``.  A sub-frame
    suffix such as ``'01:02:03:04.50'`` is accepted but not returned
    here; use :meth:`Timecode.parse` to keep it.
    """
    return _parse_timecode(timecode)[0]


def _parse_timecode(timecode):
    """Return (parts, sub_frames, drop_hint)."""
    if isinstance(timecode, Timecode):
        return timecode.parts, timecode.sub_frames, timecode.drop_frame

    if isinstance(timecode, str):
        match = _TIMECODE_RE.match(timecode)
        if match is None:
            raise ValueError(f'invalid time code {timecode!r}')
        parts = (int(match.group('hours')),
                 int(match.group('minutes')),
                 int(match.group('seconds')),
                 int(match.group('frames')))
        sub_frames = int(match.group('subframes') or 0)
        drop_hint = match.group('separator') == ';'
        if sub_frames >= _SUBFRAMES_PER_FRAME:
            raise ValueError(
                f'sub-frames must be in range 0..99, got {sub_frames}')
        return parts, sub_frames, drop_hint

    try:
        values = tuple(timecode)
    except TypeError as err:
        raise TypeError('timecode must be a string or a sequence') from err

    if len(values) != 4:
        raise ValueError(
            'timecode must have four fields: hours, minutes, seconds, '
            'frames')

    parts = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError('timecode fields must be integers')
        if value < 0:
            raise ValueError('timecode fields must be non-negative')
        parts.append(value)
    return tuple(parts), 0, False


def format_timecode(timecode, drop_frame=None, sub_frames=None):
    """Format ``(hours, minutes, seconds, frames)`` as ``HH:MM:SS:FF``.

    A :class:`Timecode` is formatted with its own separator, sub-frames
    and reel unless those arguments are overridden.  For tuples, pass
    ``drop_frame=True`` to use ``;`` before the frame field and
    ``sub_frames`` to append ``.SS``.
    """
    if isinstance(timecode, Timecode):
        if drop_frame is None:
            drop_frame = timecode.drop_frame
        if sub_frames is None:
            sub_frames = timecode.sub_frames
        parts = timecode.parts
    else:
        parts = parse_timecode(timecode)
    hours, minutes, seconds, frames = parts
    separator = ';' if drop_frame else ':'
    label = (f'{hours:02d}:{minutes:02d}:{seconds:02d}'
             f'{separator}{frames:02d}')
    if sub_frames:
        label += f'.{sub_frames:02d}'
    return label


def frames_to_timecode(frames, frame_rate, drop_frame=None, wrap=True):
    """Convert a continuous frame number to a SMPTE label.

    ``frames`` is a 0-based count.  Returns ``(hours, minutes, seconds,
    frames)``.

    ``drop_frame`` defaults to True for 29.97 and 59.94 fps and False
    otherwise.  Pass ``drop_frame=False`` explicitly for non-drop
    material at those rates.

    With ``wrap=True`` (the default) the count wraps at midnight.  With
    ``wrap=False`` negative counts raise :class:`ValueError` and hours
    may exceed 23.
    """
    rate = frame_rate_fraction(frame_rate)
    drop = drop_frames_per_minute(rate, drop_frame)
    nominal = nominal_frame_rate(rate)

    if isinstance(frames, bool) or not isinstance(frames, int):
        raise TypeError('frames must be an int')
    if frames < 0 and not wrap:
        raise ValueError('frames must be non-negative')
    if wrap:
        # Modulo also normalizes negative counts.
        frames %= frames_per_day(rate, drop)

    return _frame_number_label(frames, nominal, drop)


def timecode_to_frames(timecode, frame_rate, drop_frame=None):
    """Convert a SMPTE label to a continuous frame number.

    ``timecode`` can be a string or a sequence of four integers.

    Drop-frame labels that were skipped raise
    :class:`DroppedFrameError`.  Other impossible labels (seconds or
    frames out of range) raise :class:`InvalidTimecodeError`.  Both
    exceptions carry the exact offending position in ``parts``.
    """
    rate = frame_rate_fraction(frame_rate)
    drop = drop_frames_per_minute(rate, drop_frame)
    nominal = nominal_frame_rate(rate)

    parts, _, drop_hint = _parse_timecode(timecode)
    if drop_frame is None and drop_hint and rate in _DROP_FRAMES:
        drop = drop_frames_per_minute(rate, True)

    _check_parts(parts, nominal, drop, frame_rate=rate)
    number = _label_frame_number(parts, nominal, drop)
    return number % frames_per_day(rate, drop)

def midnight_wrap(frames, frame_rate, drop_frame=None):
    """Wrap a frame count into ``0..frames_per_day - 1``.

    This is the frame equivalent of a time code passing midnight.
    """
    rate = frame_rate_fraction(frame_rate)
    drop = drop_frames_per_minute(rate, drop_frame)
    return frames % frames_per_day(rate, drop)


def smpte_offset_to_frames(message, drop_frame=None):
    """Convert a ``smpte_offset`` meta message to a frame count.

    The message's ``frame_rate`` selects the numbering.  It does not
    record whether 29.97 material is drop-frame, so pass
    ``drop_frame=False`` for non-drop material at those rates.
    """
    if getattr(message, 'type', None) != 'smpte_offset':
        raise TypeError('message must be a smpte_offset meta message')
    return timecode_to_frames(
        (message.hours, message.minutes, message.seconds, message.frames),
        message.frame_rate, drop_frame=drop_frame)


def frames_to_smpte_offset(frames, frame_rate=30, drop_frame=None,
                           time=0):
    """Build a ``smpte_offset`` meta message from a frame count.

    Only 24, 25, 29.97 and 30 fps can be stored in a MIDI file.  If
    *frames* is a :class:`Timecode` its label, rate and sub-frames are
    used directly.
    """
    if isinstance(frames, Timecode):
        label = frames
    else:
        label = Timecode.from_frames(frames, frame_rate,
                                     drop_frame=drop_frame)
    return label.to_meta_message(time=time)


def timecode_to_seconds(timecode, frame_rate, drop_frame=None):
    """Return the real elapsed seconds for a SMPTE label."""
    rate = frame_rate_fraction(frame_rate)
    parts, sub_frames, _ = _parse_timecode(timecode)
    frames = timecode_to_frames(parts, rate, drop_frame=drop_frame)
    return float((frames + Fraction(sub_frames, _SUBFRAMES_PER_FRAME))
                 / rate)


def seconds_to_timecode(seconds, frame_rate, drop_frame=None):
    """Convert real elapsed seconds to a :class:`Timecode`."""
    rate = frame_rate_fraction(frame_rate)
    if seconds < 0:
        raise ValueError('seconds must be non-negative')
    position = Fraction(str(seconds)) * rate
    return Timecode.from_position(
        position, rate, drop_frame=drop_frame)


class Timecode:
    """A SMPTE label with sub-frames, frame rate and optional reel.

    Instances are immutable.  Equality includes the reel, frame rate,
    numbering scheme and sub-frames, which makes it suitable for
    round-trip checks.
    """

    __slots__ = ('hours', 'minutes', 'seconds', 'frames', 'sub_frames',
                 'frame_rate', 'drop_frame', 'reel')

    def __init__(self, hours, minutes, seconds, frames, sub_frames=0,
                 frame_rate=30, drop_frame=None, reel=None):
        parts = (hours, minutes, seconds, frames)
        for value in parts:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError('timecode fields must be integers')
        rate = frame_rate_fraction(frame_rate)
        drop = _resolve_drop_frame(rate, drop_frame)
        _check_parts(parts, nominal_frame_rate(rate),
                     drop_frames_per_minute(rate, drop), frame_rate=rate)
        if (isinstance(sub_frames, bool) or not isinstance(sub_frames, int)
                or not 0 <= sub_frames < _SUBFRAMES_PER_FRAME):
            raise ValueError('sub_frames must be an integer in range 0..99')

        object.__setattr__(self, 'hours', hours)
        object.__setattr__(self, 'minutes', minutes)
        object.__setattr__(self, 'seconds', seconds)
        object.__setattr__(self, 'frames', frames)
        object.__setattr__(self, 'sub_frames', sub_frames)
        object.__setattr__(self, 'frame_rate', rate)
        object.__setattr__(self, 'drop_frame', drop)
        object.__setattr__(self, 'reel', reel)

    @classmethod
    def parse(cls, timecode, frame_rate=30, drop_frame=None, reel=None):
        """Parse a label, keeping sub-frames and a ``;`` drop hint."""
        parts, sub_frames, drop_hint = _parse_timecode(timecode)
        rate = frame_rate_fraction(frame_rate)
        if drop_frame is None and drop_hint and rate in _DROP_FRAMES:
            drop_frame = True
        return cls(*parts, sub_frames=sub_frames, frame_rate=frame_rate,
                   drop_frame=drop_frame, reel=reel)

    @classmethod
    def from_frames(cls, frames, frame_rate=30, drop_frame=None, reel=None,
                    sub_frames=0):
        """Build a label from a whole-frame count."""
        parts = frames_to_timecode(frames, frame_rate,
                                   drop_frame=drop_frame, wrap=True)
        return cls(*parts, sub_frames=sub_frames, frame_rate=frame_rate,
                   drop_frame=drop_frame, reel=reel)

    @classmethod
    def from_position(cls, position, frame_rate=30, drop_frame=None,
                      reel=None):
        """Build a label from a (possibly fractional) frame position.

        The fractional part is interpreted as sub-frames (1/100 frame).
        Values that cannot be represented exactly are rounded to the
        nearest sub-frame.
        """
        position = Fraction(position)
        if position < 0:
            raise ValueError('position must be non-negative')
        whole = position.numerator // position.denominator
        sub = int(round((position - whole) * _SUBFRAMES_PER_FRAME))
        if sub >= _SUBFRAMES_PER_FRAME:
            whole += 1
            sub = 0
        return cls.from_frames(whole, frame_rate, drop_frame=drop_frame,
                               reel=reel, sub_frames=sub)

    @classmethod
    def from_smpte_offset(cls, message, drop_frame=None, reel=None):
        """Create a label from a ``smpte_offset`` meta message.

        The message carries the rate and the start position of a track.
        It does not record whether 29.97 material is drop-frame, so
        pass ``drop_frame`` when that matters.
        """
        if getattr(message, 'type', None) != 'smpte_offset':
            raise TypeError('message must be a smpte_offset meta message')
        rate = frame_rate_fraction(message.frame_rate)
        return cls(message.hours, message.minutes, message.seconds,
                   message.frames, sub_frames=message.sub_frames,
                   frame_rate=rate, drop_frame=drop_frame, reel=reel)

    def to_meta_message(self, time=0):
        """Return a ``smpte_offset`` meta message for this label."""
        from .meta import MetaMessage

        rate = self.frame_rate
        meta_rates = {
            Fraction(24): 24,
            Fraction(25): 25,
            Fraction(30000, 1001): 29.97,
            Fraction(30): 30,
        }
        if rate not in meta_rates:
            raise ValueError(
                f'{float(rate):g} fps cannot be stored in a SMPTE offset')
        return MetaMessage('smpte_offset',
                           frame_rate=meta_rates[rate],
                           hours=self.hours,
                           minutes=self.minutes,
                           seconds=self.seconds,
                           frames=self.frames,
                           sub_frames=self.sub_frames,
                           time=time)

    @property
    def parts(self):
        """The ``(hours, minutes, seconds, frames)`` tuple."""
        return self.hours, self.minutes, self.seconds, self.frames

    @property
    def frame_number(self):
        """The whole-frame count from midnight."""
        return timecode_to_frames(self.parts, self.frame_rate,
                                  drop_frame=self.drop_frame)

    @property
    def position(self):
        """The exact frame position including sub-frames."""
        return (Fraction(self.frame_number)
                + Fraction(self.sub_frames, _SUBFRAMES_PER_FRAME))

    def with_reel(self, reel):
        """Return a copy with another reel name."""
        return Timecode(*self.parts, sub_frames=self.sub_frames,
                        frame_rate=self.frame_rate,
                        drop_frame=self.drop_frame, reel=reel)

    def __setattr__(self, name, value):
        raise AttributeError('Timecode is immutable')

    def __eq__(self, other):
        if not isinstance(other, Timecode):
            return NotImplemented
        return (self.reel, self.parts, self.sub_frames, self.frame_rate,
                self.drop_frame) == (
                    other.reel, other.parts, other.sub_frames,
                    other.frame_rate, other.drop_frame)

    def __hash__(self):
        return hash((self.reel, self.parts, self.sub_frames,
                     self.frame_rate, self.drop_frame))

    def __repr__(self):
        rate = 'Fraction({}, {})'.format(self.frame_rate.numerator,
                                         self.frame_rate.denominator)
        return ('Timecode({}, {}, {}, {}, sub_frames={}, frame_rate={}, '
                'drop_frame={!r}, reel={!r})'.format(
                    self.hours, self.minutes, self.seconds, self.frames,
                    self.sub_frames, rate, self.drop_frame, self.reel))

    def __str__(self):
        label = format_timecode(self.parts, self.drop_frame,
                                self.sub_frames)
        return f'{self.reel}:{label}' if self.reel is not None else label


class Reel:
    """A physical reel and the source time codes recorded on it.

    A reel is one or more *segments*.  Each segment is a contiguous
    range of source labels that maps to contiguous frames.  When the
    numbering jumps between segments, the labels inside the jump are a
    gap (*broken numbering*) in this reel: they do not exist on it.

    ``segments`` is a sequence of ``(start_label, frame_count)`` pairs.
    For a reel without discontinuities pass ``start`` and
    ``frame_count`` instead.
    """

    __slots__ = ('name', 'segments')

    def __init__(self, name, start=None, frame_count=None, segments=None):
        if segments is not None:
            if start is not None or frame_count is not None:
                raise ValueError(
                    'pass either segments or start/frame_count, not both')
            items = list(segments)
        else:
            if start is None:
                raise ValueError('start is required')
            items = [(start, frame_count)]

        if not items:
            raise ValueError('a reel needs at least one segment')

        checked = []
        for item in items:
            try:
                seg_start, seg_count = item
            except (TypeError, ValueError) as err:
                raise TypeError(
                    'each segment must be a (start, frame_count) pair'
                ) from err
            if (isinstance(seg_count, bool)
                    or not isinstance(seg_count, int) or seg_count <= 0):
                raise ValueError('frame_count must be a positive integer')
            checked.append((seg_start, seg_count))

        object.__setattr__(self, 'name', name)
        object.__setattr__(self, 'segments', tuple(checked))

    @property
    def frame_count(self):
        """The number of frames in all segments."""
        return sum(count for _, count in self.segments)

    @property
    def start(self):
        """The label at which the first segment starts."""
        return self.segments[0][0]

    def __setattr__(self, name, value):
        raise AttributeError('Reel is immutable')

    def __eq__(self, other):
        if not isinstance(other, Reel):
            return NotImplemented
        return self.name == other.name and self.segments == other.segments

    def __hash__(self):
        return hash((self.name, self.segments))

    def __repr__(self):
        if len(self.segments) == 1:
            start, count = self.segments[0]
            return f'Reel({self.name!r}, {start!r}, {count})'
        return f'Reel({self.name!r}, segments={self.segments!r})'


class _Segment:
    """A reel segment placed on the continuous timeline.

    ``offset`` is the continuous frame at which the segment starts.
    ``source_frame`` is the source label's frame number at that point.
    """

    __slots__ = ('reel', 'offset', 'count', 'source_frame')

    def __init__(self, reel, offset, count, source_frame):
        self.reel = reel
        self.offset = offset
        self.count = count
        self.source_frame = source_frame

    @property
    def end(self):
        return self.offset + self.count


class ReelTable:
    """Address several reels by one continuous frame count.

    ``reels`` is a sequence of :class:`Reel` objects in table order.
    Frames are laid out head to tail: the first frame of the first reel
    is continuous frame 0, and every segment follows the previous one
    even when its source labels jump.

    ``frame_rate`` may be a :data:`FRAME_RATES` name or a number.
    ``drop_frame`` defaults to True for 29.97/59.94 fps.
    """

    def __init__(self, reels, frame_rate=30, drop_frame=None):
        self.frame_rate = frame_rate_fraction(frame_rate)
        self.drop_frame = _resolve_drop_frame(self.frame_rate,
                                              drop_frame)
        self._nominal = nominal_frame_rate(self.frame_rate)
        self._drop = drop_frames_per_minute(self.frame_rate,
                                            self.drop_frame)
        self._day = frames_per_day(self.frame_rate, self.drop_frame)

        self._reels = list(reels)
        for reel in self._reels:
            if not isinstance(reel, Reel):
                raise TypeError('reels must be Reel instances')
        names = [reel.name for reel in self._reels]
        if len(set(names)) != len(names):
            raise ValueError('reel names must be unique')
        self._by_name = {reel.name: reel for reel in self._reels}

        self._segments = []
        offset = 0
        for reel in self._reels:
            for start, count in reel.segments:
                parts, sub_frames, _ = _parse_timecode(start)
                if sub_frames:
                    raise ValueError(
                        f'reel segment start {start!r} must not have '
                        'sub-frames')
                _check_parts(parts, self._nominal, self._drop,
                             frame_rate=self.frame_rate)
                if count > self._day:
                    raise ValueError(
                        'a reel segment cannot be longer than one day')
                source_frame = _label_frame_number(
                    parts, self._nominal, self._drop)
                self._segments.append(_Segment(
                    reel.name, offset, count, source_frame))
                offset += count
        self._frame_count = offset
        self._offsets = [segment.offset for segment in self._segments]

    @classmethod
    def from_starts(cls, starts, frame_rate=30, drop_frame=None):
        """Build a table from reel start labels.

        ``starts`` is a ``{reel_name: start_label}`` mapping or a
        sequence of ``(reel_name, start_label)`` pairs.  Each reel runs
        until the next reel's start and the last runs until midnight.
        """
        rate = frame_rate_fraction(frame_rate)
        drop = _resolve_drop_frame(rate, drop_frame)
        nominal = nominal_frame_rate(rate)
        drop_count = drop_frames_per_minute(rate, drop)
        day = frames_per_day(rate, drop)

        items = list(starts.items() if hasattr(starts, 'items') else starts)
        if not items:
            raise ValueError('starts must not be empty')

        parsed = []
        for name, start in items:
            parts = parse_timecode(start)
            _check_parts(parts, nominal, drop_count, frame_rate=rate)
            parsed.append((name, start,
                           _label_frame_number(parts, nominal, drop_count)))

        reels = []
        for index, (name, start, start_frame) in enumerate(parsed):
            if index + 1 < len(parsed):
                length = (parsed[index + 1][2] - start_frame) % day
            else:
                length = (day - start_frame) % day
            if length == 0:
                length = day
            reels.append(Reel(name, start, frame_count=length))
        return cls(reels, frame_rate=rate, drop_frame=drop_frame)

    @property
    def reels(self):
        """The reels in table order."""
        return tuple(self._reels)

    @property
    def frame_count(self):
        """The total number of frames in the table."""
        return self._frame_count

    def __len__(self):
        return len(self._reels)

    def __iter__(self):
        return iter(self._reels)

    def reel(self, name):
        """Return the reel called *name*."""
        try:
            return self._by_name[name]
        except KeyError as ke:
            raise KeyError(f'unknown reel {name!r}') from ke

    def _segment_for(self, reel, label_frame):
        """Return (segment, local offset) for a source frame number."""
        local_base = 0
        for segment in self._segments:
            if segment.reel != reel:
                continue
            local = (label_frame - segment.source_frame) % self._day
            if local < segment.count:
                return segment, local
            local_base += segment.count
        return None, None

    def to_frames(self, reel, timecode):
        """Convert a source label on *reel* to a continuous position.

        Returns a :class:`~fractions.Fraction`; it is an integer unless
        the label has sub-frames.

        Raises :class:`DroppedFrameError` for labels skipped by
        drop-frame numbering, :class:`InvalidTimecodeError` for labels
        that cannot exist at the declared rate, and
        :class:`TimecodeGapError` for labels inside a numbering gap of
        the reel.
        """
        parts, sub_frames, _ = _parse_timecode(timecode)
        self.reel(reel)
        _check_parts(parts, self._nominal, self._drop,
                     frame_rate=self.frame_rate)
        label_frame = _label_frame_number(parts, self._nominal, self._drop)
        segment, local = self._segment_for(reel, label_frame)
        if segment is None:
            raise TimecodeGapError(parts, reel, self.frame_rate)
        position = (Fraction(segment.offset + local)
                    + Fraction(sub_frames, _SUBFRAMES_PER_FRAME))
        if position.denominator == 1:
            return position.numerator
        return position

    def to_frame_count(self, reel, timecode):
        """Like :meth:`to_frames`, truncated to a whole frame."""
        position = self.to_frames(reel, timecode)
        return position.numerator // position.denominator

    def from_frames(self, position):
        """Convert a continuous position to ``(reel, Timecode)``."""
        position = Fraction(position)
        if position < 0:
            raise ValueError('position must be non-negative')
        whole = position.numerator // position.denominator
        fraction = position - whole
        if whole >= self._frame_count:
            raise KeyError(position)

        index = bisect_right(self._offsets, whole) - 1
        segment = self._segments[index]
        local = whole - segment.offset
        source_frame = (segment.source_frame + local) % self._day
        parts = _frame_number_label(source_frame, self._nominal, self._drop)

        sub_frames = int(round(fraction * _SUBFRAMES_PER_FRAME))
        if sub_frames >= _SUBFRAMES_PER_FRAME:
            # Rounding pushed us onto the next frame.
            if whole + 1 >= self._frame_count:
                sub_frames = _SUBFRAMES_PER_FRAME - 1
            else:
                return self.from_frames(Fraction(whole + 1))

        timecode = Timecode(*parts, sub_frames=sub_frames,
                            frame_rate=self.frame_rate,
                            drop_frame=self.drop_frame, reel=segment.reel)
        return segment.reel, timecode

    def roundtrip(self, reel, timecode, sub_frames=None):
        """Convert *timecode* to frames and back.

        Returns the restored :class:`Timecode`.  It has the same reel,
        label, frame rate and sub-frames as the input, which is the
        fidelity guarantee for an event that is fully determined by a
        source time code.
        """
        if sub_frames is not None:
            parts = parse_timecode(timecode)
            timecode = format_timecode(parts, self.drop_frame, sub_frames)
        return self.from_frames(self.to_frames(reel, timecode))[1]


#: Alias: a reel table is also a time code table covering several reels.
TimecodeTable = ReelTable


# Short aliases for interactive use.  ``tc2frames`` in particular
# mirrors the existing ``tick2second`` / ``second2tick`` naming.
tc2frames = timecode_to_frames
frames2tc = frames_to_timecode
