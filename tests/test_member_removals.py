"""#268 slice E: the member removals tool (config, rules, ledger, engine, CLI).

No PowerShell, AutoCount, Shopify or network: the engine runs against
``FakeBook``, a model of ac2_member_removals_bridge.ps1 (same pre-checks, one
save per call, claim file before the save, only MemberType assigned), and a
real member core v2 ledger built in a temp folder by the member core's own
ledger class. ``tests/test_member_removals_ps.py`` proves the real child
behaves the same. Fake data only: fake 9-digit member numbers built in code,
synthetic store.

Closure rows of the G0 design (section 5.1) each have a test named
``test_<row>_...``; the G2 mutation targets (section 5.2) are named in the
docstring of the test that kills them.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_directlocal as dl  # noqa: E402
import xb_member_benefits as mb  # noqa: E402
import xb_member_removals as mr  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_member_tags as tags  # noqa: E402
import xb_member_type_move as tm  # noqa: E402

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc)  # 12:00 Singapore, 24 Nov
RUN_DAY = date(2026, 11, 24)
LM = "2026-10-01T09:00:00.000"
YESTERDAY = "2026-11-23T00:00:00.000"
TODAY = "2026-11-24T00:00:00.000"
FUTURE = "2028-06-30T00:00:00.000"
BASIS = "2028-11-18T00:00:00"  # an upgrade's new_expiry_local (member core form)
UPGRADE_POST_LM = "2026-11-18T11:00:01.007"
PAST_DEADLINE = 120 + dl.WRITER_GRACE_SECONDS + 1


def fake_no(n: int) -> str:
    """A fake 9-digit member number (never a real one)."""
    return f"9{n:08d}"


def order_gid(n: int) -> str:
    return f"gid://shopify/Order/{5 * 10 ** 15 + n}"


def customer_gid(n: int) -> str:
    return f"gid://shopify/Customer/{10 ** 15 + n}"


def scan_form(value: str | None) -> str | None:
    """CONVERT(varchar(23), x, 126) drops a zero fraction."""
    if value is None:
        return None
    return value[:-4] if value.endswith(".000") else value


# --------------------------------------------------------------------------
# FakeBook: a model of the removals child
# --------------------------------------------------------------------------

class FakeBook:
    EXCLUDED = ("MemberType", "LastModified", "LastModifiedUserID")

    def __init__(self, claims: Path, clock):
        self.claims = Path(claims)
        self.clock = clock
        self.members: dict[str, dict] = {}
        self.server_offset = timedelta(0)
        self.saves: list[tuple[str, str, list[str]]] = []
        self.reads: list[str] = []
        self.scans = 0
        self.before_save = None  # callable(member_no) run after the parent's read, before the child's re-read
        self.kill_in_save = False
        self.save_changes_expiry = False
        self.save_changes_name = False
        self.save_raises = False

    def add(self, n: int, *, member_type: str = "MEMBER", active: str = "T", expiry: str | None = YESTERDAY, last_modified: str = LM,
            member_no: str | None = None) -> str:
        no = member_no or fake_no(n)
        self.members[no] = {"MemberNo": no, "MemberType": member_type, "IsActive": active, "ExpiryDate": expiry, "LastModified": last_modified,
                            "Name": f"Fake Member {n}", "EmailAddress": f"fake{n}@example.com", "RegisterDate": "2026-07-01T00:00:00.000",
                            "LastModifiedUserID": "STAFF"}
        return no

    def server_now(self) -> str:
        return (self.clock().astimezone(SGT).replace(tzinfo=None) + self.server_offset).strftime("%Y-%m-%dT%H:%M:%S")

    def digest(self, record: dict, salt: str) -> str:
        kept = {k: v for k, v in sorted(record.items()) if k not in self.EXCLUDED}
        return "sha256:" + hashlib.sha256((salt + ":" + json.dumps(kept, sort_keys=True)).encode()).hexdigest()

    # -- reads ---------------------------------------------------------------
    def scan(self, after: str, run_day: date):
        self.scans += 1
        floor = datetime(2020, 1, 1)
        day = datetime.combine(run_day, datetime.min.time())
        rows = []
        for no in sorted(self.members):
            m = self.members[no]
            if no <= after or m["MemberType"] != "MEMBER":
                continue
            expiry = tm.parse_local(m["ExpiryDate"])
            if m["ExpiryDate"] is None or (expiry is not None and (expiry < day or expiry < floor)) or expiry is None:
                rows.append({"member_no": no, "is_active": m["IsActive"], "expiry_local": scan_form(m["ExpiryDate"]),
                             "last_modified_local": scan_form(m["LastModified"])})
        return rows[: mr.SCAN_PAGE_SIZE], self.server_now()

    def member(self, member_no: str, salt: str) -> mr.MemberRead:
        self.reads.append(member_no)
        m = self.members.get(member_no)
        if m is None:
            return mr.MemberRead(False, server_now_local=self.server_now())
        return mr.MemberRead(True, member_type=m["MemberType"], is_active=m["IsActive"], expiry_local=m["ExpiryDate"],
                             last_modified_local=m["LastModified"], fields_digest=self.digest(m, salt), server_now_local=self.server_now())

    # -- writes ---------------------------------------------------------------
    def _bump(self, m: dict) -> None:
        m["LastModified"] = (tm.parse_local(m["LastModified"]) + timedelta(seconds=1, milliseconds=7)).strftime("%Y-%m-%dT%H:%M:%S.") + "007"
        m["LastModifiedUserID"] = "OWNER"

    def _write(self, payload: dict, move_id: str, *, restore: bool, on_started) -> mr.SaveOutcome:
        if self.save_raises:
            raise RuntimeError("fake transport failure")
        on_started(4242, "fake-created")
        no = payload["member_no"]
        if self.before_save is not None:
            hook, self.before_save = self.before_save, None
            hook(no)
        m = self.members.get(no)
        if m is None:
            return mr.SaveOutcome("SKIPPED", "member_not_found")
        source, target = ("GUEST", "MEMBER") if restore else ("MEMBER", "GUEST")
        if restore:
            if m["MemberType"] != source or m["LastModified"] != payload["expected_last_modified_local"] or m["ExpiryDate"] != payload["expected_expiry_local"]:
                return mr.SaveOutcome("SKIPPED", "changed_since_removal")
        else:
            if m["MemberType"] == "GUEST":
                return mr.SaveOutcome("SKIPPED", "type_changed_since_read")
            if m["MemberType"] != source:
                return mr.SaveOutcome("SKIPPED", "type_not_in_scope_now")
            if m["LastModified"] != payload["expected_last_modified_local"] or m["ExpiryDate"] != payload["expected_expiry_local"]:
                return mr.SaveOutcome("SKIPPED", "changed_since_read")
            if payload["kind"] == "expire":
                expiry = tm.parse_local(m["ExpiryDate"]).date()
                if not (expiry >= date(2020, 1, 1) and expiry < date.fromisoformat(payload["run_day_local"])):
                    return mr.SaveOutcome("SKIPPED", "not_due_now")
        salt = payload["salt"]
        before = self.digest(m, salt)
        fence = "fence-" + move_id[3:]
        (self.claims / f"{fence}.claim").write_text(json.dumps({"fence_id": fence, "before_digest": before}), encoding="ascii")
        m["MemberType"] = target
        assigned = ["MemberType"]
        if self.save_changes_expiry:
            m["ExpiryDate"] = "2030-01-01T00:00:00.000"
            assigned.append("ExpiryDate")
        if self.save_changes_name:
            m["Name"] = "changed by the fake"
            assigned.append("Name")
        self._bump(m)
        self.saves.append(("restore" if restore else payload["kind"], no, assigned))
        if self.kill_in_save:
            return mr.SaveOutcome("UNCERTAIN", "writer_timeout")
        if m["ExpiryDate"] != payload["expected_expiry_local"]:
            return mr.SaveOutcome("HELD", "readback_expiry_changed", m["LastModified"], ("ExpiryDate",))
        return mr.SaveOutcome("DONE", None, m["LastModified"])

    def save(self, payload, *, move_id, deadline, on_started=lambda pid, created: None):
        mr.check_save_payload(payload)  # as RemovalsBridgeClient.save
        return self._write(dict(payload), move_id, restore=False, on_started=on_started)

    def restore(self, payload, *, move_id, deadline, on_started=lambda pid, created: None):
        mr.check_restore_payload(payload)
        return self._write(dict(payload), move_id, restore=True, on_started=on_started)


class NoSaveBook(FakeBook):
    """Observe must never reach a save (mutation target 15)."""

    def save(self, *args, **kwargs):
        raise AssertionError("observe reached the save client")

    restore = save


# --------------------------------------------------------------------------
# Member core ledger fixture (the member core's own ledger class)
# --------------------------------------------------------------------------

def core_config_value(state_root: Path, sync_root: Path) -> dict:
    value = json.loads((ROOT / "config" / "member_benefits.example.json").read_text(encoding="utf-8"))
    value.update(state_root=str(state_root), member_sync_state_root=str(sync_root))
    return value


class CoreLedger:
    """Rows written straight into a real member core v2 ledger."""

    def __init__(self, root: Path, config: mb.BenefitsConfig):
        self.root = root
        ledger = mb.BenefitsLedger(root, clock=lambda: NOW)
        try:
            ledger.init_meta(config)
        finally:
            ledger.close()

    @contextlib.contextmanager
    def conn(self):
        conn = sqlite3.connect(self.root / mb.LEDGER_FILE, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    def receipt(self, n: int, member_no: str, *, state: str = "applied", code: str | None = "upgrade", status: str = "REFUNDED",
                net: int | None = 0, alerted: str | None = "refund_on_basis", link_source: str = "claim", basis: str | None = BASIS,
                customer: int | None = None) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO receipts(order_gid, customer_gid, order_created_at, order_date_sgt, order_updated_at, financial_status, net_cents, "
                      "state, code, member_no, link_source, basis_expiry_local, alerted, first_seen_at, updated_at) "
                      "VALUES (?, ?, '2026-11-18T03:00:00Z', '2026-11-18', '2026-11-20T03:00:00Z', ?, ?, ?, ?, ?, ?, ?, ?, '2026-11-18T03:05:00Z', "
                      "'2026-11-20T03:05:00Z')",
                      (order_gid(n), customer_gid(customer or n), status, net, state, code, member_no, link_source,
                       basis if state == "applied" else None, alerted))

    def write(self, n: int, member_no: str, *, kind: str = "upgrade", state: str = "DONE", post: str | None = UPGRADE_POST_LM,
              new_expiry: str = BASIS, from_type: str | None = None) -> str:
        write_id = f"write-{n:032x}"
        with self.conn() as c:
            c.execute("INSERT INTO writes(write_id, run_id, order_gid, kind, member_no, from_type, to_type, expected_last_modified_local, "
                      "expected_expiry_local, new_expiry_local, salt, state, post_last_modified_local, created_at, updated_at, deadline_at) "
                      "VALUES (?, 'run-x', ?, ?, ?, ?, 'MEMBER', ?, NULL, ?, 'aa', ?, ?, '2026-11-18T03:06:00Z', '2026-11-18T03:06:00Z', "
                      "'2026-11-18T03:08:00Z')",
                      (write_id, order_gid(n), kind, member_no, from_type or {"upgrade": "GUEST", "extend": "MEMBER"}[kind], LM, new_expiry, state,
                       post if state == "DONE" else None))
        return write_id

    def claim(self, n: int, member_no: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO claim_links(customer_gid, member_no, member_no_key, source, code, created_at) VALUES (?, ?, ?, 'order', 'claimed', "
                      "'2026-11-18T03:04:00Z')", (customer_gid(n), member_no, sync.member_no_key(member_no)))

    def unlink(self, n: int) -> None:
        with self.conn() as c:
            link = c.execute("SELECT link_id FROM claim_links WHERE customer_gid = ?", (customer_gid(n),)).fetchone()[0]
            c.execute("INSERT INTO claim_unlinks(link_id, reason, run_id, created_at) VALUES (?, 'wrong_person', 'run-y', '2026-11-21T00:00:00Z')", (link,))

    def release(self, write_id: str, finding: str = "as_intended") -> None:
        with self.conn() as c:
            c.execute("INSERT INTO write_releases(write_id, finding, member_read_digest, run_id, created_at) VALUES (?, ?, ?, 'run-z', "
                      "'2026-11-19T00:00:00Z')", (write_id, finding, "sha256:" + "0" * 64))


@contextlib.contextmanager
def private_root_allowed(elsewhere: Path):
    """state_root() refuses %LOCALAPPDATA% (where temp dirs live on Windows)
    and needs a protected ACL; both are proven in the #155 tests."""
    with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(elsewhere)}), mock.patch.object(dl, "verify_private_state", lambda root: None):
        yield


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

class Base(unittest.TestCase):
    PRIVATE_DIGITS_RE = re.compile(r"[0-9]{4}")
    REF_RE = re.compile(r"^[rom]-[0-9a-f]{20}\Z")
    MASK_RE = re.compile(r"^\*{3,}[^*]{1,3}\Z")
    WORD_RE = re.compile(r"^[a-z0-9_:]{1,64}\Z")

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-removals-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.root = self.temp / "state"
        (self.root / "claims").mkdir(parents=True)
        self.core_root = self.temp / "core"
        self.core_root.mkdir()
        self.sync_root = self.temp / "sync"
        self.sync_root.mkdir()
        self.now = NOW
        self.core_config = mb.parse_config(core_config_value(self.core_root, self.sync_root))
        self.core = CoreLedger(self.core_root, self.core_config)
        self.sync_by_gid: dict[str, str] = {}
        self.book = FakeBook(self.root / "claims", lambda: self.now)
        self.overrides: dict = {}
        self.ledger = None
        self.probe = "exited"
        self.emitted: list[dict] = []
        real_emit = mr._emit

        def tee(value):
            self.emitted.append(json.loads(json.dumps(value)))
            real_emit(value)
        patcher = mock.patch.object(mr, "_emit", tee)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self.ledger is not None:
            self.check_stored_summaries()
            self.ledger.close()
        for value in self.emitted:
            self.check_private(value)

    # -- privacy (P01) --------------------------------------------------------
    def check_private(self, value) -> None:
        text = json.dumps(value)
        for member_no in self.book.members:
            self.assertNotIn(member_no, text)
        self.assertNotIn("gid://shopify/Order", text)
        self.assertNotIn("gid://shopify/Customer", text)
        for entry in value.get("refs", []) if isinstance(value, dict) else []:
            self.assertRegex(entry["ref"], self.REF_RE)
            self.assertRegex(entry["code"], self.WORD_RE)
            if "member_no_masked" in entry:
                self.assertRegex(entry["member_no_masked"], self.MASK_RE)
        for key in (value.get("counts", {}) if isinstance(value, dict) else {}):
            self.assertRegex(key, self.WORD_RE)
            self.assertIsNone(self.PRIVATE_DIGITS_RE.search(key))
        for code in (value.get("alerts", []) if isinstance(value, dict) else []):
            self.assertRegex(code, self.WORD_RE)

    def check_stored_summaries(self) -> None:
        for (summary,) in self.ledger.conn.execute("SELECT summary FROM runs WHERE summary IS NOT NULL"):
            self.check_private(json.loads(summary))

    # -- builders -------------------------------------------------------------
    def config(self, **overrides) -> mr.RemovalsConfig:
        value = json.loads((ROOT / "config" / "member_removals.example.json").read_text(encoding="utf-8"))
        value.update(state_root=str(self.root), member_core_config="core.json")
        value.update(self.overrides)
        value.update(overrides)
        return mr.parse_config(value, core_loader=lambda path: self.core_config)

    def open_ledger(self, config: mr.RemovalsConfig | None = None) -> mr.RemovalsLedger:
        if self.ledger is None:
            self.ledger = mr.RemovalsLedger(self.root, clock=lambda: self.now)
            self.ledger.init_meta(config or self.config())
        return self.ledger

    def engine(self, book=None, **overrides) -> mr.RemovalsEngine:
        config = self.config(**overrides)
        ledger = self.open_ledger(config)
        links = mb.SyncLinks(self.sync_by_gid, {sync.member_no_key(v): k for k, v in self.sync_by_gid.items()})
        return mr.RemovalsEngine(config, ledger, ac2=book or self.book, sync_links=lambda: links, writer_probe=lambda pid, created: self.probe)

    def expire(self, *, apply: bool = False, book=None, **overrides) -> dict:
        if apply:
            overrides.setdefault("expire_write_enabled", True)
        return self.engine(book, **overrides).run(("expire",), apply=apply, command="expire")

    def revoke(self, *, apply: bool = False, book=None, **overrides) -> dict:
        if apply:
            overrides.setdefault("revoke_write_enabled", True)
        return self.engine(book, **overrides).run(("revoke",), apply=apply, command="revoke")

    def removals(self) -> list[sqlite3.Row]:
        return self.ledger.conn.execute("SELECT * FROM removals ORDER BY rowid").fetchall()

    def refund_case(self, n: int = 1, *, member_lm: str = UPGRADE_POST_LM, member_expiry: str = BASIS + ".000", **receipt) -> str:
        """A member the member core upgraded with order n, then refunded in full."""
        no = self.book.add(n, expiry=member_expiry, last_modified=member_lm)
        self.core.receipt(n, no, **receipt)
        self.core.write(n, no)
        self.core.claim(n, no)
        return no


# --------------------------------------------------------------------------
# Pins against the other tools
# --------------------------------------------------------------------------

class PinTests(unittest.TestCase):
    def test_shared_constants_equal_their_owners(self):
        self.assertEqual(mr.EXPIRY_FLOOR, tags.EXPIRY_FLOOR)
        self.assertEqual(mr.CLOCK_TOLERANCE, tags.CLOCK_TOLERANCE)
        self.assertEqual(mr.MEMBER_CORE_LEDGER_FILE, mb.LEDGER_FILE)
        self.assertEqual(mr.MEMBER_CORE_TOOL, mb.TOOL)
        self.assertEqual(mr.MEMBER_CORE_LEDGER_VERSIONS, (mb.SCHEMA_VERSION,))
        self.assertEqual(mr.SCAN_PAGE_SIZE, tm.SCAN_PAGE_SIZE)
        self.assertEqual((mr.FROM_TYPE, mr.TO_TYPE, mr.PROFILE, mr.RESTORE_PROFILE), ("MEMBER", "GUEST", "benefit_x1", "benefit_x1r"))

    def test_s01_no_shopify_client_or_query_text(self):
        source = (ROOT / "directlocal" / "xb_member_removals.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        for needle in ("import xb_shopify_direct", "ShopifyClient", "token_provider", "TokenCache", "graphql", "mutation ", "query Xb",
                       "urllib", "requests", "http://", "https://", "verify_session"):
            self.assertNotIn(needle, code)
        self.assertNotIn("benefit_p1", source)

    def test_sources_stay_ascii(self):
        for path in (ROOT / "directlocal" / "xb_member_removals.py", Path(__file__)):
            path.read_bytes().decode("ascii")


# --------------------------------------------------------------------------
# Config (C01)
# --------------------------------------------------------------------------

class ConfigTests(Base):
    def test_c01_defaults_are_off_and_bounds_hold(self):
        config = self.config()
        self.assertEqual((config.expire_write_enabled, config.revoke_write_enabled, config.restore_enabled), (False, False, False))
        self.assertEqual(config.qualify_threshold_cents, self.core_config.qualify_threshold_cents)
        for key, (low, high) in mr._BOUNDS.items():
            for bad in (low - 1, high + 1, True, "3"):
                with self.subTest(key=key, bad=bad), self.assertRaises(dl.DirectLocalError) as caught:
                    self.config(**{key: bad})
                self.assertEqual(caught.exception.code, f"removals_config_{key}_invalid")
            self.config(**{key: low})
            self.config(**{key: high})

    def test_c01_refusals(self):
        cases = [
            ({"shopify": {}}, "removals_config_shopify_refused"),
            ({"schema": mb.CONFIG_SCHEMA}, "removals_config_is_other_tool"),
            ({"schema": tm.CONFIG_SCHEMA}, "removals_config_is_other_tool"),
            ({"extra": 1}, "removals_config_keys_invalid"),
            ({"expire_write_enabled": "true"}, "removals_config_expire_write_enabled_invalid"),
            ({"surge_ack_until": "2026-13-01"}, "removals_config_surge_ack_until_invalid"),
            ({"surge_ack_until": "24 Nov"}, "removals_config_surge_ack_until_invalid"),
            ({"ac2_login_mode": "owner_shared", "ac2_refused_user_ids": ["sa"]}, "removals_config_ac2_refused_user_ids_owner_shared_refused"),
            ({"ac2_credential_target": "XB-AC2-MemberSync"}, "removals_config_ac2_credential_target_live_only"),
        ]
        for overrides, code in cases:
            with self.subTest(overrides=overrides), self.assertRaises(dl.DirectLocalError) as caught:
                self.config(**overrides)
            self.assertEqual(caught.exception.code, code)

    def test_c01_live_needs_the_type_move_lock_once_any_switch_is_on(self):
        live = json.loads((ROOT / "config" / "member_removals.live.example.json").read_text(encoding="utf-8"))
        core = mb.BenefitsConfig(**{**self.core_config.__dict__, "autocount_book": "AED_XBOUNDARIES"})
        config = mr.parse_config(live, core_loader=lambda path: core)
        self.assertEqual((config.autocount_book, config.ac2_login_mode, config.expire_write_enabled), ("AED_XBOUNDARIES", "owner_shared", False))
        for switch in mr._SWITCHES:
            with self.subTest(switch=switch), self.assertRaises(dl.DirectLocalError) as caught:
                mr.parse_config({**live, switch: True, "type_move_state_root": None}, core_loader=lambda path: core)
            self.assertEqual(caught.exception.code, "removals_config_type_move_state_root_required")
        with self.assertRaises(dl.DirectLocalError) as caught:
            mr.parse_config(live, core_loader=lambda path: self.core_config)  # the member core config is the test book's
        self.assertEqual(caught.exception.code, "removals_member_core_book_mismatch")
        with self.assertRaises(dl.DirectLocalError) as caught:
            mr.parse_config({**live, "ac2_credential_target": None}, core_loader=lambda path: core)
        self.assertEqual(caught.exception.code, "removals_config_ac2_credential_target_live_required")

    def test_member_core_config_refusal_is_named(self):
        def refuse(path):
            raise dl.DirectLocalError("benefits_config_unreadable")
        value = json.loads((ROOT / "config" / "member_removals.example.json").read_text(encoding="utf-8"))
        with self.assertRaises(dl.DirectLocalError) as caught:
            mr.parse_config(value, core_loader=refuse)
        self.assertEqual(caught.exception.code, "removals_member_core_config_refused")

    def test_rule_fingerprint_moves_with_the_threshold_only(self):
        first = self.config().rule_fingerprint()
        self.assertEqual(first, self.config(max_expires_per_run=50, surge_stop_count=10).rule_fingerprint())
        self.core_config = mb.parse_config({**core_config_value(self.core_root, self.sync_root), "qualify_threshold_cents": 20_000})
        self.assertNotEqual(first, self.config().rule_fingerprint())


# --------------------------------------------------------------------------
# Pure rules
# --------------------------------------------------------------------------

def payload(n: int = 1, **overrides) -> dict:
    value = {"kind": "expire", "member_no": fake_no(n), "from_type": "MEMBER", "to_type": "GUEST", "expected_last_modified_local": LM,
             "expected_expiry_local": YESTERDAY, "run_day_local": RUN_DAY.isoformat(), "basis_expiry_local": None, "salt": "ab" * 16}
    value.update(overrides)
    return value


class RuleTests(unittest.TestCase):
    def test_floor_and_due_day(self):
        """Mutation targets 5 (<= instead of <) and 6 (floor removed)."""
        for value in (None, "1753-01-01T00:00:00.000", "2000-01-01T00:00:00", "2019-12-31T23:59:59.997", "junk", "2026-02-30T00:00:00.000"):
            with self.subTest(value=value):
                self.assertEqual(mr.expiry_due_day(value), (None, "expiry_unreadable"))
        self.assertEqual(mr.expiry_due_day("2020-01-01T00:00:00"), (date(2020, 1, 1), None))
        self.assertTrue(mr.is_due(date(2026, 11, 23), RUN_DAY))
        self.assertFalse(mr.is_due(RUN_DAY, RUN_DAY))  # valid through the expiry day
        self.assertFalse(mr.is_due(date(2026, 11, 25), RUN_DAY))

    def test_clock_tolerance(self):
        """Mutation target 7 (clock guard removed or widened)."""
        for minutes, ok in ((0, True), (9, True), (10, True), (-10, True), (11, False), (-11, False), (600, False)):
            server = (NOW.astimezone(SGT).replace(tzinfo=None) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
            with self.subTest(minutes=minutes):
                if ok:
                    self.assertEqual(mr.server_day(server, NOW), (NOW.astimezone(SGT) + timedelta(minutes=minutes)).date())
                else:
                    with self.assertRaises(dl.DirectLocalError) as caught:
                        mr.server_day(server, NOW)
                    self.assertEqual(caught.exception.code, "clock_mismatch")
        for bad in (None, "2026-11-24 12:00:00", "2026-11-24T12:00:00.000"):
            with self.subTest(bad=bad), self.assertRaises(dl.BridgeError):
                mr.server_day(bad, NOW)

    def test_payload_boundary(self):
        """Mutation targets 1 and 2 (another target or source type), in Python."""
        mr.check_save_payload(payload())
        mr.check_save_payload(payload(kind="revoke", run_day_local=None, expected_expiry_local=BASIS + ".000", basis_expiry_local=BASIS + ".000"))
        cases = [
            (payload(to_type="MEMBER"), "removal_type_not_allowed"),
            (payload(to_type="Default"), "removal_type_not_allowed"),
            (payload(from_type="GUEST"), "removal_type_not_allowed"),
            (payload(from_type="Default"), "removal_type_not_allowed"),
            (payload(kind="move"), "removal_request_invalid"),
            (payload(expected_expiry_local=None), "removal_request_invalid"),
            (payload(expected_expiry_local=TODAY), "removal_not_due"),
            (payload(expected_expiry_local="2000-01-01T00:00:00.000"), "removal_not_due"),
            (payload(run_day_local=None), "removal_request_invalid"),
            (payload(basis_expiry_local=YESTERDAY), "removal_request_invalid"),
            (payload(kind="revoke", run_day_local=None, basis_expiry_local=FUTURE), "removal_request_invalid"),
            (payload(member_no=" 900000001"), "removal_request_invalid"),
            ({**payload(), "new_expiry_local": "2030-01-01T00:00:00"}, "removal_request_invalid"),
        ]
        for value, code in cases:
            with self.subTest(value=value), self.assertRaises(dl.DirectLocalError) as caught:
                mr.check_save_payload(value)
            self.assertEqual(caught.exception.code, code)
        restore = {"kind": "restore", "member_no": fake_no(1), "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                   "expected_expiry_local": YESTERDAY, "removal_id": "rm-" + "a" * 32, "salt": "ab" * 16}
        mr.check_restore_payload(restore)
        for change, code in (({"to_type": "GUEST"}, "removal_type_not_allowed"), ({"from_type": "MEMBER"}, "removal_type_not_allowed"),
                             ({"removal_id": "rs-" + "a" * 32}, "removal_request_invalid")):
            with self.subTest(change=change), self.assertRaises(dl.DirectLocalError) as caught:
                mr.check_restore_payload({**restore, **change})
            self.assertEqual(caught.exception.code, code)


class ClassifySaveTests(unittest.TestCase):
    def outcome(self, **changes) -> str:
        value = {"status": "saved", "save_invocation_count": 1, "readback_found": True, "readback_type_match": True, "readback_expiry_match": True,
                 "fields_unchanged": True, "changed_fields": [], "post_last_modified_local": "2026-11-24T12:00:01.007", "outbound_queue_changed": False}
        value.update(changes)
        return json.dumps(value)

    def test_readback_rules(self):
        """Mutation target 3: a changed ExpiryDate is HELD, never DONE."""
        run = lambda text, code=0: dl.WriteRun(started=True, exit_code=code, stdout=text)
        self.assertEqual(mr.classify_save(run(self.outcome()), claim_exists=True).state, "DONE")
        self.assertEqual(mr.classify_save(run(self.outcome(readback_expiry_match=False)), claim_exists=True), mr.SaveOutcome("HELD", "readback_expiry_changed", "2026-11-24T12:00:01.007"))
        self.assertEqual(mr.classify_save(run(self.outcome(readback_type_match=False)), claim_exists=True).code, "readback_type_mismatch")
        self.assertEqual(mr.classify_save(run(self.outcome(fields_unchanged=False, changed_fields=["Name"])), claim_exists=True).code, "readback_fields_changed")
        self.assertEqual(mr.classify_save(run(self.outcome(outbound_queue_changed=True)), claim_exists=True).code, "outbound_queue_changed")
        self.assertEqual(mr.classify_save(run(self.outcome()), claim_exists=False).state, "UNCERTAIN")
        self.assertEqual(mr.classify_save(run(json.dumps({"status": "skipped", "code": "not_due_now"})), claim_exists=False), mr.SaveOutcome("SKIPPED", "not_due_now"))
        self.assertEqual(mr.classify_save(run(json.dumps({"status": "skipped", "code": "expiry_not_later_now"})), claim_exists=False).state, "UNCERTAIN")
        self.assertEqual(mr.classify_save(run(json.dumps({"status": "skipped", "code": "changed_since_removal"})), claim_exists=False,
                                          skip_codes=mr.RESTORE_SKIP_CODES).state, "SKIPPED")
        self.assertEqual(mr.classify_save(run(json.dumps({"error": "removal_type_not_allowed"}), 1), claim_exists=False),
                         mr.SaveOutcome("NOT_WRITTEN", "removal_type_not_allowed"))
        self.assertEqual(mr.classify_save(run("", 3), claim_exists=True).code, "fence_already_claimed")


# --------------------------------------------------------------------------
# Expire (E01-E13)
# --------------------------------------------------------------------------

class ExpireTests(Base):
    def test_e01_observe_then_apply_moves_type_only(self):
        no = self.book.add(1)
        observe_book = NoSaveBook(self.root / "claims", lambda: self.now)
        observe_book.members = self.book.members
        result = self.expire(book=observe_book)
        self.assertEqual((result["counts"].get("would_expire"), result["saves"], result["run_day"]), (1, 0, RUN_DAY.isoformat()))
        result = self.expire(apply=True)
        self.assertEqual((result["saves"], result["counts"]["expired"]), (1, 1))
        member = self.book.members[no]
        self.assertEqual((member["MemberType"], member["ExpiryDate"]), ("GUEST", YESTERDAY))
        self.assertEqual(self.book.saves, [("expire", no, ["MemberType"])])
        row = self.removals()[0]
        self.assertEqual((row["kind"], row["state"], row["from_type"], row["to_type"], row["day_local"], row["expected_expiry_local"]),
                         ("expire", "DONE", "MEMBER", "GUEST", RUN_DAY.isoformat(), YESTERDAY))

    def test_e02_expiry_today_is_not_due(self):
        self.book.add(1, expiry=TODAY)
        self.book.add(2, expiry="2026-11-24T23:59:59.997")
        result = self.expire(apply=True)
        self.assertEqual((result["saves"], result["counts"].get("would_expire"), result["counts"]["due_expires"]), (0, None, 0))

    def test_e03_unreadable_expiry_alerts_once_and_is_never_written(self):
        for n, value in enumerate((None, "1753-01-01T00:00:00.000", "2000-01-01T00:00:00.000", "2019-12-31T00:00:00.000"), start=1):
            self.book.add(n, expiry=value)
        first = self.expire(apply=True)
        self.assertEqual((first["counts"]["expiry_unreadable"], first["alerts"], first["saves"]), (4, ["expiry_unreadable"], 0))
        second = self.expire(apply=True)
        self.assertEqual((second["counts"]["expiry_unreadable"], second["alerts"]), (4, []))  # counted, alerted once
        self.assertEqual(self.book.saves, [])

    def test_e04_other_types_are_never_candidates(self):
        for n, member_type in enumerate(("GUEST", "Default", "VIP"), start=1):
            self.book.add(n, member_type=member_type)
        result = self.expire(apply=True)
        self.assertEqual((result["saves"], result["counts"]["due_expires"]), (0, 0))
        self.assertEqual(self.book.reads, [])

    def test_e05_inactive_is_counted_not_written(self):
        self.book.add(1, active="F")
        self.book.add(2, active="X")
        result = self.expire(apply=True)
        self.assertEqual((result["counts"]["expired_inactive"], result["counts"]["active_flag_unexpected"], result["saves"]), (1, 1, 0))

    def test_e06_member_no_outside_the_pattern_is_never_read(self):
        self.book.add(1, member_no=" 900000001")
        self.book.add(2, member_no="+65" + f"9{2:07d}")  # a fake phone form, built in code
        result = self.expire(apply=True)
        self.assertEqual((result["counts"]["member_no_unsupported"], result["saves"], self.book.reads), (2, 0, []))
        self.assertIn("member_no_unsupported", result["alerts"])

    def test_e07_open_core_write_or_pending_receipt_skips(self):
        one, two = self.book.add(1), self.book.add(2)
        self.core.receipt(1, one, state="applied", code="upgrade", alerted=None)
        self.core.write(1, one, state="INTENT")
        self.core.receipt(2, two, state="ready", code=None, alerted=None)
        result = self.expire(apply=True)
        self.assertEqual((result["counts"]["core_write_open"], result["counts"]["renewal_pending"], result["saves"]), (1, 1, 0))

    def test_e08_staff_edit_between_read_and_save_is_skipped_and_retried(self):
        no = self.book.add(1)

        def staff(member_no):
            self.book.members[member_no]["LastModified"] = "2026-11-24T11:59:00.000"
        self.book.before_save = staff
        result = self.expire(apply=True)
        self.assertEqual((result["counts"]["changed_since_read"], result["saves"]), (1, 0))
        self.assertEqual(self.removals()[0]["state"], "SKIPPED")
        retry = self.expire(apply=True)
        self.assertEqual(retry["saves"], 1)
        self.assertEqual(self.book.members[no]["MemberType"], "GUEST")

    def test_e08_staff_extend_before_the_member_read_is_not_written(self):
        """Mutation target 4: the expiry re-check (scan vs read) is what keeps a fresh renewal."""
        no = self.book.add(1)
        real_member = self.book.member

        def member(member_no, salt):
            self.book.members[member_no]["ExpiryDate"] = "2028-11-24T00:00:00.000"  # staff renewed at the till
            return real_member(member_no, salt)
        self.book.member = member
        result = self.expire(apply=True)
        self.assertEqual((result["counts"]["changed_since_read"], result["saves"]), (1, 0))
        self.assertEqual(self.book.members[no]["MemberType"], "MEMBER")

    def test_e08_any_expiry_change_since_the_scan_waits_a_run(self):
        """Mutation target 4: the parent compares the scan with its own read, even when both dates are due."""
        no = self.book.add(1)
        real_member = self.book.member

        def member(member_no, salt):
            self.book.members[member_no]["ExpiryDate"] = "2026-11-22T00:00:00.000"  # edited, still past
            self.book.member = real_member
            return real_member(member_no, salt)
        self.book.member = member
        result = self.expire(apply=True)
        self.assertEqual((result["counts"]["changed_since_read"], result["saves"]), (1, 0))
        self.assertEqual(self.expire(apply=True)["saves"], 1)  # the next run reads the new facts
        self.assertEqual(self.book.members[no]["MemberType"], "GUEST")

    def test_e09_clock_gap(self):
        self.book.add(1)
        self.book.server_offset = timedelta(minutes=11)
        result = self.expire(apply=True)
        self.assertEqual((result["stopped"], result["alerts"], result["saves"]), ("clock_mismatch", ["clock_mismatch"], 0))
        self.book.server_offset = timedelta(minutes=9)
        self.assertEqual(self.expire(apply=True)["saves"], 1)

    def test_e09_server_day_decides_near_midnight(self):
        """The VM says 23:55 on the 23rd; the server already says 00:04 on the 24th: the server's day is the run day."""
        self.now = datetime(2026, 11, 23, 15, 55, tzinfo=timezone.utc)
        self.book.server_offset = timedelta(minutes=9)
        no = self.book.add(1, expiry="2026-11-23T00:00:00.000")
        result = self.expire(apply=True)
        self.assertEqual((result["run_day"], result["saves"]), ("2026-11-24", 1))
        self.assertEqual(self.book.members[no]["MemberType"], "GUEST")

    def test_e10_surge_stop_and_acknowledged_batch(self):
        """Mutation target 8 (surge stop removed, or surge_ack_until ignored)."""
        for n in range(1, 12):
            self.book.add(n)
        result = self.expire(apply=True, surge_stop_count=10)
        self.assertEqual((result["saves"], result["counts"]["would_expire"], result["alerts"]), (0, 11, ["removal_surge"]))
        again = self.expire(apply=True, surge_stop_count=10)
        self.assertEqual((again["saves"], again["alerts"]), (0, []))  # once a day
        self.assertEqual(self.expire(apply=True, surge_stop_count=10, surge_ack_until="2026-11-23")["saves"], 0)  # an old plan
        acked = self.expire(apply=True, surge_stop_count=10, surge_ack_until="2026-11-24", max_expires_per_run=4)
        self.assertEqual(acked["saves"], 4)
        self.assertEqual(self.expire(apply=True, surge_stop_count=11)["saves"], 3)  # 7 left: no surge, normal cap

    def test_e11_caps_per_run_per_day_and_stop_file(self):
        """Mutation target 9 (cap off by one; per-day cap not counted from the ledger)."""
        for n in range(1, 9):
            self.book.add(n)
        first = self.expire(apply=True, max_expires_per_run=3, max_removals_per_day=5)
        self.assertEqual((first["saves"], first["counts"]["expire_cap_reached"]), (3, 5))
        second = self.expire(apply=True, max_expires_per_run=3, max_removals_per_day=5)
        self.assertEqual((second["saves"], second["counts"]["day_cap_reached"]), (2, 3))
        self.assertEqual(self.expire(apply=True, max_expires_per_run=3, max_removals_per_day=5)["saves"], 0)
        self.now = NOW + timedelta(days=1)  # a new Singapore day
        (self.root / mr.STOP_FILE).write_text("stop", encoding="ascii")
        stopped = self.expire(apply=True, max_expires_per_run=3, max_removals_per_day=5)
        self.assertEqual((stopped["saves"], stopped["stopped"]), (0, "stop_file_present"))
        (self.root / mr.STOP_FILE).unlink()
        self.assertEqual(self.expire(apply=True, max_expires_per_run=3, max_removals_per_day=5)["saves"], 3)

    def test_e12_switch_off_never_reaches_a_save(self):
        """Mutation target 15 (observe reaches the save client)."""
        book = NoSaveBook(self.root / "claims", lambda: self.now)
        book.add(1)
        result = self.expire(book=book)
        self.assertEqual((result["counts"]["would_expire"], result["saves"]), (1, 0))
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.engine(book).run(("expire",), apply=True, command="expire")  # expire_write_enabled false
        self.assertEqual(caught.exception.code, "removals_write_disabled")
        # scheduled with only the revoke switch on: expire still observes
        result = self.engine(book, revoke_write_enabled=True).run(("revoke", "expire"), apply=True, command="scheduled")
        self.assertEqual((result["counts"]["would_expire"], result["saves"]), (1, 0))

    def test_e13_busy_lock_skips_then_alerts(self):
        """Mutation target 16 is in CliTests (the lock is taken)."""
        engine = self.engine(expire_write_enabled=True)
        first = engine.busy("scheduled")
        self.assertEqual((first["stopped"], first["counts"], first["alerts"]), ("writer_busy", {"writer_busy": 1}, []))
        second = engine.busy("scheduled")
        self.assertEqual(second["alerts"], ["writer_busy_again"])

    def test_day_cap_counts_a_released_held_removal(self):
        """G2 L1: a HELD removal landed (or may have): it counts against the day cap after its release too."""
        first = self.book.add(1)
        for n in range(2, 5):
            self.book.add(n)
        self.book.save_changes_expiry = True
        self.expire(apply=True, max_removals_per_day=2)
        self.book.save_changes_expiry = False
        self.now = NOW + timedelta(seconds=PAST_DEADLINE)
        ref = self.ledger.ref("r", self.removals()[0]["removal_id"])
        self.engine().release_held(removal_ref=ref, member_last3=first[-3:], finding="not_as_intended", apply=True)
        result = self.expire(apply=True, max_removals_per_day=2)
        self.assertEqual((result["saves"], result["counts"]["day_cap_reached"]), (1, 2))

    def test_member_read_rechecks_the_active_flag_and_last_modified(self):
        """G2 L2: the parent compares its own read with the scan before any INTENT row."""
        one, two = self.book.add(1), self.book.add(2)
        real_member = self.book.member

        def member(member_no, salt):
            if member_no == one:
                self.book.members[member_no]["IsActive"] = "F"
            else:
                self.book.members[member_no]["LastModified"] = "2026-11-24T11:00:00.000"
            return real_member(member_no, salt)
        self.book.member = member
        result = self.expire(apply=True)
        self.assertEqual((result["saves"], result["counts"]["changed_since_read"]), (0, 2))
        self.assertEqual(self.removals(), [])  # no INTENT row
        self.assertEqual((self.book.members[one]["MemberType"], self.book.members[two]["MemberType"]), ("MEMBER", "MEMBER"))

    def test_put_back_after_an_expire_is_kept(self):
        no = self.book.add(1)
        self.assertEqual(self.expire(apply=True)["saves"], 1)
        self.book.members[no]["MemberType"] = "MEMBER"  # a hand undo: expiry unchanged
        self.book.members[no]["LastModified"] = "2026-11-24T13:00:00.000"
        result = self.expire(apply=True)
        self.assertEqual((result["saves"], result["counts"]["put_back_kept"]), (0, 1))
        self.book.members[no]["ExpiryDate"] = "2026-11-20T00:00:00.000"  # someone changed the date: due again
        self.assertEqual(self.expire(apply=True)["saves"], 1)

    def test_member_reads_are_capped(self):
        for n in range(1, 5):
            self.book.add(n)
        result = self.expire(apply=True, max_member_reads_per_run=2, max_expires_per_run=10)
        self.assertEqual((result["saves"], result["alerts"]), (2, ["member_reads_capped"]))


# --------------------------------------------------------------------------
# Revoke (V01-V10)
# --------------------------------------------------------------------------

class RevokeTests(Base):
    def test_v01_full_refund_of_an_untouched_upgrade_is_revoked(self):
        no = self.refund_case()
        observe = self.revoke()
        self.assertEqual((observe["counts"]["would_revoke"], observe["saves"]), (1, 0))
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["counts"]["revoked"]), (1, 1))
        self.assertEqual((self.book.members[no]["MemberType"], self.book.members[no]["ExpiryDate"]), ("GUEST", BASIS + ".000"))
        row = self.removals()[0]
        self.assertEqual((row["kind"], row["order_gid"], row["core_write_id"]), ("revoke", order_gid(1), f"write-{1:032x}"))
        self.assertEqual(self.revoke(apply=True)["counts"]["revoke_recorded"], 1)  # never a second send

    def test_v02_units_refunded_below_the_threshold(self):
        self.refund_case(status="PARTIALLY_REFUNDED", net=29_999)
        self.assertEqual(self.revoke(apply=True)["saves"], 1)

    def test_v03_money_only_refund_needs_a_person(self):
        """Mutation target 10 (V3: accepts a money-only partial refund)."""
        self.refund_case(status="PARTIALLY_REFUNDED", net=30_000)
        self.refund_case(2, status="VOIDED", net=35_000)  # a cancellation that is not REFUNDED
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["counts"]["revoke_needs_person"]), (0, 2))
        self.assertIn("revoke_needs_person", result["alerts"])

    def test_v03_paid_receipt_with_no_refund_money_needs_a_person(self):
        """G2 fix round 1 F1: units removed (or a cancellation) with no money refunded is not a positive fact."""
        no = self.refund_case(status="PAID", net=0)
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["alerts"]), (0, ["revoke_needs_person"]))
        self.assertEqual(self.book.members[no]["MemberType"], "MEMBER")
        with self.core.conn() as c:  # the refund is issued later: the member core records it on its next read
            c.execute("UPDATE receipts SET financial_status = 'PARTIALLY_REFUNDED', net_cents = 0 WHERE order_gid = ?", (order_gid(1),))
        again = self.revoke(apply=True)
        self.assertEqual((again["saves"], again["alerts"]), (1, []))
        self.assertEqual(self.book.members[no]["MemberType"], "GUEST")

    def test_v03_other_statuses_are_never_a_positive_fact(self):
        for n, status in enumerate(("PAID", "AUTHORIZED", "PENDING", "VOIDED", "PARTIALLY_PAID", None), start=1):
            self.refund_case(n, status=status, net=0)
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["counts"]["revoke_needs_person"]), (0, 6))
        self.assertTrue(mr.refund_is_positive({"financial_status": "REFUNDED", "net_cents": 40_000}, 30_000))
        self.assertTrue(mr.refund_is_positive({"financial_status": "PARTIALLY_REFUNDED", "net_cents": 29_999}, 30_000))
        self.assertFalse(mr.refund_is_positive({"financial_status": "PARTIALLY_REFUNDED", "net_cents": 30_000}, 30_000))
        self.assertFalse(mr.refund_is_positive({"financial_status": "PARTIALLY_REFUNDED", "net_cents": None}, 30_000))

    def test_v04_refund_on_an_extend_is_an_alert(self):
        """Mutation target 10 (V1: accepts an extend)."""
        no = self.book.add(1, expiry=BASIS + ".000", last_modified=UPGRADE_POST_LM)
        self.core.receipt(1, no, code="extend")
        self.core.write(1, no, kind="extend")
        self.core.claim(1, no)
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["alerts"]), (0, ["refund_on_extend"]))

    def test_v05_a_released_write_is_not_proof(self):
        """Mutation target 11 (revoke from any receipt or write state)."""
        no = self.book.add(1, expiry=BASIS + ".000", last_modified=UPGRADE_POST_LM)
        self.core.receipt(1, no)
        write_id = self.core.write(1, no, state="HELD")
        self.core.release(write_id)
        self.core.claim(1, no)
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["alerts"]), (0, ["revoke_needs_person"]))
        no2 = self.book.add(2, expiry=BASIS + ".000", last_modified=UPGRADE_POST_LM)
        self.core.receipt(2, no2, state="no_change", code="expiry_already_later")  # not applied: not a candidate
        self.core.claim(2, no2)
        again = self.revoke(apply=True)
        self.assertEqual((again["saves"], again["alerts"], again["counts"]), (0, [], {"revoke_needs_person": 1}))

    def test_v06_another_qualifying_receipt_keeps_the_member(self):
        """Mutation target 10 (V4)."""
        no = self.refund_case()
        self.core.receipt(2, no, state="no_change", code="expiry_already_later", status="PAID", net=31_000, alerted=None, customer=1)
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["counts"]["revoke_other_basis"]), (0, 1))

    def test_v06_a_refunded_other_receipt_is_not_a_basis(self):
        """G2 L9: another receipt that is itself REFUNDED does not keep the member."""
        no = self.refund_case()
        self.core.receipt(2, no, state="no_change", code="expiry_already_later", status="REFUNDED", net=31_000, alerted=None, customer=1)
        self.assertEqual(self.revoke(apply=True)["saves"], 1)

    def test_v07_open_member_core_write_or_pending_receipt_blocks_a_revoke(self):
        """G2 fix round 1 F2 (V7): the member core may be writing or about to renew this member."""
        one = self.refund_case(1)
        self.core.write(11, one, state="UNCERTAIN")  # another order's write, not settled
        two = self.refund_case(2)
        self.core.receipt(12, two, state="ready", code=None, alerted=None, customer=2)
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["counts"]["core_write_open"], result["counts"]["renewal_pending"]), (0, 1, 1))
        self.assertEqual(self.book.reads, [])  # never read for a write

    def test_v07_open_removal_blocks_a_revoke(self):
        """G2 fix round 1 F2 (V7): an earlier removal of this member is not settled."""
        no = self.refund_case()
        ledger = self.open_ledger()
        with ledger.tx() as conn:
            conn.execute("INSERT INTO removals(removal_id, run_id, kind, member_no, from_type, to_type, expected_last_modified_local, "
                         "expected_expiry_local, basis_expiry_local, day_local, order_gid, core_write_id, salt, state, created_at, updated_at, "
                         "deadline_at) VALUES (?, 'run-x', 'revoke', ?, 'MEMBER', 'GUEST', ?, ?, ?, ?, ?, 'write-y', 's', 'INTENT', 't', 't', 't')",
                         ("rm-" + "e" * 32, no, UPGRADE_POST_LM, BASIS + ".000", BASIS + ".000", RUN_DAY.isoformat(), order_gid(99)))
            conn.execute("UPDATE removals SET state = 'UNCERTAIN'")
        result = self.revoke()
        self.assertEqual((result["counts"].get("removal_open"), result["counts"].get("would_revoke")), (1, None))

    def test_v07_inactive_member_is_not_revoked(self):
        """G2 fix round 1 F2 (V7)."""
        no = self.refund_case()
        self.book.members[no]["IsActive"] = "F"
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["counts"]["revoke_member_inactive"]), (0, 1))
        self.assertEqual(self.book.members[no]["MemberType"], "MEMBER")

    def test_revoke_cap_is_checked_before_the_member_read(self):
        """G2 L7: a revoke backlog never spends the shared read budget."""
        for n in range(1, 6):
            self.refund_case(n)
        result = self.revoke(apply=True, max_revokes_per_run=1)
        self.assertEqual((result["saves"], len(self.book.reads)), (1, 1))

    def test_l10_revoke_row_keeps_the_upgrade_basis(self):
        """G2 recheck LATER-1: the removals row stores the upgrade's basis
        (from the member core write), apart from the expiry the parent read,
        and the child's payload takes its basis from that column."""
        self.refund_case()
        seen = []
        real_save = self.book.save

        def save(payload, **kwargs):
            seen.append((payload["basis_expiry_local"], payload["expected_expiry_local"]))
            return real_save(payload, **kwargs)
        self.book.save = save
        self.assertEqual(self.revoke(apply=True)["saves"], 1)
        row = self.removals()[0]
        self.assertEqual((row["kind"], row["basis_expiry_local"], row["expected_expiry_local"]), ("revoke", BASIS + ".000", BASIS + ".000"))
        self.assertEqual(seen, [(BASIS + ".000", BASIS + ".000")])

    def test_expire_needs_the_member_core_ledger(self):
        """Every run reads the member core facts first (V7 skips), expire
        included: with no member core ledger the run refuses before any
        AutoCount read (test-book UAT prompt 1220 step order)."""
        self.book.add(1)
        (self.core_root / mb.LEDGER_FILE).rename(self.core_root / "moved.sqlite3")
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.expire()
        self.assertEqual((caught.exception.code, self.book.scans, self.book.reads), ("member_core_ledger_missing", 0, []))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_l10_expire_row_has_no_basis(self):
        self.book.add(1)
        self.assertEqual(self.expire(apply=True)["saves"], 1)
        row = self.removals()[0]
        self.assertEqual((row["kind"], row["basis_expiry_local"]), ("expire", None))
        self.assertIsNone(mr.removal_payload(row)["basis_expiry_local"])

    def test_later2_observe_does_not_read_a_changed_member_again(self):
        """G2 recheck LATER-2: once a revoke candidate was found changed since
        the upgrade, observe runs skip its member read (it stays changed:
        LastModified only moves on); a writing run still reads it."""
        no = self.refund_case()
        self.book.members[no]["LastModified"] = "2026-11-22T10:00:00.000"  # staff touched the member
        first = self.revoke()
        self.assertEqual((first["alerts"], len(self.book.reads)), (["revoke_member_changed"], 1))
        second = self.revoke()
        self.assertEqual((second["alerts"], second["counts"]["revoke_member_changed"], len(self.book.reads)), ([], 1, 1))
        third = self.revoke(apply=True)
        self.assertEqual((third["saves"], third["counts"]["revoke_member_changed"], len(self.book.reads)), (0, 1, 2))

    def test_v07_voided_claim_link(self):
        """Mutation target 10 (V5)."""
        self.refund_case()
        self.core.unlink(1)
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["alerts"]), (0, ["revoke_link_voided"]))

    def test_v07_sync155_link(self):
        no = self.book.add(1, expiry=BASIS + ".000", last_modified=UPGRADE_POST_LM)
        self.core.receipt(1, no, link_source="sync155")
        self.core.write(1, no)
        self.assertEqual(self.revoke(apply=True)["alerts"], ["revoke_link_voided"])  # no #155 link now
        self.sync_by_gid[customer_gid(1)] = no
        self.assertEqual(self.revoke(apply=True)["saves"], 1)

    def test_v08_member_touched_since_the_upgrade(self):
        """Mutation target 10 (V6: staff renewal in store is invisible to the tool)."""
        self.refund_case(member_lm="2026-11-22T10:00:00.000")
        self.refund_case(2, member_expiry="2028-11-22T00:00:00.000")
        result = self.revoke(apply=True)
        self.assertEqual((result["saves"], result["counts"]["revoke_member_changed"]), (0, 2))
        self.assertEqual(self.revoke(apply=True)["alerts"], [])  # once

    def test_v09_member_core_ledger_guards(self):
        """Mutation target 17 (member core ledger opened writable)."""
        self.refund_case()
        facts = mr.read_member_core(self.config())
        self.assertEqual(len(facts.flagged), 1)
        with mock.patch.object(mr.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            mr.read_member_core(self.config())
        args, kwargs = connect.call_args
        self.assertTrue(args[0].endswith("?mode=ro"))
        self.assertTrue(kwargs.get("uri"))
        conn = sqlite3.connect(self.core_root / mb.LEDGER_FILE)
        conn.execute("PRAGMA user_version=3")
        conn.close()
        with self.assertRaises(dl.DirectLocalError) as caught:
            mr.read_member_core(self.config())
        self.assertEqual(caught.exception.code, "member_core_ledger_version_unknown")
        (self.core_root / mb.LEDGER_FILE).rename(self.core_root / "moved.sqlite3")
        with self.assertRaises(dl.DirectLocalError) as caught:
            mr.read_member_core(self.config())
        self.assertEqual(caught.exception.code, "member_core_ledger_missing")

    def test_v09_binding_mismatch(self):
        other = mb.parse_config({**core_config_value(self.core_root, self.sync_root), "shopify": {
            **core_config_value(self.core_root, self.sync_root)["shopify"], "shop_domain": "other-store.synthetic.invalid"}})
        self.core_config = other
        with self.assertRaises(dl.DirectLocalError) as caught:
            mr.read_member_core(self.config())
        self.assertEqual(caught.exception.code, "member_core_ledger_binding_mismatch")

    def test_v09_read_only_opens_cannot_write(self):
        conn = mr.open_read_only(self.core_root / mb.LEDGER_FILE)
        try:
            with self.assertRaises(sqlite3.Error):
                conn.execute("CREATE TABLE x(a)")
        finally:
            conn.close()

    def test_v10_revoked_member_upgrades_again_on_a_new_order(self):
        """The member core's own rule: a GUEST with the revoked (future) expiry is upgraded by a later order."""
        read = mb.MemberRead(True, member_type="GUEST", is_active="T", expiry_local=BASIS + ".000", last_modified_local="2026-11-24T12:00:01.007")
        verdict = mb.decide(read, date(2026, 12, 1))
        self.assertEqual((verdict.action, verdict.new_expiry_local), ("upgrade", "2028-12-01T00:00:00"))
        self.assertEqual(mb.decide(read, date(2026, 11, 18)).code, "guest_expiry_already_later")  # same day: staff by hand

    def test_revoke_cap(self):
        for n in range(1, 6):
            self.refund_case(n)
        result = self.revoke(apply=True, max_revokes_per_run=2)
        self.assertEqual((result["saves"], result["counts"]["revoke_cap_reached"]), (2, 3))


# --------------------------------------------------------------------------
# Unclear writes, resolve, release-held
# --------------------------------------------------------------------------

class UnclearWriteTests(Base):
    def test_q20_kill_inside_save_is_uncertain_and_resolved_by_read_back(self):
        no = self.book.add(1)
        self.book.kill_in_save = True
        result = self.expire(apply=True)
        self.assertEqual((result["stopped"], result["alerts"]), ("uncertain:writer_timeout", ["write_uncertain"]))
        self.book.kill_in_save = False
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.expire(apply=True)
        self.assertEqual(caught.exception.code, "removal_uncertain_unresolved")
        with self.assertRaises(dl.DirectLocalHold):
            self.engine().resolve()  # deadline not passed yet
        self.now = NOW + timedelta(seconds=PAST_DEADLINE)
        self.probe = "running"
        with self.assertRaises(dl.DirectLocalHold):
            self.engine().resolve()
        self.probe = "exited"
        resolved = self.engine().resolve()
        self.assertEqual(resolved["resolved"], {"removal:DONE:resolved_saved": 1})
        self.assertEqual(len(self.book.saves), 1)  # never re-sent
        self.assertEqual(self.book.members[no]["MemberType"], "GUEST")

    def test_q21_changed_expiry_on_read_back_is_held_and_latches(self):
        """Mutation target 3 (ExpiryDate written or excluded from the read-back)."""
        no = self.book.add(1)
        self.book.add(2)
        self.book.save_changes_expiry = True
        result = self.expire(apply=True)
        self.assertEqual((result["stopped"], result["alerts"], result["saves"]), ("held:readback_expiry_changed", ["write_held"], 0))
        self.book.save_changes_expiry = False
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.expire(apply=True)
        self.assertEqual(caught.exception.code, "removal_held_rows_present")
        self.now = NOW + timedelta(seconds=PAST_DEADLINE)
        ref = self.ledger.ref("r", self.removals()[0]["removal_id"])
        engine = self.engine()
        with self.assertRaises(dl.DirectLocalError) as caught:
            engine.release_held(removal_ref=ref, member_last3=no[-3:], finding="as_intended", apply=True)
        self.assertEqual(caught.exception.code, "release_finding_not_proven")  # the expiry is not the one expected
        dry = engine.release_held(removal_ref=ref, member_last3=no[-3:], finding="not_as_intended", apply=False)
        self.assertEqual(dry["command"], "release_held_dry_run")
        done = engine.release_held(removal_ref=ref, member_last3=no[-3:], finding="not_as_intended", apply=True)
        self.assertEqual((done["write_kind"], done["held_writes_left"]), ("removal", 0))
        self.assertEqual(self.expire(apply=True)["saves"], 1)  # latch cleared; the other member

    def test_q21_resolve_needs_the_digest(self):
        """G2 fix round 1 F3: a killed save that also changed another column resolves HELD, never DONE."""
        no = self.book.add(1)
        self.book.add(2)
        self.book.kill_in_save = True
        self.book.save_changes_name = True
        self.assertEqual(self.expire(apply=True)["stopped"], "uncertain:writer_timeout")
        self.book.kill_in_save = self.book.save_changes_name = False
        self.now = NOW + timedelta(seconds=PAST_DEADLINE)
        resolved = self.engine().resolve()
        self.assertEqual((resolved["resolved"], resolved["alerts"]), ({"removal:HELD:resolved_unclear": 1}, ["resolved_unclear"]))
        self.assertEqual(self.book.members[no]["MemberType"], "GUEST")  # it did land, with a changed column: a person looks
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.expire(apply=True)
        self.assertEqual(caught.exception.code, "removal_held_rows_present")  # latched

    def test_released_held_member_alerts_once_not_a_silent_skip(self):
        """G2 L3: after release-held the member's removal stays HELD; the member is skipped with one alert."""
        no = self.book.add(1)
        self.book.save_changes_expiry = True
        self.expire(apply=True)
        self.book.save_changes_expiry = False
        self.book.members[no].update(MemberType="MEMBER", ExpiryDate=YESTERDAY)  # staff put it back by hand
        self.now = NOW + timedelta(seconds=PAST_DEADLINE)
        ref = self.ledger.ref("r", self.removals()[0]["removal_id"])
        self.engine().release_held(removal_ref=ref, member_last3=no[-3:], finding="not_as_intended", apply=True)
        first = self.expire(apply=True)
        self.assertEqual((first["saves"], first["alerts"]), (0, ["removal_held_released"]))
        second = self.expire(apply=True)
        self.assertEqual((second["alerts"], second["counts"]["removal_held_released"]), ([], 1))

    def test_transport_failure_is_uncertain(self):
        self.book.add(1)
        self.book.save_raises = True
        result = self.expire(apply=True)
        self.assertEqual(result["stopped"], "uncertain:save_call_failed")
        self.now = NOW + timedelta(seconds=PAST_DEADLINE)
        self.assertEqual(self.engine().resolve()["resolved"], {"removal:NOT_WRITTEN:resolved_no_claim": 1})


# --------------------------------------------------------------------------
# Undo list and restore (U01-U03)
# --------------------------------------------------------------------------

class RestoreTests(Base):
    def setUp(self):
        super().setUp()
        self.nos = [self.book.add(n) for n in (1, 2, 3)]
        self.first = self.expire(apply=True)
        self.assertEqual(self.first["saves"], 3)

    def ref(self, index: int) -> str:
        return self.ledger.ref("r", self.removals()[index]["removal_id"])

    def test_u01_restore_one_untouched_removal(self):
        engine = self.engine(restore_enabled=True)
        dry = engine.restore(removal_ref=self.ref(0), member_last3=self.nos[0][-3:], apply=False)
        self.assertEqual((dry["counts"], dry["saves"]), ({"would_restore": 1}, 0))
        done = engine.restore(removal_ref=self.ref(0), member_last3=self.nos[0][-3:], apply=True)
        self.assertEqual(done["counts"], {"restored": 1})
        member = self.book.members[self.nos[0]]
        self.assertEqual((member["MemberType"], member["ExpiryDate"]), ("MEMBER", YESTERDAY))
        self.assertEqual(self.book.saves[-1], ("restore", self.nos[0], ["MemberType"]))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM undo_list").fetchone()[0], 2)
        self.assertEqual(self.expire(apply=True)["counts"]["put_back_kept"], 1)  # never expired again on its own

    def test_u01_restore_a_run_latest_first(self):
        result = self.engine(restore_enabled=True).restore(run=self.first["run_id"], max_rows=2, apply=True)
        self.assertEqual(result["counts"], {"restored": 2})
        self.assertEqual([s[1] for s in self.book.saves[-2:]], [self.nos[2], self.nos[1]])

    def test_u02_restore_after_a_staff_edit_is_skipped(self):
        """Mutation target 12 (restore skips the changed_since_removal check)."""
        self.book.members[self.nos[0]]["LastModified"] = "2026-11-24T14:00:00.000"
        result = self.engine(restore_enabled=True).restore(removal_ref=self.ref(0), member_last3=self.nos[0][-3:], apply=True)
        self.assertEqual((result["counts"], result["saves"]), ({"changed_since_removal": 1}, 0))
        self.assertEqual(self.book.members[self.nos[0]]["MemberType"], "GUEST")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM restores").fetchone()[0], 0)  # never sent to the child

    def test_u03_restore_refusals(self):
        """Mutation target 12 (restore not bound to a DONE removal)."""
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.engine().restore(removal_ref=self.ref(0), member_last3=self.nos[0][-3:], apply=True)
        self.assertEqual(caught.exception.code, "restore_disabled")
        engine = self.engine(restore_enabled=True)
        for kwargs, code in (({"removal_ref": self.ref(0), "member_last3": "zzz"}, "removal_member_mismatch"),
                             ({"removal_ref": "r-" + "0" * 20, "member_last3": "001"}, "removal_not_found"),
                             ({"removal_ref": "o-" + "0" * 20, "member_last3": "001"}, "removal_arguments_invalid"),
                             ({}, "restore_arguments_invalid"),
                             ({"run": "run-x", "max_rows": 1}, "restore_arguments_invalid"),
                             ({"run": self.first["run_id"], "max_rows": 51}, "restore_arguments_invalid")):
            with self.subTest(kwargs=kwargs), self.assertRaises(dl.DirectLocalError) as caught:
                engine.restore(apply=True, **kwargs)
            self.assertEqual(caught.exception.code, code)
        engine.restore(removal_ref=self.ref(0), member_last3=self.nos[0][-3:], apply=True)
        again = engine.restore(removal_ref=self.ref(0), member_last3=self.nos[0][-3:], apply=True)
        self.assertEqual(again["counts"], {"restore_already_recorded": 1})

    def test_undo_list_masked_and_screen(self):
        config = self.config()
        listing = mr.undo_listing(self.root, config=config)
        self.assertEqual((listing["total"], listing["by_day"]), (3, {RUN_DAY.isoformat(): {"expire": 3, "revoke": 0}}))
        self.assertEqual({e["member_no_masked"] for e in listing["removals"]}, {mr.mask(no) for no in self.nos})
        self.check_private(listing)
        full = mr.undo_listing(self.root, config=config, full=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mr.show_undo(full)
        for no in self.nos:
            self.assertIn(json.dumps(no), out.getvalue())
        self.assertIn("VM screen only", out.getvalue())
        self.assertEqual(mr.undo_listing(self.root, config=config, kind="revoke")["total"], 0)


# --------------------------------------------------------------------------
# Ledger (L01)
# --------------------------------------------------------------------------

REVOKE_ROW = {"kind": "revoke", "expected_expiry_local": BASIS + ".000", "basis_expiry_local": BASIS + ".000", "order_gid": order_gid(1),
              "core_write_id": "write-" + "1" * 32}


class LedgerTests(Base):
    def insert(self, **changes):
        values = {"removal_id": "rm-" + "1" * 32, "run_id": "run-a", "kind": "expire", "member_no": fake_no(1), "from_type": "MEMBER",
                  "to_type": "GUEST", "expected_last_modified_local": LM, "expected_expiry_local": YESTERDAY, "basis_expiry_local": None,
                  "day_local": RUN_DAY.isoformat(), "order_gid": None, "core_write_id": None, "salt": "s", "state": "INTENT", "created_at": "t",
                  "updated_at": "t", "deadline_at": "t"}
        values.update(changes)
        with self.ledger.tx() as conn:
            conn.execute(f"INSERT INTO removals({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))

    def test_l01_checks_and_triggers(self):
        """Mutation target 20 (a ledger CHECK or trigger dropped)."""
        self.open_ledger()
        bad = [
            {"to_type": "MEMBER"}, {"to_type": "Default"}, {"from_type": "GUEST"}, {"expected_expiry_local": None},
            {"expected_expiry_local": TODAY}, {"expected_expiry_local": "2000-01-01T00:00:00.000"}, {"kind": "revoke"},
            {"kind": "move"}, {"state": "DONE"}, {"removal_id": "x-1"}, {"basis_expiry_local": YESTERDAY},
            {**REVOKE_ROW, "basis_expiry_local": None}, {**REVOKE_ROW, "order_gid": None},
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.insert(**changes)
        self.insert()
        for statement in ("UPDATE removals SET member_no = 'x'", "UPDATE removals SET expected_expiry_local = 'x'",
                          "UPDATE removals SET basis_expiry_local = 'x'", "DELETE FROM removals",
                          "UPDATE removals SET state = 'DONE'", "UPDATE meta SET ref_key = 'x'"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.Error):
                with self.ledger.tx() as conn:
                    conn.execute(statement)
        with self.assertRaises(sqlite3.Error):
            self.insert(state="INTENT")  # INSERT OR REPLACE is the only way round the key; plain insert is a key clash
        with self.assertRaises(sqlite3.Error), self.ledger.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO removals SELECT * FROM removals")
        with self.ledger.tx() as conn:
            conn.execute("UPDATE removals SET state = 'UNCERTAIN'")
        with self.assertRaises(sqlite3.Error), self.ledger.tx() as conn:
            conn.execute("UPDATE removals SET state = 'SKIPPED'")
        with self.assertRaises(sqlite3.Error), self.ledger.tx() as conn:  # a restore of a removal that is not DONE
            conn.execute("INSERT INTO restores(restore_id, removal_id, run_id, member_no, from_type, to_type, expected_last_modified_local, "
                         "expected_expiry_local, salt, state, created_at, updated_at, deadline_at) VALUES ('rs-1', ?, 'r', ?, 'GUEST', 'MEMBER', ?, ?, "
                         "'s', 'INTENT', 't', 't', 't')", ("rm-" + "1" * 32, fake_no(1), LM, YESTERDAY))
        with self.assertRaises(sqlite3.Error), self.ledger.tx() as conn:
            conn.execute("INSERT INTO write_releases(write_id, finding, member_read_digest, run_id, created_at) VALUES (?, 'as_intended', ?, 'r', 't')",
                         ("rm-" + "1" * 32, "sha256:" + "0" * 64))

    def test_l10_revoke_basis_is_immutable(self):
        self.open_ledger()
        self.insert(**REVOKE_ROW)
        with self.assertRaises(sqlite3.Error) as caught, self.ledger.tx() as conn:
            conn.execute("UPDATE removals SET basis_expiry_local = ?", (FUTURE,))
        self.assertIn("removal_immutable", str(caught.exception))

    def test_l10_child_payload_takes_the_basis_from_the_row(self):
        """G2 recheck LATER-1: the payload's basis is the stored upgrade basis,
        never a copy of the expected expiry, so a row whose two values differ
        is refused by the payload check (as by the child) before any send."""
        ledger = self.open_ledger()
        written = {**payload(kind="revoke", run_day_local=None, expected_expiry_local=BASIS + ".000"), "basis_expiry_local": FUTURE}
        row = ledger.add_removal(run_id="run-a", payload=written, day_local=RUN_DAY.isoformat(), order_gid=order_gid(1),
                                 core_write_id="write-" + "1" * 32, deadline=NOW)
        self.assertEqual((row["basis_expiry_local"], row["expected_expiry_local"]), (FUTURE, BASIS + ".000"))
        sent = mr.removal_payload(row)
        self.assertEqual((sent["basis_expiry_local"], sent["expected_expiry_local"]), (FUTURE, BASIS + ".000"))
        with self.assertRaises(dl.DirectLocalError) as caught:
            mr.check_save_payload(sent)
        self.assertEqual(caught.exception.code, "removal_request_invalid")

    def test_l01_restore_needs_a_done_removal_even_with_matching_values(self):
        """Mutation target 12: the trigger checks the removal's state, not only its values."""
        self.open_ledger()
        self.insert()
        with self.ledger.tx() as conn:
            conn.execute("UPDATE removals SET state = 'SKIPPED', post_last_modified_local = ?", ("2026-11-24T12:00:01.007",))
        with self.assertRaises(sqlite3.IntegrityError), self.ledger.tx() as conn:
            conn.execute("INSERT INTO restores(restore_id, removal_id, run_id, member_no, from_type, to_type, expected_last_modified_local, "
                         "expected_expiry_local, salt, state, created_at, updated_at, deadline_at) VALUES ('rs-1', ?, 'r', ?, 'GUEST', 'MEMBER', ?, ?, "
                         "'s', 'INTENT', 't', 't', 't')", ("rm-" + "1" * 32, fake_no(1), "2026-11-24T12:00:01.007", YESTERDAY))

    def test_l01_restore_binding(self):
        self.book.add(1)
        self.expire(apply=True)
        removal = self.removals()[0]
        with self.assertRaises(sqlite3.Error), self.ledger.tx() as conn:  # wrong expected LastModified
            conn.execute("INSERT INTO restores(restore_id, removal_id, run_id, member_no, from_type, to_type, expected_last_modified_local, "
                         "expected_expiry_local, salt, state, created_at, updated_at, deadline_at) VALUES ('rs-1', ?, 'r', ?, 'GUEST', 'MEMBER', ?, ?, "
                         "'s', 'INTENT', 't', 't', 't')", (removal["removal_id"], removal["member_no"], LM, YESTERDAY))
        self.ledger.add_restore(run_id="r", removal=removal, salt="s", deadline=NOW)
        with self.assertRaises(sqlite3.Error):
            self.ledger.add_restore(run_id="r", removal=removal, salt="s", deadline=NOW)  # one active restore per removal

    def test_ledger_made_by_other_code_is_refused(self):
        self.open_ledger()
        self.ledger.close()
        self.ledger = None
        for version in (1, 3):  # 1: the first ledger shape, made before the removals row kept the upgrade basis (G2 recheck LATER-1)
            conn = sqlite3.connect(self.root / mr.LEDGER_FILE)
            conn.execute(f"PRAGMA user_version={version}")
            conn.close()
            with self.subTest(version=version):
                with self.assertRaises(dl.DirectLocalError) as caught:
                    mr.RemovalsLedger(self.root)
                self.assertEqual(caught.exception.code, "removals_ledger_not_recognised")

    def test_rule_settings_change_refuses_runs(self):
        self.open_ledger()
        self.core_config = mb.parse_config({**core_config_value(self.core_root, self.sync_root), "qualify_threshold_cents": 20_000})
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.engine().run(("expire",), apply=False, command="expire")
        self.assertEqual(caught.exception.code, "removals_rule_settings_changed")


# --------------------------------------------------------------------------
# Digest and status
# --------------------------------------------------------------------------

class DigestTests(Base):
    def test_digest_is_read_only_and_masked(self):
        self.book.add(1)
        self.book.add(2, expiry=None)
        self.expire(apply=True)
        digest = mr.ledger_digest(self.root, hours=24, now=self.now, config=self.config())
        self.assertEqual(digest["schema"], mr.DIGEST_SCHEMA)
        self.assertEqual((digest["window"], digest["open"]), ({"expired": 1, "revoked": 0, "restored": 0},
                                                              {"uncertain_writes": 0, "held_writes": 0, "undo_list": 1}))
        self.assertEqual(digest["alerts"], {"expiry_unreadable": 1})
        self.assertTrue(all(self.MASK_RE.fullmatch(r["member_no_masked"]) for r in digest["alert_refs"]))
        self.check_private(digest)
        status = self.engine().status()
        self.assertEqual((status["removals"], status["undo_list"], status["expire_write_enabled"]), ({"expire:DONE": 1}, 1, False))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class CliTests(Base):
    def write_configs(self, **overrides) -> Path:
        core_path = self.temp / "member_benefits.json"
        core_path.write_text(json.dumps(core_config_value(self.core_root, self.sync_root)), encoding="utf-8")
        value = json.loads((ROOT / "config" / "member_removals.example.json").read_text(encoding="utf-8"))
        value.update(state_root=str(self.root), member_core_config=str(core_path))
        value.update(overrides)
        path = self.temp / "member_removals.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def cli(self, *args: str, factory=None) -> tuple[int, str]:
        out = io.StringIO()
        with private_root_allowed(self.temp / "elsewhere"), contextlib.redirect_stdout(out):
            code = mr.main(list(args), engine_factory=factory, clock=lambda: self.now)
        return code, out.getvalue()

    def factory(self):
        links = mb.SyncLinks({}, {})
        return lambda cfg, led: mr.RemovalsEngine(cfg, led, ac2=self.book, sync_links=lambda: links, writer_probe=lambda pid, created: "exited")

    def test_switch_off_refuses_before_any_ledger(self):
        path = self.write_configs()
        code, out = self.cli("expire", "--config", str(path), "--apply")
        self.assertEqual((code, json.loads(out)), (2, {"status": "refused", "code": "removals_write_disabled"}))
        self.assertFalse((self.root / mr.LEDGER_FILE).exists())
        code, out = self.cli("restore", "--config", str(path), "--apply", "--run", "run-" + "0" * 32, "--max", "1")
        self.assertEqual(json.loads(out)["code"], "restore_disabled")
        code, out = self.cli("check-config", "--config", str(path))
        self.assertEqual((code, json.loads(out)["expire_write_enabled"]), (0, False))

    def test_observe_apply_status_digest_and_undo_list(self):
        path = self.write_configs(expire_write_enabled=True)
        no = self.book.add(1)
        self.assertEqual(self.cli("init", "--config", str(path))[0], 0)
        code, out = self.cli("scheduled", "--config", str(path), factory=self.factory())
        self.assertEqual((code, json.loads(out)["counts"]["would_expire"]), (0, 1))
        code, out = self.cli("scheduled", "--config", str(path), "--apply", factory=self.factory())
        self.assertEqual((code, json.loads(out)["saves"]), (0, 1))
        self.assertEqual(self.book.members[no]["MemberType"], "GUEST")
        for command in ("status", "digest", "undo-list"):
            with self.subTest(command=command):
                code, out = self.cli(command, "--config", str(path))
                self.assertEqual(code, 0)
                self.assertNotIn(no, out)
        emitted = len(self.emitted)
        code, out = self.cli("undo-list", "--config", str(path), "--show")
        self.assertEqual(code, 0)
        self.assertIn(json.dumps(no), out)
        self.assertEqual(len(self.emitted), emitted)  # mutation target 19: --show never goes through _emit

    def test_lock_is_taken_by_apply(self):
        """Mutation target 16: apply holds the type move lock; busy is a skip."""
        move_root = self.temp / "move"
        move_root.mkdir()
        (move_root / tm.LEDGER_FILE).write_bytes(b"")
        path = self.write_configs(expire_write_enabled=True, type_move_state_root=str(move_root))
        self.book.add(1)
        self.cli("init", "--config", str(path))
        held = dl._OwnerLock(move_root / tm.LOCK_FILE)
        try:
            code, out = self.cli("scheduled", "--config", str(path), "--apply", factory=self.factory())
            self.assertEqual((code, json.loads(out)["stopped"]), (0, "writer_busy"))
            code, out = self.cli("scheduled", "--config", str(path), "--apply", factory=self.factory())
            self.assertEqual((code, json.loads(out)["alerts"]), (3, ["writer_busy_again"]))
            self.assertEqual(self.book.saves, [])
        finally:
            held.release()
        seen = []
        real_run = mr.RemovalsEngine.run

        def run(engine, *args, **kwargs):
            with self.assertRaises(dl.DirectLocalError):
                dl._OwnerLock(move_root / tm.LOCK_FILE)  # held by this run
            seen.append(True)
            return real_run(engine, *args, **kwargs)
        with mock.patch.object(mr.RemovalsEngine, "run", run):
            code, out = self.cli("scheduled", "--config", str(path), "--apply", factory=self.factory())
        self.assertEqual((code, seen), (0, [True]))
        self.assertEqual(len(self.book.saves), 1)

    def test_state_root_never_shared(self):
        path = self.write_configs(state_root=str(self.core_root))
        code, out = self.cli("init", "--config", str(path))
        self.assertEqual(json.loads(out)["code"], "removals_state_root_shared")


if __name__ == "__main__":
    unittest.main()
