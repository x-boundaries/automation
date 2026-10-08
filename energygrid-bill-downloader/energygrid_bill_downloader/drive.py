"""n8n Google Drive boundary client and core-owned receipt verifier (#226 G3).

The filesystem `DriveStager` (Google Drive for Desktop mirror) is retired. Exact
PDF bytes now reach Google Drive only through the source-controlled n8n
"EnergyGrid - Drive Upload" workflow on loopback. n8n performs the Google API
calls with the `googleDriveOAuth2Api` credential; this module never sees a
Google token.

Trust split:
- n8n reports raw Drive facts (folder check, the reserved-ID lookup, the two
  conflict searches and at most five candidate file resources).
- This module alone decides the verdict from those raw facts against the
  frozen SQLite intent. An HTTP 200 or an n8n "success" never counts by itself.

Idempotency (Web amendment A): every upload uses one pre-generated Drive file
ID obtained through `files.generateIds` before the first dispatch, frozen in
SQLite, and reused for every permitted retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable

from .errors import StateError
from .state import DRIVE_ID_RE, DRIVE_MAX_BYTES, canonical_json


DRIVE_REQUEST_SCHEMA = "energygrid.drive_request.v3"
DRIVE_RESULT_SCHEMA = "energygrid.drive_result.v3"
MODES = ("RESERVE_ID", "RECONCILE", "UPLOAD_IF_ABSENT", "RESOLVE_DESTINATION")
REQUEST_KEYS = (
    "schema", "mode", "operation_id", "invoice_id", "stream", "binding_id", "folder_id", "root_folder_id",
    "reserved_file_id", "remote_name", "local_byte_size", "local_sha256", "local_md5", "app_properties",
)
RESULT_KEYS = {
    "schema", "mode", "operation_id", "outcome", "generated_id", "folder_check", "reserved_lookup",
    "search_complete", "upload_result", "candidates", "account_ref", "support_ref",
}
CANDIDATE_KEYS = {
    "id", "name", "mimeType", "parents", "size", "md5Checksum", "sha256Checksum", "appProperties", "trashed",
}
OUTCOMES = {
    "ID_RESERVED", "FOUND_EXACT", "UPLOADED", "NOT_FOUND", "CONFLICT", "CHECKSUM_UNAVAILABLE",
    "DRIVE_UNAVAILABLE", "REQUEST_REJECTED", "DESTINATION_RESOLVED", "DESTINATION_CHANGED",
}
FOLDER_CHECKS = {"PASS", "FAIL", "UNAVAILABLE"}
RESERVED_LOOKUPS = {"FOUND", "NOT_FOUND", "UNAVAILABLE", "NOT_REQUESTED"}
UPLOAD_RESULTS = {"NOT_ATTEMPTED", "CREATED", "ALREADY_EXISTS", "FAILED"}
MAX_METADATA_BYTES = 4096
MAX_RESULT_BYTES = 8192
MAX_CANDIDATES = 5
STATUS_FOR_OUTCOME = {
    "ID_RESERVED": 200, "FOUND_EXACT": 200, "UPLOADED": 200, "NOT_FOUND": 200, "CONFLICT": 200,
    "CHECKSUM_UNAVAILABLE": 200, "DESTINATION_RESOLVED": 200, "DESTINATION_CHANGED": 200,
    "REQUEST_REJECTED": 422, "DRIVE_UNAVAILABLE": 503,
}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
MD5_RE = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
SUPPORT_REF_RE = re.compile(r"EG_[A-Z0-9_]{1,60}\Z", re.ASCII)
OPERATION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.ASCII)
SIZE_RE = re.compile(r"[1-9][0-9]{0,7}\Z", re.ASCII)
FOLDER_LABELS = {"EB_BILL": "EB Bill", "TENANT_BILL": "Tenant Bill"}


@dataclass(frozen=True)
class DriveVerdict:
    """The core's decision. `kind` is one of FOUND_EXACT, NOT_FOUND,
    CHECKSUM_UNAVAILABLE, CONFLICT, DESTINATION_CHANGED or UNAVAILABLE."""

    kind: str
    support_ref: str
    receipt: dict | None = None


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


def file_md5(payload: bytes) -> str:
    """MD5 used only as the Google-published fallback content checksum."""
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


def operation_request(mode: str, operation: dict, root_folder_id: str) -> dict:
    """The exact request metadata for one operation; never caller-supplied."""
    if mode not in {"RESERVE_ID", "RECONCILE", "UPLOAD_IF_ABSENT"}:
        raise StateError("Drive request mode is invalid")
    reserved = operation["reserved_remote_file_id"] if mode != "RESERVE_ID" else None
    if mode != "RESERVE_ID" and reserved is None:
        raise StateError("Drive request requires the reserved file ID")
    return {
        "schema": DRIVE_REQUEST_SCHEMA,
        "mode": mode,
        "operation_id": operation["operation_id"],
        "invoice_id": operation["invoice_id"],
        "stream": operation["stream"],
        "binding_id": operation["binding_id"],
        "folder_id": operation["folder_id"],
        "root_folder_id": root_folder_id,
        "reserved_file_id": reserved,
        "remote_name": operation["remote_name"],
        "local_byte_size": operation["local_byte_size"],
        "local_sha256": operation["local_sha256"],
        "local_md5": operation["local_md5"],
        "app_properties": json.loads(operation["app_properties_json"]),
    }


def destination_request(stream: str, binding) -> dict:
    """Read-only RESOLVE_DESTINATION request for the operator `drive-bind`."""
    return {
        "schema": DRIVE_REQUEST_SCHEMA,
        "mode": "RESOLVE_DESTINATION",
        "operation_id": str(uuid.uuid4()),
        "invoice_id": None,
        "stream": stream,
        "binding_id": binding.binding_id,
        "folder_id": binding.folder_id,
        "root_folder_id": binding.root_folder_id,
        "reserved_file_id": None,
        "remote_name": None,
        "local_byte_size": None,
        "local_sha256": None,
        "local_md5": None,
        "app_properties": None,
    }


def validate_request(metadata: dict, pdf_bytes: bytes | None) -> bytes:
    """Fail closed on any request that is not exactly the frozen contract."""
    if type(metadata) is not dict or list(metadata) != list(REQUEST_KEYS):
        raise StateError("Drive request metadata is invalid")
    mode = metadata["mode"]
    if metadata["schema"] != DRIVE_REQUEST_SCHEMA or mode not in MODES:
        raise StateError("Drive request metadata is invalid")
    if metadata["stream"] not in FOLDER_LABELS:
        raise StateError("Drive request metadata is invalid")
    if not OPERATION_ID_RE.fullmatch(str(metadata["operation_id"])):
        raise StateError("Drive request metadata is invalid")
    for key in ("folder_id", "root_folder_id"):
        if type(metadata[key]) is not str or not DRIVE_ID_RE.fullmatch(metadata[key]):
            raise StateError("Drive request metadata is invalid")
    file_keys = ("invoice_id", "remote_name", "local_byte_size", "local_sha256", "local_md5", "app_properties")
    if mode == "RESOLVE_DESTINATION":
        if any(metadata[key] is not None for key in (*file_keys, "reserved_file_id")) or pdf_bytes is not None:
            raise StateError("Drive request metadata is invalid")
    else:
        size = metadata["local_byte_size"]
        if type(size) is not int or not 0 < size <= DRIVE_MAX_BYTES:
            raise StateError("Drive request metadata is invalid")
        if not SHA256_RE.fullmatch(str(metadata["local_sha256"])) or not MD5_RE.fullmatch(str(metadata["local_md5"])):
            raise StateError("Drive request metadata is invalid")
        properties = metadata["app_properties"]
        if type(properties) is not dict or set(properties) != {"egApp", "egSchema", "egStream", "egInv", "egBnd", "egOp", "egSha"}:
            raise StateError("Drive request metadata is invalid")
        if (
            properties["egApp"] != "xb-energygrid" or properties["egSchema"] != "eg-drive-v3"
            or properties["egStream"] != metadata["stream"] or properties["egInv"] != metadata["invoice_id"]
            or properties["egBnd"] != metadata["binding_id"] or properties["egOp"] != metadata["operation_id"]
            or properties["egSha"] != metadata["local_sha256"]
        ):
            raise StateError("Drive request metadata is invalid")
        reserved = metadata["reserved_file_id"]
        if mode == "RESERVE_ID":
            if reserved is not None or pdf_bytes is not None:
                raise StateError("Drive request metadata is invalid")
        elif type(reserved) is not str or not DRIVE_ID_RE.fullmatch(reserved):
            raise StateError("Drive request metadata is invalid")
        if mode == "RECONCILE" and pdf_bytes is not None:
            raise StateError("Drive reconcile requests never carry a PDF")
        if mode == "UPLOAD_IF_ABSENT":
            if (
                type(pdf_bytes) is not bytes or len(pdf_bytes) != size
                or hashlib.sha256(pdf_bytes).hexdigest() != metadata["local_sha256"]
                or file_md5(pdf_bytes) != metadata["local_md5"]
                or not pdf_bytes.startswith(b"%PDF-")
            ):
                raise StateError("Drive upload bytes do not match the frozen intent")
    encoded = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(encoded) > MAX_METADATA_BYTES:
        raise StateError("Drive request metadata exceeds its bound")
    return encoded


def build_multipart(metadata: dict, pdf_bytes: bytes | None) -> tuple[bytes, str]:
    encoded = validate_request(metadata, pdf_bytes)
    boundary = "----energygrid-drive-" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
        "Content-Type: application/json; charset=utf-8\r\n\r\n"
    ).encode("ascii") + encoded
    if pdf_bytes is not None:
        body += (
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"{metadata['remote_name']}\"\r\n"
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("ascii") + pdf_bytes
    body += f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


def parse_result(status: int, body: bytes, metadata: dict) -> dict | None:
    """Strictly parse `energygrid.drive_result.v3`; None means no valid result."""
    if type(body) is not bytes or len(body) > MAX_RESULT_BYTES or type(status) is not int:
        return None
    try:
        result = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_pairs)
    except (UnicodeDecodeError, ValueError):
        return None
    if type(result) is not dict or set(result) != RESULT_KEYS:
        return None
    mode = metadata["mode"]
    if result["schema"] != DRIVE_RESULT_SCHEMA or result["mode"] != mode or result["operation_id"] != metadata["operation_id"]:
        return None
    outcome = result["outcome"]
    if outcome not in OUTCOMES or STATUS_FOR_OUTCOME[outcome] != status:
        return None
    if type(result["support_ref"]) is not str or not SUPPORT_REF_RE.fullmatch(result["support_ref"]):
        return None
    if (
        result["folder_check"] not in FOLDER_CHECKS or result["reserved_lookup"] not in RESERVED_LOOKUPS
        or result["upload_result"] not in UPLOAD_RESULTS or type(result["search_complete"]) is not bool
    ):
        return None
    candidates = result["candidates"]
    if type(candidates) is not list or len(candidates) > MAX_CANDIDATES:
        return None
    seen: set[str] = set()
    for item in candidates:
        if not _valid_candidate(item) or item["id"] in seen:
            return None
        seen.add(item["id"])
    generated = result["generated_id"]
    account_ref = result["account_ref"]
    if mode == "RESERVE_ID":
        if outcome == "ID_RESERVED":
            if type(generated) is not str or not DRIVE_ID_RE.fullmatch(generated):
                return None
        elif generated is not None or outcome not in {"DRIVE_UNAVAILABLE", "REQUEST_REJECTED", "DESTINATION_CHANGED"}:
            return None
        if candidates or result["upload_result"] != "NOT_ATTEMPTED" or account_ref is not None:
            return None
        return result
    if generated is not None or outcome == "ID_RESERVED":
        return None
    if mode == "RESOLVE_DESTINATION":
        if outcome == "DESTINATION_RESOLVED":
            if type(account_ref) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}\Z", account_ref, re.ASCII):
                return None
            if result["folder_check"] != "PASS":
                return None
        elif account_ref is not None or outcome not in {"DESTINATION_CHANGED", "DRIVE_UNAVAILABLE", "REQUEST_REJECTED"}:
            return None
        if candidates or result["upload_result"] != "NOT_ATTEMPTED":
            return None
        return result
    if account_ref is not None or outcome in {"DESTINATION_RESOLVED"}:
        return None
    # The reconcile branch has no path to the upload node: any claim of an
    # upload attempt in RECONCILE mode is itself a contract violation.
    if mode == "RECONCILE" and (result["upload_result"] != "NOT_ATTEMPTED" or outcome == "UPLOADED"):
        return None
    if outcome == "UPLOADED" and result["upload_result"] not in {"CREATED", "ALREADY_EXISTS"}:
        return None
    return result


def _valid_candidate(item: object) -> bool:
    if type(item) is not dict or set(item) != CANDIDATE_KEYS:
        return False
    if type(item["id"]) is not str or not DRIVE_ID_RE.fullmatch(item["id"]):
        return False
    if type(item["name"]) is not str or len(item["name"]) > 255 or type(item["mimeType"]) is not str:
        return False
    if type(item["parents"]) is not list or not all(type(parent) is str for parent in item["parents"]):
        return False
    if item["size"] is not None and type(item["size"]) is not str:
        return False
    for key in ("md5Checksum", "sha256Checksum"):
        if item[key] is not None and type(item[key]) is not str:
            return False
    if item["appProperties"] is not None and (
        type(item["appProperties"]) is not dict
        or not all(type(key) is str and type(value) is str for key, value in item["appProperties"].items())
    ):
        return False
    return type(item["trashed"]) is bool


def verify_result(operation: dict, result: dict | None) -> DriveVerdict:
    """Decide the verdict for RECONCILE / UPLOAD_IF_ABSENT from raw facts only."""
    if result is None:
        return DriveVerdict("UNAVAILABLE", "EG_DRIVE_RESPONSE_INVALID")
    outcome = result["outcome"]
    if outcome == "REQUEST_REJECTED":
        return DriveVerdict("REJECTED", "EG_DRIVE_REQUEST_REJECTED")
    if result["folder_check"] == "FAIL":
        verdict = DriveVerdict("DESTINATION_CHANGED", "EG_DRIVE_DESTINATION_CHANGED")
    elif (
        result["folder_check"] != "PASS" or result["reserved_lookup"] in {"UNAVAILABLE", "NOT_REQUESTED"}
        or not result["search_complete"]
    ):
        verdict = DriveVerdict("UNAVAILABLE", "EG_DRIVE_RECONCILE_UNAVAILABLE")
    else:
        verdict = _classify_candidates(operation, result)
    expected = {
        "FOUND_EXACT": {"FOUND_EXACT", "UPLOADED"},
        "NOT_FOUND": {"NOT_FOUND"},
        "CHECKSUM_UNAVAILABLE": {"CHECKSUM_UNAVAILABLE"},
        "CONFLICT": {"CONFLICT"},
        "DESTINATION_CHANGED": {"DESTINATION_CHANGED"},
        "UNAVAILABLE": {"DRIVE_UNAVAILABLE"},
    }[verdict.kind]
    if outcome not in expected:
        # n8n's own outcome disagrees with the raw facts: never trust either.
        return DriveVerdict("UNAVAILABLE", "EG_DRIVE_RESULT_DISAGREES")
    if verdict.kind == "NOT_FOUND" and result["upload_result"] in {"CREATED", "ALREADY_EXISTS"}:
        return DriveVerdict("UNAVAILABLE", "EG_DRIVE_RESULT_DISAGREES")
    return verdict


def _classify_candidates(operation: dict, result: dict) -> DriveVerdict:
    reserved = operation["reserved_remote_file_id"]
    candidates = result["candidates"]
    by_id = {item["id"]: item for item in candidates}
    others = [item for item in candidates if item["id"] != reserved]
    if result["reserved_lookup"] == "NOT_FOUND":
        if reserved in by_id:
            return DriveVerdict("UNAVAILABLE", "EG_DRIVE_RESULT_DISAGREES")
        if not candidates:
            return DriveVerdict("NOT_FOUND", "EG_DRIVE_NOT_FOUND")
        return DriveVerdict("CONFLICT", _other_candidate_ref(operation, others))
    # reserved_lookup == FOUND
    if reserved not in by_id:
        return DriveVerdict("UNAVAILABLE", "EG_DRIVE_RESULT_DISAGREES")
    if others:
        return DriveVerdict("CONFLICT", _other_candidate_ref(operation, others))
    item = by_id[reserved]
    frozen_properties = json.loads(operation["app_properties_json"])
    if item["trashed"]:
        return DriveVerdict("CONFLICT", "EG_DRIVE_IDENTITY_TRASHED")
    if item["parents"] != [operation["folder_id"]]:
        return DriveVerdict("CONFLICT", "EG_DRIVE_IDENTITY_MOVED")
    if item["appProperties"] != frozen_properties:
        return DriveVerdict("CONFLICT", "EG_DRIVE_IDENTITY_OP_MISMATCH")
    if item["name"] != operation["remote_name"]:
        return DriveVerdict("CONFLICT", "EG_DRIVE_NAME_MISMATCH")
    if item["mimeType"] != "application/pdf":
        return DriveVerdict("CONFLICT", "EG_DRIVE_MIME_MISMATCH")
    size = item["size"]
    if type(size) is not str or not SIZE_RE.fullmatch(size) or int(size, 10) > DRIVE_MAX_BYTES:
        return DriveVerdict("CONFLICT", "EG_DRIVE_IDENTITY_BYTES_MISMATCH")
    if int(size, 10) != operation["local_byte_size"]:
        return DriveVerdict("CONFLICT", "EG_DRIVE_IDENTITY_BYTES_MISMATCH")
    sha = item["sha256Checksum"]
    md5 = item["md5Checksum"]
    if sha is not None and (not SHA256_RE.fullmatch(sha) or sha != operation["local_sha256"]):
        return DriveVerdict("CONFLICT", "EG_DRIVE_IDENTITY_BYTES_MISMATCH")
    if md5 is not None and (not MD5_RE.fullmatch(md5) or md5 != operation["local_md5"]):
        return DriveVerdict("CONFLICT", "EG_DRIVE_IDENTITY_BYTES_MISMATCH")
    if sha is None and md5 is None:
        # Size alone is never accepted.
        return DriveVerdict("CHECKSUM_UNAVAILABLE", "EG_DRIVE_VERIFICATION_UNAVAILABLE")
    receipt = {
        "remote_file_id": item["id"],
        "remote_parent_id": item["parents"][0],
        "remote_name_observed": item["name"],
        "remote_mime_type": item["mimeType"],
        "remote_size": int(size, 10),
        "remote_sha256": sha,
        "remote_md5": md5,
        "accepted_app_properties_json": canonical_json(item["appProperties"]),
        "verification_method": "SHA256" if sha is not None else "MD5_SIZE",
        "receipt_sha256": hashlib.sha256(canonical_json(item).encode("ascii")).hexdigest(),
    }
    if receipt["remote_file_id"] != reserved:
        return DriveVerdict("CONFLICT", "EG_DRIVE_REMOTE_ID_MISMATCH")
    return DriveVerdict("FOUND_EXACT", "EG_DRIVE_VERIFIED", receipt)


def _other_candidate_ref(operation: dict, others: list[dict]) -> str:
    """Name the conflict for a file that is not the reserved object."""
    if any((item.get("appProperties") or {}).get("egOp") == operation["operation_id"] for item in others):
        # Our own operation identity under a different file ID: the remote side
        # minted another file. Fail closed; never adopt a different ID.
        return "EG_DRIVE_REMOTE_ID_MISMATCH"
    if any((item.get("appProperties") or {}).get("egInv") == operation["invoice_id"] for item in others):
        return "EG_DRIVE_IDENTITY_OP_MISMATCH"
    if len(others) > 1:
        return "EG_DRIVE_MULTIPLE_CANDIDATES"
    return "EG_DRIVE_NAME_OCCUPIED"


class DriveClient:
    """One bounded loopback POST per call; no retries, proxies or redirects."""

    def __init__(
        self,
        settings,
        *,
        post_once: Callable[..., tuple[int, bytes]] | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._post_once = post_once or _post_once
        self._environ = os.environ if environ is None else environ

    def authorization(self) -> str:
        token = self._environ.get(self._settings.auth_token_env)
        header = self._settings.auth_header_name
        if type(token) is not str or not token or "\r" in token or "\n" in token:
            raise StateError("Drive webhook authentication is unavailable")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header, re.ASCII):
            raise StateError("Drive webhook authentication is invalid")
        return f"{header}: {token}"

    def prepare(self, metadata: dict, pdf_bytes: bytes | None) -> tuple[bytes, str, str]:
        """Build and authenticate the request before any durable dispatch marker."""
        body, content_type = build_multipart(metadata, pdf_bytes)
        return body, content_type, self.authorization()

    def send(self, prepared: tuple[bytes, str, str], metadata: dict) -> dict | None:
        body, content_type, authorization = prepared
        try:
            status, response = self._post_once(
                self._settings.url, authorization, body, self._settings.timeout_seconds, content_type=content_type,
            )
        except Exception:
            return None
        return parse_result(status, response, metadata)


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
