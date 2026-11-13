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


Price ranges
------------

Besides the single ``amount``, ``Price`` exposes a structured
``price_range`` describing the whole quote: its endpoints, whether each
endpoint is inclusive, its currency, an explicitly stated unit, and the
raw text the range came from.

A two-sided quote such as ``"10–12 /kg"`` is parsed into a lower and an
upper bound; both are inclusive for a regular interval, and the unit is only
recorded when it is actually written (``"/kg"`` here), never guessed:

>>> price = Price.fromstring("10–12 /kg")
>>> price.price_range.values
(Decimal('10'), Decimal('12'))
>>> price.price_range.lower.inclusive
True
>>> price.price_range.unit
'kg'
>>> price.price_range.source_text
'10–12'

One-sided quotes keep the missing side as ``None``. ``"from 10"`` is a
lower bound, while ``"10 以下"`` is an inclusive upper bound:

>>> lower = Price.fromstring("from 10").price_range
>>> lower.lower.inclusive, lower.upper
(True, None)
>>> upper = Price.fromstring("10 以下").price_range
>>> upper.lower, upper.upper.inclusive
(None, True)

An exclusive limit such as ``"under 10"`` keeps the value but marks the
bound as not inclusive:

>>> from price_parser import parse_price_range
>>> parse_price_range("under 10")
PriceRange(lower=None, upper=Bound(value=Decimal('10'), inclusive=False), currency=None)

Endpoints are only joined when an explicit connector (``-``, ``–``, ``~``,
``"to"`` ...) is present and the currencies agree. A current price next to a
struck-through original price, or a quote in another currency, is therefore
never stitched into one range; it stays a single exact value:

>>> Price.fromstring("$19.99 $9.99").price_range.is_exact
True
>>> Price.fromstring("$5 – €8").price_range.is_exact
True

Use ``certainly_at_most`` / ``possibly_at_most`` to separate goods that are
definitely cheap from ones that might be. A lower-only quote (``"from 10"``)
and an interval that extends past ten (``"10–12"``) are possible but not
certain to be at most ten, while ``"10 以下"`` is certain:

>>> from decimal import Decimal
>>> Price.fromstring("10 以下").price_range.certainly_at_most(Decimal(10))
True
>>> Price.fromstring("from 10").price_range.certainly_at_most(Decimal(10))
False
>>> Price.fromstring("10–12 /kg").price_range.certainly_at_most(Decimal(10))
False
>>> Price.fromstring("10–12 /kg").price_range.possibly_at_most(Decimal(10))
True

A quote without a unit keeps ``unit=None`` instead of being treated as a
per-kilogram cap, and a one-sided quote with no unit is preserved as-is:

>>> Price.fromstring("from 10").price_range.unit is None
True


Contributing
============

* Source code: https://github.com/scrapinghub/price-parser
* Issue tracker: https://github.com/scrapinghub/price-parser/issues

Use tox_ to run tests with different Python versions::

    tox

The command above also runs type checks; we use mypy.

.. _tox: https://tox.readthedocs.io
