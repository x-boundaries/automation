"""Static regression proof for the bounded Run156 helper repair.

These tests inspect source only. They do not dot-source or execute the helper, installer,
launcher, browser, portal, Scheduler, n8n, or AutoCount paths.
"""

from pathlib import Path
import re
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

    def test_git_reads_are_sanitised_and_read_only(self):
        self.assertIn("$script:R156GitReadOnlySubcommands", self.source)
        self.assertIn("foreach ($argument in @('--no-optional-locks') + @($Arguments))", self.source)
        self.assertIn("Where-Object { ([string]$_) -like 'GIT_*' }", self.source)
        self.assertIn("EnvironmentVariables.Remove", self.source)
        self.assertIn("['GIT_TERMINAL_PROMPT'] = '0'", self.source)
        self.assertIn("['GIT_OPTIONAL_LOCKS'] = '0'", self.source)
        self.assertIn("['GIT_CONFIG_NOSYSTEM'] = '1'", self.source)
        self.assertIn("['GIT_CONFIG_GLOBAL'] = 'NUL'", self.source)
        self.assertIn("'config', '--no-includes', '--local', '--get-all', 'remote.origin.url'", self.source)

        self.assertLess(
            self.source.index("foreach ($name in $inheritedGitNames)"),
            self.source.index("$startInfo.EnvironmentVariables['GIT_TERMINAL_PROMPT']"),
        )
        self.assertLess(
            self.source.index("$startInfo.EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'"),
            self.source.index("[void]$process.Start()"),
        )

        calls = re.findall(
            r"Invoke-R156Git\s+-RepositoryRoot\s+[^\r\n]+?-Arguments\s+@\('([^']+)'",
            self.source,
        )
        self.assertTrue(calls)
        self.assertTrue(
            set(calls).issubset(
                {
                    "symbolic-ref",
                    "rev-parse",
                    "rev-list",
                    "status",
                    "ls-remote",
                    "hash-object",
                    "cat-file",
                    "config",
                }
            )
        )

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
