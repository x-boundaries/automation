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


class FakeBook:
    """The type move bridge's two reads on a fake book: ``type-move-scan``
    (MemberNo, IsActive, ExpiryDate, LastModified; TOP 500 by MemberNo) and
    ``type-move-member`` (found, member_type). ``hidden`` members are missed
    by every scan (a member saved between the scans)."""

    def __init__(self):
        self.members: dict[str, dict] = {}
        self.hidden: set[str] = set()
        self.scans: list[tuple[str, str]] = []
        self.single_reads: list[str] = []
        self.fail_scan: str | None = None
        self.fail_member: str | None = None

    def add(self, member_no: str, member_type: str = "MEMBER", active="T", expiry=FUTURE) -> str:
        self.members[member_no] = {"type": member_type, "active": active, "expiry": expiry}
        return member_no

    def scan(self, member_type, after):
        assert member_type in tm.SCAN_TYPES
        self.scans.append((member_type, after))
        if self.fail_scan:
            raise dl.BridgeError(self.fail_scan)
        rows = sorted((no, m) for no, m in self.members.items() if m["type"] == member_type and no > after and no not in self.hidden)
        return [{"member_no": no, "is_active": m["active"], "expiry_local": m["expiry"], "last_modified_local": LM}
                for no, m in rows[:tm.SCAN_PAGE_SIZE]]

    def member(self, member_no, salt):
        self.single_reads.append(member_no)
        if self.fail_member:
            raise dl.BridgeError(self.fail_member)
        m = self.members.get(member_no)
        if m is None:
            return {"found": False}
        return {"found": True, "member_type": m["type"], "fields_digest": "sha256:" + "0" * 64}


class FakeShopify:
    """The tagging app over customer fixtures: session, by-ID reads, the tag
    search and the two mutations. ``answers[gid]`` scripts a mutation answer:
    ``done`` (default), ``rejected``, ``budget`` (throttled, not executed),
    ``uncertain_applied``, ``uncertain_unapplied`` or ``crash_applied`` (the
    process dies after Shopify applied it)."""

    def __init__(self):
        self.customers_by_gid: dict[str, dict] = {}
        self.scopes = ["write_customers"]
        self.calls: list[tuple] = []
        self.writes: list[tuple[str, str, list]] = []
        self.answers: dict[str, list[str]] = {}
        self.fail_customers: list[str | None] = []
        self.carrier_cap = False
        self.search_lag: set[str] = set()
        self.after_write = None
        self.query_costs = {"XbMemberTagsCustomers": 200, "XbMemberTagsCarriers": 102}

    def add(self, n: int, tags=(), registration: bool = True) -> str:
        self.customers_by_gid[customer_gid(n)] = {"tags": list(tags), "registration": registration}
        return customer_gid(n)

    def tags(self, n: int) -> list:
        return self.customers_by_gid[customer_gid(n)]["tags"]

    def verify_session(self, *, store_gid):
        self.calls.append(("session",))
        if frozenset(self.scopes) not in mt.SCOPE_SETS:
            raise direct.ShopifyDirectError("shopify_token_scope_not_expected")
        return mt.SCOPE_SETS[frozenset(self.scopes)]

    def customers(self, gids):
        self.calls.append(("customers", tuple(sorted(gids))))
        if self.fail_customers:
            code = self.fail_customers.pop(0)
            if code:
                raise direct.ShopifyDirectError(code)
        found = {}
        for gid in gids:
            c = self.customers_by_gid.get(gid)
            found[gid] = None if c is None else mt.CustomerState(gid, tuple(c["tags"]), c["registration"])
        return found

    def carriers(self, max_pages):
        self.calls.append(("carriers",))
        return {g for g, c in self.customers_by_gid.items() if TAG in c["tags"] and g not in self.search_lag}, not self.carrier_cap

    def set_tag(self, op, gid, tags):
        mt.check_tag_variables(gid, tags)  # as the real client, before any HTTP call
        self.writes.append((op, gid, list(tags)))
        answer = (self.answers.get(gid) or ["done"]).pop(0) if self.answers.get(gid) else "done"
        customer = self.customers_by_gid.get(gid)

        def effect():
            if op == "add" and TAG not in customer["tags"]:
                customer["tags"].append(TAG)
            if op == "remove" and TAG in customer["tags"]:
                customer["tags"].remove(TAG)
        if self.after_write is not None:
            self.after_write()
        if answer == "done":
            effect()
            return "DONE", None
        if answer == "rejected":
            return "REJECTED", "shopify_user_error"
        if answer == "budget":
            return "NOT_WRITTEN", "shopify_retry_budget_exhausted"
        if answer == "uncertain_applied":
            effect()
            return "UNCERTAIN", "shopify_transport_failed"
        if answer == "uncertain_unapplied":
            return "UNCERTAIN", "shopify_http_server_error"
        if answer == "crash_applied":
            effect()
            raise KeyboardInterrupt  # the process dies after INTENT, before the answer is recorded
        raise AssertionError(answer)


class FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self.stdout_bytes = stdout
        self.returncode = returncode
        self.pid = 4242

    def communicate(self, data=None, timeout=None):
        return self.stdout_bytes, b""

    def kill(self):
        pass


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

    # -- the two input ledgers, made by the tools' own code (fake data) -----
    def make_sync(self, links=(), *, book="AED_TESTINGAPI", store=STORE_GID, shop=SHOP) -> Path:
        """The #155 sync ledger (sync.SyncLedger, schema v3) with ``links``
        of ``(n, member_no)``."""
        ledger = sync.SyncLedger(self.sync_root, check_custody=False)
        try:
            if ledger.conn.execute("SELECT 1 FROM meta").fetchone() is None:
                ledger.init_meta(autocount_book=book, store_gid=store, shop_domain=shop)
            with ledger.tx() as conn:
                for n, member_no in links:
                    ledger.add_link(conn, customer_gid(n), member_no, "matched", "test")
                    self.private |= {customer_gid(n), member_no}
        finally:
            ledger.close()
        return self.sync_root / sync.LEDGER_FILE

    def core_config(self, store=STORE_GID) -> bm.BenefitsConfig:
        return bm.parse_config({
            "schema": bm.CONFIG_SCHEMA, "state_root": str(self.core_root), "autocount_book": "AED_TESTINGAPI",
            "orders_start_utc": "2026-11-15T00:00:00Z", "member_sync_state_root": str(self.sync_root),
            "shopify": {"store_gid": store, "shop_domain": SHOP, "api_version": "2026-10",
                        "auth": {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_BENEFITS_CLIENT_ID",
                                 "client_secret_env": "XB_SHOPIFY_BENEFITS_CLIENT_SECRET"}}})

    def make_core(self, claims=(), unlinks=(), *, store=STORE_GID) -> Path:
        """The member core ledger v2 (bm.BenefitsLedger) with active claim
        links ``(n, member_no)``, then the claim links of customers ``unlinks``
        voided through the member core's own unlink (reason wrong_person)."""
        ledger = bm.BenefitsLedger(self.core_root, clock=lambda: self.now)
        try:
            config = self.core_config(store)
            ledger.init_meta(config)
            for n, member_no in claims:
                ledger.add_claim(customer_gid(n), member_no, "index", "test")
                self.private |= {customer_gid(n), member_no}
            engine = bm.BenefitsEngine(config, ledger, shopify=None, ac2=None)
            for n in unlinks:
                link = ledger.claim_for_gid(customer_gid(n))
                result = engine.unlink(customer_ref=ledger.ref("c", customer_gid(n)), member_last3=link["member_no"][-3:],
                                       reason="wrong_person", accept_applied=False, apply=True)
                self.assertEqual(result["counts"], {"claim_unlinked": 1})
        finally:
            ledger.close()
        return self.core_root / bm.LEDGER_FILE

    def links(self) -> mt.LinkSet:
        config = self.cfg()
        return mt.build_links(mt.read_sync_links(config), *mt.read_member_core(config))


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


# --------------------------------------------------------------------------
# Inputs (design 1.1) and the G2 M2 rule
# --------------------------------------------------------------------------

def set_user_version(path: Path, version: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={version}")
    conn.close()


class InputLedgerTests(Base):
    def test_the_eight_refusals(self):
        readers = {"sync_ledger": (lambda: mt.read_sync_links(self.cfg()), self.make_sync, self.sync_root / sync.LEDGER_FILE),
                   "member_core_ledger": (lambda: mt.read_member_core(self.cfg()), self.make_core, self.core_root / bm.LEDGER_FILE)}
        for prefix, (read, make, path) in readers.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(self.code(read), f"{prefix}_missing")
                path.write_bytes(b"this is not an SQLite database file" * 20)
                self.assertEqual(self.code(read), f"{prefix}_unreadable")
                path.unlink()
                make(store="gid://shopify/Shop/2")
                self.assertEqual(self.code(read), f"{prefix}_binding_mismatch")
                for name in os.listdir(path.parent):
                    os.remove(path.parent / name)
                make()
                read()
                for version in (0, 1, 2, 3, 4):
                    expected = (version in mt.SYNC_LEDGER_VERSIONS) if prefix == "sync_ledger" else (version in mt.MEMBER_CORE_LEDGER_VERSIONS)
                    set_user_version(path, version)
                    if expected:
                        read()
                    else:
                        self.assertEqual(self.code(read), f"{prefix}_version_unknown", version)

    def test_member_core_ledger_of_another_tool_is_refused(self):
        # A sync ledger copied into the member core root under the member core's name.
        self.make_sync([(1, sg(1))])
        shutil.copyfile(self.sync_root / sync.LEDGER_FILE, self.core_root / bm.LEDGER_FILE)
        set_user_version(self.core_root / bm.LEDGER_FILE, 2)
        # Its meta has no tool column: the read fails closed.
        self.assertEqual(self.code(lambda: mt.read_member_core(self.cfg())), "member_core_ledger_unreadable")

    def test_both_ledgers_unchanged_after_a_read_and_writes_through_the_reader_fail(self):
        sync_path = self.make_sync([(1, sg(1))])
        core_path = self.make_core([(2, sg(2)), (3, sg(3))], unlinks=[3])
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (sync_path, core_path)}
        links = self.links()
        self.assertEqual(set(links.basis), {customer_gid(1), customer_gid(2)})
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (sync_path, core_path)}, before)
        for path, sql in ((sync_path, "DELETE FROM links"), (core_path, "INSERT INTO claim_unlinks VALUES (1, 'other', 'r', 't')")):
            conn = mt.open_read_only(path)
            try:
                with self.assertRaises(sqlite3.Error):
                    conn.execute(sql)
            finally:
                conn.close()
        self.assertEqual({p: p.read_bytes() for p in (sync_path, core_path)}, {p: v[0] for p, v in before.items()})

    def test_static_only_the_voided_pairs_join_reads_raw_claim_links(self):
        source = (ROOT / "directlocal" / "xb_member_tags.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("FROM claim_links"), 1)
        self.assertIn("FROM claim_links c JOIN claim_unlinks u ON u.link_id = c.link_id", mt.VOIDED_PAIRS_SQL)
        self.assertIn("FROM active_claim_links", mt.ACTIVE_CLAIMS_SQL)
        self.assertEqual(source.count("FROM active_claim_links"), 1)

    def test_a_pair_in_both_sources_is_one_clean_link(self):
        self.make_sync([(1, sg(1)), (2, sg(2))])
        self.make_core([(1, " " + sg(1).upper()), (3, sg(3))])
        links = self.links()
        self.assertEqual(links.conflicts, {})
        self.assertEqual(links.basis, {customer_gid(1): (sg(1), "sync155"), customer_gid(2): (sg(2), "sync155"),
                                       customer_gid(3): (sg(3), "claim")})

    def test_the_three_conflict_codes(self):
        # 1 -> two members (sync and claim): customer conflict. 2 and 3 -> one member: member conflict, both left alone.
        self.make_sync([(1, sg(1)), (2, sg(20))])
        self.make_core([(1, sg(11)), (3, sg(20))])
        links = self.links()
        self.assertEqual(links.conflicts, {customer_gid(1): "link_conflict_customer", customer_gid(2): "link_conflict_member",
                                           customer_gid(3): "link_conflict_member"})
        self.assertEqual(links.basis, {})

    def test_m2_p1_a_customer_whose_only_claim_was_voided_has_no_basis(self):
        self.make_core([(1, sg(1))], unlinks=[1])
        self.make_sync()
        links = self.links()
        self.assertEqual(links.basis, {})
        self.assertEqual(links.voided, {customer_gid(1)})
        self.assertEqual(links.conflicts, {})

    def test_m2_p2_a_member_freed_by_an_unlink_is_a_clean_basis_for_its_sync_customer(self):
        self.make_core([(1, sg(5))], unlinks=[1])
        self.make_sync([(2, sg(5))])
        links = self.links()
        self.assertEqual(links.basis, {customer_gid(2): (sg(5), "sync155")})
        self.assertEqual(links.conflicts, {})

    def test_m2_p3_the_voided_pair_reappearing_in_the_sync_is_left_alone(self):
        self.make_core([(1, sg(1))], unlinks=[1])
        self.make_sync([(1, sg(1))])
        links = self.links()
        self.assertEqual(links.conflicts, {customer_gid(1): "link_voided_pair_active_in_sync"})
        self.assertEqual(links.basis, {})

    def test_m2_p4_voided_then_a_newer_active_claim_the_active_one_is_the_basis(self):
        self.make_core([(1, sg(1))], unlinks=[1])
        self.make_core([(1, sg(2))])
        self.make_sync()
        links = self.links()
        self.assertEqual(links.basis, {customer_gid(1): (sg(2), "claim")})
        self.assertEqual(links.conflicts, {})


# --------------------------------------------------------------------------
# AutoCount read (design 1.2)
# --------------------------------------------------------------------------

class ReadBridgeClientTests(unittest.TestCase):
    def client(self, outputs):
        self.started = []
        outputs = list(outputs)

        def popen(command, **kwargs):
            self.started.append(command)
            return FakeProcess(json.dumps(outputs.pop(0)).encode("utf-8"))
        return mt.TagsReadBridgeClient(["powershell.exe", "-File", "bridge.ps1"], autocount_book="AED_TESTINGAPI",
                                       claims=Path(tempfile.gettempdir()) / "xb-tags-unused-claims", popen=popen)

    def test_scan_and_member_are_the_only_reads(self):
        client = self.client([{"members": [{"member_no": "A1", "is_active": "T", "expiry_local": FUTURE}]},
                              {"found": True, "member_type": "GUEST", "fields_digest": "sha256:" + "a" * 64}])
        self.assertEqual(client.scan("MEMBER", "")[0]["member_no"], "A1")
        self.assertEqual(client.member("A1", "salt")["member_type"], "GUEST")
        self.assertEqual([c[3:6] for c in self.started], [["-Operation", "type-move-scan", "-EnableProductionAdapter"],
                                                          ["-Operation", "type-move-member", "-EnableProductionAdapter"]])
        self.assertEqual(self.started[0][-2:], ["-TargetBook", "AED_TESTINGAPI"])

    def test_every_write_and_every_other_operation_is_refused_before_a_child_starts(self):
        client = self.client([])
        refused = {
            "tags_autocount_write_refused": [
                lambda: client.save({"kind": "promote"}, move_id="move-1", deadline=NOW),
                lambda: client._write(["-Operation", "x"], {}, wait_seconds=1, on_started=lambda pid, created: None),
                lambda: client.write_once(fence_id="f", claim_directory=Path("."), deadline=NOW, stdin_value={}, wait_seconds=1,
                                          on_started=lambda pid, created: None),
                lambda: client._start(["-Operation", "type-move-scan", "-EnableProductionAdapter", "-FenceId", "f"]),
            ],
            "tags_operation_not_offered": [
                client.counts, lambda: client.precheck(("x",), None), client.next_member_no, lambda: client.probe("A1"),
                lambda: client.reconcile("A1", "{}"), client.book_check, client.pos_sync_status,
                lambda: client._read("benefit-member", {}),
            ],
        }
        for code, calls in refused.items():
            for call in calls:
                with self.subTest(code=code):
                    with self.assertRaises(dl.DirectLocalError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.started, [])

    def test_static_no_autocount_save_operation_text_in_the_module(self):
        source = (ROOT / "directlocal" / "xb_member_tags.py").read_text(encoding="utf-8")
        for text in ("type-move-save", "benefit-save", "write-once", "SaveMember"):
            self.assertNotIn(text, source)
        self.assertEqual(mt.READ_OPERATIONS, ("type-move-scan", "type-move-member"))


class ScanTests(unittest.TestCase):
    def test_each_scan_type_is_read_and_keys_are_normalised(self):
        book = FakeBook()
        book.add("M1"), book.add("G1", "GUEST"), book.add("D1", "Default"), book.add("V1", "VIP")
        found = mt.scan_members(book)
        self.assertEqual([t for t, _ in book.scans], ["Default", "MEMBER", "GUEST"])
        self.assertEqual({k: [t for t, _ in v] for k, v in found.items()}, {"m1": ["MEMBER"], "g1": ["GUEST"], "d1": ["Default"]})

    def test_paging_and_the_stops(self):
        book = FakeBook()
        for i in range(1001):
            book.add(f"M{i:05d}")
        self.assertEqual(len(mt.scan_type(book, "MEMBER")), 1001)
        self.assertEqual([after for _, after in book.scans], ["", "M00499", "M00999"])

        class Repeating(FakeBook):
            def scan(self, member_type, after):
                return [{"member_no": f"R{i:04d}"} for i in range(tm.SCAN_PAGE_SIZE)]
        with self.assertRaises(dl.DirectLocalError) as caught:
            mt.scan_type(Repeating(), "MEMBER")
        self.assertEqual(caught.exception.code, "tags_scan_order_invalid")

        class Endless(FakeBook):
            def __init__(self):
                super().__init__()
                self.page = 0

            def scan(self, member_type, after):
                self.page += 1
                return [{"member_no": f"E{self.page:04d}-{i:03d}"} for i in range(tm.SCAN_PAGE_SIZE)]
        with self.assertRaises(dl.DirectLocalError) as caught:
            mt.scan_type(Endless(), "MEMBER")
        self.assertEqual(caught.exception.code, "tags_scan_too_many_pages")

    def test_singapore_today_and_the_expiry_rule(self):
        day = datetime(2026, 11, 19).date()
        # 23:59:59 and 00:00:00 Singapore, given as UTC.
        self.assertEqual(mt.sg_today(datetime(2026, 11, 19, 15, 59, 59, tzinfo=timezone.utc)), day)
        self.assertEqual(mt.sg_today(datetime(2026, 11, 18, 16, 0, 0, tzinfo=timezone.utc)), day)
        self.assertEqual(mt.sg_today(datetime(2026, 11, 18, 15, 59, 59, tzinfo=timezone.utc)), day - timedelta(days=1))
        row = lambda active="T", expiry=FUTURE: [("MEMBER", {"is_active": active, "expiry_local": expiry})]  # noqa: E731
        self.assertEqual(mt.member_verdict(row(expiry=TODAY), day), ("on", "member_active"))
        self.assertEqual(mt.member_verdict(row(expiry="2026-11-19T23:59:59.000"), day), ("on", "member_active"))
        self.assertEqual(mt.member_verdict(row(expiry=PAST), day), ("off", "member_expired"))
        for unreadable in (None, "", "2000-01-01T00:00:00", "2019-12-31T00:00:00", "19/11/2026", 20271231):
            self.assertEqual(mt.member_verdict(row(expiry=unreadable), day), ("leave", "expiry_unreadable"), unreadable)
        self.assertEqual(mt.member_verdict(row(expiry="2020-01-01T00:00:00"), day), ("off", "member_expired"))
        self.assertEqual(mt.member_verdict(row(active="F", expiry=FUTURE), day), ("off", "member_inactive"))
        self.assertEqual(mt.member_verdict(row(active="F", expiry=None), day), ("off", "member_inactive"))
        for active in (None, "", "t", "Y", 1):
            self.assertEqual(mt.member_verdict(row(active=active), day), ("leave", "member_active_unreadable"), active)
        for member_type in ("GUEST", "Default"):
            self.assertEqual(mt.member_verdict([(member_type, {"is_active": "T", "expiry_local": FUTURE})], day), ("off", "member_not_member"))
        self.assertEqual(mt.member_verdict(row() + [("GUEST", {})], day), ("leave", "member_reads_conflict"))
        self.assertIsNone(mt.member_verdict([], day))
        self.assertEqual(mt.single_read_verdict({"found": False}), ("leave", "member_not_found"))
        self.assertEqual(mt.single_read_verdict({"found": True, "member_type": "MEMBER"}), ("leave", "member_scan_missed"))
        self.assertEqual(mt.single_read_verdict({"found": True, "member_type": "VIP"}), ("off", "member_not_member"))


# --------------------------------------------------------------------------
# Shopify client (design 1.3, 1.4 answers, 1.5 tag set)
# --------------------------------------------------------------------------

class FakeTransport:
    """``responses``: ``(status, value)`` pairs, or an exception to raise."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.bodies.append(json.loads(body.decode("utf-8")) if url.endswith("graphql.json") else {"token": True})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, value = item
        return direct.HttpResponse(status, {}, json.dumps(value).encode("utf-8"))


def session(scopes, store=STORE_GID, shop=SHOP):
    return 200, {"data": {"shop": {"id": store, "myshopifyDomain": shop},
                          "currentAppInstallation": {"accessScopes": [{"handle": s} for s in scopes]}}}


def cost(requested, available=1990.0):
    return {"cost": {"requestedQueryCost": requested, "actualQueryCost": 4,
                     "throttleStatus": {"maximumAvailable": 2000.0, "currentlyAvailable": available, "restoreRate": 100.0}}}


def node(n, tags=(), registration=True):
    return {"id": customer_gid(n), "tags": list(tags), "registration": {"id": f"gid://shopify/Metafield/{n}"} if registration else None}


def nodes_answer(nodes, requested=None):
    value = {"data": {"nodes": list(nodes)}}
    if requested is not None:
        value["extensions"] = cost(requested)
    return 200, value


def mutation_answer(field, n, tags, errors=()):
    return 200, {"data": {field: {"node": {"id": customer_gid(n), "tags": list(tags)}, "userErrors": list(errors)}}}


THROTTLED_ANSWER = (200, {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                          "extensions": cost(10, available=0.0)})
FORBIDDEN_TAGS = [["member"], ["member-mg"], ["member-legacy"], ["ac2-member"], ["ac2-backfill"], ["shopify-forms-1178308"], ["newsletter"],
                  ["prospect"], ["Member-Qualified"], ["member-qualified "], ["member-qualified,member"], [TAG, "member"], ["member-bday-month"],
                  [], TAG, (TAG,), [None], [1]]


class ShopifyClientTests(unittest.TestCase):
    def client(self, responses, *, mutations=True) -> tuple[mt.TagsShopifyClient, FakeTransport]:
        transport = FakeTransport(responses)
        client = mt.TagsShopifyClient(mutations_enabled=mutations, shop_domain=SHOP, api_version="2026-10",
                                      token_provider=direct.StaticTokenProvider("t" * 32), transport=transport, sleep=lambda s: None)
        return client, transport

    def code(self, call) -> str:
        with self.assertRaises(dl.DirectLocalError) as caught:
            call()
        return caught.exception.code

    def test_the_allowlist_is_exactly_the_five_texts(self):
        self.assertEqual(mt.READ_QUERIES | mt.MUTATION_TEXTS,
                         {direct.SESSION_QUERY, mt.CUSTOMERS_QUERY, mt.CARRIERS_QUERY, mt.TAGS_ADD_MUTATION, mt.TAGS_REMOVE_MUTATION})
        on, _ = self.client([], mutations=True)
        off, _ = self.client([], mutations=False)
        for text in mt.READ_QUERIES:
            self.assertTrue(on._query_allowed(text) and off._query_allowed(text))
        for text in mt.MUTATION_TEXTS:
            self.assertTrue(on._query_allowed(text))
            self.assertFalse(off._query_allowed(text))
        for other in (sync.CUSTOMER_CREATE_MUTATION, bm.CUSTOMER_QUERY, mt.TAGS_ADD_MUTATION + " ", ""):
            self.assertFalse(on._query_allowed(other))

    def test_static_exactly_two_mutation_texts_and_no_other_write(self):
        import ast
        source = (ROOT / "directlocal" / "xb_member_tags.py").read_text(encoding="utf-8")
        texts = [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        mutations = [t for t in texts if t.lstrip().startswith("mutation")]
        self.assertEqual(mutations, [mt.TAGS_ADD_MUTATION, mt.TAGS_REMOVE_MUTATION])
        for word in ("customerCreate", "customerUpdate", "customerDelete", "metafieldsSet", "metafieldDelete", "customerSet",
                     "tagsAdd(id: $id, tags: $tags) { node { ... on Order"):
            self.assertNotIn(word, source)
        self.assertEqual(source.count("tagsAdd("), 1)
        self.assertEqual(source.count("tagsRemove("), 1)

    def test_mutations_refused_before_http_when_off(self):
        client, transport = self.client([], mutations=False)
        for op in ("add", "remove"):
            self.assertEqual(self.code(lambda: client.set_tag(op, customer_gid(1), [TAG])), "shopify_query_not_pinned")
        self.assertEqual(transport.bodies, [])

    def test_every_forbidden_tag_and_bad_id_is_refused_before_http(self):
        client, transport = self.client([])
        for tags in FORBIDDEN_TAGS:
            for op in ("add", "remove"):
                with self.subTest(tags=tags, op=op):
                    self.assertEqual(self.code(lambda: client.set_tag(op, customer_gid(1), tags)), "tags_tag_not_allowed")
        for gid in ("gid://shopify/Order/1", "gid://shopify/Customer/0", customer_gid(1) + " ", None):
            self.assertEqual(self.code(lambda: client.set_tag("add", gid, [TAG])), "tags_customer_gid_invalid")
        self.assertEqual(self.code(lambda: client.set_tag("replace", customer_gid(1), [TAG])), "tags_operation_not_offered")
        self.assertEqual(transport.bodies, [])

    def test_scope_sets(self):
        for scopes, name in ((["write_customers"], "write_only"), (["read_customers", "write_customers"], "read_write"),
                             (["write_customers", "read_customers"], "read_write")):
            client, _ = self.client([session(scopes)])
            self.assertEqual(client.verify_session(store_gid=STORE_GID), name)
        for scopes in ([], ["read_customers"], ["write_customers", "read_orders"], ["read_customers", "write_customers", "write_orders"],
                       ["write_customers", "write_customers"], ["read_customers", "read_orders"], ["write_customers", "read_customers", "read_customers"]):
            with self.subTest(scopes=scopes):
                client, _ = self.client([session(scopes)])
                self.assertEqual(self.code(lambda: client.verify_session(store_gid=STORE_GID)), "shopify_token_scope_not_expected")
        client, _ = self.client([session(["write_customers"], store="gid://shopify/Shop/2")])
        self.assertEqual(self.code(lambda: client.verify_session(store_gid=STORE_GID)), "shopify_store_identity_mismatch")

    def test_token_response_and_cache_scope_sets(self):
        class Protector:
            def protect(self, data, entropy):
                return b"P" + data

            def unprotect(self, blob, entropy):
                return blob[1:]

        temp = Path(tempfile.mkdtemp(prefix="xb-tags-token-"))
        self.addCleanup(shutil.rmtree, temp, True)

        def provider(scope, cache=None):
            transport = FakeTransport([(200, {"access_token": "a" * 32, "expires_in": 3600, "scope": scope})])
            return mt.TagsTokenProvider(SHOP, "c" * 20, "s" * 20, transport=transport, clock=lambda: NOW, cache_protector=cache,
                                        cache_path=temp / mt.TOKEN_CACHE_FILE), transport
        for scope in ("write_customers", "read_customers,write_customers"):
            self.assertEqual(provider(scope)[0].token(), "a" * 32)
        for scope in ("read_customers", "write_customers,read_orders", "read_customers,write_customers,write_orders"):
            with self.subTest(scope=scope):
                self.assertEqual(self.code(lambda: provider(scope)[0].token()), "shopify_token_scope_not_expected")
        # The cache: a token with a pinned set is reused; one with any other set is discarded.
        first, _ = provider("write_customers", Protector())
        first.token()
        again, transport = provider("write_customers", Protector())
        again.token()
        self.assertEqual((again.cache_hits, transport.bodies), (1, []))
        cache = mt.TagsTokenCache(temp / mt.TOKEN_CACHE_FILE, Protector(), shop_domain=SHOP, client_id="c" * 20)
        cache.save("b" * 32, NOW + timedelta(hours=1), frozenset({"write_customers", "read_orders"}))
        self.assertIsNone(cache.load(NOW))
        self.assertEqual(cache.note, "token_cache_unreadable")
        self.assertFalse((temp / mt.TOKEN_CACHE_FILE).exists())

    def test_customers_by_id(self):
        client, transport = self.client([nodes_answer([node(1, [TAG, "member-mg"]), None, {}, node(4, registration=False)], requested=200)])
        found = client.customers([customer_gid(4), customer_gid(1), customer_gid(2), customer_gid(3), customer_gid(1)])
        self.assertEqual(transport.bodies[0]["variables"], {"ids": [customer_gid(1), customer_gid(2), customer_gid(3), customer_gid(4)]})
        self.assertEqual((found[customer_gid(1)].tags, found[customer_gid(1)].has_registration), ((TAG, "member-mg"), True))
        self.assertIsNone(found[customer_gid(2)])
        self.assertIsNone(found[customer_gid(3)])
        self.assertFalse(found[customer_gid(4)].has_registration)
        self.assertEqual(client.query_costs, {"XbMemberTagsCustomers": 200, "XbMemberTagsCarriers": None})
        for bad in ([node(2)], [node(1), node(2)][:1] * 2, [{"id": customer_gid(1), "tags": "x"}], [{"id": customer_gid(1), "tags": [], "registration": "x"}]):
            client, _ = self.client([nodes_answer(bad if len(bad) == 1 else bad)])
            with self.subTest(bad=bad):
                self.assertEqual(self.code(lambda: client.customers([customer_gid(1)] if len(bad) == 1 else [customer_gid(1), customer_gid(2)])),
                                 "shopify_response_shape_invalid")

    def test_customers_are_read_in_pages_of_the_nodes_page(self):
        gids = [customer_gid(n) for n in range(1, mt.NODES_PAGE + 2)]
        client, transport = self.client([nodes_answer([node(n) for n in range(1, mt.NODES_PAGE + 1)]), nodes_answer([node(mt.NODES_PAGE + 1)])])
        self.assertEqual(len(client.customers(gids)), mt.NODES_PAGE + 1)
        self.assertEqual([len(b["variables"]["ids"]) for b in transport.bodies], [mt.NODES_PAGE, 1])

    def test_carriers_search_and_cap(self):
        page = lambda ns, more: (200, {"data": {"customers": {"nodes": [{"id": customer_gid(n), "tags": [TAG]} for n in ns],  # noqa: E731
                                                              "pageInfo": {"hasNextPage": more is not None, "endCursor": more}}},
                                       "extensions": cost(102)})
        client, transport = self.client([page([1, 2], "c1"), page([3], None)])
        self.assertEqual(client.carriers(5), ({customer_gid(1), customer_gid(2), customer_gid(3)}, True))
        self.assertEqual(transport.bodies[0]["variables"], {"first": 100, "after": None, "search": "tag:'member-qualified'"})
        self.assertEqual(transport.bodies[1]["variables"]["after"], "c1")
        self.assertEqual(client.query_costs["XbMemberTagsCarriers"], 102)
        client, _ = self.client([page([1], "c1"), page([2], "c2")])
        self.assertEqual(client.carriers(2), ({customer_gid(1), customer_gid(2)}, False))

    def test_answer_classes(self):
        cases = [
            ("add", [mutation_answer("tagsAdd", 1, ["x", TAG])], ("DONE", None), 1),
            ("remove", [mutation_answer("tagsRemove", 1, ["x"])], ("DONE", None), 1),
            ("add", [mutation_answer("tagsAdd", 1, [TAG], errors=[{"field": ["tags"], "message": "fake"}])], ("REJECTED", "shopify_user_error"), 1),
            ("add", [mutation_answer("tagsAdd", 1, ["x"])], ("UNCERTAIN", "tag_response_unproven"), 1),
            ("remove", [mutation_answer("tagsRemove", 1, [TAG])], ("UNCERTAIN", "tag_response_unproven"), 1),
            ("add", [mutation_answer("tagsAdd", 2, [TAG])], ("UNCERTAIN", "tag_response_unproven"), 1),
            ("add", [(200, {"data": {"tagsAdd": {"node": None, "userErrors": []}}})], ("UNCERTAIN", "tag_response_unproven"), 1),
            ("add", [(200, {"data": {"tagsAdd": {"userErrors": []}}})], ("UNCERTAIN", "tag_response_unproven"), 1),
            ("add", [(200, {"data": {"tagsRemove": {"node": None, "userErrors": []}}})], ("UNCERTAIN", "tag_response_unproven"), 1),
            ("add", [(503, {})], ("UNCERTAIN", "shopify_http_server_error"), 1),
            ("add", [(500, {})], ("UNCERTAIN", "shopify_http_server_error"), 1),
            ("add", [(429, {})], ("UNCERTAIN", "shopify_http_throttled"), 1),
            ("add", [direct.ShopifyDirectError("shopify_transport_failed", retryable=True)], ("UNCERTAIN", "shopify_transport_failed"), 1),
            ("add", [TimeoutError("timed out")], None, 1),
            ("add", [(200, {"errors": [{"message": "x"}]})], ("UNCERTAIN", "shopify_graphql_error"), 1),
            ("add", [THROTTLED_ANSWER, mutation_answer("tagsAdd", 1, [TAG])], ("DONE", None), 2),
            ("add", [THROTTLED_ANSWER] * 4, ("NOT_WRITTEN", "shopify_retry_budget_exhausted"), 4),
            ("add", [THROTTLED_ANSWER, (503, {})], ("UNCERTAIN", "shopify_http_server_error"), 2),
        ]
        for op, responses, expected, requests in cases:
            with self.subTest(op=op, responses=responses):
                client, transport = self.client(list(responses))
                if expected is None:  # a raw transport exception is not a ShopifyDirectError: never swallowed
                    with self.assertRaises(TimeoutError):
                        client.set_tag(op, customer_gid(1), [TAG])
                else:
                    self.assertEqual(client.set_tag(op, customer_gid(1), [TAG]), expected)
                self.assertEqual(len(transport.bodies), requests)
                self.assertEqual(transport.bodies[0]["variables"], {"id": customer_gid(1), "tags": [TAG]})

    def test_a_token_failure_before_sending_is_not_written(self):
        class Failing:
            def token(self):
                raise direct.ShopifyDirectError("shopify_token_request_failed")

            def invalidate(self):
                return False
        transport = FakeTransport([])
        client = mt.TagsShopifyClient(mutations_enabled=True, shop_domain=SHOP, api_version="2026-10", token_provider=Failing(),
                                      transport=transport, sleep=lambda s: None)
        self.assertEqual(client.set_tag("add", customer_gid(1), [TAG]), ("NOT_WRITTEN", "shopify_token_request_failed"))
        self.assertEqual(transport.bodies, [])

    def test_reads_retry_on_a_server_error(self):
        client, transport = self.client([(503, {}), nodes_answer([node(1)])])
        self.assertEqual(set(client.customers([customer_gid(1)])), {customer_gid(1)})
        self.assertEqual(len(transport.bodies), 2)


# --------------------------------------------------------------------------
# The calculated requested cost (design 1.3, shopify.dev rate limits)
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\.\.\.\s*on\s+[A-Za-z_]+|\(([^()]*)\)|([{}])|([A-Za-z_][A-Za-z0-9_]*)(:?)")


def selection(query: str) -> list:
    """The fields of one operation as ``(name, arguments, children)``; an
    inline fragment (``... on Customer``) is transparent; an alias names its field."""

    body = query[query.index("{"):]
    assert not _TOKEN_RE.sub("", body).strip(), "query text this parser does not read"
    tokens = [m.groups() for m in _TOKEN_RE.finditer(body) if not m.group(0).startswith("...")]
    position = 1

    def fields() -> list:
        nonlocal position
        found = []
        while position < len(tokens):
            arguments, brace, name, alias = tokens[position]
            position += 1
            if brace == "}":
                return found
            if brace == "{":  # the fragment's own selection: merge it in
                found += fields()
                continue
            if alias:
                continue
            arguments = ""
            if position < len(tokens) and tokens[position][0] is not None:
                arguments = tokens[position][0]
                position += 1
            children = None
            if position < len(tokens) and tokens[position][1] == "{":
                position += 1
                children = fields()
            found.append((name, arguments, children))
        raise AssertionError("a selection was not closed")

    top = fields()
    assert position == len(tokens)
    return top


def strict_cost(fields: list, sizes: dict) -> int:
    """The member core's strict reading: a scalar 0, an object 1, every
    selected object counted once per row of the list or connection above it;
    a connection is 1 + its other selections + ``first`` rows; a list of IDs
    has one row per ID (``sizes``: argument -> rows)."""

    total = 0
    for name, arguments, children in fields:
        if children is None:
            continue
        rows = None
        for argument, size in sizes.items():
            if re.search(rf"\b{argument}: \$", arguments):
                rows = size
        inner = [child for child in children if child[0] == "nodes"]
        if rows is not None and inner:
            ((_, _, row),) = inner
            total += 1 + strict_cost([c for c in children if c[0] != "nodes"], sizes) + rows * (1 + strict_cost(row, sizes))
        elif rows is not None:
            total += rows * strict_cost(children, sizes) + rows  # each listed node is an object
        else:
            total += 1 + strict_cost(children, sizes)
    return total


class CostTests(unittest.TestCase):
    def test_the_page_sizes_and_the_strict_cost_of_both_queries_are_pinned(self):
        self.assertEqual((mt.NODES_PAGE, mt.CARRIER_PAGE), (100, 100))
        customers = strict_cost(selection(mt.CUSTOMERS_QUERY), {"ids": mt.NODES_PAGE})
        carriers = strict_cost(selection(mt.CARRIERS_QUERY), {"first": mt.CARRIER_PAGE})
        self.assertEqual((customers, carriers), (200, 102))
        self.assertEqual((mt.CUSTOMERS_QUERY_COST, mt.CARRIERS_QUERY_COST), (customers, carriers))
        self.assertLessEqual(max(customers, carriers), mt.COST_CEILING)
        self.assertEqual(mt.MUTATION_COST, 10)
        # 100 IDs is also under Shopify's array input limit of 250.
        self.assertLessEqual(mt.NODES_PAGE, 250)

    def test_the_parser_reads_the_pinned_texts(self):
        self.assertEqual(selection(mt.CUSTOMERS_QUERY),
                         [("nodes", "ids: $ids", [("id", "", None), ("tags", "", None),
                                                  ("metafield", 'namespace: "membership", key: "start_date"', [("id", "", None)])])])
        self.assertEqual([f[0] for f in selection(mt.CARRIERS_QUERY)[0][2]], ["nodes", "pageInfo"])

    def test_whole_points(self):
        self.assertEqual([mt.whole_points(v) for v in (0, 1.2, 200, 1_000_000)], [0, 2, 200, 1_000_000])
        for bad in (None, True, "200", -1, 1_000_001, float("nan")):
            self.assertIsNone(mt.whole_points(bad))


# --------------------------------------------------------------------------
# Engine (design 1.4 to 1.7, 1.10)
# --------------------------------------------------------------------------

class EngineBase(Base):
    def setUp(self):
        super().setUp()
        self.shopify = FakeShopify()
        self.book = FakeBook()
        self.ledger = None
        self.sync_links: list = []
        self.make_sync()
        self.make_core()

    def engine(self, **overrides) -> mt.TagsEngine:
        config = self.cfg(**overrides)
        if self.ledger is None:
            self.ledger = mt.TagsLedger(self.root, clock=lambda: self.now)
            self.addCleanup(self.ledger.close)
            self.ledger.init_meta(config)
        return mt.TagsEngine(config, self.ledger, shopify=self.shopify, ac2=self.book)

    def run_tool(self, apply: bool = True, **overrides) -> dict:
        return self.engine(**overrides).run(apply=apply)

    def member(self, n: int, *, member_type="MEMBER", active="T", expiry=FUTURE, tags=(), registration=True, link="sync",
               in_book=True) -> str:
        """Customer n linked to member sg(n) (by the sync, or by a member core claim)."""
        member_no = sg(n)
        if in_book:
            self.book.add(member_no, member_type, active, expiry)
        if link == "sync":
            self.make_sync([(n, member_no)])
        elif link == "claim":
            self.make_core([(n, member_no)])
        self.shopify.add(n, tags, registration)
        self.private |= {member_no, customer_gid(n)}
        return member_no

    def rows(self, sql="SELECT * FROM tag_writes ORDER BY rowid") -> list:
        conn = sqlite3.connect((self.root / mt.LEDGER_FILE).resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def counts_of(self, table: str) -> int:
        if not (self.root / mt.LEDGER_FILE).exists():
            return 0
        return self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def assert_refused(self, call, code: str, *, http_calls: int = 0) -> None:
        self.assertEqual(self.code(call), code)
        self.assertEqual(self.counts_of("runs"), 0, "no runs row")
        self.assertEqual(self.counts_of("tag_writes"), 0, "no tag_writes row")
        self.assertEqual(len(self.shopify.calls), http_calls, self.shopify.calls)
        self.assertEqual(self.shopify.writes, [])


class DecisionTableTests(EngineBase):
    """Design 1.6: one test per row 1 to 16."""

    def test_row01_conflict_left_alone_alert_once(self):
        self.member(1, tags=[TAG])
        self.make_core([(1, sg(9))])  # the same customer, another member
        self.book.add(sg(9), "GUEST")
        first = self.run_tool()
        self.assertEqual((self.shopify.writes, first["alerts"]), ([], ["link_conflict_customer"]))
        self.assertEqual(first["refs"], [{"ref": self.ledger.ref("c", customer_gid(1)), "code": "link_conflict_customer"}])
        second = self.run_tool()
        self.assertEqual((second["alerts"], second["counts"]["link_conflict_customer"]), ([], 1))

    def test_row01_every_customer_of_a_conflicted_member_is_left_alone(self):
        self.member(1, member_type="GUEST", tags=[TAG])
        self.make_core([(2, sg(1))])
        self.shopify.add(2, [TAG])
        result = self.run_tool()
        self.assertEqual(self.shopify.writes, [])
        self.assertEqual(result["counts"]["link_conflict_member"], 2)

    def test_row01_voided_pair_active_in_sync(self):
        self.make_core([(1, sg(1))], unlinks=[1])
        self.member(1, member_type="GUEST", tags=[TAG])
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["alerts"]), ([], ["link_voided_pair_active_in_sync"]))

    def test_row02_customer_not_found(self):
        self.member(1)
        del self.shopify.customers_by_gid[customer_gid(1)]
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["alerts"]), ([], ["customer_not_found"]))

    def test_row03_tag_case_variant(self):
        for n, variant in ((1, "Member-Qualified"), (2, " member-qualified"), (3, "MEMBER-QUALIFIED")):
            self.member(n, tags=[variant])
        self.member(4, member_type="GUEST", tags=[TAG, "Member-Qualified"])
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["counts"]["tag_case_variant"]), ([], 4))

    def test_row04_on_add_then_kept(self):
        self.member(1)
        self.member(2, tags=[TAG, "newsletter"])
        observed = self.run_tool(apply=False)
        self.assertEqual((self.shopify.writes, observed["counts"]["would_tag"], observed["counts"]["kept_tagged"]), ([], 1, 1))
        self.assertEqual([r["code"] for r in observed["refs"]], ["would_tag"])
        self.assertEqual(observed["refs"][0]["member_no_masked"], mt.mask(sg(1)))
        applied = self.run_tool()
        self.assertEqual(self.shopify.writes, [("add", customer_gid(1), [TAG])])
        self.assertEqual((applied["counts"]["tagged"], applied["tagged_now"], applied["stopped"]), (1, 2, None))
        row = self.rows()[0]
        self.assertEqual((row["op"], row["state"], row["reason"], row["basis"], row["member_no"]), ("add", "DONE", "member_active", "sync155", sg(1)))
        again = self.run_tool()
        self.assertEqual((len(self.shopify.writes), again["counts"]["kept_tagged"]), (1, 2))

    def test_row04_a_claim_link_is_a_basis(self):
        self.member(1, link="claim")
        self.run_tool()
        self.assertEqual(self.rows()[0]["basis"], "claim")

    def test_row05_flow_dates_guard(self):
        self.member(1, tags=["member-mg"], registration=False)
        self.member(2, tags=["member-mg"], registration=True)
        self.member(3, tags=["Member-MG "], registration=False)
        result = self.run_tool()
        self.assertEqual(self.shopify.writes, [("add", customer_gid(2), [TAG])])
        self.assertEqual(result["counts"]["flow_dates_guard"], 2)
        self.assertIn("flow_dates_guard", result["alerts"])

    def test_row06_inactive_off(self):
        self.member(1, active="F", tags=[TAG])
        self.member(2, active="F", expiry=None, tags=[TAG])
        self.member(3, active="F")
        result = self.run_tool()
        self.assertEqual(self.shopify.writes, [("remove", customer_gid(1), [TAG]), ("remove", customer_gid(2), [TAG])])
        self.assertEqual((result["counts"]["untag_reason:member_inactive"], result["counts"]["no_change_untagged"]), (2, 1))
        self.assertEqual([r["reason"] for r in self.rows()], ["member_inactive", "member_inactive"])

    def test_row07_expired_off_today_keeps(self):
        self.member(1, expiry=PAST, tags=[TAG])
        self.member(2, expiry=TODAY, tags=[TAG])
        self.member(3, expiry="2020-01-01T00:00:00", tags=[TAG])
        result = self.run_tool()
        self.assertEqual(self.shopify.writes, [("remove", customer_gid(1), [TAG]), ("remove", customer_gid(3), [TAG])])
        self.assertEqual(result["counts"]["untag_reason:member_expired"], 2)

    def test_row07_singapore_midnight_given_as_utc(self):
        self.member(1, expiry="2026-11-19T00:00:00", tags=[TAG])
        self.now = datetime(2026, 11, 19, 15, 59, 59, tzinfo=timezone.utc)  # 23:59:59 Singapore, 19 Nov
        self.run_tool()
        self.assertEqual(self.shopify.writes, [])
        self.now = datetime(2026, 11, 19, 16, 0, 0, tzinfo=timezone.utc)  # 00:00:00 Singapore, 20 Nov
        self.run_tool()
        self.assertEqual(self.shopify.writes, [("remove", customer_gid(1), [TAG])])

    def test_row08_guest_or_default_off(self):
        self.member(1, member_type="GUEST", tags=[TAG])
        self.member(2, member_type="Default", tags=[TAG])
        result = self.run_tool()
        self.assertEqual([w[0] for w in self.shopify.writes], ["remove", "remove"])
        self.assertEqual(result["counts"]["untag_reason:member_not_member"], 2)

    def test_row09_active_flag_unreadable_left_alone(self):
        self.member(1, active=None, tags=[TAG])
        self.member(2, active="X")
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["counts"]["member_active_unreadable"]), ([], 2))

    def test_row10_expiry_unreadable_left_alone_both_ways(self):
        self.member(1, expiry=None, tags=[TAG])
        self.member(2, expiry="2000-01-01T00:00:00")
        self.member(3, expiry="not a date", tags=[TAG])
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["counts"]["expiry_unreadable"]), ([], 3))
        self.assertEqual(self.shopify.tags(1), [TAG])

    def test_row11_in_no_scan_single_read_not_member_off(self):
        self.member(1, member_type="VIP", tags=[TAG])
        result = self.run_tool()
        self.assertEqual(self.book.single_reads, [sg(1)])
        self.assertEqual(self.shopify.writes, [("remove", customer_gid(1), [TAG])])
        self.assertEqual(result["counts"]["untag_reason:member_not_member"], 1)

    def test_row11a_scan_missed_left_alone(self):
        self.member(1, tags=[TAG])
        self.member(2)
        self.book.hidden |= {sg(1), sg(2)}
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["counts"]["member_scan_missed"]), ([], 2))

    def test_row11b_member_not_found_left_alone(self):
        self.member(1, tags=[TAG], in_book=False)
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["alerts"]), ([], ["member_not_found"]))

    def test_row11c_single_reads_capped(self):
        self.member(1, member_type="VIP", tags=[TAG])
        self.member(2, member_type="VIP", tags=[TAG])
        result = self.run_tool(max_member_reads_per_run=1)
        self.assertEqual(self.book.single_reads, [sg(1)])
        self.assertEqual(self.shopify.writes, [("remove", customer_gid(1), [TAG])])
        self.assertIn("member_reads_capped", result["alerts"])

    def test_row12_member_in_two_scans_left_alone(self):
        member_no = self.member(1, tags=[TAG])
        self.book.add(member_no.lower() + " ", "GUEST")
        self.book.members[member_no.lower() + " "]["type"] = "GUEST"
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["alerts"]), ([], ["member_reads_conflict"]))

    def test_row13_voided_link_carrier_off_m2_p1(self):
        self.make_core([(1, sg(1)), (2, sg(2))], unlinks=[1, 2])
        self.shopify.add(1, [TAG])
        self.shopify.add(2, [])  # not tagged: never tagged
        self.book.add(sg(1)), self.book.add(sg(2))
        self.shopify.search_lag.add(customer_gid(1))  # the decision rests on the by-ID read, not the search
        result = self.run_tool()
        self.assertEqual(self.shopify.writes, [("remove", customer_gid(1), [TAG])])
        row = self.rows()[0]
        self.assertEqual((row["reason"], row["basis"], row["member_no"]), ("link_voided", "voided", None))
        self.assertEqual(result["counts"]["no_change_untagged"], 1)

    def test_row14_tag_without_basis_left_alone(self):
        self.shopify.add(1, [TAG])
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["alerts"], result["carriers_total"]), ([], ["tag_without_basis"], 1))
        self.assertEqual(result["tagged_now"], 0)

    def test_row15_no_change(self):
        self.member(1, tags=[TAG])
        self.member(2, member_type="GUEST")
        result = self.run_tool()
        self.assertEqual(self.shopify.writes, [])
        self.assertEqual((result["counts"]["kept_tagged"], result["counts"]["no_change_untagged"], result["alerts"]), (1, 1, []))

    def test_row16_open_write_that_cannot_be_settled(self):
        self.member(1, member_type="GUEST", tags=[TAG])
        self.engine()
        self.ledger.add_intent(run_id="run-old", customer_gid=customer_gid(1), tag=TAG, op="add", reason="member_active",
                               member_no=sg(1), basis="sync155")
        self.shopify.fail_customers = ["shopify_http_server_error"]
        observed = self.run_tool(apply=False)
        self.assertEqual(observed["alerts"], ["write_unresolved"])
        self.assertEqual(self.rows()[0]["state"], "INTENT")
        runs = self.counts_of("runs")
        self.shopify.fail_customers = ["shopify_http_server_error"]
        self.shopify.calls.clear()
        self.assertEqual(self.code(lambda: self.run_tool()), "tags_uncertain_unresolved")
        self.assertEqual((self.counts_of("runs"), self.shopify.writes, self.rows()[0]["state"]), (runs, [], "INTENT"))


class GuardTests(EngineBase):
    def test_m2_p2_a_freed_member_tags_its_sync_customer(self):
        self.make_core([(1, sg(5))], unlinks=[1])
        self.book.add(sg(5))
        self.make_sync([(2, sg(5))])
        self.shopify.add(1, [])
        self.shopify.add(2, [])
        self.private |= {sg(5)}
        self.run_tool()
        self.assertEqual(self.shopify.writes, [("add", customer_gid(2), [TAG])])

    def test_m2_p4_voided_then_newer_active_claim_is_the_basis(self):
        self.make_core([(1, sg(1))], unlinks=[1])
        self.make_core([(1, sg(2))])
        self.book.add(sg(1), "GUEST")
        self.book.add(sg(2))
        self.shopify.add(1, [])
        self.run_tool()
        self.assertEqual(self.shopify.writes, [("add", customer_gid(1), [TAG])])
        self.assertEqual(self.rows()[0]["member_no"], sg(2))

    def test_observe_never_sends_even_with_the_switch_on(self):
        self.member(1)
        self.member(2, member_type="GUEST", tags=[TAG])
        result = self.run_tool(apply=False, tags_write_enabled=True)
        self.assertEqual(self.shopify.writes, [])
        self.assertEqual((result["counts"]["would_tag"], result["counts"]["would_untag"]), (1, 1))
        self.assertEqual(self.counts_of("tag_writes"), 0)

    def test_apply_with_the_switch_off_is_refused_before_anything(self):
        self.member(1)
        self.engine()
        self.assert_refused(lambda: self.run_tool(tags_write_enabled=False), "tags_write_disabled")

    def test_adds_cap_pilot_of_ten_in_customer_order(self):
        for n in range(12, 0, -1):
            self.member(n)
        result = self.run_tool()
        self.assertEqual([w[1] for w in self.shopify.writes], [customer_gid(n) for n in range(1, 11)])
        self.assertEqual((result["counts"]["tagged"], result["counts"]["tag_adds_capped"]), (10, 2))
        self.run_tool()
        self.assertEqual(len(self.shopify.writes), 12)

    def test_removes_cap(self):
        for n in range(1, 8):
            self.member(n, member_type="GUEST", tags=[TAG])
        for n in range(20, 100):
            self.member(n, tags=[TAG])  # 87 tagged: the volume guard allows 8
        result = self.run_tool()
        self.assertEqual([w[0] for w in self.shopify.writes], ["remove"] * 5)
        self.assertEqual(result["counts"]["tag_removes_capped"], 2)
        self.assertIn("tag_removes_capped", result["alerts"])

    def test_volume_guard_withholds_removals_adds_go_on(self):
        for n in range(1, 7):
            self.member(n, member_type="GUEST", tags=[TAG])
        for n in range(10, 20):
            self.member(n, tags=[TAG])
        self.member(30)
        result = self.run_tool(max_tag_removes_per_run=50)
        self.assertEqual(self.shopify.writes, [("add", customer_gid(30), [TAG])])
        self.assertEqual((result["counts"]["untag_withheld"], result["alerts"]), (6, ["untag_volume_unexpected"]))
        observed = self.run_tool(apply=False)
        self.assertIn("untag_volume_unexpected", observed["alerts"])
        # Five or fewer removals are never withheld.
        for n in range(2, 7):
            self.book.members[sg(n)]["type"] = "MEMBER"
        self.run_tool()
        self.assertEqual(self.shopify.writes[-1], ("remove", customer_gid(1), [TAG]))

    def test_stop_file_before_the_next_mutation(self):
        self.member(1)
        self.member(2)
        (self.root / mt.STOP_FILE).write_text("stop", encoding="ascii")
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["stopped"], self.counts_of("runs")), ([], "stop_file_present", 1))
        self.assertEqual(result["counts"]["would_tag"], 2)  # reads and the record still ran
        (self.root / mt.STOP_FILE).unlink()
        self.shopify.after_write = lambda: (self.root / mt.STOP_FILE).write_text("stop", encoding="ascii")
        result = self.run_tool()
        self.assertEqual((len(self.shopify.writes), result["stopped"]), (1, "stop_file_present"))

    def test_flapping_guard(self):
        self.member(1)
        self.run_tool()
        self.shopify.tags(1).remove(TAG)  # another writer removes it
        self.now += timedelta(hours=1)
        self.run_tool()
        self.shopify.tags(1).remove(TAG)
        self.now += timedelta(hours=1)
        result = self.run_tool()
        self.assertEqual(len(self.shopify.writes), 2)
        self.assertIn("tag_keeps_disappearing", result["alerts"])
        self.now += timedelta(hours=25)
        self.run_tool()
        self.assertEqual(len(self.shopify.writes), 3)

    def test_a_bridge_failure_stops_before_any_shopify_write(self):
        self.member(1)
        self.book.fail_scan = "bridge_read_failed:json:x0a0b0c0d:l1"
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["stopped"]), ([], "bridge_read_failed:json:x0a0b0c0d:l1"))
        self.book.fail_scan = None
        self.member(2, member_type="VIP", tags=[TAG])
        self.book.fail_member = "bridge_read_timeout"
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["stopped"]), ([], "bridge_read_timeout"))

    def test_a_shopify_read_failure_stops_before_any_write(self):
        self.member(1)
        self.shopify.fail_customers = ["shopify_http_server_error"]
        result = self.run_tool()
        self.assertEqual((self.shopify.writes, result["stopped"], self.counts_of("runs")), ([], "shopify_http_server_error", 1))

    def test_a_capped_search_decides_nothing(self):
        self.shopify.add(1, [TAG])
        self.shopify.carrier_cap = True
        result = self.run_tool()
        self.assertEqual((result["carriers_total"], result["alerts"]), (None, ["tag_search_incomplete", "tag_without_basis"]))
        self.assertEqual(self.shopify.writes, [])


class WriteStateTests(EngineBase):
    def test_rejected_goes_on_uncertain_stops(self):
        for n in (1, 2, 3):
            self.member(n)
        self.shopify.answers = {customer_gid(1): ["rejected"], customer_gid(2): ["uncertain_unapplied"]}
        result = self.run_tool()
        self.assertEqual([w[1] for w in self.shopify.writes], [customer_gid(1), customer_gid(2)])
        self.assertEqual([(r["state"], r["code"]) for r in self.rows()],
                         [("REJECTED", "shopify_user_error"), ("UNCERTAIN", "shopify_http_server_error")])
        self.assertEqual(result["stopped"], "write_uncertain")
        self.assertEqual(set(result["alerts"]), {"shopify_user_error", "write_uncertain"})

    def test_throttled_budget_is_not_written_and_stops(self):
        self.member(1)
        self.member(2)
        self.shopify.answers = {customer_gid(1): ["budget"]}
        result = self.run_tool()
        self.assertEqual(len(self.shopify.writes), 1)
        self.assertEqual((self.rows()[0]["state"], result["stopped"]), ("NOT_WRITTEN", "shopify_retry_budget_exhausted"))

    def test_next_run_settles_uncertain_by_read_both_ways(self):
        self.member(1)
        self.member(2)
        self.shopify.answers = {customer_gid(1): ["uncertain_applied"]}
        self.run_tool()
        result = self.run_tool()
        self.assertEqual(self.rows()[0]["state"], "DONE")
        self.assertEqual(self.rows()[0]["code"], "resolved_by_read")
        self.assertEqual(result["counts"]["write_settled_done"], 1)
        self.assertEqual([w[1] for w in self.shopify.writes], [customer_gid(1), customer_gid(2)])  # never sent twice
        self.shopify.answers = {customer_gid(3): ["uncertain_unapplied"]}
        self.member(3)
        self.run_tool()
        result = self.run_tool()
        third = [r for r in self.rows() if r["customer_gid"] == customer_gid(3)]
        self.assertEqual([(r["state"], r["code"]) for r in third], [("NOT_WRITTEN", "resolved_by_read"), ("DONE", None)])
        self.assertEqual(result["counts"]["write_settled_not_written"], 1)

    def test_a_customer_gone_settles_not_written(self):
        self.member(1)
        self.shopify.answers = {customer_gid(1): ["uncertain_unapplied"]}
        self.run_tool()
        del self.shopify.customers_by_gid[customer_gid(1)]
        self.run_tool()
        self.assertEqual((self.rows()[0]["state"], self.rows()[0]["code"]), ("NOT_WRITTEN", "customer_not_found"))

    def test_a_crash_after_intent_does_not_double_send(self):
        self.member(1)
        self.shopify.answers = {customer_gid(1): ["crash_applied"]}
        with self.assertRaises(KeyboardInterrupt):
            self.run_tool()
        self.assertEqual(self.rows()[0]["state"], "INTENT")
        self.run_tool()
        self.assertEqual(len(self.shopify.writes), 1)
        self.assertEqual((self.rows()[0]["state"], self.rows()[0]["code"]), ("DONE", "resolved_by_read"))

    def test_a_remove_settles_by_read_too(self):
        self.member(1, member_type="GUEST", tags=[TAG])
        self.shopify.answers = {customer_gid(1): ["uncertain_applied"]}
        self.run_tool()
        self.run_tool()
        self.assertEqual((self.rows()[0]["state"], len(self.shopify.writes)), ("DONE", 1))

    def test_no_mutation_when_the_state_already_matches(self):
        self.member(1, tags=[TAG])
        self.member(2, member_type="GUEST")
        self.run_tool()
        self.assertEqual(self.shopify.writes, [])


class RefusalTests(EngineBase):
    """Every refusal records nothing (no runs row, no tag_writes row) and
    makes no HTTP call where it comes before one."""

    def test_input_ledger_refusals(self):
        self.member(1)
        self.engine()
        for path, prefix in ((self.sync_root / sync.LEDGER_FILE, "sync_ledger"), (self.core_root / bm.LEDGER_FILE, "member_core_ledger")):
            with self.subTest(prefix=prefix):
                saved = path.read_bytes()
                set_user_version(path, 9)
                self.assert_refused(lambda: self.run_tool(), f"{prefix}_version_unknown")
                path.write_bytes(b"not sqlite" * 50)
                self.assert_refused(lambda: self.run_tool(), f"{prefix}_unreadable")
                path.unlink()
                self.assert_refused(lambda: self.run_tool(), f"{prefix}_missing")
                path.write_bytes(saved)
        for name in os.listdir(self.core_root):
            os.remove(self.core_root / name)
        self.make_core(store="gid://shopify/Shop/2")
        self.assert_refused(lambda: self.run_tool(), "member_core_ledger_binding_mismatch")
        for name in os.listdir(self.core_root):
            os.remove(self.core_root / name)
        self.make_core()
        for name in os.listdir(self.sync_root):
            os.remove(self.sync_root / name)
        self.make_sync(store="gid://shopify/Shop/2")
        self.assert_refused(lambda: self.run_tool(), "sync_ledger_binding_mismatch")

    def test_scope_and_store_refusals_after_the_session_call_only(self):
        self.member(1)
        self.engine()
        for scopes in (["read_customers"], ["write_customers", "read_orders"]):
            self.shopify.scopes = scopes
            self.shopify.calls.clear()
            self.assert_refused(lambda: self.run_tool(), "shopify_token_scope_not_expected", http_calls=1)

    def test_ledger_binding_refusal(self):
        self.member(1)
        self.engine()
        other = raw_config(self.root, self.sync_root, self.core_root)
        other["shopify"] = {**other["shopify"], "store_gid": "gid://shopify/Shop/2"}
        engine = mt.TagsEngine(mt.parse_config(other), self.ledger, shopify=self.shopify, ac2=self.book)
        self.assert_refused(lambda: engine.run(apply=True), "tags_ledger_binding_mismatch")


class CliTests(EngineBase):
    def run_main(self, *args, factory=True, **overrides) -> int:
        with private_root_allowed(self.temp / "elsewhere"):
            engine_factory = (lambda cfg, led, write: mt.TagsEngine(cfg, led, shopify=self.shopify, ac2=self.book)) if factory else None
            return mt.main([*args, "--config", str(self.write_config(**overrides))], engine_factory=engine_factory, clock=lambda: self.now)

    def test_observe_apply_digest(self):
        self.member(1)
        self.member(2, member_type="GUEST", tags=[TAG])
        self.member(3, tags=["member-mg"], registration=False)
        self.assertEqual(self.run_main("init"), 0)
        self.assertEqual(self.run_main("observe"), 3)  # flow_dates_guard is an alert
        observed = self.emitted[-1]
        self.assertEqual((observed["command"], observed["scope_set"], observed["counts"]["would_tag"]), ("observe", "write_only", 1))
        self.assertEqual(observed["query_costs"], {"XbMemberTagsCustomers": 200, "XbMemberTagsCarriers": 102})
        self.assertEqual(self.run_main("observe"), 0)  # alert once per subject
        self.assertEqual(self.run_main("apply"), 0)
        self.assertEqual(self.emitted[-1]["counts"]["tagged"], 1)
        self.assertEqual(self.run_main("digest", "--hours", "24"), 0)
        digest = self.emitted[-1]
        self.assertEqual((digest["runs"], digest["shadow"], digest["open"]),
                         ({"completed": 3, "unfinished": 0}, {"would_tag": 1, "would_untag": 1}, {"uncertain_writes": 0, "intent_writes": 0}))
        self.assertEqual(digest["alerts"], {"flow_dates_guard": 1})
        self.assertEqual(digest["alert_refs"][0]["code"], "flow_dates_guard")
        self.assertEqual(digest["tagged_now"], 1)
        self.assertEqual(self.run_main("digest", "--hours", "0"), 2)
        self.assertEqual(self.run_main("status"), 0)
        self.assertEqual(self.emitted[-1]["tag_writes"], {"add:DONE": 1, "remove:DONE": 1})

    def test_apply_with_the_switch_off_creates_nothing(self):
        self.member(1)
        self.assertEqual(self.run_main("apply", tags_write_enabled=False), 2)
        self.assertEqual(self.emitted[-1], {"status": "refused", "code": "tags_write_disabled"})
        self.assertFalse((self.root / mt.LEDGER_FILE).exists())
        self.assertEqual(self.shopify.calls, [])

    def test_state_root_refusals_create_nothing(self):
        self.assertEqual(self.run_main("observe", member_core_state_root=str(self.root)), 2)
        self.assertEqual(self.emitted[-1]["code"], "tags_state_root_shared")
        self.assertFalse((self.root / mt.LEDGER_FILE).exists())
        self.assertEqual(self.run_main("observe"), 2)
        self.assertEqual(self.emitted[-1]["code"], "tags_ledger_not_initialised")
        self.assertEqual(self.shopify.calls, [])

    def test_a_second_runner_is_refused(self):
        self.run_main("init")
        holder = mt.TagsLedger(self.root)
        try:
            self.assertEqual(self.run_main("observe"), 2)
            self.assertEqual(self.emitted[-1]["code"], "ledger_locked_by_another_owner")
        finally:
            holder.close()
        self.assertEqual(self.shopify.calls, [])

    def test_build_real_engine_mutations_follow_the_command(self):
        self.run_main("init")
        environ = {"XB_SHOPIFY_TAGS_CLIENT_ID": "i" * 20, "XB_SHOPIFY_TAGS_CLIENT_SECRET": "s" * 20}
        ledger = mt.TagsLedger(self.root)
        try:
            observe = mt.build_real_engine(self.cfg(), ledger, environ=environ, apply=False)
            apply = mt.build_real_engine(self.cfg(), ledger, environ=environ, apply=True)
            off = mt.build_real_engine(self.cfg(tags_write_enabled=False), ledger, environ=environ, apply=True)
            self.assertEqual((observe.shopify.mutations_enabled, apply.shopify.mutations_enabled, off.shopify.mutations_enabled), (False, True, False))
            self.assertIsInstance(apply.ac2, mt.TagsReadBridgeClient)
            self.assertEqual(apply.ac2.command[-1], str(mt.BRIDGE_SCRIPT))
        finally:
            ledger.close()


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
