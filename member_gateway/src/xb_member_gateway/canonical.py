"""Canonical source-event validation and deterministic field derivation."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .crypto import payload_hash
from .models import SourceEvent


class CanonicalizationError(ValueError):
    """Raised when a source event is missing, ambiguous, or malformed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


SOURCE_SCHEMA_VERSION = "xb.member.source_event.v1"
JOB_SCHEMA_VERSION = "xb.member.gateway.job.v1"
OPERATION = "member.create"
SINGAPORE = "Asia/Singapore"
DOB_YEAR = 2000
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
PAYLOAD_FIELDS = frozenset(
    {"name", "phone", "email", "birthday_month", "marketing_consent", "pdpa_acknowledged"}
)
EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "source_system",
        "form_alias",
        "response_id",
        "create_time",
        "mapping_version",
        "request_id",
        "payload",
        "payload_hash",
        "operation",
    }
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        raise CanonicalizationError("text_value_required")
    value = unicodedata.normalize("NFC", value)
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_spaces(value: Any) -> str:
    return " ".join(normalize_text(value).split())


def normalize_name(value: Any) -> str:
    name = normalize_spaces(value)
    if not 1 <= len(name) <= 200:
        raise CanonicalizationError("name_invalid")
    return name


def normalize_email(value: Any) -> str:
    email = normalize_spaces(value).lower()
    if not email or len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise CanonicalizationError("email_invalid")
    return email


def canonical_phone(value: Any) -> str:
    """Return the canonical Singapore phone-shaped identifier."""

    raw = normalize_text(value)
    if not raw:
        raise CanonicalizationError("phone_required")
    if re.search(r"[^0-9\s()+.\-]", raw):
        raise CanonicalizationError("phone_contains_letters_or_unsupported_characters")
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 8 and digits[0] in "89":
        return f"65{digits}"
    if len(digits) == 10 and digits.startswith("65") and digits[2] in "89":
        return digits
    raise CanonicalizationError("phone_shape_invalid")


def normalize_month(value: Any) -> str:
    month = normalize_spaces(value).lower()
    if month not in MONTHS:
        raise CanonicalizationError("birthday_month_invalid")
    return month.capitalize()


def birthday_month_to_dob(value: Any) -> str:
    month = normalize_month(value).lower()
    return f"{DOB_YEAR}-{MONTHS[month]:02d}-01"


def parse_marketing_consent(value: Any) -> tuple[str, bool]:
    normalized = normalize_spaces(value).lower()
    if normalized == "yes":
        return "Yes", True
    if normalized == "no":
        return "No", False
    raise CanonicalizationError("marketing_consent_must_be_yes_or_no")


def parse_pdpa_acknowledgement(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = normalize_spaces(value).lower()
    return normalized in {"yes", "i agree"}


def _safe_identity(value: Any, field: str) -> str:
    normalized = normalize_text(value)
    if not SAFE_ID_RE.fullmatch(normalized):
        raise CanonicalizationError(f"{field}_invalid")
    return normalized


def parse_rfc3339(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalizationError("create_time_required")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CanonicalizationError("create_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalizationError("create_time_timezone_required")
    return parsed


def format_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise CanonicalizationError("create_time_timezone_required")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise CanonicalizationError("payload_must_be_object")
    keys = set(payload)
    if keys != PAYLOAD_FIELDS:
        raise CanonicalizationError("payload_fields_invalid")
    marketing_consent, _ = parse_marketing_consent(payload["marketing_consent"])
    return {
        "name": normalize_name(payload["name"]),
        "phone": canonical_phone(payload["phone"]),
        "email": normalize_email(payload["email"]),
        "birthday_month": normalize_month(payload["birthday_month"]),
        "marketing_consent": marketing_consent,
        "pdpa_acknowledged": parse_pdpa_acknowledgement(payload["pdpa_acknowledged"]),
    }


def canonical_json(value: Any) -> str:
    """Stable UTF-8 JSON used for payload hashing."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonicalize_source_event(value: Mapping[str, Any]) -> SourceEvent:
    if not isinstance(value, Mapping):
        raise CanonicalizationError("source_event_must_be_object")
    if set(value) != EVENT_FIELDS:
        raise CanonicalizationError("source_event_fields_invalid")
    if value["schema_version"] != SOURCE_SCHEMA_VERSION:
        raise CanonicalizationError("source_schema_version_invalid")
    if value["source_system"] != "google_forms":
        raise CanonicalizationError("source_system_invalid")
    if value["operation"] != OPERATION:
        raise CanonicalizationError("operation_invalid")
    response_id = _safe_identity(value["response_id"], "response_id")
    request_id = _safe_identity(value["request_id"], "request_id")
    create_time = format_rfc3339(parse_rfc3339(value["create_time"]))
    form_alias = _safe_identity(value["form_alias"], "form_alias")
    mapping_version = _safe_identity(value["mapping_version"], "mapping_version")
    payload = _canonical_payload(value["payload"])
    supplied_hash = value["payload_hash"]
    if not isinstance(supplied_hash, str) or not HASH_RE.fullmatch(supplied_hash):
        raise CanonicalizationError("payload_hash_format_invalid")
    computed_hash = payload_hash(canonical_json(payload))
    if supplied_hash != computed_hash:
        raise CanonicalizationError("payload_hash_mismatch")
    return SourceEvent(
        schema_version=SOURCE_SCHEMA_VERSION,
        source_system="google_forms",
        form_alias=form_alias,
        response_id=response_id,
        create_time=create_time,
        mapping_version=mapping_version,
        request_id=request_id,
        payload=payload,
        payload_hash=computed_hash,
        operation=OPERATION,
    )


def canonical_payload_from_fields(
    *,
    name: str,
    phone: str,
    email: str,
    birthday_month: str,
    marketing_consent: str,
    pdpa_acknowledged: bool | str,
) -> dict[str, Any]:
    return _canonical_payload(
        {
            "name": name,
            "phone": phone,
            "email": email,
            "birthday_month": birthday_month,
            "marketing_consent": marketing_consent,
            "pdpa_acknowledged": pdpa_acknowledged,
        }
    )


def build_source_event(
    *,
    response_id: str,
    create_time: str,
    request_id: str,
    form_alias: str,
    mapping_version: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    canonical_payload = _canonical_payload(payload)
    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "source_system": "google_forms",
        "form_alias": form_alias,
        "response_id": response_id,
        "create_time": create_time,
        "mapping_version": mapping_version,
        "request_id": request_id,
        "payload": canonical_payload,
        "payload_hash": payload_hash(canonical_json(canonical_payload)),
        "operation": OPERATION,
    }


def derive_register_and_expiry(create_time: str | datetime) -> tuple[str, str]:
    parsed = parse_rfc3339(create_time) if isinstance(create_time, str) else create_time
    if parsed.tzinfo is None:
        raise CanonicalizationError("create_time_timezone_required")
    try:
        singapore_date = parsed.astimezone(ZoneInfo(SINGAPORE)).date()
    except ZoneInfoNotFoundError as exc:
        raise CanonicalizationError("singapore_timezone_unavailable") from exc
    try:
        anniversary = singapore_date.replace(year=singapore_date.year + 2)
    except ValueError:
        # A Feb 29 submission has no Feb 29 anniversary in a non-leap year.
        anniversary = singapore_date.replace(year=singapore_date.year + 2, day=28)
    expiry = anniversary - timedelta(days=1)
    return singapore_date.isoformat(), expiry.isoformat()


def iter_forms_pages(
    fetch_page: Callable[[str | None], Mapping[str, Any]],
    *,
    max_pages: int = 1000,
) -> Iterable[Mapping[str, Any]]:
    """Iterate synthetic/adapter pages while enforcing safe token progress."""

    token: str | None = None
    seen_tokens: set[str | None] = set()
    for _ in range(max_pages):
        if token in seen_tokens:
            raise CanonicalizationError("forms_page_token_repeated")
        seen_tokens.add(token)
        page = fetch_page(token)
        if not isinstance(page, Mapping):
            raise CanonicalizationError("forms_page_invalid")
        responses = page.get("responses", [])
        if not isinstance(responses, list):
            raise CanonicalizationError("forms_responses_invalid")
        for response in responses:
            if not isinstance(response, Mapping):
                raise CanonicalizationError("forms_response_invalid")
            yield response
        snake_token = page.get("next_page_token")
        camel_token = page.get("nextPageToken")
        if snake_token is not None and camel_token is not None and snake_token != camel_token:
            raise CanonicalizationError("forms_page_token_conflict")
        next_token = snake_token if snake_token is not None else camel_token
        if next_token is None or next_token == "":
            return
        if not isinstance(next_token, str):
            raise CanonicalizationError("forms_page_token_invalid")
        token = next_token
    raise CanonicalizationError("forms_pagination_limit_exceeded")


def dedupe_poll_responses(responses: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Keep overlap rows once, while retaining conflicting observations for ingest."""

    seen: set[str] = set()
    result: list[Mapping[str, Any]] = []
    for response in responses:
        if not isinstance(response, Mapping):
            raise CanonicalizationError("forms_response_invalid")
        response_id = response.get("response_id")
        if response_id is None:
            response_id = response.get("responseId")
        if not isinstance(response_id, str) or not response_id:
            raise CanonicalizationError("response_id_required")
        if response_id in seen:
            continue
        seen.add(response_id)
        result.append(response)
    return result
