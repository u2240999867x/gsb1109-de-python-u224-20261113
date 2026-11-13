from click.testing import CliRunner
from sqlite_diffable import cli
from sqlite_diffable.lineage import Lineage, LineageError, load_version
import json
import pathlib
import pytest


def write_version(directory, columns, rows, table="customers"):
    pathlib.Path(str(directory)).mkdir(parents=True, exist_ok=True)
    metadata = directory / (table + ".metadata.json")
    ndjson = directory / (table + ".ndjson")
    metadata.write_text(
        json.dumps({"name": table, "columns": columns, "schema": "schema"}),
        "utf-8",
    )
    ndjson.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), "utf-8"
    )
    return str(directory)


@pytest.fixture
def three_versions(tmpdir):
    # v1: "code" is the customer number (客户编号), position 1
    v1 = write_version(
        tmpdir.mkdir("v2021"),
        ["id", "code", "name"],
        [[1, "C001", "Acme"], [2, "C002", "Globex"]],
    )
    # v2: customer number renamed customer_no; a NEW empty "code" is added
    v2 = write_version(
        tmpdir.mkdir("v2022"),
        ["id", "customer_no", "name", "code"],
        [[1, "C001", "Acme", None], [2, "C002", "Globex", None]],
    )
    # v3: name is split; the new code is now populated with a different meaning
    v3 = write_version(
        tmpdir.mkdir("v2023"),
        ["id", "customer_no", "first_name", "last_name", "code"],
        [
            [1, "C001", "Ac", "me", "R1"],
            [2, "C002", "Glo", "bex", "R2"],
        ],
    )
    changes = [
        {
            "type": "rename",
            "effective_version": v2,
            "from": "code",
            "to": "customer_no",
        },
        {
            "type": "split",
            "effective_version": v3,
            "from": "name",
            "into": ["first_name", "last_name"],
        },
    ]
    return v1, v2, v3, changes


def build(three_versions):
    v1, v2, v3, changes = three_versions
    return Lineage.from_paths([v1, v2, v3], changes)


def test_load_version_reads_positional_archive(three_versions):
    v1, _, _, _ = three_versions
    version = load_version(v1)
    assert version.columns == ["id", "code", "name"]
    assert version.rows() == [[1, "C001", "Acme"], [2, "C002", "Globex"]]
    assert version.directory.endswith("v2021")


def test_row_width_mismatch_is_rejected(tmpdir):
    path = write_version(
        tmpdir.mkdir("bad"), ["id", "code"], [[1, "x"], [2, "y", "z"]]
    )
    lineage = Lineage.from_paths([path])
    with pytest.raises(LineageError):
        lineage.reconstruct()


def test_duplicate_column_names_are_rejected(tmpdir):
    path = write_version(tmpdir.mkdir("dup"), ["code", "code"], [])
    with pytest.raises(LineageError):
        load_version(path)


def test_unified_columns_follow_first_appearance(three_versions):
    lineage = build(three_versions)
    assert lineage.unified_columns == [
        "id",
        "customer_no",
        "name",
        "code",
        "first_name",
        "last_name",
    ]


def test_effective_columns_keep_physical_positions(three_versions):
    lineage = build(three_versions)
    v1, v2, v3, _ = three_versions
    assert lineage.effective_columns(v1) == [
        "id",
        "customer_no",
        "name",
    ]
    assert lineage.effective_columns(v2) == [
        "id",
        "customer_no",
        "name",
        "code",
    ]
    assert lineage.effective_columns(v3) == [
        "id",
        "customer_no",
        "first_name",
        "last_name",
        "code",
    ]


def test_reconstruct_preserves_order_and_uses_null_without_guessing(three_versions):
    lineage = build(three_versions)
    rows = lineage.reconstruct()
    assert [row["_version"] for row in rows] == [
        "v2021",
        "v2021",
        "v2022",
        "v2022",
        "v2023",
        "v2023",
    ]
    # v1 rows: the re-added region "code" and split children did not exist yet
    assert rows[0] == {
        "_version": "v2021",
        "_source_directory": rows[0]["_source_directory"],
        "id": 1,
        "customer_no": "C001",
        "name": "Acme",
        "code": None,
        "first_name": None,
        "last_name": None,
    }
    assert rows[0]["_source_directory"].endswith("v2021")
    # v2: the empty re-added code stays null and is never merged by content
    assert rows[2]["code"] is None
    assert rows[2]["customer_no"] == "C001"
    assert rows[2]["_source_directory"].endswith("v2022")
    # v3: original name column was split away; region code now carries values
    assert rows[4] == {
        "_version": "v2023",
        "_source_directory": rows[4]["_source_directory"],
        "id": 1,
        "customer_no": "C001",
        "name": None,
        "code": "R1",
        "first_name": "Ac",
        "last_name": "me",
    }
    assert rows[4]["_source_directory"].endswith("v2023")


def test_reconstruct_single_version_and_without_provenance(three_versions):
    lineage = build(three_versions)
    _, v2, _, _ = three_versions
    rows = lineage.reconstruct(version=v2, provenance=False)
    assert len(rows) == 2
    assert set(rows[0]) == set(lineage.unified_columns)
    assert rows[0]["code"] is None


def test_reused_name_code_resolves_to_two_distinct_fields(three_versions):
    lineage = build(three_versions)
    history = lineage.history("code")
    assert len(history) == 2
    customer_number, region_code = history
    # The original customer-number field carries the rename and spans all versions
    assert customer_number["canonical"] == "customer_no"
    assert [p["index"] for p in customer_number["positions"]] == [1, 1, 1]
    assert [p["directory"].split("/")[-1] for p in customer_number["positions"]] == [
        "v2021",
        "v2022",
        "v2023",
    ]
    assert customer_number["events"][0]["type"] == "rename"
    assert customer_number["events"][0]["from"] == "code"
    assert customer_number["events"][0]["to"] == "customer_no"
    note = customer_number["events"][0]["change"]
    assert not any(key.startswith("_") for key in note)
    # The re-added region code starts in v2 at position 3, unrelated to the first
    assert region_code["canonical"] == "code"
    assert [p["index"] for p in region_code["positions"]] == [3, 4]
    assert [p["directory"].split("/")[-1] for p in region_code["positions"]] == [
        "v2022",
        "v2023",
    ]
    assert region_code["events"] == []
    assert region_code["derived_from"] is None


def test_split_children_record_their_parent(three_versions):
    lineage = build(three_versions)
    first_name = next(
        field for field in lineage.fields if field.canonical == "first_name"
    )
    assert first_name.parent is not None
    assert first_name.parent.canonical == "name"
    describe = lineage.describe()
    split_events = [
        event
        for field in describe["fields"]
        for event in field["events"]
        if event["type"] == "split"
    ]
    assert len(split_events) == 1
    assert split_events[0]["into"] == ["first_name", "last_name"]


def test_without_rename_evidence_the_reused_name_is_followed_by_name(three_versions):
    # Documents why evidence is required: no changes -> name-based continuation
    # wrongly treats the new empty "code" as the same field as the old one.
    v1, v2, v3, _ = three_versions
    lineage = Lineage.from_paths([v1, v2, v3], [])
    assert len(lineage.history("code")) == 1
    assert "customer_no" in lineage.unified_columns


def test_invalid_change_types_are_rejected(three_versions):
    v1, v2, v3, _ = three_versions
    with pytest.raises(LineageError):
        Lineage.from_paths(
            [v1, v2, v3],
            [{"type": "bogus", "effective_version": v2, "from": "a", "to": "b"}],
        )
    with pytest.raises(LineageError):
        Lineage.from_paths(
            [v1, v2, v3],
            [
                {
                    "type": "rename",
                    "effective_version": v2,
                    "from": "missing",
                    "to": "customer_no",
                }
            ],
        )
    with pytest.raises(LineageError):
        Lineage.from_paths(
            [v1, v2, v3],
            [
                {
                    "type": "split",
                    "effective_version": v3,
                    "from": "name",
                    "into": ["only_one"],
                }
            ],
        )


def test_cli_describe_reconstruct_history(three_versions, tmpdir):
    v1, v2, v3, changes = three_versions
    changes_path = tmpdir / "changes.json"
    changes_path.write_text(json.dumps(changes), "utf-8")
    runner = CliRunner()

    result = runner.invoke(
        cli.cli,
        ["lineage", "describe", v1, v2, v3, "-c", str(changes_path)],
    )
    assert result.exit_code == 0, result.output
    describe = json.loads(result.output)
    assert len(describe["versions"]) == 3
    assert len(describe["fields"]) == 6

    out_file = tmpdir / "unified.json"
    result = runner.invoke(
        cli.cli,
        [
            "lineage",
            "reconstruct",
            v1,
            v2,
            v3,
            "-c",
            str(changes_path),
            "-o",
            str(out_file),
        ],
    )
    assert result.exit_code == 0, result.output
    unified = json.loads(out_file.read_text("utf-8"))
    assert len(unified) == 6
    assert unified[0]["customer_no"] == "C001"

    result = runner.invoke(
        cli.cli,
        [
            "lineage",
            "history",
            v1,
            v2,
            v3,
            "-c",
            str(changes_path),
            "--name",
            "code",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)) == 2


def test_cli_rejects_bad_changes(three_versions, tmpdir):
    v1, v2, v3, _ = three_versions
    changes_path = tmpdir / "bad.json"
    changes_path.write_text(
        json.dumps(
            [
                {
                    "type": "rename",
                    "effective_version": v2,
                    "from": "ghost",
                    "to": "customer_no",
                }
            ]
        ),
        "utf-8",
    )
    result = CliRunner().invoke(
        cli.cli,
        ["lineage", "describe", v1, v2, v3, "-c", str(changes_path)],
    )
    assert result.exit_code == 1
    assert "ghost" in result.output
