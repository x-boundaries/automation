import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import autocount_inventory_operation_extract as operation_extract  # noqa: E402
import autocount_inventory_staging_audit as staging_audit  # noqa: E402
import autocount_inventory_staging_build as staging_build  # noqa: E402
import autocount_inventory_staging_schema_gap_review as schema_gap_review  # noqa: E402
import autocount_inventory_staging_warning_review as warning_review  # noqa: E402


DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\review\inventory_pipeline_local_run"
DEFAULT_EXTRACT_CONFIG_PATH = operation_extract.DEFAULT_CONFIG_PATH
NEEDS_RECONCILIATION = "Needs reconciliation"
DATA_MATURITY = "immature_pre_go_live"
BUSINESS_RECONCILIATION_STATUS = "not_reconciled"

PHASES = [
    "source_extract",
    "staging_build",
    "audit",
    "warning_review",
    "schema_gap_review",
]
RUN_PATH_FIELDS = {
    "source_extract": "source_extract_run_path",
    "staging_build": "staging_build_run_path",
    "audit": "audit_run_path",
    "warning_review": "warning_review_run_path",
    "schema_gap_review": "schema_gap_review_run_path",
}
OUTPUT_FILES = [
    "inventory_pipeline_local_run_manifest.json",
    "inventory_pipeline_local_run_report.md",
]


def run_extraction(config, output_root=None, now=None):
    return operation_extract.run_extraction(config, output_root=output_root, now=now)


def run_staging_build(manifest_path, output_root=None, now=None):
    return staging_build.run_staging_build(
        manifest_path,
        output_root=output_root,
        allow_warning_source=True,
        now=now,
    )


def run_staging_audit(manifest_path, output_root=None, now=None):
    return staging_audit.run_staging_audit(manifest_path, output_root=output_root, now=now)


def run_warning_review(manifest_path, output_root=None, now=None):
    return warning_review.run_warning_review(manifest_path, output_root=output_root, now=now)


def run_schema_gap_review(manifest_path, output_root=None, now=None):
    return schema_gap_review.run_schema_gap_review(manifest_path, output_root=output_root, now=now)


def load_manifest(path):
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"phase_manifest_missing:{sanitize_text(manifest_path)}")
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def run_pipeline_local_run(
    extract_config=None,
    existing_extract_manifest=None,
    output_root=None,
    phase_output_roots=None,
    now=None,
):
    plan = build_run_plan(output_root, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)
    phase_output_roots = dict(phase_output_roots or {})
    phase_manifests = {}
    phase_statuses = {phase: "" for phase in PHASES}
    warning_counts = {phase: 0 for phase in PHASES}
    exception_counts = {phase: 0 for phase in PHASES}
    run_paths = {field: "" for field in RUN_PATH_FIELDS.values()}
    pipeline_notes = []
    blocked = False

    def record_phase(phase, manifest, status_override=None):
        phase_manifests[phase] = manifest
        phase_statuses[phase] = status_override or manifest.get("status", "")
        warning_counts[phase] = warning_count_for(manifest)
        exception_counts[phase] = exception_count_for(manifest)
        run_paths[RUN_PATH_FIELDS[phase]] = str(manifest.get("storage", {}).get("run_path") or "")
        if manifest.get("status") == "failed" and exception_counts[phase] == 0:
            exception_counts[phase] = 1
        manifest_path = manifest.get("storage", {}).get("manifest")
        if not manifest_path or not Path(manifest_path).exists():
            exception_counts[phase] = max(exception_counts[phase], 1)
            phase_statuses[phase] = "failed_missing_manifest"
            pipeline_notes.append(f"{phase}_manifest_missing")
            return None
        return manifest_path

    try:
        if existing_extract_manifest:
            extract_manifest = load_manifest(existing_extract_manifest)
            extract_manifest_path = record_phase(
                "source_extract",
                extract_manifest,
                status_override="skipped_existing_manifest",
            )
        else:
            config = dict(extract_config or {})
            extract_manifest = run_extraction(
                config,
                output_root=phase_output_roots.get("source_extract"),
                now=now,
            )
            extract_manifest_path = record_phase("source_extract", extract_manifest)
    except Exception as exc:  # noqa: BLE001 - final local manifest should summarize safely.
        exception_counts["source_extract"] = 1
        phase_statuses["source_extract"] = "failed"
        pipeline_notes.append(sanitize_text(str(exc)))
        extract_manifest_path = None

    blocked = blocked or phase_blocked("source_extract", phase_statuses, exception_counts)

    if not blocked and extract_manifest_path:
        build_manifest_path = run_and_record_phase(
            "staging_build",
            lambda: run_staging_build(
                extract_manifest_path,
                output_root=phase_output_roots.get("staging_build"),
                now=now,
            ),
            record_phase,
            phase_statuses,
            exception_counts,
            pipeline_notes,
        )
        blocked = blocked or phase_blocked("staging_build", phase_statuses, exception_counts)
    else:
        build_manifest_path = None

    if not blocked and build_manifest_path:
        audit_manifest_path = run_and_record_phase(
            "audit",
            lambda: run_staging_audit(
                build_manifest_path,
                output_root=phase_output_roots.get("audit"),
                now=now,
            ),
            record_phase,
            phase_statuses,
            exception_counts,
            pipeline_notes,
        )
        blocked = blocked or phase_blocked("audit", phase_statuses, exception_counts)
    else:
        audit_manifest_path = None

    if not blocked and audit_manifest_path:
        warning_manifest_path = run_and_record_phase(
            "warning_review",
            lambda: run_warning_review(
                audit_manifest_path,
                output_root=phase_output_roots.get("warning_review"),
                now=now,
            ),
            record_phase,
            phase_statuses,
            exception_counts,
            pipeline_notes,
        )
        blocked = blocked or phase_blocked("warning_review", phase_statuses, exception_counts)
    else:
        warning_manifest_path = None

    if not blocked and warning_manifest_path:
        run_and_record_phase(
            "schema_gap_review",
            lambda: run_schema_gap_review(
                warning_manifest_path,
                output_root=phase_output_roots.get("schema_gap_review"),
                now=now,
            ),
            record_phase,
            phase_statuses,
            exception_counts,
            pipeline_notes,
        )

    final_manifest = build_pipeline_manifest(
        plan=plan,
        phase_manifests=phase_manifests,
        phase_statuses=phase_statuses,
        warning_counts=warning_counts,
        exception_counts=exception_counts,
        run_paths=run_paths,
        pipeline_notes=pipeline_notes,
        now=now,
    )
    manifest_out = run_path / OUTPUT_FILES[0]
    report_out = run_path / OUTPUT_FILES[1]
    final_manifest["storage"]["manifest"] = str(manifest_out)
    final_manifest["storage"]["report"] = str(report_out)
    manifest_out.write_text(json.dumps(normalize_for_json(final_manifest), indent=2, sort_keys=True), encoding="utf-8")
    report_out.write_text(render_report(final_manifest), encoding="utf-8")
    return normalize_for_json(final_manifest)


def run_and_record_phase(
    phase,
    runner,
    record_phase,
    phase_statuses,
    exception_counts,
    pipeline_notes,
):
    try:
        manifest = runner()
        return record_phase(phase, manifest)
    except Exception as exc:  # noqa: BLE001 - final local manifest should summarize safely.
        phase_statuses[phase] = "failed"
        exception_counts[phase] = max(exception_counts[phase], 1)
        pipeline_notes.append(f"{phase}_failed:{sanitize_text(exc)}")
        return None


def build_pipeline_manifest(
    plan,
    phase_manifests,
    phase_statuses,
    warning_counts,
    exception_counts,
    run_paths,
    pipeline_notes,
    now=None,
):
    finished_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    schema_manifest = phase_manifests.get("schema_gap_review", {})
    final_decision = determine_final_decision(schema_manifest, phase_statuses, exception_counts)
    row_counts = collect_staging_row_counts(phase_manifests)
    status = determine_status(warning_counts, exception_counts)
    return {
        "job": "autocount_inventory_pipeline_local_run",
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": finished_at.isoformat(),
        "final_decision": final_decision,
        "final_recommendation": final_decision,
        **run_paths,
        "row_counts_by_staging_table": row_counts,
        "warning_counts_by_phase": warning_counts,
        "exception_counts_by_phase": exception_counts,
        "phase_statuses": phase_statuses,
        "generated_files": list(OUTPUT_FILES),
        "decision": NEEDS_RECONCILIATION,
        "business_reconciliation_status": BUSINESS_RECONCILIATION_STATUS,
        "data_maturity": DATA_MATURITY,
        "final_production_selected": False,
        "storage": {"output_root": plan["output_root"], "run_path": plan["run_path"], "manifest": "", "report": ""},
        "notes": unique_preserve_order(
            [
                "Read-only local operator bundle. It orchestrates approved local pipeline phases and summarizes metadata only.",
                "No raw CSV rows, raw ERP/business identifiers, dashboards, joins, reconciled dimensions, DB loads, scheduler, or write-back are produced.",
                "CoA, GL, bank, and accounting migration scope remain parked.",
                *[sanitize_text(note) for note in pipeline_notes],
            ]
        ),
    }


def determine_final_decision(schema_manifest, phase_statuses, exception_counts):
    if any(count > 0 for count in exception_counts.values()) or any(status.startswith("failed") for status in phase_statuses.values()):
        return "pipeline_blocked_by_exceptions"
    recommendation = schema_manifest.get("recommendation") or schema_manifest.get("review_conclusion")
    if recommendation == "no_dashboard_until_schema_gap_resolved":
        return "no_dashboard_until_schema_gap_resolved"
    if schema_manifest.get("data_thin_tables"):
        return "pipeline_ready_for_reconciliation_data_thin"
    return recommendation or "pipeline_ready_for_reconciliation_data_thin"


def determine_status(warning_counts, exception_counts):
    if any(count > 0 for count in exception_counts.values()):
        return "failed"
    if any(count > 0 for count in warning_counts.values()):
        return "success_with_warnings"
    return "success"


def collect_staging_row_counts(phase_manifests):
    for phase in ["schema_gap_review", "warning_review", "audit", "staging_build"]:
        row_counts = phase_manifests.get(phase, {}).get("row_counts")
        if isinstance(row_counts, dict) and row_counts:
            return {str(table): int(count or 0) for table, count in row_counts.items()}
    return {}


def warning_count_for(manifest):
    if "warning_count" in manifest:
        return int(manifest.get("warning_count") or 0)
    warnings = manifest.get("warnings")
    return len(warnings) if isinstance(warnings, list) else 0


def exception_count_for(manifest):
    if "exception_count" in manifest:
        return int(manifest.get("exception_count") or 0)
    exceptions = manifest.get("exceptions")
    return len(exceptions) if isinstance(exceptions, list) else 0


def phase_blocked(phase, phase_statuses, exception_counts):
    return exception_counts.get(phase, 0) > 0 or str(phase_statuses.get(phase, "")).startswith("failed")


def build_run_plan(output_root=None, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    resolved_output_root = resolve_output_root(output_root or DEFAULT_OUTPUT_ROOT)
    run_id = str(uuid4())
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "output_root": str(resolved_output_root),
        "run_path": str(
            resolved_output_root / f"inventory_pipeline_local_run_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"
        ),
    }


def resolve_output_root(output_root, repo_root=None):
    resolved = Path(output_root).expanduser().resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def render_report(manifest):
    lines = [
        "# Inventory Pipeline Local Run Report",
        "",
        "This is a read-only local operator summary. It does not paste raw CSV rows, query dashboards, load a database, schedule jobs, or write back to AutoCount.",
        "",
        "## Safe Summary",
        "",
        f"- Status: {manifest['status']}",
        f"- Final decision: {manifest['final_decision']}",
        f"- Final recommendation: {manifest['final_recommendation']}",
        f"- Decision: {manifest['decision']}",
        f"- Business reconciliation status: {manifest['business_reconciliation_status']}",
        f"- Data maturity: {manifest['data_maturity']}",
        f"- Final production selected: {str(manifest['final_production_selected']).lower()}",
        "",
        "## Phase Run Paths",
        "",
        f"- Source extract run path: {manifest['source_extract_run_path'] or 'not_run'}",
        f"- Staging build run path: {manifest['staging_build_run_path'] or 'not_run'}",
        f"- Audit run path: {manifest['audit_run_path'] or 'not_run'}",
        f"- Warning review run path: {manifest['warning_review_run_path'] or 'not_run'}",
        f"- Schema gap review run path: {manifest['schema_gap_review_run_path'] or 'not_run'}",
        "",
        "## Row Counts By Staging Table",
        "",
    ]
    for table_name, row_count in manifest.get("row_counts_by_staging_table", {}).items():
        lines.append(f"- `{table_name}`: {row_count}")
    lines.extend(["", "## Warning Counts By Phase", ""])
    for phase, count in manifest.get("warning_counts_by_phase", {}).items():
        lines.append(f"- `{phase}`: {count}")
    lines.extend(["", "## Exception Counts By Phase", ""])
    for phase, count in manifest.get("exception_counts_by_phase", {}).items():
        lines.append(f"- `{phase}`: {count}")
    lines.extend(["", "## Generated Files", ""])
    for filename in manifest.get("generated_files", []):
        lines.append(f"- `{filename}`")
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Read-only only.",
            "- Do not commit generated outputs, local configs, credentials, screenshots, production connection strings, raw CSVs, or raw ERP/business rows.",
            "- Do not build dashboards, purchase recommendations, KPIs, joins, reconciled dimensions, DB loads, scheduler, API behavior, or write-back from this run.",
            "- CoA, GL, bank, and accounting migration remain parked.",
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
    parser = argparse.ArgumentParser(description="Run the read-only local AutoCount inventory pipeline chain.")
    parser.add_argument("--existing-extract-manifest", help="Path to an existing inventory_operation_extract_manifest.json.")
    parser.add_argument(
        "--extract-config",
        default=str(DEFAULT_EXTRACT_CONFIG_PATH),
        help="Path to the secret-free extract config used when not starting from an existing extract manifest.",
    )
    parser.add_argument("--output-root", help=r"Final local pipeline summary output root outside the repo.")
    parser.add_argument("--extract-output-root", help=r"Optional raw extract phase output root outside the repo.")
    parser.add_argument("--staging-output-root", help=r"Optional staging build phase output root outside the repo.")
    parser.add_argument("--audit-output-root", help=r"Optional staging audit phase output root outside the repo.")
    parser.add_argument("--warning-review-output-root", help=r"Optional warning review phase output root outside the repo.")
    parser.add_argument("--schema-gap-review-output-root", help=r"Optional schema gap review phase output root outside the repo.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        extract_config = None
        if not args.existing_extract_manifest:
            extract_config = operation_extract.load_config(args.extract_config)
        manifest = run_pipeline_local_run(
            extract_config=extract_config,
            existing_extract_manifest=args.existing_extract_manifest,
            output_root=args.output_root,
            phase_output_roots={
                "source_extract": args.extract_output_root,
                "staging_build": args.staging_output_root,
                "audit": args.audit_output_root,
                "warning_review": args.warning_review_output_root,
                "schema_gap_review": args.schema_gap_review_output_root,
            },
        )
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "final_decision": manifest["final_decision"],
                    "final_recommendation": manifest["final_recommendation"],
                    "run_path": manifest["storage"]["run_path"],
                    "source_extract_run_path": manifest["source_extract_run_path"],
                    "staging_build_run_path": manifest["staging_build_run_path"],
                    "audit_run_path": manifest["audit_run_path"],
                    "warning_review_run_path": manifest["warning_review_run_path"],
                    "schema_gap_review_run_path": manifest["schema_gap_review_run_path"],
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
