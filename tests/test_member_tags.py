"""#268 slice C1: the Shopify tagging tool ``xb_member_tags`` (tag
``member-qualified``).

No PowerShell, AutoCount, Shopify or network: the engine runs against
``FakeShopify`` (customer nodes in Shopify's documented 2026-10 shapes, and the
real Shopify client over ``FakeTransport`` where the HTTP layer matters) and
``FakeBook`` (the type move bridge's two reads). The two input ledgers are the
real tools' own ledgers: the #155 sync ledger made by ``sync.SyncLedger`` and
the member core v2 ledger made by ``bm.BenefitsLedger``, with links voided
through the member core's own ``unlink``. Fake data only: example.com, a
synthetic store, numbers built in code.

Every test ends with the privacy base check (``assert_outputs_private``, as the
member core's): every CLI output and every stored run summary carries codes,
counts, keyed refs and masked MemberNos only, with exactly the keys its command
allows.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
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
import xb_member_benefits as bm  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_member_tags as mt  # noqa: E402
import xb_member_type_move as tm  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402

STORE_GID = "gid://shopify/Shop/1"
SHOP = "fake-store.synthetic.invalid"
TAG = "member-qualified"
# Thu 19 Nov 2026, 12:00 Singapore.
NOW = datetime(2026, 11, 19, 4, 0, tzinfo=timezone.utc)
FUTURE = "2027-07-08T00:00:00"
PAST = "2026-11-18T00:00:00"
TODAY = "2026-11-19T00:00:00"
LM = "2026-10-01T09:00:00.000"


def local(n: int) -> str:
    """Eight fake Singapore digits, built in code (never a literal number)."""
    return "9" + f"{n:07d}"


def sg(n: int) -> str:
    return "65" + local(n)


def customer_number(n: int) -> int:
    """A fake customer ID of 16 digits, built in code (as the member core's tests)."""
    return 10 ** 15 + n


def customer_gid(n: int) -> str:
    return f"gid://shopify/Customer/{customer_number(n)}"


def raw_config(root: Path, sync_root: Path, core_root: Path, **overrides) -> dict:
    value = {
        "schema": mt.CONFIG_SCHEMA, "state_root": str(root), "autocount_book": "AED_TESTINGAPI",
        "member_sync_state_root": str(sync_root), "member_core_state_root": str(core_root),
        "shopify": {"store_gid": STORE_GID, "shop_domain": SHOP, "api_version": "2026-10",
                    "auth": {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_TAGS_CLIENT_ID",
                             "client_secret_env": "XB_SHOPIFY_TAGS_CLIENT_SECRET"}},
        "tags_write_enabled": True,
    }
    value.update(overrides)
    return value


@contextlib.contextmanager
def private_root_allowed(elsewhere: Path):
    """state_root() refuses %LOCALAPPDATA% (where temp dirs live on Windows)
    and needs a protected ACL; both are proven in the #155 tests."""
    with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(elsewhere)}), mock.patch.object(dl, "verify_private_state", lambda root: None):
        yield


class Base(unittest.TestCase):
    """Temp roots for this tool and the two input tools, the fakes, and the
    privacy base check after every test."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-tags-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.root = self.temp / "state"
        self.root.mkdir()
        self.sync_root = self.temp / "sync"
        self.sync_root.mkdir()
        self.core_root = self.temp / "core"
        self.core_root.mkdir()
        self.now = NOW
        self.private: set[str] = set()
        self.emitted: list[dict] = []
        real_emit = mt._emit

        def tee(value):
            self.emitted.append(json.loads(json.dumps(value)))
            real_emit(value)
        patcher = mock.patch.object(mt, "_emit", tee)
        patcher.start()
        self.addCleanup(patcher.stop)

    # The privacy base check of tests/test_member_benefits.py (G2 B7, slice B1
    # T2-T5), copied for this tool: every pattern ends with \Z.
    PRIVATE_REF_RE = re.compile(r"^[cm]-[0-9a-f]{20}\Z")
    PRIVATE_MASK_RE = re.compile(r"^\*{3,}[^*]{1,3}\Z")
    PRIVATE_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")
    PRIVATE_WORD_RE = re.compile(r"^[a-z0-9_:]{1,64}\Z")
    PRIVATE_DIGITS_RE = re.compile(r"[0-9]{4}")
    BRIDGE_PREFIX = "bridge_read_failed:"
    BRIDGE_TAIL_RE = re.compile(r":x(?:[0-9a-f]{8}|none):l[0-9]\Z")
    # The allowed top-level keys of every output, per command (required,
    # optional). Written out here, never taken from the tool.
    CONFIG_KEYS = frozenset({
        "status", "autocount_book", "api_version", "scope_sets", "tag", "tags_write_enabled", "max_tag_adds_per_run",
        "max_tag_removes_per_run", "max_member_reads_per_run", "shopify_max_carrier_pages", "ac2_credential", "ac2_login_mode"})
    RUN_KEYS = frozenset({"status", "command", "run_id", "counts", "alerts", "refs", "stopped", "scope_set", "query_costs",
                          "tagged_now", "carriers_total"})
    OUTPUT_KEYS = {
        "observe": RUN_KEYS,
        "apply": RUN_KEYS,
        "status": frozenset({"status", "command", "tag_writes", "open_writes", "reviews", "runs", "stop_file_present", "tag_set"}),
        "digest": frozenset({"status", "command", "hours", "runs", "counts", "alerts", "alert_refs", "open", "shadow",
                             "tagged_now", "carriers_total"}),
    }
    DIGEST_PART_KEYS = {"runs": frozenset({"completed", "unfinished"}), "open": frozenset({"uncertain_writes", "intent_writes"}),
                        "shadow": frozenset({"would_tag", "would_untag"})}
    QUERY_NAMES = frozenset({"XbMemberTagsCustomers", "XbMemberTagsCarriers"})
    SCOPE_SET_NAMES = ("write_only", "read_write")
    WRITE_STATES = ("INTENT", "DONE", "REJECTED", "NOT_WRITTEN", "UNCERTAIN")

    def tearDown(self):
        self.assert_outputs_private()

    def assert_outputs_private(self) -> None:
        """After every test: every CLI output and every stored run summary
        carries keyed refs and masked MemberNos only; no customer GID, no full
        MemberNo; every count key, alert code, ref code and ``stopped`` text is
        a code word with no run of four digits; the keys are exactly the ones
        the command allows."""

        outputs = [("cli", value) for value in self.emitted]
        private = set(self.private)
        path = self.root / mt.LEDGER_FILE
        if path.is_file():
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if "runs" in tables:
                    outputs += [("run", json.loads(r[0])) for r in conn.execute("SELECT summary FROM runs WHERE summary IS NOT NULL")]
                if "tag_writes" in tables:
                    for sql in ("SELECT customer_gid FROM tag_writes", "SELECT member_no FROM tag_writes"):
                        private |= {r[0] for r in conn.execute(sql) if r[0]}
            finally:
                conn.close()
        private = {value for value in private if len(value) >= 6}
        for kind, value in outputs:
            text = json.dumps(value)
            self.assertNotIn("gid://", text, kind)
            for secret in private:
                self.assertNotIn(secret, text, kind)
                self.assertNotIn(secret[-7:], text, kind)  # no tail longer than 3 either
            self.assertIsInstance(value, dict, kind)
            entries = [e for key in ("refs", "alert_refs") for e in (value.get(key) or [])]
            for entry in entries:
                self.assertLessEqual(set(entry), {"ref", "code", "member_no_masked"}, kind)
                self.assertRegex(entry["ref"], self.PRIVATE_REF_RE, kind)
                if "member_no_masked" in entry:
                    self.assertRegex(entry["member_no_masked"], self.PRIVATE_MASK_RE, kind)
            words = self.output_shape_words(value, kind)
            words += list(value.get("counts") or []) + list(value.get("alerts") or []) + [entry.get("code") for entry in entries]
            if value.get("stopped") is not None:
                words.append(value["stopped"])
            for word in words:
                self.assertIsInstance(word, str, kind)
                self.assertRegex(word, self.PRIVATE_WORD_RE, kind)
                rest = self.BRIDGE_TAIL_RE.sub("", word) if word.startswith(self.BRIDGE_PREFIX) else word
                self.assertNotRegex(rest, self.PRIVATE_DIGITS_RE, kind)

    def output_shape_words(self, value: dict, kind: str) -> list:
        def whole(number) -> bool:
            return type(number) is int

        keys = set(value)
        words: list = []
        if value.get("status") == "refused":
            self.assertEqual(keys, {"status", "code"}, kind)
            words.append(value["code"])
        elif "command" in value:
            self.assertIn(value["command"], self.OUTPUT_KEYS, kind)
            self.assertEqual(keys, set(self.OUTPUT_KEYS[value["command"]]), kind)
            if value["command"] in ("observe", "apply"):
                self.assertIsInstance(value["alerts"], list, kind)
                self.assertTrue(value["scope_set"] is None or value["scope_set"] in self.SCOPE_SET_NAMES, kind)
                self.assertIsInstance(value["query_costs"], dict, kind)
                self.assertLessEqual(set(value["query_costs"]), self.QUERY_NAMES, kind)
                for cost in value["query_costs"].values():
                    self.assertTrue(cost is None or (whole(cost) and 0 <= cost <= 1_000_000), kind)
            if value["command"] in ("observe", "apply", "digest"):
                self.assertTrue(whole(value["tagged_now"]) or value["tagged_now"] is None, kind)
                self.assertTrue(whole(value["carriers_total"]) or value["carriers_total"] is None, kind)
            if value["command"] == "digest":
                for part, allowed in self.DIGEST_PART_KEYS.items():
                    self.assertEqual(set(value[part]), set(allowed), kind)
                    self.assertTrue(all(whole(n) for n in value[part].values()), kind)
                self.assertIsInstance(value["alerts"], dict, kind)
                self.assertTrue(all(whole(n) for n in value["alerts"].values()), kind)
                words += list(value["alerts"])
                self.assertTrue(whole(value["hours"]), kind)
            if value["command"] == "status":
                self.assertEqual(value["tag_set"], ",".join(mt.TAG_SET), kind)
                for key, number in value["tag_writes"].items():
                    self.assertRegex(key, r"^(add|remove):(" + "|".join(self.WRITE_STATES) + r")\Z", kind)
                    self.assertTrue(whole(number), kind)
                for key in ("open_writes", "reviews", "runs"):
                    self.assertTrue(whole(value[key]), kind)
                self.assertIs(type(value["stop_file_present"]), bool, kind)
        elif keys == {"status"}:
            self.assertIn(value["status"], ("initialised", "existing"), kind)
        else:
            self.assertEqual(keys, set(self.CONFIG_KEYS), kind)
            self.assertEqual(value["status"], "ok", kind)
        if "counts" in value:
            self.assertIsInstance(value["counts"], dict, kind)
            self.assertTrue(all(whole(n) for n in value["counts"].values()), kind)
        return words

    # -- helpers -------------------------------------------------------------
    def cfg(self, **overrides) -> mt.TagsConfig:
        return mt.parse_config(raw_config(self.root, self.sync_root, self.core_root, **overrides))

    def code(self, call) -> str:
        with self.assertRaises(dl.DirectLocalError) as caught:
            call()
        return caught.exception.code

    def write_config(self, **overrides) -> Path:
        path = self.temp / "member_tags.json"
        path.write_text(json.dumps(raw_config(self.root, self.sync_root, self.core_root, **overrides)), encoding="utf-8")
        return path


# --------------------------------------------------------------------------
# Constants, pinned literals, config
# --------------------------------------------------------------------------

class ConstantTests(Base):
    def test_tag_set_and_active_tags_are_pinned_exactly(self):
        self.assertEqual(mt.TAG_SET, ("member-qualified", "member-bday-month"))
        self.assertEqual(mt.ACTIVE_TAGS, ("member-qualified",))
        self.assertEqual(mt.TAG, "member-qualified")
        self.assertLessEqual(set(mt.ACTIVE_TAGS), set(mt.TAG_SET))

    def test_input_ledger_literals_equal_the_tools_own_constants(self):
        # A version bump in either tool fails here (the merge-with-main test) instead of being misread.
        self.assertEqual(mt.MEMBER_CORE_LEDGER_FILE, bm.LEDGER_FILE)
        self.assertEqual(mt.MEMBER_CORE_TOOL, bm.TOOL)
        self.assertEqual(mt.MEMBER_CORE_LEDGER_VERSIONS, (bm.SCHEMA_VERSION,))
        self.assertEqual(mt.MEMBER_CORE_CONFIG_SCHEMA, bm.CONFIG_SCHEMA)
        self.assertEqual(mt.SYNC_LEDGER_FILE, sync.LEDGER_FILE)
        self.assertEqual(mt.SYNC_LEDGER_VERSIONS, (sync.SCHEMA_VERSION,))
        self.assertEqual(mt.TYPE_MOVE_LEDGER_FILE, tm.LEDGER_FILE)
        self.assertEqual(mt.SCAN_TYPES, ("Default", "MEMBER", "GUEST"))
        self.assertEqual(mt.SCOPE_SETS, {frozenset({"write_customers"}): "write_only",
                                         frozenset({"read_customers", "write_customers"}): "read_write"})

    def test_the_module_does_not_import_the_member_core(self):
        source = (ROOT / "directlocal" / "xb_member_tags.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(import|from)\s+xb_member_benefits\b")
        self.assertNotIn("import xb_member_benefits", source)


class ConfigTests(Base):
    def test_defaults_are_observe_and_the_pilot_caps(self):
        value = raw_config(self.root, self.sync_root, self.core_root)
        del value["tags_write_enabled"]
        config = mt.parse_config(value)
        self.assertFalse(config.tags_write_enabled)
        self.assertEqual((config.max_tag_adds_per_run, config.max_tag_removes_per_run, config.max_member_reads_per_run,
                          config.shopify_max_carrier_pages, config.http_timeout_seconds), (10, 5, 20, 60, 30))

    def test_bounds_and_types_are_refused(self):
        cases = {
            "max_tag_adds_per_run": (0, 251, True, "10"), "max_tag_removes_per_run": (0, 51), "max_member_reads_per_run": (0, 101),
            "shopify_max_carrier_pages": (0, 101), "http_timeout_seconds": (4, 61),
        }
        for key, values in cases.items():
            for bad in values:
                with self.subTest(key=key, value=bad):
                    self.assertEqual(self.code(lambda: self.cfg(**{key: bad})), f"tags_config_{key}_invalid")
        for key, good in (("max_tag_adds_per_run", 250), ("max_tag_removes_per_run", 50), ("max_member_reads_per_run", 100)):
            self.assertEqual(getattr(self.cfg(**{key: good}), key), good)
        self.assertEqual(self.code(lambda: self.cfg(tags_write_enabled="yes")), "tags_config_tags_write_enabled_invalid")
        self.assertEqual(self.code(lambda: self.cfg(member_core_state_root="")), "tags_config_member_core_state_root_invalid")
        self.assertEqual(self.code(lambda: self.cfg(extra=1)), "tags_config_keys_invalid")
        value = raw_config(self.root, self.sync_root, self.core_root)
        del value["member_core_state_root"]
        self.assertEqual(self.code(lambda: mt.parse_config(value)), "tags_config_keys_invalid")
        self.assertEqual(self.code(lambda: self.cfg(schema="xb.directlocal.member_tags.config.v2")), "tags_config_schema_invalid")

    def test_another_tools_config_is_refused(self):
        for schema in (sync.CONFIG_SCHEMA, tm.CONFIG_SCHEMA, bm.CONFIG_SCHEMA):
            with self.subTest(schema=schema):
                self.assertEqual(self.code(lambda: self.cfg(schema=schema)), "tags_config_is_other_tool")

    def test_live_book_needs_the_live_credential_and_a_login_mode(self):
        self.assertEqual(self.code(lambda: self.cfg(autocount_book="AED_XBOUNDARIES")), "tags_config_ac2_credential_target_live_required")
        self.assertEqual(self.code(lambda: self.cfg(autocount_book="AED_XBOUNDARIES", ac2_credential_target=sync.LIVE_CREDENTIAL_TARGET)),
                         "tags_config_ac2_login_mode_live_required")
        live = self.cfg(autocount_book="AED_XBOUNDARIES", ac2_credential_target=sync.LIVE_CREDENTIAL_TARGET, ac2_login_mode="owner_shared")
        self.assertEqual(live.autocount_book, "AED_XBOUNDARIES")
        self.assertEqual(self.code(lambda: self.cfg(ac2_credential_target=sync.LIVE_CREDENTIAL_TARGET)), "tags_config_ac2_credential_target_live_only")
        self.assertEqual(self.code(lambda: self.cfg(autocount_book="AED_XBOUNDARIES", ac2_credential_target=sync.LIVE_CREDENTIAL_TARGET,
                                                    ac2_login_mode="dedicated")), "tags_config_ac2_refused_user_ids_live_required")
        self.assertEqual(self.code(lambda: self.cfg(ac2_login_mode="owner_shared", ac2_refused_user_ids=["ADMIN"])),
                         "tags_config_ac2_refused_user_ids_owner_shared_refused")
        self.assertEqual(self.code(lambda: self.cfg(autocount_book="AED_OTHER")), self.code(lambda: sync.check_book("AED_OTHER")))

    def test_an_unqualified_store_is_refused(self):
        shopify = raw_config(self.root, self.sync_root, self.core_root)["shopify"]
        self.assertEqual(self.code(lambda: self.cfg(shopify={**shopify, "shop_domain": "example.myshopify.com"})), "tags_config_store_not_qualified")

    def test_example_configs(self):
        example = json.loads((ROOT / "config" / "member_tags.example.json").read_text(encoding="utf-8"))
        config = mt.parse_config(example)
        self.assertEqual(config.autocount_book, "AED_TESTINGAPI")
        self.assertFalse(config.tags_write_enabled)
        self.assertEqual(config.shopify.auth_env, {"client_id_env": "XB_SHOPIFY_TAGS_CLIENT_ID", "client_secret_env": "XB_SHOPIFY_TAGS_CLIENT_SECRET"})
        live = json.loads((ROOT / "config" / "member_tags.live.example.json").read_text(encoding="utf-8"))
        # Placeholders, never the real store (Controller answer 5): refused as they stand.
        self.assertEqual((live["shopify"]["store_gid"], live["shopify"]["shop_domain"]), ("gid://shopify/Shop/1", "example.myshopify.com"))
        self.assertEqual(self.code(lambda: mt.parse_config(live)), "tags_config_store_not_qualified")
        self.assertFalse(live["tags_write_enabled"])
        self.assertEqual(live["shopify"]["token_cache"], "dpapi_current_user")
        # With the store's two values copied in (as the VM does from the member core's live config) it parses.
        ((gid, domain),) = dl.QUALIFIED_CONNECTOR_STORES.items()
        live["shopify"].update(store_gid=gid, shop_domain=domain)
        config = mt.parse_config(live)
        self.assertEqual((config.autocount_book, config.ac2_credential_target), ("AED_XBOUNDARIES", sync.LIVE_CREDENTIAL_TARGET))
        self.assertEqual(config.max_tag_adds_per_run, 10)

    def test_check_config_prints_the_summary_only(self):
        self.assertEqual(mt.main(["check-config", "--config", str(self.write_config())]), 0)
        self.assertEqual(self.emitted[-1]["status"], "ok")
        self.assertEqual(self.emitted[-1]["scope_sets"], ["read_write", "write_only"])
        self.assertEqual(mt.main(["check-config", "--config", str(self.temp / "missing.json")]), 2)
        self.assertEqual(self.emitted[-1], {"status": "refused", "code": "tags_config_unreadable"})
        self.assertFalse((self.root / mt.LEDGER_FILE).exists())


# --------------------------------------------------------------------------
# Own ledger (design 1.8)
# --------------------------------------------------------------------------

LEDGER_SCHEMA_SHA256 = "62eb3c7fc0c6005169db32ad658a18a3507f13a6f999ef9da4889221ca1628b6"


class LedgerTests(Base):
    def ledger(self) -> mt.TagsLedger:
        ledger = mt.TagsLedger(self.root, clock=lambda: self.now)
        self.addCleanup(ledger.close)
        ledger.init_meta(self.cfg())
        return ledger

    def intent(self, ledger, n=1, op="add") -> str:
        return ledger.add_intent(run_id="run-x", customer_gid=customer_gid(n), tag=TAG, op=op, reason="member_active",
                                 member_no=sg(n), basis="sync155")

    def test_v1_schema_text_is_pinned(self):
        import hashlib
        self.assertEqual(mt.SCHEMA_VERSION, 1)
        self.assertEqual(hashlib.sha256(mt.LEDGER_SCHEMA.encode("utf-8")).hexdigest(), LEDGER_SCHEMA_SHA256)
        ledger = self.ledger()
        self.assertEqual(ledger.conn.execute("PRAGMA user_version").fetchone()[0], 1)
        tables = {r[0] for r in ledger.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"meta", "tag_writes", "runs", "reviews"})
        self.assertEqual(ledger.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(ledger.conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(ledger.conn.execute("PRAGMA recursive_triggers").fetchone()[0], 1)
        self.assertEqual(ledger.conn.execute("PRAGMA trusted_schema").fetchone()[0], 0)
        meta = ledger.conn.execute("SELECT * FROM meta").fetchone()
        self.assertEqual((meta["tool"], meta["tag_set"], meta["autocount_book"]), ("xb268_member_tags", "member-qualified,member-bday-month", "AED_TESTINGAPI"))
        self.assertRegex(meta["ref_key"], r"^[0-9a-f]{64}\Z")

    def test_meta_and_tag_writes_refuse_update_delete_and_replace(self):
        ledger = self.ledger()
        write_id = self.intent(ledger)
        ledger.start_run("observe")
        attempts = [
            "UPDATE meta SET store_gid = 'gid://shopify/Shop/2'", "DELETE FROM meta",
            "INSERT OR REPLACE INTO meta SELECT * FROM meta",
            "DELETE FROM tag_writes",
            f"UPDATE tag_writes SET customer_gid = '{customer_gid(2)}'", "UPDATE tag_writes SET op = 'remove'",
            "UPDATE tag_writes SET tag = 'member-bday-month'", "UPDATE tag_writes SET member_no = 'x'", "UPDATE tag_writes SET basis = 'claim'",
            "UPDATE tag_writes SET reason = 'other'", "UPDATE tag_writes SET run_id = 'run-y'", "UPDATE tag_writes SET created_at = 'x'",
            "INSERT OR REPLACE INTO tag_writes SELECT * FROM tag_writes",
            "DELETE FROM runs", "UPDATE runs SET command = 'apply'",
            "INSERT INTO reviews VALUES ('s', 'c', 't')",
        ]
        for sql in attempts[:-1]:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.DatabaseError):
                    ledger.conn.execute(sql)
        ledger.conn.execute(attempts[-1])
        for sql in ("UPDATE reviews SET code = 'x'", "DELETE FROM reviews"):
            with self.assertRaises(sqlite3.DatabaseError):
                ledger.conn.execute(sql)
        self.assertEqual(ledger.conn.execute("SELECT state FROM tag_writes WHERE write_id = ?", (write_id,)).fetchone()[0], "INTENT")

    def test_checks_on_new_rows(self):
        ledger = self.ledger()
        bad = [
            ("customer_gid", "gid://shopify/Order/1"), ("tag", "member-mg"), ("op", "set"), ("basis", "other"), ("state", "DONE"),
        ]
        for column, value in bad:
            with self.subTest(column=column):
                row = {"write_id": "w", "run_id": "r", "customer_gid": customer_gid(1), "tag": TAG, "op": "add", "reason": "x",
                       "member_no": None, "basis": None, "state": "INTENT", "created_at": "t", "updated_at": "t", column: value}
                with self.assertRaises(sqlite3.DatabaseError):
                    ledger.conn.execute(f"INSERT INTO tag_writes({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM tag_writes").fetchone()[0], 0)

    def test_transitions(self):
        ledger = self.ledger()
        allowed = {"INTENT": ("DONE", "REJECTED", "NOT_WRITTEN", "UNCERTAIN"), "UNCERTAIN": ("DONE", "NOT_WRITTEN")}
        for start in mt.WRITE_STATES:
            for end in mt.WRITE_STATES:
                with self.subTest(start=start, end=end):
                    write_id = self.intent(ledger)
                    path = {"INTENT": [], "UNCERTAIN": ["UNCERTAIN"], "DONE": ["DONE"], "REJECTED": ["REJECTED"], "NOT_WRITTEN": ["NOT_WRITTEN"]}[start]
                    for state in path:
                        ledger.set_outcome(write_id, state, None)
                    if end in allowed.get(start, ()):
                        ledger.set_outcome(write_id, end, "resolved_by_read")
                    else:
                        with self.assertRaises(sqlite3.DatabaseError):
                            ledger.set_outcome(write_id, end, "x")
        # A settled row stays as it is, code included.
        write_id = self.intent(ledger)
        ledger.set_outcome(write_id, "DONE", None)
        with self.assertRaises(sqlite3.DatabaseError):
            ledger.conn.execute("UPDATE tag_writes SET code = 'x' WHERE write_id = ?", (write_id,))

    def test_another_tools_or_versions_ledger_is_refused(self):
        conn = sqlite3.connect(self.root / mt.LEDGER_FILE)
        conn.execute("CREATE TABLE receipts(x)")
        conn.execute("PRAGMA user_version=2")
        conn.close()
        self.assertEqual(self.code(lambda: mt.TagsLedger(self.root)), "tags_ledger_not_recognised")
        (self.root / mt.LEDGER_FILE).write_bytes(b"not a database at all, just text" * 10)
        self.assertEqual(self.code(lambda: mt.TagsLedger(self.root)), "tags_ledger_not_recognised")
        # The lock was released after each refusal.
        (self.root / mt.LEDGER_FILE).unlink()
        ledger = mt.TagsLedger(self.root)
        ledger.close()

    def test_binding_is_checked_at_every_open(self):
        ledger = self.ledger()
        other = raw_config(self.root, self.sync_root, self.core_root)
        other["shopify"] = {**other["shopify"], "store_gid": "gid://shopify/Shop/2"}
        self.assertEqual(self.code(lambda: ledger.check_binding(mt.parse_config(other))), "tags_ledger_binding_mismatch")
        self.assertEqual(self.code(lambda: ledger.init_meta(mt.parse_config(other))), "tags_ledger_binding_mismatch")
        self.assertEqual(self.code(lambda: ledger.check_binding(self.cfg(autocount_book="AED_XBOUNDARIES", ac2_credential_target=sync.LIVE_CREDENTIAL_TARGET,
                                                                        ac2_login_mode="owner_shared"))), "tags_ledger_binding_mismatch")
        self.assertEqual(ledger.init_meta(self.cfg()), {"status": "existing"})

    def test_a_second_runner_is_refused(self):
        self.ledger()
        self.assertEqual(self.code(lambda: mt.TagsLedger(self.root)), "ledger_locked_by_another_owner")

    def test_review_once(self):
        ledger = self.ledger()
        self.assertTrue(ledger.review_once("s1", "flow_dates_guard"))
        self.assertFalse(ledger.review_once("s1", "flow_dates_guard"))


class StateRootTests(Base):
    def test_own_root_never_another_tools_or_an_input_root(self):
        with private_root_allowed(self.temp / "elsewhere"):
            self.assertEqual(mt.state_root(self.cfg()), self.root.resolve())
            for name in (sync.LEDGER_FILE, bm.LEDGER_FILE, tm.LEDGER_FILE, "ledger.sqlite3"):
                with self.subTest(name=name):
                    (self.root / name).write_bytes(b"")
                    self.assertEqual(self.code(lambda: mt.state_root(self.cfg())), "tags_state_root_shared")
                    (self.root / name).unlink()
            self.assertEqual(self.code(lambda: mt.state_root(self.cfg(member_sync_state_root=str(self.root)))), "tags_state_root_shared")
            self.assertEqual(self.code(lambda: mt.state_root(self.cfg(member_core_state_root=str(self.root)))), "tags_state_root_shared")
            self.assertEqual(self.code(lambda: mt.state_root(self.cfg(member_core_state_root=str(self.sync_root)))), "tags_state_root_shared")


class LedgerCliTests(Base):
    def run_main(self, *args, **overrides) -> int:
        with private_root_allowed(self.temp / "elsewhere"):
            return mt.main([*args, "--config", str(self.write_config(**overrides))], clock=lambda: self.now)

    def test_init_then_status(self):
        self.assertEqual(self.run_main("status"), 2)
        self.assertEqual(self.emitted[-1], {"status": "refused", "code": "tags_ledger_not_initialised"})
        self.assertEqual(self.run_main("init"), 0)
        self.assertEqual(self.emitted[-1], {"status": "initialised"})
        self.assertEqual(self.run_main("init"), 0)
        self.assertEqual(self.emitted[-1], {"status": "existing"})
        self.assertEqual(self.run_main("status"), 0)
        self.assertEqual(self.emitted[-1]["tag_writes"], {})
        self.assertFalse(self.emitted[-1]["stop_file_present"])
        (self.root / mt.STOP_FILE).write_text("stop", encoding="ascii")
        self.run_main("status")
        self.assertTrue(self.emitted[-1]["stop_file_present"])


if __name__ == "__main__":
    unittest.main()
