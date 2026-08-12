# Delta migration — keeping Manager current during side-by-side operation

For when MYOB is still the point of data entry for some transactions
during side-by-side operation (§7 of [runbook.md](runbook.md)) and you
need a repeatable, idempotent, **on-demand** way to pull what's new since
the last confirmed-good point into Manager — not a scheduled job, not a
full re-migration, just "catch Manager up."

## Why this exists, and what it replaced

The original migration docs (`manager-import.md`, `specs/001-manager-import.md`,
`SKILL.md` itself) describe a generic `apply_manager_api.py` runner with
built-in "skip if Reference exists" idempotency, backing
`build_purchase_invoices.py`/`build_sales_invoices.py`/
`apply_deferred_yearend_journals.py`. **None of these exist in any project
using this skill, confirmed by a full git-history search** — they were
removed in an early cleanup pass and never rebuilt. Don't go looking for
them. The real, live, working pattern for Manager writes in this skill
(and its `manager-automation` dependency) is **direct
`lib_manager_api.ManagerAPI` calls** (`post_form`/`get_form`/`put_form`)
written per-script — see `manager-automation`'s
[invoice-linking.md](../../manager-automation/reference/invoice-linking.md)
for the field-level conventions those calls must follow.

The scripts below (`scripts/myob_playwright/`, `scripts/myob_delta/`)
build the apply+dedup layer the docs assumed already existed, on top of
that real pattern. If you're forking this skill, treat these as the
current canonical way to do delta writes — not the old
`apply_manager_api.py` references elsewhere in this skill's docs, which
are historical/aspirational and should be read with that in mind until
cleaned up.

## Architecture

```
Manual, human-run (MYOB session can't be scripted end-to-end — see below):
  myob_playwright/download_bills.py     login → harvest → download
  myob_playwright/download_invoices.py  harvest → download
  myob_playwright/download_journals.py  --fy <FY>

Fully unattended after that (no more MYOB interaction):
  myob_delta/filter_delta.py           read-only: what's new, not yet in Manager
  myob_delta/apply_bills_invoices.py   creates Purchase/Sales Invoices
  myob_delta/link_payments.py          links Suspense-parked bank Payments
  myob_delta/apply_journals.py         creates standalone adjusting journals
  myob_delta/delta_migrate.py          orchestrates all of the above
```

`myob_playwright/manager_index.py` is the dedup engine both halves share —
composite Reference for Purchase Invoices, verbatim number for Sales
Invoices, a `[MYOB-DELTA ref/date]` Narration marker for Journals — all
checked live against Manager's REST API, no per-record `get_form` scan
needed for the existence/already-paid checks (list endpoints already
expose `reference`/`balanceDue`/`narration`).

Per-project config these scripts read (create these when forking):

| File | Contents |
|---|---|
| `config/myob_business_id.txt` | The MYOB Business ID GUID, from the URL `https://app.myob.com/#/au/<id>/...` |
| `config/manager_business_name.txt` | Manager business name, for `backup_manager_business.py --business` |
| `config/myob_tax_code_map.tsv` | `myob_code\tmanager_tax_code_name` — MYOB's short AU tax codes aren't universal Manager tax-code *names* (those are per-business); `"N-T"` is deliberately never a key, it means "no TaxCode at all" |
| `config/real_bank_accounts.tsv` | `code\tname` of genuine bank/cash accounts — a journal group touching one is already captured by the bank feed |
| `config/last_migration_date.txt` | The moving cutoff (SKILL.md Golden Rule 7) |

## MYOB sessions are extremely short-lived — plan around it, don't fight it

Confirmed on a real business 2026-08-12: a saved Playwright `storage_state`
session can expire **within about a minute** of a fresh script start, even
immediately after a successful run. This is not a one-off flake — it
happened five times in one session, including once mid-script between two
sequential UI actions. Practical consequences:

- **Harvesting cannot be one unattended command.** Each of
  `download_bills.py`/`download_invoices.py`/`download_journals.py` may
  need a fresh interactive `login` (real browser, human completes MFA,
  confirms the business) immediately before the run that actually needs
  data — not "log in once this week." `delta_migrate.py` deliberately does
  **not** attempt to drive this itself.
- **When iterating/debugging a new Playwright flow against MYOB, do the
  whole sequence in one continuous script run**, not several separate
  exploratory calls each starting a fresh browser context. A cold restart
  is exactly when the session is most likely to have already expired.
- **A non-interactive way to check session validity** (before spending a
  login on a run that will just fail): launch headless, load the saved
  `storage_state`, navigate to a known in-app URL, check whether the
  landing title/URL is the MYOB login page (`id.myob.com`) or the real
  page. Cheap, and confirms the failure mode precisely instead of a
  confusing mid-script exception.

## MYOB report export mechanics (Journal entries)

Not discoverable by URL-guessing — required five discovery passes to nail
down (documented in full so nobody has to redo this):

1. **Reports live on a different subdomain** (`reports.myob.com`), not
   `app.myob.com`. A cold `page.goto()` straight to a guessed
   `reports.myob.com` URL does not reliably carry the session over from a
   `storage_state` snapshot — reach it only by clicking through the UI
   from `app.myob.com`: side-nav **"Reporting"** → **"Reports"** →
   **"Journal entries"**. The resulting URL is stable once you're there:
   `https://reports.myob.com/#/au/<BUSINESS_ID>/journals`.
2. **"Date from"/"Date to" are real labeled inputs**, fillable directly
   like the Bills/Invoices date-range flow.
3. **The report defaults to collapsed** (one row per transaction, Debit/
   Credit show the transaction *total*, no account breakdown) — exactly
   the trap [runbook.md](runbook.md) §2 already warns about for the manual
   process. A collapsed export looks perfectly valid and is useless.
4. **"Customise" opens a dialog that already lists the right 5 columns**
   (Ref no, Code, Category name, Debit ($), Credit ($)) by default — but
   its **Apply** button only configures *which* columns would show in
   expanded view. It does **not** switch the report into expanded view.
5. **The actual collapsed/expanded toggle is the separate "Expand all"
   button** next to Customise. Only after clicking that does the export
   contain real per-line detail: each transaction becomes a repeated
   `Ref no | Code | Category name | Debit | Credit` sub-table followed by
   a `Total amount` row — this is what `build_journals.py` actually
   parses, and what the manual runbook process has always required.
6. **"Export" reveals an Excel/PDF choice**, not an immediate download —
   click "Excel" to trigger the real file download.

Full working sequence: `START_URL → click "Reporting" → click "Reports" →
click "Journal entries" → fill Date from/Date to → click "Customise" →
click "Apply" → click "Expand all" → click "Export" → click "Excel" →
capture the download.` See `myob_playwright/download_journals.py` for the
implementation.

**A MYOB "Bill" journal line's Debit/Credit is the transaction's gross
double-entry total, not its face value**, when any line is a contra
(negative) posting — e.g. an ATO BAS bill with lines `+3640 / -676 /
+3690` (net $6,654 payable) shows as a `7,330 / 7,330` transaction in the
Journal entries report (sum of the two positive/debit-side lines =
sum of the two credit-side postings, including the implicit credit to
Accounts Payable). Not a data error — just MYOB's GL display convention.
`build_journals.py` already handles this correctly (parses per-line, never
trusts the summary row); don't reconstruct a bill's real total from this
report's top-level Debit/Credit columns.

## The "recovering deferred non-bank journals" filter, validated

The technique already documented in [runbook.md](runbook.md) — group by
`txn_id`, drop any group touching a real bank account, drop `txn_type` in
`{Bill, Invoice, Sale, Bill payment, Pay run, Supplier return applied,
Invoice payment, Receive refund}` — was directly validated 2026-08-12
against a real business's FY2026 data: of 801 transactions, it correctly
surfaced exactly the 5 known genuine standalone journals (an "End of Year
Adjustment" equity shuffle, an FBT-instalment write-off, a depreciation
catch-up, an FBT expense reallocation, and an employee-contribution FBT
journal) and nothing else. `filter_delta.candidate_journal_groups()` is
this filter, implemented and tested.

**Don't use `txn_id` as a persisted dedup key** — confirmed unstable by
`build_journals.py`'s own docstring (reassigned on every regeneration).
`manager_index.journal_marker(ref_no, issue_date)` — embedded as
`[MYOB-DELTA {ref_no}/{issue_date}]` in the created Journal's Narration —
is the stable alternative. **Don't reuse a bare `[MYOB ...]` bracket
format either**: confirmed 2026-08-12 that every historically-migrated
journal already carries an unrelated `[MYOB txn NNNN]` annotation from the
original import, and a naive `\[MYOB [^\]]+\]` regex matched 55 of them
before any delta-migration journal had ever been created — which would
have made every future journal look like a false-positive duplicate
forever. Keep marker formats for different purposes visually and
programmatically distinct (`MYOB-DELTA` vs bare `MYOB`).

## Confirmed API bug: `AmountsIncludeTax` does not persist on invoice creation

**`AmountsIncludeTax: true` on a `purchase-invoice-form`/`sales-invoice-form`
POST is silently dropped** — absent from the form on a GET-after-POST
round trip — and Manager falls back to exclusive-tax behavior, adding the
TaxCode's rate **on top** of whatever unit price was given, regardless of
the flag. Confirmed by direct round-trip: a $62.39 tax-inclusive bill
posted with `PurchaseUnitPrice: 62.39, TaxCode: <GST 10%>,
AmountsIncludeTax: true` came back as a **$68.63** invoice (62.39 × 1.1).
Affected **every** tax-coded invoice in one batch-create run (29 of 30);
the 30th was unaffected only because its lines used no TaxCode at all.

This is a close cousin of the already-documented
`manager-automation` hazard ("`AmountsIncludeTax:false` can be a silent
no-op, understating the total") but the opposite direction and a
different confirmed trigger (creation via `post_form`, not an existing
form edit) — both amount to the same rule: **never trust
`AmountsIncludeTax` to do what its name says. Always send the
tax-EXCLUSIVE unit price with a TaxCode and omit `AmountsIncludeTax`
entirely** — that matches what Manager actually does when a TaxCode is
present, and verify with a live GET-after-POST round trip on the first
record of any new batch before trusting the rest. `apply_bills_invoices.py`
does this and also re-verifies every created record's live `invoiceAmount`
against the source total before moving on, specifically to catch a
regression of this class immediately rather than after 30 records.

## Recycled MYOB numbers are a live risk, not just a historical one

Golden Rule already covers dedup-key construction
(`invoice-linking.md`'s `<myob-ref>-<YYYYMMDD>` convention) — this is
about **acting on a match result**, not just building the key. A one-off
fix/correction script that resolves "which live Manager record does this
MYOB number belong to" via anything less strict than the full composite
key (e.g. collapsing several source rows into a `{bill_number: date}`
dict, silently keeping whichever row happens to be last) can point a
correction at a **completely unrelated historical record that happens to
share the bare number**. Confirmed 2026-08-12: bill number `00001053`
belongs to both a real 2026 transaction and an unrelated 2016 bill to a
different supplier; a naive lookup during a correction script's own
development pointed at the 2016 record. Caught in dry-run before any
write, but only because dry-run output was actually inspected rather than
trusted. **Always iterate per-source-row (bill_number *and* issue_date
together), never collapse to a bill_number-keyed dict** — and add a
belt-and-braces check that a resolved live record's own Reference matches
what was expected before writing to it, in any script that resolves a
MYOB number to a live Manager key for the purpose of *modifying* that
record (creation is lower-risk; modification of the wrong record is not).
