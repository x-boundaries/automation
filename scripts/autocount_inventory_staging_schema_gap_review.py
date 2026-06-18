import argparse
import csv
import json
import re
import sys
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_staging_schema_gap_review"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"
LINEAGE_HEADERS = ["source_surface", "source_extract_run_id", "source_row_number"]

EXPECTED_STAGING_HEADERS = OrderedDict(
    [
        ("stg_ac2_supplier", ["supplier_code", "supplier_name", *LINEAGE_HEADERS]),
        (
            "stg_ac2_purchase_order_header",
            [
                "po_doc_key",
                "po_doc_no",
                "po_doc_date",
                "supplier_code",
                "supplier_name",
                "purchase_location",
                "doc_status",
                "cancelled",
                *LINEAGE_HEADERS,
            ],
        ),
        (
            "stg_ac2_purchase_order_line",
            [
                "po_doc_key",
                "po_dtl_key",
                "item_code",
                "description",
                "uom",
                "qty",
                "transferred_qty",
                "location",
                "outstanding_qty_candidate",
                *LINEAGE_HEADERS,
            ],
        ),
        ("stg_ac2_grn_header", ["grn_doc_no", "grn_doc_date", "supplier_code", *LINEAGE_HEADERS]),
        ("stg_ac2_grn_line", ["grn_doc_no", "grn_dtl_key", "item_code", *LINEAGE_HEADERS]),
        ("stg_ac2_stock_receive_header", ["receive_doc_no", "receive_doc_date", *LINEAGE_HEADERS]),
        ("stg_ac2_stock_receive_line", ["receive_doc_no", "item_code", *LINEAGE_HEADERS]),
        (
            "stg_ac2_transfer_header",
            ["transfer_doc_no", "transfer_doc_date", "from_location", "to_location", *LINEAGE_HEADERS],
        ),
        ("stg_ac2_transfer_line", ["transfer_doc_no", "item_code", *LINEAGE_HEADERS]),
        ("stg_ac2_stock_movement_reference", ["doc_type", "item_code", *LINEAGE_HEADERS]),
        ("stg_ac2_item_balance_reference", ["item_code", *LINEAGE_HEADERS]),
    ]
)

OPERATIONAL_MOVEMENT_TABLES = {
    "stg_ac2_grn_header",
    "stg_ac2_grn_line",
    "stg_ac2_stock_receive_header",
    "stg_ac2_stock_receive_line",
    "stg_ac2_transfer_header",
    "stg_ac2_transfer_line",
}
SOURCE_TO_STAGING_TABLE = {
    "dbo.GRDTL": "stg_ac2_grn_line",
    "dbo.vGoodsReceivedNoteDetail": "stg_ac2_grn_line",
    "dbo.vGoodsReceivedNoteSubDetail": "stg_ac2_grn_line",
    "dbo.vStockReceiveDetail": "stg_ac2_stock_receive_line",
    "dbo.vStockTransferDetail": "stg_ac2_transfer_line",
    "dbo.PODTL": "stg_ac2_purchase_order_line",
}
DUPLICATE_SOURCE_SURFACES = {
    "stg_ac2_supplier": ["dbo.vCreditor", "dbo.Creditor"],
    "stg_ac2_purchase_order_header": ["dbo.vPurchaseOrder", "dbo.PO"],
}


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_schema_gap_review(manifest_path, output_root=None, now=None):
    plan = build_run_plan(output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    manifest_path = Path(manifest_path)
    source_manifest = {}
    warning_run_path = manifest_path.parent.resolve(strict=False)
    staging_run_path = warning_run_path
    exceptions = []
    row_counts = {}
    header_evidence = []

    try:
        source_manifest = load_manifest(manifest_path)
        validate_source_safety(source_manifest, exceptions)
        warning_run_path = resolve_manifest_run_path(manifest_path, source_manifest)
        staging_run_path = Path(source_manifest.get("source_staging_run_path") or warning_run_path).resolve(strict=False)
        row_counts = collect_row_counts(source_manifest)
        header_evidence = collect_header_evidence(source_manifest, staging_run_path, row_counts, exceptions)
    except Exception as exc:  # noqa: BLE001 - local review writes redacted failure output.
        exceptions.append(sanitize_text(str(exc)))

    present_headers_by_table = {item["table_name"]: item["present_header_names"] for item in header_evidence}
    expected_headers_by_table = {item["table_name"]: item["expected_header_names"] for item in header_evidence}
    source_schema_gaps = [
        classify_schema_gap(item, expected_headers_by_table, present_headers_by_table)
        for item in source_manifest.get("warning_classifications", [])
        if item.get("classification") == "source_schema_gap"
    ]
    numeric_candidate_reviews = [
        classify_numeric_candidate(item.get("warning_code", ""), present_headers_by_table)
        for item in source_manifest.get("warning_classifications", [])
        if item.get("classification") == "numeric_candidate_not_computable"
    ]
    duplicate_overlap_reviews = [
        review_duplicate_overlap(item.get("warning_code", ""), row_counts, present_headers_by_table)
        for item in source_manifest.get("warning_classifications", [])
        if item.get("classification") == "review_only_duplicate_source_overlap"
    ]
    data_thin_tables = classify_data_thin_tables(source_manifest, row_counts)
    blockers_remain = bool(source_schema_gaps or numeric_candidate_reviews)
    recommendation = "no_dashboard_until_schema_gap_resolved" if blockers_remain else "schema_gap_review_ready"
    status = "failed" if exceptions else "success_with_warnings" if blockers_remain or duplicate_overlap_reviews or data_thin_tables else "success"
    review_conclusion = "schema_gap_review_failed" if exceptions else recommendation

    review_manifest = build_review_manifest(
        source_manifest=source_manifest,
        plan=plan,
        status=status,
        review_conclusion=review_conclusion,
        recommendation=recommendation,
        warning_run_path=warning_run_path,
        staging_run_path=staging_run_path,
        header_evidence=header_evidence,
        row_counts=row_counts,
        source_schema_gaps=source_schema_gaps,
        numeric_candidate_reviews=numeric_candidate_reviews,
        duplicate_overlap_reviews=duplicate_overlap_reviews,
        data_thin_tables=data_thin_tables,
        exceptions=exceptions,
        now=now,
    )
    manifest_out = run_path / "inventory_staging_schema_gap_review_manifest.json"
    report_out = run_path / "inventory_staging_schema_gap_review_report.md"
    review_manifest["storage"]["manifest"] = str(manifest_out)
    review_manifest["storage"]["report"] = str(report_out)
    manifest_out.write_text(json.dumps(normalize_for_json(review_manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_out.write_text(render_report(review_manifest), encoding="utf-8")
    return normalize_for_json(review_manifest)


def validate_source_safety(manifest, exceptions):
    if manifest.get("decision") != NEEDS_RECONCILIATION:
        exceptions.append("unsafe_source_warning_review_decision")
    if manifest.get("business_reconciliation_status") != BUSINESS_RECONCILIATION_STATUS:
        exceptions.append("unsafe_source_warning_review_business_reconciliation_status")
    if manifest.get("data_maturity") != DATA_MATURITY:
        exceptions.append("unsafe_source_warning_review_data_maturity")
    if manifest.get("final_production_selected") is not False:
        exceptions.append("unsafe_source_warning_review_final_production_selected")


def resolve_manifest_run_path(manifest_path, manifest):
    selected = Path(manifest_path).parent.resolve(strict=False)
    declared = manifest.get("storage", {}).get("run_path")
    if declared and Path(declared).resolve(strict=False) != selected:
        raise ValueError("warning_review_manifest_run_path_mismatch")
    return selected


def collect_row_counts(manifest):
    counts = manifest.get("row_counts") if isinstance(manifest.get("row_counts"), dict) else {}
    row_counts = {str(table): int(count or 0) for table, count in counts.items()}
    for table_name in EXPECTED_STAGING_HEADERS:
        row_counts.setdefault(table_name, 0)
    return row_counts


def collect_header_evidence(manifest, staging_run_path, row_counts, exceptions):
    evidence_by_table = {
        item.get("table_name"): item
        for item in manifest.get("header_evidence", [])
        if isinstance(item, dict) and item.get("table_name")
    }
    evidence = []
    for table_name, expected_headers in EXPECTED_STAGING_HEADERS.items():
        source = evidence_by_table.get(table_name, {})
        csv_path = resolve_csv_path(source, table_name, staging_run_path)
        if not _is_relative_to(csv_path, staging_run_path.resolve(strict=False)):
            exceptions.append(f"staging_csv_outside_run:{table_name}")
            present_headers = []
        elif csv_path.exists():
            present_headers = read_csv_headers(csv_path)
        else:
            present_headers = list(source.get("present_header_names") or [])
        evidence.append(
            {
                "table_name": table_name,
                "expected_header_names": list(expected_headers),
                "present_header_names": present_headers,
                "row_count": row_counts.get(table_name, 0),
                "file_name": csv_path.name,
            }
        )
    return evidence


def resolve_csv_path(source, table_name, staging_run_path):
    output_path = source.get("output_path") if isinstance(source, dict) else ""
    if output_path:
        return Path(output_path).resolve(strict=False)
    return (staging_run_path / f"{table_name}.csv").resolve(strict=False)


def read_csv_headers(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle), [])


def classify_schema_gap(warning_item, expected_headers_by_table, present_headers_by_table):
    warning_code = sanitize_text(warning_item.get("warning_code", ""))
    source_surface = warning_code.split(":", 1)[1] if ":" in warning_code else sanitize_text(warning_item.get("source", ""))
    staging_table = SOURCE_TO_STAGING_TABLE.get(source_surface, "unknown")
    expected_headers = list(expected_headers_by_table.get(staging_table, []))
    present_headers = list(present_headers_by_table.get(staging_table, []))
    present_header_set = set(present_headers)
    missing_expected_headers = [header for header in expected_headers if header not in present_header_set]
    return {
        "warning_code": warning_code,
        "source_surface": source_surface,
        "staging_table": staging_table,
        "expected_header_names": expected_headers,
        "present_header_names": present_headers,
        "missing_expected_headers": missing_expected_headers,
        "source_column_gap_detail": (
            "source warning did not include exact source column names; "
            "review uses mapped staging table expected headers as conservative proxy"
        ),
        "classification": "needs_source_column_mapping",
        "decision_flags": [
            "needs_source_column_mapping",
            "needs_alternative_surface",
            "safe_until_data_arrives",
            "dashboard_blocker_when_data_arrives",
        ],
    }


def classify_numeric_candidate(warning_code, headers_by_table):
    warning_code = sanitize_text(warning_code)
    source_surface = warning_code.split(":", 1)[1] if ":" in warning_code else ""
    staging_table = SOURCE_TO_STAGING_TABLE.get(source_surface, "stg_ac2_purchase_order_line")
    candidate_headers = [
        header
        for header in ["qty", "transferred_qty", "outstanding_qty_candidate"]
        if header in headers_by_table.get(staging_table, [])
    ]
    return {
        "warning_code": warning_code,
        "source_surface": source_surface,
        "staging_table": staging_table,
        "candidate_header_names": candidate_headers,
        "classification": "needs_numeric_type_mapping",
        "decision_flags": [
            "needs_numeric_type_mapping",
            "optional_candidate_metric",
            "dashboard_blocker_for_outstanding_po",
        ],
    }


def review_duplicate_overlap(warning_code, row_counts, headers_by_table):
    warning_code = sanitize_text(warning_code)
    staging_table = warning_code.split(":", 1)[1] if ":" in warning_code else ""
    return {
        "warning_code": warning_code,
        "staging_table": staging_table,
        "source_surfaces_reviewed": DUPLICATE_SOURCE_SURFACES.get(staging_table, []),
        "row_count": row_counts.get(staging_table, 0),
        "present_header_names": headers_by_table.get(staging_table, []),
        "classification": "review_only_duplicate_source_overlap",
        "decision_flags": [
            "review_only_duplicate_source_overlap",
            "future_dimension_dedupe_required",
        ],
    }


def classify_data_thin_tables(manifest, row_counts):
    zero_tables = set(str(table) for table in manifest.get("zero_row_tables", []))
    zero_tables.update(table for table in OPERATIONAL_MOVEMENT_TABLES if row_counts.get(table, 0) == 0)
    return [
        {
            "staging_table": table_name,
            "row_count": row_counts.get(table_name, 0),
            "classification": "safe_until_data_arrives",
            "decision_flags": ["source_data_thin", "safe_until_data_arrives"],
        }
        for table_name in sorted(zero_tables)
        if table_name in OPERATIONAL_MOVEMENT_TABLES
    ]


def build_run_plan(output_root=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    resolved_output_root = resolve_output_root(output_root or DEFAULT_OUTPUT_ROOT)
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root / f"inventory_staging_schema_gap_review_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    resolved = Path(output_root).expanduser().resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def build_review_manifest(
    source_manifest,
    plan,
    status,
    review_conclusion,
    recommendation,
    warning_run_path,
    staging_run_path,
    header_evidence,
    row_counts,
    source_schema_gaps,
    numeric_candidate_reviews,
    duplicate_overlap_reviews,
    data_thin_tables,
    exceptions,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    warnings = source_schema_gaps + numeric_candidate_reviews + duplicate_overlap_reviews + data_thin_tables
    return {
        "job": "autocount_inventory_staging_schema_gap_review",
        "status": status,
        "review_conclusion": review_conclusion,
        "recommendation": recommendation,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_warning_review_run_id": source_manifest.get("run_id"),
        "source_warning_review_run_path": str(warning_run_path),
        "source_staging_run_path": str(staging_run_path),
        "source_surfaces_reviewed": sorted(
            {
                item.get("source_surface")
                for item in source_schema_gaps + numeric_candidate_reviews
                if item.get("source_surface")
            }
        ),
        "staging_tables_reviewed": list(EXPECTED_STAGING_HEADERS.keys()),
        "row_counts": row_counts,
        "header_evidence": header_evidence,
        "source_schema_gaps": source_schema_gaps,
        "numeric_candidate_reviews": numeric_candidate_reviews,
        "duplicate_overlap_reviews": duplicate_overlap_reviews,
        "data_thin_tables": data_thin_tables,
        "warning_count": len(warnings),
        "exception_count": len(unique_preserve_order(exceptions)),
        "exceptions": unique_preserve_order([sanitize_text(exception) for exception in exceptions]),
        "generated_files": [
            "inventory_staging_schema_gap_review_manifest.json",
            "inventory_staging_schema_gap_review_report.md",
        ],
        "decision": NEEDS_RECONCILIATION,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": [
            "Manifest-only and local-file-only schema gap and duplicate overlap decision review.",
            "Evidence is limited to source/staging names, header names, row counts, warning codes, and generated filenames.",
            "No dashboards, joins, reconciled dimensions, DB loads, scheduler, or write-back are produced.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Staging Schema Gap Review Report",
        "",
        "This review uses manifests and CSV headers only. It does not query AutoCount or build dashboards.",
        "",
        "## Safe Summary",
        "",
        f"- Status: {manifest['status']}",
        f"- Review conclusion: {manifest['review_conclusion']}",
        f"- Recommendation: {manifest['recommendation']}",
        f"- Decision: {manifest['decision']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        f"- Warning count: {manifest['warning_count']}",
        f"- Exception count: {manifest['exception_count']}",
        "",
        "## Source Schema Gaps",
        "",
    ]
    for item in manifest.get("source_schema_gaps", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- `{item['source_surface']}` -> `{item['staging_table']}`: {', '.join(item['decision_flags'])}")
            lines.append(f"  - Expected headers: {', '.join(item['expected_header_names'])}")
            lines.append(f"  - Present headers: {', '.join(item['present_header_names'])}")
            lines.append(f"  - Computed missing headers: {', '.join(item['missing_expected_headers']) or 'none'}")
            if item.get("source_column_gap_detail"):
                lines.append(f"  - Source column gap detail: {item['source_column_gap_detail']}")
    lines.extend(["", "## Numeric Candidate Reviews", ""])
    for item in manifest.get("numeric_candidate_reviews", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- `{item['source_surface']}` -> `{item['staging_table']}`: {', '.join(item['decision_flags'])}")
            lines.append(f"  - Candidate headers: {', '.join(item['candidate_header_names'])}")
    lines.extend(["", "## Duplicate Source Overlap Warnings", ""])
    for item in manifest.get("duplicate_overlap_reviews", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- `{item['staging_table']}`: {', '.join(item['source_surfaces_reviewed'])}")
            lines.append(f"  - Decision flags: {', '.join(item['decision_flags'])}")
    lines.extend(["", "## Data-Thin Operational Tables", ""])
    for item in manifest.get("data_thin_tables", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- `{item['staging_table']}`: {item['classification']}")
    lines.extend(["", "## Staging Tables Reviewed", ""])
    for table_name in manifest.get("staging_tables_reviewed", []):
        lines.append(f"- `{table_name}`")
    lines.extend(["", "## Generated Files", ""])
    for filename in manifest["generated_files"]:
        lines.append(f"- `{filename}`")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Do not paste raw staging CSV rows.",
            "- Do not build dashboards, purchase recommendations, business KPIs, joins, reconciled dimensions, DB loads, scheduler, or write-back from this review.",
        ]
    )
    return "\n".join(lines) + "\n"


def sanitize_text(text):
    return re.sub(
        r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+",
        lambda match: f"{match.group(1)}=<redacted>",
        str(text),
    )


def normalize_for_json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): normalize_for_json(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_for_json(nested) for nested in value]
    return value


def unique_preserve_order(values):
    seen = set()
    result = []
    for value in values:
        marker = json.dumps(normalize_for_json(value), sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _coerce_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Review local inventory staging schema gaps and duplicate source overlap.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_staging_warning_review_manifest.json.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_schema_gap_review(args.manifest, output_root=args.output_root)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "review_conclusion": manifest["review_conclusion"],
                    "recommendation": manifest["recommendation"],
                    "run_path": manifest["storage"]["run_path"],
                    "warning_count": manifest["warning_count"],
                    "exception_count": manifest["exception_count"],
                    "generated_files": manifest["generated_files"],
                },
                indent=2,
            )
        )
        return 0 if manifest["status"] in {"success", "success_with_warnings"} else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
