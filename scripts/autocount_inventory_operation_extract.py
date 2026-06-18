import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from autocount_inventory_operation_validate import (  # noqa: E402
    BUSINESS_RECONCILIATION_STATUS,
    DATA_MATURITY,
    DEFAULT_SELECTED_SURFACES,
    NEEDS_RECONCILIATION,
    validate_target_context,
)
from csv_safety import safe_csv_row  # noqa: E402


DEFAULT_CONFIG_PATH = Path("config/autocount_inventory_operation_extract.example.json")
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\extract\inventory_operations"
SOURCE_OF_TRUTH_STATUS = "current_ac2_database_snapshot"

BASE_WARNINGS = [
    "This is a local raw snapshot export for inspection only.",
    "Data may be immature/pre-go-live/test/partial.",
    "This is not business-reconciled and does not approve extraction for production use.",
    "Do not commit generated outputs.",
    "Do not use as final migration/import/dashboard approval.",
]

SECRET_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+"
)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_extraction(config, source=None, output_root=None, now=None):
    plan = build_run_plan(config, output_root=output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    warnings = list(BASE_WARNINGS)
    exceptions = []
    context = {}
    surface_exports = []
    status = "success"
    row_limit = normalize_row_limit(config.get("row_limit"))

    try:
        selected_surfaces = normalize_selected_surfaces(config.get("selected_surfaces") or DEFAULT_SELECTED_SURFACES)
        validate_allowlisted_surfaces(selected_surfaces)
        active_source = source or create_source(config)
        context = sanitize_context(active_source.fetch_context())
        validate_target_context(context, config)
        column_inventory = sanitize_column_inventory(active_source.fetch_columns(selected_surfaces))
        columns_by_surface = columns_grouped_by_surface(column_inventory)

        for surface in selected_surfaces:
            export, export_warnings, export_exceptions = extract_surface(
                active_source,
                surface,
                columns_by_surface.get(make_surface_id(surface["schema_name"], surface["object_name"]), []),
                run_path,
                row_limit,
            )
            surface_exports.append(export)
            warnings.extend(export_warnings)
            exceptions.extend(export_exceptions)

        if any(export["status"] == "failed" for export in surface_exports) or any(
            warning.startswith("missing_expected_columns:") for warning in warnings
        ):
            status = "success_with_warnings"
        if all_critical_surfaces_failed(selected_surfaces, surface_exports):
            status = "failed"
            warnings.append("all_critical_surfaces_failed")
    except Exception as exc:  # noqa: BLE001 - manifest captures setup/target failures.
        status = "failed"
        exceptions.append({"surface_id": "run_setup", "message": sanitize_text(str(exc))})
        surface_exports = []

    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    manifest = normalize_for_json(
        {
            "job": config.get("job", "autocount_inventory_operation_extract"),
            "status": status,
            "run_id": plan["run_id"],
            "started_at": plan["started_at"],
            "finished_at": finished_at.isoformat(),
            "connection_string_env": plan["connection_string_env"],
            "context": context,
            "data_maturity": DATA_MATURITY,
            "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
            "source_of_truth_status": SOURCE_OF_TRUTH_STATUS,
            "decision": NEEDS_RECONCILIATION,
            "final_production_selected": False,
            "extraction_mode": "raw_selected_inventory_operation_snapshot",
            "row_limit": row_limit,
            "selected_surfaces": [
                {
                    "surface_name": surface.get("surface_name", ""),
                    "business_function": surface.get("business_function", ""),
                    "schema_name": surface.get("schema_name", ""),
                    "object_name": surface.get("object_name", ""),
                    "object_id": make_surface_id(surface.get("schema_name"), surface.get("object_name")),
                    "critical": bool(surface.get("critical", False)),
                }
                for surface in normalize_selected_surfaces(config.get("selected_surfaces") or DEFAULT_SELECTED_SURFACES)
            ],
            "surface_exports": surface_exports,
            "row_counts": {export["object_id"]: export.get("row_count", 0) for export in surface_exports},
            "output_files": {export["object_id"]: export.get("output_path") for export in surface_exports},
            "schema_metadata": build_schema_metadata(surface_exports),
            "warnings": unique_preserve_order(warnings),
            "exception_count": len(exceptions),
            "exceptions": exceptions,
            "generated_output_disclaimer": (
                "Generated raw snapshot files are local-only under the configured output root; "
                "do not commit or paste raw rows."
            ),
            "storage": {
                "output_root": plan["output_root"],
                "run_path": plan["run_path"],
                "manifest": str(run_path / "inventory_operation_extract_manifest.json"),
                "report": str(run_path / "inventory_operation_extract_report.md"),
            },
            "notes": [
                "No joins, transformations, final dashboard metrics, scheduler, or write-back are performed.",
                "Review only manifest/report counts before deciding whether staging/dashboard design is safe.",
                "CoA, GL, bank opening balances, and accounting migration scope remain parked.",
            ],
        }
    )

    manifest_path = run_path / "inventory_operation_extract_manifest.json"
    report_path = run_path / "inventory_operation_extract_report.md"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    report_path.write_text(render_report(manifest), encoding="utf-8")
    return manifest


def build_run_plan(config, output_root=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    connection_string_env = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if connection_string_env != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    resolved_output_root = resolve_output_root(output_root or config.get("output_root") or DEFAULT_OUTPUT_ROOT)
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "connection_string_env": connection_string_env,
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root
            / f"inventory_operation_extract_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def extract_surface(source, surface, available_columns, run_path, row_limit):
    object_id = make_surface_id(surface["schema_name"], surface["object_name"])
    warnings = []
    exceptions = []
    expected_columns = list(surface.get("expected_columns", []))
    available_normalized = {normalize_keyword(column) for column in available_columns}
    selected_columns = [column for column in expected_columns if normalize_keyword(column) in available_normalized]
    missing_columns = [column for column in expected_columns if normalize_keyword(column) not in available_normalized]
    if missing_columns:
        warnings.append(f"missing_expected_columns:{object_id}")
    if not selected_columns:
        selected_columns = expected_columns

    order_by = [column for column in surface.get("order_by", []) if column in selected_columns]
    sql = build_extract_sql(surface, selected_columns, order_by=order_by, row_limit=row_limit)
    output_path = run_path / f"{safe_filename(object_id)}.csv"
    base_export = build_surface_export(surface, selected_columns, missing_columns, output_path)

    try:
        rows = list(source.fetch_rows({**surface, "object_id": object_id, "columns": selected_columns, "sql": sql}))
        write_csv(output_path, rows, selected_columns)
        base_export.update(
            {
                "status": "success",
                "row_count": len(rows),
                "output_path": str(output_path),
                "file_name": output_path.name,
                "file": describe_file(output_path),
            }
        )
    except Exception as exc:  # noqa: BLE001 - one surface failure should not stop the whole run.
        base_export.update({"status": "failed", "row_count": 0, "output_path": str(output_path), "file_name": output_path.name})
        warnings.append(f"surface_extract_failed:{object_id}")
        exceptions.append({"surface_id": object_id, "message": sanitize_text(str(exc))})

    return base_export, warnings, exceptions


def build_surface_export(surface, selected_columns, missing_columns, output_path):
    object_id = make_surface_id(surface["schema_name"], surface["object_name"])
    return {
        "surface_name": surface.get("surface_name") or surface["object_name"],
        "business_function": surface.get("business_function", ""),
        "schema_name": surface["schema_name"],
        "object_name": surface["object_name"],
        "object_id": object_id,
        "critical": bool(surface.get("critical", False)),
        "status": "pending",
        "row_count": 0,
        "selected_columns": list(selected_columns),
        "expected_columns": list(surface.get("expected_columns", [])),
        "missing_expected_columns": list(missing_columns),
        "output_path": str(output_path),
        "file_name": output_path.name,
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
        "data_maturity": DATA_MATURITY,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
    }


def build_schema_metadata(surface_exports):
    return [
        {
            "object_id": export["object_id"],
            "schema_name": export["schema_name"],
            "object_name": export["object_name"],
            "selected_columns": export.get("selected_columns", []),
            "expected_columns": export.get("expected_columns", []),
            "missing_expected_columns": export.get("missing_expected_columns", []),
        }
        for export in surface_exports
    ]


def build_extract_sql(surface, columns, order_by=None, row_limit=None):
    if not columns:
        raise ValueError(f"{make_surface_id(surface.get('schema_name'), surface.get('object_name'))} needs explicit columns")
    if any(str(column).strip() == "*" for column in columns):
        raise ValueError("Column allowlist must not include *")
    top_clause = f"TOP ({int(row_limit)}) " if row_limit is not None else ""
    sql = (
        f"SELECT {top_clause}{', '.join(quote_identifier(column) for column in columns)} "
        f"FROM {quote_identifier(surface.get('schema_name', 'dbo'))}.{quote_identifier(surface.get('object_name', ''))}"
    )
    if order_by:
        sql += f" ORDER BY {', '.join(quote_identifier(column) for column in order_by)}"
    return sql


def normalize_selected_surfaces(value):
    surfaces = []
    for surface in list(value or []):
        object_name = sanitize_text(surface.get("object_name", ""))
        if not object_name:
            continue
        surfaces.append(
            {
                "surface_name": sanitize_text(surface.get("surface_name", object_name)),
                "business_function": sanitize_text(surface.get("business_function", "")),
                "schema_name": sanitize_text(surface.get("schema_name", "dbo")),
                "object_name": object_name,
                "expected_columns": [sanitize_text(column) for column in surface.get("expected_columns", [])],
                "order_by": [sanitize_text(column) for column in surface.get("order_by", surface.get("safe_key_columns", []))],
                "critical": bool(surface.get("critical", False)),
            }
        )
    return surfaces


def validate_allowlisted_surfaces(selected_surfaces):
    allowlist = {
        make_surface_id(surface.get("schema_name"), surface.get("object_name"))
        for surface in normalize_selected_surfaces(DEFAULT_SELECTED_SURFACES)
    }
    for surface in selected_surfaces:
        object_id = make_surface_id(surface["schema_name"], surface["object_name"])
        if object_id not in allowlist:
            raise ValueError(f"{object_id} is not allowlisted for selected inventory operation extraction")


def all_critical_surfaces_failed(selected_surfaces, surface_exports):
    critical_ids = {
        make_surface_id(surface.get("schema_name"), surface.get("object_name"))
        for surface in selected_surfaces
        if surface.get("critical")
    }
    if not critical_ids:
        return False
    succeeded_ids = {export["object_id"] for export in surface_exports if export.get("status") == "success"}
    return critical_ids.isdisjoint(succeeded_ids)


def normalize_row_limit(value):
    if value in (None, ""):
        return None
    row_limit = int(value)
    if row_limit <= 0:
        raise ValueError("row_limit must be a positive integer or null")
    return row_limit


def sanitize_column_inventory(columns):
    safe = []
    for column in columns:
        schema_name = sanitize_text(column.get("object_schema", column.get("schema_name", "")))
        object_name = sanitize_text(column.get("object_name", ""))
        safe.append(
            {
                "object_id": make_surface_id(schema_name, object_name),
                "object_schema": schema_name,
                "object_name": object_name,
                "column_name": sanitize_text(column.get("column_name", "")),
                "data_type": sanitize_text(column.get("data_type", "")),
                "is_nullable": column.get("is_nullable"),
                "column_id": column.get("column_id"),
            }
        )
    return safe


def sanitize_context(context):
    return {str(key): sanitize_text(value) for key, value in dict(context or {}).items()}


def columns_grouped_by_surface(columns):
    grouped = {}
    for column in columns:
        grouped.setdefault(column["object_id"], []).append(column["column_name"])
    return grouped


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows([safe_csv_row(row, columns) for row in rows])


def describe_file(path):
    return {
        "path": str(path),
        "byte_size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def build_console_summary(manifest):
    return {
        "status": manifest.get("status"),
        "run_path": manifest.get("storage", {}).get("run_path"),
        "selected_surface_count": len(manifest.get("surface_exports", [])),
        "exported_surface_count": sum(1 for export in manifest.get("surface_exports", []) if export.get("status") == "success"),
        "failed_surface_count": sum(1 for export in manifest.get("surface_exports", []) if export.get("status") == "failed"),
        "row_counts": manifest.get("row_counts", {}),
        "warnings": manifest.get("warnings", []),
    }


def render_report(manifest):
    lines = [
        "# Inventory Operation Raw Snapshot Extract Report",
        "",
        "This report summarizes local raw snapshot files by surface. It does not include raw ERP/business rows.",
        "",
        "## Run Context",
        "",
        f"- Status: {manifest['status']}",
        f"- Run ID: {manifest['run_id']}",
        f"- Run path: {manifest['storage']['run_path']}",
        f"- Database: {manifest.get('context', {}).get('current_database', '')}",
        f"- Login/User: {manifest.get('context', {}).get('current_login', '')}"
        f"/{manifest.get('context', {}).get('current_user_name', '')}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Decision: {manifest['decision']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        f"- Extraction mode: {manifest['extraction_mode']}",
        f"- Row limit: {manifest.get('row_limit')}",
        "",
        "## Warnings",
        "",
    ]
    for warning in manifest["warnings"]:
        lines.append(f"- {warning}")

    lines.extend(["", "## Surface Outputs", ""])
    for export in manifest["surface_exports"]:
        lines.append(f"### `{export['object_id']}`")
        lines.append(f"- Status: {export['status']}")
        lines.append(f"- Business function: {export['business_function']}")
        lines.append(f"- Row count: {export['row_count']}")
        lines.append(f"- File name: {export.get('file_name')}")
        lines.append(f"- Selected columns: {len(export.get('selected_columns', []))}")
        lines.append(f"- Missing expected columns: {', '.join(export.get('missing_expected_columns', [])) or 'none'}")
        lines.append(f"- Decision: {export['decision']}")
        lines.append(f"- Final production selected: {str(export['final_production_selected']).lower()}")
        lines.append("")

    lines.extend(
        [
            "## Follow-Up",
            "",
            "- Review only manifest/report counts first; inspect raw local files only on the AC2 VM as needed.",
            "- Do not commit generated CSVs or paste raw rows into PRs or chats.",
            "- Do not build staging, warehouse, dashboards, scheduler, or write-back from this run.",
        ]
    )
    if manifest["exceptions"]:
        lines.extend(["", "## Exceptions", ""])
        for exception in manifest["exceptions"]:
            lines.append(f"- {exception.get('surface_id')}: {sanitize_text(exception.get('message', ''))}")
    return "\n".join(lines) + "\n"


def create_source(config):
    env_name = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if env_name != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    connection_string = os.environ.get(env_name)
    if not connection_string:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return SqlServerInventoryOperationExtractSource(connection_string)


class SqlServerInventoryOperationExtractSource:
    def __init__(self, connection_string):
        self.connection_string = connection_string

    def fetch_context(self):
        result = self.query(
            "SELECT @@SERVERNAME AS server_name, DB_NAME() AS current_database, "
            "SUSER_SNAME() AS current_login, USER_NAME() AS current_user_name"
        )
        return result[0] if result else {}

    def fetch_columns(self, selected_surfaces):
        clause, params = build_surface_filter("s.name", "o.name", selected_surfaces)
        if not clause:
            return []
        return self.query(
            "SELECT s.name AS object_schema, o.name AS object_name, c.name AS column_name, "
            "t.name AS data_type, CONVERT(bit, c.is_nullable) AS is_nullable, c.column_id "
            "FROM sys.columns AS c "
            "INNER JOIN sys.objects AS o ON o.object_id = c.object_id "
            "INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
            "INNER JOIN sys.types AS t ON t.user_type_id = c.user_type_id "
            f"WHERE o.type IN ('U', 'V') AND ({clause}) "
            "ORDER BY s.name, o.name, c.column_id",
            params,
        )

    def fetch_rows(self, surface_spec):
        return self.query(surface_spec["sql"])

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before inventory operation extraction") from exc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, [coerce_cell(value) for value in record])) for record in cursor.fetchall()]


def build_surface_filter(schema_expression, object_expression, surfaces):
    clauses = []
    params = []
    for surface in surfaces:
        clauses.append(f"({schema_expression} = ? AND {object_expression} = ?)")
        params.extend([surface.get("schema_name", "dbo"), surface.get("object_name", "")])
    return " OR ".join(clauses), params


def make_surface_id(schema_name, object_name):
    return f"{schema_name}.{object_name}"


def quote_identifier(value):
    escaped = str(value).replace("]", "]]")
    return f"[{escaped}]"


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._") or "surface"


def normalize_keyword(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def sanitize_text(text):
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", str(text))


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


def coerce_cell(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
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
    parser = argparse.ArgumentParser(
        description="Export local raw snapshots for selected inventory operation AutoCount surfaces."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free extraction config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_extraction(load_config(args.config), output_root=args.output_root)
        print(json.dumps(build_console_summary(manifest), indent=2))
        return 0 if manifest["status"] in {"success", "success_with_warnings"} else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
