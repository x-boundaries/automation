"""Helpers for writing CSV cells safely for spreadsheet consumers."""

from datetime import date, datetime
from decimal import Decimal
from unicodedata import category

DANGEROUS_FORMULA_PREFIXES = {"=", "+", "-", "@"}


def neutralize_csv_formula_cell(value):
    """Return a string value with spreadsheet formula prefixes neutralized.

    Spreadsheet applications can interpret text cells beginning with formula
    characters as executable formulas. We check the first meaningful character
    after leading whitespace/control characters and prefix dangerous strings with
    a single quote so spreadsheet tools treat them as text.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.startswith("'"):
        return value

    first_meaningful = _first_meaningful_character(value)
    if first_meaningful in DANGEROUS_FORMULA_PREFIXES:
        return f"'{value}"
    return value


def coerce_csv_cell(value):
    """Coerce a Python value to a safe CSV cell value.

    Numeric values remain numeric/stringified normally, so negative numeric
    values are not prefixed merely because their string representation starts
    with ``-``. Date/time values use ISO 8601 formatting for deterministic CSVs.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return neutralize_csv_formula_cell(value)
    if isinstance(value, (int, float, Decimal)):
        return value
    if isinstance(value, (datetime, date)) or hasattr(value, "isoformat"):
        return neutralize_csv_formula_cell(value.isoformat())
    return neutralize_csv_formula_cell(str(value))


def safe_csv_row(row, fieldnames):
    """Build a formula-neutralized CSV row for the supplied field order."""
    return {key: coerce_csv_cell(row.get(key, "")) for key in fieldnames}


def _first_meaningful_character(value):
    for character in value:
        if character.isspace() or category(character)[0] == "C":
            continue
        return character
    return ""
