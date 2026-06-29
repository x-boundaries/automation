import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_api_probe.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"


class Ac2MemberApiProbeStaticTests(unittest.TestCase):
    def test_probe_script_is_metadata_only_and_targets_installed_member_api(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("C:\\Program Files\\AutoCount\\Accounting 2.2", script)
        self.assertIn("AutoCount.Invoicing.dll", script)
        self.assertIn("AutoCount.BonusPoint.Member.MemberCommand", script)
        self.assertIn("AutoCount.BonusPoint.Member.MemberEntity", script)
        self.assertIn("AutoCount.BonusPoint.Member.MemberTypeCommand", script)
        self.assertIn("AutoCount.BonusPoint.Member.MemberTypeEntity", script)
        self.assertIn("AutoCount.BonusPoint.Member.MemberRecord", script)
        self.assertIn("AutoCount.BonusPoint.Member.MemberTypeRecord", script)
        self.assertIn("AssemblyResolve", script)
        self.assertIn("GetConstructors", script)
        self.assertIn("GetMethods", script)
        self.assertIn("GetProperties", script)
        self.assertIn("JsonOut", script)
        self.assertRegex(script, r"(?i)metadata[- ]only")

    def test_probe_script_has_no_executable_member_writeback_or_secret_material(self):
        script = SCRIPT.read_text(encoding="utf-8")

        forbidden_call = re.compile(
            r"(?im)^\s*(?!#).*?\.\s*"
            r"(SaveMember|DeleteMember|SaveMemberType|DeleteMemberType)\s*\("
        )
        self.assertIsNone(forbidden_call.search(script))
        self.assertNotRegex(script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=)")
        self.assertNotRegex(script, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")

    def test_member_intake_docs_record_discovery_and_guardrails(self):
        research = (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8")
        mapping = (DOCS / "member_intake_field_mapping.md").read_text(encoding="utf-8")
        runbook = (DOCS / "member_intake_local_probe_runbook.md").read_text(encoding="utf-8")

        self.assertIn("Installed AC2 2.2 local discovery", research)
        self.assertIn("AutoCount.Invoicing.dll", research)
        self.assertIn("AutoCount.BonusPoint.Member", research)
        self.assertIn("AutoCount.GeneralMaint.MemberMaintenance was not found", research)
        self.assertIn("constructor/bootstrap", research)

        for field in [
            "MemberNo",
            "MemberType",
            "Name",
            "MobilePhone",
            "EmailAddress",
            "DOB",
            "IsActive",
            "RegisterDate",
            "Note",
            "UDF",
        ]:
            self.assertIn(field, mapping)
        self.assertIn("Default", mapping)
        self.assertIn("operator confirmation", mapping)
        self.assertIn("PDPA/marketing consent storage remains open", mapping)

        self.assertIn("scripts/ac2_member_api_probe.ps1", runbook)
        self.assertIn("C:\\XB\\autocount_outputs\\review\\member_intake_discovery", runbook)
        self.assertIn("synthetic/test data", runbook)
        self.assertIn("no SaveMember/DeleteMember", runbook)
