# Scheduled native host: least-privilege policy templates (P-5)

Status: **templates and an installation procedure only**. Nothing here is installed by this repository, and a template is not a qualified guard. Until the qualification in §3 passes on the real host and Web accepts it, production stays on HOLD for **M-5 (managed native guard unproven)**.

## 1. Files

| File | Purpose |
|---|---|
| `egnative.cmd.template` | The only command the scheduled host runs. It refuses every subcommand outside the loop list with exit 64 **before Python starts**. That covers the operator commands `backfill-*`, `init-uat`, `migrate-hold-recovery`, `hold-release`, `drive-readback` and `make-synthetic-fixture`. The match is exact and case-sensitive. |
| `settings.template.json` | Claude Code permission policy for a dedicated host task folder. `defaultMode: dontAsk`, so anything not listed is refused instead of prompting. It allows the exact loop subcommands, writing the run folder, Drive `search_files` and `get_file_metadata`, and Gmail `create_draft`, `get_draft` and `send_message`. It explicitly denies the operator subcommands, Bash, the Drive write, download and share tools, the other Gmail tools and the web tools. |
| `host_prompt.template.md` | The scheduled task prompt. It carries the P-1 exact Drive poll and the handling for `RETRY_LATER` and `DRIVE_RECONCILE_PENDING`. |

Placeholders, which are never committed with real values:
- `{{EG_HOME}}`: the host's EnergyGrid folder.
- `{{PINNED_COMMIT}}`: the installed copy.
- `{{LAUNCHER}}`: the absolute launcher path.
- `{{RUNS_GLOB}}`: the run-evidence folder in permission-rule form.
- `{{DRIVE_MCP}}` and `{{GMAIL_MCP}}`: the host's connector server ids.

## 2. Defence in depth

1. **Launcher:** the subcommand allow-list is enforced by the launcher itself, independent of any model or permission setting. This is tested in `tests/test_native_host_policy.py`.
2. **Permission policy:** decides which tools the host model may call at all. With `dontAsk`, an unlisted command spelling, such as `cmd /c ...`, `& '...'`, a direct `python -m ...`, or a compound command, is refused rather than run or prompted. Whether the installed Claude Code build really enforces each of these is **unproven** until §3 is done on the host.
3. **Core:** every effect is still sealed, ceiling-bounded, verified and recorded by the Core. The host cannot widen a recipient or overwrite a Drive file through the CLI.

## 3. Installation qualification (needs a separate Owner/Web grant; not done here)

1. Disable the scheduled task. Install the new pinned copy beside the current one and verify every package blob against the reviewed commit.
2. Render the three templates into a **dedicated host task folder**. Lock the launcher, the settings file and the prompt admin-write only, in the same way as the `app` and `runtime` folders.
3. **Positive control:** run one idle loop (no new bill) through the scheduled task. Expected: no prompt, no error, no Drive or Gmail write.
4. **Negative controls**, run as the host user. Each must be refused without a prompt and without side effects:
   - the launcher with each operator subcommand, which must exit 64;
   - `cmd /c` and `&`-call spellings of the launcher;
   - a direct `python -m energygrid_bill_downloader.native_cli`;
   - a compound command (`<launcher> next; <other command>`);
   - Drive `create_file` and `download_file_content`;
   - Gmail `forward`.
5. Record the results in private custody, then re-enable the task.
6. **Rollback:** disable the task, then restore the previous launcher, settings and prompt files.

If any negative control is not refused, M-5 stays a production HOLD. Report the exact gap; never weaken the Core instead.
