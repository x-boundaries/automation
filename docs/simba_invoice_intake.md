# SIMBA invoice intake: email to Google Drive

Tracking: #244 (child of #117). Exports: [`n8n-workflows/simba_invoice_intake.workflow.json`](../n8n-workflows/simba_invoice_intake.workflow.json) (live name `SIMBA Invoices - Email To Drive`) and its error workflow [`n8n-workflows/simba_invoice_intake_error_alert.workflow.json`](../n8n-workflows/simba_invoice_intake_error_alert.workflow.json) (live name `SIMBA Invoices - Error Alert`).

These repository copies are inactive design evidence. They are not imported, activated or run anywhere. Import, credential binding, folder, label, account and alert setup, and activation each need the owner's separate approval.

## What it does

SIMBA sends each invoice as an email with a link, not an attachment. The workflow watches the intake mailbox, reads each SIMBA invoice email, downloads the invoice PDF, checks the PDF is our bill, and stores it in Google Drive with Broadband or Mobile in the name. Anything it cannot prove goes to a review folder for a person, with a Telegram alert. After the file is stored, the email gets a Gmail label saying what happened.

- **When it runs:** the Gmail Trigger checks the mailbox every minute. It only sees mail that arrives after activation; older invoices are not back-filled. Mail in Spam or Trash is not seen.
- **What it reads:** emails from `bills@my.simba.sg`, or with "SIMBA Invoice" in the subject (this also catches forwarded copies and internal replies), and the text of the downloaded invoice PDF.
- **What it changes:** it creates one file per invoice in Google Drive, adds one Gmail label to that email (`SIMBA/Filed` or `SIMBA/Needs review`), and sends a Telegram message for anything needing review or any failure. It never sends, moves, archives, deletes or marks email read, never removes labels, and never edits or deletes Drive files.
- **No AI:** every decision is fixed code in the workflow. No language model reads the email or the PDF, so hidden instructions in an email, link or PDF ("prompt injection") have nothing to act on; they are just text that fails or passes fixed checks.

## How each email is decided

| Email | Result | Gmail label | Telegram |
| --- | --- | --- | --- |
| Genuine SIMBA email, subject containing "SIMBA Invoice", addressed `To` the intake mailbox, with one invoice number, one "Issued On" date and one PDF link; the PDF shows that invoice number and exactly one of our account numbers | PDF filed in the invoice folder as `Oct 2026 - SIMBA Broadband Invoice S0000000000.pdf` (or `Mobile`) | `SIMBA/Filed` | none |
| Same, but the PDF does not show the invoice number, shows none of our account numbers, shows both a broadband and a mobile one, or its text cannot be read | PDF filed in the review folder | `SIMBA/Needs review` | review alert |
| Genuine SIMBA email addressed to someone else, or with several `To` lines or more than one `Cc` line | Review folder (it may be someone else's bill) | `SIMBA/Needs review` | review alert |
| Genuine SIMBA email whose subject does not say "SIMBA Invoice" | Review folder | `SIMBA/Needs review` | review alert |
| Says it is from SIMBA but Gmail cannot prove it | Review folder (it may be fake) | `SIMBA/Needs review` | review alert |
| A forwarded copy (Apple Mail "Fwd:" or Outlook "FW:") | Review folder | `SIMBA/Needs review` | review alert |
| Missing or repeated invoice number, date or PDF link, or an attached file | Review folder | `SIMBA/Needs review` | review alert |
| PDF cannot be downloaded, is not a real PDF, is over 10 MB, or carries active content | Review folder, as a text note (the PDF is not stored) | `SIMBA/Needs review` | review alert |
| Invoice already filed by an earlier run, and this is SIMBA's own email or a forwarded copy of it | Nothing new stored | `SIMBA/Filed` | none |
| Invoice already filed, but this email only claims to be from SIMBA; or a review copy is already stored | Nothing new stored | `SIMBA/Needs review` | review alert |
| Not a SIMBA invoice (for example an internal reply that quotes nothing) | Ignored | none | none |

Review items are named `Needs review - ... .pdf` when a safe PDF was downloaded, or `Needs review - ... .txt` for a note. The note says why the email was not filed and how to find it in Gmail. It never copies the PDF link, the amount, an account number or the email body.

The month in the name comes from the email's `Issued On: DD-Mon-YYYY` line, not from the send time. The invoice amount is never read or used.

**The first real bill may land in review.** No real SIMBA PDF has been seen in this work, so the PDF checks below are deliberately small. If the first bill goes to review with a `PDF_...` reason, compare its text with the rules and adjust the account numbers in n8n, or the rules in this repository.

### Proving the email is from SIMBA

Only Gmail's own top `Authentication-Results` header (from `mx.google.com`) counts; anything lower down could have been written by the sender. It must show both:

- SIMBA's domain passing: exactly one DMARC result, and it is `dmarc=pass header.from=my.simba.sg` (a failing result is never outvoted); and
- delivery through Amazon SES: a passing DKIM signature by `amazonses.com`, or SPF passing for an `amazonses.com` sender.

The `From` header must be exactly `bills@my.simba.sg`, and there must be exactly one `Subject` header containing "SIMBA Invoice" (any case). Manual forwards hide SIMBA's own checks, so they always go to review. Gmail's own server-side forwarding keeps SIMBA's DKIM signature and can pass.

### Recipient check

A genuine SIMBA bill is filed only when SIMBA addressed it to us. The `intake_recipients` setting in **Intake Settings** lists the address or addresses SIMBA sends our bills to, separated by new lines, commas or semicolons. Normally that is just the intake mailbox. Plus-addresses are not matched automatically; list each exact address.

The check reads only the `To` header of a genuine SIMBA email. The email must have exactly one `To` line, containing a listed address, and at most one `Cc` line. Only the mailbox address counts, never a display name: in `"intake@example.com" <someone@example.com>` the address is `someone@example.com`. `Cc`, `Delivered-To` and `X-Original-To` are never used, because anyone who receives a genuine SIMBA bill could resend it to the intake mailbox with one of those headers added. SIMBA's DKIM signature normally covers `To`.

If SIMBA mail reaches the intake mailbox by Gmail auto-forwarding from another mailbox instead of being sent to it directly, `To` shows that other mailbox's address; add it to `intake_recipients` (in n8n only, never in this repository). An entry that is not an email address stops the run until the setting is fixed.

### PDF content check: our account, broadband or mobile

After a safe PDF is downloaded, n8n's built-in **Extract From File** node (PDF to text) reads its text, and **Check Invoice PDF** applies plain text matching. There is no AI and no paid service. The PDF is never opened in a viewer or run.

- `broadband_account_numbers` and `mobile_account_numbers` in **Intake Settings** list our SIMBA account numbers as printed on the bill (more than one allowed, separated by new lines, commas or semicolons). They live only in n8n, never in this repository. Spaces and dashes are ignored on both sides, so `MB-000 000` matches `MB000000`. Each entry must be 5 to 30 letters or digits, and no number may be in both lists.
- The PDF must show the email's invoice number, and exactly one of our account numbers. A longer number that merely contains ours does not count.
- The matched account gives the bill type, which goes in the file name and in the Drive property `simbaBillType`.
- Anything else goes to review with the PDF and a reason: `PDF_INVOICE_NUMBER_MISMATCH`, `PDF_ACCOUNT_NOT_OURS` (another customer's bill sent to us), `PDF_ACCOUNT_UNCLEAR` (both types) or `PDF_TEXT_UNREADABLE` (for example a scanned image, or an extraction error).

### The PDF link, and malicious content

The invoice link goes through Amazon SES click tracking (`*.awstrack.me`). The real PDF address is written, URL-encoded, inside the tracking link. The workflow reads it out of the link and downloads from `https://billing-invoice-pdf.amdocs-dbs.com/.../pdf` directly, so the tracker is never called and no "click" is recorded.

- **Host allowlist:** only that exact host over HTTPS on the default port, with a path ending in `/pdf`, is accepted. Any other link is ignored and the email goes to review with `NO_PDF_LINK`. Redirects are never followed, so a redirect to any other host (or the same host) fails the download and goes to review.
- **Size and type:** the download must start with `%PDF-` and be at most 10 MB.
- **Active content:** a PDF containing `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile(s)`, `/AA` (automatic actions), `/XFA`, `/RichMedia`, `/GoToR`, `/GoToE`, `/SubmitForm`, `/ImportData` or an `/OpenAction` that is not just a page view (`/OpenAction [ ... ]`) is refused. Names hidden with `#xx` escapes are decoded first. Stream data (`stream ... endstream`) is skipped, because page content or image bytes can look like a key by chance. A refused PDF is not stored, not read and not opened; the invoice goes to review as a note (`PDF_ACTIVE_CONTENT`) with an alert. Limit: keys inside compressed object streams (`/ObjStm`) are not scanned. The PDF is still never executed; n8n only extracts its text, so keep n8n updated for its PDF library's security fixes.
- **Prompt injection:** nothing in the email, link or PDF is given to an AI model, so hidden text cannot instruct anything.

Whether the PDF address works without a SIMBA login, and for how long, is **not yet verified**. If it does not, every invoice lands in review as a note and a person downloads the PDF from SIMBA.

### Duplicates

Each stored file carries Drive properties: `simbaIntakeKey` (the invoice number, or `gmail-<message id>` when there is none), `simbaStatus` (`FILED` or `REVIEW`), `simbaGmailId`, `simbaBillType` (`BROADBAND` or `MOBILE`, when the PDF check passed) and, for PDFs, `simbaSha256`. Before storing, the workflow searches Drive for the same key:

- a filed invoice is never filed twice;
- a review copy is never stored twice, so a long reply thread that keeps quoting one invoice produces one review item;
- a genuine SIMBA email still gets filed when only a review copy of that invoice exists. Delete the review copy by hand. If that filing then falls back to review (for example the PDF download fails), no second review copy is stored.

An email whose invoice is already in Drive is still labelled: `SIMBA/Filed` when the invoice is filed and the email is SIMBA's own (or a forward of it), otherwise `SIMBA/Needs review`. Adding a label the email already has changes nothing.

Emails are handled one at a time, so two emails for the same invoice in the same minute cannot both be stored.

### Checking what was stored

After each upload the workflow reads the file back from Drive and checks its name, folder, size, properties and, for PDFs, Google's SHA-256 checksum against the downloaded bytes. Only then is the Gmail label added and any review alert sent.

## Alerts on Telegram

- **Review alert** (from the main workflow): every email labelled `SIMBA/Needs review` sends one message to `telegram_chat_id` with the reason codes, the invoice number and issue date if known, and whether a review copy was stored. It never includes an address, link, amount, account number or email text.
- **Failure alert** (from `SIMBA Invoices - Error Alert`): when the main workflow fails on its own, n8n runs the error workflow, which sends the workflow name, the step it stopped at, the execution number and link, and the time. The error message is left out because it can quote data; open the execution in n8n to read it. **The error workflow must be published (active):** n8n runs only the published version of an error workflow, and an unpublished one sends nothing. It does not run for manual editor runs.
- The error workflow must be selected in the main workflow's **Settings > Error workflow** after both are imported. Its n8n ID only exists after import, so it is set in n8n, never in this repository.

## Failures

Any Gmail read or Drive failure (credential expired, Drive unavailable, duplicate check incomplete, upload not confirmed, read-back mismatch) stops the run and sends the failure alert. The workflow keeps failed executions (`saveDataErrorExecution: all`) so the failure is visible in n8n's Executions list; successful runs keep no email data. A kept failed execution contains that email's content, including the PDF link, and stays in the owner's n8n only; delete it once the email is handled.

A **label** or **review alert** failure never stops or undoes filing. Each step retries three times; if it still fails, the email is noted, the rest of the batch carries on, and at the end **Report Failures** fails the run with the number of emails and their Gmail message ids, which also sends the failure alert. Add those labels by hand, check the Gmail and Telegram credentials, the label IDs and `telegram_chat_id`.

The Gmail Trigger does not offer an email again after a failed run. When a run fails:

1. Open the failed execution in n8n and read the error on the red node. The messages say what to check.
2. Fix the cause (for example reconnect the Google credential).
3. Handle the affected email by hand: find it in Gmail, download the invoice PDF from SIMBA, save it in the right folder with the naming above, and add the label.

A Gmail read or Drive failure on one email also stops the rest of that minute's batch; handle those by hand too. Such a failure also ends the run before **Report Failures**, so check that the emails handled earlier in that batch have their SIMBA label.

## Setup (owner, each step separately approved)

1. Have SIMBA send both bills (broadband and mobile) straight to the intake mailbox, by changing the bill email in SIMBA My Account. Until then, manual forwards all go to review.
2. Owner decision: all SIMBA invoices file into the EnergyGrid invoice folder used by #226, so set `invoice_folder_id` to that folder. Choose a separate review folder; the workflow refuses to run if `review_folder_id` equals `invoice_folder_id`, so review notes never land among filed invoices. Every Drive call uses `supportsAllDrives=false`, so check before activation that both folders are in **My Drive** (not a shared drive) and that the Google account behind the n8n Drive credential can add files to them. Otherwise uploads fail loudly.
3. In the intake mailbox's Gmail web UI, create a label `SIMBA` with two nested labels, `Filed` and `Needs review`. Read their label IDs (for example from the Gmail API labels list) for the next step. The workflow does not create labels.
4. In n8n, create a Gmail OAuth2 credential on the intake mailbox and a Google Drive OAuth2 credential, and reuse the Telegram credential the EnergyGrid alerts use. Bind them in a local import copy: Gmail to `Watch Mailbox For SIMBA Emails`, `Fetch Full Email` and `Label SIMBA Email`; Drive to the four Drive nodes; Telegram to `Send Review Alert` and, in the error workflow, `Send Error Alert`. n8n's Gmail credential already includes label permission. Never commit the bound copies.
5. In the local import copy, fill **Intake Settings**: `invoice_folder_id`, `review_folder_id`, `intake_recipients` (the intake mailbox address), `filed_label_id`, `review_label_id`, `broadband_account_numbers`, `mobile_account_numbers` (from a real bill or SIMBA My Account) and `telegram_chat_id`. In the error workflow, set the `Send Error Alert` chat ID. The workflow refuses to run while any setting is still a `REPLACE_WITH_...` placeholder, if the two label IDs are the same, or if an account number is malformed or in both lists.
6. Import both inactive. In the main workflow's **Settings**, set **Error workflow** to `SIMBA Invoices - Error Alert`.
7. Run the real-mail checks below.
8. Activation, only after the owner approves: publish `SIMBA Invoices - Error Alert` first (or together with the main workflow), then publish `SIMBA Invoices - Email To Drive`. Confirm one real failure alert: create a throwaway workflow with a Schedule Trigger and a Stop and Error node, set its **Error workflow** to `SIMBA Invoices - Error Alert`, publish it until one Telegram failure alert arrives, then unpublish and delete it.

## Acceptance checks still to do with real mail

These cannot be proven offline:

- A real SIMBA email delivered directly shows the expected `Authentication-Results` (SIMBA domain plus Amazon SES). If not, every invoice lands in review with "Gmail could not confirm it really came from SIMBA".
- The extracted PDF address downloads a real PDF without a SIMBA login.
- The real PDF passes the content check: Extract From File returns its text, the text shows the invoice number and exactly one of our account numbers, and the right bill type appears in the name. If not, tune the account numbers or the rules.
- The real PDF passes the active-content check. A real SIMBA PDF with, for example, an automatic action would land in review with `PDF_ACTIVE_CONTENT`; report it before changing the rule.
- The intake mailbox address appears in the single `To` line of directly delivered (or auto-forwarded) SIMBA mail.
- The `DKIM-Signature` header of a direct SIMBA email lists `To` in its `h=` tag, so the recipient check relies on a signed header.
- SIMBA's HTML does not carry its logo as an inline image part. Any part with a file name counts as an unexpected attachment, so an inline logo would send every invoice to review.
- Drive's property search shows a newly uploaded file straight away. If two emails for one invoice arrive in one poll and both are filed, the duplicate check lagged; report it.
- n8n behaves as the workflow assumes: `$('Node').first()` returns the current loop iteration's data, the Crypto node passes the PDF through, and a redirect answer from the PDF host (with redirects off) reaches the download error output, which sends the invoice to review.
- A poll with two or more SIMBA emails stores each one once (the one-at-a-time loop).
- Drive returns `sha256Checksum` on read-back for the uploaded PDF.
- The filed email carries `SIMBA/Filed` (or `SIMBA/Needs review`) and nothing else about it changed: still in the Inbox, read state unchanged, no other label added or removed. A second run on the same email stores nothing and leaves one SIMBA label.
- A review outcome sends one Telegram review alert with no address or account number, and the error workflow's Telegram message arrives (run it once from the editor; its Error Trigger supplies sample data). A real failure alert needs the error workflow published, so it is confirmed at activation (Setup step 8).

The malicious-PDF refusal is proven with synthetic PDFs in the offline tests only; never test it with a real malicious file.

## Not covered

- Back-filling invoices that arrived before activation.
- Billing period and due date: SIMBA's emails state neither.
- Original PDF file name: the stored name is built from the issue month, bill type and invoice number.
- Active-content keys inside compressed PDF object streams (see "The PDF link, and malicious content").
