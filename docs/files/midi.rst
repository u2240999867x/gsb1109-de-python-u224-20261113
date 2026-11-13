.. SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
..
.. SPDX-License-Identifier: CC-BY-4.0

Standard MIDI Files
===================

``MidiFile`` objects can be used to *read*, *write* and *play back* MIDI files.


Opening
-------

You can open a file with::

    from mido import MidiFile

    mid = MidiFile('song.mid')

.. note::

    :term:`SysEx` dumps such as patch data are often stored in ``SYX``
    files rather than MIDI files. If you get "MThd not found. Probably not a
    MIDI file" try ``mido.read_syx_file()``.
    (See :doc:`syx` for more.)

The ``tracks`` attribute is a list of tracks. Each track is a list of
messages and meta messages, with the ``time`` attribute of each
messages set to its delta time (in ticks). (See Tempo and Beat
Resolution below for more on delta times.)

To print out all messages in the file, you can do::

    for i, track in enumerate(mid.tracks):
        print('Track {}: {}'.format(i, track.name))
        for msg in track:
            print(msg)

The entire file is read into memory. Thus you can freely modify tracks
and messages and save the file back by calling the ``save()``
method. (More on this below.)


Iterating Over Messages
-----------------------

Iterating over a ``MidiFile`` object will generate all MIDI messages
in the file in playback order. The ``time`` attribute of each message
is the number of seconds since the last message or the start of the
file.

Meta messages will also be included. If you want to filter them out,
you can do::

    if msg.is_meta:
        ...

This makes it easy to play back a MIDI file on a port (though this simple
implementation is subject to time drift)::

    for msg in MidiFile('song.mid'):
        time.sleep(msg.time)
        if not msg.is_meta:
            port.send(msg)

This is so useful that there's a method for it::

    for msg in MidiFile('song.mid').play():
        port.send(msg)

This does the sleeping and filtering for you (while avoiding drift). If you
pass ``meta_messages=True`` you will also get meta messages. These **cannot**
be sent on ports, which is why they are ``off`` by default.



Creating a New File
-------------------

You can create a new file by calling ``MidiFile`` without the ``filename``
argument. The file can then be saved by calling the ``save()`` method::

    from mido import Message, MidiFile, MidiTrack

    mid = MidiFile()
    track = MidiTrack()
    mid.tracks.append(track)

    track.append(Message('program_change', program=12, time=0))
    track.append(Message('note_on', note=64, velocity=64, time=32))
    track.append(Message('note_off', note=64, velocity=127, time=32))

    mid.save('new_song.mid')

The ``MidiTrack`` class is a subclass of list, so you can use all the
usual methods.

All messages must be tagged with delta time (in ticks). (A delta time
is how long to wait before the next message.)

If there is no ``end_of_track`` message at the end of a track, one will
be written anyway.

A complete example can be found in ``examples/midifiles/``.

The ``save`` method takes either a filename (``str``) or, using the ``file``
keyword parameter, a file-like object such as an in-memory binary file (an
``io.BytesIO``). If you pass a file object, ``save`` does not close it.
Similarly, the ``MidiFile`` constructor can take either a filename, or
a file object by using the ``file`` keyword parameter. if you pass a file
object to ``MidiFile`` as a context manager, the file is not closed when
the context manager exits.
Examples can be found in ``test_midifiles2.py``.


File Types
----------

There are three types of MIDI files:

* type 0 (single track): all messages are saved in one track
* type 1 (synchronous): all tracks start at the same time
* type 2 (asynchronous): each track is independent of the others

When creating a new file, you can select type by passing the ``type``
keyword argument or by setting the ``type`` attribute::

   mid = MidiFile(type=2)
   mid.type = 1

Type 0 files must have exactly one track. A ``ValueError`` is raised
if you attempt to save a file with no tracks or with more than one
track.


Playback Length
---------------

You can get the total playback time in seconds by accessing the
``length`` property::

   mid.length

This is only supported for type 0 and 1 files. Accessing ``length`` on
a type 2 file will raise ``ValueError``, since it is impossible to
compute the playback time of an asynchronous file.


Meta Messages
-------------

Meta messages behave like normal messages and can be created in the
usual way, for example::

    >>> from mido import MetaMessage
    >>> MetaMessage('key_signature', key='C#', mode='major')
    MetaMessage('key_signature', key='C#', mode='major', time=0)

You can tell meta messages apart from normal messages with::

    if msg.is_meta:
        ...

or if you know the message type you can use the ``type`` attribute::

    if msg.type == 'key_signature':
        ...
    elif msg.type == 'note_on':
        ...

Meta messages **cannot** be sent on ports.

For a list of supported meta messages and their attributes, and also
how to implement new meta messages, see :doc:`../meta_message_types`.


About the Time Attribute
------------------------

The ``time`` attribute is used in several different ways:

* inside a track, it is delta time in ticks. This must be an integer.

* in messages yielded from ``play()``, it is delta time in seconds
  (time elapsed since the last yielded message)

* (only important to implementers) inside certain methods it is
  used for absolute time in ticks or seconds

.. todo: Review implementation to separate concerns and units into dedicated
         attributes.


Tempo and Time Resolution
-------------------------

.. image:: ../images/midi_time.svg

Timing in MIDI files is centered around ticks. Each message in a MIDI file has
a delta time, which tells how many ticks have passed since the last message.

A tick is the smallest unit of time in MIDI and remains fixed throughout the
song. Each quarter notes is divided into a certain number of ticks, often
referred as the resolution of the file or pulses per quarter note (PPQN). This
resolution is stored as ``ticks_per_beat`` in MidiFile objects.

The meaning of this ``ticks_per_beat`` in terms of absolute timing depends on
the tempo and time signature of the file.


MIDI Tempo vs. BPM
^^^^^^^^^^^^^^^^^^

Unlike music, tempo in MIDI is not given as beats per minute (BPM), but rather
in microseconds per quarter note, with a default tempo of 500000 microseconds
per quarter note. Given a default 4/4 time signature where a beat is exactly a
quarter note, this corresponds to 120 beats per minute.

In case of different time signatures, the length of a beat depends on the
denominator of the time signature. E.g. in 2/2 time signature a beat has a
length of a half note, i.e. two quarter notes. Thus the default MIDI tempo of
500000 corresponds to a beat length of 1 second which is 60 BPM.

The meta messages 'set_tempo' and 'time_signature' can be used to change
the tempo and time signature during a song, respectively.

You can use :py:func:`bpm2tempo` and :py:func:`tempo2bpm` to convert to and
from beats per minute. Note that :py:func:`tempo2bpm` may return a floating
point number.


Converting Between Ticks and Seconds
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To convert from MIDI time to absolute time in seconds, the tempo (either
in number of beats per minute (BPM) or microseconds per quarter note, see
`MIDI Tempo vs. BPM`_ above) and ticks per per quarter note have to be decided
upon.

You can use :py:func:`tick2second` and :py:func:`second2tick` to convert to
and from seconds and ticks. Note that integer rounding of the result might be
necessary because MIDI files require ticks to be integers.

If you have a lot of rounding errors you should increase the time resolution
with more ticks per quarter note, by setting MidiFile.ticks_per_beat to a
large number. Typical values range from 96 to 480 but some use even more ticks
per quarter note.


Converting Between SMPTE Time Code and Frames
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

MIDI files can carry an ``smpte_offset`` meta message that marks the
time code at which a track starts.  Use :py:func:`timecode_to_frames`
and :py:func:`frames_to_timecode` to convert between such a label and a
continuous frame count::

    >>> from mido import frames_to_timecode, timecode_to_frames
    >>> timecode_to_frames('00:10:00;00', '29.97')
    17982
    >>> frames_to_timecode(1800, '29.97')
    (0, 1, 0, 2)

The frame rate may be a number or one of the names ``'23.976'``,
``'24'``, ``'25'``, ``'29.97'``, ``'30'`` and ``'59.94'``.  NTSC rates
are handled as their exact rational values (30000/1001 for 29.97),
never as a rounded float, so long durations do not drift.

Drop-frame numbering is used automatically for 29.97 and 59.94 fps and
can be disabled with ``drop_frame=False``.  Labels that drop-frame
numbering skips (for example ``00:01:00;00``) raise
:py:class:`DroppedFrameError`, whose ``parts`` attribute holds the exact
position that does not exist::

    >>> timecode_to_frames('00:01:00;00', '29.97')
    Traceback (most recent call last):
    ...
    mido.midifiles.timecode.DroppedFrameError: time code 00:01:00:00 does not exist: frame numbers 00..01 are dropped at the start of minute 1 (29.97 fps drop-frame)

Frame counts wrap at midnight by default.  Pass ``wrap=False`` to let
the hours grow past 23 instead.

A studio typically works with several reels of material.  A
:py:class:`ReelTable` maps the source labels of each reel onto one
continuous frame count and back::

    >>> from mido import Reel, ReelTable
    >>> table = ReelTable.from_starts(
    ...     {'A': '00:00:00;00', 'B': '00:01:00;02'}, frame_rate='29.97')
    >>> table.roundtrip('A', '00:00:10;05')
    Timecode(0, 0, 10, 5, sub_frames=0, frame_rate=Fraction(30000, 1001), drop_frame=True, reel='A')
    >>> table.roundtrip('A', '00:00:10;05', sub_frames=25)
    Timecode(0, 0, 10, 5, sub_frames=25, frame_rate=Fraction(30000, 1001), drop_frame=True, reel='A')

A reel may also be built from several segments when its numbering is
broken; labels inside the jump are reported as numbering gaps::

    >>> reel = Reel('R', segments=[('00:00:00;00', 100),
    ...                            ('00:00:10;00', 100)])
    >>> table = ReelTable([reel], frame_rate='29.97')
    >>> table.to_frames('R', '00:00:05;00')
    Traceback (most recent call last):
    ...
    mido.midifiles.timecode.TimecodeGapError: time code 00:00:05:00 does not belong to reel 'R': it falls in a gap in the numbering (29.97 fps)

:py:class:`Timecode` carries a label together with its frame rate,
sub-frames and reel.  Equality includes all of them, which makes it
straightforward to verify a round trip::

    >>> table.roundtrip('R', '00:00:00;00') == \
    ...     table.roundtrip('R', '00:00:00;00')
    True
