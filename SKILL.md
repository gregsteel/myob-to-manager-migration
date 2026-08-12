---
name: myob-to-manager-migration
description: >-
  Migrates MYOB Business Lite to Manager.io: export reshape, API apply of
  creates/updates (Batch TSV→form), COA SQLite seed, bank categorization,
  journal-as-dictionary gap fixes, and cent-exact reconciliation. Use when
  working on MYOB exports, Manager import, trial-balance gates, purchase/sales
  invoices, bank matching, migration diffs, or post-load alignment. Once a
  migration is complete and MYOB is decommissioned, this skill is no longer
  needed day-to-day — see manager-automation for ongoing Manager work.
---

# MYOB → Manager.io Migration

Generic toolkit for migrating a company from **MYOB Business Lite** to
**Manager.io**, preserving full chart of accounts and multi-year history.
**Durable knowledge lives in this skill.** The host project holds extracts
(`exports/`, `archive/`, `out/`) and **instance-only** notes under `docs/`
(open gaps, payroll status for that company).

This skill is **migration-only**. Everything about Manager itself —
its API mechanics, COA/tax-code/bank-account behavior, invoice linking,
custom themes, safety practices for scripted writes — lives in the
**`manager-automation`** skill, which this one depends on. Someone who has
only ever used Manager (no MYOB involved) would never need anything in
this skill; once a migration is done and MYOB is decommissioned, this
skill's day-to-day relevance ends too (it remains useful only for
forensic review of the harvested MYOB data afterward).

MYOB Business Lite is an AU/NZ product, so this skill's export/import
mechanics apply regardless of your own country's tax rules. Where a fact
is specific to a *country's* tax/compliance law (GST, ABN, BAS, PAYGW,
FBT, Superannuation, STP) rather than to MYOB or Manager themselves, it
lives in `manager-automation`'s country-specific file instead — see
[../manager-automation/reference/tax-au.md](../manager-automation/reference/tax-au.md)
for Australia.

This skill's knowledge is **durable and reusable** — it applies to any
MYOB→Manager migration with a similar chart of accounts, invoice structure,
and bank feeds, not just one specific company. If you are working on a
different migration, **fork this skill** and adapt it to your own instance.

The skill makes lots of backups and logs, and is designed to be **safe to
run repeatedly and idempotent**: re-running a step should not create
duplicates or corrupt data, provided each script's own idempotency check
actually works. Always check the logs and reconcile with MYOB after each
step. That said, protecting your data is your responsibility — take a
backup before any batch of writes that isn't purely additive (see
`manager-automation`'s Golden Rules).

## Dependency: manager-automation (do not duplicate)

Everything Manager-side — API client, safety practices, COA/tax-code/
bank-account mechanics, invoice linking, custom themes, computing a live
Trial Balance — lives in the sibling **`manager-automation`** skill.
Typical local path: `.cursor/skills/manager-automation` (symlinked into
`.claude/skills/` the same way). Read its `SKILL.md` first if you haven't.

**Writes (creates/updates):** `apply_manager_api.py`, referenced below and
elsewhere in this skill's older docs, **does not exist in any project using
this skill** — confirmed by a full git-history search 2026-08-12; it was
removed in an early cleanup pass and never rebuilt. Treat every mention of
it in this skill as historical/aspirational, not a real tool to reach for.
The actual live pattern is **direct `lib_manager_api.ManagerAPI` calls**
(`post_form`/`get_form`/`put_form`) written per-script — see
`scripts/fix_general_journal_gaps.py`, `scripts/build_director_clearing_journals.py`,
and, for the fullest worked example (dedup + create + link + verify, all
built on this same pattern), [reference/delta-migration.md](reference/delta-migration.md)'s
`myob_delta/` scripts. Do not paste Batch Create/Update in the UI unless
direct API calls fail for a specific reason worth documenting.

## Read first (progressive disclosure — all inside this skill)

| Doc | When |
|---|---|
| [reference/runbook.md](reference/runbook.md) | Export, validate, build, reconcile — start here |
| [reference/manager-import.md](reference/manager-import.md) | Load into Manager (COA, bank, PIs, API apply) |
| [reference/receipts.md](reference/receipts.md) | Bill/receipt harvest + image attach |
| [reference/live-trial-balance.md](reference/live-trial-balance.md) | Reconciling a live Manager instance against MYOB export data |
| [reference/delta-migration.md](reference/delta-migration.md) | Side-by-side operation: on-demand delta harvest+apply (Bills/Invoices/Journals) once MYOB is no longer the sole system of record, MYOB session-fragility handling, the Journal-entries report export sequence |
| [specs/](specs/README.md) | MYOB export/harvest format facts (protobuf COA map, BFF, journal-as-dictionary) |

**Instance-only** (this company's open gaps / choices): project `docs/`
(e.g. `docs/MIGRATION_DIFFS.md`, `docs/PAYROLL.md`). Do not put generic learnings there.

Project config often includes: `scripts/config.json`,
`config/builtin_account_map.tsv`, `config/pending_pastes.tsv`.

## When to use which tool

| Need | Prefer |
|---|---|
| Create/update receipts, payments, journals, invoices from builder output | **`scripts/apply_manager_api.py`** (GET-merge-PUT / POST) |
| Spot-check live AR/AP, COA, search by name | `manager` CLI (manager-automation dependency) |
| Post-load balance gate vs MYOB TB (final signoff) | Manager **UI report export** (Trial Balance PDF) → `reconcile_manager_to_myob.py`. |
| Fast iteration without re-exporting the PDF each time | `reconcile_manager_to_myob.py --live-api` — computes Manager's side from live Journal/Payment/Receipt/Invoice data instead. Accounts touched by invoice tax lines are excluded (see [reference/live-trial-balance.md](reference/live-trial-balance.md)) — the PDF export is still the real signoff gate. |
| Drill into *why* one specific account mismatches (once the gate names it) | `scripts/audit_account_vs_myob.py <code>` — every Manager `/transactions` line for that code alongside every matching MYOB `journal_entries_FY*.xlsx` line, date-sorted with running balances, plus a day-level net-diff ranking (see [reference/live-trial-balance.md](reference/live-trial-balance.md) "Line-by-line auditing"). This is the generalized, single-account version of the GST-specific technique in `audit_gst_accounts.py` — use the GST script instead only for a code inside `MERGED_CODES` (pooled matching needed), this one otherwise. The workhorse tool for closing out reconciliation-gate mismatches one account at a time. |
| COA seed | **REST API** (`seed_chart_of_accounts.py`) — see [reference/manager-import.md](reference/manager-import.md) §1 |
| Receipt PDF attach | **SQLite** (`attach_purchase_images.py`) — Quit Manager first |
| Bank lines + MYOB categorization | Playwright harvest (`download_bank.py` → `exports/myob/bank/`); journals for pre-feed / dictionary |
| Re-export the Trial Balance PDF without asking the user to do it by hand | `scripts/manager_playwright/export_trial_balance.py` (manager-automation) — Playwright against **Manager's own web UI**, not MYOB's. |
| Clipboard Batch Create/Update | **Legacy fallback only** |

## Golden rules

1. **Every phase ends with a MYOB reconciliation.** No exact Trial Balance match → do not proceed.
2. **Do not apply `journal_dictionary.tsv` wholesale.** It is the dictionary for bank categorization and §6a gap fixes; apply only targeted extracts via API.
3. **Money is integer cents** end to end. Dates are **day-first** (`DD/MM/YYYY`), matching MYOB's own export convention.
4. **Sign off on a real Manager UI report export**, not a from-API or from-DB reconstruction. `reconcile_manager_to_myob.py --live-api` is fine for checking a batch of writes before spending an export cycle on it, but it can't see the tax split inside ordinary invoice lines (see [reference/live-trial-balance.md](reference/live-trial-balance.md)) — always confirm the final gate against a fresh PDF export, always pinned to **MYOB's own TB snapshot date** via `--to-date` (the form defaults To to today, silently exporting a cumulative-to-today report and manufacturing false mismatches for every Manager-only posting made after MYOB's snapshot).
5. Python **stdlib only** — no pandas/openpyxl.
6. **A backdated MYOB General Journal, or an ordinary Bill, can be entirely absent from every harvested `journal_entries_FY*.xlsx` file, even for the fiscal year matching its own transaction date, with no export-timestamp explanation required.** The General-Journal case is export *staleness*: a journal entered (and backdated) after the relevant export was generated simply isn't in it — compare the `Generated <date>` line each export file prints against each other (they can legitimately differ if the business is still live in MYOB mid-migration), and check MYOB's own "Find transactions" screen filtered to `Transaction type = General journal` directly. But a real, unremarkable Bill can also simply be missing from the GL export with no staleness explanation at all — an export-completeness gap, not a timing one. When a Balance Sheet or P&L gap's source transaction can't be found anywhere in `journal_dictionary.tsv` under *any* account code (not just the expected one), don't conclude it's unexplained, assume Manager is wrong, or reconstruct it by inference: check the bill/invoice harvest archive directly for a matching document first — if a genuine harvested source document exists, Manager's side is very likely the *more* complete one, and the "gap" is really MYOB's own GL export missing a real transaction it should have had. See [reference/live-trial-balance.md](reference/live-trial-balance.md) for the full worked examples — confirmed there that MYOB Business Lite has no Audit Trail report, so a journal's *actual* creation timestamp generally can't be verified after the fact; treat export-timestamp comparison and the harvest-archive check above as the only available signals, not proof.

7. **Track a "last migration date" and treat anything in Manager dated after it as not-yet-migrated, not as a gap to fill.** During side-by-side operation, Manager should only contain data actually migrated and reconciled up to a known cutoff (project `config/last_migration_date.txt`). A record dated after that boundary got in some other way (stray manual entry, premature sync, testing) — it is not backed by a completed migration+reconcile pass yet, even if it looks like an ordinary transaction. Confirmed 2026-08-06: a Sales Invoice dated after the boundary had no matching Receipt purely because the *invoice itself* was premature, not because a payment was ever missing — a missing-counterpart audit (e.g. `audit_ar_receipts_vs_myob.py`) run without bounding by this date will misdiagnose "this record is premature and should be removed" as "reconstruct the missing side" instead. Run `scripts/audit_post_migration_date.py` (read-only — flags, never deletes) to check; advance the date file only after the next migration+reconcile pass confirms Manager matches MYOB up to the new date.
8. **A numeric-pattern heuristic (e.g. "this bill's payment term is suspiciously long") is a lead to investigate, never a verified finding — confirming it always means checking whether the transaction is genuinely absent from MYOB's own GL export (`journal_dictionary.tsv`) under its *current* date, the same check Golden rule 6 already requires.** Confirmed 2026-08-06: one MYOB bill was directly verified (via its live MYOB edit screen) to have a corrupted Issue Date field — created 2010, really incurred and paid in 2018, ~8-year gap. Generalizing that single confirmed case into a heuristic ("payment term > ~120 days ⇒ same bug") and applying it to 8 more bills without re-running the GL-export check corrected 7 of them *incorrectly* — every one was already present in MYOB's real GL export under its original date with a normal (if unusually long) payment term; nothing was ever missing. All 7 had to be reverted. The tell that should have stopped it sooner: cross-checking the affected account's real, already-filed P&L statements (if available — project `exports/myob/statements/`) against a from-source total computed using each bill's *original* date matched exactly, which only happens when the original date is already correct. **Before changing any transaction's date based on a suspected data-entry error, verify two things independently, not one**: (a) the transaction is absent from the GL export under its current date, and (b) if a real filed P&L/Balance Sheet exists for the affected period, the from-source total including the original date matches it. Either alone is not proof; a long payment term alone is not evidence at all.

See `manager-automation`'s own Golden Rules for everything about safe Manager API writes (GET-merge-PUT, never delete on inference, snapshot before bulk writes, never create a future-dated transaction, etc.) — those apply here unchanged.

## Three dates (do not confuse)

| Date | Meaning |
|---|---|
| **Opening balance date** | Balances only; no detail before this (example: 30 Jun 2015) |
| **Manager Start Date** | Start of retained history (often day after opening) |
| **Go-live / cutover** | Dual-entry begins — **not** Manager Start Date |
| **Last migration date** | The moving cutoff during side-by-side operation — Manager is only confirmed correct up to here (project `config/last_migration_date.txt`); anything dated after it in Manager is unmigrated, not a reconciliation gap. Advances only after a fresh migration+reconcile pass. See Golden rule 7. |

## Hard-won MYOB-migration facts

- **A bank-feed harvest join (matching each transaction's own event ID to the journal export's reference number) supersedes manual bank-categorization heuristics** once bank-feed data is available — see [reference/manager-import.md](reference/manager-import.md) §3.
- MYOB **Categories** = Manager **Accounts**. Export the **Categories list** (filters must be **All**).
- Journal export must be **expanded** (Code + Debit/Credit columns) or it is unusable.
- One PurchaseInvoice per director-clearing journal row (multi-invoice lines truncate).
- **MYOB Trial balance** (Business Lite): no Cash/Accrual toggle; period is a **month** picker → treat as **month-end** (Jun 2026 ⇒ 30/06/2026).
- Not every MYOB `Bank`-typed account is a real bank account — see [reference/manager-import.md](reference/manager-import.md) §1.
- **A MYOB Bill's Issue Date can be genuinely mistyped years into the past** (a real, editable field error, confirmed via MYOB's own bill-edit screen — not a display artifact of anything else). MYOB's own harvested `payment_term.days` field (`InAGivenNumberOfDays`) is an honest record of `due_date − issue_date` at entry time, so an absurd value (hundreds to thousands of days) is a symptom worth investigating — but per Golden rule 8, it is *only* a symptom; confirm against the GL export and, if available, a real filed P&L before concluding the date is actually wrong. MYOB's own UI surfaces a banner on such bills — "Bills dated before your opening balance month will not automatically update account balances" — meaning a bill accidentally dated before MYOB's opening balance month may never flow into the normal ledger at all unless someone manually adds it to the opening balance; that manual step not happening is a plausible reason a real bill ends up genuinely absent from every `journal_entries_FY*.xlsx` export.
- **MYOB “End of Year Adjustment” (`Dr 3-1800` / `Cr 3-1600`, dated 1 July) is an equity-only shuffle, not the accountant’s adjusting journals.** Real EOY take-ups (BAS clearing, book depreciation, FBT instalment → expense, income-tax provision, HP interest) are separate General Journals, usually dated 30 June (FBT often early July because the FBT year ends 31 March). Don’t treat the equity shuffle as evidence those adjusting journals were done — and when rebuilding close in Manager, zeroing real P&L accounts into `3-1800` is a different step from either.
- **A saved MYOB Playwright session can expire within about a minute of a fresh script start**, confirmed repeatedly on one real business (not a one-off flake — happened five times in a row, including once mid-script between two sequential UI actions). Harvesting cannot be a single unattended command as a result: `login` may be needed immediately before *each* harvest run, not once per day/week. See [reference/delta-migration.md](reference/delta-migration.md) for the non-interactive session-validity check and the "do the whole flow in one continuous run" mitigation.
- **MYOB's Journal entries report defaults to collapsed (no account detail) even when its own Customise dialog already lists the right expanded columns** — Customise's Apply button only configures *which* columns would show in expanded view, not whether the report is in that view. The actual toggle is a separate "Expand all" button. Full click-by-click sequence (including that Export reveals an Excel/PDF choice rather than downloading immediately): [reference/delta-migration.md](reference/delta-migration.md).
- **A correction/dedup script that resolves a MYOB number to a live Manager record must iterate per-source-row (number *and* issue_date together), never collapse to a bare-number-keyed dict** — recycled MYOB numbers aren't just a historical-import risk, a naive lookup during a *new* script's own development pointed a correction at a completely unrelated historical record sharing the same bare number. Caught in dry-run, not by the lookup logic itself. See [reference/delta-migration.md](reference/delta-migration.md) for the incident and the belt-and-braces reference-match guard that now guards against it.

See [reference/invoice-linking.md](../manager-automation/reference/invoice-linking.md) (manager-automation) for everything about how Manager applies Payments/Receipts/Journals to invoices — the payment-reference join technique used throughout this skill's AP-linking scripts is documented there.

## Workflow (high level)

```
Task Progress:
- [ ] 1. MYOB export → exports/myob/; harvest bank + bills (Playwright → archive/)
- [ ] 2. validate_categories → build COA / opening / journals / contacts
- [ ] 3. reconcile_trial_balance (+ reconcile_pl) — exact match required
- [ ] 4. manager-import: seed COA → bank + start bal → PIs + images → bank feed
- [ ] 5. Bank categorization from harvest + journals / §6a → direct `lib_manager_api` writes (see "Apply runner" below)
- [ ] 6. Spot-check with manager CLI; update config/pending_pastes.tsv
- [ ] 7. reconcile_manager_to_myob.py (--live-api for a quick check first); triage instance docs/MIGRATION_DIFFS.md
```

Dry run: use `samples/` when `exports/myob/` is empty — see project README.

## Apply runner

There is no generic TSV-apply runner (`apply_manager_api.py` — see the
"Dependency: manager-automation" section above for why not). Writes are
direct `lib_manager_api.ManagerAPI` calls, one script per concern. For the
delta-migration case (side-by-side operation, catching Manager up on what's
new in MYOB), the ready-made pipeline is:

```bash
python3 scripts/myob_delta/filter_delta.py                    # read-only: what's new
python3 scripts/myob_delta/delta_migrate.py                   # dry-run everything
python3 scripts/myob_delta/delta_migrate.py --apply            # real writes, snapshots first
```

See [reference/delta-migration.md](reference/delta-migration.md) for the
full architecture, the MYOB-harvest prerequisite (manual, human-run —
session fragility means it can't be scripted end-to-end), and per-project
config these scripts expect (`config/myob_business_id.txt` etc.).

Client: `../manager-automation/scripts/lib_manager_api.py`. Logs: `out/manager/apply_log_*.tsv`.
Skill-local, canonical scripts: [`scripts/`](scripts/) — `lib_xlsx.py`,
`build_journals.py`, `audit_account_vs_myob.py`, `audit_ar_receipts_vs_myob.py`,
`audit_post_migration_date.py`, and two subdirectories for the delta-migration
pipeline: `myob_playwright/` (`download_bills.py`, `download_invoices.py`,
`download_journals.py`, `manager_index.py`) and `myob_delta/` (`filter_delta.py`,
`apply_bills_invoices.py`, `link_payments.py`, `apply_journals.py`,
`delta_migrate.py`)
— the MYOB-comparison-specific tools that don't belong in
`manager-automation` (which holds the pure-Manager helpers this skill
depends on: `lib_manager_api.py`, `export_trial_balance.py`,
`backup_manager_business.py`, plus its own generic AP/COA audit tools —
`audit_supplier_ap.py`, `cache_ap_ledger.py`, `find_orphaned_account_refs.py`
— which don't touch MYOB at all and so live there instead, even though
they support the same reconciliation work). **A host project reaches these
via a symlink** (`project/scripts/<name>.py -> ../.claude/skills/<skill>/scripts/<name>.py`,
or `project/scripts/<subdir>/<name>.py -> ../../.claude/skills/<skill>/scripts/<subdir>/<name>.py`
for the two subdirectories, mirroring the project-side directory structure
one level deeper),
never a hand-copied duplicate — see manager-automation's SKILL.md "Agent
habits" for why (a duplicate drifts silently). These skill copies locate
the *project's* root by searching upward from the current working directory
or via `Path.cwd()` (never from the file's own location, since it may be
symlinked into any project) — always invoke with the project root as the
working directory. Locating *sibling skill* modules (e.g.
`lib_manager_api.py` in `manager-automation`) is different: those use
`Path(__file__).resolve()`-based relative paths, since that correctly
follows symlinks to each script's real, fixed location within the skills
directory regardless of which project invokes it — see any `myob_delta/`
script's header comment for the exact pattern.

Project-specific tools that embed this company's own account codes, known
transaction IDs, or bespoke matching logic (`fix_general_journal_gaps.py`,
`build_director_clearing_journals.py`, `audit_gst_accounts.py`,
`reconcile_manager_to_myob.py` itself) stay project-only — their
*technique* is documented in `reference/` for reuse, not the literal
script.

## Maintenance

When a migration step teaches something **durable and reusable**, update
the right skill in the same turn (enforced by the project's always-on
rule):

- Something true about **MYOB, or comparing MYOB to Manager** → this skill.
- Something true about **Manager itself**, independent of MYOB → `manager-automation` instead. Check there aren't two copies drifting apart.
- Short non-negotiable → Golden rules / Hard-won facts in the relevant `SKILL.md`
- Long format contract → `specs/` + one link in the table above
- Operational how-to → `reference/` + one link above
- Repeatable helper → skill `scripts/` (whichever skill it belongs to) or project `scripts/`; document invoke line here

Write additions as durable, generic knowledge — the mechanism, the risk, and
the mitigation — not as a narrative of how or when it was discovered on a
particular migration. A future reader with no context on this project
should be able to use every fact here without needing to know its history.

**Do not** put generic learnings in project `docs/`. That folder is for
**this company's** status, open gaps, and choices only.

## Agent habits

- Prefer `apply_manager_api.py` over asking the user to paste Batch files.
- Prefer existing builders under project `scripts/` over ad-hoc one-offs; extend builders when paste/API shape is known.
- When Manager and MYOB disagree after load: check project `docs/MIGRATION_DIFFS.md` and `config/intentional_exceptions.tsv` before inventing fixes.
- For GUID lookup / aged reports: **manager-accounting** CLI; for bulk writes: **lib_manager_api** / apply runner.
- Specs under `specs/` are for recreating scripts; operational steps stay in `reference/`.
- See `manager-automation`'s Agent habits for dashboard/reporting conventions (stay local, don't publish as Artifacts) — they apply here too.
