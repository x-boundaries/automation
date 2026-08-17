from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .config import is_within, resolved
from .errors import ArchiveConflictError, ConfigError, InvalidPdfError, StateError


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
WINDOWS_INVALID_FILENAME_CHARS = '<>:"/|?*'
RUN_DIRECTORY_RE = re.compile(
    r"^run-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
PDF_TAIL_WINDOW = 4096


@dataclass(frozen=True)
class FileInfo:
    byte_size: int
    sha256: str


def filename_key(filename: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", filename).casefold()


def validate_filename(filename: str, archive_root: Path) -> Path:
    if not isinstance(filename, str) or not filename or filename in {".", ".."}:
        raise ConfigError("filename is empty or invalid")
    if any(ord(char) < 32 for char in filename):
        raise ConfigError("filename contains a control character")
    if any(char in WINDOWS_INVALID_FILENAME_CHARS or char == chr(92) for char in filename):
        raise ConfigError("filename contains a path or device separator")
    if PureWindowsPath(filename).drive or PureWindowsPath(filename).root:
        raise ConfigError("filename contains a Windows drive or root")
    if filename.endswith((".", " ")):
        raise ConfigError("filename has a trailing dot or space")
    if not filename.lower().endswith(".pdf"):
        raise ConfigError("filename must have a PDF extension")
    stem = filename.rsplit(".", 1)[0].rstrip(" .").upper()
    if stem in WINDOWS_RESERVED_NAMES:
        raise ConfigError("filename uses a reserved Windows device name")
    final_path = resolved(archive_root / filename)
    if not is_within(final_path, archive_root):
        raise ConfigError("filename escapes archive_root")
    return final_path


def validate_pdf(path: Path) -> FileInfo:
    try:
        if not path.is_file():
            raise InvalidPdfError("download is not a regular file")
        byte_size = path.stat().st_size
        if byte_size <= 0:
            raise InvalidPdfError("download is empty")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            header = handle.read(5)
            if header != b"%PDF-":
                raise InvalidPdfError("download does not have a PDF header")
            handle.seek(max(0, byte_size - PDF_TAIL_WINDOW))
            tail = handle.read(PDF_TAIL_WINDOW)
        if not tail.rstrip().endswith(b"%%EOF"):
            raise InvalidPdfError("download has no terminal PDF EOF marker")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return FileInfo(byte_size=byte_size, sha256=digest.hexdigest())
    except InvalidPdfError:
        raise
    except OSError as exc:
        raise InvalidPdfError("download could not be read") from exc


def volume_identity(path: Path) -> tuple[int, str]:
    try:
        device = os.stat(path).st_dev
    except OSError as exc:
        raise ConfigError("publication volume could not be inspected") from exc
    drive = os.path.splitdrive(str(path))[0].casefold()
    return device, drive


def ensure_same_volume(source: Path, destination: Path) -> None:
    source_device, source_drive = volume_identity(source)
    destination_device, destination_drive = volume_identity(destination.parent)
    if source_device != destination_device:
        raise ConfigError("cross-volume publication is forbidden")
    if os.name == "nt" and source_drive != destination_drive:
        raise ConfigError("source and destination drives differ")


def publish_no_replace(source: Path, destination: Path) -> None:
    if destination.exists():
        raise ArchiveConflictError("archive destination already exists")
    if not source.is_file():
        raise StateError("owned publication source is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ensure_same_volume(source, destination)
    if os.name != "nt":
        raise ConfigError("v1 publication requires Windows MoveFileExW")

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file_ex.restype = ctypes.c_int
    movefile_write_through = 0x00000008
    result = move_file_ex(str(source), str(destination), movefile_write_through)
    if not result:
        if destination.exists():
            raise ArchiveConflictError("archive destination appeared during publication")
        error_code = ctypes.get_last_error()
        raise StateError(f"no-replace publication failed with Windows error {error_code}")


def create_run_directory(temp_root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not RUN_DIRECTORY_RE.fullmatch(f"run-{run_id}"):
        raise ConfigError("run_id is not a UUID")
    run_dir = resolved(temp_root / f"run-{run_id}")
    if run_dir.parent != resolved(temp_root):
        raise ConfigError("run directory escaped temp_root")
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def cleanup_run_directory(run_dir: Path, temp_root: Path) -> None:
    run_dir = resolved(run_dir)
    temp_root = resolved(temp_root)
    if run_dir.parent != temp_root or not RUN_DIRECTORY_RE.fullmatch(run_dir.name):
        raise ConfigError("refusing to clean an unowned run directory")
    if run_dir.exists():
        shutil.rmtree(run_dir)


def cleanup_stale_owned_temp(temp_root: Path, older_than_seconds: int = 24 * 60 * 60) -> int:
    """Remove only direct, UUID-shaped operation directories older than the bound."""

    import time

    root = resolved(temp_root)
    if not root.exists():
        return 0
    now = time.time()
    removed = 0
    for child in root.iterdir():
        if not child.is_dir() or not RUN_DIRECTORY_RE.fullmatch(child.name):
            continue
        try:
            age = now - child.stat().st_mtime
        except OSError as exc:
            raise StateError("owned temp artifact could not be inspected") from exc
        if age >= older_than_seconds:
            cleanup_run_directory(child, root)
            removed += 1
    return removed
