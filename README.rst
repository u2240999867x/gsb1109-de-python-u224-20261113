============
price-parser
============

.. image:: https://img.shields.io/pypi/v/price-parser.svg
   :target: https://pypi.python.org/pypi/price-parser
   :alt: PyPI Version

.. image:: https://img.shields.io/pypi/pyversions/price-parser.svg
   :target: https://pypi.python.org/pypi/price-parser
   :alt: Supported Python Versions

.. image:: https://github.com/scrapinghub/price-parser/actions/workflows/main.yml/badge.svg?branch=master
   :target: https://github.com/scrapinghub/price-parser/actions?workflow=Tests
   :alt: Build Status

.. image:: https://codecov.io/github/scrapinghub/price-parser/coverage.svg?branch=master
   :target: https://codecov.io/gh/scrapinghub/price-parser
   :alt: Coverage report


``price-parser`` is a small library for extracting price and currency from
raw text strings.

Features:

* robust price amount and currency symbol extraction
* zero-effort handling of thousand and decimal separators

The main use case is parsing prices extracted from web pages.
For example, you can write a CSS/XPath selector which targets an element
with a price, and then use this library for cleaning it up,
instead of writing custom site-specific regex or Python code.

License is BSD 3-clause.

Installation
============

::

    pip install price-parser

price-parser requires Python 3.9+.

Usage
=====

Basic usage
-----------

>>> from price_parser import Price
>>> price = Price.fromstring("22,90 €")
>>> price
Price(amount=Decimal('22.90'), currency='€')
>>> price.amount       # numeric price amount
Decimal('22.90')
>>> price.currency     # currency symbol, as appears in the string
'€'
>>> price.amount_text  # price amount, as appears in the string
'22,90'
>>> price.amount_float # price amount as float, not Decimal
22.9

If you prefer, ``Price.fromstring`` has an alias ``price_parser.parse_price``,
they do the same:

>>> from price_parser import parse_price
>>> parse_price("22,90 €")
Price(amount=Decimal('22.90'), currency='€')

The library has extensive tests (900+ real-world examples of price strings).
Some of the supported cases are described below.

Supported cases
---------------

Unclean price strings with various currencies are supported;
thousand separators and decimal separators are handled:

>>> Price.fromstring("Price: $119.00")
Price(amount=Decimal('119.00'), currency='$')

>>> Price.fromstring("15 130 Р")
Price(amount=Decimal('15130'), currency='Р')

>>> Price.fromstring("151,200 تومان")
Price(amount=Decimal('151200'), currency='تومان')

>>> Price.fromstring("Rp 1.550.000")
Price(amount=Decimal('1550000'), currency='Rp')

>>> Price.fromstring("Běžná cena 75 990,00 Kč")
Price(amount=Decimal('75990.00'), currency='Kč')


Euro sign is used as a decimal separator in a wild:

>>> Price.fromstring("1,235€ 99")
Price(amount=Decimal('1235.99'), currency='€')

>>> Price.fromstring("99 € 95 €")
Price(amount=Decimal('99'), currency='€')

>>> Price.fromstring("35€ 999")
Price(amount=Decimal('35'), currency='€')


Some special cases are handled:

>>> Price.fromstring("Free")
Price(amount=Decimal('0'), currency=None)


When price or currency can't be extracted, corresponding
attribute values are set to None:

>>> Price.fromstring("")
Price(amount=None, currency=None)

>>> Price.fromstring("Foo")
Price(amount=None, currency=None)

>>> Price.fromstring("50% OFF")
Price(amount=None, currency=None)

>>> Price.fromstring("50")
Price(amount=Decimal('50'), currency=None)

>>> Price.fromstring("R$")
Price(amount=None, currency='R$')


Currency hints
--------------

``currency_hint`` argument allows to pass a text string which may (or may not)
contain currency information. This feature is most useful for automated price
extraction.

>>> Price.fromstring("34.99", currency_hint="руб. (шт)")
Price(amount=Decimal('34.99'), currency='руб.')

Note that currency mentioned in the main price string may be
**preferred** over currency specified in ``currency_hint`` argument;
it depends on currency symbols found there. If you know the correct currency,
you can set it directly:

>>> price = Price.fromstring("1 000")
>>> price.currency = 'EUR'
>>> price
Price(amount=Decimal('1000'), currency='EUR')


Decimal separator
-----------------

If you know which symbol is used as a decimal separator in the input string,
pass that symbol in the ``decimal_separator`` argument to prevent price-parser
from guessing the wrong decimal separator symbol.

>>> Price.fromstring("Price: $140.600", decimal_separator=".")
Price(amount=Decimal('140.600'), currency='$')

>>> Price.fromstring("Price: $140.600", decimal_separator=",")
Price(amount=Decimal('140600'), currency='$')


Currency resolution
-------------------

The parser returns the symbol as printed on the page, but a bare ``$`` is
ambiguous: many currencies use it, so the same merchant can mean CAD, AUD and
USD on its Canadian, Australian and American stores. ``CurrencyResolver``
maps an observed symbol to one currency code using explicit declarations.

Declarations have three levels, consulted from the narrowest to the broadest
scope:

* ``EvidenceLevel.ITEM`` -- a currency declared for one product;
* ``EvidenceLevel.STORE`` -- a currency declared for one store, optionally
  restricted to an effective time window;
* ``EvidenceLevel.SITE`` -- the default currency of a whole site.

>>> from datetime import date
>>> from price_parser import (
...     CurrencyDeclaration,
...     CurrencyResolver,
...     EvidenceLevel,
...     ResolutionStatus,
... )
>>> resolver = CurrencyResolver([
...     CurrencyDeclaration(declaration_id="ca-store", currency="CAD",
...                         level=EvidenceLevel.STORE, store_id="ca"),
...     CurrencyDeclaration(declaration_id="au-store", currency="AUD",
...                         level=EvidenceLevel.STORE, store_id="au"),
...     CurrencyDeclaration(declaration_id="us-store", currency="USD",
...                         level=EvidenceLevel.STORE, store_id="us"),
... ])
>>> canadian = resolver.resolve("$", store_id="ca")
>>> canadian.currency
'CAD'
>>> canadian.evidence.declaration_id
'ca-store'
>>> resolver.resolve("$", store_id="au").currency
'AUD'
>>> resolver.resolve("$", store_id="us").currency
'USD'

An item-level declaration overrides the store and site defaults, but only for
that product:

>>> item = CurrencyDeclaration(declaration_id="sku-1-usd", currency="USD",
...                            level=EvidenceLevel.ITEM, item_id="sku-1")
>>> with_item = CurrencyResolver([*resolver.declarations, item])
>>> with_item.resolve("$", item_id="sku-1", store_id="ca").currency
'USD'
>>> with_item.resolve("$", item_id="sku-2", store_id="ca").currency
'CAD'

If declarations at the same level support different currencies, the row is
suspended instead of guessed:

>>> conflicting = CurrencyResolver([
...     CurrencyDeclaration(declaration_id="says-cad", currency="CAD",
...                         level=EvidenceLevel.STORE, store_id="ca"),
...     CurrencyDeclaration(declaration_id="says-usd", currency="USD",
...                         level=EvidenceLevel.STORE, store_id="ca"),
... ])
>>> held = conflicting.resolve("$", store_id="ca")
>>> held.status is ResolutionStatus.SUSPENDED
True
>>> held.is_suspended
True
>>> held.currency is None
True

Timed declarations only apply when the resolution date falls inside their
window, so today's store settings never backfill historical pages. Recomputing
an old quote keeps the exact declaration that drove the conclusion:

>>> timed = CurrencyResolver([
...     CurrencyDeclaration(declaration_id="old-cad", currency="CAD",
...                         level=EvidenceLevel.STORE, store_id="ca",
...                         effective_until=date(2026, 10, 31)),
...     CurrencyDeclaration(declaration_id="new-usd", currency="USD",
...                         level=EvidenceLevel.STORE, store_id="ca",
...                         effective_from=date(2026, 11, 1)),
... ])
>>> timed.resolve("$", store_id="ca", as_of=date(2026, 9, 1)).currency
'CAD'
>>> recomputed = timed.resolve("$", store_id="ca", as_of=date(2026, 11, 14))
>>> recomputed.currency
'USD'
>>> recomputed.evidence.declaration_id
'new-usd'


Contributing
============

* Source code: https://github.com/scrapinghub/price-parser
* Issue tracker: https://github.com/scrapinghub/price-parser/issues

Use tox_ to run tests with different Python versions::

    tox

The command above also runs type checks; we use mypy.

.. _tox: https://tox.readthedocs.io
