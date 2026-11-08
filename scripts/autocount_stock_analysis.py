"""Nightly buying-analysis files for the AC2 stock export (#292 slice S2).

Pure helpers used by scripts/autocount_stock_balance_hourly.py. Nothing here opens a database
connection, a file or a network socket: the exporter reads the rows, calls these builders and
publishes the bytes with its own verified-set writer.

Files (all units only, latest-only, gzip with a fixed header time, one verified set):

* analysis_sales_daily.json.gz   item x location x day x channel: sold (net of returns), returned
* analysis_stock_state.json.gz   item x pool: first stock day, zero-or-negative intervals, qty now,
                                 opening at go-live, received per month
* analysis_placement.json.gz     item x placement pair x zero location: zero days 30/90, open now
* analysis_highlights.json.gz    rule-based lines, each with its rule id and inputs
* analysis_manifest.json         run, close day, file hashes, counts, settings digest, coverage

Privacy contract: no cost, price or amount column, no document number, no debtor or creditor
code or name, no personal data. The debtor code is only compared inside SQL Server (a CASE over
parameter markers that returns a channel id), so it never reaches Python, a file or a log.
Channel, pool, placement and size rules all come from settings (analysis.*).
"""

import copy
import hashlib
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

SALES_NAME = "analysis_sales_daily.json.gz"
STATE_NAME = "analysis_stock_state.json.gz"
PLACEMENT_NAME = "analysis_placement.json.gz"
HIGHLIGHTS_NAME = "analysis_highlights.json.gz"
MANIFEST_NAME = "analysis_manifest.json"
SALES_SCHEMA = "xb.ac2.analysis_sales_daily"
STATE_SCHEMA = "xb.ac2.analysis_stock_state"
PLACEMENT_SCHEMA = "xb.ac2.analysis_placement"
HIGHLIGHTS_SCHEMA = "xb.ac2.analysis_highlights"
MANIFEST_SCHEMA = "xb.ac2.analysis_manifest"
SCHEMA_VERSION = 1
# Data files in publish order; the manifest is always published last.
DATA_FILES = (
    (SALES_NAME, SALES_SCHEMA),
    (STATE_NAME, STATE_SCHEMA),
    (PLACEMENT_NAME, PLACEMENT_SCHEMA),
    (HIGHLIGHTS_NAME, HIGHLIGHTS_SCHEMA),
)

# Same wording as the feed and the item index.
QUANTITY_RULE = "Units only; non-stock-controlled items excluded"

# Closed channel set: the Buying tab (S3) knows exactly these ids.
CHANNELS = ("outlet", "marketplace", "shopify", "wholesale", "other")
OTHER = "other"
# Sales header tables that carry the debtor, keyed by DocType. These identifiers are fixed here
# and never come from settings; settings only decide which of these DocTypes are read.
DEBTOR_HEADER_TABLES = {"CN": "CN", "DO": "DO", "IV": "IV"}

PLACEMENT_WINDOW_DAYS = 90
PLACEMENT_SHORT_DAYS = 30
MAX_SALES_WINDOW_DAYS = 400
UNRECOGNISED_LIST_CAP = 50

HIGHLIGHT_RULES = ("sold_out_early", "open_placement", "size_drift", "slow_seller")
SIZE_CLASSES = ("letter", "one_size", "numeric", "footwear", "footwear_us", "kids", "waist", "range", "volume", "other")
GENDERS = ("M", "W", "U", "K")

DOC_TYPE_RE = re.compile(r"\A[A-Z]{2,4}\Z")
ID_RE = re.compile(r"\A[a-z][a-z0-9_]{0,39}\Z")
DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
NUMERIC_SIZE_RE = re.compile(r"\A\d{1,3}(\.\d{1,2})?\Z")
PAIR_SIZE_RE = re.compile(r"\A([A-Z0-9.]{1,6})-([A-Z0-9.]{1,6})\Z")
# A letter prefix then a number (J8, US9.5) or a number then a unit (140CM, 0.5L, 21 OZ).
PREFIX_SIZE_RE = re.compile(r"\A([A-Z]{1,3}) ?(\d{1,3}(?:\.\d{1,2})?)\Z")
UNIT_SIZE_RE = re.compile(r"\A(\d{1,4}(?:\.\d{1,2})?) ?([A-Z]{1,3})\Z")
SIZE_TOKEN_RE = re.compile(r"\A[A-Z]{1,3}\Z")

# Sort keys: letters 100.., one size 200, numeric 1000 + 10 x value, unrecognised 9000.
ORDER_LETTER_BASE = 100
ORDER_ONE_SIZE = 200
ORDER_NUMERIC_BASE = 1000
ORDER_OTHER = 9000

DEFAULT_ANALYSIS = {
    "enabled": False,
    # Opening stock day (go-live) and the stock-take reset day used as the default window.
    "golive_day": "2026-07-31",
    "stock_take_day": None,
    "sales_window_days": MAX_SALES_WINDOW_DAYS,
    # AutoCount's standard DocType codes. Internal types (adjustments, transfers, issues,
    # receives) are simply not listed, so they are never read as sales.
    "doc_types": {"sales": ["OS", "IV", "DO"], "returns": ["CN"], "receipts": ["GR", "PI"]},
    # location -> {DocType -> channel}
    "channel_by_location": {},
    # debtor code -> channel. Used only as SQL parameters; never written anywhere.
    "channel_by_debtor": {},
    # A debtor DocType row whose debtor matched no rule above (null = "other").
    "debtor_default_channel": "wholesale",
    # pool name -> locations. Locations in no pool (transit, defective) are never counted.
    "pools": {},
    "placement": {"min_hold": 2, "pairs": []},
    "highlights": {
        "pool": "retail_network",
        "channels": ["outlet", "marketplace", "shopify"],
        "top_n": 20,
        # open_placement lines need at least this many units sold at the zero location in the
        # placement window (demand evidence). 0 lists every open row, demand rows still first.
        "placement_min_sold": 1,
        "sold_out_days": 28,
        "sell_through_pct": 80,
        "drift_pts": 3,
        "drift_min_units": 100,
        "slow_days": 60,
    },
    "attributes": {
        # Raw size (upper case, spaces collapsed) -> canonical size. Extend at VM proof.
        "size_map": {
            "2XL": "XXL", "3XL": "XXXL", "2XS": "XXS",
            "O.S": "OS", "O/S": "OS", "OS": "OS", "F": "OS", "FREE": "OS", "FREESIZE": "OS", "ONE SIZE": "OS",
        },
        # Combined sizes (S/M, M/L) sit between their neighbours.
        "letter_order": ["XXS", "XS", "S", "S/M", "M", "M/L", "L", "L/XL", "XL", "XXL", "XXXL"],
        # Values that mean "no value" for size and colour (blank always counts).
        "blank_values": ["DEFAULT"],
        # Values that mean "no size" for the size only (packaging such as packs); colour is kept.
        "size_blank_values": ["PACK", "PKG", "BOX", "PEG"],
        # Letter prefix + number -> size class (kids J8, US shoe US9.5).
        "size_prefix_class": {"J": "kids", "US": "footwear_us"},
        # Number + unit -> size class (heights 140CM, volumes 20L/0.5L/21OZ). "numeric" takes
        # the category's class, as plain numbers do.
        "size_unit_class": {"CM": "numeric", "L": "volume", "ML": "volume", "OZ": "volume"},
        # Non-size length codes allowed after a dash (32-R): size 32 with length R.
        "length_codes": ["R", "REG", "SH", "LG", "T"],
        # In a footwear category, plain numbers below this are US/UK sizes (class footwear_us);
        # from it up they are cm (class footwear). 0 turns the split off.
        "footwear_cm_min": 18,
        "gender_map": {
            "M": "M", "MEN": "M", "MALE": "M",
            "F": "W", "W": "W", "WOMEN": "W", "FEMALE": "W",
            "U": "U", "BOTH": "U", "UNISEX": "U",
        },
        "kids_categories": [],
        # category -> size class for numeric sizes and pairs ("waist" turns XL-S into size + length).
        "size_class_by_category": {},
    },
}
NESTED_CLOSED = ("doc_types", "placement", "highlights", "attributes")


class AnalysisSettingsError(ValueError):
    """Invalid analysis settings. Messages name the setting path only, never a value or key."""


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def to_dec(value):
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("a quantity was not numeric") from exc


def num(value):
    """A Decimal as a JSON number: whole numbers as int, others as float."""
    value = to_dec(value).normalize()
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def as_day(value):
    if hasattr(value, "date") and callable(value.date):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError("a row had no usable date")


def settings_digest(cfg):
    """sha256 of the analysis settings. The digest is published; the values never are."""
    raw = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _code(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 64 and not any(ord(c) < 32 for c in value)


def _codes(value, path, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty) or not all(_code(v) for v in value):
        raise AnalysisSettingsError("%s must be a %slist of codes" % (path, "" if allow_empty else "non-empty "))
    if len(set(value)) != len(value):
        raise AnalysisSettingsError("%s has duplicates" % path)


def _int(value, path, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AnalysisSettingsError("%s must be a whole number from %d to %d" % (path, low, high))


def _day(value, path, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not DAY_RE.match(value):
        raise AnalysisSettingsError("%s must be a date as YYYY-MM-DD" % path)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AnalysisSettingsError("%s must be a date as YYYY-MM-DD" % path) from exc


def _channel(value, path, allow_none=False):
    if value is None and allow_none:
        return
    if value not in CHANNELS:
        raise AnalysisSettingsError("%s must be one of the channel ids" % path)


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------


def validate_analysis(raw):
    """Return the complete analysis settings (defaults applied) or raise AnalysisSettingsError.

    Values that are set are always checked, enabled or not. Enabling also needs the highlights
    pool to exist. Messages name the setting path only: never a location, debtor or size value.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AnalysisSettingsError("analysis must be an object")
    unknown = set(raw) - set(DEFAULT_ANALYSIS)
    if unknown:
        raise AnalysisSettingsError("analysis has unknown setting(s)")
    cfg = copy.deepcopy(DEFAULT_ANALYSIS)
    for key, value in raw.items():
        if key in NESTED_CLOSED:
            if not isinstance(value, dict):
                raise AnalysisSettingsError("analysis.%s must be an object" % key)
            if set(value) - set(DEFAULT_ANALYSIS[key]):
                raise AnalysisSettingsError("analysis.%s has unknown setting(s)" % key)
            cfg[key].update(copy.deepcopy(value))
        else:
            cfg[key] = copy.deepcopy(value)

    if not isinstance(cfg["enabled"], bool):
        raise AnalysisSettingsError("analysis.enabled must be true or false")
    golive = _day(cfg["golive_day"], "analysis.golive_day")
    take = _day(cfg["stock_take_day"], "analysis.stock_take_day", allow_none=True)
    if take is not None and take < golive:
        raise AnalysisSettingsError("analysis.stock_take_day must not be before analysis.golive_day")
    _int(cfg["sales_window_days"], "analysis.sales_window_days", 1, MAX_SALES_WINDOW_DAYS)

    doc_types = cfg["doc_types"]
    seen = set()
    for group in ("sales", "returns", "receipts"):
        values = doc_types[group]
        if not isinstance(values, list) or not all(isinstance(v, str) and DOC_TYPE_RE.match(v) for v in values):
            raise AnalysisSettingsError("analysis.doc_types.%s must be a list of DocType codes" % group)
        if len(set(values)) != len(values) or seen & set(values):
            raise AnalysisSettingsError("analysis.doc_types: a DocType is listed twice")
        seen |= set(values)
    if not doc_types["sales"]:
        raise AnalysisSettingsError("analysis.doc_types.sales must not be empty")
    sale_types = set(doc_types["sales"]) | set(doc_types["returns"])

    by_loc = cfg["channel_by_location"]
    if not isinstance(by_loc, dict) or not all(_code(k) for k in by_loc):
        raise AnalysisSettingsError("analysis.channel_by_location must map location codes to objects")
    for rules in by_loc.values():
        if not isinstance(rules, dict) or not rules:
            raise AnalysisSettingsError("analysis.channel_by_location values must map DocTypes to channels")
        for doc_type, channel in rules.items():
            if doc_type not in sale_types:
                raise AnalysisSettingsError("analysis.channel_by_location uses a DocType that is not a sales or returns type")
            _channel(channel, "analysis.channel_by_location")
    by_debtor = cfg["channel_by_debtor"]
    if not isinstance(by_debtor, dict) or not all(_code(k) for k in by_debtor):
        raise AnalysisSettingsError("analysis.channel_by_debtor must map debtor codes to channels")
    for channel in by_debtor.values():
        _channel(channel, "analysis.channel_by_debtor")
    if by_debtor and not (sale_types & set(DEBTOR_HEADER_TABLES)):
        raise AnalysisSettingsError("analysis.channel_by_debtor needs a debtor DocType in analysis.doc_types")
    _channel(cfg["debtor_default_channel"], "analysis.debtor_default_channel", allow_none=True)

    pools = cfg["pools"]
    if not isinstance(pools, dict) or not all(isinstance(k, str) and ID_RE.match(k) for k in pools):
        raise AnalysisSettingsError("analysis.pools must map pool ids to location lists")
    for name in pools:
        _codes(pools[name], "analysis.pools.%s" % name)

    placement = cfg["placement"]
    _int(placement["min_hold"], "analysis.placement.min_hold", 1, 1000000)
    pairs = placement["pairs"]
    if not isinstance(pairs, list) or len(pairs) > 20:
        raise AnalysisSettingsError("analysis.placement.pairs must be a list of at most 20 pairs")
    pair_ids = set()
    for n, pair in enumerate(pairs):
        path = "analysis.placement.pairs[%d]" % n
        if not isinstance(pair, dict) or set(pair) != {"id", "label", "zero_locations", "holder_locations"}:
            raise AnalysisSettingsError("%s must have id, label, zero_locations and holder_locations" % path)
        if not isinstance(pair["id"], str) or not ID_RE.match(pair["id"]) or pair["id"] in pair_ids:
            raise AnalysisSettingsError("%s.id must be a unique lower-case id" % path)
        pair_ids.add(pair["id"])
        if not isinstance(pair["label"], str) or not 0 < len(pair["label"].strip()) <= 80:
            raise AnalysisSettingsError("%s.label must be a short label" % path)
        _codes(pair["zero_locations"], path + ".zero_locations")
        _codes(pair["holder_locations"], path + ".holder_locations")
        if set(pair["zero_locations"]) & set(pair["holder_locations"]):
            raise AnalysisSettingsError("%s: a location is on both sides" % path)

    hl = cfg["highlights"]
    if not isinstance(hl["pool"], str) or not ID_RE.match(hl["pool"]):
        raise AnalysisSettingsError("analysis.highlights.pool must be a pool id")
    if not isinstance(hl["channels"], list) or not hl["channels"] or len(set(hl["channels"])) != len(hl["channels"]):
        raise AnalysisSettingsError("analysis.highlights.channels must be a non-empty list of channel ids")
    for channel in hl["channels"]:
        _channel(channel, "analysis.highlights.channels")
    _int(hl["top_n"], "analysis.highlights.top_n", 1, 200)
    _int(hl["placement_min_sold"], "analysis.highlights.placement_min_sold", 0, 1000000)
    _int(hl["sold_out_days"], "analysis.highlights.sold_out_days", 1, 365)
    _int(hl["sell_through_pct"], "analysis.highlights.sell_through_pct", 1, 100)
    _int(hl["drift_pts"], "analysis.highlights.drift_pts", 1, 100)
    _int(hl["drift_min_units"], "analysis.highlights.drift_min_units", 1, 1000000)
    _int(hl["slow_days"], "analysis.highlights.slow_days", 1, 365)

    attrs = cfg["attributes"]
    size_map = attrs["size_map"]
    if not isinstance(size_map, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip() for k, v in size_map.items()
    ):
        raise AnalysisSettingsError("analysis.attributes.size_map must map sizes to sizes")
    _codes(attrs["letter_order"], "analysis.attributes.letter_order")
    _codes(attrs["blank_values"], "analysis.attributes.blank_values", allow_empty=True)
    _codes(attrs["size_blank_values"], "analysis.attributes.size_blank_values", allow_empty=True)
    _codes(attrs["length_codes"], "analysis.attributes.length_codes", allow_empty=True)
    for key in ("size_prefix_class", "size_unit_class"):
        rules = attrs[key]
        if not isinstance(rules, dict) or not all(
            isinstance(k, str) and SIZE_TOKEN_RE.match(k) and v in SIZE_CLASSES and v != "other" for k, v in rules.items()
        ):
            raise AnalysisSettingsError("analysis.attributes.%s must map 1-3 upper-case letters to size classes" % key)
    _int(attrs["footwear_cm_min"], "analysis.attributes.footwear_cm_min", 0, 100)
    gender_map = attrs["gender_map"]
    if not isinstance(gender_map, dict) or not all(isinstance(k, str) and k.strip() and v in GENDERS for k, v in gender_map.items()):
        raise AnalysisSettingsError("analysis.attributes.gender_map must map values to M, W, U or K")
    _codes(attrs["kids_categories"], "analysis.attributes.kids_categories", allow_empty=True)
    classes = attrs["size_class_by_category"]
    if not isinstance(classes, dict) or not all(_code(k) and v in SIZE_CLASSES for k, v in classes.items()):
        raise AnalysisSettingsError("analysis.attributes.size_class_by_category must map categories to size classes")

    if cfg["enabled"]:
        if not pools:
            raise AnalysisSettingsError("analysis.pools is required when analysis.enabled is true")
        if hl["pool"] not in pools:
            raise AnalysisSettingsError("analysis.highlights.pool must be one of analysis.pools")
    return cfg


def debtor_doc_types(cfg):
    """The configured sales/returns DocTypes that have a debtor header table, sorted."""
    types = set(cfg["doc_types"]["sales"]) | set(cfg["doc_types"]["returns"])
    return sorted(types & set(DEBTOR_HEADER_TABLES))


def read_doc_types(cfg):
    """Every DocType the analysis read selects, in a stable order."""
    d = cfg["doc_types"]
    return list(d["sales"]) + list(d["returns"]) + list(d["receipts"])


# ---------------------------------------------------------------------------------------------
# SQL: one grouped, SELECT-only read of the stock ledger with the debtor channel
# ---------------------------------------------------------------------------------------------


def sales_query(cfg):
    """The analysis read for these settings. Parameter markers only; no identifier from settings.

    The debtor code is compared inside SQL Server (CASE ... WHEN ? THEN ?) and only the matched
    channel id and a has-a-debtor flag come back, never the code. The row-level CASE sits in a
    derived table so the outer GROUP BY never repeats a parameter marker. No cost column, no
    DocNo, no free-text column. Window: DocDate >= ? AND DocDate < ? (next local midnight).
    """
    types = debtor_doc_types(cfg)
    joins = []
    debtor_terms = []
    for doc_type in types:
        alias = "h" + doc_type.lower()
        joins.append(
            "LEFT JOIN dbo.[%s] AS %s ON d.DocType = N'%s' AND %s.DocKey = d.DocKey"
            % (DEBTOR_HEADER_TABLES[doc_type], alias, doc_type, alias)
        )
        debtor_terms.append("%s.DebtorCode" % alias)
    if debtor_terms:
        debtor = debtor_terms[0] if len(debtor_terms) == 1 else "COALESCE(%s)" % ", ".join(debtor_terms)
        has_debtor = "CASE WHEN %s IS NOT NULL AND %s <> N'' THEN 1 ELSE 0 END" % (debtor, debtor)
        if cfg["channel_by_debtor"]:
            # The result marker is cast so SQL Server never has to infer its type.
            whens = " ".join("WHEN ? THEN CAST(? AS nvarchar(20))" for _ in cfg["channel_by_debtor"])
            debtor_channel = "CASE %s %s ELSE NULL END" % (debtor, whens)
        else:
            debtor_channel = "CAST(NULL AS nvarchar(20))"
    else:
        has_debtor = "0"
        debtor_channel = "CAST(NULL AS nvarchar(20))"
    markers = ", ".join("?" for _ in read_doc_types(cfg))
    return (
        "SELECT s.ItemCode, s.Location, s.DocDay, s.DocType, s.DebtorChannel, s.HasDebtor,\n"
        "    SUM(s.Qty) AS Qty,\n"
        "    SUM(CASE WHEN s.Qty > 0 THEN s.Qty ELSE 0 END) AS QtyIn,\n"
        "    COUNT_BIG(*) AS LedgerRows\n"
        "FROM (\n"
        "    SELECT d.ItemCode, d.Location, CAST(d.DocDate AS date) AS DocDay, d.DocType, d.Qty,\n"
        "        %s AS DebtorChannel,\n"
        "        %s AS HasDebtor\n"
        "    FROM dbo.StockDTL AS d\n"
        "%s"
        "    WHERE d.DocType IN (%s) AND d.DocDate >= ? AND d.DocDate < ?\n"
        ") AS s\n"
        "GROUP BY s.ItemCode, s.Location, s.DocDay, s.DocType, s.DebtorChannel, s.HasDebtor"
        % (debtor_channel, has_debtor, "".join("    " + j + "\n" for j in joins), markers)
    )


def sales_params(cfg, window_from, last_day):
    """Parameters in marker order: debtor/channel pairs, DocTypes, window start, next midnight."""
    params = []
    if debtor_doc_types(cfg) and cfg["channel_by_debtor"]:
        for code, channel in sorted(cfg["channel_by_debtor"].items()):
            params.extend([code, channel])
    params.extend(read_doc_types(cfg))
    params.extend([window_from, last_day + timedelta(days=1)])
    return params


def window_from(first_ledger_day, last_day, cfg):
    """First day of the sales window: the first ledger day, capped at sales_window_days."""
    cap = last_day - timedelta(days=cfg["sales_window_days"] - 1)
    if first_ledger_day is None:
        return last_day
    return max(first_ledger_day, cap)


# ---------------------------------------------------------------------------------------------
# Item attributes (table-driven; no description parsing)
# ---------------------------------------------------------------------------------------------

ATTRIBUTE_FIELDS = (
    "category", "size", "size_raw", "size_order", "size_class", "length", "gender", "colour", "style_code", "style_name",
)


def _upper(value):
    return " ".join(clean(value).upper().split())


def _number(text):
    """(canonical text, value, sort key) for a plain number."""
    value = Decimal(text).normalize()
    return format(value, "f"), value, ORDER_NUMERIC_BASE + int(value * 10)


def _numeric_class(value, cat_class, attrs):
    """The class of a plain number: the category's class, with footwear split into US and cm."""
    if not cat_class:
        return "numeric"
    if cat_class == "footwear" and attrs["footwear_cm_min"] and value < attrs["footwear_cm_min"]:
        return "footwear_us"
    return cat_class


def _size_part(token, attrs, cat_class=None):
    """(canonical, class, order) for one size token, or None when it is not a size."""
    token = attrs["size_map"].get(token, token)
    letters = attrs["letter_order"]
    if token == "OS":
        return "OS", "one_size", ORDER_ONE_SIZE
    if token in letters:
        return token, "letter", ORDER_LETTER_BASE + letters.index(token)
    if NUMERIC_SIZE_RE.match(token):
        text, value, order = _number(token)
        return text, _numeric_class(value, cat_class, attrs), order
    prefixed = PREFIX_SIZE_RE.match(token)
    if prefixed and prefixed.group(1) in attrs["size_prefix_class"]:
        text, _value, order = _number(prefixed.group(2))
        return prefixed.group(1) + text, attrs["size_prefix_class"][prefixed.group(1)], order
    unit = UNIT_SIZE_RE.match(token)
    if unit and unit.group(2) in attrs["size_unit_class"]:
        text, value, order = _number(unit.group(1))
        size_class = attrs["size_unit_class"][unit.group(2)]
        if size_class == "numeric":
            size_class = _numeric_class(value, cat_class, attrs)
        return text + unit.group(2), size_class, order
    return None


def normalise_size(raw, category, attrs):
    """{size, size_raw, size_order, size_class, length} for one item. Pure, table-driven.

    * blank, a blank_values entry or a size_blank_values entry (packs) -> no size ("not sized");
    * size_map first (2XL -> XXL, O.S -> OS, ...), then the letter order (with S/M, M/L), then
      numbers, prefixed numbers (J8 kids, US9 footwear_us) and numbers with a unit (140CM, 20L);
    * a pair "A-B" (also when it is a size_map target) in a category whose size class is
      "waist" -> size A, length B; in any other category it stays one size with class "range",
      ordered by A. A length code as B (32-R) always gives size A with length B;
    * numeric sizes take their class from size_class_by_category (footwear, kids, waist ...);
      in footwear, numbers below footwear_cm_min are US/UK sizes (footwear_us);
    * anything else is kept as given with class "other": counted for the size map, never
      counted as sized.
    """
    size_raw = clean(raw)
    token = _upper(raw)
    out = {"size": None, "size_raw": size_raw, "size_order": None, "size_class": None, "length": None}
    no_size = set(attrs["blank_values"]) | set(attrs["size_blank_values"])
    mapped = attrs["size_map"].get(token, token)
    if not token or token in no_size or mapped in no_size:
        return out
    cat_class = attrs["size_class_by_category"].get(clean(category))
    part = _size_part(mapped, attrs, cat_class)
    if part is not None:
        size, size_class, order = part
        out.update(size=size, size_class=size_class, size_order=order)
        return out
    pair = PAIR_SIZE_RE.match(mapped)
    if pair:
        first = _size_part(pair.group(1), attrs, cat_class)
        second = _size_part(pair.group(2), attrs, cat_class)
        if first is not None and second is None and pair.group(2) in attrs["length_codes"]:
            size_class = "waist" if cat_class == "waist" else first[1]
            out.update(size=first[0], size_class=size_class, size_order=first[2], length=pair.group(2))
            return out
        if first is not None and second is not None:
            if cat_class == "waist":
                out.update(size=first[0], size_class="waist", size_order=first[2], length=second[0])
            else:
                out.update(size="%s-%s" % (first[0], second[0]), size_class="range", size_order=first[2])
            return out
    out.update(size=token, size_class="other", size_order=ORDER_OTHER)
    return out


def is_sized(attr):
    """True when the item has a recognised size. Class "other" is not sized (it is listed instead)."""
    return attr["size"] is not None and attr["size_class"] != "other"


def normalise_gender(raw, category, attrs):
    """M, W, U, K or "" (unknown). Kids categories are always K."""
    if clean(category) in set(attrs["kids_categories"]):
        return "K"
    return attrs["gender_map"].get(_upper(raw), "")


def item_attributes(item, attrs):
    """The attribute fields for one item master row (from the attribute item master read)."""
    category = clean(item.get("ItemCategory"))
    size = normalise_size(item.get("StockSize"), category, attrs)
    colour = clean(item.get("StockColour"))
    if _upper(colour) in set(attrs["blank_values"]):
        colour = ""
    return {
        "category": category,
        "size": size["size"],
        "size_raw": size["size_raw"],
        "size_order": size["size_order"],
        "size_class": size["size_class"],
        "length": size["length"],
        "gender": normalise_gender(item.get("StockGender"), category, attrs),
        "colour": colour,
        "style_code": clean(item.get("ItemClass")),
        "style_name": clean(item.get("Desc2")),
    }


def brand_of(item):
    return clean(item.get("ItemBrand")) or clean(item.get("ItemType"))


# ---------------------------------------------------------------------------------------------
# Channels and the sales file
# ---------------------------------------------------------------------------------------------


def resolve_channel(doc_type, location, debtor_channel, has_debtor, cfg):
    """The channel of one grouped ledger row. Rules, in order:

    1. a debtor DocType row whose debtor matched channel_by_debtor (done in SQL) -> that channel;
    2. channel_by_location[location][doc_type] -> that channel;
    3. a debtor DocType row with any other non-blank debtor -> debtor_default_channel;
    4. anything else -> "other" (shown and counted, never folded into a retail channel).
    """
    if debtor_channel in CHANNELS:
        return debtor_channel
    by_location = cfg["channel_by_location"].get(location, {}).get(doc_type)
    if by_location:
        return by_location
    if doc_type in DEBTOR_HEADER_TABLES and has_debtor and cfg["debtor_default_channel"]:
        return cfg["debtor_default_channel"]
    return OTHER


def build_sales(rows, cfg, is_non_stock, day0, last_day):
    """Aggregate the analysis read. Pure.

    Returns (sales, received, reconcile, counts):
      sales     {(item, location, day, channel): [sold, returned]}; sold = units out net of
                returns (a CN row or a positive sales row is a return), returned = units back;
      received  {(item, location, "YYYY-MM"): qty} from the receipt DocTypes (net);
      reconcile {(doc_type, location, "YYYY-MM"): [net_out, qty_in]} for the VM SQL check (AC3);
      counts    rows, units per channel, other rows, non-stock rows excluded.
    A DO later invoiced posts no second stock row, so OS, IV and DO all count with no double count.
    """
    sale_types = set(cfg["doc_types"]["sales"]) | set(cfg["doc_types"]["returns"])
    receipt_types = set(cfg["doc_types"]["receipts"])
    sales = {}
    received = {}
    reconcile = {}
    counts = {
        "groups_read": 0,
        "ledger_rows": 0,
        "non_stock_rows_excluded": 0,
        "out_of_window_groups": 0,
        "unclassified_doc_type_groups": 0,
        "sold_units_by_channel": {c: 0 for c in CHANNELS},
        "returned_units_by_channel": {c: 0 for c in CHANNELS},
        "other_groups": 0,
        "receipt_units": 0,
    }
    sold_by_channel = {c: Decimal(0) for c in CHANNELS}
    returned_by_channel = {c: Decimal(0) for c in CHANNELS}
    receipt_units = Decimal(0)
    for r in rows:
        counts["groups_read"] += 1
        n_rows = int(r.get("LedgerRows") or 0)
        counts["ledger_rows"] += n_rows
        item = clean(r.get("ItemCode"))
        if is_non_stock(item):
            counts["non_stock_rows_excluded"] += n_rows
            continue
        day = as_day(r.get("DocDay"))
        if day < day0 or day > last_day:
            counts["out_of_window_groups"] += 1
            continue
        doc_type = clean(r.get("DocType"))
        location = clean(r.get("Location"))
        qty = to_dec(r.get("Qty"))
        qty_in = to_dec(r.get("QtyIn"))
        month = day.strftime("%Y-%m")
        if doc_type in receipt_types:
            key = (item, location, month)
            received[key] = received.get(key, Decimal(0)) + qty
            receipt_units += qty
        elif doc_type in sale_types:
            channel = resolve_channel(doc_type, location, clean(r.get("DebtorChannel")) or None,
                                      bool(r.get("HasDebtor")), cfg)
            if channel == OTHER:
                counts["other_groups"] += 1
            entry = sales.setdefault((item, location, day, channel), [Decimal(0), Decimal(0)])
            entry[0] -= qty
            entry[1] += qty_in
            sold_by_channel[channel] -= qty
            returned_by_channel[channel] += qty_in
        else:
            counts["unclassified_doc_type_groups"] += 1
            continue
        rec = reconcile.setdefault((doc_type, location, month), [Decimal(0), Decimal(0)])
        rec[0] -= qty
        rec[1] += qty_in
    sales = {k: v for k, v in sales.items() if v[0] != 0 or v[1] != 0}
    counts["sold_units_by_channel"] = {c: num(v) for c, v in sold_by_channel.items()}
    counts["returned_units_by_channel"] = {c: num(v) for c, v in returned_by_channel.items()}
    counts["receipt_units"] = num(receipt_units)
    return sales, received, reconcile, counts


def sales_payload(sales, reconcile, counts, header, day0):
    """The columnar sales file. Days are offsets from day0."""
    keys = sorted(sales, key=lambda k: (k[0], k[1], k[2], CHANNELS.index(k[3])))
    items = sorted({k[0] for k in keys})
    locations = sorted({k[1] for k in keys})
    item_pos = {c: n for n, c in enumerate(items)}
    loc_pos = {c: n for n, c in enumerate(locations)}
    columns = {"item": [], "location": [], "day": [], "channel": [], "sold": [], "returned": []}
    for key in keys:
        sold, returned = sales[key]
        columns["item"].append(item_pos[key[0]])
        columns["location"].append(loc_pos[key[1]])
        columns["day"].append((key[2] - day0).days)
        columns["channel"].append(CHANNELS.index(key[3]))
        columns["sold"].append(num(sold))
        columns["returned"].append(num(returned))
    payload = dict(header)
    payload.update(
        {
            "schema": SALES_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "day0": day0.isoformat(),
            "sold_rule": "sold = units out net of returns; returned = units back (CN rows and positive sales rows)",
            "channels": list(CHANNELS),
            "items": items,
            "locations": locations,
            "fields": ["item", "location", "day", "channel", "sold", "returned"],
            "columns": columns,
            "reconcile_fields": ["doc_type", "location", "month", "net_out", "qty_in"],
            "reconcile": [
                [doc_type, location, month, num(v[0]), num(v[1])]
                for (doc_type, location, month), v in sorted(reconcile.items())
            ],
            "counts": dict(counts, rows=len(keys), items=len(items)),
        }
    )
    return payload


# ---------------------------------------------------------------------------------------------
# Stock state: zero intervals per item and pool, from the ledger already in memory
# ---------------------------------------------------------------------------------------------


def walk_groups(movements, last_day, groups, snapshot_day=None):
    """Run the ledger day by day for location groups. Pure.

    movements {day: {(item, uom, location): qty}} (the exporter's ledger); groups {gid: [locs]}.
    The close of a day sums every movement up to and including that day, from the first ledger
    row (the daily-close rule). Returns {(item, gid): state} with
      first_stock_day  first day the group close was > 0;
      intervals        [[from, to], ...] inclusive days with close <= 0 after first stock (the
                       last one may run to last_day);
      qty_now          close of last_day;
      at_snapshot      close of snapshot_day (or None when no snapshot_day).
    Days after last_day (future-dated rows) are never read.
    """
    of_loc = {}
    for gid, locs in groups.items():
        for loc in locs:
            of_loc.setdefault(loc, []).append(gid)
    totals = {}
    state = {}
    snapshot = None
    for day in sorted(d for d in movements if d <= last_day):
        if snapshot_day is not None and snapshot is None and day > snapshot_day:
            snapshot = dict(totals)
        touched = set()
        for (item, _uom, loc), qty in movements[day].items():
            for gid in of_loc.get(loc, ()):
                key = (item, gid)
                totals[key] = totals.get(key, Decimal(0)) + qty
                touched.add(key)
        for key in touched:
            total = totals[key]
            st = state.setdefault(key, {"first_stock_day": None, "intervals": [], "zero_since": None})
            if total > 0:
                if st["first_stock_day"] is None:
                    st["first_stock_day"] = day
                if st["zero_since"] is not None:
                    st["intervals"].append([st["zero_since"], day - timedelta(days=1)])
                    st["zero_since"] = None
            elif st["first_stock_day"] is not None and st["zero_since"] is None:
                st["zero_since"] = day
    if snapshot_day is not None and snapshot is None:
        snapshot = dict(totals)
    for key, st in state.items():
        since = st.pop("zero_since")
        if since is not None:
            # Still zero at the end: the interval runs to last_day.
            st["intervals"].append([since, last_day])
        st["qty_now"] = totals.get(key, Decimal(0))
        st["at_snapshot"] = None if snapshot is None else snapshot.get(key, Decimal(0))
    return state


def stock_state_payload(state, received_by_pool, pools, header, day0, last_day):
    """The stock-state file: one row per item and pool that ever held stock or received any."""
    pool_ids = sorted(pools)
    keys = sorted(set(k for k, st in state.items() if st["first_stock_day"] is not None) | set(received_by_pool))
    items = sorted({k[0] for k in keys})
    item_pos = {c: n for n, c in enumerate(items)}
    rows = []
    for item, pool in keys:
        st = state.get((item, pool))
        first = st["first_stock_day"] if st else None
        received = received_by_pool.get((item, pool), {})
        rows.append(
            [
                item_pos[item],
                pool_ids.index(pool),
                None if first is None else (first - day0).days,
                [[(a - day0).days, (b - day0).days] for a, b in (st["intervals"] if st else [])],
                num(st["qty_now"]) if st else 0,
                num(st["at_snapshot"]) if st and st["at_snapshot"] is not None else 0,
                {month: num(q) for month, q in sorted(received.items()) if q != 0},
            ]
        )
    payload = dict(header)
    payload.update(
        {
            "schema": STATE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "day0": day0.isoformat(),
            "interval_rule": "Days (offsets from day0, inclusive) whose pool close was zero or negative after the first day the pool held stock; closes sum from the first ledger row",
            "pools": [{"id": p, "locations": list(pools[p])} for p in pool_ids],
            "items": items,
            "fields": ["item", "pool", "first_stock_day", "zero_intervals", "qty_now", "opening_at_golive", "received"],
            "rows": rows,
            # A closed interval ends the day before stock came back, so only an open one ends on last_day.
            "counts": {"rows": len(rows), "items": len(items), "zero_now": sum(
                1 for k in keys if state.get(k) and state[k]["intervals"] and state[k]["intervals"][-1][1] == last_day)},
        }
    )
    return payload


def received_by_pool(received, pools):
    """{(item, pool): {month: qty}} from {(item, location, month): qty}."""
    out = {}
    for (item, loc, month), qty in received.items():
        for pool, locs in pools.items():
            if loc in locs:
                months = out.setdefault((item, pool), {})
                months[month] = months.get(month, Decimal(0)) + qty
    return out


# ---------------------------------------------------------------------------------------------
# Placement: an item at zero at one location while another holds it
# ---------------------------------------------------------------------------------------------


def location_series(movements, locations, first_day, last_day):
    """{(item, location): [close per day first_day..last_day]} for keys that are ever non-zero."""
    n = (last_day - first_day).days + 1
    start = {}
    deltas = {}
    for day, moves in movements.items():
        if day > last_day:
            continue
        for (item, _uom, loc), qty in moves.items():
            if loc not in locations:
                continue
            key = (item, loc)
            if day < first_day:
                start[key] = start.get(key, Decimal(0)) + qty
            else:
                d = deltas.setdefault(key, {})
                offset = (day - first_day).days
                d[offset] = d.get(offset, Decimal(0)) + qty
    out = {}
    for key in set(start) | set(deltas):
        value = start.get(key, Decimal(0))
        changes = deltas.get(key, {})
        series = []
        for i in range(n):
            value += changes.get(i, Decimal(0))
            series.append(value)
        if any(v != 0 for v in series):
            out[key] = series
    return out


def build_placement(movements, cfg, sales, last_day, first_ledger_day):
    """Placement rows for the configured pairs over the last PLACEMENT_WINDOW_DAYS days. Pure.

    For each pair and each of its zero locations Z: a day counts when Z's close is zero or
    negative after the first day Z ever held the item, while the holder locations together hold
    at least placement.min_hold. Rows with no such day in the window are left out.
    """
    pairs = cfg["placement"]["pairs"]
    min_hold = Decimal(cfg["placement"]["min_hold"])
    if not pairs or first_ledger_day is None:
        return [], None
    first_day = max(first_ledger_day, last_day - timedelta(days=PLACEMENT_WINDOW_DAYS - 1))
    if first_day > last_day:
        return [], None
    n = (last_day - first_day).days + 1
    short_from = max(0, n - PLACEMENT_SHORT_DAYS)
    zero_locs = {z for p in pairs for z in p["zero_locations"]}
    holder_locs = {h for p in pairs for h in p["holder_locations"]}
    series = location_series(movements, zero_locs | holder_locs, first_day, last_day)
    first_stock = {
        (item, gid): st["first_stock_day"]
        for (item, gid), st in walk_groups(movements, last_day, {z: [z] for z in zero_locs}).items()
        if st["first_stock_day"] is not None
    }
    sold_90 = {}
    for (item, loc, day, _channel), (sold, _returned) in sales.items():
        if loc in zero_locs and first_day <= day <= last_day:
            sold_90[(item, loc)] = sold_90.get((item, loc), Decimal(0)) + sold
    zeros = [Decimal(0)] * n
    rows = []
    for pair in pairs:
        holders_by_item = {}
        for (item, loc), values in series.items():
            if loc in pair["holder_locations"]:
                acc = holders_by_item.setdefault(item, [Decimal(0)] * n)
                for i, v in enumerate(values):
                    acc[i] += v
        for item in sorted(holders_by_item):
            holder = holders_by_item[item]
            if max(holder) < min_hold:
                continue
            for z in pair["zero_locations"]:
                first = first_stock.get((item, z))
                if first is None:
                    continue
                zero = series.get((item, z), zeros)
                flags = [
                    zero[i] <= 0 and holder[i] >= min_hold and first_day + timedelta(days=i) > first
                    for i in range(n)
                ]
                days_90 = sum(flags)
                if not days_90:
                    continue
                rows.append(
                    {
                        "item": item,
                        "pair": pair["id"],
                        "zero_location": z,
                        "zero_qty_now": zero[-1],
                        "holder_qty_now": holder[-1],
                        "zero_days_30": sum(flags[short_from:]),
                        "zero_days_90": days_90,
                        "open_now": flags[-1],
                        "sold_at_zero_loc_90": sold_90.get((item, z), Decimal(0)),
                    }
                )
    rows.sort(key=lambda r: (r["pair"], not r["open_now"], -r["zero_days_90"], r["item"], r["zero_location"]))
    return rows, first_day


PLACEMENT_FIELDS = (
    "item", "pair", "zero_location", "zero_qty_now", "holder_qty_now", "zero_days_30", "zero_days_90", "open_now",
    "sold_at_zero_loc_90",
)


def placement_payload(rows, cfg, header, window_from_day):
    items = sorted({r["item"] for r in rows})
    item_pos = {c: n for n, c in enumerate(items)}
    pair_ids = [p["id"] for p in cfg["placement"]["pairs"]]
    out_rows = []
    for r in rows:
        out_rows.append(
            [
                item_pos[r["item"]], pair_ids.index(r["pair"]), r["zero_location"], num(r["zero_qty_now"]),
                num(r["holder_qty_now"]), r["zero_days_30"], r["zero_days_90"], bool(r["open_now"]),
                num(r["sold_at_zero_loc_90"]),
            ]
        )
    payload = dict(header)
    payload.update(
        {
            "schema": PLACEMENT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "window_from": window_from_day.isoformat() if window_from_day else None,
            "window_days": PLACEMENT_WINDOW_DAYS,
            "short_window_days": PLACEMENT_SHORT_DAYS,
            "min_hold": cfg["placement"]["min_hold"],
            "rule": "A day counts when the zero location's close is <= 0 after it first held the item, while the holder locations together hold >= min_hold",
            "pairs": [
                {"id": p["id"], "label": p["label"], "zero_locations": list(p["zero_locations"]),
                 "holder_locations": list(p["holder_locations"])}
                for p in cfg["placement"]["pairs"]
            ],
            "items": items,
            "fields": list(PLACEMENT_FIELDS),
            "rows": out_rows,
            "counts": {"rows": len(out_rows), "open_now": sum(1 for r in rows if r["open_now"])},
        }
    )
    return payload


# ---------------------------------------------------------------------------------------------
# Highlights: deterministic rules, each line with its rule id and inputs
# ---------------------------------------------------------------------------------------------


def _pct(part, whole):
    if not whole:
        return None
    return round(float(part) * 100.0 / float(whole), 1)


def build_highlights(cfg, attrs_by_item, items, state, received_pool, sales, placement_rows, last_day):
    """Rule lines per brand, top_n per rule and brand. Pure.

    Rules (design section 5.3):
      sold_out_early  first zero within sold_out_days of first stock, or sell-through >= pct;
      open_placement  a placement row that is open now;
      size_drift      sold vs stocked size share differs by >= drift_pts with n >= drift_min_units
                      (per brand, category and gender; sized items only);
      slow_seller     pool stock > 0, no sale in slow_days, first stock more than slow_days ago.
    stocked = opening at go-live (if positive) + received in the pool. Sales count only the
    highlight channels; size drift uses the stock-take window when one is set.
    """
    hl = cfg["highlights"]
    pool = hl["pool"]
    channels = set(hl["channels"])
    take = cfg["stock_take_day"]
    drift_from = date.fromisoformat(take) if take else None

    sold_by_item = {}
    sold_since_take = {}
    last_sale = {}
    for (item, _loc, day, channel), (sold, _ret) in sales.items():
        if channel not in channels:
            continue
        sold_by_item.setdefault(item, []).append((day, sold))
        if drift_from is None or day >= drift_from:
            sold_since_take[item] = sold_since_take.get(item, Decimal(0)) + sold
        if sold > 0 and (item not in last_sale or day > last_sale[item]):
            last_sale[item] = day

    def stocked(item):
        st = state.get((item, pool))
        opening = st["at_snapshot"] if st and st["at_snapshot"] is not None and st["at_snapshot"] > 0 else Decimal(0)
        return opening + sum(received_pool.get((item, pool), {}).values(), Decimal(0))

    lines = {rule: {} for rule in HIGHLIGHT_RULES}

    def add(rule, brand, item, rank, inputs):
        lines[rule].setdefault(brand, []).append((rank, item or "", inputs))

    for (item, gid), st in state.items():
        if gid != pool or st["first_stock_day"] is None:
            continue
        brand = brand_of(items.get(item, {}))
        first = st["first_stock_day"]
        sold = sum((q for d, q in sold_by_item.get(item, []) if d >= first), Decimal(0))
        stock_in = stocked(item)
        first_zero = st["intervals"][0][0] if st["intervals"] else None
        days_to_zero = (first_zero - first).days if first_zero else None
        zero_days = sum((b - a).days + 1 for a, b in st["intervals"])
        st_pct = _pct(sold, stock_in) if stock_in > 0 else None
        early = days_to_zero is not None and days_to_zero <= hl["sold_out_days"]
        through = st_pct is not None and sold > 0 and st_pct >= hl["sell_through_pct"]
        if early or through:
            add("sold_out_early", brand, item, (-sold, item), {
                "first_stock_day": first.isoformat(),
                "first_zero_day": first_zero.isoformat() if first_zero else None,
                "days_to_zero": days_to_zero,
                "zero_days": zero_days,
                "sold": num(sold),
                "stocked": num(stock_in),
                "sell_through_pct": st_pct,
                "qty_now": num(st["qty_now"]),
            })
        slow_from = last_day - timedelta(days=hl["slow_days"])
        if st["qty_now"] > 0 and first <= slow_from and (item not in last_sale or last_sale[item] <= slow_from):
            add("slow_seller", brand, item, (-st["qty_now"], item), {
                "first_stock_day": first.isoformat(),
                "last_sale_day": last_sale[item].isoformat() if item in last_sale else None,
                "qty_now": num(st["qty_now"]),
            })

    # Demand first (G2 L5): most open rows have no sale at the zero location, and the top_n cap
    # must never drop a row with demand for one without. Rows under placement_min_sold are left
    # out (counted); with 0, no-demand rows are listed after every demand row.
    min_sold = Decimal(hl["placement_min_sold"])
    below_min_sold = 0
    for r in placement_rows:
        if r["open_now"]:
            sold_90 = to_dec(r["sold_at_zero_loc_90"])
            if sold_90 < min_sold:
                below_min_sold += 1
                continue
            brand = brand_of(items.get(r["item"], {}))
            rank = (sold_90 <= 0, -r["zero_days_30"], -r["zero_days_90"], -sold_90, r["item"], r["zero_location"])
            add("open_placement", brand, r["item"], rank, {
                "pair": r["pair"],
                "zero_location": r["zero_location"],
                "holder_qty_now": num(r["holder_qty_now"]),
                "zero_days_30": r["zero_days_30"],
                "zero_days_90": r["zero_days_90"],
                "sold_at_zero_loc_90": num(r["sold_at_zero_loc_90"]),
            })

    groups = {}
    for item, attr in attrs_by_item.items():
        if not is_sized(attr) or (item, pool) not in state:
            continue
        key = (brand_of(items.get(item, {})), attr["category"], attr["gender"])
        sizes = groups.setdefault(key, {})
        entry = sizes.setdefault(attr["size"], {"sold": Decimal(0), "stocked": Decimal(0), "order": attr["size_order"]})
        entry["sold"] += sold_since_take.get(item, Decimal(0))
        entry["stocked"] += stocked(item)
    for (brand, category, gender), sizes in sorted(groups.items()):
        n_sold = sum((e["sold"] for e in sizes.values()), Decimal(0))
        n_stocked = sum((e["stocked"] for e in sizes.values()), Decimal(0))
        if n_sold < hl["drift_min_units"] or n_stocked <= 0:
            continue
        for size, e in sorted(sizes.items(), key=lambda kv: (kv[1]["order"], kv[0])):
            sold_share = _pct(e["sold"], n_sold)
            stocked_share = _pct(e["stocked"], n_stocked)
            diff = round(sold_share - stocked_share, 1)
            if abs(diff) >= hl["drift_pts"]:
                add("size_drift", brand, None, (-abs(diff), category, gender, e["order"] or 0), {
                    "category": category,
                    "gender": gender,
                    "size": size,
                    "window_from": take,
                    "sold": num(e["sold"]),
                    "sold_share_pct": sold_share,
                    "stocked": num(e["stocked"]),
                    "stocked_share_pct": stocked_share,
                    "diff_pts": diff,
                    "n_sold": num(n_sold),
                })

    out = []
    counts = {}
    for rule in HIGHLIGHT_RULES:
        total = 0
        truncated_brands = 0
        for brand in sorted(lines[rule]):
            entries = sorted(lines[rule][brand], key=lambda e: e[0])
            total += len(entries)
            if len(entries) > hl["top_n"]:
                truncated_brands += 1
            for _rank, item, inputs in entries[: hl["top_n"]]:
                out.append({"rule": rule, "brand": brand, "item": item or None, "inputs": inputs})
        counts[rule] = {"lines_before_cap": total, "brands_truncated": truncated_brands}
    counts["open_placement"]["open_below_min_sold"] = below_min_sold
    return out, counts


def highlights_payload(lines, counts, cfg, header):
    payload = dict(header)
    hl = cfg["highlights"]
    payload.update(
        {
            "schema": HIGHLIGHTS_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "pool": hl["pool"],
            "channels": list(hl["channels"]),
            "rules": {
                "sold_out_early": "first zero within %d days of first stock, or sell-through >= %d%%" % (hl["sold_out_days"], hl["sell_through_pct"]),
                "open_placement": "a placement row that is open now with >= %d sold at the zero location in the placement window; demand rows first" % hl["placement_min_sold"],
                "size_drift": "sold vs stocked size share differs by >= %d pts with n >= %d sold" % (hl["drift_pts"], hl["drift_min_units"]),
                "slow_seller": "pool stock > 0, no sale in %d days, first stock more than %d days ago" % (hl["slow_days"], hl["slow_days"]),
            },
            "top_n_per_rule_and_brand": hl["top_n"],
            "lines": lines,
            "counts": dict(counts, lines=len(lines)),
        }
    )
    return payload


# ---------------------------------------------------------------------------------------------
# Coverage and the whole build
# ---------------------------------------------------------------------------------------------


def size_coverage(attrs_by_item, items, sales):
    """Per brand: items, sized items, items with an unrecognised size (class "other", never
    counted as sized), sold units and sold units of sized items (AC5, G2 L4)."""
    sold = {}
    for (item, _loc, _day, _ch), (s, _r) in sales.items():
        sold[item] = sold.get(item, Decimal(0)) + s
    out = {}
    for item, attr in attrs_by_item.items():
        brand = brand_of(items.get(item, {}))
        entry = out.setdefault(brand, {"items": 0, "items_sized": 0, "items_size_other": 0,
                                       "sold_units": Decimal(0), "sold_units_sized": Decimal(0)})
        entry["items"] += 1
        sized = is_sized(attr)
        entry["items_sized"] += sized
        entry["items_size_other"] += attr["size_class"] == "other"
        entry["sold_units"] += sold.get(item, Decimal(0))
        if sized:
            entry["sold_units_sized"] += sold.get(item, Decimal(0))
    return {
        brand: dict(v, sold_units=num(v["sold_units"]), sold_units_sized=num(v["sold_units_sized"]))
        for brand, v in sorted(out.items())
    }


def attribute_counts(attrs_by_item, raw_genders):
    unrecognised = {}
    for attr in attrs_by_item.values():
        if attr["size_class"] == "other":
            unrecognised[attr["size"]] = unrecognised.get(attr["size"], 0) + 1
    unknown_gender = sum(1 for code, g in raw_genders.items() if g and not attrs_by_item[code]["gender"])
    top = sorted(unrecognised.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "items": len(attrs_by_item),
        "items_sized": sum(1 for a in attrs_by_item.values() if is_sized(a)),
        "items_with_gender": sum(1 for a in attrs_by_item.values() if a["gender"]),
        "items_unknown_gender_value": unknown_gender,
        "unrecognised_size_items": sum(unrecognised.values()),
        "unrecognised_size_values": [[v, n] for v, n in top[:UNRECOGNISED_LIST_CAP]],
        "unrecognised_size_values_truncated": len(top) > UNRECOGNISED_LIST_CAP,
    }


def build_analysis(cfg, rows, movements, items, is_non_stock, first_ledger_day, last_day, header):
    """Every analysis payload from the analysis read and the ledger already in memory. Pure.

    Returns ({file name: payload}, manifest_parts). header carries job, run_id, as_of_local,
    timezone and built_from_close_day for every file.
    """
    day0 = window_from(first_ledger_day, last_day, cfg)
    sales, received, reconcile, sales_counts = build_sales(rows, cfg, is_non_stock, day0, last_day)
    pools = cfg["pools"]
    golive = date.fromisoformat(cfg["golive_day"])
    state = walk_groups(movements, last_day, pools, snapshot_day=golive)
    rec_pool = received_by_pool(received, pools)
    placement_rows, placement_from = build_placement(movements, cfg, sales, last_day, first_ledger_day)
    stock_codes = [code for code in items if not is_non_stock(code)]
    attrs_by_item = {code: item_attributes(items[code], cfg["attributes"]) for code in stock_codes}
    raw_genders = {code: clean(items[code].get("StockGender")) for code in stock_codes}
    lines, hl_counts = build_highlights(cfg, attrs_by_item, items, state, rec_pool, sales, placement_rows, last_day)
    common = dict(header, quantity_rule=QUANTITY_RULE)
    payloads = {
        SALES_NAME: sales_payload(sales, reconcile, sales_counts, common, day0),
        STATE_NAME: stock_state_payload(state, rec_pool, pools, common, day0, last_day),
        PLACEMENT_NAME: placement_payload(placement_rows, cfg, common, placement_from),
        HIGHLIGHTS_NAME: highlights_payload(lines, hl_counts, cfg, common),
    }
    other = {
        "groups": sales_counts["other_groups"],
        "sold_units": sales_counts["sold_units_by_channel"][OTHER],
    }
    parts = {
        "windows_hint": {
            "golive_day": cfg["golive_day"],
            "stock_take_day": cfg["stock_take_day"],
            "sales_from": day0.isoformat(),
            "sales_to": last_day.isoformat(),
            "placement_from": placement_from.isoformat() if placement_from else None,
        },
        "channels": list(CHANNELS),
        "sold_units_by_channel": sales_counts["sold_units_by_channel"],
        "other": other,
        "size_coverage": size_coverage(attrs_by_item, items, sales),
        "attributes": attribute_counts(attrs_by_item, raw_genders),
        "settings_digest": settings_digest(cfg),
    }
    return payloads, parts


def manifest_payload(header, files, parts):
    """files: [{name, schema, schema_version, sha256, bytes, counts}] in publish order."""
    payload = dict(header)
    payload.update(
        {
            "schema": MANIFEST_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "quantity_rule": QUANTITY_RULE,
            "files": files,
        }
    )
    payload.update(parts)
    return payload
