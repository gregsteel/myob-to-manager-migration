#!/usr/bin/env python3
"""On-demand orchestrator: apply everything new in MYOB since
config/last_migration_date.txt into Manager, safely and idempotently.

IMPORTANT — before touching any of this, confirm with the human TWO
separate things, not one: (1) that every MYOB bank transaction up to the
target cutoff has already been categorised/matched in MYOB's own "Bank
transactions" screen (no lines left "Unmatched"), and (2) that every bank
transaction already imported into Manager's own bank feed has likewise
been categorised/matched there, as far as possible. **Ask both
explicitly, every run — do not assume either, and do not infer one from
the other.** Confirmed 2026-08-13: an uncategorised MYOB bank line can
carry a different date than its own journal record (MYOB's accrual-side
journal vs its bank feed's cash-clearing date can genuinely disagree for
one transaction, e.g. a superannuation payment journalled 30/6 that the
real bank statement clears 1/7) -- this surfaces as a false-looking
Manager-vs-MYOB reconciliation gap that isn't actually about Manager at
all, and wastes real investigation time chasing it (see
delta-migration.md's incident writeup). An uncategorised *Manager* bank
line is the mirror-image risk: it's a real, ledger-counted amount sitting
unlinked (Suspense-parked), but with no invoice/contact link it can only
be matched against MYOB by raw amount/date, producing the same kind of
false gap from the other direction. Running the delta against either
system with unresolved unmatched bank lines risks migrating on top of
exactly this kind of still-moving data.

This script does NOT harvest from MYOB itself. Confirmed 2026-08-12: this
business's MYOB session expires within roughly a minute of a fresh script
start, sometimes faster. Harvesting is therefore a separate prerequisite,
run via a Playwright harvester after logging in:

    python3 scripts/myob_playwright/download_bills.py login
    python3 scripts/myob_playwright/download_bills.py harvest
    python3 scripts/myob_playwright/download_bills.py download
    python3 scripts/myob_playwright/download_invoices.py harvest
    python3 scripts/myob_playwright/download_invoices.py download
    python3 scripts/myob_playwright/download_journals.py --fy <current FY>
    python3 scripts/myob_playwright/download_bank.py --from-date <last cutoff>

`download_bills.py login` needs an interactive human (MFA, business
confirmation). If MYOB_USERNAME/MYOB_PASSWORD/MYOB_TOTP_SECRET are present
in the project .env, `download_bills.py login-auto` logs in
non-interactively instead (confirmed working 2026-08-14) -- use only for
read-only harvesting/export, same credential-handling care as any other
live-credential automation. IMPORTANT when harvesting the *current, still-
open* fiscal year: always pass an explicit `--date-to` to
`download_journals.py` pinned to that FY's own end date (e.g.
`--date-to 2026-06-30` for `--fy 2026` once that year has closed) --
leaving it to default to today silently overlaps the next FY's own
already-harvested file and double-counts every transaction in the overlap
window (see myob-to-manager-migration SKILL.md's Hard-won facts).

(each of the above may itself need a fresh `login` first if the session
has expired since the last one -- check the printed output).

Once that's done, THIS script is fully unattended -- no more MYOB
interaction, only the already-harvested files under exports/myob/ and
Manager's own REST API:

    1. filter_delta.py-style dedup against live Manager (read-only)
    2. apply_bills_invoices.py  -- create new Purchase/Sales Invoices
    3. link_payments.py         -- link matching Suspense-parked bank
                                    Payments to the invoices just created
    4. apply_journals.py        -- create new standalone Journal Entries
                                    (BAS/FBT/depreciation/etc)
    5. Re-run the dedup filter  -- confirm zero new candidates remain
    6. Run scripts/reconciliation_dashboard.py (project-specific, optional)
                                 -- confirm every account it checks is
                                    clean too, not just the delta-apply
                                    candidates (see Golden Rule 9, SKILL.md)
    7. Advance config/last_migration_date.txt (only if BOTH 5 and 6 pass)

Snapshots the Manager business file before any writes (Golden Rule 5,
manager-automation SKILL.md).

Usage:
  python3 scripts/myob_delta/delta_migrate.py                # dry-run (default)
  python3 scripts/myob_delta/delta_migrate.py --apply
  python3 scripts/myob_delta/delta_migrate.py --apply --skip-journals
  python3 scripts/myob_delta/delta_migrate.py --apply --skip-payment-links
  python3 scripts/myob_delta/delta_migrate.py --apply --cutover-today
      # advance the cutoff to today instead of yesterday -- only pass this
      # when a human explicitly wants that for this specific run; never
      # the default (per project decision 2026-08-12: same-day MYOB
      # transactions are still landing, so the default stays one day
      # behind real time as a safety margin).
  python3 scripts/myob_delta/delta_migrate.py --apply --cutover-date 2026-08-11
      # advance to a specific date instead of yesterday/today -- e.g. a
      # human names the exact date they've confirmed MYOB is fully
      # matched/categorised through (see Golden Rule 9, SKILL.md).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# SKILL_SCRIPTS locates sibling modules (and sibling scripts this file
# subprocess-invokes) at this file's real (post-symlink) location; ROOT is
# the host project's root (data files), found via cwd -- see
# filter_delta.py's header comment for the full explanation.
SKILL_SCRIPTS = Path(__file__).resolve().parents[1]
MANAGER_AUTOMATION_SCRIPTS = SKILL_SCRIPTS.parent.parent / "manager-automation" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS / "myob_playwright"))
sys.path.insert(0, str(MANAGER_AUTOMATION_SCRIPTS))
# This script's own prints interleave with its subprocess children's --
# force line-buffering so console/log output stays in the right order
# instead of batching until a buffer flush (cosmetic-only, confirmed
# 2026-08-12: no logic bug, just confusing read order under a pipe).
sys.stdout.reconfigure(line_buffering=True)
import lib_manager_api as API  # noqa: E402
import manager_index as MI  # noqa: E402
import filter_delta as FD  # noqa: E402

ROOT = Path.cwd()
LAST_MIGRATION_FILE = ROOT / "config" / "last_migration_date.txt"
BUSINESS_NAME_FILE = ROOT / "config" / "manager_business_name.txt"


def run(script: str, *extra_args: str) -> int:
    cmd = [sys.executable, str(SKILL_SCRIPTS / "myob_delta" / script), *extra_args]
    print(f"\n{'=' * 70}\n$ {' '.join(cmd)}\n{'=' * 70}")
    result = subprocess.run(cmd)
    return result.returncode


def remaining_candidates(after: str) -> tuple[int, int, int]:
    """(new_bills, new_invoices, new_journals) still outstanding -- used
    both for the pre-flight report and the post-apply clean-pass check."""
    api = API.ManagerAPI()
    idx = MI.build_index(api)
    markers = MI.build_journal_index(api)
    new_bills = FD.find_new_bills(idx, after)
    new_invoices = FD.find_new_invoices(idx, after)
    new_journals = FD.find_new_journals(markers, after)
    return len(new_bills), len(new_invoices), len(new_journals)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="perform real writes (default: dry-run)")
    ap.add_argument("--skip-journals", action="store_true")
    ap.add_argument("--skip-payment-links", action="store_true")
    ap.add_argument("--cutover-today", action="store_true",
                     help="advance last_migration_date to today instead of yesterday -- explicit opt-in only")
    ap.add_argument("--cutover-date", default=None,
                     help="YYYY-MM-DD -- advance last_migration_date to this exact date instead of "
                          "yesterday/today, e.g. when a human names a specific date they've confirmed "
                          "MYOB is fully matched/categorised through. Must not be in the future.")
    ap.add_argument("--after-date", default=None)
    args = ap.parse_args()

    if args.cutover_date:
        if date.fromisoformat(args.cutover_date) > date.today():
            raise SystemExit(f"[error] --cutover-date {args.cutover_date} is in the future -- refusing")

    after = args.after_date or FD.last_migration_date()
    print(f"[info] delta cutoff: issue_date > {after}")

    nb, ni, nj = remaining_candidates(after)
    print(f"[info] outstanding before this run: {nb} bills, {ni} invoices, {nj} journals")

    if args.apply and (nb or ni):
        print("\n[info] snapshotting Manager business file before writes...")
        business_name = BUSINESS_NAME_FILE.read_text().strip()
        snap = subprocess.run(
            [sys.executable, str(MANAGER_AUTOMATION_SCRIPTS / "backup_manager_business.py"),
             "--business", business_name, "--label", f"delta-migrate-{date.today().isoformat()}"]
        )
        if snap.returncode != 0:
            print("[error] snapshot failed -- aborting rather than writing without a backup")
            return 1

    apply_flag = ["--apply"] if args.apply else []

    rc = run("apply_bills_invoices.py", *apply_flag, "--after-date", after)
    if rc != 0:
        print("[error] apply_bills_invoices.py failed -- stopping pipeline")
        return rc

    if not args.skip_payment_links:
        rc = run("link_payments.py", *apply_flag, "--after-date", after)
        if rc != 0:
            print("[error] link_payments.py failed -- stopping pipeline")
            return rc
    else:
        print("\n[skip] payment linking (--skip-payment-links)")

    if not args.skip_journals:
        rc = run("apply_journals.py", *apply_flag, "--after-date", after)
        if rc != 0:
            print("[error] apply_journals.py failed -- stopping pipeline")
            return rc
    else:
        print("\n[skip] journal apply (--skip-journals)")

    if not args.apply:
        print("\n[dry-run] no writes made -- pass --apply to run for real")
        return 0

    print(f"\n{'=' * 70}\nPost-apply verification\n{'=' * 70}")
    nb2, ni2, nj2 = remaining_candidates(after)
    print(f"[info] outstanding after this run: {nb2} bills, {ni2} invoices"
          + ("" if args.skip_journals else f", {nj2} journals"))

    clean = nb2 == 0 and ni2 == 0 and (args.skip_journals or nj2 == 0)
    if not clean:
        print("\n[NOT CLEAN] some candidates remain unresolved (see apply output above for why) -- "
              "config/last_migration_date.txt NOT advanced. Investigate, then re-run.")
        return 1

    # Second gate, separate from the bills/invoices/journals check above:
    # the reconciliation dashboard checks a broader set of accounts (bank,
    # GST, AP, AR, Retained Earnings, Director Advances) that
    # remaining_candidates() never touches. Per explicit project decision
    # 2026-08-15: last_migration_date.txt must not advance until the
    # dashboard itself is fully clear, not just until the delta apply
    # scripts report nothing new to create. This is a project-specific
    # script (config/config.py's own ACCOUNTS list is tailored to this
    # business), not a skill file, so it's optional -- skip gracefully if
    # a project hasn't built one, falling back to the check above alone.
    dashboard_script = ROOT / "scripts" / "reconciliation_dashboard.py"
    if dashboard_script.is_file():
        print(f"\n{'=' * 70}\n$ {sys.executable} {dashboard_script} --after-date {after}\n{'=' * 70}")
        dash = subprocess.run([sys.executable, str(dashboard_script), "--after-date", after])
        if dash.returncode != 0:
            print("\n[NOT CLEAN] reconciliation dashboard still shows a real diff on at least one "
                  "account (see its own output above) -- config/last_migration_date.txt NOT advanced. "
                  "Investigate and regenerate the dashboard clean, then re-run.")
            return 1
    else:
        print(f"\n[info] no {dashboard_script} found -- skipping the dashboard gate "
              "(project hasn't built one; only the bills/invoices/journals check above applies)")

    if args.cutover_date:
        new_cutoff = date.fromisoformat(args.cutover_date)
        cutoff_note = f"  (explicit --cutover-date {args.cutover_date})"
    elif args.cutover_today:
        new_cutoff = date.today()
        cutoff_note = ""
    else:
        new_cutoff = date.today() - timedelta(days=1)
        cutoff_note = "  (yesterday, per project default -- pass --cutover-today or --cutover-date for something else)"
    old_cutoff = FD.last_migration_date()
    LAST_MIGRATION_FILE.write_text(new_cutoff.isoformat() + "\n")
    print(f"\n[ok] clean pass -- config/last_migration_date.txt advanced: {old_cutoff} -> {new_cutoff.isoformat()}{cutoff_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
