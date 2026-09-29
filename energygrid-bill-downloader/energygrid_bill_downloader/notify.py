"""Privacy-minimal terminal-failure alert (DL-XB-199 G3-101).

runtime -> loopback n8n ingress -> existing Telegram capability -> Owner.

The payload is built only from closed, validated values: a fixed event name,
a UTC timestamp, the run UUID, a closed stage, the result status, a bounded
support reference, the exit code, four counts and `attention_required`. It
never carries a tenant/account identifier, an endpoint, a filename, bill
content, a header or a credential value; a value that fails validation is
replaced by a fixed sentinel rather than passed through.

Delivery is best effort with one bounded attempt. A notifier failure is
recorded as a closed log phase and NEVER changes the run's status or exit code.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .config import AlertSettings
from .errors import SUPPORT_REF_PATTERN


ALERT_SCHEMA = "energygrid.alert.v1"
ALERT_EVENT = "energygrid_run_failed"
ALERT_TIMEOUT_SECONDS = 5
ALERT_STAGES = frozenset({"config", "lock", "list", "fetch", "publish", "run"})
ALERT_STATUSES = frozenset(
    {
        "RETRYABLE_NETWORK_FAILURE",
        "LOGIN_FAILED",
        "PORTAL_LAYOUT_CHANGED",
        "DOWNLOAD_FAILED",
        "INVALID_PDF",
        "ARCHIVE_CONFLICT",
        "STATE_INCONSISTENT",
        "ACTION_REQUIRED",
        "RUN_IN_PROGRESS",
    }
)
ALERT_KEYS = (
    "schema",
    "event",
    "timestamp",
    "run_id",
    "stage",
    "status",
    "support_ref",
    "exit_code",
    "counts",
    "attention_required",
)
COUNT_KEYS = ("inventory", "downloaded", "present", "failure")
UNKNOWN = "UNKNOWN"
UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
NOT_ATTENTION_STATUSES = frozenset({"RUN_IN_PROGRESS"})


def build_alert_payload(
    *,
    run_id: Any,
    stage: Any,
    status: Any,
    support_ref: Any,
    exit_code: Any,
    counts: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    counts = counts or {}
    safe_status = status if isinstance(status, str) and status in ALERT_STATUSES else UNKNOWN
    return {
        "schema": ALERT_SCHEMA,
        "event": ALERT_EVENT,
        "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id if isinstance(run_id, str) and UUID_RE.fullmatch(run_id) else UNKNOWN,
        "stage": stage if isinstance(stage, str) and stage in ALERT_STAGES else "run",
        "status": safe_status,
        "support_ref": (
            support_ref
            if isinstance(support_ref, str) and re.fullmatch(SUPPORT_REF_PATTERN, support_ref)
            else "APP_ERROR_UNCLASSIFIED"
        ),
        "exit_code": exit_code if type(exit_code) is int and 0 <= exit_code <= 255 else -1,
        "counts": {key: _count(counts.get(key)) for key in COUNT_KEYS},
        "attention_required": safe_status not in NOT_ATTENTION_STATUSES,
    }


def _count(value: Any) -> int:
    return value if type(value) is int and 0 <= value <= 1_000_000 else 0


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def send_alert(settings: AlertSettings, payload: dict[str, Any]) -> bool:
    """POST the payload once. Returns True on a 2xx. Never raises."""

    try:
        if tuple(payload) != ALERT_KEYS:
            return False
        headers = {"Content-Type": "application/json"}
        if settings.auth_header_name is not None:
            token = os.environ.get(settings.auth_token_env or "")
            if not token:
                return False
            headers[settings.auth_header_name] = token
        data = json.dumps(payload, sort_keys=False, ensure_ascii=True).encode("ascii")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect())
        request = urllib.request.Request(settings.url, data=data, method="POST", headers=headers)
        try:
            response = opener.open(request, timeout=ALERT_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            exc.close()
            return False
        with response:
            response.read(64 * 1024)
            return 200 <= response.status < 300
    except Exception:
        # Losing the alert is strictly less harmful than masking or changing
        # the product result; the caller records a closed log phase instead.
        return False
