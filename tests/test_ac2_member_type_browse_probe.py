import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_type_browse_probe.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_type_browse_probe_runbook.md"


class Ac2MemberTypeBrowseProbeStaticTests(unittest.TestCase):
    def test_member_type_browse_probe_exists_and_requires_explicit_opt_in(self):
        self.assertTrue(SCRIPT.exists())
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("C:\\Program Files\\AutoCount\\Accounting 2.2", script)
        self.assertIn("EnableMemberTypeBrowseProbe", script)
        self.assertIn("AC2_PROBE_PASSWORD", script)
        self.assertIn("PasswordEnvVar", script)
        self.assertIn("GetEnvironmentVariable", script)
        self.assertIn("CreateAutoCountDefaultDBSetting", script)
        self.assertIn("UserSession", script)
        self.assertIn("Authenticate", script)
        self.assertIn('"Login"', script)
        self.assertIn("AllowRootLogin", script)
        self.assertIn("AutoCount.BonusPoint.Member.MemberTypeCommand", script)
        self.assertIn('"Create"', script)
        self.assertIn("LoadBrowseTable", script)
        self.assertIn("MaxRows", script)
        self.assertIn("default_member_type_seen", script)
        self.assertRegex(script, r"(?i)refuse|requires.*EnableMemberTypeBrowseProbe|explicit opt-in")

    def test_member_type_browse_probe_has_no_forbidden_member_or_data_calls(self):
        script = SCRIPT.read_text(encoding="utf-8")

        forbidden_calls = re.compile(
            r"(?im)^\s*(?!#).*?MemberCommand\s*::\s*Create\s*\(|"
            r"^\s*(?!#).*?\.\s*(GetMember|NewMember|SaveMember|DeleteMember|"
            r"NewMemberType|SaveMemberType|DeleteMemberType|Save)\s*\("
        )
        self.assertIsNone(forbidden_calls.search(script))
        self.assertNotRegex(script, r"\bMemberEntity\.Save\b")
        self.assertNotRegex(script, r"\bMemberTypeEntity\.Save\b")
        self.assertNotRegex(
            script,
            r"\b(CreateCommand|GetDataTable|GetFirstDataRow|ExecuteScalar|ExecuteNonQuery|"
            r"LoadDataSet|LoadDataTable|SimpleSaveDataSet|SimpleSaveDataTable)\b",
        )
        self.assertNotRegex(script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")
        self.assertNotRegex(script, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

    def test_member_type_browse_docs_explain_read_only_boundary(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        research = (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8")
        field_mapping = (DOCS / "member_intake_field_mapping.md").read_text(encoding="utf-8")
        bridge = (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("scripts/ac2_member_type_browse_probe.ps1", readme)
        self.assertIn("member_type_browse_probe_runbook.md", readme)
        self.assertIn("first read-only member API probe after auth was proven", runbook)
        self.assertIn("MemberTypeCommand.LoadBrowseTable", runbook)
        self.assertIn("does not read member/customer records", runbook)
        self.assertIn("does not create/update/delete member types", runbook)
        self.assertIn("does not create/update/delete members", runbook)
        self.assertIn("does not run SQL", runbook)
        self.assertIn("runtime-password", runbook)
        self.assertIn("safe summary", runbook)
        self.assertNotRegex(runbook, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

        self.assertIn("auth/session bootstrap result", research)
        self.assertIn("read-only MemberType browse", research)
        self.assertIn("MemberType unresolved", field_mapping)
        self.assertIn("read-only member API boundary", bridge)
