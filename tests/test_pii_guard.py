"""Personal-data guard: scanner behaviour plus hook and workflow drift guards (#211).

Every email address and phone number in this module is synthetic and assembled
at runtime from fragments, so the file never contains a literal real-looking
value and the guard does not flag its own tests (proved by the self-scan test).
"""

import collections
import contextlib
import difflib
import gzip
import html
import importlib.util
import io
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile
import zlib
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts/pii_guard.py"
ALLOWLIST = ROOT / "config/pii_guard_allowlist.json"
HOOK = ROOT / ".githooks/pre-commit"
GITATTRIBUTES = ROOT / ".gitattributes"
WORKFLOW = ROOT / ".github/workflows/pii-guard.yml"
GUARD_FILES = (
    SCANNER,
    ALLOWLIST,
    HOOK,
    GITATTRIBUTES,
    WORKFLOW,
    Path(__file__).resolve(),
    ROOT / "docs/data_policy.md",
    ROOT / "docs/ci-cd/CURRENT_CICD_STATUS.md",
)
HAS_GIT = shutil.which("git") is not None

AT = "@"


def _email(local="pat.lee", host="synthetic-mail", tld="sg"):
    return local + AT + host + "." + tld


def _sg_local(sep=" "):
    return "9" + "000" + sep + "00" + "17"


def _sg_prefixed(sep=" "):
    return "+" + "65" + sep + "8" + "000" + sep + "00" + "42"


def _intl():
    return "+" + "44" + " " + "20" + " " + "7946" + " " + "00" + "11"


OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _zip_bytes(members, compression):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _zip_entries(members, compression):
    """A zip from (name, body) pairs: repeated names allowed, fixed timestamps, so the bytes are repeatable."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns on a repeated name
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, body in members:
                archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), body, compress_type=compression)
    return buffer.getvalue()


def _fullwidth_tail(value):
    """``value`` with every digit after the first one fullwidth (U+FF10 to U+FF19)."""
    first = re.search(r"\d", value).start() + 1
    return value[:first] + re.sub(r"\d", lambda match: chr(0xFF10 + int(match.group())), value[first:])


def _xlsx(cell, compression=zipfile.ZIP_DEFLATED):
    return _zip_bytes(
        {
            "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
            "xl/workbook.xml": '<?xml version="1.0"?><workbook><sheets/></workbook>',
            "xl/sharedStrings.xml": '<?xml version="1.0"?><sst><si><t>Name</t></si><si><t>' + cell + "</t></si></sst>",
        },
        compression,
    )


def _docx(body_xml):
    return _zip_bytes({"word/document.xml": '<?xml version="1.0"?><w:document><w:body>' + body_xml + "</w:body></w:document>"}, zipfile.ZIP_DEFLATED)


def _pdf(*objects):
    """A minimal PDF: each object is (dictionary, stream bytes or None)."""
    body = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
    for number, (dictionary, stream) in enumerate(objects, 1):
        body += b"%d 0 obj\n" % number + dictionary
        if stream is not None:
            body += b"\nstream\n" + stream + b"\nendstream"
        body += b"\nendobj\n"
    return body + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def _nul_free_deflate(text):
    """zlib data for ``text`` with no NUL byte, so git diffs the file holding it as text.

    Short inputs get a fixed-Huffman block whose last byte is NUL, so a filler of
    drawing operators forces dynamic Huffman; the search varies a counter.
    """
    filler = "".join("%d 0 0 %d 0 0 cm\n" % (index, index * 7) for index in range(80))
    for counter in range(3000):
        for level in (9, 6, 1):
            data = zlib.compress((filler + text + " %d" % counter).encode("ascii"), level)
            if b"\0" not in data:
                return data
    raise AssertionError("no NUL-free deflate output")


def _biff(kind, body):
    return struct.pack("<HH", kind, len(body)) + body


def _workbook(*records, pad=0):
    """A BIFF8 workbook stream: BOF, the records, BLANK filler records, EOF."""
    filler = _biff(0x0201, struct.pack("<HHH", 0, 0, 0)) * pad
    return _biff(0x0809, bytes(16)) + b"".join(records) + filler + _biff(0x000A, b"")


def _number(value):
    return _biff(0x0203, struct.pack("<HHHd", 0, 0, 0, value))


def _rk(value):
    return _biff(0x027E, struct.pack("<HHHI", 0, 0, 0, (value << 2) | 2))


def _cfb(streams):
    """A minimal OLE2 compound file (512-byte sectors, one FAT sector) holding root streams.

    Streams under 4096 bytes go to the mini stream, as the format requires.
    """
    size, end, free = 512, 0xFFFFFFFE, 0xFFFFFFFF
    sectors, fat = [b""], [0xFFFFFFFD]  # sector 0 holds the FAT itself

    def place(data):
        if not data:
            return end
        first = len(sectors)
        chunks = [data[at : at + size] for at in range(0, len(data), size)]
        for index, chunk in enumerate(chunks):
            sectors.append(chunk.ljust(size, b"\0"))
            fat.append(first + index + 1 if index + 1 < len(chunks) else end)
        return first

    mini, minifat, entries = b"", [], []
    for name, data in streams.items():
        if len(data) >= 4096:
            entries.append((name, place(data), len(data)))
            continue
        count = -(-len(data) // 64)
        start = len(mini) // 64 if count else end
        minifat.extend(start + index + 1 if index + 1 < count else end for index in range(count))
        mini += data.ljust(count * 64, b"\0")
        entries.append((name, start, len(data)))
    root_start = place(mini)
    minifat_start = place(struct.pack("<%dI" % len(minifat), *minifat))

    def entry(name, kind, start, length):
        encoded = (name + "\0").encode("utf-16-le")
        return (
            encoded.ljust(64, b"\0")
            + struct.pack("<HBB3I", len(encoded), kind, 1, free, free, free)
            + bytes(36)
            + struct.pack("<IQ", start, length)
        )

    directory = entry("Root Entry", 5, root_start, len(mini)) + b"".join(entry(name, 2, start, length) for name, start, length in entries)
    dir_start = place(directory.ljust(-(-len(directory) // size) * size, b"\0"))
    assert len(fat) <= size // 4
    sectors[0] = struct.pack("<128I", *(fat + [free] * (size // 4 - len(fat))))
    minifat_sectors = -(-len(minifat) * 4 // size)
    header = OLE2 + bytes(16) + struct.pack("<5H6x9I", 0x3E, 3, 0xFFFE, 9, 6, 0, 1, dir_start, 0, 4096, minifat_start, minifat_sectors, end, 0)
    header += struct.pack("<109I", 0, *([free] * 108))
    return header + b"".join(sectors)


def _tar_bytes(name, body):
    buffer = io.BytesIO()
    payload = body.encode("utf-8")
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _load_scanner():
    spec = importlib.util.spec_from_file_location("pii_guard_under_test", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- scanner


class ScanLineTests(unittest.TestCase):
    def setUp(self):
        self.g = _load_scanner()
        self.allow = self.g.load_allowlist(ALLOWLIST)

    def kinds(self, text, allow=None):
        return [kind for kind, _ in self.g.scan_line(text, allow or self.allow)]

    def test_flags_personal_email(self):
        self.assertEqual(self.kinds("contact: " + _email()), ["email"])
        self.assertEqual(self.kinds("<" + _email("A.Tan+promo") + ">"), ["email"])

    def test_reserved_and_role_addresses_are_allowed(self):
        for address in (
            _email("name", "example", "com"),
            _email("name", "sub.example", "org"),
            _email("name", "example", "net"),
            _email("name", "member", "test"),
            _email("name", "login", "invalid"),
            _email("name", "host", "example"),
            _email("12345+octocat", "users.noreply.github", "com"),
            _email("noreply", "github", "com"),
            _email("git", "github", "com"),
            _email("digital", "x-boundaries", "com"),
            _email("DIGITAL", "X-Boundaries", "COM"),
            _email("noreply", "x-boundaries", "com"),
            _email("bills", "my.simba", "sg"),
            _email("0100018f2a3b4c5d-synthetic", "ap-southeast-1.amazonses", "com"),
            _email("x", "eu-west-1.amazonses", "com"),
            _email("pass", "reviewed.gateway", "internal"),
            _email("noreply", "anthropic", "com"),
        ):
            self.assertEqual(self.kinds("x " + address + " y"), [], address)

    def test_company_personal_mailbox_is_not_allowed(self):
        self.assertEqual(self.kinds(_email("synthetic.person", "x-boundaries", "com")), ["email"])
        self.assertEqual(self.kinds(_email("name", "notexample", "com")), ["email"])
        self.assertEqual(self.kinds(_email("name", "example.com.evil", "sg")), ["email"])
        self.assertEqual(self.kinds(_email("someone", "my.simba", "sg")), ["email"])
        self.assertEqual(self.kinds(_email("x", "us-east-1.amazonses", "com")), ["email"])
        self.assertEqual(self.kinds(_email("x", "other.gateway", "internal")), ["email"])

    def test_not_emails(self):
        for text in ("uses: actions/checkout" + AT + "v4", "react" + AT + "18.3.1", AT + "decorator", "a " + AT + " b.com"):
            self.assertEqual(self.kinds(text), [], text)

    def test_flags_singapore_numbers(self):
        for text in (_sg_local(), _sg_local(""), _sg_local("-"), "6" + "000" + "00" + "42", "8" + "000" + "00" + "42"):
            self.assertEqual(self.kinds("tel " + text), ["sg_phone_local"], text)
        for text in (_sg_prefixed(), _sg_prefixed(""), _sg_prefixed("-"), "(" + "65) " + "6" + "000 0042", "(+" + "65)" + "3" + "000" + "0042"):
            self.assertEqual(self.kinds("tel " + text), ["sg_phone"], text)

    def test_flags_country_code_storage_form(self):
        stored = "65" + "9" + "000" + "0017"
        for text in (stored, "65 " + "9" + "000 " + "0017", "65-" + "8" + "000-" + "0042", "https://wa.me/" + stored, "wa.me/" + stored + "?text=hi"):
            self.assertEqual(self.kinds("tel " + text), ["sg_phone"], text)
        for text in (
            stored + "1",  # 11 digits
            "1" + stored,  # longer number
            "a" + stored,  # inside an identifier
            "1." + stored,  # decimal
            "65" + "1" + "000" + "0017",  # not a 3/6/8/9 subscriber number
            "65" + "9" + "000" + "001",  # too short
        ):
            self.assertEqual(self.kinds(text), [], text)

    def test_flags_international_numbers(self):
        self.assertEqual(self.kinds("call " + _intl()), ["phone"])

    # #270: no-break space (raw or &nbsp;) and en dash separate phone digits too.

    def test_flags_numbers_split_by_no_break_space_or_en_dash(self):
        for sep in ("\u00a0", "&nbsp;", "\u2013"):
            with self.subTest(sep=repr(sep)):
                self.assertEqual(self.kinds("tel " + _sg_local(sep)), ["sg_phone_local"])
                self.assertEqual(self.kinds("tel " + _sg_prefixed(sep)), ["sg_phone"])
                self.assertEqual(self.kinds("tel " + "65" + sep + "9" + "000" + sep + "00" + "17"), ["sg_phone"])
                self.assertEqual(self.kinds("call +" + "44" + sep + "20" + sep + "7946" + sep + "00" + "11"), ["phone"])
                self.assertEqual(self.kinds(_sg_local(sep), {"emails": [], "phones": {"65" + "9" + "000" + "0017"}}), [])
        for sep in ("\u2014", "\t", "\u00a0\u00a0"):
            self.assertEqual(self.kinds("tel " + _sg_local(sep)), [], repr(sep))

    def test_not_phone_numbers(self):
        for text in (
            "9" + "000" + "001",  # 7 digits
            "9" + "000" + "00" + "017",  # 9 digits
            "7" + "000" + "00" + "17",  # leading 7
            "2026" + "1021",  # date stamp
            "1." + "9" + "000" + "00" + "17",  # decimal
            "9" + "000" + "00" + "17.5",  # decimal
            "sha a" + "9" + "000" + "00" + "17b",  # inside an identifier
            "v" + "9" + "000" + "00" + "17",
            "+" + "1 2 3",  # too few digits
        ):
            self.assertEqual(self.kinds(text), [], text)

    def test_overlap_reports_one_hit(self):
        self.assertEqual(self.kinds(_sg_local("") + AT + "synthetic-mail.sg"), ["email"])
        self.assertEqual(self.kinds(_sg_prefixed()), ["sg_phone"])

    def test_opt_out_marker_needs_a_reason(self):
        line = "tel " + _sg_local() + "  # pii-guard: allow (published company main line)"
        self.assertEqual(self.kinds(line), [])
        for marker in ("# pii-guard: allow", "# pii-guard: allow ()", "# pii-guard: allow ( )", "# pii-guard allow (x)", "# pii-guard: deny (x)"):
            self.assertEqual(self.kinds("tel " + _sg_local() + "  " + marker), ["sg_phone_local"], marker)

    def test_phone_allowlist_matches_normalised_digits(self):
        allow = {"emails": [], "phones": {"65" + "9" + "000" + "0017", "65" + "8" + "000" + "0042"}}
        self.assertEqual(self.kinds(_sg_local(), allow), [])
        self.assertEqual(self.kinds(_sg_prefixed(), allow), [])
        self.assertEqual(self.kinds(_intl(), allow), ["phone"])

    # #270 round 3: the pre-#270 patterns are a floor, so a wider separator never hides a number.

    def test_wider_separators_never_drop_an_international_number(self):
        # G2 W2: a number followed by more digit groups over a new separator went past 15 digits.
        date = "2026" + " " + "10" + " " + "23"
        for sep in (" ", "&nbsp;", "–"):
            with self.subTest(sep=repr(sep)):
                self.assertEqual(self.kinds("Tel " + _intl() + sep + date), ["phone"])
                self.assertEqual(self.kinds("Tel " + _intl() + sep + "12" + sep + "34"), ["phone"])
                self.assertEqual(self.kinds("Tel " + _intl() + sep + "1234" + "56789"), ["phone"])
        # Still one hit when the wider match is itself a valid number.
        self.assertEqual(self.kinds("Tel " + _intl() + "&nbsp;" + "12"), ["phone"])
        # Plain space: base and head both read one 20-digit group and drop it (Known limits).
        self.assertEqual(self.kinds("Tel " + _intl() + " " + date), [])

    def test_allowlisted_wider_match_cannot_hide_a_number(self):
        # A new match over a wider separator that the allowlist covers must not shadow the base match.
        for sep, lead in ((" ", "9"), ("&nbsp;", "8"), ("–", "6")):
            with self.subTest(sep=repr(sep)):
                allow = {"emails": [], "phones": {"65" + lead + "000" + "9000"}}
                self.assertEqual(self.kinds(lead + "000" + sep + _sg_local(), allow), ["sg_phone_local"])

    def test_floor_patterns_are_the_pre_270_patterns(self):
        self.assertEqual(
            [regex.pattern for _, regex in self.g.FLOOR_PHONE_PATTERNS],
            [
                r"(?<![\w+])(?:\+65|\(\+?65\))[ .-]?[3689]\d{3}[ .-]?\d{4}(?!\d)",
                r"(?<![\w.+])65[ -]?[3689]\d{3}[ -]?\d{4}(?!\d|\.\d)",
                r"(?<![\w.])[689]\d{3}[ -]?\d{4}(?!\d|\.\d)",
                r"(?<![\w+])\+[1-9](?:[ .-]?\(?\d+\)?){2,7}",
            ],
        )
        self.assertEqual([kind for kind, _ in self.g.FLOOR_PHONE_PATTERNS], [kind for kind, _ in self.g.PHONE_PATTERNS])


class AllowlistTests(unittest.TestCase):
    EXPECTED_PATTERNS = [
        "*" + AT + "example.com",
        "*" + AT + "*.example.com",
        "*" + AT + "example.org",
        "*" + AT + "*.example.org",
        "*" + AT + "example.net",
        "*" + AT + "*.example.net",
        "*" + AT + "*.example",
        "*" + AT + "*.test",
        "*" + AT + "*.invalid",
        "*" + AT + "*.localhost",
        "*" + AT + "users.noreply.github.com",
        "noreply" + AT + "github.com",
        "git" + AT + "github.com",
        "digital" + AT + "x-boundaries.com",
        "noreply" + AT + "x-boundaries.com",
        "bills" + AT + "my.simba.sg",
        "*" + AT + "ap-southeast-1.amazonses.com",
        "*" + AT + "eu-west-1.amazonses.com",
        "*" + AT + "reviewed.gateway.internal",
        "noreply" + AT + "anthropic.com",
    ]

    def setUp(self):
        self.g = _load_scanner()

    def test_tracked_allowlist_is_exactly_the_reviewed_set(self):
        # Pinned: widening the allowlist is a reviewed change to this test, never a quiet edit.
        allow = self.g.load_allowlist(ALLOWLIST)
        self.assertEqual(allow["emails"], self.EXPECTED_PATTERNS)
        self.assertEqual(allow["phones"], set())
        for pattern in allow["emails"]:
            local, domain = pattern.split(AT)
            self.assertNotIn("*", domain.lstrip("*."), pattern)
            self.assertTrue(local == "*" or "*" not in local, pattern)

    def test_malformed_allowlists_fail_closed(self):
        bad = (
            "not json",
            "[]",
            '{"emails": []}',
            '{"emails": [], "phones": [], "paths": []}',
            '{"emails": {}, "phones": []}',
            '{"emails": [{"pattern": "*' + AT + 'x.sg"}], "phones": []}',
            '{"emails": [{"pattern": "*' + AT + 'x.sg", "reason": " "}], "phones": []}',
            '{"emails": [{"pattern": "", "reason": "r"}], "phones": []}',
            '{"emails": [], "phones": [{"digits": "+65-1", "reason": "r"}]}',
        )
        with tempfile.TemporaryDirectory() as tmp:
            for index, body in enumerate(bad):
                path = Path(tmp, "a%d.json" % index)
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(self.g.GuardError, msg=body):
                    self.g.load_allowlist(path)
            with self.assertRaises(self.g.GuardError):
                self.g.load_allowlist(Path(tmp, "missing.json"))


class DiffParserTests(unittest.TestCase):
    def setUp(self):
        self.g = _load_scanner()

    def added(self, text):
        return list(self.g.iter_added_lines(text))

    def test_plain_patch(self):
        patch = (
            "diff --git a/x.txt b/x.txt\n"
            "index 1111111..2222222 100644\n"
            "--- a/x.txt\n"
            "+++ b/x.txt\n"
            "@@ -3 +3,2 @@ context\n"
            "-old\n"
            "+new one\n"
            "++++ looks like a header\n"
            "@@ -10,0 +12 @@\n"
            "+tail\n"
            "\\ No newline at end of file\n"
            "diff --git a/gone.txt b/gone.txt\n"
            "deleted file mode 100644\n"
            "index 3333333..0000000\n"
            "--- a/gone.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-bye\n"
        )
        self.assertEqual(self.added(patch), [("x.txt", 3, "new one"), ("x.txt", 4, "+++ looks like a header"), ("x.txt", 12, "tail")])

    def test_combined_merge_patch_keeps_only_lines_new_to_every_parent(self):
        patch = (
            "diff --cc m.txt\n"
            "index 1111111,2222222..3333333\n"
            "--- a/m.txt\n"
            "+++ b/m.txt\n"
            "@@@ -1,1 -1,1 +1,4 @@@\n"
            "+ from second parent\n"
            " +from first parent\n"
            "++evil merge line\n"
            "- removed from first\n"
            "  context\n"
        )
        self.assertEqual(self.added(patch), [("m.txt", 3, "evil merge line")])

    def test_combined_binary_summary_needs_a_blob_scanned_path(self):
        patch = "diff --cc data.bin\nindex 1111111,2222222..3333333\nBinary files differ\n"
        self.assertEqual(list(self.g.iter_added_lines(patch, {"data.bin"})), [])
        for scanned in ((), {"other.bin"}):
            with self.assertRaises(self.g.GuardError):
                list(self.g.iter_added_lines(patch, scanned))
        plain = "diff --git a/data.bin b/data.bin\nindex 1111111..2222222 100644\nBinary files a/data.bin and b/data.bin differ\n"
        with self.assertRaises(self.g.GuardError):
            list(self.g.iter_added_lines(plain, {"data.bin"}))

    def test_unrecognised_output_fails_closed(self):
        for patch in (
            "garbage before any diff\n",
            "diff --git a/x b/x\nweird header\n",
            "diff --git a/x b/x\n+++ b/x\n@@ nonsense @@\n+v\n",
            "@@ -1 +1 @@\n+v\n",
            "diff --git a/x b/x\n+++ b/x\n@@ -1 +1 @@\n*v\n",
            "diff --git a/x b/x\n@@ -1 +1 @@\n+v\n",
            "diff --git a/x b/x\n+++ x\n@@ -1 +1 @@\n+v\n",
            # --text is always passed, so a binary summary means the diff was not scanned.
            "diff --git a/img.png b/img.png\nnew file mode 100644\nindex 0000000..4444444\nBinary files /dev/null and b/img.png differ\n",
        ):
            with self.assertRaises(self.g.GuardError, msg=patch):
                self.added(patch)


@unittest.skipUnless(HAS_GIT, "git is required")
class GitScanTests(unittest.TestCase):
    def setUp(self):
        self.g = _load_scanner()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        empty = base / "empty.gitconfig"
        empty.write_text("", encoding="utf-8")
        isolated = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty), "GIT_TERMINAL_PROMPT": "0"}
        patcher = mock.patch.dict(os.environ, isolated)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.name", "Synthetic Tester")
        self.git("config", "user.email", "tester" + AT + "example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.hooksPath", str(base / "no-hooks"))
        self.commit("README.md", "base\n")
        self.base = self.rev("HEAD")

    def git(self, *args):
        proc = subprocess.run(["git"] + list(args), cwd=str(self.repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode:
            raise AssertionError("git %s failed: %s" % (args, proc.stderr.decode("utf-8", "replace")))
        return proc.stdout.decode("utf-8", "replace")

    def rev(self, name):
        return self.git("rev-parse", name).strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def commit(self, name, text, message="change", author=None):
        self.write(name, text)
        self.git("add", "--", name)
        self.git("commit", "-q", "-m", message, *(["--author", author] if author else []))

    def commit_bytes(self, name, data, message="change"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.git("add", "--", name)
        self.git("commit", "-q", "-m", message)

    def range_code(self, start):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.run_main("--range", start + "..HEAD")

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = self.g.main(list(argv), cwd=self.repo, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_range_catches_a_value_added_then_removed(self):
        self.commit("notes.md", "owner " + _email() + "\n")
        self.commit("notes.md", "owner removed\n")
        head = self.rev("HEAD")
        # The final diff alone is clean ...
        final = self.git("diff", "-U0", "--src-prefix=a/", "--dst-prefix=b/", self.base, head)
        self.assertEqual([hit for _, _, text in self.g.iter_added_lines(final) for hit in self.g.scan_line(text, self.g.load_allowlist(ALLOWLIST))], [])
        # ... but the per-commit range scan still catches the published value.
        code, out, err = self.run_main("--range", self.base + ".." + head)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(out.count("pii-guard: "), 1)
        self.assertIn("notes.md:1 email [redacted]", out)
        self.assertNotIn(_email(), out + err)
        code, out, _ = self.run_main("--range", self.base + ".." + head, "--show-values")
        self.assertEqual(code, 1)
        self.assertIn(_email(), out)

    def test_clean_range_and_allowed_lines_pass(self):
        self.commit("a.md", "write to " + _email("name", "example", "com") + "\n")
        self.commit("b.md", "tel " + _sg_local() + "  <!-- pii-guard: allow (synthetic fixture) -->\n")
        code, out, err = self.run_main("--range", self.base + "..HEAD")
        self.assertEqual(code, 0, out + err)

    def test_every_commit_in_the_range_is_scanned(self):
        self.commit("a.md", "tel " + _sg_local() + "\n")
        self.commit("b.md", "mail " + _email() + "\n")
        self.commit("c.md", "clean\n")
        code, out, _ = self.run_main("--range", self.base + "..HEAD")
        self.assertEqual(code, 1)
        self.assertIn("a.md:1 sg_phone_local", out)
        self.assertIn("b.md:1 email", out)

    def test_merge_commits_flag_only_lines_new_to_the_merge(self):
        self.git("checkout", "-q", "-b", "feature")
        self.commit("feature.md", "clean feature\n")
        self.git("checkout", "-q", "main")
        self.commit("legacy.md", "existing " + _email("old", "legacy-mail", "sg") + "\n")
        main_tip = self.rev("HEAD")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-edit", "main")
        code, out, err = self.run_main("--range", main_tip + "..feature")
        self.assertEqual(code, 0, out + err)
        # An "evil" merge that adds a value of its own is still caught.
        self.git("checkout", "-q", "main")
        self.commit("other.md", "main moves on\n")
        main_tip = self.rev("HEAD")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-commit", "--no-ff", "main")
        self.write("feature.md", "clean feature\nmerge adds " + _sg_prefixed() + "\n")
        self.git("add", "feature.md")
        self.git("commit", "-q", "--no-edit")
        code, out, _ = self.run_main("--range", main_tip + "..feature")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("pii-guard: "), 1)
        self.assertIn("feature.md:2 sg_phone", out)

    def test_pure_rename_of_existing_content_is_not_a_new_addition(self):
        self.commit("old.md", "legacy " + _email("old", "legacy-mail", "sg") + "\n" + "filler line\n" * 20)
        start = self.rev("HEAD")
        self.git("mv", "old.md", "new.md")
        self.git("commit", "-q", "-m", "rename")
        code, out, err = self.run_main("--range", start + "..HEAD")
        self.assertEqual(code, 0, out + err)

    def test_user_diff_config_cannot_change_the_parse(self):
        for key, value in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"), ("color.ui", "always"), ("color.diff", "always")):
            self.git("config", key, value)
        self.commit("a.md", "mail " + _email() + "\n")
        code, out, err = self.run_main("--range", self.base + "..HEAD")
        self.assertEqual(code, 1, out + err)
        self.assertIn("a.md:1 email", out)

    def test_staged_mode(self):
        self.write("s.md", "mail " + _email() + "\n")
        self.git("add", "s.md")
        code, out, _ = self.run_main("--staged", "--show-values")
        self.assertEqual(code, 1)
        self.assertIn("staged s.md:1 email " + _email(), out)
        self.write("s.md", "mail " + _email("name", "example", "com") + "\n")
        self.git("add", "s.md")
        self.assertEqual(self.run_main("--staged")[0], 0)

    def test_tree_report_counts_without_values(self):
        self.commit("t.md", "a " + _email() + "\nb " + _sg_local() + "\n")
        code, out, _ = self.run_main("--tree", "HEAD", "--report-only")
        self.assertEqual(code, 0)
        self.assertIn("pii-guard report: 2 hit(s) in 1 file(s)", out)
        self.assertIn("  2 t.md", out)
        self.assertNotIn(_email(), out)
        self.assertNotIn(_sg_local(), out)
        self.assertEqual(self.run_main("--tree", "HEAD")[0], 1)

    def test_errors_fail_closed(self):
        head = self.rev("HEAD")
        for argv in (
            [],
            ["--range"],
            ["--range", ""],
            ["--range", ".." + head],
            ["--range", self.base + ".."],
            ["--range", head],
            ["--range", self.base + "..." + head],
            ["--range", "0" * 40 + ".." + head],
            ["--range", "-x.." + head],
            ["--range", "no-such-branch.." + head],
            ["--range", head + ".." + head],
            ["--tree", ""],
            ["--tree", "no-such-branch"],
            ["--staged", "--range", head + ".." + head],
            ["--staged", "--allowlist", str(Path(self.tmp.name, "missing.json"))],
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                code, out, err = self.run_main(*argv)
            self.assertEqual(code, 2, (argv, out, err))


    # F1: attributes and encodings cannot hide added lines.

    def test_diff_attribute_cannot_hide_added_lines(self):
        for attributes, name in (("*.json -diff\n", "data.json"), ("*.csv binary\n", "rows.csv")):
            with self.subTest(attributes=attributes):
                start = self.rev("HEAD")
                self.commit(".gitattributes", attributes)
                self.commit(name, "header\nrow " + _email() + "\n")
                code, out, err = self.range_code(start)
                self.assertEqual(code, 1, out + err)
                self.assertIn(name + ":2 email [redacted]", out)

    def test_nul_bytes_without_a_bom_fail_closed(self):
        start = self.rev("HEAD")
        self.commit_bytes("notes.txt", b"head\x00er\nmail " + _email().encode("ascii") + b"\n")
        code, out, err = self.range_code(start)
        self.assertEqual(code, 2, out + err)
        self.assertNotIn(_email(), out + err)
        start = self.rev("HEAD")
        self.commit_bytes("raw16.txt", ("mail " + _email() + "\n").encode("utf-16-le"))
        self.assertEqual(self.range_code(start)[0], 2)

    def test_clean_binary_types_pass(self):
        start = self.rev("HEAD")
        self.commit_bytes("logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
        self.commit_bytes("book.xlsx", _xlsx("Synthetic Member"))
        code, out, err = self.range_code(start)
        self.assertEqual(code, 0, out + err)

    # R2-1: no NUL-bearing blob is skipped whole.

    def test_binary_content_is_scanned_by_text_runs(self):
        email = _email().encode("ascii")
        cases = (
            ("b1.pdf", b"%PDF-1.4\n\x00\x01\x02contact " + email + b"\n"),
            (
                "b3.pdf",
                b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Author (" + email + b") /Producer (Synthetic) >>\nendobj\n"
                b"2 0 obj\n<< /Length 24 /Filter /FlateDecode >>\nstream\n" + zlib.compress(b"q 1 0 0 1 0 0 cm Q") + b"\x00\x00"
                b"\nendstream\nendobj\n%%EOF\n",
            ),
            ("b3be.pdf", b"%PDF-1.7\n\x00\x00 << /Title (\xfe\xff" + ("call " + _sg_prefixed()).encode("utf-16-be") + b") >>\n"),
            ("b6.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00tEXtAuthor\x00" + email + b"\x00\x00IEND"),
            ("c1.txt", b"\xff\xfe" + b"plain ascii mail " + email + b"\n\x00tail"),
            ("utf16.doc", OLE2 + b"\x00\x00" + ("owner " + _email() + " ").encode("utf-16-le") + b"\x00\x00"),
        )
        for name, data in cases:
            with self.subTest(name=name):
                start = self.rev("HEAD")
                self.commit_bytes(name, data)
                code, out, err = self.range_code(start)
                self.assertEqual(code, 1, (name, out, err))
                self.assertIn(name + " (text runs):", out)
                self.assertNotIn(_email(), out + err)
        # Editing a binary elsewhere does not re-flag its existing text.
        start = self.rev("HEAD")
        self.commit_bytes("b6.png", cases[3][1] + b"\x00\x01\x02\x03")
        self.assertEqual(self.range_code(start)[0], 0)

    def test_zip_containers_are_expanded(self):
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                start = self.rev("HEAD")
                name = "book-%d.xlsx" % compression
                self.commit_bytes(name, _xlsx(_email(), compression))
                code, out, err = self.range_code(start)
                self.assertEqual(code, 1, out + err)
                self.assertIn(name + "!xl/sharedStrings.xml:1 email [redacted]", out)
                self.assertNotIn(_email(), out + err)
        start = self.rev("HEAD")
        document = _zip_bytes({"word/document.xml": "<w:t>tel " + _sg_local() + "</w:t>"}, zipfile.ZIP_DEFLATED)
        self.commit_bytes("letter.docx", document)
        code, out, _ = self.range_code(start)
        self.assertEqual(code, 1)
        self.assertIn("letter.docx!word/document.xml:1 sg_phone_local", out)
        # Rewriting the container with the same member text is not a new addition.
        start = self.rev("HEAD")
        self.commit_bytes("letter.docx", _zip_bytes({"word/document.xml": "<w:t>tel " + _sg_local() + "</w:t>", "docProps/app.xml": "<x/>"}, zipfile.ZIP_DEFLATED))
        self.assertEqual(self.range_code(start)[0], 0)

    def test_unreadable_zip_containers_fail_closed(self):
        good = _xlsx("Synthetic Member")
        encrypted = bytearray(good)
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = 0
            while True:
                at = encrypted.find(signature, at)
                if at < 0:
                    break
                encrypted[at + offset] |= 0x01
                at += 4
        for name, data in (
            ("corrupt.xlsx", b"PK\x03\x04" + b"\x00\x01" * 40),
            ("truncated.xlsx", good[: len(good) // 2]),
            ("encrypted.xlsx", bytes(encrypted)),
            ("notzip.zip", b"plain text only\n"),
        ):
            with self.subTest(name=name):
                start = self.rev("HEAD")
                self.commit_bytes(name, data)
                self.assertEqual(self.range_code(start)[0], 2, name)
                self.git("rm", "-q", name)
                self.git("commit", "-q", "-m", "drop")
        start = self.rev("HEAD")
        self.commit_bytes("big.xlsx", good)
        with mock.patch.object(self.g, "ZIP_MAX_BYTES", 16):
            self.assertEqual(self.range_code(start)[0], 2)


    # R3-1: zips are found by signature at every depth and expanded recursively.

    def test_nested_and_signature_detected_zips_are_expanded(self):
        deflated = zipfile.ZIP_DEFLATED
        inner_xlsx = _xlsx(_email())
        cases = (
            ("bundle.zip", _zip_bytes({"reports/report.xlsx": inner_xlsx}, deflated), "bundle.zip!reports/report.xlsx!xl/sharedStrings.xml:1 email"),
            (
                "outer.zip",
                _zip_bytes({"inner.zip": _zip_bytes({"notes.txt": "mail " + _email()}, deflated)}, deflated),
                "outer.zip!inner.zip!notes.txt:1 email",
            ),
            ("export.dat", _zip_bytes({"rows.csv": "name,phone\nA," + _sg_local() + "\n"}, deflated), "export.dat!rows.csv:2 sg_phone_local"),
            (
                "embed.xlsx",
                _zip_bytes({"xl/embeddings/Microsoft_Excel_Worksheet.xlsx": inner_xlsx}, deflated),
                "embed.xlsx!xl/embeddings/Microsoft_Excel_Worksheet.xlsx!xl/sharedStrings.xml:1 email",
            ),
        )
        for name, data, expected in cases:
            with self.subTest(name=name):
                start = self.rev("HEAD")
                self.commit_bytes(name, data)
                code, out, err = self.range_code(start)
                self.assertEqual(code, 1, (name, out, err))
                self.assertIn(expected + " [redacted]", out)
                self.assertNotIn(_email(), out + err)
        self.assertEqual(self.run_main("--tree", "HEAD", "--report-only")[0], 0)

    def test_zip_nesting_depth_is_capped(self):
        def nested(levels):
            data, name = "mail " + _email(), "notes.txt"
            for level in range(levels):
                data, name = _zip_bytes({name: data}, zipfile.ZIP_DEFLATED), "level%d.zip" % level
            return data

        start = self.rev("HEAD")
        self.commit_bytes("three.zip", nested(3))
        code, out, err = self.range_code(start)
        self.assertEqual(code, 1, out + err)
        self.assertIn("three.zip!level1.zip!level0.zip!notes.txt:1 email", out)
        start = self.rev("HEAD")
        self.commit_bytes("four.zip", nested(4))
        self.assertEqual(self.range_code(start)[0], 2)

    # R3-2: disguised containers and archives the scanner cannot open fail closed.

    def test_disguised_and_unsupported_archives_fail_closed(self):
        clean_zip = _zip_bytes({"a.txt": "nothing personal"}, zipfile.ZIP_DEFLATED)
        packed = gzip.compress(("mail " + _email()).encode("ascii"), mtime=0)
        for name, data in (
            ("image.png", clean_zip),
            ("doc.pdf", clean_zip),
            ("sheet.xls", clean_zip),
            ("photo.png", packed),
            ("logs.gz", packed),
            ("backup.tar", _tar_bytes("notes.txt", "mail " + _email())),
            ("archive.7z", b"7z\xbc\xaf\x27\x1c\x00\x04" + bytes(32)),
            ("archive.rar", b"Rar!\x1a\x07\x00" + bytes(32)),
            ("inner.zip", _zip_bytes({"logs.gz": packed}, zipfile.ZIP_DEFLATED)),
            ("sheet2.xls", b"\xd0\xcf\x11\xe0" + b"\x00" * 16),
        ):
            with self.subTest(name=name):
                start = self.rev("HEAD")
                self.commit_bytes(name, data)
                code, out, err = self.range_code(start)
                self.assertEqual(code, 2, (name, out, err))
                self.assertNotIn(_email(), out + err)
                self.git("rm", "-q", name)
                self.git("commit", "-q", "-m", "drop")

    def test_member_names_cannot_forge_log_lines(self):
        start = self.rev("HEAD")
        self.commit_bytes("names.zip", _zip_bytes({"a\nb.txt": "mail " + _email()}, zipfile.ZIP_DEFLATED))
        code, out, _ = self.range_code(start)
        self.assertEqual(code, 1)
        self.assertIn("names.zip!a?b.txt:1 email", out)
        self.assertTrue(all(line.startswith("pii-guard: ") for line in out.splitlines()), out)

    def test_staged_and_tree_modes_scan_binary_content(self):
        (self.repo / "s.xlsx").write_bytes(_xlsx(_email()))
        (self.repo / "s.pdf").write_bytes(b"%PDF-1.4\n\x00 /Author (" + _email("other.person").encode("ascii") + b")\n")
        self.git("add", "s.xlsx", "s.pdf")
        code, out, _ = self.run_main("--staged")
        self.assertEqual(code, 1)
        self.assertIn("staged s.xlsx!xl/sharedStrings.xml:1 email", out)
        self.assertIn("staged s.pdf (text runs):", out)
        self.git("commit", "-q", "-m", "binaries")
        code, out, _ = self.run_main("--tree", "HEAD", "--report-only")
        self.assertEqual(code, 0)
        self.assertIn("pii-guard report: 2 hit(s) in 2 file(s)", out)

    # R2-2: evil merges that touch NUL-bearing files are scanned, not errors.

    def test_evil_merge_touching_binary_files(self):
        self.commit_bytes("export.txt", b"\xff\xfe" + "first\r\n".encode("utf-16-le"))
        self.commit_bytes("logo.png", b"\x89PNG\x00\x00base")
        self.git("checkout", "-q", "-b", "feature")
        self.commit("feature.md", "clean feature\n")
        self.git("checkout", "-q", "main")
        self.commit("other.md", "main moves on\n")
        main_tip = self.rev("HEAD")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-commit", "--no-ff", "main")
        (self.repo / "logo.png").write_bytes(b"\x89PNG\x00\x00base\x00changed in merge")
        self.git("add", "logo.png")
        self.git("commit", "-q", "--no-edit")
        code, out, err = self.range_code(main_tip)
        self.assertEqual(code, 0, out + err)
        self.git("checkout", "-q", "main")
        self.commit("other.md", "main moves again\n")
        main_tip = self.rev("HEAD")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-commit", "--no-ff", "main")
        (self.repo / "export.txt").write_bytes(b"\xff\xfe" + ("first\r\nmail " + _email() + "\r\n").encode("utf-16-le"))
        self.git("add", "export.txt")
        self.git("commit", "-q", "--no-edit")
        code, out, err = self.range_code(main_tip)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(out.count("pii-guard: "), 1)
        self.assertIn("export.txt:2 email", out)

    def test_utf16_text_is_decoded_and_only_added_lines_count(self):
        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le"):
            with self.subTest(encoding=encoding):
                bom = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff", "utf-32-le": b"\xff\xfe\x00\x00"}[encoding]
                name = "log-%s.txt" % encoding
                start = self.rev("HEAD")
                self.commit_bytes(name, bom + ("first\r\nmail " + _email() + "\r\n").encode(encoding))
                code, out, err = self.range_code(start)
                self.assertEqual(code, 1, out + err)
                self.assertIn(name + ":2 email [redacted]", out)
                # Editing the file elsewhere does not re-flag the existing line; a new one is caught.
                start = self.rev("HEAD")
                self.commit_bytes(name, bom + ("first\r\nmail " + _email() + "\r\nclean\r\n").encode(encoding))
                self.assertEqual(self.range_code(start)[0], 0)
                start = self.rev("HEAD")
                body = "first\r\nmail " + _email() + "\r\nclean\r\ntel " + _sg_local() + "\r\n"
                self.commit_bytes(name, bom + body.encode(encoding))
                code, out, _ = self.range_code(start)
                self.assertEqual(code, 1)
                self.assertEqual(out.count("pii-guard: "), 1)
                self.assertIn(name + ":4 sg_phone_local", out)

    def test_utf16_text_from_a_merged_parent_is_not_new(self):
        self.git("checkout", "-q", "-b", "feature")
        self.commit("feature.md", "clean feature\n")
        self.git("checkout", "-q", "main")
        self.commit_bytes("export.txt", b"\xff\xfe" + ("mail " + _email() + "\r\n").encode("utf-16-le"))
        main_tip = self.rev("HEAD")
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-edit", "main")
        code, out, err = self.range_code(main_tip)
        self.assertEqual(code, 0, out + err)

    def test_staged_and_tree_modes_decode_utf16(self):
        self.write("s.txt", "")
        (self.repo / "s.txt").write_bytes(b"\xff\xfe" + ("mail " + _email() + "\r\n").encode("utf-16-le"))
        self.git("add", "s.txt")
        code, out, _ = self.run_main("--staged")
        self.assertEqual(code, 1)
        self.assertIn("staged s.txt:1 email", out)
        self.git("commit", "-q", "-m", "utf16")
        code, out, _ = self.run_main("--tree", "HEAD", "--report-only")
        self.assertEqual(code, 0)
        self.assertIn("  1 s.txt", out)
        self.commit_bytes("bad.txt", b"x\x00y\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main("--tree", "HEAD", "--report-only")[0], 2)
        self.write("bad2.txt", "")
        (self.repo / "bad2.txt").write_bytes(b"x\x00y\n")
        self.git("add", "bad2.txt")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main("--staged")[0], 2)

    # F2 (owner decision 2026-10-21): commit messages are scanned, identities are not.

    def test_commit_message_is_scanned_but_identity_is_not(self):
        start = self.rev("HEAD")
        self.commit("a.md", "clean\n", author="Synthetic Person <" + _email("synthetic.person") + ">")
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_EMAIL": _email("synthetic.committer")}):
            self.commit("b.md", "clean\n")
        code, out, err = self.range_code(start)
        self.assertEqual(code, 0, out + err)
        start = self.rev("HEAD")
        self.commit("c.md", "clean\n", message="Fix intake\n\nReported by " + _email() + " on " + _sg_prefixed())
        code, out, err = self.range_code(start)
        self.assertEqual(code, 1)
        self.assertIn("(commit message):3 email [redacted]", out)
        self.assertIn("(commit message):3 sg_phone", out)
        self.assertNotIn(_email(), out + err)
        # The same identity address used in content is still flagged.
        start = self.rev("HEAD")
        self.commit("e.md", "contact " + _email("synthetic.person") + "\n", author="Synthetic Person <" + _email("synthetic.person") + ">")
        code, out, _ = self.range_code(start)
        self.assertEqual(code, 1)
        self.assertIn("e.md:1 email", out)
        start = self.rev("HEAD")
        self.commit("d.md", "clean\n", message="Clean change\n\nCo-Authored-By: Bot <noreply" + AT + "anthropic.com>")
        self.assertEqual(self.range_code(start)[0], 0)

    # #270: new decoders run in range mode, including a NUL-free PDF git would diff as text.

    def test_range_scans_hidden_binary_text(self):
        local = int("9" + "000" + "00" + "17")
        pdf = _pdf((b"<< /Filter /FlateDecode >>", _nul_free_deflate("BT (mail " + _email() + ") Tj ET")))
        self.assertNotIn(b"\0", pdf)
        self.assertNotIn(_email().encode("ascii"), pdf)
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(32))
        for name, data, expected in (
            ("plain.pdf", pdf, "plain.pdf (deflated streams):81 email"),  # after 80 filler lines
            ("book.xls", _cfb({"Workbook": _workbook(_rk(local))}), "book.xls (numeric cells):1 sg_phone_local"),
            ("poly.png", png + _zip_bytes({"notes.txt": "mail " + _email()}, zipfile.ZIP_DEFLATED), "poly.png!notes.txt:1 email"),
            ("split.docx", _docx("<w:p><w:r><w:t>" + "9" + "000</w:t></w:r><w:r><w:t>" + "00" + "17</w:t></w:r></w:p>"), "split.docx!word/document.xml (joined text):"),
        ):
            with self.subTest(name=name):
                start = self.rev("HEAD")
                self.commit_bytes(name, data)
                code, out, err = self.range_code(start)
                self.assertEqual(code, 1, (name, out, err))
                self.assertIn(expected, out)
                self.assertNotIn(_email(), out + err)
        # A new clean cell does not re-flag the workbook's existing one; a new phone cell is caught.
        start = self.rev("HEAD")
        self.commit_bytes("book.xls", _cfb({"Workbook": _workbook(_rk(local), _rk(3))}))
        self.assertEqual(self.range_code(start)[0], 0)
        start = self.rev("HEAD")
        self.commit_bytes("book.xls", _cfb({"Workbook": _workbook(_rk(local), _rk(3), _number(float("6" + "000" + "00" + "42")))}))
        code, out, _ = self.range_code(start)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("pii-guard: "), 1)
        self.assertIn("book.xls (numeric cells):3 sg_phone_local", out)

    def test_hit_message_wording_is_pinned(self):
        # The "report it to the repository owner" step is policy; changing it is a reviewed change.
        self.commit("a.md", "mail " + _email() + "\n")
        code, out, err = self.run_main("--range", self.base + "..HEAD")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(
            err,
            "pii-guard: 1 likely personal-data hit(s). Remove the value, use a reserved example "
            "(name" + AT + "example.com), or add 'pii-guard: allow (<reason>)' on the line if it is justified. "
            "A hit in an earlier commit of a pull request needs that commit rewritten, not a follow-up fix, "
            "and a hit on a pushed branch is already public: report it to the repository owner as well.\n",
        )

    def test_not_a_repository_fails_closed(self):
        outside = Path(self.tmp.name, "plain")
        outside.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.tmp.name}):
            code = self.g.main(["--staged"], cwd=outside, out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 2)


    def test_nul_free_blob_keeps_git_added_lines(self):
        # #270 round 3: a NUL-free text file starting %PDF is blob-scanned for its streams,
        # but git still decides its added text lines, so a moved line is still caught.
        line = "x " + _email()
        self.commit("notes.txt", "%PDF-1.4\n" + line + "\ny\n")
        start = self.rev("HEAD")
        self.commit("notes.txt", "%PDF-1.4\ny\n" + line + "\n")
        code, out, _ = self.range_code(start)
        self.assertEqual(code, 1, out)
        self.assertIn("notes.txt:3 email [redacted]", out)

    def test_round_4_shapes_in_range_and_staged_modes(self):
        # G2 W3 flags and G2 W4 fails closed in every git mode, as base flagged or failed.
        clean = _zip_bytes({"readme.txt": "clean"}, zipfile.ZIP_DEFLATED)
        bom = b"\xff\xfe" + ("tel " + _fullwidth_tail(_sg_local()) + "\r\n").encode("utf-16-le") + clean
        lines = "\n".join("line %d" % index for index in range(9)) + "\nmail " + _email()
        collide = _zip_entries([("a.xml (joined text)", lines), ("a.xml", "<a/>")], zipfile.ZIP_DEFLATED)
        for name, data, expected in (("w3.zip", _zip_bytes({"n.txt": bom}, zipfile.ZIP_DEFLATED), 1), ("w4.zip", collide, 2)):
            with self.subTest(name=name):
                start = self.rev("HEAD")
                (self.repo / name).write_bytes(data)
                self.git("add", "--", name)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(self.run_main("--staged")[0], expected)
                self.git("commit", "-q", "-m", name)
                self.assertEqual(self.range_code(start)[0], expected)

    def test_text_parent_runs_do_not_hide_new_binary_runs(self):
        # #270 round 4 fuzz: base read a NUL-free PDF parent as text only, so when the file
        # gains a NUL byte every run of the new blob counts as added, as it did in base.
        line = "BT (contact " + _email() + ") Tj ET"
        self.commit_bytes("scan.pdf", _pdf((b"<< /Length 9 >>", line.encode("ascii"))))
        start = self.rev("HEAD")
        self.commit_bytes("scan.pdf", _pdf((b"<< /Length 9 >>", line.encode("ascii")), (b"<< /Length 2 >>", b"\x00\x01")))
        code, out, _ = self.range_code(start)
        self.assertEqual(code, 1, out)
        self.assertIn("scan.pdf (text runs):", out)


class BinaryFormatTests(unittest.TestCase):
    """#270: formats whose raw bytes hide text, checked through blob_documents."""

    def setUp(self):
        self.g = _load_scanner()
        self.allow = self.g.load_allowlist(ALLOWLIST)

    def hits(self, path, data):
        return [(item.path, item.kind) for item in self.g._scan_documents("t", self.g.blob_documents(path, data), self.allow)]

    def assert_fails_closed(self, path, data):
        with self.assertRaises(self.g.GuardError, msg=path):
            self.g.blob_documents(path, data)

    def test_text_runs_keep_no_break_space_and_en_dash(self):
        for sep in ("\u00a0", "\u2013"):
            with self.subTest(sep=repr(sep)):
                data = OLE2 + b"\x00\x00" + ("tel " + _sg_local(sep) + " ").encode("utf-16-le") + b"\x00\x00"
                self.assertEqual(self.hits("note.doc", data), [("note.doc (text runs)", "sg_phone_local")])
        latin1 = b"%PDF-1.4\n\x00 (tel " + _sg_local("\u00a0").encode("latin-1") + b") Tj\n"
        self.assertEqual(self.hits("scan.pdf", latin1), [("scan.pdf (text runs)", "sg_phone_local")])

    def test_pdf_flate_streams_are_decompressed(self):
        email = _email()
        content = zlib.compress(("BT (contact " + email + ") Tj ET").encode("ascii"))
        objects = zlib.compress(("<< /Author (tel " + _sg_local() + ") >>").encode("ascii"))
        data = _pdf(
            (b"<< /Type /Catalog >>", None),
            (b"<< /Length %d /Filter /FlateDecode >>" % len(content), content),
            (b"<< /Type /ObjStm /N 1 /First 0 /Filter [/FlateDecode] >>", objects),
            (b"<< /Length 16 >>", b"80 0 0 80 0 0 cm"),
        )
        self.assertNotIn(email.encode("ascii"), data)
        self.assertEqual(self.hits("invoice.pdf", data), [("invoice.pdf (deflated streams)", "email"), ("invoice.pdf (deflated streams)", "sg_phone_local")])
        # Zlib is read to its own end, so a forged endstream inside the data cannot cut it short.
        forged = zlib.compress(b"q endstream Q " + ("mail " + email).encode("ascii"), 0)
        self.assertIn(("forged.pdf (deflated streams)", "email"), self.hits("forged.pdf", _pdf((b"<< /Filter /FlateDecode >>", forged))))

    def test_unreadable_pdf_streams_fail_closed(self):
        corrupt = b"\x78\x9c" + b"\xff" * 24
        self.assert_fails_closed("corrupt.pdf", _pdf((b"<< /Filter /FlateDecode >>", corrupt)))
        # The same bytes with no FlateDecode filter are not zlib data, so they are skipped.
        self.assertEqual(self.hits("raw.pdf", _pdf((b"<< /Length 26 >>", corrupt))), [])
        big = _pdf((b"<< /Filter /FlateDecode >>", zlib.compress(b"x" * 4096)))
        with mock.patch.object(self.g, "ZIP_MAX_BYTES", 1024):
            self.assert_fails_closed("big.pdf", big)

    def test_declared_flate_without_zlib_data_fails_closed(self):
        # #270 B1: the declared filter is checked before the zlib-header skip.
        text = ("BT (contact " + _email() + ") Tj ET").encode("ascii")
        packer = zlib.compressobj(wbits=-15)
        raw_deflate = packer.compress(text) + packer.flush()
        bad_header = b"\x78\x9d" + zlib.compress(text)[2:]
        self.assertFalse(self.g._zlib_header(raw_deflate, 0))
        self.assertFalse(self.g._zlib_header(bad_header, 0))
        rng = random.Random(270)
        ciphertexts = [rng.randbytes(64) for _ in range(200)]
        streams = [("badheader.pdf", bad_header), ("rawdeflate.pdf", raw_deflate)]
        streams += [("cipher%d.pdf" % index, data) for index, data in enumerate(ciphertexts)]
        for name, stream in streams:
            with self.subTest(name=name):
                declared = b"<< /Length %d /Filter /FlateDecode >>" % len(stream)
                self.assert_fails_closed(name, _pdf((b"<< /Type /Catalog >>", None), (declared, stream)))
        # The same bytes with nothing declared are not zlib data, so they are still skipped.
        self.assertEqual(self.hits("plain.pdf", _pdf((b"<< /Length 64 >>", ciphertexts[0]))), [])

    def test_encrypted_pdf_fails_closed(self):
        clean = _pdf((b"<< /Type /Catalog >>", None), (b"<< /Filter /FlateDecode >>", zlib.compress(b"BT (hello) Tj ET")))
        self.assertEqual(self.hits("clean.pdf", clean), [])
        secured = clean.replace(b"<< /Root 1 0 R >>", b"<< /Root 1 0 R /Encrypt 3 0 R >>")
        self.assertNotEqual(secured, clean)
        self.assert_fails_closed("secured.pdf", secured)
        self.assert_fails_closed("bundle.zip", _zip_bytes({"invoices/secured.pdf": secured}, zipfile.ZIP_DEFLATED))

    def test_xls_numeric_cells_are_decoded(self):
        local = int("9" + "000" + "00" + "17")
        stored = float("65" + "8" + "000" + "00" + "42")
        mulrk = _biff(0x00BD, struct.pack("<HHHIHIH", 0, 0, 0, (5 << 2) | 2, 0, (local << 2) | 2, 1))
        formula = _biff(0x0006, struct.pack("<HHHd6x", 0, 0, 0, float(local)))
        text_formula = _biff(0x0006, struct.pack("<HHH6sH6x", 0, 0, 0, bytes(6), 0xFFFF))
        for pad in (0, 700):  # the mini stream and the regular FAT
            with self.subTest(pad=pad):
                workbook = _workbook(_number(3.8), _number(stored), _rk(1), _rk(local), pad=pad)
                self.assertEqual(len(workbook) >= 4096, bool(pad))
                data = _cfb({"Workbook": workbook})
                self.assertEqual(self.g._xls_numeric_cells("book.xls", data), ["3.8", "65" + "8" + "000" + "00" + "42", "1", "9" + "000" + "00" + "17"])
                self.assertEqual(self.hits("book.xls", data), [("book.xls (numeric cells)", "sg_phone"), ("book.xls (numeric cells)", "sg_phone_local")])
        for record in (mulrk, formula):
            self.assertEqual(self.hits("cells.xls", _cfb({"Workbook": _workbook(record)})), [("cells.xls (numeric cells)", "sg_phone_local")])
        self.assertEqual(self.g._xls_numeric_cells("t.xls", _cfb({"Workbook": _workbook(text_formula)})), [])
        self.assertEqual(self.hits("clean.xls", _cfb({"Workbook": _workbook(_number(3.8), _rk(1)), "SummaryInformation": bytes(64)})), [])
        bundle = _zip_bytes({"exports/book.xls": _cfb({"Book": _workbook(_rk(local))})}, zipfile.ZIP_DEFLATED)
        self.assertEqual(self.hits("bundle.zip", bundle), [("bundle.zip!exports/book.xls (numeric cells)", "sg_phone_local")])

    def test_rk_numbers_decode(self):
        self.assertEqual(self.g._rk_value((7 << 2) | 2), 7)
        self.assertEqual(self.g._rk_value((((-5) & 0x3FFFFFFF) << 2) | 2), -5)
        self.assertAlmostEqual(self.g._rk_value((12345 << 2) | 3), 123.45)
        self.assertEqual(self.g._rk_value(0x3FF80000), 1.5)
        self.assertAlmostEqual(self.g._rk_value(0x3FF80001), 0.015)
        self.assertEqual(self.g._number_text(float("9" + "000" + "00" + "17")), "9" + "000" + "00" + "17")

    def test_unreadable_xls_fails_closed(self):
        good = _cfb({"Workbook": _workbook(_number(1.0))})

        def patched(offset, fmt, value):
            data = bytearray(good)
            struct.pack_into(fmt, data, offset, value)
            return bytes(data)

        for name, data in (
            ("encrypted.xls", _cfb({"Workbook": _workbook(_biff(0x002F, bytes(6)), _number(1.0))})),
            ("truncated.xls", _cfb({"Workbook": _biff(0x0809, bytes(16)) + struct.pack("<HH", 0x0203, 14) + bytes(4)})),
            ("notbiff.xls", _cfb({"Workbook": b"plain text that is not a workbook"})),
            ("noworkbook.xls", _cfb({"WordDocument": _workbook()})),
            ("sectorsize.xls", patched(0x1E, "<H", 10)),
            ("cutoff.xls", patched(0x38, "<I", 0)),
            ("directory.xls", patched(0x30, "<I", 99)),
            ("fatcount.xls", patched(0x2C, "<I", 500)),
            ("short.xls", good[:300]),
        ):
            with self.subTest(name=name):
                self.assert_fails_closed(name, data)
        # Word files keep the text-run scan only: no workbook is expected there.
        self.assertEqual(self.hits("letter.doc", OLE2 + bytes(32)), [])

    def test_appended_zip_is_expanded_or_fails_closed(self):
        appended = _zip_bytes({"notes.txt": "mail " + _email()}, zipfile.ZIP_DEFLATED)
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(64))
        for name, data in (
            ("poly.png", png + appended),
            ("poly.pdf", b"%PDF-1.4\n\x00\x01" + appended),
            ("poly.txt", b"\xff\xfe" + "clean\r\n".encode("utf-16-le") + appended),
        ):
            with self.subTest(name=name):
                self.assertIn((name + "!notes.txt", "email"), self.hits(name, data))
        outer = _zip_bytes({"media/poly.png": png + appended}, zipfile.ZIP_DEFLATED)
        self.assertIn(("outer.zip!media/poly.png!notes.txt", "email"), self.hits("outer.zip", outer))
        self.assert_fails_closed("broken.png", png + b"PK\x05\x06" + bytes(8))
        # An unrecognised NUL-bearing file still fails closed, appended zip or not.
        self.assert_fails_closed("data.bin", b"\x00\x01\x02" + appended)

    def test_member_with_appended_zip_keeps_its_format_decoder(self):
        # #270 H1: a zip member holding an end-of-central-directory record still gets its PDF or .xls decoder.
        email = _email()
        content = zlib.compress(("BT (contact " + email + ") Tj ET").encode("ascii"))
        pdf = _pdf((b"<< /Filter /FlateDecode >>", content))
        self.assertNotIn(email.encode("ascii"), pdf)
        clean = _zip_bytes({"readme.txt": "clean"}, zipfile.ZIP_DEFLATED)
        bundle = _zip_bytes({"docs/invoice.pdf": pdf + clean}, zipfile.ZIP_DEFLATED)
        self.assertIn(("bundle.zip!docs/invoice.pdf (deflated streams)", "email"), self.hits("bundle.zip", bundle))
        book = _cfb({"Workbook": _workbook(_rk(int("9" + "000" + "00" + "17")))})
        sheets = _zip_bytes({"exports/book.xls": book + clean}, zipfile.ZIP_DEFLATED)
        self.assertIn(("sheets.zip!exports/book.xls (numeric cells)", "sg_phone_local"), self.hits("sheets.zip", sheets))

    def test_second_zip_in_one_file_fails_closed(self):
        # #270 H2: zipfile reads only the last end-of-central-directory record, so any other zip fails closed.
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(64))
        hidden = _zip_bytes({"notes.txt": "mail " + _email()}, zipfile.ZIP_DEFLATED)
        clean = _zip_bytes({"readme.txt": "clean"}, zipfile.ZIP_DEFLATED)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("notes.txt", "mail " + _email())
            archive.comment = b"PK\x05\x06" + bytes(18)
        for name, data in (
            ("poly.png", png + hidden + clean),
            ("double.zip", hidden + clean),
            ("headers.png", png + hidden[: hidden.index(b"PK\x01\x02")] + clean),
            ("masked.zip", buffer.getvalue()),
        ):
            with self.subTest(name=name):
                self.assert_fails_closed(name, data)
        # A zip stored whole inside another (its records inside the member's data) is still expanded.
        outer = _zip_bytes({"inner.zip": hidden, "readme.txt": "clean"}, zipfile.ZIP_STORED)
        self.assertIn(("outer.zip!inner.zip!notes.txt", "email"), self.hits("outer.zip", outer))
        self.assertIn(("poly.png!notes.txt", "email"), self.hits("poly.png", png + hidden))

    def test_office_xml_inline_wrappers_are_joined(self):
        # #270 H3: wrappers around runs, the no-break hyphen and a nested rPrChange do not split text.
        head, tail = "tel " + "9" + "000 ", "00" + "17"

        def run(text, props=""):
            return '<w:r>' + props + '<w:t xml:space="preserve">' + text + "</w:t></w:r>"

        change = '<w:rPr><w:b/><w:rPrChange w:id="2" w:author="Editor"><w:rPr><w:i/></w:rPr></w:rPrChange></w:rPr>'
        cases = {
            "hyperlink": run(head) + '<w:hyperlink r:id="rId4">' + run(tail) + "</w:hyperlink>",
            "ins": run(head) + '<w:ins w:id="1" w:author="Editor">' + run(tail) + "</w:ins>",
            "smartTag": '<w:smartTag w:element="phone">' + run(head) + "</w:smartTag>" + run(tail),
            "sdt": '<w:sdt><w:sdtPr><w:alias w:val="Phone"/></w:sdtPr><w:sdtContent>' + run(head) + "</w:sdtContent></w:sdt>" + run(tail),
            "fldSimple": run(head) + '<w:fldSimple w:instr=" MERGEFIELD Phone ">' + run(tail) + "</w:fldSimple>",
            "customXml": '<w:customXml w:element="contact">' + run(head) + "</w:customXml>" + run(tail),
            "noBreakHyphen": run(head.strip()) + "<w:r><w:noBreakHyphen/></w:r>" + run(tail),
            "rPrChange": run(head) + run(tail, change),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                path = name + ".docx"
                self.assertEqual(self.hits(path, _docx("<w:p>" + body + "</w:p>")), [(path + "!word/document.xml (joined text)", "sg_phone_local")])
        # #270 G2 R2-H3: wrapper properties are stripped, so the split may fall on either edge.
        sdt_open = '<w:sdt><w:sdtPr><w:alias w:val="Phone"/><w:rPr><w:b/></w:rPr></w:sdtPr><w:sdtEndPr/><w:sdtContent>'
        cases = {
            "sdtEntering": run(head) + sdt_open + run(tail) + "</w:sdtContent></w:sdt>",
            "sdtEndPrOpen": run(head) + '<w:sdt><w:sdtEndPr><w:rPr><w:i/></w:rPr></w:sdtEndPr><w:sdtContent>' + run(tail) + "</w:sdtContent></w:sdt>",
            "smartTagEntering": run(head) + '<w:smartTag w:element="phone"><w:smartTagPr><w:attr w:name="kind" w:val="tel"/></w:smartTagPr>' + run(tail) + "</w:smartTag>",
            "customXmlEntering": run(head) + '<w:customXml w:element="contact"><w:customXmlPr><w:attr w:name="kind" w:val="tel"/></w:customXmlPr>' + run(tail) + "</w:customXml>",
            "commentRange": run(head) + '<w:commentRangeStart w:id="3"/>' + run(tail) + '<w:commentRangeEnd w:id="3"/>',
            "moveTo": run(head) + '<w:moveToRangeStart w:id="4" w:name="move1"/><w:moveTo w:id="5">' + run(tail) + '</w:moveTo><w:moveToRangeEnd w:id="4"/>',
            "softHyphen": run(head.strip()) + "<w:r><w:softHyphen/></w:r>" + run("-" + tail),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                path = name + ".docx"
                self.assertEqual(self.hits(path, _docx("<w:p>" + body + "</w:p>")), [(path + "!word/document.xml (joined text)", "sg_phone_local")])
        linked = run("pat.lee") + '<w:hyperlink r:id="rId5">' + run(AT + "synthetic-mail.sg") + "</w:hyperlink>"
        self.assertEqual(self.hits("link.docx", _docx("<w:p>" + linked + "</w:p>")), [("link.docx!word/document.xml (joined text)", "email")])

    def test_office_xml_runs_are_joined(self):
        split_phone = (
            "<w:p><w:r><w:t>tel " + "9" + "000</w:t></w:r><w:proofErr w:type=\"spellStart\"/>"
            "<w:r><w:rPr><w:b/><w:sz w:val=\"20\"/></w:rPr><w:t xml:space=\"preserve\"> " + "00" + "17</w:t></w:r></w:p>"
        )
        split_email = "<w:p><w:r><w:t>pat.lee</w:t></w:r><w:bookmarkStart w:id=\"0\"/><w:r><w:t>" + AT + "synthetic-mail.sg</w:t></w:r></w:p>"
        escaped_email = "<w:p><w:r><w:t>pat.lee&#64;synthetic-mail.sg</w:t></w:r></w:p>"
        rich = '<?xml version="1.0"?><sst><si><r><t>' + "9" + "000</t></r><r><rPr><b/></rPr><t>" + "00" + "17</t></r></si></sst>"
        member = "word/document.xml (joined text)"
        self.assertEqual(self.hits("a.docx", _docx(split_phone)), [("a.docx!" + member, "sg_phone_local")])
        self.assertEqual(self.hits("b.docx", _docx(split_email)), [("b.docx!" + member, "email")])
        self.assertEqual(self.hits("c.docx", _docx(escaped_email)), [("c.docx!" + member, "email")])
        sheet = _zip_bytes({"xl/sharedStrings.xml": rich}, zipfile.ZIP_DEFLATED)
        self.assertEqual(self.hits("d.xlsx", sheet), [("d.xlsx!xl/sharedStrings.xml (joined text)", "sg_phone_local")])
        # Separate paragraphs, cells and shared strings are not joined.
        apart = "<w:p><w:r><w:t>" + "9" + "000</w:t></w:r></w:p><w:p><w:r><w:t>" + "00" + "17</w:t></w:r></w:p>"
        self.assertEqual(self.hits("e.docx", _docx(apart)), [])
        cells = '<sheetData><row><c r="A1"><v>' + "9" + "000</v></c><c r=\"B1\"><v>" + "00" + "17</v></c></row></sheetData>"
        strings = "<sst><si><t>" + "9" + "000</t></si><si><t>" + "00" + "17</t></si></sst>"
        self.assertEqual(self.hits("f.xlsx", _zip_bytes({"xl/worksheets/sheet1.xml": cells, "xl/sharedStrings.xml": strings}, zipfile.ZIP_DEFLATED)), [])

    # #270 round 3: regressions against base found by the G2 recheck, and their family.

    def test_other_archive_members_fail_closed_before_an_appended_zip(self):
        # G2 W1: an appended zip used to take a gzip, 7z, RAR or tar member past the refusal.
        clean = _zip_bytes({"readme.txt": "clean"}, zipfile.ZIP_DEFLATED)
        gz = gzip.compress(("mail " + _email()).encode("ascii"))
        members = {
            "notes.gz": gz,
            "a.7z": b"7z\xbc\xaf\x27\x1c" + bytes(20),
            "a.rar": b"Rar!\x1a\x07\x00" + bytes(20),
            "a.tar": _tar_bytes("notes.txt", "clean"),
        }
        for name, data in members.items():
            with self.subTest(name=name):
                self.assert_fails_closed("bundle.zip", _zip_bytes({name: data + clean}, zipfile.ZIP_DEFLATED))
        nested = _zip_bytes({"inner.zip": _zip_bytes({"notes.gz": gz + clean}, zipfile.ZIP_DEFLATED)}, zipfile.ZIP_STORED)
        self.assert_fails_closed("outer.zip", nested)

    def test_wider_text_runs_keep_the_pre_270_runs(self):
        # A run widened by a no-break space or en dash must not join a hit to an opt-out
        # marker, or lengthen an international number past 15 digits.
        marker = "pii-guard: allow (x)"
        hit = "tel " + _sg_local()
        date = " " + "2026" + " " + "10" + " " + "23"
        for name, body in (
            ("ascii", hit.encode("ascii") + b"\xa0" + marker.encode("ascii")),
            ("utf16le", (hit + " " + marker).encode("utf-16-le")),
            ("utf16be", (hit + "–" + marker).encode("utf-16-be")),
            ("intl", ("Tel " + _intl() + date).encode("latin-1")),
        ):
            with self.subTest(name=name):
                data = OLE2 + b"\x00\x00" + body + b"\x00\x00"
                self.assertEqual(len(self.hits("note.doc", data)), 1)

    def test_undeclared_stream_that_will_not_inflate_is_retried_raw(self):
        # G2 R2-H1: a hidden FlateDecode declaration plus a bad Adler-32 used to skip the stream.
        email = _email()
        good = zlib.compress(("BT (contact " + email + ") Tj ET").encode("ascii"))
        bad = good[:-4] + bytes(byte ^ 0xFF for byte in good[-4:])
        tiny = zlib.compress(b"x")
        for name, dictionary in (
            ("forged.pdf", b"<< /Filter /FlateDecode /Note (stream\n" + tiny + b") >>"),
            ("nul.pdf", b"<< /Filter\x00/FlateDecode >>"),
            ("comment.pdf", b"<< /Filter % note\n/FlateDecode >>"),
        ):
            with self.subTest(name=name):
                data = _pdf((b"<< /Type /Catalog >>", None), (dictionary, bad))
                self.assertIn((name + " (deflated streams)", "email"), self.hits(name, data))
        self.assert_fails_closed("declared.pdf", _pdf((b"<< /Filter /FlateDecode >>", bad)))

    def test_pdf_name_escapes_are_decoded_for_the_name_checks(self):
        # G2 R2-H2: /Encr#79pt is /Encrypt, and /Fl#61teDecode is /FlateDecode.
        clean = _pdf((b"<< /Type /Catalog >>", None))
        self.assert_fails_closed("secured.pdf", clean.replace(b"<< /Root 1 0 R >>", b"<< /Root 1 0 R /Encr#79pt 3 0 R >>"))
        self.assert_fails_closed("escaped.pdf", _pdf((b"<< /Filt#65r /Fl#61teDecode >>", b"\xff" * 24)))
        self.assertEqual(self.hits("plain.pdf", _pdf((b"<< /Note (#41#42) >>", b"\xff" * 24))), [])

    def test_pdf_member_header_may_follow_leading_bytes(self):
        # G2 L6: readers accept a header within the first 1024 bytes.
        content = zlib.compress(("BT (contact " + _email() + ") Tj ET").encode("ascii"))
        bundle = _zip_bytes({"x.pdf": b"\n" + _pdf((b"<< /Filter /FlateDecode >>", content))}, zipfile.ZIP_DEFLATED)
        self.assertIn(("bundle.zip!x.pdf (deflated streams)", "email"), self.hits("bundle.zip", bundle))

    # #270 round 4: the last two regressions against base found by G2 recheck 2.

    def test_bom_text_member_with_an_appended_zip_keeps_its_text(self):
        # G2 W3: base's Unicode \d reads fullwidth digits in the decoded text; the raw-byte runs never do.
        clean = _zip_bytes({"readme.txt": "clean"}, zipfile.ZIP_DEFLATED)
        line = "tel " + _fullwidth_tail(_sg_local()) + "\r\n"
        for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "utf-8")):
            member = bom + ("clean\r\n" + line).encode(encoding) + clean
            for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                with self.subTest(encoding=encoding, compression=compression):
                    bundle = _zip_bytes({"n.txt": member}, compression)
                    self.assertIn(("bundle.zip!n.txt", "sg_phone_local"), self.hits("bundle.zip", bundle))
        nested = _zip_bytes({"inner.zip": _zip_bytes({"n.txt": b"\xff\xfe" + line.encode("utf-16-le") + clean}, zipfile.ZIP_DEFLATED)}, zipfile.ZIP_STORED)
        self.assertIn(("outer.zip!inner.zip!n.txt", "sg_phone_local"), self.hits("outer.zip", nested))

    def test_duplicate_document_names_fail_closed(self):
        # G2 W4: added lines are matched per document name, so a second document with the
        # same name hid (or crashed on) the first one's lines; base had the same gap.
        clean = "\n".join("line %d" % index for index in range(9))
        content = zlib.compress(("BT (contact " + _email() + ") Tj ET").encode("ascii"))
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(64))
        book = _cfb({"Workbook": _workbook(_rk(17))})
        cases = {
            "joined": [("a.xml (joined text)", clean + "\nmail " + _email()), ("a.xml", "<a/>")],
            "deflated": [("a.pdf (deflated streams)", clean + "\nmail " + _email()), ("a.pdf", _pdf((b"<< /Filter /FlateDecode >>", content)))],
            "cells": [("a.xls (numeric cells)", clean + "\nmail " + _email()), ("a.xls", book)],
            "runs": [("a.bin (text runs)", clean + "\nmail " + _email()), ("a.bin", b"\xff\xfe" + "clean".encode("utf-16-le"))],
            "bang": [("m!x", clean + "\nmail " + _email()), ("m", png + _zip_bytes({"x": "clean"}, zipfile.ZIP_DEFLATED))],
            "repeat": [("a.txt", clean + "\nmail " + _email()), ("a.txt", "clean")],
            "safe_name": [("a\x01.txt", clean + "\nmail " + _email()), ("a\x02.txt", "clean")],
        }
        for label, members in cases.items():
            with self.subTest(label=label):
                self.assert_fails_closed("bundle.zip", _zip_entries(members, zipfile.ZIP_DEFLATED))
        # Distinct names that only share a prefix still scan.
        bundle = _zip_entries([("a.xml (notes)", "mail " + _email()), ("a.xml", "<a/>")], zipfile.ZIP_DEFLATED)
        self.assertIn(("bundle.zip!a.xml (notes)", "email"), self.hits("bundle.zip", bundle))


# --------------------------------------------------------------------------- fuzz differential
#
# #270 pass bar (owner, round 4): over seeded random inputs, no input that main's scanner
# from before #270 flags or fails closed may pass this scanner clean, in tree mode or in
# range/staged mode (an added file, or a changed file against a parent blob). CI runs
# FUZZ_CASES inputs; set PII_GUARD_FUZZ_CASES for a longer run (it prints the counts).

BASE_COMMIT = "4a73583d596f1e8680b9fa3787bfc2c537397c82"
FUZZ_SEED = int(os.environ.get("PII_GUARD_FUZZ_SEED", "270"))
FUZZ_CASES = int(os.environ.get("PII_GUARD_FUZZ_CASES", "1000"))
FUZZ_MODES = ("tree", "added", "changed")
NULL_SHA, NEW_SHA, OLD_SHA = "0" * 40, "1" * 40, "2" * 40
FUZZ_SEPARATORS = (" ", "-", ".", "", "\u00a0", "&nbsp;", "\u2013", "  ")
FUZZ_SUFFIXES = (" (joined text)", " (deflated streams)", " (numeric cells)", " (text runs)")
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(64))


def _load_base_scanner():
    """main's scanner from before #270, or None when the checkout lacks that commit (shallow clone)."""
    if not HAS_GIT:
        return None
    proc = subprocess.run(["git", "show", BASE_COMMIT + ":scripts/pii_guard.py"], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pii_guard_base.py"
        path.write_bytes(proc.stdout)
        spec = importlib.util.spec_from_file_location("pii_guard_base", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _Fuzz:
    """One input's structure from ``rng``; ``drop`` (a second Random, or None) removes some lines.

    Rendering the same seed with and without ``drop`` gives a parent blob that shares the
    structure but lacks some lines, so the added-line matching is exercised too.
    """

    def __init__(self, rng, drop):
        self.rng, self.drop = rng, drop

    def digits(self, count):
        return "".join(self.rng.choice("0123456789") for _ in range(count))

    def value(self):
        rng, sep = self.rng, self.rng.choice(FUZZ_SEPARATORS)
        kind = rng.randrange(9)
        if kind == 0:
            value = rng.choice("689") + self.digits(3) + sep + self.digits(4)
        elif kind == 1:
            value = rng.choice(("+", "(+", "(")) + "6" + "5" + rng.choice((")", "")) + sep + rng.choice("3689") + self.digits(3) + sep + self.digits(4)
        elif kind == 2:
            value = "6" + "5" + sep + rng.choice("3689") + self.digits(3) + sep + self.digits(4)
        elif kind == 3:
            value = "+" + rng.choice("123456789") + "".join(sep + rng.choice(("", "(0)")) + self.digits(rng.randint(1, 5)) for _ in range(rng.randint(1, 7)))
        elif kind == 4:
            value = rng.choice(("pat.lee", "ops", "a_b")) + AT + rng.choice(("synthetic-mail", "mail.host")) + "." + rng.choice(("sg", "com", "net"))
        elif kind == 5:
            value = rng.choice(("name", "ops")) + AT + rng.choice(("example.com", "example.org", "sub.example.com"))
        elif kind == 6:
            value = "20" + self.digits(2) + sep + self.digits(2) + sep + self.digits(2)
        elif kind == 7:
            value = "".join(rng.choice("0123456789abcdef") for _ in range(rng.randint(8, 40)))
        else:
            value = self.digits(rng.randint(7, 17))
        if rng.random() < 0.25 and re.search(r"\d", value):
            value = _fullwidth_tail(value) if rng.random() < 0.7 else re.sub(r"\d", lambda match: chr(0xFF10 + int(match.group())), value)
        if rng.random() < 0.1:
            value += " pii-guard: allow (fuzz)"
        return rng.choice(("", "tel ", "id ", "(", "x", "<", "+")) + value + rng.choice(("", " ok", ")", ".", ".5", "1", "-", "/"))

    def lines(self):
        out = []
        for index in range(self.rng.randint(1, 6)):
            line = self.value() if self.rng.random() < 0.6 else "line %d" % index
            if self.drop is None or self.drop.random() >= 0.3:
                out.append(line)
        return out

    def text(self, newline="\n"):
        return newline.join(self.lines()) + self.rng.choice(("", newline))

    def bom_text(self):
        bom, encoding = self.rng.choice(((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "utf-8")))
        return bom + self.text(self.rng.choice(("\n", "\r\n"))).encode(encoding)

    def xml(self):
        paragraphs = []
        for line in self.lines():
            cuts = sorted(self.rng.sample(range(len(line) + 1), min(len(line) + 1, self.rng.randint(0, 3))))
            parts = [line[start:end] for start, end in zip([0] + cuts, cuts + [len(line)])]
            props = self.rng.choice(("", "<w:rPr><w:b/></w:rPr>", "<w:rPr><w:rPrChange><w:rPr/></w:rPrChange></w:rPr>"))
            paragraphs.append("<w:p>" + "".join("<w:r>%s<w:t>%s</w:t></w:r>" % (props, html.escape(part)) for part in parts) + "</w:p>")
        return '<?xml version="1.0"?><w:document><w:body>' + "".join(paragraphs) + "</w:body></w:document>"

    def pdf(self):
        rng, objects = self.rng, [(b"<< /Type /Catalog >>", None)]
        for line in self.lines():
            content = ("BT (" + line + ") Tj ET").encode("utf-8")
            style = rng.choice((0, 0, 0, 0, 0, 0, 1, 2, 3, 3, 4, 4))
            if style == 0:
                objects.append((b"<< /Filter /FlateDecode >>", zlib.compress(content)))
            elif style == 1:
                raw = zlib.compressobj(wbits=-15)
                objects.append((b"<< /Filter /FlateDecode >>", raw.compress(content) + raw.flush()))
            elif style == 2:
                good = zlib.compress(content)
                objects.append((rng.choice((b"<< /Filter /FlateDecode >>", b"<< /Filter % c\n/FlateDecode >>")), good[:-4] + bytes(byte ^ 0xFF for byte in good[-4:])))
            elif style == 3:
                objects.append((b"<< /Length 9 >>", content))
            else:
                objects.append((b"<< /Author (" + line.encode("utf-8") + b") >>", None))
        data = b"\n" * rng.choice((0, 0, 0, 1, 1000, 1100)) + _pdf(*objects)
        if rng.random() < 0.05:
            data = data.replace(b"<< /Root 1 0 R >>", b"<< /Root 1 0 R /Encrypt 3 0 R >>")
        return data

    def workbook(self):
        values = [int(line) for line in (re.sub(r"\D", "", self.value())[:15] for _ in range(self.rng.randint(1, 4))) if line]
        records = [_rk(value) if value < 2**29 and self.rng.random() < 0.5 else _number(float(value)) for value in values]
        if self.drop is not None:
            records = [record for record in records if self.drop.random() >= 0.3]
        return _cfb({"Workbook": _workbook(*records)})

    def member_name(self, kind, names):
        rng = self.rng
        roll = rng.random()
        if names and roll < 0.08:
            return rng.choice(names)  # repeated name
        if names and roll < 0.18:
            return rng.choice(names) + rng.choice(FUZZ_SUFFIXES + ("!x", "!readme.txt"))
        if roll < 0.21:
            return "n" + rng.choice(("\x01", "\x02", "\n")) + ".txt"
        stems = ("a", "b", "m", "word/document")
        extension = {"text": ".txt", "bom": ".txt", "xml": ".xml", "pdf": ".pdf", "xls": ".xls", "zip": ".zip", "png": ".png", "gzip": ".gz"}[kind]
        if roll < 0.31:
            # A name a later member's document may also carry: a suffixed name or a ``!`` path.
            other = rng.choice(stems) + rng.choice((".xml", ".pdf", ".xls", ".png", ".txt"))
            return other + rng.choice(FUZZ_SUFFIXES + ("!" + rng.choice(stems) + ".txt",))
        return rng.choice(stems) + (extension if rng.random() < 0.9 else ".bin")

    def body(self, kind, depth):
        if kind == "text":
            return self.text().encode("utf-8")
        if kind == "bom":
            return self.bom_text()
        if kind == "xml":
            return self.xml().encode("utf-8")
        if kind == "pdf":
            return self.pdf()
        if kind == "xls":
            return self.workbook()
        if kind == "zip":
            return self.zip(depth + 1)
        if kind == "png":
            return PNG + self.zip(depth + 1)
        return gzip.compress(self.text().encode("utf-8"), mtime=0)

    def zip(self, depth):
        kinds = ("text", "bom", "bom", "xml", "pdf", "xls") + (("zip", "png") if depth < 3 else ())
        members, names = [], []
        for _ in range(self.rng.randint(1, 4)):
            kind = self.rng.choice(kinds) if self.rng.random() < 0.97 else "gzip"
            name = self.member_name(kind, names)
            names.append(name)
            data = self.body(kind, depth)
            if self.rng.random() < (0.4 if kind == "bom" else 0.15) and depth < 3:
                # An appended zip, often clean so it cannot mask what the member's own documents miss.
                data += self.zip(depth + 1) if self.rng.random() < 0.5 else _zip_entries([("readme.txt", "clean")], zipfile.ZIP_DEFLATED)
            members.append((name, data))
        return _zip_entries(members, self.rng.choice((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)))

    def blob(self):
        """(path, bytes) for one top-level file."""
        rng = self.rng
        kind = rng.randrange(10)
        if kind == 0:
            return rng.choice(("notes.md", "a.xml", "data.csv")), self.text().encode("utf-8")
        if kind == 1:
            data = self.bom_text()
            return "notes.txt", data + (self.zip(1) if rng.random() < 0.4 else b"")
        if kind in (2, 3):
            return rng.choice(("bundle.zip", "report.docx", "sheet.xlsx", "zip.bin")), self.zip(1)
        if kind == 4:
            return "report.docx", _zip_entries([("word/document.xml", self.xml())], zipfile.ZIP_DEFLATED)
        if kind == 5:
            data = self.pdf()
            return ("scan.pdf" if data.startswith(b"%PDF") else "scan.bin"), data
        if kind == 6:
            return "book.xls", self.workbook()
        if kind == 7:
            return "image.png", PNG + self.zip(1)
        if kind == 8:
            return "letter.doc", OLE2 + b"\x00\x00" + self.text().encode("utf-16-le") + b"\x00\x00"
        return "notes.txt", self.text().encode("utf-8") + (self.zip(1) if rng.random() < 0.5 else b"")


def _fuzz_case(seed):
    """(path, new blob, parent blob) for one seed; the parent drops some of the new blob's lines."""
    path, new = _Fuzz(random.Random(seed), None).blob()
    _, old = _Fuzz(random.Random(seed), random.Random(seed + 1)).blob()
    return path, new, old


def _fuzz_outcomes(g, allow, path, new, old):
    """Per mode, ("fail", error class) or ("hits", frozenset of (kind, value)), the way main() would end."""
    blobs = {NEW_SHA: new, OLD_SHA: old}
    outcomes = {}
    for mode in FUZZ_MODES:
        try:
            if mode == "tree":
                if g.needs_blob_scan(path, new):
                    hits = {(item.kind, item.value) for item in g._scan_documents("t", g.blob_documents(path, new), allow)}
                else:
                    hits = {hit for line in g._text(new).splitlines() for hit in g.scan_line(line, allow)}
            else:
                parent = NULL_SHA if mode == "added" else OLD_SHA
                with mock.patch.object(g, "_read_blobs", lambda shas, cwd: {sha: blobs[sha] for sha in shas}):
                    handled, found = g._blob_findings([(path, NEW_SHA, [parent])], "t", None, allow)
                hits = {(item.kind, item.value) for item in found}
                if path not in handled:
                    # git diffs a NUL-free blob as text: scan the lines it reports as added.
                    before = [] if mode == "added" else g._text(old).splitlines()
                    after = g._text(new).splitlines()
                    for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                        if tag in ("insert", "replace"):
                            hits.update(hit for line in after[j1:j2] for hit in g.scan_line(line, allow))
            outcomes[mode] = ("hits", frozenset(hits))
        except Exception as exc:  # main() ends non-zero on any error: GuardError (exit 2) or a traceback
            outcomes[mode] = ("fail", exc.__class__.__name__)
    return outcomes


def _fuzz_differential(head, base, count, seed=FUZZ_SEED):
    """Counts over ``count`` inputs, with the inputs where base flags or fails and head passes clean."""
    head_allow, base_allow = head.load_allowlist(ALLOWLIST), base.load_allowlist(ALLOWLIST)
    stats = collections.Counter()
    regressions, crashes = [], []
    for case in range(count):
        path, new, old = _fuzz_case(seed * 100003 + case)
        head_out = _fuzz_outcomes(head, head_allow, path, new, old)
        base_out = _fuzz_outcomes(base, base_allow, path, new, old)
        stats["inputs"] += 1
        for mode in FUZZ_MODES:
            ours, theirs = head_out[mode], base_out[mode]
            stats["base " + ("fails" if theirs[0] == "fail" else "flags" if theirs[1] else "clean")] += 1
            stats["head " + ("fails" if ours[0] == "fail" else "flags" if ours[1] else "clean")] += 1
            if ours[0] == "fail" and ours[1] != "GuardError":
                crashes.append((case, mode, path, ours[1]))
            if (theirs[0] == "fail" or theirs[1]) and ours == ("hits", frozenset()):
                regressions.append((case, mode, path, theirs))
            elif theirs[0] == "hits" and ours[0] == "hits" and not theirs[1] <= ours[1]:
                stats["base value not in head (both flag)"] += 1
    return stats, regressions, crashes


class FuzzDifferentialTests(unittest.TestCase):
    def test_no_input_main_flags_or_fails_passes_clean(self):
        base = _load_base_scanner()
        if base is None:
            # Shallow CI checkouts lack main's commit; the pii-guard job fetches full history.
            if os.environ.get("GITHUB_WORKFLOW") == "pii-guard":
                self.fail("commit %s is missing from the pii-guard checkout" % BASE_COMMIT)
            self.skipTest("main's scanner (%s) is not in this checkout" % BASE_COMMIT[:7])
        stats, regressions, crashes = _fuzz_differential(_load_scanner(), base, FUZZ_CASES)
        if "PII_GUARD_FUZZ_CASES" in os.environ:
            sys.stderr.write("\nfuzz differential (seed %d): %s\n" % (FUZZ_SEED, dict(sorted(stats.items()))))
        self.assertEqual(regressions, [])
        self.assertEqual(crashes, [])
        # The inputs must reach every outcome, or the comparison proves nothing.
        for outcome in ("base flags", "base fails", "base clean"):
            self.assertGreater(stats[outcome], 0, outcome)


class SelfScanTests(unittest.TestCase):
    def test_guard_files_contain_no_likely_personal_data(self):
        g = _load_scanner()
        allow = g.load_allowlist(ALLOWLIST)
        for path in GUARD_FILES:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                self.assertEqual(g.scan_line(line, allow), [], "%s:%d" % (path.name, number))


# --------------------------------------------------------------------------- hook

GITATTRIBUTES_TEXT = (
    "# The tracked pre-commit hook is a POSIX shell script; CRLF would break it on Windows checkouts.\n"
    "/.githooks/** text eol=lf\n"
)
HOOK_EXEC = '    exec "$candidate" "$scanner" --staged --show-values\n'


def _assert_hook(test, text):
    test.assertTrue(text.startswith("#!/bin/sh\n"))
    test.assertNotIn("\r", text)
    test.assertIn("git config core.hooksPath .githooks", text)
    test.assertEqual(text.count("exec "), 1)
    test.assertEqual(text.count(HOOK_EXEC), 1)
    test.assertTrue(text.endswith('fail closed)." >&2\nexit 1\n'), text[-80:])
    test.assertIn('if [ ! -f "$scanner" ]; then\n  echo "pii-guard: $scanner is missing; commit blocked (fail closed)." >&2\n  exit 1\nfi\n', text)
    test.assertIn('scanner="$repo_root/scripts/pii_guard.py"\n', text)
    test.assertIn('repo_root="$(git rev-parse --show-toplevel)" || exit 1\n', text)
    for forbidden in (r"\|\|\s*true", r"\bexit 0\b", r"set \+e", "--report-only", "--range", "--tree", "--allowlist", r"(?m)^\s*:\s*$", "trap "):
        test.assertIsNone(re.search(forbidden, text), forbidden)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.text = HOOK.read_bytes().decode("utf-8")

    def test_hook_is_pinned(self):
        _assert_hook(self, self.text)
        self.assertEqual(GITATTRIBUTES.read_bytes().decode("utf-8").replace("\r\n", "\n"), GITATTRIBUTES_TEXT)

    def test_hook_guard_rejects_drift(self):
        for mutated in (
            self.text.replace("--staged --show-values", "--tree HEAD --show-values", 1),
            self.text.replace("--staged --show-values", "--staged --report-only", 1),
            self.text.replace(HOOK_EXEC, HOOK_EXEC.rstrip("\n") + " || true\n", 1),
            self.text.replace("#!/bin/sh\n", "#!/bin/sh\nexit 0\n", 1),
            self.text[: self.text.rindex("exit 1")] + "exit 0\n",
            self.text.replace("commit blocked (fail closed).\" >&2\n  exit 1\nfi", "commit blocked (fail closed).\" >&2\n  exit 0\nfi", 1),
            self.text.replace("#!/bin/sh\n", "#!/bin/sh\nset +e\n", 1),
            self.text.replace("scripts/pii_guard.py", "scripts/other.py", 1),
            self.text.replace(HOOK_EXEC, '    "$candidate" "$scanner" --staged --show-values\n', 1),
            self.text.replace("\n", "\r\n"),
        ):
            self.assertNotEqual(mutated, self.text)
            with self.assertRaises(AssertionError):
                _assert_hook(self, mutated)

    def test_hook_is_executable_in_the_index(self):
        if not HAS_GIT or not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        proc = subprocess.run(["git", "ls-files", "-s", "--", ".githooks/pre-commit"], cwd=str(ROOT), stdout=subprocess.PIPE, check=True)
        self.assertTrue(proc.stdout.decode().startswith("100755 "), proc.stdout)

    @unittest.skipUnless(HAS_GIT, "git is required")
    def test_hook_blocks_a_commit_with_staged_personal_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp, "empty.gitconfig")
            empty.write_text("", encoding="utf-8")
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(empty))
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
            repo = Path(tmp, "repo")
            for source in (SCANNER, ALLOWLIST, HOOK):
                target = repo / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            os.chmod(repo / ".githooks/pre-commit", 0o755)

            def git(*args):
                return subprocess.run(["git"] + list(args), cwd=str(repo), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

            for args in (
                ("init", "-q"),
                ("config", "user.name", "Synthetic Tester"),
                ("config", "user.email", "tester" + AT + "example.com"),
                ("config", "commit.gpgsign", "false"),
                ("config", "core.autocrlf", "false"),
                ("config", "core.hooksPath", ".githooks"),
            ):
                self.assertEqual(git(*args).returncode, 0, args)
            (repo / "clean.md").write_text("nothing personal\n", encoding="utf-8")
            self.assertEqual(git("add", ".").returncode, 0)
            done = git("commit", "-q", "-m", "clean")
            self.assertEqual(done.returncode, 0, done.stderr)
            head = git("rev-parse", "HEAD").stdout
            (repo / "leak.md").write_text("mail " + _email() + "\n", encoding="utf-8")
            git("add", "leak.md")
            blocked = git("commit", "-q", "-m", "leak")
            self.assertNotEqual(blocked.returncode, 0)
            self.assertEqual(git("rev-parse", "HEAD").stdout, head)
            (repo / "scripts/pii_guard.py").unlink()
            git("add", "-A")
            self.assertNotEqual(git("commit", "-q", "-m", "no scanner").returncode, 0)
            self.assertEqual(git("rev-parse", "HEAD").stdout, head)


# --------------------------------------------------------------------------- workflow

TRIGGER_TEXT = "\non:\n  pull_request: {}\n\npermissions:\n  contents: read\n\njobs:\n"
JOB = "scan-pr-commits"
JOB_HEAD = "  scan-pr-commits:\n    runs-on: ubuntu-latest\n    steps:\n"
STEPS = [
    "uses: actions/checkout@v4",
    "name: Assert literal exact-head checkout",
    "uses: actions/setup-python@v5",
    "name: Scan every commit in the pull-request range",
    "name: Scanner, hook and workflow drift guards",
]
CHECKOUT_TEXT = (
    "      - uses: actions/checkout@v4\n"
    "        with:\n"
    "          ref: ${{ github.event.pull_request.head.sha }}\n"
    "          fetch-depth: 0\n"
    "          persist-credentials: false\n"
)
ASSERT_TEXT = (
    "      - name: Assert literal exact-head checkout\n"
    "        shell: bash\n"
    "        env:\n"
    "          EXPECTED_SHA: ${{ github.event.pull_request.head.sha }}\n"
    "        run: |\n"
    '          ACTUAL_SHA="$(git rev-parse HEAD)"\n'
    '          echo "expected: ${EXPECTED_SHA}"\n'
    '          echo "actual:   ${ACTUAL_SHA}"\n'
    '          test -n "${EXPECTED_SHA}"\n'
    '          test "${ACTUAL_SHA}" = "${EXPECTED_SHA}"'
)
SCAN_RUN = 'run: python scripts/pii_guard.py --range "${BASE_SHA}..${HEAD_SHA}"'
SCAN_TEXT = (
    "      - name: Scan every commit in the pull-request range\n"
    "        shell: bash\n"
    "        env:\n"
    "          BASE_SHA: ${{ github.event.pull_request.base.sha }}\n"
    "          HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n"
    "        " + SCAN_RUN
)
TESTS_RUN = "run: python -m unittest tests.test_pii_guard -v"
TESTS_TEXT = "      - name: Scanner, hook and workflow drift guards\n        shell: bash\n        " + TESTS_RUN


def _step(job, name):
    marker = "      - name: " + name + "\n"
    if job.count(marker) != 1:
        raise AssertionError("step %r appears %d times" % (name, job.count(marker)))
    return marker + job.split(marker, 1)[1].split("\n      - ", 1)[0].rstrip("\n")


def _assert_workflow(test, text):
    header, jobs = text.split("\njobs:\n", 1)
    test.assertTrue(text.startswith("name: pii-guard\n"))
    test.assertIn(TRIGGER_TEXT, text)
    test.assertEqual(re.findall(r"(?m)^([a-z-]+):", text), ["name", "on", "permissions", "jobs"])
    test.assertEqual(re.findall(r"(?m)^  ([a-z0-9-]+):", jobs), [JOB])
    test.assertTrue(jobs.startswith(JOB_HEAD), jobs[: len(JOB_HEAD)])
    test.assertEqual(re.findall(r"(?m)^    ([a-z-]+):", jobs), ["runs-on", "steps"])
    test.assertEqual(re.findall(r"(?m)^      - ((?:name|uses): .*)$", jobs), STEPS)
    test.assertIn(CHECKOUT_TEXT, jobs)
    test.assertEqual(_step(jobs, "Assert literal exact-head checkout"), ASSERT_TEXT)
    test.assertEqual(_step(jobs, "Scan every commit in the pull-request range"), SCAN_TEXT)
    test.assertEqual(_step(jobs, "Scanner, hook and workflow drift guards"), TESTS_TEXT)
    test.assertTrue(jobs.endswith(TESTS_TEXT + "\n"), jobs[-120:])
    test.assertIn('          python-version: "3.12"\n', jobs)
    test.assertEqual(text.count("scripts/pii_guard.py"), 1)
    for forbidden in (
        r"(?m)^\s*if:",
        "continue-on-error",
        r"\|\|",
        r"\bexit 0\b",
        r"set \+e",
        "--report-only",
        "--show-values",
        "--staged",
        "--tree",
        "--allowlist",
        r"\bpaths(-ignore)?:",
        r"\bbranches(-ignore)?:",
        r"\btypes:",
        "github.sha",
        "fetch-depth: 1",
        "timeout-minutes",
        r"\bneeds:",
        "secrets.",
    ):
        test.assertIsNone(re.search(forbidden, text), forbidden)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_is_pinned(self):
        _assert_workflow(self, self.text)

    def test_workflow_guard_rejects_drift(self):
        job_line = "    runs-on: ubuntu-latest\n"
        scan_name = "      - name: Scan every commit in the pull-request range\n"
        for mutated in (
            self.text.replace(job_line, "    continue-on-error: true\n" + job_line, 1),
            self.text.replace(scan_name, scan_name + "        continue-on-error: true\n", 1),
            self.text.replace(SCAN_RUN, SCAN_RUN + " || true", 1),
            self.text.replace(SCAN_RUN, SCAN_RUN + "; exit 0", 1),
            self.text.replace(SCAN_RUN, "run: set +e; " + SCAN_RUN[5:], 1),
            self.text.replace(job_line, "    if: false\n" + job_line, 1),
            self.text.replace(scan_name, scan_name + "        if: ${{ false }}\n", 1),
            self.text.replace(SCAN_RUN, "run: python scripts/pii_guard.py --staged", 1),
            self.text.replace(SCAN_RUN, 'run: python scripts/pii_guard.py --range "${HEAD_SHA}~1..${HEAD_SHA}"', 1),
            self.text.replace(SCAN_RUN, 'run: python scripts/pii_guard.py --range "${HEAD_SHA}"', 1),
            self.text.replace(SCAN_RUN, "run: python scripts/pii_guard.py", 1),
            self.text.replace(SCAN_RUN, SCAN_RUN + " --report-only", 1),
            self.text.replace(SCAN_RUN, SCAN_RUN + " --show-values", 1),
            self.text.replace(SCAN_RUN, SCAN_RUN + " --allowlist /tmp/wide.json", 1),
            self.text.replace("          BASE_SHA: ${{ github.event.pull_request.base.sha }}\n", "", 1),
            self.text.replace("          BASE_SHA: ${{ github.event.pull_request.base.sha }}\n", "          BASE_SHA: ${{ github.sha }}\n", 1),
            self.text.replace("          ref: ${{ github.event.pull_request.head.sha }}\n", "", 1),
            self.text.replace("          ref: ${{ github.event.pull_request.head.sha }}\n", "          ref: ${{ github.sha }}\n", 1),
            self.text.replace("          fetch-depth: 0\n", "", 1),
            self.text.replace("          fetch-depth: 0\n", "          fetch-depth: 1\n", 1),
            self.text.replace("  pull_request: {}\n", "  pull_request:\n    paths:\n      - scripts/**\n", 1),
            self.text.replace("  pull_request: {}\n", "  pull_request:\n    branches: [main]\n", 1),
            self.text.replace("  pull_request: {}\n", "  push: {}\n", 1),
            self.text.replace("  contents: read\n", "  contents: write\n", 1),
            self.text.replace('          test -n "${EXPECTED_SHA}"\n', "", 1),
            self.text.replace(TESTS_RUN, "run: python -m unittest tests.test_pii_guard.ScanLineTests -v", 1),
            self.text.replace(TESTS_RUN, TESTS_RUN + " || true", 1),
            self.text.replace(job_line, "    timeout-minutes: 1\n" + job_line, 1),
            self.text.replace(job_line, "    needs: other\n" + job_line, 1),
            self.text.replace(
                "      - name: Scanner, hook and workflow drift guards\n",
                "      - name: Extra\n        run: echo skip\n      - name: Scanner, hook and workflow drift guards\n",
                1,
            ),
            self.text + "  second-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n",
        ):
            self.assertNotEqual(mutated, self.text)
            with self.assertRaises(AssertionError):
                _assert_workflow(self, mutated)


if __name__ == "__main__":
    unittest.main()
