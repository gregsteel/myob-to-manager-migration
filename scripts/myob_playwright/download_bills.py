#!/usr/bin/env python3
"""Download MYOB Business bills via Playwright into per-bill archive folders.

For each bill creates:
    exports/myob/bills/by_bill/<bill_number>-<supplier>/
        receipt.<ext>     linked source document (supplier PDF/image)
        myob_bill.pdf     MYOB View PDF → Export
        bill.json         structured metadata + Manager journal keys

Bill data comes from MYOB's own web API (`bill/load_bill/<id>` and
`bill/load_bill_activity_history/<id>`) captured while the page loads, so line
items, tax codes, accounts and payments are exact rather than screen-scraped.
Ids are resolved to names using the option lists in the same payloads plus a
cached supplier list.

SETUP
-----
    cd scripts/myob_playwright
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium

USAGE
-----
    python3 download_bills.py login
    python3 download_bills.py harvest          # Issue from 30/06/2015 → today
    python3 download_bills.py download --limit 5
    python3 download_bills.py download         # full resumable run
    python3 download_bills.py reset            # pending + wipe by_bill/
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import manager_index

# Root resolution: this file is normally reached via a project symlink
# (project/scripts/myob_playwright/download_bills.py -> this skill copy),
# so ROOT must be the *project's* root -- found by walking up from the
# current working directory (matching lib_manager_api.py's own .env
# search convention), never Path(__file__)'s own location, which would
# resolve inside the skill instead of the host project when symlinked.
def _find_project_root() -> Path:
    d = Path.cwd()
    for candidate in (d, *d.parents):
        if (candidate / "config" / "myob_business_id.txt").is_file():
            return candidate
    raise FileNotFoundError(
        "config/myob_business_id.txt not found searching upward from cwd -- "
        "run from the project root, with that file created (the MYOB "
        "Business ID GUID from the URL: https://app.myob.com/#/au/<id>/...)"
    )


ROOT = _find_project_root()
ARCHIVE = ROOT / "exports" / "myob" / "bills"
STATE_DIR = ARCHIVE / "state"
BY_BILL_DIR = ARCHIVE / "by_bill"
STORAGE = STATE_DIR / "storage.json"
BILLS_JSONL = STATE_DIR / "bills.jsonl"
CONTACTS_JSON = STATE_DIR / "contacts.json"
INDEX_TSV = ARCHIVE / "_index.tsv"

START_URL = "https://app.myob.com/"
BUSINESS_ID = (ROOT / "config" / "myob_business_id.txt").read_text().strip()
BILLS_LIST_URL = f"https://app.myob.com/#/au/{BUSINESS_ID}/bill"
BFF_BASE = f"https://production.sme-web-bff.myob.com/{BUSINESS_ID}"
DATE_FROM = "30/06/2015"
BILL_HREF_RE = re.compile(r"/bill/(\d+)(?:/|$|\?)", re.I)

SEL = {
    "purchases_menu": "text=Purchases",
    "view_pdf": 'button:has-text("View PDF"), a:has-text("View PDF")',
    "split_view": 'button:has-text("Open split view"), a:has-text("Open split view"), '
                  'button:has-text("split view"), [aria-label*="split" i]',
    "download_attach": 'button[aria-label*="Download" i], a[aria-label*="Download" i], '
                       '[data-testid*="download" i], button:has-text("Download")',
}


# ---------------------------------------------------------------- plumbing --

def _ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BY_BILL_DIR.mkdir(parents=True, exist_ok=True)


def _browser(playwright, headless: bool = False):
    # Keep Chromium's temp download dirs out of by_bill/.
    tmp = STATE_DIR / "downloads"
    tmp.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch(headless=headless, downloads_path=str(tmp))


def _context(browser):
    kwargs = {"accept_downloads": True, "viewport": {"width": 1400, "height": 900}}
    if STORAGE.exists():
        kwargs["storage_state"] = str(STORAGE)
    return browser.new_context(**kwargs)


def _safe_folder_part(text: str) -> str:
    text = (text or "unknown").strip()
    text = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("._")
    return text[:80] or "unknown"


def _bill_folder(number: str, supplier: str, myob_bill_id: str | None = None) -> Path:
    # Include MYOB bill id so recycled bill numbers (same number, different years)
    # do not overwrite each other.
    parts = [_safe_folder_part(number)]
    if myob_bill_id:
        parts.append(_safe_folder_part(str(myob_bill_id)))
    parts.append(_safe_folder_part(supplier))
    path = BY_BILL_DIR / "-".join(parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_bills() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not BILLS_JSONL.exists():
        return out
    with BILLS_JSONL.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                out[rec["url"]] = rec
    return out


def _rewrite_bills(records: dict[str, dict]) -> None:
    with BILLS_JSONL.open("w") as f:
        for rec in records.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_index(records: dict[str, dict]) -> None:
    with INDEX_TSV.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["bill_number", "supplier", "issue_date", "total", "status",
                    "receipt", "myob_pdf", "manager_journals", "folder", "error"])
        for rec in records.values():
            w.writerow([
                rec.get("bill_number", ""),
                rec.get("supplier", ""),
                rec.get("issue_date", ""),
                rec.get("total", ""),
                rec.get("status", ""),
                "yes" if rec.get("has_receipt") else "no",
                "yes" if rec.get("has_myob_pdf") else "no",
                rec.get("manager_journals", ""),
                rec.get("folder", ""),
                rec.get("error", ""),
            ])


def _assert_logged_in(page) -> None:
    title = (page.title() or "").lower()
    if "sign in" in title:
        raise SystemExit(
            "[error] Session expired (Sign in page). Run: python3 download_bills.py login"
        )


def _money(val) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("$", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None


def _iso_date(val) -> str | None:
    if not val:
        return None
    s = str(val)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


# ------------------------------------------------------------------ login ---

def cmd_login(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    _ensure_dirs()
    with sync_playwright() as p:
        browser = _browser(p, headless=False)
        context = browser.new_context(accept_downloads=True,
                                      viewport={"width": 1400, "height": 900})
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        print("\nLog in to MYOB (complete MFA if prompted).")
        print(f"Open the business matching BUSINESS_ID={BUSINESS_ID} "
              "(config/myob_business_id.txt), then press Enter here…")
        input()
        context.storage_state(path=str(STORAGE))
        print(f"[ok] saved session → {STORAGE}")
        browser.close()


def _totp(secret: str, digits: int = 6, period: int = 30) -> str:
    """RFC 6238 TOTP, stdlib only (see manager-automation/myob-to-manager-
    migration's own "Python stdlib only" convention -- no need for a new
    dependency just for this)."""
    import base64
    import hashlib
    import hmac
    import struct
    import time as _time

    key = base64.b32decode(secret.upper().replace(" ", ""), casefold=True)
    counter = int(_time.time() // period)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def cmd_login_auto(args: argparse.Namespace) -> None:
    """Non-interactive login using MYOB_USERNAME/MYOB_PASSWORD/MYOB_TOTP_SECRET
    from the environment (source project .env first). Confirmed working
    2026-08-14 against this business's real Auth0-style universal login
    (id.myob.com) -- three real steps (email -> password -> TOTP), landing
    directly on this business's own dashboard (URL contains BUSINESS_ID)
    with no separate business-picker step observed for a single-business
    account. Only use this for read-only harvesting/export tasks -- it logs
    in with real credentials, so treat it with the same care as any other
    live-credential automation (never print the secret values, never persist
    them anywhere but the caller's own .env)."""
    import os

    from playwright.sync_api import sync_playwright

    username = os.environ.get("MYOB_USERNAME")
    password = os.environ.get("MYOB_PASSWORD")
    totp_secret = os.environ.get("MYOB_TOTP_SECRET")
    missing = [n for n, v in [("MYOB_USERNAME", username), ("MYOB_PASSWORD", password),
                               ("MYOB_TOTP_SECRET", totp_secret)] if not v]
    if missing:
        raise SystemExit(f"[error] missing env var(s): {', '.join(missing)} -- "
                          "add to .env and `source` it first, or use `login` for "
                          "interactive login instead")

    _ensure_dirs()
    with sync_playwright() as p:
        browser = _browser(p, headless=not args.headed)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1400, "height": 900})
        page = context.new_page()

        page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        page.locator("#username").fill(username)
        page.get_by_role("button", name="Sign in with password").click()
        page.wait_for_timeout(2000)
        page.locator("input[type='password']").first.fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.wait_for_timeout(3000)
        if "mfa-otp-challenge" in page.url:
            code = _totp(totp_secret)
            otp_input = page.locator("input[type='text'], input[inputmode='numeric'], input[type='tel']").first
            otp_input.fill(code)
            page.get_by_role("button", name="Continue").click()
            page.wait_for_timeout(3500)

        if "app.myob.com" not in page.url:
            shot = STATE_DIR / "login_auto_debug.png"
            page.screenshot(path=str(shot))
            raise SystemExit(f"[error] login didn't land on app.myob.com -- url={page.url}, "
                              f"screenshot saved to {shot}")

        context.storage_state(path=str(STORAGE))
        print(f"[ok] logged in and saved session → {STORAGE} ({page.url})")
        browser.close()


def cmd_probe(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    if not STORAGE.exists():
        raise SystemExit("[error] run `login` first")
    _ensure_dirs()
    with sync_playwright() as p:
        browser = _browser(p, headless=False)
        context = _context(browser)
        page = context.new_page()
        page.goto(BILLS_LIST_URL, wait_until="domcontentloaded")
        print("Confirm Bills list + date range, then Enter…")
        input()
        for a in page.locator("a[href*='/bill/']").all()[:40]:
            try:
                print(repr((a.inner_text() or "").strip()[:40]), a.get_attribute("href"))
            except Exception:
                pass
        browser.close()


# ---------------------------------------------------------------- harvest ---

def _absolute_url(page, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "/".join(page.url.split("/", 3)[:3]) + href
    return page.url.rsplit("/", 1)[0] + "/" + href


def _goto_bills_list(page) -> None:
    page.goto(BILLS_LIST_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(2)
    if page.locator("a[href*='/bill/']").count() > 0:
        return
    try:
        page.locator(SEL["purchases_menu"]).first.click(timeout=5000)
        time.sleep(0.4)
        page.get_by_role("link", name=re.compile(r"^Bills$", re.I)).first.click(timeout=5000)
        time.sleep(1)
        return
    except Exception:
        pass
    print("[warn] navigate to Purchases → Bills, then Enter…")
    input()


def _fill_date_input(page, label: str, value: str) -> bool:
    try:
        loc = page.get_by_label(re.compile(label, re.I))
        if loc.count():
            loc.first.click(timeout=3000)
            loc.first.fill("")
            loc.first.fill(value)
            loc.first.press("Tab")
            return True
    except Exception:
        pass
    try:
        lab = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I)).first
        lab.click(timeout=3000)
        box = lab.bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"] + 40, box["y"] + box["height"] / 2)
        focused = page.locator("input:focus")
        if focused.count():
            focused.first.fill("")
            focused.first.fill(value)
            focused.first.press("Tab")
            return True
        inp = lab.locator("xpath=following::input[1]")
        if inp.count():
            inp.first.fill("")
            inp.first.fill(value)
            inp.first.press("Tab")
            return True
    except Exception:
        pass
    try:
        loc = page.locator(f'input[placeholder*="{label}" i], input[aria-label*="{label}" i]')
        if loc.count():
            loc.first.fill("")
            loc.first.fill(value)
            loc.first.press("Tab")
            return True
    except Exception:
        pass
    return False


def _set_issue_date_range(page, date_from: str, date_to: str) -> None:
    print(f"[info] setting Issue from={date_from}  Issue to={date_to}")
    ok_from = _fill_date_input(page, "Issue from", date_from)
    ok_to = _fill_date_input(page, "Issue to", date_to)
    if not (ok_from and ok_to):
        inputs = page.locator('input[type="date"], input[placeholder*="date" i], '
                              'input[placeholder*="/" i], input[inputmode="numeric"]')
        visible = []
        for i in range(min(inputs.count(), 8)):
            el = inputs.nth(i)
            try:
                if el.is_visible():
                    visible.append(el)
            except Exception:
                pass
        if len(visible) >= 2:
            for el, val in ((visible[0], date_from), (visible[1], date_to)):
                el.click()
                el.fill("")
                el.fill(val)
                el.press("Tab")
            ok_from = ok_to = True
    if not (ok_from and ok_to):
        print(f"[warn] set Issue from={date_from}, Issue to={date_to} manually, then Enter…")
        input()
    else:
        for name in ("Apply", "Search", "Update", "Filter"):
            btn = page.get_by_role("button", name=re.compile(rf"^{name}$", re.I))
            if btn.count() and btn.first.is_visible():
                try:
                    btn.first.click(timeout=2000)
                except Exception:
                    pass
        time.sleep(2)
        print("[ok] date range set")


def _collect_bill_hrefs(page) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    anchors = page.locator("a[href*='/bill/']")
    for i in range(anchors.count()):
        a = anchors.nth(i)
        try:
            href = a.get_attribute("href") or ""
            text = (a.inner_text() or "").strip().replace("\n", " ")
        except Exception:
            continue
        if not href or not BILL_HREF_RE.search(href):
            continue
        if href.startswith(("javascript:", "mailto:")):
            continue
        url = _absolute_url(page, href)
        found.setdefault(url, text)
    return list(found.items())


def cmd_harvest(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    if not STORAGE.exists():
        raise SystemExit("[error] run `login` first")
    _ensure_dirs()
    existing = _load_bills()
    print(f"[info] already tracked: {len(existing)} bills")
    date_from = args.date_from
    date_to = args.date_to or date.today().strftime("%d/%m/%Y")

    with sync_playwright() as p:
        browser = _browser(p, headless=args.headless)
        context = _context(browser)
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        time.sleep(1)
        _assert_logged_in(page)
        _goto_bills_list(page)
        _set_issue_date_range(page, date_from, date_to)
        try:
            status = page.get_by_label(re.compile(r"Status", re.I))
            if status.count():
                status.first.click()
                page.get_by_role("option", name=re.compile(r"^All$", re.I)).first.click(timeout=3000)
                time.sleep(1)
        except Exception:
            pass

        stable_rounds = 0
        last_count = 0
        for round_i in range(args.max_scrolls):
            for url, text in _collect_bill_hrefs(page):
                if url not in existing:
                    existing[url] = {
                        "url": url,
                        "list_text": text,
                        "bill_number": text,
                        "supplier": "",
                        "status": "pending",
                        "folder": "",
                        "error": "",
                    }
            gained = len(existing) - last_count
            print(f"[harvest] round {round_i + 1}: {len(existing)} urls (+{gained})")
            stable_rounds = stable_rounds + 1 if gained == 0 else 0
            last_count = len(existing)
            if stable_rounds >= 3:
                print("[harvest] stable — stopping")
                break
            btn = page.get_by_role("button", name=re.compile(r"Load more", re.I))
            if btn.count() and btn.first.is_visible():
                try:
                    btn.first.click(timeout=5000)
                    time.sleep(args.pause)
                    continue
                except Exception:
                    pass
            page.mouse.wheel(0, 4000)
            time.sleep(args.pause)

        _rewrite_bills(existing)
        context.storage_state(path=str(STORAGE))
        print(f"[ok] wrote {len(existing)} bills → {BILLS_JSONL}")
        browser.close()


def cmd_reset(args: argparse.Namespace) -> None:
    _ensure_dirs()
    if BY_BILL_DIR.exists():
        shutil.rmtree(BY_BILL_DIR)
        print(f"[ok] removed {BY_BILL_DIR}")
    BY_BILL_DIR.mkdir(parents=True, exist_ok=True)
    legacy = ARCHIVE / "pdfs"
    if legacy.exists():
        shutil.rmtree(legacy)
        print(f"[ok] removed {legacy}")
    if INDEX_TSV.exists():
        INDEX_TSV.unlink()
    records = _load_bills()
    for rec in records.values():
        rec.update({"status": "pending", "folder": "", "error": "",
                    "supplier": "", "issue_date": "", "total": "",
                    "has_receipt": False, "has_myob_pdf": False,
                    "manager_journals": ""})
        rec["bill_number"] = rec.get("bill_number") or rec.get("list_text") or ""
        rec.pop("files", None)
        rec.pop("ref", None)
    _rewrite_bills(records)
    print(f"[ok] reset {len(records)} bills → pending")


# ------------------------------------------------------- supplier lookup ---

def _load_contacts_cache() -> dict[str, dict]:
    """Load cached contacts, keeping only API-sourced entries.

    Entries without contact_type came from screen reading and are untrustworthy.
    """
    if not CONTACTS_JSON.exists():
        return {}
    try:
        raw = json.loads(CONTACTS_JSON.read_text())
    except Exception:
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("contact_type")}


def _save_contacts_cache(cache: dict[str, dict]) -> None:
    CONTACTS_JSON.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n")


def _fetch_contacts(context, contact_type: str, headers: dict) -> dict[str, dict]:
    """Page through MYOB's contact option list; returns {id: {...}}.

    The BFF requires the app's own bearer token, so `headers` must come from a
    request the page itself made.
    """
    out: dict[str, dict] = {}
    offset = 0
    while True:
        url = f"{BFF_BASE}/contact/load_contact_options?contactType={contact_type}&offset={offset}"
        try:
            resp = context.request.get(url, headers=headers, timeout=20000)
            if not resp.ok:
                break
            data = resp.json()
        except Exception:
            break
        for opt in data.get("contactOptions") or []:
            cid = str(opt.get("id"))
            if cid:
                out[cid] = {
                    "name": opt.get("displayName"),
                    "contact_type": opt.get("contactType"),
                    "business_number": opt.get("businessNumber"),
                    "display_id": opt.get("displayId"),
                }
        pag = data.get("pagination") or {}
        if not pag.get("hasNextPage"):
            break
        offset = pag.get("offset") or (offset + 20)
    return out


_DOM_NOISE = re.compile(r"^(this is required|required|select|choose|none|\*|-)?$|required", re.I)


def _dom_supplier(page) -> str:
    """Read the Supplier field value from the bill form (label may be 'Supplier*')."""
    try:
        value = page.evaluate(
            """() => {
              const clean = (s) => (s || '').replace(/\\*/g, '').replace(/:$/, '').trim();
              const val = (el) => {
                if (!el) return '';
                if ('value' in el && el.value) return String(el.value).trim();
                const t = (el.innerText || el.textContent || '').trim();
                return t.split('\\n')[0].trim();
              };
              const labels = Array.from(document.querySelectorAll('label, span, div, dt'));
              for (const lab of labels) {
                if (clean(lab.textContent).toLowerCase() !== 'supplier') continue;
                // input associated by for=
                const forId = lab.getAttribute && lab.getAttribute('for');
                if (forId) {
                  const target = document.getElementById(forId);
                  const v = val(target);
                  if (v) return v;
                }
                // nearest following input/select/combobox in the same field group
                const scope = lab.closest('div, fieldset') || document;
                const cand = scope.querySelector(
                  'input:not([type=hidden]), select, [role=combobox], [class*="value"]'
                );
                const v2 = val(cand);
                if (v2 && clean(v2).toLowerCase() !== 'supplier') return v2;
                const nxt = lab.nextElementSibling;
                const v3 = val(nxt && (nxt.querySelector('input, select') || nxt));
                if (v3) return v3;
              }
              return '';
            }"""
        ) or ""
    except Exception:
        return ""
    value = value.strip()
    return "" if _DOM_NOISE.search(value) else value


# --------------------------------------------------------- bill extraction --

def _due_date(bill: dict) -> str | None:
    issue = _iso_date(bill.get("issueDate"))
    if not issue:
        return None
    term = bill.get("expirationTerm")
    try:
        days = int(float(bill.get("expirationDays") or 0))
    except (TypeError, ValueError):
        days = 0
    if term == "InAGivenNumberOfDays":
        return (date.fromisoformat(issue) + timedelta(days=days)).isoformat()
    if term in ("CashOnDelivery", "PrePaid"):
        return issue
    return None


def _normalize_lines(bill: dict, accounts: dict, tax_codes: dict, items: dict) -> list[dict]:
    lines = []
    for i, ln in enumerate(bill.get("lines") or [], start=1):
        acct = accounts.get(str(ln.get("accountId")), {})
        tax = tax_codes.get(str(ln.get("taxCodeId")), {})
        item = items.get(str(ln.get("itemId")), {})
        ex = _money(ln.get("taxExclusiveAmount"))
        tx = _money(ln.get("taxAmount"))
        lines.append({
            "line_number": i,
            "myob_line_id": ln.get("id"),
            "type": ln.get("type"),
            "description": ln.get("description"),
            "account_id": ln.get("accountId") or None,
            "account_code": acct.get("code"),
            "account_name": acct.get("name"),
            "account_type": acct.get("account_type"),
            "item_id": ln.get("itemId") or None,
            "item_name": item.get("name"),
            "job_id": ln.get("jobId") or None,
            "units": _money(ln.get("units")),
            "unit_price": _money(ln.get("unitPrice")),
            "discount_percent": _money(ln.get("discount")),
            "tax_code_id": ln.get("taxCodeId") or None,
            "tax_code": tax.get("name"),
            "tax_rate": tax.get("rate"),
            "amount_ex_tax": ex,
            "tax_amount": tx,
            "amount_inc_tax": round((ex or 0) + (tx or 0), 2) if (ex is not None or tx is not None) else None,
        })
    return lines


def _normalize_payments(history: list[dict]) -> list[dict]:
    payments = []
    for ev in history or []:
        status = str(ev.get("status") or "")
        event = str(ev.get("businessEventType") or "")
        if "payment" not in status.lower() and "pay" not in event.lower():
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
            "business_event_type": event or None,
            "status": status or None,
            "description": desc or None,
        })
    return payments


def _build_bill_record(
    *,
    url: str,
    bill_payload: dict,
    history: list[dict],
    supplier: dict,
    accounts: dict,
    tax_codes: dict,
    items: dict,
    folder: Path,
    receipt: Path | None,
    myob_pdf: Path | None,
    mgr_match: dict,
) -> dict:
    bill = bill_payload.get("bill") or {}
    lines = _normalize_lines(bill, accounts, tax_codes, items)
    payments = _normalize_payments(history)

    # Attach the matched Manager journal onto each payment (1:1).
    pmt_by_ref = {
        str(pm.get("payment_reference_no") or ""): pm
        for pm in (mgr_match.get("payments") or [])
    }
    for pmt in payments:
        linked = pmt_by_ref.get(str(pmt.get("reference_no") or ""))
        journal = (linked or {}).get("journal")
        pmt["manager_journal"] = (
            {
                "key": journal.get("key"),
                "reference": journal.get("reference"),
                "narration": journal.get("narration"),
                "date": journal.get("date"),
                "amount": journal.get("amount"),
                "match_method": journal.get("match_method"),
                "match_confidence": journal.get("match_confidence"),
            }
            if journal else None
        )

    subtotal = round(sum(l["amount_ex_tax"] or 0 for l in lines)
                     + (_money(bill.get("taxExclusiveFreightAmount")) or 0), 2)
    tax_total = round(sum(l["tax_amount"] or 0 for l in lines)
                      + (_money(bill.get("freightTaxAmount")) or 0), 2)
    total = round(subtotal + tax_total, 2)
    paid = _money(bill.get("amountPaid")) or 0.0

    files = {}
    if receipt:
        files["receipt"] = receipt.name
    if myob_pdf:
        files["myob_pdf"] = myob_pdf.name

    purchase = mgr_match.get("purchase")
    return {
        "schema_version": 3,
        "source": "myob_business",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "folder": str(folder.relative_to(ARCHIVE)),
        "bill": {
            "myob_business_id": BUSINESS_ID,
            "myob_bill_id": bill.get("id"),
            "myob_uid": bill.get("uid"),
            "url": url,
            "number": bill.get("billNumber"),
            "layout": bill.get("layout"),
            "supplier": {
                "myob_id": bill.get("supplierId"),
                "name": supplier.get("name"),
                "business_number": supplier.get("business_number"),
                "address": bill.get("supplierAddress") or None,
            },
            "supplier_invoice_number": bill.get("supplierInvoiceNumber") or None,
            "memo": bill.get("memo") or None,
            "note": bill.get("note") or None,
            "issue_date": _iso_date(bill.get("issueDate")),
            "due_date": _due_date(bill),
            "payment_term": {
                "term": bill.get("expirationTerm"),
                "days": bill.get("expirationDays"),
            },
            "status": bill.get("status"),
            "is_tax_inclusive": bill.get("isTaxInclusive"),
            "is_reportable": bill.get("isReportable"),
            "currency": "AUD" if not bill.get("isForeignCurrency") else None,
            "totals": {
                "subtotal_ex_tax": subtotal,
                "tax": tax_total,
                "total_inc_tax": total,
                "amount_paid": paid,
                "amount_due": round(total - paid, 2),
                "freight_ex_tax": _money(bill.get("taxExclusiveFreightAmount")),
                "freight_tax": _money(bill.get("freightTaxAmount")),
            },
            "lines": lines,
            "manager_journal": (
                {
                    "key": purchase.get("key"),
                    "reference": purchase.get("reference"),
                    "narration": purchase.get("narration"),
                    "date": purchase.get("date"),
                    "amount": purchase.get("amount"),
                    "match_method": purchase.get("match_method"),
                    "match_confidence": purchase.get("match_confidence"),
                }
                if purchase else None
            ),
        },
        "payments": payments,
        "activity_history": [
            {
                "date": _iso_date(ev.get("date")),
                "status": ev.get("status"),
                "description": ev.get("description"),
                "myob_journal_id": ev.get("journalId"),
                "reference_no": ev.get("referenceNo"),
                "business_event_type": ev.get("businessEventType"),
            }
            for ev in history or []
        ],
        "attachments": {
            "attachment_id": bill_payload.get("attachmentId"),
            "in_tray_id": bill_payload.get("inTrayId"),
            "uploaded_date": (bill_payload.get("inTrayDocument") or {}).get("uploadedDate"),
            "documents": bill_payload.get("attachments") or [],
        },
        "files": files,
        "manager": {
            "match_strategy": (
                "purchase: bill_number + date + amount + supplier; "
                "payment: 'Payment for {bill}' + payment_ref + date + amount"
            ),
            "purchase": purchase,
            "payments": mgr_match.get("payments") or [],
            "matched_journals": mgr_match.get("matched_journals") or [],
        },
        "raw": {"bill": bill},
    }


def _download_receipt(page, folder: Path, doc_name: str | None, doc_urls: list[str]) -> Path | None:
    """Save the linked source document: direct pre-signed URL first, else UI click."""
    ext = Path(doc_name).suffix if doc_name else ""
    # 1) A full-document URL observed in network traffic (not the thumbnail).
    for url in doc_urls:
        try:
            resp = page.context.request.get(url, timeout=30000)
            if not resp.ok:
                continue
            body = resp.body()
            if len(body) < 1000:
                continue
            suffix = ext or (".pdf" if body[:4] == b"%PDF" else ".bin")
            path = folder / f"receipt{suffix}"
            path.write_bytes(body)
            return path
        except Exception:
            continue

    # 2) Fall back to split view + download control.
    split = page.locator(SEL["split_view"])
    if split.count():
        try:
            split.first.click(timeout=5000)
            time.sleep(1.2)
        except Exception:
            pass
    for loc in (page.locator(SEL["download_attach"]),
                page.get_by_role("button", name=re.compile(r"download", re.I)),
                page.get_by_role("link", name=re.compile(r"download", re.I))):
        if not loc.count():
            continue
        try:
            with page.expect_download(timeout=20000) as di:
                loc.first.click()
            download = di.value
            suffix = Path(download.suggested_filename).suffix or ext or ".pdf"
            path = folder / f"receipt{suffix}"
            download.save_as(path)
            return path
        except Exception:
            continue
    return None


def _download_myob_pdf(page, folder: Path) -> Path | None:
    loc = page.locator(SEL["view_pdf"])
    if not loc.count():
        loc = page.get_by_role("button", name=re.compile(r"view pdf", re.I))
    if not loc.count():
        return None
    path = folder / "myob_bill.pdf"
    try:
        with page.expect_download(timeout=25000) as di:
            loc.first.click()
            time.sleep(0.5)
            for name in ("Export", "Download", "OK", "Save"):
                btn = page.get_by_role("button", name=re.compile(rf"^{name}$", re.I))
                if btn.count() and btn.first.is_visible():
                    try:
                        btn.first.click(timeout=3000)
                        break
                    except Exception:
                        pass
        di.value.save_as(path)
        return path
    except Exception:
        pass
    try:
        with page.context.expect_page(timeout=15000) as pi:
            loc.first.click()
        pdf_page = pi.value
        pdf_page.wait_for_load_state("domcontentloaded")
        time.sleep(1.5)
        try:
            with pdf_page.expect_download(timeout=8000) as di:
                pdf_page.get_by_role(
                    "button", name=re.compile(r"download|export", re.I)
                ).first.click()
            di.value.save_as(path)
        except Exception:
            pdf_page.pdf(path=str(path))
        pdf_page.close()
        return path if path.exists() else None
    except Exception:
        return None


def cmd_download(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    if not STORAGE.exists():
        raise SystemExit("[error] run `login` first")
    records = _load_bills()
    if not records:
        raise SystemExit("[error] no bills — run `harvest` first")

    print("[info] building Manager journal index…")
    mgr_index = manager_index.build_index()
    print(f"[info] Manager refs indexed: {len(mgr_index)}")

    pending = [r for r in records.values() if r.get("status") not in ("ok", "partial")]
    if args.retry_errors:
        pending = [r for r in records.values() if r.get("status") != "ok"]
    if args.limit:
        pending = pending[: args.limit]
    print(f"[info] to process: {len(pending)}  (tracked={len(records)})")
    _ensure_dirs()

    contacts = _load_contacts_cache()
    accounts: dict[str, dict] = {}
    tax_codes: dict[str, dict] = {}
    items: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = _browser(p, headless=args.headless)
        context = _context(browser)
        page = context.new_page()

        page.goto(START_URL, wait_until="domcontentloaded")
        time.sleep(1.5)
        _assert_logged_in(page)

        captured: dict[str, object] = {}
        doc_urls: list[str] = []
        # The BFF is bearer-authenticated; borrow the app's own headers.
        auth_headers: dict[str, str] = {}

        def on_request(request):
            try:
                if "sme-web-bff" not in request.url:
                    return
                headers = request.all_headers()
                token = headers.get("authorization")
                if not token:
                    return
                auth_headers.clear()
                auth_headers.update({
                    "authorization": token,
                    "accept": "application/json",
                    "content-type": "application/json",
                    "origin": "https://app.myob.com",
                    "referer": "https://app.myob.com/",
                })
                for extra in ("x-myobapi-idtoken", "region"):
                    if headers.get(extra):
                        auth_headers[extra] = headers[extra]
            except Exception:
                pass

        page.on("request", on_request)

        def on_response(response):
            try:
                url = response.url
                low = url.lower()
                if "/bill/load_bill/" in low:
                    captured["bill"] = response.json()
                elif "/bill/load_bill_activity_history/" in low:
                    captured["history"] = response.json()
                elif "/contact/load_contact_options" in low:
                    for opt in (response.json().get("contactOptions") or []):
                        cid = str(opt.get("id"))
                        if cid and cid not in contacts:
                            contacts[cid] = {
                                "name": opt.get("displayName"),
                                "contact_type": opt.get("contactType"),
                                "business_number": opt.get("businessNumber"),
                                "display_id": opt.get("displayId"),
                            }
                elif "/inventory/load_item_options" in low:
                    for opt in (response.json().get("itemOptions") or []):
                        iid = str(opt.get("id"))
                        if iid:
                            items[iid] = {"name": opt.get("displayName"),
                                          "code": opt.get("displayId")}
                elif "amazonaws.com" in low and "/thumbnail/" not in low:
                    doc_urls.append(url)
            except Exception:
                pass

        page.on("response", on_response)

        done = 0
        for rec in pending:
            url = rec["url"]
            done += 1
            list_text = rec.get("bill_number") or rec.get("list_text") or ""
            print(f"[{done}/{len(pending)}] {list_text or url}")
            captured.clear()
            doc_urls.clear()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Wait for MYOB's own bill payload rather than guessing at load state.
                for attempt in range(2):
                    deadline = time.time() + args.timeout
                    while "bill" not in captured and time.time() < deadline:
                        page.wait_for_timeout(250)
                    if "bill" in captured:
                        break
                    if attempt == 0:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                _assert_logged_in(page)
                if "bill" not in captured:
                    raise RuntimeError("bill payload not seen (page may not have loaded)")

                bill_payload = captured["bill"]
                bill = bill_payload.get("bill") or {}
                for opt in bill_payload.get("accountOptions") or []:
                    accounts[str(opt.get("id"))] = {
                        "code": opt.get("displayId"),
                        "name": opt.get("displayName"),
                        "account_type": opt.get("accountType"),
                    }
                for opt in bill_payload.get("taxCodeOptions") or []:
                    tax_codes[str(opt.get("id"))] = {
                        "name": opt.get("displayName"),
                        "rate": opt.get("rate"),
                        "description": opt.get("description"),
                    }

                # Supplier: cached contact list, else the on-page Supplier field.
                supplier_id = str(bill.get("supplierId") or "")
                if supplier_id and supplier_id not in contacts and auth_headers:
                    print("[info] caching MYOB contact list…")
                    fetched = _fetch_contacts(context, "Supplier", auth_headers)
                    fetched.update(_fetch_contacts(context, "Customer", auth_headers))
                    if fetched:
                        contacts.update(fetched)
                        _save_contacts_cache(contacts)
                        print(f"[ok] cached {len(contacts)} contacts → {CONTACTS_JSON}")
                    else:
                        print("[warn] contact list unavailable — using on-page Supplier field")
                supplier = dict(contacts.get(supplier_id) or {})
                if not supplier.get("name"):
                    # Screen-read value is per-bill only; never cached as a contact.
                    supplier["name"] = _dom_supplier(page) or None
                if not supplier.get("name"):
                    # Memo is often "Purchase; <Supplier Name>"
                    memo = (bill.get("memo") or "").strip()
                    m = re.match(r"(?i)^(?:purchase|bill)\s*;\s*(.+)$", memo)
                    if m:
                        supplier["name"] = m.group(1).strip() or None
                supplier_name = supplier.get("name") or "UnknownSupplier"

                number = bill.get("billNumber") or list_text or f"id{BILL_HREF_RE.search(url).group(1)}"
                folder = _bill_folder(number, supplier_name, bill.get("id"))

                docs = bill_payload.get("attachments") or []
                doc_name = docs[0].get("name") if docs else None
                # Opening split view makes MYOB fetch the full document URL.
                receipt = _download_receipt(page, folder, doc_name, list(doc_urls))
                if receipt is None and doc_urls:
                    receipt = _download_receipt(page, folder, doc_name, list(doc_urls))
                myob_pdf = _download_myob_pdf(page, folder)

                history = (captured.get("history") or {}).get("billHistory") or []
                payments_preview = _normalize_payments(history)
                # Rough total for matching before we build the full record
                bill_lines = bill.get("lines") or []
                rough_total = round(
                    sum((_money(ln.get("taxExclusiveAmount")) or 0)
                        + (_money(ln.get("taxAmount")) or 0)
                        for ln in bill_lines)
                    + (_money(bill.get("taxExclusiveFreightAmount")) or 0)
                    + (_money(bill.get("freightTaxAmount")) or 0),
                    2,
                )
                mgr_match = manager_index.match_bill(
                    mgr_index,
                    bill_number=number,
                    issue_date=_iso_date(bill.get("issueDate")),
                    total=rough_total,
                    supplier_name=supplier_name,
                    payments=payments_preview,
                )
                bill_json = _build_bill_record(
                    url=url,
                    bill_payload=bill_payload,
                    history=history,
                    supplier=supplier,
                    accounts=accounts,
                    tax_codes=tax_codes,
                    items=items,
                    folder=folder,
                    receipt=receipt,
                    myob_pdf=myob_pdf,
                    mgr_match=mgr_match,
                )
                (folder / "bill.json").write_text(
                    json.dumps(bill_json, indent=2, ensure_ascii=False) + "\n"
                )

                n_mgr = len(mgr_match.get("matched_journals") or [])
                n_pay_matched = sum(
                    1 for p in (mgr_match.get("payments") or []) if p.get("journal")
                )
                n_pay = len(payments_preview)
                rec.update({
                    "bill_number": number,
                    "supplier": supplier_name,
                    "issue_date": bill_json["bill"]["issue_date"] or "",
                    "total": bill_json["bill"]["totals"]["total_inc_tax"],
                    "folder": str(folder.relative_to(ARCHIVE)),
                    "has_receipt": bool(receipt),
                    "has_myob_pdf": bool(myob_pdf),
                    "manager_journals": n_mgr,
                    "error": "",
                })
                expected_receipt = bool(docs)
                if myob_pdf and (receipt or not expected_receipt):
                    rec["status"] = "ok"
                    if expected_receipt and not receipt:
                        rec["status"] = "partial"
                else:
                    rec["status"] = "partial"
                    rec["error"] = "; ".join(filter(None, [
                        "no receipt" if (expected_receipt and not receipt) else "",
                        "no myob pdf" if not myob_pdf else "",
                    ]))
                purch_ok = "yes" if (mgr_match.get("purchase") or {}).get("key") else "no"
                print(f"         → {folder.name}  receipt="
                      f"{'yes' if receipt else ('n/a' if not expected_receipt else 'no')}  "
                      f"myob_pdf={'yes' if myob_pdf else 'no'}  "
                      f"lines={len(bill_json['bill']['lines'])}  "
                      f"payments={n_pay_matched}/{n_pay}  "
                      f"purchase_journal={purch_ok}")
            except SystemExit:
                raise
            except Exception as e:
                rec["status"] = "error"
                rec["error"] = str(e)[:400]
                print(f"         → ERROR {e}")

            records[url] = rec
            if done % 5 == 0:
                _rewrite_bills(records)
                _write_index(records)
                _save_contacts_cache(contacts)
                context.storage_state(path=str(STORAGE))

        _rewrite_bills(records)
        _write_index(records)
        _save_contacts_cache(contacts)
        context.storage_state(path=str(STORAGE))
        counts: dict[str, int] = {}
        for r in records.values():
            counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
        print(f"[done] {counts}  folders under {BY_BILL_DIR}")
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="Interactive login; save session").set_defaults(func=cmd_login)
    p_login_auto = sub.add_parser(
        "login-auto",
        help="Non-interactive login using MYOB_USERNAME/MYOB_PASSWORD/MYOB_TOTP_SECRET from env")
    p_login_auto.add_argument("--headed", action="store_true", help="show the browser window (debugging)")
    p_login_auto.set_defaults(func=cmd_login_auto)
    sub.add_parser("probe", help="Dump bill links on Bills page").set_defaults(func=cmd_probe)
    sub.add_parser("reset", help="Reset statuses and delete bill folders").set_defaults(func=cmd_reset)

    p_harv = sub.add_parser("harvest", help="Collect bill URLs from Bills list")
    p_harv.add_argument("--headless", action="store_true")
    p_harv.add_argument("--max-scrolls", type=int, default=500)
    p_harv.add_argument("--pause", type=float, default=1.2)
    p_harv.add_argument("--date-from", default=DATE_FROM)
    p_harv.add_argument("--date-to", default="")
    p_harv.set_defaults(func=cmd_harvest)

    p_dl = sub.add_parser("download", help="Download receipt + MYOB PDF + bill.json per bill")
    p_dl.add_argument("--headless", action="store_true")
    p_dl.add_argument("--limit", type=int, default=0)
    p_dl.add_argument("--timeout", type=float, default=25.0,
                      help="Seconds to wait for MYOB's bill payload")
    p_dl.add_argument("--retry-errors", action="store_true")
    p_dl.set_defaults(func=cmd_download)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted] progress saved periodically — re-run to resume", file=sys.stderr)
        sys.exit(130)
