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


def customer_gid(n: int) -> str:
    return f"gid://shopify/Customer/{1000 + n}"


def order_gid(n: int) -> str:
    return f"gid://shopify/Order/{5000 + n}"


def money(amount: str) -> dict:
    return {"shopMoney": {"amount": amount, "currencyCode": "SGD"}}


def customer_node(n: int, *, tags=("member-mg",), registration: str | None = SIGNUP_AT, phone: str | None = "default",
                  first: str = "Fake", last: str | None = None, created: str = "2026-11-01T00:00:00Z") -> dict:
    return {"id": customer_gid(n), "createdAt": created, "tags": list(tags), "firstName": first, "lastName": last if last is not None else f"Member {n}",
            "defaultPhoneNumber": None if phone is None else {"phoneNumber": "+" + sg(n) if phone == "default" else phone},
            "registration": None if registration is None else {"createdAt": registration}}


def order_node(n: int, *, customer: dict | None | str = "default", lines=(("300.00", 1, 1, False, ()),), shipping=("15.00",),
               created: str = ORDER_AT, updated: str | None = None, status: str = "PAID", test: bool = False, cancelled: str | None = None,
               taxes: bool = True, currency: str = "SGD", refunded: str = "0.00", more_lines: bool = False) -> dict:
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
    }


class FakeShopify:
    """The benefits app's reads over fixtures in the documented shapes."""

    def __init__(self):
        self.orders_nodes: dict[str, dict] = {}
        self.customers: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.scopes = ["read_customers", "read_orders"]
        self.fail_orders: str | None = None
        self.complete = True

    def add(self, node: dict) -> None:
        self.orders_nodes[node["id"]] = node
        if node.get("customer"):
            self.customers[node["customer"]["id"]] = node["customer"]

    def verify_session(self, *, store_gid):
        self.calls.append(("session",))
        if frozenset(self.scopes) != bm.REQUIRED_SCOPES:
            raise direct.ShopifyDirectError("shopify_token_scope_not_expected")

    def orders(self, since, max_pages):
        self.calls.append(("orders", since))
        if self.fail_orders:
            raise direct.ShopifyDirectError(self.fail_orders, retryable=True)
        nodes = sorted((n for n in self.orders_nodes.values() if dl.parse_created_at(n["updatedAt"]) >= since), key=lambda n: n["updatedAt"])
        return [bm.order_from_node(n) for n in nodes], self.complete

    def customer(self, gid):
        self.calls.append(("customer", gid))
        node = self.customers.get(gid)
        return None if node is None else bm.customer_from_node(node)

    def find_by_phone(self, digits):
        self.calls.append(("phone", digits))
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
            ("gift_card_excluded", (("250.00", 1, 1, False, ()), ("100.00", 1, 1, True, ())), (), {}, (25000, 25000)),
            ("gift_card_included_option", (("250.00", 1, 1, False, ()), ("100.00", 1, 1, True, ())), (), {"amount_exclude_gift_cards": False}, (35000, 35000)),
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
        self.guest_with_order(1, lines=(("200.00", 2, 2, False, ()),))
        engine = self.engine()
        engine.run(apply=True)
        self.advance(days=2)
        self.shopify.add(order_node(1, lines=(("200.00", 2, 2, False, ()),), refunded="50.00", updated="2026-11-20T03:00:00Z"))
        out = engine.run(apply=True)
        self.assertEqual(out["alerts"], [])
        self.assertEqual((self.receipt(1)["state"], self.receipt(1)["refunded_cents"]), ("applied", 5000))
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
        self.assertEqual((out["saves"], out["counts"].get("write_already_recorded")), (0, 1))
        self.assertEqual((self.book.saves, len(self.book.payloads)), (1, 1))

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
                self.assertTrue(cfg.amount_before_discounts and cfg.amount_exclude_shipping and cfg.amount_exclude_gift_cards and cfg.same_day_signup_counts)

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
        client, transport = self.client([(200, {"data": {"customers": {"nodes": nodes}}})])
        self.assertEqual([c.gid for c in client.find_by_phone(sg(1))], [customer_gid(1)])
        self.assertEqual(transport.bodies[0]["variables"], {"search": "phone:+" + sg(1)})


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
