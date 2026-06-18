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

from scripts import autocount_inventory_operation_validate as validate


class FakeInventoryOperationValidationSource:
    def __init__(self, context=None, missing=None, unreadable=None, aggregate_failures=None):
        self.context = context or {
            "server_name": "XB-AC2\\A2006",
            "current_database": "AED_XBOUNDARIES",
            "current_login": "xb_ac2_readonly",
            "current_user_name": "xb_ac2_readonly",
        }
        self.missing = set(missing or [])
        self.unreadable = set(unreadable or [])
        self.aggregate_failures = set(aggregate_failures or [])
        self.aggregate_calls = []

    def fetch_context(self):
        return dict(self.context)

    def fetch_surface_inventory(self, selected_surfaces):
        records = []
        for surface in selected_surfaces:
            object_id = validate.make_surface_id(surface["schema_name"], surface["object_name"])
            if object_id in self.missing:
                continue
            records.append(
                {
                    "object_schema": surface["schema_name"],
                    "object_name": surface["object_name"],
                    "object_type": "VIEW" if surface["object_name"].startswith("v") else "USER_TABLE",
                }
            )
        return records

    def fetch_columns(self, selected_surfaces):
        records = []
        for surface in selected_surfaces:
            object_id = validate.make_surface_id(surface["schema_name"], surface["object_name"])
            if object_id in self.missing:
                continue
            for column_name in column_names_for(surface["object_name"]):
                records.append(column_record(surface["schema_name"], surface["object_name"], column_name))
        return records

    def fetch_surface_aggregates(self, surface, aggregate_spec):
        object_id = validate.make_surface_id(surface["schema_name"], surface["object_name"])
        if object_id in self.unreadable:
            raise PermissionError(f"SELECT permission denied for {object_id}; Password=secret;")
        if object_id in self.aggregate_failures:
            raise RuntimeError(f"Aggregate failed for {object_id}; PWD=secret;")
        self.aggregate_calls.append((object_id, aggregate_spec))
        return {
            "row_count": 12,
            "blank_DocNo": 0,
            "distinct_DocNo": 10,
            "distinct_ItemCode": 8,
            "min_DocDate": "2026-01-01",
            "max_DocDate": "2026-06-18",
            "sample_doc_no": "PO-0001",
            "sample_item_code": "ITEM-ABC",
        }


class InventoryOperationValidationTests(unittest.TestCase):
    def test_example_config_is_secret_free_and_selected_profile_scoped(self):
        config = json.loads((ROOT / "config" / "autocount_inventory_operation_validate.example.json").read_text())

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\probe\inventory_operations_validation")
        self.assertEqual(config["expected_server"], r"localhost\A2006")
        self.assertEqual(config["expected_database"], "AED_XBOUNDARIES")
        self.assertEqual(config["expected_login"], "xb_ac2_readonly")
        self.assertNotIn("connection_string", config)
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")
        surface_ids = {validate.make_surface_id(s["schema_name"], s["object_name"]) for s in config["selected_surfaces"]}
        self.assertIn("dbo.vGoodsReceivedNote", surface_ids)
        self.assertIn("dbo.vStockTransfer", surface_ids)
        self.assertIn("dbo.PODTL", surface_ids)
        self.assertNotIn("dbo.GLDTL", surface_ids)
        self.assertNotIn("coa_account_master", surface_ids)

    def test_build_aggregate_sql_is_read_only_explicit_and_aggregate_only(self):
        surface = selected_surface("dbo", "vGoodsReceivedNote", ["DocNo", "DocDate"], ["DocNo"], ["DocNo"], ["DocDate"])
        sql = validate.build_aggregate_sql(surface, {"DocNo", "DocDate"})

        self.assertNotRegex(sql, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql, r"(?i)\bSELECT\s+\*")
        self.assertIn("COUNT_BIG(1)", sql)
        self.assertIn("COUNT(DISTINCT", sql)
        self.assertIn("MIN(", sql)
        self.assertIn("MAX(", sql)
        self.assertNotRegex(sql, r"(?i)\bTOP\b|ORDER\s+BY")

    def test_run_validation_writes_safe_manifest_and_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validate.run_validation(
                base_config(tmpdir),
                source=FakeInventoryOperationValidationSource(),
                now=datetime.fromisoformat("2026-06-18T13:00:00+08:00"),
            )

            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads(
                (run_path / "inventory_operation_validation_manifest.json").read_text(encoding="utf-8")
            )
            report_text = (run_path / "inventory_operation_validation_report.md").read_text(encoding="utf-8")

        self.assertEqual(saved_manifest["status"], "success")
        self.assertEqual(saved_manifest["decision"], "Needs reconciliation")
        self.assertEqual(saved_manifest["data_maturity"], "immature_pre_go_live")
        self.assertEqual(saved_manifest["business_reconciliation_status"], "not_reconciled")
        self.assertFalse(saved_manifest["final_production_selected"])
        surface = surface_result(saved_manifest, "dbo.vGoodsReceivedNote")
        self.assertTrue(surface["exists"])
        self.assertTrue(surface["readable"])
        self.assertEqual(surface["object_type"], "VIEW")
        self.assertEqual(surface["row_count"], 12)
        self.assertIn("DocNo", surface["expected_columns_present"])
        self.assertEqual(surface["missing_expected_columns"], [])
        self.assertEqual(surface["blank_counts"]["DocNo"], 0)
        self.assertEqual(surface["distinct_counts"]["DocNo"], 10)
        self.assertEqual(surface["date_ranges"]["DocDate"]["min"], "2026-01-01")
        self.assertEqual(surface["date_ranges"]["DocDate"]["max"], "2026-06-18")
        self.assert_no_raw_values(saved_manifest)
        self.assertIn("aggregate-only", report_text)
        self.assertIn("Needs reconciliation", report_text)
        self.assertIn("not_reconciled", report_text)
        self.assertIn("Final production selected: false", report_text)

    def test_missing_selected_surface_warns_without_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validate.run_validation(
                base_config(tmpdir),
                source=FakeInventoryOperationValidationSource(missing={"dbo.vGoodsReceivedNoteDetail"}),
                now=datetime.fromisoformat("2026-06-18T13:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "success_with_warnings")
        surface = surface_result(manifest, "dbo.vGoodsReceivedNoteDetail")
        self.assertFalse(surface["exists"])
        self.assertFalse(surface["readable"])
        self.assertIn("surface_missing:dbo.vGoodsReceivedNoteDetail", manifest["warnings"])
        self.assertEqual(manifest["exception_count"], 0)

    def test_unreadable_surface_warns_without_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validate.run_validation(
                base_config(tmpdir),
                source=FakeInventoryOperationValidationSource(unreadable={"dbo.GR"}),
                now=datetime.fromisoformat("2026-06-18T13:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "success_with_warnings")
        surface = surface_result(manifest, "dbo.GR")
        self.assertTrue(surface["exists"])
        self.assertFalse(surface["readable"])
        self.assertIn("surface_unreadable:dbo.GR", manifest["warnings"])
        self.assertRegex("\n".join(manifest["exceptions"]), "Password=<redacted>")

    def test_aggregate_failure_warns_without_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validate.run_validation(
                base_config(tmpdir),
                source=FakeInventoryOperationValidationSource(aggregate_failures={"dbo.PODTL"}),
                now=datetime.fromisoformat("2026-06-18T13:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "success_with_warnings")
        surface = surface_result(manifest, "dbo.PODTL")
        self.assertTrue(surface["exists"])
        self.assertTrue(surface["readable"])
        self.assertEqual(surface["aggregate_status"], "failed")
        self.assertIn("aggregate_failed:dbo.PODTL", manifest["warnings"])
        self.assertRegex("\n".join(manifest["exceptions"]), "PWD=<redacted>")

    def test_wrong_target_context_fails_before_aggregate_validation(self):
        source = FakeInventoryOperationValidationSource(
            {
                "server_name": "localhost\\SQLEXPRESS",
                "current_database": "AED_XBoundaries",
                "current_login": "sa",
                "current_user_name": "dbo",
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validate.run_validation(
                base_config(tmpdir),
                source=source,
                now=datetime.fromisoformat("2026-06-18T13:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["selected_surface_results"], [])
        self.assertRegex("\n".join(manifest["exceptions"]), "SQLEXPRESS")
        self.assertRegex("\n".join(manifest["exceptions"]), "AED_XBoundaries")

    def test_all_critical_surfaces_missing_fails(self):
        config = base_config(tempfile.gettempdir())
        critical_ids = {
            validate.make_surface_id(surface["schema_name"], surface["object_name"])
            for surface in config["selected_surfaces"]
            if surface.get("critical")
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config["output_root"] = tmpdir
            manifest = validate.run_validation(
                config,
                source=FakeInventoryOperationValidationSource(missing=critical_ids),
                now=datetime.fromisoformat("2026-06-18T13:00:00+08:00"),
            )

        self.assertEqual(manifest["status"], "failed")
        self.assertIn("all_critical_surfaces_missing", manifest["warnings"])

    def test_local_outputs_and_configs_are_guarded_by_gitignore(self):
        gitignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("autocount_inventory_operation_validate.local.json", gitignore_text)
        self.assertIn("config/autocount_inventory_operation_validate.local.json", gitignore_text)
        self.assertIn("inventory_operation_validation_outputs/", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_operation_validation_manifest.json", gitignore_text)
        self.assertIn("autocount_outputs/**/inventory_operation_validation_report.md", gitignore_text)

    def test_runbook_includes_safe_local_command_and_boundaries(self):
        runbook = (
            ROOT
            / "docs"
            / "autocount2-automation"
            / "inventory_operation_aggregate_validation_runbook.md"
        ).read_text(encoding="utf-8")

        self.assertIn("python scripts\\autocount_inventory_operation_validate.py", runbook)
        self.assertIn("config\\autocount_inventory_operation_validate.local.json", runbook)
        self.assertIn("C:\\XB\\autocount_outputs\\probe\\inventory_operations_validation", runbook)
        self.assertIn("raw ERP/business rows", runbook)
        self.assertIn("XFERUDF_GIT", runbook)
        self.assertIn("column evidence on", runbook)
        self.assertIn("final_production_selected=false", runbook)

    def test_create_source_uses_only_default_env_var(self):
        original = os.environ.get("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = "Driver={local};Password=secret;"
        try:
            source = validate.create_source(base_config(tempfile.gettempdir()))
        finally:
            if original is None:
                os.environ.pop("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING", None)
            else:
                os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = original

        self.assertEqual(source.connection_string, "Driver={local};Password=secret;")

    def assert_no_raw_values(self, value):
        text = json.dumps(value)
        forbidden_values = ["PO-0001", "ITEM-ABC", "sample_doc_no", "sample_item_code", "raw_rows", "sample_rows"]
        for forbidden in forbidden_values:
            self.assertNotIn(forbidden, text)


def base_config(output_root):
    return {
        "job": "autocount_inventory_operation_validate",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "expected_server": r"localhost\A2006",
        "expected_database": "AED_XBOUNDARIES",
        "expected_login": "xb_ac2_readonly",
        "reject_server_patterns": ["SQLEXPRESS"],
        "reject_databases": ["AED_XBoundaries", "A893478"],
        "selected_surfaces": [
            selected_surface("dbo", "vGoodsReceivedNote", ["DocNo", "DocDate"], ["DocNo"], ["DocNo"], ["DocDate"], True),
            selected_surface("dbo", "GR", ["DocNo", "DocDate"], ["DocNo"], ["DocNo"], ["DocDate"], True),
            selected_surface("dbo", "vGoodsReceivedNoteDetail", ["DocNo", "ItemCode"], ["ItemCode"], ["DocNo", "ItemCode"]),
            selected_surface("dbo", "PODTL", ["DocNo", "ItemCode", "TransferedQty"], ["ItemCode"], ["DocNo", "ItemCode"]),
        ],
    }


def selected_surface(
    schema_name,
    object_name,
    expected_columns,
    critical_columns,
    safe_key_columns,
    safe_date_columns=None,
    critical=False,
):
    return {
        "surface_name": object_name,
        "business_function": "test",
        "schema_name": schema_name,
        "object_name": object_name,
        "expected_columns": list(expected_columns),
        "critical_columns": list(critical_columns),
        "safe_key_columns": list(safe_key_columns),
        "safe_date_columns": list(safe_date_columns or []),
        "critical": critical,
    }


def column_names_for(object_name):
    columns = {
        "vGoodsReceivedNote": ["DocNo", "DocDate", "CreditorCode"],
        "GR": ["DocNo", "DocDate", "CreditorCode"],
        "vGoodsReceivedNoteDetail": ["DocNo", "ItemCode", "DtlKey"],
        "PODTL": ["DocNo", "ItemCode", "TransferedQty"],
    }
    return columns.get(object_name, ["DocNo"])


def column_record(schema_name, object_name, column_name):
    return {
        "object_schema": schema_name,
        "object_name": object_name,
        "column_name": column_name,
        "data_type": "nvarchar",
        "is_nullable": True,
        "column_id": 1,
    }


def surface_result(manifest, object_id):
    for result in manifest["selected_surface_results"]:
        if result["object_id"] == object_id:
            return result
    return None


if __name__ == "__main__":
    unittest.main()
