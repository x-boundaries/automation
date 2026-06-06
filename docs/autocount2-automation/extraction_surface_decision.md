# AutoCount Extraction Surface Decision

## Status

Decision pack date: 2026-06-06

This document records the decision process for choosing read-only AutoCount 2.0
extraction surfaces after running the SQL Server local probe on the Windows VM.

Current PR limitation: the safe probe summaries from `probe_report.md`,
`candidates.csv`, `permission_risks.csv`, and `role_risks.csv` were not supplied
inside this branch context. No generated probe outputs, raw CSVs, credentials,
connection strings, local configs, or production database names are committed
here. Until safe summary values are pasted into this document, all candidates
remain `Unknown`.

## Decision Labels

Use exactly these labels for each candidate:

- `Selected for Phase 1`: safe summary, permissions, columns, and
  reconciliation evidence are strong enough to try in the read-only extractor.
- `Needs reconciliation`: candidate looks plausible but totals or business
  rules must still be checked against AutoCount UI/report output.
- `Rejected`: candidate is unsafe, lacks required fields, duplicates business
  meaning, or fails reconciliation.
- `Unknown`: safe probe summary is insufficient or not yet reviewed.

## Probe Run Summary

| Field | Safe value |
| --- | --- |
| Probe run date/time | Unknown - safe summary not supplied |
| Probe output folder | Not committed; keep local only |
| Probe mode | Metadata-only expected |
| Sample rows used | None should be committed or pasted here |
| SQL Server version/edition | Unknown - safe summary not supplied |
| Current database label | Unknown - do not add sensitive production names |
| Current login/user | Unknown - safe principal name may be added if non-sensitive |
| Schemas reviewed | Unknown |
| Tables/views visible | Unknown |
| Columns visible | Unknown |

## Permission And Role-Risk Summary

| Check | Result | Decision impact |
| --- | --- | --- |
| Direct risky permissions from `permission_risks.csv` | Unknown | Cannot approve probe login until reviewed |
| Risky database roles from `role_risks.csv` | Unknown | Cannot approve probe login until reviewed |
| `db_owner` membership | Unknown | Blocks read-only use if present |
| `db_datawriter` membership | Unknown | Blocks read-only use if present |
| `db_ddladmin` membership | Unknown | Blocks read-only use if present |
| `db_securityadmin` or `db_accessadmin` membership | Unknown | Blocks read-only use if present |
| Broad schema/database `EXECUTE` | Unknown | Blocks or requires manual review before use |

Current conclusion: the probe login is **not yet approved** for read-only
extraction in this decision pack because the safe permission and role-risk
summaries are not available here. If the safe summaries show no direct write,
schema, security, broad execute, or risky role membership, the login can move to
manual read-only reconciliation testing.

## Candidate Groups

The SQL probe produces heuristic candidate matches. Treat object and column
matches as hints only. Do not treat candidates as verified AutoCount business
surfaces until they reconcile to AutoCount UI/report outputs.

### Item/Product/Stock Master

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Needed for Phase 1 `stock_master` |

### Stock Balance/Status

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Needed for Phase 1 `stock_balance` |

### Stock Movement/Stock Card

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Needed for Phase 1 `stock_movement` |

### Stock Document/Transfer/Adjustment

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Needed for Phase 1 `stock_documents`; cancelled/voided handling must be explicit |

### Sales Invoice/Cash Sale

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Later phase; not required for stock extractor Phase 1 |

### Purchase Order/GRN

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Later phase; useful for movement/receiving reconciliation |

### Debtor/Customer

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Later phase |

### Creditor/Supplier

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Later phase |

### AR/AP

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Later phase |

### GL/Account/Journal

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Later phase; extra care needed around finance reporting semantics |

### Payment/Bank/Cashbook

| Candidate object | Evidence from safe summary | Decision | Notes |
| --- | --- | --- | --- |
| Not supplied | No safe candidate summary in this branch context | `Unknown` | Later phase; sensitive financial data handling required |

## Phase 1 Decision

No SQL object is selected for Phase 1 in this document yet. The next manual step
is to paste or summarise only safe metadata from the probe report:

- candidate object names by group,
- relevant column names,
- `permission_risks.csv` summary,
- `role_risks.csv` summary,
- any non-sensitive SQL Server/database context needed to identify the
  environment.

After that update, move each stock-related candidate to `Selected for Phase 1`,
`Needs reconciliation`, or `Rejected` with a short reason.
