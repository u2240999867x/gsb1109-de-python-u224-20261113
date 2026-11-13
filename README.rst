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


Multi-packs, free units and minimum order quantities
----------------------------------------------------

Shelf descriptions often mix pack capacity, free units and order rules, for
example ``"12×250 mL 加赠 2 瓶"`` and ``"两箱起购"``.  ``parse_pack`` keeps
these values separate, so a minimum order quantity is never mistaken for the
capacity of one box:

>>> from decimal import Decimal
>>> from price_parser import parse_pack
>>> pack = parse_pack("两箱起购 12×250 mL 加赠 2 瓶")
>>> pack.count, pack.size, pack.unit
(12, Decimal('250'), 'mL')
>>> pack.total_size            # paid quantity in one box
Decimal('3000')
>>> pack.min_order_boxes       # order multiple, not pack capacity
2
>>> pack.gift_count
2

``parse_quote`` combines a price with a pack description.  Unit prices are
calculated for a target amount; free units are opt-in because their size is
not always stated:

>>> from price_parser import parse_quote
>>> quote = parse_quote("¥59.90 12×250 mL 加赠 2 瓶", label="A")
>>> quote.unit_price("L", include_gifts=False)
Decimal('19.96666666666666666666666667')
>>> quote.unit_price("L", gift_size=Decimal("250"))  # same-size gift
Decimal('17.11428571428571428571428571')
>>> quote.unit_price("瓶")                            # count needs no gift size
Decimal('4.278571428571428571428571429')

If the free-unit size is unknown, ``unit_price_range`` reports the interval
that can be determined without inventing a value.  By default the unknown
size is bounded by the paid unit size; pass ``gift_size_bounds`` to declare a
tighter policy:

>>> price_range = quote.unit_price_range("L")
>>> price_range.low < price_range.high
True
>>> price_range.assumptions
('gift size unknown; assumed between 0 and the paid unit size',)
>>> tight = quote.unit_price_range("L", gift_size_bounds=(100, 200))
>>> tight.low, tight.high
(Decimal('17.61764705882352941176470588'), Decimal('18.71875'))

``compare_quotes`` ranks quotes by unit price and reports the gift quantity
at which one quote catches another, so uncertain free units are visible
instead of silently affecting the winner:

>>> from price_parser import compare_quotes
>>> other = parse_quote("¥55.50 12×250 mL", label="B")
>>> comparison = compare_quotes([quote, other], target_unit="L")
>>> comparison.certain
False
>>> comparison.possible_winners
('A', 'B')
>>> change = comparison.rank_changes[0]
>>> change.gift_size, change.gift_size_unit
(Decimal('118.9189189189189189189189190'), 'mL')

This says that if each of A's two free bottles contains less than about
118.9 mL, B remains cheaper; above that crossover, A wins.  The crossover is
included in the result only because it lies inside A's declared gift bounds.

Contributing
============

* Source code: https://github.com/scrapinghub/price-parser
* Issue tracker: https://github.com/scrapinghub/price-parser/issues

Use tox_ to run tests with different Python versions::

    tox

The command above also runs type checks; we use mypy.

.. _tox: https://tox.readthedocs.io
