import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_lookup_review.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_lookup_review_runbook.md"
DIRECT_RUNBOOK = DOCS / "member_intake_n8n_direct_lookup_runbook.md"
README = ROOT / "README.md"
FIELD_MAPPING = DOCS / "member_intake_field_mapping.md"
BRIDGE = DOCS / "member_intake_local_bridge_design.md"

OUTPUT_FIELDS = [
    "status",
    "authentication_success",
    "user_session_available",
    "member_command_found",
    "get_member_found",
    "submitted_member_no_status",
    "normalized_member_no_length",
    "member_exists",
    "member_found_by",
    "manual_review_required",
    "warning_count",
    "error",
]


class Ac2MemberLookupReviewStaticTests(unittest.TestCase):
    def test_script_exists_and_requires_explicit_lookup_review_opt_in(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("EnableMemberLookupReview", script)
        self.assertIn("[string]$MemberNo", script)
        self.assertIn("[string]$MemberNoBase64Utf8", script)
        self.assertRegex(script, r"(?i)refus|explicit opt-in|required")

        refusal_index = script.index("Refusing to run")
        first_load_index = script.index("LoadFrom")
        first_factory_invoke_index = script.index("dbSettingFactory.Invoke")
        self.assertLess(refusal_index, first_load_index)
        self.assertLess(refusal_index, first_factory_invoke_index)

    def test_script_accepts_exactly_one_member_no_input_source(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("Resolve-MemberNoInput", script)
        self.assertIn('$PSBoundParameters.ContainsKey("MemberNo")', script)
        self.assertIn('$PSBoundParameters.ContainsKey("MemberNoBase64Utf8")', script)
        self.assertRegex(script, r"\$HasMemberNo\s+-and\s+\$HasMemberNoBase64Utf8")
        self.assertRegex(script, r"-not\s+\$HasMemberNo\)\s+-and\s+\(-not\s+\$HasMemberNoBase64Utf8")
        self.assertEqual(script.count("Exactly one of MemberNo or MemberNoBase64Utf8 must be supplied."), 2)
        self.assertIn('return Get-RequiredValue "MemberNo" $RawMemberNo "MemberNo"', script)

    def test_script_decodes_member_no_base64_utf8_with_sanitized_failure(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("[System.Convert]::FromBase64String($EncodedMemberNo)", script)
        self.assertIn("[System.Text.UTF8Encoding]::new($false, $true)", script)
        self.assertIn("$strictUtf8.GetString($decodedBytes)", script)
        self.assertIn("MemberNoBase64Utf8 could not be decoded as UTF-8 base64.", script)
        self.assertIn("Get-SanitizedMessage $_.Exception.Message @($normalizedForSecretScrub, $memberForReview)", script)
        self.assertNotRegex(script, r"(?i)throw\s+\$_\.Exception\.Message")
        self.assertNotRegex(script, r"(?i)ConvertTo-Json.{0,120}\$memberForReview")

    def test_script_does_not_output_raw_decoded_member_no(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertNotRegex(script, r"(?i)\b(raw|decoded|normalized)_?member_?no\b\s*=")
        self.assertNotRegex(script, r"(?i)(member_no|memberno)_?(value|raw|decoded|normalized)")
        self.assertIn("normalized_member_no_length", script)
        self.assertIn("submitted_member_no_status", script)

    def test_script_reads_password_only_from_required_environment_variable(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("AC2_PROBE_PASSWORD", script)
        self.assertIn('[Environment]::GetEnvironmentVariable("AC2_PROBE_PASSWORD")', script)
        self.assertNotRegex(script, r"(?i)\[string\]\s*\$Password\b")
        self.assertNotIn("PasswordEnvVar", script)
        self.assertNotRegex(script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")

    def test_script_uses_proven_member_command_get_member_path_only_after_auth(self):
        script = SCRIPT.read_text(encoding="utf-8")

        for term in [
            "AutoCount.dll",
            "AutoCount.Invoicing.dll",
            "AutoCount.BonusPoint.Member.MemberCommand",
            "CreateAutoCountDefaultDBSetting",
            "UserSession",
            "Authenticate",
            '"Login"',
            "SetAsCurrent",
            "CheckHasLogined",
            "AllowRootLogin",
            '"Create"',
            '"GetMember"',
            "MemberCommand.Create",
            "MemberCommand.GetMember",
            "member_command_found",
            "get_member_found",
        ]:
            self.assertIn(term, script)

        auth_success_index = script.index("$result.authentication_success")
        command_create_index = script.index("memberCommandCreate.Invoke")
        get_member_index = script.index("getMemberMethod.Invoke")
        self.assertLess(auth_success_index, command_create_index)
        self.assertLess(command_create_index, get_member_index)

    def test_script_normalizes_submitted_member_no_like_form_validator(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("Normalize-MemberNo", script)
        self.assertIn('[regex]::Replace($RawMemberNo, "[^A-Za-z0-9]", "")', script)
        self.assertIn('[regex]::IsMatch($cleaned, "^\\d+$")', script)
        self.assertRegex(script, r"\$isAllDigits\s+-and\s+\$cleaned\.Length\s+-eq\s+8")
        self.assertRegex(script, r"\$cleaned\.StartsWith\(['\"]8['\"]\)")
        self.assertRegex(script, r"\$cleaned\.StartsWith\(['\"]9['\"]\)")
        self.assertIn('"65" + $cleaned', script)
        self.assertRegex(script, r"\$isAllDigits\s+-and\s+\$cleaned\.Length\s+-eq\s+10")
        self.assertRegex(script, r"\$cleaned\.StartsWith\(['\"]65['\"]\)")
        self.assertIn("manual_review", script)
        self.assertIn("normalized_member_no_length", script)
        self.assertNotRegex(script, r"\.Substring\(0,\s*20\)")

    def test_script_rejects_over_20_cleaned_member_no_before_lookup_without_truncation(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("invalid_too_long", script)
        self.assertIn("TooLong", script)
        self.assertIn('Submitted value exceeds AutoCount MemberNo length after cleaning.', script)
        self.assertNotRegex(script, r"\.Substring\(0,\s*20\)")

        length_assignment_index = script.index("$result.normalized_member_no_length = $normalization.Value.Length")
        too_long_check_index = script.index("if ($normalization.TooLong)")
        resolver_add_index = script.index("add_AssemblyResolve")
        core_load_index = script.index("$coreAssembly = [System.Reflection.Assembly]::LoadFrom")
        get_member_index = script.index("getMemberMethod.Invoke")
        self.assertLess(length_assignment_index, too_long_check_index)
        self.assertLess(too_long_check_index, resolver_add_index)
        self.assertLess(too_long_check_index, core_load_index)
        self.assertLess(too_long_check_index, get_member_index)

    def test_script_preserves_alphanumeric_manual_review_shapes(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('$cleaned = [regex]::Replace($RawMemberNo, "[^A-Za-z0-9]", "")', script)
        self.assertIn("$normalized = $cleaned", script)
        self.assertIn("$isAllDigits", script)
        self.assertNotIn('[regex]::Replace($RawMemberNo, "\\D", "")', script)
        self.assertRegex(script, r"elseif\s*\(\$isAllDigits\s+-and\s+\$cleaned\.Length\s+-eq\s+10")

    def test_script_outputs_only_sanitized_schema_and_no_pii_fields(self):
        script = SCRIPT.read_text(encoding="utf-8")

        for field in OUTPUT_FIELDS:
            self.assertIn(field, script)

        for pii_field in [
            "Name",
            "Email",
            "EmailAddress",
            "MobilePhone",
            "DOB",
            "Address",
            "AutoKey",
            "Guid",
        ]:
            self.assertNotRegex(script, rf"(?i)(member|row|entity).{{0,80}}\b{pii_field}\b")

        self.assertNotRegex(script, r"(?i)normalized_member_no\s*=")
        self.assertRegex(script, r"member_exists\s*=\s*\$false")
        self.assertRegex(script, r"member_found_by\s*=\s*\$null")

    def test_script_has_read_only_guardrails_no_browse_writes_or_direct_sql(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("LoadBrowseTable", script)
        self.assertNotIn("NewMember", script)
        self.assertNotIn("SaveMember", script)
        self.assertNotIn("DeleteMember", script)
        self.assertNotIn("GetNextMemberNo", script)
        self.assertNotRegex(script, r"(?im)^\s*(?!#).*?\.\s*Save\s*\(")
        self.assertNotRegex(
            script,
            r"\b(CreateCommand|GetDataTable|GetFirstDataRow|ExecuteScalar|ExecuteNonQuery|"
            r"LoadDataSet|LoadDataTable|SimpleSaveDataSet|SimpleSaveDataTable)\b",
        )
        self.assertNotRegex(script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(script, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

    def test_runbook_readme_and_design_docs_describe_safe_member_lookup_boundary(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        direct_runbook = DIRECT_RUNBOOK.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        field_mapping = FIELD_MAPPING.read_text(encoding="utf-8")
        bridge = BRIDGE.read_text(encoding="utf-8")

        self.assertIn("scripts/ac2_member_lookup_review.ps1", readme)
        self.assertIn("member_lookup_review_runbook.md", readme)
        self.assertIn("member_intake_n8n_direct_lookup_runbook.md", readme)
        self.assertIn("scripts/ac2_member_lookup_review.ps1", runbook)
        self.assertIn("scripts/ac2_member_lookup_review.ps1", direct_runbook)
        self.assertIn("-EnableMemberLookupReview", runbook)
        self.assertIn("AC2_PROBE_PASSWORD", runbook)
        self.assertIn("MemberNoBase64Utf8", runbook)
        self.assertIn("MemberCommand.GetMember", runbook)
        self.assertIn("read-only lookup only", runbook)
        self.assertIn("future n8n/local bridge duplicate checking", runbook)
        self.assertIn("must not be used as final write automation", runbook)
        self.assertRegex(runbook, r"(?i)sanitized.*PII-free|PII-free.*sanitized")

        for text in [runbook, readme, field_mapping, bridge]:
            self.assertRegex(text, r"(?i)AC2|AutoCount 2\.0")
            self.assertRegex(text, r"(?i)source of truth")
            self.assertRegex(text, r"(?i)MobilePhone.*intentionally unused|intentionally unused.*MobilePhone")
            self.assertRegex(text, r"(?i)does not create/update/delete|does not create, update, or delete")
            self.assertNotRegex(text, r"(?i)(?<!not be used as )final write automation")

    def test_direct_lookup_runbook_documents_local_n8n_contract_and_no_writes(self):
        direct_runbook = DIRECT_RUNBOOK.read_text(encoding="utf-8")
        combined = "\n".join(
            [
                direct_runbook,
                RUNBOOK.read_text(encoding="utf-8"),
                BRIDGE.read_text(encoding="utf-8"),
                README.read_text(encoding="utf-8"),
            ]
        )

        self.assertRegex(direct_runbook, r"(?i)local self-hosted n8n|self-hosted local n8n")
        self.assertRegex(direct_runbook, r"(?i)Cloud n8n cannot call local AC2 PowerShell")
        self.assertIn("MemberNoBase64Utf8", direct_runbook)
        self.assertRegex(direct_runbook, r"(?i)not raw `MemberNo`")
        self.assertRegex(direct_runbook, r"(?i)AC2_PROBE_PASSWORD.*local environment secret")
        self.assertRegex(direct_runbook, r"(?i)sanitized JSON only")
        self.assertRegex(direct_runbook, r"(?i)status != ok.*lookup error review")
        self.assertRegex(direct_runbook, r"(?i)manual_review_required = true.*manual review")
        self.assertRegex(direct_runbook, r"(?i)member_exists = true.*existing member review")
        self.assertRegex(direct_runbook, r"(?i)member_exists = false.*ready for create review")
        self.assertRegex(direct_runbook, r"(?i)Base64.*shell interpolation risk|shell interpolation risk.*Base64")
        self.assertRegex(direct_runbook, r"(?i)not final write automation|does not authorize final write automation")
        self.assertRegex(direct_runbook, r"(?i)does not create a production n8n workflow")
        self.assertRegex(combined, r"(?i)does not create/update/delete|does not create, update, or delete")

    def test_runbook_documents_output_schema_fields_and_member_no_normalization(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")

        for field in OUTPUT_FIELDS:
            self.assertIn(f"`{field}`", runbook)

        self.assertIn("remove spaces, plus signs, dashes, brackets, dots, underscores, and symbols", runbook)
        self.assertIn("8 digits starting with 8 or 9", runbook)
        self.assertIn("65XXXXXXXX", runbook)
        self.assertIn("10 digits starting with 65", runbook)
        self.assertIn("manual_review", runbook)
        self.assertIn("over 20 characters", runbook)
        self.assertIn("rejected before lookup", runbook)
        self.assertIn("manual-review shapes may be looked up only if they are 20 characters or fewer", runbook)
        self.assertNotRegex(runbook, r"(?i)truncate|truncated")


if __name__ == "__main__":
    unittest.main()
