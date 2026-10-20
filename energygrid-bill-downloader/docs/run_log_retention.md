# EnergyGrid run-evidence retention (#257)

Hardening for the shipped Claude-free daily run (#226, closed). This keeps clean daily run evidence for 90
complete days, then removes it safely. Every incident or doubtful run is kept and archived privately.
**Source and offline tests only.** Nothing here is installed, scheduled or run on the owner PC until a
separate, owner-approved administrator install (section 6).

| File | Role |
|---|---|
| `runtime/maintenance/energygrid_retention.py` | The tool. Standard library only. Installed as `<EG_HOME>\maintenance\energygrid_retention.py`. |
| `runtime/maintenance/egretention.ps1.template` | The only command a task runs. Checks ACLs, then runs the tool (dry run unless `-Apply`). |
| `task-scheduler/energygrid_retention.task.example.xml` | Inert task shape: disabled, 19:00 SGT, no catch-up, no `-Apply`. |
| `tests/test_retention.py` | Synthetic offline tests (section 7). |

## 1. What it may touch, and nothing else

There is **no path argument**. `EG_HOME` is the folder above the tool's own `maintenance` folder, so the
tool can only see:

- `<EG_HOME>\data\runs\YYYYMMDD-HHMMSS` run folders. These are the only things it ever deletes, and only
  after the checks in sections 2 and 3.
- `<EG_HOME>\data\egdaily.log`. It is moved aside by one rename and is never truncated or rewritten.
- `<EG_HOME>\data\retention\` is the tool's own private folder. It holds the incident archive, the prune
  journal, quarantine and ledger, rotated log segments, reports and the lock file.

It never opens or changes the SQLite state, provenance or notification sidecars, invoice PDFs, named or
source archives, the Drive for desktop sync folder, the outbox or dispatch markers, n8n, credentials,
private evidence or UAT archives, the installed code and config, or Task Scheduler. Other entries in
`data\runs` (anything not named exactly `YYYYMMDD-HHMMSS`) are counted as `skipped` and left alone.

## 2. When a run counts as proven clean

A run folder is pruned only when **all** of the following are true. Anything else is **preserved**.

1. **Path.** It is a direct child of the runs root and a plain directory. It is not a symlink, junction or
   other reparse point. Every ancestor up to the drive root is also a plain directory. It holds exactly
   these five regular files and nothing else: `acquire.json`, `status.json`, `daily.json`,
   `monitor-report.json` and `monitor-post.json`. Each file is at most 1 MiB, has one hard link and is not
   a reparse point.
2. **Strict JSON.** Each file is UTF-8 with no BOM. It has no duplicate keys and no NaN or Infinity.
3. **daily.json** (`energygrid.native_daily.run.v1`):
   - `monitor_report` is SUBMIT and `withheld_reasons` is empty.
   - Acquire is ACQUIRED and status is DRAINED.
   - `email` has `uncertain` 0 and `held` 0, and the probe is absent or READY.
   - Every step is clean: drive-seal SEALED, drive-record VERIFIED, drive-adopt ADOPTED or notify SENT,
     with no `support_ref`. The run ends on `next` IDLE.
   - The `sent` count equals the number of notify steps.
4. **acquire.json** (Core result):
   - ACQUIRED with exit 0, and the run id matches the daily record.
   - Nothing was recovered (`recovered_unknown` and `recovered_notifications` are both 0).
   - Both `EB_BILL` and `TENANT_BILL` are present, each with a good acquisition status and no
     `support_ref`.
   - Every named-archive entry is PRESENT.
5. **status.json** (Core result):
   - DRAINED with exit 0.
   - Both streams are DRAINED with no `support_ref`.
   - Every invoice's Drive upload is VERIFIED and its named archive is PRESENT.
   - Notification counts show 0 for PENDING, SEALED, UNKNOWN and HOLD, and every item is SENT.
6. **Monitor.**
   - `monitor-post.json` is exactly `{"outcome": "EG_MONITOR_LOCAL_REPORT_POST_ACCEPTED"}`.
   - `monitor-report.json` carries the same run id, and both streams are `OK` with no references.
   - A daily SUBMIT without these files is an **incident** (the reporter failed). Exit 0 alone is never
     enough.
7. **Time agrees three ways.**
   - The folder name is the SGT wall clock at the start. `daily.started_at` (UTC) must be within 2
     minutes of it.
   - `finished_at` falls within the task's 2-hour limit.
   - The monitor observed the run after it finished.
   - Every file's write time is within 8 hours of the start.

   A back-dated or copied folder therefore fails. Age never comes from file times alone.
8. **Age.**
   - `complete_days` is the number of whole SGT calendar days strictly between the run's day and today.
   - A run is eligible only when `complete_days > 90`.
   - So 89 and 90 are kept and 91 is eligible. The SGT date is used, not UTC: 00:30 SGT is already the
     next day.
9. **No same-day incident.** If any run on the same SGT day was preserved, the clean run of that day is
   kept too. An `UNSAFE` entry (a junction, link, reparse point or file posing as a run) still counts: its
   SGT day is taken from its exact `YYYYMMDD-HHMMSS` name alone, without following or reading it. An
   impossible date in the name gives no day.

Preserved kinds:

| Kind | Meaning |
|---|---|
| `INCIDENT` | The evidence shows a failure, a pending run or uncertainty. |
| `INCOMPLETE` | Evidence is missing, malformed or contradictory. |
| `UNSAFE` | Links, reparse points or unexpected types. These are never followed or archived. |
| `FUTURE` | The run is dated ahead of the clock. |
| `SETTLING` | The run is younger than 24 hours. It is never read for action. |

Preserved runs stay **indefinitely** until a separate owner decision.

## 3. What `--apply` does, in order

1. **Gates, before any change.**
   - `data\retention` must already exist (`EG_RET_RETENTION_MISSING` otherwise). The tool never creates
     it; the protected install does (section 6). Before the gates the tool creates only its lock file and
     its own empty subfolders inside it.
   - The tool takes an OS byte-range lock, so only one instance runs. A second instance gets
     `EG_RET_LOCK_HELD`.
   - It refuses outside 15:00 to 23:00 SGT (`EG_RET_OUTSIDE_WINDOW`).
   - It **holds every effect**, including the recovery in step 2, if any of these is true:
     - an entry named like a run is younger than 4 hours (`EG_RET_WRITER_MAY_BE_ACTIVE`: the 11:30 task
       or a manual run may still be writing). This reads the names only, so a young junction or file
       posing as a run fences too;
     - an entry named like a run is dated in the future (`EG_RET_FUTURE_RUN_PRESENT`);
     - the runs root cannot be listed (`EG_RET_RUNS_UNREADABLE`);
     - an interrupted operation cannot be settled.
   - While the writer fence holds, an interrupted prune is left exactly as it is
     (`EG_RET_RECOVERY_DEFERRED`; a dry run reports `EG_RET_RECOVERY_PENDING`). The first `--apply`
     whose gates are clear settles it.
   - The window and the writer fence are checked again immediately before each prune and before the log
     rotation.
2. **Settle interrupted work** (only when every gate is clear).
   - Its own leftover archive staging and compaction temp files are removed. The originals were never
     touched.
   - Prune journals are completed, but only where the quarantined bytes still match the journal.
   - A mismatch, or a quarantine folder with no journal, is left exactly as found and holds the run.
3. **Archive every incident first.**
   - INCIDENT and INCOMPLETE runs are **copied** to `data\retention\incident-archive\<run>\`. Only plain
     files are copied: up to 16 files of at most 1 MiB each, with safe names.
   - A `manifest.json` records the size and SHA-256 of each file. The tool writes into a staging folder,
     re-reads every copy, checks that the source has not changed, publishes with one rename and reads the
     result back (`verify_archive`).
   - The original run is never altered.
   - An existing archive is re-verified on every run.
   - Any archive failure holds every prune and every log change for that run of the tool.
4. **Prune** each eligible run:
   1. Re-classify it immediately before acting. It must still be clean and its hashes unchanged.
   2. Write a journal with the file hashes.
   3. Rename the folder out of `data\runs` into quarantine in **one step**. Windows refuses this while any
      file is open, and the run then stays in place (`EG_RET_RUN_IN_USE`).
   4. Re-verify every hash. On a mismatch, rename the folder back.
   5. Delete the five files and the folder.
   6. Write a permanent ledger record of names and hashes, with no content, in `data\retention\pruned\`.

   **A failed or doubtful prune holds everything after it** in that execution: no further prune, no log
   rotation and no compaction. A run that changed between classification and its re-proof holds the same
   way (`EG_RET_RUN_CHANGED_BEFORE_PRUNE`). The failure's own reference is reported with one of:

   | Hold | State left | Next `--apply` |
   |---|---|---|
   | `EG_RET_PRUNE_HOLD_NOTHING_CHANGED` | The run is intact in `data\runs`, with no journal or quarantine (for example the rename was refused while a file was open). | Re-judges and retries it. |
   | `EG_RET_PRUNE_HOLD_RESUMABLE` | A journal, and either the run still in place or a quarantine whose remaining files all match the journal. | Settles it (step 2) once the gates are clear. |
   | `EG_RET_PRUNE_HOLD_OPERATOR` | Anything else: a substituted folder, changed quarantined bytes, a missing or invalid journal, or the run in both places. | Holds again. Nothing is deleted or restored; an operator decides. |

   A failed prune is never counted as pruned and gets no ledger record until it is settled.
5. **Summary log.**
   - When the live log's first line is at least 31 complete days old, or the log reaches 1 MiB, it is
     **renamed** into `data\retention\egdaily-log\`.
   - The daily runner opens the log with PowerShell `Add-Content`, which does not share delete access.
     While the log is held open the rename fails (`LOG_IN_USE`) and nothing changes. The runner's next
     write simply creates a new file. The live log is never truncated or rewritten.
   - Only rotated segments are compacted. A compacted segment is written to a temp file, read back and
     swapped in with one atomic replace.
   - A line is dropped only if it is a complete clean line older than 90 complete days and its day has no
     abnormal line and no preserved run. A clean line is exactly
     `exit=0 monitor_report=SUBMIT submitted=True withheld=`.
   - Abnormal and unparseable lines, and every line of an incident day, are kept.
   - An unreadable log or segment stops all compaction.
6. **Independent after-check.** Every run folder that was not pruned is re-hashed. Any difference is
   reported as `EG_RET_NON_TARGET_CHANGED`.

A **dry run** (the default) does the classification and writes only a report. It shows `WOULD_ARCHIVE`,
`WOULD_PRUNE`, `WOULD_ROTATE` and the lines it would drop. It archives, prunes, rotates and compacts
nothing.

## 4. Output and reports

stdout carries one JSON line (`energygrid.retention.summary.v1`) with:

- mode, outcome and exit code;
- these counts: `considered`, `eligible`, `pruned`, `retained`, `protected`, `archived`,
  `archive_verified`, `skipped`, `errors`, `resumed`, `log_rotated`, `log_segments_compacted` and
  `log_lines_dropped`;
- sorted `EG_RET_*` references;
- a report id.

The line carries no run names, file names, bill numbers, addresses, paths or tokens.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Completed. |
| 20 | Held, or completed with errors. Read the refs and the report. |
| 64 | Refused: bad argument, tree refused, `data\retention` missing, lock held, outside the window or ACL refused. |

The private report `data\retention\reports\<stamp>-<mode>-<id>.json` records per run the kind, reason,
complete days, action and file hashes. It also holds the log plan and the before and after inventory
digests. It names run folders, so it stays in the private folder. **Never paste it into GitHub.** Public
evidence is the summary line only.

## 5. Interruption and recovery

| Interrupted at | Next `--apply` |
|---|---|
| After journal, before rename | The run is still in place. The journal is discarded and the run is re-judged. |
| After rename or mid-delete | The remaining quarantined files are re-hashed against the journal, then deleted and recorded. Not while the writer fence holds: then nothing is touched (`EG_RET_RECOVERY_DEFERRED`). |
| A prune failed without a crash | Everything after it was held in that run. What it left is settled as above, or held for the operator (`EG_RET_PRUNE_HOLD_*`, section 3 step 4). |
| Quarantined bytes changed | Held with `EG_RET_PRUNE_DIGEST_MISMATCH`. Nothing is deleted and nothing else is pruned. An operator decides. |
| Archive staging | Staging is removed and the archive is redone and verified before any prune. |
| Log compaction | The temp file is removed. The old segment was whole the whole time, and it is compacted again. |

## 6. Proposed protected installation (NOT authorised by the source PR)

Each step needs its own owner grant naming the target and the effect.

**Preconditions:**

- Source G2 PASS and an owner merge.
- A read-only reconciliation of the 19 Oct first-run timestamps. Task Scheduler reported "fired 11:30:30",
  but the run folder is `113001` and the monitor reported at 11:30:03 SGT. Read the
  `Microsoft-Windows-TaskScheduler/Operational` events 100, 200, 201 and 102 for the daily task, and its
  `LastRunTime`. Compare them with the run folder name and `daily.started_at`, and note the timezone in
  use. The retention rules depend on the folder name being SGT, so confirm that before any destructive
  step.

**Steps:**

1. **Copy (administrator).**
   - Copy `energygrid_retention.py` to `<EG_HOME>\maintenance\`.
   - Render `egretention.ps1.template` to `<EG_HOME>\maintenance\egretention.ps1`, replacing only the
     EG_HOME placeholder.
   - Do not edit the daily runner, the app slot, the config or the monitor.
2. **Lock (administrator).**
   - `maintenance`, both files and every executed item must be owned by Administrators or SYSTEM. Only
     SYSTEM, Administrators and TrustedInstaller may hold write, delete, WRITE_DAC or WRITE_OWNER.
     The executed items are EG_HOME, the venv, `pyvenv.cfg`, `python.exe` and the base CPython from
     #226 CB1.
   - Create `data\retention` **before the first run**. The wrapper refuses with
     `EG_RET_RETENTION_MISSING` if it is absent, and the tool never creates it. Break inheritance and grant
     it only to SYSTEM, Administrators and the task principal (Modify), all **inheritable to subfolders
     and files**, for example:

     ```bat
     icacls "<EG_HOME>\data\retention" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F "<task principal>:(OI)(CI)M"
     ```

     Users, Everyone and Authenticated Users get no read, not even inherit-only. The tool creates its
     subfolders (`incident-archive`, `prune-quarantine`, `prune-journal`, `pruned`, `egdaily-log`,
     `reports`) and files inside it, so they inherit exactly these entries. Do not set explicit ACLs on
     them.
   - Read back the root and, after the first dry run, one report file and one subfolder with `icacls`.
     The root must show no inherited entries, and nothing may show a `BUILTIN\Users`, `Everyone` or
     `NT AUTHORITY\Authenticated Users` entry. `/grant:r` replaces only the named principals' entries,
     so remove any other explicit entry the folder was created with until only the three grants remain.
     The wrapper refuses otherwise.
3. **Negative ACL proof** as the non-elevated task principal:
   - Writing, renaming or deleting `egretention.ps1`, `energygrid_retention.py` or `maintenance` fails.
   - Changing their ACL or owner fails.
   - A standard second user cannot list `data\retention`.
   - The wrapper itself refuses before Python starts (exit 64) if any of this is wrong:
     - `EG_RET_ACL_NOT_PROTECTED`: an executed item or folder is writable by someone else;
     - `EG_RET_RETENTION_MISSING`: `data\retention` does not exist;
     - `EG_RET_RETENTION_NOT_PLAIN`: it, or one of the tool's subfolders, is a junction or other reparse
       point;
     - `EG_RET_RETENTION_NOT_PRIVATE`: its inheritance is not broken, or it or an existing subfolder
       grants read (effective, inherited or inherit-only) to anyone but SYSTEM, Administrators, the task
       principal or CREATOR OWNER;
     - `EG_RET_ACL_UNREADABLE`: an ACL cannot be read.
4. **Scratch rehearsal on the owner PC.**
   - Copy the installed pair into a throwaway `EG_HOME` holding **copies** of some run folders, never the
     live ones. Run a dry run, then `--apply` inside the window.
   - Expected: no Google, n8n, Gmail, portal or billing effect. Read back the counts and the archive.
5. **Production dry run (manual, not scheduled).**
   - Run `egretention.ps1` with no argument after 14:00 SGT, as the task principal.
   - Read back:
     - the summary line;
     - the private report's classification of every existing run (expect the 19 Oct and later runs to be
       `SETTLING` or `SUCCESS` within retention, and nothing eligible before mid-January 2027);
     - that `data\runs` and `egdaily.log` hashes are unchanged.
6. **Register the task DISABLED.**
   - Use `energygrid_retention.task.example.xml`: 19:00 SGT, no catch-up, PT1H, the same principal as the
     daily task, and no `-Apply`.
   - Read back the task XML. Installing the task deletes nothing.
7. **Enable as a dry run** (separate grant). Review at least one scheduled dry-run report.
8. **Enable `-Apply`** (separate grant).
   - Add `-Apply` to the task action and read it back.
   - The first enabled run returns `retained`, `protected`, `archived`, `pruned`, `skipped` and `errors`.
   - Expect `pruned` 0 until the first clean run passes 90 complete days.

**Uninstall or rollback:**

- Disable or delete the retention task.
- Delete `maintenance\egretention.ps1` and `energygrid_retention.py`.
- `data\retention` holds the only archive copies and the ledger. Keep it.

## 7. Tests (offline, synthetic)

`python -m unittest tests.test_retention` from `energygrid-bill-downloader`. The CI project suite also
runs it. The tests cover:

- **Age:** 89, 90 and 91 complete days; the SGT versus UTC boundary; folder name versus recorded start;
  back-dated file times; same-day incidents; future runs; an unsafe entry (a file, or on Windows a
  junction) keeping the clean run of its SGT day.
- **Evidence:** 33 doubtful-evidence variants, plus separate extra-file and invalid-name tests, each preserved with the expected kind. These include:
  - withheld runs, uncertain, held or probe-failed email, and pending, held or unknown notifications;
  - pending or blocked streams, unverified invoices, failed acquisition, recovered work and named-archive
    conflicts;
  - a failed monitor POST, a missing monitor report or POST (exit 0 alone), a monitor PENDING and run id
    mismatches;
  - truncated JSON, duplicate keys, NaN, a BOM, non-UTF-8 and the wrong schema.
- **Paths:** junction run folders, a substituted runs root or data ancestor, hard links, subfolders,
  symlinked files (needs the symlink privilege; CI has it), a hard-linked log, a run held open, and
  non-run entries.
- **Apply:** prune plus archive with consumer readback; non-target state, sidecars, outbox, PDFs and
  config unchanged; a summary line with no names; repeat-run idempotence; window refusal; writer-active
  hold; the lock; argument refusal; a tampered existing archive; a missing `data\retention` refused with
  nothing created.
- **Gates before recovery:** an interrupted prune with leftover staging and temp files, plus a young, a
  future-dated or a young unsafe entry. Nothing at all changes until the fence clears; then recovery,
  the next prune and the rotation complete. A dry run reports the pending recovery.
- **Prune failure holds:** a failure before the rename, after the rename and mid-delete, a changed
  quarantine, and a run that changed before its re-proof. Each has a second eligible run, a log due for
  rotation and a segment due for compaction. Nothing after the failure changes, the right
  `EG_RET_PRUNE_HOLD_*` is reported, and the next run settles it or keeps holding.
- **Interruptions:** after the journal, after the rename, mid-delete, a tampered quarantine, a quarantine
  with no journal, archive staging, compaction temp files.
- **Log:** dry run, rotation and compaction rules (abnormal lines, incident days, the 90-day boundary,
  BOM, unparseable lines), a writer holding the log open, a fully old segment, an unterminated last line.
- **Templates:**
  - task XML contract;
  - wrapper parse;
  - in-memory ACL verdicts (protected and private, including inherit-only CREATOR OWNER and Users,
    Everyone and Authenticated Users entries);
  - in-memory verdicts for the retention root (inheritance broken, inherit-only broad read) and an
    inheriting subfolder;
  - the rendered wrapper refuses a user-writable install before Python runs;
  - natively, against real folders: a missing, an inheriting, a Users-readable and an unreadable
    retention folder, and a broadly readable subfolder, are each refused before Python runs. A protected
    folder runs, and the subfolders and report the tool creates inherit no Users, Everyone or
    Authenticated Users entry. Each scratch DACL is written whole (SDDL) and printed on failure. The
    unreadable case is skipped, never passed, if the test process can still read the ACL (for example
    with an enabled backup privilege). These tests render the wrapper with the admin-ownership loop
    removed, because a scratch folder cannot be admin-owned; that loop has its own test above.
