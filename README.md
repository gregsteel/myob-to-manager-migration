# myob-to-manager-migration

## What is Manager.io?

[Manager.io](https://www.manager.io/) is free, self-hosted accounting software for Windows, macOS, and Linux (also available as [Cloud Edition](https://www.manager.io/cloud-edition)). It covers sales, purchases, banking, payroll, and the full ledger, with an HTTP API (`/api2`) for automation.

## What this skill does

This project provides a [Claude Code](https://claude.com/claude-code) / 
Cursor **agent skill**: generic toolkit and durable knowledge for migrating 
a company from **MYOB Business Lite** to 
**[Manager.io](https://www.manager.io/)**, preserving the full chart of 
accounts and multi-year transaction history.

This is not a hands-off migration tool and can never be complete.  It has
supported exactly ONE migration and was developed through that effort
to capture learnings.  If you use it to help your migration (including
forking and improving it) then let me know and I'll update this.

This is not a standalone program you run; it's a knowledge package an
agent reads. Start at [`SKILL.md`](SKILL.md) — it's the entry point and
links to everything else here.

**Migration-only.** Everything about Manager itself — API mechanics,
COA/tax-code/bank-account behavior, invoice linking, custom themes, safe
scripted-write practices — lives in the dependencies. Someone who has only 
ever used Manager (no MYOB involved) would never need anything in this 
skill.

## Using it in a project

Symlink this repo into a host project's skill directories rather than
copying it in:

```
project/.claude/skills/myob-to-manager-migration -> /path/to/myob-to-manager-migration
project/.cursor/skills/myob-to-manager-migration -> ../../.claude/skills/myob-to-manager-migration
```

It expects a sibling (`manager-automation` skill) from **[`gregsteel/manager-automation`](https://github.com/gregsteel/manager-automation)** 
(referenced throughout as `../manager-automation`) for everything Manager-side.

Scripts in [`scripts/`](scripts/) are the **single canonical copy** —
a host project that wants to run one should symlink it in
(`project/scripts/<name>.py -> ../.claude/skills/myob-to-manager-migration/scripts/<name>.py`),
never hand-copy it. Scripts locate the project root by searching upward
from the current working directory, so they must be invoked with the
project root as the working directory.

## Structure

| Path | Contents |
|---|---|
| [`SKILL.md`](SKILL.md) | Golden rules, workflow, when to use which tool |
| [`reference/runbook.md`](reference/runbook.md) | Export, validate, build, reconcile — start here for a real run |
| [`reference/manager-import.md`](reference/manager-import.md) | Loading into Manager: COA, bank, purchase invoices, API apply |
| [`reference/receipts.md`](reference/receipts.md) | Bill/receipt harvest + image attach |
| [`reference/live-trial-balance.md`](reference/live-trial-balance.md) | Reconciling a live Manager instance against MYOB export data |
| [`specs/`](specs/README.md) | MYOB export/harvest format facts (protobuf COA map, BFF, journal-as-dictionary) |
| [`scripts/`](scripts/) | `lib_xlsx.py`, `build_journals.py`, `audit_account_vs_myob.py`, `audit_ar_receipts_vs_myob.py`, `audit_post_migration_date.py` |

Project-specific tools that embed one company's own account codes or
bespoke matching logic (e.g. `reconcile_manager_to_myob.py`,
`build_director_clearing_journals.py`) stay in that project, not here —
their *technique* is documented in `reference/` for reuse, not the
literal script.

## Workflow (high level)

```
1. MYOB export → exports/myob/; harvest bank + bills (Playwright → archive/)
2. validate_categories → build COA / opening / journals / contacts
3. reconcile_trial_balance (+ reconcile_pl) — exact match required
4. manager-import: seed COA → bank + start balance → PIs + images → bank feed
5. Bank categorization from harvest + journals / SKILL.md §6a → apply_manager_api.py
6. Spot-check with the manager CLI; update config/pending_pastes.tsv
7. reconcile_manager_to_myob.py (--live-api for a quick check); triage project docs/MIGRATION_DIFFS.md
```

## Related tools and skills

- **[`manager-automation`](https://github.com/gregsteel/manager-automation)**
  — durable, reusable knowledge and tools for automating **Manager.io
  itself** — its REST API's real behavior, quirks, and safe-operation practices
- **[`mprokopov/manager-ai-skills`](https://github.com/mprokopov/manager-ai-skills)**
  (`manager-accounting` skill/CLI) — quick reads and spot-checks (aged
  receivables, GUID lookups). Not a dependency of this repo, but commonly
  used alongside it.

## Scope

Durable and reusable — applies to any MYOB→Manager migration with a
similar chart of accounts, invoice structure, and bank feeds, not just
one company. If you're working on a different migration, fork this skill
and adapt it to your own instance. Once a migration is complete and MYOB
is decommissioned, this skill's day-to-day relevance ends too — see
`manager-automation` for ongoing Manager work, and this skill again only
for forensic review of the harvested MYOB data afterward.

Instance-only content (one company's open reconciliation gaps, payroll
status, config) does **not** belong here — it lives in that company's own
project `docs/` and `config/`.


## License

[MIT](LICENSE)
