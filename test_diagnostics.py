"""Tests for the diagnostic (rejection-slip) validation entry point.

These tests pin down four guarantees:

* a successful diagnostic run yields the exact same value as ``validate``;
* a rejected entry lists every independently confirmable bad field at once;
* transformations consume one-shot inputs only once;
* checks that cannot run after a parent failure are reported as unknown rather
  than turned into fabricated child errors.
"""

from pytest import raises

from schema import (
    FAILED,
    UNKNOWN,
    And,
    Forbidden,
    Or,
    Regex,
    Schema,
    SchemaError,
    SchemaOnlyOneAllowedError,
    Use,
    validate_with_report,
)

SUPPLIER_SCHEMA = Schema(
    {
        "name": And(str, Regex(r"^[A-Za-z0-9 ]+$"), lambda value: len(value) >= 3),
        "code": And(Use(int), lambda number: 1000 <= number <= 9999),
        "contact": {
            "email": Regex(r"^[^@]+@[^@]+\.[^@]+$"),
            "phone": And(str, Regex(r"^\d{3}-\d{4}$")),
        },
        "tags": [And(str, lambda tag: len(tag) <= 5)],
        "tier": Or("bronze", "silver", "gold"),
    }
)

GOOD_SUPPLIER = {
    "name": "Acme Corp",
    "code": "1234",
    "contact": {"email": "a@b.com", "phone": "555-1234"},
    "tags": ["fast", "ok"],
    "tier": "gold",
}


def failure_paths(report):
    return {diagnostic.path for diagnostic in report.failures}


def test_successful_report_matches_plain_validate():
    report = validate_with_report(SUPPLIER_SCHEMA, GOOD_SUPPLIER)
    assert report.valid
    assert report.diagnostics == ()
    # The diagnostic run performs the same transformations once and returns the
    # same value as the regular entry point.
    assert report.value == SUPPLIER_SCHEMA.validate(GOOD_SUPPLIER)
    assert report.value["code"] == 1234


def test_valid_report_keeps_container_types():
    report = validate_with_report(Schema((int, int)), (1, 2))
    assert report.valid
    assert isinstance(report.value, tuple)
    assert report.value == (1, 2)


def test_all_independent_bad_fields_are_listed_at_once():
    bad_supplier = {
        "name": "x",
        "code": "not-a-number",
        "contact": {"email": "nope", "phone": 12345},
        "tags": ["ok", "too-long-tag", 7],
        "tier": "platinum",
    }
    report = validate_with_report(SUPPLIER_SCHEMA, bad_supplier)
    assert not report.valid
    assert failure_paths(report) == {
        ("name",),
        ("code",),
        ("contact", "email"),
        ("contact", "phone"),
        ("tags", 1),
        ("tags", 2),
        ("tier",),
    }


def test_each_failure_points_at_the_actual_check():
    report = validate_with_report(
        SUPPLIER_SCHEMA, {"code": "abc", **_without(GOOD_SUPPLIER, "code")}
    )
    code_failure = next(d for d in report.failures if d.path == ("code",))
    assert code_failure.status == FAILED
    assert "int('abc') raised" in code_failure.check
    assert "int('abc') raised" in code_failure.message


def test_transformation_runs_once_on_success_with_one_shot_input():
    consumed = []

    def consume(value):
        consumed.append(value)
        return int(next(iter(value)))

    schema = Schema({"once": And(Use(consume), int)})
    report = validate_with_report(schema, {"once": iter(["42"])})
    assert report.valid
    assert report.value == {"once": 42}
    assert len(consumed) == 1


def test_transformation_runs_once_on_failure_with_one_shot_input():
    consumed = []

    def consume(value):
        consumed.append(value)
        return int(next(iter(value)))

    schema = Schema({"once": And(Use(consume), int)})
    report = validate_with_report(schema, {"once": iter(["bad"])})
    assert not report.valid
    # Collecting errors must never replay the value through the transformation.
    assert len(consumed) == 1
    assert report.failures[0].path == ("once",)


def test_or_does_not_replay_a_one_shot_transformation():
    calls = []

    def parse(value):
        calls.append(value)
        return int(value)

    report = validate_with_report(Schema({"v": Or(Use(parse), "literal")}), {"v": "xx"})
    assert not report.valid
    assert calls == ["xx"]
    assert report.failures[0].path == ("v",)


def test_failed_and_step_marks_later_checks_unknown():
    schema = Schema(
        {"n": And(Use(int), lambda number: number > 0, lambda number: number < 10)}
    )
    report = validate_with_report(schema, {"n": "abc"})
    failed = [d for d in report.diagnostics if d.status == FAILED]
    unknown = [d for d in report.diagnostics if d.status == UNKNOWN]
    assert len(failed) == 1
    assert failed[0].path == ("n",)
    # The two predicates never ran; they are unknown, not fabricated failures.
    assert len(unknown) == 2
    assert all(d.path == ("n",) for d in unknown)
    assert all("not evaluated" in d.message for d in unknown)


def test_nested_failure_does_not_fabricate_deeper_errors():
    schema = Schema({"outer": {"inner": And(Use(int), lambda number: number > 0)}})
    report = validate_with_report(schema, {"outer": {"inner": "xx"}})
    assert [d.path for d in report.failures] == [("outer", "inner")]
    assert any(
        d.status == UNKNOWN and d.path == ("outer", "inner") for d in report.diagnostics
    )


def test_missing_and_wrong_keys_are_each_reported():
    report = validate_with_report(
        Schema({"a": int, "b": str}), {"a": "x", "c": 1, "d": 2}
    )
    paths = failure_paths(report)
    assert ("a",) in paths  # bad value
    assert ("b",) in paths  # missing
    assert ("c",) in paths  # wrong
    assert ("d",) in paths  # wrong


def test_multiple_missing_keys_are_independent_conclusions():
    report = validate_with_report(Schema({"a": int, "b": int, "c": int}), {})
    assert sorted(d.path for d in report.failures) == [("a",), ("b",), ("c",)]


def test_or_success_leaves_no_diagnostics_and_keeps_value():
    report = validate_with_report(Schema({"v": Or(int, Use(int))}), {"v": "5"})
    assert report.valid
    assert report.value == {"v": 5}
    assert report.diagnostics == ()


def test_nested_or_failure_keeps_precise_field_paths():
    report = validate_with_report(
        Schema(Or({"x": int, "y": int}, str)), {"x": "bad", "y": "also"}
    )
    paths = failure_paths(report)
    assert ("x",) in paths
    assert ("y",) in paths


def test_custom_error_text_is_preserved():
    report = validate_with_report(
        Schema({"code": Use(int, error="code must be numeric")}), {"code": "bad"}
    )
    assert "code must be numeric" in report.failures[0].errors


def test_regex_failure_is_recorded_at_field_path():
    report = validate_with_report(Schema({"phone": Regex(r"^\d+$")}), {"phone": "abc"})
    failure = report.failures[0]
    assert failure.path == ("phone",)
    assert "does not match" in failure.message


def test_forbidden_key_is_recorded():
    report = validate_with_report(
        Schema({Forbidden("secret"): int, "a": int}), {"secret": 1, "a": 2}
    )
    assert not report.valid
    assert ("secret",) in failure_paths(report)


def test_only_one_or_violation_is_collected():
    report = validate_with_report(
        Schema({Or("a", "b", only_one=True): int}), {"a": 1, "b": 2}
    )
    assert not report.valid
    assert any("multiple keys" in d.message for d in report.failures)


def test_fast_fail_behavior_is_unchanged_without_diagnostics():
    # The regular entry point still raises on the first problem.
    with raises(SchemaError):
        Schema({"a": int, "b": int}).validate({"a": "x"})
    with raises(SchemaOnlyOneAllowedError):
        Schema({Or("a", "b", only_one=True): int}).validate({"a": 1, "b": 2})


def test_root_level_failure_uses_empty_path():
    report = validate_with_report(Schema(int), "x")
    assert not report.valid
    assert report.failures[0].path == ()


def test_path_text_renders_keys_and_indexes():
    report = validate_with_report(Schema({"items": [int]}), {"items": ["x"]})
    assert report.failures[0].path_text == "items[0]"
    report = validate_with_report(Schema({"a": {"b": int}}), {"a": {"b": "x"}})
    assert report.failures[0].path_text == "a.b"


def test_ignore_extra_keys_produces_no_wrong_key_diagnostic():
    report = validate_with_report(
        Schema({"a": int}, ignore_extra_keys=True), {"a": 1, "extra": 2}
    )
    assert report.valid
    assert report.value == {"a": 1}


def test_transformed_key_failure_points_at_original_field():
    # The key schema converts the data key; a value failure must still be
    # located via the original key present in the input, not the derived one.
    schema = Schema({Use(str.lower): And(Use(int), lambda number: number > 0)})
    report = validate_with_report(schema, {"COUNT": "bad"})
    assert not report.valid
    assert report.failures[0].path == ("COUNT",)
    # The field must not additionally be denounced as a wrong key.
    assert all(d.path != ("count",) for d in report.failures)


def test_transformed_key_success_value_uses_validated_key():
    schema = Schema({Use(str.upper): int})
    report = validate_with_report(schema, {"name": 7})
    assert report.valid
    assert report.value == {"NAME": 7}


def _without(mapping, key):
    return {current: value for current, value in mapping.items() if current != key}
