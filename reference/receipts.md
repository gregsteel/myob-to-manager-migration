# Source documents (receipts / bills)

| Source | Decision |
|---|---|
| **Purchase bills + attached receipts** | **Extracted** via Playwright into `exports/myob/bills/`, imported as Manager Purchase Invoices, images attached with `attach_purchase_images.py`. See [manager-import.md](manager-import.md). |
| **MYOB In Tray bulk dump** | **Not used** — paid Business API / third-party bulk rejected on cost. In Tray typically holds only a handful of recent items; historical substantiation is on the bills themselves. |

Payroll remains a decommission blocker — see [payroll.md](../../manager-automation/reference/payroll.md).

**`receipt.pdf` is a re-render, not always byte-identical to the original
upload.** Check harvested receipts against MYOB's own
`attachments.documents[].size` before assuming otherwise — a meaningful
share can mismatch, including originals that were actually `.jpg`/`.png`
rather than PDF at all, even though the local copy is always a PDF. PDF
metadata explains why: byte-matching files carry `Creator: Genius Scan` (a
phone scanning app — genuinely the original); mismatching ones carry
`Producer: PDFTron PDFNet` with no scanning-app fingerprint (a normalized
preview, almost certainly rendered server-side by MYOB's own In Tray
viewer, which is what a UI-driven Playwright harvest can actually retrieve
without a paid API). Visual content is presumably preserved either way —
this just means "the same bytes the supplier uploaded" is never on the
table via this harvest method, only "what MYOB's viewer shows you." This
is generally acceptable as substantiation, but confirm that's acceptable
for the business's own recordkeeping requirements rather than assuming it.

---

## Purchase bill harvest

```bash
# Interactive / long-running harvest of bills + PDFs
python3 scripts/myob_playwright/download_bills.py

# Fix specific bill numbers / wrong issue dates
python3 scripts/myob_playwright/refetch_by_number.py

python3 scripts/build_purchase_invoices.py
# … Batch Create in Manager …
# Quit Manager
python3 scripts/attach_purchase_images.py
```

Archive layout: `exports/myob/bills/by_bill/<folder>/bill.json` plus `receipt.pdf` /
images. Manager cannot embed PDFs in Batch Create; attachments go into the SQLite
`Images` table keyed by invoice Key. Format facts:
[specs/001-manager-import.md](../specs/001-manager-import.md).

## Going forward (FY2027+)

Attach receipts on new Manager transactions as you enter them. Historical purchase
substantiation is covered by the Playwright archive + Manager Images.
