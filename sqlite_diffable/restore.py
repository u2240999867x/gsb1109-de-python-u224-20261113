import json
import pathlib
import sqlite3

import sqlite_utils


class RestoreError(Exception):
    pass


def _load_archive(directory):
    """Read every *.metadata.json / *.ndjson pair in the archive directory."""
    directory = pathlib.Path(directory)
    archive = {}
    for metadata_path in directory.glob("*.metadata.json"):
        info = json.loads(metadata_path.read_text())
        table = info["name"]
        columns = info["columns"]
        ndjson_path = metadata_path.parent / metadata_path.name.replace(
            ".metadata.json", ".ndjson"
        )
        rows = []
        if ndjson_path.exists():
            for line in ndjson_path.open():
                if line.strip():
                    rows.append(dict(zip(columns, json.loads(line))))
        archive[table] = {"columns": columns, "rows": rows}
    if not archive:
        raise RestoreError("No *.metadata.json files found in {}".format(directory))
    return archive


def _foreign_key_groups(db):
    """Group PRAGMA foreign_key_list rows so composite keys stay together."""
    groups = {}
    reverse = {}
    for table in db.table_names():
        raw = db.conn.execute(
            'PRAGMA foreign_key_list("{}")'.format(table)
        ).fetchall()
        by_id = {}
        for fk in raw:
            # columns: id, seq, table, from, to, on_update, on_delete, match
            group = by_id.setdefault(
                fk[0],
                {"ref_table": fk[2], "columns": [], "ref_columns": []},
            )
            group["columns"].append(fk[3])
            group["ref_columns"].append(fk[4])
        groups[table] = list(by_id.values())
        for group in by_id.values():
            reverse.setdefault(group["ref_table"], []).append(
                {
                    "dependent_table": table,
                    "columns": group["columns"],
                    "ref_columns": group["ref_columns"],
                }
            )
    return groups, reverse


def _primary_key_columns(db, table):
    if table not in db.table_names():
        return None
    return db[table].pks


def _row_key(db, table, row):
    pks = _primary_key_columns(db, table)
    if pks:
        return tuple(row[col] for col in pks)
    return tuple(row[col] for col in db[table].columns_dict)


class _ArchiveIndex:
    def __init__(self, archive):
        self.archive = archive
        self._lookup_cache = {}

    def rows(self, table):
        return self.archive.get(table, {"rows": []})["rows"]

    def find(self, table, columns, key):
        """Find an archive row by an arbitrary tuple of columns."""
        cache_key = (table, tuple(columns))
        index = self._lookup_cache.get(cache_key)
        if index is None:
            index = {
                tuple(row[col] for col in columns): row
                for row in self.rows(table)
            }
            self._lookup_cache[cache_key] = index
        return index.get(tuple(key))

    def find_all(self, table, columns, key):
        cache_key = (table, tuple(columns))
        index = self._lookup_cache.get(cache_key)
        if index is None:
            index = {}
            for row in self.rows(table):
                index.setdefault(
                    tuple(row[col] for col in columns), []
                ).append(row)
            self._lookup_cache[cache_key] = index
        return index.get(tuple(key), [])


def _as_dict(cursor, found):
    if found is None:
        return None
    if isinstance(found, sqlite3.Row):
        return dict(found)
    return {
        desc[0]: value for desc, value in zip(cursor.description, found)
    }


def _target_find(db, table, columns, key):
    if table not in db.table_names():
        return None
    available = set(db[table].columns_dict)
    if not set(columns).issubset(available):
        return None
    where = " AND ".join('"{}" = ?'.format(col) for col in columns)
    cursor = db.conn.execute(
        'SELECT * FROM "{}" WHERE {}'.format(table, where), tuple(key)
    )
    return _as_dict(cursor, cursor.fetchone())


def _normalized(db, table, row):
    """Project a row onto the target table's column order for comparison."""
    target_columns = [col.name for col in db[table].columns]
    archive_columns = set(row)
    if set(target_columns) != archive_columns:
        raise RestoreError(
            "Column mismatch on table {}: archive has {}, target has {}".format(
                table, sorted(archive_columns), target_columns
            )
        )
    return tuple(row[col] for col in target_columns)


def _rows_differ(db, table, archive_row, target_row):
    target_columns = [col.name for col in db[table].columns]
    archive_values = _normalized(db, table, archive_row)
    target_values = tuple(target_row[col] for col in target_columns)
    if archive_values == target_values:
        return []
    return [
        col
        for col, archived, existing in zip(
            target_columns, archive_values, target_values
        )
        if archived != existing
    ]


def restore(db_path, archive_dir, anchor_table, filters):
    """
    Restore rows reachable from filtered anchor rows into an existing database.

    anchor_table: table holding the documents to filter (e.g. receipts).
    filters: mapping of column/value pairs the anchor rows must match,
             e.g. {"branch": "S001"} restores one branch only.

    Rows already present in the target with identical content are skipped.
    Rows present with the same key but different content are conflicts and
    are never overwritten; anchor documents depending on them are reported as
    blocked. Cyclic groups restore as long as the cycle is closed inside the
    archive. Only data reachable from the filtered anchors is imported.
    """
    db = sqlite_utils.Database(db_path)
    archive = _load_archive(archive_dir)
    index = _ArchiveIndex(archive)
    fk_groups, reverse_fks = _foreign_key_groups(db)

    if anchor_table not in archive:
        raise RestoreError(
            "Anchor table {} is not present in the archive".format(anchor_table)
        )
    if anchor_table not in db.table_names():
        raise RestoreError(
            "Anchor table {} does not exist in target database".format(anchor_table)
        )
    anchor_columns = set(archive[anchor_table]["columns"])
    unknown = [col for col in filters if col not in anchor_columns]
    if unknown:
        raise RestoreError(
            "Unknown filter column(s) {} on table {} (available: {})".format(
                unknown, anchor_table, sorted(anchor_columns)
            )
        )

    anchors = [
        row
        for row in index.rows(anchor_table)
        if all(row.get(col) == value for col, value in filters.items())
    ]
    anchor_idents = [
        (anchor_table, _row_key(db, anchor_table, row)) for row in anchors
    ]
    anchor_ident_set = set(anchor_idents)

    # Walk the foreign-key closure starting at the filtered anchor rows, in
    # both directions: outgoing references (suppliers, contracts) and rows
    # referencing the anchors back (receipt lines). Reverse edges are guarded
    # so the walk never crosses into another branch's anchor document.
    # edges[x] holds the idents x requires; bad holds unsatisfiable keys.
    needed = {}
    bad = {}
    edges = {}
    queue = [(anchor_table, row) for row in anchors]
    enqueued = set()

    def require_pk(table):
        pks = _primary_key_columns(db, table)
        if pks is None:
            raise RestoreError(
                "Table {} has no primary key; cannot restore safely".format(table)
            )
        return pks

    while queue:
        table, row = queue.pop(0)
        pks = require_pk(table)
        key = tuple(row[col] for col in pks)
        ident = (table, key)
        if ident in enqueued:
            continue
        enqueued.add(ident)
        edges.setdefault(ident, set())

        target_row = _target_find(db, table, pks, key)
        if target_row is not None:
            differing = _rows_differ(db, table, row, target_row)
            if differing:
                bad[ident] = {
                    "kind": "content_mismatch",
                    "table": table,
                    "key": list(key),
                    "differing_columns": differing,
                }
                continue
            # Identical row already in the target: nothing to import. Its own
            # references are satisfied by the target, but dependent rows may
            # still need restoring, so fall through to reverse traversal.
            needed[ident] = {"table": table, "row": row, "exists": True}
        else:
            needed[ident] = {"table": table, "row": row, "exists": False}
            for group in fk_groups.get(table, []):
                ref_key = tuple(row[col] for col in group["columns"])
                if any(value is None for value in ref_key):
                    continue
                ref_table = group["ref_table"]
                ref_ident = (ref_table, tuple(ref_key))
                edges[ident].add(ref_ident)
                target_ref = _target_find(
                    db, ref_table, group["ref_columns"], ref_key
                )
                archive_ref = index.find(
                    ref_table, group["ref_columns"], ref_key
                )
                if target_ref is not None:
                    if archive_ref is not None:
                        differing = _rows_differ(
                            db, ref_table, archive_ref, target_ref
                        )
                        if differing:
                            bad[ref_ident] = {
                                "kind": "content_mismatch",
                                "table": ref_table,
                                "key": list(
                                    _row_key(db, ref_table, target_ref)
                                ),
                                "differing_columns": differing,
                            }
                            continue
                    # Satisfied by the target, no import required.
                    continue
                if archive_ref is None:
                    bad[ref_ident] = {
                        "kind": "missing_dependency",
                        "table": ref_table,
                        "key": list(ref_key),
                        "referenced_by_columns": group["columns"],
                    }
                    continue
                queue.append((ref_table, archive_ref))

        # Reverse traversal: rows (e.g. receipt lines) pointing at this row.
        for back in reverse_fks.get(table, []):
            lookup_key = tuple(row[col] for col in back["ref_columns"])
            dep_table = back["dependent_table"]
            for dep_row in index.find_all(dep_table, back["columns"], lookup_key):
                dep_pks = require_pk(dep_table)
                dep_ident = (
                    dep_table,
                    tuple(dep_row[col] for col in dep_pks),
                )
                # Never pull in anchor documents the filter did not select.
                if dep_table == anchor_table and dep_ident not in anchor_ident_set:
                    continue
                edges.setdefault(dep_ident, set()).add(ident)
                queue.append((dep_table, dep_row))

    def reaches_bad(ident, seen):
        if ident in bad:
            return True
        if ident in seen:
            return False
        seen.add(ident)
        return any(reaches_bad(ref, seen) for ref in edges.get(ident, ()))

    blocked_anchor_idents = set()
    for ident in anchor_idents:
        if reaches_bad(ident, set()):
            blocked_anchor_idents.add(ident)

    # Keep nodes tied to a surviving anchor. Two directed sweeps from the
    # surviving anchors cover both directions separately:
    #   forward  -> documents' dependencies (suppliers, contracts, cycles)
    #   backward -> rows depending on the documents (e.g. receipt lines)
    # They must NOT be merged into an undirected walk, otherwise shared
    # dependency nodes (a branch row referenced by every receipt) would pull
    # blocked documents back in. reaches_bad filters those out anyway.
    reverse_edges = {}
    for source, targets in edges.items():
        for target in targets:
            reverse_edges.setdefault(target, set()).add(source)

    def sweep(adjacency, seeds):
        seen = set()
        work = list(seeds)
        while work:
            ident = work.pop()
            if ident in seen:
                continue
            seen.add(ident)
            work.extend(adjacency.get(ident, ()))
        return seen

    surviving = [
        ident for ident in anchor_idents if ident not in blocked_anchor_idents
    ]
    forward_reach = sweep(edges, surviving)
    backward_reach = sweep(reverse_edges, surviving)
    keep = {
        ident
        for ident in forward_reach | backward_reach
        if not reaches_bad(ident, set())
    }

    imported = {}
    skipped_identical = {}
    rows_to_insert = {}
    for ident in keep:
        table, key = ident
        entry = needed.get(ident)
        if entry is None:
            continue
        if entry["exists"]:
            skipped_identical.setdefault(table, []).append(list(key))
        else:
            rows_to_insert.setdefault(table, []).append(entry["row"])
            imported.setdefault(table, []).append(list(key))

    # Foreign keys are deferred for the load so closed cyclic groups insert.
    # Only violations introduced by this load are fatal: pre-existing target
    # database inconsistencies are recorded separately, not treated as ours.
    fk_was_on = db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
    baseline_violations = {
        tuple(violation)
        for violation in db.conn.execute("PRAGMA foreign_key_check").fetchall()
    }
    db.conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table, rows in rows_to_insert.items():
            db[table].insert_all(rows, alter=False)
        db.conn.commit()
        current_violations = {
            tuple(violation)
            for violation in db.conn.execute("PRAGMA foreign_key_check").fetchall()
        }
        new_violations = current_violations - baseline_violations
        if new_violations:
            db.conn.rollback()
            raise RestoreError(
                "Restored data violates foreign keys: {}".format(
                    [list(violation) for violation in sorted(new_violations)]
                )
            )
    except Exception:
        db.conn.rollback()
        raise
    finally:
        db.conn.execute(
            "PRAGMA foreign_keys={}".format("ON" if fk_was_on else "OFF")
        )

    blocked = []
    for ident in sorted(blocked_anchor_idents):
        reasons = []
        seen_bad = set()

        def collect(ident):
            for ref in edges.get(ident, []):
                if ref in bad and ref not in seen_bad:
                    seen_bad.add(ref)
                    reasons.append(bad[ref])
                elif ref not in seen_bad:
                    seen_bad.add(ref)
                    collect(ref)

        collect(ident)
        if ident in bad:
            reasons.append(bad[ident])
        blocked.append(
            {"table": ident[0], "key": list(ident[1]), "reasons": reasons}
        )

    return {
        "anchor_table": anchor_table,
        "filters": filters,
        "imported": imported,
        "skipped_identical": skipped_identical,
        "blocked": blocked,
        "conflicts": list(bad.values()),
    }
