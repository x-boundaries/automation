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
  starting with 6, 8 or 9) and other `+`-prefixed international numbers, with
  digit groups split by a space, hyphen, no-break space, `&nbsp;` or en dash.
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

`core.hooksPath` replaces `.git/hooks` for that clone: git stops running any
hook in `.git/hooks` (for example a personal `pre-push` hook). Move such a hook
into `.githooks/` only through a reviewed change, or keep it outside the repo
and call it yourself.

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
  always scanned (the runs keep no-break spaces, and UTF-16 runs keep en
  dashes), so PDF metadata, image text chunks and legacy `.xls` or `.doc`
  strings are covered. The runs as cut before #270 (without those characters)
  are scanned too, so a wider run cannot join a hit to an opt-out marker.
- BOM-marked UTF-8, UTF-16 or UTF-32 text is also decoded line by line (Windows
  PowerShell 5.1 `Out-File` writes UTF-16).
- Zip containers, recognised by their signature whatever the file is called
  (so `.zip`, `.xlsx`, `.docx`, `.pptx` and renamed zips), have every member
  expanded and scanned, deflated members and nested zips included, up to three
  levels deep within one 64 MiB budget. A corrupt, encrypted, oversized or
  too deeply nested container fails the scan.
- A zip appended to a reviewed binary or to text (a polyglot) is found by its
  end-of-central-directory record and expanded the same way; if it cannot be
  opened, the scan fails. A zip member holding an appended zip still has its
  PDF streams or `.xls` cells decoded.
- A second zip in the same bytes fails the scan: more than one
  end-of-central-directory record outside the members' data, or a zip file
  header that does not start a parsed member (such as a zip in front of the one
  that is read). A zip stored whole inside a member is expanded as usual.
- Two documents with the same display name in one file fail the scan: a
  repeated member name, names that differ only in control characters, or a
  member named like another document's ` (joined text)`, ` (deflated streams)`,
  ` (numeric cells)`, ` (text runs)` or `member!inner` name. Added lines are
  matched per name, so a second document with the name would hide the first
  one's lines (#270 G2 W4; the scanner before #270 had the same gap).
- Office XML parts inside a container are also scanned with their text runs
  joined (Word, PowerPoint and shared-string runs, plus XML character
  references such as `&#64;`), so a number or address split across formatting
  runs is still seen. Hyperlinks, tracked insertions, moved text, smart tags,
  content controls, simple fields, custom XML and comment ranges around runs
  are joined too, whichever edge the split falls on (their property elements
  are stripped like run properties). A no-break hyphen reads as a hyphen, a
  soft hyphen reads as nothing, and tracked formatting changes are stripped
  with the run properties. Separate paragraphs and cells are not joined.
- A PDF's FlateDecode streams (page content, object streams, metadata) are
  decompressed with the standard-library `zlib` within the same 64 MiB budget
  and scanned by text runs. A stream whose object names FlateDecode as its
  first filter fails the scan when its data has no zlib header or will not
  decompress, and so does one that exceeds the budget. A stream with a zlib
  header that is not declared and will not decompress (a bad checksum, for
  example) is retried as raw deflate and whatever it yields is scanned. A PDF
  that names `/Encrypt` (an encrypted PDF, including owner-password-only
  "secured" files) fails the scan. Both name checks also read `#xx` name
  escapes (`/Encr#79pt` is `/Encrypt`). A zip member is read as a PDF when the
  header appears within its first 1024 bytes.
- A legacy `.xls` workbook has its numeric cells decoded (number, RK, multiple
  RK and cached formula results), so a phone number typed into a number cell
  is seen. A workbook the scanner cannot read, or an encrypted one, fails the
  scan.
- gzip, 7z, RAR and tar archives fail the scan at any depth, because the
  scanner cannot open them.
- A reviewed binary type (images, `.pdf`, `.xls`, `.doc`) must start with its
  file signature, so a renamed archive fails the scan. Any other file
  containing NUL bytes fails the scan.

Hits inside binary content are reported as `file (text runs):N`,
`file (deflated streams):N`, `file (numeric cells):N`, `container!member:N` or
`container!member (joined text):N`.

Known limits (accepted, #270):

- PDF text is seen only when it is literal text in a FlateDecode stream (or
  uncompressed). Other filters and filter chains (LZW, ASCII85, ASCIIHex
  first), text drawn with font-specific encodings or hex strings, and text
  split across separate show operators are not decoded. Encrypted PDFs fail the
  scan (see above).
- PDF streams the scanner cannot place are skipped (LATER, #270 G2 L2): a
  `stream` keyword followed by a bare carriage return or a space (not
  conforming to the PDF standard), and the zip members of a file embedded in a
  PDF (an `/EmbeddedFile` such as an `.xlsx` attachment is inflated and
  scanned by text runs only). A FlateDecode declaration is also not seen when
  the `/Filter` is an indirect reference, when a NUL byte or a `%` comment
  sits between `/Filter` and `/FlateDecode`, or when a forged `stream` token
  inside the object's own dictionary cuts the look-back window short (#270 G2
  R2-H1). Such a stream is still decoded when it starts with a zlib header
  (as raw deflate if it will not decompress); without a zlib header it is
  skipped. Reason: each needs a real PDF object parser; the scanner stays a
  small standard-library tool.
- The FlateDecode check looks back from each stream to the previous stream's
  data, so a previous uncompressed stream that happens to contain the text
  `/Filter /FlateDecode` can make the next stream fail the scan (the safe
  direction).
- Packages embedded inside OLE2 files (legacy `.xls` or `.doc` objects, or an
  `oleObject.bin` in an Office zip) are scanned by text runs only; only a file
  or member named `.xls` has its numeric cells decoded.
- Phone separators are space, hyphen, dot (in the `+65` and international
  forms), no-break space, `&nbsp;` and en dash. Other dashes or spaces (em
  dash, figure or narrow no-break space), and two separators in a row, still
  split a number. Raw-byte text runs see the no-break space, and the en dash
  only in UTF-16. The phone patterns as they were before #270 (space, hyphen
  and dot only) also run on every line, so the wider separators never hide a
  number those patterns find.
- An international number directly followed by more digit groups with plain
  spaces, dots or hyphens (for example a date), so that the groups add up to
  more than 15 digits, is not reported (as before #270).
- Separators written other ways are not seen (LATER, #270 G2 L1): numeric
  character references (`&#160;`, `&#xa0;`, `&#8211;`), the `&ndash;` entity
  and the non-breaking hyphen U+2011 in text, and in raw bytes or PDF streams
  the UTF-8 no-break space (`C2 A0`) or the cp1252 en dash (`0x96`); raw-byte
  runs keep only the Latin-1 no-break space byte. Reason: each widens the
  phone patterns or the run decoder for a rare form; a number split this way
  is still read by a human reviewer.
- Office XML with a crafted unclosed `<w:rPr>`, `<w:rPrChange`, `<w:sdtPr>`,
  `<w:sdtEndPr>`, `<w:smartTagPr>`, `<w:customXmlPr>` or `<` makes the
  joined-text pass slow (quadratic: an unclosed `<` repeated takes about 15
  seconds at 100 KB and a minute at 200 KB, so minutes at 380 KB) (LATER, #270
  G2 L3, L8, R3-L1). It ends in a hook or CI timeout, which fails the check
  rather than passing it.
- A zip hidden inside a member's declared span (past the end of its
  compressed data, with the central-directory size inflated to cover it, or
  in an extra field) is neither expanded nor refused (LATER, #270 G2 L9).
  Reaching it needs data carving, the same class as any custom encoding.
- A text file without NUL bytes that starts with `%PDF` or holds a zip
  end-of-central-directory record has its PDF streams or appended zip scanned
  by the blob scan, while git's diff still decides which of its text lines are
  added (the blob scan's own line matching can miss a moved line). Such a
  file is sent to the PDF decoder only when `%PDF` is its first byte, not
  anywhere in the first 1024 bytes as for a zip member (LATER, #270 G2 R3-L2):
  a NUL-free PDF after a leading newline has only its text lines scanned, as
  before #270. Widening the window would send ordinary text that mentions
  `%PDF` near its top to the PDF decoder, which can fail the scan.
- A NUL-bearing file whose previous version had no NUL bytes counts every
  raw-byte text run as added, as before #270, even runs the previous version
  already held (the previous version is read as plain text, as it was then).
- Random binary data can contain the 4-byte zip end-of-central-directory
  record by chance; such a file fails the scan (the safe direction). The same
  holds for a 4-byte zip file header by chance in front of an appended zip.
- CI runs the scanner and allowlist from the pull request itself, so a pull
  request can weaken them in the same change. Once `pii-guard` is a required
  check (an owner decision, not made yet), CI should run the base branch's
  scanner and allowlist against the pull-request range instead.

Known false positives and deferred items:

- Hex SHAs and other hex tokens: a token that starts with eight digits, the
  first 6, 8 or 9, followed by a non-digit matches the bare 8-digit form (about
  1 in 400 random SHAs). A token that starts with `65`, then 3, 6, 8 or 9, then
  seven more digits and a non-digit matches the `65XXXXXXXX` storage form
  (about 1 in 70,000). Shorten the SHA or add `pii-guard: allow (commit SHA)`.
- Deferred (from the #263 review, not built): scanning pushes to non-main
  branches before a pull request exists (F4); replacing three `phone=` test
  values in `member_gateway/tests` with obvious placeholders (F6); detecting
  URL-encoded `%40` addresses and counting added opt-out markers (F7).

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
