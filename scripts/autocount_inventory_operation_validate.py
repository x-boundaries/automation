import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


DEFAULT_CONFIG_PATH = Path("config/autocount_inventory_operation_validate.example.json")
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\probe\inventory_operations_validation"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"

BASE_WARNINGS = [
    "This is read-only, aggregate-only validation and does not approve extraction.",
    "No raw ERP/business rows, sample rows, top-N values, or distinct values are exported.",
    "Every selected surface remains Needs reconciliation.",
    "No final production mapping is selected.",
]

DEFAULT_SELECTED_SURFACES = [
    {
        "surface_name": "grn_header_vGoodsReceivedNote",
        "business_function": "grn_header",
        "schema_name": "dbo",
        "object_name": "vGoodsReceivedNote",
        "expected_columns": ["DocNo", "DocDate", "CreditorCode"],
        "critical_columns": ["DocNo"],
        "safe_key_columns": ["DocNo", "DocKey", "CreditorCode"],
        "safe_date_columns": ["DocDate", "LastModified"],
        "critical": True,
    },
    {
        "surface_name": "grn_header_GR",
        "business_function": "grn_header",
        "schema_name": "dbo",
        "object_name": "GR",
        "expected_columns": ["DocNo", "DocDate", "CreditorCode"],
        "critical_columns": ["DocNo"],
        "safe_key_columns": ["DocNo", "DocKey", "CreditorCode"],
        "safe_date_columns": ["DocDate", "LastModified"],
        "critical": True,
    },
    {
        "surface_name": "grn_detail_vGoodsReceivedNoteDetail",
        "business_function": "grn_detail",
        "schema_name": "dbo",
        "object_name": "vGoodsReceivedNoteDetail",
        "expected_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "critical_columns": ["ItemCode"],
        "safe_key_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "grn_detail_vGoodsReceivedNoteSubDetail",
        "business_function": "grn_detail",
        "schema_name": "dbo",
        "object_name": "vGoodsReceivedNoteSubDetail",
        "expected_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode", "BatchBalQty"],
        "critical_columns": ["ItemCode"],
        "safe_key_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "grn_detail_GRDTL",
        "business_function": "grn_detail",
        "schema_name": "dbo",
        "object_name": "GRDTL",
        "expected_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "critical_columns": ["ItemCode"],
        "safe_key_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "stock_receive_vStockReceive",
        "business_function": "stock_receive",
        "schema_name": "dbo",
        "object_name": "vStockReceive",
        "expected_columns": ["DocNo", "DocDate"],
        "critical_columns": ["DocNo"],
        "safe_key_columns": ["DocNo", "DocKey"],
        "safe_date_columns": ["DocDate", "LastModified"],
    },
    {
        "surface_name": "stock_receive_vStockReceiveDetail",
        "business_function": "stock_receive",
        "schema_name": "dbo",
        "object_name": "vStockReceiveDetail",
        "expected_columns": ["DocNo", "ItemCode", "SmallestQty"],
        "critical_columns": ["ItemCode"],
        "safe_key_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "transfer_header_vStockTransfer",
        "business_function": "transfer_header",
        "schema_name": "dbo",
        "object_name": "vStockTransfer",
        "expected_columns": [
            "DocNo",
            "DocDate",
            "FromLocation",
            "ToLocation",
            "XFERUDF_GIT",
            "XFERUDF_RcvDate",
            "XFERUDF_RcvBy",
            "XFERUDF_UseGIT",
        ],
        "critical_columns": ["DocNo"],
        "safe_key_columns": ["DocNo", "DocKey", "FromLocation", "ToLocation"],
        "safe_date_columns": ["DocDate", "LastModified", "XFERUDF_RcvDate"],
        "critical": True,
    },
    {
        "surface_name": "transfer_header_XFER",
        "business_function": "transfer_header",
        "schema_name": "dbo",
        "object_name": "XFER",
        "expected_columns": ["DocNo", "DocDate", "FromLocation", "ToLocation"],
        "critical_columns": ["DocNo"],
        "safe_key_columns": ["DocNo", "DocKey", "FromLocation", "ToLocation"],
        "safe_date_columns": ["DocDate", "LastModified"],
        "critical": True,
    },
    {
        "surface_name": "transfer_detail_vStockTransferDetail",
        "business_function": "transfer_detail",
        "schema_name": "dbo",
        "object_name": "vStockTransferDetail",
        "expected_columns": ["DocNo", "ItemCode", "SmallestQty"],
        "critical_columns": ["ItemCode"],
        "safe_key_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "outstanding_po_vPurchaseOrder",
        "business_function": "outstanding_po_in_transit",
        "schema_name": "dbo",
        "object_name": "vPurchaseOrder",
        "expected_columns": ["DocNo", "DocDate", "CreditorCode", "PurchaseLocation"],
        "critical_columns": ["DocNo"],
        "safe_key_columns": ["DocNo", "DocKey", "CreditorCode", "PurchaseLocation"],
        "safe_date_columns": ["DocDate", "LastModified"],
    },
    {
        "surface_name": "outstanding_po_PO",
        "business_function": "outstanding_po_in_transit",
        "schema_name": "dbo",
        "object_name": "PO",
        "expected_columns": ["DocNo", "DocDate", "CreditorCode", "PurchaseLocation"],
        "critical_columns": ["DocNo"],
        "safe_key_columns": ["DocNo", "DocKey", "CreditorCode", "PurchaseLocation"],
        "safe_date_columns": ["DocDate", "LastModified"],
    },
    {
        "surface_name": "outstanding_po_PODTL",
        "business_function": "outstanding_po_in_transit",
        "schema_name": "dbo",
        "object_name": "PODTL",
        "expected_columns": ["DocNo", "DtlKey", "ItemCode", "TransferedQty", "PostToStock"],
        "critical_columns": ["ItemCode"],
        "safe_key_columns": ["DocNo", "DocKey", "DtlKey", "ItemCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "supplier_context_vCreditor",
        "business_function": "supplier_context",
        "schema_name": "dbo",
        "object_name": "vCreditor",
        "expected_columns": ["CreditorCode"],
        "critical_columns": ["CreditorCode"],
        "safe_key_columns": ["CreditorCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "supplier_context_Creditor",
        "business_function": "supplier_context",
        "schema_name": "dbo",
        "object_name": "Creditor",
        "expected_columns": ["CreditorCode"],
        "critical_columns": ["CreditorCode"],
        "safe_key_columns": ["CreditorCode"],
        "safe_date_columns": ["LastModified"],
    },
    {
        "surface_name": "stock_movement_StockDTL",
        "business_function": "movement_stock_reference",
        "schema_name": "dbo",
        "object_name": "StockDTL",
        "expected_columns": ["DocType", "DocNo", "ItemCode"],
        "critical_columns": ["DocNo", "ItemCode"],
        "safe_key_columns": ["DocType", "DocNo", "ItemCode"],
        "safe_date_columns": ["DocDate", "LastModified"],
        "optional": True,
    },
    {
        "surface_name": "stock_balance_vItemBalQty",
        "business_function": "movement_stock_reference",
        "schema_name": "dbo",
        "object_name": "vItemBalQty",
        "expected_columns": ["ItemCode", "LocationBalQty"],
        "critical_columns": ["ItemCode"],
        "safe_key_columns": ["ItemCode"],
        "safe_date_columns": ["LastModified"],
        "optional": True,
    },
]

SECRET_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+"
)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_validation(config, source=None, output_root=None, now=None):
    plan = build_run_plan(config, output_root=output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    warnings = list(BASE_WARNINGS)
    exceptions = []
    context = {}
    results = []
    status = "success"
    selected_surfaces = normalize_selected_surfaces(config.get("selected_surfaces") or DEFAULT_SELECTED_SURFACES)

    try:
        active_source = source or create_source(config)
        context = sanitize_context(active_source.fetch_context())
        validate_target_context(context, config)
        inventory = sanitize_surface_inventory(active_source.fetch_surface_inventory(selected_surfaces))
        columns = sanitize_column_inventory(active_source.fetch_columns(selected_surfaces))
        results, surface_warnings, surface_exceptions = validate_selected_surfaces(
            active_source,
            selected_surfaces,
            inventory,
            columns,
        )
        warnings.extend(surface_warnings)
        exceptions.extend(surface_exceptions)
        if surface_warnings or surface_exceptions:
            status = "success_with_warnings"
        if critical_surfaces_all_missing(selected_surfaces, results):
            status = "failed"
            warnings.append("all_critical_surfaces_missing")
    except Exception as exc:  # noqa: BLE001 - safe manifest should preserve redacted failure context.
        status = "failed"
        exceptions.append(sanitize_text(str(exc)))
        results = []

    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    manifest = normalize_for_json(
        {
            "job": config.get("job", "autocount_inventory_operation_validate"),
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
            "selected_surface_results": results,
            "summary": summarize_results(results),
            "warnings": unique_preserve_order(warnings),
            "exception_count": len(exceptions),
            "exceptions": exceptions,
            "storage": {
                "output_root": plan["output_root"],
                "run_path": plan["run_path"],
                "manifest": str(run_path / "inventory_operation_validation_manifest.json"),
                "report": str(run_path / "inventory_operation_validation_report.md"),
            },
            "notes": [
                "Use this aggregate-only validation before considering any selected raw snapshot extraction.",
                "Do not schedule extraction, write back to AC2, or build dashboards from this validation output.",
                "CoA, GL, bank opening balances, and accounting migration scope remain parked.",
            ],
        }
    )

    (run_path / "inventory_operation_validation_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (run_path / "inventory_operation_validation_report.md").write_text(render_report(manifest), encoding="utf-8")
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
            / f"inventory_operation_validation_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def validate_target_context(context, config):
    expected_database = str(config.get("expected_database", "AED_XBOUNDARIES"))
    expected_login = str(config.get("expected_login", "xb_ac2_readonly"))
    rejected_databases = {str(value) for value in config.get("reject_databases", ["AED_XBoundaries", "A893478"])}
    rejected_server_patterns = [str(value) for value in config.get("reject_server_patterns", [r"SQLEXPRESS"])]

    server_name = str(context.get("server_name", ""))
    current_database = str(context.get("current_database", ""))
    current_login = str(context.get("current_login", ""))
    current_user_name = str(context.get("current_user_name", ""))
    errors = []

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


def validate_selected_surfaces(source, selected_surfaces, inventory, columns):
    inventory_by_id = {record["object_id"]: record for record in inventory}
    columns_by_id = columns_grouped_by_surface(columns)
    results = []
    warnings = []
    exceptions = []

    for surface in selected_surfaces:
        object_id = make_surface_id(surface["schema_name"], surface["object_name"])
        object_record = inventory_by_id.get(object_id)
        available_columns = columns_by_id.get(object_id, set())
        result = base_surface_result(surface, object_record, available_columns)
        if not object_record:
            warnings.append(f"surface_missing:{object_id}")
            results.append(result)
            continue

        aggregate_spec = build_aggregate_spec(surface, available_columns)
        try:
            raw_values = source.fetch_surface_aggregates(surface, aggregate_spec)
            aggregate_values = sanitize_aggregate_values(raw_values)
            apply_aggregate_values(result, aggregate_values, surface)
        except PermissionError as exc:
            result["readable"] = False
            result["aggregate_status"] = "unreadable"
            warnings.append(f"surface_unreadable:{object_id}")
            exceptions.append(sanitize_text(str(exc)))
        except Exception as exc:  # noqa: BLE001 - one surface should not stop all validation.
            result["aggregate_status"] = "failed"
            warnings.append(f"aggregate_failed:{object_id}")
            exceptions.append(sanitize_text(str(exc)))

        results.append(result)

    return results, warnings, exceptions


def base_surface_result(surface, object_record, available_columns):
    available_column_names = {normalize_keyword(column) for column in available_columns}
    expected_columns = [sanitize_text(column) for column in surface.get("expected_columns", [])]
    present = [column for column in expected_columns if normalize_keyword(column) in available_column_names]
    missing = [column for column in expected_columns if normalize_keyword(column) not in available_column_names]
    exists = object_record is not None
    return {
        "surface_name": sanitize_text(surface.get("surface_name", "")),
        "business_function": sanitize_text(surface.get("business_function", "")),
        "object_id": make_surface_id(surface.get("schema_name"), surface.get("object_name")),
        "schema_name": sanitize_text(surface.get("schema_name", "")),
        "object_name": sanitize_text(surface.get("object_name", "")),
        "object_type": object_record.get("object_type") if object_record else None,
        "exists": bool(exists),
        "readable": bool(exists),
        "aggregate_status": "pending" if exists else "missing",
        "row_count": None,
        "expected_columns_present": present,
        "missing_expected_columns": missing,
        "blank_counts": {},
        "distinct_counts": {},
        "date_ranges": {},
        "consistency_checks": build_consistency_checks(surface, exists, present, missing),
        "decision": NEEDS_RECONCILIATION,
        "final_production_selected": False,
    }


def build_consistency_checks(surface, exists, present_columns, missing_columns):
    return [
        {
            "check": "surface_exists",
            "passed": bool(exists),
        },
        {
            "check": "expected_columns_present",
            "passed": not missing_columns,
            "present_count": len(present_columns),
            "missing_count": len(missing_columns),
        },
        {
            "check": "future_join_keys_present",
            "passed": all(
                normalize_keyword(column) in {normalize_keyword(present) for present in present_columns}
                for column in surface.get("critical_columns", [])
            ),
            "critical_columns": [sanitize_text(column) for column in surface.get("critical_columns", [])],
        },
    ]


def build_aggregate_spec(surface, available_columns):
    normalized_available = {normalize_keyword(column) for column in available_columns}
    return {
        "sql": build_aggregate_sql(surface, normalized_available),
        "critical_columns": columns_present(surface.get("critical_columns", []), normalized_available),
        "safe_key_columns": columns_present(surface.get("safe_key_columns", []), normalized_available),
        "safe_date_columns": columns_present(surface.get("safe_date_columns", []), normalized_available),
    }


def build_aggregate_sql(surface, available_columns):
    normalized_available = {normalize_keyword(column) for column in available_columns}
    select_parts = ["COUNT_BIG(1) AS row_count"]

    for column in columns_present(surface.get("critical_columns", []), normalized_available):
        quoted = quote_identifier(column)
        alias = metric_alias("blank", column)
        select_parts.append(
            f"SUM(CASE WHEN {quoted} IS NULL OR "
            f"LTRIM(RTRIM(TRY_CONVERT(nvarchar(4000), {quoted}))) = '' THEN 1 ELSE 0 END) AS {alias}"
        )

    for column in columns_present(surface.get("safe_key_columns", []), normalized_available):
        select_parts.append(f"COUNT(DISTINCT {quote_identifier(column)}) AS {metric_alias('distinct', column)}")

    for column in columns_present(surface.get("safe_date_columns", []), normalized_available):
        quoted = quote_identifier(column)
        select_parts.append(f"MIN({quoted}) AS {metric_alias('min', column)}")
        select_parts.append(f"MAX({quoted}) AS {metric_alias('max', column)}")

    return (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {quote_identifier(surface.get('schema_name', 'dbo'))}.{quote_identifier(surface.get('object_name', ''))}"
    )


def columns_present(columns, normalized_available):
    return [str(column) for column in columns if normalize_keyword(column) in normalized_available]


def apply_aggregate_values(result, aggregate_values, surface):
    result["aggregate_status"] = "success"
    result["row_count"] = aggregate_values.get("row_count")
    expected_keys = {
        "row_count",
        *[metric_alias("blank", column) for column in surface.get("critical_columns", [])],
        *[metric_alias("distinct", column) for column in surface.get("safe_key_columns", [])],
        *[metric_alias("min", column) for column in surface.get("safe_date_columns", [])],
        *[metric_alias("max", column) for column in surface.get("safe_date_columns", [])],
    }
    for key, value in aggregate_values.items():
        if key not in expected_keys:
            continue
        if key.startswith("blank_"):
            result["blank_counts"][key.removeprefix("blank_")] = value
        elif key.startswith("distinct_"):
            result["distinct_counts"][key.removeprefix("distinct_")] = value
        elif key.startswith("min_"):
            column = key.removeprefix("min_")
            result["date_ranges"].setdefault(column, {})["min"] = value
        elif key.startswith("max_"):
            column = key.removeprefix("max_")
            result["date_ranges"].setdefault(column, {})["max"] = value


def critical_surfaces_all_missing(selected_surfaces, results):
    critical_ids = {
        make_surface_id(surface.get("schema_name"), surface.get("object_name"))
        for surface in selected_surfaces
        if surface.get("critical")
    }
    if not critical_ids:
        return False
    existing_ids = {result["object_id"] for result in results if result.get("exists")}
    return critical_ids.isdisjoint(existing_ids)


def summarize_results(results):
    return {
        "selected_surface_count": len(results),
        "existing_surface_count": sum(1 for result in results if result.get("exists")),
        "readable_surface_count": sum(1 for result in results if result.get("readable")),
        "missing_surface_count": sum(1 for result in results if not result.get("exists")),
        "failed_aggregate_count": sum(1 for result in results if result.get("aggregate_status") == "failed"),
    }


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
                "critical_columns": [sanitize_text(column) for column in surface.get("critical_columns", [])],
                "safe_key_columns": [sanitize_text(column) for column in surface.get("safe_key_columns", [])],
                "safe_date_columns": [sanitize_text(column) for column in surface.get("safe_date_columns", [])],
                "critical": bool(surface.get("critical", False)),
                "optional": bool(surface.get("optional", False)),
            }
        )
    return surfaces


def sanitize_surface_inventory(records):
    safe = []
    for record in records:
        schema_name = sanitize_text(record.get("object_schema", record.get("schema_name", "")))
        object_name = sanitize_text(record.get("object_name", ""))
        safe.append(
            {
                "object_id": make_surface_id(schema_name, object_name),
                "object_schema": schema_name,
                "object_name": object_name,
                "object_type": sanitize_text(record.get("object_type", "")),
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


def columns_grouped_by_surface(columns):
    grouped = {}
    for column in columns:
        object_id = column["object_id"]
        grouped.setdefault(object_id, set()).add(column.get("column_name", ""))
    return grouped


def sanitize_aggregate_values(values):
    return {
        metric_alias("", sanitize_text(key)).lstrip("_"): normalize_for_json(value)
        for key, value in dict(values or {}).items()
    }


def sanitize_context(context):
    return {str(key): sanitize_text(value) for key, value in dict(context or {}).items()}


def render_report(manifest):
    lines = [
        "# Inventory Operation Aggregate Validation Report",
        "",
        "This report is read-only and aggregate-only. It does not include raw ERP/business rows.",
        "",
        "## Run Context",
        "",
        f"- Status: {manifest['status']}",
        f"- Run ID: {manifest['run_id']}",
        f"- Run path: {manifest['storage']['run_path']}",
        f"- Server: {manifest.get('context', {}).get('server_name', '')}",
        f"- Database: {manifest.get('context', {}).get('current_database', '')}",
        f"- Login/User: {manifest.get('context', {}).get('current_login', '')}"
        f"/{manifest.get('context', {}).get('current_user_name', '')}",
        f"- Decision: {manifest['decision']}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        "",
        "## Warnings",
        "",
    ]
    for warning in manifest["warnings"]:
        lines.append(f"- {warning}")

    lines.extend(["", "## Selected Surface Results", ""])
    for result in manifest["selected_surface_results"]:
        lines.append(f"### `{result['object_id']}`")
        lines.append(f"- Business function: {result['business_function']}")
        lines.append(f"- Exists: {str(result['exists']).lower()}")
        lines.append(f"- Readable: {str(result['readable']).lower()}")
        lines.append(f"- Object type: {result.get('object_type')}")
        lines.append(f"- Aggregate status: {result['aggregate_status']}")
        lines.append(f"- Row count: {result.get('row_count')}")
        lines.append(f"- Expected columns present: {len(result.get('expected_columns_present', []))}")
        lines.append(f"- Missing expected columns: {', '.join(result.get('missing_expected_columns', [])) or 'none'}")
        if result.get("blank_counts"):
            lines.append(f"- Blank-count metrics: {', '.join(sorted(result['blank_counts']))}")
        if result.get("distinct_counts"):
            lines.append(f"- Distinct-count metrics: {', '.join(sorted(result['distinct_counts']))}")
        if result.get("date_ranges"):
            lines.append(f"- Date-range metrics: {', '.join(sorted(result['date_ranges']))}")
        lines.append(f"- Decision: {result['decision']}")
        lines.append(f"- Final production selected: {str(result['final_production_selected']).lower()}")
        lines.append("")

    lines.extend(
        [
            "## Follow-Up",
            "",
            "- Run this locally on the AC2 VM and compare aggregate counts with AutoCount UI/report expectations.",
            "- Use the results to decide whether selected raw snapshot extraction is safe to design next.",
            "- Do not schedule extraction, write back to AC2, or start dashboard/warehouse build from this report.",
        ]
    )
    if manifest["exceptions"]:
        lines.extend(["", "## Exceptions", ""])
        for exception in manifest["exceptions"]:
            lines.append(f"- {sanitize_text(exception)}")
    return "\n".join(lines) + "\n"


def create_source(config):
    env_name = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    if env_name != DEFAULT_CONNECTION_STRING_ENV:
        raise ValueError(f"connection_string_env must be {DEFAULT_CONNECTION_STRING_ENV}")
    connection_string = os.environ.get(env_name)
    if not connection_string:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return SqlServerInventoryOperationValidationSource(connection_string)


class SqlServerInventoryOperationValidationSource:
    def __init__(self, connection_string):
        self.connection_string = connection_string

    def fetch_context(self):
        result = self.query(
            "SELECT @@SERVERNAME AS server_name, DB_NAME() AS current_database, "
            "SUSER_SNAME() AS current_login, USER_NAME() AS current_user_name"
        )
        return result[0] if result else {}

    def fetch_surface_inventory(self, selected_surfaces):
        clause, params = build_surface_filter("s.name", "o.name", selected_surfaces)
        if not clause:
            return []
        return self.query(
            "SELECT s.name AS object_schema, o.name AS object_name, o.type_desc AS object_type "
            "FROM sys.objects AS o INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
            f"WHERE o.type IN ('U', 'V') AND ({clause})",
            params,
        )

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

    def fetch_surface_aggregates(self, surface, aggregate_spec):
        result = self.query(aggregate_spec["sql"])
        return result[0] if result else {}

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before inventory operation validation") from exc

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


def metric_alias(prefix, column_name):
    safe_column = re.sub(r"[^A-Za-z0-9_]+", "_", str(column_name)).strip("_")
    if not prefix:
        return safe_column
    return f"{prefix}_{safe_column}"


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
        description="Run aggregate-only validation for selected inventory operation AutoCount surfaces."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free validation config JSON.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_validation(load_config(args.config), output_root=args.output_root)
        print(json.dumps({"status": manifest["status"], "run_path": manifest["storage"]["run_path"]}, indent=2))
        return 0 if manifest["status"] in {"success", "success_with_warnings"} else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
