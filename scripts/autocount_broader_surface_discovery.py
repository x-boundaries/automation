import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_CONFIG_PATH = Path("config/autocount_broader_surface_discovery.example.json")
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\probe\broader_surfaces"
NEEDS_RECONCILIATION = "Needs reconciliation"

DEFAULT_DISCOVERY_GROUPS = {
    "debtor_customer": ["debtor", "customer", "cust"],
    "creditor_supplier": ["creditor", "supplier", "vendor"],
    "chart_of_accounts_gl": ["account", "gl", "ledger", "coa", "accno", "journal"],
    "ar_ap_opening": ["ar", "ap", "opening", "receivable", "payable", "balance"],
    "locations": ["location", "warehouse", "store", "branch"],
    "payment_methods": ["payment", "paymethod", "cash", "bank", "cheque", "receipt"],
    "purchase_order_outstanding_po": ["purchase", "purchaseorder", "po", "order", "outstanding"],
    "stock_in_transit_candidates": ["transit", "transfer", "fromlocation", "tolocation", "intransit"],
    "stock_reference_followup": ["item", "stock", "uom", "balance", "movement", "stockdtl"],
}

SECRET_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+"
)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_discovery(config, source=None, output_root=None, include_row_counts=None, now=None):
    plan = build_run_plan(
        config,
        output_root=output_root,
        include_row_counts=include_row_counts,
        now=now,
    )
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    groups = normalize_discovery_groups(config.get("discovery_groups") or DEFAULT_DISCOVERY_GROUPS)
    warnings = []
    exceptions = []
    context = {}
    object_inventory = []
    column_inventory = []
    approximate_counts = []
    matches = empty_matches(groups)
    status = "success"

    try:
        active_source = source or create_source(config)
        context = sanitize_context(active_source.fetch_context())
        object_inventory = sanitize_object_inventory(active_source.fetch_objects(plan["schemas"]))
        column_inventory = sanitize_column_inventory(active_source.fetch_columns(plan["schemas"]))
        if plan["include_row_counts"]:
            approximate_counts = sanitize_approximate_counts(active_source.fetch_row_counts(object_inventory))
            apply_approximate_counts(object_inventory, approximate_counts)
        matches = match_candidate_surfaces(object_inventory, column_inventory, groups)
    except Exception as exc:  # noqa: BLE001 - manifest must preserve safe failure detail.
        status = "failed"
        exceptions.append(sanitize_text(str(exc)))

    manifest = {
        "job": config.get("job", "autocount_broader_surface_discovery"),
        "status": status,
        "run_id": plan["run_id"],
        "timestamp": plan["started_at"],
        "started_at": plan["started_at"],
        "finished_at": datetime.now().astimezone().isoformat(),
        "connection_string_env": plan["connection_string_env"],
        "schemas": plan["schemas"],
        "include_row_counts": plan["include_row_counts"],
        "context": context,
        "counts": {
            "objects": len(object_inventory),
            "columns": len(column_inventory),
            "approximate_object_counts": len(approximate_counts),
            "exceptions": len(exceptions),
        },
        "candidate_groups": build_candidate_group_summary(groups, matches),
        "matched_objects_by_group": matches["matched_objects_by_group"],
        "matched_columns_by_group": matches["matched_columns_by_group"],
        "object_counts_by_group": {
            group_name: len(records) for group_name, records in matches["matched_objects_by_group"].items()
        },
        "object_inventory": object_inventory,
        "column_inventory": column_inventory,
        "stock_smoke_reference_surfaces": sanitize_reference_surfaces(
            config.get("stock_smoke_reference_surfaces", [])
        ),
        "warnings": warnings,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
        "storage": {
            "output_root": plan["output_root"],
            "run_path": plan["run_path"],
            "manifest": str(run_path / "broader_surface_discovery_manifest.json"),
            "report": str(run_path / "broader_surface_discovery_report.md"),
        },
        "notes": [
            "All candidate surfaces are metadata-only heuristic matches and require AutoCount UI/report reconciliation before extraction use.",
            "Debtor, creditor, GL, AR/AP, payment, PO, stock-in-transit, and stock reference candidates are not final production mappings.",
            "This discovery run does not export raw ERP rows and does not schedule extraction.",
        ],
    }

    report_path = run_path / "broader_surface_discovery_report.md"
    report_path.write_text(render_report(manifest), encoding="utf-8")
    manifest_path = run_path / "broader_surface_discovery_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def build_run_plan(config, output_root=None, include_row_counts=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    run_id = str(uuid4())
    connection_string_env = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if connection_string_env != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    resolved_output_root = resolve_output_root(output_root or config.get("output_root") or DEFAULT_OUTPUT_ROOT)
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "connection_string_env": connection_string_env,
        "schemas": parse_schemas(config.get("schemas")),
        "include_row_counts": bool(
            include_row_counts if include_row_counts is not None else config.get("include_row_counts", False)
        ),
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root
            / f"broader_surface_discovery_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
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
    return SqlServerBroaderSurfaceSource(connection_string, database_name=config.get("database_name", ""))


class SqlServerBroaderSurfaceSource:
    def __init__(self, connection_string, database_name=""):
        self.connection_string = connection_string
        self.database_name = database_name or ""

    def fetch_context(self):
        rows = self.query(
            """
            SELECT
              DB_NAME() AS current_database,
              SUSER_SNAME() AS current_login,
              USER_NAME() AS current_user_name
            """
        )
        return rows[0] if rows else {}

    def fetch_objects(self, schemas=None):
        clause, params = schema_filter_clause("s.name", schemas)
        return self.query(
            f"""
            SELECT
              s.name AS schema_name,
              o.name AS object_name,
              o.type_desc AS object_type,
              o.create_date,
              o.modify_date
            FROM sys.objects AS o
            INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            WHERE o.type IN ('U', 'V') {clause}
            ORDER BY s.name, o.name
            """,
            params,
        )

    def fetch_columns(self, schemas=None):
        clause, params = schema_filter_clause("s.name", schemas)
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
            WHERE o.type IN ('U', 'V') {clause}
            ORDER BY s.name, o.name, c.column_id
            """,
            params,
        )

    def fetch_row_counts(self, objects):
        clause, params = build_object_filter(objects)
        if not clause:
            return []
        return self.query(
            f"""
            SELECT
              s.name AS object_schema,
              o.name AS object_name,
              SUM(p.row_count) AS approximate_row_count
            FROM sys.dm_db_partition_stats AS p
            INNER JOIN sys.objects AS o ON o.object_id = p.object_id
            INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            WHERE p.index_id IN (0, 1) AND ({clause})
            GROUP BY s.name, o.name
            ORDER BY s.name, o.name
            """,
            params,
        )

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before running broader surface discovery") from exc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            if self.database_name:
                cursor.execute(f"USE {quote_identifier(self.database_name)}")
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, [coerce_cell(value) for value in record])) for record in cursor.fetchall()]


def build_check_definitions():
    return [
        {
            "name": "context",
            "sql": "SELECT DB_NAME() AS current_database, SUSER_SNAME() AS current_login, USER_NAME() AS current_user_name",
        },
        {
            "name": "object_inventory",
            "sql": "SELECT s.name AS schema_name, o.name AS object_name, o.type_desc AS object_type, o.create_date, o.modify_date FROM sys.objects AS o INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id WHERE o.type IN ('U', 'V')",
        },
        {
            "name": "column_inventory",
            "sql": "SELECT s.name AS object_schema, o.name AS object_name, c.name AS column_name, t.name AS data_type, c.max_length, c.precision, c.scale, CONVERT(bit, c.is_nullable) AS is_nullable, c.column_id FROM sys.columns AS c INNER JOIN sys.objects AS o ON o.object_id = c.object_id INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id INNER JOIN sys.types AS t ON t.user_type_id = c.user_type_id WHERE o.type IN ('U', 'V')",
        },
        {
            "name": "approximate_object_counts",
            "sql": "SELECT s.name AS object_schema, o.name AS object_name, SUM(p.row_count) AS approximate_row_count FROM sys.dm_db_partition_stats AS p INNER JOIN sys.objects AS o ON o.object_id = p.object_id INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id WHERE p.index_id IN (0, 1) GROUP BY s.name, o.name",
        },
    ]


def normalize_discovery_groups(configured_groups):
    groups = {}
    for group_name, group_config in configured_groups.items():
        if isinstance(group_config, dict):
            hints = group_config.get("keyword_hints", [])
        else:
            hints = group_config
        groups[group_name] = [str(hint) for hint in hints if str(hint).strip()]
    return groups


def match_candidate_surfaces(objects, columns, groups):
    columns_by_object = {}
    for column in columns:
        object_id = make_object_id(column.get("object_schema"), column.get("object_name"))
        columns_by_object.setdefault(object_id, []).append(column)

    matched_objects_by_group = {group_name: [] for group_name in groups}
    matched_columns_by_group = {group_name: [] for group_name in groups}

    for group_name, hints in groups.items():
        for obj in objects:
            object_id = make_object_id(obj.get("schema_name"), obj.get("object_name"))
            object_columns = columns_by_object.get(object_id, [])
            object_texts = [obj.get("schema_name", ""), obj.get("object_name", ""), obj.get("object_type", "")]
            matched_keywords = set()
            matched_column_names = set()

            for hint in hints:
                if any(matches_keyword(text, hint) for text in object_texts):
                    matched_keywords.add(hint)
                for column in object_columns:
                    if matches_keyword(column.get("column_name", ""), hint):
                        matched_keywords.add(hint)
                        matched_column_names.add(column.get("column_name", ""))

            if not matched_keywords:
                continue

            matched_objects_by_group[group_name].append(
                {
                    "group": group_name,
                    "object_id": object_id,
                    "schema_name": obj.get("schema_name", ""),
                    "object_name": obj.get("object_name", ""),
                    "object_type": obj.get("object_type", ""),
                    "create_date": obj.get("create_date", ""),
                    "modify_date": obj.get("modify_date", ""),
                    "approximate_row_count": obj.get("approximate_row_count"),
                    "matched_keywords": sorted(matched_keywords),
                    "matched_columns": sorted(name for name in matched_column_names if name),
                    "decision": NEEDS_RECONCILIATION,
                    "heuristic": True,
                }
            )

            for column in object_columns:
                column_matches = [hint for hint in hints if matches_keyword(column.get("column_name", ""), hint)]
                if not column_matches:
                    continue
                matched_columns_by_group[group_name].append(
                    {
                        "group": group_name,
                        "object_id": object_id,
                        "schema_name": obj.get("schema_name", ""),
                        "object_name": obj.get("object_name", ""),
                        "column_name": column.get("column_name", ""),
                        "data_type": column.get("data_type", ""),
                        "max_length": column.get("max_length"),
                        "precision": column.get("precision"),
                        "scale": column.get("scale"),
                        "is_nullable": column.get("is_nullable"),
                        "matched_keywords": sorted(set(column_matches)),
                        "decision": NEEDS_RECONCILIATION,
                        "heuristic": True,
                    }
                )

    return {
        "matched_objects_by_group": matched_objects_by_group,
        "matched_columns_by_group": matched_columns_by_group,
    }


def build_candidate_group_summary(groups, matches):
    summary = {}
    for group_name, hints in groups.items():
        summary[group_name] = {
            "keyword_hints": list(hints),
            "decision": NEEDS_RECONCILIATION,
            "matched_object_count": len(matches["matched_objects_by_group"].get(group_name, [])),
            "matched_column_count": len(matches["matched_columns_by_group"].get(group_name, [])),
            "final_production_selected": False,
        }
    return summary


def empty_matches(groups):
    return {
        "matched_objects_by_group": {group_name: [] for group_name in groups},
        "matched_columns_by_group": {group_name: [] for group_name in groups},
    }


def sanitize_object_inventory(objects):
    safe = []
    for obj in objects:
        safe.append(
            {
                "object_id": make_object_id(obj.get("schema_name"), obj.get("object_name")),
                "schema_name": sanitize_text(obj.get("schema_name", "")),
                "object_name": sanitize_text(obj.get("object_name", "")),
                "object_type": sanitize_text(obj.get("object_type", "")),
                "create_date": coerce_cell(obj.get("create_date", "")),
                "modify_date": coerce_cell(obj.get("modify_date", "")),
            }
        )
    return safe


def sanitize_column_inventory(columns):
    safe = []
    for column in columns:
        safe.append(
            {
                "object_id": make_object_id(column.get("object_schema"), column.get("object_name")),
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


def sanitize_approximate_counts(counts):
    safe = []
    for count in counts:
        safe.append(
            {
                "object_id": make_object_id(count.get("object_schema"), count.get("object_name")),
                "object_schema": sanitize_text(count.get("object_schema", "")),
                "object_name": sanitize_text(count.get("object_name", "")),
                "approximate_row_count": count.get("approximate_row_count"),
            }
        )
    return safe


def apply_approximate_counts(objects, counts):
    by_object = {record["object_id"]: record.get("approximate_row_count") for record in counts}
    for obj in objects:
        object_id = obj["object_id"]
        if object_id in by_object:
            obj["approximate_row_count"] = by_object[object_id]


def sanitize_reference_surfaces(surfaces):
    safe = []
    for surface in surfaces:
        safe.append(
            {
                "schema_name": sanitize_text(surface.get("schema_name", "")),
                "object_name": sanitize_text(surface.get("object_name", "")),
                "status": "reference_only_not_final_mapping",
                "note": sanitize_text(surface.get("note", "Known stock smoke reference only.")),
            }
        )
    return safe


def sanitize_context(context):
    return {str(key): sanitize_text(value) for key, value in dict(context or {}).items()}


def render_report(manifest):
    lines = [
        "# AutoCount Broader Surface Discovery Report",
        "",
        "This report is generated from SQL Server metadata only. It does not approve final extraction mapping.",
        "",
        "## Run",
        "",
        f"- Status: {manifest['status']}",
        f"- Run ID: {manifest['run_id']}",
        f"- Objects inspected: {manifest['counts']['objects']}",
        f"- Columns inspected: {manifest['counts']['columns']}",
        "",
        "## Candidate Groups",
        "",
    ]
    for group_name, group in manifest["candidate_groups"].items():
        lines.append(f"### {group_name}")
        lines.append(f"- Decision: {group['decision']}")
        lines.append(f"- Matched objects: {group['matched_object_count']}")
        lines.append(f"- Matched columns: {group['matched_column_count']}")
        records = manifest["matched_objects_by_group"].get(group_name, [])
        if not records:
            lines.append("- No metadata matches.")
        for record in records:
            lines.append(
                f"- `{record['object_id']}` ({record['object_type']}): "
                f"{', '.join(record['matched_keywords'])}"
            )
        lines.append("")

    lines.extend(
        [
            "## Required Follow-Up",
            "",
            "- Reconcile every candidate against AutoCount UI/report outputs before any extraction use.",
            "- Keep debtor, creditor, GL, payment, PO, and stock-in-transit accounting treatment unresolved until sign-off.",
            "- Do not schedule extraction from this discovery output.",
        ]
    )
    if manifest["exceptions"]:
        lines.extend(["", "## Exceptions", ""])
        for exception in manifest["exceptions"]:
            lines.append(f"- {sanitize_text(exception)}")
    return "\n".join(lines) + "\n"


def build_object_filter(objects):
    clauses = []
    params = []
    for obj in objects:
        clauses.append("(s.name = ? AND o.name = ?)")
        params.extend([obj.get("schema_name"), obj.get("object_name")])
    return " OR ".join(clauses), params


def schema_filter_clause(column_expression, schemas):
    selected = parse_schemas(schemas)
    if not selected:
        return "", []
    placeholders = ", ".join("?" for _ in selected)
    return f" AND {column_expression} IN ({placeholders})", selected


def parse_schemas(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def make_object_id(schema_name, object_name):
    return f"{schema_name}.{object_name}"


def matches_keyword(value, keyword):
    text = str(value or "")
    if not text:
        return False
    key = normalize_keyword(keyword)
    compact_text = normalize_keyword(text)
    if len(key) <= 2:
        return compact_text.startswith(key) or bool(
            re.search(rf"(^|[^a-z0-9]){re.escape(key)}([^a-z0-9]|$)", text.lower())
        )
    return key in compact_text


def normalize_keyword(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def quote_identifier(value):
    escaped = str(value).replace("]", "]]")
    return f"[{escaped}]"


def coerce_cell(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def sanitize_text(text):
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", str(text))


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
        description="Discover broader AutoCount 2 candidate SQL surfaces using metadata-only reads."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free discovery config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    parser.add_argument("--include-row-counts", action="store_true", help="Include safe approximate object row counts.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned run context without connecting to SQL.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_config(args.config)
        include_row_counts = True if args.include_row_counts else None
        if args.dry_run:
            plan = build_run_plan(config, output_root=args.output_root, include_row_counts=include_row_counts)
            plan["dry_run"] = True
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        manifest = run_discovery(config, output_root=args.output_root, include_row_counts=include_row_counts)
        print(json.dumps({"status": manifest["status"], "run_path": manifest["storage"]["run_path"]}, indent=2))
        return 0 if manifest["status"] == "success" else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
