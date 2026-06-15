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

from scripts import autocount_broader_surface_discovery as discovery


REQUIRED_GROUPS = {
    "debtor_customer",
    "creditor_supplier",
    "chart_of_accounts_gl",
    "ar_ap_opening",
    "locations",
    "payment_methods",
    "purchase_order_outstanding_po",
    "stock_in_transit_candidates",
    "stock_reference_followup",
}


class FakeDiscoverySource:
    def __init__(self):
        self.row_count_calls = []

    def fetch_context(self):
        return {
            "current_database": "AED_XBOUNDARIES",
            "current_login": "xb_ac2_readonly",
            "current_user_name": "xb_ac2_readonly",
        }

    def fetch_objects(self, schemas=None):
        return [
            object_record("dbo", "Debtor", "USER_TABLE"),
            object_record("dbo", "Creditor", "USER_TABLE"),
            object_record("dbo", "GLAccount", "USER_TABLE"),
            object_record("dbo", "ARAPOpening", "USER_TABLE"),
            object_record("dbo", "Location", "USER_TABLE"),
            object_record("dbo", "PaymentMethod", "USER_TABLE"),
            object_record("dbo", "PurchaseOrder", "VIEW"),
            object_record("dbo", "StockTransfer", "VIEW"),
            object_record("dbo", "Item", "USER_TABLE"),
        ]

    def fetch_columns(self, schemas=None):
        return [
            column_record("dbo", "Debtor", "DebtorCode", "nvarchar"),
            column_record("dbo", "Debtor", "CompanyName", "nvarchar"),
            column_record("dbo", "Creditor", "CreditorCode", "nvarchar"),
            column_record("dbo", "GLAccount", "AccNo", "nvarchar"),
            column_record("dbo", "ARAPOpening", "OpeningBalance", "decimal"),
            column_record("dbo", "Location", "Location", "nvarchar"),
            column_record("dbo", "PaymentMethod", "PaymentMethod", "nvarchar"),
            column_record("dbo", "PurchaseOrder", "OutstandingQty", "decimal"),
            column_record("dbo", "StockTransfer", "FromLocation", "nvarchar"),
            column_record("dbo", "StockTransfer", "ToLocation", "nvarchar"),
            column_record("dbo", "Item", "ItemCode", "nvarchar"),
        ]

    def fetch_row_counts(self, objects):
        self.row_count_calls.append(list(objects))
        return [
            {"object_schema": "dbo", "object_name": "Debtor", "approximate_row_count": 12},
            {"object_schema": "dbo", "object_name": "Creditor", "approximate_row_count": 7},
        ]


class EmptyDiscoverySource:
    def fetch_context(self):
        return {"current_database": "AED_XBOUNDARIES"}

    def fetch_objects(self, schemas=None):
        return []

    def fetch_columns(self, schemas=None):
        return []

    def fetch_row_counts(self, objects):
        return []


class AutoCountBroaderSurfaceDiscoveryTests(unittest.TestCase):
    def test_load_config_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "autocount_broader_surface_discovery.local.json"
            path.write_text("\ufeff" + json.dumps(base_config(tmpdir)), encoding="utf-8")

            loaded = discovery.load_config(path)

            self.assertEqual(loaded["job"], "autocount_broader_surface_discovery")
            self.assertEqual(loaded["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")

    def test_example_config_contract_is_secret_free_and_complete(self):
        config_path = ROOT / "config" / "autocount_broader_surface_discovery.example.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\probe\broader_surfaces")
        self.assertEqual(set(config["discovery_groups"]), REQUIRED_GROUPS)
        self.assertNotIn("connection_string", config)
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")
        for group_config in config["discovery_groups"].values():
            self.assertEqual(set(group_config), {"keyword_hints"})
            self.assertTrue(group_config["keyword_hints"])
        self.assertIn("stock_smoke_reference_surfaces", config)
        self.assertTrue(all(surface["status"] == "reference_only_not_final_mapping" for surface in config["stock_smoke_reference_surfaces"]))

    def test_create_source_resolves_connection_string_only_from_configured_env_var(self):
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

    def test_check_sql_definitions_do_not_contain_writes_or_select_star(self):
        sql_text = "\n".join(query["sql"] for query in discovery.build_check_definitions())

        self.assertNotRegex(sql_text, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql_text, r"(?i)\bSELECT\s+\*")

    def test_run_discovery_writes_safe_manifest_and_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=FakeDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads((run_path / "broader_surface_discovery_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(saved_manifest["status"], "success")
            self.assertEqual(saved_manifest["job"], "autocount_broader_surface_discovery")
            self.assertEqual(saved_manifest["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
            self.assertEqual(saved_manifest["candidate_groups"]["debtor_customer"]["decision"], "Needs reconciliation")
            self.assertIn("dbo.Debtor", object_ids(saved_manifest["matched_objects_by_group"]["debtor_customer"]))
            self.assertIn("dbo.PaymentMethod", object_ids(saved_manifest["matched_objects_by_group"]["payment_methods"]))
            self.assertIn("dbo.StockTransfer", object_ids(saved_manifest["matched_objects_by_group"]["stock_in_transit_candidates"]))
            self.assertTrue(saved_manifest["matched_columns_by_group"]["purchase_order_outstanding_po"])
            self.assertGreaterEqual(saved_manifest["object_counts_by_group"]["stock_reference_followup"], 1)
            self.assert_no_raw_payload_keys(saved_manifest)
            self.assertTrue((run_path / "broader_surface_discovery_report.md").exists())
            self.assertIn("AutoCount UI/report reconciliation", "\n".join(saved_manifest["notes"]))

    def test_empty_no_match_metadata_is_safe_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=EmptyDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["counts"]["objects"], 0)
            self.assertEqual(manifest["counts"]["columns"], 0)
            self.assertTrue(all(count == 0 for count in manifest["object_counts_by_group"].values()))
            self.assertTrue(all(group["decision"] == "Needs reconciliation" for group in manifest["candidate_groups"].values()))
            self.assert_no_raw_payload_keys(manifest)

    def test_secret_redaction_covers_password_pwd_token_and_api_key_fragments(self):
        text = "Login failed; Password=super-secret; PWD=other; token=abc123; api key: xyz; Api-Key=hidden"

        redacted = discovery.sanitize_text(text)

        self.assertNotIn("super-secret", redacted)
        self.assertNotIn("other", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertNotIn("hidden", redacted)
        self.assertIn("<redacted>", redacted)

    def assert_no_raw_payload_keys(self, value):
        if isinstance(value, dict):
            forbidden = {"rows", "data"}
            self.assertFalse(forbidden.intersection(value), f"raw payload-like key present in {value.keys()}")
            for nested in value.values():
                self.assert_no_raw_payload_keys(nested)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_raw_payload_keys(item)


def base_config(output_root):
    return {
        "job": "autocount_broader_surface_discovery",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "schemas": ["dbo"],
        "include_row_counts": False,
        "discovery_groups": {
            "debtor_customer": {"keyword_hints": ["debtor", "customer"]},
            "creditor_supplier": {"keyword_hints": ["creditor", "supplier", "vendor"]},
            "chart_of_accounts_gl": {"keyword_hints": ["account", "gl", "ledger", "coa", "accno"]},
            "ar_ap_opening": {"keyword_hints": ["ar", "ap", "opening", "receivable", "payable"]},
            "locations": {"keyword_hints": ["location", "warehouse", "store"]},
            "payment_methods": {"keyword_hints": ["payment", "paymethod", "cash", "bank", "cheque"]},
            "purchase_order_outstanding_po": {"keyword_hints": ["purchase", "po", "order", "outstanding"]},
            "stock_in_transit_candidates": {"keyword_hints": ["transit", "transfer", "fromlocation", "tolocation"]},
            "stock_reference_followup": {"keyword_hints": ["item", "stock", "uom", "balance", "movement"]},
        },
    }


def object_record(schema_name, object_name, object_type):
    return {
        "schema_name": schema_name,
        "object_name": object_name,
        "object_type": object_type,
        "create_date": "2026-01-01T00:00:00",
        "modify_date": "2026-02-01T00:00:00",
    }


def column_record(schema_name, object_name, column_name, data_type):
    return {
        "object_schema": schema_name,
        "object_name": object_name,
        "column_name": column_name,
        "data_type": data_type,
        "max_length": 40,
        "precision": 18,
        "scale": 2,
        "is_nullable": True,
    }


def object_ids(records):
    return {record["object_id"] for record in records}


if __name__ == "__main__":
    unittest.main()
