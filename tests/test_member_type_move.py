"""#268 member type move: the Python tool (config, ledger, rule, engine).

No PowerShell, AutoCount or network: the engine runs against ``FakeBook``, a
model of the child bridge (same pre-checks, one save per call, claim file
before the save). ``tests/test_member_type_move_ps.py`` proves the real child
behaves the same. Fake data only (example.com, fake 9-digit numbers).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_directlocal as dl  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_member_type_move as tm  # noqa: E402

SNAPSHOT_AT = "2026-11-06T20:00:00"
LM = "2026-10-01T09:00:00.000"
# write_deadline_seconds (120) + the DirectLocal writer grace, plus one second.
PAST_DEADLINE = 120 + dl.WRITER_GRACE_SECONDS + 1


def fake_no(n: int) -> str:
    return f"9{n:08d}"


def fake_member(n: int, *, member_type: str = "Default", active: str = "T", expiry: str | None = "2027-06-30T00:00:00", last_modified: str = LM) -> dict:
    return {"member_no": fake_no(n), "type": member_type, "active": active, "expiry": expiry, "last_modified": last_modified,
            "name": f"Fake Member {n}", "email": f"fake{n}@example.com", "register": "2024-07-01"}


# The four L2 fixture kinds (amendment 6).
FIXTURES = [
    fake_member(1, active="T", expiry="2027-06-30T00:00:00"),  # active, future expiry -> MEMBER
    fake_member(2, active="T", expiry="2025-01-31T00:00:00"),  # active, past expiry -> GUEST
    fake_member(3, active="T", expiry=None),  # active, NULL expiry -> GUEST
    fake_member(4, active="F", expiry="2027-06-30T00:00:00"),  # inactive -> GUEST
]
FIXTURE_TARGETS = {fake_no(1): "MEMBER", fake_no(2): "GUEST", fake_no(3): "GUEST", fake_no(4): "GUEST"}


def sql_local(value: str | None) -> str | None:
    """As SQL Server CONVERT(..., 126): no fraction when it is zero."""
    return None if value is None else value.removesuffix(".000")


class FakeBook:
    """A model of ac2_member_type_move_bridge.ps1 on a fake book."""

    def __init__(self, members, claims: Path, *, types=("Default", "MEMBER", "GUEST"), server_now=SNAPSHOT_AT):
        self.members = {m["member_no"]: dict(m) for m in members}
        self.claims = claims
        self.types = set(types)
        self.server_now = server_now
        self.saves = 0
        self.calls: list[str] = []
        self.save_mode: str | None = None  # mutate, raise, uncertain_saved, uncertain_unsaved, type_mismatch
        self.outlets = [{"location": "OUT1", "sync_name": "POS", "last_finish_sync_local": "2026-11-06T19:45:00", "last_alive_local": SNAPSHOT_AT}]
        self.counter = 100
        self.tick = 0
        self.payloads: list[tuple[dict, str, object]] = []
        self.mode_for: dict[str, str] = {}  # one-shot save_mode for one member

    def scan(self, member_type, after):
        self.calls.append("scan")
        assert member_type in tm.SCAN_TYPES
        rows = sorted((m for m in self.members.values() if m["type"] == member_type and m["member_no"] > after), key=lambda m: m["member_no"])[:tm.SCAN_PAGE_SIZE]
        return [{"member_no": m["member_no"], "is_active": m["active"], "expiry_local": m["expiry"], "last_modified_local": sql_local(m["last_modified"])} for m in rows]

    def counts(self):
        self.calls.append("counts")
        groups: dict[tuple, int] = {}
        for m in self.members.values():
            groups[(m["type"], m["active"])] = groups.get((m["type"], m["active"]), 0) + 1
        return {"counts": [{"member_type": t, "is_active": a, "members": n} for (t, a), n in sorted(groups.items())],
                "types_exist": {t: t in self.types for t in tm.SCAN_TYPES}, "server_now_local": self.server_now,
                "member_change_counter": self.counter, "outlets": [dict(o) for o in self.outlets]}

    @staticmethod
    def digest(m, salt):
        kept = {k: v for k, v in m.items() if k not in ("type", "last_modified")}
        return "sha256:" + hashlib.sha256((salt + ":" + json.dumps(kept, sort_keys=True)).encode()).hexdigest()

    def member(self, member_no, salt):
        self.calls.append("member")
        m = self.members.get(member_no)
        if m is None:
            return {"found": False}
        return {"found": True, "member_type": m["type"], "last_modified_local": m["last_modified"], "is_active": m["active"],
                "expiry_local": m["expiry"], "fields_digest": self.digest(m, salt)}

    def save(self, payload, *, move_id, deadline=None, on_started=None):
        self.calls.append("save")
        self.payloads.append((dict(payload), move_id, deadline))
        if on_started is not None:
            on_started(4242, "fake-created")
        mode = self.mode_for.pop(payload["member_no"], self.save_mode)
        if mode == "raise":
            raise RuntimeError("bridge exploded")
        kind, no, src, dst = payload["kind"], payload["member_no"], payload["from_type"], payload["to_type"]
        assert set(payload) == {"kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "snapshot_at_local", "salt"}
        assert (kind == "move" and src == "Default" and dst in tm.TARGET_TYPES) or (kind == "rollback" and src in tm.TARGET_TYPES and dst == "Default")
        if not {src, dst} <= self.types:
            return tm.SaveOutcome("NOT_WRITTEN", "member_type_unknown")
        m = self.members.get(no)
        if m is None:
            return tm.SaveOutcome("SKIPPED", "member_not_found")
        if m["type"] != src:
            return tm.SaveOutcome("SKIPPED", "not_default_now" if kind == "move" else "not_moved_type_now")
        if m["last_modified"] != payload["expected_last_modified_local"]:
            return tm.SaveOutcome("SKIPPED", "changed_since_snapshot" if kind == "move" else "changed_since_move")
        if kind == "move" and tm.target_for(m["active"], m["expiry"], tm.parse_local(payload["snapshot_at_local"]))[0] != dst:
            return tm.SaveOutcome("SKIPPED", "target_changed")
        fence = "fence-" + move_id.removeprefix("move-")
        (self.claims / f"{fence}.claim").write_text(json.dumps({"before_digest": self.digest(m, payload["salt"])}), encoding="ascii")
        if mode == "uncertain_unsaved":
            return tm.SaveOutcome("UNCERTAIN", "writer_timeout")
        self.saves += 1
        self.counter += 1
        self.tick += 1
        m["type"] = dst if mode != "type_mismatch" else "Default"
        m["last_modified"] = f"2026-11-06T20:{self.tick // 60:02d}:{self.tick % 60:02d}.007"
        if mode == "mutate":
            m["name"] = "changed"
            return tm.SaveOutcome("HELD", "readback_fields_changed", m["last_modified"], ("Name",))
        if mode == "type_mismatch":
            return tm.SaveOutcome("HELD", "readback_type_mismatch", m["last_modified"])
        if mode == "uncertain_saved":
            return tm.SaveOutcome("UNCERTAIN", "writer_timeout")
        return tm.SaveOutcome("DONE", None, m["last_modified"])


def config(**overrides) -> tm.MoveConfig:
    value = {"schema": tm.CONFIG_SCHEMA, "state_root": "C:\\XB\\xb268-type-move-test", "autocount_book": "AED_TESTINGAPI",
             "type_move_enabled": True, "rollback_enabled": True, "max_moves_per_run": 10, "max_move_runs_per_day": 10}
    value.update(overrides)
    return tm.parse_config(value)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-type-move-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.root = self.temp / "state"
        self.root.mkdir()
        (self.root / "claims").mkdir()
        self.book = FakeBook(FIXTURES, self.root / "claims")
        self.ledger = None
        self.now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        self.probe_result = "exited"
        self.probed: list[tuple] = []

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def probe(self, pid, created) -> str:
        self.probed.append((pid, created))
        return self.probe_result

    def open(self, cfg: tm.MoveConfig | None = None, clock=None) -> tm.TypeMoveEngine:
        if self.ledger is None:
            self.ledger = tm.MoveLedger(self.root, clock=clock or (lambda: self.now))
            self.addCleanup(self.ledger.close)
            self.ledger.init_meta("AED_TESTINGAPI")
        return tm.TypeMoveEngine(cfg or config(), self.book, ledger=self.ledger, writer_probe=self.probe)

    def code(self, call) -> str:
        with self.assertRaises(dl.DirectLocalError) as caught:
            call()
        return caught.exception.code


class ConfigTests(Base):
    def test_example_configs_parse_with_every_switch_off(self):
        for name in ("member_type_move.example.json", "member_type_move.live.example.json"):
            with self.subTest(name=name):
                cfg = tm.load_config(ROOT / "config" / name)
                self.assertFalse(cfg.type_move_enabled or cfg.rollback_enabled)
                self.assertEqual(cfg.max_moves_per_run, 10)
                self.assertEqual(cfg.max_move_runs_per_day, 2)
        self.assertEqual(tm.load_config(ROOT / "config" / "member_type_move.live.example.json").autocount_book, "AED_XBOUNDARIES")

    def test_the_member_sync_config_is_refused(self):
        for name in ("member_sync.example.json", "member_sync.live.example.json"):
            with self.subTest(name=name):
                self.assertEqual(self.code(lambda: tm.load_config(ROOT / "config" / name)), "type_move_config_is_member_sync")
        # And the other way: the sync refuses this tool's config.
        with self.assertRaises(dl.DirectLocalError):
            sync.load_config(ROOT / "config" / "member_type_move.example.json")

    def test_types_and_unknown_keys_cannot_be_configured(self):
        base = {"schema": tm.CONFIG_SCHEMA, "state_root": "C:\\x", "autocount_book": "AED_TESTINGAPI"}
        for extra in ({"from_type": "Default"}, {"to_type": "MEMBER"}, {"target_types": ["VIP"]}, {"max_writes_per_run": 1}):
            with self.subTest(extra=extra):
                self.assertEqual(self.code(lambda: tm.parse_config({**base, **extra})), "type_move_config_keys_invalid")

    def test_bounds_switches_and_live_rules(self):
        base = {"schema": tm.CONFIG_SCHEMA, "state_root": "C:\\x", "autocount_book": "AED_TESTINGAPI"}
        cases = {
            "type_move_config_max_moves_per_run_invalid": [{"max_moves_per_run": 501}, {"max_moves_per_run": 0}, {"max_moves_per_run": True}],
            "type_move_config_max_move_runs_per_day_invalid": [{"max_move_runs_per_day": 11}],
            "type_move_config_type_move_enabled_invalid": [{"type_move_enabled": "yes"}],
            "type_move_config_ac2_credential_target_live_only": [{"ac2_credential_target": "xb-ac2-membersync"}],
            "autocount_book_not_authorised": [{"autocount_book": "aed_testingapi"}, {"autocount_book": "AED_OTHER"}],
            "type_move_config_schema_invalid": [{"schema": "xb.directlocal.member_type_move.config.v2"}],
        }
        for code, extras in cases.items():
            for extra in extras:
                with self.subTest(extra=extra):
                    self.assertEqual(self.code(lambda: tm.parse_config({**base, **extra})), code)
        live = {**base, "autocount_book": "AED_XBOUNDARIES"}
        self.assertEqual(self.code(lambda: tm.parse_config(live)), "type_move_config_ac2_credential_target_live_required")
        self.assertEqual(self.code(lambda: tm.parse_config({**live, "ac2_credential_target": sync.LIVE_CREDENTIAL_TARGET})), "type_move_config_ac2_login_mode_live_required")
        ok = tm.parse_config({**live, "ac2_credential_target": sync.LIVE_CREDENTIAL_TARGET, "ac2_login_mode": "owner_shared"})
        self.assertFalse(ok.type_move_enabled or ok.rollback_enabled)
        self.assertEqual(self.code(lambda: tm.parse_config({**live, "ac2_credential_target": sync.LIVE_CREDENTIAL_TARGET, "ac2_login_mode": "dedicated"})),
                         "type_move_config_ac2_refused_user_ids_live_required")


@contextlib.contextmanager
def private_root_allowed(elsewhere: Path):
    """state_root() refuses %LOCALAPPDATA% (where temp dirs live on Windows)
    and needs a protected ACL; both are proven in the #155 tests."""
    with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(elsewhere)}), mock.patch.object(dl, "verify_private_state", lambda root: None):
        yield


class StateRootAndLedgerTests(Base):
    def test_a_root_holding_the_sync_or_core_ledger_is_refused(self):
        with private_root_allowed(self.temp / "elsewhere"):
            cfg = config(state_root=str(self.root))
            self.assertEqual(tm.state_root(cfg), self.root.resolve())
            for name in (sync.LEDGER_FILE, "ledger.sqlite3"):
                with self.subTest(name=name):
                    (self.root / name).write_bytes(b"")
                    self.assertEqual(self.code(lambda: tm.state_root(cfg)), "type_move_state_root_shared")
                    (self.root / name).unlink()

    def test_the_sync_ledger_is_refused_under_this_tools_name(self):
        other = self.temp / "sync-root"
        other.mkdir()
        sync.SyncLedger(other, check_custody=False).close()
        (other / sync.LEDGER_FILE).rename(other / tm.LEDGER_FILE)
        self.assertEqual(self.code(lambda: tm.MoveLedger(other)), "type_move_ledger_not_recognised")

    def test_ledger_checks_pin_the_types(self):
        self.open().snapshot()
        conn = self.ledger.conn
        bad = [
            ("INSERT INTO snapshot_rows(member_no, is_active, last_modified_local, target) VALUES ('X1', 'T', ?, 'VIP')", (LM,)),
            ("INSERT INTO snapshot_rows(member_no, is_active, last_modified_local, target) VALUES ('X2', 'T', ?, 'Default')", (LM,)),
            ("INSERT INTO moves(move_id, run_id, kind, member_no, from_type, to_type, expected_last_modified_local, salt, state, created_at, updated_at) "
             "VALUES ('m1', 'r', 'move', 'X', 'Default', 'VIP', ?, 's', 'INTENT', 't', 't')", (LM,)),
            ("INSERT INTO moves(move_id, run_id, kind, member_no, from_type, to_type, expected_last_modified_local, salt, state, created_at, updated_at) "
             "VALUES ('m2', 'r', 'move', 'X', 'GUEST', 'MEMBER', ?, 's', 'INTENT', 't', 't')", (LM,)),
            ("INSERT INTO moves(move_id, run_id, kind, member_no, from_type, to_type, expected_last_modified_local, salt, state, created_at, updated_at) "
             "VALUES ('m3', 'r', 'rollback', 'X', 'MEMBER', 'GUEST', ?, 's', 'INTENT', 't', 't')", (LM,)),
            ("UPDATE snapshot_rows SET target = 'MEMBER'", ()),
            ("DELETE FROM snapshot_rows", ()),
            ("UPDATE meta SET from_type = 'GUEST'", ()),
        ]
        for statement, params in bad:
            with self.subTest(statement=statement[:60]), self.assertRaises(sqlite3.DatabaseError):
                conn.execute(statement, params)


class RuleTests(unittest.TestCase):
    def test_each_fixture_kind_gets_its_target(self):
        at = tm.parse_local(SNAPSHOT_AT)
        for m in FIXTURES:
            with self.subTest(member=m["member_no"]):
                self.assertEqual(tm.target_for(m["active"], m["expiry"], at), (FIXTURE_TARGETS[m["member_no"]], None))

    def test_boundary_and_holds(self):
        at = tm.parse_local(SNAPSHOT_AT)
        self.assertEqual(tm.target_for("T", "2026-11-06T20:00:00", at), ("GUEST", None))  # at the snapshot time
        self.assertEqual(tm.target_for("T", "2026-11-06T20:00:00.003", at), ("MEMBER", None))
        self.assertEqual(tm.target_for("T", "2026-11-07T00:00:00", at), ("MEMBER", None))
        self.assertEqual(tm.target_for("F", "garbage", at), ("GUEST", None))
        self.assertEqual(tm.target_for("T", "garbage", at), (None, "expiry_unparseable"))
        for flag in ("t", "Y", "", None, True, 1):
            self.assertEqual(tm.target_for(flag, "2027-01-01T00:00:00", at), (None, "active_flag_unexpected"))

    def test_no_third_target_can_be_produced(self):
        at = tm.parse_local(SNAPSHOT_AT)
        flags = ("T", "F", "t", None, "X")
        expiries = (None, "2020-01-01T00:00:00", SNAPSHOT_AT, "2030-01-01T00:00:00", "bad", "2026-11-06T20:00:00.999")
        targets = {tm.target_for(f, e, at)[0] for f in flags for e in expiries}
        self.assertEqual(targets, {"MEMBER", "GUEST", None})

    def test_classify_row_holds_rows_it_cannot_move(self):
        at = tm.parse_local(SNAPSHOT_AT)
        good = {"member_no": fake_no(1), "is_active": "T", "expiry_local": None, "last_modified_local": "2026-10-01T09:00:00"}
        self.assertEqual(tm.classify_row(good, at)["last_modified_local"], LM)
        self.assertEqual(tm.classify_row({**good, "last_modified_local": "2026-10-01T09:00:00.12"}, at)["last_modified_local"], "2026-10-01T09:00:00.120")
        for change, hold in (({"member_no": "BAD NO"}, "member_no_unsupported"), ({"last_modified_local": None}, "last_modified_missing"),
                             ({"last_modified_local": "yesterday"}, "last_modified_unparseable"), ({"is_active": "Y"}, "active_flag_unexpected")):
            with self.subTest(hold=hold):
                row = tm.classify_row({**good, **change}, at)
                self.assertEqual((row["target"], row["hold_code"]), (None, hold))


class EngineTests(Base):
    def test_plan_counts_by_target_and_writes_nothing(self):
        self.book.members[fake_no(9)] = fake_member(9, member_type="GUEST")
        engine = tm.TypeMoveEngine(config(), self.book, root=self.root)
        result = engine.plan()
        self.assertEqual(result["default_members"], 4)
        self.assertEqual(result["by_target"], {"MEMBER": {"active": 1, "inactive": 0}, "GUEST": {"active": 2, "inactive": 1}})
        self.assertEqual((result["writes"], self.book.saves), (0, 0))
        self.assertNotIn("masked", result)
        masked = engine.plan(show_masked=True)["masked"]
        self.assertEqual(masked["MEMBER"], [tm.mask(fake_no(1))])
        self.assertNotIn(fake_no(1), json.dumps(masked))
        self.assertFalse((self.root / tm.LEDGER_FILE).exists())

    def test_snapshot_needs_all_three_types_and_is_taken_once(self):
        self.book.types = {"Default", "MEMBER"}
        self.assertEqual(self.code(lambda: self.open().snapshot()), "member_type_unknown")
        self.assertIsNone(self.ledger.snapshot())
        self.book.types = {"Default", "MEMBER", "GUEST"}
        result = self.open().snapshot()
        self.assertEqual((result["default_members"], result["snapshot_at_local"]), (4, SNAPSHOT_AT))
        self.assertEqual(self.code(lambda: self.open().snapshot()), "type_move_snapshot_exists")
        rows = {r["member_no"]: r["target"] for r in self.ledger.conn.execute("SELECT * FROM snapshot_rows")}
        self.assertEqual(rows, FIXTURE_TARGETS)

    def test_move_happy_path_cap_and_idempotent_rerun(self):
        engine = self.open()
        engine.snapshot()
        first = engine.move(max_moves=3)
        self.assertEqual((first["saves"], first["stopped"], first["alerts"]), (3, None, []))
        self.assertEqual(first["moved"], {"MEMBER": {"active": 1, "inactive": 0}, "GUEST": {"active": 2, "inactive": 0}})
        self.assertEqual((first["batch_end_local"], first["member_change_counter_before"], first["member_change_counter_after"]), (SNAPSHOT_AT, 100, 103))
        second = engine.move()
        self.assertEqual((second["saves"], second["moved"]["GUEST"]["inactive"]), (1, 1))
        third = engine.move()
        self.assertEqual((third["saves"], self.book.saves), (0, 4))
        self.assertEqual({no: m["type"] for no, m in self.book.members.items()}, FIXTURE_TARGETS)
        self.assertEqual(self.code(lambda: engine.move(max_moves=11)), "type_move_max_invalid")

    def test_switch_off_refuses_before_any_bridge_call(self):
        engine = self.open(config(type_move_enabled=False, rollback_enabled=False))
        self.assertEqual(self.code(lambda: engine.move()), "type_move_disabled")
        self.assertEqual(self.code(lambda: engine.rollback(max_moves=1)), "type_move_rollback_disabled")
        self.assertEqual(self.book.calls, [])

    def test_changes_after_the_snapshot_are_skipped_and_not_counted_against_the_cap(self):
        engine = self.open()
        engine.snapshot()
        self.book.members[fake_no(1)]["expiry"] = "2026-11-01T00:00:00"  # expiry now past: target_changed
        self.book.members[fake_no(2)]["type"] = "GUEST"  # staff moved it: not_default_now
        self.book.members[fake_no(3)]["last_modified"] = "2026-11-06T19:00:00.000"  # edited: changed_since_snapshot
        self.book.members[fake_no(4)]["active"] = "T"  # reactivated: now MEMBER, snapshot said GUEST
        result = engine.move(max_moves=1)
        self.assertEqual((result["saves"], result["skipped"]), (0, {"target_changed": 2, "not_default_now": 1, "changed_since_snapshot": 1}))
        self.assertEqual(self.book.saves, 0)
        self.assertEqual(engine.move()["saves"], 0)  # skipped members are not retried

    def test_read_back_mismatch_stops_the_run(self):
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "mutate"
        result = engine.move()
        self.assertEqual((result["saves"], result["stopped"], result["changed_fields"]), (0, "held:readback_fields_changed", ["Name"]))
        self.assertEqual(self.book.saves, 1)  # nothing after the first member

    def test_type_mismatch_read_back_is_held_and_stops(self):
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "type_mismatch"
        self.assertEqual(engine.move()["stopped"], "held:readback_type_mismatch")
        self.assertEqual(self.book.saves, 1)

    def test_held_latches_move_but_not_rollback(self):
        engine = self.open()
        engine.snapshot()
        self.book.mode_for[fake_no(3)] = "mutate"
        result = engine.move()
        self.assertEqual((result["saves"], result["stopped"]), (2, "held:readback_fields_changed"))
        self.book.save_mode = None
        self.assertEqual(self.code(lambda: engine.move()), "type_move_held_rows_present")
        self.assertEqual(self.book.saves, 3)  # the refusal touched nothing
        rolled = engine.rollback(max_moves=10)
        self.assertEqual((rolled["saves"], rolled["rolled_back"]), (2, {"MEMBER": 1, "GUEST": 1}))
        self.assertEqual(self.code(lambda: engine.move()), "type_move_held_rows_present")

    def test_save_exception_stops_and_blocks_until_resolved(self):
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "raise"
        result = engine.move()
        self.assertEqual(result["stopped"], "uncertain:save_call_failed")
        self.assertEqual(self.code(lambda: engine.move()), "type_move_uncertain_unresolved")
        self.assertEqual(self.code(lambda: engine.rollback(max_moves=1)), "type_move_uncertain_unresolved")
        self.advance(PAST_DEADLINE)
        resolved = engine.resolve()
        self.assertEqual(resolved["resolved"], {"NOT_WRITTEN:resolved_no_claim": 1})
        self.book.save_mode = None
        self.assertEqual(engine.move()["saves"], 4)  # the NOT_WRITTEN member is eligible again

    def test_kill_after_save_is_resolved_without_a_second_save(self):
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "uncertain_saved"
        result = engine.move()
        self.assertEqual((result["stopped"], self.book.saves), ("uncertain:writer_timeout", 1))
        self.book.save_mode = None
        self.advance(PAST_DEADLINE)
        resolved = engine.resolve()
        self.assertEqual((resolved["resolved"], resolved["alerts"]), ({"DONE:resolved_saved": 1}, []))
        self.assertEqual(self.book.saves, 1)
        done = self.ledger.conn.execute("SELECT * FROM moves WHERE state = 'DONE'").fetchone()
        self.assertEqual(done["post_last_modified_local"], self.book.members[fake_no(1)]["last_modified"])
        self.assertEqual(engine.move()["saves"], 3)
        self.assertEqual(self.book.saves, 4)

    def test_uncertain_but_not_saved_resolves_to_not_written_and_is_moved_later(self):
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "uncertain_unsaved"
        engine.move()
        self.book.save_mode = None
        self.advance(PAST_DEADLINE)
        self.assertEqual(engine.resolve()["resolved"], {"NOT_WRITTEN:resolved_not_written": 1})
        self.assertEqual(engine.move()["saves"], 4)

    def test_uncertain_that_changed_otherwise_is_held(self):
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "uncertain_saved"
        engine.move()
        self.book.members[fake_no(1)]["name"] = "edited by staff"
        self.advance(PAST_DEADLINE)
        resolved = engine.resolve()
        self.assertEqual((resolved["resolved"], resolved["alerts"]), ({"HELD:resolved_unclear": 1}, ["resolved_unclear"]))
        self.book.save_mode = None
        self.assertEqual(self.code(lambda: engine.move()), "type_move_held_rows_present")  # the owner decides first

    def open_rows(self) -> list[tuple]:
        return [tuple(r) for r in self.ledger.conn.execute("SELECT move_id, state, code, post_last_modified_local, updated_at FROM moves ORDER BY rowid")]

    def test_resolve_holds_while_a_move_child_may_be_live(self):
        # G2 HARDEN-1: the parent stops, the orphan save child may still run.
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "uncertain_unsaved"
        engine.move()
        row = self.ledger.conn.execute("SELECT * FROM moves").fetchone()
        payload, move_id, deadline = self.book.payloads[-1]
        self.assertEqual((move_id, dl.ts(deadline)), (row["move_id"], row["deadline_at"]))  # the child got the recorded deadline
        self.assertEqual(row["deadline_at"], dl.ts(self.now + timedelta(seconds=120)))
        self.assertEqual((row["child_pid"], row["child_created"]), (4242, "fake-created"))
        before = self.open_rows()
        for wait in (0, 120, 120 + dl.WRITER_GRACE_SECONDS - 1):
            with self.subTest(wait=wait):
                self.now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=wait)
                self.assertEqual(self.code(lambda: engine.resolve()), "writer_may_be_live")
                self.assertEqual(self.open_rows(), before)  # nothing settled
                self.assertEqual(self.code(lambda: engine.move()), "type_move_uncertain_unresolved")
                self.assertEqual(self.code(lambda: engine.rollback(max_moves=1)), "type_move_uncertain_unresolved")
        # The orphan saves after all: the read-back finds it, no second save.
        self.book.save_mode = None
        claim_after = self.book.saves
        member = self.book.members[fake_no(1)]
        member["type"], member["last_modified"] = "MEMBER", "2026-11-06T20:30:00.007"
        self.now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=120 + dl.WRITER_GRACE_SECONDS)
        self.assertEqual(engine.resolve()["resolved"], {"DONE:resolved_saved": 1})
        self.assertEqual(self.book.saves, claim_after)

    def test_resolve_holds_after_the_deadline_while_the_recorded_child_runs(self):
        engine = self.open()
        engine.snapshot()
        self.book.save_mode = "uncertain_unsaved"
        engine.move()
        self.advance(PAST_DEADLINE)
        self.probe_result = "running"
        self.assertEqual(self.code(lambda: engine.resolve()), "writer_may_be_live")
        self.assertEqual(self.probed, [(4242, "fake-created")])
        self.probe_result = "exited"
        self.assertEqual(engine.resolve()["resolved"], {"NOT_WRITTEN:resolved_not_written": 1})

    def test_resolve_holds_while_a_rollback_child_or_a_bare_intent_may_be_live(self):
        engine = self.open()
        engine.snapshot()
        engine.move(max_moves=1)
        self.book.save_mode = "uncertain_saved"
        engine.rollback(max_moves=1)
        self.assertEqual(self.code(lambda: engine.resolve()), "writer_may_be_live")
        self.advance(PAST_DEADLINE)
        self.assertEqual(engine.resolve()["resolved"], {"DONE:resolved_saved": 1})
        self.assertEqual(self.book.members[fake_no(1)]["type"], "Default")
        # An INTENT left by a parent that stopped before its child reported.
        run_id = self.ledger.start_run("move")
        self.ledger.add_intent(run_id=run_id, kind="move", member_no=fake_no(2), from_type="Default", to_type="GUEST",
                               expected=LM, deadline=self.now + timedelta(seconds=120))
        self.assertEqual(self.code(lambda: engine.resolve()), "writer_may_be_live")
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM moves ORDER BY rowid DESC").fetchone()[0], "INTENT")
        self.advance(PAST_DEADLINE)
        self.assertEqual(engine.resolve()["resolved"], {"NOT_WRITTEN:resolved_no_claim": 1})

    def test_rollback_takes_only_done_moves_and_sends_the_recorded_intent(self):
        # G2 LATER h (M6, M7): SKIPPED, HELD and NOT_WRITTEN moves are never rolled back.
        engine = self.open()
        engine.snapshot()
        self.book.members[fake_no(1)]["type"] = "GUEST"  # staff: SKIPPED not_default_now
        self.book.mode_for[fake_no(3)] = "mutate"  # HELD
        engine.move()
        self.book.save_mode = None
        run_id = self.ledger.start_run("move")
        intent = self.ledger.add_intent(run_id=run_id, kind="move", member_no=fake_no(4), from_type="Default", to_type="GUEST",
                                        expected=LM, deadline=self.now)
        self.ledger.set_outcome(intent["move_id"], "NOT_WRITTEN", "writer_failed_before_claim")
        states = {r["member_no"]: r["state"] for r in self.ledger.conn.execute("SELECT member_no, state FROM moves WHERE kind = 'move'")}
        self.assertEqual(states, {fake_no(1): "SKIPPED", fake_no(2): "DONE", fake_no(3): "HELD", fake_no(4): "NOT_WRITTEN"})
        result = engine.rollback(max_moves=10, show_masked=True)
        self.assertEqual((result["saves"], result["masked"]["rolled_back"], result["skipped"]), (1, [tm.mask(fake_no(2))], {}))
        self.assertEqual({n: m["type"] for n, m in self.book.members.items()},
                         {fake_no(1): "GUEST", fake_no(2): "Default", fake_no(3): "GUEST", fake_no(4): "Default"})
        # Every payload the child got equals its INTENT row.
        rows = {r["move_id"]: r for r in self.ledger.conn.execute("SELECT * FROM moves")}
        for payload, move_id, deadline in self.book.payloads:
            row = rows[move_id]
            with self.subTest(member=payload["member_no"], kind=payload["kind"]):
                self.assertEqual({k: payload[k] for k in ("kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "salt")},
                                 {k: row[k] for k in ("kind", "member_no", "from_type", "to_type", "expected_last_modified_local", "salt")})
                self.assertEqual(dl.ts(deadline), row["deadline_at"])
        rollback_row = next(r for r in rows.values() if r["kind"] == "rollback")
        move_row = next(r for r in rows.values() if r["kind"] == "move" and r["member_no"] == fake_no(2))
        self.assertEqual(rollback_row["expected_last_modified_local"], move_row["post_last_modified_local"])

    def test_stop_file_and_runs_per_day(self):
        engine = self.open(config(max_move_runs_per_day=2))
        engine.snapshot()
        (self.root / tm.STOP_FILE).write_text("", encoding="ascii")
        result = engine.move()
        self.assertEqual((result["saves"], result["stopped"]), (0, "stop_file_present"))
        (self.root / tm.STOP_FILE).unlink()
        engine.move(max_moves=1)
        self.assertEqual(self.code(lambda: engine.move()), "type_move_runs_per_day_reached")

    def test_rollback_restores_default_for_both_targets_and_only_tool_moves(self):
        engine = self.open()
        engine.snapshot()
        engine.move()
        self.book.members[fake_no(5)] = fake_member(5)  # never in the snapshot
        self.book.members[fake_no(2)]["last_modified"] = "2026-11-07T10:00:00.000"  # staff edited after the move
        result = engine.rollback(max_moves=3)
        self.assertEqual(result["saves"], 3)
        self.assertEqual(result["rolled_back"], {"MEMBER": 1, "GUEST": 2})
        self.assertEqual(result["skipped"], {"changed_since_move": 1})
        types = {no: m["type"] for no, m in self.book.members.items()}
        self.assertEqual(types, {fake_no(1): "Default", fake_no(2): "GUEST", fake_no(3): "Default", fake_no(4): "Default", fake_no(5): "Default"})
        self.assertEqual(engine.rollback(max_moves=3)["saves"], 0)  # nothing left to roll back
        self.assertEqual(engine.move()["saves"], 0)  # rolled-back members are not moved again

    def test_rollback_takes_the_latest_moves_first_across_second_boundaries(self):
        from datetime import datetime, timedelta, timezone
        ticks = iter(datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc) + timedelta(milliseconds=700 * i) for i in range(10_000))
        engine = self.open(clock=lambda: next(ticks))
        engine.snapshot()
        engine.move()
        result = engine.rollback(max_moves=1, show_masked=True)
        self.assertEqual(result["masked"]["rolled_back"], [tm.mask(fake_no(4))])
        self.assertEqual(engine.rollback(max_moves=2, show_masked=True)["masked"]["rolled_back"], [tm.mask(fake_no(3)), tm.mask(fake_no(2))])

    def test_reconcile_lists_what_is_left(self):
        engine = self.open()
        engine.snapshot()
        engine.move(max_moves=2)
        self.book.members[fake_no(6)] = fake_member(6)  # keyed as Default after the snapshot
        self.book.members[fake_no(1)]["type"] = "Default"  # put back by staff
        result = engine.reconcile(show_masked=True)
        self.assertEqual(result["counts"]["pending"], 2)
        self.assertEqual(result["counts"]["new_default"], 1)
        self.assertEqual(result["counts"]["not_on_expected_type"], 1)
        self.assertEqual(result["moved_net"], {"MEMBER": {"active": 1, "inactive": 0}, "GUEST": {"active": 1, "inactive": 0}})
        self.assertEqual(result["outlets_not_synced"], ["OUT1"])
        self.assertFalse(result["complete"])
        self.assertIn("outlets_not_synced_since_last_batch", result["alerts"])
        self.assertEqual(result["masked"]["new_default"], [tm.mask(fake_no(6))])
        self.assertNotIn(fake_no(6), json.dumps(result))
        # Finished: everything moved, outlets synced after the last batch.
        self.book.members[fake_no(1)]["type"] = "MEMBER"
        engine.move()
        self.book.outlets[0]["last_finish_sync_local"] = "2026-11-06T20:15:00"
        del self.book.members[fake_no(6)]
        done = engine.reconcile()
        self.assertEqual((done["complete"], done["alerts"], done["counts"]["pending"]), (True, [], 0))
        self.assertEqual(done["moved_net"], {"MEMBER": {"active": 1, "inactive": 0}, "GUEST": {"active": 2, "inactive": 1}})


class ClassifySaveTests(unittest.TestCase):
    SAVED = {"status": "saved", "save_invocation_count": 1, "readback_found": True, "readback_type_match": True, "fields_unchanged": True,
             "changed_fields": [], "post_last_modified_local": "2026-11-06T20:00:01.007", "outbound_queue_changed": False}

    def run_of(self, value, code=0) -> dl.WriteRun:
        return dl.WriteRun(started=True, exit_code=code, stdout=json.dumps(value) + "\n")

    def test_matrix(self):
        c = tm.classify_save
        self.assertEqual(c(self.run_of(self.SAVED), claim_exists=True, kind="move"), tm.SaveOutcome("DONE", None, "2026-11-06T20:00:01.007", ()))
        self.assertEqual(c(self.run_of(self.SAVED), claim_exists=False, kind="move").state, "UNCERTAIN")
        self.assertEqual(c(self.run_of({**self.SAVED, "save_invocation_count": 2}), claim_exists=True, kind="move").code, "writer_outcome_invalid")
        self.assertEqual(c(self.run_of({**self.SAVED, "readback_found": False}), claim_exists=True, kind="move").code, "readback_absent")
        self.assertEqual(c(self.run_of({**self.SAVED, "outbound_queue_changed": True}), claim_exists=True, kind="move").state, "HELD")
        self.assertEqual(c(self.run_of({**self.SAVED, "post_last_modified_local": None}), claim_exists=True, kind="move").code, "readback_last_modified_missing")
        self.assertEqual(c(self.run_of({**self.SAVED, "changed_fields": ["bad name!"]}), claim_exists=True, kind="move").code, "writer_outcome_invalid")
        self.assertEqual(c(self.run_of({"status": "skipped", "code": "target_changed"}), claim_exists=False, kind="move"), tm.SaveOutcome("SKIPPED", "target_changed"))
        self.assertEqual(c(self.run_of({"status": "skipped", "code": "target_changed"}), claim_exists=True, kind="move").state, "UNCERTAIN")
        self.assertEqual(c(self.run_of({"status": "skipped", "code": "target_changed"}), claim_exists=False, kind="rollback").state, "UNCERTAIN")
        self.assertEqual(c(self.run_of({"error": "member_type_unknown"}, 1), claim_exists=False, kind="move"), tm.SaveOutcome("NOT_WRITTEN", "member_type_unknown"))
        self.assertEqual(c(self.run_of({"error": "bridge_failed"}, 1), claim_exists=False, kind="move"), tm.SaveOutcome("NOT_WRITTEN", "writer_failed_before_claim"))
        self.assertEqual(c(self.run_of({"error": "bridge_failed"}, 1), claim_exists=True, kind="move").state, "UNCERTAIN")
        self.assertEqual(c(dl.WriteRun(started=True, timed_out=True, exited=True), claim_exists=False, kind="move").state, "NOT_WRITTEN")
        self.assertEqual(c(dl.WriteRun(started=True, timed_out=True, exited=False), claim_exists=False, kind="move").state, "UNCERTAIN")
        self.assertEqual(c(dl.WriteRun(started=False), claim_exists=False, kind="move").state, "NOT_WRITTEN")
        self.assertEqual(c(self.run_of({"error": "fence_already_claimed"}, 3), claim_exists=True, kind="move").code, "fence_already_claimed")


class CliTests(Base):
    def run_main(self, *args, cfg: dict | None = None, factory=None) -> tuple[int, dict, str]:
        path = self.temp / "config.json"
        value = {"schema": tm.CONFIG_SCHEMA, "state_root": str(self.root), "autocount_book": "AED_TESTINGAPI", "type_move_enabled": True,
                 "rollback_enabled": True, "max_move_runs_per_day": 10}
        value.update(cfg or {})
        path.write_text(json.dumps(value), encoding="utf-8")
        out = io.StringIO()
        with private_root_allowed(self.temp / "elsewhere"), contextlib.redirect_stdout(out):
            code = tm.main([args[0], "--config", str(path), *args[1:]], bridge_factory=factory or (lambda c, r: self.book))
        text = out.getvalue()
        return code, json.loads(text.strip().splitlines()[-1]), text

    def test_full_flow_prints_no_full_member_number(self):
        texts = []
        for args in (("init",), ("plan", "--show-masked"), ("snapshot", "--show-masked"), ("move", "--max", "2", "--show-masked"), ("move", "--show-masked"),
                     ("rollback", "--max", "1", "--show-masked"), ("reconcile", "--show-masked"), ("status",)):
            code, out, text = self.run_main(*args)
            texts.append(text)
            with self.subTest(args=args):
                self.assertIn(code, (0, 3))
                self.assertNotEqual(out.get("status"), "refused", out)
        joined = "".join(texts)
        for m in FIXTURES:
            self.assertNotIn(m["member_no"], joined)
            self.assertNotIn(m["email"], joined)
        self.assertIn(tm.mask(fake_no(1)), joined)

    def test_switch_off_and_missing_max_refuse_before_the_bridge(self):
        def factory(cfg, root):
            raise AssertionError("bridge built")
        self.assertEqual(self.run_main("move", cfg={"type_move_enabled": False}, factory=factory)[:2], (2, {"status": "refused", "code": "type_move_disabled"}))
        self.assertEqual(self.run_main("rollback", "--max", "1", cfg={"rollback_enabled": False}, factory=factory)[:2],
                         (2, {"status": "refused", "code": "type_move_rollback_disabled"}))
        self.assertEqual(self.run_main("rollback", factory=factory)[:2], (2, {"status": "refused", "code": "type_move_max_required"}))
        self.assertFalse((self.root / tm.LEDGER_FILE).exists())

    def test_a_ledger_for_the_other_book_is_refused(self):
        self.run_main("init")
        code, out, _ = self.run_main("status", cfg={"autocount_book": "AED_XBOUNDARIES", "ac2_credential_target": sync.LIVE_CREDENTIAL_TARGET, "ac2_login_mode": "owner_shared"})
        self.assertEqual((code, out), (2, {"status": "refused", "code": "type_move_ledger_binding_mismatch"}))


class StaticTests(unittest.TestCase):
    def test_python_builds_no_autocount_sql(self):
        source = (ROOT / "directlocal" / "xb_member_type_move.py").read_text(encoding="utf-8")
        self.assertNotIn("ExecuteScalar", source)
        self.assertNotRegex(source, r"(?i)\bFROM\s+(dbo\.)?(Member|MemberType|ChangeLog|SyncRegistration)\b")
        self.assertNotRegex(source, r"(?i)\bUPDATE\s+(dbo\.)?Member\b")

    def test_sources_stay_ascii(self):
        for path in (ROOT / "directlocal" / "xb_member_type_move.py", Path(__file__)):
            path.read_bytes().decode("ascii")


if __name__ == "__main__":
    unittest.main()
