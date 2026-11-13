.. _wikistore:

Incremental ingestion (``wiki_store``)
======================================

:mod:`mwclient.wiki_store` turns a MediaWiki recentchanges feed into an
identity-aware, incrementally updated knowledge base. It answers two
different questions separately:

* **Which page is this?** Pages are keyed by their MediaWiki ``pageid``, so a
  move keeps one entity with one continuous history. A new page created later
  under a freed title gets a new ``pageid`` and a separate history.
* **What did this title point at?** Titles are stored as time-ranged
  assignments (``TitleInterval``) to pageids, including vacancy and the
  redirects left behind by moves.

A minimal daily sync looks like this::

    import mwclient
    from mwclient.wiki_store import PageStore

    site = mwclient.Site('example.org')
    store = PageStore.load('kb.json') if os.path.exists('kb.json') else PageStore()
    summary = store.sync(site)
    store.save('kb.json')

The first run needs an explicit start, e.g. ``store.sync(site,
start='2026-01-01T00:00:00Z')``; later runs resume from the stored watermark.

Replays and page identity
-------------------------

Incremental fetch windows overlap at their boundary, so the watermark row is
fetched twice. Every row is de-duplicated by its rcid (or a composite key
when the rcid is missing) before it touches the store. Replaying a move
notification therefore never creates a second entity or an extra title
interval.

When a page is moved from "Old Port" to "New Port" and someone later creates
a new page at "Old Port", the store keeps both histories::

    store.resolve_title('Old Port', day_before_move)  # -> first pageid
    store.resolve_title('Old Port', day_after_move)   # -> same pageid via redirect
    store.resolve_title('Old Port', day_recreated)    # -> new pageid

Moves that suppress the redirect mark the old title as *vacant*, which is a
known answer and is distinct from having no records at all.

Deletions versus unreadable content
-----------------------------------

A delete-log entry with no readable body is recorded as
``ReadState.DELETED``. If the body fetch instead fails with a
permission-style API error, or returns ``missing`` without any delete log on
record, the snapshot is recorded as ``ReadState.INACCESSIBLE``: the content
is temporarily unreadable, but deletion has not been confirmed. The two
states are never conflated.

Asking about a day
------------------

``PageStore.resolve_title(title, day)`` returns every title assignment
clamped to that day plus the exact sub-ranges for which no change record has
been ingested yet. A complete result explains who the title pointed at all
day; an incomplete one tells you precisely which interval of change history
is still owed. ``PageStore.page_at(title, timestamp)`` is the point-in-time
variant and raises ``GapError`` instead of guessing when coverage is
missing.
