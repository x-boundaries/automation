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

from csv_safety import safe_csv_row


DEFAULT_CONFIG_PATH = Path("config/autocount_selected_surface_extract.example.json")
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\extract\selected_surfaces"
NEEDS_RECONCILIATION = "Needs reconciliation"

DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"
SOURCE_OF_TRUTH_STATUS = "current_ac2_database_snapshot"

WARNINGS = [
    "This is a local raw snapshot export.",
    "Data may be immature/pre-go-live/test/partial.",
    "This is not business-reconciled.",
    "Do not commit generated outputs.",
    "Do not use as final migration/import approval.",
    "CoA/account master remains unresolved.",
]

DEFAULT_SURFACES = {
    "Debtor": {
        "schema_name": "dbo",
        "object_name": "Debtor",
        "enabled": True,
        "object_type": "master_table",
        "columns": [
            "AccNo",
            "CompanyName",
            "IsActive",
            "DebtorType",
            "Phone1",
            "Mobile",
            "EmailAddress",
            "CurrencyCode",
            "TaxCode",
            "LastModified",
        ],
        "order_by": ["AccNo"],
    },
    "vDebtor": {
        "schema_name": "dbo",
        "object_name": "vDebtor",
        "enabled": True,
        "object_type": "enriched_view",
        "columns": [
            "DebtorCode",
            "DebtorCompanyName",
            "DebtorType",
            "DebtorPhone1",
            "DebtorMobile",
            "DebtorEmailAddress",
            "DebtorCurrencyCode",
            "DebtorTaxCode",
            "DebtorLastModified",
        ],
        "order_by": ["DebtorCode"],
    },
    "Creditor": {
        "schema_name": "dbo",
        "object_name": "Creditor",
        "enabled": True,
        "object_type": "master_table",
        "columns": [
            "AccNo",
            "CompanyName",
            "IsActive",
            "CreditorType",
            "Phone1",
            "Mobile",
            "EmailAddress",
            "CurrencyCode",
            "TaxCode",
            "LastModified",
        ],
        "order_by": ["AccNo"],
    },
    "vCreditor": {
        "schema_name": "dbo",
        "object_name": "vCreditor",
        "enabled": True,
        "object_type": "enriched_view",
        "columns": [
            "CreditorCode",
            "CreditorCompanyName",
            "CreditorType",
            "CreditorPhone1",
            "CreditorMobile",
            "CreditorEmailAddress",
            "CreditorCurrencyCode",
            "CreditorTaxCode",
            "CreditorLastModified",
        ],
        "order_by": ["CreditorCode"],
    },
    "PaymentMethod": {
        "schema_name": "dbo",
        "object_name": "PaymentMethod",
        "enabled": True,
        "object_type": "master_table",
        "columns": ["PaymentMethod", "BankAccount", "JournalType", "PaymentBy", "PaymentType", "IsActive", "LastUpdate"],
        "order_by": ["PaymentMethod"],
    },
    "PO": {
        "schema_name": "dbo",
        "object_name": "PO",
        "enabled": True,
        "object_type": "header_table",
        "columns": [
            "DocKey",
            "DocNo",
            "DocDate",
            "CreditorCode",
            "CreditorName",
            "NetTotal",
            "LocalNetTotal",
            "Total",
            "Cancelled",
            "DocStatus",
            "LastModified",
            "PurchaseLocation",
        ],
        "order_by": ["DocNo"],
    },
    "PODTL": {
        "schema_name": "dbo",
        "object_name": "PODTL",
        "enabled": True,
        "object_type": "detail_table",
        "columns": [
            "DocKey",
            "DtlKey",
            "Seq",
            "ItemCode",
            "Location",
            "Description",
            "Qty",
            "TransferedQty",
            "UOM",
            "DeliveryDate",
            "SubTotal",
            "LocalSubTotal",
        ],
        "order_by": ["DocKey", "DtlKey"],
    },
    "vPurchaseOrder": {
        "schema_name": "dbo",
        "object_name": "vPurchaseOrder",
        "enabled": True,
        "object_type": "enriched_view",
        "columns": [
            "DocKey",
            "DocNo",
            "DocDate",
            "CreditorCode",
            "CreditorName",
            "NetTotal",
            "LocalNetTotal",
            "Total",
            "Cancelled",
            "DocStatus",
            "LastModified",
            "PurchaseLocation",
        ],
        "order_by": ["DocNo"],
    },
    "ARInvoice": {
        "schema_name": "dbo",
        "object_name": "ARInvoice",
        "enabled": True,
        "object_type": "header_table",
        "columns": [
            "DocKey",
            "DocNo",
            "DocDate",
            "DebtorCode",
            "NetTotal",
            "LocalNetTotal",
            "Outstanding",
            "PaymentAmt",
            "Cancelled",
            "DocStatus",
            "LastModified",
        ],
        "order_by": ["DocNo"],
    },
    "APInvoice": {
        "schema_name": "dbo",
        "object_name": "APInvoice",
        "enabled": True,
        "object_type": "header_table",
        "columns": [
            "DocKey",
            "DocNo",
            "DocDate",
            "CreditorCode",
            "SupplierInvoiceNo",
            "NetTotal",
            "LocalNetTotal",
            "Outstanding",
            "PaymentAmt",
            "Cancelled",
            "DocStatus",
            "LastModified",
        ],
        "order_by": ["DocNo"],
    },
    "ARInvoiceDTL": {
        "schema_name": "dbo",
        "object_name": "ARInvoiceDTL",
        "enabled": False,
        "requires_flag": "enable_ap_ar_detail",
        "object_type": "detail_table",
        "columns": ["DocNo", "DtlKey", "ItemCode", "Description", "Qty", "UOM", "Amount"],
        "order_by": ["DocNo", "DtlKey"],
    },
    "APInvoiceDTL": {
        "schema_name": "dbo",
        "object_name": "APInvoiceDTL",
        "enabled": False,
        "requires_flag": "enable_ap_ar_detail",
        "object_type": "detail_table",
        "columns": ["DocNo", "DtlKey", "ItemCode", "Description", "Qty", "UOM", "Amount"],
        "order_by": ["DocNo", "DtlKey"],
    },
    "GLDTL": {
        "schema_name": "dbo",
        "object_name": "GLDTL",
        "enabled": False,
        "requires_flag": "enable_gl_transaction",
        "object_type": "transaction_detail",
        "columns": ["JournalNo", "DocNo", "TransDate", "AccNo", "Debit", "Credit", "Description"],
        "order_by": ["TransDate", "JournalNo"],
    },
}

SECRET_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+"
)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_extraction(config, source=None, output_root=None, now=None):
    plan = build_run_plan(config, output_root=output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    definitions = build_extract_definitions(config)
    skipped_surfaces = skipped_surface_specs(config)
    row_counts = {}
    output_files = {}
    selected_surfaces_exported = []
    exceptions = []
    context = {}
    status = "success"

    try:
        active_source = source or create_source(config)
        context = sanitize_context(active_source.fetch_context())
        for surface_spec in definitions:
            try:
                rows = active_source.fetch_rows(surface_spec)
                output_path = run_path / surface_spec["output_filename"]
                write_csv(output_path, rows, surface_spec["columns"])
                row_counts[surface_spec["surface_id"]] = len(rows)
                output_files[surface_spec["surface_id"]] = str(output_path)
                selected_surfaces_exported.append(
                    {
                        "surface_id": surface_spec["surface_id"],
                        "schema_name": surface_spec["schema_name"],
                        "object_name": surface_spec["object_name"],
                        "object_type": surface_spec.get("object_type", ""),
                        "columns": surface_spec["columns"],
                        "row_count": len(rows),
                        "output_file": str(output_path),
                        "file": describe_file(output_path),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - preserve run evidence while redacting secrets.
                status = "failed"
                row_counts[surface_spec["surface_id"]] = 0
                exceptions.append(
                    {
                        "surface_id": surface_spec["surface_id"],
                        "message": sanitize_text(str(exc)),
                    }
                )
    except Exception as exc:  # noqa: BLE001 - manifest should capture setup/connection failures.
        status = "failed"
        exceptions.append({"surface_id": "run_setup", "message": sanitize_text(str(exc))})

    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    manifest = {
        "job": config.get("job", "autocount_selected_surface_extract"),
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "connection_string_env": plan["connection_string_env"],
        "context": context,
        "business_date": config.get("business_date") or None,
        "as_of_date": config.get("as_of_date") or None,
        "data_maturity": DATA_MATURITY,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "source_of_truth_status": SOURCE_OF_TRUTH_STATUS,
        "warnings": list(WARNINGS),
        "selected_surfaces_exported": selected_surfaces_exported,
        "skipped_surfaces": skipped_surfaces,
        "row_counts": row_counts,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
        "output_files": output_files,
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
        "storage": {
            "output_root": plan["output_root"],
            "run_path": plan["run_path"],
            "manifest": str(run_path / "selected_surface_extract_manifest.json"),
            "report": str(run_path / "selected_surface_extract_report.md"),
        },
        "notes": [
            "Raw exported business data must stay local under the configured output root.",
            "Data may be immature/pre-go-live/test/partial and is not business-reconciled.",
            "CoA/account master remains unresolved and is not extracted.",
            "This snapshot must not be used as final migration/import approval.",
        ],
    }
    json_manifest = normalize_for_json(manifest)

    (run_path / "selected_surface_extract_manifest.json").write_text(
        json.dumps(json_manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (run_path / "selected_surface_extract_report.md").write_text(render_report(json_manifest), encoding="utf-8")
    return json_manifest


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
            / f"selected_surface_extract_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def build_extract_definitions(config):
    max_rows = normalize_max_rows(config.get("max_rows"))
    columns_by_surface = None
    if "column_inventory" in config:
        columns_by_surface = columns_grouped_by_surface(config.get("column_inventory"))
    definitions = []
    for surface in normalize_surface_configs(config.get("surfaces") or DEFAULT_SURFACES):
        if should_skip_surface(surface, config):
            continue
        columns = require_columns(surface)
        order_by = list(surface.get("order_by") or columns[:1])
        validate_columns_against_inventory(surface, columns, columns_by_surface, "column allowlist")
        validate_columns_against_inventory(surface, order_by, columns_by_surface, "order_by")
        top_clause = f"TOP ({max_rows}) " if max_rows is not None else ""
        sql = (
            f"SELECT {top_clause}{', '.join(quote_identifier(column) for column in columns)} "
            f"FROM {quote_identifier(surface['schema_name'])}.{quote_identifier(surface['object_name'])} "
            f"ORDER BY {', '.join(quote_identifier(column) for column in order_by)}"
        )
        definitions.append({**surface, "columns": columns, "order_by": order_by, "sql": sql})
    return definitions


def skipped_surface_specs(config):
    skipped = []
    for surface in normalize_surface_configs(config.get("surfaces") or DEFAULT_SURFACES):
        reason = skip_reason(surface, config)
        if reason:
            skipped.append(
                {
                    "surface_id": surface["surface_id"],
                    "schema_name": surface.get("schema_name", ""),
                    "object_name": surface.get("object_name", ""),
                    "reason": reason,
                }
            )
    if "coa_account_master" not in {item["surface_id"] for item in skipped}:
        skipped.append(
            {
                "surface_id": "coa_account_master",
                "schema_name": "",
                "object_name": "",
                "reason": "unresolved_no_confirmed_account_master_surface",
            }
        )
    return skipped


def normalize_surface_configs(value):
    normalized = []
    for config_key, surface in dict(value or {}).items():
        surface = dict(surface or {})
        schema_name = str(surface.get("schema_name", "dbo"))
        object_name = str(surface.get("object_name", config_key))
        normalized.append(
            {
                "config_key": str(config_key),
                "schema_name": schema_name,
                "object_name": object_name,
                "surface_id": make_surface_id(schema_name, object_name),
                "object_type": str(surface.get("object_type", "")),
                "enabled": bool(surface.get("enabled", True)),
                "requires_flag": str(surface.get("requires_flag", "")),
                "columns": [str(column) for column in list(surface.get("columns") or [])],
                "order_by": [str(column) for column in list(surface.get("order_by") or [])],
                "output_filename": f"{safe_filename(make_surface_id(schema_name, object_name))}.csv",
            }
        )
    return normalized


def should_skip_surface(surface, config):
    return skip_reason(surface, config) is not None


def skip_reason(surface, config):
    object_name = surface.get("object_name", "")
    config_key = surface.get("config_key", "")
    requires_flag = surface.get("requires_flag", "")
    gate_enabled = bool(requires_flag and config.get(requires_flag, False))
    if str(config_key).lower() == "coa_account_master" or str(object_name).lower() == "coa_account_master":
        return "unresolved_no_confirmed_account_master_surface"
    if object_name == "GLDTL" and not bool(config.get("enable_gl_transaction", False)):
        return "disabled_until_enable_gl_transaction_true"
    if object_name in {"ARInvoiceDTL", "APInvoiceDTL"} and not bool(config.get("enable_ap_ar_detail", False)):
        return "disabled_until_enable_ap_ar_detail_true"
    if object_name.startswith("v") and not bool(config.get("include_views", True)):
        return "skipped_include_views_false"
    if requires_flag and not bool(config.get(requires_flag, False)):
        return f"disabled_until_{requires_flag}_true"
    if not surface.get("enabled", True) and not gate_enabled:
        return "disabled_in_config"
    return None


def require_columns(surface):
    columns = list(surface.get("columns") or [])
    if not columns:
        raise ValueError(f"{surface['surface_id']} must define an explicit column allowlist")
    if any(str(column).strip() == "*" for column in columns):
        raise ValueError(f"{surface['surface_id']} column allowlist must not include *")
    return columns


def validate_columns_against_inventory(surface, column_names, columns_by_surface, field_name):
    if columns_by_surface is None:
        return
    surface_id = surface["surface_id"]
    available_columns = columns_by_surface.get(surface_id)
    if available_columns is None:
        raise ValueError(f"{surface_id} is missing from supplied column_inventory")
    missing_columns = [
        column_name
        for column_name in column_names
        if normalize_identifier(column_name) not in available_columns
    ]
    if missing_columns:
        raise ValueError(
            f"{surface_id} configured {field_name} includes column(s) absent from supplied "
            f"column_inventory: {', '.join(missing_columns)}"
        )


def columns_grouped_by_surface(column_inventory):
    grouped = {}
    for column in list(column_inventory or []):
        schema_name = str(column.get("object_schema") or column.get("schema_name") or "dbo")
        object_name = str(column.get("object_name") or "")
        column_name = str(column.get("column_name") or "")
        if not object_name or not column_name:
            continue
        surface_id = make_surface_id(schema_name, object_name)
        grouped.setdefault(surface_id, set()).add(normalize_identifier(column_name))
    return grouped


def normalize_max_rows(value):
    if value in (None, ""):
        return None
    max_rows = int(value)
    if max_rows <= 0:
        raise ValueError("max_rows must be a positive integer or null")
    return max_rows


def write_csv(path, rows, columns):
    rows = list(rows)
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


def create_source(config):
    env_name = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if env_name != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    connection_string = os.environ.get(env_name)
    if not connection_string:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return SqlServerSelectedSurfaceExtractSource(connection_string, database_name=config.get("database_name", ""))


class SqlServerSelectedSurfaceExtractSource:
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

    def fetch_rows(self, surface_spec):
        return self.query(surface_spec["sql"])

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before selected surface extraction") from exc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            if self.database_name:
                cursor.execute(f"USE {quote_identifier(self.database_name)}")
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, record)) for record in cursor.fetchall()]


def render_report(manifest):
    lines = [
        "# Selected Surface Raw Snapshot Extract Report",
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
            f"- Data maturity: {manifest['data_maturity']}",
            f"- Business reconciliation: {manifest['business_reconciliation_status']}",
            f"- Source of truth: {manifest['source_of_truth_status']}",
            f"- Decision: {manifest['decision']}",
            f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
            "",
            "## Exported Surfaces",
            "",
        ]
    )
    for surface in manifest["selected_surfaces_exported"]:
        lines.append(
            f"- `{surface['surface_id']}`: {surface['row_count']} rows -> {surface['output_file']}"
        )
    if not manifest["selected_surfaces_exported"]:
        lines.append("- None")
    lines.extend(["", "## Skipped Surfaces", ""])
    for surface in manifest["skipped_surfaces"]:
        lines.append(f"- `{surface['surface_id']}`: {surface['reason']}")
    lines.extend(
        [
            "",
            "## Handling",
            "",
            "- Keep raw CSVs and this run folder local under the configured output root.",
            "- Do not paste raw ERP rows, generated CSV contents, local config, screenshots, or connection strings.",
            "- Use this only as a current AC2 database snapshot for later reconciliation planning.",
        ]
    )
    return "\n".join(lines) + "\n"


def extract_sql_selected_columns(sql):
    match = re.search(r"\bSELECT\s+(?:TOP\s+\(\d+\)\s+)?(.+?)\s+FROM\b", sql, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    select_list = match.group(1)
    return [value.replace("]]", "]") for value in re.findall(r"\[((?:[^\]]|\]\])*)\]", select_list)]


def make_surface_id(schema_name, object_name):
    return f"{schema_name}.{object_name}"


def quote_identifier(value):
    escaped = str(value).replace("]", "]]")
    return f"[{escaped}]"


def normalize_identifier(value):
    return str(value).strip().casefold()


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).replace(".", "_")


def sanitize_context(context):
    return {str(key): sanitize_text(value) for key, value in dict(context or {}).items()}


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
        description="Export selected AutoCount read-only surfaces into a local raw snapshot folder."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free extractor config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_extraction(load_config(args.config), output_root=args.output_root)
        print(json.dumps({"status": manifest["status"], "run_path": manifest["storage"]["run_path"]}, indent=2))
        return 0 if manifest["status"] == "success" else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
