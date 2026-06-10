import csv
import sys
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_phase1_reconcile as reconcile
from scripts import autocount_sql_probe as probe
from scripts import autocount_stock_extract as extract
from scripts.csv_safety import coerce_csv_cell, neutralize_csv_formula_cell


class CsvSafetyTests(unittest.TestCase):
    def test_formula_prefix_strings_are_neutralized(self):
        dangerous_values = ["=1+1", "+cmd", "-2+3", "@SUM(1,1)"]

        for value in dangerous_values:
            with self.subTest(value=value):
                self.assertEqual(neutralize_csv_formula_cell(value), f"'{value}")

    def test_leading_whitespace_and_control_characters_are_neutralized(self):
        dangerous_values = ["\t=WEBSERVICE('https://example.invalid')", " \r\n=HYPERLINK('x')", "\x00\x1f+cmd"]

        for value in dangerous_values:
            with self.subTest(value=value):
                self.assertEqual(neutralize_csv_formula_cell(value), f"'{value}")

    def test_safe_strings_none_dates_and_numeric_values_are_coerced_deterministically(self):
        self.assertEqual(coerce_csv_cell("SKU-001"), "SKU-001")
        self.assertEqual(coerce_csv_cell("'=1+1"), "'=1+1")
        self.assertEqual(coerce_csv_cell(None), "")
        self.assertEqual(coerce_csv_cell(datetime(2026, 6, 4, 12, 18, 7)), "2026-06-04T12:18:07")
        self.assertEqual(coerce_csv_cell(date(2026, 6, 4)), "2026-06-04")
        self.assertEqual(coerce_csv_cell(Decimal("-2")), Decimal("-2"))
        self.assertEqual(coerce_csv_cell(-2), -2)
        self.assertEqual(coerce_csv_cell(-2.5), -2.5)

    def test_stock_extract_write_csv_neutralizes_formula_cells(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "stock_master.csv"

            extract.write_csv(path, [{"ItemCode": "=1+1", "Qty": Decimal("-2")}])

            rows = read_csv(path)
            self.assertEqual(rows[0]["ItemCode"], "'=1+1")
            self.assertEqual(rows[0]["Qty"], "-2")
            self.assertFalse(rows[0]["ItemCode"].startswith("="))

    def test_sql_probe_write_csv_neutralizes_formula_cells_in_sample_style_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "dbo.Item.csv"

            probe.write_csv(path, [{"ItemCode": "\t=WEBSERVICE('x')", "Description": "Normal"}])

            rows = read_csv(path)
            self.assertEqual(rows[0]["ItemCode"], "'\t=WEBSERVICE('x')")
            self.assertEqual(rows[0]["Description"], "Normal")
            self.assertFalse(rows[0]["ItemCode"].lstrip().startswith("="))

    def test_phase1_reconcile_write_csv_neutralizes_formula_cells_after_redaction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "location_counts.csv"

            reconcile.write_csv(path, [{"object_id": "dbo.StockDTL", "location_code": " @SUM(1,1)", "row_count": 2}])

            rows = read_csv(path)
            self.assertEqual(rows[0]["location_code"], "' @SUM(1,1)")
            self.assertEqual(rows[0]["row_count"], "2")
            self.assertNotIn("Password=", path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()
