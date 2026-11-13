import sqlite3

import pytest

from schema import (
    And,
    ConditionalRequired,
    MutuallyExclusive,
    Optional,
    Or,
    Regex,
    RequiredTogether,
    Schema,
    SchemaError,
    SchemaMissingKeyError,
    SchemaOnlyOneAllowedError,
    SchemaUnexpectedTypeError,
    Use,
)
from schema.sqlite import (
    UnmigratableError,
    create_table_ddl,
    event_to_row,
    row_to_event,
)

SE = pytest.raises(SchemaError)

EVENT_SCHEMA = And(
    {
        "event_type": str,
        "amount": Or(int, float),
        Optional("original_txn_id"): Or(None, str),
    },
    ConditionalRequired("event_type", "refund", "original_txn_id"),
)


def test_conditional_required():
    validate = Schema(EVENT_SCHEMA).validate
    # Normal transactions may omit the key entirely.
    assert validate({"event_type": "sale", "amount": 10}) == {
        "event_type": "sale",
        "amount": 10,
    }
    # Refunds must carry the original transaction id.
    assert validate(
        {"event_type": "refund", "amount": 10, "original_txn_id": "tx-1"}
    ) == {"event_type": "refund", "amount": 10, "original_txn_id": "tx-1"}
    # An explicit null means "still under investigation" and counts as present.
    assert validate({"event_type": "refund", "amount": 10, "original_txn_id": None})[
        "original_txn_id"
    ] is None
    with pytest.raises(SchemaMissingKeyError):
        validate({"event_type": "refund", "amount": 10})


def test_conditional_required_error_message():
    with pytest.raises(SchemaMissingKeyError) as exc_info:
        Schema(EVENT_SCHEMA).validate({"event_type": "refund", "amount": 10})
    assert exc_info.value.args[0] == (
        "Missing key 'original_txn_id': "
        "required when 'event_type' == 'refund'"
    )


def test_conditional_required_constructor():
    with pytest.raises(ValueError):
        ConditionalRequired("event_type", "refund")


def test_mutually_exclusive():
    schema = Schema(
        And(
            {
                "event_type": str,
                Optional("coupon_id"): str,
                Optional("voucher_id"): str,
            },
            MutuallyExclusive("coupon_id", "voucher_id"),
        )
    )
    assert schema.validate({"event_type": "sale", "coupon_id": "c1"})
    assert schema.validate({"event_type": "sale", "voucher_id": "v1"})
    assert schema.validate({"event_type": "sale"})
    with pytest.raises(SchemaOnlyOneAllowedError):
        schema.validate({"event_type": "sale", "coupon_id": "c1", "voucher_id": "v1"})
    with pytest.raises(ValueError):
        MutuallyExclusive("only_one")


def test_required_together():
    schema = Schema(
        And(
            {
                "event_type": str,
                Optional("discount_code"): str,
                Optional("discount_amount"): float,
            },
            RequiredTogether("discount_code", "discount_amount"),
        )
    )
    assert schema.validate({"event_type": "sale"})
    assert schema.validate(
        {"event_type": "sale", "discount_code": "X", "discount_amount": 1.5}
    )
    with SE:
        schema.validate({"event_type": "sale", "discount_code": "X"})
    with SE:
        schema.validate({"event_type": "sale", "discount_amount": 1.5})
    with pytest.raises(ValueError):
        RequiredTogether("only_one")


def test_rules_require_a_mapping():
    with pytest.raises(SchemaUnexpectedTypeError):
        Schema(ConditionalRequired("a", 1, "b")).validate("not-a-dict")
    with pytest.raises(SchemaUnexpectedTypeError):
        Schema(MutuallyExclusive("a", "b")).validate([1, 2])
    with pytest.raises(SchemaUnexpectedTypeError):
        Schema(RequiredTogether("a", "b")).validate(None)


def _make_db(ddl):
    connection = sqlite3.connect(":memory:")
    connection.execute(ddl.rstrip(";"))
    return connection


def _insert(connection, table, row):
    columns = ", ".join('"%s"' % k for k in row)
    placeholders = ", ".join("?" for _ in row)
    connection.execute(
        'INSERT INTO "%s" (%s) VALUES (%s)' % (table, columns, placeholders),
        list(row.values()),
    )


def test_create_table_ddl():
    ddl = create_table_ddl("events", EVENT_SCHEMA)
    assert '"event_type" TEXT NOT NULL' in ddl
    assert '"original_txn_id__present" INTEGER NOT NULL DEFAULT 0' in ddl
    # The conditional-required rule becomes a CHECK on the presence flag.
    assert (
        "\"event_type\" != 'refund' OR (\"original_txn_id__present\" = 1)" in ddl
    )


def test_sqlite_roundtrip_preserves_three_states():
    ddl = create_table_ddl("events", EVENT_SCHEMA)
    connection = _make_db(ddl)
    events = [
        {"event_type": "sale", "amount": 10},  # key absent
        {"event_type": "refund", "amount": 5, "original_txn_id": None},  # null
        {"event_type": "refund", "amount": 5, "original_txn_id": "tx-9"},  # value
    ]
    for event in events:
        _insert(connection, "events", event_to_row(event, EVENT_SCHEMA))
    rows = connection.execute(
        "SELECT * FROM events ORDER BY rowid"
    ).fetchall()
    columns = [d[0] for d in connection.execute("SELECT * FROM events").description]
    read_back = [
        row_to_event(dict(zip(columns, row)), EVENT_SCHEMA) for row in rows
    ]
    # The three business states did not collapse into a single null.
    assert read_back == events
    assert "original_txn_id" not in read_back[0]
    assert read_back[1]["original_txn_id"] is None
    assert read_back[2]["original_txn_id"] == "tx-9"


def test_sqlite_check_enforces_conditional_required():
    ddl = create_table_ddl("events", EVENT_SCHEMA)
    connection = _make_db(ddl)
    # A direct SQL writer inserting a refund without the presence flag set
    # violates the same convention.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            'INSERT INTO events (event_type, amount, original_txn_id,'
            " original_txn_id__present) VALUES ('refund', 5, NULL, 0)"
        )
    # ...while a normal transaction without the key is fine.
    connection.execute(
        'INSERT INTO events (event_type, amount) VALUES (\'sale\', 5)'
    )


def test_sqlite_check_enforces_mutually_exclusive_and_together():
    schema = And(
        {
            "event_type": str,
            Optional("coupon_id"): str,
            Optional("voucher_id"): str,
            Optional("discount_code"): str,
            Optional("discount_amount"): float,
        },
        MutuallyExclusive("coupon_id", "voucher_id"),
        RequiredTogether("discount_code", "discount_amount"),
    )
    connection = _make_db(create_table_ddl("promos", schema))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO promos (event_type, coupon_id, voucher_id)"
            " VALUES ('sale', 'c1', 'v1')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO promos (event_type, discount_code) VALUES ('sale', 'X')"
        )
    connection.execute(
        "INSERT INTO promos (event_type, discount_code, discount_amount)"
        " VALUES ('sale', 'X', 1.5)"
    )


def test_bool_column_roundtrip():
    schema = {"event_type": str, "settled": bool}
    ddl = create_table_ddl("settled_events", schema)
    assert '"settled" INTEGER NOT NULL' in ddl
    connection = _make_db(ddl)
    row = event_to_row({"event_type": "sale", "settled": True}, schema)
    assert row["settled"] == 1
    _insert(connection, "settled_events", row)
    # Direct writers cannot store values other than 0/1.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO settled_events (event_type, settled) VALUES ('sale', 2)"
        )
    raw = connection.execute("SELECT * FROM settled_events").fetchone()
    event = row_to_event({"event_type": raw[0], "settled": raw[1]}, schema)
    assert event == {"event_type": "sale", "settled": True}


def test_optional_default_becomes_sql_default():
    ddl = create_table_ddl(
        "defaults", {"event_type": str, Optional("currency", default="USD"): str}
    )
    assert '"currency" TEXT DEFAULT \'USD\'' in ddl


def test_unmigratable_transformations_are_reported():
    schema = {
        "event_type": str,
        "amount": Use(int),
        "comment": Regex(r"^[a-z]+$"),
        "score": lambda s: s > 0,
        "payload": {"nested": dict},
    }
    with pytest.raises(UnmigratableError) as exc_info:
        create_table_ddl("events", schema)
    reasons = exc_info.value.reasons
    assert any("Use(int)" in r for r in reasons)
    assert any("Regex(" in r for r in reasons)
    assert any("callable predicate" in r for r in reasons)
    assert any("nested" in r for r in reasons)
    # The plain str key is not among the problems.
    assert not any("event_type" in r for r in reasons)


def test_rule_referencing_unknown_key_is_reported():
    with pytest.raises(UnmigratableError) as exc_info:
        create_table_ddl(
            "events",
            {"event_type": str},
            rules=[ConditionalRequired("event_type", "refund", "ghost")],
        )
    assert any("ghost" in r for r in exc_info.value.reasons)


def test_event_to_row_validates_first():
    with SE:
        event_to_row({"event_type": "refund", "amount": 5}, EVENT_SCHEMA)
