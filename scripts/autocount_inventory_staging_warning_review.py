import argparse
import csv
import json
import re
import sys
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_staging_warning_review"
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
SOURCE_COLUMN_TO_STAGING_FIELD = {
    "stg_ac2_grn_line": {
        "DocNo": ["grn_doc_no"],
        "DtlKey": ["grn_dtl_key"],
        "ItemCode": ["item_code"],
    },
    "stg_ac2_stock_receive_line": {
        "DocNo": ["receive_doc_no"],
        "ItemCode": ["item_code"],
    },
    "stg_ac2_transfer_line": {
        "DocNo": ["transfer_doc_no"],
        "ItemCode": ["item_code"],
    },
    "stg_ac2_purchase_order_line": {
        "DocKey": ["po_doc_key"],
        "DtlKey": ["po_dtl_key"],
        "ItemCode": ["item_code"],
        "Description": ["description"],
        "UOM": ["uom"],
        "Qty": ["qty", "outstanding_qty_candidate"],
        "TransferedQty": ["transferred_qty", "outstanding_qty_candidate"],
        "Location": ["location"],
    },
}


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_warning_review(manifest_path, output_root=None, now=None):
    plan = build_run_plan(output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    manifest_path = Path(manifest_path)
    selected_manifest_run_path = manifest_path.parent.resolve(strict=False)
    source_manifest = {}
    build_manifest = {}
    selected_staging_run_path = selected_manifest_run_path
    warnings = []
    exceptions = []
    carried_forward_warning_codes = []
    header_evidence = []
    row_counts = {}
    zero_row_tables = []
    source_column_diagnostics = []

    try:
        source_manifest = load_manifest(manifest_path)
        validate_source_safety(source_manifest, exceptions)
        selected_manifest_run_path = resolve_manifest_run_path(manifest_path, source_manifest)
        selected_staging_run_path = resolve_staging_run_path(manifest_path, source_manifest)
        build_manifest = load_build_manifest_for_review(manifest_path, source_manifest, selected_staging_run_path, exceptions)
        validate_source_safety(build_manifest, exceptions)

        warnings = collect_warning_codes(source_manifest, build_manifest)
        carried_forward_warning_codes = collect_carried_forward_warning_codes(source_manifest, build_manifest)
        row_counts = collect_row_counts(source_manifest, build_manifest)
        zero_row_tables = collect_zero_row_tables(source_manifest, row_counts)
        header_evidence = collect_header_evidence(build_manifest, selected_staging_run_path, row_counts, exceptions)
        source_column_diagnostics = collect_source_column_diagnostics(build_manifest, selected_staging_run_path, exceptions)
    except Exception as exc:  # noqa: BLE001 - local review should emit a redacted failure manifest.
        exceptions.append(sanitize_text(str(exc)))

    warning_classifications = classify_warning_set(warnings, carried_forward_warning_codes, zero_row_tables)
    dashboard_blockers = [
        item for item in warning_classifications if item.get("dashboard_impact") == "dashboard_blocker_when_data_arrives"
    ]
    explainable = all(item.get("classification") in EXPECTED_CLASSIFICATIONS for item in warning_classifications)
    failed = bool(exceptions) or not explainable
    data_thin = any(item.get("classification") == "source_data_thin" for item in warning_classifications)
    readiness_conclusion = (
        "staging_warning_review_failed"
        if failed
        else "staging_warning_review_ready_data_thin"
        if data_thin
        else "staging_warning_review_ready"
    )
    status = "failed" if failed else "success_with_warnings" if warning_classifications else "success"

    review_manifest = build_review_manifest(
        source_manifest=source_manifest,
        build_manifest=build_manifest,
        plan=plan,
        status=status,
        readiness_conclusion=readiness_conclusion,
        selected_manifest_run_path=selected_manifest_run_path,
        selected_staging_run_path=selected_staging_run_path,
        row_counts=row_counts,
        zero_row_tables=zero_row_tables,
        header_evidence=header_evidence,
        source_column_diagnostics=source_column_diagnostics,
        warning_classifications=warning_classifications,
        dashboard_blockers=dashboard_blockers,
        carried_forward_warning_codes=carried_forward_warning_codes,
        exceptions=exceptions,
        now=now,
    )
    manifest_out = run_path / "inventory_staging_warning_review_manifest.json"
    report_out = run_path / "inventory_staging_warning_review_report.md"
    review_manifest["storage"]["manifest"] = str(manifest_out)
    review_manifest["storage"]["report"] = str(report_out)
    manifest_out.write_text(json.dumps(normalize_for_json(review_manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_out.write_text(render_report(review_manifest), encoding="utf-8")
    return normalize_for_json(review_manifest)


EXPECTED_CLASSIFICATIONS = {
    "source_data_thin",
    "source_schema_gap",
    "numeric_candidate_not_computable",
    "carried_forward_disclaimer",
    "review_only_duplicate_source_overlap",
}


def validate_source_safety(manifest, exceptions):
    if not manifest:
        return
    if manifest.get("decision") != NEEDS_RECONCILIATION:
        exceptions.append(f"unsafe_source_{manifest_label(manifest)}_decision")
    if manifest.get("business_reconciliation_status") != BUSINESS_RECONCILIATION_STATUS:
        exceptions.append(f"unsafe_source_{manifest_label(manifest)}_business_reconciliation_status")
    if manifest.get("data_maturity") != DATA_MATURITY:
        exceptions.append(f"unsafe_source_{manifest_label(manifest)}_data_maturity")
    if manifest.get("final_production_selected") is not False:
        exceptions.append(f"unsafe_source_{manifest_label(manifest)}_final_production_selected")


def manifest_label(manifest):
    job = str(manifest.get("job", "manifest"))
    if job.endswith("_audit"):
        return "audit"
    if job.endswith("_build"):
        return "build"
    return "manifest"


def resolve_manifest_run_path(manifest_path, manifest):
    selected = Path(manifest_path).parent.resolve(strict=False)
    declared = manifest.get("storage", {}).get("run_path")
    if declared:
        declared_path = Path(declared).resolve(strict=False)
        if declared_path != selected:
            raise ValueError(f"{manifest_label(manifest)}_manifest_run_path_mismatch")
    return selected


def resolve_staging_run_path(manifest_path, manifest):
    if manifest.get("job") == "autocount_inventory_staging_build":
        return Path(manifest_path).parent.resolve(strict=False)
    raw = manifest.get("source_staging_run_path")
    if raw:
        return Path(raw).resolve(strict=False)
    return Path(manifest_path).parent.resolve(strict=False)


def load_build_manifest_for_review(manifest_path, source_manifest, staging_run_path, exceptions):
    if source_manifest.get("job") == "autocount_inventory_staging_build":
        return source_manifest
    build_manifest_path = staging_run_path / "inventory_staging_build_manifest.json"
    if not build_manifest_path.exists():
        exceptions.append("source_staging_build_manifest_missing")
        return {}
    build_manifest = load_manifest(build_manifest_path)
    declared = build_manifest.get("storage", {}).get("run_path")
    if declared and Path(declared).resolve(strict=False) != staging_run_path.resolve(strict=False):
        exceptions.append("build_manifest_run_path_mismatch")
    return build_manifest


def collect_warning_codes(source_manifest, build_manifest):
    warnings = []
    for manifest in [build_manifest, source_manifest]:
        for warning in manifest.get("warnings", []) if manifest else []:
            warnings.append(sanitize_text(warning))
    return unique_preserve_order(warnings)


def collect_carried_forward_warning_codes(source_manifest, build_manifest):
    warnings = []
    for manifest in [source_manifest, build_manifest]:
        for warning in manifest.get("carried_forward_warnings", []) if manifest else []:
            warnings.append(sanitize_text(warning))
    return unique_preserve_order(warnings)


def collect_row_counts(source_manifest, build_manifest):
    row_counts = {}
    for manifest in [build_manifest, source_manifest]:
        manifest_counts = manifest.get("row_counts") if manifest else {}
        if isinstance(manifest_counts, dict):
            row_counts.update({str(table): int(count or 0) for table, count in manifest_counts.items()})
    for table_name in EXPECTED_STAGING_HEADERS:
        row_counts.setdefault(table_name, 0)
    return row_counts


def collect_zero_row_tables(source_manifest, row_counts):
    zero_row_tables = [str(table) for table in source_manifest.get("zero_row_tables", []) if source_manifest]
    for table_name in OPERATIONAL_MOVEMENT_TABLES:
        if row_counts.get(table_name, 0) == 0:
            zero_row_tables.append(table_name)
    return sorted(set(zero_row_tables))


def collect_header_evidence(build_manifest, staging_run_path, row_counts, exceptions):
    tables = {table.get("table_name"): table for table in build_manifest.get("staging_tables", [])} if build_manifest else {}
    evidence = []
    for table_name, expected_headers in EXPECTED_STAGING_HEADERS.items():
        table = tables.get(table_name, {})
        csv_path = resolve_staging_csv_path(table, table_name, staging_run_path)
        if not _is_relative_to(csv_path, staging_run_path.resolve(strict=False)):
            exceptions.append(f"staging_csv_outside_run:{table_name}")
            present_headers = []
        elif csv_path.exists():
            present_headers = read_csv_headers(csv_path)
        else:
            present_headers = []
            exceptions.append(f"staging_csv_missing:{table_name}")
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


def resolve_staging_csv_path(table, table_name, staging_run_path):
    raw_path = table.get("output_path") if isinstance(table, dict) else ""
    if raw_path:
        return Path(raw_path).resolve(strict=False)
    return (staging_run_path / f"{table_name}.csv").resolve(strict=False)


def read_csv_headers(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return next(reader, [])


def collect_source_column_diagnostics(build_manifest, staging_run_path, exceptions):
    diagnostics = []
    extract_manifest = load_declared_extract_manifest(build_manifest, staging_run_path)
    for item in extract_manifest.get("schema_metadata", []) + extract_manifest.get("surface_exports", []):
        diagnostic = build_source_column_diagnostic(item)
        if diagnostic:
            diagnostics.append(diagnostic)
    return unique_preserve_order(diagnostics)


def load_declared_extract_manifest(build_manifest, staging_run_path):
    raw_extract_run_path = build_manifest.get("source_extract_run_path") if build_manifest else ""
    if not raw_extract_run_path:
        return {}
    extract_run_path = Path(raw_extract_run_path).resolve(strict=False)
    repo_root = Path(__file__).resolve().parents[1].resolve(strict=False)
    if _is_relative_to(extract_run_path, repo_root):
        return {}
    if _is_relative_to(staging_run_path.resolve(strict=False), extract_run_path):
        return {}
    extract_manifest_path = extract_run_path / "inventory_operation_extract_manifest.json"
    if not extract_manifest_path.exists():
        return {}
    return load_manifest(extract_manifest_path)


def build_source_column_diagnostic(item):
    if not isinstance(item, dict):
        return {}
    source_surface = sanitize_text(
        item.get("source_surface")
        or item.get("object_id")
        or ".".join([part for part in [item.get("schema_name"), item.get("object_name")] if part])
    )
    staging_table = SOURCE_TO_STAGING_TABLE.get(source_surface)
    if not source_surface or not staging_table:
        return {}
    expected = unique_list(item.get("expected_source_columns") or item.get("expected_columns") or [])
    present = unique_list(item.get("present_source_columns") or item.get("selected_columns") or [])
    missing = unique_list(item.get("missing_source_columns") or item.get("missing_expected_columns") or [])
    if not (expected or present or missing):
        return {}
    return {
        "source_surface": source_surface,
        "staging_table": staging_table,
        "expected_source_columns": expected,
        "present_source_columns": present,
        "missing_source_columns": missing,
        "dependent_staging_fields": build_dependent_staging_fields(staging_table, missing),
        "classification": "true_source_column_gap" if missing else "source_column_gap_not_confirmed",
        "dashboard_impact": "dashboard_blocker_when_data_arrives" if missing else "review_only",
    }


def build_dependent_staging_fields(staging_table, source_columns):
    mapping = SOURCE_COLUMN_TO_STAGING_FIELD.get(staging_table, {})
    return {column: mapping.get(column, []) for column in source_columns if mapping.get(column)}


def classify_warning_set(warnings, carried_forward_warning_codes, zero_row_tables):
    classifications = [classify_warning(warning) for warning in warnings]
    for warning in carried_forward_warning_codes:
        classifications.append(
            {
                "warning_code": f"carried_forward_disclaimer:{warning}",
                "classification": "carried_forward_disclaimer",
                "source": warning,
                "dashboard_impact": "review_only",
                "explanation": "Warning was carried forward from an upstream staging build or audit manifest for review continuity.",
            }
        )
    for table_name in zero_row_tables:
        if table_name in OPERATIONAL_MOVEMENT_TABLES:
            classifications.append(
                {
                    "warning_code": f"zero_row_operational_table:{table_name}",
                    "classification": "source_data_thin",
                    "source": table_name,
                    "dashboard_impact": "dashboard_blocker_when_data_arrives",
                    "explanation": "Operational movement staging table has headers but no rows in the current immature data set.",
                }
            )
    return unique_preserve_order(classifications)


def classify_warning(warning_code):
    warning_code = sanitize_text(warning_code)
    if warning_code.startswith("missing_expected_columns:"):
        source = warning_code.split(":", 1)[1]
        return {
            "warning_code": warning_code,
            "classification": "source_schema_gap",
            "source": source,
            "dashboard_impact": "dashboard_blocker_when_data_arrives",
            "explanation": "Expected source columns were absent in the local AC2 metadata evidence, so downstream movement semantics need review before dashboard use.",
        }
    if warning_code.startswith("outstanding_qty_candidate_not_numeric:"):
        source = warning_code.split(":", 1)[1]
        return {
            "warning_code": warning_code,
            "classification": "numeric_candidate_not_computable",
            "source": source,
            "dashboard_impact": "dashboard_blocker_when_data_arrives",
            "explanation": "The outstanding_qty_candidate field could not be computed from the available staged quantity fields.",
        }
    if warning_code.startswith("duplicate_source_surfaces_review:"):
        source = warning_code.split(":", 1)[1]
        return {
            "warning_code": warning_code,
            "classification": "review_only_duplicate_source_overlap",
            "source": source,
            "dashboard_impact": "review_only",
            "explanation": "Multiple source surfaces can feed the same staging table; this is review evidence, not a failure.",
        }
    if warning_code == "operational_movement_rows_data_thin":
        return {
            "warning_code": warning_code,
            "classification": "source_data_thin",
            "source": "operational_movement_tables",
            "dashboard_impact": "dashboard_blocker_when_data_arrives",
            "explanation": "Operational movement tables are currently thin or zero-row in immature pre-go-live data.",
        }
    return {
        "warning_code": warning_code,
        "classification": "carried_forward_disclaimer",
        "source": "upstream_manifest",
        "dashboard_impact": "review_only",
        "explanation": "Warning is preserved for human review but does not add raw-row evidence.",
    }


def build_run_plan(output_root=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    resolved_output_root = resolve_output_root(output_root or DEFAULT_OUTPUT_ROOT)
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root / f"inventory_staging_warning_review_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
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
    build_manifest,
    plan,
    status,
    readiness_conclusion,
    selected_manifest_run_path,
    selected_staging_run_path,
    row_counts,
    zero_row_tables,
    header_evidence,
    source_column_diagnostics,
    warning_classifications,
    dashboard_blockers,
    carried_forward_warning_codes,
    exceptions,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    return {
        "job": "autocount_inventory_staging_warning_review",
        "status": status,
        "readiness_conclusion": readiness_conclusion,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_audit_run_id": source_manifest.get("run_id"),
        "source_staging_build_run_id": build_manifest.get("run_id"),
        "source_manifest_run_path": str(selected_manifest_run_path),
        "source_staging_run_path": str(selected_staging_run_path),
        "row_counts": row_counts,
        "zero_row_tables": zero_row_tables,
        "header_evidence": header_evidence,
        "source_column_diagnostics": source_column_diagnostics,
        "warning_classifications": warning_classifications,
        "dashboard_blockers": dashboard_blockers,
        "carried_forward_warning_codes": carried_forward_warning_codes,
        "warning_count": len(warning_classifications),
        "exception_count": len(unique_preserve_order(exceptions)),
        "exceptions": unique_preserve_order([sanitize_text(exception) for exception in exceptions]),
        "generated_files": [
            "inventory_staging_warning_review_manifest.json",
            "inventory_staging_warning_review_report.md",
        ],
        "decision": NEEDS_RECONCILIATION,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": [
            "Manifest-only and local-file-only staging warning review.",
            "Evidence is limited to table names, header names, row counts, warning codes, and generated filenames.",
            "No raw ERP rows, dashboards, joins, reconciled dimensions, scheduler, DB loading, or write-back are produced.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Staging Warning Review Report",
        "",
        "This review explains staging warnings using manifests and CSV headers only. It does not query AutoCount or build dashboards.",
        "",
        "## Safe Summary",
        "",
        f"- Status: {manifest['status']}",
        f"- Readiness conclusion: {manifest['readiness_conclusion']}",
        f"- Decision: {manifest['decision']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        f"- Warning count: {manifest['warning_count']}",
        f"- Exception count: {manifest['exception_count']}",
        "",
        "## Row Counts",
        "",
    ]
    for table_name in EXPECTED_STAGING_HEADERS:
        lines.append(f"- `{table_name}`: {manifest['row_counts'].get(table_name, 0)}")
    lines.extend(["", "## Warning Classifications", ""])
    for item in manifest.get("warning_classifications", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['warning_code']}`: {item['classification']} ({item['dashboard_impact']})"
            )
    lines.extend(["", "## Header Evidence", ""])
    for item in manifest.get("header_evidence", []):
        lines.append(f"### `{item['table_name']}`")
        lines.append(f"- Row count: {item['row_count']}")
        lines.append(f"- File name: `{item['file_name']}`")
        lines.append(f"- Expected headers: {', '.join(item['expected_header_names'])}")
        lines.append(f"- Present headers: {', '.join(item['present_header_names'])}")
    lines.extend(["", "## Source Column Diagnostics", ""])
    for item in manifest.get("source_column_diagnostics", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['source_surface']}` -> `{item['staging_table']}`: {item['classification']} ({item['dashboard_impact']})"
            )
            lines.append(f"  - Expected source columns: {', '.join(item['expected_source_columns']) or 'unknown'}")
            lines.append(f"  - Present source columns: {', '.join(item['present_source_columns']) or 'unknown'}")
            lines.append(f"  - Missing source columns: {', '.join(item['missing_source_columns']) or 'none'}")
    lines.extend(["", "## Dashboard Blockers When Data Arrives", ""])
    for item in manifest.get("dashboard_blockers", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- `{item['warning_code']}`")
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


def unique_list(values):
    return list(unique_preserve_order([sanitize_text(value) for value in values if str(value).strip()]))


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
    parser = argparse.ArgumentParser(description="Review local inventory staging warning evidence.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_staging_audit_manifest.json or inventory_staging_build_manifest.json.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_warning_review(args.manifest, output_root=args.output_root)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "readiness_conclusion": manifest["readiness_conclusion"],
                    "run_path": manifest["storage"]["run_path"],
                    "row_counts": manifest["row_counts"],
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
