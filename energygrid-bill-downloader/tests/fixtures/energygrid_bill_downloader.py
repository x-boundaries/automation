"""Bounded genuine-Python child used by the one-shot supervisor tests."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


def verify_committed_module_identity() -> None:
    expected_path = os.environ.get("EG_TEST_MODULE_PATH", "")
    expected_hash = os.environ.get("EG_TEST_MODULE_SHA256", "")
    actual_path = Path(__file__).resolve()
    if not expected_path or actual_path != Path(expected_path).resolve():
        raise RuntimeError("fixture module path mismatch")
    if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash.lower()):
        raise RuntimeError("fixture module hash is missing")
    if hashlib.sha256(actual_path.read_bytes()).hexdigest() != expected_hash.lower():
        raise RuntimeError("fixture module hash mismatch")
    fixture_root = str(actual_path.parent)
    if os.environ.get("PYTHONPATH") != fixture_root:
        raise RuntimeError("fixture module search path mismatch")
    if os.environ.get("PYTHONHOME"):
        raise RuntimeError("unexpected Python home")
    if os.environ.get("PYTHONNOUSERSITE") != "1":
        raise RuntimeError("Python user site is enabled")
    if os.environ.get("PYTHONDONTWRITEBYTECODE") != "1" or not sys.dont_write_bytecode:
        raise RuntimeError("Python bytecode output is enabled")
    allowed_roots = {
        Path(fixture_root).resolve(),
        Path.cwd().resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
    }
    for item in sys.path:
        if not item:
            continue
        resolved = Path(item).resolve()
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise RuntimeError("unexpected Python module search path")


def _read_config(path: str) -> dict[str, object]:
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("fixture config must be a JSON object")
    return value


def _bounded_delay(value: object, default: float) -> float:
    seconds = default if value is None else float(value)
    if not 0.0 <= seconds <= 60.0:
        raise ValueError("fixture duration is outside its bound")
    return seconds


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    for key in ("PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTHONINSPECT"):
        environment.pop(key, None)
    return environment


def _module_child(
    operation: str, config_path: str, *, canonical_command_line: bool = False
) -> subprocess.Popen[bytes]:
    arguments = [
        sys.executable,
        "-m",
        "energygrid_bill_downloader",
        operation,
        "--config",
        config_path,
    ]
    if canonical_command_line and os.name == "nt":
        if any('"' in argument for argument in arguments):
            raise ValueError("canonical fixture arguments cannot contain quotes")
        return subprocess.Popen(
            " ".join(f'"{argument}"' for argument in arguments),
            executable=sys.executable,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=_child_environment(),
        )
    return subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=_child_environment(),
    )


def _write_marker(path_value: object, value: str) -> None:
    if isinstance(path_value, str) and path_value:
        Path(path_value).write_text(value, encoding="ascii")


def _run(config_path: str, mode: str) -> int:
    config = _read_config(config_path)
    if mode == "list":
        _write_marker(
            config.get("ready_path"),
            json.dumps({"pid": os.getpid(), "parent_pid": os.getppid()}, separators=(",", ":")),
        )
        time.sleep(_bounded_delay(config.get("list_delay_seconds"), 2.5))
        return 0
    if mode != "run":
        raise ValueError("unsupported fixture operation")

    child_count = config.get("descendant_count", 0)
    if isinstance(child_count, bool) or not isinstance(child_count, int) or not 0 <= child_count <= 40:
        raise ValueError("descendant count is outside its bound")
    detached_descendants = config.get("detached_descendants", False)
    if not isinstance(detached_descendants, bool):
        raise ValueError("detached descendant flag is invalid")
    children: list[subprocess.Popen[bytes]] = []
    try:
        marker_value = json.dumps(
            {"pid": os.getpid(), "parent_pid": os.getppid()},
            sort_keys=True,
            separators=(",", ":"),
        )
        _write_marker(config.get("ready_path"), marker_value)
        sys.stdout.write("fixture_stdout=ready\n")
        sys.stdout.flush()
        sys.stderr.write("fixture_stderr=ready\n")
        sys.stderr.flush()
        for _ in range(child_count):
            child = _module_child("tree-child", config_path)
            children.append(child)
        time.sleep(_bounded_delay(config.get("run_delay_seconds"), 2.5))
        release_path = config.get("release_path")
        if isinstance(release_path, str) and release_path:
            deadline = time.monotonic() + 60.0
            while not Path(release_path).is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not Path(release_path).is_file():
                raise TimeoutError("release marker timed out")
        if not detached_descendants:
            child_wait = _bounded_delay(config.get("child_wait_seconds"), 60.0)
            for child in children:
                child.wait(timeout=child_wait + 5.0)
                if child.returncode != 0:
                    raise RuntimeError("descendant process failed")
        return 0
    finally:
        if not detached_descendants:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                if child.poll() is None:
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)


def _tree_child(config_path: str) -> int:
    config = _read_config(config_path)
    _write_marker(
        config.get("grandchild_ready_path"),
        json.dumps({"pid": os.getpid(), "parent_pid": os.getppid()}, separators=(",", ":")),
    )
    time.sleep(_bounded_delay(config.get("tree_child_delay_seconds"), 60.0))
    return 0


def _noise_child(config_path: str) -> int:
    _read_config(config_path)
    time.sleep(0.15)
    return 0


def _wrong_parent_child(config_path: str) -> int:
    config = _read_config(config_path)
    child = _module_child("run", config_path, canonical_command_line=True)
    try:
        time.sleep(_bounded_delay(config.get("wrong_parent_delay_seconds"), 30.0))
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        return 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def _crash_child(config_path: str) -> int:
    config = _read_config(config_path)
    ready_path = config.get("ready_path")
    grandchild_ready_path = config.get("grandchild_ready_path")
    child = None
    if isinstance(grandchild_ready_path, str) and grandchild_ready_path:
        child = _module_child("tree-child", config_path)
        deadline = time.monotonic() + 10.0
        while not Path(grandchild_ready_path).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not Path(grandchild_ready_path).exists():
            child.kill()
            child.wait(timeout=5)
            raise TimeoutError("grandchild readiness timed out")
    if isinstance(ready_path, str) and ready_path:
        value: dict[str, int] = {"pid": os.getpid(), "parent_pid": os.getppid()}
        if child is not None:
            grandchild = json.loads(Path(grandchild_ready_path).read_text(encoding="ascii"))
            value["grandchild_pid"] = int(grandchild["pid"])
            value["grandchild_parent_pid"] = int(grandchild["parent_pid"])
        Path(ready_path).write_text(json.dumps(value, separators=(",", ":")), encoding="ascii")
    time.sleep(60.0)
    return 0


def _saturate() -> int:
    byte_count = 1024 * 1024
    payload = b"S" * byte_count
    start_gate = threading.Event()
    errors: list[BaseException] = []

    def write_stream(stream: object) -> None:
        try:
            start_gate.wait(timeout=10)
            stream.write(payload)
            stream.flush()
        except BaseException as error:
            errors.append(error)

    stdout_writer = threading.Thread(target=write_stream, args=(sys.stdout.buffer,), daemon=True)
    stderr_writer = threading.Thread(target=write_stream, args=(sys.stderr.buffer,), daemon=True)
    stdout_writer.start()
    stderr_writer.start()
    start_gate.set()
    stdout_writer.join(timeout=30)
    stderr_writer.join(timeout=30)
    if stdout_writer.is_alive() or stderr_writer.is_alive():
        raise TimeoutError("stream saturation writer timed out")
    if errors:
        raise RuntimeError("stream saturation writer failed") from errors[0]
    return 0


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation", _FileTime),
        ("last_access", _FileTime),
        ("last_write", _FileTime),
        ("volume_serial", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32),
        ("index_low", ctypes.c_uint32),
    ]


def _handle_canary(handle_value: int, volume_serial: int, file_index: int) -> int:
    if os.name != "nt" or handle_value <= 0:
        raise ValueError("invalid inherited handle")
    info = _ByHandleFileInformation()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.GetFileInformationByHandle
    query.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ByHandleFileInformation))
    query.restype = ctypes.c_int
    if not query(ctypes.c_void_p(handle_value), ctypes.byref(info)):
        print("handle_canary=PASS identity=not_inherited", flush=True)
        return 0
    observed_index = (int(info.index_high) << 32) | int(info.index_low)
    if int(info.volume_serial) == volume_serial and observed_index == file_index:
        print("handle_canary=FAIL identity=inherited", flush=True)
        return 1
    print("handle_canary=PASS identity=not_inherited", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "operation",
        choices=(
            "run",
            "list",
            "noise-child",
            "tree-child",
            "saturate",
            "crash-child",
            "wrong-parent-child",
            "handle-canary",
        ),
    )
    parser.add_argument("--config")
    parser.add_argument("--handle", type=int)
    parser.add_argument("--volume", type=int)
    parser.add_argument("--file-index", type=int)
    args = parser.parse_args()
    verify_committed_module_identity()
    if args.operation == "saturate":
        return _saturate()
    if args.operation == "handle-canary":
        if args.handle is None or args.volume is None or args.file_index is None:
            raise ValueError("handle identity arguments are required")
        return _handle_canary(args.handle, args.volume, args.file_index)
    if not args.config:
        raise ValueError("fixture config is required")
    if args.operation in {"run", "list"}:
        return _run(args.config, args.operation)
    if args.operation == "noise-child":
        return _noise_child(args.config)
    if args.operation == "tree-child":
        return _tree_child(args.config)
    if args.operation == "crash-child":
        return _crash_child(args.config)
    return _wrong_parent_child(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
