"""Claude Code hooks for the #155 DirectLocal attended connector session.

guard   (PreToolUse, matcher ``mcp__.*``): default-deny for every MCP tool.
        Only the pinned DirectLocal queries through the connector
        ``graphql_query`` tool pass; anything else, including malformed hook
        input, exits 2 (blocking). Deny rules in settings are belt-and-braces;
        this allowlist is the enforcement that covers new or renamed tools.
        Fail closed (G2 O10, 2026-10-16): Claude Code treats any other non-zero
        exit as non-blocking, so every unexpected failure (for example
        RecursionError on deeply nested JSON, or the core failing to import)
        also exits 2, with a fixed message and never the payload.
capture (PostToolUse, matcher = the connector graphql_query tool): writes the
        hook-delivered ``tool_input``/``tool_response`` JSON of an allowed call
        to ``<inbox>/<tool_use_id>.json`` (link-based exclusive create, fsync).
        Any failure leaves no record and no partial or temporary file.

The record is the structured JSON Claude Code hands to the hook. It is not
proof of the original Shopify wire bytes, and its response shape is only
accepted by the core once an attended gate has qualified it.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "directlocal"))

try:
    import xb_directlocal as core  # noqa: E402
except Exception:  # noqa: BLE001 - guard() blocks while the core cannot load
    core = None

BLOCK = 2


def _read_event() -> dict:
    event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if not isinstance(event, dict):
        raise ValueError("hook_event_invalid")
    return event


def _guard() -> int:
    if core is None:
        sys.stderr.write("DirectLocal guard: core unavailable; tool call blocked.\n")
        return BLOCK
    try:
        event = _read_event()
    except (ValueError, UnicodeDecodeError):
        sys.stderr.write("DirectLocal guard: unreadable hook input; tool call blocked.\n")
        return BLOCK
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return 0  # non-MCP tools are governed by the session permission rules
    if tool_name != core.CONNECTOR_TOOL:
        sys.stderr.write("DirectLocal guard: only the pinned connector graphql_query is allowed in this session.\n")
        return BLOCK
    try:
        core.check_tool_input(event.get("tool_input"))
    except core.CaptureRejected as exc:
        sys.stderr.write(f"DirectLocal guard: blocked ({exc.code}).\n")
        return BLOCK
    return 0


def guard() -> int:
    try:
        return _guard()
    except Exception:  # noqa: BLE001 - RecursionError, MemoryError or anything else: block
        sys.stderr.write("DirectLocal guard: hook input could not be checked; tool call blocked.\n")
        return BLOCK


def _capture(inbox: Path) -> int:
    if core is None:
        sys.stderr.write("DirectLocal capture: core unavailable; nothing captured.\n")
        return 1
    try:
        event = _read_event()
    except (ValueError, UnicodeDecodeError):
        sys.stderr.write("DirectLocal capture: unreadable hook input; nothing captured.\n")
        return 1
    if event.get("tool_name") != core.CONNECTOR_TOOL:
        return 0
    try:
        core.check_tool_input(event.get("tool_input"))
    except core.CaptureRejected:
        return 0  # not a pinned DirectLocal query; never captured
    tool_use_id = event.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not core.TOOL_USE_ID_RE.fullmatch(tool_use_id):
        sys.stderr.write("DirectLocal capture: tool_use_id invalid; nothing captured.\n")
        return 1
    if "tool_response" not in event:
        sys.stderr.write("DirectLocal capture: tool_response missing; nothing captured.\n")
        return 1
    if not inbox.is_dir():
        sys.stderr.write("DirectLocal capture: inbox missing; nothing captured.\n")
        return 1
    record = {
        "schema_version": core.CAPTURE_SCHEMA,
        "provenance": core.CAPTURE_PROVENANCE,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "session_id": event.get("session_id") if isinstance(event.get("session_id"), str) else None,
        "tool_use_id": tool_use_id,
        "tool_name": core.CONNECTOR_TOOL,
        "tool_input": event["tool_input"],
        "tool_response": event["tool_response"],
    }
    # Serialised before any file exists, so a failure here writes nothing.
    data = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    final = inbox / f"{tool_use_id}.json"
    temporary = inbox / f".{tool_use_id}.{os.getpid()}.tmp"
    handle = open(temporary, "xb")
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, final)  # fails if the final record already exists
    except FileExistsError:
        sys.stderr.write("DirectLocal capture: duplicate tool_use_id; existing record kept.\n")
        return 1
    finally:
        temporary.unlink()  # only ever this call's own temporary file
    return 0


def capture(inbox: Path) -> int:
    try:
        return _capture(inbox)
    except Exception:  # noqa: BLE001 - never persist or echo malformed input
        sys.stderr.write("DirectLocal capture: hook input could not be checked; nothing captured.\n")
        return 1


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "guard":
        return guard()
    if len(argv) == 3 and argv[0] == "capture" and argv[1] == "--inbox":
        return capture(Path(argv[2]))
    sys.stderr.write("usage: directlocal_connector_hook.py guard | capture --inbox <dir>\n")
    return BLOCK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
