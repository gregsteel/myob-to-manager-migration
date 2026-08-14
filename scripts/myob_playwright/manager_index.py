#!/usr/bin/env python3
"""Match MYOB bills/invoices to live Manager.io Purchase/Sales Invoice records.

Rewritten 2026-08-12. The original version of this file (see the sibling
Manager1 project) matched against Manager **Journal** records via a direct
SQLite scan of the `.manager` business file, fuzzy-scored by reference +
narration + date + amount — that was built for this project's original
journal-based import approach.

This project has since moved to a different, validated convention (see
`scripts/fix_general_journal_gaps.py`, `scripts/build_director_clearing_journals.py`,
and `.claude/skills/manager-automation/reference/invoice-linking.md`):
Manager Purchase Invoices are created with
`Reference = "{myob_bill_number}-{issue_date:%Y%m%d}"` (the date suffix
exists specifically because MYOB bill numbers recycle across years — a bare
number is not a safe dedup key). This rewrite matches against that
convention directly over the live REST API instead of scanning SQLite,
which is both simpler and reflects what MYOB->Manager creation scripts in
this repo actually produce today.

Sales Invoice numbers are used **verbatim** (no date suffix) per the only
surviving evidence of that convention (`out/manager/sales_invoices_seeded.tsv`)
-- this has never been stress-tested for collisions the way bill numbers
were. Call `check_si_collisions()` against a freshly harvested
`exports/myob/invoices/_index.tsv` before trusting it at scale; if
collisions turn up, switch to the same `-YYYYMMDD` suffix used for bills.

Both `balanceDue` and `reference` are exposed on the live list endpoints
for purchase-invoices and sales-invoices (confirmed directly, not assumed)
so both existence-check and already-paid-check are answerable from one
cheap `list_all()` call each -- no per-record `get_form` scan needed for
either check, unlike the old SQLite approach.

Public API kept compatible with the original module so `download_bills.py`
doesn't need to change its call sites:
    build_index() -> dict
    match_bill(index, *, bill_number, issue_date, total=None,
               supplier_name=None, payments=None) -> dict
New:
    match_invoice(index, *, number, issue_date=None) -> dict
    check_si_collisions(index_tsv_path) -> list[str]   # duplicate SI numbers found
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# This file's real (post-symlink-resolution) location is
# .claude/skills/myob-to-manager-migration/scripts/myob_playwright/ --
# lib_manager_api.py lives in the sibling manager-automation skill.
# parents[3] from here is .claude/skills/ (0=myob_playwright, 1=scripts,
# 2=myob-to-manager-migration, 3=skills).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "manager-automation" / "scripts"))
import lib_manager_api as API  # noqa: E402

BUILTIN_AP = "dac7ba37-0ccd-45e5-906e-548e6c50df37"
BUILTIN_AR = "d1489e95-bb28-4f5d-b42e-67d3291b3893"


def _iso(val) -> str | None:
    if not val:
        return None
    if isinstance(val, date):
        return val.isoformat()
    s = str(val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def pi_reference(bill_number: str, issue_date) -> str:
    """The composite Reference convention this project uses for Purchase
    Invoices: "{bill_number}-{issue_date:%Y%m%d}". Falls back to the bare
    number if the date can't be parsed (should not happen for real data)."""
    d = _iso(issue_date)
    if not d:
        return str(bill_number)
    return f"{bill_number}-{d.replace('-', '')}"


def build_index(api: API.ManagerAPI | None = None) -> dict:
    """Live snapshot of Manager Purchase/Sales Invoices + Debit Notes, keyed
    by Reference."""
    api = api or API.ManagerAPI()
    pis = api.list_all("purchase-invoice")
    sis = api.list_all("sales-invoice")
    return {
        "api": api,
        "pi_by_ref": {p["reference"]: p for p in pis if p.get("reference")},
        "si_by_ref": {s["reference"]: s for s in sis if s.get("reference")},
        "dn_by_ref": _build_dn_index(api),
    }


def _build_dn_index(api: API.ManagerAPI) -> dict[str, dict]:
    """Reference -> Debit Note summary. Unlike purchase-invoices/sales-invoices,
    the debit-notes list endpoint does NOT expose `reference` on its rows
    (confirmed 2026-08-12 -- only key/date/supplier/description/amount) --
    a per-record get_form is required to read it. Fine at this document
    type's expected volume (negative-total bills are rare; this is not the
    thousands-of-records case purchase-invoice/sales-invoice dedup has to
    handle cheaply)."""
    out: dict[str, dict] = {}
    for d in api.list_all("debit-note"):
        form = api.get_form("debit-note", d["key"])
        ref = form.get("Reference")
        if ref:
            out[ref] = {
                "key": d["key"],
                "reference": ref,
                "date": d.get("date"),
                "amount": (d.get("amount") or {}).get("value"),
            }
    return out


def _invoice_summary(rec: dict, method: str) -> dict:
    amount = (rec.get("invoiceAmount") or {}).get("value")
    balance_due = (rec.get("balanceDue") or {}).get("value")
    return {
        "key": rec.get("key"),
        "reference": rec.get("reference"),
        "date": rec.get("issueDate"),
        "amount": amount,
        "balance_due": balance_due,
        "already_paid": balance_due is not None and balance_due == 0,
        "match_method": method,
        "match_confidence": "high",
    }


def match_bill(
    index: dict,
    *,
    bill_number: str,
    issue_date,
    total: float | None = None,
    supplier_name: str | None = None,
    payments: list[dict] | None = None,
) -> dict:
    """Does this MYOB bill already exist in Manager as a Purchase Invoice or
    a Debit Note? (Negative-total bills are created as Debit Notes, not
    Purchase Invoices -- see apply_bills_invoices.py.)

    Returns the same top-level shape the original module returned
    (`purchase` / `payments` / `matched_journals`), plus `debit_note`, so
    `download_bills.py` doesn't need changes -- but `payments`/`matched_journals`
    are no longer individually fuzzy-matched (that was informational detail
    for the old workflow, not needed for dedup + already-paid detection,
    both of which come straight off the Purchase Invoice's own `balanceDue`).
    """
    ref = pi_reference(bill_number, issue_date)
    pi = index["pi_by_ref"].get(ref)
    purchase = _invoice_summary(pi, "reference_exact") if pi else None
    debit_note = index["dn_by_ref"].get(ref)
    matched_journals = [{**purchase, "link": "purchase"}] if purchase else []
    return {"purchase": purchase, "debit_note": debit_note, "payments": [], "matched_journals": matched_journals}


def match_invoice(index: dict, *, number: str, issue_date=None) -> dict:
    """Does this MYOB sales invoice already exist in Manager as a Sales Invoice?"""
    si = index["si_by_ref"].get(str(number))
    sale = _invoice_summary(si, "number_exact") if si else None
    return {"sale": sale}


def journal_marker(ref_no: str, issue_date: str) -> str:
    """Dedup marker embedded in a created Journal Entry's Narration for
    standalone MYOB General Journals (BAS/FBT/depreciation/etc adjusting
    entries) -- deliberately NOT the MYOB `Ref no` alone (recall
    build_journals.py's own warning: MYOB reference numbers/formats like
    "GJ000003" are stable within one MYOB era but nothing here guarantees
    global uniqueness across the full history the same way bill numbers
    needed a date suffix) and NEVER the journal_dictionary.tsv `txn_id`
    (confirmed unstable -- reassigned on every regeneration). This pairs
    the MYOB ref_no with its own issue_date, which is what's actually
    stable and unique for a given MYOB business.

    Uses "MYOB-DELTA" (not bare "MYOB") specifically because every
    historically-migrated journal already carries an unrelated
    "[MYOB txn NNNN]" annotation from the original import -- confirmed
    2026-08-12: a bare "[MYOB ...]" regex matched 55 pre-existing
    historical narrations before any delta-migration journal had ever
    been created, which would have made every future journal look like a
    false-positive duplicate forever. Keep the two formats visually and
    programmatically distinct.
    """
    return f"[MYOB-DELTA {ref_no}/{issue_date}]"


def build_journal_index(api: API.ManagerAPI) -> set[str]:
    """Every journal_marker() string already present in some live Manager
    Journal Entry's Narration. journal-entries list rows expose narration
    directly, so this is one cheap list_all call, no per-record GET
    needed."""
    markers: set[str] = set()
    for j in api.list_all("journal-entry"):
        narr = j.get("narration") or ""
        for m in re.findall(r"\[MYOB-DELTA [^\]]+\]", narr):
            markers.add(m)
    return markers


def check_si_collisions(index_tsv: Path) -> list[str]:
    """Scan a harvested exports/myob/invoices/_index.tsv for MYOB invoice
    numbers that appear more than once (would break the verbatim-number
    dedup key the same way recycled bill numbers did for Purchase
    Invoices). Returns the list of colliding numbers; empty = safe to use
    verbatim numbers as-is."""
    seen: dict[str, list[str]] = defaultdict(list)
    with index_tsv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            num = (row.get("number") or "").strip()
            if num:
                seen[num].append(row.get("id", ""))
    return [num for num, ids in seen.items() if len(ids) > 1]


if __name__ == "__main__":
    idx = build_index()
    print(f"[ok] live Manager index: {len(idx['pi_by_ref'])} Purchase Invoices, "
          f"{len(idx['si_by_ref'])} Sales Invoices (by Reference)")

    r = match_bill(idx, bill_number="00000822", issue_date="2025-10-06")
    print("\n=== 00000822-20251006 (known-existing FY2026 Q1 BAS bill) ===")
    print("purchase:", r["purchase"])

    r2 = match_bill(idx, bill_number="00099999", issue_date="2099-01-01")
    print("\n=== 00099999-20990101 (should NOT exist) ===")
    print("purchase:", r2["purchase"])
