"""Personal-data guard: flag likely personal data in lines a change adds.

This repository is public, so an email address or phone number that reaches any
pushed commit is published, even if a later commit removes it. The guard scans
only the lines a change ADDS and reports email addresses and phone numbers that
are not covered by the tracked allowlist (``config/pii_guard_allowlist.json``) or
by an explicit per-line opt-out marker.

Modes (exactly one):

* ``--staged`` - the staged additions of the next commit (used by the tracked
  pre-commit hook in ``.githooks/pre-commit``; also runnable by hand).
* ``--range BASE..HEAD`` - every commit in the range, one commit at a time, so a
  value added and later removed inside a pull request is still caught. Merge
  commits contribute only the lines that are new relative to every parent. Each
  commit's message is scanned too; author and committer identities are not.
* ``--tree REV`` - every line of every text file at a revision (an audit of
  existing content, normally with ``--report-only``).

Diffs are forced to text (``--text``), so a ``-diff`` or ``binary`` attribute cannot
hide added lines. No blob git cannot diff as text is skipped whole: printable
ASCII, UTF-16LE and UTF-16BE runs of its raw bytes are always scanned;
BOM-marked Unicode text (UTF-8, UTF-16 or UTF-32) is also decoded line by line;
any zip, recognised by its signature or by ``ZIP_EXTENSIONS`` (such as .xlsx), has
every member expanded and scanned, nested zips included up to ``ZIP_MAX_DEPTH``
levels within one shared ``ZIP_MAX_BYTES`` budget; a corrupt, encrypted,
over-budget or too deeply nested one fails closed, and so does a second zip in
the same bytes. A zip appended to a reviewed binary or to text (found by its
end-of-central-directory record) is expanded the same way. Office XML parts are
also scanned with their text runs joined. A PDF's FlateDecode streams are
decompressed (stdlib ``zlib``) within the same budget and scanned by text runs; a
stream declared FlateDecode that is not zlib data or will not decompress, and an
encrypted PDF, fail closed. A legacy ``.xls`` workbook has
its numeric cells decoded; an unreadable or encrypted one fails closed. A reviewed
binary extension must match its file signature. gzip, 7z, RAR and tar archives
fail closed at any depth. Any other NUL-bearing blob fails closed unless its
extension is on the reviewed ``BINARY_EXTENSIONS`` list. Known limits are listed
in ``docs/data_policy.md``. Hits inside binary content are reported as
``path (text runs):N``, ``path (deflated streams):N``, ``path (numeric cells):N``,
``container!member:N`` or ``container!member (joined text):N``.

Exit codes: 0 clean, 1 likely personal data found, 2 error. Every error fails
closed: an unreadable allowlist, a missing or empty range, an unknown revision,
a failed git command, an unparseable diff or an undecodable NUL-bearing file all
exit 2, never 0.

Matched values are redacted in the output unless ``--show-values`` is given, so
a CI log never republishes the value it caught. ``--report-only`` prints counts
per path without values and exits 0 when the scan itself succeeded.

Opt-out marker (same line, reason required)::

    pii-guard: allow (published company main line)

The scanner uses the Python standard library only and performs no network action.
"""

import argparse
import bisect
import codecs
import collections
import difflib
import fnmatch
import html
import io
import json
import math
import re
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO_ROOT / "config" / "pii_guard_allowlist.json"

EXIT_CLEAN = 0
EXIT_FOUND = 1
EXIT_ERROR = 2

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9_%+-](?:[A-Za-z0-9._%+-]*[A-Za-z0-9_%+-])?)"
    r"@"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63})"
    r"(?![A-Za-z0-9-])"
)
# Phone separators: space, hyphen, no-break space (raw or as &nbsp;) and en dash;
# the dotted form also takes a dot.
_SEP = r"(?:[ \-\u00a0\u2013]|&nbsp;)"
_SEP_DOT = r"(?:[ .\-\u00a0\u2013]|&nbsp;)"
# Singapore numbers with the +65 (or (65)/(+65)) prefix: 8 digits starting 3, 6, 8 or 9.
SG_PREFIXED_RE = re.compile(r"(?<![\w+])(?:\+65|\(\+?65\))" + _SEP_DOT + r"?[3689]\d{3}" + _SEP_DOT + r"?\d{4}(?!\d)")
# Singapore numbers with the country code but no "+", the member-intake storage form
# (65XXXXXXXX), including inside wa.me links.
SG_COUNTRY_CODE_RE = re.compile(r"(?<![\w.+])65" + _SEP + r"?[3689]\d{3}" + _SEP + r"?\d{4}(?!\d|\.\d)")
# Bare Singapore-style 8-digit numbers starting 6, 8 or 9, optionally split 4-4.
SG_LOCAL_RE = re.compile(r"(?<![\w.])[689]\d{3}" + _SEP + r"?\d{4}(?!\d|\.\d)")
# Other international numbers written with a leading +: 8 to 15 digits in total.
INTL_RE = re.compile(r"(?<![\w+])\+[1-9](?:" + _SEP_DOT + r"?\(?\d+\)?){2,7}")
PHONE_PATTERNS = (
    ("sg_phone", SG_PREFIXED_RE),
    ("sg_phone", SG_COUNTRY_CODE_RE),
    ("sg_phone_local", SG_LOCAL_RE),
    ("phone", INTL_RE),
)
# Base floor (#270): the phone patterns as they were before #270 (space, hyphen and
# dot separators only) run as a second pass, so a wider separator can never leave a
# line weaker than before. Without it, a match the wider separators lengthen past 15
# digits is dropped whole, and a new match the allowlist covers hides the overlapping
# number the narrower patterns find.
FLOOR_PHONE_PATTERNS = (
    ("sg_phone", re.compile(r"(?<![\w+])(?:\+65|\(\+?65\))[ .-]?[3689]\d{3}[ .-]?\d{4}(?!\d)")),
    ("sg_phone", re.compile(r"(?<![\w.+])65[ -]?[3689]\d{3}[ -]?\d{4}(?!\d|\.\d)")),
    ("sg_phone_local", re.compile(r"(?<![\w.])[689]\d{3}[ -]?\d{4}(?!\d|\.\d)")),
    ("phone", re.compile(r"(?<![\w+])\+[1-9](?:[ .-]?\(?\d+\)?){2,7}")),
)
ALLOW_MARKER_RE = re.compile(r"pii-guard:\s*allow\s*\(\s*[^)\s][^)]*\)")

ALLOWLIST_KEYS = {"emails", "phones"}
ENTRY_KEYS = {"emails": {"pattern", "reason"}, "phones": {"digits", "reason"}}

# Lines git prints between "diff" and the first hunk of a file section.
_HEADER_PREFIXES = (
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "--- ",
    "+++ ",
)
_HUNK_RE = re.compile(r"^(@{2,}) (?:-\d+(?:,\d+)? )+\+(\d+)(?:,\d+)? \1(?: |$)")

# Neutralise user configuration that changes diff output shape.
_GIT_BASE = [
    "git",
    "-c", "core.quotepath=off",
    "-c", "color.ui=never",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.external=",
]
_DIFF_OPTS = [
    "-U0",
    "--text",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]

# Blobs git cannot diff as text are never skipped whole: their printable ASCII and
# UTF-16 runs are always scanned. Zip containers also have every member expanded and
# scanned (corrupt or encrypted fails closed). Other NUL-bearing blobs must be
# BOM-marked Unicode text or a reviewed binary type, or they fail closed. The binary
# list only means "expected to be binary"; it never means "not scanned".
ZIP_EXTENSIONS = frozenset({".zip", ".xlsx", ".docx", ".pptx"})
# A reviewed binary extension must start with its file signature, so a renamed
# archive (zip, gzip, 7z and so on) cannot pass as an image, PDF or OLE2 file.
BINARY_MAGIC = {
    ".png": b"\x89PNG",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".gif": b"GIF8",
    ".ico": b"\x00\x00\x01\x00",
    ".webp": b"RIFF",
    ".pdf": b"%PDF",
    ".xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    ".doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
}
BINARY_EXTENSIONS = frozenset(BINARY_MAGIC)
# One shared expansion budget per blob: zip members and decompressed PDF streams.
ZIP_MAX_BYTES = 64 * 1024 * 1024
ZIP_MAX_DEPTH = 3
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")
# A zip appended to another file (a polyglot) is found by its end-of-central-directory record.
_ZIP_EOCD = b"PK\x05\x06"
_ZIP_LOCAL = b"PK\x03\x04"
_OLE2_MAGIC = BINARY_MAGIC[".xls"]
_PDF_MAGIC = BINARY_MAGIC[".pdf"]
# (signature, offset, label) for archives the scanner cannot open; they fail closed.
_OTHER_ARCHIVES = (
    (b"\x1f\x8b", 0, "gzip"),
    (b"7z\xbc\xaf\x27\x1c", 0, "7z"),
    (b"Rar!\x1a\x07", 0, "RAR"),
    (b"ustar", 257, "tar"),
)
RUNS_SUFFIX = " (text runs)"
DEFLATED_SUFFIX = " (deflated streams)"
CELLS_SUFFIX = " (numeric cells)"
JOINED_SUFFIX = " (joined text)"
# Runs keep the no-break space (Latin-1 byte, and in UTF-16) and the UTF-16 en dash,
# so phone numbers split by those separators stay whole.
_ASCII_RUN = re.compile(rb"[\x20-\x7e\t\xa0]{6,}")
_UTF16LE_RUN = re.compile(rb"(?:[\x20-\x7e\xa0]\x00|\x13\x20){6,}")
_UTF16BE_RUN = re.compile(rb"(?:\x00[\x20-\x7e\xa0]|\x20\x13){6,}")
# Base floor (#270): the runs as they were cut before #270 are kept too, so a wider run
# cannot join a hit to an opt-out marker or lengthen a number past the digit limit.
_FLOOR_RUNS = (
    (re.compile(rb"[\x20-\x7e\t]{6,}"), "ascii"),
    (re.compile(rb"(?:[\x20-\x7e]\x00){6,}"), "utf-16-le"),
    (re.compile(rb"(?:\x00[\x20-\x7e]){6,}"), "utf-16-be"),
)
# PDF: a stream keyword, the object header before it, and a FlateDecode first filter.
_PDF_STREAM = re.compile(rb"(?<![A-Za-z])stream(?:\r\n|\n)")
_PDF_FLATE = re.compile(rb"/Filter\s*(?:\[\s*)?/FlateDecode(?![A-Za-z0-9])")
# An encryption dictionary (named in the trailer) means every stream is ciphertext.
_PDF_ENCRYPT = re.compile(rb"/Encrypt(?![A-Za-z0-9])")
# A name may spell any byte as #xx (so /Encr#79pt is /Encrypt).
_PDF_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_INFLATE_CHUNK = 4096
# OLE2 (compound file) layout constants and the BIFF records that hold numbers.
_CFB_END = 0xFFFFFFFE
_CFB_MINI_CUTOFF = 4096
_BIFF_BOF = b"\x09\x08"
_BIFF_FILEPASS = 0x002F
_BIFF_NUMBER = 0x0203
_BIFF_RK = 0x027E
_BIFF_MULRK = 0x00BD
_BIFF_FORMULA = 0x0006
# Office XML: run properties and the properties of the inline wrappers (content
# controls, smart tags, custom XML) hold no text; joining runs (r, t, the markers Word
# drops between them and the inline wrappers around runs) rebuilds text split across
# runs. A no-break hyphen reads as a hyphen and a soft hyphen as nothing. Every other
# tag is a break. A tracked property change (rPrChange, which holds its own rPr) is
# stripped first so the outer rPr strip does not stop at the inner closing tag.
_XML_RUN_PROPS_CHANGE = re.compile(r"<((?:[\w.-]+:)?rPrChange)\b[^>]*?(?:/>|>.*?</\1>)", re.S)
_XML_RUN_PROPS = re.compile(r"<((?:[\w.-]+:)?(?:rPr|sdtPr|sdtEndPr|smartTagPr|customXmlPr))\b[^>]*?(?:/>|>.*?</\1>)", re.S)
_XML_TAG = re.compile(r"<(?:/?(?:[\w.-]+:)?([\w.-]+)[^>]*|[!?][^>]*)>")
_XML_INLINE = frozenset(
    {
        "r", "t", "proofErr", "bookmarkStart", "bookmarkEnd", "lastRenderedPageBreak",
        "hyperlink", "ins", "smartTag", "sdt", "sdtContent", "fldSimple", "customXml",
        "commentRangeStart", "commentRangeEnd", "moveTo", "moveToRangeStart", "moveToRangeEnd",
        "softHyphen",
    }
)
_XML_INLINE_TEXT = {"noBreakHyphen": "-"}
# Longest BOM first: the UTF-32-LE BOM starts with the UTF-16-LE BOM.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
METADATA_PATH = "(commit message)"


class GuardError(Exception):
    """Any condition that must fail the guard closed (exit 2)."""


Finding = collections.namedtuple("Finding", "commit path line kind value")


# --------------------------------------------------------------------------- allowlist


def load_allowlist(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError("cannot read allowlist %s: %s" % (path, exc))
    if not isinstance(data, dict) or set(data) != ALLOWLIST_KEYS:
        raise GuardError("allowlist must be an object with exactly the keys %s" % sorted(ALLOWLIST_KEYS))
    for key in sorted(ALLOWLIST_KEYS):
        entries = data[key]
        if not isinstance(entries, list):
            raise GuardError("allowlist %r must be a list" % key)
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS[key]:
                raise GuardError("allowlist %r entries need exactly %s" % (key, sorted(ENTRY_KEYS[key])))
            for field in ENTRY_KEYS[key]:
                if not isinstance(entry[field], str) or not entry[field].strip():
                    raise GuardError("allowlist %r entry field %r must be a non-empty string" % (key, field))
            if key == "phones" and not entry["digits"].isdigit():
                raise GuardError("allowlist phone digits must be digits only")
    return {
        "emails": [entry["pattern"].lower() for entry in data["emails"]],
        "phones": {entry["digits"] for entry in data["phones"]},
    }


def _email_allowed(address, allowlist):
    lowered = address.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in allowlist["emails"])


def _phone_digits(kind, value):
    digits = re.sub(r"\D", "", value)
    if kind == "sg_phone_local":
        return "65" + digits
    return digits


def _phone_allowed(kind, value, allowlist):
    return _phone_digits(kind, value) in allowlist["phones"]


# --------------------------------------------------------------------------- matching


def scan_line(text, allowlist):
    """Return [(kind, value)] for likely personal data on one added line.

    The phone patterns run twice: ``PHONE_PATTERNS``, then ``FLOOR_PHONE_PATTERNS``.
    A floor hit is added unless a reported hit of the first pass already covers it.
    """
    if ALLOW_MARKER_RE.search(text):
        return []
    hits = _line_hits(text, PHONE_PATTERNS, allowlist)
    covered = [(start, end) for start, end, _, _ in hits]
    for start, end, kind, value in _line_hits(text, FLOOR_PHONE_PATTERNS, allowlist):
        if not any(left <= start and end <= right for left, right in covered):
            hits.append((start, end, kind, value))
    hits.sort(key=lambda hit: hit[0])
    return [(kind, value) for _, _, kind, value in hits]


def _line_hits(text, phone_patterns, allowlist):
    """Return [(start, end, kind, value)] for one pass of the email and phone patterns."""
    spans = []
    for match in EMAIL_RE.finditer(text):
        spans.append((match.start(), match.end(), "email", match.group(0)))
    for kind, regex in phone_patterns:
        for match in regex.finditer(text):
            value = match.group(0)
            if kind == "phone" and not 8 <= len(re.sub(r"\D", "", value)) <= 15:
                continue
            spans.append((match.start(), match.end(), kind, value))
    # Keep the first, longest match where matches overlap (an email wins over digits inside it).
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    hits, last_end = [], -1
    for start, end, kind, value in spans:
        if start < last_end:
            continue
        last_end = end
        if kind == "email":
            if _email_allowed(value, allowlist):
                continue
        elif _phone_allowed(kind, value, allowlist):
            continue
        hits.append((start, end, kind, value))
    return hits


# --------------------------------------------------------------------------- diff parsing


def _strip_path(raw):
    raw = raw.rstrip("\t")
    if raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if raw.startswith("b/"):
        return raw[2:]
    raise GuardError("unexpected diff target path %r" % raw)


def iter_added_lines(diff_text, blob_scanned=()):
    """Yield (path, new_line_number, text) for every added line of a git patch.

    Handles ordinary patches and combined (``--cc``) merge diffs. For a merge, a
    line counts as added only when it is new relative to every parent. Anything
    the parser does not recognise raises GuardError so the guard fails closed.
    ``git show --cc`` ignores ``--text``, so a combined section may summarise a
    blob as "Binary files ... differ"; that is accepted only for a path in
    ``blob_scanned``, whose content the blob scanner has already covered.
    """
    path = None
    cc_path = None
    in_section = False
    hunk_parents = 0
    line_no = 0
    for raw in diff_text.split("\n"):
        if raw.startswith("diff --git ") or raw.startswith("diff --cc ") or raw.startswith("diff --combined "):
            in_section, hunk_parents, path = True, 0, None
            cc_path = raw.split(" ", 2)[2] if raw.startswith("diff --cc ") else None
            continue
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if not in_section or not match:
                raise GuardError("unparseable hunk header %r" % raw[:80])
            hunk_parents = len(match.group(1)) - 1
            line_no = int(match.group(2))
            continue
        if hunk_parents:
            if raw.startswith("\\"):
                continue
            if raw == "":
                # Only the trailing newline of the whole output may be empty.
                continue
            prefix = raw[:hunk_parents]
            if len(prefix) != hunk_parents or set(prefix) - set(" +-"):
                raise GuardError("unparseable diff line in %s" % path)
            if "-" in prefix:
                continue
            if prefix == "+" * hunk_parents:
                if path is None:
                    raise GuardError("added line without a target path")
                yield path, line_no, raw[hunk_parents:]
            line_no += 1
            continue
        if raw == "":
            continue
        if not in_section:
            raise GuardError("unexpected diff output %r" % raw[:80])
        if raw.startswith("+++ "):
            path = _strip_path(raw[4:])
            continue
        if raw.startswith(_HEADER_PREFIXES):
            continue
        if raw.startswith("Binary files ") and cc_path is not None and cc_path in blob_scanned:
            continue
        raise GuardError("unexpected diff header %r" % raw[:80])


# --------------------------------------------------------------------------- git


def _git(args, cwd, input_bytes=None):
    try:
        proc = subprocess.run(
            _GIT_BASE + list(args),
            cwd=str(cwd),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise GuardError("cannot run git: %s" % exc)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GuardError("git %s failed: %s" % (args[0], detail[-1] if detail else "exit %d" % proc.returncode))
    return proc.stdout


def _text(data):
    return data.decode("utf-8", "replace")


def _scan_patch(diff_text, commit, allowlist, skip_paths=()):
    findings = []
    for path, line_no, text in iter_added_lines(diff_text, skip_paths):
        if path in skip_paths:
            continue
        for kind, value in scan_line(text, allowlist):
            findings.append(Finding(commit, path, line_no, kind, value))
    return findings


def _read_blobs(shas, cwd):
    """Return {sha: bytes} for the given blob ids via one ``git cat-file --batch``."""
    wanted = sorted(set(shas))
    if not wanted:
        return {}
    out = _git(["cat-file", "--batch"], cwd, "".join(sha + "\n" for sha in wanted).encode("ascii"))
    blobs, offset = {}, 0
    for sha in wanted:
        header_end = out.index(b"\n", offset)
        parts = out[offset:header_end].decode("ascii", "replace").split(" ")
        if len(parts) != 3 or parts[0] != sha or parts[1] != "blob":
            raise GuardError("unexpected cat-file record for %s" % sha)
        size = int(parts[2])
        blobs[sha] = out[header_end + 1 : header_end + 1 + size]
        offset = header_end + 1 + size + 1
    return blobs


def _printable_runs(data):
    """Printable ASCII (plus no-break space), UTF-16LE and UTF-16BE runs of 6+ characters in raw bytes.

    Floor runs (``_FLOOR_RUNS``) that differ from every wider run are appended.
    """
    runs = [match.group(0).decode("latin-1") for match in _ASCII_RUN.finditer(data)]
    runs.extend(match.group(0).decode("utf-16-le") for match in _UTF16LE_RUN.finditer(data))
    runs.extend(match.group(0).decode("utf-16-be") for match in _UTF16BE_RUN.finditer(data))
    seen = set(runs)
    for regex, encoding in _FLOOR_RUNS:
        for match in regex.finditer(data):
            run = match.group(0).decode(encoding)
            if run not in seen:
                seen.add(run)
                runs.append(run)
    return runs


def _bom_text(data):
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, "replace")
    return None


def _is_zip(data):
    return data.startswith(_ZIP_SIGNATURES)


def _refuse_other_archives(path, data):
    """Compressed archives the scanner cannot open fail closed at any depth."""
    for signature, offset, label in _OTHER_ARCHIVES:
        if data[offset : offset + len(signature)] == signature:
            raise GuardError("%s is a %s archive, which the scanner cannot open" % (path, label))


def _zlib_header(data, at):
    head = data[at : at + 2]
    return len(head) == 2 and head[0] & 0x0F == 8 and head[0] >> 4 <= 7 and (head[0] << 8 | head[1]) % 31 == 0


def _pdf_stream_runs(path, data, budget):
    """Printable runs of every zlib-compressed (FlateDecode) stream in a PDF.

    An encrypted PDF (one that names ``/Encrypt``) fails closed. Every stream whose
    data starts with a zlib header is inflated until the zlib data ends (so a forged
    ``endstream`` inside it cannot cut it short), within the shared ``budget``.
    A stream whose object names FlateDecode as its first filter fails closed when
    its data has no zlib header or will not decompress; an undeclared one that will
    not decompress (a bad checksum, say) is retried as raw deflate after its 2-byte
    header and whatever that yields is scanned; other data that is not zlib is
    skipped. Both name checks also try a copy with ``#xx`` name escapes decoded. Exceeding
    the budget fails closed.
    """
    if _pdf_names_match(_PDF_ENCRYPT, data):
        raise GuardError("%s is an encrypted PDF and cannot be scanned" % path)
    runs, previous = [], 0
    for match in _PDF_STREAM.finditer(data):
        # The window runs from the previous stream's data, so it normally holds this
        # object's header (over-inclusive, the safe direction). A forged "stream"
        # token inside the object's own dictionary can cut it short (Known limits L2).
        start, declared_from, previous = match.end(), previous, match.end()
        declared = _pdf_names_match(_PDF_FLATE, data[declared_from : match.start()])
        if not _zlib_header(data, start):
            if declared:
                raise GuardError("%s has a FlateDecode stream that is not zlib data and cannot be scanned" % path)
            continue
        pieces = []
        try:
            _inflate(path, data, start, zlib.MAX_WBITS, budget, pieces)
        except zlib.error:
            if declared:
                raise GuardError("%s has a corrupt FlateDecode stream and cannot be scanned" % path)
            pieces = []
            try:
                _inflate(path, data, start + 2, -zlib.MAX_WBITS, budget, pieces)
            except zlib.error:
                pass  # best effort for undeclared data: scan what was recovered
        runs.extend(_printable_runs(b"".join(pieces)))
    return runs


def _inflate(path, data, at, wbits, budget, pieces):
    """Inflate ``data`` from ``at`` into ``pieces`` until the stream ends, within ``budget``.

    ``zlib.error`` propagates; ``pieces`` keeps the output produced before it.
    """
    inflater = zlib.decompressobj(wbits)
    for offset in range(at, len(data), _INFLATE_CHUNK):
        piece = inflater.decompress(data[offset : offset + _INFLATE_CHUNK])
        budget[0] -= len(piece)
        if budget[0] < 0:
            raise GuardError("%s expands beyond %d bytes and cannot be scanned" % (path, ZIP_MAX_BYTES))
        pieces.append(piece)
        if inflater.eof:
            break


def _pdf_names_match(regex, data):
    """True when ``regex`` matches PDF bytes as written or with ``#xx`` name escapes decoded."""
    if regex.search(data):
        return True
    return b"#" in data and regex.search(_PDF_NAME_ESCAPE.sub(lambda match: bytes([int(match.group(1), 16)]), data)) is not None


def _cfb_streams(path, data, names):
    """Bytes of every stream named in ``names`` (lower case) in an OLE2 compound file.

    A minimal reader: header, FAT (with DIFAT sectors), directory and mini stream.
    Anything malformed (sector size, mini stream cutoff, a chain out of range or
    looping, a stream shorter than its size) fails closed.
    """

    def broken(reason):
        return GuardError("%s is not a readable OLE2 file (%s)" % (path, reason))

    if len(data) < 512:
        raise broken("short header")
    sector_shift, mini_shift = struct.unpack_from("<HH", data, 0x1E)
    if (sector_shift, mini_shift) not in ((9, 6), (12, 6)):
        raise broken("sector size")
    size = 1 << sector_shift
    count = len(data) // size - 1
    fat_count, dir_start = struct.unpack_from("<II", data, 0x2C)
    cutoff, minifat_start, _, difat_start, difat_count = struct.unpack_from("<5I", data, 0x38)
    if cutoff != _CFB_MINI_CUTOFF:
        raise broken("mini stream cutoff")
    if difat_count > count or fat_count > count:
        raise broken("FAT size")

    def sector(number):
        if number >= count:
            raise broken("sector out of range")
        return data[(number + 1) * size : (number + 2) * size]

    def chain(start, table):
        numbers = []
        while start != _CFB_END:
            if start >= len(table) or len(numbers) >= len(table):
                raise broken("sector chain")
            numbers.append(start)
            start = table[start]
        return numbers

    def table(sectors):
        raw = b"".join(sector(number) for number in sectors)
        return struct.unpack("<%dI" % (len(raw) // 4), raw)

    def read(start, length, chain_table, unit):
        raw = b"".join(unit(number) for number in chain(start, chain_table))
        if length is None:
            return raw
        if len(raw) < length:
            raise broken("short stream")
        return raw[:length]

    difat = list(struct.unpack_from("<109I", data, 0x4C))
    following = difat_start
    for _ in range(difat_count):
        entries = table([following])
        difat.extend(entries[:-1])
        following = entries[-1]
    if fat_count > len(difat):
        raise broken("FAT size")
    fat = table(difat[:fat_count])
    directory = read(dir_start, None, fat, sector)
    entries = []
    for at in range(0, len(directory) - 127, 128):
        name_length, kind = struct.unpack_from("<HB", directory, at + 64)
        start, length = struct.unpack_from("<IQ", directory, at + 116)
        if size == 512:
            length &= 0xFFFFFFFF
        name = directory[at : at + min(name_length, 64)].decode("utf-16-le", "replace").rstrip("\0")
        entries.append((name, kind, start, length))
    if not entries or entries[0][1] != 5:
        raise broken("no root entry")
    mini = minifat = None
    streams = []
    for name, kind, start, length in entries:
        if kind != 2 or name.lower() not in names:
            continue
        if length >= _CFB_MINI_CUTOFF:
            streams.append(read(start, length, fat, sector))
            continue
        if mini is None:
            mini = read(entries[0][2], entries[0][3], fat, sector)
            minifat = table(chain(minifat_start, fat))
        streams.append(read(start, length, minifat, lambda number: mini[number * 64 : (number + 1) * 64]))
    return streams


def _rk_value(rk):
    """Decode a BIFF RK number: a 30-bit integer or the high half of a double, optionally /100."""
    if rk & 2:
        value = (rk >> 2) - ((1 << 30) if rk & 0x80000000 else 0)
    else:
        value = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))[0]
    return value / 100 if rk & 1 else value


def _number_text(value):
    if math.isfinite(value) and value == int(value):
        return "%d" % value
    return repr(value)


def _xls_numeric_cells(path, data):
    """Numeric cell values of a legacy .xls workbook as text, one per line.

    Numbers (NUMBER, RK, MULRK and cached numeric FORMULA results) are stored as
    binary values, so text runs never see a phone number typed into a number
    cell. A file without a BIFF workbook stream, an encrypted workbook or a
    truncated record fails closed.
    """
    streams = _cfb_streams(path, data, {"workbook", "book"})
    if not streams:
        raise GuardError("%s has no workbook stream and cannot be scanned" % path)
    values = []
    for stream in streams:
        if not stream.startswith(_BIFF_BOF):
            raise GuardError("%s has no BIFF workbook stream and cannot be scanned" % path)
        at = 0
        while at + 4 <= len(stream):
            kind, length = struct.unpack_from("<HH", stream, at)
            body = stream[at + 4 : at + 4 + length]
            if len(body) != length:
                raise GuardError("%s has a truncated workbook record and cannot be scanned" % path)
            at += 4 + length
            if kind == _BIFF_FILEPASS:
                raise GuardError("%s is an encrypted workbook and cannot be scanned" % path)
            if kind == _BIFF_NUMBER and length >= 14:
                values.append(struct.unpack_from("<d", body, 6)[0])
            elif kind == _BIFF_FORMULA and length >= 14 and body[12:14] != b"\xff\xff":
                values.append(struct.unpack_from("<d", body, 6)[0])
            elif kind == _BIFF_RK and length >= 10:
                values.append(_rk_value(struct.unpack_from("<I", body, 6)[0]))
            elif kind == _BIFF_MULRK:
                values.extend(_rk_value(struct.unpack_from("<I", body, offset)[0]) for offset in range(6, length - 5, 6))
    return [_number_text(value) for value in values]


def _xml_tag_text(match):
    name = match.group(1)
    if name in _XML_INLINE:
        return ""
    return _XML_INLINE_TEXT.get(name, "\n")


def _xml_joined_lines(text):
    """Text of an Office XML part with its runs joined, so text split across runs reads whole."""
    text = _XML_RUN_PROPS.sub("", _XML_RUN_PROPS_CHANGE.sub("", text))
    joined = _XML_TAG.sub(_xml_tag_text, text)
    return html.unescape(joined).splitlines()


def _format_documents(name, data, budget):
    """Extra documents for formats whose raw bytes hide text: PDF streams and .xls numbers."""
    docs = []
    # PDF readers accept a header within the first 1024 bytes (#270 G2 L6).
    if _PDF_MAGIC in data[:1024]:
        docs.append((name + DEFLATED_SUFFIX, _pdf_stream_runs(name, data, budget)))
    if data.startswith(_OLE2_MAGIC) and Path(name).suffix.lower() == ".xls":
        docs.append((name + CELLS_SUFFIX, _xls_numeric_cells(name, data)))
    return docs


def _member_documents(name, data, budget, depth):
    """Documents for one archive member: nested or appended zips recurse, other bytes are scanned in place."""
    # Refuse first, as blob_documents does, so an appended zip cannot take a gzip, 7z,
    # RAR or tar member past the refusal (#270 G2 W1).
    _refuse_other_archives(name, data)
    nul_free = b"\0" not in data
    zipped = _is_zip(data) or _ZIP_EOCD in data
    docs = []
    # The decoded text is kept even when a zip follows it, as base kept BOM text (#270 G2 W3).
    text = _text(data) if nul_free else _bom_text(data)
    if text is not None:
        docs.append((name, text.splitlines()))
        if nul_free and name.lower().endswith(".xml"):
            docs.append((name + JOINED_SUFFIX, _xml_joined_lines(text)))
    if zipped:
        docs.extend(_zip_documents(name, data, budget, depth + 1))
    docs.extend(_format_documents(name, data, budget))
    if zipped or not nul_free:
        docs.append((name + RUNS_SUFFIX, _printable_runs(data)))
    return docs


def _zip_documents(path, data, budget, depth):
    """Text documents for every member of a zip container, recursing into nested zips.

    ``budget`` is a one-item list holding the bytes still allowed across the whole
    blob, nested containers included. A corrupt, encrypted, over-budget or too
    deeply nested container fails closed.
    """
    if depth > ZIP_MAX_DEPTH:
        raise GuardError("%s nests zip containers deeper than %d levels" % (path, ZIP_MAX_DEPTH))
    docs = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _refuse_hidden_zips(path, data, archive.infolist())
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    raise GuardError("%s member %s is encrypted and cannot be scanned" % (path, _safe_name(info.filename)))
                with archive.open(info) as handle:
                    member = handle.read(budget[0] + 1)
                if len(member) > budget[0]:
                    raise GuardError("%s expands beyond %d bytes and cannot be scanned" % (path, ZIP_MAX_BYTES))
                budget[0] -= len(member)
                docs.extend(_member_documents("%s!%s" % (path, _safe_name(info.filename)), member, budget, depth))
    except GuardError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, OSError, ValueError, zlib.error) as exc:
        raise GuardError("%s is not a readable zip container: %s" % (path, exc.__class__.__name__))
    return docs


def _refuse_hidden_zips(path, data, infos):
    """Fail closed when ``data`` holds zip records that ``zipfile`` would not read.

    ``zipfile`` reads only the last end-of-central-directory record, so a second
    zip in front of the parsed one (or hidden between its members) would be
    ignored. Every local file header must start a parsed member, and only one
    end-of-central-directory record may lie outside the members' data (records
    inside a stored member are scanned when that member is expanded).
    """
    spans = []
    for info in infos:
        head = data[info.header_offset : info.header_offset + 30]
        if len(head) < 30 or not head.startswith(_ZIP_LOCAL):
            raise GuardError("%s is not a readable zip container: bad member header" % path)
        name_length, extra_length = struct.unpack_from("<HH", head, 26)
        spans.append((info.header_offset, info.header_offset + 30 + name_length + extra_length + info.compress_size))
    spans.sort()
    starts, reach, furthest = [start for start, _ in spans], [], -1
    for _, end in spans:
        furthest = max(furthest, end)
        reach.append(furthest)

    def outside(offset):
        index = bisect.bisect_right(starts, offset) - 1
        return index < 0 or offset >= reach[index]

    if any(outside(match.start()) for match in re.finditer(re.escape(_ZIP_LOCAL), data)):
        raise GuardError("%s has a zip file header outside its parsed members (a second zip) and cannot be scanned" % path)
    if sum(1 for match in re.finditer(re.escape(_ZIP_EOCD), data) if outside(match.start())) > 1:
        raise GuardError("%s has more than one zip end-of-central-directory record and cannot be scanned" % path)


def _safe_name(name):
    """Member names are display-only; control characters must not forge log lines."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", name)


def needs_blob_scan(path, data):
    return (
        b"\0" in data
        or _is_zip(data)
        or _ZIP_EOCD in data
        or data.startswith(_PDF_MAGIC)
        or Path(path).suffix.lower() in ZIP_EXTENSIONS
    )


def blob_documents(path, data):
    """Return [(display_path, lines)] to scan for a blob git cannot diff as text.

    No such blob is skipped whole. A reviewed binary extension must match its magic
    bytes. Printable ASCII and UTF-16 runs of the raw bytes are always scanned;
    BOM-marked Unicode text (and NUL-free content) is also decoded line by line;
    any zip (by signature or by extension) has every member expanded and scanned,
    nested zips included up to ``ZIP_MAX_DEPTH`` levels within one shared
    ``ZIP_MAX_BYTES`` budget, and so does a zip appended to a reviewed binary or to
    text (found by its end-of-central-directory record); a second zip in the same
    bytes fails closed. A PDF's FlateDecode streams and a legacy ``.xls``
    workbook's numeric cells are decoded too, in a zip member as well; an
    encrypted PDF fails closed. gzip,
    7z, RAR and tar archives, any other NUL-bearing blob that is neither
    BOM-marked text nor a reviewed binary type, and two documents with one
    display name, fail closed.
    """
    suffix = Path(path).suffix.lower()
    if suffix in BINARY_EXTENSIONS and not data.startswith(BINARY_MAGIC[suffix]):
        raise GuardError("%s does not start with the %s file signature" % (path, suffix))
    _refuse_other_archives(path, data)
    budget = [ZIP_MAX_BYTES]
    docs = []
    text = _bom_text(data)
    if text is None and b"\0" not in data:
        text = _text(data)
    if text is not None:
        docs.append((path, text.splitlines()))
    if suffix in ZIP_EXTENSIONS or _is_zip(data):
        docs.extend(_zip_documents(path, data, budget, 1))
    elif text is None and suffix not in BINARY_EXTENSIONS:
        raise GuardError("%s contains NUL bytes but is neither BOM-marked Unicode text nor a reviewed binary type" % path)
    elif _ZIP_EOCD in data:
        docs.extend(_zip_documents(path, data, budget, 1))
    docs.extend(_format_documents(path, data, budget))
    docs.append((path + RUNS_SUFFIX, _printable_runs(data)))
    _refuse_duplicate_names(path, docs)
    return docs


def _refuse_duplicate_names(path, docs):
    """Fail closed when two documents share a display name (#270 G2 W4).

    Added lines are matched per name, so a second document with the same name
    (a repeated member, a ``_safe_name`` collision, or a member named like another
    document's suffixed or ``!`` name) would replace the first one's line set.
    """
    seen = set()
    for name, _ in docs:
        if name in seen:
            raise GuardError("%s holds two documents named %s and cannot be scanned" % (path, name))
        seen.add(name)


def _parent_documents(path, data):
    if not needs_blob_scan(path, data):
        return {path: _text(data).splitlines()}
    try:
        docs = dict(blob_documents(path, data))
    except GuardError:
        return {}
    if _diff_keeps_text(path, data):
        # Base read this parent as plain text only, so its raw runs must not hide the
        # new blob's runs that base reports as added (#270 round 4 fuzz differential).
        docs.pop(path + RUNS_SUFFIX, None)
    return docs


def _scan_documents(label, docs, allowlist, keep=None):
    """Scan documents, reporting each (kind, value) once per blob; run hits come last."""
    findings, seen = [], set()
    for name, lines in docs:
        indices = range(len(lines)) if keep is None else sorted(keep.get(name, ()))
        for index in indices:
            for kind, value in scan_line(lines[index], allowlist):
                if (kind, value) in seen:
                    continue
                seen.add((kind, value))
                findings.append(Finding(label, name, index + 1, kind, value))
    return findings


def _blob_findings(changes, label, cwd, allowlist):
    """Scan changed blobs git cannot diff as text; return (handled_paths, findings).

    Their added lines are recomputed here, per document: lines of the new blob's
    document absent from the same document of every parent blob.
    """
    new_blobs = _read_blobs([sha for _, sha, _ in changes], cwd)
    flagged = [(path, sha, parents) for path, sha, parents in changes if needs_blob_scan(path, new_blobs[sha])]
    if not flagged:
        return set(), []
    old_blobs = _read_blobs([sha for _, _, parents in flagged for sha in parents if not _is_null_sha(sha)], cwd)
    handled, findings = set(), []
    for path, sha, parents in flagged:
        docs = blob_documents(path, new_blobs[sha])
        if _diff_keeps_text(path, new_blobs[sha]):
            docs = [doc for doc in docs if doc[0] != path]
        else:
            handled.add(path)
        parent_docs = [{} if _is_null_sha(parent) else _parent_documents(path, old_blobs[parent]) for parent in parents or ["0"]]
        keep = {}
        for name, lines in docs:
            added = set(range(len(lines)))
            for old in parent_docs:
                fresh = set()
                matcher = difflib.SequenceMatcher(None, old.get(name, []), lines, autojunk=False)
                for tag, _, _, j1, j2 in matcher.get_opcodes():
                    if tag in ("insert", "replace"):
                        fresh.update(range(j1, j2))
                added &= fresh
            keep[name] = added
        findings.extend(_scan_documents(label, docs, allowlist, keep))
    return handled, findings


def _diff_keeps_text(path, data):
    """True for a blob only the #270 checks send to the blob scan (NUL-free, not a zip by name or signature).

    git's diff still decides which of its text lines are added, as before #270: the
    blob scan's line matching can treat a moved line as unchanged where git reports it.
    """
    return b"\0" not in data and not _is_zip(data) and Path(path).suffix.lower() not in ZIP_EXTENSIONS


def _is_null_sha(sha):
    return set(sha) == {"0"}


def _raw_changes(raw):
    """Parse ``--raw -z`` output (plain or combined) into (path, new_sha, parent_shas)."""
    tokens = raw.decode("utf-8", "surrogateescape").split("\0")
    changes, index = [], 0
    while index < len(tokens):
        meta = tokens[index]
        if meta == "":
            index += 1
            continue
        colons = len(meta) - len(meta.lstrip(":"))
        fields = meta[colons:].split(" ")
        if colons < 1 or len(fields) != 2 * colons + 3:
            raise GuardError("unparseable raw diff record %r" % meta[:80])
        modes, shas, status = fields[: colons + 1], fields[colons + 1 : 2 * colons + 2], fields[-1]
        step = 3 if colons == 1 and status[:1] in ("R", "C") else 2
        path = tokens[index + step - 1] if index + step - 1 < len(tokens) else ""
        index += step
        if not path:
            raise GuardError("raw diff record without a path")
        if modes[-1] in ("000000", "120000", "160000") or _is_null_sha(shas[-1]):
            continue
        changes.append((path, shas[-1], shas[:-1]))
    return changes


def scan_staged(cwd, allowlist):
    raw = _git(["diff", "--cached", "--raw", "-z", "--no-abbrev", "--find-renames"], cwd)
    handled, encoded = _blob_findings(_raw_changes(raw), "staged", cwd, allowlist)
    diff = _git(["diff", "--cached"] + _DIFF_OPTS, cwd)
    return _scan_patch(_text(diff), "staged", allowlist, handled) + encoded


def _resolve_commit(rev, cwd):
    if rev.startswith("-") or any(ch.isspace() for ch in rev):
        raise GuardError("invalid revision %r" % rev)
    return _text(_git(["rev-parse", "--verify", "--quiet", rev + "^{commit}"], cwd)).strip()


def resolve_range(spec, cwd):
    if not isinstance(spec, str) or spec.count("..") != 1 or "..." in spec:
        raise GuardError("range must be BASE..HEAD")
    base, head = spec.split("..")
    if not base.strip() or not head.strip():
        raise GuardError("range is missing its base or head")
    base_sha = _resolve_commit(base, cwd)
    head_sha = _resolve_commit(head, cwd)
    commits = _text(_git(["rev-list", "--reverse", "--topo-order", head_sha, "^" + base_sha], cwd)).split()
    if not commits:
        raise GuardError("range %s contains no commits" % spec)
    return commits


def scan_commit_metadata(commit, cwd, allowlist):
    """Scan a commit's full message.

    Author and committer identities are deliberately not scanned: the owner
    accepts their own addresses on commits (decision of 2026-10-21), and a git
    identity is configured outside the change under review.
    """
    meta = _text(_git(["show", "-s", "--no-notes", "--format=%B", commit], cwd))
    findings = []
    for line_no, text in enumerate(meta.splitlines(), 1):
        for kind, value in scan_line(text, allowlist):
            findings.append(Finding(commit, METADATA_PATH, line_no, kind, value))
    return findings


def scan_range(spec, cwd, allowlist):
    findings = []
    for commit in resolve_range(spec, cwd):
        findings.extend(scan_commit_metadata(commit, cwd, allowlist))
        raw = _git(["diff-tree", "-r", "-z", "-c", "--raw", "--no-abbrev", "--no-commit-id", "--find-renames", "--root", commit], cwd)
        handled, encoded = _blob_findings(_raw_changes(raw), commit, cwd, allowlist)
        diff = _git(["show", "--format=", "--cc"] + _DIFF_OPTS + [commit], cwd)
        findings.extend(_scan_patch(_text(diff), commit, allowlist, handled))
        findings.extend(encoded)
    return findings


def scan_tree(rev, cwd, allowlist):
    if not rev or not rev.strip():
        raise GuardError("tree revision is empty")
    tree = _resolve_commit(rev, cwd)
    entries = []
    for record in _text(_git(["ls-tree", "-r", "-z", "--full-tree", tree], cwd)).split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, kind, sha = meta.split(" ")
        if kind == "blob" and mode != "120000":
            entries.append((path, sha))
    blobs = _read_blobs([sha for _, sha in entries], cwd)
    findings = []
    for path, sha in entries:
        body = blobs[sha]
        if needs_blob_scan(path, body):
            findings.extend(_scan_documents(tree, blob_documents(path, body), allowlist))
            continue
        for line_no, line in enumerate(_text(body).splitlines(), 1):
            for hit_kind, value in scan_line(line, allowlist):
                findings.append(Finding(tree, path, line_no, hit_kind, value))
    return findings


# --------------------------------------------------------------------------- CLI


def _print_findings(findings, show_values, out):
    for item in findings:
        where = "%s:%d" % (item.path, item.line)
        commit = item.commit if item.commit == "staged" else item.commit[:12]
        value = item.value if show_values else "[redacted]"
        out.write("pii-guard: %s %s %s %s\n" % (commit, where, item.kind, value))


def _print_report(findings, out):
    per_path = collections.Counter(item.path for item in findings)
    per_kind = collections.Counter(item.kind for item in findings)
    out.write("pii-guard report: %d hit(s) in %d file(s)\n" % (len(findings), len(per_path)))
    for kind in sorted(per_kind):
        out.write("  kind %s: %d\n" % (kind, per_kind[kind]))
    for path in sorted(per_path):
        out.write("  %d %s\n" % (per_path[path], path))


def main(argv=None, cwd=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(description="Flag likely personal data in added lines.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan the staged additions")
    mode.add_argument("--range", metavar="BASE..HEAD", help="scan every commit in BASE..HEAD")
    mode.add_argument("--tree", metavar="REV", help="scan every text file at REV")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST), help="allowlist JSON path")
    parser.add_argument("--show-values", action="store_true", help="print matched values (local use only)")
    parser.add_argument("--report-only", action="store_true", help="print counts per path and exit 0")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code else EXIT_CLEAN
    cwd = Path(cwd) if cwd else Path.cwd()
    try:
        allowlist = load_allowlist(args.allowlist)
        if args.staged:
            findings = scan_staged(cwd, allowlist)
        elif args.range is not None:
            findings = scan_range(args.range, cwd, allowlist)
        else:
            findings = scan_tree(args.tree, cwd, allowlist)
    except GuardError as exc:
        err.write("pii-guard: ERROR (failing closed): %s\n" % exc)
        return EXIT_ERROR
    if args.report_only:
        _print_report(findings, out)
        return EXIT_CLEAN
    if not findings:
        out.write("pii-guard: no likely personal data in added lines\n")
        return EXIT_CLEAN
    _print_findings(findings, args.show_values, out)
    err.write(
        "pii-guard: %d likely personal-data hit(s). Remove the value, use a reserved example "
        "(name@example.com), or add 'pii-guard: allow (<reason>)' on the line if it is justified. "
        "A hit in an earlier commit of a pull request needs that commit rewritten, not a follow-up fix, "
        "and a hit on a pushed branch is already public: report it to the repository owner as well.\n"
        % len(findings)
    )
    return EXIT_FOUND


if __name__ == "__main__":
    sys.exit(main())
