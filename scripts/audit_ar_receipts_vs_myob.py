#!/usr/bin/env python3
"""Audit: does every individual MYOB payment record for every Sales Invoice
have a matching Manager Receipt/Journal Entry (same customer, date, amount)?

Root cause under investigation: Manager's builtin-AR cascade applies
whatever unlinked AR-crediting money exists, oldest-invoice-first, splitting
a receipt's amount across invoice boundaries when needed. This is
reasonable behavior -- but if some of MYOB's real payments were never
migrated as Manager Receipts at all (e.g. one leg of a split payment), the
cascade "solves" the wrong invoices using money that actually belongs to
later ones, silently pushing an equivalent shortfall onto the last
invoice(s) in a customer's queue. See docs/MIGRATION_DIFFS.md and
reference/invoice-linking.md for the Xinja IP Holdings worked example that
led to this script.

IMPORTANT -- a "missing" result from this script's first version turned out
to mean "not matched under any customer key", not "genuinely absent". A
real Manager Receipt can credit builtin AR with the exact right date and
amount but carry NO `AccountsReceivableCustomer` at all -- invisible to any
per-customer grouping. Some are also parked in a completely different
holding account (e.g. `9-9999 Bank transactions suspense`) with a
`LineDescription` literally saying `REVIEW customer unresolved: <ref>`. This
version checks both before reporting anything as missing. Creating a new
Receipt for a "missing" record that was actually one of these silently
double-counts real cash -- always resolve every candidate here before
reconstructing anything.

This script is read-only. It reports genuinely-missing payment records
plus separately reports "unresolved" ones that need a GET-merge-PUT fix
(add the customer link / fix the Account) rather than a new Receipt.

Skill-local, portable copy: expects `exports/myob/` and `out/manager/`
under the current working directory (run from the project root), not
relative to this file's own location. Depends on the sibling
`manager-automation` skill for `lib_manager_api.py` -- expects it at
`../../manager-automation/scripts` relative to this file, i.e. both
skills installed side by side under the same `skills/` parent.

Usage:
  python3 scripts/audit_ar_receipts_vs_myob.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manager-automation" / "scripts"))
import lib_manager_api as API  # noqa: E402

EXPORT_DIR = Path.cwd() / "exports" / "myob" / "invoices" / "by_invoice"
BUILTIN_AR = "d1489e95-bb28-4f5d-b42e-67d3291b3893"
AMOUNT_TOLERANCE = 0.01
REVIEW_RE = re.compile(r"REVIEW customer unresolved:\s*(\S+)", re.IGNORECASE)


def load_myob_payments():
    """Returns {customer_name: [{"invoice_ref": str, "date": str, "amount": float, "reference_no": str}]}"""
    by_customer = defaultdict(list)
    for d in sorted(EXPORT_DIR.iterdir()):
        f = d / "invoice.json"
        if not f.exists():
            continue
        data = json.loads(f.read_text())
        inv = data["invoice"]
        cust = inv["customer"]["name"]
        for p in inv.get("payments", []):
            by_customer[cust].append({
                "invoice_ref": inv["number"],
                "date": p["date"],
                "amount": p["amount"],
                "reference_no": p.get("reference_no"),
            })
    return by_customer


def load_manager_ar_records(api: API.ManagerAPI):
    """Scans every Receipt and Journal Entry business-wide once. Returns:
      by_customer_key: {customer_key: [record, ...]}   -- properly tagged
      untagged: [record, ...]                           -- builtin AR, Account correct, but no customer
      review_by_ref: {embedded_reference: record}       -- "REVIEW customer unresolved: X" on ANY account
    """
    def all_records(path, key):
        out = []
        skip = 0
        while True:
            r = api.get(path, pageSize=50, skip=skip)
            out.extend(r[key])
            skip += 50
            if skip >= r["totalRecords"]:
                break
        return out

    by_customer_key = defaultdict(list)
    untagged = []
    review_by_ref = {}

    def scan(kind, form, line, amount):
        desc = line.get("LineDescription") or ""
        review_match = REVIEW_RE.search(desc)
        record = {
            "kind": kind, "date": form["Date"][:10], "amount": amount,
            "form_key": form["Key"], "reference": form.get("Reference"),
            "account": line.get("Account"), "line_index": form["Lines"].index(line),
        }
        if review_match:
            review_by_ref[review_match.group(1)] = record
        elif line.get("Account") == BUILTIN_AR:
            cust_key = line.get("AccountsReceivableCustomer")
            if cust_key:
                by_customer_key[cust_key].append(record)
            else:
                untagged.append(record)

    receipts = all_records("/receipts", "receipts")
    print(f"[info] scanning {len(receipts)} receipts...")
    for i, rec in enumerate(receipts):
        form = api.get(f"/receipt-form/{rec['key']}")
        for line in form.get("Lines", []):
            scan("Receipt", form, line, line.get("Amount"))
        if (i + 1) % 100 == 0:
            print(f"  ...{i + 1}/{len(receipts)}")

    journals = all_records("/journal-entries", "journalEntries")
    print(f"[info] scanning {len(journals)} journal entries...")
    for i, j in enumerate(journals):
        form = api.get(f"/journal-entry-form/{j['key']}")
        for line in form.get("Lines", []):
            amt = line.get("Credit") or (-line.get("Debit", 0) if line.get("Debit") else None)
            scan("Journal", form, line, amt)
        if (i + 1) % 100 == 0:
            print(f"  ...{i + 1}/{len(journals)}")

    # Also scan Payments for a stray "REVIEW ... unresolved" description --
    # confirmed on one instance that the AP-side mirror of this same
    # migration-time convention exists too (a different problem, but the
    # same reference embedded, so cheap to also record here for visibility).
    payments = all_records("/payments", "payments")
    print(f"[info] scanning {len(payments)} payments for stray REVIEW markers...")
    for p in payments:
        form = api.get(f"/payment-form/{p['key']}")
        for line in form.get("Lines", []):
            desc = line.get("LineDescription") or ""
            m = REVIEW_RE.search(desc)
            if m:
                review_by_ref[m.group(1)] = {
                    "kind": "Payment", "date": form["Date"][:10], "amount": line.get("Amount"),
                    "form_key": form["Key"], "reference": form.get("Reference"),
                    "account": line.get("Account"), "line_index": form["Lines"].index(line),
                }

    return by_customer_key, untagged, review_by_ref


def main() -> int:
    api = API.ManagerAPI()

    myob_by_customer = load_myob_payments()
    print(f"[info] {sum(len(v) for v in myob_by_customer.values())} MYOB payment records "
          f"across {len(myob_by_customer)} customers")

    customers = api.get("/customers", pageSize=500)["customers"]
    customer_key_by_name = {c["name"]: c["key"] for c in customers}

    by_customer_key, untagged, review_by_ref = load_manager_ar_records(api)
    print(f"[info] {sum(len(v) for v in by_customer_key.values())} tagged, "
          f"{len(untagged)} untagged (no customer), {len(review_by_ref)} REVIEW-marked records found")

    missing = []
    unresolved = []  # found, but needs a fix (not a new Receipt)
    untagged_used = set()

    for cust_name, payments in myob_by_customer.items():
        cust_key = customer_key_by_name.get(cust_name)
        if not cust_key:
            print(f"[warn] no Manager customer found for MYOB name {cust_name!r}")
            continue
        tagged_records = by_customer_key.get(cust_key, [])
        tagged_used = set()

        for p in payments:
            # 1. Already correctly tagged to this customer?
            found = False
            for idx, m in enumerate(tagged_records):
                if idx in tagged_used:
                    continue
                if m["date"] == p["date"] and abs(m["amount"] - p["amount"]) < AMOUNT_TOLERANCE:
                    tagged_used.add(idx)
                    found = True
                    break
            if found:
                continue

            # 2. A REVIEW-marked record embedding this exact MYOB reference?
            ref = p.get("reference_no")
            if ref and ref in review_by_ref:
                unresolved.append({**p, "customer": cust_name, "found_as": review_by_ref[ref]})
                continue

            # 3. An untagged builtin-AR record matching by date+amount, globally?
            match_idx = None
            for idx, m in enumerate(untagged):
                if idx in untagged_used:
                    continue
                if m["date"] == p["date"] and abs(m["amount"] - p["amount"]) < AMOUNT_TOLERANCE:
                    match_idx = idx
                    break
            if match_idx is not None:
                untagged_used.add(match_idx)
                unresolved.append({**p, "customer": cust_name, "found_as": untagged[match_idx]})
                continue

            # Genuinely no match anywhere.
            missing.append({**p, "customer": cust_name})

    print(f"\n[result] {len(missing)} MYOB payment records have NO matching Manager record anywhere "
          f"(genuinely missing -- safe to reconstruct as a new Receipt)")
    print(f"[result] {len(unresolved)} MYOB payment records found an existing Manager record that "
          f"needs fixing (GET-merge-PUT the found record -- do NOT create a new Receipt for these)")

    out_dir = Path.cwd() / "out" / "manager"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "missing_ar_receipts.json").write_text(json.dumps(missing, indent=2))
    (out_dir / "unresolved_ar_receipts.json").write_text(json.dumps(unresolved, indent=2))
    print(f"[ok] wrote {out_dir / 'missing_ar_receipts.json'}")
    print(f"[ok] wrote {out_dir / 'unresolved_ar_receipts.json'}")

    if unresolved:
        print("\nUnresolved (fix the existing record, don't create a new one):")
        for u in unresolved:
            f = u["found_as"]
            print(f"  {u['customer']}: MYOB {u['reference_no']} {u['date']} ${u['amount']:,.2f} "
                  f"-> found as {f['kind']} {f['reference']} (key {f['form_key']}, "
                  f"account={'builtin AR, untagged' if f['account'] == BUILTIN_AR else f['account']})")

    if missing:
        by_cust = defaultdict(list)
        for m in missing:
            by_cust[m["customer"]].append(m)
        print("\nGenuinely missing:")
        for cust, ms in sorted(by_cust.items()):
            total = sum(m["amount"] for m in ms)
            print(f"  {cust}: {len(ms)} missing payment(s), total ${total:,.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
