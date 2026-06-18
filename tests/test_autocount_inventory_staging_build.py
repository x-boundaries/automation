import csv
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

from scripts import autocount_inventory_staging_build as build


class InventoryStagingBuildTests(unittest.TestCase):
    def test_build_reads_manifest_and_local_csvs_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_run = Path(tmpdir) / "extract"
            extract_run.mkdir()
            write_fixture_extract(extract_run)
            manifest_path = extract_run / "inventory_operation_extract_manifest.json"

            result = build.run_staging_build(
                manifest_path,
                output_root=Path(tmpdir) / "staging",
                now=datetime.fromisoformat("2026-06-18T18:00:00+08:00"),
            )
            run_path = Path(result["storage"]["run_path"])

            self.assertEqual(result["status"], "success_with_warnings")
            self.assertEqual(result["source_extract_run_id"], "extract-run-123")
            self.assertEqual(result["decision"], "Needs reconciliation")
            self.assertEqual(result["business_reconciliation_status"], "not_reconciled")
            self.assertEqual(result["data_maturity"], "immature_pre_go_live")
            self.assertFalse(result["final_production_selected"])
            self.assertTrue((run_path / "stg_ac2_supplier.csv").exists())
            self.assertTrue((run_path / "stg_ac2_purchase_order_header.csv").exists())
            self.assertTrue((run_path / "stg_ac2_grn_header.csv").exists())

    def test_supplier_and_po_inputs_map_to_normalized_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_run = Path(tmpdir) / "extract"
            extract_run.mkdir()
            write_fixture_extract(extract_run)

            result = build.run_staging_build(
                extract_run / "inventory_operation_extract_manifest.json",
                output_root=Path(tmpdir) / "staging",
                now=datetime.fromisoformat("2026-06-18T18:00:00+08:00"),
            )
            run_path = Path(result["storage"]["run_path"])
            suppliers = read_csv(run_path / "stg_ac2_supplier.csv")
            po_headers = read_csv(run_path / "stg_ac2_purchase_order_header.csv")
            po_lines = read_csv(run_path / "stg_ac2_purchase_order_line.csv")

        self.assertEqual(suppliers[0]["supplier_code"], "SUP-001")
        self.assertEqual(suppliers[0]["supplier_name"], "Synthetic Supplier")
        self.assertEqual(po_headers[0]["po_doc_no"], "PO-001")
        self.assertEqual(po_headers[0]["supplier_code"], "SUP-001")
        self.assertEqual(po_lines[0]["item_code"], "ITEM-001")
        self.assertEqual(po_lines[0]["outstanding_qty_candidate"], "8")
        self.assertEqual(po_lines[0]["source_surface"], "dbo.PODTL")
        self.assertEqual(po_lines[0]["source_extract_run_id"], "extract-run-123")
        self.assertEqual(po_lines[0]["source_row_number"], "1")

    def test_zero_row_source_surfaces_emit_zero_row_staging_files_with_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_run = Path(tmpdir) / "extract"
            extract_run.mkdir()
            write_fixture_extract(extract_run)
            result = build.run_staging_build(
                extract_run / "inventory_operation_extract_manifest.json",
                output_root=Path(tmpdir) / "staging",
                now=datetime.fromisoformat("2026-06-18T18:00:00+08:00"),
            )
            run_path = Path(result["storage"]["run_path"])
            grn_rows = read_csv(run_path / "stg_ac2_grn_header.csv")
            headers = read_header(run_path / "stg_ac2_grn_header.csv")

        self.assertEqual(grn_rows, [])
        self.assertIn("grn_doc_no", headers)
        self.assertEqual(table_result(result, "stg_ac2_grn_header")["row_count"], 0)

    def test_rejects_old_surface_results_manifest_and_output_root_inside_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_run = Path(tmpdir) / "extract"
            extract_run.mkdir()
            manifest = synthetic_extract_manifest(extract_run)
            manifest["surface_results"] = manifest.pop("surface_exports")
            manifest_path = extract_run / "inventory_operation_extract_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = build.run_staging_build(
                manifest_path,
                output_root=Path(tmpdir) / "staging",
                now=datetime.fromisoformat("2026-06-18T18:00:00+08:00"),
            )

        self.assertEqual(result["status"], "failed")
        self.assertRegex("\n".join(result["exceptions"]), "surface_exports")
        with self.assertRaises(ValueError):
            build.resolve_output_root(ROOT / "staging_outputs")

    def test_missing_source_csv_warns_and_stays_local_to_extract_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_run = Path(tmpdir) / "extract"
            extract_run.mkdir()
            write_fixture_extract(extract_run)
            missing_path = extract_run / "dbo.PO.csv"
            missing_path.unlink()

            result = build.run_staging_build(
                extract_run / "inventory_operation_extract_manifest.json",
                output_root=Path(tmpdir) / "staging",
                now=datetime.fromisoformat("2026-06-18T18:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertIn("source_csv_missing:dbo.PO", result["warnings"])

    def test_refuses_csv_outside_extract_run_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_run = Path(tmpdir) / "extract"
            outside = Path(tmpdir) / "outside.csv"
            extract_run.mkdir()
            outside.write_text("DocNo\nPO-001\n", encoding="utf-8")
            manifest = synthetic_extract_manifest(extract_run)
            manifest["surface_exports"][0]["output_path"] = str(outside)
            manifest_path = extract_run / "inventory_operation_extract_manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = build.run_staging_build(
                manifest_path,
                output_root=Path(tmpdir) / "staging",
                now=datetime.fromisoformat("2026-06-18T18:00:00+08:00"),
            )

        self.assertEqual(result["status"], "success_with_warnings")
        self.assertIn("source_csv_outside_extract_run:dbo.vCreditor", result["warnings"])

    def test_report_contains_no_raw_business_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_run = Path(tmpdir) / "extract"
            extract_run.mkdir()
            write_fixture_extract(extract_run)
            result = build.run_staging_build(
                extract_run / "inventory_operation_extract_manifest.json",
                output_root=Path(tmpdir) / "staging",
                now=datetime.fromisoformat("2026-06-18T18:00:00+08:00"),
            )
            report_text = Path(result["storage"]["report"]).read_text(encoding="utf-8")

        for raw_value in ["PO-001", "ITEM-001", "SUP-001", "Synthetic Supplier"]:
            self.assertNotIn(raw_value, report_text)
        self.assertIn("dashboards are not meaningful yet", report_text)

    def test_example_config_runbook_and_gitignore_are_safe(self):
        config = json.loads((ROOT / "config" / "autocount_inventory_staging_build.example.json").read_text())
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\staging\inventory_operations")
        self.assertNotIn("connection_string", config)
        runbook = (
            ROOT / "docs" / "autocount2-automation" / "inventory_operation_staging_build_runbook.md"
        ).read_text(encoding="utf-8")
        self.assertIn("python scripts\\autocount_inventory_staging_build.py --manifest $manifest", runbook)
        self.assertIn("do not paste raw staging csv rows", runbook.lower())
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("inventory_staging_build_outputs/", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_staging_build_manifest.json", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_staging_build_report.md", gitignore_text)

    def test_redaction_helper_masks_password_like_fragments(self):
        redacted = build.sanitize_text("Driver=x;Password=secret;PWD=other;")
        self.assertNotIn("secret", redacted)
        self.assertNotIn("other", redacted)


def write_fixture_extract(extract_run):
    write_csv(
        extract_run / "dbo.vCreditor.csv",
        ["CreditorCode", "CreditorCompanyName"],
        [{"CreditorCode": "SUP-001", "CreditorCompanyName": "Synthetic Supplier"}],
    )
    write_csv(
        extract_run / "dbo.Creditor.csv",
        ["AccNo", "CompanyName"],
        [{"AccNo": "SUP-002", "CompanyName": "Fallback Supplier"}],
    )
    write_csv(
        extract_run / "dbo.vPurchaseOrder.csv",
        ["DocKey", "DocNo", "DocDate", "CreditorCode", "CreditorName", "PurchaseLocation", "DocStatus", "Cancelled"],
        [
            {
                "DocKey": "100",
                "DocNo": "PO-001",
                "DocDate": "2026-06-18",
                "CreditorCode": "SUP-001",
                "CreditorName": "Synthetic Supplier",
                "PurchaseLocation": "MAIN",
                "DocStatus": "OPEN",
                "Cancelled": "F",
            }
        ],
    )
    write_csv(extract_run / "dbo.PO.csv", ["DocKey", "DocNo"], [{"DocKey": "101", "DocNo": "PO-002"}])
    write_csv(
        extract_run / "dbo.PODTL.csv",
        ["DtlKey", "ItemCode", "Description", "UOM", "Qty", "TransferedQty", "Location"],
        [
            {
                "DtlKey": "200",
                "ItemCode": "ITEM-001",
                "Description": "Synthetic item",
                "UOM": "PCS",
                "Qty": "10",
                "TransferedQty": "2",
                "Location": "MAIN",
            }
        ],
    )
    for object_id in [
        "dbo.vGoodsReceivedNote",
        "dbo.GR",
        "dbo.vGoodsReceivedNoteDetail",
        "dbo.vGoodsReceivedNoteSubDetail",
        "dbo.GRDTL",
        "dbo.vStockReceive",
        "dbo.vStockReceiveDetail",
        "dbo.vStockTransfer",
        "dbo.XFER",
        "dbo.vStockTransferDetail",
    ]:
        write_csv(extract_run / f"{object_id}.csv", ["DocNo", "ItemCode"], [])
    write_csv(extract_run / "dbo.StockDTL.csv", ["DocType", "ItemCode"], [{"DocType": "PO", "ItemCode": "ITEM-001"}])
    write_csv(extract_run / "dbo.vItemBalQty.csv", ["ItemCode"], [{"ItemCode": "ITEM-001"}])
    manifest = synthetic_extract_manifest(extract_run)
    (extract_run / "inventory_operation_extract_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def synthetic_extract_manifest(extract_run):
    surfaces = {
        "dbo.vCreditor": ("supplier_context", 1),
        "dbo.Creditor": ("supplier_context", 1),
        "dbo.vPurchaseOrder": ("outstanding_po_in_transit", 1),
        "dbo.PO": ("outstanding_po_in_transit", 1),
        "dbo.PODTL": ("outstanding_po_in_transit", 1),
        "dbo.vGoodsReceivedNote": ("grn_header", 0),
        "dbo.GR": ("grn_header", 0),
        "dbo.vGoodsReceivedNoteDetail": ("grn_detail", 0),
        "dbo.vGoodsReceivedNoteSubDetail": ("grn_detail", 0),
        "dbo.GRDTL": ("grn_detail", 0),
        "dbo.vStockReceive": ("stock_receive", 0),
        "dbo.vStockReceiveDetail": ("stock_receive", 0),
        "dbo.vStockTransfer": ("transfer_header", 0),
        "dbo.XFER": ("transfer_header", 0),
        "dbo.vStockTransferDetail": ("transfer_detail", 0),
        "dbo.StockDTL": ("movement_stock_reference", 1),
        "dbo.vItemBalQty": ("movement_stock_reference", 1),
    }
    return {
        "job": "autocount_inventory_operation_extract",
        "status": "success_with_warnings",
        "run_id": "extract-run-123",
        "storage": {"run_path": str(extract_run)},
        "decision": "Needs reconciliation",
        "business_reconciliation_status": "not_reconciled",
        "data_maturity": "immature_pre_go_live",
        "final_production_selected": False,
        "warnings": ["Data is thin"],
        "surface_exports": [
            {
                "object_id": object_id,
                "business_function": function,
                "status": "success",
                "row_count": row_count,
                "output_path": str(extract_run / f"{object_id}.csv"),
                "file_name": f"{object_id}.csv",
                "selected_columns": [],
                "missing_expected_columns": [],
            }
            for object_id, (function, row_count) in surfaces.items()
        ],
    }


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_header(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


def table_result(manifest, table_name):
    for table in manifest["staging_tables"]:
        if table["table_name"] == table_name:
            return table
    return None


if __name__ == "__main__":
    unittest.main()
