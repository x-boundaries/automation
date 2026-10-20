"""EnergyGrid run-evidence retention (#257): prune proven-clean old runs, archive incidents, rotate the log.

Installed as  <EG_HOME>\\maintenance\\energygrid_retention.py  and started only by the admin-protected
<EG_HOME>\\maintenance\\egretention.ps1 (which checks the ACLs first). Standard library only.

    python -I energygrid_retention.py            DRY RUN (the default): classify every run, write a
                                                 private proposed-effects report, change nothing else
    python -I energygrid_retention.py --apply    archive incidents, prune eligible runs, rotate the log

There is no path argument. EG_HOME is the folder above this script's own folder, so the tool can only
ever act on <EG_HOME>\\data\\runs, <EG_HOME>\\data\\egdaily.log and its own <EG_HOME>\\data\\retention.

Safety floor (docs/run_log_retention.md):
- A run folder is pruned only when it is a direct, exact-pattern child of the runs root, more than
  RETENTION_COMPLETE_DAYS complete SGT days old, and its five evidence files independently prove a clean
  run: Core acquire and status for both streams, the daily record (SUBMIT, nothing withheld, no
  uncertain or held email, only clean steps) and an accepted monitor report and POST. Exit 0 alone is
  never enough. Anything missing, malformed, contradictory, failed, pending or uncertain is PRESERVED.
- Preserved incident runs are copied (never moved) into the private incident archive with a SHA-256
  manifest and read back before any prune in the same run.
- Symlinks, junctions, other reparse points, hard links and unexpected file types are never followed,
  archived or deleted. The tree is re-checked before every destructive step.
- A prune is journalled, renamed out of the runs root in one step, re-verified against the journal and
  only then deleted, so an interrupted run resumes or stops safely on the next invocation.
- egdaily.log is rotated by one rename (refused by Windows while the daily runner holds it open), never
  truncated. Only rotated copies are compacted, and only old clean lines on non-incident days are dropped.
- Only one instance runs (OS byte-range lock). --apply runs only inside APPLY_WINDOW and never while a
  run folder younger than ACTIVE_FENCE exists (the daily writer may still be active).

Output: one JSON summary line with counts and closed EG_RET_ references only; no file names, bill
numbers, addresses or tokens. The private report under data\\retention\\reports holds per-run detail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SUMMARY_SCHEMA = "energygrid.retention.summary.v1"
REPORT_SCHEMA = "energygrid.retention.report.v1"
ARCHIVE_SCHEMA = "energygrid.retention.incident_archive.v1"
JOURNAL_SCHEMA = "energygrid.retention.prune.v1"

SGT = timezone(timedelta(hours=8), "SGT")  # Singapore has no daylight saving
RETENTION_COMPLETE_DAYS = 90       # prune only when MORE than this many complete SGT days have passed
SETTLE = timedelta(hours=24)       # younger runs are never judged, archived or touched
ACTIVE_FENCE = timedelta(hours=4)  # --apply refuses while a run this young exists (PT2H task limit + margin)
CLOCK_SKEW = timedelta(minutes=2)
DAILY_SPAN = timedelta(hours=2, minutes=10)  # the daily task's hard PT2H limit, plus margin
RUN_SPAN = timedelta(hours=8)      # every evidence file of a run is written within this of its start
APPLY_WINDOW = (dtime(15, 0), dtime(23, 0))  # SGT: after the 14:00 monitor check, well before 11:30
LOG_ROTATE_DAYS = 31
LOG_ROTATE_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_FILES = 16
EXIT_OK, EXIT_HOLD, EXIT_REFUSED = 0, 20, 64

RUN_DIR_RE = re.compile(r"\d{8}-\d{6}", re.ASCII)
RUN_FILES = ("acquire.json", "status.json", "daily.json", "monitor-report.json", "monitor-post.json")
ARCHIVE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
STAGING_RE = re.compile(r"\.staging-\d{8}-\d{6}-[0-9a-f]{8}", re.ASCII)
SEGMENT_RE = re.compile(r"egdaily\.\d{8}-\d{6}\.log", re.ASCII)
COMPACT_TMP_RE = re.compile(r"\.egdaily\.\d{8}-\d{6}\.log\.compact-[0-9a-f]{8}\.tmp", re.ASCII)
RUN_ID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.ASCII)
ISO_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
LOG_LINE_RE = re.compile(rb"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2} (.*)", re.DOTALL)
LOG_CLEAN_TAIL = b"exit=0 monitor_report=SUBMIT submitted=True withheld="
BOM = b"\xef\xbb\xbf"

CORE_SCHEMA = "energygrid.native.v4.result.v1"
DAILY_SCHEMA = "energygrid.native_daily.run.v1"
MONITOR_SCHEMA = "xb.energygrid.monitor.v1"
MONITOR_ACCEPTED = {"outcome": "EG_MONITOR_LOCAL_REPORT_POST_ACCEPTED"}
STREAMS = ("EB_BILL", "TENANT_BILL")
GOOD_ACQUISITION = frozenset(("ADMITTED", "ALREADY_ADMITTED", "PREDECESSOR_DELIVERED", "EMPTY"))
CLEAN_STEPS = frozenset({("drive-seal", "SEALED"), ("drive-record", "VERIFIED"), ("drive-adopt", "ADOPTED"),
                         ("notify", "SENT")})
NOTIFICATION_STATES = ("PENDING", "SEALED", "SENT", "UNKNOWN", "HOLD")

SUCCESS, INCIDENT, INCOMPLETE, UNSAFE, FUTURE, SETTLING = (
    "SUCCESS", "INCIDENT", "INCOMPLETE", "UNSAFE", "FUTURE", "SETTLING")
PRESERVED_KINDS = frozenset((INCIDENT, INCOMPLETE, UNSAFE, FUTURE))
ARCHIVABLE_KINDS = frozenset((INCIDENT, INCOMPLETE))

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class RetentionError(Exception):
    """A closed EG_RET_ reference; never carries a path or a private value."""

    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref


class _Hold(Exception):
    def __init__(self, kind: str, ref: str) -> None:
        super().__init__(ref)
        self.kind, self.ref = kind, ref


def _fault(point: str) -> None:
    """Test seam: tests replace this to simulate a crash at a named point. Does nothing in production."""


# -- filesystem primitives --------------------------------------------------------------------------


def _is_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _plain_dir(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode) and not _is_reparse(st)


def _plain_file(st: os.stat_result) -> bool:
    # A hard link would make "delete" leave the bytes elsewhere and "archive" copy shared bytes: refuse.
    return stat.S_ISREG(st.st_mode) and not _is_reparse(st) and st.st_nlink == 1


def _check_components(path: Path) -> None:
    """Every component of `path` exists and is a plain directory: no symlink, junction or reparse point."""
    lexical = Path(os.path.abspath(path))
    current = Path(lexical.parts[0])
    for part in lexical.parts[1:]:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise RetentionError("EG_RET_TREE_MISSING") from None
        except OSError:
            raise RetentionError("EG_RET_TREE_UNREADABLE") from None
        if not _plain_dir(st):
            raise RetentionError("EG_RET_TREE_REFUSED")


def _read_plain(path: Path, limit: int) -> bytes:
    """Bytes of a plain regular file, refusing a swap between the check and the open."""
    before = os.lstat(path)
    if not _plain_file(before):
        raise RetentionError("EG_RET_FILE_NOT_PLAIN")
    if before.st_size > limit:
        raise RetentionError("EG_RET_FILE_TOO_LARGE")
    with open(path, "rb") as handle:
        opened = os.fstat(handle.fileno())
        data = handle.read(limit + 1)
    if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev) or len(data) > limit:
        raise RetentionError("EG_RET_FILE_CHANGED")
    return data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_new(path: Path, data: bytes) -> None:
    """Create a new file (never overwrite) and make it durable."""
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _json_bytes(document: Any) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _parse_json(data: bytes) -> Any:
    """Strict UTF-8 (no BOM), no duplicate keys, no NaN/Infinity."""
    return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)


def _iso_z(value: Any) -> datetime | None:
    if type(value) is not str or not ISO_Z_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def complete_days(run_day: date, today: date) -> int:
    """Whole SGT calendar days that have fully passed between the run's day and today (both excluded)."""
    return (today - run_day).days - 1


# -- layout and lock --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    home: Path

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def runs(self) -> Path:
        return self.data / "runs"

    @property
    def log(self) -> Path:
        return self.data / "egdaily.log"

    @property
    def retention(self) -> Path:
        return self.data / "retention"

    @property
    def archive(self) -> Path:
        return self.retention / "incident-archive"

    @property
    def quarantine(self) -> Path:
        return self.retention / "prune-quarantine"

    @property
    def journal(self) -> Path:
        return self.retention / "prune-journal"

    @property
    def ledger(self) -> Path:
        return self.retention / "pruned"

    @property
    def segments(self) -> Path:
        return self.retention / "egdaily-log"

    @property
    def reports(self) -> Path:
        return self.retention / "reports"

    def own_dirs(self) -> tuple[Path, ...]:
        return (self.retention, self.archive, self.quarantine, self.journal, self.ledger, self.segments,
                self.reports)


def installed_home() -> Path:
    """EG_HOME derived from this script's own location: <EG_HOME>\\maintenance\\energygrid_retention.py."""
    return Path(os.path.abspath(__file__)).parent.parent


def check_tree(layout: Layout) -> None:
    """The runs root and every ancestor are plain directories; the tool's own folders are created and checked."""
    if len(Path(os.path.abspath(layout.home)).parts) < 2:
        raise RetentionError("EG_RET_TREE_REFUSED")
    _check_components(layout.runs)
    if os.path.lexists(layout.log) and not _plain_file(os.lstat(layout.log)):
        raise RetentionError("EG_RET_LOG_NOT_PLAIN")
    for folder in layout.own_dirs():
        try:
            os.mkdir(folder)
        except FileExistsError:
            pass
        _check_components(folder)


class Lock:
    """One instance at a time: an OS byte-range lock the kernel releases when the process ends."""

    def __init__(self, path: Path) -> None:
        self.path, self.fd = path, None

    def __enter__(self) -> "Lock":
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RetentionError("EG_RET_LOCK_HELD") from None
        self.fd = fd
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.fd is None:
            return
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


# -- classification ---------------------------------------------------------------------------------


@dataclass
class Run:
    name: str
    kind: str
    reason: str | None
    started: datetime | None = None
    complete_days: int | None = None
    files: dict[str, str] = field(default_factory=dict)   # plain evidence file -> sha256
    eligible: bool = False
    action: str = "NONE"


def _need(condition: bool, ref: str, kind: str = INCOMPLETE) -> None:
    if not condition:
        raise _Hold(kind, ref)


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _judge_daily(daily: Any, started: datetime) -> tuple[str, datetime]:
    _need(type(daily) is dict and daily.get("schema") == DAILY_SCHEMA, "EG_RET_DAILY_SCHEMA")
    reasons = daily.get("withheld_reasons")
    _need(type(reasons) is list, "EG_RET_DAILY_SCHEMA")
    _need(not reasons, "EG_RET_DAILY_WITHHELD", INCIDENT)
    _need(daily.get("monitor_report") == "SUBMIT", "EG_RET_DAILY_NOT_SUBMITTED", INCIDENT)
    _need(daily.get("acquire_outcome") == "ACQUIRED", "EG_RET_ACQUIRE_NOT_ACQUIRED", INCIDENT)
    _need(daily.get("status_outcome") == "DRAINED", "EG_RET_STATUS_NOT_DRAINED", INCIDENT)
    run_id = daily.get("run_id")
    _need(type(run_id) is str and bool(RUN_ID_RE.fullmatch(run_id)), "EG_RET_RUN_ID_INVALID")
    began, ended = _iso_z(daily.get("started_at")), _iso_z(daily.get("finished_at"))
    _need(began is not None and ended is not None, "EG_RET_DAILY_TIME_INVALID")
    # The folder name is the SGT wall clock at start; started_at is UTC from the same process.
    _need(abs(began - started) <= CLOCK_SKEW and began <= ended <= began + DAILY_SPAN,
          "EG_RET_DAILY_TIME_INCONSISTENT")
    email = daily.get("email")
    _need(type(email) is dict and set(email) == {"probe", "sent", "uncertain", "held"}
          and all(_count(email[key]) for key in ("sent", "uncertain", "held")), "EG_RET_DAILY_EMAIL_INVALID")
    _need(email["uncertain"] == 0 and email["held"] == 0, "EG_RET_EMAIL_UNCERTAIN_OR_HELD", INCIDENT)
    _need(email["probe"] in (None, "READY"), "EG_RET_EMAIL_PROBE_NOT_READY", INCIDENT)
    steps = daily.get("steps")
    _need(type(steps) is list and len(steps) >= 1 and all(type(item) is dict for item in steps),
          "EG_RET_DAILY_STEPS_INVALID")
    last = steps[-1]
    _need(last.get("step") == "next" and last.get("outcome") == "IDLE" and "support_ref" not in last,
          "EG_RET_DAILY_NOT_IDLE", INCIDENT)
    for item in steps[:-1]:
        _need((item.get("step"), item.get("outcome")) in CLEAN_STEPS and "support_ref" not in item,
              "EG_RET_DAILY_STEP_NOT_CLEAN", INCIDENT)
    sent = sum(1 for item in steps if item.get("step") == "notify")
    _need(sent == email["sent"], "EG_RET_EMAIL_COUNT_MISMATCH")
    return run_id, ended


def _judge_acquire(acquire: Any, run_id: str) -> None:
    _need(type(acquire) is dict and acquire.get("schema") == CORE_SCHEMA and acquire.get("command") == "acquire",
          "EG_RET_ACQUIRE_SCHEMA")
    _need(acquire.get("outcome") == "ACQUIRED" and acquire.get("exit_code") == 0, "EG_RET_ACQUIRE_NOT_ACQUIRED",
          INCIDENT)
    _need(acquire.get("run_id") == run_id, "EG_RET_RUN_ID_MISMATCH")
    _need(acquire.get("recovered_unknown") == 0 and acquire.get("recovered_notifications") == 0
          and type(acquire.get("recovered_unknown")) is int and type(acquire.get("recovered_notifications")) is int,
          "EG_RET_ACQUIRE_RECOVERED_WORK", INCIDENT)
    acquisition = acquire.get("acquisition")
    _need(type(acquisition) is dict and set(acquisition) == set(STREAMS), "EG_RET_ACQUISITION_STREAMS")
    for stream in STREAMS:
        entry = acquisition[stream]
        _need(type(entry) is dict and entry.get("status") in GOOD_ACQUISITION and entry.get("support_ref") is None,
              "EG_RET_ACQUISITION_NOT_CLEAN", INCIDENT)
    named = acquire.get("named_archive")
    _need(type(named) is dict and set(named) == set(STREAMS), "EG_RET_NAMED_ARCHIVE_STREAMS")
    for stream in STREAMS:
        _need(type(named[stream]) is list, "EG_RET_NAMED_ARCHIVE_STREAMS")
        for item in named[stream]:
            _need(type(item) is dict and item.get("named_archive") == "PRESENT" and item.get("support_ref") is None,
                  "EG_RET_NAMED_ARCHIVE_NOT_PRESENT", INCIDENT)


def _judge_status(status: Any) -> None:
    _need(type(status) is dict and status.get("schema") == CORE_SCHEMA and status.get("command") == "status",
          "EG_RET_STATUS_SCHEMA")
    _need(status.get("outcome") == "DRAINED" and status.get("exit_code") == 0, "EG_RET_STATUS_NOT_DRAINED", INCIDENT)
    streams = status.get("streams")
    _need(type(streams) is dict and set(streams) == set(STREAMS), "EG_RET_STATUS_STREAMS")
    for stream in STREAMS:
        row = streams[stream]
        _need(type(row) is dict and row.get("state") == "DRAINED" and row.get("support_ref") is None,
              "EG_RET_STREAM_NOT_DRAINED", INCIDENT)
        invoices = row.get("invoices")
        _need(type(invoices) is list, "EG_RET_STATUS_STREAMS")
        for invoice in invoices:
            drive = invoice.get("drive_upload") if type(invoice) is dict else None
            _need(type(drive) is dict and drive.get("state") == "VERIFIED"
                  and invoice.get("named_archive") == "PRESENT", "EG_RET_INVOICE_NOT_SETTLED", INCIDENT)
    notifications = status.get("notifications")
    _need(type(notifications) is dict, "EG_RET_NOTIFICATIONS_MISSING")
    counts, items = notifications.get("counts"), notifications.get("items")
    _need(type(counts) is dict and set(counts) == set(NOTIFICATION_STATES)
          and all(_count(value) for value in counts.values()) and type(items) is list, "EG_RET_NOTIFICATIONS_MISSING")
    _need(all(counts[state] == 0 for state in NOTIFICATION_STATES if state != "SENT")
          and all(type(item) is dict and item.get("state") == "SENT" for item in items),
          "EG_RET_NOTIFICATION_UNSETTLED", INCIDENT)


def _judge_monitor(report: Any, post: Any, run_id: str, started: datetime, ended: datetime) -> None:
    _need(post == MONITOR_ACCEPTED, "EG_RET_MONITOR_POST_NOT_ACCEPTED", INCIDENT)
    _need(type(report) is dict and report.get("schema") == MONITOR_SCHEMA, "EG_RET_MONITOR_REPORT_SCHEMA")
    _need(report.get("run_id") == run_id, "EG_RET_RUN_ID_MISMATCH")
    observed = _iso_z(report.get("observed_at_utc"))
    _need(observed is not None and ended - CLOCK_SKEW <= observed <= started + RUN_SPAN,
          "EG_RET_MONITOR_TIME_INCONSISTENT")
    streams = report.get("streams")
    _need(type(streams) is dict and set(streams) == set(STREAMS), "EG_RET_MONITOR_REPORT_SCHEMA")
    for stream in STREAMS:
        _need(streams[stream] == {"result": "OK", "support_ref": None, "incident_ref": None},
              "EG_RET_MONITOR_NOT_OK", INCIDENT)


def _judge(folder: Path, started: datetime, present: dict[str, bytes], mtimes: dict[str, float]) -> None:
    """Raise _Hold unless the evidence independently proves one clean, completed, reported run."""
    unexpected = set(present) - set(RUN_FILES)
    _need(not unexpected, "EG_RET_RUN_UNEXPECTED_FILE")
    for name in ("acquire.json", "status.json", "daily.json"):
        _need(name in present, "EG_RET_EVIDENCE_MISSING")
    parsed: dict[str, Any] = {}
    for name, data in present.items():
        try:
            parsed[name] = _parse_json(data)
        except (UnicodeError, ValueError, RecursionError):
            raise _Hold(INCOMPLETE, "EG_RET_EVIDENCE_MALFORMED") from None
    run_id, ended = _judge_daily(parsed["daily.json"], started)
    _judge_acquire(parsed["acquire.json"], run_id)
    _judge_status(parsed["status.json"])
    # The daily said SUBMIT, so a missing monitor report means the reporter failed: an incident.
    _need("monitor-report.json" in present and "monitor-post.json" in present, "EG_RET_MONITOR_MISSING", INCIDENT)
    _judge_monitor(parsed["monitor-report.json"], parsed["monitor-post.json"], run_id, started, ended)
    for name, mtime in mtimes.items():
        written = datetime.fromtimestamp(mtime, tz=timezone.utc)
        _need(started - CLOCK_SKEW <= written <= started + RUN_SPAN, "EG_RET_EVIDENCE_TIME_INCONSISTENT")


def classify(layout: Layout, name: str, now: datetime) -> Run:
    """Read-only classification of one runs-root entry whose name has the run-folder pattern."""
    folder = layout.runs / name
    # Type first: a link or junction is never parsed, read or archived, whatever its name says.
    try:
        st = os.lstat(folder)
    except OSError:
        return Run(name, UNSAFE, "EG_RET_RUN_DIR_UNREADABLE")
    if not _plain_dir(st):
        return Run(name, UNSAFE, "EG_RET_RUN_DIR_NOT_PLAIN")
    try:
        started = datetime.strptime(name, "%Y%m%d-%H%M%S").replace(tzinfo=SGT)
    except ValueError:
        return Run(name, INCOMPLETE, "EG_RET_RUN_NAME_INVALID")
    run = Run(name, INCOMPLETE, None, started, complete_days(started.date(), now.astimezone(SGT).date()))
    if started > now + CLOCK_SKEW:
        run.kind, run.reason = FUTURE, "EG_RET_RUN_IN_FUTURE"
        return run
    if now - started < SETTLE:
        run.kind, run.reason = SETTLING, "EG_RET_RUN_SETTLING"  # may still be written: not even read
        return run
    present: dict[str, bytes] = {}
    mtimes: dict[str, float] = {}
    try:
        with os.scandir(folder) as entries:
            names = sorted(entry.name for entry in entries)
        for entry_name in names:
            path = folder / entry_name
            entry_st = os.lstat(path)
            if not _plain_file(entry_st):
                run.kind, run.reason = UNSAFE, "EG_RET_RUN_CONTENT_NOT_PLAIN"
                run.files = {}
                return run
            if entry_st.st_size > MAX_EVIDENCE_BYTES:
                run.reason = "EG_RET_EVIDENCE_TOO_LARGE"
                continue
            data = _read_plain(path, MAX_EVIDENCE_BYTES)
            present[entry_name], mtimes[entry_name] = data, entry_st.st_mtime
            run.files[entry_name] = _sha256(data)
    except (OSError, RetentionError):
        run.kind, run.reason, run.files = UNSAFE, "EG_RET_RUN_CONTENT_UNREADABLE", {}
        return run
    if run.reason is not None:
        return run  # an oversized file: preserved, archived without it
    try:
        _judge(folder, started, present, mtimes)
    except _Hold as hold:
        run.kind, run.reason = hold.kind, hold.ref
        return run
    run.kind, run.reason = SUCCESS, None
    return run


def scan(layout: Layout, now: datetime) -> tuple[list[Run], int]:
    """Classify every run-pattern entry of the runs root; anything else is counted as skipped, never touched."""
    runs: list[Run] = []
    skipped = 0
    with os.scandir(layout.runs) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        if RUN_DIR_RE.fullmatch(name):
            runs.append(classify(layout, name, now))
        else:
            skipped += 1
    incident_days = {run.started.date() for run in runs if run.kind in PRESERVED_KINDS and run.started}
    for run in runs:
        if run.kind != SUCCESS:
            continue
        if run.started.date() in incident_days:
            run.reason = "EG_RET_SAME_DAY_INCIDENT"
        elif run.complete_days is not None and run.complete_days > RETENTION_COMPLETE_DAYS:
            run.eligible = True
        else:
            run.reason = "EG_RET_WITHIN_RETENTION"
    return runs, skipped


# -- incident archive -------------------------------------------------------------------------------


def verify_archive(folder: Path) -> dict | None:
    """Consumer readback: the archive manifest and every archived file match exactly. None if not."""
    try:
        _check_components(folder)
        manifest = _parse_json(_read_plain(folder / "manifest.json", MAX_EVIDENCE_BYTES))
        if type(manifest) is not dict or manifest.get("schema") != ARCHIVE_SCHEMA:
            return None
        files = manifest.get("files")
        if type(files) is not list:
            return None
        expected = {"manifest.json"}
        for item in files:
            name = item.get("name") if type(item) is dict else None
            if type(name) is not str or not ARCHIVE_NAME_RE.fullmatch(name) or name == "manifest.json":
                return None
            data = _read_plain(folder / name, MAX_EVIDENCE_BYTES)
            if _sha256(data) != item.get("sha256") or len(data) != item.get("bytes"):
                return None
            expected.add(name)
        with os.scandir(folder) as entries:
            if {entry.name for entry in entries} != expected:
                return None
        return manifest
    except (OSError, RetentionError, UnicodeError, ValueError):
        return None


def _archive_sources(layout: Layout, run: Run) -> tuple[list[tuple[str, bytes]], list[dict]]:
    chosen: list[tuple[str, bytes]] = []
    excluded: list[dict] = []
    folder = layout.runs / run.name
    _check_components(folder)  # re-checked at copy time: never list through a swapped-in link
    with os.scandir(folder) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        path = folder / name
        st = os.lstat(path)
        if not ARCHIVE_NAME_RE.fullmatch(name) or name == "manifest.json":
            excluded.append({"name": name, "reason": "EG_RET_ARCHIVE_NAME_REFUSED"})
        elif not _plain_file(st):
            excluded.append({"name": name, "reason": "EG_RET_ARCHIVE_NOT_PLAIN"})
        elif st.st_size > MAX_EVIDENCE_BYTES:
            excluded.append({"name": name, "reason": "EG_RET_ARCHIVE_TOO_LARGE"})
        elif len(chosen) >= MAX_ARCHIVE_FILES:
            excluded.append({"name": name, "reason": "EG_RET_ARCHIVE_FILE_LIMIT"})
        else:
            chosen.append((name, _read_plain(path, MAX_EVIDENCE_BYTES)))
    return chosen, excluded


def _remove_tree_of_plain_files(folder: Path) -> None:
    """Delete a folder holding only plain files (the tool's own staging or quarantine). Refuses anything else."""
    st = os.lstat(folder)
    if not _plain_dir(st):
        raise RetentionError("EG_RET_STAGING_NOT_PLAIN")
    with os.scandir(folder) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        if not _plain_file(os.lstat(folder / name)):
            raise RetentionError("EG_RET_STAGING_NOT_PLAIN")
    for name in names:
        os.unlink(folder / name)
    os.rmdir(folder)


def archive_incident(layout: Layout, run: Run, now: datetime) -> str:
    """Copy a preserved run's evidence into the private archive and read it back. The original stays.

    Returns ARCHIVED (new, verified) or ARCHIVE_VERIFIED (already archived and still matching)."""
    target = layout.archive / run.name
    if os.path.lexists(target):
        manifest = verify_archive(target)
        if manifest is None:
            raise RetentionError("EG_RET_ARCHIVE_EXISTING_INVALID")
        current = {name: _sha256(data) for name, data in _archive_sources(layout, run)[0]}
        archived = {item["name"]: item["sha256"] for item in manifest["files"]}
        if current != archived:
            raise RetentionError("EG_RET_ARCHIVE_SOURCE_CHANGED")
        return "ARCHIVE_VERIFIED"
    chosen, excluded = _archive_sources(layout, run)
    staging = layout.archive / f".staging-{run.name}-{secrets.token_hex(4)}"
    os.mkdir(staging)
    files = []
    for name, data in chosen:
        _write_new(staging / name, data)
        if _sha256(_read_plain(staging / name, MAX_EVIDENCE_BYTES)) != _sha256(data):
            raise RetentionError("EG_RET_ARCHIVE_DIGEST_MISMATCH")
        files.append({"name": name, "bytes": len(data), "sha256": _sha256(data)})
    _fault("archive:before-manifest")
    manifest = {"schema": ARCHIVE_SCHEMA, "run": run.name, "kind": run.kind, "reason": run.reason,
                "archived_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": files, "excluded": excluded, "complete": not excluded}
    _write_new(staging / "manifest.json", _json_bytes(manifest))
    # The source must still hold exactly what was copied (no change during the copy).
    if {name: _sha256(data) for name, data in _archive_sources(layout, run)[0]} != {
            item["name"]: item["sha256"] for item in files}:
        raise RetentionError("EG_RET_ARCHIVE_SOURCE_CHANGED")
    _fault("archive:before-publish")
    os.rename(staging, target)  # one step: the archive either exists complete or not at all
    if verify_archive(target) is None:
        raise RetentionError("EG_RET_ARCHIVE_READBACK_FAILED")
    return "ARCHIVED"


def clean_archive_staging(layout: Layout) -> int:
    """Remove the tool's own interrupted staging copies (the originals were never touched)."""
    removed = 0
    with os.scandir(layout.archive) as entries:
        names = [entry.name for entry in entries if STAGING_RE.fullmatch(entry.name)]
    for name in names:
        _remove_tree_of_plain_files(layout.archive / name)
        removed += 1
    return removed


# -- prune ------------------------------------------------------------------------------------------


def _folder_digest(folder: Path) -> dict[str, str]:
    with os.scandir(folder) as entries:
        names = sorted(entry.name for entry in entries)
    return {name: _sha256(_read_plain(folder / name, MAX_EVIDENCE_BYTES)) for name in names}


def _finish_prune(layout: Layout, name: str, journal: dict, now: datetime) -> None:
    """Delete the quarantined copy only if every remaining file matches the journal, then write the ledger."""
    held = layout.quarantine / name
    state = "PRUNED"
    if not os.path.lexists(held):
        state = "PRUNED_QUARANTINE_ALREADY_EMPTY"  # resumed after the last delete; nothing left to verify
    else:
        _check_components(held)
        expected = journal["files"]
        remaining = _folder_digest(held)
        if any(expected.get(file_name) != digest for file_name, digest in remaining.items()):
            raise RetentionError("EG_RET_PRUNE_DIGEST_MISMATCH")
        for file_name in sorted(remaining):
            _check_components(held)
            if not _plain_file(os.lstat(held / file_name)):
                raise RetentionError("EG_RET_PRUNE_NOT_PLAIN")
            os.unlink(held / file_name)
            _fault("prune:mid-delete")
        os.rmdir(held)
    record = dict(journal, state=state, pruned_at=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    ledger = layout.ledger / f"{name}.json"
    if not os.path.lexists(ledger):
        _write_new(ledger, _json_bytes(record))
    os.unlink(layout.journal / f"{name}.json")


def prune(layout: Layout, run: Run, now: datetime) -> None:
    """Journal, rename out of the runs root in one step, re-verify, delete, record."""
    source, held = layout.runs / run.name, layout.quarantine / run.name
    if os.path.lexists(held) or os.path.lexists(layout.journal / f"{run.name}.json"):
        raise RetentionError("EG_RET_PRUNE_ALREADY_IN_PROGRESS")
    journal = {"schema": JOURNAL_SCHEMA, "run": run.name, "state": "PENDING", "files": run.files,
               "complete_days": run.complete_days}
    _write_new(layout.journal / f"{run.name}.json", _json_bytes(journal))
    _fault("prune:after-journal")
    _check_components(layout.runs)
    _check_components(layout.quarantine)
    before = os.lstat(source)
    if not _plain_dir(before):
        os.unlink(layout.journal / f"{run.name}.json")
        raise RetentionError("EG_RET_RUN_DIR_NOT_PLAIN")
    try:
        os.rename(source, held)  # Windows refuses while any evidence file is open: the run stays in place
    except OSError:
        os.unlink(layout.journal / f"{run.name}.json")
        raise RetentionError("EG_RET_RUN_IN_USE") from None
    after = os.lstat(held)
    if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev) or not _plain_dir(after):
        raise RetentionError("EG_RET_PRUNE_SUBSTITUTED")  # journal kept: the next run stops on it
    _fault("prune:after-rename")
    try:
        same = _folder_digest(held) == run.files
    except (OSError, RetentionError):
        same = False
    if not same:
        # Put the run back where it was, untouched, and forget the journal.
        os.rename(held, source)
        os.unlink(layout.journal / f"{run.name}.json")
        raise RetentionError("EG_RET_PRUNE_DIGEST_MISMATCH")
    _finish_prune(layout, run.name, journal, now)


def resume_prunes(layout: Layout, now: datetime) -> tuple[int, list[str]]:
    """Settle journals left by an interrupted run. Returns (completed, error refs)."""
    completed, errors = 0, []
    with os.scandir(layout.journal) as entries:
        names = sorted(entry.name for entry in entries)
    for file_name in names:
        name = file_name[:-5] if file_name.endswith(".json") else ""
        if not RUN_DIR_RE.fullmatch(name):
            errors.append("EG_RET_JOURNAL_UNEXPECTED")
            continue
        try:
            journal = _parse_json(_read_plain(layout.journal / file_name, MAX_EVIDENCE_BYTES))
            if type(journal) is not dict or journal.get("schema") != JOURNAL_SCHEMA or journal.get("run") != name \
                    or type(journal.get("files")) is not dict:
                raise RetentionError("EG_RET_JOURNAL_INVALID")
            in_runs = os.path.lexists(layout.runs / name)
            in_quarantine = os.path.lexists(layout.quarantine / name)
            if in_runs and in_quarantine:
                raise RetentionError("EG_RET_JOURNAL_CONFLICT")
            if in_runs:
                os.unlink(layout.journal / file_name)  # the rename never happened: the run is intact in place
                continue
            _finish_prune(layout, name, journal, now)
            completed += 1
        except (OSError, RetentionError, UnicodeError, ValueError) as exc:
            errors.append(getattr(exc, "ref", "EG_RET_JOURNAL_RESUME_FAILED"))
    with os.scandir(layout.quarantine) as entries:
        for entry in entries:
            if not os.path.lexists(layout.journal / f"{entry.name}.json"):
                errors.append("EG_RET_QUARANTINE_WITHOUT_JOURNAL")  # left untouched for the operator
    return completed, errors


# -- egdaily.log ------------------------------------------------------------------------------------


def _log_lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def _line_day(line: bytes, first: bool) -> tuple[date | None, bool]:
    """(SGT day, clean?) of one log line. Unparseable lines have no day and are never clean."""
    body = line[len(BOM):] if first and line.startswith(BOM) else line
    complete = body.endswith(b"\n")
    match = LOG_LINE_RE.fullmatch(body.rstrip(b"\r\n"))
    if not match:
        return None, False
    try:
        day = date.fromisoformat(match.group(1).decode("ascii"))
    except ValueError:
        return None, False
    return day, complete and match.group(2) == LOG_CLEAN_TAIL


def _segments(layout: Layout) -> list[str]:
    with os.scandir(layout.segments) as entries:
        return sorted(entry.name for entry in entries if SEGMENT_RE.fullmatch(entry.name))


def log_incident_days(layout: Layout) -> set[date]:
    """Days with any abnormal line, in the live log or in any rotated segment. Raises if any is unreadable,
    because an unread line could be an incident: the caller then compacts nothing."""
    days: set[date] = set()
    sources = [layout.log] if os.path.lexists(layout.log) else []
    sources += [layout.segments / name for name in _segments(layout)]
    for path in sources:
        lines = _log_lines(_read_plain(path, MAX_SEGMENT_BYTES))
        for index, line in enumerate(lines):
            day, clean = _line_day(line, index == 0)
            if day is not None and not clean:
                days.add(day)
    return days


def rotation_due(layout: Layout, today: date) -> bool:
    if not os.path.lexists(layout.log):
        return False
    st = os.lstat(layout.log)
    if st.st_size >= LOG_ROTATE_BYTES:
        return True
    lines = _log_lines(_read_plain(layout.log, MAX_SEGMENT_BYTES))
    for index, line in enumerate(lines):
        day, _clean = _line_day(line, index == 0)
        if day is not None:
            return complete_days(day, today) >= LOG_ROTATE_DAYS
    return False


def rotate_log(layout: Layout, now: datetime) -> str:
    """Move the live log aside in one rename. The daily runner's next Add-Content starts a new file."""
    target = layout.segments / f"egdaily.{now.astimezone(SGT):%Y%m%d-%H%M%S}.log"
    if os.path.lexists(target):
        raise RetentionError("EG_RET_LOG_SEGMENT_EXISTS")
    if not _plain_file(os.lstat(layout.log)):
        raise RetentionError("EG_RET_LOG_NOT_PLAIN")
    try:
        os.rename(layout.log, target)
    except PermissionError:
        return "LOG_IN_USE"  # an open writer handle: nothing changed, try again next time
    return "ROTATED"


def compact_plan(data: bytes, today: date, keep_days: set[date]) -> tuple[bytes, int]:
    """(kept bytes, dropped line count). Drops only complete clean lines older than retention on safe days."""
    kept: list[bytes] = []
    dropped = 0
    lines = _log_lines(data)
    for index, line in enumerate(lines):
        day, clean = _line_day(line, index == 0)
        if clean and day not in keep_days and complete_days(day, today) > RETENTION_COMPLETE_DAYS:
            dropped += 1
            continue
        kept.append(line)
    output = b"".join(kept)
    if dropped and data.startswith(BOM) and not output.startswith(BOM):
        output = BOM + output
    return output, dropped


def compact_segment(layout: Layout, name: str, today: date, keep_days: set[date]) -> int:
    """Rewrite one rotated segment atomically (temp file, readback, replace). Returns lines dropped."""
    path = layout.segments / name
    data = _read_plain(path, MAX_SEGMENT_BYTES)
    output, dropped = compact_plan(data, today, keep_days)
    if not dropped:
        return 0
    if output in (b"", BOM):
        _fault("log:before-unlink")
        if _sha256(_read_plain(path, MAX_SEGMENT_BYTES)) != _sha256(data):
            raise RetentionError("EG_RET_LOG_SEGMENT_CHANGED")
        os.unlink(path)
        return dropped
    temp = layout.segments / f".{name}.compact-{secrets.token_hex(4)}.tmp"
    _write_new(temp, output)
    if _read_plain(temp, MAX_SEGMENT_BYTES) != output:
        raise RetentionError("EG_RET_LOG_DIGEST_MISMATCH")
    _fault("log:before-replace")
    if _sha256(_read_plain(path, MAX_SEGMENT_BYTES)) != _sha256(data):
        os.unlink(temp)
        raise RetentionError("EG_RET_LOG_SEGMENT_CHANGED")
    os.replace(temp, path)  # atomic on one volume: the old or the new segment, never a mix
    return dropped


def clean_compact_temps(layout: Layout) -> int:
    removed = 0
    with os.scandir(layout.segments) as entries:
        names = [entry.name for entry in entries if COMPACT_TMP_RE.fullmatch(entry.name)]
    for name in names:
        if _plain_file(os.lstat(layout.segments / name)):
            os.unlink(layout.segments / name)
            removed += 1
    return removed


# -- the run ----------------------------------------------------------------------------------------


def in_window(moment: datetime) -> bool:
    local = moment.astimezone(SGT).time()
    return APPLY_WINDOW[0] <= local < APPLY_WINDOW[1]


def _counts() -> dict[str, int]:
    return {key: 0 for key in ("considered", "eligible", "pruned", "retained", "protected", "archived",
                               "archive_verified", "skipped", "errors", "log_rotated", "log_segments_compacted",
                               "log_lines_dropped", "resumed")}


def _inventory(layout: Layout, runs: list[Run]) -> dict[str, dict[str, str]]:
    result = {}
    for run in runs:
        if run.kind in (UNSAFE, FUTURE) or not run.files:
            continue
        try:
            result[run.name] = _folder_digest(layout.runs / run.name)
        except (OSError, RetentionError):
            result[run.name] = {}
    return result


def execute(layout: Layout, *, apply: bool, clock: Callable[[], datetime]) -> dict:
    """The whole maintenance pass under the instance lock. Returns the summary (and writes the report).

    Order: settle interrupted work, classify, archive every incident, then (only if all of that is clean)
    prune, rotate and compact. Any doubt on the way becomes a hold that skips every later effect."""
    now = clock().astimezone(SGT)
    mode = "APPLY" if apply else "DRY_RUN"
    counts = _counts()
    refs: list[str] = []
    holds: list[str] = []
    check_tree(layout)
    with Lock(layout.retention / "retention.lock"):
        if apply and not in_window(now):
            raise RetentionError("EG_RET_OUTSIDE_WINDOW")
        if apply:
            for cleaner in (clean_archive_staging, clean_compact_temps):
                try:
                    cleaner(layout)
                except (OSError, RetentionError) as exc:
                    holds.append(getattr(exc, "ref", "EG_RET_CLEANUP_FAILED"))
            completed, errors = resume_prunes(layout, now)
            counts["resumed"] = completed
            holds += errors
        runs, counts["skipped"] = scan(layout, now)
        counts["considered"] = len(runs)
        if any(run.kind == FUTURE for run in runs):
            holds.append("EG_RET_FUTURE_RUN_PRESENT")
        if any(run.started and run.kind != FUTURE and now - run.started < ACTIVE_FENCE for run in runs):
            holds.append("EG_RET_WRITER_MAY_BE_ACTIVE")
        before = _inventory(layout, runs)
        today = now.date()
        keep_days = {run.started.date() for run in runs if run.kind in PRESERVED_KINDS and run.started}
        log: dict[str, Any] = {"rotation": "NOT_DUE", "segments": {}}
        log_ok = True
        try:
            keep_days |= log_incident_days(layout)
        except (OSError, RetentionError) as exc:
            log_ok = False
            log["rotation"] = "LOG_UNREADABLE"
            refs.append(getattr(exc, "ref", "EG_RET_LOG_UNREADABLE"))

        # 1. Archive every preserved incident before any change is considered.
        for run in runs:
            if run.kind not in ARCHIVABLE_KINDS:
                continue
            if not apply:
                run.action = "ARCHIVED_BEFORE" if os.path.lexists(layout.archive / run.name) else "WOULD_ARCHIVE"
                continue
            if holds:
                run.action = "HELD"
                continue
            try:
                run.action = archive_incident(layout, run, now)
                counts["archived" if run.action == "ARCHIVED" else "archive_verified"] += 1
            except (OSError, RetentionError) as exc:
                run.action = "ARCHIVE_FAILED"
                refs.append(getattr(exc, "ref", "EG_RET_ARCHIVE_FAILED"))
                if "EG_RET_PRUNE_SKIPPED_ARCHIVE_INCOMPLETE" not in holds:
                    holds.append("EG_RET_PRUNE_SKIPPED_ARCHIVE_INCOMPLETE")

        # 2. Prune runs that are proven clean and old enough, re-proving each one just before it goes.
        pruned: set[str] = set()
        for run in runs:
            if not run.eligible:
                continue
            counts["eligible"] += 1
            if not apply:
                run.action = "WOULD_PRUNE"
                continue
            if holds:
                run.action = "HELD"
                continue
            if not in_window(clock()):
                run.action = "HELD"
                holds.append("EG_RET_WINDOW_CLOSED")
                continue
            fresh = classify(layout, run.name, clock().astimezone(SGT))
            if fresh.kind != SUCCESS or fresh.files != run.files:
                run.action = "CHANGED_SKIPPED"
                refs.append("EG_RET_RUN_CHANGED_BEFORE_PRUNE")
                continue
            try:
                prune(layout, run, now)
                run.action = "PRUNED"
                pruned.add(run.name)
                counts["pruned"] += 1
            except (OSError, RetentionError) as exc:
                run.action = "PRUNE_FAILED"
                refs.append(getattr(exc, "ref", "EG_RET_PRUNE_FAILED"))

        # 3. The summary log: rotate the live file by one rename, compact only rotated copies.
        if log_ok:
            try:
                if rotation_due(layout, today):
                    if not apply:
                        log["rotation"] = "WOULD_ROTATE"
                    elif holds:
                        log["rotation"] = "HELD"
                    elif not in_window(clock()):
                        log["rotation"] = "HELD"
                        holds.append("EG_RET_WINDOW_CLOSED")
                    else:
                        log["rotation"] = rotate_log(layout, now)
                        if log["rotation"] == "ROTATED":
                            counts["log_rotated"] = 1
                        else:
                            counts["skipped"] += 1
                for name in _segments(layout):
                    if not apply:
                        _kept, dropped = compact_plan(_read_plain(layout.segments / name, MAX_SEGMENT_BYTES),
                                                      today, keep_days)
                        log["segments"][name] = {"would_drop_lines": dropped}
                        continue
                    if holds:
                        continue
                    dropped = compact_segment(layout, name, today, keep_days)
                    log["segments"][name] = {"dropped_lines": dropped}
                    if dropped:
                        counts["log_segments_compacted"] += 1
                        counts["log_lines_dropped"] += dropped
            except (OSError, RetentionError) as exc:
                refs.append(getattr(exc, "ref", "EG_RET_LOG_FAILED"))

        # 4. Independent after-check: every run folder that was not pruned is byte-identical.
        after = _inventory(layout, [run for run in runs if run.name not in pruned])
        changed = sorted(name for name, digest in before.items() if name not in pruned and after.get(name) != digest)
        if changed:
            refs.append("EG_RET_NON_TARGET_CHANGED")

        for run in runs:
            if run.kind in PRESERVED_KINDS:
                counts["protected"] += 1
            elif run.name not in pruned:
                counts["retained"] += 1
        if apply:
            refs += holds  # an apply that held is not a clean completion
        counts["errors"] = len(refs)
        report = {
            "schema": REPORT_SCHEMA, "mode": mode, "at": now.isoformat(), "counts": counts, "refs": refs,
            "holds": holds,
            "rules": {"retention_complete_days": RETENTION_COMPLETE_DAYS,
                      "settle_hours": SETTLE.total_seconds() / 3600,
                      "active_fence_hours": ACTIVE_FENCE.total_seconds() / 3600,
                      "apply_window_sgt": [APPLY_WINDOW[0].isoformat(), APPLY_WINDOW[1].isoformat()]},
            "runs": [{"run": run.name, "kind": run.kind, "reason": run.reason, "complete_days": run.complete_days,
                      "eligible": run.eligible, "action": run.action, "files": run.files} for run in runs],
            "log": log,
            "inventory": {"before_sha256": _sha256(_json_bytes(before)), "after_sha256": _sha256(_json_bytes(after)),
                          "changed_non_targets": changed},
        }
        report_id = f"{now:%Y%m%d-%H%M%S}-{mode.lower()}-{secrets.token_hex(2)}"
        _write_new(layout.reports / f"{report_id}.json", _json_bytes(report))
    outcome = "COMPLETED" if not counts["errors"] else "HOLD"
    # A dry run reports its would-be holds as information; they do not make the dry run fail.
    return {"schema": SUMMARY_SCHEMA, "mode": mode, "outcome": outcome,
            "exit_code": EXIT_OK if outcome == "COMPLETED" else EXIT_HOLD, "counts": counts,
            "refs": sorted(set(refs + holds)), "report": report_id}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: ARG002 - closed refusal instead of usage text
        raise RetentionError("EG_RET_ARGUMENTS_REFUSED")


def main(argv: list[str] | None = None, *, home: Path | None = None,
         clock: Callable[[], datetime] | None = None) -> int:
    parser = _Parser(add_help=False)
    parser.add_argument("--apply", action="store_true")
    mode = "DRY_RUN"
    try:
        args = parser.parse_args(argv)
        mode = "APPLY" if args.apply else "DRY_RUN"
        summary = execute(Layout(home or installed_home()), apply=args.apply,
                          clock=clock or (lambda: datetime.now(timezone.utc)))
    except RetentionError as exc:
        summary = {"schema": SUMMARY_SCHEMA, "mode": mode, "outcome": "REFUSED", "exit_code": EXIT_REFUSED,
                   "refs": [exc.ref]}
    except Exception:  # noqa: BLE001 - one generic, support-safe line; no path or private value
        summary = {"schema": SUMMARY_SCHEMA, "mode": mode, "outcome": "HOLD", "exit_code": EXIT_HOLD,
                   "refs": ["EG_RET_UNEXPECTED"]}
    print(json.dumps(summary, sort_keys=True))
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
