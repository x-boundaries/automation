"""Native Claude connector bridge for the EnergyGrid v4 Core (#226 V3 G1).

The v4 Core cannot call the claude.ai Google Drive and Gmail connectors: only the
Claude host can. This module is the deterministic half of a stepwise protocol in
which the Core decides, seals and verifies, and the host performs exactly one
native connector call per sealed dispatch:

    acquire                                 portal -> archive -> admission (no provider effect)
    next                                    plan one step; write the base64 payload; no seal
    DRIVE  drive-seal   (precheck evidence) name not occupied -> seal -> host create_file
           drive-readback (download base64) Core hashes what Drive returns
           drive-record (create evidence)   metadata + readback -> VERIFIED, else REJECTED/UNKNOWN
    GMAIL  gmail-readback (draft attachment) Core hashes the draft attachment, when required
           gmail-seal  (draft evidence)     recipient, subject, one attachment, name, size -> seal
           gmail-record (send evidence)     the bound draft was sent -> VERIFIED

Authority stays in SQLite (StateV4Store). Every write is lease-fenced. A seal is
durable before the host is told to act, and the host acts only on the seal
command's answer. A crash or a lost answer after a seal leaves DISPATCHED, which
the next `acquire` or `next` turns into UNKNOWN; UNKNOWN is never resent and is
resolved only by a late VERIFIED receipt with the exact archive SHA-256. Bridge
files under the private outbox are evidence and payload staging, never authority.

The module imports no network, Google or process code. The CLI builds the real
portal adapters separately.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import AppError, ConfigError, OfflineV4Error
from .publication import ensure_no_reparse_components, validate_pdf

NATIVE_CONFIG_SCHEMA = "energygrid.native.v4.config.v1"
NATIVE_RESULT_SCHEMA = "energygrid.native.v4.result.v1"
MODE_UAT_SYNTHETIC = "UAT_SYNTHETIC"
MODE_UAT_PORTAL = "UAT_PORTAL"
# Production mode is deliberately absent: unattended production delivery needs its own authority.
NATIVE_MODES = (MODE_UAT_SYNTHETIC, MODE_UAT_PORTAL)
OWNER_TOKEN = "native-bridge"
LEASE_TTL_MS = 600_000
PDF_MIME = "application/pdf"
STREAM_LABELS = {"EB_BILL": "EB Bill", "TENANT_BILL": "Tenant Bill"}
MAX_RECIPIENTS = 5
MAX_EFFECT_CEILING = 20
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}", re.ASCII)
_DRIVE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,200}", re.ASCII)
_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}", re.ASCII)
_INTENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}", re.ASCII)

# Exit codes follow the v3 core contract: 0 done or nothing to do, 10 retryable,
# 20 HOLD or uncertainty recorded, 64 refused with no state change.
EXIT_OK, EXIT_RETRY, EXIT_HOLD, EXIT_REFUSED = 0, 10, 20, 64


class NativeRefusal(OfflineV4Error):
    """A bridge refusal: nothing was written to SQLite."""

    def __init__(self, support_ref: str, exit_code: int = EXIT_REFUSED) -> None:
        super().__init__(support_ref, status="REFUSED", exit_code=exit_code)
        self.support_ref = support_ref


# ---------------------------------------------------------------------------
# Configuration (private, outside Git)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeDriveSettings:
    account: str = field(repr=False)
    folder_ids: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class NativeGmailSettings:
    account: str = field(repr=False)
    to: tuple[str, ...] = field(repr=False)
    subject_template: str = field(repr=False)
    body_template: str = field(repr=False)
    require_attachment_readback: bool = True


@dataclass(frozen=True)
class NativeConfig:
    mode: str
    state_path: Path = field(repr=False)
    archive_root: Path = field(repr=False)
    temp_root: Path = field(repr=False)
    outbox_root: Path = field(repr=False)
    drive: NativeDriveSettings = field(repr=False)
    gmail: NativeGmailSettings = field(repr=False)
    max_drive_uploads: int
    max_gmail_sends: int
    synthetic_root: Path | None = field(default=None, repr=False)
    portal_config_path: Path | None = field(default=None, repr=False)
    inventory_ceiling: int = 200


def _closed(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ConfigError(f"{label} has an invalid shape")
    return value


def _text(value: Any, label: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > max_length or not value.isprintable():
        raise ConfigError(f"{label} is invalid")
    return value


def _email(value: Any, label: str) -> str:
    text = _text(value, label, max_length=320)
    if not _EMAIL_RE.fullmatch(text):
        raise ConfigError(f"{label} is not a plain email address")
    return text


def _roots(checkout_root: Path | tuple[Path, ...] | None) -> tuple[Path, ...]:
    if checkout_root is None:
        return ()
    return (checkout_root,) if isinstance(checkout_root, Path) else tuple(checkout_root)


def _private_path(value: Any, label: str, checkout_root: Path | tuple[Path, ...] | None) -> Path:
    text = _text(value, label, max_length=1024)
    path = Path(text)
    if not path.is_absolute() or text.startswith("\\\\") or text.startswith("//"):
        raise ConfigError(f"{label} must be an absolute local path (no UNC)")
    ensure_no_reparse_components(path)
    # Resolved comparison, so 8.3 short names and `..` cannot hide a checkout path.
    resolved = path.resolve(strict=False)
    for root in _roots(checkout_root):
        root = root.resolve(strict=False)
        if resolved == root or root in resolved.parents:
            # Private bills, payloads and state must never live in a Git checkout.
            raise ConfigError(f"{label} must be outside the repository checkout")
    return path


def _ceiling(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_EFFECT_CEILING:
        raise ConfigError(f"{label} must be an integer from 0 to {MAX_EFFECT_CEILING}")
    return value


def load_native_config(raw: Any, *, checkout_root: Path | tuple[Path, ...] | None) -> NativeConfig:
    """Load the closed native bridge config. No key is optional and no value is coerced."""
    common = {"schema", "mode", "state_path", "archive_root", "temp_root", "outbox_root", "drive", "gmail",
              "effect_ceiling"}
    if type(raw) is not dict or raw.get("schema") != NATIVE_CONFIG_SCHEMA:
        raise ConfigError("native config schema is invalid")
    mode = raw.get("mode")
    if mode == MODE_UAT_SYNTHETIC:
        data = _closed(raw, common | {"synthetic_root"}, "native config")
    elif mode == MODE_UAT_PORTAL:
        data = _closed(raw, common | {"portal_config_path"}, "native config")
    else:
        raise ConfigError("native config mode must be UAT_SYNTHETIC or UAT_PORTAL")
    paths = {name: _private_path(data[name], name, checkout_root)
             for name in ("state_path", "archive_root", "temp_root", "outbox_root")}
    drive_raw = _closed(data["drive"], {"account", "folder_ids"}, "drive")
    folders_raw = _closed(drive_raw["folder_ids"], {"EB_BILL", "TENANT_BILL"}, "drive.folder_ids")
    folders: dict[str, str] = {}
    for stream, folder in folders_raw.items():
        if type(folder) is not str or not _DRIVE_ID_RE.fullmatch(folder):
            raise ConfigError(f"drive.folder_ids.{stream} is invalid")
        folders[stream] = folder
    if folders["EB_BILL"] == folders["TENANT_BILL"]:
        raise ConfigError("each stream needs its own Drive folder")
    gmail_raw = _closed(
        data["gmail"], {"account", "to", "subject_template", "body_template", "require_attachment_readback"}, "gmail"
    )
    to = gmail_raw["to"]
    if type(to) is not list or not 1 <= len(to) <= MAX_RECIPIENTS:
        raise ConfigError("gmail.to must list 1 to 5 recipients")
    recipients = tuple(_email(item, "gmail.to") for item in to)
    if len({item.lower() for item in recipients}) != len(recipients):
        raise ConfigError("gmail.to has a duplicate recipient")
    subject = _text(gmail_raw["subject_template"], "gmail.subject_template", max_length=200)
    if subject.count("{display_filename}") != 1:
        raise ConfigError("gmail.subject_template must contain {display_filename} exactly once")
    body = gmail_raw["body_template"]
    if type(body) is not str or not body.strip() or len(body) > 4000:
        raise ConfigError("gmail.body_template is invalid")
    readback = gmail_raw["require_attachment_readback"]
    if type(readback) is not bool:
        raise ConfigError("gmail.require_attachment_readback must be true or false")
    ceiling = _closed(data["effect_ceiling"], {"drive_uploads", "gmail_sends"}, "effect_ceiling")
    synthetic_root = portal_config = None
    if mode == MODE_UAT_SYNTHETIC:
        synthetic_root = _private_path(data["synthetic_root"], "synthetic_root", checkout_root)
    else:
        portal_config = _private_path(data["portal_config_path"], "portal_config_path", checkout_root)
    return NativeConfig(
        mode=mode,
        drive=NativeDriveSettings(account=_email(drive_raw["account"], "drive.account"), folder_ids=folders),
        gmail=NativeGmailSettings(
            account=_email(gmail_raw["account"], "gmail.account"), to=recipients, subject_template=subject,
            body_template=body, require_attachment_readback=readback,
        ),
        max_drive_uploads=_ceiling(ceiling["drive_uploads"], "effect_ceiling.drive_uploads"),
        max_gmail_sends=_ceiling(ceiling["gmail_sends"], "effect_ceiling.gmail_sends"),
        synthetic_root=synthetic_root,
        portal_config_path=portal_config,
        **paths,
    )


# ---------------------------------------------------------------------------
# Synthetic UAT portal (local files only)
# ---------------------------------------------------------------------------

SYNTHETIC_NAMESPACE = "UAT-SYNTHETIC-ENERGYGRID"
SYNTHETIC_EVIDENCE = "EG_UAT_SYNTHETIC_SOURCE"


class SyntheticPortalAdapter:
    """Serves synthetic PDFs from `<synthetic_root>/<STREAM>/` per `inventory.json`. No network.

    inventory.json: {"EB_BILL": [{"filename": "...pdf", "date": "YYYY-MM-DD"}], "TENANT_BILL": [...]}
    """

    def __init__(self, root: Path, stream: str) -> None:
        from .invoice import Stream

        self._root = root
        self._stream = Stream(stream)
        self._issued: tuple = ()
        self.inventory_calls = 0
        self.acquire_calls = 0

    def inventory(self, safety_ceiling: int):
        from .errors import SourceContractError
        from .invoice import DATE_PROFILE_ISO_V1, Candidate, InventorySnapshot

        self.inventory_calls += 1
        self._issued = ()
        try:
            listing = json.loads((self._root / "inventory.json").read_text(encoding="utf-8"))[self._stream.value]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SourceContractError("EG_UAT_SYNTHETIC_INVENTORY_INVALID") from exc
        if type(listing) is not list or len(listing) > safety_ceiling:
            raise SourceContractError("EG_UAT_SYNTHETIC_INVENTORY_INVALID")
        candidates = tuple(
            Candidate.create(
                stream=self._stream, source_namespace=SYNTHETIC_NAMESPACE, source_filename=item["filename"],
                raw_date=item["date"], date_profile=DATE_PROFILE_ISO_V1, evidence_ref=SYNTHETIC_EVIDENCE,
                fetch_handle={"filename": item["filename"]},
            )
            for item in listing
        )
        self._issued = candidates
        return InventorySnapshot(
            stream=self._stream, source_namespace=SYNTHETIC_NAMESPACE, date_profile=DATE_PROFILE_ISO_V1,
            completeness_witness="EG_UAT_SYNTHETIC_COMPLETE", candidates=candidates,
        )

    def acquire(self, candidate, destination: Path) -> str:
        from .errors import SourceContractError

        if not any(candidate is issued for issued in self._issued):
            raise SourceContractError("EG_FETCH_HANDLE_INVALID")
        self.acquire_calls += 1
        source = self._root / self._stream.value / candidate.source_filename
        try:
            destination.write_bytes(source.read_bytes())
        except OSError as exc:
            raise SourceContractError("EG_UAT_SYNTHETIC_ARTIFACT_MISSING") from exc
        return candidate.source_filename


def synthetic_pdf(label: str) -> bytes:
    """A small valid synthetic PDF. It carries no private data."""
    text = f"EnergyGrid UAT synthetic invoice {label}. Not a real bill."
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


def write_synthetic_fixture(root: Path, listing: dict[str, list[tuple[str, str]]]) -> None:
    """Create a synthetic UAT portal under `root`: inventory.json plus one PDF per entry."""
    inventory: dict[str, list[dict[str, str]]] = {}
    for stream in ("EB_BILL", "TENANT_BILL"):
        folder = root / stream
        folder.mkdir(parents=True, exist_ok=True)
        inventory[stream] = []
        for filename, day in listing.get(stream, []):
            date.fromisoformat(day)
            if type(filename) is not str or Path(filename).name != filename or filename in ("", ".", ".."):
                raise ConfigError("synthetic listing filename must be a plain file name")
            (folder / filename).write_bytes(synthetic_pdf(f"{stream} {day} {filename}"))
            inventory[stream].append({"filename": filename, "date": day})
    (root / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Bridge session helpers
# ---------------------------------------------------------------------------


def _iso(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    return str(uuid.uuid4())


@contextlib.contextmanager
def _session(state, now_ms: int) -> Iterator[None]:
    """Hold the v4 lease for one bridge command. A held lease refuses with exit 10 and no mutation."""
    state.acquire_lease(OWNER_TOKEN, now_ms=now_ms, ttl_ms=LEASE_TTL_MS)
    try:
        yield
    finally:
        state.release_lease(OWNER_TOKEN)


def _check_intent_id(intent_id: Any) -> str:
    if type(intent_id) is not str or not _INTENT_ID_RE.fullmatch(intent_id):
        raise NativeRefusal("EG_NATIVE_INTENT_ID_INVALID")
    return intent_id


def _outbox(cfg: NativeConfig) -> Path:
    ensure_no_reparse_components(cfg.outbox_root)
    cfg.outbox_root.mkdir(parents=True, exist_ok=True)
    return cfg.outbox_root


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temporary, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_json_once(path: Path, document: dict) -> None:
    """Write-once evidence: an existing file is never replaced."""
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise NativeRefusal("EG_NATIVE_EVIDENCE_UNREADABLE")
    if type(value) is not dict:
        raise NativeRefusal("EG_NATIVE_EVIDENCE_UNREADABLE")
    return value


def load_evidence(path: Path) -> dict:
    value = _read_json(path)
    if value is None:
        raise NativeRefusal("EG_NATIVE_EVIDENCE_MISSING")
    return value


def _invoice_for_intent(state, intent_id: str) -> tuple[dict, dict]:
    intent = state.intent(intent_id)
    if intent is None:
        raise NativeRefusal("EG_NATIVE_INTENT_UNKNOWN")
    rows = [row for stream in STREAM_LABELS for row in state.invoices_for_stream(stream)
            if row["invoice_key"] == intent["invoice_key"]]
    if len(rows) != 1:
        raise NativeRefusal("EG_NATIVE_INTENT_UNKNOWN")
    return intent, rows[0]


def _current_step(state, intent_id: str, action: str) -> tuple[dict, dict]:
    """The intent must be PENDING and be exactly the next planned step of its stream (FIFO, Drive first)."""
    from .orchestration import _v4_stream_step

    intent, invoice = _invoice_for_intent(state, intent_id)
    if intent["action"] != action:
        raise NativeRefusal("EG_NATIVE_ACTION_MISMATCH")
    if intent["state"] != "PENDING":
        raise NativeRefusal("EG_NATIVE_INTENT_NOT_PENDING")
    kind, planned_action, _invoice, planned = _v4_stream_step(state, invoice["stream"])
    if kind != "STEP" or planned_action != action or planned["intent_id"] != intent_id:
        raise NativeRefusal("EG_NATIVE_STEP_NOT_PLANNED")
    return intent, invoice


def _verified_payload(state, cfg: NativeConfig, intent_id: str, invoice: dict, run_id: str, now_iso: str) -> bytes | None:
    """Re-check the archived bytes immediately before any seal. A failure HOLDs the PENDING intent."""
    path = cfg.archive_root / invoice["archive_relpath"]
    try:
        ensure_no_reparse_components(path)
        info = validate_pdf(path)
        payload = path.read_bytes()
    except (AppError, OSError):
        state.hold_pending(intent_id, support_ref="EG_V4_ARCHIVE_UNREADABLE", run_id=run_id, now_iso=now_iso)
        return None
    digest = hashlib.sha256(payload).hexdigest()
    if digest != invoice["sha256"] or info.sha256 != digest or len(payload) != invoice["byte_size"]:
        state.hold_pending(intent_id, support_ref="EG_V4_ARCHIVE_IDENTITY_CHANGED", run_id=run_id, now_iso=now_iso)
        return None
    return payload


def _at_ceiling(state, cfg: NativeConfig, action: str) -> bool:
    limit = cfg.max_drive_uploads if action == "DRIVE_UPLOAD" else cfg.max_gmail_sends
    return state.dispatch_count(action) >= limit


def _check_ceiling(state, cfg: NativeConfig, action: str) -> None:
    if _at_ceiling(state, cfg, action):
        raise NativeRefusal("EG_NATIVE_EFFECT_CEILING_REACHED")


def drive_title_query(folder_id: str, title: str) -> str:
    """The exact Drive search query for the precheck. String values are single-quoted with
    backslash and quote escaped, so a title containing an apostrophe cannot break the query."""
    escaped = title.replace("\\", "\\\\").replace("'", "\\'")
    return f"parentId = '{folder_id}' and title = '{escaped}'"


def subject_for(cfg: NativeConfig, invoice: dict) -> str:
    return cfg.gmail.subject_template.replace("{display_filename}", invoice["display_filename"])


def body_for(cfg: NativeConfig, invoice: dict) -> str:
    return (cfg.gmail.body_template
            .replace("{display_filename}", invoice["display_filename"])
            .replace("{stream_label}", STREAM_LABELS[invoice["stream"]]))


def _payload_path(cfg: NativeConfig, intent_id: str) -> Path:
    return _outbox(cfg) / f"{intent_id}.pdf.b64"


def _result(command: str, outcome: str, exit_code: int, **fields: Any) -> dict:
    return {"schema": NATIVE_RESULT_SCHEMA, "command": command, "outcome": outcome, "exit_code": exit_code, **fields}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def acquire(state, cfg: NativeConfig, adapters: dict[str, Any], *, run_id: str, now_ms: int) -> dict:
    """Recover, then admit the latest invoice of each stream. No provider effect."""
    from .reconcile import admit_latest_offline_v4

    now_iso = _iso(now_ms)
    with _session(state, now_ms):
        recovered = state.recover_unfinished(run_id, now_iso=now_iso)
        acquisition = admit_latest_offline_v4(
            adapters=adapters, state=state, archive_root=cfg.archive_root, temp_root=cfg.temp_root,
            run_id=run_id, now_iso=now_iso, inventory_ceiling=cfg.inventory_ceiling,
        )
    return _result("acquire", "ACQUIRED", EXIT_OK, run_id=run_id, recovered_unknown=recovered,
                   acquisition=acquisition)


def next_step(state, cfg: NativeConfig, *, run_id: str, now_ms: int) -> dict:
    """Plan the next native step (no seal). Writes the exact archive bytes as base64 for the host."""
    from .orchestration import _v4_rotated, _v4_stream_step
    from .state import V4_STREAMS

    now_iso = _iso(now_ms)
    with _session(state, now_ms):
        recovered = state.recover_unfinished(run_id, now_iso=now_iso)
        plans = {stream: _v4_stream_step(state, stream) for stream in V4_STREAMS}
        stepping = [stream for stream in _v4_rotated(V4_STREAMS, state.cursor()) if plans[stream][0] == "STEP"]
        # A stream whose next action is at its effect ceiling is skipped, so it never starves the other.
        ready = [stream for stream in stepping if not _at_ceiling(state, cfg, plans[stream][1])]
        if not ready:
            # Recovery may have written above, so this is a normal result, never a no-change refusal.
            return _result("next", "CEILING_REACHED" if stepping else "IDLE", EXIT_OK, run_id=run_id,
                           recovered_unknown=recovered, streams=stream_report(state))
        stream = ready[0]
        _kind, action, invoice, intent = plans[stream]
        intent_id = intent["intent_id"]
        payload = _verified_payload(state, cfg, intent_id, invoice, run_id, now_iso)
        if payload is None:
            return _result("next", "HOLD", EXIT_HOLD, run_id=run_id, intent_id=intent_id, stream=stream,
                           support_ref=state.intent(intent_id)["support_ref"])
        if action == "GMAIL_SEND":
            drive_intent = state.intents_for_invoice(invoice["invoice_key"])["DRIVE_UPLOAD"]
            if state.verified_provider_ref(drive_intent["intent_id"]) is None:
                state.hold_pending(intent_id, support_ref="EG_V4_DRIVE_NOT_VERIFIED", run_id=run_id, now_iso=now_iso)
                return _result("next", "HOLD", EXIT_HOLD, run_id=run_id, intent_id=intent_id, stream=stream,
                               support_ref="EG_V4_DRIVE_NOT_VERIFIED")
        payload_path = _payload_path(cfg, intent_id)
        _write_atomic(payload_path, base64.b64encode(payload))
        state.set_cursor(stream, now_iso=now_iso)
    common = {
        "run_id": run_id, "recovered_unknown": recovered, "intent_id": intent_id, "stream": stream,
        "display_filename": invoice["display_filename"], "byte_size": invoice["byte_size"],
        "sha256": invoice["sha256"], "payload_base64_path": str(payload_path),
    }
    if action == "DRIVE_UPLOAD":
        return _result("next", "DRIVE_PRECHECK", EXIT_OK, **common, drive_account=cfg.drive.account,
                       folder_id=cfg.drive.folder_ids[stream],
                       precheck_query=drive_title_query(cfg.drive.folder_ids[stream], invoice["display_filename"]))
    return _result("next", "GMAIL_DRAFT", EXIT_OK, **common, gmail_account=cfg.gmail.account,
                   to=list(cfg.gmail.to), subject=subject_for(cfg, invoice), body=body_for(cfg, invoice),
                   attachment_mime_type=PDF_MIME, require_attachment_readback=cfg.gmail.require_attachment_readback)


def drive_seal(state, cfg: NativeConfig, intent_id: str, evidence: dict, *, run_id: str, now_ms: int) -> dict:
    """Seal one Drive upload after a complete name-occupancy precheck of the configured folder.

    evidence: {"folder_id": <id searched>, "title": <title searched>, "query": <the exact precheck_query
               from `next`>, "complete": true, "matches": [{"id": ..., "title": ..., "parentId": ...}, ...]}
    """
    intent_id = _check_intent_id(intent_id)
    now_iso = _iso(now_ms)
    with _session(state, now_ms):
        _intent, invoice = _current_step(state, intent_id, "DRIVE_UPLOAD")
        folder = cfg.drive.folder_ids[invoice["stream"]]
        matches = evidence.get("matches")
        if (evidence.get("folder_id") != folder or evidence.get("title") != invoice["display_filename"]
                or evidence.get("query") != drive_title_query(folder, invoice["display_filename"])
                or evidence.get("complete") is not True or type(matches) is not list):
            raise NativeRefusal("EG_NATIVE_PRECHECK_EVIDENCE_INVALID")
        _check_ceiling(state, cfg, "DRIVE_UPLOAD")
        payload = _verified_payload(state, cfg, intent_id, invoice, run_id, now_iso)
        if payload is None:
            return _result("drive-seal", "HOLD", EXIT_HOLD, intent_id=intent_id,
                           support_ref=state.intent(intent_id)["support_ref"])
        if matches:
            # Never overwrite, suffix or create a variant. An operator decides.
            state.hold_pending(intent_id, support_ref="EG_NATIVE_DRIVE_NAME_OCCUPIED", run_id=run_id, now_iso=now_iso)
            return _result("drive-seal", "HOLD", EXIT_HOLD, intent_id=intent_id,
                           support_ref="EG_NATIVE_DRIVE_NAME_OCCUPIED")
        payload_path = _payload_path(cfg, intent_id)
        _write_atomic(payload_path, base64.b64encode(payload))
        state.assert_lease_held()
        state.seal_dispatch(intent_id, run_id=run_id, now_iso=now_iso)
    return _result(
        "drive-seal", "SEALED", EXIT_OK, intent_id=intent_id,
        create_file={
            "title": invoice["display_filename"], "parentId": folder, "contentMimeType": PDF_MIME,
            "disableConversionToGoogleType": True, "base64Content_path": str(payload_path),
        },
    )


def _readback(state, cfg: NativeConfig, intent_id: str, kind: str, provider_id: str, encoded_path: Path) -> dict:
    """Hash the bytes a provider returned and write a write-once Core attestation."""
    intent_id = _check_intent_id(intent_id)
    if type(provider_id) is not str or not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise NativeRefusal("EG_NATIVE_PROVIDER_ID_INVALID")
    _intent, invoice = _invoice_for_intent(state, intent_id)
    try:
        text = encoded_path.read_text(encoding="ascii").strip()
        raw = base64.b64decode(re.sub(r"\s+", "", text), validate=True)
    except (OSError, UnicodeDecodeError, ValueError, binascii.Error):
        raise NativeRefusal("EG_NATIVE_READBACK_UNDECODABLE")
    digest = hashlib.sha256(raw).hexdigest()
    attestation = {
        "intent_id": intent_id, "kind": kind, "provider_id": provider_id, "sha256": digest, "byte_size": len(raw),
        "matches_archive": digest == invoice["sha256"] and len(raw) == invoice["byte_size"],
    }
    path = _outbox(cfg) / f"{intent_id}.{kind}-readback.{provider_id}.json"
    try:
        _write_json_once(path, attestation)
    except FileExistsError:
        if _read_json(path) != attestation:
            raise NativeRefusal("EG_NATIVE_READBACK_CONFLICT")
    return _result(f"{kind}-readback", "MATCH" if attestation["matches_archive"] else "MISMATCH", EXIT_OK,
                   intent_id=intent_id, provider_id=provider_id, matches_archive=attestation["matches_archive"])


def drive_readback(state, cfg: NativeConfig, intent_id: str, file_id: str, encoded_path: Path) -> dict:
    return _readback(state, cfg, intent_id, "drive", file_id, encoded_path)


def gmail_readback(state, cfg: NativeConfig, intent_id: str, draft_id: str, encoded_path: Path) -> dict:
    return _readback(state, cfg, intent_id, "gmail", draft_id, encoded_path)


def _attestation(cfg: NativeConfig, intent_id: str, kind: str, provider_id: str) -> dict | None:
    """The attestation for exactly this intent, kind and provider id. Windows file names are
    case-insensitive, so the stored identity is compared as well as the file name."""
    document = _read_json(cfg.outbox_root / f"{intent_id}.{kind}-readback.{provider_id}.json")
    if document is None:
        return None
    if (document.get("intent_id"), document.get("kind"), document.get("provider_id")) != (intent_id, kind, provider_id):
        return None
    return document


def _provider_outcome(evidence: dict, success: str) -> str:
    outcome = evidence.get("outcome")
    if outcome not in (success, "DENIED", "ERROR", "NO_RESPONSE"):
        raise NativeRefusal("EG_NATIVE_OUTCOME_INVALID")
    return outcome


def _dispatched_intent(state, intent_id: str, action: str) -> tuple[dict, dict]:
    intent, invoice = _invoice_for_intent(state, intent_id)
    if intent["action"] != action:
        raise NativeRefusal("EG_NATIVE_ACTION_MISMATCH")
    if intent["state"] not in ("DISPATCHED", "UNKNOWN", "VERIFIED", "HOLD") or state.dispatch_row(intent_id) is None:
        raise NativeRefusal("EG_NATIVE_NOT_DISPATCHED")
    return intent, invoice


def drive_record(state, cfg: NativeConfig, intent_id: str, evidence: dict, *, run_id: str, now_ms: int) -> dict:
    """Record the outcome of the one sealed Drive create.

    evidence: {"outcome": "CREATED" | "DENIED" | "ERROR" | "NO_RESPONSE",
               "file": {"id", "title", "parentId", "mimeType", "fileSize"}}   (file for CREATED only)
    CREATED needs matching metadata and a matching Core readback attestation for the same file id.
    DENIED (the native call was refused before any effect) is REJECTED -> HOLD; ERROR and
    NO_RESPONSE are UNKNOWN. None of them is ever resent.
    """
    intent_id = _check_intent_id(intent_id)
    now_iso = _iso(now_ms)
    with _session(state, now_ms):
        _intent, invoice = _dispatched_intent(state, intent_id, "DRIVE_UPLOAD")
        outcome = _provider_outcome(evidence, "CREATED")
        if outcome == "DENIED":
            result = state.record_receipt(intent_id, outcome="REJECTED", provider_ref="gdrive:denied",
                                          provider_sha256="", run_id=run_id, now_iso=now_iso)
            return _result("drive-record", result, EXIT_HOLD, intent_id=intent_id)
        if outcome in ("ERROR", "NO_RESPONSE"):
            result = state.record_receipt(intent_id, outcome="UNKNOWN", provider_ref="gdrive:unknown",
                                          provider_sha256="", run_id=run_id, now_iso=now_iso)
            return _result("drive-record", result, EXIT_HOLD, intent_id=intent_id)
        file = evidence.get("file")
        if type(file) is not dict or type(file.get("id")) is not str or not _PROVIDER_ID_RE.fullmatch(file["id"]):
            raise NativeRefusal("EG_NATIVE_DRIVE_EVIDENCE_INVALID")
        provider_ref = "gdrive:" + file["id"]
        size = file.get("fileSize")
        metadata_ok = (
            file.get("parentId") == cfg.drive.folder_ids[invoice["stream"]]
            and file.get("title") == invoice["display_filename"]
            and file.get("mimeType") == PDF_MIME
            and type(size) in (str, int) and str(size) == str(invoice["byte_size"])
        )
        if not metadata_ok:
            result = state.record_receipt(intent_id, outcome="REJECTED", provider_ref=provider_ref,
                                          provider_sha256="", run_id=run_id, now_iso=now_iso)
            return _result("drive-record", result, EXIT_HOLD, intent_id=intent_id,
                           support_ref="EG_NATIVE_DRIVE_METADATA_MISMATCH")
        attestation = _attestation(cfg, intent_id, "drive", file["id"])
        if attestation is None:
            # Nothing is written: the intent stays DISPATCHED until a readback is attested.
            raise NativeRefusal("EG_NATIVE_DRIVE_READBACK_REQUIRED")
        verified = attestation.get("matches_archive") is True and attestation.get("sha256") == invoice["sha256"]
        result = state.record_receipt(
            intent_id, outcome="VERIFIED" if verified else "REJECTED", provider_ref=provider_ref,
            provider_sha256=str(attestation.get("sha256", "")), run_id=run_id, now_iso=now_iso,
        )
    return _result("drive-record", result, EXIT_OK if result in ("VERIFIED", "REPLAY") else EXIT_HOLD,
                   intent_id=intent_id, provider_ref=provider_ref)


def _seal_file(cfg: NativeConfig, intent_id: str) -> Path:
    return _outbox(cfg) / f"{intent_id}.gmail-seal.json"


def gmail_seal(state, cfg: NativeConfig, intent_id: str, evidence: dict, *, run_id: str, now_ms: int) -> dict:
    """Verify a native Gmail draft against the Core, then seal and bind exactly that draft.

    evidence (normalised from get_draft): {"draft_id", "from", "to": [...], "cc": [], "bcc": [], "subject",
    "attachments": [{"filename", "mimeType", "size"}]}. `from` must be the configured Gmail account. A mismatching draft is refused with no state
    change: a draft is not delivery, so the host may discard it and draft again.
    """
    intent_id = _check_intent_id(intent_id)
    now_iso = _iso(now_ms)
    with _session(state, now_ms):
        _intent, invoice = _current_step(state, intent_id, "GMAIL_SEND")
        draft_id = evidence.get("draft_id")
        if type(draft_id) is not str or not _PROVIDER_ID_RE.fullmatch(draft_id):
            raise NativeRefusal("EG_NATIVE_DRAFT_EVIDENCE_INVALID")
        attachments = evidence.get("attachments")
        to = evidence.get("to")
        if type(to) is not list or type(attachments) is not list or any(type(item) is not dict for item in attachments):
            raise NativeRefusal("EG_NATIVE_DRAFT_EVIDENCE_INVALID")
        expected_to = sorted(item.lower() for item in cfg.gmail.to)
        sender = evidence.get("from")
        if (type(sender) is not str or sender.lower() != cfg.gmail.account.lower()
                or sorted(str(item).lower() for item in to) != expected_to
                or evidence.get("cc") != [] or evidence.get("bcc") != []
                or evidence.get("subject") != subject_for(cfg, invoice)):
            raise NativeRefusal("EG_NATIVE_DRAFT_ENVELOPE_MISMATCH")
        if (len(attachments) != 1 or attachments[0].get("filename") != invoice["display_filename"]
                or attachments[0].get("mimeType") != PDF_MIME
                or str(attachments[0].get("size")) != str(invoice["byte_size"])):
            raise NativeRefusal("EG_NATIVE_DRAFT_ATTACHMENT_MISMATCH")
        if cfg.gmail.require_attachment_readback:
            attestation = _attestation(cfg, intent_id, "gmail", draft_id)
            if attestation is None:
                raise NativeRefusal("EG_NATIVE_GMAIL_READBACK_REQUIRED")
            if attestation.get("matches_archive") is not True or attestation.get("sha256") != invoice["sha256"]:
                raise NativeRefusal("EG_NATIVE_DRAFT_BYTES_MISMATCH")
        drive_intent = state.intents_for_invoice(invoice["invoice_key"])["DRIVE_UPLOAD"]
        if drive_intent["state"] != "VERIFIED":
            raise NativeRefusal("EG_V4_DRIVE_NOT_VERIFIED")
        _check_ceiling(state, cfg, "GMAIL_SEND")
        if _verified_payload(state, cfg, intent_id, invoice, run_id, now_iso) is None:
            return _result("gmail-seal", "HOLD", EXIT_HOLD, intent_id=intent_id,
                           support_ref=state.intent(intent_id)["support_ref"])
        state.assert_lease_held()
        state.seal_dispatch(intent_id, run_id=run_id, now_iso=now_iso)
        # The binding is written after the durable seal. If it is lost, gmail-record refuses and the
        # intent becomes UNKNOWN: fail-closed, never a second send.
        _write_json_once(_seal_file(cfg, intent_id), {"intent_id": intent_id, "draft_id": draft_id})
    return _result("gmail-seal", "SEALED", EXIT_OK, intent_id=intent_id, send_draft_id=draft_id)


def gmail_record(state, cfg: NativeConfig, intent_id: str, evidence: dict, *, run_id: str, now_ms: int) -> dict:
    """Record the outcome of sending the one bound draft.

    evidence: {"outcome": "SENT" | "DENIED" | "ERROR" | "NO_RESPONSE", "draft_id", "message_id"}
    """
    intent_id = _check_intent_id(intent_id)
    now_iso = _iso(now_ms)
    with _session(state, now_ms):
        _intent, invoice = _dispatched_intent(state, intent_id, "GMAIL_SEND")
        outcome = _provider_outcome(evidence, "SENT")
        if outcome == "DENIED":
            result = state.record_receipt(intent_id, outcome="REJECTED", provider_ref="gmail:denied",
                                          provider_sha256="", run_id=run_id, now_iso=now_iso)
            return _result("gmail-record", result, EXIT_HOLD, intent_id=intent_id)
        if outcome in ("ERROR", "NO_RESPONSE"):
            result = state.record_receipt(intent_id, outcome="UNKNOWN", provider_ref="gmail:unknown",
                                          provider_sha256="", run_id=run_id, now_iso=now_iso)
            return _result("gmail-record", result, EXIT_HOLD, intent_id=intent_id)
        binding = _read_json(_seal_file(cfg, intent_id))
        draft_id, message_id = evidence.get("draft_id"), evidence.get("message_id")
        if binding is None or binding.get("draft_id") != draft_id:
            raise NativeRefusal("EG_NATIVE_DRAFT_NOT_BOUND")
        if type(message_id) is not str or not _PROVIDER_ID_RE.fullmatch(message_id):
            raise NativeRefusal("EG_NATIVE_GMAIL_EVIDENCE_INVALID")
        # The bytes were proven on the bound draft (gmail-seal), and Gmail sends a draft as is.
        result = state.record_receipt(
            intent_id, outcome="VERIFIED", provider_ref=f"gmail:{message_id};draft:{draft_id}",
            provider_sha256=invoice["sha256"], run_id=run_id, now_iso=now_iso,
        )
    return _result("gmail-record", result, EXIT_OK if result in ("VERIFIED", "REPLAY") else EXIT_HOLD,
                   intent_id=intent_id)


def stream_report(state) -> dict[str, dict]:
    """Per-stream drain truth, read only: DRAINED, PENDING (next step) or BLOCKED (with the held reference)."""
    from .orchestration import _v4_stream_step
    from .state import V4_STREAMS

    report: dict[str, dict] = {}
    for stream in V4_STREAMS:
        kind, reference, invoice, _intent = _v4_stream_step(state, stream)
        invoices = state.invoices_for_stream(stream)
        delivered = []
        detail = []
        for row in invoices:
            intents = state.intents_for_invoice(row["invoice_key"])
            if all(intents[action]["state"] == "VERIFIED" for action in ("DRIVE_UPLOAD", "GMAIL_SEND")):
                delivered.append(row["display_filename"])
            detail.append({
                "display_filename": row["display_filename"],
                "adopted_from_v3": row["predecessor_invoice_id"] is not None,
                **{action.lower(): {
                    "state": intents[action]["state"],
                    "support_ref": intents[action]["support_ref"],
                    # A receipt is shown only for a VERIFIED intent; a HOLD keeps its evidence in SQLite.
                    "receipt": (state.verified_provider_ref(intents[action]["intent_id"])
                                if intents[action]["state"] == "VERIFIED" else None),
                } for action in ("DRIVE_UPLOAD", "GMAIL_SEND")},
            })
        report[stream] = {
            "state": {"STEP": "PENDING", "BLOCKED": "BLOCKED", "IDLE": "DRAINED"}[kind],
            "support_ref": reference if kind == "BLOCKED" else None,
            "next_action": reference if kind == "STEP" else None,
            "admitted": len(invoices),
            "delivered": delivered,
            "invoices": detail,
        }
    return report


def status(state) -> dict:
    report = stream_report(state)
    order = ("BLOCKED", "PENDING", "DRAINED")
    overall = min((row["state"] for row in report.values()), key=order.index)
    return _result(
        "status", overall, EXIT_OK if overall != "BLOCKED" else EXIT_HOLD, streams=report,
        effects={"drive_dispatches": state.dispatch_count("DRIVE_UPLOAD"),
                 "gmail_dispatches": state.dispatch_count("GMAIL_SEND")},
    )
