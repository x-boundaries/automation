"""#288 slice 3: the #155 member sync daily summary (offline, fakes only).

No VM, AutoCount book, SQL Server, Task Scheduler, n8n or Gmail is contacted. The sync's
check-config and digest answers and the task state are fake JSON fixtures built here (one test
also runs the real check-config child and the real digest reader on a throwaway ledger); the SQL
connection, the Credential Manager reader and the webhook POST are in-memory fakes. Names, phones
and emails are fake (reserved test ranges and example.com) and exist only to prove they never
reach the event.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_member_sync_daily_summary as summary  # noqa: E402

READ_SQL_COUNTS = summary.read_sql_counts  # the real reader, kept for fakes that patch the module

SOURCE = ROOT / "directlocal" / "xb_member_sync_daily_summary.py"
WORKFLOW = ROOT / "n8n-workflows" / "member_sync_daily_summary.workflow.json"
INSTALLER = ROOT / "scripts" / "install_member_sync_summary_task.ps1"
EXAMPLE_SETTINGS = ROOT / "config" / "member_sync_daily_summary.example.json"
# 7 Nov 2026 06:40 SGT.
NOW = datetime(2026, 11, 6, 22, 40, 0, tzinfo=timezone.utc)
TODAY = "2026-11-07"
# Fake personal data, assembled at runtime from the reserved test range; it must never leak.
FAKE_PHONE = "65" + "8000" + "0042"
FAKE_MEMBER_NO = "8000" + "0042"
FAKE_NAME = "Alpha Tester"
FAKE_EMAIL = "alpha.tester@example.com"
FAKE_GID = "gid://shopify/Customer/" + "700042"


def task_fixture(**overrides):
    value = {"state": "Ready", "last_run": "2026-11-07T06:30", "last_result": 0, "next_run": "2026-11-07T06:45", "missed_runs": 0}
    value.update(overrides)
    return value


def config_fixture(**overrides):
    value = {"status": "ok", "autocount_book": "AED_XBOUNDARIES", "ac2_create_enabled": True, "shopify_create_enabled": False,
             "max_writes_per_run": 1, "max_creates_per_hour": 6, "ac2_create_member_type": "GUEST",
             "immediate_create_enabled": False, "deferred_create_local_time": "06:00", "ac2_credential": "credential_manager"}
    value.update(overrides)
    return value


def digest_fixture(counts=None, alerts=None, refs=None, opened=None, **overrides):
    value = {
        "schema": "xb.directlocal.member_sync.digest.v1", "status": "ok", "autocount_book": "AED_XBOUNDARIES", "hours": 24,
        "runs": {"completed": 96, "unfinished": 0, "duration_seconds_max": 41, "duration_seconds_median": 12},
        "counts": {"waiting_for_staff": 3, "ac2_member_created": 1, "created_after_wait": 1, **(counts or {})},
        "alerts": {"created_after_wait": 1, "waiting_for_staff": 2, **(alerts or {})},
        "alert_refs": refs if refs is not None else [{"ref": "m-0a1b2c", "code": "memberno_conflict", "member_no_masked": "*******042"}],
        "open": {"shopify_pending": 0, "ac2_pending": 5, "ac2_pending_would_create": 4, "uncertain_writes": 0, "links": 1234, **(opened or {})},
        "create_latency_seconds": {"count": 0, "p50": None, "p95": None, "max": None},
        "creates": {"ac2_member_created": 1, "created_immediately": 0, "created_after_wait": 1, "writes_per_hour_max": 1, "writes_per_hour": {}},
        "caps": {"create_rate_capped": 0, "create_rate_capped_alerts": 0},
        "probe": None,
    }
    value.update(overrides)
    return value


class FakeRunner:
    """Answers the three read-only children by their argv; records every argv it is given."""

    def __init__(self, task=None, config=None, digest=None, codes=None):
        self.answers = {"task": task_fixture() if task is None else task, "check-config": config_fixture() if config is None else config,
                        "digest": digest_fixture() if digest is None else digest}
        self.codes = codes or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        kind = "task" if argv[0].lower().endswith("powershell.exe") else argv[2]
        answer = self.answers[kind]
        if isinstance(answer, BaseException):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return self.codes.get(kind, 0), "vendor noise line\n" + text + "\n"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last = None

    def execute(self, sql):
        self.conn.executed.append(sql)
        failure = self.conn.fail.get(sql)
        if failure is not None:
            raise failure
        self.last = sql

    def fetchone(self):
        return self.conn.rows[self.last][0]

    def fetchall(self):
        return self.conn.rows[self.last]


class FakeConnection:
    def __init__(self, identity=("EXAMPLE-VM\\example-user", "AED_XBOUNDARIES", 0, 0), per_type=None, new_members=3, fail=None):
        self.rows = {
            summary.IDENTITY_QUERY: [identity],
            summary.MEMBERS_PER_TYPE_QUERY: per_type if per_type is not None else [("GUEST", 12), ("MEMBER", 2000), (None, 1)],
            summary.NEW_MEMBERS_QUERY: [(new_members,)],
            summary.SESSION_SETUP: [],
        }
        self.fail = fail or {}
        self.executed: list[str] = []
        self.conn_str = None
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class SqlFailure(Exception):
    pass


def sql_reader_for(connection):
    def reader(sql, book):
        def connect(conn_str, timeout):
            connection.conn_str = conn_str
            return connection
        return READ_SQL_COUNTS(sql, book, connect=connect)
    return reader


class Poster:
    def __init__(self, status=200, error=None, check=None):
        self.status, self.error, self.check = status, error, check
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(body.decode("utf-8")), "timeout": timeout})
        if self.check is not None:
            self.check()
        if self.error is not None:
            raise self.error
        return self.status


class SummaryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-ms-summary-"))
        self.state_root = self.temp / "sync-state"
        self.state_root.mkdir()
        self.out = self.temp / "summary-records"
        self.out.mkdir()
        self.sync_config = self.temp / "member_sync_live.json"
        self.sync_config_text = json.dumps({"schema": "xb.directlocal.member_sync.config.v1", "state_root": str(self.state_root)})
        self.sync_config.write_text(self.sync_config_text, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def settings_value(self, **overrides):
        value = json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))
        value["sync_config_path"] = str(self.sync_config)
        value["output_dir"] = str(self.out)
        value["send"] = {**value["send"], "enabled": True, "webhook_url": "https://n8n.example.com/webhook/example-summary-path"}
        for key, item in overrides.items():
            value[key] = {**value[key], **item} if isinstance(item, dict) else item
        return value

    def settings(self, **overrides):
        return summary.parse_settings(self.settings_value(**overrides))

    def run_daily(self, runner=None, connection=None, poster=None, cred=None, settings=None, no_send=False):
        self.runner = runner or FakeRunner()
        self.connection = connection or FakeConnection()
        self.poster = poster or Poster()
        self.result = summary.daily(settings or self.settings(), now=NOW, no_send=no_send, runner=self.runner,
                                    sql_reader=sql_reader_for(self.connection), cred_reader=cred or (lambda target: "FAKE-SECRET-0001"),
                                    poster=self.poster)
        self.record = json.loads((self.out / f"daily_summary_{TODAY}.json").read_text(encoding="utf-8"))
        return self.record["event"]

    def stops(self, **kwargs):
        return self.run_daily(**kwargs)["stop_reasons"]


# ------------------------------------------------------------------------------------------------
# CLEAN and every STOP code
# ------------------------------------------------------------------------------------------------

class DecisionTests(SummaryTestCase):
    def test_stop_code_list_is_closed_and_pinned(self):
        self.assertEqual(summary.STOP_CODES, (
            "task_state_unavailable", "task_last_result", "check_config_failed", "config_unexpected", "digest_failed",
            "unfinished_runs", "shopify_write", "uncertain_writes", "possible_duplicate_member", "member_type_unknown",
            "login_refused", "writes_over_cap", "alert_unexpected"))
        self.assertEqual(summary.AUTO_OFF_CODES, ("shopify_write_while_disabled", "uncertain_writes", "possible_duplicate_member"))
        self.assertEqual(summary.EXPECTED_ALERTS, ("waiting_for_staff", "created_after_wait", "no_phone_sign_up_needs_staff",
                                                   "max_writes_per_run_reached"))
        self.assertEqual(summary.EXPECTED_ALERT_PREFIXES, ("memberno_",))
        # The n8n validator carries the same closed lists.
        code = next(n for n in json.loads(WORKFLOW.read_text(encoding="utf-8"))["nodes"]
                    if n["name"] == "Validate Summary Event")["parameters"]["jsCode"]
        for name, values in (("STOP_CODES", summary.STOP_CODES), ("AUTO_OFF_CODES", summary.AUTO_OFF_CODES),
                             ("TASK_STATES", summary.TASK_STATES), ("BOOKS", summary.AUTHORISED_BOOKS),
                             ("SQL_REASONS", summary.SQL_REASONS)):
            line = next(line for line in code.splitlines() if line.startswith("const %s = [" % name))
            self.assertEqual(json.loads(line.split("=", 1)[1].strip().rstrip(";").replace("'", '"')), list(values), name)
        for name in ("MAX_ALERT_CODES", "MAX_ALERT_REFS", "MAX_MEMBER_TYPES", "COUNT_MAX"):
            self.assertIn("const %s = %d;" % (name, getattr(summary, name)), code)

    def test_clean(self):
        event = self.run_daily()
        self.assertEqual(event["result"], "CLEAN")
        self.assertEqual(event["stop_reasons"], [])
        self.assertEqual((event["auto_off"], event["auto_off_reasons"]), ("off", []))
        self.assertEqual(event["summary_date"], TODAY)
        self.assertEqual(event["generated_at"], "2026-11-07T06:40+08:00")
        self.assertEqual(event["owner_session"], "not_checked")
        self.assertEqual(event["task"]["last_result"], 0)
        self.assertEqual(event["config"]["ac2_create_member_type"], "GUEST")
        self.assertEqual(event["digest"]["ac2_creates"], 1)
        self.assertEqual(event["digest"]["alert_refs"], [{"code": "memberno_conflict", "member_no": "***042"}])
        self.assertEqual(event["members_per_type"], {"available": True, "reason": None, "counts": {"-": 1, "GUEST": 12, "MEMBER": 2000}})
        self.assertEqual(event["new_members_24h"], {"available": True, "reason": None, "total": 3, "by_sync": 1, "by_staff": 2})
        self.assertEqual(self.result["email_status"], "sent")
        self.assertEqual(self.record["email_status"], "sent")

    def test_running_task_is_not_a_bad_result(self):
        self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(state="Running", last_result=summary.TASK_RESULT_RUNNING))), [])
        self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(last_result=3))), [])

    def test_stop_task_state_unavailable(self):
        self.assertEqual(self.stops(runner=FakeRunner(task="not json")), ["task_state_unavailable"])
        self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(), codes={"task": 1})), ["task_state_unavailable"])

    def test_stop_task_last_result(self):
        self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(last_result=2))), ["task_last_result"])
        self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(last_result=2147942402))), ["task_last_result"])

    def test_stop_check_config_failed(self):
        event = self.run_daily(runner=FakeRunner(config={"status": "refused", "code": "sync_config_invalid"}, codes={"check-config": 2}))
        self.assertEqual(event["stop_reasons"], ["check_config_failed"])
        self.assertEqual(event["members_per_type"]["reason"], "config_unavailable")
        self.assertEqual(self.stops(runner=FakeRunner(config=subprocess.TimeoutExpired("x", 1))), ["check_config_failed"])

    def test_stop_config_unexpected(self):
        self.assertEqual(self.stops(runner=FakeRunner(config=config_fixture(shopify_create_enabled=True))), ["config_unexpected"])
        self.assertEqual(self.stops(runner=FakeRunner(config=config_fixture(ac2_create_member_type="Default"))), ["config_unexpected"])

    def test_stop_digest_failed(self):
        self.assertEqual(self.stops(runner=FakeRunner(digest={"status": "refused", "code": "sync_ledger_missing"}, codes={"digest": 2})),
                         ["digest_failed"])

    def test_stop_unfinished_runs(self):
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(runs={"completed": 90, "unfinished": 1}))), [])
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(runs={"completed": 90, "unfinished": 2}))), ["unfinished_runs"])

    def test_stop_shopify_write(self):
        event = self.run_daily(runner=FakeRunner(digest=digest_fixture(counts={"shopify_customer_created": 1})))
        self.assertEqual(event["stop_reasons"], ["shopify_write"])
        self.assertEqual((event["auto_off"], event["auto_off_reasons"]), ("would_switch", ["shopify_write_while_disabled"]))
        # Not an auto-off condition once Shopify creates are switched on (that is config_unexpected).
        event = self.run_daily(runner=FakeRunner(config=config_fixture(shopify_create_enabled=True),
                                                 digest=digest_fixture(counts={"shopify_create_uncertain": 1})))
        self.assertEqual(event["stop_reasons"], ["config_unexpected", "shopify_write"])
        self.assertEqual(event["auto_off"], "off")

    def test_stop_uncertain_writes(self):
        event = self.run_daily(runner=FakeRunner(digest=digest_fixture(opened={"uncertain_writes": 1})))
        self.assertEqual(event["stop_reasons"], ["uncertain_writes"])
        self.assertEqual(event["auto_off_reasons"], ["uncertain_writes"])

    def test_stop_possible_duplicate_member(self):
        event = self.run_daily(runner=FakeRunner(digest=digest_fixture(alerts={"possible_duplicate_member": 1})))
        self.assertEqual(event["stop_reasons"], ["possible_duplicate_member"])
        self.assertEqual(event["auto_off_reasons"], ["possible_duplicate_member"])

    def test_stop_member_type_unknown(self):
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(alerts={"member_type_unknown": 4}))), ["member_type_unknown"])

    def test_stop_login_refused(self):
        for code in ("autocount_login_refused", "bridge_read_failed", "bridge_read_failed_timeout"):
            with self.subTest(code=code):
                self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(alerts={code: 1}))), ["login_refused"])

    def test_stop_writes_over_cap(self):
        # max_writes_per_run 1 x 4 runs = 4, max_creates_per_hour 6: the hour cap is 6.
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(creates={"writes_per_hour_max": 6}))), [])
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(creates={"writes_per_hour_max": 7}))), ["writes_over_cap"])

    def test_stop_alert_unexpected(self):
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(alerts={"customer_held_by_open_write": 1}))), ["alert_unexpected"])
        # The expected alerts and the memberno_* family are not a stop.
        expected = {code: 1 for code in summary.EXPECTED_ALERTS}
        expected.update({"memberno_conflict": 1, "memberno_exists_outside_window": 2})
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(alerts=expected))), [])

    def test_every_stop_together_keeps_the_pinned_order(self):
        task, config, digest = summary.task_section(task_fixture(last_result=1)), summary.config_section(config_fixture(ac2_create_member_type="X")), \
            summary.digest_section(digest_fixture(runs={"completed": 1, "unfinished": 5}, counts={"shopify_customer_created": 1},
                                                  opened={"uncertain_writes": 2}, creates={"writes_per_hour_max": 99},
                                                  alerts={"possible_duplicate_member": 1, "member_type_unknown": 1,
                                                          "autocount_x": 1, "zzz_new": 1}), None)
        stops, auto_off = summary.decide(task, config, digest, "GUEST")
        self.assertEqual(stops, [c for c in summary.STOP_CODES if c not in ("task_state_unavailable", "check_config_failed", "digest_failed")])
        self.assertEqual(auto_off, list(summary.AUTO_OFF_CODES))


# ------------------------------------------------------------------------------------------------
# Event privacy, history, SQL
# ------------------------------------------------------------------------------------------------

class EventTests(SummaryTestCase):
    def test_fake_names_phones_emails_and_full_member_nos_never_reach_the_event(self):
        noisy_refs = [
            {"ref": "m-0a1b2c", "code": "memberno_conflict", "member_no_masked": "*******042", "member_no": FAKE_MEMBER_NO,
             "name": FAKE_NAME, "phone": FAKE_PHONE, "email": FAKE_EMAIL, "customer_gid": FAKE_GID},
            {"ref": "c-ffff", "code": "memberno_conflict", "member_no_masked": FAKE_MEMBER_NO},  # not masked: dropped
            {"ref": "c-eeee", "code": FAKE_NAME, "member_no_masked": "***042"},  # not a code: dropped
        ]
        noisy_refs.append({"ref": "m-dddd", "code": "phone_" + FAKE_PHONE, "member_no_masked": "***042"})  # phone-shaped code
        digest = digest_fixture(refs=noisy_refs, alerts={FAKE_NAME: 1, "phone_" + FAKE_PHONE: 1, "memberno_conflict": 1},
                                customer={"name": FAKE_NAME, "phone": FAKE_PHONE, "email": FAKE_EMAIL})
        config = config_fixture(contact={"name": FAKE_NAME, "email": FAKE_EMAIL}, ac2_create_member_type=FAKE_MEMBER_NO)
        connection = FakeConnection(per_type=[("GUEST", 1), (FAKE_NAME, 1), (FAKE_PHONE, 2)])
        event = self.run_daily(runner=FakeRunner(digest=digest, config=config), connection=connection)
        for text in (json.dumps(event), json.dumps(self.record), json.dumps(self.poster.calls[0]["body"])):
            for leak in (FAKE_PHONE, FAKE_MEMBER_NO, "Alpha", "Tester", "example.com", "gid://", "m-0a1b2c", "c-ffff"):
                self.assertNotIn(leak, text)
        self.assertEqual(event["digest"]["alert_refs"], [{"code": "memberno_conflict", "member_no": "***042"}])
        self.assertIsNone(event["config"]["ac2_create_member_type"])
        self.assertEqual(event["members_per_type"]["counts"], {"GUEST": 1, "other": 3})
        # Unreadable alert codes are counted under one code that is not expected, so they still stop.
        self.assertEqual(event["digest"]["alerts"], {"created_after_wait": 1, "memberno_conflict": 1, "unreadable_alert_code": 2,
                                                     "waiting_for_staff": 2})
        self.assertIn("alert_unexpected", event["stop_reasons"])
        self.assertEqual(list(event), list(summary.EVENT_KEYS))
        summary.check_event(event)
        # The last guard before the POST refuses a phone-shaped value even if a section let it in.
        event["digest"]["alerts"] = {"phone_" + FAKE_PHONE: 1}
        with self.assertRaises(summary.SummaryError) as caught:
            summary.check_event(event)
        self.assertEqual(caught.exception.code, "event_personal_data_like")

    def test_masked_member_no_keeps_three_characters_only(self):
        self.assertEqual(summary._masked("*******042"), "***042")
        self.assertEqual(summary._masked("***42"), "***42")
        for bad in (FAKE_MEMBER_NO, "042", "", None, 42, "***", "**042", "*******0é2"):
            self.assertIsNone(summary._masked(bad), bad)

    def test_check_event_refuses_a_full_member_no(self):
        event = self.run_daily()
        event["digest"]["alert_refs"] = [{"code": "memberno_conflict", "member_no": FAKE_MEMBER_NO}]
        with self.assertRaises(summary.SummaryError) as caught:
            summary.check_event(event)
        self.assertEqual(caught.exception.code, "event_ref_invalid")

    def test_lists_are_bounded(self):
        alerts = {"memberno_%02d" % n: 1 for n in range(40)}
        refs = [{"ref": "m-%02x" % n, "code": "memberno_conflict", "member_no_masked": "****%03d" % n} for n in range(30)]
        event = self.run_daily(runner=FakeRunner(digest=digest_fixture(alerts=alerts, refs=refs)),
                               connection=FakeConnection(per_type=[("T%02d" % n, n + 1) for n in range(30)]))
        self.assertEqual(len(event["digest"]["alerts"]), summary.MAX_ALERT_CODES)
        # Nothing is lost: the codes past the bound are summed under other_alert_codes.
        self.assertEqual(sum(event["digest"]["alerts"].values()), 40 + 1 + 2)
        self.assertIn("other_alert_codes", event["digest"]["alerts"])
        # The bound is applied after the decision: 40 expected memberno_* codes are still CLEAN.
        self.assertEqual(event["result"], "CLEAN")
        self.assertEqual(len(event["digest"]["alert_refs"]), summary.MAX_ALERT_REFS)
        self.assertEqual(len(event["members_per_type"]["counts"]), summary.MAX_MEMBER_TYPES)

    def test_first_create_and_links_yesterday_come_from_earlier_records(self):
        event = self.run_daily()
        self.assertIs(event["first_create"], True)
        self.assertIsNone(event["digest"]["links_yesterday"])
        (self.out / f"daily_summary_{TODAY}.json").rename(self.out / "daily_summary_2026-11-06.json")
        event = self.run_daily()
        self.assertIs(event["first_create"], False)
        self.assertEqual(event["digest"]["links_yesterday"], 1234)
        # The owner can declare that creates happened before this task existed.
        for path in self.out.glob("*.json"):
            path.unlink()
        self.assertIs(self.run_daily(settings=self.settings(first_create_already_seen=True))["first_create"], False)

    def test_real_digest_shape_is_read(self):
        # The digest reader of the sync itself, on a throwaway ledger: the summary must understand
        # exactly what it prints (no fixture drift).
        import xb_member_sync as sync
        import xb_directlocal as dl

        ledger_root = self.temp / "ledger"
        ledger_root.mkdir()
        cfg = sync.parse_config({"schema": sync.CONFIG_SCHEMA, "state_root": str(ledger_root), "autocount_book": "AED_TESTINGAPI",
                                 "shopify": {"store_gid": "gid://shopify/Shop/900000001", "shop_domain": "xb-sync-test.synthetic.invalid",
                                             "api_version": "2026-07", "auth": {"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_SYNC_TOKEN"}},
                                 "shopify_cutover_utc": "2026-11-01T00:00:00Z"})
        ledger = sync.SyncLedger(ledger_root, clock=lambda: NOW, check_custody=False)
        try:
            ledger.init_meta(autocount_book="AED_TESTINGAPI", store_gid=cfg.shopify.store_gid, shop_domain=cfg.shopify.shop_domain)
            with ledger.tx() as conn:
                stored = {"counts": {"ac2_member_created": 2, "shopify_customer_created": 1}, "alerts": ["possible_duplicate_member"],
                          "refs": [{"ref": "m-0a1b2c", "code": "possible_duplicate_member", "member_no_masked": "*******042"}]}
                conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('r1', ?, ?, ?)",
                             (dl.ts(NOW - timedelta(hours=2)), dl.ts(NOW - timedelta(hours=2) + timedelta(seconds=30)), json.dumps(stored)))
                conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('r2', ?, NULL, NULL)", (dl.ts(NOW - timedelta(hours=1)),))
        finally:
            ledger.close()
        real = sync.ledger_digest(ledger_root, hours=24, now=NOW, config=cfg)
        section = summary.digest_section(json.loads(json.dumps(real)), None)
        self.assertTrue(section["available"])
        self.assertEqual((section["runs_completed"], section["runs_unfinished"], section["duration_max_s"]), (1, 1, 30))
        self.assertEqual((section["ac2_creates"], section["shopify_writes"], section["possible_duplicate_member"]), (2, 1, 1))
        self.assertEqual(section["alert_refs"], [{"code": "possible_duplicate_member", "member_no": "***042"}])
        for key in ("links", "ac2_pending", "ac2_pending_would_create", "shopify_pending", "uncertain_writes", "writes_per_hour_max"):
            self.assertIsNotNone(section[key], key)

    def test_real_check_config_child_is_read(self):
        sync_config = self.temp / "sync_test.json"
        sync_config.write_text(json.dumps({
            "schema": "xb.directlocal.member_sync.config.v1", "state_root": str(self.state_root), "autocount_book": "AED_TESTINGAPI",
            "shopify": {"store_gid": "gid://shopify/Shop/900000001", "shop_domain": "xb-sync-test.synthetic.invalid", "api_version": "2026-07",
                        "auth": {"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_SYNC_TOKEN"}},
            "shopify_cutover_utc": "2026-11-01T00:00:00Z"}), encoding="utf-8")
        settings = dict(self.settings())
        settings["sync_config_path"] = str(sync_config)
        section = summary.config_section(summary.run_sync("check-config", settings, summary.run_child))
        self.assertTrue(section["available"])
        self.assertEqual(section["autocount_book"], "AED_TESTINGAPI")
        self.assertIs(section["shopify_create_enabled"], False)
        self.assertIsInstance(section["max_writes_per_run"], int)


class SqlTests(SummaryTestCase):
    def sql(self, **overrides):
        return {**self.settings()["sql"], **overrides}

    def read(self, connection, book="AED_XBOUNDARIES", **overrides):
        return summary.read_sql_counts(self.sql(**overrides), book, connect=lambda conn_str, timeout: (setattr(connection, "conn_str", conn_str), connection)[1])

    def test_windows_auth_read_only_intent_one_book(self):
        connection = FakeConnection()
        members, new = self.read(connection)
        self.assertTrue(members["available"] and new["available"])
        parts = connection.conn_str.split(";")
        self.assertIn("Trusted_Connection=yes", parts)
        self.assertIn("ApplicationIntent=ReadOnly", parts)
        self.assertIn("DATABASE=AED_XBOUNDARIES", parts)
        self.assertNotRegex(connection.conn_str, r"(?i)\b(UID|PWD|User ID|Password)=")
        self.assertEqual(connection.executed, [summary.SESSION_SETUP, summary.IDENTITY_QUERY, summary.MEMBERS_PER_TYPE_QUERY,
                                               summary.NEW_MEMBERS_QUERY])
        self.assertTrue(connection.closed)

    def test_sql_texts_are_counts_only_selects(self):
        for text in summary.SQL_TEXTS:
            self.assertRegex(text, r"^(SELECT|SET) ")
            self.assertNotRegex(text.upper(), r"\b(INSERT|UPDATE|DELETE|MERGE|EXEC|EXECUTE|DROP|ALTER|CREATE|TRUNCATE|GRANT|INTO|OPENROWSET|XP_)\b")
        for text in (summary.MEMBERS_PER_TYPE_QUERY, summary.NEW_MEMBERS_QUERY):
            self.assertIn("COUNT_BIG(*)", text)
            # Only the type code is read next to the counts: no name, phone, email or MemberNo column.
            self.assertNotRegex(text, r"(?i)\b(Name|Mobile|Phone|Email|MemberNo|Address|Content|DOB|IC)\b")

    def test_no_select_right_reports_not_available_and_still_sends(self):
        denied = SqlFailure("[42000] The SELECT permission was denied on the object 'Member' (229)")
        connection = FakeConnection(fail={summary.MEMBERS_PER_TYPE_QUERY: denied, summary.NEW_MEMBERS_QUERY: denied})
        # The fake raises a plain Exception subclass; the reader classifies it by the 229 text.
        members, new = summary.read_sql_counts(self.sql(), "AED_XBOUNDARIES", connect=lambda c, t: connection)
        self.assertEqual((members["available"], members["reason"]), (False, "permission_denied"))
        self.assertEqual((new["available"], new["reason"]), (False, "permission_denied"))
        event = self.run_daily(connection=FakeConnection(fail={summary.NEW_MEMBERS_QUERY: denied}))
        self.assertEqual(event["result"], "CLEAN")
        self.assertEqual(event["new_members_24h"]["reason"], "permission_denied")
        self.assertTrue(event["members_per_type"]["available"])
        self.assertEqual(self.result["email_status"], "sent")

    def test_identity_guard(self):
        cases = {
            "identity_refused": [("sa", "AED_XBOUNDARIES", 1, 1), (" SA ", "AED_XBOUNDARIES", 0, 0),
                                 ("EXAMPLE-VM\\example-user", "AED_XBOUNDARIES", 1, 0),
                                 ("EXAMPLE-VM\\example-user", "AED_XBOUNDARIES", 0, None)],
            "book_mismatch": [("EXAMPLE-VM\\example-user", "AED_TESTINGAPI", 0, 0), ("EXAMPLE-VM\\example-user", "master", 0, 0)],
        }
        for reason, identities in cases.items():
            for identity in identities:
                with self.subTest(identity=identity):
                    connection = FakeConnection(identity=identity)
                    members, new = self.read(connection)
                    self.assertEqual((members["reason"], new["reason"]), (reason, reason))
                    self.assertNotIn(summary.MEMBERS_PER_TYPE_QUERY, connection.executed, "no business query after a refusal")
        # A privileged Windows identity is accepted only when the owner allows it.
        members, _ = self.read(FakeConnection(identity=("EXAMPLE-VM\\example-user", "AED_XBOUNDARIES", 1, 0)), allow_privileged_identity=True)
        self.assertTrue(members["available"])
        # sa is never accepted, not even then.
        members, _ = self.read(FakeConnection(identity=("sa", "AED_XBOUNDARIES", 1, 1)), allow_privileged_identity=True)
        self.assertEqual(members["reason"], "identity_refused")

    def test_disabled_missing_driver_unknown_book_and_connect_failure(self):
        self.assertEqual(self.read(FakeConnection(), enabled=False)[0]["reason"], "disabled")
        self.assertEqual(self.read(FakeConnection(), book="master")[0]["reason"], "book_mismatch")
        with mock.patch.dict(sys.modules, {"pyodbc": None}):
            self.assertEqual(summary.read_sql_counts(self.sql(), "AED_XBOUNDARIES")[0]["reason"], "driver_missing")

        def refuse(conn_str, timeout):
            raise SqlFailure("login failed")
        self.assertEqual(summary.read_sql_counts(self.sql(), "AED_XBOUNDARIES", connect=refuse)[0]["reason"], "connect_failed")


# ------------------------------------------------------------------------------------------------
# Send, record and exit code
# ------------------------------------------------------------------------------------------------

class SendTests(SummaryTestCase):
    def test_record_is_written_before_the_send_and_after(self):
        seen = []

        def check():
            record = json.loads((self.out / f"daily_summary_{TODAY}.json").read_text(encoding="utf-8"))
            seen.append(record["email_status"])
        self.run_daily(poster=Poster(check=check))
        self.assertEqual(seen, ["pending"])
        self.assertEqual(self.record["email_status"], "sent")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [f"daily_summary_{TODAY}.json"])

    def test_the_post_carries_the_closed_event_and_the_header_secret_only(self):
        self.run_daily()
        (call,) = self.poster.calls
        self.assertEqual(call["url"], "https://n8n.example.com/webhook/example-summary-path")
        self.assertEqual(call["headers"], {"Content-Type": "application/json", "X-XB-Summary-Token": "FAKE-SECRET-0001"})
        self.assertEqual(call["body"], self.record["event"])
        self.assertNotIn("FAKE-SECRET", json.dumps(self.record))

    def test_send_failure_is_recorded_and_never_fails_the_task(self):
        for poster, status, http_status, error in (
            (Poster(status=502), "send_failed", 502, "http_status"),
            (Poster(status=500), "send_failed", 500, "http_status"),
            (Poster(status=400), "send_failed", 400, "http_status"),
            (Poster(error=OSError("unreachable")), "send_failed", None, "no_answer"),
            (Poster(error=TimeoutError()), "send_failed", None, "no_answer"),
        ):
            with self.subTest(status=http_status, error=error):
                self.run_daily(poster=poster)
                self.assertEqual((self.record["email_status"], self.record["http_status"], self.record["send_error"]),
                                 (status, http_status, error))
                self.assertEqual(self.result["email_status"], status)
                # One POST per run: no retry (a retry after a timeout can send a second email).
                self.assertEqual(len(poster.calls), 1)

    def test_secret_unavailable_and_send_disabled(self):
        def no_secret(target):
            raise summary.CredentialUnavailable()
        self.run_daily(cred=no_secret)
        self.assertEqual(self.record["email_status"], "secret_unavailable")
        self.assertEqual(self.poster.calls, [])
        self.run_daily(settings=self.settings(send={"enabled": False}))
        self.assertEqual(self.record["email_status"], "disabled")
        self.assertEqual(self.poster.calls, [])
        self.run_daily(no_send=True)
        self.assertEqual(self.record["email_status"], "not_sent_check_only")
        self.assertEqual(self.poster.calls, [])

    def cli(self, poster, task=None):
        settings_path = self.temp / "settings.json"
        settings_path.write_text(json.dumps(self.settings_value()), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(summary, "run_child", FakeRunner(task=task)), \
                mock.patch.object(summary, "read_sql_counts", sql_reader_for(FakeConnection())), \
                mock.patch.object(summary, "read_credential_secret", lambda target: "FAKE-SECRET-0001"), \
                mock.patch.object(summary, "post_event", poster), redirect_stdout(out):
            code = summary.main(["daily", "--settings", str(settings_path)])
        return code, json.loads(out.getvalue())

    def test_exit_code_is_0_for_clean_stop_and_any_send_failure(self):
        for poster in (Poster(), Poster(status=502), Poster(error=OSError("down"))):
            code, out = self.cli(poster)
            self.assertEqual(code, 0, out)
        code, out = self.cli(Poster(status=500), task=task_fixture(last_result=2))
        self.assertEqual((code, out["result"], out["email_status"], out["http_status"]), (0, "STOP", "send_failed", 500))

    def test_bad_settings_refuse_with_a_code(self):
        settings_path = self.temp / "settings.json"
        for bad in ({**self.settings_value(), "extra": 1}, {**self.settings_value(), "schema": "v2"},
                    self.settings_value(send={"webhook_url": "http://n8n.example.com/webhook/x"}),
                    self.settings_value(send={"webhook_url": "https://n8n.example.com/webhook/REPLACE_WITH_X_PATH"}),
                    self.settings_value(sql={"odbc_driver": "SQL Server Native Client"}),
                    self.settings_value(sql={"server": "localhost;UID=sa"}),
                    self.settings_value(output_dir="relative\\dir")):
            settings_path.write_text(json.dumps(bad), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = summary.main(["check-settings", "--settings", str(settings_path)])
            self.assertEqual(code, 2, bad)
            self.assertEqual(json.loads(out.getvalue())["status"], "refused")

    def test_example_settings_pass_check_settings(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = summary.main(["check-settings", "--settings", str(EXAMPLE_SETTINGS)])
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"status": "ok", "send_enabled": False, "sql_enabled": True}))

    def test_output_dir_inside_the_sync_state_is_refused(self):
        with self.assertRaises(summary.SummaryError) as caught:
            summary.daily(self.settings(output_dir=str(self.state_root)), now=NOW, runner=FakeRunner())
        self.assertEqual(caught.exception.code, "output_dir_inside_sync_state")
        nested = self.state_root / "reports"
        nested.mkdir()
        with self.assertRaises(summary.SummaryError):
            summary.daily(self.settings(output_dir=str(nested)), now=NOW, runner=FakeRunner())


# ------------------------------------------------------------------------------------------------
# The "never" rules: runtime and static
# ------------------------------------------------------------------------------------------------

def snapshot(root: Path, skip: Path) -> dict[str, tuple[int, bytes]]:
    return {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file() and skip not in p.parents}


class NeverRuntimeTests(SummaryTestCase):
    def test_only_check_config_digest_and_the_task_read_are_started(self):
        self.run_daily()
        commands = [call[2] if not call[0].lower().endswith("powershell.exe") else "task-state" for call in self.runner.calls]
        self.assertEqual(commands, ["task-state", "check-config", "digest"])
        for call in self.runner.calls:
            if call[0].lower().endswith("powershell.exe"):
                self.assertEqual(call[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
                self.assertEqual(call[4], summary.TASK_STATE_SCRIPT)
            else:
                self.assertEqual(call[:2], [sys.executable, str(ROOT / "directlocal" / "xb_member_sync.py")])
                self.assertNotIn("status", call)
        self.assertEqual(self.runner.calls[2][2:], ["digest", "--config", str(self.sync_config), "--hours", "24"])
        self.assertEqual(self.runner.calls[1][2:], ["check-config", "--config", str(self.sync_config)])

    def test_any_other_sync_command_is_refused_before_a_process_starts(self):
        runner = FakeRunner()
        for command in ("status", "run", "init", "initial-link", "probe", "check-credential", "digest --apply"):
            with self.subTest(command=command), self.assertRaises(summary.SummaryError):
                summary.run_sync(command, self.settings(), runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(summary.SYNC_COMMANDS, ("check-config", "digest"))

    def test_observe_mode_never_writes_the_config_or_anything_outside_the_record_folder(self):
        before = snapshot(self.temp, self.out)
        for digest in (digest_fixture(counts={"shopify_customer_created": 1}), digest_fixture(opened={"uncertain_writes": 1}),
                       digest_fixture(alerts={"possible_duplicate_member": 1})):
            event = self.run_daily(runner=FakeRunner(digest=digest))
            self.assertEqual(event["auto_off"], "would_switch")
        self.assertEqual(snapshot(self.temp, self.out), before)
        self.assertEqual(self.sync_config.read_text(encoding="utf-8"), self.sync_config_text)
        self.assertEqual(list(self.state_root.iterdir()), [])


class NeverStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")
        cls.code = cls.code_only(cls.source)
        cls.tree = ast.parse(cls.source)

    @staticmethod
    def code_only(source):
        """The source with comments and docstrings blanked."""

        docstrings = {(node.body[0].value.lineno, node.body[0].value.col_offset) for node in ast.walk(ast.parse(source))
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}
        lines = [list(line) for line in source.splitlines(keepends=True)]
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT or (token.type == tokenize.STRING and token.start in docstrings):
                (row, col), (end_row, end_col) = token.start, token.end
                for r in range(row, end_row + 1):
                    first = col if r == row else 0
                    last = end_col if r == end_row else len(lines[r - 1])
                    for c in range(first, last):
                        if lines[r - 1][c] not in "\r\n":
                            lines[r - 1][c] = " "
        return "".join("".join(line) for line in lines)

    def constants(self):
        return {node.value for node in ast.walk(self.tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    def test_imports_are_stdlib_only_and_never_the_sync_modules(self):
        imported = {alias.name for node in ast.walk(self.tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(self.tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "argparse", "json", "os", "re", "subprocess", "sys", "urllib.error", "urllib.request",
                                    "datetime", "pathlib", "typing", "pyodbc", "ctypes"})

    def test_never_takes_the_ledger_lock_or_opens_the_ledger(self):
        forbidden = ("SyncLedger", "SyncEngine", "_OwnerLock", "member_sync.lock", ".lock", "msvcrt", "fcntl", "LockFile", "locking(",
                     "sqlite3", "member_sync.sqlite3", "ledger_digest", "xb_member_sync import", "import xb_member_sync",
                     "xb_directlocal", "verify_private_state")
        self.assertEqual([word for word in forbidden if word in self.code], [])

    def test_never_runs_status_run_init_initial_link_or_the_installer(self):
        for literal in ("run", "init", "initial-link", "probe", "check-credential", "--apply", "--direction",
                        "install_member_sync_task.ps1", "install_member_sync_summary_task.ps1"):
            self.assertNotIn(literal, self.constants(), literal)
        # Every run_sync call names one of the two allowed commands as a literal.
        calls = [node for node in ast.walk(self.tree) if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_sync"]
        self.assertEqual(sorted(call.args[0].value for call in calls), ["check-config", "digest"])
        # One child-process call site, no shell, and no other way to start a process.
        self.assertEqual(self.code.count("subprocess.run("), 1)
        self.assertIn("shell=False", self.code)
        for word in ("Popen", "os.system", "os.popen", "os.startfile", "os.exec", "os.spawn", "shell=True"):
            self.assertNotIn(word, self.code)

    def test_never_starts_the_bridge_or_signs_in_to_autocount(self):
        forbidden = ("BridgeClient", "default_bridge_command", "bridge_command", ".ps1", "write_once",
                     "load_ac2_credential", "XB-AC2-MemberSync", "AutoCount.", "UserLogin", "Login(", "create_member(")
        self.assertEqual([word for word in forbidden if word in self.code], [])

    def test_never_uses_the_sa_login_or_a_sql_password(self):
        self.assertNotRegex(self.code, r"(?i)\b(UID|PWD)=|sql_login|User ID=|Password=")
        self.assertIn('"Trusted_Connection=yes"', self.code)
        self.assertIn('"ApplicationIntent=ReadOnly"', self.code)
        self.assertIn('== "sa"', self.code)  # the identity guard refuses sa, never selects it

    def test_never_writes_the_ledger_the_config_or_any_file_but_its_record(self):
        self.assertEqual(len(re.findall(r"(?<![.\w])open\(", self.code)), 1)
        self.assertIn('open(temp, "x", encoding="utf-8")', self.code)
        self.assertEqual(self.code.count("os.replace("), 1)
        for word in ("write_text", "write_bytes", "shutil", "copyfile", "os.rename", ".rename(", "os.remove", "rmtree", "mkdir",
                     "INSERT", "UPDATE ", "DELETE", "MERGE", "Set-Content", "Copy-Item"):
            self.assertNotIn(word, self.code, word)
        # The one writer is write_record, and it writes only inside the output folder.
        writer = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == "write_record")
        writer_code = ast.get_source_segment(self.source, writer)
        self.assertIn("open(temp", writer_code)
        self.assertIn("os.replace(temp, target)", writer_code)
        self.assertIn('target.parent == out_dir, "record_path_invalid"', writer_code)

    def test_never_changes_a_scheduled_task(self):
        for word in ("Register-ScheduledTask", "Set-ScheduledTask", "Disable-ScheduledTask", "Enable-ScheduledTask",
                     "Unregister-ScheduledTask", "Start-ScheduledTask", "Stop-ScheduledTask", "schtasks", "/Change", "/Run"):
            self.assertNotIn(word, self.source, word)
        # PowerShell verb-noun commands (the quoted task name is not one).
        cmdlets = set(re.findall(r"(?<!')\b[A-Z][A-Za-z]+-[A-Z][A-Za-z]+\b(?!')", summary.TASK_STATE_SCRIPT))
        self.assertEqual(cmdlets, {"Get-ScheduledTask", "Get-ScheduledTaskInfo", "ConvertTo-Json"})
        self.assertIn("-TaskPath '\\XB\\' -TaskName 'XB-MemberSync'", summary.TASK_STATE_SCRIPT)

    def test_never_reads_or_prints_contact_fields(self):
        self.assertEqual(re.findall(r"(?i)phone|first_?name|last_?name|display_?name|birthday|\bdob\b|e-?mail_?address|customer_gid",
                                    self.code.replace('"no_phone_sign_up_needs_staff"', "")), [])
        self.assertNotIn("print(", self.code)
        self.assertEqual(self.code.count("sys.stdout.write("), 1)

    def test_the_secret_is_never_written_or_printed(self):
        sender = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == "send_event")
        text = ast.get_source_segment(self.source, sender)
        self.assertNotIn("_emit", text)
        self.assertNotIn("write_record", text)
        # The record holds the event and the outcome codes, never the headers.
        writer = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == "write_record")
        self.assertNotIn("headers", ast.get_source_segment(self.source, writer))

    def test_no_retry_and_no_redirect_on_the_post(self):
        poster = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == "post_event")
        text = ast.get_source_segment(self.source, poster)
        self.assertIn("ProxyHandler({})", text)
        self.assertIn("_RefuseRedirect()", text)
        self.assertNotRegex(text, r"\b(for|while)\b")
        sender = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == "send_event")
        sender_text = ast.get_source_segment(self.source, sender)
        self.assertNotRegex(sender_text, r"\b(for|while)\b")
        self.assertEqual(sender_text.count("poster("), 1)


# ------------------------------------------------------------------------------------------------
# Installer
# ------------------------------------------------------------------------------------------------

class InstallerStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = INSTALLER.read_text(encoding="utf-8")

    def test_pinned_name_folder_and_password_logon(self):
        self.assertIn('$PinnedTaskName = "XB-MemberSync-DailySummary"', self.text)
        self.assertIn('$PinnedTaskPath = "\\XB\\"', self.text)
        self.assertIn("[string]::Equals($LogonType, \"Password\", [StringComparison]::Ordinal)", self.text)
        self.assertIn("<LogonType>Password</LogonType>", self.text)
        self.assertIn("<RunLevel>LeastPrivilege</RunLevel>", self.text)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", self.text)
        self.assertIn('[string]$StartTime = "06:40"', self.text)
        self.assertIn("<ScheduleByDay>", self.text)
        # It never names the sync task itself.
        self.assertNotRegex(self.text, r"XB-MemberSync(?!-DailySummary)")

    def test_dry_run_by_default_and_never_replaces(self):
        self.assertIn("if ($DryRun -or -not $Register) {", self.text)
        self.assertLess(self.text.index("if ($DryRun -or -not $Register) {"), self.text.index("Register-ScheduledTask -TaskName"))
        self.assertLess(self.text.index("Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath"), self.text.index("Register-ScheduledTask -TaskName"))
        self.assertIn("It is never replaced by this installer.", self.text)
        # It may print the off switch for the owner, but never runs a task change itself.
        self.assertIsNone(re.search(r"(?m)^\s*(Unregister|Set|Disable|Enable|Start|Stop)-ScheduledTask", self.text))
        for word in ("-Force", "schtasks", "Invoke-Expression", "Start-Process"):
            self.assertNotIn(word, self.text)
        self.assertEqual(len(re.findall(r"(?m)^\s*Register-ScheduledTask ", self.text)), 1)
        self.assertIn("check-settings --settings $SettingsPath", self.text)


class InstallerPowerShell51Tests(unittest.TestCase):
    """Runs the installer under Windows PowerShell 5.1 with -File and -DryRun (never registers;
    a stand-in for Register-ScheduledTask would be replaced once the module autoloads, so only
    -DryRun is probed). Fake user name only."""

    def setUp(self):
        if os.name != "nt":
            self.skipTest("Windows PowerShell 5.1 runs only on Windows")
        self.shell = shutil.which("powershell")
        if self.shell is None:
            self.skipTest("Windows PowerShell is not installed")

    def run_installer(self, *extra):
        args = [self.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                "-PythonExe", sys.executable, "-UserId", "EXAMPLE-PC\\example-user", "-SettingsPath", str(EXAMPLE_SETTINGS), *extra]
        done = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def test_dry_run_prints_the_pinned_task_and_registers_nothing(self):
        for extra in (("-DryRun",), ("-DryRun", "-Register")):
            with self.subTest(extra=extra):
                code, out = self.run_installer(*extra)
                self.assertEqual(code, 0, out)
                self.assertIn("would register \\XB\\XB-MemberSync-DailySummary", out)
                self.assertIn("Daily at 06:40", out)
                self.assertIn("Repo root: " + str(ROOT), out)
                self.assertIn("xb_member_sync_daily_summary.py&quot; daily --settings", out)
                self.assertIn("Nothing was registered.", out)

    def test_refusals(self):
        for extra, message in ((("-TaskName", "XB-MemberSync"), "TaskName is pinned"),
                               (("-TaskPath", "\\XB\\Other\\"), "TaskPath is pinned"),
                               (("-LogonType", "S4U"), "LogonType must be Password"),
                               (("-StartTime", "6:40"), "StartTime must be HH:mm")):
            with self.subTest(extra=extra):
                code, out = self.run_installer("-DryRun", *extra)
                self.assertNotEqual(code, 0, out)
                self.assertIn(message, out)
                self.assertNotIn("would register", out)


if __name__ == "__main__":
    unittest.main()
