# Repository Agent Rules

## Repository Appendix

Repository-local rules. The installed AI Agent Toolkit plugin serves the global agent rules, the n8n safety router and the playbooks at session start, so this file holds only the rules written for this repository and does not repeat Toolkit text.

### n8n workflow export naming

- This repository's established n8n workflow export convention is lowercase snake_case with the suffix `*.workflow.json`.
- Existing Gate workflow filenames are canonical and must not be renamed during unrelated work.
- For this repository, the hyphenated filename guidance in the n8n workflows playbook (installed Toolkit) (for example `member-intake-small-batch.json`) is illustrative only and does not override the established local convention.
- `n8n-workflows/README.md` is the directory-level source of truth for workflow names and their associated runbooks.

### n8n local runtime state

- `.n8n/` and `.n8n-local/` are local n8n runtime state. They may contain databases, credentials, configuration, execution history, or other private operational data.
- They must never be committed, staged for publication, copied into workflow exports, or treated as source-controlled workflow definitions.
- Agents must not inspect or print their contents unless a separate explicit current-turn request safely authorises a bounded local diagnostic.

### n8n skill routing

- Agents should load the host-qualified installed Toolkit skill equivalent to `n8n-safety-router` (for example `ai-agent-toolkit:n8n-safety-router`), plus the smallest relevant official `n8n-skills:*` skill set. Host-specific naming may differ.
- An agent must not stop merely because the unqualified literal path `skills/n8n-safety-router` is unavailable when the equivalent complete host-qualified Toolkit skill has been successfully loaded.
- The agent must still stop when no equivalent complete Toolkit n8n safety rules are available.
- This clarification does not weaken any approval, credential, local-only, import/export, execution, activation, or deployment safety gate.

### Personal-data guard

- This repository's guard is `scripts/pii_guard.py`: `--staged` through the opt-in `.githooks/pre-commit`, and `--range` in the required `pii-guard` CI job, with `config/pii_guard_allowlist.json` and the `pii-guard: allow (<reason>)` line marker.
- CI uses the pull request head's allowlist and does not scan pull request titles or bodies, so check allowlist, guard and test changes and the pull request title and body by hand.

### Live automation docs stay current

- A PR that ships or changes a live automation updates the root README (and AC2_DATA_GUIDE.md when AC2 data changes) in the same PR.

### Temporary Toolkit #342 structural-impact compatibility rule

`TEMPORARY TOOLKIT #342 COMPATIBILITY RULE — REMOVE AFTER PROPAGATED TOOLKIT RULE IS VERIFIED`

**Structural-change impact check:** Keep ordinary narrow tasks narrow. Before renaming, removing, moving, re-signaturing, or structurally replacing an existing symbol, path, command, schema field, allowlist, receipt shape, generated shape, or other established contract, run a targeted repo-wide search for the exact current identity plus relevant source-shape/contract consumers. Classify every material consumer as preserved, intentionally updated/superseded, or outside current authority requiring escalation. After editing, run the affected contract/source-shape tests first, then the task's broader required validation. This is a targeted impact exception, not permission for indiscriminate broad scans.

Remove this temporary repository copy only after Toolkit #342 is merged and this repository verifies that the propagated managed `AGENTS.md` version contains the permanent structural-impact rule.
