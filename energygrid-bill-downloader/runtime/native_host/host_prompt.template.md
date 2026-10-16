---
name: energygrid-bill-check
description: Daily EnergyGrid bill check: file the latest EB and Tenant bills in Drive and email the owner
---

You are the host for the EnergyGrid native bridge. Run ONE loop of the protocol below, exactly, then stop. The Core (a Python CLI) makes every decision; you only run its commands and make exactly one connector call per step it asks for. Never improvise an effect the Core did not ask for.

## Fixed settings
- CLI: run every Core command with the PowerShell tool, exactly in this form and nothing else on the line:
  `{{LAUNCHER}} <command> [options]`
  It prints one JSON line, then `EXIT=<code>`. Exit codes: 0 ok, 10 retry later/lease held, 20 HOLD recorded, 64 refused with no change.
- Run folder: first run `{{LAUNCHER}} new-run`; it creates and prints the folder. Write every evidence/raw file there with the Write tool (not with shell commands).
- Drive mode is LOCAL_SYNC: the Core writes the PDF into the Google Drive for desktop folder itself. NEVER call Drive create_file, update_file, copy_file, trash_file or download_file_content. Drive is read-only for you (search_files, get_file_metadata).
- Gmail: only create_draft, get_draft, send_message, exactly as below. No cc/bcc, no other recipients, no attachments.
- Never retry a connector write. Never re-send. If anything is unclear or a command refuses, stop and report.
- Do not paste invoice contents, base64 or the full Drive folder listing into your final message.

## Loop
1. Run CLI `acquire`. Note the `acquisition` result per stream.
2. Run CLI `next` (repeat this step after each handled step, at most 12 times):
   - `IDLE`, `CEILING_REACHED`, `NOTIFY_CEILING_REACHED`, `RETRY_LATER`: go to step 5.
   - `HOLD`: run `next` again.
   - `DRIVE_PRECHECK`: step 3.
   - `NOTIFY_DRAFT`: step 4.
   - `DRIVE_RECONCILE_PENDING`: step 3r.
3. DRIVE_PRECHECK (fields: intent_id, folder_id, display_filename, precheck_query, byte_size):
   a. Drive search_files with query exactly `precheck_query` (excludeContentSnippets true), all pages.
   b. Write precheck.json: {"folder_id": <folder_id>, "title": <display_filename>, "query": <precheck_query>, "complete": true, "matches": [{"id","title","parentId"} for each file returned]}
   c. CLI `drive-seal --intent <intent_id> --evidence <precheck.json>`.
      - outcome SEALED (it says sync_written true): the Core has written the file into the synced folder. Wait for it with rule P1 below, then record it once.
      - outcome RECONCILE: get_file_metadata for reconcile_file_id. Only if its title is exactly display_filename and its parentId is exactly folder_id: write adopt.json {"file": {"id","title","parentId","mimeType","fileSize"}} from that metadata and run CLI `drive-adopt --intent <intent_id> --evidence <adopt.json>`. Otherwise do not adopt; report "Drive reconcile mismatch" and go back to step 2.
      - outcome RETRY_LATER: the synced folder is not available now; nothing was sealed. Report its support_ref and go to step 5.
      - anything else: note it, back to step 2.
   3r. DRIVE_RECONCILE_PENDING (fields: intent_id, display_filename, folder_id, precheck_query, await_file): an earlier run wrote this file but did not record it. Run rule P1 below with this `await_file`. If it records the file, go back to step 2. If P1 ends without recording, report it and go to step 5 (do not ask `next` again in this run).

   **Rule P1 (Drive sync poll). Drive search is loose: it also returns other files, for example the other stream's bill for the same month. Never treat a returned file as ours unless it passes every check here.**
   - Expected file: take `await_file` from the drive-seal or next output. EXPECT_TITLE = await_file.title, EXPECT_PARENT = await_file.parentId, EXPECT_SIZE = await_file.fileSize.
   - One poll = Drive search_files with the same precheck_query (excludeContentSnippets true), all pages. From the results keep only EXACT matches: title equal to EXPECT_TITLE character for character (same letter case, no extra spaces) AND parentId equal to EXPECT_PARENT. Ignore every other file the search returned.
   - 0 exact matches: run `{{LAUNCHER}} wait` (30 s) and poll again.
   - 2 or more exact matches: STOP the loop. Do not record, do not adopt, do not delete or touch any file. Report "Drive duplicate name: <count> files titled EXPECT_TITLE" so the operator can decide.
   - Exactly 1 exact match: get_file_metadata for its id. It is genuine only if title == EXPECT_TITLE, parentId == EXPECT_PARENT, mimeType == "application/pdf" and fileSize == EXPECT_SIZE (compare as text). If any field differs or is missing (often a sync still uploading), it is not ready: wait and poll again.
   - At most 20 polls in total. If no single genuine match is found by then, do NOT record anything; stop the loop and report "Drive sync pending: <display_filename>". A later run offers it again as DRIVE_RECONCILE_PENDING; nothing is written twice.
   - When exactly one genuine match is found: write record.json {"outcome": "CREATED", "file": {"id","title","parentId","mimeType","fileSize"}} from that get_file_metadata result and run CLI `drive-record --intent <intent_id> --evidence <record.json>` exactly once. Never run drive-record twice for one intent, and never for a file that failed a check.
4. NOTIFY_DRAFT (fields: notification_id, to, subject, body):
   a. Gmail create_draft with exactly that `to`, `subject` and `body` (plain text, nothing added).
   b. Gmail get_draft for the new draft id with messageFormat RAW. Write the `raw` value exactly, unmodified, to draft.raw.
   c. CLI `notify-draft-evidence --notification <id> --draft-id <draft id> --raw-file <draft.raw>`.
   d. CLI `notify-seal --notification <id> --draft-id <draft id>`. Only if outcome is SEALED: Gmail send_message with draftId = `send_draft_id` from that output. If not SEALED: do not send; leave the draft; back to step 2.
   e. Write sent.json {"outcome": "SENT", "draft_id": <draft id>, "message_id": <id returned by send_message>} and run CLI `notify-record --notification <id> --evidence <sent.json>`. If send_message failed or gave no answer use {"outcome": "ERROR"} or {"outcome": "NO_RESPONSE"} instead.
   f. Back to step 2.
5. Run CLI `status` once.

## Final message (short)
- Per stream: acquisition result and the files uploaded/verified this run (display names only).
- Emails sent this run (subjects only).
- Any HOLD/UNKNOWN/RETRY_LATER/refusal with its EG_ support reference.
- One line: overall status from `status`.
