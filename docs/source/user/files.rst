.. _files:

Working with files
==================

Assuming you have :ref:`connected <connecting>` to your site.


Getting info about a file
-------------------------

To get information about a file:

    >>> file = site.images['Example.jpg']

where ``file`` is now an instance of :class:`Image <mwclient.image.Image>`
that you can query for various properties:

    >>> file.imageinfo
    {'comment': 'Reverted to version as of 17:58, 12 March 2010',
     'descriptionshorturl': 'https://commons.wikimedia.org/w/index.php?curid=6428847',
     'descriptionurl': 'https://commons.wikimedia.org/wiki/File:Example.jpg',
     'height': 178,
     'metadata': [{'name': 'MEDIAWIKI_EXIF_VERSION', 'value': 1}],
     'sha1': 'd01b79a6781c72ac9bfff93e5e2cfbeef4efc840',
     'size': 9022,
     'timestamp': '2010-03-14T17:20:20Z',
     'url': 'https://upload.wikimedia.org/wikipedia/commons/a/a9/Example.jpg',
     'user': 'SomeUser',
     'width': 172}

You also have easy access to file usage:

    >>> for page in image.imageusage():
    >>>     print('Page:', page.name, '; namespace:', page.namespace)

See the :class:`API reference <mwclient.image.Image>` for more options.

.. caution::
    Note that ``Image.exists`` refers to whether a file exists *locally*. If a file
    does not exist locally, but in a shared repo like Wikimedia Commons, it will
    return ``False``.

    To check if a file exists locally *or* in a shared repo, you could test if
    ``image.imageinfo != {}``.

Downloading a file
------------------

The :meth:`Image.download() <mwclient.image.Image.download>` method can be used to download
the full size file. Pass it a file object and it will stream the image to it,
avoiding the need for keeping the whole file in memory:

    >>> file = site.images['Example.jpg']
    >>> with open('Example.jpg', 'wb') as fd:
    ...     image.download(fd)

Uploading a file
----------------

    >>> site.upload(open('file.jpg'), 'destination.jpg', 'Image description')

.. _historical-versions:

Resolving historical versions
-----------------------------

When a file has been overwritten under the same name, the latest
``imageinfo`` no longer reflects what a historical page revision
referenced. Use :meth:`Image.imageinfo_at() <mwclient.image.Image.imageinfo_at>`
to look a timestamp up in the file history instead:

    >>> resolution = file.imageinfo_at('2010-03-14T17:20:20Z')
    >>> resolution.status
    'resolved'
    >>> resolution.sha1
    'd01b79a6781c72ac9bfff93e5e2cfbeef4efc840'

File redirects are followed by default, so a reference through an old
title still resolves to the canonical file. If the file was replaced
twice within the same second and the history does not record the order,
the result is ``ambiguous`` rather than a guess — the latest download is
never silently treated as the version that was active at the time.

Building a traceable manifest
-----------------------------

The :class:`AssetManifest <mwclient.provenance.AssetManifest>` resolves
every file referenced by a set of historical page revisions and records
the result in a manifest. Identified versions are deduplicated by
content (``sha1``), while every entry keeps the provenance of the page
revision it came from:

    >>> from mwclient.provenance import AssetManifest
    >>> manifest = AssetManifest(site)
    >>> manifest.add_page('Catalog/1901', '2010-03-14T17:20:20Z')
    >>> data = manifest.build()
    >>> data['entries'][0]['content_key']
    'd01b79a6781c72ac9bfff93e5e2cfbeef4efc840'

Entries that share content point at the same record in
``manifest.contents`` but remain separate entries, so the manifest always
shows which historical entry referenced each version.

