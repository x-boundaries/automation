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
  commit's author and committer name and email and its message are scanned too.
* ``--tree REV`` - every line of every text file at a revision (an audit of
  existing content, normally with ``--report-only``).

Diffs are forced to text (``--text``), so a ``-diff`` or ``binary`` attribute cannot
hide added lines. A blob containing NUL bytes is decoded when it is BOM-marked
Unicode text (UTF-8, UTF-16 or UTF-32); otherwise it fails closed unless its
extension is on the reviewed ``BINARY_EXTENSIONS`` list.

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
import codecs
import collections
import difflib
import fnmatch
import json
import re
import subprocess
import sys
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
# Singapore numbers with the +65 (or (65)/(+65)) prefix: 8 digits starting 3, 6, 8 or 9.
SG_PREFIXED_RE = re.compile(r"(?<![\w+])(?:\+65|\(\+?65\))[ .-]?[3689]\d{3}[ .-]?\d{4}(?!\d)")
# Bare Singapore-style 8-digit numbers starting 6, 8 or 9, optionally split 4-4.
SG_LOCAL_RE = re.compile(r"(?<![\w.])[689]\d{3}[ -]?\d{4}(?!\d|\.\d)")
# Other international numbers written with a leading +: 8 to 15 digits in total.
INTL_RE = re.compile(r"(?<![\w+])\+[1-9](?:[ .-]?\(?\d+\)?){2,7}")
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

# A blob with NUL bytes is scanned only when it is BOM-marked Unicode text; otherwise
# it fails closed unless its extension is on this reviewed binary list.
BINARY_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".xlsx", ".xls", ".docx", ".doc", ".pptx"}
)
# Longest BOM first: the UTF-32-LE BOM starts with the UTF-16-LE BOM.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
METADATA_PATH = "(commit author, committer and message)"


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
    """Return [(kind, value)] for likely personal data on one added line."""
    if ALLOW_MARKER_RE.search(text):
        return []
    spans = []
    for match in EMAIL_RE.finditer(text):
        spans.append((match.start(), match.end(), "email", match.group(0)))
    for kind, regex in (("sg_phone", SG_PREFIXED_RE), ("sg_phone_local", SG_LOCAL_RE), ("phone", INTL_RE)):
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
        hits.append((kind, value))
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


def iter_added_lines(diff_text):
    """Yield (path, new_line_number, text) for every added line of a git patch.

    Handles ordinary patches and combined (``--cc``) merge diffs. For a merge, a
    line counts as added only when it is new relative to every parent. Anything
    the parser does not recognise raises GuardError so the guard fails closed.
    """
    path = None
    in_section = False
    hunk_parents = 0
    line_no = 0
    for raw in diff_text.split("\n"):
        if raw.startswith("diff --git ") or raw.startswith("diff --cc ") or raw.startswith("diff --combined "):
            in_section, hunk_parents, path = True, 0, None
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
    for path, line_no, text in iter_added_lines(diff_text):
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


def decode_nul_blob(path, data):
    """Decode a blob that contains NUL bytes, or return None for a reviewed binary type.

    Text in a BOM-marked Unicode encoding (UTF-8, UTF-16 or UTF-32, as PowerShell 5.1
    ``Out-File`` writes) is decoded and scanned. Anything else with NUL bytes fails
    closed unless its extension is on the reviewed binary list.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, "replace")
    if Path(path).suffix.lower() in BINARY_EXTENSIONS:
        return None
    raise GuardError("%s contains NUL bytes but is neither BOM-marked Unicode text nor a reviewed binary type" % path)


def _lenient_lines(data):
    if b"\0" in data:
        for bom, encoding in _BOMS:
            if data.startswith(bom):
                return data.decode(encoding, "replace").splitlines()
        return []
    return _text(data).splitlines()


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


def _encoded_text_findings(changes, label, cwd, allowlist):
    """Scan changed blobs that contain NUL bytes; return (handled_paths, findings).

    git's line diff cannot represent these blobs reliably, so their added lines are
    recomputed here: lines of the decoded new blob absent from every parent's blob.
    """
    new_blobs = _read_blobs([sha for _, sha, _ in changes], cwd)
    flagged = [(path, sha, parents) for path, sha, parents in changes if b"\0" in new_blobs[sha]]
    if not flagged:
        return set(), []
    old_blobs = _read_blobs([sha for _, _, parents in flagged for sha in parents if not _is_null_sha(sha)], cwd)
    handled, findings = set(), []
    for path, sha, parents in flagged:
        handled.add(path)
        text = decode_nul_blob(path, new_blobs[sha])
        if text is None:
            continue
        new_lines = text.splitlines()
        added = set(range(len(new_lines)))
        for parent in parents or ["0"]:
            old_lines = [] if _is_null_sha(parent) else _lenient_lines(old_blobs[parent])
            fresh = set()
            for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
                if tag in ("insert", "replace"):
                    fresh.update(range(j1, j2))
            added &= fresh
        for index in sorted(added):
            for kind, value in scan_line(new_lines[index], allowlist):
                findings.append(Finding(label, path, index + 1, kind, value))
    return handled, findings


def scan_staged(cwd, allowlist):
    raw = _git(["diff", "--cached", "--raw", "-z", "--no-abbrev", "--find-renames"], cwd)
    handled, encoded = _encoded_text_findings(_raw_changes(raw), "staged", cwd, allowlist)
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
    """Scan a commit's author and committer name and email and its full message."""
    meta = _text(_git(["show", "-s", "--no-notes", "--format=%an <%ae>%n%cn <%ce>%n%B", commit], cwd))
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
        handled, encoded = _encoded_text_findings(_raw_changes(raw), commit, cwd, allowlist)
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
        if b"\0" in body:
            text = decode_nul_blob(path, body)
            if text is None:
                continue
        else:
            text = _text(body)
        for line_no, line in enumerate(text.splitlines(), 1):
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
        "A hit in an earlier commit of a pull request needs that commit rewritten, not a follow-up fix.\n"
        % len(findings)
    )
    return EXIT_FOUND


if __name__ == "__main__":
    sys.exit(main())
