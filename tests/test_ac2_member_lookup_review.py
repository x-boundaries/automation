import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_lookup_review.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_lookup_review_runbook.md"
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
        self.assertRegex(script, r"(?i)refus|explicit opt-in|required")

        refusal_index = script.index("Refusing to run")
        first_load_index = script.index("LoadFrom")
        first_factory_invoke_index = script.index("dbSettingFactory.Invoke")
        self.assertLess(refusal_index, first_load_index)
        self.assertLess(refusal_index, first_factory_invoke_index)

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
        self.assertRegex(script, r"\[regex\]::Replace\(\$RawMemberNo,\s*['\"]\\D['\"],\s*['\"]['\"]\)")
        self.assertRegex(script, r"\$digits\.Length\s+-eq\s+8")
        self.assertRegex(script, r"\$digits\.StartsWith\(['\"]8['\"]\)")
        self.assertRegex(script, r"\$digits\.StartsWith\(['\"]9['\"]\)")
        self.assertIn('"65" + $digits', script)
        self.assertRegex(script, r"\$digits\.Length\s+-eq\s+10")
        self.assertRegex(script, r"\$digits\.StartsWith\(['\"]65['\"]\)")
        self.assertIn("manual_review", script)
        self.assertIn("normalized_member_no_length", script)
        self.assertRegex(script, r"\.Substring\(0,\s*20\)")

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
        readme = README.read_text(encoding="utf-8")
        field_mapping = FIELD_MAPPING.read_text(encoding="utf-8")
        bridge = BRIDGE.read_text(encoding="utf-8")

        self.assertIn("scripts/ac2_member_lookup_review.ps1", readme)
        self.assertIn("member_lookup_review_runbook.md", readme)
        self.assertIn("scripts/ac2_member_lookup_review.ps1", runbook)
        self.assertIn("-EnableMemberLookupReview", runbook)
        self.assertIn("AC2_PROBE_PASSWORD", runbook)
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

    def test_runbook_documents_output_schema_fields_and_member_no_normalization(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")

        for field in OUTPUT_FIELDS:
            self.assertIn(f"`{field}`", runbook)

        self.assertIn("remove spaces, plus signs, dashes, brackets, dots, symbols", runbook)
        self.assertIn("8 digits starting with 8 or 9", runbook)
        self.assertIn("65XXXXXXXX", runbook)
        self.assertIn("10 digits starting with 65", runbook)
        self.assertIn("manual_review", runbook)
        self.assertIn("max 20 characters", runbook)


if __name__ == "__main__":
    unittest.main()
