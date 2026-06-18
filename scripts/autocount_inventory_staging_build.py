import argparse
import csv
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from csv_safety import safe_csv_row  # noqa: E402


DEFAULT_CONFIG_PATH = Path("config/autocount_inventory_staging_build.example.json")
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\staging\inventory_operations"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"

STAGING_TABLES = [
    {
        "table_name": "stg_ac2_supplier",
        "sources": ["dbo.vCreditor", "dbo.Creditor"],
        "columns": ["supplier_code", "supplier_name", "source_surface", "source_extract_run_id", "source_row_number"],
    },
    {
        "table_name": "stg_ac2_purchase_order_header",
        "sources": ["dbo.vPurchaseOrder", "dbo.PO"],
        "columns": [
            "po_doc_key",
            "po_doc_no",
            "po_doc_date",
            "supplier_code",
            "supplier_name",
            "purchase_location",
            "doc_status",
            "cancelled",
            "source_surface",
            "source_extract_run_id",
            "source_row_number",
        ],
    },
    {
        "table_name": "stg_ac2_purchase_order_line",
        "sources": ["dbo.PODTL"],
        "columns": [
            "po_doc_key",
            "po_dtl_key",
            "item_code",
            "description",
            "uom",
            "qty",
            "transferred_qty",
            "location",
            "outstanding_qty_candidate",
            "source_surface",
            "source_extract_run_id",
            "source_row_number",
        ],
    },
    {
        "table_name": "stg_ac2_grn_header",
        "sources": ["dbo.vGoodsReceivedNote", "dbo.GR"],
        "columns": ["grn_doc_no", "grn_doc_date", "supplier_code", "source_surface", "source_extract_run_id", "source_row_number"],
    },
    {
        "table_name": "stg_ac2_grn_line",
        "sources": ["dbo.vGoodsReceivedNoteDetail", "dbo.vGoodsReceivedNoteSubDetail", "dbo.GRDTL"],
        "columns": ["grn_doc_no", "grn_dtl_key", "item_code", "source_surface", "source_extract_run_id", "source_row_number"],
    },
    {
        "table_name": "stg_ac2_stock_receive_header",
        "sources": ["dbo.vStockReceive"],
        "columns": ["receive_doc_no", "receive_doc_date", "source_surface", "source_extract_run_id", "source_row_number"],
    },
    {
        "table_name": "stg_ac2_stock_receive_line",
        "sources": ["dbo.vStockReceiveDetail"],
        "columns": ["receive_doc_no", "item_code", "source_surface", "source_extract_run_id", "source_row_number"],
    },
    {
        "table_name": "stg_ac2_transfer_header",
        "sources": ["dbo.vStockTransfer", "dbo.XFER"],
        "columns": [
            "transfer_doc_no",
            "transfer_doc_date",
            "from_location",
            "to_location",
            "source_surface",
            "source_extract_run_id",
            "source_row_number",
        ],
    },
    {
        "table_name": "stg_ac2_transfer_line",
        "sources": ["dbo.vStockTransferDetail"],
        "columns": ["transfer_doc_no", "item_code", "source_surface", "source_extract_run_id", "source_row_number"],
    },
    {
        "table_name": "stg_ac2_stock_movement_reference",
        "sources": ["dbo.StockDTL"],
        "columns": ["doc_type", "item_code", "source_surface", "source_extract_run_id", "source_row_number"],
    },
    {
        "table_name": "stg_ac2_item_balance_reference",
        "sources": ["dbo.vItemBalQty"],
        "columns": ["item_code", "source_surface", "source_extract_run_id", "source_row_number"],
    },
]


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Extract manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_staging_build(manifest_path, output_root=None, config=None, allow_warning_source=False, now=None):
    plan = build_run_plan(output_root or (config or {}).get("output_root"), now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    warnings = []
    exceptions = []
    status = "success"
    staging_tables = []
    source_manifest = {}
    manifest_path = Path(manifest_path)
    selected_extract_run_path = manifest_path.parent.resolve(strict=False)

    try:
        source_manifest = load_manifest(manifest_path)
        validate_source_manifest(source_manifest, allow_warning_source=allow_warning_source)
        warnings.extend([sanitize_text(warning) for warning in source_manifest.get("warnings", [])])
        extract_run_path = resolve_selected_extract_run_path(manifest_path, source_manifest)
        selected_extract_run_path = extract_run_path
        exports = {export.get("object_id"): export for export in source_manifest.get("surface_exports", [])}
        for table_spec in STAGING_TABLES:
            table, table_warnings = build_staging_table(table_spec, exports, extract_run_path, run_path, source_manifest)
            staging_tables.append(table)
            warnings.extend(table_warnings)
        if warnings:
            status = "success_with_warnings"
    except Exception as exc:  # noqa: BLE001 - write redacted failure manifest for local review.
        status = "failed"
        exceptions.append(sanitize_text(str(exc)))
        if "surface_results" in str(exc):
            warnings.append("manifest_uses_legacy_surface_results_field")

    manifest = build_manifest(
        source_manifest,
        staging_tables,
        plan,
        status,
        warnings,
        exceptions,
        source_extract_run_path=selected_extract_run_path,
        now=now,
    )
    manifest_path_out = run_path / "inventory_staging_build_manifest.json"
    report_path = run_path / "inventory_staging_build_report.md"
    manifest["storage"]["manifest"] = str(manifest_path_out)
    manifest["storage"]["report"] = str(report_path)
    manifest_path_out.write_text(json.dumps(normalize_for_json(manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_path.write_text(render_report(manifest), encoding="utf-8")
    return normalize_for_json(manifest)


def build_run_plan(output_root=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    resolved_output_root = resolve_output_root(output_root or DEFAULT_OUTPUT_ROOT)
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root / f"inventory_staging_build_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    resolved = Path(output_root).expanduser().resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def validate_source_manifest(source_manifest, allow_warning_source=False):
    if "surface_exports" not in source_manifest:
        if "surface_results" in source_manifest:
            raise ValueError("Expected PR #50 manifest field `surface_exports`; found legacy `surface_results`.")
        raise ValueError("Extract manifest missing required `surface_exports` field.")
    if source_manifest.get("status") == "failed" or (
        source_manifest.get("status") == "success_with_warnings" and not allow_warning_source
    ):
        # PR #50 local run is success_with_warnings, so default call path treats warnings as reviewable.
        if source_manifest.get("status") == "failed":
            raise ValueError("Source extract manifest status is failed.")


def resolve_selected_extract_run_path(manifest_path, source_manifest):
    selected_extract_run_path = Path(manifest_path).parent.resolve(strict=False)
    declared_run_path = source_manifest.get("storage", {}).get("run_path")
    if declared_run_path:
        declared = Path(declared_run_path).resolve(strict=False)
        if declared != selected_extract_run_path:
            raise ValueError(
                "source_manifest_run_path_mismatch:"
                f"declared={sanitize_text(declared)};"
                f"selected={sanitize_text(selected_extract_run_path)}"
            )
    return selected_extract_run_path


def build_staging_table(table_spec, exports, extract_run_path, run_path, source_manifest):
    rows = []
    warnings = []
    source_extract_run_id = source_manifest.get("run_id", "")
    for object_id in table_spec["sources"]:
        export = exports.get(object_id)
        if not export:
            warnings.append(f"source_surface_missing:{object_id}")
            continue
        csv_path, path_warning = resolve_source_csv(export, extract_run_path)
        if path_warning:
            warnings.append(path_warning)
            continue
        if not csv_path.exists():
            warnings.append(f"source_csv_missing:{object_id}")
            continue
        for row_number, source_row in enumerate(read_csv(csv_path), start=1):
            rows.append(map_row(table_spec["table_name"], object_id, source_row, source_extract_run_id, row_number, warnings))

    output_path = run_path / f"{table_spec['table_name']}.csv"
    write_csv(output_path, table_spec["columns"], rows)
    return (
        {
            "table_name": table_spec["table_name"],
            "row_count": len(rows),
            "output_path": str(output_path),
            "file_name": output_path.name,
            "source_surfaces": list(table_spec["sources"]),
            "status": "ready_empty_surface" if not rows else "ready_non_empty_surface",
            "decision": NEEDS_RECONCILIATION,
            "final_production_selected": False,
        },
        warnings,
    )


def resolve_source_csv(export, extract_run_path):
    path = Path(export.get("output_path") or "")
    resolved = path.resolve(strict=False)
    if not _is_relative_to(resolved, extract_run_path.resolve(strict=False)):
        return resolved, f"source_csv_outside_extract_run:{export.get('object_id')}"
    return resolved, None


def map_row(table_name, source_surface, row, source_extract_run_id, row_number, warnings):
    lineage = {
        "source_surface": source_surface,
        "source_extract_run_id": source_extract_run_id,
        "source_row_number": str(row_number),
    }
    if table_name == "stg_ac2_supplier":
        return {
            "supplier_code": first_present(row, ["CreditorCode", "AccNo"]),
            "supplier_name": first_present(row, ["CreditorCompanyName", "CompanyName"]),
            **lineage,
        }
    if table_name == "stg_ac2_purchase_order_header":
        return {
            "po_doc_key": row.get("DocKey", ""),
            "po_doc_no": row.get("DocNo", ""),
            "po_doc_date": row.get("DocDate", ""),
            "supplier_code": row.get("CreditorCode", ""),
            "supplier_name": row.get("CreditorName", ""),
            "purchase_location": row.get("PurchaseLocation", ""),
            "doc_status": row.get("DocStatus", ""),
            "cancelled": row.get("Cancelled", ""),
            **lineage,
        }
    if table_name == "stg_ac2_purchase_order_line":
        outstanding = calculate_outstanding(row.get("Qty"), row.get("TransferedQty"))
        if outstanding == "" and (row.get("Qty") or row.get("TransferedQty")):
            warnings.append("outstanding_qty_candidate_not_numeric:dbo.PODTL")
        return {
            "po_doc_key": row.get("DocKey", ""),
            "po_dtl_key": row.get("DtlKey", ""),
            "item_code": row.get("ItemCode", ""),
            "description": row.get("Description", ""),
            "uom": row.get("UOM", ""),
            "qty": row.get("Qty", ""),
            "transferred_qty": row.get("TransferedQty", ""),
            "location": row.get("Location", ""),
            "outstanding_qty_candidate": outstanding,
            **lineage,
        }
    if table_name == "stg_ac2_grn_header":
        return {"grn_doc_no": row.get("DocNo", ""), "grn_doc_date": row.get("DocDate", ""), "supplier_code": row.get("CreditorCode", ""), **lineage}
    if table_name == "stg_ac2_grn_line":
        return {"grn_doc_no": row.get("DocNo", ""), "grn_dtl_key": row.get("DtlKey", ""), "item_code": row.get("ItemCode", ""), **lineage}
    if table_name == "stg_ac2_stock_receive_header":
        return {"receive_doc_no": row.get("DocNo", ""), "receive_doc_date": row.get("DocDate", ""), **lineage}
    if table_name == "stg_ac2_stock_receive_line":
        return {"receive_doc_no": row.get("DocNo", ""), "item_code": row.get("ItemCode", ""), **lineage}
    if table_name == "stg_ac2_transfer_header":
        return {
            "transfer_doc_no": row.get("DocNo", ""),
            "transfer_doc_date": row.get("DocDate", ""),
            "from_location": row.get("FromLocation", ""),
            "to_location": row.get("ToLocation", ""),
            **lineage,
        }
    if table_name == "stg_ac2_transfer_line":
        return {"transfer_doc_no": row.get("DocNo", ""), "item_code": row.get("ItemCode", ""), **lineage}
    if table_name == "stg_ac2_stock_movement_reference":
        return {"doc_type": row.get("DocType", ""), "item_code": row.get("ItemCode", ""), **lineage}
    if table_name == "stg_ac2_item_balance_reference":
        return {"item_code": row.get("ItemCode", ""), **lineage}
    return lineage


def first_present(row, columns):
    for column in columns:
        if row.get(column):
            return row.get(column, "")
    return ""


def calculate_outstanding(qty, transferred_qty):
    if qty in (None, "") or transferred_qty in (None, ""):
        return ""
    try:
        value = Decimal(str(qty)) - Decimal(str(transferred_qty))
    except (InvalidOperation, ValueError):
        return ""
    if value == value.to_integral():
        return str(value.quantize(Decimal("1")))
    return str(value)


def build_manifest(source_manifest, staging_tables, plan, status, warnings, exceptions, source_extract_run_path=None, now=None):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    return {
        "job": "autocount_inventory_staging_build",
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_extract_run_id": source_manifest.get("run_id"),
        "source_extract_run_path": str(source_extract_run_path or ""),
        "staging_output_root": plan["run_path"],
        "staging_tables": staging_tables,
        "row_counts": {table["table_name"]: table["row_count"] for table in staging_tables},
        "warnings": unique_preserve_order([sanitize_text(warning) for warning in warnings]),
        "exception_count": len(exceptions),
        "exceptions": [sanitize_text(exception) for exception in exceptions],
        "decision": NEEDS_RECONCILIATION,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": [
            "Local staging CSV outputs only; no database load is performed.",
            "Dashboards are not meaningful yet because GRN/receive/transfer row counts are 0.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Operation Staging Build Report",
        "",
        "This is a local-only staging package. It does not load a database or build dashboards.",
        "",
        "## Source Extract",
        "",
        f"- Source extract run ID: {manifest.get('source_extract_run_id')}",
        f"- Source extract run path: {manifest.get('source_extract_run_path')}",
        f"- Status: {manifest['status']}",
        f"- Decision: {manifest['decision']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        "",
        "## Staging Tables",
        "",
    ]
    for table in manifest.get("staging_tables", []):
        lines.append(f"- `{table['table_name']}`: rows {table['row_count']}, file {table['file_name']}")
    lines.extend(["", "## Empty But Schema-Ready Tables", ""])
    for table in manifest.get("staging_tables", []):
        if table["row_count"] == 0:
            lines.append(f"- `{table['table_name']}`")
    lines.extend(["", "## Non-Empty Tables", ""])
    for table in manifest.get("staging_tables", []):
        if table["row_count"] > 0:
            lines.append(f"- `{table['table_name']}`")
    lines.extend(["", "## Warnings", ""])
    for warning in manifest.get("warnings", []) or ["none"]:
        lines.append(f"- {warning}")
    lines.extend(
        [
            "",
            "## Next Step",
            "",
            "- Review local staging build row counts and warnings.",
            "- dashboards are not meaningful yet because GRN/receive/transfer row counts are 0.",
            "- Do not build dashboards, scheduler, write-back, import/API behavior, or production database loading yet.",
        ]
    )
    return "\n".join(lines) + "\n"


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([safe_csv_row(row, columns) for row in rows])


def sanitize_text(text):
    return re.sub(
        r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+",
        lambda match: f"{match.group(1)}=<redacted>",
        str(text),
    )


def normalize_for_json(value):
    if isinstance(value, Decimal):
        return str(value)
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
        if value not in seen:
            seen.add(value)
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
    parser = argparse.ArgumentParser(description="Build local inventory operation staging CSV package.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_operation_extract_manifest.json.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free staging build config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    parser.add_argument("--allow-warning-source", action="store_true", help="Allow source extract manifests with warnings.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_config(args.config) if args.config else {}
        manifest = run_staging_build(
            args.manifest,
            output_root=args.output_root,
            config=config,
            allow_warning_source=args.allow_warning_source,
        )
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "run_path": manifest["storage"]["run_path"],
                    "row_counts": manifest["row_counts"],
                    "warnings": manifest["warnings"],
                    "exceptions": manifest["exceptions"],
                    "emitted_files": [table["file_name"] for table in manifest["staging_tables"]],
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
