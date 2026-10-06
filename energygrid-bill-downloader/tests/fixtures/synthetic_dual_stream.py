from __future__ import annotations

from pathlib import Path
from typing import Any

from energygrid_bill_downloader.config import BOUND_ADMISSION, UNBOUND_ADMISSION, DualStreamEntry
from energygrid_bill_downloader.invoice import Candidate, DATE_PROFILE_ISO_V1, InventorySnapshot, Stream


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


# ---------------------------------------------------------------------------
# #226 G3: v3 fixtures. Synthetic only: no Google, n8n or SMTP contact.
# ---------------------------------------------------------------------------

SYNTHETIC_ACCOUNT = "01234567890123456789"
SYNTHETIC_ROOT = "synthRootFolder000001"
SYNTHETIC_FOLDERS = {"EB_BILL": "synthEbBillFolder00001", "TENANT_BILL": "synthTenantFolder00001"}


def create_v3_database(path: Path, *, bound: tuple[Stream, ...] = (Stream.EB_BILL, Stream.TENANT_BILL)) -> None:
    from energygrid_bill_downloader.state import migrate_state_database_v3

    migrate_state_database_v3(path, apply=True, streams=test_stream_entries(bound=bound))


def synthetic_drive_settings(*, streams: tuple[str, ...] = ("EB_BILL", "TENANT_BILL"),
                             url: str = "http://127.0.0.1:5678/webhook/SYNTHETIC-DRIVE"):
    from energygrid_bill_downloader.config import DRIVE_V3_MODE, DriveBinding, DriveV3Settings
    from energygrid_bill_downloader.state import drive_binding_id

    bindings = {}
    for name in ("EB_BILL", "TENANT_BILL"):
        if name not in streams:
            bindings[name] = None
            continue
        bindings[name] = DriveBinding(
            binding_id=drive_binding_id(name, SYNTHETIC_ACCOUNT, SYNTHETIC_ROOT, SYNTHETIC_FOLDERS[name]),
            account_ref=SYNTHETIC_ACCOUNT, root_folder_id=SYNTHETIC_ROOT, folder_id=SYNTHETIC_FOLDERS[name],
        )
    return DriveV3Settings(
        mode=DRIVE_V3_MODE, url=url, auth_header_name="X-Synthetic-Drive", auth_token_env="ENERGYGRID_DRIVE_TOKEN",
        timeout_seconds=5, bindings=bindings,
    )


def bind_synthetic_drive(path: Path, streams: tuple[str, ...] = ("EB_BILL", "TENANT_BILL")) -> None:
    from energygrid_bill_downloader.state import StateV3Store

    with StateV3Store(path) as state:
        for name in streams:
            state.insert_binding(
                stream=name, account_ref=SYNTHETIC_ACCOUNT, root_folder_id=SYNTHETIC_ROOT,
                folder_id=SYNTHETIC_FOLDERS[name], chain_sha256="0" * 64,
                run_id="00000000-0000-4000-8000-0000000000b1", timestamp="2026-10-06T00:00:00+00:00",
            )


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, bytes | None]:
    import json

    boundary = content_type.split("boundary=", 1)[1].encode("ascii")
    metadata = None
    pdf = None
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, payload = part.split(b"\r\n\r\n", 1)
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        if b'name="metadata"' in head:
            metadata = json.loads(payload.decode("utf-8"))
        elif b'name="pdf"' in head:
            pdf = payload
    return metadata, pdf


class FakeDriveService:
    """Python model of the n8n "EnergyGrid - Drive Upload" workflow plus the
    relevant Google Drive semantics: generateIds, create-with-ID (a second
    create with the same ID answers 409), files.get, and the two searches.

    Faults are explicit switches so each regression names its failure."""

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.generated: list[str] = []
        self.calls: list[str] = []
        self.creates: list[str] = []
        self.create_conflicts = 0
        self.folder_state = "PASS"
        self.lose_response_after_create = False
        self.drop_reservation_response = False
        self.drop_reconcile_response = False
        self.fail_create_before_google = False
        self.report_sha256 = True
        self.report_md5 = True
        self.incomplete_search = False
        self.mint_different_id = False
        self.mutate_after_create = None
        self.n8n_outcome_override = None
        self.stale_lookup_once = False
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"synthDriveFile{self._counter:010d}"

    def _resource(self, item: dict) -> dict:
        return {
            "id": item["id"], "name": item["name"], "mimeType": item["mimeType"], "parents": list(item["parents"]),
            "size": str(len(item["bytes"])),
            "md5Checksum": item["md5"] if self.report_md5 else None,
            "sha256Checksum": item["sha256"] if self.report_sha256 else None,
            "appProperties": dict(item["appProperties"]), "trashed": item["trashed"],
        }

    def put_file(self, *, file_id: str, name: str, parent: str, payload: bytes, app_properties: dict,
                 mime: str = "application/pdf", trashed: bool = False) -> None:
        import hashlib

        self.files[file_id] = {
            "id": file_id, "name": name, "mimeType": mime, "parents": [parent], "bytes": payload,
            "md5": hashlib.md5(payload).hexdigest(), "sha256": hashlib.sha256(payload).hexdigest(),
            "appProperties": dict(app_properties), "trashed": trashed,
        }

    def _create(self, file_id: str, metadata: dict, pdf: bytes) -> str:
        if file_id in self.files:
            self.create_conflicts += 1
            return "ALREADY_EXISTS"
        actual = self._new_id() if self.mint_different_id else file_id
        self.creates.append(actual)
        self.put_file(file_id=actual, name=metadata["remote_name"], parent=metadata["folder_id"], payload=pdf,
                      app_properties=metadata["app_properties"])
        if self.mutate_after_create:
            self.mutate_after_create(self.files[actual])
        return "CREATED"

    def _candidates(self, metadata: dict) -> tuple[str, list[dict]]:
        reserved = metadata["reserved_file_id"]
        found = {}
        lookup = "NOT_FOUND"
        if reserved in self.files:
            lookup = "FOUND"
            found[reserved] = self._resource(self.files[reserved])
        for item in self.files.values():
            props = item["appProperties"] or {}
            by_inv = props.get("egInv") == metadata["invoice_id"]
            by_name = (metadata["folder_id"] in item["parents"] and item["name"] == metadata["remote_name"]
                       and not item["trashed"])
            if by_inv or by_name:
                found[item["id"]] = self._resource(item)
        return lookup, list(found.values())[:5]

    def respond(self, metadata: dict, pdf: bytes | None) -> tuple[int, dict]:
        mode = metadata["mode"]
        self.calls.append(mode)
        base = {
            "schema": "energygrid.drive_result.v3", "mode": mode, "operation_id": metadata["operation_id"],
            "generated_id": None, "folder_check": self.folder_state, "reserved_lookup": "NOT_REQUESTED",
            "search_complete": False, "upload_result": "NOT_ATTEMPTED", "candidates": [], "account_ref": None,
        }
        if self.folder_state != "PASS":
            if self.folder_state == "FAIL":
                return 200, {**base, "outcome": "DESTINATION_CHANGED", "support_ref": "EG_DRIVE_FOLDER_CHECK"}
            return 503, {**base, "outcome": "DRIVE_UNAVAILABLE", "support_ref": "EG_DRIVE_FOLDER_CHECK"}
        if mode == "RESERVE_ID":
            generated = self._new_id()
            self.generated.append(generated)
            return 200, {**base, "outcome": "ID_RESERVED", "generated_id": generated, "support_ref": "EG_DRIVE_ID_RESERVED"}
        if mode == "RESOLVE_DESTINATION":
            return 200, {**base, "outcome": "DESTINATION_RESOLVED", "account_ref": SYNTHETIC_ACCOUNT,
                         "support_ref": "EG_DRIVE_DESTINATION_RESOLVED"}
        lookup, candidates = self._candidates(metadata)
        upload_result = "NOT_ATTEMPTED"
        if self.stale_lookup_once and mode == "UPLOAD_IF_ABSENT":
            # Eventual consistency: the first lookup misses an existing object.
            self.stale_lookup_once = False
            lookup, candidates = "NOT_FOUND", []
        if mode == "UPLOAD_IF_ABSENT" and lookup == "NOT_FOUND" and not candidates:
            if self.fail_create_before_google:
                return 503, {**base, "reserved_lookup": lookup, "search_complete": True, "upload_result": "FAILED",
                             "outcome": "DRIVE_UNAVAILABLE", "support_ref": "EG_DRIVE_CREATE_FAILED"}
            # Create with the reserved ID; Google answers 409 when it exists.
            upload_result = self._create(metadata["reserved_file_id"], metadata, pdf)
            lookup, candidates = self._candidates(metadata)
        complete = not self.incomplete_search
        result = {**base, "reserved_lookup": lookup, "search_complete": complete, "upload_result": upload_result,
                  "candidates": candidates}
        if not complete:
            return 503, {**result, "outcome": "DRIVE_UNAVAILABLE", "support_ref": "EG_DRIVE_SEARCH_INCOMPLETE"}
        if lookup == "NOT_FOUND" and not candidates:
            outcome = "NOT_FOUND"
        elif lookup == "FOUND" and len(candidates) == 1:
            # Mirrors the workflow's own exact classification of the reserved object.
            item = candidates[0]
            exact = (
                not item["trashed"] and item["parents"] == [metadata["folder_id"]]
                and item["appProperties"] == metadata["app_properties"] and item["name"] == metadata["remote_name"]
                and item["mimeType"] == "application/pdf" and item["size"] == str(metadata["local_byte_size"])
                and item["sha256Checksum"] in {None, metadata["local_sha256"]}
                and item["md5Checksum"] in {None, metadata["local_md5"]}
            )
            if not exact:
                outcome = "CONFLICT"
            elif item["sha256Checksum"] is None and item["md5Checksum"] is None:
                outcome = "CHECKSUM_UNAVAILABLE"
            else:
                outcome = "UPLOADED" if upload_result in {"CREATED", "ALREADY_EXISTS"} else "FOUND_EXACT"
        else:
            outcome = "CONFLICT"
        if self.n8n_outcome_override is not None:
            outcome = self.n8n_outcome_override
        return 200, {**result, "outcome": outcome, "support_ref": "EG_DRIVE_RESULT"}

    def post_once(self, url: str, authorization: str, body: bytes, timeout: int, *, content_type: str) -> tuple[int, bytes]:
        import json

        metadata, pdf = parse_multipart(body, content_type)
        status, document = self.respond(metadata, pdf)
        mode = metadata["mode"]
        if mode == "RESERVE_ID" and self.drop_reservation_response:
            raise TimeoutError("synthetic lost reservation response")
        if mode == "UPLOAD_IF_ABSENT" and self.lose_response_after_create:
            raise TimeoutError("synthetic lost upload response")
        if mode == "RECONCILE" and self.drop_reconcile_response:
            raise TimeoutError("synthetic lost reconcile response")
        return status, json.dumps(document).encode("utf-8")


class FakeDelivery:
    """Synthetic email leg using the real durable intent/marker/outcome writes."""

    def __init__(self, outcome: str = "DELIVERED") -> None:
        self.sent: list[str] = []
        self.outcome = outcome

    def deliver(self, state, invoice, archive_path, run_id, *, logger=None):
        import uuid as _uuid

        from energygrid_bill_downloader.delivery import DELIVERY_SCHEMA, DeliveryOutcome
        from energygrid_bill_downloader.publication import validate_pdf

        info = validate_pdf(archive_path)
        previous = state.delivery_for_invoice(invoice["invoice_id"])
        delivery_id = previous["delivery_id"] if previous else "egmail-v1-" + _uuid.uuid4().hex
        metadata = {
            "schema": DELIVERY_SCHEMA, "stream": invoice["stream"], "bill_date": invoice["bill_date"],
            "attachment_name": invoice["canonical_filename"], "pdf_byte_size": info.byte_size, "pdf_sha256": info.sha256,
        }
        row, _ = state.prepare_delivery(invoice_id=invoice["invoice_id"], metadata=metadata, run_id=run_id,
                                        timestamp="2026-10-06T00:00:03+00:00", delivery_id=delivery_id)
        if row["state"] != "PENDING_SEND":
            return DeliveryOutcome(row["state"], delivery_id, row["support_ref"] or "EG_MAIL_TERMINAL", False)
        if row["dispatch_started_at_utc"] is not None:
            state.recover_uncertain_deliveries(run_id, "2026-10-06T00:00:04+00:00")
            return DeliveryOutcome("DELIVERY_OUTCOME_UNCERTAIN", delivery_id, "EG_MAIL_RECOVERY_UNCERTAIN", False)
        if not state.claim_delivery_dispatch(delivery_id, run_id, "2026-10-06T00:00:04+00:00"):
            raise AssertionError("synthetic dispatch marker was not acquired")
        self.sent.append(invoice["stream"])
        accepted = "2026-10-06T00:00:05+00:00" if self.outcome == "DELIVERED" else None
        state.record_delivery_outcome(delivery_id, run_id, "2026-10-06T00:00:05+00:00", state=self.outcome,
                                      evidence="VALIDATED_N8N_RESULT", support_ref="EG_SYNTHETIC_MAIL",
                                      accepted_at_utc=accepted)
        return DeliveryOutcome(self.outcome, delivery_id, "EG_SYNTHETIC_MAIL", True)


def seed_verified_drive(state, invoice: dict, *, run_id: str = "00000000-0000-4000-8000-0000000000c1") -> dict:
    """Give a committed latest invoice a DRIVE_VERIFIED receipt through the real
    v3 state API (bind, intent, reserve, dispatch, verified read-back)."""
    import hashlib
    import json as _json
    import uuid as _uuid

    from energygrid_bill_downloader.state import canonical_json

    stream = invoice["stream"]
    binding = state.active_binding(stream)
    if binding is None:
        binding = state.insert_binding(
            stream=stream, account_ref=SYNTHETIC_ACCOUNT, root_folder_id=SYNTHETIC_ROOT,
            folder_id=SYNTHETIC_FOLDERS[stream], chain_sha256="0" * 64, run_id=run_id,
            timestamp="2026-10-06T00:00:00+00:00",
        )
    archive_md5 = invoice.get("_md5")
    if archive_md5 is None:
        raise ValueError("seed_verified_drive needs invoice['_md5']")
    operation, _ = state.create_drive_intent(
        invoice=invoice, binding=binding, local_md5=archive_md5, operation_id=str(_uuid.uuid4()),
        run_id=run_id, timestamp="2026-10-06T00:00:01+00:00",
    )
    file_id = "synthSeeded" + operation["operation_id"].replace("-", "")[:16]
    state.reserve_drive_file_id(operation["operation_id"], file_id, run_id, "2026-10-06T00:00:02+00:00")
    state.begin_drive_dispatch(operation["operation_id"], run_id, "2026-10-06T00:00:03+00:00")
    resource = {
        "id": file_id, "name": operation["remote_name"], "mimeType": "application/pdf", "parents": [operation["folder_id"]],
        "size": str(operation["local_byte_size"]), "md5Checksum": operation["local_md5"],
        "sha256Checksum": operation["local_sha256"], "appProperties": _json.loads(operation["app_properties_json"]),
        "trashed": False,
    }
    receipt = {
        "remote_file_id": file_id, "remote_parent_id": operation["folder_id"], "remote_name_observed": operation["remote_name"],
        "remote_mime_type": "application/pdf", "remote_size": operation["local_byte_size"],
        "remote_sha256": operation["local_sha256"], "remote_md5": operation["local_md5"],
        "accepted_app_properties_json": operation["app_properties_json"], "verification_method": "SHA256",
        "receipt_sha256": hashlib.sha256(canonical_json(resource).encode("ascii")).hexdigest(),
    }
    return state.finish_drive_operation(
        operation["operation_id"], run_id=run_id, timestamp="2026-10-06T00:00:04+00:00", new_state="DRIVE_VERIFIED",
        dispatch_outcome="VALID_RESULT", dispatch_support_ref="EG_DRIVE_RESULT", receipt=receipt,
    )
