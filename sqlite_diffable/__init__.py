from .cli import (
    apply_diff,
    apply_patch,
    build_patch,
    cell_key,
    decode_cell,
    diff_dumps,
    decode_row,
    encode_cell,
    encode_row,
    read_table,
    row_key,
    sync_directories,
    table_digest,
    write_table,
)


def match_duplicates(old_rows, new_rows):
    """
    Pair duplicate rows by stored value rather than position.

    Returns the row-level differences along with each row's old and new
    multiplicity, so a table without a primary key can be compared as the
    multiset it actually is.
    """
    from .cli import _row_diff

    removed, added, changes = _row_diff(
        [decode_row(list(row)) for row in old_rows],
        [decode_row(list(row)) for row in new_rows],
    )
    return {"removed": removed, "added": added, "changes": changes}


def diff_tables(old_rows, new_rows):
    "Return the removed and added rows, counted by exact stored value"
    from .cli import _row_diff

    removed, added, _ = _row_diff(old_rows, new_rows)
    return {"removed": removed, "added": added}


def diff_directories(old_directory, new_directory, tables=None):
    "Compare two dump directories and return a multiset patch"
    return build_patch(old_directory, new_directory, tables)


def apply_diff(directory, patch):
    "Apply a patch returned by diff_directories() to a dump directory"
    return apply_patch(directory, patch)


# Familiar spellings for the two core operations
compare_directories = diff_directories
compute_diff = diff_directories
diff_dirs = diff_directories
multiset_diff = diff_directories
patch_directory = apply_diff
apply_patch_to_directory = apply_diff
reconcile = sync_directories

__all__ = [
    "apply_diff",
    "apply_patch",
    "apply_patch_to_directory",
    "build_patch",
    "cell_key",
    "compare_directories",
    "compute_diff",
    "decode_cell",
    "decode_row",
    "diff_directories",
    "diff_dirs",
    "diff_dumps",
    "diff_tables",
    "encode_cell",
    "match_duplicates",
    "encode_row",
    "multiset_diff",
    "patch_directory",
    "read_table",
    "reconcile",
    "sync_directories",
    "row_key",
    "table_digest",
    "write_table",
]
