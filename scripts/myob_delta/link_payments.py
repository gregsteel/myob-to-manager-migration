#!/usr/bin/env python3
"""MYOB-delta-specific candidate sourcing for linking unlinked bank
Payments/Receipts to the Purchase Invoices / Debit Notes / Sales Invoices
apply_bills_invoices.py just created -- the actual matching/linking engine
lives in manager-automation's `link_open_invoices.py` (generic, no MYOB
knowledge at all) and is imported here, not reimplemented. Refactored
2026-08-13: this file used to duplicate that engine (find_suspense_records,
generate_combos, greedy_assign, the field-setting logic) alongside
categorize_bank_payments.py, which did the same generic job for
non-migration bookkeeping. One canonical engine now serves both.

Why this exists: applying a batch of new Purchase Invoices for bills MYOB
already shows as paid does NOT double-post the expense (confirmed by direct
check -- the matching bank Payments already in Manager have Account=None,
i.e. still raw/uncategorized from the bank feed, never coded anywhere) but
it does leave those Payments unlinked and the new PIs showing open/unpaid
in AP. This script closes that gap.

**What's actually MYOB-specific here** (the reason this file still exists
separately from the generic engine): candidate *targets* aren't discovered
live from Manager's own open-invoice lists the way link_open_invoices.py's
own CLI does it -- they're cross-referenced against what's new in the MYOB
delta (`filter_delta.candidate_bill_rows()`/`candidate_invoice_rows()` +
`manager_index.match_bill()`/`match_invoice()`), and supplier/customer
names are resolved from the harvested `bill.json`/`invoice.json` files, not
from Manager's own `supplier`/`customer` field on the target record (which
would also work generically, but this project's delta pipeline already has
the harvest data on hand and it's the authoritative source during
migration).

**Debit Notes settle via Receipts, not Payments** -- a negative-total bill
(supplier refund/credit) is created as a Debit Note (see
apply_bills_invoices.py / delta-migration.md), and the money for it comes
*in*, not out, so the matching unlinked bank line is a Receipt
(`Account=None`), not a Payment. The link_open_invoices.link_ap() field
shape (same `PurchaseInvoice` field) applies to a Debit Note's key too --
there is no separate `DebitNote` field (tried; silently dropped, confirmed
via offline SQLite protobuf inspection). Debit Notes and Sales Invoices
share the same Receipt pool, so their combos are assigned together in one
greedy_assign call -- keeps a Receipt from being claimed by both a
debit-note-settlement and a sales-invoice-payment match.

**Sales Invoices settle via Receipts too, and the AR link field IS
writable** -- see link_open_invoices.py's own docstring and
manager-automation SKILL.md's corrected Hard-won fact for the full
"SalesInvoice vs AccountsReceivableSalesInvoice" incident.

Matching: exact amount, Account is currently None (Suspense), date within
+/- DATE_WINDOW_DAYS of the bill/invoice's issue_date, up to
GROUP_SIZE_MAX Suspense records summed together (a foreign-currency charge
posts as two separate bank lines -- the charge plus a separate
transaction-fee line -- that only sum to the target amount together; see
link_open_invoices.py's docstring). One shared greedy assignment across
every combo found, so two different bills/invoices sharing an amount
(recurring subscriptions) don't both claim the same Suspense record.

Usage:
  python3 scripts/myob_delta/link_payments.py                     # dry-run
  python3 scripts/myob_delta/link_payments.py --apply              # real writes, skips ambiguous
  python3 scripts/myob_delta/link_payments.py --apply \\
      --force-pair 00001056:<payment_key>                         # resolve one ambiguous case
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
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
import link_open_invoices as LOI  # noqa: E402

ROOT = Path.cwd()

BILLS_DIR = ROOT / "exports" / "myob" / "bills" / "by_bill"
INVOICES_DIR = ROOT / "exports" / "myob" / "invoices" / "by_invoice"


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-pair", action="append", default=[],
                     help="bill_number:record_key, resolves one ambiguous match")
    ap.add_argument("--after-date", default=None)
    args = ap.parse_args()
    force_pairs = dict(p.split(":", 1) for p in args.force_pair)

    api = API.ManagerAPI()
    after = args.after_date or FD.last_migration_date()
    idx = MI.build_index(api)

    print("[info] scanning recent live Payments for unlinked (Suspense) ones...")
    suspense_payments = LOI.find_suspense_records(api, "payment", after_date=after)
    print(f"[info] {len(suspense_payments)} unlinked Payments found")

    print("[info] scanning recent live Receipts for unlinked (Suspense) ones...")
    suspense_receipts = LOI.find_suspense_records(api, "receipt", after_date=after)
    print(f"[info] {len(suspense_receipts)} unlinked Receipts found")

    party_cache: dict[str, str | None] = {}
    row_by_key: dict[str, tuple] = {}  # our claim-key -> (row, direction)
    linked = skipped_ambiguous = skipped_no_match = skipped_already = 0

    # --- Gather candidates across all three directions: positive bills
    # (Purchase Invoices) settle via Payments; negative bills (Debit Notes)
    # and Sales Invoices both settle via Receipts. Candidates are gathered
    # for every bill/invoice first and assigned globally (LOI.greedy_assign),
    # not claimed in isolation (confirmed regression otherwise: two
    # different bills sharing an amount both matched the same Suspense
    # payment before this fix).
    pi_flat, receipt_flat = [], []
    no_candidates = []  # (claim_key, target_amount, kind)
    for row in FD.candidate_bill_rows(after):
        bn = row["bill_number"]
        m = MI.match_bill(idx, bill_number=bn, issue_date=row["issue_date"])
        bill_date = _d(row["issue_date"])

        if m["purchase"] is not None:
            pi = m["purchase"]
            if pi["already_paid"]:
                skipped_already += 1
                continue
            row_by_key[bn] = (row, "ap")
            combos = LOI.generate_combos(bill_date, pi["amount"], suspense_payments, force_pairs.get(bn))
            if not combos:
                no_candidates.append((bn, pi["amount"], "Payment"))
                continue
            pi_flat.extend((score, bn, pi, combo) for score, combo in combos)
        elif m["debit_note"] is not None:
            dn = m["debit_note"]
            row_by_key[bn] = (row, "debit_note")
            combos = LOI.generate_combos(bill_date, dn["amount"], suspense_receipts, force_pairs.get(bn))
            if not combos:
                no_candidates.append((bn, dn["amount"], "Receipt"))
                continue
            receipt_flat.extend((score, bn, dn, combo) for score, combo in combos)
        # else: not created yet -- apply_bills_invoices.py handles that

    for row in FD.candidate_invoice_rows(after):
        num = row["number"]
        key = f"SI:{num}"
        m = MI.match_invoice(idx, number=num, issue_date=row["issue_date"])
        sale = m["sale"]
        if sale is None:
            continue  # not created yet -- apply_bills_invoices.py handles that
        if sale["already_paid"]:
            skipped_already += 1
            continue
        row_by_key[key] = (row, "ar")
        inv_date = _d(row["issue_date"])
        combos = LOI.generate_combos(inv_date, sale["amount"], suspense_receipts, force_pairs.get(key))
        if not combos:
            no_candidates.append((key, sale["amount"], "Receipt"))
            continue
        receipt_flat.extend((score, key, sale, combo) for score, combo in combos)

    pi_assignments, pi_contested = LOI.greedy_assign(pi_flat)
    receipt_assignments, receipt_contested = LOI.greedy_assign(receipt_flat)
    pi_claimed = {bn for bn, _, _, _ in pi_assignments}
    receipt_claimed = {bn for bn, _, _, _ in receipt_assignments}

    for bn, target_amount, kind in no_candidates:
        print(f"[no-match] {bn}  ${target_amount:,.2f}  -- no combination of unlinked {kind}s "
              f"(up to {LOI.GROUP_SIZE_MAX}) sums to this amount")
        skipped_no_match += 1

    for contested, claimed in ((pi_contested, pi_claimed), (receipt_contested, receipt_claimed)):
        for bn, alts in contested.items():
            if bn in claimed:
                continue  # got a different, unclaimed combo instead
            print(f"[ambiguous] {bn}  -- every candidate combo was already claimed by a "
                  f"closer-matching bill/invoice, pass --force-pair {bn}:<key> to override:")
            for score, combo in sorted(alts):
                desc = " + ".join(f"{c['description'][:30]}(${c['amount']:.2f})" for c in combo)
                print(f"             size={score[0]} dist={score[1]}  {desc}")
            skipped_ambiguous += 1

    def apply_assignment(claim_key: str, target: dict, combo: list[dict], *, kind_label: str) -> None:
        nonlocal linked
        row, direction = row_by_key[claim_key]

        if direction == "ar":
            folder = INVOICES_DIR / row["folder"].split("/", 1)[1]
            doc = json.loads((folder / "invoice.json").read_text())["invoice"]
            party_name = doc["customer"]["name"]
            party_key = LOI.resolve_party(api, party_cache, party_name, "customer")
            entity = "receipt"
        else:
            folder = BILLS_DIR / row["folder"].split("/", 1)[1]
            doc = json.loads((folder / "bill.json").read_text())["bill"]
            party_name = doc["supplier"]["name"]
            party_key = LOI.resolve_party(api, party_cache, party_name, "supplier")
            entity = "payment" if direction == "ap" else "receipt"

        if not party_key:
            print(f"[skip] {claim_key}: no {'Customer' if direction == 'ar' else 'Supplier'} key for {party_name!r}")
            return

        tag = "" if len(combo) == 1 else f"  ({len(combo)} {kind_label}s)"
        combo_desc = "; ".join(
            f"{c['key']} dated {c['date']} ${c['amount']:.2f} {c['description'][:40]}" for c in combo
        )
        print(f"{'[APPLY]' if args.apply else '[DRY-RUN]'} {claim_key} [{kind_label}]  "
              f"${target['amount']:,.2f}{tag}  -> {combo_desc}")

        if args.apply:
            for rec in combo:
                LOI.link(api, direction, entity, rec["key"], party_key, target["key"])
            print(f"           -> {LOI.verify_balance(api, direction, target['key'], target['reference'])}")
        linked += 1

    for bn, pi, combo, score in sorted(pi_assignments, key=lambda a: a[0]):
        apply_assignment(bn, pi, combo, kind_label="payment")
    for key, target, combo, score in sorted(receipt_assignments, key=lambda a: a[0]):
        apply_assignment(key, target, combo, kind_label="receipt")

    print(f"\n[summary] linked={linked}  ambiguous={skipped_ambiguous}  "
          f"no_match={skipped_no_match}  already_paid={skipped_already}")
    if not args.apply:
        print("[dry-run] no writes made -- pass --apply to link these")
    return 0


if __name__ == "__main__":
    sys.exit(main())
