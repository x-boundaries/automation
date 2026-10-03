from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import DeliverySettings
from .errors import StateError
from .invoice import Stream, parse_invoice_date
from .publication import FileInfo, validate_pdf
from .state import StateV2Store


DELIVERY_SCHEMA = "energygrid.invoice_delivery.v1"
DELIVERY_RESULT_SCHEMA = "energygrid.invoice_delivery_result.v1"
DELIVERY_ID_RE = re.compile(r"egmail-v1-[0-9a-f]{32}\Z", re.ASCII)
RUN_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
SUPPORT_REF_RE = re.compile(r"EG_[A-Z0-9_]{1,60}\Z", re.ASCII)
RESULT_KEYS = {"schema", "delivery_id", "outcome", "duplicate", "support_ref"}
RESULT_OUTCOMES = {"DELIVERED", "ALREADY_DELIVERED", "DELIVERY_OUTCOME_UNCERTAIN", "REQUEST_REJECTED"}
MAX_METADATA_BYTES = 4096
MAX_MULTIPART_OVERHEAD = 65_536
MAX_RESULT_BYTES = 1024


@dataclass(frozen=True)
class DeliveryOutcome:
    state: str
    delivery_id: str
    support_ref: str
    dispatched: bool


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def build_multipart(metadata: dict, pdf_bytes: bytes, max_pdf_bytes: int) -> tuple[bytes, str]:
    required = {"schema", "delivery_id", "run_id", "stream", "bill_date", "attachment_name", "pdf_byte_size", "pdf_sha256"}
    if type(metadata) is not dict or set(metadata) != required:
        raise StateError("delivery metadata is invalid")
    if metadata["schema"] != DELIVERY_SCHEMA or not isinstance(metadata["stream"], str) or metadata["stream"] not in {item.value for item in Stream}:
        raise StateError("delivery metadata is invalid")
    if type(metadata["delivery_id"]) is not str or not DELIVERY_ID_RE.fullmatch(metadata["delivery_id"]):
        raise StateError("delivery metadata is invalid")
    if type(metadata["run_id"]) is not str or not RUN_ID_RE.fullmatch(metadata["run_id"]):
        raise StateError("delivery metadata is invalid")
    bill_date = metadata["bill_date"]
    parsed = parse_invoice_date(bill_date, "INVOICE_DATE_ISO_V1")
    if metadata["attachment_name"] != f"{parsed.isoformat()}.pdf":
        raise StateError("delivery attachment name is invalid")
    if type(metadata["pdf_byte_size"]) is not int or not 0 < metadata["pdf_byte_size"] <= max_pdf_bytes or metadata["pdf_byte_size"] != len(pdf_bytes):
        raise StateError("delivery PDF size is invalid")
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    if type(metadata["pdf_sha256"]) is not str or not SHA256_RE.fullmatch(metadata["pdf_sha256"]) or metadata["pdf_sha256"] != digest:
        raise StateError("delivery PDF digest is invalid")
    if len(pdf_bytes) < 10 or not pdf_bytes.startswith(b"%PDF-") or not pdf_bytes[-4096:].rstrip().endswith(b"%%EOF"):
        raise StateError("delivery PDF is invalid")
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise StateError("delivery metadata exceeds its bound")
    boundary = "----energygrid-" + uuid.uuid4().hex
    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
        "Content-Type: application/json; charset=utf-8\r\n\r\n"
    ).encode("ascii")
    attachment = (
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"{metadata['attachment_name']}\"\r\n"
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    body = prefix + encoded + attachment + pdf_bytes + suffix
    overhead = len(body) - len(pdf_bytes)
    if overhead > MAX_MULTIPART_OVERHEAD:
        raise StateError("delivery multipart overhead exceeds its bound")
    return body, f"multipart/form-data; boundary={boundary}"


class DeliveryClient:
    """One local durable dispatch permission and at most one bounded HTTP POST."""

    def __init__(
        self,
        settings: DeliverySettings,
        *,
        post_once: Callable[[str, str, bytes, int], tuple[int, bytes]] | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._post_once = post_once or _post_once
        self._environ = os.environ if environ is None else environ

    def deliver(self, state: StateV2Store, invoice: dict, archive_path: Path, run_id: str) -> DeliveryOutcome:
        if type(run_id) is not str or not RUN_ID_RE.fullmatch(run_id):
            raise StateError("delivery run identity is invalid")
        info = validate_pdf(archive_path)
        if info.byte_size > self._settings.max_pdf_bytes:
            raise StateError("delivery PDF exceeds its configured bound")
        with archive_path.open("rb") as handle:
            pdf_bytes = handle.read(self._settings.max_pdf_bytes + 1)
        if len(pdf_bytes) != info.byte_size or hashlib.sha256(pdf_bytes).hexdigest() != info.sha256:
            raise StateError("archive bytes changed during delivery preparation")

        previous = state.delivery_for_invoice(invoice["invoice_id"])
        delivery_id = previous["delivery_id"] if previous else "egmail-v1-" + uuid.uuid4().hex
        intent_run_id = previous["intent_run_id"] if previous else run_id
        metadata = {
            "schema": DELIVERY_SCHEMA,
            "delivery_id": delivery_id,
            "run_id": intent_run_id,
            "stream": invoice["stream"],
            "bill_date": invoice["bill_date"],
            "attachment_name": invoice["canonical_filename"],
            "pdf_byte_size": info.byte_size,
            "pdf_sha256": info.sha256,
        }
        intent, _created = state.prepare_delivery(
            invoice_id=invoice["invoice_id"], metadata=metadata, run_id=run_id,
            timestamp=_utc_now(), delivery_id=delivery_id,
        )
        if intent["state"] == "DELIVERED":
            return DeliveryOutcome("DELIVERED", intent["delivery_id"], intent["support_ref"] or "EG_MAIL_ALREADY_DELIVERED", False)
        if intent["state"] in {"DELIVERY_OUTCOME_UNCERTAIN", "REQUEST_REJECTED"}:
            return DeliveryOutcome(intent["state"], intent["delivery_id"], intent["support_ref"] or "EG_MAIL_TERMINAL", False)
        if intent["dispatch_started_at_utc"] is not None:
            state.recover_uncertain_deliveries(run_id, _utc_now())
            return DeliveryOutcome("DELIVERY_OUTCOME_UNCERTAIN", delivery_id, "EG_MAIL_RECOVERY_UNCERTAIN", False)

        # Prepare the exact bytes and retrieve authentication before consuming
        # the one-time dispatch permission.
        body, content_type = build_multipart(metadata, pdf_bytes, self._settings.max_pdf_bytes)
        token = self._environ.get(self._settings.auth_token_env)
        if type(token) is not str or not token:
            raise StateError("delivery authentication is unavailable")
        header_name = self._settings.auth_header_name
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header_name, re.ASCII) or "\r" in token or "\n" in token:
            raise StateError("delivery authentication is invalid")

        if not state.claim_delivery_dispatch(delivery_id, run_id, _utc_now()):
            after = state.delivery(delivery_id)
            if after and after["dispatch_started_at_utc"] is not None:
                state.recover_uncertain_deliveries(run_id, _utc_now())
                return DeliveryOutcome("DELIVERY_OUTCOME_UNCERTAIN", delivery_id, "EG_MAIL_DISPATCH_CLAIMED", False)
            return DeliveryOutcome("REQUEST_REJECTED", delivery_id, "EG_MAIL_DISPATCH_NOT_CLAIMED", False)

        try:
            status, response_body = self._post_once(
                self._settings.url,
                f"{header_name}: {token}",
                body,
                self._settings.timeout_seconds,
                content_type=content_type,
            )
            response = _parse_result(response_body, delivery_id)
        except Exception:
            response = None
            status = 0

        outcome, support_ref = _classify_response(status, response)
        accepted_at = _utc_now() if outcome == "DELIVERED" else None
        try:
            recorded = state.record_delivery_outcome(
                delivery_id, run_id, _utc_now(), state=outcome,
                evidence="VALIDATED_N8N_RESULT" if response is not None else "NO_VALID_N8N_RESULT",
                support_ref=support_ref, accepted_at_utc=accepted_at,
            )
        except Exception:
            recorded = False
        if not recorded:
            return DeliveryOutcome("DELIVERY_OUTCOME_UNCERTAIN", delivery_id, "EG_MAIL_OUTCOME_NOT_DURABLE", True)
        return DeliveryOutcome(outcome, delivery_id, support_ref, True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _post_once(url: str, authorization: str, body: bytes, timeout: int, *, content_type: str) -> tuple[int, bytes]:
    header_name, separator, token = authorization.partition(": ")
    if not separator:
        raise ValueError("invalid authorization input")
    request = urllib.request.Request(
        url,
        data=body,
        headers={header_name: token, "Content-Type": content_type, "Content-Length": str(len(body)), "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_RESULT_BYTES + 1)
    except urllib.error.HTTPError as response:
        return response.code, response.read(MAX_RESULT_BYTES + 1)


def _parse_result(body: bytes, expected_delivery_id: str) -> dict | None:
    if type(body) is not bytes or len(body) > MAX_RESULT_BYTES:
        return None
    try:
        result = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_pairs)
    except (UnicodeDecodeError, ValueError):
        return None
    if type(result) is not dict or set(result) != RESULT_KEYS:
        return None
    if result["schema"] != DELIVERY_RESULT_SCHEMA:
        return None
    if result["delivery_id"] != expected_delivery_id:
        return None
    if result["outcome"] not in RESULT_OUTCOMES:
        return None
    if type(result["duplicate"]) is not bool:
        return None
    if result["duplicate"] != (result["outcome"] == "ALREADY_DELIVERED"):
        return None
    if type(result["support_ref"]) is not str or not SUPPORT_REF_RE.fullmatch(result["support_ref"]):
        return None
    return result


def _classify_response(status: int, response: dict | None) -> tuple[str, str]:
    if response is None:
        return "DELIVERY_OUTCOME_UNCERTAIN", "EG_MAIL_RESPONSE_INVALID"
    result = response["outcome"]
    if status == 200 and result in {"DELIVERED", "ALREADY_DELIVERED"}:
        return "DELIVERED", response["support_ref"]
    if status in {400, 401, 403, 409, 413, 415, 422} and result == "REQUEST_REJECTED":
        return "REQUEST_REJECTED", response["support_ref"]
    if status == 503 and result == "DELIVERY_OUTCOME_UNCERTAIN":
        return "DELIVERY_OUTCOME_UNCERTAIN", response["support_ref"]
    return "DELIVERY_OUTCOME_UNCERTAIN", "EG_MAIL_RESPONSE_CONFLICT"
