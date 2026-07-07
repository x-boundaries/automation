"""Tests for the dry-run Google Form member intake validator.

All rows here are synthetic fake data only. Synthetic emails use the
reserved .invalid TLD and synthetic member numbers use the 9000000x /
8000000x series. No fixture files are committed; every CSV is written to a
temporary directory at runtime.
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

from scripts import member_intake_validate as validator

SCRIPT_PATH = ROOT / "scripts" / "member_intake_validate.py"


def synthetic_row(**overrides):
    row = {
        "Timestamp": "2026/07/01 10:00:00",
        "Full Name": "Synthetic Alpha",
        "AutoCount MemberNo": "9000 0001",
        "Email Address": "synthetic.alpha@example.invalid",
        "Birthday Month": "March",
        "Marketing Consent": "Yes",
        "PDPA Acknowledged": "Yes",
    }
    row.update(overrides)
    return row


def write_form_csv(path, rows, headers=None, encoding="utf-8"):
    headers = headers or validator.FORM_HEADERS
    with open(path, "w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_ac2_extract_csv(path, rows, headers=("MemberNo", "EmailAddress")):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(headers))
        writer.writeheader()
        writer.writerows(rows)


def error_codes(result):
    return [entry["code"] for entry in result["errors"]]


class MemberNoNormalizationTests(unittest.TestCase):
    def test_eight_digit_sg_mobile_starting_9_canonicalizes_to_65_prefix(self):
        result = validator.normalize_member_no("9000 0001")

        self.assertEqual(result["value"], "6590000001")
        self.assertEqual(result["status"], "canonical")

    def test_eight_digit_sg_mobile_starting_8_canonicalizes_to_65_prefix(self):
        result = validator.normalize_member_no("8000-0002")

        self.assertEqual(result["value"], "6580000002")
        self.assertEqual(result["status"], "canonical")

    def test_already_canonical_ten_digit_65_value_stays_unchanged(self):
        result = validator.normalize_member_no("6590000003")

        self.assertEqual(result["value"], "6590000003")
        self.assertEqual(result["status"], "canonical")

    def test_formatting_symbols_are_removed_before_canonicalization(self):
        for raw in ["+65 9000-0004", "(65) 9000.0004", "[65]9000_0004", "65 9000 0004"]:
            result = validator.normalize_member_no(raw)

            self.assertEqual(result["value"], "6590000004", raw)
            self.assertEqual(result["status"], "canonical", raw)

    def test_ambiguous_digit_shapes_are_kept_but_marked_manual_review(self):
        for raw in ["12345", "6123456", "0123456789", "790000005"]:
            result = validator.normalize_member_no(raw)

            self.assertEqual(result["status"], "manual_review", raw)
            self.assertTrue(result["value"].isdigit(), raw)

    def test_alphanumeric_value_is_kept_cleaned_and_marked_manual_review(self):
        result = validator.normalize_member_no(" LEGACY-9000A ")

        self.assertEqual(result["value"], "LEGACY9000A")
        self.assertEqual(result["status"], "manual_review")

    def test_value_longer_than_twenty_characters_is_an_error(self):
        result = validator.normalize_member_no("9" * 21)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "member_no_too_long")

    def test_missing_value_is_an_error(self):
        result = validator.normalize_member_no("   ")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "missing_member_no")

    def test_symbols_only_value_is_an_error(self):
        result = validator.normalize_member_no("+-() .")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "member_no_empty_after_cleaning")


class ValidateRowTests(unittest.TestCase):
    def test_valid_row_builds_full_normalized_shape(self):
        result = validator.validate_row(synthetic_row())

        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            result["normalized"],
            {
                "submitted_at": "2026/07/01 10:00:00",
                "member_no": "6590000001",
                "member_no_status": "canonical",
                "name": "Synthetic Alpha",
                "email_address": "synthetic.alpha@example.invalid",
                "dob": "2000-03-01",
                "birthday_month": "March",
                "mobile_phone": "",
                "pdpa_acknowledged": True,
                "marketing_allowed": True,
                "sync_eligible": True,
            },
        )

    def test_mobile_phone_is_intentionally_blank_not_missing_data(self):
        result = validator.validate_row(synthetic_row())

        self.assertTrue(result["valid"])
        self.assertEqual(result["normalized"]["mobile_phone"], "")
        self.assertTrue(result["sync_eligible"])

    def test_full_name_is_trimmed_and_repeated_spaces_collapse(self):
        result = validator.validate_row(synthetic_row(**{"Full Name": "  Synthetic   Alpha  "}))

        self.assertTrue(result["valid"])
        self.assertEqual(result["normalized"]["name"], "Synthetic Alpha")

    def test_missing_full_name_is_an_error(self):
        result = validator.validate_row(synthetic_row(**{"Full Name": "   "}))

        self.assertFalse(result["valid"])
        self.assertEqual(error_codes(result), ["missing_full_name"])
        self.assertIsNone(result["normalized"])

    def test_email_is_trimmed_and_lowercased(self):
        result = validator.validate_row(
            synthetic_row(**{"Email Address": "  SYNTHETIC.Alpha@Example.INVALID  "})
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["normalized"]["email_address"], "synthetic.alpha@example.invalid")

    def test_invalid_email_is_an_error(self):
        result = validator.validate_row(synthetic_row(**{"Email Address": "not an email"}))

        self.assertFalse(result["valid"])
        self.assertEqual(error_codes(result), ["invalid_email"])

    def test_missing_email_is_an_error(self):
        result = validator.validate_row(synthetic_row(**{"Email Address": ""}))

        self.assertFalse(result["valid"])
        self.assertEqual(error_codes(result), ["missing_email"])

    def test_birthday_month_maps_to_sentinel_dob(self):
        expectations = {
            "January": "2000-01-01",
            "march": "2000-03-01",
            "DECEMBER": "2000-12-01",
            " September ": "2000-09-01",
        }
        for raw, dob in expectations.items():
            result = validator.validate_row(synthetic_row(**{"Birthday Month": raw}))

            self.assertTrue(result["valid"], raw)
            self.assertEqual(result["normalized"]["dob"], dob, raw)

    def test_unrecognized_birthday_month_is_an_error(self):
        result = validator.validate_row(synthetic_row(**{"Birthday Month": "Marchtober"}))

        self.assertFalse(result["valid"])
        self.assertEqual(error_codes(result), ["invalid_birthday_month"])

    def test_missing_birthday_month_is_an_error(self):
        result = validator.validate_row(synthetic_row(**{"Birthday Month": ""}))

        self.assertFalse(result["valid"])
        self.assertEqual(error_codes(result), ["missing_birthday_month"])

    def test_pdpa_yes_is_acknowledged_case_insensitively(self):
        for raw in ["Yes", "yes", "YES", " Yes "]:
            result = validator.validate_row(synthetic_row(**{"PDPA Acknowledged": raw}))

            self.assertTrue(result["valid"], raw)
            self.assertFalse(result["pdpa_blocked"], raw)
            self.assertTrue(result["sync_eligible"], raw)

    def test_legacy_pdpa_i_agree_is_acknowledged_for_older_exports_only(self):
        for raw in ["I agree", "i agree", "I AGREE", " I  agree "]:
            result = validator.validate_row(synthetic_row(**{"PDPA Acknowledged": raw}))

            self.assertTrue(result["valid"], raw)
            self.assertFalse(result["pdpa_blocked"], raw)
            self.assertTrue(result["sync_eligible"], raw)

    def test_pdpa_missing_blocks_sync_eligibility_but_row_stays_valid(self):
        result = validator.validate_row(synthetic_row(**{"PDPA Acknowledged": ""}))

        self.assertTrue(result["valid"])
        self.assertTrue(result["pdpa_blocked"])
        self.assertFalse(result["sync_eligible"])
        self.assertFalse(result["normalized"]["sync_eligible"])

    def test_pdpa_not_agreed_blocks_sync_eligibility_but_row_stays_valid(self):
        for raw in ["No", "I disagree", "I have read the terms"]:
            result = validator.validate_row(synthetic_row(**{"PDPA Acknowledged": raw}))

            self.assertTrue(result["valid"], raw)
            self.assertTrue(result["pdpa_blocked"], raw)
            self.assertFalse(result["sync_eligible"], raw)

    def test_marketing_consent_no_does_not_block_registration(self):
        result = validator.validate_row(synthetic_row(**{"Marketing Consent": "no"}))

        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["sync_eligible"])
        self.assertFalse(result["normalized"]["marketing_allowed"])

    def test_missing_marketing_consent_is_an_error(self):
        result = validator.validate_row(synthetic_row(**{"Marketing Consent": ""}))

        self.assertFalse(result["valid"])
        self.assertEqual(error_codes(result), ["missing_marketing_consent"])

    def test_unrecognized_marketing_consent_is_an_error(self):
        result = validator.validate_row(synthetic_row(**{"Marketing Consent": "Maybe"}))

        self.assertFalse(result["valid"])
        self.assertEqual(error_codes(result), ["invalid_marketing_consent"])

    def test_manual_review_member_no_keeps_row_valid_but_not_sync_eligible(self):
        result = validator.validate_row(synthetic_row(**{"AutoCount MemberNo": "12345"}))

        self.assertTrue(result["valid"])
        self.assertTrue(result["manual_review"])
        self.assertFalse(result["sync_eligible"])
        self.assertEqual(result["normalized"]["member_no"], "12345")
        self.assertEqual(result["normalized"]["member_no_status"], "manual_review")

    def test_error_messages_never_echo_submitted_values(self):
        secret_value = "SECRET-SYNTH-VALUE-42@nowhere"
        result = validator.validate_row(
            synthetic_row(
                **{
                    "AutoCount MemberNo": "",
                    "Email Address": secret_value,
                    "Birthday Month": secret_value,
                    "Marketing Consent": secret_value,
                }
            )
        )

        self.assertFalse(result["valid"])
        dumped = json.dumps(result["errors"])
        self.assertNotIn(secret_value, dumped)


class Ac2ExtractMatchingTests(unittest.TestCase):
    def build_index(self, rows, headers=("MemberNo", "EmailAddress")):
        with tempfile.TemporaryDirectory() as tmp:
            extract_path = Path(tmp) / "synthetic_ac2_extract.csv"
            write_ac2_extract_csv(extract_path, rows, headers)
            return validator.load_ac2_extract_index(extract_path)

    def fake_ac2_index(self):
        return self.build_index(
            [
                {"MemberNo": "6590000001", "EmailAddress": "synthetic.alpha@example.invalid"},
                # Stored in legacy 8-digit form on purpose: matching must
                # canonicalize the AC2 side with the same rules.
                {"MemberNo": "90000002", "EmailAddress": "synthetic.beta@example.invalid"},
            ]
        )

    def decide_for(self, ac2_index, member_no, email):
        result = validator.validate_row(
            synthetic_row(**{"AutoCount MemberNo": member_no, "Email Address": email})
        )
        self.assertTrue(result["valid"])
        return validator.decide(result["normalized"], ac2_index)

    def test_without_extract_decision_is_validation_only(self):
        decision = self.decide_for(None, "9000 0001", "synthetic.alpha@example.invalid")

        self.assertEqual(decision, validator.DECISION_VALIDATION_ONLY)

    def test_member_no_found_in_extract_is_existing_member_review(self):
        decision = self.decide_for(
            self.fake_ac2_index(), "9000 0001", "synthetic.alpha@example.invalid"
        )

        self.assertEqual(decision, validator.DECISION_EXISTING_MEMBER_REVIEW)

    def test_ac2_extract_member_no_is_canonicalized_for_matching(self):
        decision = self.decide_for(
            self.fake_ac2_index(), "6590000002", "synthetic.beta@example.invalid"
        )

        self.assertEqual(decision, validator.DECISION_EXISTING_MEMBER_REVIEW)

    def test_member_no_not_found_is_new_member_candidate(self):
        decision = self.decide_for(
            self.fake_ac2_index(), "9000 0009", "synthetic.gamma@example.invalid"
        )

        self.assertEqual(decision, validator.DECISION_NEW_MEMBER_CANDIDATE)

    def test_email_under_different_member_no_is_possible_conflict_review(self):
        decision = self.decide_for(
            self.fake_ac2_index(), "9000 0009", "synthetic.beta@example.invalid"
        )

        self.assertEqual(decision, validator.DECISION_POSSIBLE_CONFLICT_REVIEW)

    def test_conflict_outranks_member_no_match(self):
        decision = self.decide_for(
            self.fake_ac2_index(), "9000 0001", "synthetic.beta@example.invalid"
        )

        self.assertEqual(decision, validator.DECISION_POSSIBLE_CONFLICT_REVIEW)

    def test_extract_rows_with_unusable_member_no_are_counted_as_skipped(self):
        index = self.build_index(
            [
                {"MemberNo": "6590000001", "EmailAddress": "synthetic.alpha@example.invalid"},
                {"MemberNo": "   ", "EmailAddress": "synthetic.orphan@example.invalid"},
            ]
        )

        self.assertEqual(index["row_count"], 2)
        self.assertEqual(index["skipped_rows"], 1)
        self.assertEqual(index["member_nos"], {"6590000001"})

    def test_extract_without_member_no_column_is_a_contract_error(self):
        with self.assertRaises(validator.FormContractError):
            self.build_index(
                [{"EmailAddress": "synthetic.alpha@example.invalid"}],
                headers=("EmailAddress",),
            )


class RunValidationCountsTests(unittest.TestCase):
    def test_counts_cover_all_decision_flag_and_error_buckets(self):
        rows = [
            synthetic_row(),  # existing member (matches extract member no + own email)
            synthetic_row(
                **{
                    "AutoCount MemberNo": "9000 0009",
                    "Email Address": "synthetic.gamma@example.invalid",
                }
            ),  # new member candidate
            synthetic_row(
                **{
                    "AutoCount MemberNo": "9000 0008",
                    "Email Address": "synthetic.alpha@example.invalid",
                }
            ),  # conflict: email owned by 6590000001
            synthetic_row(
                **{
                    "AutoCount MemberNo": "12345",
                    "Email Address": "synthetic.delta@example.invalid",
                    "PDPA Acknowledged": "No",
                }
            ),  # manual review + pdpa blocked, still valid
            synthetic_row(
                **{
                    "Full Name": "",
                    "Email Address": "broken",
                }
            ),  # invalid with two field errors
        ]
        with tempfile.TemporaryDirectory() as tmp:
            extract_path = Path(tmp) / "synthetic_ac2_extract.csv"
            write_ac2_extract_csv(
                extract_path,
                [{"MemberNo": "6590000001", "EmailAddress": "synthetic.alpha@example.invalid"}],
            )
            ac2_index = validator.load_ac2_extract_index(extract_path)

        results, counts = validator.run_validation(rows, ac2_index)

        self.assertEqual(
            counts,
            {
                "total_rows": 5,
                "valid_rows": 4,
                "invalid_rows": 1,
                "existing_member_review_count": 1,
                "new_member_candidate_count": 2,
                "conflict_review_count": 1,
                "pdpa_blocked_count": 1,
                "manual_review_count": 1,
                "error_count": 2,
            },
        )
        self.assertEqual(results[0]["row_number"], 2)
        self.assertEqual(results[4]["decision"], validator.DECISION_INVALID)

    def test_without_extract_valid_rows_get_validation_only(self):
        results, counts = validator.run_validation([synthetic_row()], None)

        self.assertEqual(results[0]["decision"], validator.DECISION_VALIDATION_ONLY)
        self.assertEqual(counts["existing_member_review_count"], 0)
        self.assertEqual(counts["new_member_candidate_count"], 0)
        self.assertEqual(counts["conflict_review_count"], 0)


class LoadFormRowsTests(unittest.TestCase):
    def test_missing_required_header_is_a_contract_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            form_path = Path(tmp) / "synthetic_form.csv"
            row = synthetic_row()
            row.pop("PDPA Acknowledged")
            headers = [name for name in validator.FORM_HEADERS if name != "PDPA Acknowledged"]
            write_form_csv(form_path, [row], headers=headers)

            with self.assertRaises(validator.FormContractError) as context:
                validator.load_form_rows(form_path)

        self.assertIn("PDPA Acknowledged", str(context.exception))

    def test_utf8_bom_export_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            form_path = Path(tmp) / "synthetic_form.csv"
            write_form_csv(form_path, [synthetic_row()], encoding="utf-8-sig")

            rows = validator.load_form_rows(form_path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Full Name"], "Synthetic Alpha")

    def test_extra_columns_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            form_path = Path(tmp) / "synthetic_form.csv"
            headers = validator.FORM_HEADERS + ["Extra Question"]
            row = synthetic_row()
            row["Extra Question"] = "synthetic extra"
            write_form_csv(form_path, [row], headers=headers)

            rows = validator.load_form_rows(form_path)

        self.assertEqual(len(rows), 1)
        result = validator.validate_row(rows[0])
        self.assertTrue(result["valid"])


class CliDryRunTests(unittest.TestCase):
    def run_cli(self, arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH)] + arguments,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_end_to_end_dry_run_prints_counts_only_and_writes_local_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            form_path = tmp_path / "synthetic_form_responses.csv"
            extract_path = tmp_path / "synthetic_ac2_extract.csv"
            output_dir = tmp_path / "outputs"
            write_form_csv(
                form_path,
                [
                    synthetic_row(),
                    synthetic_row(
                        **{
                            "Full Name": "=SYNTHETIC()",
                            "AutoCount MemberNo": "9000 0009",
                            "Email Address": "synthetic.gamma@example.invalid",
                            "Marketing Consent": "No",
                        }
                    ),
                ],
            )
            write_ac2_extract_csv(
                extract_path,
                [{"MemberNo": "6590000001", "EmailAddress": "synthetic.alpha@example.invalid"}],
            )

            completed = self.run_cli(
                [
                    "--input",
                    str(form_path),
                    "--ac2-extract",
                    str(extract_path),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["mode"], "match")
            self.assertTrue(summary["dry_run_only"])
            self.assertEqual(summary["counts"]["total_rows"], 2)
            self.assertEqual(summary["counts"]["valid_rows"], 2)
            self.assertEqual(summary["counts"]["existing_member_review_count"], 1)
            self.assertEqual(summary["counts"]["new_member_candidate_count"], 1)
            self.assertEqual(summary["ac2_extract_member_count"], 1)

            # Console output must not leak row values: names, emails, or
            # member numbers never appear on stdout.
            self.assertNotIn("synthetic", completed.stdout.lower())
            self.assertNotIn("example.invalid", completed.stdout)
            self.assertNotIn("90000001", completed.stdout)
            self.assertNotIn("6590000001", completed.stdout)

            rows_csv = (output_dir / "member_intake_validation_rows.csv").read_text(encoding="utf-8")
            self.assertIn("6590000001", rows_csv)
            self.assertIn("6590000009", rows_csv)
            # Spreadsheet formula prefixes are neutralized in the review CSV.
            self.assertIn("'=SYNTHETIC()", rows_csv)

            report = (output_dir / "member_intake_validation_report.md").read_text(encoding="utf-8")
            self.assertIn("| total_rows | 2 |", report)
            self.assertNotIn("example.invalid", report)
            self.assertNotIn("6590000001", report)

            manifest = json.loads(
                (output_dir / "member_intake_validation_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["counts"]["total_rows"], 2)
            self.assertTrue(manifest["dry_run_only"])
            self.assertEqual(manifest["autocount_writes"], "none")

            marker = output_dir / "PRIVATE_DO_NOT_COMMIT_MEMBER_INTAKE_VALIDATION.txt"
            self.assertTrue(marker.exists())

    def test_invalid_rows_return_exit_code_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            form_path = Path(tmp) / "synthetic_form_responses.csv"
            write_form_csv(form_path, [synthetic_row(**{"Email Address": "broken"})])

            completed = self.run_cli(["--input", str(form_path)])

            self.assertEqual(completed.returncode, 1, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["counts"]["invalid_rows"], 1)
            self.assertNotIn("broken", completed.stdout)

    def test_header_contract_violation_returns_exit_code_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            form_path = Path(tmp) / "synthetic_form_responses.csv"
            headers = [name for name in validator.FORM_HEADERS if name != "Birthday Month"]
            row = synthetic_row()
            row.pop("Birthday Month")
            write_form_csv(form_path, [row], headers=headers)

            completed = self.run_cli(["--input", str(form_path)])

            self.assertEqual(completed.returncode, 2, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "error")
            self.assertIn("Birthday Month", summary["error"])


class GuardrailStaticTests(unittest.TestCase):
    def test_validator_declares_the_exact_google_form_headers(self):
        self.assertEqual(
            validator.FORM_HEADERS,
            [
                "Timestamp",
                "Full Name",
                "AutoCount MemberNo",
                "Email Address",
                "Birthday Month",
                "Marketing Consent",
                "PDPA Acknowledged",
            ],
        )

    def test_validator_source_has_no_autocount_write_or_sql_or_network_surface(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")

        forbidden_tokens = [
            "SaveMember",
            "DeleteMember",
            "NewMember",
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
            "SELECT *",
            "urllib",
            "requests",
            "socket",
            "subprocess",
        ]
        for token in forbidden_tokens:
            self.assertNotIn(token, source, token)

    def test_validator_source_states_dry_run_and_source_of_truth_boundary(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("never creates, updates, or deletes members", source)
        self.assertIn("source of truth", source)
        self.assertIn("intentionally blank", source)

    def test_no_csv_or_spreadsheet_fixtures_are_committed_under_tests(self):
        fixture_files = [
            path
            for pattern in ("*.csv", "*.xlsx", "*.xls", "*.json")
            for path in (ROOT / "tests").rglob(pattern)
        ]

        self.assertEqual(fixture_files, [])


if __name__ == "__main__":
    unittest.main()
