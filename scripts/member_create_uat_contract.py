"""Shared, pure contract library for the single-member creation UAT path.

This module is the Python source of truth for the bounded single-member creation
UAT. It is imported by the approval/package CLI, the sanitized result precheck,
and the tests. It performs no network, AutoCount, n8n, Google, or live action and
holds no credentials. The language-neutral package contract lives in
``schemas/member_create_uat_package.schema.json``; the checks here mirror that
schema exactly, and the PowerShell VM runner validates the same constraints
in-process (the VM is not assumed to have Python).

Identity model (per approved design):

* ``source_record_id`` - stable identity. SHA-256 over ``schema_version`` and the
  normalized member number. Stable across spreadsheet row moves and non-identity
  edits, one-way, exposes no member value.
* ``source_fingerprint`` - change detection. SHA-256 over all approved canonical
  fields. Any approved-field change invalidates a prior approval.
* ``operation_id`` - authoritative uniqueness. UUIDv4 (128 crypto-random bits).
  FNV and other non-cryptographic hashes are never used for authoritative values.
* ``payload_hash`` - integrity and change detection that also binds the embedded
  approval to the exact package content. It is NOT tamper-proof authentication:
  there is no secret or signature, so it detects accidental drift and re-pairing,
  not a determined forger.

Canonical serialization (must be reproduced identically by the PowerShell runner
so both sides recompute the same ``payload_hash``):

* ``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)``.
* Object keys sorted by Unicode code point; no insignificant whitespace; all
  non-ASCII and control characters escaped as ``\\uXXXX`` (or the short forms
  ``\\b \\t \\n \\f \\r``); forward slash is not escaped; the package contains only
  strings and non-negative integers, so number formatting is unambiguous.
"""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

SCHEMA_VERSION = "member_create_uat_package/v1"
BUSINESS_CONFIRMATION_SCHEMA_VERSION = "member_create_uat_business_confirmation/v1"

# The only decision state that may ever be approved for creation.
READY_FOR_CREATE_REVIEW = "READY_FOR_CREATE_REVIEW"

# Exact whitelist of fields the runner may assign. ExpiryDate is intentionally
# excluded: it has no proven AutoCount assignment/persistence path.
ASSIGNABLE_FIELDS = (
    "MemberNo",
    "Name",
    "EmailAddress",
    "MobilePhone",
    "DOB",
    "MemberType",
    "RegisterDate",
    "OpeningPoints",
)

# Business-desired fields recorded for audit; ExpiryDate is desired-only.
DESIRED_BUSINESS_FIELDS = ("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints")

# Fields whose business confirmation must be recorded before any real write.
BUSINESS_CONFIRMATION_REQUIRED = ("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints")

# Reviewer approval time-to-live. Defined here (and documented in the runbook)
# rather than hidden inside CLI code, so the expiry policy is explicit and
# reviewable. An operator may shorten it per run but never silently extend it.
DEFAULT_APPROVAL_TTL_HOURS = 72

# Intended business-desired values for this bounded UAT. These are recorded intent
# only; each remains gated by config/member_create_uat_business_confirmation.json,
# and ExpiryDate is never assigned to AutoCount until its behaviour is proven.
INTENDED_BUSINESS_VALUES = {
    "MemberType": "Default",
    "RegisterDate": "2026-07-01",
    "ExpiryDate": "2028-06-30",
    "OpeningPoints": 0,
}

# Fields that must never enter the actual AutoCount assignment payload until their
# assignment and persistence behaviour is proven, regardless of confirmation state.
NEVER_ASSIGN_FIELDS = ("ExpiryDate",)

# Controlled terminal result vocabulary. Every runner outcome is exactly one of
# these. Documented in the runbook.
TERMINAL_CODES = frozenset(
    {
        "DRY_RUN_VALIDATED",
        "BLOCKED_MEMBER_EXISTS",
        "APPROVAL_INVALID",
        "SOURCE_FINGERPRINT_MISMATCH",
        "CREATED_VERIFIED",
        "CREATED_READBACK_MISMATCH",
        "FAILED_BEFORE_WRITE",
        "WRITE_OUTCOME_UNCERTAIN",
        "OPERATOR_CONFIG_REQUIRED",
        "WRITE_NOT_CONFIRMED",
        "APPROVAL_REJECTED",
        "APPROVAL_ON_HOLD",
        "APPROVAL_ALREADY_CONSUMED",
        "PACKAGE_ALREADY_CONSUMED",
        "EXECUTION_LOCKED",
    }
)

SAFE_TIMESTAMP_RE = re.compile(r"^[0-9T:+.Z-]{1,64}$")
REVIEWER_ID_RE = re.compile(r"^[a-z0-9_-]{2,32}$")
OPERATION_ID_RE = re.compile(r"^mcuat_[0-9a-f]{32}$")
APPROVAL_ID_RE = re.compile(r"^appr_[0-9a-f]{32}$")
SOURCE_RECORD_ID_RE = re.compile(r"^srcrec_[0-9a-f]{64}$")
SOURCE_FINGERPRINT_RE = re.compile(r"^fp_[0-9a-f]{64}$")
PAYLOAD_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MEMBER_NO_RE = re.compile(r"^[0-9A-Za-z]{1,20}$")
DOB_SENTINEL_RE = re.compile(r"^2000-(0[1-9]|1[0-2])-01$")
DATE_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
SAFE_BASENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ContractError(ValueError):
    """Raised for local contract/input failures."""


# --------------------------------------------------------------------------- #
# Canonicalization and hashing
# --------------------------------------------------------------------------- #
def canonical_json(obj):
    """Deterministic canonical JSON, reproduced identically by the PS runner."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_record_id(normalized_member_no):
    """Stable one-way source identity derived from the member identity only."""
    if not isinstance(normalized_member_no, str) or not MEMBER_NO_RE.fullmatch(normalized_member_no):
        raise ContractError("A canonical member number is required for the source record id.")
    digest = sha256_hex(f"{SCHEMA_VERSION}|{normalized_member_no}")
    return f"srcrec_{digest}"


def source_fingerprint(canonical_fields):
    """Change-detection fingerprint over all approved canonical fields."""
    serialized = canonical_json(canonical_fields)
    return f"fp_{sha256_hex(serialized)}"


def build_fingerprint_fields(member_payload, desired_business_fields):
    """The exact field set covered by source_fingerprint (order-independent)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "MemberNo": member_payload["MemberNo"],
        "Name": member_payload["Name"],
        "EmailAddress": member_payload["EmailAddress"],
        "DOB": member_payload["DOB"],
        "MemberType": desired_business_fields["MemberType"],
        "RegisterDate": desired_business_fields["RegisterDate"],
        "ExpiryDate": desired_business_fields["ExpiryDate"],
        "OpeningPoints": desired_business_fields["OpeningPoints"],
    }


def compute_payload_hash(package_without_hash):
    """SHA-256 over canonical JSON of the package, excluding the two derived hash
    mirrors so their presence cannot make the hash circular.

    Excluded from the hashed body: the top-level ``payload_hash`` and
    ``approval.bound_package_payload_hash``. Everything else in the approval block
    (reviewer, timestamps, fingerprints, decision) remains covered, so integrity
    still binds the approval to the exact package content.
    """
    body = {k: v for k, v in package_without_hash.items() if k != "payload_hash"}
    approval = body.get("approval")
    if isinstance(approval, dict):
        body["approval"] = {k: v for k, v in approval.items() if k != "bound_package_payload_hash"}
    return f"sha256:{sha256_hex(canonical_json(body))}"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def mask_member_no(value):
    """Mask a member number for safe aggregate output: first two, last one."""
    text = "" if value is None else str(value)
    if len(text) <= 2:
        return "***"
    return text[:2] + "***" + text[-1:]


def redact(text, secrets=()):
    """Redact known secret substrings from a message; best-effort, never raises."""
    result = "" if text is None else str(text)
    for secret in secrets:
        if secret:
            result = result.replace(str(secret), "<redacted>")
    return result


# --------------------------------------------------------------------------- #
# Path safety (independent reimplementation; does NOT import the shared-folder
# handoff module, so this PR carries no dependency on its open POSIX findings).
# --------------------------------------------------------------------------- #
def is_reparse_point(path):
    """True if the final path entry is a reparse point / symlink (never followed)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(st, "st_file_attributes", 0)
    return bool(file_attributes & reparse)


def assert_safe_local_path(path, *, must_exist=False):
    """Validate a local package path fail-closed. Never prints the path."""
    text = str(path)
    if not text:
        raise ContractError("A package path is required.")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise ContractError("The package path must be absolute.")
    parts = candidate.parts[1:] if candidate.drive or candidate.root else candidate.parts
    if any(part in ("..", ".") for part in parts):
        raise ContractError("The package path must not contain '.' or '..' components.")
    if not SAFE_BASENAME_RE.fullmatch(candidate.name):
        raise ContractError("The package file name uses unsupported characters.")
    if is_reparse_point(candidate):
        raise ContractError("The package path is a reparse point and is rejected.")
    if must_exist and not candidate.is_file():
        raise ContractError("The package file was not found.")
    return candidate


# --------------------------------------------------------------------------- #
# Deterministic package validation (mirrors the JSON Schema exactly)
# --------------------------------------------------------------------------- #
def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_member_payload(payload, reasons):
    if not isinstance(payload, dict):
        reasons.append("member_payload_not_object")
        return
    if set(payload) != set(ASSIGNABLE_FIELDS):
        reasons.append("member_payload_field_set_mismatch")
        return
    if not (isinstance(payload["MemberNo"], str) and MEMBER_NO_RE.fullmatch(payload["MemberNo"])):
        reasons.append("member_no_invalid")
    if not (isinstance(payload["Name"], str) and 1 <= len(payload["Name"]) <= 100):
        reasons.append("name_invalid")
    if not (isinstance(payload["EmailAddress"], str) and 3 <= len(payload["EmailAddress"]) <= 200):
        reasons.append("email_invalid")
    if payload["MobilePhone"] != "":
        reasons.append("mobile_phone_not_blank")
    if not (isinstance(payload["DOB"], str) and DOB_SENTINEL_RE.fullmatch(payload["DOB"])):
        reasons.append("dob_invalid")
    if not (isinstance(payload["MemberType"], str) and 1 <= len(payload["MemberType"]) <= 20):
        reasons.append("member_type_invalid")
    if not (isinstance(payload["RegisterDate"], str) and DATE_RE.fullmatch(payload["RegisterDate"])):
        reasons.append("register_date_invalid")
    if payload["OpeningPoints"] != 0 or not _is_int(payload["OpeningPoints"]):
        reasons.append("opening_points_not_zero")
    for forbidden in NEVER_ASSIGN_FIELDS:
        if forbidden in payload:
            reasons.append("forbidden_assignable_field")


def _validate_desired_business(desired, reasons):
    if not isinstance(desired, dict) or set(desired) != set(DESIRED_BUSINESS_FIELDS):
        reasons.append("desired_business_fields_mismatch")
        return
    if not (isinstance(desired["MemberType"], str) and 1 <= len(desired["MemberType"]) <= 20):
        reasons.append("desired_member_type_invalid")
    if not (isinstance(desired["RegisterDate"], str) and DATE_RE.fullmatch(desired["RegisterDate"])):
        reasons.append("desired_register_date_invalid")
    if not (isinstance(desired["ExpiryDate"], str) and DATE_RE.fullmatch(desired["ExpiryDate"])):
        reasons.append("desired_expiry_date_invalid")
    if desired["OpeningPoints"] != 0 or not _is_int(desired["OpeningPoints"]):
        reasons.append("desired_opening_points_not_zero")


def _validate_approval_block(package, reasons):
    approval = package.get("approval")
    if not isinstance(approval, dict):
        reasons.append("approval_not_object")
        return
    expected = {
        "approval_id",
        "reviewer_id",
        "decision",
        "approved_at",
        "expires_at",
        "source_record_id",
        "source_fingerprint",
        "bound_package_payload_hash",
    }
    if set(approval) != expected:
        reasons.append("approval_field_set_mismatch")
        return
    if not (isinstance(approval["approval_id"], str) and APPROVAL_ID_RE.fullmatch(approval["approval_id"])):
        reasons.append("approval_id_invalid")
    if not (isinstance(approval["reviewer_id"], str) and REVIEWER_ID_RE.fullmatch(approval["reviewer_id"])):
        reasons.append("reviewer_id_invalid")
    if approval["decision"] != "approved":
        reasons.append("approval_decision_not_approved")
    for key in ("approved_at", "expires_at"):
        if not (isinstance(approval[key], str) and SAFE_TIMESTAMP_RE.fullmatch(approval[key])):
            reasons.append(f"approval_{key}_invalid")
    if approval.get("source_record_id") != package.get("source_record_id"):
        reasons.append("approval_source_record_id_mismatch")
    if approval.get("source_fingerprint") != package.get("source_fingerprint"):
        reasons.append("approval_source_fingerprint_mismatch")
    if not (
        isinstance(approval["bound_package_payload_hash"], str)
        and PAYLOAD_HASH_RE.fullmatch(approval["bound_package_payload_hash"])
    ):
        reasons.append("bound_package_payload_hash_invalid")


def validate_package(package):
    """Return (ok, reasons). Deterministic; mirrors the language-neutral schema.

    This is the same structural contract the PowerShell runner enforces in-process.
    It validates shape, types, the exactly-one-record payload, the field whitelist,
    the embedded approval, internal identity consistency, and payload_hash
    integrity. It does NOT evaluate approval expiry, single-use, or the business
    gate (those depend on wall-clock and VM-owned state).
    """
    reasons = []
    if not isinstance(package, dict):
        return False, ["package_not_object"]

    expected_top = {
        "schema_version",
        "operation_id",
        "source_record_id",
        "source_fingerprint",
        "row_number_hint",
        "created_at",
        "approval",
        "assignable_fields",
        "member_payload",
        "desired_business_fields",
        "business_confirmation_required",
        "payload_hash",
    }
    if set(package) != expected_top:
        return False, ["top_level_field_set_mismatch"]

    if package["schema_version"] != SCHEMA_VERSION:
        reasons.append("schema_version_mismatch")
    if not (isinstance(package["operation_id"], str) and OPERATION_ID_RE.fullmatch(package["operation_id"])):
        reasons.append("operation_id_invalid")
    if not (
        isinstance(package["source_record_id"], str)
        and SOURCE_RECORD_ID_RE.fullmatch(package["source_record_id"])
    ):
        reasons.append("source_record_id_invalid")
    if not (
        isinstance(package["source_fingerprint"], str)
        and SOURCE_FINGERPRINT_RE.fullmatch(package["source_fingerprint"])
    ):
        reasons.append("source_fingerprint_invalid")
    if not (_is_int(package["row_number_hint"]) and package["row_number_hint"] >= 2):
        reasons.append("row_number_hint_invalid")
    if not (isinstance(package["created_at"], str) and SAFE_TIMESTAMP_RE.fullmatch(package["created_at"])):
        reasons.append("created_at_invalid")
    if list(package["assignable_fields"]) != list(ASSIGNABLE_FIELDS):
        reasons.append("assignable_fields_mismatch")
    if sorted(package["business_confirmation_required"]) != sorted(BUSINESS_CONFIRMATION_REQUIRED):
        reasons.append("business_confirmation_required_mismatch")

    _validate_member_payload(package.get("member_payload"), reasons)
    _validate_desired_business(package.get("desired_business_fields"), reasons)
    _validate_approval_block(package, reasons)

    # payload_hash integrity: recompute over the canonical body.
    if not (isinstance(package["payload_hash"], str) and PAYLOAD_HASH_RE.fullmatch(package["payload_hash"])):
        reasons.append("payload_hash_format_invalid")
    else:
        recomputed = compute_payload_hash(package)
        if recomputed != package["payload_hash"]:
            reasons.append("payload_hash_integrity_failed")
        approval = package.get("approval")
        if isinstance(approval, dict) and approval.get("bound_package_payload_hash") != package["payload_hash"]:
            reasons.append("approval_binding_mismatch")

    # Cross-field identity consistency: member_payload vs desired business values.
    payload = package.get("member_payload")
    desired = package.get("desired_business_fields")
    if isinstance(payload, dict) and isinstance(desired, dict):
        for field in ("MemberType", "RegisterDate", "OpeningPoints"):
            if field in payload and field in desired and payload[field] != desired[field]:
                reasons.append("assignable_desired_value_mismatch")
                break
        # Recompute source_fingerprint and confirm it matches the stored value.
        try:
            fp = source_fingerprint(build_fingerprint_fields(payload, desired))
            if fp != package.get("source_fingerprint"):
                reasons.append("source_fingerprint_recompute_mismatch")
        except (KeyError, ContractError):
            reasons.append("source_fingerprint_recompute_error")

    return (len(reasons) == 0), reasons


# --------------------------------------------------------------------------- #
# Business confirmation gate
# --------------------------------------------------------------------------- #
def load_business_confirmation(path):
    """Load the committed business-confirmation config. Fail closed on any error."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ContractError("The business confirmation config could not be read.") from error
    if not isinstance(data, dict) or data.get("schema_version") != BUSINESS_CONFIRMATION_SCHEMA_VERSION:
        raise ContractError("The business confirmation config schema version is unexpected.")
    confirmations = data.get("confirmations")
    if not isinstance(confirmations, dict):
        raise ContractError("The business confirmation config is missing confirmations.")
    return confirmations


def business_fields_confirmed(confirmations):
    """Return (all_confirmed, unconfirmed_fields). Any missing/false is unconfirmed."""
    unconfirmed = []
    for field in BUSINESS_CONFIRMATION_REQUIRED:
        entry = confirmations.get(field)
        if not isinstance(entry, dict) or entry.get("confirmed") is not True:
            unconfirmed.append(field)
    return (len(unconfirmed) == 0), unconfirmed
