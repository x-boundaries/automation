import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ac2_member_browse_extract_review.ps1"
DOCS = ROOT / "docs" / "autocount2-automation"
RUNBOOK = DOCS / "member_browse_extract_review_runbook.md"


class Ac2MemberBrowseExtractReviewStaticTests(unittest.TestCase):
    def test_script_exists_and_requires_explicit_opt_in_before_loading_or_api_calls(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("EnableMemberBrowseExtractReview", script)
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

    def test_script_uses_member_command_create_and_load_browse_table_only_after_auth(self):
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
            '"Create"',
            "LoadBrowseTable",
            "member_command_found",
            "member_command_create_found",
            "load_browse_table_found",
            "load_browse_table_success",
            "output_file_paths",
        ]:
            self.assertIn(term, script)

        auth_success_index = script.index("$result.authentication_success")
        command_create_index = script.index("memberCommandCreate.Invoke")
        browse_index = script.index("loadBrowseTableMethod.Invoke")
        self.assertLess(auth_success_index, command_create_index)
        self.assertLess(command_create_index, browse_index)

    def test_script_has_read_only_member_guardrails_no_sql_and_output_directory_only(self):
        script = SCRIPT.read_text(encoding="utf-8")

        for forbidden in [
            "SaveMember",
            "DeleteMember",
            "NewMember",
            "GetMember",
            "MemberEntity.Save",
        ]:
            self.assertNotIn(forbidden, script)

        self.assertNotRegex(script, r"(?im)^\s*(?!#).*?\.\s*Save\s*\(")
        self.assertNotRegex(
            script,
            r"\b(CreateCommand|GetDataTable|GetFirstDataRow|ExecuteScalar|ExecuteNonQuery|"
            r"LoadDataSet|LoadDataTable|SimpleSaveDataSet|SimpleSaveDataTable)\b",
        )
        self.assertNotRegex(script, r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE\s+TABLE|ALTER|DROP|TRUNCATE)\b")
        self.assertIn("OutputDirectory", script)
        self.assertIn("ac2_member_browse_extract.csv", script)
        self.assertIn("ac2_member_browse_extract_summary.json", script)
        self.assertIn("PRIVATE - DO NOT COMMIT", script)
        self.assertNotRegex(script, r"(?i)(AED_|XBOUNDARIES|xPass|localhost\\A2006)")

    def test_docs_and_readme_warn_about_pii_local_only_outputs(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        research = (DOCS / "member_intake_api_research.md").read_text(encoding="utf-8")
        bridge = (DOCS / "member_intake_local_bridge_design.md").read_text(encoding="utf-8")

        self.assertIn("scripts/ac2_member_browse_extract_review.ps1", readme)
        self.assertIn("member_browse_extract_review_runbook.md", readme)
        self.assertIn("C:\\XB\\autocount_outputs\\review\\member_browse_extract", readme)

        for text in [runbook, readme, research, bridge]:
            self.assertRegex(text, r"(?i)PII|personal data")
            self.assertRegex(text, r"(?i)must not be committed|do not commit|not committed")

        self.assertIn("read-only", runbook)
        self.assertIn("LoadBrowseTable", runbook)
        self.assertIn("AC2_PROBE_PASSWORD", runbook)
        self.assertIn("-EnableMemberBrowseExtractReview", runbook)
        self.assertIn('"localhost\\A2006"', runbook)
        self.assertIn('"AED_XBOUNDARIES"', runbook)
        self.assertIn("ac2_member_browse_extract.csv", runbook)
        self.assertIn("ac2_member_browse_extract_summary.json", runbook)
        self.assertIn("Output contains PII", runbook)

    def test_generated_member_extract_outputs_are_not_committed(self):
        generated_names = {
            "ac2_member_browse_extract.csv",
            "ac2_member_browse_extract_summary.json",
        }

        committed_like_files = [
            path
            for path in ROOT.rglob("*")
            if path.is_file()
            and path.name in generated_names
            and path.parts[-2] not in {"docs", "scripts", "tests"}
        ]

        self.assertEqual([], committed_like_files)
