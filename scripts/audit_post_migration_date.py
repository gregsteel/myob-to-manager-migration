#!/usr/bin/env python3
"""Audit: flag any Manager record dated after the last confirmed migration date.

During side-by-side MYOB/Manager operation, Manager should only contain data
that has actually been migrated and reconciled up to a known cutoff -- the
"last migration date". Anything dated after that boundary got into Manager
some other way (a stray manual entry, a premature sync, testing) and is not
yet backed by a completed migration+reconcile pass, even if it looks like an
ordinary transaction.

This matters because a missing-counterpart audit (e.g. "this Sales Invoice
has no matching Receipt") run without bounding by this date will misdiagnose
the gap -- the fix isn't "reconstruct the missing side", it's "this record
shouldn't be in Manager yet at all". Confirmed 2026-08-06: a Sales Invoice
dated after the last migration date had no matching Receipt purely because
the invoice itself was premature, not because a receipt was ever missing.

Read-only: this script only reports, it never deletes (per manager-automation
Golden Rule 2 -- never delete a live ledger object based on inference).
Review the flagged list and delete anything genuinely premature by hand.
Only advance config/last_migration_date.txt after the next migration+reconcile
pass confirms Manager matches MYOB up to the new date.

Skill-local, portable copy: expects `config/last_migration_date.txt` (a
single ISO date, e.g. `2026-06-30`) under the project root unless
--after-date is given explicitly -- searched for by walking upward from the
current working directory, so always invoke with the project root as the
working directory. Depends on the sibling `manager-automation` skill for
`lib_manager_api.py` -- expects it at `../../manager-automation/scripts`
relative to this file.

Usage:
  python3 scripts/audit_post_migration_date.py
  python3 scripts/audit_post_migration_date.py --after-date 2026-06-30
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manager-automation" / "scripts"))
import lib_manager_api as API  # noqa: E402

# entity alias (matches lib_manager_api.LIST_PATHS) -> (date field, reference field)
# NOTE: these are LIST-endpoint field names (camelCase), not form-level field
# names (PascalCase, e.g. form["Date"]) -- confirmed 2026-08-06 after this
# script's original PascalCase field names silently matched nothing (r.get()
# on a missing key returns "", which never compares > a real date), making
# an early version wrongly report zero flagged records on a live instance
# that actually had dozens. Verify field casing against a real list response
# before trusting a new domain here, don't assume it matches the form.
DOMAINS = [
    ("sales-invoice", "issueDate", "reference"),
    ("purchase-invoice", "issueDate", "reference"),
    ("receipt", "date", "reference"),
    ("payment", "date", "reference"),
    ("journal-entry", "date", "reference"),
]


def find_last_migration_date() -> str:
    d = Path.cwd()
    for candidate in (d, *d.parents):
        p = candidate / "config" / "last_migration_date.txt"
        if p.is_file():
            return p.read_text().strip()
    raise SystemExit(
        "No --after-date given and no config/last_migration_date.txt found "
        "searching upward from the current working directory."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--after-date", help="ISO date (YYYY-MM-DD); defaults to config/last_migration_date.txt")
    args = ap.parse_args()

    after = args.after_date or find_last_migration_date()
    print(f"[info] flagging any Manager record dated after {after} (last migration date)")

    api = API.ManagerAPI()
    flagged: dict[str, list[dict]] = {}
    total = 0
    for entity, date_field, ref_field in DOMAINS:
        rows = api.list_all(entity, page_size=200)
        hits = [r for r in rows if r.get(date_field, "") > after]
        if hits:
            flagged[entity] = [
                {
                    "date": r.get(date_field),
                    "reference": r.get(ref_field),
                    "key": r["key"],
                    "party": r.get("customer") or r.get("supplier") or r.get("description"),
                }
                for r in hits
            ]
            total += len(hits)
            print(f"  {entity}: {len(hits)} record(s) after {after}")

    out_dir = Path.cwd() / "out" / "manager"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "post_migration_date_flagged.json"
    out_path.write_text(json.dumps(flagged, indent=2))
    print(f"\n[result] {total} record(s) flagged across {len(flagged)} domain(s) -- see {out_path}")
    if total:
        print("[action] review each by hand; delete anything genuinely premature. "
              "Do not advance config/last_migration_date.txt until a fresh "
              "migration+reconcile pass confirms Manager matches MYOB up to the new date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
