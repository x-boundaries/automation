"""Offline tests for the one-off AC1 sales history build (#292).

Synthetic workbooks only, built in a temp folder with zipfile: every item code, description and
amount here is fake. No AutoCount, SQL Server, Drive or network access.
"""

import gzip
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ac1_sales_history_build as b  # noqa: E402

SECRET_DESC = "FAKE-SECRET-DESCRIPTION"
SECRET_AMOUNT = "987654.321"
HEADER = list(b.EXPECTED_HEADER)


def _col(n):
    return b._col_letter(n)


def write_xlsx(path, rows, inline_codes=False):
    """rows: list of lists (row 1 = header). str -> shared string, int/float -> number, None -> empty."""
    sst, sst_ix = [], {}

    def s(text):
        if text not in sst_ix:
            sst_ix[text] = len(sst)
            sst.append(text)
        return sst_ix[text]

    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, val in enumerate(row, start=1):
            ref = "%s%d" % (_col(c), r)
            if val is None:
                continue
            if isinstance(val, str):
                if inline_codes and c == 1 and r > 1:
                    cells.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, escape(val)))
                else:
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, s(val)))
            else:
                cells.append('<c r="%s"><v>%s</v></c>' % (ref, val))
        xml_rows.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    main = b.NS_MAIN
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="%s"><sheetData>%s</sheetData>'
             '</worksheet>' % (main, "".join(xml_rows)))
    shared = ('<?xml version="1.0" encoding="UTF-8"?><sst xmlns="%s" count="%d">%s</sst>' % (
        main, len(sst), "".join("<si><t>%s</t></si>" % escape(t) for t in sst)))
    workbook = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="%s" xmlns:r="%s"><sheets>'
                '<sheet name="Sheet" sheetId="1" r:id="rId1"/></sheets></workbook>' % (main, b.NS_REL))
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="%s"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>' % b.NS_PKG_REL)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml", shared)


def item_row(code, qty, desc=SECRET_DESC):
    return [code, desc, desc, "FAKE-GRP", "FAKE-BRAND", "EACH", qty] + [SECRET_AMOUNT] * 7


def total_row(qty_total):
    return [None] * 6 + [qty_total, SECRET_AMOUNT, SECRET_AMOUNT, SECRET_AMOUNT,
                         "9.99%", "9.99%", "1.00", "1.00"]


def month_rows(*items):
    return [HEADER] + [item_row(c, q) for c, q in items] + [total_row(sum(q for _, q in items))]


def write_index(path, codes):
    doc = {"schema": "fake", "item_fields": ["code", "description"],
           "items": [[c, SECRET_DESC] for c in codes], "as_of_local": "2026-11-09T00:00:00+08:00"}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh)


class Ac1HistoryBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ac1 = self.root / "ac1"
        self.ac1.mkdir()
        self.index = self.root / "stock_item_index.json.gz"
        write_index(self.index, ["FAKE0001", "fake0002 ", "FAKE0003"])
        self.out = self.root / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def standard_set(self):
        write_xlsx(self.ac1 / "2025-11.xlsx", month_rows(("FAKE0001", 5), ("FAKE0009", -1)))
        # 2025-12 missing -> gap
        write_xlsx(self.ac1 / "2026-01.xlsx", month_rows((" fake0002", 3), ("FAKE0001", 0)))
        write_xlsx(self.ac1 / "2026-02.xlsx", month_rows(("FAKE0003", 7)))
        write_xlsx(self.ac1 / "2026-03.xlsx", month_rows(("FAKE0003", 100)))  # after cut-over
        write_xlsx(self.ac1 / ".FAKE ITEM LIST.xlsx", [["Tick", "Item Code"], ["x", "FAKE0001"]])

    def run_build(self, **kw):
        kw.setdefault("last_month", "2026-02")
        kw.setdefault("partial_months", ("2026-01",))
        return b.build(self.ac1, self.index, built_at="2026-11-09T00:00:00+08:00", **kw)

    def test_happy_path_structure(self):
        self.standard_set()
        data, manifest = self.run_build()
        self.assertEqual(data["schema"], b.DATA_SCHEMA)
        self.assertEqual(data["months"], ["2025-11", "2026-01", "2026-02"])
        self.assertEqual(data["gaps"], ["2025-12"])
        self.assertEqual(data["partial_months"], ["2026-01"])
        self.assertEqual(data["ac2_from_month"], "2026-03")
        self.assertEqual(data["items"], ["FAKE0001", "FAKE0009", "FAKE0002", "FAKE0003"])
        self.assertEqual(data["item_mapped"], [True, False, True, True])
        self.assertEqual(data["columns"]["item"], [0, 1, 2, 0, 3])
        self.assertEqual(data["columns"]["month"], [0, 0, 1, 1, 2])
        self.assertEqual(data["columns"]["sold"], [5, -1, 3, 0, 7])
        self.assertEqual(data["month_units"], [4, 3, 7])
        self.assertEqual(data["month_items"], [2, 2, 1])
        c = data["counts"]
        self.assertEqual((c["units"], c["unmapped_items"], c["unmapped_units"]), (14, 1, -1))
        self.assertEqual((c["zero_rows"], c["negative_rows"]), (1, 1))
        self.assertEqual(manifest["files_after_last_month_excluded"], ["2026-03.xlsx"])
        self.assertEqual(manifest["files_ignored"], [".FAKE ITEM LIST.xlsx"])
        self.assertEqual([s["total_rows_skipped"] for s in manifest["source_files"]], [1, 1, 1])
        self.assertEqual(manifest["columns_read"], ["A Item Code", "G Sold Qty"])

    def test_outputs_are_units_only_and_deterministic(self):
        self.standard_set()
        data, manifest = self.run_build()
        out, final = b.write_outputs(self.out, self.ac1, data, manifest)
        payload = (out / b.DATA_NAME).read_bytes()
        text = gzip.decompress(payload).decode("ascii") + (out / b.MANIFEST_NAME).read_text("ascii")
        for secret in (SECRET_DESC, SECRET_AMOUNT, "FAKE-GRP", "FAKE-BRAND", "9.99%"):
            self.assertNotIn(secret, text)
        self.assertEqual(final["data_file"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(b.encode_data(data), payload)  # fixed gzip time, sorted keys
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted([b.DATA_NAME, b.MANIFEST_NAME]))

    def test_description_strings_are_never_resolved(self):
        write_xlsx(self.ac1 / "2026-01.xlsx", month_rows(("FAKE0001", 1)))
        seen = []
        real = b._shared_strings

        def spy(z, wanted):
            found = real(z, wanted)
            seen.extend(found.values())
            return found

        b._shared_strings = spy
        try:
            b.read_month_file(self.ac1 / "2026-01.xlsx")
        finally:
            b._shared_strings = real
        self.assertIn("FAKE0001", seen)
        self.assertNotIn(SECRET_DESC, seen)
        self.assertNotIn("FAKE-GRP", seen)

    def test_inline_string_codes(self):
        write_xlsx(self.ac1 / "2026-01.xlsx", month_rows(("FAKE0001", 2)), inline_codes=True)
        rows, info = b.read_month_file(self.ac1 / "2026-01.xlsx")
        self.assertEqual(rows, [("FAKE0001", 2)])

    def assert_fails(self, rows, fragment, name="2026-01.xlsx"):
        write_xlsx(self.ac1 / name, rows)
        with self.assertRaises(b.BuildError) as ctx:
            b.read_month_file(self.ac1 / name)
        msg = str(ctx.exception)
        self.assertIn(fragment, msg)
        for secret in ("FAKE0001", "FAKE0002", SECRET_DESC, SECRET_AMOUNT):
            self.assertNotIn(secret, msg)

    def test_header_mismatch_fails(self):
        bad = list(HEADER)
        bad[6] = "Qty"
        self.assert_fails([bad, item_row("FAKE0001", 1)], "header row")
        self.assert_fails([HEADER + ["Extra"], item_row("FAKE0001", 1)], "header row")

    def test_duplicate_code_fails(self):
        self.assert_fails(month_rows(("FAKE0001", 1), ("fake0001", 2)), "repeated")

    def test_fractional_qty_fails(self):
        self.assert_fails([HEADER, item_row("FAKE0001", 1.5)], "whole number")

    def test_text_qty_fails(self):
        self.assert_fails([HEADER, item_row("FAKE0001", "1")], "not a number")

    def test_numeric_code_fails(self):
        self.assert_fails([HEADER, item_row(1234567890123, 1)], "not text")

    def test_codeless_row_before_last_fails(self):
        rows = [HEADER, total_row(1), item_row("FAKE0001", 1)]
        self.assert_fails(rows, "before the last row")

    def test_not_a_zip_fails(self):
        (self.ac1 / "2026-01.xlsx").write_bytes(b"not a zip")
        with self.assertRaises(b.BuildError):
            b.read_month_file(self.ac1 / "2026-01.xlsx")

    def test_out_dir_guards(self):
        self.standard_set()
        data, manifest = self.run_build()
        with self.assertRaises(b.BuildError):
            b.write_outputs(self.ac1 / "sub", self.ac1, data, manifest)
        archive = self.root / "archive"
        archive.mkdir()
        (archive / "last_run_status.json").write_text("{}")
        with self.assertRaises(b.BuildError):
            b.write_outputs(archive, self.ac1, data, manifest)
        self.assertEqual(sorted(p.name for p in archive.iterdir()), ["last_run_status.json"])

    def test_no_month_files_or_bad_settings_fail(self):
        with self.assertRaises(b.BuildError):
            self.run_build()
        self.standard_set()
        with self.assertRaises(b.BuildError):
            self.run_build(last_month="2026-13")
        with self.assertRaises(b.BuildError):
            self.run_build(partial_months=("Jan-2026",))

    def test_bad_item_index_fails(self):
        self.standard_set()
        self.index.write_bytes(b"not gzip")
        with self.assertRaises(b.BuildError):
            self.run_build()

    def test_main_exit_codes(self):
        self.standard_set()
        args = ["--ac1-dir", str(self.ac1), "--item-index", str(self.index), "--out-dir",
                str(self.out), "--last-month", "2026-02", "--partial-months", ""]
        self.assertEqual(b.main(args), 0)
        manifest = json.loads((self.out / b.MANIFEST_NAME).read_text("ascii"))
        self.assertEqual(manifest["partial_months"], [])
        self.assertEqual(b.main(args[:-4] + ["--last-month", "bad"]), 2)


if __name__ == "__main__":
    unittest.main()
