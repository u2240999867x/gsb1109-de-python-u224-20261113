import unittest
import pytest
import mwclient
from mwclient.listing import List, NestedList, GeneratorList
from mwclient.listing import Category, PageList, PageProperty, RevisionsIterator
from mwclient.listing import continuation_modules, merge_page_info
from mwclient.page import Page

import unittest.mock as mock


if __name__ == "__main__":
    print()
    print("Note: Running in stand-alone mode. Consult the README")
    print("      (section 'Contributing') for advice on running tests.")
    print()


class TestList(unittest.TestCase):

    def setUp(self):
        pass

    def setupDummyResponsesOne(self, mock_site, result_member, ns=None):
        if ns is None:
            ns = [0, 0, 0]
        mock_site.get.side_effect = [
            {
                'continue': {
                    'apcontinue': 'Kre_Mbaye',
                    'continue': '-||'
                },
                'query': {
                    result_member: [
                        {
                            "pageid": 19839654,
                            "ns": ns[0],
                            "title": "Kre'fey",
                        },
                    ]
                }
            },
            {
                'continue': {
                    'apcontinue': 'Kre_Blip',
                    'continue': '-||'
                },
                'query': {
                    result_member: [
                        {
                            "pageid": 19839654,
                            "ns": ns[1],
                            "title": "Kre-O",
                        }
                    ]
                }
            },
            {
                'query': {
                    result_member: [
                        {
                            "pageid": 30955295,
                            "ns": ns[2],
                            "title": "Kre-O Transformers",
                        }
                    ]
                }
            },
        ]

    def setupDummyResponsesTwo(self, mock_site, result_member, ns=None):
        if ns is None:
            ns = [0, 0, 0]
        mock_site.get.side_effect = [
            {
                'continue': {
                    'apcontinue': 'Kre_Mbaye',
                    'continue': '-||'
                },
                'query': {
                    result_member: [
                        {
                            "pageid": 19839654,
                            "ns": ns[0],
                            "title": "Kre'fey",
                        },
                        {
                            "pageid": 19839654,
                            "ns": ns[1],
                            "title": "Kre-O",
                        }
                    ]
                }
            },
            {
                'query': {
                    result_member: [
                        {
                            "pageid": 30955295,
                            "ns": ns[2],
                            "title": "Kre-O Transformers",
                        }
                    ]
                }
            },
        ]

    @mock.patch('mwclient.client.Site')
    def test_list_continuation(self, mock_site):
        # Test that the list fetches all three responses
        # and yields dicts when return_values not set

        lst = List(mock_site, 'allpages', 'ap', api_chunk_size=2)
        self.setupDummyResponsesTwo(mock_site, 'allpages')
        vals = [x for x in lst]

        assert len(vals) == 3
        assert type(vals[0]) == dict
        assert lst.args["aplimit"] == "2"
        assert mock_site.get.call_count == 2

    @mock.patch('mwclient.client.Site')
    def test_list_limit_deprecated(self, mock_site):
        # Test that the limit arg acts as api_chunk_size but generates
        # DeprecationWarning

        with pytest.deprecated_call():
            lst = List(mock_site, 'allpages', 'ap', limit=2)
        self.setupDummyResponsesTwo(mock_site, 'allpages')
        vals = [x for x in lst]

        assert len(vals) == 3
        assert type(vals[0]) == dict
        assert lst.args["aplimit"] == "2"
        assert mock_site.get.call_count == 2

    @mock.patch('mwclient.client.Site')
    def test_list_max_items(self, mock_site):
        # Test that max_items properly caps the list
        # iterations

        mock_site.api_limit = 500
        lst = List(mock_site, 'allpages', 'ap', max_items=2)
        self.setupDummyResponsesTwo(mock_site, 'allpages')
        vals = [x for x in lst]

        assert len(vals) == 2
        assert type(vals[0]) == dict
        assert lst.args["aplimit"] == "2"
        assert mock_site.get.call_count == 1

    @mock.patch('mwclient.client.Site')
    def test_list_max_items_continuation(self, mock_site):
        # Test that max_items and api_chunk_size work together

        mock_site.api_limit = 500
        lst = List(mock_site, 'allpages', 'ap', max_items=2, api_chunk_size=1)
        self.setupDummyResponsesOne(mock_site, 'allpages')
        vals = [x for x in lst]

        assert len(vals) == 2
        assert type(vals[0]) == dict
        assert lst.args["aplimit"] == "1"
        assert mock_site.get.call_count == 2

    @mock.patch('mwclient.client.Site')
    def test_list_with_str_return_value(self, mock_site):
        # Test that the List yields strings when return_values is string

        lst = List(mock_site, 'allpages', 'ap', limit=2, return_values='title')
        self.setupDummyResponsesTwo(mock_site, 'allpages')
        vals = [x for x in lst]

        assert len(vals) == 3
        assert type(vals[0]) == str

    @mock.patch('mwclient.client.Site')
    def test_list_with_tuple_return_value(self, mock_site):
        # Test that the List yields tuples when return_values is tuple

        lst = List(mock_site, 'allpages', 'ap', limit=2,
                   return_values=('title', 'ns'))
        self.setupDummyResponsesTwo(mock_site, 'allpages')
        vals = [x for x in lst]

        assert len(vals) == 3
        assert type(vals[0]) == tuple

    @mock.patch('mwclient.client.Site')
    def test_list_empty(self, mock_site):
        # Test that we handle an empty response from get correctly
        # (stop iterating)

        lst = List(mock_site, 'allpages', 'ap', limit=2,
                   return_values=('title', 'ns'))
        mock_site.get.side_effect = [{}]
        vals = [x for x in lst]
        assert len(vals) == 0

    @mock.patch('mwclient.client.Site')
    def test_list_invalid(self, mock_site):
        # Test that we handle the response for a list that doesn't
        # exist correctly (set an empty iterator, then stop
        # iterating)

        mock_site.api_limit = 500
        lst = List(mock_site, 'allpagess', 'ap')
        mock_site.get.side_effect = [
            {
                'batchcomplete': '',
                'warnings': {
                    'main': {'*': 'Unrecognized parameter: aplimit.'},
                    'query': {'*': 'Unrecognized value for parameter "list": allpagess'}
                },
                'query': {
                    'userinfo': {
                        'id': 0,
                        'name': 'DEAD:BEEF:CAFE',
                        'anon': ''
                    }
                }
            }
        ]
        vals = [x for x in lst]
        assert len(vals) == 0

    @mock.patch('mwclient.client.Site')
    def test_list_repr(self, mock_site):
        # Test __repr__ of a List is as expected

        mock_site.__str__.return_value = "some wiki"
        lst = List(mock_site, 'allpages', 'ap', limit=2,
                   return_values=('title', 'ns'))
        assert repr(lst) == "<List object 'allpages' for some wiki>"

    @mock.patch('mwclient.client.Site')
    def test_get_list(self, mock_site):
        # Test get_list behaves as expected

        lst = List.get_list()(mock_site, 'allpages', 'ap', limit=2,
                              return_values=('title', 'ns'))
        genlst = List.get_list(True)(mock_site, 'allpages', 'ap', limit=2,
                                     return_values=('title', 'ns'))
        assert isinstance(lst, List)
        assert not isinstance(lst, GeneratorList)
        assert isinstance(genlst, GeneratorList)

    @mock.patch('mwclient.client.Site')
    def test_nested_list(self, mock_site):
        # Test NestedList class works as expected

        mock_site.api_limit = 500
        nested = NestedList('entries', mock_site, 'checkuserlog', 'cul')
        mock_site.get.side_effect = [
            # this is made-up because I do not have permissions on any
            # wiki with this extension installed and the extension doc
            # does not show a sample API response
            {
                'query': {
                    'checkuserlog': {
                        'entries': [
                            {
                                'user': 'Dreamyjazz',
                                'action': 'users',
                                'ip': '172.18.0.1',
                                'message': 'suspected sockpuppet',
                                'time': 1662328680
                            },
                            {
                                'user': 'Dreamyjazz',
                                'action': 'ip',
                                'targetuser': 'JohnDoe124',
                                'message': 'suspected sockpuppet',
                                'time': 1662328380
                            },
                        ]
                    }
                }
            }
        ]
        vals = [x for x in nested]
        assert len(vals) == 2
        assert vals[0]['action'] == 'users'
        assert vals[1]['action'] == 'ip'

    @mock.patch('mwclient.client.Site')
    def test_generator_list(self, mock_site):
        # Test that the GeneratorList yields Page objects

        mock_site.api_limit = 500
        lst = GeneratorList(mock_site, 'pages', 'p')
        self.setupDummyResponsesTwo(mock_site, 'pages', ns=[0, 6, 14])
        vals = [x for x in lst]

        assert len(vals) == 3
        assert type(vals[0]) == mwclient.page.Page
        assert type(vals[1]) == mwclient.image.Image
        assert type(vals[2]) == mwclient.listing.Category

    @mock.patch('mwclient.client.Site')
    def test_pagelist_with_content(self, mock_site):
        mock_site.__str__.return_value = 'some wiki'
        mock_site.api_limit = 500
        mock_site.get.return_value = {
            "query": {
                "pages": {
                    "1886677": {
                        "pageid": 1886677,
                        "ns": 0,
                        "title": "Test",
                        "contentmodel": "wikitext",
                        "pagelanguage": "en",
                        "pagelanguagehtmlcode": "en",
                        "pagelanguagedir": "ltr",
                        "touched": "2026-02-23T06:55:21Z",
                        "lastrevid": 6267986,
                        "length": 38,
                        "redirect": "",
                        "new": "",
                        "protection": [],
                        "restrictiontypes": [
                            "edit",
                            "move"
                        ],
                        "revisions": [
                            {
                                "contentmodel": "wikitext",
                                "contentformat": "text/x-wiki",
                                "*": "test content"
                            }
                        ]
                    },
                }
            }
        }

        gl = GeneratorList(mock_site, 'pages', 'p', prop='info|imageinfo|revisions')
        assert gl.args['prop'] == 'info|imageinfo|revisions'

        vals = [x for x in gl];
    
        assert len(vals) == 1
        assert type(vals[0]) == mwclient.page.Page
        assert vals[0].text() == 'test content'

    @mock.patch('mwclient.client.Site')
    def test_category(self, mock_site):
        # Test that Category works as expected

        mock_site.__str__.return_value = "some wiki"
        mock_site.api_limit = 500
        # first response is for Page.__init__ as Category inherits
        # from both Page and GeneratorList, second response is for
        # the Category treated as an iterator with the namespace
        # filter applied, third response is for the Category.members()
        # call without a namespace filter
        mock_site.get.side_effect = [
            {
                'query': {
                    'pages': {
                        '54565': {
                            'pageid': 54565, 'ns': 14, 'title': 'Category:Furry things'
                        }
                    }
                }
            },
            {
                'query': {
                    'pages': {
                        '36245': {
                            'pageid': 36245,
                            'ns': 118,
                            'title': 'Draft:Cat'
                        },
                        '36275': {
                            'pageid': 36275,
                            'ns': 118,
                            'title': 'Draft:Dog'
                        }
                    }
                }
            },
            {
                'query': {
                    'pages': {
                        '36245': {
                            'pageid': 36245,
                            'ns': 118,
                            'title': 'Draft:Cat'
                        },
                        '36275': {
                            'pageid': 36275,
                            'ns': 118,
                            'title': 'Draft:Dog'
                        },
                        '36295': {
                            'pageid': 36295,
                            'ns': 0,
                            'title': 'Hamster'
                        }
                    }
                }
            },
        ]

        cat = Category(mock_site, 'Category:Furry things', namespace=118)
        assert repr(cat) == "<Category object 'Category:Furry things' for some wiki>"
        assert cat.args['gcmnamespace'] == 118
        vals = [x for x in cat]
        assert len(vals) == 2
        assert vals[0].name == "Draft:Cat"
        newcat = cat.members()
        assert 'gcmnamespace' not in newcat.args
        vals = [x for x in newcat]
        assert len(vals) == 3
        assert vals[2].name == "Hamster"

    @mock.patch('mwclient.client.Site')
    def test_pagelist(self, mock_site):
        # Test that PageList works as expected
        mock_site.__str__.return_value = "some wiki"
        mock_site.api_limit = 500
        mock_site.namespaces = {0: "", 6: "Image", 14: "Category"}
        mock_site.get.return_value = {
            'query': {
                'pages': {
                    '8052484': {
                        'pageid': 8052484, 'ns': 0, 'title': 'Impossible'
                    }
                }
            }
        }
        pl = PageList(mock_site, start="Herring", end="Marmalade")
        assert pl.args["gapfrom"] == "Herring"
        assert pl.args["gapto"] == "Marmalade"
        pg = pl["Impossible"]
        assert isinstance(pg, Page)
        assert mock_site.get.call_args[0] == ("query",)
        assert mock_site.get.call_args[1]["titles"] == "Impossible"
        # covers the catch of AttributeError in get()
        pg = pl[8052484]
        assert isinstance(pg, Page)
        assert mock_site.get.call_args[0] == ("query",)
        assert mock_site.get.call_args[1]["pageids"] == 8052484
        pg = pl["Category:Spreads"]
        assert mock_site.get.call_args[1]["titles"] == "Category:Spreads"
        assert isinstance(pg, Category)
        pl = PageList(mock_site, prefix="Ham")
        assert pl.args["gapprefix"] == "Ham"

    @mock.patch('mwclient.client.Site')
    def test_revisions_iterator(self, mock_site):
        # Test RevisionsIterator, including covering a line of
        # PageProperty.set_iter
        mock_site.api_limit = 500
        mock_site.get.return_value = {
            'query': {
                'pages': {
                    '8052484': {
                        'pageid': 8052484,
                        'ns': 0,
                        'title': 'Impossible',
                        'revisions': [
                            {
                                "revid": 5000,
                                "parentid": 4999,
                                "user": "Bob",
                                "comment": "an edit"
                            },
                            {
                                "revid": 4999,
                                "parentid": 4998,
                                "user": "Alice",
                                "comment": "an earlier edit"
                            }
                        ]
                    }
                }
            }
        }
        page = mock.MagicMock()
        page.site = mock_site
        page.name = "Impossible"
        rvi = RevisionsIterator(
            page, "revisions", "rv", rvstartid=5, rvstart="2001-01-15T14:56:00Z"
        )
        assert "rvstart" in rvi.args and "rvstartid" in rvi.args
        vals = [x for x in rvi]
        assert "rvstart" not in rvi.args and "rvstartid" in rvi.args
        assert len(vals) == 2
        assert vals[0]["comment"] == "an edit"
        assert vals[1]["comment"] == "an earlier edit"
        # now test the StopIteration line in PageProperty.set_iter
        # by mocking a return value for a different page
        mock_site.get.return_value = {
            'query': {
                'pages': {
                    '8052485': {
                        'pageid': '8052485',
                        'ns': 0,
                        'title': 'Impractical'
                    }
                }
            }
        }
        rvi = RevisionsIterator(
            page, "revisions", "rv", rvstartid=5, rvstart="2001-01-15T14:56:00Z"
        )
        vals = [x for x in rvi]
        assert len(vals) == 0


class TestContinuationModules:

    def test_attribute_tokens_to_properties(self):
        # Tokens are split per property instead of kept as one blob
        modules = continuation_modules(
            {'clcontinue': 'A|B', 'elcontinue': 'C', 'continue': '-||'},
            prefix='el', list_name='extlinks'
        )
        assert modules == {'categories': 'A|B', 'extlinks': 'C'}

    def test_listing_token_kept_distinct(self):
        # The listing's own branch is not confused with a page property
        modules = continuation_modules(
            {'gcmcontinue': 'X', 'continue': '-||'},
            prefix='cm', list_name='categorymembers'
        )
        assert modules == {'categorymembers': 'X'}

    def test_unknown_token_stays_visible(self):
        # Extension tokens keep their own module name instead of becoming the
        # listing's next-page token
        modules = continuation_modules(
            {'xyzcontinue': 'X', 'continue': '-||'}, prefix='ap', list_name='allpages'
        )
        assert modules == {'xyz': 'X'}

    def test_empty_continuation(self):
        assert continuation_modules(None) == {}
        assert continuation_modules({}) == {}


class TestMergePageInfo:

    def test_lists_are_joined_and_deduplicated(self):
        target = {'title': 'A', 'categories': [{'title': 'C1'}]}
        merge_page_info(target, {'categories': [{'title': 'C1'}, {'title': 'C2'}]})
        assert target['categories'] == [{'title': 'C1'}, {'title': 'C2'}]

    def test_absent_keys_do_not_clear_values(self):
        target = {'title': 'A', 'extlinks': [{'*': 'u1'}]}
        merge_page_info(target, {'title': 'A'})
        assert target['extlinks'] == [{'*': 'u1'}]

    def test_explicit_empty_list_is_kept(self):
        target = {'title': 'A', 'categories': [{'title': 'C1'}]}
        merge_page_info(target, {'categories': []})
        assert target['categories'] == [{'title': 'C1'}]

    def test_repeated_revision_is_merged_by_revid(self):
        target = {'title': 'A', 'revisions': [{'revid': 5, 'user': 'Bob'}]}
        merge_page_info(
            target, {'revisions': [{'revid': 5, 'user': 'Bob', '*': 'text'}]}
        )
        assert target['revisions'] == [
            {'revid': 5, 'user': 'Bob', '*': 'text'}
        ]


class TestPropertyContinuation:

    @mock.patch('mwclient.client.Site')
    def test_property_continuation_and_completion(self, mock_site):
        # A property split over several responses yields every member once,
        # even when a retried chunk repeats the boundary member.
        mock_site.api_limit = 500
        page = mock.MagicMock()
        page.site = mock_site
        page.name = 'P'
        mock_site.get.side_effect = [
            {
                'continue': {'elcontinue': 'X', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'title': 'P', 'extlinks': [{'*': 'u1'}]
                }}}
            },
            {
                'continue': {'elcontinue': 'Y', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'title': 'P', 'extlinks': [{'*': 'u1'}, {'*': 'u2'}]
                }}}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {'1': {
                    'title': 'P', 'extlinks': [{'*': 'u3'}]
                }}}
            },
        ]
        lst = PageProperty(page, 'extlinks', 'el', return_values='*')
        assert list(lst) == ['u1', 'u2', 'u3']
        assert lst.complete is True
        assert lst.property_status('extlinks').complete is True

    @mock.patch('mwclient.client.Site')
    def test_categories_complete_while_other_property_continues(self, mock_site):
        # The response only continues extlinks.  Categories must be reported
        # complete instead of copying the batch's pending state.
        mock_site.api_limit = 500
        mock_site.get.side_effect = [
            {
                'continue': {'elcontinue': 'X', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'title': 'P',
                    'categories': [{'title': 'Category:A'}],
                    'extlinks': [{'*': 'u1'}]
                }}}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {'1': {'title': 'P', 'extlinks': [{'*': 'u2'}]}}}
            },
        ]
        lst = List(mock_site, 'extlinks', 'el', titles='P',
                   prop='categories|extlinks')
        list(lst)
        assert lst.property_status('categories').complete is True
        assert lst.property_status('categories').basis == 'batchcomplete'
        assert lst.property_status('extlinks').complete is True
        assert lst.complete is True

    @mock.patch('mwclient.client.Site')
    def test_incomplete_property_is_reported(self, mock_site):
        mock_site.api_limit = 500
        page = mock.MagicMock()
        page.site = mock_site
        page.name = 'P'
        mock_site.get.side_effect = [
            {
                'continue': {'elcontinue': 'X', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'title': 'P', 'extlinks': [{'*': 'u1'}]
                }}}
            },
        ]
        lst = PageProperty(page, 'extlinks', 'el', return_values='*')
        assert next(lst) == 'u1'
        assert lst.complete is False
        assert lst.incomplete_properties == ['extlinks']
        assert lst.property_status('extlinks').continuation == 'X'
        assert 'X' in lst.args.values()

    @mock.patch('mwclient.client.Site')
    def test_finished_module_token_is_dropped(self, mock_site):
        # Once categories stop continuing, its token must not be resent
        mock_site.api_limit = 500
        mock_site.get.side_effect = [
            {
                'continue': {'clcontinue': 'A', 'elcontinue': 'X', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'title': 'P',
                    'categories': [{'title': 'Category:A'}],
                    'extlinks': [{'*': 'u1'}]
                }}}
            },
            {
                'continue': {'elcontinue': 'Y', 'continue': '-||'},
                'query': {'pages': {'1': {'title': 'P', 'extlinks': [{'*': 'u2'}]}}}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {'1': {'title': 'P', 'extlinks': [{'*': 'u3'}]}}}
            },
        ]
        lst = List(mock_site, 'extlinks', 'el', titles='P',
                   prop='categories|extlinks')
        list(lst)
        assert 'clcontinue' not in lst.args
        assert 'elcontinue' not in lst.args


class TestGeneratorPageMerging:

    def _run(self, responses, prop='info|categories|extlinks'):
        site = mock.MagicMock()
        site.api_limit = 500
        site.__str__.return_value = 'some wiki'
        site.get.side_effect = responses
        gl = GeneratorList(site, 'pages', 'p', prop=prop)
        return gl, list(gl)

    def test_later_pages_keep_members_from_the_final_chunk(self):
        # Page B first appears in an intermediate chunk without extlinks; the
        # final chunk carries its extlinks.  Pending pages must not be
        # released before the whole chunk that completes the batch is read.
        responses = [
            {
                'continue': {'elcontinue': 'E1', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'pageid': 1, 'ns': 0, 'title': 'A',
                    'categories': [{'title': 'C1'}],
                    'extlinks': [{'*': 'x1'}]
                }}}
            },
            {
                'continue': {'elcontinue': 'E2', 'continue': '-||'},
                'query': {'pages': {
                    '1': {'pageid': 1, 'ns': 0, 'title': 'A',
                          'extlinks': [{'*': 'x2'}]},
                    '2': {'pageid': 2, 'ns': 0, 'title': 'B',
                          'categories': [{'title': 'C2'}]},
                }}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {
                    '1': {'pageid': 1, 'ns': 0, 'title': 'A',
                          'extlinks': [{'*': 'x3'}]},
                    '2': {'pageid': 2, 'ns': 0, 'title': 'B',
                          'extlinks': [{'*': 'y1'}]},
                }}
            },
        ]
        _, pages = self._run(responses)
        by_name = {page.name: page for page in pages}
        assert by_name['A']._info['extlinks'] == [
            {'*': 'x1'}, {'*': 'x2'}, {'*': 'x3'}
        ]
        assert by_name['B']._info['extlinks'] == [{'*': 'y1'}]
        assert by_name['B']._info['categories'] == [{'title': 'C2'}]

    def test_pages_hold_back_until_properties_complete(self):
        # Page A arrives in three pieces while extlinks continues.  It must
        # be yielded once, after the last piece, with all members merged.
        responses = [
            {
                'continue': {'elcontinue': 'X', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'pageid': 1, 'ns': 0, 'title': 'A',
                    'categories': [{'title': 'C1'}],
                    'extlinks': [{'*': 'a1'}]
                }}}
            },
            {
                'continue': {'elcontinue': 'Y', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'pageid': 1, 'ns': 0, 'title': 'A',
                    'extlinks': [{'*': 'a2'}]
                }}}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {'1': {
                    'pageid': 1, 'ns': 0, 'title': 'A',
                    'extlinks': [{'*': 'a3'}]
                }}}
            },
        ]
        gl, pages = self._run(responses)
        assert [page.name for page in pages] == ['A']
        assert pages[0]._info['categories'] == [{'title': 'C1'}]
        assert pages[0]._info['extlinks'] == [
            {'*': 'a1'}, {'*': 'a2'}, {'*': 'a3'}
        ]
        assert pages[0].completion_info['complete'] is True

    def test_two_pagination_styles_deliver_same_page(self):
        # The split style and the single-response style must produce the
        # same property members and the same set of property names.
        split = [
            {
                'continue': {'elcontinue': 'X', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'pageid': 1, 'ns': 0, 'title': 'A',
                    'categories': [{'title': 'C1'}],
                    'extlinks': [{'*': 'a1'}]
                }}}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {'1': {
                    'pageid': 1, 'ns': 0, 'title': 'A',
                    'extlinks': [{'*': 'a2'}, {'*': 'a3'}]
                }}}
            },
        ]
        whole = [{
            'batchcomplete': '',
            'query': {'pages': {'1': {
                'pageid': 1, 'ns': 0, 'title': 'A',
                'categories': [{'title': 'C1'}],
                'extlinks': [{'*': 'a1'}, {'*': 'a2'}, {'*': 'a3'}]
            }}}
        }]
        split_pages = self._run(split)[1]
        whole_pages = self._run(whole)[1]
        assert len(split_pages) == len(whole_pages) == 1
        assert split_pages[0]._info['categories'] == \
            whole_pages[0]._info['categories']
        assert split_pages[0]._info['extlinks'] == \
            whole_pages[0]._info['extlinks']
        assert set(split_pages[0].completion_info['properties']) == \
            set(whole_pages[0].completion_info['properties'])

    def test_partial_page_is_not_reported_complete(self):
        # When the response stream stops while a property still continues,
        # the page is delivered but explicitly marked incomplete instead of
        # pretending to be a finished record.
        responses = [
            {
                'continue': {'elcontinue': 'X', 'continue': '-||'},
                'query': {'pages': {'1': {
                    'pageid': 1, 'ns': 0, 'title': 'A',
                    'extlinks': [{'*': 'a1'}]
                }}}
            },
        ]
        site = mock.MagicMock()
        site.api_limit = 500
        site.get.side_effect = list(responses)
        gl = GeneratorList(site, 'pages', 'p', prop='info|extlinks')
        pages = list(gl)
        assert [page.name for page in pages] == ['A']
        assert pages[0].completion_info['complete'] is False
        status = pages[0].completion_info['properties']['extlinks']
        assert status['complete'] is False
        assert status['continuation'] == 'X'

    def test_empty_property_is_explicit(self):
        # A property that is known to be empty carries an explicit empty
        # marker instead of looking the same as one that is merely absent.
        responses = [{
            'batchcomplete': '',
            'query': {'pages': {'1': {
                'pageid': 1, 'ns': 0, 'title': 'A', 'categories': []
            }}}
        }]
        _, pages = self._run(responses, prop='info|categories')
        status = pages[0].completion_info['properties']['categories']
        assert status['complete'] is True
        assert status['present'] is True
        assert status['empty'] is True

    def test_retried_chunk_does_not_duplicate_pages(self):
        # A continuation chunk retried after a transient failure repeats a
        # page that was already delivered.  It must not appear twice.
        responses = [
            {
                'continue': {'gcmcontinue': 'C1', 'continue': '-||'},
                'query': {'pages': {'1': {'pageid': 1, 'ns': 0, 'title': 'A'}}}
            },
            {
                'continue': {'gcmcontinue': 'C2', 'continue': '-||'},
                'query': {'pages': {'1': {'pageid': 1, 'ns': 0, 'title': 'A'}}}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {'2': {'pageid': 2, 'ns': 0, 'title': 'B'}}}
            },
        ]
        _, pages = self._run(responses, prop='info')
        assert [page.name for page in pages] == ['A', 'B']

    def test_listing_continuation_does_not_block_pages(self):
        # A continuation of the branch itself only means more pages follow.
        # It must not hold earlier pages back or label them partial.
        responses = [
            {
                'continue': {'gcmcontinue': 'C1', 'continue': '-||'},
                'query': {'pages': {
                    '1': {'pageid': 1, 'ns': 0, 'title': 'A'},
                    '2': {'pageid': 2, 'ns': 0, 'title': 'B'},
                }}
            },
            {
                'continue': {'gcmcontinue': 'C2', 'continue': '-||'},
                'query': {'pages': {'3': {'pageid': 3, 'ns': 0, 'title': 'C'}}}
            },
            {
                'batchcomplete': '',
                'query': {'pages': {'4': {'pageid': 4, 'ns': 0, 'title': 'D'}}}
            },
        ]
        _, pages = self._run(responses, prop='info')
        assert [page.name for page in pages] == ['A', 'B', 'C', 'D']
        for page in pages:
            assert page.completion_info['complete'] is True


class TestGetListingArgs:

    @pytest.mark.parametrize('prefix, generator, args, expected', [
        ('au', False, { 'from': 'Test', 'limit': 500, 'exclude': None }, { 'aufrom': 'Test', 'aulimit': 500 }),
        ('ap', True, { 'from': 'Test', 'limit': 500, 'other': False }, { 'gapfrom': 'Test', 'gaplimit': 500 })
    ])
    def test_get_listing_args(self, prefix, generator, args, expected):
        assert List.get_listing_args(prefix, generator, args) == expected

    @pytest.mark.parametrize('prefix, generator, with_content, args, expected', [
        ('ap', True, False, { 'from': 'Test', 'limit': 500, 'other': False }, { 'gapfrom': 'Test', 'gaplimit': 500 }),
        ('ap', True, True, { 'from': 'Test', 'limit': 500, 'other': False }, {
            'gapfrom': 'Test',
            'gaplimit': 500,
            'prop': 'info|imageinfo|revisions',
            'rvprop': 'content'
            }),
        ('bl', False, False, { 'from': 'Test', 'limit': 500, 'other': False }, { 'blfrom': 'Test', 'bllimit': 500 }),
        ('bl', False, True, { 'from': 'Test', 'limit': 500, 'other': False }, {
            'blfrom': 'Test',
            'bllimit': 500,
            'prop': 'info|imageinfo|revisions',
            'rvprop': 'content'
            })
    ])
    def test_get_page_listing_args(self, prefix, generator, with_content, args, expected):
        assert List.get_page_listing_args(prefix, generator, with_content, args) == expected

if __name__ == '__main__':
    unittest.main()
