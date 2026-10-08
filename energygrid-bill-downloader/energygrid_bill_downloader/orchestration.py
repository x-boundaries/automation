"""Deterministic EnergyGrid core commands for the bounded Claude orchestrator (#226 G3).

Claude sequences these commands; it never decides business state. Every command:

- takes the single-run lock without waiting (held -> exit 10, RUN_IN_PROGRESS,
  no mutation);
- opens only an existing COMPLETE_V3/RESUMABLE_V3 database and re-checks the
  private Drive binding against the ACTIVE SQLite binding;
- recomputes the plan and refuses (exit 64, EG_CORE_ACTION_NOT_PLANNED, no
  mutation) unless the requested command is the planned action for its stream;
- prints exactly one line of strict JSON carrying only fixed words, stream
  names, support references, counts and booleans - never an invoice, file or
  folder ID, date, path, amount, address or token;
- exits 0 (done or nothing to do), 10 (retryable), 20 (HOLD, conflict or
  uncertainty recorded) or 64 (refused, no mutation).

Every command result document also carries `disposition`, classified by the
core from the exact (command, outcome, exit code) tuple through one closed
table: CONTINUE (plan again), STREAM_STOPPED (this stream is stopped for the
current run; a stop row is written before the result is printed; plan again)
or RUN_STOP (end the run loop). A stopped stream is never planned again in the
same run and a direct command for it is refused; the next run ignores the row.

The caller supplies only the command and, where required, the stream.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import SUPPORT_REF_PATTERN, AppError, ConfigError, RunLockedError, StateError
from .publication import ensure_no_reparse_components, validate_pdf
from .state import (
    DRIVE_MAX_BYTES,
    DRIVE_VERIFICATION_UNAVAILABLE,
    RUN_STREAM_STOP_RESULTS,
    StateV3Store,
    StreamStateConflictError,
)


CORE_SCHEMA = "energygrid.core.v3"
PLAN_SCHEMA = "energygrid.core.plan.v3"
STATUS_SCHEMA = "energygrid.core.status.v3"
RESULT_SCHEMA = "energygrid.core.result.v3"
STREAMS = ("EB_BILL", "TENANT_BILL")
COMMANDS = ("plan", "status", "acquire", "drive-intent", "drive-upload", "drive-reconcile", "deliver")
STREAM_COMMANDS = frozenset({"drive-intent", "drive-upload", "drive-reconcile", "deliver"})
ACTION_COMMAND = {
    "ACQUIRE_LATEST": "acquire",
    "DRIVE_PREPARE": "drive-intent",
    "DRIVE_UPLOAD": "drive-upload",
    "DRIVE_RECONCILE": "drive-reconcile",
    "EMAIL_DELIVER": "deliver",
    "EMAIL_RECONCILE": "deliver",
}
ACTIONS = ("NOTHING_TO_DO", "ACQUIRE_LATEST", "DRIVE_PREPARE", "DRIVE_RECONCILE", "DRIVE_UPLOAD",
           "EMAIL_RECONCILE", "EMAIL_DELIVER", "HOLD")
# The exact eleven command strings the Claude envelope may execute.
ALLOWED_COMMAND_LINES = (
    "egcore.cmd plan",
    "egcore.cmd status",
    "egcore.cmd acquire",
    "egcore.cmd drive-intent --stream EB_BILL",
    "egcore.cmd drive-intent --stream TENANT_BILL",
    "egcore.cmd drive-reconcile --stream EB_BILL",
    "egcore.cmd drive-reconcile --stream TENANT_BILL",
    "egcore.cmd drive-upload --stream EB_BILL",
    "egcore.cmd drive-upload --stream TENANT_BILL",
    "egcore.cmd deliver --stream EB_BILL",
    "egcore.cmd deliver --stream TENANT_BILL",
)
BUSINESS_OUTCOMES = ("NO_WORK", "COMPLETED", "HOLD", "SOURCE_FAILURE_RETRYABLE", "SOURCE_FAILURE",
                     "DRIVE_UNCERTAIN", "DRIVE_CONFLICT", "EMAIL_UNCERTAIN", "INCOMPLETE")
# Most severe first; mirrors the supervisor exit-code severity order
# 89 > 85 > 86 > 84 > 83 > 81 > 82 > 0, so INCOMPLETE is never hidden by a HOLD.
OUTCOME_SEVERITY = ("INCOMPLETE", "DRIVE_CONFLICT", "EMAIL_UNCERTAIN", "DRIVE_UNCERTAIN", "SOURCE_FAILURE", "HOLD",
                    "SOURCE_FAILURE_RETRYABLE", "COMPLETED", "NO_WORK")

# #226 G2 fairness reclosure: the closed disposition taxonomy. Classification is
# by the exact (command, outcome, exit code) tuple, never by exit code alone:
# exit 20 is both a stream HOLD and the CLI's global FAILED, and exit 10 is both
# RESERVATION_UNAVAILABLE (one stream) and RUN_IN_PROGRESS (global).
DISPOSITIONS = ("CONTINUE", "STREAM_STOPPED", "RUN_STOP")
CONTINUE_RESULTS = frozenset({
    ("acquire", "ACQUIRED", 0),
    ("acquire", "ACQUIRED_WITH_HOLDS", 0),
    ("drive-intent", "INTENT_CREATED", 0),
    ("drive-intent", "INTENT_EXISTS", 0),
    ("drive-upload", "DRIVE_VERIFIED", 0),
    ("drive-reconcile", "DRIVE_VERIFIED", 0),
    ("drive-reconcile", "RETRY_READY", 0),
    ("drive-reconcile", "RETRY_AUTHORISED", 0),
    ("deliver", "DELIVERED", 0),
})
STREAM_STOPPED_RESULTS = RUN_STREAM_STOP_RESULTS
RUN_STOP_RESULTS = frozenset({("acquire", "ACQUIRE_FAILED", 20)})
# For any command: a refusal, run-lock contention, or the CLI's deterministic
# failure document for an application error that escaped a handler.
RUN_STOP_ANY_COMMAND = frozenset({("REFUSED", 64), ("RUN_IN_PROGRESS", 10), ("FAILED", 10), ("FAILED", 20)})


def result_disposition(command: str | None, outcome: str, exit_code: int) -> str:
    """Classify one command result; an unknown tuple is a broken invariant."""
    key = (command, outcome, exit_code)
    if key in CONTINUE_RESULTS:
        return "CONTINUE"
    if key in STREAM_STOPPED_RESULTS:
        return "STREAM_STOPPED"
    if key in RUN_STOP_RESULTS or (outcome, exit_code) in RUN_STOP_ANY_COMMAND:
        return "RUN_STOP"
    raise StateError("core result is outside the closed disposition table")


class CoreRefusal(Exception):
    """A refused command: exit 64, no mutation."""

    def __init__(self, support_ref: str) -> None:
        super().__init__(support_ref)
        self.support_ref = support_ref


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def command_line(command: str, stream: str | None) -> str:
    return f"egcore.cmd {command}" + (f" --stream {stream}" if stream else "")


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _stream_context(config, state: StateV3Store, run: dict | None, run_id: str, stream: str) -> dict[str, Any]:
    """Compute one stream's planned action plus the private rows behind it."""
    ctx: dict[str, Any] = {
        "action": "HOLD", "archive": "NONE", "drive": "NOT_UPLOADED", "email": "NONE",
        "support_ref": None, "invoice": None, "operation": None, "binding": None, "delivery": None,
        "acquire": None, "legacy_local_stage": False,
    }

    def hold(ref: str) -> dict[str, Any]:
        ctx["action"] = "HOLD"
        ctx["support_ref"] = ref
        return ctx

    stream_row = state.stream(stream)
    invoice = None
    if stream_row is not None and stream_row.get("watermark_invoice_id"):
        invoice = state.invoice(stream_row["watermark_invoice_id"])
    if invoice is not None:
        ctx["invoice"] = invoice
        ctx["archive"] = "COMMITTED" if invoice["archive_state"] == "COMMITTED" else "NOT_COMMITTED"
        ctx["legacy_local_stage"] = invoice.get("drive_state") == "DRIVE_STAGED"
        delivery = state.delivery_for_invoice(invoice["invoice_id"])
        ctx["delivery"] = delivery
        if delivery is not None:
            ctx["email"] = delivery["state"]
    binding = state.active_binding(stream)
    ctx["binding"] = binding
    if invoice is not None and binding is not None:
        operation = state.drive_operation_for(invoice["invoice_id"], binding["binding_id"])
        ctx["operation"] = operation
        if operation is not None:
            ctx["drive"] = operation["state"]

    if run is None:
        ctx["action"] = "ACQUIRE_LATEST"
        return ctx
    key = "eb_bill" if stream == "EB_BILL" else "tenant_bill"
    acquired = run[f"{key}_acquire"]
    ctx["acquire"] = acquired
    if acquired == "UNBOUND":
        return hold(run[f"{key}_support_ref"] or "EG_STREAM_UNBOUND")
    if acquired in {"SOURCE_FAILURE", "SOURCE_FAILURE_RETRYABLE", "HOLD", "NOT_REACHED"}:
        return hold(run[f"{key}_support_ref"] or "EG_ACQUIRE_STREAM_HOLD")
    if acquired == "EMPTY":
        ctx["action"] = "NOTHING_TO_DO"
        return ctx
    if invoice is None or invoice.get("classification") != "CLASSIFIED":
        return hold("EG_CORE_LATEST_UNAVAILABLE")
    if invoice["archive_state"] != "COMMITTED":
        return hold("EG_ARCHIVE_NOT_COMMITTED")
    configured = config.drive.bindings.get(stream)
    if configured is None and binding is None:
        return hold("EG_DRIVE_BINDING_UNBOUND")
    if (
        configured is None or binding is None or configured.binding_id != binding["binding_id"]
        or configured.folder_id != binding["folder_id"] or configured.root_folder_id != binding["root_folder_id"]
        or configured.account_ref != binding["account_ref"]
    ):
        return hold("EG_DRIVE_BINDING_MISMATCH")
    operation = ctx["operation"]
    if operation is None:
        if any(item["state"] == "DRIVE_VERIFIED" for item in state.drive_operations_for_invoice(invoice["invoice_id"])):
            return hold("EG_DRIVE_BINDING_CHANGED")
        ctx["action"] = "DRIVE_PREPARE"
        return ctx
    status = operation["state"]
    reconciled_now = operation["last_reconcile_run_id"] == run_id
    if state.open_drive_dispatch(operation["operation_id"]) is not None:
        ctx["action"] = "DRIVE_RECONCILE"
        return ctx
    if status == "DRIVE_UPLOAD_INTENT":
        if operation["upload_attempt_count"] == 0:
            ctx["action"] = "DRIVE_UPLOAD"
        elif reconciled_now and operation["last_reconcile_result"] == "NOT_FOUND":
            ctx["action"] = "DRIVE_UPLOAD"
        elif reconciled_now:
            return hold(operation["support_ref"] or "EG_DRIVE_RECONCILE_UNAVAILABLE")
        else:
            ctx["action"] = "DRIVE_RECONCILE"
        return ctx
    if status == "DRIVE_UPLOAD_UNCERTAIN":
        if reconciled_now:
            return hold("EG_DRIVE_UPLOAD_UNCERTAIN")
        ctx["action"] = "DRIVE_RECONCILE"
        return ctx
    if status == "HOLD" and operation["support_ref"] == DRIVE_VERIFICATION_UNAVAILABLE:
        if reconciled_now:
            return hold(DRIVE_VERIFICATION_UNAVAILABLE)
        ctx["action"] = "DRIVE_RECONCILE"
        return ctx
    if status in {"DRIVE_CONFLICT", "HOLD"}:
        return hold(operation["support_ref"] or "EG_DRIVE_HOLD")
    # DRIVE_VERIFIED: Drive-before-email is satisfied; now the email leg.
    delivery = ctx["delivery"]
    if delivery is None or (delivery["state"] == "PENDING_SEND" and delivery["dispatch_started_at_utc"] is None):
        ctx["action"] = "EMAIL_DELIVER"
    elif delivery["state"] == "PENDING_SEND":
        ctx["action"] = "EMAIL_RECONCILE"
    elif delivery["state"] == "DELIVERED":
        ctx["action"] = "NOTHING_TO_DO"
    else:
        return hold(delivery["support_ref"] or "EG_MAIL_TERMINAL")
    return ctx


def compute_plan(config, state: StateV3Store, run_id: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    run = state.run_record(run_id)
    contexts = {stream: _stream_context(config, state, run, run_id, stream) for stream in STREAMS}
    # Only this run's stop rows apply. The per-stream contexts (and therefore
    # status) are computed without them; they only exclude a stream from `next`.
    stopped = state.run_stream_stops(run_id)
    if run is None:
        next_step = {"action": "ACQUIRE_LATEST", "stream": None, "argv": command_line("acquire", None)}
    else:
        next_step = None
        for stream in STREAMS:
            action = contexts[stream]["action"]
            if stream not in stopped and action not in {"NOTHING_TO_DO", "HOLD"}:
                next_step = {"action": action, "stream": stream, "argv": command_line(ACTION_COMMAND[action], stream)}
                break
        if next_step is None:
            held = bool(stopped) or any(contexts[stream]["action"] == "HOLD" for stream in STREAMS)
            next_step = {"action": "HOLD" if held else "NOTHING_TO_DO", "stream": None, "argv": None}
    if next_step["argv"] is not None and next_step["argv"] not in ALLOWED_COMMAND_LINES:
        raise StateError("planned command is outside the reviewed allowlist")
    document = {
        "schema": PLAN_SCHEMA,
        "next": next_step,
        "streams": {
            stream: {
                "action": contexts[stream]["action"],
                "archive": contexts[stream]["archive"],
                "drive": contexts[stream]["drive"],
                "email": contexts[stream]["email"],
                "support_ref": contexts[stream]["support_ref"],
                "stopped_for_run": stream in stopped,
            }
            for stream in STREAMS
        },
    }
    for stream in STREAMS:
        contexts[stream]["stopped_for_run"] = stream in stopped
    return document, contexts


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _archive_matches(config, invoice: dict) -> bool:
    from .reconcile import _canonical_path

    try:
        path = _canonical_path(config.archive_root, invoice["stream"], invoice["canonical_filename"])
        ensure_no_reparse_components(path)
        info = validate_pdf(path)
    except (OSError, AppError):
        return False
    return info.byte_size == invoice["byte_size"] and info.sha256 == invoice["sha256"]


def _stream_outcome(config, ctx: dict[str, Any], run_id: str) -> tuple[str, bool]:
    """Return (business outcome, fully_handled) for one stream."""
    action = ctx["action"]
    operation = ctx["operation"]
    delivery = ctx["delivery"]
    if action == "ACQUIRE_LATEST":
        return "INCOMPLETE", False
    if ctx["acquire"] == "SOURCE_FAILURE_RETRYABLE":
        return "SOURCE_FAILURE_RETRYABLE", False
    if ctx["acquire"] == "SOURCE_FAILURE":
        return "SOURCE_FAILURE", False
    if action == "HOLD":
        if operation is not None and operation["state"] == "DRIVE_UPLOAD_UNCERTAIN":
            return "DRIVE_UNCERTAIN", False
        if operation is not None and operation["state"] == "DRIVE_UPLOAD_INTENT" and operation["upload_attempt_count"] >= 1:
            return "DRIVE_UNCERTAIN", False
        if operation is not None and operation["state"] in {"DRIVE_CONFLICT", "HOLD"}:
            return "DRIVE_CONFLICT", False
        if operation is not None and operation["state"] == "DRIVE_VERIFIED" and delivery is not None and delivery["state"] == "DELIVERY_OUTCOME_UNCERTAIN":
            return "EMAIL_UNCERTAIN", False
        return "HOLD", False
    if action != "NOTHING_TO_DO":
        return "INCOMPLETE", False
    if ctx["acquire"] == "EMPTY":
        return "NO_WORK", False
    invoice = ctx["invoice"]
    fully = (
        invoice is not None and operation is not None and operation["state"] == "DRIVE_VERIFIED"
        and delivery is not None and delivery["state"] == "DELIVERED"
        and delivery["byte_size"] == invoice["byte_size"] and delivery["sha256"] == invoice["sha256"]
        and _archive_matches(config, invoice)
    )
    if not fully:
        return "HOLD", False
    worked = operation["verified_run_id"] == run_id or delivery.get("outcome_run_id") == run_id
    return ("COMPLETED" if worked else "NO_WORK"), True


def compute_status(config, state: StateV3Store, run_id: str) -> dict[str, Any]:
    _plan, contexts = compute_plan(config, state, run_id)
    run = state.run_record(run_id)
    streams: dict[str, Any] = {}
    outcomes: list[str] = []
    uncertainty = False
    for stream in STREAMS:
        ctx = contexts[stream]
        outcome, fully = _stream_outcome(config, ctx, run_id)
        outcomes.append(outcome)
        operation = ctx["operation"]
        delivery = ctx["delivery"]
        if operation is not None and (
            operation["state"] == "DRIVE_UPLOAD_UNCERTAIN" or state.open_drive_dispatch(operation["operation_id"]) is not None
        ):
            uncertainty = True
        if delivery is not None and (
            delivery["state"] == "DELIVERY_OUTCOME_UNCERTAIN"
            or (delivery["state"] == "PENDING_SEND" and delivery["dispatch_started_at_utc"] is not None)
        ):
            uncertainty = True
        streams[stream] = {
            "latest_present": ctx["invoice"] is not None,
            "action": ctx["action"],
            "archive": ctx["archive"],
            "drive": ctx["drive"],
            "drive_verification_method": operation["verification_method"] if operation is not None else None,
            "drive_attempts": operation["upload_attempt_count"] if operation is not None else 0,
            "email": ctx["email"],
            "legacy_local_stage": ctx["legacy_local_stage"],
            "support_ref": ctx["support_ref"],
            "outcome": outcome,
            "fully_handled": fully,
            "stopped_for_run": ctx["stopped_for_run"],
        }
    business = next(item for item in OUTCOME_SEVERITY if item in outcomes)
    terminal = run is not None and all(contexts[stream]["action"] in {"NOTHING_TO_DO", "HOLD"} for stream in STREAMS)
    if not terminal:
        # Any planned step left (including an open Drive or email dispatch) means the
        # run is unfinished, whatever the other stream holds on.
        business = "INCOMPLETE"
    return {
        "schema": STATUS_SCHEMA,
        "run": {
            "acquire": run["acquire_state"] if run is not None else "NOT_RUN",
            "acquire_exit_code": run["acquire_exit_code"] if run is not None else None,
        },
        "streams": streams,
        "terminal": terminal,
        "uncertainty_outstanding": uncertainty,
        "business_outcome": business,
    }


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

class CoreContext:
    """Injected collaborators; production wiring lives in cli.py."""

    def __init__(
        self,
        config,
        *,
        run_id: str,
        logger=None,
        adapters_factory: Callable[[], dict[str, Any]] | None = None,
        drive_client=None,
        delivery_client=None,
        lock_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self.config = config
        self.run_id = run_id
        self.logger = logger
        self.adapters_factory = adapters_factory
        self.drive_client = drive_client
        self.delivery_client = delivery_client
        if lock_factory is None:
            from .run_lock import RunLock

            lock_factory = RunLock
        self.lock_factory = lock_factory

    def log(self, phase: str, status: str, **fields: Any) -> None:
        try:
            if self.logger is not None:
                self.logger.event(phase, status=status, **fields)
        except Exception:
            return None


def run_command(command: str, stream: str | None, core: CoreContext) -> tuple[dict[str, Any], int]:
    """Execute one core command; return (document, exit code). Never raises for
    expected refusals; unexpected application errors propagate to the CLI."""
    if command not in COMMANDS:
        return _result(command, stream, "REFUSED", "EG_CORE_COMMAND_UNKNOWN", False, 64), 64
    if (command in STREAM_COMMANDS) != (stream is not None) or (stream is not None and stream not in STREAMS):
        return _result(command, stream, "REFUSED", "EG_CORE_ARGUMENTS_INVALID", False, 64), 64
    if command_line(command, stream) not in ALLOWED_COMMAND_LINES:
        return _result(command, stream, "REFUSED", "EG_CORE_ARGUMENTS_INVALID", False, 64), 64
    config = core.config
    try:
        with core.lock_factory(config.state_path.parent):
            read_only = command in {"plan", "status"}
            with StateV3Store(config.state_path, read_only=read_only) as state:
                if command == "plan":
                    document, _contexts = compute_plan(config, state, core.run_id)
                    return document, 0
                if command == "status":
                    return compute_status(config, state, core.run_id), 0
                _plan, contexts = compute_plan(config, state, core.run_id)
                if command == "acquire":
                    if state.run_record(core.run_id) is not None:
                        raise CoreRefusal("EG_CORE_ACTION_NOT_PLANNED")
                    return _acquire(core, state)
                ctx = contexts[stream]
                if ctx["stopped_for_run"]:
                    # Even a misbehaving caller cannot retry a stopped stream this run.
                    raise CoreRefusal("EG_CORE_STREAM_STOPPED_FOR_RUN")
                if ACTION_COMMAND.get(ctx["action"]) != command:
                    raise CoreRefusal("EG_CORE_ACTION_NOT_PLANNED")
                handler = {
                    "drive-intent": _drive_intent,
                    "drive-upload": _drive_upload,
                    "drive-reconcile": _drive_reconcile,
                    "deliver": _deliver,
                }[command]
                document, exit_code = handler(core, state, stream, ctx)
                if document["disposition"] == "STREAM_STOPPED":
                    # Durable and read back under the run lock before the result is printed.
                    state.insert_run_stream_stop(
                        run_id=core.run_id, stream=stream, command=command, outcome=document["outcome"],
                        exit_code=exit_code, support_ref=document["support_ref"], timestamp=utc_now(),
                    )
                return document, exit_code
    except CoreRefusal as refusal:
        core.log("core_command", "REFUSED", stream=stream, support_ref=refusal.support_ref)
        return _result(command, stream, "REFUSED", refusal.support_ref, False, 64), 64
    except RunLockedError:
        return _result(command, stream, "RUN_IN_PROGRESS", "EG_RUN_ALREADY_ACTIVE", False, 10), 10


def fallback_result(command: str, stream: str | None, error: AppError) -> tuple[dict[str, Any], int]:
    """The deterministic CLI document for an error that escaped `run_command`
    (configuration refused, broken invariant, unreadable state): always RUN_STOP."""
    if isinstance(error, ConfigError):
        return _result(command, stream, "REFUSED", "EG_CORE_CONFIG_INVALID", False, 64), 64
    ref = getattr(error, "support_ref", None)
    exit_code = error.exit_code if error.exit_code in {10, 20} else 20
    support_ref = ref if type(ref) is str and re.fullmatch(SUPPORT_REF_PATTERN, ref) else "EG_CORE_FAILURE"
    return _result(command, stream, "FAILED", support_ref, None, exit_code), exit_code


def _result(command: str, stream: str | None, outcome: str, support_ref: str | None, mutated: bool | None,
            exit_code: int) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "command": command,
        "stream": stream,
        "outcome": outcome,
        "support_ref": support_ref,
        "mutated": mutated,
        "disposition": result_disposition(command, outcome, exit_code),
    }


def _finish(core: CoreContext, command: str, stream: str | None, outcome: str, support_ref: str | None,
            mutated: bool, exit_code: int) -> tuple[dict[str, Any], int]:
    document = _result(command, stream, outcome, support_ref, mutated, exit_code)
    core.log("core_command", outcome, stream=stream, support_ref=support_ref)
    return document, exit_code


def _acquire(core: CoreContext, state: StateV3Store) -> tuple[dict[str, Any], int]:
    from .reconcile import reconcile_dual_stream

    started = utc_now()
    if core.adapters_factory is None:
        raise ConfigError("acquire requires the configured source adapters")
    results: dict[str, tuple[str, str | None]] = {}
    try:
        adapters = core.adapters_factory()
        summary = reconcile_dual_stream(core.config, adapters, state, core.logger, core.run_id)
    except StateError:
        raise
    except AppError as error:
        ref = getattr(error, "support_ref", None) or "EG_ACQUIRE_FAILED"
        state.insert_run_record(
            run_id=core.run_id, started_at_utc=started, acquire_state="FAILED", acquire_exit_code=20,
            acquire_support_ref=ref if isinstance(ref, str) and ref.startswith("EG_") else "EG_ACQUIRE_FAILED",
            streams={}, timestamp=utc_now(),
        )
        return _finish(core, "acquire", None, "ACQUIRE_FAILED", "EG_ACQUIRE_FAILED", True, 20)
    for detail in summary.stream_results:
        name = detail["stream"]
        status = detail["status"]
        ref = detail.get("support_ref")
        if status in {"ARCHIVE_READY", "ALREADY_HANDLED"}:
            results[name] = ("READY", None)
        elif status == "EMPTY":
            results[name] = ("EMPTY", None)
        elif status == "UNBOUND":
            results[name] = ("UNBOUND", ref or "EG_STREAM_UNBOUND")
        elif status == "SOURCE_FAILURE":
            retryable = summary.stream_exit_codes.get(name) == 10
            results[name] = ("SOURCE_FAILURE_RETRYABLE" if retryable else "SOURCE_FAILURE", ref or "EG_SOURCE_FAILURE")
        else:
            results[name] = ("HOLD", ref or "EG_ACQUIRE_STREAM_HOLD")
    exit_code = summary.exit_code if summary.exit_code in {0, 10, 20} else 20
    state.insert_run_record(
        run_id=core.run_id, started_at_utc=started,
        acquire_state="COMPLETED" if exit_code == 0 else "FAILED",
        acquire_exit_code=exit_code, acquire_support_ref=None if exit_code == 0 else "EG_ACQUIRE_INCOMPLETE",
        streams=results, timestamp=utc_now(),
    )
    # Per-stream holds (an UNBOUND stream, a source failure, an ambiguous
    # latest) are durable in the run row and surface through plan/status, so
    # one stream's hold never stops the other stream's Drive/email steps.
    outcome = "ACQUIRED" if exit_code == 0 else "ACQUIRED_WITH_HOLDS"
    return _finish(core, "acquire", None, outcome, None if exit_code == 0 else "EG_ACQUIRE_INCOMPLETE", True, 0)


def _read_archive(core: CoreContext, invoice: dict) -> tuple[bytes, Path]:
    """Re-check the canonical local archive bytes against the committed facts."""
    from .reconcile import _canonical_path

    path = _canonical_path(core.config.archive_root, invoice["stream"], invoice["canonical_filename"])
    ensure_no_reparse_components(core.config.archive_root)
    ensure_no_reparse_components(path)
    info = validate_pdf(path)
    if info.byte_size != invoice["byte_size"] or info.sha256 != invoice["sha256"] or info.byte_size > DRIVE_MAX_BYTES:
        raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_ARCHIVE_FACTS_CONFLICT")
    with path.open("rb") as handle:
        payload = handle.read(DRIVE_MAX_BYTES + 1)
    if len(payload) != invoice["byte_size"] or hashlib.sha256(payload).hexdigest() != invoice["sha256"]:
        raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_ARCHIVE_FACTS_CONFLICT")
    return payload, path


def _recheck_archive(core: CoreContext, invoice: dict, operation: dict) -> bytes | None:
    from .drive import file_md5

    try:
        payload, _path = _read_archive(core, invoice)
    except (OSError, AppError):
        return None
    if file_md5(payload) != operation["local_md5"] or len(payload) != operation["local_byte_size"]:
        return None
    return payload


def _binding_still_current(core: CoreContext, state: StateV3Store, operation: dict) -> bool:
    binding = state.active_binding(operation["stream"])
    configured = core.config.drive.bindings.get(operation["stream"])
    return (
        binding is not None and configured is not None and binding["binding_id"] == operation["binding_id"]
        and configured.binding_id == binding["binding_id"] and binding["folder_id"] == operation["folder_id"]
    )


def _drive_intent(core: CoreContext, state: StateV3Store, stream: str, ctx: dict) -> tuple[dict, int]:
    from .drive import file_md5

    invoice = ctx["invoice"]
    try:
        payload, _path = _read_archive(core, invoice)
    except StreamStateConflictError as error:
        return _finish(core, "drive-intent", stream, "HOLD", error.support_ref, False, 20)
    except (OSError, AppError):
        return _finish(core, "drive-intent", stream, "HOLD", "EG_DRIVE_ARCHIVE_UNAVAILABLE", False, 20)
    _row, created = state.create_drive_intent(
        invoice=invoice, binding=ctx["binding"], local_md5=file_md5(payload),
        operation_id=str(uuid.uuid4()), run_id=core.run_id, timestamp=utc_now(),
    )
    return _finish(core, "drive-intent", stream, "INTENT_CREATED" if created else "INTENT_EXISTS", None, created, 0)


def _apply_verdict(core: CoreContext, state: StateV3Store, operation: dict, verdict, *, command: str,
                   dispatch_outcome: str | None, payload_check: bool = True) -> tuple[dict, int]:
    """Map a core verdict onto exactly one durable state change."""
    stream = operation["stream"]
    op_id = operation["operation_id"]
    status = operation["state"]
    from_hold = status == "HOLD"
    kind = verdict.kind
    finish = lambda **kwargs: state.finish_drive_operation(op_id, run_id=core.run_id, timestamp=utc_now(),
                                                            dispatch_outcome=dispatch_outcome, **kwargs)
    reconcile_result = None if command == "drive-upload" else {
        "FOUND_EXACT": "FOUND_EXACT", "NOT_FOUND": "NOT_FOUND", "CHECKSUM_UNAVAILABLE": "CHECKSUM_UNAVAILABLE",
        "CONFLICT": "CONFLICT", "DESTINATION_CHANGED": "DESTINATION_CHANGED", "UNAVAILABLE": "UNAVAILABLE",
        "REJECTED": "UNAVAILABLE",
    }[kind]
    if kind == "FOUND_EXACT":
        invoice = state.invoice(operation["invoice_id"])
        if payload_check and (invoice is None or _recheck_archive(core, invoice, operation) is None):
            if from_hold:
                finish(new_state=None, reconcile_result=reconcile_result)
                return _finish(core, command, stream, "HOLD", "EG_DRIVE_ARCHIVE_FACTS_CONFLICT", True, 20)
            finish(new_state="HOLD", support_ref="EG_DRIVE_ARCHIVE_FACTS_CONFLICT", reconcile_result=reconcile_result)
            return _finish(core, command, stream, "HOLD", "EG_DRIVE_ARCHIVE_FACTS_CONFLICT", True, 20)
        if not _binding_still_current(core, state, operation):
            if from_hold:
                finish(new_state=None, reconcile_result=reconcile_result)
            else:
                finish(new_state="HOLD", support_ref="EG_DRIVE_BINDING_MISMATCH", reconcile_result=reconcile_result)
            return _finish(core, command, stream, "HOLD", "EG_DRIVE_BINDING_MISMATCH", True, 20)
        if verdict.receipt is None or verdict.receipt["remote_file_id"] != operation["reserved_remote_file_id"]:
            raise StateError("Drive receipt identity does not match the reserved file ID")
        finish(new_state="DRIVE_VERIFIED", receipt=verdict.receipt, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "DRIVE_VERIFIED", None, True, 0)
    if from_hold:
        # HOLD(EG_DRIVE_VERIFICATION_UNAVAILABLE) only ever leaves to VERIFIED.
        finish(new_state=None, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "HOLD", DRIVE_VERIFICATION_UNAVAILABLE, True, 20)
    if kind == "CONFLICT":
        finish(new_state="DRIVE_CONFLICT", support_ref=verdict.support_ref, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "DRIVE_CONFLICT", verdict.support_ref, True, 20)
    if kind == "CHECKSUM_UNAVAILABLE":
        finish(new_state="HOLD", support_ref=DRIVE_VERIFICATION_UNAVAILABLE, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "HOLD", DRIVE_VERIFICATION_UNAVAILABLE, True, 20)
    if kind in {"DESTINATION_CHANGED", "REJECTED"}:
        finish(new_state="HOLD", support_ref=verdict.support_ref, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "HOLD", verdict.support_ref, True, 20)
    if command == "drive-upload":
        # Any other result after a committed dispatch is uncertain: the create
        # may have happened. Only the reserved ID can ever be reconciled later.
        ref = "EG_DRIVE_UPLOAD_NOT_OBSERVED" if kind == "NOT_FOUND" else verdict.support_ref
        finish(new_state="DRIVE_UPLOAD_UNCERTAIN", support_ref=ref)
        return _finish(core, command, stream, "DRIVE_UPLOAD_UNCERTAIN", ref, True, 20)
    # drive-reconcile
    if kind == "UNAVAILABLE":
        finish(new_state=None, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "DRIVE_UPLOAD_UNCERTAIN", "EG_DRIVE_RECONCILE_UNAVAILABLE", True, 20)
    # NOT_FOUND: the reserved ID is positively absent and both complete
    # conflict searches are empty. Only the core may authorise a retry.
    if status == "DRIVE_UPLOAD_INTENT":
        finish(new_state=None, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "RETRY_READY", None, True, 0)
    attempts = state.drive_dispatches(op_id)
    if operation["upload_attempt_count"] >= 2:
        finish(new_state="HOLD", support_ref="EG_DRIVE_RETRY_EXHAUSTED", reconcile_result=reconcile_result)
        return _finish(core, command, stream, "HOLD", "EG_DRIVE_RETRY_EXHAUSTED", True, 20)
    if any(item["dispatch_run_id"] == core.run_id for item in attempts):
        # Retry may happen only in a later run than the uncertain attempt.
        finish(new_state=None, reconcile_result=reconcile_result)
        return _finish(core, command, stream, "DRIVE_UPLOAD_UNCERTAIN", "EG_DRIVE_RETRY_NEXT_RUN", True, 20)
    invoice = state.invoice(operation["invoice_id"])
    if invoice is None or _recheck_archive(core, invoice, operation) is None or not _binding_still_current(core, state, operation):
        finish(new_state="HOLD", support_ref="EG_DRIVE_RETRY_PRECONDITION_FAILED", reconcile_result=reconcile_result)
        return _finish(core, command, stream, "HOLD", "EG_DRIVE_RETRY_PRECONDITION_FAILED", True, 20)
    finish(new_state="DRIVE_UPLOAD_INTENT", reconcile_result=reconcile_result, authorise_retry=True)
    return _finish(core, command, stream, "RETRY_AUTHORISED", None, True, 0)


def _require_drive_client(core: CoreContext):
    if core.drive_client is None:
        raise ConfigError("Drive client is not configured")
    return core.drive_client


def _drive_upload(core: CoreContext, state: StateV3Store, stream: str, ctx: dict) -> tuple[dict, int]:
    from .drive import operation_request

    client = _require_drive_client(core)
    operation = ctx["operation"]
    binding = ctx["binding"]
    invoice = ctx["invoice"]
    payload = _recheck_archive(core, invoice, operation)
    if payload is None:
        return _finish(core, "drive-upload", stream, "HOLD", "EG_DRIVE_ARCHIVE_FACTS_CONFLICT", False, 20)
    try:
        client.authorization()
    except StateError:
        return _finish(core, "drive-upload", stream, "HOLD", "EG_DRIVE_AUTH_UNAVAILABLE", False, 20)
    mutated = False
    if operation["reserved_remote_file_id"] is None:
        # Amendment A: obtain exactly one pre-generated Drive file ID and
        # persist it before any dispatch marker. No file is created here, so a
        # lost or invalid reservation simply means zero upload this run.
        metadata = operation_request("RESERVE_ID", operation, binding["root_folder_id"])
        result = client.send(client.prepare(metadata, None), metadata)
        if result is None or result["outcome"] != "ID_RESERVED":
            if result is not None and result["outcome"] == "DESTINATION_CHANGED":
                state.finish_drive_operation(operation["operation_id"], run_id=core.run_id, timestamp=utc_now(),
                                             new_state="HOLD", support_ref="EG_DRIVE_DESTINATION_CHANGED")
                return _finish(core, "drive-upload", stream, "HOLD", "EG_DRIVE_DESTINATION_CHANGED", True, 20)
            return _finish(core, "drive-upload", stream, "RESERVATION_UNAVAILABLE", "EG_DRIVE_RESERVATION_UNAVAILABLE", False, 10)
        state.reserve_drive_file_id(operation["operation_id"], result["generated_id"], core.run_id, utc_now())
        mutated = True
        operation = state.drive_operation(operation["operation_id"])
        if operation is None or operation["reserved_remote_file_id"] is None:
            raise StateError("reserved Drive file ID was not durable")
    metadata = operation_request("UPLOAD_IF_ABSENT", operation, binding["root_folder_id"])
    prepared = client.prepare(metadata, payload)
    state.begin_drive_dispatch(operation["operation_id"], core.run_id, utc_now())
    core.log("drive_dispatch", "COMMITTED", stream=stream)
    from .drive import verify_result

    result = client.send(prepared, metadata)
    operation = state.drive_operation(operation["operation_id"])
    verdict = verify_result(operation, result)
    return _apply_verdict(
        core, state, operation, verdict, command="drive-upload",
        dispatch_outcome="VALID_RESULT" if result is not None else "NO_VALID_RESULT",
    )


def _drive_reconcile(core: CoreContext, state: StateV3Store, stream: str, ctx: dict) -> tuple[dict, int]:
    from .drive import operation_request, verify_result

    client = _require_drive_client(core)
    operation = ctx["operation"]
    binding = ctx["binding"]
    if state.open_drive_dispatch(operation["operation_id"]) is not None:
        # A crash after the dispatch marker: the create may have happened.
        operation = state.finish_drive_operation(
            operation["operation_id"], run_id=core.run_id, timestamp=utc_now(),
            new_state="DRIVE_UPLOAD_UNCERTAIN" if operation["state"] == "DRIVE_UPLOAD_INTENT" else None,
            support_ref="EG_DRIVE_RECOVERY_UNCERTAIN", dispatch_outcome="RECOVERED_MARKER",
            dispatch_support_ref="EG_DRIVE_RECOVERED_MARKER",
        )
    if operation["reserved_remote_file_id"] is None:
        raise StateError("Drive reconciliation requires the reserved file ID")
    try:
        client.authorization()
    except StateError:
        return _finish(core, "drive-reconcile", stream, "HOLD", "EG_DRIVE_AUTH_UNAVAILABLE", True, 20)
    metadata = operation_request("RECONCILE", operation, binding["root_folder_id"])
    result = client.send(client.prepare(metadata, None), metadata)
    verdict = verify_result(operation, result)
    return _apply_verdict(core, state, operation, verdict, command="drive-reconcile", dispatch_outcome=None)


def _deliver(core: CoreContext, state: StateV3Store, stream: str, ctx: dict) -> tuple[dict, int]:
    invoice = ctx["invoice"]
    operation = ctx["operation"]
    if operation is None or operation["state"] != "DRIVE_VERIFIED" or not _binding_still_current(core, state, operation):
        raise CoreRefusal("EG_CORE_DRIVE_NOT_VERIFIED")
    try:
        _payload, path = _read_archive(core, invoice)
    except (OSError, AppError):
        return _finish(core, "deliver", stream, "HOLD", "EG_DELIVERY_ARCHIVE_FACTS_CONFLICT", False, 20)
    if core.delivery_client is None:
        raise ConfigError("delivery client is not configured")
    try:
        outcome = core.delivery_client.deliver(state, invoice, path, core.run_id, logger=core.logger)
    except StreamStateConflictError as error:
        return _finish(core, "deliver", stream, "HOLD", error.support_ref, True, 20)
    except AppError:
        # Includes the unchanged DeliveryClient's StateError for missing or
        # invalid webhook authentication: no POST happened, nothing is resent.
        return _finish(core, "deliver", stream, "HOLD", "EG_DELIVERY_PREPARATION_FAILED", True, 20)
    if outcome.state == "DELIVERED":
        return _finish(core, "deliver", stream, "DELIVERED", outcome.support_ref, True, 0)
    return _finish(core, "deliver", stream, outcome.state, outcome.support_ref, True, 20)
