"""Offline tests of the monthly in-store member discount check (#268 Q7).

No SQL Server, AutoCount, n8n or network is used: a fake source returns rows shaped like the
pinned receipts and sync queries, and a fake transport records the alert POST. Every value is
synthetic (example outlets OUT01/OUT02, member numbers TEST000xxx, receipt numbers CS-T0xxx).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import xb_member_pos_exceptions as xr  # noqa: E402

EXAMPLE = ROOT / "config" / "member_pos_exceptions.example.json"
INSTALLER_PATH = ROOT / "scripts" / "install_member_pos_exceptions_task.ps1"
RUNBOOK = ROOT / "docs" / "autocount2-automation" / "member_pos_exceptions_runbook.md"
SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 12, 2, 9, 15, tzinfo=SGT)
MONTH = date(2026, 11, 1)
ENV = {
    "AUTOCOUNT_EXPECTED_SQL_LOGIN": "EXAMPLE\\reader",
    "AUTOCOUNT_EXPECTED_SQL_USER": "reader",
    "AUTOCOUNT_EXPECTED_SQL_DATABASE": "AED_EXAMPLE_BOOK",
    "XB_TEST_ALERT_SECRET": "FAKE-SECRET-0001",
}
CANARY_MEMBER = "TEST000987"

# Pinned query hashes per terminal column: a changed query text must be reviewed (G2) and this
# table updated with it.
PINNED_QUERY_SHA256 = {
    "TerminalID": {
        "identity": "5429df38941a267a8ece8d3c8e3a6b4b9543974ee4e11d13d0386b1d17ba36f6",
        "receipts": "5ff984e96510495ae7d7637239691f7f6f6b4ffc543448d03d1c56d0710b2e1b",
        "sync": "b0b93832f16f5fd1c3bb1e08c3068a628ca37f3170501a2e2d150526951c7440",
    },
    "LastModifiedTerminalID": {
        "identity": "5429df38941a267a8ece8d3c8e3a6b4b9543974ee4e11d13d0386b1d17ba36f6",
        "receipts": "58ebb96daec6263d3f886a3aca9d2e152198cd6174f7a083c0fcd432e45fbcd3",
        "sync": "b0b93832f16f5fd1c3bb1e08c3068a628ca37f3170501a2e2d150526951c7440",
    },
}


def receipt(doc_no="CS-T0001", day=date(2026, 11, 10), cancelled="F", positive=1, member_no=CANARY_MEMBER,
            outlet="OUT01", found=True, member_type="GUEST", expiry=None, modified=date(2026, 10, 1),
            created=date(2026, 1, 5), active="T", discounts=("15.00%",), key=None):
    """Rows of one receipt as receipts_query returns them (one per discounted line)."""
    base = {
        "DocKey": key if key is not None else hash(doc_no) & 0xFFFFFF,
        "DocNo": doc_no,
        "DocDate": datetime.combine(day, datetime.min.time()),
        "Cancelled": cancelled,
        "IsPositive": positive,
        "MemberNo": member_no,
        "Outlet": outlet,
        "MemberFound": (member_no if found else None) if member_no else None,
        "MemberType": member_type if found and member_no else None,
        "ExpiryDate": datetime.combine(expiry, datetime.min.time()) if expiry else None,
        "LastModified": datetime.combine(modified, datetime(2000, 1, 1, 14, 5).time()) if modified else None,
        "MemberCreated": datetime.combine(created, datetime(2000, 1, 1, 13, 50).time()) if created else None,
        "IsActive": active,
    }
    return [dict(base, Discount=d) for d in discounts] or [dict(base, Discount=None)]


def settings(**rule):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["output"]["record_dir"] = rule.pop("record_dir", "C:\\EXAMPLE\\unused")
    raw["identity"]["allow_privileged_identity"] = rule.pop("allow_privileged", False)
    alert = rule.pop("alert", None)
    if alert:
        raw["alert"].update(alert)
    raw["rule"].update(rule)
    return xr.validate_settings(raw)


class FakeSession:
    def __init__(self, source):
        self.source = source

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_identity(self):
        return dict(self.source.identity)

    def read_sync(self):
        return list(self.source.sync)

    def read_receipts(self, start, end):
        self.source.windows.append((start, end))
        return [r for r in self.source.rows if start <= r["DocDate"] < end]


class FakeSource:
    def __init__(self, rows, sync=None, identity=None):
        self.rows = rows
        self.sync = sync if sync is not None else [
            {"Location": "OUT01", "LastSyncTime": datetime(2026, 12, 1, 11, 0)},
            {"Location": "OUT02", "LastSyncTime": datetime(2026, 12, 2, 8, 0)},
            {"Location": "OUT01", "LastSyncTime": datetime(2026, 6, 22, 9, 0)},
        ]
        self.identity = identity or {"login": "EXAMPLE\\reader", "user": "reader", "database": "AED_EXAMPLE_BOOK",
                                     "is_sysadmin": 0, "is_db_owner": 0}
        self.windows = []

    def open_session(self):
        return FakeSession(self)


class FakeTransport:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body.decode("ascii"))})
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


ALERT_ON = {"webhook_enabled": True, "webhook_url": "https://alerts.example.com/webhook/fake-path",
            "secret_env_var": "XB_TEST_ALERT_SECRET"}


class RuleTests(unittest.TestCase):
    """The rule, including the mutation checks of the G1 brief."""

    def judge(self, *receipts, **rule):
        rows = [row for r in receipts for row in r]
        return xr.classify(rows, settings(**rule))

    def test_guest_with_member_rate_is_flagged(self):
        counts, outlets, listed = self.judge(receipt())
        self.assertEqual(counts["guest"], 1)
        self.assertEqual(listed[0]["reason"], "guest")
        self.assertEqual(listed[0]["rate"], "15%")
        self.assertEqual(listed[0]["member"], "***987")
        self.assertEqual(listed[0]["record_note"], "none")
        self.assertEqual(outlets, [{"outlet": "OUT01", "guest": 1, "expired": 0, "same_receipt": 0, "no_member_on_receipt": 0}])

    def test_default_type_counts_as_guest_and_20_percent_is_a_member_rate(self):
        counts, _, listed = self.judge(receipt(member_type="Default", discounts=("20.00%",)))
        self.assertEqual(counts["guest"], 1)
        self.assertEqual(listed[0]["rate"], "20%")

    def test_valid_member_is_not_flagged(self):
        counts, _, listed = self.judge(receipt(member_type="MEMBER", expiry=date(2028, 6, 30)))
        self.assertEqual(listed, [])
        self.assertEqual(counts["member_valid"], 1)
        self.assertEqual(xr.exceptions_total(counts), 0)

    def test_cancelled_receipt_is_excluded(self):
        counts, _, listed = self.judge(receipt(cancelled="T"))
        self.assertEqual((counts["cancelled"], counts["guest"], listed), (1, 0, []))

    def test_unknown_cancel_flag_is_excluded_too(self):
        counts, _, listed = self.judge(receipt(cancelled=None))
        self.assertEqual((counts["cancelled"], listed), (1, []))

    def test_return_or_zero_receipt_is_excluded(self):
        counts, _, listed = self.judge(receipt(positive=0))
        self.assertEqual((counts["not_positive"], listed), (1, []))

    def test_no_member_number_is_counted_separately_never_listed(self):
        counts, outlets, listed = self.judge(receipt(member_no=None), receipt(doc_no="CS-T0002", member_no="  "))
        self.assertEqual(counts["no_member_on_receipt"], 2)
        self.assertEqual(listed, [])
        self.assertEqual(xr.exceptions_total(counts), 0)
        self.assertEqual(outlets[0]["no_member_on_receipt"], 2)

    def test_member_number_not_found_is_counted_only(self):
        counts, _, listed = self.judge(receipt(found=False))
        self.assertEqual((counts["member_not_found"], listed), (1, []))

    def test_other_rates_and_no_discount_are_not_member_rate(self):
        counts, _, listed = self.judge(receipt(discounts=("25.00%", "10.00%", "0.00%", "staff")),
                                       receipt(doc_no="CS-T0002", discounts=()))
        self.assertEqual((counts["member_rate"], listed), (0, []))
        self.assertEqual(counts["receipts_read"], 2)

    def test_rate_text_forms(self):
        rates = {Decimal(15), Decimal(20)}
        for text in ("15%", "15.00%", " 15.0 % ", "20.00%"):
            self.assertIsNotNone(xr.rate_of(text, rates), text)
        for text in ("15", "115%", "15.5%", "-15%", "15%%", "", None, "1 5%"):
            self.assertIsNone(xr.rate_of(text, rates), text)

    def test_member_rates_setting_is_used(self):
        counts, _, _ = self.judge(receipt(discounts=("25.00%",)), member_rates=[25])
        self.assertEqual(counts["guest"], 1)

    def test_expired_member_is_flagged(self):
        counts, _, listed = self.judge(receipt(member_type="MEMBER", expiry=date(2026, 11, 9)))
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(listed[0]["reason"], "expired")

    def test_expiry_on_the_receipt_day_is_still_valid(self):
        counts, _, _ = self.judge(receipt(member_type="MEMBER", expiry=date(2026, 11, 10)))
        self.assertEqual((counts["expired"], counts["member_valid"]), (0, 1))

    def test_same_receipt_upgrade_is_flagged(self):
        counts, _, listed = self.judge(receipt(member_type="MEMBER", expiry=date(2028, 11, 10), modified=date(2026, 11, 10)))
        self.assertEqual(counts["same_receipt"], 1)
        self.assertEqual(listed[0]["reason"], "same_receipt")
        # One day either side is the same receipt; two days is not.
        counts, _, _ = self.judge(receipt(member_type="MEMBER", expiry=date(2028, 11, 11), modified=date(2026, 11, 10)))
        self.assertEqual(counts["same_receipt"], 1)
        counts, _, _ = self.judge(receipt(member_type="MEMBER", expiry=date(2028, 11, 12), modified=date(2026, 11, 10)))
        self.assertEqual((counts["same_receipt"], counts["member_valid"]), (0, 1))

    def test_upgrade_before_the_receipt_is_not_same_receipt(self):
        counts, _, _ = self.judge(receipt(member_type="MEMBER", expiry=date(2028, 11, 10), modified=date(2026, 11, 9)))
        self.assertEqual((counts["same_receipt"], counts["member_valid"]), (0, 1))

    def test_guest_edited_on_or_after_the_sale_is_listed_with_a_neutral_note(self):
        # LastModified moves on any edit (bulk edits too): the note says only that the record was edited.
        for modified in (date(2026, 11, 10), date(2026, 11, 20)):
            counts, _, listed = self.judge(receipt(modified=modified))
            self.assertEqual((counts["guest"], counts["changed_after_sale"]), (1, 1))
            self.assertEqual(listed[0]["record_note"], "edited_on_or_after_sale_day")
        counts, _, listed = self.judge(receipt(modified=date(2026, 11, 9)))
        self.assertEqual((counts["guest"], counts["changed_after_sale"], listed[0]["record_note"]), (1, 0, "none"))

    def test_guest_registered_on_the_sale_day_is_listed_as_created_that_day(self):
        # The owner's main case: registered at the till (new GUEST record) and 15% keyed on that receipt.
        counts, _, listed = self.judge(receipt(created=date(2026, 11, 10), modified=date(2026, 11, 10)))
        self.assertEqual((counts["guest"], counts["changed_after_sale"]), (1, 1))
        self.assertEqual((listed[0]["reason"], listed[0]["record_note"]), ("guest", "created_on_sale_day"))
        # Created the day before and edited on the sale day: edited, not created.
        _, _, listed = self.judge(receipt(created=date(2026, 11, 9), modified=date(2026, 11, 10)))
        self.assertEqual(listed[0]["record_note"], "edited_on_or_after_sale_day")
        # A note is only ever given to a GUEST row.
        _, _, listed = self.judge(receipt(member_type="MEMBER", expiry=date(2026, 11, 1),
                                          created=date(2026, 11, 10), modified=date(2026, 11, 10)))
        self.assertEqual((listed[0]["reason"], listed[0]["record_note"]), ("expired", "none"))

    def test_other_member_type_is_counted_only(self):
        counts, _, listed = self.judge(receipt(member_type="VIP"))
        self.assertEqual((counts["type_other"], listed), (1, []))

    def test_inactive_member_is_counted_and_still_judged(self):
        counts, _, listed = self.judge(receipt(active="F"))
        self.assertEqual((counts["member_inactive"], counts["guest"]), (1, 1))

    def test_several_lines_count_one_receipt(self):
        counts, _, listed = self.judge(receipt(discounts=("15.00%", "20.00%", "15.00%")))
        self.assertEqual((counts["receipts_read"], counts["member_rate"], counts["guest"]), (1, 1, 1))
        self.assertEqual(listed[0]["rate"], "20%")

    def test_unreadable_outlet_and_doc_number_are_replaced(self):
        _, outlets, listed = self.judge(receipt(outlet=None, doc_no="CS T 0001 <x>"))
        self.assertEqual(listed[0]["outlet"], "UNKNOWN")
        self.assertEqual(listed[0]["doc_no"], "UNREADABLE")
        self.assertEqual(outlets[0]["outlet"], "UNKNOWN")

    def test_masking_keeps_only_three_letters_or_digits(self):
        self.assertEqual(xr.mask_member("+TEST000123"), "***123")
        self.assertEqual(xr.mask_member("TEST-00 12-3"), "***123")
        self.assertEqual(xr.mask_member("+"), "***?")

    def test_listed_rows_are_in_day_then_number_order(self):
        _, _, listed = self.judge(receipt(doc_no="CS-T0009", day=date(2026, 11, 3)),
                                  receipt(doc_no="CS-T0002", day=date(2026, 11, 20)),
                                  receipt(doc_no="CS-T0001", day=date(2026, 11, 3)))
        self.assertEqual([r["doc_no"] for r in listed], ["CS-T0001", "CS-T0009", "CS-T0002"])


class SyncTests(unittest.TestCase):
    def stale(self, sync):
        return xr.stale_outlets(sync, settings(), MONTH, SGT)

    def test_synced_after_month_end(self):
        self.assertEqual(self.stale(FakeSource([]).sync), [])

    def test_last_sync_before_month_end_is_stale(self):
        sync = [{"Location": "OUT01", "LastSyncTime": datetime(2026, 11, 30, 20, 38)},
                {"Location": "OUT02", "LastSyncTime": datetime(2026, 12, 1, 0, 0)}]
        self.assertEqual(self.stale(sync), ["OUT01", "OUT02"])

    def test_missing_outlet_or_unreadable_time_is_stale(self):
        sync = [{"Location": "OUT01", "LastSyncTime": datetime(2026, 12, 1, 9, 0)},
                {"Location": "OUT02", "LastSyncTime": "2026-12-01"}]
        self.assertEqual(self.stale(sync), ["OUT02"])

    def test_aware_time_is_compared_in_its_own_zone(self):
        sync = [{"Location": "OUT01", "LastSyncTime": datetime(2026, 11, 30, 16, 30, tzinfo=timezone.utc)},
                {"Location": "OUT02", "LastSyncTime": datetime(2026, 12, 1, 1, 0)}]
        self.assertEqual(self.stale(sync), [])


class SettingsAndSqlTests(unittest.TestCase):
    def test_example_settings_are_valid_and_switched_off(self):
        s = settings()
        self.assertFalse(s["alert"]["webhook_enabled"])
        self.assertFalse(s["identity"]["allow_privileged_identity"])
        self.assertEqual(s["rule"]["member_rates"], [15, 20])

    def test_bad_settings_are_refused(self):
        base = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        cases = [
            ("unknown", {"extra": 1}),
            ("rule.outlets", {"rule": dict(base["rule"], outlets=[])}),
            ("rule.outlets", {"rule": dict(base["rule"], outlets=["OUT 1"])}),
            ("rule.member_rates", {"rule": dict(base["rule"], member_rates=[0])}),
            ("rule.max_listed", {"rule": dict(base["rule"], max_listed=13)}),
            ("source.terminal_column", {"source": dict(base["source"], terminal_column="Name")}),
            ("sa login", {"identity": dict(base["identity"], auth_mode="sql_login", sql_login="SA",
                                          credential_target="X"), "schedule": dict(base["schedule"], logon_type="Password")}),
            ("schedule.run_day", {"schedule": dict(base["schedule"], run_day=29)}),
            ("schedule.run_time", {"schedule": dict(base["schedule"], run_time="9:15")}),
            ("unknown", {"rule": dict(base["rule"], cashier=True)}),
        ]
        for name, change in cases:
            raw = dict(base, **change)
            with self.subTest(name), self.assertRaises(xr.ConfigError):
                xr.validate_settings(raw)

    def test_every_query_is_one_read_only_select(self):
        for column in xr.TERMINAL_COLUMNS:
            for name, text in xr.all_query_texts(column).items():
                with self.subTest(column=column, query=name):
                    xr.assert_read_only(text)
        for bad in ("SELECT 1; DELETE FROM dbo.Pos", "SELECT 1; SELECT 2", "UPDATE dbo.Pos SET MemberNo = ''", "SELECT 1 -- x",
                    "SELECT * INTO x FROM dbo.Pos", "EXEC sp_who"):
            with self.assertRaises(xr.ConfigError):
                xr.assert_read_only(bad)

    def test_query_hashes_pinned(self):
        # A changed query text must be reviewed (G2) and this table updated with it.
        for column, pinned in PINNED_QUERY_SHA256.items():
            with self.subTest(column=column):
                self.assertEqual(xr.queries_sha256(column), pinned)

    def test_receipts_query_reads_no_personal_column_or_amount(self):
        text = xr.receipts_query("TerminalID")
        for column in ("Name", "Phone", "Mobile", "Email", "Address", "IC", "Birth", "Note", "Remark", "UserID"):
            self.assertNotRegex(text, r"\.%s\b" % column)
        # FinalTotal only as its sign, decided in SQL.
        self.assertIn("CASE WHEN p.FinalTotal > 0 THEN 1 ELSE 0 END AS IsPositive", text)
        self.assertEqual(text.count("FinalTotal"), 1)
        self.assertEqual(text.count("?"), 2)
        with self.assertRaises(xr.ConfigError):
            xr.receipts_query("TerminalID] = 1 OR [x")

    def test_connection_string_is_read_only_intent(self):
        text = xr.build_connection_string(settings())
        self.assertIn("ApplicationIntent=ReadOnly", text)
        self.assertIn("Trusted_Connection=yes", text)

    def test_month_helpers(self):
        self.assertEqual(xr.parse_month("2026-11"), MONTH)
        for bad in ("2026-13", "26-11", "2026-1", "", None):
            with self.assertRaises(xr.ConfigError):
                xr.parse_month(bad)
        self.assertEqual(xr.next_month(date(2026, 12, 1)), date(2027, 1, 1))
        self.assertEqual(xr.previous_month(date(2027, 1, 2)), date(2026, 12, 1))
        self.assertEqual(xr.add_months(date(2028, 2, 29), 12), date(2029, 2, 28))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def settings(self, **extra):
        return settings(record_dir=str(self.tmp / "records"), **extra)

    def run_once(self, rows, send=True, transport=None, source=None, clock=NOW, **extra):
        s = self.settings(**extra)
        return xr.run_month(s, MONTH, send=send, source=source or FakeSource(rows), environ=ENV,
                            clock=lambda: clock, sleep=lambda _s: None, transport=transport)

    def record(self):
        return json.loads((self.tmp / "records" / "pos_exceptions_2026-11.json").read_text(encoding="utf-8"))

    def test_report_with_exceptions_sends_one_event_and_keeps_privacy(self):
        transport = FakeTransport()
        rows = receipt() + receipt(doc_no="CS-T0002", member_type="MEMBER", expiry=date(2028, 6, 30)) \
            + receipt(doc_no="CS-T0003", member_no=None)
        result = self.run_once(rows, transport=transport, alert=ALERT_ON)
        self.assertEqual(result["status"], "report")
        self.assertEqual((result["alert_status"], result["alert_http_status"]), ("sent", 200))
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["headers"]["X-XB-Alert-Token"], "FAKE-SECRET-0001")
        body = call["body"]
        self.assertEqual(tuple(body), xr.ALERT_PAYLOAD_KEYS)
        self.assertEqual(body["rows"], [{"doc_no": "CS-T0001", "outlet": "OUT01", "day": "2026-11-10", "rate": "15%",
                                         "reason": "guest", "member": "***987", "record_note": "none"}])
        self.assertEqual(body["counts"]["no_member_on_receipt"], 1)
        self.assertEqual(body["counts"]["member_valid"], 1)
        # The full member number appears nowhere: event, record, console.
        for text in (json.dumps(body), json.dumps(self.record()), json.dumps(xr.console_summary(result))):
            self.assertNotIn(CANARY_MEMBER, text)
        self.assertNotIn("CS-T0001", json.dumps(xr.console_summary(result)))
        self.assertNotIn("FAKE-SECRET", json.dumps(self.record()))
        self.assertEqual(xr.exit_code(result), 0)

    def test_the_same_month_is_sent_once(self):
        transport = FakeTransport()
        self.run_once(receipt(), transport=transport, alert=ALERT_ON)
        second = self.run_once(receipt(), transport=transport, alert=ALERT_ON)
        self.assertEqual(second["alert_status"], "suppressed")
        self.assertEqual(len(transport.calls), 1)

    def test_each_month_is_sent_on_its_own(self):
        transport = FakeTransport()
        self.run_once(receipt(), transport=transport, alert=ALERT_ON)
        december = FakeSource(receipt(doc_no="CS-T0002", day=date(2026, 12, 15)),
                              sync=[{"Location": "OUT01", "LastSyncTime": datetime(2027, 1, 1, 11, 0)},
                                    {"Location": "OUT02", "LastSyncTime": datetime(2027, 1, 2, 8, 0)}])
        result = xr.run_month(self.settings(alert=ALERT_ON), date(2026, 12, 1), source=december, environ=ENV,
                              clock=lambda: datetime(2027, 1, 2, 9, 15, tzinfo=SGT), sleep=lambda _s: None,
                              transport=transport)
        self.assertEqual(result["alert_status"], "sent")
        self.assertEqual([c["body"]["month"] for c in transport.calls], ["2026-11", "2026-12"])

    def test_unwritable_record_folder_sends_nothing_and_fails(self):
        # The state could not remember the send, so the same email would go out every day: send none.
        blocker = self.tmp / "not_a_folder"
        blocker.write_text("x", encoding="utf-8")
        transport = FakeTransport()
        s = settings(record_dir=str(blocker), alert=ALERT_ON)
        for _ in range(3):
            result = xr.run_month(s, MONTH, source=FakeSource(receipt()), environ=ENV, clock=lambda: NOW,
                                  sleep=lambda _s: None, transport=transport)
            self.assertEqual((result["status"], result["failure_class"], result["alert_status"]),
                             ("failed", "write_failed", "not_sent_record_unwritable"))
            self.assertEqual(xr.exit_code(result), 1)
        self.assertEqual(transport.calls, [])

    def test_failed_send_is_retried_next_run(self):
        failing = FakeTransport(status=502)
        first = self.run_once(receipt(), transport=failing, alert=ALERT_ON)
        self.assertEqual((first["alert_status"], first["alert_http_status"]), ("send_failed", 502))
        network = FakeTransport(status=OSError("down"))
        self.assertEqual(self.run_once(receipt(), transport=network, alert=ALERT_ON)["alert_status"], "send_failed")
        ok = FakeTransport()
        self.assertEqual(self.run_once(receipt(), transport=ok, alert=ALERT_ON)["alert_status"], "sent")

    def test_nothing_to_report_sends_nothing(self):
        transport = FakeTransport()
        result = self.run_once(receipt(member_type="MEMBER", expiry=date(2028, 6, 30)), transport=transport, alert=ALERT_ON)
        self.assertEqual((result["status"], result["alert_status"], transport.calls), ("report", "no_alert", []))

    def test_alert_off_by_default_and_no_send_never_posts(self):
        transport = FakeTransport()
        self.assertEqual(self.run_once(receipt(), transport=transport)["alert_status"], "disabled")
        self.assertEqual(self.run_once(receipt(), send=False, transport=transport, alert=ALERT_ON)["alert_status"],
                         "not_sent_no_send")
        self.assertEqual(transport.calls, [])

    def test_missing_secret_sends_nothing(self):
        transport = FakeTransport()
        alert = dict(ALERT_ON, secret_env_var="XB_TEST_NOT_SET")
        self.assertEqual(self.run_once(receipt(), transport=transport, alert=alert)["alert_status"], "secret_unavailable")
        self.assertEqual(transport.calls, [])

    def test_stale_sync_reports_nothing_and_warns_once(self):
        transport = FakeTransport()
        source = FakeSource(receipt(), sync=[{"Location": "OUT01", "LastSyncTime": datetime(2026, 11, 30, 20, 38)},
                                             {"Location": "OUT02", "LastSyncTime": datetime(2026, 12, 1, 9, 0)}])
        result = self.run_once(None, source=source, transport=transport, alert=ALERT_ON)
        self.assertEqual((result["status"], result["stale_outlets"], result["counts"], result["rows"]),
                         ("pos_sync_stale", ["OUT01"], None, []))
        self.assertEqual(xr.exit_code(result), 3)
        body = transport.calls[0]["body"]
        self.assertEqual((body["status"], body["stale_outlets"], body["counts"], body["rows"]),
                         ("pos_sync_stale", ["OUT01"], None, []))
        self.assertEqual(self.run_once(None, source=source, transport=transport, alert=ALERT_ON)["alert_status"], "suppressed")

    def test_privileged_login_is_refused_by_default(self):
        transport = FakeTransport()
        source = FakeSource(receipt(), identity={"login": "EXAMPLE\\reader", "user": "reader", "database": "AED_EXAMPLE_BOOK",
                                                 "is_sysadmin": 1, "is_db_owner": 1})
        result = self.run_once(None, source=source, transport=transport, alert=ALERT_ON)
        self.assertEqual((result["status"], result["failure_class"]), ("failed", "identity_refused"))
        self.assertEqual(source.windows, [])  # no business query ran
        self.assertEqual(transport.calls[0]["body"]["failure_class"], "identity_refused")
        self.assertEqual(xr.exit_code(result), 1)
        allowed = self.run_once(None, source=source, allow_privileged=True)
        self.assertEqual(allowed["status"], "report")

    def test_settings_without_the_privileged_key_refuse_a_privileged_login(self):
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["output"]["record_dir"] = str(self.tmp / "records")
        del raw["identity"]["allow_privileged_identity"]
        s = xr.validate_settings(raw)
        self.assertIs(s["identity"]["allow_privileged_identity"], False)
        source = FakeSource(receipt(), identity={"login": "EXAMPLE\\reader", "user": "reader", "database": "AED_EXAMPLE_BOOK",
                                                 "is_sysadmin": 1, "is_db_owner": 0})
        result = xr.run_month(s, MONTH, send=False, source=source, environ=ENV, clock=lambda: NOW, sleep=lambda _s: None)
        self.assertEqual((result["failure_class"], source.windows), ("identity_refused", []))

    def test_sa_login_is_always_refused(self):
        source = FakeSource(receipt(), identity={"login": "sa", "user": "dbo", "database": "AED_EXAMPLE_BOOK",
                                                 "is_sysadmin": 1, "is_db_owner": 1})
        result = self.run_once(None, source=source, allow_privileged=True)
        self.assertEqual(result["failure_class"], "identity_refused")

    def test_reads_exactly_the_month(self):
        source = FakeSource(receipt(day=date(2026, 10, 31)) + receipt(doc_no="CS-T0002", day=date(2026, 11, 30))
                            + receipt(doc_no="CS-T0003", day=date(2026, 12, 1)))
        result = self.run_once(None, source=source)
        self.assertEqual(source.windows, [(datetime(2026, 11, 1), datetime(2026, 12, 1))])
        self.assertEqual([r["doc_no"] for r in result["rows"]], ["CS-T0002"])

    def test_month_not_ended_is_refused(self):
        with self.assertRaises(xr.ConfigError):
            self.run_once(receipt(), clock=datetime(2026, 11, 30, 23, 0, tzinfo=SGT))

    def test_lock_timeout_retries_then_fails(self):
        class Locked(FakeSource):
            def open_session(self):
                session = FakeSession(self)
                session.read_sync = lambda: (_ for _ in ()).throw(xr.SourceLockTimeout("lock"))
                return session

        result = self.run_once(None, source=Locked(receipt()))
        self.assertEqual((result["status"], result["failure_class"]), ("failed", "lock_timeout"))

    def test_lock_timeout_is_emailed_only_when_it_repeats_on_a_later_day(self):
        class Locked(FakeSource):
            def open_session(self):
                session = FakeSession(self)
                session.read_sync = lambda: (_ for _ in ()).throw(xr.SourceLockTimeout("lock"))
                return session

        transport = FakeTransport()
        for _ in range(2):  # the first day, twice: held
            first = self.run_once(None, source=Locked(receipt()), transport=transport, alert=ALERT_ON)
            self.assertEqual((first["failure_class"], first["alert_status"]), ("lock_timeout", "held_until_repeat"))
            self.assertEqual(xr.exit_code(first), 1)
        self.assertEqual(transport.calls, [])
        # A good run the next day replaces the record, so a later single timeout is held again.
        self.run_once(receipt(), transport=transport, alert=ALERT_ON, clock=NOW + timedelta(days=1))
        self.assertEqual([c["body"]["status"] for c in transport.calls], ["report"])
        held = self.run_once(None, source=Locked(receipt()), transport=transport, alert=ALERT_ON, clock=NOW + timedelta(days=2))
        self.assertEqual(held["alert_status"], "held_until_repeat")
        # Still failing on the next day: emailed once.
        again = self.run_once(None, source=Locked(receipt()), transport=transport, alert=ALERT_ON, clock=NOW + timedelta(days=3))
        self.assertEqual(again["alert_status"], "sent")
        self.assertEqual(transport.calls[-1]["body"]["failure_class"], "lock_timeout")

    def test_list_is_capped_and_the_rest_counted(self):
        rows = [row for i in range(15) for row in receipt(doc_no="CS-T%04d" % i, key=i)]
        transport = FakeTransport()
        result = self.run_once(rows, transport=transport, alert=ALERT_ON, max_listed=5)
        body = transport.calls[0]["body"]
        self.assertEqual((len(result["rows"]), len(body["rows"]), body["rows_not_listed"]), (15, 5, 10))

    def test_scheduled_month(self):
        s = self.settings()
        self.assertEqual(xr.scheduled_month(s, lambda: datetime(2026, 12, 1, 9, 15, tzinfo=SGT)), (MONTH, "not_due"))
        self.assertEqual(xr.scheduled_month(s, lambda: NOW), (MONTH, None))
        self.run_once(receipt())
        self.assertEqual(xr.scheduled_month(s, lambda: NOW), (MONTH, "already_reported"))

    def test_scheduled_month_retries_a_failed_or_unsent_month(self):
        s = self.settings()
        self.run_once(receipt(), transport=FakeTransport(status=500), alert=ALERT_ON)
        self.assertEqual(xr.scheduled_month(s, lambda: NOW), (MONTH, None))
        self.run_once(receipt(), send=False)
        self.assertEqual(xr.scheduled_month(s, lambda: NOW), (MONTH, None))

    def test_cli_check_settings_and_scheduled_not_due(self):
        config = self.tmp / "settings.local.json"
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["output"]["record_dir"] = str(self.tmp / "records")
        config.write_text(json.dumps(raw), encoding="utf-8")
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "xb_member_pos_exceptions.py"), "check-settings",
                              "--config", str(config)], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout)
        self.assertEqual((payload["status"], payload["run_day"], payload["alert_enabled"]), ("valid", 2, False))
        self.assertEqual(payload["queries_sha256"], xr.queries_sha256())
        code = xr.main(["run", "--scheduled", "--config", str(config)], clock=lambda: datetime(2026, 12, 1, 9, 15, tzinfo=SGT))
        self.assertEqual(code, 0)
        bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "xb_member_pos_exceptions.py"), "run",
                              "--month", "2026-13", "--config", str(config)], capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 1)


class InstallerAndDocsTests(unittest.TestCase):
    def test_installer_is_ascii_dry_run_first_and_daily(self):
        text = INSTALLER_PATH.read_bytes().decode("ascii")
        self.assertIn("New-ScheduledTaskTrigger -Daily -At $startAt", text)
        self.assertIn('run --scheduled --config', text)
        self.assertLess(text.index("if ($DryRun) {"), text.index("Register-ScheduledTask `"))
        self.assertIn('"XB Member Discount Check"', text)

    def test_installer_powershell_parse_only(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("Neither pwsh nor powershell is available for the parse-only check")
        command = (
            "$errs = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$errs) | Out-Null; "
            "if (@($errs).Count -gt 0) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 } else { exit 0 }"
        ) % str(INSTALLER_PATH).replace("'", "''")
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installer_dry_run_under_windows_powershell(self):
        if sys.platform != "win32" or not shutil.which("powershell"):
            self.skipTest("Windows PowerShell 5.1 runs only on Windows")
        tmp = Path(tempfile.mkdtemp(prefix="xr-inst-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stub = tmp / "stub"
        stub.mkdir()
        (stub / "pyodbc.py").write_text("# test stub: the installer only imports pyodbc\n", encoding="utf-8")
        raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        raw["output"]["record_dir"] = str(tmp / "records")
        config = tmp / "settings.local.json"
        config.write_text(json.dumps(raw), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (str(stub), os.environ.get("PYTHONPATH")) if p))
        args = [shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                str(INSTALLER_PATH), "-UserId", "EXAMPLE-PC\\example-user", "-ConfigPath", str(config),
                "-PythonExe", sys.executable, "-TaskName", "XB member discount installer test (never registered)", "-DryRun"]
        done = subprocess.run(args, capture_output=True, text=True, timeout=180, env=env, cwd=str(tmp))
        out = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, out)
        self.assertIn("Runs daily at 09:15 (reports the previous month once from day 2), IgnoreNew, 30 minute limit, "
                      "RunLevel Limited, LogonType S4U (windows_integrated).", out)
        self.assertIn("Nothing was registered.", out)

    def test_runbook_and_readme_name_the_pieces(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for needle in ("xb_member_pos_exceptions.py", "install_member_pos_exceptions_task.ps1", "-DryRun",
                       "check-settings", "--no-send", "MEMBER-POS", "member_pos_exceptions_alert.workflow.json",
                       "allow_privileged_identity", "pos_sync_stale", "Built, switched off"):
            self.assertIn(needle, runbook)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("member_pos_exceptions_runbook.md", readme)
        self.assertIn("`MEMBER-POS`", readme)

    def test_committed_text_has_no_real_looking_member_number(self):
        for path in (EXAMPLE, RUNBOOK, ROOT / "scripts" / "xb_member_pos_exceptions.py"):
            self.assertIsNone(re.search(r"(?<!\d)[89]\d{7}(?!\d)|(?<!\d)65[89]\d{7}(?!\d)", path.read_text(encoding="utf-8")),
                              path.name)


if __name__ == "__main__":
    unittest.main()
