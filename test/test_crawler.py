import pytest

from mwclient import CategoryCrawler


class FakeSite:
    """Minimal in-memory stand-in for mwclient.Site.

    Serves category member listings and page texts from fixed dicts and
    counts requests so tests can assert that nothing is fetched twice.
    """

    api_limit = 500
    namespaces = {0: '', 14: 'Category'}
    rights = ['read']
    version = (1, 39, 0)

    def __init__(self, members, contents):
        self.members = members
        self.contents = contents
        self.member_requests = {}
        self.content_requests = {}

    def get(self, action, *args, **kwargs):
        if args and isinstance(args[0], tuple):
            kind, list_name = args[0]
            params = dict(args[1:])
            if list_name == 'categorymembers':
                title = params['cmtitle']
                self.member_requests[title] = \
                    self.member_requests.get(title, 0) + 1
                return {'query': {
                    'categorymembers': list(self.members.get(title, []))
                }}
            if list_name == 'revisions':
                title = params['titles']
                self.content_requests[title] = \
                    self.content_requests.get(title, 0) + 1
                return {'query': {'pages': {'1': {
                    'title': title,
                    'revisions': [{
                        'timestamp': '2024-01-01T00:00:00Z',
                        '*': self.contents[title],
                    }],
                }}}}
        if kwargs.get('prop') == 'info':
            title = kwargs['titles']
            namespace = 14 if title.startswith('Category:') else 0
            return {'query': {'pages': {'1': {'title': title, 'ns': namespace}}}}
        raise AssertionError(f'unexpected get: {args} {kwargs}')


def category(title):
    return {'title': f'Category:{title}', 'ns': 14}


def page(title):
    return {'title': title, 'ns': 0}


@pytest.fixture
def wiki():
    members = {
        'Category:农业馆': [category('作物'), page('水稻')],
        'Category:地方志馆': [category('作物'), page('县志')],
        'Category:作物': [
            page('水稻'),
            page('小麦'),
            category('农业馆'),  # cycle back to the root
        ],
        'Category:空馆': [],
        'Category:深库': [page('深层页')],
        'Category:交叉馆': [category('作物'), page('交叉页')],
    }
    contents = {
        '水稻': '水稻正文',
        '小麦': '小麦正文',
        '县志': '县志正文',
        '深层页': '深层页正文',
        '交叉页': '交叉页正文',
    }
    return FakeSite(members, contents)


def test_shared_pages_fetched_once_but_attributed_to_each_root(wiki):
    crawler = CategoryCrawler(
        wiki, ['Category:农业馆', 'Category:地方志馆'], max_depth=3
    )
    result = crawler.crawl()

    # The shared page is fetched only once ...
    assert wiki.content_requests['水稻'] == 1
    assert result.content['水稻'] == '水稻正文'

    # ... yet the catalog lists the shortest path from every root.
    assert result.catalog['水稻'] == {
        'Category:农业馆': [('Category:农业馆', '水稻')],
        'Category:地方志馆': [('Category:地方志馆', 'Category:作物', '水稻')],
    }
    assert result.catalog['小麦'] == {
        'Category:农业馆': [('Category:农业馆', 'Category:作物', '小麦')],
        'Category:地方志馆': [('Category:地方志馆', 'Category:作物', '小麦')],
    }

    # The shared subcategory listing is requested only once as well.
    assert wiki.member_requests['Category:作物'] == 1


def test_cycles_do_not_revisit_ancestors(wiki):
    crawler = CategoryCrawler(wiki, ['Category:农业馆'], max_depth=5)
    result = crawler.crawl()

    # The cycle 作物 -> 农业馆 is cut: the root keeps its own short path.
    assert result.catalog['Category:农业馆'] == {
        'Category:农业馆': [('Category:农业馆',)]
    }
    assert result.catalog['Category:作物'] == {
        'Category:农业馆': [('Category:农业馆', 'Category:作物')]
    }


def test_all_tied_shortest_paths_are_listed():
    members = {
        'Category:R': [category('X'), category('Y')],
        'Category:X': [page('P')],
        'Category:Y': [page('P')],
    }
    wiki = FakeSite(members, {'P': 'text'})
    result = CategoryCrawler(wiki, ['Category:R'], max_depth=3).crawl()

    assert result.catalog['P'] == {
        'Category:R': [
            ('Category:R', 'Category:X', 'P'),
            ('Category:R', 'Category:Y', 'P'),
        ]
    }
    assert wiki.content_requests['P'] == 1


def test_only_shortest_paths_are_listed():
    members = {
        'Category:R': [category('X'), page('P')],
        'Category:X': [page('P')],
    }
    wiki = FakeSite(members, {'P': 'text'})
    result = CategoryCrawler(wiki, ['Category:R'], max_depth=3).crawl()

    assert result.catalog['P'] == {'Category:R': [('Category:R', 'P')]}


def test_unexpanded_and_empty_are_delivered_separately(wiki):
    members = {
        'Category:农业馆': [category('空馆'), category('作物')],
        'Category:空馆': [],
        'Category:作物': [category('深库'), page('水稻')],
        'Category:深库': [page('深层页')],
    }
    wiki = FakeSite(members, wiki.contents)

    result = CategoryCrawler(wiki, ['Category:农业馆'], max_depth=2).crawl()

    assert result.unexpanded == {'Category:深库'}
    assert result.empty == {'Category:空馆'}
    assert result.unexpanded.isdisjoint(result.empty)
    # The depth-limited category is never expanded.
    assert 'Category:深库' not in wiki.member_requests
    assert '深层页' not in result.catalog


def test_later_root_can_expand_previously_unexpanded_category(wiki):
    members = dict(wiki.members)
    members['Category:农业馆'] = [category('深库')]
    wiki = FakeSite(members, wiki.contents)

    crawler = CategoryCrawler(wiki, ['Category:农业馆'], max_depth=1)
    result = crawler.crawl()
    assert result.unexpanded == {'Category:深库'}

    crawler.add_root('Category:深库')
    result = crawler.crawl()
    assert result.unexpanded == set()
    assert result.catalog['深层页'] == {
        'Category:深库': [('Category:深库', '深层页')]
    }


def test_new_root_reuses_cache_without_losing_attribution(wiki):
    crawler = CategoryCrawler(
        wiki, ['Category:农业馆', 'Category:地方志馆'], max_depth=3
    )
    crawler.crawl()
    assert wiki.member_requests['Category:作物'] == 1
    assert wiki.content_requests['小麦'] == 1

    crawler.add_root('Category:交叉馆')
    result = crawler.crawl()

    # Cached listings and texts are reused: no new requests for old pages.
    assert wiki.member_requests['Category:作物'] == 1
    assert wiki.content_requests['小麦'] == 1

    # ... but pages reachable from the new root are still attributed to it.
    assert result.catalog['小麦']['Category:交叉馆'] == [
        ('Category:交叉馆', 'Category:作物', '小麦')
    ]
    assert result.catalog['交叉页'] == {
        'Category:交叉馆': [('Category:交叉馆', '交叉页')]
    }
    # Attribution for the old roots is preserved.
    assert 'Category:农业馆' in result.catalog['小麦']
    assert 'Category:地方志馆' in result.catalog['小麦']


def test_root_prefix_is_normalized(wiki):
    crawler = CategoryCrawler(wiki, ['农业馆'], max_depth=1)
    assert crawler.roots == ['Category:农业馆']
    result = crawler.crawl()
    assert 'Category:农业馆' in result.categories


def test_result_splits_pages_and_categories(wiki):
    crawler = CategoryCrawler(wiki, ['Category:农业馆'], max_depth=2)
    result = crawler.crawl()

    assert '水稻' in result.pages
    assert 'Category:作物' in result.categories
    assert 'Category:作物' not in result.pages


def test_fetch_content_can_be_disabled(wiki):
    crawler = CategoryCrawler(
        wiki, ['Category:农业馆'], max_depth=2, fetch_content=False
    )
    result = crawler.crawl()

    assert result.content == {}
    assert wiki.content_requests == {}
    assert '水稻' in result.catalog
