import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_CONFIG_PATH = Path("config/autocount_selected_surface_reconcile.example.json")
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\probe\selected_surface_reconcile"
NEEDS_RECONCILIATION = "Needs reconciliation"

WARNINGS = [
    "This is read-only and aggregate-only; it is not approved for extraction.",
    "Compare outputs with AutoCount UI/reports before any raw extraction design.",
    "No final production mapping selected.",
]

SAFE_TOTAL_COLUMNS = {
    "ar_opening": ["Outstanding", "LocalNetTotal", "NetTotal", "PaymentAmt"],
    "ap_opening": ["Outstanding", "LocalNetTotal", "NetTotal", "PaymentAmt"],
    "po_outstanding": ["NetTotal", "LocalNetTotal", "Total", "Amount"],
}

SELECTED_SURFACE_DEFAULTS = {
    "customer_master": [{"schema_name": "dbo", "object_name": "Debtor"}, {"schema_name": "dbo", "object_name": "vDebtor"}],
    "supplier_master": [{"schema_name": "dbo", "object_name": "Creditor"}, {"schema_name": "dbo", "object_name": "vCreditor"}],
    "branch_location": [{"schema_name": "dbo", "object_name": "Branch"}, {"schema_name": "dbo", "object_name": "vBranch"}],
    "payment_method": [{"schema_name": "dbo", "object_name": "PaymentMethod"}],
    "ar_opening": [{"schema_name": "dbo", "object_name": "ARInvoice"}],
    "ap_opening": [{"schema_name": "dbo", "object_name": "APInvoice"}],
    "po_outstanding": [{"schema_name": "dbo", "object_name": "PO"}, {"schema_name": "dbo", "object_name": "PODTL"}],
    "gl_transaction": [{"schema_name": "dbo", "object_name": "GLDTL"}],
    "coa_account_master": [],
}

SECRET_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+"
)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_reconciliation(config, source=None, output_root=None, now=None):
    plan = build_run_plan(config, output_root=output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    selected_surfaces = normalize_selected_surfaces(config.get("selected_surfaces") or SELECTED_SURFACE_DEFAULTS)
    enabled_groups = normalize_enabled_groups(config.get("enabled_aggregate_groups"), selected_surfaces)
    warnings = list(WARNINGS)
    exceptions = []
    context = {}
    column_inventory = []
    aggregate_results = []
    status = "success"

    try:
        active_source = source or create_source(config)
        context = sanitize_context(active_source.fetch_context())
        column_inventory = sanitize_column_inventory(active_source.fetch_columns(selected_surfaces))
        aggregate_results = run_aggregate_checks(active_source, selected_surfaces, enabled_groups, column_inventory)
    except Exception as exc:  # noqa: BLE001 - keep local report useful while redacting secrets.
        status = "failed"
        exceptions.append(sanitize_text(str(exc)))

    manifest = {
        "job": config.get("job", "autocount_selected_surface_reconcile"),
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": datetime.now().astimezone().isoformat(),
        "connection_string_env": plan["connection_string_env"],
        "context": context,
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
        "selected_surfaces_checked": selected_surface_ids(selected_surfaces, enabled_groups),
        "aggregate_results": aggregate_results,
        "warnings": warnings,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
        "storage": {
            "output_root": plan["output_root"],
            "run_path": plan["run_path"],
            "manifest": str(run_path / "selected_surface_reconcile_manifest.json"),
            "report": str(run_path / "selected_surface_reconcile_report.md"),
        },
        "notes": [
            "Outputs are aggregate-only and must be compared with AutoCount UI/reports before extraction.",
            "All areas remain Needs reconciliation with final_production_selected=false.",
            "GL and CoA remain unresolved and are not ready for extraction approval.",
        ],
    }

    (run_path / "selected_surface_reconcile_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (run_path / "selected_surface_reconcile_report.md").write_text(render_report(manifest), encoding="utf-8")
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
            / f"selected_surface_reconcile_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def create_source(config):
    env_name = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if env_name != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    connection_string = os.environ.get(env_name)
    if not connection_string:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return SqlServerSelectedSurfaceSource(connection_string, database_name=config.get("database_name", ""))


class SqlServerSelectedSurfaceSource:
    def __init__(self, connection_string, database_name=""):
        self.connection_string = connection_string
        self.database_name = database_name or ""

    def fetch_context(self):
        result = self.query(
            """
            SELECT
              DB_NAME() AS current_database,
              SUSER_SNAME() AS current_login,
              USER_NAME() AS current_user_name
            """
        )
        return result[0] if result else {}

    def fetch_columns(self, selected_surfaces):
        surfaces = flatten_surfaces(selected_surfaces)
        clause, params = build_surface_filter("s.name", "o.name", surfaces)
        if not clause:
            return []
        return self.query(
            f"""
            SELECT
              s.name AS object_schema,
              o.name AS object_name,
              c.name AS column_name,
              t.name AS data_type,
              c.max_length,
              c.precision,
              c.scale,
              CONVERT(bit, c.is_nullable) AS is_nullable,
              c.column_id
            FROM sys.columns AS c
            INNER JOIN sys.objects AS o ON o.object_id = c.object_id
            INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            INNER JOIN sys.types AS t ON t.user_type_id = c.user_type_id
            WHERE o.type IN ('U', 'V') AND ({clause})
            ORDER BY s.name, o.name, c.column_id
            """,
            params,
        )

    def fetch_aggregate(self, sql):
        result = self.query(sql)
        return result[0] if result else {}

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before selected surface reconciliation") from exc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            if self.database_name:
                cursor.execute(f"USE {quote_identifier(self.database_name)}")
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, [coerce_cell(value) for value in record])) for record in cursor.fetchall()]


def build_check_definitions(config):
    selected_surfaces = normalize_selected_surfaces(config.get("selected_surfaces") or SELECTED_SURFACE_DEFAULTS)
    enabled_groups = normalize_enabled_groups(config.get("enabled_aggregate_groups"), selected_surfaces)
    definitions = []
    for group_name in enabled_groups:
        for surface in selected_surfaces.get(group_name, []):
            spec = build_surface_check_spec(group_name, surface, {})
            if spec:
                definitions.append(spec)
    return definitions


def run_aggregate_checks(source, selected_surfaces, enabled_groups, columns):
    columns_by_surface = columns_grouped_by_surface(columns)
    results = []
    for group_name in enabled_groups:
        area = {
            "review_area": group_name,
            "decision": NEEDS_RECONCILIATION,
            "final_production_selected": False,
            "checks": [],
        }
        configured_surfaces = selected_surfaces.get(group_name, [])
        if not configured_surfaces:
            area["checks"].append(skipped_check(group_name, "", "", "unresolved_surface"))
            results.append(area)
            continue
        for surface in configured_surfaces:
            surface_id = make_surface_id(surface.get("schema_name"), surface.get("object_name"))
            available_columns = columns_by_surface.get(surface_id, {})
            if not available_columns:
                area["checks"].append(
                    skipped_check(
                        group_name,
                        surface.get("schema_name", ""),
                        surface.get("object_name", ""),
                        "surface_or_columns_not_found",
                    )
                )
                continue
            spec = build_surface_check_spec(group_name, surface, available_columns)
            if not spec:
                area["checks"].append(
                    skipped_check(
                        group_name,
                        surface.get("schema_name", ""),
                        surface.get("object_name", ""),
                        "no_safe_aggregate_defined",
                    )
                )
                continue
            values = sanitize_aggregate_values(source.fetch_aggregate(spec["sql"]))
            spec["aggregate_values"] = values
            area["checks"].append(spec)
        results.append(area)
    return results


def build_surface_check_spec(group_name, surface, available_columns):
    schema_name = surface.get("schema_name", "")
    object_name = surface.get("object_name", "")
    select_parts = ["COUNT(1) AS total_count"]
    skipped_metrics = []

    if group_name in {"customer_master", "supplier_master", "branch_location", "payment_method"}:
        add_optional_bit_counts(select_parts, skipped_metrics, available_columns, "IsActive", "active", "inactive")
    elif group_name in {"ar_opening", "ap_opening"}:
        if object_name not in {"ARInvoice", "APInvoice"}:
            return None
        add_optional_bit_counts(select_parts, skipped_metrics, available_columns, "Cancelled", "cancelled", "non_cancelled")
        add_optional_sums(select_parts, skipped_metrics, available_columns, SAFE_TOTAL_COLUMNS[group_name])
    elif group_name == "po_outstanding":
        if object_name == "PO":
            add_optional_bit_counts(select_parts, skipped_metrics, available_columns, "Cancelled", "cancelled", "non_cancelled")
            add_optional_sums(select_parts, skipped_metrics, available_columns, SAFE_TOTAL_COLUMNS[group_name])
        elif object_name == "PODTL":
            add_optional_sums(select_parts, skipped_metrics, available_columns, ["Qty", "TransferedQty"])
            if has_column(available_columns, "Qty") and has_column(available_columns, "TransferedQty"):
                select_parts.append(
                    "SUM(TRY_CONVERT(decimal(38, 6), [Qty]) - "
                    "TRY_CONVERT(decimal(38, 6), [TransferedQty])) AS outstanding_qty_total"
                )
            else:
                skipped_metrics.append(
                    {
                        "metric": "outstanding_qty_total",
                        "reason": "missing_optional_column",
                        "required_columns": ["Qty", "TransferedQty"],
                    }
                )
        else:
            return None
    elif group_name == "gl_transaction":
        add_optional_min_max(select_parts, skipped_metrics, available_columns, "TransDate", "trans_date")
    elif group_name == "coa_account_master":
        return None
    else:
        return None

    sql = f"SELECT {', '.join(select_parts)} FROM {quote_identifier(schema_name)}.{quote_identifier(object_name)}"
    return {
        "review_area": group_name,
        "schema_name": schema_name,
        "object_name": object_name,
        "object_id": make_surface_id(schema_name, object_name),
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
        "sql": sql,
        "aggregate_values": {},
        "skipped_metrics": skipped_metrics,
    }


def add_optional_bit_counts(select_parts, skipped_metrics, available_columns, column_name, true_alias, false_alias):
    if not has_column(available_columns, column_name):
        skipped_metrics.append({"metric": f"{true_alias}_count", "reason": "missing_optional_column", "column": column_name})
        skipped_metrics.append({"metric": f"{false_alias}_count", "reason": "missing_optional_column", "column": column_name})
        return
    quoted = quote_identifier(column_name)
    select_parts.append(
        f"SUM(CASE WHEN TRY_CONVERT(bit, {quoted}) = 1 THEN 1 ELSE 0 END) AS {true_alias}_count"
    )
    select_parts.append(
        f"SUM(CASE WHEN TRY_CONVERT(bit, {quoted}) = 1 THEN 0 ELSE 1 END) AS {false_alias}_count"
    )


def add_optional_sums(select_parts, skipped_metrics, available_columns, column_names):
    for column_name in column_names:
        if not has_column(available_columns, column_name):
            skipped_metrics.append(
                {"metric": f"{to_snake(column_name)}_total", "reason": "missing_optional_column", "column": column_name}
            )
            continue
        select_parts.append(
            f"SUM(TRY_CONVERT(decimal(38, 6), {quote_identifier(column_name)})) AS {to_snake(column_name)}_total"
        )


def add_optional_min_max(select_parts, skipped_metrics, available_columns, column_name, alias_prefix):
    if not has_column(available_columns, column_name):
        skipped_metrics.append({"metric": f"min_{alias_prefix}", "reason": "missing_optional_column", "column": column_name})
        skipped_metrics.append({"metric": f"max_{alias_prefix}", "reason": "missing_optional_column", "column": column_name})
        return
    quoted = quote_identifier(column_name)
    select_parts.append(f"MIN({quoted}) AS min_{alias_prefix}")
    select_parts.append(f"MAX({quoted}) AS max_{alias_prefix}")


def skipped_check(review_area, schema_name, object_name, reason):
    return {
        "review_area": review_area,
        "schema_name": schema_name,
        "object_name": object_name,
        "object_id": make_surface_id(schema_name, object_name) if object_name else "unresolved",
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
        "aggregate_values": {},
        "skipped_metrics": [{"metric": "surface_check", "reason": reason}],
    }


def normalize_selected_surfaces(value):
    normalized = {}
    for group_name, surfaces in dict(value or {}).items():
        normalized[group_name] = [
            {
                "schema_name": sanitize_text(surface.get("schema_name", "dbo")),
                "object_name": sanitize_text(surface.get("object_name", "")),
            }
            for surface in list(surfaces or [])
            if surface.get("object_name")
        ]
    return normalized


def normalize_enabled_groups(enabled_groups, selected_surfaces):
    if enabled_groups:
        return [str(group) for group in enabled_groups if str(group) in selected_surfaces or str(group) == "coa_account_master"]
    return list(selected_surfaces)


def sanitize_column_inventory(columns):
    safe = []
    for column in columns:
        safe.append(
            {
                "object_id": make_surface_id(column.get("object_schema"), column.get("object_name")),
                "object_schema": sanitize_text(column.get("object_schema", "")),
                "object_name": sanitize_text(column.get("object_name", "")),
                "column_name": sanitize_text(column.get("column_name", "")),
                "data_type": sanitize_text(column.get("data_type", "")),
                "max_length": column.get("max_length"),
                "precision": column.get("precision"),
                "scale": column.get("scale"),
                "is_nullable": column.get("is_nullable"),
                "column_id": column.get("column_id"),
            }
        )
    return safe


def columns_grouped_by_surface(columns):
    grouped = {}
    for column in columns:
        surface_id = make_surface_id(column.get("object_schema"), column.get("object_name"))
        grouped.setdefault(surface_id, {})[normalize_keyword(column.get("column_name", ""))] = column
    return grouped


def selected_surface_ids(selected_surfaces, enabled_groups):
    surface_ids = []
    for group_name in enabled_groups:
        for surface in selected_surfaces.get(group_name, []):
            surface_ids.append(make_surface_id(surface.get("schema_name"), surface.get("object_name")))
    return surface_ids


def flatten_surfaces(selected_surfaces):
    surfaces = []
    seen = set()
    for group_surfaces in selected_surfaces.values():
        for surface in group_surfaces:
            surface_id = make_surface_id(surface.get("schema_name"), surface.get("object_name"))
            if surface_id not in seen:
                seen.add(surface_id)
                surfaces.append(surface)
    return surfaces


def build_surface_filter(schema_expression, object_expression, surfaces):
    clauses = []
    params = []
    for surface in surfaces:
        clauses.append(f"({schema_expression} = ? AND {object_expression} = ?)")
        params.extend([surface.get("schema_name", "dbo"), surface.get("object_name", "")])
    return " OR ".join(clauses), params


def sanitize_aggregate_values(values):
    return {sanitize_text(key): coerce_cell(value) for key, value in dict(values or {}).items()}


def sanitize_context(context):
    return {str(key): sanitize_text(value) for key, value in dict(context or {}).items()}


def render_report(manifest):
    lines = [
        "# Selected Surface Reconciliation Aggregate Report",
        "",
        "This report is read-only, aggregate-only, and not approved for extraction.",
        "",
        "## Warnings",
        "",
    ]
    for warning in manifest["warnings"]:
        lines.append(f"- {warning}")
    lines.extend(
        [
            "",
            "## Run Context",
            "",
            f"- Status: {manifest['status']}",
            f"- Run ID: {manifest['run_id']}",
            f"- Database: {manifest.get('context', {}).get('current_database', '')}",
            f"- Login/User: {manifest.get('context', {}).get('current_login', '')}"
            f"/{manifest.get('context', {}).get('current_user_name', '')}",
            f"- Decision: {manifest['decision']}",
            f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
            "",
            "## Aggregate Results",
            "",
        ]
    )
    for area in manifest["aggregate_results"]:
        lines.append(f"### {area['review_area']}")
        lines.append(f"- Decision: {area['decision']}")
        lines.append(f"- Final production selected: {str(area['final_production_selected']).lower()}")
        for check in area["checks"]:
            lines.append(f"- `{check['object_id']}`")
            if check.get("aggregate_values"):
                for key, value in check["aggregate_values"].items():
                    lines.append(f"  - {key}: {value}")
            for skipped in check.get("skipped_metrics", []):
                lines.append(f"  - skipped {skipped.get('metric')}: {skipped.get('reason')}")
        lines.append("")
    lines.extend(
        [
            "## Follow-Up",
            "",
            "- Compare these aggregate counts and totals with AutoCount UI/reports before any extraction planning.",
            "- CoA remains unresolved; GLDTL is transaction/detail metadata only.",
            "- Do not use this report as final extraction approval.",
        ]
    )
    return "\n".join(lines) + "\n"


def extract_sql_aliases(sql):
    return re.findall(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", sql, flags=re.IGNORECASE)


def has_column(available_columns, column_name):
    return normalize_keyword(column_name) in available_columns


def make_surface_id(schema_name, object_name):
    return f"{schema_name}.{object_name}"


def quote_identifier(value):
    escaped = str(value).replace("]", "]]")
    return f"[{escaped}]"


def to_snake(value):
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^A-Za-z0-9]+", "_", parts).strip("_").lower()


def normalize_keyword(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def sanitize_text(text):
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", str(text))


def coerce_cell(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


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
        description="Run aggregate-only read-only reconciliation checks for selected AutoCount surfaces."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free reconcile config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_reconciliation(load_config(args.config), output_root=args.output_root)
        print(json.dumps({"status": manifest["status"], "run_path": manifest["storage"]["run_path"]}, indent=2))
        return 0 if manifest["status"] == "success" else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
