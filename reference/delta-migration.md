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

## `config/myob_migration_diffs.tsv` was referenced but never built until 2026-08-13

Same pattern as `apply_manager_api.py` and the original `attach_purchase_images.py`'s
`lib_myob` dependency above: referenced by name twice in this skill
(`SKILL.md`'s Agent habits, `manager-import.md` §6a) as where "intentional
remediations" live, but the file itself never actually existed in any
project using this skill until built for a real need — netting known,
already-investigated, structural MYOB-vs-Manager diffs (see
`docs/MIGRATION_DIFFS.md` for what these look like) out of a
reconciliation run, so what's left over is only genuinely new/unexplained
activity. **Before trusting a skill doc's reference to a file/script by
path, check it actually exists** — the doc reference alone isn't
evidence.

Format: `account_code\tscope\tknown_diff\texplanation`, where `scope` is
`ongoing` (a structural, permanent diff — a report-bucketing quirk, a
fixed historical misstatement, a rounding artifact on an untouched asset —
that recurs identically at *every* future comparison date) or `opening`
(specific to the one 2015 opening-balance comparison only, never applies
elsewhere). **`known_diff` must be signed to match `manager_value -
myob_value` exactly**, not just the magnitude shown in a docs table —
storing a bare magnitude for a diff that's actually negative doubles the
error instead of canceling it (confirmed the hard way: an opening-date
exception stored as `+22852.50` when the real diff was `-22852.50` netted
to `-45705.00`, the *opposite* of the intended zero). `reconcile_manager_to_myob.py`
loads this file and nets it against every raw account-level diff before
deciding pass/fail, printing both the raw and net figures so nothing is
silently hidden — a code with a registered exception whose net still
isn't zero is flagged loudly (`*** net still nonzero ***`), never quietly
absorbed. This is also a real early-warning signal: if a "known" baseline
suddenly stops explaining the observed diff (confirmed 2026-08-13 —
`3-1600`'s documented `$192.60` no longer matched a live `$12,816.78` raw
diff at the same checkpoint), that's new drift needing its own
investigation, not something to paper over by inflating the stored
exception to match.

## Architecture

```
Manual, human-run (MYOB session can't be scripted end-to-end — see below):
  myob_playwright/download_bills.py     login → harvest → download
  myob_playwright/download_invoices.py  harvest → download
  myob_playwright/download_journals.py  --fy <FY>

Fully unattended after that (no more MYOB interaction):
  myob_delta/filter_delta.py           read-only: what's new, not yet in Manager
  myob_delta/apply_bills_invoices.py   creates Purchase/Sales Invoices
  myob_delta/link_payments.py          links Suspense-parked bank Payments/Receipts
                                        (MYOB-specific candidate sourcing only --
                                        the actual matching/linking engine is
                                        manager-automation's link_open_invoices.py,
                                        imported not reimplemented, see below)
  myob_delta/apply_journals.py         creates standalone adjusting journals
  myob_delta/delta_migrate.py          orchestrates all of the above

Separate manual step, Manager Desktop quit required (not part of
delta_migrate.py -- lives in manager-automation, not this skill, since the
attach mechanism itself is pure Manager knowledge -- see below):
  ../manager-automation/scripts/attach_purchase_images.py
```

**`apply_bills_invoices.py` never attaches receipt images** -- creating the
Purchase Invoice and attaching its substantiating document are two
different write paths (REST API vs. direct SQLite, see below), so this is
a deliberate, separate step, not an oversight to fold into the orchestrator.
Run `manager-automation`'s `attach_purchase_images.py` after a
`delta_migrate.py --apply` batch, once Manager Desktop is fully quit. That
script consumes this project's own `exports/myob/bills/by_bill/` archive
(the same Reference convention as the rest of this pipeline), so despite
living in the Manager-only skill it works against this project's harvest
with no extra wiring.

This script was briefly, incorrectly believed to not exist at all (same
mistake as the `apply_manager_api.py` one above) — the search that missed
it didn't follow the `.claude/skills/*` symlinks into the sibling skill
repos, so a real, working script sitting in `manager-automation/scripts/`
went unseen. It did need a real fix once found: it depended on a
`lib_myob` module and a `purchase_invoice_image_manifest.jsonl` file, both
genuinely gone (same lost pre-API-first builder pipeline as
`apply_manager_api.py`) — fixed by building the reference→folder manifest
directly from the harvest archive and resolving each Purchase Invoice's
Key via the REST API instead. Lesson for next time: a "does this exist"
search across a project using symlinked skills must resolve/follow those
symlinks (`find -L`, or search the skills' real repo paths directly), or
it will silently skip everything inside them.

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

## A negative-total bill is a Debit Note, not a negative Purchase Invoice

A MYOB bill with a negative total (a supplier refund/credit — e.g. an
insurance policy adjustment) is Manager's **Debit Note** document type,
not an ordinary Purchase Invoice posted with a negative amount. Confirmed
2026-08-12 by direct instruction, and independently corroborated: one
existing record created as a negative Purchase Invoice by an earlier
version of this pipeline had its own `Description` field already reading
`"Purchase; <Supplier> (credit note)"` — a leftover marker from whoever
built the *original* migration recognizing this was conceptually a credit
note, without the tooling at the time actually using the right document
type. `apply_bills_invoices.py` now branches on `bill["totals"]["total_inc_tax"]
< 0` and posts to `debit-note-form` instead of `purchase-invoice-form`
when true (`build_debit_note_payload`).

**Debit Note API facts, confirmed via disposable test records (create,
verify, delete) the same way the `AmountsIncludeTax` placement bug was
pinned down**:
- Needed adding to `lib_manager_api.py`'s `FORM_PATHS`/`LIST_PATHS` —
  neither `debit-note` nor `credit-note` were mapped at all before this.
- Line shape is **identical** to `purchase-invoice-form`: `PurchaseUnitPrice`,
  `TaxCode`, and the same top-level (not per-line) `AmountsIncludeTax`
  convention documented above.
- **Amounts go in as their positive magnitude, not the source's negative
  sign.** A Debit Note's own document type already encodes the
  AP-reducing direction (the same way a Credit Note does on the AR side)
  — sending a negative amount on top of that double-negates. Confirmed
  against a real bill: MYOB's `$-608.11` (lines `$-552.83`/`$-55.28`
  inclusive) posts correctly as a live `$608.11` Debit Note when the lines
  are sent as `+552.83`/`+55.28`.
- **The `/debit-notes` list endpoint does not expose `Reference` on its
  rows** (only key/date/supplier/description/amount) — unlike
  purchase-invoices/sales-invoices, where `reference` is free on the list
  call. Existence/dedup checking needs a per-record `get_form` to read
  Reference (`manager_index._build_dn_index()`) — acceptable since
  negative-total bills are rare, nothing like the thousands-of-records
  volume that makes purchase-invoice dedup need to stay list-only.
- `manager_index.match_bill()` now also returns a `debit_note` key
  alongside `purchase`, checked by both the dedup-skip logic in
  `apply_bills_invoices.py` and (going forward) any payment-linking script
  that needs to recognize a bill as already migrated regardless of which
  document type it landed in.

**Scope of the 2026-08-12 cleanup, deliberately limited**: only the 2
bills that were *currently unresolved* (AAMI `$-608.11`, and a 2016 QBE
`$-87.62` bill that pre-dates this delta-migration tooling entirely) were
converted — delete the wrong Purchase Invoice, create the correct Debit
Note in its place, same Reference. A further ~50 negative Purchase
Invoices exist across the business's full history (2016–2026) and were
**deliberately left alone** — they're already `PaidInFull`/`balanceDue:
$0`, i.e. already correctly reconciled, and retroactively converting
settled history for pure consistency risks breaking something that
currently works for no functional benefit (the exact risk Golden Rule 10
warns about). Revisit only if specifically asked to.

### Settling a Debit Note: Receipts, not Payments — and the same `PurchaseInvoice` field

A negative bill represents money coming back *in* (a refund), so the
matching unlinked bank line is a **Receipt** (money in), not a Payment
(money out) — confirmed by finding the real historical Receipt that
settles one of the ~50 already-correct negative Purchase Invoices
(`J7822`, "AAMI: Debit from 00001026"), located via an offline SQLite
protobuf scan (no live-record set makes an exhaustive per-record API scan
of ~4000 Payments/Receipts practical — see [manager-automation's Hard-won
facts](../../manager-automation/SKILL.md) on that N+1 cost) rather than
guessing at the mechanism.

**There is no separate `DebitNote` field on a Payment/Receipt line —
Manager reuses the same `PurchaseInvoice` field for both document types.**
Tried `DebitNote` first (by analogy with the field naming pattern); it was
silently dropped (confirmed via offline SQLite decode: the field never
made it into the persisted protobuf). Confirmed instead, via a disposable
test Receipt (create, verify field 8 in the raw protobuf, delete), that
setting `PurchaseInvoice` to a **Debit Note's** key works and persists
correctly — `link_payments.py`'s Receipt-linking branch does exactly this:
`Account=builtin AP`, `AccountsPayableSupplier=supplier key`,
`PurchaseInvoice=<the Debit Note's key>`, same field name, same builtin
AP GUID, on a Receipt instead of a Payment.

Real-world proof of this working: applying it against the live AAMI Debit
Note immediately found and linked a genuine matching Receipt already
sitting unlinked in Suspense ("VISA Refund-AAMI INSURANCE BRISBANE CITY",
`$608.11`, dated 2026-08-08) — the refund had already hit the bank feed,
it just hadn't been categorized yet.

**A Debit Note exposes no `balanceDue` on the API** (its list rows only
carry key/date/supplier/description/amount, confirmed by direct
inspection) — so unlike the Purchase-Invoice-linking branch,
`link_payments.py` cannot re-fetch and confirm a post-link balance for
Debit Notes; it can only confirm the write itself didn't error. If a
future need arises to verify a Debit Note is actually "closed" after
linking, the underlying AP ledger impact (not this list endpoint) would
be the thing to check.

### Debit Notes need the same substantiating PDF as Purchase Invoices

`attach_purchase_images.py` (in `manager-automation`) originally only
indexed the Purchase Invoice `ContentType` GUID. Extended 2026-08-12 to
also index Debit Notes (`ContentType 274fc6d0-2eac-43d0-8286-79c856e644aa`,
confirmed via the same offline SQLite decode) into the same
Reference→Key lookup — Debit Note `Reference` is top-level protobuf field
2, identical to Purchase Invoice, and Images rows are keyed by the parent
record's own Key regardless of which document type it is, so no other
part of the attach pipeline needed to change.

## A bill "not found" among bank payments can be paid by more than one payment

`link_payments.py` originally only searched for a single Suspense payment
whose amount exactly equalled a bill's balance. **Confirmed 2026-08-12: 3
of 4 apparent "no-match" bills weren't actually missing a payment at all**
— a foreign-currency card charge posts as **two separate bank lines**, the
converted charge itself plus a separate international-transaction-fee
line, both same-day, that only sum to the bill's AUD total *together*
(e.g. OpenAI `$29.58` bill = `$28.72` charge + `$0.86` fee; same pattern
for ISC2 and Cursor). A genuine multi-instalment payment against one bill
is the same shape. Single-payment-only matching reports these as
unresolved even though the money is fully, correctly present.

Fix: search combinations of up to `GROUP_SIZE_MAX` (3) Suspense payments
for an exact-cent sum, not just single payments — see
`manager-automation`'s [reconciliation-matching.md](../../manager-automation/reference/reconciliation-matching.md)
for the general technique (bounded candidate pool, exact-cent group sums,
one shared greedy assignment across every combo size so a clean
single-payment match always outranks a coincidental multi-payment one).
Linking is unchanged per payment — each payment in a winning combo gets
`PurchaseInvoice` set individually; Manager sums the applied amounts
across multiple linked Payments on its own, no proration needed.

**Not every remaining no-match is this** — a genuinely negative/credit
bill balance (a supplier refund) won't resolve via any combination of
ordinary (positive) Suspense payments, and shouldn't; that's a different
situation (likely needs a Receipt, or a credit note, not a Payment link)
and the script correctly leaves it flagged rather than forcing a match.

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

## Resolved: `AmountsIncludeTax` must be top-level on the payload, not per-line

Two failure modes were chased on this before the real cause was found —
both are worth knowing since either can recur if `AmountsIncludeTax` is
placed wrong again:

1. **Per-line placement (the original bug, 2026-08-12 batch of 30
   invoices)**: `AmountsIncludeTax: true` set *inside* a `Lines[]` entry is
   silently dropped by `purchase-invoice-form`/`sales-invoice-form` — absent
   from the form on a GET-after-POST round trip — and Manager falls back to
   exclusive-tax behavior, adding the TaxCode's rate **on top** of whatever
   unit price was given. Confirmed by direct round-trip: a $62.39
   tax-inclusive bill posted with `Lines: [{PurchaseUnitPrice: 62.39,
   TaxCode: <GST 10%>, AmountsIncludeTax: true}]` came back as a **$68.63**
   invoice (62.39 × 1.1). Hit 29 of 30 invoices in that batch-create run;
   the 30th was unaffected only because its lines used no TaxCode at all.
2. **The interim "fix" (send tax-exclusive, omit the flag) traded that for
   a subtler bug**: letting Manager compute tax from the exclusive amount
   via the TaxCode's rate means Manager does its own cents rounding, which
   can disagree with the source system's rounding on an exact half-cent
   tie. Confirmed on bill `00001060`: MYOB's own tax calc on
   `amount_ex_tax=86.75` at 10% (`8.675` exactly) rounded to `8.67`;
   Manager's identical calc rounded to `8.68` — same math, opposite
   tie-break, invoice posted as `$95.43` against a real `$95.42` bill. This
   is what "Manager sometimes rounds up the GST amount by a cent" looks
   like in practice — not a bug in the rate or the TaxCode, a rounding
   convention mismatch that only surfaces on amounts landing exactly on
   `x.xx5`.

**The actual fix, confirmed 2026-08-12 by creating, verifying, and
deleting disposable test records (both `POST` and `GET`-merge-`PUT`)**:
`AmountsIncludeTax` is a **whole-invoice** field and belongs at the **top
level** of the payload, a sibling of `Lines`, not nested inside any line:

```json
{
  "IssueDate": "...", "Reference": "...", "Supplier": "...",
  "AmountsIncludeTax": true,
  "Lines": [{"PurchaseUnitPrice": 95.42, "TaxCode": "<key>", ...}]
}
```

Placed there, it works correctly and the resulting `invoiceAmount` matches
the tax-inclusive unit price exactly — Manager never needs to re-round
anything, so it can't disagree with the source system's own rounding.
Verified against the same `00001060` bill: exclusive-price construction
gave `$95.43`; inclusive-price + top-level flag gave the correct `$95.42`
on both a fresh create and a GET-merge-PUT of the existing (wrong) record.

**Current guidance**: send the tax-**inclusive** unit price on every
tax-coded line, and set `AmountsIncludeTax: true` once, at the top level
of the payload, whenever any line carries a `TaxCode`. `apply_bills_invoices.py`
does this and also re-verifies every created record's live `invoiceAmount`
against the source total before moving on, specifically to catch a
regression of this class immediately rather than after 30 records — that
verification step is what would have caught either of the two failure
modes above on the very first record.

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
