#!/usr/bin/env python3
"""Filter harvested MYOB Bills/Invoices to the delta since the last confirmed
migration point, cross-checked against live Manager for existence.

Reads:
  exports/myob/bills/_index.tsv      (from myob_playwright/download_bills.py)
  exports/myob/invoices/_index.tsv   (from myob_playwright/download_invoices.py)
  config/last_migration_date.txt     (cutoff -- see myob-to-manager-migration
                                       SKILL.md Golden Rule 7)

For each row dated after the cutoff, checks live Manager (via
scripts/myob_playwright/manager_index.py's composite-Reference convention)
and reports only the ones NOT already present -- this is the actual
"don't double-up" layer; the docs assumed a generic `apply_manager_api.py`
did this, but that script doesn't exist in this repo (confirmed by full
git-history search), so it lives here instead.

Read-only. Does not touch Manager or MYOB data. Phase 2
(apply_bills_invoices.py) consumes this script's output.

Usage:
  python3 scripts/myob_delta/filter_delta.py [--after-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# Two different "roots" here, not to be confused: SKILL_SCRIPTS locates
# sibling modules at this file's real (post-symlink) location; ROOT is the
# *host project's* root (data files -- exports/, out/, config/), found via
# cwd per this skill's established convention (build_journals.py does the
# same) -- always invoke with the project root as the working directory.
SKILL_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_SCRIPTS / "myob_playwright"))
import manager_index as MI  # noqa: E402

ROOT = Path.cwd()
BILLS_INDEX = ROOT / "exports" / "myob" / "bills" / "_index.tsv"
INVOICES_INDEX = ROOT / "exports" / "myob" / "invoices" / "_index.tsv"
JOURNAL_DICT = ROOT / "out" / "manager" / "journal_dictionary.tsv"
LAST_MIGRATION_FILE = ROOT / "config" / "last_migration_date.txt"
REAL_BANK_ACCOUNTS_FILE = ROOT / "config" / "real_bank_accounts.tsv"

USABLE_BILL_STATUS = {"ok", "partial"}


def _load_bank_account_codes() -> set[str]:
    """Real bank/cash account codes, from project config (per-business --
    every business has its own chart of accounts) -- a journal group
    touching one of these is already captured by the Manager bank feed,
    never a standalone adjusting entry to recreate. Reuses
    config/real_bank_accounts.tsv rather than a second hardcoded copy --
    this file already existed for other reconciliation scripts before the
    delta-migration tooling did."""
    codes = set()
    with REAL_BANK_ACCOUNTS_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row.get("code"):
                codes.add(row["code"])
    return codes


BANK_ACCOUNT_CODES = _load_bank_account_codes()

# Transaction types already captured via apply_bills_invoices.py's PI/SI
# pipeline (or, for Pay run/Supplier return applied, other existing
# pipelines this project already has) -- see reference/runbook.md
# "Recovering deferred non-bank journals" for the source of this list.
CAPTURED_TXN_TYPES = {
    "Bill", "Invoice", "Sale", "Bill payment", "Pay run",
    "Supplier return applied", "Invoice payment", "Receive refund",
}


def last_migration_date() -> str:
    return LAST_MIGRATION_FILE.read_text().strip()


def candidate_bill_rows(after_date: str) -> list[dict]:
    rows = []
    with BILLS_INDEX.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row.get("status") not in USABLE_BILL_STATUS:
                continue
            if (row.get("issue_date") or "") > after_date:
                rows.append(row)
    return rows


def candidate_invoice_rows(after_date: str) -> list[dict]:
    rows = []
    with INVOICES_INDEX.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row.get("download_status") != "done":
                continue
            if (row.get("issue_date") or "") > after_date:
                rows.append(row)
    return rows


def find_new_bills(idx: dict, after_date: str) -> list[dict]:
    new = []
    for row in candidate_bill_rows(after_date):
        m = MI.match_bill(idx, bill_number=row["bill_number"], issue_date=row["issue_date"])
        if m["purchase"] is None:
            new.append(row)
    return new


def find_new_invoices(idx: dict, after_date: str) -> list[dict]:
    new = []
    for row in candidate_invoice_rows(after_date):
        m = MI.match_invoice(idx, number=row["number"], issue_date=row["issue_date"])
        if m["sale"] is None:
            new.append(row)
    return new


def _dmy_to_iso(dmy: str) -> str:
    d, m, y = dmy.strip().split("/")
    return f"{y}-{m}-{d}"


def candidate_journal_groups(after_date: str) -> list[list[dict]]:
    """journal_dictionary.tsv rows dated after `after_date`, grouped by
    txn_id (stable within one regeneration -- see build_journals.py's
    warning about persisting it across regenerations, which this does
    NOT do), filtered to genuine standalone MYOB General Journals: not
    touching a real bank account, not one of CAPTURED_TXN_TYPES.
    Validated 2026-08-12 against FY2026's known real journals (the EOY
    Adjustment, FBT write-off, FY25 depreciation catch-up, FBT
    reallocation, employee-contribution journal) -- all 5 correctly
    surfaced, nothing else did.
    """
    groups: dict[str, list[dict]] = {}
    with JOURNAL_DICT.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if _dmy_to_iso(row["issue_date"]) <= after_date:
                continue
            groups.setdefault(row["txn_id"], []).append(row)

    genuine = []
    for txn_id, rows in groups.items():
        codes = {r["code"] for r in rows}
        if codes & BANK_ACCOUNT_CODES:
            continue
        if rows[0]["txn_type"] in CAPTURED_TXN_TYPES:
            continue
        genuine.append(rows)
    return genuine


def find_new_journals(existing_markers: set[str], after_date: str) -> list[list[dict]]:
    new = []
    for rows in candidate_journal_groups(after_date):
        r0 = rows[0]
        marker = MI.journal_marker(r0["ref_no"], _dmy_to_iso(r0["issue_date"]))
        if marker not in existing_markers:
            new.append(rows)
    return new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after-date", default=None,
                     help="override config/last_migration_date.txt")
    args = ap.parse_args()

    after = args.after_date or last_migration_date()
    print(f"[info] delta filter: issue_date > {after}")

    idx = MI.build_index()

    bills = candidate_bill_rows(after)
    new_bills = find_new_bills(idx, after)
    print(f"\n[bills] {len(bills)} harvested in range, {len(new_bills)} not yet in Manager")
    for b in new_bills:
        print(f"  NEW  {b['bill_number']:>10s}  {b['issue_date']}  "
              f"{b['supplier']:30s}  ${b['total']:>10s}")

    invoices = candidate_invoice_rows(after)
    new_invoices = find_new_invoices(idx, after)
    print(f"\n[invoices] {len(invoices)} harvested in range, {len(new_invoices)} not yet in Manager")
    for i in new_invoices:
        print(f"  NEW  {i['number']:>10s}  {i['issue_date']}  "
              f"{i['customer']:30s}  ${i['amount']:>10s}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
