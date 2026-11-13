Quickstart
==========

Parse an SRT to Python objects
------------------------------

.. code:: python

    >>> import srt
    >>> subtitle_generator = srt.parse('''\
    ... 1
    ... 00:31:37,894 --> 00:31:39,928
    ... OK, look, I think I have a plan here.
    ...
    ... 2
    ... 00:31:39,931 --> 00:31:41,931
    ... Using mainly spoons,
    ...
    ... 3
    ... 00:31:41,933 --> 00:31:43,435
    ... we dig a tunnel under the city and release it into the wild.
    ...
    ... ''')
    >>> subtitles = list(subtitle_generator)
    >>>
    >>> subtitles[0].start
    datetime.timedelta(0, 1897, 894000)
    >>> subtitles[1].content
    'Using mainly spoons,'

Compose an SRT from Python objects
----------------------------------

.. code:: python

    >>> print(srt.compose(subtitles))
    1
    00:31:37,894 --> 00:31:39,928
    OK, look, I think I have a plan here.
    <BLANKLINE>
    2
    00:31:39,931 --> 00:31:41,931
    Using mainly spoons,
    <BLANKLINE>
    3
    00:31:41,933 --> 00:31:43,435
    we dig a tunnel under the city and release it into the wild.
    <BLANKLINE>
    <BLANKLINE>

Export fine-grained timing to the millisecond grid
---------------------------------------------------

Subtitle data is stored at microsecond resolution, but an SRT timestamp can
only name whole milliseconds. Use :py:func:`srt.export` for a batch export that
quantises every boundary first. A cutpoint shared by two subtitles is kept on a
single position, the first start and last end can be locked, and every subtitle
can be given a minimum display duration.

.. code:: python

    >>> from datetime import timedelta
    >>> fine = [
    ...     srt.Subtitle(1, timedelta(microseconds=999), timedelta(microseconds=2501), 'a'),
    ... ]
    >>> print(srt.export(fine, min_duration=timedelta(milliseconds=2)))
    1
    00:00:00,001 --> 00:00:00,003
    a
    <BLANKLINE>
    <BLANKLINE>

When the locks and minimum duration cannot all be met,
:py:class:`srt.TimestampQuantizationError` is raised and names the first
boundary group that conflicts. Subtitles are never silently discarded to make
the output look valid.
