import json
import time
import unittest
import unittest.mock as mock

from mwclient.image import VersionResolution
from mwclient.provenance import AssetManifest, ManifestEntry


def ts(value):
    return time.strptime(value, '%Y-%m-%dT%H:%M:%SZ')


def resolved(sha1, timestamp='2020-01-01T00:00:00Z', title='File:A.jpg'):
    return VersionResolution(
        status=VersionResolution.RESOLVED,
        requested_title=title,
        resolved_title=title,
        version={
            'sha1': sha1,
            'timestamp': ts(timestamp),
            'url': f'https://example.org/{sha1}.jpg',
            'size': 100,
            'width': 10,
            'height': 10,
            'mime': 'image/jpeg',
        },
    )


def ambiguous(title='File:A.jpg'):
    return VersionResolution(
        status=VersionResolution.AMBIGUOUS,
        requested_title=title,
        resolved_title=title,
        candidates=(
            {'sha1': 'sha1-x', 'timestamp': ts('2020-01-02T00:00:00Z')},
            {'sha1': 'sha1-y', 'timestamp': ts('2020-01-02T00:00:00Z')},
        ),
    )


def missing(title='File:A.jpg'):
    return VersionResolution(
        status=VersionResolution.MISSING,
        requested_title=title,
        resolved_title=title,
    )


class TestAssetManifest:

    @mock.patch('mwclient.client.Site')
    def test_add_resolved_records_content(self, mock_site):
        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.return_value = resolved('sha1-a')
            entry = manifest.add('Catalog', '2020-01-02T00:00:00Z', 'A.jpg')

        assert entry.status == 'resolved'
        assert entry.content_key == 'sha1-a'
        assert entry.source_page == 'Catalog'
        assert entry.source_timestamp == '2020-01-02T00:00:00Z'
        assert manifest.contents['sha1-a']['url'] == 'https://example.org/sha1-a.jpg'

    @mock.patch('mwclient.client.Site')
    def test_content_deduplicated_but_provenance_kept(self, mock_site):
        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.return_value = resolved('sha1-shared')
            first = manifest.add(
                'Catalog/1901', '2020-01-02T00:00:00Z', 'A.jpg', revision_id=11)
            second = manifest.add(
                'Catalog/1902', '2020-01-03T00:00:00Z', 'A.jpg', revision_id=22)

        # Shared content is stored once...
        assert len(manifest.contents) == 1
        assert first.content_key == second.content_key == 'sha1-shared'
        # ...but each entry keeps its own provenance.
        assert first.source_page == 'Catalog/1901'
        assert second.source_page == 'Catalog/1902'
        assert first.source_revision == 11
        assert second.source_revision == 22

    @mock.patch('mwclient.client.Site')
    def test_distinct_content_stored_separately(self, mock_site):
        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.side_effect = [
                resolved('sha1-a'), resolved('sha1-b')
            ]
            manifest.add('Catalog', '2020-01-02T00:00:00Z', 'A.jpg')
            manifest.add('Catalog', '2020-01-03T00:00:00Z', 'A.jpg')

        assert set(manifest.contents) == {'sha1-a', 'sha1-b'}

    @mock.patch('mwclient.client.Site')
    def test_ambiguous_entry_has_no_content_key(self, mock_site):
        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.return_value = ambiguous()
            entry = manifest.add('Catalog', '2020-01-02T12:00:00Z', 'A.jpg')

        assert entry.status == 'ambiguous'
        assert entry.content_key is None
        assert entry.candidates == ('sha1-x', 'sha1-y')
        assert manifest.contents == {}

    @mock.patch('mwclient.client.Site')
    def test_missing_entry(self, mock_site):
        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.return_value = missing()
            entry = manifest.add('Catalog', '2019-01-01T00:00:00Z', 'A.jpg')

        assert entry.status == 'missing'
        assert entry.content_key is None

    @mock.patch('mwclient.client.Site')
    def test_content_path_is_sharded(self, mock_site):
        assert AssetManifest.content_path('abcdef') == 'ab/cd/abcdef'

    @mock.patch('mwclient.client.Site')
    def test_build_and_json(self, mock_site):
        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.return_value = resolved('sha1-a')
            manifest.add('Catalog', '2020-01-02T00:00:00Z', 'A.jpg')

        built = manifest.build()
        assert len(built['entries']) == 1
        assert built['entries'][0]['content_key'] == 'sha1-a'
        assert 'sha1-a' in built['contents']

        parsed = json.loads(manifest.to_json())
        assert parsed['entries'][0]['source_page'] == 'Catalog'

    @mock.patch('mwclient.client.Site')
    def test_len_and_iter(self, mock_site):
        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.return_value = resolved('sha1-a')
            manifest.add('Catalog', '2020-01-02T00:00:00Z', 'A.jpg')

        assert len(manifest) == 1
        assert isinstance(list(manifest)[0], ManifestEntry)


class TestAddPage:

    @mock.patch('mwclient.client.Site')
    def test_resolves_every_referenced_file(self, mock_site):
        page = mock.Mock()
        page.revisions.return_value = iter([
            {'revid': 42, 'timestamp': ts('2020-01-02T00:00:00Z')}
        ])
        mock_site.pages.__getitem__.return_value = page
        mock_site.get.return_value = {
            'parse': {'images': ['File:A.jpg', 'File:B.jpg']}
        }

        manifest = AssetManifest(mock_site)
        with mock.patch('mwclient.provenance._image_title') as image_title:
            image_title.return_value.imageinfo_at.side_effect = [
                resolved('sha1-a', title='File:A.jpg'),
                resolved('sha1-b', title='File:B.jpg'),
            ]
            entries = manifest.add_page('Catalog', '2020-01-02T00:00:00Z')

        assert len(entries) == 2
        assert {e.content_key for e in entries} == {'sha1-a', 'sha1-b'}
        assert all(e.source_revision == 42 for e in entries)
        assert all(e.source_page == 'Catalog' for e in entries)
        # The revision's own timestamp is used, and the parser was queried
        # for that exact revision.
        mock_site.get.assert_called_once_with('parse', oldid=42, prop='images')

    @mock.patch('mwclient.client.Site')
    def test_no_revision_at_time(self, mock_site):
        page = mock.Mock()
        page.revisions.return_value = iter([])
        mock_site.pages.__getitem__.return_value = page

        manifest = AssetManifest(mock_site)
        assert manifest.add_page('Catalog', '2019-01-01T00:00:00Z') == []


if __name__ == '__main__':
    unittest.main()
