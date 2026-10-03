from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
import re
import uuid

from .config import MAX_INVENTORY_CEILING, RuntimeConfig, is_within
from .errors import (
    ACTION_REQUIRED,
    ALREADY_PRESENT,
    ARCHIVE_CONFLICT,
    DOWNLOADED,
    DOWNLOAD_FAILED,
    INVALID_PDF,
    NO_NEW_BILLS,
    PORTAL_LAYOUT_CHANGED,
    RETRYABLE_NETWORK_FAILURE,
    STATE_INCONSISTENT,
    AppError,
    ArchiveConflictError,
    ConfigError,
    InvalidPdfError,
    SUPPORT_REF_PATTERN,
    SourceContractError,
    StateError,
    exit_code_for,
)
from .publication import (
    FileInfo,
    cleanup_run_directory,
    create_run_directory,
    filename_key,
    publish_no_replace,
    validate_filename,
    validate_pdf,
)
from .portal import (
    DOWNLOAD_PREFLIGHT_CHECKPOINTS,
    DOWNLOAD_PREFLIGHT_REASONS,
    DownloadPreflightError,
    InvoiceRow,
)
from .state import BillRecord, StateStore


class PortalProtocol(Protocol):
    def inventory(self, safety_ceiling: int) -> list[InvoiceRow]: ...

    def download(self, row: InvoiceRow, destination: Path) -> str: ...


class LoggerProtocol(Protocol):
    def event(self, phase: str, status: str | None = None, **fields: Any) -> None: ...


@dataclass
class RunSummary:
    run_id: str
    status: str
    exit_code: int
    inventory_count: int = 0
    downloaded_count: int = 0
    present_count: int = 0
    failure_count: int = 0
    failures: list[str] = field(default_factory=list)
    # DL-XB-199 G3-101 (direct HTTP only). `pending_count` is reported by
    # `list`; the stage, reference and fetch count feed the alert and the log,
    # never stdout.
    pending_count: int | None = None
    failure_stage: str | None = None
    failure_support_ref: str | None = None
    fetch_count: int = 0
    archive_reused_count: int = 0
    drive_staged_count: int = 0
    delivered_count: int = 0
    handled_count: int = 0
    uncertain_count: int = 0
    stream_results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        document = self._base_dict()
        if self.pending_count is not None:
            document["pending_count"] = self.pending_count
        if self.stream_results:
            document.update(
                {
                    "archive_reused_count": self.archive_reused_count,
                    "archive_staged_count": self.downloaded_count,
                    "drive_staged_count": self.drive_staged_count,
                    "delivered_count": self.delivered_count,
                    "handled_count": self.handled_count,
                    "uncertain_count": self.uncertain_count,
                    "stream_results": self.stream_results,
                }
            )
        return document

    def _base_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "inventory_count": self.inventory_count,
            "downloaded_count": self.downloaded_count,
            "present_count": self.present_count,
            "failure_count": self.failure_count,
            "failure_classes": sorted(set(self.failures)),
        }


@dataclass
class _Acquisition:
    """One Phase-A row: an owned temp source and its authoritative name.

    The filename is private: it is excluded from `repr`, and nothing derived
    from it is ever logged or emitted.
    """

    ordinal: int
    run_dir: Path
    temp_path: Path
    filename: str = field(repr=False)
    info: FileInfo = field(repr=False)
    key: str = field(default="", repr=False)
    final_path: Path | None = field(default=None, repr=False)
    preserve: bool = False


def reconcile_inventory(
    config: RuntimeConfig,
    portal: PortalProtocol,
    state: StateStore,
    logger: LoggerProtocol,
    run_id: str,
    list_only: bool = False,
) -> RunSummary:
    """Download-first reconciliation (DL-XB-199, G2-076).

    Phase A inventories once and acquires every row in frozen table order, each
    into its own owned temp directory, learning the invoice name only from the
    browser's suggested filename. No state is written and nothing is published
    until every row has been acquired and every normalised filename key has
    proven unique. Phase B then reconciles each acquisition by filename key
    with the existing state/archive primitives. Identical bytes under
    different filename keys are allowed: there is no cross-key hash identity.
    """

    summary = RunSummary(run_id=run_id, status=NO_NEW_BILLS, exit_code=0)
    logger.event("inventory_start")
    rows = portal.inventory(config.inventory_safety_ceiling)
    summary.inventory_count = len(rows)
    logger.event("inventory_complete", inventory_count=len(rows))

    if not rows:
        return summary
    if list_only:
        # Presence cannot be known without a Download, so every listed row is
        # unresolved. Nothing is downloaded and nothing is written.
        summary.status = ACTION_REQUIRED
        summary.exit_code = exit_code_for(ACTION_REQUIRED)
        return summary

    unresolved: list[str] = []
    acquisitions: list[_Acquisition] = []
    run_dirs: list[Path] = []
    try:
        # ---- Phase A: acquire every row before any durable write ---- #
        seen_keys: set[str] = set()
        for row in rows:
            try:
                acquisition = _acquire_row(config, portal, logger, row, run_dirs)
            except AppError as exc:
                # One terminal row stops all later dispatch and skips Phase B.
                # Only the row that actually failed is counted.
                _count_failure(summary, unresolved, logger, exc, row.ordinal)
                break
            # Whole-run filename contract errors keep their existing raise
            # semantics: nothing has been written or published yet.
            _bind_archive_name(config, acquisition)
            if acquisition.key in seen_keys:
                raise AppError("duplicate normalized filename", status=PORTAL_LAYOUT_CHANGED, exit_code=20)
            seen_keys.add(acquisition.key)
            acquisitions.append(acquisition)

        # ---- Phase B: filename-keyed local reconciliation ---- #
        if not unresolved:
            for acquisition in acquisitions:
                try:
                    if _reconcile_acquisition(config, state, logger, acquisition) == "downloaded":
                        summary.downloaded_count += 1
                    else:
                        summary.present_count += 1
                except AppError as exc:
                    _record_failure(state, acquisition.key, acquisition.filename, exc)
                    _count_failure(summary, unresolved, logger, exc, acquisition.ordinal)
    finally:
        preserved = {acquisition.run_dir for acquisition in acquisitions if acquisition.preserve}
        for run_dir in run_dirs:
            if run_dir in preserved:
                continue
            try:
                cleanup_run_directory(run_dir, config.temp_root)
            except (OSError, ConfigError):
                pass

    if unresolved:
        summary.status = _worst_status(unresolved)
        summary.exit_code = 20 if any(exit_code_for(item) == 20 for item in unresolved) else 10
    elif summary.downloaded_count:
        summary.status = DOWNLOADED
        summary.exit_code = 0
    else:
        summary.status = ALREADY_PRESENT
        summary.exit_code = 0
    return summary


def _acquire_row(
    config: RuntimeConfig,
    portal: PortalProtocol,
    logger: LoggerProtocol,
    row: InvoiceRow,
    run_dirs: list[Path],
) -> _Acquisition:
    """Download one row into its own owned temp directory and validate it.

    A known, retryable failure retries only this row, at most `max_attempts`
    times, and the portal re-proves the whole frozen surface before each
    attempt. Anything non-retryable -- an uncertain dispatch, a drift latch,
    an invalid PDF -- ends the row at once.
    """

    run_dir = create_run_directory(config.temp_root, str(uuid.uuid4()))
    run_dirs.append(run_dir)
    temp_path = run_dir / "download.bin"
    try:
        suggested_filename: str | None = None
        for attempt in range(1, config.max_attempts + 1):
            if temp_path.exists():
                temp_path.unlink()
            try:
                suggested_filename = portal.download(row, temp_path)
                break
            except AppError as exc:
                if not exc.retryable or attempt >= config.max_attempts:
                    raise
                logger.event("download_retry", status=RETRYABLE_NETWORK_FAILURE, attempt=attempt)
        if suggested_filename is None:
            raise StateError("download loop completed without a result")
        info = validate_pdf(temp_path)
    except OSError as exc:
        raise StateError("owned temporary file could not be managed") from exc
    return _Acquisition(
        ordinal=row.ordinal,
        run_dir=run_dir,
        temp_path=temp_path,
        filename=suggested_filename,
        info=info,
    )


def _bind_archive_name(config: RuntimeConfig, acquisition: _Acquisition) -> None:
    """Validate the authoritative name and derive its filename key and target."""

    try:
        acquisition.final_path = validate_filename(acquisition.filename, config.archive_root)
    except ConfigError as exc:
        raise AppError("unsafe invoice filename", status=ACTION_REQUIRED, exit_code=20) from exc
    acquisition.key = filename_key(acquisition.filename)


def _reconcile_acquisition(
    config: RuntimeConfig,
    state: StateStore,
    logger: LoggerProtocol,
    acquisition: _Acquisition,
) -> str:
    """Reconcile one acquired row by filename key with the existing rules."""

    key = acquisition.key
    filename = acquisition.filename
    final_path = acquisition.final_path
    if final_path is None:
        raise StateError("acquisition reached reconciliation without a validated name")
    record = state.get(key)
    state.mark_seen(key, filename)
    try:
        decision = _inspect_existing(final_path, record)
        if decision == "present":
            # The archive already holds this invoice; the fresh temp is
            # discarded by the caller's cleanup and nothing is published.
            if record is None or record.status != "ARCHIVED":
                info = validate_pdf(final_path)
                state.record_archived(key, filename, info, completion_source="preexisting")
            return "present"
        if decision == "conflict":
            raise ArchiveConflictError("existing archive hash conflicts with state")
        info = validate_pdf(acquisition.temp_path)
        if info != acquisition.info:
            raise StateError("owned temporary download changed after acquisition")
        expected_hash = record.sha256 if record and record.sha256 else None
        if expected_hash is not None and info.sha256 != expected_hash:
            # Same-key repair evidence is retained exactly as before.
            acquisition.preserve = True
            raise ArchiveConflictError("repair download hash differs from recorded state")
        publish_no_replace(acquisition.temp_path, final_path)
        final_info = validate_pdf(final_path)
        if final_info != info:
            raise StateError("published final hash differs from validated download")
        state.record_archived(key, filename, final_info, completion_source="downloaded")
        logger.event("invoice_archived", status=DOWNLOADED)
        return "downloaded"
    except OSError as exc:
        raise StateError("owned temporary file could not be managed") from exc


def _count_failure(
    summary: RunSummary,
    unresolved: list[str],
    logger: LoggerProtocol,
    error: AppError,
    row_ordinal: Any = None,
) -> None:
    unresolved.append(error.status)
    summary.failure_count += 1
    summary.failures.append(error.status)
    logger.event("invoice_failure", status=error.status, **_failure_enrichment(error, row_ordinal))


def _failure_enrichment(error: AppError, row_ordinal: Any) -> dict[str, Any]:
    """The additive, closed invoice_failure fields (DL-XB-199 G2-083).

    `row_ordinal` accompanies every failure whose row is known; the preflight
    reason and checkpoint accompany only a `DownloadPreflightError`. Each
    value is validated here, before anything is logged: an invalid value is
    omitted, never coerced and never logged raw, so no free-form text can
    reach the log through these fields.
    """

    fields: dict[str, Any] = {}
    if type(row_ordinal) is int and 0 <= row_ordinal < MAX_INVENTORY_CEILING:
        fields["row_ordinal"] = row_ordinal
    support_ref = getattr(error, "support_ref", None)
    if type(support_ref) is str and re.fullmatch(SUPPORT_REF_PATTERN, support_ref):
        fields["support_ref"] = support_ref
    if isinstance(error, DownloadPreflightError):
        evidence = getattr(error, "evidence", None)
        reason = getattr(evidence, "reason_code", None)
        checkpoint = getattr(evidence, "last_checkpoint", None)
        if type(reason) is str and reason in DOWNLOAD_PREFLIGHT_REASONS:
            fields["preflight_reason"] = reason
        if type(checkpoint) is str and checkpoint in DOWNLOAD_PREFLIGHT_CHECKPOINTS:
            fields["preflight_checkpoint"] = checkpoint
    return fields


def _inspect_existing(final_path: Path, record: BillRecord | None) -> str:
    if final_path.exists() and final_path.is_dir():
        return "conflict"
    if not final_path.exists():
        return "missing"
    if record and record.sha256 and record.byte_size is not None:
        info = validate_pdf(final_path)
        if info.sha256 != record.sha256 or info.byte_size != record.byte_size:
            return "conflict"
        return "present"
    try:
        validate_pdf(final_path)
    except InvalidPdfError:
        return "conflict"
    return "present"


def _record_failure(state: StateStore, key: str, filename: str, error: AppError) -> None:
    status = "CONFLICT" if error.status == ARCHIVE_CONFLICT else "FAILED"
    try:
        state.record_failure(key, filename, error.status, status=status)
    except StateError:
        raise StateError("state failure prevented recording the invoice error")


def _worst_status(statuses: list[str]) -> str:
    priority = {
        STATE_INCONSISTENT: 100,
        ARCHIVE_CONFLICT: 90,
        INVALID_PDF: 80,
        PORTAL_LAYOUT_CHANGED: 70,
        ACTION_REQUIRED: 60,
        DOWNLOAD_FAILED: 40,
        RETRYABLE_NETWORK_FAILURE: 30,
    }
    return max(statuses, key=lambda item: priority.get(item, 50))


# ---------------------------------------------------------------------------
# DL-XB-199 G3-101: listed-inventory reconciliation for the direct-HTTP source
# ---------------------------------------------------------------------------
# The LIST response names every bill, so presence is decided BEFORE any FETCH:
# an already-archived valid bill is never fetched again. Everything else keeps
# the download-first path's guarantees and reuses its primitives unchanged:
#
#   Phase 0  bind every listed name (existing filename rules), refuse duplicate
#            normalised keys, refuse a known archived bill that disappeared
#            from the inventory, and decide which rows need a FETCH. Read-only.
#   Phase A  FETCH every needed row into its own owned temp directory and
#            validate it as a PDF. No state write, no publication. The first
#            failure stops dispatch and skips Phase B entirely.
#   Phase B  in inventory order, reconcile present rows and publish fetched
#            rows with the existing no-replace publication and state rules.

INVENTORY_REF_FILENAME_UNSAFE = "EG_INVENTORY_FILENAME_UNSAFE"
INVENTORY_REF_DUPLICATE_FILENAME = "EG_INVENTORY_DUPLICATE_FILENAME"
INVENTORY_REF_KNOWN_BILL_MISSING = "EG_INVENTORY_KNOWN_BILL_MISSING"
INVENTORY_SUPPORT_REFS = frozenset(
    {INVENTORY_REF_FILENAME_UNSAFE, INVENTORY_REF_DUPLICATE_FILENAME, INVENTORY_REF_KNOWN_BILL_MISSING}
)
ARCHIVED_STATUSES = frozenset({"ARCHIVED", "PRESENT_RECONCILED"})


class ListedSourceProtocol(Protocol):
    def inventory(self, safety_ceiling: int) -> list[Any]: ...

    def download(self, row: Any, destination: Path) -> str: ...


@dataclass
class _PlannedRow:
    ordinal: int
    row: Any = field(repr=False)
    filename: str = field(repr=False)
    key: str = field(repr=False)
    final_path: Path = field(repr=False)
    needs_fetch: bool = False


def reconcile_listed_inventory(
    config: RuntimeConfig,
    source: ListedSourceProtocol,
    state: StateStore,
    logger: LoggerProtocol,
    run_id: str,
    list_only: bool = False,
) -> RunSummary:
    summary = RunSummary(run_id=run_id, status=NO_NEW_BILLS, exit_code=0)
    logger.event("inventory_start")
    rows = source.inventory(config.inventory_safety_ceiling)
    summary.inventory_count = len(rows)
    logger.event("inventory_complete", inventory_count=len(rows))
    if not rows:
        # The source already refuses an empty inventory; this keeps the
        # guarantee independent of any one source implementation.
        raise SourceContractError("EG_HTTP_LIST_EMPTY")

    planned = _plan_listed_rows(config, state, rows)
    pending = [item for item in planned if item.needs_fetch]
    if list_only:
        summary.pending_count = len(pending)
        if pending:
            summary.status = ACTION_REQUIRED
            summary.exit_code = exit_code_for(ACTION_REQUIRED)
        return summary

    unresolved: list[str] = []
    acquisitions: dict[int, _Acquisition] = {}
    run_dirs: list[Path] = []
    try:
        # ---- Phase A: fetch every needed row before any durable write ---- #
        for item in pending:
            try:
                acquisitions[item.ordinal] = _fetch_listed_row(config, source, item, run_dirs)
                summary.fetch_count += 1
            except AppError as exc:
                summary.failure_stage = "fetch"
                summary.failure_support_ref = _support_ref_of(exc)
                _count_failure(summary, unresolved, logger, exc, item.ordinal)
                break
        logger.event("fetch_complete", downloaded_count=len(acquisitions), failure_count=len(unresolved))

        # ---- Phase B: filename-keyed reconciliation and publication ---- #
        if not unresolved:
            for item in planned:
                try:
                    acquisition = acquisitions.get(item.ordinal)
                    if acquisition is not None:
                        outcome = _reconcile_acquisition(config, state, logger, acquisition)
                    else:
                        outcome = _reconcile_listed_present(state, item)
                    if outcome == "downloaded":
                        summary.downloaded_count += 1
                    else:
                        summary.present_count += 1
                except AppError as exc:
                    if summary.failure_stage is None:
                        summary.failure_stage = "publish"
                        summary.failure_support_ref = _support_ref_of(exc)
                    _record_failure(state, item.key, item.filename, exc)
                    _count_failure(summary, unresolved, logger, exc, item.ordinal)
    finally:
        preserved = {acquisition.run_dir for acquisition in acquisitions.values() if acquisition.preserve}
        for run_dir in run_dirs:
            if run_dir in preserved:
                continue
            try:
                cleanup_run_directory(run_dir, config.temp_root)
            except (OSError, ConfigError):
                pass

    if unresolved:
        summary.status = _worst_status(unresolved)
        summary.exit_code = 20 if any(exit_code_for(item) == 20 for item in unresolved) else 10
    elif summary.downloaded_count:
        summary.status = DOWNLOADED
        summary.exit_code = 0
    else:
        summary.status = ALREADY_PRESENT
        summary.exit_code = 0
    return summary


def _support_ref_of(error: AppError) -> str | None:
    ref = getattr(error, "support_ref", None)
    return ref if type(ref) is str and re.fullmatch(SUPPORT_REF_PATTERN, ref) else None


def _plan_listed_rows(config: RuntimeConfig, state: StateStore, rows: list[Any]) -> list[_PlannedRow]:
    """Phase 0. Read-only: no fetch, no state write, no archive write."""

    planned: list[_PlannedRow] = []
    seen: set[str] = set()
    for row in rows:
        filename = row.filename
        try:
            final_path = validate_filename(filename, config.archive_root)
        except ConfigError:
            raise SourceContractError(INVENTORY_REF_FILENAME_UNSAFE) from None
        key = filename_key(filename)
        if key in seen:
            raise SourceContractError(INVENTORY_REF_DUPLICATE_FILENAME)
        seen.add(key)
        planned.append(_PlannedRow(ordinal=row.ordinal, row=row, filename=filename, key=key, final_path=final_path))

    # Completeness: a bill this program already archived must still be listed.
    # Its disappearance means the inventory is no longer the complete one.
    # "Archived" is judged by durable archival evidence, not only the current status: a
    # later failure or conflict overwrites the status but keeps the archive time and hash.
    for record in state.records():
        was_archived = (
            record.status in ARCHIVED_STATUSES
            or record.archived_at_utc is not None
            or record.sha256 is not None
        )
        if was_archived and record.filename_key not in seen:
            raise SourceContractError(INVENTORY_REF_KNOWN_BILL_MISSING)

    for item in planned:
        # An existing archive entry is never fetched: Phase B proves it present
        # (or a conflict) with the existing rules. Only an absent one is fetched,
        # and a same-key repair still compares against the recorded hash.
        item.needs_fetch = not (item.final_path.exists() or item.final_path.is_symlink())
    return planned


def _fetch_listed_row(
    config: RuntimeConfig,
    source: ListedSourceProtocol,
    item: _PlannedRow,
    run_dirs: list[Path],
) -> _Acquisition:
    """FETCH one listed row into its own owned temp directory and validate it.

    Transport retries live inside the source and are bounded by max_attempts;
    this function never retries, so attempts are never multiplied.
    """

    run_dir = create_run_directory(config.temp_root, str(uuid.uuid4()))
    run_dirs.append(run_dir)
    temp_path = run_dir / "download.bin"
    try:
        source.download(item.row, temp_path)
        info = validate_pdf(temp_path)
    except OSError as exc:
        raise StateError("owned temporary file could not be managed") from exc
    return _Acquisition(
        ordinal=item.ordinal,
        run_dir=run_dir,
        temp_path=temp_path,
        filename=item.filename,
        info=info,
        key=item.key,
        final_path=item.final_path,
    )


def _reconcile_listed_present(state: StateStore, item: _PlannedRow) -> str:
    """Reconcile a row whose archive entry already existed at planning time."""

    record = state.get(item.key)
    state.mark_seen(item.key, item.filename)
    try:
        decision = _inspect_existing(item.final_path, record)
        if decision == "present":
            if record is None or record.status != "ARCHIVED":
                info = validate_pdf(item.final_path)
                state.record_archived(item.key, item.filename, info, completion_source="preexisting")
            return "present"
        if decision == "conflict":
            raise ArchiveConflictError("existing archive hash conflicts with state")
        raise StateError("archive entry disappeared during reconciliation")
    except OSError as exc:
        raise StateError("archive entry could not be inspected") from exc


DUAL_SUCCESS_STATUSES = frozenset({"EMPTY", "ALREADY_HANDLED", "DELIVERED", "COMPLETED"})
DUAL_STREAMS = ("EB_BILL", "TENANT_BILL")


def reconcile_dual_stream(
    config,
    adapters: dict[str, Any],
    state,
    logger: LoggerProtocol,
    run_id: str,
    *,
    list_only: bool = False,
) -> RunSummary:
    """Select at most one latest candidate per independently bound stream."""
    from .config import BOUND_ADMISSION, UNBOUND_ADMISSION
    from .delivery import DeliveryClient
    from .drive import DriveStager
    from .invoice import InventorySnapshot, Stream
    from .publication import ensure_no_reparse_components, ensure_same_volume
    from .state import StateV2Store

    if not isinstance(state, StateV2Store):
        raise StateError("dual-stream execution requires the v2 state store")
    summary = RunSummary(run_id=run_id, status=ACTION_REQUIRED, exit_code=20)
    details: dict[str, dict[str, Any]] = {}
    failures: list[int] = []
    if config.drive.mode != "local_stage" or config.drive.root is None or not config.drive.binding_id:
        for stream_name in DUAL_STREAMS:
            details[stream_name] = _dual_detail(stream_name, "SHARED_SINK_UNBOUND", "EG_DRIVE_BINDING_UNBOUND")
        summary.stream_results = [details[name] for name in DUAL_STREAMS]
        summary.failure_count = len(DUAL_STREAMS)
        summary.failures = [ACTION_REQUIRED]
        summary.status = ACTION_REQUIRED
        summary.exit_code = 20
        return summary

    drive = DriveStager(config.archive_root, config.drive.root, config.drive.binding_id)
    delivery = DeliveryClient(config.delivery)
    # Validate the shared roots before any adapter is contacted.
    ensure_no_reparse_components(config.archive_root)
    ensure_no_reparse_components(config.drive.root)
    ensure_no_reparse_components(config.temp_root)
    ensure_same_volume(config.temp_root, config.archive_root / "_volume_probe_")

    # Snapshot every bound stream before any state change, acquisition, archive,
    # Drive or delivery effect. Streams remain independently fail-closed below,
    # but one stream's later inventory response cannot arrive after another
    # stream has already started processing its latest invoice.
    prepared_snapshots: dict[str, InventorySnapshot] = {}
    for stream_name in DUAL_STREAMS:
        entry = config.streams[stream_name]
        detail = _dual_detail(stream_name, "PENDING", None)
        details[stream_name] = detail
        if not state.verify_stream_binding(stream_name, entry):
            _dual_hold(detail, "STREAM_BINDING_MISMATCH", "EG_STREAM_BINDING_MISMATCH")
            failures.append(20)
            continue
        if entry.admission == UNBOUND_ADMISSION:
            _dual_hold(detail, "UNBOUND", "EG_STREAM_UNBOUND")
            failures.append(20)
            continue
        if entry.admission != BOUND_ADMISSION:
            _dual_hold(detail, "STREAM_HELD", "EG_STREAM_ADMISSION_HOLD")
            failures.append(20)
            continue
        adapter = adapters.get(stream_name)
        if adapter is None:
            _dual_hold(detail, "ADAPTER_UNAVAILABLE", "EG_SOURCE_ADAPTER_UNAVAILABLE")
            failures.append(20)
            continue

        try:
            snapshot = adapter.inventory(config.inventory_safety_ceiling)
            if not isinstance(snapshot, InventorySnapshot):
                raise SourceContractError("EG_INVENTORY_SNAPSHOT_INVALID")
            stream_enum = Stream(stream_name)
            if (
                snapshot.stream is not stream_enum
                or snapshot.source_namespace != entry.source_namespace
                or snapshot.date_profile != entry.date_profile
                or len(snapshot.candidates) > config.inventory_safety_ceiling
            ):
                raise SourceContractError("EG_INVENTORY_BINDING_INVALID")
            detail["inventory_count"] = len(snapshot.candidates)
            summary.inventory_count += len(snapshot.candidates)
        except AppError as exc:
            _dual_hold(detail, "SOURCE_FAILURE", _dual_support_ref(exc, "EG_SOURCE_INVENTORY_FAILED"))
            failures.append(exc.exit_code or 20)
            continue
        except Exception:
            _dual_hold(detail, "SOURCE_FAILURE", "EG_SOURCE_INVENTORY_FAILED")
            failures.append(20)
            continue
        prepared_snapshots[stream_name] = snapshot

    for stream_name in DUAL_STREAMS:
        detail = details[stream_name]
        if stream_name not in prepared_snapshots:
            continue
        snapshot = prepared_snapshots[stream_name]
        adapter = adapters[stream_name]
        stream_state = state.stream(stream_name)
        if not snapshot.candidates:
            if stream_state is not None and stream_state["watermark_day"] is not None:
                _dual_hold(detail, "LATEST_INVOICE_MISSING", "EG_LATEST_INVOICE_MISSING")
                failures.append(20)
            else:
                detail["status"] = "EMPTY"
                detail["handled_count"] = 1
                summary.handled_count += 1
                logger.event("stream_complete", status="EMPTY", stream=stream_name, inventory_count=0, handled_count=1)
            continue

        latest_day = max(item.day_ordinal for item in snapshot.candidates)
        latest = [item for item in snapshot.candidates if item.day_ordinal == latest_day]
        if len(latest) != 1:
            _dual_hold(detail, "LATEST_AMBIGUOUS", "EG_LATEST_AMBIGUOUS")
            failures.append(20)
            continue
        candidate = latest[0]
        try:
            candidate_archive_path = _canonical_path(config.archive_root, stream_name, candidate.canonical_filename)
            ensure_no_reparse_components(candidate_archive_path)
        except AppError as exc:
            _dual_hold(detail, "ARCHIVE_PATH_INVALID", _dual_support_ref(exc, "EG_ARCHIVE_PATH_INVALID"))
            failures.append(exc.exit_code or 20)
            continue
        stream_state = state.stream(stream_name)
        if stream_state is None or stream_state["admission"] != BOUND_ADMISSION:
            _dual_hold(detail, "STREAM_BINDING_MISMATCH", "EG_STREAM_BINDING_MISMATCH")
            failures.append(20)
            continue
        watermark_day = stream_state["watermark_day"]
        if watermark_day is not None and candidate.day_ordinal < watermark_day:
            _dual_hold(detail, "SOURCE_REGRESSION", "EG_SOURCE_REGRESSION")
            failures.append(20)
            continue

        existing = state.invoice_by_identity(candidate.source_namespace, stream_name, candidate.source_invoice_key)
        candidate_invoice_id = _invoice_id(candidate.source_namespace, stream_name, candidate.source_invoice_key)
        if (
            watermark_day == candidate.day_ordinal
            and stream_state["watermark_invoice_id"] is not None
            and stream_state["watermark_invoice_id"] != candidate_invoice_id
        ):
            _dual_hold(detail, "LATEST_IDENTITY_CONFLICT", "EG_LATEST_IDENTITY_CONFLICT")
            failures.append(20)
            continue
        if list_only:
            detail["status"] = "INVENTORY_READY"
            detail["handled_count"] = int(_is_fully_handled(config, state, drive, existing))
            continue

        if existing is not None and _is_fully_handled(config, state, drive, existing):
            detail["status"] = "ALREADY_HANDLED"
            detail["archive_reused_count"] = 1
            detail["delivered_count"] = 1
            detail["handled_count"] = 1
            summary.archive_reused_count += 1
            summary.delivered_count += 1
            summary.handled_count += 1
            logger.event("stream_complete", status="ALREADY_HANDLED", stream=stream_name, handled_count=1, delivered_count=1)
            continue

        invoice_id = state.accept_latest(candidate, run_id, _dual_utc_now())
        invoice = state.invoice(invoice_id)
        if invoice is None or invoice["classification"] != "CLASSIFIED":
            raise StateError("selected invoice could not be read back")
        relpath = invoice["archive_relpath"]
        archive_path = _canonical_path(config.archive_root, stream_name, invoice["canonical_filename"])

        try:
            operation = state.file_operation(invoice_id, "ARCHIVE_PUBLISH", None, relpath)
            if operation is not None and operation["state"] in {"PREPARED", "HOLD", "COMMITTED"}:
                archive_info = _recover_archive_operation(state, invoice, archive_path, operation)
                if archive_info is None:
                    _dual_hold(detail, "ARCHIVE_RECOVERY_HOLD", "EG_ARCHIVE_RECOVERY_HOLD")
                    failures.append(20)
                    continue
                invoice = state.invoice(invoice_id)
            elif invoice["archive_state"] == "COMMITTED":
                if not archive_path.exists():
                    _dual_hold(detail, "ARCHIVE_MISSING", "EG_COMMITTED_ARCHIVE_MISSING")
                    failures.append(20)
                    continue
                archive_info = validate_pdf(archive_path)
                if archive_info.byte_size != invoice["byte_size"] or archive_info.sha256 != invoice["sha256"]:
                    _dual_hold(detail, "ARCHIVE_CONFLICT", "EG_ARCHIVE_BYTES_CONFLICT")
                    failures.append(20)
                    continue
                summary.archive_reused_count += 1
                detail["archive_reused_count"] = 1
            else:
                if archive_path.exists() or archive_path.is_symlink():
                    _dual_hold(detail, "ARCHIVE_CONFLICT", "EG_ARCHIVE_UNOWNED_DESTINATION")
                    failures.append(20)
                    continue
                run_dir = _create_dual_temp_directory(config.temp_root)
                temp_path = run_dir / "latest.pdf"
                keep_run_dir = False
                try:
                    returned_name = adapter.acquire(candidate, temp_path)
                    if type(returned_name) is not str or returned_name != candidate.source_filename:
                        raise SourceContractError("EG_FETCH_HANDLE_NAME_MISMATCH", stage="fetch")
                    archive_info = validate_pdf(temp_path)
                    if archive_info.byte_size > config.delivery.max_pdf_bytes:
                        raise SourceContractError("EG_FETCH_EXCEEDS_DELIVERY_LIMIT", stage="fetch")
                    op = state.start_file_operation(
                        operation_id=str(uuid.uuid4()), invoice_id=invoice_id,
                        kind="ARCHIVE_PUBLISH", private_path_ref=str(temp_path),
                        target_relpath=relpath, binding_id=None, info=archive_info,
                        source_role="SOURCE_ACQUISITION", run_id=run_id, timestamp=_dual_utc_now(),
                    )
                    if op["state"] != "PREPARED":
                        raise StateError("archive publication already has an unresolved operation")
                    keep_run_dir = True
                    ensure_no_reparse_components(config.archive_root)
                    ensure_no_reparse_components(archive_path)
                    publish_no_replace(temp_path, archive_path)
                    if validate_pdf(archive_path) != archive_info:
                        raise ArchiveConflictError("published archive bytes failed verification")
                    state.complete_file_operation(op["operation_id"], _dual_utc_now(), evidence_ref="EG_ARCHIVE_HASH_VERIFIED")
                    state.update_invoice_file_state(
                        invoice_id, archive_state="COMMITTED", byte_size=archive_info.byte_size,
                        sha256=archive_info.sha256, archived_at_utc=_dual_utc_now(),
                    )
                    summary.fetch_count += 1
                    summary.downloaded_count += 1
                    detail["archive_staged_count"] = 1
                except Exception:
                    # Once the journal owns the temp, keep it for recovery.
                    raise
                finally:
                    if not keep_run_dir or not temp_path.exists():
                        try:
                            cleanup_run_directory(run_dir, config.temp_root)
                        except (OSError, ConfigError):
                            pass

            invoice = state.invoice(invoice_id)
            if invoice is None or invoice["archive_state"] != "COMMITTED":
                raise StateError("archive commit was not durable")
            archive_info = validate_pdf(archive_path)
            if archive_info.byte_size != invoice["byte_size"] or archive_info.sha256 != invoice["sha256"]:
                raise ArchiveConflictError("archive facts changed before Drive staging")
        except AppError as exc:
            if isinstance(exc, (StateError, ConfigError)):
                raise
            _dual_hold(detail, "ARCHIVE_FAILURE", _dual_support_ref(exc, "EG_ARCHIVE_FAILURE"))
            failures.append(exc.exit_code or 20)
            continue
        except OSError:
            _dual_hold(detail, "ARCHIVE_FAILURE", "EG_ARCHIVE_FAILURE")
            failures.append(20)
            continue

        try:
            invoice = state.invoice(invoice_id)
            if invoice is None:
                raise StateError("invoice state is unavailable")
            was_staged = invoice["drive_state"] == "DRIVE_STAGED"
            drive.stage(state, invoice, archive_path, run_id)
            if not was_staged:
                detail["drive_staged_count"] = 1
                summary.drive_staged_count += 1
        except AppError as exc:
            if isinstance(exc, (StateError, ConfigError)):
                raise
            _dual_hold(detail, "DRIVE_FAILURE", _dual_support_ref(exc, "EG_DRIVE_STAGE_FAILURE"))
            failures.append(exc.exit_code or 20)
            continue
        except Exception:
            _dual_hold(detail, "DRIVE_FAILURE", "EG_DRIVE_STAGE_FAILURE")
            failures.append(20)
            continue

        try:
            invoice = state.invoice(invoice_id)
            if invoice is None:
                raise StateError("invoice state is unavailable")
            mail = delivery.deliver(state, invoice, archive_path, run_id)
            detail["delivery_outcome"] = mail.state
            detail["support_ref"] = mail.support_ref
            if mail.state == "DELIVERED":
                detail["status"] = "DELIVERED"
                detail["delivered_count"] = 1
                detail["handled_count"] = 1
                summary.delivered_count += 1
                summary.handled_count += 1
            elif mail.state == "DELIVERY_OUTCOME_UNCERTAIN":
                detail["status"] = "DELIVERY_OUTCOME_UNCERTAIN"
                detail["uncertain_count"] = 1
                summary.uncertain_count += 1
                failures.append(20)
            else:
                _dual_hold(detail, "REQUEST_REJECTED", mail.support_ref)
                failures.append(20)
        except AppError as exc:
            if isinstance(exc, StateError):
                raise
            _dual_hold(detail, "DELIVERY_FAILURE", _dual_support_ref(exc, "EG_DELIVERY_PREPARATION_FAILED"))
            failures.append(exc.exit_code or 20)
            continue
        except Exception:
            _dual_hold(detail, "DELIVERY_FAILURE", "EG_DELIVERY_PREPARATION_FAILED")
            failures.append(20)
            continue
        logger.event(
            "stream_complete", status=detail["status"], stream=stream_name,
            archive_reused_count=detail["archive_reused_count"],
            archive_staged_count=detail["archive_staged_count"],
            drive_staged_count=detail["drive_staged_count"],
            delivered_count=detail["delivered_count"],
            uncertain_count=detail["uncertain_count"],
        )

    summary.stream_results = [details[name] for name in DUAL_STREAMS]
    summary.failure_count = len(failures)
    summary.failures = [ACTION_REQUIRED] if failures else []
    if failures:
        summary.status = ACTION_REQUIRED
        summary.exit_code = 10 if failures and all(code == 10 for code in failures) else 20
    else:
        if summary.downloaded_count:
            summary.status = DOWNLOADED
        elif summary.archive_reused_count or summary.handled_count:
            summary.status = ALREADY_PRESENT
        else:
            summary.status = NO_NEW_BILLS
        summary.exit_code = 0
    summary.present_count = summary.archive_reused_count
    return summary


def _dual_detail(stream: str, status: str, support_ref: str | None) -> dict[str, Any]:
    return {
        "stream": stream,
        "status": status,
        "inventory_count": 0,
        "archive_reused_count": 0,
        "archive_staged_count": 0,
        "drive_staged_count": 0,
        "delivered_count": 0,
        "handled_count": 0,
        "uncertain_count": 0,
        "delivery_outcome": None,
        "support_ref": support_ref,
    }


def _dual_hold(detail: dict[str, Any], status: str, support_ref: str) -> None:
    detail["status"] = status
    detail["support_ref"] = support_ref if re.fullmatch(SUPPORT_REF_PATTERN, support_ref) else "EG_DUAL_STREAM_FAILURE"


def _dual_support_ref(error: AppError, fallback: str) -> str:
    ref = getattr(error, "support_ref", None)
    return ref if type(ref) is str and re.fullmatch(SUPPORT_REF_PATTERN, ref) else fallback


def _dual_utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _invoice_id(namespace: str, stream: str, source_key: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"energygrid-v2:{namespace}:{stream}:{source_key}"))


def _canonical_path(root: Path, stream: str, filename: str) -> Path:
    from .invoice import DATE_PROFILE_ISO_V1, parse_invoice_date, Stream

    if stream not in {item.value for item in Stream} or type(filename) is not str:
        raise ConfigError("canonical archive path is invalid")
    folder = "EB Bill" if stream == "EB_BILL" else "Tenant Bill"
    path = root / folder / filename
    if not is_within(path, root) or path.name != filename or not filename.endswith(".pdf"):
        raise ConfigError("canonical archive path is invalid")
    try:
        parsed = parse_invoice_date(filename[:-4], DATE_PROFILE_ISO_V1)
    except AppError:
        raise ConfigError("canonical archive path is invalid") from None
    if filename != f"{parsed.isoformat()}.pdf":
        raise ConfigError("canonical archive path is invalid")
    return path


def _create_dual_temp_directory(root: Path) -> Path:
    from .publication import create_run_directory, ensure_no_reparse_components
    ensure_no_reparse_components(root)
    return create_run_directory(root, str(uuid.uuid4()))


def _recover_archive_operation(state, invoice: dict, destination: Path, operation: dict) -> FileInfo | None:
    from .publication import ensure_no_reparse_components

    if operation["state"] == "HOLD":
        return None
    expected = FileInfo(operation["expected_size"], operation["expected_sha256"])
    temp_path = Path(operation["private_path_ref"])
    try:
        if operation["state"] == "COMMITTED" and not destination.exists():
            return None
        if destination.exists():
            ensure_no_reparse_components(destination)
            info = validate_pdf(destination)
            if info != expected:
                state.hold_file_operation(operation["operation_id"], "EG_ARCHIVE_RECOVERY_CONFLICT")
                return None
        elif temp_path.exists():
            ensure_no_reparse_components(temp_path)
            if validate_pdf(temp_path) != expected:
                state.hold_file_operation(operation["operation_id"], "EG_ARCHIVE_RECOVERY_CONFLICT")
                return None
            ensure_no_reparse_components(destination)
            publish_no_replace(temp_path, destination)
            info = validate_pdf(destination)
            if info != expected:
                state.hold_file_operation(operation["operation_id"], "EG_ARCHIVE_RECOVERY_CONFLICT")
                return None
        else:
            state.hold_file_operation(operation["operation_id"], "EG_ARCHIVE_RECOVERY_SOURCE_MISSING")
            return None
        if operation["state"] == "PREPARED":
            state.complete_file_operation(operation["operation_id"], _dual_utc_now(), evidence_ref="EG_ARCHIVE_RECOVERY_HASH_VERIFIED")
        state.update_invoice_file_state(
            invoice["invoice_id"], archive_state="COMMITTED", byte_size=info.byte_size,
            sha256=info.sha256, archived_at_utc=invoice.get("archived_at_utc") or _dual_utc_now(),
        )
        return info
    except (OSError, AppError) as error:
        if isinstance(error, StateError):
            raise
        try:
            state.hold_file_operation(operation["operation_id"], "EG_ARCHIVE_RECOVERY_HOLD")
        except Exception:
            pass
        return None


def _is_fully_handled(config, state, drive, invoice: dict | None) -> bool:
    if invoice is None or invoice.get("classification") != "CLASSIFIED" or state.has_open_file_operation(invoice["invoice_id"]):
        return False
    watermark = state.stream(invoice["stream"])
    if (
        watermark is None
        or watermark.get("watermark_day") != invoice.get("day_ordinal")
        or watermark.get("watermark_invoice_id") != invoice.get("invoice_id")
    ):
        return False
    if invoice.get("archive_state") != "COMMITTED" or invoice.get("drive_state") != "DRIVE_STAGED":
        return False
    if (
        invoice.get("drive_binding_id") != config.drive.binding_id
        or invoice.get("drive_relpath") != invoice.get("archive_relpath")
        or invoice.get("drive_size") != invoice.get("byte_size")
        or invoice.get("drive_sha256") != invoice.get("sha256")
    ):
        return False
    delivery_row = state.delivery_for_invoice(invoice["invoice_id"])
    if delivery_row is None or delivery_row.get("state") != "DELIVERED":
        return False
    try:
        archive_path = _canonical_path(config.archive_root, invoice["stream"], invoice["canonical_filename"])
        archive_info = validate_pdf(archive_path)
        if archive_info.byte_size != invoice["byte_size"] or archive_info.sha256 != invoice["sha256"]:
            return False
        drive_path = config.drive.root / invoice["archive_relpath"]
        drive_info = validate_pdf(drive_path)
        if drive_info != archive_info:
            return False
    except (OSError, AppError):
        return False
    return (
        delivery_row.get("stream") == invoice["stream"]
        and delivery_row.get("bill_date") == invoice["bill_date"]
        and delivery_row.get("attachment_name") == invoice["canonical_filename"]
        and delivery_row.get("byte_size") == invoice["byte_size"]
        and delivery_row.get("sha256") == invoice["sha256"]
    )
