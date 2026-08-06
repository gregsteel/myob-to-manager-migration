# Spec 003 — Sales Invoice harvest + Manager import

| | |
|---|---|
| **Decision** | Playwright BFF (same as bills), not paid API |
| **Ops** | [manager-import.md](../reference/manager-import.md) §9 |
| **Manager formats** | [001-manager-import.md](001-manager-import.md) §5 |

## Harvest

```bash
cd scripts/myob_playwright && source .venv/bin/activate
python3 download_invoices.py harvest
python3 download_invoices.py download
```

- List: `GET …/invoice/load_invoice_list_without_totals` (year windows on **DateDue**;
  offset broken for wide ranges)
- Detail: `GET …/invoice/load_invoice_detail/{id}`
- History: `GET …/invoice/load_invoice_history/{id}`
- Output: `exports/myob/invoices/by_invoice/<number>-<customer>/invoice.json`
- Payments live under `invoice.payments[]` (`reference_no` = CP*/CR*, matches bank
  `eventId`)

## Manager Batch Create

```bash
python3 scripts/build_sales_invoices.py --by-year
```

- Template: `samples/sales_invoice_batch_update.tsv`
- Output: `out/manager/sales_invoices.tsv` (+ `by_year/`)
- Paste **once** → Sales Invoices → Batch Create
- Do **not** Batch-Create reopen TSVs (creates empty duplicates)
- Customers must already exist

## Receipt → Sales Invoice linking

```bash
python3 scripts/build_ar_receipt_si_links.py
# Receipts → Batch Update → out/manager/ar_receipts_si_link_batch_update.tsv
```

Hard facts:

1. Line **Account** must be **builtin Accounts receivable** (`d1489e95-…`).
2. Set `AccountsReceivableCustomer` + `AccountsReceivableSalesInvoice` (SI Reference).
3. Match harvest payment ref → bank `eventId` → receipt Key (date/amount). Receipt
   Reference is empty.
4. `Paid by` can remain Other; line links are what clear Balance due.
5. Verify in Sales Invoices list (**Balance due**), not Edit form.

## Why not journal-only?

`Invoice` journals have AR/income/GST amounts but not Manager SI line structure or
customer GUIDs for receipt allocation. Harvest → SI is the purchase-invoice path
mirror.

## Reconstruct sales that are journals only

Some early MYOB sales exist **only** as `General journal; Sale` lines in
`journal_dictionary.tsv` — because there may be no Sales Invoice document in the 
MYOB UI/BFF depending on how/when MYOB was migrated to, so harvest cannot retrieve 
them. Reconstruct:

```bash
python3 scripts/build_early_sales_from_journals.py
```

Same pattern for any gap where Manager is missing a document that MYOB recorded
only as a general journal: **read the full journal extract, do not paste it
wholesale** — see
[manager-import.md §6a](../reference/manager-import.md#6a-resolving-balance-sheet-gaps-with-the-full-journal-extract).
