# Manager.io import — operational how-to

This is the **repeatable** path for loading history into Manager after the MYOB
export/validate/build steps in [runbook.md](runbook.md). It incorporates everything
learned on a real company migration so the next run does not rediscover the same traps.
Country-specific compliance detail (GST/ABN/BAS/PAYGW/FBT/Super/STP for
Australia) lives in [tax-au.md](../../manager-automation/reference/tax-au.md) rather than here.

Companion design notes: [specs/001-manager-import.md](../specs/001-manager-import.md).

Dates, account codes, and ballpark counts below are **illustrative** (from one
completed migration). Adjust to the company's opening date, Start Date, and chart.

> **Golden rule still applies:** prove each phase before the next. Quit Manager before
> any script that writes the `.manager` SQLite file.

### Default write path: API apply (not clipboard paste)

Builders still emit Batch-shaped TSVs under `out/manager/`, but **apply them with
the API** — do not open Batch Create/Update in the UI unless the API path fails:

```bash
# credentials: .env (MANAGER_API_URL, MANAGER_API_KEY)
python3 scripts/apply_manager_api.py --dry-run out/manager/some_batch_update.tsv
python3 scripts/apply_manager_api.py out/manager/some_batch_update.tsv
```

- **Updates:** GET form → merge patch → PUT full body (partial Batch rows wipe fields).
- **Creates:** POST form; skip if `Reference` already exists (`--force` to override).
- **Still SQLite:** COA seed (`seed_manager_coa.py`) and receipt image attach — Quit Manager first.
- **Still UI/CSV:** initial bank statement import. Categorization / §6a fixes → API apply.
- Spot-check with `../manager-ai-skills/manager` (see `.env`). Details: [specs/001](../specs/001-manager-import.md) §1.1.

**Business file:** set via `MANAGER_DB` or `resolve_manager_db()` in `manager_journal_format.py`
(default looks under `~/Documents/Manager.io/Businesses/`).

---

## Decision: what gets imported as what

Do **not** paste the full `journal_dictionary.tsv` as the live books. MYOB journals mix
bill purchases, bill payments, bank fees, payroll, sales, and director advances into
one ledger feed. In Manager those must become documents + bank lines:

| MYOB source | Manager destination | Why |
|---|---|---|
| Bills + receipt images | **Purchase Invoices** (+ Images) | Aging, supplier, attach PDFs |
| Bank register | **Bank statement import** → Payments/Receipts | Reconciles Actual balance |
| Bill payments funded by bank | Payment → **builtin Accounts payable** + supplier + invoice | Clears the PI |
| Bill payments funded by director | **Journal** clearing AP ↔ Advances from Company Director | No bank line |
| Bank reimbursements to director | Receipt/Payment → **2-1510 Advances…** | Not an invoice |
| Payroll / HP / ATO / interest / fees | Payment/Receipt lines to the same accounts MYOB used | From journal lookup |
| Customer receipts | Receipt → **builtin AR** + customer + Sales Invoice | Chart 1-1800 does not clear SIs |
| Opening equity/assets (ex bank) | One **OPENING** journal (builtin AR for opening AR) | Bank opening is separate |

`journal_dictionary.tsv` remains the **lookup source of truth** for round-2 bank
categorization and for reconciliation proofs. It is not the primary import vehicle
once Purchase Invoices + bank are in. It is also the **gap-resolution dictionary**
after the live load — see §6a.

**Why Manager still differs from MYOB on some lines after a successful load:** see
[MIGRATION_DIFFS.md](../../../../docs/MIGRATION_DIFFS.md) (BAS Clearing, RE, bank statement truth,
reconstructed early sales, optional MYOB reclass side-effects).

---

## 0. Prerequisites (once)

1. Business created with **Start Date = 01/07/2015** (not Lock Date — see §1).
2. Tax codes from `config.json` `tax_code_map`. **Verify each tax code's
   actual configured rate, don't trust its name** — see below.
3. Chart of accounts seeded (no Batch Create exists — see §1).
4. **Bank and Cash Account** created for `1-1110 primary bank account` (separate from COA).
5. Suppliers + customers Batch Created from `build_contacts.py`.
6. Bill archive present under `exports/myob/bills/by_bill/*/bill.json` (+ receipt files).
7. Bank harvest present under `exports/myob/bank/` (`download_bank.py`); journals built
   (`journal_dictionary.tsv`) for pre-feed months and categorization dictionary.

### Tax code rate/design pitfalls -- see manager-automation

Two important, purely-Manager pitfalls to check before seeding tax codes
from `tax_code_map`: a tax code's name is not proof of its configured
rate (a "GST 10%" code can silently be 0%), and Manager only supports one
GL account per tax code (consolidate a two-account source split onto one,
don't replicate it). Full detail, the fix procedure, and the consolidation
pattern: [manager-automation reference/tax-codes.md](../../manager-automation/reference/tax-codes.md).
---

## 1. Chart of accounts + bank account

General Manager COA/builtin-control/bank-account mechanics (protobuf vs
REST API seeding, builtin AR/AP/RE requirements, Bank-and-Cash vs chart
rows, Start Date/Lock Date, reclassifying a wrongly-typed bank account):
[chart-of-accounts-and-banking.md](../../manager-automation/reference/chart-of-accounts-and-banking.md)
in the `manager-automation` skill. The MYOB-specific parts of seeding from
an export:

```bash
# Quit Manager first (SQLite path) -- or use the REST API path, no quit needed
python3 scripts/build_chart_of_accounts.py
python3 scripts/seed_manager_coa.py     # or: scripts/seed_chart_of_accounts.py (REST API)
```

`seed_chart_of_accounts.py` sets **Name** = bare title and **Code** = MYOB
number, and recognizes MYOB accounts named "Accounts receivable",
"Accounts payable", or "Retained earnings" by name
(`BUILTIN_CONTROL_ACCOUNTS`) to skip creating a duplicate chart row for
them automatically — it prints a `[MANUAL STEP]` line telling you to edit
the matching Manager builtin control's Code instead (see the
`manager-automation` reference above for why this has to be UI-only).
GST payable / Payroll liabilities stay **unmapped** (keep MYOB detail);
Foreign exchange is typically **absent**.

### Not every MYOB `Bank`-typed account is a real bank account — confirm with the user first

MYOB Business Lite has no "Other Current Asset"/clearing type, so it lumps
genuinely non-transactional accounts (a director loan, an ABN-withholding
tracker, electronic-clearing/undeposited-funds suspense accounts) under
`Bank` alongside the real operating accounts — don't assume every
`Bank`-typed row is a real bank account without checking; it's common for
only a minority of them to be.

`seed_chart_of_accounts.py` only creates a real
Manager Bank-and-Cash-Account for a MYOB `Bank`-typed row if its code is in
project config `config/real_bank_accounts.tsv` (columns: `code`, `name`) —
everything else typed `Bank` becomes an ordinary Balance Sheet asset
account. The script fails fast with an explanatory message if that file
doesn't exist, rather than guessing; create it once per business, after
confirming the real list with the user. If a wrongly-typed account already
has live history by the time this is caught, see
`manager-automation`'s reclassification guidance above (dormant vs
historied accounts need different fixes) —
`scripts/reclassify_dormant_bank_accounts.py` /
`scripts/reclassify_historied_bank_accounts.py` are the project-specific
appliers of that technique.

### Opening journal excludes the bank

```bash
python3 scripts/build_opening_balances.py
```

The script **omits** bank/cash lines (journals do not set Actual balance) and offsets
the excluded bank opening against `3-1600 Retained earnings` so the journal still
balances. Then:

1. Set bank Starting balance (or opening Receipt) to the omitted amount.
2. Delete any previous OPENING journal.
3. Journal Entries → Batch Create → `out/manager/opening_balance_journal.tsv`.

---

## 2. Purchase invoices + receipts

```bash
python3 scripts/build_purchase_invoices.py          # or --by-year / --limit N
# Purchase Invoices → Batch Create → paste out/manager/purchase_invoices.tsv
# Quit Manager
python3 scripts/attach_purchase_images.py           # JPEG into Images table
```

### Hard facts

- **Supplier / Account columns are Manager GUIDs**, not names. Rebuild after contacts
  and COA exist so GUID lookup succeeds.
- **Unit price:** if MYOB `unit_price` is missing/0, the builder derives it from
  amount ÷ qty (empty unit price breaks Manager lines).
- **Images:** Batch Create cannot embed PDFs. `CustomFields2.Images` in Batch Update is
  a useless ToString. Always use `attach_purchase_images.py` after import (Manager
  closed).
- **Closed invoices:** imported PIs often arrive with **Closed invoice** checked even
  when Balance due shows. Payments cannot match until reopened:
  `out/manager/purchase_invoices_reopen_batch_update.tsv` (and `by_year/` splits) —
  set `ClosedInvoice=FALSE` via Batch Update. Clearing `HideBalanceDue` in the DB alone
  is not enough.
- **Wrong issue dates / missed harvest:** use
  `python3 scripts/myob_playwright/refetch_by_number.py` then rebuild PIs. Do not rely
  on issue-date windows alone — MYOB sometimes stores absurd dates (e.g. 2010).
- **Recycled bill numbers:** MYOB can reuse bill numbers across years — check for
  this rather than assume bill numbers are unique across the full export (a
  meaningful fraction of bills can collide). A reliable dedup key is
  **`{bill_number}-{issue_date:%Y%m%d}`** (e.g. `00000784-20150704`) —
  verify zero collisions on that combined key across the full export before
  trusting it. Matching bank payments must use payment ref + number +
  supplier (`load_payment_invoice_refs` in `build_bank_categorization.py`),
  never Reference alone (Manager allows duplicate References; it just flags
  them with a warning icon).
- **A bill index's own `total` column can disagree with the actual bill
  detail's line sum** for the same bill. Don't use an index total as a
  dedup/existing-check key without cross-checking — a mismatch here can
  produce false "missing" positives and real duplicate PIs when compared
  against Manager's live invoice amounts. Once References follow a
  collision-free `{bill_number}-{issue_date}` convention, checking
  **Reference alone** against Manager's live PI list is the reliable
  existing-check — no amount/supplier needed.
- **Idempotent builder scripts must regenerate their `*_seeded.tsv` output from
  Manager's live list**, not just append/write what the current run created — a
  script that does `open(path, "w")` with only this run's rows silently truncates
  the file on every partial/pilot run, discarding prior rows. Always re-list from
  the API and write the full set at the end.
- **`AmountsIncludeTax:false` on `purchase-invoice-form` /
  `sales-invoice-form` can be a silent no-op via the API.** Feeding the
  tax-EXCLUSIVE unit price with a TaxCode and `AmountsIncludeTax:false` may
  not add the tax on top at all — the invoice total, Balance due, and AP/AR
  balance can all end up equal to the bare ex-tax amount, silently dropping
  the GST/VAT. Test this directly before trusting a batch: create one
  controlled test line with a known tax-exclusive amount and TaxCode, and
  confirm whether the resulting total actually includes the tax. **Safe
  pattern: always send the tax-INCLUSIVE amount as the unit price with
  `AmountsIncludeTax:true`, regardless of the source document's own
  tax-inclusive flag** — Manager then correctly back-calculates the
  net/tax split from the TaxCode's rate. For a 0%-rated line, ex-tax ==
  inc-tax so this is a no-op there too — safe to apply universally rather
  than branching on the source's own flag. `build_purchase_invoices.py` and
  `build_sales_invoices.py` both do this; verify the total invoice/AP/AR
  sum against the source's own tax-inclusive total after any batch, and
  check other document types (Receipts/Payments) for the same risk before
  assuming they're unaffected.

---

## 3. Bank statement import + harvest

**Source of truth:** MYOB bank harvest + journals — not a manual bank-register export.

```bash
python3 scripts/myob_playwright/download_bank.py
# → exports/myob/bank/transactions.jsonl (+ .tsv, accounts.json)  — see specs/002
python3 scripts/build_journals.py   # dictionary + pre-feed coverage
```

| Window | Source |
|---|---|
| Bank-feed era (from bank-feed start onward) | `exports/myob/bank/` — each line already carries MYOB categorization |
| Pre-feed / gaps | `journal_dictionary.tsv` (MYOB Journal entries export) |

Import uncategorized **Payments** and **Receipts** into Manager via the bank account’s
**Import bank statement** (or bank feed). Align opening + running balance with Manager’s
bank Actual (after Starting balance / opening Receipt) before categorizing at scale.

Do not categorize by hand — use §4–§5 (journals + patterns) then harvest round 3:

```bash
python3 scripts/build_bank_from_harvest.py
# → bank_*_harvest_round3.tsv  (clears leftover suspense from harvest metadata)
```

Once Payments/Receipts exist in Manager, further work is Batch Update — do not re-import
the statement.

### `matchedJournals` join supersedes rounds 4–5 for the bank-feed era

MYOB's bank-feed harvest (`transactions.jsonl`) is a far better categorization
source than a raw statement re-import — its `matchedJournals[].eventId` is
identical to the "Journal entries report" export's own `Ref no` column, so
the two can be joined exactly (no date/amount fuzzy-matching needed for
~95% of transactions). Each `matchedJournals[]` entry also carries
`contactName`/`contactType` (the real supplier/customer, even when the
journal export itself leaves `description` blank — true for ~61% of "Bill
payment" rows) and `paymentSource[].sourceId` (the **exact bill/invoice
number** being paid, letting a Payment/Receipt line set
`PurchaseInvoice`/`SalesInvoice` directly instead of relying on builtin-
AP/AR FIFO — see [invoice-linking.md](../../manager-automation/reference/invoice-linking.md) for why that
matters).

This eliminates the old §4–§5 harvest→suspense→round-1/round-2 heuristic
categorization pipeline below **for the bank-feed era**: `build_journals.py`
(parses the expanded per-FY journal export into a flat dictionary) +
`build_bank_from_journals.py` (joins on Ref no==eventId, replays each
transaction's non-bank lines straight onto a Payment/Receipt with signed
amounts) supersede §4–§5 for that window. Only the pre-feed window (before
the harvest starts) still needs the journal's own description text and the
round 1/2 heuristics below.

Bonus safety property: director-advance-funded bills never appear in this
path at all (they're `Dr AP/Cr Director` journals with no bank line), so it
structurally cannot trigger the Balance-due failure mode described in
[invoice-linking.md](../../manager-automation/reference/invoice-linking.md).

### Secondary bank accounts need `/inter-account-transfer-form`, not a plain line

A Payment/Receipt `Lines[].Account` pointing at another Manager
bank-and-cash-account does not move that account's balance — Manager
accepts it silently (no error, no validation), but only the
`PaidFrom`/`ReceivedIn` side of a Payment/Receipt is tracked as real bank
movement; a bank-type account referenced as a plain line item is a dead
end. This surfaces via trial-balance reconciliation: the operating bank
account can match the source system to the cent while a secondary
bank-type account (a savings sweep, for example) stays frozen at its
opening balance despite transactions that should have moved it.

**Fix: use `/inter-account-transfer-form` instead** whenever a journal
transaction's two "sides" are both Manager bank-and-cash-accounts (not just
the operating bank). Its amount fields are **`DebitAmount` and
`CreditAmount`** (both set to the same transfer amount) — `Amount`,
`Total`, `TransferAmount`, `Value`, `PaidAmount`/`ReceivedAmount`, and half
a dozen other guesses are all silently accepted and dropped (POST returns
200, the field is simply absent from the echoed form, zero balance effect)
— found only by brute-force probing since neither the API nor
`manager-ai-skills` document it.

MYOB's own COA often has several "Bank"-type accounts beyond the operating
account (a savings sweep, a loan-to-director account, an electronic
clearing/payroll-staging account, an ABN-withholding account — though see
["Not every MYOB `Bank`-typed account is a real bank account"](#not-every-myob-bank-typed-account-is-a-real-bank-account--confirm-with-the-user-first)
above, several of these usually shouldn't be Bank-and-Cash accounts at
all), so any journal-driven bank builder must check **every** bank-type
code for both legs of a transaction, not just the one operating account —
`build_bank_secondary_accounts.py` is the pattern for finding and fixing
transactions missed this way.

---

## 4. Round 1 — categorize from bill payments + known patterns

```bash
python3 scripts/build_bank_categorization.py
```

Paste (Batch **Update**, Key column required), oldest FY first:

- `by_year/bank_payments_batch_update_FY*.tsv` → Payments
- `by_year/bank_receipts_batch_update_FY*.tsv` → Receipts
- `by_year/director_advance_invoice_clearing_FY*.tsv` → Journal Entries **Batch Create**

### What round 1 assigns

| Bucket | Account / treatment |
|---|---|
| Confident single bill match | Builtin **Accounts payable** + supplier + purchase invoice |
| Bank fees | `6-1200 Bank Charges` |
| Director reimbursements | `2-1510 Advances from Company Director` |
| Super / ATO / interest / Cyber Saver | Mapped control accounts |
| Everything else | `1-1180` renamed **Bank transactions suspense** |

### Director advances (critical)

~149 MYOB bill payments were funded from **Advances from Company Director**, not the
bank. They clear AP with no bank line. Later bank “Reimburse / Transfer … Steel”
lines repay the director.

1. **Do not** match those bank lines to purchase invoices.
2. Import **director clearing journals** (AP debit / Advances credit) so PIs close.
3. **One journal per invoice** (references `SPxxxx-1`, `SPxxxx-2`, …). A single journal
   row with multiple `Lines.N.PurchaseInvoice` values for the same Reference gets
   **truncated** by Manager Batch Create (only first invoices stick). If that already
   happened, delete the bad journals and import
   `director_advance_grouped_journal_repairs.tsv`.

### Builtin AP vs chart `2-1800 Accounts payable`

Payment lines that clear purchase invoices **must** use Manager’s builtin Accounts
payable GUID (`dac7ba37-0ccd-45e5-906e-548e6c50df37`), plus
`AccountsPayableSupplier` (+ optional `PurchaseInvoice`). Posting to the plain chart
account `2-1800` leaves real invoices open and drives chart AP negative.

Same rule for AR: receipts that clear Sales Invoices must use builtin AR
(`d1489e95-bb28-4f5d-b42e-67d3291b3893`), not chart `1-1800`.

Account lookup in scripts: code, bare name, or legacy `"code name"`
(`load_account_guids`).

---

## 5. Round 2 — clear suspense from MYOB journals

After round 1, hundreds of lines remain in suspense (multi-invoice payments, payroll
splits, hire purchase, AR receipts, “General journal; Payment”, etc.).

```bash
python3 scripts/build_bank_suspense_recategorization.py
```

Paste:

- `by_year/bank_payments_suspense_round2_FY*.tsv`
- `by_year/bank_receipts_suspense_round2_FY*.tsv`

Review: `bank_suspense_round2_review.tsv`. Leftovers: `bank_suspense_remaining.tsv`.

### Matching algorithm (do not “improve” casually)

1. Index MYOB journals that move money through `1-1110` by (date, |amount|).
2. Each suspense bank line picks an unused journal same date ±1 day, same sign;
   disambiguate by narration↔description token overlap; if structures identical, take any.
3. **Invoice split:** journal Reference maps to archive bill payments that sum to the
   bank amount → multi-line AP + supplier + specific invoices.
4. **AP supplier FIFO:** journal touches plain AP but no per-bill payment archive →
   builtin AP + supplier only (Manager applies to oldest open invoices). Supplier from
   narration (`…; Payment; TPG…`) or bank description.
5. **Journal accounts:** replicate non-bank journal lines as payment/receipt lines
   (supports multi-line payroll / HP / GST splits, up to 12 lines).
6. Never post unresolved AP to plain `2-1800` — leave in remaining suspense instead.

Expected ballpark (example migration): ~830 of ~923 suspense cleared; ~90 left for manual review.

---

## 5b. Non-bank journals (year-end / CIB / pure Pay runs)

PI + bank load never posted BAS clearing, depreciation, dividends, FBT/tax
provisions, GST tidy-ups, or clearing-account Pay runs. Extract and paste:

```bash
python3 scripts/build_nonbank_journals.py
```

**Prefer the API over clipboard Batch Create.** The
recommended technique reads `journal_dictionary.tsv` directly and POSTs
each txn via `journal-entry-form` (avoids hand-transcribing amounts/codes,
idempotent via a Narration marker) — see
[runbook.md § Recovering deferred non-bank journals](runbook.md#recovering-deferred-non-bank-journals)
for the filter that finds these (year-end BAS/FBT/depreciation/tax
adjustments, the "End of Year Adjustment" closing entries Manager never runs
automatically, dividend reallocations, director-funded reimbursements with
no bank line) and `scripts/apply_deferred_yearend_journals.py` as the
reusable pattern.

Then reconcile:

```bash
python3 scripts/reconcile_manager_to_myob.py           # official signoff, needs a fresh PDF export
python3 scripts/reconcile_manager_to_myob.py --live-api # fast check, no export needed -- see live-trial-balance.md
```

Do **not** paste the full `journal_dictionary.tsv` — that re-books bank and bill
documents already in Manager.

---

## 6. Verify after each batch paste

Quit is not required for read-only checks. Example checks (already proven useful):

```bash
# Spot-check: every Key in a Batch Update TSV still has the expected line accounts
# (see verification approach used after round 2 — decode Objects protobuf field 11)
```

Minimum manual checks in Manager:

1. Bank **Actual balance** matches statement closing balance.
2. Suspense account ≈ only `bank_suspense_remaining.tsv` items.
3. Purchase invoices that should be paid show Balance due 0 (or expected residual).
4. Advances from Company Director movement matches MYOB for a sample year.

---

## 6a. Resolving balance-sheet gaps with the full journal extract

The document-based load (PIs + bank + SIs + selective journals) will **not** match
MYOB to the cent on the first reconcile. That is expected. What makes gaps
tractable is having already built the **complete** MYOB journal feed:

```bash
python3 scripts/build_journals.py
# → out/manager/journal_dictionary.tsv   (± opening_balance_journal.tsv)
```

Do **not** paste that file into Manager. Use it as a read-only ledger dictionary.

### Why this works

| Source | What it proves |
|---|---|
| `journal_dictionary.tsv` (+ opening) | Full MYOB accrual history, account-by-account, nets to the trial balance |
| Live Manager (`.manager`) | What the document path actually posted |
| `reconcile_manager_to_myob.py` | Manager **report export** − MYOB TB (BS gate; not SQLite) |

Every material BS variance is a **missing, duplicated, or mis-typed posting** relative
to a concrete set of MYOB journal lines. Filtering `journal_dictionary.tsv` for that
account (or Reference) shows the MYOB story; comparing to Manager documents shows
what the load omitted or got wrong. Fix with a **targeted** Batch Create/Update —
never by pasting the full journal file.

### Method (repeat per variance)

0. **Align (preferred)** — regenerate txn match + residual for the bound BS codes:
   ```bash
   python3 scripts/align_account_to_journals.py --account 2-1800
   # or: python3 scripts/align_account_to_journals.py --bound
   ```
   Reads `out/manager/journal_dictionary.tsv` + live `.manager`, writes
   `reconcile/{slug}_alignment.tsv`, `_bridge.tsv`, `_residual.tsv`.
   Intentional remediations live in `config/intentional_exceptions.tsv`.
   **Sign-off:** `Untagged_residual ≈ 0` and no `MISSING_*` in residual
   (only `INTENTIONAL_*` / documented `GAP_*` may remain).
   Do **not** paste the full journal dictionary.

1. **Measure**
   ```bash
   # Export Manager Reports -> Trial Balance (From-To -> Accrual; PDF "As at" = To date)
   # into exports/manager/trial_balance_{opening,current}.pdf, then:
   python3 scripts/reconcile_manager_to_myob.py
   # fast check without a fresh export (excludes invoice-tax-line accounts --
   # see live-trial-balance.md -- not a substitute for the run above):
   python3 scripts/reconcile_manager_to_myob.py --live-api
   ```
   Manager balances for the real signoff must come from a **report export**
   (PDF). Ignore large P&L deltas on a YTD MYOB TB. Rank **balance-sheet** variances.
   MYOB Business Lite TB has no Cash/Accrual toggle and only a **month** picker
   (Jun ⇒ month-end).

2. **Dictionary lookup** — in `journal_dictionary.tsv` (+ opening), collect every line
   that hits the variance account (legacy chart GUID or code). Group by Reference /
   narration pattern (`Invoice`, `Invoice payment`, `General journal; Sale`,
   `CIB Adj …`, etc.). Confirm the MYOB net for that account is what you expect
   (often 0.00 for fully cleared control accounts). Prefer the aligner residual
   file over hand-filtering when the account is in the bound set.

3. **Classify the gap** — for each MYOB cluster, ask which Manager document should
   have carried it:

   | MYOB pattern | Expected Manager form |
   |---|---|
   | `Invoice` / `Invoice; Sale; …` | Sales Invoice (+ receipt link) |
   | `General journal; Sale; …` | Sales Invoice reconstructed from journal (not harvestable) |
   | `Invoice payment` / `General journal; Payment` | Receipt → builtin AR + SI |
   | Non-bank payment (equity / clearing) | Journal Dr funding account / Cr builtin AR + SI |
   | Bill / bill payment | Purchase Invoice + Payment → builtin AP |
   | `General journal; Purchase; …` | Purchase Invoice reconstructed from journal (not harvestable) |
   | Bill payment with **no bank** (AP ↔ clearing / director) | Journal Dr clearing or director / Cr builtin AP (or reverse) |
   | Hire-purchase bank pay with GST deferred | Payment multi-line: HP + term charges **and** Dr `2-2400` / Cr `2-2201` |
   | Year-end / CIB / Pay run (no bank) | Non-bank journal (`build_nonbank_journals.py`) |

4. **Quantify exactly** — missing MYOB debits (or credits) should equal the
   Manager variance to the cent. If they do, you have the full explanation; if not,
   look for tax-inclusive flag bugs, combined bank receipts vs split MYOB CPs,
   chart-vs-builtin AR/AP GUID splits, or **MYOB AP split across ex-GST + GST lines**
   on one journal (aggregate per Reference before matching Manager’s single PI total).

5. **Fix narrowly** — emit a small Batch Create/Update for that cluster only, paste,
   re-run `reconcile_manager_to_myob.py`, confirm that account drops off the variance
   list before moving on.

### Worked example (AR −$198k)

| Step | Finding |
|---|---|
| Measure | Builtin AR Manager −198066.91; MYOB TB 0.00 |
| Dictionary | MYOB AR lines in journals net **0.00** (opening + invoices − payments) |
| Classify | Harvested SIs start at **140**. Refs **111–139** exist only as `General journal; Sale` — not as MYOB Sales Invoice documents, so Playwright harvest cannot retrieve them. Bank receipts that paid them **were** imported → naked AR credits |
| Quantify | Missing SI debits **$207,515** + opening interaction; orphan pre-SI receipts **$230,367.50**; difference **$22,852.50** = OPENING AR |
| Fix | `build_early_sales_from_journals.py` → Batch Create SIs 111–139 from journal lines, then Receipt Batch Update linking orphans; leave opening-clearance credits unlinked on purpose |
| Secondary | One invoice was settled via a non-bank equity transfer instead of a normal receipt, and its closing journal had never been linked — journal `Dr <settlement account> / Cr builtin AR + SI` fixed it |

Reports from that pass: `reconcile/ar_missing_sales_invoices.tsv`,
`reconcile/ar_discrepancy_summary.tsv`,
`out/manager/sales_invoices_early_111_139.tsv`,
`out/manager/ar_receipts_early_si_link_batch_update.tsv`.

### Worked example (BAS Clearing +$80k)

| Step | Finding |
|---|---|
| Measure | `2-1340` Manager +75263; MYOB TB −5300 (journals also −5300) |
| Dictionary | Manager **journals** already match MYOB (−5300). The gap is entirely **bank** lines parked on BAS |
| Classify | Round-1 bank rules coded ATO BPAY / IAS / BAS refunds to `2-1340`. MYOB booked the same bank movements as multi-line Spend money / GJs across GST, PAYG, FBT, instalments |
| Quantify | 14 payments + 19 receipts on BAS; non-journal net **+80563.25** = exact Manager−journals gap |
| Fix | `build_bank_reallocation_from_journals.py bas` → Payment/Receipt Batch Update with MYOB line splits. Leave `16/07/2024` $5300 BPAY on BAS (Manager bank feed; MYOB booked the same cash as ATO bill+ABN withholding — optional MYOB reclass only, no second bank credit) |
| Side-effect | Also clears `2-1410` old PAYG, `2-2600` PAYG, `1-1530`/`1-1550` instalments, and most of `2-2200` GST collected |

Outputs: `bank_bas_clearing_payments_batch_update.tsv`,
`bank_bas_clearing_receipts_batch_update.tsv`,
`bank_bas_clearing_reallocation_review.tsv`.

Cyber Saver `1-1120` (−199.51): missing mecu interest on the non-imported saver account —

```bash
python3 scripts/extract_journals_by_ref.py \
  --refs CR000180,CR000183,CR000190 \
  --out journal_cyber_saver_interest.tsv
```

### Worked example (tax provision −$14.8k)

| Step | Finding |
|---|---|
| Measure | `2-1700` Manager −14800.56; MYOB journals/TB 0.00 |
| Dictionary | Two non-bank Spend money rows: SM000032 ($2276.90) + SM000068 ($12523.66) Dr provision / Cr director |
| Classify | Director-funded tax returns — no bank line, so bank import never carried them. Bank tax returns (SM000087/116) already on `2-1700` |
| Fix | `extract_journals_by_ref.py --refs SM000032,SM000068 --out journal_tax_provision_director_clears.tsv` → Journal Batch Create |

Also: bank payments coded to builtin AP whose MYOB twins are Transfer money → director or Spend money → expense:

```bash
python3 scripts/build_bank_reallocation_from_journals.py ap-miscoded
# → bank_ap_miscoded_payments_batch_update.tsv (~$5k)
```

### Worked example (`2-2201` +$49.69)

| Step | Finding |
|---|---|
| Measure | `2-2201` Manager +49.69; MYOB TB / journals net 0 |
| Dictionary | Opening Dr 722.82 + 16 VWFS monthly Cr + CIB Adj 6/12 = 0 |
| Classify | 14/16 monthly bank payments already had Dr `2-2400` / Cr `2-2201`; **Oct 2015** twins (26/10 bank, 24/10 MYOB) lacked the pair |
| Quantify | Missing −24.37 −25.32 = **−49.69** = exact variance |
| Fix | Payment Batch Update adding the offsetting GST lines (bank total unchanged) |

Outputs: `bank_hp_gst_deferred_oct2015_payments_batch_update.tsv`,
`reconcile/gst_deferred_2201_alignment.tsv`.

### Worked example (`1-3000` −$203)

| Step | Finding |
|---|---|
| Measure | Electronic Clearing Manager −203; MYOB 0 |
| Dictionary | 14 MYOB lines net 0 (pay-run EP pairs + one ATO pair) |
| Classify | Bank receipt CR000054 Cr `1-3000` present; non-bank bill payment **177** Dr `1-3000` / Cr AP **missing** |
| Fix | Journal Batch Create remapping MYOB chart AP → builtin AP |

Output: `journal_ato_electronic_clearing_177.tsv`.

### Worked example (AP after early GJ — fair compare)

| Step | Finding |
|---|---|
| Measure | Builtin AP still off after early GJ PIs + payment links |
| Dictionary | ~99.6% of AP docs match when aggregated per journal |
| Classify | Largest MYOB-only: bill payment **1213** \$2,639 (ATO) — same cash Manager put on BAS (§3.3). Real gap: CR000128 AAMI refund Cr AP / Dr director |
| Fair compare | Live variance − intentional \$2,639 ≈ residual to chase |
| Fix | `journal_aami_refund_cr000128.tsv`; do **not** move BAS BPAY onto AP |

Outputs: `reconcile/ap_1800_alignment.tsv`, `reconcile/ap_1800_bridge.tsv`.

### Scripts that already use the journal dictionary

| Script | Uses journals for |
|---|---|
| `build_bank_suspense_recategorization.py` | Round-2 bank line → MYOB journal account structure |
| `build_bank_reallocation_from_journals.py` | Post-load reallocate BAS / miscoded-AP bank lines from MYOB twins |
| `extract_journals_by_ref.py` | Copy missing non-bank journal rows by Reference (watch recycled refs) |
| `build_nonbank_journals.py` | Year-end / CIB / Pay runs with no bank line |
| `build_early_sales_from_journals.py` | Reconstruct SIs when MYOB has no invoice document |
| `build_early_purchases_from_journals.py` | Reconstruct PIs when MYOB has no Bill document |
| `reconcile_manager_to_myob.py` | Prove Manager **report export** vs MYOB TB (post-load); `--live-api` for fast iteration, see [live-trial-balance.md](live-trial-balance.md) |
| `align_account_to_journals.py` | Bound-account MYOB↔Manager match → alignment / bridge / residual |
| `build_ap_close_journal_cleared_pis.py` | (Ineffective for BS) ClosedInvoice on journal-cleared PIs — kept for review only |
| `build_ap_payment_clear_journal_ghosts.py` | **Superseded** — clearing-account interim hack, kept for history only |
| `fix_ap_ghost_pi_journals.py` | Real fix for ghost PI Balance due: delete duplicate imported Journal(s), pay the PI for real from operating bank |
| `build_ar_clear_opening_customer_credits.py` | Strip customer from opening-clearance AR receipts (BS credit); restore bank if wiped |

**Rule of thumb:** if a control account is wrong in Manager, the answer is almost
always already sitting in `journal_dictionary.tsv`. Read it before inventing a fix.
**Refs recycle** across years (`177`, bill numbers) — filter by date + narration, not
Reference alone, when extracting.

---

## 7. Script command sequence (happy path)

```bash
# --- data build (MYOB exports + Playwright harvests already in place) ---
python3 scripts/validate_categories.py
python3 scripts/build_chart_of_accounts.py
python3 scripts/build_contacts.py
python3 scripts/build_journals.py                    # lookup + reconcile source
python3 scripts/build_opening_balances.py            # needs bank account already in Manager
python3 scripts/build_purchase_invoices.py --by-year
# bank: download_bank.py → exports/myob/bank/; categorize via §4–§5 + build_bank_from_harvest.py
python3 scripts/build_bank_categorization.py
python3 scripts/build_bank_suspense_recategorization.py
python3 scripts/build_bank_from_harvest.py           # round 3 from harvest

# --- Manager load (order matters) ---
# 1. seed_manager_coa.py (Manager quit) — still SQLite
# 2. Create Bank and Cash Account 1-1110; set Start Date; set Starting balance
# 3. apply_manager_api.py → suppliers, customers (or legacy Batch Create)
# 4. apply_manager_api.py → opening journal
# 5. apply_manager_api.py → purchase invoices (by year)
# 6. attach_purchase_images.py (Manager quit) — still SQLite
# 7. apply_manager_api.py → reopen invoices (Batch Update TSV)
# 8. Import bank statement CSV (UI)
# 9. apply_manager_api.py → payments/receipts round 1 (by year)
# 10. apply_manager_api.py → director clearing journals (one invoice per journal)
# 11. apply_manager_api.py → payments/receipts round 2 (by year)
# 12. apply_manager_api.py → non-bank adjustments + pay runs
# 13. Manually clear bank_suspense_remaining.tsv
# 14. Export Manager TB/BS; python3 scripts/reconcile_manager_to_myob.py
# 15. For each remaining BS variance: filter journal_dictionary.tsv (§6a), apply_manager_api.py
```

---

## 8. Known leftover classes

| Leftover | Typical cause | Manual action |
|---|---|---|
| No journal match | Card spend not journalled same day/amount; personal transfers | Expense account or director advance by judgement |
| AP supplier unresolved | Refund / receive-refund journals with no supplier name | Match to supplier credit or income |
| AR receipts without sales | Sales never imported as Sales Invoices; or early `General journal; Sale` only | Harvest SIs, or `build_early_sales_from_journals.py`; see §6a / §9 |
| Duplicate bill numbers | Wrong PI linked if payment_ref ignored | Fix via Batch Update with correct Reference |

---

## 9. Sales side

Customer receipts must credit **builtin Accounts receivable** and link the Sales
Invoice. Chart `1-1800` alone will not clear Balance due.

### Harvest (Playwright BFF)

```bash
cd scripts/myob_playwright && source .venv/bin/activate
python3 download_invoices.py harvest
python3 download_invoices.py download
# Optional: keyword pull for specific numbers (fails if MYOB has no invoice document)
python3 download_invoices.py refetch --from-num 111 --to-num 139
```

Archive: `exports/myob/invoices/by_invoice/*/invoice.json` (~167 from harvest). Spec:
[specs/003-sales-invoices.md](../specs/003-sales-invoices.md).

### Batch Create (harvested invoices)

```bash
python3 scripts/build_sales_invoices.py --by-year
```

1. Paste `out/manager/sales_invoices.tsv` → Sales Invoices → **Batch Create** (once only).
2. Do **not** paste reopen/ref TSVs as Create — that produces empty duplicate invoices.
3. Verify receipts actually settle invoices. **Do not rely on a plan to do
   this "later," and do not trust "balance shows zero" as proof it's
   correct.** A Sales Invoice with no matching cash shows `Overdue` forever
   — but builtin-AR cascade can also make an invoice show a clean
   `PaidInFull` while attached to the *wrong* specific Receipt, if some of
   the customer's real payment history never made it into Manager (a
   split-payment leg going missing is the common cause). `scripts/link_ar_receipts_to_invoices.py`
   (description/amount/date-window matching, editing existing
   Receipts/Journals in place) **does not actually work on this API** —
   confirmed live that its `SalesInvoice` field assignment silently fails
   to persist on both create and update. Use
   `scripts/audit_ar_receipts_vs_myob.py` instead: it cross-checks every
   individual source-system payment record (not the invoice aggregate)
   against Manager's live Receipts/Journals and reports exactly which ones
   are missing, so you can reconstruct them (POST new Receipts dated/amounted
   per the source record) and let cascade resolve the rest correctly on its
   own. Full mechanics, the worked example that surfaced this, and why this
   needs a different risk model than the AP side:
   [invoice-linking.md § Accounts Receivable](../../manager-automation/reference/invoice-linking.md#accounts-receivable--mirror-problem-different-risk-profile).

### Early sales that are journals only

MYOB Business sometimes posts early sales as **`General journal; Sale`** rather than
Sales Invoice documents. They appear in `journal_dictionary.tsv` but **cannot** be
harvested via the invoice BFF. Reconstruct these with
`scripts/build_early_sales_from_journals.py`: find them in
`journal_dictionary.tsv` (`General journal` type + `Sale;`-prefixed
description, with no matching harvested `invoice.json`) and recreate them
as proper Sales Invoice documents. Some source exports split a single line
amount into two adjacent rows (e.g. a tax-sized row and a total-sized row)
— sum every matching row per transaction, not just the first, or
multi-line invoices get badly undercounted.

Re-reconcile (`§6a`) after any batch of AR fixes. Some opening-clearance
receipt credits genuinely predate the invoice harvest's coverage entirely
— a customer with Receipts but zero Sales Invoices at all is the tell —
and stay unlinked on purpose: there is nothing to link them *to*, and they
correctly fall back to reducing the opening AR balance instead of pointing
at an invoice.

Header sample: `samples/sales_invoice_batch_update.tsv`.

Verify paid status on the Sales Invoices **list** (Balance due), not the Edit form.
`Paid by` on the receipt may stay Other.

### Matching a source system's invoice appearance -- custom HTML themes

Pure Manager feature, not MYOB-specific -- full detail (the `/api4/view-v1`
theme contract, Country-localization-gated business fields, Business-scoped
Custom Fields as the portable alternative, report themes) moved to
[manager-automation reference/custom-themes.md](../../manager-automation/reference/custom-themes.md).
