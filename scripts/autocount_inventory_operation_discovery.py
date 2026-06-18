import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


DEFAULT_CONFIG_PATH = Path("config/autocount_inventory_operation_discovery.example.json")
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\probe\inventory_operations"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"

KNOWN_CONFIRMED_SURFACES = {
    "dbo.PO",
    "dbo.PODTL",
    "dbo.vPurchaseOrder",
    "dbo.Debtor",
    "dbo.vDebtor",
    "dbo.Creditor",
    "dbo.vCreditor",
    "dbo.PaymentMethod",
    "dbo.ARInvoice",
    "dbo.APInvoice",
}

PARKED_SCOPE_OBJECT_PATTERNS = [
    r"^GL",
    r"GLDTL",
    r"^Bank",
    r"CashBook",
    r"Account",
    r"ARInvoice",
    r"APInvoice",
]

DEFAULT_CANDIDATE_FAMILIES = {
    "grn_receiving": {
        "label": "GRN / goods receiving",
        "keyword_hints": [
            "GRN",
            "GoodsReceive",
            "GoodsReceived",
            "GoodsReceipt",
            "Receive",
            "Receipt",
            "PurchaseReceive",
            "StockReceive",
        ],
        "object_aliases": {"GR": "GRN", "GRDTL": "GRN"},
        "evidence_columns": ["DocNo", "DocDate", "CreditorCode", "Supplier", "ItemCode", "Qty"],
    },
    "stock_transfer": {
        "label": "Stock transfer / inter-location movement",
        "keyword_hints": ["Transfer", "StockTransfer", "LocationTransfer", "Xfer", "FromLocation", "ToLocation"],
        "evidence_columns": ["DocNo", "DocDate", "FromLocation", "ToLocation", "ItemCode", "Qty"],
    },
    "stock_location": {
        "label": "Stock location / location master",
        "keyword_hints": ["Location", "StockLocation", "LocationCode", "Warehouse", "WH", "Branch"],
        "evidence_columns": ["Location", "LocationCode", "Warehouse", "BranchCode", "Description"],
    },
    "item_product_attributes": {
        "label": "Richer item/product attributes",
        "keyword_hints": [
            "Item",
            "ItemCode",
            "UOM",
            "Barcode",
            "BarCode",
            "Brand",
            "Category",
            "Class",
            "Group",
            "LeadTime",
            "Supplier",
            "Creditor",
        ],
        "evidence_columns": ["ItemCode", "UOM", "Barcode", "BarCode", "Brand", "Category", "Class", "Group"],
    },
    "movement_semantics": {
        "label": "Stock movement document type semantics",
        "keyword_hints": [
            "DocType",
            "DocNo",
            "DocDate",
            "SourceType",
            "Ref",
            "Reference",
            "Module",
            "TransType",
            "MovementType",
            "Qty",
            "InQty",
            "OutQty",
            "BalanceQty",
        ],
        "evidence_columns": [
            "DocType",
            "DocNo",
            "DocDate",
            "SourceType",
            "TransType",
            "MovementType",
            "Qty",
            "InQty",
            "OutQty",
            "BalanceQty",
        ],
        "required_object_keywords_any": ["Stock", "Item", "Transfer", "GR", "Receive", "PO", "Purchase"],
    },
    "purchasing_supplier_context": {
        "label": "Supplier lead-time or purchasing context",
        "keyword_hints": [
            "Supplier",
            "Creditor",
            "LeadTime",
            "Delivery",
            "ETA",
            "Purchase",
            "PO",
            "Outstanding",
            "TransferedQty",
            "BackOrder",
        ],
        "evidence_columns": ["Supplier", "CreditorCode", "LeadTime", "Delivery", "ETA", "Purchase", "BackOrder"],
    },
    "outstanding_po_transit_support": {
        "label": "Outstanding PO / stock-in-transit support",
        "keyword_hints": ["PO", "Purchase", "Outstanding", "TransferedQty", "BackOrder", "Transit", "InTransit"],
        "evidence_columns": ["DocNo", "CreditorCode", "ItemCode", "OutstandingQty", "TransferedQty", "BackOrder"],
        "known_surface_boosts": ["dbo.PO", "dbo.PODTL", "dbo.vPurchaseOrder"],
    },
}

SECRET_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+"
)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_discovery(config, source=None, output_root=None, include_row_counts=None, now=None):
    plan = build_run_plan(config, output_root=output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    families = normalize_candidate_families(config.get("candidate_families") or DEFAULT_CANDIDATE_FAMILIES)
    include_counts = bool(config.get("include_row_counts", True) if include_row_counts is None else include_row_counts)
    context = {}
    objects = []
    columns = []
    row_counts = []
    candidates_by_family = {family_name: [] for family_name in families}
    exceptions = []
    status = "success"

    try:
        active_source = source or create_source(config)
        context = sanitize_context(active_source.fetch_context())
        validate_target_context(context, config)
        objects = sanitize_object_inventory(active_source.fetch_objects(plan["schemas"]))
        columns = sanitize_column_inventory(active_source.fetch_columns(plan["schemas"]))
        if include_counts:
            row_counts = sanitize_row_counts(active_source.fetch_row_counts(objects))
        candidates_by_family = score_inventory_candidates(objects, columns, row_counts, families)
    except Exception as exc:  # noqa: BLE001 - safe manifest should preserve redacted failure context.
        status = "failed"
        exceptions.append(sanitize_text(str(exc)))

    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    manifest = {
        "job": config.get("job", "autocount_inventory_operation_discovery"),
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "connection_string_env": plan["connection_string_env"],
        "context": context,
        "expected_target": {
            "server": config.get("expected_server", r"localhost\A2006"),
            "database": config.get("expected_database", "AED_XBOUNDARIES"),
            "login": config.get("expected_login", "xb_ac2_readonly"),
        },
        "data_maturity": DATA_MATURITY,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
        "candidate_families": {
            name: {
                "label": family["label"],
                "keyword_hints": list(family["keyword_hints"]),
                "candidate_count": len(candidates_by_family.get(name, [])),
                "decision": NEEDS_RECONCILIATION,
                "final_production_selected": False,
            }
            for name, family in families.items()
        },
        "candidates_by_family": candidates_by_family,
        "candidate_summary": summarize_candidates(candidates_by_family),
        "counts": {
            "objects_inspected": len(objects),
            "columns_inspected": len(columns),
            "row_count_records": len(row_counts),
        },
        "include_row_counts": include_counts,
        "known_confirmed_surfaces": sorted(KNOWN_CONFIRMED_SURFACES),
        "unresolved_inventory_operation_surfaces": [
            "GRN / goods receiving",
            "stock transfer / inter-location movement",
            "stock location / location master",
            "richer item/product attributes",
            "stock movement document type semantics",
            "supplier lead-time or purchasing context",
            "outstanding PO / stock-in-transit support beyond PO/PODTL/vPurchaseOrder",
        ],
        "parked_scope": [
            "CoA/account master",
            "GL opening balances",
            "bank opening balances",
            "full accounting cutover",
            "direct AC2 write-back",
            "scheduler/automation runs",
        ],
        "exceptions": exceptions,
        "storage": {
            "output_root": plan["output_root"],
            "run_path": plan["run_path"],
            "manifest": str(run_path / "inventory_operation_discovery_manifest.json"),
            "report": str(run_path / "inventory_operation_discovery_report.md"),
        },
        "warnings": [
            "This is metadata only and does not approve extraction.",
            "No raw ERP/business rows are queried or written by this discovery step.",
            "Every candidate remains Needs reconciliation.",
            "No final production mapping is selected.",
        ],
        "notes": [
            "Use this as an inventory-intelligence shortlist for local review only.",
            "Reconcile every candidate against AutoCount UI/report paths before extraction use.",
            "Do not schedule extraction, write back to AC2, or revive parked accounting migration scope from this output.",
        ],
    }

    manifest = normalize_for_json(manifest)
    manifest_path = run_path / "inventory_operation_discovery_manifest.json"
    report_path = run_path / "inventory_operation_discovery_report.md"
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
        "schemas": [str(schema) for schema in config.get("schemas", ["dbo"])],
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root / f"inventory_operation_discovery_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def normalize_candidate_families(value):
    normalized = {}
    for name, family in dict(value or {}).items():
        normalized[str(name)] = {
            "label": str(family.get("label") or name),
            "keyword_hints": [str(keyword) for keyword in family.get("keyword_hints", [])],
            "object_aliases": {str(key): str(val) for key, val in dict(family.get("object_aliases", {})).items()},
            "evidence_columns": [str(column) for column in family.get("evidence_columns", [])],
            "required_object_keywords_any": [
                str(keyword) for keyword in family.get("required_object_keywords_any", [])
            ],
            "known_surface_boosts": [str(surface_id) for surface_id in family.get("known_surface_boosts", [])],
        }
    return normalized


def validate_target_context(context, config):
    expected_database = str(config.get("expected_database", "AED_XBOUNDARIES"))
    expected_login = str(config.get("expected_login", "xb_ac2_readonly"))
    rejected_databases = {str(value) for value in config.get("reject_databases", ["AED_XBoundaries", "A893478"])}
    rejected_server_patterns = [
        str(value) for value in config.get("reject_server_patterns", [r"SQLEXPRESS"])
    ]
    errors = []

    server_name = str(context.get("server_name", ""))
    current_database = str(context.get("current_database", ""))
    current_login = str(context.get("current_login", ""))
    current_user_name = str(context.get("current_user_name", ""))

    for pattern in rejected_server_patterns:
        if pattern and re.search(pattern, server_name, flags=re.IGNORECASE):
            errors.append(f"Rejected SQL server target `{server_name}` matched `{pattern}`.")
    if current_database in rejected_databases:
        errors.append(f"Rejected SQL database target `{current_database}`.")
    if current_database != expected_database:
        errors.append(f"Expected database `{expected_database}` but connected to `{current_database}`.")
    if expected_login not in {current_login, current_user_name}:
        errors.append(
            f"Expected read-only login/user `{expected_login}` but connected as `{current_login}/{current_user_name}`."
        )
    if errors:
        raise ValueError(" ".join(errors))


def score_inventory_candidates(objects, columns, row_counts, families):
    columns_by_object = group_columns_by_object(columns)
    row_counts_by_object = {
        make_object_id(record.get("object_schema"), record.get("object_name")): record.get("row_count")
        for record in row_counts
    }
    result = {}
    for family_name, family in families.items():
        candidates = []
        for obj in objects:
            candidate = score_object_for_family(
                obj,
                columns_by_object.get(obj["object_id"], []),
                row_counts_by_object.get(obj["object_id"]),
                family_name,
                family,
            )
            if candidate:
                candidates.append(candidate)
        candidates.sort(key=lambda item: (-item["score"], item["schema_name"].lower(), item["object_name"].lower()))
        result[family_name] = [
            {**candidate, "rank": index}
            for index, candidate in enumerate(candidates[: int(family.get("top_n", 15) or 15)], start=1)
        ]
    return result


def score_object_for_family(obj, columns, row_count, family_name, family):
    object_name = obj.get("object_name", "")
    object_id = obj.get("object_id", make_object_id(obj.get("schema_name"), object_name))
    if is_parked_scope_object(object_name):
        return None

    required_object_keywords = family.get("required_object_keywords_any") or []
    if required_object_keywords and not any(keyword_matches_object(keyword, object_name) for keyword in required_object_keywords):
        return None

    score = 0
    reason_codes = []
    matched_column_names = []

    aliases = family.get("object_aliases") or {}
    alias_keyword = aliases.get(object_name)
    if alias_keyword:
        score += 45
        reason_codes.append(f"object_name_keyword:{alias_keyword}")

    for keyword in family["keyword_hints"]:
        if keyword_matches_object(keyword, object_name):
            score += 30
            reason_codes.append(f"object_name_keyword:{keyword}")

    evidence_columns = set(normalize_keyword(column) for column in family.get("evidence_columns", []))
    keyword_columns = set(normalize_keyword(keyword) for keyword in family.get("keyword_hints", []))
    for column in columns:
        column_name = column.get("column_name", "")
        normalized_column = normalize_keyword(column_name)
        if normalized_column in evidence_columns or any(keyword in normalized_column for keyword in keyword_columns):
            matched_column_names.append(column_name)
            score += 8
            reason_codes.append(f"column_name_keyword:{column_name}")

    known_boosts = set(family.get("known_surface_boosts") or [])
    if object_id in known_boosts or object_id in KNOWN_CONFIRMED_SURFACES and family_name == "outstanding_po_transit_support":
        score += 25
        reason_codes.append(f"known_confirmed_surface:{object_id}")

    if family_name == "stock_location" and object_name in {"Branch", "vBranch"} and row_count == 0:
        reason_codes.append("zero_row_location_candidate")

    if score <= 0:
        return None

    return {
        "object_id": object_id,
        "schema_name": obj.get("schema_name", ""),
        "object_name": object_name,
        "object_type": obj.get("object_type", ""),
        "row_count": row_count,
        "matched_keyword_family": family_name,
        "matched_column_names": unique_preserve_order(matched_column_names),
        "score": score,
        "reason_codes": unique_preserve_order(reason_codes),
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
    }


def summarize_candidates(candidates_by_family):
    counts_by_family = {family: len(candidates) for family, candidates in candidates_by_family.items()}
    return {
        "total_candidates": sum(counts_by_family.values()),
        "counts_by_family": counts_by_family,
    }


def is_parked_scope_object(object_name):
    return any(re.search(pattern, str(object_name), flags=re.IGNORECASE) for pattern in PARKED_SCOPE_OBJECT_PATTERNS)


def keyword_matches_object(keyword, object_name):
    normalized_keyword = normalize_keyword(keyword)
    normalized_object = normalize_keyword(object_name)
    if not normalized_keyword or not normalized_object:
        return False
    if normalized_keyword in normalized_object:
        return True
    return any(normalize_keyword(part) == normalized_keyword for part in split_name_tokens(object_name))


def group_columns_by_object(columns):
    grouped = {}
    for column in columns:
        grouped.setdefault(column["object_id"], []).append(column)
    return grouped


def sanitize_object_inventory(objects):
    safe = []
    for obj in objects:
        schema_name = sanitize_text(obj.get("schema_name", ""))
        object_name = sanitize_text(obj.get("object_name", ""))
        safe.append(
            {
                "object_id": make_object_id(schema_name, object_name),
                "schema_name": schema_name,
                "object_name": object_name,
                "object_type": sanitize_text(obj.get("object_type", "")),
            }
        )
    return safe


def sanitize_column_inventory(columns):
    safe = []
    for column in columns:
        schema_name = sanitize_text(column.get("object_schema", ""))
        object_name = sanitize_text(column.get("object_name", ""))
        safe.append(
            {
                "object_id": make_object_id(schema_name, object_name),
                "object_schema": schema_name,
                "object_name": object_name,
                "column_name": sanitize_text(column.get("column_name", "")),
                "data_type": sanitize_text(column.get("data_type", "")),
            }
        )
    return safe


def sanitize_row_counts(row_counts):
    safe = []
    for record in row_counts:
        safe.append(
            {
                "object_schema": sanitize_text(record.get("object_schema", "")),
                "object_name": sanitize_text(record.get("object_name", "")),
                "row_count": record.get("row_count", record.get("approximate_row_count")),
            }
        )
    return safe


def sanitize_context(context):
    return {str(key): sanitize_text(value) for key, value in dict(context or {}).items()}


def render_report(manifest):
    lines = [
        "# Inventory Operation Surface Discovery Report",
        "",
        "This report is generated from SQL Server metadata only. It does not query raw ERP/business rows.",
        "",
        "## Run",
        "",
        f"- Status: {manifest['status']}",
        f"- Run ID: {manifest['run_id']}",
        f"- Database: {manifest.get('context', {}).get('current_database', '')}",
        f"- Login/User: {manifest.get('context', {}).get('current_login', '')}"
        f"/{manifest.get('context', {}).get('current_user_name', '')}",
        f"- Decision: {manifest['decision']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Business reconciliation: {manifest['business_reconciliation_status']}",
        "",
        "## Candidate Families",
        "",
    ]
    for family_name, family in manifest["candidate_families"].items():
        lines.append(f"### {family['label']} (`{family_name}`)")
        lines.append(f"- Candidates: {family['candidate_count']}")
        lines.append(f"- Decision: {family['decision']}")
        candidates = manifest["candidates_by_family"].get(family_name, [])
        if not candidates:
            lines.append("- No metadata matches.")
        for candidate in candidates:
            lines.append(
                f"- #{candidate['rank']} `{candidate['object_id']}` ({candidate['object_type']}), "
                f"row count: {candidate.get('row_count')}, score: {candidate['score']}, "
                f"columns: {', '.join(candidate.get('matched_column_names', [])) or 'none'}"
            )
            lines.append(f"  - Reason codes: {', '.join(candidate.get('reason_codes', []))}")
        lines.append("")

    lines.extend(
        [
            "## Required Follow-Up",
            "",
            "- Reconcile shortlisted candidates against AutoCount UI/report outputs before extraction use.",
            "- Use results to update the next extraction profile only after review.",
            "- Do not schedule extraction, write back to AC2, or revive CoA/GL/bank migration scope from this report.",
        ]
    )
    if manifest["exceptions"]:
        lines.extend(["", "## Exceptions", ""])
        for exception in manifest["exceptions"]:
            lines.append(f"- {sanitize_text(exception)}")
    return "\n".join(lines) + "\n"


def build_metadata_sql_definitions():
    return [
        {
            "name": "context",
            "sql": (
                "SELECT @@SERVERNAME AS server_name, DB_NAME() AS current_database, "
                "SUSER_SNAME() AS current_login, USER_NAME() AS current_user_name"
            ),
        },
        {
            "name": "object_inventory",
            "sql": (
                "SELECT s.name AS schema_name, o.name AS object_name, o.type_desc AS object_type "
                "FROM sys.objects AS o INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
                "WHERE o.type IN ('U', 'V')"
            ),
        },
        {
            "name": "column_inventory",
            "sql": (
                "SELECT s.name AS object_schema, o.name AS object_name, c.name AS column_name, "
                "t.name AS data_type FROM sys.columns AS c "
                "INNER JOIN sys.objects AS o ON o.object_id = c.object_id "
                "INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
                "INNER JOIN sys.types AS t ON t.user_type_id = c.user_type_id "
                "WHERE o.type IN ('U', 'V')"
            ),
        },
        {
            "name": "row_counts",
            "sql": (
                "SELECT s.name AS object_schema, o.name AS object_name, SUM(p.row_count) AS row_count "
                "FROM sys.dm_db_partition_stats AS p "
                "INNER JOIN sys.objects AS o ON o.object_id = p.object_id "
                "INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
                "WHERE p.index_id IN (0, 1) AND o.type IN ('U', 'V') "
                "GROUP BY s.name, o.name"
            ),
        },
    ]


def create_source(config):
    env_name = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if env_name != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    connection_string = os.environ.get(env_name)
    if not connection_string:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return SqlServerInventoryOperationDiscoverySource(connection_string)


class SqlServerInventoryOperationDiscoverySource:
    def __init__(self, connection_string):
        self.connection_string = connection_string

    def fetch_context(self):
        return self.query(query_by_name("context"))[0]

    def fetch_objects(self, schemas=None):
        return self.query(add_schema_filter(query_by_name("object_inventory"), "s.name", schemas))

    def fetch_columns(self, schemas=None):
        return self.query(add_schema_filter(query_by_name("column_inventory"), "s.name", schemas))

    def fetch_row_counts(self, objects):
        return self.query(query_by_name("row_counts"))

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before inventory operation discovery") from exc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, record)) for record in cursor.fetchall()]


def query_by_name(name):
    for definition in build_metadata_sql_definitions():
        if definition["name"] == name:
            return definition["sql"]
    raise KeyError(name)


def add_schema_filter(sql, schema_column, schemas):
    schemas = [str(schema) for schema in list(schemas or []) if str(schema)]
    if not schemas:
        return sql
    quoted = ", ".join("'" + schema.replace("'", "''") + "'" for schema in schemas)
    return f"{sql} AND {schema_column} IN ({quoted})"


def make_object_id(schema_name, object_name):
    return f"{schema_name}.{object_name}"


def normalize_keyword(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def split_name_tokens(value):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    return [part for part in re.split(r"[^A-Za-z0-9]+", spaced) if part]


def unique_preserve_order(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


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
        description="Discover inventory-operation candidate AC2 SQL surfaces from metadata only."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free discovery config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    parser.add_argument(
        "--no-row-counts",
        action="store_true",
        help="Skip metadata row counts if the read-only login cannot access partition stats.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_config(args.config)
        manifest = run_discovery(
            config,
            output_root=args.output_root,
            include_row_counts=False if args.no_row_counts else None,
        )
        print(json.dumps({"status": manifest["status"], "run_path": manifest["storage"]["run_path"]}, indent=2))
        return 0 if manifest["status"] == "success" else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
