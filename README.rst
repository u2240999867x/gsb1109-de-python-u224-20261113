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


Price history analysis
----------------------

``price_parser.history`` builds comparable price histories from scraped
price tags.  Each tag is normalized to a net-of-tax price-per-gram
interval (using the raw price text, the net content, and the tax rate in
effect in the region on the capture date), so that a merchant changing
the net content (500 g -> 450 g) or switching between tax-inclusive and
tax-exclusive display is not mistaken for a price change.

>>> from datetime import date
>>> from decimal import Decimal
>>> from price_parser import (
...     PriceTag, TaxCalendar, TaxMode, TaxRateChange, analyze_price_history,
... )
>>> vat = TaxCalendar({"DE": [TaxRateChange(date(2020, 1, 1), Decimal("0.19"))]})
>>> tags = [
...     PriceTag("t1", "p1", date(2026, 1, 10), "5,99 €", region="DE",
...              tax_mode=TaxMode.INCLUSIVE, net_grams_min=Decimal("500")),
...     PriceTag("t2", "p1", date(2026, 3, 1), "5,49 €", region="DE",
...              tax_mode=TaxMode.EXCLUSIVE, net_grams_min=Decimal("450")),
... ]
>>> events = analyze_price_history(tags, vat)
>>> [event.verdict.value for event in events]
['definite_increase']
>>> [(pair.before_tag.tag_id, pair.after_tag.tag_id)
...  for pair in events[0].supporting_pairs]
[('t1', 't2')]

When the tax rate or the net content is only known as a range, the
normalized price is an interval, and changes are classified as
``definite_increase`` (intervals do not overlap), ``possible_increase``
(intervals overlap) or ``insufficient_evidence`` (data missing).  Tags
captured on the same day are treated as coexisting versions and never
ordered by scrape sequence; every reported increase keeps references to
the two price tags supporting it via ``event.supporting_pairs``.


Contributing
============

* Source code: https://github.com/scrapinghub/price-parser
* Issue tracker: https://github.com/scrapinghub/price-parser/issues

Use tox_ to run tests with different Python versions::

    tox

The command above also runs type checks; we use mypy.

.. _tox: https://tox.readthedocs.io
