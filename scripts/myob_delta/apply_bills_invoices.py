#!/usr/bin/env python3
"""Create Manager Purchase/Sales Invoices for the MYOB delta identified by
filter_delta.py.

Built directly on lib_manager_api.ManagerAPI (post_form/get_form/put_form),
following the pattern already used in this repo by
scripts/fix_general_journal_gaps.py and
scripts/build_director_clearing_journals.py -- there is no generic
apply-with-dedup runner to reuse (apply_manager_api.py is documented but
does not exist in this repo; confirmed by full git-history search).

Reference convention (matches every existing PI in this business, see
scripts/myob_playwright/manager_index.py): "{bill_number}-{issue_date:%Y%m%d}"
for Purchase Invoices, the bare MYOB number for Sales Invoices.

Tax handling: a MYOB line's own `tax_code`/`tax_rate` decides the Manager
side --
  - "N-T" (not taxable, rate 0) -> no TaxCode at all, PurchaseUnitPrice =
    amount_ex_tax (== amount_inc_tax since tax is always 0 on these lines).
    This is the direct-control-account posting pattern (e.g. the ATO BAS
    bills' GST/PAYGW take-up lines) -- matches every existing bill of this
    shape already in Manager (TaxCode: null, confirmed by direct read).
  - "FRE" -> Manager "GST Free" tax code.
  - "GST" -> Manager "GST 10%" tax code.
  Any other MYOB tax code is *skipped* with a warning rather than guessed
  at -- extend TAX_CODE_MAP once you've confirmed the right Manager code.
  Tax-coded lines send the tax-INCLUSIVE unit price with a top-level (not
  per-line) `AmountsIncludeTax: true` on the invoice payload -- see
  build_purchase_invoice_payload's own comment for why per-line placement
  is a silent no-op and top-level is the confirmed-working fix.

Never creates anything dated after today's real date (Golden Rule, both
skills' SKILL.md) -- flags and skips instead.

This script does NOT touch Payments/Receipts -- matching an existing bank
Payment to a newly-created invoice is a separate, explicit, per-record step
(see the FIFO-cascade danger and "always set PurchaseInvoice/SalesInvoice
explicitly" rule in manager-automation's invoice-linking.md) done by hand
or by a follow-up targeted script once the invoice exists to link against.

Usage:
  python3 scripts/myob_delta/apply_bills_invoices.py             # dry-run (default)
  python3 scripts/myob_delta/apply_bills_invoices.py --apply     # real writes
  python3 scripts/myob_delta/apply_bills_invoices.py --apply --only 00001043
"""
from __future__ import annotations

import argparse
import csv
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

ROOT = Path.cwd()
BILLS_DIR = ROOT / "exports" / "myob" / "bills" / "by_bill"
INVOICES_DIR = ROOT / "exports" / "myob" / "invoices" / "by_invoice"
TAX_CODE_MAP_FILE = ROOT / "config" / "myob_tax_code_map.tsv"


def _load_tax_code_map() -> dict[str, str]:
    """myob_code -> Manager tax code name, from project config (per-business
    -- Manager tax code *names* aren't standardised the way MYOB's AU
    short-codes are, so this isn't safe to hardcode in a skill script
    meant to be forked across businesses). "N-T" is deliberately never a
    key here -- it means "no TaxCode", handled explicitly, not mapped."""
    mapping = {}
    with TAX_CODE_MAP_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            mapping[row["myob_code"]] = row["manager_tax_code_name"]
    return mapping


TAX_CODE_MAP = _load_tax_code_map()


def _load_coa(api: API.ManagerAPI) -> dict[str, str]:
    return {a["code"]: a["key"] for a in api.get("/chart-of-accounts")["chartOfAccounts"] if a.get("code")}


def _load_tax_codes(api: API.ManagerAPI) -> dict[str, str]:
    return {c["name"]: c["key"] for c in api.get("/tax-codes")["taxCodes"]}


def _find_supplier(api: API.ManagerAPI, name: str) -> str | None:
    r = api.get("/suppliers", pageSize=5, term=name)
    hit = next((s for s in r["suppliers"] if s["name"] == name), None)
    return hit["key"] if hit else None


def _find_customer(api: API.ManagerAPI, name: str) -> str | None:
    r = api.get("/customers", pageSize=5, term=name)
    hit = next((c for c in r["customers"] if c["name"] == name), None)
    return hit["key"] if hit else None


def build_purchase_invoice_payload(bill: dict, coa: dict, tax_codes: dict, supplier_key: str) -> dict | None:
    lines = []
    any_tax_coded = False
    for ln in bill["lines"]:
        code = ln.get("account_code")
        if code not in coa:
            print(f"    [skip line] unknown account code {code!r}")
            return None
        account_key = coa[code]
        tax_name = ln.get("tax_code")
        amt_inc = ln.get("amount_inc_tax")
        if tax_name == "N-T" or (ln.get("tax_amount") or 0) == 0 and tax_name not in TAX_CODE_MAP:
            if tax_name not in ("N-T", None):
                print(f"    [skip bill] unmapped tax_code {tax_name!r} on account {code} -- extend TAX_CODE_MAP")
                return None
            lines.append({
                "Account": account_key,
                "LineDescription": ln.get("description") or "",
                "Qty": 1,
                "PurchaseUnitPrice": amt_inc,
            })
        else:
            manager_tax_name = TAX_CODE_MAP.get(tax_name)
            if manager_tax_name is None or manager_tax_name not in tax_codes:
                print(f"    [skip bill] unmapped tax_code {tax_name!r} on account {code} -- extend TAX_CODE_MAP")
                return None
            any_tax_coded = True
            lines.append({
                "Account": account_key,
                "LineDescription": ln.get("description") or "",
                "Qty": 1,
                "PurchaseUnitPrice": amt_inc,
                "TaxCode": tax_codes[manager_tax_name],
            })
    reference = MI.pi_reference(bill["number"], bill["issue_date"])
    payload = {
        "IssueDate": bill["issue_date"],
        "Reference": reference,
        "Supplier": supplier_key,
        "Lines": lines,
    }
    if any_tax_coded:
        # AmountsIncludeTax is a whole-invoice flag, NOT a per-line one --
        # it must sit at this top level of the payload. Placing it inside a
        # Lines[] entry instead (the mistake in an earlier version of this
        # script) is silently dropped: Manager falls back to exclusive-tax
        # behavior and adds the TaxCode's rate ON TOP of the price given,
        # confirmed 2026-08-12 to overstate a $62.39 bill as $68.63 that
        # way. At the correct top level, confirmed working on both POST and
        # PUT: sending the tax-INCLUSIVE unit price with
        # AmountsIncludeTax:true here reproduces the source total exactly,
        # including on the one bill (00001060) where re-deriving the total
        # from amount_ex_tax + Manager's own tax-rounding landed 1 cent
        # high because Manager and MYOB broke an exact rounding tie
        # (8.675 -> 8.68 vs MYOB's 8.67) in opposite directions -- sending
        # the already-rounded inclusive total sidesteps that entirely.
        payload["AmountsIncludeTax"] = True
    return payload


def build_debit_note_payload(bill: dict, coa: dict, tax_codes: dict, supplier_key: str) -> dict | None:
    """A negative-total MYOB bill (a supplier refund/credit, e.g. an
    insurance policy adjustment) is a Debit Note in Manager, not a
    Purchase Invoice with a negative total -- confirmed 2026-08-12 both by
    direct instruction and by finding an existing record whose own
    Description already read "... (credit note)" despite having been
    created as an ordinary (negative) Purchase Invoice by an earlier
    version of this pipeline.

    Same line-building logic as build_purchase_invoice_payload (same
    `debit-note-form` field names -- PurchaseUnitPrice, TaxCode,
    top-level AmountsIncludeTax, confirmed identical via a disposable
    test record), except every amount is sent as its **positive**
    magnitude: a Debit Note's own document type already encodes the
    AP-reducing direction, the way a Credit Note does on the AR side --
    entering a negative amount on top of that would double the sign.
    Confirmed against both real records: AAMI $-608.11 bill -> Debit Note
    with lines +$552.83/+$55.28 posts as a live $608.11 Debit Note, not
    -$608.11."""
    lines = []
    any_tax_coded = False
    for ln in bill["lines"]:
        code = ln.get("account_code")
        if code not in coa:
            print(f"    [skip line] unknown account code {code!r}")
            return None
        account_key = coa[code]
        tax_name = ln.get("tax_code")
        amt_inc = abs(ln.get("amount_inc_tax") or 0)
        if tax_name == "N-T" or (ln.get("tax_amount") or 0) == 0 and tax_name not in TAX_CODE_MAP:
            if tax_name not in ("N-T", None):
                print(f"    [skip bill] unmapped tax_code {tax_name!r} on account {code} -- extend TAX_CODE_MAP")
                return None
            lines.append({
                "Account": account_key,
                "LineDescription": ln.get("description") or "",
                "Qty": 1,
                "PurchaseUnitPrice": amt_inc,
            })
        else:
            manager_tax_name = TAX_CODE_MAP.get(tax_name)
            if manager_tax_name is None or manager_tax_name not in tax_codes:
                print(f"    [skip bill] unmapped tax_code {tax_name!r} on account {code} -- extend TAX_CODE_MAP")
                return None
            any_tax_coded = True
            lines.append({
                "Account": account_key,
                "LineDescription": ln.get("description") or "",
                "Qty": 1,
                "PurchaseUnitPrice": amt_inc,
                "TaxCode": tax_codes[manager_tax_name],
            })
    reference = MI.pi_reference(bill["number"], bill["issue_date"])
    payload = {
        "IssueDate": bill["issue_date"],
        "Reference": reference,
        "Supplier": supplier_key,
        "Lines": lines,
    }
    if any_tax_coded:
        payload["AmountsIncludeTax"] = True
    return payload


def build_sales_invoice_payload(inv: dict, coa: dict, tax_codes: dict, customer_key: str) -> dict | None:
    lines = []
    any_tax_coded = False
    for ln in inv["lines"]:
        code = ln.get("account_code")
        if code not in coa:
            print(f"    [skip line] unknown account code {code!r}")
            return None
        account_key = coa[code]
        tax_name = ln.get("tax_code")
        amt_inc = ln.get("amount_inc_tax")
        if tax_name == "N-T" or (ln.get("tax_amount") or 0) == 0 and tax_name not in TAX_CODE_MAP:
            if tax_name not in ("N-T", None):
                print(f"    [skip invoice] unmapped tax_code {tax_name!r} on account {code} -- extend TAX_CODE_MAP")
                return None
            lines.append({
                "Account": account_key,
                "LineDescription": ln.get("description") or "",
                "Qty": 1,
                "SalesUnitPrice": amt_inc,
            })
        else:
            manager_tax_name = TAX_CODE_MAP.get(tax_name)
            if manager_tax_name is None or manager_tax_name not in tax_codes:
                print(f"    [skip invoice] unmapped tax_code {tax_name!r} on account {code} -- extend TAX_CODE_MAP")
                return None
            any_tax_coded = True
            lines.append({
                "Account": account_key,
                "LineDescription": ln.get("description") or "",
                "Qty": 1,
                "SalesUnitPrice": amt_inc,
                "TaxCode": tax_codes[manager_tax_name],
            })
    # Sales Invoice Reference convention: verbatim MYOB number, no date suffix
    # (confirmed zero collisions across the full harvested invoice history --
    # see manager_index.check_si_collisions()). Unlike bills, no -YYYYMMDD needed.
    payload = {
        "IssueDate": inv["issue_date"],
        "Reference": inv["number"],
        "Customer": customer_key,
        "Lines": lines,
    }
    if any_tax_coded:
        # See build_purchase_invoice_payload's comment -- same top-level
        # (not per-line) AmountsIncludeTax placement, same confirmed fix.
        payload["AmountsIncludeTax"] = True
    return payload


def apply_bills(api: API.ManagerAPI, idx: dict, rows: list[dict], *, apply: bool, only: set[str] | None) -> None:
    coa = _load_coa(api)
    tax_codes = _load_tax_codes(api)
    today = date.today().isoformat()
    supplier_cache: dict[str, str | None] = {}

    for row in rows:
        bn = row["bill_number"]
        if only and bn not in only:
            continue
        folder = BILLS_DIR / row["folder"].split("/", 1)[1]
        bill_json = folder / "bill.json"
        if not bill_json.exists():
            print(f"[skip] {bn}: no bill.json under {folder}")
            continue
        bill = json.loads(bill_json.read_text())["bill"]

        if bill["issue_date"] > today:
            print(f"[skip] {bn}: issue_date {bill['issue_date']} is after today ({today}) -- refuse to create")
            continue

        # Re-check live (idx may be stale if this run applies several bills in sequence).
        m = MI.match_bill(idx, bill_number=bn, issue_date=bill["issue_date"])
        if m["purchase"] is not None:
            print(f"[skip] {bn}: already exists in Manager as {m['purchase']['reference']}")
            continue
        if m["debit_note"] is not None:
            print(f"[skip] {bn}: already exists in Manager as Debit Note {m['debit_note']['reference']}")
            continue

        supplier_name = bill["supplier"]["name"]
        if supplier_name not in supplier_cache:
            supplier_cache[supplier_name] = _find_supplier(api, supplier_name)
        supplier_key = supplier_cache[supplier_name]
        if not supplier_key:
            print(f"[skip] {bn}: no Manager Supplier found named {supplier_name!r} -- create it first")
            continue

        # A negative-total bill (supplier refund/credit) is a Debit Note,
        # not a Purchase Invoice with a negative total -- see
        # build_debit_note_payload's docstring.
        is_debit_note = bill["totals"]["total_inc_tax"] < 0
        entity = "debit-note" if is_debit_note else "purchase"
        list_path, list_key = ("/debit-notes", "debitNotes") if is_debit_note else ("/purchase-invoices", "purchaseInvoices")
        builder = build_debit_note_payload if is_debit_note else build_purchase_invoice_payload

        payload = builder(bill, coa, tax_codes, supplier_key)
        if payload is None:
            print(f"[skip] {bn}: could not build a full line set (see above)")
            continue

        total = sum(
            (l.get("PurchaseUnitPrice") or 0) * l.get("Qty", 1)
            for l in payload["Lines"]
        )
        tag = " [DEBIT NOTE]" if is_debit_note else ""
        print(f"{'[APPLY]' if apply else '[DRY-RUN]'} {bn}{tag}  {bill['issue_date']}  "
              f"{supplier_name}  ref={payload['Reference']}  "
              f"lines={len(payload['Lines'])}  total=${total:,.2f}")
        for l in payload["Lines"]:
            tc = l.get("TaxCode", "—")
            print(f"           {l['LineDescription'][:40]:40s} ${l['PurchaseUnitPrice']:>10,.2f}  tax={tc}")

        if apply:
            rec = api.post_form(entity, payload)
            key = rec.get("key") or rec.get("Key")
            live = api.get(list_path, pageSize=1, term=payload["Reference"])
            live_row = next((r for r in live[list_key] if r.get("key") == key), live[list_key][0])
            live_amount = (live_row.get("invoiceAmount") or live_row.get("amount") or {}).get("value")
            expected = abs(bill["totals"]["total_inc_tax"]) if is_debit_note else bill["totals"]["total_inc_tax"]
            ok = live_amount is not None and abs(live_amount - expected) < 0.01
            print(f"           -> created key={key}  live_amount=${live_amount:,.2f}  "
                  f"expected=${expected:,.2f}  {'OK' if ok else '*** MISMATCH ***'}")


def apply_invoices(api: API.ManagerAPI, idx: dict, rows: list[dict], *, apply: bool, only: set[str] | None) -> None:
    coa = _load_coa(api)
    tax_codes = _load_tax_codes(api)
    today = date.today().isoformat()
    customer_cache: dict[str, str | None] = {}

    for row in rows:
        num = row["number"]
        if only and num not in only:
            continue
        folder = INVOICES_DIR / row["folder"].split("/", 1)[1] if row.get("folder") else None
        if not folder or not (folder / "invoice.json").exists():
            print(f"[skip] SI {num}: no invoice.json harvested")
            continue
        inv = json.loads((folder / "invoice.json").read_text())["invoice"]

        if inv["issue_date"] > today:
            print(f"[skip] SI {num}: issue_date {inv['issue_date']} is after today ({today}) -- refuse to create")
            continue

        m = MI.match_invoice(idx, number=num, issue_date=inv["issue_date"])
        if m["sale"] is not None:
            print(f"[skip] SI {num}: already exists in Manager")
            continue

        cust_name = inv["customer"]["name"]
        if cust_name not in customer_cache:
            customer_cache[cust_name] = _find_customer(api, cust_name)
        cust_key = customer_cache[cust_name]
        if not cust_key:
            print(f"[skip] SI {num}: no Manager Customer found named {cust_name!r} -- create it first")
            continue

        payload = build_sales_invoice_payload(inv, coa, tax_codes, cust_key)
        if payload is None:
            print(f"[skip] SI {num}: could not build a full line set (see above)")
            continue

        total = sum(
            (l.get("SalesUnitPrice") or 0) * l.get("Qty", 1)
            for l in payload["Lines"]
        )
        print(f"{'[APPLY]' if apply else '[DRY-RUN]'} SI {num}  {inv['issue_date']}  "
              f"{cust_name}  ref={payload['Reference']}  "
              f"lines={len(payload['Lines'])}  total=${total:,.2f}")
        for l in payload["Lines"]:
            tc = l.get("TaxCode", "—")
            print(f"           {l['LineDescription'][:40]:40s} ${l['SalesUnitPrice']:>10,.2f}  tax={tc}")

        if apply:
            si = api.post_form("sales", payload)
            key = si.get("key") or si.get("Key")
            live = api.get("/sales-invoices", pageSize=1, term=payload["Reference"])
            live_amount = live["salesInvoices"][0]["invoiceAmount"]["value"]
            expected = sum(l["amount_inc_tax"] for l in inv["lines"])
            ok = abs(live_amount - expected) < 0.01
            print(f"           -> created key={key}  live_amount=${live_amount:,.2f}  "
                  f"expected=${expected:,.2f}  {'OK' if ok else '*** MISMATCH ***'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform real writes (default: dry-run)")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these bill/invoice numbers")
    ap.add_argument("--after-date", default=None)
    args = ap.parse_args()

    api = API.ManagerAPI()
    after = args.after_date or FD.last_migration_date()
    idx = MI.build_index(api)
    only = set(args.only) if args.only else None

    print(f"=== Purchase Invoices (issue_date > {after}) ===")
    new_bills = FD.find_new_bills(idx, after)
    apply_bills(api, idx, new_bills, apply=args.apply, only=only)

    print(f"\n=== Sales Invoices (issue_date > {after}) ===")
    new_invoices = FD.find_new_invoices(idx, after)
    apply_invoices(api, idx, new_invoices, apply=args.apply, only=only)

    if not args.apply:
        print("\n[dry-run] no writes made -- pass --apply to create these records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
