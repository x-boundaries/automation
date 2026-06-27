import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import member_intake_validate as validator


class MemberIntakeValidateTests(unittest.TestCase):
    def test_valid_singapore_mobile_builds_canonical_dry_run_payload(self):
        result = validator.validate_row(
            {
                "IntakeID": "INT-001",
                "FullName": "  Jane   Tan  ",
                "MobileCountryCode": "+65",
                "MobileNumber": "9123 4567",
                "Email": " JANE.TAN@EXAMPLE.COM ",
                "BirthDate": "1990-01-02",
                "CountryOfResidence": "Singapore",
                "SignupSource": "Google Form",
                "PDPAAcknowledged": "Yes",
                "MarketingConsent": "Yes",
                "MemberType": "STANDARD",
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            result["payload"],
            {
                "intake_id": "INT-001",
                "member_no_strategy": "auto",
                "member_type": "STANDARD",
                "full_name": "Jane Tan",
                "mobile": "+6591234567",
                "email": "jane.tan@example.com",
                "birth_date": "1990-01-02",
                "country": "Singapore",
                "signup_source": "Google Form",
                "sync_eligible": True,
                "dry_run_only": False,
                "consent_flags": {
                    "pdpa_acknowledged": True,
                    "marketing_allowed": True,
                },
            },
        )

    def test_valid_non_singapore_country_code_is_normalized(self):
        result = validator.validate_row(
            valid_row(
                MobileCountryCode="60",
                MobileNumber="012-345 6789",
                CountryOfResidence="Malaysia",
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["mobile"], "+60123456789")
        self.assertEqual(result["payload"]["country"], "Malaysia")

    def test_missing_name_returns_structured_error(self):
        result = validator.validate_row(valid_row(FullName=" "))

        self.assertFalse(result["ok"])
        self.assertEqual(error_codes(result), ["missing_full_name"])
        self.assertIsNone(result["payload"])

    def test_missing_mobile_returns_structured_error(self):
        result = validator.validate_row(valid_row(MobileNumber=""))

        self.assertFalse(result["ok"])
        self.assertIn("missing_mobile_number", error_codes(result))
        self.assertIsNone(result["payload"])

    def test_missing_pdpa_acknowledgement_returns_structured_error(self):
        result = validator.validate_row(valid_row(PDPAAcknowledged="No"))

        self.assertFalse(result["ok"])
        self.assertEqual(error_codes(result), ["missing_pdpa_acknowledgement"])
        self.assertIsNone(result["payload"])

    def test_yes_no_consent_values_are_case_insensitive(self):
        result = validator.validate_row(valid_row(PDPAAcknowledged="YES", MarketingConsent="no"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertFalse(result["payload"]["consent_flags"]["marketing_allowed"])

        result = validator.validate_row(valid_row(PDPAAcknowledged="yes", MarketingConsent="NO"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertFalse(result["payload"]["consent_flags"]["marketing_allowed"])

    def test_long_checkbox_acknowledgement_text_is_rejected(self):
        result = validator.validate_row(
            valid_row(
                PDPAAcknowledged=(
                    "I acknowledge that X-Boundaries may collect and process my personal data "
                    "for membership administration."
                )
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(error_codes(result), ["invalid_pdpa_acknowledgement"])
        self.assertEqual(
            result["errors"][0]["message"],
            "PDPAAcknowledged must be normalized to exact Yes or No before dry-run validation.",
        )
        self.assertIsNone(result["payload"])

    def test_marketing_consent_no_validates_but_disables_marketing(self):
        result = validator.validate_row(valid_row(MarketingConsent="No"))

        self.assertTrue(result["ok"])
        self.assertFalse(result["payload"]["consent_flags"]["marketing_allowed"])

    def test_missing_member_type_is_dry_run_only_with_warning(self):
        result = validator.validate_row(valid_row(MemberType=""))

        self.assertTrue(result["ok"])
        self.assertEqual(error_codes(result), [])
        self.assertEqual(warning_codes(result), ["member_type_unconfirmed"])
        self.assertEqual(result["payload"]["member_type"], "OPEN_MEMBER_TYPE")
        self.assertFalse(result["payload"]["sync_eligible"])
        self.assertTrue(result["payload"]["dry_run_only"])

    def test_invalid_email_normalization_returns_structured_error(self):
        result = validator.validate_row(valid_row(Email="not an email"))

        self.assertFalse(result["ok"])
        self.assertEqual(error_codes(result), ["invalid_email"])
        self.assertIsNone(result["payload"])

    def test_phone_normalization_strips_local_formatting(self):
        result = validator.validate_row(valid_row(MobileCountryCode="+65", MobileNumber="(9123)-4567"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["mobile"], "+6591234567")

    def test_cli_reads_json_and_prints_canonical_result(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "member_intake_validate.py"),
                "--input",
                "-",
            ],
            input=json.dumps(valid_row()),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertTrue(output["ok"])
        self.assertEqual(output["payload"]["mobile"], "+6591234567")


def valid_row(**overrides):
    row = {
        "IntakeID": "INT-001",
        "FullName": "Jane Tan",
        "MobileCountryCode": "+65",
        "MobileNumber": "9123 4567",
        "Email": "jane.tan@example.com",
        "BirthDate": "1990-01-02",
        "CountryOfResidence": "Singapore",
        "SignupSource": "Google Form",
        "PDPAAcknowledged": "Yes",
        "MarketingConsent": "Yes",
        "MemberType": "STANDARD",
    }
    row.update(overrides)
    return row


def error_codes(result):
    return [error["code"] for error in result["errors"]]


def warning_codes(result):
    return [warning["code"] for warning in result["warnings"]]
