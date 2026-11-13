"""Reproduction check: run the ORIGINAL validation entry point on each record.

- original_record.json   : internal only, must fail at rule R2 (quantity subtotal)
- sanitized_record.json  : shareable substitute, must fail at the SAME rule R2
- naive_masked_record.json: per-cell "***" masking, expected to fail EARLIER
  (field-level format rule) — demonstrates why naive masking is not deliverable

Exit code 0 only if original and sanitized hit the identical failure path.
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))  # make the local `schema` package importable

from schema import SchemaError  # noqa: E402

from order_validation import validate_order  # noqa: E402


def failure_path(record_file):
    record = json.loads((BASE / record_file).read_text(encoding="utf-8"))
    try:
        validate_order(record)
    except SchemaError as exc:
        return exc.code
    return None


def main():
    original = failure_path("original_record.json")
    sanitized = failure_path("sanitized_record.json")
    naive = failure_path("naive_masked_record.json")

    print("original  ->", original)
    print("sanitized ->", sanitized)
    print("naive *** ->", naive)
    print()

    ok = True
    if original is None:
        print("FAIL: original record did not reproduce the rejection at all")
        ok = False
    if original != sanitized:
        print("FAIL: sanitized record does not hit the same failure path as original")
        ok = False
    else:
        print("OK: sanitized record reproduces the identical failure path (R2)")
    if naive == original:
        print("UNEXPECTED: naive *** masking also reaches R2")
    else:
        print("OK: naive *** masking fails earlier (different rule) -> not deliverable")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
