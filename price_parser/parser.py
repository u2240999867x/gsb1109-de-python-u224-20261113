import re
import string
from decimal import Decimal, InvalidOperation
from re import Pattern
from typing import Callable, NamedTuple, Optional

import attr

from ._currencies import CURRENCY_CODES, CURRENCY_NATIONAL_SYMBOLS, CURRENCY_SYMBOLS


@attr.s(frozen=True, auto_attribs=True)
class Bound:
    """One endpoint of a :class:`PriceRange`.

    ``value`` is the parsed numeric value, ``inclusive`` says whether the
    endpoint itself is part of the quoted range, and ``text`` keeps the
    endpoint as it appeared in the source string.
    """

    value: Optional[Decimal]
    inclusive: bool
    text: Optional[str] = attr.ib(default=None, repr=False)


@attr.s(frozen=True, auto_attribs=True)
class PriceRange:
    """A quoted price interval.

    ``lower`` and ``upper`` are the range endpoints; either one can be
    ``None`` for a one-sided quote such as "from 10" or "10 以下".
    ``currency`` and ``unit`` are only set when stated explicitly in the
    source text; ``unit`` is never guessed, so a unit-less quote keeps
    ``unit=None`` instead of being treated as a per-kilogram cap.
    ``source_text`` preserves the raw text the range was extracted from.
    """

    lower: Optional[Bound]
    upper: Optional[Bound]
    currency: Optional[str] = None
    unit: Optional[str] = attr.ib(default=None, repr=False)
    source_text: Optional[str] = attr.ib(default=None, repr=False)

    @property
    def values(self) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Numeric ``(lower, upper)`` values; either side can be ``None``."""
        lower_value = self.lower.value if self.lower is not None else None
        upper_value = self.upper.value if self.upper is not None else None
        return lower_value, upper_value

    @property
    def is_exact(self) -> bool:
        """True if the quote collapses to a single inclusive value."""
        lower_value, upper_value = self.values
        return (
            self.lower is not None
            and self.upper is not None
            and lower_value is not None
            and upper_value is not None
            and lower_value == upper_value
            and self.lower.inclusive
            and self.upper.inclusive
        )

    def certainly_at_most(self, value: Decimal) -> bool:
        """True if every price allowed by the quote is no greater than ``value``.

        A one-sided lower bound ("from 10") can never guarantee an upper
        limit, while an upper bound can, regardless of its inclusivity:
        "under 10" certainly costs no more than 10.
        """
        if self.upper is None or self.upper.value is None:
            return False
        return self.upper.value <= value

    def possibly_at_most(self, value: Decimal) -> bool:
        """True if some price allowed by the quote may be no greater than
        ``value``.

        Lets callers separate goods that are *certainly* cheap from ones
        that are only *possibly* cheap, e.g. a "10-12" quote is possibly
        but not certainly at most 10.
        """
        if self.certainly_at_most(value):
            return True
        if self.lower is not None and self.lower.value is not None:
            if self.lower.inclusive:
                return self.lower.value <= value
            return self.lower.value < value
        # No lower bound and no guaranteed upper bound: the price could
        # still turn out below ``value`` (e.g. "under 100" vs. 10).
        return self.upper is not None


@attr.s(auto_attribs=True)
class Price:
    amount: Optional[Decimal]  # price numeric value, as Decimal
    currency: Optional[str]  # currency symbol (as appeared in text)

    # price value, as a raw string
    amount_text: Optional[str] = attr.ib(repr=False)

    # structured quote interval; None when no price number is present
    price_range: Optional[PriceRange] = attr.ib(default=None, repr=False)

    @property
    def amount_float(self) -> Optional[float]:
        """price numeric value, as float"""
        if self.amount is not None:
            return float(self.amount)
        return None

    @classmethod
    def fromstring(
        cls,
        price: Optional[str],
        currency_hint: Optional[str] = None,
        decimal_separator: Optional[str] = None,
        digit_group_separator: Optional[str] = None,
    ) -> "Price":
        """
        Given price and currency text extracted from HTML elements, return
        ``Price`` instance, which provides a clean currency symbol and
        price amount as a Decimal number.

        ``currency_hint`` is optional; you can pass value of some element
        which may contain currency, as a hint. If currency is present in
        ``price`` string, it could be **preferred** over a value extracted
        from ``currency_hint`` string.

        ``decimal_separator`` is optional; it is used to determine the
        decimal separator in price. If ``decimal_separator`` is ``None``,
        then it is guessed from ``price`` string. If ``decimal_separator``
        is ``"."``, then ``1.000`` is parsed as ``1``. If it is ``,```,
        then ``1.000`` is parsed as ``1000``.

        ``digit_group_separator`` is optional; it is used to determine the
        digit group separator in price. If ``digit_group_separator`` is
        ``None``, then it is guessed from ``price`` string. If
        ``digit_group_separator`` is ``"."``, then ``1.000`` is parsed as
        ``1000``. If it is ``,``, then ``1.000`` is parsed as ``1``.
        """
        currency = extract_currency_symbol(price, currency_hint)
        if currency is not None:
            currency = currency.strip()
        if digit_group_separator is not None and price is not None:
            price = price.replace(digit_group_separator, "")
        amount_text = extract_price_text(price) if price is not None else None
        amount_num = (
            parse_number(amount_text, decimal_separator)
            if amount_text is not None
            else None
        )
        # Only expose a structured range when the established extractor
        # recognizes a price; percentage-only strings such as "50% OFF"
        # then keep price_range None instead of inventing a quote.
        price_range: Optional[PriceRange] = None
        if price is not None and amount_num is not None:
            price_range = extract_price_range(
                price,
                currency,
                decimal_separator,
                amount=amount_num,
                amount_text=amount_text,
            )
            if price_range is None:
                # No literal number but a canonical amount exists, e.g.
                # "Free" (parsed as zero): mirror it as an exact quote.
                bound = Bound(value=amount_num, inclusive=True, text=amount_text)
                price_range = PriceRange(
                    lower=bound,
                    upper=bound,
                    currency=currency,
                    unit=_extract_unit(price),
                    source_text=price.strip(),
                )
        return Price(
            amount=amount_num,
            currency=currency,
            amount_text=amount_text,
            price_range=price_range,
        )


parse_price = Price.fromstring


def or_regex(symbols: list[str]) -> Pattern[str]:
    """Return a regex which matches any of ``symbols``"""
    return re.compile("|".join(re.escape(s) for s in symbols))


# If one of these symbols is found either in price or in currency,
# it is considered currency symbol, and returned as a currency, regardless
# of its position in text.
SAFE_CURRENCY_SYMBOLS = [
    # Variants of $, etc. They need to be before $.
    "Bds$",
    "CUC$",
    "MOP$",
    "AR$",
    "AU$",
    "BN$",
    "BZ$",
    "CA$",
    "CL$",
    "CO$",
    "CV$",
    "HK$",
    "MX$",
    "NT$",
    "NZ$",
    "TT$",
    "RD$",
    "WS$",
    "US$",
    "$U",
    "C$",
    "J$",
    "N$",
    "R$",
    "S$",
    "T$",
    "Z$",
    "A$",
    "SY£",
    "LB£",
    "CN¥",
    "GH₵",
    # unique currency symbols
    "$",
    "€",
    "£",
    "zł",
    "Zł",
    "Kč",
    "₽",
    "¥",
    "￥",
    "฿",
    "դր.",
    "դր",
    "₦",
    "₴",
    "₱",
    "৳",
    "₭",
    "₪",
    "﷼",
    "៛",
    "₩",
    "₫",
    "₡",
    "টকা",
    "ƒ",
    "₲",
    "؋",
    "₮",
    "नेरू",
    "₨",
    "₶",
    "₾",
    "֏",
    "ރ",
    "৲",
    "૱",
    "௹",
    "₠",
    "₢",
    "₣",
    "₤",
    "₧",
    "₯",
    "₰",
    "₳",
    "₷",
    "₸",
    "₹",
    "₺",
    "₼",
    "₾",
    "₿",
    "ℳ",
    "ر.ق.\u200f",
    "د.ك.\u200f",
    "د.ع.\u200f",
    "ر.ع.\u200f",
    "ر.ي.\u200f",
    "ر.س.\u200f",
    "د.ج.\u200f",
    "د.م.\u200f",
    "د.إ.\u200f",
    "د.ت.\u200f",
    "د.ل.\u200f",
    "ل.س.\u200f",
    "د.ب.\u200f",
    "د.أ.\u200f",
    "ج.م.\u200f",
    "ل.ل.\u200f",
    " تومان",
    "تومان",
    "درهم",
    "ريال",
    "جنيه",
    # other common symbols, which we consider unambiguous
    "EUR",
    "euro",
    "eur",
    "CHF",
    "DKK",
    "Rp",
    "lei",
    "руб.",
    "руб",
    "грн.",
    "грн",
    "дин.",
    "Dinara",
    "динар",
    "лв.",
    "лв",
    "р.",
    "тңг",
    "тңг.",
    "ман.",
]

# "D" in some abbreviations means "dollar", and so currency
# can be written as SGD$123 or NZD $123. Currency code should take priority
# over $ symbol in this case.
DOLLAR_CODES = [k for k in CURRENCY_CODES if k.endswith("D")]
_DOLLAR_REGEX = re.compile(
    r"""
        \b
        (?:{})  # currency code like NZD
        (?=
            \$?  # dollar sign to ignore if attached to the currency code
            (?:[\W\d]|$)  # not a letter
        )
    """.format("|".join(re.escape(k) for k in DOLLAR_CODES)),
    re.VERBOSE,
)


# Other common currency symbols: 3-letter codes, less safe abbreviations
OTHER_CURRENCY_SYMBOLS_SET = (
    set(
        CURRENCY_CODES
        + CURRENCY_SYMBOLS
        + CURRENCY_NATIONAL_SYMBOLS
        +
        # even if they appear in text, currency is likely to be rouble
        ["р", "Р"]
    )
    - set(SAFE_CURRENCY_SYMBOLS)  # already handled
    - {"-", "XXX"}  # placeholder values
    - set(string.ascii_uppercase)  # very unreliable on their own
)
OTHER_CURRENCY_SYMBOLS = sorted(OTHER_CURRENCY_SYMBOLS_SET, key=len, reverse=True)

_search_dollar_code = _DOLLAR_REGEX.search
_search_safe_currency = or_regex(SAFE_CURRENCY_SYMBOLS).search
_search_unsafe_currency = or_regex(OTHER_CURRENCY_SYMBOLS).search


def extract_currency_symbol(
    price: Optional[str], currency_hint: Optional[str]
) -> Optional[str]:
    """
    Guess currency symbol from extracted price and currency strings.
    Return an empty string if symbol is not found.
    """
    methods: list[tuple[Callable[[str], Optional[re.Match[str]]], Optional[str]]] = [
        (_search_safe_currency, price),
        (_search_safe_currency, currency_hint),
        (_search_unsafe_currency, price),
        (_search_unsafe_currency, currency_hint),
    ]

    if currency_hint and "$" in currency_hint:
        methods.insert(0, (_search_dollar_code, currency_hint))

    if price and "$" in price:
        methods.insert(0, (_search_dollar_code, price))

    for meth, value in methods:
        m = meth(value) if value else None
        if m:
            return m.group(0)

    return None


def extract_price_text(price: str) -> Optional[str]:
    r"""
    Extract text of a price from a string which contains price and
    maybe some other text. If multiple price-looking substrings are present,
    the first is returned (FIXME: it is better to return a number
    which is near a currency symbol).

    >>> extract_price_text("price: $12.99")
    '12.99'
    >>> extract_price_text("Free")
    '0'
    >>> extract_price_text("Foo")
    >>> extract_price_text("1,235 USD")
    '1,235'

    In addition to numbers, it has a limited support for a case where
    currency symbol (currently only euro) is a decimal separator:

    >>> extract_price_text("99 €, 79 €")
    '99'
    >>> extract_price_text("99 € 79 €")
    '99'
    >>> extract_price_text("35€ 99")
    '35€99'
    >>> extract_price_text("35€ 999")
    '35'
    >>> extract_price_text("1,235€ 99")
    '1,235€99'
    >>> extract_price_text("50% OFF")
    >>> extract_price_text("50%")
    >>> extract_price_text("50")
    '50'
    >>> extract_price_text("$1\xa0298,00")
    '1 298,00'
    >>> extract_price_text("$.75")
    '.75'
    """
    price = re.sub(
        r"\s+", " ", price
    )  # clean initial text from non-breaking and extra spaces

    if price.count("€") == 1:
        m = re.search(
            r"""
        [\d\s.,']*?\d    # number, probably with thousand separators
        \s*?€(\s*?)?    # euro, probably separated by whitespace
        \d(?(1)\d|\d*?)  # if separated by whitespace - search one digit,
                         # multiple digits otherwise
        (?:$|[^\d])     # something which is not a digit
        """,
            price,
            re.VERBOSE,
        )
        if m:
            return m.group(0).replace(" ", "")

    m = re.search(
        r"""
        ([.]?\d[\d\s.,']*)   # number, probably with thousand separators
        \s*?                # skip whitespace
        (?:[^%\d]|$)        # capture next symbol - it shouldn't be %
        """,
        price,
        re.VERBOSE,
    )

    if m:
        price_text = m.group(1).rstrip(",.")
        price_text = price_text.replace("'", "")
        return (
            price_text.strip()
            if price_text.count(".") == 1
            else price_text.lstrip(",.").strip()
        )
    if "free" in price.lower():
        return "0"
    return None


# NOTE: Keep supported separators in sync with parse_number()
_search_decimal_sep = re.compile(
    r"""
\d*          # null or more digits (there can be more before it)
([.,€])      # decimal separator
(?:          # 1,2 or 4+ digits. 3 digits is likely to be a thousand separator.
   \d{1,2}?|
   \d{4}\d*?
)
$
""",
    re.VERBOSE,
).search


def get_decimal_separator(price: str) -> Optional[str]:
    """Return decimal separator symbol or None if there
    is no decimal separator.

    >>> get_decimal_separator("1000")
    >>> get_decimal_separator("12.99")
    '.'
    >>> get_decimal_separator("12,99")
    ','
    >>> get_decimal_separator("12.999")
    >>> get_decimal_separator("3,0000")
    ','
    >>> get_decimal_separator("1,235€99")
    '€'
    >>> get_decimal_separator(".75")
    '.'
    """
    m = _search_decimal_sep(price)
    if m:
        return m.group(1)
    return None


def parse_number(
    num: str, decimal_separator: Optional[str] = None
) -> Optional[Decimal]:
    """Parse a string with a number to a Decimal, guessing its format:
    decimal separator, thousand separator. Return None if parsing fails.

    >>> parse_number("1,234")
    Decimal('1234')
    >>> parse_number("12,34")
    Decimal('12.34')
    >>> parse_number("12,345")
    Decimal('12345')
    >>> parse_number("1,1")
    Decimal('1.1')
    >>> parse_number("1.1")
    Decimal('1.1')
    >>> parse_number("1234")
    Decimal('1234')
    >>> parse_number("12€34")
    Decimal('12.34')
    >>> parse_number("12€ 34")
    Decimal('12.34')
    >>> parse_number("1 234.99")
    Decimal('1234.99')
    >>> parse_number("1,235€99")
    Decimal('1235.99')
    >>> parse_number("1 235€99")
    Decimal('1235.99')
    >>> parse_number("1.235€99")
    Decimal('1235.99')
    >>> parse_number("140.000", decimal_separator=",")
    Decimal('140000')
    >>> parse_number("140.000", decimal_separator=".")
    Decimal('140.000')
    >>> parse_number("")
    >>> parse_number("foo")
    """
    if not num:
        return None
    num = num.strip().replace(" ", "")
    decimal_separator = decimal_separator or get_decimal_separator(num)
    # NOTE: Keep supported separators in sync with _search_decimal_sep
    if decimal_separator is None:
        num = num.replace(".", "").replace(",", "")
    elif decimal_separator == ".":
        num = num.replace(",", "")
    elif decimal_separator == ",":
        num = num.replace(".", "").replace(",", ".")
    else:
        assert decimal_separator == "€"
        num = num.replace(".", "").replace(",", "").replace("€", ".")
    try:
        return Decimal(num)
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# Price ranges
# ---------------------------------------------------------------------------

# A price number: either a leading-dot fraction (".75") or a digit followed by
# number-ish characters (digits, spaces and thousand/decimal separators).
_NUMBER_RE = re.compile(r"(?<!\d)(?:\.\d+|\d[\d\s.,']*)")

# Explicit connectors that turn two neighboring numbers into a quoted range.
# Adjacent numbers without one of these stay separate quotes, so a current
# price next to a struck-through original price is never merged into a range.
_RANGE_CONNECTORS = {"-", "–", "—", "−", "~", "～", "〜", "→", "to", "bis"}

# Qualifiers marking a one-sided lower bound ("from 10", "10 以上").
_LOWER_INCLUSIVE_RE = re.compile(
    r"(?:\b(?:from|ab|vanaf|от|från|a\s+partir\s+de)\b|≥|>=)",
    re.IGNORECASE,
)
_LOWER_EXCLUSIVE_RE = re.compile(
    r"(?:\b(?:over|above|more\s+than)\b|>|&gt;)",
    re.IGNORECASE,
)
# CJK / symbol suffixes sit right after the number and may be followed by a
# unit ("100 以下 /kg"); English phrases are still anchored to the end.
_LOWER_SUFFIX_RE = re.compile(r"(?:以上|起|\+)", re.IGNORECASE)

# Qualifiers marking a one-sided upper bound ("up to 10", "10 以下").
# Inclusive markers are checked before exclusive ones so "<=" / "≤" win.
_UPPER_INCLUSIVE_RE = re.compile(
    r"(?:\b(?:up\s+to|upto|max\.?|bis|до)\b|≤|<=)",
    re.IGNORECASE,
)
_UPPER_EXCLUSIVE_RE = re.compile(
    r"(?:\b(?:under|below|less\s+than)\b|<|&lt;)",
    re.IGNORECASE,
)
_UPPER_SUFFIX_RE = re.compile(
    r"(?:以下|以内|\b(?:or|and)\s+(?:less|under)\b\s*$)",
    re.IGNORECASE,
)

# Explicit measurement units only: a "/kg" style suffix, or a "per/pro" phrase.
# A quote without one of these keeps unit=None instead of guessing a unit.
_SLASH_UNIT_RE = re.compile(
    r"/\s*([^\s\d/.,;:()\[\]{}\"'`~!@#$%^&*+=<>?\\|][^\s/.,;:()\[\]{}]*)"
)
_WORD_UNIT_RE = re.compile(
    r"\b(?:per|pro|por)\s+([^\s\d.,;:()\[\]{}]+)",
    re.IGNORECASE,
)


class _NumberToken(NamedTuple):
    text: str
    value: Decimal
    start: int
    end: int


def _iter_number_tokens(
    text: str, decimal_separator: Optional[str]
) -> list[_NumberToken]:
    """Find all price-looking numbers, skipping percentage values such as
    discount rates ("-30%"), which are never price-range endpoints."""
    tokens: list[_NumberToken] = []
    for match in _NUMBER_RE.finditer(text):
        if text[match.end() : match.end() + 1] == "%":
            continue
        number_text = match.group(0).strip().rstrip(",.'").strip()
        if not number_text:
            continue
        value = parse_number(number_text, decimal_separator)
        if value is not None:
            tokens.append(_NumberToken(number_text, value, match.start(), match.end()))
    return tokens


def _currency_in_window(text: str, start: int, end: int) -> Optional[str]:
    """Detect a currency symbol next to one number of a potential range."""
    window = text[max(0, start - 12) : end + 4]
    found = extract_currency_symbol(window, None)
    return found.strip() if found else None


def _connector_between(
    text: str, first_end: int, second_start: int, currency: Optional[str]
) -> Optional[str]:
    """Return the connector text between two numbers, if it is exactly one."""
    middle = text[first_end:second_start].strip()
    if currency:
        middle = middle.replace(currency, "", 1).strip()
    if middle in _RANGE_CONNECTORS:
        return middle
    return None


def _extract_unit(text: str) -> Optional[str]:
    """Extract an explicitly stated measurement unit, e.g. "kg" from "/kg"."""
    match = _SLASH_UNIT_RE.search(text)
    if match:
        return match.group(1).strip()
    match = _WORD_UNIT_RE.search(text)
    if match:
        return match.group(1).strip()
    return None


def _head_qualifier(head: str) -> Optional[re.Match[str]]:
    for regex in (
        _UPPER_INCLUSIVE_RE,
        _UPPER_EXCLUSIVE_RE,
        _LOWER_INCLUSIVE_RE,
        _LOWER_EXCLUSIVE_RE,
    ):
        match = regex.search(head)
        if match:
            return match
    return None


def extract_price_range(
    price: str,
    currency: Optional[str] = None,
    decimal_separator: Optional[str] = None,
    amount: Optional[Decimal] = None,
    amount_text: Optional[str] = None,
) -> Optional[PriceRange]:
    """Extract a structured :class:`PriceRange` from a raw price string.

    Return ``None`` when the string contains no parseable number.
    ``amount`` / ``amount_text`` are the canonical single-price values; when
    given, they are used for a one-sided or exact bound so the range never
    disagrees with :attr:`Price.amount` (e.g. euro-decimal strings).

    >>> price_range = extract_price_range("10–12 /kg")
    >>> price_range.values
    (Decimal('10'), Decimal('12'))
    >>> price_range.unit
    'kg'
    >>> extract_price_range("from 10").lower.value
    Decimal('10')
    >>> extract_price_range("10 以下").upper.inclusive
    True
    >>> extract_price_range("8").unit is None
    True
    """
    text = re.sub(r"\s+", " ", price).strip()
    tokens = _iter_number_tokens(text, decimal_separator)
    if not tokens:
        return None

    unit = _extract_unit(text)
    first = tokens[0]

    # Two-sided quote: the first two numbers must be joined by an explicit
    # connector, and any currencies next to them must agree.
    if len(tokens) >= 2:
        second = tokens[1]
        connector = _connector_between(text, first.end, second.start, currency)
        first_currency = _currency_in_window(text, first.start, first.end)
        second_currency = _currency_in_window(text, second.start, second.end)
        currencies_conflict = (
            first_currency is not None
            and second_currency is not None
            and first_currency != second_currency
        )
        if connector is not None and not currencies_conflict:
            return PriceRange(
                lower=Bound(value=first.value, inclusive=True, text=first.text),
                upper=Bound(value=second.value, inclusive=True, text=second.text),
                currency=currency,
                unit=unit,
                source_text=text[first.start : second.end].strip(),
            )

    # One-sided or exact quote: only the first number is taken into account,
    # so neighboring prices of other products are never stitched together.
    head = text[: first.start]
    tail = text[first.end :]
    bound = Bound(
        value=amount if amount is not None else first.value,
        inclusive=True,
        text=amount_text if amount_text is not None else first.text,
    )
    lower: Optional[Bound] = None
    upper: Optional[Bound] = None

    upper_suffix = _UPPER_SUFFIX_RE.search(tail)
    lower_suffix = _LOWER_SUFFIX_RE.search(tail)
    if _UPPER_INCLUSIVE_RE.search(head) or upper_suffix:
        upper = bound
    elif _UPPER_EXCLUSIVE_RE.search(head):
        upper = attr.evolve(bound, inclusive=False)
    elif _LOWER_INCLUSIVE_RE.search(head) or lower_suffix:
        lower = bound
    elif _LOWER_EXCLUSIVE_RE.search(head):
        lower = attr.evolve(bound, inclusive=False)
    else:
        lower = upper = bound

    start = first.start
    qualifier = _head_qualifier(head)
    if qualifier is not None:
        start = qualifier.start()
    end = first.end
    suffix = upper_suffix or lower_suffix
    if suffix is not None:
        end = first.end + suffix.end()

    return PriceRange(
        lower=lower,
        upper=upper,
        currency=currency,
        unit=unit,
        source_text=text[start:end].strip(),
    )


def parse_price_range(
    price: Optional[str],
    currency_hint: Optional[str] = None,
    decimal_separator: Optional[str] = None,
    digit_group_separator: Optional[str] = None,
) -> Optional[PriceRange]:
    """Parse a raw price string and return only its :class:`PriceRange`."""
    return Price.fromstring(
        price,
        currency_hint,
        decimal_separator,
        digit_group_separator,
    ).price_range
