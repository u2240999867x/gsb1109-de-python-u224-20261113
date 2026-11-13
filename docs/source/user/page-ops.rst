.. _page-ops:

Page operations
===============

Start by :ref:`connecting <connecting>` to your site as described in the
:ref:`Connecting to your site <connecting>` section.

.. code-block:: python

    from mwclient import Site

    user_agent = 'MyCoolTool/0.2 (xyz@example.org)'
    site = Site('en.wikipedia.org', clients_useragent=user_agent)

For information about authenticating, please see
:ref:`the section on authenticating <auth>`.

Editing or creating a page
--------------------------

To get the content of a specific page:

    >>> page = site.pages['Greater guinea pig']
    >>> text = page.text()

If a page doesn't exist, :meth:`Page.text() <mwclient.page.Page.text>`
just returns an empty string. If you need to test the existence of the
page, use `page.exists`:

    >>> page.exists
    True

Edit the text as you like before saving it back to the wiki:

    >>> page.edit(text, 'Edit summary')

If the page didn't exist, this operation will create it.

Listing page revisions
----------------------

:meth:`Page.revisions() <mwclient.page.Page.revisions>` returns a List object
that you can iterate over using a for loop. Continuation
is handled under the hood so you don't have to worry about it.

*Example:* Let's find out which users did the most number of edits to a page:

    >>> users = [rev['user'] for rev in page.revisions()]
    >>> unique_users = set(users)
    >>> user_revisions = [{'user': user, 'count': users.count(user)} for user in unique_users]
    >>> sorted(user_revisions, key=lambda x: x['count'], reverse=True)[:5]
    [{'count': 6, 'user': 'Wolf12345'},
     {'count': 4, 'user': 'Test-bot'},
     {'count': 4, 'user': 'Mirxaeth'},
     {'count': 3, 'user': '192.251.192.201'},
     {'count': 3, 'user': '78.50.51.180'}]

*Tip:* If you want to retrieve a specific number of revisions, the
:code:`itertools.islice` method can come in handy:

    >>> from datetime import datetime
    >>> from time import mktime
    >>> from itertools import islice
    >>> for revision in islice(page.revisions(), 5):
    ...     dt = datetime.fromtimestamp(mktime(revision['timestamp']))
    ...     print(dt.strftime('%F %T'))

Outdated links and sections
---------------------------

A legacy link can point at a section of a page that has since been renamed:

    >>> result = site.resolve_link('Old name#Material')
    >>> result.status
    'resolved'
    >>> result.target
    'New entry#Material'
    >>> result.text
    '== Material ==\nMaterial text.\n'

The original link remains available as ``result.original``.  Redirects are
followed as reported by the wiki, including a fragment added by the redirect
itself; if the redirect names no fragment, the link's fragment is kept.

Only the referenced section is fetched: ``result.text`` never silently
contains the whole page.  Repeated headings are handled as well.  If a page
contains two sections titled ``Material``, ``Old name#Material`` is ambiguous
and ``result.candidates`` (plus ``result.texts``) holds every section that
shares that displayed heading.  The second one is addressed by MediaWiki's
numbered anchor::

    >>> result = site.resolve_link('Entry#Material_2')
    >>> result.status
    'section_numbered'
    >>> result.section.index
    3

The status also distinguishes a target section that no longer exists
(``section_deleted``), a missing page (``page_missing``), an invalid link
(``invalid``) and a redirect cycle (``redirect_loop``), so those outcomes do
not have to be guessed from empty text.

Categories
----------

Categories can be retrieved in the same way as pages, but you can also use
:meth:`Site.categories() <mwclient.client.Site.categories>` and skip the namespace prefix.
The returned :class:`Category <mwclient.listing.Category>` object
supports the same methods as the :class:`Page <mwclient.page.Page>`
object, but also provides an extra function, :meth:`members() <mwclient.listing.Category.members>`,
to list all members of a category.

The Category object can also be used itself as an iterator to yield all its members:

    >>> category = site.categories['Python']
    >>> for page in category:
    >>>     print(page.name)

Other page operations
---------------------

There are many other page operations like
:meth:`backlinks() <mwclient.page.Page.backlinks>`,
:meth:`embeddedin() <mwclient.page.Page.embeddedin>`,
etc. See the :class:`API reference <mwclient.page.Page>` for more.
