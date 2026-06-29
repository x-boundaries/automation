import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_bootstrap_api_probe.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"


class Ac2BootstrapApiProbeStaticTests(unittest.TestCase):
    def test_probe_script_targets_bootstrap_session_metadata(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("C:\\Program Files\\AutoCount\\Accounting 2.2", script)
        for dll in [
            "AutoCount.dll",
            "AutoCount.Accounting.dll",
            "AutoCount.Invoicing.dll",
            "AutoCount.ImportExport.dll",
            "AutoCount.Tools.dll",
        ]:
            self.assertIn(dll, script)
        self.assertIn("AutoCount.Authentication.UserSession", script)
        self.assertIn("AutoCount.Data.DBSetting", script)
        for term in [
            "UserSession",
            "DBSetting",
            "Login",
            "Authentication",
            "Auth",
            "AccountBook",
            "Company",
            "Database",
            "DB",
            "Session",
        ]:
            self.assertIn(term, script)
        self.assertIn("AssemblyResolve", script)
        self.assertIn("GetConstructors", script)
        self.assertIn("GetMethods", script)
        self.assertIn("GetProperties", script)
        self.assertIn("JsonOut", script)
        self.assertRegex(script, r"(?i)metadata[-/ ]only|reflection[-/ ]only")

    def test_probe_script_has_no_factory_invocation_writes_or_secret_material(self):
        script = SCRIPT.read_text(encoding="utf-8")

        forbidden_call = re.compile(
            r"(?im)^\s*(?!#).*?\.\s*"
            r"(Create|LoadBrowseTable|GetMember|GetMemberType|NewMember|NewMemberType|"
            r"SaveMember|DeleteMember|SaveMemberType|DeleteMemberType|Save)\s*\("
        )
        self.assertIsNone(forbidden_call.search(script))
        self.assertNotRegex(script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=)")
        self.assertNotRegex(script, r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")

    def test_docs_record_factory_discovery_and_bootstrap_unknown(self):
        research = (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8")
        bridge = (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8")
        runbook = (DOCS / "member_intake_bootstrap_probe_runbook.md").read_text(encoding="utf-8")

        for text in [research, bridge, runbook]:
            self.assertIn("UserSession", text)
            self.assertIn("DBSetting", text)

        self.assertIn("MemberCommand", research)
        self.assertIn("public static Create", research)
        self.assertIn("MemberTypeCommand", research)
        self.assertIn("official UserSession", research)
        self.assertIn("Do not reflect-call internal constructors", bridge)
        self.assertIn("No live read/list/create", bridge)
        self.assertIn("scripts/ac2_bootstrap_api_probe.ps1", runbook)
        self.assertIn("C:\\XB\\autocount_outputs\\review\\member_intake_discovery", runbook)
        self.assertIn("no command factory invocation", runbook)
