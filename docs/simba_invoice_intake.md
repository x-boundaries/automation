# SIMBA invoice intake: email to Google Drive

Tracking: #244 (child of #117). Export: [`n8n-workflows/simba_invoice_intake.workflow.json`](../n8n-workflows/simba_invoice_intake.workflow.json), live name `SIMBA Invoices - Email To Drive`.

This repository copy is inactive design evidence. It is not imported, activated or run anywhere. Import, credential binding, folder setup and activation each need the owner's separate approval.

## What it does

SIMBA sends each invoice as an email with a link, not an attachment. The workflow watches the intake mailbox, reads each SIMBA invoice email, downloads the invoice PDF and stores it in Google Drive. Anything it cannot prove goes to a review folder for a person.

- **When it runs:** the Gmail Trigger checks the mailbox every minute. It only sees mail that arrives after activation; older invoices are not back-filled. Mail in Spam or Trash is not seen.
- **What it reads:** emails from `bills@my.simba.sg`, or with "SIMBA Invoice" in the subject (this also catches forwarded copies and internal replies).
- **What it changes:** it creates one file per invoice in Google Drive. It never changes, labels or deletes email, and never edits or deletes Drive files.
- **No AI:** every decision is fixed code in the workflow. There is no language model in the runtime path.

## How each email is decided

| Email | Result |
| --- | --- |
| Genuine SIMBA email, sent to an address listed as BROADBAND, with one invoice number, one "Issued On" date and one PDF link | PDF filed in the broadband folder as `Oct 2026 - SIMBA Broadband Invoice S0000000000.pdf` |
| Same, sent to an address listed as MOBILE | PDF filed in the mobile folder as `Oct 2026 - SIMBA Mobile Invoice S0000000000.pdf` |
| Genuine SIMBA email whose single `To` line has no listed address or both a BROADBAND and a MOBILE address, or with several `To` lines or more than one `Cc` line | Review folder |
| Says it is from SIMBA but Gmail cannot prove it | Review folder (it may be fake) |
| A forwarded copy (Apple Mail "Fwd:" or Outlook "FW:") | Review folder, with a "probably Broadband/Mobile" hint when the quoted original recipient is a listed address |
| Missing or repeated invoice number, date or PDF link, or an attached file | Review folder |
| PDF cannot be downloaded, or the download is not a real PDF | Review folder, as a text note |
| Not a SIMBA invoice (for example an internal reply that quotes nothing) | Ignored |

Review items are named `Needs review - ... .pdf` when the PDF was downloaded, or `Needs review - ... .txt` for a note. The note says why the email was not filed and how to find it in Gmail. It never copies the PDF link, the amount or the email body.

The month in the name comes from the email's `Issued On: DD-Mon-YYYY` line, not from the send time. The invoice amount is never read or used.

### Proving the email is from SIMBA

Only Gmail's own top `Authentication-Results` header (from `mx.google.com`) counts; anything lower down could have been written by the sender. It must show both:

- SIMBA's domain passing: exactly one DMARC result, and it is `dmarc=pass header.from=my.simba.sg` (a failing result is never outvoted); and
- delivery through Amazon SES: a passing DKIM signature by `amazonses.com`, or SPF passing for an `amazonses.com` sender.

The `From` header must be exactly `bills@my.simba.sg`. Manual forwards hide SIMBA's own checks, so they always go to review. Gmail's own server-side forwarding keeps SIMBA's DKIM signature and can pass.

### Telling broadband from mobile

SIMBA's emails look the same for both accounts, so the account comes only from where SIMBA sent the email. The `billing_addresses` setting in **Intake Settings** lists each billing address with its account, as `address = BROADBAND` or `address = MOBILE` pairs separated by new lines, commas or semicolons:

```text
personal-billing@example.com = BROADBAND
company-billing@example.com = MOBILE
```

An account can have more than one address, which helps while an address is being changed. When a billing address changes in SIMBA My Account, edit this list; the workflow itself does not change. The workflow matches the list only against the `To` header of a genuine SIMBA email. The email must have exactly one `To` line and at most one `Cc` line. `Cc`, `Delivered-To` and `X-Original-To` are never used, because anyone who receives a genuine SIMBA invoice could resend it to the intake mailbox with one of those headers added. SIMBA's DKIM signature normally covers `To`, and both a billing-address alias and server-side auto-forwarding keep the billing address in `To`. A malformed entry, or one address listed for both accounts, stops the run until the list is fixed.

On a manual forward, the same list is checked against the quoted original `To:` line. A match names the review file "probably Broadband/Mobile"; a forward is never filed automatically.

**Current state:** SIMBA bills go to two of the forwarder's addresses, one per account, and arrive in the intake mailbox only as manual forwards. Listing those two addresses gives every forward the right hint, and all of them go to review. Invoices are filed automatically once SIMBA mail reaches the intake mailbox directly, either because the billing addresses are changed (for example to `+broadband` and `+mobile` aliases of the intake mailbox) or through server-side auto-forwarding, which keeps SIMBA's original headers and DKIM signature.

### The PDF link

The invoice link goes through Amazon SES click tracking (`*.awstrack.me`). The real PDF address is written, URL-encoded, inside the tracking link. The workflow reads it out of the link and downloads from `https://billing-invoice-pdf.amdocs-dbs.com/.../pdf` directly, so the tracker is never called and no "click" is recorded. Only that exact host over HTTPS is accepted, and redirects are not followed.

Whether that address works without a SIMBA login, and for how long, is **not yet verified**. If it does not, every invoice lands in review as a note and a person downloads the PDF from SIMBA.

### Duplicates

Each stored file carries Drive properties: `simbaIntakeKey` (the invoice number, or `gmail-<message id>` when there is none), `simbaStatus` (`FILED` or `REVIEW`), `simbaGmailId`, `simbaAccount` and, for PDFs, `simbaSha256`. Before storing, the workflow searches Drive for the same key:

- a filed invoice is never filed twice;
- a review copy is never stored twice, so a long reply thread that keeps quoting one invoice produces one review item;
- a genuine SIMBA email still gets filed when only a review copy of that invoice exists. Delete the review copy by hand. If that filing then falls back to review (for example the PDF download fails), no second review copy is stored.

Emails are handled one at a time, so two emails for the same invoice in the same minute cannot both be stored.

### Checking what was stored

After each upload the workflow reads the file back from Drive and checks its name, folder, size, properties and, for PDFs, Google's SHA-256 checksum against the downloaded bytes.

## Failures

Any Gmail or Drive failure (credential expired, Drive unavailable, duplicate check incomplete, upload not confirmed, read-back mismatch) stops the run. The workflow keeps failed executions (`saveDataErrorExecution: all`) so the failure is visible in n8n's Executions list; successful runs keep no email data. A kept failed execution contains that email's content, including the PDF link, and stays in the owner's n8n only; delete it once the email is handled.

The Gmail Trigger does not offer an email again after a failed run. When a run fails:

1. Open the failed execution in n8n and read the error on the red node. The messages say what to check.
2. Fix the cause (for example reconnect the Google credential).
3. Handle the affected email by hand: find it in Gmail, download the invoice PDF from SIMBA and save it in the right folder with the naming above.

A failure on one email also stops the rest of that minute's batch; handle those by hand too.

## Setup (owner, each step separately approved)

1. Decide how SIMBA mail reaches the intake mailbox directly: change each account's billing address in SIMBA My Account (for example to `+broadband` and `+mobile` aliases), or set up server-side auto-forwarding from the current billing addresses. Until then, everything goes to review.
2. Owner decision: broadband and mobile invoices both file into the EnergyGrid invoice folder used by #226, so set `broadband_folder_id` and `mobile_folder_id` to that folder. Choose a separate review folder; the workflow refuses to run if `review_folder_id` equals an invoice folder, so review notes never land among filed invoices. Every Drive call uses `supportsAllDrives=false`, so check before activation that both folders are in **My Drive** (not a shared drive) and that the Google account behind the n8n Drive credential can add files to them. Otherwise uploads fail loudly.
3. In n8n, create a Gmail OAuth2 credential on the intake mailbox and a Google Drive OAuth2 credential. Bind them in a local import copy: Gmail to `Watch Mailbox For SIMBA Emails` and `Fetch Full Email`; Drive to the four Drive nodes. Never commit the bound copy.
4. In the local import copy, fill **Intake Settings**: `broadband_folder_id`, `mobile_folder_id`, `review_folder_id` and `billing_addresses`. The workflow refuses to run while a folder ID is still a `REPLACE_WITH_...` placeholder. While `billing_addresses` is still the placeholder nothing matches, so every invoice goes to review.
5. Import inactive, then activate only after the owner approves.

## Acceptance checks still to do with real mail

These cannot be proven offline:

- A real SIMBA email delivered directly shows the expected `Authentication-Results` (SIMBA domain plus Amazon SES). If not, every invoice lands in review with "Gmail could not confirm it really came from SIMBA".
- The extracted PDF address downloads a real PDF without a SIMBA login.
- The listed billing address appears in the single `To` line of directly delivered or auto-forwarded SIMBA mail.
- The `DKIM-Signature` header of a direct SIMBA email lists `To` in its `h=` tag, so the account-deciding header is covered by SIMBA's signature.
- SIMBA's HTML does not carry its logo as an inline image part. Any part with a file name counts as an unexpected attachment, so an inline logo would send every invoice to review.
- Drive's property search shows a newly uploaded file straight away. If two emails for one invoice arrive in one poll and both are filed, the duplicate check lagged; report it.
- n8n behaves as the workflow assumes: `$('Node').first()` returns the current loop iteration's data, the Crypto node passes the PDF through, and a redirect answer from the PDF host (with redirects off) reaches the download error output, which sends the invoice to review.
- A poll with two or more SIMBA emails stores each one once (the one-at-a-time loop).
- Drive returns `sha256Checksum` on read-back for the uploaded PDF.

## Not covered

- Back-filling invoices that arrived before activation.
- Billing period and due date: SIMBA's emails state neither.
- Original PDF file name: the stored name is built from the issue month, account and invoice number.
