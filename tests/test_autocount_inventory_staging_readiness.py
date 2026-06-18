import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_inventory_staging_readiness as readiness


class InventoryStagingReadinessTests(unittest.TestCase):
    def test_run_readiness_reads_manifest_only_and_classifies_surfaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            raw_csv = tmp_path / "raw.csv"
            raw_csv.write_text("DocNo,ItemCode\nPO-001,ITEM-001\n", encoding="utf-8")
            manifest_path = tmp_path / "inventory_operation_extract_manifest.json"
            manifest_path.write_text(json.dumps(synthetic_extract_manifest(raw_csv)), encoding="utf-8")
            raw_csv.unlink()

            manifest = readiness.run_readiness(
                manifest_path,
                output_root=tmp_path / "review",
                now=datetime.fromisoformat("2026-06-18T17:00:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            report_text = (run_path / "inventory_staging_readiness_report.md").read_text(encoding="utf-8")

        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["staging_readiness_decision"], "schema_ready_data_thin")
        self.assertEqual(manifest["source_extract"]["status"], "success_with_warnings")
        self.assertEqual(manifest["decision"], "Needs reconciliation")
        self.assertEqual(manifest["data_maturity"], "immature_pre_go_live")
        self.assertEqual(manifest["business_reconciliation_status"], "not_reconciled")
        self.assertFalse(manifest["final_production_selected"])
        self.assertEqual(surface_status(manifest, "dbo.vGoodsReceivedNote"), "ready_empty_surface")
        self.assertEqual(surface_status(manifest, "dbo.PO"), "ready_non_empty_surface")
        self.assertIn("dashboard analytics are not meaningful yet", report_text)
        self.assertIn("surface_exports", manifest["source_manifest_fields_used"])
        self.assertNotIn("PO-001", report_text)
        self.assertNotIn("ITEM-001", report_text)

    def test_rejects_old_surface_results_manifest_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = synthetic_extract_manifest(Path(tmpdir) / "raw.csv")
            manifest["surface_results"] = manifest.pop("surface_exports")
            manifest_path = Path(tmpdir) / "inventory_operation_extract_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = readiness.run_readiness(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T17:00:00+08:00"),
            )

        self.assertEqual(result["status"], "failed")
        self.assertRegex("\n".join(result["exceptions"]), "surface_exports")
        self.assertRegex("\n".join(result["warnings"]), "surface_results")

    def test_invalid_manifest_path_and_output_root_inside_repo_are_rejected(self):
        with self.assertRaises(FileNotFoundError):
            readiness.load_manifest(Path("missing_inventory_operation_extract_manifest.json"))
        with self.assertRaises(ValueError):
            readiness.resolve_output_root(ROOT / "review_outputs")

    def test_report_and_manifest_include_warning_summary_and_guardrails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "inventory_operation_extract_manifest.json"
            source_manifest = synthetic_extract_manifest(Path(tmpdir) / "raw.csv")
            source_manifest["warnings"].append("missing_expected_columns:dbo.GR")
            manifest_path.write_text(json.dumps(source_manifest), encoding="utf-8")

            result = readiness.run_readiness(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T17:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        self.assertIn("missing_expected_columns:dbo.GR", result["warning_summary"])
        self.assertIn("Needs reconciliation", report_text)
        self.assertIn("not_reconciled", report_text)
        self.assertIn("immature_pre_go_live", report_text)
        self.assertIn("Final production selected: false", report_text)

    def test_blueprint_lists_future_tables_and_unresolved_location(self):
        blueprint = (
            ROOT / "docs" / "autocount2-automation" / "inventory_operation_staging_blueprint.md"
        ).read_text(encoding="utf-8")

        for table_name in [
            "stg_ac2_supplier",
            "stg_ac2_purchase_order_header",
            "stg_ac2_purchase_order_line",
            "stg_ac2_grn_header",
            "stg_ac2_grn_line",
            "stg_ac2_stock_receive_header",
            "stg_ac2_stock_receive_line",
            "stg_ac2_transfer_header",
            "stg_ac2_transfer_line",
            "stg_ac2_stock_movement_reference",
            "stg_ac2_item_balance_reference",
        ]:
            self.assertIn(table_name, blueprint)
        self.assertIn("location dimension", blueprint)
        self.assertIn("unresolved", blueprint.lower())
        self.assertIn("not implement loading", blueprint)

    def test_runbook_uses_surface_exports_and_safe_paste_back(self):
        runbook = (
            ROOT / "docs" / "autocount2-automation" / "inventory_operation_staging_readiness_runbook.md"
        ).read_text(encoding="utf-8")

        self.assertIn("python scripts\\autocount_inventory_staging_readiness.py --manifest $manifest", runbook)
        self.assertIn("surface_exports", runbook)
        self.assertNotIn("surface_results", runbook)
        self.assertIn("do not paste raw csv rows", runbook.lower())
        self.assertIn("readiness decision", runbook.lower())

    def test_local_review_outputs_are_ignored(self):
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("inventory_staging_readiness_outputs/", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_staging_readiness_manifest.json", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_staging_readiness_report.md", gitignore_text)


def synthetic_extract_manifest(raw_csv_path):
    return {
        "job": "autocount_inventory_operation_extract",
        "status": "success_with_warnings",
        "run_id": "extract-run-123",
        "storage": {
            "run_path": r"C:\XB\autocount_outputs\extract\inventory_operations\inventory_operation_extract_20260618_154313_a804be8d",
        },
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "warnings": ["Data may be immature/pre-go-live/test/partial."],
        "row_counts": {
            "dbo.vGoodsReceivedNote": 0,
            "dbo.PO": 1,
            "dbo.vCreditor": 70,
            "dbo.StockDTL": 2,
        },
        "selected_surfaces": [
            {"object_id": "dbo.vGoodsReceivedNote", "business_function": "grn_header"},
            {"object_id": "dbo.PO", "business_function": "outstanding_po_in_transit"},
        ],
        "surface_exports": [
            {
                "object_id": "dbo.vGoodsReceivedNote",
                "schema_name": "dbo",
                "object_name": "vGoodsReceivedNote",
                "business_function": "grn_header",
                "status": "success",
                "row_count": 0,
                "output_path": str(raw_csv_path),
                "file_name": raw_csv_path.name,
                "selected_columns": ["DocNo", "DocDate"],
                "missing_expected_columns": [],
            },
            {
                "object_id": "dbo.PO",
                "schema_name": "dbo",
                "object_name": "PO",
                "business_function": "outstanding_po_in_transit",
                "status": "success",
                "row_count": 1,
                "output_path": str(raw_csv_path),
                "file_name": raw_csv_path.name,
                "selected_columns": ["DocNo"],
                "missing_expected_columns": [],
            },
            {
                "object_id": "dbo.vCreditor",
                "schema_name": "dbo",
                "object_name": "vCreditor",
                "business_function": "supplier_context",
                "status": "success",
                "row_count": 70,
                "output_path": str(raw_csv_path),
                "file_name": raw_csv_path.name,
                "selected_columns": ["CreditorCode"],
                "missing_expected_columns": [],
            },
            {
                "object_id": "dbo.StockDTL",
                "schema_name": "dbo",
                "object_name": "StockDTL",
                "business_function": "movement_stock_reference",
                "status": "success",
                "row_count": 2,
                "output_path": str(raw_csv_path),
                "file_name": raw_csv_path.name,
                "selected_columns": ["DocType", "ItemCode"],
                "missing_expected_columns": [],
            },
        ],
        "schema_metadata": [
            {"object_id": "dbo.vGoodsReceivedNote", "selected_columns": ["DocNo", "DocDate"]},
            {"object_id": "dbo.PO", "selected_columns": ["DocNo"]},
            {"object_id": "dbo.vCreditor", "selected_columns": ["CreditorCode"]},
            {"object_id": "dbo.StockDTL", "selected_columns": ["DocType", "ItemCode"]},
        ],
    }


def surface_status(manifest, object_id):
    for surface in manifest["surface_readiness"]:
        if surface["object_id"] == object_id:
            return surface["readiness_status"]
    return None


if __name__ == "__main__":
    unittest.main()
