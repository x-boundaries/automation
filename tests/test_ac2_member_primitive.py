"""Member-write v2 primitive (W-G2-149 sections 2.4-2.7, 4.2, 5).

Rows covered: P-PJ (projection), P-SP (same person), P-DT (decision table,
first-match order, D4/D5), P-MX (mutex), P-SV (save/readback, at most one
save), P-GD (book, integration user, clock, synthetic guards), P-FI (fault
hooks). The primitive runs in-process against the test-only AutoCount double
in tests/fixtures/ac2_member_primitive/fake_autocount.ps1. Synthetic data only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "scripts/ac2_member_create_primitive.ps1"
ADAPTER = ROOT / "scripts/ac2_member_gateway_autocount_adapter.ps1"
FAKE = ROOT / "tests/fixtures/ac2_member_primitive/fake_autocount.ps1"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

PROD_BOOK = "BOOK_PROD_PLACEHOLDER"
TEST_BOOK = "BOOK_TEST_PLACEHOLDER"
IU = "IU_PLACEHOLDER"
SERVER_TIME = "2026-09-30T02:00:00Z"
CREATED_GUID = "0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"

RESULT_KEYS = {
    "outcome", "rule", "branch", "member_no", "member_guid", "save_invoked", "save_invocation_count",
    "readback", "reason_code", "dq_flags", "primitive", "error_code",
}


def name_component(name: str, base: str) -> str:
    """Contract 2.2 (gateway side), reproduced here only to build requests."""
    text = "".join(ch for ch in unicodedata.normalize("NFKD", name) if unicodedata.category(ch) != "Mn")
    kept = "".join(ch.upper() if "a" <= ch <= "z" else ch for ch in text if "a" <= ch <= "z" or "A" <= ch <= "Z")
    return kept[: 20 - len(base)]


def request(**overrides):
    base = overrides.pop("base", "91234567")
    name = overrides.pop("name", "Tan Ah Kow")
    value = {
        "rule": "XB-MN-1",
        "base_member_no": base,
        "name_component": name_component(name, base),
        "phone": overrides.pop("phone", base[:4] + " " + base[4:]),
        "name": name,
        "email": overrides.pop("email", "member.one@example.test"),
        "MemberType": "Default",
        "DOB": "2000-05-01",
        "RegisterDate": "2026-09-30",
        "ExpiryDate": "2028-09-29",
        "OpeningPoints": 0,
        "IsActive": True,
        "Individual": True,
        "job_id": "job-" + "A" * 24,
        "attempt_no": overrides.pop("attempt_no", 1),
        "first_claimed_at": overrides.pop("first_claimed_at", SERVER_TIME),
        "server_time_utc": SERVER_TIME,
    }
    value.update(overrides)
    return value


def synthetic_request(**overrides):
    overrides.setdefault("name", "ZZTEST Alpha")
    overrides.setdefault("email", "zztest.alpha@example.invalid")
    overrides.setdefault("base", "00091234567")
    return request(**overrides)


PROD_CONFIG = {"DatabaseName": PROD_BOOK, "LoginUserId": IU, "IntegrationUserId": IU, "ProductionBook": PROD_BOOK, "TestBookAllowlist": [TEST_BOOK], "Fault": ""}
TEST_CONFIG = {**PROD_CONFIG, "DatabaseName": TEST_BOOK}

OTHER = {"MemberNo": "91234567", "Name": "Lim Mei Ling", "EmailAddress": "other.person@example.test"}


def case(name, *, book="production", config=None, req=None, **extra):
    return {"name": name, "book": book, "config": config or (PROD_CONFIG if book == "production" else TEST_CONFIG), "request": req or request(), **extra}


CASES = [
    # P-DT decision table (first-match order R0, R1, R2a, R2b, R2c, R3, R3b, R4).
    case("r1_empty_book"),
    case("r1_unrelated_rows", rows=[{"MemberNo": "81111111", "MobilePhone": "81111111", "Name": "Other", "EmailAddress": "x@example.test"}]),
    case("r1_email_only_flag", rows=[{"MemberNo": "82222222", "MobilePhone": "82222222", "Name": "Somebody Else", "EmailAddress": "MEMBER.ONE@example.test"}]),
    case("r1_production_base_000", req=request(base="000123456", name="Ong Bee Lian")),
    case("r1_zztest_without_space_is_not_synthetic", req=request(name="ZZTESTER Bob")),
    case("r2c_link_word_order", rows=[{"MemberNo": "91234567", "Name": "ah kow  TAN", "EmailAddress": "old@example.test"}]),
    case("r2c_link_variant_email", rows=[{"MemberNo": "M000123", "MobilePhone": "6591234567", "Name": "T A Kow", "EmailAddress": " Member.One@Example.Test "}]),
    case("r2c_link_native_script", req=request(name="\u9648\u5927\u6587"), rows=[{"MemberNo": "91234567", "Name": "\u9648\u5927\u6587", "EmailAddress": "old@example.test"}]),
    case("r2c_before_r3", rows=[{"MemberNo": "91234567", "Name": "Tan Ah Kow"}, {"MemberNo": "M77", "MobilePhone": "6591234567", "Name": "Different Person", "EmailAddress": "d@example.test"}]),
    case("r2a_multiple_same_person", rows=[{"MemberNo": "91234567", "Name": "Tan Ah Kow"}, {"MemberNo": "M2", "MobilePhone": "91234567", "EmailAddress": "member.one@example.test"}]),
    case("r2b_inactive_match", rows=[{"MemberNo": "91234567", "Name": "Tan Ah Kow", "IsActive": "F"}]),
    case("r3_variant_other_person", rows=[{"MemberNo": "M9", "MobilePhone": "6591234567", "Name": "Different Person", "EmailAddress": "d@example.test"}]),
    case("r3_before_r3b", rows=[{"MemberNo": "M9", "MobilePhone": "6591234567", "Name": "Different Person", "EmailAddress": "d@example.test"}, {"MemberNo": "91234567"}]),
    case("r3b_holder_identity_unknown", rows=[{"MemberNo": "91234567"}]),
    case("r3b_before_r4", rows=[OTHER, {"MemberNo": "M5", "MobilePhone": "91234567"}]),
    case("r4_create_name_appended", rows=[OTHER]),
    case("r4_name_component_empty", req=request(name="\u9648\u5927\u6587"), rows=[OTHER]),
    case("r4_name_candidate_collision", rows=[OTHER, {"MemberNo": "91234567tanahkow", "MobilePhone": "80000000", "Name": "Unrelated", "EmailAddress": "u@example.test"}]),
    case("variant_needs_eight_digits", req=request(base="1234567", name="Short Base"), rows=[{"MemberNo": "M1", "MobilePhone": "651234567", "Name": "Different Person", "EmailAddress": "d@example.test"}]),
    case("malformed_member_no_flag", rows=[{"MemberNo": "9.1234567E+07", "MobilePhone": "", "Name": "Sci Notation", "EmailAddress": "s@example.test"}]),
    # R0 (D5).
    case("r0_prior_attempt_verified", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_exact_threshold", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": -10}]),
    case("r0_prior_attempt_name_appended", req=request(attempt_no=2), rows=[OTHER | {"MemberNo": "81234567", "MobilePhone": "91234567"}, {"MemberNo": "91234567TANAHKOW", "from_request": True, "CreatedUserID": IU, "created_minutes": 2}]),
    case("r0_staff_edited_prior_row", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "Name": "Edited By Staff", "EmailAddress": "edited@example.test", "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_two_candidates", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}, {"MemberNo": "91234567TANAHKOW", "from_request": True, "CreatedUserID": IU, "created_minutes": 2}]),
    case("r0_member_no_not_candidate", req=request(attempt_no=2), rows=[{"MemberNo": "X91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_window_excludes_old_rows", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": -11}]),
    case("r0_wrong_creator", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": "OTHER_USER_PLACEHOLDER", "created_minutes": 1}]),
    case("r0_null_created_time", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_time_mode": "null"}]),
    case("r0_dbnull_created_time", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_time_mode": "dbnull"}]),
    case("r0_malformed_created_time", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_time_mode": "malformed"}]),
    case("r0_non_datetime_created_time", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_time_mode": "non_datetime"}]),
    case("r0_missing_created_time_column", req=request(attempt_no=2), probe_mode="missing_created_time", rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_readback_null_created_time", req=request(attempt_no=2), readback_created_time_mode="null", rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_readback_malformed_created_time", req=request(attempt_no=2), readback_created_time_mode="malformed", rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_readback_old_created_time", req=request(attempt_no=2), readback_created_time_mode="old", rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_readback_missing_created_time", req=request(attempt_no=2), readback_created_time_mode="missing", rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_readback_wrong_creator", req=request(attempt_no=2), readback_created_user_mode="wrong", rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    case("r0_readback_missing_guid", req=request(attempt_no=2), rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "Guid": "", "created_minutes": 1}]),
    case("r0_not_on_first_attempt", rows=[{"MemberNo": "91234567", "from_request": True, "CreatedUserID": IU, "created_minutes": 1}]),
    # P-SV 4.2 classification.
    case("sv_threw_after_commit_equal", save_mode="throw_after_commit"),
    case("sv_returned_fields_differ", save_mode="commit_mutated"),
    case("sv_threw_fields_differ", save_mode="commit_mutated_throw"),
    case("sv_returned_foreign_row", save_mode="commit_foreign_user"),
    case("sv_threw_foreign_row", save_mode="throw_foreign"),
    case("sv_threw_absent", save_mode="throw_no_commit"),
    case("sv_returned_absent", save_mode="vanish"),
    case("sv_readback_failed", readback_mode="fail"),
    case("sv_guid_missing", save_mode="commit_no_guid"),
    case("sv_post_save_same_person_other_row", save_mode="commit_plus_staff_dup"),
    case("sv_new_member_failed", new_member_mode="fail"),
    # P-GD guards.
    case("gd_book_missing", book=""),
    case("gd_production_book_mismatch", config={**PROD_CONFIG, "DatabaseName": "BOOK_OTHER_PLACEHOLDER"}),
    case("gd_test_book_is_production", book="test", config={**TEST_CONFIG, "DatabaseName": PROD_BOOK, "TestBookAllowlist": [PROD_BOOK]}, req=synthetic_request()),
    case("gd_test_book_not_allowlisted", book="test", config={**TEST_CONFIG, "TestBookAllowlist": ["BOOK_OTHER_PLACEHOLDER"]}, req=synthetic_request()),
    case("gd_login_user_not_iu", config={**PROD_CONFIG, "LoginUserId": "ADMIN_PLACEHOLDER"}),
    case("gd_session_user_not_iu", session_user="ADMIN_PLACEHOLDER"),
    case("gd_session_book_mismatch", session_database="BOOK_OTHER_PLACEHOLDER"),
    case("gd_session_unavailable", session_mode="fail"),
    case("gd_clock_skew", now_offset_seconds=121),
    case("gd_clock_within_limit", now_offset_seconds=-119),
    case("gd_synthetic_name_in_production", req=request(name="ZZTEST Person")),
    case("gd_synthetic_email_in_production", req=request(email="someone@EXAMPLE.INVALID")),
    case("gd_test_book_requires_synthetic", book="test"),
    case("gd_test_book_one_marker", book="test", req=request(name="ZZTEST Person")),
    case("gd_test_book_fully_synthetic_creates", book="test", req=synthetic_request()),
    case("gd_name_101_units", req=request(name="N" * 101)),
    case("gd_name_astral_over_limit", req=request(name="\U0001d400" * 51)),
    case("gd_name_100_units_ok", req=request(name="Tan " + "A" * 96)),
    case("gd_email_201_units", req=request(email="e" * 188 + "@example.test")),
    case("gd_phone_base_mismatch", req=request(phone="+65 9123 4567")),
    case("gd_component_lowercase", req=request(name_component="tanahkow")),
    case("gd_component_too_long", req=request(name_component="ABCDEFGHIJKLM")),
    case("gd_extra_field", req=request(extra="x")),
    case("gd_not_json", raw_line="not json"),
    case("gd_probe_failed", probe_mode="fail"),
    case("gd_probe_column_missing", probe_mode="missing_column"),
    # P-MX mutex.
    case("mx_busy", mutex="busy"),
    case("mx_abandoned_is_acquired", mutex="abandoned"),
    case("mx_unavailable", mutex="unavailable"),
    case("mx_released_after_create", mutex="free_checked"),
    # P-FI fault hooks.
    case("fi_production_refused", config={**PROD_CONFIG, "Fault": "save_error_after_commit"}),
    case("fi_test_book_is_production_refused", book="test", config={**TEST_CONFIG, "DatabaseName": PROD_BOOK, "TestBookAllowlist": [PROD_BOOK], "Fault": "save_error_after_commit"}, req=synthetic_request()),
    case("fi_not_allowlisted_refused", book="test", config={**TEST_CONFIG, "TestBookAllowlist": [], "Fault": "save_error_after_commit"}, req=synthetic_request()),
    case("fi_unknown_fault_refused", book="test", config={**TEST_CONFIG, "Fault": "drop_tables"}, req=synthetic_request()),
    case("fi_guarded_save_error_after_commit", book="test", config={**TEST_CONFIG, "Fault": "save_error_after_commit"}, req=synthetic_request()),
    case("fi_guarded_readback_error", book="test", config={**TEST_CONFIG, "Fault": "readback_error"}, req=synthetic_request()),
]

HARNESS = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Primitive,
    [Parameter(Mandatory)][string]$Fake,
    [Parameter(Mandatory)][string]$CasesPath,
    [Parameter(Mandatory)][string]$OutPath
)
$ErrorActionPreference = "Stop"
. $Primitive -LibraryOnly
. $Fake
$interpreter = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

function Start-XbMutexHolder {
    param([string]$Name, [string]$Mode)
    $script = "`$m = New-Object System.Threading.Mutex(`$false, '$Name'); [void]`$m.WaitOne(); [Console]::Out.WriteLine('held'); [Console]::Out.Flush(); if ('$Mode' -eq 'hold') { Start-Sleep -Seconds 60 }"
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $interpreter
    $info.Arguments = '-NoLogo -NoProfile -NonInteractive -Command "' + $script.Replace('"', '\"') + '"'
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $first = $process.StandardOutput.ReadLine()
    if ($first -ne "held") { throw "holder_failed" }
    return $process
}

function Test-XbMutexFreeFromOtherProcess {
    param([string]$Name)
    $script = "`$m = New-Object System.Threading.Mutex(`$false, '$Name'); try { `$r = `$m.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { `$r = 'abandoned' }; [Console]::Out.Write([string]`$r)"
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $interpreter
    $info.Arguments = '-NoLogo -NoProfile -NonInteractive -Command "' + $script.Replace('"', '\"') + '"'
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $text = $process.StandardOutput.ReadToEnd()
    $process.WaitForExit()
    return $text
}

$cases = [IO.File]::ReadAllText($CasesPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$results = [ordered]@{}
foreach ($case in $cases) {
    $script:XbSaveMemberInvocationCount = 0
    $request = $case.request
    $server = [DateTimeOffset]::Parse([string]$request.server_time_utc, [Globalization.CultureInfo]::InvariantCulture)
    $now = $server.AddSeconds([double](Get-XbFakeValue $case "now_offset_seconds" 0))
    $cfg = $case.config
    $config = [pscustomobject]@{
        DatabaseName = [string]$cfg.DatabaseName
        LoginUserId = [string]$cfg.LoginUserId
        IntegrationUserId = [string]$cfg.IntegrationUserId
        ProductionBook = [string]$cfg.ProductionBook
        TestBookAllowlist = [string[]]@($cfg.TestBookAllowlist)
        Fault = [string]$cfg.Fault
        PasswordEnvironmentVariable = "XB_TEST_PRIMITIVE_PASSWORD"
    }
    [Environment]::SetEnvironmentVariable("XB_TEST_PRIMITIVE_PASSWORD", "synthetic-password", "Process")
    $fakeBook = New-XbFakeAutoCountBook -Case $case -Request $request -ServerNow $now -IntegrationUserId $config.IntegrationUserId -DatabaseName $config.DatabaseName
    $rawLine = Get-XbFakeValue $case "raw_line" $null
    $line = $(if ($null -ne $rawLine) { [string]$rawLine } else { ConvertTo-XbAc2AsciiJson -Value $request })
    $mutexName = "Global\XB-AC2-MemberCreate-test-" + [Guid]::NewGuid().ToString("N")
    $mode = [string](Get-XbFakeValue $case "mutex" "free")
    $wait = 30000
    $holder = $null
    $handle = $null
    if ($mode -eq "busy") { $holder = Start-XbMutexHolder -Name $mutexName -Mode "hold"; $wait = 300 }
    if ($mode -eq "abandoned") {
        $handle = New-Object System.Threading.Mutex($false, $mutexName)
        $holder = Start-XbMutexHolder -Name $mutexName -Mode "abandon"
        [void]$holder.WaitForExit(20000)
    }
    if ($mode -eq "unavailable") { $handle = New-Object System.Threading.EventWaitHandle($false, [System.Threading.EventResetMode]::ManualReset, $mutexName) }
    if ($mode -eq "free_checked") { $handle = New-Object System.Threading.Mutex($false, $mutexName) }
    $result = Invoke-XbAc2MemberCreatePrimitive -RequestLine $line -Book ([string]$case.book) -Config $config -EnableProductionAdapter -SessionFactory $fakeBook.SessionFactory -MemberCommandFactory $fakeBook.MemberCommandFactory -MutexName $mutexName -MutexWaitMilliseconds $wait -UtcNow $now
    $freeAfter = $null
    if ($mode -eq "free_checked") { $freeAfter = Test-XbMutexFreeFromOtherProcess -Name $mutexName }
    if ($null -ne $holder) { if (-not $holder.HasExited) { $holder.Kill() }; [void]$holder.WaitForExit(20000) }
    if ($null -ne $handle) { $handle.Dispose() }
    $results[[string]$case.name] = [ordered]@{
        result = $result
        output_json = (ConvertTo-XbAc2AsciiJson -Value $result)
        saves = [int]$fakeBook.State.saves
        getmember = [int]$fakeBook.State.getmember
        probe_calls = [int]$fakeBook.State.probe_calls
        new_member = [int]$fakeBook.State.new_member
        session_opened = [int]$fakeBook.State.session_opened
        password_cleared = [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("XB_TEST_PRIMITIVE_PASSWORD", "Process"))
        mutex_free_after = $freeAfter
    }
}

# P-PJ / P-SP unit vectors for the projection helpers.
$units = [ordered]@{
    digits_fullwidth = (Get-XbAc2Digits ([string][char]0xFF16 + [char]0xFF15 + " 9123-4567"))
    digits_null = (Get-XbAc2Digits $null)
    digits_trim = (Get-XbAc2Digits " (65) 9123 4567 ")
    name_word_order = ((Get-XbAc2NameKey "Tan  Ah-Kow") -ceq (Get-XbAc2NameKey "kow ah TAN"))
    name_key = (Get-XbAc2NameKey "  Tan, Ah-Kow!! ")
    name_accent_kept = ((Get-XbAc2NameKey "Jose") -ceq (Get-XbAc2NameKey ("Jos" + [char]0x00E9)))
    name_nfkc = ((Get-XbAc2NameKey ([string][char]0xFF34 + "an")) -ceq (Get-XbAc2NameKey "Tan"))
    name_empty = (Get-XbAc2NameKey " -- ")
    email_key = (Get-XbAc2EmailKey "  Member.One@Example.TEST ")
    mnu = (Get-XbAc2MemberNoUpper " 91234567tanAhkow ")
    malformed_yes = (Test-XbAc2MalformedMemberNo "6.59E+09")
    malformed_plain = (Test-XbAc2MalformedMemberNo "65900000")
    malformed_lower = (Test-XbAc2MalformedMemberNo "6e9")
    synthetic_name_exact = (Test-XbAc2SyntheticName "ZZTEST A")
    synthetic_name_no_space = (Test-XbAc2SyntheticName "ZZTESTA")
    synthetic_name_lower = (Test-XbAc2SyntheticName "zztest a")
    synthetic_email_case = (Test-XbAc2SyntheticEmail "a@Example.Invalid")
}
$script:XbSaveMemberInvocationCount = 0
$double = New-XbFakeAutoCountBook -Case ([pscustomobject]@{}) -Request ([pscustomobject](@{ first_claimed_at = "2026-09-30T02:00:00Z"; name = "x"; base_member_no = "91234567" })) -ServerNow ([DateTimeOffset]::UtcNow) -IntegrationUserId "IU_PLACEHOLDER" -DatabaseName "BOOK"
$session = New-XbAutoCountSession -EnableProductionAdapter -SessionFactory $double.SessionFactory
$prepared = New-XbAutoCountMemberEntity -Session $session -Member @{ MemberNo = "91234567"; MemberType = "Default"; Name = "x"; MobilePhone = "91234567"; EmailAddress = "x@example.test"; DOB = "2000-01-01"; RegisterDate = "2026-01-01"; ExpiryDate = "2027-12-31"; OpeningPoints = 0 } -EnableProductionAdapter -MemberCommandFactory $double.MemberCommandFactory
$first = Invoke-XbAutoCountSaveMember -Prepared $prepared -EnableProductionAdapter
$second = "not_raised"
try { [void](Invoke-XbAutoCountSaveMember -Prepared $prepared -EnableProductionAdapter) } catch { $second = [string]$_.Exception.Message }
$units.second_save_in_process = $second
$units.first_save_returned = $first
$units.saves_after_two_calls = [int]$double.State.saves
$disabled = "not_raised"
$script:XbSaveMemberInvocationCount = 0
try { [void](Invoke-XbAutoCountSaveMember -Prepared $prepared -EnableProductionAdapter:$false) } catch { $disabled = [string]$_.Exception.Message }
$units.save_without_adapter_switch = $disabled
$units.saves_after_disabled_call = [int]$double.State.saves

$payload = [ordered]@{ cases = $results; units = $units } | ConvertTo-Json -Depth 12
[IO.File]::WriteAllText($OutPath, $payload, [Text.UTF8Encoding]::new($false))
"""


@unittest.skipUnless(Path(POWERSHELL).exists() or shutil.which("powershell"), "Windows PowerShell 5.1 is required")
class Ac2MemberPrimitiveTests(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        pwsh = POWERSHELL if Path(POWERSHELL).exists() else shutil.which("powershell")
        with tempfile.TemporaryDirectory(prefix="xb-ac2-primitive-") as temp:
            root = Path(temp)
            harness = root / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8")
            cases_path = root / "cases.json"
            cases_path.write_text(json.dumps(CASES, ensure_ascii=False), encoding="utf-8")
            out_path = root / "out.json"
            completed = subprocess.run(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness),
                 "-Primitive", str(PRIMITIVE), "-Fake", str(FAKE), "-CasesPath", str(cases_path), "-OutPath", str(out_path)],
                capture_output=True, text=True, timeout=900,
            )
            if completed.returncode != 0 or not out_path.exists():
                raise AssertionError(completed.stdout[-4000:] + completed.stderr[-4000:])
            cls.report = json.loads(out_path.read_text(encoding="utf-8-sig"))

    def run_case(self, name: str) -> dict:
        entry = self.report["cases"][name]
        result = entry["result"]
        self.assertEqual(set(result), RESULT_KEYS, name)
        # The emitted line is ASCII and equals the structured result.
        self.assertTrue(entry["output_json"].isascii(), name)
        self.assertEqual(json.loads(entry["output_json"]), result, name)
        self.assertEqual(result["primitive"]["rule_version"], "XB-MN-1")
        self.assertRegex(result["primitive"]["release_sha256"], r"^[0-9a-f]{64}$")
        # At most one save on every path, and the flags describe that exact call.
        self.assertLessEqual(entry["saves"], 1, name)
        self.assertEqual(result["save_invocation_count"], 1 if result["save_invoked"] else 0, name)
        self.assertEqual(entry["saves"], result["save_invocation_count"], name)
        self.assertLessEqual(len(result["dq_flags"]), 3)
        return entry

    def assert_outcome(self, name, outcome, *, rule="NONE", branch="NONE", reason=None, saves=0):
        entry = self.run_case(name)
        result = entry["result"]
        self.assertEqual(
            (result["outcome"], result["rule"], result["branch"], result["reason_code"], entry["saves"]),
            (outcome, rule, branch, reason, saves),
            (name, result),
        )
        return entry

    # ---- P-DT ----------------------------------------------------------
    def test_r1_creates_the_base_member(self):
        for name in ("r1_empty_book", "r1_unrelated_rows", "r1_email_only_flag", "r1_zztest_without_space_is_not_synthetic", "gd_clock_within_limit", "gd_name_100_units_ok"):
            with self.subTest(name=name):
                entry = self.assert_outcome(name, "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
                result = entry["result"]
                self.assertEqual(result["member_no"], "91234567")
                self.assertEqual(result["member_guid"], CREATED_GUID)
                self.assertEqual(result["readback"], {"found": True, "match": True, "created_by_integration_user": True})
        self.assertEqual(self.report["cases"]["r1_email_only_flag"]["result"]["dq_flags"], ["email_seen_on_other_member"])
        self.assertEqual(self.report["cases"]["r1_empty_book"]["result"]["dq_flags"], [])

    def test_production_base_starting_000_creates_normally(self):
        entry = self.assert_outcome("r1_production_base_000", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
        self.assertEqual(entry["result"]["member_no"], "000123456")

    def test_r2c_links_same_person(self):
        for name, member_no in (("r2c_link_word_order", "91234567"), ("r2c_link_variant_email", "M000123"), ("r2c_link_native_script", "91234567"), ("r2c_before_r3", "91234567")):
            with self.subTest(name=name):
                entry = self.assert_outcome(name, "LINKED_EXISTING", rule="R2c", branch="EXISTING", saves=0)
                self.assertEqual(entry["result"]["member_no"], member_no)
                self.assertRegex(entry["result"]["member_guid"], r"^00000000-0000-4000-8000-0000000000\d\d$")
                self.assertIsNone(entry["result"]["readback"])
                self.assertEqual(entry["new_member"], 0)

    def test_review_rules_in_first_match_order(self):
        expected = {
            "r2a_multiple_same_person": ("R2a", "multiple_same_person"),
            "r2b_inactive_match": ("R2b", "inactive_match"),
            "r3_variant_other_person": ("R3", "format_variant_other_person"),
            "r3_before_r3b": ("R3", "format_variant_other_person"),
            "r3b_holder_identity_unknown": ("R3b", "holder_identity_unknown"),
            "r3b_before_r4": ("R3b", "holder_identity_unknown"),
            "r4_name_component_empty": ("R4", "name_component_empty"),
            "r4_name_candidate_collision": ("R4", "name_candidate_collision"),
        }
        for name, (rule, reason) in expected.items():
            with self.subTest(name=name):
                entry = self.assert_outcome(name, "MANUAL_REVIEW", rule=rule, reason=reason, saves=0)
                self.assertIsNone(entry["result"]["member_no"])
                self.assertIsNone(entry["result"]["member_guid"])
                self.assertEqual(entry["new_member"], 0)

    def test_r4_creates_name_appended_member(self):
        entry = self.assert_outcome("r4_create_name_appended", "CREATED_VERIFIED", rule="R4", branch="NAME_APPENDED", saves=1)
        self.assertEqual(entry["result"]["member_no"], "91234567TANAHKOW")

    def test_variant_requires_eight_digits_on_both_sides(self):
        self.assert_outcome("variant_needs_eight_digits", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)

    def test_malformed_member_no_is_excluded_and_flagged(self):
        entry = self.assert_outcome("malformed_member_no_flag", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
        self.assertEqual(entry["result"]["dq_flags"], ["malformed_member_no_excluded"])

    def test_r0_prior_attempt_rules(self):
        entry = self.assert_outcome("r0_prior_attempt_verified", "CREATED_VERIFIED_PRIOR_ATTEMPT", rule="R0", branch="BASE", saves=0)
        self.assertEqual(entry["result"]["member_no"], "91234567")
        self.assertIsNotNone(entry["result"]["member_guid"])
        threshold = self.assert_outcome("r0_exact_threshold", "CREATED_VERIFIED_PRIOR_ATTEMPT", rule="R0", branch="BASE", saves=0)
        self.assertEqual(threshold["result"]["member_no"], "91234567")
        entry = self.assert_outcome("r0_prior_attempt_name_appended", "CREATED_VERIFIED_PRIOR_ATTEMPT", rule="R0", branch="NAME_APPENDED", saves=0)
        self.assertEqual(entry["result"]["member_no"], "91234567TANAHKOW")

        for name in (
            "r0_staff_edited_prior_row",
            "r0_two_candidates",
            "r0_member_no_not_candidate",
            "r0_readback_null_created_time",
            "r0_readback_malformed_created_time",
            "r0_readback_old_created_time",
            "r0_readback_missing_created_time",
            "r0_readback_wrong_creator",
            "r0_readback_missing_guid",
        ):
            with self.subTest(name=name):
                self.assert_outcome(name, "MANUAL_REVIEW", rule="R0", reason="prior_attempt_ambiguous", saves=0)

        # Rows that fail R0 eligibility keep the existing first-match route.
        for name in (
            "r0_window_excludes_old_rows",
            "r0_wrong_creator",
            "r0_null_created_time",
            "r0_dbnull_created_time",
            "r0_malformed_created_time",
            "r0_non_datetime_created_time",
            "r0_not_on_first_attempt",
        ):
            with self.subTest(name=name):
                self.assert_outcome(name, "LINKED_EXISTING", rule="R2c", branch="EXISTING", saves=0)

        self.assert_outcome("r0_missing_created_time_column", "FAILED_BEFORE_WRITE", reason="probe_unavailable", saves=0)
        for name, entry in self.report["cases"].items():
            if name.startswith("r0_"):
                self.assertEqual(entry["saves"], 0, name)

        for name in (
            "r0_window_excludes_old_rows",
            "r0_wrong_creator",
            "r0_null_created_time",
            "r0_dbnull_created_time",
            "r0_malformed_created_time",
            "r0_non_datetime_created_time",
            "r0_readback_null_created_time",
            "r0_readback_malformed_created_time",
            "r0_readback_old_created_time",
            "r0_readback_missing_created_time",
            "r0_readback_wrong_creator",
            "r0_readback_missing_guid",
            "r0_missing_created_time_column",
        ):
            self.assertNotEqual(self.report["cases"][name]["result"]["outcome"], "CREATED_VERIFIED_PRIOR_ATTEMPT", name)

    # ---- P-SV ----------------------------------------------------------
    def test_post_save_classification_table(self):
        found_iu_match = {"found": True, "match": True, "created_by_integration_user": True}
        rows = {
            "sv_threw_after_commit_equal": ("CREATED_VERIFIED", None, found_iu_match),
            "sv_returned_fields_differ": ("CREATED_READBACK_MISMATCH", None, {"found": True, "match": False, "created_by_integration_user": True}),
            "sv_threw_fields_differ": ("CREATED_READBACK_MISMATCH", None, {"found": True, "match": False, "created_by_integration_user": True}),
            "sv_returned_foreign_row": ("MANUAL_REVIEW", "readback_foreign_row", {"found": True, "match": True, "created_by_integration_user": False}),
            "sv_threw_foreign_row": ("NOT_CREATED_CONFLICT", None, {"found": True, "match": True, "created_by_integration_user": False}),
            "sv_threw_absent": ("NOT_CREATED", None, {"found": False, "match": False, "created_by_integration_user": False}),
            "sv_returned_absent": ("OUTCOME_UNCERTAIN", "readback_absent_after_save", {"found": False, "match": False, "created_by_integration_user": False}),
            "sv_readback_failed": ("OUTCOME_UNCERTAIN", "readback_unavailable", None),
            "sv_guid_missing": ("CREATED_READBACK_MISMATCH", None, found_iu_match),
        }
        for name, (outcome, reason, readback) in rows.items():
            with self.subTest(name=name):
                entry = self.assert_outcome(name, outcome, rule="R1", branch="BASE", reason=reason, saves=1)
                self.assertEqual(entry["result"]["readback"], readback)
                self.assertTrue(entry["result"]["save_invoked"])
                self.assertEqual(entry["result"]["member_no"], "91234567")
        self.assertEqual(self.report["cases"]["sv_threw_after_commit_equal"]["result"]["error_code"], "save_threw")
        self.assertEqual(self.report["cases"]["sv_guid_missing"]["result"]["error_code"], "readback_guid_missing")
        self.assertIsNone(self.report["cases"]["sv_threw_foreign_row"]["result"]["member_guid"])
        self.assertIsNone(self.report["cases"]["sv_returned_foreign_row"]["result"]["member_guid"])

    def test_post_save_reprojection_flags_only_on_created_verified(self):
        entry = self.assert_outcome("sv_post_save_same_person_other_row", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
        self.assertEqual(entry["result"]["dq_flags"], ["post_save_same_person_other_row"])
        self.assertEqual(entry["probe_calls"], 2)
        self.assertEqual(self.report["cases"]["sv_returned_fields_differ"]["probe_calls"], 1)

    def test_failure_before_save_is_failed_before_write(self):
        entry = self.assert_outcome("sv_new_member_failed", "FAILED_BEFORE_WRITE", reason="unexpected_error", saves=0)
        self.assertEqual(entry["new_member"], 1)

    def test_single_save_call_site_refuses_a_second_call_in_process(self):
        units = self.report["units"]
        self.assertTrue(units["first_save_returned"])
        self.assertEqual(units["second_save_in_process"], "save_member_invocation_count_invalid")
        self.assertEqual(units["saves_after_two_calls"], 1)
        self.assertEqual(units["save_without_adapter_switch"], "autocount_adapter_disabled")
        self.assertEqual(units["saves_after_disabled_call"], 1)

    # ---- P-GD ----------------------------------------------------------
    def test_book_and_integration_user_guards(self):
        expected = {
            "gd_book_missing": "primitive_config_invalid",
            "gd_production_book_mismatch": "book_binding_mismatch",
            "gd_test_book_is_production": "book_binding_mismatch",
            "gd_test_book_not_allowlisted": "book_binding_mismatch",
            "gd_login_user_not_iu": "integration_user_mismatch",
            "gd_session_user_not_iu": "integration_user_mismatch",
            "gd_session_book_mismatch": "book_binding_mismatch",
            "gd_session_unavailable": "session_unavailable",
            "gd_clock_skew": "clock_skew",
            "gd_test_book_requires_synthetic": "test_book_requires_synthetic",
            "gd_test_book_one_marker": "test_book_requires_synthetic",
            "gd_probe_failed": "probe_unavailable",
            "gd_probe_column_missing": "probe_unavailable",
        }
        for name, reason in expected.items():
            with self.subTest(name=name):
                entry = self.assert_outcome(name, "FAILED_BEFORE_WRITE", reason=reason, saves=0)
                self.assertEqual(entry["new_member"], 0)
        for name in ("gd_book_missing", "gd_production_book_mismatch", "gd_login_user_not_iu", "gd_clock_skew", "gd_test_book_requires_synthetic"):
            self.assertEqual(self.report["cases"][name]["session_opened"], 0, name)
        for name in ("gd_session_user_not_iu", "gd_session_book_mismatch", "gd_session_unavailable"):
            self.assertEqual(self.report["cases"][name]["probe_calls"], 0, name)
            self.assertTrue(self.report["cases"][name]["password_cleared"], name)
        self.assertTrue(self.report["cases"]["r1_empty_book"]["password_cleared"])

    def test_synthetic_and_limit_rejections(self):
        for name, reason in (
            ("gd_synthetic_name_in_production", "synthetic_in_production"),
            ("gd_synthetic_email_in_production", "synthetic_in_production"),
            ("gd_name_101_units", "name_exceeds_autocount_limit"),
            ("gd_name_astral_over_limit", "name_exceeds_autocount_limit"),
            ("gd_email_201_units", "email_exceeds_autocount_limit"),
        ):
            with self.subTest(name=name):
                entry = self.assert_outcome(name, "REJECTED_VALIDATION", reason=reason, saves=0)
                self.assertEqual(entry["session_opened"], 0)
        entry = self.assert_outcome("gd_test_book_fully_synthetic_creates", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
        self.assertEqual(entry["result"]["member_no"], "00091234567")

    def test_request_contract_violations(self):
        for name in ("gd_phone_base_mismatch", "gd_component_lowercase", "gd_component_too_long", "gd_extra_field", "gd_not_json"):
            with self.subTest(name=name):
                entry = self.assert_outcome(name, "MANUAL_REVIEW", reason="request_contract_violation", saves=0)
                self.assertEqual(entry["session_opened"], 0)

    # ---- P-MX ----------------------------------------------------------
    def test_mutex_cases(self):
        entry = self.assert_outcome("mx_busy", "MUTEX_BUSY", saves=0)
        self.assertEqual(entry["session_opened"], 0)
        self.assertIsNone(entry["result"]["reason_code"])
        self.assert_outcome("mx_abandoned_is_acquired", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
        entry = self.assert_outcome("mx_unavailable", "FAILED_BEFORE_WRITE", reason="mutex_unavailable", saves=0)
        self.assertEqual(entry["session_opened"], 0)
        entry = self.assert_outcome("mx_released_after_create", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
        self.assertEqual(entry["mutex_free_after"], "True")

    # ---- P-FI ----------------------------------------------------------
    def test_fault_hooks_refused_outside_the_guard(self):
        for name in ("fi_production_refused", "fi_test_book_is_production_refused", "fi_not_allowlisted_refused", "fi_unknown_fault_refused"):
            with self.subTest(name=name):
                entry = self.assert_outcome(name, "FAILED_BEFORE_WRITE", reason="fault_injection_refused", saves=0)
                self.assertEqual(entry["session_opened"], 0)
                self.assertEqual(entry["new_member"], 0)

    def test_guarded_fault_hooks_apply_in_the_test_book(self):
        entry = self.assert_outcome("fi_guarded_save_error_after_commit", "CREATED_VERIFIED", rule="R1", branch="BASE", saves=1)
        self.assertEqual(entry["result"]["error_code"], "save_threw")
        self.assert_outcome("fi_guarded_readback_error", "OUTCOME_UNCERTAIN", rule="R1", branch="BASE", reason="readback_unavailable", saves=1)

    # ---- P-PJ / P-SP ---------------------------------------------------
    def test_projection_and_same_person_helpers(self):
        units = self.report["units"]
        self.assertEqual(units["digits_fullwidth"], "6591234567")
        self.assertEqual(units["digits_null"], "")
        self.assertEqual(units["digits_trim"], "6591234567")
        self.assertTrue(units["name_word_order"])
        self.assertEqual(units["name_key"], "ah kow tan")
        self.assertFalse(units["name_accent_kept"])
        self.assertTrue(units["name_nfkc"])
        self.assertEqual(units["name_empty"], "")
        self.assertEqual(units["email_key"], "member.one@example.test")
        self.assertEqual(units["mnu"], "91234567TANAHKOW")
        self.assertTrue(units["malformed_yes"])
        self.assertFalse(units["malformed_plain"])
        self.assertTrue(units["malformed_lower"])
        self.assertTrue(units["synthetic_name_exact"])
        self.assertFalse(units["synthetic_name_no_space"])
        self.assertFalse(units["synthetic_name_lower"])
        self.assertTrue(units["synthetic_email_case"])

    def test_every_case_was_asserted(self):
        self.assertEqual(set(self.report["cases"]), {entry["name"] for entry in CASES})
        for name in self.report["cases"]:
            self.run_case(name)


class Ac2MemberPrimitiveSourceTests(unittest.TestCase):
    def test_primitive_has_no_write_call_or_loop_around_the_save(self):
        source = PRIMITIVE.read_text(encoding="utf-8")
        self.assertNotIn("SaveMember(", source)
        self.assertNotIn("DeleteMember", source)
        self.assertEqual(source.count("Invoke-XbAutoCountSaveMember -Prepared"), 1)
        adapter = ADAPTER.read_text(encoding="utf-8")
        self.assertEqual(adapter.count("SaveMember("), 1)
        save_block = adapter[adapter.index("function Invoke-XbAutoCountSaveMember"):adapter.index("function Get-XbAutoCountMemberAudit")]
        for loop in ("foreach", "for (", "while", "do {"):
            self.assertNotIn(loop, save_block)
        self.assertIn('$script:XbAc2MemberCreateMutexName = "Global\\XB-AC2-MemberCreate"', adapter)
        self.assertIn("[string]$MutexName = $script:XbAc2MemberCreateMutexName", source)
        self.assertIn("[ValidateRange(0, 30000)][int]$MutexWaitMilliseconds = 30000", source)
        self.assertIn("[System.Threading.AbandonedMutexException] { $owned = $true }", source)

    def test_primitive_source_is_ascii(self):
        for path in (PRIMITIVE, ADAPTER, FAKE):
            self.assertTrue(path.read_bytes().isascii(), path.name)


if __name__ == "__main__":
    unittest.main()
