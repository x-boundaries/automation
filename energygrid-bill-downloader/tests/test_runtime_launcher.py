"""Offline tests for the source-controlled Energy@Grid runtime layer.

Design lock: DL-XB-141-RUNTIME-005-SOURCE-DURABILITY.
Controlling specification: docs/runtime_source_durability_design.md.
Controlling plan: docs/runtime_source_durability_implementation_plan.md.

No test in this module contacts the Energy@Grid portal, the production server, the
production launcher root, a real DPAPI credential artefact, Task Scheduler, or n8n. Every
filesystem behaviour is exercised in a per-test scratch directory, every credential is a
per-run synthetic throwaway, and the application itself is never invoked: only a scratch
child stub is.

Three tiers, per design section 12.1:

* Tier A, portable library tests, run under whichever PowerShell the host provides.
* Tier B, compatibility-boundary tests, pinned to Windows PowerShell 5.1 (PSEdition
  Desktop), because that is the production runtime and the runtime on which the
  File.Replace behaviour in design section 7.3 was observed.
* Tier C, static guard tests, which read the committed runtime files as text and as a
  parsed abstract syntax tree and assert structural properties no dynamic test can
  guarantee across runtimes.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_ROOT / "runtime"
LIB = RUNTIME_DIR / "launcher_lib.ps1"
LAUNCHER = RUNTIME_DIR / "launcher.ps1"
INSTALLER = RUNTIME_DIR / "install_or_update_launcher.ps1"
RUNTIME_PS1_FILES = (LIB, LAUNCHER, INSTALLER)

EXAMPLE_SETTINGS = RUNTIME_DIR / "launcher.settings.example.json"
RUNTIME_README = RUNTIME_DIR / "README.md"


def existing_runtime_ps1_files():
    """Every runtime .ps1 that exists right now, so early tasks run before later ones."""
    return tuple(path for path in RUNTIME_PS1_FILES if path.is_file())


# --------------------------------------------------------------------------------------
# Scratch directory helper
# --------------------------------------------------------------------------------------

def _force_remove_tree(root):
    """Remove a scratch tree, clearing read-only attributes a test deliberately set."""
    def _onerror(func, path, _excinfo):
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass

    for child in sorted(Path(root).rglob("*"), reverse=True):
        try:
            if child.is_file() or child.is_symlink():
                os.chmod(child, 0o700)
        except OSError:
            pass
    shutil.rmtree(root, onerror=_onerror)


class TemporaryScratch:
    """A per-test scratch directory that is always removed, even when marked read-only."""

    def __init__(self):
        self._path = None

    def __enter__(self):
        self._path = Path(tempfile.mkdtemp(prefix="eg_runtime_"))
        return self._path

    def __exit__(self, exc_type, exc, tb):
        if self._path is None:
            return False
        _force_remove_tree(self._path)
        self._path = None
        return False


# --------------------------------------------------------------------------------------
# Interpreter discovery
# --------------------------------------------------------------------------------------

def _probe_edition(exe):
    """Return the PSEdition string an interpreter reports, or None when it cannot run."""
    try:
        completed = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSEdition"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def find_any_powershell():
    """Tier A host: pwsh, then powershell, then powershell.exe. None when absent."""
    for candidate in ("pwsh", "powershell", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved and _probe_edition(resolved) is not None:
            return resolved
    return None


def find_desktop_powershell():
    """Tier B host: powershell.exe whose $PSVersionTable.PSEdition is 'Desktop'.

    The path is returned only after a probe confirms the Desktop edition, so a pwsh shim
    named powershell.exe cannot masquerade as the Windows PowerShell 5.1 boundary.
    """
    for candidate in ("powershell.exe", "powershell"):
        resolved = shutil.which(candidate)
        if resolved and _probe_edition(resolved) == "Desktop":
            return resolved
    return None


ANY_PS = find_any_powershell()
DESKTOP_PS = find_desktop_powershell()


# --------------------------------------------------------------------------------------
# Shared PowerShell invocation helpers
# --------------------------------------------------------------------------------------

def run_ps(exe, script_path, *args):
    """Invoke a scratch .ps1 with a non-interactive, profile-free, bypassed host."""
    command = [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    command.extend(str(arg) for arg in args)
    return subprocess.run(command, capture_output=True, text=True, timeout=900)


PROBE_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Lib,
    [Parameter(Mandatory)][string]$Op,
    [string]$Dir = '',
    [string]$Dir2 = '',
    [string]$Path = '',
    [string]$Path2 = '',
    [string]$Path3 = '',
    [string]$Value = '',
    [string]$Value2 = '',
    [string]$Json = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. $Lib

switch ($Op) {
    'contract' {
        Get-EgLauncherLibraryContract | ConvertTo-Json -Depth 8 -Compress
    }
    default {
        throw ('unknown probe operation: ' + $Op)
    }
}
"""


def _named_args(kwargs):
    """Map python keyword arguments to PowerShell -Name value pairs."""
    args = []
    for name, value in kwargs.items():
        args.extend(["-" + name[0].upper() + name[1:], str(value)])
    return args


def write_probe(tmp):
    """Materialise the shared probe script inside a scratch directory."""
    path = Path(tmp) / "eg_probe.ps1"
    path.write_text(PROBE_SCRIPT, encoding="utf-8")
    return path


def probe(exe, op, tmp, **kwargs):
    """Run the shared probe script with -Lib <LIB> -Op <op> and named arguments."""
    script = write_probe(tmp)
    args = ["-Lib", str(LIB), "-Op", op]
    args.extend(_named_args(kwargs))
    return run_ps(exe, script, *args)


def probe_json(exe, op, tmp, **kwargs):
    """probe() plus a return-code assertion plus json.loads of standard output."""
    completed = probe(exe, op, tmp, **kwargs)
    if completed.returncode != 0:
        raise AssertionError(
            "probe %r exited %d\nstdout:\n%s\nstderr:\n%s"
            % (op, completed.returncode, completed.stdout, completed.stderr)
        )
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------------------
# PowerShell abstract-syntax-tree inspection, used by the Tier C guards
# --------------------------------------------------------------------------------------

PARSE_INSPECTOR = r"""
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Target)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$errors = $null
$tokens = $null
[System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$errors) | Out-Null
$count = 0
if ($null -ne $errors) { $count = @($errors).Count }
[pscustomobject]@{ parseErrors = $count } | ConvertTo-Json -Depth 4 -Compress
"""


def run_inspector(exe, tmp, source, **kwargs):
    """Write an inspector script into a scratch directory, run it, and parse its JSON."""
    script = Path(tmp) / "eg_inspector.ps1"
    script.write_text(source, encoding="utf-8")
    completed = run_ps(exe, script, *_named_args(kwargs))
    if completed.returncode != 0:
        raise AssertionError(
            "inspector exited %d\nstdout:\n%s\nstderr:\n%s"
            % (completed.returncode, completed.stdout, completed.stderr)
        )
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------------------
# Tier base classes
# --------------------------------------------------------------------------------------

class TierABase(unittest.TestCase):
    """Portable library tests. Skips when no PowerShell host is available."""

    @classmethod
    def setUpClass(cls):
        if ANY_PS is None:
            raise unittest.SkipTest("no PowerShell host found (pwsh or powershell)")


class TierBBase(unittest.TestCase):
    """Compatibility-boundary tests pinned to Windows PowerShell 5.1."""

    @classmethod
    def setUpClass(cls):
        if DESKTOP_PS is None:
            raise unittest.SkipTest(
                "Windows PowerShell 5.1 boundary interpreter powershell.exe "
                "(PSEdition Desktop) is not available on this host"
            )


class TierCBase(unittest.TestCase):
    """Static guard tests. Text-only assertions need no interpreter."""


# --------------------------------------------------------------------------------------
# Windows PowerShell 5.1 compatibility guard
# --------------------------------------------------------------------------------------
# Each entry is a prohibited construct from the plan's Global Constraints compatibility
# table, expressed as a regular expression matched over non-comment lines of the committed
# runtime files. The plan records the required 5.1-safe form for each.
POWERSHELL_7_ONLY_PATTERNS = {
    "pipeline_chain_and": r"&&",
    "pipeline_chain_or": r"\|\|",
    "ternary_conditional": r"\s\?\s",
    "null_coalescing": r"\?\?",
    "null_conditional_member": r"\?\.",
    "null_conditional_index": r"\?\[",
    "native_stderr_merge": r"2>&1",
    "convertfrom_json_as_hashtable": r"-AsHashtable",
    "join_path_additional_child_path": r"-AdditionalChildPath",
    "is_windows_automatic_variable": r"\$IsWindows\b",
    "new_item_force": r"New-Item[^\r\n]*-Force",
    "set_or_add_content": r"\b(?:Set-Content|Add-Content)\b",
}


def non_comment_lines(text):
    """Yield (line_number, line) for every line that is not a whole-line comment."""
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        yield number, line


class RuntimeHarnessAndStructuralGuards(TierCBase):
    """Task 1: the harness, the tiering, and the guards every later task depends on."""

    def test_runtime_library_exists_and_parses_cleanly(self):
        """EGRT-T17: every committed runtime .ps1 parses with zero errors."""
        self.assertTrue(LIB.is_file(), "runtime/launcher_lib.ps1 must exist")
        if ANY_PS is None:
            self.skipTest("no PowerShell host found (pwsh or powershell)")
        with TemporaryScratch() as tmp:
            for target in existing_runtime_ps1_files():
                with self.subTest(runtime_file=target.name):
                    result = run_inspector(ANY_PS, tmp, PARSE_INSPECTOR, target=target)
                    self.assertEqual(
                        0,
                        result["parseErrors"],
                        "%s must parse with zero errors" % target.name,
                    )

    def test_ci_requires_the_desktop_boundary_interpreter(self):
        """The Tier B boundary can never be silently unexercised inside the gate."""
        if os.environ.get("CI"):
            self.assertIsNotNone(
                DESKTOP_PS,
                "CI is set but the Windows PowerShell 5.1 boundary interpreter "
                "powershell.exe (PSEdition Desktop) was not found; Tier B must not be "
                "silently skipped in the gate",
            )
        else:
            self.assertTrue(True)

    def test_committed_runtime_files_avoid_powershell_7_only_syntax(self):
        """Every committed runtime .ps1 must execute correctly under PSEdition Desktop."""
        targets = existing_runtime_ps1_files()
        self.assertTrue(targets, "at least runtime/launcher_lib.ps1 must exist")
        for target in targets:
            text = target.read_text(encoding="utf-8")
            for name, pattern in POWERSHELL_7_ONLY_PATTERNS.items():
                compiled = re.compile(pattern)
                for number, line in non_comment_lines(text):
                    with self.subTest(runtime_file=target.name, construct=name):
                        self.assertIsNone(
                            compiled.search(line),
                            "%s line %d uses the PowerShell 7 only construct %r: %s"
                            % (target.name, number, name, line.strip()),
                        )


if __name__ == "__main__":
    unittest.main()
