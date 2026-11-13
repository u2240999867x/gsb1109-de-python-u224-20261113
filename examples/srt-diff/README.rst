srt diff reproducible example
==============================

These two files use whole-second timestamps on purpose so every figure in the
report can be recomputed by hand.

Run the comparison from the repository root::

    PYTHONPATH=. python srt_tools/srt-diff \
        -i examples/srt-diff/old.srt \
        -i examples/srt-diff/new.srt

Add ``--json`` for machine-readable output, and swap the two ``-i`` arguments
to check symmetry::

    PYTHONPATH=. python srt_tools/srt-diff \
        -i examples/srt-diff/new.srt \
        -i examples/srt-diff/old.srt

What each part of the sample exercises
--------------------------------------

- 00:00-00:10: identical subtitles, expected ``equal`` for 10 seconds.
- 00:12-00:25: one old subtitle is split into two new ones, with the sentence
  boundary moved. The words are unchanged, so it stays ``equal`` (13 seconds)
  instead of being reported as a text edit.
- 00:27-00:40: the wording is actually changed ("quick brown fox" versus
  "lazy red dog"), expected ``different`` for 13 seconds.
- 00:42-00:46: text only in the old file, ``only_a`` for 4 seconds.
- 00:46-00:50: text only in the new file, ``only_b`` for 4 seconds.
- 00:52-01:04: "Repeat me" is shown twice in the old file but once in the new
  file. The timeline is still covered once, giving one ``equal`` interval of
  12 seconds rather than counting the duplicate twice.
- 01:12-01:28: the new file adds a second simultaneous speaker. The speaker
  columns show 1/1, then 1/2 between 01:16 and 01:20, then 1/1.

Hand-checked summary
--------------------

==============  ========  ==============================
kind            seconds   note
==============  ========  ==============================
equal           39.000    includes the re-split sentence
different       25.000    13s wording change + 12s speaker change
only_a           4.000    old-only region
only_b           4.000    new-only region
total diff      33.000    different + only_a + only_b
covered         72.000    union of all subtitle time
==============  ========  ==============================

After swapping the two inputs the totals for ``equal``, ``different`` and
``total diff`` stay at 39/25/33 seconds, while ``only_a`` and ``only_b``
exchange their 4-second regions and the speaker columns are reversed.
