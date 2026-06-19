import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_inventory_source_bridge_evidence_review as review


class InventorySourceBridgeEvidenceReviewTests(unittest.TestCase):
    def test_header_detail_bridge_candidates_are_generated_from_metadata_coverage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_metadata_coverage_review(Path(tmpdir))

            result = review.run_bridge_evidence_review(
                manifest_path,
                output_root=Path(tmpdir) / "bridge_review",
                now=datetime.fromisoformat("2026-06-19T13:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["decision"], "Needs reconciliation")
        self.assertEqual(result["recommendation"], "no_dashboard_until_schema_gap_resolved")
        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])

        candidates = {item["bridge_name"]: item for item in result["bridge_candidates"]}
        self.assertEqual(
            candidates["GRN"]["detail_source_surfaces"],
            ["dbo.vGoodsReceivedNoteDetail", "dbo.vGoodsReceivedNoteSubDetail"],
        )
        self.assertEqual(candidates["GRN"]["header_source_surface"], "dbo.vGoodsReceivedNote")
        self.assertEqual(candidates["GRN"]["proposed_bridge"], "detail.DocKey -> header.DocKey -> header.DocNo")
        self.assertEqual(candidates["GRN"]["dependent_staging_field"], "grn_doc_no")
        self.assertEqual(candidates["GRN"]["confidence"], "needs_manual_validation")
        self.assertEqual(candidates["GRN"]["selection_status"], "not_selected_manual_validation_required")
        self.assertTrue(candidates["GRN"]["metadata_evidence"]["header_has_docno_and_dockey"])

        self.assertEqual(candidates["GR / GRDTL"]["header_source_surface"], "dbo.GR")
        self.assertEqual(candidates["GR / GRDTL"]["detail_source_surfaces"], ["dbo.GRDTL"])
        self.assertEqual(candidates["Stock Receive"]["dependent_staging_field"], "receive_doc_no")
        self.assertEqual(candidates["Stock Transfer"]["dependent_staging_field"], "transfer_doc_no")

    def test_quantity_direct_and_possible_alias_candidates_are_separated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_metadata_coverage_review(Path(tmpdir))

            result = review.run_bridge_evidence_review(
                manifest_path,
                output_root=Path(tmpdir) / "bridge_review",
                now=datetime.fromisoformat("2026-06-19T13:00:00+08:00"),
            )

        quantity = {item["source_surface"]: item for item in result["quantity_alias_reviews"]}
        self.assertEqual(
            quantity["dbo.vGoodsReceivedNoteDetail"]["candidate_classification"],
            "direct_quantity_column_available",
        )
        self.assertEqual(quantity["dbo.vGoodsReceivedNoteDetail"]["reviewed_expected_column"], "SmallestQty")
        self.assertIn("SmallestQty", quantity["dbo.vGoodsReceivedNoteDetail"]["available_quantity_columns"])
        self.assertEqual(quantity["dbo.GRDTL"]["candidate_classification"], "direct_quantity_column_available")

        self.assertEqual(
            quantity["dbo.vStockReceiveDetail"]["candidate_classification"],
            "possible_quantity_alias_requires_manual_validation",
        )
        self.assertEqual(quantity["dbo.vStockReceiveDetail"]["possible_alias_columns"], ["Qty", "BatchBalQty"])
        self.assertEqual(quantity["dbo.vStockReceiveDetail"]["selected_quantity_column"], "")
        self.assertEqual(
            quantity["dbo.vStockTransferDetail"]["candidate_classification"],
            "possible_quantity_alias_requires_manual_validation",
        )
        self.assertEqual(quantity["dbo.vStockTransferDetail"]["possible_alias_columns"], ["Qty"])
        self.assertEqual(quantity["dbo.vStockTransferDetail"]["selected_quantity_column"], "")

    def test_batchbalqty_without_dependent_staging_field_remains_expected_column_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_metadata_coverage_review(Path(tmpdir))

            result = review.run_bridge_evidence_review(
                manifest_path,
                output_root=Path(tmpdir) / "bridge_review",
                now=datetime.fromisoformat("2026-06-19T13:00:00+08:00"),
            )

        batch = result["expected_column_reviews"][0]
        self.assertEqual(batch["gap_source_surface"], "dbo.vGoodsReceivedNoteSubDetail")
        self.assertEqual(batch["missing_source_column"], "BatchBalQty")
        self.assertEqual(batch["metadata_gap_reason"], "expected_column_has_no_dependent_staging_field")
        self.assertEqual(batch["dependent_staging_fields"], [])
        self.assertEqual(batch["staging_mapping_status"], "not_required_without_dependent_staging_field")
        self.assertEqual(batch["confidence"], "needs_manual_validation")

    def test_final_mapping_is_never_selected_and_dashboard_recommendation_remains_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_metadata_coverage_review(Path(tmpdir))

            result = review.run_bridge_evidence_review(
                manifest_path,
                output_root=Path(tmpdir) / "bridge_review",
                now=datetime.fromisoformat("2026-06-19T13:00:00+08:00"),
            )

        manifest_text = json.dumps(result)
        self.assertNotIn("selected_mapping", manifest_text)
        self.assertEqual(result["recommendation"], "no_dashboard_until_schema_gap_resolved")
        for candidate in result["bridge_candidates"]:
            self.assertEqual(candidate["selection_status"], "not_selected_manual_validation_required")

    def test_report_and_manifest_do_not_include_raw_business_row_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_metadata_coverage_review(Path(tmpdir), include_raw_rows=True)

            result = review.run_bridge_evidence_review(
                manifest_path,
                output_root=Path(tmpdir) / "bridge_review",
                now=datetime.fromisoformat("2026-06-19T13:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        manifest_text = json.dumps(result)
        for raw_value in ["GRN-001", "ITEM-001", "SUP-001", "Synthetic Supplier", "RAW-QTY-VALUE"]:
            self.assertNotIn(raw_value, manifest_text)
            self.assertNotIn(raw_value, report_text)
        self.assertIn("metadata-only and require manual validation", report_text)
        self.assertIn("This PR does not make staging/dashboard production-ready", report_text)

    def test_output_root_must_stay_outside_repo(self):
        repo_output = ROOT / "tmp_bridge_evidence_review"

        with self.assertRaises(ValueError):
            review.resolve_output_root(repo_output)

    def test_missing_or_unsafe_source_manifest_status_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_metadata_coverage_review(
                Path(tmpdir),
                overrides={
                    "status": "failed",
                    "decision": "Ready",
                    "recommendation": "dashboard_ready",
                    "final_production_selected": True,
                },
            )

            result = review.run_bridge_evidence_review(
                manifest_path,
                output_root=Path(tmpdir) / "bridge_review",
                now=datetime.fromisoformat("2026-06-19T13:00:00+08:00"),
            )

        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["exception_count"], 0)
        self.assertIn("unsafe_source_metadata_coverage_review_status", result["exceptions"])
        self.assertIn("unsafe_source_metadata_coverage_review_decision", result["exceptions"])
        self.assertEqual(result["recommendation"], "no_dashboard_until_schema_gap_resolved")
        self.assertFalse(result["final_production_selected"])


def write_fixture_metadata_coverage_review(root, include_raw_rows=False, overrides=None):
    run = root / "metadata_coverage_run"
    run.mkdir()
    manifest = {
        "job": "autocount_inventory_source_metadata_coverage_review",
        "status": "success_with_warnings",
        "decision": "Needs reconciliation",
        "recommendation": "no_dashboard_until_schema_gap_resolved",
        "run_id": "metadata-coverage-run-123",
        "source_mapping_candidate_review_run_path": str(root / "mapping_run"),
        "source_staging_run_path": str(root / "staging_run"),
        "reviewed_surfaces": [
            "dbo.GR",
            "dbo.GRDTL",
            "dbo.vGoodsReceivedNote",
            "dbo.vGoodsReceivedNoteDetail",
            "dbo.vGoodsReceivedNoteSubDetail",
            "dbo.vStockReceive",
            "dbo.vStockReceiveDetail",
            "dbo.vStockTransfer",
            "dbo.vStockTransferDetail",
        ],
        "surface_column_inventory": [
            inventory("dbo.GR", ["DocKey", "DocNo"]),
            inventory("dbo.GRDTL", ["DocKey", "Qty", "SmallestQty", "TransferedQty"]),
            inventory("dbo.vGoodsReceivedNote", ["DocKey", "DocNo"]),
            inventory("dbo.vGoodsReceivedNoteDetail", ["DocKey", "Qty", "SmallestQty", "TransferedQty", "BatchBalQty"]),
            inventory("dbo.vGoodsReceivedNoteSubDetail", ["DocKey", "Qty"]),
            inventory("dbo.vStockReceive", ["DocKey", "DocNo"]),
            inventory("dbo.vStockReceiveDetail", ["DocKey", "Qty", "BatchBalQty"]),
            inventory("dbo.vStockTransfer", ["DocKey", "DocNo"]),
            inventory("dbo.vStockTransferDetail", ["DocKey", "Qty"]),
        ],
        "docno_doc_key_candidates": [
            doc_candidate("dbo.GR"),
            doc_candidate("dbo.vGoodsReceivedNote"),
            doc_candidate("dbo.vStockReceive"),
            doc_candidate("dbo.vStockTransfer"),
        ],
        "quantity_column_candidates": [
            quantity_candidate("dbo.GRDTL", ["Qty", "SmallestQty", "TransferedQty"]),
            quantity_candidate("dbo.vGoodsReceivedNoteDetail", ["Qty", "SmallestQty", "TransferedQty", "BatchBalQty"]),
            quantity_candidate("dbo.vGoodsReceivedNoteSubDetail", ["Qty"]),
            quantity_candidate("dbo.vStockReceiveDetail", ["Qty", "BatchBalQty"]),
            quantity_candidate("dbo.vStockTransferDetail", ["Qty"]),
        ],
        "unresolved_metadata_gaps": [
            gap("dbo.vGoodsReceivedNoteDetail", "DocNo", ["grn_doc_no"], "related_header_surface_has_docno_and_dockey", ["dbo.vGoodsReceivedNote"]),
            gap("dbo.vGoodsReceivedNoteSubDetail", "DocNo", ["grn_doc_no"], "related_header_surface_has_docno_and_dockey", ["dbo.vGoodsReceivedNote"]),
            gap("dbo.vGoodsReceivedNoteSubDetail", "BatchBalQty", [], "expected_column_has_no_dependent_staging_field", []),
            gap("dbo.GRDTL", "DocNo", ["grn_doc_no"], "related_header_surface_has_docno_and_dockey", ["dbo.GR"]),
            gap("dbo.vStockReceiveDetail", "DocNo", ["receive_doc_no"], "related_header_surface_has_docno_and_dockey", ["dbo.vStockReceive"]),
            gap("dbo.vStockReceiveDetail", "SmallestQty", [], "related_quantity_columns_available", ["dbo.vStockReceiveDetail"]),
            gap("dbo.vStockTransferDetail", "DocNo", ["transfer_doc_no"], "related_header_surface_has_docno_and_dockey", ["dbo.vStockTransfer"]),
            gap("dbo.vStockTransferDetail", "SmallestQty", [], "related_quantity_columns_available", ["dbo.vStockTransferDetail"]),
        ],
        "warning_count": 8,
        "exception_count": 0,
        "exceptions": [],
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(run),
            "manifest": str(run / "inventory_source_metadata_coverage_review_manifest.json"),
        },
    }
    if include_raw_rows:
        manifest["sample_rows"] = [
            {
                "DocNo": "GRN-001",
                "ItemCode": "ITEM-001",
                "SupplierCode": "SUP-001",
                "SupplierName": "Synthetic Supplier",
                "Qty": "RAW-QTY-VALUE",
            }
        ]
    if overrides:
        manifest.update(overrides)
    manifest_path = run / "inventory_source_metadata_coverage_review_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def inventory(source_surface, columns):
    return {
        "source_surface": source_surface,
        "columns": columns,
        "has_docno": "DocNo" in columns,
        "has_dockey": "DocKey" in columns,
        "quantity_like_columns": [column for column in ["Qty", "SmallestQty", "TransferedQty", "BatchBalQty"] if column in columns],
        "candidate_classification": "metadata_available",
        "confidence": "metadata_only",
    }


def doc_candidate(source_surface):
    return {
        "source_surface": source_surface,
        "docno_doc_key_columns": ["DocNo", "DocKey"],
        "candidate_classification": "candidate_header_surface_has_docno_and_dockey",
        "confidence": "metadata_only",
    }


def quantity_candidate(source_surface, columns):
    return {
        "source_surface": source_surface,
        "quantity_like_columns": columns,
        "candidate_classification": "candidate_quantity_surface",
        "confidence": "needs_manual_validation",
    }


def gap(source_surface, missing_column, dependent_fields, reason, related_surfaces):
    return {
        "gap_source_surface": source_surface,
        "staging_table": "stg_ac2_inventory_line",
        "missing_source_column": missing_column,
        "dependent_staging_fields": dependent_fields,
        "candidate_classification": "metadata_available",
        "confidence": "needs_manual_validation",
        "metadata_gap_reason": reason,
        "related_candidate_surfaces": related_surfaces,
    }


if __name__ == "__main__":
    unittest.main()
