import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_inventory_source_mapping_candidate_review as review


class InventorySourceMappingCandidateReviewTests(unittest.TestCase):
    def test_docno_gaps_identify_header_bridge_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_schema_gap_review(Path(tmpdir), include_metadata=True)

            result = review.run_mapping_candidate_review(
                manifest_path,
                output_root=Path(tmpdir) / "mapping_review",
                now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["recommendation"], "no_dashboard_until_schema_gap_resolved")
        self.assertEqual(result["decision"], "Needs reconciliation")
        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])

        candidates = candidates_by_gap(result)
        grn_docno = candidates[("dbo.GRDTL", "DocNo")]
        self.assertEqual(grn_docno["candidate_source_surface"], "dbo.vGoodsReceivedNote")
        self.assertEqual(grn_docno["candidate_bridge_columns"], ["DocKey", "DocNo"])
        self.assertEqual(grn_docno["candidate_value_columns"], ["DocNo"])
        self.assertEqual(grn_docno["candidate_classification"], "candidate_header_bridge")
        self.assertEqual(grn_docno["confidence"], "metadata_only_candidate")
        self.assertEqual(grn_docno["dashboard_impact"], "dashboard_blocker_requires_manual_validation")
        self.assertEqual(grn_docno["dependent_staging_fields"], ["grn_doc_no"])

    def test_smallestqty_gaps_identify_quantity_like_candidates_without_final_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_schema_gap_review(Path(tmpdir), include_metadata=True)

            result = review.run_mapping_candidate_review(
                manifest_path,
                output_root=Path(tmpdir) / "mapping_review",
                now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
            )

        candidates = candidates_by_gap(result)
        transfer_qty = candidates[("dbo.vStockTransferDetail", "SmallestQty")]
        self.assertEqual(transfer_qty["candidate_source_surface"], "dbo.StockTransferDetailCandidate")
        self.assertEqual(transfer_qty["candidate_classification"], "candidate_quantity_alias")
        self.assertEqual(transfer_qty["confidence"], "requires_manual_validation")
        self.assertEqual(transfer_qty["candidate_bridge_columns"], ["ItemCode"])
        self.assertEqual(transfer_qty["candidate_value_columns"], ["Qty", "TransferQty"])
        self.assertIn(transfer_qty, result["unresolved_mapping_gaps"])

    def test_batchbalqty_without_dependent_staging_field_is_flagged_for_expected_column_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_schema_gap_review(Path(tmpdir), include_metadata=True)

            result = review.run_mapping_candidate_review(
                manifest_path,
                output_root=Path(tmpdir) / "mapping_review",
                now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
            )

        candidates = candidates_by_gap(result)
        batch = candidates[("dbo.vGoodsReceivedNoteSubDetail", "BatchBalQty")]
        self.assertEqual(batch["candidate_source_surface"], "")
        self.assertEqual(batch["dependent_staging_fields"], [])
        self.assertEqual(batch["candidate_classification"], "expected_column_may_be_unneeded")
        self.assertEqual(batch["confidence"], "requires_manual_validation")
        self.assertEqual(batch["dashboard_impact"], "dashboard_blocker_requires_manual_validation")

    def test_insufficient_metadata_is_explicitly_labelled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_schema_gap_review(Path(tmpdir), include_metadata=False)

            result = review.run_mapping_candidate_review(
                manifest_path,
                output_root=Path(tmpdir) / "mapping_review",
                now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
            )

        candidate = candidates_by_gap(result)[("dbo.GRDTL", "DocNo")]
        self.assertEqual(candidate["candidate_classification"], "needs_source_metadata_evidence")
        self.assertEqual(candidate["confidence"], "insufficient_metadata")
        self.assertEqual(candidate["candidate_source_surface"], "")
        self.assertIn(candidate, result["unresolved_mapping_gaps"])

    def test_report_does_not_leak_raw_business_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_schema_gap_review(Path(tmpdir), include_metadata=True)

            result = review.run_mapping_candidate_review(
                manifest_path,
                output_root=Path(tmpdir) / "mapping_review",
                now=datetime.fromisoformat("2026-06-18T22:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        for raw_value in ["GRN-001", "ITEM-001", "SUP-001", "Synthetic Supplier", "42"]:
            self.assertNotIn(raw_value, report_text)
        self.assertIn("dbo.GRDTL", report_text)
        self.assertIn("candidate_header_bridge", report_text)
        self.assertIn("no_dashboard_until_schema_gap_resolved", report_text)


def candidates_by_gap(result):
    return {
        (item["gap_source_surface"], item["missing_source_column"]): item
        for item in result["mapping_candidates"]
    }


def write_fixture_schema_gap_review(root, include_metadata):
    staging_run = root / "staging_run"
    schema_run = root / "schema_run"
    extract_run = root / "extract_run"
    staging_run.mkdir()
    schema_run.mkdir()
    if include_metadata:
        extract_run.mkdir()

    schema_manifest = {
        "job": "autocount_inventory_staging_schema_gap_review",
        "status": "success_with_warnings",
        "review_conclusion": "no_dashboard_until_schema_gap_resolved",
        "recommendation": "no_dashboard_until_schema_gap_resolved",
        "run_id": "schema-run-123",
        "source_staging_run_path": str(staging_run),
        "source_schema_gaps": [
            schema_gap(
                "dbo.vGoodsReceivedNoteDetail",
                "stg_ac2_grn_line",
                ["DocKey", "DtlKey", "ItemCode"],
                ["DocNo"],
                {"DocNo": ["grn_doc_no"]},
            ),
            schema_gap(
                "dbo.vGoodsReceivedNoteSubDetail",
                "stg_ac2_grn_line",
                ["DocKey", "DtlKey", "ItemCode"],
                ["DocNo", "BatchBalQty"],
                {"DocNo": ["grn_doc_no"]},
            ),
            schema_gap(
                "dbo.GRDTL",
                "stg_ac2_grn_line",
                ["DocKey", "DtlKey", "ItemCode"],
                ["DocNo"],
                {"DocNo": ["grn_doc_no"]},
            ),
            schema_gap(
                "dbo.vStockReceiveDetail",
                "stg_ac2_stock_receive_line",
                ["ItemCode"],
                ["DocNo", "SmallestQty"],
                {"DocNo": ["receive_doc_no"]},
            ),
            schema_gap(
                "dbo.vStockTransferDetail",
                "stg_ac2_transfer_line",
                ["ItemCode"],
                ["DocNo", "SmallestQty"],
                {"DocNo": ["transfer_doc_no"]},
            ),
        ],
        "warning_count": 5,
        "exception_count": 0,
        "exceptions": [],
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(schema_run),
            "manifest": str(schema_run / "inventory_staging_schema_gap_review_manifest.json"),
        },
    }

    build_manifest = {
        "job": "autocount_inventory_staging_build",
        "status": "success_with_warnings",
        "source_extract_run_path": str(extract_run) if include_metadata else "",
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(staging_run),
            "manifest": str(staging_run / "inventory_staging_build_manifest.json"),
        },
    }
    (staging_run / "inventory_staging_build_manifest.json").write_text(json.dumps(build_manifest), encoding="utf-8")

    if include_metadata:
        extract_manifest = {
            "job": "autocount_inventory_operation_extract",
            "status": "success_with_warnings",
            "surface_exports": [
                metadata("dbo.vGoodsReceivedNote", ["DocKey", "DocNo", "SupplierCode"]),
                metadata("dbo.GRN", ["DocKey", "DocNo"]),
                metadata("dbo.vStockReceive", ["DocKey", "DocNo"]),
                metadata("dbo.vStockTransfer", ["DocKey", "DocNo"]),
                metadata("dbo.StockReceiveDetailCandidate", ["ItemCode", "Qty", "ReceiveQty"]),
                metadata("dbo.StockTransferDetailCandidate", ["ItemCode", "Qty", "TransferQty"]),
            ],
            "schema_metadata": [
                metadata("dbo.vGoodsReceivedNoteDetail", ["DocKey", "DtlKey", "ItemCode"]),
                metadata("dbo.vGoodsReceivedNoteSubDetail", ["DocKey", "DtlKey", "ItemCode"]),
                metadata("dbo.GRDTL", ["DocKey", "DtlKey", "ItemCode"]),
                metadata("dbo.vStockReceiveDetail", ["ItemCode"]),
                metadata("dbo.vStockTransferDetail", ["ItemCode"]),
            ],
            "sample_rows": [{"DocNo": "GRN-001", "ItemCode": "ITEM-001", "Supplier": "Synthetic Supplier", "Qty": "42"}],
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

    manifest_path = schema_run / "inventory_staging_schema_gap_review_manifest.json"
    manifest_path.write_text(json.dumps(schema_manifest), encoding="utf-8")
    return manifest_path


def schema_gap(source_surface, staging_table, present, missing, dependent):
    return {
        "warning_code": f"missing_expected_columns:{source_surface}",
        "source_surface": source_surface,
        "staging_table": staging_table,
        "expected_source_columns": present + missing,
        "present_source_columns": present,
        "missing_source_columns": missing,
        "dependent_staging_fields": dependent,
        "classification": "needs_source_column_mapping",
        "dashboard_impact": "dashboard_blocker_when_data_arrives",
    }


def metadata(source_surface, columns):
    return {
        "object_id": source_surface,
        "expected_columns": columns,
        "selected_columns": columns,
        "missing_expected_columns": [],
    }


if __name__ == "__main__":
    unittest.main()
