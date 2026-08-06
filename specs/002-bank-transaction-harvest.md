# Spec 002 — Bank transaction harvest from MYOB (Playwright)

| | |
|---|---|
| **Decision** | Harvest bank lines + categorization from MYOB's web BFF, not the paid API |
| **Ops** | `scripts/myob_playwright/download_bank.py` |
| **Manager formats** | [001-manager-import.md](001-manager-import.md) §6 |

## Why

MYOB stores **how each bank line was categorized**. Harvest that via the same
authenticated `sme-web-bff` session used for bills — do not rely on a manual
bank-register export (Date/Description/Amount only). Journals from the MYOB
**Journal entries** export cover the pre-feed window and act as the categorization
dictionary.

## Endpoint (undocumented BFF)

```
GET {BFF}/banking/load_bank_transactions
    ?transactionType=All&bankAccount=-1&keywords=
    &dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD&period=Custom
    &sortOrder=asc&orderBy=Date&isSuggestedCategoryEnabled=true&offset=N
```

- `BFF = https://production.sme-web-bff.myob.com/{BUSINESS_ID}`
- Bearer auth: capture the app's `authorization` header from any live BFF request
  (same technique as `download_bills.py` / `refetch_by_number.py`).
- **Page size 50**; page via `offset` until `pagination.hasNextPage == false`.

### Entry shape (the useful fields)

| Field | Meaning |
|---|---|
| `date` | ISO datetime |
| `withdrawal` / `deposit` | amount (withdrawal = money out) |
| `description` | raw bank-feed text |
| `allocateOrMatch` | human label ("Matched to Supplier Payment SP000014", "Bank Charges", "Split across accounts", …) |
| `type` | `singleAllocation` \| `splitAllocation` \| `splitMatched` |
| `matchingMethod` | `Logic` \| `Manual` \| `Rule` |
| `bankingRuleId` | set when a banking rule fired |
| `matchedJournals[]` | `eventId` (SP/CP/PY/SF…), `businessEventType`, `description`, `amount`, `isCredit`, `categoryItems:[{accountId, taxCodeId}]` |
| `status`, `hasAttachment`, `transactionId`, `transactionUid` | |

`categoryItems.accountId` resolves via `/account/load_account_list`
(`entries[].id → accountNumber/accountName`). Populated for spend/receive money
(`CashPayment` / `CashReceipt`); bill/invoice matches carry the SP/CP document in
`eventId` instead.

### Supporting endpoints

| Path | Use |
|---|---|
| `/account/load_account_list` | id → code/name/type; also `openingBalanceDate`, opening balances |
| `/bankReconciliation/load_bank_reconciliation?statementDate=…` | reconciliation view, `closingBankStatementBalance` |
| `/bankFeeds/load_bank_feeds` | which feed accounts exist |
| `/banking/load_bank_transaction_unallocated_count` | count of uncategorized |

## Coverage — bank feed start date

Bank feed history often starts mid-era (example: ~March 2016). Windows before the
feed return **zero** entries. Pre-feed bank lines exist only as entered
documents/journals and must come from `journal_dictionary.tsv`.
`download_bank.py` defaults `--date-from` to the feed start.

## Outputs

```
exports/myob/bank/transactions.jsonl   full raw entries (fidelity)
exports/myob/bank/transactions.tsv     flattened + resolved category accounts
exports/myob/bank/accounts.json        accountId -> {code,name,type}
exports/myob/bank/harvest_state.json   window + count + timestamp
```

## How this feeds Manager categorization

```bash
python3 scripts/build_bank_from_harvest.py
# → out/manager/bank_payments_harvest_round3.tsv
# → out/manager/bank_receipts_harvest_round3.tsv
# → out/manager/bank_suspense_remaining_after_harvest.tsv
```

Round 3 clears remaining Manager suspense by joining harvest (date+amount) to
`bank_suspense_remaining.tsv`. Pre-feed months stay on journals. Do not re-import
the bank statement once Payments/Receipts already exist in Manager.

## Robustness notes

- Session expiry: `download_bank.py` reopens headed and waits for login.
- Re-runnable; dedups by `transactionUid`.
