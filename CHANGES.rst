Changes
=======

0.6.0 (unreleased)
------------------

* Added structured price-range extraction. ``Price`` now exposes a
  ``price_range`` attribute (with ``Bound`` / ``PriceRange`` types and a
  ``parse_price_range`` helper) describing two-sided quotes (``"10–12 /kg"``)
  and one-sided quotes (``"from 10"``, ``"10 以下"``), including endpoint
  inclusivity, currency, an explicitly stated unit and the original range
  text.
* Range endpoints are only joined through an explicit connector and matching
  currency, so adjacent original/sale prices or quotes in different
  currencies are never stitched into one interval.
* Added ``PriceRange.certainly_at_most`` and ``possibly_at_most`` to
  distinguish goods that are definitely within a budget from ones that only
  might be. A unit-less one-sided quote keeps ``unit=None`` instead of being
  treated as a per-kilogram limit.

0.5.1 (2026-03-19)
------------------

* Added support for additional currencies: Egyptian pound (EGP), UAE dirham
  (AED) and Saudi riyal (SAR).

0.5.0 (2025-10-06)
------------------

* Added support for Python 3.14.
* Added support for ``'`` as a thousands separator.
* Migrated the build system to ``hatchling``.
* Improved type hints.
* CI improvements.

0.4.0 (2025-02-05)
------------------

* Dropped support for Python 3.8 and lower, added support for Python 3.11-3.13.
* Added a ``digit_group_separator`` parameter to ``Price.fromstring()``.

0.3.4 (2020-11-25)
------------------

* Improved parsing of prices without digits before a decimal point ('.75'),
  https://github.com/scrapinghub/price-parser/pull/42
* Fix parsing of price with non-breaking spaces
  https://github.com/scrapinghub/price-parser/pull/43

0.3.3 (2020-02-05)
------------------

* Fixed installation issue on some Windows machines.

0.3.2 (2020-01-28)
------------------

* Improved Korean and Japanese currency detection.
* Declare Python 3.8 support.

0.3.1 (2019-10-21)
------------------

* Redundant $ signs are no longer returned as a part of currency, e.g.
  for ``SGD$ 100`` currency would be ``SGD``, not ``SGD$``.

0.3.0 (2019-10-19)
------------------

* New ``Price.fromstring`` argument ``decimal_separator`` allows to override
  decimal separator for the cases where it is known
  (i.e. disable decimal separator detection);
* NTD and RBM unofficial currency names are added;
* quantifiers in regular expressions are made non-greedy, which provides
  a small speedup;
* test improvements.

0.2.4 (2019-07-03)
------------------

* Declare price-parser as providing type annotations (pep-561). This enables
  better type checking for projects using price-parser.
* improved test coverage

0.2.3 (2019-06-18)
------------------

* Follow-up for 0.2.2 release: improved parsing of prices with 4+ digits
  after a decimal separator.

0.2.2 (2019-06-18)
------------------

* Fixed parsing of prices with 4+ digits after a decimal separator.

0.2.1 (2019-04-19)
------------------

* 23 additional currency symbols are added;
* ``A$`` alias for Australian Dollar is added.

0.2 (2019-04-12)
----------------

Added support for currencies replaced by euro.

0.1.1 (2019-04-12)
------------------

Minor packaging fixes.

0.1 (2019-04-12)
----------------

Initial release.
