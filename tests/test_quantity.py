from __future__ import annotations

from decimal import Decimal

import pytest

from price_parser.quantity import (
    QuantityError,
    UnitMismatchError,
    UnitPriceRange,
    UnknownGiftSizeError,
    compare_quotes,
    convert_amount,
    normalize_unit,
    parse_pack,
    parse_quote,
    unit_price,
)


class TestPackParsing:
    def test_multipack_with_gift_count_only(self) -> None:
        pack = parse_pack("12×250 mL 加赠 2 瓶")
        assert pack.count == 12
        assert pack.size == Decimal(250)
        assert pack.unit == "mL"
        assert pack.container is None
        assert pack.gift_count == 2
        assert pack.gift_size is None
        assert pack.gift_container == "瓶"
        assert pack.min_order_boxes == 1

    def test_same_model_gift(self) -> None:
        pack = parse_pack("12×250 mL 加赠 2 瓶 同款")
        assert pack.gift_count == 2
        assert pack.gift_same_as_base
        assert pack.assumed_gift_size == Decimal(250)

    def test_different_size_gift(self) -> None:
        pack = parse_pack("12×250 mL 加赠 2×100 mL")
        assert pack.gift_count == 2
        assert pack.gift_size == Decimal(100)
        assert pack.gift_unit == "mL"
        assert not pack.gift_same_as_base

    def test_gift_size_in_parentheses(self) -> None:
        pack = parse_pack("12×250mL 加赠 2瓶(100mL)")
        assert pack.gift_count == 2
        assert pack.gift_size == Decimal(100)
        assert pack.gift_unit == "mL"

    def test_gift_count_range(self) -> None:
        pack = parse_pack("12×250 mL 赠 1-2 瓶")
        assert pack.gift_count == 2
        assert pack.gift_count_bounds == (1, 2)

    def test_moq_is_not_pack_capacity(self) -> None:
        pack = parse_pack("两箱起购 12×250 mL 加赠 2 瓶")
        assert pack.count == 12
        assert pack.size == Decimal(250)
        assert pack.total_size == Decimal(3000)
        assert pack.min_order_boxes == 2
        assert pack.order_size == Decimal(6000)

    def test_moq_without_gift(self) -> None:
        pack = parse_pack("两箱起购 12×250 mL")
        assert pack.count == 12
        assert pack.gift_count == 0
        assert pack.min_order_boxes == 2

    def test_english_pack_and_gift(self) -> None:
        pack = parse_pack("12 bottles 250 mL plus 2 bottles")
        assert pack.count == 12
        assert pack.size == Decimal(250)
        assert pack.unit == "mL"
        assert pack.gift_count == 2
        assert pack.gift_container == "瓶"

    def test_unparseable_pack(self) -> None:
        with pytest.raises(QuantityError):
            parse_pack("no quantity here")


class TestUnitPrices:
    def test_exclude_gifts(self) -> None:
        price = unit_price("¥59.90", "12×250 mL 加赠 2 瓶", "L", include_gifts=False)
        assert price == Decimal("59.90") / Decimal(3)

    def test_gift_size_unknown_is_not_silently_assumed(self) -> None:
        quote = parse_quote("¥59.90 12×250 mL 加赠 2 瓶")
        with pytest.raises(UnknownGiftSizeError):
            quote.unit_price("L", include_gifts=True)

    def test_same_model_gift_is_included(self) -> None:
        quote = parse_quote("¥59.90 12×250 mL 加赠 2 瓶 同款")
        price = quote.unit_price("L")
        assert price == Decimal("59.90") / Decimal("3.5")

    def test_explicit_gift_size(self) -> None:
        quote = parse_quote("¥59.90 12×250 mL 加赠 2×100 mL")
        assert quote.unit_price("L") == Decimal("59.90") / Decimal("3.2")

    def test_unknown_gift_size_gives_range(self) -> None:
        quote = parse_quote("¥59.90 12×250 mL 加赠 2 瓶")
        price_range = quote.unit_price_range("L")
        assert isinstance(price_range, UnitPriceRange)
        assert price_range.low == Decimal("59.90") / Decimal("3.5")
        assert price_range.high == Decimal("59.90") / Decimal(3)
        assert price_range.quantity_low == Decimal(3)
        assert price_range.quantity_high == Decimal("3.5")
        assert price_range.gift_size_min == Decimal(0)
        assert price_range.gift_size_max == Decimal(250)
        assert (
            "gift size unknown; assumed between 0 and the paid unit size"
            in price_range.assumptions
        )

    def test_declared_gift_bounds(self) -> None:
        quote = parse_quote("¥59.90 12×250 mL 加赠 2 瓶")
        price_range = quote.unit_price_range(
            "L",
            gift_size_bounds=(Decimal(100), Decimal(200)),
        )
        assert price_range.low == Decimal("59.90") / Decimal("3.4")
        assert price_range.high == Decimal("59.90") / Decimal("3.2")
        assert "caller-supplied gift size bounds" in price_range.assumptions

    def test_count_target_does_not_need_gift_volume(self) -> None:
        quote = parse_quote("¥59.90 12×250 mL 加赠 2 瓶")
        assert quote.unit_price("瓶") == Decimal("59.90") / Decimal(14)

    def test_target_amount(self) -> None:
        quote = parse_quote("¥59.90 12×250 mL 加赠 2×100 mL")
        assert quote.unit_price("100mL") == Decimal("59.90") / Decimal(32)
        assert quote.unit_price("mL", target_amount="100") == Decimal("59.90") / Decimal(
            32
        )

    def test_moq_does_not_multiply_unit_price(self) -> None:
        quote = parse_quote("¥59.90 两箱起购 12×250 mL")
        assert quote.unit_price("L", include_gifts=False) == Decimal("59.90") / Decimal(
            3
        )

    def test_mass_conversion(self) -> None:
        assert convert_amount(Decimal("1.5"), "kg", "g") == Decimal("1500.0")
        assert normalize_unit("毫升") == "mL"

    def test_incompatible_units(self) -> None:
        with pytest.raises(UnitMismatchError):
            parse_pack("12×250 mL").base_quantity("g")


class TestComparison:
    def test_unknown_gift_size_changes_ranking(self) -> None:
        quote_a = parse_quote("¥59.90 12×250 mL 加赠 2 瓶", label="A")
        quote_b = parse_quote("¥55.50 12×250 mL", label="B")
        comparison = compare_quotes([quote_a, quote_b], target_unit="L")

        assert comparison.ranges["B"].is_exact
        assert not comparison.ranges["A"].is_exact
        assert comparison.ranges["A"].low < comparison.ranges["B"].low
        assert comparison.ranges["A"].high > comparison.ranges["B"].low
        assert not comparison.certain
        assert comparison.winner is None
        assert set(comparison.possible_winners) == {"A", "B"}

        changes = [c for c in comparison.changes if c.quote == "A"]
        assert len(changes) == 1
        # A matches B's 18.50/L price at 118.9 mL per free bottle.
        assert changes[0].gift_size == pytest.approx(
            Decimal("118.9189189189189189189189190")
        )
        assert changes[0].gift_total == pytest.approx(
            Decimal("237.8378378378378378378378380")
        )
        assert changes[0].gift_size_unit == "mL"
        assert changes[0].within_bounds

    def test_certain_ranking_without_interval_overlap(self) -> None:
        quote_a = parse_quote("¥59.90 12×250 mL 加赠 2×250 mL", label="A")
        quote_b = parse_quote("¥80.00 12×250 mL", label="B")
        comparison = compare_quotes([quote_a, quote_b], target_unit="L")
        assert comparison.certain
        assert comparison.winner == "A"
        assert comparison.possible_winners == ("A",)

    def test_declared_bounds_can_make_ranking_certain(self) -> None:
        quote_a = parse_quote("¥59.90 12×250 mL 加赠 2 瓶", label="A")
        quote_b = parse_quote("¥55.50 12×250 mL", label="B")
        # A cannot beat B once each gift is limited to 100 mL.
        comparison = compare_quotes(
            [quote_a, quote_b],
            target_unit="L",
            gift_size_bounds=(Decimal(0), Decimal(100)),
        )
        assert comparison.ranges["A"].low > comparison.ranges["B"].high
        assert comparison.certain
        assert comparison.winner == "B"
        assert not any(change.within_bounds for change in comparison.changes)

    def test_gift_size_crossover_outside_bounds_is_flagged(self) -> None:
        quote_a = parse_quote("¥59.90 12×250 mL 加赠 2 瓶", label="A")
        quote_b = parse_quote("¥40.00 12×250 mL", label="B")
        comparison = compare_quotes(
            [quote_a, quote_b],
            target_unit="L",
            gift_size_bounds=(Decimal(0), Decimal(50)),
        )
        assert comparison.certain
        assert comparison.winner == "B"
        # A would need a larger gift than allowed to catch B.
        overstated = [c for c in comparison.changes if c.quote == "A"]
        assert overstated
        assert not any(c.within_bounds for c in overstated)

    def test_break_even_helper(self) -> None:
        quote_a = parse_quote("¥59.90 12×250 mL 加赠 2 瓶", label="A")
        quote_b = parse_quote("¥55.50 12×250 mL", label="B")
        gift_size = quote_a.break_even_gift_size(quote_b, target_unit="L")
        assert gift_size is not None
        assert gift_size == pytest.approx(Decimal("118.9189189189189189189189190"))

    def test_duplicate_labels_rejected(self) -> None:
        with pytest.raises(QuantityError):
            compare_quotes(
                [
                    parse_quote("¥1 1×1 L", label="same"),
                    parse_quote("¥2 1×1 L", label="same"),
                ],
                target_unit="L",
            )


def test_quote_price_is_not_confused_with_pack_numbers() -> None:
    quote = parse_quote("售价 ¥59.90，12×250 mL 加赠 2 瓶")
    assert quote.amount == Decimal("59.90")
    assert quote.pack.count == 12
    assert quote.pack.size == Decimal(250)
