"""#155 phase 1: two-way member create sync (offline, fakes only).

No AutoCount process, Shopify request or network call happens: the Shopify
and AC2 ports are in-memory fakes, the real Shopify client runs on a scripted
transport, and the credential loader reads a fake store. Phone numbers are
built at runtime from fragments in the reserved fake range (country code,
then 8000, then 0001-0099); emails use example.com.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))

import xb_directlocal as dl  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402

STORE = "gid://shopify/Shop/900000001"
DOMAIN = "xb-sync-test.synthetic.invalid"
T0 = datetime(2026, 10, 21, 2, 0, 0, tzinfo=timezone.utc)
CUTOVER = T0 - timedelta(days=1)
TEST_BOOK = "AED_TESTINGAPI"
PROD_BOOK = "AED_XBOUNDARIES"


def sg(n: int) -> str:
    """Reserved fake Singapore number ``65`` + ``8000`` + 4 digits (n 1-99)."""

    assert 1 <= n <= 99
    return "65" + "8000" + f"{n:04d}"


def local(n: int) -> str:
    return sg(n)[2:]


def plus(digits: str) -> str:
    return "+" + digits


# A fake foreign (Malaysian-format) number, assembled so no literal appears.
FOREIGN_DIGITS = "60" + "1" + "2000" + "0001"


def gid(n: int) -> str:
    return f"gid://shopify/Customer/{700000 + n}"


def config_value(**overrides) -> dict:
    value = {
        "schema": sync.CONFIG_SCHEMA, "state_root": "C:\\XB\\xb155-member-sync", "autocount_book": TEST_BOOK,
        "shopify": {"store_gid": STORE, "shop_domain": DOMAIN, "api_version": "2026-07", "auth": {"method": "admin_api_access_token", "token_env": "XB_SHOPIFY_SYNC_TOKEN"}},
        "shopify_cutover_utc": dl.ts(CUTOVER), "changelog_xml_column": "XmlData",
    }
    value.update(overrides)
    return value


def make_config(**overrides) -> sync.SyncConfig:
    return sync.parse_config(config_value(**overrides))


def customer(n: int, *, first="Alpha", last="Tester", phone="sg", email=None, registered=None, tags=("member-mg",), birthday=None, start_value="2026-10-21") -> sync.ShopifyCustomer:
    registered = T0 if registered is None else registered
    phone_value = plus(sg(n)) if phone == "sg" else phone
    return sync.ShopifyCustomer(
        gid=gid(n), first_name=first, last_name=last, phone=phone_value, email=email, created_at=registered - timedelta(days=400),
        updated_at=registered, tags=tuple(tags), registration_at=registered, registration_value=start_value, birthday_value=birthday,
    )


def changelog_xml(member_no: str, name: str, mobile: str | None = None, email: str | None = None, dob: str | None = None) -> str:
    parts = [f"<MemberNo>{member_no}</MemberNo>", f"<Name>{name}</Name>"]
    if mobile is not None:
        parts.append(f"<MobilePhone>{mobile}</MobilePhone>")
    if email is not None:
        parts.append(f"<EmailAddress>{email}</EmailAddress>")
    if dob is not None:
        parts.append(f"<DOB>{dob}T00:00:00</DOB>")
    return "<Member>" + "".join(parts) + "</Member>"


class FakeShopify:
    def __init__(self):
        self.customers: dict[str, sync.ShopifyCustomer] = {}
        self.created: list[dict] = []
        self.searches: list[str] = []
        self.create_mode = "ok"
        self.next_gid = 900

    def add(self, item: sync.ShopifyCustomer) -> sync.ShopifyCustomer:
        self.customers[item.gid] = item
        return item

    def registrations(self, search, max_pages, birthday):
        self.searches.append(search)
        return [c for c in self.customers.values() if "member-mg" in c.tags], True

    def customer(self, customer_gid, birthday):
        return self.customers.get(customer_gid)

    def find_by_phone(self, phone_e164):
        digits = phone_e164[1:]
        return [c for c in self.customers.values() if sync.phone_member_no(c.phone)[0] == digits]

    def create_customer(self, customer_input):
        if self.create_mode == "uncertain":
            raise direct.ShopifyDirectError("shopify_transport_failed")
        if self.create_mode == "rejected":
            raise sync.ShopifyCreateRejected("shopify_create_rejected")
        self.created.append(dict(customer_input))
        self.next_gid += 1
        created = sync.ShopifyCustomer(gid(self.next_gid), customer_input["firstName"], None, customer_input.get("phone"), customer_input.get("email"), T0, T0, tuple(customer_input["tags"]), None, None, None)
        self.customers[created.gid] = created
        return created.gid


class FakeAc2:
    """In-memory AC2 book and HQ ChangeLog (ids with gaps)."""

    def __init__(self):
        self.members: list[sync.Ac2Member] = []
        self.changelog: list[tuple[int, str]] = []
        self.next_id = 1000
        self.creates: list[tuple[str, dict]] = []
        self.create_result = ("CREATED_VERIFIED", None)
        self.lookups = 0
        # GetNextMemberNo previews; like the test book, saving does not advance it.
        self.next_previews: list = ["M000201"]
        self.generator_calls = 0

    def next_member_no(self):
        self.generator_calls += 1
        value = self.next_previews[min(self.generator_calls, len(self.next_previews)) - 1]
        if isinstance(value, Exception):
            raise value
        return value

    def add(self, member: sync.Ac2Member, *, log: bool = False) -> None:
        self.members.append(member)
        if log:
            self.log_insert(member)

    def log_insert(self, member: sync.Ac2Member) -> None:
        self.next_id += 7  # ids have gaps
        self.changelog.append((self.next_id, changelog_xml(member.member_no, member.name or "", member.mobile_phone, member.email_address, member.dob)))

    def lookup(self, phone_digits, member_nos):
        self.lookups += 1
        local_digits = sync.sg_local(phone_digits)
        keys = {sync.member_no_key(m) for m in member_nos}
        found = []
        for m in self.members:
            key = sync.member_no_key(m.member_no)
            mobile = "".join(ch for ch in (m.mobile_phone or "") if ch.isdigit())
            if (phone_digits and (key in {phone_digits, local_digits} or mobile in {phone_digits, local_digits})) or (local_digits and local_digits in m.member_no) or key in keys:
                found.append(m)
        return found

    def changelog_inserts(self, after_id, limit, column):
        assert column == "XmlData"
        rows = [row for row in self.changelog if after_id is None or row[0] > after_id][:limit]
        return rows, max([row[0] for row in self.changelog], default=0)

    def create_member(self, member_no, payload, *, write_id):
        self.creates.append((member_no, dict(payload)))
        state, code = self.create_result
        if state in ("CREATED_VERIFIED", "WRITE_OUTCOME_UNCERTAIN"):
            member = sync.Ac2Member(member_no, payload["name"], payload["mobile_phone"], payload["email_address"], payload["dob"])
            self.add(member, log=True)  # HQ ChangeLog sees our own insert
        return state, code


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-"))
        self.now = T0 + timedelta(hours=1)
        self.ledger = sync.SyncLedger(self.temp, clock=lambda: self.now, check_custody=False)
        self.ledger.init_meta(autocount_book=TEST_BOOK, store_gid=STORE, shop_domain=DOMAIN)
        self.shopify = FakeShopify()
        self.ac2 = FakeAc2()

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.temp, ignore_errors=True)

    def engine(self, **overrides) -> sync.SyncEngine:
        settings = {"ac2_create_enabled": True, "shopify_create_enabled": True}
        settings.update(overrides)
        return sync.SyncEngine(self.ledger, make_config(**settings), shopify=self.shopify, ac2=self.ac2)

    def run_sync(self, direction="both", **overrides) -> dict:
        return self.engine(**overrides).run(direction=direction)

    def links(self) -> dict[str, str]:
        return {row["customer_gid"]: row["member_no"] for row in self.ledger.conn.execute("SELECT * FROM links")}

    def assert_no_pii(self, value) -> None:
        text = json.dumps(value)
        for n in range(1, 100):
            self.assertNotIn(local(n), text)
        for word in ("Alpha", "Tester", "Bravo", "example.com", "<Member"):
            self.assertNotIn(word, text)


class PhoneRuleTests(unittest.TestCase):
    def test_singapore_foreign_and_missing_country_code(self):
        cases = [
            (plus(sg(1)), (sg(1), "ok")),
            ("+65 " + local(2)[:4] + " " + local(2)[4:], (sg(2), "ok")),
            (local(3), (sg(3), "ok")),  # SG 8 digits without 65 -> 65 prepended
            (sg(4), (sg(4), "ok")),
            (plus(FOREIGN_DIGITS), (FOREIGN_DIGITS, "ok")),  # foreign keeps its country code
            (FOREIGN_DIGITS, (FOREIGN_DIGITS, "ok")),
            ("1" + local(5)[1:], (None, "invalid")),  # 8 digits not starting 3/6/8/9
            ("+65" + "1234", (None, "invalid")),
            ("12345", (None, "invalid")),
            ("call me", (None, "invalid")),
            (None, (None, "absent")),
            ("  ", (None, "absent")),
        ]
        for raw, expected in cases:
            with self.subTest(case=cases.index((raw, expected))):
                self.assertEqual(sync.phone_member_no(raw), expected)
        for prefix in "3689":
            self.assertEqual(sync.phone_member_no(prefix + "0000001")[1], "ok")

    def test_member_no_key_ignores_case_and_surrounding_spaces(self):
        self.assertEqual(sync.member_no_key(" abC1  "), sync.member_no_key("ABC1"))
        self.assertNotEqual(sync.member_no_key("AB-C1"), sync.member_no_key("ABC1"))  # hyphens stay significant

    def test_first_name_matching(self):
        self.assertTrue(sync.first_name_matches("Mei Ling", "TAN MEI LING"))
        self.assertTrue(sync.first_name_matches("meiling", "Mei Ling Tan"))
        self.assertTrue(sync.first_name_matches(" ALPHA ", "alpha tester"))
        self.assertFalse(sync.first_name_matches("Al", "Alpha Tester"))
        self.assertFalse(sync.first_name_matches("", "Alpha"))
        self.assertFalse(sync.first_name_matches(None, "Alpha"))
        self.assertFalse(sync.first_name_matches("Bravo", "Alpha Tester"))

    def test_fallback_member_no_fits_twenty_characters(self):
        value = sync.fallback_member_no(sync.phone_member_no(plus(sg(9)))[0], "Bravo-Zoë", "Very Long Family Name")
        self.assertEqual(len(value), 20)
        self.assertTrue(value.startswith(sg(9)))
        self.assertRegex(value[10:], r"^[A-Z]+$")
        self.assertEqual(sync.fallback_member_no(sg(9), "Bravo", "Tester"), sg(9) + "BRAVOTESTE")
        self.assertIsNone(sync.fallback_member_no(sg(9), "陈", "美玲"))
        self.assertTrue(dl.MEMBER_NO_RE.fullmatch(value))

    def test_suffix_candidates_trim_letters_for_the_digit(self):
        base = sync.fallback_member_no(sg(9), "Bravo", "Tester")
        candidates = sync.suffix_candidates(base)
        self.assertEqual(candidates[0], base)
        self.assertEqual(candidates[1:], [base[:19] + str(d) for d in range(2, 10)])
        self.assertTrue(all(len(c) <= 20 for c in candidates))
        short = sync.suffix_candidates(FOREIGN_DIGITS + "AB")
        self.assertEqual(short[1], FOREIGN_DIGITS + "AB2")

    def test_birthday_month_and_dob(self):
        for raw, month in ((3, 3), ("03", 3), ("March", 3), ("sep", 9), ("1990-07-15", 7), ("--12-01", 12), ("2001-02", 2), ("13", None), ("", None), (True, None), ("Smarch", None)):
            self.assertEqual(sync.birthday_month(raw), month, raw)
        self.assertEqual(sync.dob_from_month(4), "2000-04-01")
        self.assertIsNone(sync.dob_from_month(None))

    def test_changelog_xml_parsing(self):
        member = sync.parse_changelog_member(changelog_xml(sg(1), "Alpha Tester", local(1), "alpha@example.com", "2000-05-01"))
        self.assertEqual((member.member_no, member.name, member.mobile_phone, member.dob), (sg(1), "Alpha Tester", local(1), "2000-05-01"))
        attrs = sync.parse_changelog_member(f'<row MEMBERNO="{sg(2)}" Name="Bravo" />')
        self.assertEqual(attrs.member_no, sg(2))
        for bad in (None, "", "<Member><Name>x</Name></Member>", "<not-xml", '<!DOCTYPE x [<!ENTITY a "b">]><Member/>'):
            self.assertIsNone(sync.parse_changelog_member(bad))
        self.assertEqual(repr(member), "Ac2Member()")


class ConfigTests(unittest.TestCase):
    def test_defaults_switch_every_write_off(self):
        config = make_config()
        self.assertFalse(config.ac2_create_enabled)
        self.assertFalse(config.shopify_create_enabled)
        self.assertEqual(config.max_writes_per_run, 5)

    def test_unknown_keys_refused(self):
        for value in (config_value(extra=True), config_value(shopify={**config_value()["shopify"], "extra": 1})):
            with self.assertRaises(dl.DirectLocalError):
                sync.parse_config(value)
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.parse_config(config_value(birthday_month_metafield={"namespace": "custom", "key": "birthday_month", "type": "number_integer", "x": 1}))
        self.assertEqual(caught.exception.code, "sync_config_birthday_month_metafield_invalid")

    def test_production_book_refused(self):
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.parse_config(config_value(autocount_book=PROD_BOOK))
        self.assertEqual(caught.exception.code, "production_book_refused")
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.parse_config(config_value(autocount_book="AED_OTHER"))
        self.assertEqual(caught.exception.code, "autocount_book_not_authorised")
        with self.assertRaises(dl.DirectLocalError):
            sync.SyncBridgeClient(["x"], autocount_book=PROD_BOOK, claims=Path("."))

    def test_write_cap_bounds(self):
        for bad in (6, -1, True, "5"):
            with self.assertRaises(dl.DirectLocalError):
                sync.parse_config(config_value(max_writes_per_run=bad))

    def test_member_no_metafield_is_validated(self):
        for bad in (
            {"namespace": "custom", "key": "ac2_member_no", "type": "number_integer"},
            {"namespace": "custom", "key": "birthday_month", "type": "single_line_text_field"},
            {"namespace": "custom", "key": "ac2_member_no", "type": "single_line_text_field", "x": 1},
        ):
            with self.assertRaises(dl.DirectLocalError):
                sync.parse_config(config_value(ac2_member_no_metafield=bad))

    def test_registration_tag_cannot_be_a_create_tag(self):
        with self.assertRaises(dl.DirectLocalError):
            sync.parse_config(config_value(shopify_create_tags=["member-mg"]))

    def test_example_config_parses_with_writes_off(self):
        example = json.loads((ROOT / "config" / "member_sync.example.json").read_text(encoding="utf-8"))
        config = sync.parse_config(example)
        self.assertEqual(config.autocount_book, TEST_BOOK)
        self.assertFalse(config.ac2_create_enabled or config.shopify_create_enabled)


class LedgerTests(unittest.TestCase):
    def test_production_book_refused_by_ledger(self):
        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-ledger-"))
        try:
            ledger = sync.SyncLedger(temp, check_custody=False)
            try:
                with self.assertRaises(dl.DirectLocalError):
                    ledger.init_meta(autocount_book=PROD_BOOK, store_gid=STORE, shop_domain=DOMAIN)
                import sqlite3
                with self.assertRaises(sqlite3.IntegrityError):
                    ledger.conn.execute("INSERT INTO meta(id, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at) VALUES (1, 'x', ?, 's', 'd', ?, 't')", (PROD_BOOK, "0" * 64))
            finally:
                ledger.close()
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_custody_is_checked_by_default(self):
        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-ledger-"))
        try:
            with mock.patch.object(dl, "verify_private_state", side_effect=dl.DirectLocalError("state_root_acl_unsafe")):
                with self.assertRaises(dl.DirectLocalError):
                    sync.SyncLedger(temp)
        finally:
            shutil.rmtree(temp, ignore_errors=True)


class ShopifyToAc2Tests(SyncTestCase):
    def test_new_registration_creates_phone_member_no(self):
        self.shopify.add(customer(1, email="alpha@example.com", birthday="7"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 1)
        member_no, payload = self.ac2.creates[0]
        self.assertEqual(member_no, sg(1))
        self.assertEqual(payload, {"profile": "sync_p1", "name": "Alpha Tester", "mobile_phone": sg(1), "email_address": "alpha@example.com", "register_date": "2026-10-21", "dob": "2000-07-01"})
        self.assertEqual(self.links(), {gid(1): sg(1)})
        self.assertEqual(result["counts"]["ac2_member_created"], 1)
        self.assert_no_pii(result)

    def test_same_phone_same_first_name_links(self):
        self.ac2.add(sync.Ac2Member(sg(2), "TESTER ALPHA", None))
        self.shopify.add(customer(2))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(self.links(), {gid(2): sg(2)})
        self.assertEqual(result["counts"]["phone_and_first_name_match"], 1)

    def test_mobile_phone_and_embedded_digits_count_as_same_phone(self):
        self.ac2.add(sync.Ac2Member("OLD-17", "Alpha T", local(3)[:4] + " " + local(3)[4:]))
        self.shopify.add(customer(3))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(3): "OLD-17"})
        self.ac2.add(sync.Ac2Member("X" + local(4), "Alpha", None))
        self.shopify.add(customer(4))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links()[gid(4)], "X" + local(4))
        self.assertEqual(self.ac2.creates, [])

    def test_same_phone_other_name_creates_suffix_member_no(self):
        self.ac2.add(sync.Ac2Member(sg(5), "Charlie Holder", None))
        self.shopify.add(customer(5, first="Bravo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        member_no = self.ac2.creates[0][0]
        self.assertEqual(member_no, (sg(5) + "BRAVOTESTER")[:20])
        self.assertLessEqual(len(member_no), 20)
        self.assertEqual(result["counts"]["shared_phone_name_fallback"], 1)
        self.assertEqual(self.links(), {gid(5): member_no})

    def test_suffix_member_no_taken_adds_a_digit(self):
        base = (sg(6) + "BRAVOTESTER")[:20]
        self.ac2.add(sync.Ac2Member(sg(6), "Charlie Holder", None))
        # Taken in a different case and with spaces: still taken.
        self.ac2.add(sync.Ac2Member(" " + base.lower() + " ", "Delta Other", None))
        self.shopify.add(customer(6, first="Bravo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates], [base[:19] + "2"])
        self.assertEqual(result["counts"]["shared_phone_suffix_digit"], 1)
        self.assertEqual(self.links(), {gid(6): base[:19] + "2"})

    def test_suffix_digits_two_to_nine_all_taken_skips(self):
        base = (sg(17) + "BRAVOTESTER")[:20]
        self.ac2.add(sync.Ac2Member(sg(17), "Charlie Holder", None))
        for candidate in sync.suffix_candidates(base):
            self.ac2.add(sync.Ac2Member(candidate, "Delta Other", None))
        self.shopify.add(customer(17, first="Bravo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["shared_phone_member_no_exhausted"], 1)

    def test_suffix_taken_by_our_own_link_adds_a_digit(self):
        base = (sg(18) + "BRAVOTESTER")[:20]
        self.ac2.add(sync.Ac2Member(sg(18), "Charlie Holder", None))
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(99), base, "matched", "test")
        self.shopify.add(customer(18, first="Bravo", last="Tester"))
        self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates], [base[:19] + "2"])

    def test_no_phone_uses_autocount_next_member_no(self):
        self.shopify.add(customer(7, phone=None, first="Echo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates[0][0], "M000201")
        self.assertIsNone(self.ac2.creates[0][1]["mobile_phone"])
        self.assertEqual(result["counts"]["no_phone_generator_member_no"], 1)
        self.assertEqual(self.links(), {gid(7): "M000201"})

    def test_invalid_phone_uses_autocount_next_member_no(self):
        self.shopify.add(customer(8, phone="12345", first="Echo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates[0][0], "M000201")
        self.assertEqual(result["counts"]["shopify_phone_invalid_treated_as_absent"], 1)
        self.assertEqual(result["counts"]["no_phone_generator_member_no"], 1)

    def test_repeated_generator_preview_continues_its_format(self):
        # The preview repeats after a save (test-book behaviour, 16 Oct 2026).
        self.shopify.add(customer(19, phone=None, first="Echo", last="Tester"))
        self.shopify.add(customer(28, phone=None, first="Foxtrot", last="Tester"))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(sorted(m for m, _ in self.ac2.creates), ["M000201", "M000202"])
        self.ac2.add(sync.Ac2Member(" m000203 ", "Golf Other", None))
        self.shopify.add(customer(29, phone=None, first="Hotel", last="Tester"))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates[-1][0], "M000204")

    def test_generator_failure_writes_nothing_and_retries_later(self):
        self.ac2.next_previews = [dl.BridgeError("bridge_read_failed")]
        self.shopify.add(customer(34, phone=None))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.ac2.creates, result["writes"]), ([], 0))
        self.assertEqual(result["counts"]["member_no_generator_failed"], 1)
        self.ac2.next_previews = ["M000301"]
        self.ac2.generator_calls = 0
        self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates], ["M000301"])

    def test_generator_not_called_while_writes_are_off(self):
        self.shopify.add(customer(35, phone=None))
        result = self.run_sync("shopify_to_ac2", ac2_create_enabled=False)
        self.assertEqual(self.ac2.generator_calls, 0)
        self.assertEqual(result["counts"]["ac2_create_disabled"], 1)

    def test_member_no_with_spaces_and_case_still_links(self):
        self.ac2.add(sync.Ac2Member(" " + sg(9) + "  ", "alpha", None))
        self.shopify.add(customer(9))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(self.links(), {gid(9): " " + sg(9) + "  "})

    def test_email_only_match_never_links(self):
        self.ac2.add(sync.Ac2Member(sg(10), "Alpha Tester", sg(10), "alpha@example.com"))
        self.shopify.add(customer(11, email="alpha@example.com"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates[0][0], sg(11))
        self.assertEqual(result["counts"]["new_member_phone"], 1)

    def test_shared_phone_records_link_only_to_exact_member_no(self):
        self.ac2.add(sync.Ac2Member(sg(12), "Alpha Tester", None))
        self.ac2.add(sync.Ac2Member("A" + local(12), "Alpha Tester", sg(12)))
        self.shopify.add(customer(12))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(12): sg(12)})
        self.ac2.add(sync.Ac2Member("B" + local(13), "Alpha Tester", sg(13)))
        self.ac2.add(sync.Ac2Member("C" + local(13), "Alpha Tester", sg(13)))
        self.shopify.add(customer(13))
        result = self.run_sync("shopify_to_ac2")
        self.assertNotIn(gid(13), self.links())
        self.assertEqual(result["counts"]["phone_on_multiple_records"], 1)
        self.assertEqual(self.ac2.creates, [])

    def test_before_cutover_and_untagged_are_skipped(self):
        self.shopify.add(customer(14, registered=CUTOVER - timedelta(minutes=1)))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["shopify_registered_before_cutover"], 1)

    def test_registration_event_waits_then_decides(self):
        pending = replace(customer(15), registration_at=None, registration_value=None)
        self.shopify.add(pending)
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS - 1):
            self.assertEqual(self.run_sync("shopify_to_ac2")["counts"]["shopify_registration_event_pending"], 1)
        self.assertEqual(self.ac2.creates, [])
        self.shopify.customers[gid(15)] = customer(15)  # start_date written
        self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 1)

    def test_ac2_create_disabled_is_refused_and_retried_later(self):
        self.shopify.add(customer(16))
        result = self.run_sync("shopify_to_ac2", ac2_create_enabled=False)
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["ac2_create_disabled"], 1)
        self.assertEqual(result["writes"], 0)
        self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(16)])

    def test_max_writes_per_run(self):
        for n in range(20, 27):
            self.shopify.add(customer(n))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 5)
        self.assertEqual(result["counts"]["max_writes_per_run_reached"], 2)
        self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 7)
        self.assertEqual(len({m for m, _ in self.ac2.creates}), 7)

    def test_uncertain_write_is_never_retried_and_resolves_by_lookup(self):
        self.ac2.create_result = ("WRITE_OUTCOME_UNCERTAIN", "writer_timeout")
        self.shopify.add(customer(30))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 1)
        self.assertNotIn(gid(30), self.links())
        self.ac2.create_result = ("CREATED_VERIFIED", None)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 1)  # never a second SaveMember
        self.assertEqual(self.links(), {gid(30): sg(30)})
        self.assertEqual(result["counts"]["uncertain_write_resolved_linked"], 1)

    def test_not_written_is_retried(self):
        self.ac2.create_result = ("NOT_WRITTEN_PROVEN", "writer_failed_before_claim")
        self.shopify.add(customer(31))
        self.run_sync("shopify_to_ac2")
        self.ac2.create_result = ("CREATED_VERIFIED", None)
        self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(31), sg(31)])
        self.assertEqual(self.links(), {gid(31): sg(31)})

    def test_crashed_intent_becomes_uncertain(self):
        engine = self.engine()
        engine._intent("shopify_to_ac2", sg(32), gid(32))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(result["counts"]["crashed_intent_uncertain"], 1)
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "UNCERTAIN")

    def test_watermark_advances_with_margin(self):
        self.shopify.add(customer(33))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ledger.watermark("shopify_updated_at"), dl.ts(self.now - timedelta(seconds=sync.SHOPIFY_WATERMARK_MARGIN_SECONDS)))
        self.run_sync("shopify_to_ac2")
        self.assertIn(self.ledger.watermark("shopify_updated_at"), self.shopify.searches[-1])
        self.assertEqual(len(self.ac2.creates), 1)


class Ac2ToShopifyTests(SyncTestCase):
    def initialise(self):
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"], {"changelog_watermark_initialised": 1})

    def test_first_run_starts_from_now(self):
        self.ac2.add(sync.Ac2Member(sg(40), "Alpha Tester", sg(40)), log=True)
        self.initialise()
        self.run_sync("ac2_to_shopify")
        self.assertEqual(self.shopify.created, [])

    def test_till_member_creates_shopify_customer(self):
        self.initialise()
        self.ac2.add(sync.Ac2Member(sg(41), "Alpha Tester", sg(41), "alpha@example.com", "2000-09-01"), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.shopify.created, [{
            "firstName": "Alpha Tester", "phone": plus(sg(41)), "email": "alpha@example.com", "tags": ["ac2-member"],
            "metafields": [
                {"namespace": "custom", "key": "ac2_member_no", "type": "single_line_text_field", "value": sg(41)},
                {"namespace": "custom", "key": "birthday_month", "type": "number_integer", "value": "9"},
            ],
        }])
        self.assertEqual(list(self.links().values()), [sg(41)])
        self.assertEqual(result["counts"]["shopify_customer_created"], 1)
        self.assert_no_pii(result)

    def test_existing_shopify_phone_links_instead_of_creating(self):
        self.initialise()
        self.shopify.add(customer(42, tags=()))
        self.ac2.add(sync.Ac2Member("TILL42", "alpha tester", local(42)), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(self.links(), {gid(42): "TILL42"})
        self.assertEqual(result["counts"]["linked_existing_shopify_customer"], 1)

    def test_shopify_phone_with_other_name_creates_separate_customer_without_phone(self):
        self.initialise()
        self.shopify.add(customer(43, first="Bravo", tags=()))
        self.ac2.add(sync.Ac2Member("TILL43", "Alpha Tester", local(43), "alpha@example.com"), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.shopify.created, [{
            "firstName": "Alpha Tester", "email": "alpha@example.com", "tags": ["ac2-member"],
            "metafields": [{"namespace": "custom", "key": "ac2_member_no", "type": "single_line_text_field", "value": "TILL43"}],
        }])
        new_gid = next(g for g in self.links())
        self.assertNotEqual(new_gid, gid(43))  # never linked to the phone's owner
        self.assertEqual(self.links(), {new_gid: "TILL43"})
        self.assertEqual(result["counts"]["shopify_phone_owned_by_other_name"], 1)
        self.assertEqual(result["counts"]["shopify_separate_customer_created_without_phone"], 1)
        self.assert_no_pii(result)
        self.assertEqual(self.run_sync("ac2_to_shopify")["counts"], {})  # done; nothing repeats

    def test_separate_customer_without_email_uses_name_only(self):
        self.initialise()
        self.shopify.add(customer(51, first="Bravo", tags=()))
        self.ac2.add(sync.Ac2Member(sg(51), "Alpha Tester", sg(51)), log=True)
        self.run_sync("ac2_to_shopify")
        created = self.shopify.created[0]
        self.assertNotIn("phone", created)
        self.assertNotIn("email", created)
        self.assertEqual(created["firstName"], "Alpha Tester")

    def test_separate_customer_waits_while_shopify_create_is_off(self):
        self.initialise()
        self.shopify.add(customer(52, first="Bravo", tags=()))
        self.ac2.add(sync.Ac2Member(sg(52), "Alpha Tester", sg(52)), log=True)
        result = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertEqual((self.shopify.created, self.links()), ([], {}))
        self.assertEqual(result["counts"]["shopify_create_disabled"], 1)
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM ac2_pending").fetchone()[0], "PENDING")

    def test_watermark_with_gaps_and_replay_is_a_no_op(self):
        self.initialise()
        for n in (44, 45, 46):
            self.ac2.add(sync.Ac2Member(sg(n), f"Member {n}", sg(n)), log=True)
        ids = [row[0] for row in self.ac2.changelog]
        self.assertTrue(all(b - a > 1 for a, b in zip(ids, ids[1:])))  # gaps
        self.run_sync("ac2_to_shopify")
        self.assertEqual(len(self.shopify.created), 3)
        self.assertEqual(self.ledger.watermark("ac2_changelog_id"), str(ids[-1]))
        # Replay: the watermark is rolled back and the same rows are read again.
        with self.ledger.tx() as conn:
            self.ledger.set_watermark(conn, "ac2_changelog_id", str(ids[0] - 1))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(len(self.shopify.created), 3)
        self.assertEqual(result["counts"]["ac2_insert_own_or_linked"], 3)
        self.assertNotIn("shopify_customer_created", result["counts"])

    def test_shopify_create_disabled_refuses_and_keeps_member_pending(self):
        self.initialise()
        self.ac2.add(sync.Ac2Member(sg(47), "Alpha Tester", sg(47)), log=True)
        result = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(result["counts"]["shopify_create_disabled"], 1)
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM ac2_pending").fetchone()[0], "PENDING")
        self.run_sync("ac2_to_shopify")
        self.assertEqual(len(self.shopify.created), 1)

    def test_member_without_phone_is_counted(self):
        self.initialise()
        self.ac2.add(sync.Ac2Member("TILL48", "Alpha Tester", None), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"]["ac2_member_without_phone"], 1)
        self.assertEqual(self.shopify.created, [])

    def test_uncertain_shopify_create_is_not_repeated(self):
        self.initialise()
        self.ac2.add(sync.Ac2Member(sg(49), "Alpha Tester", sg(49)), log=True)
        self.shopify.create_mode = "uncertain"
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"]["shopify_create_uncertain"], 1)
        self.shopify.create_mode = "ok"
        self.shopify.add(customer(49, tags=()))  # it had been created after all
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(self.links(), {gid(49): sg(49)})
        self.assertEqual(result["counts"]["uncertain_write_resolved_linked"], 1)

    def test_rejected_create_is_counted(self):
        self.initialise()
        self.ac2.add(sync.Ac2Member(sg(50), "Alpha Tester", sg(50)), log=True)
        self.shopify.create_mode = "rejected"
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"]["shopify_create_rejected"], 1)

    def test_unconfigured_changelog_column_does_nothing(self):
        result = self.run_sync("ac2_to_shopify", changelog_xml_column=None)
        self.assertEqual(result["counts"], {"changelog_column_unconfigured": 1})


class LoopPreventionTests(SyncTestCase):
    def test_member_created_in_ac2_never_returns_to_shopify(self):
        self.run_sync("ac2_to_shopify")  # watermark at the current end
        self.shopify.add(customer(60))
        result = self.run_sync()
        self.assertEqual(len(self.ac2.creates), 1)
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(result["counts"]["ac2_insert_own_or_linked"], 1)
        result = self.run_sync()
        self.assertEqual((len(self.ac2.creates), self.shopify.created), (1, []))

    def test_customer_created_in_shopify_never_returns_to_ac2(self):
        self.run_sync("ac2_to_shopify")
        self.ac2.add(sync.Ac2Member(sg(61), "Alpha Tester", sg(61)), log=True)
        self.run_sync()
        self.assertEqual(len(self.shopify.created), 1)
        created_gid = next(iter(self.links()))
        # Even if staff later tag the new customer as a member, it is linked.
        self.shopify.customers[created_gid] = replace(self.shopify.customers[created_gid], tags=("member-mg", "ac2-member"), registration_at=T0, updated_at=T0)
        result = self.run_sync()
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["shopify_already_linked"], 1)


class CredentialTests(unittest.TestCase):
    SECRET = "fake-secret-value-9f2c"

    def test_loader_never_reveals_the_secret(self):
        credential = sync.load_ac2_credential("XB-AC2-Automation", cred_read=lambda target: ("xbsync01", self.SECRET))
        self.assertNotIn(self.SECRET, repr(credential))
        self.assertNotIn("xbsync01", repr(credential))
        self.assertIn("c01", repr(credential))
        env = credential.child_environment({"PATH": "x", "AC2_PROBE_PASSWORD": "stale"})
        self.assertEqual(env["XB_AC2_PASSWORD_ENV_VAR"], sync.AC2_PASSWORD_ENV)
        self.assertEqual(env[sync.AC2_PASSWORD_ENV], self.SECRET)
        self.assertNotIn("AC2_PROBE_PASSWORD", env)

    def test_loader_failures_are_codes_only(self):
        cases = [
            (lambda target: None, "ac2_credential_missing"),
            (lambda target: (_ for _ in ()).throw(OSError(self.SECRET)), "ac2_credential_unreadable"),
            (lambda target: ("sa", self.SECRET), "ac2_credential_sa_refused"),
            (lambda target: ("SA", self.SECRET), "ac2_credential_sa_refused"),
            (lambda target: ("xbsync01", ""), "ac2_credential_secret_invalid"),
        ]
        for reader, code in cases:
            with self.assertRaises(dl.DirectLocalError) as caught:
                sync.load_ac2_credential("XB-AC2-Automation", cred_read=reader)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(self.SECRET, str(caught.exception))

    def test_check_credential_command_prints_masked_user_only(self):
        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-cli-"))
        try:
            path = temp / "config.json"
            path.write_text(json.dumps(config_value(ac2_credential_target="XB-AC2-Automation")), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(sync, "_win_cred_read", return_value=("xbsync01", self.SECRET)), redirect_stdout(out):
                code = sync.main(["check-credential", "--config", str(path)])
            self.assertEqual(code, 0)
            self.assertNotIn(self.SECRET, out.getvalue())
            self.assertNotIn("xbsync01", out.getvalue())
            self.assertEqual(json.loads(out.getvalue())["user_id"], "*****c01")
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_mask_last3(self):
        self.assertEqual(sync.mask_last3(sg(1)), "*******" + sg(1)[-3:])
        self.assertEqual(sync.mask_last3("xbsync01"), "*****c01")
        self.assertNotIn(local(1), sync.mask_last3(sg(1)))


def _graphql(data: dict | None = None, *, errors=None, status: int = 200) -> direct.HttpResponse:
    body = {"data": data} if errors is None else {"errors": errors}
    return direct.HttpResponse(status, {}, json.dumps(body).encode("utf-8"))


class SyncShopifyClientTests(unittest.TestCase):
    def client(self, responses, *, create_enabled):
        sent = []

        def transport(url, body, headers, timeout):
            sent.append(json.loads(body))
            return responses.pop(0)

        client = sync.SyncShopifyClient(
            create_enabled=create_enabled, shop_domain=DOMAIN, api_version="2026-07",
            token_provider=direct.StaticTokenProvider("shpat_synthetic_token_value_0001"), transport=transport, sleep=lambda s: None,
        )
        return client, sent

    def test_create_refused_while_disabled(self):
        client, sent = self.client([], create_enabled=False)
        with self.assertRaises(sync.WriteRefused):
            client.create_customer({"firstName": "Alpha"})
        with self.assertRaises(direct.ShopifyDirectError) as caught:
            client.execute(sync.CUSTOMER_CREATE_MUTATION, {"input": {}})
        self.assertEqual(caught.exception.code, "shopify_query_not_pinned")
        self.assertEqual(sent, [])

    def test_create_is_never_retried_after_server_error(self):
        client, sent = self.client([direct.HttpResponse(503, {}, b"")], create_enabled=True)
        with self.assertRaises(direct.ShopifyDirectError):
            client.create_customer({"firstName": "Alpha", "phone": plus(sg(70)), "tags": []})
        self.assertEqual(len(sent), 1)

    def test_reads_are_retried_and_create_returns_gid(self):
        responses = [
            direct.HttpResponse(503, {}, b""),
            _graphql({"customers": {"nodes": [{"id": gid(71), "firstName": "Alpha", "lastName": None}]}}),
            _graphql({"customerCreate": {"customer": {"id": gid(72)}, "userErrors": []}}),
            _graphql({"customerCreate": {"customer": None, "userErrors": [{"field": ["phone"], "message": "taken"}]}}),
        ]
        client, sent = self.client(responses, create_enabled=True)
        found = client.find_by_phone(plus(sg(71)))
        self.assertEqual([c.gid for c in found], [gid(71)])
        self.assertEqual(sent[0]["variables"], {"search": "phone:" + plus(sg(71))})
        self.assertEqual(client.create_customer({"firstName": "Alpha", "phone": plus(sg(72)), "tags": []}), gid(72))
        with self.assertRaises(sync.ShopifyCreateRejected):
            client.create_customer({"firstName": "Alpha", "phone": plus(sg(72)), "tags": []})
        with self.assertRaises(dl.DirectLocalError):
            client.create_customer({"firstName": "Alpha", "note": "x"})

    def test_session_scopes_follow_the_create_switch(self):
        session = {"shop": {"id": STORE, "myshopifyDomain": DOMAIN}, "currentAppInstallation": {"accessScopes": [{"handle": "read_customers"}, {"handle": "write_customers"}]}}
        client, _ = self.client([_graphql(session)], create_enabled=False)
        with self.assertRaises(direct.ShopifyDirectError) as caught:
            client.verify_session(store_gid=STORE)
        self.assertEqual(caught.exception.code, "shopify_token_scope_not_expected")
        client, _ = self.client([_graphql(session)], create_enabled=True)
        client.verify_session(store_gid=STORE)

    def test_poller_allowlist_is_unchanged(self):
        poller_client = direct.ShopifyDirectClient(shop_domain=DOMAIN, api_version="2026-07", token_provider=direct.StaticTokenProvider("shpat_synthetic_token_value_0001"), transport=lambda *a: None)
        for query in (sync.CUSTOMER_CREATE_MUTATION, sync.PHONE_LOOKUP_QUERY, sync.REGISTRATIONS_QUERY):
            with self.assertRaises(direct.ShopifyDirectError):
                poller_client.execute(query, {})


class BridgeClientTests(unittest.TestCase):
    def test_password_only_in_child_environment_and_lookup_parsing(self):
        captured = {}

        class Process:
            returncode = 0
            pid = 1

            def communicate(self, data, timeout=None):
                captured["stdin"] = data
                return (json.dumps({"members": [{"member_no": sg(80), "name": "Alpha", "mobile_phone": None, "email_address": None, "dob": None}]}).encode(), b"")

        def popen(args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return Process()

        env = sync.Ac2Credential("xbsync01", "fake-secret-value-9f2c").child_environment({"PATH": "x"})
        client = sync.SyncBridgeClient(["powershell.exe"], autocount_book=TEST_BOOK, claims=Path("."), env=env, popen=popen)
        members = client.lookup(sg(80), [])
        self.assertEqual(members[0].member_no, sg(80))
        self.assertEqual(captured["env"][sync.AC2_PASSWORD_ENV], "fake-secret-value-9f2c")
        self.assertNotIn("fake-secret-value-9f2c", " ".join(captured["args"]))
        self.assertNotIn(b"fake-secret", captured["stdin"])
        self.assertEqual(captured["args"][-2:], ["-TargetBook", TEST_BOOK])
        self.assertEqual(json.loads(captured["stdin"]), {"phone_digits": sg(80), "sg_local": local(80), "member_nos": []})


if __name__ == "__main__":
    unittest.main()
