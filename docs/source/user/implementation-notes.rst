.. _implementation-notes:

Implementation notes
====================

Most properties and generators accept the same parameters as the API, without their two-letter prefix.
Some notable exceptions:

* ``Image.imageinfo`` is the imageinfo of the latest image. Earlier versions can be fetched using ``imagehistory()``.
* ``Site.all*``: parameter ``(ap)from`` renamed to ``start``
* ``categorymembers`` is implemented as ``Category.members``
* ``deletedrevs`` is ``deletedrevisions``
* ``usercontribs`` is ``usercontributions``
* First parameters of ``search`` and ``usercontributions`` are ``search`` and ``user``, respectively

Properties and generators are implemented as Python generators which yield one item per iteration. Their deprecated ``limit`` parameter is only an indication of the number of items retrieved from the API per request. It is not the total limit. Doing ``list(generator(limit = 50))`` will return ALL items, not 50, but it will query the API in chunks of 50 items at a time (so after yielding one item from the generator, the next 49 will be "free", then the next will trigger a new API call). The replacement ``api_chunk_size`` parameter does the same thing, but is more clearly named. If both ``limit`` and ``api_chunk_size`` are specified, ``limit`` will be ignored. The ``max_items`` parameter sets a total limit on the number of items which will be yielded. Use ``list(generator(max_items = 50))`` to limit the amount of items returned to 50. Higher level functions that have a ``limit`` parameter also now have ``api_chunk_size`` and ``max_items`` parameters that should be preferred. Default API chunk size is generally the maximum chunk size (500 for most wikis).

Page objects
------------

The base Page object is called ``Page``
and from that derive ``Category`` and ``Image``.
When the page is retrieved via ``Site.pages`` or a generator,
it will check automatically which of those three specific types
should be returned.

Continuation and per-property completeness
------------------------------------------

A single API request can return several page properties, and each property has
its own continuation branch.  ``prop=categories|extlinks`` may therefore
complete the categories while external links still need another request.  The
listing keeps these branches apart instead of merging every continuation token
into one bag of request parameters:

* ``List.complete`` is true only once no module still needs a continuation.
* ``List.incomplete_properties`` names the properties that are still streaming.
* ``List.property_status(name)`` returns a :class:`PropertyState
  <mwclient.listing.PropertyState>` with the property's remaining
  ``continuation`` token, its ``complete`` flag and a ``basis`` explaining why
  (``batchcomplete``, ``continuation ended``, ...).
* ``List.completion_state`` summarises the same information for every property.

A property that is genuinely empty is reported with an explicit ``empty``
marker; a property whose members may still arrive is not.

For page generators, a page can be repeated across continuation chunks while a
property is paged through, each time carrying only part of that property.
``GeneratorList`` merges the repeated representations by page identity and
deduplicates members, including a chunk retried after a transient failure.
Pages are released once all their requested properties are complete, so both
pagination styles deliver the same page with the same members.  Every yielded
page has a ``completion_info`` attribute mirroring the list-level states.  If
the response stream stops while a property is still incomplete, the page is
still delivered but ``completion_info['complete']`` is false and the pending
property keeps its continuation token, so it is not mistaken for a finished
record.

For convenience, ``Site.images`` and ``Site.categories`` are also provided.
These do exactly the same as `Site.Pages`, except that they require the page name
without its namespace prefixed.

    >>> page = site.Pages['Template:Stub']   # a Page object
    >>> image = site.Pages['Image:Wiki.png'] # an Image object
    >>> image = site.Images['Wiki.png']      # the same Image object
    >>> cat = site.Pages['Category:Python']  # a Category object
    >>> cat = site.Images['Python']          # the same Category object
