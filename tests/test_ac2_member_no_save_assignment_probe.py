import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_no_save_assignment_probe.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_no_save_assignment_probe_runbook.md"


class Ac2MemberNoSaveAssignmentProbeStaticTests(unittest.TestCase):
    def test_probe_script_requires_explicit_opt_in_and_env_password(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("C:\\Program Files\\AutoCount\\Accounting 2.2", script)
        self.assertIn("EnableMemberNoSaveAssignmentProbe", script)
        self.assertIn("AllowRootLogin", script)
        self.assertIn("AC2_PROBE_PASSWORD", script)
        self.assertIn("PasswordEnvVar", script)
        self.assertIn("GetEnvironmentVariable", script)
        self.assertRegex(script, r"(?i)refus|explicit opt-in|required")

    def test_probe_script_uses_member_command_no_save_assignment_flow(self):
        script = SCRIPT.read_text(encoding="utf-8")

        for term in [
            "AutoCount.Invoicing.dll",
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
            "MemberTable",
            "Row",
            "assignment_results",
            "required_assignment_results",
            "intake_assignment_results",
            "all_required_assignment_success",
            "all_intake_assignment_success",
            "member_type_default_assignment_success",
            "no_save_confirmed",
        ]:
            self.assertIn(term, script)

    def test_probe_script_contains_only_synthetic_fake_data_markers(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("XB Dry Run Member", script)
        self.assertIn("dryrun.member@example.invalid", script)
        self.assertIn("NO_SAVE_ASSIGNMENT_PROBE", script)
        self.assertIn("Default", script)
        self.assertIn("+6590000000", script)
        self.assertIn("1990-01-01", script)

    def test_probe_script_does_not_output_actual_next_member_number(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("next_member_no =", script)
        self.assertNotIn("next_member_no:", script)
        self.assertNotRegex(script, r"['\"]next_member_no['\"]")
        allowed = {"next_member_no_length", "next_member_no_nonempty"}
        discovered = set(re.findall(r"\bnext_member_no_[a-z_]+\b", script))
        self.assertTrue(discovered)
        self.assertTrue(discovered.issuperset(allowed))

    def test_probe_script_has_no_forbidden_member_read_write_browse_or_sql(self):
        script = SCRIPT.read_text(encoding="utf-8")

        forbidden_member_api = re.compile(
            r"(?i)\b("
            r"GetMember|LoadBrowseTable|SaveMember|DeleteMember|MemberEntity\.Save|"
            r"NewMemberType|SaveMemberType|DeleteMemberType|MemberTypeEntity\.Save"
            r")\b"
        )
        self.assertIsNone(forbidden_member_api.search(script))
        self.assertNotRegex(script, r"(?im)^\s*(?!#).*?\.\s*Save\s*\(")
        self.assertNotRegex(
            script,
            r"\b(CreateCommand|GetDataTable|GetFirstDataRow|ExecuteScalar|ExecuteNonQuery|"
            r"LoadDataSet|LoadDataTable|SimpleSaveDataSet|SimpleSaveDataTable)\b",
        )
        self.assertNotRegex(script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")

    def test_docs_and_script_have_no_real_local_runtime_values(self):
        combined = "\n".join(
            [
                SCRIPT.read_text(encoding="utf-8"),
                RUNBOOK.read_text(encoding="utf-8"),
                (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8"),
                (DOCS / "member_intake_field_mapping.md").read_text(encoding="utf-8"),
                (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8"),
            ]
        )

        self.assertNotRegex(combined, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

    def test_docs_describe_synthetic_no_save_assignment_boundary(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        research = (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8")
        mapping = (DOCS / "member_intake_field_mapping.md").read_text(encoding="utf-8")
        bridge = (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("scripts/ac2_member_no_save_assignment_probe.ps1", readme)
        self.assertIn("scripts/ac2_member_no_save_assignment_probe.ps1", runbook)
        self.assertIn("fake-data no-save assignment probe", runbook)
        self.assertIn("in-memory MemberEntity", runbook)
        self.assertIn("must not output the actual number", runbook)
        self.assertIn("synthetic fake data only", runbook)
        self.assertIn("does not read existing member/customer records", runbook)
        self.assertIn("does not browse members", runbook)
        self.assertIn("does not create/update/delete members", runbook)
        self.assertIn("does not run SQL", runbook)
        self.assertIn("runtime credentials only", runbook)
        self.assertIn("Generated outputs stay local and are not committed", runbook)
        self.assertIn("Only sanitized summary should be shared", runbook)

        self.assertIn("PR #69", research)
        self.assertIn("fake-data no-save assignment probe", research)
        self.assertIn("MemberNo", mapping)
        self.assertIn("max_length 20", mapping)
        self.assertIn("member creation remains blocked", mapping)
        self.assertIn("Fake-Data No-Save Assignment Boundary", bridge)
