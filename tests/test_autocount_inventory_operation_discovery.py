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

from scripts import autocount_inventory_operation_discovery as discovery


EXPECTED_FAMILIES = {
    "grn_receiving",
    "stock_transfer",
    "stock_location",
    "item_product_attributes",
    "movement_semantics",
    "purchasing_supplier_context",
    "outstanding_po_transit_support",
}


class FakeInventoryOperationSource:
    def __init__(self, context=None):
        self.context = context or {
            "server_name": "XB-AC2\\A2006",
            "current_database": "AED_XBOUNDARIES",
            "current_login": "xb_ac2_readonly",
            "current_user_name": "xb_ac2_readonly",
        }

    def fetch_context(self):
        return dict(self.context)

    def fetch_objects(self, schemas=None):
        return [
            object_record("dbo", "GR", "USER_TABLE"),
            object_record("dbo", "GRDTL", "USER_TABLE"),
            object_record("dbo", "GoodsReceive", "USER_TABLE"),
            object_record("dbo", "StockTransfer", "USER_TABLE"),
            object_record("dbo", "StockTransferDTL", "USER_TABLE"),
            object_record("dbo", "Location", "USER_TABLE"),
            object_record("dbo", "Branch", "USER_TABLE"),
            object_record("dbo", "vBranch", "VIEW"),
            object_record("dbo", "Item", "USER_TABLE"),
            object_record("dbo", "ItemUOM", "USER_TABLE"),
            object_record("dbo", "ItemBarcode", "USER_TABLE"),
            object_record("dbo", "StockDTL", "USER_TABLE"),
            object_record("dbo", "PO", "USER_TABLE"),
            object_record("dbo", "PODTL", "USER_TABLE"),
            object_record("dbo", "vPurchaseOrder", "VIEW"),
            object_record("dbo", "Creditor", "USER_TABLE"),
            object_record("dbo", "SupplierItem", "USER_TABLE"),
            object_record("dbo", "GLDTL", "USER_TABLE"),
            object_record("dbo", "BankRecon", "USER_TABLE"),
        ]

    def fetch_columns(self, schemas=None):
        return [
            column_record("dbo", "GR", "DocNo", "nvarchar"),
            column_record("dbo", "GR", "DocDate", "datetime"),
            column_record("dbo", "GR", "CreditorCode", "nvarchar"),
            column_record("dbo", "GRDTL", "ItemCode", "nvarchar"),
            column_record("dbo", "GRDTL", "Qty", "decimal"),
            column_record("dbo", "GoodsReceive", "GoodsReceiveNo", "nvarchar"),
            column_record("dbo", "GoodsReceive", "PurchaseReceiveDate", "datetime"),
            column_record("dbo", "StockTransfer", "FromLocation", "nvarchar"),
            column_record("dbo", "StockTransfer", "ToLocation", "nvarchar"),
            column_record("dbo", "StockTransferDTL", "ItemCode", "nvarchar"),
            column_record("dbo", "StockTransferDTL", "Qty", "decimal"),
            column_record("dbo", "Location", "LocationCode", "nvarchar"),
            column_record("dbo", "Branch", "BranchCode", "nvarchar"),
            column_record("dbo", "vBranch", "BranchCode", "nvarchar"),
            column_record("dbo", "Item", "ItemCode", "nvarchar"),
            column_record("dbo", "Item", "ItemBrand", "nvarchar"),
            column_record("dbo", "Item", "ItemCategory", "nvarchar"),
            column_record("dbo", "ItemUOM", "UOM", "nvarchar"),
            column_record("dbo", "ItemBarcode", "BarCode", "nvarchar"),
            column_record("dbo", "StockDTL", "DocType", "nvarchar"),
            column_record("dbo", "StockDTL", "DocNo", "nvarchar"),
            column_record("dbo", "StockDTL", "InQty", "decimal"),
            column_record("dbo", "StockDTL", "OutQty", "decimal"),
            column_record("dbo", "PO", "DocNo", "nvarchar"),
            column_record("dbo", "PO", "CreditorCode", "nvarchar"),
            column_record("dbo", "PODTL", "ItemCode", "nvarchar"),
            column_record("dbo", "PODTL", "TransferedQty", "decimal"),
            column_record("dbo", "PODTL", "OutstandingQty", "decimal"),
            column_record("dbo", "vPurchaseOrder", "DocNo", "nvarchar"),
            column_record("dbo", "Creditor", "CreditorCode", "nvarchar"),
            column_record("dbo", "SupplierItem", "Supplier", "nvarchar"),
            column_record("dbo", "SupplierItem", "LeadTime", "int"),
            column_record("dbo", "SupplierItem", "ETA", "datetime"),
            column_record("dbo", "GLDTL", "DocNo", "nvarchar"),
            column_record("dbo", "BankRecon", "DocNo", "nvarchar"),
        ]

    def fetch_row_counts(self, objects):
        return [
            {"object_schema": "dbo", "object_name": "GR", "row_count": 3},
            {"object_schema": "dbo", "object_name": "GRDTL", "row_count": 5},
            {"object_schema": "dbo", "object_name": "StockTransfer", "row_count": 2},
            {"object_schema": "dbo", "object_name": "Location", "row_count": 7},
            {"object_schema": "dbo", "object_name": "Branch", "row_count": 0},
            {"object_schema": "dbo", "object_name": "vBranch", "row_count": 0},
            {"object_schema": "dbo", "object_name": "Item", "row_count": 21831},
            {"object_schema": "dbo", "object_name": "StockDTL", "row_count": 2},
            {"object_schema": "dbo", "object_name": "PO", "row_count": 1},
            {"object_schema": "dbo", "object_name": "PODTL", "row_count": 1},
            {"object_schema": "dbo", "object_name": "vPurchaseOrder", "row_count": 1},
            {"object_schema": "dbo", "object_name": "SupplierItem", "row_count": 10},
            {"object_schema": "dbo", "object_name": "GLDTL", "row_count": 0},
        ]


class InventoryOperationDiscoveryTests(unittest.TestCase):
    def test_example_config_is_secret_free_and_inventory_scoped(self):
        config_path = ROOT / "config" / "autocount_inventory_operation_discovery.example.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\probe\inventory_operations")
        self.assertEqual(config["expected_server"], r"localhost\A2006")
        self.assertEqual(config["expected_database"], "AED_XBOUNDARIES")
        self.assertEqual(config["expected_login"], "xb_ac2_readonly")
        self.assertTrue(config["include_row_counts"])
        self.assertEqual(set(config["candidate_families"]), EXPECTED_FAMILIES)
        self.assertNotIn("connection_string", config)
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")
        self.assertNotIn("chart_of_accounts_gl", config["candidate_families"])
        self.assertNotIn("bank", config["candidate_families"])

    def test_metadata_sql_definitions_are_read_only_and_explicit(self):
        sql_text = "\n".join(query["sql"] for query in discovery.build_metadata_sql_definitions())

        self.assertNotRegex(sql_text, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql_text, r"(?i)\bSELECT\s+\*")
        self.assertIn("sys.objects", sql_text)
        self.assertIn("sys.columns", sql_text)
        self.assertIn("sys.dm_db_partition_stats", sql_text)

    def test_run_discovery_writes_safe_inventory_operation_manifest_and_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=FakeInventoryOperationSource(),
                now=datetime.fromisoformat("2026-06-18T12:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads(
                (run_path / "inventory_operation_discovery_manifest.json").read_text(encoding="utf-8")
            )
            report_text = (run_path / "inventory_operation_discovery_report.md").read_text(encoding="utf-8")

            self.assertEqual(saved_manifest["status"], "success")
            self.assertEqual(saved_manifest["job"], "autocount_inventory_operation_discovery")
            self.assertEqual(saved_manifest["decision"], "Needs reconciliation")
            self.assertFalse(saved_manifest["final_production_selected"])
            self.assertEqual(saved_manifest["data_maturity"], "immature_pre_go_live")
            self.assertEqual(saved_manifest["business_reconciliation_status"], "not_reconciled")
            self.assertEqual(saved_manifest["context"]["current_database"], "AED_XBOUNDARIES")
            self.assertEqual(set(saved_manifest["candidate_families"]), EXPECTED_FAMILIES)

            grn_candidate = candidate_by_id(saved_manifest, "grn_receiving", "dbo.GR")
            self.assertIsNotNone(grn_candidate)
            self.assertEqual(grn_candidate["schema_name"], "dbo")
            self.assertEqual(grn_candidate["object_name"], "GR")
            self.assertEqual(grn_candidate["object_type"], "USER_TABLE")
            self.assertEqual(grn_candidate["row_count"], 3)
            self.assertEqual(grn_candidate["matched_keyword_family"], "grn_receiving")
            self.assertIn("DocNo", grn_candidate["matched_column_names"])
            self.assertIn("object_name_keyword:GRN", grn_candidate["reason_codes"])
            self.assertGreater(grn_candidate["score"], 0)

            self.assertIsNotNone(candidate_by_id(saved_manifest, "stock_transfer", "dbo.StockTransfer"))
            self.assertIsNotNone(candidate_by_id(saved_manifest, "stock_location", "dbo.Location"))
            self.assertIsNotNone(candidate_by_id(saved_manifest, "item_product_attributes", "dbo.Item"))
            self.assertIsNotNone(candidate_by_id(saved_manifest, "movement_semantics", "dbo.StockDTL"))
            self.assertIsNotNone(candidate_by_id(saved_manifest, "purchasing_supplier_context", "dbo.SupplierItem"))
            self.assertIsNotNone(candidate_by_id(saved_manifest, "outstanding_po_transit_support", "dbo.PODTL"))

            branch_candidate = candidate_by_id(saved_manifest, "stock_location", "dbo.Branch")
            self.assertEqual(branch_candidate["row_count"], 0)
            self.assertIn("zero_row_location_candidate", branch_candidate["reason_codes"])

            self.assertIsNone(candidate_by_id(saved_manifest, "movement_semantics", "dbo.GLDTL"))
            self.assertIsNone(candidate_by_id(saved_manifest, "movement_semantics", "dbo.BankRecon"))
            self.assert_no_raw_payload_keys(saved_manifest)
            self.assertIn("metadata only", report_text)
            self.assertIn("dbo.GR", report_text)
            self.assertIn("Needs reconciliation", report_text)

    def test_wrong_target_context_fails_before_candidate_scoring(self):
        source = FakeInventoryOperationSource(
            {
                "server_name": "localhost\\SQLEXPRESS",
                "current_database": "AED_XBoundaries",
                "current_login": "sa",
                "current_user_name": "dbo",
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=source,
                now=datetime.fromisoformat("2026-06-18T12:30:00+08:00"),
            )

        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["candidate_summary"]["total_candidates"], 0)
        self.assertRegex("\n".join(manifest["exceptions"]), "SQLEXPRESS")
        self.assertRegex("\n".join(manifest["exceptions"]), "AED_XBoundaries")

    def test_create_source_uses_only_default_env_var(self):
        original = os.environ.get("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = "Driver={local};Password=secret;"
        try:
            source = discovery.create_source(base_config(tempfile.gettempdir()))
        finally:
            if original is None:
                os.environ.pop("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING", None)
            else:
                os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = original

        self.assertEqual(source.connection_string, "Driver={local};Password=secret;")

    def test_local_outputs_and_configs_are_guarded_by_gitignore(self):
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("autocount_inventory_operation_discovery.local.json", gitignore_text)
        self.assertIn("config/autocount_inventory_operation_discovery.local.json", gitignore_text)
        self.assertIn("inventory_operation_discovery_outputs/", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_operation_discovery_manifest.json", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_operation_discovery_report.md", gitignore_text)

    def assert_no_raw_payload_keys(self, value):
        if isinstance(value, dict):
            forbidden = {"rows", "records", "sample_rows", "raw_rows", "row_values", "business_rows"}
            self.assertTrue(forbidden.isdisjoint(value.keys()))
            for nested in value.values():
                self.assert_no_raw_payload_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                self.assert_no_raw_payload_keys(nested)


def base_config(output_root):
    return {
        "job": "autocount_inventory_operation_discovery",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "schemas": ["dbo"],
        "expected_server": r"localhost\A2006",
        "expected_database": "AED_XBOUNDARIES",
        "expected_login": "xb_ac2_readonly",
        "include_row_counts": True,
        "candidate_families": discovery.DEFAULT_CANDIDATE_FAMILIES,
    }


def object_record(schema_name, object_name, object_type):
    return {
        "schema_name": schema_name,
        "object_name": object_name,
        "object_type": object_type,
    }


def column_record(schema_name, object_name, column_name, data_type):
    return {
        "object_schema": schema_name,
        "object_name": object_name,
        "column_name": column_name,
        "data_type": data_type,
    }


def candidate_by_id(manifest, family_name, object_id):
    for candidate in manifest["candidates_by_family"].get(family_name, []):
        if candidate["object_id"] == object_id:
            return candidate
    return None


if __name__ == "__main__":
    unittest.main()
