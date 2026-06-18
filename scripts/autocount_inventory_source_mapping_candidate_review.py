import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_source_mapping_candidate_review"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"
DASHBOARD_BLOCKER = "dashboard_blocker_requires_manual_validation"
BLOCKED_RECOMMENDATION = "no_dashboard_until_schema_gap_resolved"

PREFERRED_DOCNO_HEADER_SURFACES = {
    "dbo.vGoodsReceivedNoteDetail": ["dbo.vGoodsReceivedNote", "dbo.GRN"],
    "dbo.vGoodsReceivedNoteSubDetail": ["dbo.vGoodsReceivedNote", "dbo.GRN"],
    "dbo.GRDTL": ["dbo.vGoodsReceivedNote", "dbo.GRN"],
    "dbo.vStockReceiveDetail": ["dbo.vStockReceive", "dbo.StockReceive"],
    "dbo.vStockTransferDetail": ["dbo.vStockTransfer", "dbo.StockTransfer"],
}
QUANTITY_LIKE_COLUMNS = [
    "Qty",
    "Quantity",
    "BaseQty",
    "SmallestQty",
    "TransferQty",
    "ReceiveQty",
    "TransferedQty",
    "TransferredQty",
    "BalQty",
    "BatchBalQty",
]


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_mapping_candidate_review(manifest_path, output_root=None, now=None):
    plan = build_run_plan(output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    manifest_path = Path(manifest_path)
    source_manifest = {}
    source_schema_run_path = manifest_path.parent.resolve(strict=False)
    staging_run_path = source_schema_run_path
    exceptions = []
    source_metadata = {}

    try:
        source_manifest = load_manifest(manifest_path)
        validate_source_safety(source_manifest, exceptions)
        source_schema_run_path = resolve_manifest_run_path(manifest_path, source_manifest)
        staging_run_path = Path(source_manifest.get("source_staging_run_path") or source_schema_run_path).resolve(strict=False)
        source_metadata = collect_source_metadata(staging_run_path, exceptions)
    except Exception as exc:  # noqa: BLE001 - local review writes redacted failure output.
        exceptions.append(sanitize_text(str(exc)))

    source_schema_gaps = [
        item
        for item in source_manifest.get("source_schema_gaps", [])
        if isinstance(item, dict) and item.get("classification") == "needs_source_column_mapping"
    ]
    mapping_candidates = build_mapping_candidates(source_schema_gaps, source_metadata)
    unresolved_mapping_gaps = [
        item
        for item in mapping_candidates
        if item.get("candidate_classification")
        in {
            "candidate_header_bridge",
            "candidate_quantity_alias",
            "expected_column_may_be_unneeded",
            "needs_source_metadata_evidence",
            "no_candidate_found",
        }
    ]
    status = "failed" if exceptions else "success_with_warnings" if unresolved_mapping_gaps else "success"
    recommendation = BLOCKED_RECOMMENDATION if unresolved_mapping_gaps else "source_mapping_candidate_review_ready"

    review_manifest = build_review_manifest(
        source_manifest=source_manifest,
        plan=plan,
        status=status,
        recommendation=recommendation,
        source_schema_run_path=source_schema_run_path,
        staging_run_path=staging_run_path,
        source_schema_gaps=source_schema_gaps,
        mapping_candidates=mapping_candidates,
        unresolved_mapping_gaps=unresolved_mapping_gaps,
        exceptions=exceptions,
        now=now,
    )
    manifest_out = run_path / "inventory_source_mapping_candidate_review_manifest.json"
    report_out = run_path / "inventory_source_mapping_candidate_review_report.md"
    review_manifest["storage"]["manifest"] = str(manifest_out)
    review_manifest["storage"]["report"] = str(report_out)
    manifest_out.write_text(json.dumps(normalize_for_json(review_manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_out.write_text(render_report(review_manifest), encoding="utf-8")
    return normalize_for_json(review_manifest)


def validate_source_safety(manifest, exceptions):
    if manifest.get("decision") != NEEDS_RECONCILIATION:
        exceptions.append("unsafe_source_schema_gap_review_decision")
    if manifest.get("business_reconciliation_status") != BUSINESS_RECONCILIATION_STATUS:
        exceptions.append("unsafe_source_schema_gap_review_business_reconciliation_status")
    if manifest.get("data_maturity") != DATA_MATURITY:
        exceptions.append("unsafe_source_schema_gap_review_data_maturity")
    if manifest.get("final_production_selected") is not False:
        exceptions.append("unsafe_source_schema_gap_review_final_production_selected")


def resolve_manifest_run_path(manifest_path, manifest):
    selected = Path(manifest_path).parent.resolve(strict=False)
    declared = manifest.get("storage", {}).get("run_path")
    if declared and Path(declared).resolve(strict=False) != selected:
        raise ValueError("schema_gap_review_manifest_run_path_mismatch")
    return selected


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
        if not isinstance(item, dict):
            continue
        source_surface = sanitize_text(
            item.get("source_surface")
            or item.get("object_id")
            or ".".join([part for part in [item.get("schema_name"), item.get("object_name")] if part])
        )
        if not source_surface:
            continue
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
            continue
        existing = metadata.setdefault(source_surface, {"source_surface": source_surface, "columns": []})
        existing["columns"] = unique_list(existing["columns"] + columns)
    return metadata


def build_mapping_candidates(source_schema_gaps, source_metadata):
    candidates = []
    for gap in source_schema_gaps:
        for missing_column in gap.get("missing_source_columns", []):
            candidates.append(classify_missing_column(gap, sanitize_text(missing_column), source_metadata))
    return unique_preserve_order(candidates)


def classify_missing_column(gap, missing_column, source_metadata):
    source_surface = sanitize_text(gap.get("source_surface", ""))
    staging_table = sanitize_text(gap.get("staging_table", ""))
    dependent_fields = dependent_staging_fields_for(gap, missing_column)
    base = {
        "gap_source_surface": source_surface,
        "staging_table": staging_table,
        "missing_source_column": missing_column,
        "dependent_staging_fields": dependent_fields,
        "candidate_source_surface": "",
        "candidate_bridge_columns": [],
        "candidate_value_columns": [],
        "candidate_classification": "",
        "confidence": "",
        "dashboard_impact": DASHBOARD_BLOCKER,
    }

    if not source_metadata:
        return {
            **base,
            "candidate_classification": "needs_source_metadata_evidence",
            "confidence": "insufficient_metadata",
        }

    if missing_column == "BatchBalQty" and not dependent_fields:
        return {
            **base,
            "candidate_classification": "expected_column_may_be_unneeded",
            "confidence": "requires_manual_validation",
        }

    if missing_column == "DocNo":
        candidate = find_docno_header_candidate(source_surface, source_metadata)
        if candidate:
            return {
                **base,
                "candidate_source_surface": candidate["source_surface"],
                "candidate_bridge_columns": ["DocKey", "DocNo"],
                "candidate_value_columns": ["DocNo"],
                "candidate_classification": "candidate_header_bridge",
                "confidence": "metadata_only_candidate",
            }

    if missing_column == "SmallestQty":
        candidate = find_quantity_candidate(source_surface, gap.get("present_source_columns", []), source_metadata)
        if candidate:
            return {
                **base,
                "candidate_source_surface": candidate["source_surface"],
                "candidate_bridge_columns": candidate["bridge_columns"],
                "candidate_value_columns": candidate["value_columns"],
                "candidate_classification": "candidate_quantity_alias",
                "confidence": "requires_manual_validation",
            }

    return {
        **base,
        "candidate_classification": "no_candidate_found",
        "confidence": "insufficient_metadata",
    }


def dependent_staging_fields_for(gap, missing_column):
    dependent = gap.get("dependent_staging_fields")
    if isinstance(dependent, dict):
        return unique_list(dependent.get(missing_column) or [])
    return []


def find_docno_header_candidate(source_surface, source_metadata):
    preferred = PREFERRED_DOCNO_HEADER_SURFACES.get(source_surface, [])
    for candidate_surface in preferred:
        candidate = source_metadata.get(candidate_surface)
        if candidate and has_columns(candidate, ["DocKey", "DocNo"]):
            return candidate
    for candidate in source_metadata.values():
        if candidate["source_surface"] == source_surface:
            continue
        if has_columns(candidate, ["DocKey", "DocNo"]) and surfaces_related(source_surface, candidate["source_surface"]):
            return candidate
    return {}


def find_quantity_candidate(source_surface, present_columns, source_metadata):
    matches = []
    present_set = {str(column) for column in present_columns}
    for candidate in source_metadata.values():
        if candidate["source_surface"] == source_surface:
            continue
        if not surfaces_related(source_surface, candidate["source_surface"]):
            continue
        value_columns = [column for column in QUANTITY_LIKE_COLUMNS if column in set(candidate["columns"])]
        if not value_columns:
            continue
        bridge_columns = [column for column in candidate["columns"] if column in present_set and column not in value_columns]
        matches.append(
            {
                "source_surface": candidate["source_surface"],
                "bridge_columns": unique_list(bridge_columns),
                "value_columns": unique_list(value_columns),
            }
        )
    if not matches:
        return {}
    return sorted(matches, key=lambda item: (0 if item["bridge_columns"] else 1, item["source_surface"]))[0]


def surfaces_related(source_surface, candidate_surface):
    source = source_surface.lower()
    candidate = candidate_surface.lower()
    if "goodsreceivednote" in source or "grdtl" in source or ".gr" in source:
        return "goodsreceivednote" in candidate or ".gr" in candidate
    if "stockreceive" in source:
        return "stockreceive" in candidate
    if "stocktransfer" in source:
        return "stocktransfer" in candidate
    return False


def has_columns(candidate, required_columns):
    columns = set(candidate.get("columns") or [])
    return all(column in columns for column in required_columns)


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
            / f"inventory_source_mapping_candidate_review_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
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
    recommendation,
    source_schema_run_path,
    staging_run_path,
    source_schema_gaps,
    mapping_candidates,
    unresolved_mapping_gaps,
    exceptions,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    return {
        "job": "autocount_inventory_source_mapping_candidate_review",
        "status": status,
        "decision": NEEDS_RECONCILIATION,
        "recommendation": recommendation,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_schema_gap_review_run_id": source_manifest.get("run_id"),
        "source_schema_gap_review_run_path": str(source_schema_run_path),
        "source_staging_run_path": str(staging_run_path),
        "reviewed_source_surfaces": sorted(
            {sanitize_text(item.get("source_surface", "")) for item in source_schema_gaps if item.get("source_surface")}
        ),
        "mapping_candidates": mapping_candidates,
        "unresolved_mapping_gaps": unresolved_mapping_gaps,
        "warning_count": len(unresolved_mapping_gaps),
        "exception_count": len(unique_preserve_order(exceptions)),
        "exceptions": unique_preserve_order([sanitize_text(exception) for exception in exceptions]),
        "generated_files": [
            "inventory_source_mapping_candidate_review_manifest.json",
            "inventory_source_mapping_candidate_review_report.md",
        ],
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": [
            "Diagnostic-only source mapping candidate review from local manifests and source column metadata.",
            "Candidate mappings are not final business logic and require manual validation before any staging change.",
            "No raw ERP rows, dashboards, joins, reconciled dimensions, DB loads, scheduler, or write-back are produced.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Source Mapping Candidate Review Report",
        "",
        "This review uses local manifests and source column metadata only. It does not query AutoCount or build dashboards.",
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
        "## Reviewed Source Surfaces",
        "",
    ]
    for source_surface in manifest.get("reviewed_source_surfaces", []) or ["none"]:
        lines.append(f"- `{source_surface}`")
    lines.extend(["", "## Mapping Candidates", ""])
    for item in manifest.get("mapping_candidates", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['gap_source_surface']}` `{item['missing_source_column']}` -> "
                f"`{item['candidate_source_surface'] or 'none'}`: {item['candidate_classification']}"
            )
            lines.append(f"  - Staging table: `{item['staging_table']}`")
            lines.append(f"  - Dependent staging fields: {', '.join(item['dependent_staging_fields']) or 'none'}")
            lines.append(f"  - Candidate bridge columns: {', '.join(item['candidate_bridge_columns']) or 'none'}")
            lines.append(f"  - Candidate value columns: {', '.join(item['candidate_value_columns']) or 'none'}")
            lines.append(f"  - Confidence: {item['confidence']}")
            lines.append(f"  - Dashboard impact: {item['dashboard_impact']}")
    lines.extend(["", "## Unresolved Mapping Gaps", ""])
    for item in manifest.get("unresolved_mapping_gaps", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- `{item['gap_source_surface']}` `{item['missing_source_column']}`: "
                f"{item['candidate_classification']} ({item['confidence']})"
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
            "- Treat every candidate as manual-validation evidence only.",
        ]
    )
    return "\n".join(lines) + "\n"


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
    parser = argparse.ArgumentParser(description="Review local inventory source mapping candidates.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_staging_schema_gap_review_manifest.json.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_mapping_candidate_review(args.manifest, output_root=args.output_root)
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
