# Reconciling a live Manager instance against MYOB export data

General Manager knowledge used here (computing a Trial Balance from live
API data instead of a report export, the tax-line exposure caveat, and the
Retained-Earnings/equity-rollup blind spot with its year-by-year real
P&L-closing fix) has moved to
[manager-automation reference/live-trial-balance-computation.md](../../manager-automation/reference/live-trial-balance-computation.md) —
read that first if you haven't. What follows here is specific to comparing
a live Manager instance against **MYOB's own exports** during a migration.

## When Manager consolidates two source-system accounts into one

Some source-system structures don't map 1:1 onto Manager's design at all —
not a data error, a genuine structural difference (see
[manager-automation reference/tax-codes.md](../../manager-automation/reference/tax-codes.md#manager-only-supports-one-target-gl-account-per-tax-code--adopt-that-design-dont-route-around-it)
for the GST-collected/GST-paid example: Manager's tax-code model only
supports one target account, so a two-account source structure gets
deliberately consolidated onto one). Once that consolidation happens, a
line-by-line reconciliation against the source system's original two codes
will show one side pinned at zero forever and the other absorbing both —
correct, but only if the comparison itself accounts for the merge.

`reconcile_manager_to_myob.py`'s `MERGED_CODES` dict (`{surviving Manager
code: [source-system codes whose sum it must match]}`) handles this
generically: the retired code is skipped from being compared *individually*
(never shown as its own mismatch row), and instead both sides of the
comparison sum every code in the group. Add an entry here any time a
consolidation like the tax-code one is performed.

**Don't assume the retired code stays pinned at $0 forever — it can
legitimately receive new postings later, and the comparison must sum
*both* sides of the merge, not just the source system's.** A one-off
consolidation journal zeroing out the retired code at the moment of the
merge does not mean nothing will ever post to it again: a real historical
source-system journal discovered and migrated afterward (e.g. a deferred
year-end/BAS-reallocation entry) can reference the retired code by its own
account code, same as any other account, and correctly move it away from
zero again. Confirmed the hard way: comparing Manager's bare surviving
code alone against the source system's combined sum silently produced a
huge, wrong "mismatch" once the retired code picked up real new activity —
the fix was summing `manager_coded` across every code in the merge group,
exactly like the source-system side already did, not assuming the Manager
side only ever has one non-zero member.

## A gap absent from every MYOB export file can mean the export is stale, not that the gap is unexplained

If a Balance Sheet mismatch's source transaction genuinely cannot be found by
grepping every `journal_entries_FY*.xlsx` file for its account code, reference,
description, or amount, don't conclude it's unexplained or reconstruct it by
inference — **check whether the business is still being used live in MYOB
in parallel with Manager**, and whether the export files were generated at
different times. Each MYOB export file prints its own `Generated <date>`
line; compare it across files (`journal_entries_FY2026.xlsx` vs
`trial_balance_current.xlsx`, etc.) before trusting an absence. Confirmed
2026-08-04: a real MYOB general journal (`GJ000004`, backdated to
16/07/2024) was invisible in `journal_entries_FY2025.xlsx` and
`journal_entries_FY2026.xlsx` alike, purely because both were generated
*before* the journal was entered into MYOB — `trial_balance_current.xlsx`,
regenerated 10 days later, already reflected it. The tell: MYOB's own
journal-detail export and its own trial-balance export disagreed with each
other on the same account, which is impossible if both are current. Once
found (MYOB's own "Find transactions" screen, filtered to
`Transaction type = General journal`, sorted by date, is the fastest way to
spot a recent manual journal that hasn't made it into the last export
cycle), recreate it directly in Manager via the API using the exact
reference/date/lines shown in MYOB — do not skip it as unexplained. Also
check MYOB's sibling journals in the same numbering series (e.g.
`GJ000001`-`GJ000003` around a `GJ000004` gap) individually against
Manager, rather than assuming the whole series is missing or the whole
series is already migrated — in the confirmed case, 3 of 4 were already
correctly migrated and only the newest one was the actual gap.

**MYOB Business Lite has no Audit Trail report** (confirmed 2026-08-04) — there
is no way to check a transaction's actual creation/edit timestamp separately
from its (possibly backdated) transaction date. Don't suggest checking it;
if the question "was this entered before or after export X" comes up again,
the export-generation-timestamp comparison above is the only signal
available, not ground truth from MYOB itself.

**A GL-export gap isn't only a General Journal thing — an ordinary Bill can
be missing from `journal_entries_FY*.xlsx` too, with no staleness
explanation at all.** Confirmed 2026-08-05: 5 real Purchase Invoices with
genuine, correctly-harvested `bill.json` files under
`exports/myob/bills/by_bill/` (proper bill numbers, normal suppliers, clean
GST-inclusive math) simply never appeared in the GL export, under any
account code — checked against a full journal-dictionary export, not just
the account in question. This isn't the staleness mechanism above (the
bills predate every relevant export by months); it's a plain
export-completeness gap. When a Manager-only transaction can't be found in
the GL export, **check the bill/invoice harvest archive before concluding
anything is wrong** — a real harvested document there means Manager's
books are the *more* complete side, not the source of an error.

### Reconstructing a missing transaction: invoice, or a plain no-invoice line?

Not every gap that needs reconstructing represents an invoiced purchase.
MYOB (like most accounting systems) lets a bank transaction be allocated
directly to an expense account with no Bill/Invoice document at all — an
entirely normal pattern for small transactions nobody bothers tracking a
formal receipt for. Before reconstructing *any* transaction that's missing
from every export, check the bill/invoice harvest archive for a matching
document first, and let that answer decide the shape of the fix:

- **A genuine harvested bill/invoice exists** (a real `bill.json`/
  `invoice.json`, not just a plausible-looking reference number) → the
  source system really did track this as an invoiced purchase/sale.
  Reconstruct it as a real Purchase Invoice or Sales Invoice, the same as
  any other missing-document gap.
- **No harvested bill/invoice exists anywhere** → the source system never
  created one, so don't invent one either. The simpler, more faithful fix
  is a **plain Payment** (bank-funded) or **Journal** (director-funded, no
  bank line) with a direct line to the target expense account — no
  `PurchaseInvoice`/`AccountsPayableSupplier` field at all, mirroring
  MYOB's own "spend money straight to an account" structure exactly. This
  avoids fabricating an invoice number, a Balance-due/aging entry that was
  never real, and — for a bank-funded case — the director-clearing-journal
  step a synthetic invoice would otherwise need just to close itself back
  out to zero.

Determining which account to code a no-invoice line to, with no invoice to
read it from: check the bank harvest's own `matchedJournals[].contactName`
and category metadata for that specific transaction first (see
[manager-import.md §3](manager-import.md#3-bank-statement-import--harvest));
if that's unavailable too, the supplier's own most-common ("modal")
account across its *other*, real, invoiced transactions is a reasonable
fallback — but it's an estimate, not a fact read from a record, so say so
in the line description rather than presenting it as an ordinary sourced
entry. Apply this check regardless of dollar size — a few-dollar gap
deserves the same harvest-archive check as a large one, not an assumption
that small amounts are automatically safe to guess at.

## Country-specific liability-clearing accounts (e.g. Australia's BAS Clearing)

A source business may carry a liability account that exists purely to
accrue a compliance-lodgement amount at fiscal year-end before it's
actually paid the following period (e.g. a quarter's indirect-tax return
that straddles the year boundary). Don't assume a pattern like this
applies, or that every related account zeros the same way at year-end —
verify by walking cumulative account balances year-by-year across every
`journal_entries_FY*.xlsx` file. See
[tax-au.md § BAS Clearing](../../manager-automation/reference/tax-au.md#a-bas-clearing-ac-month-account-annual-gstpaygw-zeroing-fbt-excluded)
for the concrete Australian mechanism (which accounts zero, which don't,
and why) if this is an AU migration; a different country's equivalent
compliance cycle belongs in its own `tax-<country>.md`.

MYOB's own automatic EOFY closing journal (`Dr 3-1800 Current year earnings
/ Cr 3-1600 Retained earnings`, dated 1 July of the following year) is a
**separate, fully automatic** entry — it appears once the business
actually runs MYOB's year-close/lock function and needs no manual
construction; just wait for it, then migrate it into Manager the same way
as the other closing journals (see the Retained Earnings section above).

## Calibration: use the Opening TB as ground truth

An opening/inception-date Trial Balance predates every Purchase/Sales
Invoice in the business, so a correct live computation should match the
official PDF export for that date **to the cent** using only
Journal/Payment/Receipt data. `--calibrate` checks exactly this against
`exports/manager/trial_balance_opening.pdf`. A residual mismatch here can
itself be diagnostic rather than a bug: for example, if a builtin AR/AP
posting was moved onto a builtin control account without a Customer/
Invoice link, some report engines bucket unlinked builtin postings under an
unlabeled "Suspense"-style line rather than showing them on the account's
own coded line, while a live-ledger sum will include them on the coded
line directly (arguably the more literally correct figure, since the money
really is posted to that account — the report is just choosing to display
it differently until something claims it). Re-run `--calibrate` after any
change near AR/AP/RE and interpret any residual in this light before
assuming the tool is wrong.

## Line-by-line auditing a specific account gap (not just the balance)

Once a Balance Sheet reconciliation gate reports a real, non-zero gap on a
specific code, the natural next step is "which transaction(s)?" —
`scripts/audit_account_vs_myob.py <code>` is the go-to tool: every Manager
`/transactions` line for that code alongside every matching MYOB
`journal_entries_FY*.xlsx` line, date-sorted with running balances, plus
the day-level net-diff ranking described below. Use `audit_gst_accounts.py`
instead only when the code is a member of `MERGED_CODES` in
`reconcile_manager_to_myob.py` (currently just `2-2200`/`2-2400`) — it pools
both codes' rows into one match set before comparing, which a single-code
tool can't do. Two things make naive line-by-line matching unproductive on
either tool, discovered auditing a GST gap (see the worked example in
`docs/MIGRATION_DIFFS.md`):

1. **Manager's `/transactions` list endpoint is a genuine, exact GL read**
   — unlike reconstructing from Purchase/Sales Invoice line fields (see
   "What's NOT exact" above), it already reflects the real computed tax
   split, so it's safe to use directly for this without hitting the
   exposed/incomplete-account problem. Filter server-side with
   `term=<account code>`; paginate with `skip` (50/page cap, as always).
2. **A per-code, per-line, exact-(date,amount) match floods with false
   positives whenever Manager consolidated multiple source-system
   accounts into one** (see "When Manager consolidates" above) — an
   ordinary transaction that legitimately lands on the *other* original
   code in MYOB but the *same* single code in Manager will never find a
   partner if matched one-code-at-a-time. **Pool every consolidated
   code's rows into one match set before matching**, not per-code.
3. **Line-level granularity genuinely differs between the two systems**
   even after pooling — MYOB's exported journal report nets multiple
   invoice lines sharing the same code+category into one row, while
   Manager keeps one ledger entry per invoice line — so a single MYOB row
   can legitimately correspond to two-or-more Manager rows on the same
   date (or vice versa) with no error involved. Exact per-line matching
   (even amount-only, ignoring date) doesn't resolve this. **Aggregate
   both sides by date and diff the daily net** instead — same-day
   granularity noise cancels out, and real gaps surface as a handful of
   dates with a materially nonzero day-level diff. Rank those by
   `abs(diff)` descending: the small residuals scattered across dozens of
   dates (a few dollars or cents each) are usually already-documented
   reconstruction artifacts (see the `General journal; Sale/Purchase`
   note in `manager-import.md`); the few large ones are worth opening
   directly (`GET /purchase-invoice-form/<key>` or equivalent) to inspect
   for a missing `TaxCode` or similar real data-entry gap.

## Usage

```bash
python3 scripts/compute_live_trial_balance.py --calibrate        # validate against the Opening TB PDF
python3 scripts/compute_live_trial_balance.py --as-at 2026-06-30  # coded balances as at a date
python3 scripts/reconcile_manager_to_myob.py --live-api           # full gate, Manager side computed live
```

`reconcile_manager_to_myob.py --live-api` reuses the same MYOB-side XLSX
parsing as the PDF-based gate, skips exposed/incomplete codes (reported as
`[skip] N account(s) excluded`), and otherwise reports identically. It has
no "Manager-only" (builtin/Suspense) section — those figures fold straight
into their now-coded lines (see calibration note above), which is arguably
more informative than the PDF's fragmented view, though it can also
disagree with the PDF in magnitude for exactly that reason — don't be
surprised when a builtin-account figure differs from the PDF while still
being internally consistent.

Cost: this fetches full detail (`get_form`) for every Journal Entry,
Payment, Receipt, Purchase Invoice, and Sales Invoice — potentially several
thousand individual API calls on a business with a long transaction
history. Budget a few minutes and run it as a background task rather than
blocking on it.
