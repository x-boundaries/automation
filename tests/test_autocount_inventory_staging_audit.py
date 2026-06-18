import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_inventory_staging_audit as audit


class InventoryStagingAuditTests(unittest.TestCase):
    def test_happy_path_classifies_contract_ready_data_thin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            staging_run.mkdir()
            manifest_path = write_fixture_staging_build(staging_run)

            result = audit.run_staging_audit(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T19:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["audit_decision"], "staging_contract_ready_data_thin")
        self.assertEqual(len(result["staging_tables"]), 11)
        self.assertEqual(result["row_counts"]["stg_ac2_supplier"], 2)
        self.assertIn("stg_ac2_grn_header", result["zero_row_tables"])
        self.assertEqual(result["missing_files"], [])
        self.assertEqual(result["missing_headers"], [])
        self.assertEqual(result["decision"], "Needs reconciliation")
        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])
        self.assertIn("missing_expected_columns:dbo.GRDTL", result["carried_forward_warnings"])
        self.assertIn("outstanding_qty_candidate_not_numeric:dbo.PODTL", result["carried_forward_warnings"])
        self.assertIn("duplicate_source_surfaces_review:stg_ac2_supplier", result["warnings"])
        self.assertIn("duplicate_source_surfaces_review:stg_ac2_purchase_order_header", result["warnings"])

    def test_missing_staging_file_fails_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            staging_run.mkdir()
            manifest_path = write_fixture_staging_build(staging_run)
            (staging_run / "stg_ac2_supplier.csv").unlink()

            result = audit.run_staging_audit(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T19:00:00+08:00"),
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("stg_ac2_supplier", result["missing_files"])
        self.assertEqual(result["audit_decision"], "staging_contract_failed")

    def test_missing_required_lineage_header_fails_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            staging_run.mkdir()
            manifest_path = write_fixture_staging_build(staging_run)
            write_csv(
                staging_run / "stg_ac2_purchase_order_line.csv",
                [
                    "po_doc_key",
                    "po_dtl_key",
                    "item_code",
                    "description",
                    "uom",
                    "qty",
                    "transferred_qty",
                    "location",
                    "outstanding_qty_candidate",
                    "source_surface",
                    "source_extract_run_id",
                ],
                [],
            )

            result = audit.run_staging_audit(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T19:00:00+08:00"),
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            {"table_name": "stg_ac2_purchase_order_line", "header": "source_row_number"},
            result["missing_headers"],
        )

    def test_zero_row_staging_files_with_headers_pass_as_data_thin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            staging_run.mkdir()
            manifest_path = write_fixture_staging_build(staging_run, all_zero=True)

            result = audit.run_staging_audit(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T19:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["audit_decision"], "staging_contract_ready_data_thin")
        self.assertEqual(len(result["zero_row_tables"]), 11)
        self.assertEqual(result["exception_count"], 0)

    def test_manifest_with_final_production_selected_true_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            staging_run.mkdir()
            manifest_path = write_fixture_staging_build(staging_run)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["final_production_selected"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = audit.run_staging_audit(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T19:00:00+08:00"),
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("unsafe_manifest_final_production_selected", result["exceptions"])
        self.assertFalse(result["final_production_selected"])

    def test_path_traversal_outside_staging_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            outside = Path(tmpdir) / "outside.csv"
            staging_run.mkdir()
            outside.write_text("supplier_code,source_surface,source_extract_run_id,source_row_number\nLEAK,dbo.X,run,1\n", encoding="utf-8")
            manifest_path = write_fixture_staging_build(staging_run)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["staging_tables"][0]["output_path"] = str(outside)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = audit.run_staging_audit(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T19:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        self.assertEqual(result["status"], "failed")
        self.assertIn("staging_csv_outside_run:stg_ac2_supplier", result["exceptions"])
        self.assertNotIn("LEAK", json.dumps(result))
        self.assertNotIn("LEAK", report_text)

    def test_report_does_not_leak_raw_row_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            staging_run.mkdir()
            manifest_path = write_fixture_staging_build(staging_run)

            result = audit.run_staging_audit(
                manifest_path,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T19:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        for raw_value in ["SUP-001", "PO-001", "ITEM-001", "Synthetic Supplier"]:
            self.assertNotIn(raw_value, report_text)
        self.assertIn("stg_ac2_supplier", report_text)
        self.assertIn("inventory_staging_audit_manifest.json", report_text)

    def test_example_config_runbook_and_gitignore_are_safe(self):
        config = json.loads((ROOT / "config" / "autocount_inventory_staging_audit.example.json").read_text())
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\review\inventory_staging_audit")
        self.assertNotIn("connection_string", config)
        runbook = (
            ROOT / "docs" / "autocount2-automation" / "inventory_operation_staging_audit_runbook.md"
        ).read_text(encoding="utf-8")
        self.assertIn("python scripts\\autocount_inventory_staging_audit.py --manifest $manifest", runbook)
        self.assertIn("do not paste raw staging csv rows", runbook.lower())
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("inventory_staging_audit_outputs/", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_staging_audit_manifest.json", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_staging_audit_report.md", gitignore_text)


def write_fixture_staging_build(staging_run, all_zero=False):
    rows_by_table = {
        "stg_ac2_supplier": [
            {
                "supplier_code": "SUP-001",
                "supplier_name": "Synthetic Supplier",
                "source_surface": "dbo.vCreditor",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "1",
            },
            {
                "supplier_code": "SUP-001",
                "supplier_name": "Synthetic Supplier",
                "source_surface": "dbo.Creditor",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "2",
            },
        ],
        "stg_ac2_purchase_order_header": [
            {
                "po_doc_key": "100",
                "po_doc_no": "PO-001",
                "po_doc_date": "2026-06-18",
                "supplier_code": "SUP-001",
                "supplier_name": "Synthetic Supplier",
                "purchase_location": "MAIN",
                "doc_status": "OPEN",
                "cancelled": "F",
                "source_surface": "dbo.vPurchaseOrder",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "1",
            },
            {
                "po_doc_key": "101",
                "po_doc_no": "PO-001",
                "po_doc_date": "2026-06-18",
                "supplier_code": "SUP-001",
                "supplier_name": "Synthetic Supplier",
                "purchase_location": "MAIN",
                "doc_status": "OPEN",
                "cancelled": "F",
                "source_surface": "dbo.PO",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "2",
            },
        ],
        "stg_ac2_purchase_order_line": [
            {
                "po_doc_key": "100",
                "po_dtl_key": "200",
                "item_code": "ITEM-001",
                "description": "Synthetic item",
                "uom": "PCS",
                "qty": "10",
                "transferred_qty": "2",
                "location": "MAIN",
                "outstanding_qty_candidate": "8",
                "source_surface": "dbo.PODTL",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "1",
            }
        ],
        "stg_ac2_grn_header": [],
        "stg_ac2_grn_line": [],
        "stg_ac2_stock_receive_header": [],
        "stg_ac2_stock_receive_line": [],
        "stg_ac2_transfer_header": [],
        "stg_ac2_transfer_line": [],
        "stg_ac2_stock_movement_reference": [
            {
                "doc_type": "PO",
                "item_code": "ITEM-001",
                "source_surface": "dbo.StockDTL",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "1",
            }
        ],
        "stg_ac2_item_balance_reference": [
            {
                "item_code": "ITEM-001",
                "source_surface": "dbo.vItemBalQty",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "1",
            }
        ],
    }
    if all_zero:
        rows_by_table = {table: [] for table in rows_by_table}

    staging_tables = []
    for table_name, rows in rows_by_table.items():
        columns = audit.EXPECTED_STAGING_TABLES[table_name]
        write_csv(staging_run / f"{table_name}.csv", columns, rows)
        staging_tables.append(
            {
                "table_name": table_name,
                "row_count": len(rows),
                "output_path": str(staging_run / f"{table_name}.csv"),
                "file_name": f"{table_name}.csv",
                "status": "ready_empty_surface" if not rows else "ready_non_empty_surface",
                "decision": "Needs reconciliation",
                "final_production_selected": False,
            }
        )

    manifest = {
        "job": "autocount_inventory_staging_build",
        "status": "success_with_warnings",
        "run_id": "staging-build-run-123",
        "source_extract_run_id": "extract-run-123",
        "source_extract_run_path": str(staging_run.parent / "extract_run"),
        "staging_tables": staging_tables,
        "row_counts": {table["table_name"]: table["row_count"] for table in staging_tables},
        "warnings": [
            "missing_expected_columns:dbo.GRDTL",
            "outstanding_qty_candidate_not_numeric:dbo.PODTL",
            "source_surface_missing:dbo.vStockTransferDetail",
        ],
        "exception_count": 0,
        "exceptions": [],
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "output_root": str(staging_run.parent),
            "run_path": str(staging_run),
            "manifest": str(staging_run / "inventory_staging_build_manifest.json"),
            "report": str(staging_run / "inventory_staging_build_report.md"),
        },
    }
    manifest_path = staging_run / "inventory_staging_build_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
