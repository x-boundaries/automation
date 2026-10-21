"""Personal-data guard: scanner behaviour plus hook and workflow drift guards (#211).

Every email address and phone number in this module is synthetic and assembled
at runtime from fragments, so the file never contains a literal real-looking
value and the guard does not flag its own tests (proved by the self-scan test).
"""

import contextlib
import gzip
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
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


def _xlsx(cell, compression=zipfile.ZIP_DEFLATED):
    return _zip_bytes(
        {
            "[Content_Types].xml": '<?xml version="1.0"?><Types/>',
            "xl/workbook.xml": '<?xml version="1.0"?><workbook><sheets/></workbook>',
            "xl/sharedStrings.xml": '<?xml version="1.0"?><sst><si><t>Name</t></si><si><t>' + cell + "</t></si></sst>",
        },
        compression,
    )


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

    def test_not_a_repository_fails_closed(self):
        outside = Path(self.tmp.name, "plain")
        outside.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.tmp.name}):
            code = self.g.main(["--staged"], cwd=outside, out=io.StringIO(), err=io.StringIO())
        self.assertEqual(code, 2)


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
