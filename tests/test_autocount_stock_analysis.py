"""Offline tests for the nightly buying-analysis builders (#292 slice S2).

Pure functions only: in-memory fake rows, no SQL Server, AutoCount, Drive or n8n. Every item,
location, debtor code, brand and category here is synthetic. The exporter-level tests (nightly
gating, verified-set publishing, failure isolation) live in test_autocount_stock_balance_hourly.py.
"""

import copy
import json
import re
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_stock_analysis as an  # noqa: E402

FAKE_DEBTOR = "FAKE-DEBTOR-ONLINE"
FAKE_DEBTOR_MKT = "FAKE-DEBTOR-MKT"
LAST_DAY = date(2026, 10, 31)
GOLIVE = date(2026, 7, 31)
OS_ONLY = {"LOC-A": {"OS": "outlet"}}


def cfg(**overrides):
    raw = {
        "enabled": True,
        "golive_day": "2026-07-31",
        "stock_take_day": "2026-10-10",
        "channel_by_location": {
            "LOC-A": {"OS": "outlet"},
            "LOC-E": {"IV": "marketplace", "CN": "marketplace"},
        },
        "channel_by_debtor": {FAKE_DEBTOR: "shopify"},
        "pools": {"retail": ["LOC-A", "LOC-E"], "outlets": ["LOC-A"], "wholesale": ["LOC-W"]},
        "placement": {
            "min_hold": 2,
            "pairs": [
                {"id": "online_out_shop_has", "label": "Online out, shop has it",
                 "zero_locations": ["LOC-E"], "holder_locations": ["LOC-A"]},
                {"id": "held_at_wholesale", "label": "Held at wholesale",
                 "zero_locations": ["LOC-A", "LOC-E"], "holder_locations": ["LOC-W"]},
            ],
        },
        "highlights": {"pool": "retail", "drift_min_units": 10, "drift_pts": 5},
        "attributes": {
            "kids_categories": ["FAKE-KIDS"],
            "size_class_by_category": {"FAKE-FOOTWEAR": "footwear", "FAKE-KIDS": "kids", "FAKE-PANTS": "waist"},
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict) and key in an.NESTED_CLOSED:
            raw[key] = dict(raw[key], **value)
        else:
            raw[key] = value
    return an.validate_analysis(raw)


def arow(item, loc, day, doc_type, qty, qty_in=None, debtor_channel=None, has_debtor=0, rows=1):
    qty = Decimal(str(qty))
    return {
        "ItemCode": item,
        "Location": loc,
        "DocDay": day,
        "DocType": doc_type,
        "DebtorChannel": debtor_channel,
        "HasDebtor": has_debtor,
        "Qty": qty,
        "QtyIn": Decimal(str(qty_in)) if qty_in is not None else max(qty, Decimal(0)),
        "LedgerRows": rows,
    }


def mv(entries):
    """{day: {(item, uom, loc): qty}} from (item, loc, day, qty) tuples."""
    out = {}
    for item, loc, day, qty in entries:
        key = (item, "PCS", loc)
        day_moves = out.setdefault(day, {})
        day_moves[key] = day_moves.get(key, Decimal(0)) + Decimal(str(qty))
    return out


def item(code, brand="FAKE-BRAND", category="FAKE-JACKET", size="M", gender="M", colour="FAKE-BLUE",
         style_code="FAKE-STY", style_name="Fake style", stock="T"):
    return {
        "ItemCode": code,
        "ItemDescription": "Fake item",
        "ItemType": brand,
        "ItemBrand": brand,
        "StockControl": stock,
        "IsActive": "T",
        "ItemCategory": category,
        "ItemClass": style_code,
        "Desc2": style_name,
        "StockSize": size,
        "StockGender": gender,
        "StockColour": colour,
    }


def never_non_stock(code):
    return False


def sales_of(rows, config=None, day0=date(2026, 7, 1), last_day=LAST_DAY):
    return an.build_sales(rows, config or cfg(), never_non_stock, day0, last_day)


# ---------------------------------------------------------------------------------------------
# AC1: SELECT-only analysis read; the debtor code never leaves SQL Server
# ---------------------------------------------------------------------------------------------


class SalesQueryContractTests(unittest.TestCase):
    FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|CREATE|DROP|EXEC|EXECUTE|TRUNCATE|GRANT|INTO)\b", re.I)

    def variants(self):
        yield cfg()
        yield cfg(channel_by_debtor={})
        yield cfg(channel_by_debtor={FAKE_DEBTOR: "shopify", FAKE_DEBTOR_MKT: "marketplace"})
        yield cfg(doc_types={"sales": ["OS"], "returns": [], "receipts": ["GR"]}, channel_by_debtor={},
                  channel_by_location=OS_ONLY)
        yield cfg(doc_types={"sales": ["IV"], "returns": ["CN"], "receipts": []},
                  channel_by_location={"LOC-E": {"IV": "marketplace", "CN": "marketplace"}})

    def test_select_only_and_no_forbidden_columns(self):
        for config in self.variants():
            q = an.sales_query(config)
            self.assertTrue(q.lstrip().upper().startswith("SELECT"), q)
            self.assertIsNone(self.FORBIDDEN.search(q), q)
            lowered = q.lower()
            for word in ("cost", "price", "amount", "docno", "description", "nolock", "snapshot", "creditor"):
                self.assertNotIn(word, lowered, word)
            self.assertNotIn(";", q, "one statement")

    def test_parameter_markers_match_parameters(self):
        for config in self.variants():
            q = an.sales_query(config)
            params = an.sales_params(config, date(2026, 7, 3), LAST_DAY)
            self.assertEqual(q.count("?"), len(params))
            self.assertEqual(params[-2:], [date(2026, 7, 3), date(2026, 11, 1)], "DocDate < next local midnight")

    def test_debtor_code_is_only_a_parameter_never_selected_or_in_the_text(self):
        config = cfg(channel_by_debtor={"X'; DROP TABLE dbo.Item; --": "shopify", FAKE_DEBTOR: "shopify"})
        q = an.sales_query(config)
        self.assertNotIn(FAKE_DEBTOR, q)
        self.assertNotIn("DROP", q)
        params = an.sales_params(config, date(2026, 7, 3), LAST_DAY)
        self.assertIn(FAKE_DEBTOR, params)
        outer = q.split("FROM (", 1)[0]
        self.assertNotIn("DebtorCode", outer, "the outer select list carries the channel id, never the code")
        inner_select = q.split("FROM (", 1)[1].split("FROM dbo.StockDTL", 1)[0]
        aliases = re.findall(r"\bAS ([A-Z]\w+)", inner_select)
        self.assertEqual(aliases, ["DocDay", "DebtorChannel", "HasDebtor"])
        self.assertNotRegex(inner_select, r"DebtorCode\s*,\s*$")

    def test_only_fixed_header_tables_are_joined(self):
        q = an.sales_query(cfg())
        self.assertEqual(sorted(re.findall(r"dbo\.\[(\w+)\]", q)), ["CN", "DO", "IV"])
        self.assertIn("FROM dbo.StockDTL AS d", q)
        self.assertNotIn("dbo.[", an.sales_query(cfg(doc_types={"sales": ["OS"], "returns": [], "receipts": []},
                                                    channel_by_debtor={}, channel_by_location=OS_ONLY)))

    def test_group_by_never_repeats_a_marker(self):
        q = an.sales_query(cfg())
        self.assertNotIn("?", q.split("GROUP BY", 1)[1])


# ---------------------------------------------------------------------------------------------
# AC2: channel rules from settings, returns and the DO rule
# ---------------------------------------------------------------------------------------------


class ChannelRuleTests(unittest.TestCase):
    def test_each_rule(self):
        c = cfg()
        cases = [
            # (doc_type, location, debtor_channel, has_debtor) -> channel
            (("OS", "LOC-A", None, 0), "outlet"),
            (("IV", "LOC-E", None, 1), "marketplace"),
            (("CN", "LOC-E", None, 1), "marketplace"),
            (("IV", "LOC-A", "shopify", 1), "shopify"),
            (("IV", "LOC-E", "shopify", 1), "shopify"),  # the debtor rule comes first
            (("CN", "LOC-HQ", "shopify", 1), "shopify"),
            (("IV", "LOC-W", None, 1), "wholesale"),
            (("DO", "LOC-HQ", None, 1), "wholesale"),
            (("IV", "LOC-A", None, 1), "wholesale"),  # LOC-A maps only OS
            (("OS", "LOC-E", None, 0), "other"),  # no OS rule at LOC-E
            (("IV", "LOC-W", None, 0), "other"),  # blank debtor
        ]
        for args, expected in cases:
            self.assertEqual(an.resolve_channel(*args, c), expected, args)

    def test_null_default_channel_sends_unmatched_debtors_to_other(self):
        c = cfg(debtor_default_channel=None)
        self.assertEqual(an.resolve_channel("IV", "LOC-W", None, 1, c), "other")

    def test_sold_returned_and_returns(self):
        day = date(2026, 9, 1)
        rows = [
            arow("FAKE-1", "LOC-A", day, "OS", -5, qty_in=0, rows=5),
            # A positive POS row is a return.
            arow("FAKE-1", "LOC-A", day, "OS", 1, qty_in=1),
            arow("FAKE-1", "LOC-E", day, "IV", -3, has_debtor=1),
            arow("FAKE-1", "LOC-E", day, "CN", 1, has_debtor=1),
            arow("FAKE-2", "LOC-A", day, "IV", -2, debtor_channel="shopify", has_debtor=1),
        ]
        sales, received, reconcile, counts = sales_of(rows)
        self.assertEqual(sales[("FAKE-1", "LOC-A", day, "outlet")], [Decimal(4), Decimal(1)])
        self.assertEqual(sales[("FAKE-1", "LOC-E", day, "marketplace")], [Decimal(2), Decimal(1)])
        self.assertEqual(sales[("FAKE-2", "LOC-A", day, "shopify")], [Decimal(2), Decimal(0)])
        self.assertEqual(counts["sold_units_by_channel"],
                         {"outlet": 4, "marketplace": 2, "shopify": 2, "wholesale": 0, "other": 0})
        self.assertEqual(counts["returned_units_by_channel"]["outlet"], 1)
        self.assertEqual(received, {})
        self.assertEqual(reconcile[("OS", "LOC-A", "2026-09")], [Decimal(4), Decimal(1)])
        self.assertEqual(reconcile[("CN", "LOC-E", "2026-09")], [Decimal(-1), Decimal(1)])

    def test_do_counts_once(self):
        # A DO that is later invoiced carries the stock row; the IV posts none (VM r1 step 2), so
        # only the DO row is in the ledger read and the units count once.
        rows = [arow("FAKE-1", "LOC-W", date(2026, 9, 2), "DO", -3, has_debtor=1)]
        sales, _received, _reconcile, counts = sales_of(rows)
        self.assertEqual(list(sales.values()), [[Decimal(3), Decimal(0)]])
        self.assertEqual(counts["sold_units_by_channel"]["wholesale"], 3)

    def test_unknown_goes_to_other_and_is_counted(self):
        rows = [arow("FAKE-1", "LOC-E", date(2026, 9, 3), "OS", -2), arow("FAKE-1", "LOC-X", date(2026, 9, 3), "IV", -1)]
        sales, _received, _reconcile, counts = sales_of(rows)
        self.assertEqual(counts["other_groups"], 2)
        self.assertEqual(counts["sold_units_by_channel"]["other"], 3)
        self.assertEqual({k[3] for k in sales}, {"other"})

    def test_receipts_internal_types_non_stock_and_window(self):
        rows = [
            arow("FAKE-1", "LOC-A", date(2026, 8, 10), "GR", 10, rows=2),
            arow("FAKE-1", "LOC-A", date(2026, 8, 11), "ST", -4),  # not configured: never a sale
            arow("FAKE-FEE", "LOC-E", date(2026, 8, 12), "IV", -1),
            arow("FAKE-1", "LOC-A", date(2026, 11, 1), "OS", -1),  # after last_day
        ]
        sales, received, _reconcile, counts = an.build_sales(
            rows, cfg(), lambda code: code == "FAKE-FEE", date(2026, 7, 1), LAST_DAY)
        self.assertEqual(sales, {})
        self.assertEqual(received, {("FAKE-1", "LOC-A", "2026-08"): Decimal(10)})
        self.assertEqual((counts["unclassified_doc_type_groups"], counts["non_stock_rows_excluded"],
                          counts["out_of_window_groups"], counts["receipt_units"]), (1, 1, 1, 10))

    def test_rules_come_from_settings(self):
        c = cfg(channel_by_location={"LOC-Z": {"OS": "marketplace"}}, channel_by_debtor={})
        self.assertEqual(an.resolve_channel("OS", "LOC-Z", None, 0, c), "marketplace")
        self.assertEqual(an.resolve_channel("OS", "LOC-A", None, 0, c), "other")


class SettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_valid(self):
        c = an.validate_analysis(None)
        self.assertFalse(c["enabled"])
        self.assertEqual(c["highlights"]["sold_out_days"], 28)
        self.assertEqual(c["placement"]["min_hold"], 2)

    def test_nested_blocks_merge_with_defaults(self):
        c = cfg(highlights={"top_n": 5})
        self.assertEqual((c["highlights"]["top_n"], c["highlights"]["slow_days"]), (5, 60))

    def test_bad_values_fail_without_echoing_codes(self):
        bad = [
            ({"channel_by_debtor": {FAKE_DEBTOR: "retail"}}, "channel_by_debtor"),
            ({"channel_by_location": {"LOC-A": {"GR": "outlet"}}}, "channel_by_location"),
            ({"doc_types": {"sales": ["OS"], "returns": ["OS"], "receipts": []}}, "listed twice"),
            ({"pools": {}}, "analysis.pools is required"),
            ({"highlights": {"pool": "nope"}}, "highlights.pool"),
            ({"placement": {"pairs": [{"id": "p", "label": "x", "zero_locations": ["LOC-A"], "holder_locations": ["LOC-A"]}]}},
             "both sides"),
            ({"stock_take_day": "2026-07-01"}, "stock_take_day"),
            ({"attributes": {"gender_map": {"M": "X"}}}, "gender_map"),
            ({"unknown": 1}, "unknown"),
            ({"highlights": {"unknown": 1}}, "unknown"),
        ]
        for overrides, fragment in bad:
            with self.assertRaises(an.AnalysisSettingsError) as caught:
                cfg(**overrides)
            self.assertIn(fragment, str(caught.exception))
            self.assertNotIn(FAKE_DEBTOR, str(caught.exception))
            self.assertNotIn("LOC-A", str(caught.exception))


# ---------------------------------------------------------------------------------------------
# AC5: table-driven size and gender normalisation
# ---------------------------------------------------------------------------------------------


class SizeNormalisationTests(unittest.TestCase):
    def setUp(self):
        self.attrs = cfg()["attributes"]

    def size(self, raw, category="FAKE-JACKET"):
        return an.normalise_size(raw, category, self.attrs)

    def test_letters_and_aliases(self):
        for raw, expected in (("M", "M"), (" xl ", "XL"), ("2XL", "XXL"), ("3xl", "XXXL"), ("XXS", "XXS")):
            out = self.size(raw)
            self.assertEqual((out["size"], out["size_class"]), (expected, "letter"), raw)
        orders = [self.size(s)["size_order"] for s in ("XS", "S", "M", "L", "XL", "XXL")]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(self.size(" xl ")["size_raw"], "xl")

    def test_one_size(self):
        for raw in ("O.S", "OS", "F", "FREE", "o/s", "One Size"):
            out = self.size(raw)
            self.assertEqual((out["size"], out["size_class"]), ("OS", "one_size"), raw)

    def test_default_and_blank_are_not_sized(self):
        for raw in ("DEFAULT", "default", "", None, "   "):
            out = self.size(raw)
            self.assertEqual((out["size"], out["size_class"], out["size_order"]), (None, None, None), raw)

    def test_waist_length(self):
        out = self.size("XL-S", "FAKE-PANTS")
        self.assertEqual((out["size"], out["length"], out["size_class"]), ("XL", "S", "waist"))
        self.assertEqual(out["size_order"], self.size("XL")["size_order"])
        numeric = self.size("32-30", "FAKE-PANTS")
        self.assertEqual((numeric["size"], numeric["length"]), ("32", "30"))

    def test_pair_outside_a_waist_category_is_a_range(self):
        out = self.size("S-M", "FAKE-GLOVES")
        self.assertEqual((out["size"], out["length"], out["size_class"]), ("S-M", None, "range"))

    def test_numeric_takes_its_class_from_the_category(self):
        self.assertEqual(self.size("26.5", "FAKE-FOOTWEAR")["size_class"], "footwear")
        self.assertEqual(self.size("26.50", "FAKE-FOOTWEAR")["size"], "26.5")
        self.assertEqual(self.size("140", "FAKE-KIDS")["size_class"], "kids")
        self.assertEqual(self.size("42", "FAKE-OTHER")["size_class"], "numeric")
        self.assertLess(self.size("26", "FAKE-FOOTWEAR")["size_order"], self.size("26.5", "FAKE-FOOTWEAR")["size_order"])

    def test_unrecognised_is_kept_and_classed_other(self):
        out = self.size("FAKE SIZE")
        self.assertEqual((out["size"], out["size_class"], out["size_order"]), ("FAKE SIZE", "other", an.ORDER_OTHER))

    def test_map_is_extendable_from_settings(self):
        attrs = cfg(attributes={"size_map": {"MEDIUM": "M"}})["attributes"]
        self.assertEqual(an.normalise_size("medium", "", attrs)["size"], "M")

    def test_gender(self):
        for raw, expected in (("M", "M"), ("MEN", "M"), ("F", "W"), ("women", "W"), ("BOTH", "U"), ("", ""), ("FAKE", "")):
            self.assertEqual(an.normalise_gender(raw, "FAKE-JACKET", self.attrs), expected, raw)
        self.assertEqual(an.normalise_gender("M", "FAKE-KIDS", self.attrs), "K", "kids categories are K")

    def test_item_attributes_and_coverage(self):
        items = {
            "FAKE-1": item("FAKE-1", size="L", colour="DEFAULT"),
            "FAKE-2": item("FAKE-2", size="DEFAULT"),
            "FAKE-3": item("FAKE-3", brand="FAKE-OTHER", size="FAKE SIZE", gender="FAKE"),
        }
        attrs = {code: an.item_attributes(row, self.attrs) for code, row in items.items()}
        self.assertEqual(attrs["FAKE-1"]["colour"], "", "DEFAULT colour is blank")
        self.assertEqual(attrs["FAKE-1"]["style_code"], "FAKE-STY")
        self.assertEqual(attrs["FAKE-1"]["style_name"], "Fake style")
        sales = {("FAKE-1", "LOC-A", date(2026, 9, 1), "outlet"): [Decimal(3), Decimal(0)],
                 ("FAKE-2", "LOC-A", date(2026, 9, 1), "outlet"): [Decimal(1), Decimal(0)]}
        coverage = an.size_coverage(attrs, items, sales)
        self.assertEqual(coverage["FAKE-BRAND"],
                         {"items": 2, "items_sized": 1, "items_size_other": 0, "sold_units": 4, "sold_units_sized": 3})
        counts = an.attribute_counts(attrs, {c: r["StockGender"] for c, r in items.items()})
        self.assertEqual(counts["unrecognised_size_values"], [["FAKE SIZE", 1]])
        self.assertEqual(counts["items_unknown_gender_value"], 1)

    def test_coverage_never_counts_class_other_as_sized(self):
        items = {
            "FAKE-1": item("FAKE-1", size="M"),
            "FAKE-2": item("FAKE-2", size="FAKE SIZE"),
            "FAKE-3": item("FAKE-3", size="ZZ9"),
        }
        attrs = {code: an.item_attributes(row, self.attrs) for code, row in items.items()}
        self.assertEqual([attrs[c]["size_class"] for c in sorted(attrs)], ["letter", "other", "other"])
        sales = {(c, "LOC-A", date(2026, 9, 1), "outlet"): [Decimal(2), Decimal(0)] for c in items}
        coverage = an.size_coverage(attrs, items, sales)["FAKE-BRAND"]
        self.assertEqual(coverage, {"items": 3, "items_sized": 1, "items_size_other": 2, "sold_units": 6, "sold_units_sized": 2})
        counts = an.attribute_counts(attrs, {c: r["StockGender"] for c, r in items.items()})
        self.assertEqual((counts["items_sized"], counts["unrecognised_size_items"]), (1, 2))
        self.assertFalse(an.is_sized(attrs["FAKE-2"]))

    # G2 L4 size-map gaps, on fake values of the same shapes.

    def test_kids_prefix_and_cm_heights(self):
        out = self.size("j8")
        self.assertEqual((out["size"], out["size_class"]), ("J8", "kids"))
        self.assertLess(self.size("J6")["size_order"], self.size("J10")["size_order"])
        self.assertEqual((self.size("140CM", "FAKE-KIDS")["size"], self.size("140CM", "FAKE-KIDS")["size_class"]), ("140CM", "kids"))
        self.assertEqual(self.size("140 cm", "FAKE-OTHER")["size_class"], "numeric")
        self.assertEqual(self.size("140", "FAKE-KIDS")["size_class"], "kids")

    def test_volumes(self):
        for raw, expected in (("20L", "20L"), ("0.5L", "0.5L"), ("21OZ", "21OZ"), ("500 ml", "500ML")):
            out = self.size(raw, "FAKE-BOTTLE")
            self.assertEqual((out["size"], out["size_class"]), (expected, "volume"), raw)
        self.assertLess(self.size("0.5L")["size_order"], self.size("20L")["size_order"])

    def test_packs_are_not_sized_and_keep_their_colour(self):
        for raw in ("PKG", "box", "PEG", "Pack"):
            self.assertIsNone(self.size(raw)["size"], raw)
        row = item("FAKE-1", size="PKG", colour="FAKE-RED")
        attrs = an.item_attributes(row, self.attrs)
        self.assertEqual((attrs["size"], attrs["colour"]), (None, "FAKE-RED"))

    def test_us_shoe_sizes_and_cm_split_in_footwear(self):
        out = self.size("US9.5", "FAKE-FOOTWEAR")
        self.assertEqual((out["size"], out["size_class"]), ("US9.5", "footwear_us"))
        self.assertEqual(self.size("US 8")["size"], "US8")
        self.assertEqual(self.size("7", "FAKE-FOOTWEAR")["size_class"], "footwear_us")
        self.assertEqual(self.size("23", "FAKE-FOOTWEAR")["size_class"], "footwear")
        off = cfg(attributes={"footwear_cm_min": 0, "size_class_by_category": {"FAKE-FOOTWEAR": "footwear"}})["attributes"]
        self.assertEqual(an.normalise_size("7", "FAKE-FOOTWEAR", off)["size_class"], "footwear")

    def test_combined_letter_sizes(self):
        for raw in ("S/M", "m/l"):
            self.assertEqual(self.size(raw)["size_class"], "letter", raw)
        orders = [self.size(s)["size_order"] for s in ("S", "S/M", "M", "M/L", "L")]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(set(orders)), 5)

    def test_length_code_after_a_dash(self):
        out = self.size("32-R", "FAKE-PANTS")
        self.assertEqual((out["size"], out["length"], out["size_class"]), ("32", "R", "waist"))
        out = self.size("M-R", "FAKE-JACKET")
        self.assertEqual((out["size"], out["length"], out["size_class"]), ("M", "R", "letter"))
        self.assertEqual(self.size("M-ZZ", "FAKE-JACKET")["size_class"], "other")

    def test_size_map_target_is_parsed_as_a_pair(self):
        attrs = cfg(attributes={"size_map": {"XL/S": "XL-S"},
                                "size_class_by_category": {"FAKE-PANTS": "waist"}})["attributes"]
        out = an.normalise_size("XL/S", "FAKE-PANTS", attrs)
        self.assertEqual((out["size"], out["length"], out["size_class"], out["size_raw"]), ("XL", "S", "waist", "XL/S"))

    def test_new_size_settings_are_checked(self):
        for bad in ({"size_prefix_class": {"J1": "kids"}}, {"size_prefix_class": {"J": "other"}},
                    {"size_unit_class": {"L": "fake"}}, {"footwear_cm_min": 101}, {"length_codes": "R"},
                    {"size_blank_values": [""]}):
            with self.assertRaises(an.AnalysisSettingsError, msg=str(bad)):
                cfg(attributes=bad)


# ---------------------------------------------------------------------------------------------
# Stock state and placement (AC4 is the VM proof; these pin the rules on fake rows)
# ---------------------------------------------------------------------------------------------


class StockStateTests(unittest.TestCase):
    def test_intervals_first_stock_negative_and_open(self):
        moves = mv([
            ("FAKE-1", "LOC-A", date(2026, 7, 20), -1),  # negative before first stock: ignored
            ("FAKE-1", "LOC-A", date(2026, 7, 31), 3),   # close 2 -> first stock
            ("FAKE-1", "LOC-A", date(2026, 8, 5), -2),   # close 0 -> zero from 5 Aug
            ("FAKE-1", "LOC-A", date(2026, 8, 9), 1),    # close 1 -> back on 9 Aug
            ("FAKE-1", "LOC-A", date(2026, 10, 20), -2),  # close -1 -> zero, still open
            ("FAKE-1", "LOC-A", date(2026, 11, 5), 9),   # future-dated: never read
        ])
        state = an.walk_groups(moves, LAST_DAY, {"outlets": ["LOC-A"]}, snapshot_day=GOLIVE)
        st = state[("FAKE-1", "outlets")]
        self.assertEqual(st["first_stock_day"], date(2026, 7, 31))
        self.assertEqual(st["intervals"], [[date(2026, 8, 5), date(2026, 8, 8)], [date(2026, 10, 20), LAST_DAY]])
        self.assertEqual((st["qty_now"], st["at_snapshot"]), (Decimal(-1), Decimal(2)))

    def test_pool_sums_locations_and_ignores_others(self):
        moves = mv([
            ("FAKE-1", "LOC-A", date(2026, 8, 1), 1),
            ("FAKE-1", "LOC-E", date(2026, 8, 1), 1),
            ("FAKE-1", "LOC-A", date(2026, 8, 3), -1),  # pool still 1
            ("FAKE-1", "LOC-GIT", date(2026, 8, 3), 50),  # in no pool
            ("FAKE-1", "LOC-E", date(2026, 8, 6), -1),  # pool 0
        ])
        state = an.walk_groups(moves, LAST_DAY, {"retail": ["LOC-A", "LOC-E"]})
        self.assertEqual(state[("FAKE-1", "retail")]["intervals"], [[date(2026, 8, 6), LAST_DAY]])
        self.assertNotIn(("FAKE-1", "LOC-GIT"), state)

    def test_net_zero_day_does_not_open_an_interval(self):
        moves = mv([("FAKE-1", "LOC-A", date(2026, 8, 1), 2), ("FAKE-1", "LOC-A", date(2026, 8, 2), 0)])
        state = an.walk_groups(moves, LAST_DAY, {"outlets": ["LOC-A"]})
        self.assertEqual(state[("FAKE-1", "outlets")]["intervals"], [])

    def test_payload_offsets_and_received(self):
        moves = mv([("FAKE-1", "LOC-A", date(2026, 7, 31), 2), ("FAKE-1", "LOC-A", date(2026, 8, 2), -2)])
        state = an.walk_groups(moves, LAST_DAY, {"outlets": ["LOC-A"]}, snapshot_day=GOLIVE)
        received = an.received_by_pool({("FAKE-1", "LOC-A", "2026-08"): Decimal(4), ("FAKE-2", "LOC-W", "2026-08"): Decimal(1)},
                                       {"outlets": ["LOC-A"]})
        header = {"built_from_close_day": LAST_DAY.isoformat()}
        payload = an.stock_state_payload(state, received, {"outlets": ["LOC-A"]}, header, date(2026, 7, 31), LAST_DAY)
        self.assertEqual(payload["items"], ["FAKE-1"])
        self.assertEqual(payload["rows"], [[0, 0, 0, [[2, 92]], 0, 2, {"2026-08": 4}]])
        self.assertEqual(payload["counts"], {"rows": 1, "items": 1, "zero_now": 1})


class PlacementTests(unittest.TestCase):
    def test_open_now_counts_and_demand(self):
        moves = mv([
            ("FAKE-1", "LOC-A", date(2026, 7, 31), 5),
            ("FAKE-1", "LOC-E", date(2026, 8, 1), 1),
            ("FAKE-1", "LOC-E", date(2026, 8, 5), -1),  # LOC-E zero from 5 Aug while LOC-A holds 5
            # Never at LOC-W: the wholesale pair has no holder stock.
        ])
        sales = {("FAKE-1", "LOC-E", date(2026, 8, 5), "marketplace"): [Decimal(1), Decimal(0)],
                 ("FAKE-1", "LOC-E", date(2026, 7, 1), "marketplace"): [Decimal(9), Decimal(0)]}
        rows, first_day = an.build_placement(moves, cfg(), sales, LAST_DAY, date(2026, 7, 31))
        self.assertEqual(first_day, LAST_DAY - timedelta(days=89))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["item"], row["pair"], row["zero_location"]), ("FAKE-1", "online_out_shop_has", "LOC-E"))
        self.assertEqual((row["zero_days_90"], row["zero_days_30"], row["open_now"]), (88, 30, True))
        self.assertEqual((row["holder_qty_now"], row["zero_qty_now"], row["sold_at_zero_loc_90"]), (5, 0, 1))

    def test_holder_below_min_hold_and_never_stocked_zero_location(self):
        moves = mv([
            ("FAKE-1", "LOC-A", date(2026, 8, 1), 1),  # holder 1 < min_hold 2
            ("FAKE-1", "LOC-E", date(2026, 8, 1), 1),
            ("FAKE-1", "LOC-E", date(2026, 8, 2), -1),
            ("FAKE-2", "LOC-A", date(2026, 8, 1), 9),  # LOC-E never held FAKE-2: not a placement issue
        ])
        rows, _ = an.build_placement(moves, cfg(), {}, LAST_DAY, date(2026, 8, 1))
        self.assertEqual(rows, [])

    def test_closed_issue_is_listed_but_not_open(self):
        moves = mv([
            ("FAKE-1", "LOC-W", date(2026, 8, 1), 4),
            ("FAKE-1", "LOC-A", date(2026, 8, 1), 1),
            ("FAKE-1", "LOC-A", date(2026, 10, 1), -1),
            ("FAKE-1", "LOC-A", date(2026, 10, 4), 1),
        ])
        rows, _ = an.build_placement(moves, cfg(), {}, LAST_DAY, date(2026, 8, 1))
        self.assertEqual([(r["pair"], r["zero_location"], r["zero_days_90"], r["zero_days_30"], r["open_now"]) for r in rows],
                         [("held_at_wholesale", "LOC-A", 3, 2, False)], "2 and 3 Oct fall in the last 30 days")


# ---------------------------------------------------------------------------------------------
# AC9: highlights rules, each line with its rule id and inputs
# ---------------------------------------------------------------------------------------------


class HighlightsTests(unittest.TestCase):
    def build(self, entries, rows, items, config=None, placement_rows=()):
        config = config or cfg()
        moves = mv(entries)
        sales, received, _rec, _counts = sales_of(rows, config)
        state = an.walk_groups(moves, LAST_DAY, config["pools"], snapshot_day=GOLIVE)
        rec_pool = an.received_by_pool(received, config["pools"])
        attrs = {code: an.item_attributes(row, config["attributes"]) for code, row in items.items()}
        return an.build_highlights(config, attrs, items, state, rec_pool, sales, list(placement_rows), LAST_DAY)

    def by_rule(self, lines):
        out = {}
        for line in lines:
            out.setdefault(line["rule"], []).append(line)
        return out

    def test_sold_out_early_by_days_and_by_sell_through(self):
        items = {c: item(c) for c in ("FAKE-1", "FAKE-2", "FAKE-3")}
        entries = [
            ("FAKE-1", "LOC-A", date(2026, 8, 1), 10), ("FAKE-1", "LOC-A", date(2026, 8, 10), -10),
            ("FAKE-2", "LOC-A", GOLIVE, 10), ("FAKE-2", "LOC-A", date(2026, 9, 30), -2),
            ("FAKE-3", "LOC-A", GOLIVE, 5), ("FAKE-3", "LOC-A", date(2026, 9, 30), -4),
        ]
        rows = [
            arow("FAKE-1", "LOC-A", date(2026, 8, 1), "GR", 10),
            arow("FAKE-1", "LOC-A", date(2026, 8, 10), "OS", -10),
            arow("FAKE-2", "LOC-A", date(2026, 9, 30), "OS", -2),
            arow("FAKE-3", "LOC-A", date(2026, 9, 30), "OS", -4),
        ]
        lines, counts = self.build(entries, rows, items)
        early = {l["item"]: l for l in self.by_rule(lines)["sold_out_early"]}
        self.assertEqual(sorted(early), ["FAKE-1", "FAKE-3"])
        self.assertEqual(early["FAKE-1"]["inputs"]["days_to_zero"], 9)
        self.assertEqual(early["FAKE-1"]["inputs"]["stocked"], 10)
        self.assertEqual(early["FAKE-3"]["inputs"]["sell_through_pct"], 80.0)
        self.assertIsNone(early["FAKE-3"]["inputs"]["days_to_zero"])
        self.assertEqual(early["FAKE-1"]["brand"], "FAKE-BRAND")
        self.assertEqual(counts["sold_out_early"], {"lines_before_cap": 2, "brands_truncated": 0})

    def test_slow_seller(self):
        items = {c: item(c) for c in ("FAKE-1", "FAKE-2", "FAKE-3")}
        entries = [
            ("FAKE-1", "LOC-A", GOLIVE, 6),                                        # no sale ever
            ("FAKE-2", "LOC-A", GOLIVE, 6), ("FAKE-2", "LOC-A", date(2026, 10, 20), -1),  # recent sale
            ("FAKE-3", "LOC-A", date(2026, 10, 1), 6),                             # stocked too recently
        ]
        rows = [arow("FAKE-2", "LOC-A", date(2026, 10, 20), "OS", -1)]
        lines, _ = self.build(entries, rows, items)
        slow = self.by_rule(lines)["slow_seller"]
        self.assertEqual([(l["item"], l["inputs"]["qty_now"], l["inputs"]["last_sale_day"]) for l in slow], [("FAKE-1", 6, None)])

    def test_size_drift(self):
        items = {"FAKE-S": item("FAKE-S", size="S"), "FAKE-M": item("FAKE-M", size="M"), "FAKE-L": item("FAKE-L", size="L"),
                 "FAKE-N": item("FAKE-N", size="DEFAULT")}
        entries = [(c, "LOC-A", GOLIVE, 10) for c in items]
        sold = {"FAKE-S": 2, "FAKE-M": 3, "FAKE-L": 5, "FAKE-N": 50}
        rows = [arow(c, "LOC-A", date(2026, 10, 15), "OS", -q) for c, q in sold.items()]
        # A sale before the stock-take day is outside the drift window.
        rows.append(arow("FAKE-S", "LOC-A", date(2026, 9, 1), "OS", -40))
        lines, _ = self.build(entries, rows, items)
        drift = self.by_rule(lines)["size_drift"]
        got = {l["inputs"]["size"]: l["inputs"] for l in drift}
        self.assertEqual(sorted(got), ["L", "S"], "M differs by 3.3 pts, under drift_pts 5; unsized items are left out")
        self.assertEqual((got["L"]["sold_share_pct"], got["L"]["stocked_share_pct"], got["L"]["diff_pts"]), (50.0, 33.3, 16.7))
        self.assertEqual((got["S"]["n_sold"], got["S"]["window_from"]), (10, "2026-10-10"))
        self.assertTrue(all(l["item"] is None for l in drift))

    def test_size_drift_needs_minimum_units(self):
        items = {"FAKE-S": item("FAKE-S", size="S"), "FAKE-L": item("FAKE-L", size="L")}
        entries = [(c, "LOC-A", GOLIVE, 10) for c in items]
        rows = [arow("FAKE-L", "LOC-A", date(2026, 10, 15), "OS", -9)]
        lines, _ = self.build(entries, rows, items)
        self.assertNotIn("size_drift", self.by_rule(lines))

    def test_open_placement_and_cap(self):
        items = {"FAKE-%d" % n: item("FAKE-%d" % n) for n in range(4)}
        placement = [
            {"item": "FAKE-%d" % n, "pair": "online_out_shop_has", "zero_location": "LOC-E", "zero_qty_now": Decimal(0),
             "holder_qty_now": Decimal(5), "zero_days_30": n, "zero_days_90": n, "open_now": n != 0,
             "sold_at_zero_loc_90": Decimal(1)}
            for n in range(4)
        ]
        config = cfg(highlights={"top_n": 2})
        lines, counts = self.build([], [], items, config, placement)
        rows = self.by_rule(lines)["open_placement"]
        self.assertEqual([l["item"] for l in rows], ["FAKE-3", "FAKE-2"], "open only, most zero days first, capped")
        self.assertEqual(rows[0]["inputs"]["pair"], "online_out_shop_has")
        self.assertEqual(counts["open_placement"], {"lines_before_cap": 3, "brands_truncated": 1, "open_below_min_sold": 0})

    def placement_row(self, code, zero_days, sold):
        return {"item": code, "pair": "online_out_shop_has", "zero_location": "LOC-E", "zero_qty_now": Decimal(0),
                "holder_qty_now": Decimal(5), "zero_days_30": zero_days, "zero_days_90": zero_days, "open_now": True,
                "sold_at_zero_loc_90": Decimal(sold)}

    def test_no_demand_row_cannot_crowd_out_a_demand_row_under_the_cap(self):
        """G2 L5: the no-demand row has far more zero days, yet the demand row wins the one slot."""
        items = {"FAKE-NODEMAND": item("FAKE-NODEMAND"), "FAKE-DEMAND": item("FAKE-DEMAND")}
        placement = [self.placement_row("FAKE-NODEMAND", 30, 0), self.placement_row("FAKE-DEMAND", 2, 1)]
        for min_sold in (0, 1):
            config = cfg(highlights={"top_n": 1, "placement_min_sold": min_sold})
            lines, counts = self.build([], [], items, config, placement)
            rows = self.by_rule(lines)["open_placement"]
            self.assertEqual([l["item"] for l in rows], ["FAKE-DEMAND"], "min_sold %d" % min_sold)
            self.assertEqual(rows[0]["inputs"]["sold_at_zero_loc_90"], 1)
            self.assertEqual(counts["open_placement"]["open_below_min_sold"], min_sold)

    def test_default_filters_no_demand_rows_and_zero_lists_them_last(self):
        self.assertEqual(an.DEFAULT_ANALYSIS["highlights"]["placement_min_sold"], 1)
        items = {c: item(c) for c in ("FAKE-1", "FAKE-2", "FAKE-3")}
        placement = [self.placement_row("FAKE-1", 30, 0), self.placement_row("FAKE-2", 5, 3),
                     self.placement_row("FAKE-3", 9, 1)]
        lines, counts = self.build([], [], items, cfg(), placement)
        self.assertEqual([l["item"] for l in self.by_rule(lines)["open_placement"]], ["FAKE-3", "FAKE-2"],
                         "demand rows only, most zero days first")
        self.assertEqual(counts["open_placement"]["open_below_min_sold"], 1)
        lines, _ = self.build([], [], items, cfg(highlights={"placement_min_sold": 0}), placement)
        self.assertEqual([l["item"] for l in self.by_rule(lines)["open_placement"]], ["FAKE-3", "FAKE-2", "FAKE-1"])

    def test_placement_min_sold_is_checked(self):
        for bad in (-1, 1.5, True, 1000001):
            with self.assertRaises(an.AnalysisSettingsError):
                cfg(highlights={"placement_min_sold": bad})

    def test_every_line_carries_rule_and_inputs(self):
        items = {"FAKE-1": item("FAKE-1")}
        lines, _ = self.build([("FAKE-1", "LOC-A", GOLIVE, 6)], [], items)
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(set(line), {"rule", "brand", "item", "inputs"})
            self.assertIn(line["rule"], an.HIGHLIGHT_RULES)
            self.assertIsInstance(line["inputs"], dict)


# ---------------------------------------------------------------------------------------------
# The whole build: shapes, privacy, coverage
# ---------------------------------------------------------------------------------------------


class BuildAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.items = {
            "FAKE-1": item("FAKE-1", size="M"),
            "FAKE-2": item("FAKE-2", size="L", brand="FAKE-OTHER"),
            "FAKE-FEE": item("FAKE-FEE", stock="F", size="DEFAULT"),
        }
        self.moves = mv([
            ("FAKE-1", "LOC-A", GOLIVE, 4), ("FAKE-1", "LOC-A", date(2026, 9, 1), -4),
            ("FAKE-2", "LOC-E", GOLIVE, 3), ("FAKE-2", "LOC-E", date(2026, 9, 2), -1),
        ])
        self.rows = [
            arow("FAKE-1", "LOC-A", date(2026, 9, 1), "OS", -4, rows=4),
            arow("FAKE-2", "LOC-E", date(2026, 9, 2), "IV", -1, debtor_channel="shopify", has_debtor=1),
            arow("FAKE-FEE", "LOC-E", date(2026, 9, 2), "IV", -1, has_debtor=1),
        ]
        self.header = {"job": "fake_job", "run_id": "0" * 32, "as_of_local": "2026-11-01T00:00:06+08:00",
                       "timezone": "Asia/Singapore", "built_from_close_day": LAST_DAY.isoformat()}

    def build(self, config=None):
        return an.build_analysis(config or cfg(), self.rows, self.moves, self.items, lambda c: c == "FAKE-FEE",
                                 GOLIVE, LAST_DAY, self.header)

    def test_files_and_manifest_parts(self):
        payloads, parts = self.build()
        self.assertEqual(sorted(payloads), sorted(name for name, _ in an.DATA_FILES))
        for name, schema in an.DATA_FILES:
            self.assertEqual((payloads[name]["schema"], payloads[name]["schema_version"]), (schema, 1))
            self.assertEqual(payloads[name]["built_from_close_day"], "2026-10-31")
            self.assertIn("counts", payloads[name])
        sales = payloads[an.SALES_NAME]
        self.assertEqual(sales["day0"], "2026-07-31")
        self.assertEqual(sales["items"], ["FAKE-1", "FAKE-2"])
        self.assertEqual(sales["columns"]["day"], [32, 33])
        self.assertEqual([sales["channels"][c] for c in sales["columns"]["channel"]], ["outlet", "shopify"])
        self.assertEqual(sales["columns"]["sold"], [4, 1])
        self.assertEqual(sales["counts"]["non_stock_rows_excluded"], 1)
        self.assertEqual(parts["sold_units_by_channel"]["shopify"], 1)
        self.assertEqual(parts["other"], {"groups": 0, "sold_units": 0})
        self.assertEqual(set(parts["size_coverage"]), {"FAKE-BRAND", "FAKE-OTHER"}, "non-stock items are left out")
        self.assertEqual(parts["windows_hint"]["stock_take_day"], "2026-10-10")
        self.assertEqual(len(parts["settings_digest"]), 64)
        manifest = an.manifest_payload(dict(self.header, built_at="x"), [{"name": "f"}], parts)
        self.assertEqual((manifest["schema"], manifest["schema_version"]), (an.MANIFEST_SCHEMA, 1))

    def test_no_debtor_code_cost_or_document_data_in_any_output(self):
        payloads, parts = self.build(cfg(channel_by_debtor={FAKE_DEBTOR: "shopify", FAKE_DEBTOR_MKT: "marketplace"}))
        text = json.dumps([payloads, parts], default=str)
        for word in (FAKE_DEBTOR, FAKE_DEBTOR_MKT, "DEBTOR"):
            self.assertNotIn(word, text)
        lowered = text.lower()
        for word in ("cost", "price", "amount", "docno", "doc_no", "creditor", "customer", "member", "phone", "email"):
            self.assertNotIn(word, lowered, word)

    def test_digest_changes_with_settings_but_holds_no_values(self):
        _p1, a = self.build(cfg(channel_by_debtor={FAKE_DEBTOR: "shopify"}))
        _p2, b = self.build(cfg(channel_by_debtor={FAKE_DEBTOR_MKT: "shopify"}))
        self.assertNotEqual(a["settings_digest"], b["settings_digest"])

    def test_window_cap(self):
        config = cfg(sales_window_days=30)
        self.assertEqual(an.window_from(GOLIVE, LAST_DAY, config), date(2026, 10, 2))
        self.assertEqual(an.window_from(None, LAST_DAY, config), LAST_DAY)


if __name__ == "__main__":
    unittest.main()
