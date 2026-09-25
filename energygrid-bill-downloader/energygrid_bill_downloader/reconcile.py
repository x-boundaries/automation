from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
import uuid

from .config import RuntimeConfig
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
from .portal import InvoiceRow
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

    def as_dict(self) -> dict[str, Any]:
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
                _count_failure(summary, unresolved, logger, exc)
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
                    _count_failure(summary, unresolved, logger, exc)
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
    summary: RunSummary, unresolved: list[str], logger: LoggerProtocol, error: AppError
) -> None:
    unresolved.append(error.status)
    summary.failure_count += 1
    summary.failures.append(error.status)
    logger.event("invoice_failure", status=error.status)


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
