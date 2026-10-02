from __future__ import annotations

from pathlib import Path
from typing import Any

from energygrid_bill_downloader.invoice import Candidate, DATE_PROFILE_ISO_V1, InventorySnapshot, Stream
from energygrid_bill_downloader.config import BOUND_ADMISSION, UNBOUND_ADMISSION, DualStreamEntry


SYNTHETIC_NAMESPACE = "SYNTHETIC-ENERGYGRID-NAMESPACE"
SYNTHETIC_EVIDENCE = "EG_SYNTHETIC_SOURCE_CONTRACT"


def candidate(
    stream: Stream = Stream.EB_BILL,
    *,
    name: str = "invoice-001.pdf",
    invoice_date: str = "2026-10-01",
    namespace: str = SYNTHETIC_NAMESPACE,
    handle: Any = None,
) -> Candidate:
    return Candidate.create(
        stream=stream,
        source_namespace=namespace,
        source_filename=name,
        raw_date=invoice_date,
        date_profile=DATE_PROFILE_ISO_V1,
        evidence_ref=SYNTHETIC_EVIDENCE,
        fetch_handle=handle if handle is not None else {"synthetic": True},
    )


def snapshot(stream: Stream, candidates: tuple[Candidate, ...]) -> InventorySnapshot:
    namespace = candidates[0].source_namespace if candidates else SYNTHETIC_NAMESPACE
    return InventorySnapshot(
        stream=stream,
        source_namespace=namespace,
        date_profile=DATE_PROFILE_ISO_V1,
        completeness_witness="EG_SYNTHETIC_COMPLETE_SNAPSHOT",
        candidates=candidates,
    )


class SyntheticAdapter:
    def __init__(self, inventory: InventorySnapshot, files: dict[str, bytes] | None = None) -> None:
        self._inventory = inventory
        self._files = dict(files or {})
        self.inventory_calls = 0
        self.acquire_calls: list[str] = []

    def inventory(self, safety_ceiling: int) -> InventorySnapshot:
        self.inventory_calls += 1
        if len(self._inventory.candidates) > safety_ceiling:
            from energygrid_bill_downloader.errors import SourceContractError

            raise SourceContractError("EG_INVENTORY_CEILING")
        return self._inventory

    def acquire(self, item: Candidate, destination: Path) -> str:
        self.acquire_calls.append(item.source_filename)
        try:
            payload = self._files[item.source_filename]
        except KeyError:
            from energygrid_bill_downloader.errors import SourceContractError

            raise SourceContractError("EG_SYNTHETIC_ARTIFACT_MISSING") from None
        destination.write_bytes(payload)
        return item.source_filename


def test_stream_entries(*, bound: tuple[Stream, ...] = (Stream.EB_BILL, Stream.TENANT_BILL)) -> dict[str, DualStreamEntry]:
    result: dict[str, DualStreamEntry] = {}
    for stream in Stream:
        if stream in bound:
            result[stream.value] = DualStreamEntry(
                admission=BOUND_ADMISSION,
                source_namespace=SYNTHETIC_NAMESPACE,
                adapter_id="DIRECT_HTTP_V1",
                date_profile=DATE_PROFILE_ISO_V1,
                evidence_ref=SYNTHETIC_EVIDENCE,
            )
        else:
            result[stream.value] = DualStreamEntry(admission=UNBOUND_ADMISSION)
    return result


def create_v2_database(path: Path, *, bound: tuple[Stream, ...] = (Stream.EB_BILL, Stream.TENANT_BILL)) -> None:
    from energygrid_bill_downloader.state import migrate_state_database

    migrate_state_database(path, apply=True, streams=test_stream_entries(bound=bound))
