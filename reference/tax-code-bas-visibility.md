# Tax codes: BAS/GST report visibility is line-item-tag-driven, not balance-driven

Found and fixed on a live BAS reconciliation (2026-09-01, Jul–Sep 2025 and
Jan–Mar/Apr–Jun 2026 quarters). Extends `tax-codes.md`'s existing point that
"Manager's BAS/Tax Summary report is generated directly from line-item tax
codes, not from any GL account balance" — this section is the concrete
failure mode that produces, and the fix pattern for it.

## The failure mode

A transaction line can post the **correct amount to the correct account**
(including a real GST account) and still be **completely invisible to
Manager's BAS report**, because the report reads `Lines[].TaxCode`, not the
resulting ledger balance. Trial balance and GL are fine; G1/G11/1A/1B are
silently short. Nothing about the transaction *looks* broken — balances due,
AP/AR, and account balances are all correct — so this only surfaces when the
BAS itself is checked against an independent source (MYOB, in this case).

Confirmed instances of this exact shape, across multiple document types:

1. **Purchase Invoice**, single line, no `TaxCode` at all — two ASIC annual
   review fee invoices ($329 each). Full amount posted to the expense
   account; G11 never saw them.
2. **Journal Entry**, manually pre-split into three lines (Dr liability / Cr
   income / Cr `2-2200 GST collected`) with the GST **correctly calculated
   and posted to the right account**, but **none of the three lines carried
   a `TaxCode`** — an FBT employee-contribution recognition journal
   ($24,302 gross, $2,209.27 GST). GL was correct; BAS was not.
3. **Payment** (a "Spend Money"-style direct bank categorization, not linked
   to any Bill), no `TaxCode` — a recurring monthly bank fee ("Monthly
   Account Fee", $8), present essentially every month for years. Small per
   period, but systematic across the whole history.

## This is NOT limited to Purchase/Sales Invoices

`compute_live_trial_balance.py`'s docstring states Journal Entry, Payment,
and Receipt lines "carry explicit signed amounts... no hidden computation",
which is true for **balance calculation**, but was previously (wrongly)
read as implying those document types don't support `TaxCode` at all. They
do: `GET`-ing a live Journal Entry, Receipt, or Payment form all return
`"TaxCodeEnabled": true` and a `Lines[].TaxCode` slot, structurally
identical to Purchase/Sales Invoice lines. **Any script auditing for
missing tax codes must scan all six entity types** — Purchase Invoice,
Sales Invoice, Credit Note, Debit Note, Journal Entry, Receipt, Payment —
not just invoices. (Expense Claims may also qualify; not yet confirmed
either way — check before assuming they're covered or excluded.)

## Journal Entries: how Manager infers Sale vs Purchase (no explicit toggle)

MYOB's General Journal has an explicit "Display in GST report as:
Purchase/Sale" radio button, because a journal isn't inherently a sale or a
purchase. Manager's Journal Entry form has **no visible equivalent field**.

Confirmed working fix, mirrored from MYOB's own correct structure for the
FBT employee-contribution case above: put the tax code on the **Credit**
line to an **income** account, with the amount tax-inclusive and
`"AmountsIncludeTax": true` set on that line — exactly the same
auto-split mechanism Purchase/Sales Invoice lines already use:

```json
{"Account": "<liability account>", "Debit": 24302}
{"Account": "<income account>", "Credit": 24302,
 "TaxCode": "<GST 10% key>", "AmountsIncludeTax": true}
```

Manager auto-split this into `$22,092.73` net income + `$2,209.27` GST
(posted to the tax code's configured target account), matching MYOB's
figures to the cent — confirmed both by a GET-after-PUT round trip on the
journal itself, and independently by cross-checking the Profit & Loss
transactions feed, which showed the split lines each tagged `"tax": "GST
10%"`. **A Credit to a tax-coded line reads as a sale-side GST-collected
entry; a Debit reads as a purchase-side GST-paid entry** — this is the
inference mechanism Manager appears to use in place of MYOB's explicit
toggle. Not officially documented anywhere; inferred from this one
successful case. Re-verify if a future fix on this document type behaves
unexpectedly.

## Fix heuristic for a blanket "assign a tax code" script

When batch-fixing a set of no-tax-code lines (built as
`fix_purchase_invoice_no_tax_code.py` in the host project), **do not**
default every uncoded line to GST Free (or any code) uniformly. Two
carve-outs matter:

1. **A sibling line on the same document already posts directly to a GST
   account** (`2-2200 GST collected`, `2-2400 GST paid`, `2-2201 GST
   Deferred on Cash H/P`, or your business's equivalents). That's evidence
   someone already calculated the real GST amount by hand and posted it —
   the fix there is GST 10% applied to the *real, already-known split*, not
   GST Free. Blanket-applying GST Free would silently discard/obscure a
   real, already-quantified credit. Found ~29 such invoices/journals in one
   business's 11-year history (mostly 2015–2016 supplier bills and
   `CIB Adj`/`Yr End` reconciliation journals) — skip these and list them
   for manual review rather than guessing.

2. **The counterparty is the tax authority itself** (Australian Taxation
   Office, or equivalent). BAS/PAYG/FBT integrated-client settlement
   transactions are not purchases — applying any tax code to them would
   incorrectly inject them into G11/BAS totals. Always exclude by
   supplier/payee before doing anything else.

For everything else — a single-determination line with zero GST evidence
anywhere on the document — **GST Free is the safe default**: it costs
nothing if the line was in fact meant to be GST-inclusive (no credit is
fabricated either way), but it does correctly add the line's value to G11,
which a completely uncoded line never will.

## MYOB's `GNR` tax code = GST Free, not GST 10%

MYOB's `GNR` ("GST for Non-Registered purchases" — supplier not
GST-registered / no ABN quoted) is functionally equivalent to Manager's
"GST Free": no input tax credit, but still a legitimate purchase that
belongs in G11. **Don't assume every no-tax-code gap is an oversight** —
some were deliberately coded GST-free in MYOB using a code whose name
doesn't obviously say "free". When in doubt on a specific transaction,
check what MYOB actually had before assuming GST 10% was missed.

## Resolve tax code keys by name at runtime — and match on name, not rate

Existing convention (`fix_general_journal_gaps.py`) resolves the GST-10%
tax code's key at runtime via `GET /tax-codes`, matching on `rate≈10` +
`"gst" in name.lower()`, rather than hardcoding a UUID. Extend this same
pattern for GST Free, but **match on name, not rate**: a 0%/no-percentage
tax code's `rate` field shape in the API is not guaranteed to be a clean
comparable `0.0` the way GST 10%'s `rate≈10.0` is — Manager's own UI shows
`—` (not `0%`) for GST Free's rate. A rate-based filter that assumes
`abs(rate) < 0.001` can silently match zero codes even though the code
exists and is correctly configured. Match `name.strip().lower() == "gst
free"` (or `"free" in name.lower()` as a fallback with an ambiguity check)
instead, and if that still fails, dump the raw `/tax-codes` response for
debugging rather than failing blind.

## Stale local MYOB exports can masquerade as a Manager bug

When a small BAS discrepancy shows up for an already-**closed** quarter,
check the mtime of the local MYOB export files
(`exports/myob/bills/_index.tsv`, `journal_entries_FY*.xlsx`, etc.) before
concluding it's a Manager-side data or tax-code problem. If Manager's
figures match the local MYOB export **exactly**, but MYOB's *live* current
report doesn't match either, the export is simply older than a since-made
MYOB edit for a period that was supposedly already closed and migrated —
not a migration defect. Confirm by having the user re-export the specific
period fresh and diffing transaction-by-transaction against the old export
(byte-identical match = nothing changed, the real cause is elsewhere; a new
row = there's your edit). In one such case, the fresh export revealed the
real cause was actually a *different* long-standing bug (an uncoded
recurring Payment) rather than a MYOB edit at all — don't stop at "the
export is stale," confirm what actually changed (or didn't) before
concluding.
