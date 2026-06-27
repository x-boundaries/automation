import argparse
import json
import re
import sys


DEFAULT_MEMBER_TYPE = "OPEN_MEMBER_TYPE"


def validate_row(row):
    errors = []
    warnings = []
    full_name = normalize_spaces(row.get("FullName"))
    country_code = normalize_country_code(row.get("MobileCountryCode"))
    mobile_number = normalize_mobile_number(row.get("MobileNumber"))
    email = normalize_email(row.get("Email"))
    _, pdpa_error = parse_pdpa_acknowledgement(row.get("PDPAAcknowledged"))
    marketing_allowed, marketing_error = parse_explicit_yes_no(row.get("MarketingConsent"))
    member_type = normalize_spaces(row.get("MemberType"))

    if not full_name:
        errors.append(error("missing_full_name", "FullName is required."))
    if not country_code:
        errors.append(error("missing_mobile_country_code", "MobileCountryCode is required."))
    if not mobile_number:
        errors.append(error("missing_mobile_number", "MobileNumber is required."))
    if email is None and normalize_spaces(row.get("Email")):
        errors.append(error("invalid_email", "Email must be blank or a valid email address."))
    if pdpa_error == "missing":
        errors.append(error("missing_pdpa_acknowledgement", "PDPAAcknowledged must be Yes before dry-run sync."))
    elif pdpa_error == "invalid":
        errors.append(
            error(
                "invalid_pdpa_acknowledgement",
                "PDPAAcknowledged must be normalized to exact Yes or No before dry-run validation.",
            )
        )
    if marketing_error:
        errors.append(error("invalid_marketing_consent", "MarketingConsent must be explicit Yes or No."))
    if not member_type:
        warnings.append(
            error(
                "member_type_unconfirmed",
                "MemberType is missing; payload is dry-run-only until a sandbox-confirmed MemberType is supplied.",
            )
        )

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "payload": None}

    member_no = normalize_spaces(row.get("AutoCountMemberNo") or row.get("MemberNo"))
    payload = {
        "intake_id": normalize_spaces(row.get("IntakeID")),
        "member_no_strategy": "explicit" if member_no else "auto",
        "member_type": member_type or DEFAULT_MEMBER_TYPE,
        "full_name": full_name,
        "mobile": f"+{country_code}{mobile_number}",
        "email": email,
        "birth_date": normalize_spaces(row.get("BirthDate")),
        "country": normalize_spaces(row.get("CountryOfResidence")),
        "signup_source": normalize_spaces(row.get("SignupSource")),
        "sync_eligible": not warnings,
        "dry_run_only": bool(warnings),
        "consent_flags": {
            "pdpa_acknowledged": True,
            "marketing_allowed": marketing_allowed,
        },
    }
    if member_no:
        payload["member_no"] = member_no
    return {"ok": True, "errors": [], "warnings": warnings, "payload": payload}


def normalize_spaces(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalize_country_code(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.lstrip("0")


def normalize_mobile_number(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits.lstrip("0")


def normalize_email(value):
    email = normalize_spaces(value).lower()
    if not email:
        return ""
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return None
    return email


def parse_pdpa_acknowledgement(value):
    normalized = normalize_spaces(value).lower()
    if normalized == "yes":
        return True, None
    if normalized == "no" or not normalized:
        return False, "missing"
    return False, "invalid"


def parse_explicit_yes_no(value):
    normalized = normalize_spaces(value).lower()
    if normalized == "yes":
        return True, False
    if normalized == "no":
        return False, False
    return False, True


def error(code, message):
    return {"code": code, "message": message}


def load_input(path):
    if path == "-":
        return json.loads(sys.stdin.read())
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run validate a normalized member intake row.")
    parser.add_argument("--input", default="-", help="JSON file path, or '-' for stdin.")
    args = parser.parse_args(argv)
    result = validate_row(load_input(args.input))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
