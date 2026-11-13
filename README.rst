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


Ambiguous quote totals
----------------------

A number like ``1.250`` is ambiguous: it may mean ``1.25`` or ``1250``, and
different suppliers on the same quote may use different conventions.
``resolve_quote`` takes the line quantities, the raw quoted prices and the
confirmed grand total, and checks *every* combination of plausible readings.
A line is only reported as determined when the same reading appears in all
matching combinations:

>>> from decimal import Decimal
>>> from price_parser import resolve_quote
>>> result = resolve_quote(
...     [(1, "1.250"), (2, "2,500")], Decimal("5001.25")
... )
>>> result.is_unique
True
>>> result.determined_lines
(0, 1)
>>> [reading.unit_price for reading in result.solutions[0].readings]
[Decimal('1.250'), Decimal('2500')]

When the total does not pin every line down, the remaining lines stay
ambiguous and their genuinely different readings can be listed:

>>> result = resolve_quote(
...     [(1, "1.250"), (1, "1.250")], Decimal("1251.25")
... )
>>> result.is_unique
False
>>> result.ambiguous_lines
(0, 1)
>>> [reading.unit_price for reading in result.differing_readings(0)]
[Decimal('1.250'), Decimal('1250')]

If no combination reproduces the confirmed total, the closest achievable
totals are returned instead of a forced match:

>>> result = resolve_quote([(1, "1.250")], Decimal("100"))
>>> result.exact
False
>>> result.achieved_totals
(Decimal('1.25'),)

Line totals and the grand total are computed as ``quantity * price`` and
rounded to the given ``precision`` (a quantum such as ``Decimal("0.01")`` or
an integer number of decimal places) using the given ``rounding`` mode,
which defaults to ``decimal.ROUND_HALF_UP``.


Contributing
============

* Source code: https://github.com/scrapinghub/price-parser
* Issue tracker: https://github.com/scrapinghub/price-parser/issues

Use tox_ to run tests with different Python versions::

    tox

The command above also runs type checks; we use mypy.

.. _tox: https://tox.readthedocs.io
