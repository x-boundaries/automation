# SIMBA invoice intake: email to Google Drive

Tracking: #244 (child of #117). Export: [`n8n-workflows/simba_invoice_intake.workflow.json`](../n8n-workflows/simba_invoice_intake.workflow.json), live name `SIMBA Invoices - Email To Drive`.

This repository copy is inactive design evidence. It is not imported, activated or run anywhere. Import, credential binding, folder and label setup, and activation each need the owner's separate approval.

## What it does

SIMBA sends each invoice as an email with a link, not an attachment. The workflow watches the intake mailbox, reads each SIMBA invoice email, downloads the invoice PDF and stores it in Google Drive. Anything it cannot prove goes to a review folder for a person. After the file is stored, the email gets a Gmail label saying what happened.

- **When it runs:** the Gmail Trigger checks the mailbox every minute. It only sees mail that arrives after activation; older invoices are not back-filled. Mail in Spam or Trash is not seen.
- **What it reads:** emails from `bills@my.simba.sg`, or with "SIMBA Invoice" in the subject (this also catches forwarded copies and internal replies).
- **What it changes:** it creates one file per invoice in Google Drive, then adds one Gmail label to that email: `SIMBA/Filed` or `SIMBA/Needs review`. It never sends, moves, archives, deletes or marks email read, never removes labels, and never edits or deletes Drive files.
- **No AI:** every decision is fixed code in the workflow. There is no language model in the runtime path.

## How each email is decided

| Email | Result | Gmail label |
| --- | --- | --- |
| Genuine SIMBA email, subject containing "SIMBA Invoice", addressed `To` the intake mailbox, with one invoice number, one "Issued On" date and one PDF link | PDF filed in the invoice folder as `Oct 2026 - SIMBA Invoice S0000000000.pdf` | `SIMBA/Filed` |
| Genuine SIMBA email addressed to someone else, or with several `To` lines or more than one `Cc` line | Review folder (it may be someone else's bill) | `SIMBA/Needs review` |
| Genuine SIMBA email whose subject does not say "SIMBA Invoice" | Review folder | `SIMBA/Needs review` |
| Says it is from SIMBA but Gmail cannot prove it | Review folder (it may be fake) | `SIMBA/Needs review` |
| A forwarded copy (Apple Mail "Fwd:" or Outlook "FW:") | Review folder | `SIMBA/Needs review` |
| Missing or repeated invoice number, date or PDF link, or an attached file | Review folder | `SIMBA/Needs review` |
| PDF cannot be downloaded, or the download is not a real PDF | Review folder, as a text note | `SIMBA/Needs review` |
| Invoice already filed by an earlier run (including a forwarded copy of it) | Nothing new stored | `SIMBA/Filed` |
| Not a SIMBA invoice (for example an internal reply that quotes nothing) | Ignored | none |

Broadband and mobile bills are handled the same way and file into the same folder. SIMBA's emails do not say which service a bill is for, so the file name carries no bill type; the invoice number keeps names unique.

Review items are named `Needs review - ... .pdf` when the PDF was downloaded, or `Needs review - ... .txt` for a note. The note says why the email was not filed and how to find it in Gmail. It never copies the PDF link, the amount or the email body.

The month in the name comes from the email's `Issued On: DD-Mon-YYYY` line, not from the send time. The invoice amount is never read or used.

### Proving the email is from SIMBA

Only Gmail's own top `Authentication-Results` header (from `mx.google.com`) counts; anything lower down could have been written by the sender. It must show both:

- SIMBA's domain passing: exactly one DMARC result, and it is `dmarc=pass header.from=my.simba.sg` (a failing result is never outvoted); and
- delivery through Amazon SES: a passing DKIM signature by `amazonses.com`, or SPF passing for an `amazonses.com` sender.

The `From` header must be exactly `bills@my.simba.sg`, and there must be exactly one `Subject` header containing "SIMBA Invoice" (any case). Manual forwards hide SIMBA's own checks, so they always go to review. Gmail's own server-side forwarding keeps SIMBA's DKIM signature and can pass.

### Recipient check

A genuine SIMBA bill is filed only when SIMBA addressed it to us. The `intake_recipients` setting in **Intake Settings** lists the address or addresses SIMBA sends our bills to, separated by new lines, commas or semicolons. Normally that is just the intake mailbox. Plus-addresses are not matched automatically; list each exact address.

The check reads only the `To` header of a genuine SIMBA email. The email must have exactly one `To` line, containing a listed address, and at most one `Cc` line. `Cc`, `Delivered-To` and `X-Original-To` are never used, because anyone who receives a genuine SIMBA bill could resend it to the intake mailbox with one of those headers added. SIMBA's DKIM signature normally covers `To`. This stops someone else's genuine SIMBA bill being filed among our invoices.

If SIMBA mail reaches the intake mailbox by Gmail auto-forwarding from another mailbox instead of being sent to it directly, `To` shows that other mailbox's address; add it to `intake_recipients` (in n8n only, never in this repository). An entry that is not an email address stops the run until the setting is fixed.

### Bill type (broadband or mobile) later

Nothing in SIMBA's email tells broadband from mobile. If the owner later needs the bill type in file names, a step that learns it (for example from the PDF text) only has to set `service` to `BROADBAND` or `MOBILE` on the decision; **Prepare Drive File** then names the file `Oct 2026 - SIMBA Broadband Invoice S0000000000.pdf`. Whether the PDF shows the plan or account is not yet known: no SIMBA bill PDF has been opened in this work.

### The PDF link

The invoice link goes through Amazon SES click tracking (`*.awstrack.me`). The real PDF address is written, URL-encoded, inside the tracking link. The workflow reads it out of the link and downloads from `https://billing-invoice-pdf.amdocs-dbs.com/.../pdf` directly, so the tracker is never called and no "click" is recorded. Only that exact host over HTTPS is accepted, and redirects are not followed.

Whether that address works without a SIMBA login, and for how long, is **not yet verified**. If it does not, every invoice lands in review as a note and a person downloads the PDF from SIMBA.

### Duplicates

Each stored file carries Drive properties: `simbaIntakeKey` (the invoice number, or `gmail-<message id>` when there is none), `simbaStatus` (`FILED` or `REVIEW`), `simbaGmailId` and, for PDFs, `simbaSha256`. Before storing, the workflow searches Drive for the same key:

- a filed invoice is never filed twice;
- a review copy is never stored twice, so a long reply thread that keeps quoting one invoice produces one review item;
- a genuine SIMBA email still gets filed when only a review copy of that invoice exists. Delete the review copy by hand. If that filing then falls back to review (for example the PDF download fails), no second review copy is stored.

An email whose invoice is already in Drive is still labelled. Adding a label the email already has changes nothing.

Emails are handled one at a time, so two emails for the same invoice in the same minute cannot both be stored.

### Checking what was stored

After each upload the workflow reads the file back from Drive and checks its name, folder, size, properties and, for PDFs, Google's SHA-256 checksum against the downloaded bytes. Only then is the Gmail label added.

## Failures

Any Gmail read or Drive failure (credential expired, Drive unavailable, duplicate check incomplete, upload not confirmed, read-back mismatch) stops the run. The workflow keeps failed executions (`saveDataErrorExecution: all`) so the failure is visible in n8n's Executions list; successful runs keep no email data. A kept failed execution contains that email's content, including the PDF link, and stays in the owner's n8n only; delete it once the email is handled.

A **label** failure never stops or undoes filing. The label step retries three times; if it still fails, the email is noted, the rest of the batch carries on, and at the end **Report Label Failures** fails the run with the number of emails and their Gmail message ids. Add those labels by hand and check the Gmail credential and the label IDs.

The Gmail Trigger does not offer an email again after a failed run. When a run fails:

1. Open the failed execution in n8n and read the error on the red node. The messages say what to check.
2. Fix the cause (for example reconnect the Google credential).
3. Handle the affected email by hand: find it in Gmail, download the invoice PDF from SIMBA, save it in the right folder with the naming above, and add the label.

A Gmail read or Drive failure on one email also stops the rest of that minute's batch; handle those by hand too.

## Setup (owner, each step separately approved)

1. Have SIMBA send both bills (broadband and mobile) straight to the intake mailbox, by changing the bill email in SIMBA My Account. Until then, manual forwards all go to review.
2. Owner decision: all SIMBA invoices file into the EnergyGrid invoice folder used by #226, so set `invoice_folder_id` to that folder. Choose a separate review folder; the workflow refuses to run if `review_folder_id` equals `invoice_folder_id`, so review notes never land among filed invoices. Every Drive call uses `supportsAllDrives=false`, so check before activation that both folders are in **My Drive** (not a shared drive) and that the Google account behind the n8n Drive credential can add files to them. Otherwise uploads fail loudly.
3. In the intake mailbox's Gmail web UI, create a label `SIMBA` with two nested labels, `Filed` and `Needs review`. Read their label IDs (for example from the Gmail API labels list) for the next step. The workflow does not create labels.
4. In n8n, create a Gmail OAuth2 credential on the intake mailbox and a Google Drive OAuth2 credential. Bind them in a local import copy: Gmail to `Watch Mailbox For SIMBA Emails`, `Fetch Full Email` and `Label SIMBA Email`; Drive to the four Drive nodes. n8n's Gmail credential already includes label permission. Never commit the bound copy.
5. In the local import copy, fill **Intake Settings**: `invoice_folder_id`, `review_folder_id`, `intake_recipients` (the intake mailbox address), `filed_label_id` and `review_label_id`. The workflow refuses to run while any of them is still a `REPLACE_WITH_...` placeholder, or if the two label IDs are the same.
6. Import inactive, run the real-mail checks below, then activate only after the owner approves.

## Acceptance checks still to do with real mail

These cannot be proven offline:

- A real SIMBA email delivered directly shows the expected `Authentication-Results` (SIMBA domain plus Amazon SES). If not, every invoice lands in review with "Gmail could not confirm it really came from SIMBA".
- The extracted PDF address downloads a real PDF without a SIMBA login.
- The intake mailbox address appears in the single `To` line of directly delivered (or auto-forwarded) SIMBA mail.
- The `DKIM-Signature` header of a direct SIMBA email lists `To` in its `h=` tag, so the recipient check relies on a signed header.
- SIMBA's HTML does not carry its logo as an inline image part. Any part with a file name counts as an unexpected attachment, so an inline logo would send every invoice to review.
- Drive's property search shows a newly uploaded file straight away. If two emails for one invoice arrive in one poll and both are filed, the duplicate check lagged; report it.
- n8n behaves as the workflow assumes: `$('Node').first()` returns the current loop iteration's data, the Crypto node passes the PDF through, and a redirect answer from the PDF host (with redirects off) reaches the download error output, which sends the invoice to review.
- A poll with two or more SIMBA emails stores each one once (the one-at-a-time loop).
- Drive returns `sha256Checksum` on read-back for the uploaded PDF.
- The filed email carries `SIMBA/Filed` (or `SIMBA/Needs review`) and nothing else about it changed: still in the Inbox, read state unchanged, no other label added or removed. A second run on the same email stores nothing and leaves one SIMBA label.

## Not covered

- Back-filling invoices that arrived before activation.
- Billing period and due date: SIMBA's emails state neither.
- Original PDF file name: the stored name is built from the issue month and invoice number.
- Broadband or mobile in the file name (see "Bill type (broadband or mobile) later").
