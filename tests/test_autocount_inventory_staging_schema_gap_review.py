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

from scripts import autocount_inventory_staging_schema_gap_review as review


class InventoryStagingSchemaGapReviewTests(unittest.TestCase):
    def test_source_schema_gap_classification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            warning_run = Path(tmpdir) / "warning_run"
            staging_run.mkdir()
            warning_run.mkdir()
            manifest_path = write_fixture_warning_review(staging_run, warning_run)

            result = review.run_schema_gap_review(
                manifest_path,
                output_root=Path(tmpdir) / "schema_gap_review",
                now=datetime.fromisoformat("2026-06-18T21:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["review_conclusion"], "no_dashboard_until_schema_gap_resolved")
        schema_items = [item for item in result["source_schema_gaps"] if item["source_surface"] == "dbo.GRDTL"]
        self.assertEqual(len(schema_items), 1)
        self.assertEqual(schema_items[0]["classification"], "needs_source_column_mapping")
        self.assertIn("dashboard_blocker_when_data_arrives", schema_items[0]["decision_flags"])
        self.assertEqual(schema_items[0]["missing_expected_headers"], ["missing_expected_columns"])
        self.assertIn("grn_doc_no", schema_items[0]["present_header_names"])

    def test_numeric_candidate_not_computable_classification(self):
        classification = review.classify_numeric_candidate(
            "outstanding_qty_candidate_not_numeric:dbo.PODTL",
            {"stg_ac2_purchase_order_line": ["qty", "transferred_qty", "outstanding_qty_candidate"]},
        )

        self.assertEqual(classification["source_surface"], "dbo.PODTL")
        self.assertEqual(classification["staging_table"], "stg_ac2_purchase_order_line")
        self.assertEqual(classification["classification"], "needs_numeric_type_mapping")
        self.assertIn("optional_candidate_metric", classification["decision_flags"])
        self.assertIn("dashboard_blocker_for_outstanding_po", classification["decision_flags"])
        self.assertEqual(classification["candidate_header_names"], ["qty", "transferred_qty", "outstanding_qty_candidate"])

    def test_duplicate_supplier_source_overlap_classification(self):
        result = review.review_duplicate_overlap(
            "duplicate_source_surfaces_review:stg_ac2_supplier",
            {"stg_ac2_supplier": 140},
            {"stg_ac2_supplier": ["supplier_code", "supplier_name", "source_surface"]},
        )

        self.assertEqual(result["staging_table"], "stg_ac2_supplier")
        self.assertEqual(result["source_surfaces_reviewed"], ["dbo.vCreditor", "dbo.Creditor"])
        self.assertIn("review_only_duplicate_source_overlap", result["decision_flags"])
        self.assertIn("future_dimension_dedupe_required", result["decision_flags"])

    def test_duplicate_po_header_source_overlap_classification(self):
        result = review.review_duplicate_overlap(
            "duplicate_source_surfaces_review:stg_ac2_purchase_order_header",
            {"stg_ac2_purchase_order_header": 2},
            {"stg_ac2_purchase_order_header": ["po_doc_key", "po_doc_no", "source_surface"]},
        )

        self.assertEqual(result["staging_table"], "stg_ac2_purchase_order_header")
        self.assertEqual(result["source_surfaces_reviewed"], ["dbo.vPurchaseOrder", "dbo.PO"])
        self.assertIn("review_only_duplicate_source_overlap", result["decision_flags"])
        self.assertIn("future_dimension_dedupe_required", result["decision_flags"])

    def test_zero_row_operational_movement_tables_remain_data_thin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            warning_run = Path(tmpdir) / "warning_run"
            staging_run.mkdir()
            warning_run.mkdir()
            manifest_path = write_fixture_warning_review(staging_run, warning_run)

            result = review.run_schema_gap_review(
                manifest_path,
                output_root=Path(tmpdir) / "schema_gap_review",
                now=datetime.fromisoformat("2026-06-18T21:00:00+08:00"),
            )

        data_thin_tables = {item["staging_table"] for item in result["data_thin_tables"]}
        self.assertIn("stg_ac2_grn_line", data_thin_tables)
        self.assertIn("stg_ac2_transfer_line", data_thin_tables)
        self.assertTrue(all(item["classification"] == "safe_until_data_arrives" for item in result["data_thin_tables"]))

    def test_path_traversal_outside_staging_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            warning_run = Path(tmpdir) / "warning_run"
            outside = Path(tmpdir) / "outside.csv"
            staging_run.mkdir()
            warning_run.mkdir()
            outside.write_text("supplier_code,source_surface\nLEAK,dbo.X\n", encoding="utf-8")
            manifest_path = write_fixture_warning_review(staging_run, warning_run)
            warning_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in warning_manifest["header_evidence"]:
                if item["table_name"] == "stg_ac2_supplier":
                    item["output_path"] = str(outside)
            manifest_path.write_text(json.dumps(warning_manifest), encoding="utf-8")

            result = review.run_schema_gap_review(
                manifest_path,
                output_root=Path(tmpdir) / "schema_gap_review",
                now=datetime.fromisoformat("2026-06-18T21:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        self.assertEqual(result["status"], "failed")
        self.assertIn("staging_csv_outside_run:stg_ac2_supplier", result["exceptions"])
        self.assertNotIn("LEAK", json.dumps(result))
        self.assertNotIn("LEAK", report_text)

    def test_report_does_not_leak_raw_synthetic_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            warning_run = Path(tmpdir) / "warning_run"
            staging_run.mkdir()
            warning_run.mkdir()
            manifest_path = write_fixture_warning_review(staging_run, warning_run)

            result = review.run_schema_gap_review(
                manifest_path,
                output_root=Path(tmpdir) / "schema_gap_review",
                now=datetime.fromisoformat("2026-06-18T21:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        for raw_value in ["SUP-001", "PO-001", "ITEM-001", "Synthetic Supplier", "Synthetic item", "MAIN"]:
            self.assertNotIn(raw_value, report_text)
        self.assertIn("dbo.GRDTL", report_text)
        self.assertIn("stg_ac2_supplier", report_text)

    def test_manifest_safety_fields_remain_forced_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_run = Path(tmpdir) / "staging_run"
            warning_run = Path(tmpdir) / "warning_run"
            staging_run.mkdir()
            warning_run.mkdir()
            manifest_path = write_fixture_warning_review(staging_run, warning_run)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["business_reconciliation_status"] = "reconciled"
            manifest["data_maturity"] = "production"
            manifest["final_production_selected"] = True
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = review.run_schema_gap_review(
                manifest_path,
                output_root=Path(tmpdir) / "schema_gap_review",
                now=datetime.fromisoformat("2026-06-18T21:00:00+08:00"),
            )

        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])
        self.assertIn("unsafe_source_warning_review_business_reconciliation_status", result["exceptions"])


def write_fixture_warning_review(staging_run, warning_run):
    rows_by_table = {
        "stg_ac2_supplier": [
            {"supplier_code": "SUP-001", "supplier_name": "Synthetic Supplier", "source_surface": "dbo.vCreditor"}
        ],
        "stg_ac2_purchase_order_header": [
            {"po_doc_key": "100", "po_doc_no": "PO-001", "supplier_code": "SUP-001", "source_surface": "dbo.vPurchaseOrder"}
        ],
        "stg_ac2_purchase_order_line": [
            {
                "po_doc_key": "100",
                "po_dtl_key": "200",
                "item_code": "ITEM-001",
                "description": "Synthetic item",
                "qty": "10",
                "transferred_qty": "2",
                "location": "MAIN",
                "outstanding_qty_candidate": "",
                "source_surface": "dbo.PODTL",
            }
        ],
        "stg_ac2_grn_header": [],
        "stg_ac2_grn_line": [],
        "stg_ac2_stock_receive_header": [],
        "stg_ac2_stock_receive_line": [],
        "stg_ac2_transfer_header": [],
        "stg_ac2_transfer_line": [],
        "stg_ac2_stock_movement_reference": [
            {"doc_type": "PO", "item_code": "ITEM-001", "source_surface": "dbo.StockDTL"}
        ],
        "stg_ac2_item_balance_reference": [{"item_code": "ITEM-001", "source_surface": "dbo.vItemBalQty"}],
    }
    row_counts = {}
    header_evidence = []
    for table_name, headers in review.EXPECTED_STAGING_HEADERS.items():
        rows = rows_by_table[table_name]
        write_csv(staging_run / f"{table_name}.csv", headers, rows)
        row_counts[table_name] = len(rows)
        header_evidence.append(
            {
                "table_name": table_name,
                "expected_header_names": headers,
                "present_header_names": headers,
                "row_count": len(rows),
                "file_name": f"{table_name}.csv",
                "output_path": str(staging_run / f"{table_name}.csv"),
            }
        )

    warning_manifest = {
        "job": "autocount_inventory_staging_warning_review",
        "status": "success_with_warnings",
        "readiness_conclusion": "staging_warning_review_ready_data_thin",
        "run_id": "warning-review-run-123",
        "source_staging_run_path": str(staging_run),
        "row_counts": row_counts,
        "zero_row_tables": [table for table, count in row_counts.items() if count == 0],
        "header_evidence": header_evidence,
        "warning_classifications": [
            {
                "warning_code": "missing_expected_columns:dbo.GRDTL",
                "classification": "source_schema_gap",
                "source": "dbo.GRDTL",
                "dashboard_impact": "dashboard_blocker_when_data_arrives",
            },
            {
                "warning_code": "outstanding_qty_candidate_not_numeric:dbo.PODTL",
                "classification": "numeric_candidate_not_computable",
                "source": "dbo.PODTL",
                "dashboard_impact": "dashboard_blocker_when_data_arrives",
            },
            {
                "warning_code": "duplicate_source_surfaces_review:stg_ac2_supplier",
                "classification": "review_only_duplicate_source_overlap",
                "source": "stg_ac2_supplier",
                "dashboard_impact": "review_only",
            },
            {
                "warning_code": "duplicate_source_surfaces_review:stg_ac2_purchase_order_header",
                "classification": "review_only_duplicate_source_overlap",
                "source": "stg_ac2_purchase_order_header",
                "dashboard_impact": "review_only",
            },
            {
                "warning_code": "zero_row_operational_table:stg_ac2_grn_line",
                "classification": "source_data_thin",
                "source": "stg_ac2_grn_line",
                "dashboard_impact": "dashboard_blocker_when_data_arrives",
            },
        ],
        "dashboard_blockers": [
            {
                "warning_code": "missing_expected_columns:dbo.GRDTL",
                "classification": "source_schema_gap",
                "source": "dbo.GRDTL",
            },
            {
                "warning_code": "outstanding_qty_candidate_not_numeric:dbo.PODTL",
                "classification": "numeric_candidate_not_computable",
                "source": "dbo.PODTL",
            },
        ],
        "warning_count": 5,
        "exception_count": 0,
        "exceptions": [],
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(warning_run),
            "manifest": str(warning_run / "inventory_staging_warning_review_manifest.json"),
        },
    }
    manifest_path = warning_run / "inventory_staging_warning_review_manifest.json"
    manifest_path.write_text(json.dumps(warning_manifest), encoding="utf-8")
    return manifest_path


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
