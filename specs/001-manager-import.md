# Spec 001 — Manager.io import formats and pitfalls

| | |
|---|---|
| **Audience** | Anyone recreating or extending migration scripts |
| **Ops guide** | [manager-import.md](../reference/manager-import.md) |

Hard-won Manager format facts. Prefer this over rediscovering behaviour in the UI
or by poking protobuf blobs. Instance-specific variances live in
[docs/MIGRATION_DIFFS.md](../../../../docs/MIGRATION_DIFFS.md).

---

## 1. Manager storage model

- Business file is SQLite: `~/Documents/Manager.io/Businesses/<Name>.manager`.
- Domain objects live in `Objects(Key, ContentType, Content)` with **protobuf**
  `Content`. Images live in `Images(Key, ContentType, Content, Timestamp)` keyed by
  the parent transaction Key.
- **Quit Manager** before any write script; the app holds a write lock.
- Content-type GUIDs used by scripts (Manager ~26.x):

| Kind | ContentType GUID |
|---|---|
| Balance sheet account | `6ef13e42-ad89-4d42-9480-546e0c04a411` |
| P&L account | `26b9e4a5-ce10-4f30-94c7-23a1ca4428f9` |
| Supplier | `6d2dc48d-2053-4e45-8330-285ebd431242` |
| Customer | `ec37c11e-2b67-49c6-8a58-6eccb7dd75ee` |
| Purchase invoice | `58b9eb90-f6b8-4abc-8ea1-12fd77b8336e` |
| Sales invoice | `ad12b60b-23bf-4421-94df-8be79cef533e` |
| Payment | `79f99d26-e43a-4ecb-a9c9-0774601a9b2e` |
| Receipt | `7662b887-c8d8-486e-98fd-f9dbcd41c6dc` |
| Journal entry | `5ea52bc4-90ae-4e4a-aec4-ef1224b279ad` |
| **Builtin Accounts payable** (control) | `dac7ba37-0ccd-45e5-906e-548e6c50df37` |
| **Builtin Accounts receivable** (control) | `d1489e95-bb28-4f5d-b42e-67d3291b3893` |
| **Builtin Retained earnings** (auto) | `74dfd025-d68e-4a99-9c78-5d43e17c0e09` |

Builtin AR / AP / RE appear when the relevant tabs/features are enabled. In the DB
they are special objects with **Key == ContentType**; optional **Code** lives in
protobuf **f12** (not f11/f17). Chart accounts also named `1-1800` / `2-1800` /
`3-1600` are separate manual COA rows — **do not** use chart AR/AP for invoice
matching, and **do not** keep a duplicate chart Retained earnings (see §2.1).

Account columns in Batch Create/Update are usually **GUIDs**. Discover live headers via
**Batch Update** on one manually created row; Batch Create uses the same columns minus
`Key`. Prefer applying those TSVs with [`scripts/apply_manager_api.py`](../../../../scripts/apply_manager_api.py)
instead of pasting (see §1.1).

### 1.1 Form API apply (default write path)

Manager API v2 form endpoints (`/receipt-form`, `/payment-form`,
`/journal-entry-form`, `/purchase-invoice-form`, `/sales-invoice-form`, …) accept the
same GUID-oriented payloads the Batch UI uses.

| Op | Rule |
|---|---|
| **Update** | `GET /{entity}-form/{key}` → merge intended fields → `PUT` **full** body. Partial bodies wipe fields (seen: Receipt `ReceivedIn` cleared → bank −$15,510). |
| **Create** | `POST /{entity}-form` with a complete body. Idempotent skip when `Reference` already exists. |
| **Auth** | `X-API-KEY` + base URL `…/api2` from workspace `.env`. Desktop port is random per launch. |

Client: [`scripts/lib_manager_api.py`](../../../../scripts/lib_manager_api.py). Runner:
[`scripts/apply_manager_api.py`](../../../../scripts/apply_manager_api.py) (TSV or JSONL;
`--dry-run`, `--limit`, `--continue-on-error`). Do **not** shell the bash `manager`
CLI for bulk writes on macOS `/bin/bash` 3.2 (`${var^^}` breaks).

Still use SQLite for COA seed and image attach. Clipboard Batch paste is a **legacy
fallback** only.

### GUID encoding pitfall (Batch Update / protobuf)

Account GUIDs in protobuf are often two **fixed64** fields (tags `0x09` then `0x11`),
.Net Guid byte order. When decoding hex dumps, **do not** treat the `0x11` tag byte as
part of the UUID — that produces a plausible but **wrong** GUID that Manager may still
accept on Batch Update, parking amounts on a phantom account. Always verify the target
GUID exists as `Objects.Key` (or matches a known builtin constant) before pasting.

Use `guid_msg()` from `reconcile_manager_to_myob.py` / the same logic in builders.

---

## 2. Chart of accounts

- **No Batch Create** for COA. `seed_manager_coa.py` inserts protobuf rows.
- Protobuf (Manager 26.4 — confirmed against native UI-created accounts):
  - **f1 = Name** (bare title), **f3 = Group GUID**
  - Balance sheet: **f17 = Code**
  - P&L: **f11 = Code**, **f10 = sort**, **f14 = 1**
  - **Pitfall:** P&L Code written as f17 (BS layout) makes Sales/Purchase Invoice
    Account dropdowns show raw GUIDs instead of names. `seed_manager_coa.py`
    encodes the two types differently; do not “simplify” them to one layout.
- **Never put the account number in Name.** Manager has a separate Code field
  (Settings → Chart of Accounts → Edit). Same for builtins: Name =
  `Accounts receivable`, Code = `1-1800`.
- `seed_manager_coa.py` and `load_account_guids()` honour
  [`config/builtin_account_map.tsv`](../../../../config/builtin_account_map.tsv) (§2.2).
- Do not use localisation subgroup header `c03d1921-…` as a postable account — every
  journal line resolves to Suspense.
- Built-in section GUIDs (Assets / Liabilities / Equity / …) are baked into Manager;
  wrong group GUID mis-files every account.

### 2.2 Builtin ↔ MYOB account mapping (required before / at COA seed)

Manager ships **control accounts** that are not ordinary chart rows. Seeding the MYOB
Categories list without an explicit map creates **duplicate equity/liability/P&L
lines**, breaks invoice clearing, and confuses Trial Balance compare.

This step is **mandatory** for every new business — do not hardcode one company’s
account numbers into the seeder.

#### Control accounts to decide (AU / Manager ~26.x)

| Manager control | Typical Key / ContentType | Appears when | Role |
|---|---|---|---|
| **Accounts receivable** | Key==CT `d1489e95-…` | Sales Invoices enabled | Invoice control — only this GUID clears SIs |
| **Accounts payable** | Key==CT `dac7ba37-…` | Purchase Invoices enabled | Invoice control — only this GUID clears PIs |
| **Retained earnings** | Key==CT `74dfd025-…` | Always | Auto P&L rollup + postable equity |
| **GST payable** | BS account (often **no Code**); example `010176ed-…` | Tax codes / GST | Report **rollup** of GST collected − GST paid (not a MYOB-style posting account) |
| **Payroll liabilities** | BS account (often **no Code**); example `8a3514e3-…` | Employees / payslips | Payroll control / rollup |
| **Foreign exchange gains (losses)** | P&L control | Multi-currency | FX revaluation P&L |

True builtins use **Key == ContentType**. GST payable / Payroll liabilities are
auto-created **BalanceSheetAccount** rows (Key ≠ ContentType) that still must not be
confused with seeded MYOB detail accounts. FX may be absent until multi-currency is
on — still record a decision so a later enable does not surprise the chart.

Optional Code on builtins lives in protobuf **f12** (not f11/f17).

#### Decision per control (exactly one)

| Decision | Meaning | Seed / post-load effect |
|---|---|---|
| **MAP** | This MYOB account *is* the Manager control | Do **not** seed that MYOB code as a chart row; set the control’s **Code** to the MYOB number; `load_account_guids()` resolves that code/name → control GUID; after any accidental seed, Batch Update journal/payment/receipt lines from chart GUID → control GUID, then **delete** the chart duplicate once balance is \$0 |
| **UNMAPPED** | Keep Manager control **and** keep MYOB detail account(s) separate | Seed MYOB details normally; never post migration history to the control; TB compare treats the control as Manager-only (rollup / unused) |
| **ABSENT** | Control not present in this `.manager` (feature off) | No action; if the feature is enabled later, re-run this gate before posting |

**Never** invent a MAP that collapses several MYOB details into one control unless the
owner explicitly wants that presentation (e.g. do **not** map both `GST collected`
and `GST paid` onto **GST payable** — Manager already rolls them up).

#### Agentic process (propose → confirm → apply)

1. **Inventory Manager controls** in the target `.manager` (empty or pre-seed):
   - Objects with `Key == ContentType` that look like AR / AP / RE.
   - BS/P&L accounts named `GST payable`, `Payroll liabilities`, `Foreign exchange
     gains (losses)` (and localised variants), especially those with **no Code**.
2. **Inventory MYOB candidates** from `out/manager/chart_of_accounts.tsv` (or
   Categories list): name/code heuristics — receivable/payable, retained earnings,
   GST collected/paid/payable, PAYG/super/payroll, foreign/exchange/forex.
3. **Propose** a row per control: best MAP candidate(s) or UNMAPPED/ABSENT, with
   rationale (name match, single control vs MYOB split, invoice-clearing need).
4. **Human confirms** every row (this is the gate — scripts must not guess MAP for
   GST/payroll/FX). Write
   [`config/builtin_account_map.tsv`](../../../../config/builtin_account_map.tsv):

   ```
   manager_control	manager_key	decision	myob_code	myob_name	notes
   Accounts receivable	d1489e95-…	MAP	1-1800	Accounts receivable	Invoice control
   GST payable	010176ed-…	UNMAPPED			Rollup; keep 2-2200/2-2400
   ```

5. **Apply before seed** (or immediately after if correcting a live file):
   - Seeder **skips** every `myob_code` with `decision=MAP`.
   - After seed: Edit each MAP’d control → set **Code** = `myob_code` (Name stays the
     plain Manager title).
   - `load_account_guids()` aliases each MAP’d code/name → `manager_key`.
   - If a chart duplicate already exists for a MAP’d control: Journal (and
     Payment/Receipt) **Batch Update** chart GUID → `manager_key`, delete chart row,
     set Code on the control (same pattern as Retained earnings below).
6. **Reconcile**: Manager-only controls (`UNMAPPED` rollups such as GST payable)
   must be skipped or listed as structural — they will not appear as MYOB TB codes.

#### Invoice clearing (AR / AP) — MAP is mandatory

| Use | GUID | Clears invoices? |
|---|---|---|
| Receipts against Sales Invoices | Builtin AR + customer + SI | **Yes** |
| Payments against Purchase Invoices | Builtin AP + supplier + PI | **Yes** |
| Manual chart AR/AP rows | Extra BS accounts | **No** |

Always **MAP** MYOB AR/AP (typically `1-1800` / `2-1800`) to the builtins. Chart
duplicates break Balance due.

#### Retained earnings — MAP + consolidate

| | Built-in RE | Chart `3-1600 Retained earnings` |
|---|---|---|
| Role | Mandatory auto account | Extra COA row from MYOB import |
| Deletable? | **No** | Yes, once balance is \$0 |

If RE was seeded before the map existed:

1. Journal **Batch Update** every line on chart RE → builtin RE GUID.
2. Delete chart RE.
3. Edit builtin RE → Code `3-1600` (optionally delete unused chart `3-1800` Current
   year earnings).

**Verify whether Manager auto-derives RE on your own instance before deciding
what to do with MYOB's `01/07/YYYY` End of Year Adjustment closes — don't
assume either way.** Confirmed on at least one instance that Manager does
**not** run a year-end close automatically: the source system's own closing
journals (`Dr Current year earnings / Cr Retained earnings`, one per
fiscal year) had to be migrated explicitly, or the equity section stayed
wrong indefinitely, compounded by a separate report-level effect (a
cumulative Trial Balance can additionally *compute* a Retained Earnings
display figure by re-summing all-time P&L, independent of what's actually
posted) — see [manager-import.md's Hard-won facts](../SKILL.md#hard-won-manager-facts)
and [live-trial-balance.md](../reference/live-trial-balance.md) for the
full mechanism and how to tell the two effects apart numerically before
choosing whether to migrate, skip, or reverse these closing journals.

#### GST payable / Payroll liabilities / Foreign exchange — usual AU MYOB choice

| Control | Usual decision for MYOB Business Lite | Why |
|---|---|---|
| **GST payable** | **UNMAPPED** | MYOB posts **GST collected** + **GST paid** (and deferred GST) as separate liabilities. Manager’s GST payable is a **TB/BS rollup**. Keep MYOB detail accounts; do not seed a second “GST payable”. |
| **Payroll liabilities** | **UNMAPPED** | MYOB keeps PAYG / Super / deductions as detail. Manager’s control is for in-app payslips. If STP runs outside Manager (e.g. e-PayDay), leave the control unused. |
| **Foreign exchange gains (losses)** | **ABSENT** or **UNMAPPED** | Only if multi-currency is on. Most AU single-currency Lite files never need it; default P&L wipe during seed may remove a stub — record ABSENT. |

The confirmed map lives in `config/builtin_account_map.tsv`.

---

## 3. Bank accounts

- COA “bank” account ≠ **Bank and Cash Account**. Statement import and Actual balance
  require the latter.
- Journals **do not** set bank Actual balance. Opening must use Starting balance (or an
  opening Receipt workaround).
- `build_opening_balances.py` excludes bank lines and offsets retained earnings.
- **Start Date** (business) unlocks Starting balance. **Lock Date** does not.

### Payment / receipt line protobuf (verification)

Top-level field **11** = each allocation line:

| Subfield | Meaning |
|---|---|
| 2 | Account GUID (two fixed64 = .NET Guid byte order) |
| 3 | AccountsReceivableCustomer GUID (receipts) |
| 4 | AccountsReceivableSalesInvoice GUID (receipts) |
| 7 | AccountsPayableSupplier GUID (payments) |
| 8 | PurchaseInvoice GUID (payments) |
| 18 | Amount decimal (`f1` coefficient, `f3` scale marker; odd marker ⇒ negative) |

Receipt top-level: **f3=1** + **f4** customer GUID often appear when Paid by = Customer
(optional; line-level customer/SI is what clears invoices).

**Paid by** can stay Other while line links still clear invoices.

---

## 4. Purchase invoices

- Source of truth: Playwright harvest → `exports/myob/bills/by_bill/<id>/bill.json`
  (see [receipts.md](../reference/receipts.md)).
- Manager Reference = MYOB bill number + disambiguating suffix when needed.
- **ClosedInvoice** must be Batch-Updated to `FALSE` for payment matching; DB-only
  clearing of HideBalanceDue is insufficient.
- Attachments: write JPEG to `Images` after import (`attach_purchase_images.py`).
  Batch Create cannot carry PDFs; Batch Update’s Images column is not usable.
- Missing/zero unit prices must be derived from amount/qty before import.

---

## 5. Sales invoices + AR receipt linking

Mirror of the purchase path. Harvest detail: [003-sales-invoices.md](003-sales-invoices.md).

### Import pitfalls

- Paste **Sales Invoices → Batch Create once**. Do **not** paste reopen TSVs as
  Batch Create — that creates empty duplicate invoices (Reference only).
- Prefer identifying duplicates by **Key**, not Reference.
- `ClosedInvoice` reopen alone does not clear balances if receipts post outside builtin AR.
- **AmountsIncludeTax** is protobuf **f8** on Sales Invoices, **f7** on Purchase Invoices.
  SQLite reconstructors must use the matching field or tax-inclusive SIs look
  overstated.

### Receipt → SI linking

```bash
python3 scripts/build_ar_receipt_si_links.py
# Receipts → Batch Update → ar_receipts_si_link_batch_update.tsv
```

Match path: harvest `invoice.payments[].reference_no` (CP*/CR*) → bank
`matchedJournals[].eventId` → Manager receipt by date±1 + amount. Receipt **Reference**
is empty on bank imports — never join on it.

Each line must set:

- `Account` = **builtin AR** (`d1489e95-…`)
- `AccountsReceivableCustomer` = customer GUID
- `AccountsReceivableSalesInvoice` = SI Reference (or GUID)

Multi-invoice bank deposits → multi-line receipts (preserve all lines on Batch Update).

---

## 6. Bank categorization design

### Round 1 — `build_bank_categorization.py`

Join imported Payments/Receipts (by date/amount/description) to
`bank_match_suggestions.tsv` / `bank_unmatched.tsv`:

- Unique bill payment → builtin AP + supplier + PI Reference.
- Special descriptions → fee / director / super / ATO / interest / transfers.
- Else → suspense.

Also emits **director advance clearing journals** for MYOB payments funded from a
director-advance liability (no bank line). **One invoice per journal entry** —
Manager truncates multiple `PurchaseInvoice` lines on one Batch Create row sharing
a Reference.

Ambiguous recycled bill numbers: resolve with `(payment_ref, bill_number, supplier)`
→ Manager Reference map from the archive.

### Round 2 — `build_bank_suspense_recategorization.py`

For each suspense line, match a MYOB journal that moves the same amount through
the bank COA on date ±1 day (consume each journal once):

| Method | When | Effect |
|---|---|---|
| `invoice_split` | Journal ref → archive payments summing to amount | Multi-invoice AP lines |
| `ap_supplier_fifo` | Touches plain AP; supplier known | Builtin AP + supplier, no invoice |
| `journal_accounts` | Other balanced non-bank lines | Copy expense/liability/AR splits |
| remain suspense | No journal / unresolved AP supplier | Manual |

**Never** allocate bank payments or receipts to a manual chart AR/AP row when
invoices live under the builtins. `load_account_guids()` maps `1-1800` /
`2-1800` / those names to the builtin GUIDs.

### Round 3 — bank BFF harvest

Authoritative categorization for the feed era:
[002-bank-transaction-harvest.md](002-bank-transaction-harvest.md) →
`build_bank_from_harvest.py`.

### Director-advance pattern

```
Bill → PI (open)
MYOB pays via Advances from Company Director → clearing journal (closes PI)
Later bank transfer to director → categorize to director liability (not to the PI)
```

Matching the reimbursement bank line to the PI double-counts.

---

## 7. Journals as dictionary — not wholesale paste

Importing `journal_dictionary.tsv` wholesale duplicates purchases once PIs exist, skips
bank reconciliation, and posts bill payments to chart AP instead of documents.
Keep journals as:

1. Pre-load proof — diff `opening_balance_journal.tsv` against the MYOB TB by hand
   (the standalone `reconcile_trial_balance.py`/`reconcile_pl.py` this doc used to
   reference no longer exist, abandoned with the first attempt; not rebuilt).
2. Round-2 bank categorization dictionary.
3. Selective non-bank import — see
   [runbook.md § Recovering deferred non-bank journals](../reference/runbook.md#recovering-deferred-non-bank-journals)
   for the technique: filter `journal_dictionary.tsv`
   directly by txn_type + code (not a `build_nonbank_journals.py` script,
   which was never built) and apply via `apply_deferred_yearend_journals.py`'s
   pattern.
4. **Post-load gap dictionary** — when reconcile shows a BS variance, filter the
   journal extract for that account, classify the missing Manager documents, and fix
   with a targeted Batch Create/Update. Method:
   [manager-import.md §6a](../reference/manager-import.md#6a-resolving-balance-sheet-gaps-with-the-full-journal-extract).

### Reconcile gate = Manager **report export**, not SQLite or live-API reconstruction

`reconcile_manager_to_myob.py` must compare **Manager’s own Trial Balance**
(PDF export) to the MYOB TB for the real signoff:

```bash
python3 scripts/reconcile_manager_to_myob.py
```

For fast iteration between exports, `--live-api` computes Manager's side from
live Journal/Payment/Receipt/Invoice data instead — see
[live-trial-balance.md](../reference/live-trial-balance.md). It independently
confirms this section's original warning: an account touched by a
tax-inclusive invoice line (GST payable/paid/collected, most expense
accounts) can't be safely reconstructed outside Manager's own report, because
the net/GST split it computes internally is never echoed back anywhere in
the API — the live-API mode excludes those accounts explicitly rather than
guess at them, whereas an earlier from-DB (SQLite/protobuf) approach
diverged silently on AR, AP, GST, Retained earnings, and Suspense even when
bank and fixed assets matched. Neither is a substitute for the PDF export.

### Reconstructing document-less MYOB rows

| MYOB pattern | Builder |
|---|---|
| `General journal; Sale` | `build_early_sales_from_journals.py` |
| `General journal; Purchase` | `build_early_purchases_from_journals.py` |

Also:

- Reallocate bank lines parked on the wrong account
  (`build_bank_reallocation_from_journals.py`).
- Copy missing non-bank journals by Reference (`extract_journals_by_ref.py`) —
  **and** date/narration when refs recycle.

### Format pitfalls when matching journals to documents

- MYOB Bills/GJ often post **two AP credits** (ex-GST + GST); Manager PIs post
  **one** total — aggregate MYOB AP by journal before comparing.
- Director-funded bill payments: one MYOB SP ref may be **several** Manager journals
  (one PI each) — sum journals sharing the SP ref.
- Some bank payments carry **offsetting GST pairs** that net to zero on the payment
  total (e.g. hire-purchase GST deferred). Dropping the pair drifts GST accounts
  while bank Actual stays correct — fix with Payment Batch Update, not a second bank line.
- MYOB may park AP on an electronic-clearing asset and clear it with a bank receipt
  (or the reverse). The bank feed only carries one leg → Batch Create the non-bank
  clearing journal (map chart AP → builtin AP).
- Deleted bank account GUIDs may still appear in `journal_dictionary.tsv`. Map to the
  live bank or director liability when reconstructing — never leave them postable.
- Pay-run accruals vs bank clear timing can differ by a day at FYE; statement/bank
  Actual wins — do not re-date Manager to chase MYOB TB.
