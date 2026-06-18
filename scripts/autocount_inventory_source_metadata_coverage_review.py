import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_source_metadata_coverage_review"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"
BLOCKED_RECOMMENDATION = "no_dashboard_until_schema_gap_resolved"
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"

DOCNO_COLUMNS = ["DocNo", "DocKey"]
QUANTITY_LIKE_COLUMNS = [
    "Qty",
    "Quantity",
    "BaseQty",
    "SmallestQty",
    "ReceiveQty",
    "TransferQty",
    "TransferredQty",
    "TransferedQty",
    "BatchBalQty",
    "BalQty",
]


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_metadata_coverage_review(manifest_path, output_root=None, now=None, source=None):
    plan = build_run_plan(output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    manifest_path = Path(manifest_path)
    source_manifest = {}
    source_mapping_run_path = manifest_path.parent.resolve(strict=False)
    staging_run_path = source_mapping_run_path
    exceptions = []
    source_metadata = {}

    try:
        source_manifest = load_manifest(manifest_path)
        validate_source_safety(source_manifest, exceptions)
        source_mapping_run_path = resolve_manifest_run_path(manifest_path, source_manifest)
        staging_run_path = resolve_staging_run_path(source_manifest, source_mapping_run_path)
        source_metadata = source.fetch_metadata() if source else collect_source_metadata(staging_run_path, exceptions)
    except Exception as exc:  # noqa: BLE001 - local review writes redacted failure output.
        exceptions.append(sanitize_text(str(exc)))

    mapping_gaps = collect_mapping_gaps(source_manifest)
    surface_column_inventory = build_surface_column_inventory(source_metadata)
    docno_doc_key_candidates = build_docno_doc_key_candidates(surface_column_inventory)
    quantity_column_candidates = build_quantity_column_candidates(surface_column_inventory)
    unresolved_metadata_gaps = build_unresolved_metadata_gaps(
        mapping_gaps,
        docno_doc_key_candidates,
        quantity_column_candidates,
    )
    status = "failed" if exceptions else "success_with_warnings" if unresolved_metadata_gaps else "success"

    review_manifest = build_review_manifest(
        source_manifest=source_manifest,
        plan=plan,
        status=status,
        source_mapping_run_path=source_mapping_run_path,
        staging_run_path=staging_run_path,
        surface_column_inventory=surface_column_inventory,
        docno_doc_key_candidates=docno_doc_key_candidates,
        quantity_column_candidates=quantity_column_candidates,
        unresolved_metadata_gaps=unresolved_metadata_gaps,
        exceptions=exceptions,
        now=now,
    )
    manifest_out = run_path / "inventory_source_metadata_coverage_review_manifest.json"
    report_out = run_path / "inventory_source_metadata_coverage_review_report.md"
    review_manifest["storage"]["manifest"] = str(manifest_out)
    review_manifest["storage"]["report"] = str(report_out)
    manifest_out.write_text(json.dumps(normalize_for_json(review_manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_out.write_text(render_report(review_manifest), encoding="utf-8")
    return normalize_for_json(review_manifest)


def validate_source_safety(manifest, exceptions):
    if manifest.get("decision") != NEEDS_RECONCILIATION:
        exceptions.append("unsafe_source_mapping_candidate_review_decision")
    if manifest.get("business_reconciliation_status") != BUSINESS_RECONCILIATION_STATUS:
        exceptions.append("unsafe_source_mapping_candidate_review_business_reconciliation_status")
    if manifest.get("data_maturity") != DATA_MATURITY:
        exceptions.append("unsafe_source_mapping_candidate_review_data_maturity")
    if manifest.get("final_production_selected") is not False:
        exceptions.append("unsafe_source_mapping_candidate_review_final_production_selected")


def resolve_manifest_run_path(manifest_path, manifest):
    selected = Path(manifest_path).parent.resolve(strict=False)
    declared = manifest.get("storage", {}).get("run_path")
    if declared and Path(declared).resolve(strict=False) != selected:
        raise ValueError("mapping_candidate_review_manifest_run_path_mismatch")
    return selected


def resolve_staging_run_path(manifest, fallback_run_path):
    raw_path = manifest.get("source_staging_run_path")
    if raw_path:
        return Path(raw_path).resolve(strict=False)
    schema_run_path = manifest.get("source_schema_gap_review_run_path")
    if schema_run_path:
        schema_manifest_path = Path(schema_run_path) / "inventory_staging_schema_gap_review_manifest.json"
        if schema_manifest_path.exists():
            schema_manifest = load_manifest(schema_manifest_path)
            raw_schema_staging_path = schema_manifest.get("source_staging_run_path")
            if raw_schema_staging_path:
                return Path(raw_schema_staging_path).resolve(strict=False)
    return Path(fallback_run_path).resolve(strict=False)


def collect_source_metadata(staging_run_path, exceptions):
    build_manifest_path = staging_run_path / "inventory_staging_build_manifest.json"
    if not build_manifest_path.exists():
        exceptions.append("source_staging_build_manifest_missing")
        return {}
    build_manifest = load_manifest(build_manifest_path)
    extract_manifest = load_declared_extract_manifest(build_manifest, staging_run_path)
    return build_source_metadata_index(extract_manifest)


def load_declared_extract_manifest(build_manifest, staging_run_path):
    raw_extract_run_path = build_manifest.get("source_extract_run_path") if build_manifest else ""
    if not raw_extract_run_path:
        return {}
    extract_run_path = Path(raw_extract_run_path).resolve(strict=False)
    repo_root = Path(__file__).resolve().parents[1].resolve(strict=False)
    if _is_relative_to(extract_run_path, repo_root):
        return {}
    if _is_relative_to(staging_run_path.resolve(strict=False), extract_run_path):
        return {}
    extract_manifest_path = extract_run_path / "inventory_operation_extract_manifest.json"
    if not extract_manifest_path.exists():
        return {}
    return load_manifest(extract_manifest_path)


def build_source_metadata_index(extract_manifest):
    metadata = {}
    for item in extract_manifest.get("schema_metadata", []) + extract_manifest.get("surface_exports", []):
        merge_metadata_item(metadata, item)
    return metadata


def merge_metadata_item(metadata, item):
    if not isinstance(item, dict):
        return
    source_surface = sanitize_text(
        item.get("source_surface")
        or item.get("object_id")
        or ".".join([part for part in [item.get("schema_name"), item.get("object_name")] if part])
    )
    if not source_surface:
        return
    columns = list(item.get("present_source_columns") or item.get("selected_columns") or [])
    if not columns:
        missing = set(item.get("missing_source_columns") or item.get("missing_expected_columns") or [])
        columns = [
            column
            for column in list(item.get("expected_source_columns") or item.get("expected_columns") or [])
            if column not in missing
        ]
    columns = unique_list(columns)
    if not columns:
        return
    existing = metadata.setdefault(source_surface, {"source_surface": source_surface, "columns": []})
    existing["columns"] = unique_list(existing["columns"] + columns)


def collect_mapping_gaps(manifest):
    gaps = []
    for item in manifest.get("unresolved_mapping_gaps", []) or manifest.get("mapping_candidates", []):
        if not isinstance(item, dict):
            continue
        source_surface = sanitize_text(item.get("gap_source_surface") or item.get("source_surface") or "")
        missing_source_column = sanitize_text(item.get("missing_source_column") or "")
        if not source_surface or not missing_source_column:
            continue
        gaps.append(
            {
                "gap_source_surface": source_surface,
                "staging_table": sanitize_text(item.get("staging_table", "")),
                "missing_source_column": missing_source_column,
                "dependent_staging_fields": unique_list(item.get("dependent_staging_fields") or []),
            }
        )
    return unique_preserve_order(gaps)


def build_surface_column_inventory(source_metadata):
    inventory = []
    for source_surface in sorted(source_metadata):
        columns = unique_list(source_metadata[source_surface].get("columns") or [])
        quantity_like = [column for column in QUANTITY_LIKE_COLUMNS if column in set(columns)]
        roles = infer_surface_roles(source_surface)
        has_docno = "DocNo" in columns
        has_dockey = "DocKey" in columns
        classification = "metadata_available"
        if roles and has_dockey and not has_docno:
            classification = "candidate_header_surface_missing_docno"
        if roles and has_dockey and has_docno:
            classification = "candidate_header_surface_has_docno_and_dockey"
        if quantity_like and not roles:
            classification = "candidate_quantity_surface"
        inventory.append(
            {
                "source_surface": source_surface,
                "columns": columns,
                "has_docno": has_docno,
                "has_dockey": has_dockey,
                "quantity_like_columns": quantity_like,
                "surface_roles": roles,
                "candidate_classification": classification,
                "confidence": "metadata_only",
            }
        )
    return inventory


def infer_surface_roles(source_surface):
    value = source_surface.lower()
    roles = []
    if ("goodsreceivednote" in value and "detail" not in value) or value.endswith(".grn") or value.endswith(".gr"):
        roles.append("grn_header_candidate")
    if "stockreceive" in value and "detail" not in value:
        roles.append("stock_receive_header_candidate")
    if "stocktransfer" in value and "detail" not in value:
        roles.append("stock_transfer_header_candidate")
    return roles


def build_docno_doc_key_candidates(surface_column_inventory):
    candidates = []
    for item in surface_column_inventory:
        if not (item["has_docno"] or item["has_dockey"]):
            continue
        if not item["surface_roles"]:
            continue
        classification = (
            "candidate_header_surface_has_docno_and_dockey"
            if item["has_docno"] and item["has_dockey"]
            else "candidate_header_surface_missing_docno"
        )
        columns = [column for column in ["DocNo", "DocKey"] if column in set(item["columns"])]
        candidates.append(
            {
                "source_surface": item["source_surface"],
                "surface_roles": item["surface_roles"],
                "docno_doc_key_columns": columns,
                "candidate_classification": classification,
                "confidence": "metadata_only" if classification.endswith("has_docno_and_dockey") else "needs_manual_validation",
            }
        )
    return candidates


def build_quantity_column_candidates(surface_column_inventory):
    return [
        {
            "source_surface": item["source_surface"],
            "surface_roles": item["surface_roles"],
            "quantity_like_columns": item["quantity_like_columns"],
            "candidate_classification": "candidate_quantity_surface",
            "confidence": "needs_manual_validation",
        }
        for item in surface_column_inventory
        if item.get("quantity_like_columns")
    ]


def build_unresolved_metadata_gaps(mapping_gaps, docno_doc_key_candidates, quantity_column_candidates):
    unresolved = []
    for gap in mapping_gaps:
        missing = gap["missing_source_column"]
        if missing == "BatchBalQty" and not gap["dependent_staging_fields"]:
            unresolved.append(
                {
                    **gap,
                    "candidate_classification": "needs_manual_validation",
                    "confidence": "requires_manual_validation",
                    "metadata_gap_reason": "expected_column_has_no_dependent_staging_field",
                    "related_candidate_surfaces": [],
                }
            )
            continue

        related_docno = [
            item
            for item in docno_doc_key_candidates
            if missing == "DocNo"
            and "candidate_header_surface_has_docno_and_dockey" == item["candidate_classification"]
            and surfaces_related(gap["gap_source_surface"], item["source_surface"])
        ]
        if related_docno:
            unresolved.append(
                {
                    **gap,
                    "candidate_classification": "metadata_available",
                    "confidence": "needs_manual_validation",
                    "metadata_gap_reason": "related_header_surface_has_docno_and_dockey",
                    "related_candidate_surfaces": [item["source_surface"] for item in related_docno],
                }
            )
            continue

        related_quantity = [
            item
            for item in quantity_column_candidates
            if missing in {"SmallestQty", "BatchBalQty"}
            and surfaces_related(gap["gap_source_surface"], item["source_surface"])
        ]
        if related_quantity:
            unresolved.append(
                {
                    **gap,
                    "candidate_classification": "metadata_available",
                    "confidence": "needs_manual_validation",
                    "metadata_gap_reason": "related_quantity_columns_available",
                    "related_candidate_surfaces": [item["source_surface"] for item in related_quantity],
                }
            )
            continue

        unresolved.append(
            {
                **gap,
                "candidate_classification": "metadata_insufficient",
                "confidence": "insufficient_metadata",
                "metadata_gap_reason": "no_related_surface_metadata_with_required_column",
                "related_candidate_surfaces": [],
            }
        )
    return unique_preserve_order(unresolved)


def surfaces_related(gap_surface, candidate_surface):
    gap = gap_surface.lower()
    candidate = candidate_surface.lower()
    if "goodsreceivednote" in gap or "grdtl" in gap:
        return "goodsreceivednote" in candidate or candidate.endswith(".grn") or candidate.endswith(".gr")
    if "stockreceive" in gap:
        return "stockreceive" in candidate
    if "stocktransfer" in gap:
        return "stocktransfer" in candidate
    return False


def build_run_plan(output_root=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    resolved_output_root = resolve_output_root(output_root or DEFAULT_OUTPUT_ROOT)
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root
            / f"inventory_source_metadata_coverage_review_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    resolved = Path(output_root).expanduser().resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def build_review_manifest(
    source_manifest,
    plan,
    status,
    source_mapping_run_path,
    staging_run_path,
    surface_column_inventory,
    docno_doc_key_candidates,
    quantity_column_candidates,
    unresolved_metadata_gaps,
    exceptions,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    reviewed_surfaces = sorted({item["source_surface"] for item in surface_column_inventory})
    return {
        "job": "autocount_inventory_source_metadata_coverage_review",
        "status": status,
        "decision": NEEDS_RECONCILIATION,
        "recommendation": BLOCKED_RECOMMENDATION,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_mapping_candidate_review_run_id": source_manifest.get("run_id"),
        "source_mapping_candidate_review_run_path": str(source_mapping_run_path),
        "source_staging_run_path": str(staging_run_path),
        "reviewed_surfaces": reviewed_surfaces,
        "surface_column_inventory": surface_column_inventory,
        "docno_doc_key_candidates": docno_doc_key_candidates,
        "quantity_column_candidates": quantity_column_candidates,
        "unresolved_metadata_gaps": unresolved_metadata_gaps,
        "warning_count": len(unresolved_metadata_gaps),
        "exception_count": len(unique_preserve_order(exceptions)),
        "exceptions": unique_preserve_order([sanitize_text(exception) for exception in exceptions]),
        "generated_files": [
            "inventory_source_metadata_coverage_review_manifest.json",
            "inventory_source_metadata_coverage_review_report.md",
        ],
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": [
            "Diagnostic-only metadata coverage review from local manifests and source column metadata.",
            "Column evidence is not a selected mapping and requires manual validation before any staging change.",
            "No raw ERP rows, dashboards, joins, reconciled dimensions, DB loads, scheduler, or write-back are produced.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Source Metadata Coverage Review Report",
        "",
        "This review uses source surface and column metadata only. It does not query raw AutoCount business rows or build dashboards.",
        "",
        "## Safe Summary",
        "",
        f"- Status: {manifest['status']}",
        f"- Decision: {manifest['decision']}",
        f"- Recommendation: {manifest['recommendation']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        f"- Warning count: {manifest['warning_count']}",
        f"- Exception count: {manifest['exception_count']}",
        "",
        "## DocNo / DocKey Candidates",
        "",
    ]
    for item in manifest.get("docno_doc_key_candidates", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['source_surface']}`: {item['candidate_classification']} "
                f"({', '.join(item['docno_doc_key_columns']) or 'no DocNo/DocKey columns'})"
            )
            lines.append(f"  - Roles: {', '.join(item['surface_roles']) or 'none'}")
            lines.append(f"  - Confidence: {item['confidence']}")
    lines.extend(["", "## Quantity Column Candidates", ""])
    for item in manifest.get("quantity_column_candidates", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['source_surface']}`: {item['candidate_classification']} "
                f"({', '.join(item['quantity_like_columns'])})"
            )
            lines.append(f"  - Confidence: {item['confidence']}")
    lines.extend(["", "## Unresolved Metadata Gaps", ""])
    for item in manifest.get("unresolved_metadata_gaps", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['gap_source_surface']}` `{item['missing_source_column']}`: "
                f"{item['candidate_classification']} ({item['metadata_gap_reason']})"
            )
            lines.append(f"  - Dependent staging fields: {', '.join(item['dependent_staging_fields']) or 'none'}")
            lines.append(f"  - Related candidate surfaces: {', '.join(item['related_candidate_surfaces']) or 'none'}")
    lines.extend(["", "## Surface Column Inventory", ""])
    for item in manifest.get("surface_column_inventory", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['source_surface']}`: columns={', '.join(item['columns'])}; "
                f"classification={item['candidate_classification']}"
            )
    lines.extend(["", "## Generated Files", ""])
    for filename in manifest["generated_files"]:
        lines.append(f"- `{filename}`")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Do not paste raw staging CSV rows or raw ERP rows.",
            "- Do not build dashboards, purchase recommendations, business KPIs, joins, reconciled dimensions, DB loads, scheduler, or write-back from this review.",
            "- Treat metadata coverage as manual-validation evidence only.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_metadata_sql_definitions():
    return [
        {
            "name": "inventory_source_metadata_columns",
            "sql": (
                "SELECT s.name AS schema_name, o.name AS object_name, o.type_desc AS object_type, "
                "c.name AS column_name, t.name AS data_type "
                "FROM sys.objects AS o "
                "INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id "
                "INNER JOIN sys.columns AS c ON c.object_id = o.object_id "
                "INNER JOIN sys.types AS t ON t.user_type_id = c.user_type_id "
                "WHERE o.type IN ('U', 'V') "
                "AND (o.name LIKE '%GoodsReceivedNote%' OR o.name LIKE '%GR%' "
                "OR o.name LIKE '%StockReceive%' OR o.name LIKE '%StockTransfer%')"
            ),
        }
    ]


class SqlServerMetadataCoverageSource:
    def __init__(self, connection_string):
        self.connection_string = connection_string

    def fetch_metadata(self):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before metadata coverage probing") from exc

        metadata = {}
        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            cursor.execute(build_metadata_sql_definitions()[0]["sql"])
            columns = [column[0] for column in cursor.description or []]
            for row in cursor.fetchall():
                record = dict(zip(columns, row))
                source_surface = ".".join([record.get("schema_name", ""), record.get("object_name", "")])
                merge_metadata_item(
                    metadata,
                    {
                        "source_surface": source_surface,
                        "selected_columns": [record.get("column_name", "")],
                    },
                )
        return metadata


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


def unique_list(values):
    return list(unique_preserve_order([sanitize_text(value) for value in values if str(value).strip()]))


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
    parser = argparse.ArgumentParser(description="Review inventory source metadata coverage for mapping candidates.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_source_mapping_candidate_review_manifest.json.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    parser.add_argument(
        "--use-sql-metadata-probe",
        action="store_true",
        help="Use read-only SQL Server catalog metadata from AUTOCOUNT_READONLY_SQL_CONNECTION_STRING.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        source = None
        if args.use_sql_metadata_probe:
            import os

            connection_string = os.environ.get(DEFAULT_CONNECTION_STRING_ENV)
            if not connection_string:
                raise RuntimeError(f"Environment variable {DEFAULT_CONNECTION_STRING_ENV} is not set")
            source = SqlServerMetadataCoverageSource(connection_string)
        manifest = run_metadata_coverage_review(args.manifest, output_root=args.output_root, source=source)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "recommendation": manifest["recommendation"],
                    "run_path": manifest["storage"]["run_path"],
                    "warning_count": manifest["warning_count"],
                    "exception_count": manifest["exception_count"],
                    "generated_files": manifest["generated_files"],
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
