"""Presentation filename for the EnergyGrid v4 offline core (#226 G3).

The archive keeps its frozen date-only identity (`YYYY-MM-DD.pdf`, see
`invoice.Candidate.canonical_filename`). Presentation surfaces, meaning the
Drive copy name, the user-facing named archive copy and the email text, use the display name:

    MMM YYYY - EnergyGrid - <EB Bill|Tenant Bill> - <source filename>

  for example `AUG 2026 - EnergyGrid - EB Bill - 2026-08-26_ACCOUNT_00001.pdf` (owner naming
  decision, #226 V3 G1 UAT).

- `MMM` is an uppercase English month abbreviation (JAN..DEC) taken from the
  validated source invoice date, never from the source filename.
- The stream label comes from the invoice stream (the `invoice.STREAM_LABELS` text).
  An unknown stream is refused (`EG_V4_DISPLAY_STREAM_INVALID`).
- The source filename is kept verbatim after the prefix, extension included.
- The month prefix is applied exactly once. A source name that already starts
  with the same `MMM YYYY - ` (any letter case) has that token removed before
  the full prefix is added, so the month appears once and in uppercase.
- Ambiguous shapes are refused, never guessed (`EG_V4_DISPLAY_PREFIX_CONFLICT`):
  a `MMM YYYY - ` token (any letter case, any month and year) anywhere except
  as the one accepted leading same-period prefix, or a source name or
  remainder that starts with a month abbreviation, a full English month name
  or `SEPT`, followed by a non-letter and a four-digit year with any or no
  ASCII separator in between (for example `OCT2026-x.pdf`, `OCTOBER 2026 - x.pdf`).
- Names are never truncated. An invalid or over-long result is refused.
- Old archive files are never renamed. The display name is computed at
  presentation time and stored once, immutably, in the v4 admission row.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from .errors import OfflineV4Error
from .publication import WINDOWS_INVALID_FILENAME_CHARS, WINDOWS_RESERVED_NAMES

MONTH_ABBREVIATIONS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
MONTH_NAMES = (
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
    "NOVEMBER", "DECEMBER",
)
DISPLAY_NAME_MAX_LENGTH = 260
DISPLAY_BRAND = "EnergyGrid"
# Same labels as invoice.STREAM_LABELS (tested); kept here so naming imports no source module.
STREAM_LABELS = {"EB_BILL": "EB Bill", "TENANT_BILL": "Tenant Bill"}
# The canonical prefix token, matched anywhere in a name.
_PREFIX_RE = re.compile(
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC) [0-9]{4} - ",
    re.ASCII | re.IGNORECASE,
)
# A leading month token, a non-letter boundary, then a four-digit year after any or no ASCII separator.
_LEADING_MONTH_YEAR_RE = re.compile(
    r"(?:" + "|".join(MONTH_NAMES + ("SEPT",) + MONTH_ABBREVIATIONS) + r")[^A-Za-z0-9]*[0-9]{4}",
    re.ASCII | re.IGNORECASE,
)


def month_prefix(invoice_date: date) -> str:
    """Return the exact prefix for an invoice date, for example `OCT 2026 - `."""
    if type(invoice_date) is not date:
        raise OfflineV4Error("EG_V4_DISPLAY_DATE_INVALID")
    return f"{MONTH_ABBREVIATIONS[invoice_date.month - 1]} {invoice_date.year:04d} - "


def display_prefix(invoice_date: date, stream: str) -> str:
    """Return the full prefix, for example `OCT 2026 - EnergyGrid - EB Bill - `."""
    month = month_prefix(invoice_date)
    if not isinstance(stream, str) or stream not in STREAM_LABELS:
        raise OfflineV4Error("EG_V4_DISPLAY_STREAM_INVALID")
    return f"{month}{DISPLAY_BRAND} - {STREAM_LABELS[stream]} - "


def display_filename(source_filename: str, invoice_date: date, *, stream: str) -> str:
    """Return `MMM YYYY - EnergyGrid - <label> - <source filename>`, the month applied exactly once."""
    prefix = month_prefix(invoice_date)
    full_prefix = display_prefix(invoice_date, stream)
    if type(source_filename) is not str or not source_filename:
        raise OfflineV4Error("EG_V4_DISPLAY_SOURCE_INVALID")
    if not source_filename.isascii():
        # Only qualified ASCII names are supported. Other real shapes are a typed HOLD, never a guess.
        raise OfflineV4Error("EG_V4_DISPLAY_SHAPE_UNSUPPORTED")
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in source_filename):
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    if source_filename != source_filename.lstrip():
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    exact = _PREFIX_RE.match(source_filename)
    if exact is not None and exact.group(0).casefold() == prefix.casefold():
        remainder = source_filename[len(exact.group(0)):]
    else:
        remainder = source_filename
    # Any prefix token left (another period, doubled or embedded) or a leading month-year shape is ambiguous.
    if _PREFIX_RE.search(remainder) is not None or _LEADING_MONTH_YEAR_RE.match(remainder) is not None:
        raise OfflineV4Error("EG_V4_DISPLAY_PREFIX_CONFLICT")
    if not remainder.lower().endswith(".pdf") or not remainder[: -len(".pdf")].strip():
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    display = full_prefix + remainder
    _validate_display(display)
    return display


def display_key(name: str) -> str:
    """Collision identity for a display name: NFC plus casefold, as for source names."""
    if type(name) is not str or not name:
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    return unicodedata.normalize("NFC", name).casefold()


def _validate_display(name: str) -> None:
    if len(name) > DISPLAY_NAME_MAX_LENGTH:
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    if any(unicodedata.category(char) == "Cc" or char in WINDOWS_INVALID_FILENAME_CHARS or char == "\\" for char in name):
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    if name.endswith((".", " ")) or not name.lower().endswith(".pdf"):
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    stem = name[: -len(".pdf")]
    if not stem.strip():
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    device_base = name.split(".", 1)[0].rstrip(" .").upper()
    if device_base in WINDOWS_RESERVED_NAMES:
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
