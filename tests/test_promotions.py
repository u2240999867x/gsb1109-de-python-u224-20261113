import itertools
from decimal import Decimal

import pytest

from price_parser.promotions import (
    Allocation,
    Item,
    Promotion,
    StackingGroup,
    ThresholdBasis,
    best_settlement,
    evaluate_plan,
)

D = Decimal


def basket():
    return [
        Item("A", "books", "100.00"),
        Item("B", "electronics", "200.00"),
        Item("C", "books", "50.00"),
    ]


def promotions():
    return [
        Promotion.member_discount("member", "0.9"),
        Promotion.threshold_coupon("coupon", threshold="300", amount_off="40"),
        Promotion.category_coupon("cat", ["books"], amount_off="30"),
    ]


def test_stacking_group_applies_in_declared_trigger_order():
    settlement = best_settlement(
        basket(), promotions(), [StackingGroup(("member", "coupon", "cat"))]
    )
    assert settlement.applied_promotions == ("member", "coupon", "cat")
    assert settlement.total_original == D("350.00")
    assert settlement.total_payable == D("245.00")
    assert settlement.total_discount == D("105.00")
    payable = {line.item_id: line.payable for line in settlement.lines}
    assert payable == {"A": D("58.57"), "B": D("157.14"), "C": D("29.29")}


def test_every_line_traces_its_allocated_discounts():
    settlement = best_settlement(
        basket(), promotions(), [StackingGroup(("member", "coupon", "cat"))]
    )
    assert settlement.allocation_by_item("member") == {
        "A": D("10.00"),
        "B": D("20.00"),
        "C": D("5.00"),
    }
    assert settlement.allocation_by_item("coupon") == {
        "A": D("11.43"),
        "B": D("22.86"),
        "C": D("5.71"),
    }
    # the category coupon only touches books
    assert settlement.allocation_by_item("cat") == {"A": D("20.00"), "C": D("10.00")}
    for line in settlement.lines:
        allocated = sum((a.amount for a in line.allocations), D("0.00"))
        assert allocated == line.discount
    assert sum(line.payable for line in settlement.lines) == settlement.total_payable


def test_threshold_on_after_previous_amount_can_block_a_coupon():
    items = [Item("A", "misc", "340.00")]
    promos = [
        Promotion.member_discount("member", "0.9"),
        Promotion.threshold_coupon("coupon", threshold="310", amount_off="50"),
    ]
    group = [StackingGroup(("member", "coupon"))]
    settlement = best_settlement(items, promos, group)
    # member first drops the basket to 306 < 310, so the coupon cannot fire;
    # the optimizer rather skips the member discount and takes the coupon
    assert settlement.applied_promotions == ("coupon",)
    assert settlement.total_payable == D("290.00")


def test_threshold_on_original_price_still_fires_after_discounts():
    items = [Item("A", "misc", "340.00")]
    promos = [
        Promotion.member_discount("member", "0.9"),
        Promotion.threshold_coupon(
            "coupon",
            threshold="310",
            amount_off="50",
            basis=ThresholdBasis.ORIGINAL,
        ),
    ]
    group = [StackingGroup(("member", "coupon"))]
    settlement = best_settlement(items, promos, group)
    # the threshold looks at the original 340, so both promotions stack
    assert settlement.applied_promotions == ("member", "coupon")
    assert settlement.total_payable == D("256.00")
    (line,) = settlement.lines
    assert line.allocations == (
        Allocation("member", D("34.00")),
        Allocation("coupon", D("50.00")),
    )


def test_undeclared_combinations_are_never_used():
    items = [Item("A", "misc", "350.00")]
    promos = [
        Promotion.member_discount("member", "0.9"),
        Promotion.threshold_coupon("coupon", threshold="300", amount_off="40"),
    ]
    settlement = best_settlement(items, promos)  # no stacking group declared
    # stacking the two would give 275, but it was never declared compatible
    assert settlement.applied_promotions == ("coupon",)
    assert settlement.total_payable == D("310.00")


def test_display_order_does_not_change_the_optimum():
    groups = [
        StackingGroup(("member", "coupon", "cat")),
        StackingGroup(("coupon", "cat")),
    ]
    reference = best_settlement(basket(), promotions(), groups)
    for promo_perm in itertools.permutations(promotions()):
        for group_perm in itertools.permutations(groups):
            settlement = best_settlement(basket(), list(promo_perm), list(group_perm))
            assert settlement == reference
    for item_perm in itertools.permutations(basket()):
        settlement = best_settlement(list(item_perm), promotions(), groups)
        assert settlement.total_payable == reference.total_payable
        by_id = {line.item_id: line for line in settlement.lines}
        assert by_id == {line.item_id: line for line in reference.lines}


def test_threshold_not_met_means_not_applied():
    items = [Item("A", "misc", "350.00")]
    promos = [Promotion.threshold_coupon("coupon", threshold="400", amount_off="40")]
    settlement = best_settlement(items, promos)
    assert settlement.applied_promotions == ()
    assert settlement.total_payable == D("350.00")


def test_amount_off_is_capped_at_zero_payable():
    items = [Item("A", "misc", "200.00"), Item("B", "misc", "150.00")]
    promos = [Promotion.threshold_coupon("coupon", threshold="100", amount_off="500")]
    settlement = best_settlement(items, promos)
    assert settlement.total_payable == D("0.00")
    assert settlement.total_discount == D("350.00")


def test_rate_rounds_half_up_per_line():
    items = [Item("A", "misc", "33.33")]
    promos = [Promotion.member_discount("member", "0.9")]
    settlement = best_settlement(items, promos)
    (line,) = settlement.lines
    assert line.payable == D("30.00")
    assert line.discount == D("3.33")


def test_quantity_scales_the_line_total():
    items = [Item("Q", "books", "25.00", quantity=4)]
    promos = [Promotion.category_coupon("cat", ["books"], rate="0.5")]
    settlement = best_settlement(items, promos)
    (line,) = settlement.lines
    assert line.original == D("100.00")
    assert line.payable == D("50.00")


def test_evaluate_plan_applies_exactly_the_given_order():
    items = [Item("A", "misc", "340.00")]
    promos = [
        Promotion.member_discount("member", "0.9"),
        Promotion.threshold_coupon("coupon", threshold="310", amount_off="50"),
    ]
    settlement = evaluate_plan(items, promos, ("member", "coupon"))
    assert settlement.applied_promotions == ("member",)
    assert settlement.total_payable == D("306.00")


def test_explain_renders_a_traceable_breakdown():
    settlement = best_settlement(
        basket(), promotions(), [StackingGroup(("member", "coupon", "cat"))]
    )
    text = settlement.explain()
    assert "member -> coupon -> cat" in text
    assert "245.00" in text
    for item_id in ("A", "B", "C"):
        assert item_id in text


def test_duplicate_promotion_ids_are_rejected():
    promos = [
        Promotion.member_discount("member", "0.9"),
        Promotion.member_discount("member", "0.8"),
    ]
    with pytest.raises(ValueError, match="duplicate promotion id"):
        best_settlement([Item("A", "misc", "10.00")], promos)


def test_stacking_group_must_reference_known_promotions():
    with pytest.raises(ValueError, match="unknown promotions"):
        best_settlement(
            [Item("A", "misc", "10.00")],
            [Promotion.member_discount("member", "0.9")],
            [StackingGroup(("member", "ghost"))],
        )


def test_promotion_requires_exactly_one_discount_mechanism():
    with pytest.raises(ValueError, match="exactly one of rate or amount_off"):
        Promotion("p", "member_discount")
    with pytest.raises(ValueError, match="exactly one of rate or amount_off"):
        Promotion("p", "member_discount", rate="0.9", amount_off="5")


def test_kind_specific_validation():
    with pytest.raises(ValueError, match="whole basket"):
        Promotion("m", "member_discount", rate="0.9", categories=["books"])
    with pytest.raises(ValueError, match="amount_off and threshold"):
        Promotion("t", "threshold_coupon", amount_off="10")
    with pytest.raises(ValueError, match="at least one category"):
        Promotion("c", "category_coupon", amount_off="10")


def test_empty_basket_is_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        best_settlement([], promotions())
