from schema import (
    And,
    Or,
    Schema,
    SchemaError,
    Use,
    find_conflicts,
    reconcile_row,
    reconcile_rows,
)


def as_celsius(row):
    if row["unit"] != "C":
        raise ValueError("row is not expressed in degrees Celsius")
    return {"degrees_c": float(row["value"]), "unit": "C"}


def as_fahrenheit(row):
    return {
        "degrees_f": float(row["value"]) * 9.0 / 5.0 + 32.0,
        "unit": "F",
    }


def temperature_rule():
    return Or(Use(as_celsius), Use(as_fahrenheit))


def test_conflicting_cleaning_results_are_all_retained_with_basis():
    row = {"value": 10, "unit": "C"}
    report = reconcile_row(temperature_rule(), row)

    assert report.conflict is True
    assert report.row is row
    assert [outcome.index for outcome in report.outcomes] == [0, 1]
    assert all(outcome.passed for outcome in report.outcomes)
    assert all("candidate #" in outcome.basis for outcome in report.outcomes)

    results = [group.result for group in report.reconciled]
    assert results == [
        {"degrees_c": 10.0, "unit": "C"},
        {"degrees_f": 50.0, "unit": "F"},
    ]
    assert all(len(group.outcomes) == 1 for group in report.reconciled)


def test_row_accepted_by_one_rule_is_not_a_conflict_and_keeps_failure_basis():
    row = {"value": 10, "unit": "F"}
    report = reconcile_row(temperature_rule(), row)

    assert report.conflict is False
    assert len(report.reconciled) == 1
    assert report.reconciled[0].result == {"degrees_f": 50.0, "unit": "F"}

    failed = [outcome for outcome in report.outcomes if not outcome.passed]
    assert len(failed) == 1
    assert failed[0].index == 0
    assert failed[0].error is not None


def test_mapping_key_order_differences_do_not_count_as_conflict():
    rule = Or(
        Use(lambda row: {"celsius": row["value"], "unit": "C"}),
        Use(lambda row: {"unit": "C", "celsius": row["value"]}),
    )
    report = reconcile_row(rule, {"value": 10})

    assert report.conflict is False
    assert len(report.reconciled) == 1
    assert len(report.reconciled[0].outcomes) == 2
    assert report.reconciled[0].result == {"celsius": 10, "unit": "C"}


def test_dict_schemas_differing_only_in_key_order_are_equivalent():
    rule = Or(
        {"reading": {"value": int, "unit": str}},
        {"reading": {"unit": str, "value": int}},
    )
    row = {"reading": {"value": 10, "unit": "C"}}
    report = reconcile_row(rule, row)

    assert report.conflict is False
    assert len(report.reconciled) == 1
    assert len(report.reconciled[0].outcomes) == 2


def test_mutating_candidate_cannot_make_a_later_candidate_fail():
    def stringify_value_in_place(row):
        # Legacy cleaners like this mutate the mapping before returning it.
        row["reading"]["value"] = str(row["reading"]["value"])
        return row

    rule = Or(
        Use(stringify_value_in_place),
        {"reading": {"value": int, "unit": str}},
    )
    original = {"reading": {"value": 10, "unit": "C"}}
    report = reconcile_row(rule, original)

    # Without per-candidate copies the second schema would see "10" and fail.
    assert all(outcome.passed for outcome in report.outcomes)
    assert original == {"reading": {"value": 10, "unit": "C"}}
    assert report.row == original


def test_original_row_can_be_replayed_on_its_own():
    rule = temperature_rule()
    row = {"value": 10, "unit": "C"}
    report = reconcile_row(rule, row)
    replayed = reconcile_row(rule, report.row)

    assert replayed.row == row
    assert [group.result for group in replayed.reconciled] == [
        group.result for group in report.reconciled
    ]
    assert replayed.conflict == report.conflict


def test_reconcile_rows_and_find_conflicts_preserve_order():
    celsius_row = {"value": 10, "unit": "C"}
    fahrenheit_only_row = {"value": 10, "unit": "F"}
    rule = temperature_rule()

    reports = reconcile_rows(rule, [celsius_row, fahrenheit_only_row])
    assert [report.row for report in reports] == [
        celsius_row,
        fahrenheit_only_row,
    ]

    conflicts = find_conflicts(rule, [celsius_row, fahrenheit_only_row])
    assert len(conflicts) == 1
    assert conflicts[0].row is celsius_row


def test_schema_wrapped_or_and_plain_rule_are_supported():
    wrapped = Schema(temperature_rule())
    assert reconcile_row(wrapped, {"value": 10, "unit": "C"}).conflict is True

    single = And(Use(int), lambda value: value > 0)
    report = reconcile_row(single, 5)
    assert report.conflict is False
    assert report.reconciled[0].result == 5


def test_failed_candidates_never_mask_schema_errors_of_the_whole_batch():
    rule = Or(Use(as_celsius), Use(as_fahrenheit))
    report = reconcile_row(rule, {})

    assert report.conflict is False
    assert report.reconciled == []
    assert all(not outcome.passed for outcome in report.outcomes)
    assert all(isinstance(outcome.error, str) for outcome in report.outcomes)
    try:
        rule.validate({})
    except SchemaError:
        pass
    else:
        raise AssertionError("Or validation of a fully rejected row must fail")
