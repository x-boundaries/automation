"""Regression proof for the bounded Run156 helper repair.

These tests inspect source and, on Windows, perform parser-only validation. They do not
dot-source or execute the helper, installer, launcher, browser, portal, Scheduler, n8n,
AutoCount, or Git mutation paths.
"""

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
    def hash_object_shape(args):
        """Mirror the source's exact hash-object shape for a no-write challenge."""
        if len(args) != 3 or args[0] != "hash-object":
            return False
        if not args[1].startswith("--path="):
            return False
        path = args[1][len("--path=") :]
        if not path or path.startswith(("/", "-")) or "\\" in path or "//" in path:
            return False
        segments = path.split("/")
        if any(
            not segment
            or segment in {".", ".."}
            or re.fullmatch(r"[A-Za-z0-9._-]+", segment) is None
            for segment in segments
        ):
            return False
        return args[2] == path

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
            "if (@($errors).Count -ne 0) { exit 1 }; exit 0"
        )
        environment = os.environ.copy()
        environment["R156_HELPER_PARSE_PATH"] = str(HELPER)
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, "Windows PowerShell 5.1 parse failed")

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

    def test_git_wrapper_enforces_central_exact_read_only_contract(self):
        contract = self.source_function(
            "function Test-R156GitReadOnlyArguments",
            "function Invoke-R156Git",
        )
        wrapper = self.source_function(
            "function Invoke-R156Git",
            "function Get-R156RepositoryState",
        )

        self.assertEqual(self.source.count("$startInfo.FileName = 'git.exe'"), 1)
        self.assertIn("Test-R156GitReadOnlyArguments -Arguments $Arguments", wrapper)
        self.assertIn("$script:R156GitReadOnlySubcommands", contract)
        self.assertIn("Test-R156GitPathToken", contract)

        exact_markers = (
            "$items.Count -eq 4",
            "'--short'",
            "'-q'",
            "$items.Count -eq 5",
            "'--parents'",
            "'--porcelain=v1'",
            "'--untracked-files=all'",
            "'--no-includes'",
            "'--local'",
            "'--get-all'",
            "'remote.origin.url'",
            "'refs/heads/main'",
            "$items.Count -ne 3",
            "StartsWith('--path='",
            "'cat-file'",
            "'-s'",
            "'--verify'",
            "'HEAD^{tree}'",
            "'--show-toplevel'",
            "'--is-inside-work-tree'",
            "'--git-dir'",
            "'--git-common-dir'",
        )
        for marker in exact_markers:
            self.assertIn(marker, contract)

        self.assertIn("'--no-optional-locks'", wrapper)
        self.assertNotIn("hash-object', '-w'", self.source)

    def test_hash_object_write_flag_challenge_is_rejected_without_running_git(self):
        valid = [
            "hash-object",
            "--path=energygrid-bill-downloader/runtime/launcher.ps1",
            "energygrid-bill-downloader/runtime/launcher.ps1",
        ]
        self.assertTrue(self.hash_object_shape(valid))
        self.assertFalse(self.hash_object_shape(valid + ["-w"]))
        self.assertFalse(
            self.hash_object_shape(
                [
                    "hash-object",
                    "-w",
                    "--path=energygrid-bill-downloader/runtime/launcher.ps1",
                    "energygrid-bill-downloader/runtime/launcher.ps1",
                ]
            )
        )

        contract = self.source_function(
            "function Test-R156GitReadOnlyArguments",
            "function Invoke-R156Git",
        )
        hash_branch = contract[
            contract.index("if ($command -ceq 'hash-object')") :
            contract.index("if ($command -ceq 'cat-file')")
        ]
        self.assertIn("$items.Count -ne 3", hash_branch)
        self.assertIn("Test-R156GitPathToken -Value $path", hash_branch)
        self.assertIn("[string]$items[2] -ceq $path", hash_branch)

    def test_git_environment_remote_and_credential_isolation(self):
        wrapper = self.source_function(
            "function Invoke-R156Git",
            "function Get-R156RepositoryState",
        )
        remote = self.source_function(
            "function Test-R156RemoteHead",
            "function Get-R156SourceBytes",
        )

        self.assertIn("Where-Object { ([string]$_) -like 'GIT_*' }", wrapper)
        self.assertIn("EnvironmentVariables.Remove", wrapper)
        self.assertIn("['GIT_TERMINAL_PROMPT'] = '0'", wrapper)
        self.assertIn("['GIT_OPTIONAL_LOCKS'] = '0'", wrapper)
        self.assertIn("['GIT_CONFIG_NOSYSTEM'] = '1'", wrapper)
        self.assertIn("['GIT_CONFIG_GLOBAL'] = 'NUL'", wrapper)
        self.assertNotIn("['GIT_CONFIG_COUNT']", wrapper)
        self.assertNotIn("['GIT_CONFIG_KEY_0']", wrapper)
        self.assertNotIn("['GIT_CONFIG_VALUE_0']", wrapper)

        reset = wrapper.index("'-c', 'credential.helper='")
        manager = wrapper.index("'-c', 'credential.helper=manager'")
        start = wrapper.index("[void]$process.Start()")
        self.assertLess(reset, manager)
        self.assertLess(manager, start)

        self.assertIn("$remoteProbe = ([string]$Arguments[0] -ceq 'ls-remote')", wrapper)
        self.assertIn("[Environment+SpecialFolder]::Windows", wrapper)
        self.assertIn("['GIT_CEILING_DIRECTORIES'] = $isolatedWorkingDirectory", wrapper)
        self.assertIn("Test-R156CanonicalOrigin -Origin @($RemoteOrigin)", remote)
        self.assertIn("'ls-remote', $RemoteOrigin, 'refs/heads/main'", remote)
        self.assertNotIn("'ls-remote', 'origin'", remote)
        self.assertIn("$initialState.Origin[0]", self.source)
        self.assertIn("$beforeRealState.Origin[0]", self.source)
        self.assertEqual(self.source.count("Test-R156RemoteHead -RepositoryRoot"), 2)

    def test_transport_strips_all_inherited_git_environment_before_child_start(self):
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

    def test_repository_identity_is_part_of_every_repository_fence(self):
        for marker in (
            "'rev-parse', '--show-toplevel'",
            "'rev-parse', '--is-inside-work-tree'",
            "'rev-parse', '--git-dir'",
            "'rev-parse', '--git-common-dir'",
            "Test-R156CanonicalOrigin",
            "Test-R156RepositoryIdentity",
            "State.CommonDirectory",
            "State.Origin",
        ):
            self.assertIn(marker, self.source)

        fence_calls = re.findall(
            r"Test-R156RepositoryFence\s+-State\s+[^\r\n]+",
            self.source,
        )
        self.assertEqual(len(fence_calls), 4)
        for call in fence_calls:
            self.assertIn("-RepositoryRoot", call)

    def test_stale_preimage_checks_bind_only_to_execution_parent(self):
        self.assertNotIn("R156ExpectedParentForPreimage", self.source)
        stale_calls = re.findall(
            r"Read-R156ManifestState\s+-LauncherRoot\s+[^\r\n]+?-ExpectedAdmission\s+\$script:R156ExpectedParentAtExecution",
            self.source,
        )
        self.assertEqual(len(stale_calls), 3)

    def test_real_dispatch_is_one_shot_and_post_dispatch_is_non_retryable(self):
        self.assertEqual(self.source.count("Invoke-R156Transport -Mode 'REAL'"), 1)
        self.assertIn("$script:R156RealInstallerInvocations = 1", self.source)
        self.assertIn("$script:R156AuthorityConsumed = 'YES'", self.source)
        self.assertIn("if ($script:R156RealStarted)", self.source)
        self.assertIn("'CONTROLLER_REQUIRED_POST_DISPATCH'", self.source)
        self.assertIn("$fields['retry_allowed'] = 'NO'", self.source)


if __name__ == "__main__":
    unittest.main()
