"""Parsing of rental/lease payment plans written in the agreed line format.

A rental quote page puts promotional monthly payments, the first-installment
fee and the final buyout on the same line.  The agreed format is a single
string with semicolon- or whitespace-separated components, each introduced by
a Chinese keyword, for example::

    首付 2000元；押金 3000元 可退；首期手续费 500元；
    促销月付 999元 前3期；月付 1299元 第4-12期；尾款买断 8000元

The parser separates cash flows the customer is contractually obliged to pay
(:attr:`PaymentPlan.mandatory`) from cash flows the customer may choose or
that depend on a declared field (:attr:`PaymentPlan.optional`).

Date rules:

* upfront components (down payment, deposit, first-installment fee) are due
  on the signing date;
* installment *n* is due on the last calendar day of the *n*-th month after
  the signing date;
* the buyout and a refundable deposit are settled at the end of the term.

Promotions are only accepted for the first three installments.  Whether a
deposit is refundable is taken from the component itself (``可退`` /
``不退``); it is never guessed.

When the page only discloses a minimum monthly payment, no installment
schedule is fabricated and the minimum payment is never multiplied by the
full term to be presented as the total cost; only a computable lower bound is
exposed through :attr:`PaymentPlan.total_lower_bound`.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

import attr

from .parser import Price

if TYPE_CHECKING:
    from collections.abc import Iterator

PROMOTION_LAST_PERIOD = 3

# Full-width digits and punctuation are common on scraped pages.
_FULLWIDTH_DIGITS = {
    ord(ch): str(index) for index, ch in enumerate("０１２３４５６７８９")
}
_FULLWIDTH_PUNCTUATION = {
    ord("，"): ",",
    ord("；"): ";",
    ord("、"): ";",
    ord("　"): " ",
}

# Keywords are ordered so that longer, more specific names match first.
_COMPONENT_RE = re.compile(
    r"(?:(?P<promo>促销|优惠)\s*)?"
    r"(?P<kind>"
    r"最低月供|最低月付|首期手续费|手续费|"
    r"首付|定金|押金|月付|月供|"
    r"尾款买断|尾款|买断"
    r")"
)
_TERM_RE = re.compile(r"共\s*(\d+)\s*期")
_PROMO_PERIOD_RE = re.compile(r"前\s*(\d+)\s*期")
_RANGE_RE = re.compile(r"第\s*(\d+)\s*期?\s*[-~至到]\s*(\d+)\s*期?")
_COUNT_RE = re.compile(r"[×xX*]\s*(\d+)\s*期?|(\d+)\s*期")

# Longest phrases first, so that "不退还" is stripped before "退还".
_NON_REFUNDABLE_WORDS = ("不予退还", "不可返还", "不退还", "不可退", "不退")
_REFUNDABLE_WORDS = ("可返还", "可退回", "可退", "退还")
_REFUND_WORDS = _NON_REFUNDABLE_WORDS + _REFUNDABLE_WORDS
_NON_REFUNDABLE_RE = re.compile("|".join(_NON_REFUNDABLE_WORDS))
_REFUNDABLE_RE = re.compile("|".join(_REFUNDABLE_WORDS))

# Period markers must be hidden from the generic amount extractor so that
# "第4-12期" can never be mistaken for a money amount.
_PERIOD_MARKER_RES = (
    _TERM_RE,
    _PROMO_PERIOD_RE,
    _RANGE_RE,
    re.compile(r"[×xX*]\s*\d+\s*期?"),
    re.compile(r"\d+\s*期"),
)

# Foreign currency names ending in 元, checked before the plain 元 fallback.
_FOREIGN_YUAN_CURRENCIES = {
    "美元": "USD",
    "美金": "USD",
    "港元": "HKD",
    "港币": "HKD",
    "加元": "CAD",
    "澳元": "AUD",
    "日元": "JPY",
    "欧元": "EUR",
    "新元": "SGD",
}
_YUAN_RE = re.compile(r"(?<!美)元|圆|块")

_UPFRONT_KINDS = {"down_payment", "deposit", "fee"}

_KIND_LABELS = {
    "down_payment": "首付",
    "deposit": "押金",
    "fee": "首期手续费",
    "installment": "月付",
    "promo_installment": "促销月付",
    "balloon": "尾款买断",
    "deposit_refund": "押金退还",
    "minimum_monthly": "最低月供",
}


class PaymentParseError(ValueError):
    """Raised when a payment plan string does not follow the agreed format."""


class _Component(TypedDict):
    kind: str
    promo: bool
    body: str
    refundable: bool | None


@attr.s(auto_attribs=True, frozen=True)
class CashFlow:
    """A single cash movement (a payment, or a negative refund)."""

    description: str
    amount: Decimal
    currency: str | None
    due_date: date
    required: bool
    kind: str
    period: int | None = None
    refundable: bool | None = None


@attr.s(auto_attribs=True, frozen=True)
class PaymentPlan:
    """Payment plan split into mandatory and optional cash flows."""

    mandatory: list[CashFlow]
    optional: list[CashFlow]
    currency: str | None
    term_months: int | None = None
    minimum_monthly_payment: Decimal | None = None

    @classmethod
    def parse(
        cls,
        text: str | None,
        signing_date: date | str,
        decimal_separator: str | None = None,
        digit_group_separator: str | None = None,
    ) -> PaymentPlan:
        """Parse an agreed-format payment plan string.

        ``signing_date`` is the contract signing date; it accepts a
        :class:`datetime.date` or an ``YYYY-MM-DD`` string. Upfront cash flows
        are dated on it, installments on month ends after it.

        >>> plan = PaymentPlan.parse(
        ...     "首付 2000元；押金 3000元 可退；首期手续费 500元；"
        ...     "促销月付 999元 前3期；月付 1299元 第4-12期；尾款买断 8000元",
        ...     signing_date="2026-11-14",
        ... )
        >>> len(plan.mandatory)
        15
        >>> [flow.period for flow in plan.mandatory
        ...  if flow.kind == "promo_installment"]
        [1, 2, 3]
        >>> plan.mandatory[0].due_date
        datetime.date(2026, 11, 14)
        >>> plan.mandatory[3].due_date
        datetime.date(2026, 12, 31)
        >>> sorted(flow.kind for flow in plan.optional)
        ['balloon', 'deposit_refund']
        >>> plan.total_lower_bound
        Decimal('20188')
        """
        if text is None or not text.strip():
            msg = "payment plan text is empty"
            raise PaymentParseError(msg)
        contract_date = _coerce_date(signing_date)
        normalized = _normalize(text)

        explicit_term = _parse_single_term(normalized)
        components = list(_iter_components(normalized))
        if not components:
            msg = f"no recognizable payment component in {text!r}"
            raise PaymentParseError(msg)

        currency = _shared_currency(components)
        upfront: list[CashFlow] = []
        optional: list[CashFlow] = []
        installments: dict[int, Decimal] = {}
        promo_periods: set[int] = set()
        minimum_payment: Decimal | None = None
        has_balloon = False
        seen_kinds: set[str] = set()
        next_period = 1

        for component in components:
            kind = component["kind"]
            if kind in seen_kinds and kind != "installment":
                msg = f"duplicate payment component: {_KIND_LABELS.get(kind, kind)}"
                raise PaymentParseError(msg)
            seen_kinds.add(kind)
            amount = _component_amount(
                component, decimal_separator, digit_group_separator
            )

            if kind in _UPFRONT_KINDS:
                upfront.append(
                    CashFlow(
                        description=_KIND_LABELS[kind],
                        amount=amount,
                        currency=currency,
                        due_date=contract_date,
                        required=True,
                        kind=kind,
                        refundable=(
                            component["refundable"] if kind == "deposit" else False
                        ),
                    )
                )
            elif kind == "balloon":
                has_balloon = True
                optional.append(
                    CashFlow(
                        description=_KIND_LABELS["balloon"],
                        amount=amount,
                        currency=currency,
                        due_date=contract_date,  # replaced once the term is known
                        required=False,
                        kind=kind,
                    )
                )
            elif kind == "minimum_monthly":
                if installments:
                    msg = (
                        "minimum monthly payment cannot be combined "
                        "with fixed installments"
                    )
                    raise PaymentParseError(msg)
                minimum_payment = amount
            else:
                next_period = _collect_installments(
                    component,
                    amount,
                    installments,
                    promo_periods,
                    explicit_term,
                    next_period,
                )

        term_months = explicit_term
        if installments:
            covered = set(installments)
            inferred_term = max(covered)
            if term_months is None:
                term_months = inferred_term
            if covered != set(range(1, term_months + 1)):
                missing = sorted(set(range(1, term_months + 1)) - covered)
                msg = (
                    "installment schedule is not contiguous, "
                    f"missing periods: {missing}"
                )
                raise PaymentParseError(msg)
        if minimum_payment is not None and installments:
            msg = "minimum monthly payment cannot be combined with fixed installments"
            raise PaymentParseError(msg)

        mandatory = list(upfront)
        for period in sorted(installments):
            is_promo = period in promo_periods
            mandatory.append(
                CashFlow(
                    description=(
                        _KIND_LABELS["promo_installment"]
                        if is_promo
                        else _KIND_LABELS["installment"]
                    ),
                    amount=installments[period],
                    currency=currency,
                    due_date=_month_end_after(contract_date, period),
                    required=True,
                    kind="promo_installment" if is_promo else "installment",
                    period=period,
                )
            )

        if term_months is None:
            end_date = contract_date
        else:
            end_date = _month_end_after(contract_date, term_months)
        optional = [attr.evolve(flow, due_date=end_date) for flow in optional]

        deposit = next((flow for flow in upfront if flow.kind == "deposit"), None)
        if deposit is not None and deposit.refundable:
            if term_months is None:
                msg = "refundable deposit requires a known term"
                raise PaymentParseError(msg)
            optional.append(
                CashFlow(
                    description=_KIND_LABELS["deposit_refund"],
                    amount=-deposit.amount,
                    currency=currency,
                    due_date=end_date,
                    required=False,
                    kind="deposit_refund",
                    refundable=True,
                )
            )
        if has_balloon and term_months is None:
            msg = "buyout amount requires a known term (use '共N期' or fixed installments)"
            raise PaymentParseError(msg)

        return cls(
            mandatory=mandatory,
            optional=optional,
            currency=currency,
            term_months=term_months,
            minimum_monthly_payment=minimum_payment,
        )

    @property
    def mandatory_total(self) -> Decimal:
        """Sum of the amounts the customer is obliged to pay."""
        return _sum_amounts(self.mandatory)

    @property
    def optional_total(self) -> Decimal:
        """Sum of the optional cash flows; refunds count as negative amounts."""
        return _sum_amounts(self.optional)

    @property
    def is_lower_bound(self) -> bool:
        """True when only a minimum monthly payment is disclosed."""
        return self.minimum_monthly_payment is not None

    @property
    def total_lower_bound(self) -> Decimal:
        """Best computable lower bound on the customer's total outlay.

        For a fully specified plan it is the exact mandatory total.  When only
        a minimum monthly payment is given, the disclosed minimum is never
        multiplied by the full term as a quoted total; instead, the bound adds
        ``minimum_monthly_payment * term`` when the term itself is disclosed,
        or a single minimum payment when the term is unknown, on top of the
        known upfront money.
        """
        bound = self.mandatory_total
        if self.minimum_monthly_payment is not None:
            multiplier = self.term_months if self.term_months is not None else 1
            bound += self.minimum_monthly_payment * multiplier
        return bound


parse_payment_plan = PaymentPlan.parse


def _normalize(text: str) -> str:
    return text.translate(_FULLWIDTH_DIGITS).translate(_FULLWIDTH_PUNCTUATION)


def _coerce_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        msg = f"signing_date must be a date or YYYY-MM-DD string, got {value!r}"
        raise PaymentParseError(msg) from exc


def _month_end_after(anchor: date, months: int) -> date:
    """Last calendar day of the month ``months`` after ``anchor``."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _parse_single_term(text: str) -> int | None:
    match = _TERM_RE.search(text)
    if match is None:
        return None
    return int(match.group(1))


def _iter_components(text: str) -> Iterator[_Component]:
    matches = list(_COMPONENT_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # "共N期" is a plan-level term marker, not part of this component.
        body = _TERM_RE.sub(" ", text[match.start() : end])
        kind = _normalize_kind(match.group("kind"))
        yield {
            "kind": kind,
            "promo": match.group("promo") is not None or kind == "minimum_monthly",
            "body": body,
            "refundable": _parse_refundable(body),
        }


def _normalize_kind(label: str) -> str:
    if label in {"首付", "定金"}:
        return "down_payment"
    if label == "押金":
        return "deposit"
    if label in {"首期手续费", "手续费"}:
        return "fee"
    if label in {"最低月供", "最低月付"}:
        return "minimum_monthly"
    if label in {"尾款买断", "尾款", "买断"}:
        return "balloon"
    return "installment"


def _parse_refundable(body: str) -> bool | None:
    # Non-refundable phrases contain refundable substrings ("不退还" ->
    # "退还"), so they must be checked first.
    if _NON_REFUNDABLE_RE.search(body):
        return False
    if _REFUNDABLE_RE.search(body):
        return True
    return None


def _strip_period_markers(body: str) -> str:
    cleaned = body
    for marker_re in _PERIOD_MARKER_RES:
        cleaned = marker_re.sub(" ", cleaned)
    for word in _REFUND_WORDS:
        cleaned = cleaned.replace(word, " ")
    return cleaned


def _extract_price(
    component: _Component,
    decimal_separator: str | None,
    digit_group_separator: str | None,
) -> Price:
    body = _strip_period_markers(component["body"])
    price = Price.fromstring(
        body,
        decimal_separator=decimal_separator,
        digit_group_separator=digit_group_separator,
    )
    if price.currency is None:
        for foreign_name, foreign_code in _FOREIGN_YUAN_CURRENCIES.items():
            if foreign_name in body:
                price.currency = foreign_code
                break
        else:
            if _YUAN_RE.search(body):
                price.currency = "元"
    return price


def _component_amount(
    component: _Component,
    decimal_separator: str | None,
    digit_group_separator: str | None,
) -> Decimal:
    price = _extract_price(component, decimal_separator, digit_group_separator)
    if price.amount is None:
        label = _KIND_LABELS.get(component["kind"], component["kind"])
        msg = f"no amount found for {label} in {component['body']!r}"
        raise PaymentParseError(msg)
    return price.amount


def _shared_currency(components: list[_Component]) -> str | None:
    currencies: set[str] = set()
    for component in components:
        price = _extract_price(component, None, None)
        if price.currency:
            currencies.add(price.currency)
    if len(currencies) > 1:
        msg = f"conflicting currencies in payment plan: {sorted(currencies)}"
        raise PaymentParseError(msg)
    return next(iter(currencies)) if currencies else None


def _collect_installments(
    component: _Component,
    amount: Decimal,
    installments: dict[int, Decimal],
    promo_periods: set[int],
    explicit_term: int | None,
    next_period: int,
) -> int:
    """Add the component's installments; return the next free period."""
    body = component["body"]
    promo = bool(component["promo"])

    range_match = _RANGE_RE.search(body)
    if range_match is not None:
        # Explicit ranges use absolute period numbers.
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if start < 1 or end < start:
            msg = f"invalid installment period range in {body!r}"
            raise PaymentParseError(msg)
        if promo:
            _ensure_promo_window(start, end, body)
            promo_periods.update(range(start, end + 1))
        periods = range(start, end + 1)
        cursor = end + 1
    else:
        # Count-based segments continue after the previous segments.
        count = _parse_count(body, promo, explicit_term)
        if promo:
            _ensure_promo_window(next_period, next_period + count - 1, body)
            promo_periods.update(range(next_period, next_period + count))
        periods = range(next_period, next_period + count)
        cursor = next_period + count

    for period in periods:
        if period in installments:
            msg = f"installment period {period} is defined more than once"
            raise PaymentParseError(msg)
        installments[period] = amount
    return cursor


def _parse_count(body: str, promo: bool, explicit_term: int | None) -> int:
    promo_match = _PROMO_PERIOD_RE.search(body)
    if promo_match is not None:
        count = int(promo_match.group(1))
    else:
        count_match = _COUNT_RE.search(body)
        if count_match is not None:
            count_text = count_match.group(1)
            if count_text is None:
                count_text = count_match.group(2)
            count = int(count_text)
        elif promo:
            if explicit_term is None:
                msg = (
                    f"promotional payment needs a period count, e.g. '前3期': {body!r}"
                )
                raise PaymentParseError(msg)
            count = min(PROMOTION_LAST_PERIOD, explicit_term)
        else:
            # A global "共N期" term is not a count of this specific component;
            # without an explicit count or range the schedule would be ambiguous.
            msg = (
                "installment count is missing "
                f"(use '×N期', '前N期' or '第a-b期'): {body!r}"
            )
            raise PaymentParseError(msg)
    if count < 1:
        msg = f"installment count must be a positive number in {body!r}"
        raise PaymentParseError(msg)
    return count


def _ensure_promo_window(start: int, end: int, body: str) -> None:
    if end > PROMOTION_LAST_PERIOD:
        msg = (
            "promotions are limited to the first "
            f"{PROMOTION_LAST_PERIOD} installments, got period {end} in {body!r}"
        )
        raise PaymentParseError(msg)


def _sum_amounts(flows: list[CashFlow]) -> Decimal:
    total = Decimal(0)
    for flow in flows:
        total += flow.amount
    return total
