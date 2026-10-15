"""#155 DirectLocal core: synthetic Shopify captures and a fake AutoCount bridge.

No Shopify connector, AutoCount book, network or real customer data is used.
All identities are synthetic (``*.synthetic.invalid`` store, ``example.test``).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_directlocal as dl  # noqa: E402

HOOK = ROOT / "scripts" / "directlocal_connector_hook.py"
TEMPLATE = ROOT / "config" / "directlocal_claude_settings.template.json"
STORE = "gid://shopify/Shop/900000001"
DOMAIN = "xb-directlocal-test.synthetic.invalid"
T0 = datetime(2026, 10, 14, 0, 0, 0, tzinfo=timezone.utc)
HISTORICAL = "gid://shopify/Customer/9000000001"
NEW = "gid://shopify/Customer/9000000002"
NON_MEMBER = "gid://shopify/Customer/9000000003"
NEW_TWO = "gid://shopify/Customer/9000000004"
PII = ("Synthetic Member Alpha", "6580000001", "80000001", "alpha@example.test")


class SimulatedCrash(BaseException):
    """Stands in for the owning process dying at a precise point."""


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(tool_use_id: str, tool_input: dict, data: dict, captured_at: datetime, **overrides) -> dict:
    value = {
        "schema_version": dl.CAPTURE_SCHEMA, "provenance": dl.CAPTURE_PROVENANCE, "captured_at": stamp(captured_at),
        "session_id": "session-synthetic", "tool_use_id": tool_use_id, "tool_name": dl.CONNECTOR_TOOL,
        "tool_input": tool_input, "tool_response": {"data": data},
    }
    value.update(overrides)
    return value


def page_input(after: str | None) -> dict:
    value = {"query": dl.BASELINE_QUERY, "variables": {"first": dl.PAGE_SIZE, "after": after}, "first": dl.PAGE_SIZE}
    if after is not None:
        value["after"] = after
    return value


def page_data(nodes: list[dict], has_next: bool, end_cursor: str | None, *, store: str = STORE, domain: str = DOMAIN) -> dict:
    return {"shop": {"id": store, "myshopifyDomain": domain}, "customers": {"pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}, "nodes": nodes}}


def metafield_gid(gid: str) -> str:
    return "gid://shopify/Metafield/" + gid.rsplit("/", 1)[1]


def node(gid: str, created: datetime, tags: list[str], *, registered: datetime | None | bool = True) -> dict:
    """Baseline node. ``registered`` is the membership.start_date metafield
    instant (default: same as createdAt); ``None`` means not written yet."""

    when = created if registered is True else registered
    registration = None if when is None else {"id": metafield_gid(gid), "createdAt": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {"id": gid, "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "tags": tags, "registration": registration}


def profile(gid: str, *, created: datetime, registered: datetime | None = None, tags=("member-mg",), phone="+65 8000 0001", email="Alpha@Example.test", start="2026-10-15", expiry="2028-10-14", first="Synthetic", last="Member Alpha", registration_id: str | None = None) -> dict:
    when = created if registered is None else registered
    return {
        "id": gid, "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "tags": list(tags),
        "firstName": first, "lastName": last,
        "defaultEmailAddress": None if email is None else {"emailAddress": email},
        "defaultPhoneNumber": None if phone is None else {"phoneNumber": phone},
        "startDate": None if start is None else {"id": registration_id or metafield_gid(gid), "createdAt": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "date", "value": start},
        "expiryDate": None if expiry is None else {"type": "date", "value": expiry},
    }


def profile_record(tool_use_id: str, customer: dict | None, gid: str, captured_at: datetime) -> dict:
    data = {"shop": {"id": STORE, "myshopifyDomain": DOMAIN}, "customer": customer}
    return record(tool_use_id, {"query": dl.PROFILE_QUERY, "variables": {"id": gid}}, data, captured_at)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeAutoCount:
    def __init__(self):
        self.members: dict[str, dict] = {}
        self.browse: list[tuple[str | None, str | None]] = []
        self.saves = 0
        self.next: list[str] = ["M000201", "M000202", "M000203", "M000204"]
        self.next_calls = 0


class FakeBridge:
    """Mirrors the PowerShell bridge contract, including the claim file.
    Positively marked as the test-only synthetic target."""

    autocount_target = dl.SYNTHETIC_BRIDGE_TARGET

    def __init__(self, ac: FakeAutoCount, *, write_mode: str = "ok", fail: tuple[str, ...] = (), precheck_result: dict | None = None):
        self.ac = ac
        self.write_mode = write_mode
        self.fail = set(fail)
        self.precheck_result = precheck_result
        self.write_calls = 0
        self.precheck_calls: list[tuple] = []

    def precheck(self, keys, email):
        self.precheck_calls.append((tuple(keys), email))
        if "precheck" in self.fail:
            raise dl.BridgeError("bridge_read_failed")
        if self.precheck_result is not None:
            return self.precheck_result
        phone_no = any(key in self.ac.members for key in keys)
        rows = self.ac.browse + [(m["mobile_phone"], m["email_address"]) for m in self.ac.members.values()]
        mobile = any(row_phone is not None and "".join(ch for ch in row_phone if ch.isdigit()) in keys for row_phone, _ in rows)
        email_hit = email is not None and any(row_email is not None and row_email.strip().lower() == email for _, row_email in rows)
        hit = phone_no or mobile or email_hit
        return {"outcome": "CANDIDATE_FOUND" if hit else "NO_CANDIDATE", "phone_member_no_hit": phone_no, "mobile_phone_hit": mobile, "email_hit": email_hit}

    def next_member_no(self):
        if "next" in self.fail:
            raise dl.BridgeError("bridge_read_failed")
        if "next_crash" in self.fail:
            self.fail.discard("next_crash")
            raise SimulatedCrash()
        value = self.ac.next[min(self.ac.next_calls, len(self.ac.next) - 1)]
        self.ac.next_calls += 1
        return value

    def probe(self, member_no):
        if "probe" in self.fail:
            raise dl.BridgeError("bridge_read_failed")
        if "recheck_occupied" in self.fail and member_no in self.ac.members:
            return "OCCUPIED"
        return "OCCUPIED" if member_no in self.ac.members else "FREE"

    def reconcile(self, member_no, payload_text):
        found = member_no in self.ac.members
        return {"found": found, "match": found and self.ac.members[member_no] == json.loads(payload_text)}

    def write_once(self, *, fence_id, claim_directory, deadline, stdin_value, wait_seconds, on_started):
        self.write_calls += 1
        assert dl.payload_digest(stdin_value["salt"], stdin_value["payload_canonical"]) == stdin_value["payload_digest"]
        assert stdin_value["fence_id"] == fence_id
        if self.write_mode == "start_fail":
            return dl.WriteRun(started=False)
        on_started(4242, "synthetic-created-1")
        if self.write_mode == "crash_before_claim":
            return dl.WriteRun(started=True, exit_code=1)
        if self.write_mode == "timeout_before_claim":
            return dl.WriteRun(started=True, exit_code=1, timed_out=True, exited=True)
        claim = Path(claim_directory) / f"{fence_id}.claim"
        try:
            with open(claim, "xb") as handle:
                handle.write(b"{}")
        except FileExistsError:
            return dl.WriteRun(started=True, exit_code=3)
        if self.write_mode == "fail_after_claim":
            return dl.WriteRun(started=True, exit_code=1)
        payload = json.loads(stdin_value["payload_canonical"])
        member_no = stdin_value["member_no"]
        self.ac.saves += 1
        self.ac.members[member_no] = payload
        if self.write_mode == "parent_crash_after_save":
            raise SimulatedCrash()
        if self.write_mode == "timeout_after_save":
            return dl.WriteRun(started=True, timed_out=True, exited=True, exit_code=1)
        if self.write_mode == "timeout_unconfirmed":
            return dl.WriteRun(started=True, timed_out=True, exited=False)
        match = self.write_mode != "mismatch"
        outcome = {"save_invocation_count": 1, "readback_found": True, "readback_match": match, "outbound_queue_changed": self.write_mode == "outbound_changed"}
        return dl.WriteRun(started=True, exit_code=0, stdout=json.dumps(outcome))


class DirectLocalTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-directlocal-"))
        self.clock = Clock(T0)
        self.ledger = dl.Ledger(self.temp, clock=self.clock)
        self.writer_status = "exited"
        self.processor = dl.Processor(self.ledger, writer_probe=lambda pid, created: self.writer_status)
        self.processor.init_ledger(mode="synthetic", store_gid=STORE, shop_domain=DOMAIN)
        self.ac = FakeAutoCount()
        self.counter = 0

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:  # noqa: BLE001 - already closed by a crash test
            pass
        shutil.rmtree(self.temp, ignore_errors=True)

    def reopen(self, *, advance: timedelta = timedelta(0)) -> None:
        self.ledger.close()
        self.clock.now += advance
        self.ledger = dl.Ledger(self.temp, clock=self.clock)
        self.processor = dl.Processor(self.ledger, writer_probe=lambda pid, created: self.writer_status)

    def put(self, value: dict, name: str | None = None) -> Path:
        self.counter += 1
        path = self.temp / "inbox" / f"{name or value['tool_use_id']}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def seal_baseline(self, extra_transition: tuple[str, ...] = ()) -> dict:
        begun = self.processor.begin_run("BASELINE")
        self.assertEqual(begun["cutover_at"], "2026-10-14T00:00:00Z")
        later = T0 + timedelta(minutes=5)
        self.put(record("toolu_page1", page_input(None), page_data([
            node(HISTORICAL, T0 - timedelta(days=30), ["member-mg"]),
            node(NON_MEMBER, T0 - timedelta(days=3), ["vip"]),
        ], True, "cursor-1"), T0 + timedelta(seconds=10)))
        nodes = [node(NEW, later, ["Member-MG "])] + [node(gid, later, ["member-mg"]) for gid in extra_transition]
        self.put(record("toolu_page2", page_input("cursor-1"), page_data(nodes, False, None), T0 + timedelta(seconds=20)))
        self.clock.now = T0 + timedelta(minutes=10)
        return self.processor.ingest_run()

    def fresh_profile(self, gid: str = NEW, **kwargs) -> Path:
        self.counter += 1
        customer = profile(gid, created=T0 + timedelta(minutes=5), **kwargs)
        return self.put(profile_record(f"toolu_profile_{self.counter}", customer, gid, self.clock.now - timedelta(seconds=5)))

    def admit_and_approve(self, gid: str = NEW, **kwargs) -> None:
        result = self.processor.process(self.fresh_profile(gid, **kwargs))
        self.assertEqual(result["customer_state"], "ADMITTED")
        self.processor.approve_write(gid, confirm_suffix=gid.rsplit("/", 1)[1][-6:])

    def write(self, bridge: FakeBridge, gid: str = NEW, **kwargs) -> dict:
        return self.processor.process(self.fresh_profile(gid, **kwargs), bridge=bridge, enable_write=True)

    def customer(self, gid: str = NEW) -> sqlite3.Row:
        return self.ledger.conn.execute("SELECT * FROM customers WHERE customer_gid = ?", (gid,)).fetchone()

    def fence(self, gid: str = NEW) -> sqlite3.Row | None:
        return self.ledger.conn.execute("SELECT * FROM fences WHERE customer_gid = ?", (gid,)).fetchone()

    def assert_no_pii_at_rest(self) -> None:
        self.ledger.conn.execute("PRAGMA wal_checkpoint(FULL)")
        blob = b"".join(path.read_bytes() for path in self.temp.glob("ledger.sqlite3*"))
        for value in PII:
            self.assertNotIn(value.encode("utf-8"), blob, value)


class PhoneKeyTests(unittest.TestCase):
    def test_symmetric_singapore_keys(self):
        self.assertEqual(dl.duplicate_phone_keys("6580000001"), ("6580000001", "80000001"))
        self.assertEqual(dl.duplicate_phone_keys("80000001"), ("80000001", "6580000001"))

    def test_other_shapes_are_single_keys_and_unchanged(self):
        for value in ("15550100101", "658000000", "650080000001", "123456", "800000012"):
            self.assertEqual(dl.duplicate_phone_keys(value), (value,))
        self.assertEqual(dl.duplicate_phone_keys(None), ())

    def test_non_canonical_input_refused(self):
        for value in ("+6580000001", "8000 0001", "", "12345"):
            with self.assertRaises(dl.DirectLocalError):
                dl.duplicate_phone_keys(value)


class CaptureTests(unittest.TestCase):
    def parse(self, value: dict, mode: str = "synthetic"):
        return dl.parse_capture(json.dumps(value).encode("utf-8"), mode=mode, store_gid=STORE, shop_domain=DOMAIN)

    def good_page(self, **overrides) -> dict:
        value = record("toolu_x", page_input(None), page_data([node(NEW, T0, ["member-mg"])], False, None), T0)
        value.update(overrides)
        return value

    def test_valid_page_and_profile(self):
        self.assertEqual(self.parse(self.good_page()).kind, "baseline")
        capture = self.parse(profile_record("toolu_p", profile(NEW, created=T0), NEW, T0))
        self.assertEqual(capture.kind, "profile")
        self.assertNotIn("Synthetic", repr(capture))

    def test_rejections(self):
        cases = {
            "capture_store_identity_mismatch": record("toolu_x", page_input(None), page_data([], False, None, store="gid://shopify/Shop/900000002"), T0),
            "capture_store_identity_mismatch ": record("toolu_x", page_input(None), page_data([], False, None, domain="other.synthetic.invalid"), T0),
            "query_not_pinned": record("toolu_x", {**page_input(None), "query": dl.BASELINE_QUERY + " "}, page_data([], False, None), T0),
            "pagination_variables_invalid": record("toolu_x", {**page_input(None), "first": 10}, page_data([], False, None), T0),
            "pagination_variables_invalid ": record("toolu_x", {**page_input("cursor-1"), "after": "cursor-2"}, page_data([], False, None), T0),
            "baseline_page_shape_invalid": record("toolu_x", page_input(None), page_data([{**node(NEW, T0, []), "email": "x"}], False, None), T0),
            "capture_response_shape_invalid": self.good_page(tool_response={"data": {}, "errors": [{"message": "x"}]}),
            "capture_tool_not_allowed": self.good_page(tool_name="mcp__27c48ff3-54ed-4728-aa90-a7906cba3158__graphql_mutation"),
            "capture_record_shape_invalid": self.good_page(provenance="shopify_wire_bytes"),
            "capture_tool_use_id_invalid": self.good_page(tool_use_id="../escape"),
            "profile_identity_mismatch": profile_record("toolu_p", profile(NEW_TWO, created=T0), NEW, T0),
            "profile_variables_invalid": record("toolu_p", {"query": dl.PROFILE_QUERY, "variables": {"id": "gid://shopify/Order/1"}}, {}, T0),
        }
        for code, value in cases.items():
            with self.subTest(code=code.strip()):
                with self.assertRaises(dl.CaptureRejected) as raised:
                    self.parse(value)
                self.assertEqual(raised.exception.code, code.strip())

    def test_unqualified_real_response_shape_fails_closed(self):
        with self.assertRaises(dl.CaptureRejected) as raised:
            self.parse(self.good_page(), mode="attended")
        self.assertEqual(raised.exception.code, "capture_response_shape_unqualified")


class StateRootTests(unittest.TestCase):
    def test_refusals(self):
        with tempfile.TemporaryDirectory() as temp:
            nested = Path(temp) / "child"
            nested.mkdir()
            self.assertEqual(dl.validate_state_root(nested, environ={}), nested.resolve())
            cases = {
                "state_root_unc_refused": ("\\\\server\\share\\xb", {}),
                "state_root_not_absolute": ("relative\\path", {}),
                "state_root_location_refused": (str(nested), {"LOCALAPPDATA": temp}),
                "state_root_missing": (str(Path(temp) / "absent"), {}),
            }
            for code, (path, env) in cases.items():
                with self.subTest(code=code), self.assertRaises(dl.DirectLocalError) as raised:
                    dl.validate_state_root(path, environ=env)
                self.assertEqual(raised.exception.code, code)
        with self.assertRaises(dl.DirectLocalError) as raised:
            dl.validate_state_root(ROOT / "member_gateway", environ={})
        self.assertEqual(raised.exception.code, "state_root_location_refused")


class LedgerTests(DirectLocalTestCase):
    def test_second_owner_is_refused(self):
        with self.assertRaises(dl.DirectLocalError) as raised:
            dl.Ledger(self.temp, clock=self.clock)
        self.assertEqual(raised.exception.code, "ledger_locked_by_another_owner")

    def test_stale_epoch_cannot_mutate(self):
        self.ledger.conn.execute("UPDATE writer_owner SET epoch = epoch + 1")
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.begin_run("BASELINE")
        self.assertEqual(raised.exception.code, "ledger_owner_epoch_lost")

    def test_reopen_advances_epoch_and_keeps_chain(self):
        epoch = self.ledger.epoch
        self.reopen()
        self.assertEqual(self.ledger.epoch, epoch + 1)
        self.assertGreaterEqual(self.ledger.verify_events(), 1)

    def test_append_only_and_immutable_tables(self):
        self.seal_baseline()
        statements = (
            "UPDATE events SET code = 'x'",
            "DELETE FROM events",
            "UPDATE runs SET cutover_at = '2026-10-13T00:00:00Z'",
            "UPDATE runs SET page_count = 99",
            "DELETE FROM baseline_members",
            "INSERT INTO baseline_members(customer_gid) VALUES ('gid://shopify/Customer/1')",
            "UPDATE ledger_meta SET store_gid = 'gid://shopify/Shop/1'",
            "DELETE FROM captures",
        )
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                self.ledger.conn.execute(statement)

    def test_tampered_event_chain_detected(self):
        self.ledger.conn.execute("DROP TRIGGER events_append_only")
        self.ledger.conn.execute("UPDATE events SET code = 'tampered' WHERE seq = 1")
        with self.assertRaises(dl.DirectLocalError):
            self.ledger.verify_events()

    def test_only_synthetic_mode_initialises(self):
        other = Path(tempfile.mkdtemp(prefix="xb-directlocal-"))
        try:
            ledger = dl.Ledger(other, clock=self.clock)
            try:
                processor = dl.Processor(ledger)
                for mode, store, domain, code in (
                    ("attended", STORE, DOMAIN, "capture_response_shape_unqualified"),
                    ("synthetic", STORE, "xb.myshopify.com", "synthetic_store_identity_invalid"),
                ):
                    with self.subTest(mode=mode, domain=domain), self.assertRaises(dl.DirectLocalError) as raised:
                        processor.init_ledger(mode=mode, store_gid=store, shop_domain=domain)
                    self.assertEqual(raised.exception.code, code)
            finally:
                ledger.close()
        finally:
            shutil.rmtree(other, ignore_errors=True)


class BaselineTests(DirectLocalTestCase):
    def test_exhaustive_capture_seals_with_cutover_partition(self):
        result = self.seal_baseline()
        self.assertEqual((result["pages"], result["historical"], result["transition"], result["discovered"]), (2, 1, 1, 1))
        self.assertEqual(self.customer(NEW)["state"], "DISCOVERED")
        self.assertIsNone(self.customer(HISTORICAL))
        self.assertIsNone(self.customer(NON_MEMBER))
        self.assertEqual(self.ledger.conn.execute("SELECT customer_gid FROM baseline_members").fetchall()[0][0], HISTORICAL)

    def test_missing_page_never_seals_then_completes(self):
        self.processor.begin_run("BASELINE")
        self.put(record("toolu_page1", page_input(None), page_data([node(HISTORICAL, T0 - timedelta(days=1), ["member-mg"])], True, "cursor-1"), T0 + timedelta(seconds=1)))
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.ingest_run()
        self.assertEqual(raised.exception.code, "run_not_sealed:baseline_read_failed:capture_page_missing")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM baseline_members").fetchone()[0], 0)
        self.put(record("toolu_page2", page_input("cursor-1"), page_data([], False, None), T0 + timedelta(seconds=2)))
        self.assertEqual(self.processor.ingest_run()["pages"], 2)

    def test_fail_closed_cases(self):
        cases = {
            "page_capture_conflict": [
                record("toolu_a", page_input(None), page_data([], False, None), T0 + timedelta(seconds=1)),
                record("toolu_b", page_input(None), page_data([node(NEW, T0, ["member-mg"])], False, None), T0 + timedelta(seconds=2)),
            ],
            "run_not_sealed:baseline_duplicate_gid": [
                record("toolu_a", page_input(None), page_data([node(NEW, T0, [])], True, "cursor-1"), T0 + timedelta(seconds=1)),
                record("toolu_b", page_input("cursor-1"), page_data([node(NEW, T0, [])], False, None), T0 + timedelta(seconds=2)),
            ],
            "run_not_sealed:baseline_cursor_repeated": [
                record("toolu_a", page_input(None), page_data([], True, "cursor-1"), T0 + timedelta(seconds=1)),
                record("toolu_b", page_input("cursor-1"), page_data([], True, "cursor-1"), T0 + timedelta(seconds=2)),
            ],
            "run_not_sealed:unchained_page_capture": [
                record("toolu_a", page_input(None), page_data([], False, None), T0 + timedelta(seconds=1)),
                record("toolu_b", page_input("cursor-9"), page_data([], False, None), T0 + timedelta(seconds=2)),
            ],
            "page_capture_predates_run": [
                record("toolu_a", page_input(None), page_data([], False, None), T0 - timedelta(seconds=1)),
            ],
        }
        for code, captures in cases.items():
            with self.subTest(code=code):
                temp = Path(tempfile.mkdtemp(prefix="xb-directlocal-"))
                ledger = dl.Ledger(temp, clock=Clock(T0))
                try:
                    processor = dl.Processor(ledger)
                    processor.init_ledger(mode="synthetic", store_gid=STORE, shop_domain=DOMAIN)
                    processor.begin_run("BASELINE")
                    for value in captures:
                        (temp / "inbox" / f"{value['tool_use_id']}.json").write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(dl.DirectLocalError) as raised:
                        processor.ingest_run()
                    self.assertEqual(raised.exception.code, code)
                    self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM runs WHERE state = 'SEALED'").fetchone()[0], 0)
                finally:
                    ledger.close()
                    shutil.rmtree(temp, ignore_errors=True)

    def test_identical_recapture_is_consumed_with_the_run(self):
        self.processor.begin_run("BASELINE")
        page = page_data([node(NEW, T0 + timedelta(minutes=1), ["member-mg"])], False, None)
        self.put(record("toolu_a", page_input(None), page, T0 + timedelta(seconds=1)))
        self.put(record("toolu_b", page_input(None), page, T0 + timedelta(seconds=2)))
        self.assertEqual(self.processor.ingest_run()["pages"], 1)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 2)
        self.clock.now = T0 + timedelta(hours=1)
        self.processor.begin_run("DISCOVERY")
        self.put(record("toolu_c", page_input(None), page, T0 + timedelta(hours=1, seconds=1)))
        self.assertEqual(self.processor.ingest_run()["discovered"], 0)

    def test_foreign_store_capture_in_inbox_blocks_ingest(self):
        self.processor.begin_run("BASELINE")
        self.put(record("toolu_a", page_input(None), page_data([], False, None, store="gid://shopify/Shop/900000002"), T0 + timedelta(seconds=1)))
        with self.assertRaises(dl.CaptureRejected) as raised:
            self.processor.ingest_run()
        self.assertEqual(raised.exception.code, "capture_store_identity_mismatch")

    def test_cutover_and_baseline_are_single_and_immutable(self):
        self.seal_baseline()
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.begin_run("BASELINE")
        self.assertEqual(raised.exception.code, "baseline_already_exists")

    def test_discovery_reuses_cutover_and_suppresses_replayed_pages(self):
        self.seal_baseline()
        self.clock.now = T0 + timedelta(hours=2)
        run = self.processor.begin_run("DISCOVERY")
        self.assertEqual(run["cutover_at"], "2026-10-14T00:00:00Z")
        late_historical = "gid://shopify/Customer/9000000009"
        self.put(record("toolu_disc", page_input(None), page_data([
            node(HISTORICAL, T0 - timedelta(days=30), ["member-mg"]),
            node(NEW, T0 + timedelta(minutes=5), ["member-mg"]),
            node(NEW_TWO, T0 + timedelta(hours=1), ["member-mg"]),
            node(late_historical, T0 - timedelta(days=1), ["member-mg"]),
        ], False, None), T0 + timedelta(hours=2, seconds=1)))
        result = self.processor.ingest_run()
        self.assertEqual(result["discovered"], 2)  # NEW_TWO and the unbaselined pre-cutover GID
        self.assertEqual(self.customer(late_historical)["state"], "DISCOVERED")
        self.assertIsNone(self.customer(HISTORICAL))


class AdmissionTests(DirectLocalTestCase):
    def setUp(self):
        super().setUp()
        self.seal_baseline(extra_transition=(NEW_TWO,))

    def test_admit_records_no_pii_and_discards_capture(self):
        path = self.fresh_profile()
        result = self.processor.process(path)
        self.assertEqual(result, {"customer_ref": self.processor._ref("c", NEW), "customer_state": "ADMITTED", "code": None, "capture_discarded": True})
        self.assertRegex(result["customer_ref"], r"^c-[0-9a-f]{20}$")
        self.assertFalse(path.exists())
        self.assert_no_pii_at_rest()

    def test_replayed_capture_refused(self):
        path = self.fresh_profile()
        raw = path.read_bytes()
        self.processor.process(path)
        path.write_bytes(raw)
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.process(path)
        self.assertEqual(raised.exception.code, "capture_replayed")

    def test_decisions(self):
        result = self.processor.process(self.fresh_profile(NEW_TWO, tags=("member-mg", "XB Member")))
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "legacy_tag_present"))
        result = self.processor.process(self.fresh_profile(NEW, tags=("vip",)))
        self.assertEqual((result["customer_state"], result["code"]), ("NOT_ELIGIBLE", "not_member_mg"))
        # NOT_ELIGIBLE is re-evaluable; a malformed phone then goes to review.
        result = self.processor.process(self.fresh_profile(NEW, phone="+65 8000-00x1"))
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "phone_malformed"))
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.process(self.fresh_profile(NEW))
        self.assertEqual(raised.exception.code, "customer_state_terminal")

    def test_wait_is_bounded_then_review(self):
        for check in range(1, dl.PROFILE_WAIT_MAX_CHECKS + 1):
            result = self.processor.process(self.fresh_profile(start=None))
            expected = "MANUAL_REVIEW" if check == dl.PROFILE_WAIT_MAX_CHECKS else "WAITING"
            self.assertEqual(result["customer_state"], expected)

    def test_baseline_member_and_undiscovered_gid(self):
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.process(self.fresh_profile(NON_MEMBER))
        self.assertEqual(raised.exception.code, "customer_not_discovered")
        # A historical GID can only reach the processor through discovery; the
        # sealed baseline still excludes it.
        self.ledger.conn.execute("INSERT INTO customers(store_gid, customer_gid, state, updated_at) VALUES (?, ?, 'DISCOVERED', 'x')", (STORE, HISTORICAL))
        customer = profile(HISTORICAL, created=T0 - timedelta(days=30))
        result = self.processor.process(self.put(profile_record("toolu_hist", customer, HISTORICAL, self.clock.now)))
        self.assertEqual(result["customer_state"], "EXCLUDED_BASELINE")

    def test_stale_capture_refused(self):
        customer = profile(NEW, created=T0 + timedelta(minutes=5))
        path = self.put(profile_record("toolu_old", customer, NEW, self.clock.now - timedelta(seconds=dl.CAPTURE_MAX_AGE_SECONDS + 1)))
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.process(path)
        self.assertEqual(raised.exception.code, "capture_stale")

    def test_capture_outside_inbox_refused(self):
        path = self.fresh_profile()
        outside = self.temp / "elsewhere.json"
        shutil.move(path, outside)
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.process(outside)
        self.assertEqual(raised.exception.code, "capture_outside_inbox")


class WriteTests(DirectLocalTestCase):
    def setUp(self):
        super().setUp()
        self.seal_baseline()

    def test_happy_path_writes_once_and_records_no_pii(self):
        self.admit_and_approve()
        bridge = FakeBridge(self.ac)
        result = self.write(bridge)
        self.assertEqual(result["fence_state"], "CREATED_VERIFIED")
        self.assertEqual(self.ac.saves, 1)
        self.assertEqual(bridge.precheck_calls, [(("6580000001", "80000001"), "alpha@example.test")])
        self.assertEqual(self.ac.members["M000201"]["mobile_phone"], "6580000001")  # storage format unchanged
        self.assertEqual(self.ac.members["M000201"]["register_date"], "2026-10-15")
        self.assertEqual(self.customer()["state"], "FENCED")
        self.assert_no_pii_at_rest()
        self.assertTrue(self.ledger.verify_events() > 0)

    def test_write_requires_approval_and_flag(self):
        self.processor.process(self.fresh_profile())
        bridge = FakeBridge(self.ac)
        self.assertEqual(self.processor.process(self.fresh_profile(), bridge=bridge, enable_write=False)["customer_state"], "ADMITTED")
        result = self.write(bridge)
        self.assertEqual(result["code"], "write_approval_missing")
        self.assertEqual((bridge.write_calls, self.ac.saves), (0, 0))

    def test_approval_expires_and_confirmation_must_match(self):
        self.processor.process(self.fresh_profile())
        with self.assertRaises(dl.DirectLocalError):
            self.processor.approve_write(NEW, confirm_suffix="999999")
        self.processor.approve_write(NEW, confirm_suffix="000002", ttl_seconds=60)
        self.clock.now += timedelta(seconds=61)
        bridge = FakeBridge(self.ac)
        self.assertEqual(self.write(bridge)["code"], "write_approval_missing")
        self.assertEqual(self.ac.saves, 0)

    def test_symmetric_duplicates_and_lookup_failures_go_to_review(self):
        cases = [
            ("local_row_vs_65_payload", {"browse": [("8000 0001", None)]}, {}, "legacy_candidate_found"),
            ("65_row_vs_local_payload", {"browse": [("+65 8000 0001", None)]}, {"phone": "80000001"}, "legacy_candidate_found"),
            ("legacy_member_no_is_local_phone", {"member": "80000001"}, {}, "legacy_candidate_found"),
            ("email", {"browse": [(None, " ALPHA@example.TEST ")]}, {"phone": None}, "legacy_candidate_found"),
            ("lookup_failed", {"fail": ("precheck",)}, {}, "legacy_lookup_failed"),
            ("ambiguous", {"result": {"outcome": "NO_CANDIDATE", "phone_member_no_hit": False, "mobile_phone_hit": None, "email_hit": False}}, {}, "legacy_precheck_ambiguous"),
        ]
        for name, setup, kwargs, code in cases:
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                self.admit_and_approve(**kwargs)
                ac = self.ac
                ac.browse = list(setup.get("browse", []))
                if "member" in setup:
                    ac.members[setup["member"]] = {"mobile_phone": None, "email_address": None}
                bridge = FakeBridge(ac, fail=setup.get("fail", ()), precheck_result=setup.get("result"))
                result = self.write(bridge, **kwargs)
                self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", code))
                self.assertEqual((bridge.write_calls, ac.saves, ac.next_calls), (0, 0, 0))

    def test_member_no_allocation_rules(self):
        cases = [
            ("two_occupied_then_free", {"occupied": ("M000201", "M000202")}, (), "CREATED_VERIFIED", "M000203"),
            ("three_occupied", {"occupied": ("M000201", "M000202", "M000203")}, (), "member_no_candidates_exhausted", None),
            ("not_advancing", {"occupied": ("M000201",), "next": ["M000201", "M000201"]}, (), "member_no_generator_not_advancing", None),
            ("generator_failed", {}, ("next",), "member_no_generator_failed", None),
            ("probe_failed", {}, ("probe",), "member_no_probe_failed", None),
            ("invalid_member_no", {"next": ["M 1"]}, (), "member_no_generator_invalid", None),
        ]
        for name, setup, fail, expected, member_no in cases:
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                self.admit_and_approve()
                for occupied in setup.get("occupied", ()):
                    self.ac.members[occupied] = {"mobile_phone": None, "email_address": None}
                if "next" in setup:
                    self.ac.next = setup["next"]
                bridge = FakeBridge(self.ac, fail=fail)
                result = self.write(bridge)
                if member_no:
                    self.assertEqual(result["fence_state"], expected)
                    self.assertEqual(self.fence()["member_no"], member_no)
                    self.assertEqual(self.ac.saves, 1)
                else:
                    self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", expected))
                    self.assertEqual((bridge.write_calls, self.ac.saves), (0, 0))
                self.assertLessEqual(self.ac.next_calls, dl.MAX_MEMBER_NO_CANDIDATES)

    def test_values_autocount_cannot_store_go_to_review_at_admission(self):
        # nvarchar(n) counts UTF-16 code units: an astral character counts twice.
        cases = [
            ("name", {"first": "Synthetic", "last": "M" * 100}, "autocount_field_width_exceeded"),
            ("email", {"email": "a" * 190 + "@example.test"}, "autocount_field_width_exceeded"),
            ("astral_name", {"first": "S", "last": "\U0001F600" * 50}, "autocount_field_width_exceeded"),  # 1 + 1 + 100 units
            ("control_character", {"first": "Synthetic", "last": "Member\x07Alpha"}, "autocount_text_invalid"),
            ("unpaired_surrogate", {"first": "Synthetic", "last": "Member \ud800"}, "autocount_text_invalid"),
        ]
        for name, kwargs, code in cases:
            with self.subTest(field=name):
                self.tearDown()
                self.setUp()
                result = self.processor.process(self.fresh_profile(**kwargs))
                self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", code))
                bridge = FakeBridge(self.ac)
                with self.assertRaises(dl.DirectLocalError):
                    self.write(bridge, **kwargs)  # terminal: never reaches any lookup or write
                self.assertEqual((bridge.precheck_calls, bridge.write_calls, self.ac.next_calls, self.ac.saves), ([], 0, 0, 0))
        for last in ("M" * 90, "\U0001F600" * 44 + "M"):  # exactly 100 code units are accepted
            with self.subTest(last=last[:3]):
                self.tearDown()
                self.setUp()
                self.admit_and_approve(first="Synthetic", last=last)
                self.assertEqual(self.write(FakeBridge(self.ac), first="Synthetic", last=last)["fence_state"], "CREATED_VERIFIED")

    def test_utf16_code_units(self):
        self.assertEqual((dl.utf16_code_units("abc"), dl.utf16_code_units("\u6c88"), dl.utf16_code_units("\U0001F600")), (3, 1, 2))

    def test_member_no_held_by_another_customer_counts_as_occupied(self):
        self.ledger.conn.execute("UPDATE customers SET member_no = 'M000201' WHERE customer_gid = ?", (NEW,))
        self.ledger.conn.execute("INSERT INTO customers(store_gid, customer_gid, state, updated_at) VALUES (?, ?, 'DISCOVERED', 'x')", (STORE, NEW_TWO))
        self.admit_and_approve(NEW_TWO)
        result = self.write(FakeBridge(self.ac), NEW_TWO)
        self.assertEqual(self.fence(NEW_TWO)["member_no"], "M000202")
        self.assertEqual(result["fence_state"], "CREATED_VERIFIED")

    def test_bound_candidate_recheck_never_advances(self):
        self.admit_and_approve()
        bridge = FakeBridge(self.ac)
        calls = []

        def probe(member_no):
            calls.append(member_no)
            return "FREE" if len(calls) == 1 else "OCCUPIED"

        bridge.probe = probe
        result = self.write(bridge)
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "bound_member_no_recheck_failed"))
        self.assertEqual(calls, ["M000201", "M000201"])
        self.assertEqual((bridge.write_calls, self.ac.saves), (0, 0))

    def test_crash_during_allocation_spends_the_attempt(self):
        self.admit_and_approve()
        bridge = FakeBridge(self.ac, fail=("next_crash",))
        with self.assertRaises(SimulatedCrash):
            self.write(bridge)
        self.reopen()
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM allocation_attempts").fetchone()[0], 1)
        result = self.write(FakeBridge(self.ac))
        self.assertEqual(result["fence_state"], "CREATED_VERIFIED")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM allocation_attempts").fetchone()[0], 2)

    def test_outcome_classification(self):
        cases = {
            "start_fail": ("NOT_WRITTEN_PROVEN", "writer_start_failed", 0),
            "crash_before_claim": ("NOT_WRITTEN_PROVEN", "writer_failed_before_claim", 0),
            "timeout_before_claim": ("NOT_WRITTEN_PROVEN", "writer_timeout_before_claim", 0),
            "fail_after_claim": ("WRITE_OUTCOME_UNCERTAIN", "writer_failed_after_claim", 0),
            "timeout_after_save": ("WRITE_OUTCOME_UNCERTAIN", "writer_timeout", 1),
            "timeout_unconfirmed": ("WRITER_TERMINATION_UNCONFIRMED", "writer_timeout_exit_unconfirmed", 1),
            "mismatch": ("CREATED_READBACK_MISMATCH", "readback_mismatch_manual_review", 1),
            "outbound_changed": ("CREATED_OUTBOUND_QUEUE_CHANGED", "outbound_queue_changed_manual_review", 1),
        }
        for mode, (state, code, saves) in cases.items():
            with self.subTest(mode=mode):
                self.tearDown()
                self.setUp()
                self.admit_and_approve()
                bridge = FakeBridge(self.ac, write_mode=mode)
                result = self.write(bridge)
                self.assertEqual((result["fence_state"], result["code"]), (state, code))
                self.assertEqual((bridge.write_calls, self.ac.saves), (1, saves))
                self.assert_never_rewritten(saves)

    def assert_never_rewritten(self, saves: int) -> None:
        """No later attempt (fresh capture, new approval, recovery) writes again."""

        self.clock.now += timedelta(hours=1)
        self.writer_status = "exited"
        self.processor.recover()
        bridge = FakeBridge(self.ac)
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.write(bridge)
        self.assertEqual(raised.exception.code, "customer_state_terminal")
        with self.assertRaises(dl.DirectLocalError):
            self.processor.approve_write(NEW, confirm_suffix="000002")
        self.assertEqual((bridge.write_calls, self.ac.saves), (0, saves))

    def test_fence_rows_cannot_be_rewound(self):
        self.admit_and_approve()
        self.write(FakeBridge(self.ac))
        for statement in (
            "UPDATE fences SET state = 'DISPATCHED'",
            "UPDATE fences SET state = 'FENCED'",
            "UPDATE fences SET member_no = 'M999999'",
            "DELETE FROM fences",
            "UPDATE customers SET state = 'ADMITTED'",
            "UPDATE approvals SET consumed_at = NULL",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                self.ledger.conn.execute(statement)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.conn.execute(
                "INSERT INTO fences(fence_id, customer_gid, member_no, approval_id, payload_digest, salt, owner_epoch, state, created_at, updated_at) "
                "SELECT 'fence-x', customer_gid, 'M000999', approval_id, payload_digest, salt, owner_epoch, 'FENCED', created_at, updated_at FROM fences"
            )


class CrashRecoveryTests(DirectLocalTestCase):
    def setUp(self):
        super().setUp()
        self.seal_baseline()
        self.admit_and_approve()

    def test_crash_between_fence_and_dispatch_never_writes(self):
        original = dl.Processor._fence_state

        def crash_on_dispatch(processor, conn, fence, state, code, **extra):
            if state == "DISPATCHED":
                raise SimulatedCrash()
            return original(processor, conn, fence, state, code, **extra)

        bridge = FakeBridge(self.ac)
        with mock.patch.object(dl.Processor, "_fence_state", crash_on_dispatch), self.assertRaises(SimulatedCrash):
            self.write(bridge)
        self.reopen()
        self.assertEqual(self.processor.recover()["orphaned"], 1)
        self.assertEqual((self.fence()["state"], self.fence()["code"]), ("MANUAL_REVIEW", "fence_orphaned_pre_dispatch"))
        self.assertEqual((bridge.write_calls, self.ac.saves), (0, 0))

    def test_parent_crash_after_save_holds_then_uncertain_then_reconciles(self):
        bridge = FakeBridge(self.ac, write_mode="parent_crash_after_save")
        with self.assertRaises(SimulatedCrash):
            self.write(bridge)
        self.assertEqual(self.ac.saves, 1)
        self.reopen()
        with self.assertRaises(dl.DirectLocalHold):
            self.processor.recover()  # before deadline + grace: writer may be live
        self.clock.now += timedelta(seconds=dl.WRITER_DEADLINE_SECONDS + dl.WRITER_GRACE_SECONDS + 1)
        for status, code in (("running", "writer_may_be_live"), ("unknown", "writer_exit_unproven")):
            self.writer_status = status
            with self.assertRaises(dl.DirectLocalHold) as raised:
                self.processor.recover()  # recorded child running, or exit not provable
            self.assertEqual(raised.exception.code, code)
        self.writer_status = "exited"
        self.assertEqual(self.processor.recover()["uncertain"], 1)
        self.assertEqual(self.fence()["state"], "WRITE_OUTCOME_UNCERTAIN")
        replay = FakeBridge(self.ac)
        with self.assertRaises(dl.DirectLocalError):
            self.write(replay)
        result = self.processor.reconcile(self.fresh_profile(), bridge=replay)
        self.assertEqual(result["fence_state"], "RECONCILED_VERIFIED")
        self.assertEqual((replay.write_calls, self.ac.saves), (0, 1))

    def test_reconcile_detects_changed_source_and_absent_member(self):
        bridge = FakeBridge(self.ac, write_mode="fail_after_claim")
        self.write(bridge)
        self.assertEqual(self.fence()["state"], "WRITE_OUTCOME_UNCERTAIN")
        result = self.processor.reconcile(self.fresh_profile(email="changed@example.test"), bridge=bridge)
        self.assertEqual(result, {"fence_state": "MANUAL_REVIEW", "code": "reconcile_source_changed"})
        self.assertEqual(self.ac.saves, 0)

    def test_reconcile_absent_member_is_manual_followup(self):
        self.write(FakeBridge(self.ac, write_mode="fail_after_claim"))
        result = self.processor.reconcile(self.fresh_profile(), bridge=FakeBridge(self.ac))
        self.assertEqual((result["fence_state"], result["code"]), ("MANUAL_REVIEW", "confirmed_absent_manual_followup"))
        self.assertRegex(result["member_ref"], r"^m-[0-9a-f]{20}$")

    def test_unrecorded_writer_identity_holds_until_attended_release(self):
        bridge = FakeBridge(self.ac, write_mode="parent_crash_after_save")
        original = bridge.write_once
        bridge.write_once = lambda **kw: original(**{**kw, "on_started": lambda pid, created: None})
        with self.assertRaises(SimulatedCrash):
            self.write(bridge)
        self.reopen(advance=timedelta(hours=1))
        for _ in range(2):  # stays on HOLD however often recovery runs
            with self.assertRaises(dl.DirectLocalHold) as raised:
                self.processor.recover()
            self.assertEqual(raised.exception.code, "writer_identity_unrecorded")
        with self.assertRaises(dl.DirectLocalHold):
            self.write(FakeBridge(self.ac))  # every command is blocked by the HOLD
        fence_id = self.fence()["fence_id"]
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.release_writer_hold(fence_id, confirm_suffix="00000000")
        self.assertEqual(raised.exception.code, "release_confirmation_mismatch")
        result = self.processor.release_writer_hold(fence_id, confirm_suffix=fence_id[-8:])
        self.assertEqual(result["code"], "operator_attested_writer_absent")
        self.assertEqual(self.fence()["state"], "WRITE_OUTCOME_UNCERTAIN")
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.release_writer_hold(fence_id, confirm_suffix=fence_id[-8:])
        self.assertEqual(raised.exception.code, "no_writer_hold")
        self.assertEqual(self.ac.saves, 1)

    def test_release_refused_before_deadline(self):
        bridge = FakeBridge(self.ac, write_mode="parent_crash_after_save")
        with self.assertRaises(SimulatedCrash):
            self.write(bridge)
        fence_id = self.fence()["fence_id"]
        with self.assertRaises(dl.DirectLocalHold):
            self.processor.release_writer_hold(fence_id, confirm_suffix=fence_id[-8:])
        self.assertEqual(self.fence()["state"], "DISPATCHED")

    def test_missing_claim_with_unproven_exit_is_never_not_written(self):
        bridge = FakeBridge(self.ac)

        def crash_before_claim(**kw):
            kw["on_started"](4242, "synthetic-created-1")
            raise SimulatedCrash()

        bridge.write_once = crash_before_claim
        with self.assertRaises(SimulatedCrash):
            self.write(bridge)
        self.reopen(advance=timedelta(hours=1))
        self.writer_status = "unknown"
        with self.assertRaises(dl.DirectLocalHold):
            self.processor.recover()
        self.writer_status = "exited"
        self.processor.recover()
        self.assertEqual((self.fence()["state"], self.fence()["code"]), ("NOT_WRITTEN_PROVEN", "dispatch_owner_lost_before_claim"))
        self.assertEqual(self.ac.saves, 0)

    def test_termination_unconfirmed_resolves_only_when_child_gone(self):
        self.write(FakeBridge(self.ac, write_mode="timeout_unconfirmed"))
        for status in ("running", "unknown"):
            self.writer_status = status
            with self.assertRaises(dl.DirectLocalHold):
                self.processor.recover()
        self.writer_status = "exited"
        self.processor.recover()
        self.assertEqual(self.fence()["state"], "WRITE_OUTCOME_UNCERTAIN")
        self.assertEqual(self.ac.saves, 1)

    def test_existing_claim_blocks_save(self):
        bridge = FakeBridge(self.ac)
        original = bridge.write_once

        def preclaimed(**kwargs):
            (Path(kwargs["claim_directory"]) / f"{kwargs['fence_id']}.claim").write_bytes(b"{}")
            return original(**kwargs)

        bridge.write_once = preclaimed
        result = self.write(bridge)
        self.assertEqual((result["fence_state"], result["code"]), ("WRITE_OUTCOME_UNCERTAIN", "fence_already_claimed"))
        self.assertEqual(self.ac.saves, 0)


class SyntheticInertnessTests(DirectLocalTestCase):
    """A synthetic ledger never starts a real AutoCount bridge process."""

    def setUp(self):
        super().setUp()
        self.seal_baseline()
        self.admit_and_approve()
        self.spawned: list = []

    def spy_popen(self, *args, **kwargs):
        self.spawned.append(args)
        raise AssertionError("a real bridge process must never be started")

    def test_real_bridge_client_refused_before_any_spawn_or_state_change(self):
        real = dl.BridgeClient(dl.default_bridge_command(), popen=self.spy_popen)
        self.assertEqual(real.autocount_target, dl.REAL_BRIDGE_TARGET)
        path = self.fresh_profile()
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.process(path, bridge=real, enable_write=True)
        self.assertEqual(raised.exception.code, "real_autocount_refused_synthetic_ledger")
        self.assertTrue(path.exists())  # refused before the capture was consumed
        self.assertEqual(self.customer()["state"], "ADMITTED")
        for bridge in (None, real, FakeBridge(self.ac)):
            if bridge is not None and not isinstance(bridge, dl.BridgeClient):
                bridge.autocount_target = dl.REAL_BRIDGE_TARGET  # mislabelled fake is refused too
            with self.subTest(bridge=type(bridge).__name__), self.assertRaises(dl.DirectLocalError) as raised:
                self.processor.process(self.fresh_profile(), bridge=bridge, enable_write=True)
            self.assertEqual(raised.exception.code, "real_autocount_refused_synthetic_ledger")
        self.assertEqual(self.spawned, [])
        self.assertIsNone(self.fence())

    def test_reconcile_refuses_real_bridge(self):
        real = dl.BridgeClient(dl.default_bridge_command(), popen=self.spy_popen)
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.reconcile(self.fresh_profile(), bridge=real)
        self.assertEqual(raised.exception.code, "real_autocount_refused_synthetic_ledger")
        self.assertEqual(self.spawned, [])

    def test_direct_write_path_refuses_real_bridge(self):
        real = dl.BridgeClient(dl.default_bridge_command(), popen=self.spy_popen)
        with self.assertRaises(dl.DirectLocalError):
            self.processor._write(NEW, {"name": "x"}, real)
        self.assertEqual(self.spawned, [])


@unittest.skipUnless(os.name == "nt", "Windows process identity")
class WriterProbeTests(unittest.TestCase):
    def test_pid_reuse_and_indeterminate_identity(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            created = dl.child_creation_time(child)
            self.assertIsNotNone(created)
            self.assertEqual(dl.probe_writer(child.pid, created), "running")
            self.assertEqual(dl.probe_writer(child.pid, None), "unknown")
            self.assertEqual(dl.probe_writer(child.pid, str(int(created) + 1)), "exited")  # same PID, other process
        finally:
            child.kill()
            child.wait(timeout=30)
        self.assertEqual(dl.probe_writer(child.pid, created), "exited")


class RunAbandonTests(DirectLocalTestCase):
    def test_incomplete_baseline_abandoned_and_recaptured_with_new_cutover(self):
        first = self.processor.begin_run("BASELINE")
        self.put(record("toolu_old1", page_input(None), page_data([node(HISTORICAL, T0 - timedelta(days=1), ["member-mg"])], True, "cursor-1"), T0 + timedelta(seconds=1)))
        with self.assertRaises(dl.DirectLocalError):
            self.processor.ingest_run()
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.abandon_run(first["run_id"], confirm_suffix="deadbeef")
        self.assertEqual(raised.exception.code, "abandon_confirmation_mismatch")
        result = self.processor.abandon_run(first["run_id"], confirm_suffix=first["run_id"][-8:])
        self.assertEqual((result["state"], result["captures_retired"]), ("ABANDONED", 1))
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.begin_run("BASELINE")  # same instant: cutover must advance
        self.assertEqual(raised.exception.code, "cutover_not_advanced")
        self.clock.now = T0 + timedelta(minutes=1)
        second = self.processor.begin_run("BASELINE")
        self.assertEqual(second["cutover_at"], "2026-10-14T00:01:00Z")
        self.put(record("toolu_new1", page_input(None), page_data([node(HISTORICAL, T0 - timedelta(days=1), ["member-mg"]), node(NEW, T0 + timedelta(minutes=2), ["member-mg"])], False, None), T0 + timedelta(minutes=1, seconds=1)))
        sealed = self.processor.ingest_run()
        self.assertEqual((sealed["pages"], sealed["historical"], sealed["transition"]), (1, 1, 1))
        for statement in ("UPDATE runs SET state = 'CAPTURING' WHERE state = 'ABANDONED'", "UPDATE runs SET state = 'ABANDONED' WHERE state = 'SEALED'", "DELETE FROM runs"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                self.ledger.conn.execute(statement)
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.abandon_run(second["run_id"], confirm_suffix=second["run_id"][-8:])
        self.assertEqual(raised.exception.code, "run_not_abandonable")
        self.assertEqual(self.ledger.conn.execute("PRAGMA user_version").fetchone()[0], dl.SCHEMA_VERSION)


class IdentityShapeTests(unittest.TestCase):
    """Qualified connector shape, replicated with a synthetic store only."""

    SYN_GID = "gid://shopify/Shop/900000001"
    SYN_DOMAIN = "synthetic-xb-directlocal-0000.myshopify.com"

    def identity(self, response, **overrides) -> bytes:
        value = record("toolu_identity_1", {"query": dl.IDENTITY_QUERY}, {}, T0)
        value["tool_response"] = response
        value.update(overrides)
        return json.dumps(value).encode("utf-8")

    def text(self, value) -> str:
        return json.dumps(value, separators=(",", ":"))

    def test_qualified_shape_yields_store_identity_only(self):
        result = dl.inspect_identity_capture(self.identity(self.text({"data": {"shop": {"id": self.SYN_GID, "myshopifyDomain": self.SYN_DOMAIN}}})))
        self.assertEqual((result["response_shape"], result["store_gid"], result["shop_domain"]), (dl.CONNECTOR_RESPONSE_SHAPE, self.SYN_GID, self.SYN_DOMAIN))

    def test_rejections(self):
        shop = {"id": self.SYN_GID, "myshopifyDomain": self.SYN_DOMAIN}
        cases = {
            "connector_response_shape_invalid": [
                {"data": {"shop": shop}},  # object, not the qualified JSON text
                "not json",
                self.text({"data": {"shop": shop}, "errors": [{"message": "x"}]}),
                self.text({"data": {"shop": shop}, "extensions": {"cost": {}}}),
                self.text([{"type": "text", "text": "{}"}]),
            ],
            "identity_shape_invalid": [
                self.text({"data": {"shop": shop, "customers": {"nodes": []}}}),
                self.text({"data": {"shop": {**shop, "email": "owner@example.test"}}}),
            ],
            "identity_values_invalid": [
                self.text({"data": {"shop": {"id": "gid://shopify/Customer/1", "myshopifyDomain": self.SYN_DOMAIN}}}),
                self.text({"data": {"shop": {"id": self.SYN_GID, "myshopifyDomain": "evil.example.test"}}}),
            ],
        }
        for code, responses in cases.items():
            for response in responses:
                with self.subTest(code=code, response=str(response)[:60]), self.assertRaises(dl.CaptureRejected) as raised:
                    dl.inspect_identity_capture(self.identity(response))
                self.assertEqual(raised.exception.code, code)
        good = self.text({"data": {"shop": shop}})
        for code, overrides in (
            ("capture_not_identity", {"tool_input": page_input(None)}),
            ("identity_variables_invalid", {"tool_input": {"query": dl.IDENTITY_QUERY, "first": 1}}),
            ("capture_record_shape_invalid", {"tool_name": "mcp__27c48ff3-54ed-4728-aa90-a7906cba3158__get-shop-info"}),
        ):
            with self.subTest(code=code), self.assertRaises(dl.CaptureRejected) as raised:
                dl.inspect_identity_capture(self.identity(good, **overrides))
            self.assertEqual(raised.exception.code, code)

    def test_ledger_ingestion_still_rejects_the_connector_shape(self):
        raw = self.identity(self.text({"data": {"shop": {"id": STORE, "myshopifyDomain": DOMAIN}}}))
        with self.assertRaises(dl.CaptureRejected) as raised:
            dl.parse_capture(raw, mode="synthetic", store_gid=STORE, shop_domain=DOMAIN)
        self.assertEqual(raised.exception.code, "capture_response_shape_invalid")


class ClassifyTests(unittest.TestCase):
    def test_exit_zero_requires_exact_outcome(self):
        for stdout in ("", "not json", json.dumps({"save_invocation_count": 2, "readback_found": True, "readback_match": True}), json.dumps({"save_invocation_count": 1, "readback_found": "yes", "readback_match": True})):
            with self.subTest(stdout=stdout):
                self.assertEqual(dl.classify_write(dl.WriteRun(True, 0, stdout), claim_exists=True), ("WRITE_OUTCOME_UNCERTAIN", "writer_outcome_invalid"))
        absent = json.dumps({"save_invocation_count": 1, "readback_found": False, "readback_match": False, "outbound_queue_changed": False})
        missing_flag = json.dumps({"save_invocation_count": 1, "readback_found": True, "readback_match": True})
        self.assertEqual(dl.classify_write(dl.WriteRun(True, 0, missing_flag), claim_exists=True), ("WRITE_OUTCOME_UNCERTAIN", "writer_outcome_invalid"))
        self.assertEqual(dl.classify_write(dl.WriteRun(True, 0, absent), claim_exists=True), ("WRITE_OUTCOME_UNCERTAIN", "readback_absent"))

    def test_start_failure_with_claim_is_uncertain(self):
        self.assertEqual(dl.classify_write(dl.WriteRun(False), claim_exists=True), ("WRITE_OUTCOME_UNCERTAIN", "claim_without_writer_start"))


class HookTests(unittest.TestCase):
    def run_hook(self, args: list[str], event) -> subprocess.CompletedProcess:
        stdin = event if isinstance(event, bytes) else json.dumps(event).encode("utf-8")
        return subprocess.run([sys.executable, str(HOOK), *args], input=stdin, capture_output=True, timeout=60, check=False)

    def test_guard_default_denies_mcp(self):
        good = {"tool_name": dl.CONNECTOR_TOOL, "tool_input": page_input(None)}
        cases = [
            (good, 0),
            ({"tool_name": dl.CONNECTOR_TOOL, "tool_input": {"query": dl.PROFILE_QUERY, "variables": {"id": NEW}}}, 0),
            ({"tool_name": "Bash", "tool_input": {"command": "echo synthetic"}}, 0),
            ({"tool_name": dl.CONNECTOR_TOOL, "tool_input": {"query": "query { shop { name } }"}}, 2),
            ({"tool_name": dl.CONNECTOR_TOOL, "tool_input": {"query": "mutation { customerDelete(input: {id: \"x\"}) { deletedCustomerId } }"}}, 2),
            ({"tool_name": dl.CONNECTOR_TOOL, "tool_input": {**page_input(None), "first": 10}}, 2),
            ({"tool_name": "mcp__27c48ff3-54ed-4728-aa90-a7906cba3158__graphql_mutation", "tool_input": page_input(None)}, 2),
            ({"tool_name": "mcp__27c48ff3-54ed-4728-aa90-a7906cba3158__switch-shop", "tool_input": {}}, 2),
            ({"tool_name": "mcp__27c48ff3-54ed-4728-aa90-a7906cba3158__list-customers", "tool_input": {}}, 2),
            ({"tool_name": "mcp__some_future_server__write_everything", "tool_input": {}}, 2),
            (b"not json", 2),
        ]
        for event, code in cases:
            with self.subTest(event=str(event)[:80]):
                self.assertEqual(self.run_hook(["guard"], event).returncode, code)

    def test_capture_writes_exclusive_record_accepted_by_core(self):
        with tempfile.TemporaryDirectory() as temp:
            inbox = Path(temp)
            data = page_data([node(NEW, T0, ["member-mg"])], False, None)
            event = {"session_id": "s", "tool_use_id": "toolu_synthetic_1", "tool_name": dl.CONNECTOR_TOOL, "tool_input": page_input(None), "tool_response": {"data": data}}
            self.assertEqual(self.run_hook(["capture", "--inbox", temp], event).returncode, 0)
            raw = (inbox / "toolu_synthetic_1.json").read_bytes()
            capture = dl.parse_capture(raw, mode="synthetic", store_gid=STORE, shop_domain=DOMAIN)
            self.assertEqual(capture.kind, "baseline")
            self.assertEqual(json.loads(raw)["provenance"], dl.CAPTURE_PROVENANCE)
            changed = {**event, "tool_response": {"data": page_data([], False, None)}}
            self.assertEqual(self.run_hook(["capture", "--inbox", temp], changed).returncode, 1)
            self.assertEqual((inbox / "toolu_synthetic_1.json").read_bytes(), raw)
            self.assertEqual(sorted(p.name for p in inbox.iterdir()), ["toolu_synthetic_1.json"])
            ignored = {**event, "tool_use_id": "toolu_synthetic_2", "tool_input": {"query": "query { shop { name } }"}}
            self.assertEqual(self.run_hook(["capture", "--inbox", temp], ignored).returncode, 0)
            self.assertFalse((inbox / "toolu_synthetic_2.json").exists())
        self.assertEqual(self.run_hook(["capture", "--inbox", str(Path(temp) / "missing")], event).returncode, 1)

    def test_settings_template_allows_only_the_pinned_tool(self):
        settings = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        self.assertNotIn(dl.CONNECTOR_TOOL, deny)
        for tool in ("graphql_mutation", "switch-shop", "list-customers", "update-product", "set-inventory", "create-discount"):
            self.assertIn(f"mcp__27c48ff3-54ed-4728-aa90-a7906cba3158__{tool}", deny)
        self.assertIn("mcp__plugin_small-business_shopify", deny)
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["matcher"], "mcp__.*")
        self.assertIn("directlocal_connector_hook.py\" guard", pre[0]["hooks"][0]["command"])
        self.assertEqual(settings["hooks"]["PostToolUse"][0]["matcher"], dl.CONNECTOR_TOOL)


class CliTests(unittest.TestCase):
    def run_cli(self, root: Path, *args: str, extra_env: dict | None = None) -> tuple[int, dict]:
        env = {**os.environ, "LOCALAPPDATA": str(root / "not-the-state-root"), **(extra_env or {})}
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, str(ROOT / "directlocal" / "xb_directlocal.py"), "--state-root", str(root), *args], capture_output=True, timeout=120, env=env, check=False)
        return proc.returncode, json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])

    def test_cli_initialise_baseline_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(self.run_cli(root, "init-ledger", "--mode", "synthetic", "--store-gid", STORE, "--shop-domain", DOMAIN)[0], 0)
            self.assertEqual(self.run_cli(root, "init-ledger", "--mode", "synthetic", "--store-gid", STORE, "--shop-domain", DOMAIN), (2, {"status": "refused", "code": "ledger_already_initialised"}))
            code, run = self.run_cli(root, "begin-baseline")
            self.assertEqual(code, 0)
            captured = parse_iso(run["cutover_at"]) + timedelta(seconds=1)
            (root / "inbox" / "toolu_cli.json").write_text(json.dumps(record("toolu_cli", page_input(None), page_data([node(NEW, captured, ["member-mg"])], False, None), captured)), encoding="utf-8")
            code, sealed = self.run_cli(root, "ingest-run")
            self.assertEqual((code, sealed["transition"], sealed["discovered"]), (0, 1, 1))
            code, status = self.run_cli(root, "status")
            self.assertEqual((code, status["customers"]), (0, {"DISCOVERED": 1}))
            self.assertEqual(self.run_cli(root / "absent", "status"), (2, {"status": "refused", "code": "state_root_missing"}))
            profile_path = root / "inbox" / "toolu_cli_profile.json"
            now = datetime.now(timezone.utc)
            profile_path.write_text(json.dumps(profile_record("toolu_cli_profile", profile(NEW, created=captured), NEW, now)), encoding="utf-8")
            code, refused = self.run_cli(root, "process", "--capture", str(profile_path), "--enable-autocount-write", extra_env={
                "XB_AC2_SERVER_NAME": "synthetic-server", "XB_AC2_DATABASE_NAME": "synthetic-db", "XB_AC2_USER_ID": "synthetic",
                "XB_AC2_PASSWORD_ENV_VAR": "XB_SYNTHETIC_PW", "XB_SYNTHETIC_PW": "not-a-secret", "XB_AC2_ASSEMBLY_PATH": str(root),
                "SystemRoot": str(root / "no-powershell-here"),
            })
            self.assertEqual((code, refused), (2, {"status": "refused", "code": "real_autocount_refused_synthetic_ledger"}))
            self.assertTrue(profile_path.exists())
            self.assertEqual(list((root / "claims").iterdir()), [])


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
