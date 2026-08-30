"""Deterministic, race-safe MemberNo allocation before the dispatch fence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .models import ProbeStatus


class AllocationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AllocationExhausted(AllocationError):
    pass


class AllocationAmbiguous(AllocationError):
    pass


class AllocationRace(AllocationError):
    pass


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    member_no: str
    probe_reference: str
    reused: bool


_PHONE_RE = re.compile(r"^65[89][0-9]{7}$")
_CANDIDATE_RE = re.compile(r"^65[89][0-9]{7}(?:X[1-9][0-9]*)?$")


class MemberNoAllocator:
    """Progresses only base -> X1 -> X2 and never truncates or wraps."""

    def __init__(self, max_length: int):
        if not isinstance(max_length, int) or isinstance(max_length, bool) or not 10 <= max_length <= 20:
            raise AllocationError("member_no_max_length_invalid")
        self.max_length = max_length

    def candidates(self, base_phone: str):
        if not _PHONE_RE.fullmatch(base_phone):
            raise AllocationError("canonical_phone_required")
        yield base_phone
        index = 1
        while True:
            candidate = f"{base_phone}X{index}"
            if len(candidate) > self.max_length:
                return
            yield candidate
            index += 1

    def validate_candidate(self, base_phone: str, candidate: str) -> bool:
        if not _CANDIDATE_RE.fullmatch(candidate):
            return False
        if candidate == base_phone:
            return True
        if not candidate.startswith(f"{base_phone}X"):
            return False
        suffix = candidate[len(base_phone) + 1 :]
        return suffix.isdigit() and not suffix.startswith("0") and int(suffix) >= 1 and len(candidate) <= self.max_length

    def allocate(
        self,
        *,
        base_phone: str,
        probe: Callable[[str], ProbeStatus],
        bind: Callable[[str, str], None],
        existing_member_no: str | None = None,
    ) -> AllocationDecision:
        if existing_member_no is not None:
            if not self.validate_candidate(base_phone, existing_member_no):
                raise AllocationError("existing_allocation_invalid")
            return AllocationDecision(existing_member_no, "existing-bound-allocation", True)

        for candidate in self.candidates(base_phone):
            try:
                status = ProbeStatus(probe(candidate))
            except ValueError as exc:
                raise AllocationAmbiguous("probe_status_invalid") from exc
            if status == ProbeStatus.OCCUPIED:
                continue
            if status != ProbeStatus.FREE:
                raise AllocationAmbiguous("member_no_probe_not_positive")
            probe_reference = f"probe-{candidate}-{len(candidate)}"
            try:
                bind(candidate, probe_reference)
            except AllocationRace:
                # A positive-free candidate was lost to a concurrent binder;
                # the next deterministic candidate is the only safe advance.
                continue
            return AllocationDecision(candidate, probe_reference, False)
        raise AllocationExhausted("member_no_suffix_exhausted")
