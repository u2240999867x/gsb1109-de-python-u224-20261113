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

Keeping paragraph breaks through strict export
-----------------------------------------------

Strict players reject blank lines inside a subtitle, but paragraph-based
retrieval systems rely on those same blank lines (and whitespace-only lines)
to split one subtitle into answers. :func:`srt.compose_strict_with_manifest`
writes a strictly legal SRT together with a manifest recording every removed
break. The manifest also keeps the exact anchor lines, including speaker
markers like ``ALICE:`` and whitespace-only lines, so the original structure
can be rebuilt later with :func:`srt.restore_paragraphs`.

.. code:: python

    >>> subtitles = list(srt.parse('''\
    ... 1
    ... 00:00:01,000 --> 00:00:02,000
    ... ALICE:
    ... Hi.
    ...
    ... BOB:
    ... Hello.
    ... ''')
    >>> strict_srt, manifest = srt.compose_strict_with_manifest(subtitles)
    >>> print(strict_srt)
    1
    00:00:01,000 --> 00:00:02,000
    ALICE:
    Hi.
    BOB:
    Hello.
    <BLANKLINE>

If the strict SRT is edited away from the original boundaries, those edits are
kept when the paragraphs are restored:

.. code:: python

    >>> edited = strict_srt.replace('Hello.', 'Hello there.')
    >>> restored, conflicts = srt.restore_paragraphs(
    ...     edited, manifest, on_conflict='skip'
    ... )
    >>> conflicts
    []
    >>> print(restored)
    1
    00:00:01,000 --> 00:00:02,000
    ALICE:
    Hi.
    <BLANKLINE>
    BOB:
    Hello there.
    <BLANKLINE>

If an original boundary line itself was deleted or rewritten beyond
recognition, :class:`srt.ParagraphRestoreError` identifies the conflicting
subtitle instead of guessing. The blank line is never inserted into another
subtitle — even one carrying the same speaker marker — so later subtitles stay
untouched.
