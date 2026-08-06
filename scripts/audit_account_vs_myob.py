#!/usr/bin/env python3
"""Line-by-line audit of a single chart-of-accounts code: every Manager
`/transactions` entry alongside every MYOB journal-export line for that
code, date-sorted, to spot exactly where a Balance Sheet reconciliation
gap (from `reconcile_manager_to_myob.py`) comes from.

Generalized from `audit_gst_accounts.py` (see that script's docstring for
why day-level aggregation beats per-line matching -- MYOB nets multiple
invoice lines sharing a code+category into one journal-export row, while
Manager keeps one ledger entry per line, so exact per-line matching finds
false positives even on an unmerged, single-code account). Only use the
multi-code pooling from `audit_gst_accounts.py` if this account is a
member of `MERGED_CODES` there.

Skill-local, portable copy: expects `exports/myob/` and `out/manager/`
under the current working directory (run from the project root, the
established convention throughout this skill), not relative to this
file's own location -- this file lives inside the skill, which may be
symlinked into any project.

Depends on the sibling `manager-automation` skill for `lib_manager_api.py`
(this skill's own `scripts/` only holds MYOB-comparison-specific code) --
expects it at `../../manager-automation/scripts` relative to this file,
i.e. both skills installed side by side under the same `skills/` parent
(the convention this whole split assumes).

Usage:
  python3 scripts/audit_account_vs_myob.py 1-1560
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manager-automation" / "scripts"))
import lib_manager_api as API  # noqa: E402
from lib_xlsx import read_rows  # noqa: E402

ROOT = Path.cwd()
MYOB_DIR = ROOT / "exports" / "myob"
OUT_DIR = ROOT / "out" / "manager"
AMOUNT_TOLERANCE = 0.01


def fetch_manager_transactions(api: API.ManagerAPI, code: str) -> list[dict]:
    out = []
    skip = 0
    while True:
        r = api.get("/transactions", pageSize=50, skip=skip, term=code, sortBy="Date")
        out.extend(r["transactions"])
        skip += 50
        if skip >= r["totalRecords"]:
            break
    return [
        {"date": t["date"], "type": t["transaction"], "reference": "", "detail": t["account"], "amount": t["amount"]}
        for t in out
    ]


def dmy_to_iso(d: str) -> str:
    day, month, year = d.split("/")
    return f"{year}-{month}-{day}"


def parse_myob_journals(code: str) -> list[dict]:
    out = []
    for f in sorted(MYOB_DIR.glob("journal_entries_FY*.xlsx")):
        rows = read_rows(f)
        i = 0
        while i < len(rows):
            row = rows[i]
            if row[0] == "Ref no" and row[1] == "Code":
                header = rows[i - 1]
                txn_type, txn_date, txn_desc = header[0], header[1], header[2]
                i += 1
                while i < len(rows) and rows[i][0] != "Total amount":
                    ref_no, this_code, category, debit, credit = (rows[i] + ["", "", "", "", ""])[:5]
                    if this_code == code:
                        amount = (float(debit) if debit else 0.0) - (float(credit) if credit else 0.0)
                        out.append({
                            "date": dmy_to_iso(txn_date), "type": txn_type,
                            "reference": ref_no, "detail": f"{txn_desc} ({category})",
                            "amount": round(amount, 2),
                        })
                    i += 1
                continue
            i += 1

    opening = read_rows(MYOB_DIR / "trial_balance_opening.xlsx")
    header_i = next(idx for idx, r in enumerate(opening) if r[:1] == ["Account (code)"])
    for r in opening[header_i + 1:]:
        if r and r[0] == code:
            debit = float(r[2]) if r[2] else 0.0
            credit = float(r[3]) if r[3] else 0.0
            amount = round(debit - credit, 2)
            if abs(amount) >= AMOUNT_TOLERANCE:
                out.append({
                    "date": "2015-06-30", "type": "Opening balance", "reference": "",
                    "detail": "MYOB opening TB", "amount": amount,
                })
    return out


def match_and_report(manager_rows: list[dict], myob_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    myob_used = [False] * len(myob_rows)
    unmatched_manager = []
    for m in manager_rows:
        found = False
        for i, y in enumerate(myob_rows):
            if myob_used[i]:
                continue
            if y["date"] == m["date"] and abs(y["amount"] - m["amount"]) < AMOUNT_TOLERANCE:
                myob_used[i] = True
                found = True
                break
        if not found:
            unmatched_manager.append(m)
    unmatched_myob = [y for i, y in enumerate(myob_rows) if not myob_used[i]]
    return unmatched_manager, unmatched_myob


def write_csv(code: str, manager_rows: list[dict], myob_rows: list[dict]) -> Path:
    combined = (
        [{"source": "Manager", **r} for r in manager_rows]
        + [{"source": "MYOB", **r} for r in myob_rows]
    )
    combined.sort(key=lambda r: (r["date"], r["source"]))

    running = {"Manager": 0.0, "MYOB": 0.0}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"account_audit_{code}.csv"
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "source", "type", "reference", "detail", "amount",
                    "running_balance_manager", "running_balance_myob"])
        for r in combined:
            running[r["source"]] += r["amount"]
            w.writerow([r["date"], r["source"], r["type"], r["reference"], r["detail"],
                        f"{r['amount']:.2f}", f"{running['Manager']:.2f}", f"{running['MYOB']:.2f}"])
    return out_path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/audit_account_vs_myob.py <account-code>")
        return 1
    code = sys.argv[1]

    api = API.ManagerAPI()
    manager_rows = sorted(fetch_manager_transactions(api, code), key=lambda r: r["date"])
    myob_rows = sorted(parse_myob_journals(code), key=lambda r: r["date"])

    out_path = write_csv(code, manager_rows, myob_rows)
    m_total = sum(r["amount"] for r in manager_rows)
    y_total = sum(r["amount"] for r in myob_rows)
    print(f"[ok] wrote {out_path} ({len(manager_rows)} Manager rows, {len(myob_rows)} MYOB rows)")
    print(f"[total] Manager: {m_total:,.2f}  MYOB: {y_total:,.2f}  diff: {m_total - y_total:,.2f}")

    unmatched_manager, unmatched_myob = match_and_report(manager_rows, myob_rows)
    print(f"[match] {len(manager_rows) - len(unmatched_manager)} exact (date,amount) matches, "
          f"{len(unmatched_manager)} Manager rows unmatched, {len(unmatched_myob)} MYOB rows unmatched")

    # Day-level net diff -- more reliable than per-line matching when
    # granularity differs between the two systems (see docstring).
    by_date = defaultdict(lambda: {"Manager": 0.0, "MYOB": 0.0})
    for r in manager_rows:
        by_date[r["date"]]["Manager"] += r["amount"]
    for r in myob_rows:
        by_date[r["date"]]["MYOB"] += r["amount"]
    day_diffs = []
    for d, v in by_date.items():
        dd = round(v["Manager"] - v["MYOB"], 2)
        if abs(dd) >= 0.01:
            day_diffs.append((d, v["Manager"], v["MYOB"], dd))
    day_diffs.sort(key=lambda x: -abs(x[3]))

    print(f"\n{len(day_diffs)} dates with a day-level net diff, sorted by size:")
    for d, m, y, dd in day_diffs:
        print(f"  {d}  manager={m:>12,.2f}  myob={y:>12,.2f}  diff={dd:>10,.2f}")

    if unmatched_manager:
        print(f"\nIn Manager but no matching MYOB line ({len(unmatched_manager)}):")
        for m in unmatched_manager:
            print(f"  {m['date']}  {m['type']:<20} {m['amount']:>12,.2f}  {m['detail']}")
    if unmatched_myob:
        print(f"\nIn MYOB but no matching Manager transaction ({len(unmatched_myob)}):")
        for y in unmatched_myob:
            print(f"  {y['date']}  {y['type']:<20} {y['amount']:>12,.2f}  ref {y['reference']}  {y['detail']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
