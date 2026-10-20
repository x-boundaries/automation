"""EnergyGrid run-evidence retention (#257): synthetic, offline tests.

Every test builds a throwaway EG_HOME under the system temp folder with synthetic run evidence shaped
exactly like the Core's acquire/status/daily files and the monitor reporter's write-once files. Nothing
reads or writes the real install, Task Scheduler, n8n, Google, Gmail or any portal.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "runtime" / "maintenance" / "energygrid_retention.py"
WRAPPER = PROJECT / "runtime" / "maintenance" / "egretention.ps1.template"
TASK = PROJECT / "task-scheduler" / "energygrid_retention.task.example.xml"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

_spec = importlib.util.spec_from_file_location("energygrid_retention_under_test", SCRIPT)
ret = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ret  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(ret)

SGT = ret.SGT
NOW = datetime(2026, 10, 20, 19, 0, 0, tzinfo=SGT)   # inside the 15:00-23:00 SGT apply window
TODAY = NOW.date()
CORE = "energygrid.native.v4.result.v1"
STREAMS = ("EB_BILL", "TENANT_BILL")
PRIVATE_NAME = "Synthetic Private Bill 0001.pdf"


class SimulatedCrash(BaseException):
    """Not an Exception: escapes every handler in the tool, like a power cut."""


def day_with_complete_days(days: int) -> date:
    return TODAY - timedelta(days=days + 1)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_docs(started: datetime, run_id: str = "12345678-1234-4234-8234-123456789abc") -> dict:
    invoice = {"display_filename": PRIVATE_NAME, "adopted_from_v3": False, "named_archive": "PRESENT",
               "drive_upload": {"state": "VERIFIED", "support_ref": None, "receipt": "gdrive:SyntheticFileId01",
                                "hash_provenance": "LOCAL_MIRROR_SHA256", "remote_attestation": "GOOGLE_SHA256"}}
    return {
        "acquire.json": {
            "schema": CORE, "command": "acquire", "outcome": "ACQUIRED", "exit_code": 0, "run_id": run_id,
            "recovered_unknown": 0, "recovered_notifications": 0, "notifications_created": 0,
            "acquisition": {s: {"status": "ALREADY_ADMITTED", "support_ref": None} for s in STREAMS},
            "named_archive": {s: [{"display_filename": PRIVATE_NAME, "named_archive": "PRESENT"}] for s in STREAMS},
        },
        "status.json": {
            "schema": CORE, "command": "status", "outcome": "DRAINED", "exit_code": 0,
            "streams": {s: {"state": "DRAINED", "support_ref": None, "admitted": 1, "delivered": [PRIVATE_NAME],
                            "invoices": [json.loads(json.dumps(invoice))]} for s in STREAMS},
            "notifications": {"counts": {"PENDING": 0, "SEALED": 0, "SENT": 2, "UNKNOWN": 0, "HOLD": 0},
                              "items": [{"kind": "SUCCESS", "stream": s, "state": "SENT", "status": "SENT_OK",
                                         "support_ref": None, "display_filename": PRIVATE_NAME} for s in STREAMS]},
            "ceiling": {"period": "MONTH"}, "effects": {"drive_dispatches": 2, "gmail_dispatches": 2},
            "remote_verify": {"required": True},
        },
        "daily.json": {
            "schema": "energygrid.native_daily.run.v1", "run_id": run_id,
            "started_at": iso(started + timedelta(seconds=1)), "finished_at": iso(started + timedelta(seconds=40)),
            "acquire_outcome": "ACQUIRED", "status_outcome": "DRAINED",
            "steps": [{"step": "next", "outcome": "IDLE"}],
            "email": {"probe": None, "sent": 0, "uncertain": 0, "held": 0},
            "monitor_report": "SUBMIT", "withheld_reasons": [],
        },
        "monitor-report.json": {
            "schema": "xb.energygrid.monitor.v1", "run_id": run_id,
            "observed_at_utc": iso(started + timedelta(seconds=45)),
            "streams": {s: {"result": "OK", "support_ref": None, "incident_ref": None} for s in STREAMS},
        },
        "monitor-post.json": {"outcome": "EG_MONITOR_LOCAL_REPORT_POST_ACCEPTED"},
    }


def tree_digest(root: Path, exclude: Path | None = None) -> dict[str, str]:
    """Every file under root (not following links) -> sha256, except under `exclude`."""
    result = {}
    for folder, dirs, files in os.walk(root):
        if exclude is not None and Path(folder).resolve().is_relative_to(exclude.resolve()):
            dirs[:] = []
            continue
        for name in files:
            path = Path(folder) / name
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class Home:
    """A throwaway EG_HOME with data/runs, non-target production look-alikes and the tool installed."""

    def __init__(self, base: Path) -> None:
        self.root = base / "EnergyGrid"
        self.runs = self.root / "data" / "runs"
        self.runs.mkdir(parents=True)
        # The protected install creates data\retention with its private ACL (docs section 6); the tool never does.
        (self.root / "data" / "retention").mkdir()
        (self.root / "maintenance").mkdir()
        shutil.copy2(SCRIPT, self.root / "maintenance" / SCRIPT.name)
        # Non-targets the tool must never touch.
        (self.root / "data" / "state").mkdir()
        (self.root / "data" / "state" / "energygrid.v4.sqlite").write_bytes(b"SQLite format 3\x00synthetic")
        (self.root / "data" / "state" / "energygrid.v4.sqlite.notifications.sqlite").write_bytes(b"sidecar")
        (self.root / "data" / "outbox").mkdir()
        (self.root / "data" / "outbox" / "dispatch.marker").write_bytes(b"marker")
        (self.root / "data" / "archive").mkdir()
        (self.root / "data" / "archive" / "Synthetic.pdf").write_bytes(b"%PDF-1.4 synthetic")
        (self.root / "runtime" / "config").mkdir(parents=True)
        (self.root / "runtime" / "config" / "native.daily.json").write_text("{}", encoding="utf-8")

    @property
    def retention(self) -> Path:
        return self.root / "data" / "retention"

    def layout(self):
        return ret.Layout(self.root)

    def run(self, started: datetime, mutate=None, *, files: dict | None = None, raw: dict | None = None,
            drop: tuple = (), mtime_offset: timedelta | None = None) -> Path:
        folder = self.runs / started.strftime("%Y%m%d-%H%M%S")
        folder.mkdir()
        docs = files if files is not None else clean_docs(started)
        if mutate:
            mutate(docs)
        written = started + timedelta(seconds=50)
        for name, document in docs.items():
            if name in drop:
                continue
            data = raw[name] if raw and name in raw else (json.dumps(document, indent=2) + "\n").encode("utf-8")
            (folder / name).write_bytes(data)
            stamp = (written + (mtime_offset or timedelta())).timestamp()
            os.utime(folder / name, (stamp, stamp))
        return folder

    def clean_run(self, complete: int, hour: int = 11, minute: int = 30, second: int = 1, **kwargs) -> Path:
        day = day_with_complete_days(complete)
        return self.run(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=SGT), **kwargs)

    def main(self, *argv: str, now: datetime = NOW, clock=None) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ret.main(list(argv), home=self.root, clock=clock or (lambda: now))
        line = out.getvalue().strip()
        return code, json.loads(line)

    def report(self) -> dict:
        # The fixed test clock gives every report the same timestamp: take the most recently written.
        reports = sorted((self.retention / "reports").iterdir(), key=lambda path: path.stat().st_mtime_ns)
        return json.loads(reports[-1].read_text(encoding="utf-8"))

    def run_entry(self, folder: Path) -> dict:
        return next(item for item in self.report()["runs"] if item["run"] == folder.name)

    def digest(self) -> dict[str, str]:
        return tree_digest(self.root, exclude=self.retention)


class RetentionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Home(Path(self._tmp.name))
        self._fault = ret._fault

    def tearDown(self) -> None:
        ret._fault = self._fault
        self._tmp.cleanup()

    def crash_at(self, point: str, times: int = 1) -> None:
        remaining = {"n": times}

        def fault(name: str) -> None:
            if name == point and remaining["n"] > 0:
                remaining["n"] -= 1
                raise SimulatedCrash(point)

        ret._fault = fault

    def crash(self, *argv: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SimulatedCrash):
            ret.main(list(argv), home=self.home.root, clock=lambda: NOW)
        ret._fault = self._fault

    def fail_at(self, point: str, before=None) -> None:
        """An ordinary OSError (not a crash) once at `point`, after running `before` if given."""
        def fault(name: str) -> None:
            if name == point:
                ret._fault = self._fault
                if before:
                    before()
                raise OSError("injected failure")

        ret._fault = fault

    def write_log(self, lines: list[bytes], bom: bool = True) -> Path:
        path = self.home.root / "data" / "egdaily.log"
        path.write_bytes((ret.BOM if bom else b"") + b"".join(lines))
        return path

    def write_segment(self, name: str, lines: list[bytes]) -> Path:
        folder = self.home.retention / "egdaily-log"
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(b"".join(lines))
        return folder / name


# ---------------------------------------------------------------------------------------------------
# Classification and age
# ---------------------------------------------------------------------------------------------------


class AgeBoundary(RetentionCase):
    def test_89_and_90_complete_days_retained_91_eligible_in_dry_run(self) -> None:
        r89, r90, r91 = (self.home.clean_run(days) for days in (89, 90, 91))
        before = self.home.digest()
        code, summary = self.home.main()
        self.assertEqual((code, summary["outcome"], summary["mode"]), (0, "COMPLETED", "DRY_RUN"))
        self.assertEqual(summary["counts"]["eligible"], 1)
        self.assertEqual(summary["counts"]["pruned"], 0)
        self.assertEqual(self.home.run_entry(r89)["complete_days"], 89)
        self.assertEqual(self.home.run_entry(r89)["reason"], "EG_RET_WITHIN_RETENTION")
        self.assertFalse(self.home.run_entry(r90)["eligible"])
        self.assertEqual(self.home.run_entry(r91)["action"], "WOULD_PRUNE")
        self.assertEqual(self.home.digest(), before, "a dry run changes nothing outside data/retention")

    def test_age_is_counted_in_sgt_not_utc(self) -> None:
        folder = self.home.clean_run(90)
        # 16:30Z on 20 Oct is 00:30 SGT on 21 Oct: one more complete SGT day has passed.
        late = datetime(2026, 10, 20, 16, 30, tzinfo=timezone.utc)
        self.home.main(now=late)
        self.assertTrue(self.home.run_entry(folder)["eligible"])
        self.assertEqual(self.home.run_entry(folder)["complete_days"], 91)

    def test_folder_name_must_agree_with_recorded_start(self) -> None:
        # Folder says 91+ days ago, the run's own UTC start says 10 days ago: contradictory, preserved.
        recent = NOW - timedelta(days=10)
        day = day_with_complete_days(95)
        started = datetime(day.year, day.month, day.day, 11, 30, 1, tzinfo=SGT)
        folder = self.home.run(started, files=clean_docs(recent))
        self.home.main()
        entry = self.home.run_entry(folder)
        self.assertFalse(entry["eligible"])
        self.assertEqual(entry["reason"], "EG_RET_DAILY_TIME_INCONSISTENT")

    def test_file_times_alone_never_make_a_run_old(self) -> None:
        # A young run whose files were back-dated is not eligible, and is flagged as inconsistent.
        folder = self.home.clean_run(10, mtime_offset=-timedelta(days=300))
        self.home.main()
        entry = self.home.run_entry(folder)
        self.assertFalse(entry["eligible"])
        self.assertEqual(entry["reason"], "EG_RET_EVIDENCE_TIME_INCONSISTENT")

    def test_same_day_incident_keeps_the_clean_run_of_that_day(self) -> None:
        clean = self.home.clean_run(120, hour=11)
        incident = self.home.clean_run(120, hour=15, mutate=lambda d: d["daily.json"].update(
            monitor_report="WITHHELD", withheld_reasons=["EG_DAILY_FAILURE_NOTICE_UNDELIVERED"]))
        self.home.main()
        self.assertFalse(self.home.run_entry(clean)["eligible"])
        self.assertEqual(self.home.run_entry(clean)["reason"], "EG_RET_SAME_DAY_INCIDENT")
        self.assertEqual(self.home.run_entry(incident)["kind"], "INCIDENT")

    def test_future_run_is_preserved_and_holds_apply(self) -> None:
        future = NOW + timedelta(days=2)
        folder = self.home.run(future)
        old = self.home.clean_run(120)
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 20)
        self.assertIn("EG_RET_FUTURE_RUN_PRESENT", summary["refs"])
        self.assertTrue(old.exists())
        self.assertEqual(self.home.run_entry(folder)["kind"], "FUTURE")


def _set(path: tuple, value):
    def mutate(docs: dict) -> None:
        target = docs
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


def _delete(path: tuple):
    def mutate(docs: dict) -> None:
        target = docs
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return mutate


NEGATIVE_EVIDENCE = {
    # name: (mutate, drop, raw, expected kind)
    "daily withheld": (_set(("daily.json", "withheld_reasons"), ["EG_DAILY_CORE_REFUSED"]), (), None, "INCIDENT"),
    "daily report withheld": (_set(("daily.json", "monitor_report"), "WITHHELD"), (), None, "INCIDENT"),
    "email uncertain": (_set(("daily.json", "email", "uncertain"), 1), (), None, "INCIDENT"),
    "email held": (_set(("daily.json", "email", "held"), 1), (), None, "INCIDENT"),
    "email probe failed": (_set(("daily.json", "email", "probe"), "EG_DAILY_EMAIL_PROBE_FAILED"), (), None,
                           "INCIDENT"),
    "email count contradicts steps": (_set(("daily.json", "email", "sent"), 1), (), None, "INCOMPLETE"),
    "drive sync pending step": (_set(("daily.json", "steps"), [
        {"step": "drive-await", "outcome": "SYNC_PENDING", "support_ref": "EG_DAILY_DRIVE_SYNC_PENDING"},
        {"step": "next", "outcome": "IDLE"}]), (), None, "INCIDENT"),
    "last step not idle": (_set(("daily.json", "steps"), [{"step": "next", "outcome": "RETRY_LATER"}]), (), None,
                           "INCIDENT"),
    "daily finished before start": (_set(("daily.json", "finished_at"), "2000-01-01T00:00:00Z"), (), None,
                                    "INCOMPLETE"),
    "notification pending": (_set(("status.json", "notifications", "counts", "PENDING"), 1), (), None, "INCIDENT"),
    "notification hold": (_set(("status.json", "notifications", "counts", "HOLD"), 1), (), None, "INCIDENT"),
    "notification unknown": (_set(("status.json", "notifications", "counts", "UNKNOWN"), 1), (), None, "INCIDENT"),
    "tenant stream pending": (_set(("status.json", "streams", "TENANT_BILL", "state"), "PENDING"), (), None,
                              "INCIDENT"),
    "eb stream blocked": (_set(("status.json", "streams", "EB_BILL", "state"), "BLOCKED"), (), None, "INCIDENT"),
    "invoice not verified": (_set(("status.json", "streams", "EB_BILL", "invoices", 0, "drive_upload", "state"),
                                  "DISPATCHED"), (), None, "INCIDENT"),
    "tenant acquisition failed": (_set(("acquire.json", "acquisition", "TENANT_BILL"),
                                       {"status": "LOGIN_FAILED", "support_ref": "EG_TENANT_AUTH_FAILED"}), (), None,
                                  "INCIDENT"),
    "acquisition stream missing": (_delete(("acquire.json", "acquisition", "EB_BILL")), (), None, "INCOMPLETE"),
    "recovered unknown work": (_set(("acquire.json", "recovered_unknown"), 1), (), None, "INCIDENT"),
    "named archive conflict": (_set(("acquire.json", "named_archive", "EB_BILL", 0, "named_archive"), "CONFLICT"),
                               (), None, "INCIDENT"),
    "monitor post failed": (_set(("monitor-post.json", "outcome"), "EG_MONITOR_LOCAL_WEBHOOK_UNAVAILABLE"), (), None,
                            "INCIDENT"),
    "monitor stream pending": (_set(("monitor-report.json", "streams", "EB_BILL"),
                                    {"result": "PENDING", "support_ref": "EG_MONITOR_PENDING_AFTER_RUN",
                                     "incident_ref": "bill-0123456789abcdef01234567"}), (), None, "INCIDENT"),
    "monitor run id mismatch": (_set(("monitor-report.json", "run_id"), "87654321-1234-4234-8234-123456789abc"), (),
                                None, "INCOMPLETE"),
    "missing daily": (None, ("daily.json",), None, "INCOMPLETE"),
    "missing status": (None, ("status.json",), None, "INCOMPLETE"),
    "missing monitor post (exit 0 alone)": (None, ("monitor-post.json",), None, "INCIDENT"),
    "missing monitor report and post": (None, ("monitor-report.json", "monitor-post.json"), None, "INCIDENT"),
    "truncated json": (None, (), {"status.json": b'{"schema": "energygrid.native.v4.result.v1", "comm'}, "INCOMPLETE"),
    "duplicate keys": (None, (), {"monitor-post.json": b'{"outcome": "x", "outcome": '
                                                      b'"EG_MONITOR_LOCAL_REPORT_POST_ACCEPTED"}'}, "INCOMPLETE"),
    "non-finite number": (None, (), {"daily.json": b'{"schema": NaN}'}, "INCOMPLETE"),
    "byte order mark": (None, (), {"monitor-post.json": b'\xef\xbb\xbf{"outcome": '
                                                       b'"EG_MONITOR_LOCAL_REPORT_POST_ACCEPTED"}'}, "INCOMPLETE"),
    "not utf-8": (None, (), {"monitor-post.json": b'{"outcome": "\xff"}'}, "INCOMPLETE"),
    "wrong core schema": (_set(("status.json", "schema"), "energygrid.native.v3.result.v1"), (), None, "INCOMPLETE"),
    "json array not object": (None, (), {"acquire.json": b"[]"}, "INCOMPLETE"),
}


class NegativeEvidence(RetentionCase):
    def test_every_doubtful_old_run_is_preserved(self) -> None:
        folders = {}
        for offset, (label, (mutate, drop, raw, _kind)) in enumerate(NEGATIVE_EVIDENCE.items()):
            # One distinct old day each, so the same-day rule does not mask the evidence rule.
            folders[label] = self.home.clean_run(100 + offset, mutate=mutate, drop=drop, raw=raw)
        code, summary = self.home.main()
        self.assertEqual(code, 0)
        self.assertEqual(summary["counts"]["eligible"], 0)
        self.assertEqual(summary["counts"]["protected"], len(NEGATIVE_EVIDENCE))
        for label, (_mutate, _drop, _raw, kind) in NEGATIVE_EVIDENCE.items():
            with self.subTest(label):
                entry = self.home.run_entry(folders[label])
                self.assertFalse(entry["eligible"])
                self.assertEqual(entry["kind"], kind, entry["reason"])
                self.assertEqual(entry["action"], "WOULD_ARCHIVE")
                self.assertTrue(entry["reason"].startswith("EG_RET_"))

    def test_unexpected_extra_file_is_preserved(self) -> None:
        folder = self.home.clean_run(150)
        (folder / "notes.txt").write_text("operator note", encoding="utf-8")
        self.home.main()
        self.assertEqual(self.home.run_entry(folder)["reason"], "EG_RET_RUN_UNEXPECTED_FILE")

    def test_invalid_calendar_name_is_preserved(self) -> None:
        folder = self.home.runs / "20261399-113000"
        folder.mkdir()
        self.home.main()
        self.assertEqual(self.home.run_entry(folder)["reason"], "EG_RET_RUN_NAME_INVALID")

    def test_non_run_entries_are_skipped_and_untouched(self) -> None:
        (self.home.runs / "notes.txt").write_text("x", encoding="utf-8")
        (self.home.runs / "20260101-1130").mkdir()
        (self.home.runs / "20260101-113000-copy").mkdir()
        self.home.clean_run(150)
        before = self.home.digest()
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["counts"]["skipped"], 3)
        self.assertEqual(summary["counts"]["pruned"], 1)
        self.assertTrue((self.home.runs / "notes.txt").exists())
        self.assertTrue((self.home.runs / "20260101-1130").is_dir())
        pruned_prefix = str(Path("data") / "runs" / f"{day_with_complete_days(150):%Y%m%d}-113001") + os.sep
        expected = {k: v for k, v in before.items() if not k.startswith(pruned_prefix)}
        self.assertEqual(self.home.digest(), expected)


# ---------------------------------------------------------------------------------------------------
# Reparse points, links and path substitution
# ---------------------------------------------------------------------------------------------------


def make_junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


@unittest.skipUnless(os.name == "nt", "junctions and Windows sharing semantics")
class LinksAndPaths(RetentionCase):
    def test_junction_run_folder_is_never_followed(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        day = day_with_complete_days(150)
        started = datetime(day.year, day.month, day.day, 11, 30, 1, tzinfo=SGT)
        for name, doc in clean_docs(started).items():
            (outside / name).write_text(json.dumps(doc), encoding="utf-8")
        link = self.home.runs / started.strftime("%Y%m%d-%H%M%S")
        make_junction(link, outside)
        before = tree_digest(outside)
        code, summary = self.home.main("--apply")
        self.assertEqual(self.home.run_entry(link)["kind"], "UNSAFE")
        self.assertEqual(summary["counts"]["pruned"], 0)
        self.assertEqual(tree_digest(outside), before)
        self.assertTrue(os.path.lexists(link))
        self.assertFalse((self.home.retention / "incident-archive" / link.name).exists(), "never archived")

    def test_junction_with_an_impossible_date_name_is_never_followed_or_archived(self) -> None:
        outside = Path(self._tmp.name) / "outside-invalid"
        outside.mkdir()
        (outside / "private.txt").write_text("not EnergyGrid evidence", encoding="utf-8")
        link = self.home.runs / "20261399-113000"
        make_junction(link, outside)
        before = tree_digest(outside)
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.home.run_entry(link)["kind"], "UNSAFE")
        self.assertEqual(list((self.home.retention / "incident-archive").iterdir()), [])
        self.assertEqual(tree_digest(outside), before)

    def test_hard_link_and_subfolder_inside_a_run_are_unsafe(self) -> None:
        linked = self.home.clean_run(150)
        os.link(self.home.root / "data" / "archive" / "Synthetic.pdf", linked / "extra.pdf")
        nested = self.home.clean_run(151)
        (nested / "sub").mkdir()
        self.home.main("--apply")
        for folder in (linked, nested):
            self.assertEqual(self.home.run_entry(folder)["kind"], "UNSAFE")
            self.assertTrue(folder.is_dir())
        self.assertEqual((self.home.root / "data" / "archive" / "Synthetic.pdf").read_bytes(), b"%PDF-1.4 synthetic")

    def test_symlinked_evidence_file_is_unsafe(self) -> None:
        folder = self.home.clean_run(150, drop=("monitor-post.json",))
        target = Path(self._tmp.name) / "post.json"
        target.write_text(json.dumps({"outcome": "EG_MONITOR_LOCAL_REPORT_POST_ACCEPTED"}), encoding="utf-8")
        try:
            os.symlink(target, folder / "monitor-post.json")
        except OSError:
            self.skipTest("symbolic links need a privilege this session does not hold")
        self.home.main("--apply")
        self.assertEqual(self.home.run_entry(folder)["kind"], "UNSAFE")
        self.assertTrue(target.exists())

    def test_substituted_runs_root_is_refused(self) -> None:
        real = self.home.root / "data" / "runs-real"
        self.home.runs.rename(real)
        make_junction(self.home.runs, real)
        day = day_with_complete_days(150)
        (real / f"{day:%Y%m%d}-113001").mkdir()
        before = tree_digest(real)
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["outcome"], summary["refs"]), (64, "REFUSED", ["EG_RET_TREE_REFUSED"]))
        self.assertEqual(tree_digest(real), before)

    def test_substituted_data_ancestor_is_refused(self) -> None:
        real_data = Path(self._tmp.name) / "elsewhere-data"
        (self.home.root / "data").rename(real_data)
        make_junction(self.home.root / "data", real_data)
        code, summary = self.home.main()
        self.assertEqual((code, summary["refs"]), (64, ["EG_RET_TREE_REFUSED"]))

    def test_log_that_is_a_hard_link_is_refused(self) -> None:
        os.link(self.home.root / "data" / "archive" / "Synthetic.pdf", self.home.root / "data" / "egdaily.log")
        code, summary = self.home.main()
        self.assertEqual((code, summary["refs"]), (64, ["EG_RET_LOG_NOT_PLAIN"]))

    def test_run_held_open_is_not_pruned(self) -> None:
        folder = self.home.clean_run(150)
        second = self.home.clean_run(149)  # also eligible: must wait for the next execution
        second_before = tree_digest(second)
        with open(folder / "daily.json", "rb"):
            code, summary = self.home.main("--apply")
        self.assertEqual(code, 20)
        self.assertIn("EG_RET_RUN_IN_USE", summary["refs"])
        self.assertIn("EG_RET_PRUNE_HOLD_NOTHING_CHANGED", summary["refs"])
        self.assertTrue((folder / "daily.json").exists())
        self.assertEqual(tree_digest(second), second_before)
        self.assertEqual(list((self.home.retention / "prune-journal").iterdir()), [])
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["pruned"]), (0, 2))


# ---------------------------------------------------------------------------------------------------
# Apply: archive, prune, idempotence, gates
# ---------------------------------------------------------------------------------------------------


class Apply(RetentionCase):
    def test_apply_prunes_only_the_proven_old_run_and_archives_the_incident(self) -> None:
        old = self.home.clean_run(91)
        young = self.home.clean_run(89)
        incident = self.home.clean_run(200, drop=("monitor-post.json",))
        incident_before = tree_digest(incident)
        non_targets = {k: v for k, v in self.home.digest().items() if not k.startswith("data" + os.sep + "runs")}
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["outcome"]), (0, "COMPLETED"), summary)
        counts = summary["counts"]
        self.assertEqual((counts["considered"], counts["eligible"], counts["pruned"], counts["retained"],
                          counts["protected"], counts["archived"], counts["errors"]), (3, 1, 1, 1, 1, 1, 0))
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertEqual(tree_digest(incident), incident_before, "the original incident is never altered")
        archive = self.home.retention / "incident-archive" / incident.name
        manifest = ret.verify_archive(archive)
        self.assertIsNotNone(manifest, "consumer readback of the private archive")
        self.assertEqual({item["name"] for item in manifest["files"]}, set(incident_before))
        self.assertEqual({item["name"]: item["sha256"] for item in manifest["files"]}, incident_before)
        ledger = json.loads((self.home.retention / "pruned" / f"{old.name}.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["state"], "PRUNED")
        self.assertEqual(set(ledger["files"]), set(ret.RUN_FILES))
        self.assertEqual(list((self.home.retention / "prune-quarantine").iterdir()), [])
        self.assertEqual(list((self.home.retention / "prune-journal").iterdir()), [])
        after = {k: v for k, v in self.home.digest().items() if not k.startswith("data" + os.sep + "runs")}
        self.assertEqual(after, non_targets, "state, sidecars, outbox, PDFs and config are untouched")
        report = self.home.report()
        self.assertEqual(report["inventory"]["changed_non_targets"], [])

    def test_summary_line_carries_no_names(self) -> None:
        old = self.home.clean_run(91)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ret.main(["--apply"], home=self.home.root, clock=lambda: NOW)
        text = out.getvalue()
        self.assertNotIn(old.name, text)
        self.assertNotIn("Synthetic", text)
        self.assertNotIn(str(self.home.root), text)
        self.assertEqual(len(text.strip().splitlines()), 1)

    def test_repeat_apply_is_idempotent(self) -> None:
        self.home.clean_run(91)
        self.home.clean_run(200, mutate=_set(("daily.json", "withheld_reasons"), ["EG_DAILY_CORE_REFUSED"]))
        self.home.main("--apply")
        snapshot = tree_digest(self.home.root, exclude=self.home.retention / "reports")
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual((summary["counts"]["pruned"], summary["counts"]["archived"],
                          summary["counts"]["archive_verified"]), (0, 0, 1))
        self.assertEqual(tree_digest(self.home.root, exclude=self.home.retention / "reports"), snapshot)

    def test_apply_outside_window_is_refused(self) -> None:
        old = self.home.clean_run(91)
        for moment in (datetime(2026, 10, 20, 11, 30, tzinfo=SGT), datetime(2026, 10, 20, 14, 59, tzinfo=SGT),
                       datetime(2026, 10, 20, 23, 0, tzinfo=SGT)):
            with self.subTest(moment=moment):
                code, summary = self.home.main("--apply", now=moment)
                self.assertEqual((code, summary["refs"]), (64, ["EG_RET_OUTSIDE_WINDOW"]))
        self.assertTrue(old.exists())

    def test_young_run_means_the_writer_may_be_active(self) -> None:
        old = self.home.clean_run(91)
        incident = self.home.clean_run(200, drop=("daily.json",))
        active = self.home.run(NOW - timedelta(hours=3), drop=("monitor-report.json", "monitor-post.json"))
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 20)
        self.assertIn("EG_RET_WRITER_MAY_BE_ACTIVE", summary["refs"])
        self.assertTrue(old.exists())
        self.assertFalse((self.home.retention / "incident-archive" / incident.name).exists())
        self.assertEqual(self.home.run_entry(active)["kind"], "SETTLING")
        # The same tree, dry run: reported, not an error.
        code, summary = self.home.main()
        self.assertEqual(code, 0)
        self.assertIn("EG_RET_WRITER_MAY_BE_ACTIVE", summary["refs"])

    def test_second_instance_is_refused(self) -> None:
        self.home.clean_run(91)
        ret.check_tree(self.home.layout())
        with ret.Lock(self.home.retention / "retention.lock"):
            code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["refs"]), (64, ["EG_RET_LOCK_HELD"]))

    def test_unknown_argument_is_refused(self) -> None:
        for argv in (["--root", "C:\\"], ["--apply", "extra"], ["--force"]):
            with self.subTest(argv=argv):
                code, summary = self.home.main(*argv)
                self.assertEqual((code, summary["refs"]), (64, ["EG_RET_ARGUMENTS_REFUSED"]))
        self.assertEqual(list(self.home.retention.iterdir()), [], "a refused call creates nothing")

    def test_existing_archive_that_no_longer_verifies_holds_every_prune(self) -> None:
        old = self.home.clean_run(91)
        incident = self.home.clean_run(200, drop=("monitor-post.json",))
        self.home.main("--apply")
        self.assertFalse(old.exists())
        newer = self.home.clean_run(95)
        archived = self.home.retention / "incident-archive" / incident.name / "daily.json"
        archived.write_bytes(archived.read_bytes() + b" ")
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 20)
        self.assertIn("EG_RET_ARCHIVE_EXISTING_INVALID", summary["refs"])
        self.assertIn("EG_RET_PRUNE_SKIPPED_ARCHIVE_INCOMPLETE", summary["refs"])
        self.assertTrue(newer.exists())
        self.assertEqual(self.home.run_entry(newer)["action"], "HELD")


# ---------------------------------------------------------------------------------------------------
# Interrupted operations
# ---------------------------------------------------------------------------------------------------


class Interrupted(RetentionCase):
    def test_crash_after_rename_resumes_to_completion(self) -> None:
        old = self.home.clean_run(91)
        self.crash_at("prune:after-rename")
        self.crash("--apply")
        self.assertFalse(old.exists())
        self.assertTrue((self.home.retention / "prune-quarantine" / old.name).is_dir())
        self.assertTrue((self.home.retention / "prune-journal" / f"{old.name}.json").exists())
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["resumed"]), (0, 1), summary)
        self.assertEqual(list((self.home.retention / "prune-quarantine").iterdir()), [])
        self.assertTrue((self.home.retention / "pruned" / f"{old.name}.json").exists())

    def test_crash_mid_delete_resumes(self) -> None:
        old = self.home.clean_run(91)
        self.crash_at("prune:mid-delete")
        self.crash("--apply")
        held = self.home.retention / "prune-quarantine" / old.name
        self.assertEqual(len(list(held.iterdir())), 4)
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["resumed"]), (0, 1), summary)
        self.assertFalse(held.exists())

    def test_crash_after_journal_leaves_the_run_in_place(self) -> None:
        old = self.home.clean_run(91)
        self.crash_at("prune:after-journal")
        self.crash("--apply")
        self.assertTrue(old.exists())
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["pruned"]), (0, 1), summary)

    def test_tampered_quarantine_is_left_for_the_operator_and_holds_everything(self) -> None:
        old = self.home.clean_run(91)
        another = self.home.clean_run(92)
        self.crash_at("prune:after-rename")
        self.crash("--apply")
        held = self.home.retention / "prune-quarantine"
        tampered = next(held.iterdir())
        (tampered / "status.json").write_bytes(b"{}")
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 20)
        self.assertIn("EG_RET_PRUNE_DIGEST_MISMATCH", summary["refs"])
        self.assertTrue((tampered / "status.json").exists())
        survivors = [folder for folder in (old, another) if folder.exists()]
        self.assertEqual(len(survivors), 1, "the untouched run is held, not pruned")

    def test_quarantine_without_journal_holds(self) -> None:
        self.home.clean_run(91)
        ret.check_tree(self.home.layout())
        (self.home.retention / "prune-quarantine" / "20260101-113001").mkdir()
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 20)
        self.assertIn("EG_RET_QUARANTINE_WITHOUT_JOURNAL", summary["refs"])
        self.assertEqual(summary["counts"]["pruned"], 0)

    def test_interrupted_archive_is_cleaned_and_redone(self) -> None:
        incident = self.home.clean_run(200, drop=("daily.json",))
        old = self.home.clean_run(91)
        self.crash_at("archive:before-publish")
        self.crash("--apply")
        archive = self.home.retention / "incident-archive"
        self.assertEqual([p.name.startswith(".staging-") for p in archive.iterdir()], [True])
        self.assertTrue(old.exists(), "no prune before every incident is archived")
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual([p.name for p in archive.iterdir()], [incident.name])
        self.assertIsNotNone(ret.verify_archive(archive / incident.name))
        self.assertFalse(old.exists())

    def test_archive_digest_mismatch_is_detected_on_readback(self) -> None:
        incident = self.home.clean_run(200, drop=("daily.json",))
        self.home.main("--apply")
        archive = self.home.retention / "incident-archive" / incident.name
        self.assertIsNotNone(ret.verify_archive(archive))
        (archive / "status.json").write_bytes(b"tampered")
        self.assertIsNone(ret.verify_archive(archive))
        (archive / "status.json").unlink()
        self.assertIsNone(ret.verify_archive(archive))


# ---------------------------------------------------------------------------------------------------
# Web pre-G2 safety findings (#257:6282090315): gates first, prune failure holds, unsafe-run dates, ACL
# ---------------------------------------------------------------------------------------------------


def log_line(day: date, tail: str, at: str = "11:30:35") -> bytes:
    return f"{day.isoformat()}T{at} {tail}\r\n".encode("utf-8")


CLEAN = "exit=0 monitor_report=SUBMIT submitted=True withheld="


class WriterFenceBeforeRecovery(RetentionCase):
    """Finding 1: no cleanup, resumed deletion, archive, prune or log change while a writer may be active."""

    def interrupted_with_leftovers(self) -> tuple[Path, Path, Path]:
        old = self.home.clean_run(91)
        self.crash_at("prune:after-rename")
        self.crash("--apply")
        held = self.home.retention / "prune-quarantine" / old.name
        self.assertTrue(held.is_dir())
        staging = self.home.retention / "incident-archive" / ".staging-20260101-113001-0123abcd"
        staging.mkdir()
        (staging / "daily.json").write_bytes(b"{}")
        temp = self.home.retention / "egdaily-log" / ".egdaily.20260101-190000.log.compact-0123abcd.tmp"
        temp.write_bytes(b"partial")
        second = self.home.clean_run(95)
        log = self.write_log([log_line(day_with_complete_days(120), CLEAN), log_line(TODAY, CLEAN)])
        return held, second, log

    def assert_held_unchanged(self, before: dict, summary: dict, code: int, *refs: str) -> None:
        # Effects first, so a regression shows what was destroyed rather than a missing reference.
        self.assertEqual((summary["counts"]["resumed"], summary["counts"]["pruned"],
                          summary["counts"]["log_rotated"]), (0, 0, 0), summary)
        reports = self.home.retention / "reports"
        self.assertEqual(tree_digest(self.home.root, exclude=reports), before,
                         "no staging cleanup, temp cleanup, resumed delete, prune or rotation")
        self.assertEqual(code, 20, summary)
        for ref in refs:
            self.assertIn(ref, summary["refs"])
        self.assertIn("EG_RET_RECOVERY_DEFERRED", summary["refs"])

    def test_young_run_holds_interrupted_recovery_until_the_fence_clears(self) -> None:
        held, second, log = self.interrupted_with_leftovers()
        self.home.run(NOW - timedelta(hours=3), drop=("monitor-report.json", "monitor-post.json"))
        before = tree_digest(self.home.root, exclude=self.home.retention / "reports")
        code, summary = self.home.main("--apply")
        self.assert_held_unchanged(before, summary, code, "EG_RET_WRITER_MAY_BE_ACTIVE")
        self.assertEqual(len(list(held.iterdir())), 5)
        # 90 minutes later the young run is 4.5 hours old: still inside the window, the fence has cleared.
        code, summary = self.home.main("--apply", now=NOW + timedelta(minutes=90))
        self.assertEqual(code, 0, summary)
        self.assertEqual((summary["counts"]["resumed"], summary["counts"]["pruned"],
                          summary["counts"]["log_rotated"]), (1, 1, 1))
        self.assertFalse(held.exists())
        self.assertFalse(second.exists())
        self.assertFalse(log.exists())
        self.assertEqual(sorted(p.name for p in (self.home.retention / "incident-archive").iterdir()), [])
        self.assertTrue((self.home.retention / "pruned" / f"{held.name}.json").exists())

    def test_future_run_holds_interrupted_recovery(self) -> None:
        held, _second, _log = self.interrupted_with_leftovers()
        self.home.run(NOW + timedelta(days=2))
        before = tree_digest(self.home.root, exclude=self.home.retention / "reports")
        code, summary = self.home.main("--apply")
        self.assert_held_unchanged(before, summary, code, "EG_RET_FUTURE_RUN_PRESENT")

    def test_young_unsafe_entry_still_fences(self) -> None:
        # The fence reads names only: a young entry that is not even a plain folder still means "writer active".
        held, _second, _log = self.interrupted_with_leftovers()
        (self.home.runs / (NOW - timedelta(hours=1)).strftime("%Y%m%d-%H%M%S")).write_bytes(b"not a folder")
        before = tree_digest(self.home.root, exclude=self.home.retention / "reports")
        code, summary = self.home.main("--apply")
        self.assert_held_unchanged(before, summary, code, "EG_RET_WRITER_MAY_BE_ACTIVE")

    def test_dry_run_reports_pending_recovery_without_settling_it(self) -> None:
        held, _second, _log = self.interrupted_with_leftovers()
        before = tree_digest(self.home.root, exclude=self.home.retention / "reports")
        code, summary = self.home.main()
        self.assertEqual(code, 0, summary)
        self.assertIn("EG_RET_RECOVERY_PENDING", summary["refs"])
        self.assertEqual(tree_digest(self.home.root, exclude=self.home.retention / "reports"), before)


class PruneFailureHolds(RetentionCase):
    """Finding 2: a failed or uncertain prune stops every later prune and log change in that execution."""

    def setup_tree(self) -> tuple[Path, Path, Path, Path]:
        first = self.home.clean_run(95)    # oldest name: pruned first
        second = self.home.clean_run(91)
        log = self.write_log([log_line(day_with_complete_days(120), CLEAN), log_line(TODAY, CLEAN)])
        segment = self.write_segment("egdaily.20260601-190000.log",
                                     [log_line(day_with_complete_days(130), CLEAN), log_line(TODAY, CLEAN)])
        return first, second, log, segment

    def assert_nothing_after(self, code: int, summary: dict, second: Path, snapshot: dict, hold: str) -> None:
        # Effects first, so a regression shows what changed rather than a missing reference.
        self.assertEqual((summary["counts"]["pruned"], summary["counts"]["log_rotated"],
                          summary["counts"]["log_segments_compacted"]), (0, 0, 0), summary)
        self.assertEqual(tree_digest(second), snapshot["second"], "the next eligible run is untouched")
        self.assertEqual(self.home.root.joinpath("data", "egdaily.log").read_bytes(), snapshot["log"])
        self.assertEqual(snapshot["segment_path"].read_bytes(), snapshot["segment"])
        self.assertEqual(code, 20, summary)
        self.assertEqual(summary["outcome"], "HOLD")
        self.assertIn(hold, summary["refs"])
        self.assertEqual(self.home.report()["log"]["rotation"], "HELD")
        self.assertEqual(self.home.run_entry(second)["action"], "HELD")
        self.assertEqual(self.home.report()["inventory"]["changed_non_targets"], [])

    def snapshot(self, second: Path, log: Path, segment: Path) -> dict:
        return {"second": tree_digest(second), "log": log.read_bytes(), "segment": segment.read_bytes(),
                "segment_path": segment}

    def test_failure_after_rename_holds_then_resumes_next_time(self) -> None:
        first, second, log, segment = self.setup_tree()
        snap = self.snapshot(second, log, segment)
        self.fail_at("prune:after-rename")
        code, summary = self.home.main("--apply")
        self.assert_nothing_after(code, summary, second, snap, "EG_RET_PRUNE_HOLD_RESUMABLE")
        held = self.home.retention / "prune-quarantine" / first.name
        self.assertEqual(len(list(held.iterdir())), 5, "quarantine evidence kept for recovery")
        self.assertTrue((self.home.retention / "prune-journal" / f"{first.name}.json").exists())
        self.assertFalse((self.home.retention / "pruned" / f"{first.name}.json").exists(), "not a completion")
        self.assertEqual(self.home.run_entry(first)["action"], "PRUNE_FAILED")
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual((summary["counts"]["resumed"], summary["counts"]["pruned"],
                          summary["counts"]["log_rotated"]), (1, 1, 1))
        self.assertFalse(second.exists())

    def test_failure_mid_delete_holds_and_is_never_a_completion(self) -> None:
        first, second, log, segment = self.setup_tree()
        snap = self.snapshot(second, log, segment)
        self.fail_at("prune:mid-delete")
        code, summary = self.home.main("--apply")
        self.assert_nothing_after(code, summary, second, snap, "EG_RET_PRUNE_HOLD_RESUMABLE")
        held = self.home.retention / "prune-quarantine" / first.name
        self.assertEqual(len(list(held.iterdir())), 4)
        self.assertFalse((self.home.retention / "pruned" / f"{first.name}.json").exists())
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["resumed"]), (0, 1), summary)
        ledger = json.loads((self.home.retention / "pruned" / f"{first.name}.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["state"], "PRUNED")

    def test_failure_with_changed_quarantine_needs_the_operator(self) -> None:
        first, second, log, segment = self.setup_tree()
        snap = self.snapshot(second, log, segment)
        held = self.home.retention / "prune-quarantine" / first.name
        self.fail_at("prune:after-rename", before=lambda: (held / "status.json").write_bytes(b"{}"))
        code, summary = self.home.main("--apply")
        self.assert_nothing_after(code, summary, second, snap, "EG_RET_PRUNE_HOLD_OPERATOR")
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 20)
        self.assertIn("EG_RET_PRUNE_DIGEST_MISMATCH", summary["refs"])
        self.assertEqual((held / "status.json").read_bytes(), b"{}", "left exactly as found")
        self.assertEqual(tree_digest(second), snap["second"])

    def test_failure_before_rename_changes_nothing_and_still_holds(self) -> None:
        first, second, log, segment = self.setup_tree()
        snap = self.snapshot(second, log, segment)
        self.fail_at("prune:after-journal")
        code, summary = self.home.main("--apply")
        self.assert_nothing_after(code, summary, second, snap, "EG_RET_PRUNE_HOLD_RESUMABLE")
        self.assertTrue(first.is_dir(), "the rename never happened")
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["pruned"]), (0, 2), summary)

    def test_run_changed_before_prune_holds(self) -> None:
        first, second, log, segment = self.setup_tree()
        snap = self.snapshot(second, log, segment)
        original, calls = ret.classify, {"n": 0}

        def changed(layout, name, now):
            run = original(layout, name, now)
            if name == first.name:
                calls["n"] += 1
                if calls["n"] > 1:  # the re-proof just before the prune sees different bytes
                    run.files = dict(run.files, **{"daily.json": "0" * 64})
            return run

        ret.classify = changed
        try:
            code, summary = self.home.main("--apply")
        finally:
            ret.classify = original
        self.assert_nothing_after(code, summary, second, snap, "EG_RET_RUN_CHANGED_BEFORE_PRUNE")
        self.assertTrue(first.is_dir())


class UnsafeRunKeepsItsDate(RetentionCase):
    """Finding 3: an unsafe run-named entry keeps the date its name encodes, so its day stays preserved."""

    def test_unsafe_entry_on_the_same_day_keeps_the_clean_run(self) -> None:
        clean = self.home.clean_run(150, hour=11)
        day = day_with_complete_days(150)
        unsafe = self.home.runs / f"{day:%Y%m%d}-150000"
        unsafe.write_bytes(b"a file where a run folder should be")
        code, summary = self.home.main("--apply")
        self.assertEqual(summary["counts"]["pruned"], 0, summary)
        self.assertTrue(clean.is_dir())
        self.assertEqual(self.home.run_entry(clean)["reason"], "EG_RET_SAME_DAY_INCIDENT")
        entry = self.home.run_entry(unsafe)
        self.assertEqual((entry["kind"], entry["complete_days"]), ("UNSAFE", 150))
        self.assertEqual(unsafe.read_bytes(), b"a file where a run folder should be")

    def test_unsafe_entry_with_an_impossible_date_has_no_day(self) -> None:
        (self.home.runs / "20261399-150000").write_bytes(b"x")
        clean = self.home.clean_run(150)
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["pruned"]), (0, 1), summary)
        self.assertFalse(clean.exists())
        self.assertEqual(self.home.run_entry(self.home.runs / "20261399-150000")["complete_days"], None)

    @unittest.skipUnless(os.name == "nt", "junctions")
    def test_same_day_junction_run_is_neither_followed_nor_lets_the_clean_run_go(self) -> None:
        clean = self.home.clean_run(150, hour=11)
        day = day_with_complete_days(150)
        started = datetime(day.year, day.month, day.day, 15, 0, 0, tzinfo=SGT)
        outside = Path(self._tmp.name) / "outside-same-day"
        outside.mkdir()
        for name, doc in clean_docs(started).items():
            (outside / name).write_text(json.dumps(doc), encoding="utf-8")
        link = self.home.runs / started.strftime("%Y%m%d-%H%M%S")
        make_junction(link, outside)
        outside_before = tree_digest(outside)
        clean_before = tree_digest(clean)
        day_line = log_line(day, CLEAN)
        self.write_log([day_line, log_line(TODAY, CLEAN)])
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["counts"]["pruned"], 0)
        self.assertEqual(tree_digest(clean), clean_before)
        self.assertEqual(self.home.run_entry(clean)["reason"], "EG_RET_SAME_DAY_INCIDENT")
        entry = self.home.run_entry(link)
        self.assertEqual((entry["kind"], entry["complete_days"]), ("UNSAFE", 150))
        self.assertTrue(os.path.lexists(link))
        self.assertEqual(tree_digest(outside), outside_before)
        self.assertFalse((self.home.retention / "incident-archive" / link.name).exists())
        segments = list((self.home.retention / "egdaily-log").iterdir())
        self.assertEqual(len(segments), 1)
        self.assertIn(day_line, segments[0].read_bytes(), "the unsafe run's day is an incident day for the log")


class RetentionFolderRequired(RetentionCase):
    """Finding 4 (tool side): the tool never creates the private folder; the protected install does."""

    def test_missing_retention_folder_is_refused_and_nothing_is_created(self) -> None:
        self.home.clean_run(150)
        self.home.retention.rmdir()
        before = tree_digest(self.home.root)
        for argv in ((), ("--apply",)):
            with self.subTest(argv=argv):
                code, summary = self.home.main(*argv)
                self.assertEqual((code, summary["refs"]), (64, ["EG_RET_RETENTION_MISSING"]))
        self.assertFalse(self.home.retention.exists())
        self.assertEqual(tree_digest(self.home.root), before)

    def test_retention_folder_that_is_not_a_folder_is_refused(self) -> None:
        self.home.retention.rmdir()
        self.home.retention.write_bytes(b"")
        code, summary = self.home.main()
        self.assertEqual((code, summary["outcome"]), (64, "REFUSED"))


# ---------------------------------------------------------------------------------------------------
# egdaily.log rotation and compaction
# ---------------------------------------------------------------------------------------------------


class SummaryLog(RetentionCase):
    def test_dry_run_reports_rotation_without_touching_the_log(self) -> None:
        path = self.write_log([log_line(day_with_complete_days(120), CLEAN), log_line(TODAY, CLEAN)])
        before = path.read_bytes()
        code, summary = self.home.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.home.report()["log"]["rotation"], "WOULD_ROTATE")
        self.assertEqual(path.read_bytes(), before)

    def test_rotation_and_compaction_keep_every_abnormal_and_incident_day_line(self) -> None:
        old_clean = day_with_complete_days(120)
        old_abnormal = day_with_complete_days(130)
        incident_run_day = day_with_complete_days(140)
        self.home.clean_run(140, mutate=_set(("daily.json", "monitor_report"), "WITHHELD"))
        boundary = day_with_complete_days(90)
        lines = [
            log_line(old_abnormal, CLEAN, "09:00:00"),                                  # same day as an abnormal line
            log_line(old_abnormal, "exit=20 monitor_report=WITHHELD submitted=False withheld=EG_DAILY_CORE_REFUSED"),
            log_line(incident_run_day, CLEAN),                                          # a preserved run's day
            log_line(old_clean, CLEAN),                                                 # dropped
            b"garbage without a date\r\n",                                              # unparseable: kept
            log_line(day_with_complete_days(125), "exit=0 monitor_report=UNREADABLE submitted=False withheld="),
            log_line(boundary, CLEAN),                                                  # exactly 90: kept
            log_line(TODAY - timedelta(days=1), CLEAN),
        ]
        path = self.write_log(lines)
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["counts"]["log_rotated"], 1)
        self.assertFalse(path.exists(), "the live log was moved aside, never truncated")
        segments = list((self.home.retention / "egdaily-log").iterdir())
        self.assertEqual(len(segments), 1)
        kept = segments[0].read_bytes()
        self.assertTrue(kept.startswith(ret.BOM))
        expected = ret.BOM + b"".join(line for index, line in enumerate(lines) if index != 3)
        self.assertEqual(kept, expected)
        self.assertEqual(summary["counts"]["log_lines_dropped"], 1)
        # The daily runner's next Add-Content simply starts a new file.
        path.write_bytes(log_line(TODAY, CLEAN))
        code, summary = self.home.main("--apply")
        self.assertEqual((code, summary["counts"]["log_lines_dropped"]), (0, 0))

    def test_recent_log_is_not_rotated(self) -> None:
        path = self.write_log([log_line(day_with_complete_days(5), CLEAN)])
        self.home.main("--apply")
        self.assertTrue(path.exists())
        self.assertEqual(self.home.report()["log"]["rotation"], "NOT_DUE")

    @unittest.skipUnless(os.name == "nt", "Windows refuses to rename a file another handle holds open")
    def test_log_held_by_a_writer_is_not_rotated(self) -> None:
        path = self.write_log([log_line(day_with_complete_days(120), CLEAN)])
        with open(path, "ab") as writer:
            code, summary = self.home.main("--apply")
            writer.write(log_line(TODAY, CLEAN))
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.home.report()["log"]["rotation"], "LOG_IN_USE")
        self.assertTrue(path.read_bytes().endswith(log_line(TODAY, CLEAN)))
        self.assertEqual(list((self.home.retention / "egdaily-log").iterdir()), [])

    def test_interrupted_compaction_keeps_the_segment_and_finishes_later(self) -> None:
        path = self.write_log([log_line(day_with_complete_days(120), CLEAN), log_line(TODAY, CLEAN)])
        original = path.read_bytes()
        self.crash_at("log:before-replace")
        self.crash("--apply")
        segments = self.home.retention / "egdaily-log"
        names = sorted(p.name for p in segments.iterdir())
        self.assertEqual(len(names), 2)
        segment = segments / next(name for name in names if not name.startswith("."))
        self.assertEqual(segment.read_bytes(), original, "the old segment is whole until the atomic replace")
        code, summary = self.home.main("--apply")
        self.assertEqual(code, 0, summary)
        self.assertEqual([p.name for p in segments.iterdir()], [segment.name])
        self.assertEqual(segment.read_bytes(), ret.BOM + log_line(TODAY, CLEAN))

    def test_fully_old_clean_segment_is_removed(self) -> None:
        self.write_log([log_line(day_with_complete_days(120), CLEAN)], bom=False)
        self.home.main("--apply")
        self.assertEqual(list((self.home.retention / "egdaily-log").iterdir()), [])

    def test_compaction_never_drops_an_unterminated_last_line(self) -> None:
        data, dropped = ret.compact_plan(log_line(day_with_complete_days(120), CLEAN).rstrip(b"\r\n"), TODAY, set())
        self.assertEqual(dropped, 0)


# ---------------------------------------------------------------------------------------------------
# Installed entry point, templates
# ---------------------------------------------------------------------------------------------------


class InstalledEntryPoint(RetentionCase):
    def test_script_derives_its_home_from_its_own_location(self) -> None:
        installed = self.home.root / "maintenance" / SCRIPT.name
        result = subprocess.run([sys.executable, "-I", str(installed)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout.strip())
        self.assertEqual((summary["mode"], summary["outcome"]), ("DRY_RUN", "COMPLETED"))
        self.assertEqual(len(list((self.home.retention / "reports").iterdir())), 1)

    def test_script_refuses_a_path_argument(self) -> None:
        installed = self.home.root / "maintenance" / SCRIPT.name
        result = subprocess.run([sys.executable, "-I", str(installed), str(self._tmp.name)], capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 64)
        self.assertEqual(json.loads(result.stdout)["refs"], ["EG_RET_ARGUMENTS_REFUSED"])

    def test_script_is_standard_library_only_and_never_names_production_paths(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("Program Files", "energygrid_bill_downloader", "shutil.rmtree", "import requests",
                          "subprocess", "urllib"):
            self.assertNotIn(forbidden, source)


class TaskTemplate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ET.fromstring(TASK.read_bytes())

    def value(self, path: str) -> str:
        element = self.root.find(path, NS)
        self.assertIsNotNone(element, path)
        return (element.text or "").strip()

    def test_disabled_dry_run_evening_task_with_no_catch_up(self) -> None:
        self.assertEqual(self.value("t:Settings/t:Enabled"), "false")
        self.assertEqual(self.value("t:Triggers/t:CalendarTrigger/t:Enabled"), "false")
        self.assertTrue(self.value("t:Triggers/t:CalendarTrigger/t:StartBoundary").endswith("T19:00:00+08:00"))
        self.assertEqual(self.value("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval"), "1")
        self.assertEqual(len(self.root.findall("t:Triggers/*", NS)), 1)
        self.assertEqual(self.value("t:Settings/t:StartWhenAvailable"), "false")
        self.assertEqual(self.value("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(self.value("t:Settings/t:ExecutionTimeLimit"), "PT1H")
        self.assertEqual(self.value("t:Settings/t:WakeToRun"), "false")
        self.assertEqual(self.value("t:Principals/t:Principal/t:RunLevel"), "LeastPrivilege")
        execs = self.root.findall("t:Actions/t:Exec", NS)
        self.assertEqual(len(execs), 1)
        self.assertEqual(self.value("t:Actions/t:Exec/t:Command"), POWERSHELL)
        arguments = self.value("t:Actions/t:Exec/t:Arguments")
        self.assertTrue(arguments.endswith('-File "REPLACE_WITH_EG_HOME\\maintenance\\egretention.ps1"'))
        self.assertNotIn("-Apply", arguments)

    def test_window_constants_match_the_schedule(self) -> None:
        self.assertTrue(ret.in_window(datetime(2026, 10, 20, 19, 0, tzinfo=SGT)))
        self.assertFalse(ret.in_window(datetime(2026, 10, 20, 11, 30, tzinfo=SGT)))
        self.assertFalse(ret.in_window(datetime(2026, 10, 20, 14, 0, tzinfo=SGT)))


ACL_PROBE = r'''
$ErrorActionPreference = 'Stop'
. ([scriptblock]::Create([System.IO.File]::ReadAllText($env:EG_ACL_FUNCTIONS)))
function Sid([string]$Value) { New-Object System.Security.Principal.SecurityIdentifier $Value }
function New-Sec([string]$Owner, [object[]]$Rules, [switch]$Directory, [switch]$Inherited) {
    if ($Directory) { $sec = New-Object System.Security.AccessControl.DirectorySecurity }
    else { $sec = New-Object System.Security.AccessControl.FileSecurity }
    $sec.SetAccessRuleProtection((-not $Inherited), $false)
    $sec.SetOwner((Sid $Owner))
    foreach ($rule in $Rules) {
        $inherit = 'None'; $propagate = 'None'
        if ($rule.Count -gt 2) { $inherit = 'ContainerInherit,ObjectInherit'; $propagate = $rule[2] }
        $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule((Sid $rule[0]), $rule[1], $inherit, $propagate, 'Allow')))
    }
    return $sec
}
$admins = 'S-1-5-32-544'; $system = 'S-1-5-18'; $users = 'S-1-5-32-545'; $auth = 'S-1-5-11'; $everyone = 'S-1-1-0'
$runner = 'S-1-5-21-1111111111-2222222222-3333333333-1001'; $creator = 'S-1-3-0'
$result = [ordered]@{
  protected_ok = Test-EgProtectedSecurity (New-Sec $admins @(@($system, 'FullControl'), @($admins, 'FullControl'), @($users, 'ReadAndExecute')))
  protected_creator_inherit_only_ok = Test-EgProtectedSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($creator, 'FullControl', 'InheritOnly')) -Directory)
  protected_creator_effective = Test-EgProtectedSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($creator, 'FullControl', 'None')) -Directory)
  protected_users_modify = Test-EgProtectedSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($users, 'Modify')))
  protected_auth_write = Test-EgProtectedSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($auth, 'Write')))
  protected_runner_delete = Test-EgProtectedSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($runner, 'Delete')))
  protected_users_inherit_only_write = Test-EgProtectedSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($users, 'Modify', 'InheritOnly')) -Directory)
  protected_runner_owner = Test-EgProtectedSecurity (New-Sec $runner (,@($admins, 'FullControl')))
  private_ok = Test-EgPrivateSecurity (New-Sec $admins @(@($system, 'FullControl'), @($admins, 'FullControl'), @($runner, 'Modify')) -Directory) $runner
  private_users_read = Test-EgPrivateSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($runner, 'Modify'), @($users, 'ReadAndExecute')) -Directory) $runner
  private_everyone_read = Test-EgPrivateSecurity (New-Sec $admins @(@($runner, 'Modify'), @($everyone, 'Read')) -Directory) $runner
  root_protected_ok = Test-EgPrivateSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($runner, 'Modify', 'None')) -Directory) $runner -RequireProtected
  root_inheriting = Test-EgPrivateSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($runner, 'Modify', 'None')) -Directory -Inherited) $runner -RequireProtected
  root_users_inherit_only_read = Test-EgPrivateSecurity (New-Sec $admins @(@($runner, 'Modify', 'None'), @($users, 'ReadAndExecute', 'InheritOnly')) -Directory) $runner -RequireProtected
  child_inheriting_ok = Test-EgPrivateSecurity (New-Sec $admins @(@($admins, 'FullControl'), @($runner, 'Modify')) -Directory -Inherited) $runner
}
$result | ConvertTo-Json -Compress
'''


@unittest.skipUnless(os.name == "nt" and Path(POWERSHELL).exists(), "Windows PowerShell 5.1")
class WrapperTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_template_parses_and_deletes_nothing_itself(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        self.assertEqual(source.count("{{EG_HOME}}"), 1)
        self.assertIn("$EgHome = '{{EG_HOME}}'", source)
        for forbidden in ("Remove-Item", "Set-Acl", "Move-Item", "Register-ScheduledTask", "schtasks"):
            self.assertNotIn(forbidden, source)
        command = ("$e=$null; [System.Management.Automation.Language.Parser]::ParseFile($env:EG_WRAPPER,[ref]$null,"
                   "[ref]$e) | Out-Null; if (@($e).Count -gt 0) { exit 1 }")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                                env={**os.environ, "EG_WRAPPER": str(WRAPPER)}, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_acl_rules_in_memory(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        start = source.index("# BEGIN EG RETENTION ACL FUNCTIONS")
        end = source.index("# END EG RETENTION ACL FUNCTIONS")
        functions = self.tmp / "acl_functions.ps1"
        functions.write_text(source[start:end], encoding="utf-8")
        probe = self.tmp / "probe.ps1"
        probe.write_text(ACL_PROBE, encoding="utf-8")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                 str(probe)], env={**os.environ, "EG_ACL_FUNCTIONS": str(functions)},
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        verdicts = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(verdicts, {
            "protected_ok": True, "protected_creator_inherit_only_ok": True, "protected_creator_effective": False,
            "protected_users_modify": False, "protected_auth_write": False, "protected_runner_delete": False,
            "protected_users_inherit_only_write": False, "protected_runner_owner": False,
            "private_ok": True, "private_users_read": False, "private_everyone_read": False,
            "root_protected_ok": True, "root_inheriting": False, "root_users_inherit_only_read": False,
            "child_inheriting_ok": True,
        })

    def test_rendered_wrapper_refuses_a_user_writable_install_before_python(self) -> None:
        home = self.tmp / "EnergyGrid"
        (home / "maintenance").mkdir(parents=True)
        (home / "data" / "runs").mkdir(parents=True)
        shutil.copy2(SCRIPT, home / "maintenance" / SCRIPT.name)
        wrapper = home / "maintenance" / "egretention.ps1"
        wrapper.write_text(WRAPPER.read_text(encoding="utf-8").replace("{{EG_HOME}}", str(home)), encoding="utf-8")
        # A Security module that cannot load, first on the module path: this is what Windows PowerShell meets
        # when started from PowerShell 7 (CI's pwsh steps). The ACL check must not depend on that module.
        module = self.tmp / "modules" / "Microsoft.PowerShell.Security"
        module.mkdir(parents=True)
        (module / "Microsoft.PowerShell.Security.psm1").write_text("throw 'incompatible module'\n", encoding="utf-8")
        (module / "Microsoft.PowerShell.Security.psd1").write_text(
            "@{ ModuleVersion='9.9.9'; RootModule='Microsoft.PowerShell.Security.psm1'; FunctionsToExport=@('Get-Acl') }\n",
            encoding="utf-8")
        env = {**os.environ, "PSModulePath": str(self.tmp / "modules") + os.pathsep + os.environ.get("PSModulePath", "")}
        for extra in ([], ["-Apply"], ["unexpected"]):
            with self.subTest(extra=extra):
                result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                         "-File", str(wrapper), *extra], capture_output=True, text=True, timeout=120,
                                        env=env)
                self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
                ref = json.loads(result.stdout.strip().splitlines()[-1])["refs"][0]
                expected = "EG_RET_ARGUMENTS_REFUSED" if extra == ["unexpected"] else "EG_RET_ACL_NOT_PROTECTED"
                self.assertEqual(ref, expected)
        self.assertFalse((home / "data" / "retention").exists(), "Python never ran")

    def test_wrapper_children_match_the_tool_folders(self) -> None:
        source = WRAPPER.read_text(encoding="utf-8")
        layout = ret.Layout(Path("X:/EnergyGrid"))
        for folder in layout.own_dirs()[1:]:
            self.assertIn(f"'{folder.name}'", source)
        self.assertEqual(layout.own_dirs()[0], layout.retention)

    # -- Finding 4: the private retention folder, natively ------------------------------------------

    def retention_home(self, label: str) -> tuple[Path, Path]:
        """A scratch install whose rendered wrapper skips only the admin-ownership loop (a temp folder can
        never be admin-owned) and runs this interpreter. The retention gate under test is unchanged."""
        home = self.tmp / label / "EnergyGrid"
        (home / "maintenance").mkdir(parents=True)
        (home / "data" / "runs").mkdir(parents=True)
        shutil.copy2(SCRIPT, home / "maintenance" / SCRIPT.name)
        source = WRAPPER.read_text(encoding="utf-8").replace("{{EG_HOME}}", str(home))
        for old, new in (("foreach ($item in $protected) {", "foreach ($item in @()) {"),
                         ("$Python = Join-Path $EgHome 'runtime\\venv\\Scripts\\python.exe'",
                          f"$Python = '{sys.executable}'")):
            self.assertEqual(source.count(old), 1, old)
            source = source.replace(old, new)
        wrapper = home / "maintenance" / "egretention.ps1"
        wrapper.write_text(source, encoding="utf-8")
        return home / "data" / "retention", wrapper

    def run_wrapper(self, wrapper: Path) -> tuple[int, dict]:
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                 str(wrapper)], capture_output=True, text=True, timeout=180)
        return result.returncode, json.loads(result.stdout.strip().splitlines()[-1])

    @staticmethod
    def icacls(path: Path, *args: str) -> None:
        subprocess.run(["icacls", str(path), *args], check=True, capture_output=True, timeout=60)

    @staticmethod
    def current_sid() -> str:
        output = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], check=True, capture_output=True,
                                text=True, timeout=60).stdout
        return output.strip().split(",")[-1].strip('"')

    def private_grants(self) -> list[str]:
        # The documented install: inheritance broken, inheritable grants for SYSTEM, Administrators and the
        # task principal only (docs section 6).
        return ["/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                f"*{self.current_sid()}:(OI)(CI)M"]

    def sddl(self, path: Path) -> str:
        command = ("$p=$env:EG_PATH; if ([System.IO.Directory]::Exists($p)) { $s=[System.IO.Directory]::"
                   "GetAccessControl($p) } else { $s=[System.IO.File]::GetAccessControl($p) }; "
                   "$s.GetSecurityDescriptorSddlForm('Access')")
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command], check=True,
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "EG_PATH": str(path)}).stdout.strip()

    def assert_refused_before_python(self, wrapper: Path, retention: Path, ref: str, present: set[str]) -> None:
        code, summary = self.run_wrapper(wrapper)
        self.assertEqual((code, summary["refs"]), (64, [ref]), summary)
        found = {entry.name for entry in retention.iterdir()} if retention.exists() else set()
        self.assertEqual(found, present, "Python never ran: no lock, report or tool folder was created")

    def test_missing_retention_folder_is_refused_before_python(self) -> None:
        retention, wrapper = self.retention_home("missing")
        self.assert_refused_before_python(wrapper, retention, "EG_RET_RETENTION_MISSING", set())
        self.assertFalse(retention.exists())

    def test_inherited_or_broadly_readable_retention_folder_is_refused_before_python(self) -> None:
        inherited, wrapper = self.retention_home("inherited")
        inherited.mkdir()  # plain mkdir: inherits whatever the parent grants, inheritance not broken
        self.assert_refused_before_python(wrapper, inherited, "EG_RET_RETENTION_NOT_PRIVATE", set())

        broad, wrapper = self.retention_home("users-read")
        broad.mkdir()
        self.icacls(broad, *self.private_grants(), "*S-1-5-32-545:(OI)(CI)RX")
        self.assert_refused_before_python(wrapper, broad, "EG_RET_RETENTION_NOT_PRIVATE", set())

        child, wrapper = self.retention_home("broad-child")
        child.mkdir()
        self.icacls(child, *self.private_grants())
        (child / "reports").mkdir()
        self.icacls(child / "reports", "/grant", "*S-1-1-0:(OI)(CI)R")
        self.assert_refused_before_python(wrapper, child, "EG_RET_RETENTION_NOT_PRIVATE", {"reports"})

    def test_unreadable_retention_acl_is_refused_before_python(self) -> None:
        retention, wrapper = self.retention_home("unreadable")
        retention.mkdir()
        # Only an OWNER RIGHTS entry without READ_CONTROL: the owner loses its implicit right to read the ACL.
        # DELETE and SYNCHRONIZE stay, so the empty folder can still be removed afterwards.
        self.icacls(retention, "/inheritance:r", "/grant:r", "*S-1-3-4:(D,S,WDAC)")
        try:
            code, summary = self.run_wrapper(wrapper)
            self.assertEqual((code, summary["refs"]), (64, ["EG_RET_ACL_UNREADABLE"]), summary)
        finally:
            os.rmdir(retention)
        self.assertFalse(retention.exists())

    def test_protected_retention_folder_runs_and_children_inherit_private(self) -> None:
        retention, wrapper = self.retention_home("protected")
        retention.mkdir()
        self.icacls(retention, *self.private_grants())
        code, summary = self.run_wrapper(wrapper)
        self.assertEqual((code, summary["mode"], summary["outcome"]), (0, "DRY_RUN", "COMPLETED"), summary)
        report = next((retention / "reports").iterdir())
        broad = (";;;BU)", ";;;WD)", ";;;AU)")  # SDDL aliases: Users, Everyone, Authenticated Users
        for path in (retention, retention / "reports", retention / "incident-archive", report):
            with self.subTest(path=path.name):
                text = self.sddl(path)
                self.assertTrue(text.startswith("D:"), text)
                self.assertFalse(any(alias in text for alias in broad), text)
        self.assertIn("D:P", self.sddl(retention), "inheritance broken on the root")
        # A second run now also checks the existing subfolders, and still passes.
        code, summary = self.run_wrapper(wrapper)
        self.assertEqual((code, summary["outcome"]), (0, "COMPLETED"), summary)


if __name__ == "__main__":
    unittest.main()
