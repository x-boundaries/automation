"""#268 member core: the benefits tool (config, rule, ledger, engine, CLI).

No PowerShell, AutoCount, Shopify or network: the engine runs against
``FakeShopify`` (orders and customers built in Shopify's documented
2026-10 shapes and parsed by the real parser) and ``FakeBook``, a model of
ac2_member_benefits_bridge.ps1 (same pre-checks, one save per call, claim file
before the save). ``tests/test_member_benefits_ps.py`` proves the real child
behaves the same. Fake data only: example.com, synthetic store, phone numbers
built in code.

Closure rows (G0 design 3.4, as cut by the G1 brief) each have a test named
``test_qNN_...`` in ``ClosureRowTests``.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
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
import xb_member_benefits as bm  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_member_type_move as tm  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402

STORE_GID = "gid://shopify/Shop/1"
SHOP = "fake-store.synthetic.invalid"
LM = "2026-10-01T09:00:00.000"
START = "2026-11-15T00:00:00Z"
ORDER_AT = "2026-11-18T03:00:00Z"  # 11:00 Singapore, 18 Nov
SIGNUP_AT = "2026-11-17T02:00:00Z"
NEW_EXPIRY = "2028-11-18T00:00:00"
PAST_DEADLINE = 120 + dl.WRITER_GRACE_SECONDS + 1


def local(n: int) -> str:
    """Eight fake Singapore digits, built in code (never a literal number)."""
    return "9" + f"{n:07d}"


def sg(n: int) -> str:
    return "65" + local(n)


def customer_number(n: int) -> int:
    """A fake customer ID of 16 digits, built in code. The CLI privacy tests
    look for the bare number in output that also holds random hex (run ids,
    keyed refs): 16 digits cannot turn up there by chance (odds below 1 in
    10**15 a run), where the four digits used before did about once in 75."""
    return 10 ** 15 + n


def order_number(n: int) -> int:
    """A fake order ID of 16 digits, as ``customer_number``."""
    return 5 * 10 ** 15 + n


def customer_gid(n: int) -> str:
    return f"gid://shopify/Customer/{customer_number(n)}"


def order_gid(n: int) -> str:
    return f"gid://shopify/Order/{order_number(n)}"


def money(amount: str) -> dict:
    return {"shopMoney": {"amount": amount, "currencyCode": "SGD"}}


def customer_node(n: int, *, tags=("member-mg",), registration: str | None = SIGNUP_AT, phone: str | None = "default",
                  first: str = "Fake", last: str | None = None, created: str = "2026-11-01T00:00:00Z") -> dict:
    return {"id": customer_gid(n), "createdAt": created, "tags": list(tags), "firstName": first, "lastName": last if last is not None else f"Member {n}",
            "defaultPhoneNumber": None if phone is None else {"phoneNumber": "+" + sg(n) if phone == "default" else phone},
            "registration": None if registration is None else {"createdAt": registration}}


def tx(amount: str, gateway: str | None = "gift_card", kind: str = "SALE", status: str = "SUCCESS", currency: str = "SGD") -> dict:
    """One ``OrderTransaction`` in the documented 2026-10 shape (a gift card payment unless told otherwise)."""
    return {"gateway": gateway, "kind": kind, "status": status, "amountSet": {"shopMoney": {"amount": amount, "currencyCode": currency}}}


def card(amount: str, **fields) -> dict:
    """A payment by card (any gateway that is not a gift card)."""
    return tx(amount, gateway="shopify_payments", **fields)


def order_node(n: int, *, customer: dict | None | str = "default", lines=(("300.00", 1, 1, False, ()),), shipping=("15.00",),
               created: str = ORDER_AT, updated: str | None = None, status: str = "PAID", test: bool = False, cancelled: str | None = None,
               taxes: bool = True, currency: str = "SGD", refunded: str = "0.00", more_lines: bool = False, transactions=(),
               tx_count="exact") -> dict:
    nodes = []
    for index, (unit, quantity, current, gift, discounts) in enumerate(lines):
        nodes.append({"id": f"gid://shopify/LineItem/{9000 + n * 10 + index}", "quantity": quantity, "currentQuantity": current, "isGiftCard": gift,
                      "originalUnitPriceSet": {"shopMoney": {"amount": unit, "currencyCode": currency}},
                      "discountAllocations": [{"allocatedAmountSet": money(d)} for d in discounts]})
    return {
        "id": order_gid(n), "createdAt": created, "updatedAt": updated or created, "cancelledAt": cancelled, "test": test,
        "displayFinancialStatus": status, "taxesIncluded": taxes, "currencyCode": currency, "totalRefundedSet": money(refunded),
        "customer": customer_node(n) if customer == "default" else customer,
        "lineItems": {"pageInfo": {"hasNextPage": more_lines}, "nodes": nodes},
        "shippingLines": {"pageInfo": {"hasNextPage": False}, "nodes": [{"originalPriceSet": money(s)} for s in shipping]},
        "transactions": [dict(t) for t in transactions],
        # Slice B1 (M1): Order.transactionsCount, exact and equal to the list unless a test says otherwise.
        "transactionsCount": {"count": len(transactions), "precision": "EXACT"} if tx_count == "exact" else tx_count,
    }


class FakeShopify:
    """The benefits app's reads over fixtures in the documented shapes."""

    def __init__(self):
        self.orders_nodes: dict[str, dict] = {}
        self.customers: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.scopes = ["read_customers", "read_orders"]
        self.fail_orders: str | None = None
        self.fail_after_cursor = False
        self.truncated_phones: set[str] = set()
        self.last_end_cursor: str | None = None

    def add(self, node: dict) -> None:
        self.orders_nodes[node["id"]] = node
        if node.get("customer"):
            self.customers[node["customer"]["id"]] = node["customer"]

    def verify_session(self, *, store_gid):
        self.calls.append(("session",))
        if frozenset(self.scopes) != bm.REQUIRED_SCOPES:
            raise direct.ShopifyDirectError("shopify_token_scope_not_expected")

    def orders(self, since, max_pages, after=None):
        """As Shopify: ascending by updatedAt (ties by id), pages of
        SHOPIFY_ORDER_PAGE, at most max_pages, an opaque cursor per position."""
        self.calls.append(("orders", since))
        if self.fail_orders:
            raise direct.ShopifyDirectError(self.fail_orders, retryable=True)
        if after is not None and self.fail_after_cursor:
            raise direct.ShopifyDirectError("shopify_graphql_error")
        nodes = sorted((n for n in self.orders_nodes.values() if dl.parse_created_at(n["updatedAt"]) >= since), key=lambda n: (n["updatedAt"], n["id"]))
        start = 0 if after is None else int(after.removeprefix("cursor-")) + 1
        limit = bm.SHOPIFY_ORDER_PAGE * max_pages
        page = nodes[start:start + limit]
        complete = start + limit >= len(nodes)
        self.last_end_cursor = None if complete else f"cursor-{start + limit - 1}"
        return [bm.order_from_node(n) for n in page], complete

    def customer(self, gid):
        self.calls.append(("customer", gid))
        node = self.customers.get(gid)
        return None if node is None else bm.customer_from_node(node)

    def find_by_phone(self, digits):
        self.calls.append(("phone", digits))
        if digits in self.truncated_phones:
            raise bm.PhoneCandidatesTruncated()
        found = [bm.customer_from_node({**c, "tags": []}) for c in self.customers.values()]
        return [c for c in found if sync.phone_member_no(c.phone)[0] == digits]


def fake_member(member_no: str, *, member_type: str = "GUEST", active: str = "T", expiry: str | None = None, name: str = "Fake Member 1",
                last_modified: str = LM) -> dict:
    return {"member_no": member_no, "type": member_type, "active": active, "expiry": expiry, "last_modified": last_modified, "name": name,
            "email": "fake@example.com", "register": "2024-07-01"}


class FakeBook:
    """A model of ac2_member_benefits_bridge.ps1 on a fake book."""

    def __init__(self, members, claims: Path):
        self.members = {m["member_no"]: dict(m) for m in members}
        self.claims = claims
        self.saves = 0
        self.reads = 0
        self.payloads: list[dict] = []
        self.mode: str | None = None  # uncertain_saved, uncertain_unsaved, mutate, raise
        self.staff_edit_before_save: str | None = None
        self.fail_read: str | None = None
        self.tick = 0

    @staticmethod
    def digest(m, salt):
        kept = {k: v for k, v in m.items() if k not in ("type", "expiry", "last_modified")}
        return "sha256:" + hashlib.sha256((salt + ":" + json.dumps(kept, sort_keys=True)).encode()).hexdigest()

    def member(self, member_no, salt):
        self.reads += 1
        if self.fail_read:
            raise dl.BridgeError(self.fail_read)
        m = self.members.get(member_no)
        if m is None:
            return bm.MemberRead(False)
        return bm.MemberRead(True, member_no=m["member_no"], member_type=m["type"], name=m["name"], is_active=m["active"],
                             expiry_local=m["expiry"], last_modified_local=m["last_modified"], fields_digest=self.digest(m, salt))

    def scan(self, member_type, after):
        assert member_type in bm.SCOPE_TYPES
        rows = sorted((m for m in self.members.values() if m["type"] == member_type and m["member_no"] > after), key=lambda m: m["member_no"])
        return [{"member_no": m["member_no"], "name": m["name"]} for m in rows[:tm.SCAN_PAGE_SIZE]]

    def save(self, payload, *, move_id, deadline=None, on_started=None):
        bm.check_save_payload(payload)  # as the real client
        self.payloads.append(dict(payload))
        if on_started is not None:
            on_started(4242, "fake-created")
        if self.mode == "raise":
            raise RuntimeError("bridge exploded")
        m = self.members.get(payload["member_no"])
        if self.staff_edit_before_save == payload["member_no"] and m is not None:
            m["last_modified"] = "2026-11-18T05:00:00.123"
            self.staff_edit_before_save = None
        if m is None:
            return bm.SaveOutcome("SKIPPED", "member_not_found")
        if m["type"] not in bm.SCOPE_TYPES:
            return bm.SaveOutcome("SKIPPED", "type_not_in_scope_now")
        if m["type"] != payload["from_type"]:
            return bm.SaveOutcome("SKIPPED", "type_changed_since_read")
        if m["last_modified"] != payload["expected_last_modified_local"] or m["expiry"] != payload["expected_expiry_local"]:
            return bm.SaveOutcome("SKIPPED", "changed_since_read")
        if m["expiry"] is not None and not bm.is_later(payload["new_expiry_local"], m["expiry"]):
            return bm.SaveOutcome("SKIPPED", "expiry_not_later_now")
        fence = "fence-" + move_id.removeprefix("write-")
        (self.claims / f"{fence}.claim").write_text(json.dumps({"before_digest": self.digest(m, payload["salt"])}), encoding="ascii")
        if self.mode == "uncertain_unsaved":
            return bm.SaveOutcome("UNCERTAIN", "writer_timeout")
        self.saves += 1
        self.tick += 1
        m["type"] = bm.TARGET_TYPE
        m["expiry"] = payload["new_expiry_local"] + ".000"
        m["last_modified"] = f"2026-11-20T04:00:{self.tick:02d}.007"
        if self.mode == "mutate":
            m["name"] = "changed"
            return bm.SaveOutcome("HELD", "readback_fields_changed", m["last_modified"], ("Name",))
        if self.mode == "uncertain_saved":
            return bm.SaveOutcome("UNCERTAIN", "writer_timeout")
        if self.mode == "uncertain_mutated":  # saved, another column changed, outcome lost
            m["name"] = "changed"
            return bm.SaveOutcome("UNCERTAIN", "writer_timeout")
        return bm.SaveOutcome("DONE", None, m["last_modified"])


def raw_config(root: Path, sync_root: Path, **overrides) -> dict:
    value = {
        "schema": bm.CONFIG_SCHEMA, "state_root": str(root), "autocount_book": "AED_TESTINGAPI", "orders_start_utc": START,
        "member_sync_state_root": str(sync_root),
        "shopify": {"store_gid": STORE_GID, "shop_domain": SHOP, "api_version": "2026-10",
                    "auth": {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_BENEFITS_CLIENT_ID", "client_secret_env": "XB_SHOPIFY_BENEFITS_CLIENT_SECRET"}},
        "benefits_write_enabled": True,
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
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-benefits-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.root = self.temp / "state"
        self.root.mkdir()
        (self.root / "claims").mkdir()
        self.sync_root = self.temp / "sync"
        self.sync_root.mkdir()
        self.now = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)
        self.shopify = FakeShopify()
        self.book = FakeBook([], self.root / "claims")
        self.links_by_gid: dict[str, str] = {}
        self.ledger = None
        self.probe_result = "exited"
        # G2 B7: every CLI output of every test is kept for the privacy check.
        self.emitted: list[dict] = []
        real_emit = bm._emit

        def tee(value):
            self.emitted.append(json.loads(json.dumps(value)))
            real_emit(value)
        patcher = mock.patch.object(bm, "_emit", tee)
        patcher.start()
        self.addCleanup(patcher.stop)

    # Slice B1 T2: every pattern ends with \Z ("$" also matches before one
    # line break at the end, so "upgraded\n" passed).
    PRIVATE_REF_RE = re.compile(r"^[ocm]-[0-9a-f]{20}\Z")
    PRIVATE_MASK_RE = re.compile(r"^\*{3,}[^*]{1,3}\Z")
    PRIVATE_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")
    PRIVATE_WORD_RE = re.compile(r"^[a-z0-9_:]{1,64}\Z")  # a count key, an alert code, a ref code, the ``stopped`` text
    PRIVATE_DIGITS_RE = re.compile(r"[0-9]{4}")  # IDs are shown by their last 3 characters at most
    # The one exception to the digit rule. A generic failure of the AutoCount
    # child is named by the shared bridge code (xb_directlocal.bridge_failure_code)
    # ``bridge_read_failed:<shape>:x<exit code as 8 hex characters, or "none">:l<output lines, 0 to 9>``.
    # That tail is worked out by the tool, never taken from the child's text,
    # and an exit code is not private. Exactly that tail, at the end of a word
    # with exactly that prefix, is taken off before the digit-run test; the
    # rest of the word is checked as any other word.
    BRIDGE_PREFIX = "bridge_read_failed:"
    BRIDGE_TAIL_RE = re.compile(r":x(?:[0-9a-f]{8}|none):l[0-9]\Z")
    # Slice B1 T5: the allowed top-level keys of every output, per command
    # (required, optional). Written out here, never taken from the tool, so a
    # new key in any output fails the check until it is added on purpose.
    DIGEST_SCHEMA = "xb.directlocal.member_benefits.digest.v1"
    CONFIG_KEYS = frozenset({
        "status", "autocount_book", "api_version", "required_scopes", "orders_start_utc", "benefits_write_enabled", "max_upgrades_per_run",
        "max_member_reads_per_run", "qualify_threshold_cents", "amount_before_discounts", "amount_include_tax", "amount_exclude_shipping",
        "amount_exclude_gift_cards", "gift_card_gateways", "amount_count_sale_items", "same_day_signup_counts", "expiry_months", "target_type",
        "type_move_lock", "ac2_credential", "ac2_login_mode", "rule_fingerprint"})
    RUN_KEYS = frozenset({"status", "command", "run_id", "saves", "counts", "alerts", "refs", "stopped"})
    OUTPUT_KEYS = {
        "status": (frozenset({"status", "command", "receipts", "observed", "writes", "held_writes", "claim_links", "claim_links_voided",
                              "reviews", "watermark_set", "stop_file_present", "rule_settings_match"}), frozenset()),
        "release_held": (frozenset({"status", "command", "run_id", "finding", "receipt_now", "held_writes_left", "counts", "refs", "alerts"}),
                         frozenset()),
        "release_held_dry_run": (frozenset({"status", "command", "run_id", "finding", "receipt_now", "held_writes_left", "counts", "refs", "alerts"}),
                                 frozenset()),
        "unlink": (frozenset({"status", "command", "run_id", "reason", "link_source", "counts", "alerts", "refs"}), frozenset()),
        "unlink_dry_run": (frozenset({"status", "command", "run_id", "reason", "link_source", "counts", "alerts", "refs"}), frozenset()),
        "observe": (RUN_KEYS | {"orders_query_cost"}, frozenset()),
        "apply": (RUN_KEYS | {"orders_query_cost"}, frozenset()),
        "resolve": (frozenset({"status", "command", "writes", "resolved", "alerts"}), frozenset({"receipts_left"})),
        "index_dry_run": (RUN_KEYS, frozenset()),
        "index_apply": (RUN_KEYS, frozenset()),
        # Slice B2 (B2-11): the read-only claim links listing.
        "claim_links": (frozenset({"status", "command", "links", "active", "voided", "truncated"}), frozenset()),
    }
    DIGEST_KEYS = frozenset({"schema", "status", "autocount_book", "hours", "runs", "counts", "alerts", "alert_refs", "receipts", "shadow",
                             "gift_cards", "open", "rule_settings_match"})
    DIGEST_PART_KEYS = {"runs": frozenset({"completed", "unfinished"}), "shadow": frozenset({"would_upgrade", "would_extend"}),
                        "open": frozenset({"uncertain_writes", "held_writes", "waiting_member", "claim_links", "claim_links_voided"})}
    WRITE_STATES = ("INTENT", "DONE", "SKIPPED", "NOT_WRITTEN", "UNCERTAIN", "HELD")
    # Fix round 1 of slice B1 (G2 L2): the values of the keys slice B1 added,
    # written out here (never taken from the tool): fixed words for the text
    # keys, whole numbers for the counts, true or false for the match.
    NEW_KEY_TEXTS = {"finding": ("as_intended", "not_as_intended"), "receipt_now": ("applied", "held", "left_decided", "left_open"),
                     "reason": ("wrong_person", "customers_merged", "member_renumbered", "other"), "link_source": ("order", "index")}
    NEW_KEY_NUMBERS = ("held_writes_left", "held_writes", "claim_links", "claim_links_voided", "active", "voided")
    # Slice B2 (B2-11): the keys and values of one claim-links entry, written out here.
    CLAIM_LINK_KEYS = frozenset({"ref", "member_no_masked", "source", "created_day"})
    CLAIM_LINK_SOURCES = ("order", "index")
    CLAIM_LINK_REF_RE = re.compile(r"^c-[0-9a-f]{20}\Z")
    CLAIM_LINK_DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

    def tearDown(self):
        self.assert_outputs_private()

    def assert_outputs_private(self) -> None:
        """G2 B7, after every test: every stored run summary and every CLI
        output carries keyed refs and masked MemberNos only, and no order
        ID, customer ID or full MemberNo anywhere. Slice A: a payment gateway
        name leaves the tool only as a checked code word, and a name in the
        ledger that is not one appears in no output. Slice A2: the query
        cost of a run is a whole number of points or nothing; every count
        key, alert code, ref code and ``stopped`` text is a code word (lower
        case letters, digits, ``_`` and ``:``, 1 to 64 characters) with no
        run of four or more digits, so no part of an order number, customer
        number or MemberNo and no name can sit there (one exception, the
        tail of a generic bridge failure code: see ``BRIDGE_TAIL_RE``). The
        rule reads fixed code words only, never the random run ids or keyed
        refs, so it cannot fail by chance."""

        outputs = [("cli", value) for value in self.emitted]
        private = {m for m in self.book.members}
        path = self.root / bm.LEDGER_FILE
        if path.is_file():
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if "runs" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}:
                    outputs += [("run", json.loads(r[0])) for r in conn.execute("SELECT summary FROM runs WHERE summary IS NOT NULL")]
                    for sql in ("SELECT order_gid FROM receipts", "SELECT customer_gid FROM receipts", "SELECT member_no FROM receipts",
                                "SELECT customer_gid FROM claim_links", "SELECT member_no FROM claim_links", "SELECT member_no FROM writes"):
                        private |= {r[0] for r in conn.execute(sql) if r[0]}
                    for (names,) in conn.execute("SELECT payment_gateways FROM receipts WHERE payment_gateways IS NOT NULL"):
                        private |= {name for name in json.loads(names) if not self.PRIVATE_CODE_RE.fullmatch(name)}
            finally:
                conn.close()
        private = {value for value in private if len(value) >= 6}
        for kind, value in outputs:
            text = json.dumps(value)
            self.assertNotIn("gid://", text, kind)
            for secret in private:
                self.assertNotIn(secret, text, kind)
            entries = [e for key in ("refs", "alert_refs") for e in (value.get(key) or [])] if isinstance(value, dict) else []
            for entry in entries:
                self.assertLessEqual(set(entry), {"ref", "code", "member_no_masked"}, kind)
                self.assertRegex(entry["ref"], self.PRIVATE_REF_RE, kind)
                if "member_no_masked" in entry:
                    self.assertRegex(entry["member_no_masked"], self.PRIVATE_MASK_RE, kind)
            if isinstance(value, dict) and "gift_cards" in value:
                self.assertEqual(set(value["gift_cards"]), {"receipts_paid_by_gift_card", "gateways_seen"}, kind)
                self.assertIsInstance(value["gift_cards"]["receipts_paid_by_gift_card"], int, kind)
                for name, number in value["gift_cards"]["gateways_seen"].items():
                    self.assertRegex(name, self.PRIVATE_CODE_RE, kind)
                    self.assertIsInstance(number, int, kind)
            for name in (value.get("gift_card_gateways") or []) if isinstance(value, dict) else []:
                self.assertRegex(name, self.PRIVATE_CODE_RE, kind)
            if isinstance(value, dict) and "orders_query_cost" in value:
                cost = value["orders_query_cost"]
                self.assertTrue(cost is None or (type(cost) is int and 0 <= cost <= 1_000_000), kind)
            self.assertIsInstance(value, dict, kind)
            if isinstance(value, dict):
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
        """Slice B1 T3-T5: the top-level keys of the output are exactly the
        ones its command allows; ``counts`` (where present) is an object of
        whole numbers and ``alerts`` a list (a run) or an object of whole
        numbers (the digest); the ``code`` of a refused output and the keys of
        ``resolved``, ``receipts``, ``observed`` and ``writes`` are code words.
        Returns the extra words for the word rule."""

        def whole(number) -> bool:
            return type(number) is int

        keys = set(value)
        words: list = []
        if value.get("status") == "refused":
            self.assertEqual(keys, {"status", "code"}, kind)
            words.append(value["code"])
        elif value.get("schema") == self.DIGEST_SCHEMA:
            self.assertEqual(keys, set(self.DIGEST_KEYS), kind)
            for part, allowed in self.DIGEST_PART_KEYS.items():
                self.assertIsInstance(value[part], dict, kind)
                self.assertEqual(set(value[part]), set(allowed), kind)
            for part in ("alerts", "receipts"):
                self.assertIsInstance(value[part], dict, kind)
                self.assertTrue(all(whole(n) for n in value[part].values()), kind)
                words += list(value[part])
            self.assertIs(type(value["rule_settings_match"]), bool, kind)
            self.assertTrue(all(whole(n) for n in value["open"].values()), kind)
        elif "command" in value:
            self.assertIn(value["command"], self.OUTPUT_KEYS, kind)
            required, optional = self.OUTPUT_KEYS[value["command"]]
            self.assertLessEqual(set(required), keys, kind)
            self.assertLessEqual(keys, set(required) | set(optional), kind)
            if "alerts" in value:
                self.assertIsInstance(value["alerts"], list, kind)
            for key, allowed in self.NEW_KEY_TEXTS.items():
                if key in value:
                    self.assertIn(value[key], allowed, kind)
            for key in self.NEW_KEY_NUMBERS:
                if key in value:
                    self.assertTrue(whole(value[key]), kind)
            if "rule_settings_match" in value:
                self.assertIs(type(value["rule_settings_match"]), bool, kind)
            for part in ("receipts", "observed"):
                if part in value:
                    self.assertIsInstance(value[part], dict, kind)
                    words += list(value[part])
            if value["command"] == "status":
                self.assertIsInstance(value["writes"], dict, kind)
                for key in value["writes"]:
                    self.assertRegex(key, r"^(upgrade|extend):(" + "|".join(self.WRITE_STATES) + r")\Z", kind)
            if value["command"] == "claim_links":
                self.assertIs(type(value["truncated"]), bool, kind)
                self.assertIsInstance(value["links"], list, kind)
                self.assertLessEqual(len(value["links"]), 500, kind)
                for entry in value["links"]:
                    self.assertEqual(set(entry), set(self.CLAIM_LINK_KEYS), kind)
                    self.assertRegex(entry["ref"], self.CLAIM_LINK_REF_RE, kind)
                    self.assertRegex(entry["member_no_masked"], self.PRIVATE_MASK_RE, kind)
                    self.assertIn(entry["source"], self.CLAIM_LINK_SOURCES, kind)
                    self.assertRegex(entry["created_day"], self.CLAIM_LINK_DAY_RE, kind)
            if value["command"] == "resolve":
                self.assertIsInstance(value["resolved"], dict, kind)
                for key in value["resolved"]:
                    state, _, code = key.partition(":")
                    self.assertIn(state, self.WRITE_STATES, kind)
                    words.append(code)
        elif keys == {"status"}:
            self.assertIn(value["status"], ("initialised", "existing"), kind)
        else:
            self.assertEqual(keys, set(self.CONFIG_KEYS), kind)
            self.assertEqual(value["status"], "ok", kind)
        if "counts" in value:
            self.assertIsInstance(value["counts"], dict, kind)
            self.assertTrue(all(whole(n) for n in value["counts"].values()), kind)
        return words

    def cfg(self, **overrides) -> bm.BenefitsConfig:
        return bm.parse_config(raw_config(self.root, self.sync_root, **overrides))

    def link(self, n: int, member_no: str | None = None) -> str:
        member_no = member_no or sg(n)
        self.links_by_gid[customer_gid(n)] = member_no
        return member_no

    def sync_links(self) -> bm.SyncLinks:
        return bm.SyncLinks(self.links_by_gid, {sync.member_no_key(m): g for g, m in self.links_by_gid.items()})

    def engine(self, **overrides) -> bm.BenefitsEngine:
        cfg = self.cfg(**overrides)
        if self.ledger is None:
            self.ledger = bm.BenefitsLedger(self.root, clock=lambda: self.now)
            self.addCleanup(self.ledger.close)
            self.ledger.init_meta(cfg)
        return bm.BenefitsEngine(cfg, self.ledger, shopify=self.shopify, ac2=self.book, sync_links=self.sync_links,
                                 writer_probe=lambda pid, created: self.probe_result)

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)

    def receipt(self, n: int) -> sqlite3.Row:
        return self.ledger.receipt(order_gid(n))

    def code(self, call) -> str:
        with self.assertRaises(dl.DirectLocalError) as caught:
            call()
        return caught.exception.code

    def guest_with_order(self, n: int = 1, *, linked: bool = True, member: dict | None = None, **order) -> str:
        member_no = sg(n)
        self.book.members[member_no] = member or fake_member(member_no, name=f"Fake Member {n}")
        if linked:
            self.link(n, member_no)
        self.shopify.add(order_node(n, **order))
        return member_no


# --------------------------------------------------------------------------
# Pure rules
# --------------------------------------------------------------------------

class RuleTests(Base):
    def amount(self, node, **cfg) -> tuple[int, int] | None:
        return bm.qualifying_amount(bm.order_from_node(node), self.cfg(**cfg))

    def test_amount_table(self):
        cases = [
            # (name, lines, shipping, config, expected (gross, net) or None)
            ("exactly_300", (("300.00", 1, 1, False, ()),), ("15.00",), {}, (30000, 30000)),
            ("299_99", (("299.99", 1, 1, False, ()),), (), {}, (29999, 29999)),
            ("before_discounts", (("300.00", 1, 1, False, ("17.00",)),), (), {}, (30000, 30000)),
            ("after_discounts_option", (("300.00", 1, 1, False, ("17.00",)),), (), {"amount_before_discounts": False}, (28300, 28300)),
            ("shipping_excluded", (("295.00", 1, 1, False, ()),), ("15.00",), {}, (29500, 29500)),
            ("shipping_included_option", (("295.00", 1, 1, False, ()),), ("15.00",), {"amount_exclude_shipping": False}, (31000, 31000)),
            ("gift_card_excluded_option", (("250.00", 1, 1, False, ()), ("100.00", 1, 1, True, ())), (), {"amount_exclude_gift_cards": True}, (25000, 25000)),
            ("gift_card_line_counts", (("250.00", 1, 1, False, ()), ("100.00", 1, 1, True, ())), (), {}, (35000, 35000)),
            ("several_lines_and_units", (("100.00", 2, 2, False, ()), ("100.00", 1, 1, False, ())), (), {}, (30000, 30000)),
            ("refunded_unit_netted", (("100.00", 3, 2, False, ()),), (), {}, (30000, 20000)),
            ("sale_item_counts", (("150.00", 2, 2, False, ()),), (), {}, (30000, 30000)),
        ]
        for name, lines, shipping, cfg, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(self.amount(order_node(1, lines=lines, shipping=shipping), **cfg), expected)

    def test_unmeasurable_orders(self):
        self.assertIsNone(self.amount(order_node(1, taxes=False)))
        self.assertIsNone(self.amount(order_node(1, currency="USD")))
        self.assertIsNone(self.amount(order_node(1, more_lines=True)))

    def test_threshold_is_inclusive(self):
        cfg = self.cfg()
        self.assertTrue(bm.qualifies(30000, cfg))
        self.assertFalse(bm.qualifies(29999, cfg))
        self.assertTrue(bm.qualifies(25000, self.cfg(qualify_threshold_cents=25000)))

    def test_money_is_exact(self):
        self.assertEqual([bm.money_cents(v) for v in ("300.00", "300.0", "300", "0.01", "2000.0")], [30000, 30000, 30000, 1, 200000])
        self.assertEqual([bm.money_cents(v) for v in ("300.001", "abc", None, 300, "1e3")], [None] * 5)

    def test_expiry_is_order_date_plus_24_months_in_singapore(self):
        self.assertEqual(bm.new_expiry_local(date(2026, 11, 18)), NEW_EXPIRY)
        self.assertEqual(bm.new_expiry_local(date(2028, 2, 29)), "2030-02-28T00:00:00")
        # 23:30 UTC on 17 Nov is 18 Nov in Singapore.
        self.assertEqual(bm.sg_date(datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)), date(2026, 11, 18))

    def test_evaluator_is_upgrade_only(self):
        day = date(2026, 11, 18)
        cases = [
            (dict(member_type="GUEST", expiry_local=None), "upgrade", "would_upgrade"),
            (dict(member_type="GUEST", expiry_local="2025-01-31T00:00:00.000"), "upgrade", "would_upgrade"),
            (dict(member_type="GUEST", expiry_local="2029-01-01T00:00:00.000"), "hold", "guest_expiry_already_later"),
            (dict(member_type="MEMBER", expiry_local="2028-01-31T00:00:00.000"), "extend", "would_extend"),
            (dict(member_type="MEMBER", expiry_local="2000-01-01T00:00:00.000"), "extend", "would_extend"),
            (dict(member_type="MEMBER", expiry_local=None), "extend", "would_extend"),
            (dict(member_type="MEMBER", expiry_local=NEW_EXPIRY + ".000"), "no_change", "expiry_already_later"),
            (dict(member_type="MEMBER", expiry_local="2029-01-01T00:00:00.000"), "no_change", "expiry_already_later"),
            (dict(member_type="Default", expiry_local=None), "not_in_scope", "member_not_in_scope"),
            (dict(member_type="VIP", expiry_local=None), "not_in_scope", "member_not_in_scope"),
            (dict(member_type="GUEST", expiry_local=None, is_active="F"), "hold", "member_inactive"),
            (dict(member_type="MEMBER", expiry_local="31/01/2028"), "hold", "expiry_unreadable"),
        ]
        for spec, action, code in cases:
            with self.subTest(spec=spec):
                read = bm.MemberRead(True, **{"is_active": "T", "last_modified_local": LM, **spec})
                verdict = bm.decide(read, day)
                self.assertEqual((verdict.action, verdict.code), (action, code))
                if action in ("upgrade", "extend"):
                    self.assertEqual(verdict.new_expiry_local, NEW_EXPIRY)
        self.assertEqual(bm.decide(bm.MemberRead(False), day).code, "member_not_found")

    def test_write_boundary_in_python_refuses_every_other_move(self):
        good = {"kind": "upgrade", "member_no": sg(1), "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                "expected_expiry_local": None, "new_expiry_local": NEW_EXPIRY, "salt": "a" * 32}
        bm.check_save_payload(good)
        bm.check_save_payload({**good, "kind": "extend", "from_type": "MEMBER", "expected_expiry_local": "2028-01-31T00:00:00.000"})
        cases = {
            "benefit_type_not_allowed": [{"to_type": "GUEST"}, {"to_type": "Default"}, {"to_type": "member"}, {"from_type": "Default"},
                                         {"from_type": "MEMBER"}, {"kind": "extend", "from_type": "GUEST"}],
            "benefit_expiry_not_later": [{"expected_expiry_local": "2029-01-01T00:00:00.000"}, {"expected_expiry_local": NEW_EXPIRY + ".000"},
                                         {"expected_expiry_local": "garbage"}],
            "benefit_payload_invalid": [{"kind": "downgrade"}, {"new_expiry_local": "2028-11-18T10:00:00"}, {"member_no": "bad no"}],
        }
        for code, changes in cases.items():
            for change in changes:
                with self.subTest(change=change):
                    self.assertEqual(self.code(lambda: bm.check_save_payload({**good, **change})), code)
        self.assertEqual(self.code(lambda: bm.check_save_payload({**good, "ExpiryDate": "x"})), "benefit_payload_invalid")

    def test_ledger_checks_pin_the_upgrade_only_shape(self):
        self.engine()
        base = ("w", "r", order_gid(1), "upgrade", sg(1), "GUEST", "MEMBER", LM, None, NEW_EXPIRY, "s", "INTENT", "t", "t", "t")
        sql = ("INSERT INTO writes(write_id, run_id, order_gid, kind, member_no, from_type, to_type, expected_last_modified_local, expected_expiry_local, "
               "new_expiry_local, salt, state, created_at, updated_at, deadline_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        bad = [
            dict(i=6, v="GUEST"), dict(i=5, v="Default"), dict(i=3, v="downgrade"), dict(i=8, v="2029-01-01T00:00:00.000"),
            dict(i=8, v=NEW_EXPIRY + ".000"), dict(i=9, v="2028-11-18T10:00:00"),
        ]
        for index, change in enumerate(bad):
            row = list(base)
            row[0] = f"w{index}"
            row[change["i"]] = change["v"]
            with self.subTest(change=change), self.assertRaises(sqlite3.IntegrityError):
                with self.ledger.tx() as conn:
                    conn.execute(sql, row)


# --------------------------------------------------------------------------
# Closure rows
# --------------------------------------------------------------------------

class ClosureRowTests(Base):
    def test_q01_linked_guest_with_a_300_order_becomes_member_with_expiry(self):
        member_no = self.guest_with_order(1)
        out = self.engine().run(apply=True)
        self.assertEqual(out["saves"], 1)
        m = self.book.members[member_no]
        self.assertEqual((m["type"], m["expiry"]), ("MEMBER", NEW_EXPIRY + ".000"))
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"], row["basis_expiry_local"], row["link_source"]), ("applied", "upgrade", NEW_EXPIRY, "sync155"))
        self.assertEqual(self.book.payloads[0]["kind"], "upgrade")
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchall()[0][0], "DONE")

    def test_q02_299_99_is_below_threshold_with_no_read(self):
        self.guest_with_order(1, lines=(("299.99", 1, 1, False, ()),))
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "below_threshold")
        self.assertEqual((self.book.reads, self.book.saves), (0, 0))

    def test_q03_300_before_discounts_qualifies_though_less_was_paid(self):
        self.guest_with_order(1, lines=(("300.00", 1, 1, False, ("17.00",)),))
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")

    def test_q04_shipping_never_counts(self):
        self.guest_with_order(1, lines=(("295.00", 1, 1, False, ()),), shipping=("15.00",))
        self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["gross_cents"]), ("below_threshold", 29500))
        self.assertEqual(self.book.saves, 0)

    def test_q05_cancelled_test_and_unpaid_orders_are_not_receipts_that_count(self):
        self.guest_with_order(1, cancelled="2026-11-18T03:30:00Z", updated="2026-11-18T03:30:00Z")
        self.guest_with_order(2, test=True)
        self.guest_with_order(3, status="PENDING")
        engine = self.engine()
        engine.run(apply=True)
        self.assertEqual([self.receipt(n)["state"] for n in (1, 2, 3)], ["void", "ignored_test", "ignored_unpaid"])
        self.assertEqual(self.book.saves, 0)
        # Paid later: the same receipt row is re-read and now counts.
        self.advance(minutes=15)
        self.shopify.add(order_node(3, updated="2026-11-18T04:10:00Z"))
        engine.run(apply=True)
        self.assertEqual(self.receipt(3)["state"], "applied")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 3)

    def test_q06_signed_up_customer_without_an_ac2_member_waits_then_applies(self):
        self.shopify.add(order_node(1))
        engine = self.engine()
        out = engine.run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "waiting_member")
        self.assertNotIn("qualified_member_missing", out["alerts"])
        # 48 hours later still no member: one alert, then counts only.
        self.advance(hours=49)
        self.assertIn("qualified_member_missing", engine.run(apply=True)["alerts"])
        self.advance(minutes=15)
        self.assertNotIn("qualified_member_missing", engine.run(apply=True)["alerts"])
        # #155 creates and links the GUEST: applied on the next run.
        self.book.members[sg(1)] = fake_member(sg(1))
        self.link(1)
        self.advance(minutes=15)
        engine.run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")
        self.assertEqual(self.book.members[sg(1)]["type"], "MEMBER")

    def test_q07_a_customer_who_never_signed_up_gets_nothing(self):
        self.guest_with_order(1, customer=customer_node(1, tags=(), registration=None))
        engine = self.engine()
        engine.run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "pending_signup")
        self.advance(hours=49)
        engine.run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "not_signed_up")
        self.assertEqual(self.book.saves, 0)

    def test_q07_sign_up_later_the_same_singapore_day_counts_and_a_later_day_does_not(self):
        # Order 11:00 SGT; sign-up 15:00 SGT the same day -> counts.
        self.guest_with_order(1, customer=customer_node(1, registration="2026-11-18T07:00:00Z"))
        # Sign-up the next Singapore day -> does not count.
        self.guest_with_order(2, customer=customer_node(2, registration="2026-11-18T17:00:00Z"))
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")
        self.assertEqual(self.receipt(2)["state"], "not_signed_up")

    def test_q07_same_day_rule_can_be_switched_off(self):
        self.guest_with_order(1, customer=customer_node(1, registration="2026-11-18T07:00:00Z"))
        self.now = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
        self.engine(same_day_signup_counts=False).run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "not_signed_up")

    def test_q07_sign_up_seen_on_a_later_run_moves_the_receipt_on(self):
        self.guest_with_order(1, customer=customer_node(1, tags=(), registration=None))
        engine = self.engine()
        engine.run(apply=True)
        self.shopify.customers[customer_gid(1)] = customer_node(1, registration="2026-11-18T06:00:00Z")
        self.advance(minutes=15)
        engine.run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")

    def test_q08_member_with_a_legacy_expiry_moves_only_later(self):
        self.guest_with_order(1, member=fake_member(sg(1), member_type="MEMBER", expiry="2028-01-31T00:00:00.000"))
        self.guest_with_order(2, member=fake_member(sg(2), member_type="MEMBER", expiry="2029-01-31T00:00:00.000"))
        self.engine().run(apply=True)
        self.assertEqual(self.book.members[sg(1)]["expiry"], NEW_EXPIRY + ".000")
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("applied", "extend"))
        self.assertEqual(self.book.members[sg(2)]["expiry"], "2029-01-31T00:00:00.000")
        self.assertEqual(self.receipt(2)["state"], "no_change")
        self.assertEqual(self.book.saves, 1)

    def test_q09_refund_leaving_the_basis_at_300_is_recorded_only(self):
        # A unit refund (one of four units) that leaves the basis at S$300
        # (G2 B1: a refund by money with no unit now alerts; see below).
        self.guest_with_order(1, lines=(("100.00", 4, 4, False, ()),))
        engine = self.engine()
        engine.run(apply=True)
        self.advance(days=2)
        self.shopify.add(order_node(1, lines=(("100.00", 4, 3, False, ()),), refunded="100.00", updated="2026-11-20T03:00:00Z"))
        out = engine.run(apply=True)
        self.assertEqual(out["alerts"], [])
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["refunded_cents"]), ("applied", 10000))
        self.assertEqual(self.book.saves, 1)

    def test_q10_q12_refund_taking_the_basis_below_300_alerts_once_and_changes_nothing(self):
        self.guest_with_order(1, lines=(("100.00", 3, 3, False, ()),))
        engine = self.engine()
        engine.run(apply=True)
        self.advance(days=2)
        self.shopify.add(order_node(1, lines=(("100.00", 3, 1, False, ()),), refunded="200.00", updated="2026-11-20T03:00:00Z"))
        out = engine.run(apply=True)
        self.assertEqual(out["alerts"], ["refund_on_basis"])
        self.assertEqual(self.book.members[sg(1)]["type"], "MEMBER")  # never downgraded
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["net_cents"], self.receipt(1)["refunded_cents"]), ("applied", 10000, 20000))
        self.advance(minutes=15)
        self.shopify.add(order_node(1, lines=(("100.00", 3, 1, False, ()),), refunded="200.00", updated="2026-11-20T04:00:00Z"))
        self.assertEqual(engine.run(apply=True)["alerts"], [])
        self.assertEqual(self.book.saves, 1)

    def test_q13_a_refund_read_after_60_days_alerts_as_late(self):
        self.guest_with_order(1, lines=(("100.00", 3, 3, False, ()),))
        engine = self.engine()
        engine.run(apply=True)
        self.advance(days=61)
        self.shopify.add(order_node(1, lines=(("100.00", 3, 1, False, ()),), refunded="200.00", updated="2027-01-18T03:00:00Z"))
        self.assertEqual(engine.run(apply=True)["alerts"], ["late_refund_on_basis"])
        self.assertEqual(self.book.saves, 1)

    def test_refund_seen_before_the_upgrade_is_netted_off(self):
        self.guest_with_order(1, lines=(("100.00", 3, 2, False, ()),), refunded="100.00")
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "below_threshold")
        self.assertEqual(self.book.saves, 0)

    def test_q17_staff_made_member_with_a_blank_expiry_gets_the_receipt_expiry(self):
        self.guest_with_order(1, member=fake_member(sg(1), member_type="MEMBER", expiry="2000-01-01T00:00:00.000"))
        self.engine().run(apply=True)
        self.assertEqual(self.book.members[sg(1)]["expiry"], NEW_EXPIRY + ".000")
        self.assertEqual(self.book.members[sg(1)]["type"], "MEMBER")

    def test_q18_staff_edit_between_read_and_save_skips_and_retries_next_run(self):
        self.guest_with_order(1)
        self.book.staff_edit_before_save = sg(1)
        engine = self.engine()
        out = engine.run(apply=True)
        self.assertEqual((out["saves"], out["counts"].get("changed_since_read")), (0, 1))
        self.assertEqual(self.receipt(1)["state"], "ready")
        self.assertEqual(self.ledger.conn.execute("SELECT state, code FROM writes").fetchall()[0][:], ("SKIPPED", "changed_since_read"))
        self.advance(minutes=15)
        engine.run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")
        self.assertEqual(self.book.payloads[1]["expected_last_modified_local"], "2026-11-18T05:00:00.123")

    def test_q19_default_and_other_types_are_counted_and_never_written(self):
        self.guest_with_order(1, member=fake_member(sg(1), member_type="Default"))
        self.guest_with_order(2, member=fake_member(sg(2), member_type="VIP"))
        out = self.engine().run(apply=True)
        self.assertEqual(out["counts"]["member_not_in_scope"], 2)
        self.assertEqual([self.receipt(n)["state"] for n in (1, 2)], ["not_in_scope", "not_in_scope"])
        self.assertEqual((self.book.saves, self.book.payloads), (0, []))

    def test_q20_uncertain_is_never_re_sent_and_resolves_by_read_back(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_saved"
        engine = self.engine()
        out = engine.run(apply=True)
        self.assertEqual(out["alerts"], ["write_uncertain"])
        self.book.mode = None
        self.assertEqual(self.code(lambda: engine.run(apply=True)), "benefit_uncertain_unresolved")
        self.assertEqual(self.code(lambda: engine.resolve()), "writer_may_be_live")
        self.advance(seconds=PAST_DEADLINE)
        result = engine.resolve()
        self.assertEqual(result["resolved"], {"DONE:resolved_saved": 1})
        self.assertEqual(self.receipt(1)["state"], "applied")
        self.advance(minutes=15)
        engine.run(apply=True)
        self.assertEqual((self.book.saves, len(self.book.payloads)), (1, 1))

    def test_q20_uncertain_that_never_saved_resolves_not_written_then_retries_on_a_fresh_read(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_unsaved"
        engine = self.engine()
        engine.run(apply=True)
        self.book.mode = None
        self.advance(seconds=PAST_DEADLINE)
        self.assertEqual(engine.resolve()["resolved"], {"NOT_WRITTEN:resolved_not_written": 1})
        engine.run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.book.saves), ("applied", 1))

    def test_q20_one_write_per_receipt_even_after_a_crash_before_the_receipt_update(self):
        self.guest_with_order(1)
        engine = self.engine()
        real = self.ledger.update_receipt

        def crash_on_applied(order, **values):
            if values.get("state") == "applied":
                raise RuntimeError("parent killed after the save")
            return real(order, **values)
        with mock.patch.object(self.ledger, "update_receipt", crash_on_applied), self.assertRaises(RuntimeError):
            engine.run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.book.saves), ("ready", 1))
        # Staff put the member back by hand; the same receipt is never sent again.
        self.book.members[sg(1)].update(type="GUEST", expiry=None, last_modified="2026-11-18T06:00:00.000")
        self.advance(minutes=15)
        out = engine.run(apply=True)
        # G2 LATER 1: the DONE write repairs the receipt; still never re-sent.
        self.assertEqual((out["saves"], out["counts"].get("receipt_repaired_applied")), (0, 1))
        self.assertEqual((self.book.saves, len(self.book.payloads)), (1, 1))
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"], row["basis_expiry_local"]), ("applied", "upgrade", NEW_EXPIRY))

    def test_q20_a_crash_inside_the_save_call_is_uncertain(self):
        self.guest_with_order(1)
        self.book.mode = "raise"
        out = self.engine().run(apply=True)
        self.assertEqual(out["stopped"], "uncertain:save_call_failed")

    def test_q21_read_back_with_another_field_changed_stops_and_latches(self):
        self.guest_with_order(1)
        self.guest_with_order(2)
        self.book.mode = "mutate"
        engine = self.engine()
        out = engine.run(apply=True)
        self.assertEqual((out["stopped"], out["alerts"]), ("held:readback_fields_changed", ["write_held"]))
        self.assertEqual(len(self.book.payloads), 1)  # the run stopped at the first
        self.book.mode = None
        self.assertEqual(self.code(lambda: engine.run(apply=True)), "benefit_held_rows_present")

    def test_q22_a_shopify_error_moves_nothing(self):
        self.guest_with_order(1)
        self.shopify.fail_orders = "shopify_http_throttled"
        engine = self.engine()
        self.assertEqual(self.code(lambda: engine.run(apply=True)), "shopify_http_throttled")
        self.assertIsNone(self.ledger.watermark(bm.WATERMARK))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
        self.assertEqual(self.book.reads, 0)
        self.shopify.fail_orders = None
        engine.run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")

    def test_q24_the_same_order_read_again_is_a_no_op(self):
        self.guest_with_order(1)
        engine = self.engine()
        engine.run(apply=True)
        for _ in range(3):
            self.advance(minutes=15)
            self.shopify.add(order_node(1, updated=dl.ts(self.now)))
            out = engine.run(apply=True)
            self.assertEqual(out["saves"], 0)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 1)
        self.assertEqual((self.book.saves, len(self.book.payloads)), (1, 1))

    def test_q24_the_watermark_overlaps_and_starts_at_the_configured_start(self):
        self.guest_with_order(1)
        engine = self.engine()
        engine.run(apply=False)
        first = [c for c in self.shopify.calls if c[0] == "orders"][-1][1]
        self.assertEqual(first, dl.parse_created_at(START) - bm.WATERMARK_MARGIN)
        engine.run(apply=False)
        second = [c for c in self.shopify.calls if c[0] == "orders"][-1][1]
        self.assertEqual(second, dl.parse_created_at(ORDER_AT) - bm.WATERMARK_MARGIN)

    def test_orders_created_before_the_start_never_count(self):
        self.guest_with_order(1, created="2026-11-14T03:00:00Z", updated="2026-11-18T01:00:00Z")
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "before_start")

    def test_q25_a_customer_mapped_to_two_members_blocks_the_write(self):
        self.guest_with_order(1)  # #155 link customer 1 -> sg(1)
        self.book.members[sg(2)] = fake_member(sg(2))
        engine = self.engine()
        self.ledger.add_claim(customer_gid(1), sg(2), "index", "phone_and_name_match")
        out = engine.run(apply=True)
        self.assertIn("link_conflict", out["alerts"])
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "link_conflict"))
        self.assertEqual(self.book.saves, 0)

    def test_q25_a_member_mapped_to_two_customers_blocks_the_write(self):
        self.guest_with_order(1)
        engine = self.engine()
        self.ledger.add_claim(customer_gid(9), sg(1), "index", "phone_and_name_match")
        self.assertIn("link_conflict", engine.run(apply=True)["alerts"])
        self.assertEqual(self.book.saves, 0)

    def test_q26_existing_member_signing_up_with_the_same_mobile_is_claim_linked(self):
        self.guest_with_order(1, linked=False, member=fake_member(sg(1), member_type="MEMBER", expiry="2028-01-31T00:00:00.000", name="Fake Member 1"))
        engine = self.engine()
        out = engine.run(apply=True)
        claim = self.ledger.claim_for_gid(customer_gid(1))
        self.assertEqual((claim["member_no"], claim["source"]), (sg(1), "order"))
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["link_source"]), ("applied", "claim"))
        self.assertEqual(out["counts"]["claim_linked"], 1)
        # #155's ledger is never written: the fake's links are unchanged.
        self.assertEqual(self.links_by_gid, {})

    def test_q26_the_eight_digit_member_no_form_also_claims(self):
        self.book.members[local(1)] = fake_member(local(1), name="Fake Member 1")
        self.shopify.add(order_node(1))
        self.engine().run(apply=True)
        self.assertEqual(self.ledger.claim_for_gid(customer_gid(1))["member_no"], local(1))
        self.assertEqual(self.book.members[local(1)]["type"], "MEMBER")

    def test_q27_name_mismatch_or_shared_phone_is_reviewed_once_and_never_linked(self):
        self.guest_with_order(1, linked=False, member=fake_member(sg(1), name="Someone Else Entirely"))
        # Two Shopify customers share customer 2's phone.
        self.guest_with_order(2, linked=False)
        self.shopify.customers[customer_gid(12)] = customer_node(2, phone="+" + sg(2)) | {"id": customer_gid(12)}
        engine = self.engine()
        out = engine.run(apply=True)
        self.assertEqual(out["counts"].get("claim_needs_review"), 2)
        self.assertEqual((out["counts"].get("claim_name_mismatch"), out["counts"].get("claim_phone_not_unique")), (1, 1))
        self.assertIsNone(self.ledger.claim_for_gid(customer_gid(1)))
        self.assertIsNone(self.ledger.claim_for_gid(customer_gid(2)))
        self.assertEqual(self.book.saves, 0)
        self.advance(minutes=15)
        self.assertNotIn("claim_needs_review", engine.run(apply=True)["alerts"])

    def test_q27_both_member_no_forms_present_is_reviewed(self):
        self.book.members[sg(1)] = fake_member(sg(1))
        self.book.members[local(1)] = fake_member(local(1))
        self.shopify.add(order_node(1))
        out = self.engine().run(apply=True)
        self.assertEqual(out["counts"].get("claim_memberno_conflict"), 1)
        self.assertEqual(self.book.saves, 0)

    def test_q33_switch_off_reports_would_counts_and_writes_nothing(self):
        self.guest_with_order(1)
        self.guest_with_order(2, member=fake_member(sg(2), member_type="MEMBER", expiry="2028-01-31T00:00:00.000"))
        engine = self.engine(benefits_write_enabled=False)
        self.assertEqual(self.code(lambda: engine.run(apply=True)), "benefits_write_disabled")
        out = engine.run(apply=False)
        self.assertEqual((out["counts"]["would_upgrade"], out["counts"]["would_extend"], out["saves"]), (1, 1, 0))
        self.assertEqual((self.book.saves, self.book.payloads), (0, []))
        self.assertEqual(self.receipt(1)["observed"], "would_upgrade")
        # Observe re-reads a member at most once a day.
        reads = self.book.reads
        self.advance(minutes=15)
        engine.run(apply=False)
        self.assertEqual(self.book.reads, reads)
        self.assertEqual(engine.status()["observed"], {"would_upgrade": 1, "would_extend": 1})

    def test_q33_cap_reached_and_stop_file(self):
        for n in range(1, 6):
            self.guest_with_order(n)
        engine = self.engine()
        out = engine.run(apply=True)
        self.assertEqual((out["saves"], out["counts"]["upgrade_cap_reached"]), (3, 1))
        (self.root / bm.STOP_FILE).write_text("", encoding="ascii")
        self.advance(minutes=15)
        out = engine.run(apply=True)
        self.assertEqual((out["saves"], out["stopped"]), (0, "stop_file_present"))
        self.assertEqual(self.book.saves, 3)

    def test_q34_scope_check_fails_before_any_order_read(self):
        self.guest_with_order(1)
        for scopes in (["read_customers"], ["read_customers", "read_orders", "write_customers"], ["read_orders", "read_all_orders", "read_customers"]):
            with self.subTest(scopes=scopes):
                self.shopify.scopes = scopes
                self.assertEqual(self.code(lambda: self.engine().run(apply=False)), "shopify_token_scope_not_expected")
                self.assertNotIn("orders", [c[0] for c in self.shopify.calls])

    def test_q34_an_autocount_guard_failure_refuses_with_nothing_written(self):
        self.guest_with_order(1)
        self.book.fail_read = "bridge_read_failed:host_not_authorised"
        self.assertEqual(self.code(lambda: self.engine().run(apply=True)), "bridge_read_failed:host_not_authorised")
        self.assertEqual((self.book.saves, self.book.payloads), (0, []))

    def test_q35_a_second_run_is_refused_by_the_lock(self):
        self.engine()
        self.assertEqual(self.code(lambda: bm.BenefitsLedger(self.root)), "ledger_locked_by_another_owner")

    def test_member_reads_are_capped_per_run(self):
        for n in range(1, 5):
            self.guest_with_order(n)
        out = self.engine(max_member_reads_per_run=2, benefits_write_enabled=False).run(apply=False)
        self.assertEqual((self.book.reads, out["counts"]["member_reads_capped"]), (2, 1))


# --------------------------------------------------------------------------
# G2 fix round 1 (PR #298 at ceae854): B1-B4 and LATER 1, 2, 6, 8
# --------------------------------------------------------------------------

class G2FixRound1Tests(Base):
    # -- B1: refunds by money ------------------------------------------------
    def test_b1_r1_an_order_refunded_by_money_only_is_held_never_upgraded(self):
        self.guest_with_order(1, status="REFUNDED", refunded="300.00")
        engine = self.engine()
        out = engine.run(apply=True)
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"]), ("held", "refund_unattributed"))
        self.assertEqual(out["alerts"], ["refund_unattributed"])
        self.assertEqual((self.book.saves, self.book.reads, self.book.members[sg(1)]["type"]), (0, 0, "GUEST"))
        self.advance(minutes=15)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-18T04:05:00Z"))
        self.assertEqual(engine.run(apply=True)["alerts"], [])  # once; not re-decided
        self.assertEqual((self.receipt(1)["state"], self.book.saves), ("held", 0))

    def test_b1_a_partial_refund_by_money_before_the_decision_is_held(self):
        # S$320.00 of lines, S$100.00 refunded with no refunded unit.
        self.guest_with_order(1, lines=(("320.00", 1, 1, False, ()),), status="PARTIALLY_REFUNDED", refunded="100.00")
        out = self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "refund_unattributed"))
        self.assertIn("refund_unattributed", out["alerts"])
        self.assertEqual(self.book.saves, 0)

    def test_b1_r1b_a_full_refund_by_money_after_the_upgrade_alerts_once_and_changes_nothing(self):
        self.guest_with_order(1)
        engine = self.engine()
        engine.run(apply=True)
        self.advance(days=2)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-20T03:00:00Z"))
        self.assertEqual(engine.run(apply=True)["alerts"], ["refund_on_basis"])
        self.assertEqual((self.receipt(1)["state"], self.book.members[sg(1)]["type"], self.book.saves), ("applied", "MEMBER", 1))
        self.advance(minutes=15)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-20T04:00:00Z"))
        self.assertEqual(engine.run(apply=True)["alerts"], [])

    def test_b1_a_unit_refund_still_behaves_as_before(self):
        # One of four units refunded with its own money: net S$300.00, qualifies.
        self.guest_with_order(1, lines=(("100.00", 4, 3, False, ()),), status="PARTIALLY_REFUNDED", refunded="100.00")
        # One of three units refunded: net S$200.00, below the threshold (not held).
        self.guest_with_order(2, lines=(("100.00", 3, 2, False, ()),), status="PARTIALLY_REFUNDED", refunded="100.00")
        out = self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(2)["state"]), ("applied", "below_threshold"))
        self.assertNotIn("refund_unattributed", out["alerts"])

    # -- B2: review receipts must not starve the run ---------------------------
    def test_b2_r4_claim_reviews_do_not_starve_a_linked_receipt(self):
        for n in range(1, 11):  # signed up, AC2 member by phone, name does not match
            self.guest_with_order(n, linked=False, member=fake_member(sg(n), name="Someone Else Entirely"))
        self.guest_with_order(20, created="2026-11-18T03:30:00Z")  # newer, #155-linked GUEST
        reads, alerts = [], []
        for _ in range(4):  # a fresh engine per run, as on the VM
            before = self.book.reads
            out = self.engine().run(apply=True)
            reads.append(self.book.reads - before)
            alerts.append(out["alerts"])
            if len(reads) == 1:
                # Decided on the first run: linked ready receipts go first.
                self.assertEqual((self.receipt(20)["state"], out["saves"]), ("applied", 1))
            self.advance(minutes=15)
        self.assertEqual((self.receipt(20)["state"], self.book.members[sg(20)]["type"]), ("applied", "MEMBER"))
        self.assertIn("member_reads_capped", alerts[0])
        self.assertEqual([self.receipt(n)["state"] for n in range(1, 11)], ["held"] * 10)
        self.assertEqual({self.receipt(n)["code"] for n in range(1, 11)}, {"claim_name_mismatch"})
        self.assertEqual(reads[2:], [0, 0])

    def test_b2_a_held_claim_review_reopens_when_a_link_appears(self):
        self.guest_with_order(1, linked=False, member=fake_member(sg(1), name="Someone Else Entirely"))
        engine = self.engine()
        engine.run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "claim_name_mismatch"))
        self.link(1)  # #155 links the customer
        self.advance(minutes=15)
        out = self.engine().run(apply=True)
        self.assertEqual(out["counts"].get("claim_hold_reopened"), 1)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["link_source"]), ("applied", "sync155"))

    def test_b2_member_out_of_scope_and_no_phone_settle(self):
        self.guest_with_order(1, linked=False, member=fake_member(sg(1), member_type="Default"))
        self.shopify.add(order_node(2, customer=customer_node(2, phone=None)))
        out = self.engine().run(apply=True)
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in (1, 2)],
                         [("held", "claim_member_not_in_scope"), ("held", "claim_no_phone")])
        self.assertIn("qualified_member_no_phone", out["alerts"])
        self.advance(minutes=15)
        before = self.book.reads
        self.assertEqual(self.engine().run(apply=True)["alerts"], [])
        self.assertEqual(self.book.reads, before)

    def test_b2_waiting_member_rechecks_back_off_until_a_link_appears(self):
        self.shopify.add(order_node(1))  # signed up, no AC2 member yet
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "waiting_member")
        first = self.book.reads
        for _ in range(3):
            self.advance(minutes=15)
            self.assertEqual(self.engine().run(apply=True)["counts"].get("waiting_member_backoff"), 1)
        self.assertEqual(self.book.reads, first)
        self.advance(hours=6)
        self.engine().run(apply=True)
        self.assertGreater(self.book.reads, first)
        # A #155 link makes it due at once, inside the back-off.
        self.book.members[sg(1)] = fake_member(sg(1))
        self.link(1)
        self.advance(minutes=15)
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")
        self.assertEqual(self.cfg().waiting_member_recheck_hours, 6)

    # -- B3: the order read must not stall ------------------------------------
    def test_b3_r5_a_bulk_update_does_not_stall_new_orders(self):
        for n in range(100, 250):  # 150 small orders updated in the same minute
            self.shopify.add(order_node(n, customer=None, lines=(("10.00", 1, 1, False, ()),), updated=f"2026-11-18T03:30:{n % 60:02d}Z"))
        self.now = datetime(2026, 11, 18, 5, 0, tzinfo=timezone.utc)
        self.guest_with_order(1, updated="2026-11-18T04:50:00Z")  # 80 minutes later
        runs = []
        for _ in range(3):
            out = self.engine().run(apply=True)
            runs.append((out["counts"]["orders_read"], "orders_read_incomplete" in out["alerts"]))
            if self.receipt(1) is not None and self.receipt(1)["state"] == "applied":
                break
            self.advance(minutes=15)
        self.assertEqual(runs[0], (100, True))
        self.assertEqual((self.receipt(1)["state"], len(runs)), ("applied", 2))
        self.assertIsNone(self.ledger.watermark(bm.RESUME_MARK))
        self.assertEqual(self.ledger.watermark(bm.WATERMARK), "2026-11-18T04:50:00Z")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 151)

    def test_b3_a_failed_resume_moves_nothing_and_restarts_from_the_watermark(self):
        for n in range(100, 250):
            self.shopify.add(order_node(n, customer=None, lines=(("10.00", 1, 1, False, ()),), updated="2026-11-18T03:30:00Z"))
        engine = self.engine()
        engine.run(apply=False)
        self.assertIsNotNone(self.ledger.watermark(bm.RESUME_MARK))
        self.shopify.fail_after_cursor = True
        self.assertEqual(self.code(lambda: self.engine().run(apply=False)), "shopify_graphql_error")
        self.assertIsNone(self.ledger.watermark(bm.RESUME_MARK))
        self.assertIsNone(self.ledger.watermark(bm.WATERMARK))  # never moved by an incomplete read
        self.shopify.fail_after_cursor = False
        self.engine().run(apply=False)
        self.engine().run(apply=False)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 150)

    # -- B4: never twice, resolve path ----------------------------------------
    def test_b4_g16_resolve_holds_an_uncertain_save_whose_read_back_shows_another_column_changed(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_mutated"
        engine = self.engine()
        engine.run(apply=True)
        self.advance(seconds=PAST_DEADLINE)
        self.assertEqual(engine.resolve()["resolved"], {"HELD:resolved_unclear": 1})
        self.assertEqual(self.receipt(1)["state"], "ready")
        self.book.mode = None
        self.assertEqual(self.code(lambda: engine.run(apply=True)), "benefit_held_rows_present")

    def test_b4_g17_resolve_holds_while_the_save_child_still_runs(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_unsaved"
        engine = self.engine()
        engine.run(apply=True)
        self.advance(seconds=PAST_DEADLINE)
        self.probe_result = "running"
        self.assertEqual(self.code(lambda: engine.resolve()), "writer_may_be_live")
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "UNCERTAIN")
        self.probe_result = "exited"
        self.assertEqual(engine.resolve()["resolved"], {"NOT_WRITTEN:resolved_not_written": 1})

    # -- LATER 2, 6, 8 -------------------------------------------------------
    def test_later6_a_truncated_phone_search_holds_the_claim_for_review(self):
        self.guest_with_order(1, linked=False)
        self.shopify.truncated_phones.add(sg(1))
        out = self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "claim_phone_not_unique"))
        self.assertIn("claim_needs_review", out["alerts"])
        self.assertIsNone(self.ledger.claim_for_gid(customer_gid(1)))

    def test_later8_g09_the_sign_up_tag_is_required(self):
        self.guest_with_order(1, customer=customer_node(1, tags=("newsletter",)))  # metafield, no member-mg tag
        self.now = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "not_signed_up")
        self.assertEqual(self.book.saves, 0)

    def test_later8_g28_a_claim_refuses_a_member_already_linked_by_155(self):
        self.guest_with_order(1, linked=False)
        self.link(9, sg(1))  # #155 links that member to another customer
        out = self.engine().run(apply=True)
        self.assertIsNone(self.ledger.claim_for_gid(customer_gid(1)))
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "claim_link_conflict"))
        self.assertIn("claim_needs_review", out["alerts"])
        self.assertEqual(self.book.saves, 0)


# --------------------------------------------------------------------------
# G2 fix round 2 (recheck 1 at 86b42c1): B5, B6 and new LATER 1, 2, 3, 9
# --------------------------------------------------------------------------

class G2FixRound2Tests(Base):
    def claim_held(self, n: int = 1) -> bm.BenefitsEngine:
        """A signed-up customer whose AC2 GUEST (found by phone) has another
        name: the S$300 receipt is held by the claim review."""
        self.guest_with_order(n, linked=False, member=fake_member(sg(n), name="Someone Else Entirely"))
        self.engine().run(apply=True)
        self.assertEqual((self.receipt(n)["state"], self.receipt(n)["code"]), ("held", "claim_name_mismatch"))
        return self.engine()

    def link_and_run(self, n: int = 1) -> dict:
        self.link(n)
        self.advance(minutes=15)
        return self.engine().run(apply=True)

    def assert_never_upgraded(self, out: dict, n: int = 1, code: str = "order_no_longer_qualifies") -> None:
        self.assertEqual((out["saves"], self.book.saves, self.book.payloads), (0, 0, []))
        self.assertEqual(self.book.members[sg(n)]["type"], "GUEST")
        self.assertEqual((self.receipt(n)["state"], self.receipt(n)["code"]), ("held", code))
        self.assertIsNone(out["counts"].get("claim_hold_reopened"))

    # -- B5: a claim-held receipt is looked at again before it can re-open ----
    def test_b5_r6a_a_unit_refund_while_claim_held_never_upgrades(self):
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, lines=(("300.00", 1, 0, False, ()),), status="REFUNDED", refunded="300.00", updated="2026-11-19T03:00:00Z"))
        out = self.engine().run(apply=True)
        self.assertEqual((out["alerts"], out["counts"].get("claim_hold_settled")), ([], 1))  # silent: nothing was granted
        self.assertEqual((self.receipt(1)["code"], self.receipt(1)["net_cents"]), ("order_no_longer_qualifies", 0))
        self.assert_never_upgraded(self.link_and_run())

    def test_b5_r6b_a_cancellation_while_claim_held_never_upgrades(self):
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, cancelled="2026-11-19T02:00:00Z", status="VOIDED", updated="2026-11-19T03:00:00Z"))
        self.assertEqual(self.engine().run(apply=True)["alerts"], [])
        self.assert_never_upgraded(self.link_and_run())

    def test_b5_r6c_a_refund_by_money_while_claim_held_alerts_once_and_never_upgrades(self):
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-19T03:00:00Z"))
        self.assertEqual(self.engine().run(apply=True)["alerts"], ["refund_unattributed"])
        self.assertEqual(self.receipt(1)["alerted"], "refund_unattributed")
        self.advance(minutes=15)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-19T04:10:00Z"))
        self.assertEqual(self.engine().run(apply=True)["alerts"], [])  # once
        self.assert_never_upgraded(self.link_and_run(), code="refund_unattributed")

    def test_b5_r6d_no_phone_then_refunded_then_linked_never_upgrades(self):
        self.shopify.add(order_node(1, customer=customer_node(1, phone=None)))
        self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "claim_no_phone"))
        self.advance(days=1)
        self.shopify.add(order_node(1, customer=customer_node(1, phone=None), lines=(("300.00", 1, 0, False, ()),), status="REFUNDED",
                                    refunded="300.00", updated="2026-11-19T03:00:00Z"))
        self.engine().run(apply=True)
        # The customer adds a phone; #155 creates and links the GUEST.
        self.shopify.customers[customer_gid(1)] = customer_node(1)
        self.book.members[sg(1)] = fake_member(sg(1))
        self.assert_never_upgraded(self.link_and_run())

    def test_b5_a_claim_hold_whose_order_still_qualifies_re_opens_as_before(self):
        self.claim_held()
        self.advance(days=1)
        # One of four units refunded with its own money: still S$300.00.
        self.shopify.add(order_node(1, lines=(("100.00", 4, 3, False, ()),), status="PARTIALLY_REFUNDED", refunded="100.00", updated="2026-11-19T03:00:00Z"))
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["code"], "claim_name_mismatch")
        out = self.link_and_run()
        self.assertEqual((out["counts"].get("claim_hold_reopened"), self.receipt(1)["state"], self.book.saves), (1, "applied", 1))

    def test_b5_the_re_open_rests_on_the_order_as_last_read(self):
        # The stored facts decide even when the order is not replayed in the
        # run that sees the link (a newer order has moved the watermark on).
        self.claim_held()
        self.advance(hours=2)
        self.shopify.add(order_node(9, customer=None, lines=(("10.00", 1, 1, False, ()),), created="2026-11-18T05:30:00Z"))
        self.engine().run(apply=True)
        self.ledger.update_receipt(order_gid(1), net_cents=0, financial_status="REFUNDED")
        self.advance(hours=1)
        calls = len(self.shopify.calls)
        out = self.link_and_run()
        self.assertEqual(out["counts"].get("claim_hold_settled"), 1)
        self.assertEqual(out["counts"]["orders_read"], 1)  # order 1 itself was not read again
        self.assertGreater(len(self.shopify.calls), calls)
        self.assert_never_upgraded(out)

    # -- B6: never twice with an open or held write, also in observe ----------
    def test_b6_observe_with_an_uncertain_write_neither_reads_nor_marks_the_receipt(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_saved"
        self.engine().run(apply=True)
        self.book.mode = None
        reads = self.book.reads
        self.advance(minutes=15)
        out = self.engine().run(apply=False)
        row = self.receipt(1)
        self.assertEqual((row["state"], row["basis_expiry_local"], out["counts"].get("write_already_recorded")), ("ready", None, 1))
        self.assertIsNone(out["counts"].get("receipt_repaired_applied"))
        self.assertEqual((self.book.reads, self.book.saves, len(self.book.payloads)), (reads, 1, 1))

    def test_b6_observe_with_a_held_write_neither_reads_nor_marks_the_receipt(self):
        self.guest_with_order(1)
        self.book.mode = "mutate"
        self.engine().run(apply=True)
        self.book.mode = None
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "HELD")
        reads = self.book.reads
        self.advance(minutes=15)
        out = self.engine().run(apply=False)
        row = self.receipt(1)
        self.assertEqual((row["state"], row["basis_expiry_local"], out["counts"].get("write_already_recorded")), ("ready", None, 1))
        self.assertIsNone(out["counts"].get("receipt_repaired_applied"))
        self.assertEqual((self.book.reads, self.book.saves, len(self.book.payloads)), (reads, 1, 1))

    # -- new LATER 1, 2 -------------------------------------------------------
    def test_later1_resolve_leaves_a_receipt_that_was_held_in_between(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_saved"
        self.engine().run(apply=True)
        self.book.mode = None
        # An observe run reads a refund by money before the resolve.
        self.advance(minutes=15)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-18T04:10:00Z"))
        self.assertIn("refund_unattributed", self.engine().run(apply=False)["alerts"])
        self.advance(seconds=PAST_DEADLINE)
        result = self.engine().resolve()
        self.assertEqual((result["resolved"], result.get("receipts_left"), result["alerts"]), ({"DONE:resolved_saved": 1}, 1, ["receipt_left_decided"]))
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "DONE")
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "refund_unattributed"))
        self.assertEqual(self.ledger.open_writes(), [])

    def test_later2_r2b_a_refund_read_in_the_repair_run_still_alerts_on_the_basis(self):
        self.guest_with_order(1, lines=(("100.00", 3, 3, False, ()),))
        engine = self.engine()
        real = self.ledger.update_receipt

        def crash_on_applied(order, **values):
            if values.get("state") == "applied":
                raise RuntimeError("parent killed after the save")
            return real(order, **values)
        with mock.patch.object(self.ledger, "update_receipt", crash_on_applied), self.assertRaises(RuntimeError):
            engine.run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.book.saves), ("ready", 1))
        # The next run reads the refund of one unit (net S$200.00).
        self.advance(minutes=15)
        self.shopify.add(order_node(1, lines=(("100.00", 3, 2, False, ()),), status="PARTIALLY_REFUNDED", refunded="100.00", updated="2026-11-18T04:10:00Z"))
        out = self.engine().run(apply=True)
        self.assertEqual((out["alerts"], out["counts"].get("receipt_repaired_applied")), (["refund_on_basis"], 1))
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"], row["basis_expiry_local"], row["net_cents"]), ("applied", "upgrade", NEW_EXPIRY, 20000))
        self.assertEqual((self.book.saves, len(self.book.payloads)), (1, 1))

    # -- new LATER 3: the receipts trigger ------------------------------------
    def test_later3_the_receipts_trigger_pins_code_and_the_one_way_back(self):
        self.engine()
        counter = iter(range(100, 200))

        def row(state: str, code: str | None) -> str:
            gid = order_gid(next(counter))
            with self.ledger.tx() as conn:
                conn.execute("INSERT INTO receipts(order_gid, order_created_at, order_date_sgt, order_updated_at, state, code, member_no, "
                             "basis_expiry_local, first_seen_at, updated_at) VALUES (?, 't', 'd', 't', ?, ?, 'm', 'e', 't', 't')", (gid, state, code))
            return gid

        def allowed(gid: str, **values) -> bool:
            try:
                self.ledger.update_receipt(gid, **values)
            except sqlite3.IntegrityError as exc:
                self.assertIn("receipt_decided", str(exc))
                return False
            return True

        claim, other = "claim_name_mismatch", "refund_unattributed"
        cases = [
            # (state, code, update, allowed)
            ("held", claim, dict(state="ready", code=None), True),  # the one way back
            ("held", claim, dict(state="applied"), False),
            ("held", claim, dict(state="waiting_member"), False),
            ("held", other, dict(state="ready", code=None), False),
            ("held", other, dict(state="applied"), False),
            ("held", other, dict(state="waiting_member"), False),
            ("held", "link_conflict", dict(state="ready", code=None), False),
            ("held", "order_no_longer_qualifies", dict(state="ready", code=None), False),
            ("held", None, dict(state="ready"), False),  # a NULL code never re-opens
            ("held", claim, dict(code=other), True),  # settled for good
            ("held", claim, dict(code="order_no_longer_qualifies"), True),
            ("held", claim, dict(code="claim_no_phone"), False),
            ("held", claim, dict(code=None), False),
            ("held", other, dict(code=claim), False),  # never back to a re-openable code
            ("held", other, dict(code=None), False),
            ("held", "order_no_longer_qualifies", dict(code=claim), False),
            ("applied", "upgrade", dict(code=None), False),
            ("applied", "upgrade", dict(code=claim), False),
            ("no_change", "expiry_already_later", dict(code=None), False),
            ("not_in_scope", "member_not_in_scope", dict(code=claim), False),
            ("applied", "upgrade", dict(state="ready", code=None), False),
            ("ready", None, dict(state="held", code=claim), True),  # undecided rows move freely
            ("waiting_member", None, dict(state="ready", code=None), True),
        ]
        for state, code, update, want in cases:
            with self.subTest(state=state, code=code, update=update):
                self.assertEqual(allowed(row(state, code), **update), want)
        # Two steps cannot do what one step may not: NULL the code, then re-open.
        gid = row("held", other)
        self.assertFalse(allowed(gid, code=None))
        self.assertFalse(allowed(gid, state="ready"))

    # -- new LATER 9: D12, D25 -------------------------------------------------
    def test_later9_d12_a_claim_link_re_opens_a_held_receipt_and_ends_a_wait(self):
        self.claim_held(1)  # held by a name mismatch
        self.shopify.add(order_node(2))  # signed up, no AC2 member yet
        self.advance(minutes=15)
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(2)["state"], "waiting_member")
        # Staff fix the name and the index claims customer 1; member 2 is keyed and claimed.
        self.ledger.add_claim(customer_gid(1), sg(1), "index", "phone_and_name_match")
        self.book.members[sg(2)] = fake_member(sg(2))
        self.ledger.add_claim(customer_gid(2), sg(2), "index", "phone_and_name_match")
        self.advance(minutes=15)  # inside the 6-hour back-off
        out = self.engine().run(apply=True)
        self.assertEqual(out["counts"].get("claim_hold_reopened"), 1)
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["link_source"]) for n in (1, 2)], [("applied", "claim"), ("applied", "claim")])
        self.assertEqual(self.links_by_gid, {})

    def test_later9_d25_ready_receipts_are_decided_before_waiting_ones(self):
        self.shopify.add(order_node(1))  # older, no AC2 member: waiting_member (2 reads a check)
        self.engine(max_member_reads_per_run=2).run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "waiting_member")
        self.advance(hours=7)  # the waiting receipt is due again
        # A newer, unlinked but claimable GUEST (2 reads: both MemberNo forms).
        self.guest_with_order(2, linked=False, created="2026-11-18T10:30:00Z")
        out = self.engine(max_member_reads_per_run=2).run(apply=True)
        self.assertEqual((self.receipt(2)["state"], out["saves"]), ("applied", 1))
        self.assertIn("member_reads_capped", out["alerts"])
        self.assertEqual(self.receipt(1)["state"], "waiting_member")


# --------------------------------------------------------------------------
# G2 fix round 3 (recheck 2 at eb1fc8b): B8 and the LATER 6 test gaps
# (B7 is the privacy check in Base.tearDown, run after every test)
# --------------------------------------------------------------------------

class G2FixRound3Tests(Base):
    # The round 2 helpers, without running its tests a second time.
    claim_held = G2FixRound2Tests.claim_held
    link_and_run = G2FixRound2Tests.link_and_run
    assert_never_upgraded = G2FixRound2Tests.assert_never_upgraded

    def settled_and_never_upgraded(self, out: dict, n: int = 1) -> None:
        self.assert_never_upgraded(out, n)
        self.assertIsNone(out["counts"].get("would_upgrade"))

    # -- B8: a claim-held receipt must not act for a customer who lost the order
    def test_b8_p06_an_order_moved_to_another_customer_while_held_never_upgrades_the_first(self):
        self.claim_held()
        self.advance(days=1)
        # Staff move the order to customer 2 (signed up too).
        self.shopify.add(order_node(1, customer=customer_node(2), updated="2026-11-19T03:00:00Z"))
        out = self.engine().run(apply=True)
        self.assertEqual((out["alerts"], out["counts"].get("claim_hold_settled")), ([], 1))  # silent
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"], row["customer_gid"]), ("held", "order_no_longer_qualifies", customer_gid(1)))
        # #155 links customer 1: no re-open, no save, the member stays GUEST.
        self.settled_and_never_upgraded(self.link_and_run())
        # P06c: customer 2 is linked later: still nothing (handled by hand).
        self.book.members[sg(2)] = fake_member(sg(2))
        out = self.link_and_run(2)
        self.assertEqual((out["saves"], self.book.saves, self.book.members[sg(2)]["type"]), (0, 0, "GUEST"))

    def test_b8_p06d_in_observe_no_would_upgrade_for_the_first_customer(self):
        self.guest_with_order(1, linked=False, member=fake_member(sg(1), name="Someone Else Entirely"))
        self.engine().run(apply=False)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "claim_name_mismatch"))
        self.advance(days=1)
        self.shopify.add(order_node(1, customer=customer_node(2), updated="2026-11-19T03:00:00Z"))
        self.engine().run(apply=False)
        self.link(1)
        self.advance(minutes=15)
        out = self.engine().run(apply=False)
        self.assertIsNone(out["counts"].get("would_upgrade"))
        self.assertIsNone(out["counts"].get("claim_hold_reopened"))
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"], self.receipt(1)["observed"]), ("held", "order_no_longer_qualifies", None))
        self.assertEqual((self.book.saves, self.book.payloads), (0, []))

    def test_b8_an_order_whose_customer_was_removed_while_held_never_upgrades(self):
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, customer=None, updated="2026-11-19T03:00:00Z"))
        self.assertEqual(self.engine().run(apply=True)["counts"].get("claim_hold_settled"), 1)
        self.settled_and_never_upgraded(self.link_and_run())

    def test_b8_an_order_moved_and_refunded_by_money_settles_silently(self):
        # The order is no longer this customer's: nothing to decide, no alert.
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, customer=customer_node(2), status="REFUNDED", refunded="300.00", updated="2026-11-19T03:00:00Z"))
        out = self.engine().run(apply=True)
        self.assertEqual((out["alerts"], out["counts"].get("claim_hold_settled")), ([], 1))
        self.assertEqual((self.receipt(1)["code"], self.receipt(1)["alerted"]), ("order_no_longer_qualifies", None))
        self.settled_and_never_upgraded(self.link_and_run())

    # -- LATER 6: test gaps on the round 2 code (tests only) ------------------
    def test_e07b_any_other_reason_settles_a_claim_hold_for_good(self):
        # As built (Controller answer 3): a passing state settles it too.
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, status="PARTIALLY_PAID", updated="2026-11-19T03:00:00Z"))
        out = self.engine().run(apply=True)
        # Slice B2 (B2-2): an unpaid status is not a positive fact, so the settle now also alerts once.
        self.assertEqual((out["alerts"], out["counts"].get("claim_hold_settled"), self.receipt(1)["code"]),
                         (["claim_hold_settled_unclear"], 1, "order_no_longer_qualifies"))
        # Paid again later: the hold stays settled, also after the link.
        self.advance(minutes=15)
        self.shopify.add(order_node(1, updated="2026-11-19T03:20:00Z"))
        self.engine().run(apply=True)
        self.settled_and_never_upgraded(self.link_and_run())

    def test_e26_a_cancelled_order_that_is_still_paid_never_re_opens(self):
        # Only the settle code keeps it shut: the stored facts still look paid.
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, cancelled="2026-11-19T02:00:00Z", status="PAID", updated="2026-11-19T03:00:00Z"))
        self.engine().run(apply=True)
        row = self.receipt(1)
        self.assertEqual((row["code"], row["financial_status"], row["net_cents"]), ("order_no_longer_qualifies", "PAID", 30000))
        self.settled_and_never_upgraded(self.link_and_run())

    def test_e09_e10_e11_each_stored_fact_alone_blocks_the_re_open(self):
        facts = {1: dict(financial_status="REFUNDED"), 2: dict(net_cents=0), 3: dict(financial_status="PENDING")}
        for n in facts:
            self.guest_with_order(n, linked=False, member=fake_member(sg(n), name="Someone Else Entirely"))
        self.engine().run(apply=True)
        # A newer order moves the watermark on, so the three are not read again.
        self.advance(hours=2)
        self.shopify.add(order_node(9, customer=None, lines=(("10.00", 1, 1, False, ()),), created="2026-11-18T05:30:00Z"))
        self.engine().run(apply=True)
        for n, change in facts.items():
            self.assertEqual(self.receipt(n)["code"], "claim_name_mismatch")
            self.ledger.update_receipt(order_gid(n), **change)
            self.link(n)
        self.advance(hours=1)
        out = self.engine().run(apply=True)
        self.assertEqual((out["counts"].get("claim_hold_settled"), out["counts"].get("claim_hold_reopened"), out["saves"]), (3, None, 0))
        for n in facts:
            with self.subTest(n=n):
                self.assertEqual((self.receipt(n)["state"], self.receipt(n)["code"], self.book.members[sg(n)]["type"]),
                                 ("held", "order_no_longer_qualifies", "GUEST"))
        self.assertEqual(self.book.saves, 0)

    def test_e13_a_replay_after_resolve_left_the_receipt_changes_nothing(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_saved"
        self.engine().run(apply=True)
        self.book.mode = None
        self.advance(minutes=15)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-18T04:10:00Z"))
        self.engine().run(apply=False)
        self.advance(seconds=PAST_DEADLINE)
        self.assertEqual(self.engine().resolve()["alerts"], ["receipt_left_decided"])
        # The order is read again: a held receipt is never repaired to applied.
        self.advance(minutes=15)
        self.shopify.add(order_node(1, status="REFUNDED", refunded="300.00", updated="2026-11-18T04:40:00Z"))
        out = self.engine().run(apply=True)
        self.assertIsNone(out["counts"].get("receipt_repaired_applied"))
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "refund_unattributed"))
        self.assertEqual((self.book.saves, len(self.book.payloads)), (1, 1))

    def test_e15_the_repair_also_runs_when_the_order_is_not_read_again(self):
        self.now = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
        self.guest_with_order(1)
        # A newer order moves the watermark past order 1 in the crash run itself.
        self.shopify.add(order_node(9, customer=None, lines=(("10.00", 1, 1, False, ()),), created="2026-11-18T05:30:00Z"))
        engine = self.engine()
        real = self.ledger.update_receipt

        def crash_on_applied(order, **values):
            if values.get("state") == "applied":
                raise RuntimeError("parent killed after the save")
            return real(order, **values)
        with mock.patch.object(self.ledger, "update_receipt", crash_on_applied), self.assertRaises(RuntimeError):
            engine.run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.book.saves), ("ready", 1))
        self.advance(minutes=15)
        out = self.engine().run(apply=True)
        self.assertEqual(out["counts"]["orders_read"], 1)  # only the newer order
        self.assertEqual((out["counts"].get("receipt_repaired_applied"), out["saves"]), (1, 0))
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"], row["basis_expiry_local"]), ("applied", "upgrade", NEW_EXPIRY))
        self.assertEqual((self.book.saves, len(self.book.payloads)), (1, 1))

    def test_e23_a_settled_code_never_changes_to_another_code(self):
        self.engine()
        counter = iter(range(300, 400))
        cases = [
            ("refund_unattributed", "order_no_longer_qualifies"), ("order_no_longer_qualifies", "refund_unattributed"),
            ("link_conflict", "order_no_longer_qualifies"), ("member_inactive", "refund_unattributed"),
            ("refund_unattributed", "anything_else"),
        ]
        for old, new in cases:
            gid = order_gid(next(counter))
            with self.ledger.tx() as conn:
                conn.execute("INSERT INTO receipts(order_gid, order_created_at, order_date_sgt, order_updated_at, state, code, first_seen_at, updated_at) "
                             "VALUES (?, 't', 'd', 't', 'held', ?, 't', 't')", (gid, old))
            with self.subTest(old=old, new=new), self.assertRaises(sqlite3.IntegrityError) as caught:
                self.ledger.update_receipt(gid, code=new)
            self.assertIn("receipt_decided", str(caught.exception))
            self.assertEqual(self.ledger.receipt(gid)["code"], old)

    def test_e32_p03_a_refund_and_the_link_read_by_the_same_run_settle_first(self):
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, lines=(("300.00", 1, 0, False, ()),), status="REFUNDED", refunded="300.00", updated="2026-11-19T03:00:00Z"))
        self.link(1)  # the link appears in the same run that reads the refund
        out = self.engine().run(apply=True)
        self.assertEqual((out["counts"].get("claim_hold_settled"), out["counts"].get("claim_hold_reopened")), (1, None))
        self.settled_and_never_upgraded(out)


# --------------------------------------------------------------------------
# Index (one-off claim links for existing members)
# --------------------------------------------------------------------------

class IndexTests(Base):
    def setUp(self):
        super().setUp()
        for n in range(1, 5):
            self.book.members[sg(n)] = fake_member(sg(n), member_type="MEMBER" if n % 2 else "GUEST", name=f"Fake Member {n}")
            self.shopify.customers[customer_gid(n)] = customer_node(n)
        self.book.members["GEN000123"] = fake_member("GEN000123")  # not a phone MemberNo
        self.book.members[sg(5)] = fake_member(sg(5), member_type="Default")
        self.shopify.customers[customer_gid(3)] = customer_node(3, first="Other", last="Person")
        self.link(4)

    def test_dry_run_counts_and_links_nothing(self):
        out = self.engine().index(apply=False, max_members=100)
        self.assertEqual(out["counts"]["index_would_claim"], 2)
        self.assertEqual(out["counts"]["index_would_review"], 1)
        self.assertEqual(out["counts"]["index_already_linked"], 1)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0], 0)
        self.assertNotIn(("orders",), [c[:1] for c in self.shopify.calls])

    def test_apply_claims_only_unique_name_matches_and_resumes(self):
        engine = self.engine()
        out = engine.index(apply=True, max_members=1)
        self.assertEqual(out["counts"]["examined"], 1)
        engine.index(apply=True, max_members=100)
        claims = {r["customer_gid"]: r["member_no"] for r in self.ledger.conn.execute("SELECT * FROM claim_links")}
        self.assertEqual(claims, {customer_gid(1): sg(1), customer_gid(2): sg(2)})
        self.assertEqual(self.ledger.conn.execute("SELECT code FROM reviews").fetchall()[0][0], "claim_name_mismatch")
        self.assertEqual(self.book.saves, 0)
        self.assertEqual(self.code(lambda: engine.index(apply=True, max_members=1001)), "benefits_index_max_invalid")

    def test_later9_d45_a_truncated_phone_search_on_the_index_path_is_a_review(self):
        self.shopify.truncated_phones.add(sg(1))
        out = self.engine().index(apply=True, max_members=100)
        self.assertEqual(out["counts"].get("claim_phone_not_unique"), 1)
        self.assertIn("claim_needs_review", out["alerts"])
        self.assertIsNone(out["counts"].get("index_no_shopify_customer"))
        self.assertIsNone(self.ledger.claim_for_member(sg(1)))
        self.assertEqual(self.ledger.conn.execute("SELECT code FROM reviews WHERE subject = ?", ("m:" + sg(1),)).fetchone()[0], "claim_phone_not_unique")

    def test_later2_both_member_no_forms_are_reviewed_never_linked(self):
        self.book.members[local(1)] = fake_member(local(1), member_type="GUEST", name="Fake Member 1")  # the 8-digit form too
        out = self.engine().index(apply=True, max_members=100)
        claims = {r["member_no_key"] for r in self.ledger.conn.execute("SELECT * FROM claim_links")}
        self.assertNotIn(sg(1), claims)
        self.assertNotIn(local(1), claims)
        self.assertEqual(out["counts"].get("claim_memberno_conflict"), 2)
        self.assertIn("claim_needs_review", out["alerts"])


# --------------------------------------------------------------------------
# Config, state root, #155 ledger, Shopify client, CLI
# --------------------------------------------------------------------------

class ConfigTests(Base):
    def test_example_configs_parse_in_observe_mode(self):
        for name in ("member_benefits.example.json", "member_benefits.live.example.json"):
            with self.subTest(name=name):
                cfg = bm.load_config(ROOT / "config" / name)
                self.assertFalse(cfg.benefits_write_enabled)
                self.assertEqual((cfg.max_upgrades_per_run, cfg.qualify_threshold_cents, cfg.shopify.api_version), (3, 30000, "2026-10"))
                self.assertTrue(cfg.amount_before_discounts and cfg.amount_exclude_shipping and cfg.same_day_signup_counts)
                self.assertIs(cfg.amount_exclude_gift_cards, False)
                self.assertEqual(cfg.gift_card_gateways, ("gift_card",))

    def test_other_tools_configs_are_refused_both_ways(self):
        for name in ("member_sync.example.json", "member_type_move.example.json"):
            with self.subTest(name=name):
                self.assertEqual(self.code(lambda: bm.load_config(ROOT / "config" / name)), "benefits_config_is_other_tool")
        for loader in (sync.load_config, tm.load_config):
            with self.assertRaises(dl.DirectLocalError):
                loader(ROOT / "config" / "member_benefits.example.json")

    def test_bounds_and_refusals(self):
        cases = {
            "benefits_config_max_upgrades_per_run_invalid": [{"max_upgrades_per_run": 21}, {"max_upgrades_per_run": 0}, {"max_upgrades_per_run": True}],
            "benefits_config_qualify_threshold_cents_invalid": [{"qualify_threshold_cents": 99}, {"qualify_threshold_cents": "30000"}],
            "benefits_config_amount_include_tax_unsupported": [{"amount_include_tax": False}],
            "benefits_config_amount_count_sale_items_unsupported": [{"amount_count_sale_items": False}],
            "benefits_config_benefits_write_enabled_invalid": [{"benefits_write_enabled": "yes"}],
            "benefits_config_keys_invalid": [{"target_type": "GUEST"}, {"write_customers": True}],
            "benefits_config_orders_start_utc_invalid": [{"orders_start_utc": "soon"}],
            "benefits_config_shopify_auth_invalid": [{"shopify": {"store_gid": STORE_GID, "shop_domain": SHOP, "api_version": "2026-10", "auth": {"method": "password"}}}],
            "autocount_book_not_authorised": [{"autocount_book": "AED_OTHER"}],
        }
        for code, extras in cases.items():
            for extra in extras:
                with self.subTest(extra=extra):
                    self.assertEqual(self.code(lambda: bm.parse_config(raw_config(self.root, self.sync_root, **extra))), code)
        live = raw_config(self.root, self.sync_root, autocount_book="AED_XBOUNDARIES", shopify={
            "store_gid": "gid://shopify/Shop/48759472277", "shop_domain": "xboundariessg.myshopify.com", "api_version": "2026-10",
            "auth": {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_BENEFITS_CLIENT_ID", "client_secret_env": "XB_SHOPIFY_BENEFITS_CLIENT_SECRET"}},
            ac2_credential_target=sync.LIVE_CREDENTIAL_TARGET, ac2_login_mode="owner_shared")
        self.assertEqual(self.code(lambda: bm.parse_config(live)), "benefits_config_type_move_state_root_required")
        self.assertFalse(bm.parse_config({**live, "benefits_write_enabled": False}).benefits_write_enabled)
        self.assertTrue(bm.parse_config({**live, "type_move_state_root": "C:\\x"}).benefits_write_enabled)

    def test_state_root_never_shared_with_another_tool(self):
        with private_root_allowed(self.temp / "elsewhere"):
            cfg = self.cfg()
            self.assertEqual(bm.state_root(cfg), self.root.resolve())
            for name in (sync.LEDGER_FILE, tm.LEDGER_FILE, "ledger.sqlite3"):
                with self.subTest(name=name):
                    (self.root / name).write_bytes(b"")
                    self.assertEqual(self.code(lambda: bm.state_root(cfg)), "benefits_state_root_shared")
                    (self.root / name).unlink()
            self.assertEqual(self.code(lambda: bm.state_root(self.cfg(member_sync_state_root=str(self.root)))), "benefits_state_root_shared")

    def test_another_tools_ledger_is_refused_under_this_name(self):
        other = self.temp / "other"
        other.mkdir()
        sync.SyncLedger(other, check_custody=False).close()
        (other / sync.LEDGER_FILE).rename(other / bm.LEDGER_FILE)
        self.assertEqual(self.code(lambda: bm.BenefitsLedger(other)), "benefits_ledger_not_recognised")


class SyncLedgerReadOnlyTests(Base):
    def make_sync_ledger(self, *, book="AED_TESTINGAPI", store=STORE_GID, shop=SHOP) -> Path:
        ledger = sync.SyncLedger(self.sync_root, check_custody=False)
        ledger.init_meta(autocount_book=book, store_gid=store, shop_domain=shop)
        with ledger.tx() as conn:
            ledger.add_link(conn, customer_gid(1), sg(1), "matched", "test")
        ledger.close()
        return self.sync_root / sync.LEDGER_FILE

    def test_links_are_read_without_any_write(self):
        path = self.make_sync_ledger()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        links = bm.SyncLinks.load(self.sync_root, self.cfg())
        self.assertEqual(links.by_gid, {customer_gid(1): sg(1)})
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_later8_g29_the_sync_ledger_is_opened_read_only(self):
        self.make_sync_ledger()
        uris = []
        real = sqlite3.connect

        def spy(target, *args, **kwargs):
            uris.append((str(target), kwargs.get("uri")))
            return real(target, *args, **kwargs)
        with mock.patch.object(bm.sqlite3, "connect", spy):
            bm.SyncLinks.load(self.sync_root, self.cfg())
        self.assertEqual(len(uris), 1)
        self.assertTrue(uris[0][0].endswith(sync.LEDGER_FILE + "?mode=ro"))
        self.assertTrue(uris[0][1])

    def test_a_sync_ledger_for_another_store_or_book_is_refused(self):
        self.make_sync_ledger(store="gid://shopify/Shop/2")
        self.assertEqual(self.code(lambda: bm.SyncLinks.load(self.sync_root, self.cfg())), "sync_ledger_binding_mismatch")
        self.assertEqual(self.code(lambda: bm.SyncLinks.load(self.temp / "missing", self.cfg())), "sync_ledger_missing")


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.bodies.append(json.loads(body.decode("utf-8")) if url.endswith("graphql.json") else {"token": True})
        status, value = self.responses.pop(0)
        return direct.HttpResponse(status, {}, json.dumps(value).encode("utf-8"))


class ShopifyClientTests(unittest.TestCase):
    def client(self, responses) -> tuple[bm.BenefitsShopifyClient, FakeTransport]:
        transport = FakeTransport(responses)
        client = bm.BenefitsShopifyClient(shop_domain=SHOP, api_version="2026-10", token_provider=direct.StaticTokenProvider("t" * 32),
                                          transport=transport, sleep=lambda s: None)
        return client, transport

    @staticmethod
    def session(scopes):
        return 200, {"data": {"shop": {"id": STORE_GID, "myshopifyDomain": SHOP}, "currentAppInstallation": {"accessScopes": [{"handle": s} for s in scopes]}}}

    def test_scopes_must_be_exactly_read_orders_and_read_customers(self):
        client, _ = self.client([self.session(["read_orders", "read_customers"])])
        client.verify_session(store_gid=STORE_GID)
        for scopes in (["read_customers"], ["read_orders", "read_customers", "write_customers"], ["read_orders", "read_orders", "read_customers"]):
            with self.subTest(scopes=scopes):
                client, _ = self.client([self.session(scopes)])
                with self.assertRaises(direct.ShopifyDirectError):
                    client.verify_session(store_gid=STORE_GID)

    def test_pages_are_bounded_and_429_backs_off(self):
        page1 = {"data": {"orders": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [order_node(1)]}}}
        page2 = {"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [order_node(2)]}}}
        client, transport = self.client([(429, {}), (200, page1), (200, page2)])
        orders, complete = client.orders(dl.parse_created_at(START), 5)
        self.assertEqual(([o.gid for o in orders], complete), ([order_gid(1), order_gid(2)], True))
        self.assertEqual(transport.bodies[0]["variables"], {"first": bm.SHOPIFY_ORDER_PAGE, "after": None, "search": "updated_at:>='2026-11-15T00:00:00Z'"})
        self.assertEqual(transport.bodies[2]["variables"]["after"], "c1")
        client, _ = self.client([(200, page1)])
        self.assertEqual(len(client.orders(dl.parse_created_at(START), 1)[0]), 1)
        self.assertFalse(client.orders.__self__ is None)

    def test_a_malformed_order_fails_the_read(self):
        bad = order_node(1)
        bad["lineItems"]["nodes"][0]["currentQuantity"] = 5  # more than ordered
        client, _ = self.client([(200, {"data": {"orders": {"pageInfo": {"hasNextPage": False}, "nodes": [bad]}}})])
        with self.assertRaises(direct.ShopifyDirectError):
            client.orders(dl.parse_created_at(START), 1)

    def test_only_pinned_reads_are_sent_and_no_mutation_exists(self):
        client, _ = self.client([])
        with self.assertRaises(direct.ShopifyDirectError) as caught:
            client.execute("mutation X { tagsAdd(id: \"x\", tags: [\"y\"]) { userErrors { message } } }", {})
        self.assertEqual(caught.exception.code, "shopify_query_not_pinned")
        source = (ROOT / "directlocal" / "xb_member_benefits.py").read_text(encoding="utf-8")
        # A GraphQL mutation operation (text, or an operation name with its body).
        self.assertIsNone(re.search(r'"""\s*mutation\b|\bmutation\s+[A-Z][A-Za-z0-9]*\s*[({]|\bmutation\s*\{', source))
        for word in ("tagsAdd", "tagsRemove", "metafieldsSet", "customerUpdate", "customerCreate", "write_customers\"", "write_orders"):
            self.assertNotIn(word, source)
        for query in bm.READ_QUERIES:
            self.assertTrue(query.lstrip().startswith("query "))

    def test_phone_search_drops_other_phones(self):
        nodes = [{"id": customer_gid(1), "firstName": "A", "lastName": "B", "defaultPhoneNumber": {"phoneNumber": "+" + sg(1)}},
                 {"id": customer_gid(2), "firstName": "A", "lastName": "B", "defaultPhoneNumber": {"phoneNumber": "+" + sg(2)}},
                 {"id": customer_gid(3), "firstName": "A", "lastName": "B", "defaultPhoneNumber": None}]
        client, transport = self.client([(200, {"data": {"customers": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}})])
        self.assertEqual([c.gid for c in client.find_by_phone(sg(1))], [customer_gid(1)])
        self.assertEqual(transport.bodies[0]["variables"], {"search": "phone:+" + sg(1)})

    def test_later6_a_truncated_phone_search_is_never_exactly_one(self):
        nodes = [{"id": customer_gid(1), "firstName": "A", "lastName": "B", "defaultPhoneNumber": {"phoneNumber": "+" + sg(1)}}]
        for page_info in ({"hasNextPage": True}, {}):
            with self.subTest(page_info=page_info):
                client, _ = self.client([(200, {"data": {"customers": {"pageInfo": page_info, "nodes": nodes}}})])
                with self.assertRaises(bm.PhoneCandidatesTruncated):
                    client.find_by_phone(sg(1))
        self.assertIn("pageInfo { hasNextPage }", bm.PHONE_LOOKUP_QUERY)

    def test_b3_a_read_at_the_page_cap_keeps_the_cursor(self):
        page = {"data": {"orders": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [order_node(1)]}}}
        client, transport = self.client([(200, page)])
        orders, complete = client.orders(dl.parse_created_at(START), 1)
        self.assertEqual((len(orders), complete, client.last_end_cursor), (1, False, "c1"))
        last = {"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [order_node(2)]}}}
        client, transport = self.client([(200, last)])
        orders, complete = client.orders(dl.parse_created_at(START), 1, after="c1")
        self.assertEqual((complete, client.last_end_cursor, transport.bodies[0]["variables"]["after"]), (True, None, "c1"))


class TokenTests(unittest.TestCase):
    def test_client_credentials_token_must_hold_exactly_the_two_scopes(self):
        for scope, ok in (("read_customers,read_orders", True), ("read_customers", False), ("read_customers,read_orders,write_customers", False)):
            with self.subTest(scope=scope):
                transport = FakeTransport([(200, {"access_token": "t" * 32, "expires_in": 86399, "scope": scope})])
                provider = bm.BenefitsTokenProvider(SHOP, "client-id-0000000", "client-secret-000", transport=transport,
                                                    clock=lambda: datetime(2026, 11, 18, tzinfo=timezone.utc))
                if ok:
                    self.assertEqual(provider.token(), "t" * 32)
                else:
                    with self.assertRaises(direct.ShopifyDirectError):
                        provider.token()


class TypeMoveLockTests(Base):
    def test_apply_and_the_type_move_never_hold_the_lock_together(self):
        move_root = self.temp / "type-move"
        move_root.mkdir()
        (move_root / tm.LEDGER_FILE).write_bytes(b"")
        cfg = self.cfg(type_move_state_root=str(move_root))
        with bm.type_move_lock(cfg):
            with self.assertRaises(dl.DirectLocalError) as caught:
                dl._OwnerLock(move_root / tm.LOCK_FILE)  # what MoveLedger takes first
            self.assertEqual(caught.exception.code, "ledger_locked_by_another_owner")
        held = dl._OwnerLock(move_root / tm.LOCK_FILE)
        try:
            with self.assertRaises(dl.DirectLocalError) as caught:
                with bm.type_move_lock(cfg):
                    pass
            self.assertEqual(caught.exception.code, "type_move_running")
        finally:
            held.release()
        self.assertEqual(self.code(lambda: bm.type_move_lock(self.cfg(type_move_state_root=str(self.temp / "nope"))).__enter__()), "type_move_state_root_invalid")


class CliTests(Base):
    def run_main(self, *args, cfg: dict | None = None, factory=None) -> tuple[int, dict, str]:
        path = self.temp / "config.json"
        path.write_text(json.dumps(raw_config(self.root, self.sync_root, **(cfg or {}))), encoding="utf-8")
        out = io.StringIO()

        def default_factory(config, ledger):
            return bm.BenefitsEngine(config, ledger, shopify=self.shopify, ac2=self.book, sync_links=self.sync_links,
                                     writer_probe=lambda pid, created: "exited")
        with private_root_allowed(self.temp / "elsewhere"), contextlib.redirect_stdout(out):
            code = bm.main([args[0], "--config", str(path), *args[1:]], engine_factory=factory or default_factory, clock=lambda: self.now)
        text = out.getvalue()
        return code, json.loads(text.strip().splitlines()[-1]), text

    def test_full_flow_prints_no_customer_data_or_full_member_number(self):
        self.guest_with_order(1)
        self.guest_with_order(2, linked=False, member=fake_member(sg(2), name="Fake Member 2"))
        texts = []
        for args in (("check-config",), ("init",), ("observe",), ("apply",), ("status",), ("digest",), ("index",), ("resolve",)):
            code, out, text = self.run_main(*args)
            texts.append(text)
            with self.subTest(args=args):
                self.assertIn(code, (0, 3), out)
                self.assertNotEqual(out.get("status"), "refused", out)
        joined = "".join(texts)
        for n in (1, 2):
            for secret in (sg(n), local(n), customer_gid(n), order_gid(n), f"Fake Member {n}", "fake@example.com"):
                self.assertNotIn(secret, joined)
        self.assertIn(bm.mask(sg(1)), joined)
        digest = json.loads(texts[5].strip())
        self.assertEqual(digest["receipts"], {"applied": 2})

    def test_b4_g35_refund_and_unverifiable_paths_carry_keyed_refs_only(self):
        self.guest_with_order(1)  # applied, then refunded below the threshold
        self.guest_with_order(2, taxes=False)  # amount_unverifiable
        self.guest_with_order(3, status="REFUNDED", refunded="300.00")  # refund_unattributed
        texts = [self.run_main("init")[2], self.run_main("apply")[2]]
        self.advance(days=2)
        self.shopify.add(order_node(1, lines=(("300.00", 1, 0, False, ()),), refunded="300.00", updated="2026-11-20T03:00:00Z"))
        code, out, text = self.run_main("apply")
        texts.append(text)
        joined = "".join(texts)
        for n in (1, 2, 3):
            self.assertNotIn(order_gid(n), joined)
            self.assertNotIn(str(order_number(n)), joined)
            self.assertNotIn(str(customer_number(n)), joined)
        self.assertIn("refund_on_basis", out["alerts"])
        codes = {r["code"] for r in json.loads(texts[1].strip().splitlines()[-1])["refs"]} | {r["code"] for r in out["refs"]}
        self.assertLessEqual({"refund_on_basis", "order_amount_unverifiable", "refund_unattributed"}, codes)
        for entry in out["refs"]:
            self.assertRegex(entry["ref"], r"^[ocm]-[0-9a-f]{20}$")

    def test_b6_re_open_repair_and_index_review_carry_keyed_refs_only(self):
        # Re-open: order 1 is held by a name mismatch, then #155 links the customer.
        self.guest_with_order(1, linked=False, member=fake_member(sg(1), name="Someone Else Entirely"))
        texts = [self.run_main("init")[2], self.run_main("apply")[2]]
        # Repair: order 2's save lands, the parent stops before the receipt update.
        self.guest_with_order(2)

        def crashing(config, ledger):
            real = ledger.update_receipt

            def crash_on_applied(order, **values):
                if values.get("state") == "applied":
                    raise RuntimeError("parent killed after the save")
                return real(order, **values)
            ledger.update_receipt = crash_on_applied
            return bm.BenefitsEngine(config, ledger, shopify=self.shopify, ac2=self.book, sync_links=self.sync_links)
        self.advance(minutes=15)
        with self.assertRaises(RuntimeError):
            self.run_main("apply", factory=crashing)
        self.assertEqual(self.book.saves, 1)
        self.link(1)
        self.advance(minutes=15)
        code, run, text = self.run_main("apply")
        texts.append(text)
        # Index review: both MemberNo forms of one phone exist in AC2.
        self.book.members[sg(3)] = fake_member(sg(3), name="Fake Member 3")
        self.book.members[local(3)] = fake_member(local(3), name="Fake Member 3")
        self.shopify.customers[customer_gid(3)] = customer_node(3)
        code, index, text = self.run_main("index", "--apply")
        texts.append(text)
        codes = {r["code"] for r in run["refs"]} | {r["code"] for r in index["refs"]}
        self.assertLessEqual({"claim_hold_reopened", "receipt_repaired_applied", "claim_memberno_conflict", "claim_needs_review"}, codes)
        for entry in run["refs"] + index["refs"]:
            self.assertRegex(entry["ref"], r"^[ocm]-[0-9a-f]{20}$")
            if "member_no_masked" in entry:
                self.assertRegex(entry["member_no_masked"], r"^\*{3,}[^*]{1,3}$")
        joined = "".join(texts)
        for n in (1, 2, 3):
            for secret in (order_gid(n), customer_gid(n), str(order_number(n)), str(customer_number(n)), sg(n), local(n), f"Fake Member {n}"):
                self.assertNotIn(secret, joined)
        self.assertEqual((self.book.saves, len(self.book.payloads)), (2, 2))

    def test_apply_with_the_switch_off_refuses_before_any_ledger_or_login(self):
        def factory(config, ledger):
            raise AssertionError("engine built")
        self.assertEqual(self.run_main("apply", cfg={"benefits_write_enabled": False}, factory=factory)[:2],
                         (2, {"status": "refused", "code": "benefits_write_disabled"}))
        self.assertFalse((self.root / bm.LEDGER_FILE).exists())

    def test_a_ledger_for_another_store_is_refused(self):
        self.run_main("init")
        other = {"shopify": {"store_gid": "gid://shopify/Shop/2", "shop_domain": SHOP, "api_version": "2026-10",
                             "auth": {"method": "client_credentials", "client_id_env": "XB_SHOPIFY_BENEFITS_CLIENT_ID", "client_secret_env": "XB_SHOPIFY_BENEFITS_CLIENT_SECRET"}}}
        self.assertEqual(self.run_main("status", cfg=other)[:2], (2, {"status": "refused", "code": "benefits_ledger_binding_mismatch"}))


# --------------------------------------------------------------------------
# #268 slice A: the gift card rule (owner, 10 Nov 2026). Buying a gift card
# counts towards the S$300; the part of an order paid with a gift card does
# not. Plus the three B9 tests of the last review (G2 recheck 3).
# --------------------------------------------------------------------------

def stored_receipt(root: Path, n: int, column: str):
    """One receipt column read straight from the ledger file (for tests that
    run the CLI, where the ledger is opened and closed by ``main``)."""
    conn = sqlite3.connect((root / bm.LEDGER_FILE).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT {column} FROM receipts WHERE order_gid = ?", (order_gid(n),)).fetchone()[0]
    finally:
        conn.close()


class GiftCardRuleTests(Base):
    run_main = CliTests.run_main

    def measured(self, lines, transactions, shipping=(), **cfg):
        order = bm.order_from_node(order_node(1, lines=lines, shipping=shipping, transactions=transactions))
        config = self.cfg(**cfg)
        return bm.gift_card_paid(order, config), bm.qualifying_amount(order, config)

    # -- the pure rule ---------------------------------------------------------
    def test_worked_examples_of_the_design(self):
        # G0 round 2 plan, 2.2: goods kept, gift card paid, qualifying amount, result.
        cases = [
            # (name, lines, transactions, gift card paid, (gross, qualifying), qualifies)
            ("gift_card_300_bought_and_paid_by_card", (("300.00", 1, 1, True, ()),), (card("300.00"),), 0, (30000, 30000), True),
            ("goods_500_with_a_17_code_paid_by_card", (("500.00", 1, 1, False, ("17.00",)),), (card("483.00"),), 0, (50000, 50000), True),
            ("goods_400_with_150_by_gift_card", (("400.00", 1, 1, False, ()),), (tx("150.00"), card("250.00")), 15000, (40000, 25000), False),
            ("goods_500_with_200_by_gift_card", (("500.00", 1, 1, False, ()),), (tx("200.00"), card("300.00")), 20000, (50000, 30000), True),
            ("goods_320_all_by_gift_card", (("320.00", 1, 1, False, ()),), (tx("320.00"),), 32000, (32000, 0), False),
            ("goods_350_with_one_80_item_returned", (("270.00", 1, 1, False, ()), ("80.00", 1, 0, False, ())),
             (card("350.00"), card("80.00", kind="REFUND")), 0, (35000, 27000), False),
        ]
        for name, lines, transactions, paid, amounts, result in cases:
            with self.subTest(name=name):
                self.assertEqual(self.measured(lines, transactions), (paid, amounts))
                self.assertIs(bm.qualifies(amounts[1], self.cfg()), result)

    def test_gift_card_amount_table(self):
        goods_400 = (("400.00", 1, 1, False, ()),)
        with_gift_line = (("250.00", 1, 1, False, ()), ("100.00", 1, 1, True, ()))
        cases = [
            # (name, lines, shipping, transactions, config, gift card paid, (gross, qualifying))
            ("gift_card_line_plus_other_goods", with_gift_line, (), (card("350.00"),), {}, 0, (35000, 35000)),
            ("split_payment_plus_unit_refund_still_300", (("100.00", 6, 5, False, ()),), (),
             (tx("200.00"), card("400.00"), card("100.00", kind="REFUND")), {}, 20000, (60000, 30000)),
            ("split_payment_plus_unit_refund_below", (("100.00", 5, 4, False, ()),), (),
             (tx("150.00"), card("350.00"), card("100.00", kind="REFUND")), {}, 15000, (50000, 25000)),
            ("gift_card_payment_larger_than_the_goods", (("100.00", 1, 1, False, ()),), ("15.00",), (tx("115.00"),), {}, 11500, (10000, 0)),
            ("two_gift_cards_are_summed", goods_400, (), (tx("100.00"), tx("50.00"), card("250.00")), {}, 15000, (40000, 25000)),
            ("a_capture_counts_as_a_sale_does", goods_400, (), (tx("150.00", kind="CAPTURE"), card("250.00")), {}, 15000, (40000, 25000)),
            ("a_failed_gift_card_payment_is_ignored", goods_400, (), (tx("150.00", status="FAILURE"), card("400.00")), {}, 0, (40000, 40000)),
            ("an_errored_gift_card_payment_is_ignored", goods_400, (), (tx("150.00", status="ERROR"), card("400.00")), {}, 0, (40000, 40000)),
            ("a_refund_back_to_a_gift_card_is_not_added_back", goods_400, (), (tx("150.00"), tx("150.00", kind="REFUND"), card("250.00")), {},
             15000, (40000, 25000)),
            ("a_pending_refund_to_a_gift_card_is_ignored", goods_400, (), (tx("150.00", kind="REFUND", status="PENDING"), card("400.00")), {},
             0, (40000, 40000)),
            ("a_pending_card_payment_is_not_a_gift_card_matter", goods_400, (), (card("400.00", status="PENDING"),), {}, 0, (40000, 40000)),
            ("the_gateway_is_compared_in_lower_case", goods_400, (), (tx("150.00", gateway="GIFT_CARD"), card("250.00")), {}, 15000, (40000, 25000)),
            ("the_gateway_in_mixed_case", goods_400, (), (tx("150.00", gateway="Gift_Card"), card("250.00")), {}, 15000, (40000, 25000)),
            ("another_gateway_is_not_a_gift_card", goods_400, (), (tx("150.00", gateway="store_credit"), card("250.00")), {}, 0, (40000, 40000)),
            ("a_payment_without_a_gateway_is_not_a_gift_card", goods_400, (), (tx("150.00", gateway=None), card("250.00")), {}, 0, (40000, 40000)),
            ("a_second_gateway_name_from_the_setting", goods_400, (), (tx("100.00"), tx("50.00", gateway="Store_Credit"), card("250.00")),
             {"gift_card_gateways": ["gift_card", "store_credit"]}, 15000, (40000, 25000)),
            ("only_the_names_in_the_setting", goods_400, (), (tx("100.00"), tx("50.00", gateway="store_credit"), card("250.00")),
             {"gift_card_gateways": ["store_credit"]}, 5000, (40000, 35000)),
            ("the_exclude_key_still_takes_gift_card_lines_off", with_gift_line, (), (card("350.00"),), {"amount_exclude_gift_cards": True},
             0, (25000, 25000)),
            ("the_exclude_key_with_a_gift_card_payment", with_gift_line, (), (tx("50.00"), card("300.00")), {"amount_exclude_gift_cards": True},
             5000, (25000, 20000)),
        ]
        for kind in ("AUTHORIZATION", "VOID", "CHANGE", "EMV_AUTHORIZATION", "SUGGESTED_REFUND"):
            cases.append((f"kind_{kind.lower()}_is_ignored", goods_400, (), (tx("150.00", kind=kind), card("400.00")), {}, 0, (40000, 40000)))
        for name, lines, shipping, transactions, cfg, paid, amounts in cases:
            with self.subTest(name=name):
                self.assertEqual(self.measured(lines, transactions, shipping, **cfg), (paid, amounts))

    def test_payments_that_cannot_be_read_make_the_order_unmeasurable(self):
        goods_400 = (("400.00", 1, 1, False, ()),)
        unreadable = {
            "pending": (tx("150.00", status="PENDING"), card("250.00")),
            "awaiting_response": (tx("150.00", status="AWAITING_RESPONSE"), card("250.00")),
            "unknown": (tx("150.00", status="UNKNOWN"), card("250.00")),
            "a_status_this_tool_does_not_know": (tx("150.00", status="SOMETHING_NEW"), card("250.00")),
            "a_pending_capture": (tx("150.00", kind="CAPTURE", status="PENDING"),),
            "a_full_list_of_10": tuple(card("40.00") for _ in range(10)),
            "more_than_10": tuple(card("16.00") for _ in range(11)),
            "gift_card_amount_not_sgd": (tx("150.00", currency="USD"), card("250.00")),
            "card_amount_not_sgd": (tx("150.00"), card("250.00", currency="USD")),
            "amount_malformed": (tx("abc"), card("250.00")),
            "amount_with_three_places": (tx("150.001"), card("250.00")),
            "amount_negative": (tx("-150.00"), card("250.00")),
            "amount_set_missing": ({**tx("150.00"), "amountSet": None}, card("250.00")),
            "shop_money_missing": ({**tx("150.00"), "amountSet": {}}, card("250.00")),
        }
        for name, transactions in unreadable.items():
            with self.subTest(name=name):
                self.assertEqual(self.measured(goods_400, transactions), (None, None))
        # One short of a full list is read as usual.
        self.assertEqual(self.measured(goods_400, tuple(card("16.00") for _ in range(8)) + (tx("150.00"),)), (15000, (40000, 25000)))
        self.assertEqual(bm.SHOPIFY_PAYMENT_PAGE, 10)

    def test_a_malformed_transaction_list_fails_the_read(self):
        missing = order_node(1)
        del missing["transactions"]
        bad_nodes = [
            missing,
            {**order_node(1), "transactions": {"nodes": []}},
            {**order_node(1), "transactions": ["gift_card"]},
            {**order_node(1), "transactions": [{**tx("1.00"), "kind": None}]},
            {**order_node(1), "transactions": [{**tx("1.00"), "status": 7}]},
            {**order_node(1), "transactions": [{**tx("1.00"), "gateway": 7}]},
        ]
        for index, bad in enumerate(bad_nodes):
            with self.subTest(index=index):
                with self.assertRaises(direct.ShopifyDirectError) as caught:
                    bm.order_from_node(bad)
                self.assertEqual(caught.exception.code, "shopify_response_shape_invalid")

    def test_the_orders_query_reads_the_transactions_and_the_scopes_are_unchanged(self):
        self.assertIn("transactions(first: 10) { gateway kind status amountSet { shopMoney { amount currencyCode } } }", bm.ORDERS_QUERY)
        self.assertIn("transactionsCount { count precision }", bm.ORDERS_QUERY)  # slice B1 (M1)
        self.assertEqual(bm.REQUIRED_SCOPES, frozenset({"read_orders", "read_customers"}))
        page = {"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [order_node(1, transactions=(tx("150.00", gateway="Gift_Card"), card("150.00", kind="CAPTURE")))]}}}
        transport = FakeTransport([(200, page)])
        client = bm.BenefitsShopifyClient(shop_domain=SHOP, api_version="2026-10", token_provider=direct.StaticTokenProvider("t" * 32),
                                          transport=transport, sleep=lambda s: None)
        orders, complete = client.orders(dl.parse_created_at(START), 1)
        self.assertEqual(transport.bodies[0]["query"], bm.ORDERS_QUERY)
        self.assertEqual((orders[0].payments, orders[0].payments_complete, complete),
                         ((bm.Payment("gift_card", "SALE", "SUCCESS", 15000), bm.Payment("shopify_payments", "CAPTURE", "SUCCESS", 15000)), True, True))

    # -- settings --------------------------------------------------------------
    def test_gift_card_settings(self):
        cfg = self.cfg()
        self.assertIs(cfg.amount_exclude_gift_cards, False)
        self.assertIs(bm._CONFIG_DEFAULTS["amount_exclude_gift_cards"], False)
        self.assertEqual((cfg.gift_card_gateways, bm._CONFIG_DEFAULTS["gift_card_gateways"]), (("gift_card",), ["gift_card"]))
        self.assertEqual((cfg.summary()["amount_exclude_gift_cards"], cfg.summary()["gift_card_gateways"]), (False, ["gift_card"]))
        five = ["gift_card", "store_credit", "a", "b_2", "c" * 64]
        self.assertEqual(self.cfg(gift_card_gateways=five).gift_card_gateways, tuple(five))
        self.assertIs(self.cfg(amount_exclude_gift_cards=True).amount_exclude_gift_cards, True)
        refused = [[], five + ["d"], ["Gift_Card"], ["gift card"], ["gift-card"], [""], ["c" * 65], "gift_card", "abc", "a", [7], [None], None,
                   {"gift_card": True}, {"a": 1}]
        for value in refused:
            with self.subTest(value=value):
                self.assertEqual(self.code(lambda: self.cfg(gift_card_gateways=value)), "benefits_config_gift_card_gateways_invalid")
        self.assertEqual(self.code(lambda: self.cfg(amount_exclude_gift_cards="no")), "benefits_config_amount_exclude_gift_cards_invalid")

    # -- through the engine ----------------------------------------------------
    def test_the_first_decision_uses_the_rule(self):
        orders = {
            # n: (lines, transactions, order status, refunded, state, gift card cents, net cents)
            1: ((("300.00", 1, 1, True, ()),), (card("300.00"),), "PAID", "0.00", "applied", 0, 30000),
            2: ((("500.00", 1, 1, False, ("17.00",)),), (card("483.00"),), "PAID", "0.00", "applied", 0, 50000),
            3: ((("400.00", 1, 1, False, ()),), (tx("150.00"), card("250.00")), "PAID", "0.00", "below_threshold", 15000, 25000),
            4: ((("500.00", 1, 1, False, ()),), (tx("200.00"), card("300.00")), "PAID", "0.00", "applied", 20000, 30000),
            5: ((("320.00", 1, 1, False, ()),), (tx("320.00"),), "PAID", "0.00", "below_threshold", 32000, 0),
            6: ((("270.00", 1, 1, False, ()), ("80.00", 1, 0, False, ())), (card("350.00"), card("80.00", kind="REFUND")),
                "PARTIALLY_REFUNDED", "80.00", "below_threshold", 0, 27000),
        }
        for n, (lines, transactions, status, refunded, *_) in orders.items():
            self.guest_with_order(n, lines=lines, transactions=transactions, status=status, refunded=refunded)
        out = self.engine().run(apply=True)
        self.assertEqual((out["saves"], out["alerts"]), (3, []))
        for n, (*_, state, paid, net) in orders.items():
            with self.subTest(n=n):
                row = self.receipt(n)
                self.assertEqual((row["state"], row["gift_card_cents"], row["net_cents"]), (state, paid, net))
                self.assertEqual(self.book.members[sg(n)]["type"], "MEMBER" if state == "applied" else "GUEST")
        self.assertEqual(json.loads(self.receipt(4)["payment_gateways"]), ["gift_card", "shopify_payments"])
        self.assertEqual(self.book.saves, 3)

    def test_unreadable_payments_hold_the_order_with_the_existing_alert(self):
        goods_500 = (("500.00", 1, 1, False, ()),)
        self.guest_with_order(1, lines=goods_500, transactions=(tx("200.00", status="PENDING"), card("300.00")))
        self.guest_with_order(2, lines=goods_500, transactions=tuple(card("20.00") for _ in range(25)))
        self.guest_with_order(3, lines=goods_500, transactions=(tx("200.00", currency="USD"), card("300.00")))
        # The older codes keep their place when the goods cannot be measured either.
        self.guest_with_order(4, lines=goods_500, taxes=False, transactions=(tx("200.00", status="PENDING"),))
        self.guest_with_order(5, lines=goods_500, more_lines=True, transactions=(tx("200.00", status="PENDING"),))
        engine = self.engine()
        out = engine.run(apply=True)
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in range(1, 6)],
                         [("amount_unverifiable", "payments_unreadable")] * 3
                         + [("amount_unverifiable", "taxes_not_included"), ("amount_unverifiable", "currency_or_lines")])
        self.assertEqual((out["alerts"], out["counts"]["order_amount_unverifiable"]), (["order_amount_unverifiable"], 5))
        self.assertEqual([(self.receipt(n)["net_cents"], self.receipt(n)["gift_card_cents"]) for n in (1, 2, 3)], [(None, None)] * 3)
        self.assertEqual((self.book.reads, self.book.saves, out["saves"]), (0, 0, 0))
        # The pending gift card payment settles: the same receipt is measured and decided.
        self.advance(minutes=15)
        self.shopify.add(order_node(1, lines=goods_500, transactions=(tx("200.00"), card("300.00")), updated="2026-11-18T04:10:00Z"))
        engine.run(apply=True)
        row = self.receipt(1)
        self.assertEqual((row["state"], row["net_cents"], row["gift_card_cents"]), ("applied", 30000, 20000))

    def test_a_held_claim_receipt_whose_order_becomes_gift_card_paid_settles_for_good(self):
        self.claim_held()
        self.advance(days=1)
        # Read again: S$100.00 of the S$300.00 order is now paid by gift card.
        self.shopify.add(order_node(1, transactions=(tx("100.00"), card("200.00")), updated="2026-11-19T03:00:00Z"))
        out = self.engine().run(apply=True)
        self.assertEqual((out["alerts"], out["counts"].get("claim_hold_settled")), ([], 1))  # silent: nothing was granted
        row = self.receipt(1)
        self.assertEqual((row["code"], row["net_cents"], row["gift_card_cents"]), ("order_no_longer_qualifies", 20000, 10000))
        self.assert_never_upgraded(self.link_and_run())

    def test_a_held_claim_receipt_with_a_gift_card_payment_that_still_qualifies_re_opens(self):
        self.guest_with_order(1, linked=False, member=fake_member(sg(1), name="Someone Else Entirely"), lines=(("500.00", 1, 1, False, ()),),
                              transactions=(tx("200.00"), card("300.00")))
        self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "claim_name_mismatch"))
        out = self.link_and_run()
        self.assertEqual((out["counts"].get("claim_hold_reopened"), self.receipt(1)["state"], self.book.saves), (1, "applied", 1))

    def test_an_applied_receipt_is_not_alerted_for_a_gift_card_payment_that_was_already_there(self):
        goods_500 = (("500.00", 1, 1, False, ()),)
        paid = (tx("200.00"), card("300.00"))
        self.guest_with_order(1, lines=goods_500, transactions=paid)
        engine = self.engine()
        engine.run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["gift_card_cents"]), ("applied", 20000))
        for _ in range(2):
            self.advance(minutes=15)
            self.shopify.add(order_node(1, lines=goods_500, transactions=paid, updated=dl.ts(self.now)))
            out = engine.run(apply=True)
            self.assertEqual((out["alerts"], out["counts"].get("receipt_replayed"), out["saves"]), ([], 1, 0))
        self.assertIsNone(self.receipt(1)["alerted"])
        # The same amount is used after the upgrade: a larger gift card part,
        # read later, takes the order below S$300.00 and alerts once.
        self.advance(days=1)
        self.shopify.add(order_node(1, lines=goods_500, transactions=(tx("250.00"), card("250.00")), updated="2026-11-19T05:00:00Z"))
        self.assertEqual(engine.run(apply=True)["alerts"], ["refund_on_basis"])
        row = self.receipt(1)
        self.assertEqual((row["state"], row["net_cents"], row["gift_card_cents"]), ("applied", 25000, 25000))
        self.assertEqual((self.book.members[sg(1)]["type"], self.book.saves), ("MEMBER", 1))  # never downgraded
        self.advance(minutes=15)
        self.shopify.add(order_node(1, lines=goods_500, transactions=(tx("250.00"), card("250.00")), updated="2026-11-19T05:10:00Z"))
        self.assertEqual(engine.run(apply=True)["alerts"], [])

    # -- digest ----------------------------------------------------------------
    def test_the_digest_counts_gift_card_receipts_and_shows_gateway_names_as_code_words_only(self):
        odd, odder = "Counter Payment (Fake Name)", "Another Odd Name!"
        self.guest_with_order(1, lines=(("500.00", 1, 1, False, ()),), transactions=(tx("200.00"), card("300.00")))
        self.guest_with_order(2, transactions=(tx("100.00", status="FAILURE"), card("300.00")))
        self.guest_with_order(3, transactions=(tx("290.00", gateway=odd), tx("10.00", gateway=odder)))
        self.guest_with_order(4, transactions=(tx("150.00", gateway=None), tx("150.00", gateway="PayPal")))
        self.guest_with_order(5)  # no transaction read at all
        texts = [self.run_main(*args)[2] for args in (("init",), ("observe",), ("check-config",))]
        code, digest, text = self.run_main("digest")
        self.assertEqual(code, 0)
        self.assertEqual(digest["gift_cards"], {"receipts_paid_by_gift_card": 1,
                                                "gateways_seen": {"gift_card": 2, "other": 2, "paypal": 1, "shopify_payments": 2}})
        joined = "".join(texts + [text])
        for raw in (odd, odd.lower(), odder, odder.lower(), "PayPal", "Fake Name", "fake name"):
            self.assertNotIn(raw, joined)
        # The names as read stay in the private ledger only.
        self.assertEqual(json.loads(stored_receipt(self.root, 3, "payment_gateways")), sorted([odd.lower(), odder.lower()]))


GiftCardRuleTests.claim_held = G2FixRound2Tests.claim_held
GiftCardRuleTests.link_and_run = G2FixRound2Tests.link_and_run
GiftCardRuleTests.assert_never_upgraded = G2FixRound2Tests.assert_never_upgraded


class AmountReadingTests(Base):
    """Slice B1 (G0 design 1.3): M1 Order.transactionsCount, M3 a kind Shopify
    does not document on a gift card gateway, M4 the gateway name trimmed, M5
    AMOUNT_RULE_VERSION."""

    GOODS_400 = (("400.00", 1, 1, False, ()),)

    def measured(self, transactions, tx_count="exact", **cfg):
        order = bm.order_from_node(order_node(1, lines=self.GOODS_400, shipping=(), transactions=transactions, tx_count=tx_count))
        config = self.cfg(**cfg)
        return order.payments_complete, bm.gift_card_paid(order, config), bm.qualifying_amount(order, config)

    def test_m1_the_list_is_complete_only_with_an_exact_count_equal_to_it(self):
        three = (tx("100.00"), card("150.00"), card("150.00"))
        cases = [
            # (name, transactions, transactionsCount, complete)
            ("empty_with_an_exact_zero", (), {"count": 0, "precision": "EXACT"}, True),
            ("three_with_an_exact_three", three, {"count": 3, "precision": "EXACT"}, True),
            ("nine_with_an_exact_nine", tuple(card("10.00") for _ in range(8)) + (tx("100.00"),), {"count": 9, "precision": "EXACT"}, True),
            ("empty_with_a_null_count", (), None, False),
            ("three_with_a_null_count", three, None, False),
            ("three_listed_four_counted", three, {"count": 4, "precision": "EXACT"}, False),
            ("three_listed_two_counted", three, {"count": 2, "precision": "EXACT"}, False),
            ("three_at_least_three", three, {"count": 3, "precision": "AT_LEAST"}, False),
            ("three_with_another_precision", three, {"count": 3, "precision": "exact"}, False),
            ("three_without_a_precision", three, {"count": 3}, False),
            ("three_without_a_count", three, {"precision": "EXACT"}, False),
            ("a_count_as_text", three, {"count": "3", "precision": "EXACT"}, False),
            ("a_count_true", (tx("100.00"),), {"count": True, "precision": "EXACT"}, False),
            ("a_count_as_a_float", three, {"count": 3.0, "precision": "EXACT"}, False),
            ("a_count_object_as_a_list", three, [3, "EXACT"], False),
            ("a_count_object_as_a_number", three, 3, False),
            ("ten_with_an_exact_ten", tuple(card("40.00") for _ in range(10)), {"count": 10, "precision": "EXACT"}, False),
        ]
        for name, transactions, count, complete in cases:
            with self.subTest(name=name):
                got = self.measured(transactions, tx_count=count)
                self.assertEqual(got[0], complete)
                if complete:
                    self.assertIsNotNone(got[2])
                else:
                    self.assertEqual(got[1:], (None, None))
        # The key missing altogether is not complete, and never a failed read.
        node = order_node(1, lines=self.GOODS_400, transactions=three)
        del node["transactionsCount"]
        self.assertFalse(bm.order_from_node(node).payments_complete)
        self.assertEqual(self.measured((), tx_count={"count": 0, "precision": "EXACT"})[1:], (0, (40000, 40000)))  # proven: no gift card

    def test_m1_an_order_with_an_unproven_list_is_held_for_a_person_and_the_others_carry_on(self):
        self.guest_with_order(1, lines=self.GOODS_400, transactions=(card("400.00"),), tx_count={"count": 2, "precision": "EXACT"})
        self.guest_with_order(2, lines=self.GOODS_400, transactions=(card("400.00"),), tx_count=None)
        self.guest_with_order(3, lines=self.GOODS_400, transactions=(card("400.00"),), tx_count={"count": 1, "precision": "AT_LEAST"})
        self.guest_with_order(4, lines=self.GOODS_400, transactions=(card("400.00"),))
        out = self.engine().run(apply=True)
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in (1, 2, 3, 4)],
                         [("amount_unverifiable", "payments_unreadable")] * 3 + [("applied", "upgrade")])
        self.assertEqual((out["alerts"], out["counts"]["order_amount_unverifiable"], out["saves"]), (["order_amount_unverifiable"], 3, 1))

    def test_m3_a_kind_shopify_does_not_document_on_a_gift_card_gateway_is_unreadable(self):
        self.assertEqual(bm.PAYMENT_KINDS, {"AUTHORIZATION", "CAPTURE", "CHANGE", "EMV_AUTHORIZATION", "REFUND", "SALE", "SUGGESTED_REFUND", "VOID"})
        for kind in ("SALE_LATER", "sale", "", "PAYOUT"):
            with self.subTest(kind=kind, gateway="gift_card"):
                self.assertEqual(self.measured((tx("150.00", kind=kind), card("250.00")))[1:], (None, None))
            with self.subTest(kind=kind, gateway="card"):  # not a gift card gateway: ignored as before
                self.assertEqual(self.measured((tx("150.00"), card("250.00", kind=kind)))[1:], (15000, (40000, 25000)))
        for kind in sorted(bm.PAYMENT_KINDS - bm.GIFT_CARD_PAYMENT_KINDS):  # documented, not a payment: ignored
            with self.subTest(kind=kind):
                self.assertEqual(self.measured((tx("150.00", kind=kind), card("400.00")))[1:], (0, (40000, 40000)))
        self.assertEqual(self.measured((tx("150.00", kind="SALE_LATER"), card("250.00")), gift_card_gateways=["store_credit"])[1:],
                         (0, (40000, 40000)))  # the gateway named in the setting decides

    def test_fix1_n31_an_undocumented_kind_on_a_gift_card_gateway_is_unreadable_whatever_its_status(self):
        for status in ("SUCCESS", "PENDING", "FAILURE", "ERROR", "AWAITING_RESPONSE", "UNKNOWN"):
            with self.subTest(status=status):
                self.assertEqual(self.measured((tx("150.00", kind="SALE_LATER", status=status), card("250.00")))[1:], (None, None))
        # The same transactions with a documented payment kind: only a failed one is ignored.
        self.assertEqual(self.measured((tx("150.00", status="FAILURE"), card("250.00")))[1:], (0, (40000, 40000)))

    def test_m4_the_gateway_name_is_trimmed(self):
        for gateway in (" gift_card", "gift_card ", "\tGift_Card\n", " GIFT_CARD "):
            with self.subTest(gateway=gateway):
                order = bm.order_from_node(order_node(1, lines=self.GOODS_400, transactions=(tx("150.00", gateway=gateway), card("250.00"))))
                self.assertEqual(order.payments[0].gateway, "gift_card")
                self.assertEqual(bm.gift_card_paid(order, self.cfg()), 15000)
        self.assertEqual(self.measured((tx("150.00", gateway="gift card"), card("250.00")))[1], 0)  # a space inside is another name

    def test_m5_the_amount_rule_version_is_in_the_fingerprint(self):
        self.assertEqual(bm.AMOUNT_RULE_VERSION, 2)
        cfg = self.cfg()
        now = cfg.rule_fingerprint()
        with mock.patch.object(bm, "AMOUNT_RULE_VERSION", 3):
            self.assertNotEqual(cfg.rule_fingerprint(), now)
        self.assertEqual(cfg.rule_fingerprint(), now)
        source = (ROOT / "directlocal" / "xb_member_benefits.py").read_text(encoding="utf-8")
        self.assertIn('"amount_rule_version": AMOUNT_RULE_VERSION', source)


class RuleFingerprintTests(Base):
    """Slice B1 (G0 design 1.4): observe and apply refuse
    benefits_rule_settings_changed when an amount setting or the amount rule
    changed since init, before anything is read or recorded."""

    run_main = CliTests.run_main
    # Each of the eight inputs, changed alone (two of them cannot be set in a
    # config file today, so the config object is changed directly).
    CHANGES = {
        "qualify_threshold_cents": {"qualify_threshold_cents": 35_000},
        "amount_before_discounts": {"amount_before_discounts": False},
        "amount_include_tax": {"amount_include_tax": False},
        "amount_exclude_shipping": {"amount_exclude_shipping": False},
        "amount_exclude_gift_cards": {"amount_exclude_gift_cards": True},
        "gift_card_gateways": {"gift_card_gateways": ("gift_card", "store_credit")},
        "amount_count_sale_items": {"amount_count_sale_items": False},
        "amount_rule_version": {},
    }

    def recorded(self) -> tuple:
        conn = self.ledger.conn
        return (conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], self.ledger.watermark(bm.WATERMARK),
                [tuple(r) for r in conn.execute("SELECT * FROM receipts ORDER BY order_gid")],
                conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0], len(self.shopify.calls), self.book.reads, self.book.saves)

    def test_each_input_changed_alone_refuses_observe_and_apply_with_nothing_recorded(self):
        self.guest_with_order(1)
        self.engine().run(apply=False)
        base = self.cfg()
        kept = base.rule_fingerprint()
        self.advance(minutes=15)
        before = self.recorded()
        self.assertEqual(before[0], 1)
        for name, change in self.CHANGES.items():
            cfg = dataclasses.replace(base, **change)
            with contextlib.ExitStack() as stack:
                if name == "amount_rule_version":
                    stack.enter_context(mock.patch.object(bm, "AMOUNT_RULE_VERSION", bm.AMOUNT_RULE_VERSION + 1))
                self.assertNotEqual(cfg.rule_fingerprint(), kept, name)
                engine = bm.BenefitsEngine(cfg, self.ledger, shopify=self.shopify, ac2=self.book, sync_links=self.sync_links)
                for apply in (False, True):
                    with self.subTest(name=name, apply=apply):
                        self.assertEqual(self.code(lambda: engine.run(apply=apply)), "benefits_rule_settings_changed")
                        self.assertEqual(self.recorded(), before)
                self.assertFalse(engine.status()["rule_settings_match"])
                self.assertFalse(bm.ledger_digest(self.root, hours=24, now=self.now, config=cfg)["rule_settings_match"])
        # The same settings again: the run goes ahead.
        out = self.engine().run(apply=True)
        self.assertEqual((out["saves"], self.recorded()[0]), (1, 2))

    def test_settings_outside_the_rule_and_the_order_of_gateways_change_nothing(self):
        self.guest_with_order(1)
        self.engine(gift_card_gateways=["gift_card", "store_credit"])
        same = {"gift_card_gateways": ["store_credit", "gift_card"], "same_day_signup_counts": False, "signup_wait_hours": 72,
                "orders_start_utc": "2026-11-16T00:00:00Z", "max_upgrades_per_run": 1, "max_member_reads_per_run": 5,
                "waiting_member_alert_hours": 24, "shopify_max_pages": 3, "http_timeout_seconds": 10}
        for key, value in same.items():
            with self.subTest(key=key):
                cfg = self.cfg(**{"gift_card_gateways": ["gift_card", "store_credit"], key: value})
                self.assertTrue(self.ledger.rule_settings_match(cfg))
        out = self.engine(gift_card_gateways=["store_credit", "gift_card"], same_day_signup_counts=False).run(apply=False)
        self.assertEqual(out["counts"].get("would_upgrade"), 1)

    def test_the_fingerprint_is_shown_and_the_other_commands_still_run(self):
        self.guest_with_order(1)
        code, config, _ = self.run_main("check-config")
        self.assertEqual((code, config["rule_fingerprint"]), (0, self.cfg().rule_fingerprint()))
        self.assertRegex(config["rule_fingerprint"], r"^sha256:[0-9a-f]{64}\Z")
        self.run_main("init")
        self.assertEqual(self.run_main("observe")[0], 0)
        changed = {"qualify_threshold_cents": 25_000}
        self.advance(minutes=15)
        for command in ("observe", "apply"):
            with self.subTest(command=command):
                self.assertEqual(self.run_main(command, cfg=changed)[:2], (2, {"status": "refused", "code": "benefits_rule_settings_changed"}))
        code, status, _ = self.run_main("status", cfg=changed)
        self.assertEqual((code, status["rule_settings_match"]), (0, False))
        code, digest, _ = self.run_main("digest", cfg=changed)
        self.assertEqual((code, digest["rule_settings_match"], digest["runs"]["completed"]), (0, False, 1))
        for args in (("resolve",), ("index",)):
            with self.subTest(args=args):
                self.assertIn(self.run_main(*args, cfg=changed)[0], (0, 3))
        self.assertTrue(self.run_main("status")[1]["rule_settings_match"])
        self.assertTrue(self.run_main("digest")[1]["rule_settings_match"])
        code, out, _ = self.run_main("check-config", cfg=changed)
        self.assertNotEqual(out["rule_fingerprint"], config["rule_fingerprint"])


class B9Tests(Base):
    """G2 recheck 3, blocker B9: the engine branches no test reached, so the
    privacy check after every test never saw their output (tests only)."""

    run_main = CliTests.run_main
    HELD_VERDICTS = {
        # n: (code, the linked AC2 member; None: the link points at no member)
        1: ("member_inactive", dict(active="F")),
        2: ("guest_expiry_already_later", dict(expiry="2029-01-01T00:00:00.000")),
        3: ("expiry_unreadable", dict(member_type="MEMBER", expiry="31/01/2028")),
        4: ("member_not_found", None),
    }

    def held_verdicts(self, *, apply: bool) -> None:
        for n, (code, member) in self.HELD_VERDICTS.items():
            if member is None:
                self.link(n)
                self.shopify.add(order_node(n))
            else:
                self.guest_with_order(n, member=fake_member(sg(n), name=f"Fake Member {n}", **member))
        before = {no: dict(m) for no, m in self.book.members.items()}
        out = self.engine().run(apply=apply)
        self.assertEqual(out["alerts"], [code for code, _ in self.HELD_VERDICTS.values()])
        for n, (code, _) in self.HELD_VERDICTS.items():
            with self.subTest(code=code):
                row = self.receipt(n)
                self.assertEqual((row["state"], row["code"], row["member_no"], row["link_source"], row["observed"]),
                                 ("held", code, sg(n), "sync155", None))
                self.assertEqual(out["counts"].get(code), 1)
                entries = [entry for entry in out["refs"] if entry["code"] == code]
                self.assertEqual(len(entries), 1)
                self.assertRegex(entries[0]["ref"], self.PRIVATE_REF_RE)
                self.assertEqual(entries[0]["member_no_masked"], bm.mask(sg(n)))
        self.assertEqual((out["saves"], self.book.saves, self.book.payloads, self.book.members), (0, 0, [], before))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 0)
        self.assertEqual((out["counts"].get("would_upgrade"), out["counts"].get("would_extend")), (None, None))
        # Settled: not read or alerted again.
        reads = self.book.reads
        self.advance(minutes=15)
        out = self.engine().run(apply=apply)
        self.assertEqual((out["alerts"], out["saves"], self.book.reads), ([], 0, reads))

    def test_b9_the_four_held_verdicts_in_apply(self):
        self.held_verdicts(apply=True)

    def test_b9_the_four_held_verdicts_in_observe(self):
        self.held_verdicts(apply=False)

    def test_b9_the_sign_up_wait_ends_without_the_orders_being_read_again(self):
        self.shopify.add(order_node(1, customer=customer_node(1, registration=None)))  # the tag without its time
        self.shopify.add(order_node(2, customer=customer_node(2, tags=(), registration=None)))  # no marker at all
        self.engine().run(apply=True)
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in (1, 2)],
                         [("pending_signup", "signup_time_missing"), ("pending_signup", "not_signed_up_yet")])
        # A newer order moves the watermark on, so the two are not read again.
        self.advance(hours=2)
        self.shopify.add(order_node(9, customer=None, lines=(("10.00", 1, 1, False, ()),), created="2026-11-18T05:30:00Z"))
        out = self.engine().run(apply=True)
        self.assertEqual((out["counts"].get("signup_pending"), out["alerts"]), (2, []))
        self.advance(hours=49)  # the sign-up wait (48 hours) is over
        out = self.engine().run(apply=True)
        self.assertEqual(out["counts"]["orders_read"], 1)  # only the newer order
        self.assertEqual((out["alerts"], out["counts"].get("signup_time_missing"), out["counts"].get("not_signed_up")), (["signup_time_missing"], 1, 1))
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in (1, 2)],
                         [("not_signed_up", "signup_time_missing"), ("not_signed_up", "not_signed_up_yet")])
        by_code = {entry["code"]: entry for entry in out["refs"]}
        self.assertEqual(set(by_code), {"signup_time_missing", "not_signed_up"})
        for entry in by_code.values():
            self.assertEqual(set(entry), {"ref", "code"})
            self.assertRegex(entry["ref"], self.PRIVATE_REF_RE)
        self.assertNotEqual(by_code["signup_time_missing"]["ref"], by_code["not_signed_up"]["ref"])
        self.assertEqual((self.book.reads, self.book.saves), (0, 0))
        # Settled: the next run says nothing more about them.
        self.advance(minutes=15)
        out = self.engine().run(apply=True)
        self.assertEqual((out["alerts"], out["counts"].get("not_signed_up"), out["counts"].get("signup_pending")), ([], None, None))

    def test_b9_resolve_reports_a_held_write_by_its_code_only(self):
        member_no = self.guest_with_order(1)
        self.book.mode = "uncertain_mutated"
        self.run_main("init")
        self.assertEqual(self.run_main("apply")[1]["alerts"], ["write_uncertain"])
        self.book.mode = None
        self.advance(seconds=PAST_DEADLINE)
        code, result, text = self.run_main("resolve")
        self.assertEqual((code, result["resolved"], result["alerts"]), (3, {"HELD:resolved_unclear": 1}, ["resolved_unclear"]))
        self.assertNotIn(member_no, text)
        self.assertEqual(stored_receipt(self.root, 1, "state"), "ready")


# --------------------------------------------------------------------------
# #268 slice A2 (11 Nov 2026): the orders query must fit Shopify's query cost
# limit on the strict reading; the requested cost Shopify reports is kept on
# the run; a refused query moves nothing; two test gaps of the PR #300 review.
# --------------------------------------------------------------------------

_QUERY_TOKEN_RE = re.compile(r"\(([^()]*)\)|([{}])|([A-Za-z_][A-Za-z0-9_]*)(:?)")


def parse_selection(query: str) -> list:
    """The fields of one GraphQL operation as ``(name, arguments, children)``.
    ``children`` is None for a field with no selection (a scalar or an enum);
    an alias is dropped in favour of the field it names."""

    body = query[query.index("{"):]
    assert not _QUERY_TOKEN_RE.sub("", body).strip(), "query text this parser does not read"
    tokens = [match.groups() for match in _QUERY_TOKEN_RE.finditer(body)]
    position = 1

    def fields() -> list:
        nonlocal position
        found = []
        while position < len(tokens):
            arguments, brace, name, alias = tokens[position]
            position += 1
            if brace == "}":
                return found
            assert name, "a field name was expected"
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

    assert tokens[0][1] == "{"
    top = fields()
    assert position == len(tokens), "text after the operation"
    return top


def strict_cost(fields: list, order_page: int) -> int:
    """The requested cost on the strict reading of shopify.dev's rate limits
    page: a scalar or an enum costs 0 and an object 1; every selected object
    is counted once for each row of every connection and list above it; a
    connection is its own object, plus what it selects beside ``nodes``, plus
    ``first`` rows; a plain list with ``first`` is ``first`` rows. A list
    with no size argument (``discountAllocations``) has no size to count and
    is taken as one row."""

    total = 0
    for _name, arguments, children in fields:
        if children is None:
            continue
        size = re.search(r"\bfirst: (\$first|[0-9]+)", arguments)
        rows = None if size is None else order_page if size.group(1) == "$first" else int(size.group(1))
        nodes = [child for child in children if child[0] == "nodes"]
        if rows is not None and nodes:
            ((_, _, row),) = nodes
            total += 1 + strict_cost([child for child in children if child[0] != "nodes"], order_page) + rows * (1 + strict_cost(row, order_page))
        elif rows is not None:
            total += rows * (1 + strict_cost(children, order_page))
        else:
            total += 1 + strict_cost(children, order_page)
    return total


def shopify_cost(requested, actual=12) -> dict:
    """``extensions.cost`` in the shape shown on shopify.dev (rate limits)."""
    return {"requestedQueryCost": requested, "actualQueryCost": actual,
            "throttleStatus": {"maximumAvailable": 2000.0, "currentlyAvailable": 1988, "restoreRate": 100.0}}


def orders_page(nodes, *, more: str | None = None, cost: dict | None = None) -> tuple[int, dict]:
    value: dict = {"data": {"orders": {"pageInfo": {"hasNextPage": more is not None, "endCursor": more}, "nodes": list(nodes)}}}
    if cost is not None:
        value["extensions"] = {"cost": cost}
    return 200, value


class OrdersQueryCostTests(Base):
    run_main = CliTests.run_main
    SHOPIFY_LIMIT = 1000  # shopify.dev: a single query may not exceed 1,000 points
    CEILING = 900  # this tool's own ceiling on the strict reading
    STRICT_COST = 852  # slice B1: 847 + 5 (transactionsCount); work it out again (runbook section 9) before changing a page size or the query text

    def real_client(self, responses) -> tuple[bm.BenefitsShopifyClient, FakeTransport]:
        transport = FakeTransport(responses)
        client = bm.BenefitsShopifyClient(shop_domain=SHOP, api_version="2026-10", token_provider=direct.StaticTokenProvider("t" * 32),
                                          transport=transport, sleep=lambda s: None)
        return client, transport

    def factory(self, client):
        return lambda config, ledger: bm.BenefitsEngine(config, ledger, shopify=client, ac2=self.book, sync_links=self.sync_links,
                                                        writer_probe=lambda pid, created: "exited")

    def ledger_rows(self) -> dict[str, int]:
        conn = sqlite3.connect((self.root / bm.LEDGER_FILE).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("receipts", "watermarks", "runs", "claim_links", "writes")}
        finally:
            conn.close()

    # -- the calculated cost ---------------------------------------------------
    def test_the_page_sizes_and_the_strict_cost_of_the_orders_query_are_pinned(self):
        self.assertEqual((bm.SHOPIFY_ORDER_PAGE, bm.SHOPIFY_LINE_PAGE, bm.SHOPIFY_SHIPPING_PAGE, bm.SHOPIFY_PAYMENT_PAGE), (5, 20, 3, 10))
        for text in ("orders(first: $first, after: $after, sortKey: UPDATED_AT, query: $search) {", "lineItems(first: 20) {",
                     "shippingLines(first: 3) {", "transactions(first: 10) {", "discountAllocations { allocatedAmountSet {"):
            self.assertIn(text, bm.ORDERS_QUERY)
        self.assertNotIn("last:", bm.ORDERS_QUERY)  # every size is a ``first``
        page = bm.SHOPIFY_ORDER_PAGE
        ((name, _, orders),) = parse_selection(bm.ORDERS_QUERY)
        self.assertEqual(name, "orders")
        ((_, _, order_row),) = [child for child in orders if child[0] == "nodes"]
        # Line by line, as the runbook (section 9). One order row:
        self.assertEqual({field[0]: strict_cost([field], page) for field in order_row if field[2] is not None},
                         {"totalRefundedSet": 2,     # MoneyBag + MoneyV2
                          "customer": 3,             # Customer + CustomerPhoneNumber + Metafield
                          "lineItems": 122,          # connection 1 + pageInfo 1 + 20 x 6
                          "shippingLines": 11,       # connection 1 + pageInfo 1 + 3 x 3
                          "transactions": 30,        # 10 x 3, a plain list
                          "transactionsCount": 1})   # slice B1: one Count object
        by_name = {field[0]: field for field in order_row}
        ((_, _, line_row),) = [child for child in by_name["lineItems"][2] if child[0] == "nodes"]
        ((_, _, shipping_row),) = [child for child in by_name["shippingLines"][2] if child[0] == "nodes"]
        self.assertEqual((1 + strict_cost(line_row, page), 1 + strict_cost(shipping_row, page), 1 + strict_cost(by_name["transactions"][2], page)),
                         (6, 3, 3))  # LineItem + price 2 + one discount allocation 3; ShippingLine + price 2; OrderTransaction + amount 2
        self.assertEqual(1 + strict_cost(order_row, page), 170)  # the Order itself + 2 + 3 + 122 + 11 + 30 + 1
        total = strict_cost(parse_selection(bm.ORDERS_QUERY), page)
        self.assertEqual(total, 2 + 5 * 170)  # the orders connection + its pageInfo + 5 order rows
        self.assertEqual(total, self.STRICT_COST)
        self.assertLessEqual(total, self.CEILING)
        self.assertLess(self.CEILING, self.SHOPIFY_LIMIT)
        # The same sum for the sizes of PR #300 (10 orders, 50 lines, 5 shipping lines, 25 transactions).
        before = bm.ORDERS_QUERY.replace("lineItems(first: 20)", "lineItems(first: 50)").replace(
            "shippingLines(first: 3)", "shippingLines(first: 5)").replace("transactions(first: 10)", "transactions(first: 25)").replace(
            "transactionsCount { count precision }", "")  # slice B1: the query of PR #300 had no transactionsCount
        self.assertEqual(strict_cost(parse_selection(before), 10), 4002)
        # The two small reads of this tool.
        self.assertEqual((strict_cost(parse_selection(bm.CUSTOMER_QUERY), page), strict_cost(parse_selection(bm.PHONE_LOOKUP_QUERY), page)), (3, 12))

    def test_one_run_still_reaches_as_many_orders_as_before(self):
        cfg = self.cfg()
        self.assertEqual((cfg.shopify_max_pages, bm._CONFIG_DEFAULTS["shopify_max_pages"], bm._BOUNDS["shopify_max_pages"]), (20, 20, (1, 40)))
        self.assertEqual(bm.SHOPIFY_ORDER_PAGE * cfg.shopify_max_pages, 100)  # 10 pages x 10 orders before
        self.assertEqual(bm.SHOPIFY_ORDER_PAGE * bm._BOUNDS["shopify_max_pages"][1], 200)  # at the hard maximum (400 before)
        for n in range(100, 201):  # 101 small orders
            self.shopify.add(order_node(n, customer=None, lines=(("10.00", 1, 1, False, ()),), updated=f"2026-11-18T03:30:{n % 60:02d}Z"))
        out = self.engine().run(apply=False)
        self.assertEqual((out["counts"]["orders_read"], out["alerts"]), (100, ["orders_read_incomplete"]))
        out = self.engine().run(apply=False)
        self.assertEqual((out["counts"]["orders_read"], out["counts"].get("orders_read_resumed"), out["alerts"]), (1, 1, []))

    def test_an_order_with_more_rows_than_a_page_is_held_for_a_person(self):
        twenty = tuple(("20.00", 1, 1, False, ()) for _ in range(20))
        goods_400 = (("400.00", 1, 1, False, ()),)
        self.guest_with_order(1, lines=twenty)  # 20 line items: read in full, S$400.00
        self.guest_with_order(2, lines=twenty, more_lines=True)  # a 21st line item was not returned
        self.guest_with_order(3, lines=goods_400, transactions=tuple(card("50.00") for _ in range(8)) + (tx("0.00"),))  # 9 transactions
        self.guest_with_order(4, lines=goods_400, transactions=tuple(card("40.00") for _ in range(10)))  # 10: the list may be cut short
        out = self.engine().run(apply=True)
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in (1, 2, 3, 4)],
                         [("applied", "upgrade"), ("amount_unverifiable", "currency_or_lines"), ("applied", "upgrade"),
                          ("amount_unverifiable", "payments_unreadable")])
        self.assertEqual((out["alerts"], out["counts"]["order_amount_unverifiable"], out["saves"], self.book.reads), (["order_amount_unverifiable"], 2, 2, 2))
        self.assertEqual([self.receipt(n)["net_cents"] for n in (1, 2, 3, 4)], [40000, None, 40000, None])

    # -- the measured cost -----------------------------------------------------
    def test_whole_points(self):
        cases = [(847, 847), (847.0, 847), (846.5, 847), (0, 0), (0.2, 1), (1_000_000, 1_000_000), (None, None), ("847", None), (True, None),
                 (False, None), (-1, None), (-0.5, None), (1_000_000.5, None), (float("nan"), None), (float("inf"), None), ([847], None), ({}, None)]
        for value, expected in cases:
            with self.subTest(value=value):
                got = bm.whole_points(value)
                self.assertEqual(got, expected)
                self.assertIs(type(got), int if expected is not None else type(None))

    def test_the_client_keeps_the_requested_cost_of_the_orders_read_only(self):
        session = ShopifyClientTests.session(["read_orders", "read_customers"])
        session[1]["extensions"] = {"cost": shopify_cost(2, 2)}
        client, _ = self.real_client([session, orders_page([order_node(1)], more="c1", cost=shopify_cost(412, 30)),
                                      orders_page([order_node(2)], more="c2", cost=shopify_cost(847, 41)), orders_page([order_node(3)])])
        self.assertIsNone(client.last_orders_query_cost)
        client.verify_session(store_gid=STORE_GID)
        self.assertIsNone(client.last_orders_query_cost)  # the session check is not the orders read
        orders, complete = client.orders(dl.parse_created_at(START), 5)
        self.assertEqual((len(orders), complete, client.last_orders_query_cost), (3, True, 847))  # the largest of the pages; not the actual cost
        # A read whose responses carry no cost reports none (never the figure of an earlier read).
        client, _ = self.real_client([session, orders_page([order_node(1)], more="c1", cost=shopify_cost(847)), orders_page([order_node(2)])])
        client.verify_session(store_gid=STORE_GID)
        client.orders(dl.parse_created_at(START), 1)
        self.assertEqual(client.last_orders_query_cost, 847)
        client.orders(dl.parse_created_at(START), 1, after="c1")
        self.assertIsNone(client.last_orders_query_cost)
        # A cost that is not a number is no cost.
        client, _ = self.real_client([orders_page([order_node(1)], cost=shopify_cost("847"))])
        client.orders(dl.parse_created_at(START), 1)
        self.assertIsNone(client.last_orders_query_cost)

    def test_the_run_output_and_the_stored_summary_carry_the_cost_as_a_whole_number(self):
        self.guest_with_order(1)
        nodes = list(self.shopify.orders_nodes.values())
        self.run_main("init")
        for requested, expected in ((846.5, 847), (None, None)):
            with self.subTest(requested=requested):
                self.advance(minutes=15)
                client, transport = self.real_client([ShopifyClientTests.session(["read_orders", "read_customers"]),
                                                      orders_page(nodes, cost=None if requested is None else shopify_cost(requested))])
                code, out, text = self.run_main("observe", factory=self.factory(client))
                self.assertEqual((code, out["status"], out["counts"]["orders_read"]), (0, "ok", 1))
                self.assertIn("orders_query_cost", out)
                self.assertEqual(out["orders_query_cost"], expected)
                self.assertIs(type(out["orders_query_cost"]), int if expected is not None else type(None))
                self.assertIn('"orders_query_cost":' + ("847," if expected else "null,"), text)  # as printed: digits only, or null
                conn = sqlite3.connect((self.root / bm.LEDGER_FILE).resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    stored = json.loads(conn.execute("SELECT summary FROM runs WHERE run_id = ?", (out["run_id"],)).fetchone()[0])
                finally:
                    conn.close()
                self.assertEqual(stored["orders_query_cost"], expected)
                self.assertEqual(transport.bodies[1]["query"], bm.ORDERS_QUERY)

    def test_the_engine_takes_a_whole_number_only_and_the_index_reports_no_cost(self):
        self.guest_with_order(1)
        out = self.engine().run(apply=False)  # a reader that reports no cost at all
        self.assertEqual((out["orders_query_cost"], "orders_query_cost" in out), (None, True))
        for value, expected in ((846.5, 847), ("847 points", None), (True, None), (847, 847)):
            with self.subTest(value=value):
                self.advance(minutes=15)
                self.shopify.last_orders_query_cost = value
                out = self.engine().run(apply=True)
                self.assertEqual(out["orders_query_cost"], expected)
                self.assertIs(type(out["orders_query_cost"]), int if expected is not None else type(None))
        self.assertNotIn("orders_query_cost", self.engine().index(apply=False, max_members=1))

    # -- a refused query -------------------------------------------------------
    def test_a_query_refused_for_its_cost_moves_nothing_and_ends_with_one_code(self):
        """shopify.dev does not show what a cost refusal looks like. Whatever
        its body, an answer with GraphQL ``errors`` (with or without data
        beside it, with or without a cost) is ``shopify_graphql_error``: not
        sent again, exit 2, nothing recorded and no watermark moved."""

        self.guest_with_order(1)
        nodes = list(self.shopify.orders_nodes.values())
        refusals = {
            "errors_only": {"errors": [{"message": "fake refusal", "extensions": {"code": "FAKE_QUERY_COST_REFUSAL", "cost": 1002, "maxCost": 1000}}]},
            "errors_without_a_code": {"errors": [{"message": "fake refusal"}]},
            "errors_beside_data": {"errors": [{"message": "fake refusal"}], **orders_page(nodes)[1]},
            "errors_beside_a_cost": {"errors": [{"message": "fake refusal"}], "extensions": {"cost": shopify_cost(1002)}},
        }
        self.run_main("init")
        self.assertEqual(self.ledger_rows(), {"receipts": 0, "watermarks": 0, "runs": 0, "claim_links": 0, "writes": 0})
        for command in ("observe", "apply"):
            for name, body in refusals.items():
                with self.subTest(command=command, name=name):
                    client, transport = self.real_client([ShopifyClientTests.session(["read_orders", "read_customers"]), (200, body)])
                    code, out, _ = self.run_main(command, factory=self.factory(client))
                    self.assertEqual((code, out), (2, {"status": "refused", "code": "shopify_graphql_error"}))
                    self.assertEqual(len(transport.bodies), 2)  # the session check and one orders query
                    self.assertEqual(self.ledger_rows(), {"receipts": 0, "watermarks": 0, "runs": 0, "claim_links": 0, "writes": 0})
                    self.assertEqual((self.book.reads, self.book.saves), (0, 0))
        # The same order is read and decided once Shopify answers.
        client, _ = self.real_client([ShopifyClientTests.session(["read_orders", "read_customers"]), orders_page(nodes, cost=shopify_cost(847))])
        code, out, _ = self.run_main("apply", factory=self.factory(client))
        self.assertEqual((code, out["saves"], out["orders_query_cost"], self.ledger_rows()["receipts"]), (0, 1, 847, 1))

    # -- test gaps of the PR #300 review (LATER 7: P43, P52; P35 is in test_gift_card_settings) --
    def test_p43_p52_a_decided_receipt_read_again_with_unreadable_payments_keeps_its_amounts(self):
        goods_500 = (("500.00", 1, 1, False, ()),)
        paid = (tx("200.00"), card("300.00"))
        refund = dict(status="PARTIALLY_REFUNDED", refunded="50.00")  # by money, no unit: held before the decision
        self.guest_with_order(1, lines=goods_500, transactions=paid)
        self.guest_with_order(2, lines=goods_500, transactions=paid, **refund)
        engine = self.engine()
        engine.run(apply=True)

        def rows():
            return [(self.receipt(n)["state"], self.receipt(n)["code"], self.receipt(n)["gross_cents"], self.receipt(n)["net_cents"],
                     self.receipt(n)["gift_card_cents"]) for n in (1, 2)]
        decided = [("applied", "upgrade", 50000, 30000, 20000), ("held", "refund_unattributed", 50000, 30000, 20000)]
        self.assertEqual(rows(), decided)
        unreadable = {
            "a_pending_gift_card_payment": (tx("200.00", status="PENDING"), card("300.00")),
            "a_full_transaction_list": tuple(card("50.00") for _ in range(10)),
            "an_amount_that_is_not_sgd": (tx("200.00", currency="USD"), card("300.00")),
        }
        for name, transactions in unreadable.items():
            with self.subTest(name=name):
                self.advance(minutes=15)
                self.shopify.add(order_node(1, lines=goods_500, transactions=transactions, updated=dl.ts(self.now)))
                self.shopify.add(order_node(2, lines=goods_500, transactions=transactions, updated=dl.ts(self.now), **refund))
                out = engine.run(apply=True)
                self.assertEqual((out["alerts"], out["saves"], out["counts"].get("receipt_replayed"), out["counts"].get("refund_recorded")), ([], 0, 1, 1))
                self.assertEqual(rows(), decided)  # the amounts as last measured, not blanks
        self.assertEqual((self.book.saves, self.book.members[sg(1)]["type"], self.book.members[sg(2)]["type"]), (1, "MEMBER", "GUEST"))
        # A readable replay is still taken: the gift card part grows by S$10.00.
        self.advance(minutes=15)
        self.shopify.add(order_node(1, lines=goods_500, transactions=(tx("210.00"), card("290.00")), updated=dl.ts(self.now)))
        engine.run(apply=True)
        self.assertEqual((self.receipt(1)["net_cents"], self.receipt(1)["gift_card_cents"]), (29000, 21000))


class OutputShapeRuleTests(Base):
    """Fix round 1 of slice A2: the check after every test must itself notice
    part of an ID, or a name, in a place where only a code word belongs."""

    GOOD_RUN = {"status": "ok", "command": "apply", "run_id": "run-" + "0" * 32, "saves": 1, "stopped": None, "orders_query_cost": 847,
                "counts": {"orders_read": 2, "receipt_ready": 1, "upgraded": 1, "step_155": 1, "a" * 64: 1},
                "alerts": ["order_amount_unverifiable"],
                "refs": [{"ref": "o-" + "a" * 20, "code": "upgraded", "member_no_masked": "*******001"}]}
    # Slice B1 T5: the digest in its full shape (the check now knows every key).
    GOOD_DIGEST = {"schema": "xb.directlocal.member_benefits.digest.v1", "status": "ok", "autocount_book": "AED_TESTINGAPI", "hours": 24,
                   "rule_settings_match": True,
                   "runs": {"completed": 2, "unfinished": 0}, "counts": {"orders_read": 2}, "alerts": {"claim_needs_review": 2},
                   "alert_refs": [{"ref": "m-" + "b" * 20, "code": "claim_needs_review"}], "receipts": {"applied": 1, "held": 1},
                   "shadow": {"would_upgrade": 0, "would_extend": 0}, "gift_cards": {"receipts_paid_by_gift_card": 0, "gateways_seen": {}},
                   "open": {"uncertain_writes": 0, "held_writes": 0, "waiting_member": 0, "claim_links": 1, "claim_links_voided": 0}}
    GOOD_RESOLVE = {"status": "ok", "command": "resolve", "writes": 0, "resolved": {"HELD:resolved_unclear": 1}, "alerts": ["resolved_unclear"]}
    GOOD_INDEX = {"status": "ok", "command": "index_apply", "run_id": "run-" + "0" * 32, "saves": 0, "stopped": None,
                  "counts": {"examined": 1}, "alerts": [], "refs": []}

    def checked(self, value: dict, *, stored: bool = False) -> None:
        """Run the check after every test on one more output: a CLI output,
        or (``stored``) the summary of a run kept in a ledger."""

        real_root = self.root
        try:
            if stored:
                self.root = Path(tempfile.mkdtemp(prefix="stored-", dir=self.temp))
                ledger = bm.BenefitsLedger(self.root, clock=lambda: self.now)
                try:
                    ledger.init_meta(self.cfg())
                    ledger.finish_run(ledger.start_run("apply"), value)
                finally:
                    ledger.close()
            else:
                self.emitted.append(value)
            self.assert_outputs_private()
        finally:
            self.root = real_root
            if not stored:
                self.emitted.pop()

    def test_the_check_after_every_test_takes_code_words_only(self):
        order, customer = str(order_number(1)), str(customer_number(1))
        for good in (self.GOOD_RUN, self.GOOD_DIGEST, {**self.GOOD_RUN, "stopped": "uncertain:writer_timeout"},
                     {**self.GOOD_RUN, "stopped": "stop_file_present"}, {**self.GOOD_RESOLVE, "resolved": {}, "alerts": []}, self.GOOD_RESOLVE,
                     {**self.GOOD_RESOLVE, "receipts_left": 1}, self.GOOD_INDEX, {**self.GOOD_INDEX, "stopped": "bridge_read_failed:member_no_invalid"},
                     {"status": "refused", "code": "benefits_ledger_not_recognised"}, {"status": "initialised"}):
            with self.subTest(good=good):
                self.checked(good)
                self.checked(good, stored=True)
        run = self.GOOD_RUN
        bad = {
            # part of an order or customer number where a code word belongs
            "count_key_with_the_last_8_digits_of_an_order_number": {**run, "counts": {"seen_" + order[-8:]: 1}},
            "count_key_with_the_last_6_digits_of_an_order_number": {**run, "counts": {"seen_" + order[-6:]: 1}},
            "count_key_that_is_the_last_8_digits": {**run, "counts": {order[-8:]: 1}},
            "count_key_with_the_last_8_digits_of_a_customer_number": {**run, "counts": {"seen_" + customer[-8:]: 1}},
            "count_key_with_an_order_number_less_its_first_digit": {**run, "counts": {"seen_" + order[1:]: 1}},
            "count_key_with_a_whole_order_number": {**run, "counts": {"seen_" + order: 1}},
            "count_key_with_a_whole_customer_number": {**run, "counts": {"seen_" + customer: 1}},
            "count_key_with_four_digits": {**run, "counts": {"seen_2026": 1}},
            "alert_code_with_part_of_an_order_number": {**run, "alerts": ["alert_" + order[-6:]]},
            "digest_alert_code_with_part_of_an_order_number": {**self.GOOD_DIGEST, "alerts": {"alert_" + order[-6:]: 1}},
            "ref_code_with_part_of_an_order_number": {**run, "refs": [{"ref": "o-" + "a" * 20, "code": "upgraded_" + order[-6:]}]},
            "digest_ref_code_with_part_of_an_order_number": {**self.GOOD_DIGEST, "alert_refs": [{"ref": "m-" + "b" * 20, "code": "x_" + order[-6:]}]},
            "stopped_with_the_last_6_digits_of_an_order_number": {**run, "stopped": order[-6:]},
            "stopped_with_a_whole_order_number": {**run, "stopped": order},
            "stopped_code_with_part_of_an_order_number": {**run, "stopped": "uncertain:" + order[-8:]},
            # a name, or any other free text
            "count_key_with_a_name": {**run, "counts": {"seen_Fake Member 1": 1}},
            "count_key_with_a_capital": {**run, "counts": {"Seen": 1}},
            "count_key_with_a_dash": {**run, "counts": {"seen-it": 1}},
            "count_key_with_a_dot": {**run, "counts": {"seen.it": 1}},
            "count_key_that_is_empty": {**run, "counts": {"": 1}},
            "count_key_of_65_characters": {**run, "counts": {"a" * 65: 1}},
            "alert_code_with_a_name": {**run, "alerts": ["Fake Member 1"]},
            "ref_code_with_a_name": {**run, "refs": [{"ref": "o-" + "a" * 20, "code": "upgraded Fake Member 1"}]},
            "ref_without_a_code": {**run, "refs": [{"ref": "o-" + "a" * 20}]},
            "stopped_with_a_name": {**run, "stopped": "uncertain:Fake Member 1"},
            "stopped_that_is_not_text": {**run, "stopped": 404},
            # slice B1 T1 (B11): the places no bad case reached before
            "digest_count_key_with_part_of_an_order_number": {**self.GOOD_DIGEST, "counts": {"seen_" + order[-6:]: 1}},
            "resolve_alert_code_with_part_of_an_order_number": {**self.GOOD_RESOLVE, "alerts": ["held_" + order[-6:]]},
            "index_stopped_with_part_of_an_order_number": {**self.GOOD_INDEX, "stopped": "bridge_read_failed:x_" + order[-6:]},
            "count_key_with_part_of_an_order_number_and_a_text_value": {**run, "counts": {"seen_" + order[-6:]: "1"}},
            # T2: a line break after a word, a ref, a masked MemberNo or a gateway name
            "count_key_with_a_line_break_after_it": {**run, "counts": {"upgraded\n": 1}},
            "alert_code_with_a_line_break_after_it": {**run, "alerts": ["upgraded\n"]},
            "ref_with_a_line_break_after_it": {**run, "refs": [{"ref": "o-" + "a" * 20 + "\n", "code": "upgraded"}]},
            "masked_member_no_with_a_line_break_after_it": {**run, "refs": [{"ref": "o-" + "a" * 20, "code": "upgraded",
                                                                            "member_no_masked": "*******001\n"}]},
            "gateway_name_with_a_line_break_after_it": {**self.GOOD_DIGEST, "gift_cards": {"receipts_paid_by_gift_card": 1,
                                                                                        "gateways_seen": {"gift_card\n": 1}}},
            # T3: counts as an object of whole numbers, alerts as a list (a run) or an object of whole numbers (the digest)
            "count_value_as_text": {**run, "counts": {"upgraded": "1"}},
            "count_value_as_a_float": {**run, "counts": {"upgraded": 1.0}},
            "count_value_true": {**run, "counts": {"upgraded": True}},
            "counts_as_a_list": {**run, "counts": ["upgraded"]},
            "counts_as_one_string": {**run, "counts": "seen_" + order[-6:]},
            "alerts_as_one_string": {**run, "alerts": "alert_" + order[-6:]},
            "digest_counts_as_a_list": {**self.GOOD_DIGEST, "counts": ["orders_read"]},
            "digest_count_value_as_text": {**self.GOOD_DIGEST, "counts": {"orders_read": "2"}},
            "digest_alerts_as_a_list": {**self.GOOD_DIGEST, "alerts": ["claim_needs_review"]},
            # T4: the code of a refused output
            "refused_code_with_part_of_an_order_number": {"status": "refused", "code": "refused_" + order[-6:]},
            "refused_code_with_a_name": {"status": "refused", "code": "Fake Member 1"},
            "refused_code_that_is_not_text": {"status": "refused", "code": 2},
            "refused_with_another_key": {"status": "refused", "code": "benefits_config_invalid", "detail": "x"},
            # T5: a new key in any output, and the keys of resolved, receipts and writes
            "run_with_a_new_key": {**run, "seen": "seen_" + order[-6:]},
            "run_without_a_key": {key: v for key, v in run.items() if key != "orders_query_cost"},
            "index_with_the_cost_key": {**self.GOOD_INDEX, "orders_query_cost": 847},
            "resolve_with_a_new_key": {**self.GOOD_RESOLVE, "seen": 1},
            "resolved_key_with_part_of_an_order_number": {**self.GOOD_RESOLVE, "resolved": {"HELD:held_" + order[-6:]: 1}},
            "resolved_key_with_another_state": {**self.GOOD_RESOLVE, "resolved": {"SEEN:resolved_unclear": 1}},
            "digest_with_a_new_key": {**self.GOOD_DIGEST, "seen": 1},
            "digest_open_with_a_new_key": {**self.GOOD_DIGEST, "open": {**self.GOOD_DIGEST["open"], "seen_" + order[-6:]: 1}},
            "digest_receipts_key_with_part_of_an_order_number": {**self.GOOD_DIGEST, "receipts": {"held_" + order[-6:]: 1}},
            "an_unknown_command": {**run, "command": "seen"},
            "status_with_a_new_key": {"status": "ok", "command": "status", "receipts": {}, "observed": {}, "writes": {}, "held_writes": 0, "claim_links": 0, "claim_links_voided": 0, "rule_settings_match": True,
                                      "reviews": 0, "watermark_set": False, "stop_file_present": False, "seen": 1},
            "status_receipts_key_with_part_of_an_order_number": {
                "status": "ok", "command": "status", "receipts": {"held_" + order[-6:]: 1}, "observed": {}, "writes": {}, "held_writes": 0, "claim_links": 0, "claim_links_voided": 0, "rule_settings_match": True,
                "reviews": 0, "watermark_set": False, "stop_file_present": False},
            "status_writes_key_with_part_of_an_order_number": {
                "status": "ok", "command": "status", "receipts": {}, "observed": {}, "writes": {"upgrade:" + order[-6:]: 1}, "held_writes": 0, "claim_links": 0, "claim_links_voided": 0, "rule_settings_match": True,
                "reviews": 0, "watermark_set": False, "stop_file_present": False},
            "check_config_with_a_new_key": {"status": "ok", **{key: None for key in self.CONFIG_KEYS if key != "status"}, "seen": 1},
            "init_with_another_status": {"status": "seen_" + order[-6:]},
            # slice B1: earlier families of the rule re-run (each part of the check on its own)
            "a_ref_that_is_not_a_keyed_ref": {**run, "refs": [{"ref": "o-" + order[-8:], "code": "upgraded"}]},
            "a_member_number_unmasked": {**run, "refs": [{"ref": "o-" + "a" * 20, "code": "upgraded", "member_no_masked": "x" + order[-9:-6]}]},
            "a_ref_entry_with_another_key": {**run, "refs": [{"ref": "o-" + "a" * 20, "code": "upgraded", "name": "x"}]},
            "a_gid_anywhere": {**run, "run_id": "gid://shopify/Order/x"},
            "a_private_value_anywhere": {**run, "run_id": "run-" + sg(1)},
            "a_cost_as_text": {**run, "orders_query_cost": "847"},
            "a_cost_with_a_fraction": {**run, "orders_query_cost": 846.5},
        }
        self.book.members[sg(1)] = fake_member(sg(1))  # a private value the check knows
        for name, value in bad.items():
            for stored in (False, True):
                with self.subTest(name=name, stored=stored):
                    with self.assertRaises(AssertionError):
                        self.checked(value, stored=stored)

    def test_fix1_l2_the_values_of_the_keys_slice_b1_added_are_checked(self):
        order = str(order_number(1))
        release = {"status": "ok", "command": "release_held", "run_id": "run-" + "0" * 32, "finding": "as_intended", "receipt_now": "applied",
                   "held_writes_left": 0, "counts": {"release_receipt_applied": 1}, "alerts": [],
                   "refs": [{"ref": "o-" + "a" * 20, "code": "write_released", "member_no_masked": "*******001"}]}
        unlink = {"status": "ok", "command": "unlink", "run_id": "run-" + "0" * 32, "reason": "wrong_person", "link_source": "index",
                  "counts": {"claim_unlinked": 1}, "alerts": [], "refs": [{"ref": "c-" + "a" * 20, "code": "claim_unlinked", "member_no_masked": "*******001"}]}
        status = {"status": "ok", "command": "status", "receipts": {}, "observed": {}, "writes": {}, "held_writes": 1, "claim_links": 2,
                  "claim_links_voided": 1, "rule_settings_match": False, "reviews": 0, "watermark_set": False, "stop_file_present": False}
        digest = self.GOOD_DIGEST
        goods = [release, {**release, "command": "release_held_dry_run", "run_id": None, "finding": "not_as_intended", "receipt_now": "held"},
                 {**release, "receipt_now": "left_decided"}, unlink, {**unlink, "command": "unlink_dry_run", "run_id": None, "link_source": "order"},
                 *({**unlink, "reason": reason} for reason in ("customers_merged", "member_renumbered", "other")), status,
                 {**digest, "rule_settings_match": False}]
        for good in goods:
            with self.subTest(good=good):
                self.checked(good)
                self.checked(good, stored=True)
        self.book.members[sg(1)] = fake_member(sg(1))
        bad = {
            "finding_with_part_of_an_order_number": {**release, "finding": "as_intended_" + order[-6:]},
            "finding_with_a_name": {**release, "finding": "Fake Member 1"},
            "receipt_now_with_part_of_an_order_number": {**release, "receipt_now": "held_" + order[-6:]},
            "receipt_now_with_a_line_break": {**release, "receipt_now": "applied\n"},
            "held_writes_left_as_text": {**release, "held_writes_left": order[-6:]},
            "held_writes_left_true": {**release, "held_writes_left": True},
            "reason_with_part_of_an_order_number": {**unlink, "reason": "other_" + order[-6:]},
            "reason_in_capitals": {**unlink, "reason": "OTHER"},
            "link_source_with_a_name": {**unlink, "link_source": "Fake Member 1"},
            "link_source_sync155": {**unlink, "link_source": "sync155"},
            "status_held_writes_as_text": {**status, "held_writes": order[-6:]},
            "status_claim_links_voided_as_a_float": {**status, "claim_links_voided": 1.0},
            "status_claim_links_as_text": {**status, "claim_links": "2"},
            "status_rule_settings_match_as_text": {**status, "rule_settings_match": "true"},
            "status_rule_settings_match_as_a_number": {**status, "rule_settings_match": 1},
            "digest_rule_settings_match_as_text": {**digest, "rule_settings_match": "yes_" + order[-6:]},
            "digest_open_claim_links_voided_as_text": {**digest, "open": {**digest["open"], "claim_links_voided": order[-6:]}},
            "digest_open_held_writes_true": {**digest, "open": {**digest["open"], "held_writes": True}},
        }
        for name, value in bad.items():
            for stored in (False, True):
                with self.subTest(name=name, stored=stored):
                    with self.assertRaises(AssertionError):
                        self.checked(value, stored=stored)

    # -- fix round 2: the one exception, the generic bridge failure code ------
    def real_generic_bridge_codes(self) -> list[str]:
        """Real outputs of ``xb_directlocal.bridge_failure_code`` for a
        generic failure: every shape class, small and large exit codes, a
        negative one, no exit code, and 0 to 12 non-empty output lines."""

        outputs = [b"", b"not json", b"\xff\xfe", b"[1]", b'{"ok": true}', b'{"error": "fake", "more": 1}', b'{"error": 7}',
                   b'{"error": "a_code_that_is_not_listed"}']
        outputs += [b"\n".join([b"line"] * lines) for lines in range(1, 13)]
        exits = [None, 0, 1, 2, 255, 4242, 0xE0434352, 0xFFFFFFFF, -1, -1073741510]
        return sorted({dl.bridge_failure_code(stdout, returncode) for stdout in outputs for returncode in exits})

    def test_the_generic_bridge_failure_code_is_the_one_exception(self):
        codes = self.real_generic_bridge_codes()
        # The whole shape, as the tool builds it today: if bridge_failure_code
        # ever produces another part, this line fails first.
        shape = re.compile(r"^bridge_read_failed:(output_empty|output_not_utf8|output_not_json|result_on_failed_exit|envelope_invalid|code_unlisted)"
                           r":x([0-9a-f]{8}|none):l[0-9]$")
        for code in codes:
            self.assertRegex(code, shape)
        self.assertEqual({code.rsplit(":l", 1)[1] for code in codes}, set("0123456789"))  # the line count is capped at 9
        self.assertLessEqual({"x00000001", "xe0434352", "xffffffff", "xc000013a", "x00001092", "xnone"}, {code.split(":")[2] for code in codes})
        self.assertGreater(len(codes), 100)
        with_digits = [code for code in codes if self.PRIVATE_DIGITS_RE.search(code)]  # these would fail without the exception
        self.assertGreater(len(with_digits), 50)
        self.assertLessEqual({"bridge_read_failed:output_empty:x00000001:l0", "bridge_read_failed:output_empty:xe0434352:l0"}, set(with_digits))
        run = self.GOOD_RUN
        for code in codes:
            with self.subTest(code=code):
                value = {**run, "counts": {code: 1}, "alerts": [code], "stopped": code, "refs": [{"ref": "o-" + "a" * 20, "code": code}]}
                self.checked(value)
        for code in (codes[0], codes[-1], dl.bridge_failure_code(b"", 0xE0434352)):
            self.checked({**run, "counts": {code: 1}, "alerts": [code], "stopped": code}, stored=True)
            self.checked({**self.GOOD_DIGEST, "counts": {code: 2}, "alerts": {code: 2}, "alert_refs": [{"ref": "m-" + "b" * 20, "code": code}]})
        # An allowlisted child code has no tail and passes as any code word.
        self.checked({**run, "stopped": dl.bridge_failure_code(b'{"error": "bridge_failed"}', 1), "alerts": ["bridge_read_failed:member_no_invalid"]})
        order = str(order_number(1))
        tail = ":x00000001:l0"
        bad = {
            # the same tail on any other word
            "the_tail_on_another_word": "write_failed:output_empty" + tail,
            "the_tail_on_a_look_alike_prefix": "bridge_write_failed:output_empty" + tail,
            "the_tail_on_a_longer_prefix": "bridge_read_failed_too:output_empty" + tail,
            "the_tail_after_another_prefix": "uncertain:bridge_read_failed:output_empty" + tail,
            "the_tail_alone": tail.lstrip(":"),
            # a bridge_read_failed word with something private outside the tail
            "part_of_an_order_number_before_the_tail": "bridge_read_failed:seen_" + order[-8:] + tail,
            "a_whole_order_number_before_the_tail": "bridge_read_failed:" + order + tail,
            "five_digits_before_the_tail": "bridge_read_failed:output_12345" + tail,
            "four_digits_before_the_none_tail": "bridge_read_failed:output_2026:xnone:l0",
            "a_name_before_the_tail": "bridge_read_failed:Fake Member 1" + tail,
            "part_of_an_order_number_after_the_tail": "bridge_read_failed:output_empty" + tail + "_" + order[-6:],
            "an_order_number_in_place_of_the_exit_code": "bridge_read_failed:output_empty:x" + order + ":l0",
            # a tail that is almost the shape
            "seven_hex_characters": "bridge_read_failed:output_empty:x0000001:l0",
            "nine_hex_characters": "bridge_read_failed:output_empty:x000000001:l0",
            "capital_hex_characters": "bridge_read_failed:output_empty:xE0434352:l0",
            "a_letter_that_is_not_hex": "bridge_read_failed:output_empty:x0000000z:l0",
            "an_underscore_in_the_exit_part": "bridge_read_failed:output_empty:x0000_001:l0",
            "eight_digits_without_the_x": "bridge_read_failed:output_empty:00000001:l0",
            "a_line_count_of_two_digits": "bridge_read_failed:output_empty:x00000001:l10",
            "no_line_count": "bridge_read_failed:output_empty:x00000001",
            "a_second_tail": "bridge_read_failed:output_empty" + tail + tail,
            "the_tail_not_at_the_end": "bridge_read_failed" + tail + ":output_empty",
            "the_tail_with_a_line_break_after_it": "bridge_read_failed:output_empty" + tail + "\n",
            "the_two_parts_the_other_way_round": "bridge_read_failed:output_empty:l0:x00000001",
        }
        for name, word in bad.items():
            for place in ("counts", "alerts", "stopped", "ref"):
                value = {"counts": {**run, "counts": {word: 1}}, "alerts": {**run, "alerts": [word]}, "stopped": {**run, "stopped": word},
                         "ref": {**run, "refs": [{"ref": "o-" + "a" * 20, "code": word}]}}[place]
                with self.subTest(name=name, place=place):
                    with self.assertRaises(AssertionError):
                        self.checked(value)
            with self.subTest(name=name, stored=True):
                with self.assertRaises(AssertionError):
                    self.checked({**run, "stopped": word}, stored=True)

    def test_a_generic_bridge_failure_in_a_run_passes_the_check(self):
        """The case that showed the need: an AutoCount read fails in a
        generic way inside a run; the code reaches the stored run summary as
        a count key, an alert code and the ``stopped`` text."""

        self.guest_with_order(1)
        for returncode in (1, 0xE0434352):
            code = dl.bridge_failure_code(b"", returncode)
            self.book.fail_read = code
            self.assertEqual(self.code(lambda: self.engine().run(apply=True)), code)
            stored = json.loads(self.ledger.conn.execute("SELECT summary FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()[0])
            self.assertEqual((stored["stopped"], stored["alerts"], stored["counts"].get(code), stored["saves"]), (code, [code], 1, 0))
            self.assert_outputs_private()
            self.advance(minutes=15)
        self.assertEqual((self.book.saves, self.receipt(1)["state"]), (0, "ready"))


# --------------------------------------------------------------------------
# Slice B1: ledger v2 (G0 design 1.2). The v1 schema text below is main's at
# ff053a5, kept here only to prove that a ledger made by that code is refused.
# --------------------------------------------------------------------------

V1_LEDGER_SCHEMA = """
CREATE TABLE meta(
  id INTEGER PRIMARY KEY CHECK (id = 1), tool TEXT NOT NULL CHECK (tool = 'xb268_member_benefits'), ledger_id TEXT NOT NULL,
  autocount_book TEXT NOT NULL CHECK (autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')),
  store_gid TEXT NOT NULL, shop_domain TEXT NOT NULL, ref_key TEXT NOT NULL CHECK (length(ref_key) = 64), created_at TEXT NOT NULL);
CREATE TABLE receipts(
  order_gid TEXT PRIMARY KEY, customer_gid TEXT, order_created_at TEXT NOT NULL, order_date_sgt TEXT NOT NULL,
  order_updated_at TEXT NOT NULL, financial_status TEXT, gross_cents INTEGER, net_cents INTEGER, refunded_cents INTEGER,
  gift_card_cents INTEGER, payment_gateways TEXT,
  state TEXT NOT NULL CHECK (state IN ('ignored_unpaid', 'ignored_test', 'void', 'before_start', 'no_customer', 'amount_unverifiable',
    'below_threshold', 'not_signed_up', 'pending_signup', 'waiting_member', 'ready', 'applied', 'no_change', 'not_in_scope', 'held')),
  code TEXT, member_no TEXT, link_source TEXT CHECK (link_source IS NULL OR link_source IN ('sync155', 'claim')),
  basis_expiry_local TEXT, observed TEXT, observed_at TEXT, waiting_since TEXT, checked_at TEXT, alerted TEXT,
  first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK (state <> 'applied' OR (member_no IS NOT NULL AND basis_expiry_local IS NOT NULL)));
CREATE TABLE claim_links(
  customer_gid TEXT NOT NULL UNIQUE, member_no TEXT NOT NULL, member_no_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL CHECK (source IN ('order', 'index')), code TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE reviews(subject TEXT PRIMARY KEY, code TEXT NOT NULL, first_seen_at TEXT NOT NULL);
CREATE TABLE writes(
  write_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, order_gid TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('upgrade', 'extend')),
  member_no TEXT NOT NULL, from_type TEXT NOT NULL, to_type TEXT NOT NULL CHECK (to_type = 'MEMBER'),
  expected_last_modified_local TEXT NOT NULL, expected_expiry_local TEXT, new_expiry_local TEXT NOT NULL, salt TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('INTENT', 'DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD')),
  code TEXT, post_last_modified_local TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL, child_pid INTEGER, child_created TEXT,
  CHECK ((kind = 'upgrade' AND from_type = 'GUEST') OR (kind = 'extend' AND from_type = 'MEMBER')),
  CHECK (new_expiry_local GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T00:00:00'),
  CHECK (expected_expiry_local IS NULL OR substr(new_expiry_local, 1, 10) > substr(expected_expiry_local, 1, 10)),
  CHECK (state <> 'DONE' OR post_last_modified_local IS NOT NULL));
CREATE TABLE watermarks(name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
CREATE TRIGGER meta_immutable BEFORE UPDATE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta BEGIN SELECT RAISE(ABORT, 'meta_immutable'); END;
CREATE TRIGGER receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'receipt_immutable'); END;
CREATE TRIGGER receipts_binding_immutable BEFORE UPDATE OF order_gid, order_created_at, first_seen_at ON receipts
  BEGIN SELECT RAISE(ABORT, 'receipt_immutable'); END;
CREATE TRIGGER receipts_decided_stay BEFORE UPDATE OF state ON receipts
  WHEN OLD.state IN ('applied', 'no_change', 'not_in_scope', 'held') AND NEW.state <> OLD.state
   AND NOT (OLD.state = 'held' AND NEW.state = 'ready' AND COALESCE(OLD.code, '') IN ('claim_name_mismatch', 'claim_phone_not_unique',
            'claim_link_conflict', 'claim_memberno_conflict', 'claim_member_not_in_scope', 'claim_no_phone'))
  BEGIN SELECT RAISE(ABORT, 'receipt_decided'); END;
CREATE TRIGGER receipts_decided_code BEFORE UPDATE OF code ON receipts
  WHEN OLD.state IN ('applied', 'no_change', 'not_in_scope', 'held') AND NEW.state = OLD.state AND NEW.code IS NOT OLD.code
   AND NOT (OLD.state = 'held' AND NEW.code IS NOT NULL
            AND COALESCE(OLD.code, '') IN ('claim_name_mismatch', 'claim_phone_not_unique',
                'claim_link_conflict', 'claim_memberno_conflict', 'claim_member_not_in_scope', 'claim_no_phone')
            AND NEW.code NOT IN ('claim_name_mismatch', 'claim_phone_not_unique',
                'claim_link_conflict', 'claim_memberno_conflict', 'claim_member_not_in_scope', 'claim_no_phone'))
  BEGIN SELECT RAISE(ABORT, 'receipt_decided'); END;
CREATE TRIGGER claim_links_immutable BEFORE UPDATE ON claim_links BEGIN SELECT RAISE(ABORT, 'claim_link_immutable'); END;
CREATE TRIGGER claim_links_no_delete BEFORE DELETE ON claim_links BEGIN SELECT RAISE(ABORT, 'claim_link_immutable'); END;
CREATE TRIGGER writes_no_delete BEFORE DELETE ON writes BEGIN SELECT RAISE(ABORT, 'write_immutable'); END;
CREATE TRIGGER writes_binding_immutable BEFORE UPDATE OF write_id, run_id, order_gid, kind, member_no, from_type, to_type,
  expected_last_modified_local, expected_expiry_local, new_expiry_local, salt, created_at, deadline_at ON writes
  BEGIN SELECT RAISE(ABORT, 'write_immutable'); END;
CREATE TRIGGER writes_transition BEFORE UPDATE OF state ON writes
  WHEN NOT ((OLD.state = 'INTENT' AND NEW.state IN ('DONE', 'SKIPPED', 'NOT_WRITTEN', 'UNCERTAIN', 'HELD'))
         OR (OLD.state = 'UNCERTAIN' AND NEW.state IN ('DONE', 'NOT_WRITTEN', 'HELD')))
  BEGIN SELECT RAISE(ABORT, 'write_transition_invalid'); END;
"""

def make_v1_ledger(root: Path) -> bytes:
    """A ledger made by main's code at ff053a5 (schema v1, one meta row);
    returns the file's bytes."""
    conn = sqlite3.connect(root / bm.LEDGER_FILE, isolation_level=None)
    try:
        for statement in dl._split_sql(V1_LEDGER_SCHEMA):
            conn.execute(statement)
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO meta(id, tool, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at) VALUES "
                     "(1, 'xb268_member_benefits', 'mb-1', 'AED_TESTINGAPI', ?, ?, ?, '2026-11-17T00:00:00Z')", (STORE_GID, SHOP, "0" * 64))
    finally:
        conn.close()
    return (root / bm.LEDGER_FILE).read_bytes()


class LedgerV2Tests(Base):
    run_main = CliTests.run_main
    # Commands that open the ledger; slice B1 adds release-held and unlink.
    LEDGER_COMMANDS = (("init",), ("status",), ("observe",), ("apply",), ("resolve",), ("index",), ("index", "--apply"), ("digest",),
                       ("release-held", "--order-ref", "o-" + "a" * 20, "--member-last3", "001", "--finding", "as_intended", "--apply"),
                       ("unlink", "--customer-ref", "c-" + "a" * 20, "--member-last3", "001", "--reason", "other", "--apply"))

    def setUp(self):
        super().setUp()
        self.counter = iter(range(500, 900))

    def add_receipt(self, state: str, code: str | None = None, **values) -> str:
        gid = order_gid(next(self.counter))
        columns = {"order_gid": gid, "order_created_at": "t", "order_date_sgt": "d", "order_updated_at": "t", "state": state, "code": code,
                   "member_no": "m", "basis_expiry_local": "e", "first_seen_at": "t", "updated_at": "t", **values}
        with self.ledger.tx() as conn:
            conn.execute(f"INSERT INTO receipts({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))
        return gid

    def add_write(self, state: str, order: str | None = None, member_no: str | None = None) -> str:
        payload = {"kind": "upgrade", "member_no": member_no or sg(1), "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                   "expected_expiry_local": None, "new_expiry_local": NEW_EXPIRY, "salt": "s"}
        row = self.ledger.add_intent(run_id="run-x", order_gid=order or order_gid(next(self.counter)), payload=payload, deadline=self.now)
        if state != "INTENT":
            self.ledger.set_outcome(row["write_id"], state, "c", LM if state == "DONE" else None)
        return row["write_id"]

    def void(self, link_id: int) -> None:
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO claim_unlinks(link_id, reason, run_id, created_at) VALUES (?, 'wrong_person', 'run-x', 't')", (link_id,))

    def link_id(self, gid: str) -> int:
        return self.ledger.conn.execute("SELECT MAX(link_id) FROM claim_links WHERE customer_gid = ?", (gid,)).fetchone()[0]

    def refused(self, sql: str, params=(), message: str | None = None) -> None:
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            with self.ledger.tx() as conn:
                conn.execute(sql, params)
        if message is not None:
            self.assertIn(message, str(caught.exception))

    # -- the version step -------------------------------------------------------
    def test_a_v1_ledger_is_refused_by_every_command_and_left_as_it_is(self):
        before = make_v1_ledger(self.root)
        for args in self.LEDGER_COMMANDS:
            with self.subTest(args=args):
                code, out, _ = self.run_main(*args)
                self.assertEqual((code, out), (2, {"status": "refused", "code": "benefits_ledger_not_recognised"}))
        self.assertEqual((self.root / bm.LEDGER_FILE).read_bytes(), before)
        self.assertFalse((self.root / (bm.LEDGER_FILE + "-wal")).exists())
        self.assertEqual(self.code(lambda: bm.BenefitsLedger(self.root)), "benefits_ledger_not_recognised")

    def test_a_new_ledger_is_v2_with_the_rule_fingerprint_and_every_pragma(self):
        self.engine()
        conn = self.ledger.conn
        self.assertEqual(bm.SCHEMA_VERSION, 2)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual([conn.execute(f"PRAGMA {name}").fetchone()[0] for name in ("foreign_keys", "trusted_schema", "recursive_triggers")], [1, 0, 1])
        self.assertEqual(conn.execute("SELECT rule_fingerprint FROM meta").fetchone()[0], self.cfg().rule_fingerprint())
        self.assertRegex(self.cfg().rule_fingerprint(), r"^sha256:[0-9a-f]{64}\Z")
        names = {row[0]: row[1] for row in conn.execute("SELECT name, type FROM sqlite_master")}
        for table in ("claim_unlinks", "write_releases"):
            self.assertEqual(names.get(table), "table")
        self.assertEqual(names.get("active_claim_links"), "view")
        for trigger in ("claim_links_one_active", "claim_links_pair_unlinked", "write_releases_held_only", "claim_unlinks_immutable",
                        "claim_unlinks_no_delete", "write_releases_immutable", "write_releases_no_delete"):
            self.assertEqual(names.get(trigger), "trigger", trigger)
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(claim_links)")}
        self.assertEqual(columns["link_id"][5], 1)  # the primary key
        self.assertEqual([row for row in conn.execute("PRAGMA index_list(claim_links)")], [])  # no UNIQUE left on the columns
        for index, bad in enumerate(("sha256:abc", "md5:" + "0" * 67, None)):
            with self.subTest(fingerprint=bad):
                other = self.temp / f"fp-{index}"
                other.mkdir()
                ledger = bm.BenefitsLedger(other)
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        with ledger.tx() as c:
                            c.execute("INSERT INTO meta(id, tool, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at, "
                                      "rule_fingerprint) VALUES (1, 'xb268_member_benefits', 'x', 'AED_TESTINGAPI', 's', 'd', ?, 't', ?)",
                                      ("0" * 64, bad))
                finally:
                    ledger.close()

    # -- append-only tables -----------------------------------------------------
    def test_every_append_only_table_refuses_update_delete_and_insert_or_replace(self):
        self.engine()
        self.ledger.add_claim(customer_gid(1), sg(1), "order", "phone_and_name_match")
        link = self.link_id(customer_gid(1))
        self.void(link)
        held = self.add_write("HELD")
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO write_releases(write_id, finding, member_read_digest, run_id, created_at) VALUES (?, 'as_intended', ?, 'run-x', 't')",
                         (held, "sha256:" + "0" * 64))
        receipt = self.add_receipt("ready")
        snapshot = {table: [tuple(r) for r in self.ledger.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("meta", "receipts", "claim_links", "claim_unlinks", "writes", "write_releases")}
        cases = {
            "meta": ["UPDATE meta SET shop_domain = 'x'", "DELETE FROM meta",
                     "INSERT OR REPLACE INTO meta SELECT * FROM meta"],
            "receipts": ["UPDATE receipts SET order_gid = 'x'", "UPDATE receipts SET first_seen_at = 'x'", "DELETE FROM receipts",
                         "INSERT OR REPLACE INTO receipts SELECT * FROM receipts"],
            "claim_links": ["UPDATE claim_links SET member_no = 'x'", "UPDATE claim_links SET link_id = 99", "DELETE FROM claim_links",
                            "INSERT OR REPLACE INTO claim_links SELECT * FROM claim_links"],
            "claim_unlinks": ["UPDATE claim_unlinks SET reason = 'other'", "DELETE FROM claim_unlinks",
                              "INSERT OR REPLACE INTO claim_unlinks SELECT * FROM claim_unlinks"],
            "writes": ["UPDATE writes SET member_no = 'x'", "UPDATE writes SET state = 'DONE'", "DELETE FROM writes",
                       "INSERT OR REPLACE INTO writes SELECT * FROM writes"],
            "write_releases": ["UPDATE write_releases SET finding = 'not_as_intended'", "DELETE FROM write_releases",
                               "INSERT OR REPLACE INTO write_releases SELECT * FROM write_releases"],
        }
        for table, statements in cases.items():
            for sql in statements:
                with self.subTest(sql=sql):
                    self.refused(sql)
        for table, rows in snapshot.items():
            self.assertEqual([tuple(r) for r in self.ledger.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")], rows, table)
        self.assertEqual(receipt, self.ledger.conn.execute("SELECT order_gid FROM receipts").fetchone()[0])

    # -- the two claim triggers -------------------------------------------------
    def test_the_claim_triggers_keep_one_active_link_and_never_link_a_voided_pair_again(self):
        self.engine()
        add = self.ledger.add_claim
        add(customer_gid(1), sg(1), "order", "phone_and_name_match")
        for gid, member_no in ((customer_gid(1), sg(2)), (customer_gid(2), sg(1)), (customer_gid(2), sg(1) + " "), (customer_gid(1), sg(1))):
            with self.subTest(gid=gid, member_no=member_no), self.assertRaises(sqlite3.IntegrityError) as caught:
                add(gid, member_no, "index", "phone_and_name_match")
            self.assertIn("claim_link_conflict", str(caught.exception))
        self.void(self.link_id(customer_gid(1)))
        for member_no in (sg(1), " " + sg(1) + " "):  # the same pair (AutoCount ignores the spaces)
            with self.subTest(member_no=member_no), self.assertRaises(sqlite3.IntegrityError) as caught:
                add(customer_gid(1), member_no, "order", "phone_and_name_match")
            self.assertIn("claim_pair_unlinked", str(caught.exception))
        add(customer_gid(1), sg(3), "order", "phone_and_name_match")  # the customer, to another member
        add(customer_gid(4), sg(1), "index", "phone_and_name_match")  # the member, to another customer
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            add(customer_gid(5), sg(3), "order", "phone_and_name_match")
        self.assertIn("claim_link_conflict", str(caught.exception))
        self.assertEqual(self.ledger.claim_counts(), (2, 1))
        self.assertEqual({r["customer_gid"]: r["member_no"] for r in self.ledger.active_claims()}, {customer_gid(1): sg(3), customer_gid(4): sg(1)})
        self.refused("INSERT INTO claim_unlinks(link_id, reason, run_id, created_at) VALUES (999, 'other', 'r', 't')")  # no such link
        self.refused("INSERT INTO claim_unlinks(link_id, reason, run_id, created_at) VALUES (?, 'mistake', 'r', 't')",
                     (self.link_id(customer_gid(4)),))  # reason not listed
        self.refused("INSERT INTO claim_unlinks(link_id, reason, run_id, created_at) VALUES (?, 'other', 'r', 't')",
                     (self.ledger.conn.execute("SELECT MIN(link_id) FROM claim_links").fetchone()[0],))  # one void per link

    # -- the code trigger -------------------------------------------------------
    def test_a_held_claim_receipt_settles_only_to_the_three_settle_codes(self):
        self.engine()
        self.assertEqual(bm.CLAIM_SETTLE_CODES, {"order_no_longer_qualifies", "refund_unattributed", "order_past_read_window"})
        self.assertIn("claim_pair_unlinked", bm.CLAIM_HOLD_CODES)
        others = sorted(bm.CLAIM_HOLD_CODES) + ["link_conflict", "member_inactive", "held_write_released", "would_upgrade", "anything_else", None]
        for old in sorted(bm.CLAIM_HOLD_CODES):
            for new in sorted(bm.CLAIM_SETTLE_CODES):
                with self.subTest(old=old, new=new):
                    gid = self.add_receipt("held", old)
                    self.ledger.update_receipt(gid, code=new)
                    self.assertEqual(self.ledger.receipt(gid)["code"], new)
            for new in others:
                if new == old:
                    continue
                with self.subTest(old=old, new=new):
                    gid = self.add_receipt("held", old)
                    with self.assertRaises(sqlite3.IntegrityError) as caught:
                        self.ledger.update_receipt(gid, code=new)
                    self.assertIn("receipt_decided", str(caught.exception))
            with self.subTest(old=old, reopen=True):
                gid = self.add_receipt("held", old)
                self.ledger.update_receipt(gid, state="ready", code=None)  # the one way back, claim_pair_unlinked included
        for old in sorted(bm.CLAIM_SETTLE_CODES):  # a settled code never moves again
            for new in sorted(bm.CLAIM_SETTLE_CODES - {old}):
                with self.subTest(settled=old, new=new):
                    gid = self.add_receipt("held", old)
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.ledger.update_receipt(gid, code=new)

    # -- write releases ---------------------------------------------------------
    def test_a_release_row_needs_a_held_write_and_is_one_per_write(self):
        self.engine()
        sql = "INSERT INTO write_releases(write_id, finding, member_read_digest, run_id, created_at) VALUES (?, ?, ?, 'run-x', 't')"
        digest = "sha256:" + "a" * 64
        for state in ("INTENT", "DONE", "SKIPPED", "NOT_WRITTEN", "UNCERTAIN"):
            with self.subTest(state=state):
                self.refused(sql, (self.add_write(state), "as_intended", digest), "release_not_held")
        self.refused(sql, ("write-none", "as_intended", digest), "release_not_held")
        held = self.add_write("HELD")
        for finding, read in (("seen", digest), ("as_intended", "sha256:short"), ("as_intended", "md5:" + "a" * 67)):
            with self.subTest(finding=finding, read=read):
                self.refused(sql, (held, finding, read))
        with self.ledger.tx() as conn:
            conn.execute(sql, (held, "not_as_intended", digest))
        self.refused(sql, (held, "as_intended", digest))  # one release per write
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes WHERE write_id = ?", (held,)).fetchone()[0], "HELD")

    # -- every reader reads active links ----------------------------------------
    def test_a_voided_link_is_read_by_no_reader(self):
        member_no = self.guest_with_order(1, linked=False)
        engine = self.engine()
        self.ledger.add_claim(customer_gid(1), member_no, "order", "phone_and_name_match")
        self.ledger.add_claim(customer_gid(2), sg(2), "index", "phone_and_name_match")
        self.void(self.link_id(customer_gid(1)))
        self.assertIsNone(self.ledger.claim_for_gid(customer_gid(1)))
        self.assertIsNone(self.ledger.claim_for_member(member_no))
        self.assertIsNotNone(self.ledger.claim_for_gid(customer_gid(2)))
        engine._load_links()
        self.assertEqual(engine.linked_gids, {customer_gid(2)})
        status = engine.status()
        self.assertEqual((status["claim_links"], status["claim_links_voided"]), (1, 1))
        digest = bm.ledger_digest(self.root, hours=24, now=self.now, config=self.cfg())
        self.assertEqual((digest["open"]["claim_links"], digest["open"]["claim_links_voided"]), (1, 1))
        # A voided claim no longer blocks a #155 link's member (link_conflicts sees active links only).
        self.link(3, member_no)
        engine._load_links()
        self.assertEqual((engine.conflict_gids, engine.conflict_keys), (set(), set()))

    def test_no_sql_outside_the_schema_reads_claim_links_directly(self):
        source = (ROOT / "directlocal" / "xb_member_benefits.py").read_text(encoding="utf-8")
        self.assertIn(bm.LEDGER_SCHEMA, source)
        rest = source.replace(bm.LEDGER_SCHEMA, "")
        for node in ast.walk(ast.parse(source)):
            if getattr(node, "name", None) in self.CLAIM_LINKS_READERS:
                rest = rest.replace(ast.get_source_segment(source, node), "")
        self.assertNotRegex(rest, r"(?i)\b(FROM|JOIN)\s+claim_links\b")
        self.assertRegex(source, r"FROM active_claim_links")

    # The unlink code (slice B1, 1.6) is the one place outside the schema
    # that reads claim_links itself (a voided pair is history, not active).
    CLAIM_LINKS_READERS = frozenset({"pair_unlinked"})


class ReleaseHeldTests(Base):
    """Slice B1 (G0 design 1.5): release-held clears the latch of one HELD
    write after the owner checked the member; one read-only proof read; the
    write row stays HELD (never sent again) and the release is a new row."""

    run_main = CliTests.run_main

    def held(self, n: int = 1) -> tuple[str, str]:
        """Order n's save lands with another column changed: HELD, receipt
        left ready, the member saved as MEMBER with the new expiry."""
        member_no = self.guest_with_order(n)
        self.book.mode = "mutate"
        out = self.engine().run(apply=True)
        self.book.mode = None
        self.assertEqual(out["stopped"], "held:readback_fields_changed")
        self.assertEqual(self.receipt(n)["state"], "ready")
        self.advance(seconds=PAST_DEADLINE)
        return self.ledger.ref("o", order_gid(n)), member_no

    def release(self, ref: str, last3: str, finding: str, *, apply: bool = True, **cfg) -> dict:
        return self.engine(**cfg).release_held(order_ref=ref, member_last3=last3, finding=finding, apply=apply)

    def recorded(self) -> tuple:
        conn = self.ledger.conn
        return ([tuple(r) for r in conn.execute("SELECT * FROM write_releases ORDER BY rowid")],
                [tuple(r) for r in conn.execute("SELECT * FROM runs ORDER BY rowid")],
                [tuple(r) for r in conn.execute("SELECT * FROM receipts ORDER BY order_gid")],
                [tuple(r) for r in conn.execute("SELECT * FROM writes ORDER BY rowid")],
                [tuple(r) for r in conn.execute("SELECT * FROM watermarks ORDER BY name")], self.book.saves)

    def refused(self, call, code: str) -> None:
        before = self.recorded()
        self.assertEqual(self.code(call), code)
        self.assertEqual(self.recorded(), before)

    def test_as_intended_moves_the_ready_receipt_to_applied_and_clears_the_latch(self):
        ref, member_no = self.held()
        self.assertEqual(self.code(lambda: self.engine().run(apply=True)), "benefit_held_rows_present")
        before, reads = self.recorded(), self.book.reads
        dry = self.release(ref, member_no[-3:], "as_intended", apply=False)
        self.assertEqual(self.recorded(), before)  # a dry run records nothing
        self.assertEqual((dry["command"], dry["run_id"], dry["receipt_now"], dry["held_writes_left"], self.book.reads),
                         ("release_held_dry_run", None, "applied", 0, reads + 1))
        out = self.release(ref, member_no[-3:], "as_intended")
        self.assertEqual({k: out[k] for k in ("status", "command", "finding", "receipt_now", "held_writes_left", "counts", "alerts")},
                         {"status": "ok", "command": "release_held", "finding": "as_intended", "receipt_now": "applied", "held_writes_left": 0,
                          "counts": {"release_receipt_applied": 1}, "alerts": []})
        self.assertEqual(out["refs"], [{"ref": ref, "code": "write_released", "member_no_masked": bm.mask(member_no)}])
        conn = self.ledger.conn
        write = conn.execute("SELECT * FROM writes").fetchone()
        release = conn.execute("SELECT * FROM write_releases").fetchone()
        self.assertEqual((write["state"], release["write_id"], release["finding"], release["run_id"]), ("HELD", write["write_id"], "as_intended", out["run_id"]))
        self.assertEqual(release["member_read_digest"][:7], "sha256:")
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (out["run_id"],)).fetchone()
        self.assertEqual((run["command"], json.loads(run["summary"])), ("release_held", out))
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"], row["member_no"], row["basis_expiry_local"]), ("applied", "upgrade", member_no, NEW_EXPIRY))
        self.assertEqual(self.engine().status()["held_writes"], 0)
        self.assertEqual(bm.ledger_digest(self.root, hours=24, now=self.now, config=self.cfg())["open"]["held_writes"], 0)
        # The latch is clear: apply runs again and never sends this receipt again.
        self.advance(minutes=15)
        again = self.engine().run(apply=True)
        self.assertEqual((again["saves"], self.book.saves, conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]), (0, 1, 1))
        self.assertTrue(self.ledger.write_landed_or_open(order_gid(1)))  # HELD stays in the list, released or not
        # The same write is never released twice.
        self.refused(lambda: self.release(ref, member_no[-3:], "as_intended"), "release_no_held_write")

    def test_not_as_intended_holds_the_ready_receipt_for_good(self):
        ref, member_no = self.held()
        self.book.members[member_no].update(type="GUEST", expiry=None)  # staff put the member back by hand
        out = self.release(ref, member_no[-3:], "not_as_intended")
        self.assertEqual((out["receipt_now"], out["counts"]), ("held", {"release_receipt_held": 1}))
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "held_write_released"))
        self.advance(minutes=15)
        again = self.engine().run(apply=True)
        self.assertEqual((again["saves"], self.book.saves, self.receipt(1)["state"]), (0, 1, "held"))
        with self.assertRaises(sqlite3.IntegrityError):  # terminal: never re-opened
            self.ledger.update_receipt(order_gid(1), state="ready", code=None)

    def test_each_proof_contradiction_refuses_with_nothing_recorded(self):
        ref, member_no = self.held()
        m = self.book.members[member_no]
        saved = dict(m)
        cases = [
            ("as_intended", dict(type="GUEST")),
            ("as_intended", dict(expiry="2028-11-19T00:00:00.000")),
            ("as_intended", dict(expiry=NEW_EXPIRY)),  # the stored form always has milliseconds
            ("as_intended", dict(type="VIP")),
            ("as_intended", None),  # not found
            ("not_as_intended", {}),  # exactly as the tool meant
        ]
        for finding, change in cases:
            with self.subTest(finding=finding, change=change):
                self.book.members[member_no] = dict(saved)
                if change is None:
                    del self.book.members[member_no]
                else:
                    self.book.members[member_no].update(change)
                self.refused(lambda: self.release(ref, member_no[-3:], finding), "release_finding_not_proven")
        self.book.members[member_no] = dict(saved)
        del self.book.members[member_no]
        self.assertEqual(self.release(ref, member_no[-3:], "not_as_intended", apply=False)["receipt_now"], "held")  # not found proves it

    def test_every_refusal_records_nothing(self):
        ref, member_no = self.held()
        last3 = member_no[-3:]
        for bad in (dict(ref=None), dict(ref="o-" + "a" * 19), dict(ref="c-" + "a" * 20), dict(ref=ref + "\n"), dict(ref=ref.upper()),
                    dict(last3=None), dict(last3=""), dict(last3="0012"), dict(last3="01 "), dict(last3="00\n"),
                    dict(finding=None), dict(finding="intended"), dict(finding="AS_INTENDED")):
            args = {"ref": ref, "last3": last3, "finding": "as_intended", **bad}
            with self.subTest(bad=bad):
                self.refused(lambda: self.release(args["ref"], args["last3"], args["finding"]), "release_arguments_invalid")
        self.refused(lambda: self.release("o-" + "0" * 20, last3, "as_intended"), "release_no_held_write")
        self.refused(lambda: self.release(ref, "999" if last3 != "999" else "998", "as_intended"), "release_member_mismatch")
        self.book.fail_read = "bridge_read_failed:benefit_member_read_failed"
        self.refused(lambda: self.release(ref, last3, "as_intended"), "bridge_read_failed:benefit_member_read_failed")
        self.book.fail_read = None
        self.probe_result = "running"
        self.refused(lambda: self.release(ref, last3, "as_intended"), "writer_may_be_live")
        self.probe_result = "exited"
        self.now -= timedelta(seconds=PAST_DEADLINE)  # before the write's deadline and grace
        self.refused(lambda: self.release(ref, last3, "as_intended"), "writer_may_be_live")
        self.advance(seconds=PAST_DEADLINE)
        # An open write anywhere refuses (resolve first).
        payload = {"kind": "upgrade", "member_no": sg(9), "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                   "expected_expiry_local": None, "new_expiry_local": NEW_EXPIRY, "salt": "s"}
        intent = self.ledger.add_intent(run_id="run-x", order_gid=order_gid(9), payload=payload, deadline=self.now)
        self.refused(lambda: self.release(ref, last3, "as_intended"), "benefit_uncertain_unresolved")
        self.ledger.set_outcome(intent["write_id"], "UNCERTAIN", "writer_timeout")
        self.refused(lambda: self.release(ref, last3, "as_intended"), "benefit_uncertain_unresolved")
        self.ledger.set_outcome(intent["write_id"], "NOT_WRITTEN", "resolved_no_claim")
        self.assertEqual(self.release(ref, last3, "as_intended")["receipt_now"], "applied")

    def test_fix1_n12_the_last_3_is_compared_exactly_never_as_a_shorter_suffix(self):
        ref, member_no = self.held()
        for short in (member_no[-2:], member_no[-1:]):  # a true suffix of the MemberNo, but not its last 3 characters
            with self.subTest(short=short):
                self.refused(lambda: self.release(ref, short, "as_intended"), "release_member_mismatch")
        self.assertEqual(self.release(ref, member_no[-3:], "as_intended", apply=False)["receipt_now"], "applied")

    def test_a_receipt_decided_in_between_is_left_as_it_is(self):
        ref, member_no = self.held()
        self.ledger.update_receipt(order_gid(1), state="held", code="link_conflict")
        out = self.release(ref, member_no[-3:], "as_intended")
        self.assertEqual((out["receipt_now"], out["counts"]), ("left_decided", {"release_receipt_left_decided": 1}))
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "link_conflict"))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM write_releases").fetchone()[0], 1)

    def test_two_held_writes_are_released_one_at_a_time_and_the_latch_stays_until_both(self):
        ref1, member1 = self.held(1)
        member2 = self.guest_with_order(2)
        self.advance(minutes=15)
        self.engine().run(apply=False)  # order 2 recorded and observed, still ready
        payload = {"kind": "upgrade", "member_no": member2, "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                   "expected_expiry_local": None, "new_expiry_local": NEW_EXPIRY, "salt": "s"}
        intent = self.ledger.add_intent(run_id="run-x", order_gid=order_gid(2), payload=payload, deadline=self.now)
        self.ledger.set_outcome(intent["write_id"], "HELD", "resolved_unclear")
        self.advance(seconds=PAST_DEADLINE)
        ref2 = self.ledger.ref("o", order_gid(2))
        self.assertEqual(self.release(ref1, member1[-3:], "as_intended")["held_writes_left"], 1)
        self.assertEqual(self.code(lambda: self.engine().run(apply=True)), "benefit_held_rows_present")
        self.assertEqual(self.engine().status()["held_writes"], 1)
        out = self.release(ref2, member2[-3:], "not_as_intended")  # the member is still GUEST: not saved
        self.assertEqual((out["held_writes_left"], out["receipt_now"]), (0, "held"))
        self.advance(minutes=15)
        self.assertEqual(self.engine().run(apply=True)["saves"], 0)
        self.assertEqual(self.book.saves, 1)

    def test_g15_a_released_held_write_whose_receipt_is_still_ready_is_never_sent_again(self):
        """G15 (R1:41): the per-receipt check is the last guard once a HELD
        write is released and its receipt is still ready."""
        ref, member_no = self.held()
        write_id = self.ledger.conn.execute("SELECT write_id FROM writes").fetchone()[0]
        with self.ledger.tx() as conn:  # set up through the ledger: released, receipt left ready
            conn.execute("INSERT INTO write_releases(write_id, finding, member_read_digest, run_id, created_at) VALUES (?, 'as_intended', ?, 'run-x', 't')",
                         (write_id, "sha256:" + "0" * 64))
        self.assertEqual((self.ledger.held_writes(), self.receipt(1)["state"]), (0, "ready"))
        self.book.members[member_no].update(type="GUEST", expiry=None, last_modified=LM)  # would be sent again if anything let it
        reads, saves = self.book.reads, self.book.saves
        self.advance(minutes=15)
        out = self.engine().run(apply=True)
        self.assertEqual((out["counts"].get("write_already_recorded"), out["saves"]), (1, 0))
        self.assertEqual((self.book.reads, self.book.saves, len(self.book.payloads)), (reads, saves, 1))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 1)

    def test_the_cli_command_dry_run_apply_and_refusals(self):
        member_no = self.guest_with_order(1)
        self.run_main("init")
        self.book.mode = "mutate"
        self.assertEqual(self.run_main("apply")[0], 3)
        self.book.mode = None
        self.advance(seconds=PAST_DEADLINE)
        ref = self.ledger_ref(order_gid(1))
        args = ("release-held", "--order-ref", ref, "--member-last3", member_no[-3:], "--finding", "as_intended")
        code, dry, _ = self.run_main(*args)
        self.assertEqual((code, dry["command"], dry["receipt_now"]), (0, "release_held_dry_run", "applied"))
        # Not refused by the write switch or a changed amount setting (it measures nothing).
        code, out, _ = self.run_main(*args, "--apply", cfg={"benefits_write_enabled": False, "qualify_threshold_cents": 25_000})
        self.assertEqual((code, out["command"], out["held_writes_left"]), (0, "release_held", 0))
        self.assertEqual(self.run_main("status")[1]["held_writes"], 0)
        for bad in (("release-held",), ("release-held", "--order-ref", ref), args[:-2] + ("--finding", "seen")):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_main(*bad)[:2], (2, {"status": "refused", "code": "release_arguments_invalid"}))
        self.assertEqual(self.run_main(*args, "--apply")[:2], (2, {"status": "refused", "code": "release_no_held_write"}))

    def ledger_ref(self, value: str) -> str:
        ledger = bm.BenefitsLedger(self.root, clock=lambda: self.now)
        try:
            return ledger.ref("o", value)
        finally:
            ledger.close()

    def test_a_locked_ledger_refuses(self):
        self.run_main("init")
        ledger = bm.BenefitsLedger(self.root, clock=lambda: self.now)  # a running task holds the ledger
        try:
            code, out, _ = self.run_main("release-held", "--order-ref", "o-" + "a" * 20, "--member-last3", "001", "--finding", "as_intended", "--apply")
        finally:
            ledger.close()
        self.assertEqual((code, out), (2, {"status": "refused", "code": "ledger_locked_by_another_owner"}))


class UnlinkTests(Base):
    """Slice B1 (G0 design 1.6): unlink voids one claim link this tool made;
    the link row stays, the void is a new row; a voided pair is never linked
    again by the tool."""

    run_main = CliTests.run_main

    def claimed(self, n: int = 1, *, apply: bool = False) -> tuple[str, str]:
        """Order n's customer is claim-linked to its member by an observe run
        (receipt ready, observed would_upgrade), or upgraded with ``apply``."""
        member_no = self.guest_with_order(n, linked=False)
        out = self.engine().run(apply=apply)
        self.assertEqual(out["counts"].get("claim_linked"), 1)
        self.assertEqual(self.receipt(n)["link_source"], "claim")
        return self.ledger.ref("c", customer_gid(n)), member_no

    def unlink(self, ref: str, last3: str, reason: str = "wrong_person", *, apply: bool = True, accept_applied: bool = False) -> dict:
        return self.engine().unlink(customer_ref=ref, member_last3=last3, reason=reason, accept_applied=accept_applied, apply=apply)

    def recorded(self) -> tuple:
        conn = self.ledger.conn
        return tuple([tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                     for table in ("claim_links", "claim_unlinks", "runs", "receipts", "writes", "watermarks", "reviews")) + (self.book.saves,)

    def refused(self, call, code: str) -> None:
        before = self.recorded()
        self.assertEqual(self.code(call), code)
        self.assertEqual(self.recorded(), before)

    def test_a_ready_receipt_is_reset_and_the_voided_pair_is_held_never_linked_again(self):
        ref, member_no = self.claimed()
        before = self.recorded()
        dry = self.unlink(ref, member_no[-3:], apply=False)
        self.assertEqual(self.recorded(), before)  # a dry run records nothing
        self.assertEqual((dry["command"], dry["run_id"], dry["counts"]), ("unlink_dry_run", None, {"claim_unlinked": 1, "unlink_receipt_reset": 1}))
        out = self.unlink(ref, member_no[-3:])
        self.assertEqual({k: out[k] for k in ("status", "command", "reason", "link_source", "counts", "alerts")},
                         {"status": "ok", "command": "unlink", "reason": "wrong_person", "link_source": "order",
                          "counts": {"claim_unlinked": 1, "unlink_receipt_reset": 1}, "alerts": []})
        self.assertEqual(out["refs"], [{"ref": ref, "code": "claim_unlinked", "member_no_masked": bm.mask(member_no)}])
        conn = self.ledger.conn
        void = conn.execute("SELECT * FROM claim_unlinks").fetchone()
        self.assertEqual((void["reason"], void["run_id"]), ("wrong_person", out["run_id"]))
        self.assertEqual(conn.execute("SELECT command FROM runs WHERE run_id = ?", (out["run_id"],)).fetchone()[0], "unlink")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0], 1)  # the link row stays as history
        row = self.receipt(1)
        self.assertEqual((row["state"], row["member_no"], row["link_source"], row["observed"], row["observed_at"], row["checked_at"]),
                         ("ready", None, None, None, None, None))
        status = self.engine().status()
        self.assertEqual((status["claim_links"], status["claim_links_voided"]), (0, 1))
        # The next run: the same customer and member again -> held, one review alert, no link.
        self.advance(minutes=15)
        out = self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"], out["saves"]), ("held", "claim_pair_unlinked", 0))
        self.assertEqual(out["alerts"], ["claim_needs_review"])
        self.assertIn({"ref": ref, "code": "claim_pair_unlinked", "member_no_masked": bm.mask(member_no)}, out["refs"])
        self.assertEqual(self.ledger.claim_counts(), (0, 1))
        self.assertIn(("unlinked:" + customer_gid(1), "claim_pair_unlinked"), [tuple(r)[:2] for r in conn.execute("SELECT * FROM reviews")])
        # A #155 link for the customer re-opens the hold and is followed (the #155 sync is the master link).
        self.link(1, member_no)
        self.advance(minutes=15)
        out = self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["link_source"], out["saves"]), ("applied", "sync155", 1))

    def test_after_an_unlink_the_customer_and_the_member_may_each_be_claimed_to_another(self):
        ref, member_no = self.claimed()
        self.unlink(ref, member_no[-3:], "customers_merged")
        # The customer's phone is now another member's MemberNo (same name): a new claim to that member.
        self.book.members[sg(5)] = fake_member(sg(5), name="Fake Member 1")
        moved = customer_node(1, phone="+" + sg(5))
        self.shopify.customers[customer_gid(1)] = moved
        self.shopify.add({**self.shopify.orders_nodes[order_gid(1)], "customer": moved, "updatedAt": "2026-11-18T04:10:00Z"})
        # Another customer, with the first member's phone and name.
        self.shopify.add(order_node(6, customer=customer_node(6, phone="+" + sg(1), last="Member 1")))
        self.advance(minutes=15)
        out = self.engine().run(apply=False)
        active = {r["customer_gid"]: r["member_no"] for r in self.ledger.active_claims()}
        self.assertEqual(active, {customer_gid(1): sg(5), customer_gid(6): member_no})
        self.assertEqual(out["counts"].get("claim_linked"), 2)
        self.assertEqual(self.ledger.claim_counts(), (2, 1))

    def test_an_applied_receipt_needs_accept_applied_and_is_never_changed(self):
        ref, member_no = self.claimed(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.book.members[member_no]["type"]), ("applied", "MEMBER"))
        self.refused(lambda: self.unlink(ref, member_no[-3:]), "unlink_has_applied")
        row = tuple(self.receipt(1))
        out = self.unlink(ref, member_no[-3:], accept_applied=True)
        order_ref = self.ledger.ref("o", order_gid(1))
        self.assertEqual((out["counts"], out["alerts"]), ({"claim_unlinked": 1, "unlink_receipt_applied": 1, "unlinked_with_applied": 1},
                                                          ["unlinked_with_applied"]))
        self.assertIn({"ref": order_ref, "code": "unlinked_with_applied", "member_no_masked": bm.mask(member_no)}, out["refs"])
        self.assertEqual(tuple(self.receipt(1)), row)  # immutable: the tool never downgrades
        self.assertEqual((self.book.saves, self.book.members[member_no]["type"]), (1, "MEMBER"))

    def test_decided_receipts_through_the_link_are_kept(self):
        member_no = self.guest_with_order(1, linked=False, member=fake_member(sg(1), name="Fake Member 1", active="F"))
        self.guest_with_order(2, linked=False)
        self.shopify.add(order_node(3, customer=customer_node(1)))  # a second order of customer 1
        self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "member_inactive"))
        # Receipts of the same customer in every other position, set up through the ledger.
        extra = {
            11: ("no_change", "expiry_already_later", member_no, "claim"),
            12: ("not_in_scope", "member_not_in_scope", member_no, "claim"),
            13: ("ready", None, sg(7), "claim"),  # through another member (an earlier link)
            14: ("ready", None, member_no, "sync155"),  # through a #155 link
            15: ("held", "claim_link_conflict", sg(7), "claim"),
        }
        with self.ledger.tx() as conn:
            for n, (state, code, member, source) in extra.items():
                conn.execute("INSERT INTO receipts(order_gid, customer_gid, order_created_at, order_date_sgt, order_updated_at, state, code, member_no, "
                             "link_source, observed, observed_at, first_seen_at, updated_at) VALUES (?, ?, 't', 'd', 't', ?, ?, ?, ?, 'would_upgrade', 't', 't', 't')",
                             (order_gid(n), customer_gid(1), state, code, member, source))
        before = {n: tuple(self.receipt(n)) for n in (1, 3, *extra)}
        out = self.unlink(self.ledger.ref("c", customer_gid(1)), member_no[-3:], "other")
        self.assertEqual(out["counts"], {"claim_unlinked": 1, "unlink_receipt_kept": 4})
        self.assertEqual({n: tuple(self.receipt(n)) for n in before}, before)  # none of them touched
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["member_no"]) for n in (1, 3)], [("held", member_no)] * 2)
        self.assertEqual(self.ledger.claim_counts(), (1, 1))  # customer 2's link stays

    def test_every_refusal_records_nothing(self):
        ref, member_no = self.claimed()
        last3 = member_no[-3:]
        for bad in (dict(ref=None), dict(ref="o-" + "a" * 20), dict(ref="c-" + "a" * 21), dict(ref=ref + "\n"), dict(last3=None),
                    dict(last3="1234"), dict(last3="1 2"), dict(reason=None), dict(reason="mistake"), dict(reason="OTHER"), dict(accept="yes")):
            args = {"ref": ref, "last3": last3, "reason": "other", "accept": False, **bad}
            with self.subTest(bad=bad):
                self.refused(lambda: self.engine().unlink(customer_ref=args["ref"], member_last3=args["last3"], reason=args["reason"],
                                                          accept_applied=args["accept"], apply=True), "unlink_arguments_invalid")
        self.refused(lambda: self.unlink("c-" + "0" * 20, last3), "unlink_not_a_claim_link")
        self.refused(lambda: self.unlink(ref, "999" if last3 != "999" else "998"), "unlink_member_mismatch")
        # A customer linked by #155 only is not this tool's to void.
        self.guest_with_order(4)
        self.engine().run(apply=False)
        self.refused(lambda: self.unlink(self.ledger.ref("c", customer_gid(4)), sg(4)[-3:]), "unlink_not_a_claim_link")
        # An open write anywhere refuses (resolve first).
        payload = {"kind": "upgrade", "member_no": sg(9), "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                   "expected_expiry_local": None, "new_expiry_local": NEW_EXPIRY, "salt": "s"}
        intent = self.ledger.add_intent(run_id="run-x", order_gid=order_gid(9), payload=payload, deadline=self.now)
        self.refused(lambda: self.unlink(ref, last3), "benefit_uncertain_unresolved")
        self.ledger.set_outcome(intent["write_id"], "UNCERTAIN", "writer_timeout")
        self.refused(lambda: self.unlink(ref, last3), "benefit_uncertain_unresolved")
        self.ledger.set_outcome(intent["write_id"], "NOT_WRITTEN", "resolved_no_claim")
        self.assertEqual(self.unlink(ref, last3, apply=False)["counts"]["claim_unlinked"], 1)

    # -- fix round 1 (G2 B13): --order-ref, the second selector -------------
    def by_order(self, order_ref, last3: str, reason: str = "wrong_person", *, apply: bool = True, accept_applied: bool = False) -> dict:
        return self.engine().unlink(order_ref=order_ref, member_last3=last3, reason=reason, accept_applied=accept_applied, apply=apply)

    def test_fix1_b13_an_order_ref_voids_the_claim_link_of_that_receipts_customer_only(self):
        # Two customers claim-linked to two members whose MemberNos share their last 3 characters.
        member_a = self.guest_with_order(1, linked=False)
        member_b = self.guest_with_order(1001, linked=False)
        self.shopify.add(order_node(3, customer=customer_node(1001)))  # a second order of customer 1001
        self.assertEqual((member_a[-3:] == member_b[-3:], member_a == member_b), (True, False))
        self.assertEqual(self.engine().run(apply=False)["counts"].get("claim_linked"), 2)
        before = self.recorded()
        dry = self.by_order(self.ledger.ref("o", order_gid(1001)), member_b[-3:], apply=False)
        self.assertEqual(self.recorded(), before)  # a dry run records nothing
        self.assertEqual((dry["command"], dry["run_id"], dry["counts"]), ("unlink_dry_run", None, {"claim_unlinked": 1, "unlink_receipt_reset": 2}))
        # The other order of the same customer names the same link.
        out = self.by_order(self.ledger.ref("o", order_gid(3)), member_b[-3:])
        self.assertEqual({k: out[k] for k in ("status", "command", "reason", "link_source", "counts", "alerts")},
                         {"status": "ok", "command": "unlink", "reason": "wrong_person", "link_source": "order",
                          "counts": {"claim_unlinked": 1, "unlink_receipt_reset": 2}, "alerts": []})
        self.assertEqual(out["refs"], [{"ref": self.ledger.ref("c", customer_gid(1001)), "code": "claim_unlinked", "member_no_masked": bm.mask(member_b)}])
        self.assertEqual({r["customer_gid"]: r["member_no"] for r in self.ledger.active_claims()}, {customer_gid(1): member_a})
        self.assertEqual(self.ledger.claim_counts(), (1, 1))
        self.assertEqual([(self.receipt(n)["member_no"], self.receipt(n)["link_source"]) for n in (1, 1001, 3)],
                         [(member_a, "claim"), (None, None), (None, None)])
        # The voided link is no longer a claim link for either selector.
        self.refused(lambda: self.by_order(self.ledger.ref("o", order_gid(1001)), member_b[-3:]), "unlink_not_a_claim_link")
        self.refused(lambda: self.unlink(self.ledger.ref("c", customer_gid(1001)), member_b[-3:]), "unlink_not_a_claim_link")
        # The order of the other customer still names its own link (the shared last 3 picks nothing).
        out = self.by_order(self.ledger.ref("o", order_gid(1)), member_a[-3:], apply=False)
        self.assertEqual(out["refs"], [{"ref": self.ledger.ref("c", customer_gid(1)), "code": "claim_unlinked", "member_no_masked": bm.mask(member_a)}])

    def test_fix1_b13_order_ref_refusals_record_nothing(self):
        ref, member_no = self.claimed()
        last3 = member_no[-3:]
        order_ref = self.ledger.ref("o", order_gid(1))
        # Exactly one selector, and every other argument is checked as before.
        both = lambda: self.engine().unlink(customer_ref=ref, order_ref=order_ref, member_last3=last3, reason="other", accept_applied=False, apply=True)
        neither = lambda: self.engine().unlink(member_last3=last3, reason="other", accept_applied=False, apply=True)
        for call in (both, neither, lambda: self.by_order(5, last3), lambda: self.by_order(order_ref.encode(), last3),
                     lambda: self.by_order(order_ref, "1234"), lambda: self.by_order(order_ref, last3, "mistake"),
                     lambda: self.by_order(order_ref, last3, accept_applied="yes")):
            self.refused(call, "unlink_arguments_invalid")
        # A well-formed unknown o- ref names no receipt.
        self.refused(lambda: self.by_order("o-" + "0" * 20, last3), "unlink_not_a_claim_link")
        # Slice B2 (B2-10, Controller answer Q4): a malformed o- ref is refused as an argument, as a malformed c- ref is.
        for bad in ("o-" + order_ref[2:-1], order_ref + "\n", order_ref.upper(), ref, "", "o-"):
            with self.subTest(bad=bad):
                self.refused(lambda: self.by_order(bad, last3), "unlink_arguments_invalid")
        # The last 3 are compared exactly (G2 N13), and a wrong one refuses.
        for short in (member_no[-2:], member_no[-1:], "999" if last3 != "999" else "998"):
            with self.subTest(short=short):
                self.refused(lambda: self.by_order(order_ref, short), "unlink_member_mismatch")
        # A receipt whose customer has no claim link: a customer linked by #155 only, and an order with no customer.
        self.guest_with_order(4)
        self.shopify.add(order_node(5, customer=None))
        self.engine().run(apply=False)
        self.assertEqual((self.receipt(4)["link_source"], self.receipt(5)["state"], self.receipt(5)["customer_gid"]), ("sync155", "no_customer", None))
        self.refused(lambda: self.by_order(self.ledger.ref("o", order_gid(4)), sg(4)[-3:]), "unlink_not_a_claim_link")
        self.refused(lambda: self.by_order(self.ledger.ref("o", order_gid(5)), last3), "unlink_not_a_claim_link")
        self.assertEqual(self.by_order(order_ref, last3, apply=False)["counts"]["claim_unlinked"], 1)

    def test_fix1_n13_the_last_3_is_compared_exactly_never_as_a_shorter_suffix(self):
        ref, member_no = self.claimed()
        for short in (member_no[-2:], member_no[-1:]):  # a true suffix of the MemberNo, but not its last 3 characters
            with self.subTest(short=short):
                self.refused(lambda: self.unlink(ref, short), "unlink_member_mismatch")
        self.assertEqual(self.unlink(ref, member_no[-3:], apply=False)["counts"]["claim_unlinked"], 1)

    def test_fix1_n14_a_receipt_of_another_customer_through_the_same_member_is_not_through_the_link(self):
        ref, member_no = self.claimed()
        with self.ledger.tx() as conn:
            for n, state, code, basis in ((16, "ready", None, None), (17, "applied", "upgrade", NEW_EXPIRY)):
                conn.execute("INSERT INTO receipts(order_gid, customer_gid, order_created_at, order_date_sgt, order_updated_at, state, code, member_no, "
                             "link_source, basis_expiry_local, observed, observed_at, first_seen_at, updated_at) VALUES (?, ?, 't', 'd', 't', ?, ?, ?, "
                             "'claim', ?, 'would_upgrade', 't', 't', 't')", (order_gid(n), customer_gid(2), state, code, member_no, basis))
        before = {n: tuple(self.receipt(n)) for n in (16, 17)}
        out = self.unlink(ref, member_no[-3:])  # no unlink_has_applied: the applied receipt is another customer's
        self.assertEqual(out["counts"], {"claim_unlinked": 1, "unlink_receipt_reset": 1})
        self.assertEqual({n: tuple(self.receipt(n)) for n in before}, before)
        self.assertEqual((self.receipt(1)["member_no"], self.receipt(1)["state"]), (None, "ready"))

    def test_a_held_write_for_the_member_refuses_until_it_is_released(self):
        ref, member_no = self.claimed()
        self.book.mode = "mutate"
        self.advance(minutes=15)
        self.engine().run(apply=True)
        self.book.mode = None
        self.advance(seconds=PAST_DEADLINE)
        self.refused(lambda: self.unlink(ref, member_no[-3:]), "unlink_member_write_held")
        self.engine().release_held(order_ref=self.ledger.ref("o", order_gid(1)), member_last3=member_no[-3:], finding="as_intended", apply=True)
        self.refused(lambda: self.unlink(ref, member_no[-3:]), "unlink_has_applied")
        self.assertEqual(self.unlink(ref, member_no[-3:], accept_applied=True)["counts"]["unlink_receipt_applied"], 1)

    def test_a_save_that_landed_but_was_not_repaired_refuses(self):
        ref, member_no = self.claimed()
        real = self.ledger.update_receipt

        def crash_on_applied(order, **values):
            if values.get("state") == "applied":
                raise RuntimeError("parent killed after the save")
            return real(order, **values)
        self.ledger.update_receipt = crash_on_applied
        self.advance(minutes=15)
        with self.assertRaises(RuntimeError):
            self.engine().run(apply=True)
        self.ledger.update_receipt = real
        self.assertEqual((self.receipt(1)["state"], self.ledger.done_write(order_gid(1)) is not None), ("ready", True))
        self.refused(lambda: self.unlink(ref, member_no[-3:]), "unlink_receipt_needs_repair")
        self.advance(minutes=15)
        self.engine().run(apply=False)  # observe repairs it
        self.assertEqual(self.receipt(1)["state"], "applied")
        self.refused(lambda: self.unlink(ref, member_no[-3:]), "unlink_has_applied")

    def test_the_index_skips_a_voided_pair(self):
        ref, member_no = self.claimed()
        self.unlink(ref, member_no[-3:])
        for apply in (False, True):
            with self.subTest(apply=apply):
                out = self.engine().index(apply=apply, max_members=10)
                self.assertEqual((out["counts"].get("index_pair_unlinked"), out["counts"].get("claim_linked")), (1, None))
        self.assertEqual(self.ledger.claim_counts(), (0, 1))

    def test_the_cli_command_and_its_output(self):
        member_no = self.guest_with_order(1, linked=False)
        self.run_main("init")
        self.assertEqual(self.run_main("observe")[1]["counts"]["claim_linked"], 1)
        ledger = bm.BenefitsLedger(self.root, clock=lambda: self.now)
        try:
            ref = ledger.ref("c", customer_gid(1))
        finally:
            ledger.close()
        args = ("unlink", "--customer-ref", ref, "--member-last3", member_no[-3:], "--reason", "member_renumbered")
        code, dry, _ = self.run_main(*args, cfg={"benefits_write_enabled": False, "qualify_threshold_cents": 25_000})
        self.assertEqual((code, dry["command"]), (0, "unlink_dry_run"))
        code, out, _ = self.run_main(*args, "--apply")
        self.assertEqual((code, out["command"], out["link_source"]), (0, "unlink", "order"))
        status = self.run_main("status")[1]
        self.assertEqual((status["claim_links"], status["claim_links_voided"]), (0, 1))
        digest = self.run_main("digest")[1]
        self.assertEqual((digest["open"]["claim_links"], digest["open"]["claim_links_voided"], digest["counts"]["claim_unlinked"]), (0, 1, 1))
        for bad in (("unlink",), args[:-2], args[:-2] + ("--reason", "seen")):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_main(*bad)[:2], (2, {"status": "refused", "code": "unlink_arguments_invalid"}))
        self.assertEqual(self.run_main(*args, "--apply")[:2], (2, {"status": "refused", "code": "unlink_not_a_claim_link"}))
        self.advance(minutes=15)
        code, out, text = self.run_main("observe")
        self.assertEqual((code, out["alerts"]), (3, ["claim_needs_review"]))
        for secret in (sg(1), local(1), customer_gid(1), order_gid(1), str(customer_number(1)), str(order_number(1)), "Fake Member 1"):
            self.assertNotIn(secret, text)

    def test_fix1_the_cli_takes_order_ref_and_passes_accept_applied_only_when_given(self):
        member_no = self.guest_with_order(1, linked=False)
        self.run_main("init")
        out = self.run_main("apply")[1]
        self.assertEqual((out["saves"], out["counts"].get("claim_linked")), (1, 1))
        ledger = bm.BenefitsLedger(self.root, clock=lambda: self.now)
        try:
            order_ref, customer_ref = ledger.ref("o", order_gid(1)), ledger.ref("c", customer_gid(1))
        finally:
            ledger.close()
        args = ("unlink", "--order-ref", order_ref, "--member-last3", member_no[-3:], "--reason", "wrong_person")
        for extra in ((), ("--apply",)):  # G2 N33: without the flag an applied receipt refuses, dry run or not
            with self.subTest(extra=extra):
                self.assertEqual(self.run_main(*args, *extra)[:2], (2, {"status": "refused", "code": "unlink_has_applied"}))
        self.assertEqual(self.run_main(*args, "--customer-ref", customer_ref, "--accept-applied")[:2],
                         (2, {"status": "refused", "code": "unlink_arguments_invalid"}))  # both selectors
        code, dry, _ = self.run_main(*args, "--accept-applied")
        self.assertEqual((code, dry["command"], dry["counts"]["unlink_receipt_applied"]), (3, "unlink_dry_run", 1))
        code, out, _ = self.run_main(*args, "--accept-applied", "--apply")
        self.assertEqual((code, out["command"], out["alerts"]), (3, "unlink", ["unlinked_with_applied"]))
        self.assertEqual(out["refs"][0], {"ref": customer_ref, "code": "claim_unlinked", "member_no_masked": bm.mask(member_no)})
        self.assertEqual(self.run_main("status")[1]["claim_links_voided"], 1)


class ClaimConflictTriggerTests(Base):
    """Slice B1: the column UNIQUEs of claim_links are now insert triggers.
    The engine's own checks still turn a conflict into a review before any
    insert, on the order path and the index path (never a failed run)."""

    def test_a_member_already_claimed_by_another_customer_is_a_review_on_the_order_path(self):
        member_no = self.guest_with_order(1, linked=False)
        self.engine()
        self.ledger.add_claim(customer_gid(2), member_no, "index", "phone_and_name_match")
        out = self.engine().run(apply=True)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"], out["saves"]), ("held", "claim_link_conflict", 0))
        self.assertIn("claim_needs_review", out["alerts"])
        self.assertEqual(self.ledger.claim_counts(), (1, 0))

    def test_a_customer_already_claimed_to_another_member_is_a_review_on_the_index_path(self):
        self.book.members[sg(1)] = fake_member(sg(1), name="Fake Member 1")
        self.shopify.customers[customer_gid(1)] = customer_node(1)
        self.engine()
        self.ledger.add_claim(customer_gid(1), sg(8), "order", "phone_and_name_match")  # an earlier link to another member
        out = self.engine().index(apply=True, max_members=10)
        self.assertEqual((out["stopped"], out["counts"].get("claim_link_conflict"), out["alerts"]), (None, 1, ["claim_needs_review"]))
        self.assertEqual({r["customer_gid"]: r["member_no"] for r in self.ledger.active_claims()}, {customer_gid(1): sg(8)})

    def test_an_intent_write_keeps_observe_away_from_the_receipt(self):
        member_no = self.guest_with_order(1)
        self.engine()
        payload = {"kind": "upgrade", "member_no": member_no, "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                   "expected_expiry_local": None, "new_expiry_local": NEW_EXPIRY, "salt": "s"}
        self.ledger.add_intent(run_id="run-x", order_gid=order_gid(1), payload=payload, deadline=self.now)  # the parent died in the save
        out = self.engine().run(apply=False)
        self.assertEqual((out["counts"].get("write_already_recorded"), self.book.reads, self.receipt(1)["observed"]), (1, 0, None))


class RealOutputsReachTheCheckTests(Base):
    """Slice B1 T6 (B12, H07): the digest alert codes and refs, and the
    ``stopped`` text of an index run that stops, reach the check after every
    test with real data."""

    run_main = CliTests.run_main

    def test_b12_a_real_digest_with_an_alert_and_the_ref_filter(self):
        twenty = tuple(("20.00", 1, 1, False, ()) for _ in range(20))
        self.guest_with_order(1, lines=twenty, more_lines=True)  # a 21st line item was not returned
        self.assertEqual(self.run_main("init")[0], 0)
        code, first, _ = self.run_main("observe")
        self.assertEqual((code, first["alerts"]), (3, ["order_amount_unverifiable"]))
        ref = first["refs"][0]["ref"]
        self.assertEqual(first["refs"], [{"ref": ref, "code": "order_amount_unverifiable"}])
        # H07: a second run whose ref's code is not one of that run's alerts.
        self.shopify.orders_nodes.clear()
        self.guest_with_order(2)
        self.advance(minutes=15)
        code, second, _ = self.run_main("observe")
        self.assertEqual((code, second["alerts"], [r["code"] for r in second["refs"]]), (0, [], ["would_upgrade"]))
        self.assertNotEqual(second["refs"][0]["ref"], ref)
        code, digest, _ = self.run_main("digest", "--hours", "24")
        self.assertEqual(code, 0)
        self.assertEqual(digest["alerts"], {"order_amount_unverifiable": 1})
        self.assertEqual(digest["alert_refs"], [{"ref": ref, "code": "order_amount_unverifiable"}])
        self.assertEqual((digest["counts"]["order_amount_unverifiable"], digest["counts"]["would_upgrade"]), (1, 1))

    def test_b12_an_index_run_that_stops_on_a_failed_read(self):
        self.book.members[sg(1)] = fake_member(sg(1), name="Fake Member 1")  # an unlinked GUEST whose MemberNo is a phone
        self.shopify.customers[customer_gid(1)] = customer_node(1)
        self.book.fail_read = "bridge_read_failed:member_no_invalid"
        self.assertEqual(self.run_main("init")[0], 0)
        code, out, _ = self.run_main("index", "--max", "5", "--apply")
        self.assertEqual(code, 3)
        self.assertEqual((out["command"], out["stopped"], out["alerts"], out["counts"]),
                         ("index_apply", "bridge_read_failed:member_no_invalid", ["bridge_read_failed:member_no_invalid"],
                          {"bridge_read_failed:member_no_invalid": 1, "examined": 0, "index_member_complete": 1}))  # MEMBER pages first, none
        conn = sqlite3.connect((self.root / bm.LEDGER_FILE).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            stored = json.loads(conn.execute("SELECT summary FROM runs WHERE run_id = ?", (out["run_id"],)).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(stored, out)


class B2TestGapTests(Base):
    """Slice B2, tests only (G0 B2-12, B2-13): gaps left open by earlier
    rounds, each named after the mutation it now catches."""

    run_main = CliTests.run_main
    real_client = OrdersQueryCostTests.real_client
    factory = OrdersQueryCostTests.factory

    # -- B2-12 (PR #303 L6): a real WAL v1 ledger with a reader open ----------
    def test_b2_12_a_wal_v1_ledger_with_a_reader_open_is_refused_and_its_main_file_left_as_it_is(self):
        make_v1_ledger(self.root)
        path = self.root / bm.LEDGER_FILE
        reader = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        reader.execute("INSERT INTO watermarks(name, value, updated_at) VALUES ('w', 'v', 't')")  # a page in the -wal file
        reader.execute("BEGIN")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM watermarks").fetchone()[0], 1)  # a read transaction held open
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        for args in self.COMMANDS:
            with self.subTest(args=args):
                self.assertEqual(self.run_main(*args)[:2], (2, {"status": "refused", "code": "benefits_ledger_not_recognised"}))
        self.assertEqual(self.code(lambda: bm.BenefitsLedger(self.root)), "benefits_ledger_not_recognised")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertTrue((self.root / (bm.LEDGER_FILE + "-wal")).exists())  # the side files stay (runbook)
        self.assertEqual(reader.execute("SELECT value FROM watermarks WHERE name = 'w'").fetchone()[0], "v")

    COMMANDS = (("init",), ("observe",), ("status",), ("digest",), ("claim-links",))

    # -- B2-13 A21, A23: the line and shipping pages ---------------------------
    def test_b2_13_a21_a_line_item_page_without_has_next_page_is_not_complete(self):
        for info, complete in (({"hasNextPage": False}, True), ({}, False), ({"hasNextPage": None}, False), ({"hasNextPage": True}, False)):
            with self.subTest(info=info):
                node = order_node(1)
                node["lineItems"]["pageInfo"] = info
                order = bm.order_from_node(node)
                self.assertIs(order.lines_complete, complete)
                self.assertEqual(bm.goods_amount(order, self.cfg()) is not None, complete)

    def test_b2_13_a23_a_shipping_list_not_shown_complete_is_not_measured_when_shipping_counts(self):
        for info, exclude, measured in (({"hasNextPage": True}, True, True), ({"hasNextPage": True}, False, False), ({}, False, False),
                                        ({"hasNextPage": False}, False, True)):
            with self.subTest(info=info, exclude=exclude):
                node = order_node(1)
                node["shippingLines"]["pageInfo"] = info
                order = bm.order_from_node(node)
                self.assertIs(order.shipping_complete, info.get("hasNextPage") is False)
                self.assertEqual(bm.goods_amount(order, self.cfg(amount_exclude_shipping=exclude)) is not None, measured)
        # In a run: the order is held for a person, never measured without its shipping.
        self.shopify.add({**order_node(1), "shippingLines": {"pageInfo": {"hasNextPage": True}, "nodes": [{"originalPriceSet": money("15.00")}]}})
        out = self.engine(amount_exclude_shipping=False).run(apply=False)
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"], out["alerts"]),
                         ("amount_unverifiable", "currency_or_lines", ["order_amount_unverifiable"]))

    # -- B2-13 A33, A34, A36, A37: the query cost ------------------------------
    def stored_summary(self, run_id: str) -> dict:
        conn = sqlite3.connect((self.root / bm.LEDGER_FILE).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return json.loads(conn.execute("SELECT summary FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0])
        finally:
            conn.close()

    def test_b2_13_a33_a34_a36_a37_the_cost_of_a_capped_and_a_resumed_read_is_recorded_never_counted_or_added_up(self):
        self.guest_with_order(1)
        self.guest_with_order(2, updated="2026-11-18T03:05:00Z")
        first, second = (self.shopify.orders_nodes[order_gid(n)] for n in (1, 2))
        self.run_main("init")
        session = ShopifyClientTests.session(["read_orders", "read_customers"])
        client, _ = self.real_client([session, orders_page([first], more="cursor-a", cost=shopify_cost(846.5))])
        code, capped, _ = self.run_main("observe", cfg={"shopify_max_pages": 1}, factory=self.factory(client))
        self.assertEqual((code, capped["alerts"], capped["orders_query_cost"]), (3, ["orders_read_incomplete"], 847))  # A33
        self.advance(minutes=15)
        client, transport = self.real_client([session, orders_page([second], cost=shopify_cost(500))])
        code, resumed, _ = self.run_main("observe", cfg={"shopify_max_pages": 1}, factory=self.factory(client))
        self.assertEqual(transport.bodies[1]["variables"]["after"], "cursor-a")
        self.assertEqual((code, resumed["counts"].get("orders_read_resumed"), resumed["orders_query_cost"]), (0, 1, 500))  # A34
        for out in (capped, resumed):
            stored = self.stored_summary(out["run_id"])
            self.assertEqual(stored["orders_query_cost"], out["orders_query_cost"])
            for value in (out, stored):  # A36: never a count
                self.assertEqual([key for key in value["counts"] if "cost" in key], [])
                self.assertNotIn(out["orders_query_cost"], [n for k, n in value["counts"].items() if k not in ("orders_read",)])
        digest = self.run_main("digest", "--hours", "24")[1]
        self.assertEqual([key for key in digest["counts"] if "cost" in key], [])  # A37: never added up
        self.assertNotIn(1347, digest["counts"].values())
        self.assertEqual(digest["runs"], {"completed": 2, "unfinished": 0})

    # -- B2-13 G32: the digest's masked-MemberNo filter -------------------------
    def test_b2_13_g32_the_digest_keeps_a_masked_member_number_only(self):
        other = self.temp / "g32"
        other.mkdir()
        ledger = bm.BenefitsLedger(other, clock=lambda: self.now)
        try:
            ledger.init_meta(self.cfg())
            refs = [{"ref": "o-" + "a" * 20, "code": "write_held", "member_no_masked": sg(1)},
                    {"ref": "o-" + "b" * 20, "code": "write_held", "member_no_masked": "*******001"},
                    {"ref": "o-" + "c" * 20, "code": "write_held", "member_no_masked": "**1234"},
                    {"ref": "o-" + "d" * 20, "code": "write_held", "member_no_masked": "*" * 10}]
            summary = {"counts": {"write_held": 4}, "alerts": ["write_held"], "refs": refs}
            with ledger.tx() as conn:  # a stored summary injected through the ledger (the tool itself always masks)
                conn.execute("INSERT INTO runs(run_id, command, started_at, finished_at, summary) VALUES ('run-g32', 'apply', ?, ?, ?)",
                             (dl.ts(self.now), dl.ts(self.now), json.dumps(summary)))
        finally:
            ledger.close()
        digest = bm.ledger_digest(other, hours=24, now=self.now, config=self.cfg())
        self.assertEqual(digest["alert_refs"], [{"ref": "o-" + "a" * 20, "code": "write_held"},
                                                {"ref": "o-" + "b" * 20, "code": "write_held", "member_no_masked": "*******001"},
                                                {"ref": "o-" + "c" * 20, "code": "write_held"},
                                                {"ref": "o-" + "d" * 20, "code": "write_held"}])
        self.assertNotIn(sg(1), json.dumps(digest))

    # -- B2-13 D01: the REFUNDED branch ----------------------------------------
    def test_b2_13_d01_a_refunded_status_decides_when_the_money_check_alone_would_not(self):
        lines = (("300.00", 1, 0, False, ()),)  # the one unit refunded: the money is explained by it
        for status, unattributed in (("REFUNDED", True), ("PARTIALLY_REFUNDED", False), ("PAID", False)):
            with self.subTest(status=status):
                order = bm.order_from_node(order_node(1, lines=lines, status=status, refunded="300.00"))
                self.assertEqual(order.refunded_cents, bm.refunded_unit_cents(order))
                self.assertIs(bm.money_refund_unattributed(order), unattributed)
        # An applied receipt read again as REFUNDED with no money left unexplained still alerts.
        self.guest_with_order(1)
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(1)["state"], "applied")
        self.shopify.add(order_node(1, status="REFUNDED", updated="2026-11-18T03:30:00Z"))
        self.advance(minutes=15)
        out = self.engine().run(apply=False)
        self.assertEqual(out["alerts"], ["refund_on_basis"])

    # -- B2-13 D32: a malformed stored resume cursor ----------------------------
    def test_b2_13_d32_a_malformed_stored_resume_cursor_is_cleared_and_the_read_restarts_from_the_watermark(self):
        self.guest_with_order(1)
        engine = self.engine()
        good_since = "2026-11-18T01:00:00Z"
        for value in ("not json", "[1]", json.dumps({"since": "x", "cursor": "cursor-0"}), json.dumps({"since": good_since, "cursor": ""}),
                      json.dumps({"since": good_since, "cursor": 5}), json.dumps({"since": good_since}), json.dumps({"cursor": "cursor-0"})):
            with self.subTest(value=value):
                self.ledger.set_watermark(bm.WATERMARK, "2026-11-18T02:00:00Z")
                self.ledger.set_watermark(bm.RESUME_MARK, value)
                self.shopify.calls.clear()
                self.advance(minutes=15)
                out = engine.run(apply=False)
                self.assertNotIn("orders_read_resumed", out["counts"])
                self.assertEqual([c for c in self.shopify.calls if c[0] == "orders"],
                                 [("orders", datetime(2026, 11, 18, 1, 45, tzinfo=timezone.utc))])  # the watermark less the margin
                self.assertIsNone(self.ledger.watermark(bm.RESUME_MARK))


class B2AlertTests(Base):
    """Slice B2 (G0 B2-1, B2-2): an order seen with another customer after
    its upgrade, and a claim hold settled for a reason that is not a
    positive fact. Each alerts once per order; nothing else changes."""

    claim_held = G2FixRound2Tests.claim_held
    link_and_run = G2FixRound2Tests.link_and_run
    MOVED = "order_customer_changed_after_upgrade"
    UNCLEAR = "claim_hold_settled_unclear"

    def upgraded(self, n: int = 1) -> str:
        member_no = self.guest_with_order(n)
        self.assertEqual(self.engine().run(apply=True)["saves"], 1)
        self.assertEqual(self.receipt(n)["state"], "applied")
        return member_no

    def replay(self, n: int = 1, minute: int = 10, **order) -> dict:
        self.shopify.add(order_node(n, updated=f"2026-11-18T04:{minute:02d}:00Z", **order))
        self.advance(minutes=15)
        return self.engine().run(apply=False)

    # -- B2-1 -----------------------------------------------------------------
    def test_b2_1_an_order_moved_after_its_upgrade_alerts_once_and_the_receipt_keeps_its_customer(self):
        member_no = self.upgraded()
        before = tuple(self.receipt(1))
        out = self.replay(customer=customer_node(2))
        ref = self.ledger.ref("o", order_gid(1))
        self.assertEqual(out["alerts"], [self.MOVED])
        self.assertIn({"ref": ref, "code": self.MOVED, "member_no_masked": bm.mask(member_no)}, out["refs"])
        row = self.receipt(1)
        self.assertEqual((row["state"], row["customer_gid"], row["member_no"], row["code"], row["basis_expiry_local"]),
                         ("applied", customer_gid(1), member_no, "upgrade", NEW_EXPIRY))
        self.assertEqual({k: row[k] for k in row.keys() if k not in ("order_updated_at", "updated_at", "payment_gateways")},
                         {k: dict(zip(row.keys(), before))[k] for k in row.keys() if k not in ("order_updated_at", "updated_at", "payment_gateways")})
        self.assertIn(("moved:" + order_gid(1), self.MOVED), [tuple(r)[:2] for r in self.ledger.conn.execute("SELECT * FROM reviews")])
        # Seen again, moved back, moved on: never a second alert.
        for minute, customer in ((20, customer_node(2)), (30, customer_node(1)), (40, customer_node(3)), (50, None)):
            with self.subTest(minute=minute):
                out = self.replay(minute=minute, customer=customer)
                self.assertEqual((out["alerts"], out["counts"].get(self.MOVED)), ([], None))
        self.assertEqual((self.book.saves, self.receipt(1)["customer_gid"]), (1, customer_gid(1)))

    def test_b2_1_a_removed_customer_alerts_and_a_refund_on_the_same_read_alerts_too(self):
        member_no = self.guest_with_order(1)
        self.guest_with_order(2)
        self.assertEqual(self.engine().run(apply=True)["saves"], 2)
        out = self.replay(customer=None)
        self.assertEqual(out["alerts"], [self.MOVED])
        out = self.replay(2, minute=20, customer=customer_node(5), status="REFUNDED")
        self.assertEqual(sorted(out["alerts"]), sorted([self.MOVED, "refund_on_basis"]))
        self.assertEqual(self.receipt(2)["alerted"], "refund_on_basis")
        self.assertEqual(self.book.members[member_no]["type"], "MEMBER")  # never downgraded

    def test_b2_1_a_receipt_that_is_not_applied_never_alerts_for_a_move(self):
        self.guest_with_order(1, member=fake_member(sg(1), name="Fake Member 1", active="F"))  # held: member_inactive
        self.guest_with_order(2, member=fake_member(sg(2), name="Fake Member 2", member_type="MEMBER", expiry="2030-01-01T00:00:00.000"))
        self.guest_with_order(3, member=fake_member(sg(3), name="Fake Member 3", member_type="VIP"))
        self.guest_with_order(4)
        self.engine().run(apply=False)  # 4 stays ready (observed)
        self.assertEqual([self.receipt(n)["state"] for n in (1, 2, 3, 4)], ["held", "no_change", "not_in_scope", "ready"])
        for n in (1, 2, 3, 4):
            self.shopify.add(order_node(n, customer=customer_node(n + 10), updated="2026-11-18T04:10:00Z"))
        self.advance(minutes=15)
        out = self.engine().run(apply=False)
        self.assertNotIn(self.MOVED, out["alerts"])
        self.assertIsNone(out["counts"].get(self.MOVED))

    # -- B2-2: the replay path -------------------------------------------------
    def settle(self, **order) -> dict:
        self.claim_held()
        self.advance(days=1)
        self.shopify.add(order_node(1, updated="2026-11-19T03:00:00Z", **order))
        out = self.engine().run(apply=True)
        self.assertEqual((out["counts"].get("claim_hold_settled"), self.receipt(1)["state"], self.receipt(1)["code"]),
                         (1, "held", "order_no_longer_qualifies"))
        return out

    def test_b2_2_a_positive_fact_settles_a_claim_hold_silently(self):
        cases = {"cancelled": dict(cancelled="2026-11-19T02:00:00Z"),
                 "unit_refund": dict(lines=(("300.00", 1, 0, False, ()),), status="PARTIALLY_REFUNDED", refunded="300.00"),
                 "unit_refund_below": dict(lines=(("300.00", 1, 1, False, ()), ("50.00", 1, 1, False, ())), refunded="0.00", transactions=(tx("60.00"),)),
                 "moved": dict(customer=customer_node(2)), "no_customer": dict(customer=None)}
        for name, order in cases.items():
            with self.subTest(name=name):
                self.setUp()
                try:
                    out = self.settle(**order)
                    self.assertNotIn(self.UNCLEAR, out["alerts"])
                    self.assertIsNone(out["counts"].get(self.UNCLEAR))
                finally:
                    self.doCleanups()

    def test_b2_2_any_other_reason_settles_and_alerts_once(self):
        twenty = tuple(("20.00", 1, 1, False, ()) for _ in range(20))
        cases = {"unpaid": dict(status="PENDING"), "partly_paid": dict(status="PARTIALLY_PAID"),
                 "signup_marker_gone": dict(customer=customer_node(1, tags=())),
                 "a_21st_line": dict(lines=twenty, more_lines=True),
                 "a_10th_transaction": dict(transactions=tuple(card("1.00") for _ in range(10))),
                 "test_order": dict(test=True)}
        for name, order in cases.items():
            with self.subTest(name=name):
                self.setUp()
                try:
                    out = self.settle(**order)
                    ref = self.ledger.ref("o", order_gid(1))
                    self.assertEqual((out["counts"].get(self.UNCLEAR), out["saves"]), (1, 0))
                    self.assertIn(self.UNCLEAR, out["alerts"])
                    self.assertIn({"ref": ref, "code": self.UNCLEAR}, out["refs"])
                    self.assertIn(("settled:" + order_gid(1), self.UNCLEAR), [tuple(r)[:2] for r in self.ledger.conn.execute("SELECT * FROM reviews")])
                    # Once: seen again (and linked), the settled hold stays shut and quiet.
                    self.advance(minutes=15)
                    again = self.link_and_run()
                    self.assertEqual((again["counts"].get(self.UNCLEAR), self.receipt(1)["code"], self.book.saves),
                                     (None, "order_no_longer_qualifies", 0))
                finally:
                    self.doCleanups()

    # -- B2-2: the stored-facts path (_reopen_claim_holds) ----------------------
    def test_b2_2_the_stored_facts_path_alerts_unless_a_paid_amount_is_below_the_threshold(self):
        facts = {1: (dict(financial_status="REFUNDED"), True), 2: (dict(net_cents=0), False), 3: (dict(financial_status="PENDING"), True),
                 4: (dict(net_cents=None), True), 5: (dict(financial_status=None), True), 6: (dict(net_cents=29_999, financial_status="PARTIALLY_REFUNDED"), False),
                 7: (dict(net_cents=0, financial_status="REFUNDED"), False), 8: (dict(net_cents=0, financial_status="PENDING"), True)}
        for n in facts:
            self.guest_with_order(n, linked=False, member=fake_member(sg(n), name="Someone Else Entirely"))
        self.engine().run(apply=True)
        self.advance(hours=2)
        self.shopify.add(order_node(9, customer=None, lines=(("10.00", 1, 1, False, ()),), created="2026-11-18T05:30:00Z"))
        self.engine().run(apply=True)  # a newer order moves the watermark on: the six are not read again
        for n, (change, _) in facts.items():
            self.assertEqual(self.receipt(n)["code"], "claim_name_mismatch")
            self.ledger.update_receipt(order_gid(n), **change)
            self.link(n)
        self.advance(hours=1)
        out = self.engine().run(apply=True)
        alerted = {r["ref"] for r in out["refs"] if r["code"] == self.UNCLEAR}
        self.assertEqual(alerted, {self.ledger.ref("o", order_gid(n)) for n, (_, alert) in facts.items() if alert})
        self.assertEqual((out["counts"].get("claim_hold_settled"), out["counts"].get(self.UNCLEAR), out["saves"]), (8, 5, 0))
        self.advance(hours=1)
        self.assertIsNone(self.engine().run(apply=True)["counts"].get(self.UNCLEAR))  # once


class B2WindowTests(Base):
    """Slice B2 (G0 B2-3, B2-4): no re-open after a read that ended at the
    page cap; the 60-day age limit (exactly 60 days settles: ``>=``)."""

    claim_held = G2FixRound2Tests.claim_held
    AGED = "receipt_past_read_window"
    CREATED = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)  # ORDER_AT

    # -- B2-3 ----------------------------------------------------------------
    def test_b2_3_a_capped_read_defers_the_re_open_and_the_next_complete_read_re_opens(self):
        self.claim_held()
        self.link(1)
        for n in range(20, 26):  # six newer orders: more than one page of five
            self.shopify.add(order_node(n, customer=None, lines=(("10.00", 1, 1, False, ()),), created="2026-11-18T04:0%d:00Z" % (n - 20)))
        self.advance(minutes=15)
        out = self.engine(shopify_max_pages=1).run(apply=True)
        self.assertIn("orders_read_incomplete", out["alerts"])
        self.assertEqual((out["counts"].get("claim_reopen_deferred"), out["counts"].get("claim_hold_reopened"), out["saves"]), (1, None, 0))
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "claim_name_mismatch"))
        self.advance(minutes=15)
        out = self.engine(shopify_max_pages=1).run(apply=True)
        self.assertNotIn("orders_read_incomplete", out["alerts"])
        self.assertEqual((out["counts"].get("claim_reopen_deferred"), out["counts"].get("claim_hold_reopened")), (None, 1))
        self.assertEqual(self.receipt(1)["state"], "applied")

    # -- B2-4 ----------------------------------------------------------------
    def at(self, delta: timedelta) -> dict:
        self.now = self.CREATED + delta
        return self.engine().run(apply=True)

    def test_b2_4_the_age_limit_comes_before_the_re_open_in_the_same_run(self):
        self.claim_held()  # order 1, created at ORDER_AT
        self.guest_with_order(2, linked=False, member=fake_member(sg(2), name="Someone Else Entirely"), created="2026-11-18T03:01:00Z")
        self.engine().run(apply=True)
        self.assertEqual([self.receipt(n)["code"] for n in (1, 2)], ["claim_name_mismatch"] * 2)
        self.link(1)
        self.link(2)
        out = self.at(timedelta(days=60))  # order 1 exactly 60 days old, order 2 one minute younger
        self.assertEqual((out["counts"].get(self.AGED), out["counts"].get("claim_hold_reopened"), out["saves"]), (1, 1, 1))
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in (1, 2)],
                         [("held", "order_past_read_window"), ("applied", "upgrade")])
        self.assertEqual((self.book.members[sg(1)]["type"], self.book.members[sg(2)]["type"]), ("GUEST", "MEMBER"))

    def test_b2_4_the_limit_is_exactly_60_days_for_both_receipt_kinds(self):
        self.claim_held()
        self.shopify.add(order_node(2))  # signed up, no member yet: waits
        self.engine().run(apply=True)
        self.assertEqual((self.receipt(2)["state"], self.receipt(1)["state"]), ("waiting_member", "held"))
        out = self.at(timedelta(days=60) - timedelta(minutes=1))
        self.assertNotIn(self.AGED, out["alerts"])
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"]) for n in (1, 2)],
                         [("held", "claim_name_mismatch"), ("waiting_member", None)])
        out = self.at(timedelta(days=60))  # exactly 60 days: settles
        self.assertEqual((out["alerts"].count(self.AGED), out["counts"].get(self.AGED)), (1, 2))
        self.assertEqual({r["ref"] for r in out["refs"] if r["code"] == self.AGED}, {self.ledger.ref("o", order_gid(n)) for n in (1, 2)})
        reviews = [tuple(r)[:2] for r in self.ledger.conn.execute("SELECT * FROM reviews")]
        self.assertTrue(all(("aged:" + order_gid(n), self.AGED) in reviews for n in (1, 2)))
        self.assertEqual([(self.receipt(n)["state"], self.receipt(n)["code"], self.receipt(n)["waiting_since"]) for n in (1, 2)],
                         [("held", "order_past_read_window", None), ("held", "order_past_read_window", None)])
        # Once, and never re-opened or waited on: a link appears and a member is made.
        self.link(1)
        self.link(2)
        self.book.members[sg(2)] = fake_member(sg(2), name="Fake Member 2")
        out = self.at(timedelta(days=61))
        self.assertEqual((out["counts"].get(self.AGED), out["counts"].get("claim_hold_reopened"), out["saves"]), (None, None, 0))
        self.assertEqual([self.receipt(n)["code"] for n in (1, 2)], ["order_past_read_window"] * 2)
        self.assertEqual(self.book.saves, 0)

    def test_b2_4_fix1_the_age_limit_also_runs_after_a_capped_read(self):
        """G2 F2 (P5): a 61-day-old ``waiting_member`` receipt whose member then
        appears is settled, never upgraded, in a run whose read hit the page cap."""
        self.shopify.add(order_node(2))  # signed up, no member yet: waits
        self.engine().run(apply=True)
        self.assertEqual(self.receipt(2)["state"], "waiting_member")
        self.book.members[sg(2)] = fake_member(sg(2), name="Fake Member 2")  # the member appears
        self.link(2)
        for days, alerted in ((61, 1), (62, 0)):
            with self.subTest(days=days):
                for n in range(days * 10, days * 10 + 6):  # six more: this read is capped too
                    self.shopify.add(order_node(n, customer=None, lines=(("10.00", 1, 1, False, ()),), created="2026-11-18T0%d:0%d:00Z" % (days - 56, n % 10)))
                self.now = self.CREATED + timedelta(days=days)
                out = self.engine(shopify_max_pages=1).run(apply=True)
                self.assertIn("orders_read_incomplete", out["alerts"])
                self.assertEqual((out["alerts"].count(self.AGED), out["counts"].get(self.AGED), out["saves"], self.book.saves),
                                 (alerted, alerted or None, 0, 0))
                self.assertEqual((self.receipt(2)["state"], self.receipt(2)["code"]), ("held", "order_past_read_window"))
        self.assertEqual(self.book.members[sg(2)]["type"], "GUEST")

    def test_b2_4_applied_and_other_receipts_are_never_touched(self):
        self.guest_with_order(1)
        self.guest_with_order(2, member=fake_member(sg(2), name="Fake Member 2", active="F"))
        self.engine().run(apply=True)
        self.shopify.add(order_node(3, status="PENDING"))
        self.engine().run(apply=True)
        before = {n: tuple(self.receipt(n)) for n in (1, 2, 3)}
        self.assertEqual([r[0] for r in [(self.receipt(n)["state"],) for n in (1, 2, 3)]], ["applied", "held", "ignored_unpaid"])
        out = self.at(timedelta(days=90))
        self.assertNotIn(self.AGED, out["alerts"])
        self.assertEqual({n: tuple(self.receipt(n))[:-1] for n in before}, {n: row[:-1] for n, row in before.items()})


class B2RecordTests(Base):
    """Slice B2 (G0 B2-5, B2-7): carry-over when an undecided receipt
    follows its order; the goods measured first."""

    RESET = ("waiting_since", "alerted", "observed", "observed_at", "checked_at", "member_no", "link_source")

    # -- B2-5 ----------------------------------------------------------------
    def test_b2_5_an_undecided_receipt_that_follows_its_order_keeps_nothing_of_the_old_customer(self):
        self.guest_with_order(1)
        engine = self.engine()
        engine.run(apply=False)
        self.ledger.update_receipt(order_gid(1), waiting_since="2026-11-18T03:30:00Z", alerted="qualified_member_missing", checked_at="2026-11-18T03:40:00Z")
        row = self.receipt(1)
        self.assertEqual((row["state"], row["observed"], row["link_source"]), ("ready", "would_upgrade", "sync155"))
        self.assertTrue(all(row[name] is not None for name in self.RESET))
        # The same customer again: everything kept.
        engine.record(bm.order_from_node(order_node(1, updated="2026-11-18T04:05:00Z")), bm.RunSummary("observe"))
        self.assertEqual({name: self.receipt(1)[name] for name in self.RESET}, {name: row[name] for name in self.RESET})
        for n, customer in ((2, customer_node(2)), (None, None)):
            with self.subTest(customer=n):
                self.ledger.update_receipt(order_gid(1), **{name: row[name] for name in self.RESET})
                engine.record(bm.order_from_node(order_node(1, customer=customer, updated="2026-11-18T04:06:00Z")), bm.RunSummary("observe"))
                moved = self.receipt(1)
                self.assertEqual(moved["customer_gid"], customer_gid(n) if n else None)
                for name in self.RESET:
                    self.assertIsNone(moved[name], name)
        # In a run: the receipt is linked afresh through the new customer's own link.
        self.link(2)
        self.book.members[sg(2)] = fake_member(sg(2), name="Fake Member 2")
        self.shopify.add(order_node(1, customer=customer_node(2), updated="2026-11-18T04:20:00Z"))
        self.advance(minutes=30)
        out = self.engine().run(apply=False)
        self.assertIn({"ref": self.ledger.ref("o", order_gid(1)), "code": "would_upgrade", "member_no_masked": bm.mask(sg(2))}, out["refs"])
        self.assertEqual((self.receipt(1)["member_no"], self.receipt(1)["customer_gid"]), (sg(2), customer_gid(2)))

    # -- B2 fix round 1 (G2 F1) ----------------------------------------------
    run_main = CliTests.run_main
    WRITE_KEEPS = {"INTENT": True, "UNCERTAIN": True, "HELD": True, "DONE": True, "SKIPPED": False, "NOT_WRITTEN": False, None: False}

    def cli_receipt(self, n: int = 1) -> tuple:
        return tuple(stored_receipt(self.root, n, name) for name in ("state", "code", "customer_gid", "member_no", "link_source", "basis_expiry_local"))

    def test_b2_5_fix1_an_open_or_landed_write_keeps_member_no_and_link_source(self):
        cases = dict(enumerate(self.WRITE_KEEPS, start=1))  # order n: its write state (None: no write)
        members = {n: self.guest_with_order(n) for n in cases}
        engine = self.engine()
        engine.run(apply=False)
        for n, state in cases.items():
            self.assertEqual((self.receipt(n)["state"], self.receipt(n)["member_no"], self.receipt(n)["link_source"]), ("ready", members[n], "sync155"))
            if state is not None:
                payload = {"kind": "upgrade", "member_no": members[n], "from_type": "GUEST", "to_type": "MEMBER", "expected_last_modified_local": LM,
                           "expected_expiry_local": None, "new_expiry_local": NEW_EXPIRY, "salt": "s"}
                write = self.ledger.add_intent(run_id="run-x", order_gid=order_gid(n), payload=payload, deadline=self.now)
                if state != "INTENT":
                    self.ledger.set_outcome(write["write_id"], state, "c", LM if state == "DONE" else None)
        for n, state in cases.items():
            with self.subTest(write=state):
                engine.record(bm.order_from_node(order_node(n, customer=customer_node(n + 20), updated="2026-11-18T04:06:00Z")), bm.RunSummary("observe"))
                row = self.receipt(n)
                if state == "DONE":  # the landed save is repaired first (G2 LATER 1): applied, nothing moves
                    self.assertEqual((row["state"], row["customer_gid"], row["member_no"]), ("applied", customer_gid(n), members[n]))
                    continue
                self.assertEqual((row["state"], row["customer_gid"]), ("ready", customer_gid(n + 20)))
                kept = (members[n], "sync155") if self.WRITE_KEEPS[state] else (None, None)
                self.assertEqual((row["member_no"], row["link_source"]), kept)
                for name in self.RESET[:5]:  # the rest of the carry-over is reset either way
                    self.assertIsNone(row[name], name)

    def test_b2_5_fix1_resolve_after_an_observe_that_moved_the_order_settles_done(self):
        """G2 F1 (P1/P2): an UNCERTAIN save that landed; an observe replays the
        order with another customer; ``resolve`` reads the save back as DONE
        and sets the receipt applied: coded, exit 0, as on main."""
        member_no = self.guest_with_order(1)
        self.run_main("init")
        self.book.mode = "uncertain_saved"
        self.assertEqual(self.run_main("apply", cfg={"benefits_write_enabled": True})[1]["alerts"], ["write_uncertain"])
        self.book.mode = None
        self.shopify.add(order_node(1, customer=customer_node(2), updated="2026-11-18T04:10:00Z"))
        self.advance(minutes=15)
        self.assertEqual(self.run_main("observe")[0], 0)
        self.assertEqual(self.cli_receipt(), ("ready", None, customer_gid(2), member_no, "sync155", None))
        self.advance(seconds=PAST_DEADLINE)
        code, out, _ = self.run_main("resolve")
        self.assertEqual((code, out["status"], out["resolved"], out["alerts"]), (0, "ok", {"DONE:resolved_saved": 1}, []))
        self.assertEqual(self.cli_receipt(), ("applied", "upgrade", customer_gid(2), member_no, "sync155", NEW_EXPIRY))
        self.advance(minutes=15)
        out = self.run_main("apply", cfg={"benefits_write_enabled": True})[1]
        self.assertEqual((out["counts"].get("receipt_repaired_applied"), out["saves"], self.book.saves), (None, 0, 1))

    def test_b2_5_fix1_a_held_write_keeps_the_member_through_a_move_and_release_applies(self):
        ref, member_no = ReleaseHeldTests.held(self)
        self.shopify.add(order_node(1, customer=customer_node(2), updated="2026-11-18T04:10:00Z"))
        self.advance(minutes=15)
        self.engine().run(apply=False)
        row = self.receipt(1)
        self.assertEqual((row["state"], row["customer_gid"], row["member_no"], row["link_source"]), ("ready", customer_gid(2), member_no, "sync155"))
        self.assertEqual(ReleaseHeldTests.release(self, ref, member_no[-3:], "as_intended")["receipt_now"], "applied")
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["member_no"]), ("applied", member_no))

    # -- B2-7 ----------------------------------------------------------------
    def test_b2_7_goods_below_the_threshold_with_unread_payments_are_below_threshold_with_no_alert(self):
        twenty = tuple(("10.00", 1, 1, False, ()) for _ in range(20))
        cases = {
            1: (dict(lines=(("299.99", 1, 1, False, ()),), tx_count=None), "below_threshold", None, False),
            2: (dict(lines=(("300.00", 1, 1, False, ()),), tx_count=None), "amount_unverifiable", "payments_unreadable", True),
            3: (dict(lines=(("450.00", 1, 1, False, ()),), transactions=tuple(card("1.00") for _ in range(10))),
                "amount_unverifiable", "payments_unreadable", True),
            4: (dict(lines=twenty, more_lines=True, tx_count=None), "amount_unverifiable", "currency_or_lines", True),  # goods unreadable
            5: (dict(lines=(("400.00", 1, 0, False, ()),), status="PARTIALLY_REFUNDED", refunded="400.00", tx_count=None), "below_threshold", None, False),
        }
        for n, (order, *_rest) in cases.items():
            self.guest_with_order(n, **order)
        out = self.engine().run(apply=False)
        for n, (_, state, code, alert) in cases.items():
            with self.subTest(n=n):
                self.assertEqual((self.receipt(n)["state"], self.receipt(n)["code"]), (state, code))
                self.assertEqual({"ref": self.ledger.ref("o", order_gid(n)), "code": "order_amount_unverifiable"} in out["refs"], alert)
        self.assertEqual((out["counts"].get("below_threshold_payments_unread"), out["counts"].get("order_amount_unverifiable")), (2, 3))
        self.assertEqual(self.book.reads, 0)


class B2DigestProbeTests(Base):
    """Slice B2 (G0 B2-6, B2-8): digest gateway names with a run of digits;
    reprs; a writer probe of ``unknown`` counts as possibly live."""

    release = ReleaseHeldTests.release
    held = ReleaseHeldTests.held
    NOT_EXITED = ("running", "unknown", "", "RUNNING", "exited ", "Exited")

    # -- B2-6 ----------------------------------------------------------------
    def test_b2_6_the_digest_shows_a_gateway_name_with_four_digits_in_a_row_as_other(self):
        names = {1: "gift_card_1234", 2: "gift_card_123", 3: "x9999y", 4: "gift_card", 5: "pay_12_34_56"}
        for n, gateway in names.items():
            self.guest_with_order(n, transactions=(tx("1.00", gateway=gateway),))
        self.engine().run(apply=False)
        seen = bm.ledger_digest(self.root, hours=24, now=self.now, config=self.cfg())["gift_cards"]["gateways_seen"]
        self.assertEqual(seen, {"other": 2, "gift_card_123": 1, "gift_card": 1, "pay_12_34_56": 1})

    def test_b2_6_payment_and_order_line_reprs_print_no_field(self):
        order = bm.order_from_node(order_node(1, lines=(("123.45", 7, 6, True, ("9.87",)),), transactions=(tx("54.32", gateway="gift_card_7777"),)))
        payment, line = order.payments[0], order.lines[0]
        self.assertEqual((repr(payment), repr(line), str(payment), str(line)), ("Payment()", "OrderLine()", "Payment()", "OrderLine()"))
        for text in (repr(order.payments), repr(order.lines), repr(order), repr(order.customer)):
            for value in ("gift_card", "7777", "5432", "12345", "987", "SUCCESS", "SALE", "True"):
                self.assertNotIn(value, text)

    # -- B2-8 ----------------------------------------------------------------
    def test_b2_8_resolve_holds_unless_the_probe_says_exited(self):
        self.guest_with_order(1)
        self.book.mode = "uncertain_unsaved"
        engine = self.engine()
        engine.run(apply=True)
        self.advance(seconds=PAST_DEADLINE)
        for result in self.NOT_EXITED:
            with self.subTest(result=result):
                self.probe_result = result
                self.assertEqual(self.code(lambda: engine.resolve()), "writer_may_be_live")
                self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "UNCERTAIN")
        self.probe_result = "exited"
        self.assertEqual(engine.resolve()["resolved"], {"NOT_WRITTEN:resolved_not_written": 1})

    def test_b2_8_release_held_holds_unless_the_probe_says_exited(self):
        ref, member_no = self.held()
        before = ReleaseHeldTests.recorded(self)
        for result in self.NOT_EXITED:
            with self.subTest(result=result):
                self.probe_result = result
                self.assertEqual(self.code(lambda: self.release(ref, member_no[-3:], "as_intended")), "writer_may_be_live")
                self.assertEqual(ReleaseHeldTests.recorded(self), before)
        self.probe_result = "exited"
        self.assertEqual(self.release(ref, member_no[-3:], "as_intended")["receipt_now"], "applied")


class B2ReleaseUnlinkTests(Base):
    """Slice B2 (G0 B2-9, B2-10): ``left_open`` and the late settle of a
    ready receipt behind a released HELD write; malformed refs."""

    run_main = CliTests.run_main
    held = ReleaseHeldTests.held
    release = ReleaseHeldTests.release
    claimed = UnlinkTests.claimed
    by_order = UnlinkTests.by_order
    unlink = UnlinkTests.unlink

    def recorded(self) -> tuple:
        return UnlinkTests.recorded(self) + ReleaseHeldTests.recorded(self)

    def refused(self, call, code: str) -> None:
        before = self.recorded()
        self.assertEqual(self.code(call), code)
        self.assertEqual(self.recorded(), before)

    # -- B2-9 ----------------------------------------------------------------
    def released_while_open(self, finding: str) -> tuple[str, str]:
        ref, member_no = self.held()
        self.ledger.update_receipt(order_gid(1), state="waiting_member", code=None)  # neither ready nor decided
        if finding == "not_as_intended":
            self.book.members[member_no].update(type="GUEST", expiry=None)
        row = tuple(self.receipt(1))
        dry = self.release(ref, member_no[-3:], finding, apply=False)
        out = self.release(ref, member_no[-3:], finding)
        for value in (dry, out):
            self.assertEqual((value["receipt_now"], value["counts"]), ("left_open", {"release_receipt_left_open": 1}))
        self.assertEqual(tuple(self.receipt(1)), row)  # left as it is
        self.assertEqual(self.ledger.held_writes(), 0)
        self.advance(minutes=15)
        out = self.engine().run(apply=True)  # not ready: the run settles nothing late
        self.assertEqual((out["counts"].get("release_receipt_settled_late"), self.receipt(1)["state"]), (None, "waiting_member"))
        return ref, member_no

    def late(self) -> dict:
        self.ledger.update_receipt(order_gid(1), state="ready")  # ready again
        reads, saves, writes = self.book.reads, self.book.saves, self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
        payloads = len(self.book.payloads)
        self.advance(minutes=15)
        out = self.engine().run(apply=True)
        self.assertEqual(len(self.book.payloads), payloads)  # nothing sent, not even a save that would be skipped
        self.assertEqual((out["counts"].get("write_already_recorded"), out["counts"].get("release_receipt_settled_late"), out["saves"]), (1, 1, 0))
        self.assertEqual((self.book.reads, self.book.saves, self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0]),
                         (reads, saves, writes))
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "HELD")
        return out

    def test_b2_9_as_intended_a_receipt_ready_again_is_applied_late_and_a_later_refund_alerts(self):
        ref, member_no = self.released_while_open("as_intended")
        out = self.late()
        self.assertIn({"ref": ref, "code": "release_receipt_settled_late", "member_no_masked": bm.mask(member_no)}, out["refs"])
        row = self.receipt(1)
        self.assertEqual((row["state"], row["code"], row["member_no"], row["basis_expiry_local"]), ("applied", "upgrade", member_no, NEW_EXPIRY))
        self.advance(minutes=15)  # settled once: the next run leaves it
        self.assertIsNone(self.engine().run(apply=True)["counts"].get("release_receipt_settled_late"))
        self.shopify.add(order_node(1, status="REFUNDED", updated="2026-11-18T05:00:00Z"))
        self.advance(minutes=15)
        self.assertEqual(self.engine().run(apply=False)["alerts"], ["refund_on_basis"])

    def test_b2_9_not_as_intended_a_receipt_ready_again_is_held_late(self):
        self.released_while_open("not_as_intended")
        self.late()
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["code"]), ("held", "held_write_released"))
        self.assertEqual(self.book.members[sg(1)]["type"], "GUEST")

    def test_b2_9_an_unreleased_held_write_settles_nothing_and_left_decided_stays_for_decided_receipts(self):
        ref, member_no = self.held()
        self.advance(minutes=15)
        out = self.engine().run(apply=False)  # observe: the HELD write is not released yet
        self.assertEqual((out["counts"].get("write_already_recorded"), out["counts"].get("release_receipt_settled_late")), (1, None))
        self.assertEqual(self.receipt(1)["state"], "ready")
        self.ledger.update_receipt(order_gid(1), state="no_change", code="expiry_already_later", member_no=member_no)
        self.assertEqual(self.release(ref, member_no[-3:], "as_intended")["receipt_now"], "left_decided")

    # -- B2-10 ---------------------------------------------------------------
    def test_b2_10_a_malformed_ref_of_either_kind_is_an_argument_refusal_and_records_nothing(self):
        ref, member_no = self.claimed()
        last3 = member_no[-3:]
        order_ref = self.ledger.ref("o", order_gid(1))
        bad_orders = ("o-" + "a" * 19, "o-" + "a" * 21, "o-" + "g" * 20, order_ref.upper(), " " + order_ref, order_ref + "\n", order_ref + " ",
                      "x-" + order_ref[2:], ref, "", "o-", "o_" + order_ref[2:])
        bad_customers = ("c-" + "a" * 19, "c-" + "a" * 21, ref.upper(), ref + "\n", " " + ref, order_ref, "", "c-")
        for bad in bad_orders:
            with self.subTest(order_ref=bad):
                self.refused(lambda: self.by_order(bad, last3), "unlink_arguments_invalid")
        for bad in bad_customers:
            with self.subTest(customer_ref=bad):
                self.refused(lambda: self.unlink(bad, last3), "unlink_arguments_invalid")
        self.refused(lambda: self.by_order("o-" + "0" * 20, last3), "unlink_not_a_claim_link")  # well-formed, unknown
        self.refused(lambda: self.unlink("c-" + "0" * 20, last3), "unlink_not_a_claim_link")
        self.assertEqual(self.by_order(order_ref, last3, apply=False)["counts"]["claim_unlinked"], 1)

    def test_b2_10_the_cli_refuses_a_malformed_order_ref_as_an_argument(self):
        member_no = self.guest_with_order(1, linked=False)
        self.run_main("init")
        self.run_main("observe")
        for bad in ("o-" + "a" * 19, "O-" + "A" * 20, "c-" + "a" * 20):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_main("unlink", "--order-ref", bad, "--member-last3", member_no[-3:], "--reason", "other", "--apply")[:2],
                                 (2, {"status": "refused", "code": "unlink_arguments_invalid"}))
        self.assertEqual(self.run_main("unlink", "--order-ref", "o-" + "0" * 20, "--member-last3", member_no[-3:], "--reason", "other")[:2],
                         (2, {"status": "refused", "code": "unlink_not_a_claim_link"}))


class B2ClaimLinksTests(Base):
    """Slice B2 (G0 B2-11, PR #303 N1): ``claim-links``, a read-only listing
    of the active claim links."""

    run_main = CliTests.run_main

    def listing(self) -> dict:
        return bm.claim_links_listing(self.root, config=self.cfg())

    def tables(self) -> dict:
        conn = self.ledger.conn
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in ("meta", "receipts", "claim_links", "claim_unlinks", "reviews", "writes", "write_releases", "watermarks", "runs")}

    def test_b2_11_active_links_keyed_masked_sorted_and_voided_links_counted_only(self):
        self.engine()
        self.now = datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc)  # 01:30 on 19 Nov in Singapore
        for n, source in ((1, "order"), (2, "index"), (3, "order"), (4, "index")):
            self.ledger.add_claim(customer_gid(n), sg(n), source, "phone_and_name_match")
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO claim_unlinks(link_id, reason, run_id, created_at) VALUES "
                         "((SELECT link_id FROM claim_links WHERE customer_gid = ?), 'other', 'run-x', 't')", (customer_gid(3),))
        before = self.tables()
        out = self.listing()
        self.assertEqual(self.tables(), before)  # read only: no run row, nothing written
        expected = sorted(({"ref": self.ledger.ref("c", customer_gid(n)), "member_no_masked": bm.mask(sg(n)), "source": source,
                            "created_day": "2026-11-19"} for n, source in ((1, "order"), (2, "index"), (4, "index"))), key=lambda e: e["ref"])
        self.assertEqual(out, {"status": "ok", "command": "claim_links", "links": expected, "active": 3, "voided": 1, "truncated": False})
        text = json.dumps(out)
        for secret in [customer_gid(n) for n in range(1, 5)] + [sg(n) for n in range(1, 5)] + [local(n) for n in range(1, 5)]:
            self.assertNotIn(secret, text)
        self.assertNotIn(self.ledger.ref("c", customer_gid(3)), text)  # the voided link is not listed
        self.emitted.append(out)  # through the check after every test as well

    def test_b2_11_at_most_500_entries_then_truncated(self):
        self.engine()
        with self.ledger.tx() as conn:
            for n in range(500):
                conn.execute("INSERT INTO claim_links(customer_gid, member_no, member_no_key, source, code, created_at) VALUES (?, ?, ?, 'index', "
                             "'phone_and_name_match', '2026-11-18T04:00:00Z')", (customer_gid(n), sg(n), sync.member_no_key(sg(n))))
        out = self.listing()
        self.assertEqual((len(out["links"]), out["active"], out["truncated"]), (500, 500, False))
        self.ledger.add_claim(customer_gid(500), sg(500), "index", "phone_and_name_match")
        out = self.listing()
        self.assertEqual((len(out["links"]), out["active"], out["truncated"]), (500, 501, True))
        refs = sorted(self.ledger.ref("c", customer_gid(n)) for n in range(501))
        self.assertEqual([e["ref"] for e in out["links"]], refs[:500])
        self.emitted.append(out)

    def test_b2_11_the_cli_command_needs_no_lock_and_refuses_as_the_digest(self):
        member_no = self.guest_with_order(1, linked=False)
        self.assertEqual(self.run_main("claim-links")[:2], (2, {"status": "refused", "code": "benefits_ledger_missing"}))
        self.run_main("init")
        self.run_main("observe")
        holder = bm.BenefitsLedger(self.root, clock=lambda: self.now)  # a running task holds the ledger
        try:
            code, out, text = self.run_main("claim-links")
        finally:
            holder.close()
        self.assertEqual((code, out["command"], out["active"], out["voided"], [e["source"] for e in out["links"]]), (0, "claim_links", 1, 0, ["order"]))
        self.assertEqual(out["links"][0]["member_no_masked"], bm.mask(member_no))
        for secret in (member_no, local(1), customer_gid(1), str(customer_number(1)), order_gid(1), "Fake Member 1"):
            self.assertNotIn(secret, text)
        # Its c- ref is the one unlink takes.
        code, dry, _ = self.run_main("unlink", "--customer-ref", out["links"][0]["ref"], "--member-last3", member_no[-3:], "--reason", "other")
        self.assertEqual((code, dry["command"], dry["counts"]["claim_unlinked"]), (0, "unlink_dry_run", 1))
        # Bound to the store and the book, as every command.
        self.assertEqual(self.run_main("claim-links", cfg={"shopify": {**raw_config(self.root, self.sync_root)["shopify"],
                                                                         "store_gid": "gid://shopify/Shop/2"}})[:2],
                         (2, {"status": "refused", "code": "benefits_ledger_binding_mismatch"}))

    def test_b2_11_a_v1_ledger_is_refused_and_left_as_it_is(self):
        before = make_v1_ledger(self.root)
        self.assertEqual(CliTests.run_main(self, "claim-links")[:2], (2, {"status": "refused", "code": "benefits_ledger_not_recognised"}))
        self.assertEqual((self.root / bm.LEDGER_FILE).read_bytes(), before)
        self.assertFalse((self.root / (bm.LEDGER_FILE + "-wal")).exists())


class B2TokenCacheScopeTests(unittest.TestCase):
    """Slice B2, tests only (G0 B2-13 G25): the cached token's scope check."""

    class Plain:
        def protect(self, data, entropy):
            return data

        def unprotect(self, data, entropy):
            return data

    def test_b2_13_g25_a_cached_token_with_any_other_scope_set_is_refused_and_cleared(self):
        temp = Path(tempfile.mkdtemp(prefix="xb-benefits-"))
        self.addCleanup(shutil.rmtree, temp, True)
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        for scope, kept in (("read_customers,read_orders", True), ("read_orders, read_customers", True),
                            ("read_customers,read_orders,write_customers", False), ("read_customers", False),
                            ("read_customers,read_orders,read_products", False)):
            with self.subTest(scope=scope):
                path = temp / "token.bin"
                path.write_bytes(json.dumps({"schema": direct.TOKEN_CACHE_SCHEMA, "shop_domain": SHOP, "expires_at": "2026-11-19T00:00:00Z",
                                             "scope": scope, "access_token": "t" * 32}).encode("utf-8"))
                cache = bm.BenefitsTokenCache(path, self.Plain(), shop_domain=SHOP, client_id="client-id-0000000")
                loaded = cache.load(now)
                if kept:
                    self.assertEqual((loaded[0], cache.note), ("t" * 32, None))
                else:
                    self.assertEqual((loaded, cache.note, path.exists()), (None, "token_cache_unreadable", False))


class B2NoReinitGuardTests(unittest.TestCase):
    """Slice B2 pass bar 2 (G0 1): the VM never needs a re-init for B2. The
    ledger schema, its version, the amount rule (its version and the source
    of the five amount functions) and the example config's rule fingerprint
    are the values at main a6e7142 (slice B1 + C1)."""

    SCHEMA_SHA256 = "d3b426a76493d09d0aefdc3b70a1085a63f4d654d4cc4d5dc0ecb8acef2e3e60"
    AMOUNT_FUNCTIONS = ("goods_amount", "gift_card_paid", "qualifying_amount", "qualifies", "money_refund_unattributed")
    AMOUNT_SOURCE_SHA256 = "67f7f494a268f8314d908151f4472ffa4e7318eedc5c4e0582786131440a672d"
    EXAMPLE_FINGERPRINT = "sha256:964788c0b03682173413a8cc7e290a5f4126ad1ba70d0ae061e92c6e66c08463"
    # B2 fix round 1 (G2 L2): the refunded-unit value and the order amount parsing.
    MORE_SOURCES = {
        ("refunded_unit_cents",): "9bebeca606e55d96269f21343f7fe7c54ac7f190eb77965976c6e13e2679061a",
        ("order_from_node", "_money", "_payment_cents", "payments_complete"): "d4ef8532c73a124a80e45229a3eea32e46f88e27ae1a3f9db1c417715258c670",
    }

    def test_b2_schema_amount_rule_and_fingerprint_are_unchanged_since_a6e7142(self):
        import inspect

        self.assertEqual((bm.SCHEMA_VERSION, bm.AMOUNT_RULE_VERSION), (2, 2))
        self.assertEqual(hashlib.sha256(bm.LEDGER_SCHEMA.encode("utf-8")).hexdigest(), self.SCHEMA_SHA256)
        source = "".join(inspect.getsource(getattr(bm, name)) for name in self.AMOUNT_FUNCTIONS)
        self.assertEqual(hashlib.sha256(source.encode("utf-8")).hexdigest(), self.AMOUNT_SOURCE_SHA256)
        for names, digest in self.MORE_SOURCES.items():
            with self.subTest(names=names):
                source = "".join(inspect.getsource(getattr(bm, name)) for name in names)
                self.assertEqual(hashlib.sha256(source.encode("utf-8")).hexdigest(), digest)
        for name in ("member_benefits.example.json", "member_benefits.live.example.json"):
            with self.subTest(name=name):
                value = json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))
                self.assertEqual(bm.parse_config(value).rule_fingerprint(), self.EXAMPLE_FINGERPRINT)


class ClassifySaveTests(unittest.TestCase):
    @staticmethod
    def run_of(value, code=0) -> dl.WriteRun:
        return dl.WriteRun(started=True, exit_code=code, stdout=json.dumps(value))

    def test_outcomes(self):
        saved = {"status": "saved", "save_invocation_count": 1, "readback_found": True, "readback_type_match": True, "readback_expiry_match": True,
                 "fields_unchanged": True, "changed_fields": [], "post_last_modified_local": "2026-11-20T04:00:01.007", "outbound_queue_changed": False}
        c = bm.classify_save
        self.assertEqual(c(self.run_of(saved), claim_exists=True), bm.SaveOutcome("DONE", None, "2026-11-20T04:00:01.007"))
        self.assertEqual(c(self.run_of({**saved, "readback_expiry_match": False}), claim_exists=True).code, "readback_expiry_mismatch")
        self.assertEqual(c(self.run_of({**saved, "readback_type_match": False}), claim_exists=True).code, "readback_type_mismatch")
        self.assertEqual(c(self.run_of({**saved, "fields_unchanged": False, "changed_fields": "Name"}), claim_exists=True).changed_fields, ("Name",))
        self.assertEqual(c(self.run_of(saved), claim_exists=False).state, "UNCERTAIN")
        for skip in sorted(bm.SAVE_SKIP_CODES):
            self.assertEqual(c(self.run_of({"status": "skipped", "code": skip}), claim_exists=False), bm.SaveOutcome("SKIPPED", skip))
        self.assertEqual(c(self.run_of({"status": "skipped", "code": "changed_since_read"}), claim_exists=True).state, "UNCERTAIN")
        self.assertEqual(c(self.run_of({"error": "benefit_type_not_allowed"}, 1), claim_exists=False), bm.SaveOutcome("NOT_WRITTEN", "benefit_type_not_allowed"))
        self.assertEqual(c(self.run_of({"error": "bridge_failed"}, 1), claim_exists=True).state, "UNCERTAIN")
        self.assertEqual(c(dl.WriteRun(started=True, timed_out=True, exited=False), claim_exists=False).state, "UNCERTAIN")


class StaticTests(unittest.TestCase):
    def test_python_builds_no_autocount_sql(self):
        source = (ROOT / "directlocal" / "xb_member_benefits.py").read_text(encoding="utf-8")
        self.assertNotIn("ExecuteScalar", source)
        self.assertNotRegex(source, r"(?i)\bFROM\s+(dbo\.)?(Member|MemberType|ChangeLog)\b")
        self.assertNotRegex(source, r"(?i)\bUPDATE\s+(dbo\.)?Member\b")

    def test_the_sync_and_type_move_code_is_not_imported_for_writing(self):
        source = (ROOT / "directlocal" / "xb_member_benefits.py").read_text(encoding="utf-8")
        self.assertNotIn("SyncLedger(", source)
        self.assertNotIn("MoveLedger(", source)
        self.assertNotIn(".add_link(", source)

    def test_sources_stay_ascii(self):
        for path in (ROOT / "directlocal" / "xb_member_benefits.py", Path(__file__)):
            path.read_bytes().decode("ascii")


if __name__ == "__main__":
    unittest.main()
