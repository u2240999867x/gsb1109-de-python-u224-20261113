import json
import pathlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from string import Template


def _paths(directory, table):
    directory = pathlib.Path(directory)
    return (
        directory / "{}.metadata.json".format(table),
        directory / "{}.ndjson".format(table),
    )


def load_export(directory, table):
    """Read one diffable export.

    Returns (metadata, rows) where each row is a tuple of
    (line_number, row_dict, raw_values_list, raw_line_text).
    """
    meta_path, data_path = _paths(directory, table)
    info = json.loads(meta_path.read_text(encoding="utf-8"))
    columns = info["columns"]
    rows = []
    with data_path.open(encoding="utf-8") as fp:
        for line_number, line in enumerate(fp, start=1):
            if not line.strip():
                continue
            values = json.loads(line)
            rows.append(
                (line_number, dict(zip(columns, values)), values, line.rstrip("\n"))
            )
    return info, rows


def _freeze(values):
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _group_key(row, join_keys):
    return json.dumps(
        [row.get(key) for key in join_keys],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_columns(
    old_columns, master_columns, preference_columns, join_keys
):
    for key in join_keys:
        if key not in master_columns:
            raise ValueError(
                "join key {!r} is missing from master table columns".format(key)
            )
        if key not in preference_columns:
            raise ValueError(
                "join key {!r} is missing from preference table columns".format(key)
            )
        if key not in old_columns:
            raise ValueError(
                "join key {!r} is missing from original table columns".format(key)
            )
    uncovered = [c for c in old_columns if c not in master_columns + preference_columns]
    if uncovered:
        raise ValueError(
            "original columns cannot be sourced from the new exports: {}".format(
                ", ".join(uncovered)
            )
        )
    master_only = set(master_columns) - set(join_keys)
    preference_only = set(preference_columns) - set(join_keys)
    ambiguous = sorted(master_only & preference_only)
    if ambiguous:
        raise ValueError(
            "non-key columns present on both sides, cannot decide which value "
            "to keep: {}".format(", ".join(ambiguous))
        )
    unknown = (set(master_columns) | set(preference_columns)) - set(old_columns)
    if unknown:
        raise ValueError(
            "new exports contain columns absent from the original table: "
            "{}".format(", ".join(sorted(unknown)))
        )


@dataclass
class VerifyReport:
    old_directory: str
    new_directory: str
    original_table: str
    master_table: str
    preference_table: str
    join_keys: list
    columns: list
    old_count: int
    master_count: int
    preference_count: int
    reconstructed_count: int
    order_preserved: bool
    missing: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    orphan_preferences: list = field(default_factory=list)
    witnesses: list = field(default_factory=list)

    @property
    def status(self):
        if self.missing or self.extra or self.orphan_preferences:
            return "mismatch"
        if not self.order_preserved:
            return "reordered"
        return "ok"

    def to_dict(self):
        return {
            "status": self.status,
            "order_preserved": self.order_preserved,
            "old_directory": self.old_directory,
            "new_directory": self.new_directory,
            "original_table": self.original_table,
            "master_table": self.master_table,
            "preference_table": self.preference_table,
            "join_keys": self.join_keys,
            "columns": self.columns,
            "counts": {
                "original": self.old_count,
                "master": self.master_count,
                "preference": self.preference_count,
                "reconnected": self.reconstructed_count,
                "orphan_preferences": len(self.orphan_preferences),
            },
            "missing": [
                {"row": dict(zip(self.columns, frozen_values)), "copies": copies}
                for frozen_values, copies in self.missing
            ],
            "extra": [
                {"row": dict(zip(self.columns, frozen_values)), "copies": copies}
                for frozen_values, copies in self.extra
            ],
            "orphan_preferences": self.orphan_preferences,
            "witnesses": self.witnesses,
        }


def _evidence(numbered_rows, join_keys, group_key, limit=3):
    matches = [
        {"line": line_number, "json": raw_line}
        for line_number, row, _, raw_line in numbered_rows
        if _group_key(row, join_keys) == group_key
    ]
    return matches[:limit], len(matches)


_REPRO_TEMPLATE = Template(
    """python3 - <<'PY'
import json

def load(directory, table):
    metadata = json.load(open(f"{directory}/{table}.metadata.json", encoding="utf-8"))
    columns = metadata["columns"]
    with open(f"{directory}/{table}.ndjson", encoding="utf-8") as fp:
        return [dict(zip(columns, json.loads(line))) for line in fp if line.strip()]

def match(rows):
    return [row for row in rows if all(row.get(k) == v for k, v in key.items())]

key = $KEY
old_rows = match(load($OLD_DIR, $ORIGINAL))
master_rows = match(load($NEW_DIR, $MASTER))
preference_rows = match(load($NEW_DIR, $PREF))
reconnected = []
for master in master_rows:
    for preference in preference_rows or [None]:
        merged = dict(master)
        if preference is not None:
            merged.update(preference)
        reconnected.append(merged)
target = $TARGET
print("见证记录:", json.dumps(target, ensure_ascii=False))
print("旧导出同键行数:", len(old_rows))
print("主档同键行数:", len(master_rows))
print("偏好同键行数:", len(preference_rows))
print("旧导出中该完整记录份数:", sum(1 for row in old_rows if row == target))
print("重连生成该完整记录份数:", sum(1 for row in reconnected if row == target))
print("重连生成的全部组合:")
for row in reconnected:
    print(" ", json.dumps(row, ensure_ascii=False))
PY"""
)


def _repro_script(
    old_dir,
    new_dir,
    original_table,
    master_table,
    preference_table,
    key_dict,
    target_dict,
):
    return _REPRO_TEMPLATE.substitute(
        KEY=json.dumps(key_dict, ensure_ascii=False),
        TARGET=json.dumps(target_dict, ensure_ascii=False),
        OLD_DIR=json.dumps(str(old_dir)),
        NEW_DIR=json.dumps(str(new_dir)),
        ORIGINAL=json.dumps(original_table),
        MASTER=json.dumps(master_table),
        PREF=json.dumps(preference_table),
    )


def verify_restore(
    old_dir,
    new_dir,
    original_table,
    master_table,
    preference_table,
    join_keys,
    witness_limit=3,
):
    """Reconnect the split exports and audit them against the old export.

    Rows are compared as multisets, so duplicate join keys and duplicate
    full rows are counted instead of being collapsed away.
    """
    old_info, old_rows = load_export(old_dir, original_table)
    master_info, master_rows = load_export(new_dir, master_table)
    pref_info, pref_rows = load_export(new_dir, preference_table)

    old_columns = old_info["columns"]
    master_columns = master_info["columns"]
    pref_columns = pref_info["columns"]
    _validate_columns(old_columns, master_columns, pref_columns, join_keys)

    old_counter = Counter()
    old_tuples = {}
    old_lines = defaultdict(list)
    old_sequence = []
    for line_number, row, _, _ in old_rows:
        values = tuple(row.get(column) for column in old_columns)
        frozen = _freeze(values)
        old_counter[frozen] += 1
        old_tuples.setdefault(frozen, values)
        old_lines[frozen].append(line_number)
        old_sequence.append(frozen)

    pref_groups = defaultdict(list)
    pref_group_keys = set()
    for line_number, row, _, _ in pref_rows:
        group_key = _group_key(row, join_keys)
        pref_groups[group_key].append((line_number, row))
        pref_group_keys.add(group_key)

    master_group_keys = set()
    reconstructed = Counter()
    reconstructed_tuples = {}
    provenance = defaultdict(list)
    reconstructed_sequence = []
    for master_line, master_row, _, _ in master_rows:
        group_key = _group_key(master_row, join_keys)
        master_group_keys.add(group_key)
        partners = pref_groups.get(group_key) or [(None, None)]
        for pref_line, pref_row in partners:
            merged = dict(master_row)
            if pref_row is not None:
                merged.update(pref_row)
            values = tuple(merged.get(column) for column in old_columns)
            frozen = _freeze(values)
            reconstructed[frozen] += 1
            reconstructed_tuples.setdefault(frozen, values)
            provenance[frozen].append((master_line, pref_line))
            reconstructed_sequence.append(frozen)

    orphan_preferences = []
    master_raw_by_line = {line_number: raw for line_number, _, _, raw in master_rows}
    pref_raw_by_line = {line_number: raw for line_number, _, _, raw in pref_rows}
    pref_meta_name = preference_table
    for line_number, row, _, raw_line in pref_rows:
        group_key = _group_key(row, join_keys)
        if group_key not in master_group_keys:
            orphan_preferences.append(
                {
                    "table": pref_meta_name,
                    "line": line_number,
                    "json": raw_line,
                    "join_key": {key: row.get(key) for key in join_keys},
                }
            )

    missing_counter = old_counter - reconstructed
    extra_counter = reconstructed - old_counter

    missing = [
        (old_tuples[frozen], copies) for frozen, copies in missing_counter.items()
    ]
    extra = [
        (reconstructed_tuples[frozen], copies)
        for frozen, copies in extra_counter.items()
    ]

    witnesses = []
    for values, copies in missing[:witness_limit]:
        frozen = _freeze(values)
        target = dict(zip(old_columns, values))
        key_dict = {key: target[key] for key in join_keys}
        group_key = _group_key(target, join_keys)
        master_evidence, master_total = _evidence(
            master_rows, join_keys, group_key
        )
        pref_evidence, pref_total = _evidence(pref_rows, join_keys, group_key)
        witnesses.append(
            {
                "kind": "missing",
                "join_key": key_dict,
                "row": target,
                "copies": copies,
                "evidence": {
                    "original": [
                        {
                            "file": "/{}.ndjson".format(original_table),
                            "line": line_number,
                            "json": _raw_line(old_rows, line_number),
                        }
                        for line_number in old_lines[frozen][:witness_limit]
                    ],
                    "master": master_evidence,
                    "master_total_matches": master_total,
                    "preference": pref_evidence,
                    "preference_total_matches": pref_total,
                },
                "repro": _repro_script(
                    old_dir,
                    new_dir,
                    original_table,
                    master_table,
                    preference_table,
                    key_dict,
                    target,
                ),
            }
        )

    for values, copies in extra[:witness_limit]:
        frozen = _freeze(values)
        target = dict(zip(old_columns, values))
        key_dict = {key: target[key] for key in join_keys}
        group_key = _group_key(target, join_keys)
        master_line, pref_line = provenance[frozen][0]
        master_evidence = [
            {"line": master_line, "json": master_raw_by_line[master_line]}
        ]
        if pref_line is None:
            pref_evidence = []
        else:
            pref_evidence = [
                {"line": pref_line, "json": pref_raw_by_line[pref_line]}
            ]
        old_evidence, old_total = _evidence(old_rows, join_keys, group_key)
        witnesses.append(
            {
                "kind": "extra",
                "join_key": key_dict,
                "row": target,
                "copies": copies,
                "evidence": {
                    "original_same_key": old_evidence,
                    "original_total_matches": old_total,
                    "master": master_evidence,
                    "preference": pref_evidence,
                },
                "repro": _repro_script(
                    old_dir,
                    new_dir,
                    original_table,
                    master_table,
                    preference_table,
                    key_dict,
                    target,
                ),
            }
        )

    return VerifyReport(
        old_directory=str(old_dir),
        new_directory=str(new_dir),
        original_table=original_table,
        master_table=master_table,
        preference_table=preference_table,
        join_keys=list(join_keys),
        columns=old_columns,
        old_count=len(old_rows),
        master_count=len(master_rows),
        preference_count=len(pref_rows),
        reconstructed_count=sum(reconstructed.values()),
        order_preserved=old_sequence == reconstructed_sequence,
        missing=missing,
        extra=extra,
        orphan_preferences=orphan_preferences,
        witnesses=witnesses,
    )


def _raw_line(numbered_rows, line_number):
    for current, _, _, raw_line in numbered_rows:
        if current == line_number:
            return raw_line
    raise KeyError(line_number)


def render_text(report):
    lines = []
    lines.append("== 还原核查报告 ==")
    lines.append(
        "旧导出: {}  原表: {}".format(
            report.old_directory, report.original_table
        )
    )
    lines.append(
        "新导出: {}  主档: {}  偏好: {}".format(
            report.new_directory, report.master_table, report.preference_table
        )
    )
    lines.append("关联键: {}".format(", ".join(report.join_keys)))
    lines.append("")
    lines.append("计数")
    lines.append("  原表行数: {}".format(report.old_count))
    lines.append(
        "  主档行数: {}  偏好行数: {}".format(
            report.master_count, report.preference_count
        )
    )
    lines.append("  重连后行数: {}".format(report.reconstructed_count))
    lines.append(
        "  悬空偏好行(在主档中找不到关联键): {}".format(
            len(report.orphan_preferences)
        )
    )
    lines.append("")

    missing_copies = sum(copies for _, copies in report.missing)
    extra_copies = sum(copies for _, copies in report.extra)
    if report.status == "ok":
        lines.append("结论: OK —— 行数、多重集与行序完全一致")
    elif report.status == "reordered":
        lines.append(
            "结论: 仅行序变化 —— 记录多重集完全一致，没有丢失或凭空多出的组合"
        )
    else:
        lines.append(
            "结论: 不一致 —— 丢失 {} 种共 {} 条；凭空多出 {} 种共 {} 条".format(
                len(report.missing),
                missing_copies,
                len(report.extra),
                extra_copies,
            )
        )
    lines.append("")

    for orphan in report.orphan_preferences:
        lines.append(
            "悬空偏好行 {}.ndjson 第 {} 行, 关联键 {}:".format(
                orphan["table"], orphan["line"], json.dumps(orphan["join_key"])
            )
        )
        lines.append("  {}".format(orphan["json"]))
    if report.orphan_preferences:
        lines.append("")

    for index, witness in enumerate(report.witnesses, start=1):
        if witness["kind"] == "missing":
            title = "丢失 MISSING"
        else:
            title = "凭空多出 EXTRA"
        lines.append(
            "见证 {} [{}] 关联键 {}".format(
                index, title, json.dumps(witness["join_key"], ensure_ascii=False)
            )
        )
        lines.append(
            "  记录(缺失 {} 份): {}".format(
                witness["copies"],
                json.dumps(witness["row"], ensure_ascii=False),
            )
        )
        evidence = witness["evidence"]
        if witness["kind"] == "missing":
            for item in evidence["original"]:
                lines.append(
                    "  旧导出 {}.ndjson 第 {} 行:".format(
                        report.original_table, item["line"]
                    )
                )
                lines.append("    {}".format(item["json"]))
            lines.append(
                "  新导出主档同键行数: {}".format(evidence["master_total_matches"])
            )
            for item in evidence["master"]:
                lines.append(
                    "    {}.ndjson 第 {} 行: {}".format(
                        report.master_table, item["line"], item["json"]
                    )
                )
            lines.append(
                "  新导出偏好同键行数: {}".format(
                    evidence["preference_total_matches"]
                )
            )
            if not evidence["preference"]:
                lines.append("    （偏好导出中没有该关联键的任何行）")
            for item in evidence["preference"]:
                lines.append(
                    "    {}.ndjson 第 {} 行: {}".format(
                        report.preference_table, item["line"], item["json"]
                    )
                )
        else:
            for item in evidence["master"]:
                lines.append(
                    "  主档 {}.ndjson 第 {} 行:".format(
                        report.master_table, item["line"]
                    )
                )
                lines.append("    {}".format(item["json"]))
            if evidence["preference"]:
                for item in evidence["preference"]:
                    lines.append(
                        "  偏好 {}.ndjson 第 {} 行:".format(
                            report.preference_table, item["line"]
                        )
                    )
                    lines.append("    {}".format(item["json"]))
            else:
                lines.append("  偏好: 该键无偏好行，重连时偏好列被填为 null")
            lines.append(
                "  旧导出中同键行数: {}（其中不存在上述完整记录）".format(
                    evidence["original_total_matches"]
                )
            )
            for item in evidence["original_same_key"]:
                lines.append(
                    "    {}.ndjson 第 {} 行: {}".format(
                        report.original_table, item["line"], item["json"]
                    )
                )
        lines.append("  复现步骤（只依赖这两个导出目录，Python 标准库即可）:")
        for script_line in witness["repro"].splitlines():
            lines.append("    " + script_line)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
