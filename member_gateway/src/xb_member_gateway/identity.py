"""XB-MN-1 member identity rule (W-G2-149 sections 2.1-2.3, amended).

The gateway computes the base MemberNo and the name component exactly once,
when a job reaches VALIDATED. Both values are stored immutably on the job and
are the only values a later result may claim as the created MemberNo. Nothing
here truncates or rewrites customer input: a value that does not fit is a
bounded ``REJECTED_VALIDATION`` reason.

Amendment (#155 comment 5890550314): a digits-only base that starts with
``000`` is an ordinary base. The only synthetic markers are a name starting
with ``"ZZTEST "`` (exact, trailing space included) and an email ending with
``"@example.invalid"`` (case-insensitive).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


RULE = "XB-MN-1"
BOOK_MODES = frozenset({"production", "test"})
MEMBER_NO_MAX_LENGTH = 20
NAME_UTF16_LIMIT = 100
EMAIL_UTF16_LIMIT = 200
SYNTHETIC_NAME_PREFIX = "ZZTEST "
SYNTHETIC_EMAIL_SUFFIX = "@example.invalid"
BASE_RE = re.compile(r"^[0-9]{6,15}$", re.ASCII)
NAME_COMPONENT_RE = re.compile(r"^[A-Z]{0,14}$", re.ASCII)

# Closed REJECTED_VALIDATION reasons produced at VALIDATED.
REJECTION_REASONS = frozenset(
    {
        "phone_digits_out_of_range",
        "name_exceeds_autocount_limit",
        "email_exceeds_autocount_limit",
        "synthetic_in_production",
        "test_book_requires_synthetic",
    }
)


class IdentityRejected(ValueError):
    """The job must become REJECTED_VALIDATION with this bounded reason."""

    def __init__(self, reason: str):
        if reason not in REJECTION_REASONS:
            raise ValueError("identity_reason_invalid")
        self.reason = reason
        super().__init__(reason)


class IdentityConfigError(ValueError):
    """The book mode is missing or not one of the closed values."""


@dataclass(frozen=True, slots=True)
class MemberIdentity:
    rule: str
    base_member_no: str
    name_component: str

    @property
    def name_appended_member_no(self) -> str:
        return self.base_member_no + self.name_component


def _digits_ascii(value: str) -> str:
    return "".join(character for character in value if "0" <= character <= "9")


def base_member_no(phone: Any) -> str:
    """Section 2.1: ASCII digits of NFKC(canonical phone); 6..15 digits."""

    if not isinstance(phone, str):
        raise IdentityRejected("phone_digits_out_of_range")
    base = _digits_ascii(unicodedata.normalize("NFKC", phone))
    if not BASE_RE.fullmatch(base):
        raise IdentityRejected("phone_digits_out_of_range")
    return base


def name_letters(name: str) -> str:
    """Section 2.2: NFKD, drop Mn, ASCII-only upper case map, drop the rest."""

    decomposed = unicodedata.normalize("NFKD", name)
    letters: list[str] = []
    for character in decomposed:
        if unicodedata.category(character) == "Mn":
            continue
        if "a" <= character <= "z":
            letters.append(chr(ord(character) - 32))
        elif "A" <= character <= "Z":
            letters.append(character)
    return "".join(letters)


def name_component(name: Any, base: str) -> str:
    if not isinstance(name, str) or not BASE_RE.fullmatch(base):
        raise IdentityRejected("phone_digits_out_of_range")
    component = name_letters(name)[: MEMBER_NO_MAX_LENGTH - len(base)]
    # Defensive: the construction can only produce this shape.
    if not NAME_COMPONENT_RE.fullmatch(component) or len(base + component) > MEMBER_NO_MAX_LENGTH:
        raise ValueError("name_component_shape_invalid")
    return component


def utf16_len(value: str) -> int:
    """Length in UTF-16 code units (supplementary-plane characters count 2)."""

    return len(value.encode("utf-16-le")) // 2


def synthetic_markers(name: str, email: str) -> tuple[bool, bool]:
    return name.startswith(SYNTHETIC_NAME_PREFIX), email.lower().endswith(SYNTHETIC_EMAIL_SUFFIX)


def validate_book_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in BOOK_MODES:
        raise IdentityConfigError("member_book_mode_invalid")
    return value


def derive_identity(payload: Mapping[str, Any], book_mode: Any) -> MemberIdentity:
    """Return the immutable XB-MN-1 identity or raise ``IdentityRejected``.

    Check order is fixed: base (2.1), name limit, email limit (2.3), then the
    synthetic-marker rule for the configured book mode.
    """

    mode = validate_book_mode(book_mode)
    name = payload.get("name")
    email = payload.get("email")
    base = base_member_no(payload.get("phone"))
    if not isinstance(name, str) or utf16_len(name) > NAME_UTF16_LIMIT:
        raise IdentityRejected("name_exceeds_autocount_limit")
    if not isinstance(email, str) or utf16_len(email) > EMAIL_UTF16_LIMIT:
        raise IdentityRejected("email_exceeds_autocount_limit")
    synthetic_name, synthetic_email = synthetic_markers(name, email)
    if mode == "production" and (synthetic_name or synthetic_email):
        raise IdentityRejected("synthetic_in_production")
    if mode == "test" and not (synthetic_name and synthetic_email):
        raise IdentityRejected("test_book_requires_synthetic")
    return MemberIdentity(RULE, base, name_component(name, base))
