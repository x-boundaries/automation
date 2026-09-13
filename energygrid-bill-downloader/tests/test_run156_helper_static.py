"""Static, in-memory, parser, and synthetic regression proof for Run156 G3."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


HELPER = (
    Path(__file__).resolve().parents[1]
    / "runtime"
    / "tools"
    / "XB141-EnergyGrid-Run156.ps1"
)
REPO_ROOT = HELPER.parents[3]
OWNED_RELATIVE_PATHS = (
    "energygrid-bill-downloader/runtime/tools/XB141-EnergyGrid-Run156.ps1",
    "energygrid-bill-downloader/tests/test_run156_helper_static.py",
)


class Run156HelperStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HELPER.read_text(encoding="utf-8")
        native_start = cls.source.index("$script:R156NativeSource = @'")
        native_end = cls.source.index("'@", native_start + 1)
        cls.native = cls.source[native_start:native_end]

    def method_body(self, signature, next_signature):
        start = self.native.index(signature)
        end = self.native.index(next_signature, start + len(signature))
        return self.native[start:end]

    def source_function(self, signature, next_signature):
        start = self.source.index(signature)
        end = self.source.index(next_signature, start + len(signature))
        return self.source[start:end]

    @staticmethod
    def config_accepts(entries):
        allowed = {
            "core.repositoryformatversion": "0",
            "core.filemode": None,
            "core.bare": "false",
            "core.logallrefupdates": "true",
            "core.symlinks": None,
            "core.ignorecase": None,
            "remote.origin.url": None,
            "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
            "branch.main.remote": "origin",
            "branch.main.merge": "refs/heads/main",
        }
        canonical = {
            "https://github.com/x-boundaries/automation",
            "https://github.com/x-boundaries/automation.git",
            "git@github.com:x-boundaries/automation",
            "git@github.com:x-boundaries/automation.git",
            "ssh://git@github.com:x-boundaries/automation",
            "ssh://git@github.com:x-boundaries/automation.git",
        }
        if len(entries) != len(allowed):
            return False
        seen = set()
        for key, value in entries:
            if key not in allowed or key in seen:
                return False
            seen.add(key)
            if key == "remote.origin.url":
                if value not in canonical:
                    return False
            elif key in {"core.filemode", "core.symlinks", "core.ignorecase"}:
                if value not in {"true", "false"}:
                    return False
            elif value != allowed[key]:
                return False
        return seen == set(allowed)

    @staticmethod
    def canonical_byte_contract(committed):
        if (
            not committed
            or b"\xef\xbb\xbf" in committed
            or b"\r" in committed
            or not committed.endswith(b"\n")
            or committed.endswith(b"\n\n")
        ):
            return False
        try:
            committed.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        if any(
            (byte < 0x20 and byte not in (0x09, 0x0A)) or byte == 0x7F
            for byte in committed
        ):
            return False
        return True

    @staticmethod
    def working_byte_contract(working):
        if not working or b"\xef\xbb\xbf" in working:
            return None
        try:
            working.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None

        has_crlf = False
        has_bare_lf = False
        for index, byte in enumerate(working):
            if byte == 0x0D:
                if index + 1 >= len(working) or working[index + 1] != 0x0A:
                    return None
                has_crlf = True
            elif byte == 0x0A:
                if index == 0 or working[index - 1] != 0x0D:
                    has_bare_lf = True
            elif (byte < 0x20 and byte != 0x09) or byte == 0x7F:
                return None
        if has_crlf and has_bare_lf:
            return None
        if not working.endswith(b"\n"):
            return None
        if has_crlf and not working.endswith(b"\r\n"):
            return None

        normalized = working.replace(b"\r\n", b"\n")
        if normalized.endswith(b"\n\n"):
            return None
        return normalized

    @classmethod
    def normalize_contract(cls, working, committed):
        if not cls.canonical_byte_contract(committed):
            return False
        normalized = cls.working_byte_contract(working)
        return normalized is not None and normalized == committed

    @staticmethod
    def git_object_sha1(object_type, raw):
        header = f"{object_type} {len(raw)}\0".encode("ascii")
        return hashlib.sha1(header + raw).hexdigest()

    @staticmethod
    def commit_headers(tree, parent):
        return (
            f"tree {tree}\n"
            f"parent {parent}\n"
            "author WJ <10020253+weijunswj@users.noreply.github.com> 0 +0000\n"
            "committer WJ <10020253+weijunswj@users.noreply.github.com> 0 +0000\n"
            "\n"
            "synthetic\n"
        ).encode("utf-8")

    @staticmethod
    def remote_record(head):
        return f"{head}\trefs/heads/main\n".encode("ascii")

    def read_head_blob(self, relative_path):
        environment = os.environ.copy()
        environment["GIT_NO_REPLACE_OBJECTS"] = "1"
        environment["GIT_NO_LAZY_FETCH"] = "1"
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", f"HEAD:{relative_path}"],
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            resolved.returncode,
            0,
            f"HEAD blob resolution failed for {relative_path}",
        )
        resolved_text = resolved.stdout.decode("ascii", errors="strict")
        match = re.fullmatch(r"([0-9a-f]{40})\r?\n", resolved_text)
        self.assertIsNotNone(match, f"invalid HEAD blob identity for {relative_path}")
        object_id = match.group(1)

        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            blob.returncode,
            0,
            f"HEAD blob read failed for {relative_path}",
        )
        return object_id, blob.stdout

    def test_helper_parses_under_windows_powershell_51_when_available(self):
        powershell = shutil.which("powershell.exe")
        if os.name != "nt" or powershell is None:
            self.skipTest("Windows PowerShell 5.1 parser is not available on this host")

        command = (
            "$tokens=$null; $errors=$null; "
            "if ([string]$PSVersionTable.PSEdition -cne 'Desktop' -or "
            "[int]$PSVersionTable.PSVersion.Major -ne 5 -or "
            "[int]$PSVersionTable.PSVersion.Minor -ne 1) { exit 2 }; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$env:R156_HELPER_PARSE_PATH,[ref]$tokens,[ref]$errors) | Out-Null; "
            "if (@($errors).Count -ne 0) { "
            "foreach ($parseError in @($errors) | Select-Object -First 8) { "
            "Write-Output (('{0}:{1}:{2}' -f "
            "[int]$parseError.Extent.StartLineNumber, "
            "[int]$parseError.Extent.StartColumnNumber, "
            "[string]$parseError.ErrorId)) }; exit 1 }; exit 0"
        )
        environment = os.environ.copy()
        environment["R156_HELPER_PARSE_PATH"] = str(HELPER)
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raw_diagnostics = result.stdout[:2048].decode("ascii", errors="ignore")
            diagnostics = [
                line
                for line in raw_diagnostics.splitlines()
                if re.fullmatch(r"[0-9]+:[0-9]+:[A-Za-z0-9_]+", line)
            ][:8]
            bounded = ", ".join(diagnostics) if diagnostics else "none"
            self.fail(
                "Windows PowerShell 5.1 parse failed; "
                f"bounded diagnostics: {bounded}"
            )

    def test_pointer_readers_extract_sids_before_freeing_native_buffers(self):
        user = self.method_body(
            "public static byte[] ReadTokenUserSid",
            "public static byte[] ReadTokenOwnerSid",
        )
        owner = self.method_body(
            "public static byte[] ReadTokenOwnerSid",
            "public static TokenGroupRecord[] ReadTokenGroups",
        )
        groups = self.method_body(
            "public static TokenGroupRecord[] ReadTokenGroups",
            "// Scalar and inline-value token classes",
        )

        for body in (user, owner, groups):
            self.assertNotIn("ReadTokenInformationBytes", body)
            self.assertLess(
                body.index("TryGetTokenInformationBuffer"),
                body.index("TryCopySidFromBuffer"),
            )
            self.assertLess(
                body.index("TryCopySidFromBuffer"),
                body.index("Marshal.FreeHGlobal(buffer)"),
            )
        self.assertIn("return sidBytes", user)
        self.assertIn("return sidBytes", owner)
        self.assertIn("result[index].Sid = sidBytes", groups)

        self.assertIn("returnedLength < structureLength", user)
        self.assertIn("returnedLength < structureLength", owner)
        self.assertIn("countOffset", groups)
        self.assertIn("entriesLength", groups)
        self.assertIn("TryCopySidFromBuffer", groups)

    def test_sid_pointer_and_structure_bounds_fail_closed(self):
        self.assertIn("targetAddress < bufferAddress", self.native)
        self.assertIn("targetAddress > bufferEnd - (long)minimumLength", self.native)
        self.assertIn("returned <= 0", self.native)
        self.assertIn("returned > required", self.native)
        self.assertIn("subAuthorityCount > SID_MAX_SUB_AUTHORITIES", self.native)
        self.assertIn("!HasBufferRange(bufferLength, sidOffset, sidLength)", self.native)
        self.assertIn("count > 65536", self.native)
        self.assertIn("!HasBufferRange(returnedLength, firstOffset, entriesLength)", self.native)
        self.assertIn("catch {\n                return null;", self.native)

    def test_local_and_remote_git_wrappers_are_distinct(self):
        self.assertEqual(self.source.count("function Invoke-R156LocalGitRead {"), 1)
        self.assertEqual(self.source.count("function Invoke-R156RemoteHeadProof {"), 1)
        self.assertNotIn("function Invoke-R156Git {", self.source)
        self.assertIn("Test-R156LocalGitArguments", self.source)
        self.assertIn("Test-R156RemoteGitArguments", self.source)
        self.assertIn("R156LocalGitReadOnlySubcommands", self.source)
        self.assertIn("R156RemoteGitReadOnlySubcommands", self.source)

    def test_absolute_git_and_gcm_trust_anchor_is_frozen(self):
        self.assertEqual(
            self.source.count(
                "$startInfo.FileName = [string]$script:R156FrozenGitPath"
            ),
            2,
        )
        self.assertNotIn("$startInfo.FileName = 'git.exe'", self.source)
        self.assertNotIn("FileName='git.exe'", self.source)
        self.assertNotIn("credential.helper=manager", self.source)
        self.assertIn("git-credential-manager.exe", self.source)
        self.assertIn("Get-R156CredentialHelperConfig", self.source)
        self.assertIn("'credential.helper='", self.source)
        self.assertIn("credential.helper=\"", self.source)
        self.assertIn("ProgramFiles", self.source)
        self.assertIn("ProgramFilesX86", self.source)
        self.assertIn("Test-R156ProtectedInstallationRoot", self.source)
        self.assertIn("FileSystemRights", self.source)
        self.assertIn("Test-R156NoReparseAncestors", self.source)
        self.assertNotIn("Get-Command", self.source)
        self.assertNotIn("$env:PATH", self.source)

    def test_git_process_environment_and_output_are_bounded(self):
        local = self.source_function(
            "function Invoke-R156LocalGitRead",
            "function Get-R156RemoteWorkingDirectory",
        )
        remote = self.source_function(
            "function Invoke-R156RemoteHeadProof",
            "function Get-R156RepositoryState",
        )
        for wrapper in (local, remote):
            self.assertIn("Where-Object { ([string]$_) -like 'GIT_*' }", wrapper)
            self.assertIn("EnvironmentVariables.Remove", wrapper)
            self.assertIn("RedirectStandardOutput", wrapper)
            self.assertIn("RedirectStandardError", wrapper)
            self.assertIn("GIT_TERMINAL_PROMPT", wrapper)
        self.assertIn("Where-Object { ([string]$_) -like 'GCM_*' }", remote)
        self.assertIn("GCM_INTERACTIVE", remote)
        self.assertIn("StderrDiscarded", self.source)
        self.assertIn("ReadAsync", self.source)
        self.assertIn("MaximumOutputBytes", self.source)
        self.assertIn("TimedOut", self.source)
        self.assertIn("Overflow", self.source)
        self.assertIn("process.Kill", self.source)
        self.assertIn("process.ExitCode -eq 0", self.source)

    def test_remote_discovery_isolation_is_fixed_and_repository_free(self):
        remote = self.source_function(
            "function Get-R156RemoteWorkingDirectory",
            "function Invoke-R156RemoteHeadProof",
        )
        self.assertIn("[Environment+SpecialFolder]::Windows", remote)
        self.assertIn("StartingDirectory", remote)
        self.assertIn("CeilingDirectory", remote)
        self.assertIn("[System.IO.Path]::GetPathRoot", remote)
        self.assertIn("EnvironmentVariables['GIT_CEILING_DIRECTORIES']", self.source)
        self.assertIn("File]::Exists($marker)", remote)
        self.assertIn("Directory]::Exists($marker)", remote)
        self.assertNotIn("GIT_DIR=NUL", self.source)
        self.assertNotIn("['GIT_DIR']", self.source)
        self.assertIn(
            "'https://github.com/x-boundaries/automation.git'",
            self.source,
        )
        self.assertNotIn("RemoteOrigin", self.source)

    def test_local_git_command_matrix_is_exact_and_read_only(self):
        contract = self.source_function(
            "function Test-R156LocalGitArguments",
            "function Test-R156RemoteGitArguments",
        )
        for marker in (
            "symbolic-ref",
            "'--short'",
            "'-q'",
            "'HEAD'",
            "rev-parse",
            "'--verify'",
            "'--show-toplevel'",
            "'--is-inside-work-tree'",
            "'--git-dir'",
            "'--git-common-dir'",
            "status",
            "'--porcelain=v1'",
            "'--untracked-files=all'",
            "'--ignore-submodules=none'",
            "config",
            "'--no-includes'",
            "'--null'",
            "'--list'",
            "cat-file",
            "'commit'",
            "'blob'",
        ):
            self.assertIn(marker, contract)
        self.assertIn(
            "'--git-dir=C:\\XB\\automation\\.git'",
            self.source,
        )
        self.assertIn(
            "'--work-tree=C:\\XB\\automation'",
            self.source,
        )
        for marker in (
            "'--no-optional-locks'",
            "'--no-replace-objects'",
            "'--no-lazy-fetch'",
            "'--literal-pathspecs'",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("rev-list", self.source)
        self.assertNotIn("hash-object", self.source)
        self.assertNotIn("HEAD^{tree}", self.source)

    def test_remote_command_and_helper_scope_are_fixed(self):
        remote = self.source_function(
            "function Invoke-R156RemoteHeadProof",
            "function Get-R156RepositoryState",
        )
        for marker in (
            "'ls-remote'",
            "'--quiet'",
            "'--refs'",
            "'--exit-code'",
            "'refs/heads/main'",
            "credential.interactive=false",
            "protocol.allow=never",
            "protocol.https.allow=always",
            "credential.helper=",
            "GIT_ALLOW_PROTOCOL",
            "'GCM_INTERACTIVE'] = '0'",
        ):
            self.assertIn(marker, remote)
        self.assertIn(
            "GIT_CONFIG_NOSYSTEM",
            remote,
        )
        self.assertIn("GIT_CONFIG_SYSTEM", remote)
        self.assertIn("GIT_CONFIG_GLOBAL", remote)

    def test_config_admission_rejects_includes_url_rewrites_and_unknown_keys(self):
        base = [
            ("core.repositoryformatversion", "0"),
            ("core.filemode", "false"),
            ("core.bare", "false"),
            ("core.logallrefupdates", "true"),
            ("core.symlinks", "false"),
            ("core.ignorecase", "true"),
            ("remote.origin.url", "https://github.com/x-boundaries/automation.git"),
            ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ("branch.main.remote", "origin"),
            ("branch.main.merge", "refs/heads/main"),
        ]
        self.assertTrue(self.config_accepts(base))
        for replacement in (
            ("include.path", "C:\\user\\config"),
            ("includeIf.gitdir", "C:\\user\\config"),
            ("url.https://evil/.insteadOf", "https://github.com/x-boundaries/automation.git"),
            ("credential.helper", "manager"),
            ("core.fsmonitor", "true"),
            ("remote.origin.promisor", "true"),
        ):
            mutated = list(base)
            mutated[1] = replacement
            self.assertFalse(self.config_accepts(mutated))
        self.assertIn("Test-R156ConfigAdmission", self.source)
        self.assertIn("acceptedKeys", self.source)
        self.assertIn("'--file=C:\\XB\\automation\\.git\\config'", self.source)
        self.assertIn("'--no-includes'", self.source)

    def test_status_is_side_effect_free_and_index_is_held(self):
        status = self.source_function(
            "function Get-R156RepositoryState",
            "function Resolve-R156RepositoryPath",
        )
        for marker in (
            "core.fsmonitor=false",
            "core.untrackedCache=false",
            "core.hooksPath=NUL",
            "submodule.recurse=false",
            "'--ignore-submodules=none'",
            "R156IndexHandle",
            "indexMetadataBefore",
            "indexMetadataAfter",
            "indexDigestBefore",
            "indexDigestAfter",
            "indexSideEffectFree",
            "FileShare]::Read",
            "GIT_OPTIONAL_LOCKS",
        ):
            self.assertIn(marker, status + self.source)
        self.assertIn("R156RepositoryConfigHandle", status)
        self.assertIn("ConfigAdmission", status)

    def test_cat_file_rejects_filters_textconv_mailmap_and_arbitrary_objects(self):
        contract = self.source_function(
            "function Test-R156LocalGitArguments",
            "function Test-R156RemoteGitArguments",
        )
        self.assertIn("R156LibraryGitBlob", contract)
        self.assertIn("R156InstallerGitBlob", contract)
        self.assertIn("R156LauncherGitBlob", contract)
        for marker in (
            "--filters",
            "--textconv",
            "--follow-symlinks",
            "--mailmap",
            "arbitrary",
        ):
            self.assertNotIn(marker, contract)
        self.assertIn("ExpectedHeadAtExecution", contract)
        self.assertIn("'cat-file', 'commit'", self.source)
        self.assertIn("'cat-file', 'blob'", self.source)

    def test_raw_commit_tree_parent_and_blob_hash_proofs_are_present(self):
        proof = self.source_function(
            "function Get-R156CommitTreeParentProof",
            "function Close-R156TrackedHandles",
        )
        for marker in (
            "RawCommitBytes",
            "Get-R156Sha1ForGitObject",
            "CommitObjectHash",
            "headerText",
            "allowedHeaders",
            "counts",
            "'tree'",
            "'parent'",
            "ExpectedTreeValue",
            "ExpectedParentValue",
            "ParentCount",
        ):
            self.assertIn(marker, proof)
        tree = "a" * 40
        parent = "b" * 40
        raw = self.commit_headers(tree, parent)
        self.assertEqual(self.git_object_sha1("commit", raw), self.git_object_sha1("commit", raw))
        headers = raw.split(b"\n\n", 1)[0].decode("utf-8").splitlines()
        self.assertEqual([line.split(" ", 1)[1] for line in headers if line.startswith("tree ")], [tree])
        self.assertEqual([line.split(" ", 1)[1] for line in headers if line.startswith("parent ")], [parent])
        duplicate_parent = raw.replace(
            f"parent {parent}\n".encode(),
            f"parent {parent}\nparent {parent}\n".encode(),
        )
        self.assertEqual(
            len(re.findall(r"^parent [0-9a-f]{40}$", duplicate_parent.decode(), re.MULTILINE)),
            2,
        )

        source = self.source_function(
            "function Read-R156TrustedSource",
            "function Read-R156Locator",
        )
        for marker in (
            "rev-parse",
            "ExpectedGitBlob",
            "cat-file",
            "committedBytes",
            "Get-R156Sha1ForGitObject -Type 'blob'",
            "workingBytes",
            "NormalizedBytes",
            "Test-R156ByteArraysEqual",
            "Test-R156PowerShellParse -Path $workingPath -Bytes $workingBytes",
            "R156SourceHandles",
        ):
            self.assertIn(marker, source)

    def test_byte_contract_synthetic_matrix(self):
        committed = b"alpha\nbeta\n"
        canonical_cases = (
            ("valid uniform LF", committed, True),
            ("CRLF is not canonical", b"alpha\r\nbeta\r\n", False),
            ("BOM", b"\xef\xbb\xbf" + committed, False),
            ("invalid UTF-8", b"alpha\n\xff\n", False),
            ("NUL", b"alpha\x00\n", False),
            ("EOT", b"alpha\x04\n", False),
            ("other forbidden C0 control", b"alpha\x01\n", False),
            ("DEL", b"alpha\x7f\n", False),
            ("missing terminal LF", b"alpha\nbeta", False),
            ("surplus terminal LF", b"alpha\nbeta\n\n", False),
        )
        for label, candidate, expected in canonical_cases:
            with self.subTest(contract="canonical", case=label):
                self.assertEqual(self.canonical_byte_contract(candidate), expected)

        working_cases = (
            ("valid uniform LF", committed, True),
            ("valid uniform CRLF", b"alpha\r\nbeta\r\n", True),
            ("mixed line endings", b"alpha\r\nbeta\n", False),
            ("lone CR", b"alpha\rbeta\n", False),
            ("BOM", b"\xef\xbb\xbf" + committed, False),
            ("invalid UTF-8", b"alpha\n\xff\n", False),
            ("NUL", b"alpha\x00\n", False),
            ("EOT", b"alpha\x04\n", False),
            ("other forbidden C0 control", b"alpha\x01\n", False),
            ("DEL", b"alpha\x7f\n", False),
            ("missing terminal LF", b"alpha\nbeta", False),
            ("surplus terminal LF", b"alpha\nbeta\n\n", False),
        )
        for label, candidate, expected in working_cases:
            with self.subTest(contract="working", case=label):
                self.assertEqual(self.normalize_contract(candidate, committed), expected)

        self.assertFalse(self.normalize_contract(b"alpha\n", b"omega\n"))
        for marker in (
            "Test-R156CommittedByteContract",
            "Test-R156WorkingByteContract",
            "Convert-R156WorkingBytesToLf",
            "NormalizedBytes",
            "Ending = if ($hasCrlf)",
            "UTF8Encoding($false, $true)",
            "0xEF",
            "0x0D",
            "0x0A",
        ):
            self.assertIn(marker, self.source)

    def test_source_and_repository_handles_deny_replacement(self):
        for marker in (
            "Open-R156ReadOnlyHandle",
            "FileMode]::Open",
            "FileAccess]::Read",
            "FileShare]::Read",
            "R156RepositoryConfigHandle",
            "R156SourceHandles",
            "R156IndexHandle",
            "Test-R156NoReparseAncestors",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("FileShare]::ReadWrite", self.source)
        self.assertNotIn("FileShare]::Delete", self.source)

    def test_malformed_extra_output_overflow_timeout_and_nonzero_fail_closed(self):
        head = "0" * 40
        self.assertEqual(self.remote_record(head), f"{head}\trefs/heads/main\n".encode())
        self.assertNotEqual(
            self.remote_record(head) + b"extra\n",
            self.remote_record(head),
        )
        for marker in (
            "Test-R156SingleGitLineBytes",
            "Convert-R156ConfigBytes",
            "StdoutBytes",
            "StderrDiscarded",
            "MaximumOutputBytes",
            "TimedOut",
            "Overflow",
            "ExitCode",
            "Success",
            "process.Kill",
            "RedirectStandardOutput",
            "RedirectStandardError",
        ):
            self.assertIn(marker, self.source)

    def test_frozen_boundaries_and_dispatch_protocol_remain_unchanged(self):
        transport = self.source_function(
            "function Invoke-R156Transport",
            "function Test-R156PrivateBindingsOutsideCheckout",
        )
        self.assertIn("Where-Object { ([string]$_) -like 'GIT_*' }", transport)
        self.assertIn("EnvironmentVariables.Remove", transport)
        collect = transport.index("$inheritedGitNames = @(")
        remove = transport.index("foreach ($name in $inheritedGitNames)")
        child_binding = transport.index("$startInfo.EnvironmentVariables['EG_R156_MODE']")
        start = transport.index("$started = [bool]$process.Start()")
        self.assertLess(collect, remove)
        self.assertLess(remove, child_binding)
        self.assertLess(child_binding, start)
        self.assertEqual(self.source.count("Invoke-R156Transport -Mode 'REAL'"), 1)
        self.assertIn("$script:R156RealInstallerInvocations = 1", self.source)
        self.assertIn("$script:R156AuthorityConsumed = 'YES'", self.source)
        self.assertIn("'CONTROLLER_REQUIRED_POST_DISPATCH'", self.source)
        self.assertIn("$fields['retry_allowed'] = 'NO'", self.source)

        self.assertNotIn("R156ExpectedParentForPreimage", self.source)
        stale_calls = re.findall(
            r"Read-R156ManifestState\s+-LauncherRoot\s+[^\r\n]+?-ExpectedAdmission\s+\$script:R156ExpectedParentAtExecution",
            self.source,
        )
        self.assertEqual(len(stale_calls), 3)
        fence_calls = re.findall(
            r"Test-R156RepositoryFence\s+-State\s+[^\r\n]+",
            self.source,
        )
        self.assertEqual(len(fence_calls), 4)

    def test_head_blobs_are_canonical_for_both_owned_files(self):
        for relative_path in OWNED_RELATIVE_PATHS:
            with self.subTest(path=relative_path):
                object_id, committed = self.read_head_blob(relative_path)
                self.assertTrue(self.canonical_byte_contract(committed))
                self.assertEqual(self.git_object_sha1("blob", committed), object_id)

    def test_working_tree_bytes_match_verified_head_blobs_after_normalization(self):
        for relative_path in OWNED_RELATIVE_PATHS:
            with self.subTest(path=relative_path):
                _, committed = self.read_head_blob(relative_path)
                working = (REPO_ROOT / Path(relative_path)).read_bytes()
                normalized = self.working_byte_contract(working)
                self.assertIsNotNone(normalized)
                self.assertEqual(normalized, committed)



if __name__ == "__main__":
    unittest.main()
