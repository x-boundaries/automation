from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

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
    cleanup_run_directory,
    create_run_directory,
    filename_key,
    publish_no_replace,
    validate_filename,
    validate_pdf,
)
from .portal import BillRef


class PortalProtocol(Protocol):
    def inventory(self, safety_ceiling: int) -> list[BillRef]: ...

    def download(self, bill: BillRef, destination: Path) -> str: ...


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


def reconcile_inventory(
    config: RuntimeConfig,
    portal: PortalProtocol,
    state: StateStore,
    logger: LoggerProtocol,
    run_id: str,
    list_only: bool = False,
) -> RunSummary:
    summary = RunSummary(run_id=run_id, status=NO_NEW_BILLS, exit_code=0)
    logger.event("inventory_start")
    bills = portal.inventory(config.inventory_safety_ceiling)
    summary.inventory_count = len(bills)
    logger.event("inventory_complete", inventory_count=len(bills))

    accepted: list[tuple[BillRef, str, Path]] = []
    seen_keys: set[str] = set()
    for bill in bills:
        try:
            key = filename_key(bill.filename)
            if key in seen_keys:
                raise AppError("duplicate normalized filename", status=PORTAL_LAYOUT_CHANGED, exit_code=20)
            seen_keys.add(key)
            final_path = validate_filename(bill.filename, config.archive_root)
            accepted.append((bill, key, final_path))
        except AppError:
            raise
        except ConfigError as exc:
            raise AppError("unsafe invoice filename", status=ACTION_REQUIRED, exit_code=20) from exc

    if not bills:
        return summary

    unresolved: list[str] = []
    for bill, key, final_path in accepted:
        record = state.get(key)
        if not list_only:
            state.mark_seen(key, bill.filename)
        try:
            decision = _inspect_existing(final_path, record)
            if decision == "present":
                summary.present_count += 1
                if not list_only and (record is None or record.status != "ARCHIVED"):
                    info = validate_pdf(final_path)
                    state.record_archived(key, bill.filename, info, completion_source="preexisting")
                continue
            if decision == "conflict":
                raise ArchiveConflictError("existing archive hash conflicts with state")
            if list_only:
                unresolved.append(ACTION_REQUIRED)
                continue
            expected_hash = record.sha256 if record and record.sha256 else None
            result = _download_and_publish(
                config=config,
                portal=portal,
                state=state,
                logger=logger,
                bill=bill,
                key=key,
                final_path=final_path,
                run_id=run_id,
                expected_hash=expected_hash,
            )
            if result == "downloaded":
                summary.downloaded_count += 1
            else:
                summary.present_count += 1
        except AppError as exc:
            if not list_only:
                _record_failure(state, key, bill.filename, exc)
            unresolved.append(exc.status)
            summary.failure_count += 1
            summary.failures.append(exc.status)
            logger.event("invoice_failure", status=exc.status)

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


def _download_and_publish(
    config: RuntimeConfig,
    portal: PortalProtocol,
    state: StateStore,
    logger: LoggerProtocol,
    bill: BillRef,
    key: str,
    final_path: Path,
    run_id: str,
    expected_hash: str | None,
) -> str:
    run_dir = create_run_directory(config.temp_root, run_id)
    temp_path = run_dir / "download.bin"
    keep_temp = False
    try:
        last_error: AppError | None = None
        for attempt in range(1, config.max_attempts + 1):
            if temp_path.exists():
                temp_path.unlink()
            try:
                suggested_filename = portal.download(bill, temp_path)
                if filename_key(suggested_filename) != key:
                    raise AppError("download filename identity mismatch", status=PORTAL_LAYOUT_CHANGED, exit_code=20)
                info = validate_pdf(temp_path)
                if expected_hash is not None and info.sha256 != expected_hash:
                    keep_temp = True
                    raise ArchiveConflictError("repair download hash differs from recorded state")
                publish_no_replace(temp_path, final_path)
                final_info = validate_pdf(final_path)
                if final_info != info:
                    raise StateError("published final hash differs from validated download")
                state.record_archived(key, bill.filename, final_info, completion_source="downloaded")
                logger.event("invoice_archived", status=DOWNLOADED)
                return "downloaded"
            except AppError as exc:
                last_error = exc
                if not exc.retryable or attempt >= config.max_attempts:
                    raise
                logger.event("download_retry", status=RETRYABLE_NETWORK_FAILURE, attempt=attempt)
        if last_error is not None:
            raise last_error
        raise StateError("download loop completed without a result")
    except (OSError, InvalidPdfError) as exc:
        if isinstance(exc, InvalidPdfError):
            raise
        raise StateError("owned temporary file could not be managed") from exc
    finally:
        if not keep_temp:
            try:
                cleanup_run_directory(run_dir, config.temp_root)
            except (OSError, ConfigError):
                pass


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
