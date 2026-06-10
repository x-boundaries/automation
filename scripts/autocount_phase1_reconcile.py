import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from csv_safety import coerce_csv_cell

DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_CONFIG_PATH = Path("config/autocount_phase1_reconcile.example.json")
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_phase1_reconcile_outputs"

REQUIRED_CONTRACTS = {
    "stock_master": [
        "ItemCode",
        "Description",
        "UOM",
        "Barcode",
        "ItemGroup",
        "ItemBrand",
        "ItemCategory",
        "ItemClass",
        "IsActive",
        "StockControl",
        "LastModified",
    ],
    "stock_balance": ["ItemCode", "Location", "UOM", "BalQty", "LastModified"],
    "stock_movement": ["ItemCode", "DocDate", "DocNo", "Location", "UOM", "Qty", "Cost", "TotalCost"],
    "stock_documents": ["DocDate", "DocNo", "ItemCode", "Location", "UOM", "Qty", "Cost", "TotalCost", "Cancelled"],
}

SUMMARY_FILES = {
    "object_counts": "object_counts.csv",
    "column_inventory": "column_inventory.csv",
    "column_coverage": "column_coverage.csv",
    "date_ranges": "date_ranges.csv",
    "location_counts": "location_counts.csv",
    "stock_balance_summary": "stock_balance_summary.csv",
    "movement_summary": "movement_summary.csv",
}

SENSITIVE_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_-]?key|access[_-]?key|connection\s*string|server|database|uid|user\s*id)\s*=\s*[^;\s]+"
)


def run_reconciliation(config, source=None, output_root=None, dry_run=False, now=None):
    plan = build_run_plan(config, output_root=output_root, dry_run=dry_run, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    objects = normalize_objects(config.get("objects", []))
    contracts = config.get("required_contracts") or REQUIRED_CONTRACTS
    summaries = empty_summaries()
    warnings = []

    if plan["dry_run"]:
        warnings.append("Dry run: SQL connection was not opened and output files contain configuration-only placeholders.")
        columns_by_object = {object_id(obj): [] for obj in objects}
    else:
        active_source = source or create_source(plan["connection_string_env"])
        summaries["column_inventory"] = active_source.fetch_column_inventory(objects)
        columns_by_object = columns_by_object_from_inventory(summaries["column_inventory"])
        summaries["object_counts"] = active_source.fetch_object_counts(objects)
        summaries["date_ranges"] = active_source.fetch_date_ranges(objects, columns_by_object)
        summaries["location_counts"] = active_source.fetch_location_counts(objects, columns_by_object)
        summaries["stock_balance_summary"] = active_source.fetch_balance_summaries(objects, columns_by_object)
        summaries["movement_summary"] = active_source.fetch_movement_summaries(objects, columns_by_object)

    summaries["column_coverage"] = build_column_coverage(objects, columns_by_object, contracts)
    output_files = write_outputs(run_path, summaries)

    manifest = {
        "job": config.get("job", "autocount_phase1_reconcile"),
        "status": "success",
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "target_label": sanitize_text(config.get("target_label", "")),
        "connection_string_env": plan["connection_string_env"],
        "dry_run": plan["dry_run"],
        "safe_outputs_only": True,
        "raw_rows_exported": False,
        "notes": [
            "Outputs are aggregate summaries only and intentionally exclude raw business rows.",
            "Connection strings and credentials are never written to output files.",
            "SQL surfaces remain draft candidates until AutoCount UI/report reconciliation passes.",
        ],
        "storage": {"output_root": str(Path(plan["output_root"])), "run_path": str(run_path)},
        "objects": [object_id(obj) for obj in objects],
        "output_files": output_files,
        "counts": {key: len(value) for key, value in summaries.items()},
        "warnings": [sanitize_text(warning) for warning in warnings],
    }

    manifest_path = run_path / "phase1_reconcile_manifest.json"
    manifest_path.write_text(json.dumps(redact_value(manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_path = run_path / "phase1_reconcile_report.md"
    report_path.write_text(render_report(manifest, summaries), encoding="utf-8")
    manifest["output_files"]["manifest"] = str(manifest_path)
    manifest["output_files"]["report"] = str(report_path)
    manifest_path.write_text(json.dumps(redact_value(manifest), indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def build_run_plan(config, output_root=None, dry_run=False, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    run_id = str(uuid4())
    resolved_output_root = resolve_output_root(output_root or config.get("output_root") or DEFAULT_OUTPUT_ROOT)
    connection_string_env = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if connection_string_env != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "connection_string_env": connection_string_env,
        "dry_run": bool(dry_run),
        "output_root": str(resolved_output_root),
        "run_path": str(resolved_output_root / f"phase1_reconcile_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def normalize_objects(objects):
    normalized = []
    for entry in objects:
        if isinstance(entry, str):
            schema, name = split_object_name(entry)
            normalized.append({"schema_name": schema, "object_name": name})
        else:
            normalized.append({"schema_name": entry["schema_name"], "object_name": entry["object_name"]})
    return normalized


def split_object_name(name):
    parts = name.split(".", 1)
    if len(parts) == 1:
        return "dbo", parts[0]
    return parts[0], parts[1]


def object_id(obj):
    return f"{obj.get('schema_name')}.{obj.get('object_name')}"


def empty_summaries():
    return {key: [] for key in SUMMARY_FILES}


def columns_by_object_from_inventory(column_inventory):
    columns = {}
    for row in column_inventory:
        object_key = row.get("object_id")
        column_name = row.get("column_name")
        if object_key and column_name:
            columns.setdefault(object_key, []).append(column_name)
    return columns


def build_column_coverage(objects, columns_by_object, contracts):
    rows = []
    for obj in objects:
        oid = object_id(obj)
        available = {column.lower(): column for column in columns_by_object.get(oid, [])}
        for contract_name, required_columns in contracts.items():
            present = [column for column in required_columns if column.lower() in available]
            missing = [column for column in required_columns if column.lower() not in available]
            rows.append(
                {
                    "object_id": oid,
                    "contract": contract_name,
                    "required_column_count": len(required_columns),
                    "present_column_count": len(present),
                    "missing_column_count": len(missing),
                    "present_columns": "|".join(present),
                    "missing_columns": "|".join(missing),
                    "status": "complete" if not missing else "needs_mapping",
                }
            )
    return rows


class SqlServerSummarySource:
    def __init__(self, connection_string):
        self.connection_string = connection_string

    def query(self, sql, params=None):
        import pyodbc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def fetch_column_inventory(self, objects):
        rows = []
        for obj in objects:
            for row in self.query(
                """
                SELECT
                    COLUMN_NAME AS column_name,
                    DATA_TYPE AS data_type,
                    IS_NULLABLE AS is_nullable
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                ORDER BY ORDINAL_POSITION
                """,
                [obj["schema_name"], obj["object_name"]],
            ):
                rows.append(
                    {
                        "object_id": object_id(obj),
                        "column_name": row.get("column_name"),
                        "data_type": row.get("data_type"),
                        "is_nullable": row.get("is_nullable"),
                    }
                )
        return rows

    def fetch_columns(self, objects):
        return columns_by_object_from_inventory(self.fetch_column_inventory(objects))

    def fetch_object_counts(self, objects):
        rows = []
        for obj in objects:
            count_rows = self.query(f"SELECT COUNT_BIG(1) AS row_count FROM {quote_object(obj)}")
            rows.append({"object_id": object_id(obj), "row_count": count_rows[0].get("row_count", 0) if count_rows else 0})
        return rows

    def fetch_date_ranges(self, objects, columns_by_object):
        rows = []
        for obj in objects:
            if has_column(columns_by_object, obj, "DocDate"):
                result = self.query(f"SELECT COUNT_BIG(1) AS row_count, MIN([DocDate]) AS min_doc_date, MAX([DocDate]) AS max_doc_date FROM {quote_object(obj)}")
                row = result[0] if result else {}
                rows.append({"object_id": object_id(obj), "row_count": row.get("row_count", 0), "min_doc_date": safe_scalar(row.get("min_doc_date")), "max_doc_date": safe_scalar(row.get("max_doc_date"))})
        return rows

    def fetch_location_counts(self, objects, columns_by_object):
        rows = []
        for obj in objects:
            if has_column(columns_by_object, obj, "Location"):
                for row in self.query(f"SELECT [Location] AS location_code, COUNT_BIG(1) AS row_count FROM {quote_object(obj)} GROUP BY [Location] ORDER BY [Location]"):
                    rows.append({"object_id": object_id(obj), "location_code": safe_bucket(row.get("location_code")), "row_count": row.get("row_count", 0)})
        return rows

    def fetch_balance_summaries(self, objects, columns_by_object):
        rows = []
        for obj in objects:
            if has_column(columns_by_object, obj, "BalQty"):
                result = self.query(f"SELECT COUNT_BIG(1) AS row_count, SUM(TRY_CONVERT(decimal(38, 6), [BalQty])) AS total_bal_qty, MIN(TRY_CONVERT(decimal(38, 6), [BalQty])) AS min_bal_qty, MAX(TRY_CONVERT(decimal(38, 6), [BalQty])) AS max_bal_qty FROM {quote_object(obj)}")
                row = result[0] if result else {}
                rows.append({"object_id": object_id(obj), "row_count": row.get("row_count", 0), "total_bal_qty": safe_scalar(row.get("total_bal_qty")), "min_bal_qty": safe_scalar(row.get("min_bal_qty")), "max_bal_qty": safe_scalar(row.get("max_bal_qty"))})
        return rows

    def fetch_movement_summaries(self, objects, columns_by_object):
        rows = []
        for obj in objects:
            oid = object_id(obj)
            if oid.lower() != "dbo.stockdtl":
                continue
            columns = columns_by_object.get(oid, [])
            select_parts = ["COUNT_BIG(1) AS row_count"]
            if column_in(columns, "DocDate"):
                select_parts.extend(["MIN([DocDate]) AS min_doc_date", "MAX([DocDate]) AS max_doc_date"])
            if column_in(columns, "Qty"):
                select_parts.append("SUM(TRY_CONVERT(decimal(38, 6), [Qty])) AS total_qty")
            if column_in(columns, "Cost"):
                select_parts.append("SUM(TRY_CONVERT(decimal(38, 6), [Cost])) AS total_cost")
            elif column_in(columns, "TotalCost"):
                select_parts.append("SUM(TRY_CONVERT(decimal(38, 6), [TotalCost])) AS total_cost")
            result = self.query(f"SELECT {', '.join(select_parts)} FROM {quote_object(obj)}")
            row = result[0] if result else {}
            rows.append({"object_id": oid, "row_count": row.get("row_count", 0), "min_doc_date": safe_scalar(row.get("min_doc_date")), "max_doc_date": safe_scalar(row.get("max_doc_date")), "total_qty": safe_scalar(row.get("total_qty")), "total_cost": safe_scalar(row.get("total_cost"))})
        return rows


def create_source(connection_string_env):
    connection_string = os.environ.get(connection_string_env)
    if not connection_string:
        raise RuntimeError(f"Set {connection_string_env} before running without --dry-run")
    return SqlServerSummarySource(connection_string)


def quote_object(obj):
    return f"{quote_name(obj['schema_name'])}.{quote_name(obj['object_name'])}"


def quote_name(name):
    return "[" + str(name).replace("]", "]]") + "]"


def has_column(columns_by_object, obj, column_name):
    return column_in(columns_by_object.get(object_id(obj), []), column_name)


def column_in(columns, column_name):
    return column_name.lower() in {str(column).lower() for column in columns}


def safe_bucket(value):
    if value is None or str(value).strip() == "":
        return "<blank>"
    return sanitize_text(str(value))


def safe_scalar(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return sanitize_text(str(value))


def write_outputs(run_path, summaries):
    output_files = {}
    for key, filename in SUMMARY_FILES.items():
        path = run_path / filename
        write_csv(path, summaries.get(key, []))
        output_files[key] = str(path)
    return output_files


def write_csv(path, rows):
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or ["status"])
        writer.writeheader()
        for row in rows:
            # Spreadsheet formula injection protection for future metadata/aggregate fields.
            writer.writerow({key: coerce_csv_cell(redact_value(row.get(key, ""))) for key in fieldnames})
    return str(path)


def render_report(manifest, summaries):
    lines = [
        "# AutoCount 2 Phase 1 Reconciliation Report",
        "",
        "This report contains safe aggregate summaries only. It does not contain raw business rows, credentials, connection strings, item descriptions, names, addresses, phone numbers, or remarks.",
        "",
        f"- Run ID: `{manifest['run_id']}`",
        f"- Target label: `{manifest.get('target_label', '')}`",
        f"- Dry run: `{manifest['dry_run']}`",
        "- SQL surface status: `Needs reconciliation`; no surface is selected for Phase 1 by this report.",
        "",
        "## Summary Counts",
    ]
    for key in SUMMARY_FILES:
        lines.append(f"- `{SUMMARY_FILES[key]}`: {len(summaries.get(key, []))} summary rows")
    if manifest.get("warnings"):
        lines.extend(["", "## Warnings"])
        lines.extend([f"- {warning}" for warning in manifest["warnings"]])
    lines.extend([
        "",
        "## Required Manual Comparisons",
        "- Compare stock item listing count to `object_counts.csv`, `column_inventory.csv`, and `column_coverage.csv`.",
        "- Compare location setup count to `location_counts.csv`.",
        "- Compare stock balance/status report totals to `stock_balance_summary.csv`.",
        "- Compare stock card/movement report totals to `movement_summary.csv` and `date_ranges.csv`.",
        "- Confirm the two `StockDTL` rows dated 2026-06-04 in AutoCount UI/report output without pasting raw rows into Git.",
    ])
    return "\n".join(lines) + "\n"


def redact_value(value):
    if isinstance(value, dict):
        return {key: redact_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [redact_value(inner) for inner in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def sanitize_text(text):
    return SENSITIVE_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", str(text))


def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _coerce_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def load_config(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Create safe Phase 1 AutoCount stock reconciliation summaries.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to reconciliation JSON config.")
    parser.add_argument("--output-root", help="Output root outside the repository.")
    parser.add_argument("--dry-run", action="store_true", help="Write manifest/report placeholders without opening SQL.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    manifest = run_reconciliation(load_config(args.config), output_root=args.output_root, dry_run=args.dry_run)
    print(f"Wrote Phase 1 reconciliation outputs to {manifest['storage']['run_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
