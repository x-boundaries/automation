import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_selected_surface_reconcile as reconcile


class FakeSelectedSurfaceSource:
    def __init__(self):
        self.queries = []

    def fetch_context(self):
        return {
            "current_database": "AED_XBOUNDARIES",
            "current_login": "xb_ac2_readonly",
            "current_user_name": "xb_ac2_readonly",
        }

    def fetch_columns(self, selected_surfaces):
        return [
            column_record("dbo", "Debtor", "IsActive", "bit"),
            column_record("dbo", "vDebtor", "IsActive", "bit"),
            column_record("dbo", "Creditor", "IsActive", "bit"),
            column_record("dbo", "vCreditor", "IsActive", "bit"),
            column_record("dbo", "Branch", "BranchCode", "nvarchar"),
            column_record("dbo", "vBranch", "BranchCode", "nvarchar"),
            column_record("dbo", "PaymentMethod", "IsActive", "bit"),
            column_record("dbo", "ARInvoice", "Cancelled", "bit"),
            column_record("dbo", "ARInvoice", "Outstanding", "decimal"),
            column_record("dbo", "ARInvoice", "LocalNetTotal", "decimal"),
            column_record("dbo", "ARInvoice", "NetTotal", "decimal"),
            column_record("dbo", "ARInvoice", "PaymentAmt", "decimal"),
            column_record("dbo", "APInvoice", "Cancelled", "bit"),
            column_record("dbo", "APInvoice", "Outstanding", "decimal"),
            column_record("dbo", "APInvoice", "NetTotal", "decimal"),
            column_record("dbo", "PO", "Cancelled", "bit"),
            column_record("dbo", "PO", "NetTotal", "decimal"),
            column_record("dbo", "PODTL", "Qty", "decimal"),
            column_record("dbo", "PODTL", "TransferedQty", "decimal"),
            column_record("dbo", "GLDTL", "TransDate", "datetime"),
        ]

    def fetch_aggregate(self, sql):
        self.queries.append(sql)
        return {alias: index + 1 for index, alias in enumerate(reconcile.extract_sql_aliases(sql))}


class MissingOptionalColumnSource(FakeSelectedSurfaceSource):
    def fetch_columns(self, selected_surfaces):
        return [
            column_record("dbo", "Debtor", "DebtorCode", "nvarchar"),
            column_record("dbo", "ARInvoice", "DocNo", "nvarchar"),
        ]


class DecimalAndDateAggregateSource(FakeSelectedSurfaceSource):
    def fetch_aggregate(self, sql):
        self.queries.append(sql)
        return {
            "total_count": 2,
            "outstanding_total": Decimal("123.45"),
            "max_trans_date": datetime(2026, 6, 16, 15, 43, 57),
            "nested": {
                "as_of_date": date(2026, 6, 16),
                "totals": [Decimal("1.10"), Decimal("2.20")],
            },
        }


class SelectedSurfaceReconcileTests(unittest.TestCase):
    def test_example_config_is_secret_free_and_complete(self):
        config_path = ROOT / "config" / "autocount_selected_surface_reconcile.example.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\probe\selected_surface_reconcile")
        self.assertIn("customer_master", config["selected_surfaces"])
        self.assertIn("safe_aggregate_settings", config)
        self.assertNotIn("connection_string", config)
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")

    def test_check_sql_definitions_are_aggregate_only(self):
        sql_text = "\n".join(check["sql"] for check in reconcile.build_check_definitions(base_config("C:\\XB\\tmp")))

        self.assertNotRegex(sql_text, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql_text, r"(?i)\bSELECT\s+\*")
        self.assertRegex(sql_text, r"(?i)\b(COUNT|SUM|MIN|MAX)\s*\(")
        self.assertNotRegex(sql_text, r"(?i)\b(DocNo|DebtorCode|CreditorCode|ItemCode|Description|Remark|Address|Email)\b")

    def test_run_reconciliation_writes_aggregate_only_manifest_and_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = FakeSelectedSurfaceSource()
            manifest = reconcile.run_reconciliation(
                base_config(tmpdir),
                source=source,
                now=datetime.fromisoformat("2026-06-16T09:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads(
                (run_path / "selected_surface_reconcile_manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(saved_manifest["status"], "success")
            self.assertEqual(saved_manifest["job"], "autocount_selected_surface_reconcile")
            self.assertEqual(saved_manifest["decision"], "Needs reconciliation")
            self.assertFalse(saved_manifest["final_production_selected"])
            self.assertTrue(all(area["decision"] == "Needs reconciliation" for area in saved_manifest["aggregate_results"]))
            self.assertTrue(all(area["final_production_selected"] is False for area in saved_manifest["aggregate_results"]))
            self.assertIn("dbo.ARInvoice", saved_manifest["selected_surfaces_checked"])
            self.assertIn("dbo.PODTL", saved_manifest["selected_surfaces_checked"])
            self.assert_no_forbidden_payload_keys(saved_manifest)
            self.assertTrue(source.queries)
            self.assertTrue(all("SELECT *" not in query.upper() for query in source.queries))
            self.assertTrue(all("DocNo" not in query for query in source.queries))
            self.assertTrue((run_path / "selected_surface_reconcile_report.md").exists())
            report_text = (run_path / "selected_surface_reconcile_report.md").read_text(encoding="utf-8")
            self.assertIn("aggregate-only", report_text)
            self.assertIn("not approved for extraction", report_text)

    def test_decimal_and_date_aggregate_values_serialize_safely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = DecimalAndDateAggregateSource()
            manifest = reconcile.run_reconciliation(
                base_config(tmpdir),
                source=source,
                now=datetime.fromisoformat("2026-06-16T09:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads(
                (run_path / "selected_surface_reconcile_manifest.json").read_text(encoding="utf-8")
            )

            aggregate_values = saved_manifest["aggregate_results"][0]["checks"][0]["aggregate_values"]
            self.assertEqual(aggregate_values["outstanding_total"], "123.45")
            self.assertEqual(aggregate_values["max_trans_date"], "2026-06-16T15:43:57")
            self.assertEqual(aggregate_values["nested"]["as_of_date"], "2026-06-16")
            self.assertEqual(aggregate_values["nested"]["totals"], ["1.10", "2.20"])
            self.assertEqual(saved_manifest["decision"], "Needs reconciliation")
            self.assertFalse(saved_manifest["final_production_selected"])
            self.assertTrue(all(area["decision"] == "Needs reconciliation" for area in saved_manifest["aggregate_results"]))
            self.assertTrue(all(area["final_production_selected"] is False for area in saved_manifest["aggregate_results"]))
            self.assert_no_forbidden_payload_keys(saved_manifest)

    def test_missing_optional_columns_are_skipped_safely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = reconcile.run_reconciliation(
                base_config(tmpdir),
                source=MissingOptionalColumnSource(),
                now=datetime.fromisoformat("2026-06-16T09:30:00+08:00"),
            )

            skips = [
                skip
                for area in manifest["aggregate_results"]
                for check in area["checks"]
                for skip in check.get("skipped_metrics", [])
            ]
            self.assertTrue(skips)
            self.assertTrue(any(skip["reason"] == "missing_optional_column" for skip in skips))
            self.assertEqual(manifest["status"], "success")

    def test_output_root_must_stay_outside_repo(self):
        with self.assertRaises(ValueError):
            reconcile.resolve_output_root(ROOT / "outputs" / "selected_surface_reconcile", repo_root=ROOT)

    def test_create_source_resolves_connection_string_only_from_env_var(self):
        original = os.environ.get("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = "Driver={local};Password=secret;"
        try:
            source = reconcile.create_source(base_config(tempfile.gettempdir()))
        finally:
            if original is None:
                os.environ.pop("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING", None)
            else:
                os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = original

        self.assertEqual(source.connection_string, "Driver={local};Password=secret;")

    def assert_no_forbidden_payload_keys(self, value):
        if isinstance(value, dict):
            forbidden = {"rows", "items", "records"}
            self.assertFalse(forbidden.intersection(value), f"forbidden payload-like key present in {value.keys()}")
            for nested in value.values():
                self.assert_no_forbidden_payload_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                self.assert_no_forbidden_payload_keys(nested)


def base_config(output_root):
    return {
        "job": "autocount_selected_surface_reconcile",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "schemas": ["dbo"],
        "enabled_aggregate_groups": [
            "customer_master",
            "supplier_master",
            "branch_location",
            "payment_method",
            "ar_opening",
            "ap_opening",
            "po_outstanding",
            "gl_transaction",
            "coa_account_master",
        ],
        "business_date": {"enabled": False, "as_of_date": ""},
        "safe_aggregate_settings": {"allow_status_breakdowns": False, "allow_gl_amount_totals": False},
        "selected_surfaces": {
            "customer_master": [{"schema_name": "dbo", "object_name": "Debtor"}],
            "supplier_master": [{"schema_name": "dbo", "object_name": "Creditor"}],
            "branch_location": [{"schema_name": "dbo", "object_name": "Branch"}],
            "payment_method": [{"schema_name": "dbo", "object_name": "PaymentMethod"}],
            "ar_opening": [{"schema_name": "dbo", "object_name": "ARInvoice"}],
            "ap_opening": [{"schema_name": "dbo", "object_name": "APInvoice"}],
            "po_outstanding": [
                {"schema_name": "dbo", "object_name": "PO"},
                {"schema_name": "dbo", "object_name": "PODTL"},
            ],
            "gl_transaction": [{"schema_name": "dbo", "object_name": "GLDTL"}],
            "coa_account_master": [],
        },
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
        "column_id": 1,
    }


if __name__ == "__main__":
    unittest.main()
