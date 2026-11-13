"""
Extraction of payment components from promotional pricing strings.

Promotional pages often describe a payment plan instead of a single price,
for example a Chinese e-commerce presale offer::

    定金20抵50，尾款149

Here the deposit (定金, 20) is money actually paid, the deduction
(抵扣, 50) is a marketing value which is never paid in cash, and the
balance (尾款, 149) can be stated either before or after the deduction is
applied. This module keeps those interpretations separate instead of
summing every number found on the page.

Every extracted value keeps a ``source_text`` slice (and its span) of the
whitespace-normalized input, so each tenable amount can be traced back to
the original wording.
"""

import re
from decimal import Decimal
from typing import Optional

import attr

from .parser import extract_currency_symbol, parse_number

# Payment component kinds.
DEPOSIT = "deposit"
DEDUCTION = "deduction"
BALANCE = "balance"

# Interpretations of a payment total / balance.
BALANCE_POST_DEDUCTION = "balance_post_deduction"
BALANCE_PRE_DEDUCTION = "balance_pre_deduction"
DEPOSIT_ONLY = "deposit_only"

# Reasons why a number from the text is not part of the payment plan.
IGNORE_PERCENT = "percent"
IGNORE_REFERENCE_PRICE = "reference_price"
IGNORE_UNASSOCIATED = "unassociated"

# Maximum distance between a keyword and the number belonging to it.
_GAP = 12

_COMPONENT_KEYWORD_RE = re.compile(r"定金|尾款|抵扣|抵")
_TOTAL_KEYWORD_RE = re.compile(
    r"到手价|到手|实付|券后价|券后|预售价|活动价|售价|总价|合计"
)
_IGNORE_KEYWORD_RE = re.compile(r"建议零售价|专柜价|吊牌价|划线价|市场价|日常价|原价")
_NUMBER_RE = re.compile(r"\d[\d\s.,']*")
_PERCENT_AFTER_RE = re.compile(r"\s*%")


@attr.s(auto_attribs=True)
class BalanceInterpretation:
    """One possible reading of a balance amount.

    ``interpretation`` is one of ``BALANCE_POST_DEDUCTION`` (the stated
    balance is the cash due after the deduction) or
    ``BALANCE_PRE_DEDUCTION`` (the stated balance is a marked price and the
    deduction still has to be applied); ``amount`` is the cash due at the
    balance stage under that reading.
    """

    interpretation: str
    amount: Decimal


@attr.s(auto_attribs=True)
class PaymentComponent:
    """A single payment component mentioned in the text.

    ``kind`` is one of ``DEPOSIT``, ``DEDUCTION`` or ``BALANCE``.
    ``source_text`` is the slice of the normalized input the component was
    extracted from and ``source_span`` its (start, end) position there.
    ``interpretations`` is only populated for balance components, as the
    balance may be ambiguous with respect to the deduction.
    """

    kind: str
    amount: Decimal
    amount_text: str
    source_text: str
    source_span: tuple[int, int]
    interpretations: list[BalanceInterpretation] = attr.ib(factory=list)


@attr.s(auto_attribs=True)
class PaymentTotal:
    """A tenable amount of money actually paid for the whole purchase.

    ``interpretation`` explains how ``amount`` was derived, ``sources`` are
    the source text slices it was computed from, ``complete`` tells whether
    all payment stages were present in the text, and
    ``matches_stated_total`` tells whether an explicit total on the page
    confirms this candidate.
    """

    amount: Decimal
    interpretation: str
    sources: list[str]
    complete: bool
    matches_stated_total: bool


@attr.s(auto_attribs=True)
class StatedAmount:
    """A number explicitly presented as a total price (e.g. 到手价)."""

    amount: Decimal
    amount_text: str
    source_text: str


@attr.s(auto_attribs=True)
class IgnoredNumber:
    """A number from the text which is not part of the payment plan.

    ``reason`` is one of the ``IGNORE_*`` constants: the number is a
    percentage, a reference/list price next to words such as 原价, or it
    could not be associated with any payment keyword.
    """

    amount: Optional[Decimal]
    amount_text: str
    reason: str
    source_text: str


@attr.s(auto_attribs=True)
class PaymentBreakdown:
    """Payment components extracted from a promotional pricing string."""

    currency: Optional[str]
    components: list[PaymentComponent]
    payment_totals: list[PaymentTotal]
    stated_totals: list[StatedAmount]
    ignored_amounts: list[IgnoredNumber]


@attr.s(auto_attribs=True)
class _Number:
    text: str
    amount: Optional[Decimal]
    start: int
    end: int
    is_percent: bool


@attr.s(auto_attribs=True)
class _Marker:
    start: int
    end: int
    category: str
    kind: Optional[str]


def _component_kind(match_text: str) -> str:
    if match_text == "定金":
        return DEPOSIT
    if match_text == "尾款":
        return BALANCE
    return DEDUCTION  # "抵" or "抵扣"


def _find_numbers(text: str, decimal_separator: Optional[str]) -> list[_Number]:
    numbers: list[_Number] = []
    for match in _NUMBER_RE.finditer(text):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        cleaned = raw.strip().rstrip(".,'").strip()
        if not any(char.isdigit() for char in cleaned):
            continue
        start = match.start() + leading
        end = start + len(cleaned)
        percent_match = _PERCENT_AFTER_RE.match(text, match.end())
        is_percent = percent_match is not None
        amount = parse_number(cleaned, decimal_separator)
        if amount is None and not is_percent:
            continue
        numbers.append(
            _Number(
                text=cleaned,
                amount=amount,
                start=start,
                end=end,
                is_percent=is_percent,
            )
        )
    return numbers


def _find_markers(text: str) -> list[_Marker]:
    markers = [
        _Marker(
            start=match.start(),
            end=match.end(),
            category="component",
            kind=_component_kind(match.group(0)),
        )
        for match in _COMPONENT_KEYWORD_RE.finditer(text)
    ]
    markers.extend(
        _Marker(match.start(), match.end(), "total", None)
        for match in _TOTAL_KEYWORD_RE.finditer(text)
    )
    markers.extend(
        _Marker(match.start(), match.end(), "ignore", None)
        for match in _IGNORE_KEYWORD_RE.finditer(text)
    )
    return sorted(markers, key=lambda marker: marker.start)


def _nearest_marker(markers: list[_Marker], number: _Number) -> Optional[_Marker]:
    preceding = [
        marker
        for marker in markers
        if marker.end <= number.start and number.start - marker.end <= _GAP
    ]
    if not preceding:
        return None
    return max(preceding, key=lambda marker: marker.end)


def extract_payments(
    price: Optional[str],
    currency_hint: Optional[str] = None,
    decimal_separator: Optional[str] = None,
    digit_group_separator: Optional[str] = None,
) -> PaymentBreakdown:
    """
    Extract deposit, deduction and balance payment components from a
    promotional pricing string.

    Return a :class:`PaymentBreakdown`. If the text contains no payment
    plan keywords, the component and total lists are empty; use
    :meth:`Price.fromstring` for plain single-amount prices.

    >>> breakdown = extract_payments("定金20抵50，尾款149")
    >>> [component.kind for component in breakdown.components]
    ['deposit', 'deduction', 'balance']
    >>> [(total.amount, total.interpretation) for total in breakdown.payment_totals]
    [(Decimal('169'), 'balance_post_deduction'), (Decimal('119'), 'balance_pre_deduction')]

    The deduction is a marketing value, not cash paid, so it is never added
    on top of the deposit and balance:

    >>> extract_payments("定金20抵50").payment_totals[0].amount
    Decimal('20')
    """
    if price is None:
        return PaymentBreakdown(None, [], [], [], [])

    if digit_group_separator is not None:
        price = price.replace(digit_group_separator, "")
    text = re.sub(r"\s+", " ", price).strip()
    if not text:
        return PaymentBreakdown(None, [], [], [], [])

    currency = extract_currency_symbol(text, currency_hint)
    if currency is not None:
        currency = currency.strip()

    numbers = _find_numbers(text, decimal_separator)
    markers = _find_markers(text)
    component_markers = [marker for marker in markers if marker.category == "component"]
    if not component_markers:
        # Not a payment plan; plain prices are handled by Price.fromstring.
        return PaymentBreakdown(currency, [], [], [], [])

    components: list[PaymentComponent] = []
    claimed: set[int] = set()
    for index, marker in enumerate(component_markers):
        next_start = (
            component_markers[index + 1].start
            if index + 1 < len(component_markers)
            else len(text)
        )
        for number_index, number in enumerate(numbers):
            if number_index in claimed or number.is_percent:
                continue
            if number.amount is None:
                continue
            gap = number.start - marker.end
            if 0 <= gap <= _GAP and number.start < next_start:
                assert marker.kind is not None
                components.append(
                    PaymentComponent(
                        kind=marker.kind,
                        amount=number.amount,
                        amount_text=number.text,
                        source_text=text[marker.start : number.end],
                        source_span=(marker.start, number.end),
                    )
                )
                claimed.add(number_index)
                break

    def _component(kind: str) -> Optional[PaymentComponent]:
        for component in components:
            if component.kind == kind:
                return component
        return None

    deposit = _component(DEPOSIT)
    deduction = _component(DEDUCTION)
    balance = _component(BALANCE)
    deposit_expected = any(marker.kind == DEPOSIT for marker in component_markers)

    stated_totals: list[StatedAmount] = []
    ignored_amounts: list[IgnoredNumber] = []
    for number_index, number in enumerate(numbers):
        if number_index in claimed:
            continue
        if number.is_percent:
            percent_end = _PERCENT_AFTER_RE.match(text, number.end)
            source_end = percent_end.end() if percent_end else number.end
            ignored_amounts.append(
                IgnoredNumber(
                    amount=number.amount,
                    amount_text=number.text,
                    reason=IGNORE_PERCENT,
                    source_text=text[number.start : source_end],
                )
            )
            continue
        nearest = _nearest_marker(markers, number)
        if nearest is not None and nearest.category == "total":
            if number.amount is not None:
                stated_totals.append(
                    StatedAmount(
                        amount=number.amount,
                        amount_text=number.text,
                        source_text=text[nearest.start : number.end],
                    )
                )
        elif nearest is not None and nearest.category == "ignore":
            ignored_amounts.append(
                IgnoredNumber(
                    amount=number.amount,
                    amount_text=number.text,
                    reason=IGNORE_REFERENCE_PRICE,
                    source_text=text[nearest.start : number.end],
                )
            )
        else:
            ignored_amounts.append(
                IgnoredNumber(
                    amount=number.amount,
                    amount_text=number.text,
                    reason=IGNORE_UNASSOCIATED,
                    source_text=text[number.start : number.end],
                )
            )

    payment_totals = _build_payment_totals(
        deposit, deduction, balance, stated_totals, deposit_expected
    )
    return PaymentBreakdown(
        currency=currency,
        components=components,
        payment_totals=payment_totals,
        stated_totals=stated_totals,
        ignored_amounts=ignored_amounts,
    )


def _build_payment_totals(
    deposit: Optional[PaymentComponent],
    deduction: Optional[PaymentComponent],
    balance: Optional[PaymentComponent],
    stated_totals: list[StatedAmount],
    deposit_expected: bool,
) -> list[PaymentTotal]:
    deposit_amount = deposit.amount if deposit is not None else Decimal(0)
    deposit_sources = [deposit.source_text] if deposit is not None else []
    stated_amounts = {stated.amount for stated in stated_totals}

    if balance is None:
        if deposit is None:
            return []
        total = PaymentTotal(
            amount=deposit.amount,
            interpretation=DEPOSIT_ONLY,
            sources=list(deposit_sources),
            complete=False,
            matches_stated_total=deposit.amount in stated_amounts,
        )
        return [total]

    post_amount = deposit_amount + balance.amount
    pre_amount: Optional[Decimal] = None
    if deduction is not None and balance.amount > deduction.amount:
        pre_amount = deposit_amount + balance.amount - deduction.amount

    post_supported = post_amount in stated_amounts
    pre_supported = pre_amount is not None and pre_amount in stated_amounts
    explicit_evidence = bool(stated_amounts) and (post_supported or pre_supported)

    interpretations: list[BalanceInterpretation] = []
    totals: list[PaymentTotal] = []
    if not explicit_evidence or post_supported:
        interpretations.append(
            BalanceInterpretation(BALANCE_POST_DEDUCTION, balance.amount)
        )
        totals.append(
            PaymentTotal(
                amount=post_amount,
                interpretation=BALANCE_POST_DEDUCTION,
                sources=[*deposit_sources, balance.source_text],
                complete=deposit is not None or not deposit_expected,
                matches_stated_total=post_supported,
            )
        )
    if pre_amount is not None and (not explicit_evidence or pre_supported):
        assert deduction is not None
        interpretations.append(
            BalanceInterpretation(
                BALANCE_PRE_DEDUCTION,
                balance.amount - deduction.amount,
            )
        )
        totals.append(
            PaymentTotal(
                amount=pre_amount,
                interpretation=BALANCE_PRE_DEDUCTION,
                sources=[
                    *deposit_sources,
                    balance.source_text,
                    deduction.source_text,
                ],
                complete=deposit is not None or not deposit_expected,
                matches_stated_total=pre_supported,
            )
        )

    balance.interpretations = interpretations
    return totals
