"""Offline tests of the read-only daily health checker (#325 H1): settings, rules, pauses and
expiry, records, the one POST, privacy, and the installer dry run.

Nothing here reads Task Scheduler, Drive, Credential Manager or n8n: every reader and the poster are
replaced by fakes, and the installer runs only with -DryRun and a test-only task name. Fake data only.
"""

import copy
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import xb_daily_health as health  # noqa: E402

SCRIPT = ROOT / "scripts" / "xb_daily_health.py"
INSTALLER = ROOT / "scripts" / "install_daily_health_task.ps1"
EXAMPLE = ROOT / "config" / "daily_health.example.json"
RUNBOOK = ROOT / "docs" / "daily_health_check.md"
SGT = health.SGT
NOW = datetime(2026, 11, 19, 7, 30, tzinfo=SGT)  # a Thursday
SECRET = "FAKE-HEALTH-SECRET-0001"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def raw_settings(tmp, **overrides):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw.update(record_dir=str(Path(tmp) / "records"), drive_root=str(Path(tmp) / "drive"),
               stock_folder=str(Path(tmp) / "drive" / "stock"), arap_folder=str(Path(tmp) / "drive" / "arap"),
               mirror_log_dir=str(Path(tmp) / "mirror"))
    raw["tasks"] = {"energygrid_daily_bills": "Example Bills Task", "energygrid_retention": "Example Retention Task",
                    "drive_mirror": "XB Drive Mirror of main (weekly)", "checker": "XB-DailyHealth"}
    raw["send"] = dict(raw["send"], webhook_url="http://127.0.0.1:5678/webhook/test-health-path-0001",
                       secret_credential_target="XB test health target")
    raw.update(overrides)
    return raw


def settings(tmp="C:\\fake-root", **overrides):
    return health.parse_settings(raw_settings(tmp, **overrides), NOW)


def task(name, state="Ready", last_run="2026-11-18T11:30", last_result=0):
    return {"name": name, "state": state, "last_run": last_run, "last_result": last_result}


def healthy_obs():
    return {
        "tasks": [task("Example Bills Task"), task("Example Retention Task", last_run="2026-11-18T19:30"),
                  task("XB Drive Mirror of main (weekly)", last_run="2026-11-15T03:00"),
                  task("XB-DailyHealth", state="Running", last_run="2026-11-19T07:30", last_result=0x41301)],
        "stock_status": {"status": "success", "started_at": "2026-11-19T07:00:05+08:00",
                         "finished_at": "2026-11-19T07:03:10+08:00", "delivery_status": "handed_to_drive_sync_folder",
                         "daily_close_status": "not_due", "analysis_failure_class": None, "alert_status": "no_alert"},
        "stock_manifest": {"finished_at": "2026-11-19T07:03:09+08:00"},
        "arap_status": {"status": "success", "started_at": "2026-11-19T06:30:04+08:00",
                        "finished_at": "2026-11-19T06:31:00+08:00", "delivery_status": "handed_to_drive_sync_folder"},
        "arap_manifest": {"finished_at": "2026-11-19T06:31:00+08:00"},
        "mirror_status": {"status": "ok"},
        "drive_root_ok": True,
    }


def record(day, verdicts, send_status="posted"):
    jobs = [{"job": job, "verdict": verdict} for job, verdict in verdicts.items()]
    return {"schema": health.RECORD_SCHEMA, "date": day, "event": {"jobs": jobs}, "send_status": send_status}


def entry(event, job):
    return next(e for e in event["jobs"] if e["job"] == job)


def judge(obs=None, records=None, now=NOW, **overrides):
    return health.judge(settings(**overrides), obs or healthy_obs(), records or {}, now)


class SettingsTests(unittest.TestCase):
    def test_example_is_complete_but_refused_until_its_placeholders_are_set(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(set(raw) - {"_comment"}, health.SETTINGS_KEYS)
        self.assertEqual(set(raw["expected"]), set(health.JOBS))
        self.assertEqual(set(raw["thresholds"]), set(health.THRESHOLDS))
        for name, value in raw["thresholds"].items():
            self.assertEqual(value, health.THRESHOLDS[name][0], name)
        # Brief, 18 Nov 2026: the AR/AP hourly task is registered, so V6 starts `on`.
        self.assertEqual(raw["expected"]["arap_export"], "on")
        for job in ("member_sync_summary", "member_tags", "simba_intake"):
            self.assertEqual(raw["expected"][job], "not_installed", job)
        with self.assertRaises(health.SettingsError) as caught:
            health.parse_settings(raw, NOW)
        self.assertEqual(caught.exception.code, "settings_drive_root_invalid")

    def test_fake_complete_settings_are_accepted(self):
        parsed = settings()
        self.assertEqual(parsed["thresholds"]["stock_fail_age"], 180)
        self.assertEqual(parsed["send"]["timeout_seconds"], 10)

    def test_every_broken_value_has_its_own_code(self):
        def bad(code, mutate):
            raw = raw_settings("C:\\fake-root")
            mutate(raw)
            with self.subTest(code=code):
                with self.assertRaises(health.SettingsError) as caught:
                    health.parse_settings(raw, NOW)
                self.assertEqual(caught.exception.code, code)

        bad("settings_keys", lambda r: r.update(extra=1))
        bad("settings_schema", lambda r: r.update(schema="xb.daily_health.settings.v0"))
        bad("settings_record_dir_invalid", lambda r: r.update(record_dir="records"))
        bad("settings_record_dir_invalid", lambda r: r.update(record_dir="\\\\server\\share\\x"))
        bad("settings_task_path_invalid", lambda r: r.update(task_path="\\XB'; Stop-Computer; '\\"))
        bad("settings_tasks_keys", lambda r: r["tasks"].pop("checker"))
        bad("settings_task_name_invalid", lambda r: r["tasks"].update(checker="REPLACE-WITH-X"))
        bad("settings_known_tasks_invalid", lambda r: r.update(known_tasks=["bad;name"]))
        bad("settings_expected_keys", lambda r: r["expected"].pop("checker"))
        bad("settings_expected_value", lambda r: r["expected"].update(checker="maybe"))
        bad("settings_thresholds_keys", lambda r: r["thresholds"].update(unknown=1))
        bad("settings_threshold_invalid", lambda r: r["thresholds"].update(stock_warn_age=5))
        bad("settings_threshold_order", lambda r: r["thresholds"].update(stock_warn_age=200, stock_fail_age=180))
        bad("settings_pause_job", lambda r: r.update(pauses=[{"job": "x", "until": "2026-11-20T18:00",
                                                              "reason_code": "sitting"}]))
        bad("settings_pause_reason", lambda r: r.update(pauses=[{"job": "checker", "until": "2026-11-20T18:00",
                                                                 "reason_code": "because"}]))
        bad("settings_pause_until", lambda r: r.update(pauses=[{"job": "checker", "until": "2026-11-20 18:00",
                                                                "reason_code": "sitting"}]))
        bad("settings_pause_too_long", lambda r: r.update(pauses=[{"job": "checker", "until": "2026-11-26T08:00",
                                                                   "reason_code": "sitting"}]))
        # The secret travels in the request: the webhook must be loopback, plain http, /webhook/ only.
        for url in ("https://n8n.example.com/webhook/test-health-path-0001",
                    "http://192.168.1.20:5678/webhook/test-health-path-0001",
                    "http://127.0.0.1:5678/webhook-test/test-health-path-0001",
                    "http://127.0.0.1:5678/webhook/REPLACE-WITH-PRIVATE-PATH",
                    "http://127.0.0.1.example.com:5678/webhook/test-health-path-0001"):
            bad("settings_send_url_invalid", lambda r, url=url: r["send"].update(webhook_url=url))
        bad("settings_send_header_invalid", lambda r: r["send"].update(auth_header_name="Content-Type"))
        bad("settings_send_target_invalid", lambda r: r["send"].update(secret_credential_target="REPLACE-WITH-X"))
        bad("settings_send_timeout_invalid", lambda r: r["send"].update(timeout_seconds=30))

    def test_a_pause_up_to_seven_days_ahead_is_accepted(self):
        parsed = settings(pauses=[{"job": "checker", "until": "2026-11-26T07:30", "reason_code": "vm_work"}])
        self.assertEqual(parsed["pauses"][0]["until"], datetime(2026, 11, 26, 7, 30, tzinfo=SGT))


class RuleTests(unittest.TestCase):
    def test_a_healthy_morning_is_all_ok_and_n8n_jobs_are_left_to_n8n(self):
        event = judge()
        self.assertEqual(event["schema"], "xb.daily_health.v1")
        self.assertEqual((event["date"], event["checked_at"]), ("2026-11-19", "2026-11-19T07:30+08:00"))
        self.assertEqual([e["job"] for e in event["jobs"]], list(health.JOBS))
        self.assertEqual(event["notes"], [])
        for e in event["jobs"]:
            with self.subTest(job=e["job"]):
                if e["job"] in ("member_sync_summary", "member_tags", "simba_intake"):
                    self.assertEqual((e["state"], e["verdict"]), ("not_installed", "NA"))
                elif e["job"] == "n8n_workflows":
                    self.assertEqual((e["state"], e["verdict"]), ("on", "N8N"))
                else:
                    self.assertEqual((e["state"], e["verdict"], e["reasons"], e["since"]), ("on", "OK", [], None))
        self.assertEqual(health.worst(event), "OK")

    def stock(self, **changes):
        obs = healthy_obs()
        obs["stock_status"] = dict(obs["stock_status"], **changes)
        return entry(judge(obs), "ac2_stock_export")

    def test_stock_export_rules(self):
        cases = [
            ({"finished_at": "2026-11-19T06:15:00+08:00"}, "WARN", ["last_run_old"]),
            ({"finished_at": "2026-11-19T04:29:00+08:00"}, "FAIL", ["last_run_too_old"]),
            ({"delivery_status": "verify_failed"}, "WARN", ["delivery_failed"]),
            ({"delivery_status": "copy_failed"}, "WARN", ["delivery_failed"]),
            ({"daily_close_status": "failed"}, "WARN", ["daily_close_failed"]),
            ({"analysis_failure_class": "purchasing_read_failed"}, "WARN", ["analysis_failed"]),
            ({"started_at": "2026-11-19T06:55:00+08:00"}, "WARN", ["run_slow"]),  # 8 min 10 s
            ({"started_at": "2026-11-19T06:50:00+08:00"}, "FAIL", ["run_too_slow"]),  # 13 min 10 s
            ({"status": "failed", "finished_at": None, "started_at": "2026-11-19T07:00:05+08:00"}, "WARN", ["run_failed"]),
        ]
        for changes, verdict, reasons in cases:
            with self.subTest(changes=changes):
                e = self.stock(**changes)
                self.assertEqual((e["verdict"], e["reasons"]), (verdict, reasons))
                self.assertEqual(e["since"], "2026-11-19")
        e = self.stock(finished_at="2026-11-18T23:30:00+08:00", started_at="2026-11-18T23:27:00+08:00")
        self.assertEqual((e["verdict"], e["reasons"], e["age_min"]), ("FAIL", ["last_run_too_old"], 480))

    def test_stock_failed_run_depends_on_the_last_good_manifest(self):
        obs = healthy_obs()
        obs["stock_status"] = dict(obs["stock_status"], status="failed")
        obs["stock_manifest"] = {"finished_at": "2026-11-19T04:00:00+08:00"}
        self.assertEqual(entry(judge(obs), "ac2_stock_export")["reasons"], ["manifest_too_old"])
        obs["stock_manifest"] = None
        self.assertEqual(entry(judge(obs), "ac2_stock_export")["reasons"], ["manifest_missing"])
        obs["stock_status"] = None
        e = entry(judge(obs), "ac2_stock_export")
        self.assertEqual((e["verdict"], e["reasons"]), ("FAIL", ["status_missing"]))
        # A missing file is the export's FAIL, never a second line for the alert route.
        self.assertEqual(entry(judge(obs), "stock_alert_route")["verdict"], "OK")

    def test_fail_reasons_come_first_and_at_most_six_are_sent(self):
        e = self.stock(finished_at="2026-11-19T03:00:00+08:00", started_at="2026-11-19T02:40:00+08:00",
                       delivery_status="copy_failed", daily_close_status="failed",
                       analysis_failure_class="x", status="failed")
        self.assertEqual(e["verdict"], "FAIL")
        self.assertEqual(e["reasons"][:2], ["last_run_too_old", "run_too_slow"])
        self.assertLessEqual(len(e["reasons"]), 6)
        levels = health.REASONS["ac2_stock_export"]
        seen_warn = False
        for reason in e["reasons"]:
            seen_warn = seen_warn or levels[reason] == "WARN"
            self.assertFalse(seen_warn and levels[reason] == "FAIL")

    def test_alert_route_rules(self):
        for status, reason in (("send_failed", "alert_send_failed"), ("secret_unavailable", "alert_secret_unavailable"),
                               ("error", "alert_error")):
            obs = healthy_obs()
            obs["stock_status"]["alert_status"] = status
            e = entry(judge(obs), "stock_alert_route")
            self.assertEqual((e["verdict"], e["reasons"]), ("WARN", [reason]))
        for status in ("no_alert", "sent", "suppressed", "disabled", None):
            obs = healthy_obs()
            obs["stock_status"]["alert_status"] = status
            self.assertEqual(entry(judge(obs), "stock_alert_route")["verdict"], "OK", status)

    def test_arap_export_rules(self):
        cases = [
            ({"finished_at": "2026-11-19T05:50:00+08:00"}, "WARN", ["last_run_old"]),
            ({"finished_at": "2026-11-19T04:20:00+08:00"}, "FAIL", ["last_run_too_old"]),
            ({"status": "failed"}, "WARN", ["run_failed"]),
            ({"delivery_status": "copy_failed"}, "WARN", ["delivery_failed"]),
            # Stock-only rules never apply to AR/AP.
            ({"daily_close_status": "failed", "analysis_failure_class": "x", "started_at": "2026-11-19T06:00:00+08:00"},
             "OK", []),
        ]
        for changes, verdict, reasons in cases:
            obs = healthy_obs()
            obs["arap_status"] = dict(obs["arap_status"], **changes)
            e = entry(judge(obs), "arap_export")
            self.assertEqual((e["verdict"], e["reasons"]), (verdict, reasons), changes)

    def task_entry(self, job, name, **changes):
        obs = healthy_obs()
        obs["tasks"] = [dict(t, **changes) if t["name"] == name else t for t in obs["tasks"]]
        return entry(judge(obs), job)

    def test_energygrid_tasks_are_liveness_only(self):
        bills = "Example Bills Task"
        cases = [
            ({"last_result": 1}, "WARN", ["task_result_nonzero"], 1),
            ({"last_run": "2026-11-18T05:00"}, "WARN", ["last_run_old"], None),
            ({"last_run": "2026-11-17T05:00"}, "FAIL", ["last_run_too_old"], None),
            ({"state": "Disabled"}, "FAIL", ["task_disabled"], None),
            ({"last_run": "1999-11-30T00:00", "last_result": 0x41303}, "FAIL", ["last_run_too_old"], None),
            ({"last_result": 0x41301}, "OK", [], None),
            ({"last_result": 0x8007052E}, "WARN", ["task_result_nonzero"], 0x8007052E),
        ]
        for changes, verdict, reasons, exit_code in cases:
            e = self.task_entry("energygrid_daily_bills", bills, **changes)
            self.assertEqual((e["verdict"], e["reasons"], e["exit"]), (verdict, reasons, exit_code), changes)
        retention = "Example Retention Task"
        for result, reason in ((20, "retention_held"), (64, "retention_refused"), (5, "task_result_nonzero")):
            e = self.task_entry("energygrid_retention", retention, last_result=result)
            self.assertEqual((e["verdict"], e["reasons"], e["exit"]), ("WARN", [reason], result))
        obs = healthy_obs()
        obs["tasks"] = [t for t in obs["tasks"] if t["name"] != bills]
        self.assertEqual(entry(judge(obs), "energygrid_daily_bills")["reasons"], ["task_missing"])

    def test_drive_mirror_rules(self):
        name = "XB Drive Mirror of main (weekly)"
        for result, verdict, reason in ((4, "WARN", "mirror_fetch_failed"), (7, "WARN", "mirror_locked"),
                                        (1, "FAIL", "mirror_failed"), (2, "FAIL", "mirror_failed"),
                                        (3, "FAIL", "mirror_failed"), (5, "FAIL", "mirror_failed"),
                                        (6, "FAIL", "mirror_failed"), (9, "WARN", "task_result_nonzero")):
            e = self.task_entry("drive_mirror", name, last_result=result)
            self.assertEqual((e["verdict"], e["reasons"], e["exit"]), (verdict, [reason], result), result)
        e = self.task_entry("drive_mirror", name, last_run="2026-11-10T03:00")
        self.assertEqual(e["reasons"], ["last_run_old"])
        e = self.task_entry("drive_mirror", name, last_run="2026-11-03T03:00")
        self.assertEqual(e["reasons"], ["last_run_too_old"])
        obs = healthy_obs()
        obs["mirror_status"] = {"status": "running"}
        self.assertEqual(entry(judge(obs), "drive_mirror")["reasons"], ["mirror_stuck_running"])
        obs["tasks"] = [dict(t, state="Running") if t["name"] == name else t for t in obs["tasks"]]
        self.assertEqual(entry(judge(obs), "drive_mirror")["verdict"], "OK")

    def test_task_scheduler_unreadable_is_a_warning_on_every_task_job(self):
        obs = healthy_obs()
        obs["tasks"] = None
        event = judge(obs)
        for job in ("energygrid_daily_bills", "energygrid_retention", "drive_mirror", "server_tasks"):
            self.assertEqual((entry(event, job)["verdict"], entry(event, job)["reasons"]), ("WARN", ["task_query_failed"]))

    def test_unknown_server_task_is_counted_never_named(self):
        obs = healthy_obs()
        obs["tasks"] += [task("Fake New Job A"), task("Fake New Job B"), task("Known Old Job")]
        event = judge(obs, known_tasks=["known old job"])
        e = entry(event, "server_tasks")
        self.assertEqual((e["verdict"], e["reasons"], e["count"]), ("WARN", ["unknown_task"], 2))
        self.assertNotIn("Fake New Job", json.dumps(event))

    def test_drive_folder_and_checker(self):
        obs = healthy_obs()
        obs["drive_root_ok"] = False
        self.assertEqual(entry(judge(obs), "drive_folder")["reasons"], ["folder_missing"])
        records = {"2026-11-18": record("2026-11-18", {}, send_status="failed")}
        e = entry(judge(records=records), "checker")
        self.assertEqual((e["verdict"], e["reasons"]), ("WARN", ["previous_send_failed"]))
        for status in ("posted", "not_sent", "disabled"):
            records = {"2026-11-18": record("2026-11-18", {}, send_status=status)}
            self.assertEqual(entry(judge(records=records), "checker")["verdict"], "OK", status)
        # Only yesterday counts: a failure two days ago is old news.
        records = {"2026-11-17": record("2026-11-17", {}, send_status="failed")}
        self.assertEqual(entry(judge(records=records), "checker")["verdict"], "OK")

    def test_vm_or_drive_down_note(self):
        obs = healthy_obs()
        obs["stock_status"]["finished_at"] = "2026-11-18T23:30:00+08:00"
        obs["arap_status"]["finished_at"] = "2026-11-18T23:01:00+08:00"
        self.assertEqual(judge(obs)["notes"], ["vm_or_drive_down"])
        obs["arap_status"]["finished_at"] = "2026-11-18T20:00:00+08:00"  # 3.5 h apart: not "similar"
        self.assertEqual(judge(obs)["notes"], [])
        obs["arap_status"]["finished_at"] = "2026-11-19T06:31:00+08:00"  # AR/AP fresh
        self.assertEqual(judge(obs)["notes"], [])
        obs["arap_status"]["finished_at"] = "2026-11-18T23:01:00+08:00"
        expected = dict(raw_settings("C:\\fake-root")["expected"], arap_export="not_installed")
        self.assertEqual(judge(obs, expected=expected)["notes"], [])

    def test_since_comes_from_consecutive_own_records(self):
        obs = healthy_obs()
        obs["tasks"] = [dict(t, last_result=4) if t["name"].startswith("XB Drive Mirror") else t for t in obs["tasks"]]
        records = {d: record(d, {"drive_mirror": v}) for d, v in
                   (("2026-11-18", "WARN"), ("2026-11-17", "FAIL"), ("2026-11-16", "WARN"), ("2026-11-14", "WARN"))}
        self.assertEqual(entry(judge(obs, records), "drive_mirror")["since"], "2026-11-16")
        records["2026-11-17"] = record("2026-11-17", {"drive_mirror": "OK"})
        self.assertEqual(entry(judge(obs, records), "drive_mirror")["since"], "2026-11-18")
        self.assertEqual(entry(judge(obs, {}), "drive_mirror")["since"], "2026-11-19")


class PauseTests(unittest.TestCase):
    def broken_stock(self):
        obs = healthy_obs()
        obs["stock_status"]["finished_at"] = "2026-11-18T20:00:00+08:00"
        return obs

    def test_an_active_pause_is_one_info_state_never_a_warning(self):
        pauses = [{"job": "ac2_stock_export", "until": "2026-11-20T18:00", "reason_code": "sitting"}]
        e = entry(judge(self.broken_stock(), pauses=pauses), "ac2_stock_export")
        self.assertEqual((e["state"], e["verdict"], e["reasons"]), ("paused", "NA", []))
        self.assertEqual((e["until"], e["pause_reason"], e["since"]), ("2026-11-20T18:00", "sitting", None))
        # Two pauses: the later end is shown.
        pauses.append({"job": "ac2_stock_export", "until": "2026-11-21T09:00", "reason_code": "vm_work"})
        e = entry(judge(self.broken_stock(), pauses=pauses), "ac2_stock_export")
        self.assertEqual((e["until"], e["pause_reason"]), ("2026-11-21T09:00", "vm_work"))
        # n8n-judged jobs can be paused too.
        expected = dict(raw_settings("C:\\fake-root")["expected"], member_sync_summary="on")
        e = entry(judge(expected=expected, pauses=[{"job": "member_sync_summary", "until": "2026-11-19T12:00",
                                                    "reason_code": "maintenance"}]), "member_sync_summary")
        self.assertEqual((e["state"], e["verdict"]), ("paused", "NA"))

    def test_an_expired_pause_on_a_job_that_is_still_down_is_a_fail(self):
        pauses = [{"job": "ac2_stock_export", "until": "2026-11-19T06:00", "reason_code": "sitting"}]
        e = entry(judge(self.broken_stock(), pauses=pauses), "ac2_stock_export")
        self.assertEqual((e["state"], e["verdict"]), ("on", "FAIL"))
        self.assertEqual(e["reasons"], ["pause_expired", "last_run_too_old"])
        # A WARN job with an expired pause is a FAIL too.
        obs = healthy_obs()
        obs["stock_status"]["delivery_status"] = "copy_failed"
        e = entry(judge(obs, pauses=pauses), "ac2_stock_export")
        self.assertEqual((e["verdict"], e["reasons"]), ("FAIL", ["pause_expired", "delivery_failed"]))

    def test_an_expired_pause_on_a_healthy_job_or_an_old_pause_changes_nothing(self):
        pauses = [{"job": "ac2_stock_export", "until": "2026-11-19T06:00", "reason_code": "sitting"}]
        self.assertEqual(entry(judge(pauses=pauses), "ac2_stock_export")["verdict"], "OK")
        old = [{"job": "ac2_stock_export", "until": "2026-11-11T06:00", "reason_code": "sitting"}]
        e = entry(judge(self.broken_stock(), pauses=old), "ac2_stock_export")
        self.assertEqual(e["reasons"], ["last_run_too_old"])

    def test_off_and_not_installed_jobs_are_never_judged(self):
        expected = {job: "off" for job in health.JOBS}
        expected["ac2_stock_export"] = "not_installed"
        obs = self.broken_stock()
        obs["tasks"] = None
        event = judge(obs, expected=expected)
        for e in event["jobs"]:
            self.assertEqual(e["verdict"], "NA", e["job"])
            self.assertEqual(e["reasons"], [], e["job"])
        self.assertEqual(health.worst(event), "OK")


class PrivacyTests(unittest.TestCase):
    EVENT_KEYS = {"schema", "date", "checked_at", "jobs", "notes"}
    ENTRY_KEYS = {"job", "state", "verdict", "reasons", "since", "until", "pause_reason", "age_min", "exit", "count"}

    def worst_case_event(self):
        obs = healthy_obs()
        obs["tasks"] = [task("Example Bills Task", state="Disabled", last_result=0x8007052E),
                        task("Fake Secret-Named Task 0001")]
        obs["stock_status"] = dict(obs["stock_status"], status="failed", alert_status="send_failed",
                                   failure_class="ledger_mismatch", hourly_folder="C:\\private\\fake\\folder",
                                   message="FAKE free text that must never travel")
        obs["mirror_status"] = {"status": "running", "message": "FAKE free text", "commit": "f" * 40}
        obs["drive_root_ok"] = False
        pauses = [{"job": "arap_export", "until": "2026-11-20T18:00", "reason_code": "sitting"}]
        return judge(obs, pauses=pauses)

    def test_the_event_is_closed_codes_counts_and_dates_only(self):
        event = self.worst_case_event()
        self.assertEqual(set(event), self.EVENT_KEYS)
        text = json.dumps(event)
        for leak in ("Example Bills Task", "Secret-Named", "private", "FAKE free text", "ledger_mismatch", "f" * 40,
                     "XB Drive Mirror", "XB-DailyHealth", "C:\\\\", "fake-root"):
            self.assertNotIn(leak, text)
        self.assertEqual(EMAIL_RE.findall(text), [])
        for e in event["jobs"]:
            self.assertEqual(set(e), self.ENTRY_KEYS)
            self.assertIn(e["job"], health.JOBS)
            for reason in e["reasons"]:
                self.assertIn(reason, health.REASONS[e["job"]])
            for key in ("age_min", "exit", "count"):
                self.assertTrue(e[key] is None or (isinstance(e[key], int) and e[key] >= 0), (e["job"], key))
            for key in ("since", "until"):
                self.assertTrue(e[key] is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2})?", e[key]), e[key])
        self.assertRegex(text, r"^[\x20-\x7E]+$")

    def test_the_task_query_is_read_only_and_cannot_be_injected(self):
        script = health.task_query_script("\\XB\\")
        cmdlets = set(re.findall(r"\b([A-Z][A-Za-z]+-[A-Z][A-Za-z]+)\b", script))
        self.assertEqual(cmdlets, {"Get-ScheduledTask", "Get-ScheduledTaskInfo", "ForEach-Object", "ConvertTo-Json"})
        for verb in ("Set-", "Start-", "Stop-", "Enable-", "Disable-", "Register-", "Unregister-", "Remove-", "New-"):
            self.assertNotIn(verb, script)
        for bad in ("\\XB'; Stop-Computer; '\\", "XB", "\\XB", "\\..\\"):
            with self.assertRaises(ValueError):
                health.task_query_script(bad)

    def test_task_reader_parses_one_or_many_rows_and_fails_closed(self):
        one = json.dumps({"name": "A", "state": "Ready", "last_run": "2026-11-18T11:30", "last_result": 0})
        self.assertEqual(len(health.read_tasks("\\XB\\", lambda s, t: (0, one))), 1)
        many = json.dumps([json.loads(one), dict(json.loads(one), name="B", last_run=None)])
        self.assertEqual([t["name"] for t in health.read_tasks("\\XB\\", lambda s, t: (0, many))], ["A", "B"])
        self.assertEqual(health.read_tasks("\\XB\\", lambda s, t: (0, "[]")), [])
        for runner in (lambda s, t: (1, one), lambda s, t: (0, "not json"), lambda s, t: (0, '"x"'),
                       lambda s, t: (0, '[{"state": "Ready"}]')):
            self.assertIsNone(health.read_tasks("\\XB\\", runner))

        def boom(script, timeout):
            raise subprocess.TimeoutExpired("powershell", timeout)

        self.assertIsNone(health.read_tasks("\\XB\\", boom))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.settings = health.parse_settings(raw_settings(self.tmp), NOW)
        self.posts = []

    def poster(self, status=200, error=None):
        def post(url, headers, body, timeout):
            self.posts.append((url, dict(headers), body, timeout))
            if error:
                raise error
            return status
        return post

    def run_check(self, poster=None, cred=lambda target: SECRET, no_send=False, obs=None):
        return health.run(self.settings, NOW, no_send=no_send, observer=lambda s: obs or healthy_obs(),
                          cred_reader=cred, poster=poster or self.poster())

    def test_one_post_to_the_loopback_hook_and_a_record_without_the_secret(self):
        result = self.run_check()
        self.assertEqual((result["send_status"], result["http_status"], result["send_error"]), ("posted", 200, None))
        (url, headers, body, timeout), = self.posts
        self.assertEqual(url, "http://127.0.0.1:5678/webhook/test-health-path-0001")
        self.assertEqual(headers["X-XB-Health-Auth"], SECRET)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(timeout, 10)
        self.assertEqual(json.loads(body), result["event"])
        path = Path(self.tmp) / "records" / "health_2026-11-19.json"
        saved = path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, saved)
        self.assertEqual(json.loads(saved)["send_status"], "posted")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["health_2026-11-19.json"])

    def test_every_send_failure_is_recorded_and_never_raised(self):
        cases = [
            (dict(poster=self.poster(500)), ("failed", 500, "http_status")),
            (dict(poster=self.poster(400)), ("failed", 400, "http_status")),
            (dict(poster=self.poster(error=OSError("refused"))), ("failed", None, "no_answer")),
            (dict(poster=self.poster(error=ValueError("odd"))), ("failed", None, "post_error")),
            (dict(cred=lambda t: (_ for _ in ()).throw(health.CredentialUnavailable())), ("failed", None, "secret_unavailable")),
            (dict(no_send=True), ("not_sent", None, None)),
        ]
        for kwargs, expected in cases:
            result = self.run_check(**kwargs)
            self.assertEqual((result["send_status"], result["http_status"], result["send_error"]), expected, kwargs)
        self.settings["send"]["enabled"] = False
        self.assertEqual(self.run_check()["send_status"], "disabled")

    def test_no_send_posts_nothing(self):
        self.run_check(no_send=True)
        self.assertEqual(self.posts, [])

    def test_yesterdays_failed_post_shows_today(self):
        records = Path(self.tmp) / "records"
        records.mkdir()
        (records / "health_2026-11-18.json").write_text(json.dumps(
            {"schema": health.RECORD_SCHEMA, "date": "2026-11-18", "event": {"jobs": []}, "send_status": "failed"}),
            encoding="utf-8")
        # A record whose content claims another date is ignored.
        (records / "health_2026-11-17.json").write_text(json.dumps(
            {"schema": health.RECORD_SCHEMA, "date": "2026-11-01", "event": {"jobs": []}, "send_status": "failed"}),
            encoding="utf-8")
        result = self.run_check()
        self.assertEqual(entry(result["event"], "checker")["reasons"], ["previous_send_failed"])
        self.assertEqual(set(health.read_records(records, date(2026, 11, 19))), {"2026-11-18"})

    def test_main_check_settings_and_run(self):
        config = Path(self.tmp) / "daily_health.local.json"
        config.write_text(json.dumps(raw_settings(self.tmp)), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = health.main(["check-settings", "--config", str(config)], now=NOW)
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"ok": True}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = health.main(["run", "--config", str(config)], now=NOW, observer=lambda s: healthy_obs(),
                               cred_reader=lambda t: SECRET, poster=self.poster())
        self.assertEqual(code, 0)
        line = json.loads(out.getvalue())
        self.assertEqual((line["date"], line["worst"], line["send_status"]), ("2026-11-19", "OK", "posted"))
        self.assertNotIn(SECRET, out.getvalue())
        # Broken settings: exit 2 with a code, and nothing is posted or written.
        self.posts.clear()
        config.write_text(json.dumps(raw_settings(self.tmp, schema="nope")), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = health.main(["run", "--config", str(config)], now=NOW, poster=self.poster())
        self.assertEqual((code, json.loads(out.getvalue())), (2, {"ok": False, "code": "settings_schema"}))
        self.assertEqual(self.posts, [])
        out = io.StringIO()
        with redirect_stdout(out):
            code = health.main(["check-settings", "--config", str(Path(self.tmp) / "missing.json")], now=NOW)
        self.assertEqual((code, json.loads(out.getvalue())), (2, {"ok": False, "code": "settings_unreadable"}))


class PostTests(unittest.TestCase):
    """G2 F2 (mutation M14): the real post_event, not a fake poster: the timeout reaches the socket,
    there is one attempt only, and no proxy or redirect is followed."""

    URL = "http://127.0.0.1:5678/webhook/test-health-path-0001"

    def fake_opener(self, answer):
        calls = []

        class Response:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Opener:
            def open(self, request, *args, **kwargs):
                calls.append({"args": args, "kwargs": kwargs, "url": request.full_url, "method": request.get_method()})
                if isinstance(answer, BaseException):
                    raise answer
                return Response()

        handlers = []

        def build_opener(*given):
            handlers.extend(given)
            return Opener()

        return build_opener, calls, handlers

    def post(self, answer, timeout=10):
        build, calls, handlers = self.fake_opener(answer)
        with mock.patch.object(health.urllib.request, "build_opener", build):
            try:
                result = health.post_event(self.URL, {"X-XB-Health-Auth": SECRET}, b"{}", timeout)
            except OSError as exc:
                result = exc
        return result, calls, handlers

    def test_the_timeout_reaches_the_one_open_call(self):
        for timeout in (5, 10):
            result, calls, handlers = self.post(None, timeout)
            self.assertEqual(result, 202)
            self.assertEqual(len(calls), 1, "exactly one attempt")
            self.assertEqual(calls[0]["kwargs"], {"timeout": timeout})
            self.assertEqual(calls[0]["args"], ())
            self.assertEqual((calls[0]["url"], calls[0]["method"]), (self.URL, "POST"))
            # No proxy (an empty ProxyHandler) and redirects refused.
            proxies = [h for h in handlers if isinstance(h, health.urllib.request.ProxyHandler)]
            self.assertEqual(len(proxies), 1)
            self.assertEqual(proxies[0].proxies, {})
            self.assertTrue(any(isinstance(h, health._RefuseRedirect) for h in handlers))

    def test_no_answer_is_one_attempt_and_an_oserror(self):
        for error in (health.urllib.error.URLError("timed out"), TimeoutError("timed out"), ConnectionRefusedError()):
            result, calls, _ = self.post(error)
            self.assertIsInstance(result, OSError)
            self.assertEqual(len(calls), 1, "no retry")

    def test_the_run_posts_once_with_the_design_timeout_even_when_n8n_does_not_answer(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        parsed = health.parse_settings(raw_settings(tmp), NOW)
        self.assertEqual(parsed["send"]["timeout_seconds"], 10)  # design: 10 s
        build, calls, _ = self.fake_opener(health.urllib.error.URLError("timed out"))
        with mock.patch.object(health.urllib.request, "build_opener", build):
            result = health.run(parsed, NOW, no_send=False, observer=lambda s: healthy_obs(),
                                cred_reader=lambda target: SECRET)
        self.assertEqual((result["send_status"], result["send_error"]), ("failed", "no_answer"))
        self.assertEqual(len(calls), 1, "one POST, no retry")
        self.assertEqual(calls[0]["kwargs"], {"timeout": 10})

    def test_the_timeout_cannot_be_raised_past_the_design_value(self):
        for value in (11, 30, 0, 4, True, "10"):
            raw = raw_settings("C:\\fake-root")
            raw["send"]["timeout_seconds"] = value
            with self.assertRaises(health.SettingsError, msg=value) as caught:
                health.parse_settings(raw, NOW)
            self.assertEqual(caught.exception.code, "settings_send_timeout_invalid")
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(example["send"]["timeout_seconds"], 10)

    def test_a_silent_loopback_server_is_cut_off_by_the_timeout(self):
        # A real socket on 127.0.0.1 that accepts and never answers: without the timeout the call
        # would hang until the server closes; with it, an OSError comes back in about one second.
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        accepted = []

        def accept():
            try:
                conn, _ = server.accept()
                accepted.append(conn)
                time.sleep(6)
                conn.close()
            except OSError:
                pass

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/webhook/test-health-path-0001" % server.getsockname()[1]
        started = time.monotonic()
        with self.assertRaises(OSError):
            health.post_event(url, {"X-XB-Health-Auth": SECRET}, b"{}", 1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(len(accepted), 1, "one connection, no retry")


class FailSafeTests(unittest.TestCase):
    """G2 L5: an unexpected error in a run still exits 0 with one closed code and no detail."""

    def test_unexpected_error_exits_0_with_internal_error(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        config = Path(tmp) / "daily_health.local.json"
        config.write_text(json.dumps(raw_settings(tmp)), encoding="utf-8")

        def broken(settings):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "FAKE detail C:\\private\\path")

        out = io.StringIO()
        with redirect_stdout(out):
            code = health.main(["run", "--config", str(config)], now=NOW, observer=broken)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": False, "code": "internal_error"})
        self.assertNotIn("private", out.getvalue())
        for error in (KeyError("x"), ValueError("x"), RuntimeError("x")):
            out = io.StringIO()
            with redirect_stdout(out):
                code = health.main(["run", "--config", str(config)], now=NOW,
                                   observer=lambda s, error=error: (_ for _ in ()).throw(error))
            self.assertEqual((code, json.loads(out.getvalue())), (0, {"ok": False, "code": "internal_error"}))
        # A record folder that cannot be written keeps its own code.
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(health, "write_record", side_effect=PermissionError()):
            code = health.main(["run", "--config", str(config), "--no-send"], now=NOW,
                               observer=lambda s: healthy_obs())
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"ok": False, "code": "record_write_failed"}))


class StaticTests(unittest.TestCase):
    def test_script_never_writes_outside_its_record_folder_or_sends_mail_itself(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("smtplib", "telegram", "api.telegram", "gmail", "Register-ScheduledTask", "Start-ScheduledTask",
                          "Disable-ScheduledTask", "shell=True", "os.system", "requests"):
            self.assertNotIn(forbidden, text, forbidden)
        self.assertEqual(text.count("os.replace("), 1)
        self.assertEqual(text.count("post_event("), 1)  # defined once; called only through the poster hook

    def test_files_are_free_of_real_addresses(self):
        for path in (SCRIPT, INSTALLER, EXAMPLE, RUNBOOK, Path(__file__)):
            text = path.read_text(encoding="utf-8")
            emails = [e for e in EMAIL_RE.findall(text) if not e.endswith("example.com")]
            self.assertEqual(emails, [], path.name)

    def test_runbook_names_the_rules_pauses_off_switch_and_monday_rule(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for phrase in ("No `[XB health]` email on a Monday by 09:00 means the checker is down: tell Claude.",
                       "Disable-ScheduledTask -TaskPath '\\XB\\' -TaskName 'XB-DailyHealth'", "pause_expired",
                       "set `expected: on`", "xb_health_runs", "HEALTH SENDER UPDATE", "HEALTH CREDENTIALS",
                       "HEALTH N8N IMPORT", "HEALTH TEST EMAIL", "HEALTH TASK"):
            self.assertIn(phrase, text)
        for job in health.JOBS:
            self.assertIn("`%s`" % job, text)


class InstallerStaticTests(unittest.TestCase):
    def setUp(self):
        self.text = INSTALLER.read_text(encoding="utf-8")
        self.code = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))

    def test_task_shape(self):
        self.assertIn("SEPARATE OWNER OK", self.text)
        self.assertIn('[string]$TaskName = "XB-DailyHealth"', self.code)
        self.assertIn('[string]$TaskPath = "\\XB\\"', self.code)
        self.assertIn("New-ScheduledTaskTrigger -Daily -At $startAt", self.code)
        self.assertIn("(Get-Date).Date.AddHours(7).AddMinutes(30)", self.code)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Minutes 5)", self.code)
        self.assertIn("-MultipleInstances IgnoreNew", self.code)
        self.assertIn("-StartWhenAvailable", self.code)
        self.assertIn("-LogonType Interactive", self.code)
        self.assertIn("-RunLevel Limited", self.code)
        self.assertIn('run --config', self.code)
        self.assertNotIn("-Force", self.code)
        self.assertNotIn("--no-send", self.code)

    def test_dry_run_is_the_default_and_comes_before_register(self):
        self.assertIn("if ($DryRun -or -not $Register) {", self.code)
        self.assertLess(self.code.index("if ($DryRun -or -not $Register) {"), self.code.index("Register-ScheduledTask `"))
        self.assertLess(self.code.index("check-settings"), self.code.index("if ($DryRun -or -not $Register) {"))
        self.assertEqual(self.code.count("Register-ScheduledTask"), 1)
        # Windows PowerShell 5.1: $PSScriptRoot is resolved in the body, never in a param() default.
        param_block = self.code[self.code.index("param("):self.code.index("$ErrorActionPreference")]
        self.assertNotIn("$PSScriptRoot", param_block)
        self.assertIn('$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path', self.code)

    def test_parses_when_powershell_available(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            self.skipTest("PowerShell is not installed")
        command = ("$e = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                   "$env:XB_HEALTH_INSTALLER, [ref]$null, [ref]$e) | Out-Null; "
                   "if (@($e).Count -gt 0) { $e | ForEach-Object { $_.Message }; exit 1 }")
        env = dict(os.environ, XB_HEALTH_INSTALLER=str(INSTALLER))
        done = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True,
                              text=True, timeout=120, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class InstallerPowerShell51Tests(unittest.TestCase):
    """Runs the installer under Windows PowerShell 5.1 with -File and -DryRun only.

    -DryRun stops before Register-ScheduledTask and the task name is test-only, so nothing is ever
    registered. Fake user name and fake settings in a temporary folder only.
    """

    TASK_NAME = "XB daily health installer test (never registered)"

    def setUp(self):
        if os.name != "nt":
            self.skipTest("Windows PowerShell 5.1 runs only on Windows")
        self.shell = shutil.which("powershell")
        if self.shell is None:
            self.skipTest("Windows PowerShell is not installed")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Path(self.tmp) / "daily_health.local.json"
        # Pauses far ahead would go stale; this settings file has none, so it stays valid.
        self.config.write_text(json.dumps(raw_settings(self.tmp)), encoding="utf-8")

    def run_installer(self, *extra, config=None):
        args = [self.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                "-PythonExe", sys.executable, "-UserId", "EXAMPLE-PC\\example-user", "-TaskName", self.TASK_NAME,
                "-ConfigPath", str(config or self.config), "-DryRun", *extra]
        done = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def test_runs_under_windows_powershell_5_1(self):
        done = subprocess.run([self.shell, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.Major"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.stdout.strip(), "5", done.stdout + done.stderr)

    def test_dry_run_finds_this_clone_and_checks_the_settings(self):
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        self.assertIn("Dry run: would register \\XB\\" + self.TASK_NAME, out)
        self.assertIn("Repo root: " + str(ROOT), out)
        self.assertIn('Settings check: {"ok": true}', out)
        self.assertIn("Nothing was registered.", out)

    def test_dry_run_wins_over_register(self):
        code, out = self.run_installer("-Register")
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing was registered.", out)

    def test_example_settings_are_refused_before_anything_else(self):
        code, out = self.run_installer(config=EXAMPLE)
        self.assertNotEqual(code, 0, out)
        self.assertIn("Settings check failed", out)
        self.assertIn("settings_drive_root_invalid", out)
        self.assertNotIn("Dry run:", out)

    def test_bad_task_path_is_refused(self):
        for bad in ("\\", "XB", "\\XB", "\\X;B\\", "\\..\\"):
            with self.subTest(task_path=bad):
                code, out = self.run_installer("-TaskPath", bad)
                self.assertNotEqual(code, 0, out)
                self.assertIn("TaskPath must look like", out)
                self.assertNotIn("Dry run:", out)


if __name__ == "__main__":
    unittest.main()
