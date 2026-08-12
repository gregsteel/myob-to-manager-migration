#!/usr/bin/env python3
"""Link existing, unlinked ("Suspense", Account=None) Manager bank Payments
to the Purchase Invoices apply_bills_invoices.py just created for the MYOB
delta, and reconcile-check the result.

Why this exists: applying a batch of new Purchase Invoices for bills MYOB
already shows as paid does NOT double-post the expense (confirmed by direct
check -- the matching bank Payments already in Manager have Account=None,
i.e. still raw/uncategorized from the bank feed, never coded anywhere) but
it does leave those Payments unlinked and the new PIs showing open/unpaid
in AP. This script closes that gap the same explicit way
apply_bills_invoices.py's ATO-bill payment was linked by hand: GET the
Payment form, set Account=builtin AP + AccountsPayableSupplier +
PurchaseInvoice=the new PI's key, PUT the full form back. Never FIFO
cascade (manager-automation invoice-linking.md's documented
overpayment-risk pattern).

Matching: exact amount, Account is currently None (Suspense), date within
+/- DATE_WINDOW_DAYS of the bill's issue_date. Some bill totals collide
(confirmed duplicates in this batch: $79.99 x2, $25.99 x2, $173.00 x2) --
when more than one Suspense payment matches an amount, every candidate is
listed with its date-distance from the bill and nothing is auto-applied
for that bill; resolve manually with --force-pair.

Usage:
  python3 scripts/myob_delta/link_payments.py                     # dry-run
  python3 scripts/myob_delta/link_payments.py --apply              # real writes, skips ambiguous
  python3 scripts/myob_delta/link_payments.py --apply \\
      --force-pair 00001056:<payment_key>                         # resolve one ambiguous case
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# SKILL_SCRIPTS locates sibling modules at this file's real (post-symlink)
# location; ROOT is the host project's root (data files), found via cwd --
# see filter_delta.py's header comment for the full explanation.
SKILL_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_SCRIPTS / "myob_playwright"))
sys.path.insert(0, str(SKILL_SCRIPTS.parent.parent / "manager-automation" / "scripts"))
import lib_manager_api as API  # noqa: E402
import manager_index as MI  # noqa: E402
import filter_delta as FD  # noqa: E402

ROOT = Path.cwd()

BILLS_DIR = ROOT / "exports" / "myob" / "bills" / "by_bill"
BUILTIN_AP = "dac7ba37-0ccd-45e5-906e-548e6c50df37"
DATE_WINDOW_DAYS = 45


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def find_suspense_payments(api: API.ManagerAPI, after_date: str) -> list[dict]:
    """Payments dated after `after_date` whose only line has Account=None
    (raw, uncategorized -- i.e. still sitting in Suspense). Filters on the
    cheap list-level `date` field first so only the recent subset needs a
    per-record get_form -- a full-history scan is ~4000 records and times
    out (confirmed 2026-08-12); the delta this script cares about is only
    ever recent."""
    out = []
    all_payments = api.list_all("payment")
    recent = [p for p in all_payments if (p.get("date") or "") > after_date]
    print(f"[info] {len(all_payments)} total payments, {len(recent)} dated after {after_date} -- checking those")
    for p in recent:
        form = api.get_form("payment", p["key"])
        lines = form.get("Lines", [])
        if len(lines) == 1 and lines[0].get("Account") is None:
            out.append({
                "key": p["key"],
                "date": form.get("Date", "")[:10],
                "amount": lines[0].get("Amount"),
                "description": form.get("Description", ""),
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-pair", action="append", default=[],
                     help="bill_number:payment_key, resolves one ambiguous match")
    ap.add_argument("--after-date", default=None)
    args = ap.parse_args()
    force_pairs = dict(p.split(":", 1) for p in args.force_pair)

    api = API.ManagerAPI()
    after = args.after_date or FD.last_migration_date()
    idx = MI.build_index(api)

    print("[info] scanning recent live Payments for unlinked (Suspense) ones...")
    suspense = find_suspense_payments(api, after_date=after)
    print(f"[info] {len(suspense)} unlinked Payments found")

    supplier_cache: dict[str, str] = {}
    linked = skipped_ambiguous = skipped_no_match = skipped_already = 0

    # --- Pass 1: gather every (bill, candidate-payment, distance) triple.
    # Two different bills can genuinely share an amount (confirmed:
    # recurring subscriptions like TPG $79.99/month, Fairfax $25.99/month --
    # each month is a separate bill with the same total). Picking a
    # candidate per-bill in isolation lets two bills both "claim" the same
    # single real Suspense payment, silently stealing it from whichever
    # bill it actually belongs to when applied (confirmed: 00001070's Aug
    # TPG bill and 00001056's Jul TPG bill both matched the Jul payment
    # before this fix). Instead: collect all triples, then assign
    # payments to bills globally, smallest date-distance first, so each
    # Suspense payment is claimed by at most one bill.
    triples = []  # (distance, bill_row, pi, payment)
    no_candidates = []
    for row in FD.candidate_bill_rows(after):
        bn = row["bill_number"]
        m = MI.match_bill(idx, bill_number=bn, issue_date=row["issue_date"])
        pi = m["purchase"]
        if pi is None:
            continue  # not created yet -- apply_bills_invoices.py handles that
        if pi["already_paid"]:
            skipped_already += 1
            continue

        target_amount = pi["amount"]
        candidates = [s for s in suspense if abs(s["amount"] - target_amount) < 0.01]
        if bn in force_pairs:
            candidates = [s for s in candidates if s["key"] == force_pairs[bn]]

        if not candidates:
            no_candidates.append((bn, target_amount))
            continue

        bill_date = _d(row["issue_date"])
        for c in candidates:
            dist = abs((_d(c["date"]) - bill_date).days)
            if dist <= DATE_WINDOW_DAYS:
                triples.append((dist, row, pi, c))

    # --- Pass 2: greedy global assignment, smallest distance first.
    triples.sort(key=lambda t: t[0])
    claimed_payment_keys: set[str] = set()
    claimed_bill_numbers: set[str] = set()
    assignments = []
    contested: dict[str, list] = {}
    for dist, row, pi, payment in triples:
        bn = row["bill_number"]
        if bn in claimed_bill_numbers:
            continue
        if payment["key"] in claimed_payment_keys:
            contested.setdefault(bn, []).append((dist, payment))
            continue
        claimed_payment_keys.add(payment["key"])
        claimed_bill_numbers.add(bn)
        assignments.append((row, pi, payment, dist))

    for bn, target_amount in no_candidates:
        print(f"[no-match] {bn}  ${target_amount:,.2f}  -- no unlinked Payment of this amount found")
        skipped_no_match += 1
    for bn, alts in contested.items():
        if bn in claimed_bill_numbers:
            continue  # got a different, unclaimed candidate instead
        print(f"[ambiguous] {bn}  -- every candidate was already claimed by a closer-matching bill, "
              f"pass --force-pair {bn}:<key> to override:")
        for dist, payment in sorted(alts):
            print(f"             key={payment['key']}  date={payment['date']} (±{dist}d)  {payment['description'][:60]}")
        skipped_ambiguous += 1

    for row, pi, payment, dist in sorted(assignments, key=lambda a: a[0]["bill_number"]):
        bn = row["bill_number"]
        target_amount = pi["amount"]

        folder = BILLS_DIR / row["folder"].split("/", 1)[1]
        bill = json.loads((folder / "bill.json").read_text())["bill"]
        supplier_name = bill["supplier"]["name"]
        if supplier_name not in supplier_cache:
            r = api.get("/suppliers", pageSize=5, term=supplier_name)
            hit = next((s for s in r["suppliers"] if s["name"] == supplier_name), None)
            supplier_cache[supplier_name] = hit["key"] if hit else None
        supplier_key = supplier_cache[supplier_name]
        if not supplier_key:
            print(f"[skip] {bn}: no Supplier key for {supplier_name!r}")
            continue

        print(f"{'[APPLY]' if args.apply else '[DRY-RUN]'} {bn}  ${target_amount:,.2f}  "
              f"-> payment {payment['key']} dated {payment['date']} (±{dist}d)  {payment['description'][:50]}")

        if args.apply:
            form = api.get_form("payment", payment["key"])
            form["Lines"][0]["Account"] = BUILTIN_AP
            form["Lines"][0]["AccountsPayableSupplier"] = supplier_key
            form["Lines"][0]["PurchaseInvoice"] = pi["key"]
            api.put_form("payment", payment["key"], form)
            # Re-verify live, per invoice-linking.md's "re-fetch, don't trust a snapshot" rule.
            check = api.get("/purchase-invoices", pageSize=1, term=pi["reference"])
            new_balance = check["purchaseInvoices"][0]["balanceDue"]["value"]
            status = "OK" if new_balance == 0 else f"STILL DUE ${new_balance:,.2f}"
            print(f"           -> linked, balanceDue now {status}")
        linked += 1

    print(f"\n[summary] linked={linked}  ambiguous={skipped_ambiguous}  "
          f"no_match={skipped_no_match}  already_paid={skipped_already}")
    if not args.apply:
        print("[dry-run] no writes made -- pass --apply to link these")
    return 0


if __name__ == "__main__":
    sys.exit(main())
