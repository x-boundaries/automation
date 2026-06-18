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

from scripts import autocount_inventory_staging_warning_review as review


class InventoryStagingWarningReviewTests(unittest.TestCase):
    def test_warning_classification_happy_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            audit_run = Path(tmpdir) / "audit_run"
            staging_run.mkdir()
            audit_run.mkdir()
            audit_manifest = write_fixture_run(staging_run, audit_run)

            result = review.run_warning_review(
                audit_manifest,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T20:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["readiness_conclusion"], "staging_warning_review_ready_data_thin")
        self.assertEqual(result["decision"], "Needs reconciliation")
        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])
        classifications = {item["warning_code"]: item for item in result["warning_classifications"]}
        self.assertEqual(classifications["missing_expected_columns:dbo.GRDTL"]["classification"], "source_schema_gap")
        self.assertEqual(
            classifications["outstanding_qty_candidate_not_numeric:dbo.PODTL"]["classification"],
            "numeric_candidate_not_computable",
        )
        self.assertEqual(classifications["operational_movement_rows_data_thin"]["classification"], "source_data_thin")
        self.assertEqual(
            classifications["duplicate_source_surfaces_review:stg_ac2_supplier"]["classification"],
            "review_only_duplicate_source_overlap",
        )
        self.assertIn("missing_expected_columns:dbo.GRDTL", result["carried_forward_warning_codes"])
        self.assertIn("inventory_staging_warning_review_manifest.json", result["generated_files"])
        self.assertEqual(result["exception_count"], 0)

    def test_missing_expected_columns_classified_as_source_schema_gap(self):
        classification = review.classify_warning("missing_expected_columns:dbo.vStockTransferDetail")

        self.assertEqual(classification["classification"], "source_schema_gap")
        self.assertEqual(classification["dashboard_impact"], "dashboard_blocker_when_data_arrives")
        self.assertIn("dbo.vStockTransferDetail", classification["source"])

    def test_warning_review_carries_safe_source_column_diagnostics_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            audit_run = Path(tmpdir) / "audit_run"
            extract_run = Path(tmpdir) / "extract_run"
            staging_run.mkdir()
            audit_run.mkdir()
            extract_run.mkdir()
            audit_manifest = write_fixture_run(staging_run, audit_run, extract_run=extract_run)

            result = review.run_warning_review(
                audit_manifest,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T20:00:00+08:00"),
            )

        diagnostic = next(item for item in result["source_column_diagnostics"] if item["source_surface"] == "dbo.GRDTL")
        self.assertEqual(diagnostic["staging_table"], "stg_ac2_grn_line")
        self.assertEqual(diagnostic["expected_source_columns"], ["DocNo", "DtlKey", "ItemCode"])
        self.assertEqual(diagnostic["present_source_columns"], ["DocNo", "ItemCode"])
        self.assertEqual(diagnostic["missing_source_columns"], ["DtlKey"])
        self.assertEqual(diagnostic["dependent_staging_fields"], {"DtlKey": ["grn_dtl_key"]})
        self.assertEqual(diagnostic["classification"], "true_source_column_gap")
        self.assertEqual(diagnostic["dashboard_impact"], "dashboard_blocker_when_data_arrives")

    def test_zero_row_operational_tables_are_source_data_thin_not_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            audit_run = Path(tmpdir) / "audit_run"
            staging_run.mkdir()
            audit_run.mkdir()
            audit_manifest = write_fixture_run(staging_run, audit_run, all_operational_zero=True)

            result = review.run_warning_review(
                audit_manifest,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T20:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["readiness_conclusion"], "staging_warning_review_ready_data_thin")
        self.assertEqual(result["exceptions"], [])
        data_thin = [item for item in result["warning_classifications"] if item["classification"] == "source_data_thin"]
        self.assertTrue(data_thin)

    def test_non_numeric_candidate_warning_classified(self):
        classification = review.classify_warning("outstanding_qty_candidate_not_numeric:dbo.PODTL")

        self.assertEqual(classification["classification"], "numeric_candidate_not_computable")
        self.assertEqual(classification["dashboard_impact"], "dashboard_blocker_when_data_arrives")
        self.assertIn("outstanding_qty_candidate", classification["explanation"])

    def test_report_does_not_leak_raw_synthetic_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            audit_run = Path(tmpdir) / "audit_run"
            staging_run.mkdir()
            audit_run.mkdir()
            audit_manifest = write_fixture_run(staging_run, audit_run)

            result = review.run_warning_review(
                audit_manifest,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T20:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        for raw_value in ["SUP-001", "PO-001", "ITEM-001", "Synthetic Supplier", "Synthetic item", "PCS", "MAIN"]:
            self.assertNotIn(raw_value, report_text)
        self.assertIn("missing_expected_columns:dbo.GRDTL", report_text)
        self.assertIn("stg_ac2_purchase_order_line", report_text)

    def test_path_traversal_outside_staging_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            audit_run = Path(tmpdir) / "audit_run"
            outside = Path(tmpdir) / "outside.csv"
            staging_run.mkdir()
            audit_run.mkdir()
            outside.write_text("supplier_code,source_surface,source_extract_run_id,source_row_number\nLEAK,dbo.X,run,1\n", encoding="utf-8")
            audit_manifest = write_fixture_run(staging_run, audit_run)
            build_manifest_path = staging_run / "inventory_staging_build_manifest.json"
            build_manifest = json.loads(build_manifest_path.read_text(encoding="utf-8"))
            build_manifest["staging_tables"][0]["output_path"] = str(outside)
            build_manifest_path.write_text(json.dumps(build_manifest), encoding="utf-8")

            result = review.run_warning_review(
                audit_manifest,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T20:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        self.assertEqual(result["status"], "failed")
        self.assertIn("staging_csv_outside_run:stg_ac2_supplier", result["exceptions"])
        self.assertNotIn("LEAK", json.dumps(result))
        self.assertNotIn("LEAK", report_text)

    def test_manifest_safety_fields_remain_forced_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            audit_run = Path(tmpdir) / "audit_run"
            staging_run.mkdir()
            audit_run.mkdir()
            audit_manifest = write_fixture_run(staging_run, audit_run)
            manifest = json.loads(audit_manifest.read_text(encoding="utf-8"))
            manifest["decision"] = "Production ready"
            manifest["business_reconciliation_status"] = "reconciled"
            manifest["data_maturity"] = "production"
            manifest["final_production_selected"] = True
            audit_manifest.write_text(json.dumps(manifest), encoding="utf-8")

            result = review.run_warning_review(
                audit_manifest,
                output_root=Path(tmpdir) / "review",
                now=datetime.fromisoformat("2026-06-18T20:00:00+08:00"),
            )

        self.assertEqual(result["decision"], "Needs reconciliation")
        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])
        self.assertIn("unsafe_source_audit_decision", result["exceptions"])


def write_fixture_run(staging_run, audit_run, all_operational_zero=False, extract_run=None):
    rows_by_table = {
        "stg_ac2_supplier": [
            {
                "supplier_code": "SUP-001",
                "supplier_name": "Synthetic Supplier",
                "source_surface": "dbo.vCreditor",
                "source_extract_run_id": "extract-run-123",
                "source_row_number": "1",
            }
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
            }
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
                "outstanding_qty_candidate": "",
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
    if all_operational_zero:
        for table_name in [
            "stg_ac2_grn_header",
            "stg_ac2_grn_line",
            "stg_ac2_stock_receive_header",
            "stg_ac2_stock_receive_line",
            "stg_ac2_transfer_header",
            "stg_ac2_transfer_line",
        ]:
            rows_by_table[table_name] = []

    staging_tables = []
    row_counts = {}
    for table_name, rows in rows_by_table.items():
        headers = review.EXPECTED_STAGING_HEADERS[table_name]
        write_csv(staging_run / f"{table_name}.csv", headers, rows)
        row_counts[table_name] = len(rows)
        staging_tables.append(
            {
                "table_name": table_name,
                "row_count": len(rows),
                "output_path": str(staging_run / f"{table_name}.csv"),
                "file_name": f"{table_name}.csv",
            }
        )

    build_manifest = {
        "job": "autocount_inventory_staging_build",
        "status": "success_with_warnings",
        "run_id": "staging-build-run-123",
        "staging_tables": staging_tables,
        "row_counts": row_counts,
        "warnings": [
            "missing_expected_columns:dbo.vGoodsReceivedNoteDetail",
            "missing_expected_columns:dbo.vGoodsReceivedNoteSubDetail",
            "missing_expected_columns:dbo.GRDTL",
            "missing_expected_columns:dbo.vStockReceiveDetail",
            "missing_expected_columns:dbo.vStockTransferDetail",
            "outstanding_qty_candidate_not_numeric:dbo.PODTL",
        ],
        "exception_count": 0,
        "exceptions": [],
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(staging_run),
            "manifest": str(staging_run / "inventory_staging_build_manifest.json"),
        },
    }
    if extract_run:
        extract_manifest = {
            "job": "autocount_inventory_operation_extract",
            "status": "success_with_warnings",
            "run_id": "extract-run-123",
            "surface_exports": [
                {
                    "object_id": "dbo.GRDTL",
                    "expected_columns": ["DocNo", "DtlKey", "ItemCode"],
                    "selected_columns": ["DocNo", "ItemCode"],
                    "missing_expected_columns": ["DtlKey"],
                }
            ],
            "decision": "Needs reconciliation",
            "business_reconciliation_status": "not_reconciled",
            "data_maturity": "immature_pre_go_live",
            "final_production_selected": False,
            "storage": {
                "run_path": str(extract_run),
                "manifest": str(extract_run / "inventory_operation_extract_manifest.json"),
            },
        }
        (extract_run / "inventory_operation_extract_manifest.json").write_text(json.dumps(extract_manifest), encoding="utf-8")
        build_manifest["source_extract_run_path"] = str(extract_run)
    (staging_run / "inventory_staging_build_manifest.json").write_text(json.dumps(build_manifest), encoding="utf-8")

    audit_manifest = {
        "job": "autocount_inventory_staging_audit",
        "status": "success_with_warnings",
        "audit_decision": "staging_contract_ready_data_thin",
        "run_id": "audit-run-123",
        "source_staging_build_run_id": "staging-build-run-123",
        "source_staging_run_path": str(staging_run),
        "row_counts": row_counts,
        "zero_row_tables": [table for table, count in row_counts.items() if count == 0],
        "missing_files": [],
        "missing_headers": [],
        "carried_forward_warnings": [
            "missing_expected_columns:dbo.GRDTL",
            "outstanding_qty_candidate_not_numeric:dbo.PODTL",
        ],
        "warnings": [
            "missing_expected_columns:dbo.GRDTL",
            "outstanding_qty_candidate_not_numeric:dbo.PODTL",
            "duplicate_source_surfaces_review:stg_ac2_supplier",
            "operational_movement_rows_data_thin",
        ],
        "warning_count": 4,
        "exception_count": 0,
        "exceptions": [],
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(audit_run),
            "manifest": str(audit_run / "inventory_staging_audit_manifest.json"),
        },
    }
    audit_manifest_path = audit_run / "inventory_staging_audit_manifest.json"
    audit_manifest_path.write_text(json.dumps(audit_manifest), encoding="utf-8")
    return audit_manifest_path


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
