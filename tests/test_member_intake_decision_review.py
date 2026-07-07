"""Synthetic tests for the member intake decision review runner.

No real member data or fixture files are committed. CSV and JSONL inputs are
created under a temporary directory at runtime with synthetic .invalid emails.
"""

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import member_intake_decision_review as decision_review
from scripts import member_intake_validate as validator


SCRIPT = ROOT / "scripts" / "member_intake_decision_review.py"
RUNBOOK = ROOT / "docs" / "autocount2-automation" / "member_intake_decision_review_runbook.md"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"


def synthetic_row(**overrides):
    row = {
        "Timestamp": "2026/07/02 10:00:00",
        "Full Name": "Synthetic Alpha",
        "AutoCount MemberNo": "9000 0001",
        "Email Address": "synthetic.alpha@example.invalid",
        "Birthday Month": "March",
        "Marketing Consent": "Yes",
        "PDPA Acknowledged": "Yes",
    }
    row.update(overrides)
    return row


def write_form_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=validator.FORM_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def lookup(row_number, *, status="ok", member_exists=False, manual_review_required=False, warning_count=0):
    return {
        "row_number": row_number,
        "status": status,
        "authentication_success": status == "ok",
        "user_session_available": status == "ok",
        "member_command_found": status == "ok",
        "get_member_found": status == "ok",
        "submitted_member_no_status": "manual_review" if manual_review_required else "canonical_65_mobile",
        "normalized_member_no_length": 10,
        "member_exists": member_exists,
        "member_found_by": "MemberCommand.GetMember" if member_exists else None,
        "manual_review_required": manual_review_required,
        "warning_count": warning_count,
        "error": {"type": "SyntheticError", "message": "sanitized lookup error"} if status == "error" else None,
    }


def write_lookup_jsonl(path, entries):
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


class DecisionReviewTests(unittest.TestCase):
    def decide_one(self, row, lookup_entry=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            form_path = tmp_path / "synthetic_form.csv"
            write_form_csv(form_path, [row])
            lookup_index = None
            if lookup_entry is not None:
                lookup_path = tmp_path / "synthetic_lookup.jsonl"
                write_lookup_jsonl(lookup_path, [lookup_entry])
                lookup_index = decision_review.load_lookup_jsonl(lookup_path)
            results, counts = decision_review.run_decision_review(
                decision_review.load_form_rows(form_path),
                lookup_index,
            )
        return results[0], counts

    def test_new_member_candidate_with_lookup_not_found_is_ready_for_create_review(self):
        result, counts = self.decide_one(synthetic_row(), lookup(2, member_exists=False))

        self.assertEqual(result["decision_code"], decision_review.READY_FOR_CREATE_REVIEW)
        self.assertEqual(counts["ready_for_create_review_count"], 1)

    def test_existing_member_lookup_found_is_existing_member_review(self):
        result, counts = self.decide_one(synthetic_row(), lookup(2, member_exists=True))

        self.assertEqual(result["decision_code"], decision_review.EXISTING_MEMBER_REVIEW)
        self.assertEqual(counts["existing_member_review_count"], 1)

    def test_manual_review_member_no_shape_is_manual_review_required(self):
        result, counts = self.decide_one(
            synthetic_row(**{"AutoCount MemberNo": "LEGACY-9000A"}),
            lookup(2, member_exists=False),
        )

        self.assertEqual(result["decision_code"], decision_review.MANUAL_REVIEW_REQUIRED)
        self.assertIn("manual_review_member_no", result["issue_codes"])
        self.assertEqual(counts["manual_review_required_count"], 1)

    def test_lookup_warning_or_manual_review_flag_is_manual_review_required(self):
        result, _ = self.decide_one(
            synthetic_row(),
            lookup(2, member_exists=False, manual_review_required=True, warning_count=1),
        )

        self.assertEqual(result["decision_code"], decision_review.MANUAL_REVIEW_REQUIRED)
        self.assertIn("lookup_manual_review_required", result["issue_codes"])

    def test_pdpa_not_acknowledged_is_pdpa_blocked(self):
        for marker in ["No", "Imported", ""]:
            result, _ = self.decide_one(
                synthetic_row(**{"PDPA Acknowledged": marker}),
                lookup(2, member_exists=False),
            )

            self.assertEqual(result["decision_code"], decision_review.PDPA_BLOCKED, marker)
            self.assertIn("pdpa_blocked", result["issue_codes"], marker)

    def test_invalid_required_fields_are_invalid_form_row(self):
        result, counts = self.decide_one(
            synthetic_row(**{"Full Name": "", "Email Address": "broken"}),
            lookup(2, member_exists=False),
        )

        self.assertEqual(result["decision_code"], decision_review.INVALID_FORM_ROW)
        self.assertIn("missing_full_name", result["issue_codes"])
        self.assertIn("invalid_email", result["issue_codes"])
        self.assertEqual(counts["invalid_form_row_count"], 1)

    def test_valid_row_without_lookup_result_is_lookup_required(self):
        result, counts = self.decide_one(synthetic_row(), None)

        self.assertEqual(result["decision_code"], decision_review.LOOKUP_REQUIRED)
        self.assertIn("lookup_required", result["issue_codes"])
        self.assertEqual(counts["lookup_required_count"], 1)

    def test_lookup_error_or_inconsistent_schema_is_lookup_error_review(self):
        error_result, _ = self.decide_one(synthetic_row(), lookup(2, status="error"))
        inconsistent_result, _ = self.decide_one(
            synthetic_row(),
            {"row_number": 2, "status": "ok", "member_exists": "false"},
        )

        self.assertEqual(error_result["decision_code"], decision_review.LOOKUP_ERROR_REVIEW)
        self.assertIn("lookup_status_error", error_result["issue_codes"])
        self.assertEqual(inconsistent_result["decision_code"], decision_review.LOOKUP_ERROR_REVIEW)
        self.assertIn("lookup_schema_invalid", inconsistent_result["issue_codes"])


class DecisionReviewCliTests(unittest.TestCase):
    def run_cli(self, arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + arguments,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_writes_pii_free_review_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            form_path = tmp_path / "synthetic_form.csv"
            lookup_path = tmp_path / "synthetic_lookup.jsonl"
            output_dir = tmp_path / "outputs"
            rows = [
                synthetic_row(),
                synthetic_row(
                    **{
                        "Full Name": "Synthetic Existing",
                        "AutoCount MemberNo": "9000 0002",
                        "Email Address": "synthetic.existing@example.invalid",
                    }
                ),
                synthetic_row(
                    **{
                        "Full Name": "Synthetic Imported",
                        "AutoCount MemberNo": "9000 0003",
                        "Email Address": "synthetic.imported@example.invalid",
                        "PDPA Acknowledged": "Imported",
                    }
                ),
            ]
            write_form_csv(form_path, rows)
            write_lookup_jsonl(
                lookup_path,
                [
                    lookup(2, member_exists=False),
                    lookup(3, member_exists=True),
                    lookup(4, member_exists=False),
                ],
            )

            completed = self.run_cli(
                [
                    "--input",
                    str(form_path),
                    "--lookup-jsonl",
                    str(lookup_path),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["mode"], "offline")
            self.assertTrue(summary["dry_run_only"])
            self.assertFalse(summary["final_write_automation"])
            self.assertEqual(summary["counts"]["ready_for_create_review_count"], 1)
            self.assertEqual(summary["counts"]["existing_member_review_count"], 1)
            self.assertEqual(summary["counts"]["pdpa_blocked_count"], 1)

            for output_name in [
                "member_intake_decision_report.md",
                "member_intake_decision_manifest.json",
                "member_intake_decision_rows.csv",
                "PRIVATE_DO_NOT_COMMIT_MEMBER_INTAKE_DECISION.txt",
            ]:
                self.assertTrue((output_dir / output_name).exists(), output_name)

            combined_output = "\n".join(
                [
                    completed.stdout,
                    (output_dir / "member_intake_decision_report.md").read_text(encoding="utf-8"),
                    (output_dir / "member_intake_decision_manifest.json").read_text(encoding="utf-8"),
                    (output_dir / "member_intake_decision_rows.csv").read_text(encoding="utf-8"),
                ]
            )
            for raw_value in [
                "Synthetic Alpha",
                "Synthetic Existing",
                "Synthetic Imported",
                "synthetic.alpha@example.invalid",
                "synthetic.existing@example.invalid",
                "synthetic.imported@example.invalid",
                "9000 0001",
                "6590000001",
                "2000-03-01",
            ]:
                self.assertNotIn(raw_value, combined_output)

            rows_csv = (output_dir / "member_intake_decision_rows.csv").read_text(encoding="utf-8")
            self.assertIn("RowNumber,ValidationStatus,LookupStatus,DecisionCode,IssueCodes", rows_csv)
            self.assertIn("2,valid,not_found,READY_FOR_CREATE_REVIEW,", rows_csv)
            self.assertIn("3,valid,found,EXISTING_MEMBER_REVIEW,", rows_csv)
            self.assertIn("4,valid,not_found,PDPA_BLOCKED,pdpa_blocked", rows_csv)

    def test_planned_live_lookup_mode_does_not_execute_lookup_and_marks_lookup_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            form_path = tmp_path / "synthetic_form.csv"
            output_dir = tmp_path / "outputs"
            write_form_csv(form_path, [synthetic_row()])

            completed = self.run_cli(["--input", str(form_path), "--output-dir", str(output_dir)])

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["mode"], "planned_live_lookup")
            self.assertEqual(summary["counts"]["lookup_required_count"], 1)
            report = (output_dir / "member_intake_decision_report.md").read_text(encoding="utf-8")
            self.assertIn("planned_live_lookup", report)
            self.assertIn("does not execute live lookup", report)


class DecisionReviewGuardrailTests(unittest.TestCase):
    def test_gitignore_protects_decision_review_outputs(self):
        gitignore = GITIGNORE.read_text(encoding="utf-8")

        for pattern in [
            "member_intake_decision_outputs/",
            "member_intake_decision_rows.csv",
            "member_intake_decision_report.md",
            "member_intake_decision_manifest.json",
            "PRIVATE_DO_NOT_COMMIT_MEMBER_INTAKE_DECISION.txt",
            "autocount_outputs/**/member_intake_decision_rows.csv",
        ]:
            self.assertIn(pattern, gitignore)

    def test_docs_state_no_writes_not_final_and_source_boundaries(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        combined = runbook + "\n" + readme

        self.assertIn("scripts/member_intake_decision_review.py", readme)
        self.assertIn("member_intake_decision_review_runbook.md", readme)
        self.assertRegex(runbook, r"(?i)dry-run review layer only")
        self.assertRegex(runbook, r"(?i)AC2.*source of truth|source of truth.*AC2")
        self.assertRegex(runbook, r"(?i)live writes remain blocked")
        self.assertRegex(runbook, r"(?i)lookup results are sanitized")
        self.assertRegex(runbook, r"(?i)row-level outputs must not contain PII")
        self.assertRegex(runbook, r"(?i)old POS.*reference-only|side sheet.*reference-only")
        self.assertIn("Imported", runbook)
        self.assertRegex(runbook, r"(?i)not.*final write automation")
        self.assertRegex(combined, r"(?i)does not create/update/delete|does not create, update, or delete")

    def test_script_has_no_autocount_write_sql_or_live_execution_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")

        forbidden_tokens = [
            "SaveMember",
            "NewMember",
            "DeleteMember",
            "GetNextMemberNo",
            "MemberCommand",
            "DBSetting",
            "UserSession",
            "pyodbc",
            "sqlite3",
            "sqlalchemy",
            "SqlConnection",
            "SqlCommand",
            "INSERT INTO",
            "DELETE FROM",
            "SELECT ",
            "subprocess",
            "requests",
            "urllib",
            "socket",
        ]
        for token in forbidden_tokens:
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main()
