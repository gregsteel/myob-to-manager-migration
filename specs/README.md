# Specs (skill-local)

Reusable MYOB Business Lite → Manager.io format facts and harvest contracts.
Operational how-to: [`../reference/`](../reference/). Instance-only variances for a
particular company belong in that project's `docs/` (e.g. `MIGRATION_DIFFS.md`).

| Spec | Summary |
|---|---|
| [001-manager-import.md](001-manager-import.md) | Manager storage, COA protobuf, **builtin↔MYOB map gate**, GUID encoding, bank categorization, journal-as-dictionary, reconcile via report export |
| [002-bank-transaction-harvest.md](002-bank-transaction-harvest.md) | Free `sme-web-bff` bank feed harvest + Round 3 categorization |
| [003-sales-invoices.md](003-sales-invoices.md) | Sales invoice BFF harvest + Batch Create + receipt→SI linking |

Purchase bill harvest: [`../reference/receipts.md`](../reference/receipts.md).
