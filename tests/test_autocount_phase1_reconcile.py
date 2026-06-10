import csv
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_phase1_reconcile as reconcile


class FakeReconcileSource(reconcile.SqlServerSummarySource):
    def __init__(self):
        self.columns = {
            "dbo.Item": ["ItemCode", "UOM", "Barcode", "ItemGroup", "IsActive"],
            "dbo.Location": ["Location"],
            "dbo.vItemUOMBalQty": ["ItemCode", "Location", "UOM", "BalQty"],
            "dbo.StockDTL": ["ItemCode", "DocDate", "Location", "Qty", "TotalCost"],
        }

    def fetch_column_inventory(self, objects):
        rows = []
        for obj in objects:
            oid = reconcile.object_id(obj)
            for column in self.columns.get(oid, []):
                rows.append({"object_id": oid, "column_name": column, "data_type": "nvarchar", "is_nullable": "YES"})
        return rows

    def fetch_columns(self, objects):
        return {reconcile.object_id(obj): self.columns.get(reconcile.object_id(obj), []) for obj in objects}

    def fetch_object_counts(self, objects):
        return [{"object_id": reconcile.object_id(obj), "row_count": 2} for obj in objects]

    def query(self, sql, params=None):
        if "MIN([DocDate])" in sql and "StockDTL" in sql:
            return [{"row_count": 2, "min_doc_date": datetime(2026, 6, 4), "max_doc_date": datetime(2026, 6, 5), "total_qty": Decimal("3.5"), "total_cost": Decimal("12.25")}]
        if "GROUP BY [Location]" in sql:
            return [{"location_code": "MAIN", "row_count": 2}, {"location_code": "", "row_count": 1}]
        if "BalQty" in sql:
            return [{"row_count": 2, "total_bal_qty": Decimal("10.5"), "min_bal_qty": Decimal("-1"), "max_bal_qty": Decimal("11.5")}]
        return [{"row_count": 2, "min_doc_date": datetime(2026, 6, 4), "max_doc_date": datetime(2026, 6, 5)}]


class AutoCountPhase1ReconcileTests(unittest.TestCase):
    def test_manifest_shape_and_safe_output_only_no_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = reconcile.run_reconciliation(
                base_config(tmpdir),
                source=FakeReconcileSource(),
                now=datetime.fromisoformat("2026-06-06T09:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads((run_path / "phase1_reconcile_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(saved_manifest["status"], "success")
            self.assertRegex(saved_manifest["run_id"], r"^[0-9a-f-]{36}$")
            self.assertEqual(saved_manifest["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
            self.assertTrue(saved_manifest["safe_outputs_only"])
            self.assertFalse(saved_manifest["raw_rows_exported"])
            self.assertNotIn('"rows"', json.dumps(saved_manifest).lower())
            self.assertNotIn("SKU-001", run_path.read_text if False else json.dumps(saved_manifest))
            for filename in reconcile.SUMMARY_FILES.values():
                self.assertTrue((run_path / filename).exists())
            self.assertTrue((run_path / "column_inventory.csv").exists())
            self.assertIn("column_inventory", saved_manifest["output_files"])
            self.assertIn("column_inventory", saved_manifest["counts"])
            report_text = (run_path / "phase1_reconcile_report.md").read_text(encoding="utf-8")
            self.assertIn("column_inventory.csv", report_text)
            self.assertTrue((run_path / "phase1_reconcile_report.md").exists())

    def test_column_coverage_logic(self):
        rows = reconcile.build_column_coverage(
            [{"schema_name": "dbo", "object_name": "vItemUOMBalQty"}],
            {"dbo.vItemUOMBalQty": ["ItemCode", "Location", "UOM", "BalQty"]},
            {"stock_balance": ["ItemCode", "Location", "UOM", "BalQty", "LastModified"]},
        )

        self.assertEqual(rows[0]["present_column_count"], 4)
        self.assertEqual(rows[0]["missing_column_count"], 1)
        self.assertEqual(rows[0]["missing_columns"], "LastModified")
        self.assertEqual(rows[0]["status"], "needs_mapping")

    def test_date_location_balance_and_movement_summary_logic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = reconcile.run_reconciliation(base_config(tmpdir), source=FakeReconcileSource())
            run_path = Path(manifest["storage"]["run_path"])

            date_rows = read_csv(run_path / "date_ranges.csv")
            self.assertTrue(any(row["object_id"] == "dbo.StockDTL" and row["min_doc_date"].startswith("2026-06-04") for row in date_rows))

            location_rows = read_csv(run_path / "location_counts.csv")
            self.assertTrue(any(row["location_code"] == "MAIN" for row in location_rows))
            self.assertTrue(any(row["location_code"] == "<blank>" for row in location_rows))

            balance_rows = read_csv(run_path / "stock_balance_summary.csv")
            self.assertEqual(balance_rows[0]["total_bal_qty"], "10.5")

            movement_rows = read_csv(run_path / "movement_summary.csv")
            self.assertEqual(movement_rows[0]["object_id"], "dbo.StockDTL")
            self.assertEqual(movement_rows[0]["total_qty"], "3.5")
            self.assertEqual(movement_rows[0]["total_cost"], "12.25")


    def test_column_inventory_contains_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = reconcile.run_reconciliation(base_config(tmpdir), source=FakeReconcileSource())
            run_path = Path(manifest["storage"]["run_path"])

            inventory_rows = read_csv(run_path / "column_inventory.csv")
            self.assertTrue(inventory_rows)
            self.assertEqual(set(inventory_rows[0]), {"object_id", "column_name", "data_type", "is_nullable"})
            self.assertTrue(any(row["object_id"] == "dbo.Item" and row["column_name"] == "ItemCode" for row in inventory_rows))
            inventory_text = (run_path / "column_inventory.csv").read_text(encoding="utf-8")
            self.assertNotIn("SKU-001", inventory_text)
            self.assertNotIn("Sample Item", inventory_text)
            self.assertNotIn("row_count", inventory_text)

    def test_no_connection_string_or_secrets_are_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = reconcile.run_reconciliation(base_config(tmpdir), source=FakeReconcileSource())
            run_path = Path(manifest["storage"]["run_path"])
            output_text = "\n".join(path.read_text(encoding="utf-8") for path in run_path.iterdir() if path.is_file())

            self.assertNotIn("Driver=", output_text)
            self.assertNotIn("Password=", output_text)
            self.assertNotIn("secret", output_text.lower())
            self.assertNotIn("Server=prod", output_text)

    def test_secret_redaction(self):
        text = "Password=secret; PWD=other; token=abc; Server=prod; Database=Demo;"

        redacted = reconcile.sanitize_text(text)

        self.assertNotIn("secret", redacted)
        self.assertNotIn("other", redacted)
        self.assertNotIn("abc", redacted)
        self.assertNotIn("prod", redacted)
        self.assertIn("<redacted>", redacted)

    def test_output_path_must_be_outside_repo(self):
        with self.assertRaises(ValueError):
            reconcile.resolve_output_root(ROOT / "outputs" / "autocount_phase1", repo_root=ROOT)

    def test_dry_run_works_without_sql_connection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = reconcile.run_reconciliation(base_config(tmpdir), dry_run=True)
            run_path = Path(manifest["storage"]["run_path"])

            self.assertTrue(manifest["dry_run"])
            self.assertTrue((run_path / "phase1_reconcile_manifest.json").exists())
            self.assertTrue((run_path / "column_inventory.csv").exists())
            self.assertEqual(read_csv(run_path / "column_inventory.csv"), [])
            self.assertIn("Dry run", (run_path / "phase1_reconcile_report.md").read_text(encoding="utf-8"))


def read_csv(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def base_config(output_root):
    return {
        "job": "autocount_phase1_reconcile",
        "target_label": "AutoCount 2.2 confirmed target",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "objects": ["dbo.Item", "dbo.Location", "dbo.vItemUOMBalQty", "dbo.StockDTL"],
        "required_contracts": {
            "stock_balance": ["ItemCode", "Location", "UOM", "BalQty", "LastModified"],
            "stock_movement": ["ItemCode", "DocDate", "Location", "Qty", "TotalCost"],
        },
    }


if __name__ == "__main__":
    unittest.main()
