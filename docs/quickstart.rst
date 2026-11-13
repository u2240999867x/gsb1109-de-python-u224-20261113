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

Reconstruct spoken increments from rolling captions
--------------------------------------------------

Live caption platforms often render a scrolling transcript as many overlapping
subtitles. What looks like "下午", then "下午三点", then "下午三点发车" is one
thing being said, not three.

.. code:: python

    >>> from datetime import timedelta
    >>> snapshots = [
    ...     srt.Subtitle(1, timedelta(0), timedelta(seconds=1), '下午'),
    ...     srt.Subtitle(2, timedelta(seconds=1), timedelta(seconds=2), '下午三点'),
    ...     srt.Subtitle(3, timedelta(seconds=2), timedelta(seconds=3), '下午三点发车'),
    ... ]
    >>> result = srt.reconstruct_increments(snapshots)
    >>> [utterance.content for utterance in result]
    ['下午三点发车']

Snapshots are merged only when their times are continuous *and* the text shows
carry-over evidence: one snapshot extends, retracts, scrolls, or closely
corrects the other. Two identical sentences spoken in sequence stay two
utterances, and unrelated lines are never fused just because they are adjacent.

Every observation is kept in a revision ledger, including words that were
later taken back:

.. code:: python

    >>> snapshots = [
    ...     srt.Subtitle(1, timedelta(0), timedelta(seconds=1), '下午三点发车'),
    ...     srt.Subtitle(2, timedelta(seconds=1), timedelta(seconds=2), '下午三点'),
    ... ]
    >>> result = srt.reconstruct_increments(snapshots)
    >>> revision = result[0].revisions[-1]
    >>> revision.kind, revision.removed, revision.after
    ('retract', '发车', '下午三点')

The last utterance is provisional, because a later snapshot could still
extend or correct it:

.. code:: python

    >>> result[-1].final
    False

Render provisional tails with a visible marker, or dump the whole ledger as
JSON:

.. code:: python

    >>> print(result.to_srt(provisional_marker=srt.PROVISIONAL_MARKER))
    1
    00:00:00,000 --> 00:00:02,000
    下午三点 〔未定稿〕
    <BLANKLINE>
    <BLANKLINE>

    >>> result.to_json()  # doctest: +ELLIPSIS
    '[...revisions...]'

Use :py:class:`srt.RollingCaptionReconstructor` to feed snapshots as they
arrive. ``feed()`` returns ``None`` when a blank snapshot clears the screen,
and the last utterance stays provisional until a later snapshot closes it.
``reconstruct_increments`` also accepts SRT text or a file-like object, and
the ``srt reconstruct`` command handles files. Pass ``finalize_last=True``
when the input is known to be complete and no provisional marker is wanted.
