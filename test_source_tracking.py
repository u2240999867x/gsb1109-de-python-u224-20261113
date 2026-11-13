"""Tests for field-source tracking and default reconciliation.

These cover the migration scenario behind the feature: an omitted warehouse
code is filled with a default. When the default changes, historical rows whose
warehouse was written by hand must be preserved, while rows that genuinely
relied on the default need migrating. A whole missing nested object must be
distinguished from an existing object whose inner field was omitted.
"""

from pytest import raises

from schema import (
    MISSING_FIELD,
    MISSING_OBJECT,
    Optional,
    Or,
    ReconciliationResult,
    Schema,
    SchemaError,
    SOURCE_AMBIGUOUS,
    SOURCE_DEFAULT,
    SOURCE_PROVIDED,
    Use,
    ValidationReport,
    reconcile,
)


def warehouse_schema(default_warehouse="WH-OLD"):
    return Schema(
        {
            "contract_no": str,
            Optional("warehouse", default=default_warehouse): str,
            Optional(
                "location", default={"zone": "Z1", "bin": "B1"}
            ): {
                Optional("zone", default="Z1"): str,
                Optional("bin", default="B1"): str,
            },
        }
    )


def test_validate_without_tracking_returns_plain_data():
    schema = warehouse_schema()
    result = schema.validate({"contract_no": "C1"})
    assert isinstance(result, dict)
    assert result["warehouse"] == "WH-OLD"


def test_track_sources_returns_report_with_origins():
    schema = warehouse_schema()
    report = schema.validate(
        {"contract_no": "C1", "warehouse": "WH-HAND", "location": {"zone": "Z9"}},
        track_sources=True,
    )
    assert isinstance(report, ValidationReport)
    assert report.data["warehouse"] == "WH-HAND"
    assert report.sources.get(("contract_no",)) == SOURCE_PROVIDED
    assert report.sources.get(("warehouse",)) == SOURCE_PROVIDED
    assert report.sources.get(("location",)) == SOURCE_PROVIDED
    assert report.sources.get(("location", "zone")) == SOURCE_PROVIDED
    # bin was omitted inside an existing object: filled from its default.
    assert report.sources.get(("location", "bin")) == SOURCE_DEFAULT


def test_dotted_path_and_to_dict_keys():
    report = warehouse_schema().validate({"contract_no": "C1"}, track_sources=True)
    assert report.origins["warehouse"] == SOURCE_DEFAULT
    assert report.sources.get("location.zone") == SOURCE_DEFAULT


def test_missing_whole_object_vs_omitted_inner_field():
    schema = warehouse_schema()

    whole_object_missing = schema.validate(
        {"contract_no": "C1"}, track_sources=True
    )
    assert (
        whole_object_missing.sources.missing_kind(("location",))
        == MISSING_OBJECT
    )
    assert "location" in whole_object_missing.sources.missing_object_fields
    # A scalar default is an omitted field, not a missing object.
    assert (
        whole_object_missing.sources.missing_kind(("warehouse",))
        == MISSING_FIELD
    )

    inner_field_omitted = schema.validate(
        {"contract_no": "C1", "location": {"zone": "Z9"}}, track_sources=True
    )
    assert inner_field_omitted.sources.missing_kind(("location",)) is None
    assert (
        inner_field_omitted.sources.missing_kind(("location", "bin"))
        == MISSING_FIELD
    )
    assert "location.bin" in inner_field_omitted.sources.missing_field_fields


def test_nested_object_default_records_each_inner_field():
    report = warehouse_schema().validate(
        {"contract_no": "C1"}, track_sources=True
    )
    for path in (
        ("location",),
        ("location", "zone"),
        ("location", "bin"),
        ("warehouse",),
    ):
        assert report.sources.get(path) == SOURCE_DEFAULT


def test_list_items_use_indexed_paths():
    schema = Schema(
        [{"id": int, Optional("warehouse", default="WH-OLD"): str}]
    )
    report = schema.validate(
        [{"id": 1, "warehouse": "X"}, {"id": 2}], track_sources=True
    )
    assert report.sources.get((0, "warehouse")) == SOURCE_PROVIDED
    assert report.sources.get((1, "warehouse")) == SOURCE_DEFAULT
    assert report.origins["1.warehouse"] == SOURCE_DEFAULT


def test_use_transform_is_still_provided():
    schema = Schema({"n": Use(int), Optional("w", default="D"): str})
    report = schema.validate({"n": "5"}, track_sources=True)
    assert report.data == {"n": 5, "w": "D"}
    assert report.sources.get(("n",)) == SOURCE_PROVIDED
    assert report.sources.get(("w",)) == SOURCE_DEFAULT


def test_or_branch_with_nested_default_keeps_path():
    schema = Schema(
        {"box": Or({Optional("w", default="D"): str}, None)}
    )
    report = schema.validate({"box": {}}, track_sources=True)
    assert report.data == {"box": {"w": "D"}}
    assert report.sources.get(("box", "w")) == SOURCE_DEFAULT


def test_callable_default_receives_user_kwargs_while_tracked():
    schema = Schema(
        {Optional("k", default=lambda increment=1: 10 + increment): int}
    )
    report = schema.validate({}, track_sources=True, increment=2)
    assert report.data == {"k": 12}
    assert report.sources.get(("k",)) == SOURCE_DEFAULT


def test_track_sources_rejected_on_nested_call():
    schema = Schema({"k": int})
    with raises(ValueError):
        schema.validate(
            {"k": 1}, track_sources=True, _schema_source_ctx=object()
        )


def test_validation_error_still_raised_when_tracking():
    schema = Schema({"n": Use(int)})
    with raises(SchemaError):
        schema.validate({"n": "bad"}, track_sources=True)


def test_collect_defaults_lists_every_default_path():
    defaults = warehouse_schema().collect_defaults()
    assert defaults[("warehouse",)] == "WH-OLD"
    assert defaults[("location",)] == {"zone": "Z1", "bin": "B1"}
    assert defaults[("location", "zone")] == "Z1"
    assert defaults[("location", "bin")] == "B1"


def test_reconcile_with_raw_preserves_manual_warehouse():
    schema = warehouse_schema()
    raw = {
        "contract_no": "C1",
        "warehouse": "WH-OLD",
        "location": {"zone": "Z9"},
    }
    stored = schema.validate(raw)
    result = reconcile(
        stored,
        schema,
        raw=raw,
        new_defaults={("warehouse",): "WH-NEW"},
    )
    assert isinstance(result, ReconciliationResult)
    # The historical row wrote the old warehouse by hand: keep it.
    assert result.data["warehouse"] == "WH-OLD"
    assert result.migrated == []
    assert "warehouse" in result.preserved
    assert result.sources.get(("warehouse",)) == SOURCE_PROVIDED
    # The omitted bin genuinely relied on a default, but has no override here.
    assert result.sources.get(("location", "bin")) == SOURCE_DEFAULT


def test_reconcile_with_raw_migrates_only_default_filled_fields():
    schema = warehouse_schema()
    raw = {"contract_no": "C2"}
    stored = schema.validate(raw)
    result = reconcile(
        stored,
        schema,
        raw=raw,
        new_defaults={
            ("warehouse",): "WH-NEW",
            ("location", "zone"): "Z2",
        },
    )
    assert result.data["warehouse"] == "WH-NEW"
    assert result.data["location"] == {"zone": "Z2", "bin": "B1"}
    assert ("warehouse", "WH-OLD", "WH-NEW") in result.migrated
    assert ("location.zone", "Z1", "Z2") in result.migrated
    # bin had no new default, so it is left at its old value.
    assert result.data["location"]["bin"] == "B1"


def test_reconcile_dotted_string_paths_accepted():
    schema = warehouse_schema()
    raw = {"contract_no": "C2"}
    result = reconcile(
        schema.validate(raw),
        schema,
        raw=raw,
        new_defaults={"warehouse": "WH-NEW"},
    )
    assert result.data["warehouse"] == "WH-NEW"


def test_reconcile_inherits_object_missing_through_inner_fields():
    schema = warehouse_schema()
    raw = {"contract_no": "C3"}
    result = reconcile(
        schema.validate(raw),
        schema,
        raw=raw,
        new_defaults={("location", "zone"): "Z2"},
    )
    # Whole location object was absent: every inner field inherits the
    # object-level missing kind instead of looking like an omitted field.
    assert (
        result.sources.missing_kind(("location", "zone"))
        == MISSING_OBJECT
    )
    assert result.data["location"]["zone"] == "Z2"


def test_reconcile_without_raw_ambiguous_value_left_untouched():
    schema = warehouse_schema()
    stored = schema.validate({"contract_no": "C4"})
    old_defaults = {
        ("warehouse",): "WH-OLD",
        ("location",): {"zone": "Z1", "bin": "B1"},
        ("location", "zone"): "Z1",
        ("location", "bin"): "B1",
    }
    result = reconcile(
        stored,
        schema,
        old_defaults=old_defaults,
        new_defaults={("warehouse",): "WH-NEW"},
    )
    # Both origins explain the stored value: do not touch it.
    assert result.data == stored
    assert result.migrated == []
    assert "warehouse" in result.ambiguous_fields
    assert result.sources.get(("warehouse",)) == SOURCE_AMBIGUOUS
    # The review list names the field and the enclosing defaulted object.
    assert result.review["location.zone"] == ["location.zone", "location"]
    assert result.review["warehouse"] == ["warehouse"]


def test_reconcile_without_raw_preserves_value_different_from_default():
    schema = warehouse_schema()
    stored = {
        "contract_no": "C5",
        "warehouse": "WH-HAND",
        "location": {"zone": "Z1", "bin": "B1"},
    }
    result = reconcile(
        stored, schema, old_defaults={("warehouse",): "WH-OLD"}
    )
    assert result.data == stored
    assert result.ambiguous_fields == []
    assert "warehouse" in result.preserved
    assert result.sources.get(("warehouse",)) == SOURCE_PROVIDED


def test_reconcile_result_as_dict_shape():
    schema = warehouse_schema()
    raw = {"contract_no": "C6"}
    result = reconcile(
        schema.validate(raw),
        schema,
        raw=raw,
        new_defaults={("warehouse",): "WH-NEW"},
    )
    payload = result.as_dict()
    assert set(payload) == {
        "data",
        "origins",
        "migrated",
        "preserved",
        "review",
    }
    assert payload["migrated"] == [
        {"field": "warehouse", "old": "WH-OLD", "new": "WH-NEW"}
    ]
    assert payload["origins"]["warehouse"] == SOURCE_DEFAULT
