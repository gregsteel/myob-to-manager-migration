#!/usr/bin/env python3
"""Harvest MYOB's bank-feed transaction list via Playwright BFF (free, no
paid API) -- confirmed working 2026-08-13. Reuses download_bills.py's
session (exports/myob/bills/state/storage.json), same as download_invoices.py.

Long documented as a planned capability (manager-automation SKILL.md's
"When to use which tool" table cites `download_bank.py` by name) but never
actually built until now -- same "documented but never built" pattern as
several other scripts this session (`apply_manager_api.py`, the original
`attach_purchase_images.py`'s `lib_myob` dependency).

**Why this matters, and what it fixes**: the reconciliation dashboard
previously compared Manager's full bank ledger against MYOB's *journal
export* for bank-account codes (1-1110/1-1120) -- but MYOB's journal
export only contains transactions that have already been categorized/
posted. An MYOB bank line still sitting uncategorized in MYOB's own
"Bank transactions" screen genuinely exists on both sides but would never
appear in the journal export at all, making it look like a Manager-only
transaction and manufacturing a false bank-account "gap". Confirmed
2026-08-13 on a real example (a $173.00 Telstra payment) before this
harvester existed to prove it. This harvester pulls MYOB's own bank feed
directly instead, which carries a genuine `status` field
("Approved"/"Unmatched") so uncategorized lines can be tagged and
excluded from the comparison, not misread as missing.

BFF endpoint (discovered 2026-08-13, one continuous exploration session
per delta-migration.md's session-fragility mitigation -- MYOB's side nav
"Banking" is a dropdown toggle, not a direct link; the real destination is
its "Bank transactions" sub-item):

    GET {BFF}/banking/load_bank_transactions
        ?transactionType=All&bankAccount=-1&keywords=
        &dateFrom=<YYYY-MM-DD>&dateTo=<YYYY-MM-DD>
        &period=Custom&sortOrder=desc&orderBy=Date
        &isSuggestedCategoryEnabled=true
        &offset=<n>                                    (0, 50, 100, ... -- 50/page)

Response shape (top-level `entries`, `pagination: {offset, hasNextPage}`,
`bankAccounts` for id->code/name mapping): each entry already carries full
detail, unlike bills/invoices which needed a separate detail-fetch pass --
no two-phase harvest needed here, one paginated crawl is enough. Per-entry
fields that matter:
  - `transactionId`/`transactionUid`, `bankAccountId`, `date`,
    `description`, `withdrawal` or `deposit` (one or the other, never both)
  - `status`: "Approved" (categorized/matched) or "Unmatched" (still
    sitting raw in MYOB's own feed, same concept as a Manager Suspense
    Payment/Receipt) -- confirmed both values live, see module docstring
  - `matchedJournals[]`: when Approved, the real join key -- `eventId`
    matches the Journal entries report's own `Ref no` column exactly (per
    manager-import.md's already-documented, previously-untested theory),
    plus `contactName`/`paymentSource[].sourceId` (the exact bill/invoice
    number paid)
  - `suggestedMatches[]`: present even when Unmatched -- MYOB's own guess,
    informational only, not a real match

Outputs (exports/myob/bank/):
    bank_transactions.jsonl   one JSON object per harvested transaction
    _index.tsv                human index: date, account, description,
                               amount, status, matched ref/contact

Usage:
  python3 scripts/myob_playwright/download_bank.py --from-date 2026-06-30
  python3 scripts/myob_playwright/download_bank.py --from-date 2015-06-30 --to-date 2026-08-13
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import download_bills as DB

ROOT = DB.ROOT
BANK_DIR = ROOT / "exports" / "myob" / "bank"
JSONL_PATH = BANK_DIR / "bank_transactions.jsonl"
INDEX_PATH = BANK_DIR / "_index.tsv"


def _iso(d: str) -> str:
    """Accept YYYY-MM-DD as-is."""
    datetime.strptime(d, "%Y-%m-%d")
    return d


def cmd_download(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    BANK_DIR.mkdir(parents=True, exist_ok=True)
    date_from = args.from_date
    date_to = args.to_date or date.today().isoformat()

    all_entries: list[dict] = []
    bank_accounts: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = DB._browser(p, headless=not args.headed)
        context = DB._context(browser)
        page = context.new_page()

        raw_pages = []

        def on_response(response):
            # Confirmed 2026-08-13: the FIRST page load uses
            # load_bank_transactions; every subsequent page (triggered by
            # clicking "Load more") uses a DIFFERENT endpoint name,
            # filter_bank_transactions, same query shape + offset. Direct
            # replay of either endpoint via page.request.get() or an
            # in-page fetch() both 401 -- the BFF needs a bearer token the
            # SPA's own request-issuing code attaches that neither of
            # those replicates. Real UI clicks are the only reliable path;
            # don't try to route around this again.
            if "load_bank_transactions" in response.url or "filter_bank_transactions" in response.url:
                try:
                    raw_pages.append(response.json())
                except Exception:
                    pass

        page.on("response", on_response)

        page.goto(DB.START_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        if "id.myob.com" in page.url:
            raise SystemExit("[error] session expired -- run download_bills.py login")

        # Navigate: Banking (dropdown toggle, not a direct link) -> Bank
        # transactions (the real destination).
        page.get_by_text(re.compile(r"^Banking$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(800)
        page.get_by_text(re.compile(r"^Bank transactions$", re.I)).first.click(timeout=8000)
        page.wait_for_timeout(3000)

        # The page's own Start/End date filter inputs don't reliably
        # trigger a reload when filled programmatically (tried fill+Tab+Enter,
        # no new request fired) -- rather than fight that, just click
        # "Load more" repeatedly (extends MYOB's own default "Last 3
        # months" window further into the past each time) until coverage
        # reaches back past `date_from`, then trim in Python below. Capped
        # to avoid an infinite loop if the date range requested is
        # implausibly old relative to how much history MYOB actually has.
        max_clicks = 60
        for _ in range(max_clicks):
            earliest = min((e["date"][:10] for pg in raw_pages for e in pg.get("entries", [])), default=None)
            if earliest is not None and earliest <= date_from:
                break
            load_more = page.get_by_text(re.compile(r"^Load more$", re.I))
            if load_more.count() == 0:
                break  # MYOB has no more history to give
            try:
                load_more.first.click(timeout=5000, force=True)
            except Exception:
                break
            page.wait_for_timeout(2500)
            print(f"[info] {sum(len(pg.get('entries', [])) for pg in raw_pages)} transactions loaded so far, "
                  f"earliest date {earliest}...")

        browser.close()

    for pg in raw_pages:
        all_entries.extend(pg.get("entries", []))
        for a in pg.get("bankAccounts", []):
            bank_accounts[a["id"]] = a

    # De-dupe (Load more can re-fetch an overlapping row if a page boundary
    # shifts between clicks) and trim to the requested [date_from, date_to].
    seen: set[str] = set()
    deduped = []
    for e in all_entries:
        uid = e.get("transactionUid") or e.get("transactionId")
        if uid in seen:
            continue
        seen.add(uid)
        if date_from <= e.get("date", "")[:10] <= date_to:
            deduped.append(e)
    all_entries = deduped

    print(f"[ok] harvested {len(all_entries)} bank transactions, {date_from} -> {date_to}")

    with JSONL_PATH.open("w", encoding="utf-8") as f:
        for e in all_entries:
            f.write(json.dumps(e) + "\n")

    with INDEX_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["date", "account_code", "account_name", "description", "withdrawal", "deposit",
                    "status", "matched_ref", "matched_contact"])
        for e in all_entries:
            acct = bank_accounts.get(e.get("bankAccountId"), {})
            matched = e.get("matchedJournals") or []
            matched_ref = ";".join(m.get("eventId", "") for m in matched)
            matched_contact = ";".join(m.get("contactName", "") for m in matched)
            w.writerow([
                e.get("date", "")[:10], acct.get("displayId", ""), acct.get("displayName", ""),
                e.get("description", ""), e.get("withdrawal", ""), e.get("deposit", ""),
                e.get("status", ""), matched_ref, matched_contact,
            ])

    unmatched = sum(1 for e in all_entries if e.get("status") != "Approved")
    print(f"[ok] wrote {JSONL_PATH} and {INDEX_PATH}")
    print(f"[info] {unmatched} of {len(all_entries)} transactions not Approved (still uncategorized in MYOB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-date", required=True, type=_iso, help="YYYY-MM-DD")
    ap.add_argument("--to-date", type=_iso, default=None, help="YYYY-MM-DD, default today")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    cmd_download(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
