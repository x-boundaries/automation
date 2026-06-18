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

from scripts import autocount_inventory_operation_extract as extract


class FakeInventoryOperationExtractSource:
    def __init__(self, context=None, rows_by_surface=None, failures=None, columns_by_surface=None):
        self.context = context or {
            "server_name": "XB-AC2\\A2006",
            "current_database": "AED_XBOUNDARIES",
            "current_login": "xb_ac2_readonly",
            "current_user_name": "xb_ac2_readonly",
        }
        self.rows_by_surface = rows_by_surface or {
            "dbo.vGoodsReceivedNote": [],
            "dbo.GR": [{"DocNo": "=GR-001", "DocDate": "2026-06-18", "CreditorCode": "SUP-001"}],
            "dbo.PODTL": [{"DocNo": "PO-001", "ItemCode": "+ITEM-001", "TransferedQty": 1}],
        }
        self.failures = set(failures or [])
        self.columns_by_surface = columns_by_surface or {}
        self.fetch_calls = []

    def fetch_context(self):
        return dict(self.context)

    def fetch_columns(self, selected_surfaces):
        records = []
        for surface in selected_surfaces:
            object_id = extract.make_surface_id(surface["schema_name"], surface["object_name"])
            for column in self.columns_by_surface.get(object_id, surface.get("expected_columns", [])):
                records.append(
                    {
                        "object_schema": surface["schema_name"],
                        "object_name": surface["object_name"],
                        "column_name": column,
                        "data_type": "nvarchar",
                        "is_nullable": True,
                        "column_id": 1,
                    }
                )
        return records

    def fetch_rows(self, surface_spec):
        object_id = surface_spec["object_id"]
        self.fetch_calls.append(surface_spec)
        if object_id in self.failures:
            raise RuntimeError(f"SELECT failed for {object_id}; Password=secret;")
        return list(self.rows_by_surface.get(object_id, []))


class InventoryOperationExtractTests(unittest.TestCase):
    def test_example_config_is_secret_free_and_allowlisted(self):
        config = json.loads((ROOT / "config" / "autocount_inventory_operation_extract.example.json").read_text())

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\extract\inventory_operations")
        self.assertEqual(config["expected_server"], r"localhost\A2006")
        self.assertEqual(config["expected_database"], "AED_XBOUNDARIES")
        self.assertEqual(config["expected_login"], "xb_ac2_readonly")
        self.assertIsNone(config["row_limit"])
        self.assertNotIn("connection_string", config)
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")
        surface_ids = {extract.make_surface_id(surface["schema_name"], surface["object_name"]) for surface in config["selected_surfaces"]}
        self.assertIn("dbo.vGoodsReceivedNote", surface_ids)
        self.assertIn("dbo.vStockTransfer", surface_ids)
        self.assertIn("dbo.vItemBalQty", surface_ids)
        self.assertNotIn("dbo.GLDTL", surface_ids)
        self.assertNotIn("coa_account_master", surface_ids)
        surface_by_id = {
            extract.make_surface_id(surface["schema_name"], surface["object_name"]): surface
            for surface in config["selected_surfaces"]
        }
        self.assertEqual(surface_by_id["dbo.Creditor"]["expected_columns"], ["AccNo"])
        self.assertEqual(surface_by_id["dbo.Creditor"]["order_by"], ["AccNo"])
        self.assertEqual(surface_by_id["dbo.vCreditor"]["expected_columns"], ["CreditorCode"])
        self.assertEqual(surface_by_id["dbo.vCreditor"]["order_by"], ["CreditorCode"])
        self.assertNotIn("DocNo", surface_by_id["dbo.PODTL"]["expected_columns"])
        self.assertNotIn("PostToStock", surface_by_id["dbo.PODTL"]["expected_columns"])
        self.assertNotIn("DocNo", surface_by_id["dbo.StockDTL"]["expected_columns"])
        self.assertNotIn("LocationBalQty", surface_by_id["dbo.vItemBalQty"]["expected_columns"])

    def test_build_extract_sql_is_read_only_explicit_and_allowlisted(self):
        surface = selected_surface("dbo", "GR", ["DocNo", "DocDate"], ["DocNo"], True)
        sql = extract.build_extract_sql(surface, ["DocNo", "DocDate"], order_by=["DocNo"], row_limit=25)

        self.assertIn("SELECT TOP (25) [DocNo], [DocDate]", sql)
        self.assertIn("FROM [dbo].[GR]", sql)
        self.assertIn("ORDER BY [DocNo]", sql)
        self.assertNotRegex(sql, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql, r"(?i)\bSELECT\s+\*")

    def test_rejects_non_allowlisted_surface(self):
        config = base_config(tempfile.gettempdir())
        config["selected_surfaces"].append(selected_surface("dbo", "GLDTL", ["DocNo"], ["DocNo"], True))

        with tempfile.TemporaryDirectory() as tmpdir:
            config["output_root"] = tmpdir
            manifest = extract.run_extraction(
                config,
                source=FakeInventoryOperationExtractSource(),
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "failed")
        self.assertRegex("\n".join(exception["message"] for exception in manifest["exceptions"]), "not allowlisted")

    def test_zero_row_surface_writes_empty_csv_and_safe_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(tmpdir),
                source=FakeInventoryOperationExtractSource(),
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads(
                (run_path / "inventory_operation_extract_manifest.json").read_text(encoding="utf-8")
            )
            report_text = (run_path / "inventory_operation_extract_report.md").read_text(encoding="utf-8")

            self.assertEqual(saved_manifest["status"], "success")
            self.assertEqual(saved_manifest["decision"], "Needs reconciliation")
            self.assertEqual(saved_manifest["data_maturity"], "immature_pre_go_live")
            self.assertEqual(saved_manifest["business_reconciliation_status"], "not_reconciled")
            self.assertFalse(saved_manifest["final_production_selected"])
            zero_surface = surface_export(saved_manifest, "dbo.vGoodsReceivedNote")
            self.assertEqual(zero_surface["row_count"], 0)
            self.assertTrue(Path(zero_surface["output_path"]).exists())
            self.assertEqual(read_csv(Path(zero_surface["output_path"])), [])
            self.assertIn("Generated raw snapshot files are local-only", saved_manifest["generated_output_disclaimer"])
            self.assertNotIn("PO-001", report_text)
            self.assertNotIn("ITEM-001", report_text)

    def test_csv_formula_injection_is_neutralized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(tmpdir),
                source=FakeInventoryOperationExtractSource(),
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )

            gr_export = surface_export(manifest, "dbo.GR")
            podtl_export = surface_export(manifest, "dbo.PODTL")
            self.assertEqual(read_csv(Path(gr_export["output_path"]))[0]["DocNo"], "'=GR-001")
            self.assertEqual(read_csv(Path(podtl_export["output_path"]))[0]["ItemCode"], "'+ITEM-001")

    def test_missing_expected_columns_are_warnings_not_blockers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(tmpdir),
                source=FakeInventoryOperationExtractSource(columns_by_surface={"dbo.GR": ["DocNo"]}),
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )

        gr_export = surface_export(manifest, "dbo.GR")
        self.assertEqual(manifest["status"], "success_with_warnings")
        self.assertEqual(gr_export["missing_expected_columns"], ["DocDate", "CreditorCode"])
        self.assertIn("missing_expected_columns:dbo.GR", manifest["warnings"])
        self.assertEqual(gr_export["row_count"], 1)

    def test_zero_matching_expected_columns_skips_surface_without_invalid_sql(self):
        config = base_config(tempfile.gettempdir())
        for surface in config["selected_surfaces"]:
            if extract.make_surface_id(surface["schema_name"], surface["object_name"]) == "dbo.PODTL":
                surface["expected_columns"] = ["MissingColumn"]
                surface["order_by"] = ["MissingColumn"]
        source = FakeInventoryOperationExtractSource(columns_by_surface={"dbo.PODTL": []})

        with tempfile.TemporaryDirectory() as tmpdir:
            config["output_root"] = tmpdir
            manifest = extract.run_extraction(
                config,
                source=source,
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )

        podtl_export = surface_export(manifest, "dbo.PODTL")
        podtl_calls = [call for call in source.fetch_calls if call["object_id"] == "dbo.PODTL"]
        self.assertEqual(manifest["status"], "success_with_warnings")
        self.assertEqual(podtl_export["status"], "skipped")
        self.assertEqual(podtl_export["selected_columns"], [])
        self.assertIn("no_selected_columns:dbo.PODTL", manifest["warnings"])
        self.assertEqual(podtl_calls, [])
        all_sql = "\n".join(call["sql"] for call in source.fetch_calls)
        self.assertNotIn("SELECT [MissingColumn]", all_sql)

    def test_surface_extraction_failure_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(tmpdir),
                source=FakeInventoryOperationExtractSource(failures={"dbo.PODTL"}),
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "success_with_warnings")
        self.assertIn("surface_extract_failed:dbo.PODTL", manifest["warnings"])
        self.assertEqual(surface_export(manifest, "dbo.PODTL")["status"], "failed")
        self.assertRegex("\n".join(exception["message"] for exception in manifest["exceptions"]), "Password=<redacted>")
        self.assertEqual(surface_export(manifest, "dbo.GR")["row_count"], 1)

    def test_all_critical_surfaces_failing_causes_failed_status(self):
        config = base_config(tempfile.gettempdir())
        critical_ids = {
            extract.make_surface_id(surface["schema_name"], surface["object_name"])
            for surface in config["selected_surfaces"]
            if surface.get("critical")
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config["output_root"] = tmpdir
            manifest = extract.run_extraction(
                config,
                source=FakeInventoryOperationExtractSource(failures=critical_ids),
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "failed")
        self.assertIn("all_critical_surfaces_failed", manifest["warnings"])

    def test_wrong_target_context_fails_before_fetching_rows(self):
        source = FakeInventoryOperationExtractSource(
            context={
                "server_name": "localhost\\SQLEXPRESS",
                "current_database": "AED_XBoundaries",
                "current_login": "sa",
                "current_user_name": "dbo",
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(tmpdir),
                source=source,
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(source.fetch_calls, [])
        self.assertRegex("\n".join(exception["message"] for exception in manifest["exceptions"]), "SQLEXPRESS")
        self.assertRegex("\n".join(exception["message"] for exception in manifest["exceptions"]), "AED_XBoundaries")

    def test_output_root_inside_repo_is_rejected(self):
        with self.assertRaises(ValueError):
            extract.resolve_output_root(ROOT / "local_extract_outputs")

    def test_console_summary_and_report_do_not_include_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(tmpdir),
                source=FakeInventoryOperationExtractSource(),
                now=datetime.fromisoformat("2026-06-18T16:00:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            report_text = (run_path / "inventory_operation_extract_report.md").read_text(encoding="utf-8")
            summary_text = json.dumps(extract.build_console_summary(manifest))

        for raw_value in ["PO-001", "ITEM-001", "SUP-001", "=GR-001"]:
            self.assertNotIn(raw_value, report_text)
            self.assertNotIn(raw_value, summary_text)

    def test_local_outputs_and_configs_are_guarded_by_gitignore(self):
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("autocount_inventory_operation_extract.local.json", gitignore_text)
        self.assertIn("config/autocount_inventory_operation_extract.local.json", gitignore_text)
        self.assertIn("inventory_operation_extract_outputs/", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_operation_extract_manifest.json", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_operation_extract_report.md", gitignore_text)

    def test_runbook_includes_local_command_and_guardrails(self):
        runbook = (
            ROOT / "docs" / "autocount2-automation" / "inventory_operation_raw_snapshot_runbook.md"
        ).read_text(encoding="utf-8")

        self.assertIn("python scripts\\autocount_inventory_operation_extract.py", runbook)
        self.assertIn("config\\autocount_inventory_operation_extract.local.json", runbook)
        self.assertIn("C:\\XB\\autocount_outputs\\extract\\inventory_operations", runbook)
        self.assertIn("raw snapshot", runbook)
        self.assertIn("final_production_selected=false", runbook)
        self.assertIn("Do not commit generated outputs", runbook)

    def test_create_source_uses_only_default_env_var(self):
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


def base_config(output_root):
    return {
        "job": "autocount_inventory_operation_extract",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "expected_server": r"localhost\A2006",
        "expected_database": "AED_XBOUNDARIES",
        "expected_login": "xb_ac2_readonly",
        "reject_server_patterns": ["SQLEXPRESS"],
        "reject_databases": ["AED_XBoundaries", "A893478"],
        "row_limit": None,
        "selected_surfaces": [
            selected_surface("dbo", "vGoodsReceivedNote", ["DocNo", "DocDate", "CreditorCode"], ["DocNo"], True),
            selected_surface("dbo", "GR", ["DocNo", "DocDate", "CreditorCode"], ["DocNo"], True),
            selected_surface("dbo", "PODTL", ["DocNo", "ItemCode", "TransferedQty"], ["ItemCode"], False),
        ],
    }


def selected_surface(schema_name, object_name, expected_columns, order_by, critical=False):
    return {
        "surface_name": object_name,
        "business_function": "test",
        "schema_name": schema_name,
        "object_name": object_name,
        "expected_columns": list(expected_columns),
        "order_by": list(order_by),
        "critical": critical,
    }


def surface_export(manifest, object_id):
    for export in manifest["surface_exports"]:
        if export["object_id"] == object_id:
            return export
    return None


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()
