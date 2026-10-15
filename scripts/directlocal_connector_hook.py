"""Claude Code hooks for the #155 DirectLocal attended connector session.

guard   (PreToolUse, matcher ``mcp__.*``): default-deny for every MCP tool.
        Only the pinned DirectLocal queries through the connector
        ``graphql_query`` tool pass; anything else, including malformed hook
        input, exits 2 (blocking). Deny rules in settings are belt-and-braces;
        this allowlist is the enforcement that covers new or renamed tools.
capture (PostToolUse, matcher = the connector graphql_query tool): writes the
        hook-delivered ``tool_input``/``tool_response`` JSON of an allowed call
        to ``<inbox>/<tool_use_id>.json`` (link-based exclusive create, fsync).

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

from xb_directlocal import (  # noqa: E402
    CAPTURE_PROVENANCE,
    CAPTURE_SCHEMA,
    CONNECTOR_TOOL,
    TOOL_USE_ID_RE,
    CaptureRejected,
    check_tool_input,
)

BLOCK = 2


def _read_event() -> dict:
    event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if not isinstance(event, dict):
        raise ValueError("hook_event_invalid")
    return event


def guard() -> int:
    try:
        event = _read_event()
    except (ValueError, UnicodeDecodeError):
        sys.stderr.write("DirectLocal guard: unreadable hook input; tool call blocked.\n")
        return BLOCK
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return 0  # non-MCP tools are governed by the session permission rules
    if tool_name != CONNECTOR_TOOL:
        sys.stderr.write("DirectLocal guard: only the pinned connector graphql_query is allowed in this session.\n")
        return BLOCK
    try:
        check_tool_input(event.get("tool_input"))
    except CaptureRejected as exc:
        sys.stderr.write(f"DirectLocal guard: blocked ({exc.code}).\n")
        return BLOCK
    return 0


def capture(inbox: Path) -> int:
    try:
        event = _read_event()
    except (ValueError, UnicodeDecodeError):
        sys.stderr.write("DirectLocal capture: unreadable hook input; nothing captured.\n")
        return 1
    if event.get("tool_name") != CONNECTOR_TOOL:
        return 0
    try:
        check_tool_input(event.get("tool_input"))
    except CaptureRejected:
        return 0  # not a pinned DirectLocal query; never captured
    tool_use_id = event.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not TOOL_USE_ID_RE.fullmatch(tool_use_id):
        sys.stderr.write("DirectLocal capture: tool_use_id invalid; nothing captured.\n")
        return 1
    if "tool_response" not in event:
        sys.stderr.write("DirectLocal capture: tool_response missing; nothing captured.\n")
        return 1
    if not inbox.is_dir():
        sys.stderr.write("DirectLocal capture: inbox missing; nothing captured.\n")
        return 1
    record = {
        "schema_version": CAPTURE_SCHEMA,
        "provenance": CAPTURE_PROVENANCE,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "session_id": event.get("session_id") if isinstance(event.get("session_id"), str) else None,
        "tool_use_id": tool_use_id,
        "tool_name": CONNECTOR_TOOL,
        "tool_input": event["tool_input"],
        "tool_response": event["tool_response"],
    }
    data = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    final = inbox / f"{tool_use_id}.json"
    temporary = inbox / f".{tool_use_id}.{os.getpid()}.tmp"
    with open(temporary, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, final)  # fails if the final record already exists
    except FileExistsError:
        sys.stderr.write("DirectLocal capture: duplicate tool_use_id; existing record kept.\n")
        return 1
    finally:
        temporary.unlink()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "guard":
        return guard()
    if len(argv) == 3 and argv[0] == "capture" and argv[1] == "--inbox":
        return capture(Path(argv[2]))
    sys.stderr.write("usage: directlocal_connector_hook.py guard | capture --inbox <dir>\n")
    return BLOCK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
