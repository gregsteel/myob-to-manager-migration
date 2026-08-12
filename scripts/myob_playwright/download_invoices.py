#!/usr/bin/env python3
"""Harvest MYOB Sales Invoices via Playwright BFF (free, no paid API).

Reuses the download_bills.py session (exports/myob/bills/state/storage.json).

BFF endpoints (discovered 2026-07-27):
  GET  {BFF}/invoice/load_invoice_list_without_totals
       ?dateFrom=&dateTo=&statuses=Open|Overdue|Credit|Closed&type=All
       &period=All time&limit=50&orderBy=DateDue&sortOrder=desc&offset=0
       NOTE: offset paging is broken for wide date ranges — use year windows
       on DateDue (orderBy=DateDue only; DateIssued returns empty).
  GET  {BFF}/invoice/load_invoice_detail/{id}
  GET  {BFF}/invoice/load_invoice_history/{id}

Outputs (exports/myob/invoices/):
  state/invoices.jsonl     harvest index (id, number, status, folder)
  by_invoice/<num>-<cust>/ invoice.json (+ optional PDF later)
  _index.tsv               human index

Usage:
  cd scripts/myob_playwright && source .venv/bin/activate
  python3 download_invoices.py harvest
  python3 download_invoices.py download --limit 5
  python3 download_invoices.py download
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import download_bills as DB

INV_DIR = DB.ROOT / "exports" / "myob" / "invoices"
STATE_DIR = INV_DIR / "state"
BY_INV_DIR = INV_DIR / "by_invoice"
INDEX_TSV = INV_DIR / "_index.tsv"
INVOICES_JSONL = STATE_DIR / "invoices.jsonl"

DATE_FROM_DEFAULT = "2015-06-30"


def _ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BY_INV_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> dict[str, dict]:
    if not INVOICES_JSONL.exists():
        return {}
    out: dict[str, dict] = {}
    with INVOICES_JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[str(rec["id"])] = rec
    return out


def _rewrite_index(records: dict[str, dict]) -> None:
    _ensure_dirs()
    with INVOICES_JSONL.open("w", encoding="utf-8") as f:
        for rec in sorted(records.values(), key=lambda r: (r.get("issue_date") or "", r.get("id") or "")):
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with INDEX_TSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "id", "number", "customer", "issue_date", "due_date",
                "amount", "due", "status", "folder", "download_status",
            ],
            delimiter="\t",
        )
        w.writeheader()
        for rec in sorted(records.values(), key=lambda r: (r.get("issue_date") or "", r.get("id") or "")):
            w.writerow({
                "id": rec.get("id", ""),
                "number": rec.get("number", ""),
                "customer": rec.get("customer", ""),
                "issue_date": rec.get("issue_date", ""),
                "due_date": rec.get("due_date", ""),
                "amount": rec.get("amount", ""),
                "due": rec.get("due", ""),
                "status": rec.get("status", ""),
                "folder": rec.get("folder", ""),
                "download_status": rec.get("download_status", rec.get("status_flag", "")),
            })


def _capture_auth(page) -> dict[str, str]:
    auth: dict[str, str] = {}

    def on_request(request):
        if "sme-web-bff" not in request.url:
            return
        headers = request.headers
        if not headers.get("authorization"):
            return
        auth.clear()
        auth["authorization"] = headers["authorization"]
        auth["accept"] = "application/json"
        for extra in ("x-myobapi-idtoken", "region"):
            if headers.get(extra):
                auth[extra] = headers[extra]

    page.on("request", on_request)
    return auth


def _year_windows(date_from: str, date_to: str) -> list[tuple[str, str]]:
    """FY-ish windows (1 Jul–30 Jun) plus a final stub to date_to."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    windows: list[tuple[str, str]] = []
    # Start at 1 Jul of the FY containing start (AU FY ends 30 Jun)
    y = start.year if start.month >= 7 else start.year - 1
    cursor = date(y, 7, 1)
    if cursor > start:
        cursor = date(y - 1, 7, 1)
    while cursor <= end:
        nxt = date(cursor.year + 1, 6, 30)
        a = max(cursor, start)
        b = min(nxt, end)
        windows.append((a.isoformat(), b.isoformat()))
        cursor = date(cursor.year + 1, 7, 1)
    return windows


def _list_url(date_from: str, date_to: str, offset: int = 0) -> str:
    q = [
        ("dateFrom", date_from),
        ("dateTo", date_to),
        ("keywords", ""),
        ("statuses", "Open"),
        ("statuses", "Overdue"),
        ("statuses", "Credit"),
        ("statuses", "Closed"),
        ("type", "All"),
        ("period", "All time"),
        ("lastMonthInFinancialYear", "6"),
        ("overdueByDays", "All"),
        ("invoiceFundingTab", "fundable"),
        ("limit", "50"),
        ("clientDate", date.today().isoformat()),
        ("isInvoiceFundingEnabled", "false"),
        ("sortOrder", "desc"),
        ("orderBy", "DateDue"),
        ("offset", str(offset)),
    ]
    return (
        f"{DB.BFF_BASE}/invoice/load_invoice_list_without_totals?"
        f"{urllib.parse.urlencode(q)}"
    )


def _fetch_list(context, auth: dict, date_from: str, date_to: str) -> list[dict]:
    """Fetch all invoices in a date window. Offset is unreliable — stop when
    a page returns no *new* ids or hasNextPage is false."""
    seen: set[str] = set()
    out: list[dict] = []
    for offset in range(0, 5000, 50):
        url = _list_url(date_from, date_to, offset)
        resp = context.request.get(url, headers=auth, timeout=30000)
        if resp.status != 200:
            raise RuntimeError(f"list {resp.status}: {resp.text()[:300]}")
        body = resp.json()
        entries = body.get("entries") or []
        if not entries:
            break
        new = [e for e in entries if str(e.get("id")) not in seen]
        for e in entries:
            seen.add(str(e.get("id")))
        out.extend(new)
        if not new:
            break
        if not (body.get("pagination") or {}).get("hasNextPage"):
            break
    return out


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s or "", flags=re.U)
    s = re.sub(r"[\s_]+", "_", s.strip())[:60]
    return s or "customer"


def _iso_date(val) -> str | None:
    if not val:
        return None
    s = str(val).strip()
    if "T" in s:
        s = s.split("T", 1)[0]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _money(val) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_invoice(detail: dict, history: list[dict], list_row: dict) -> dict:
    inv = detail.get("invoice") or {}
    accounts = {
        str(a.get("id")): a
        for a in (detail.get("accountOptions") or [])
        if a.get("id") is not None
    }
    tax_codes = {
        str(t.get("id")): t
        for t in (detail.get("taxCodeOptions") or [])
        if t.get("id") is not None
    }
    lines = []
    for i, ln in enumerate(inv.get("lines") or [], start=1):
        acct = accounts.get(str(ln.get("accountId")), {})
        tax = tax_codes.get(str(ln.get("taxCodeId")), {})
        ex = _money(ln.get("taxExclusiveAmount"))
        tx = _money(ln.get("taxAmount"))
        lines.append({
            "line_number": i,
            "myob_line_id": ln.get("id"),
            "type": ln.get("type"),
            "description": ln.get("description"),
            "account_id": ln.get("accountId") or None,
            "account_code": acct.get("displayId") or acct.get("code"),
            "account_name": acct.get("displayName") or acct.get("name"),
            "tax_code_id": ln.get("taxCodeId") or None,
            "tax_code": tax.get("displayName") or tax.get("code") or tax.get("name"),
            "tax_rate": tax.get("rate") or tax.get("displayRate"),
            "units": _money(ln.get("units")),
            "unit_price": _money(ln.get("unitPrice")),
            "amount_ex_tax": ex,
            "tax_amount": tx,
            "amount_inc_tax": round((ex or 0) + (tx or 0), 2)
            if (ex is not None or tx is not None)
            else None,
        })

    payments = []
    for ev in history or []:
        status = str(ev.get("status") or "")
        if "PAYMENT" not in status.upper() and "Received" not in (ev.get("description") or ""):
            continue
        desc = ev.get("description") or ""
        amt = None
        m = re.search(r"\$([\d,]+(?:\.\d{2})?)", desc)
        if m:
            amt = _money(m.group(1))
        payments.append({
            "date": _iso_date(ev.get("date")),
            "amount": amt,
            "reference_no": ev.get("referenceNo"),
            "myob_journal_id": ev.get("journalId"),
            "source_journal_type": ev.get("sourceJournalType"),
            "status": status,
            "description": desc,
        })

    number = inv.get("invoiceNumber") or list_row.get("referenceId") or ""
    return {
        "schema_version": 1,
        "source": "myob_business",
        "scraped_at": datetime.now(timezone.utc).isoformat() + "Z",
        "invoice": {
            "myob_business_id": DB.BUSINESS_ID,
            "myob_invoice_id": str(inv.get("id") or list_row.get("id")),
            "myob_uid": inv.get("uid") or list_row.get("uid"),
            "number": number,
            "layout": inv.get("layout"),
            "customer": {
                "myob_id": inv.get("customerId") or list_row.get("customerId"),
                "uid": inv.get("customerUid") or list_row.get("customerUID"),
                "name": inv.get("customerName") or list_row.get("customer"),
            },
            "issue_date": _iso_date(inv.get("issueDate")) or _iso_date(list_row.get("dateIssued")),
            "due_date": _iso_date(inv.get("expirationDate"))
            or _iso_date(list_row.get("dateDue")),
            "status": inv.get("status") or list_row.get("status"),
            "is_tax_inclusive": bool(inv.get("isTaxInclusive")),
            "amount_paid": _money(inv.get("amountPaid")),
            "note": inv.get("note") or None,
            "purchase_order_number": inv.get("purchaseOrderNumber") or None,
            "lines": lines,
            "payments": payments,
            "raw_list": {
                "invoiceAmount": list_row.get("invoiceAmount"),
                "invoiceDue": list_row.get("invoiceDue"),
            },
        },
        "history": history or [],
    }


def cmd_harvest(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    _ensure_dirs()
    if not DB.STORAGE.exists():
        raise SystemExit("[error] run: python3 download_bills.py login")

    date_from = args.date_from
    date_to = args.date_to or date.today().isoformat()
    existing = _load_index()
    print(f"[info] already indexed: {len(existing)}")

    with sync_playwright() as p:
        browser = DB._browser(p, headless=args.headless)
        context = DB._context(browser)
        page = context.new_page()
        auth = _capture_auth(page)
        page.goto(DB.START_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if "id.myob.com" in (page.url or "").lower():
            raise SystemExit("[error] session expired — run download_bills.py login")
        page.goto(
            f"https://app.myob.com/#/au/{DB.BUSINESS_ID}/invoice?tab=all",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(4000)
        if not auth:
            raise SystemExit("[error] no BFF auth captured")

        windows = _year_windows(date_from, date_to)
        print(f"[info] {len(windows)} date windows {date_from} → {date_to}")
        for a, b in windows:
            entries = _fetch_list(context, auth, a, b)
            added = 0
            for e in entries:
                iid = str(e["id"])
                if iid in existing and existing[iid].get("download_status") == "done":
                    # refresh list metadata only
                    pass
                if iid not in existing:
                    added += 1
                existing[iid] = {
                    **existing.get(iid, {}),
                    "id": iid,
                    "uid": e.get("uid"),
                    "number": e.get("referenceId") or "",
                    "customer": e.get("customer") or "",
                    "customer_id": e.get("customerId"),
                    "issue_date": _iso_date(e.get("dateIssued")),
                    "due_date": _iso_date(e.get("dateDue")),
                    "amount": e.get("invoiceAmount"),
                    "due": e.get("invoiceDue"),
                    "status": e.get("status"),
                    "list_row": e,
                    "download_status": existing.get(iid, {}).get("download_status", "pending"),
                    "folder": existing.get(iid, {}).get("folder", ""),
                    "error": existing.get(iid, {}).get("error", ""),
                }
            print(f"  [{a} → {b}] {len(entries)} rows, +{added} new (index={len(existing)})")

        _rewrite_index(existing)
        context.storage_state(path=str(DB.STORAGE))
        browser.close()
    print(f"[ok] indexed {len(existing)} invoices → {INVOICES_JSONL}")


def cmd_download(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    _ensure_dirs()
    records = _load_index()
    if not records:
        raise SystemExit("[error] run harvest first")
    pending = [
        r for r in records.values()
        if r.get("download_status") != "done"
    ]
    pending.sort(key=lambda r: (r.get("issue_date") or "", r.get("id") or ""))
    if args.limit:
        pending = pending[: args.limit]
    print(f"[info] downloading {len(pending)} / {len(records)} invoices")

    with sync_playwright() as p:
        browser = DB._browser(p, headless=args.headless)
        context = DB._context(browser)
        page = context.new_page()
        auth = _capture_auth(page)
        page.goto(DB.START_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        page.goto(
            f"https://app.myob.com/#/au/{DB.BUSINESS_ID}/invoice?tab=all",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(3000)
        if not auth:
            raise SystemExit("[error] no BFF auth — session expired?")

        ok = fail = 0
        for i, rec in enumerate(pending, 1):
            iid = rec["id"]
            number = rec.get("number") or iid
            cust = _slug(rec.get("customer") or "")
            folder = BY_INV_DIR / f"{number}-{cust}"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                d_url = f"{DB.BFF_BASE}/invoice/load_invoice_detail/{iid}"
                h_url = f"{DB.BFF_BASE}/invoice/load_invoice_history/{iid}"
                d_resp = context.request.get(d_url, headers=auth, timeout=30000)
                h_resp = context.request.get(h_url, headers=auth, timeout=30000)
                if d_resp.status != 200:
                    raise RuntimeError(f"detail {d_resp.status}: {d_resp.text()[:200]}")
                detail = d_resp.json()
                history = []
                if h_resp.status == 200:
                    history = (h_resp.json() or {}).get("invoiceHistory") or []
                record = _normalize_invoice(detail, history, rec.get("list_row") or rec)
                (folder / "invoice.json").write_text(
                    json.dumps(record, indent=2, ensure_ascii=False)
                )
                (folder / "detail_raw.json").write_text(
                    json.dumps(detail, indent=2, ensure_ascii=False)
                )
                rec["folder"] = str(folder.relative_to(INV_DIR))
                rec["download_status"] = "done"
                rec["error"] = ""
                ok += 1
                print(f"  [{i}/{len(pending)}] {number} {rec.get('customer','')[:40]} OK")
            except Exception as exc:
                rec["download_status"] = "error"
                rec["error"] = str(exc)[:300]
                fail += 1
                print(f"  [{i}/{len(pending)}] {number} FAIL {exc}")
            records[iid] = rec
            if i % 25 == 0:
                _rewrite_index(records)
                context.storage_state(path=str(DB.STORAGE))
            time.sleep(args.pause)

        _rewrite_index(records)
        context.storage_state(path=str(DB.STORAGE))
        browser.close()
    print(f"[ok] done={ok} fail={fail} → {BY_INV_DIR}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="Index invoice ids via BFF list")
    h.add_argument("--date-from", default=DATE_FROM_DEFAULT)
    h.add_argument("--date-to", default="")
    h.add_argument("--headless", action="store_true", default=True)
    h.add_argument("--headed", action="store_true")

    d = sub.add_parser("download", help="Fetch detail+history JSON per invoice")
    d.add_argument("--limit", type=int, default=0)
    d.add_argument("--pause", type=float, default=0.15)
    d.add_argument("--headless", action="store_true", default=True)
    d.add_argument("--headed", action="store_true")

    args = ap.parse_args()
    if getattr(args, "headed", False):
        args.headless = False
    if args.cmd == "harvest":
        cmd_harvest(args)
    elif args.cmd == "download":
        cmd_download(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
