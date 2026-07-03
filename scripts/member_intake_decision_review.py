"""Dry-run decision review for member intake rows.

This local review layer ties validated Google Form rows to sanitized AC2 lookup
results by spreadsheet row number only. It never connects to AutoCount, never
executes live lookup, and writes no row-level personal data.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import member_intake_validate as validator  # noqa: E402


READY_FOR_CREATE_REVIEW = "READY_FOR_CREATE_REVIEW"
EXISTING_MEMBER_REVIEW = "EXISTING_MEMBER_REVIEW"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
PDPA_BLOCKED = "PDPA_BLOCKED"
INVALID_FORM_ROW = "INVALID_FORM_ROW"
LOOKUP_REQUIRED = "LOOKUP_REQUIRED"
LOOKUP_ERROR_REVIEW = "LOOKUP_ERROR_REVIEW"

MODE_OFFLINE = "offline"
MODE_PLANNED_LIVE_LOOKUP = "planned_live_lookup"

LOOKUP_REQUIRED_FIELDS = [
    "row_number",
    "status",
    "member_exists",
    "manual_review_required",
    "warning_count",
]

COUNT_KEYS = [
    "total_rows",
    "ready_for_create_review_count",
    "existing_member_review_count",
    "manual_review_required_count",
    "pdpa_blocked_count",
    "invalid_form_row_count",
    "lookup_required_count",
    "lookup_error_review_count",
]

DECISION_TO_COUNT = {
    READY_FOR_CREATE_REVIEW: "ready_for_create_review_count",
    EXISTING_MEMBER_REVIEW: "existing_member_review_count",
    MANUAL_REVIEW_REQUIRED: "manual_review_required_count",
    PDPA_BLOCKED: "pdpa_blocked_count",
    INVALID_FORM_ROW: "invalid_form_row_count",
    LOOKUP_REQUIRED: "lookup_required_count",
    LOOKUP_ERROR_REVIEW: "lookup_error_review_count",
}

ROWS_CSV_NAME = "member_intake_decision_rows.csv"
REPORT_NAME = "member_intake_decision_report.md"
MANIFEST_NAME = "member_intake_decision_manifest.json"
PRIVATE_MARKER_NAME = "PRIVATE_DO_NOT_COMMIT_MEMBER_INTAKE_DECISION.txt"

ROWS_CSV_COLUMNS = [
    "RowNumber",
    "ValidationStatus",
    "LookupStatus",
    "DecisionCode",
    "IssueCodes",
]


class DecisionReviewError(ValueError):
    """Raised for local input contract failures."""


def load_form_rows(path):
    return validator.load_form_rows(path)


def normalize_lookup_row_number(value):
    try:
        row_number = int(value)
    except (TypeError, ValueError):
        return None
    return row_number if row_number >= 2 else None


def validate_lookup_entry(entry):
    if not isinstance(entry, dict):
        return False
    if any(field not in entry for field in LOOKUP_REQUIRED_FIELDS):
        return False
    if entry.get("status") not in {"ok", "error"}:
        return False
    if not isinstance(entry.get("member_exists"), bool):
        return False
    if not isinstance(entry.get("manual_review_required"), bool):
        return False
    if not isinstance(entry.get("warning_count"), int):
        return False
    return True


def load_lookup_jsonl(path):
    lookup_by_row = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                raise DecisionReviewError(f"Lookup JSONL line {line_number} is not valid JSON.") from error
            row_number = normalize_lookup_row_number(entry.get("row_number") or entry.get("RowNumber"))
            if row_number is None:
                raise DecisionReviewError(f"Lookup JSONL line {line_number} is missing a valid row_number.")
            lookup_by_row[row_number] = entry
    return lookup_by_row


def classify_lookup(entry):
    if entry is None:
        return "missing", ["lookup_required"]
    if not validate_lookup_entry(entry):
        return "schema_error", ["lookup_schema_invalid"]
    if entry["status"] == "error":
        return "error", ["lookup_status_error"]
    if entry["manual_review_required"] or entry["warning_count"] > 0:
        return "manual_review", ["lookup_manual_review_required"]
    if entry["member_exists"]:
        return "found", []
    return "not_found", []


def validation_issue_codes(validation):
    return [entry["code"] for entry in validation["errors"]]


def decide_row(row_number, row, lookup_by_row):
    validation = validator.validate_row(row)
    normalized = validation["normalized"] or {}
    lookup_entry = lookup_by_row.get(row_number) if lookup_by_row is not None else None
    lookup_status, lookup_issues = classify_lookup(lookup_entry)
    issue_codes = validation_issue_codes(validation)

    if not validation["valid"]:
        decision_code = INVALID_FORM_ROW
        lookup_status = "skipped"
    elif validation["pdpa_blocked"]:
        decision_code = PDPA_BLOCKED
        issue_codes.append("pdpa_blocked")
    elif validation["manual_review"]:
        decision_code = MANUAL_REVIEW_REQUIRED
        issue_codes.append("manual_review_member_no")
    elif lookup_status == "missing":
        decision_code = LOOKUP_REQUIRED
        issue_codes.extend(lookup_issues)
    elif lookup_status in {"schema_error", "error"}:
        decision_code = LOOKUP_ERROR_REVIEW
        issue_codes.extend(lookup_issues)
    elif lookup_status == "manual_review":
        decision_code = MANUAL_REVIEW_REQUIRED
        issue_codes.extend(lookup_issues)
    elif lookup_entry["member_exists"]:
        decision_code = EXISTING_MEMBER_REVIEW
    else:
        decision_code = READY_FOR_CREATE_REVIEW

    validation_status = "valid" if validation["valid"] else "invalid"
    if normalized.get("sync_eligible") is False and decision_code not in {INVALID_FORM_ROW, PDPA_BLOCKED}:
        validation_status = "valid_review"

    return {
        "row_number": row_number,
        "validation_status": validation_status,
        "lookup_status": lookup_status,
        "decision_code": decision_code,
        "issue_codes": issue_codes,
    }


def run_decision_review(rows, lookup_by_row=None):
    results = []
    counts = {key: 0 for key in COUNT_KEYS}
    for index, row in enumerate(rows):
        row_number = index + 2
        result = decide_row(row_number, row, lookup_by_row)
        results.append(result)
        counts["total_rows"] += 1
        counts[DECISION_TO_COUNT[result["decision_code"]]] += 1
    return results, counts


def build_rows_csv_row(result):
    return {
        "RowNumber": result["row_number"],
        "ValidationStatus": result["validation_status"],
        "LookupStatus": result["lookup_status"],
        "DecisionCode": result["decision_code"],
        "IssueCodes": ";".join(result["issue_codes"]),
    }


def build_report(results, counts, run_info):
    lines = [
        "# Member Intake Decision Review Report",
        "",
        "Dry-run review layer only: no AutoCount member is created, updated, or deleted.",
        "AC2 is the member source of truth; old POS and side sheet data are reference-only.",
        "Lookup results are sanitized and row-level outputs must not contain PII.",
        "This must not be used as final write automation; live writes remain blocked.",
        "",
        f"- Mode: {run_info['mode']}",
        f"- Run at: {run_info['run_at']}",
        f"- Input file: {run_info['input_name']}",
        f"- Lookup JSONL: {run_info['lookup_jsonl_name'] or 'not supplied'}",
    ]
    if run_info["mode"] == MODE_PLANNED_LIVE_LOOKUP:
        lines.append("- Planned live lookup: this run emits review decisions and does not execute live lookup.")
    lines.extend(["", "## Counts", "", "| Counter | Value |", "| --- | --- |"])
    lines.extend(f"| {key} | {counts[key]} |" for key in COUNT_KEYS)
    lines.extend(["", "## Row Decisions", "", "| Row | Decision code | Issue codes |", "| --- | --- | --- |"])
    for result in results:
        issue_codes = ";".join(result["issue_codes"]) or "-"
        lines.append(f"| {result['row_number']} | {result['decision_code']} | {issue_codes} |")
    lines.append("")
    return "\n".join(lines)


def write_outputs(output_dir, results, counts, run_info):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows_path = output_dir / ROWS_CSV_NAME
    with open(rows_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROWS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(build_rows_csv_row(result) for result in results)

    report_path = output_dir / REPORT_NAME
    report_path.write_text(build_report(results, counts, run_info), encoding="utf-8")

    manifest_path = output_dir / MANIFEST_NAME
    manifest = {
        "status": "ok",
        "mode": run_info["mode"],
        "counts": counts,
        "output_file_names": [ROWS_CSV_NAME, REPORT_NAME, MANIFEST_NAME, PRIVATE_MARKER_NAME],
        "dry_run_only": True,
        "final_write_automation": False,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    marker_path = output_dir / PRIVATE_MARKER_NAME
    marker_path.write_text(
        "PRIVATE - DO NOT COMMIT\n"
        "Decision review outputs are dry-run local review artifacts only.\n"
        "Do not commit, attach to PRs, paste into chat, or screenshot row-level outputs.\n",
        encoding="utf-8",
    )

    return [rows_path, report_path, manifest_path, marker_path]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dry-run member intake decision review using sanitized lookup results."
    )
    parser.add_argument("--input", required=True, help="Google Form response CSV path.")
    parser.add_argument("--lookup-jsonl", default=None, help="Optional sanitized lookup result JSONL path.")
    parser.add_argument("--output-dir", required=True, help="Local output directory for sanitized review artifacts.")
    args = parser.parse_args(argv)

    try:
        rows = load_form_rows(args.input)
        lookup_by_row = load_lookup_jsonl(args.lookup_jsonl) if args.lookup_jsonl else None
    except (validator.FormContractError, DecisionReviewError, OSError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2, sort_keys=True))
        return 2

    mode = MODE_OFFLINE if lookup_by_row is not None else MODE_PLANNED_LIVE_LOOKUP
    results, counts = run_decision_review(rows, lookup_by_row)
    run_info = {
        "mode": mode,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_name": Path(args.input).name,
        "lookup_jsonl_name": Path(args.lookup_jsonl).name if args.lookup_jsonl else None,
    }
    output_files = write_outputs(args.output_dir, results, counts, run_info)
    summary = {
        "status": "ok",
        "mode": mode,
        "counts": counts,
        "output_file_names": [path.name for path in output_files],
        "dry_run_only": True,
        "final_write_automation": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
