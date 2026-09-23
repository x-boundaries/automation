"""Canonical source-event validation and deterministic field derivation."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .crypto import payload_hash
from .models import SourceEvent, SourceRejection


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
# The phone is an opaque digit string. Only the characters below are accepted as
# presentation and stripped; anything else (letters, extensions, tabs/newlines,
# Unicode digits, other punctuation) is rejected rather than silently dropped.
PHONE_UNSUPPORTED_RE = re.compile(r"[^0-9 ()+.\-]")
PHONE_PRESENTATION_RE = re.compile(r"^\+?[0-9 ().\-]*$")
# 15 is the largest base that still leaves the production allocator its full
# effective horizon: 15 digits plus "X9999" is exactly member_no_max_length 20.
PHONE_MIN_DIGITS = 6
PHONE_MAX_DIGITS = 15
PHONE_DIGITS_RE = re.compile(rf"^[0-9]{{{PHONE_MIN_DIGITS},{PHONE_MAX_DIGITS}}}$")


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
    """Return the supplied phone as an opaque canonical ASCII digit string.

    A country code is optional and is never validated, inferred, or prepended,
    so a local-looking number and the same number carrying a country prefix stay
    distinct values. Only the presentation characters are removed; the digits
    themselves, including any leading zero, are preserved exactly as supplied.
    """

    raw = normalize_text(value)
    if not raw:
        raise CanonicalizationError("phone_required")
    if PHONE_UNSUPPORTED_RE.search(raw):
        raise CanonicalizationError("phone_contains_letters_or_unsupported_characters")
    if not PHONE_PRESENTATION_RE.fullmatch(raw):
        # A "+" is presentation only: at most one, and only in the lead position.
        raise CanonicalizationError("phone_shape_invalid")
    digits = re.sub(r"[^0-9]", "", raw)
    if not PHONE_DIGITS_RE.fullmatch(digits):
        raise CanonicalizationError("phone_shape_invalid")
    return digits


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


def parse_rfc3339(value: Any, *, field: str = "create_time") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalizationError(f"{field}_required")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CanonicalizationError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalizationError(f"{field}_timezone_required")
    return parsed


# Google Forms issues createTime in UTC with exactly 0, 3, 6 or 9 fractional
# digits. The exact string is immutable source identity and is never
# round-tripped through datetime, timestamptz, or a JavaScript Date.
GOOGLE_CREATE_TIME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<frac>\.\d{3}|\.\d{6}|\.\d{9})?Z$",
    re.ASCII,
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_google_create_time_exact(value: Any, *, field: str = "create_time_exact") -> tuple[int, int]:
    """Return the lossless ``(utc_epoch_second, nanoseconds)`` ordering key.

    Only the calendar part is validated through ``datetime`` (whole seconds, so
    nothing can be truncated). The fraction is read as digits and right-padded
    to nine places, never rounded.
    """

    if not isinstance(value, str) or not value:
        raise CanonicalizationError(f"{field}_required")
    match = GOOGLE_CREATE_TIME_RE.fullmatch(value)
    if match is None:
        raise CanonicalizationError(f"{field}_invalid")
    try:
        whole = datetime.strptime(f"{match['date']}T{match['time']}", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise CanonicalizationError(f"{field}_invalid") from exc
    delta = whole - _EPOCH
    seconds = delta.days * 86400 + delta.seconds
    fraction = match["frac"]
    nanos = int(fraction[1:].ljust(9, "0")) if fraction else 0
    return seconds, nanos


def validate_google_create_time_exact(value: Any, *, field: str = "create_time_exact") -> str:
    """Validate and return the exact source string unchanged."""

    parse_google_create_time_exact(value, field=field)
    return value


def create_time_utc_from_exact(value: str) -> datetime:
    """Derivative business timestamp, truncated (never rounded) to microseconds.

    This value may support business-date queries and ``RegisterDate``; it is
    never compared as immutable source identity.
    """

    seconds, nanos = parse_google_create_time_exact(value)
    return _EPOCH + timedelta(seconds=seconds, microseconds=nanos // 1000)


def render_create_time_utc(value: datetime) -> str:
    """Render a derivative instant with no rounding and no fabricated fraction."""

    if value.tzinfo is None:
        raise CanonicalizationError("create_time_timezone_required")
    utc = value.astimezone(timezone.utc)
    if utc.microsecond:
        return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{utc.microsecond:06d}Z"
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def verbatim_rfc3339(value: Any, *, field: str = "create_time") -> str:
    """Validate an RFC3339 instant and return the exact supplied string."""

    if isinstance(value, str) and value != value.strip():
        raise CanonicalizationError(f"{field}_invalid")
    parse_rfc3339(value, field=field)
    return value


def exact_filter(production_cutover_exact: str) -> str:
    """The verbatim inclusive Forms filter bound to one immutable cutover."""

    validate_google_create_time_exact(production_cutover_exact, field="production_cutover_exact")
    return f"timestamp >= {production_cutover_exact}"


def validate_page_token(value: Any, *, allow_none: bool = True) -> str | None:
    """Validate an opaque token without ever interpreting or logging it."""

    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[\x20-\x7e]{1,1024}", value):
        raise CanonicalizationError("forms_page_token_invalid")
    return value


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
    # Preserved verbatim, never reformatted. The strict Google 0/3/6/9-digit
    # 'Z' form is enforced at the ingest API and durable admission boundaries.
    create_time = verbatim_rfc3339(value["create_time"], field="create_time")
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


SOURCE_REJECTION_SCHEMA_VERSION = "xb.member.source_rejection.v1"
REJECTION_FIELDS = frozenset(
    {
        "schema_version",
        "source_system",
        "form_alias",
        "response_id",
        "create_time",
        "mapping_version",
        "request_id",
        "payload_hash",
        "error_code",
        "operation",
    }
)
# Customer-input validation failures only. Mapping, identity, timestamp, token,
# schema and OAuth failures are never rejections: they halt the page instead.
CUSTOMER_REJECTION_CODES = frozenset(
    {
        "name_invalid",
        "email_invalid",
        "phone_required",
        "phone_contains_letters_or_unsupported_characters",
        "phone_shape_invalid",
        "birthday_month_invalid",
        "marketing_consent_must_be_yes_or_no",
    }
)


def canonicalize_source_rejection(value: Mapping[str, Any]) -> SourceRejection:
    """Validate a PII-free customer rejection. It carries no customer field."""

    if not isinstance(value, Mapping):
        raise CanonicalizationError("source_rejection_must_be_object")
    if set(value) != REJECTION_FIELDS:
        raise CanonicalizationError("source_rejection_fields_invalid")
    if value["schema_version"] != SOURCE_REJECTION_SCHEMA_VERSION:
        raise CanonicalizationError("source_rejection_schema_version_invalid")
    if value["source_system"] != "google_forms":
        raise CanonicalizationError("source_system_invalid")
    if value["operation"] != OPERATION:
        raise CanonicalizationError("operation_invalid")
    error_code = value["error_code"]
    if not isinstance(error_code, str) or error_code not in CUSTOMER_REJECTION_CODES:
        raise CanonicalizationError("source_rejection_code_invalid")
    supplied_hash = value["payload_hash"]
    if not isinstance(supplied_hash, str) or not HASH_RE.fullmatch(supplied_hash):
        raise CanonicalizationError("payload_hash_format_invalid")
    return SourceRejection(
        schema_version=SOURCE_REJECTION_SCHEMA_VERSION,
        source_system="google_forms",
        form_alias=_safe_identity(value["form_alias"], "form_alias"),
        response_id=_safe_identity(value["response_id"], "response_id"),
        create_time=validate_google_create_time_exact(value["create_time"], field="create_time"),
        mapping_version=_safe_identity(value["mapping_version"], "mapping_version"),
        request_id=_safe_identity(value["request_id"], "request_id"),
        payload_hash=supplied_hash,
        error_code=error_code,
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
    if isinstance(create_time, str):
        # Exact Google forms truncate (never round) through the derivative
        # instant, so a 9-digit fraction cannot move RegisterDate forward.
        parsed = (
            create_time_utc_from_exact(create_time)
            if GOOGLE_CREATE_TIME_RE.fullmatch(create_time)
            else parse_rfc3339(create_time)
        )
    else:
        parsed = create_time
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
        token = validate_page_token(next_token, allow_none=False)
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
