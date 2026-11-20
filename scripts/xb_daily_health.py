"""Daily health checker on the server PC (#325 slice H1).

Read-only. Once a day (task ``\\XB\\XB-DailyHealth``, 07:30 SGT) it reads:

- the server's own Task Scheduler folder ``\\XB\\`` (``Get-ScheduledTask`` / ``Get-ScheduledTaskInfo``
  through Windows PowerShell; nothing is started, stopped or changed);
- the AC2 VM's status files that already reach this PC through Google Drive for desktop: the hourly
  stock export and the AR/AP aging export, each a ``last_run_status.json`` + ``latest_manifest.json`` pair;
- the weekly Drive mirror's ``last_run_status.json`` in its local log folder;
- its own records of the last 14 days (``health_<date>.json``).

It judges each job OK, WARN or FAIL by the rules in ``docs/daily_health_check.md``, writes
``health_<date>.json`` (codes, counts and dates only) into a private local folder, and POSTs one
closed, codes-only ``xb.daily_health.v1`` event to the loopback webhook of the n8n workflow
``XB Daily Health`` (10 s timeout, no retry, no proxy, no redirect). That workflow adds the n8n facts,
writes the email lines from fixed templates and sends the email through ``XB Ops Alert Email``.

The event carries job ids from a fixed list, closed reason codes, whole numbers and dates. It never
carries a task name, a path, a host name, a message or any business data. The webhook secret is read
from Windows Credential Manager by target name; it is never printed, logged or written.

Commands::

    python scripts/xb_daily_health.py check-settings --config config/daily_health.local.json
    python scripts/xb_daily_health.py run --config config/daily_health.local.json [--no-send]

``run`` always exits 0, also when the POST fails (the record says ``failed`` and the next day's
check reports ``previous_send_failed``). Only broken settings exit 2, with a reason code on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

# Singapore has no daylight saving time, so a fixed offset needs no time zone database.
SGT = timezone(timedelta(hours=8))

EVENT_SCHEMA = "xb.daily_health.v1"
SETTINGS_SCHEMA = "xb.daily_health.settings.v1"
RECORD_SCHEMA = "xb.daily_health.record.v1"
EXIT_OK = 0
EXIT_SETTINGS = 2

# Every job the health check knows, in email order. The n8n workflow holds the same list.
JOBS = (
    "member_sync_summary",
    "member_tags",
    "ac2_stock_export",
    "stock_alert_route",
    "arap_export",
    "arap_alert_route",
    "energygrid_daily_bills",
    "energygrid_retention",
    "drive_mirror",
    "n8n_workflows",
    "simba_intake",
    "server_tasks",
    "checker",
    "drive_folder",
)
# Judged inside n8n (the checker cannot see them): the event marks them "N8N".
N8N_JOBS = ("member_sync_summary", "member_tags", "n8n_workflows", "simba_intake")
EXPECTED_VALUES = ("on", "off", "not_installed")
PAUSE_REASONS = ("sitting", "maintenance", "vm_work", "owner_request")
PAUSE_MAX_DAYS = 7
EXPIRED_PAUSE_LOOKBACK_DAYS = 7
RECORD_LOOKBACK_DAYS = 14
WARN, FAIL = "WARN", "FAIL"

# Closed reason codes per checker-judged job, with the verdict each one gives.
REASONS: dict[str, dict[str, str]] = {
    "ac2_stock_export": {
        "pause_expired": FAIL, "status_missing": FAIL, "last_run_too_old": FAIL, "manifest_missing": FAIL,
        "manifest_too_old": FAIL, "run_too_slow": FAIL, "last_run_old": WARN, "run_failed": WARN,
        "delivery_failed": WARN, "daily_close_failed": WARN, "analysis_failed": WARN, "run_slow": WARN,
    },
    "stock_alert_route": {
        "pause_expired": FAIL, "alert_send_failed": WARN, "alert_secret_unavailable": WARN, "alert_error": WARN,
    },
    "arap_export": {
        "pause_expired": FAIL, "status_missing": FAIL, "last_run_too_old": FAIL, "manifest_missing": FAIL,
        "manifest_too_old": FAIL, "last_run_old": WARN, "run_failed": WARN, "delivery_failed": WARN,
    },
    # #321 L3: the AR/AP alert route, judged exactly like the stock alert route.
    "arap_alert_route": {
        "pause_expired": FAIL, "alert_send_failed": WARN, "alert_secret_unavailable": WARN, "alert_error": WARN,
    },
    "energygrid_daily_bills": {
        "pause_expired": FAIL, "task_missing": FAIL, "task_disabled": FAIL, "last_run_too_old": FAIL,
        "task_query_failed": WARN, "task_result_nonzero": WARN, "last_run_old": WARN,
    },
    "energygrid_retention": {
        "pause_expired": FAIL, "task_missing": FAIL, "task_disabled": FAIL, "last_run_too_old": FAIL,
        "task_query_failed": WARN, "retention_held": WARN, "retention_refused": WARN, "task_result_nonzero": WARN,
        "last_run_old": WARN,
    },
    "drive_mirror": {
        "pause_expired": FAIL, "task_missing": FAIL, "task_disabled": FAIL, "mirror_failed": FAIL,
        "last_run_too_old": FAIL, "task_query_failed": WARN, "mirror_fetch_failed": WARN, "mirror_locked": WARN,
        "mirror_stuck_running": WARN, "task_result_nonzero": WARN, "last_run_old": WARN,
    },
    "server_tasks": {"pause_expired": FAIL, "task_query_failed": WARN, "unknown_task": WARN},
    "checker": {"pause_expired": FAIL, "previous_send_failed": WARN},
    "drive_folder": {"pause_expired": FAIL, "folder_missing": FAIL},
}
MAX_REASONS = 6

# Default thresholds in minutes (design section 3.2). Settings may override each within its range.
THRESHOLDS: dict[str, tuple[int, int, int]] = {
    # name: (default, low, high)
    "stock_warn_age": (70, 30, 600),
    "stock_fail_age": (180, 60, 1440),
    "stock_manifest_fail_age": (180, 60, 1440),
    "stock_warn_run_seconds": (450, 60, 900),
    "stock_fail_run_seconds": (720, 60, 900),
    "arap_warn_age": (90, 30, 600),
    "arap_fail_age": (180, 60, 1440),
    "arap_manifest_fail_age": (180, 60, 1440),
    "vm_down_similar_age": (90, 10, 600),
    "energygrid_bills_warn_age": (26 * 60, 60, 7 * 1440),
    "energygrid_bills_fail_age": (50 * 60, 60, 7 * 1440),
    "energygrid_retention_warn_age": (26 * 60, 60, 7 * 1440),
    "energygrid_retention_fail_age": (50 * 60, 60, 7 * 1440),
    "mirror_warn_age": (8 * 1440, 1440, 30 * 1440),
    "mirror_fail_age": (15 * 1440, 1440, 60 * 1440),
}
WARN_FAIL_PAIRS = (
    ("stock_warn_age", "stock_fail_age"), ("stock_warn_run_seconds", "stock_fail_run_seconds"),
    ("arap_warn_age", "arap_fail_age"), ("energygrid_bills_warn_age", "energygrid_bills_fail_age"),
    ("energygrid_retention_warn_age", "energygrid_retention_fail_age"), ("mirror_warn_age", "mirror_fail_age"),
)

# Task Scheduler result codes that are a state, not a failed run.
TASK_INFO_RESULTS = (
    0x41300,  # ready
    0x41301,  # running
    0x41302,  # disabled
    0x41303,  # has not run yet
)
RETENTION_HELD, RETENTION_REFUSED = 20, 64
MIRROR_WARN_EXITS = {4: "mirror_fetch_failed", 7: "mirror_locked"}
MIRROR_FAIL_EXITS = (1, 2, 3, 5, 6)
# alert_status values that mean an alert did not reach n8n. The stock and the AR/AP exporters write the
# same values (`suppressed`, `no_alert`, `sent`, `disabled` and `skipped_config` are fine).
ALERT_REASONS = {"send_failed": "alert_send_failed", "secret_unavailable": "alert_secret_unavailable",
                 "error": "alert_error"}
DELIVERY_FAILURES = ("verify_failed", "copy_failed")
STATUS_FILE = "last_run_status.json"
MANIFEST_FILE = "latest_manifest.json"
MAX_JSON_BYTES = 1024 * 1024
TASK_QUERY_TIMEOUT = 60
EXIT_MAX = 0xFFFFFFFF
AGE_MAX = 1000000
COUNT_MAX = 10000

SETTINGS_KEYS = {"schema", "record_dir", "drive_root", "stock_folder", "arap_folder", "mirror_log_dir",
                 "task_path", "tasks", "known_tasks", "expected", "thresholds", "pauses", "send"}
TASK_KEYS = ("energygrid_daily_bills", "energygrid_retention", "drive_mirror", "checker")
SEND_KEYS = {"enabled", "webhook_url", "auth_header_name", "secret_credential_target", "timeout_seconds"}
PAUSE_KEYS = {"job", "until", "reason_code"}
TASK_PATH_RE = re.compile(r"^(\\[A-Za-z0-9_-][A-Za-z0-9 _-]*)+\\$")
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()._-]{0,127}$")
# Loopback only: the request carries the webhook secret, so it must never leave this PC.
WEBHOOK_RE = re.compile(r"^http://(127\.0\.0\.1|localhost):[0-9]{2,5}/webhook/[A-Za-z0-9_-]{8,128}$")
HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
RESERVED_HEADERS = ("content-type", "content-length", "host", "user-agent", "connection", "transfer-encoding")
CREDENTIAL_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$")
UNTIL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
PLACEHOLDER = "REPLACE"
USER_AGENT = "xb-daily-health/1"


class SettingsError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SettingsError(code)


def _absolute(value: Any, code: str) -> str:
    _require(isinstance(value, str) and bool(value) and PLACEHOLDER not in value, code)
    _require(os.path.isabs(value) and not value.startswith("\\\\") and ".." not in Path(value).parts, code)
    return value


def parse_settings(raw: Any, now: datetime) -> dict[str, Any]:
    """Check every value; raise SettingsError with a closed code on the first problem."""

    _require(isinstance(raw, dict), "settings_not_object")
    keys = set(raw) - {"_comment"}
    _require(keys == SETTINGS_KEYS, "settings_keys")
    _require(raw["schema"] == SETTINGS_SCHEMA, "settings_schema")
    settings: dict[str, Any] = {key: _absolute(raw[key], "settings_%s_invalid" % key)
                                for key in ("record_dir", "drive_root", "stock_folder", "arap_folder", "mirror_log_dir")}

    task_path = raw["task_path"]
    _require(isinstance(task_path, str) and TASK_PATH_RE.fullmatch(task_path) is not None, "settings_task_path_invalid")
    settings["task_path"] = task_path

    tasks = raw["tasks"]
    _require(isinstance(tasks, dict) and set(tasks) == set(TASK_KEYS), "settings_tasks_keys")
    for name in tasks.values():
        _require(isinstance(name, str) and TASK_NAME_RE.fullmatch(name) is not None and PLACEHOLDER not in name,
                 "settings_task_name_invalid")
    settings["tasks"] = dict(tasks)

    known = raw["known_tasks"]
    _require(isinstance(known, list) and len(known) <= 50, "settings_known_tasks_invalid")
    for name in known:
        _require(isinstance(name, str) and TASK_NAME_RE.fullmatch(name) is not None and PLACEHOLDER not in name,
                 "settings_known_tasks_invalid")
    settings["known_tasks"] = list(known)

    expected = raw["expected"]
    _require(isinstance(expected, dict) and set(expected) == set(JOBS), "settings_expected_keys")
    _require(all(value in EXPECTED_VALUES for value in expected.values()), "settings_expected_value")
    settings["expected"] = dict(expected)

    thresholds = raw["thresholds"]
    _require(isinstance(thresholds, dict) and set(thresholds) <= set(THRESHOLDS), "settings_thresholds_keys")
    merged = {name: default for name, (default, _, _) in THRESHOLDS.items()}
    for name, value in thresholds.items():
        _, low, high = THRESHOLDS[name]
        _require(isinstance(value, int) and not isinstance(value, bool) and low <= value <= high,
                 "settings_threshold_invalid")
        merged[name] = value
    for warn, fail in WARN_FAIL_PAIRS:
        _require(merged[warn] < merged[fail], "settings_threshold_order")
    settings["thresholds"] = merged

    pauses = raw["pauses"]
    _require(isinstance(pauses, list) and len(pauses) <= 20, "settings_pauses_invalid")
    parsed_pauses = []
    for pause in pauses:
        _require(isinstance(pause, dict) and set(pause) == PAUSE_KEYS, "settings_pause_keys")
        _require(pause["job"] in JOBS, "settings_pause_job")
        _require(pause["reason_code"] in PAUSE_REASONS, "settings_pause_reason")
        until_text = pause["until"]
        _require(isinstance(until_text, str) and UNTIL_RE.fullmatch(until_text) is not None, "settings_pause_until")
        try:
            until = datetime.strptime(until_text, "%Y-%m-%dT%H:%M").replace(tzinfo=SGT)
        except ValueError:
            raise SettingsError("settings_pause_until") from None
        # A pause may reach at most 7 days ahead of this check (design section 3.4).
        _require(until <= now + timedelta(days=PAUSE_MAX_DAYS), "settings_pause_too_long")
        parsed_pauses.append({"job": pause["job"], "until": until, "reason_code": pause["reason_code"]})
    settings["pauses"] = parsed_pauses

    send = raw["send"]
    _require(isinstance(send, dict) and set(send) == SEND_KEYS, "settings_send_keys")
    _require(isinstance(send["enabled"], bool), "settings_send_enabled")
    _require(isinstance(send["webhook_url"], str) and WEBHOOK_RE.fullmatch(send["webhook_url"]) is not None
             and PLACEHOLDER not in send["webhook_url"], "settings_send_url_invalid")
    header = send["auth_header_name"]
    _require(isinstance(header, str) and HEADER_RE.fullmatch(header) is not None
             and header.lower() not in RESERVED_HEADERS, "settings_send_header_invalid")
    target = send["secret_credential_target"]
    _require(isinstance(target, str) and CREDENTIAL_TARGET_RE.fullmatch(target) is not None
             and PLACEHOLDER not in target, "settings_send_target_invalid")
    timeout = send["timeout_seconds"]
    _require(isinstance(timeout, int) and not isinstance(timeout, bool) and 5 <= timeout <= 10,
             "settings_send_timeout_invalid")
    settings["send"] = dict(send)
    return settings


def load_settings(path: str | Path, now: datetime) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        raise SettingsError("settings_unreadable") from None
    return parse_settings(raw, now)


# --------------------------------------------------------------------------------------------- #
# Reading (all read-only)
# --------------------------------------------------------------------------------------------- #

def task_query_script(task_path: str) -> str:
    """Read only: every task directly in ``task_path``. No other cmdlet may appear (static test)."""

    if TASK_PATH_RE.fullmatch(task_path) is None:  # also refuses quotes, so nothing can be injected
        raise ValueError("task_path")
    return (
        "$ErrorActionPreference = 'Stop'; "
        "$c = [Globalization.CultureInfo]::InvariantCulture; "
        "$rows = @(Get-ScheduledTask -TaskPath '%s' | ForEach-Object { "
        "$i = $_ | Get-ScheduledTaskInfo; "
        "[pscustomobject]@{ "
        "name = [string]$_.TaskName; "
        "state = [string]$_.State; "
        "last_run = $(if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-ddTHH:mm', $c) } else { $null }); "
        "last_result = [int64]$i.LastTaskResult "
        "} }); "
        "ConvertTo-Json -InputObject $rows -Compress"
    ) % task_path


def run_powershell(script: str, timeout: int) -> tuple[int, str]:
    shell = shutil.which("powershell")
    if shell is None:
        return 1, ""
    done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                          text=True, timeout=timeout, shell=False, check=False)
    return done.returncode, done.stdout


def read_tasks(task_path: str, runner: Callable[[str, int], tuple[int, str]] = run_powershell) -> list[dict[str, Any]] | None:
    """The tasks in the folder, or None when Task Scheduler could not be read."""

    try:
        code, out = runner(task_query_script(task_path), TASK_QUERY_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if code != 0:
        return None
    try:
        rows = json.loads(out.strip() or "null")
    except ValueError:
        return None
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return None
    tasks = []
    for row in rows:
        if not (isinstance(row, dict) and isinstance(row.get("name"), str) and isinstance(row.get("state"), str)):
            return None
        result = row.get("last_result")
        tasks.append({"name": row["name"], "state": row["state"],
                      "last_run": row.get("last_run") if isinstance(row.get("last_run"), str) else None,
                      "last_result": result if isinstance(result, int) and not isinstance(result, bool) else None})
    return tasks


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def observe(settings: Mapping[str, Any], *, task_reader: Callable[[str], list[dict[str, Any]] | None] = read_tasks,
            json_reader: Callable[[Path], dict[str, Any] | None] = read_json_file,
            is_dir: Callable[[str], bool] = os.path.isdir) -> dict[str, Any]:
    stock, arap = Path(settings["stock_folder"]), Path(settings["arap_folder"])
    return {
        "tasks": task_reader(settings["task_path"]),
        "stock_status": json_reader(stock / STATUS_FILE),
        "stock_manifest": json_reader(stock / MANIFEST_FILE),
        "arap_status": json_reader(arap / STATUS_FILE),
        "arap_manifest": json_reader(arap / MANIFEST_FILE),
        "mirror_status": json_reader(Path(settings["mirror_log_dir"]) / STATUS_FILE),
        "drive_root_ok": bool(is_dir(settings["drive_root"])),
    }


def read_records(record_dir: str | Path, today: date,
                 json_reader: Callable[[Path], dict[str, Any] | None] = read_json_file) -> dict[str, dict[str, Any]]:
    """This checker's own records of the last 14 days before ``today``, by date."""

    records = {}
    for back in range(1, RECORD_LOOKBACK_DAYS + 1):
        day = (today - timedelta(days=back)).isoformat()
        record = json_reader(Path(record_dir) / ("health_%s.json" % day))
        if record and record.get("schema") == RECORD_SCHEMA and record.get("date") == day:
            records[day] = record
    return records


# --------------------------------------------------------------------------------------------- #
# Judging (pure)
# --------------------------------------------------------------------------------------------- #

def parse_time(value: Any) -> datetime | None:
    """ISO-8601 with an offset, or a naive local time (the server PC and the VM run on SGT)."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SGT)
    if parsed.year < 2000:  # Task Scheduler's "never ran" date
        return None
    return parsed


def age_minutes(then: datetime | None, now: datetime) -> int | None:
    if then is None:
        return None
    return min(AGE_MAX, max(0, int((now - then).total_seconds() // 60)))


def _job(job: str, state: str, verdict: str, reasons: list[str] | None = None, *, age: int | None = None,
         exit_code: int | None = None, count: int | None = None) -> dict[str, Any]:
    return {"job": job, "state": state, "verdict": verdict, "reasons": list(reasons or []), "since": None,
            "until": None, "pause_reason": None, "age_min": age, "exit": exit_code, "count": count}


def _judged(job: str, reasons: list[str], **figures: Any) -> dict[str, Any]:
    levels = REASONS[job]
    unique = list(dict.fromkeys(reasons))
    order = list(levels)
    unique.sort(key=lambda reason: (0 if levels[reason] == FAIL else 1, order.index(reason)))
    if not unique:
        return _job(job, "on", "OK", [], **figures)
    verdict = FAIL if levels[unique[0]] == FAIL else WARN
    return _job(job, "on", verdict, unique[:MAX_REASONS], **figures)


def judge_export(job: str, status: Mapping[str, Any] | None, manifest: Mapping[str, Any] | None,
                 now: datetime, limits: Mapping[str, int], prefix: str) -> dict[str, Any]:
    """The AC2 hourly stock export (prefix ``stock``) or the AR/AP aging export (prefix ``arap``)."""

    if status is None:
        return _judged(job, ["status_missing"])
    finished, started = parse_time(status.get("finished_at")), parse_time(status.get("started_at"))
    last = finished or started
    if last is None:
        return _judged(job, ["status_missing"])
    reasons = []
    age = age_minutes(last, now)
    if age > limits[prefix + "_fail_age"]:
        reasons.append("last_run_too_old")
    elif age > limits[prefix + "_warn_age"]:
        reasons.append("last_run_old")
    if status.get("status") != "success":
        manifest_time = parse_time((manifest or {}).get("finished_at"))
        if manifest_time is None:
            reasons.append("manifest_missing")
        elif age_minutes(manifest_time, now) > limits[prefix + "_manifest_fail_age"]:
            reasons.append("manifest_too_old")
        else:
            reasons.append("run_failed")
    if status.get("delivery_status") in DELIVERY_FAILURES:
        reasons.append("delivery_failed")
    if prefix == "stock":
        if status.get("daily_close_status") == "failed":
            reasons.append("daily_close_failed")
        if status.get("analysis_failure_class") not in (None, ""):
            reasons.append("analysis_failed")
        if finished is not None and started is not None:
            seconds = (finished - started).total_seconds()
            if seconds > limits["stock_fail_run_seconds"]:
                reasons.append("run_too_slow")
            elif seconds > limits["stock_warn_run_seconds"]:
                reasons.append("run_slow")
    return _judged(job, reasons, age=age)


def judge_alert_route(job: str, status: Mapping[str, Any] | None) -> dict[str, Any]:
    """An export's alert route (``stock_alert_route`` or ``arap_alert_route``), from ``alert_status`` in
    that export's status file. A missing file is the export's FAIL, not this job's."""

    reason = ALERT_REASONS.get((status or {}).get("alert_status"))
    return _judged(job, [reason] if reason else [])


def _find_task(tasks: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for task in tasks:
        if task["name"].casefold() == name.casefold():
            return task
    return None


def judge_task(job: str, name: str, tasks: list[dict[str, Any]] | None, now: datetime, warn_age: int,
               fail_age: int, mirror_status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if tasks is None:
        return _judged(job, ["task_query_failed"])
    task = _find_task(tasks, name)
    if task is None:
        return _judged(job, ["task_missing"])
    reasons = []
    if task["state"] == "Disabled":
        reasons.append("task_disabled")
    age = age_minutes(parse_time(task["last_run"]), now)
    if age is None or age > fail_age:
        reasons.append("last_run_too_old")
    elif age > warn_age:
        reasons.append("last_run_old")
    result = task["last_result"]
    exit_code = None
    if result is not None and result != 0 and result not in TASK_INFO_RESULTS:
        exit_code = result & EXIT_MAX
        if job == "energygrid_retention" and result == RETENTION_HELD:
            reasons.append("retention_held")
        elif job == "energygrid_retention" and result == RETENTION_REFUSED:
            reasons.append("retention_refused")
        elif job == "drive_mirror" and result in MIRROR_WARN_EXITS:
            reasons.append(MIRROR_WARN_EXITS[result])
        elif job == "drive_mirror" and result in MIRROR_FAIL_EXITS:
            reasons.append("mirror_failed")
        else:
            reasons.append("task_result_nonzero")
    if job == "drive_mirror" and (mirror_status or {}).get("status") == "running" and task["state"] != "Running":
        reasons.append("mirror_stuck_running")
    return _judged(job, reasons, age=age, exit_code=exit_code)


def judge_server_tasks(tasks: list[dict[str, Any]] | None, settings: Mapping[str, Any]) -> dict[str, Any]:
    if tasks is None:
        return _judged("server_tasks", ["task_query_failed"])
    known = {name.casefold() for name in list(settings["tasks"].values()) + settings["known_tasks"]}
    unknown = sum(1 for task in tasks if task["name"].casefold() not in known)
    return _judged("server_tasks", ["unknown_task"] if unknown else [], count=min(unknown, COUNT_MAX))


def judge_checker(records: Mapping[str, Mapping[str, Any]], today: date) -> dict[str, Any]:
    yesterday = records.get((today - timedelta(days=1)).isoformat())
    failed = bool(yesterday) and yesterday.get("send_status") == "failed"
    return _judged("checker", ["previous_send_failed"] if failed else [])


def _since(job: str, today: date, records: Mapping[str, Mapping[str, Any]]) -> str:
    """The first day of the current run of WARN/FAIL days for this job, from the checker's own
    records (consecutive days only; a missing record ends the run)."""

    since = today
    for back in range(1, RECORD_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=back)
        record = records.get(day.isoformat())
        entries = ((record or {}).get("event") or {}).get("jobs") or []
        entry = next((e for e in entries if isinstance(e, dict) and e.get("job") == job), None)
        if not entry or entry.get("verdict") not in (WARN, FAIL):
            break
        since = day
    return since.isoformat()


def judge(settings: Mapping[str, Any], obs: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]],
          now: datetime) -> dict[str, Any]:
    """Build the closed ``xb.daily_health.v1`` event from the observations. Pure."""

    now = now.astimezone(SGT)
    today = now.date()
    limits = settings["thresholds"]
    tasks = obs["tasks"]
    names = settings["tasks"]
    judged = {
        "ac2_stock_export": judge_export("ac2_stock_export", obs["stock_status"], obs["stock_manifest"], now,
                                         limits, "stock"),
        "stock_alert_route": judge_alert_route("stock_alert_route", obs["stock_status"]),
        "arap_export": judge_export("arap_export", obs["arap_status"], obs["arap_manifest"], now, limits, "arap"),
        "arap_alert_route": judge_alert_route("arap_alert_route", obs["arap_status"]),
        "energygrid_daily_bills": judge_task("energygrid_daily_bills", names["energygrid_daily_bills"], tasks, now,
                                             limits["energygrid_bills_warn_age"], limits["energygrid_bills_fail_age"]),
        "energygrid_retention": judge_task("energygrid_retention", names["energygrid_retention"], tasks, now,
                                           limits["energygrid_retention_warn_age"],
                                           limits["energygrid_retention_fail_age"]),
        "drive_mirror": judge_task("drive_mirror", names["drive_mirror"], tasks, now, limits["mirror_warn_age"],
                                   limits["mirror_fail_age"], obs["mirror_status"]),
        "server_tasks": judge_server_tasks(tasks, settings),
        "checker": judge_checker(records, today),
        "drive_folder": _judged("drive_folder", [] if obs["drive_root_ok"] else ["folder_missing"]),
    }
    jobs = []
    for job in JOBS:
        expected = settings["expected"][job]
        pauses = [p for p in settings["pauses"] if p["job"] == job]
        active = [p for p in pauses if p["until"] > now]
        if expected != "on":
            entry = _job(job, expected, "NA")
        elif active:
            pause = max(active, key=lambda p: p["until"])
            entry = _job(job, "paused", "NA")
            entry["until"] = pause["until"].strftime("%Y-%m-%dT%H:%M")
            entry["pause_reason"] = pause["reason_code"]
        elif job in N8N_JOBS:
            entry = _job(job, "on", "N8N")
        else:
            entry = judged[job]
            expired = [p for p in pauses if now - p["until"] <= timedelta(days=EXPIRED_PAUSE_LOOKBACK_DAYS)]
            if expired and entry["verdict"] != "OK":
                entry = _judged(job, ["pause_expired"] + entry["reasons"], age=entry["age_min"],
                                exit_code=entry["exit"], count=entry["count"])
            if entry["verdict"] != "OK":
                entry["since"] = _since(job, today, records)
        jobs.append(entry)

    notes = []
    by_job = {entry["job"]: entry for entry in jobs}
    stock, arap = by_job["ac2_stock_export"], by_job["arap_export"]
    if stock["state"] == "on" and arap["state"] == "on" and stock["age_min"] is not None \
            and arap["age_min"] is not None and min(stock["age_min"], arap["age_min"]) > limits["stock_fail_age"] \
            and abs(stock["age_min"] - arap["age_min"]) <= limits["vm_down_similar_age"]:
        notes.append("vm_or_drive_down")
    return {"schema": EVENT_SCHEMA, "date": today.isoformat(), "checked_at": now.strftime("%Y-%m-%dT%H:%M+08:00"),
            "jobs": jobs, "notes": notes}


def worst(event: Mapping[str, Any]) -> str:
    verdicts = {entry["verdict"] for entry in event["jobs"]}
    return FAIL if FAIL in verdicts else (WARN if WARN in verdicts else "OK")


# --------------------------------------------------------------------------------------------- #
# Sending and recording
# --------------------------------------------------------------------------------------------- #

class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - urllib hook
        return None


def post_event(url: str, headers: Mapping[str, str], body: bytes, timeout: int) -> int:
    """POST once: no proxy, no redirect, no retry. Returns the HTTP status; raises OSError when
    there is no answer. The response body is never read."""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirect())
    request = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    with response:
        return response.status


class CredentialUnavailable(Exception):
    pass


def read_credential_secret(target: str) -> str:
    """The generic credential's secret from Windows Credential Manager (CredReadW). Only the
    value is returned; it is never printed, logged or written."""

    if os.name != "nt":
        raise CredentialUnavailable()
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not cred_read(target, 1, 0, ctypes.byref(pointer)):  # 1: CRED_TYPE_GENERIC
        raise CredentialUnavailable()
    try:
        blob = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
    finally:
        cred_free(pointer)
    try:
        secret = blob.decode("utf-16-le")
    except UnicodeDecodeError:
        raise CredentialUnavailable() from None
    if not secret:
        raise CredentialUnavailable()
    return secret


def send_event(event: Mapping[str, Any], send: Mapping[str, Any], *, cred_reader: Callable[[str], str],
               poster: Callable[[str, Mapping[str, str], bytes, int], int]) -> tuple[str, int | None, str | None]:
    """(send_status, http_status, send_error). Never raises."""

    try:
        secret = cred_reader(send["secret_credential_target"])
    except CredentialUnavailable:
        return "failed", None, "secret_unavailable"
    except Exception:  # noqa: BLE001 - any credential failure is one closed code
        return "failed", None, "secret_unavailable"
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, send["auth_header_name"]: secret}
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("ascii")
    try:
        status = poster(send["webhook_url"], headers, body, send["timeout_seconds"])
    except OSError:
        return "failed", None, "no_answer"
    except Exception:  # noqa: BLE001 - never fail the task over the POST
        return "failed", None, "post_error"
    if 200 <= status < 300:
        return "posted", status, None
    return "failed", status, "http_status"


def write_record(record_dir: str | Path, record: Mapping[str, Any]) -> Path:
    folder = Path(record_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("health_%s.json" % record["date"])
    handle, temp = tempfile.mkstemp(prefix=".health_", suffix=".tmp", dir=str(folder))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return target


def run(settings: Mapping[str, Any], now: datetime, *, no_send: bool, observer: Callable[..., dict[str, Any]] = observe,
        record_reader: Callable[..., dict[str, dict[str, Any]]] = read_records,
        cred_reader: Callable[[str], str] = read_credential_secret,
        poster: Callable[[str, Mapping[str, str], bytes, int], int] = post_event) -> dict[str, Any]:
    now = now.astimezone(SGT)
    records = record_reader(settings["record_dir"], now.date())
    event = judge(settings, observer(settings), records, now)
    if no_send:
        send_status, http_status, send_error = "not_sent", None, None
    elif not settings["send"]["enabled"]:
        send_status, http_status, send_error = "disabled", None, None
    else:
        send_status, http_status, send_error = send_event(event, settings["send"], cred_reader=cred_reader,
                                                          poster=poster)
    record = {"schema": RECORD_SCHEMA, "date": event["date"], "checked_at": event["checked_at"], "event": event,
              "send_status": send_status, "http_status": http_status, "send_error": send_error}
    write_record(settings["record_dir"], record)
    return record


def summary_line(record: Mapping[str, Any]) -> str:
    event = record["event"]
    return json.dumps({"date": event["date"], "worst": worst(event), "send_status": record["send_status"],
                       "jobs": {entry["job"]: entry["verdict"] for entry in event["jobs"]},
                       "notes": event["notes"]}, separators=(",", ":"))


def main(argv: list[str] | None = None, *, now: datetime | None = None, **hooks: Any) -> int:
    parser = argparse.ArgumentParser(description="Read-only daily health checker (#325).")
    parser.add_argument("command", choices=("check-settings", "run"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--no-send", action="store_true", help="judge and write the record, but do not POST")
    args = parser.parse_args(argv)
    now = (now or datetime.now(SGT)).astimezone(SGT)
    try:
        settings = load_settings(args.config, now)
    except SettingsError as exc:
        print(json.dumps({"ok": False, "code": exc.code}))
        return EXIT_SETTINGS
    if args.command == "check-settings":
        print(json.dumps({"ok": True}))
        return EXIT_OK
    try:
        record = run(settings, now, no_send=args.no_send, **hooks)
    except OSError:
        # The record folder could not be written. Nothing else is at stake; the n8n dead-man
        # reports a missing check at 08:15.
        print(json.dumps({"ok": False, "code": "record_write_failed"}))
        return EXIT_OK
    except Exception:  # noqa: BLE001 - fail safe: a bug never fails the task or prints a traceback
        # Anything unexpected (for example odd PowerShell output) still exits 0 with one closed code
        # and no detail, so no path or message reaches the task log. The dead-man reports the missing
        # check at 08:15.
        print(json.dumps({"ok": False, "code": "internal_error"}))
        return EXIT_OK
    print(summary_line(record))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
