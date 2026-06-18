import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_inventory_pipeline_local_run as pipeline


class InventoryPipelineLocalRunTests(unittest.TestCase):
    def test_happy_path_keeps_no_dashboard_schema_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            extract = write_phase_manifest(root, "extract", "inventory_operation_extract_manifest.json", phase_manifest("extract", warnings=["extract_warning"]))
            build = write_phase_manifest(root, "build", "inventory_staging_build_manifest.json", phase_manifest("build", warnings=["build_warning"]))
            audit = write_phase_manifest(root, "audit", "inventory_staging_audit_manifest.json", phase_manifest("audit", warning_count=2))
            warning = write_phase_manifest(root, "warning", "inventory_staging_warning_review_manifest.json", phase_manifest("warning", warning_count=3))
            schema = write_phase_manifest(
                root,
                "schema",
                "inventory_staging_schema_gap_review_manifest.json",
                phase_manifest("schema", warning_count=4, recommendation="no_dashboard_until_schema_gap_resolved"),
            )

            with patched_phases(extract, build, audit, warning, schema):
                result = pipeline.run_pipeline_local_run(
                    extract_config={"job": "autocount_inventory_operation_extract"},
                    output_root=root / "pipeline",
                    now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
                )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["final_decision"], "no_dashboard_until_schema_gap_resolved")
        self.assertEqual(result["final_recommendation"], "no_dashboard_until_schema_gap_resolved")
        self.assertEqual(result["source_extract_run_path"], extract["storage"]["run_path"])
        self.assertEqual(result["staging_build_run_path"], build["storage"]["run_path"])
        self.assertEqual(result["audit_run_path"], audit["storage"]["run_path"])
        self.assertEqual(result["warning_review_run_path"], warning["storage"]["run_path"])
        self.assertEqual(result["schema_gap_review_run_path"], schema["storage"]["run_path"])
        self.assertEqual(result["row_counts_by_staging_table"]["stg_ac2_supplier"], 1)
        self.assertEqual(result["warning_counts_by_phase"]["schema_gap_review"], 4)
        self.assertEqual(result["exception_counts_by_phase"]["schema_gap_review"], 0)
        self.assertIn("Final decision: no_dashboard_until_schema_gap_resolved", report_text)

    def test_existing_extract_manifest_mode_skips_raw_extraction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            extract = write_phase_manifest(root, "extract", "inventory_operation_extract_manifest.json", phase_manifest("extract"))
            build = write_phase_manifest(root, "build", "inventory_staging_build_manifest.json", phase_manifest("build"))
            audit = write_phase_manifest(root, "audit", "inventory_staging_audit_manifest.json", phase_manifest("audit"))
            warning = write_phase_manifest(root, "warning", "inventory_staging_warning_review_manifest.json", phase_manifest("warning"))
            schema = write_phase_manifest(root, "schema", "inventory_staging_schema_gap_review_manifest.json", phase_manifest("schema"))
            run_extraction = Mock(side_effect=AssertionError("raw extraction should be skipped"))

            with patched_phases(extract, build, audit, warning, schema, run_extraction=run_extraction):
                result = pipeline.run_pipeline_local_run(
                    existing_extract_manifest=extract["storage"]["manifest"],
                    output_root=root / "pipeline",
                    now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
                )

        run_extraction.assert_not_called()
        self.assertEqual(result["phase_statuses"]["source_extract"], "skipped_existing_manifest")
        self.assertEqual(result["source_extract_run_path"], extract["storage"]["run_path"])

    def test_phase_exception_blocks_pipeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            extract = write_phase_manifest(root, "extract", "inventory_operation_extract_manifest.json", phase_manifest("extract"))
            build = write_phase_manifest(root, "build", "inventory_staging_build_manifest.json", phase_manifest("build"))
            audit = write_phase_manifest(
                root,
                "audit",
                "inventory_staging_audit_manifest.json",
                phase_manifest("audit", status="failed", exception_count=1),
            )
            warning = phase_manifest("warning")
            schema = phase_manifest("schema")

            with patched_phases(extract, build, audit, warning, schema):
                result = pipeline.run_pipeline_local_run(
                    existing_extract_manifest=extract["storage"]["manifest"],
                    output_root=root / "pipeline",
                    now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["final_decision"], "pipeline_blocked_by_exceptions")
        self.assertEqual(result["exception_counts_by_phase"]["audit"], 1)
        self.assertEqual(result["warning_review_run_path"], "")
        self.assertEqual(result["schema_gap_review_run_path"], "")

    def test_missing_phase_manifest_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            extract = write_phase_manifest(root, "extract", "inventory_operation_extract_manifest.json", phase_manifest("extract"))
            build = phase_manifest("build")
            build["storage"]["manifest"] = str(root / "missing" / "inventory_staging_build_manifest.json")
            audit = phase_manifest("audit")
            warning = phase_manifest("warning")
            schema = phase_manifest("schema")

            with patched_phases(extract, build, audit, warning, schema):
                result = pipeline.run_pipeline_local_run(
                    existing_extract_manifest=extract["storage"]["manifest"],
                    output_root=root / "pipeline",
                    now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["final_decision"], "pipeline_blocked_by_exceptions")
        self.assertEqual(result["exception_counts_by_phase"]["staging_build"], 1)

    def test_output_root_inside_repo_is_refused(self):
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            pipeline.resolve_output_root(ROOT / "local_pipeline_output")

    def test_report_does_not_leak_raw_synthetic_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_values = ["SUP-001", "PO-001", "ITEM-001", "Synthetic Supplier", "Synthetic item", "MAIN"]
            extract = write_phase_manifest(root, "extract", "inventory_operation_extract_manifest.json", phase_manifest("extract", warnings=raw_values))
            build = write_phase_manifest(root, "build", "inventory_staging_build_manifest.json", phase_manifest("build", warnings=raw_values))
            audit = write_phase_manifest(root, "audit", "inventory_staging_audit_manifest.json", phase_manifest("audit", warning_count=2))
            warning = write_phase_manifest(root, "warning", "inventory_staging_warning_review_manifest.json", phase_manifest("warning", warning_count=3))
            schema = write_phase_manifest(root, "schema", "inventory_staging_schema_gap_review_manifest.json", phase_manifest("schema", warning_count=4))

            with patched_phases(extract, build, audit, warning, schema):
                result = pipeline.run_pipeline_local_run(
                    existing_extract_manifest=extract["storage"]["manifest"],
                    output_root=root / "pipeline",
                    now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
                )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        for raw_value in raw_values:
            self.assertNotIn(raw_value, report_text)

    def test_safety_fields_remain_forced_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            extract = write_phase_manifest(root, "extract", "inventory_operation_extract_manifest.json", phase_manifest("extract"))
            build = write_phase_manifest(root, "build", "inventory_staging_build_manifest.json", phase_manifest("build"))
            audit = write_phase_manifest(root, "audit", "inventory_staging_audit_manifest.json", phase_manifest("audit"))
            warning = write_phase_manifest(root, "warning", "inventory_staging_warning_review_manifest.json", phase_manifest("warning"))
            schema = write_phase_manifest(root, "schema", "inventory_staging_schema_gap_review_manifest.json", phase_manifest("schema"))

            with patched_phases(extract, build, audit, warning, schema):
                result = pipeline.run_pipeline_local_run(
                    existing_extract_manifest=extract["storage"]["manifest"],
                    output_root=root / "pipeline",
                    now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
                )

        self.assertEqual(result["decision"], "Needs reconciliation")
        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])


def patched_phases(extract, build, audit, warning, schema, run_extraction=None):
    return patch.multiple(
        pipeline,
        run_extraction=run_extraction or Mock(return_value=extract),
        run_staging_build=Mock(return_value=build),
        run_staging_audit=Mock(return_value=audit),
        run_warning_review=Mock(return_value=warning),
        run_schema_gap_review=Mock(return_value=schema),
    )


def write_phase_manifest(root, phase_dir, filename, manifest):
    run_path = root / phase_dir
    run_path.mkdir(parents=True, exist_ok=True)
    manifest_path = run_path / filename
    manifest["storage"]["run_path"] = str(run_path)
    manifest["storage"]["manifest"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def phase_manifest(phase, status="success_with_warnings", warnings=None, warning_count=None, exception_count=0, recommendation=None):
    row_counts = {
        "stg_ac2_supplier": 1,
        "stg_ac2_purchase_order_header": 1,
        "stg_ac2_purchase_order_line": 1,
        "stg_ac2_grn_header": 0,
        "stg_ac2_grn_line": 0,
        "stg_ac2_stock_receive_header": 0,
        "stg_ac2_stock_receive_line": 0,
        "stg_ac2_transfer_header": 0,
        "stg_ac2_transfer_line": 0,
        "stg_ac2_stock_movement_reference": 1,
        "stg_ac2_item_balance_reference": 1,
    }
    warnings = list(warnings or [])
    manifest = {
        "job": f"autocount_inventory_{phase}",
        "status": status,
        "run_id": f"{phase}-run",
        "row_counts": row_counts,
        "warnings": warnings,
        "warning_count": len(warnings) if warning_count is None else warning_count,
        "exception_count": exception_count,
        "exceptions": ["synthetic_exception"] if exception_count else [],
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {"output_root": "", "run_path": "", "manifest": "", "report": ""},
    }
    if phase == "schema":
        manifest.update(
            {
                "review_conclusion": recommendation or "no_dashboard_until_schema_gap_resolved",
                "recommendation": recommendation or "no_dashboard_until_schema_gap_resolved",
                "data_thin_tables": [{"staging_table": "stg_ac2_grn_line"}],
            }
        )
    return manifest


if __name__ == "__main__":
    unittest.main()
