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
SYNC_SOURCE = ROOT / "directlocal" / "xb_member_sync.py"
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
        value["sync_state_root"] = str(self.state_root)
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
            "task_state_unavailable", "task_last_result", "task_disabled", "check_config_failed", "config_unexpected",
            "digest_failed", "no_runs", "unfinished_runs", "shopify_write", "uncertain_writes", "possible_duplicate_member",
            "member_type_unknown", "login_refused", "writes_over_cap", "alert_unexpected"))
        self.assertEqual(summary.SHOPIFY_WRITE_CODES, ("shopify_customer_created", "shopify_create_uncertain"))
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

    def test_stop_task_last_result_for_a_killed_run_and_a_task_that_never_ran(self):
        # Only 0x41301 (a run in progress) is fine. 0x41306: the run was killed by the time
        # limit. 0x41303: the task has not run yet. Neither may pass as "running".
        self.assertEqual(summary.TASK_RESULT_RUNNING, 0x41301)
        for result in (0x41306, 0x41303, 0x41300, 0x41302):
            with self.subTest(result=hex(result)):
                self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(last_result=result))), ["task_last_result"])

    def test_stop_task_disabled(self):
        # A dead sync must not read CLEAN: the last result may still be 0.
        event = self.run_daily(runner=FakeRunner(task=task_fixture(state="Disabled", next_run=None)))
        self.assertEqual((event["result"], event["stop_reasons"]), ("STOP", ["task_disabled"]))
        self.assertEqual(event["task"]["state"], "Disabled")
        self.assertEqual(event["auto_off"], "off")
        for state in ("Ready", "Running", "Queued"):
            with self.subTest(state=state):
                self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(state=state))), [])

    def test_stop_no_runs(self):
        # No completed run in 24 hours with a readable digest: STOP, not CLEAN.
        event = self.run_daily(runner=FakeRunner(digest=digest_fixture(runs={"completed": 0, "unfinished": 0})))
        self.assertEqual((event["result"], event["stop_reasons"]), ("STOP", ["no_runs"]))
        self.assertEqual(event["digest"]["runs_completed"], 0)
        # A run count that cannot be read is treated the same way.
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(runs={"unfinished": 0}))), ["no_runs"])
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(runs={"completed": 1, "unfinished": 0}))), [])
        # A disabled task with no runs gives both, in the pinned order.
        self.assertEqual(self.stops(runner=FakeRunner(task=task_fixture(state="Disabled"),
                                                      digest=digest_fixture(runs={"completed": 0, "unfinished": 0}))),
                         ["task_disabled", "no_runs"])

    def test_stop_check_config_failed(self):
        event = self.run_daily(runner=FakeRunner(config={"status": "refused", "code": "sync_config_invalid"}, codes={"check-config": 2}))
        self.assertEqual(event["stop_reasons"], ["check_config_failed"])
        self.assertEqual(event["members_per_type"]["reason"], "config_unavailable")
        self.assertEqual(self.stops(runner=FakeRunner(config=subprocess.TimeoutExpired("x", 1))), ["check_config_failed"])

    def test_stop_config_unexpected(self):
        self.assertEqual(self.stops(runner=FakeRunner(config=config_fixture(shopify_create_enabled=True))), ["config_unexpected"])
        self.assertEqual(self.stops(runner=FakeRunner(config=config_fixture(ac2_create_member_type="Default"))), ["config_unexpected"])

    def test_stop_config_unexpected_when_the_shopify_flag_is_missing_or_not_a_boolean(self):
        missing = config_fixture()
        del missing["shopify_create_enabled"]
        self.assertEqual(self.stops(runner=FakeRunner(config=missing)), ["config_unexpected"])
        for value in (None, "false", 0, "no"):
            with self.subTest(value=value):
                event = self.run_daily(runner=FakeRunner(config=config_fixture(shopify_create_enabled=value)))
                self.assertEqual(event["stop_reasons"], ["config_unexpected"])
                self.assertIsNone(event["config"]["shopify_create_enabled"])

    def test_stop_config_unexpected_when_the_live_state_root_is_not_the_one_in_the_settings(self):
        other_root = self.temp / "another-sync-state"
        other_root.mkdir()
        self.sync_config.write_text(json.dumps({"schema": "xb.directlocal.member_sync.config.v1", "state_root": str(other_root)}),
                                    encoding="utf-8")
        event = self.run_daily()
        self.assertEqual((event["result"], event["stop_reasons"]), ("STOP", ["config_unexpected"]))
        self.assertIs(event["config"]["state_root_ok"], False)
        self.assertEqual(self.result["email_status"], "sent")
        # Only the flag is in the event: no path.
        self.assertNotIn("sync-state", json.dumps(self.record))
        # The clean case says the root matches.
        self.sync_config.write_text(self.sync_config_text, encoding="utf-8")
        self.assertIs(self.run_daily()["config"]["state_root_ok"], True)
        # check-config answered but the state root could not be read here: not confirmed, so STOP.
        section = summary.config_section(config_fixture(), None)
        self.assertEqual(summary.decide(summary.task_section(task_fixture()), section,
                                        summary.digest_section(digest_fixture(), None), "GUEST")[0], ["config_unexpected"])

    def test_broken_sync_config_still_sends_a_stop_email(self):
        # G2 B2: truncated JSON in the live sync config. The real check-config and digest children
        # refuse it; the summary must still record and POST a STOP, not refuse before any read.
        self.sync_config.write_text(self.sync_config_text[:25], encoding="utf-8")
        fake = FakeRunner()

        def runner(argv, timeout):
            if argv[0].lower().endswith("powershell.exe"):
                return fake(argv, timeout)
            fake.calls.append(list(argv))
            return summary.run_child(argv, timeout)
        event = self.run_daily(runner=runner)
        self.assertEqual((event["result"], event["stop_reasons"]), ("STOP", ["check_config_failed", "digest_failed"]))
        self.assertEqual(len(self.poster.calls), 1)
        self.assertEqual(self.poster.calls[0]["body"]["stop_reasons"], ["check_config_failed", "digest_failed"])
        self.assertEqual(self.record["email_status"], "sent")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [f"daily_summary_{TODAY}.json"])
        self.assertEqual([call[2] for call in fake.calls[1:]], ["check-config", "digest"])
        # The same for a missing file and for a config with no state root.
        for broken in (None, "{}", "[]", json.dumps({"state_root": ""}), json.dumps({"state_root": 5})):
            with self.subTest(broken=broken):
                if broken is None:
                    self.sync_config.unlink()
                else:
                    self.sync_config.write_text(broken, encoding="utf-8")
                refused = FakeRunner(config={"status": "refused", "code": "sync_config_invalid"}, codes={"check-config": 2},
                                     digest={"status": "refused", "code": "sync_config_invalid"})
                refused.codes["digest"] = 2
                event = self.run_daily(runner=refused)
                self.assertEqual(event["stop_reasons"], ["check_config_failed", "digest_failed"])
                self.assertEqual(len(self.poster.calls), 1)
                self.assertIsNone(event["config"]["state_root_ok"])

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

    def test_stop_shopify_write_counts_every_shopify_create_code(self):
        # G2 B1: a customer created without a phone is counted under its own code.
        for code in summary.SHOPIFY_WRITE_CODES:
            with self.subTest(code=code):
                event = self.run_daily(runner=FakeRunner(digest=digest_fixture(counts={code: 1})))
                self.assertEqual(event["digest"]["shopify_writes"], 1)
                self.assertEqual(event["stop_reasons"], ["shopify_write"])
                self.assertEqual((event["auto_off"], event["auto_off_reasons"]), ("would_switch", ["shopify_write_while_disabled"]))
        event = self.run_daily(runner=FakeRunner(digest=digest_fixture(counts={code: 2 for code in summary.SHOPIFY_WRITE_CODES})))
        self.assertEqual(event["digest"]["shopify_writes"], 4)
        # A create that Shopify rejected wrote nothing.
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(counts={"shopify_create_rejected": 1}))), [])

    def test_shopify_write_codes_cover_every_create_outcome_in_the_sync_source(self):
        # Reads the sync's own source: every code it counts after the one Shopify mutation is sent
        # must be a write code here (or the one "Shopify rejected it, nothing created" code). A new
        # outcome code in the sync fails this test until it is classified.
        source = SYNC_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        mutations = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and node.value.lstrip().startswith("mutation ")]
        self.assertEqual(len(mutations), 1, "the sync has exactly one Shopify mutation")
        self.assertIn("customerCreate(", mutations[0])
        sends = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "create_customer"]
        self.assertEqual(len(sends), 1, "one call site sends the mutation")
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and any(child is sends[0] for child in ast.walk(node)))
        self.assertEqual(method.name, "_create_in_shopify")
        counted = set()
        for node in ast.walk(method):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "count"
                    and node.lineno > sends[0].lineno):
                literals = {child.value for child in ast.walk(node.args[0]) if isinstance(child, ast.Constant) and isinstance(child.value, str)}
                self.assertTrue(literals, "an outcome code that is not a literal")
                counted |= literals
        self.assertEqual(counted - {"shopify_create_rejected"}, set(summary.SHOPIFY_WRITE_CODES))
        self.assertIn("shopify_create_rejected", counted)

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

    def test_stop_writes_over_cap_when_four_runs_allow_more_than_the_hour_cap(self):
        # max_writes_per_run 2 x 4 runs (the 15-minute schedule) = 8, above max_creates_per_hour 6.
        self.assertEqual(summary.RUNS_PER_HOUR, 4)
        config = config_fixture(max_writes_per_run=2, max_creates_per_hour=6)
        self.assertEqual(self.stops(runner=FakeRunner(config=config, digest=digest_fixture(creates={"writes_per_hour_max": 8}))), [])
        self.assertEqual(self.stops(runner=FakeRunner(config=config, digest=digest_fixture(creates={"writes_per_hour_max": 9}))),
                         ["writes_over_cap"])

    def test_stop_alert_unexpected(self):
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(alerts={"customer_held_by_open_write": 1}))), ["alert_unexpected"])
        # The expected alerts and the memberno_* family are not a stop.
        expected = {code: 1 for code in summary.EXPECTED_ALERTS}
        expected.update({"memberno_conflict": 1, "memberno_exists_outside_window": 2})
        self.assertEqual(self.stops(runner=FakeRunner(digest=digest_fixture(alerts=expected))), [])

    def test_every_stop_together_keeps_the_pinned_order(self):
        task, config, digest = summary.task_section(task_fixture(last_result=1, state="Disabled")), \
            summary.config_section(config_fixture(ac2_create_member_type="X"), True), \
            summary.digest_section(digest_fixture(runs={"completed": 0, "unfinished": 5}, counts={"shopify_customer_created": 1},
                                                  opened={"uncertain_writes": 2}, creates={"writes_per_hour_max": 99},
                                                  alerts={"possible_duplicate_member": 1, "member_type_unknown": 1,
                                                          "autocount_x": 1, "zzz_new": 1}), None)
        stops, auto_off = summary.decide(task, config, digest, "GUEST")
        self.assertEqual(stops, [c for c in summary.STOP_CODES if c not in ("task_state_unavailable", "check_config_failed", "digest_failed")])
        self.assertEqual(auto_off, list(summary.AUTO_OFF_CODES))

    def test_317_an_unreadable_part_never_stops_with_the_codes_it_replaces(self):
        # PR #317 G2 LATER 6 (carried): the n8n node's fit() cannot shorten the STOP line, and a line with 13 to 15
        # stop codes is over 300 characters. decide() never gives one: each "could not read" stop replaces the
        # codes that need that part, so at most 12 codes. tests/test_member_sync_daily_summary_workflow.py checks
        # that the 12-code line fits.
        replaces = {
            "task_state_unavailable": {"task_last_result", "task_disabled"},
            "check_config_failed": {"config_unexpected", "writes_over_cap"},
            "digest_failed": {"no_runs", "unfinished_runs", "shopify_write", "uncertain_writes", "possible_duplicate_member",
                              "member_type_unknown", "login_refused", "writes_over_cap", "alert_unexpected"},
        }
        worst_task = summary.task_section(task_fixture(last_result=1, state="Disabled"))
        worst_config = summary.config_section(config_fixture(ac2_create_member_type="X"), True)
        worst_digest = summary.digest_section(digest_fixture(runs={"completed": 0, "unfinished": 5}, counts={"shopify_customer_created": 1},
                                                             opened={"uncertain_writes": 2}, creates={"writes_per_hour_max": 99},
                                                             alerts={"possible_duplicate_member": 1, "member_type_unknown": 1,
                                                                     "autocount_x": 1, "zzz_new": 1}), None)
        for task_ok in (True, False):
            for config_ok in (True, False):
                for digest_ok in (True, False):
                    with self.subTest(task=task_ok, config=config_ok, digest=digest_ok):
                        stops, _ = summary.decide(worst_task if task_ok else summary.task_section(None),
                                                  worst_config if config_ok else summary.config_section(None, None),
                                                  worst_digest if digest_ok else summary.digest_section(None, None), "GUEST")
                        for code, ok in (("task_state_unavailable", task_ok), ("check_config_failed", config_ok),
                                         ("digest_failed", digest_ok)):
                            self.assertEqual(code in stops, not ok, code)
                            if not ok:
                                self.assertFalse(replaces[code] & set(stops), code)
                        self.assertLessEqual(len(stops), 12)


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

    def test_check_event_refuses_a_ref_that_keeps_four_characters(self):
        # G2 B3 (S4): three stars plus at most 3 characters, on the VM and in n8n alike.
        event = self.run_daily()
        for member_no in ("***0042", "****042", "**042", "***", "***04 2"):
            with self.subTest(member_no=member_no):
                event["digest"]["alert_refs"] = [{"code": "memberno_conflict", "member_no": member_no}]
                with self.assertRaises(summary.SummaryError) as caught:
                    summary.check_event(event)
                self.assertEqual(caught.exception.code, "event_ref_invalid")
        self.assertEqual(summary.MASKED_RE.pattern, r"^\*{3}[A-Za-z0-9]{1,3}$")

    def test_check_event_refuses_an_at_sign_in_any_text(self):
        # G2 B3 (P2): the last guard refuses an @ wherever it is placed, key or value.
        places = (
            lambda e: e["digest"].__setitem__("alerts", {"a@b": 1}),
            lambda e: e["members_per_type"].__setitem__("counts", {"a@b": 1}),
            lambda e: e["config"].__setitem__("ac2_create_member_type", "a@b"),
            lambda e: e["task"].__setitem__("state", "Ready@"),
            lambda e: e["digest"].__setitem__("alert_refs", [{"code": "a@b", "member_no": "***042"}]),
            lambda e: e.__setitem__("owner_session", "@"),
        )
        for index, place in enumerate(places):
            with self.subTest(place=index):
                event = self.run_daily()
                summary.check_event(event)
                place(event)
                with self.assertRaises(summary.SummaryError) as caught:
                    summary.check_event(event)
                self.assertEqual(caught.exception.code, "event_personal_data_like")

    def test_closed_patterns_are_the_same_on_the_vm_and_in_the_n8n_validator(self):
        code = next(n for n in json.loads(WORKFLOW.read_text(encoding="utf-8"))["nodes"]
                    if n["name"] == "Validate Summary Event")["parameters"]["jsCode"]
        for name, pattern in (("MASKED_RE", summary.MASKED_RE), ("CODE_RE", summary.CODE_RE), ("TYPE_RE", summary.MEMBER_TYPE_RE)):
            self.assertIn("const %s = /%s/;" % (name, pattern.pattern), code.splitlines(), name)
        line = next(line for line in code.splitlines() if line.startswith("const CONFIG_KEYS = ["))
        self.assertEqual(json.loads(line.split("=", 1)[1].strip().rstrip(";").replace("'", '"')),
                         list(summary.config_section(None, None)))

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
        section = summary.config_section(summary.run_sync("check-config", settings, summary.run_child), True)
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

    def test_the_real_connection_gets_a_login_timeout_and_a_query_timeout(self):
        # The default connect path, with a stand-in pyodbc module (no driver, no server).
        connection = FakeConnection()
        opened = []

        class FakePyodbc:
            Error = SqlFailure

            @staticmethod
            def connect(conn_str, **kwargs):
                opened.append((conn_str, kwargs))
                return connection
        with mock.patch.dict(sys.modules, {"pyodbc": FakePyodbc}):
            members, new = summary.read_sql_counts(self.sql(timeout_seconds=15), "AED_XBOUNDARIES")
        self.assertTrue(members["available"] and new["available"])
        ((conn_str, kwargs),) = opened
        self.assertEqual(kwargs, {"autocommit": True, "timeout": 15})
        self.assertIn("Trusted_Connection=yes", conn_str.split(";"))
        # Every statement is bounded: the query timeout is set on the connection before any query.
        self.assertEqual(connection.timeout, 15)
        self.assertTrue(connection.closed)


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
        self.assertEqual(call["headers"], {"Content-Type": "application/json", "User-Agent": "xb-member-sync-daily-summary/1",
                                           "X-XB-Summary-Token": "FAKE-SECRET-0001"})
        self.assertEqual(call["body"], self.record["event"])
        self.assertNotIn("FAKE-SECRET", json.dumps(self.record))

    def test_the_post_sends_its_own_user_agent(self):
        # The webhook is behind Cloudflare, which answers 403 to the default Python-urllib
        # User-Agent (20 Nov first live send). The stock and AR/AP senders send their own too.
        self.assertEqual(summary.USER_AGENT, "xb-member-sync-daily-summary/1")
        self.run_daily()
        (call,) = self.poster.calls
        agents = [value for name, value in call["headers"].items() if name.lower() == "user-agent"]
        self.assertEqual(agents, ["xb-member-sync-daily-summary/1"])

    def test_user_agent_reaches_the_wire_through_post_event(self):
        # A loopback-only listener (no network): urllib must send this User-Agent, not its own.
        import http.server
        import threading
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server hook
                seen.append(self.headers.get_all("User-Agent"))
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status = summary.post_event(f"http://127.0.0.1:{server.server_address[1]}/webhook/example",
                                        {"Content-Type": "application/json", "User-Agent": summary.USER_AGENT,
                                         "X-XB-Summary-Token": "FAKE-SECRET-0001"}, b"{}", 5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((status, seen), (200, [["xb-member-sync-daily-summary/1"]]))

    def test_the_headers_are_cleared_after_every_send(self):
        for status, error, outcome in (
            (200, None, ("sent", 200, None)),
            (202, None, ("logged_clean", 202, None)),
            (403, None, ("send_failed", 403, "http_status")),
            (None, OSError("down"), ("send_failed", None, "no_answer")),
        ):
            held, seen = [], []

            def poster(url, headers, body, timeout, status=status, error=error, held=held, seen=seen):
                # Record only: an assert in here would be swallowed by send_event's catch-all
                # (as no_answer), so every check runs after the send.
                held.append(headers)
                seen.append(dict(headers))
                if error is not None:
                    raise error
                return status
            with self.subTest(status=status, error=error):
                self.run_daily(poster=poster)
                (headers,), (sent,) = held, seen
                # The poster got the secret and the summary's own User-Agent ...
                self.assertEqual(sent["X-XB-Summary-Token"], "FAKE-SECRET-0001")
                self.assertEqual(sent["User-Agent"], summary.USER_AGENT)
                # ... and the same dict is empty after the send: secret and User-Agent gone.
                self.assertEqual(headers, {})
                self.assertEqual((self.record["email_status"], self.record["http_status"], self.record["send_error"]),
                                 outcome)
                self.assertEqual(self.result["email_status"], outcome[0])

    def test_the_secret_header_name_can_never_be_a_reserved_header(self):
        # HEADER_RE takes X- names only, so the secret header can never replace Content-Type,
        # User-Agent or Host (the stock sender's ALERT_RESERVED_HEADERS, by construction).
        for bad in ("User-Agent", "user-agent", "Content-Type", "Content-Length", "Host", "Connection", "Transfer-Encoding"):
            with self.subTest(bad=bad), self.assertRaises(summary.SummaryError) as caught:
                self.settings(send={"auth_header_name": bad})
            self.assertEqual(caught.exception.code, "settings_send_header_invalid")

    def test_send_failure_is_recorded_and_never_fails_the_task(self):
        for poster, status, http_status, error in (
            (Poster(status=502), "send_failed", 502, "http_status"),
            # #288 slice 3: only 200 (emailed) and 202 (CLEAN, logged) are answers n8n gives on success.
            (Poster(status=201), "send_failed", 201, "http_status"),
            (Poster(status=204), "send_failed", 204, "http_status"),
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
        # #288 wiring: both member tool switches are printed too (off in the example).
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"status": "ok", "send_enabled": False, "sql_enabled": True,
                                                                  "member_core_enabled": False, "tags_enabled": False}))

    def test_output_dir_inside_the_sync_state_is_refused(self):
        with self.assertRaises(summary.SummaryError) as caught:
            summary.daily(self.settings(output_dir=str(self.state_root)), now=NOW, runner=FakeRunner())
        self.assertEqual(caught.exception.code, "output_dir_inside_sync_state")
        nested = self.state_root / "reports"
        nested.mkdir()
        with self.assertRaises(summary.SummaryError):
            summary.daily(self.settings(output_dir=str(nested)), now=NOW, runner=FakeRunner())

    def test_output_dir_is_checked_against_the_setting_and_against_a_readable_live_config(self):
        # check-settings refuses it from the settings alone (no file is read).
        for folder in (self.state_root, self.state_root / "reports"):
            with self.assertRaises(summary.SummaryError) as caught:
                summary.parse_settings(self.settings_value(output_dir=str(folder)))
            self.assertEqual(caught.exception.code, "output_dir_inside_sync_state")
        for key, code in (("sync_state_root", "settings_sync_state_root_invalid"), ("output_dir", "settings_output_dir_invalid")):
            for bad in ("relative\\dir", "", 5, str(self.temp / "a" / ".." / "b")):
                with self.subTest(key=key, bad=bad), self.assertRaises(summary.SummaryError) as caught:
                    summary.parse_settings(self.settings_value(**{key: bad}))
                self.assertEqual(caught.exception.code, code)
        missing = self.settings_value()
        del missing["sync_state_root"]
        with self.assertRaises(summary.SummaryError) as caught:
            summary.parse_settings(missing)
        self.assertEqual(caught.exception.code, "settings_keys_invalid")
        # The live config names another state root and the record folder sits inside that one:
        # the summary still never writes into the sync's state (refused before any child starts).
        live_root = self.temp / "live-state"
        (live_root / "reports").mkdir(parents=True)
        self.sync_config.write_text(json.dumps({"state_root": str(live_root)}), encoding="utf-8")
        runner = FakeRunner()
        with self.assertRaises(summary.SummaryError) as caught:
            summary.daily(self.settings(output_dir=str(live_root / "reports")), now=NOW, runner=runner)
        self.assertEqual(caught.exception.code, "output_dir_inside_sync_state")
        self.assertEqual(runner.calls, [])
        self.assertEqual(list((live_root / "reports").iterdir()), [])

    def test_timeouts_must_fit_inside_the_task_time_limit(self):
        # Three children, the SQL part and the POST, worst case, inside PT5M with a margin.
        self.assertEqual((summary.TASK_TIME_LIMIT_SECONDS, summary.TASK_TIME_MARGIN_SECONDS, summary.CHILD_COUNT, summary.SQL_WAITS),
                         (300, 15, 3, 5))
        self.assertIn("<ExecutionTimeLimit>PT%dM</ExecutionTimeLimit>" % (summary.TASK_TIME_LIMIT_SECONDS // 60),
                      INSTALLER.read_text(encoding="utf-8"))
        example = summary.parse_settings(json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8")))
        # #288 wiring (Controller Q2): 40 s per child leaves room for both member tools at 20 s.
        self.assertEqual(example["child_timeout_seconds"], 40)
        self.assertEqual(summary.worst_case_seconds(example), 3 * 40 + 5 * 15 + 20)
        self.assertLessEqual(summary.worst_case_seconds(example), 285)
        # (#288 wiring: recalibrated for the example's 40 s children; each change is just over 285 s.)
        for change in ({"child_timeout_seconds": 120}, {"child_timeout_seconds": 64}, {"sql": {"timeout_seconds": 35}},
                       {"child_timeout_seconds": 60, "send": {"timeout_seconds": 31}}):
            with self.subTest(change=change), self.assertRaises(summary.SummaryError) as caught:
                self.settings(**change)
            self.assertEqual(caught.exception.code, "settings_timeouts_exceed_task_limit")
        # At the limit exactly, and with the SQL part switched off, the settings pass.
        self.assertEqual(summary.worst_case_seconds(self.settings(child_timeout_seconds=63)), 284)
        self.assertEqual(summary.worst_case_seconds(self.settings(child_timeout_seconds=88, sql={"enabled": False})), 284)


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
                                    self.code.replace('"no_phone_sign_up_needs_staff"', "")
                                    .replace('"shopify_separate_customer_created_without_phone"', "")), [])
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
        # The explicit refusal, exactly: an existing task is looked up and the installer throws.
        self.assertIn(
            "$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue\n"
            "if ($null -ne $existing) {\n"
            "    throw \"Refusing: a scheduled task with this name already exists in $TaskPath. It is never replaced by this installer.\"\n"
            "}\n", self.text)
        self.assertEqual(self.text.count("$existing"), 2)
        self.assertLess(self.text.index("if ($null -ne $existing) {"), self.text.index("if ($DryRun -or -not $Register) {"))
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


# ------------------------------------------------------------------------------------------------
# #155 slice BF2b (design 2.12): creates expected, the backfill window, the backfill shown apart
# ------------------------------------------------------------------------------------------------

WINDOW = {"from": "2026-11-06", "to": "2026-11-08", "max_per_call": 25, "max_per_hour": 60}


def backfill_part(**overrides):
    value = {"calls": 3, "creates": 40, "uncertain": 0, "rejected": 0, "writes": 40, "writes_per_call_max": 20,
             "writes_per_hour_max": 40, "live_writes_per_hour_max": 1}
    value.update(overrides)
    return value


def creates_digest(created=0, uncertain=0, rejected=0, backfill=None, hour_max=1, **extra):
    """A digest with these create outcome totals (live plus backfill) and, optionally, the
    backfill-members part the sync adds when a call ran."""

    counts = {code: n for code, n in (("shopify_customer_created", created), ("shopify_create_uncertain", uncertain),
                                      ("shopify_create_rejected", rejected)) if n}
    value = digest_fixture(counts=counts, creates={"ac2_member_created": 1, "created_immediately": 0, "created_after_wait": 1,
                                                   "writes_per_hour_max": hour_max, "writes_per_hour": {}}, **extra)
    if backfill is not None:
        value["backfill_members"] = backfill
    return value


class Bf2bSettingsTests(SummaryTestCase):
    def bf2b_value(self, **keys):
        value = self.settings_value()
        value.update(keys)
        return value

    def test_new_keys_are_optional_and_default_off(self):
        value = self.settings_value()
        for key in ("shopify_creates_expected", "backfill_members_window"):
            value.pop(key, None)
        settings = summary.parse_settings(value)
        self.assertEqual((settings["shopify_creates_expected"], settings["backfill_members_window"]), (False, None))
        example = json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))
        self.assertEqual((example["shopify_creates_expected"], example["backfill_members_window"]), (False, None))
        self.assertEqual(summary.parse_settings(self.bf2b_value(backfill_members_window=dict(WINDOW)))["backfill_members_window"], WINDOW)

    def test_bad_values_refuse(self):
        for key, bad, code in (
            ("shopify_creates_expected", "true", "settings_shopify_creates_expected_invalid"),
            ("shopify_creates_expected", None, "settings_shopify_creates_expected_invalid"),
            ("shopify_creates_expected", 1, "settings_shopify_creates_expected_invalid"),
            ("backfill_members_window", {}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", "2026-11-06", "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "extra": 1}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "from": "2026-11-09"}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "to": "2026-11-20"}, "settings_backfill_members_window_invalid"),  # 15 days
            ("backfill_members_window", {**WINDOW, "from": "2026-02-30"}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "to": "6 Nov 2026"}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "max_per_call": 26}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "max_per_call": 0}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "max_per_hour": 61}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "max_per_call": 30, "max_per_hour": 20}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "max_per_call": 21, "max_per_hour": 20}, "settings_backfill_members_window_invalid"),
            ("backfill_members_window", {**WINDOW, "max_per_hour": True}, "settings_backfill_members_window_invalid"),
            ("other_new_key", False, "settings_keys_invalid"),
        ):
            with self.subTest(key=key, bad=bad), self.assertRaises(summary.SummaryError) as caught:
                summary.parse_settings(self.bf2b_value(**{key: bad}))
            self.assertEqual(caught.exception.code, code)
        longest = {**WINDOW, "from": "2026-11-01", "to": "2026-11-14", "max_per_call": 20, "max_per_hour": 20}  # 14 days, equal caps
        self.assertEqual(summary.parse_settings(self.bf2b_value(backfill_members_window=longest))["backfill_members_window"], longest)

    def test_window_in_effect_covers_the_24_hours_before_the_summary(self):
        window = summary._window(dict(WINDOW))
        for today, expected in (("2026-11-05", False), ("2026-11-06", True), ("2026-11-08", True), ("2026-11-09", True), ("2026-11-10", False)):
            with self.subTest(today=today):
                self.assertIs(summary.window_in_effect(window, today), expected)
        self.assertIs(summary.window_in_effect(None, "2026-11-07"), False)


class Bf2bDecisionTests(SummaryTestCase):
    def run_with(self, digest, *, expected=False, window=None, config=None, **kwargs):
        value = self.settings_value()
        value["shopify_creates_expected"] = expected
        value["backfill_members_window"] = window
        return self.run_daily(runner=FakeRunner(config=config or config_fixture(shopify_create_enabled=expected), digest=digest),
                              settings=summary.parse_settings(value), **kwargs)

    # -- creates expected, no backfill --------------------------------------------------------
    def test_creates_on_and_expected_is_clean_and_live_creates_are_counted(self):
        event = self.run_with(creates_digest(created=3, hour_max=3), expected=True)
        self.assertEqual((event["result"], event["auto_off"]), ("CLEAN", "off"))
        self.assertEqual(event["digest"]["shopify_writes"], 3)
        self.assertIs(event["config"]["shopify_create_enabled"], True)
        self.assertNotIn(summary.BACKFILL_SHOWN, event["digest"]["alerts"])
        self.assertEqual(self.record["creates"]["live"], {"created": 3, "uncertain": 0, "rejected": 0})

    def test_expected_but_switched_off_is_not_unexpected(self):
        self.assertEqual(self.run_with(creates_digest(), expected=True, config=config_fixture(shopify_create_enabled=False))["stop_reasons"], [])
        self.assertEqual(self.run_with(creates_digest(), expected=True, config=config_fixture(shopify_create_enabled=None))["stop_reasons"],
                         ["config_unexpected"])

    def test_creates_while_the_config_says_off_still_stop_with_the_auto_off_reason(self):
        event = self.run_with(creates_digest(created=1), expected=True, config=config_fixture(shopify_create_enabled=False))
        self.assertEqual((event["stop_reasons"], event["auto_off_reasons"]), (["shopify_write"], ["shopify_write_while_disabled"]))

    def test_uncertain_and_rejected_live_creates_still_stop(self):
        for counts in ({"uncertain": 1}, {"rejected": 1}, {"created": 5, "rejected": 1}):
            with self.subTest(counts=counts):
                event = self.run_with(creates_digest(**counts), expected=True)
                self.assertEqual(event["stop_reasons"], ["shopify_write"])
                self.assertEqual(event["auto_off"], "off")  # creates are on: not the write-while-off reason

    def test_writes_over_the_live_cap_still_stop(self):
        self.assertEqual(self.run_with(creates_digest(created=6, hour_max=6), expected=True)["stop_reasons"], [])
        self.assertEqual(self.run_with(creates_digest(created=7, hour_max=7), expected=True)["stop_reasons"], ["writes_over_cap"])

    def test_without_the_setting_the_bf2a_rules_stay(self):
        event = self.run_with(creates_digest(created=1), expected=False, config=config_fixture(shopify_create_enabled=True))
        self.assertEqual(event["stop_reasons"], ["config_unexpected", "shopify_write"])
        self.assertNotIn("creates", self.record)
        self.assertEqual(self.run_with(creates_digest(rejected=1))["stop_reasons"], [])  # as before: nothing created

    # -- the backfill -------------------------------------------------------------------------
    def test_backfill_inside_the_declared_window_is_counted_and_shown_apart(self):
        digest = creates_digest(created=41, backfill=backfill_part(), hour_max=41)
        event = self.run_with(digest, expected=True, window=dict(WINDOW))
        self.assertEqual(event["stop_reasons"], [])
        self.assertEqual(event["digest"]["alerts"][summary.BACKFILL_SHOWN], 40)
        self.assertEqual(self.record["creates"]["live"]["created"], 1)
        self.assertEqual(self.record["creates"]["backfill"]["calls"], 3)
        self.assertEqual(self.record["creates"]["window"], WINDOW)
        # The n8n validator's closed contract still holds (no new event key).
        summary.check_event(event)
        self.assertEqual(tuple(event), summary.EVENT_KEYS)

    def test_backfill_inside_the_window_with_creates_not_yet_expected(self):
        # The sitting before S6 is left on overnight: the backfill counts, a live create stops.
        event = self.run_with(creates_digest(created=40, backfill=backfill_part(), hour_max=40), expected=False, window=dict(WINDOW))
        self.assertEqual((event["stop_reasons"], event["digest"]["alerts"][summary.BACKFILL_SHOWN]), ([], 40))
        event = self.run_with(creates_digest(created=41, backfill=backfill_part(), hour_max=41), expected=False, window=dict(WINDOW))
        self.assertEqual((event["stop_reasons"], event["auto_off_reasons"]), (["shopify_write"], ["shopify_write_while_disabled"]))

    def test_a_live_create_while_not_expected_stops_even_with_the_switch_on(self):
        event = self.run_with(creates_digest(created=41, backfill=backfill_part(), hour_max=41), expected=False, window=dict(WINDOW),
                              config=config_fixture(shopify_create_enabled=True))
        self.assertEqual((event["stop_reasons"], event["auto_off"]), (["config_unexpected", "shopify_write"], "off"))

    def test_outside_a_window_the_live_hours_include_the_backfill(self):
        digest = creates_digest(created=41, backfill=backfill_part(live_writes_per_hour_max=1), hour_max=41)
        self.assertEqual(self.run_with(digest, expected=True)["stop_reasons"], ["shopify_write", "writes_over_cap"])

    def test_backfill_outside_a_declared_window_stops(self):
        digest = creates_digest(created=41, backfill=backfill_part(), hour_max=41)
        for window in (None, {**WINDOW, "from": "2026-11-08", "to": "2026-11-10"}, {**WINDOW, "from": "2026-10-30", "to": "2026-11-05"}):
            with self.subTest(window=window):
                event = self.run_with(digest, expected=True, window=window)
                self.assertIn("shopify_write", event["stop_reasons"])
                self.assertEqual(event["digest"]["alerts"][summary.BACKFILL_SHOWN], 40)  # still shown

    def test_any_backfill_write_outside_a_window_stops_even_with_no_create(self):
        for part in (backfill_part(creates=0, rejected=1, writes=1, writes_per_call_max=1, writes_per_hour_max=1),
                     backfill_part(creates=0, uncertain=1, writes=1, writes_per_call_max=1, writes_per_hour_max=1),
                     backfill_part(creates=0, writes=1, writes_per_call_max=1, writes_per_hour_max=1)):
            with self.subTest(part=part):
                self.assertEqual(self.run_with(creates_digest(created=1, backfill=part), expected=True)["stop_reasons"], ["shopify_write"])

    def test_a_backfill_call_that_wrote_nothing_is_no_stop_outside_a_window(self):
        part = backfill_part(creates=0, writes=0, writes_per_call_max=0, writes_per_hour_max=0)
        self.assertEqual(self.run_with(creates_digest(created=1, backfill=part), expected=True)["stop_reasons"], [])

    def test_backfill_rejected_or_uncertain_stops_inside_the_window(self):
        for change in ({"rejected": 1}, {"uncertain": 1}):
            with self.subTest(change=change):
                part = backfill_part(**change)
                digest = creates_digest(created=41, rejected=part["rejected"], uncertain=part["uncertain"], backfill=part, hour_max=41)
                self.assertEqual(self.run_with(digest, expected=True, window=dict(WINDOW))["stop_reasons"], ["shopify_write"])
                # The backfill's own outcome, not a live one.
                self.assertEqual(self.record["creates"]["live"], {"created": 1, "uncertain": 0, "rejected": 0})

    def test_backfill_over_its_declared_caps_stops(self):
        window = {**WINDOW, "max_per_call": 20, "max_per_hour": 40}
        for part, stops in ((backfill_part(), []), (backfill_part(writes_per_call_max=21), ["writes_over_cap"]),
                            (backfill_part(writes_per_hour_max=41), ["writes_over_cap"])):
            with self.subTest(part=part):
                self.assertEqual(self.run_with(creates_digest(created=41, backfill=part, hour_max=41), expected=True, window=window)["stop_reasons"], stops)

    def test_live_hours_are_judged_apart_inside_the_window_only(self):
        # 41 writes in one hour: 40 the backfill's, 1 live: under the live cap of 6.
        digest = creates_digest(created=41, backfill=backfill_part(live_writes_per_hour_max=7), hour_max=47)
        self.assertEqual(self.run_with(digest, expected=True, window=dict(WINDOW))["stop_reasons"], ["writes_over_cap"])
        digest = creates_digest(created=41, backfill=backfill_part(live_writes_per_hour_max=6), hour_max=46)
        self.assertEqual(self.run_with(digest, expected=True, window=dict(WINDOW))["stop_reasons"], [])

    def test_keys_absent_ignore_the_backfill_part(self):
        digest = creates_digest(created=40, backfill=backfill_part(), hour_max=40)
        plain = self.run_daily(runner=FakeRunner(digest=digest))
        self.assertEqual(plain["stop_reasons"], ["shopify_write", "writes_over_cap"])
        self.assertNotIn(summary.BACKFILL_SHOWN, plain["digest"]["alerts"])
        self.assertNotIn("creates", self.record)

    def test_the_shown_count_is_never_an_unexpected_alert_and_is_bounded_with_the_rest(self):
        alerts = {f"memberno_x{n:02d}": 1 for n in range(summary.MAX_ALERT_CODES)}
        event = self.run_with(creates_digest(created=40, backfill=backfill_part(), hour_max=40, alerts=alerts), expected=True, window=dict(WINDOW))
        self.assertEqual(event["stop_reasons"], [])
        self.assertEqual(len(event["digest"]["alerts"]), summary.MAX_ALERT_CODES)

    def test_creates_section_is_none_without_settings_or_digest(self):
        self.assertIsNone(summary.creates_section(creates_digest(created=1), expected=False, window=None))
        self.assertIsNone(summary.creates_section(None, expected=True, window=None))
        section = summary.creates_section(creates_digest(created=1, backfill=backfill_part(creates=5)), expected=True, window=None)
        self.assertEqual((section["live"]["created"], section["backfill"]["creates"]), (1, 5))  # outside a window: all live
        bad = summary.creates_section(creates_digest(created=1, backfill={"calls": "3", "creates": -1}), expected=True, window=dict(WINDOW))
        self.assertEqual((bad["backfill"]["calls"], bad["backfill"]["creates"]), (0, 0))

    def test_real_digest_with_a_backfill_call_end_to_end(self):
        import xb_member_sync as sync
        import xb_directlocal as dl

        ledger_root = self.temp / "ledger"
        ledger_root.mkdir()
        cfg = sync.parse_config({"schema": sync.CONFIG_SCHEMA, "state_root": str(ledger_root), "autocount_book": "AED_TESTINGAPI",
                                 "shopify": {"store_gid": "gid://shopify/Shop/900000001", "shop_domain": "xb-sync-test.synthetic.invalid",
                                             "api_version": "2026-07", "auth": {"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_SYNC_TOKEN"}},
                                 "shopify_cutover_utc": "2026-11-01T00:00:00Z"})
        ledger = sync.SyncLedger(ledger_root, clock=lambda: NOW, check_custody=False)
        start = NOW - timedelta(hours=2)
        try:
            ledger.init_meta(autocount_book="AED_TESTINGAPI", store_gid=cfg.shopify.store_gid, shop_domain=cfg.shopify.shop_domain)
            with ledger.tx() as conn:
                conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('run-1', ?, ?, ?)",
                             (dl.ts(start), dl.ts(start + timedelta(seconds=30)), json.dumps({"counts": {"shopify_customer_created": 1}, "alerts": [], "refs": []})))
                conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('bf2-1', ?, ?, ?)",
                             (dl.ts(start + timedelta(minutes=2)), dl.ts(start + timedelta(minutes=4)),
                              json.dumps({"kind": "backfill_members", "counts": {"shopify_customer_created": 3, "bf2_created": 3}, "alerts": [], "refs": []})))
                for i, at in enumerate([start + timedelta(seconds=10)] + [start + timedelta(minutes=3)] * 3):
                    conn.execute("INSERT INTO writes(write_id, direction, member_no, member_no_key, state, created_at, updated_at) "
                                 "VALUES (?, 'ac2_to_shopify', ?, ?, 'DONE', ?, ?)", (f"write-{i}", f"M{i}", f"m{i}", dl.ts(at), dl.ts(at)))
        finally:
            ledger.close()
        real = json.loads(json.dumps(sync.ledger_digest(ledger_root, hours=24, now=NOW, config=cfg)))
        section = summary.creates_section(real, expected=True, window=summary._window(dict(WINDOW)))
        self.assertEqual(section["live"], {"created": 1, "uncertain": 0, "rejected": 0})
        self.assertEqual((section["backfill"]["creates"], section["backfill"]["writes"], section["backfill"]["live_writes_per_hour_max"]), (3, 3, 1))
        stops, _ = summary.decide(summary.task_section(task_fixture()), summary.config_section(config_fixture(shopify_create_enabled=True), True),
                                  summary.digest_section(real, None), "GUEST", section)
        self.assertEqual(stops, [])


class Bf2cUnfinishedBackfillTests(SummaryTestCase):
    """#155 BF2c (F2): an unfinished backfill-members call is a STOP (unfinished_runs), never a
    pass, inside a window or not, with or without the BF2b settings."""

    run_with = Bf2bDecisionTests.run_with

    def test_one_unfinished_call_stops_everywhere(self):
        part = backfill_part(unfinished=1)
        cases = (  # the Bf2bDecisionTests cases, each now with unfinished_runs first
            ("window", 41, dict(expected=True, window=dict(WINDOW)), ["unfinished_runs"]),
            ("window_not_expected", 40, dict(expected=False, window=dict(WINDOW)), ["unfinished_runs"]),
            ("expected_no_window", 41, dict(expected=True, window=None), ["unfinished_runs", "shopify_write", "writes_over_cap"]),
        )
        for name, created, kwargs, stops in cases:
            with self.subTest(name=name):
                event = self.run_with(creates_digest(created=created, backfill=part, hour_max=created), **kwargs)
                self.assertEqual(event["stop_reasons"], stops)
                self.assertEqual(event["digest"]["alerts"][summary.BACKFILL_SHOWN], 40)
                self.assertEqual(event["digest"]["runs_unfinished"], 0)  # the event's shape is unchanged
                summary.check_event(event)
        # Keys absent (the BF2a rules): it stops too.
        plain = self.run_daily(runner=FakeRunner(digest=creates_digest(backfill=backfill_part(creates=0, writes=0, writes_per_call_max=0,
                                                                                                  writes_per_hour_max=0, unfinished=1))))
        self.assertEqual(plain["stop_reasons"], ["unfinished_runs"])
        self.assertNotIn(summary.BACKFILL_SHOWN, plain["digest"]["alerts"])

    def test_without_an_unfinished_call_nothing_changes(self):
        for part in (backfill_part(), backfill_part(unfinished=0)):
            with self.subTest(part=part):
                self.assertEqual(self.run_with(creates_digest(created=41, backfill=part, hour_max=41), expected=True, window=dict(WINDOW))["stop_reasons"], [])
        # One unfinished live run is still no stop (as before); two still are.
        self.assertEqual(self.run_daily(runner=FakeRunner(digest=digest_fixture(runs={"completed": 90, "unfinished": 1})))["stop_reasons"], [])
        self.assertEqual(self.run_daily(runner=FakeRunner(digest=digest_fixture(runs={"completed": 90, "unfinished": 2})))["stop_reasons"], ["unfinished_runs"])

    def test_the_reader_is_bounded_and_fails_to_zero(self):
        for raw, expected in ((None, 0), ({}, 0), ({"backfill_members": None}, 0), ({"backfill_members": {}}, 0),
                              ({"backfill_members": {"unfinished": "1"}}, 0), ({"backfill_members": {"unfinished": True}}, 0),
                              ({"backfill_members": {"unfinished": -1}}, 0), ({"backfill_members": {"unfinished": 2}}, 2), ("x", 0)):
            with self.subTest(raw=raw):
                self.assertEqual(summary.backfill_unfinished(raw), expected)

    def test_a_digest_that_failed_is_digest_failed_only(self):
        value = {**self.settings_value(), "shopify_creates_expected": True, "backfill_members_window": dict(WINDOW)}
        event = self.run_daily(runner=FakeRunner(digest=creates_digest(backfill=backfill_part(unfinished=1)),
                                                 codes={"digest": 2}), settings=summary.parse_settings(value))
        self.assertEqual(event["stop_reasons"], ["digest_failed"])

    def test_real_digest_with_a_killed_backfill_call_end_to_end(self):
        import xb_member_sync as sync
        import xb_directlocal as dl

        ledger_root = self.temp / "ledger"
        ledger_root.mkdir()
        cfg = sync.parse_config({"schema": sync.CONFIG_SCHEMA, "state_root": str(ledger_root), "autocount_book": "AED_TESTINGAPI",
                                 "shopify": {"store_gid": "gid://shopify/Shop/900000001", "shop_domain": "xb-sync-test.synthetic.invalid",
                                             "api_version": "2026-07", "auth": {"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_SYNC_TOKEN"}},
                                 "shopify_cutover_utc": "2026-11-01T00:00:00Z"})
        ledger = sync.SyncLedger(ledger_root, clock=lambda: NOW, check_custody=False)
        start = NOW - timedelta(hours=2)
        try:
            ledger.init_meta(autocount_book="AED_TESTINGAPI", store_gid=cfg.shopify.store_gid, shop_domain=cfg.shopify.shop_domain)
            with ledger.tx() as conn:
                conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('run-1', ?, ?, ?)",
                             (dl.ts(start), dl.ts(start + timedelta(seconds=30)), json.dumps({"counts": {}, "alerts": [], "refs": []})))
                # Killed after one create landed and with the next one sent: no end, no counts.
                conn.execute("INSERT INTO runs(run_id, started_at) VALUES ('bf2-1', ?)", (dl.ts(start + timedelta(minutes=2)),))
                for i, state in enumerate(("DONE", "INTENT")):
                    at = start + timedelta(minutes=3)
                    conn.execute("INSERT INTO writes(write_id, direction, member_no, member_no_key, state, created_at, updated_at) "
                                 "VALUES (?, 'ac2_to_shopify', ?, ?, ?, ?, ?)", (f"write-{i}", f"M{i}", f"m{i}", state, dl.ts(at), dl.ts(at)))
                conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('run-2', ?, ?, ?)",
                             (dl.ts(start + timedelta(minutes=15)), dl.ts(start + timedelta(minutes=16)), json.dumps({"counts": {}, "alerts": [], "refs": []})))
        finally:
            ledger.close()
        real = json.loads(json.dumps(sync.ledger_digest(ledger_root, hours=24, now=NOW, config=cfg)))
        self.assertEqual({k: real["backfill_members"][k] for k in ("calls", "creates", "unfinished", "writes")},
                         {"calls": 1, "creates": 1, "unfinished": 1, "writes": 2})
        for kwargs in (dict(expected=True, window=dict(WINDOW)), dict(expected=False, window=None)):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                event = self.run_with(real, config=config_fixture(shopify_create_enabled=True), **kwargs)
                self.assertIn("unfinished_runs", event["stop_reasons"])
        event = self.run_with(real, expected=True, window=dict(WINDOW))
        self.assertEqual((event["stop_reasons"], event["digest"]["alerts"][summary.BACKFILL_SHOWN]), (["unfinished_runs"], 1))


# The summary's own golden output (pass bar: with the BF2b keys absent, unchanged). One trace of
# every case below through daily(): the result line and the whole record. Its SHA-256 was made on
# the main 3340b03 source of xb_member_sync_daily_summary.py (loaded in place of this one, same
# cases and fixtures; scripted, BF2b G1) and is equal for this source with the keys absent and
# with them written at their defaults.
SUMMARY_GOLDEN_SHA256 = "72b3b53bdc8084f7d12964dda26ed8ed1c832dab66c1e0d80b07b5c0e49b2bb0"


def summary_golden_cases():
    return (
        ("clean", {}),
        ("created", {"digest": creates_digest(created=2, hour_max=2)}),
        ("uncertain", {"digest": creates_digest(uncertain=1, opened={"uncertain_writes": 1})}),
        ("rejected", {"digest": creates_digest(rejected=1)}),
        ("creates_on", {"config": config_fixture(shopify_create_enabled=True), "digest": creates_digest(created=1)}),
        ("backfill_part", {"digest": creates_digest(created=41, backfill=backfill_part(), hour_max=41)}),
        ("backfill_part_quiet", {"digest": creates_digest(backfill=backfill_part(creates=0, writes=0, writes_per_call_max=0, writes_per_hour_max=0))}),
        ("over_cap", {"digest": creates_digest(hour_max=7)}),
        ("alerts", {"digest": digest_fixture(alerts={"possible_duplicate_member": 1, "changelog_row_unparsed": 1, "autocount_x": 1})}),
        ("bad_task", {"task": task_fixture(last_result=1, state="Disabled")}),
        ("no_runs", {"digest": digest_fixture(runs={"completed": 0, "unfinished": 3})}),
        ("check_config_fails", {"codes": {"check-config": 2}}),
        ("digest_fails", {"codes": {"digest": 2}}),
    )


def summary_golden_trace(module, settings_value: dict, out_dir: Path) -> list:
    trace = []
    for name, case in summary_golden_cases():
        for path in out_dir.glob("*.json"):
            path.unlink()
        runner = FakeRunner(task=case.get("task"), config=case.get("config"), digest=case.get("digest"), codes=case.get("codes"))
        posted = Poster()
        result = module.daily(module.parse_settings(settings_value), now=NOW, runner=runner,
                              sql_reader=sql_reader_for(FakeConnection()), cred_reader=lambda target: "FAKE-SECRET-0001", poster=posted)
        record = json.loads((out_dir / f"daily_summary_{TODAY}.json").read_text(encoding="utf-8"))
        trace.append({"name": name, "result": result, "record": record, "posted": [call["body"] for call in posted.calls]})
    return trace


def summary_trace_digest(trace: list) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(trace, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class SummaryGoldenTests(SummaryTestCase):
    def base_value(self):
        value = self.settings_value()
        for key in ("shopify_creates_expected", "backfill_members_window"):
            value.pop(key, None)
        return value

    def test_keys_absent_match_the_main_output(self):
        trace = summary_golden_trace(summary, self.base_value(), self.out)
        self.assertEqual(summary_trace_digest(trace), SUMMARY_GOLDEN_SHA256, json.dumps([(t["name"], t["result"]["stop_reasons"]) for t in trace]))

    def test_keys_at_their_defaults_match_too(self):
        value = {**self.base_value(), "shopify_creates_expected": False, "backfill_members_window": None}
        self.assertEqual(summary_trace_digest(summary_golden_trace(summary, value, self.out)), SUMMARY_GOLDEN_SHA256)

    def test_a_window_that_is_not_in_effect_changes_nothing_while_creates_are_not_expected(self):
        value = {**self.base_value(), "backfill_members_window": {**WINDOW, "from": "2026-11-20", "to": "2026-11-21"}}
        self.assertEqual(summary_trace_digest(summary_golden_trace(summary, value, self.out)), SUMMARY_GOLDEN_SHA256)


# ------------------------------------------------------------------------------------------------
# #288 member email wiring (design mailbox 288 G0, 14 Nov 2026): the member core and tags digests
# ------------------------------------------------------------------------------------------------

CORE_SCRIPT = ROOT / "directlocal" / "xb_member_benefits.py"
TAGS_SCRIPT = ROOT / "directlocal" / "xb_member_tags.py"
FAKE_AT = "x@example.com"


def tags_digest(**email):
    """A tags ``digest`` answer: the fixed email block plus parts the summary must never read."""

    block = {"runs_completed": 12, "runs_unfinished": 0, "tagged_now": 812, "bday_tagged_now": 40, "would_tag": 3, "would_untag": 0,
             "would_bday_tag": 2, "would_bday_untag": 0, "alerts": [], "stale_month": False, "open_writes": 0}
    block.update(email)
    return {"status": "ok", "command": "digest", "hours": 24, "runs": {"completed": 12, "unfinished": 0}, "counts": {"tag_added": 3},
            "alerts": {code: 1 for code in block["alerts"] if isinstance(code, str)} if isinstance(block["alerts"], list) else {},
            "alert_refs": [{"ref": "c-" + "1" * 20, "code": "tag_not_written", "member_no_masked": "*******042"}],
            "open": {"uncertain_writes": 0, "intent_writes": 0}, "shadow": {}, "tagged_now": 812, "carriers_total": 900,
            "bday_tagged_now": 40, "bday_carriers_total": 41, "stale_month": False, "email": block}


def core_digest(alerts=None, runs=None, opened=None, shadow=None, **overrides):
    """A member core ``digest`` answer (the shape of its ledger_digest), fake figures only."""

    value = {"schema": summary.CORE_DIGEST_SCHEMA, "status": "ok", "autocount_book": "AED_XBOUNDARIES", "hours": 24,
             "rule_settings_match": True, "runs": {"completed": 96, "unfinished": 0, **(runs or {})},
             "counts": {"orders_read": 40}, "alerts": dict(alerts or {}),
             "alert_refs": [{"ref": "o-" + "123456" + "ab", "code": "claim_needs_review", "member_no_masked": "*******042"}],
             "receipts": {"ready": 4, "waiting_member": 2}, "shadow": {"would_upgrade": 3, "would_extend": 1, **(shadow or {})},
             "gift_cards": {"receipts_paid_by_gift_card": 1, "gateways_seen": {"gift_card": 1}},
             "open": {"uncertain_writes": 0, "held_writes": 0, "waiting_member": 2, "claim_links": 5, "claim_links_voided": 0, **(opened or {})}}
    value.update(overrides)
    return value


class ToolRunner(FakeRunner):
    """FakeRunner plus the two member tools, answered by their script name. ``log`` is shared with
    the SQL reader and the poster, so the order of every step can be checked."""

    def __init__(self, core=None, tags=None, tool_codes=None, log=None, **kwargs):
        super().__init__(**kwargs)
        self.tools = {"xb_member_benefits.py": core_digest() if core is None else core,
                      "xb_member_tags.py": tags_digest() if tags is None else tags}
        self.tool_codes = tool_codes or {}
        self.timeouts: list[int] = []
        self.log = log if log is not None else []

    def __call__(self, argv, timeout):
        script = Path(argv[1]).name if len(argv) > 1 else ""
        self.timeouts.append(timeout)
        if script not in self.tools:
            self.log.append("task-state" if argv[0].lower().endswith("powershell.exe") else argv[2])
            return super().__call__(argv, timeout)
        self.log.append(script)
        self.calls.append(list(argv))
        answer = self.tools[script]
        if isinstance(answer, BaseException):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return self.tool_codes.get(script, 0), "tool noise line\n" + text + "\n"


class WiringTestCase(SummaryTestCase):
    def setUp(self):
        super().setUp()
        self.core_root = self.temp / "core-state"
        self.tags_root = self.temp / "tags-state"
        for root in (self.core_root, self.tags_root):
            root.mkdir()

    def tools_value(self, core_on=False, tags_on=False, **changes):
        """``changes``: per tool name (member_core, tags), the keys to change in that tool's part."""

        value = {"member_core": {"enabled": core_on, "config_path": str(self.temp / "member_benefits_live.json"), "state_root": str(self.core_root),
                                 "timeout_seconds": 20},
                 "tags": {"enabled": tags_on, "config_path": str(self.temp / "member_tags_live.json"), "state_root": str(self.tags_root),
                          "timeout_seconds": 20}}
        for name, item in changes.items():
            value[name] = {**value[name], **item}
        return value

    def raw(self, tools, **overrides):
        """The settings file with member_tools set exactly as given (never merged)."""

        value = self.settings_value(**overrides)
        value["member_tools"] = tools
        return value

    def wired(self, core_on=False, tags_on=False, **changes):
        return summary.parse_settings(self.raw(self.tools_value(core_on, tags_on, **changes)))

    def run_wired(self, core=False, tags=False, runner=None, **kwargs):
        return self.run_daily(runner=runner or ToolRunner(), settings=self.wired(core, tags), **kwargs)

    def logged_run(self, core, tags, runner=None):
        """daily() with one log for the children, the SQL part and the send."""

        log: list[str] = []
        runner = runner or ToolRunner(log=log)
        runner.log = log
        connection = FakeConnection()
        reader = sql_reader_for(connection)

        def sql_reader(sql, book):
            log.append("sql")
            return reader(sql, book)

        def poster(url, headers, body, timeout):
            log.append("send")
            return 200

        self.runner = runner
        result = summary.daily(self.wired(core, tags), now=NOW, runner=runner, sql_reader=sql_reader,
                               cred_reader=lambda target: "FAKE-SECRET-0001", poster=poster)
        return log, result


class WiringGoldenTests(WiringTestCase):
    """T1: both switches off, and the key absent, give main's event, record and stdout exactly."""

    def test_absent_and_both_off_match_the_main_golden_output(self):
        absent = self.settings_value()
        absent.pop("member_tools")
        both_off = self.raw(self.tools_value(False, False))
        for value in (absent, both_off, self.settings_value()):
            with self.subTest(member_tools=value.get("member_tools")):
                self.assertEqual(summary_trace_digest(summary_golden_trace(summary, value, self.out)), SUMMARY_GOLDEN_SHA256)

    def test_both_off_starts_no_tool_and_sends_a_v1_event(self):
        log, result = self.logged_run(False, False)
        self.assertEqual(log, ["task-state", "check-config", "digest", "sql", "send"])
        record = json.loads((self.out / f"daily_summary_{TODAY}.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(record["event"]), summary.EVENT_KEYS)
        self.assertEqual(record["event"]["schema"], summary.EVENT_SCHEMA)
        self.assertNotIn("overall", result)


class WiringSettingsTests(WiringTestCase):
    """T2: settings accept and refuse."""

    def refused(self, value):
        with self.assertRaises(summary.SummaryError) as caught:
            summary.parse_settings(value)
        return caught.exception.code

    def test_the_example_has_both_tools_off_with_placeholder_paths(self):
        example = json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))
        tools = example["member_tools"]
        self.assertEqual(list(tools), ["member_core", "tags"])
        for name, tool in tools.items():
            self.assertEqual(set(tool), {"enabled", "config_path", "state_root", "timeout_seconds"})
            self.assertIs(tool["enabled"], False, name)
            self.assertTrue(tool["config_path"].startswith("C:\\XB\\example\\") and tool["state_root"].startswith("C:\\XB\\example\\"))
            self.assertEqual(tool["timeout_seconds"], 20)
        parsed = summary.parse_settings(example)
        self.assertEqual(summary.enabled_tools(parsed), ())
        # Both on: 3 x 40 + 20 + 20 + 5 x 15 + 20 = 255 s, inside 285 (Controller Q2).
        on = json.loads(json.dumps(example))
        for tool in on["member_tools"].values():
            tool["enabled"] = True
        self.assertEqual(summary.worst_case_seconds(summary.parse_settings(on)), 255)

    def test_every_key_and_bound_is_checked(self):
        self.assertEqual(self.wired(True, True)["member_tools"]["tags"]["timeout_seconds"], 20)
        for bad in (None, [], {}, {"member_core": self.tools_value()["member_core"]},
                    {**self.tools_value(), "sync": self.tools_value()["tags"]}):
            with self.subTest(member_tools=bad):
                self.assertEqual(self.refused(self.raw(bad)), "settings_member_tools_invalid")
        for name in summary.TOOL_NAMES:
            code = "settings_member_tools_%s_invalid" % name
            changes = [{"enabled": "yes"}, {"enabled": 1}, {"enabled": None},
                       {"config_path": "relative\\member.json"}, {"config_path": ""}, {"config_path": 5}, {"config_path": " C:\\x.json"},
                       {"state_root": "relative\\state"}, {"state_root": str(self.temp / "a" / ".." / "b")}, {"state_root": None},
                       {"timeout_seconds": 9}, {"timeout_seconds": 61}, {"timeout_seconds": True}, {"timeout_seconds": "20"},
                       {"timeout_seconds": 20.0}]
            for change in changes:
                with self.subTest(tool=name, change=change):
                    value = self.raw(self.tools_value(**{name: change}))
                    self.assertEqual(self.refused(value), code)
            for broken in ("extra", "missing"):
                tools = self.tools_value()
                if broken == "extra":
                    tools[name]["command"] = "digest"  # a command is never configurable
                else:
                    del tools[name]["timeout_seconds"]
                with self.subTest(tool=name, broken=broken):
                    self.assertEqual(self.refused(self.raw(tools)), code)
            with self.subTest(tool=name, kind="not a dict"):
                self.assertEqual(self.refused(self.raw({**self.tools_value(), name: True})), code)
        for timeout in (10, 60):
            self.assertEqual(self.wired(member_core={"timeout_seconds": timeout})["member_tools"]["member_core"]["timeout_seconds"], timeout)

    def test_the_time_budget_adds_each_enabled_tool_only(self):
        settings = self.wired(False, False, member_core={"timeout_seconds": 60}, tags={"timeout_seconds": 60})
        self.assertEqual(summary.worst_case_seconds(settings), 3 * 40 + 5 * 15 + 20)  # off: not counted
        self.assertEqual(summary.worst_case_seconds(self.wired(True, False)), 3 * 40 + 20 + 5 * 15 + 20)
        self.assertEqual(summary.worst_case_seconds(self.wired(True, True)), 255)
        self.assertEqual(summary.worst_case_seconds(self.wired(True, True, tags={"timeout_seconds": 50})), 285)
        for core, tags, changes in ((True, True, {"tags": {"timeout_seconds": 51}}),
                                    (True, False, {"member_core": {"timeout_seconds": 60}, "child_timeout_seconds": 50})):
            with self.subTest(core=core, tags=tags, changes=changes):
                child = changes.pop("child_timeout_seconds", 40)
                value = self.raw(self.tools_value(core, tags, **changes), child_timeout_seconds=child)
                # The same file with every tool off fits: only an enabled tool counts.
                summary.parse_settings(self.raw(self.tools_value(False, False, **changes), child_timeout_seconds=child))
                self.assertEqual(self.refused(value), "settings_timeouts_exceed_task_limit")

    def test_output_dir_inside_a_tool_state_root_is_refused_written_and_resolved(self):
        for name in summary.TOOL_NAMES:
            root = self.core_root if name == "member_core" else self.tags_root
            for folder in (root, root / "reports"):
                for enabled in (False, True):  # whether on or off: the record never goes into a tool's state
                    with self.subTest(tool=name, folder=folder, enabled=enabled):
                        value = self.raw(self.tools_value(enabled, enabled), output_dir=str(folder))
                        self.assertEqual(self.refused(value), "output_dir_inside_tool_state")
        # The resolved check itself (check-settings never gets this far with such a file).
        settings = self.wired(True, True)
        settings["member_tools"]["tags"]["state_root"] = str(self.out)
        with self.assertRaises(summary.SummaryError) as caught:
            summary.check_output_dir(settings)
        self.assertEqual(caught.exception.code, "output_dir_inside_tool_state")
        link = self.temp / "tags-link"
        try:
            os.symlink(self.out, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return  # no symlink right on this machine: the written check above still holds
        settings = self.wired(True, True)
        settings["member_tools"]["tags"]["state_root"] = str(link)
        with self.assertRaises(summary.SummaryError) as caught:
            summary.check_output_dir(settings)
        self.assertEqual(caught.exception.code, "output_dir_inside_tool_state")

    def test_check_settings_prints_both_switches(self):
        path = self.temp / "settings.json"
        for core, tags in ((False, False), (True, False), (False, True), (True, True)):
            path.write_text(json.dumps(self.raw(self.tools_value(core, tags))), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = summary.main(["check-settings", "--settings", str(path)])
            answer = json.loads(out.getvalue())
            self.assertEqual((code, answer["member_core_enabled"], answer["tags_enabled"]), (0, core, tags))
        absent = self.settings_value()
        absent.pop("member_tools")
        path.write_text(json.dumps(absent), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            summary.main(["check-settings", "--settings", str(path)])
        answer = json.loads(out.getvalue())
        self.assertEqual((answer["status"], answer["member_core_enabled"], answer["tags_enabled"]), ("ok", False, False))


class WiringChildrenTests(WiringTestCase):
    """T3: pinned argv per tool, the order, and a disabled tool is never started."""

    def test_argv_order_and_timeouts(self):
        log, result = self.logged_run(True, True)
        self.assertEqual(log, ["task-state", "check-config", "digest", "xb_member_benefits.py", "xb_member_tags.py", "sql", "send"])
        tool_calls = self.runner.calls[-2:]
        self.assertEqual(tool_calls, [
            [sys.executable, str(CORE_SCRIPT), "digest", "--config", str(self.temp / "member_benefits_live.json"), "--hours", "24"],
            [sys.executable, str(TAGS_SCRIPT), "digest", "--config", str(self.temp / "member_tags_live.json"), "--hours", "24"],
        ])
        self.assertEqual(self.runner.timeouts, [40, 40, 40, 20, 20])
        self.assertEqual(result["overall"], "CLEAN")

    def test_a_disabled_tool_is_never_started(self):
        for core, tags, started in ((True, False, ["xb_member_benefits.py"]), (False, True, ["xb_member_tags.py"])):
            with self.subTest(core=core, tags=tags):
                log, _ = self.logged_run(core, tags)
                self.assertEqual([step for step in log if step.endswith(".py")], started)
                event = json.loads((self.out / f"daily_summary_{TODAY}.json").read_text(encoding="utf-8"))["event"]
                self.assertEqual(event["schema"], summary.EVENT_SCHEMA_V2)
                self.assertEqual([name for name, part in event["member_tools"].items() if part is None],
                                 ["tags"] if core else ["member_core"])

    def test_any_other_tool_command_is_refused_before_a_process_starts(self):
        runner = ToolRunner()
        settings = self.wired(True, True)
        for name in summary.TOOL_NAMES:
            for command in ("observe", "apply", "init", "status", "check-config", "resolve", "index", "release-held", "unlink",
                            "claim-links", "digest --apply"):
                with self.subTest(tool=name, command=command), self.assertRaises(summary.SummaryError) as caught:
                    summary.run_tool(name, command, settings, runner)
                self.assertEqual(caught.exception.code, "tool_command_not_allowed")
        with self.assertRaises(summary.SummaryError):
            summary.run_tool("sync", "digest", settings, runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(summary.TOOL_COMMANDS, ("digest",))

    def test_static_only_digest_and_only_the_sibling_scripts(self):
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_tool"]
        self.assertEqual([(type(call.args[1]).__name__, call.args[1].value) for call in calls], [("Constant", "digest")])
        constants = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        for word in ("observe", "apply", "resolve", "index", "release-held", "unlink", "claim-links", "--apply", "--write"):
            self.assertNotIn(word, constants, word)
        here = SOURCE.resolve().parent
        self.assertEqual(summary.TOOL_SCRIPTS, {"member_core": here / "xb_member_benefits.py", "tags": here / "xb_member_tags.py"})
        self.assertIn('"member_core": Path(__file__).resolve().parent / "xb_member_benefits.py"', source)
        self.assertIn('"tags": Path(__file__).resolve().parent / "xb_member_tags.py"', source)
        # A tool's settings name no script, command, interpreter or argument.
        self.assertEqual(summary._TOOL_KEYS, {"enabled", "config_path", "state_root", "timeout_seconds"})
        # The tools are child processes only: never imported, never their ledgers.
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse({"xb_member_benefits", "xb_member_tags", "xb_directlocal"} & imported)


class WiringTagsRuleTests(WiringTestCase):
    """T4: the tags rule table, every reason and CLEAN."""

    def verdict(self, **email):
        section = summary.tags_section(tags_digest(**email))
        return section["status"], section["reasons"]

    def test_clean_and_every_reason(self):
        self.assertEqual(self.verdict(), ("CLEAN", []))
        cases = [
            (dict(runs_completed=0), ("STOP", ["no_runs"])),
            (dict(runs_unfinished=1), ("CLEAN", [])),  # the run that may be in progress
            (dict(runs_unfinished=2), ("STOP", ["unfinished_runs"])),
            (dict(runs_unfinished=0, open_writes=1), ("STOP", ["open_writes"])),
            (dict(runs_unfinished=1, open_writes=1), ("WARN", ["open_writes_run_in_progress"])),
            (dict(runs_unfinished=2, open_writes=1), ("STOP", ["unfinished_runs"])),
            (dict(alerts=["write_uncertain"]), ("STOP", ["write_alert"])),
            (dict(alerts=["tag_removes_capped", "write_unresolved"]), ("STOP", ["write_alert", "alerts"])),
            (dict(alerts=["tag_removes_capped"]), ("WARN", ["alerts"])),
            (dict(stale_month=True), ("WARN", ["stale_month"])),
            (dict(alerts=["code_%02d" % n for n in range(19)]), ("WARN", ["alerts"])),
            (dict(alerts=["code_%02d" % n for n in range(20)]), ("STOP", ["alerts_at_cap", "alerts"])),
            (dict(runs_completed=0, runs_unfinished=1, open_writes=1, alerts=["write_uncertain"], stale_month=True),
             ("STOP", ["no_runs", "write_alert", "open_writes_run_in_progress", "stale_month"])),
        ]
        for email, expected in cases:
            with self.subTest(email=email):
                self.assertEqual(self.verdict(**email), expected)
        self.assertEqual(summary.TAGS_INFO_ALERTS, ())

    def test_an_unreadable_code_is_shown_as_unreadable_and_still_counts(self):
        for code in ("bday_untag_reason:tag_without_basis", "code_" + "123456", "phone_" + FAKE_PHONE, FAKE_AT, "Bad Code"):
            with self.subTest(code=code):
                section = summary.tags_section(tags_digest(alerts=[code, "tag_removes_capped"]))
                self.assertEqual(section["alerts"], ["tag_removes_capped", summary.UNREADABLE_ALERT])
                self.assertEqual((section["status"], section["reasons"]), ("WARN", ["alerts"]))
                self.assertNotIn(code, json.dumps(section))
        both = summary.tags_section(tags_digest(alerts=["a:b", "c:d"]))
        self.assertEqual(both["alerts"], [summary.UNREADABLE_ALERT])

    def test_a_bad_email_block_is_digest_failed(self):
        bad_blocks = []
        missing = tags_digest()
        del missing["email"]
        bad_blocks.append(missing)
        for change in ({"extra": 1}, {"runs_completed": "12"}, {"open_writes": True}, {"open_writes": -1}, {"stale_month": 1},
                       {"stale_month": None}, {"alerts": "write_uncertain"}, {"alerts": [1]}, {"alerts": None},
                       {"alerts": ["code_%02d" % n for n in range(21)]}, {"tagged_now": 1.5}):
            bad_blocks.append(tags_digest(**change))
        no_alerts = tags_digest()
        del no_alerts["email"]["alerts"]
        bad_blocks += [no_alerts, {**tags_digest(), "email": None}, {**tags_digest(), "email": []}]
        for raw in bad_blocks:
            with self.subTest(email=raw.get("email")):
                section = summary.tags_section(raw)
                self.assertEqual(section, summary.tool_failed("tags", "email_block_invalid"))
                self.assertEqual((section["status"], section["reasons"], section["available"]), ("STOP", ["digest_failed"], False))

    def test_counts_are_capped_and_the_other_digest_parts_are_never_read(self):
        section = summary.tags_section(tags_digest(tagged_now=10 ** 12))
        self.assertEqual(section["tagged_now"], summary.COUNT_MAX)
        raw = tags_digest()
        raw["alerts"] = {"write_uncertain": 9}  # outside the email block: not read
        raw["open"] = {"uncertain_writes": 9, "intent_writes": 9}
        self.assertEqual(summary.tags_section(raw)["status"], "CLEAN")
        self.assertEqual(list(summary.tags_section(raw)), list(summary.TOOL_SECTION_HEAD + summary.TAGS_FIELDS))

    def test_child_failures(self):
        refused = {"status": "refused", "code": "tags_ledger_missing"}
        cases = [
            (dict(tags=refused, tool_codes={"xb_member_tags.py": 2}), "tags_ledger_missing"),
            (dict(tags={"status": "refused", "code": "state_root_acl_unsafe"}, tool_codes={"xb_member_tags.py": 2}), "state_root_acl_unsafe"),
            (dict(tags={"status": "refused", "code": "Bad Code"}, tool_codes={"xb_member_tags.py": 2}), "unreadable_refusal"),
            (dict(tags={"status": "refused", "code": "x_" + FAKE_PHONE}, tool_codes={"xb_member_tags.py": 2}), "unreadable_refusal"),
            (dict(tags={"status": "refused", "code": FAKE_AT}, tool_codes={"xb_member_tags.py": 2}), "unreadable_refusal"),
            (dict(tags={"status": "refused", "code": 7}, tool_codes={"xb_member_tags.py": 2}), "unreadable_refusal"),
            (dict(tags=subprocess.TimeoutExpired(["x"], 20)), "timed_out"),
            (dict(tags=OSError("no python")), "no_answer"),
            (dict(tags=tags_digest(), tool_codes={"xb_member_tags.py": 3}), "exit_code"),
            (dict(tags="Traceback (most recent call last):", tool_codes={"xb_member_tags.py": 1}), "exit_code"),
            (dict(tags="no json at all"), "no_answer"),
            (dict(tags={"status": "other"}), "no_answer"),
        ]
        for runner_args, refusal in cases:
            with self.subTest(refusal=refusal, args=str(runner_args)[:60]):
                event = self.run_wired(False, True, runner=ToolRunner(**runner_args))
                section = event["member_tools"]["tags"]
                self.assertEqual(section, summary.tool_failed("tags", refusal))
                self.assertEqual(self.result["overall"], "STOP")
                self.assertEqual(event["result"], "CLEAN")  # the sync's own result is untouched
                self.assertEqual(self.record["email_status"], "sent")


class WiringCoreRuleTests(WiringTestCase):
    """T5: the member core rule table."""

    def verdict(self, raw):
        section = summary.core_section(raw)
        return section["status"], section["reasons"]

    def test_clean_and_every_reason(self):
        self.assertEqual(self.verdict(core_digest()), ("CLEAN", []))
        cases = [
            (core_digest(rule_settings_match=False), ("STOP", ["rule_settings_mismatch"])),
            (core_digest(runs={"completed": 0}), ("STOP", ["no_runs"])),
            (core_digest(runs={"unfinished": 1}), ("CLEAN", [])),
            (core_digest(runs={"unfinished": 2}), ("STOP", ["unfinished_runs"])),
            (core_digest(opened={"uncertain_writes": 1}), ("STOP", ["uncertain_writes"])),
            (core_digest(opened={"uncertain_writes": 1}, runs={"unfinished": 1}), ("WARN", ["uncertain_writes_run_in_progress"])),
            (core_digest(opened={"uncertain_writes": 1}, runs={"unfinished": 2}), ("STOP", ["unfinished_runs"])),
            (core_digest(opened={"held_writes": 1}), ("WARN", ["held_writes"])),
            (core_digest(alerts={"orders_read_incomplete": 2, "member_reads_capped": 1}), ("CLEAN", [])),  # INFO: no status change
            (core_digest(alerts={"orders_read_incomplete": 2, "claim_needs_review": 1}), ("WARN", ["alerts"])),
            (core_digest(alerts={"bad:code": 1}), ("WARN", ["alerts"])),
            (core_digest(rule_settings_match=False, runs={"completed": 0, "unfinished": 1}, opened={"uncertain_writes": 1, "held_writes": 2},
                         alerts={"claim_needs_review": 1}),
             ("STOP", ["rule_settings_mismatch", "no_runs", "uncertain_writes_run_in_progress", "held_writes", "alerts"])),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw["runs"], opened=raw["open"], alerts=raw["alerts"]):
                self.assertEqual(self.verdict(raw), expected)
        self.assertEqual(summary.CORE_INFO_ALERTS, ("orders_read_incomplete", "member_reads_capped"))

    def test_a_wrong_schema_or_pinned_key_is_digest_failed(self):
        bad = [core_digest(schema="xb.directlocal.member_benefits.digest.v2"), {k: v for k, v in core_digest().items() if k != "schema"},
               core_digest(rule_settings_match="yes"), core_digest(rule_settings_match=None), core_digest(runs={"completed": True}),
               core_digest(runs={"unfinished": -1}), core_digest(opened={"held_writes": "0"}), core_digest(shadow={"would_extend": None}),
               core_digest(alerts={"claim_needs_review": "2"}), core_digest(alerts={"claim_needs_review": True}),
               {**core_digest(), "alerts": []}, {**core_digest(), "alerts": None}, core_digest(open=None)]
        missing = core_digest()
        del missing["shadow"]["would_upgrade"]
        bad.append(missing)
        no_alerts = core_digest()
        del no_alerts["alerts"]
        bad.append(no_alerts)
        for raw in bad:
            with self.subTest(raw=json.dumps(raw)[:200]):
                self.assertEqual(summary.core_section(raw), summary.tool_failed("member_core", "digest_schema_unknown"))

    def test_more_than_twenty_alerts_keep_the_sync_bound(self):
        alerts = {"code_%02d" % n: n + 1 for n in range(25)}
        section = summary.core_section(core_digest(alerts=alerts))
        self.assertEqual(len(section["alerts"]), summary.MAX_ALERT_CODES)
        self.assertEqual(section["alerts"][summary.OTHER_ALERTS], sum(range(20, 26)))
        self.assertEqual((section["status"], section["reasons"]), ("WARN", ["alerts"]))
        # Only INFO codes, more than 20 of them in all, is still CLEAN: the decision reads every code.
        self.assertEqual(self.verdict(core_digest(alerts={"orders_read_incomplete": 1})), ("CLEAN", []))

    def test_only_the_pinned_keys_are_read(self):
        section = summary.core_section(core_digest(open={"uncertain_writes": 0, "held_writes": 0, "waiting_member": 2},
                                                   gift_cards={"gateways_seen": {"x_" + FAKE_PHONE: 1}}, receipts=None, counts=None,
                                                   alert_refs="not read", autocount_book="OTHER"))
        self.assertEqual(section["status"], "CLEAN")
        self.assertEqual(list(section), list(summary.TOOL_SECTION_HEAD + summary.CORE_FIELDS))
        self.assertEqual({k: section[k] for k in ("waiting_member", "would_upgrade", "would_extend", "rule_settings_match")},
                         {"waiting_member": 2, "would_upgrade": 3, "would_extend": 1, "rule_settings_match": True})

    def test_child_failures(self):
        for runner_args, refusal in ((dict(core={"status": "refused", "code": "benefits_ledger_missing"},
                                           tool_codes={"xb_member_benefits.py": 2}), "benefits_ledger_missing"),
                                     (dict(core=subprocess.TimeoutExpired(["x"], 20)), "timed_out"),
                                     (dict(core=core_digest(schema="other")), "digest_schema_unknown")):
            with self.subTest(refusal=refusal):
                event = self.run_wired(True, False, runner=ToolRunner(**runner_args))
                self.assertEqual(event["member_tools"]["member_core"], summary.tool_failed("member_core", refusal))
                self.assertIsNone(event["member_tools"]["tags"])


class WiringNeverChangesTheSyncTests(WiringTestCase):
    """T6: for every tool status, the sync's part of the event is the event with the tools off."""

    def test_the_sync_part_is_unchanged(self):
        syncs = {
            "clean": dict(),
            "stop": dict(digest=digest_fixture(counts={"shopify_customer_created": 1}, opened={"uncertain_writes": 1},
                                               alerts={"possible_duplicate_member": 1})),
            "task": dict(task=task_fixture(state="Disabled", last_result=1)),
        }
        tools = {
            "clean": dict(core=core_digest(), tags=tags_digest()),
            "warn": dict(core=core_digest(opened={"held_writes": 1}), tags=tags_digest(stale_month=True)),
            "stop": dict(core=core_digest(rule_settings_match=False), tags=tags_digest(alerts=["write_uncertain"])),
            "failed": dict(core={"status": "refused", "code": "benefits_ledger_missing"}, tags=subprocess.TimeoutExpired(["x"], 20),
                           tool_codes={"xb_member_benefits.py": 2}),
        }
        for sync_name, sync in syncs.items():
            off = self.run_daily(runner=FakeRunner(**sync), settings=self.wired(False, False))
            off_result = dict(self.result)
            for tool_name, outcome in tools.items():
                for core, tags in ((True, False), (False, True), (True, True)):
                    with self.subTest(sync=sync_name, tools=tool_name, core=core, tags=tags):
                        event = self.run_wired(core, tags, runner=ToolRunner(**sync, **outcome))
                        self.assertEqual({k: v for k, v in event.items() if k not in ("schema", "member_tools")},
                                         {k: v for k, v in off.items() if k != "schema"})
                        for key in ("result", "stop_reasons", "auto_off", "auto_off_reasons"):
                            self.assertEqual(event[key], off[key], key)
                        self.assertEqual({k: v for k, v in self.result.items() if k != "overall"}, off_result)
                        words = [event["result"]] + [part["status"] for part in event["member_tools"].values() if part]
                        self.assertEqual(self.result["overall"], max(words, key=summary.TOOL_STATUSES.index))

    def test_decide_never_reads_the_tools(self):
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        decide = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "decide")
        self.assertNotIn("member_tools", ast.get_source_segment(source, decide))
        self.assertNotIn("member_tools", [arg.arg for arg in decide.args.args + decide.args.kwonlyargs])


class WiringPrivacyTests(WiringTestCase):
    """T7: fake personal data in each tool field is dropped or refused on the VM; no ref passes."""

    def test_fake_phone_and_email_never_reach_the_event(self):
        bad = ["x_" + FAKE_PHONE, FAKE_AT, FAKE_NAME]
        runs = [
            dict(core=core_digest(alerts={code: 1 for code in bad}, extra_contact={FAKE_AT: FAKE_PHONE}),
                 tags=tags_digest(alerts=bad)),
            dict(core={"status": "refused", "code": "x_" + FAKE_PHONE}, tags={"status": "refused", "code": FAKE_AT},
                 tool_codes={"xb_member_benefits.py": 2, "xb_member_tags.py": 2}),
            dict(core=core_digest(**{"x_" + FAKE_PHONE: 1}), tags={**tags_digest(), "email": {**tags_digest()["email"], FAKE_AT: 1}}),
        ]
        for n, outcome in enumerate(runs):
            with self.subTest(run=n):
                self.run_wired(True, True, runner=ToolRunner(**outcome))
                text = json.dumps(self.poster.calls[0]["body"]) + json.dumps(self.record)
                for word in (FAKE_PHONE, FAKE_AT, "@", "example.com", FAKE_NAME, "o-123456", "c-1111"):
                    self.assertNotIn(word, text)
        # The refs and the gift card names of a real-shaped digest are never carried.
        self.run_wired(True, True)
        text = json.dumps(self.record["event"]["member_tools"])
        for word in ("alert_refs", "member_no", "gateways", "gift_card", "o-", "c-", "***"):
            self.assertNotIn(word, text)

    def test_check_event_refuses_a_tool_section_with_personal_data(self):
        self.run_wired(True, True)
        good = self.record["event"]
        summary.check_event(good)
        cases = []
        for path, value in ((("tags", "alerts"), ["x_" + FAKE_PHONE]), (("tags", "alerts"), [FAKE_AT]),
                            (("member_core", "alerts"), {"x_" + FAKE_PHONE: 1})):
            event = json.loads(json.dumps(good))
            event["member_tools"][path[0]][path[1]] = value
            cases.append(event)
        failed = json.loads(json.dumps(good))
        failed["member_tools"]["tags"] = summary.tool_failed("tags", "x_" + FAKE_PHONE)
        cases.append(failed)
        failed = json.loads(json.dumps(good))
        failed["member_tools"]["tags"] = summary.tool_failed("tags", FAKE_AT)
        cases.append(failed)
        for event in cases:
            with self.subTest(event=json.dumps(event["member_tools"])[:120]), self.assertRaises(summary.SummaryError) as caught:
                summary.check_event(event)
            self.assertIn(caught.exception.code, ("event_member_tools_invalid", "event_personal_data_like"))


class WiringEventShapeTests(WiringTestCase):
    """check_event covers v2: exact keys and closed values per tool."""

    def good(self):
        self.run_wired(True, True, runner=ToolRunner(core=core_digest(opened={"held_writes": 1}), tags=tags_digest(stale_month=True)))
        return self.record["event"]

    def test_v2_shape(self):
        event = self.good()
        self.assertEqual(tuple(event), summary.EVENT_KEYS_V2)
        self.assertEqual(event["schema"], "xb.member_sync.daily_summary.v2")
        self.assertEqual(tuple(event["member_tools"]), summary.TOOL_NAMES)
        self.assertEqual(tuple(event["member_tools"]["tags"]), summary.TOOL_SECTION_HEAD + summary.TAGS_FIELDS)
        self.assertEqual(tuple(event["member_tools"]["member_core"]), summary.TOOL_SECTION_HEAD + summary.CORE_FIELDS)
        self.assertEqual(self.result["overall"], "WARN")

    def test_check_event_refusals(self):
        good = self.good()

        def changed(fn):
            event = json.loads(json.dumps(good))
            fn(event)
            return event

        def tool(name, **changes):
            return lambda e: e["member_tools"][name].update(changes)

        cases = {
            "event_keys_invalid": [changed(lambda e: e.pop("member_tools")), changed(lambda e: e.update(schema=summary.EVENT_SCHEMA))],
            "event_member_tools_invalid": [
                changed(lambda e: e.update(member_tools={"member_core": None, "tags": None})),
                changed(lambda e: e["member_tools"].update(sync=None)),
                changed(lambda e: e.update(member_tools={"tags": e["member_tools"]["tags"], "member_core": e["member_tools"]["member_core"]})),
                changed(tool("tags", status="CLEAN")), changed(tool("tags", status="STOP")), changed(tool("tags", status="OK")),
                changed(tool("tags", reasons=["alerts", "stale_month"])), changed(tool("tags", reasons=["stale_month", "stale_month"])),
                changed(tool("tags", reasons=["held_writes"])), changed(tool("member_core", reasons=["held_writes", "made_up"])),
                changed(tool("tags", open_writes=-1)), changed(tool("tags", open_writes=summary.COUNT_MAX + 1)),
                changed(tool("tags", open_writes=True)), changed(tool("tags", stale_month=0)),
                changed(tool("tags", alerts=["b_code", "a_code"])), changed(tool("tags", alerts=["code_%02d" % n for n in range(21)])),
                changed(tool("member_core", alerts={"code_%02d" % n: 1 for n in range(21)})),
                changed(tool("member_core", alerts={"Bad Code": 1})), changed(tool("member_core", rule_settings_match=None)),
                changed(tool("member_core", available=False)), changed(tool("member_core", refusal="timed_out")),
                changed(tool("member_core", status="STOP", reasons=["digest_failed", "held_writes"])),
                changed(lambda e: e["member_tools"]["tags"].update(extra=1)),
                changed(lambda e: e["member_tools"].update(tags={**summary.tool_failed("tags", "timed_out"), "runs_completed": 3})),
                changed(lambda e: e["member_tools"].update(tags={**summary.tool_failed("tags", "timed_out"), "refusal": None})),
            ],
        }
        for code, events in cases.items():
            for event in events:
                with self.subTest(code=code, tools=json.dumps(event.get("member_tools"))[:160]), \
                        self.assertRaises(summary.SummaryError) as caught:
                    summary.check_event(event)
                self.assertEqual(caught.exception.code, code)


class WiringContractTests(WiringTestCase):
    """T8: each real tool's digest gives exactly the keys and types the summary reads.

    Route: each tool's own ``ledger_digest`` in-process, on a temp ledger made by that tool's own
    ``init`` (its Ledger class and its example config) with one run row stored the way the tools'
    own tests store one. Not the CLI child: the CLI's state root check needs a private-ACL folder
    owned by the current user, which a CI temp folder is not; the CLI prints exactly this function's
    answer (``_emit(ledger_digest(...))``). The JSON of that answer is then fed through daily() as
    the child's output, so the full path is the real digest's."""

    @classmethod
    def setUpClass(cls):
        import xb_directlocal as dl  # noqa: PLC0415 - test-only imports of the real tools
        import xb_member_benefits as bm
        import xb_member_tags as mt
        cls.dl, cls.bm, cls.mt = dl, bm, mt

    def setUp(self):
        super().setUp()
        self.now = NOW

    def store_run(self, ledger_file, run_summary):
        import sqlite3  # noqa: PLC0415
        conn = sqlite3.connect(ledger_file)
        try:
            conn.execute("INSERT INTO runs(run_id, command, started_at, finished_at, summary) VALUES ('run-x', 'observe', ?, ?, ?)",
                         (self.dl.ts(self.now - timedelta(minutes=3)), self.dl.ts(self.now - timedelta(minutes=2)), json.dumps(run_summary)))
            conn.commit()
        finally:
            conn.close()

    def real_tags_digest(self):
        cfg = self.mt.parse_config(json.loads((ROOT / "config" / "member_tags.example.json").read_text(encoding="utf-8")))
        root = self.tags_root
        ledger = self.mt.TagsLedger(root, clock=lambda: self.now)
        try:
            ledger.init_meta(cfg)
        finally:
            ledger.close()
        self.store_run(root / self.mt.LEDGER_FILE, {"counts": {"would_tag": 3, "would_bday_tag": 1}, "alerts": ["tag_removes_capped", "write_uncertain"],
                                                     "tagged_now": 5, "bday_tagged_now": 2, "bday_month_now": "November"})
        return self.mt.ledger_digest(root, hours=24, now=self.now, config=cfg)

    def real_core_digest(self):
        cfg = self.bm.load_config(ROOT / "config" / "member_benefits.example.json")
        root = self.core_root
        ledger = self.bm.BenefitsLedger(root, clock=lambda: self.now)
        try:
            ledger.init_meta(cfg)
        finally:
            ledger.close()
        self.store_run(root / self.bm.LEDGER_FILE, {"counts": {"orders_read": 4}, "alerts": ["orders_read_incomplete", "claim_needs_review"]})
        return self.bm.ledger_digest(root, hours=24, now=self.now, config=cfg)

    def test_tags_email_block(self):
        digest = self.real_tags_digest()
        email = digest["email"]
        self.assertEqual(set(email), set(summary.TAGS_EMAIL_KEYS))
        for key in summary.TAGS_COUNT_KEYS:
            self.assertIs(type(email[key]), int, key)
        self.assertIs(type(email["stale_month"]), bool)
        self.assertIsInstance(email["alerts"], list)
        self.assertEqual(self.mt.EMAIL_MAX_ALERTS, summary.TAGS_MAX_ALERTS)
        section = summary.tags_section(json.loads(json.dumps(digest)))
        self.assertIs(section["available"], True)
        self.assertEqual((section["runs_completed"], section["tagged_now"], section["would_tag"], section["alerts"]),
                         (1, 5, 3, ["tag_removes_capped", "write_uncertain"]))
        self.assertEqual((section["status"], section["reasons"]), ("STOP", ["write_alert", "alerts"]))

    def test_core_pinned_keys(self):
        digest = self.real_core_digest()
        self.assertEqual(digest["schema"], summary.CORE_DIGEST_SCHEMA)
        self.assertEqual(self.bm.DIGEST_SCHEMA, summary.CORE_DIGEST_SCHEMA)
        for field, path in summary.CORE_PINNED:
            value = digest
            for part in path:
                value = value[part]
            self.assertIs(type(value), bool if field == "rule_settings_match" else int, field)
        self.assertIsInstance(digest["alerts"], dict)
        section = summary.core_section(json.loads(json.dumps(digest)))
        self.assertIs(section["available"], True)
        self.assertEqual(section["alerts"], {"claim_needs_review": 1, "orders_read_incomplete": 1})
        self.assertEqual((section["status"], section["reasons"]), ("WARN", ["alerts"]))

    def test_core_member_no_unsupported_alert_is_a_warn_not_a_refusal(self):
        # Slice NF2 (#268): the member core's new hold alert is an open alert
        # code (CODE_RE), so no closed list changes: it reads as WARN (alerts),
        # needing staff action, and never as digest_failed.
        cfg = self.bm.load_config(ROOT / "config" / "member_benefits.example.json")
        ledger = self.bm.BenefitsLedger(self.core_root, clock=lambda: self.now)
        try:
            ledger.init_meta(cfg)
        finally:
            ledger.close()
        self.store_run(self.core_root / self.bm.LEDGER_FILE, {"counts": {"member_no_unsupported": 1}, "alerts": [self.bm.MEMBER_NO_UNSUPPORTED]})
        digest = self.bm.ledger_digest(self.core_root, hours=24, now=self.now, config=cfg)
        section = summary.core_section(json.loads(json.dumps(digest)))
        self.assertEqual((section["available"], section["status"], section["reasons"]), (True, "WARN", ["alerts"]))
        self.assertEqual(section["alerts"], {"member_no_unsupported": 1})
        self.assertNotIn("member_no_unsupported", summary.CORE_INFO_ALERTS)

    def test_both_real_digests_through_daily(self):
        runner = ToolRunner(core=self.real_core_digest(), tags=self.real_tags_digest())
        event = self.run_wired(True, True, runner=runner)
        self.assertEqual(event["member_tools"]["member_core"]["status"], "WARN")
        self.assertEqual(event["member_tools"]["tags"]["status"], "STOP")
        self.assertEqual(self.result["overall"], "STOP")

    def cli_digest(self, module, example, root, **roots):
        """G2 LATER 4: the tool's own ``main(["digest", "--config", <file>, "--hours", "24"])`` in-process,
        on a ledger made by its own ``init`` through the same ``main``, with the state-root ACL check
        (``dl.verify_private_state``) a no-op and %LOCALAPPDATA% elsewhere, as the tools' own CLI tests
        do. Returns (exit code, stdout)."""

        value = json.loads((ROOT / "config" / example).read_text(encoding="utf-8"))
        value.update(state_root=str(root), **{key: str(path) for key, path in roots.items()})
        path = self.temp / ("cli_" + example)
        path.write_text(json.dumps(value), encoding="utf-8")
        elsewhere = self.temp / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(elsewhere)}), \
                mock.patch.object(self.dl, "verify_private_state", lambda folder: None):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["init", "--config", str(path)], clock=lambda: self.now), 0)
            ledger = root / module.LEDGER_FILE
            self.store_run(ledger, {"counts": {}, "alerts": ["write_uncertain" if module is self.mt else "claim_needs_review"],
                                    "tagged_now": 5, "bday_month_now": "November"})
            out = io.StringIO()
            with redirect_stdout(out):
                code = module.main(["digest", "--config", str(path), "--hours", "24"], clock=lambda: self.now)
        return code, out.getvalue()

    def test_each_tools_cli_digest_reads_as_the_summary_reads_a_child(self):
        cases = (
            (self.mt, "member_tags.example.json", self.tags_root,
             {"member_sync_state_root": self.state_root, "member_core_state_root": self.core_root}, summary.tags_section, "STOP"),
            (self.bm, "member_benefits.example.json", self.core_root, {"member_sync_state_root": self.state_root}, summary.core_section, "WARN"),
        )
        for module, example, root, roots, section_of, status in cases:
            with self.subTest(tool=module.__name__):
                code, stdout = self.cli_digest(module, example, root, **roots)
                self.assertEqual(code, 0, stdout)  # a non-zero digest exit is exit_code in the summary
                lines = stdout.strip().splitlines()
                self.assertEqual(len(lines), 1, "the digest answer is one JSON line")
                answer = summary._last_json_line(stdout)
                self.assertIsInstance(answer, dict)
                self.assertEqual(answer.get("status"), "ok")
                section = section_of(answer)
                self.assertIs(section["available"], True, section)
                self.assertEqual(section["runs_completed"], 1)
                self.assertEqual(section["status"], status)

    def test_the_tools_own_digest_commands_take_these_arguments(self):
        for module in (self.mt, self.bm):
            self.assertIn("digest", module.COMMANDS)
            text = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIn('parser.add_argument("--config", required=True)', text)
            self.assertIn('parser.add_argument("--hours", type=int, default=24', text)


# ------------------------------------------------------------------------------------------------
# #155 slice FH1: the forms line and the forms_flow_silent WARN (design B12). Counts only.
# ------------------------------------------------------------------------------------------------

def hours_ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def forms_block(*, sign_up=None, record=None, not_healed=None, would_heal=None, **counts):
    """The sync digest's ``forms`` block (fake counts)."""

    value = {code: 0 for code in ("forms_records_seen", "forms_records_new", "forms_read_incomplete", "forms_same_email_tags_missing",
                                  "forms_attached_by_phone", "forms_ok", "forms_would_heal_tags", "forms_would_heal", "forms_left_used",
                                  "forms_other_person", "forms_held_name_unclear", "forms_held_email_elsewhere", "forms_unclassified",
                                  "forms_record_invalid")}
    value.update(counts)
    not_healed_codes = ("forms_left_used", "forms_held_name_unclear", "forms_held_email_elsewhere", "forms_other_person", "forms_unclassified")
    return {"counts": value, "not_healed": sum(value[c] for c in not_healed_codes) if not_healed is None else not_healed,
            "would_heal": value["forms_would_heal"] + value["forms_would_heal_tags"] if would_heal is None else would_heal,
            "last_record_at": record, "last_sign_up_at": sign_up}


FORMS_COUNTS = {"forms_left_used": 2, "forms_held_name_unclear": 1, "forms_held_email_elsewhere": 1, "forms_unclassified": 1,
                "forms_other_person": 3, "forms_would_heal": 4, "forms_would_heal_tags": 1}


class FormsSummaryTests(SummaryTestCase):
    def daily_with(self, block, *, expected=True):
        value = digest_fixture() if block is None else digest_fixture(forms=block)
        return self.run_daily(runner=FakeRunner(digest=value), settings=self.settings(forms_watch_expected=expected))

    def warn(self) -> list:
        return self.record["forms"]["warn"]

    def test_setting_defaults_off_and_must_be_a_bool(self):
        self.assertFalse(self.settings()["forms_watch_expected"])
        self.assertIs(json.loads(EXAMPLE_SETTINGS.read_text(encoding="utf-8"))["forms_watch_expected"], False)
        value = self.settings_value()
        del value["forms_watch_expected"]
        self.assertFalse(summary.parse_settings(value)["forms_watch_expected"])
        self.assertTrue(self.settings(forms_watch_expected=True)["forms_watch_expected"])
        for bad in ("true", 1, None):
            with self.assertRaises(summary.SummaryError) as caught:
                self.settings(forms_watch_expected=bad)
            self.assertEqual(caught.exception.code, "settings_forms_watch_expected_invalid")

    def test_not_expected_changes_nothing_even_with_a_forms_block(self):
        plain = self.run_daily(runner=FakeRunner(digest=digest_fixture()))
        plain_record, plain_result = dict(self.record), dict(self.result)
        for settings in (self.settings(), self.settings(forms_watch_expected=False)):
            event = self.run_daily(runner=FakeRunner(digest=digest_fixture(forms=forms_block(sign_up=hours_ago(1), **FORMS_COUNTS))),
                                   settings=settings)
            self.assertEqual(event, plain)
            self.assertEqual(self.record, plain_record)
            self.assertEqual(self.result, plain_result)
            self.assertNotIn("forms", self.record)
            self.assertNotIn("forms_warn", self.result)

    def test_the_line_and_the_counts_in_the_alert_line(self):
        event = self.daily_with(forms_block(sign_up=hours_ago(2), record=hours_ago(2), **FORMS_COUNTS))
        self.assertEqual(self.record["forms"]["line"], "Website sign-ups on an existing account (24h): not healed 8 "
                                                       "(account used 2, held 3) | would heal 5 | other person 3")
        self.assertEqual({k: v for k, v in event["digest"]["alerts"].items() if k.startswith("forms_")},
                         {"forms_not_healed": 8, "forms_account_used": 2, "forms_held": 3, "forms_would_heal": 5, "forms_other_person": 3})
        self.assertEqual((event["result"], event["stop_reasons"], event["schema"]), ("CLEAN", [], summary.EVENT_SCHEMA))
        self.assertEqual((self.warn(), self.result["forms_warn"]), ([], []))
        self.assertEqual(self.poster.calls[0]["body"], event)

    def test_never_a_stop(self):
        base = self.daily_with(None, expected=False)
        event = self.daily_with(forms_block(sign_up=hours_ago(7), record=None, forms_read_incomplete=3, forms_read_stalled=2, **FORMS_COUNTS))
        self.assertEqual((event["result"], event["stop_reasons"], event["auto_off_reasons"]),
                         (base["result"], base["stop_reasons"], base["auto_off_reasons"]))
        self.assertEqual(self.result["forms_warn"], ["forms_flow_silent"])
        self.assertEqual(event["digest"]["alerts"]["forms_flow_silent"], 1)

    def test_warn_edges_on_the_72_hours(self):
        for hours, warn in ((71, True), (72, True), (72.001, False), (73, False)):
            event = self.daily_with(forms_block(sign_up=hours_ago(hours), record=None))
            self.assertEqual(self.warn(), ["forms_flow_silent"] if warn else [], hours)
            self.assertEqual("forms_flow_silent" in event["digest"]["alerts"], warn, hours)

    def test_warn_edges_on_the_6_hour_age(self):
        # FH1b (Controller Q2): never right after a sign-up; from 6 hours on (inclusive).
        for age, warn in ((5, False), (5.999, False), (6, True), (7, True)):
            event = self.daily_with(forms_block(sign_up=hours_ago(age), record=None))
            self.assertEqual(self.warn(), ["forms_flow_silent"] if warn else [], age)
            self.assertEqual("forms_flow_silent" in event["digest"]["alerts"], warn, age)

    def test_warn_edges_on_the_record_margin(self):
        # A record at or after the sign-up minus 30 minutes counts as "after it".
        for before, warn in ((0, False), (-1, False), (0.5, False), (0.501, True), (20, True)):
            self.daily_with(forms_block(sign_up=hours_ago(7), record=hours_ago(7 + before)))
            self.assertEqual(self.warn(), ["forms_flow_silent"] if warn else [], before)

    def test_no_warn_right_after_a_sign_up_after_a_quiet_night(self):
        # The G2's case: a sign-up 10 minutes ago, the newest record 20 hours ago.
        self.daily_with(forms_block(sign_up=hours_ago(1 / 6), record=hours_ago(20)))
        self.assertEqual(self.warn(), [])

    def test_no_sign_up_no_warn_and_a_newer_record_no_warn(self):
        for sign_up, record in ((None, None), (None, hours_ago(1)), (hours_ago(10), hours_ago(1)), ("not a time", None)):
            self.daily_with(forms_block(sign_up=sign_up, record=record))
            self.assertEqual(self.warn(), [], (sign_up, record))

    def test_the_pure_rule(self):
        self.assertTrue(summary.forms_flow_silent(NOW - timedelta(hours=72), None, NOW))
        self.assertFalse(summary.forms_flow_silent(NOW - timedelta(hours=72, seconds=1), None, NOW))
        self.assertTrue(summary.forms_flow_silent(NOW - timedelta(hours=6), None, NOW))
        self.assertFalse(summary.forms_flow_silent(NOW - timedelta(hours=6) + timedelta(seconds=1), None, NOW))
        sign_up = NOW - timedelta(hours=8)
        self.assertFalse(summary.forms_flow_silent(sign_up, sign_up - timedelta(minutes=30), NOW))
        self.assertTrue(summary.forms_flow_silent(sign_up, sign_up - timedelta(minutes=30, seconds=1), NOW))
        self.assertFalse(summary.forms_flow_silent(sign_up, sign_up + timedelta(hours=1), NOW))
        self.assertFalse(summary.forms_flow_silent(None, None, NOW))
        self.assertFalse(summary.forms_flow_silent(NOW - timedelta(minutes=10), NOW - timedelta(hours=20), NOW))

    def test_an_incomplete_or_stalled_read_is_shown(self):
        event = self.daily_with(forms_block(sign_up=None, forms_read_incomplete=4, forms_read_stalled=1, **FORMS_COUNTS))
        self.assertTrue(self.record["forms"]["line"].endswith(" | read incomplete 4 runs (stalled 1)"))
        self.assertEqual((event["digest"]["alerts"]["forms_read_incomplete"], event["digest"]["alerts"]["forms_read_stalled"]), (4, 1))
        self.assertEqual((event["result"], event["stop_reasons"]), ("CLEAN", []))
        event = self.daily_with(forms_block(sign_up=None, **FORMS_COUNTS))  # a normal day: not shown
        self.assertNotIn("read incomplete", self.record["forms"]["line"])
        self.assertFalse({"forms_read_incomplete", "forms_read_stalled"} & set(event["digest"]["alerts"]))

    def test_no_block_or_no_digest_says_not_available(self):
        for runner in (FakeRunner(digest=digest_fixture()), FakeRunner(codes={"digest": 2}, digest="not json")):
            event = self.run_daily(runner=runner, settings=self.settings(forms_watch_expected=True))
            self.assertFalse(self.record["forms"]["available"])
            self.assertIn("not available", self.record["forms"]["line"])
            self.assertEqual(self.result["forms_warn"], [])
            self.assertFalse(any(code.startswith("forms_") for code in (event["digest"]["alerts"] or {})))

    def test_counts_are_bounded_and_unreadable_ones_are_zero(self):
        self.daily_with(forms_block(not_healed=-1, would_heal="5", forms_left_used=True, forms_other_person=10 ** 12))
        forms = self.record["forms"]
        self.assertEqual((forms["forms_not_healed"], forms["forms_would_heal"], forms["forms_account_used"], forms["forms_other_person"]),
                         (0, 0, 0, summary.COUNT_MAX))

    def test_privacy_of_the_record_and_the_event(self):
        event = self.daily_with(forms_block(sign_up=hours_ago(1), **FORMS_COUNTS))
        summary.check_event(event)
        self.assertNotIn("@", json.dumps(self.record))
        self.assertIsNone(re.search(r"[0-9]{6,}", json.dumps(self.record["forms"])))

    def test_the_n8n_event_shape_is_unchanged(self):
        event = self.daily_with(forms_block(sign_up=hours_ago(1), **FORMS_COUNTS))
        self.assertEqual(tuple(event), summary.EVENT_KEYS)
        # FH1 does not touch the n8n workflow's event handling. #288 slice 3 errors only (owner 22 Nov 2026): its gate
        # names exactly one forms code, the forms watch's WARN, so a CLEAN morning with that WARN is still emailed.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("'%s'" % summary.FORMS_FLOW_SILENT, text)
        self.assertNotIn("forms_", text.replace(summary.FORMS_FLOW_SILENT, ""))


if __name__ == "__main__":
    unittest.main()
