# Data Policy

**WARNING: DO NOT COMMIT REAL OPERATIONAL DATA**

This repository is for AutoCount 2 automation code, runbooks, secret-free configuration templates, vendor import-template references, schemas, and dummy sample data related to safe local automation.

Task tracking, pending-work dashboards, completed-task logs, personal planning notes, and real operational exports belong outside this repository.

## Allowed in this repository

- Automation code and scripts.
- Documentation and runbooks for approved AutoCount automation.
- Secret-free configuration templates.
- Vendor AutoCount import templates used as references.
- Schemas and template definitions.
- Dummy sample data that is clearly marked as sample/dummy.

## Strictly Forbidden

You MUST NOT store any of the following in this repository:

- Real stock master or product master lists.
- Real customer data.
- Real supplier data.
- Real AP/AR records or invoices.
- Bank or payment exports.
- AutoCount database backups.
- API keys, passwords, or credentials, including `.env` files.
- Production exports of any kind.

Real data should live in approved, secure storage outside of GitHub (e.g., shared drives, secure vaults).

If local testing requires actual files, ensure they are placed in ignored folders such as `data/`, `exports/`, `outputs/`, or `exceptions/`.

## Personal-data guard

This repository is public. A personal email address or phone number in any pushed
commit is published, even if a later commit removes it, so the guard checks every
line a change adds.

- Scope: email addresses and phone numbers only. It does not look for names,
  street addresses, identity numbers or account numbers.
- Scanner: `scripts/pii_guard.py` (Python standard library only). It flags email
  addresses, Singapore numbers (`+65`, the `65XXXXXXXX` storage form used by
  member intake, including inside `wa.me` links, and bare 8-digit numbers
  starting with 6, 8 or 9) and other `+`-prefixed international numbers.
- Allowlist: `config/pii_guard_allowlist.json` covers reserved domains
  (`example.com`, `example.org`, `example.net`, `.example`, `.test`, `.invalid`,
  `.localhost`), GitHub noreply identities, named X-Boundaries role mailboxes
  and named vendor or system addresses (a vendor billing mailbox, AWS SES
  message-id hosts, a synthetic `.internal` test host, the Claude Code
  co-author no-reply address).
  Every entry needs a reason, and `tests/test_pii_guard.py` pins the exact set, so
  widening it is a reviewed change.
- Opt-out marker for a justified line: put `pii-guard: allow (<reason>)` on the
  same line. A marker without a reason does not count.
- Use reserved examples such as `name@example.com` in docs, fixtures and tests. In
  test code, build realistic-looking values at runtime from fragments.

One-time setup per clone (git cannot enable a tracked hook by itself):

```bash
git config core.hooksPath .githooks
```

The tracked `.githooks/pre-commit` hook then blocks a commit whose staged
additions contain a hit, and fails closed if Python 3.9+ or the scanner is
missing. Run the scanner by hand at any time:

```bash
python scripts/pii_guard.py --staged
python scripts/pii_guard.py --range origin/main..HEAD
python scripts/pii_guard.py --tree HEAD --report-only
```

Exit codes are 0 (clean), 1 (hit) and 2 (error). Output is redacted unless
`--show-values` is passed, which is for local use only.

CI: `.github/workflows/pii-guard.yml` runs on every pull request and scans each
commit in the pull-request range, not just the final diff, including each
commit's message. Author and committer emails are not scanned (owner decision).
A hit, a missing or empty range, an unknown commit or a parse error fails the
job.

Diffs are forced to text, so a `-diff` or `binary` attribute cannot hide added
lines. No binary file is skipped whole:

- Printable ASCII, UTF-16LE and UTF-16BE runs of six or more characters are
  always scanned, so PDF metadata, image text chunks and legacy `.xls` or `.doc`
  strings are covered.
- BOM-marked UTF-8, UTF-16 or UTF-32 text is also decoded line by line (Windows
  PowerShell 5.1 `Out-File` writes UTF-16).
- Zip containers, recognised by their signature whatever the file is called
  (so `.zip`, `.xlsx`, `.docx`, `.pptx` and renamed zips), have every member
  expanded and scanned, deflated members and nested zips included, up to three
  levels deep within one 64 MiB budget. A corrupt, encrypted, oversized or
  too deeply nested container fails the scan.
- gzip, 7z, RAR and tar archives fail the scan at any depth, because the
  scanner cannot open them.
- A reviewed binary type (images, `.pdf`, `.xls`, `.doc`) must start with its
  file signature, so a renamed archive fails the scan. Any other file
  containing NUL bytes fails the scan.

Known limits: compressed streams inside a non-zip binary, such as a PDF's
deflated page content, are not decompressed; packages embedded inside OLE2
files (legacy `.xls` or `.doc` objects) are scanned by text runs only; and CI
runs the scanner and allowlist from the pull request itself. Hits inside binary
content are reported as `file (text runs):N` or `container!member:N`.

If the guard flags a value, two things are needed:

1. Report it to the repository owner. A value CI flags on a pushed pull request
   is already public, so fixing it does not undo the exposure; the owner decides
   whether anything else (for example telling the person) is needed.
2. Remove it from the commit that added it (amend or rebase the branch before
   merge); a follow-up commit that deletes it still leaves it in the published
   history. If a value has already reached `main`, do not rewrite shared history
   without the owner's approval.

A pre-commit hook hit is caught before anything is published; just fix it.

Not scanned: commits created at merge time on GitHub (web merges and squash
merges land on `main`, outside any pull-request range), commit author and
committer identities (owner decision, 2026-10-21), pull-request titles, bodies
and comments, and branches pushed without a pull request.
