#!/usr/bin/env python3
"""Parse MYOB's per-FY "Journal entries report" exports into a flat dictionary.

manager-import.md §3/§5: this is the **lookup source of truth** for bank
categorization and non-bank journal reconstruction -- never pasted/posted as
the live books directly (Golden rule 2). Each MYOB transaction (General
journal / Bill payment / Spend money / Receive money / Transfer money / Pay
superannuation / ...) appears as a 3-row group:

    ['<Transaction type>', '<DD/MM/YYYY>', '<Description>', '', '']
    ['Ref no', 'Code', 'Category name', 'Debit ($)', 'Credit ($)']
    ['<ref>', '<code>', '<name>', '<debit>', '<credit>']   (one row per line)
    ...
    ['Total amount', '', '', '<sum debit>', '<sum credit>']

This must be the **expanded** export (Code + Debit/Credit columns) -- the
default MYOB "Journal entries report" without expanding categories collapses
to just Transaction type/Date/Description/Debit/Credit with no Code column
and is unusable for this purpose (see SKILL.md Hard-won facts).

Output: one row per (transaction, line) in out/manager/journal_dictionary.tsv,
keyed by (txn_index) so lines belonging to the same transaction can be
grouped back together (join on txn_id).

`txn_id` is a **sequential counter assigned during parsing, not a stable
identifier** -- re-running this against updated exports (expected repeatedly
during any side-by-side/parallel-run period) reassigns every id from the
point of any new/removed row onward. Never persist a bare `txn_id` across
regenerations (a manual-override config file, a hardcoded lookup table) --
key by something from MYOB itself (`ref_no`, or date+description+amount)
instead, or re-verify by inspection after every regeneration. See
reference/runbook.md's "txn_id is a sequential counter" section for the
full incident this was found from.

Skill-local, portable copy: expects `exports/myob/` and `out/manager/`
under the current working directory (run from the project root, the
established convention throughout this skill), not relative to this
file's own location -- this file lives inside the skill, which may be
symlinked into any project.

Usage:
  python3 scripts/build_journals.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_xlsx as X  # noqa: E402

ROOT = Path.cwd()
EXPORTS = ROOT / "exports" / "myob"
OUT = ROOT / "out" / "manager"
OUT_FILE = OUT / "journal_dictionary.tsv"


def parse_fy_file(path: Path, txn_id_start: int):
    rows = list(X.read_rows(path))
    i = 0
    txn_id = txn_id_start
    out = []
    while i < len(rows):
        row = rows[i]
        if row and row[0] and row[0] not in ("Ref no", "Total amount") and i + 1 < len(rows) and rows[i + 1][:3] == ["Ref no", "Code", "Category name"]:
            txn_type, issue_date, description = row[0], row[1], row[2]
            i += 2  # skip header row
            lines = []
            while i < len(rows) and rows[i] and rows[i][0] != "Total amount":
                ref, code, name, debit, credit = rows[i][:5]
                lines.append((ref, code, name, debit, credit))
                i += 1
            # skip the Total amount row
            if i < len(rows) and rows[i] and rows[i][0] == "Total amount":
                i += 1
            for line_no, (ref, code, name, debit, credit) in enumerate(lines, start=1):
                out.append(
                    {
                        "txn_id": txn_id,
                        "source_file": path.name,
                        "txn_type": txn_type,
                        "issue_date": issue_date,
                        "description": description,
                        "line_no": line_no,
                        "ref_no": ref,
                        "code": code,
                        "category_name": name,
                        "debit": debit,
                        "credit": credit,
                    }
                )
            txn_id += 1
        else:
            i += 1
    return out, txn_id


def main() -> int:
    files = sorted(EXPORTS.glob("journal_entries_FY*.xlsx"))
    if not files:
        print("[error] no journal_entries_FY*.xlsx files found under exports/myob/")
        return 1

    all_rows = []
    txn_id = 1
    for f in files:
        rows, txn_id = parse_fy_file(f, txn_id)
        print(f"[info] {f.name}: {len(rows)} lines, {len(set(r['txn_id'] for r in rows))} transactions")
        all_rows.extend(rows)

    OUT.mkdir(parents=True, exist_ok=True)
    fieldnames = ["txn_id", "source_file", "txn_type", "issue_date", "description",
                  "line_no", "ref_no", "code", "category_name", "debit", "credit"]
    with OUT_FILE.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t")
        w.writeheader()
        w.writerows(all_rows)

    print(f"[ok] wrote {OUT_FILE} ({len(all_rows)} lines, {txn_id - 1} transactions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
