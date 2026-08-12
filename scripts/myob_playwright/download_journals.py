#!/usr/bin/env python3
"""Download MYOB's Journal entries report (expanded layout) via Playwright,
automating the manual export process reference/runbook.md §2 documents.

Discovered 2026-08-12 (required 5 separate discovery passes due to MYOB's
very short-lived sessions on this business -- confirmed to expire within
roughly a minute of a fresh script start, sometimes faster):

  1. Reports live on a DIFFERENT subdomain (reports.myob.com), not
     app.myob.com -- direct URL guesses at app.myob.com bounce to
     Dashboard silently (no error). Reached only by clicking through the
     UI: side-nav "Reporting" -> "Reports" -> "Journal entries" link.
  2. The resulting URL is stable:
     https://reports.myob.com/#/au/{BUSINESS_ID}/journals
     but a *cold* page.goto() straight to it does not reliably carry the
     session over from a storage_state snapshot -- reaching it via the
     in-app click sequence from app.myob.com is what actually works.
  3. Date range: "Date from"/"Date to" are real labeled inputs, fillable
     directly like the bills/invoices flow.
  4. The report defaults to COLLAPSED (one row per transaction, Debit/
     Credit show the transaction total, no account breakdown) -- exactly
     the trap reference/runbook.md warns about for the manual process.
  5. "Customise" opens a dialog ("Select and reorder columns") that
     ALREADY lists the needed 5 columns (Ref no, Code, Category name,
     Debit ($), Credit ($)) by default -- but clicking its Apply button
     only configures which columns *would* show in expanded view, it does
     NOT switch the report into expanded view itself.
  6. The actual collapsed/expanded toggle is the separate "Expand all"
     button next to Customise. Only after clicking that does the export
     contain the real per-line detail (each transaction becomes a
     repeated "Ref no | Code | Category name | Debit | Credit" sub-table
     followed by a "Total amount" row -- exactly matching what
     build_journals.py expects to parse).
  7. "Export" reveals an Excel/PDF choice (not an immediate download);
     clicking "Excel" triggers the real file download.

Working sequence, confirmed end to end: START_URL -> click "Reporting" ->
click "Reports" -> click "Journal entries" -> fill Date from/Date to ->
click "Customise" -> click "Apply" -> click "Expand all" -> click
"Export" -> click "Excel" -> capture the download.

Output: exports/myob/journal_entries_FY<year>.xlsx (same filename
convention build_journals.py already expects -- no changes needed there).
Re-running for the same FY overwrites that FY's file with a fresh export,
matching how the manual process is meant to be re-run as more of the
current year accumulates.

SETUP: shares download_bills.py's session (login there first).

USAGE:
    python3 download_journals.py --date-from 2026-07-01 --date-to 2026-08-12 --fy 2027
    python3 download_journals.py --fy 2027     # defaults date-from to 1 Jul of FY start, date-to to today
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import download_bills as DB

OUT_DIR = DB.ROOT / "exports" / "myob"


def _au_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%d/%m/%Y")


def _fy_default_from(fy: int) -> str:
    # AU financial year FY<n> runs 1 Jul <n-1> -> 30 Jun <n>.
    return date(fy - 1, 7, 1).isoformat()


def cmd_download(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    if not DB.STORAGE.exists():
        raise SystemExit("[error] run: python3 download_bills.py login")

    date_from = args.date_from or _fy_default_from(args.fy)
    date_to = args.date_to or date.today().isoformat()
    out_path = OUT_DIR / f"journal_entries_FY{args.fy}.xlsx"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = DB._browser(p, headless=args.headless)
        context = DB._context(browser)
        page = context.new_page()

        page.goto(DB.START_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        DB._assert_logged_in(page)

        page.get_by_text(re.compile(r"^Reporting$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(800)
        page.get_by_text(re.compile(r"^Reports$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(2500)
        if "id.myob.com" in page.url:
            raise SystemExit("[error] session expired before reports list -- run login again")

        page.get_by_text(re.compile(r"^Journal entries$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(2500)
        if "id.myob.com" in page.url:
            raise SystemExit("[error] session expired at journals report -- run login again")
        print(f"[ok] at journals report: {page.url}")

        for label, value in [("Date from", _au_date(date_from)), ("Date to", _au_date(date_to))]:
            try:
                inp = page.get_by_label(re.compile(label, re.I))
                if inp.count():
                    inp.first.fill(value)
                    inp.first.press("Tab")
                else:
                    print(f"[warn] no input found for {label!r}")
            except Exception as e:
                print(f"[warn] couldn't set {label}: {e}")
        page.wait_for_timeout(1000)
        print(f"[ok] date range set: {date_from} -> {date_to}")

        page.get_by_text(re.compile(r"^Customise$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(1500)
        page.get_by_role("button", name=re.compile(r"^Apply$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(2000)

        expand_btn = page.get_by_text(re.compile(r"^Expand all$", re.I))
        if not expand_btn.count():
            raise SystemExit("[error] no 'Expand all' button found -- MYOB UI may have changed")
        expand_btn.first.click(timeout=8000)
        page.wait_for_timeout(2000)
        print("[ok] expanded view active")

        page.get_by_text(re.compile(r"^Export$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(1200)
        excel_opt = page.get_by_text(re.compile(r"^Excel$", re.I))
        if not excel_opt.count():
            raise SystemExit("[error] no Excel export option found")
        with page.expect_download(timeout=20000) as di:
            excel_opt.first.click(timeout=8000)
        download = di.value
        download.save_as(out_path)
        print(f"[ok] saved -> {out_path}")

        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fy", type=int, required=True, help="AU financial year end, e.g. 2027 for FY ending 30 Jun 2027")
    ap.add_argument("--date-from", default=None, help="ISO date, default 1 Jul of the FY")
    ap.add_argument("--date-to", default=None, help="ISO date, default today")
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    if args.headed:
        args.headless = False
    cmd_download(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
