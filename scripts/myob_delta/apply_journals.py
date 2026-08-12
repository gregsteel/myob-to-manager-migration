#!/usr/bin/env python3
"""Create Manager General Journal entries for the genuine standalone MYOB
journals filter_delta.candidate_journal_groups() identifies (BAS/FBT/
depreciation/income-tax adjusting entries, EOY closing entries, dividend
reallocations, director-funded reimbursements with no bank line) --
everything already captured via Bills/Invoices/bank-feed is excluded by
that filter, so anything reaching this script is meant to become a real
Manager Journal Entry.

Dedup: a `[MYOB {ref_no}/{issue_date}]` marker (manager_index.journal_marker)
is appended to each created journal's Narration, and checked against every
live journal-entry's Narration before creating (manager_index.build_journal_
index) -- NOT the journal_dictionary.tsv `txn_id`, which build_journals.py's
own docstring confirms is an unstable regeneration-order counter, unsafe to
persist across re-runs of that script.

Each MYOB journal group's own Debit/Credit split (already balanced --
verified before applying) becomes the Manager Lines directly; Journal
Entry lines use Debit/Credit fields (not Amount, which is Payment/Receipt-
only -- see manager-automation's invoice-linking.md).

Never creates anything dated after today's real date (Golden Rule, both
skills' SKILL.md).

Usage:
  python3 scripts/myob_delta/apply_journals.py             # dry-run (default)
  python3 scripts/myob_delta/apply_journals.py --apply     # real writes
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# SKILL_SCRIPTS locates sibling modules at this file's real (post-symlink)
# location -- see filter_delta.py's header comment for the full
# explanation. This file has no project-data-root paths of its own.
SKILL_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_SCRIPTS / "myob_playwright"))
sys.path.insert(0, str(SKILL_SCRIPTS.parent.parent / "manager-automation" / "scripts"))
import lib_manager_api as API  # noqa: E402
import manager_index as MI  # noqa: E402
import filter_delta as FD  # noqa: E402


def _load_coa(api: API.ManagerAPI) -> dict[str, str]:
    return {a["code"]: a["key"] for a in api.get("/chart-of-accounts")["chartOfAccounts"] if a.get("code")}


def build_journal_payload(rows: list[dict], coa: dict) -> dict | None:
    r0 = rows[0]
    issue_date = FD._dmy_to_iso(r0["issue_date"])
    lines = []
    total_debit = total_credit = 0.0
    for r in rows:
        code = r["code"]
        if code not in coa:
            print(f"    [skip line] unknown account code {code!r}")
            return None
        line = {"Account": coa[code], "LineDescription": r0["description"]}
        if r["debit"]:
            amt = float(r["debit"])
            line["Debit"] = amt
            total_debit += amt
        if r["credit"]:
            amt = float(r["credit"])
            line["Credit"] = amt
            total_credit += amt
        lines.append(line)

    if abs(total_debit - total_credit) > 0.01:
        print(f"    [skip] unbalanced: debit={total_debit:.2f} credit={total_credit:.2f}")
        return None

    marker = MI.journal_marker(r0["ref_no"], issue_date)
    narration = f"{r0['description']} {marker}"
    return {"Date": issue_date, "Narration": narration, "Lines": lines}, total_debit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--after-date", default=None)
    args = ap.parse_args()

    api = API.ManagerAPI()
    after = args.after_date or FD.last_migration_date()
    today = date.today().isoformat()

    print("[info] scanning live Journal Entries for existing MYOB markers...")
    existing_markers = MI.build_journal_index(api)
    print(f"[info] {len(existing_markers)} existing markers found")

    new_groups = FD.find_new_journals(existing_markers, after)
    print(f"\n[info] {len(new_groups)} genuine standalone journal(s) to apply (issue_date > {after})\n")

    coa = _load_coa(api)
    for rows in new_groups:
        r0 = rows[0]
        issue_date = FD._dmy_to_iso(r0["issue_date"])
        if issue_date > today:
            print(f"[skip] ref={r0['ref_no']} {issue_date}: after today ({today}) -- refuse to create")
            continue

        built = build_journal_payload(rows, coa)
        if built is None:
            print(f"[skip] ref={r0['ref_no']} {issue_date}: could not build payload (see above)")
            continue
        payload, total = built

        print(f"{'[APPLY]' if args.apply else '[DRY-RUN]'} ref={r0['ref_no']}  {issue_date}  "
              f"{r0['description']!r}  lines={len(payload['Lines'])}  total=${total:,.2f}")
        for l in payload["Lines"]:
            side = f"Dr {l['Debit']:>10,.2f}" if "Debit" in l else f"Cr {l['Credit']:>10,.2f}"
            print(f"           {side}  (account key {l['Account'][:8]}...)")

        if args.apply:
            j = api.post_form("journal", payload)
            key = j.get("key") or j.get("Key")
            print(f"           -> created key={key}")

    if not args.apply:
        print("\n[dry-run] no writes made -- pass --apply to create these")
    return 0


if __name__ == "__main__":
    sys.exit(main())
