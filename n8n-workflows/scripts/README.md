<!--
Generated from toolkit curated output for AI. Do not edit directly.
Project: n8n.workflow-toolkit
Source: _projects/n8n/workflow-toolkit/curated_output_for_ai/templates/helper-scripts/import-export-sync/README.md
Update the curated output and run sync.
-->
<!--
Curated AI-facing source.
Project: n8n.workflow-toolkit
Review rule: Preserve safety constraints from preserved source. Do not weaken credential, .env, .tmp, .n8n-local, live n8n action, approval, attribution, or local-only rules.
-->

# Import Export Sync Helpers

This folder contains review-required helper templates for n8n workflow import/export sync.

They are meant to be copied into a consumer repository that intentionally owns n8n workflow JSON under `n8n-workflows/`.

Live import/export helper entry points are not run from this toolkit repo during CI. Non-live validation, sanitizer, sync, compare, and prepare logic may be exercised by tests.

## Dedicated bounded member-gateway helper

`import-member-forms-gateway-bounded.ps1` is a reviewed local successor helper,
separate from the generic import/export/sync package above. It is the only
production path for the inactive member gateway source-adapter workflow. It
accepts a typed private binding manifest, captures an immutable cursor,
watermark, target-preimage, repository, and canonical-workflow identity, and
imports no more than that one exact workflow after explicit confirmation.

It must not call the generic importer, generic export-all path, hook surface,
workflow execution, activation, MCP exposure, Forms, AutoCount, or member
endpoints. Operation files belong only under the ignored canonical private
root `.n8n-local/member-gateway-bounded-import/operations/`; the helper rejects
tracked, non-ignored, outside-root, linked, corrupt, incomplete, and extra
material before mutation. It writes canonical UTF-8 no-BOM LF-only JSON and
retains incomplete state for fail-closed reconciliation.

The direct offline regression command is:

```text
python -m unittest tests.test_member_gateway_bounded_import_security -v
```

## Consumer Repo Layout

- Committed n8n workflow export JSON belongs under the repo root `n8n-workflows/`.
- If `n8n-workflows/` exists, include `n8n-workflows/README.md` describing the workflow set, safety notes, and validation command.
- When Toolkit creates or manages `n8n-workflows/`, copy or sync this approved helper folder into `n8n-workflows/scripts/`.
- Do not store committed workflow JSON under `docs/`, `scripts/`, `.n8n/`, `.tmp/`, app folders, or random scratch paths.
- Use stable lowercase hyphenated filenames such as `member-intake-small-batch.json`; reserve `archived/` for intentionally retained old exports.

## Included Helpers

Toolkit helper set `35defa821c2b` (Toolkit 3.146.4, #323; it replaced `a831f7fd41c3` from #316), 20 files stamped in `n8n-helper-set.json`. Every live helper checks the stamp first and refuses, before any Docker call, when a stamped file is missing or edited (`n8n-helper-set.cjs check`):

- `export-n8n-workflows-live.ps1`, `import-n8n-workflows-live.ps1`, `n8n-workflow-sync-menu.ps1`
- `_export-n8n-workflows-live.cmd`, `_import-n8n-workflows-live.cmd`
- `validate-n8n-workflows.cjs`, `sync-n8n-live-exports.cjs`, `prepare-n8n-live-import.cjs`, `should-import-n8n-workflow.cjs`, `compare-n8n-workflow-credentials.cjs`
- `n8n-portable-workflow.cjs`, `n8n-credential-metadata.cjs`, `n8n-workflow-transport.cjs`, `n8n-workflow-operation-report.cjs`, `n8n-workflow-identity.cjs`, `n8n-cli-workflow-json.cjs`, `n8n-risky-nodes.cjs`, `resolve-n8n-docker-target.cjs`, `check-n8n-helper-machine.cjs`
- `n8n-helper-set.cjs` and its stamp `n8n-helper-set.json`

`import-member-forms-gateway-bounded.ps1` and `import-member-welcome-email-bounded.ps1` are repository-local and outside the stamp (the check lists them as INFO and keeps them).

The live import/export PowerShell helpers support explicit Docker target overrides with `-Container`, `-ContainerName`, `-ContainerId`, `-ComposeProject`, and `-ComposeService`, plus the matching environment variables `N8N_CONTAINER_NAME`, `N8N_CONTAINER_ID`, `N8N_COMPOSE_PROJECT`, and `N8N_COMPOSE_SERVICE`. Without an explicit target, they detect running n8n containers from Docker Compose service output, Compose labels, and safe `n8nio/n8n` image fallback. Multiple detected instances require a valid numeric selection for the current run only and are not persisted.

## Wrapper Working Directory

The `.cmd` wrappers invoke their co-located PowerShell scripts with `%~dp0<name>.ps1` and do not change directory themselves. The PowerShell scripts resolve and set their working directory from their script location.

## Import: no confirmation prompt (owner accepted 17 Nov 2026, #316)

This repository now runs Toolkit's import design unchanged. The earlier local deviations (an `I` confirmation gate with `-ConfirmLiveImport`, an isolated dry-run directory, and an `X` export gate with `-ConfirmLiveExport`) are gone; the owner accepted that on 17 Nov 2026 (#316, PR #315).

- A plain import run, including a double-click on `_import-n8n-workflows-live.cmd` (it passes no flags), validates, reads live n8n and then imports every changed or new repository workflow **unpublished, with no prompt** (`import-n8n-workflows-live.ps1:1604-1638`).
- `-RequireConfirmation` is read only by `n8n-workflow-sync-menu.ps1`: started with it, the menu asks before a real import (`:576-581`, prompt at `:625-636`). The menu always asks before a real export. `import-n8n-workflows-live.ps1` refuses `-RequireConfirmation` and `-SkipCredentialBindingRefresh` (since Toolkit 3.146.1), and `-AllowMissingCredentialBindings` and `-CredentialExportDir` (since Toolkit 3.146.3), with `N8N_POLICY_VALIDATION_FAILED` before any Docker call (`:1438-1449`); older sets accepted and ignored them. The menu never passes any of these switches to the import script.
- `-RestartContainerAfterImport` now stops the run with an error (`:1435-1437`); the helpers never restart n8n. Restarting after an import stays an operator action.

## What still protects live n8n

- The stamp check refuses an edited or incomplete helper set before any Docker call (`Assert-HelperSetReady`, `:1455`).
- The workflow validator must pass first, with the repo-root `n8n-workflow-policy.json` (`:1476-1480`).
  - Known limit (low, tracked by Toolkit): inside a `+` chain, a `++`, a `--`, a number such as `1e-3` or `typeof -x` can still hide a literal host from the URL check. None of the 17 exports has a URL in the same value as one of those shapes.
- A published (active) live target blocks **the whole run** before any import: "Unpublish it in n8n first" (early gate `:993-994`, ending the run at `:1505-1518` before the credential-metadata step; preparation checks again at `:1156-1160`, and any blocked workflow ends the run at `:1525-1530`). Since Toolkit 3.146.1 the early gate also counts a single blocker under Windows PowerShell 5.1 (`@(...)` at `:1505`); set `a831f7fd41c3` skipped it there.
- A local target identity recorded in `.n8n-local/n8n-workflow-identities.json` that no longer exists in live n8n blocks the whole run at the early gate, with no same-name fallback (`:978-980`); under Windows PowerShell 5.1 this holds for a single blocker since Toolkit 3.146.1.
- Every import uses `n8n import:workflow --activeState=false` (`:1610`), and each is followed by a postcondition read that the workflow is unpublished (`:1630-1636`). The helpers never publish, execute, test-run or restart.
- Preparation refuses a live target whose nodes carry a credential that the repository export does not declare (`n8n-portable-workflow.cjs:897-914`); this repository's exports are credential-free and it has no `n8n-workflows/toolkit/portable-credentials.json`, so a live workflow with bound credentials is refused rather than stripped; a node that exists only in live n8n has no export counterpart, so it and its credential are dropped by the import (`n8n-portable-workflow.cjs:904`).
- 16 of the 17 exports carry a template top-level `id`. The declaration built from the export carries that id (`prepare-n8n-live-import.cjs:259-263`, `n8n-portable-workflow.cjs:31-36`), so preparation refuses a target whose id differs, including every brand-new workflow, which gets a fresh id (`n8n-portable-workflow.cjs:496-520`; import `:1152`). One refusal ends the whole run.
- Agents still never run a live import, export or sync without explicit current-turn owner approval naming the target and the operation (root `AGENTS.md`).

## What an import can change

An unpublished live workflow that matches a repository export by id (or by unique name), carries no undeclared credential, and differs from the export in any compared field is overwritten. `should-import-n8n-workflow.cjs:56-91` compares the whole workflow (every node parameter, `active`, credentials, webhook ids and settings; only ids, timestamps, versions, tags, `staticData`, `pinData` and cached resource names are ignored), so any value filled in live makes it CHANGED.

**The payload is the repository file, not a merge: values filled in live are replaced by the repository's values, including `REPLACE_WITH_...` placeholders.** `n8n-portable-workflow.cjs:921-1005` clones the export and carries over only the live `webhookId`s (`:992`), the live `settings.errorWorkflow` and `settings.availableInMCP` (`:933`), declared credentials (`:975`) and exact resource paths that a deployment policy marks environment-specific (`:999`, `:740-767`); the canonical-invariant check refuses any other difference (`:1004`, `:831-841`). This repository has no `n8n-workflows/toolkit/deployment-policy.json`, so no live value is kept. An archived name match is planned as a new workflow beside the archived one by default (`-ArchivedByNameMode CreateNew`, `:1166-1171`), which the id check above then refuses for an export that carries an `id`.

## Dry run and credential metadata

`-DryRun` performs no `n8n import:workflow` and prints "Live n8n was not changed." Like a real run it:

- clears and repopulates `.tmp/n8n-live-import` (`Initialize-RunDirectory`, `:1499`);
- once the early target checks pass (`:1505-1518`), stages the credential-metadata helper in the container's `/tmp`, runs `n8n export:credentials --all` into a Toolkit-owned folder there (encrypted, as n8n exports it), copies out only credential id, name and type, then removes the folder and the helper; a failed clean-up blocks the run (`Get-SafeCredentialMetadata`, `:836-902`). n8n's own data is not changed;
- writes a sanitized operation report under `.n8n-local/reports/` (a real import also records workflow identities in `.n8n-local/n8n-workflow-identities.json`, `:1637`).

## Export

`export-n8n-workflows-live.ps1` (and `_export-n8n-workflows-live.cmd`) rewrites tracked workflow JSON with no prompt. Canonical protected values are kept unless `-ReviewedSourceUpdate` is passed (`sync-n8n-live-exports.cjs:1162-1163`). In `AllLive` mode a live workflow with no repository file is named after the folder's own convention (`export-n8n-workflows-live.ps1:845-898, 1121-1141`, since Toolkit 3.146.1): every root file here ends `.workflow.json` and the names use `_` and never `-`, so a new file is `<lower_case_name>.workflow.json`; a taken name gets a short workflow-id suffix. Check the name before committing it. `-DryRun` writes nothing.

## Intended Scoped Writes

In a reviewed consumer repo, these helpers may write:

- `n8n-workflows/*.json`
- `n8n-workflows/README.md`
- `n8n-workflows/scripts/**`
- `.tmp/**`
- `.n8n-local/**`

`.tmp/**` and `.n8n-local/**` must stay ignored and uncommitted. They can hold transient live export/import payloads and local credential-binding metadata.

## Safety Rules

- Do not run live n8n import or export from this toolkit repo.
- Do not run live n8n import/export in CI.
- Do not commit `.tmp/**`, `.n8n-local/**`, live import/export JSON, credentials, credential bindings, private keys, or `.env` files.
- Do not commit workflow JSON containing credentials, secrets, tokens, cookies, webhook secrets, private environment values, or production execution data. Use placeholder environment variable names only and document required variables separately.
- Do not commit `.n8n/` runtime folders, n8n database files, binary backups, or live execution exports.
- Helper scripts must not print secrets and must not default to destructive live actions.
- Review workflow diffs before committing `n8n-workflows/*.json` in a consumer repo.
- Treat these executable files as helper templates, not trusted runtime code.
