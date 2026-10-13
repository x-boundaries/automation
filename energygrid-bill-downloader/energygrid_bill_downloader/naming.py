"""Presentation filename for the EnergyGrid v4 offline core (#226 G3).

The archive keeps its frozen date-only identity (`YYYY-MM-DD.pdf`, see
`invoice.Candidate.canonical_filename`). Presentation surfaces, meaning the
Drive copy name and the email attachment name, use the display name:

    MMM YYYY - <source filename>

- `MMM` is an uppercase English month abbreviation (JAN..DEC) taken from the
  validated source invoice date, never from the source filename.
- The source filename is kept verbatim after the prefix, extension included.
- The prefix is applied exactly once. A source name that already starts with
  the same month and year (any letter case) is accepted and the prefix is
  normalised to uppercase. Any other leading `MMM YYYY - ` prefix, and any
  doubled prefix, is a conflict and fails closed.
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
DISPLAY_NAME_MAX_LENGTH = 260
_PREFIX_RE = re.compile(
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC) [0-9]{4} - ",
    re.ASCII | re.IGNORECASE,
)


def month_prefix(invoice_date: date) -> str:
    """Return the exact prefix for an invoice date, for example `OCT 2026 - `."""
    if type(invoice_date) is not date:
        raise OfflineV4Error("EG_V4_DISPLAY_DATE_INVALID")
    return f"{MONTH_ABBREVIATIONS[invoice_date.month - 1]} {invoice_date.year:04d} - "


def display_filename(source_filename: str, invoice_date: date) -> str:
    """Return `MMM YYYY - <source filename>` with the prefix applied exactly once."""
    prefix = month_prefix(invoice_date)
    if type(source_filename) is not str or not source_filename:
        raise OfflineV4Error("EG_V4_DISPLAY_SOURCE_INVALID")
    if source_filename != source_filename.lstrip():
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    remainder = source_filename
    match = _PREFIX_RE.match(source_filename)
    if match is not None:
        if match.group(0).casefold() != prefix.casefold():
            raise OfflineV4Error("EG_V4_DISPLAY_PREFIX_CONFLICT")
        remainder = source_filename[len(match.group(0)):]
    if _PREFIX_RE.match(remainder) is not None:
        raise OfflineV4Error("EG_V4_DISPLAY_PREFIX_CONFLICT")
    if not remainder.lower().endswith(".pdf") or not remainder[: -len(".pdf")].strip():
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    display = prefix + remainder
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
    if any(ord(char) < 32 or char in WINDOWS_INVALID_FILENAME_CHARS or char == "\\" for char in name):
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    if name.endswith((".", " ")) or not name.lower().endswith(".pdf"):
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    stem = name[: -len(".pdf")]
    if not stem.strip():
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
    device_base = name.split(".", 1)[0].rstrip(" .").upper()
    if device_base in WINDOWS_RESERVED_NAMES:
        raise OfflineV4Error("EG_V4_DISPLAY_NAME_INVALID")
