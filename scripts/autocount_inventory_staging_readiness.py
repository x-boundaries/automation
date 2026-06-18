import argparse
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_staging_readiness"
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"
READINESS_DECISION = "schema_ready_data_thin"

ZERO_ROW_READY_FUNCTIONS = {
    "grn_header",
    "grn_detail",
    "stock_receive",
    "transfer_header",
    "transfer_detail",
}


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Extract manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_readiness(manifest_path, output_root=None, now=None):
    source_manifest = None
    warnings = []
    exceptions = []
    status = "success"
    source_manifest_path = Path(manifest_path)
    run_path = None

    try:
        source_manifest = load_manifest(source_manifest_path)
        validate_manifest_shape(source_manifest)
        plan = build_run_plan(output_root=output_root, now=now)
        run_path = Path(plan["run_path"])
        run_path.mkdir(parents=True, exist_ok=False)
        surface_readiness = classify_surfaces(source_manifest)
        warning_summary = summarize_warnings(source_manifest)
        warnings.extend(warning_summary)
        result = build_readiness_manifest(
            source_manifest,
            surface_readiness,
            warning_summary,
            plan,
            source_manifest_path,
            status,
            warnings,
            exceptions,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - local review should write a redacted failed manifest when possible.
        status = "failed"
        exceptions.append(sanitize_text(str(exc)))
        if "surface_results" in str(exc):
            warnings.append("manifest_uses_legacy_surface_results_field")
        plan = build_run_plan(output_root=output_root, now=now)
        run_path = Path(plan["run_path"])
        run_path.mkdir(parents=True, exist_ok=False)
        result = build_readiness_manifest(
            source_manifest or {},
            [],
            [],
            plan,
            source_manifest_path,
            status,
            warnings,
            exceptions,
            now=now,
        )

    manifest_path_out = run_path / "inventory_staging_readiness_manifest.json"
    report_path = run_path / "inventory_staging_readiness_report.md"
    result["storage"]["manifest"] = str(manifest_path_out)
    result["storage"]["report"] = str(report_path)
    manifest_path_out.write_text(json.dumps(normalize_for_json(result), indent=2, sort_keys=True), encoding="utf-8")
    report_path.write_text(render_report(result), encoding="utf-8")
    return normalize_for_json(result)


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
            / f"inventory_staging_readiness_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def validate_manifest_shape(source_manifest):
    if "surface_exports" not in source_manifest:
        if "surface_results" in source_manifest:
            raise ValueError("Expected PR #50 manifest field `surface_exports`; found legacy `surface_results`.")
        raise ValueError("Extract manifest missing required `surface_exports` field.")


def classify_surfaces(source_manifest):
    exports_by_id = {item.get("object_id"): item for item in source_manifest.get("surface_exports", [])}
    schema_by_id = {item.get("object_id"): item for item in source_manifest.get("schema_metadata", [])}
    row_counts = dict(source_manifest.get("row_counts", {}))
    results = []

    for object_id, export in exports_by_id.items():
        schema = schema_by_id.get(object_id, {})
        row_count = export.get("row_count", row_counts.get(object_id, 0))
        status = classify_surface_status(export, schema, row_count)
        results.append(
            {
                "object_id": object_id,
                "schema_name": export.get("schema_name", ""),
                "object_name": export.get("object_name", ""),
                "business_function": export.get("business_function", ""),
                "export_status": export.get("status", ""),
                "row_count": row_count,
                "file_name": export.get("file_name"),
                "has_output_path": bool(export.get("output_path")),
                "schema_available": bool(schema),
                "selected_column_count": len(schema.get("selected_columns", export.get("selected_columns", []))),
                "missing_expected_columns": export.get("missing_expected_columns", []),
                "readiness_status": status,
                "decision": NEEDS_RECONCILIATION,
                "final_production_selected": False,
            }
        )

    results.sort(key=lambda item: item["object_id"])
    return results


def classify_surface_status(export, schema, row_count):
    if export.get("status") in {"failed", "skipped"}:
        return "needs_profile_review"
    if not schema:
        return "needs_profile_review"
    if row_count == 0:
        return "ready_empty_surface"
    if row_count and row_count > 0:
        return "ready_non_empty_surface"
    return "not_applicable"


def summarize_warnings(source_manifest):
    return [sanitize_text(warning) for warning in source_manifest.get("warnings", [])]


def build_readiness_manifest(
    source_manifest,
    surface_readiness,
    warning_summary,
    plan,
    source_manifest_path,
    status,
    warnings,
    exceptions,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    zero_ready = [item["object_id"] for item in surface_readiness if item["readiness_status"] == "ready_empty_surface"]
    non_empty = [item["object_id"] for item in surface_readiness if item["readiness_status"] == "ready_non_empty_surface"]
    return {
        "job": "autocount_inventory_staging_readiness",
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "source_manifest_path": str(source_manifest_path),
        "source_extract": {
            "run_id": source_manifest.get("run_id"),
            "run_path": source_manifest.get("storage", {}).get("run_path"),
            "status": source_manifest.get("status"),
        },
        "source_manifest_fields_used": [
            "row_counts",
            "schema_metadata",
            "surface_exports",
            "selected_surfaces",
            "warnings",
            "guardrail_fields",
        ],
        "decision": source_manifest.get("decision", NEEDS_RECONCILIATION),
        "business_reconciliation_status": source_manifest.get(
            "business_reconciliation_status", BUSINESS_RECONCILIATION_STATUS
        ),
        "data_maturity": source_manifest.get("data_maturity", DATA_MATURITY),
        "final_production_selected": bool(source_manifest.get("final_production_selected", False)),
        "staging_readiness_decision": READINESS_DECISION if status == "success" else "blocked",
        "surface_readiness": surface_readiness,
        "zero_row_ready_surfaces": zero_ready,
        "non_empty_surfaces": non_empty,
        "warning_summary": warning_summary,
        "warnings": unique_preserve_order(warnings),
        "exception_count": len(exceptions),
        "exceptions": exceptions,
        "storage": {
            "output_root": plan["output_root"],
            "run_path": plan["run_path"],
            "manifest": "",
            "report": "",
        },
        "notes": [
            "This review reads extractor manifest/schema metadata only.",
            "It does not open raw CSV snapshot files or query AutoCount.",
            "Dashboard analytics are not meaningful yet because GRN/receive/transfer surfaces currently have 0 rows.",
        ],
    }


def render_report(manifest):
    lines = [
        "# Inventory Operation Staging Readiness Report",
        "",
        "This report is manifest-only. It does not read raw CSV rows and does not query AutoCount.",
        "",
        "## Source Extract",
        "",
        f"- Run path reviewed: {manifest.get('source_extract', {}).get('run_path')}",
        f"- Source extract run ID: {manifest.get('source_extract', {}).get('run_id')}",
        f"- Source extract status: {manifest.get('source_extract', {}).get('status')}",
        "",
        "## Guardrails",
        "",
        f"- Decision: {manifest['decision']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        "",
        "## Readiness Decision",
        "",
        f"- Staging readiness decision: {manifest['staging_readiness_decision']}",
        "- dashboard analytics are not meaningful yet because GRN/receive/transfer surfaces currently have 0 rows.",
        "",
        "## Surface Readiness",
        "",
    ]
    for item in manifest.get("surface_readiness", []):
        lines.append(f"- `{item['object_id']}`: {item['readiness_status']}, rows: {item['row_count']}, export: {item['export_status']}, schema: {str(item['schema_available']).lower()}")

    lines.extend(["", "## Zero-Row But Ready Surfaces", ""])
    for object_id in manifest.get("zero_row_ready_surfaces", []):
        lines.append(f"- `{object_id}`")
    lines.extend(["", "## Non-Empty Surfaces", ""])
    for object_id in manifest.get("non_empty_surfaces", []):
        lines.append(f"- `{object_id}`")
    lines.extend(["", "## Warning Summary", ""])
    if manifest.get("warning_summary"):
        for warning in manifest["warning_summary"]:
            lines.append(f"- {warning}")
    else:
        lines.append("- none")
    if manifest.get("exceptions"):
        lines.extend(["", "## Exceptions", ""])
        for exception in manifest["exceptions"]:
            lines.append(f"- {sanitize_text(exception)}")
    return "\n".join(lines) + "\n"


def sanitize_text(text):
    return re.sub(
        r"(?i)\b(password|pwd|token|secret|api[_ -]?key|access[_ -]?key)\b\s*[:=]\s*[^;\s]+",
        lambda match: f"{match.group(1)}=<redacted>",
        str(text),
    )


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
    parser = argparse.ArgumentParser(description="Review inventory operation staging readiness from extract manifest only.")
    parser.add_argument("--manifest", required=True, help="Path to inventory_operation_extract_manifest.json.")
    parser.add_argument("--output-root", help=r"Local output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_readiness(args.manifest, output_root=args.output_root)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "run_path": manifest["storage"]["run_path"],
                    "readiness_decision": manifest["staging_readiness_decision"],
                    "zero_row_ready_count": len(manifest["zero_row_ready_surfaces"]),
                    "non_empty_count": len(manifest["non_empty_surfaces"]),
                    "warning_count": len(manifest["warning_summary"]),
                },
                indent=2,
            )
        )
        return 0 if manifest["status"] == "success" else 1
    except Exception as exc:  # noqa: BLE001 - CLI should redact likely secret fragments.
        print(sanitize_text(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
