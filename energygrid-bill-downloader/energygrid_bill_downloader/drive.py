from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import ArchiveConflictError, ConfigError, InvalidPdfError, StateError
from .publication import FileInfo, ensure_no_reparse_components, is_within, resolved, validate_pdf
from .state import StateV2Store, StreamStateConflictError


class DriveStager:
    """Copy exact committed archive bytes into a local Drive sync root."""

    def __init__(self, archive_root: Path, drive_root: Path, binding_id: str) -> None:
        self._archive_root_input = Path(os.path.abspath(archive_root))
        self._drive_root_input = Path(os.path.abspath(drive_root))
        self._archive_root = resolved(archive_root)
        self._drive_root = resolved(drive_root)
        self._binding_id = binding_id

    def stage(self, state: StateV2Store, invoice: dict, archive_path: Path, run_id: str, *, logger=None) -> FileInfo:
        relpath = invoice.get("archive_relpath")
        if type(relpath) is not str or relpath not in {
            f"EB Bill/{invoice.get('canonical_filename')}",
            f"Tenant Bill/{invoice.get('canonical_filename')}",
        }:
            raise ConfigError("Drive target path is not canonical")
        source_input = Path(os.path.abspath(archive_path))
        target_input = Path(os.path.abspath(self._drive_root_input / relpath))
        ensure_no_reparse_components(self._archive_root_input)
        ensure_no_reparse_components(self._drive_root_input)
        ensure_no_reparse_components(source_input)
        try:
            ensure_no_reparse_components(target_input)
        except ConfigError:
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_DESTINATION_CONFLICT") from None
        source = resolved(source_input)
        target = resolved(target_input)
        if not is_within(source, self._archive_root) or not is_within(target, self._drive_root):
            raise ConfigError("Drive staging path escaped its approved root")
        info = validate_pdf(source)

        if invoice.get("archive_state") != "COMMITTED" or invoice.get("byte_size") != info.byte_size or invoice.get("sha256") != info.sha256:
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_ARCHIVE_FACTS_CONFLICT")
        already_staged = invoice.get("drive_state") == "DRIVE_STAGED"
        if already_staged and (
            invoice.get("drive_binding_id") != self._binding_id
            or invoice.get("drive_relpath") != relpath
            or invoice.get("drive_size") != info.byte_size
            or invoice.get("drive_sha256") != info.sha256
        ):
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_STAGE_FACTS_CONFLICT")

        def emit(phase: str, status: str) -> None:
            try:
                if logger is not None:
                    logger.event(phase, status=status, stream=invoice["stream"])
            except Exception:
                return None

        emit("drive_started", "STARTED")
        active = state.active_file_operation_conflicts(invoice["invoice_id"], {"DRIVE_STAGE"}, relpath)
        if len(active) > 1 or any(
            operation["invoice_id"] != invoice["invoice_id"]
            or operation["binding_id"] != self._binding_id or operation["target_relpath"] != relpath
            for operation in active
        ):
            for operation in active:
                if operation["state"] == "PREPARED":
                    state.hold_file_operation(operation["operation_id"], "EG_DRIVE_OPERATION_AUTHORITY_CONFLICT")
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_OPERATION_AUTHORITY_CONFLICT")
        if active and active[0]["state"] == "HOLD":
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_RECOVERY_HOLD")

        history = state.file_operation_history(
            invoice["invoice_id"], "DRIVE_STAGE", self._binding_id, relpath
        )
        if any(
            operation["expected_size"] != info.byte_size
            or operation["expected_sha256"] != info.sha256
            or operation["source_role"] != "ARCHIVE_COMMITTED"
            for operation in history
        ):
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_HISTORY_CONFLICT")

        target_present = target.exists() or target.is_symlink()
        if active:
            operation = active[0]
            temp_path = Path(operation["private_path_ref"])
            expected_temp_name = f".{target.name}.{operation['operation_id']}.tmp"
            if (
                operation["expected_size"] != info.byte_size
                or operation["expected_sha256"] != info.sha256
                or operation["source_role"] != "ARCHIVE_COMMITTED"
                or temp_path.parent != target.parent or temp_path.name != expected_temp_name
            ):
                state.hold_file_operation(operation["operation_id"], "EG_DRIVE_RECOVERY_CONFLICT")
                raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_RECOVERY_CONFLICT")
            try:
                ensure_no_reparse_components(temp_path)
                ensure_no_reparse_components(target)
                temp_present = temp_path.exists() or temp_path.is_symlink()
                target_present = target.exists() or target.is_symlink()
                if temp_present == target_present:
                    raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_RECOVERY_AMBIGUOUS")
                if temp_present:
                    if validate_pdf(temp_path) != info:
                        raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_RECOVERY_CONFLICT")
                    _publish_link_no_replace(temp_path, target)
                if validate_pdf(target) != info:
                    raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_RECOVERY_CONFLICT")
            except StreamStateConflictError as error:
                state.hold_file_operation(operation["operation_id"], error.support_ref)
                raise
            except (OSError, ArchiveConflictError, InvalidPdfError, ConfigError, StateError):
                try:
                    state.hold_file_operation(operation["operation_id"], "EG_DRIVE_RECOVERY_HOLD")
                except Exception:
                    raise
                raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_RECOVERY_HOLD") from None
            state.complete_file_operation(operation["operation_id"], _utc_now(), evidence_ref="EG_DRIVE_STAGE_HASH_VERIFIED")
            if not already_staged:
                self._record_stage(state, invoice["invoice_id"], relpath, info)
            emit("drive_result", "REPAIRED" if already_staged else "STAGED")
            return info

        if target_present:
            try:
                ensure_no_reparse_components(target)
                target_info = validate_pdf(target)
            except (OSError, ArchiveConflictError, InvalidPdfError, ConfigError, StateError):
                raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_DESTINATION_CONFLICT") from None
            if target_info != info:
                raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_DESTINATION_CONFLICT")
            if already_staged:
                emit("drive_result", "ALREADY_STAGED")
                return info
            if not history:
                raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_UNOWNED_DESTINATION")
            state.update_invoice_file_state(
                invoice["invoice_id"], drive_state="DRIVE_STAGED", drive_binding_id=self._binding_id,
                drive_relpath=relpath, drive_size=info.byte_size, drive_sha256=info.sha256,
                drive_staged_at_utc=_utc_now(),
            )
            emit("drive_result", "RECOVERED")
            return info

        if target.is_symlink():
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_DESTINATION_CONFLICT")
        target.parent.mkdir(parents=True, exist_ok=True)
        ensure_no_reparse_components(target.parent)
        operation_id = str(uuid.uuid4())
        temp_path = target.with_name(f".{target.name}.{operation_id}.tmp")
        operation = state.start_file_operation(
            operation_id=operation_id, invoice_id=invoice["invoice_id"], kind="DRIVE_STAGE",
            private_path_ref=str(temp_path), target_relpath=relpath, binding_id=self._binding_id,
            info=info, source_role="ARCHIVE_COMMITTED", run_id=run_id, timestamp=_utc_now(),
        )
        if operation["state"] != "PREPARED" or Path(operation["private_path_ref"]) != temp_path:
            if operation["state"] == "PREPARED":
                state.hold_file_operation(operation["operation_id"], "EG_DRIVE_OPERATION_CONFLICT")
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_OPERATION_CONFLICT")
        try:
            _copy_exclusive(source, temp_path, info)
            ensure_no_reparse_components(temp_path)
            _publish_link_no_replace(temp_path, target)
            if validate_pdf(target) != info:
                raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_STAGE_RECOVERY_HOLD")
        except StreamStateConflictError as error:
            state.hold_file_operation(operation["operation_id"], error.support_ref)
            raise
        except (OSError, ArchiveConflictError, InvalidPdfError, ConfigError, StateError):
            try:
                state.hold_file_operation(operation["operation_id"], "EG_DRIVE_STAGE_RECOVERY_HOLD")
            except Exception:
                raise
            raise StreamStateConflictError(invoice["stream"], "EG_DRIVE_STAGE_RECOVERY_HOLD") from None
        state.complete_file_operation(operation["operation_id"], _utc_now(), evidence_ref="EG_DRIVE_STAGE_HASH_VERIFIED")
        if not already_staged:
            self._record_stage(state, invoice["invoice_id"], relpath, info)
        emit("drive_result", "REPAIRED" if already_staged else "STAGED")
        return info

    def _record_stage(self, state: StateV2Store, invoice_id: str, relpath: str, info: FileInfo) -> None:
        state.update_invoice_file_state(
            invoice_id,
            drive_state="DRIVE_STAGED",
            drive_binding_id=self._binding_id,
            drive_relpath=relpath,
            drive_size=info.byte_size,
            drive_sha256=info.sha256,
            drive_staged_at_utc=_utc_now(),
        )


def _copy_exclusive(source: Path, destination: Path, expected: FileInfo) -> None:
    digest = hashlib.sha256()
    copied = 0
    try:
        with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                copied += len(chunk)
                digest.update(chunk)
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    except FileExistsError:
        raise ArchiveConflictError("Drive temporary path already exists") from None
    except OSError as exc:
        raise StateError("Drive stage copy failed") from exc
    if copied != expected.byte_size or digest.hexdigest() != expected.sha256:
        raise ArchiveConflictError("Drive stage source changed during copy")


def _publish_link_no_replace(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except FileExistsError:
        raise ArchiveConflictError("Drive destination appeared during stage") from None
    except OSError as exc:
        raise StateError("Drive no-replace publication failed") from exc
    try:
        source.unlink()
    except OSError as exc:
        # The final destination is already durable; leave both paths for
        # recovery rather than rolling back or replacing either file.
        raise StateError("Drive temporary cleanup requires recovery") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
