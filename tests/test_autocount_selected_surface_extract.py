import csv
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

from scripts import autocount_selected_surface_extract as extract


class FakeSelectedSurfaceExtractSource:
    def __init__(self, rows_by_surface=None):
        self.rows_by_surface = rows_by_surface or {}
        self.queries = []

    def fetch_context(self):
        return {
            "current_database": "AED_XBOUNDARIES",
            "current_login": "xb_ac2_readonly",
            "current_user_name": "xb_ac2_readonly",
        }

    def fetch_rows(self, surface_spec):
        self.queries.append(surface_spec["sql"])
        return list(self.rows_by_surface.get(surface_spec["surface_id"], []))


class SelectedSurfaceExtractTests(unittest.TestCase):
    def test_example_config_is_secret_free_and_default_scope_is_narrow(self):
        config_path = ROOT / "config" / "autocount_selected_surface_extract.example.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))
        enabled_ids = [spec["surface_id"] for spec in extract.build_extract_definitions(config)]
        skipped = {item["surface_id"]: item["reason"] for item in extract.skipped_surface_specs(config)}

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\extract\selected_surfaces")
        self.assertTrue(config["include_views"])
        self.assertFalse(config["enable_gl_transaction"])
        self.assertFalse(config["enable_ap_ar_detail"])
        self.assertIsNone(config["max_rows"])
        self.assertEqual(
            enabled_ids,
            [
                "dbo.Debtor",
                "dbo.vDebtor",
                "dbo.Creditor",
                "dbo.vCreditor",
                "dbo.PaymentMethod",
                "dbo.PO",
                "dbo.PODTL",
                "dbo.vPurchaseOrder",
                "dbo.ARInvoice",
                "dbo.APInvoice",
            ],
        )
        self.assertEqual(skipped["dbo.GLDTL"], "disabled_until_enable_gl_transaction_true")
        self.assertEqual(skipped["dbo.ARInvoiceDTL"], "disabled_until_enable_ap_ar_detail_true")
        self.assertEqual(skipped["dbo.APInvoiceDTL"], "disabled_until_enable_ap_ar_detail_true")
        self.assertEqual(skipped["coa_account_master"], "unresolved_no_confirmed_account_master_surface")
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")

    def test_extract_sql_is_read_only_and_uses_explicit_column_allowlists(self):
        definitions = extract.build_extract_definitions(base_config(r"C:\XB\tmp"))
        sql_text = "\n".join(spec["sql"] for spec in definitions)

        self.assertNotRegex(sql_text, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql_text, r"(?i)\bSELECT\s+\*")
        self.assertNotIn("[AccNo]", sql_text)
        for spec in definitions:
            self.assertEqual(extract.extract_sql_selected_columns(spec["sql"]), spec["columns"])
            self.assertIn("ORDER BY", spec["sql"])

    def test_max_rows_uses_top_clause_and_as_of_metadata_does_not_filter(self):
        config = base_config(r"C:\XB\tmp")
        config["max_rows"] = 25
        config["as_of_date"] = "2026-06-16"

        definitions = extract.build_extract_definitions(config)

        self.assertTrue(definitions)
        self.assertTrue(all("SELECT TOP (25)" in spec["sql"] for spec in definitions))
        self.assertTrue(all(" WHERE " not in spec["sql"].upper() for spec in definitions))

    def test_run_extraction_writes_safe_csv_manifest_and_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = FakeSelectedSurfaceExtractSource(
                {
                    "dbo.Debtor": [
                        {
                            "DebtorCode": "=XB001",
                            "CompanyName": "Formula Test",
                            "IsActive": True,
                            "TaxType": "SV",
                            "Phone1": "+6012",
                            "EmailAddress": "ops@example.invalid",
                            "LastModified": datetime(2026, 6, 16, 15, 43, 57),
                        }
                    ],
                    "dbo.PODTL": [
                        {
                            "DocKey": "POKEY-1",
                            "DtlKey": "DTL-1",
                            "DocNo": "PO-001",
                            "ItemCode": "SKU-001",
                            "Description": "Widget",
                            "Qty": Decimal("20.0000"),
                            "TransferedQty": Decimal("0.0000"),
                            "OutstandingQty": Decimal("20.0000"),
                            "UOM": "PCS",
                            "DeliveryDate": date(2026, 6, 20),
                        }
                    ],
                    "dbo.ARInvoice": [
                        {
                            "DocNo": "AR-001",
                            "DocDate": date(2026, 6, 16),
                            "DebtorCode": "D001",
                            "NetTotal": Decimal("123.45"),
                            "Outstanding": Decimal("23.45"),
                            "Cancelled": False,
                        }
                    ],
                }
            )

            manifest = extract.run_extraction(
                base_config(tmpdir),
                source=source,
                now=datetime.fromisoformat("2026-06-16T09:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads(
                (run_path / "selected_surface_extract_manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(saved_manifest["status"], "success")
            self.assertEqual(saved_manifest["data_maturity"], "immature_pre_go_live")
            self.assertEqual(saved_manifest["business_reconciliation_status"], "not_reconciled")
            self.assertEqual(saved_manifest["source_of_truth_status"], "current_ac2_database_snapshot")
            self.assertEqual(saved_manifest["decision"], "Needs reconciliation")
            self.assertFalse(saved_manifest["final_production_selected"])
            self.assertEqual(saved_manifest["exception_count"], 0)
            self.assertEqual(saved_manifest["context"]["current_database"], "AED_XBOUNDARIES")
            self.assertEqual(saved_manifest["row_counts"]["dbo.Debtor"], 1)
            self.assertEqual(saved_manifest["row_counts"]["dbo.PODTL"], 1)
            self.assertIn("dbo.GLDTL", {item["surface_id"] for item in saved_manifest["skipped_surfaces"]})
            self.assertIn("coa_account_master", {item["surface_id"] for item in saved_manifest["skipped_surfaces"]})
            self.assert_path_under(saved_manifest["storage"]["run_path"], tmpdir)
            self.assert_path_under(saved_manifest["storage"]["manifest"], tmpdir)
            for output_path in saved_manifest["output_files"].values():
                self.assert_path_under(output_path, tmpdir)

            debtor_path = Path(saved_manifest["output_files"]["dbo.Debtor"])
            self.assertTrue(debtor_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with debtor_path.open("r", encoding="utf-8-sig", newline="") as handle:
                debtor_rows = list(csv.DictReader(handle))
            self.assertEqual(debtor_rows[0]["DebtorCode"], "'=XB001")
            self.assertEqual(debtor_rows[0]["LastModified"], "2026-06-16T15:43:57")

            podtl_path = Path(saved_manifest["output_files"]["dbo.PODTL"])
            with podtl_path.open("r", encoding="utf-8-sig", newline="") as handle:
                podtl_rows = list(csv.DictReader(handle))
            self.assertEqual(podtl_rows[0]["Qty"], "20.0000")
            self.assertEqual(podtl_rows[0]["DeliveryDate"], "2026-06-20")

            report_text = (run_path / "selected_surface_extract_report.md").read_text(encoding="utf-8")
            for warning in extract.WARNINGS:
                self.assertIn(warning, report_text)
            self.assertIn("CoA/account master remains unresolved.", report_text)
            self.assertTrue(source.queries)
            self.assertTrue(all("SELECT *" not in query.upper() for query in source.queries))

    def test_output_roots_and_local_configs_are_guarded_by_gitignore(self):
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("autocount_selected_surface_extract.local.json", gitignore_text)
        self.assertIn("config/autocount_selected_surface_extract.local.json", gitignore_text)
        self.assertIn("selected_surface_extract_outputs/", gitignore_text)
        self.assertIn("autocount_outputs/**/selected_surface_extract_manifest.json", gitignore_text)
        self.assertIn("autocount_outputs/**/selected_surface_extract_report.md", gitignore_text)
        self.assertIn("autocount_outputs/**/*.csv", gitignore_text)
        with self.assertRaises(ValueError):
            extract.resolve_output_root(ROOT / "outputs" / "selected_surface_extract", repo_root=ROOT)

    def test_create_source_resolves_connection_string_only_from_default_env(self):
        original = os.environ.get("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = "Driver={local};Password=secret;"
        try:
            source = extract.create_source(base_config(tempfile.gettempdir()))
        finally:
            if original is None:
                os.environ.pop("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING", None)
            else:
                os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = original

        self.assertEqual(source.connection_string, "Driver={local};Password=secret;")

    def test_default_gates_keep_gl_detail_and_coa_unresolved(self):
        config = base_config(r"C:\XB\tmp")

        enabled_ids = [spec["surface_id"] for spec in extract.build_extract_definitions(config)]
        skipped = {item["surface_id"]: item["reason"] for item in extract.skipped_surface_specs(config)}

        self.assertNotIn("dbo.GLDTL", enabled_ids)
        self.assertNotIn("dbo.ARInvoiceDTL", enabled_ids)
        self.assertNotIn("dbo.APInvoiceDTL", enabled_ids)
        self.assertNotIn("coa_account_master", enabled_ids)
        self.assertEqual(skipped["dbo.GLDTL"], "disabled_until_enable_gl_transaction_true")
        self.assertEqual(skipped["dbo.ARInvoiceDTL"], "disabled_until_enable_ap_ar_detail_true")
        self.assertEqual(skipped["dbo.APInvoiceDTL"], "disabled_until_enable_ap_ar_detail_true")
        self.assertEqual(skipped["coa_account_master"], "unresolved_no_confirmed_account_master_surface")

    def test_explicit_flags_enable_gl_and_ar_ap_detail_surfaces(self):
        config = base_config(r"C:\XB\tmp")
        config["enable_gl_transaction"] = True
        config["enable_ap_ar_detail"] = True

        enabled_ids = [spec["surface_id"] for spec in extract.build_extract_definitions(config)]
        skipped_ids = {item["surface_id"] for item in extract.skipped_surface_specs(config)}

        self.assertIn("dbo.GLDTL", enabled_ids)
        self.assertIn("dbo.ARInvoiceDTL", enabled_ids)
        self.assertIn("dbo.APInvoiceDTL", enabled_ids)
        self.assertNotIn("dbo.GLDTL", skipped_ids)
        self.assertNotIn("dbo.ARInvoiceDTL", skipped_ids)
        self.assertNotIn("dbo.APInvoiceDTL", skipped_ids)
        self.assertIn("coa_account_master", skipped_ids)

    def assert_path_under(self, path, root):
        resolved_path = Path(path).resolve(strict=False)
        resolved_root = Path(root).resolve(strict=False)
        resolved_path.relative_to(resolved_root)


def base_config(output_root):
    return {
        "job": "autocount_selected_surface_extract",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "include_views": True,
        "enable_gl_transaction": False,
        "enable_ap_ar_detail": False,
        "max_rows": None,
        "business_date": "",
        "as_of_date": "",
        "surfaces": {
            "Debtor": surface("dbo", "Debtor", ["DebtorCode", "CompanyName", "IsActive", "TaxType", "Phone1", "EmailAddress", "LastModified"], ["DebtorCode"]),
            "vDebtor": surface("dbo", "vDebtor", ["DebtorCode", "CompanyName", "IsActive", "TaxType", "Phone1", "EmailAddress", "LastModified"], ["DebtorCode"]),
            "Creditor": surface("dbo", "Creditor", ["CreditorCode", "CompanyName", "IsActive", "TaxType", "Phone1", "EmailAddress", "LastModified"], ["CreditorCode"]),
            "vCreditor": surface("dbo", "vCreditor", ["CreditorCode", "CompanyName", "IsActive", "TaxType", "Phone1", "EmailAddress", "LastModified"], ["CreditorCode"]),
            "PaymentMethod": surface("dbo", "PaymentMethod", ["PaymentMethod", "Description", "IsActive", "LastModified"], ["PaymentMethod"]),
            "PO": surface("dbo", "PO", ["DocKey", "DocNo", "DocDate", "CreditorCode", "CreditorName", "NetTotal", "LocalNetTotal", "Cancelled", "Closed", "LastModified"], ["DocNo"]),
            "PODTL": surface("dbo", "PODTL", ["DocKey", "DtlKey", "DocNo", "ItemCode", "Description", "Qty", "TransferedQty", "OutstandingQty", "UOM", "DeliveryDate"], ["DocNo", "DtlKey"]),
            "vPurchaseOrder": surface("dbo", "vPurchaseOrder", ["DocNo", "PONo", "DocDate", "CreditorCode", "CreditorName", "ItemCode", "Description", "Qty", "TransferedQty", "OutstandingQty", "UOM"], ["DocNo", "ItemCode"]),
            "ARInvoice": surface("dbo", "ARInvoice", ["DocNo", "DocDate", "DebtorCode", "DebtorName", "NetTotal", "LocalNetTotal", "Outstanding", "OutstandingAmt", "Cancelled", "LastModified"], ["DocNo"]),
            "APInvoice": surface("dbo", "APInvoice", ["DocNo", "DocDate", "CreditorCode", "CreditorName", "NetTotal", "LocalNetTotal", "Outstanding", "OutstandingAmt", "Cancelled", "LastModified"], ["DocNo"]),
            "ARInvoiceDTL": surface("dbo", "ARInvoiceDTL", ["DocNo", "DtlKey", "ItemCode", "Description", "Qty", "UOM", "Amount"], ["DocNo", "DtlKey"], enabled=False, requires_flag="enable_ap_ar_detail"),
            "APInvoiceDTL": surface("dbo", "APInvoiceDTL", ["DocNo", "DtlKey", "ItemCode", "Description", "Qty", "UOM", "Amount"], ["DocNo", "DtlKey"], enabled=False, requires_flag="enable_ap_ar_detail"),
            "GLDTL": surface("dbo", "GLDTL", ["JournalNo", "DocNo", "TransDate", "AccNo", "Debit", "Credit", "Description"], ["TransDate", "JournalNo"], enabled=False, requires_flag="enable_gl_transaction"),
        },
    }


def surface(schema_name, object_name, columns, order_by, enabled=True, requires_flag=""):
    return {
        "schema_name": schema_name,
        "object_name": object_name,
        "enabled": enabled,
        "columns": columns,
        "order_by": order_by,
        "requires_flag": requires_flag,
    }


if __name__ == "__main__":
    unittest.main()
