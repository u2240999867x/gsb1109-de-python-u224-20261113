import copy

from pytest import raises

from schema import (
    Contract,
    FieldSpec,
    Relation,
    RepairPlan,
    RepairPlanner,
    RepairSearchLimitError,
)


def _contract():
    """EU<->EUR, and zero tax only inside EUR pricing."""
    return Contract(
        {
            "region": FieldSpec(["EU", "US"], cost=1, source="registry"),
            "currency": FieldSpec(["EUR", "USD"], cost=2, source="registry"),
            "tax": FieldSpec(["standard", "zero"], cost=4, source="finance"),
            "qty": FieldSpec([1, 2, 3], cost=8, source="warehouse"),
        },
        relations=[
            Relation(
                "region-currency",
                ["region", "currency"],
                lambda r: (r["region"] == "EU") == (r["currency"] == "EUR"),
            ),
            Relation(
                "zero-tax-only-eur",
                ["currency", "tax"],
                lambda r: not (r["tax"] == "zero" and r["currency"] != "EUR"),
            ),
        ],
    )


def _record():
    return {"region": "EU", "currency": "USD", "tax": "zero", "qty": 1}


def test_failure_lists_every_violation():
    contract = _contract()
    failures = contract.failures(_record())
    assert [failure.kind for failure in failures] == ["relation", "relation"]
    assert [failure.fields for failure in failures] == [
        ("region", "currency"),
        ("currency", "tax"),
    ]
    assert failures[0].allowed == {
        "region": ("EU", "US"),
        "currency": ("EUR", "USD"),
    }


def test_single_field_repairs_two_relations_cheapest():
    contract = _contract()
    plan = contract.repair(_record())
    assert plan.status == RepairPlan.STATUS_REPAIRABLE
    # region costs 1 but cannot fix the tax relation; currency costs 2.
    assert plan.best_cost == 2
    assert [option.fields for option in plan.options] == [("currency",)]
    assert [option.amendments for option in plan.options] == [{"currency": "EUR"}]
    assert contract.is_valid(plan.options[0].amended_record)


def test_does_not_mutate_the_record():
    contract = _contract()
    record = _record()
    before = copy.deepcopy(record)
    contract.repair(record)
    assert record == before


def test_verified_values_are_fixed():
    contract = _contract()
    plan = contract.repair(_record(), verified=["tax"])
    assert plan.status == RepairPlan.STATUS_REPAIRABLE
    assert [(option.fields, option.amendments) for option in plan.options] == [
        (("currency",), {"currency": "EUR"})
    ]
    for name, value in _record().items():
        if name != "currency":
            assert plan.options[0].amended_record[name] == value


def test_verified_value_that_blocks_every_repair_is_reported():
    contract = _contract()
    record = {"region": "EU", "currency": "USD", "tax": "zero", "qty": 1}
    record["currency"] = "GBP"
    plan = contract.repair(record, verified=["currency"])
    assert plan.status == RepairPlan.STATUS_UNREPAIRABLE
    assert plan.options == ()
    assert plan.blocked_verified == ("currency",)
    assert "currency" in plan.reason


def test_equal_cost_groups_from_different_sources_need_arbitration():
    contract = Contract(
        {
            "a": FieldSpec([1, 2], cost=3, source="left"),
            "b": FieldSpec([1, 2], cost=3, source="right"),
            "c": FieldSpec([1, 2], cost=1),
        },
        relations=[
            Relation("sum-even", ["a", "b"], lambda r: (r["a"] + r["b"]) % 2 == 0)
        ],
    )
    plan = contract.repair({"a": 1, "b": 2, "c": 1})
    assert plan.status == RepairPlan.STATUS_REQUIRES_ARBITRATION
    assert plan.requires_arbitration is True
    assert plan.best_cost == 3
    assert [option.fields for option in plan.options] == [("a",), ("b",)]
    assert [option.sources for option in plan.options] == [("left",), ("right",)]


def test_equal_cost_groups_from_same_source_are_one_option():
    contract = Contract(
        {
            "a": FieldSpec([1, 2], cost=3, source="ledger"),
            "b": FieldSpec([1, 2], cost=3, source="ledger"),
        },
        relations=[
            Relation("sum-even", ["a", "b"], lambda r: (r["a"] + r["b"]) % 2 == 0)
        ],
    )
    plan = contract.repair({"a": 1, "b": 2})
    assert plan.status == RepairPlan.STATUS_REPAIRABLE
    assert plan.requires_arbitration is False
    assert plan.best_cost == 3
    assert [option.fields for option in plan.options] == [("a",), ("b",)]
    assert {option.sources for option in plan.options} == {("ledger",)}


def test_expensive_group_only_when_no_cheaper_group_passes():
    contract = Contract(
        {
            "a": FieldSpec([1, 2], cost=1),
            "b": FieldSpec([1, 2], cost=5),
        },
        relations=[
            Relation("sum", ["a", "b"], lambda r: r["a"] + r["b"] == 3),
            Relation("b-two", ["b"], lambda r: r["b"] == 2),
        ],
    )
    plan = contract.repair({"a": 1, "b": 1})
    assert plan.status == RepairPlan.STATUS_REPAIRABLE
    assert plan.best_cost == 5
    assert [(option.fields, option.amendments) for option in plan.options] == [
        (("b",), {"b": 2})
    ]
    assert contract.is_valid(plan.options[0].amended_record)


def test_group_touches_only_the_fields_that_have_to_move():
    contract = Contract(
        {
            "a": FieldSpec([1, 2, 3], cost=1),
            "b": FieldSpec([1, 2, 3], cost=1),
            "c": FieldSpec(["x", "y"], cost=1),
        },
        relations=[Relation("sorted", ["a", "b"], lambda r: r["a"] < r["b"])],
    )
    plan = contract.repair({"a": 2, "b": 1, "c": "x"})
    assert plan.best_cost == 1
    assert [option.fields for option in plan.options] == [("b",)]
    assert plan.requires_arbitration is False
    assert all(contract.is_valid(o.amended_record) for o in plan.options)


def test_two_field_group_when_one_field_cannot_settle_it():
    contract = Contract(
        {"a": FieldSpec([1, 2], cost=2), "b": FieldSpec([1, 2], cost=3)},
        relations=[Relation("sum", ["a", "b"], lambda r: r["a"] + r["b"] == 4)],
    )
    plan = contract.repair({"a": 1, "b": 1})
    assert plan.best_cost == 5
    assert [(option.fields, option.amendments) for option in plan.options] == [
        (("a", "b"), {"a": 2, "b": 2}),
    ]
    assert plan.requires_arbitration is False


def test_unrepairable_when_all_groups_fail():
    contract = Contract(
        {"a": FieldSpec([1], cost=1), "b": FieldSpec([1], cost=1)},
        relations=[Relation("impossible", ["a", "b"], lambda r: False)],
    )
    plan = contract.repair({"a": 1, "b": 1})
    assert plan.status == RepairPlan.STATUS_UNREPAIRABLE
    assert plan.options == ()
    assert plan.best_cost is None


def test_missing_and_unknown_fields():
    contract = Contract({"a": FieldSpec([1, 2])}, required=["a"])
    missing = contract.repair({})
    assert missing.status == RepairPlan.STATUS_REPAIRABLE
    assert missing.options[0].amendments == {"a": 1}

    unknown = contract.repair({"a": 1, "ghost": 0})
    assert unknown.status == RepairPlan.STATUS_UNREPAIRABLE
    assert "ghost" in unknown.reason


def test_ok_record_needs_no_option():
    contract = Contract({"a": FieldSpec([1, 2])})
    plan = contract.repair({"a": 1})
    assert plan.status == RepairPlan.STATUS_OK
    assert plan.options == ()


def test_failure_leads_carry_fields_allowed_values_cost_and_source():
    contract = _contract()
    plan = contract.repair(_record())
    lead = plan.failure_leads[0]
    assert lead["fields"] == ["region", "currency"]
    assert lead["allowed"] == {
        "region": ["EU", "US"],
        "currency": ["EUR", "USD"],
    }
    assert lead["costs"] == {"region": 1, "currency": 2}
    assert lead["sources"] == {"region": "registry", "currency": "registry"}

    payload = plan.to_dict()
    assert payload["best_cost"] == 2
    assert payload["options"][0]["amendments"] == {"currency": "EUR"}


def test_search_limits_are_enforced():
    contract = Contract({name: FieldSpec([9], cost=1) for name in ("a", "b", "c", "d")})
    planner = RepairPlanner(contract, max_subsets=3)
    with raises(RepairSearchLimitError):
        planner.plan({"a": 0, "b": 0, "c": 0, "d": 0})


def test_relation_referencing_undeclared_field_is_rejected():
    with raises(ValueError):
        Contract(
            {"a": FieldSpec([1])},
            relations=[Relation("bad", ["a", "b"], lambda r: True)],
        )


def test_negative_cost_is_rejected():
    with raises(ValueError):
        FieldSpec([1], cost=-1)


def test_field_spec_rejects_bare_text_as_allowed_values():
    with raises(TypeError):
        FieldSpec("EU")
