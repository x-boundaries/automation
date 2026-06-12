import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from csv_safety import safe_csv_row


DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\probe"
DEFAULT_CONFIG_PATH = Path("config/autocount_sql_probe.example.json")

DEFAULT_KEYWORD_GROUPS = {
    "item_product_stock_master": ["item", "product", "stock", "sku", "barcode", "uom"],
    "stock_balance_status": ["stockbalance", "balance", "status", "onhand", "quantity", "qty"],
    "stock_movement_card": ["movement", "stockcard", "stock card", "transaction", "ledger"],
    "stock_document_transfer_adjustment": [
        "document",
        "transfer",
        "adjustment",
        "adjust",
        "receive",
        "issue",
        "stocktake",
        "assembly",
    ],
    "sales_invoice_cash_sale": ["sales", "invoice", "cashsale", "cash sale"],
    "purchase_order_grn": ["purchase", "purchaseorder", "po", "grn", "goodsreceived", "goods received"],
    "debtor_customer": ["debtor", "customer"],
    "creditor_supplier": ["creditor", "supplier", "vendor"],
    "ar_ap": ["ar", "ap", "receivable", "payable"],
    "gl_account_journal": ["gl", "account", "journal", "ledger", "coa"],
    "payment_bank_cashbook": ["payment", "bank", "cashbook", "cash book", "receipt", "cheque", "deposit"],
}

RISKY_PERMISSION_NAMES = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "ALTER",
    "CONTROL",
    "CREATE TABLE",
}

FIXED_DATABASE_ROLES_TO_CHECK = [
    "db_owner",
    "db_datawriter",
    "db_ddladmin",
    "db_securityadmin",
    "db_accessadmin",
    "db_backupoperator",
    "db_datareader",
]

RISKY_DATABASE_ROLES = {
    "db_owner": "Database owner can read, write, alter schema, and administer the database.",
    "db_datawriter": "Data writer can insert, update, or delete rows and is not read-only.",
    "db_ddladmin": "DDL admin can run schema changes and is not read-only.",
    "db_securityadmin": "Security admin can change database security settings.",
    "db_accessadmin": "Access admin can add or remove database access.",
    "db_backupoperator": "Backup operator is elevated database access and should not be used for the probe/extractor login.",
}


def run_probe(
    config,
    source=None,
    output_root=None,
    database_name=None,
    schemas=None,
    sample_limit=None,
    include_row_counts=None,
    now=None,
):
    plan = build_probe_plan(
        config,
        output_root=output_root,
        database_name=database_name,
        schemas=schemas,
        sample_limit=sample_limit,
        include_row_counts=include_row_counts,
        now=now,
    )
    probe_path = Path(plan["probe_path"])
    probe_path.mkdir(parents=True, exist_ok=False)

    active_source = source or create_source(config, plan["database_name"])
    output_files = {}
    sample_files = []
    warnings = []
    errors = []

    try:
        server_info = active_source.fetch_server_info(plan["database_name"])
        schema_rows = active_source.fetch_schemas(plan["schemas"])
        object_rows = active_source.fetch_objects(plan["schemas"])
        column_rows = active_source.fetch_columns(plan["schemas"])
        index_rows = active_source.fetch_indexes(plan["schemas"])
        permission_rows = active_source.fetch_permissions()
        role_membership_rows = active_source.fetch_role_memberships()
        row_count_rows = []
        if plan["include_row_counts"]:
            row_count_rows = active_source.fetch_row_counts(object_rows)

        keyword_groups = config.get("keyword_groups") or DEFAULT_KEYWORD_GROUPS
        candidates = match_candidate_objects(object_rows, column_rows, keyword_groups)
        permission_risks = detect_risky_permissions(permission_rows)
        role_risks = detect_risky_roles(role_membership_rows)

        output_files["schemas_csv"] = write_csv(probe_path / "schemas.csv", schema_rows)
        output_files["objects_csv"] = write_csv(probe_path / "objects.csv", object_rows)
        output_files["columns_csv"] = write_csv(probe_path / "columns.csv", column_rows)
        output_files["indexes_csv"] = write_csv(probe_path / "indexes.csv", index_rows)
        output_files["permissions_csv"] = write_csv(probe_path / "permissions.csv", permission_rows)
        output_files["permission_risks_csv"] = write_csv(probe_path / "permission_risks.csv", permission_risks)
        output_files["role_memberships_csv"] = write_csv(probe_path / "role_memberships.csv", role_membership_rows)
        output_files["role_risks_csv"] = write_csv(probe_path / "role_risks.csv", role_risks)
        output_files["candidates_csv"] = write_csv(probe_path / "candidates.csv", flatten_candidates(candidates))
        if plan["include_row_counts"]:
            output_files["row_counts_csv"] = write_csv(probe_path / "row_counts.csv", row_count_rows)

        if plan["sample_limit"] > 0:
            sample_files, sample_warnings = write_selected_samples(
                probe_path,
                active_source,
                config.get("sample_objects", []),
                candidates,
                plan["sample_limit"],
            )
            warnings.extend(sample_warnings)

        report_path = probe_path / "probe_report.md"
        report_path.write_text(
            render_markdown_report(
                server_info=server_info,
                schemas=schema_rows,
                objects=object_rows,
                columns=column_rows,
                candidates=candidates,
                permission_risks=permission_risks,
                role_memberships=role_membership_rows,
                role_risks=role_risks,
                row_counts=row_count_rows,
                sample_files=sample_files,
                warnings=warnings,
            ),
            encoding="utf-8",
        )
        output_files["probe_report_md"] = str(report_path)
        status = "success"
    except Exception as exc:  # noqa: BLE001 - probe should preserve a safe failure manifest.
        server_info = {}
        schema_rows = []
        object_rows = []
        column_rows = []
        index_rows = []
        permission_rows = []
        role_membership_rows = []
        row_count_rows = []
        candidates = {name: [] for name in (config.get("keyword_groups") or DEFAULT_KEYWORD_GROUPS)}
        permission_risks = []
        role_risks = []
        errors.append(sanitize_text(str(exc)))
        status = "failed"

    manifest = {
        "job": config.get("job", "autocount_sql_probe"),
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": datetime.now().astimezone().isoformat(),
        "connection_string_env": plan["connection_string_env"],
        "database_name": plan["database_name"],
        "schemas": plan["schemas"],
        "sample_limit": plan["sample_limit"],
        "samples_enabled": plan["sample_limit"] > 0,
        "include_row_counts": plan["include_row_counts"],
        "counts": {
            "schemas": len(schema_rows),
            "objects": len(object_rows),
            "columns": len(column_rows),
            "indexes": len(index_rows),
            "permissions": len(permission_rows),
            "role_memberships": len(role_membership_rows),
            "row_counts": len(row_count_rows),
            "permission_risks": len(permission_risks),
            "role_risks": len(role_risks),
        },
        "candidate_groups": {group: len(records) for group, records in candidates.items()},
        "permission_risks": permission_risks,
        "role_risks": role_risks,
        "storage": {
            "output_root": str(plan["output_root"]),
            "probe_path": str(probe_path),
            "output_files": output_files,
            "sample_files": sample_files,
        },
        "warnings": warnings,
        "errors": errors,
        "notes": [
            "Candidate matches are heuristic metadata matches, not verified AutoCount extraction truth.",
            "Probe outputs may include metadata about the production database and must stay outside GitHub.",
        ],
    }
    manifest_path = probe_path / "probe_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def build_probe_plan(
    config,
    output_root=None,
    database_name=None,
    schemas=None,
    sample_limit=None,
    include_row_counts=None,
    now=None,
):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    run_id = str(uuid4())
    resolved_output_root = resolve_output_root(output_root or config.get("output_root") or DEFAULT_OUTPUT_ROOT)
    resolved_schemas = parse_schemas(schemas if schemas is not None else config.get("schemas"))
    resolved_sample_limit = int(sample_limit if sample_limit is not None else config.get("sample_limit", 0))
    if resolved_sample_limit < 0:
        raise ValueError("sample_limit must be 0 or greater")

    return {
        "job": config.get("job", "autocount_sql_probe"),
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "connection_string_env": config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV),
        "database_name": database_name if database_name is not None else config.get("database_name", ""),
        "schemas": resolved_schemas,
        "sample_limit": resolved_sample_limit,
        "include_row_counts": bool(
            include_row_counts if include_row_counts is not None else config.get("include_row_counts", False)
        ),
        "output_root": str(resolved_output_root),
        "probe_path": str(resolved_output_root / f"probe_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def match_candidate_objects(objects, columns, keyword_groups=None):
    groups = keyword_groups or DEFAULT_KEYWORD_GROUPS
    columns_by_object = {}
    for column in columns:
        object_id = make_object_id(column.get("object_schema"), column.get("object_name"))
        columns_by_object.setdefault(object_id, []).append(column)

    candidates = {group_name: [] for group_name in groups}
    for obj in objects:
        object_id = make_object_id(obj.get("schema_name"), obj.get("object_name"))
        object_columns = columns_by_object.get(object_id, [])
        object_texts = [obj.get("schema_name", ""), obj.get("object_name", ""), obj.get("object_type", "")]

        for group_name, keywords in groups.items():
            matched_keywords = set()
            matched_columns = set()
            for keyword in keywords:
                if any(matches_keyword(text, keyword) for text in object_texts):
                    matched_keywords.add(keyword)
                for column in object_columns:
                    if matches_keyword(column.get("column_name", ""), keyword):
                        matched_keywords.add(keyword)
                        matched_columns.add(column.get("column_name", ""))

            if matched_keywords:
                candidates[group_name].append(
                    {
                        "group": group_name,
                        "object_id": object_id,
                        "schema_name": obj.get("schema_name", ""),
                        "object_name": obj.get("object_name", ""),
                        "object_type": obj.get("object_type", ""),
                        "matched_keywords": sorted(matched_keywords),
                        "matched_columns": sorted(name for name in matched_columns if name),
                        "heuristic": True,
                        "reason": "Keyword match in SQL metadata only; validate before using for extraction.",
                    }
                )

    return candidates


def detect_risky_permissions(permission_rows):
    risks = []
    for row in permission_rows:
        permission_name = str(row.get("permission_name", "")).upper()
        class_desc = str(row.get("class_desc", "")).upper()
        if permission_name in RISKY_PERMISSION_NAMES:
            risks.append(
                {
                    "permission_name": permission_name,
                    "class_desc": class_desc,
                    "schema_name": row.get("schema_name") or row.get("object_schema") or "",
                    "object_name": row.get("object_name", ""),
                    "reason": "Visible permission may allow writes or schema changes.",
                }
            )
        elif permission_name == "EXECUTE" and class_desc in {"SCHEMA", "DATABASE"}:
            risks.append(
                {
                    "permission_name": permission_name,
                    "class_desc": class_desc,
                    "schema_name": row.get("schema_name") or row.get("object_schema") or "",
                    "object_name": row.get("object_name", ""),
                    "reason": "EXECUTE on a broad schema/database can expose posting routines.",
                }
            )
    return risks


def detect_risky_roles(role_membership_rows):
    risks = []
    seen_roles = set()
    for row in role_membership_rows:
        role_name = str(row.get("role_name", "")).lower()
        if role_name not in RISKY_DATABASE_ROLES or not is_positive_membership(row.get("is_member", True)):
            continue
        if role_name in seen_roles:
            continue
        seen_roles.add(role_name)
        risks.append(
            {
                "role_name": role_name,
                "member_name": row.get("member_name", ""),
                "source": row.get("source", ""),
                "reason": RISKY_DATABASE_ROLES[role_name],
                "best_effort": True,
            }
        )
    return risks


def is_positive_membership(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def sanitize_text(message):
    redacted = re.sub(r"(?i)(password|pwd)\s*=\s*[^;\s]+", r"\1=<redacted>", str(message))
    redacted = re.sub(r"(?i)(token|apikey|api_key)\s*=\s*[^;&\s]+", r"\1=<redacted>", redacted)
    return redacted


def create_source(config, database_name=None):
    env_name = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    connection_string = os.environ.get(env_name)
    if not connection_string:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return SqlServerMetadataSource(connection_string, database_name=database_name or config.get("database_name"))


class SqlServerMetadataSource:
    def __init__(self, connection_string, database_name=None):
        self.connection_string = connection_string
        self.database_name = database_name or ""

    def fetch_server_info(self, database_name=None):
        rows = self.query(
            """
            SELECT
              CAST(@@VERSION AS nvarchar(max)) AS sql_server_version,
              CAST(SERVERPROPERTY('Edition') AS nvarchar(256)) AS edition,
              DB_NAME() AS current_database,
              SUSER_SNAME() AS current_login,
              USER_NAME() AS current_user_name
            """
        )
        return rows[0] if rows else {}

    def fetch_schemas(self, schemas=None):
        clause, params = schema_filter_clause("s.name", schemas)
        return self.query(
            f"""
            SELECT s.name AS schema_name
            FROM sys.schemas AS s
            WHERE 1 = 1 {clause}
            ORDER BY s.name
            """,
            params,
        )

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
              CONVERT(bit, c.is_nullable) AS is_nullable
            FROM sys.columns AS c
            INNER JOIN sys.objects AS o ON o.object_id = c.object_id
            INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            INNER JOIN sys.types AS t ON t.user_type_id = c.user_type_id
            WHERE o.type IN ('U', 'V') {clause}
            ORDER BY s.name, o.name, c.column_id
            """,
            params,
        )

    def fetch_indexes(self, schemas=None):
        clause, params = schema_filter_clause("s.name", schemas)
        return self.query(
            f"""
            SELECT
              s.name AS object_schema,
              o.name AS object_name,
              i.name AS index_name,
              i.type_desc,
              CONVERT(bit, i.is_primary_key) AS is_primary_key,
              CONVERT(bit, i.is_unique) AS is_unique,
              c.name AS column_name,
              ic.key_ordinal
            FROM sys.indexes AS i
            INNER JOIN sys.objects AS o ON o.object_id = i.object_id
            INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            LEFT JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            LEFT JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE o.type IN ('U', 'V') AND i.name IS NOT NULL {clause}
            ORDER BY s.name, o.name, i.name, ic.key_ordinal
            """,
            params,
        )

    def fetch_permissions(self):
        return self.query(
            """
            SELECT
              perm.state_desc,
              perm.permission_name,
              perm.class_desc,
              USER_NAME(perm.grantee_principal_id) AS grantee_name,
              CASE WHEN perm.class_desc = 'SCHEMA' THEN SCHEMA_NAME(perm.major_id) END AS schema_name,
              OBJECT_SCHEMA_NAME(perm.major_id) AS object_schema,
              OBJECT_NAME(perm.major_id) AS object_name
            FROM sys.database_permissions AS perm
            WHERE perm.grantee_principal_id IN (USER_ID(), DATABASE_PRINCIPAL_ID('public'))
               OR USER_NAME(perm.grantee_principal_id) = USER_NAME()
            ORDER BY perm.class_desc, perm.permission_name
            """
        )

    def fetch_role_memberships(self):
        role_memberships = self.query(
            """
            SELECT
              role_principal.name AS role_name,
              member_principal.name AS member_name,
              CAST('database_role_members' AS nvarchar(64)) AS source,
              CONVERT(bit, 1) AS is_member
            FROM sys.database_role_members AS role_member
            INNER JOIN sys.database_principals AS role_principal
              ON role_principal.principal_id = role_member.role_principal_id
            INNER JOIN sys.database_principals AS member_principal
              ON member_principal.principal_id = role_member.member_principal_id
            WHERE member_principal.principal_id = USER_ID()
               OR member_principal.name = USER_NAME()
            ORDER BY role_principal.name, member_principal.name
            """
        )
        for role_name in FIXED_DATABASE_ROLES_TO_CHECK:
            rows = self.query(
                """
                SELECT
                  CAST(? AS nvarchar(128)) AS role_name,
                  USER_NAME() AS member_name,
                  CAST('is_rolemember' AS nvarchar(64)) AS source,
                  IS_ROLEMEMBER(?) AS is_member
                """,
                [role_name, role_name],
            )
            role_memberships.extend(rows)
        return role_memberships

    def fetch_row_counts(self, objects):
        object_filter = build_object_filter(objects)
        if not object_filter[0]:
            return []
        clause, params = object_filter
        return self.query(
            f"""
            SELECT
              s.name AS object_schema,
              o.name AS object_name,
              SUM(p.rows) AS row_count
            FROM sys.partitions AS p
            INNER JOIN sys.objects AS o ON o.object_id = p.object_id
            INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            WHERE p.index_id IN (0, 1) AND ({clause})
            GROUP BY s.name, o.name
            ORDER BY s.name, o.name
            """,
            params,
        )

    def fetch_sample_rows(self, object_ref, limit):
        schema_name = object_ref["schema_name"]
        object_name = object_ref["object_name"]
        sql = f"SELECT TOP ({int(limit)}) * FROM {quote_identifier(schema_name)}.{quote_identifier(object_name)}"
        return self.query(sql)

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount SQL Server VM before running the SQL probe") from exc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            if self.database_name:
                cursor.execute(f"USE {quote_identifier(self.database_name)}")
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, [coerce_cell(value) for value in row])) for row in cursor.fetchall()]


def write_selected_samples(probe_path, source, sample_objects, candidates, sample_limit):
    candidate_lookup = {
        record["object_id"]: {"schema_name": record["schema_name"], "object_name": record["object_name"]}
        for records in candidates.values()
        for record in records
    }
    sample_files = []
    warnings = []
    if not sample_objects:
        warnings.append("sample_limit is greater than 0, but no sample_objects were explicitly selected.")
        return sample_files, warnings

    samples_dir = probe_path / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    for selected in sample_objects:
        object_id = normalize_sample_object(selected)
        object_ref = candidate_lookup.get(object_id)
        if not object_ref:
            warnings.append(f"Skipping sample for {object_id}; it was not found in heuristic candidates.")
            continue
        rows = source.fetch_sample_rows(object_ref, sample_limit)
        sample_path = samples_dir / f"{safe_filename(object_id)}.csv"
        write_csv(sample_path, rows)
        sample_files.append({"object_id": object_id, "path": str(sample_path), "row_count": len(rows)})
    return sample_files, warnings


def render_markdown_report(
    server_info,
    schemas,
    objects,
    columns,
    candidates,
    permission_risks,
    role_memberships=None,
    role_risks=None,
    row_counts=None,
    sample_files=None,
    warnings=None,
):
    lines = [
        "# AutoCount SQL Probe Report",
        "",
        "This report is generated from SQL Server metadata. Candidate matches are heuristic and must be verified before use.",
        "",
        "## Server Context",
        "",
    ]
    for key, value in server_info.items():
        lines.append(f"- `{key}`: {value}")

    lines.extend(
        [
            "",
            "## Metadata Counts",
            "",
            f"- Schemas: {len(schemas)}",
            f"- Tables/views: {len(objects)}",
            f"- Columns: {len(columns)}",
            f"- Role memberships/checks: {len(role_memberships or [])}",
            f"- Row-count rows: {len(row_counts or [])}",
            "",
            "## Candidate Groups",
            "",
        ]
    )
    for group, records in candidates.items():
        lines.append(f"### {group}")
        if not records:
            lines.append("- No heuristic matches.")
        for record in records:
            lines.append(
                f"- `{record['object_id']}` ({record['object_type']}): keywords "
                f"{', '.join(record['matched_keywords'])}"
            )
        lines.append("")

    lines.extend(["## Permission Risk Flags", ""])
    if not permission_risks:
        lines.append("- No obvious risky permissions were visible to the probe login.")
    for risk in permission_risks:
        target = risk.get("object_name") or risk.get("schema_name") or risk.get("class_desc")
        lines.append(f"- `{risk['permission_name']}` on `{target}`: {risk['reason']}")

    lines.extend(
        [
            "",
            "## Role Membership Risk Flags",
            "",
            "These flags are best-effort checks from visible role memberships and fixed-role membership probes.",
        ]
    )
    if not role_risks:
        lines.append("- No obvious risky database roles were visible to the probe login.")
    for risk in role_risks or []:
        lines.append(f"- `{risk['role_name']}` via `{risk.get('source', '')}`: {risk['reason']}")

    if sample_files:
        lines.extend(["", "## Sample Files", ""])
        for sample in sample_files:
            lines.append(f"- `{sample['object_id']}`: {sample['path']}")

    if warnings:
        lines.extend(["", "## Warnings", ""])
        for warning in warnings:
            lines.append(f"- {sanitize_text(warning)}")

    return "\n".join(lines) + "\n"


def write_csv(path, rows):
    rows = [dict(row) for row in rows]
    fieldnames = infer_columns(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fieldnames:
            handle.write("")
            return str(path)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        # Spreadsheet formula injection protection, including optional sample exports.
        writer.writerows([safe_csv_row(row, fieldnames) for row in rows])
    return str(path)


def flatten_candidates(candidates):
    rows = []
    for records in candidates.values():
        for record in records:
            row = dict(record)
            row["matched_keywords"] = ", ".join(record["matched_keywords"])
            row["matched_columns"] = ", ".join(record["matched_columns"])
            rows.append(row)
    return rows


def infer_columns(rows):
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


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
        return compact_text.startswith(key) or bool(re.search(rf"(^|[^a-z0-9]){re.escape(key)}([^a-z0-9]|$)", text.lower()))
    return key in compact_text


def normalize_keyword(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def normalize_sample_object(selected):
    if isinstance(selected, str):
        return selected
    return make_object_id(selected.get("schema_name"), selected.get("object_name"))


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def quote_identifier(value):
    escaped = str(value).replace("]", "]]")
    return f"[{escaped}]"


def coerce_cell(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _coerce_datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe AutoCount SQL Server metadata using a read-only login.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free probe config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo. Defaults to C:\XB\autocount_outputs\probe.")
    parser.add_argument("--database-name", help="Optional database name/context to use for metadata queries.")
    parser.add_argument("--schemas", help="Comma-separated schema filter. Empty means all visible schemas.")
    parser.add_argument("--sample-limit", type=int, help="Optional SELECT TOP (N) sample limit. Defaults to config value.")
    parser.add_argument("--include-row-counts", action="store_true", help="Include approximate table row counts.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned probe context without connecting to SQL.")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        include_row_counts = True if args.include_row_counts else None
        plan = build_probe_plan(
            config,
            output_root=args.output_root,
            database_name=args.database_name,
            schemas=args.schemas,
            sample_limit=args.sample_limit,
            include_row_counts=include_row_counts,
        )
        if args.dry_run:
            plan["dry_run"] = True
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0

        manifest = run_probe(
            config,
            output_root=args.output_root,
            database_name=args.database_name,
            schemas=args.schemas,
            sample_limit=args.sample_limit,
            include_row_counts=include_row_counts,
        )
        print(json.dumps({"status": manifest["status"], "probe_path": manifest["storage"]["probe_path"]}, indent=2))
        return 1 if manifest["status"] == "failed" else 0
    except Exception as exc:  # noqa: BLE001 - CLI should not leak secrets in errors.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
