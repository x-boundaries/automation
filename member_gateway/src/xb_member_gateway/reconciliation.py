"""Read-only reconciliation after an uncertain AutoCount dispatch."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from .models import AllocationRecord, DispatchFenceRecord, JobRecord, JobState, ResultRecord, ResultStatus
from .results import build_member_record, compare_readback, make_result


class ReconciliationAdapter(Protocol):
    def get_member(self, member_no: str) -> Mapping[str, Any] | None:
        ...


def reconcile_uncertain_write(
    *,
    job: JobRecord,
    allocation: AllocationRecord,
    fence: DispatchFenceRecord,
    adapter: ReconciliationAdapter,
    repository: Any,
    worker_id: str | None = None,
    now: datetime | None = None,
) -> ResultRecord:
    """Use only the already bound MemberNo; this function never calls SaveMember."""

    if job.state != JobState.WRITE_OUTCOME_UNCERTAIN:
        raise ValueError("reconciliation_requires_uncertain_state")
    if allocation.member_no != fence.member_no or job.allocation_member_no != fence.member_no:
        raise ValueError("reconciliation_member_binding_invalid")
    case = repository.open_reconciliation_case(job.job_id, fence.member_no, now=now)
    try:
        actual = adapter.get_member(fence.member_no)
    except Exception:
        result = make_result(
            job=job,
            fence=fence,
            status=ResultStatus.WRITE_OUTCOME_UNCERTAIN,
            save_invocation_count=1,
            readback_found=False,
            readback_match=False,
            error_code="reconciliation_lookup_uncertain",
            acknowledged_at=now,
        )
        repository.record_reconciliation_check(case.case_id, "ambiguous", False, False, now=now)
        repository.acknowledge_result(
            result,
            worker_id=worker_id,
            require_lease=False,
            reconciliation_case_id=case.case_id,
            now=now,
        )
        return result

    expected = build_member_record(job, fence.member_no)
    if actual is None:
        # Positive absence does not authorize a second create.
        lookup_status = "absent"
        result = make_result(
            job=job,
            fence=fence,
            status=ResultStatus.CONFIRMED_NOT_CREATED,
            save_invocation_count=1,
            readback_found=False,
            readback_match=False,
            error_code="confirmed_absent_manual_followup",
            acknowledged_at=now,
        )
    else:
        check = compare_readback(expected, actual)
        if check.match:
            lookup_status = "exact_match"
            result = make_result(
                job=job,
                fence=fence,
                status=ResultStatus.CREATED_VERIFIED,
                save_invocation_count=1,
                readback_found=True,
                readback_match=True,
                acknowledged_at=now,
            )
        else:
            result = make_result(
                job=job,
                fence=fence,
                status=ResultStatus.CREATED_READBACK_MISMATCH,
                save_invocation_count=1,
                readback_found=True,
                readback_match=False,
                error_code="readback_mismatch_manual_review",
                acknowledged_at=now,
            )
            lookup_status = "mismatch"
    repository.record_reconciliation_check(
        case.case_id,
        lookup_status,
        result.readback_found,
        result.readback_match,
        now=now,
    )
    repository.acknowledge_result(
        result,
        worker_id=worker_id,
        require_lease=False,
        reconciliation_case_id=case.case_id,
        now=now,
    )
    return result
