import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_fake_create_probe.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_fake_create_probe_runbook.md"


class Ac2MemberFakeCreateProbeStaticTests(unittest.TestCase):
    def test_probe_script_exists_and_requires_all_write_opt_ins_before_loading(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("C:\\Program Files\\AutoCount\\Accounting 2.2", script)
        for switch_name in [
            "EnableMemberFakeCreateProbe",
            "ConfirmAutoCountWrite",
            "ConfirmSyntheticDataOnly",
            "ConfirmSingleFakeMember",
        ]:
            self.assertIn(switch_name, script)

        refusal_index = script.index("Refusing to run")
        first_load_index = script.index("LoadFrom")
        self.assertLess(refusal_index, first_load_index)
        self.assertRegex(script, r"(?i)refus|explicit opt-in|required")

    def test_probe_script_reads_password_from_env_var_only(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("AC2_PROBE_PASSWORD", script)
        self.assertIn("PasswordEnvVar", script)
        self.assertIn("[Environment]::GetEnvironmentVariable($PasswordEnvVar)", script)
        self.assertNotRegex(script, r"(?i)\[string\]\s*\$Password\b")
        self.assertNotRegex(script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")

    def test_probe_script_uses_save_gated_member_command_flow(self):
        script = SCRIPT.read_text(encoding="utf-8")

        for term in [
            "AutoCount.dll",
            "AutoCount.Accounting.dll",
            "AutoCount.Invoicing.dll",
            "AutoCount.ImportExport.dll",
            "AutoCount.Tools.dll",
            "AutoCount.BonusPoint.Member.MemberCommand",
            "CreateAutoCountDefaultDBSetting",
            "UserSession",
            "Authenticate",
            '"Login"',
            "SetAsCurrent",
            "CheckHasLogined",
            "MemberCommand.Create",
            "GetNextMemberNo",
            "NewMember",
            "SaveMember",
            "MemberTable",
            "Row",
        ]:
            self.assertIn(term, script)

        self.assertIn("save_member_method_found", script)
        self.assertIn("save_member_attempted", script)
        self.assertIn("save_member_success", script)
        self.assertIn("created_fake_member_no_masked", script)
        self.assertIn("manual_cleanup_required", script)

    def test_probe_script_contains_only_required_synthetic_fake_member_values(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("XB API SAVE PROBE", script)
        self.assertIn("xb.api.save.probe@example.invalid", script)
        self.assertIn("XB_AUTOMATION_FAKE_CREATE_PROBE_DELETE_ME", script)
        self.assertIn("90000000", script)
        self.assertIn("1990-01-01", script)
        self.assertIn("2026-01-01", script)

    def test_probe_script_has_no_forbidden_reads_deletes_member_type_writes_or_sql(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertNotRegex(script, r"(?i)\b(GetMember|LoadBrowseTable|DeleteMember)\b")
        self.assertNotRegex(
            script,
            r"(?i)\b(NewMemberType|SaveMemberType|DeleteMemberType|MemberTypeEntity\.Save)\b",
        )
        self.assertNotIn("MemberEntity.Save", script)
        self.assertNotRegex(script, r"(?im)^\s*(?!#).*?\.\s*Save\s*\(")
        self.assertNotRegex(
            script,
            r"\b(CreateCommand|GetDataTable|GetFirstDataRow|ExecuteScalar|ExecuteNonQuery|"
            r"LoadDataSet|LoadDataTable|SimpleSaveDataSet|SimpleSaveDataTable)\b",
        )
        self.assertNotRegex(script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")

    def test_probe_script_masks_safe_summary_member_number(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("Get-MaskedMemberNo", script)
        self.assertIn('Substring(0, 2) + "***" +', script)
        self.assertIn('created_fake_member_no_local', script)
        self.assertIn('created_fake_member_no_masked = Get-MaskedMemberNo $generatedNumber', script)
        self.assertNotRegex(script, r"['\"]created_fake_member_no['\"]")

    def test_docs_and_script_do_not_commit_real_runtime_values_outside_required_runbook_command(self):
        combined_without_runbook = "\n".join(
            [
                SCRIPT.read_text(encoding="utf-8"),
                (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8"),
                (DOCS / "member_intake_field_mapping.md").read_text(encoding="utf-8"),
                (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8"),
                (ROOT / "README.md").read_text(encoding="utf-8"),
            ]
        )

        self.assertNotRegex(combined_without_runbook, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

        runbook = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn('"localhost\\A2006"', runbook)
        self.assertIn('"AED_XBOUNDARIES"', runbook)

    def test_docs_describe_write_boundary_cleanup_and_no_existing_member_reads(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        research = (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8")
        mapping = (DOCS / "member_intake_field_mapping.md").read_text(encoding="utf-8")
        bridge = (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("scripts/ac2_member_fake_create_probe.ps1", readme)
        self.assertIn("member_fake_create_probe_runbook.md", readme)
        self.assertIn("first write-capable probe", runbook)
        self.assertIn("writes exactly one fake member", runbook)
        self.assertIn("creates one fake member in AutoCount", runbook)
        self.assertIn("synthetic fake data only", runbook)
        self.assertIn("does not read existing member/customer records", runbook)
        self.assertIn("does not browse members", runbook)
        self.assertIn("does not delete or cleanup automatically", runbook)
        self.assertIn("Manual cleanup may be needed", runbook)
        self.assertIn("Generated output stays local and is not committed", runbook)
        self.assertIn("Share only sanitized summary", runbook)

        self.assertIn("PR #70", research)
        self.assertIn("save-gated fake member create proof", research)
        self.assertIn("production member creation remains blocked", mapping)
        self.assertIn("Save-Gated Fake Member Create Boundary", bridge)
