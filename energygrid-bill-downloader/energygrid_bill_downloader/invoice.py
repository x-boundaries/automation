from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .errors import SourceContractError
from .http_source import DirectHttpSource, ListedBill
from .publication import WINDOWS_RESERVED_NAMES, filename_key


class Stream(StrEnum):
    EB_BILL = "EB_BILL"
    TENANT_BILL = "TENANT_BILL"


STREAM_LABELS = {
    Stream.EB_BILL: "EB Bill",
    Stream.TENANT_BILL: "Tenant Bill",
}
DATE_PROFILE_ISO_V1 = "INVOICE_DATE_ISO_V1"
DATE_PROFILES = frozenset({DATE_PROFILE_ISO_V1})
ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
EVIDENCE_REF_RE = re.compile(r"EG_[A-Z0-9_]{1,60}\Z", re.ASCII)


def _valid_source_filename(value: object) -> bool:
    if (
        type(value) is not str
        or not value
        or len(value) > 260
        or any(ord(ch) < 32 or ch in '<>:"/\\|?*' for ch in value)
        or value.endswith((".", " "))
        or not value.lower().endswith(".pdf")
    ):
        return False
    device_base = value.split(".", 1)[0].rstrip(" .").upper()
    return device_base not in WINDOWS_RESERVED_NAMES


def parse_invoice_date(value: str, profile: str) -> date:
    """Parse a closed, exact invoice-date grammar; never infer from a filename."""

    if profile not in DATE_PROFILES or type(value) is not str or not ISO_DATE_RE.fullmatch(value):
        raise SourceContractError("EG_INVOICE_DATE_INVALID")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise SourceContractError("EG_INVOICE_DATE_INVALID") from None
    if parsed.isoformat() != value:
        raise SourceContractError("EG_INVOICE_DATE_INVALID")
    return parsed


@dataclass(frozen=True, slots=True)
class Candidate:
    stream: Stream
    source_namespace: str = field(repr=False)
    source_invoice_key: str = field(repr=False)
    source_filename: str = field(repr=False)
    raw_date: str = field(repr=False)
    date_profile: str
    invoice_date: date
    day_ordinal: int
    canonical_filename: str
    evidence_ref: str
    fetch_handle: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.stream, Stream):
            raise SourceContractError("EG_INVOICE_STREAM_INVALID")
        if (
            type(self.source_namespace) is not str
            or not self.source_namespace
            or len(self.source_namespace) > 256
            or any(ord(ch) < 32 for ch in self.source_namespace)
        ):
            raise SourceContractError("EG_INVOICE_IDENTITY_INVALID")
        if not _valid_source_filename(self.source_filename):
            raise SourceContractError("EG_INVOICE_IDENTITY_INVALID")
        if filename_key(self.source_filename) != self.source_invoice_key:
            raise SourceContractError("EG_INVOICE_IDENTITY_INVALID")
        if type(self.evidence_ref) is not str or not EVIDENCE_REF_RE.fullmatch(self.evidence_ref):
            raise SourceContractError("EG_INVOICE_EVIDENCE_INVALID")
        parsed = parse_invoice_date(self.raw_date, self.date_profile)
        if (
            type(self.invoice_date) is not date
            or self.invoice_date != parsed
            or type(self.day_ordinal) is not int
            or self.day_ordinal != parsed.toordinal()
            or self.canonical_filename != f"{parsed.isoformat()}.pdf"
        ):
            raise SourceContractError("EG_INVOICE_DATE_INVALID")

    @classmethod
    def create(
        cls,
        *,
        stream: Stream,
        source_namespace: str,
        source_filename: str,
        raw_date: str,
        date_profile: str,
        evidence_ref: str,
        fetch_handle: Any,
    ) -> "Candidate":
        if not isinstance(stream, Stream):
            raise SourceContractError("EG_INVOICE_STREAM_INVALID")
        if (
            type(source_namespace) is not str
            or not source_namespace
            or len(source_namespace) > 256
            or any(ord(ch) < 32 for ch in source_namespace)
        ):
            raise SourceContractError("EG_INVOICE_IDENTITY_INVALID")
        if not _valid_source_filename(source_filename):
            raise SourceContractError("EG_INVOICE_IDENTITY_INVALID")
        if type(evidence_ref) is not str or not EVIDENCE_REF_RE.fullmatch(evidence_ref):
            raise SourceContractError("EG_INVOICE_EVIDENCE_INVALID")
        parsed = parse_invoice_date(raw_date, date_profile)
        key = filename_key(source_filename)
        if not key:
            raise SourceContractError("EG_INVOICE_IDENTITY_INVALID")
        return cls(
            stream=stream,
            source_namespace=source_namespace,
            source_invoice_key=key,
            source_filename=source_filename,
            raw_date=raw_date,
            date_profile=date_profile,
            invoice_date=parsed,
            day_ordinal=parsed.toordinal(),
            canonical_filename=f"{parsed.isoformat()}.pdf",
            evidence_ref=evidence_ref,
            fetch_handle=fetch_handle,
        )


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    stream: Stream
    source_namespace: str = field(repr=False)
    date_profile: str
    completeness_witness: str = field(repr=False)
    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stream, Stream) or self.date_profile not in DATE_PROFILES:
            raise SourceContractError("EG_INVENTORY_BINDING_INVALID")
        if type(self.source_namespace) is not str or not self.source_namespace:
            raise SourceContractError("EG_INVENTORY_BINDING_INVALID")
        if type(self.completeness_witness) is not str or not self.completeness_witness:
            raise SourceContractError("EG_INVENTORY_INCOMPLETE")
        if type(self.candidates) is not tuple:
            raise SourceContractError("EG_INVENTORY_SCHEMA_INVALID")
        seen: set[tuple[str, Stream, str]] = set()
        for candidate in self.candidates:
            if (
                not isinstance(candidate, Candidate)
                or candidate.stream is not self.stream
                or candidate.source_namespace != self.source_namespace
                or candidate.date_profile != self.date_profile
            ):
                raise SourceContractError("EG_INVENTORY_BINDING_INVALID")
            identity = (candidate.source_namespace, candidate.stream, candidate.source_invoice_key)
            if identity in seen:
                raise SourceContractError("EG_INVENTORY_DUPLICATE_IDENTITY")
            seen.add(identity)


class SourceAdapter(Protocol):
    def inventory(self, safety_ceiling: int) -> InventorySnapshot: ...

    def acquire(self, candidate: Candidate, destination: Path) -> Any: ...


class DirectHttpAdapter:
    """Adapt the already reviewed HTTP source without exposing its wire row."""

    def __init__(
        self,
        source: DirectHttpSource,
        *,
        stream: Stream,
        source_namespace: str,
        evidence_ref: str,
    ) -> None:
        self._source = source
        self._stream = stream
        self._namespace = source_namespace
        self._evidence_ref = evidence_ref
        self._snapshot_candidates: tuple[Candidate, ...] = ()

    def __repr__(self) -> str:
        return "DirectHttpAdapter()"

    def inventory(self, safety_ceiling: int) -> InventorySnapshot:
        # A failed or repeated inventory cannot leave handles from an earlier
        # observation authorised for a later acquire call.
        self._snapshot_candidates = ()
        rows = self._source.inventory(safety_ceiling)
        candidates = tuple(
            Candidate.create(
                stream=self._stream,
                source_namespace=self._namespace,
                source_filename=row.filename,
                raw_date=row.date,
                date_profile=DATE_PROFILE_ISO_V1,
                evidence_ref=self._evidence_ref,
                fetch_handle=row,
            )
            for row in rows
        )
        self._snapshot_candidates = candidates
        # DirectHttpSource validates the complete single response and rejects empty lists.
        return InventorySnapshot(
            stream=self._stream,
            source_namespace=self._namespace,
            date_profile=DATE_PROFILE_ISO_V1,
            completeness_witness="EG_HTTP_COMPLETE_LIST_V1",
            candidates=candidates,
        )

    def acquire(self, candidate: Candidate, destination: Path) -> str:
        if (
            not any(candidate is issued for issued in self._snapshot_candidates)
            or not isinstance(candidate.fetch_handle, ListedBill)
            or candidate.stream is not self._stream
            or candidate.source_namespace != self._namespace
            or not any(candidate.fetch_handle is issued.fetch_handle for issued in self._snapshot_candidates)
        ):
            raise SourceContractError("EG_FETCH_HANDLE_INVALID")
        return self._source.download(candidate.fetch_handle, destination)


class SyntheticSource:
    """Small injectable adapter fixture; never constructed by production config."""

    def __init__(self, snapshot: InventorySnapshot, files: dict[str, bytes]) -> None:
        self._snapshot = snapshot
        self._files = dict(files)
        self.acquire_count = 0

    def inventory(self, safety_ceiling: int) -> InventorySnapshot:
        if len(self._snapshot.candidates) > safety_ceiling:
            raise SourceContractError("EG_INVENTORY_CEILING")
        return self._snapshot

    def acquire(self, candidate: Candidate, destination: Path) -> str:
        self.acquire_count += 1
        source_name = candidate.source_filename
        try:
            payload = self._files[source_name]
        except KeyError:
            raise SourceContractError("EG_FETCH_ARTIFACT_MISSING") from None
        destination.write_bytes(payload)
        return source_name
