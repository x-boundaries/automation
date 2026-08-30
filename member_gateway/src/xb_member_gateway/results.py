"""Member field assignment and exact read-back result contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .canonical import birthday_month_to_dob, canonical_json, derive_register_and_expiry
from .crypto import payload_hash
from .models import DispatchFenceRecord, JobRecord, MemberRecord, ReadbackCheck, ResultRecord, ResultStatus


ASSIGNED_FIELDS = (
    "MemberNo", "MemberType", "Name", "MobilePhone", "EmailAddress", "DOB",
    "RegisterDate", "ExpiryDate", "OpeningPoints", "IsActive", "Individual",
)
ADAPTER_MANAGED_FIELDS = ("IsActive", "Individual")


class ResultValidationError(ValueError):
    pass


def build_member_record(job: JobRecord, member_no: str, *, adapter_defaults: Mapping[str, Any] | None = None) -> MemberRecord:
    """Derive the only supported member.create record."""

    payload = job.member_payload
    try:
        register_date, expiry_date = derive_register_and_expiry(payload["create_time"])
        defaults = dict(adapter_defaults or {"IsActive": True, "Individual": True})
        if not isinstance(defaults.get("IsActive"), bool) or not isinstance(defaults.get("Individual"), bool):
            raise ResultValidationError("adapter_defaults_invalid")
        return MemberRecord(
            MemberNo=member_no, MemberType="Default", Name=payload["name"], MobilePhone=payload["phone"],
            EmailAddress=payload["email"], DOB=birthday_month_to_dob(payload["birthday_month"]),
            RegisterDate=register_date, ExpiryDate=expiry_date, OpeningPoints=0,
            IsActive=defaults["IsActive"], Individual=defaults["Individual"],
        )
    except KeyError as exc:
        raise ResultValidationError("member_payload_incomplete") from exc


def _mapping(value: MemberRecord | Mapping[str, Any]) -> Mapping[str, Any]:
    return value.to_dict() if isinstance(value, MemberRecord) else value


def compare_readback(expected: MemberRecord | Mapping[str, Any], actual: MemberRecord | Mapping[str, Any] | None) -> ReadbackCheck:
    if actual is None:
        return ReadbackCheck(False, ("record_absent",))
    expected_map, actual_map = _mapping(expected), _mapping(actual)
    mismatches = tuple(field for field in ASSIGNED_FIELDS if field not in actual_map or actual_map[field] != expected_map.get(field))
    return ReadbackCheck(not mismatches, mismatches)


def _validate_error_code(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_.:-" for c in value):
        raise ResultValidationError("error_code_invalid")
    return value


def make_result(*, job: JobRecord, fence: DispatchFenceRecord, status: ResultStatus, save_invocation_count: int, readback_found: bool, readback_match: bool, error_code: str | None = None, acknowledged_at: datetime | None = None) -> ResultRecord:
    if job.operation != "member.create" or fence.operation != "member.create" or job.allocation_member_no != fence.member_no:
        raise ResultValidationError("member_no_binding_invalid")
    if isinstance(save_invocation_count, bool) or save_invocation_count != 1:
        raise ResultValidationError("save_invocation_count_must_be_one")
    if not isinstance(readback_found, bool) or not isinstance(readback_match, bool):
        raise ResultValidationError("readback_flags_must_be_boolean")
    status = ResultStatus(status)
    if status == ResultStatus.CREATED_VERIFIED and not (readback_found and readback_match):
        raise ResultValidationError("verified_result_flags_invalid")
    if status == ResultStatus.WRITE_OUTCOME_UNCERTAIN and readback_match:
        raise ResultValidationError("uncertain_result_flags_invalid")
    if status == ResultStatus.CONFIRMED_NOT_CREATED and readback_found:
        raise ResultValidationError("confirmed_absence_flags_invalid")
    if status == ResultStatus.CREATED_READBACK_MISMATCH and (not readback_found or readback_match):
        raise ResultValidationError("mismatch_result_flags_invalid")
    safe_error = _validate_error_code(error_code)
    recorded = acknowledged_at or datetime.now(timezone.utc)
    if recorded.tzinfo is None:
        raise ResultValidationError("acknowledged_at_timezone_required")
    body = {
        "job_id": job.job_id, "operation": "member.create", "dispatch_fence_id": fence.fence_id,
        "status": status.value, "member_no": fence.member_no, "save_invocation_count": 1,
        "readback_found": readback_found, "readback_match": readback_match,
        "reconciliation_required": status != ResultStatus.CREATED_VERIFIED, "error_code": safe_error,
    }
    return ResultRecord(
        job_id=job.job_id, result_hash=payload_hash(canonical_json(body)), status=status,
        member_no=fence.member_no, dispatch_fence_id=fence.fence_id, save_invocation_count=1,
        readback_found=readback_found, readback_match=readback_match,
        reconciliation_required=status != ResultStatus.CREATED_VERIFIED, error_code=safe_error,
        acknowledged_at=recorded.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def result_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the worker acknowledgement body; no arbitrary operation is accepted."""

    expected = {"schema_version", "job_id", "operation", "dispatch_fence_id", "status", "member_no", "save_invocation_count", "readback_found", "readback_match", "error_code"}
    if set(value) != expected:
        raise ResultValidationError("result_fields_invalid")
    if value["schema_version"] != "xb.member.gateway.result.v1" or value["operation"] != "member.create":
        raise ResultValidationError("result_schema_or_operation_invalid")
    if not isinstance(value["job_id"], str) or not isinstance(value["dispatch_fence_id"], str) or not isinstance(value["member_no"], str):
        raise ResultValidationError("result_identity_invalid")
    if isinstance(value["save_invocation_count"], bool) or value["save_invocation_count"] != 1:
        raise ResultValidationError("save_invocation_count_must_be_one")
    if not isinstance(value["readback_found"], bool) or not isinstance(value["readback_match"], bool):
        raise ResultValidationError("result_flags_must_be_boolean")
    if value["error_code"] is not None and not isinstance(value["error_code"], str):
        raise ResultValidationError("error_code_invalid")
    try:
        status = ResultStatus(value["status"])
    except (TypeError, ValueError) as exc:
        raise ResultValidationError("result_status_invalid") from exc
    return {"job_id": value["job_id"], "operation": "member.create", "dispatch_fence_id": value["dispatch_fence_id"], "status": status, "member_no": value["member_no"], "save_invocation_count": 1, "readback_found": value["readback_found"], "readback_match": value["readback_match"], "error_code": value["error_code"]}
