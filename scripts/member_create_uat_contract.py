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

# Bumped v1 -> v2 in the "enable proven ExpiryDate" change: the package payload shape
# changed (member_payload and assignable_fields now include ExpiryDate), so a v1 package
# built under the previous contract is a different shape and must be refused fail-closed.
# The runner rejects an unrecognised schema_version, so an old v1 package can never be
# silently executed under this contract.
SCHEMA_VERSION = "member_create_uat_package/v2"
BUSINESS_CONFIRMATION_SCHEMA_VERSION = "member_create_uat_business_confirmation/v1"

# The only decision state that may ever be approved for creation.
READY_FOR_CREATE_REVIEW = "READY_FOR_CREATE_REVIEW"

# Exact whitelist of fields the runner may assign. ExpiryDate is now an ACTIVE
# assignable field: its AutoCount assignment and persistence were proven by the
# synthetic ExpiryDate capability probe (durable result SHA-256
# 48CC0185EFF59C3A21AC087BC0C120A00B70801599C3F5513675F7950CD1541B), so it moved out of
# NEVER_ASSIGN_FIELDS into this whitelist and the immutable package payload.
ASSIGNABLE_FIELDS = (
    "MemberNo",
    "Name",
    "EmailAddress",
    "MobilePhone",
    "DOB",
    "MemberType",
    "RegisterDate",
    "ExpiryDate",
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
# ExpiryDate has now been proven (synthetic capability probe) and promoted into
# ASSIGNABLE_FIELDS, so this set is empty. It is retained as an explicit, greppable
# invariant surface rather than being deleted.
NEVER_ASSIGN_FIELDS = ()

# --------------------------------------------------------------------------- #
# Active ExpiryDate assignment/read-back contract.
#
# This change PROMOTES the ExpiryDate assignment and persistence capability from the
# prepared (inactive) state that PR #112 shipped into the ACTIVE contract. ExpiryDate is
# now in ASSIGNABLE_FIELDS and the immutable package payload, the code-level capability
# gate is flipped True, and the runner assigns and reads back ExpiryDate. Every other
# fail-closed gate (five write switches, business confirmation, single-use markers,
# exclusive lock, one SaveMember, honest uncertain classification) is unchanged, so no
# live write occurs in development, tests, or CI, and a real write still requires the
# separate explicit operator step on the VM.
#
# EXPIRYDATE_ASSIGNMENT_IMPLEMENTED mirrors the PowerShell
# $script:CreateUatExpiryDateAssignmentImplemented capability flag. Both are True only
# because the synthetic ExpiryDate capability probe proved ExpiryDate persists
# (terminal_outcome=EXPIRY_VERIFIED; durable result SHA-256
# 48CC0185EFF59C3A21AC087BC0C120A00B70801599C3F5513675F7950CD1541B).
EXPIRYDATE_ASSIGNMENT_IMPLEMENTED = True

# The exact intended ExpiryDate for this bounded UAT. Named explicitly (not only
# nested in INTENDED_BUSINESS_VALUES) so the intended value is greppable and testable.
EXPIRYDATE_INTENDED_VALUE = "2028-06-30"

# The intended AutoCount assignment contract: the full field set the runner is INTENDED
# to assign. Now that ExpiryDate is proven and active, ExpiryDate is a member of both the
# intended set and ASSIGNABLE_FIELDS. The invariant
# INTENDED_ASSIGNMENT_FIELDS == ASSIGNABLE_FIELDS + NEVER_ASSIGN_FIELDS (with an empty
# never-assign set) keeps the active/intended relationship explicit.
INTENDED_ASSIGNMENT_FIELDS = ASSIGNABLE_FIELDS + NEVER_ASSIGN_FIELDS

# The normalised read-back verification contract: every field whose persisted value
# must be read back and compared after a real SaveMember. ExpiryDate is included so a
# read-back ExpiryDate mismatch can never yield CREATED_VERIFIED. The runner reads back
# exactly the fields it assigns, which now includes ExpiryDate.
READBACK_VERIFICATION_FIELDS = INTENDED_ASSIGNMENT_FIELDS

# Fields the runner assigns for member activation state that are runner-managed and are
# NOT part of the reviewer-approved package payload/whitelist. Declared here so the
# expected assigned-field count is derived from the real assignment collection rather
# than a hard-coded literal.
RUNNER_ACTIVATION_FIELDS = ("IsActive", "Individual")

# The exact number of fields the runner assigns to the new member row: every actively
# assignable field plus the runner-managed activation fields. With ExpiryDate active
# this is len(9) + len(2) = 11. A sanitized result whose assigned_field_count does not
# equal this (once a save was attempted) is treated as a stale-code contradiction.
EXPECTED_ASSIGNED_FIELD_COUNT = len(ASSIGNABLE_FIELDS) + len(RUNNER_ACTIVATION_FIELDS)

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

RECOVERY_STATES = frozenset(
    {"none", "terminal_exists", "consumed_no_terminal", "intent_no_consumed", "malformed"}
)
SAVE_OUTCOMES = frozenset({"not_attempted", "confirmed", "uncertain"})

# The single canonical set of result flags the terminal-state table reads. Kept here
# so the PowerShell runner, this module (result precheck), and the n8n validation code
# all agree on the exact relationship between state and terminal_code.
TERMINAL_STATE_FLAGS = (
    "mode",
    "package_fingerprint_problem",
    "package_structural_valid",
    "approval_not_expired",
    "write_confirmed",
    "business_confirmed",
    "lock_acquired",
    "recovery_state",
    "execution_error",
    "member_exists_initial",
    "member_exists_recheck",
    "save_member_attempted",
    "save_member_confirmed",
    "save_outcome",
    "readback_found",
    "readback_match",
    "expiry_date_assigned",
    "assigned_field_count",
)


def terminal_state_contradictions(flags):
    """Return a list of impossible-flag reasons; empty means internally consistent.

    A recognised terminal code paired with a physically impossible flag combination
    is rejected here (finding 4), independent of the recomputed code.
    """
    reasons = []
    mode = flags.get("mode")
    write = mode == "write"
    attempted = bool(flags.get("save_member_attempted"))
    confirmed = bool(flags.get("save_member_confirmed"))
    outcome = flags.get("save_outcome")
    rb_found = bool(flags.get("readback_found"))
    rb_match = bool(flags.get("readback_match"))
    expiry_assigned = bool(flags.get("expiry_date_assigned"))
    assigned_count = flags.get("assigned_field_count")

    if mode not in ("dry-run", "write"):
        reasons.append("mode_invalid")
    if flags.get("recovery_state") not in RECOVERY_STATES:
        reasons.append("recovery_state_invalid")
    if outcome not in SAVE_OUTCOMES:
        reasons.append("save_outcome_invalid")
    if confirmed and not attempted:
        reasons.append("confirmed_without_attempt")
    if outcome == "confirmed" and not confirmed:
        reasons.append("outcome_confirmed_without_confirmed_flag")
    if outcome == "not_attempted" and attempted:
        reasons.append("not_attempted_but_attempted")
    if outcome == "uncertain" and not attempted:
        reasons.append("uncertain_without_attempt")
    if rb_match and not rb_found:
        reasons.append("match_without_found")
    if (rb_found or rb_match) and outcome != "confirmed":
        reasons.append("readback_without_confirmed_save")
    if attempted and not write:
        reasons.append("attempt_in_non_write_mode")
    if attempted and not flags.get("lock_acquired"):
        reasons.append("attempt_without_lock")
    # ExpiryDate is an active assignable field, so any real save is preceded by an
    # ExpiryDate assignment and by the full expected field set. A save attempt (or a
    # matched read-back) without expiry_date_assigned, or with a stale assigned-field
    # count, is a contradiction: real-write success can never be accepted in that state,
    # so CREATED_VERIFIED is structurally impossible unless ExpiryDate was assigned and
    # the full field set (EXPECTED_ASSIGNED_FIELD_COUNT) was written.
    if (attempted or rb_match) and not expiry_assigned:
        reasons.append("expiry_date_not_assigned")
    if (attempted or rb_match) and assigned_count != EXPECTED_ASSIGNED_FIELD_COUNT:
        reasons.append("assigned_field_count_stale")
    if not write and (
        bool(flags.get("write_confirmed"))
        or bool(flags.get("member_exists_recheck"))
        or attempted
        or confirmed
        or outcome != "not_attempted"
    ):
        reasons.append("dry_run_has_write_state")
    return reasons


def recompute_terminal_state(flags):
    """Canonical terminal-state table. Returns (code, contradictions).

    The ordered rules mirror the runner's gate order exactly, so the runner can
    derive its own terminal_code from this function, the result precheck can
    recompute-and-compare, and the n8n validation code can apply the same table.
    """
    contradictions = terminal_state_contradictions(flags)
    write = flags.get("mode") == "write"
    recovery = flags.get("recovery_state", "none")
    outcome = flags.get("save_outcome", "not_attempted")

    if flags.get("package_fingerprint_problem"):
        code = "SOURCE_FINGERPRINT_MISMATCH"
    elif not flags.get("package_structural_valid"):
        code = "FAILED_BEFORE_WRITE"
    elif not flags.get("approval_not_expired"):
        code = "APPROVAL_INVALID"
    elif write and not flags.get("write_confirmed"):
        code = "WRITE_NOT_CONFIRMED"
    elif write and not flags.get("business_confirmed"):
        code = "OPERATOR_CONFIG_REQUIRED"
    elif not flags.get("lock_acquired"):
        code = "EXECUTION_LOCKED"
    elif recovery == "terminal_exists":
        code = "PACKAGE_ALREADY_CONSUMED"
    elif recovery == "consumed_no_terminal":
        code = "WRITE_OUTCOME_UNCERTAIN"
    elif recovery == "intent_no_consumed":
        code = "FAILED_BEFORE_WRITE"
    elif recovery == "malformed":
        code = "WRITE_OUTCOME_UNCERTAIN"
    elif flags.get("execution_error"):
        # An unhandled runtime error: uncertain if a save had begun, otherwise a
        # confirmed failure before any write.
        code = "WRITE_OUTCOME_UNCERTAIN" if flags.get("save_member_attempted") else "FAILED_BEFORE_WRITE"
    elif flags.get("member_exists_initial"):
        code = "BLOCKED_MEMBER_EXISTS"
    elif not write:
        code = "DRY_RUN_VALIDATED"
    elif flags.get("member_exists_recheck"):
        code = "BLOCKED_MEMBER_EXISTS"
    elif outcome == "uncertain":
        code = "WRITE_OUTCOME_UNCERTAIN"
    elif outcome == "not_attempted":
        code = "FAILED_BEFORE_WRITE"
    elif outcome == "confirmed":
        if not flags.get("readback_found"):
            code = "WRITE_OUTCOME_UNCERTAIN"
        elif flags.get("readback_match"):
            code = "CREATED_VERIFIED"
        else:
            code = "CREATED_READBACK_MISMATCH"
    else:
        code = "FAILED_BEFORE_WRITE"
    return code, contradictions


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
    if not (isinstance(payload["ExpiryDate"], str) and DATE_RE.fullmatch(payload["ExpiryDate"])):
        reasons.append("expiry_date_invalid")
    if payload["OpeningPoints"] != 0 or not _is_int(payload["OpeningPoints"]):
        reasons.append("opening_points_not_zero")
    for forbidden in NEVER_ASSIGN_FIELDS:
        if forbidden in payload:
            reasons.append("forbidden_assignable_field")
    # Intended business values must match the reviewed constants exactly.
    for field in ("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints"):
        if field in payload and payload[field] != INTENDED_BUSINESS_VALUES[field]:
            reasons.append(f"member_payload_{field}_not_intended")


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
    for field in DESIRED_BUSINESS_FIELDS:
        if field in desired and desired[field] != INTENDED_BUSINESS_VALUES[field]:
            reasons.append(f"desired_{field}_not_intended")


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
    # approved_at must strictly precede expires_at.
    try:
        from datetime import datetime as _dt

        if _dt.fromisoformat(approval["approved_at"]) >= _dt.fromisoformat(approval["expires_at"]):
            reasons.append("approval_expiry_not_after_approved_at")
    except (ValueError, TypeError, KeyError):
        reasons.append("approval_timestamp_unparseable")
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
        for field in ("MemberType", "RegisterDate", "ExpiryDate", "OpeningPoints"):
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
