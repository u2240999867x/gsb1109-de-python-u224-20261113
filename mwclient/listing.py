from collections import OrderedDict, deque
from typing import (  # noqa: F401
    Optional, Tuple, Any, Union, Iterator, Mapping, MutableMapping, Iterable, Type, Dict,
    Set, List as TypingList
)

import mwclient.image
import mwclient.page
from mwclient._types import Namespace
from mwclient.util import parse_timestamp, handle_limit


# Mapping from the two-letter module prefix used by the API to the name of the
# page property it belongs to.  Continuation parameters are usually named
# ``<prefix>continue`` (or ``<prefix>offset``), e.g. ``elcontinue`` for
# ``prop=extlinks``.  Knowing the prefix lets us report continuation and
# completion per property instead of as one opaque blob.
PROPERTY_PREFIXES = {
    'categories': 'cl',
    'categoryinfo': 'ci',
    'coordinates': 'co',
    'deletedrevisions': 'drv',
    'duplicatefiles': 'df',
    'extlinks': 'el',
    'fileusage': 'fu',
    'flagged': 'fl',
    'globalusage': 'gu',
    'imageinfo': 'ii',
    'images': 'im',
    'info': 'in',
    'iwlinks': 'iw',
    'langlinks': 'll',
    'links': 'pl',
    'pageimages': 'pi',
    'pageprops': 'pp',
    'pageterms': 'wb',
    'pageviews': 'pv',
    'redirects': 'rd',
    'references': 'rf',
    'revisions': 'rv',
    'templates': 'tl',
    'transcludedin': 'ti',
    'videoinfo': 'vi',
    'wblistentityusage': 'wu',
}

# Suffixes MediaWiki appends to a module prefix when building a token.
_CONTINUATION_SUFFIXES = ('continue', 'offset', 'from', 'to')

# Page properties whose value is a collection of members.  Only these have a
# meaningful "empty" answer; scalar properties such as ``info`` do not.
COLLECTION_PROPERTIES = {
    'categories', 'categoryinfo', 'coordinates', 'deletedrevisions',
    'duplicatefiles', 'extlinks', 'fileusage', 'flagged', 'globalusage',
    'imageinfo', 'images', 'iwlinks', 'langlinks', 'links', 'pageimages',
    'pageprops', 'pageterms', 'redirects', 'references', 'revisions',
    'templates', 'transcludedin', 'videoinfo', 'wblistentityusage',
}


def _freeze(value: Any) -> Any:
    """Return a hashable representation of an API value.

    API list members are typically dictionaries, which cannot be used as set
    members directly.  Converting them recursively lets us remove items that
    the API repeats at a continuation boundary without relying on object
    identity.
    """
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _item_identity(item: Any) -> Any:
    """Return a stable identity for a member of a page property list.

    Most members are distinguished by their full contents.  Revisions are
    identified by revision ID, because a later chunk can repeat a revision
    with additional fields (e.g. content) instead of a byte-identical entry.
    """
    if isinstance(item, dict) and item.get('revid') is not None:
        return ('revid', item['revid'])
    return _freeze(item)


def _merge_lists(old: TypingList[Any], new: TypingList[Any]) -> TypingList[Any]:
    """Merge two API lists, preserving order and dropping repeated members."""
    merged = list(old)
    index = {}  # type: Dict[Any, int]
    for position, item in enumerate(merged):
        index.setdefault(_item_identity(item), position)

    for item in new:
        key = _item_identity(item)
        if key in index:
            position = index[key]
            if isinstance(merged[position], dict) and isinstance(item, dict):
                merged[position] = merge_page_info(dict(merged[position]), item)
            continue
        index[key] = len(merged)
        merged.append(item)
    return merged


def merge_page_info(target: Dict[str, Any], new: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge a repeated API representation of a page into ``target``.

    MediaWiki may repeat a page across continuation chunks, each time carrying
    only the part of a property that fits in the current response.  Missing
    keys mean "no new value in this chunk", not "the property is empty", so
    old values are retained.  List values (categories, extlinks, revisions,
    ...) are joined and de-duplicated.  An explicitly empty list is kept.
    """
    for key, value in new.items():
        if key not in target:
            target[key] = value
        elif isinstance(target[key], list) and isinstance(value, list):
            target[key] = _merge_lists(target[key], value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            merged = dict(target[key])
            merged.update(value)
            target[key] = merged
        else:
            target[key] = value
    return target


class PropertyState:
    """Per-property continuation and completion information.

    Attributes:
        name: The MediaWiki page property this state describes.
        continuation: The continuation token still needed to fetch the next
            chunk, or ``None`` when the property is complete.
        complete: Whether all members of the property have been collected.
        basis: Human-readable reason for the current state, e.g.
            ``'batchcomplete'`` or ``'continuation ended'``.
    """

    __slots__ = ('name', 'continuation', 'complete', 'basis', 'empty')

    def __init__(
        self,
        name: str,
        continuation: Optional[str] = None,
        complete: bool = True,
        basis: str = 'no continuation',
        empty: Optional[bool] = None
    ) -> None:
        self.name = name
        self.continuation = continuation
        self.complete = complete
        self.basis = basis
        self.empty = empty

    @property
    def incomplete(self) -> bool:
        return not self.complete

    def __bool__(self) -> bool:
        return self.complete

    def __repr__(self) -> str:
        state = 'complete' if self.complete else 'incomplete'
        return (f"<PropertyState {self.name!r} {state} "
                f"(basis={self.basis!r}, continuation={self.continuation!r})>")

    def as_dict(self) -> Dict[str, Any]:
        data = {
            'name': self.name,
            'continuation': self.continuation,
            'complete': self.complete,
            'incomplete': self.incomplete,
            'basis': self.basis,
        }
        if self.empty is not None:
            data['empty'] = self.empty
        return data


def continuation_modules(
    continuation: Optional[Mapping[str, Any]],
    prefix: Optional[str] = None,
    list_name: Optional[str] = None,
    property_prefixes: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """Convert an API ``continue`` object to ``{module name: token}``.

    The generic ``continue`` bookkeeping key is ignored.  Tokens are attributed
    to the listing module itself or to a page property, so callers can tell
    that e.g. categories are complete while extlinks continue.

    The listing's own continuation only announces more pages and therefore
    never makes an already returned page partial.  A token that cannot be
    attributed to a known page property keeps its own stem so unknown
    extension properties stay visible instead of being mistaken for the
    listing's next page.
    """
    if not continuation:
        return {}

    property_prefixes = property_prefixes or {}
    properties_by_prefix = {
        module_prefix: name for name, module_prefix in PROPERTY_PREFIXES.items()
    }
    # Callers pass the properties this list actually requested; those take
    # precedence over the built-in table for unusual extensions.
    properties_by_prefix.update(
        {module_prefix: name for name, module_prefix in property_prefixes.items()}
    )

    own_keys = set()
    if prefix:
        own_keys = {
            prefix + 'continue',
            'g' + prefix + 'continue',
            prefix + 'offset',
            'g' + prefix + 'offset',
            prefix + 'from',
            'g' + prefix + 'from',
        }

    modules = {}  # type: Dict[str, str]
    for key, value in continuation.items():
        if key == 'continue':
            continue
        token = str(value)

        stem = key
        if stem.startswith('g'):
            stem = stem[1:]
        for suffix in _CONTINUATION_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[:-len(suffix)]
                break

        module = None
        if stem in properties_by_prefix:
            module = properties_by_prefix[stem]
        elif key in own_keys or stem == prefix or stem == list_name:
            module = list_name
        elif stem in PROPERTY_PREFIXES:
            module = PROPERTY_PREFIXES[stem]
        else:
            # Not a property this module mapping knows about.  It may belong
            # to an extension, so keep it as its own module rather than
            # assuming it is the listing's token: a page property that is
            # still streaming must not be mistaken for the listing's own
            # next page.
            module = stem or key
        modules[module] = token
    return modules


class List:
    """Base class for lazy iteration over api response content

    This is a class providing lazy iteration.  This means that the
    content is loaded in chunks as long as the response hints at
    continuing content.

    max_items limits the total number of items that will be yielded
    by this iterator. api_chunk_size sets the number of items that
    will be requested from the wiki per API call (this iterator itself
    always yields one item at a time). limit does the same as
    api_chunk_size for backward compatibility, but is deprecated due
    to its misleading name.

    Continuation is tracked per API module.  ``complete`` tells whether every
    requested property finished, ``incomplete_properties`` names the ones that
    still need another request, and ``property_status(name)`` returns the
    per-property :class:`PropertyState`.
    """

    def __init__(
        self,
        site: 'mwclient.client.Site',
        list_name: str,
        prefix: str,
        limit: Optional[int] = None,
        return_values: Union[str, Tuple[str, ...], None] = None,
        max_items: Optional[int] = None,
        api_chunk_size: Optional[int] = None,
        *args: Tuple[str, Any],
        **kwargs: Any
    ) -> None:
        # NOTE: Fix limit
        self.site = site
        self.list_name = list_name
        self.generator = 'list'
        self.prefix = prefix

        kwargs.update(args)
        self.args = kwargs

        (max_items, api_chunk_size) = handle_limit(limit, max_items, api_chunk_size)

        # for efficiency, if max_items is set and api_chunk_size is not,
        # set the chunk size to max_items so we don't retrieve
        # unneeded extra items (so long as it's below API limit)
        api_limit = site.api_limit
        api_chunk_size = api_chunk_size or min(max_items or api_limit, api_limit)
        self.args[self.prefix + 'limit'] = str(api_chunk_size)

        self.count = 0
        self.max_items = max_items

        self._iter = iter(range(0))  # type: Iterator[Any]

        self.last = False
        self.result_member = list_name
        self.return_values = return_values

        # Continuation is tracked per API module instead of as one flat bag of
        # parameters.  ``continuation`` maps a module name to the token that is
        # still needed for it, so a property that finished can be distinguished
        # from one that is still being paged through.
        self.continuation = {}  # type: Dict[str, str]
        self.property_states = {}  # type: Dict[str, PropertyState]
        self._continuation_args = set()  # type: Set[str]
        self._continued_modules = set()  # type: Set[str]
        self._saw_continuation = False
        self._started = False

    @property
    def requested_properties(self) -> TypingList[str]:
        """Page properties requested from the API, in declaration order.

        The listing or generator named by ``list_name`` is handled separately:
        its continuation only produces more pages, whereas every property in
        this list adds members to pages that were already returned.
        """
        if self.generator == 'prop':
            return [self.list_name]
        prop = self.args.get('prop')
        if not prop:
            return []
        return [name for name in str(prop).split('|') if name]

    @property
    def incomplete_properties(self) -> TypingList[str]:
        """Names of properties that still need at least one continuation."""
        return [name for name, token in self.continuation.items() if token]

    @property
    def complete(self) -> bool:
        """Whether every requested module has been fully collected."""
        if not self._started:
            return False
        return not self.continuation

    @property
    def completion_basis(self) -> str:
        """Human-readable explanation of the current completion state."""
        if not self._started:
            return 'not started'
        if self.continuation:
            return 'continuation pending: ' + ', '.join(sorted(self.continuation))
        if self._saw_continuation:
            return 'continuation ended'
        return 'batchcomplete'

    def _record_continuation(
        self, data: Mapping[str, Any], modules: Mapping[str, str]
    ) -> None:
        """Refresh per-module continuation state from one API response.

        Modules that appear in the response's ``continue`` object still have
        members to fetch.  A module that was requested but is not mentioned
        has finished, even while another module keeps the batch going.  This
        is what lets categories report completion while extlinks still
        continue.

        Each module also keeps the reason its state was reached.  A module
        that never had a token is complete because the response said so
        (``batchcomplete``); a module whose token was dropped is complete
        because its continuation ended.  Both are independent of whatever
        other modules are doing.
        """
        continuation = data.get('continue') or {}

        new_arg_keys = {key for key in continuation if key != 'continue'}
        for key in self._continuation_args - new_arg_keys:
            # A module that is no longer mentioned has finished.  Keeping its
            # token around would make the next request repeat that module.
            self.args.pop(key, None)
        self._continuation_args = new_arg_keys
        for key, value in continuation.items():
            self.args[key] = value

        if continuation:
            self._saw_continuation = True

        batch_complete = not continuation
        active = {
            name: str(token)
            for name, token in modules.items()
            if token is not None and str(token) != ''
        }
        previously_continued = set(self.continuation)
        self._continued_modules.update(active)

        names = set(self.requested_properties)
        names.update(active)
        names.add(self.list_name)

        states = {}  # type: Dict[str, PropertyState]
        for name in names:
            if name in active:
                states[name] = PropertyState(name, active[name], False, 'continuation')
            elif name in previously_continued or name in self._continued_modules:
                states[name] = PropertyState(name, None, True, 'continuation ended')
            elif batch_complete:
                states[name] = PropertyState(name, None, True, 'batchcomplete')
            else:
                # Another module is still streaming and this one has not
                # produced a token of its own.  Its members in this response
                # are final for the pages returned here.
                states[name] = PropertyState(name, None, True, 'not continued')

        self.continuation = active
        self.property_states = states

    def property_status(self, name: str) -> PropertyState:
        """Return the continuation/completion state of one property.

        Unknown properties follow the list's own state: incomplete before the
        first response, complete once no module continues.  They are never
        silently reported as finished while the list is still streaming.
        """
        if name in self.property_states:
            return self.property_states[name]
        if not self._started:
            return PropertyState(name, None, False, 'not started')
        return PropertyState(name, None, self.complete, self.completion_basis)

    @property
    def completion_state(self) -> Dict[str, Any]:
        """Completion details for every property requested by this list."""
        return {
            'complete': self.complete,
            'basis': self.completion_basis,
            'properties': {
                name: state.as_dict()
                for name, state in self.property_states.items()
            },
        }

    def __iter__(self) -> 'List':
        return self

    def __next__(self) -> Any:
        if self.max_items is not None:
            if self.count >= self.max_items:
                raise StopIteration

        # For filered lists, we might have to do several requests
        # to get the next element due to miser mode.
        # See: https://github.com/mwclient/mwclient/issues/194
        while True:
            try:
                item = next(self._iter)
                if item is not None:
                    break
            except StopIteration:
                if self.last:
                    raise
                self.load_chunk()

        self.count += 1
        if 'timestamp' in item:
            item['timestamp'] = parse_timestamp(item['timestamp'])

        if isinstance(self, GeneratorList):
            return item
        if type(self.return_values) is tuple:
            return tuple(item[i] for i in self.return_values)
        if self.return_values is not None:
            return item[self.return_values]
        return item

    def load_chunk(self) -> None:
        """Query a new chunk of data

        If the query is empty, `raise StopIteration`.

        Else, update the iterator accordingly.

        If 'continue' is in the response, it is added to `self.args`
        (new style continuation, added in MediaWiki 1.21, default
        since MediaWiki 1.26).

        Else, set `self.last` to True.
        """
        data = self.site.get(
            'query', (self.generator, self.list_name),
            *[(str(k), v) for k, v in self.args.items()]
        )
        if not data:
            # Non existent page
            raise StopIteration

        self._started = True

        # Process response if not empty.
        # See: https://github.com/mwclient/mwclient/issues/194
        if 'query' in data:
            self.set_iter(data)

        modules = continuation_modules(
            data.get('continue'), self.prefix, self.list_name,
            {name: PROPERTY_PREFIXES[name] for name in self.requested_properties
             if name in PROPERTY_PREFIXES}
        )
        self._record_continuation(data, modules)
        # Keep requesting only while some module still has a token.  A
        # bare continuation marker without a token would otherwise loop
        # forever on the same response.
        self.last = not self.continuation

    def set_iter(self, data: Mapping[str, Any]) -> None:
        """Set `self._iter` to the API response `data`."""
        if self.result_member not in data['query']:
            self._iter = iter(range(0))
        elif type(data['query'][self.result_member]) is list:
            self._iter = iter(data['query'][self.result_member])
        else:
            self._iter = iter(data['query'][self.result_member].values())

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} object '{self.list_name}' for {self.site}>"

    @staticmethod
    def generate_kwargs(
        _prefix: str, *args: Tuple[str, Any], **kwargs: Any
    ) -> Iterable[Tuple[str, Any]]:
        kwargs.update(args)
        for key, value in kwargs.items():
            if value is not None and value is not False:
                yield _prefix + key, value

    @staticmethod
    def get_listing_args(
        prefix: str, generator: bool, args: Mapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Prefix and extend arguments for List and subclasses.

        Args:
            prefix: Prefix to apply to each parameter name in `args`.
            generator: Whether this listing is an API generator.
            args: Parameters to pass to the API.
        """
        prefixed_args = {}
        prefix = List.get_prefix(prefix, generator)
        for key, value in args.items():
            if value is not None and value is not False:
                prefixed_args[prefix + key] = value

        return prefixed_args

    @staticmethod
    def get_page_listing_args(
        prefix: str, generator: bool, with_content: bool, args: Mapping[str, Any]
    ) -> MutableMapping[str, Any]:
        """Prefix and extend arguments for Lists returning pages.

        Args:
            prefix: Prefix to apply to each parameter name in `args`.
            generator: Whether this listing is an API generator.
            with_content: Whether the API should prefetch page content for returned items
            args: Parameters to pass to the API.
        """
        prefixed_args = List.get_listing_args(prefix, generator, args)

        if with_content:
            prefixed_args['rvprop'] = 'content'
            prefixed_args['prop'] = 'info|imageinfo|revisions'

        return prefixed_args

    @staticmethod
    def get_prefix(prefix: str, generator: bool = False) -> str:
        return ('g' if generator else '') + prefix

    @staticmethod
    def get_list(generator: bool = False) -> Union[Type['GeneratorList'], Type['List']]:
        return GeneratorList if generator else List


class NestedList(List):
    def __init__(self, nested_param: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.nested_param = nested_param

    def set_iter(self, data: Mapping[str, Any]) -> None:
        self._iter = iter(data['query'][self.result_member][self.nested_param])


class GeneratorList(List):
    """Lazy-loaded list of Page, Image or Category objects

    While the standard List class yields raw response data
    (optionally filtered based on the value of List.return_values),
    this subclass turns the data into Page, Image or Category objects.

    A page can appear more than once while a property is being paged
    through, each time carrying only part of that property.  Such partial
    representations are merged by page identity and held back until the
    page's properties are complete, so consumers never receive a page that
    looks final but is silently missing links or members.
    """

    def __init__(
        self,
        site: 'mwclient.client.Site',
        list_name: str,
        prefix: str,
        *args: Tuple[str, Any],
        **kwargs: Any
    ) -> None:
        super().__init__(
            site, list_name, prefix, *args, **kwargs  # type: ignore[arg-type]
        )

        self.args['g' + self.prefix + 'limit'] = self.args[self.prefix + 'limit']
        del self.args[self.prefix + 'limit']
        self.generator = 'generator'
        if 'prop' not in self.args:
            self.args['prop'] = 'info|imageinfo'
        self.args['inprop'] = 'protection'

        self.result_member = 'pages'

        self.page_class = mwclient.page.Page

        # Pages seen but not yet safe to yield, keyed by page identity.
        self._pending = OrderedDict()  # type: OrderedDict[Any, Dict[str, Any]]
        # Merged, complete pages waiting to be converted to page objects.
        self._ready = deque()  # type: deque
        # Completion details per page, attached to the yielded page objects.
        self._page_status = {}  # type: Dict[Any, Dict[str, Any]]
        # Pages already delivered.  A retried chunk can repeat one of them;
        # emitting it again would duplicate the record.
        self._yielded = set()  # type: Set[Any]

    @staticmethod
    def page_identity(info: Mapping[str, Any]) -> Any:
        """Return a stable identity for a page API representation.

        Titles are preferred because they are stable across continuation
        chunks, reachable before any page ID is known, and unaffected by the
        API repeating a page under a shifted numeric key.  Spaces and
        underscores are normalised so both spellings are the same page.  The
        page ID is the fallback for representations without a title.
        """
        title = info.get('title')
        if title is not None:
            return ('title', str(title).replace('_', ' '))
        if info.get('pageid') is not None:
            return ('pageid', info['pageid'])
        return ('object', id(info))

    def _active_properties(self) -> TypingList[str]:
        """Modules whose continuation still affects page contents.

        A continuation of the listing itself only means that more pages will
        follow; it does not make an already returned page partial.  Any other
        active module can add members to pages already seen, so pages have to
        wait for it to finish.
        """
        return [
            name for name in self.continuation
            if name != self.list_name
        ]

    def _page_complete(self) -> bool:
        return not self._active_properties()

    def _page_status_for(self, info: Mapping[str, Any]) -> Dict[str, Any]:
        """Completion details for one merged page.

        Each requested property gets its own state and reason, so a page whose
        categories are complete and whose extlinks are still streaming reports
        exactly that instead of one verdict for the whole batch.
        """
        active = set(self.continuation)
        properties = {}  # type: Dict[str, Any]
        for name in self.requested_properties:
            if name == self.list_name:
                # The branch itself still has more pages to produce.  This
                # page's own record is not partial because of it.
                basis = 'more pages follow' if name in active else self.completion_basis
                data = PropertyState(name, None, True, basis).as_dict()
                data['present'] = name in info
                data['empty'] = None
                properties[name] = data
                continue

            if name in active:
                data = PropertyState(
                    name, self.continuation.get(name), False, 'continuation'
                ).as_dict()
                # Members may still arrive, so emptiness is unknown.
                data['present'] = name in info
                data['empty'] = None
                properties[name] = data
                continue

            state = self.property_states.get(name)
            basis = state.basis if state is not None else self.completion_basis
            present = name in info
            value = info.get(name)
            data = PropertyState(name, None, True, basis).as_dict()
            data['present'] = present
            if name not in COLLECTION_PROPERTIES:
                # Scalar properties have no member set to be empty.
                data['empty'] = None
            elif present:
                data['empty'] = not bool(value)
            elif name in self.property_states:
                # The API omitted the key.  That is only proof of an empty
                # property once its continuation has ended.
                data['empty'] = not self.property_states[name].incomplete
            else:
                data['empty'] = None
            properties[name] = data
        return {
            'complete': self._page_complete(),
            'basis': self.completion_basis,
            'properties': properties,
        }

    def _flush_pending(self, complete: bool) -> None:
        """Move merged pages from ``_pending`` to ``_ready``, first-seen first.

        ``complete`` reflects whether the batch finished normally.  When the
        response stream stops early, pages are still delivered, but each one
        keeps its pending continuation and is marked incomplete rather than
        pretending to be a finished record.
        """
        for identity, merged in list(self._pending.items()):
            if identity in self._yielded:
                del self._pending[identity]
                continue
            status = self._page_status_for(merged)
            status['complete'] = complete and not self._active_properties()
            self._page_status[identity] = status
            self._ready.append((identity, merged))
            del self._pending[identity]

    def __next__(self) -> Union['mwclient.page.Page', 'mwclient.image.Image', 'Category']:
        while True:
            if self._ready:
                if self.max_items is not None and self.count >= self.max_items:
                    raise StopIteration
                identity, info = self._ready.popleft()
                if identity in self._yielded:
                    continue
                self._yielded.add(identity)
                self.count += 1
                return self._make_page(info, identity)

            try:
                info = next(self._iter)
            except StopIteration:
                if self.last:
                    self._flush_pending(complete=True)
                    if self._ready:
                        continue
                    raise
                try:
                    self.load_chunk()
                except StopIteration:
                    # The response stream ended without a final batch.  The
                    # pages seen so far are delivered, but marked incomplete
                    # because their properties may be missing members.
                    self.last = True
                    self._flush_pending(complete=False)
                    if self._ready:
                        continue
                    raise
                continue

            if info is None:
                continue

            identity = self.page_identity(info)
            if identity in self._pending:
                merged = merge_page_info(self._pending[identity], info)
            else:
                merged = dict(info)
                self._pending[identity] = merged

            if self._page_complete() and not self.last:
                # No page property is still streaming, and more chunks follow.
                # This response only announces further *pages*, so nothing
                # seen so far can gain members; release it now.
                self._flush_pending(complete=True)

    @property
    def incomplete_pages(self) -> TypingList[Dict[str, Any]]:
        """Pages delivered with at least one property still incomplete."""
        return [
            {'identity': identity, 'completion': status}
            for identity, status in self._page_status.items()
            if not status.get('complete', False)
        ]

    def _make_page(
        self, info: MutableMapping[str, Any], identity: Any
    ) -> Union['mwclient.page.Page', 'mwclient.image.Image', 'Category']:
        if info['ns'] == 14:
            page = Category(self.site, '', info)
        elif info['ns'] == 6:
            page = mwclient.image.Image(self.site, '', info)
        else:
            page = mwclient.page.Page(self.site, '', info)
        page.completion_info = self._page_status.get(
            identity, self._page_status_for(info)
        )
        return page

    def load_chunk(self) -> None:
        # Put this here so that the constructor does not fail
        # on uninitialized sites
        self.args['iiprop'] = 'timestamp|user|comment|url|size|sha1|metadata|archivename'
        return super().load_chunk()


class Category(mwclient.page.Page, GeneratorList):
    """
    Represents a category on a MediaWiki wiki represented by a
    :class:`~mwclient.client.Site` object.

    Args:
        site (mwclient.client.Site): The site object this page belongs to.
        name (Union[str, int, Page]): The title of the page, the page ID, or
            another :class:`Page` object to copy.
        info (Optional[dict]): Page info, if already fetched, e.g., when
            iterating over a list of pages. If not provided, the page info
            will be fetched from the API.
        namespace (Union[int, str, None]): The namespace of the category
            members to list.
    """

    def __init__(
        self,
        site: 'mwclient.client.Site',
        name: str,
        info: Optional[Mapping[str, Any]] = None,
        namespace: Optional[Namespace] = None
    ) -> None:
        mwclient.page.Page.__init__(self, site, name, info)
        kwargs = {}  # type: Dict[str, Any]
        kwargs['gcmtitle'] = self.name
        if namespace:
            kwargs['gcmnamespace'] = namespace
        GeneratorList.__init__(self, site, 'categorymembers', 'cm', **kwargs)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} object '{self.name}' for {self.site}>"

    def members(
        self,
        prop: str = 'ids|title',
        namespace: Optional[Namespace] = None,
        sort: str = 'sortkey',
        dir: str = 'asc',
        start: Optional[str] = None,
        end: Optional[str] = None,
        generator: bool = True,
        with_content: bool = False
    ) -> 'List':
        kwargs = self.get_page_listing_args('cm', generator, with_content, {
            'start': start,
            'end': end,
            'dir': dir,
            'namespace': namespace,
            'prop': prop,
            'sort': sort,
            'title': self.name
        })
        return self.get_list(generator)(self.site, 'categorymembers', 'cm', **kwargs)


class PageList(GeneratorList):

    def __init__(
        self,
        site: 'mwclient.client.Site',
        prefix: Optional[str] = None,
        start: Optional[str] = None,
        namespace: int = 0,
        redirects: str = 'all',
        end: Optional[str] = None
    ):
        self.namespace = namespace

        kwargs = {}
        if prefix:
            kwargs['gapprefix'] = prefix
        if start:
            kwargs['gapfrom'] = start
        if end:
            kwargs['gapto'] = end

        super().__init__(site, 'allpages', 'ap', gapnamespace=str(namespace),
                         gapfilterredir=redirects, **kwargs)

    def __getitem__(
        self, name: Union[str, int, 'mwclient.page.Page']
    ) -> Union['mwclient.page.Page', 'mwclient.image.Image', 'Category']:
        return self.get(name, None)

    def get(
        self,
        name: Union[str, int, 'mwclient.page.Page'],
        info: Optional[Mapping[str, Any]] = None
    ) -> Union['mwclient.page.Page', 'mwclient.image.Image', 'Category']:
        """Return the page of name `name` as an object.

        If self.namespace is not zero, use {namespace}:{name} as the
        page name, otherwise guess the namespace from the name using
        `self.guess_namespace`.

        Args:
            name: The name of the page as a string, the page ID as an int, or
                another :class:`Page` object.
            info: Page info, if already fetched, e.g., when iterating over a
                list of pages. If not provided, the page info will be fetched
                from the API.

        Returns:
            One of Category, Image or Page (default), according to namespace.
        """
        if self.namespace != 0:
            full_page_name = f"{self.site.namespaces[self.namespace]}:{name}" \
                # type: Union[str, int, 'mwclient.page.Page']
            namespace = self.namespace
        else:
            full_page_name = name
            if isinstance(name, str):
                namespace = self.guess_namespace(name)
            else:
                namespace = 0

        cls = {
            14: Category,
            6: mwclient.image.Image,
        }.get(namespace, mwclient.page.Page)

        return cls(self.site, full_page_name, info)  # type: ignore[no-any-return]

    def guess_namespace(self, name: str) -> int:
        """Guess the namespace from name

        If name starts with any of the site's namespaces' names or
        default_namespaces, use that.  Else, return zero.

        Args:
            name: The name of the page.

        Returns:
            The id of the guessed namespace or zero.
        """
        for ns in self.site.namespaces:
            if ns == 0:
                continue
            namespace = f'{self.site.namespaces[ns].replace(" ", "_")}:'
            if name.startswith(namespace):
                return ns
        return 0


class PageProperty(List):

    def __init__(
        self,
        page: 'mwclient.page.Page',
        prop: str,
        prefix: str,
        *args: Tuple[str, Any],
        **kwargs: Any
    ) -> None:
        super().__init__(
            page.site,
            prop,
            prefix,
            titles=page.name,
            *args,  # type: ignore[arg-type]
            **kwargs,
        )
        self.page = page
        self.generator = 'prop'
        # Continuation boundaries can repeat the last member of a property.
        # Remembering what was already yielded keeps a retried chunk from
        # producing duplicates.
        self._seen = set()  # type: Set[Any]
        self._present = False
        self._has_items = False

    def set_iter(self, data: Mapping[str, Any]) -> None:
        for page in data['query']['pages'].values():
            if page['title'] == self.page.name:
                items = []
                for item in page.get(self.list_name, ()):
                    key = _item_identity(item)
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    items.append(item)
                if self.list_name in page:
                    self._present = True
                    self._has_items = self._has_items or bool(page[self.list_name])
                self._iter = iter(items)
                return
        raise StopIteration

    @property
    def empty(self) -> Optional[bool]:
        """Whether the property is known to be empty, or ``None`` if unknown.

        Empty is only proven when the property finished, the API actually
        returned it, and it contained no members.  A property the API simply
        omitted is left as ``None`` rather than assumed to be empty.
        """
        if not self.complete:
            return None
        if not self._present:
            return None
        return not self._has_items


class PagePropertyGenerator(GeneratorList):

    def __init__(
        self,
        page: 'mwclient.page.Page',
        prop: str,
        prefix: str,
        *args: Tuple[str, Any],
        **kwargs: Any
    ) -> None:
        super().__init__(page.site, prop, prefix, titles=page.name, *args, **kwargs)
        self.page = page


class RevisionsIterator(PageProperty):

    def load_chunk(self) -> None:
        if 'rvstartid' in self.args and 'rvstart' in self.args:
            del self.args['rvstart']
        return super().load_chunk()
