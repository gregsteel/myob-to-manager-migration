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
# pipeline (or, for Supplier return applied, other existing pipelines this
# project already has) -- see reference/runbook.md "Recovering deferred
# non-bank journals" for the source of this list.
#
# "Pay run" is excluded from this filter for a DIFFERENT reason: nothing in
# this project actually captures it automatically. It's excluded so it
# doesn't get misfiled as a generic BAS/FBT/depreciation-style adjusting
# journal by find_new_journals() below -- payroll needs its own hand-built
# entry per pay run (manager-automation reference/payroll.md), and MYOB's
# own `description` for these rows is the business's postal address, not a
# real memo -- never copy it verbatim into a Manager Narration. See this
# skill's SKILL.md "Hard-won MYOB-migration facts".
#
# That hand-built entry is NOT always a plain journal, and which MYOB
# clearing pattern a "Pay run" row used does NOT decide whether it needs a
# Payslip -- only whether the *original booking* was dollar-correct. Check
# which account each row credits for net pay:
#   - Pattern 1 (pre-STP): the real bank account directly, no clearing leg
#     at all. Dollar-correct and self-contained as migrated (an ordinary
#     journal/Payment is not WRONG) -- but it never touches Manager's
#     builtin Employee clearing account, so it produces no Payslip and no
#     per-employee data.
#   - Pattern 2 (post-STP electronic payment): credits MYOB's Electronic
#     Clearing Account (often coded 1-3000, an Asset -- an ABA-file parking
#     lot), then a second same-day-or-later transaction clears it to the
#     real bank. Migrating the accrual leg as a journal against whatever
#     account 1-3000 was seeded to is wrong regardless of pattern -- that
#     account is a chart lookalike, not the builtin control, and can never
#     populate per-employee Payslip balances.
# Detection is which account was credited, not a date -- the switchover is
# specific to when each business adopted electronic/STP payment (confirmed
# real case: Lilith Pty Ltd switched 13/05/2024) and isn't portable across
# clients.
#
# If the target Manager instance uses native Payslips at all, BOTH patterns
# need the same correction, not just Pattern 2: build a native Manager
# Payslip per pay run (per-item mapping for Wages/PAYGW/Super, net pay to
# the builtin Employee clearing account), then repoint the linked bank
# Payment (whatever account it actually used) to Employee clearing for the
# same net amount. Don't delete an accrual-side journal that's already
# right except for the account -- reverse it dated the same historical day
# instead (P&L nets to zero, Balance Sheet was already flat by cycle end),
# especially when the tooling in use can't delete records.
#
# Known API limitation: Manager's Payment write endpoint has no employee-tag
# field on payment lines (only Account/Amount/Description/LineDescription/
# AccountsPayable*), even when Account is the builtin Employee clearing
# control -- so a Payment retrofitted via this API lands on the control
# account untagged to any employee (same "Suspense" failure shape as
# untagged builtin AR/AP). A Payslip's own net-pay credit is tagged
# automatically; the Payment side is not. Don't assume a scripted fix here
# achieves full per-employee fidelity -- check in the Manager UI whether the
# tag can be added manually.
#
# See SKILL.md's Electronic Clearing Account note and
# manager-automation/reference/payroll.md "Employee clearing account" /
# "Migration impact".
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
        if m["purchase"] is None and m["debit_note"] is None:
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
