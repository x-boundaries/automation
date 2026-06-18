import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_inventory_source_metadata_coverage_review as review


class InventorySourceMetadataCoverageReviewTests(unittest.TestCase):
    def test_docno_dockey_surfaces_are_header_bridge_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_mapping_candidate_review(Path(tmpdir), include_metadata=True)

            result = review.run_metadata_coverage_review(
                manifest_path,
                output_root=Path(tmpdir) / "metadata_coverage_review",
                now=datetime.fromisoformat("2026-06-18T23:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertEqual(result["decision"], "Needs reconciliation")
        self.assertEqual(result["recommendation"], "no_dashboard_until_schema_gap_resolved")
        self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
        self.assertEqual(result["data_maturity"], "immature_pre_go_live")
        self.assertFalse(result["final_production_selected"])

        candidates = {item["source_surface"]: item for item in result["docno_doc_key_candidates"]}
        self.assertEqual(
            candidates["dbo.vGoodsReceivedNote"]["candidate_classification"],
            "candidate_header_surface_has_docno_and_dockey",
        )
        self.assertIn("grn_header_candidate", candidates["dbo.vGoodsReceivedNote"]["surface_roles"])
        self.assertEqual(candidates["dbo.vGoodsReceivedNote"]["docno_doc_key_columns"], ["DocNo", "DocKey"])
        self.assertEqual(
            candidates["dbo.vStockReceive"]["candidate_classification"],
            "candidate_header_surface_has_docno_and_dockey",
        )
        self.assertIn("stock_receive_header_candidate", candidates["dbo.vStockReceive"]["surface_roles"])
        self.assertIn("stock_transfer_header_candidate", candidates["dbo.vStockTransfer"]["surface_roles"])

    def test_quantity_like_columns_are_detected_without_final_mapping_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_mapping_candidate_review(Path(tmpdir), include_metadata=True)

            result = review.run_metadata_coverage_review(
                manifest_path,
                output_root=Path(tmpdir) / "metadata_coverage_review",
                now=datetime.fromisoformat("2026-06-18T23:00:00+08:00"),
            )

        quantity = {item["source_surface"]: item for item in result["quantity_column_candidates"]}
        self.assertEqual(quantity["dbo.vStockReceiveDetail"]["quantity_like_columns"], ["Qty", "ReceiveQty"])
        self.assertEqual(quantity["dbo.vStockReceiveDetail"]["candidate_classification"], "candidate_quantity_surface")
        self.assertEqual(quantity["dbo.vStockReceiveDetail"]["confidence"], "needs_manual_validation")
        self.assertEqual(quantity["dbo.vStockTransferDetail"]["quantity_like_columns"], ["Qty", "TransferQty"])
        self.assertNotIn("final_mapping_selected", json.dumps(result))

    def test_missing_metadata_is_explicitly_labelled_as_insufficient(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_mapping_candidate_review(Path(tmpdir), include_metadata=False)

            result = review.run_metadata_coverage_review(
                manifest_path,
                output_root=Path(tmpdir) / "metadata_coverage_review",
                now=datetime.fromisoformat("2026-06-18T23:00:00+08:00"),
            )

        gap = next(
            item
            for item in result["unresolved_metadata_gaps"]
            if item["gap_source_surface"] == "dbo.GRDTL" and item["missing_source_column"] == "DocNo"
        )
        self.assertEqual(gap["candidate_classification"], "metadata_insufficient")
        self.assertEqual(gap["confidence"], "insufficient_metadata")
        self.assertEqual(gap["metadata_gap_reason"], "no_related_surface_metadata_with_required_column")

    def test_batchbalqty_without_dependent_staging_field_remains_expected_column_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_mapping_candidate_review(Path(tmpdir), include_metadata=True)

            result = review.run_metadata_coverage_review(
                manifest_path,
                output_root=Path(tmpdir) / "metadata_coverage_review",
                now=datetime.fromisoformat("2026-06-18T23:00:00+08:00"),
            )

        batch = next(
            item
            for item in result["unresolved_metadata_gaps"]
            if item["gap_source_surface"] == "dbo.vGoodsReceivedNoteSubDetail"
            and item["missing_source_column"] == "BatchBalQty"
        )
        self.assertEqual(batch["candidate_classification"], "needs_manual_validation")
        self.assertEqual(batch["metadata_gap_reason"], "expected_column_has_no_dependent_staging_field")
        self.assertEqual(batch["dependent_staging_fields"], [])
        self.assertEqual(result["recommendation"], "no_dashboard_until_schema_gap_resolved")

    def test_report_and_manifest_do_not_include_raw_row_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = write_fixture_mapping_candidate_review(Path(tmpdir), include_metadata=True)

            result = review.run_metadata_coverage_review(
                manifest_path,
                output_root=Path(tmpdir) / "metadata_coverage_review",
                now=datetime.fromisoformat("2026-06-18T23:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        manifest_text = json.dumps(result)
        for raw_value in ["GRN-001", "ITEM-001", "SUP-001", "Synthetic Supplier", "RAW-QTY-VALUE"]:
            self.assertNotIn(raw_value, manifest_text)
            self.assertNotIn(raw_value, report_text)
        self.assertIn("dbo.vGoodsReceivedNote", report_text)
        self.assertIn("candidate_header_surface_has_docno_and_dockey", report_text)

    def test_output_root_must_stay_outside_repo(self):
        repo_output = ROOT / "tmp_metadata_coverage_review"

        with self.assertRaises(ValueError):
            review.resolve_output_root(repo_output)

    def test_metadata_probe_sql_definitions_are_catalog_only(self):
        sql_text = "\n".join(item["sql"] for item in review.build_metadata_sql_definitions())

        self.assertNotRegex(sql_text, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql_text, r"(?i)\bSELECT\s+\*")
        self.assertIn("sys.objects", sql_text)
        self.assertIn("sys.columns", sql_text)


def write_fixture_mapping_candidate_review(root, include_metadata):
    mapping_run = root / "mapping_run"
    schema_run = root / "schema_run"
    staging_run = root / "staging_run"
    extract_run = root / "extract_run"
    mapping_run.mkdir()
    schema_run.mkdir()
    staging_run.mkdir()
    if include_metadata:
        extract_run.mkdir()

    mapping_manifest = {
        "job": "autocount_inventory_source_mapping_candidate_review",
        "status": "success_with_warnings",
        "decision": "Needs reconciliation",
        "recommendation": "no_dashboard_until_schema_gap_resolved",
        "run_id": "mapping-run-123",
        "source_schema_gap_review_run_path": str(schema_run),
        "source_staging_run_path": str(staging_run),
        "mapping_candidates": [
            mapping_gap("dbo.vGoodsReceivedNoteDetail", "stg_ac2_grn_line", "DocNo", ["grn_doc_no"]),
            mapping_gap("dbo.vGoodsReceivedNoteSubDetail", "stg_ac2_grn_line", "DocNo", ["grn_doc_no"]),
            mapping_gap("dbo.vGoodsReceivedNoteSubDetail", "stg_ac2_grn_line", "BatchBalQty", []),
            mapping_gap("dbo.GRDTL", "stg_ac2_grn_line", "DocNo", ["grn_doc_no"]),
            mapping_gap("dbo.vStockReceiveDetail", "stg_ac2_stock_receive_line", "DocNo", ["receive_doc_no"]),
            mapping_gap("dbo.vStockReceiveDetail", "stg_ac2_stock_receive_line", "SmallestQty", []),
            mapping_gap("dbo.vStockTransferDetail", "stg_ac2_transfer_line", "DocNo", ["transfer_doc_no"]),
            mapping_gap("dbo.vStockTransferDetail", "stg_ac2_transfer_line", "SmallestQty", []),
        ],
        "unresolved_mapping_gaps": [],
        "warning_count": 8,
        "exception_count": 0,
        "exceptions": [],
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(mapping_run),
            "manifest": str(mapping_run / "inventory_source_mapping_candidate_review_manifest.json"),
        },
    }
    mapping_manifest["unresolved_mapping_gaps"] = list(mapping_manifest["mapping_candidates"])

    schema_manifest = {
        "job": "autocount_inventory_staging_schema_gap_review",
        "status": "success_with_warnings",
        "recommendation": "no_dashboard_until_schema_gap_resolved",
        "source_staging_run_path": str(staging_run),
        "source_schema_gaps": [
            schema_gap("dbo.vGoodsReceivedNoteSubDetail", "stg_ac2_grn_line", ["DocNo", "BatchBalQty"], {"DocNo": ["grn_doc_no"]})
        ],
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "storage": {
            "run_path": str(schema_run),
            "manifest": str(schema_run / "inventory_staging_schema_gap_review_manifest.json"),
        },
    }
    (schema_run / "inventory_staging_schema_gap_review_manifest.json").write_text(json.dumps(schema_manifest), encoding="utf-8")

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
                metadata("dbo.vGoodsReceivedNoteDetail", ["DocKey", "DtlKey", "ItemCode"]),
                metadata("dbo.vGoodsReceivedNoteSubDetail", ["DocKey", "DtlKey", "ItemCode"]),
                metadata("dbo.GRDTL", ["DocKey", "DtlKey", "ItemCode"]),
                metadata("dbo.vStockReceive", ["DocKey", "DocNo"]),
                metadata("dbo.vStockReceiveDetail", ["ItemCode", "Qty", "ReceiveQty"]),
                metadata("dbo.vStockTransfer", ["DocKey", "DocNo"]),
                metadata("dbo.vStockTransferDetail", ["ItemCode", "Qty", "TransferQty"]),
                metadata("dbo.vStockTransferNoDoc", ["DocKey"]),
            ],
            "sample_rows": [
                {
                    "DocNo": "GRN-001",
                    "ItemCode": "ITEM-001",
                    "Supplier": "Synthetic Supplier",
                    "Qty": "RAW-QTY-VALUE",
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

    manifest_path = mapping_run / "inventory_source_mapping_candidate_review_manifest.json"
    manifest_path.write_text(json.dumps(mapping_manifest), encoding="utf-8")
    return manifest_path


def mapping_gap(source_surface, staging_table, missing_source_column, dependent_staging_fields):
    return {
        "gap_source_surface": source_surface,
        "staging_table": staging_table,
        "missing_source_column": missing_source_column,
        "dependent_staging_fields": dependent_staging_fields,
        "candidate_classification": "no_candidate_found",
        "confidence": "insufficient_metadata",
    }


def schema_gap(source_surface, staging_table, missing_columns, dependent_fields):
    return {
        "source_surface": source_surface,
        "staging_table": staging_table,
        "missing_source_columns": missing_columns,
        "dependent_staging_fields": dependent_fields,
        "classification": "needs_source_column_mapping",
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
