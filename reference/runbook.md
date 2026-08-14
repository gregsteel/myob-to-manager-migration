# MYOB Business Lite → Manager.io Migration Runbook

Generic runbook for a MYOB Business Lite → Manager migration. Full history
preserved, side-by-side operation through a complete tax cycle before MYOB
is retired. Country-specific compliance detail (GST/ABN/BAS/PAYGW/FBT/
Super/STP for Australia) lives in [tax-au.md](../../manager-automation/reference/tax-au.md) rather than here —
add your own `tax-<country>.md` if migrating elsewhere.

Example timeline below uses opening **30 June 2015**, Manager Start Date
**1 July 2015**, go-live **1 July 2026** — replace with the company's dates.

> **Golden rule:** every phase ends with a *reconciliation* against a MYOB report.
> If the Trial Balance doesn't match to the cent, you do not proceed.

---

## 0. The two dates (do not confuse these)

| Date | Meaning |
|---|---|
| **30 June 2015** | Opening balance date. Books were migrated from MYOB Essentials here, so no transaction detail exists before it — only balances. |
| **1 July 2015** | **Manager's Start Date.** The beginning of retained history. |
| **1 July 2026** | **Go-live / cutover.** Side-by-side operation begins. FY2027 is the first year run in both systems. |

**Manager's Start Date is 1 July 2015, *not* 1 July 2026.** Manager's start date defines
where its books begin; setting it to 2026 would make it impossible to hold the eleven
years of history you want. Go-live is an operational milestone, not a Manager setting.

**Scope:** opening balances at 30/06/2015, then full journals **FY2016 → FY2026**
(eleven financial years), then live dual entry from FY2027.

---

## 1. Terminology: MYOB "Categories" = Manager "Accounts"

MYOB Business (browser edition) renamed accounts to **categories**. Your chart of
accounts comes from the **Categories list** report. The scripts already expect this;
just don't go hunting for an "Accounts List" that doesn't exist in your tier.

---

## 2. Export from MYOB → `exports/myob/`

All from **Reports**. **Export as Excel (.xlsx)** — the toolkit reads MYOB's `.xlsx`
directly (including the six preamble rows and the blank row under the header), so
there's no need to convert to CSV. `.csv` also works if you prefer.

### ⚠ Report filters are per-report and they persist

**This bit us on the first export.** The Categories list had a saved
**`Categories — 28 selected`** filter, so it emitted 28 of the chart's accounts and
silently dropped Equity, Income and Cost of Sales entirely. The file looked perfectly
well-formed; only the class-total arithmetic exposed it.

**Before exporting each report,** open the right-hand options panel and confirm every
selector reads **All** — Categories, Tax codes, Category levels, Contacts, Accounts.
Click **Reset** if unsure. Then re-run §3a. Each report remembers its own filters, so
checking it once does not protect the next one.

Report options to use (Categories list; the same principles apply elsewhere):

| Option | Set to | Why |
|---|---|---|
| **Categories** | **All** | anything less silently truncates the chart |
| Categories with zero balances | ✓ on | history posts to dormant accounts |
| Header categories | ✓ on | their subtotals drive the completeness check |
| Cents | ✓ on | whole dollars destroy cent-level reconciliation |
| Category levels | All | |
| Tax codes | All | |
| Year-end adjustments | Include | you want final post-adjustment balances |
| Subtotals | off | avoids extra rows the parser must discard |
| Currency symbols | off | simpler parsing (handled either way) |
| Negative amounts | either | brackets and minus signs both parse |

Names below match your MYOB exactly.

### Essential (the migration cannot proceed without these)

| MYOB report | Section | Settings | Save as |
|---|---|---|---|
| **Categories list** | Business | **as at 30/06/2015** | `categories_list.xlsx` |
| **Journal entries** | Business | 01/07/2015 → 30/06/2026, **one file per FY**, **expanded** | `journal_entries_FY2016.xlsx` … `journal_entries_FY2026.xlsx` |
| **Trial balance** | Business | month **Jun 2026** (month-end; no Cash/Accrual toggle in Business Lite) | `trial_balance_current.xlsx` |
| **Trial balance** | Business | month **Jun 2015** | `trial_balance_opening.xlsx` |

**Categories list is exported "as at 30/06/2015"** and does double duty: it is both the
chart of accounts and the source of the opening balances, since it carries a balance
per category. No separate Balance sheet export is required.

### ⚠ Journal entries MUST be exported in EXPANDED layout

By default this report is **collapsed**: one row per transaction, Debit and Credit both
showing the transaction total, and **no accounts at all**. It looks like a valid export
and is completely unusable.

In the report, click **Customise → Select and reorder columns** and include:

| Column | Why |
|---|---|
| **Ref no** | becomes the journal Reference |
| **Code** | the account code — without it nothing can post |
| **Category name** | the account name |
| **Debit ($)** / **Credit ($)** | the amounts |

Each transaction then expands into its posting lines under a repeated
`Ref no | Code | Category name` header, followed by a `Total amount` row. The toolkit
uses that total row as a per-transaction integrity check, and refuses collapsed exports
with instructions.

Export **one financial year per file** — eleven years in one go is slow, more likely to
truncate, and much harder to diagnose. The scripts pick up all
`journal_entries_*.xlsx` automatically.

#### ⚠ Year-boundary overlap — normalise the files first

MYOB's date picker brackets each export **`30 Jun YYYY - 30 Jun YYYY+1`**, so the
leading **30 June** (the prior year's last day) appears in *two* adjacent files. Left
in, those 77 boundary transactions — including material year-end adjustments like
`CIB Adj $22,023.66` — would post **twice**.

Two defences, both active:

```bash
python3 scripts/normalize_journal_files.py   # trims leading 30 June from each file
```

This backs up your raw exports to `exports/myob_raw/`, then rewrites each file to a
clean 1 Jul – 30 Jun. It only drops a leading-30-June transaction that is verified
present in the previous file's tail; the earliest file's leading day (30 Jun 2015,
pre-migration) has none. As a belt-and-braces measure `build_journals.py` **also**
dedupes identical cross-file transactions at runtime, so a re-export that reintroduces
the overlap can't silently double-count.

**No tax codes are available on this report.** That is fine for history: GST is posted
as explicit lines to the GST control accounts, so balances are exact. Manager's tax
reports won't retrospectively analyse imported journals — keep the archived MYOB GST
reports for past periods. Tax codes matter from FY2027, entered natively in Manager.

### Verification (used to prove the migration, not to build it)

The specific reports below (and their "BAS quarter"/"TPAR" cadence) are
Australian MYOB editions' compliance-reporting names — see
[tax-au.md](../../manager-automation/reference/tax-au.md) for what they're checked against and why. A
different country's MYOB/source-system edition will have its own
equivalent compliance reports; substitute those instead.

| MYOB report | Section | Settings | Save as |
|---|---|---|---|
| **GST report** | Business | each BAS quarter, last 2 yrs | `gst_report_<period>.xlsx` |
| **GST return** | Business | each BAS quarter, last 2 yrs | `gst_return_<period>.xlsx` |
| **Profit and loss** | Business | each FY | `profit_and_loss_<FY>.xlsx` |
| **Balance sheet** | Business | as at 30/06/2026 | `balance_sheet_current.xlsx` |
| **General ledger** | Business | any FY | `general_ledger_<FY>.xlsx` — fallback if Journal entries cannot be expanded |

### Subledgers and contacts

| MYOB report | Section | Settings | Save as |
|---|---|---|---|
| **Contacts** | Business | all | `contacts.xlsx` |
| **Unpaid invoices** | Sales | as at 30/06/2026 | `unpaid_invoices.xlsx` |
| **Unpaid bills** | Purchases | as at 30/06/2026 | `unpaid_bills.xlsx` |
| **Receivables reconciliation with tax** | Sales | as at 30/06/2026 | `receivables_recon.xlsx` |
| **Payables reconciliation with tax** | Purchases | as at 30/06/2026 | `payables_recon.xlsx` |
| **Banking reconciliation** | Banking | latest per account | `banking_reconciliation.xlsx` |

### Only if applicable

- **Inventory** (if you hold stock): *Stock on hand*, *Inventory value reconciliation*.
- **Jobs** (if you use job tracking): *Job transactions (accrual)*. Manager's equivalent
  is **Tracking Codes** — a separate mapping exercise, tell me if you use these.
- **Payroll** (see §8): *Payroll register*, *Pay run history*, *Payroll activity*,
  *Accrual by fund (detail)*, *Superannuation payments*, *Leave balance (detail)*.
- **Taxable payments annual report** (if you pay contractors and lodge TPAR).
- **Journal security audit** — export once and archive. It's your change-history
  record and cannot be reconstructed after MYOB is cancelled.

Keep PDFs of every lodged **BAS**, **tax return**, and **financial statement**
outside this toolkit. Manager holds the data; those PDFs prove what you lodged.

### Bank transactions (Playwright harvest)

Do **not** use a manual bank-register xlsx. Harvest categorized bank lines from MYOB
(same BFF session as bills):

```bash
python3 scripts/myob_playwright/download_bank.py
# → exports/myob/bank/transactions.jsonl (+ .tsv, accounts.json)
```

Pre-feed months (before the bank feed starts) and gap fixes come from the MYOB
**Journal entries** export → `journal_dictionary.tsv`. See [specs/002](../specs/002-bank-transaction-harvest.md)
and [manager-import.md](manager-import.md) §3.

### Purchase bills + receipts (Playwright archive)

Historical bills are harvested into `exports/myob/bills/by_bill/` via Playwright.
See [receipts.md](receipts.md) and `scripts/myob_playwright/download_bills.py`.

### Journal entries (Playwright, expanded layout — automatable)

The manual process above (§2, "Journal entries MUST be exported in
EXPANDED layout") can be automated instead of clicked through by hand:

```bash
python3 scripts/myob_playwright/download_journals.py --fy 2027
# → exports/myob/journal_entries_FY2027.xlsx, same format build_journals.py
#   already expects -- no changes needed there
```

Reuses the bills/invoices Playwright session. The click sequence this
automates — and why each step matters (Customise's Apply only configures
columns, a separate "Expand all" is the real collapsed/expanded toggle,
Export reveals an Excel/PDF choice) — is documented in
[delta-migration.md](delta-migration.md), which also covers the wider
on-demand delta-migration pipeline (filter → apply Bills/Invoices → link
Payments → apply standalone Journals) this harvester feeds into once
MYOB is no longer the sole system of record (side-by-side operation, §7).

---

## 3a. Validate the export BEFORE building anything

```bash
python3 scripts/validate_categories.py
```

MYOB's Level-1 rows (`1-0000 Asset`, `2-0000 Liability`, …) hold the total for each
account class, so the sum of postable accounts in a class must equal it. This catches
a filtered, truncated or paginated export before it becomes a hole in your chart of
accounts. **Must pass before proceeding.**

> **Groups are subtotals.** `1-0001 Banking 57,965.92` is exactly the sum of the bank
> accounts beneath it. Group rows must never be imported as accounts or the entire
> balance sheet double-counts. The toolkit excludes them and writes
> `out/manager/review_groups_excluded.tsv` for you to eyeball.
>
> The discriminator is the **Tax code**, not `Level`: MYOB gives every postable
> category a tax code (`N-T`, `CAP`, …) and leaves it blank on headings. `Level` alone
> is wrong — `1-4000 Payroll Clearing Account` and `6-5110 FBT Expense` are Level 2 yet
> fully postable.
>
> Sub-group membership (which accounts sit under "Banking") is **not** in the export;
> MYOB keeps those ranges internally. You'll re-create the grouping in Manager by hand
> — a one-off tidy-up, not a data problem.

## 3. Set up Manager

1. Create the business. Set **Start Date = 01/07/2015** (this is **not** Lock Date —
   Starting balance on bank accounts only appears after Start Date is set).
2. **Settings → Tax Codes**: create every code in `tax_code_map`
   (`GST 10%`, `GST Free`, …). Names must match the config exactly.
3. Build and seed the chart of accounts. **There is no Batch Create for COA:**
   ```bash
   python3 scripts/build_chart_of_accounts.py
   # Quit Manager first
   python3 scripts/seed_manager_coa.py
   ```
4. Create **Bank and Cash Accounts → New** for `1-1110 primary bank account`. A COA
   row alone does **not** enable statement import or Actual balance.

> **Live headers:** for any document type that *does* support Batch Create, create one
> row manually, open **Batch Update**, and copy the header row if our TSV differs.

**Loading documents, bank, and categorization into Manager is a separate procedure** —
do not paste all of `journal_dictionary.tsv` as the live books. Follow
**[manager-import.md](manager-import.md)** (purchase invoices + bank statement + two
categorization rounds + director-advance clearing). Hard-won facts:
[specs/001-manager-import.md](../specs/001-manager-import.md).

---

## 4. Opening balances (30 June 2015)

```bash
python3 scripts/build_opening_balances.py
```

Produces one balanced journal dated 30/06/2015. **Bank lines are omitted** (set the
bank Starting balance / opening Receipt separately); retained earnings is adjusted so
the journal still balances. **Import this before purchase invoices / bank history.**

Data-level check, before touching Manager at all: diff `opening_balance_journal.tsv`
against `trial_balance_opening.xlsx` by hand/spreadsheet (the standalone
pre-load TSV-vs-XLSX reconciler this doc used to reference,
`reconcile_trial_balance.py`, no longer exists — it was part of the
abandoned first attempt and never rebuilt). After Manager paste, use
`compute_live_trial_balance.py --calibrate`
([live-trial-balance.md](live-trial-balance.md)) instead — the Opening TB
predates every invoice, so it's the one point in the whole migration where a
live-API balance should match the official PDF **exactly**, and it's a good
sanity check on the opening load specifically before moving on to full
history.

Note: after bank exclusion the TSV no longer includes the bank account line — compare
Manager’s balance sheet (bank Starting balance + opening journal) to MYOB, not the TSV
alone for class-1 totals.

Must pass before continuing.

---

## 5. Full history — FY2016 to FY2026 (data build)

```bash
python3 scripts/build_journals.py
```

Writes `out/manager/journal_dictionary.tsv` (one row per transaction line,
grouped by `txn_id`). This file is the **validated MYOB history** used for
reconciliation, bank categorization lookups, and recovering deferred non-bank
journals (below). It is **not** the primary Manager import once you follow
[manager-import.md](manager-import.md) (Purchase Invoices + bank
payments/receipts replace most journal rows).

The script fails loudly on unbalanced entries and reports unmapped tax codes. Blank is
correct for `N-T`; anything else means `tax_code_map` needs fixing.

### ⚠ `txn_id` is a sequential counter assigned during parsing, not a stable identifier

Every row's `txn_id` is just an incrementing counter over the order
transactions are encountered while parsing the export files — it has no
relationship to anything in MYOB itself (not a MYOB transaction number, not
a hash of the content). **Re-running this script against updated exports
(expected repeatedly during any side-by-side/parallel-run period, as the
source system accumulates more transactions) reassigns every id from the
point of any new/removed row onward** — inserting one new transaction
anywhere in an earlier fiscal year shifts every later id, even ones dated
long after the insertion point.

This is invisible until something *persists* a reference to a specific
`txn_id` across regenerations — e.g. a manual-override config file that
hardcodes ids for human-confirmed matches (see a host project's own
director-advance-reconciliation-style dashboard, if it has one). A drifted
id can fail loudly (it no longer resolves to anything sensible) or, worse,
**silently resolve to a different, unrelated real transaction** — producing
a wrong match with no error at all. Any downstream tool that persists a
`txn_id` reference should log what it actually resolved to (description,
amount) on every run, not just trust that the number still means the same
thing, and should be re-verified by date/amount/description (never by
assuming the old number is still correct) after every `journal_dictionary.tsv`
regeneration.

### ⚠ Don't assume Manager auto-derives retained earnings — verify first

Do not assume year-end "End of Year Adjustment" entries (01/07/YYYY,
rolling `Current year earnings` into `Retained earnings`) can be dropped on
the theory that "Manager auto-derives retained earnings." **Verify this
independently before relying on it — assuming it without checking can cost
a full reconciliation-gate cycle to discover the mistake.** Two checks
confirm whether a year-end close ever runs automatically: (1) compare
Manager's own Retained Earnings figure between the opening TB and a
current-date TB export — if they're identical despite years of intervening
P&L activity, nothing has ever rolled forward automatically; (2) check
whether the source system's own journal history has an explicit "End of
Year Adjustment"-style entry for every fiscal year (`Dr Current year
earnings / Cr Retained earnings`) — if so, each one must be migrated like
any other journal, or the whole equity section stays permanently wrong. See
[Recovering deferred non-bank journals](#recovering-deferred-non-bank-journals)
below for the technique to find and apply these.

Then reconcile the whole period **against the journal TSVs** (before Manager load):

```bash
python3 scripts/build_journals.py   # -> out/manager/journal_dictionary.tsv
```

That proves the **extract** is complete. After the document-based Manager
load, prove the **live books** the same way, using the reconcile gate
(fixed-path convention, reads
`exports/manager/trial_balance_{opening,current}.pdf` and
`exports/myob/trial_balance_{opening,current}.xlsx` — no flags for the
official signoff run):

```bash
python3 scripts/reconcile_manager_to_myob.py
# Fast iteration without a manual export each time (excludes invoice-tax-line
# accounts, NOT a substitute for the real signoff -- see live-trial-balance.md):
python3 scripts/reconcile_manager_to_myob.py --live-api
```

But always keep `journal_dictionary.tsv` as the dictionary for closing
remaining gaps (see
[manager-import.md §6a](manager-import.md#6a-resolving-balance-sheet-gaps-with-the-full-journal-extract)
and the recovery technique just below).

Documented intentional differences (BAS Clearing, RE, bank statement truth, etc.):
[MIGRATION_DIFFS.md](../../../../docs/MIGRATION_DIFFS.md).

### Recovering deferred non-bank journals

`journal_dictionary.tsv` contains MYOB's **entire** journal history — most of
it is already captured some other way (every "Purchase;"/"Sale;" description
is MYOB's own internal echo of a Bill/Invoice already migrated via the PI/SI
pipeline; every "Bill payment" is a director-advance-funded bill already
handled by the AP-linking work). **"Pay run" entries are the one exception in
this list — they are payroll, but nothing in this project auto-captures
them; each migrates as a hand-built journal** (`manager-automation`'s
[reference/payroll.md](../../manager-automation/reference/payroll.md): "past
pay runs migrate as ordinary journals"). Excluding `txn_type == "Pay run"`
below only means "don't re-surface it as a generic BAS/FBT/depreciation-style
adjusting journal" — it does **not** mean it's already in Manager or that it's
safe to skip. **Never copy a `Pay run` row's `description` column into the
Manager Narration verbatim** — MYOB's Journal entries export gives every
`Pay run` transaction the business's own postal address as its `description`,
not a real memo (confirmed 2026-08-14, three pay runs, `journal_entries_FY2026.xlsx`).
Build a real narration from the pay date and the journal's own Wages/PAYGW/
Super lines instead. The **blind
spot**: filtering only `txn_type == "General journal"` misses genuine
deferred entries MYOB recorded as `Spend money`/`Receive money`/`Bill
payment` with **no real bank-account line** (funded via Director Advances or
a loan account instead) — the same blind spot as scanning only one
transaction type when looking for transfers between two accounts (a
project's own director-advance reconciliation dashboard, if it has one,
is a real-world example — filtering by type instead of by which accounts
are actually touched misses real entries).

Working filter, in order:

1. Group all rows by `txn_id`.
2. Drop any group whose codes include a real bank account (typically the
   operating account and any secondary savings/sweep account) — already
   covered by `build_bank_from_journals.py`.
3. Drop `txn_type` of `Bill`, `Sale`, `Bill payment`, `Supplier
   return applied`, `Invoice payment`, `Receive refund` — all already
   captured via the PI/SI/AP-linking pipelines (spot-check a sample against
   the live PI/SI before trusting this for a new business). Also drop
   `Pay run` here — but track it separately as a manual to-do, **not** as
   "already handled": see the narration warning above before creating any
   of these in Manager.
4. What's left is real: typically a cluster of BAS/FBT/depreciation/
   income-tax year-end adjustments per fiscal year, the "End of Year
   Adjustment" closing entries above, dividend reallocations, and a handful
   of director-funded reimbursements (`Spend money` with `Cr Advances from
   Company Director`, no bank line).

Apply via a script that reads `journal_dictionary.tsv` **directly** rather
than hand-transcribing amounts/codes — hand-transcribing a batch of
multi-line journals is exactly where a stray digit goes unnoticed
(`scripts/apply_deferred_yearend_journals.py` is the reusable pattern: a
list of transaction IDs to recover + a generic builder + an idempotency
marker embedded in the Narration, since a source reference like "Yr End
2023" can repeat across different fiscal years and can't be used alone as
the dedup key). Verify each txn balances (`sum(debit) == sum(credit)`)
before applying, and after applying, verify the delta on each touched
account matches the journal's own line amounts exactly (not just "the
mismatch got smaller") — this catches an account being touched by *two*
things at once and looking coincidentally right.

#### ⚠ The trial balance is YTD — it only proves the balance sheet

MYOB's Trial balance columns are **`YTD Debit/Credit`** — year-to-date for the current
financial year. Balance-sheet accounts (1-, 2-) accumulate, so they reconcile against
the full-history journals exactly. But **P&L accounts (4,5,6,8,9) reset each year**, so
a single as-at-30/06/2026 trial balance shows only FY2026's movement and will *not*
match 11 years of cumulative journals.

The reconciler recognises this: if every asset/liability account reconciles and the
remaining P&L + equity differences **net to exactly 0.00**, the **balance sheet is
proven correct** — which is the single most important checkpoint. That is a PASS for
the balance sheet, reported as `[likely OK]`.

The **Trial balance has no date range** — it is "As at <month> <year>" only, so it can
never give cumulative P&L. Prove the P&L a different way:

1. **Profit and loss report, full range** ✅ preferred — the P&L report *does* take a
   date range. Export it for **01/07/2015 → 30/06/2026** and save as
   `profit_and_loss_cumulative.xlsx`. **Set it to Accrual mode** (Report options) — the
   journals are accrual, so a Cash-mode P&L differs on any account with unpaid
   invoices/bills at year end. The reconciler reads the report's mode banner and warns
   if it is Cash. That gives cumulative income/expense per account across all 11 years,
   which should reconcile against `journal_dictionary.tsv`'s P&L movement — do this
   comparison by hand/spreadsheet (`reconcile_pl.py`, the standalone tool this doc used
   to reference, was part of the abandoned first attempt and never rebuilt).
2. **Per-year trial balances** — fallback: export a Trial balance "as at June" for each
   year (YTD then equals that FY) and reconcile each `out/manager/by_year/` file against
   its year. Eleven exports; proves each year independently but is more work.

Between the two reports — Balance sheet (from the as-at TB) and cumulative P&L — every
account is covered. The goal is **every account 0.00**.

---

## 6. Open items at cutover

Contacts:
```bash
python3 scripts/build_contacts.py
```

For invoices/bills **still unpaid** at 30/06/2026 (`unpaid_invoices.xlsx`,
`unpaid_bills.xlsx`), enter them as real **Sales Invoices / Purchase Invoices** so
aging works and payments can be matched going forward.

> **Do not double-count.** Those balances are already in the journals as
> Trade Debtors/Creditors movements. Either post the journals *or* the documents for
> the open items — not both. Simplest: import journals for everything, then for open
> items only, reverse the AR/AP journal lines and re-enter as documents. Re-run the
> reconciliation afterwards; it will catch it if you get this wrong.

---

## 7. Side-by-side (first full cycle)

Enter **every** transaction in both systems.

- **Weekly:** compare Manager's Trial Balance to the old system's. Investigate
  drift immediately.
- **Each compliance-lodgement period — the real test:** produce tax figures
  in Manager and confirm they equal the old system's own return before
  lodging. Tax-code mapping is one of the highest-risk items in the whole
  migration: balances can reconcile perfectly while the lodgement is
  quietly wrong, because a mis-mapped code moves tax between fields without
  changing the ledger at all. See [tax-au.md § Cross-checking](../../manager-automation/reference/tax-au.md#cross-checking-during-side-by-side-operation)
  for the Australian BAS-quarter version of this check.
- **Year end:** prepare the annual statements and return from Manager;
  cross-check against the old system. Agreement = Manager has passed a
  full cycle.

Minimum before trusting Manager for compliance: **one full lodgement
period reconciled**. Recommended: through a full annual return.

---

## 8. Payroll — confirm before relying on Manager alone

If wages are paid via a payroll/compliance-lodgement process, check
whether **Manager can lodge that on its own** for your country — as of
writing it generally can't, and shouldn't be assumed to gain that
capability. See [payroll.md](../../manager-automation/reference/payroll.md) for the general options and
[tax-au.md § STP](../../manager-automation/reference/tax-au.md#stp-manager-has-no-lodgement)
for the Australian specifics (STP, Payday Super, low-cost lodgement
tools).

**History is unaffected** — past pay runs migrate as ordinary journals
like everything else. Only the forward process needs solving.

**Do not cancel the old system until payroll lodgement is resolved**,
however well the ledger reconciles.

---

## 9. Decommission the old system

Only after a clean full cycle **and** the payroll blocker is cleared:
1. Final full export of **every** report + PDFs → permanent archive. Do this *before*
   cancelling; access typically ends with the subscription.
2. Payroll cutover resolved — see [payroll.md](../../manager-automation/reference/payroll.md). **Hard blocker.**
3. Confirm every archive is readable and stored off-machine.
4. Cancel the old system's subscription.
5. Automate backups of the Manager `.manager` file — it is your entire book. **Do
   not wait until this stage to start** — see Golden rule 10 in `SKILL.md`:
   snapshot with `scripts/backup_manager_business.py` before any batch of API
   writes that deletes or bulk-modifies existing records, from day one of the
   build, not just at decommission.

Record-retention requirements for how long to keep the archive are
jurisdiction-specific — see [tax-au.md § Record retention](../../manager-automation/reference/tax-au.md#record-retention)
for Australia's. Keep the report archive regardless of what's in Manager —
Purchase bill PDFs/images are archived under `exports/myob/bills/` and
attached in Manager — see [receipts.md](receipts.md).

---

## File map

```
exports/myob/       ← MYOB report exports (journals, TB, categories, contacts, …)
exports/myob/bank/       ← Playwright bank transaction harvest (categorization)
exports/myob/bills/      ← Playwright bill + receipt harvest
exports/myob/invoices/   ← Playwright sales invoice harvest
exports/myob/journal_entries_FY<n>.xlsx  ← Playwright journal harvest (download_journals.py) or manual export
out/manager/        ← TSVs/CSV for Manager API apply / Batch + bank import
  by_year/          ← per-FY splits (journals, PI reopen, bank categorization)
scripts/            ← ETL + reconciliation + Manager helpers (stdlib Python only)
scripts/myob_playwright/ ← MYOB harvest (bills/invoices/journals), symlinked from the skill
scripts/myob_delta/      ← on-demand delta migration pipeline, symlinked from the skill (see delta-migration.md)
config/myob_business_id.txt, manager_business_name.txt, myob_tax_code_map.tsv, real_bank_accounts.tsv, last_migration_date.txt
                    ← per-project config the delta-migration scripts read
samples/            ← fake MYOB-shaped data for dry runs
reconcile/          ← generated variance reports
docs/               ← instance-only notes (this migration's diffs, payroll status)
.claude/skills/myob-to-manager-migration/  ← generic skill (reference/ + specs/ + scripts/)
```
