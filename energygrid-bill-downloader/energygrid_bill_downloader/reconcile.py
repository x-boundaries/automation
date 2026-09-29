from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
import re
import uuid

from .config import MAX_INVENTORY_CEILING, RuntimeConfig
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

    def as_dict(self) -> dict[str, Any]:
        document = self._base_dict()
        if self.pending_count is not None:
            document["pending_count"] = self.pending_count
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
    for record in state.records():
        if record.status in ARCHIVED_STATUSES and record.filename_key not in seen:
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
