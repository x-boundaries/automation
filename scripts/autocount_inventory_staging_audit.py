import argparse
import csv
import json
import re
import sys
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_CONFIG_PATH = Path("config/autocount_inventory_staging_audit.example.json")
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_staging_audit"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"
LINEAGE_HEADERS = ["source_surface", "source_extract_run_id", "source_row_number"]

EXPECTED_STAGING_TABLES = OrderedDict(
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
OVERLAP_REVIEW_TABLES = {"stg_ac2_supplier", "stg_ac2_purchase_order_header"}


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Staging build manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_staging_audit(manifest_path, output_root=None, config=None, now=None):
    plan = build_run_plan(output_root or (config or {}).get("output_root"), now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    manifest_path = Path(manifest_path)
    selected_staging_run_path = manifest_path.parent.resolve(strict=False)
    source_manifest = {}
    staging_tables = []
    row_counts = {}
    zero_row_tables = []
    missing_files = []
    missing_headers = []
    warnings = []
    exceptions = []
    carried_forward_warnings = []

    try:
        source_manifest = load_manifest(manifest_path)
        validate_staging_manifest_safety(source_manifest, exceptions)
        resolve_selected_staging_run_path(manifest_path, source_manifest)
        carried_forward_warnings = carry_forward_warnings(source_manifest)
        warnings.extend(carried_forward_warnings)
        tables_by_name = {table.get("table_name"): table for table in source_manifest.get("staging_tables", [])}

        for table_name, expected_headers in EXPECTED_STAGING_TABLES.items():
            table = tables_by_name.get(table_name)
            if not table:
                missing_files.append(table_name)
                exceptions.append(f"missing_staging_table_manifest_entry:{table_name}")
                staging_tables.append(audit_table_summary(table_name, 0, "", "missing_manifest_entry"))
                continue

            csv_path, path_error = resolve_staging_csv(table, selected_staging_run_path)
            if path_error:
                exceptions.append(path_error)
                staging_tables.append(audit_table_summary(table_name, 0, "", "path_refused"))
                continue
            if not csv_path.exists():
                missing_files.append(table_name)
                staging_tables.append(audit_table_summary(table_name, 0, csv_path.name, "missing_file"))
                continue

            headers, source_surfaces, row_count = inspect_csv_contract(csv_path)
            row_counts[table_name] = row_count
            if row_count == 0:
                zero_row_tables.append(table_name)
            for header in expected_headers:
                if header not in headers:
                    missing_headers.append({"table_name": table_name, "header": header})
            if table_name in OVERLAP_REVIEW_TABLES and len(source_surfaces) > 1:
                warnings.append(f"duplicate_source_surfaces_review:{table_name}")
            staging_tables.append(audit_table_summary(table_name, row_count, csv_path.name, "audited"))
    except Exception as exc:  # noqa: BLE001 - write redacted failure output for local review.
        exceptions.append(sanitize_text(str(exc)))

    for table_name in EXPECTED_STAGING_TABLES:
        row_counts.setdefault(table_name, 0)

    failed = bool(exceptions or missing_files or missing_headers)
    data_thin = any(row_counts.get(table_name, 0) == 0 for table_name in OPERATIONAL_MOVEMENT_TABLES)
    audit_decision = "staging_contract_failed" if failed else "staging_contract_ready_data_thin" if data_thin else "staging_contract_ready"
    if data_thin and not failed:
        warnings.append("operational_movement_rows_data_thin")
    status = "failed" if failed else "success_with_warnings" if warnings or zero_row_tables else "success"

    audit_manifest = build_audit_manifest(
        source_manifest,
        plan,
        status,
        audit_decision,
        staging_tables,
        row_counts,
        zero_row_tables,
        missing_files,
        missing_headers,
        warnings,
        exceptions,
        carried_forward_warnings,
        selected_staging_run_path,
        now=now,
    )
    manifest_out = run_path / "inventory_staging_audit_manifest.json"
    report_out = run_path / "inventory_staging_audit_report.md"
    audit_manifest["storage"]["manifest"] = str(manifest_out)
    audit_manifest["storage"]["report"] = str(report_out)
    manifest_out.write_text(json.dumps(normalize_for_json(audit_manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_out.write_text(render_report(audit_manifest), encoding="utf-8")
    return normalize_for_json(audit_manifest)


def validate_staging_manifest_safety(manifest, exceptions):
    if manifest.get("decision") != NEEDS_RECONCILIATION:
        exceptions.append("unsafe_manifest_decision")
    if manifest.get("business_reconciliation_status") != BUSINESS_RECONCILIATION_STATUS:
        exceptions.append("unsafe_manifest_business_reconciliation_status")
    if manifest.get("data_maturity") != DATA_MATURITY:
        exceptions.append("unsafe_manifest_data_maturity")
    if manifest.get("final_production_selected") is not False:
        exceptions.append("unsafe_manifest_final_production_selected")


def resolve_selected_staging_run_path(manifest_path, manifest):
    selected_staging_run_path = Path(manifest_path).parent.resolve(strict=False)
    declared_run_path = manifest.get("storage", {}).get("run_path")
    if declared_run_path:
        declared = Path(declared_run_path).resolve(strict=False)
        if declared != selected_staging_run_path:
            raise ValueError(
                "staging_manifest_run_path_mismatch:"
                f"declared={sanitize_text(declared)};"
                f"selected={sanitize_text(selected_staging_run_path)}"
            )
    return selected_staging_run_path


def carry_forward_warnings(manifest):
    carried_prefixes = ("missing_expected_columns:", "outstanding_qty_candidate_not_numeric:")
    return [
        sanitize_text(warning)
        for warning in manifest.get("warnings", [])
        if str(warning).startswith(carried_prefixes)
    ]


def resolve_staging_csv(table, staging_run_path):
    path = Path(table.get("output_path") or "")
    resolved = path.resolve(strict=False)
    if not _is_relative_to(resolved, staging_run_path.resolve(strict=False)):
        return resolved, f"staging_csv_outside_run:{table.get('table_name')}"
    return resolved, None


def inspect_csv_contract(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        source_surfaces = set()
        row_count = 0
        for row in reader:
            row_count += 1
            if row.get("source_surface"):
                source_surfaces.add(row["source_surface"])
    return headers, source_surfaces, row_count


def audit_table_summary(table_name, row_count, file_name, status):
    return {"table_name": table_name, "row_count": row_count, "file_name": file_name, "status": status}


def build_run_plan(output_root=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    resolved_output_root = resolve_output_root(output_root or DEFAULT_OUTPUT_ROOT)
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "output_root": str(resolved_output_root),
        "run_path": str(resolved_output_root / f"inventory_staging_audit_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"),
    }


def resolve_output_root(output_root, repo_root=None):
    resolved = Path(output_root).expanduser().resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def build_audit_manifest(
    source_manifest,
    plan,
    status,
    audit_decision,
    staging_tables,
    row_counts,
    zero_row_tables,
    missing_files,
    missing_headers,
    warnings,
    exceptions,
    carried_forward_warnings,
    selected_staging_run_path,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    return {
        "job": "autocount_inventory_staging_audit",
        "status": status,
        "audit_decision": audit_decision,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_staging_build_run_id": source_manifest.get("run_id"),
        "source_staging_run_path": str(selected_staging_run_path),
        "staging_tables": staging_tables,
        "row_counts": row_counts,
        "zero_row_tables": zero_row_tables,
        "missing_files": missing_files,
        "missing_headers": missing_headers,
        "carried_forward_warnings": unique_preserve_order(carried_forward_warnings),
        "warnings": unique_preserve_order([sanitize_text(warning) for warning in warnings]),
        "warning_count": len(unique_preserve_order(warnings)),
        "exception_count": len(unique_preserve_order(exceptions)),
        "exceptions": unique_preserve_order([sanitize_text(exception) for exception in exceptions]),
        "decision": NEEDS_RECONCILIATION,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": [
            "Manifest-only and local-file-only staging contract audit.",
            "Zero-row operational movement tables are classified as data_thin, not a dashboard signal.",
            "No dashboard metrics, purchase recommendations, joins, reconciled dimensions, DB loads, scheduler, or write-back are produced.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Staging Audit Report",
        "",
        "This audit validates the local staging contract only. It does not query AutoCount, load a database, or produce dashboards.",
        "",
        "## Safe Summary",
        "",
        f"- Status: {manifest['status']}",
        f"- Audit decision: {manifest['audit_decision']}",
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
    for table_name in EXPECTED_STAGING_TABLES:
        lines.append(f"- `{table_name}`: {manifest['row_counts'].get(table_name, 0)}")
    lines.extend(["", "## Zero-Row Tables", ""])
    for table_name in manifest.get("zero_row_tables", []) or ["none"]:
        lines.append(f"- `{table_name}`" if table_name != "none" else "- none")
    lines.extend(["", "## Missing Files", ""])
    for table_name in manifest.get("missing_files", []) or ["none"]:
        lines.append(f"- `{table_name}`" if table_name != "none" else "- none")
    lines.extend(["", "## Missing Headers", ""])
    for item in manifest.get("missing_headers", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- `{item['table_name']}` missing `{item['header']}`")
    lines.extend(["", "## Warnings", ""])
    for warning in manifest.get("warnings", []) or ["none"]:
        lines.append(f"- {warning}")
    lines.extend(["", "## Generated Files", ""])
    for filename in ["inventory_staging_audit_manifest.json", "inventory_staging_audit_report.md"]:
        lines.append(f"- `{filename}`")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Do not paste raw staging CSV rows.",
            "- Do not build dashboards, purchase recommendations, business KPIs, joins, reconciled dimensions, DB loads, scheduler, or write-back from this audit.",
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
    parser = argparse.ArgumentParser(description="Audit local inventory staging build contract.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_staging_build_manifest.json.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free staging audit config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_config(args.config) if args.config else {}
        manifest = run_staging_audit(args.manifest, output_root=args.output_root, config=config)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "audit_decision": manifest["audit_decision"],
                    "run_path": manifest["storage"]["run_path"],
                    "row_counts": manifest["row_counts"],
                    "missing_files": manifest["missing_files"],
                    "missing_headers": manifest["missing_headers"],
                    "warning_count": manifest["warning_count"],
                    "exception_count": manifest["exception_count"],
                    "generated_files": ["inventory_staging_audit_manifest.json", "inventory_staging_audit_report.md"],
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
