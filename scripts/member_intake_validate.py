"""Dry-run validator/normalizer for Google Form member intake responses.

Reads the member registration Google Form response sheet CSV, normalizes each
row toward the AutoCount 2.0 member shape, optionally dry-run matches rows
against a private local AC2 member extract, and writes local-only review
outputs. AutoCount 2.0 (AC2) is the member source of truth; the old POS and
side records are reference-only evidence. This tool never connects to
AutoCount, never runs SQL, and never creates, updates, or deletes members.

Intentional business mappings (see
docs/autocount2-automation/member_form_intake_contract.md):

- The form field "AutoCount MemberNo" is the member's mobile number and is
  used directly as the AutoCount MemberNo identifier (canonical 65XXXXXXXX
  for Singapore mobiles).
- AutoCount MobilePhone stays intentionally blank. Blank is by design, not
  missing data, because the phone number already serves as MemberNo.
- "Birthday Month" stores the birthday month only, mapped to DOB 2000-MM-01.
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from csv_safety import safe_csv_row  # noqa: E402


FORM_HEADERS = [
    "Timestamp",
    "Full Name",
    "AutoCount MemberNo",
    "Email Address",
    "Birthday Month",
    "Marketing Consent",
    "PDPA Acknowledged",
]

AC2_EXTRACT_MEMBER_NO_COLUMN = "MemberNo"
AC2_EXTRACT_EMAIL_COLUMN = "EmailAddress"

# Confirmed by the PR #69 no-save schema probe: AutoCount MemberNo is a
# non-null string with max_length 20.
MEMBER_NO_MAX_LENGTH = 20

# AC2 DOB stores the birthday month only, using a fixed sentinel year/day.
DOB_YEAR = 2000

# The live Google Form exports the PDPA checkbox as "I agree". "Yes" stays
# accepted for the earlier multiple-choice form design.
PDPA_ACKNOWLEDGED_VALUES = {"i agree", "yes"}

MONTH_NUMBERS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

DECISION_VALIDATION_ONLY = "VALIDATION_ONLY"
DECISION_NEW_MEMBER_CANDIDATE = "NEW_MEMBER_CANDIDATE"
DECISION_EXISTING_MEMBER_REVIEW = "EXISTING_MEMBER_REVIEW"
DECISION_POSSIBLE_CONFLICT_REVIEW = "POSSIBLE_CONFLICT_REVIEW"
DECISION_INVALID = "INVALID"

COUNTER_ORDER = [
    "total_rows",
    "valid_rows",
    "invalid_rows",
    "existing_member_review_count",
    "new_member_candidate_count",
    "conflict_review_count",
    "pdpa_blocked_count",
    "manual_review_count",
    "error_count",
]

ROWS_CSV_NAME = "member_intake_validation_rows.csv"
REPORT_NAME = "member_intake_validation_report.md"
MANIFEST_NAME = "member_intake_validation_manifest.json"
PRIVATE_MARKER_NAME = "PRIVATE_DO_NOT_COMMIT_MEMBER_INTAKE_VALIDATION.txt"

ROWS_CSV_COLUMNS = [
    "RowNumber",
    "Decision",
    "Valid",
    "SyncEligible",
    "ManualReview",
    "PdpaBlocked",
    "IssueCodes",
    "MemberNo",
    "MemberNoStatus",
    "Name",
    "EmailAddress",
    "DOB",
    "BirthdayMonth",
    "MobilePhone",
    "PdpaAcknowledged",
    "MarketingAllowed",
    "SubmittedAt",
]

MEMBER_NO_ERROR_MESSAGES = {
    "missing_member_no": "AutoCount MemberNo is required.",
    "member_no_empty_after_cleaning": "AutoCount MemberNo contains no usable characters after cleaning.",
    "member_no_too_long": f"AutoCount MemberNo exceeds {MEMBER_NO_MAX_LENGTH} characters after cleaning.",
}


class FormContractError(ValueError):
    """Raised when an input file does not match the expected contract."""


def normalize_spaces(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalize_member_no(raw):
    """Normalize a submitted AutoCount MemberNo (the member's mobile number).

    Returns {"value", "status", "error_code"} where status is one of
    "canonical", "manual_review", or "error". The returned dict never echoes
    the raw submitted value beyond the cleaned identifier itself.
    """
    trimmed = normalize_spaces(raw)
    cleaned = re.sub(r"[^0-9A-Za-z]+", "", trimmed)
    if not cleaned:
        code = "missing_member_no" if not trimmed else "member_no_empty_after_cleaning"
        return {"value": "", "status": "error", "error_code": code}
    if len(cleaned) > MEMBER_NO_MAX_LENGTH:
        return {"value": cleaned, "status": "error", "error_code": "member_no_too_long"}
    if not cleaned.isdigit():
        return {"value": cleaned, "status": "manual_review", "error_code": None}
    if len(cleaned) == 8 and cleaned[0] in "89":
        return {"value": f"65{cleaned}", "status": "canonical", "error_code": None}
    if len(cleaned) == 10 and cleaned.startswith("65"):
        return {"value": cleaned, "status": "canonical", "error_code": None}
    return {"value": cleaned, "status": "manual_review", "error_code": None}


def normalize_email(raw):
    """Return a trimmed lowercase email, "" when blank, or None when invalid."""
    email = normalize_spaces(raw).lower()
    if not email:
        return ""
    if not EMAIL_PATTERN.fullmatch(email):
        return None
    return email


def normalize_birthday_month(raw):
    """Map a full month name to (DOB "2000-MM-01", canonical month name)."""
    month_key = normalize_spaces(raw).lower()
    month_number = MONTH_NUMBERS.get(month_key)
    if month_number is None:
        return None, None
    return f"{DOB_YEAR}-{month_number:02d}-01", month_key.capitalize()


def parse_pdpa_acknowledgement(raw):
    """Return True only for an accepted acknowledgement value."""
    return normalize_spaces(raw).lower() in PDPA_ACKNOWLEDGED_VALUES


def parse_marketing_consent(raw):
    """Return True for Yes, False for No, None for anything else."""
    normalized = normalize_spaces(raw).lower()
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    return None


def issue(code, field, message):
    # Issue entries stay PII-safe: codes, field names, and fixed messages only.
    return {"code": code, "field": field, "message": message}


def validate_row(row):
    """Validate and normalize one Google Form response row (dry-run only).

    PDPA not acknowledged is a compliance gate, not a data-format error: the
    row stays structurally valid but is flagged pdpa_blocked and can never be
    sync eligible. Marketing Consent = No never blocks registration.
    """
    errors = []

    member_no = normalize_member_no(row.get("AutoCount MemberNo"))
    if member_no["status"] == "error":
        code = member_no["error_code"]
        errors.append(issue(code, "AutoCount MemberNo", MEMBER_NO_ERROR_MESSAGES[code]))

    full_name = normalize_spaces(row.get("Full Name"))
    if not full_name:
        errors.append(issue("missing_full_name", "Full Name", "Full Name is required."))

    email = normalize_email(row.get("Email Address"))
    if email == "":
        errors.append(issue("missing_email", "Email Address", "Email Address is required."))
    elif email is None:
        errors.append(issue("invalid_email", "Email Address", "Email Address must be a valid email address."))

    dob, month_name = normalize_birthday_month(row.get("Birthday Month"))
    if month_name is None:
        code = "missing_birthday_month" if not normalize_spaces(row.get("Birthday Month")) else "invalid_birthday_month"
        errors.append(issue(code, "Birthday Month", "Birthday Month must be a full month name from January to December."))

    marketing_allowed = parse_marketing_consent(row.get("Marketing Consent"))
    if marketing_allowed is None:
        code = "missing_marketing_consent" if not normalize_spaces(row.get("Marketing Consent")) else "invalid_marketing_consent"
        errors.append(issue(code, "Marketing Consent", "Marketing Consent must be explicit Yes or No."))

    pdpa_acknowledged = parse_pdpa_acknowledgement(row.get("PDPA Acknowledged"))

    valid = not errors
    manual_review = member_no["status"] == "manual_review"
    pdpa_blocked = not pdpa_acknowledged
    sync_eligible = valid and pdpa_acknowledged and not manual_review

    normalized = None
    if valid:
        normalized = {
            "submitted_at": normalize_spaces(row.get("Timestamp")),
            "member_no": member_no["value"],
            "member_no_status": member_no["status"],
            "name": full_name,
            "email_address": email,
            "dob": dob,
            "birthday_month": month_name,
            # AutoCount MobilePhone is intentionally blank: the phone number
            # already serves as MemberNo. Blank is by design, not missing data.
            "mobile_phone": "",
            "pdpa_acknowledged": pdpa_acknowledged,
            "marketing_allowed": marketing_allowed,
            "sync_eligible": sync_eligible,
        }

    return {
        "valid": valid,
        "errors": errors,
        "manual_review": manual_review,
        "pdpa_blocked": pdpa_blocked,
        "sync_eligible": sync_eligible,
        "normalized": normalized,
    }


def decide(normalized, ac2_index):
    """Assign a dry-run decision for one valid normalized row.

    An email owned by a different AC2 MemberNo outranks a MemberNo match,
    because it signals a possible duplicate or mis-typed identifier that a
    human must resolve against AC2, the source of truth.
    """
    if ac2_index is None:
        return DECISION_VALIDATION_ONLY
    member_no = normalized["member_no"]
    email_owner_nos = ac2_index["emails"].get(normalized["email_address"], set())
    if email_owner_nos - {member_no}:
        return DECISION_POSSIBLE_CONFLICT_REVIEW
    if member_no in ac2_index["member_nos"]:
        return DECISION_EXISTING_MEMBER_REVIEW
    return DECISION_NEW_MEMBER_CANDIDATE


def run_validation(rows, ac2_index=None):
    """Validate all rows and aggregate counters.

    error_count totals individual field-level errors, so it can exceed
    invalid_rows when a single row has several problems.
    """
    results = []
    counts = {key: 0 for key in COUNTER_ORDER}
    decision_counters = {
        DECISION_EXISTING_MEMBER_REVIEW: "existing_member_review_count",
        DECISION_NEW_MEMBER_CANDIDATE: "new_member_candidate_count",
        DECISION_POSSIBLE_CONFLICT_REVIEW: "conflict_review_count",
    }
    for index, row in enumerate(rows):
        result = validate_row(row)
        # Spreadsheet-style numbering: the header is row 1, so the first
        # response row is row 2, matching the Google Sheet the operator sees.
        result["row_number"] = index + 2
        result["decision"] = decide(result["normalized"], ac2_index) if result["valid"] else DECISION_INVALID

        counts["total_rows"] += 1
        counts["valid_rows" if result["valid"] else "invalid_rows"] += 1
        counts["error_count"] += len(result["errors"])
        if result["pdpa_blocked"]:
            counts["pdpa_blocked_count"] += 1
        if result["manual_review"]:
            counts["manual_review_count"] += 1
        decision_counter = decision_counters.get(result["decision"])
        if decision_counter:
            counts[decision_counter] += 1
        results.append(result)
    return results, counts


def load_form_rows(path):
    """Load the Google Form response sheet CSV and enforce the header contract."""
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = [normalize_spaces(name) for name in (reader.fieldnames or [])]
        missing = [name for name in FORM_HEADERS if name not in headers]
        if missing:
            raise FormContractError(
                "Input CSV is missing required Google Form headers: " + ", ".join(missing)
            )
        rows = []
        for row in reader:
            rows.append({normalize_spaces(key): value for key, value in row.items() if key is not None})
        return rows


def load_ac2_extract_index(path):
    """Index a private local AC2 member extract for dry-run matching.

    Expects the ac2_member_browse_extract.csv shape with at least a MemberNo
    column; EmailAddress is used when present. AC2 MemberNo values are
    canonicalized with the same rules as submitted rows so formatting
    differences cannot defeat matching. The extract is read-only reference
    data and must never be committed.
    """
    member_nos = set()
    emails = {}
    row_count = 0
    skipped_rows = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = [normalize_spaces(name) for name in (reader.fieldnames or [])]
        if AC2_EXTRACT_MEMBER_NO_COLUMN not in headers:
            raise FormContractError(
                f"AC2 extract is missing the {AC2_EXTRACT_MEMBER_NO_COLUMN} column."
            )
        has_email = AC2_EXTRACT_EMAIL_COLUMN in headers
        for row in reader:
            row_count += 1
            normalized_row = {normalize_spaces(key): value for key, value in row.items() if key is not None}
            member_no = normalize_member_no(normalized_row.get(AC2_EXTRACT_MEMBER_NO_COLUMN))
            if member_no["status"] == "error":
                skipped_rows += 1
                continue
            member_nos.add(member_no["value"])
            if has_email:
                email = normalize_spaces(normalized_row.get(AC2_EXTRACT_EMAIL_COLUMN)).lower()
                if email:
                    emails.setdefault(email, set()).add(member_no["value"])
    return {
        "member_nos": member_nos,
        "emails": emails,
        "row_count": row_count,
        "skipped_rows": skipped_rows,
    }


def build_rows_csv_row(result):
    """Flatten one validation result for the local review CSV.

    Invalid rows keep row number, decision, and issue codes only, so bad raw
    values are not copied forward; the operator fixes them in the sheet.
    """
    normalized = result["normalized"] or {}
    return {
        "RowNumber": result["row_number"],
        "Decision": result["decision"],
        "Valid": result["valid"],
        "SyncEligible": result["sync_eligible"],
        "ManualReview": result["manual_review"],
        "PdpaBlocked": result["pdpa_blocked"],
        "IssueCodes": ";".join(entry["code"] for entry in result["errors"]),
        "MemberNo": normalized.get("member_no", ""),
        "MemberNoStatus": normalized.get("member_no_status", ""),
        "Name": normalized.get("name", ""),
        "EmailAddress": normalized.get("email_address", ""),
        "DOB": normalized.get("dob", ""),
        "BirthdayMonth": normalized.get("birthday_month", ""),
        "MobilePhone": normalized.get("mobile_phone", ""),
        "PdpaAcknowledged": normalized.get("pdpa_acknowledged", ""),
        "MarketingAllowed": normalized.get("marketing_allowed", ""),
        "SubmittedAt": normalized.get("submitted_at", ""),
    }


def build_report(counts, results, run_info):
    """Build the pasteback-safe markdown report: counters and issue codes only."""
    lines = [
        "# Member Intake Dry-Run Validation Report",
        "",
        "Dry-run only: no AutoCount member was created, updated, or deleted.",
        "AC2 is the member source of truth; this report is review evidence only.",
        "",
        f"- Mode: {run_info['mode']}",
        f"- Run at: {run_info['run_at']}",
        f"- Input file: {run_info['input_name']}",
        f"- AC2 extract: {run_info['ac2_extract_name'] or 'not provided'}",
        "",
        "## Counts",
        "",
        "| Counter | Value |",
        "| --- | --- |",
    ]
    lines.extend(f"| {key} | {counts[key]} |" for key in COUNTER_ORDER)
    lines.extend(["", "## Rows Needing Attention", ""])
    attention = [
        result
        for result in results
        if result["errors"]
        or result["manual_review"]
        or result["pdpa_blocked"]
        or result["decision"] in (DECISION_EXISTING_MEMBER_REVIEW, DECISION_POSSIBLE_CONFLICT_REVIEW)
    ]
    if attention:
        lines.extend(["| Row | Decision | Issue codes | Flags |", "| --- | --- | --- | --- |"])
        for result in attention:
            flags = [
                name
                for name, flagged in (
                    ("manual_review", result["manual_review"]),
                    ("pdpa_blocked", result["pdpa_blocked"]),
                )
                if flagged
            ]
            issue_codes = ";".join(entry["code"] for entry in result["errors"])
            lines.append(
                f"| {result['row_number']} | {result['decision']} | {issue_codes or '-'} | {';'.join(flags) or '-'} |"
            )
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def write_outputs(output_dir, results, counts, run_info):
    """Write local-only review outputs into the user-supplied directory."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows_path = output_dir / ROWS_CSV_NAME
    with open(rows_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROWS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(
            safe_csv_row(build_rows_csv_row(result), ROWS_CSV_COLUMNS) for result in results
        )

    report_path = output_dir / REPORT_NAME
    report_path.write_text(build_report(counts, results, run_info), encoding="utf-8")

    manifest_path = output_dir / MANIFEST_NAME
    manifest = {
        "counts": counts,
        "mode": run_info["mode"],
        "run_at": run_info["run_at"],
        "input_csv": run_info["input_name"],
        "ac2_extract": run_info["ac2_extract_name"],
        "ac2_extract_member_count": run_info["ac2_extract_member_count"],
        "ac2_extract_skipped_rows": run_info["ac2_extract_skipped_rows"],
        "output_files": [ROWS_CSV_NAME, REPORT_NAME, MANIFEST_NAME, PRIVATE_MARKER_NAME],
        "dry_run_only": True,
        "autocount_writes": "none",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    marker_path = output_dir / PRIVATE_MARKER_NAME
    marker_path.write_text(
        "This folder contains member intake validation output with personal data.\n"
        "Keep it local or in approved secure storage only.\n"
        "Do not commit, attach to PRs, paste into chat, or screenshot raw rows.\n",
        encoding="utf-8",
    )
    return [rows_path, report_path, manifest_path, marker_path]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run validate/normalize Google Form member intake CSV rows. "
            "Never creates, updates, or deletes AutoCount members."
        )
    )
    parser.add_argument("--input", required=True, help="Google Form response sheet CSV export path.")
    parser.add_argument(
        "--ac2-extract",
        default=None,
        help=(
            "Optional private local AC2 member extract CSV (MemberNo/EmailAddress columns) "
            "for dry-run matching. Keep this file outside the repository; never commit it."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Local directory for detailed review outputs. The row-level CSV contains "
            "personal data and must stay local."
        ),
    )
    args = parser.parse_args(argv)

    try:
        rows = load_form_rows(args.input)
        ac2_index = load_ac2_extract_index(args.ac2_extract) if args.ac2_extract else None
    except (FormContractError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2, sort_keys=True))
        return 2

    results, counts = run_validation(rows, ac2_index)
    run_info = {
        "mode": "match" if ac2_index is not None else "validation_only",
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_name": Path(args.input).name,
        "ac2_extract_name": Path(args.ac2_extract).name if args.ac2_extract else None,
        "ac2_extract_member_count": len(ac2_index["member_nos"]) if ac2_index else None,
        "ac2_extract_skipped_rows": ac2_index["skipped_rows"] if ac2_index else None,
    }

    output_files = []
    if args.output_dir:
        output_files = write_outputs(args.output_dir, results, counts, run_info)

    # Console output stays counts-only; raw or normalized rows never print here.
    summary = {
        "status": "ok",
        "mode": run_info["mode"],
        "dry_run_only": True,
        "counts": counts,
        "output_files": [path.name for path in output_files],
    }
    if ac2_index is not None:
        summary["ac2_extract_member_count"] = run_info["ac2_extract_member_count"]
        summary["ac2_extract_skipped_rows"] = run_info["ac2_extract_skipped_rows"]
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if counts["invalid_rows"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
