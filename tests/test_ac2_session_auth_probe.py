import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_session_auth_probe.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"


class Ac2SessionAuthProbeStaticTests(unittest.TestCase):
    def test_session_auth_probe_requires_explicit_opt_in_and_env_password(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("C:\\Program Files\\AutoCount\\Accounting 2.2", script)
        self.assertIn("EnableSessionProbe", script)
        self.assertIn("AC2_PROBE_PASSWORD", script)
        self.assertIn("PasswordEnvVar", script)
        self.assertIn("GetEnvironmentVariable", script)
        self.assertIn("CreateAutoCountDefaultDBSetting", script)
        self.assertIn("UserSession", script)
        self.assertIn("Authenticate", script)
        self.assertIn('"Login"', script)
        self.assertIn("SetAsCurrent", script)
        self.assertIn("CheckHasLogined", script)
        self.assertIn("IsLogin", script)
        self.assertIn("get_CurrentUserSession", script)
        self.assertIn("JsonOut", script)
        self.assertRegex(script, r"(?i)refuse|requires.*EnableSessionProbe|explicit opt-in")
        self.assertIn("static_auth_success", script)
        self.assertIn("instance_login_method_found", script)
        self.assertIn("instance_login_success", script)
        self.assertIn("instance_is_login", script)
        self.assertIn("set_as_current_called", script)
        self.assertIn("current_session_available_after_set", script)
        self.assertIn("check_has_logined_success", script)

    def test_session_auth_probe_has_no_member_factory_read_write_sql_or_secret_literals(self):
        script = SCRIPT.read_text(encoding="utf-8")

        forbidden_member_calls = re.compile(
            r"(?im)^\s*(?!#).*?(MemberCommand|MemberTypeCommand)\s*::\s*Create\s*\(|"
            r"^\s*(?!#).*?\.\s*(LoadBrowseTable|GetMember|GetMemberType|NewMember|"
            r"NewMemberType|SaveMember|DeleteMember|SaveMemberType|DeleteMemberType|Save)\s*\("
        )
        self.assertIsNone(forbidden_member_calls.search(script))
        self.assertNotRegex(script, r"\bCurrentUserTable\b")
        self.assertNotRegex(
            script,
            r"\b(CreateCommand|GetDataTable|GetFirstDataRow|ExecuteScalar|ExecuteNonQuery|"
            r"LoadDataSet|LoadDataTable|SimpleSaveDataSet|SimpleSaveDataTable)\b",
        )
        self.assertNotRegex(script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(script, r"(?i)(Password\s*=|PWD\s*=|User\s+ID\s*=|Server\s*=|Database\s*=)")
        self.assertNotRegex(script, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

    def test_session_auth_docs_explain_runtime_only_auth_boundary(self):
        runbook = (DOCS / "member_intake_session_auth_probe_runbook.md").read_text(encoding="utf-8")
        research = (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8")
        bridge = (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("scripts/ac2_session_auth_probe.ps1", readme)
        self.assertIn("scripts/ac2_session_auth_probe.ps1", runbook)
        self.assertIn("first explicit opt-in live authentication probe", runbook)
        self.assertIn("PasswordEnvVar", runbook)
        self.assertIn("AC2_PROBE_PASSWORD", runbook)
        self.assertIn("runtime-password", runbook)
        self.assertIn("C:\\XB\\autocount_outputs\\review\\member_intake_discovery", runbook)
        self.assertIn("does not read/list/create/update/delete members", runbook)
        self.assertIn("does not call MemberCommand.Create", runbook)
        self.assertIn("does not call MemberTypeCommand.Create", runbook)
        self.assertIn("does not run SQL", runbook)
        self.assertIn("instance UserSession.Login", runbook)
        self.assertIn("Static Authenticate returned false with no exception", runbook)
        self.assertNotRegex(runbook, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

        self.assertIn("CreateAutoCountDefaultDBSetting", research)
        self.assertIn("UserSession.Authenticate", research)
        self.assertIn("UserSession.Login", research)
        self.assertIn("Static Authenticate returned false with no exception", research)
        self.assertIn("session/auth probe", research)
        self.assertIn("member read/write blocked", research)

        self.assertIn("Session/Auth Boundary", bridge)
        self.assertIn("Do not call member factories", bridge)
        self.assertIn("No member list/read", bridge)
        self.assertIn("instance login diagnostics", bridge)
