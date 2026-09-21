"""Deployment-tooling split and immutable worker blob contracts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


PROTECTED_WORKER_BLOBS = {
    "scripts/install_ac2_member_gateway_worker.ps1": "fee4fac43185eaf893966e176d23216ac9544b7f",
    "scripts/ac2_member_gateway_worker.ps1": "27f0a3f8c78ba9b391b1a09ad33fe5805f723cc1",
    "scripts/ac2_member_gateway_worker_lib.ps1": "332af5a25f2be996694fdb6cef085139192ebf19",
    "scripts/ac2_member_gateway_autocount_adapter.ps1": "37ea54fea46c57671b02cbc15a138791a2977244",
}

WORKER_SCRIPTS = tuple(PROTECTED_WORKER_BLOBS) + (
    "scripts/launch_ac2_member_gateway_worker.ps1",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1",
)

ALLOWED_FILES = {
    ".github/workflows/member-gateway-tests.yml",
    "config/ac2_member_gateway_worker.production.example.json",
    "config/member_forms_gateway_bounded_import.v2.template.json",
    "docs/autocount2-automation/member_gateway_production_runbook.md",
    "n8n-workflows/README.md",
    "n8n-workflows/scripts/README.md",
    "n8n-workflows/scripts/import-member-forms-gateway-bounded.ps1",
    "scripts/install_ac2_member_gateway_worker.ps1",
    "scripts/launch_ac2_member_gateway_worker.ps1",
    "scripts/test_ac2_member_gateway_autocount_dependencies.ps1",
    "tests/test_member_gateway_worker_deployment.py",
    "tests/test_member_gateway_bounded_import_security.py",
    "tests/test_member_gateway_ci.py",
}


@dataclass(frozen=True)
class _PowerShellToken:
    kind: str
    value: str
    literal: bool = True


_POWERSHELL_ASSIGNMENT_OPERATORS = frozenset(
    {"=", "+=", "-=", "*=", "/=", "%=", "??="}
)
_POWERSHELL_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_POWERSHELL_CLOSE_TO_OPEN = {value: key for key, value in _POWERSHELL_OPEN_TO_CLOSE.items()}
_POWERSHELL_MUTATION_COMMANDS = frozenset(
    {
        "ci",
        "clv",
        "clear-content",
        "clear-variable",
        "copy-item",
        "get-variable",
        "gv",
        "iex",
        "invoke-expression",
        "mi",
        "move-item",
        "new-variable",
        "nv",
        "new-item",
        "ni",
        "remove-variable",
        "remove-item",
        "ri",
        "rv",
        "rename-variable",
        "rename-item",
        "rni",
        "set-content",
        "set",
        "set-item",
        "set-variable",
        "sc",
        "si",
        "sv",
        "tee",
        "tee-object",
        "out-variable",
        "ov",
    }
)


def _tokenize_powershell(source: str) -> list[_PowerShellToken]:
    """Tokenize the PowerShell constructs needed for the static source contract.

    This intentionally accepts only enough syntax to inspect complete assignments,
    arrays, and member calls. Unsupported dynamic syntax is kept visible as extra
    tokens so the contract fails closed instead of guessing at its meaning.
    """

    tokens: list[_PowerShellToken] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character in " \t\r":
            index += 1
            continue
        if character == "\n":
            tokens.append(_PowerShellToken("newline", "\n"))
            index += 1
            continue
        if source.startswith("<#", index):
            end = source.find("#>", index + 2)
            if end < 0:
                raise ValueError("unterminated PowerShell block comment")
            tokens.extend(_PowerShellToken("newline", "\n") for value in source[index:end] if value == "\n")
            index = end + 2
            continue
        if character == "#":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline
            continue
        if character in ("'", '"'):
            quote = character
            index += 1
            value: list[str] = []
            is_literal = True
            closed = False
            while index < len(source):
                character = source[index]
                if quote == "'" and character == "'":
                    if index + 1 < len(source) and source[index + 1] == "'":
                        value.append("'")
                        index += 2
                    else:
                        index += 1
                        closed = True
                        break
                elif quote == '"' and character == "`":
                    is_literal = False
                    if index + 1 < len(source):
                        value.append(source[index + 1])
                        index += 2
                    else:
                        index += 1
                elif quote == '"' and character == "$":
                    is_literal = False
                    value.append(character)
                    index += 1
                elif character == quote:
                    index += 1
                    closed = True
                    break
                else:
                    value.append(character)
                    index += 1
            if not closed:
                raise ValueError("unterminated PowerShell string")
            tokens.append(_PowerShellToken("string", "".join(value), is_literal))
            continue
        if character == "$":
            if index + 1 < len(source) and source[index + 1] == "{":
                end = source.find("}", index + 2)
                if end < 0:
                    raise ValueError("unterminated PowerShell braced variable")
                value = source[index + 2 : end]
                is_literal = re.fullmatch(r"[A-Za-z_?][A-Za-z0-9_?:]*", value) is not None
                tokens.append(_PowerShellToken("variable", value, is_literal))
                index = end + 1
                continue
            if index + 1 < len(source) and (source[index + 1].isalpha() or source[index + 1] in "_?"):
                end = index + 2
                while end < len(source) and (source[end].isalnum() or source[end] in "_?:"):
                    end += 1
                tokens.append(_PowerShellToken("variable", source[index + 1 : end]))
                index = end
                continue
            tokens.append(_PowerShellToken("symbol", "$"))
            index += 1
            continue

        matched_operator = False
        for operator in ("??=", "+=", "-=", "*=", "/=", "%=", "++", "--"):
            if source.startswith(operator, index):
                tokens.append(_PowerShellToken("symbol", operator))
                index += len(operator)
                matched_operator = True
                break
        if matched_operator:
            continue

        if character in "=+*/%(),[]{}.;|&<>@?:":
            tokens.append(_PowerShellToken("symbol", character))
            index += 1
            continue

        start = index
        while index < len(source) and source[index] not in " \t\r\n#'\"$=+*/%(),[]{}.;|&<>@?:.":
            index += 1
        if start == index:
            tokens.append(_PowerShellToken("symbol", source[index]))
            index += 1
        else:
            tokens.append(_PowerShellToken("word", source[start:index]))
    return tokens


def _powershell_variable_name(token: _PowerShellToken) -> str | None:
    if token.kind != "variable" or not token.literal:
        return None
    return token.value.rsplit(":", 1)[-1].casefold()


def _powershell_is_bare_variable(token: _PowerShellToken, expected: str) -> bool:
    return (
        token.kind == "variable"
        and token.literal
        and ":" not in token.value
        and _powershell_variable_name(token) == expected.casefold()
    )


def _powershell_matching_delimiter(tokens: list[_PowerShellToken], opening_index: int) -> int:
    stack: list[str] = []
    for index in range(opening_index, len(tokens)):
        value = tokens[index].value
        if value in _POWERSHELL_OPEN_TO_CLOSE:
            stack.append(value)
        elif value in _POWERSHELL_CLOSE_TO_OPEN:
            if not stack or _POWERSHELL_CLOSE_TO_OPEN[value] != stack[-1]:
                raise ValueError("unbalanced PowerShell delimiters")
            stack.pop()
            if not stack:
                return index
    raise ValueError("unterminated PowerShell construct")


def _powershell_split_top_level(
    tokens: list[_PowerShellToken], start: int, end: int
) -> list[list[_PowerShellToken]]:
    parts: list[list[_PowerShellToken]] = []
    part_start = start
    stack: list[str] = []
    for index in range(start, end):
        value = tokens[index].value
        if value in _POWERSHELL_OPEN_TO_CLOSE:
            stack.append(value)
        elif value in _POWERSHELL_CLOSE_TO_OPEN:
            if not stack or _POWERSHELL_CLOSE_TO_OPEN[value] != stack[-1]:
                raise ValueError("unbalanced PowerShell array element")
            stack.pop()
        elif value == "," and not stack:
            parts.append(tokens[part_start:index])
            part_start = index + 1
    if stack:
        raise ValueError("unterminated PowerShell array element")
    parts.append(tokens[part_start:end])
    return parts


def _powershell_statement_start(tokens: list[_PowerShellToken], before_index: int) -> int:
    stack: list[str] = []
    for index in range(before_index - 1, -1, -1):
        token = tokens[index]
        value = token.value
        if value in _POWERSHELL_CLOSE_TO_OPEN:
            stack.append(value)
            continue
        if value in _POWERSHELL_OPEN_TO_CLOSE:
            if stack:
                stack.pop()
                continue
            return index + 1
        if not stack and (token.kind == "newline" or value in {";", "{", "}"}):
            return index + 1
    return 0


def _powershell_statement_end(tokens: list[_PowerShellToken], start_index: int) -> int:
    stack: list[str] = []
    for index in range(start_index, len(tokens)):
        token = tokens[index]
        value = token.value
        if not stack and (token.kind == "newline" or value == ";"):
            return index
        if value in _POWERSHELL_OPEN_TO_CLOSE:
            stack.append(value)
        elif value in _POWERSHELL_CLOSE_TO_OPEN:
            if stack:
                stack.pop()
            else:
                return index
    return len(tokens)


def _powershell_assignment_mutations(
    tokens: list[_PowerShellToken], expected: str
) -> list[tuple[str, int, str, list[_PowerShellToken]]]:
    mutations: list[tuple[str, int, str, list[_PowerShellToken]]] = []
    expected = expected.casefold()
    for index, token in enumerate(tokens):
        if token.kind != "symbol" or token.value not in _POWERSHELL_ASSIGNMENT_OPERATORS:
            continue
        start = _powershell_statement_start(tokens, index)
        left = [value for value in tokens[start:index] if value.kind != "newline"]
        if any(_powershell_variable_name(value) == expected for value in left):
            mutations.append(("assignment", index, token.value, left))
    for index, token in enumerate(tokens):
        if _powershell_variable_name(token) != expected:
            continue
        if index > 0 and tokens[index - 1].value in {"++", "--"}:
            mutations.append(("increment", index, tokens[index - 1].value, [token]))
        if index + 1 < len(tokens) and tokens[index + 1].value in {"++", "--"}:
            mutations.append(("increment", index, tokens[index + 1].value, [token]))
    return mutations


def _assert_autocount_probe_contract(probe: str) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    require(
        'if ($PSVersionTable.PSEdition -ne "Desktop" -or $PSVersionTable.PSVersion.Major -ne 5) {' in probe,
        "PowerShell Desktop/version gate is missing",
    )
    require(
        'if (-not [Environment]::Is64BitProcess) { throw "autocount_64bit_powershell_required" }' in probe,
        "64-bit gate is missing",
    )

    try:
        tokens = _tokenize_powershell(probe)
    except ValueError as error:
        raise AssertionError(f"PowerShell source cannot be structurally inspected: {error}") from error

    for index, token in enumerate(tokens):
        command_name = token.value.casefold().rsplit("\\", 1)[-1]
        if token.kind == "word" and command_name in _POWERSHELL_MUTATION_COMMANDS:
            require(False, f"unsupported dynamic variable mutation command: {token.value}")
        if token.kind == "word" and token.value.casefold() in {
            "-outvariable",
            "-pipelinevariable",
            "-variable",
        }:
            require(False, f"unsupported variable mutation parameter: {token.value}")
        if token.kind == "word" and token.value.casefold() in {"psvariable", "setvalue", "setvalueexact", "ref"}:
            require(False, f"unsupported indirect variable mutation token: {token.value}")
        if token.kind == "word" and token.value.casefold() == "variable" and index + 1 < len(tokens) and tokens[index + 1].value == ":":
            require(False, "unsupported variable-provider mutation surface")
    for index, token in enumerate(tokens):
        if token.kind == "symbol" and token.value == "&":
            require(False, "unsupported dynamic command invocation")
        if token.kind == "symbol" and token.value == "." and (
            index == 0 or tokens[index - 1].kind == "newline" or tokens[index - 1].value in {";", "{", "}"}
        ):
            require(False, "unsupported dot-sourced command invocation")
        if token.kind == "word" and token.value.casefold() == "set" and index > 0 and tokens[index - 1].value == ".":
            require(False, "unsupported indirect variable mutation method: Set")

    required_assemblies = [
        "AutoCount.dll",
        "AutoCount.Accounting.dll",
        "AutoCount.Invoicing.dll",
        "AutoCount.ImportExport.dll",
        "AutoCount.Tools.dll",
    ]
    required_mutations = _powershell_assignment_mutations(tokens, "requiredAssemblies")
    require(len(required_mutations) == 1, "required assembly collection must have exactly one assignment")
    mutation_kind, assignment_index, assignment_operator, left = required_mutations[0]
    require(
        mutation_kind == "assignment"
        and assignment_operator == "="
        and len(left) == 1
        and _powershell_is_bare_variable(left[0], "requiredAssemblies"),
        "required assembly collection assignment is not a simple semantic assignment",
    )
    array_start = assignment_index + 1
    while array_start < len(tokens) and tokens[array_start].kind == "newline":
        array_start += 1
    require(
        array_start + 1 < len(tokens)
        and tokens[array_start].value == "@"
        and tokens[array_start + 1].value == "(",
        "required assembly collection is not an array subexpression",
    )
    array_open = array_start + 1
    try:
        array_close = _powershell_matching_delimiter(tokens, array_open)
        array_elements = _powershell_split_top_level(tokens, array_open + 1, array_close)
    except ValueError as error:
        raise AssertionError(f"required assembly collection cannot be structurally inspected: {error}") from error
    require(
        not [token for token in tokens[array_close + 1 : _powershell_statement_end(tokens, array_close + 1)] if token.kind != "newline"],
        "required assembly collection has a trailing mutation or expression",
    )
    require(
        len(array_elements) == len(required_assemblies),
        "required assembly collection does not contain exactly five elements",
    )
    for expected, element in zip(required_assemblies, array_elements):
        literal_element = [token for token in element if token.kind != "newline"]
        require(
            len(literal_element) == 1
            and literal_element[0].kind == "string"
            and literal_element[0].literal
            and literal_element[0].value == expected,
            "required assembly collection contains a non-literal, unexpected, or misordered element",
        )

    invoicing_mutations = _powershell_assignment_mutations(tokens, "invoicing")
    require(
        len(invoicing_mutations) == 1,
        "semantic $invoicing must have exactly one assignment or mutation",
    )
    mutation_kind, assignment_index, assignment_operator, left = invoicing_mutations[0]
    require(
        mutation_kind == "assignment"
        and assignment_operator == "="
        and len(left) == 1
        and _powershell_is_bare_variable(left[0], "invoicing"),
        "semantic $invoicing binding is not the sole simple assignment",
    )
    rhs_start = assignment_index + 1
    rhs_end = _powershell_statement_end(tokens, rhs_start)
    rhs = [token for token in tokens[rhs_start:rhs_end] if token.kind != "newline"]
    require(
        len(rhs) == 4
        and _powershell_variable_name(rhs[0]) == "loaded"
        and rhs[1].value == "["
        and rhs[2].kind == "string"
        and rhs[2].literal
        and rhs[2].value == "AutoCount.Invoicing.dll"
        and rhs[3].value == "]",
        "semantic $invoicing binding does not select AutoCount.Invoicing.dll from $loaded",
    )

    require(
        probe.count("[Reflection.Assembly]::ReflectionOnlyLoadFrom($path)") == 1,
        "ReflectionOnlyLoadFrom loading contract is missing or duplicated",
    )
    require(
        '$loaded[$name] = [Reflection.Assembly]::ReflectionOnlyLoadFrom($path)' in probe,
        "required assemblies are not loaded into the named map",
    )
    require(
        probe.count('$invoicing = $loaded["AutoCount.Invoicing.dll"]') == 1,
        "invoicing binding is not exact",
    )

    member_command_lines = [
        line.strip() for line in probe.splitlines() if "AutoCount.BonusPoint.Member.MemberCommand" in line
    ]
    require(
        member_command_lines
        == [
            '@($invoicing, "AutoCount.BonusPoint.Member.MemberCommand")',
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
        ],
        "MemberCommand has an alternate binding, scan, or fallback",
    )

    required_methods_match = re.search(
        r"foreach \(\$name in @\((?P<body>[^)]*)\)\)\s*\{\s*"
        r"if \(\$methodNames -notcontains \$name\)",
        probe,
        re.DOTALL,
    )
    require(required_methods_match is not None, "required MemberCommand method check is missing")
    required_methods = re.findall(r'"([^"\r\n]+)"', required_methods_match.group("body"))
    require(
        required_methods == ["Create", "GetMember", "NewMember", "SaveMember"],
        "required MemberCommand method set is incomplete or changed",
    )
    require(
        "$memberCommand.GetMethods() | ForEach-Object Name" in probe,
        "MemberCommand method enumeration is missing",
    )

    for forbidden in (
        "AssemblyResolve",
        "GetFiles(",
        "EnumerateFiles(",
        "GetFileSystemEntries(",
        "Get-ChildItem",
        "[IO.Directory]::",
        "[IO.DirectoryInfo]::",
    ):
        require(forbidden not in probe, f"forbidden broad/fallback resolver token: {forbidden}")


class MemberGatewayWorkerDeploymentTests(unittest.TestCase):
    @staticmethod
    def _blob(path: str) -> str:
        return subprocess.run(
            ["git", "hash-object", path],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @classmethod
    def setUpClass(cls) -> None:
        cls.pwsh = shutil.which("powershell")
        if not cls.pwsh:
            windows_root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
            if windows_root:
                desktop_powershell = Path(windows_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
                if desktop_powershell.is_file():
                    cls.pwsh = str(desktop_powershell)
        cls.pwsh = cls.pwsh or shutil.which("pwsh")

    @staticmethod
    def _desktop_powershell_environment() -> dict[str, str]:
        env = os.environ.copy()
        module_path_key = next((key for key in env if key.casefold() == "psmodulepath"), None)
        module_path = env.get(module_path_key) if module_path_key else None
        if module_path and module_path_key:
            env[module_path_key] = os.pathsep.join(
                path
                for path in module_path.split(os.pathsep)
                if "native\\powershell\\modules" not in path.casefold()
            )
        return env

    def _run_powershell_harness(self, script: str, *arguments: str, env: dict[str, str] | None = None):
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-harness-") as temp_dir:
            harness = Path(temp_dir) / "harness.ps1"
            harness.write_text(script, encoding="utf-8", newline="\n")
            return subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(harness),
                    *arguments,
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

    def test_protected_worker_blobs_are_unchanged(self) -> None:
        for path, expected in PROTECTED_WORKER_BLOBS.items():
            with self.subTest(path=path):
                self.assertEqual(self._blob(path), expected)

    def test_approved_worker_scripts_parse_without_execution(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for parser validation")
        for relative in WORKER_SCRIPTS:
            command = (
                "$errors = $null; $tokens = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path '{relative}'), "
                "[ref]$tokens, [ref]$errors); "
                "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
            )
            with self.subTest(path=relative):
                completed = subprocess.run(
                    [self.pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_autocount_probe_binds_member_command_to_invoicing(self) -> None:
        probe = (ROOT / "scripts/test_ac2_member_gateway_autocount_dependencies.ps1").read_text(encoding="utf-8")
        _assert_autocount_probe_contract(probe)

        rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        subsequent_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            '$invoicing = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        case_insensitive_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            '$InVoIcInG = $loaded["AutoCount.Tools.dll"]',
            1,
        )
        compound_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            '$invoicing += $loaded["AutoCount.Tools.dll"]',
            1,
        )
        indirect_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            "Set-Variable -Name 'invoicing' -Value $loaded[\"AutoCount.Tools.dll\"]",
            1,
        )
        dynamic_indirect_rebind = probe.replace(
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]',
            '$invoicing = $loaded["AutoCount.Invoicing.dll"]\n'
            "& ('Set-Variable') -Name 'invoicing' -Value $loaded[\"AutoCount.Tools.dll\"]",
            1,
        )
        alternate_scan = probe.replace(
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)',
            '$memberCommand = $invoicing.GetType("AutoCount.BonusPoint.Member.MemberCommand", $true, $false)\n'
            '$alternateMemberCommand = @($loaded.Values | ForEach-Object { $_.GetType("AutoCount.BonusPoint.Member.MemberCommand", $false, $false) } | Where-Object { $null -ne $_ } | Select-Object -First 1)\n'
            'if ($null -eq $memberCommand) { $memberCommand = $alternateMemberCommand }',
            1,
        )
        sixth_assembly = probe.replace(
            '    "AutoCount.Tools.dll"\n)',
            '    "AutoCount.Tools.dll",\n    "AutoCount.Extended.dll"\n)',
            1,
        )
        sixth_single_assembly = probe.replace(
            '    "AutoCount.Tools.dll"\n)',
            "    \"AutoCount.Tools.dll\",\n    'AutoCount.Extended.dll'\n)",
            1,
        )
        sixth_non_literal_assembly = probe.replace(
            '    "AutoCount.Tools.dll"\n)',
            '    "AutoCount.Tools.dll",\n    ("AutoCount.Extended" + ".dll")\n)',
            1,
        )

        for name, counterexample in (
            ("wrong invoicing binding", rebind),
            ("subsequent invoicing rebinding", subsequent_rebind),
            ("case-insensitive invoicing rebinding", case_insensitive_rebind),
            ("compound invoicing rebinding", compound_rebind),
            ("indirect invoicing rebinding", indirect_rebind),
            ("dynamic indirect invoicing rebinding", dynamic_indirect_rebind),
            ("alternate assembly MemberCommand scan", alternate_scan),
            ("sixth double-quoted required assembly", sixth_assembly),
            ("sixth single-quoted required assembly", sixth_single_assembly),
            ("sixth non-literal required assembly", sixth_non_literal_assembly),
        ):
            with self.subTest(counterexample=name):
                with self.assertRaises(AssertionError):
                    _assert_autocount_probe_contract(counterexample)

    def test_production_launcher_accepts_exact_strings_and_preserves_dpapi_mapping(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-runtime-") as temp_dir:
            runtime = Path(temp_dir)
            (runtime / "config").mkdir()
            (runtime / "secrets").mkdir()
            config = {
                "gateway_base_url": "https://gateway.example.test",
                "worker_host_binding": "host-ac2-worker",
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "  server exact  ",
                "autocount_database_name": "database exact",
                "autocount_user_id": "user exact",
            }
            (runtime / "config" / "worker.config.json").write_text(json.dumps(config), encoding="utf-8")
            launcher_text = (ROOT / "scripts/launch_ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
            self.assertIn("Import-Clixml -LiteralPath $Path", launcher_text)
            self.assertIn("$secureValue -isnot [Security.SecureString]", launcher_text)
            harness = r'''
$launcher = Get-Content -Raw -LiteralPath $args[0]
$prefix = $launcher.Substring(0, $launcher.IndexOf('$runId = '))
Invoke-Expression $prefix
function Read-XbCurrentUserSecretArtifact {
    param([Parameter(Mandatory)][string]$Path)
    if ($Path -like '*worker-token.clixml') { return 'token' }
    return 'password'
}
$info = New-XbWorkerProcessStartInfo -WorkerScript $args[1] -LauncherMode Production -RuntimeRootPath $args[2]
[ordered]@{
    server = $info.EnvironmentVariables['XB_AC2_SERVER_NAME']
    database = $info.EnvironmentVariables['XB_AC2_DATABASE_NAME']
    user = $info.EnvironmentVariables['XB_AC2_USER_ID']
    probe_server = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_SERVER_NAME')
    probe_database = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_DATABASE_NAME')
    probe_user = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_USER_ID')
    probe_password = $info.EnvironmentVariables.ContainsKey('AC2_PROBE_PASSWORD')
    session_factory = $info.EnvironmentVariables.ContainsKey('XB_AC2_SESSION_FACTORY')
    password = $info.EnvironmentVariables['XB_AC2_PASSWORD']
    password_env = $info.EnvironmentVariables['XB_AC2_PASSWORD_ENV_VAR']
} | ConvertTo-Json -Compress
'''
            env = os.environ.copy()
            env.update(
                {
                    "AC2_PROBE_SERVER_NAME": "legacy-server",
                    "AC2_PROBE_DATABASE_NAME": "legacy-database",
                    "AC2_PROBE_USER_ID": "legacy-user",
                    "AC2_PROBE_PASSWORD": "legacy-password",
                    "XB_AC2_SESSION_FACTORY": "legacy-factory",
                }
            )
            completed = self._run_powershell_harness(
                harness,
                str(ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"),
                r"C:\synthetic-worker.ps1",
                str(runtime),
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            observed = json.loads(completed.stdout)
            self.assertEqual(observed["server"], "  server exact  ")
            self.assertEqual(observed["database"], "database exact")
            self.assertEqual(observed["user"], "user exact")
            for name in ("probe_server", "probe_database", "probe_user", "probe_password", "session_factory"):
                self.assertFalse(observed[name], name)
            self.assertEqual(observed["password"], "password")
            self.assertEqual(observed["password_env"], "XB_AC2_PASSWORD")

    def test_invalid_production_identity_cannot_start_synthetic_child(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        invalid_values = (_MISSING, None, 7, [], "", "   ")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-invalid-") as temp_dir:
            root = Path(temp_dir)
            install = root / "install"
            runtime = root / "runtime"
            (runtime / "config").mkdir(parents=True)
            (runtime / "secrets").mkdir()
            install.mkdir()
            marker = root / "child-started.txt"
            (install / "ac2_member_gateway_worker.ps1").write_text(
                "param([switch]$EnableProductionWorker, [switch]$EnableProductionAdapter)\n"
                "$marker = [Environment]::GetEnvironmentVariable('XB_TEST_CHILD_MARKER', 'Process')\n"
                "if ([string]::IsNullOrWhiteSpace($marker)) { throw 'synthetic_marker_missing' }\n"
                "[IO.File]::WriteAllText($marker, 'started')\n"
                "[pscustomobject]@{ status = 'completed'; writes = 0 } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            base_config = {
                "gateway_base_url": "https://gateway.example.test",
                "worker_host_binding": "host-ac2-worker",
                "autocount_assembly_path": r"C:\\AutoCount",
                "autocount_server_name": "synthetic-server",
                "autocount_database_name": "synthetic-database",
                "autocount_user_id": "synthetic-user",
            }
            (runtime / "config" / "worker.config.json").write_text(
                json.dumps(base_config), encoding="utf-8"
            )
            secret_environment = self._desktop_powershell_environment()
            secret_environment["XB_TEST_WORKER_TOKEN_PATH"] = str(
                runtime / "secrets" / "worker-token.clixml"
            )
            secret_environment["XB_TEST_PASSWORD_PATH"] = str(
                runtime / "secrets" / "autocount-password.clixml"
            )
            secrets = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference = 'Stop'; "
                    "$workerToken = [Security.SecureString]::new(); "
                    "'synthetic-worker-token'.ToCharArray() | ForEach-Object { $workerToken.AppendChar($_) }; "
                    "$workerToken.MakeReadOnly(); $workerToken | "
                    "Export-Clixml -LiteralPath $env:XB_TEST_WORKER_TOKEN_PATH; "
                    "$autocountPassword = [Security.SecureString]::new(); "
                    "'synthetic-autocount-password'.ToCharArray() | ForEach-Object { $autocountPassword.AppendChar($_) }; "
                    "$autocountPassword.MakeReadOnly(); $autocountPassword | "
                    "Export-Clixml -LiteralPath $env:XB_TEST_PASSWORD_PATH",
                ],
                cwd=ROOT,
                env=secret_environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(secrets.returncode, 0, secrets.stdout + secrets.stderr)
            self.assertTrue((runtime / "secrets" / "worker-token.clixml").is_file())
            self.assertTrue((runtime / "secrets" / "autocount-password.clixml").is_file())
            env = self._desktop_powershell_environment()
            env["XB_TEST_CHILD_MARKER"] = str(marker)
            launcher = ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"

            control = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(launcher),
                    "-Mode",
                    "Production",
                    "-InstallRoot",
                    str(install),
                    "-RuntimeRoot",
                    str(runtime),
                    "-ExecutionTimeoutMilliseconds",
                    "5000",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
            control_result = json.loads(control.stdout)
            self.assertEqual(control_result["exit_code"], 0)
            self.assertEqual(control_result["terminal_status"], "worker_completed")
            self.assertTrue(marker.exists(), control.stdout + control.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "started")

            for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id"):
                for value in invalid_values:
                    with self.subTest(field=field, value=value):
                        config = dict(base_config)
                        if value is _MISSING:
                            config.pop(field)
                        else:
                            config[field] = value
                        (runtime / "config" / "worker.config.json").write_text(
                            json.dumps(config), encoding="utf-8"
                        )
                        if marker.exists():
                            marker.unlink()
                        completed = subprocess.run(
                            [
                                self.pwsh,
                                "-ExecutionPolicy",
                                "Bypass",
                                "-NoLogo",
                                "-NoProfile",
                                "-NonInteractive",
                                "-File",
                                str(launcher),
                                "-Mode",
                                "Production",
                                "-InstallRoot",
                                str(install),
                                "-RuntimeRoot",
                                str(runtime),
                                "-ExecutionTimeoutMilliseconds",
                                "5000",
                            ],
                            cwd=ROOT,
                            env=env,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                        result = json.loads(completed.stdout)
                        self.assertEqual(result["exit_code"], 1)
                        self.assertEqual(result["terminal_status"], "launcher_failed")
                        self.assertFalse(marker.exists(), completed.stdout + completed.stderr)

    def test_disabled_proof_does_not_read_production_config_or_secrets(self) -> None:
        if not self.pwsh:
            self.skipTest("PowerShell is required for behavioral validation")
        with tempfile.TemporaryDirectory(prefix="xb-member-worker-disabled-") as temp_dir:
            root = Path(temp_dir)
            install = root / "install"
            runtime = root / "runtime"
            install.mkdir()
            (install / "ac2_member_gateway_worker.ps1").write_text(
                "[pscustomobject]@{ status = 'disabled'; writes = 0; dispatch_fence = $false } | ConvertTo-Json -Compress\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    self.pwsh,
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(ROOT / "scripts/launch_ac2_member_gateway_worker.ps1"),
                    "-Mode",
                    "DisabledProof",
                    "-InstallRoot",
                    str(install),
                    "-RuntimeRoot",
                    str(runtime),
                    "-ExecutionTimeoutMilliseconds",
                    "5000",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            observed = json.loads(completed.stdout)
            self.assertEqual(observed["terminal_status"], "disabled_proof_pass")

    def test_production_launcher_binds_exact_identity_and_removes_legacy_environment(self) -> None:
        launcher = (ROOT / "scripts/launch_ac2_member_gateway_worker.ps1").read_text(encoding="utf-8")
        fields = {
            "autocount_server_name": "launcher_autocount_server_name_invalid",
            "autocount_database_name": "launcher_autocount_database_name_invalid",
            "autocount_user_id": "launcher_autocount_user_id_invalid",
        }
        for field, error_id in fields.items():
            with self.subTest(field=field):
                self.assertIn(f'PropertyName "{field}"', launcher)
                self.assertIn(error_id, launcher)
        for environment_name in (
            "XB_AC2_SERVER_NAME",
            "XB_AC2_DATABASE_NAME",
            "XB_AC2_USER_ID",
        ):
            self.assertIn(f'EnvironmentVariables["{environment_name}"]', launcher)
        for environment_name in (
            "AC2_PROBE_SERVER_NAME",
            "AC2_PROBE_DATABASE_NAME",
            "AC2_PROBE_USER_ID",
            "AC2_PROBE_PASSWORD",
            "XB_AC2_SESSION_FACTORY",
        ):
            self.assertIn(f'EnvironmentVariables.Remove("{environment_name}")', launcher)
        self.assertIn('$value -isnot [string]', launcher)
        self.assertIn('[string]::IsNullOrWhiteSpace($value)', launcher)
        self.assertNotIn('[string]$config.autocount_server_name', launcher)
        self.assertNotIn('[string]$config.autocount_database_name', launcher)
        self.assertNotIn('[string]$config.autocount_user_id', launcher)
        self.assertLess(
            launcher.index('$autocountServerName = Get-XbRequiredProductionConfigString'),
            launcher.index('if (-not $process.Start())'),
        )

    def test_worker_production_example_has_unbound_identity_placeholders(self) -> None:
        example = json.loads(
            (ROOT / "config/ac2_member_gateway_worker.production.example.json").read_text(encoding="utf-8")
        )
        for field in (
            "gateway_base_url",
            "worker_host_binding",
            "autocount_assembly_path",
            "autocount_server_name",
            "autocount_database_name",
            "autocount_user_id",
        ):
            self.assertIn(field, example)
        for field in ("autocount_server_name", "autocount_database_name", "autocount_user_id"):
            self.assertIsNone(example[field])

    def test_worktree_change_allowlist_is_narrow(self) -> None:
        status = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        for line in status:
            self.assertGreaterEqual(len(line), 4, line)
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            self.assertIn(path.replace("\\", "/"), ALLOWED_FILES, line)


if __name__ == "__main__":
    unittest.main()
