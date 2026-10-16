"""Build and POST a PII-free EnergyGrid end-of-run report to LOCAL n8n.

This script reads Core-generated acquire/status JSON files only. It never touches
EnergyGrid SQLite, Drive, Gmail or the existing scheduler. No retries, redirects,
or non-local destinations are permitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "xb.energygrid.monitor.v1"
STREAMS = ("EB_BILL", "TENANT_BILL")
GOOD_ACQUISITION = frozenset(("ADMITTED", "ALREADY_ADMITTED", "PREDECESSOR_DELIVERED", "EMPTY"))
RUN_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
REF_RE = re.compile(r"^EG_[A-Z0-9_]{4,72}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
URL_PATH = "/webhook/xb-energygrid-monitor-v1"
MAX_REPORT_BYTES = 512 * 1024


class ReportError(Exception):
    pass


def _closed_ref(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and REF_RE.fullmatch(value) else fallback


def _incident_ref(details: object) -> str | None:
    """Hash local filename to retain incident identity but never transmit names."""
    if not isinstance(details, list):
        return None
    for item in details:
        if not isinstance(item, dict):
            continue
        drive = item.get("drive_upload")
        if not isinstance(drive, dict) or drive.get("state") == "VERIFIED":
            continue
        name = item.get("display_filename")
        if isinstance(name, str) and 0 < len(name) <= 260:
            return "bill-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return None


def summarise(acquire: dict, status: dict, now: datetime | None = None) -> dict:
    """Fail-closed acquisition precedence: DRAINED does not erase LIST/FETCH failures."""
    if not isinstance(acquire, dict) or not isinstance(status, dict):
        raise ReportError("EG_MONITOR_INVALID_CORE_JSON")
    run_id = acquire.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ReportError("EG_MONITOR_RUN_ID_UNAVAILABLE")
    acquired = acquire.get("acquisition")
    streams = status.get("streams")
    if not isinstance(acquired, dict) or not isinstance(streams, dict):
        raise ReportError("EG_MONITOR_STREAM_REPORT_MISSING")
    report = {}
    for stream in STREAMS:
        ar = acquired.get(stream)
        sr = streams.get(stream)
        if not isinstance(ar, dict) or not isinstance(sr, dict):
            raise ReportError("EG_MONITOR_STREAM_MISSING")
        status_word = ar.get("status")
        drive_state = sr.get("state")
        identity = _incident_ref(sr.get("invoices"))
        if status_word not in GOOD_ACQUISITION:
            result, support_ref = "FAILURE", _closed_ref(ar.get("support_ref"), "EG_MONITOR_ACQUISITION_FAILED")
        elif drive_state == "BLOCKED":
            result, support_ref = "FAILURE", _closed_ref(sr.get("support_ref"), "EG_MONITOR_DRIVE_BLOCKED")
        elif drive_state == "PENDING":
            result, support_ref = "PENDING", "EG_MONITOR_PENDING_AFTER_RUN"
        elif drive_state == "DRAINED":
            result, support_ref, identity = "OK", None, None
        else:
            result, support_ref = "FAILURE", "EG_MONITOR_DRIVE_STATE_INVALID"
        report[stream] = {"result": result, "support_ref": support_ref, "incident_ref": identity}
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    return {"schema": SCHEMA, "run_id": run_id, "observed_at_utc": at.isoformat().replace("+00:00", "Z"),
            "streams": report}


def _json_file(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_REPORT_BYTES:
        raise ReportError("EG_MONITOR_INPUT_NOT_REGULAR_BOUNDED_FILE")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReportError("EG_MONITOR_INPUT_UNREADABLE") from exc
    if not isinstance(obj, dict):
        raise ReportError("EG_MONITOR_INPUT_NOT_OBJECT")
    return obj


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1")
            or parsed.port is None or not (1 <= parsed.port <= 65535) or parsed.path != URL_PATH
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ReportError("EG_MONITOR_NONLOCAL_ENDPOINT_REFUSED")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(endpoint: str, token_file: Path, payload: dict) -> int:
    _validate_endpoint(endpoint)
    if not token_file.is_file() or token_file.is_symlink():
        raise ReportError("EG_MONITOR_TOKEN_MISSING")
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ReportError("EG_MONITOR_TOKEN_UNREADABLE") from exc
    if not TOKEN_RE.fullmatch(token):
        raise ReportError("EG_MONITOR_TOKEN_INVALID")
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-EnergyGrid-Monitor-Key": token})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=5) as response:
            if response.status < 200 or response.status >= 300:
                raise ReportError("EG_MONITOR_POST_REJECTED")
            return response.status
    except (urllib.error.URLError, OSError) as exc:
        raise ReportError("EG_MONITOR_LOCAL_WEBHOOK_UNAVAILABLE") from exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Report latest completed EnergyGrid Core run to local n8n")
    p.add_argument("--acquire-file", type=Path, required=True)
    p.add_argument("--status-file", type=Path, required=True)
    p.add_argument("--endpoint", default="http://127.0.0.1:5678" + URL_PATH)
    p.add_argument("--token-file", type=Path)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    try:
        report = summarise(_json_file(args.acquire_file), _json_file(args.status_file))
        if args.dry_run:
            print(json.dumps(report, indent=2))  # No file names, mail addresses or tokens.
            return 0
        if args.token_file is None:
            raise ReportError("EG_MONITOR_TOKEN_FILE_REQUIRED")
        _validate_endpoint(args.endpoint)
        send(args.endpoint, args.token_file, report)
        print("EG_MONITOR_LOCAL_REPORT_POST_ACCEPTED")
        return 0
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
