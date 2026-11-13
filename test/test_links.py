import json
import unittest
import unittest.mock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import responses

import mwclient
from mwclient.links import (
    LinkResolver,
    STATUS_INVALID,
    STATUS_PAGE_MISSING,
    STATUS_REDIRECT_LOOP,
    STATUS_SECTION_AMBIGUOUS,
    STATUS_SECTION_DELETED,
    STATUS_SECTION_NUMBERED,
)
from mwclient.page import Page

API_URL = 'https://test.wikipedia.org/w/api.php'


def _params(request: Any) -> Dict[str, str]:
    if request.method == 'GET':
        values = request.params
        if isinstance(values, dict):
            return {str(k): str(v) for k, v in values.items()}
        query = parse_qs(urlparse(request.url).query, keep_blank_values=True)
    else:
        body = request.body or ''
        query = parse_qs(body, keep_blank_values=True)
    return {key: values[0] for key, values in query.items()}


class FakeWiki:
    """A tiny MediaWiki API model for link resolution tests."""

    def __init__(self) -> None:
        self.pages: Dict[str, Dict[str, Any]] = {}
        self.redirects: Dict[str, Tuple[str, Optional[str]]] = {}
        self.sections: Dict[str, List[Dict[str, Any]]] = {}
        self.section_texts: Dict[str, Dict[int, str]] = {}
        self.full_text: Dict[str, str] = {}
        self.calls: List[Dict[str, str]] = []
        self.parse_error: Optional[str] = None

    def add_page(
        self, title: str, pageid: int, sections: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        self.pages[title] = {
            'pageid': pageid, 'ns': 0, 'title': title,
            'contentmodel': 'wikitext', 'pagelanguage': 'en',
        }
        if sections is not None:
            self.sections[title] = sections

    def add_redirect(
        self, title: str, target: str, fragment: Optional[str] = None
    ) -> None:
        self.pages[title] = {
            'pageid': len(self.pages) + 1, 'ns': 0, 'title': title,
            'redirect': '',
        }
        self.redirects[title] = (target, fragment)

    def add_missing(self, title: str) -> None:
        self.pages[title] = {'ns': 0, 'title': title, 'missing': ''}

    def _page_info(self, title: str) -> Dict[str, Any]:
        return dict(self.pages[title])

    def query(self, params: Dict[str, str]) -> Dict[str, Any]:
        titles = params.get('titles', '')
        first = titles.split('|')[0]
        page = dict(self.pages.get(first, {'ns': 0, 'title': first, 'missing': ''}))
        query: Dict[str, Any] = {'pages': {str(page.get('pageid', -1)): page}}
        redirect_entries = []
        if 'redirects' in params and first in self.redirects:
            target, fragment = self.redirects[first]
            entry: Dict[str, Any] = {'from': first, 'to': target}
            if fragment:
                entry['tofragment'] = fragment
            redirect_entries.append(entry)
            target_page = dict(
                self.pages.get(target, {'ns': 0, 'title': target, 'missing': ''})
            )
            query['pages'] = {str(target_page.get('pageid', -1)): target_page}
        if redirect_entries:
            query['redirects'] = redirect_entries
        return {'query': query}

    def parse(self, params: Dict[str, str]) -> Dict[str, Any]:
        title = params.get('page', '')
        sections = self.sections.get(title, [])
        return {
            'parse': {
                'title': title,
                'pageid': self.pages.get(title, {}).get('pageid', 1),
                'sections': sections,
            }
        }

    def revisions(self, params: Dict[str, str]) -> Dict[str, Any]:
        title = params.get('titles', '')
        page = self.pages[title]
        section = params.get('rvsection')
        if section is None:
            text = self.full_text.get(title, '')
        else:
            text = self.section_texts.get(title, {}).get(int(section), '')
        revision = {
            'contentformat': 'text/x-wiki',
            'contentmodel': 'wikitext',
            'timestamp': '2026-11-14T00:00:00Z',
            '*': text,
        }
        return {'query': {'pages': {str(page['pageid']): {
            'pageid': page['pageid'], 'ns': 0, 'title': title,
            'revisions': [revision],
        }}}}

    def callback(self, request: Any) -> Tuple[int, Dict[str, str], str]:
        params = _params(request)
        self.calls.append(params)
        action = params.get('action', '')
        if action == 'parse':
            if self.parse_error:
                body = {'error': {
                    'code': 'readapidenied',
                    'info': self.parse_error,
                }}
            else:
                body = self.parse(params)
        elif 'rvprop' in params:
            body = self.revisions(params)
        else:
            body = self.query(params)
        return 200, {'Content-Type': 'application/json'}, json.dumps(body)


class LinkResolverTest(unittest.TestCase):

    def setUp(self) -> None:
        self.wiki = FakeWiki()
        self.wiki.add_page(
            'New entry', 2, sections=[
                {'toclevel': 1, 'level': '2', 'line': 'History', 'number': '1',
                 'index': '1', 'byteoffset': 10, 'anchor': 'History'},
                {'toclevel': 1, 'level': '2', 'line': 'Material', 'number': '2',
                 'index': '2', 'byteoffset': 60, 'anchor': 'Material'},
            ]
        )
        self.wiki.full_text['New entry'] = (
            '== History ==\nHistory text.\n\n== Material ==\nMaterial text.\n'
        )
        self.wiki.section_texts['New entry'] = {
            1: '== History ==\nHistory text.\n',
            2: '== Material ==\nMaterial text.\n',
        }
        self.wiki.add_redirect('Old name', 'New entry')
        self.wiki.add_redirect('Old name with section', 'New entry', 'History')

        self.site = mwclient.Site('test.wikipedia.org', do_init=False)
        self.site.version = (1, 32, 0)
        self.site.rights = ['read']
        self.site.api_limit = 500

        self.responses = responses.RequestsMock(assert_all_requests_are_fired=False)
        self.responses.add_callback(
            responses.GET, API_URL, callback=self.wiki.callback
        )
        self.responses.start()

    def tearDown(self) -> None:
        self.responses.stop()
        self.responses.reset()

    def test_legacy_section_link_follows_redirect_and_returns_section_only(self):
        result = self.site.resolve_link('Old name#Material')

        assert result.status == 'resolved'
        assert result.original == 'Old name#Material'
        assert result.title == 'Old name'
        assert result.fragment == 'Material'
        assert result.target_title == 'New entry'
        assert result.target_fragment == 'Material'
        assert result.section is not None
        assert result.section.index == 2
        assert result.section.heading == 'Material'
        assert result.text == '== Material ==\nMaterial text.\n'
        # The whole page must never be substituted for the requested section.
        assert result.text != self.wiki.full_text['New entry']
        assert [(hop.from_title, hop.to_title) for hop in result.chain] == [
            ('Old name', 'New entry')
        ]

        revision_calls = [c for c in self.wiki.calls if 'rvsection' in c]
        assert revision_calls[-1]['rvsection'] == '2'

    def test_redirect_supplied_fragment_replaces_requested_fragment(self):
        result = self.site.resolve_link('Old name with section#Material')

        assert result.status == 'resolved'
        assert result.fragment == 'Material'
        assert result.target_fragment == 'History'
        assert result.section is not None
        assert result.section.index == 1
        assert result.text == '== History ==\nHistory text.\n'
        assert result.chain[0].to_fragment == 'History'
        assert result.target == 'New entry#History'
        assert result.path() == [
            ('Old name with section', 'Material'),
            ('New entry', 'History'),
        ]

    def test_multi_hop_redirect_keeps_fragment_only_when_redirect_does_not_set_it(self):
        self.wiki.add_redirect('Older name', 'Old name', 'History')
        result = self.site.resolve_link('Older name#Material')

        assert result.status == 'resolved'
        assert result.target_fragment == 'History'
        hops = [
            (hop.from_title, hop.to_title, hop.to_fragment)
            for hop in result.chain
        ]
        assert hops == [
            ('Older name', 'Old name', 'History'),
            ('Old name', 'New entry', None),
        ]
        assert result.path() == [
            ('Older name', 'Material'),
            ('Old name', 'History'),
            ('New entry', 'History'),
        ]

    def test_redirect_without_fragment_keeps_incoming_fragment(self):
        result = self.site.resolve_link('Old name#Material')
        assert result.target_fragment == 'Material'

    def test_deleted_section_is_reported_not_replaced_by_whole_page(self):
        result = self.site.resolve_link('Old name#Removed')

        assert result.status == STATUS_SECTION_DELETED
        assert result.section_deleted
        assert result.target_title == 'New entry'
        assert result.text is None
        assert 'Removed' in (result.reason or '')
        assert not any('rvsection' in call for call in self.wiki.calls)

    def test_missing_target_page_is_reported(self):
        result = self.site.resolve_link('Nowhere#Material')

        assert result.status == STATUS_PAGE_MISSING
        assert result.target_title == 'Nowhere'
        assert result.text is None

    def test_redirect_loop_reports_the_cycle(self):
        self.wiki.add_redirect('Loop A', 'Loop B')
        self.wiki.add_redirect('Loop B', 'Loop A')

        result = self.site.resolve_link('Loop A#Material')

        assert result.status == STATUS_REDIRECT_LOOP
        assert result.redirect_loop
        assert result.cycle == ('Loop A', 'Loop B', 'Loop A')
        assert 'Redirect cycle' in (result.reason or '')
        assert result.chain[-1].to_title == 'Loop A'

    def test_external_and_display_text_links(self):
        result = self.site.resolve_link('[[Old name#Material|see here]]')
        assert result.original == '[[Old name#Material|see here]]'
        assert result.status == 'resolved'
        assert result.text == '== Material ==\nMaterial text.\n'

        external = self.site.resolve_link('https://example.org/page#Material')
        assert external.status == STATUS_INVALID
        assert external.text is None

    def test_url_encoded_fragment_and_title(self):
        self.wiki.add_page(
            'Café', 9, sections=[{
                'toclevel': 1, 'level': '2', 'line': 'Crème brûlée',
                'number': '1', 'index': '1', 'byteoffset': 10,
                'anchor': 'Crème_brûlée',
            }]
        )
        self.wiki.section_texts['Café'] = {1: '== Crème brûlée ==\nDessert.\n'}

        result = self.site.resolve_link('Caf%C3%A9#Cr%C3%A8me_br%C3%BBl%C3%A9e')

        assert result.status == 'resolved'
        assert result.title == 'Café'
        assert result.fragment == 'Crème_brûlée'
        assert result.target_title == 'Café'
        assert result.section is not None
        assert result.section.index == 1

    def test_parse_failure_falls_back_to_wikitext_headings(self):
        self.wiki.parse_error = 'The "parse" right is required'
        self.wiki.sections = {}
        self.wiki.full_text['New entry'] = (
            '== History ==\nHistory text.\n\n== Material ==\nMaterial text.\n'
        )

        result = self.site.resolve_link('Old name#Material')

        assert result.status == 'resolved'
        assert result.section is not None
        assert result.section.heading == 'Material'
        assert result.text == '== Material ==\nMaterial text.\n'

    def test_unreadable_section_index_is_not_reported_as_deleted(self):
        self.wiki.parse_error = 'The "parse" right is required'
        self.wiki.sections = {}
        self.wiki.full_text = {}

        result = self.site.resolve_link('Old name#Material')

        assert result.status == 'section_unavailable'
        assert result.section_unavailable
        assert not result.section_deleted
        assert result.text is None
        assert 'could not be read' in (result.reason or '')

    def test_read_failure_is_not_reported_as_empty_section(self):
        with unittest.mock.patch.object(
            mwclient.page.Page, 'text',
            side_effect=mwclient.errors.APIError('readapidenied', 'no read', None),
        ):
            result = self.site.resolve_link('Old name#Material')

        assert result.status == 'resolved'
        assert result.text is None
        assert (result.text_error or '').startswith('readapidenied')

    def test_section_index_is_cached_between_resolutions(self):
        resolver = LinkResolver(self.site)
        first = resolver.resolve('Old name#Material')
        parse_calls = len([c for c in self.wiki.calls if c.get('action') == 'parse'])
        second = resolver.resolve('Old name#History')

        assert first.status == 'resolved'
        assert second.status == 'resolved'
        assert len([c for c in self.wiki.calls if c.get('action') == 'parse']) \
            == parse_calls


class DuplicateSectionTest(unittest.TestCase):

    def setUp(self) -> None:
        self.wiki = FakeWiki()
        self.wiki.add_page(
            'Entry', 3, sections=[
                {'toclevel': 1, 'level': '2', 'line': 'Intro', 'number': '1',
                 'index': '1', 'byteoffset': 0, 'anchor': 'Intro'},
                {'toclevel': 2, 'level': '3', 'line': 'Material', 'number': '2',
                 'index': '2', 'byteoffset': 20, 'anchor': 'Material'},
                {'toclevel': 2, 'level': '3', 'line': 'Material', 'number': '3',
                 'index': '3', 'byteoffset': 70, 'anchor': 'Material_2'},
            ]
        )
        self.wiki.section_texts['Entry'] = {
            1: '== Intro ==\nIntro text.\n',
            2: '== Material ==\nFirst material.\n',
            3: '== Material ==\nSecond material.\n',
        }
        self.wiki.full_text['Entry'] = (
            '== Intro ==\nIntro text.\n\n== Material ==\nFirst material.\n'
            '\n== Material ==\nSecond material.\n'
        )

        self.site = mwclient.Site('test.wikipedia.org', do_init=False)
        self.site.version = (1, 32, 0)
        self.site.rights = ['read']
        self.site.api_limit = 500

        self.responses = responses.RequestsMock(assert_all_requests_are_fired=False)
        self.responses.add_callback(
            responses.GET, API_URL, callback=self.wiki.callback
        )
        self.responses.start()

    def tearDown(self) -> None:
        self.responses.stop()
        self.responses.reset()

    def test_same_display_text_in_two_sections_is_not_silently_first(self):
        result = self.site.resolve_link('Entry#Material')

        assert result.status == STATUS_SECTION_AMBIGUOUS
        assert result.ambiguous
        assert result.text is None
        assert [section.index for section in result.candidates] == [2, 3]
        assert [section.parent for section in result.candidates] == ['Intro', 'Intro']
        assert [section.path for section in result.candidates] == [
            ('Intro', 'Material'),
            ('Intro', 'Material'),
        ]
        assert result.texts == (
            '== Material ==\nFirst material.\n',
            '== Material ==\nSecond material.\n',
        )
        assert len(set(result.texts)) == 2

        calls = [c for c in self.wiki.calls if 'rvsection' in c]
        assert {call['rvsection'] for call in calls} == {'2', '3'}

    def test_numbered_anchor_selects_the_second_duplicate(self):
        result = self.site.resolve_link('Entry#Material_2')

        assert result.status == STATUS_SECTION_NUMBERED
        assert result.numbered
        assert result.section is not None
        assert result.section.index == 3
        assert result.section.duplicate_number == 2
        assert result.text == '== Material ==\nSecond material.\n'
        assert result.text != '== Material ==\nFirst material.\n'

        revision_calls = [c for c in self.wiki.calls if 'rvsection' in c]
        assert revision_calls[-1]['rvsection'] == '3'

    def test_numbered_anchor_matching_is_case_insensitive_as_fallback(self):
        result = self.site.resolve_link('Entry#material_2')
        assert result.status == STATUS_SECTION_NUMBERED
        assert result.section is not None
        assert result.section.index == 3

    def test_first_duplicate_has_no_numbered_status_without_anchor(self):
        # The API anchor "Material" belongs to the first duplicate; without an
        # explicit number the answer is still ambiguous.
        result = self.site.resolve_link('Entry#Material')
        assert result.status == STATUS_SECTION_AMBIGUOUS
        assert result.section is None

    def test_same_heading_under_different_parents_is_distinguishable(self):
        self.wiki.add_page(
            'Split', 7, sections=[
                {'toclevel': 1, 'level': '2', 'line': 'History', 'number': '1',
                 'index': '1', 'byteoffset': 0, 'anchor': 'History'},
                {'toclevel': 2, 'level': '3', 'line': 'Material', 'number': '2',
                 'index': '2', 'byteoffset': 20, 'anchor': 'Material'},
                {'toclevel': 1, 'level': '2', 'line': 'Specification',
                 'number': '3', 'index': '3', 'byteoffset': 50,
                 'anchor': 'Specification'},
                {'toclevel': 2, 'level': '3', 'line': 'Material', 'number': '4',
                 'index': '4', 'byteoffset': 80, 'anchor': 'Material_2'},
            ]
        )
        self.wiki.section_texts['Split'] = {
            2: '== History ==\n=== Material ===\nHistorical material.\n',
            4: '== Specification ==\n=== Material ===\nSpecified material.\n',
        }

        result = self.site.resolve_link('Split#Material')

        assert result.status == STATUS_SECTION_AMBIGUOUS
        assert [section.path for section in result.candidates] == [
            ('History', 'Material'),
            ('Specification', 'Material'),
        ]
        assert result.texts == (
            '== History ==\n=== Material ===\nHistorical material.\n',
            '== Specification ==\n=== Material ===\nSpecified material.\n',
        )


class PrefetchedContentTest(unittest.TestCase):

    def test_prefetched_full_text_is_not_used_for_section_request(self):
        site: Any = unittest.mock.Mock()
        site.rights = ['read']
        site.api_limit = 500
        site.version = (1, 32, 0)
        info = {
            'title': 'Entry', 'ns': 0, 'pageid': 3,
            'revisions': [{'*': 'FULL PAGE TEXT'}],
        }
        page = Page(site, 'Entry', info=info)

        with unittest.mock.patch.object(page, 'revisions',
                                        return_value=iter([])) as revisions:
            assert page.text(section=2) == ''
            revisions.assert_called_once()
            assert revisions.call_args.kwargs['section'] == '2'

        assert page.text() == 'FULL PAGE TEXT'


if __name__ == '__main__':
    unittest.main()
