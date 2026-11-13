"""Build traceable, content-addressed manifests of historical file versions.

When files are overwritten under the same name, reconstructing which
version a historical page revision referenced requires consulting the
file history rather than the latest ``imageinfo``. This module resolves
the active version for each ``(page revision, file)`` reference and
records the result in a manifest where identified versions are
deduplicated by content (``sha1``) while every entry keeps its own
provenance, i.e. which historical page revision referenced it.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

import mwclient.client
import mwclient.image
from mwclient.image import TimestampLike, _coerce_timestamp


def _iso(value: Optional[time.struct_time]) -> Optional[str]:
    if value is None:
        return None
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', value)


def _image_title(site: 'mwclient.client.Site', title: str) -> 'mwclient.image.Image':
    if ':' not in title:
        title = f'File:{title}'
    return mwclient.image.Image(site, title)


@dataclass
class ManifestEntry:
    """One reference from a historical page revision to a file version.

    Attributes:
        source_page: Title of the page the reference was found on.
        source_revision: Revision id of that page, when known.
        source_timestamp: Timestamp of that page revision (ISO 8601).
        requested_title: The file title as referenced.
        resolved_title: The canonical file title after following redirects.
        status: ``resolved``, ``ambiguous`` or ``missing``.
        content_key: The ``sha1`` of the resolved content, used as the
            content-addressed storage key. ``None`` when unresolved.
        version_timestamp: Timestamp of the resolved file version.
        url: Download URL of the resolved file version.
        archivename: Archive name of the resolved file version, if archived.
        candidates: Content hashes of the tied versions when the file was
            replaced twice within the same second and the order is unknown.
    """

    source_page: str
    source_revision: Optional[int]
    source_timestamp: str
    requested_title: str
    resolved_title: str
    status: str
    content_key: Optional[str] = None
    version_timestamp: Optional[str] = None
    url: Optional[str] = None
    archivename: Optional[str] = None
    candidates: Tuple[str, ...] = field(default_factory=tuple)


class AssetManifest:
    """Collects file references and resolves them into a traceable manifest.

    Identified versions are deduplicated by content: every distinct
    ``sha1`` is stored once in :attr:`contents`, and entries reference it
    through their ``content_key``. Entries are never merged, so the
    manifest always shows which historical page revision each reference
    came from.

    Args:
        site: The :class:`~mwclient.client.Site` to resolve files against.
    """

    def __init__(self, site: 'mwclient.client.Site') -> None:
        self.site = site
        self.entries = []  # type: List[ManifestEntry]
        self.contents = {}  # type: Dict[str, Dict[str, Any]]

    @staticmethod
    def content_path(content_key: str) -> str:
        """Return the content-addressed storage path for a content key.

        Versions with identical content share a single storage location,
        regardless of how many historical entries reference them.
        """
        return f'{content_key[:2]}/{content_key[2:4]}/{content_key}'

    @staticmethod
    def _content_record(version: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            'sha1': version.get('sha1'),
            'url': version.get('url'),
            'timestamp': _iso(version.get('timestamp')),
            'archivename': version.get('archivename'),
            'size': version.get('size'),
            'width': version.get('width'),
            'height': version.get('height'),
            'mime': version.get('mime'),
            'user': version.get('user'),
            'comment': version.get('comment'),
        }

    def add(
        self,
        page_title: str,
        revision_timestamp: TimestampLike,
        image_title: str,
        revision_id: Optional[int] = None,
        resolve_redirect: bool = True
    ) -> ManifestEntry:
        """Resolve one ``(page revision, file)`` reference and record it.

        Args:
            page_title: Title of the page the reference was found on.
            revision_timestamp: Timestamp of the referencing page revision.
            image_title: The file title as referenced on that page.
            revision_id: The referencing page revision id, when known.
            resolve_redirect: Follow file redirects before resolving.

        Returns:
            The recorded :class:`ManifestEntry`.
        """
        image = _image_title(self.site, image_title)
        resolution = image.imageinfo_at(
            revision_timestamp, resolve_redirect=resolve_redirect
        )

        content_key = None
        version_timestamp = None
        url = None
        archivename = None
        candidates = ()  # type: Tuple[str, ...]

        if resolution.resolved and resolution.version is not None:
            version = resolution.version
            content_key = version.get('sha1')
            version_timestamp = _iso(version.get('timestamp'))
            url = version.get('url')
            archivename = version.get('archivename')
            if content_key is not None and content_key not in self.contents:
                self.contents[content_key] = self._content_record(version)
        elif resolution.ambiguous:
            candidates = tuple(
                c.get('sha1') for c in resolution.candidates if c.get('sha1')
            )

        entry = ManifestEntry(
            source_page=page_title,
            source_revision=revision_id,
            source_timestamp=_iso(_coerce_timestamp(revision_timestamp)) or '',
            requested_title=resolution.requested_title,
            resolved_title=resolution.resolved_title,
            status=resolution.status,
            content_key=content_key,
            version_timestamp=version_timestamp,
            url=url,
            archivename=archivename,
            candidates=candidates,
        )
        self.entries.append(entry)
        return entry

    def add_page(
        self,
        page_title: str,
        revision_timestamp: TimestampLike,
        resolve_redirect: bool = True
    ) -> List[ManifestEntry]:
        """Resolve every file referenced by a page revision.

        The revision active at ``revision_timestamp`` is looked up, its
        referenced files are read from the parser output for that
        revision, and each is resolved with :meth:`add`.

        Args:
            page_title: Title of the catalog page.
            revision_timestamp: Timestamp of the page revision.
            resolve_redirect: Follow file redirects before resolving.

        Returns:
            The recorded entries, one per referenced file.
        """
        page = self.site.pages[page_title]
        iso_ts = _iso(_coerce_timestamp(revision_timestamp))
        revisions = page.revisions(
            start=iso_ts, dir='older', prop='ids|timestamp', api_chunk_size=1
        )
        revision = next(iter(revisions), None)
        if revision is None:
            return []
        revision_id = revision.get('revid')
        revision_time = revision.get('timestamp')

        data = self.site.get('parse', oldid=revision_id, prop='images')
        images = data.get('parse', {}).get('images', [])

        entries = []
        for image in images:
            if isinstance(image, Mapping):
                title = image.get('title') or image.get('*')
            else:
                title = image
            if not title:
                continue
            entries.append(self.add(
                page_title,
                revision_time if revision_time is not None else revision_timestamp,
                title,
                revision_id=revision_id,
                resolve_redirect=resolve_redirect,
            ))
        return entries

    def build(self) -> Dict[str, Any]:
        """Return the manifest as a JSON-serializable dictionary."""
        return {
            'entries': [asdict(entry) for entry in self.entries],
            'contents': self.contents,
        }

    def to_json(self, **kwargs: Any) -> str:
        """Serialize the manifest to JSON."""
        return json.dumps(self.build(), **kwargs)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.entries)
