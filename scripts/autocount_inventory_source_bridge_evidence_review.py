import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_source_bridge_evidence_review"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"
BLOCKED_RECOMMENDATION = "no_dashboard_until_schema_gap_resolved"

PROPOSED_DOCNO_BRIDGE = "detail.DocKey -> header.DocKey -> header.DocNo"
NOT_SELECTED = "not_selected_manual_validation_required"

BRIDGE_CANDIDATE_DEFINITIONS = [
    {
        "bridge_name": "GRN",
        "detail_source_surfaces": ["dbo.vGoodsReceivedNoteDetail", "dbo.vGoodsReceivedNoteSubDetail"],
        "header_source_surface": "dbo.vGoodsReceivedNote",
        "dependent_staging_field": "grn_doc_no",
    },
    {
        "bridge_name": "GR / GRDTL",
        "detail_source_surfaces": ["dbo.GRDTL"],
        "header_source_surface": "dbo.GR",
        "dependent_staging_field": "grn_doc_no",
    },
    {
        "bridge_name": "Stock Receive",
        "detail_source_surfaces": ["dbo.vStockReceiveDetail"],
        "header_source_surface": "dbo.vStockReceive",
        "dependent_staging_field": "receive_doc_no",
    },
    {
        "bridge_name": "Stock Transfer",
        "detail_source_surfaces": ["dbo.vStockTransferDetail"],
        "header_source_surface": "dbo.vStockTransfer",
        "dependent_staging_field": "transfer_doc_no",
    },
]

QUANTITY_REVIEW_DEFINITIONS = [
    {
        "source_surface": "dbo.vGoodsReceivedNoteDetail",
        "reviewed_expected_column": "SmallestQty",
        "mode": "direct",
        "possible_alias_columns": [],
    },
    {
        "source_surface": "dbo.GRDTL",
        "reviewed_expected_column": "SmallestQty",
        "mode": "direct",
        "possible_alias_columns": [],
    },
    {
        "source_surface": "dbo.vStockReceiveDetail",
        "reviewed_expected_column": "SmallestQty",
        "mode": "possible_alias",
        "possible_alias_columns": ["Qty", "BatchBalQty"],
    },
    {
        "source_surface": "dbo.vStockTransferDetail",
        "reviewed_expected_column": "SmallestQty",
        "mode": "possible_alias",
        "possible_alias_columns": ["Qty"],
    },
]


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_bridge_evidence_review(manifest_path, output_root=None, now=None):
    plan = build_run_plan(output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    manifest_path = Path(manifest_path)
    source_manifest = {}
    source_metadata_run_path = manifest_path.parent.resolve(strict=False)
    exceptions = []

    try:
        source_manifest = load_manifest(manifest_path)
        validate_source_safety(source_manifest, exceptions)
        source_metadata_run_path = resolve_manifest_run_path(manifest_path, source_manifest)
    except Exception as exc:  # noqa: BLE001 - local review writes redacted failure output.
        exceptions.append(sanitize_text(str(exc)))

    bridge_candidates = build_bridge_candidates(source_manifest)
    quantity_alias_reviews = build_quantity_alias_reviews(source_manifest)
    expected_column_reviews = build_expected_column_reviews(source_manifest)
    status = (
        "failed"
        if exceptions
        else "success_with_warnings"
        if bridge_candidates or quantity_alias_reviews or expected_column_reviews
        else "success"
    )

    review_manifest = build_review_manifest(
        source_manifest=source_manifest,
        plan=plan,
        status=status,
        source_metadata_run_path=source_metadata_run_path,
        bridge_candidates=bridge_candidates,
        quantity_alias_reviews=quantity_alias_reviews,
        expected_column_reviews=expected_column_reviews,
        exceptions=exceptions,
        now=now,
    )
    manifest_out = run_path / "inventory_source_bridge_evidence_review_manifest.json"
    report_out = run_path / "inventory_source_bridge_evidence_review_report.md"
    review_manifest["storage"]["manifest"] = str(manifest_out)
    review_manifest["storage"]["report"] = str(report_out)
    manifest_out.write_text(json.dumps(normalize_for_json(review_manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_out.write_text(render_report(review_manifest), encoding="utf-8")
    return normalize_for_json(review_manifest)


def validate_source_safety(manifest, exceptions):
    if manifest.get("job") != "autocount_inventory_source_metadata_coverage_review":
        exceptions.append("unsafe_source_metadata_coverage_review_job")
    if manifest.get("status") not in {"success", "success_with_warnings"}:
        exceptions.append("unsafe_source_metadata_coverage_review_status")
    if manifest.get("recommendation") != BLOCKED_RECOMMENDATION:
        exceptions.append("unsafe_source_metadata_coverage_review_recommendation")
    if manifest.get("decision") != NEEDS_RECONCILIATION:
        exceptions.append("unsafe_source_metadata_coverage_review_decision")
    if manifest.get("business_reconciliation_status") != BUSINESS_RECONCILIATION_STATUS:
        exceptions.append("unsafe_source_metadata_coverage_review_business_reconciliation_status")
    if manifest.get("data_maturity") != DATA_MATURITY:
        exceptions.append("unsafe_source_metadata_coverage_review_data_maturity")
    if manifest.get("final_production_selected") is not False:
        exceptions.append("unsafe_source_metadata_coverage_review_final_production_selected")
    if int(manifest.get("exception_count") or 0) != 0:
        exceptions.append("unsafe_source_metadata_coverage_review_exception_count")


def resolve_manifest_run_path(manifest_path, manifest):
    selected = Path(manifest_path).parent.resolve(strict=False)
    declared = manifest.get("storage", {}).get("run_path")
    if declared and Path(declared).resolve(strict=False) != selected:
        raise ValueError("metadata_coverage_review_manifest_run_path_mismatch")
    return selected


def build_bridge_candidates(source_manifest):
    source_columns = build_source_columns_index(source_manifest)
    doc_candidates = {
        sanitize_text(item.get("source_surface", "")): item
        for item in source_manifest.get("docno_doc_key_candidates", [])
        if isinstance(item, dict) and item.get("source_surface")
    }
    docno_gaps = [
        item
        for item in source_manifest.get("unresolved_metadata_gaps", [])
        if isinstance(item, dict) and item.get("missing_source_column") == "DocNo"
    ]

    candidates = []
    for definition in BRIDGE_CANDIDATE_DEFINITIONS:
        detail_surfaces = definition["detail_source_surfaces"]
        related_gaps = [gap for gap in docno_gaps if gap.get("gap_source_surface") in detail_surfaces]
        header_surface = definition["header_source_surface"]
        header_columns = set(source_columns.get(header_surface, []))
        header_candidate = doc_candidates.get(header_surface, {})
        header_candidate_columns = set(header_candidate.get("docno_doc_key_columns") or [])
        header_has_docno_and_dockey = (
            {"DocNo", "DocKey"}.issubset(header_columns)
            or {"DocNo", "DocKey"}.issubset(header_candidate_columns)
            or header_candidate.get("candidate_classification") == "candidate_header_surface_has_docno_and_dockey"
        )
        detail_surfaces_have_dockey = all("DocKey" in set(source_columns.get(surface, [])) for surface in detail_surfaces)
        evidence_status = (
            "metadata_evidence_available"
            if related_gaps and header_has_docno_and_dockey and detail_surfaces_have_dockey
            else "metadata_evidence_incomplete"
        )
        candidates.append(
            {
                "bridge_name": definition["bridge_name"],
                "detail_source_surfaces": detail_surfaces,
                "header_source_surface": header_surface,
                "proposed_bridge": PROPOSED_DOCNO_BRIDGE,
                "dependent_staging_field": definition["dependent_staging_field"],
                "candidate_classification": "metadata_only_header_bridge_candidate",
                "confidence": "needs_manual_validation",
                "selection_status": NOT_SELECTED,
                "manual_validation_required": True,
                "metadata_evidence": {
                    "detail_surfaces_have_dockey": detail_surfaces_have_dockey,
                    "header_has_docno_and_dockey": header_has_docno_and_dockey,
                    "source_gap_surfaces": unique_list(gap.get("gap_source_surface", "") for gap in related_gaps),
                    "source_gap_reasons": unique_list(gap.get("metadata_gap_reason", "") for gap in related_gaps),
                    "related_header_surfaces": unique_list(
                        surface
                        for gap in related_gaps
                        for surface in gap.get("related_candidate_surfaces", [])
                    ),
                    "dependent_staging_fields_from_source": unique_list(
                        field
                        for gap in related_gaps
                        for field in gap.get("dependent_staging_fields", [])
                    ),
                    "evidence_status": evidence_status,
                },
            }
        )
    return candidates


def build_quantity_alias_reviews(source_manifest):
    quantity_columns = build_quantity_columns_index(source_manifest)
    reviews = []
    for definition in QUANTITY_REVIEW_DEFINITIONS:
        source_surface = definition["source_surface"]
        available_columns = quantity_columns.get(source_surface, [])
        reviewed_column = definition["reviewed_expected_column"]
        if definition["mode"] == "direct":
            classification = (
                "direct_quantity_column_available"
                if reviewed_column in set(available_columns)
                else "direct_quantity_column_missing"
            )
            possible_alias_columns = []
        else:
            classification = "possible_quantity_alias_requires_manual_validation"
            possible_alias_columns = [
                column for column in definition["possible_alias_columns"] if column in set(available_columns)
            ]
        reviews.append(
            {
                "source_surface": source_surface,
                "reviewed_expected_column": reviewed_column,
                "available_quantity_columns": available_columns,
                "candidate_classification": classification,
                "possible_alias_columns": possible_alias_columns,
                "selected_quantity_column": "",
                "selection_status": NOT_SELECTED,
                "confidence": "needs_manual_validation",
            }
        )
    return reviews


def build_expected_column_reviews(source_manifest):
    reviews = []
    for item in source_manifest.get("unresolved_metadata_gaps", []):
        if not isinstance(item, dict):
            continue
        if item.get("gap_source_surface") != "dbo.vGoodsReceivedNoteSubDetail":
            continue
        if item.get("missing_source_column") != "BatchBalQty":
            continue
        if item.get("metadata_gap_reason") != "expected_column_has_no_dependent_staging_field":
            continue
        reviews.append(
            {
                "gap_source_surface": sanitize_text(item.get("gap_source_surface", "")),
                "staging_table": sanitize_text(item.get("staging_table", "")),
                "missing_source_column": "BatchBalQty",
                "dependent_staging_fields": unique_list(item.get("dependent_staging_fields") or []),
                "metadata_gap_reason": "expected_column_has_no_dependent_staging_field",
                "candidate_classification": "expected_column_manual_review",
                "staging_mapping_status": "not_required_without_dependent_staging_field",
                "confidence": "needs_manual_validation",
            }
        )
    return unique_preserve_order(reviews)


def build_source_columns_index(source_manifest):
    columns = {}
    for item in source_manifest.get("surface_column_inventory", []):
        if not isinstance(item, dict):
            continue
        source_surface = sanitize_text(item.get("source_surface", ""))
        if not source_surface:
            continue
        columns[source_surface] = unique_list(item.get("columns") or [])
    for item in source_manifest.get("quantity_column_candidates", []):
        if not isinstance(item, dict):
            continue
        source_surface = sanitize_text(item.get("source_surface", ""))
        if not source_surface:
            continue
        existing = columns.setdefault(source_surface, [])
        columns[source_surface] = unique_list(existing + list(item.get("quantity_like_columns") or []))
    for item in source_manifest.get("docno_doc_key_candidates", []):
        if not isinstance(item, dict):
            continue
        source_surface = sanitize_text(item.get("source_surface", ""))
        if not source_surface:
            continue
        existing = columns.setdefault(source_surface, [])
        columns[source_surface] = unique_list(existing + list(item.get("docno_doc_key_columns") or []))
    return columns


def build_quantity_columns_index(source_manifest):
    source_columns = build_source_columns_index(source_manifest)
    quantity_columns = {}
    for item in source_manifest.get("quantity_column_candidates", []):
        if not isinstance(item, dict):
            continue
        source_surface = sanitize_text(item.get("source_surface", ""))
        if not source_surface:
            continue
        quantity_columns[source_surface] = unique_list(item.get("quantity_like_columns") or [])
    for source_surface, columns in source_columns.items():
        quantity_columns.setdefault(
            source_surface,
            unique_list(
                column
                for column in columns
                if column
                in {
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
                }
            ),
        )
    return quantity_columns


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
            / f"inventory_source_bridge_evidence_review_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
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
    source_metadata_run_path,
    bridge_candidates,
    quantity_alias_reviews,
    expected_column_reviews,
    exceptions,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    manual_review_count = len(bridge_candidates) + len(quantity_alias_reviews) + len(expected_column_reviews)
    return {
        "job": "autocount_inventory_source_bridge_evidence_review",
        "status": status,
        "decision": NEEDS_RECONCILIATION,
        "recommendation": BLOCKED_RECOMMENDATION,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_metadata_coverage_review_run_id": source_manifest.get("run_id"),
        "source_metadata_coverage_review_run_path": str(source_metadata_run_path),
        "bridge_candidates": bridge_candidates,
        "quantity_alias_reviews": quantity_alias_reviews,
        "expected_column_reviews": expected_column_reviews,
        "manual_validation_item_count": manual_review_count,
        "warning_count": manual_review_count,
        "exception_count": len(unique_preserve_order(exceptions)),
        "exceptions": unique_preserve_order([sanitize_text(exception) for exception in exceptions]),
        "generated_files": [
            "inventory_source_bridge_evidence_review_manifest.json",
            "inventory_source_bridge_evidence_review_report.md",
        ],
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": [
            "Diagnostic-only inventory source bridge evidence review from metadata coverage output.",
            "Bridge candidates are metadata-only and require manual validation.",
            "No final mappings are selected by this review.",
            "No raw ERP rows, dashboards, KPIs, joins, reconciled dimensions, DB loads, scheduler, or write-back are produced.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Source Bridge Evidence Review Report",
        "",
        "Bridge candidates are metadata-only and require manual validation.",
        "This PR does not make staging/dashboard production-ready.",
        "Next future PR may patch staging mappings only after manual validation.",
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
        "## Bridge Candidates",
        "",
    ]
    for item in manifest.get("bridge_candidates", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- {item['bridge_name']}: {item['candidate_classification']}")
            lines.append(f"  - Detail/source surfaces: {', '.join(item['detail_source_surfaces'])}")
            lines.append(f"  - Header/source surface: {item['header_source_surface']}")
            lines.append(f"  - Proposed bridge: {item['proposed_bridge']}")
            lines.append(f"  - Dependent staging field: {item['dependent_staging_field']}")
            lines.append(f"  - Confidence: {item['confidence']}")
            lines.append(f"  - Selection status: {item['selection_status']}")
    lines.extend(["", "## Quantity Alias Review", ""])
    for item in manifest.get("quantity_alias_reviews", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(f"- {item['source_surface']}: {item['candidate_classification']}")
            lines.append(f"  - Reviewed expected column: {item['reviewed_expected_column']}")
            lines.append(f"  - Available quantity columns: {', '.join(item['available_quantity_columns']) or 'none'}")
            lines.append(f"  - Possible alias columns: {', '.join(item['possible_alias_columns']) or 'none'}")
            lines.append(f"  - Selected quantity column: {item['selected_quantity_column'] or 'none'}")
            lines.append(f"  - Confidence: {item['confidence']}")
    lines.extend(["", "## Expected Column Manual Review", ""])
    for item in manifest.get("expected_column_reviews", []) or ["none"]:
        if item == "none":
            lines.append("- none")
        else:
            lines.append(
                f"- {item['gap_source_surface']} {item['missing_source_column']}: "
                f"{item['metadata_gap_reason']}"
            )
            lines.append(f"  - Dependent staging fields: {', '.join(item['dependent_staging_fields']) or 'none'}")
            lines.append(f"  - Staging mapping status: {item['staging_mapping_status']}")
            lines.append(f"  - Confidence: {item['confidence']}")
    lines.extend(["", "## Generated Files", ""])
    for filename in manifest["generated_files"]:
        lines.append(f"- `{filename}`")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Diagnostic-only.",
            "- Metadata/report review only.",
            "- No SQL write-back.",
            "- No scheduler.",
            "- No raw ERP/business row export.",
            "- No raw CSV row output.",
            "- No credentials or connection strings.",
            "- No staging build behaviour changes.",
            "- No extraction behaviour changes.",
            "- No final mapping selection.",
            "- No dashboards, KPIs, purchase recommendations, analytics joins, DB loads, or reconciled dimensions.",
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
    parser = argparse.ArgumentParser(description="Build inventory source bridge evidence from metadata coverage output.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_source_metadata_coverage_review_manifest.json.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_bridge_evidence_review(args.manifest, output_root=args.output_root)
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
