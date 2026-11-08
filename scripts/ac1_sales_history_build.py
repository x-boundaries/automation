"""One-off AC1 sales history build for the Buying tab (#292).

Reads the AutoCount 1 (AC1) monthly profit-margin report exports (one ``YYYY-MM.xlsx`` per
month) and writes a units-only history file the dashboard can show as its own series:

* ``ac1_sales_monthly.json.gz``        item x month: units sold (as exported, net of returns)
* ``ac1_sales_monthly_manifest.json``  source file hashes, months, gaps, counts, data file hash

Scope (owner decision, 9 Nov 2026): AC1 history feeds size mix and seasonality only. It is
labelled "AC1 history (channel mix unconfirmed)" because the exports have no location, day or
channel column and it is not known whether the report covered every channel. Stock-based views
stay AC2-only. Each month comes from one source: AC1 up to ``--last-month`` (default 2026-07),
AC2 from the month after.

Privacy contract: only two columns are ever read from a data row, A (Item Code) and G (Sold
Qty). The money and margin columns (H-N) and the description columns are never resolved: the
shared-strings table is read in a second pass that keeps only the strings column A and the
header row point to. Error messages name the file, row and column, never a cell value.

This is not part of the AC2 exporter. It never opens a database, never writes to the AC2
archive, the daily closes or the analysis files, and never touches Drive. Stdlib only.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import xml.etree.ElementTree as ET

DATA_NAME = "ac1_sales_monthly.json.gz"
MANIFEST_NAME = "ac1_sales_monthly_manifest.json"
DATA_SCHEMA = "xb.ac1.sales_monthly"
MANIFEST_SCHEMA = "xb.ac1.sales_monthly_manifest"
SCHEMA_VERSION = 1
LABEL = "AC1 history (channel mix unconfirmed)"
DEFAULT_LAST_MONTH = "2026-07"
DEFAULT_PARTIAL_MONTHS = ("2025-01",)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "autocount_outputs" / "ac1_history"

# The AC1 "Profit Margin" report layout, row 1. The whole header must match so a file with a
# different layout (where column G might not be Sold Qty) is refused, not misread.
EXPECTED_HEADER = (
    "Item Code", "Description", "Desc 2", "Item Group", "Item Type", "UOM", "Sold Qty",
    "Subtotal", "Total Cost", "Profit", "Margin(%)", "Markup(%)", "Average Subtotal",
    "Average Total Cost",
)
CODE_COL = 1  # A
QTY_COL = 7   # G
MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.xlsx$")
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_M = "{%s}" % NS_MAIN


class BuildError(Exception):
    """A validation failure. The message never contains a cell value."""


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        raise BuildError("cell without a reference")
    n = 0
    for ch in letters.group(0):
        n = n * 26 + ord(ch) - 64
    return n


def _col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _first_sheet_path(z):
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = wb.find(_M + "sheets")
    if sheets is None or len(sheets) != 1:
        raise BuildError("expected exactly one sheet")
    rid = sheets[0].get("{%s}id" % NS_REL)
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("{%s}Relationship" % NS_PKG_REL):
        if rel.get("Id") == rid:
            target = rel.get("Target").lstrip("/")
            return target if target.startswith("xl/") else "xl/" + target
    raise BuildError("sheet relationship not found")


def _shared_strings(z, wanted):
    """Second pass: resolve only the shared-string indices in ``wanted``; drop the rest."""
    found = {}
    if not wanted:
        return found
    try:
        handle = z.open("xl/sharedStrings.xml")
    except KeyError:
        raise BuildError("shared strings table missing")
    idx = -1
    with handle:
        for event, elem in ET.iterparse(handle, events=("end",)):
            if elem.tag == _M + "si":
                idx += 1
                if idx in wanted:
                    found[idx] = "".join(t.text or "" for t in elem.iter(_M + "t"))
                elem.clear()
    missing = wanted - set(found)
    if missing:
        raise BuildError("shared string index out of range")
    return found


def read_month_file(path):
    """Return (rows, info) for one monthly export.

    rows: list of (code, qty) with code trimmed and upper-cased, qty an int.
    info: item_rows, total_rows_skipped.
    """
    name = Path(path).name
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise BuildError("%s: not a readable xlsx file" % name)
    with z:
        try:
            sheet_path = _first_sheet_path(z)
            raw_rows = []  # (row number, {col: (kind, raw)}) for header + columns A and G
            with z.open(sheet_path) as handle:
                for event, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag != _M + "row":
                        continue
                    rnum = int(elem.get("r"))
                    keep_all = not raw_rows  # the first row is the header: keep every label
                    cells = {}
                    for c in elem.findall(_M + "c"):
                        col = _col_index(c.get("r"))
                        if not keep_all and col not in (CODE_COL, QTY_COL):
                            continue  # money, margin and description cells are never read
                        t = c.get("t")
                        if t == "inlineStr":
                            cells[col] = ("str", "".join(x.text or "" for x in c.iter(_M + "t")))
                            continue
                        v = c.find(_M + "v")
                        if v is None or v.text is None:
                            continue
                        if t == "s":
                            cells[col] = ("sst", int(v.text))
                        elif t in ("str", "e", "b"):
                            cells[col] = ("str" if t == "str" else "bad", v.text)
                        else:
                            cells[col] = ("num", v.text)
                    if cells:
                        raw_rows.append((rnum, cells))
                    elem.clear()
        except (KeyError, ET.ParseError, ValueError):
            raise BuildError("%s: unreadable workbook structure" % name)
        if not raw_rows:
            raise BuildError("%s: empty sheet" % name)
        wanted = set()
        for rnum, cells in raw_rows:
            for col, (kind, raw) in cells.items():
                if kind == "sst" and (rnum == raw_rows[0][0] or col == CODE_COL):
                    wanted.add(raw)
        try:
            sst = _shared_strings(z, wanted)
        except BuildError as exc:
            raise BuildError("%s: %s" % (name, exc))
        except ET.ParseError:
            raise BuildError("%s: unreadable shared strings" % name)

    def text(kind, raw):
        if kind == "sst":
            return sst[raw]
        if kind == "str":
            return raw
        return None

    hrow, hcells = raw_rows[0]
    header = tuple(
        (text(*hcells[i]) or "").strip() if i in hcells else ""
        for i in range(1, max(hcells) + 1)
    )
    if hrow != 1 or header != EXPECTED_HEADER:
        raise BuildError("%s: header row is not the AC1 profit-margin layout" % name)

    rows, seen = [], set()
    data_rows = raw_rows[1:]
    total_rows = 0
    for pos, (rnum, cells) in enumerate(data_rows):
        where = "%s row %d" % (name, rnum)
        if CODE_COL not in cells:
            if pos != len(data_rows) - 1:
                raise BuildError("%s: row without an item code before the last row" % where)
            total_rows += 1  # the report's grand-total line
            continue
        kind, raw = cells[CODE_COL]
        if kind not in ("sst", "str"):
            raise BuildError("%s: column A is not text" % where)
        code = text(kind, raw).strip().upper()
        if not code:
            raise BuildError("%s: blank item code" % where)
        if code in seen:
            raise BuildError("%s: item code repeated in the month" % where)
        seen.add(code)
        if QTY_COL not in cells or cells[QTY_COL][0] != "num":
            raise BuildError("%s: column %s is not a number" % (where, _col_letter(QTY_COL)))
        try:
            qty = Decimal(cells[QTY_COL][1])
        except InvalidOperation:
            raise BuildError("%s: column %s is not a number" % (where, _col_letter(QTY_COL)))
        if qty != qty.to_integral_value():
            raise BuildError("%s: column %s is not a whole number" % (where, _col_letter(QTY_COL)))
        rows.append((code, int(qty)))
    return rows, {"item_rows": len(rows), "total_rows_skipped": total_rows}


def load_item_index(path):
    """Return (set of AC2 item codes, info) from the exporter's stock_item_index.json.gz."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
        fields = doc["item_fields"]
        ci = fields.index("code")
        codes = {str(it[ci] or "").strip().upper() for it in doc["items"]}
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        raise BuildError("item index unreadable or not the expected schema")
    codes.discard("")
    return codes, {"as_of_local": doc.get("as_of_local"), "items": len(codes),
                   "sha256": _sha256_file(path)}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _month_seq(first, last):
    y, m = int(first[:4]), int(first[5:])
    out = []
    while "%04d-%02d" % (y, m) <= last:
        out.append("%04d-%02d" % (y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _next_month(month):
    y, m = int(month[:4]), int(month[5:])
    return "%04d-%02d" % ((y + 1, 1) if m == 12 else (y, m + 1))


def _valid_month(text):
    m = MONTH_RE.match(text or "")
    return bool(m) and 1 <= int(m.group(2)) <= 12


def build(ac1_dir, item_index_path, last_month=DEFAULT_LAST_MONTH,
          partial_months=DEFAULT_PARTIAL_MONTHS, built_at=None):
    """Return (data_doc, manifest_doc_without_data_hash). Pure apart from reading inputs."""
    if not _valid_month(last_month):
        raise BuildError("--last-month must be YYYY-MM")
    for pm in partial_months:
        if not _valid_month(pm):
            raise BuildError("--partial-months entries must be YYYY-MM")
    ac1_dir = Path(ac1_dir)
    if not ac1_dir.is_dir():
        raise BuildError("AC1 folder not found")
    ac2_codes, index_info = load_item_index(item_index_path)

    month_files, ignored, after_cutover = {}, [], []
    for entry in sorted(ac1_dir.iterdir()):
        if not entry.is_file():
            continue
        m = MONTH_FILE_RE.match(entry.name)
        if not m or not 1 <= int(m.group(2)) <= 12:
            ignored.append(entry.name)
            continue
        month = "%s-%s" % (m.group(1), m.group(2))
        if month > last_month:
            after_cutover.append(entry.name)
            continue
        month_files[month] = entry
    if not month_files:
        raise BuildError("no YYYY-MM.xlsx month files at or before the last month")

    months = sorted(month_files)
    gaps = [mo for mo in _month_seq(months[0], months[-1]) if mo not in month_files]
    item_ix, items = {}, []
    col_item, col_month, col_sold = [], [], []
    month_units, month_items, sources = [], [], []
    zero_rows = negative_rows = 0
    for mi, month in enumerate(months):
        path = month_files[month]
        rows, info = read_month_file(path)
        units = 0
        for code, qty in rows:
            if code not in item_ix:
                item_ix[code] = len(items)
                items.append(code)
            col_item.append(item_ix[code])
            col_month.append(mi)
            col_sold.append(qty)
            units += qty
            zero_rows += qty == 0
            negative_rows += qty < 0
        month_units.append(units)
        month_items.append(len(rows))
        sources.append({"name": path.name, "month": month, "sha256": _sha256_file(path),
                        "bytes": path.stat().st_size, **info})

    mapped = [code in ac2_codes for code in items]
    unit_by_item = [0] * len(items)
    for i, q in zip(col_item, col_sold):
        unit_by_item[i] += q
    counts = {
        "months": len(months),
        "gap_months": len(gaps),
        "rows": len(col_item),
        "items": len(items),
        "mapped_items": sum(mapped),
        "unmapped_items": len(items) - sum(mapped),
        "units": sum(col_sold),
        "unmapped_units": sum(u for u, ok in zip(unit_by_item, mapped) if not ok),
        "zero_rows": zero_rows,
        "negative_rows": negative_rows,
    }
    flagged = sorted(pm for pm in set(partial_months) if pm in month_files)
    data = {
        "schema": DATA_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "label": LABEL,
        "scope": "AC1 profit-margin report exports; all AC1 locations and channels the report "
                 "covered, combined; no location, day or channel split",
        "sold_rule": "Sold Qty as exported (treated as units net of returns); negative kept",
        "use": "size mix and seasonality only; stock-based views are AC2-only",
        "first_month": months[0],
        "last_month": months[-1],
        "ac2_from_month": _next_month(last_month),
        "months": months,
        "gaps": gaps,
        "partial_months": flagged,
        "items": items,
        "item_mapped": mapped,
        "fields": ["item", "month", "sold"],
        "columns": {"item": col_item, "month": col_month, "sold": col_sold},
        "month_units": month_units,
        "month_items": month_items,
        "counts": counts,
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "built_at": built_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "label": LABEL,
        "last_month_setting": last_month,
        "partial_months_setting": sorted(set(partial_months)),
        "first_month": months[0],
        "last_month": months[-1],
        "ac2_from_month": data["ac2_from_month"],
        "gaps": gaps,
        "partial_months": flagged,
        "source_files": sources,
        "files_after_last_month_excluded": after_cutover,
        "files_ignored": ignored,
        "item_index": index_info,
        "counts": counts,
        "columns_read": ["A Item Code", "G Sold Qty"],
    }
    return data, manifest


def encode_data(data):
    """Deterministic gzip bytes (fixed header time, sorted keys, compact JSON)."""
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    return buf.getvalue()


def _check_out_dir(out_dir, ac1_dir):
    out_dir = Path(out_dir).resolve()
    ac1 = Path(ac1_dir).resolve()
    if out_dir == ac1 or ac1 in out_dir.parents:
        raise BuildError("output folder must not be inside the AC1 folder")
    if (out_dir / "last_run_status.json").exists():
        raise BuildError("output folder looks like the AC2 exporter archive; use a separate folder")
    return out_dir


def _write_atomic(path, payload):
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp, "rb") as fh:
            if fh.read() != payload:
                raise BuildError("write verification failed")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_outputs(out_dir, ac1_dir, data, manifest):
    out_dir = _check_out_dir(out_dir, ac1_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = encode_data(data)
    manifest = dict(manifest)
    manifest["data_file"] = {"name": DATA_NAME, "schema": DATA_SCHEMA,
                             "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    _write_atomic(out_dir / DATA_NAME, payload)
    mbytes = (json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    _write_atomic(out_dir / MANIFEST_NAME, mbytes)  # manifest last
    return out_dir, manifest


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the units-only AC1 sales history (#292).")
    p.add_argument("--ac1-dir", required=True, help="folder holding the AC1 YYYY-MM.xlsx exports")
    p.add_argument("--item-index", required=True,
                   help="the AC2 exporter's stock_item_index.json.gz (read only, for mapping flags)")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                   help="output folder (default: autocount_outputs/ac1_history in this clone)")
    p.add_argument("--last-month", default=DEFAULT_LAST_MONTH,
                   help="last month taken from AC1, YYYY-MM; AC2 covers the months after it")
    p.add_argument("--partial-months", default=",".join(DEFAULT_PARTIAL_MONTHS),
                   help="comma-separated YYYY-MM months to flag as partial exports ('' for none)")
    args = p.parse_args(argv)
    partial = tuple(x.strip() for x in args.partial_months.split(",") if x.strip())
    try:
        data, manifest = build(args.ac1_dir, args.item_index, args.last_month, partial)
        out_dir, manifest = write_outputs(args.out_dir, args.ac1_dir, data, manifest)
    except BuildError as exc:
        print("FAILED: %s" % exc, file=sys.stderr)
        return 2
    c = manifest["counts"]
    print("OK: wrote %s and %s to %s" % (DATA_NAME, MANIFEST_NAME, out_dir))
    print("months %s..%s (%d), gaps %s, partial %s, AC2 from %s" % (
        manifest["first_month"], manifest["last_month"], c["months"], manifest["gaps"] or "none",
        manifest["partial_months"] or "none", manifest["ac2_from_month"]))
    print("items %d (mapped %d, unmapped %d), rows %d, ignored files %d, after last month %d" % (
        c["items"], c["mapped_items"], c["unmapped_items"], c["rows"],
        len(manifest["files_ignored"]), len(manifest["files_after_last_month_excluded"])))
    print("data sha256 %s" % manifest["data_file"]["sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
