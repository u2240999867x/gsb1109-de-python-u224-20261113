"""Tests for price_parser.history: comparable price-change detection."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from price_parser import (
    PriceTag,
    TaxCalendar,
    TaxMode,
    TaxRateChange,
    Verdict,
    analyze_price_history,
    normalize_tag,
)

DE_VAT_19 = TaxCalendar({"DE": [TaxRateChange(date(2020, 1, 1), Decimal("0.19"))]})


def de_tag(
    tag_id: str,
    captured: date,
    price_text: str,
    tax_mode: TaxMode,
    grams: str,
    product_id: str = "p1",
) -> PriceTag:
    return PriceTag(
        tag_id,
        product_id,
        captured,
        price_text,
        region="DE",
        tax_mode=tax_mode,
        net_grams_min=Decimal(grams),
    )


class TestShrinkflationScenario:
    """The merchant keeps the product id, shrinks 500 g -> 450 g, and
    switches from tax-inclusive to tax-exclusive display."""

    tags = [
        de_tag("t1", date(2026, 1, 10), "5,99 €", TaxMode.INCLUSIVE, "500"),
        de_tag("t2", date(2026, 3, 1), "5,49 €", TaxMode.EXCLUSIVE, "450"),
    ]

    def test_definite_increase_despite_lower_gross_price(self) -> None:
        events = analyze_price_history(self.tags, DE_VAT_19)
        assert len(events) == 1
        event = events[0]
        assert event.verdict is Verdict.DEFINITE_INCREASE
        assert event.from_date == date(2026, 1, 10)
        assert event.to_date == date(2026, 3, 1)

    def test_increase_is_traceable_to_both_price_tags(self) -> None:
        (event,) = analyze_price_history(self.tags, DE_VAT_19)
        supporting = event.supporting_pairs
        assert len(supporting) == 1
        pair = supporting[0]
        assert pair.before_tag.tag_id == "t1"
        assert pair.after_tag.tag_id == "t2"
        assert pair.before_tag.price_text == "5,99 €"
        assert pair.after_tag.price_text == "5,49 €"

    def test_normalization_uses_net_unit_prices(self) -> None:
        before = normalize_tag(self.tags[0], DE_VAT_19)
        after = normalize_tag(self.tags[1], DE_VAT_19)
        assert (
            before.unit_min
            == before.unit_max
            == Decimal("5.99") / Decimal("1.19") / 500
        )
        assert after.unit_min == after.unit_max == Decimal("5.49") / 450
        assert after.unit_min > before.unit_max


class TestRangeUncertainty:
    """Tax rate or net content known only as a range -> interval prices."""

    calendar = TaxCalendar(
        {"XY": [TaxRateChange(date(2026, 1, 1), Decimal("0.18"), Decimal("0.20"))]}
    )

    def tag(self, tag_id: str, captured: date, price_text: str) -> PriceTag:
        return PriceTag(
            tag_id,
            "p1",
            captured,
            price_text,
            region="XY",
            tax_mode=TaxMode.INCLUSIVE,
            net_grams_min=Decimal(500),
        )

    def test_small_jump_inside_interval_overlap_is_possible_increase(self) -> None:
        events = analyze_price_history(
            [
                self.tag("t1", date(2026, 2, 1), "€10.00"),
                self.tag("t2", date(2026, 3, 1), "€10.10"),
            ],
            self.calendar,
        )
        assert events[0].verdict is Verdict.POSSIBLE_INCREASE
        (pair,) = events[0].supporting_pairs
        assert (pair.before_tag.tag_id, pair.after_tag.tag_id) == ("t1", "t2")

    def test_jump_above_whole_interval_is_definite_increase(self) -> None:
        events = analyze_price_history(
            [
                self.tag("t1", date(2026, 2, 1), "€10.00"),
                self.tag("t2", date(2026, 3, 1), "€10.60"),
            ],
            self.calendar,
        )
        assert events[0].verdict is Verdict.DEFINITE_INCREASE

    def test_net_content_range_widens_interval(self) -> None:
        exact = PriceTag(
            "t1",
            "p1",
            date(2026, 2, 1),
            "€10.00",
            region="DE",
            tax_mode=TaxMode.EXCLUSIVE,
            net_grams_min=Decimal(500),
        )
        ranged = PriceTag(
            "t2",
            "p1",
            date(2026, 3, 1),
            "€5.10",
            region="DE",
            tax_mode=TaxMode.EXCLUSIVE,
            net_grams_min=Decimal(450),
            net_grams_max=Decimal(500),
        )
        events = analyze_price_history([exact, ranged], DE_VAT_19)
        # 10/500 = 0.02 vs [5.10/500, 5.10/450] = [0.0102, 0.01133]: the whole
        # new interval is below the old one, so definitely not an increase.
        assert events[0].verdict is Verdict.NO_INCREASE

    def test_unknown_tax_mode_widens_interval(self) -> None:
        unknown = PriceTag(
            "t1",
            "p1",
            date(2026, 2, 1),
            "€10.00",
            region="DE",
            tax_mode=TaxMode.UNKNOWN,
            net_grams_min=Decimal(500),
        )
        normalized = normalize_tag(unknown, DE_VAT_19)
        assert normalized.unit_min == Decimal("10.00") / Decimal("1.19") / 500
        assert normalized.unit_max == Decimal("10.00") / 500


class TestSameDayVersions:
    """Two versions captured on the same day must not be resolved by
    scrape order."""

    a1 = de_tag("a1", date(2026, 1, 10), "5,99 €", TaxMode.INCLUSIVE, "500")
    b1 = de_tag("b1", date(2026, 2, 15), "5,99 €", TaxMode.INCLUSIVE, "500")
    b2 = de_tag("b2", date(2026, 2, 15), "5,49 €", TaxMode.EXCLUSIVE, "450")
    c1 = de_tag("c1", date(2026, 3, 1), "5,69 €", TaxMode.EXCLUSIVE, "450")

    def test_verdicts_do_not_depend_on_input_order(self) -> None:
        events_forward = analyze_price_history(
            [self.a1, self.b1, self.b2, self.c1], DE_VAT_19
        )
        events_shuffled = analyze_price_history(
            [self.c1, self.b2, self.a1, self.b1], DE_VAT_19
        )
        assert events_forward == events_shuffled

    def test_mixed_same_day_versions_yield_possible_increase(self) -> None:
        events = analyze_price_history([self.a1, self.b1, self.b2, self.c1], DE_VAT_19)
        assert [event.verdict for event in events] == [
            Verdict.POSSIBLE_INCREASE,  # b1 unchanged, b2 increased
            Verdict.DEFINITE_INCREASE,  # c1 above both b1 and b2
        ]

    def test_same_day_tags_are_never_compared_to_each_other(self) -> None:
        events = analyze_price_history([self.a1, self.b1, self.b2, self.c1], DE_VAT_19)
        for event in events:
            for pair in event.pairs:
                assert pair.before_tag.captured < pair.after_tag.captured

    def test_supporting_pairs_of_definite_event(self) -> None:
        events = analyze_price_history([self.a1, self.b1, self.b2, self.c1], DE_VAT_19)
        supporting = events[1].supporting_pairs
        assert {(p.before_tag.tag_id, p.after_tag.tag_id) for p in supporting} == {
            ("b1", "c1"),
            ("b2", "c1"),
        }


class TestTaxEffectiveDates:
    """The tax rate in effect on the capture date is used, per tag."""

    calendar = TaxCalendar(
        {
            "DE": [
                TaxRateChange(date(2020, 1, 1), Decimal("0.16")),
                TaxRateChange(date(2026, 7, 1), Decimal("0.19")),
            ]
        }
    )

    def test_tax_hike_is_not_misread_as_price_change(self) -> None:
        events = analyze_price_history(
            [
                de_tag("t1", date(2026, 6, 1), "5,99 €", TaxMode.INCLUSIVE, "500"),
                de_tag("t2", date(2026, 8, 1), "5,99 €", TaxMode.INCLUSIVE, "500"),
            ],
            self.calendar,
        )
        # Same gross price, but the net-of-tax unit price went down when
        # VAT rose from 16 % to 19 %: definitely not an increase.
        assert events[0].verdict is Verdict.NO_INCREASE


class TestInsufficientEvidence:
    def test_missing_tax_rate(self) -> None:
        tag = PriceTag(
            "t1",
            "p1",
            date(2026, 2, 1),
            "5,99 €",
            region="XX",
            tax_mode=TaxMode.INCLUSIVE,
            net_grams_min=Decimal(500),
        )
        normalized = normalize_tag(tag, DE_VAT_19)
        assert not normalized.comparable
        assert "XX" in (normalized.problem or "")

    def test_exclusive_price_needs_no_tax_rate(self) -> None:
        tag = PriceTag(
            "t1",
            "p1",
            date(2026, 2, 1),
            "5,99 €",
            tax_mode=TaxMode.EXCLUSIVE,
            net_grams_min=Decimal(500),
        )
        assert normalize_tag(tag, DE_VAT_19).comparable

    def test_missing_net_content(self) -> None:
        tag = PriceTag(
            "t1",
            "p1",
            date(2026, 2, 1),
            "5,99 €",
            region="DE",
            tax_mode=TaxMode.EXCLUSIVE,
        )
        normalized = normalize_tag(tag, DE_VAT_19)
        assert not normalized.comparable
        assert "net content" in (normalized.problem or "")

    def test_unparseable_price(self) -> None:
        tag = PriceTag(
            "t1",
            "p1",
            date(2026, 2, 1),
            "currently unavailable",
            region="DE",
            tax_mode=TaxMode.EXCLUSIVE,
            net_grams_min=Decimal(500),
        )
        normalized = normalize_tag(tag, DE_VAT_19)
        assert not normalized.comparable

    def test_currency_mismatch(self) -> None:
        events = analyze_price_history(
            [
                de_tag("t1", date(2026, 1, 10), "5,99 €", TaxMode.EXCLUSIVE, "500"),
                de_tag("t2", date(2026, 3, 1), "$6.49", TaxMode.EXCLUSIVE, "500"),
            ],
            DE_VAT_19,
        )
        assert events[0].verdict is Verdict.INSUFFICIENT_EVIDENCE
        assert events[0].supporting_pairs == ()
        assert "currency" in (events[0].pairs[0].reason or "")

    def test_event_with_no_comparable_pairs_is_insufficient(self) -> None:
        events = analyze_price_history(
            [
                PriceTag("t1", "p1", date(2026, 1, 10), "5,99 €"),
                PriceTag("t2", "p1", date(2026, 3, 1), "6,49 €"),
            ],
            DE_VAT_19,
        )
        assert events[0].verdict is Verdict.INSUFFICIENT_EVIDENCE


class TestInputValidation:
    def test_rate_range_must_be_ordered(self) -> None:
        with pytest.raises(ValueError, match="rate_max"):
            TaxRateChange(date(2026, 1, 1), Decimal("0.20"), Decimal("0.18"))

    def test_net_content_range_must_be_ordered(self) -> None:
        with pytest.raises(ValueError, match="net_grams_max"):
            PriceTag(
                "t1",
                "p1",
                date(2026, 1, 10),
                "5,99 €",
                net_grams_min=Decimal(500),
                net_grams_max=Decimal(450),
            )


def test_multiple_products_are_reported_separately() -> None:
    tags = [
        de_tag("a1", date(2026, 1, 10), "5,99 €", TaxMode.EXCLUSIVE, "500"),
        de_tag("a2", date(2026, 3, 1), "6,49 €", TaxMode.EXCLUSIVE, "500"),
        de_tag(
            "b1",
            date(2026, 1, 10),
            "1,99 €",
            TaxMode.EXCLUSIVE,
            "500",
            product_id="p2",
        ),
        de_tag(
            "b2",
            date(2026, 3, 1),
            "1,99 €",
            TaxMode.EXCLUSIVE,
            "500",
            product_id="p2",
        ),
    ]
    events = analyze_price_history(tags, DE_VAT_19)
    assert [(event.product_id, event.verdict) for event in events] == [
        ("p1", Verdict.DEFINITE_INCREASE),
        ("p2", Verdict.NO_INCREASE),
    ]
