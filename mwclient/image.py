import io
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Mapping, Any, Tuple, Union, overload

import mwclient.listing
import mwclient.page
from mwclient._types import Namespace
from mwclient.util import handle_limit, parse_timestamp


TimestampLike = Union[str, datetime, time.struct_time]


def _coerce_timestamp(value: TimestampLike) -> time.struct_time:
    """Normalize a timestamp to a UTC ``time.struct_time``.

    Accepts ISO 8601 strings (``%Y-%m-%dT%H:%M:%SZ``), ``datetime``
    objects (naive values are treated as UTC) and ``time.struct_time``.
    """
    if isinstance(value, time.struct_time):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.timetuple()
    if isinstance(value, str):
        return parse_timestamp(value)
    raise TypeError(f'Unsupported timestamp type: {type(value).__name__}')


@dataclass
class VersionResolution:
    """The outcome of resolving which file version was active at a time.

    Attributes:
        status: One of ``resolved``, ``ambiguous`` or ``missing``.
        requested_title: The file title that was asked about.
        resolved_title: The canonical file title after following redirects.
        version: The imageinfo of the active version, when resolved.
        candidates: Every version tied for the active timestamp. More than
            one candidate means the file was replaced twice within the same
            second and the history does not record their order.
    """

    RESOLVED = 'resolved'
    AMBIGUOUS = 'ambiguous'
    MISSING = 'missing'

    status: str
    requested_title: str
    resolved_title: str
    version: Optional[Mapping[str, Any]] = None
    candidates: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    @property
    def resolved(self) -> bool:
        return self.status == self.RESOLVED

    @property
    def ambiguous(self) -> bool:
        return self.status == self.AMBIGUOUS

    @property
    def missing(self) -> bool:
        return self.status == self.MISSING

    @property
    def sha1(self) -> Optional[str]:
        """The content hash of the resolved version, if any."""
        if self.version is None:
            return None
        return self.version.get('sha1')


class Image(mwclient.page.Page):
    """
    Represents an image on a MediaWiki wiki represented by a
    :class:`~mwclient.client.Site` object.

    Args:
        site (mwclient.client.Site): The site object this page belongs to.
        name (Union[str, int, Page]): The title of the page, the page ID, or
            another :class:`Page` object to copy.
        info (Optional[dict]): Page info, if already fetched, e.g., when
            iterating over a list of pages. If not provided, the page info
            will be fetched from the API.
    """

    def __init__(
        self,
        site: 'mwclient.client.Site',
        name: str,
        info: Optional[Mapping[str, Any]] = None
    ) -> None:
        super().__init__(
            site, name, info, extra_properties={
                'imageinfo': (
                    ('iiprop',
                     'timestamp|user|comment|url|size|sha1|metadata|mime|archivename'),
                )
            }
        )
        self.imagerepository = self._info.get('imagerepository', '')
        self.imageinfo = self._info.get('imageinfo', ({}, ))[0]

    def imagehistory(self) -> 'mwclient.listing.PageProperty':
        """
        Get file revision info for the given file.

        API doc: https://www.mediawiki.org/wiki/API:Imageinfo
        """
        return mwclient.listing.PageProperty(
            self, 'imageinfo', 'ii',
            iiprop='timestamp|user|comment|url|size|sha1|metadata|mime|archivename'
        )

    def _follow_redirects(self) -> 'Image':
        """Follow file redirects and return the target image.

        Returns the current image unchanged when it is not a redirect or
        when the redirect chain cannot be followed (cycles, missing
        targets). The chain is never followed more than once per title.
        """
        image = self
        seen = {self.name}
        while image.redirect:
            target = image.redirects_to()
            if target is None or target.name in seen:
                break
            seen.add(target.name)
            image = Image(self.site, target.name)
        return image

    def imageinfo_at(
        self,
        timestamp: TimestampLike,
        resolve_redirect: bool = True
    ) -> VersionResolution:
        """Resolve which file version was active at a given time.

        This looks the timestamp up in the file history instead of using
        the latest ``imageinfo``, so it is safe to use when reconstructing
        which version a historical page revision referenced, even if the
        file has since been overwritten under the same name.

        When the file was replaced twice within the same second and the
        history does not record the order of those replacements, the result
        is ``ambiguous`` rather than a guess: the latest download is never
        silently treated as the version that was active at the time. If the
        tied replacements have identical content (same ``sha1``), the
        content is still considered resolved.

        Args:
            timestamp: The reference time, as an ISO 8601 string, a
                ``datetime`` or a ``time.struct_time``.
            resolve_redirect: Follow file redirects before resolving.

        Returns:
            A :class:`VersionResolution` describing the outcome.
        """
        reference = _coerce_timestamp(timestamp)
        requested_title = self.name
        image = self._follow_redirects() if resolve_redirect else self

        history = [v for v in image.imagehistory() if v.get('timestamp')]
        eligible = [v for v in history if v['timestamp'] <= reference]
        if not eligible:
            return VersionResolution(
                status=VersionResolution.MISSING,
                requested_title=requested_title,
                resolved_title=image.name,
            )

        latest = max(v['timestamp'] for v in eligible)
        candidates = tuple(v for v in eligible if v['timestamp'] == latest)
        if len(candidates) == 1:
            version = candidates[0]
        else:
            hashes = {v.get('sha1') for v in candidates}
            if len(hashes) != 1 or None in hashes:
                # Tied replacements with unknown order and differing (or
                # unidentified) content: do not guess which was active.
                return VersionResolution(
                    status=VersionResolution.AMBIGUOUS,
                    requested_title=requested_title,
                    resolved_title=image.name,
                    candidates=candidates,
                )
            # Same content uploaded twice in the same second: the content
            # is identified even though the version order is not.
            version = candidates[0]
        return VersionResolution(
            status=VersionResolution.RESOLVED,
            requested_title=requested_title,
            resolved_title=image.name,
            version=version,
            candidates=candidates,
        )

    def imageusage(
        self,
        namespace: Optional[Namespace] = None,
        filterredir: str = 'all',
        redirect: bool = False,
        limit: Optional[int] = None,
        generator: bool = True,
        max_items: Optional[int] = None,
        api_chunk_size: Optional[int] = None,
        with_content: bool = False
    ) -> 'mwclient.listing.List':
        """
        List pages that use the given file.

        API doc: https://www.mediawiki.org/wiki/API:Imageusage
        """

        (max_items, api_chunk_size) = handle_limit(limit, max_items, api_chunk_size)

        kwargs = mwclient.listing.List.get_page_listing_args(
            'iu', generator, with_content, {
                'title': self.name,
                'namespace': namespace,
                'filterredir': filterredir,
                'redirect': '1' if redirect else None
            })

        return mwclient.listing.List.get_list(generator)(
            self.site,
            'imageusage',
            'iu',
            max_items=max_items,
            api_chunk_size=api_chunk_size,
            return_values='title',
            **kwargs
        )

    def duplicatefiles(
        self,
        limit: Optional[int] = None,
        max_items: Optional[int] = None,
        api_chunk_size: Optional[int] = None
    ) -> 'mwclient.listing.PageProperty':
        """
        List duplicates of the current file.

        API doc: https://www.mediawiki.org/wiki/API:Duplicatefiles

        limit sets a hard cap on the total number of results, it does
        not only specify the API chunk size.
        """
        (max_items, api_chunk_size) = handle_limit(limit, max_items, api_chunk_size)
        return mwclient.listing.PageProperty(
            self,
            'duplicatefiles',
            'df',
            max_items=max_items,
            api_chunk_size=api_chunk_size
        )

    @overload
    def download(self) -> bytes:
        ...

    @overload
    def download(self, destination: io.BufferedWriter) -> None:
        ...

    def download(
        self, destination: Optional[io.BufferedWriter] = None
    ) -> Optional[bytes]:
        """
        Download the file. If `destination` is given, the file will be written
        directly to the stream. Otherwise the file content will be stored in memory
        and returned (with the risk of running out of memory for large files).

        Recommended usage:

            >>> with open(filename, 'wb') as fd:
            ...     image.download(fd)

        Args:
            destination: Destination file
        """
        url = self.imageinfo['url']
        if destination is not None:
            res = self.site.connection.get(url, stream=True)
            for chunk in res.iter_content(1024):
                destination.write(chunk)
            return None
        else:
            return self.site.connection.get(url).content

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} object '{self.name}' for {self.site}>"
