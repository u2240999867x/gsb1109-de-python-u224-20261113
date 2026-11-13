import time
import unittest
import unittest.mock as mock

import pytest

from mwclient.image import Image, VersionResolution, _coerce_timestamp


def ts(value):
    return time.strptime(value, '%Y-%m-%dT%H:%M:%SZ')


def version(timestamp, sha1, **extra):
    info = {
        'timestamp': ts(timestamp),
        'sha1': sha1,
        'url': f'https://example.org/{sha1}.jpg',
        'size': 100,
        'width': 10,
        'height': 10,
    }
    info.update(extra)
    return info


def make_image(site, name='File:Example.jpg', history=(), redirect=False):
    info = {'ns': 6, 'title': name, 'imageinfo': [{}]}
    if redirect:
        info['redirect'] = ''
    image = Image(site, name, info=info)
    image.imagehistory = mock.Mock(side_effect=lambda: iter(list(history)))
    return image


class TestCoerceTimestamp:

    def test_struct_time_passthrough(self):
        value = ts('2020-01-02T03:04:05Z')
        assert _coerce_timestamp(value) is value

    def test_iso_string(self):
        assert _coerce_timestamp('2020-01-02T03:04:05Z') == ts('2020-01-02T03:04:05Z')

    def test_naive_datetime(self):
        from datetime import datetime
        value = _coerce_timestamp(datetime(2020, 1, 2, 3, 4, 5))
        assert value == ts('2020-01-02T03:04:05Z')

    def test_aware_datetime_normalized_to_utc(self):
        from datetime import datetime, timezone, timedelta
        value = datetime(2020, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))
        assert _coerce_timestamp(value) == ts('2020-01-02T03:04:05Z')

    def test_invalid_type(self):
        with pytest.raises(TypeError):
            _coerce_timestamp(123)  # type: ignore[arg-type]


class TestImageInfoAt:

    @mock.patch('mwclient.client.Site')
    def test_resolves_version_active_at_timestamp(self, mock_site):
        history = [
            version('2020-01-03T00:00:00Z', 'sha1-c'),
            version('2020-01-02T00:00:00Z', 'sha1-b'),
            version('2020-01-01T00:00:00Z', 'sha1-a'),
        ]
        image = make_image(mock_site, history=history)

        result = image.imageinfo_at('2020-01-02T12:00:00Z')

        assert result.resolved
        assert result.sha1 == 'sha1-b'
        assert result.version['url'] == 'https://example.org/sha1-b.jpg'

    @mock.patch('mwclient.client.Site')
    def test_does_not_use_latest_download(self, mock_site):
        # The latest version must not be mistaken for the historical one.
        history = [
            version('2020-01-03T00:00:00Z', 'sha1-latest'),
            version('2020-01-01T00:00:00Z', 'sha1-old'),
        ]
        image = make_image(mock_site, history=history)

        result = image.imageinfo_at('2020-01-01T12:00:00Z')

        assert result.resolved
        assert result.sha1 == 'sha1-old'

    @mock.patch('mwclient.client.Site')
    def test_missing_before_first_version(self, mock_site):
        history = [version('2020-01-02T00:00:00Z', 'sha1-a')]
        image = make_image(mock_site, history=history)

        result = image.imageinfo_at('2020-01-01T00:00:00Z')

        assert result.missing
        assert result.version is None

    @mock.patch('mwclient.client.Site')
    def test_same_second_replacement_is_ambiguous(self, mock_site):
        # Two replacements in the same second with no recorded order.
        history = [
            version('2020-01-02T00:00:00Z', 'sha1-x'),
            version('2020-01-02T00:00:00Z', 'sha1-y'),
            version('2020-01-01T00:00:00Z', 'sha1-a'),
        ]
        image = make_image(mock_site, history=history)

        result = image.imageinfo_at('2020-01-02T12:00:00Z')

        assert result.ambiguous
        assert result.version is None
        assert {c['sha1'] for c in result.candidates} == {'sha1-x', 'sha1-y'}

    @mock.patch('mwclient.client.Site')
    def test_same_second_identical_content_is_resolved(self, mock_site):
        # Identical re-uploads in the same second resolve by content.
        history = [
            version('2020-01-02T00:00:00Z', 'sha1-same'),
            version('2020-01-02T00:00:00Z', 'sha1-same'),
        ]
        image = make_image(mock_site, history=history)

        result = image.imageinfo_at('2020-01-02T12:00:00Z')

        assert result.resolved
        assert result.sha1 == 'sha1-same'

    @mock.patch('mwclient.client.Site')
    def test_ambiguity_only_at_tied_timestamp(self, mock_site):
        history = [
            version('2020-01-02T00:00:00Z', 'sha1-x'),
            version('2020-01-02T00:00:00Z', 'sha1-y'),
            version('2020-01-01T00:00:00Z', 'sha1-a'),
        ]
        image = make_image(mock_site, history=history)

        # A time before the ambiguous second resolves cleanly.
        result = image.imageinfo_at('2020-01-01T12:00:00Z')
        assert result.resolved
        assert result.sha1 == 'sha1-a'

    @mock.patch('mwclient.client.Site')
    def test_accepts_datetime_and_struct_time(self, mock_site):
        from datetime import datetime
        history = [version('2020-01-01T00:00:00Z', 'sha1-a')]
        image = make_image(mock_site, history=history)

        assert image.imageinfo_at(datetime(2020, 1, 2)).sha1 == 'sha1-a'
        assert image.imageinfo_at(ts('2020-01-02T00:00:00Z')).sha1 == 'sha1-a'

    @mock.patch('mwclient.client.Site')
    def test_follows_redirect(self, mock_site):
        mock_site.api_limit = 500
        # This history goes through the real List, which parses ISO strings.
        history = [{
            'timestamp': '2020-01-01T00:00:00Z',
            'sha1': 'sha1-target',
            'url': 'https://example.org/sha1-target.jpg',
        }]

        def fake_get(action, *args, **kwargs):
            if kwargs.get('prop') == 'pageprops':
                return {'query': {'redirects': [
                    {'from': 'File:Old.jpg', 'to': 'File:New.jpg'}
                ]}}
            if kwargs.get('prop') == 'info':
                return {'query': {'pages': {'1': {
                    'ns': 6, 'title': 'File:New.jpg'
                }}}}
            if kwargs.get('prop') == 'info|imageinfo':
                return {'query': {'pages': {'1': {
                    'ns': 6, 'title': 'File:New.jpg', 'imageinfo': [{}]
                }}}}
            if ('prop', 'imageinfo') in args:
                return {'query': {'pages': {'1': {
                    'title': 'File:New.jpg', 'imageinfo': list(history)
                }}}}
            raise AssertionError(f'unexpected call: {args} {kwargs}')

        mock_site.get.side_effect = fake_get
        image = make_image(mock_site, name='File:Old.jpg', redirect=True)

        result = image.imageinfo_at('2020-01-02T00:00:00Z')

        assert result.resolved
        assert result.requested_title == 'File:Old.jpg'
        assert result.resolved_title == 'File:New.jpg'
        assert result.sha1 == 'sha1-target'

    @mock.patch('mwclient.client.Site')
    def test_redirect_cycle_does_not_loop(self, mock_site):
        mock_site.redirects_to = mock.Mock(return_value=None)
        image = make_image(mock_site, redirect=True)
        # redirects_to returns None, so the chain stops immediately.
        assert image._follow_redirects() is image


class TestVersionResolution:

    def test_status_properties(self):
        resolved = VersionResolution(
            status=VersionResolution.RESOLVED,
            requested_title='File:A.jpg',
            resolved_title='File:A.jpg',
            version={'sha1': 'abc'},
        )
        assert resolved.resolved
        assert not resolved.ambiguous
        assert not resolved.missing
        assert resolved.sha1 == 'abc'

        missing = VersionResolution(
            status=VersionResolution.MISSING,
            requested_title='File:A.jpg',
            resolved_title='File:A.jpg',
        )
        assert missing.missing
        assert missing.sha1 is None


if __name__ == '__main__':
    unittest.main()
