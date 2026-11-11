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
import re
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
        "shopify_cutover_utc": dl.ts(CUTOVER),
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
    """The VM-observed insert shape (fact r5-2): one root ``<Member .../>``
    with attributes only; a NULL column is omitted; DOB as YYYY-MM-DDTHH:MM:SS."""

    from xml.sax.saxutils import quoteattr

    values = {"MemberNo": member_no, "Name": name, "MobilePhone": mobile, "EmailAddress": email, "DOB": None if dob is None else dob + "T00:00:00"}
    return "<Member " + " ".join(f"{key}={quoteattr(value)}" for key, value in values.items() if value is not None) + "/>"


class FakeShopify:
    def __init__(self):
        self.customers: dict[str, sync.ShopifyCustomer] = {}
        self.created: list[dict] = []
        self.searches: list[str] = []
        self.create_mode = "ok"
        self.next_gid = 900
        self.page_limit = None  # round 7c: initial-link pages
        self.unindexed: set[str] = set()  # round 8: not yet in the phone search

    def add(self, item: sync.ShopifyCustomer) -> sync.ShopifyCustomer:
        self.customers[item.gid] = item
        return item

    def registrations(self, search, max_pages, birthday):
        self.searches.append(search)
        if search.startswith("created_at:<"):
            # Round 7c initial link: customers made before the cutover, any
            # tag, updated_at ascending from the cursor.
            before = dl.parse_created_at(re.search(r"created_at:<'([^']+)'", search).group(1))
            since = re.search(r"updated_at:>='([^']+)'", search)
            floor = dl.parse_created_at(since.group(1)) if since else None
            found = sorted((c for c in self.customers.values() if c.created_at is not None and c.created_at < before and (floor is None or c.updated_at >= floor)),
                           key=lambda c: (c.updated_at, c.gid))
            if self.page_limit is not None and len(found) > self.page_limit:
                return found[:self.page_limit], False
            return found, True
        return [c for c in self.customers.values() if "member-mg" in c.tags], True

    def customer(self, customer_gid, birthday):
        return self.customers.get(customer_gid)

    def find_by_phone(self, phone_e164):
        digits = phone_e164[1:]
        return [c for c in self.customers.values() if sync.phone_member_no(c.phone)[0] == digits and c.gid not in self.unindexed]

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
        assert column == "Content"
        rows = sorted(row for row in self.changelog if after_id is None or row[0] > after_id)[:limit]
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
        # A day after the default sign-up (T0, 10:00 Singapore time): past the
        # next-morning cutoff, so a create is due. DeferredCreateTests set
        # earlier times for the waiting period.
        self.now = T0 + timedelta(days=1)
        self.ledger = sync.SyncLedger(self.temp, clock=lambda: self.now, check_custody=False)
        self.ledger.init_meta(autocount_book=TEST_BOOK, store_gid=STORE, shop_domain=DOMAIN)
        self.shopify = FakeShopify()
        self.ac2 = FakeAc2()
        # Round 7c: the one-off initial link has run (InitialLinkTests undo this).
        with self.ledger.tx() as conn:
            self.ledger.set_watermark(conn, "initial_link_completed_at", dl.ts(CUTOVER))

    def tearDown(self):
        self.ledger.close()
        shutil.rmtree(self.temp, ignore_errors=True)

    def engine(self, **overrides) -> sync.SyncEngine:
        # ChangeLog reads are off by default (Owner Q4); these tests turn them on.
        settings = {"ac2_create_enabled": True, "shopify_create_enabled": True, "changelog_xml_column": "Content"}
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

    def test_full_name_word_rule(self):
        self.assertEqual(sync.name_words("  Mary-Ann  LEE. "), frozenset({"mary", "ann", "lee"}))
        self.assertEqual(sync.name_words("Ｍａｒｙ"), frozenset({"mary"}))  # NFKC folds full-width letters
        self.assertEqual(sync.name_words("O'Brien, J."), frozenset({"o", "brien", "j"}))
        self.assertEqual(sync.name_words("Lee Lee"), frozenset({"lee"}))
        for empty in (None, "", "  ", "...", "-'.,"):
            self.assertEqual(sync.name_words(empty), frozenset())
        self.assertTrue(sync.full_name_matches("Ann Lee", "MARY ANN LEE"))
        self.assertTrue(sync.full_name_matches("MARY ANN LEE", "Ann Lee"))  # either side may be the shorter
        self.assertTrue(sync.full_name_matches("Lee Mary Ann", "MARY ANN LEE"))
        self.assertFalse(sync.full_name_matches("Mary Tan", "MARY ANN LEE"))
        self.assertFalse(sync.full_name_matches("Anne Lee", "ANN LEE"))  # exact words only
        self.assertFalse(sync.full_name_matches("Al", "Alpha Tester"))  # no prefixes
        self.assertFalse(sync.full_name_matches("..", ".."))
        self.assertTrue(sync.full_name_matches("Ann Lee", "ANN LEE"))  # 2 to 2
        for one_word in (("Lee", "MARY ANN LEE"), ("Mary", "MARY"), ("MARY ANN LEE", "Lee"), ("Mary.", "Mary")):
            self.assertFalse(sync.full_name_matches(*one_word))  # r3a: one word never links
        self.assertFalse(sync.full_name_matches("", "Alpha"))
        self.assertFalse(sync.full_name_matches(None, "Alpha"))
        self.assertEqual(sync.shopify_full_name("MARY ANN", "LEE"), "MARY ANN LEE")
        self.assertEqual(sync.shopify_full_name("MARY ANN LEE", None), "MARY ANN LEE")
        self.assertFalse(hasattr(sync, "first_name_matches"))

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
        # Go-live R1 (moved refusal): the production book is admitted only
        # with the sync's own credential (V9); any other name stays refused.
        # The GoLive closure matrix below pins every row.
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.parse_config(config_value(autocount_book=PROD_BOOK))
        self.assertEqual(caught.exception.code, "sync_config_ac2_credential_target_live_required")
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.parse_config(config_value(autocount_book="AED_OTHER"))
        self.assertEqual(caught.exception.code, "autocount_book_not_authorised")
        with self.assertRaises(dl.DirectLocalError):
            sync.SyncBridgeClient(["x"], autocount_book="AED_OTHER", claims=Path("."))

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
                # Go-live R2 (moved refusal): a v3 ledger may hold the
                # production book; any other name is refused by check_book
                # and by the meta CHECK.
                with self.assertRaises(dl.DirectLocalError):
                    ledger.init_meta(autocount_book="AED_OTHER", store_gid=STORE, shop_domain=DOMAIN)
                import sqlite3
                with self.assertRaises(sqlite3.IntegrityError):
                    ledger.conn.execute("INSERT INTO meta(id, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at) VALUES (1, 'x', ?, 's', 'd', ?, 't')", ("AED_OTHER", "0" * 64))
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
        self.assertEqual(payload, {"profile": "sync_p1", "member_type": "REGISTERED", "name": "Alpha Tester", "mobile_phone": sg(1), "email_address": "alpha@example.com", "register_date": "2026-10-21", "dob": "2000-07-01"})
        self.assertEqual(self.links(), {gid(1): sg(1)})
        self.assertEqual(result["counts"]["ac2_member_created"], 1)
        self.assert_no_pii(result)

    def test_same_phone_same_name_links(self):
        self.ac2.add(sync.Ac2Member("HOLD02", "TESTER ALPHA", local(2)))  # round 7: phone in MobilePhone, so the name rule decides
        self.shopify.add(customer(2))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(self.links(), {gid(2): "HOLD02"})
        self.assertEqual(result["counts"]["phone_and_name_match"], 1)

    def test_mobile_phone_and_embedded_digits_count_as_same_phone(self):
        # Q1: exact words only, so the full name (an initial "T" is not "Tester").
        self.ac2.add(sync.Ac2Member("OLD-17", "Alpha Tester", local(3)[:4] + " " + local(3)[4:]))
        self.shopify.add(customer(3))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(3): "OLD-17"})
        self.ac2.add(sync.Ac2Member("X" + local(4), "Alpha Tester", None))
        self.shopify.add(customer(4))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links()[gid(4)], "X" + local(4))
        self.assertEqual(self.ac2.creates, [])

    def test_same_phone_other_name_creates_suffix_member_no(self):
        self.ac2.add(sync.Ac2Member("HOLD05", "Charlie Holder", local(5)))  # round 7: phone in MobilePhone, so the name rule decides
        self.shopify.add(customer(5, first="Bravo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        member_no = self.ac2.creates[0][0]
        self.assertEqual(member_no, (sg(5) + "BRAVOTESTER")[:20])
        self.assertLessEqual(len(member_no), 20)
        self.assertEqual(result["counts"]["shared_phone_name_fallback"], 1)
        self.assertEqual(self.links(), {gid(5): member_no})

    def test_suffix_member_no_taken_adds_a_digit(self):
        base = (sg(6) + "BRAVOTESTER")[:20]
        self.ac2.add(sync.Ac2Member("HOLD06", "Charlie Holder", local(6)))  # round 7: phone in MobilePhone, so the name rule decides
        # Taken in a different case and with spaces: still taken.
        self.ac2.add(sync.Ac2Member(" " + base.lower() + " ", "Delta Other", None))
        self.shopify.add(customer(6, first="Bravo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates], [base[:19] + "2"])
        self.assertEqual(result["counts"]["shared_phone_suffix_digit"], 1)
        self.assertEqual(self.links(), {gid(6): base[:19] + "2"})

    def test_suffix_digits_two_to_nine_all_taken_skips(self):
        base = (sg(17) + "BRAVOTESTER")[:20]
        self.ac2.add(sync.Ac2Member("HOLD17", "Charlie Holder", local(17)))  # round 7: phone in MobilePhone, so the name rule decides
        for candidate in sync.suffix_candidates(base):
            self.ac2.add(sync.Ac2Member(candidate, "Delta Other", None))
        self.shopify.add(customer(17, first="Bravo", last="Tester"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["shared_phone_member_no_exhausted"], 1)

    def test_suffix_taken_by_our_own_link_adds_a_digit(self):
        base = (sg(18) + "BRAVOTESTER")[:20]
        self.ac2.add(sync.Ac2Member("HOLD18", "Charlie Holder", local(18)))  # round 7: phone in MobilePhone, so the name rule decides
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
        # Round 7: the exact MemberNo (trimmed, any case) links by time.
        self.ac2.add(sync.Ac2Member(" " + sg(9) + "  ", None, None, created_at=T0 + timedelta(minutes=1)))
        self.shopify.add(replace(customer(9), created_at=T0))
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
        # Round 7: the record whose MemberNo is the phone decides, by time.
        self.ac2.add(sync.Ac2Member(sg(12), "Alpha Tester", None, created_at=T0))
        self.ac2.add(sync.Ac2Member("A" + local(12), "Alpha Tester", sg(12)))
        self.shopify.add(replace(customer(12), created_at=T0))
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
        self.ac2.add(sync.Ac2Member("TILL51", "Alpha Tester", sg(51)), log=True)  # round 8: a till number, so the name rule decides
        self.run_sync("ac2_to_shopify")
        created = self.shopify.created[0]
        self.assertNotIn("phone", created)
        self.assertNotIn("email", created)
        self.assertEqual(created["firstName"], "Alpha Tester")

    def test_separate_customer_waits_while_shopify_create_is_off(self):
        self.initialise()
        self.shopify.add(customer(52, first="Bravo", tags=()))
        self.ac2.add(sync.Ac2Member("TILL52", "Alpha Tester", sg(52)), log=True)  # round 8: a till number, so the name rule decides
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
        self.assertEqual(result["counts"]["changelog_row_replayed"], 3)  # seen IDs
        self.assertNotIn("shopify_customer_created", result["counts"])
        # Even without the seen IDs, the ledger links stop any second create.
        with self.ledger.tx() as conn:
            conn.execute("DELETE FROM changelog_seen")
            self.ledger.set_watermark(conn, "ac2_changelog_id", str(ids[0] - 1))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(len(self.shopify.created), 3)
        self.assertEqual(result["counts"]["ac2_insert_linked_or_own_write"], 3)

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
        self.assertEqual(result["counts"]["ac2_insert_linked_or_own_write"], 1)
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
        credential = sync.load_ac2_credential("XB-AC2-MemberSync", cred_read=lambda target: ("fakeuser01", self.SECRET))
        self.assertNotIn(self.SECRET, repr(credential))
        self.assertNotIn("fakeuser01", repr(credential))
        self.assertIn("r01", repr(credential))
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
            (lambda target: ("fakeuser01", ""), "ac2_credential_secret_invalid"),
        ]
        for reader, code in cases:
            with self.assertRaises(dl.DirectLocalError) as caught:
                sync.load_ac2_credential("XB-AC2-MemberSync", cred_read=reader)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(self.SECRET, str(caught.exception))

    def test_check_credential_command_prints_masked_user_only(self):
        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-cli-"))
        try:
            path = temp / "config.json"
            path.write_text(json.dumps(config_value(ac2_credential_target=sync.TEST_CREDENTIAL_TARGET)), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(sync, "_win_cred_read", return_value=("fakeuser01", self.SECRET)), redirect_stdout(out):
                code = sync.main(["check-credential", "--config", str(path)])
            self.assertEqual(code, 0)
            self.assertNotIn(self.SECRET, out.getvalue())
            self.assertNotIn("fakeuser01", out.getvalue())
            self.assertEqual(json.loads(out.getvalue())["user_id"], "*******r01")
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_mask_last3(self):
        self.assertEqual(sync.mask_last3(sg(1)), "*******" + sg(1)[-3:])
        self.assertEqual(sync.mask_last3("fakeuser01"), "*******r01")
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
            _graphql({"customers": {"nodes": [{"id": gid(71), "firstName": "Alpha", "lastName": None, "defaultPhoneNumber": {"phoneNumber": plus(sg(71))}}]}}),
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

        env = sync.Ac2Credential("fakeuser01", "fake-secret-value-9f2c").child_environment({"PATH": "x"})
        client = sync.SyncBridgeClient(["powershell.exe"], autocount_book=TEST_BOOK, claims=Path("."), env=env, popen=popen)
        members = client.lookup(sg(80), [])
        self.assertEqual(members[0].member_no, sg(80))
        self.assertEqual(captured["env"][sync.AC2_PASSWORD_ENV], "fake-secret-value-9f2c")
        self.assertNotIn("fake-secret-value-9f2c", " ".join(captured["args"]))
        self.assertNotIn(b"fake-secret", captured["stdin"])
        self.assertEqual(captured["args"][-2:], ["-TargetBook", TEST_BOOK])
        self.assertEqual(json.loads(captured["stdin"]), {"phone_digits": sg(80), "sg_local": local(80), "member_nos": []})


# A fake 9-digit number without "+" that is not a Singapore number (L1).
NINE_DIGITS = "9" + "123" + "45" + "678"


class ScriptedPhoneSearch:
    """Shopify's ``phone:`` search answered by a scripted transport that
    returns raw nodes unfiltered, through the real ``SyncShopifyClient``."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.searches = []

        def transport(url, body, headers, timeout):
            self.searches.append(json.loads(body)["variables"]["search"])
            return _graphql({"customers": {"nodes": list(self.nodes)}})

        self.client = sync.SyncShopifyClient(
            create_enabled=False, shop_domain=DOMAIN, api_version="2026-07",
            token_provider=direct.StaticTokenProvider("shpat_synthetic_token_value_0001"), transport=transport, sleep=lambda s: None,
        )


def phone_node(n: int, phone: str | None, first: str = "Alpha", last: str | None = "Tester") -> dict:
    return {"id": gid(n), "firstName": first, "lastName": last, "defaultPhoneNumber": None if phone is None else {"phoneNumber": phone}}


class B1PhoneLookupVerifiesPhoneTests(SyncTestCase):
    """B1: a loose ``phone:`` search must never decide a link by itself."""

    def use_search(self, nodes):
        search = ScriptedPhoneSearch(nodes)
        self.shopify.find_by_phone = search.client.find_by_phone
        self.run_sync("ac2_to_shopify")  # watermark
        return search

    def test_node_with_other_phone_never_links_and_phone_counts_as_free(self):
        self.use_search([phone_node(90, plus(sg(91)))])
        self.ac2.add(sync.Ac2Member(sg(90), "Alpha Tester", sg(90)), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertNotIn(gid(90), self.links())
        self.assertEqual(self.shopify.created[0]["phone"], plus(sg(90)))  # phone free: created with it
        self.assertEqual(result["counts"]["shopify_customer_created"], 1)

    def test_one_match_plus_one_other_phone_is_single(self):
        self.use_search([phone_node(92, plus(sg(92))), phone_node(93, plus(sg(93)))])
        self.ac2.add(sync.Ac2Member("TILL92", "Alpha Tester", sg(92)), log=True)  # round 8: a till number, so the name rule decides
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.links(), {gid(92): "TILL92"})
        self.assertNotIn("shopify_phone_on_multiple_customers", result["counts"])
        self.assertEqual(self.shopify.created, [])

    def test_node_without_phone_is_ignored(self):
        self.use_search([phone_node(94, None)])
        self.ac2.add(sync.Ac2Member(sg(94), "Alpha Tester", sg(94)), log=True)
        self.run_sync("ac2_to_shopify")
        self.assertNotIn(gid(94), self.links())
        self.assertEqual(self.shopify.created[0]["phone"], plus(sg(94)))

    def test_non_sg_number_without_plus_compares_as_typed(self):
        search = self.use_search([phone_node(95, "+" + NINE_DIGITS), phone_node(96, "+" + "65" + NINE_DIGITS)])
        self.ac2.add(sync.Ac2Member("TILL95", "Alpha Tester", NINE_DIGITS), log=True)
        self.run_sync("ac2_to_shopify")
        self.assertEqual(self.links(), {gid(95): "TILL95"})
        self.assertEqual(search.searches[-1], "phone:+" + NINE_DIGITS)


class B2UncertainCreateIdentityTests(SyncTestCase):
    """B2: an unclear AC2 create links only when the member found under that
    MemberNo is the person the write was for."""

    def uncertain_create(self, n: int, *, found_name: str | None) -> None:
        self.ac2.create_result = ("WRITE_OUTCOME_UNCERTAIN", "writer_failed_after_claim")
        original = self.ac2.create_member

        def taken_by(member_no, payload, *, write_id):
            self.ac2.creates.append((member_no, dict(payload)))
            if found_name is not None:  # who holds the number afterwards
                self.ac2.add(sync.Ac2Member(member_no, found_name, payload["mobile_phone"] if found_name == payload["name"] else None))
            return self.ac2.create_result

        self.ac2.create_member = taken_by
        self.shopify.add(customer(n))
        self.run_sync("shopify_to_ac2")
        self.ac2.create_member = original
        self.ac2.create_result = ("CREATED_VERIFIED", None)

    def test_number_held_by_another_person_is_never_linked(self):
        self.uncertain_create(36, found_name="Kilo Other")
        result = self.run_sync("shopify_to_ac2")
        self.assertNotIn(gid(36), self.links())
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "UNCERTAIN")
        self.assertEqual(result["counts"]["uncertain_write_identity_mismatch"], 1)
        self.assertEqual(len(self.ac2.creates), 1)

    def test_same_person_found_links(self):
        self.uncertain_create(37, found_name="Alpha Tester")
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(37): sg(37)})
        self.assertEqual(result["counts"]["uncertain_write_resolved_linked"], 1)

    def test_old_intent_without_digest_is_never_linked(self):
        self.ac2.add(sync.Ac2Member(sg(38), "Alpha Tester", sg(38)))
        now = dl.ts(self.now)
        with self.ledger.tx() as conn:
            conn.execute(
                "INSERT INTO writes(write_id, direction, customer_gid, member_no, member_no_key, state, code, created_at, updated_at) VALUES (?, 'shopify_to_ac2', ?, ?, ?, 'UNCERTAIN', 'old', ?, ?)",
                ("write-" + "f" * 32, gid(38), sg(38), sync.member_no_key(sg(38)), now, now),
            )
        result = self.run_sync("shopify_to_ac2")
        self.assertNotIn(gid(38), self.links())
        self.assertEqual(result["counts"]["uncertain_write_identity_unverifiable"], 1)

    def test_ledger_holds_only_the_digest(self):
        self.uncertain_create(39, found_name=None)
        dump = "\n".join(self.ledger.conn.iterdump())
        self.assertNotIn("Alpha", dump)
        self.assertNotIn("alpha tester", dump)
        self.assertNotIn(local(39), dump.replace(sg(39), ""))  # only the MemberNo itself carries the digits
        self.assertRegex(self.ledger.conn.execute("SELECT intent_digest FROM writes").fetchone()[0], r"^sha256:[0-9a-f]{64}$")


# Owner decision Q1 (2026-10-21, with addendum r3a): on the same phone, link
# when the shorter full name has at least 2 words and every one of them is a
# word of the longer one; a name of 0 or 1 words never links, even when
# identical. Every example runs on one shared fake phone. Non-Latin names are
# built from escapes (ASCII source).
NAME_PHONE = 81
NAME_TILL = "TILL81"
CJK_NAME = "陈美玲"  # written without spaces: one word
CJK_TWO_WORDS = "陈 美玲"  # the same name written with a space: two words

# (label, Shopify firstName, Shopify lastName, AC2 Name, links)
SHOPIFY_TO_AC2_NAMES = (
    ("ann_lee", "Ann", "Lee", "MARY ANN LEE", True),
    ("mary_lee", "Mary", "Lee", "MARY ANN LEE", True),
    ("mary_ann_lee", "Mary Ann", "Lee", "MARY ANN LEE", True),
    ("mary_tan", "Mary", "Tan", "MARY ANN LEE", False),  # re-added: first name only linked it
    ("john_lim", "John", "Lim", "MARY ANN LEE", False),
    ("anne_lee", "Anne", "Lee", "ANN LEE", False),
    ("word_order", "Lee", "Mary Ann", "MARY ANN LEE", True),
    ("punctuation_and_spacing", "Mary-Ann ", " Lee.", "MARY ANN LEE", True),
    ("one_word_surname", "Lee", None, "MARY ANN LEE", False),  # r3a: one word never links
    ("one_word_first_name", "Mary", None, "MARY ANN LEE", False),
    ("one_word_identical", "Mary", None, "MARY", False),
    ("one_word_ac2_side", "Mary", "Lee", "LEE", False),
    ("punctuation_only_both_sides", "..", None, "..", False),
    ("non_latin_identical_one_word", CJK_NAME, None, CJK_NAME, False),
    ("non_latin_identical_two_words", CJK_TWO_WORDS, None, CJK_TWO_WORDS, True),
    ("non_latin_spaced_vs_unspaced", CJK_NAME[0], CJK_NAME[1:], CJK_NAME, False),
    ("non_latin_vs_latin", CJK_NAME, None, "MARY ANN LEE", False),
    ("latin_vs_non_latin", "Mary", "Lee", CJK_NAME, False),
)

# (label, AC2 Name, Shopify firstName, Shopify lastName, links). Shopify holds
# the name split, or whole in firstName as AC2 -> Shopify creates write it.
AC2_TO_SHOPIFY_NAMES = (
    ("ann_lee_split", "Ann Lee", "MARY ANN", "LEE", True),  # re-added
    ("ann_lee_whole", "Ann Lee", "MARY ANN LEE", None, True),  # re-added
    ("mary_lee_split", "Mary Lee", "MARY ANN", "LEE", True),  # re-added
    ("mary_lee_whole", "Mary Lee", "MARY ANN LEE", None, True),  # re-added
    ("mary_ann_lee_split", "Mary Ann Lee", "MARY ANN", "LEE", True),
    ("mary_ann_lee_whole", "Mary Ann Lee", "MARY ANN LEE", None, True),
    ("mary_tan_split", "Mary Tan", "MARY ANN", "LEE", False),
    ("mary_tan_whole", "Mary Tan", "MARY ANN LEE", None, False),
    ("john_lim_split", "John Lim", "MARY ANN", "LEE", False),
    ("john_lim_whole", "John Lim", "MARY ANN LEE", None, False),
    ("anne_lee_split", "Anne Lee", "ANN", "LEE", False),
    ("anne_lee_whole", "Anne Lee", "ANN LEE", None, False),
    ("word_order_split", "Lee Mary Ann", "MARY ANN", "LEE", True),
    ("word_order_whole", "Lee Mary Ann", "MARY ANN LEE", None, True),
    ("punctuation_and_spacing_split", "Mary-Ann  Lee.", "MARY ANN", "LEE", True),
    ("punctuation_and_spacing_whole", "Mary-Ann  Lee.", "MARY ANN LEE", None, True),
    ("one_word_surname", "MARY ANN LEE", "Lee", None, False),  # r3a: one word never links
    ("one_word_first_name", "MARY ANN LEE", "Mary", None, False),
    ("one_word_identical", "MARY", "Mary", None, False),
    ("one_word_ac2_side", "Lee", "MARY ANN", "LEE", False),
    ("punctuation_only_both_sides", "..", "..", None, False),
    ("non_latin_identical_one_word", CJK_NAME, CJK_NAME, None, False),
    ("non_latin_identical_two_words", CJK_TWO_WORDS, CJK_TWO_WORDS, None, True),
    ("non_latin_vs_latin", CJK_NAME, "MARY ANN", "LEE", False),
)


class FullNameRuleTests(unittest.TestCase):
    """Q1 examples in both directions. A non-link must take that direction's
    existing no-link path: Shopify -> AC2 the shared-phone MemberNo (phone +
    letters, or phone + digit 2-9 without Latin letters); AC2 -> Shopify a
    separate customer without the phone. Round 7: Shopify -> AC2 uses the name
    rule only for a record holding the phone under another MemberNo."""

    def fresh(self):
        case = SyncTestCase("run_sync")
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def check_shopify_to_ac2(self, first, last, ac2_name, links):
        case = self.fresh()
        phone = sg(NAME_PHONE)
        case.ac2.add(sync.Ac2Member(NAME_TILL, ac2_name, phone))
        case.shopify.add(customer(NAME_PHONE, first=first, last=last))
        result = case.run_sync("shopify_to_ac2")
        if links:
            self.assertEqual((case.links(), case.ac2.creates), ({gid(NAME_PHONE): NAME_TILL}, []))
            return
        letters = sync.fallback_member_no(phone, first, last)
        expected, code = (letters, "shared_phone_name_fallback") if letters else (phone + "2", "shared_phone_digit_fallback")
        self.assertEqual([m for m, _ in case.ac2.creates], [expected])
        self.assertEqual(case.links(), {gid(NAME_PHONE): expected})  # the new member, never the phone's holder
        self.assertEqual(result["counts"][code], 1)

    def check_ac2_to_shopify(self, ac2_name, first, last, links):
        case = self.fresh()
        case.run_sync("ac2_to_shopify")  # watermark
        case.shopify.add(customer(NAME_PHONE, first=first, last=last, tags=()))
        case.ac2.add(sync.Ac2Member(NAME_TILL, ac2_name, sg(NAME_PHONE)), log=True)
        result = case.run_sync("ac2_to_shopify")
        if links:
            self.assertEqual((case.links(), case.shopify.created), ({gid(NAME_PHONE): NAME_TILL}, []))
            return
        self.assertNotIn(gid(NAME_PHONE), case.links())
        self.assertEqual(len(case.shopify.created), 1)
        self.assertNotIn("phone", case.shopify.created[0])
        self.assertEqual(result["counts"]["shopify_phone_owned_by_other_name"], 1)
        self.assertEqual(result["counts"]["shopify_separate_customer_created_without_phone"], 1)

    def test_ac2_to_shopify_no_link_only_counts_while_shopify_create_is_off(self):
        case = self.fresh()
        case.run_sync("ac2_to_shopify")
        case.shopify.add(customer(NAME_PHONE, first="MARY ANN", last="LEE", tags=()))
        case.ac2.add(sync.Ac2Member(NAME_TILL, "Mary Tan", sg(NAME_PHONE)), log=True)
        result = case.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertEqual((case.links(), case.shopify.created), ({}, []))
        self.assertEqual((result["counts"]["shopify_phone_owned_by_other_name"], result["counts"]["shopify_create_disabled"]), (1, 1))
        self.assertEqual(case.ledger.conn.execute("SELECT state FROM ac2_pending").fetchone()[0], "PENDING")

    def test_empty_shopify_name_never_links_and_is_still_skipped(self):
        case = self.fresh()
        case.ac2.add(sync.Ac2Member(NAME_TILL, "MARY ANN LEE", sg(NAME_PHONE)))
        case.shopify.add(customer(NAME_PHONE, first="", last=" "))
        result = case.run_sync("shopify_to_ac2")
        self.assertEqual((case.links(), case.ac2.creates), ({}, []))
        self.assertEqual(result["counts"]["shopify_name_missing"], 1)

    def test_empty_ac2_name_never_links(self):
        case = self.fresh()
        case.run_sync("ac2_to_shopify")
        case.shopify.add(customer(NAME_PHONE, first="MARY ANN", last="LEE", tags=()))
        case.ac2.add(sync.Ac2Member(NAME_TILL, "", sg(NAME_PHONE)), log=True)
        result = case.run_sync("ac2_to_shopify")
        self.assertEqual((case.links(), case.shopify.created), ({}, []))
        self.assertEqual(result["counts"]["ac2_member_without_name"], 1)

    def test_uncertain_shopify_create_resolves_with_the_word_rule(self):
        case = self.fresh()
        case.run_sync("ac2_to_shopify")
        case.ac2.add(sync.Ac2Member(sg(NAME_PHONE), "Mary Ann Lee", sg(NAME_PHONE)), log=True)
        case.shopify.create_mode = "uncertain"
        case.run_sync("ac2_to_shopify")
        case.shopify.create_mode = "ok"
        case.shopify.add(customer(NAME_PHONE, first="Mary-Ann", last="Lee", tags=()))  # it had been created after all
        result = case.run_sync("ac2_to_shopify")
        self.assertEqual((case.links(), case.shopify.created), ({gid(NAME_PHONE): sg(NAME_PHONE)}, []))
        self.assertEqual(result["counts"]["uncertain_write_resolved_linked"], 1)


def _name_test(method, args):
    return lambda self: getattr(self, method)(*args)


# One test per example and direction, so each result is reported by name.
for _label, _first, _last, _ac2_name, _links in SHOPIFY_TO_AC2_NAMES:
    setattr(FullNameRuleTests, f"test_shopify_to_ac2_{_label}", _name_test("check_shopify_to_ac2", (_first, _last, _ac2_name, _links)))
for _label, _ac2_name, _first, _last, _links in AC2_TO_SHOPIFY_NAMES:
    setattr(FullNameRuleTests, f"test_ac2_to_shopify_{_label}", _name_test("check_ac2_to_shopify", (_ac2_name, _first, _last, _links)))


class B2IdentityNormalisationTests(SyncTestCase):
    """B2 stays an exact identity check, with the name normalised by the same
    helper as the word rule (case, spacing and punctuation)."""

    def test_identity_digest_uses_the_shared_normalisation_exactly(self):
        digest = self.ledger.identity_digest
        self.assertEqual(digest("Mary-Ann  Lee.", sg(85)), digest("MARY ANN LEE", sg(85)))
        self.assertNotEqual(digest("Ann Lee", sg(85)), digest("Mary Ann Lee", sg(85)))  # never a subset
        self.assertNotEqual(digest("Lee Mary Ann", sg(85)), digest("Mary Ann Lee", sg(85)))  # the exact word sequence
        self.assertNotEqual(digest("Mary Ann Lee", sg(85)), digest("Mary Ann Lee", sg(86)))

    def uncertain_create(self, holder_name: str) -> dict:
        self.ac2.create_result = ("WRITE_OUTCOME_UNCERTAIN", "writer_failed_after_claim")
        original = self.ac2.create_member

        def lands_as(member_no, payload, *, write_id):
            self.ac2.creates.append((member_no, dict(payload)))
            self.ac2.add(sync.Ac2Member(member_no, holder_name, payload["mobile_phone"]))
            return self.ac2.create_result

        self.ac2.create_member = lands_as
        self.shopify.add(customer(85, first="Mary-Ann", last="Lee"))
        self.run_sync("shopify_to_ac2")
        self.ac2.create_member = original
        return self.run_sync("shopify_to_ac2")

    def test_same_words_link(self):
        result = self.uncertain_create("MARY ANN LEE")
        self.assertEqual(self.links(), {gid(85): sg(85)})
        self.assertEqual(result["counts"]["uncertain_write_resolved_linked"], 1)

    def test_a_subset_name_is_not_the_written_identity(self):
        result = self.uncertain_create("Ann Lee")
        self.assertEqual(self.links(), {})
        self.assertEqual(result["counts"]["uncertain_write_identity_mismatch"], 1)


class Q1NonLatinSharedPhoneTests(SyncTestCase):
    """Owner round 2 Q1: shared phone, no Latin letters -> phone + 2-9."""

    def register(self, n: int) -> dict:
        self.ac2.add(sync.Ac2Member(f"HOLD{n}", "Charlie Holder", local(n)))  # round 7: phone in MobilePhone, so the name rule decides
        self.shopify.add(customer(n, first="陈", last="美玲"))
        return self.run_sync("shopify_to_ac2")

    def test_non_latin_name_gets_phone_plus_two(self):
        result = self.register(53)
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(53) + "2"])
        self.assertEqual(result["counts"]["shared_phone_digit_fallback"], 1)

    def test_two_taken_in_ac2_gives_three(self):
        self.ac2.add(sync.Ac2Member(sg(54) + "2", "Delta Other", None))
        self.register(54)
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(54) + "3"])

    def test_three_taken_in_the_ledger_gives_the_next_free_digit(self):
        self.ac2.add(sync.Ac2Member(sg(55) + "2", "Delta Other", None))
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(98), sg(55) + "3", "matched", "test")
        self.register(55)
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(55) + "4"])

    def test_two_to_nine_all_taken_skips_and_counts(self):
        for digit in range(2, 10):
            self.ac2.add(sync.Ac2Member(sg(56) + str(digit), "Delta Other", None))
        result = self.register(56)
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["shared_phone_member_no_exhausted"], 1)


class ChangeLogCursorTests(SyncTestCase):
    def initialise(self):
        self.run_sync("ac2_to_shopify")

    def member_xml(self, n: int) -> str:
        return changelog_xml(sg(n), f"Member {n}", sg(n))

    def pending_numbers(self) -> list[str]:
        return [row[0] for row in self.ledger.conn.execute("SELECT member_no FROM ac2_pending ORDER BY changelog_id")]

    def test_unparseable_row_stops_and_keeps_the_watermark(self):
        self.initialise()  # empty ChangeLog: watermark 0
        self.ac2.changelog += [(1, self.member_xml(57)), (2, "<Member Name=" + chr(34) + "x"), (3, self.member_xml(58))]
        result = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertEqual(self.ledger.watermark("ac2_changelog_id"), "1")
        self.assertEqual(self.pending_numbers(), [sg(57)])  # row 3 not processed in this run
        self.assertEqual((result["status"], result["alerts"]), ("completed_with_alerts", ["changelog_row_unparsed"]))
        self.assertEqual(result["counts"]["changelog_row_unparsed"], 1)

    def test_numbering_reset_is_refused_with_an_alert(self):
        self.ac2.add(sync.Ac2Member(sg(59), "Member", sg(59)), log=True)
        self.initialise()
        watermark = self.ledger.watermark("ac2_changelog_id")
        self.ac2.changelog = [(5, self.member_xml(60))]  # IDs went backwards
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["alerts"], ["changelog_id_reset_detected"])
        self.assertEqual(self.ledger.watermark("ac2_changelog_id"), watermark)
        self.assertEqual(self.pending_numbers(), [])

    def test_overlap_reread_of_processed_rows_is_a_no_op(self):
        self.initialise()
        self.ac2.add(sync.Ac2Member(sg(61), "Member", sg(61)), log=True)
        self.run_sync("ac2_to_shopify")
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"], {})
        self.assertEqual(len(self.shopify.created), 1)

    def test_late_lower_id_inside_the_overlap_is_processed(self):
        self.initialise()
        self.ac2.add(sync.Ac2Member(sg(62), "Member", sg(62)), log=True)
        self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        watermark = int(self.ledger.watermark("ac2_changelog_id"))
        self.ac2.changelog.append((watermark - 3, self.member_xml(63)))  # committed late
        result = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertEqual(result["counts"]["changelog_late_row_processed"], 1)
        self.assertEqual(self.pending_numbers(), [sg(63), sg(62)])
        self.assertEqual(self.ledger.watermark("ac2_changelog_id"), str(watermark))

    def test_overlap_zero_behaves_as_before(self):
        self.run_sync("ac2_to_shopify", changelog_overlap=0)
        self.ac2.add(sync.Ac2Member(sg(64), "Member", sg(64)), log=True)
        self.run_sync("ac2_to_shopify", changelog_overlap=0, shopify_create_enabled=False)
        watermark = int(self.ledger.watermark("ac2_changelog_id"))
        self.ac2.changelog.append((watermark - 3, self.member_xml(65)))
        result = self.run_sync("ac2_to_shopify", changelog_overlap=0, shopify_create_enabled=False)
        self.assertNotIn("changelog_late_row_processed", result["counts"])
        self.assertEqual(self.pending_numbers(), [sg(64)])

    def test_rows_visible_at_first_run_are_history(self):
        for n in (66, 67):
            self.ac2.add(sync.Ac2Member(sg(n), "Member", sg(n)), log=True)
        self.initialise()
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM changelog_seen").fetchone()[0], 2)
        self.run_sync("ac2_to_shopify")
        self.assertEqual((self.pending_numbers(), self.shopify.created), ([], []))


class InsertXmlShapeTests(unittest.TestCase):
    """VM fact r5-2: attribute-only ``<Member .../>``; NULL columns omitted."""

    def test_all_five_attributes(self):
        member = sync.parse_changelog_member(changelog_xml(sg(68), "Alpha Tester", sg(68), "alpha@example.com", "2000-05-01"))
        self.assertEqual((member.member_no, member.name, member.mobile_phone, member.email_address, member.dob), (sg(68), "Alpha Tester", sg(68), "alpha@example.com", "2000-05-01"))

    def test_missing_attributes_are_null(self):
        for missing in ("MobilePhone", "EmailAddress", "DOB"):
            with self.subTest(missing=missing):
                fields = {"mobile": sg(69), "email": "alpha@example.com", "dob": "2000-05-01"}
                fields[{"MobilePhone": "mobile", "EmailAddress": "email", "DOB": "dob"}[missing]] = None
                member = sync.parse_changelog_member(changelog_xml(sg(69), "Alpha Tester", **fields))
                values = {"MobilePhone": member.mobile_phone, "EmailAddress": member.email_address, "DOB": member.dob}
                self.assertIsNone(values[missing])
                self.assertEqual(sum(v is not None for v in values.values()), 2)

    def test_dob_datetime_form(self):
        xml = '<Member MemberNo="TILL1" Name="Alpha" DOB="2000-11-01T00:00:00"/>'
        self.assertEqual(sync.parse_changelog_member(xml).dob, "2000-11-01")


class Ac2PhoneSourceTests(SyncTestCase):
    """VM fact r5-3: MobilePhone is usually missing; the MemberNo is then the
    phone when it is one under ``phone_member_no``."""

    def test_phone_source_shapes(self):
        cases = [
            (sync.Ac2Member(sg(70), "Alpha", None), sg(70)),  # SG phone MemberNo
            (sync.Ac2Member((sg(70) + "ALPHATESTER")[:20], "Alpha", None), None),  # phone + initials
            (sync.Ac2Member(sg(70) + "2", "Alpha", None), None),  # phone + digit (11 digits, 65...)
            (sync.Ac2Member("M000201", "Alpha", None), None),  # generator number
            (sync.Ac2Member("TILL70", "Alpha", local(71)), sg(71)),  # MobilePhone present
            (sync.Ac2Member(sg(72), "Alpha", local(73)), sg(73)),  # MobilePhone wins
        ]
        for member, expected in cases:
            with self.subTest(case=cases.index((member, expected))):
                self.assertEqual(sync._ac2_phone(member), expected)

    def test_member_no_phone_used_when_mobile_missing(self):
        self.run_sync("ac2_to_shopify")
        self.ac2.add(sync.Ac2Member(sg(74), "Alpha Tester", None), log=True)
        self.run_sync("ac2_to_shopify")
        self.assertEqual(self.shopify.created[0]["phone"], plus(sg(74)))


class Q4PhonelessUncertainTests(SyncTestCase):
    def test_lost_reply_on_phoneless_create_is_never_resent(self):
        self.run_sync("ac2_to_shopify")
        self.shopify.add(customer(75, first="Bravo", tags=()))
        self.ac2.add(sync.Ac2Member("TILL75", "Alpha Tester", local(75)), log=True)
        self.shopify.create_mode = "uncertain"
        self.run_sync("ac2_to_shopify")
        self.shopify.create_mode = "ok"
        for _ in range(2):
            result = self.run_sync("ac2_to_shopify")
            self.assertEqual(result["counts"].get("uncertain_write_unresolved"), 1)
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(self.ledger.conn.execute("SELECT state, code FROM ac2_pending").fetchone()[:], ("SKIPPED", "shopify_create_uncertain"))


class LedgerMigrationTests(unittest.TestCase):
    def test_v1_ledger_gets_the_additive_v2_columns(self):
        import sqlite3

        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-v1-"))
        try:
            v1 = sync.LEDGER_SCHEMA.replace(", intent_digest TEXT", "").replace("CREATE TABLE changelog_seen(id INTEGER PRIMARY KEY, seen_at TEXT NOT NULL);\n", "")
            v1 = v1.replace("created_at, intent_digest ON writes", "created_at ON writes")
            conn = sqlite3.connect(temp / sync.LEDGER_FILE)
            conn.executescript(v1)
            conn.execute("INSERT INTO writes(write_id, direction, member_no, member_no_key, state, created_at, updated_at) VALUES ('write-1', 'shopify_to_ac2', 'M1', 'm1', 'UNCERTAIN', 't', 't')")
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            conn.close()
            ledger = sync.SyncLedger(temp, check_custody=False)
            try:
                self.assertEqual(ledger.conn.execute("PRAGMA user_version").fetchone()[0], sync.SCHEMA_VERSION)
                self.assertIsNone(ledger.conn.execute("SELECT intent_digest FROM writes").fetchone()[0])
                ledger.conn.execute("SELECT COUNT(*) FROM changelog_seen").fetchone()
                with self.assertRaises(sqlite3.IntegrityError):
                    ledger.conn.execute("UPDATE writes SET intent_digest = 'x'")
            finally:
                ledger.close()
        finally:
            shutil.rmtree(temp, ignore_errors=True)


class ChangeLogConfigTests(unittest.TestCase):
    def test_content_column_off_by_default_and_overlap_default(self):
        value = config_value()
        self.assertNotIn("changelog_xml_column", value)
        config = sync.parse_config(value)
        self.assertEqual((config.changelog_xml_column, config.changelog_overlap), (None, 50))  # Owner Q4: null
        self.assertEqual(sync.parse_config(config_value(changelog_xml_column="Content")).changelog_xml_column, "Content")
        with self.assertRaises(dl.DirectLocalError):
            sync.parse_config(config_value(changelog_overlap=200, changelog_batch=200))


class L1PhoneWithoutPlusTests(unittest.TestCase):
    def test_member_no_is_the_digits_as_typed(self):
        self.assertEqual(sync.phone_member_no(NINE_DIGITS), (NINE_DIGITS, "ok"))
        self.assertEqual(sync.phone_member_no(sg(83)), (sg(83), "ok"))  # 10 digits starting 65
        self.assertEqual(sync.phone_member_no(local(84)), (sg(84), "ok"))  # SG 8 digits still get 65
        self.assertEqual(sync.phone_member_no("+" + NINE_DIGITS), (NINE_DIGITS, "ok"))  # "+" unchanged

    def test_invalid_without_plus_is_unchanged(self):
        for raw in ("0" + NINE_DIGITS[1:], "65" + "1" + "2345678", "1" + "2345678", "12" + "34567", "1" * 16):
            with self.subTest(length=len(raw)):
                self.assertEqual(sync.phone_member_no(raw), (None, "invalid"))


# A 9-digit all-digit MemberNo shaped like a generator number (not a phone).
GENERATOR_LIKE = "1" + "0000" + "0123"


class Q3MemberNoPhoneFallbackTests(SyncTestCase):
    """Owner Q3: without MobilePhone, the MemberNo gives a phone only when it
    is a Singapore number (65 + 8 digits starting 3/6/8/9, or those 8 digits)."""

    def test_member_no_shapes(self):
        cases = [
            ("sg_65_plus_8", sync.Ac2Member(sg(86), "Alpha", None), sg(86)),
            ("sg_bare_8", sync.Ac2Member(local(86), "Alpha", None), sg(86)),
            ("sg_with_spaces_around", sync.Ac2Member(" " + sg(86) + " ", "Alpha", None), sg(86)),
            ("nine_digits_generator_like", sync.Ac2Member(GENERATOR_LIKE, "Alpha", None), None),
            ("foreign_plus_suffix_digit", sync.Ac2Member(FOREIGN_DIGITS + "2", "Alpha", None), None),
            ("foreign_digits", sync.Ac2Member(FOREIGN_DIGITS, "Alpha", None), None),
            ("nine_digits_phone_like", sync.Ac2Member(NINE_DIGITS, "Alpha", None), None),
            ("sg_plus_suffix_digit", sync.Ac2Member(sg(86) + "2", "Alpha", None), None),
            ("65_then_8_starting_1", sync.Ac2Member("65" + "1" + local(86)[1:], "Alpha", None), None),
            ("bare_8_starting_1", sync.Ac2Member("1" + local(86)[1:], "Alpha", None), None),
            ("leading_zero", sync.Ac2Member("0" + local(86), "Alpha", None), None),
            ("phone_plus_initials", sync.Ac2Member(sg(86) + "AT", "Alpha", None), None),
            ("generator_letters", sync.Ac2Member("M000201", "Alpha", None), None),
            ("mobile_present_sg", sync.Ac2Member(GENERATOR_LIKE, "Alpha", local(87)), sg(87)),
            ("mobile_present_foreign", sync.Ac2Member("TILL87", "Alpha", FOREIGN_DIGITS), FOREIGN_DIGITS),
            ("mobile_present_wins_over_member_no", sync.Ac2Member(sg(88), "Alpha", local(87)), sg(87)),
        ]
        for label, member, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(sync._ac2_phone(member), expected)

    def check_no_phone(self, member_no: str) -> None:
        self.run_sync("ac2_to_shopify")
        self.shopify.add(customer(89, phone="+" + member_no, tags=()))  # someone holds those digits in Shopify
        self.ac2.add(sync.Ac2Member(member_no, "Alpha Tester", None), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"]["ac2_member_without_phone"], 1)
        self.assertEqual((self.links(), self.shopify.created), ({}, []))

    def test_generator_like_member_no_is_not_a_phone(self):
        self.check_no_phone(GENERATOR_LIKE)

    def test_foreign_member_no_with_suffix_is_not_a_phone(self):
        self.check_no_phone(FOREIGN_DIGITS + "2")

    def test_bare_sg_member_no_gets_65(self):
        self.run_sync("ac2_to_shopify")
        self.ac2.add(sync.Ac2Member(local(90), "Alpha Tester", None), log=True)
        self.run_sync("ac2_to_shopify")
        self.assertEqual(self.shopify.created[0]["phone"], plus(sg(90)))


class Q4ChangeLogDefaultOffTests(SyncTestCase):
    """Owner Q4: with the default config (every write switch off, the
    ChangeLog column null) AC2 -> Shopify reads no ChangeLog and links nothing."""

    def test_default_config_makes_no_links_and_sends_nothing(self):
        reads = []
        original = self.ac2.changelog_inserts
        self.ac2.changelog_inserts = lambda *args: reads.append(args) or original(*args)
        # G2 O1 shape: the AC2 insert has MemberNo = phone and no MobilePhone;
        # a Shopify customer already holds that phone.
        self.shopify.add(customer(91, first="MARY ANN", last="LEE", tags=()))
        config = make_config()
        self.assertEqual((config.ac2_create_enabled, config.shopify_create_enabled, config.changelog_xml_column), (False, False, None))
        results = [sync.SyncEngine(self.ledger, config, shopify=self.shopify, ac2=self.ac2).run(direction="ac2_to_shopify")]
        self.ac2.add(sync.Ac2Member(sg(91), "MARY ANN LEE", None), log=True)
        results.append(sync.SyncEngine(self.ledger, config, shopify=self.shopify, ac2=self.ac2).run(direction="ac2_to_shopify"))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 0)
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(reads, [])
        for result in results:
            self.assertEqual(result["counts"], {"changelog_column_unconfigured": 1})
            self.assertEqual([c for c in result["counts"] if "link" in c], [])
            self.assertEqual([r for r in result["refs"] if r["action"] == "link"], [])


class SimulatedParentDeath(BaseException):
    """The parent process dies after INTENT; the write-once child may still save."""


class H2OpenWriteRefusalTests(SyncTestCase):
    """G2 H2: a customer whose earlier write is open (INTENT or UNCERTAIN) is
    never registered again, so a killed run cannot leave two AC2 members."""

    def killed_after_intent(self, n: int) -> sync.Ac2Member:
        landed = []

        def die(member_no, payload, *, write_id):
            self.ac2.creates.append((member_no, dict(payload)))
            landed.append(sync.Ac2Member(member_no, payload["name"], payload["mobile_phone"]))
            raise SimulatedParentDeath()

        self.ac2.create_member = die
        self.shopify.add(customer(n, phone=None))  # no phone: a generator number
        with self.assertRaises(SimulatedParentDeath):
            self.run_sync("shopify_to_ac2")
        del self.ac2.create_member
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "INTENT")
        return landed[0]

    def pending_state(self, n: int) -> tuple:
        return tuple(self.ledger.conn.execute("SELECT state, code FROM shopify_pending WHERE customer_gid = ?", (gid(n),)).fetchone())

    def test_rerun_after_a_killed_run_creates_nothing_for_that_customer(self):
        orphan = self.killed_after_intent(92)
        result = self.run_sync("shopify_to_ac2")  # the orphaned child has not saved yet
        self.assertEqual(len(self.ac2.creates), 1)
        self.assertEqual(result["counts"].get("ac2_member_created", 0), 0)
        self.assertEqual(result["counts"]["customer_held_by_open_write"], 1)
        self.assertIn("customer_held_by_open_write", result["alerts"])
        self.assertEqual(self.pending_state(92), ("SKIPPED", "customer_held_by_open_write"))
        self.assertEqual(self.run_sync("shopify_to_ac2")["counts"].get("ac2_member_created", 0), 0)
        # The orphaned save lands: it is found, verified and linked. Still one member.
        self.ac2.add(orphan)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(92): orphan.member_no})
        self.assertEqual(result["counts"]["uncertain_write_resolved_linked"], 1)
        self.assertEqual(len(self.ac2.creates), 1)

    def test_another_customer_is_still_created(self):
        self.killed_after_intent(93)
        self.shopify.add(customer(94))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates[1:]], [sg(94)])
        self.assertEqual(result["counts"]["ac2_member_created"], 1)

    def add_write(self, n: int, state: str, member_no: str) -> None:
        now = dl.ts(self.now)
        with self.ledger.tx() as conn:
            conn.execute(
                "INSERT INTO writes(write_id, direction, customer_gid, member_no, member_no_key, state, code, created_at, updated_at) VALUES (?, 'shopify_to_ac2', ?, ?, ?, ?, 'test', ?, ?)",
                (f"write-{n:032d}", gid(n), member_no, sync.member_no_key(member_no), state, now, now),
            )

    def test_open_write_for_the_customer_refuses_any_create(self):
        self.add_write(95, "UNCERTAIN", "M000150")
        self.shopify.add(customer(95))  # the phone path too
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["customer_held_by_open_write"], 1)
        self.assertIn("customer_held_by_open_write", result["alerts"])
        self.assertEqual(self.pending_state(95), ("SKIPPED", "customer_held_by_open_write"))

    def test_closed_writes_do_not_block(self):
        for n, state in ((96, "NOT_WRITTEN"), (97, "RESOLVED"), (98, "DONE"), (99, "REJECTED")):
            self.add_write(n, state, f"M0001{n}")
            self.shopify.add(customer(n))
        result = self.run_sync("shopify_to_ac2")
        self.assertNotIn("customer_held_by_open_write", result["counts"])
        self.assertEqual(sorted(m for m, _ in self.ac2.creates), sorted(sg(n) for n in (96, 97, 98, 99)))


class H3ChangeLogOverlapBaselineTests(SyncTestCase):
    """G2 H3 (Option A): when the overlap window cannot be trusted (a ledger
    upgraded from v1, or a larger overlap than the last run used), the rows
    already in it are history: marked seen, never queued."""

    def log_history(self, numbers) -> None:
        for n in numbers:
            self.ac2.add(sync.Ac2Member(sg(n), f"Member {n}", sg(n)), log=True)

    def pending(self, ledger=None) -> int:
        return (ledger or self.ledger).conn.execute("SELECT COUNT(*) FROM ac2_pending").fetchone()[0]

    def test_upgraded_v1_ledger_with_a_watermark_queues_no_history(self):
        import sqlite3

        self.log_history((11, 12, 13, 14))
        watermark = self.ac2.changelog[-1][0]
        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-v1wm-"))
        self.addCleanup(shutil.rmtree, temp, True)
        v1 = sync.LEDGER_SCHEMA.replace(", intent_digest TEXT", "").replace("CREATE TABLE changelog_seen(id INTEGER PRIMARY KEY, seen_at TEXT NOT NULL);\n", "")
        conn = sqlite3.connect(temp / sync.LEDGER_FILE)
        conn.executescript(v1.replace("created_at, intent_digest ON writes", "created_at ON writes"))
        conn.execute("INSERT INTO meta(id, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at) VALUES (1, 'sync-v1', ?, ?, ?, ?, 't')", (TEST_BOOK, STORE, DOMAIN, "ab" * 32))
        conn.execute("INSERT INTO watermarks(name, value, updated_at) VALUES ('ac2_changelog_id', ?, 't')", (str(watermark),))
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        ledger = sync.SyncLedger(temp, clock=lambda: self.now, check_custody=False)
        self.addCleanup(ledger.close)
        engine = lambda: sync.SyncEngine(ledger, make_config(changelog_xml_column="Content"), shopify=self.shopify, ac2=self.ac2)
        result = engine().run(direction="ac2_to_shopify")
        self.assertEqual(self.pending(ledger), 0)
        self.assertNotIn("ac2_insert_queued", result["counts"])
        self.assertEqual(result["counts"]["changelog_overlap_baselined"], 1)
        self.assertEqual(result["counts"]["changelog_overlap_baselined_rows"], 4)
        self.assertIn("changelog_overlap_baselined", result["alerts"])
        # Afterwards a new insert is processed as usual, and no second baseline runs.
        self.log_history((15,))
        result = engine().run(direction="ac2_to_shopify")
        self.assertEqual((self.pending(ledger), result["counts"]["ac2_insert_queued"]), (1, 1))
        self.assertNotIn("changelog_overlap_baselined", result["counts"])

    def test_raising_the_overlap_queues_no_pruned_history(self):
        self.log_history(range(11, 23))
        self.run_sync("ac2_to_shopify", changelog_overlap=10)  # first run: watermark at the end
        self.log_history((23,))
        self.run_sync("ac2_to_shopify", changelog_overlap=10, shopify_create_enabled=False)
        self.assertEqual(self.pending(), 1)
        result = self.run_sync("ac2_to_shopify", changelog_overlap=100, shopify_create_enabled=False)
        self.assertEqual(self.pending(), 1)
        self.assertNotIn("ac2_insert_queued", result["counts"])
        self.assertIn("changelog_overlap_baselined", result["alerts"])
        # The larger overlap is now the stored one: the next run is a normal run.
        result = self.run_sync("ac2_to_shopify", changelog_overlap=100, shopify_create_enabled=False)
        self.assertNotIn("changelog_overlap_baselined", result["counts"])

    def test_unchanged_overlap_still_processes_a_late_row_once(self):
        self.run_sync("ac2_to_shopify")
        self.log_history((24,))
        self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        watermark = int(self.ledger.watermark("ac2_changelog_id"))
        self.ac2.changelog.append((watermark - 3, changelog_xml(sg(25), "Member", sg(25))))
        result = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertEqual(result["counts"]["changelog_late_row_processed"], 1)
        self.assertNotIn("changelog_overlap_baselined", result["counts"])
        result = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertNotIn("changelog_late_row_processed", result["counts"])
        self.assertEqual(self.pending(), 2)

    def test_fresh_ledger_first_run_is_unchanged(self):
        self.log_history((26, 27))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"], {"changelog_watermark_initialised": 1})
        self.assertEqual(self.ledger.watermark("ac2_changelog_overlap"), "50")
        self.assertEqual(self.run_sync("ac2_to_shopify")["counts"], {})

    def test_overlap_zero_never_baselines(self):
        self.run_sync("ac2_to_shopify", changelog_overlap=0)
        self.log_history((28,))
        result = self.run_sync("ac2_to_shopify", changelog_overlap=0, shopify_create_enabled=False)
        self.assertEqual(result["counts"].get("ac2_insert_queued"), 1)
        self.assertNotIn("changelog_overlap_baselined", result["counts"])


class DryRunFallbackCountTests(SyncTestCase):
    """Round 5 (Owner-approved): with ac2_create_enabled off, the summary
    counts show how many creates WOULD use a fallback MemberNo, and how many
    of those come from a one-word name or an initial (UAT checklist)."""

    def register(self, ac2_name: str, first: str, last: str | None, **overrides) -> dict:
        self.ac2.add(sync.Ac2Member("HOLD30", ac2_name, local(30)))  # round 7: phone in MobilePhone, so the name rule decides
        self.shopify.add(customer(30, first=first, last=last))
        return self.run_sync("shopify_to_ac2", **overrides)

    def test_dry_run_counts_the_name_fallback(self):
        counts = self.register("MARY ANN LEE", "Mary", "Tan", ac2_create_enabled=False)["counts"]
        self.assertEqual((counts["shared_phone_name_fallback"], counts["ac2_create_disabled"]), (1, 1))
        self.assertNotIn("name_fallback_one_word", counts)
        self.assertNotIn("name_fallback_initial", counts)
        self.assertEqual(self.ac2.creates, [])

    def test_dry_run_counts_a_one_word_name_fallback(self):
        counts = self.register("MARY ANN LEE", "Lee", None, ac2_create_enabled=False)["counts"]
        self.assertEqual((counts["shared_phone_name_fallback"], counts["name_fallback_one_word"]), (1, 1))
        self.assertEqual(self.ac2.creates, [])

    def test_dry_run_counts_an_initial_fallback(self):
        counts = self.register("Alpha T", "Alpha", "Tester", ac2_create_enabled=False)["counts"]
        self.assertEqual((counts["shared_phone_name_fallback"], counts["name_fallback_initial"]), (1, 1))
        self.assertNotIn("name_fallback_one_word", counts)

    def test_dry_run_counts_the_digit_fallback(self):
        counts = self.register("Charlie Holder", CJK_NAME, None, ac2_create_enabled=False)["counts"]
        self.assertEqual((counts["shared_phone_digit_fallback"], counts["name_fallback_one_word"]), (1, 1))

    def test_dry_run_counts_new_phone_and_generator_creates(self):
        self.shopify.add(customer(31))
        self.shopify.add(customer(32, phone=None))
        counts = self.run_sync("shopify_to_ac2", ac2_create_enabled=False)["counts"]
        self.assertEqual((counts["new_member_phone"], counts["no_phone_generator_member_no"], counts["ac2_create_disabled"]), (1, 1, 2))
        self.assertEqual(self.ac2.generator_calls, 0)  # still never called while writes are off

    def test_switch_on_counts_are_unchanged_apart_from_the_reason(self):
        counts = self.register("MARY ANN LEE", "Lee", None)["counts"]
        self.assertEqual((counts["ac2_member_created"], counts["shared_phone_name_fallback"], counts["name_fallback_one_word"]), (1, 1, 1))
        self.assertNotIn("ac2_create_disabled", counts)

    def test_write_cap_refusal_with_the_switch_on_counts_no_decision(self):
        counts = self.register("MARY ANN LEE", "Lee", None, max_writes_per_run=0)["counts"]
        self.assertEqual(counts["max_writes_per_run_reached"], 1)
        self.assertNotIn("shared_phone_name_fallback", counts)
        self.assertNotIn("name_fallback_one_word", counts)

    def test_ac2_to_shopify_counts_a_one_word_name_while_create_is_off(self):
        self.run_sync("ac2_to_shopify")
        self.shopify.add(customer(33, first="Lee", last=None, tags=()))
        self.ac2.add(sync.Ac2Member("TILL33", "MARY ANN LEE", sg(33)), log=True)
        counts = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)["counts"]
        self.assertEqual((counts["shopify_phone_owned_by_other_name"], counts["name_fallback_one_word"], counts["shopify_create_disabled"]), (1, 1, 1))
        self.assertEqual(self.shopify.created, [])


SGT = timezone(timedelta(hours=8))  # Asia/Singapore: UTC+8 all year


def sgt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=SGT)


class DeferredCreateTests(SyncTestCase):
    """Round 6 (Owner, 22 Oct 2026): staff keep creating members by hand from
    the sign-up sheet. A Shopify sign-up is first linked to the member staff
    create (phone + name rule, no AC2 write); only one still unmatched at the
    cutoff (06:00 Asia/Singapore the day after sign-up) is created by the sync,
    with an alert. Before the cutoff nothing is written."""

    def pending(self) -> dict:
        return {row[0]: (row[1], row[2]) for row in self.ledger.conn.execute("SELECT customer_gid, state, code FROM shopify_pending")}

    def test_before_the_cutoff_nothing_is_written(self):
        self.now = T0 + timedelta(hours=1)
        self.shopify.add(customer(40))
        self.shopify.add(customer(41, phone=None))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.ac2.creates, self.ac2.generator_calls, self.links()), ([], 0, {}))
        self.assertEqual(result["counts"]["waiting_for_staff"], 2)
        self.assertEqual(self.pending(), {gid(40): ("PENDING", "waiting_for_staff"), gid(41): ("PENDING", "waiting_for_staff")})
        self.assertEqual(result["alerts"], [])

    def test_a_staff_member_made_before_the_cutoff_is_linked_without_a_write(self):
        self.now = T0 + timedelta(hours=1)
        self.shopify.add(replace(customer(42, first="Mary Ann", last="Lee"), created_at=T0))
        self.run_sync("shopify_to_ac2")
        # Staff key the member on the spot (round 7: MemberNo + CreatedTime).
        self.ac2.add(sync.Ac2Member(sg(42), "MARY A LEE", None, created_at=T0 + timedelta(minutes=4)))
        self.now = T0 + timedelta(hours=3)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates), ({gid(42): sg(42)}, []))
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 1)
        self.now = T0 + timedelta(days=2)  # nothing more after the cutoff
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.ac2.creates, result["alerts"]), ([], []))

    def test_initials_never_link_so_the_sign_up_waits_then_is_created(self):
        self.now = T0 + timedelta(hours=1)
        self.ac2.add(sync.Ac2Member("STAFF43", "Mary A Lee", local(43)))  # an initial; round 7: phone in MobilePhone
        self.shopify.add(customer(43, first="Mary Ann", last="Lee"))
        self.assertEqual(self.run_sync("shopify_to_ac2")["counts"]["waiting_for_staff"], 1)
        self.now = T0 + timedelta(days=1)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual([m for m, _ in self.ac2.creates], [sync.fallback_member_no(sg(43), "Mary Ann", "Lee")])
        self.assertIn("created_after_wait", result["alerts"])

    def test_email_never_links_while_waiting(self):
        self.now = T0 + timedelta(hours=1)
        self.ac2.add(sync.Ac2Member("STAFF44", "Alpha Tester", None, "alpha@example.com"))
        self.shopify.add(customer(44, phone=None, email="alpha@example.com"))
        self.run_sync("shopify_to_ac2")
        self.now = T0 + timedelta(days=1)
        self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 1)
        self.assertNotEqual(self.links()[gid(44)], "STAFF44")

    def test_unmatched_at_the_cutoff_is_created_with_the_member_type_and_an_alert(self):
        self.shopify.add(customer(45))
        result = self.run_sync("shopify_to_ac2", ac2_create_member_type="GUEST")
        self.assertEqual([(m, p["member_type"]) for m, p in self.ac2.creates], [(sg(45), "GUEST")])
        self.assertEqual((result["counts"]["ac2_member_created"], result["counts"]["created_after_wait"]), (1, 1))
        self.assertIn("created_after_wait", result["alerts"])
        self.assertNotIn("waiting_for_staff", result["counts"])

    def test_default_member_type_and_cutoff_time(self):
        config = make_config()
        self.assertEqual((config.ac2_create_member_type, config.deferred_create_local_time), ("REGISTERED", "06:00"))
        self.shopify.add(customer(46))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.ac2.creates[0][1]["member_type"], "REGISTERED")

    def test_cutoff_is_the_next_morning_in_singapore(self):
        cutoff = sync.deferred_create_cutoff
        self.assertEqual(cutoff(sgt(21, 23, 30), "06:00"), sgt(22, 6))
        self.assertEqual(cutoff(sgt(21, 0, 10), "06:00"), sgt(22, 6))  # never the same morning
        self.assertEqual(cutoff(datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc), "06:00"), sgt(23, 6))  # 01:00 on the 22nd in Singapore
        self.assertEqual(cutoff(sgt(21, 10), "09:30"), sgt(22, 9, 30))

    def check_cutoff(self, n: int, signed_up: datetime, before: datetime, at: datetime, **overrides) -> None:
        self.shopify.add(customer(n, registered=signed_up.astimezone(timezone.utc)))
        self.now = before
        self.assertEqual(self.run_sync("shopify_to_ac2", **overrides)["counts"].get("waiting_for_staff"), 1)
        self.assertEqual(self.ac2.creates, [])
        self.now = at
        self.run_sync("shopify_to_ac2", **overrides)
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(n)])

    def test_late_evening_sign_up_waits_until_six(self):
        self.check_cutoff(47, sgt(21, 23, 30), sgt(22, 5, 59), sgt(22, 6))

    def test_configured_cutoff_time(self):
        self.check_cutoff(48, sgt(21, 10), sgt(22, 9, 29), sgt(22, 9, 30), deferred_create_local_time="09:30")

    def test_dry_run_after_the_cutoff_counts_what_would_be_created(self):
        self.shopify.add(customer(49))
        result = self.run_sync("shopify_to_ac2", ac2_create_enabled=False)
        self.assertEqual((result["counts"]["ac2_create_disabled"], result["counts"]["new_member_phone"]), (1, 1))
        self.assertNotIn("created_after_wait", result["counts"])
        self.assertNotIn("waiting_for_staff", result["counts"])

    def test_settings_are_validated(self):
        for value in ("6:00", "24:00", "06:60", "06:00 ", 600, None):
            with self.subTest(time=value), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(deferred_create_local_time=value)
            self.assertEqual(caught.exception.code, "sync_config_deferred_create_local_time_invalid")
        for value in ("", "A B", "X" * 21, "GUÉST", "-GUEST", None, 1):
            with self.subTest(member_type=ascii(value)), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(ac2_create_member_type=value)
            self.assertEqual(caught.exception.code, "sync_config_ac2_create_member_type_invalid")
        self.assertEqual(make_config(ac2_create_member_type="Default").ac2_create_member_type, "Default")

    def test_the_sync_has_no_way_to_change_an_existing_member(self):
        # Owner: never change MemberType (or anything) on a member staff created.
        bridge = (ROOT / "scripts" / "ac2_directlocal_member_bridge.ps1").read_text(encoding="utf-8")
        operations = re.search(r'ValidateSet\(([^)]*)\)\]\[string\]\$Operation', bridge).group(1)
        self.assertEqual(sorted(re.findall(r'"([a-z-]+)"', operations)),
                         sorted(["book-check", "pos-sync-status", "legacy-precheck", "next-member-no", "probe", "reconcile", "write-once", "sync-lookup", "sync-changelog"]))
        source = (ROOT / "directlocal" / "xb_member_sync.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?i)update_?member|edit_?member|change_?member_?type")
        member_script = (ROOT / "scripts" / "ac2_member_sync_member.ps1").read_text(encoding="utf-8")
        self.assertEqual((member_script.count(".SaveMember("), member_script.count(".NewMember(")), (1, 1))


class PossibleDuplicateTests(SyncTestCase):
    """Round 6: a staff-created member that appears after the sync created
    one for the same person (same phone, name rule) raises an alert; nothing
    is merged, linked or written."""

    def created_by_sync(self, n: int) -> str:
        self.shopify.add(customer(n, first="Mary Ann", last="Lee"))
        self.run_sync("shopify_to_ac2")
        self.assertEqual(len(self.ac2.creates), 1)
        return self.ac2.creates[0][0]

    def test_a_later_staff_member_for_the_same_person_is_flagged(self):
        member_no = self.created_by_sync(50)
        self.ac2.add(sync.Ac2Member("STAFF50", "MARY ANN LEE", local(50)))
        self.now += timedelta(hours=2)
        result = self.run_sync("shopify_to_ac2")
        self.assertIn("possible_duplicate_member", result["alerts"])
        self.assertEqual(result["counts"]["possible_duplicate_member"], 1)
        self.assertEqual((len(self.ac2.creates), self.links()), (1, {gid(50): member_no}))

    def test_another_name_on_the_phone_is_not_flagged(self):
        self.created_by_sync(51)
        self.ac2.add(sync.Ac2Member("STAFF51", "John Lim", local(51)))
        self.now += timedelta(hours=2)
        self.assertNotIn("possible_duplicate_member", self.run_sync("shopify_to_ac2")["counts"])

    def test_the_watch_ends_after_seven_days(self):
        self.created_by_sync(52)
        self.ac2.add(sync.Ac2Member("STAFF52", "MARY ANN LEE", local(52)))
        self.now += timedelta(days=8)
        self.assertNotIn("possible_duplicate_member", self.run_sync("shopify_to_ac2")["counts"])

    def test_a_changelog_insert_for_a_sync_created_customer_is_flagged(self):
        self.run_sync("ac2_to_shopify")  # watermark
        self.created_by_sync(53)
        self.run_sync("ac2_to_shopify")  # our own insert: loop prevention
        self.ac2.add(sync.Ac2Member("STAFF53", "MARY ANN LEE", local(53)), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertIn("possible_duplicate_member", result["alerts"])
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(self.ledger.conn.execute("SELECT state, code FROM ac2_pending WHERE member_no = 'STAFF53'").fetchone()[:], ("SKIPPED", "possible_duplicate_member"))


class MemberNoTimeWindowTests(SyncTestCase):
    """Round 7 (Owner, 22 Oct 2026): shop staff key the member in AC2 on the
    spot. A Shopify sign-up links to the AC2 member whose MemberNo is exactly
    the sign-up's phone MemberNo when AC2 CreatedTime (Singapore local time,
    no offset) and the sign-up time (round 7c: the start_date metafield's
    createdAt; the ``signup`` helper sets both) are at most
    ``memberno_link_window_minutes`` (round 10: default 60, was 10) apart,
    either way. Name is not used. The same MemberNo outside the window: no
    link, no create, an alert. Only stored times are compared, so any run
    frequency works. Round 10 (Owner UAT r4): CreatedTime is when the New
    Member form opens, not when it is saved, so the window runs from form
    open."""

    def signup(self, n: int, at: datetime, **kwargs) -> sync.ShopifyCustomer:
        return self.shopify.add(replace(customer(n, registered=at, **kwargs), created_at=at))

    def staff(self, n: int, at: datetime | None, name: str | None = "SOMEONE ELSE") -> None:
        # Typed by staff; deliberately not the sign-up's name (name not used).
        self.ac2.add(sync.Ac2Member(sg(n), name, None, created_at=at))

    def pending(self, n: int):
        return self.ledger.conn.execute("SELECT state, code FROM shopify_pending WHERE customer_gid = ?", (gid(n),)).fetchone()[:]

    def assert_linked(self, n: int, result: dict) -> None:
        self.assertEqual((self.links().get(gid(n)), self.ac2.creates), (sg(n), []))
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 1)
        self.assertEqual(result["alerts"], [])

    def assert_alerted(self, n: int, result: dict) -> None:
        self.assertEqual((self.links(), self.ac2.creates, self.ac2.generator_calls), ({}, [], 0))
        self.assertIn("memberno_exists_outside_window", result["alerts"])
        self.assertEqual(self.pending(n), ("SKIPPED", "memberno_exists_outside_window"))
        flagged = [r for r in result["refs"] if r["code"] == "memberno_exists_outside_window"]
        self.assertEqual(flagged, [{"ref": self.ledger.ref("c", gid(n)), "action": "skip", "code": "memberno_exists_outside_window", "member_no_masked": "*******" + sg(n)[-3:]}])
        self.assert_no_pii(result)

    def test_staff_two_minutes_before_sign_up_links(self):
        self.now = T0 + timedelta(hours=1)  # before the next-morning cutoff
        self.signup(60, T0)
        self.staff(60, T0 - timedelta(minutes=2))
        self.assert_linked(60, self.run_sync("shopify_to_ac2"))

    def test_staff_two_minutes_after_sign_up_links_without_a_name(self):
        self.now = T0 + timedelta(hours=1)
        self.signup(61, T0)
        self.staff(61, T0 + timedelta(minutes=2), name=None)
        self.assert_linked(61, self.run_sync("shopify_to_ac2"))

    def test_sixty_one_minutes_apart_alerts_and_never_creates(self):
        self.now = T0 + timedelta(hours=2)
        self.signup(62, T0)
        self.staff(62, T0 + timedelta(minutes=61))
        self.signup(63, T0)
        self.staff(63, T0 - timedelta(minutes=61))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(result["counts"]["memberno_exists_outside_window"], 2)
        self.assertEqual((self.pending(62), self.pending(63)), (("SKIPPED", "memberno_exists_outside_window"),) * 2)
        self.now = T0 + timedelta(days=2)  # long past the cutoff: still never created
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates, self.ac2.generator_calls), ({}, [], 0))
        self.assertEqual(result["alerts"], [])  # SKIPPED is final: alerted once, never re-read
        self.assertEqual((self.pending(62), self.pending(63)), (("SKIPPED", "memberno_exists_outside_window"),) * 2)

    def test_alert_shows_only_the_last_three_characters(self):
        self.now = T0 + timedelta(hours=2)
        self.signup(64, T0)
        self.staff(64, T0 + timedelta(minutes=61))
        self.assert_alerted(64, self.run_sync("shopify_to_ac2"))

    def test_exactly_sixty_minutes_links_and_a_millisecond_more_does_not(self):
        self.now = T0 + timedelta(hours=2)
        for n, offset in ((65, timedelta(minutes=60)), (66, -timedelta(minutes=60))):
            self.signup(n, T0)
            self.staff(n, T0 + offset)
        self.signup(67, T0)
        self.staff(67, T0 + timedelta(minutes=60, milliseconds=1))
        self.signup(68, T0)
        self.staff(68, T0 - timedelta(minutes=60, milliseconds=1))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(65): sg(65), gid(66): sg(66)})
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 2)
        self.assertEqual(result["counts"]["memberno_exists_outside_window"], 2)
        self.assertEqual(self.ac2.creates, [])

    def test_window_is_configurable_and_validated(self):
        self.assertEqual(make_config().memberno_link_window_minutes, 60)
        self.now = T0 + timedelta(hours=1)
        self.signup(69, T0)
        self.staff(69, T0 + timedelta(minutes=11))
        result = self.run_sync("shopify_to_ac2", memberno_link_window_minutes=10)
        self.assertEqual((self.links(), result["counts"]["memberno_exists_outside_window"]), ({}, 1))
        self.signup(59, T0)
        self.staff(59, T0 + timedelta(minutes=11))
        result = self.run_sync("shopify_to_ac2", memberno_link_window_minutes=15)
        self.assertEqual(self.links(), {gid(59): sg(59)})
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 1)
        for bad in (0, 61, "10", True, None, 10.5):
            with self.subTest(bad=bad), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(memberno_link_window_minutes=bad)
            self.assertEqual(caught.exception.code, "sync_config_memberno_link_window_minutes_invalid")

    def test_form_opened_forty_five_minutes_before_the_sign_up_links_by_default(self):
        # Round 10 (Owner UAT r4): staff open the New Member form (CreatedTime),
        # the customer signs up on the phone, staff save later. 45 minutes
        # from form open to sign-up is inside the default 60-minute window.
        self.now = T0 + timedelta(hours=1)
        self.signup(57, T0)
        self.staff(57, T0 - timedelta(minutes=45))
        self.assert_linked(57, self.run_sync("shopify_to_ac2"))

    def test_staff_created_across_singapore_midnight_links(self):
        # Sign-up 23:58 Singapore time on 21 Oct (15:58Z); staff key the
        # member at 00:03 on 22 Oct Singapore time (AC2 stores it with no
        # offset). Read as UTC by mistake it would be 8 hours away.
        signed_up = datetime(2026, 10, 21, 15, 58, tzinfo=timezone.utc)
        self.now = signed_up + timedelta(hours=1)
        self.signup(70, signed_up)
        self.ac2.add(sync.Ac2Member(sg(70), "SOMEONE ELSE", None, created_at=sync.ac2_created_time("2026-10-22T00:03:00")))
        self.assert_linked(70, self.run_sync("shopify_to_ac2"))

    def test_daily_run_still_links_from_stored_times(self):
        # The only run of the day is long after both events and after the
        # next-morning cutoff: the stored times decide, not the run time.
        self.signup(71, T0)
        self.staff(71, T0 + timedelta(minutes=3))
        self.now = T0 + timedelta(days=1, hours=2)
        self.assertGreater(self.now, sync.deferred_create_cutoff(T0, "06:00"))
        self.assert_linked(71, self.run_sync("shopify_to_ac2"))

    def test_no_member_no_match_waits_then_is_created(self):
        # Round 6 behaviour unchanged when no AC2 MemberNo is the phone.
        self.now = T0 + timedelta(hours=1)
        self.signup(72, T0)
        self.assertEqual(self.run_sync("shopify_to_ac2")["counts"]["waiting_for_staff"], 1)
        self.now = T0 + timedelta(days=1)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual([c[0] for c in self.ac2.creates], [sg(72)])
        self.assertEqual(self.ac2.creates[0][1]["member_type"], "REGISTERED")
        self.assertIn("created_after_wait", result["alerts"])

    def test_no_phone_is_unchanged(self):
        # No phone: no MemberNo to compare, so a member staff keyed at the
        # same minute is never linked; wait, then the generator create.
        self.now = T0 + timedelta(hours=1)
        self.signup(73, T0, phone=None)
        self.ac2.add(sync.Ac2Member("STAFF73", "Alpha Tester", None, created_at=T0))
        self.assertEqual(self.run_sync("shopify_to_ac2")["counts"]["waiting_for_staff"], 1)
        self.now = T0 + timedelta(days=1)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual([c[0] for c in self.ac2.creates], ["M000201"])
        self.assertEqual(result["counts"]["no_phone_generator_member_no"], 1)
        self.assertIn("created_after_wait", result["alerts"])
        self.assertNotIn("memberno_and_created_time_match", result["counts"])

    def test_missing_created_time_never_links_or_creates(self):
        # Round 7c: the Shopify side is the sign-up time, which a registered
        # customer always has (no sign-up time never reaches this rule).
        self.now = T0 + timedelta(days=1)
        self.signup(74, T0)
        self.staff(74, None)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates), ({}, []))
        self.assertEqual(result["counts"]["created_time_missing"], 1)
        self.assertIn("created_time_missing", result["alerts"])

    def test_name_rule_applies_only_when_no_member_no_is_the_phone(self):
        # A legacy record holding the phone in MobilePhone under another
        # MemberNo still needs the 2-word name rule (shared-phone fallback).
        self.now = T0 + timedelta(days=1)
        self.signup(77, T0, first="Mary Ann", last="Lee")
        self.ac2.add(sync.Ac2Member("LEGACY77", "MARY ANN LEE", local(77), created_at=T0 - timedelta(days=900)))
        self.signup(78, T0, first="Mary Ann", last="Lee")
        self.ac2.add(sync.Ac2Member("LEGACY78", "John Lim", local(78), created_at=T0))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links()[gid(77)], "LEGACY77")
        self.assertEqual(result["counts"]["phone_and_name_match"], 1)
        self.assertEqual(result["counts"]["shared_phone_name_fallback"], 1)
        self.assertEqual([c[0] for c in self.ac2.creates], [sync.fallback_member_no(sg(78), "Mary Ann", "Lee")])


class Ac2CreatedTimeTests(unittest.TestCase):
    """VM fact (G0, 22 Oct 2026): dbo.Member.CreatedTime is a datetime in
    Singapore local time with no offset, millisecond precision. The bridge
    sends it as CONVERT(varchar(23), CreatedTime, 126); the sync reads it as
    Asia/Singapore (UTC+8) and compares in UTC."""

    def test_singapore_local_time_becomes_utc(self):
        utc = timezone.utc
        self.assertEqual(sync.ac2_created_time("2026-10-21T10:02:00.123"), datetime(2026, 10, 21, 2, 2, 0, 123000, tzinfo=utc))
        self.assertEqual(sync.ac2_created_time("2026-10-21T10:02:00"), datetime(2026, 10, 21, 2, 2, tzinfo=utc))  # style 126 drops .000
        self.assertEqual(sync.ac2_created_time("2026-10-22T00:05:00.5"), datetime(2026, 10, 21, 16, 5, 0, 500000, tzinfo=utc))
        self.assertEqual(sync.ac2_created_time("2026-10-22T07:59:59.997"), datetime(2026, 10, 21, 23, 59, 59, 997000, tzinfo=utc))
        self.assertEqual(sync.ac2_created_time("2027-01-01T03:00:00"), datetime(2026, 12, 31, 19, 0, tzinfo=utc))  # across the year
        self.assertEqual(sync.ac2_created_time("2026-10-22T00:05:00").utcoffset(), timedelta(0))

    def test_anything_else_is_unreadable(self):
        for bad in (None, "", 20261021, "2026-10-21 10:02:00", "2026-10-21T10:02:00+08:00", "2026-10-21T10:02:00Z", "2026-10-21T10:02",
                    "2026-13-01T00:00:00", "2026-10-21T24:00:00", "2026-10-21T10:02:00.1234", " 2026-10-21T10:02:00", "2026-10-21T10:02:00\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(sync.ac2_created_time(bad))

    def test_bridge_lookup_rows_carry_the_created_time(self):
        class Process:
            returncode = 0
            pid = 1

            def communicate(self, data, timeout=None):
                rows = [{"member_no": sg(80), "name": None, "mobile_phone": None, "email_address": None, "dob": None, "created_time": "2026-10-21T10:02:00.123"},
                        {"member_no": sg(81), "name": None, "mobile_phone": None, "email_address": None, "dob": None, "created_time": None}]
                return (json.dumps({"members": rows}).encode(), b"")

        client = sync.SyncBridgeClient(["powershell.exe"], autocount_book=TEST_BOOK, claims=Path("."), env={}, popen=lambda args, **kwargs: Process())
        members = client.lookup(sg(80), [])
        self.assertEqual([m.created_at for m in members], [datetime(2026, 10, 21, 2, 2, 0, 123000, tzinfo=timezone.utc), None])


class SignUpTimeWindowTests(SyncTestCase):
    """Round 7c (Owner, 22 Oct 2026): the window compares AC2 CreatedTime with
    the sign-up time the sync already reads (``registration_at``, the
    ``membership.start_date`` metafield's ``createdAt``), not
    Customer.createdAt; a MemberNo of the 8 Singapore digits without ``65``
    is the same number; a foreign ``+`` number gets the same rule."""

    def staff(self, member_no: str, at: datetime) -> None:
        self.ac2.add(sync.Ac2Member(member_no, "SOMEONE ELSE", None, created_at=at))

    def test_an_older_shopify_account_links_by_its_sign_up_time(self):
        # customer(): account made 400 days before the sign-up at T0.
        self.now = T0 + timedelta(hours=1)
        self.shopify.add(customer(80))
        self.staff(sg(80), T0 + timedelta(minutes=2))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates), ({gid(80): sg(80)}, []))
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 1)

    def test_customer_created_at_alone_never_links(self):
        # Account made at T0, sign-up (start_date metafield) 90 minutes later:
        # staff keyed the member at T0 + 1 min, 89 minutes before the sign-up
        # (round 10: outside the default 60-minute window).
        self.now = T0 + timedelta(hours=2)
        self.shopify.add(replace(customer(81, registered=T0 + timedelta(minutes=90)), created_at=T0))
        self.staff(sg(81), T0 + timedelta(minutes=1))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates), ({}, []))
        self.assertIn("memberno_exists_outside_window", result["alerts"])

    def test_eight_digit_member_no_is_the_same_number(self):
        self.now = T0 + timedelta(hours=1)
        self.shopify.add(customer(82))
        self.staff(local(82), T0 - timedelta(minutes=3))
        self.shopify.add(customer(83))
        self.staff(local(83), T0 - timedelta(minutes=61))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(82): local(82)})
        self.assertEqual(self.ac2.creates, [])
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 1)
        flagged = [r for r in result["refs"] if r["code"] == "memberno_exists_outside_window"]
        self.assertEqual([r["member_no_masked"] for r in flagged], ["*****" + local(83)[-3:]])
        self.assert_no_pii(result)

    def test_both_forms_of_the_number_alert_as_a_conflict(self):
        self.now = T0 + timedelta(days=1)  # past the cutoff: still never created
        self.shopify.add(customer(84))
        self.staff(sg(84), T0)
        self.staff(local(84), T0)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates), ({}, []))
        self.assertIn("memberno_conflict", result["alerts"])
        self.assertEqual(self.ledger.conn.execute("SELECT state, code FROM shopify_pending WHERE customer_gid = ?", (gid(84),)).fetchone()[:], ("SKIPPED", "memberno_conflict"))

    def test_foreign_numbers_get_the_same_window(self):
        other = "60" + "1" + "2000" + "0002"  # a second fake foreign number
        self.now = T0 + timedelta(hours=1)
        self.shopify.add(customer(85, phone=plus(FOREIGN_DIGITS)))
        self.staff(FOREIGN_DIGITS, T0 + timedelta(minutes=60))
        self.shopify.add(customer(86, phone=plus(other)))
        self.staff(other, T0 - timedelta(minutes=60, seconds=1))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(85): FOREIGN_DIGITS})
        self.assertEqual(self.ac2.creates, [])
        self.assertIn("memberno_exists_outside_window", result["alerts"])


class InitialLinkTests(SyncTestCase):
    """Round 7c (Owner, 22 Oct 2026): before the window rule applies, the
    Shopify customers made before the cutover (Customer.createdAt <
    shopify_cutover_utc, any tag) are linked once to the AC2 member whose
    MemberNo is their phone (65 + 8 digits, or the 8 digits), with no time
    or name rule. Dry run by default; applying needs initial_link_enabled.
    Link-only: only ledger links are written. A conflict alerts, never
    merges. Shopify -> AC2 waits until an applied pass has completed."""

    def setUp(self):
        super().setUp()
        with self.ledger.tx() as conn:
            conn.execute("DELETE FROM watermarks WHERE name = 'initial_link_completed_at'")
        self.shopify.page_limit = None

    def historical(self, n: int, *, phone="sg", updated=None) -> sync.ShopifyCustomer:
        item = customer(n, phone=phone, tags=())
        made = CUTOVER - timedelta(days=30)
        return self.shopify.add(replace(item, created_at=made, updated_at=updated or made, registration_at=None, registration_value=None))

    def member(self, member_no: str, name: str = "WHOEVER TYPED") -> None:
        self.ac2.add(sync.Ac2Member(member_no, name, None, created_at=T0 - timedelta(days=900)))

    def link_codes(self) -> dict:
        return {row[0]: (row[1], row[2], row[3]) for row in self.ledger.conn.execute("SELECT customer_gid, member_no, origin, code FROM links")}

    def initial_link(self, *, apply: bool, **overrides) -> dict:
        return self.engine(**overrides).initial_link(apply=apply)

    def test_shopify_to_ac2_waits_for_the_initial_link(self):
        self.shopify.add(customer(1))
        result = self.run_sync("shopify_to_ac2")
        self.assertIn("initial_link_not_complete", result["alerts"])
        self.assertEqual((self.ac2.lookups, self.ac2.creates, self.links(), self.shopify.searches), (0, [], {}, []))

    def test_dry_run_counts_and_lists_masked_and_writes_nothing(self):
        self.historical(20)
        self.member(sg(20))
        self.historical(21)
        self.member(local(21))  # the 8 digits without 65: the same number
        self.historical(22, phone=None)
        self.historical(23)  # no AC2 member
        self.shopify.add(replace(customer(24), created_at=T0))  # made after the cutover: not historical
        self.member(sg(24))
        result = self.initial_link(apply=False)
        self.assertEqual(result["mode"], "dry_run")
        counts = result["counts"]
        self.assertEqual((counts["initial_link_would_link"], counts["initial_link_no_phone"], counts["initial_link_no_member"]), (2, 1, 1))
        self.assertEqual(sorted(r["member_no_masked"] for r in result["refs"] if r["action"] == "would_link"),
                         sorted(["*******" + sg(20)[-3:], "*****" + local(21)[-3:]]))
        self.assertEqual((self.links(), self.ac2.creates, self.shopify.created), ({}, [], []))
        self.assertIsNone(self.ledger.watermark("initial_link_completed_at"))
        self.assert_no_pii(result)

    def test_apply_needs_its_own_owner_gate(self):
        self.historical(25)
        self.member(sg(25))
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.initial_link(apply=True)
        self.assertEqual(caught.exception.code, "initial_link_disabled")
        self.assertEqual(self.links(), {})
        self.assertFalse(make_config().initial_link_enabled)

    def test_apply_links_by_member_no_without_time_or_name_and_writes_nothing_else(self):
        self.historical(26)
        self.member(sg(26))
        self.historical(27)
        self.member(local(27))
        result = self.initial_link(apply=True, initial_link_enabled=True)
        self.assertEqual(result["mode"], "apply")
        self.assertEqual(self.link_codes(), {gid(26): (sg(26), "matched", "initial_link_member_no"), gid(27): (local(27), "matched", "initial_link_member_no")})
        self.assertEqual(result["counts"]["initial_link_linked"], 2)
        self.assertEqual((self.ac2.creates, self.ac2.generator_calls, self.shopify.created, result["writes"]), ([], 0, [], 0))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 0)
        self.assertIsNotNone(self.ledger.watermark("initial_link_completed_at"))
        # Later the customer signs up: already linked, no window rule, no alert.
        self.shopify.add(replace(self.shopify.customers[gid(26)], tags=("member-mg",), registration_at=T0, registration_value="2026-10-21"))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(result["alerts"], [])
        self.assertEqual(self.ac2.creates, [])

    def test_conflicts_alert_and_never_link(self):
        self.historical(28)
        self.member(sg(28))
        self.member(local(28))  # both forms in AC2
        self.historical(29)
        self.member(sg(29))
        with self.ledger.tx() as conn:  # that member is already linked to another customer
            self.ledger.add_link(conn, gid(97), sg(29), "matched", "test")
        result = self.initial_link(apply=True, initial_link_enabled=True)
        self.assertEqual(result["counts"]["initial_link_member_no_conflict"], 2)
        self.assertIn("initial_link_member_no_conflict", result["alerts"])
        self.assertEqual(self.links(), {gid(97): sg(29)})
        self.assert_no_pii(result)

    def test_an_unfinished_pass_resumes_and_keeps_the_sync_waiting(self):
        for n, minutes in ((30, 1), (31, 2), (32, 3)):
            self.historical(n, updated=CUTOVER - timedelta(minutes=60 - minutes))
            self.member(sg(n))
        self.shopify.page_limit = 2
        first = self.initial_link(apply=True, initial_link_enabled=True)
        self.assertFalse(first["complete"])
        self.assertIsNone(self.ledger.watermark("initial_link_completed_at"))
        self.assertIn("initial_link_not_complete", self.run_sync("shopify_to_ac2")["alerts"])
        second = self.initial_link(apply=True, initial_link_enabled=True)
        self.assertTrue(second["complete"])
        self.assertEqual(set(self.links()), {gid(30), gid(31), gid(32)})
        self.assertIsNotNone(self.ledger.watermark("initial_link_completed_at"))
        self.assertIsNone(self.ledger.watermark("initial_link_updated_at"))  # a later pass starts again

    def test_dry_runs_resume_then_start_again_and_never_link(self):
        for n, minutes in ((36, 1), (37, 2), (38, 3)):
            self.historical(n, updated=CUTOVER - timedelta(minutes=60 - minutes))
            self.member(sg(n))
        self.shopify.page_limit = 2
        counts = [self.initial_link(apply=False)["counts"].get("initial_link_would_link", 0) for _ in range(3)]
        # Pages 1 (2 customers), 2 (the cursor customer again + the last), then a fresh pass.
        self.assertEqual(counts, [2, 2, 2])
        self.assertEqual((self.links(), self.ledger.watermark("initial_link_completed_at")), ({}, None))
        self.assertIsNone(self.ledger.watermark("initial_link_updated_at"))

    def test_a_stuck_cursor_alerts(self):
        for n in (33, 34, 35):
            self.historical(n)  # all updated at the same instant
        self.shopify.page_limit = 2
        self.initial_link(apply=True, initial_link_enabled=True)
        result = self.initial_link(apply=True, initial_link_enabled=True)
        self.assertIn("initial_link_cursor_stuck", result["alerts"])
        self.assertIsNone(self.ledger.watermark("initial_link_completed_at"))

    def test_cli_runs_a_dry_run_unless_apply_is_given(self):
        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-cli-"))
        self.addCleanup(shutil.rmtree, temp, True)
        path = temp / "config.json"
        path.write_text(json.dumps(config_value()), encoding="utf-8")
        engine = mock.Mock()
        engine.initial_link.return_value = {"alerts": []}
        with mock.patch.object(sync, "_state_root", return_value=temp), mock.patch.object(sync, "SyncLedger"), \
                mock.patch.object(sync, "build_real_engine", return_value=engine), redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main(["initial-link", "--config", str(path)]), 0)
            engine.initial_link.assert_called_once_with(apply=False)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(sync.main(["initial-link", "--apply", "--config", str(path)]), 2)
            self.assertEqual(json.loads(out.getvalue()), {"status": "refused", "code": "initial_link_disabled"})
            self.assertEqual(engine.initial_link.call_count, 1)  # refused before any engine call


class StaffMemberBeforeSignUpLinkTests(SyncTestCase):
    """Round 8, G2 H1: with ChangeLog reads and Shopify creates on, AC2 ->
    Shopify must never turn a staff-keyed member into a second Shopify
    customer. When the member's MemberNo is exactly a phone form (65 + 8
    digits or the 8 digits) and a Shopify customer or a pending sign-up has
    that phone, AC2 -> Shopify creates nothing: it links by the Shopify ->
    AC2 rule (exact MemberNo + CreatedTime within the window of the sign-up
    time), otherwise it holds the member and alerts."""

    def setUp(self):
        super().setUp()
        self.now = T0 + timedelta(hours=1)
        self.run_sync("ac2_to_shopify")  # ChangeLog watermark

    def sign_up(self, n: int, *, pending_metafield=False, tags=("member-mg",)) -> sync.ShopifyCustomer:
        item = replace(customer(n, tags=tags), created_at=T0)
        if pending_metafield:
            item = replace(item, registration_at=None, registration_value=None)
        return self.shopify.add(item)

    def staff(self, member_no: str, at: datetime) -> None:
        # Staff key the member at the till with a name typed differently.
        self.ac2.add(sync.Ac2Member(member_no, "SOMEONE ELSE", None, created_at=at), log=True)

    def ac2_pending(self, member_no: str):
        row = self.ledger.conn.execute("SELECT state, code FROM ac2_pending WHERE member_no = ?", (member_no,)).fetchone()
        return None if row is None else row[:]

    def test_g2_repro_metafield_pending_never_creates_a_duplicate(self):
        self.sign_up(90, pending_metafield=True)
        self.staff(sg(90), T0 + timedelta(minutes=2))
        first = self.run_sync("both")
        self.assertEqual((self.shopify.created, self.links()), ([], {}))
        self.assertIn("ac2_member_held_for_sign_up", first["alerts"])
        self.assertEqual(self.ac2_pending(sg(90)), ("PENDING", "held_for_sign_up"))
        # The start_date metafield appears: Shopify -> AC2 links by time and
        # AC2 -> Shopify then finds the member linked.
        self.shopify.add(replace(self.shopify.customers[gid(90)], registration_at=T0, registration_value="2026-10-21"))
        self.now += timedelta(hours=1)
        second = self.run_sync("both")
        self.assertEqual((self.links(), self.shopify.created, self.ac2.creates), ({gid(90): sg(90)}, [], []))
        self.assertEqual(second["counts"]["memberno_and_created_time_match"], 1)
        self.assertEqual(self.ac2_pending(sg(90)), ("DONE", "already_linked"))
        self.assertEqual(second["alerts"], [])

    def test_ac2_side_links_by_time_when_it_runs_first(self):
        self.sign_up(91)
        self.staff(sg(91), T0 + timedelta(minutes=3))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual((self.links(), self.shopify.created), ({gid(91): sg(91)}, []))
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 1)
        self.assertEqual(self.ac2_pending(sg(91)), ("DONE", "memberno_and_created_time_match"))
        self.assertEqual(self.run_sync("shopify_to_ac2")["counts"]["shopify_already_linked"], 1)

    def test_eight_digit_member_no_links_by_time_too(self):
        self.sign_up(92)
        self.staff(local(92), T0 - timedelta(minutes=4))
        self.run_sync("ac2_to_shopify")
        self.assertEqual((self.links(), self.shopify.created), ({gid(92): local(92)}, []))

    def test_outside_the_window_alerts_and_never_creates(self):
        self.sign_up(93)
        self.staff(sg(93), T0 - timedelta(minutes=61))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual((self.links(), self.shopify.created), ({}, []))
        self.assertIn("memberno_exists_outside_window", result["alerts"])
        self.assertEqual(self.ac2_pending(sg(93)), ("SKIPPED", "memberno_exists_outside_window"))
        self.assert_no_pii(result)

    def test_a_pending_sign_up_missing_from_the_phone_search_is_not_duplicated(self):
        self.sign_up(94)
        self.run_sync("shopify_to_ac2")  # the sign-up is now a pending (waiting) customer
        self.shopify.unindexed.add(gid(94))  # Shopify's phone search does not show it yet
        self.staff(sg(94), T0 + timedelta(minutes=1))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual((self.links(), self.shopify.created), ({gid(94): sg(94)}, []))
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 1)

    def test_an_existing_customer_who_is_not_a_sign_up_is_held_with_an_alert(self):
        # An older non-member Shopify account with the phone: no sign-up time
        # within the window (its createdAt stands in), so no link and no create.
        self.shopify.add(replace(customer(95, tags=()), created_at=T0 - timedelta(days=30), registration_at=None, registration_value=None))
        self.staff(sg(95), T0)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual((self.links(), self.shopify.created), ({}, []))
        self.assertIn("memberno_exists_outside_window", result["alerts"])

    def test_a_held_member_without_a_sign_up_time_ends_after_the_bounded_wait(self):
        self.sign_up(96, pending_metafield=True)
        self.shopify.customers[gid(96)] = replace(self.shopify.customers[gid(96)], created_at=T0 - timedelta(days=2))
        self.staff(sg(96), T0)
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS - 1):
            self.assertIn("ac2_member_held_for_sign_up", self.run_sync("ac2_to_shopify")["alerts"])
        result = self.run_sync("ac2_to_shopify")  # wait spent: createdAt decides, two days off
        self.assertIn("memberno_exists_outside_window", result["alerts"])
        self.assertEqual((self.links(), self.shopify.created), ({}, []))
        self.assertEqual(self.ac2_pending(sg(96)), ("SKIPPED", "memberno_exists_outside_window"))

    def test_customer_linked_to_another_member_alerts_on_both_sides(self):
        self.sign_up(97)
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(97), "OTHER97", "matched", "test")
        self.staff(sg(97), T0)
        result = self.run_sync("ac2_to_shopify")
        self.assertIn("shopify_customer_linked_to_other_member", result["alerts"])
        self.assertEqual(self.shopify.created, [])
        # Shopify -> AC2: the exact MemberNo is already linked to another customer.
        self.sign_up(98)
        self.ac2.add(sync.Ac2Member(sg(98), "SOMEONE ELSE", None, created_at=T0))
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(88), sg(98), "matched", "test")
        result = self.run_sync("shopify_to_ac2")
        self.assertIn("member_linked_to_other_customer", result["alerts"])

    def test_name_rule_still_decides_when_the_member_no_is_not_a_phone_form(self):
        # A till member with a generator number and the phone in MobilePhone:
        # the round 7 name rule (unchanged): another name -> a separate customer.
        self.sign_up(99)
        self.ac2.add(sync.Ac2Member("M000999", "Charlie Holder", local(99)), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(result["counts"]["shopify_separate_customer_created_without_phone"], 1)



class Ac2SideMatchesShopifySideTests(SyncTestCase):
    """Round 9 (Owner, 22 Oct 2026): the AC2 -> Shopify time link applies the
    same rules as Shopify -> AC2. G2 M2: when both forms of one phone (65 + 8
    digits and the 8 digits) are MemberNos, neither side ever links; both
    alert memberno_conflict with the MemberNo masked to its last 3
    characters. G2 L6: the hold used when more sign-ups are pending than one
    run reads ends after the same bounded wait as the sign-up-time hold, with
    an alert naming it stuck. G2 L7: the AC2 side's window boundary."""

    def setUp(self):
        super().setUp()
        self.now = T0 + timedelta(hours=1)
        self.run_sync("ac2_to_shopify")  # ChangeLog watermark

    sign_up = StaffMemberBeforeSignUpLinkTests.sign_up
    staff = StaffMemberBeforeSignUpLinkTests.staff
    ac2_pending = StaffMemberBeforeSignUpLinkTests.ac2_pending

    def legacy(self, member_no: str, mobile_phone: str | None = None) -> None:
        # A record keyed long before the sync, not in the ChangeLog window.
        self.ac2.add(sync.Ac2Member(member_no, "Legacy Holder", mobile_phone, created_at=T0 - timedelta(days=400)))

    def assert_conflict(self, result: dict, n: int) -> None:
        self.assertIn("memberno_conflict", result["alerts"])
        flagged = [r for r in result["refs"] if r["code"] == "memberno_conflict"]
        self.assertTrue(flagged)
        for ref in flagged:
            self.assertEqual(ref["member_no_masked"].lstrip("*"), sg(n)[-3:])
        self.assert_no_pii(result)

    def test_g2_a_sign_up_read_first_then_staff_key_the_other_form(self):
        self.legacy(local(70))
        self.sign_up(70)
        first = self.run_sync("shopify_to_ac2")
        self.assertIn("memberno_exists_outside_window", first["alerts"])
        self.staff(sg(70), T0 + timedelta(minutes=2))
        self.now += timedelta(hours=1)
        result = self.run_sync("both")
        self.assertEqual((self.links(), self.shopify.created), ({}, []))
        self.assert_conflict(result, 70)
        self.assertNotIn("memberno_and_created_time_match", result["counts"])
        self.assertEqual(self.ac2_pending(sg(70)), ("SKIPPED", "memberno_conflict"))

    def test_g2_b_both_forms_before_the_first_read_alert_on_both_sides(self):
        self.legacy(local(71))
        self.sign_up(71)
        self.staff(sg(71), T0 + timedelta(minutes=2))
        result = self.run_sync("both")
        self.assertEqual((self.links(), self.shopify.created, self.ac2.creates), ({}, [], []))
        self.assert_conflict(result, 71)
        self.assertEqual(result["counts"]["memberno_conflict"], 2)  # one per side
        self.assertEqual(self.ac2_pending(sg(71)), ("SKIPPED", "memberno_conflict"))

    def test_g2_c_ac2_side_first_never_links_so_shopify_side_alerts_too(self):
        # The other way round: the legacy record has 65 + 8 digits, staff key 8.
        self.legacy(sg(72))
        self.sign_up(72)
        self.staff(local(72), T0 - timedelta(minutes=2))
        first = self.run_sync("ac2_to_shopify")
        self.assertEqual((self.links(), self.shopify.created), ({}, []))
        self.assert_conflict(first, 72)
        second = self.run_sync("shopify_to_ac2")
        self.assertNotIn("shopify_already_linked", second["counts"])
        self.assert_conflict(second, 72)

    def test_both_forms_with_no_shopify_customer_never_create(self):
        self.legacy(local(73))
        self.staff(sg(73), T0)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual((self.links(), self.shopify.created), ({}, []))
        self.assert_conflict(result, 73)

    def test_one_form_only_still_links_by_time(self):
        # Round 10 (G2 L9): the other record holds the same phone in
        # MobilePhone, so the phone lookup returns it; only exact MemberNo
        # forms count towards a conflict, so it still links by time.
        self.legacy("TILL74", mobile_phone=local(74))
        self.sign_up(74)
        self.staff(sg(74), T0 + timedelta(minutes=2))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.links(), {gid(74): sg(74)})
        self.assertEqual(result["alerts"], [])
        self.assertEqual(self.ac2_pending(sg(74)), ("DONE", "memberno_and_created_time_match"))

    def test_a_member_no_with_the_phone_embedded_is_not_a_conflict(self):
        # Round 10 (G2 L9, probe P2): the 8 digits embedded in a longer
        # MemberNo are a phone hit, not a MemberNo form of the phone.
        self.legacy(local(68) + "JO")
        self.sign_up(68)
        self.staff(sg(68), T0 - timedelta(minutes=2))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.links(), {gid(68): sg(68)})
        self.assertNotIn("memberno_conflict", result["counts"])
        self.assertEqual(result["alerts"], [])

    def test_hold_for_an_unread_pending_sign_up_ends_as_stuck_after_the_bounded_wait(self):
        for n in range(75, 80):
            self.sign_up(n)
        self.run_sync("shopify_to_ac2")  # five waiting sign-ups
        # The target sign-up is the newest pending row (outside a bound of 3)
        # and not yet in Shopify's phone search.
        with self.ledger.tx() as conn:
            conn.execute("UPDATE shopify_pending SET updated_at = ? WHERE customer_gid = ?", ("2099-01-01T00:00:00Z", gid(79)))
        self.shopify.unindexed.add(gid(79))
        self.staff(sg(79), T0 + timedelta(minutes=1))
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS - 1):
            held = self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)
            self.assertIn("ac2_member_held_for_sign_up", held["alerts"])
            self.assertEqual((self.links(), self.shopify.created), ({}, []))
            self.assertEqual(self.ac2_pending(sg(79)), ("PENDING", "held_for_sign_up"))
        stuck = self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)
        self.assertIn("ac2_member_hold_stuck", stuck["alerts"])
        self.assertEqual([r["member_no_masked"].lstrip("*") for r in stuck["refs"] if r["code"] == "ac2_member_hold_stuck"], [sg(79)[-3:]])
        self.assertEqual((self.links(), self.shopify.created), ({}, []))
        self.assertEqual(self.ac2_pending(sg(79)), ("SKIPPED", "ac2_member_hold_stuck"))
        self.assertEqual(self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)["alerts"], [])  # alerted once
        self.assert_no_pii(stuck)
        # Shopify -> AC2 still links the sign-up by time on its own.
        self.run_sync("shopify_to_ac2")
        self.assertEqual(self.links(), {gid(79): sg(79)})
        # Round 10 (G2 L8): once the backlog clears the stuck row is
        # re-queued and closes as already linked; nothing is created.
        healed = self.run_sync("ac2_to_shopify")  # 4 pending now, bound 100
        self.assertEqual(healed["counts"]["ac2_member_hold_requeued"], 1)
        self.assertEqual(self.ac2_pending(sg(79)), ("DONE", "already_linked"))
        self.assertEqual(self.shopify.created, [])

    def test_a_walk_in_stuck_during_a_backlog_is_requeued_and_created_once_it_clears(self):
        # Round 10 (G2 L8, probe P3): a genuine walk-in (no Shopify customer,
        # no sign-up) held while more sign-ups are pending than one run reads.
        for n in range(75, 80):
            self.sign_up(n)
        self.run_sync("shopify_to_ac2")  # five waiting sign-ups, a bound of 3
        self.staff(sg(60), T0 + timedelta(minutes=1))
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS - 1):
            self.assertIn("ac2_member_held_for_sign_up", self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)["alerts"])
        self.assertIn("ac2_member_hold_stuck", self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)["alerts"])
        # Backlog still there: stays stuck and quiet, not re-queued.
        quiet = self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)
        self.assertEqual(quiet["alerts"], [])
        self.assertNotIn("ac2_member_hold_requeued", quiet["counts"])
        self.assertEqual((self.ac2_pending(sg(60)), self.shopify.created), (("SKIPPED", "ac2_member_hold_stuck"), []))
        # The backlog clears (every pending sign-up fits the bound; exactly
        # 5 of 5 is clear, the `sure` rule): re-queued with a fresh wait and
        # decided in the same run, created once.
        healed = self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=5)
        self.assertEqual(healed["counts"]["ac2_member_hold_requeued"], 1)
        self.assertEqual([r["ref"] for r in healed["refs"] if r["code"] == "ac2_member_hold_requeued"], [self.ledger.ref("m", sync.member_no_key(sg(60)))])
        self.assertEqual(healed["counts"]["shopify_customer_created"], 1)
        self.assertEqual(len(self.shopify.created), 1)
        self.assertEqual(self.ac2_pending(sg(60)), ("DONE", "shopify_customer_created"))
        self.assert_no_pii(healed)
        again = self.run_sync("ac2_to_shopify")
        self.assertEqual(len(self.shopify.created), 1)
        self.assertNotIn("ac2_member_hold_requeued", again["counts"])

    def test_a_requeued_walk_in_is_held_again_if_the_backlog_returns(self):
        # Re-queue resets checks: a new backlog gets the same bounded wait,
        # never an unbounded hold and never a create while unsure.
        for n in range(75, 80):
            self.sign_up(n)
        self.run_sync("shopify_to_ac2")
        self.staff(sg(61), T0 + timedelta(minutes=1))
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS):
            self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)
        self.assertEqual(self.ac2_pending(sg(61)), ("SKIPPED", "ac2_member_hold_stuck"))
        # The bound covers the backlog but Shopify creates are off: re-queued,
        # stays PENDING (would_create) with checks 0.
        result = self.run_sync("ac2_to_shopify", shopify_create_enabled=False)
        self.assertEqual(result["counts"]["ac2_member_hold_requeued"], 1)
        self.assertEqual(self.ac2_pending(sg(61))[0], "PENDING")
        checks = lambda: self.ledger.conn.execute("SELECT checks FROM ac2_pending WHERE member_no = ?", (sg(61),)).fetchone()[0]
        self.assertEqual(checks(), 0)
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS - 1):
            self.assertIn("ac2_member_held_for_sign_up", self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)["alerts"])
        self.assertIn("ac2_member_hold_stuck", self.run_sync("ac2_to_shopify", max_pending_rechecks_per_run=3)["alerts"])
        self.assertEqual(self.shopify.created, [])

    def test_ac2_side_exactly_sixty_minutes_links_and_a_millisecond_more_does_not(self):
        self.now = T0 + timedelta(hours=2)
        cases = ((80, timedelta(minutes=60)), (81, -timedelta(minutes=60)),
                 (82, timedelta(minutes=60, milliseconds=1)), (83, -timedelta(minutes=60, milliseconds=1)))
        for n, offset in cases:
            self.sign_up(n)
            self.staff(sg(n), T0 + offset)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual(self.links(), {gid(80): sg(80), gid(81): sg(81)})
        self.assertEqual(result["counts"]["memberno_and_created_time_match"], 2)
        self.assertEqual(result["counts"]["memberno_exists_outside_window"], 2)
        self.assertEqual(self.shopify.created, [])


class PendingBudgetTests(SyncTestCase):
    """Round 8, G2 M1: pending sign-ups (waiting for staff, or past the
    cutoff while AC2 creates are off) never use max_shopify_customers_per_run,
    so new sign-ups are always read; their own bound is
    max_pending_rechecks_per_run (alert when more are pending). A sign-up past
    the cutoff while creates are off raises an alert with the MemberNo masked
    to its last 3 characters."""

    def pending_count(self) -> int:
        return self.ledger.conn.execute("SELECT COUNT(*) FROM shopify_pending").fetchone()[0]

    def test_twenty_five_waiting_sign_ups_never_block_a_new_one(self):
        self.now = T0 + timedelta(hours=1)
        for n in range(1, 26):
            self.shopify.add(customer(n))
        first = self.run_sync("shopify_to_ac2")
        self.assertEqual((first["counts"]["waiting_for_staff"], first["counts"]["shopify_customer_budget_reached"]), (20, 1))
        self.shopify.add(customer(26))
        self.now += timedelta(minutes=5)
        second = self.run_sync("shopify_to_ac2")
        self.assertEqual(self.pending_count(), 26)  # the last 5 and the new one were read
        self.assertNotIn("shopify_customer_budget_reached", second["counts"])
        self.assertEqual(second["counts"]["waiting_for_staff"], 26)

    def test_past_cutoff_with_creates_off_alerts_with_the_masked_member_no(self):
        self.now = T0 + timedelta(days=1)
        self.shopify.add(customer(40))
        self.shopify.add(customer(41, phone=None))
        result = self.run_sync("shopify_to_ac2", ac2_create_enabled=False)
        self.assertIn("sign_up_past_cutoff_create_disabled", result["alerts"])
        self.assertEqual(result["counts"]["sign_up_past_cutoff_create_disabled"], 2)
        flagged = [r for r in result["refs"] if r["code"] == "sign_up_past_cutoff_create_disabled"]
        self.assertEqual(sorted(r.get("member_no_masked", "") for r in flagged), ["", "*******" + sg(40)[-3:]])
        self.assertEqual(self.ac2.creates, [])
        self.assert_no_pii(result)
        # Still pending, so it alerts again next run (and never blocks new ones).
        self.now += timedelta(hours=1)
        self.assertIn("sign_up_past_cutoff_create_disabled", self.run_sync("shopify_to_ac2", ac2_create_enabled=False)["alerts"])

    def test_rechecks_have_their_own_bound_and_alert_beyond_it(self):
        self.assertEqual(make_config().max_pending_rechecks_per_run, 100)
        self.now = T0 + timedelta(hours=1)
        for n in range(1, 6):
            self.shopify.add(customer(n))
        self.run_sync("shopify_to_ac2")
        result = self.run_sync("shopify_to_ac2", max_pending_rechecks_per_run=3)
        self.assertIn("shopify_pending_backlog", result["alerts"])
        self.assertEqual(result["counts"]["waiting_for_staff"], 3)
        for bad in (0, 501, "100", True):
            with self.subTest(bad=bad), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(max_pending_rechecks_per_run=bad)
            self.assertEqual(caught.exception.code, "sync_config_max_pending_rechecks_per_run_invalid")


# --------------------------------------------------------------------------
# Go-live closure matrix (G0 go-live design section 4.4, Owner SCOPE-CONFIRMED
# 29 Oct 2026, plus the V4 and V9 rows, and ML10d, V9b, T6 from the G2 #281 LATER notes,
# and V9c, V9d for the Owner's login decision of 30 Oct 2026). One row = one test, named
# ``test_row_<ID>_...``; the PowerShell rows (B1-B16, B18, T1-T3, T6) are in
# tests/test_member_sync_bridge_ps.py. GoLiveRowCoverageTests proves every
# row has its test. Fake data only: reserved phones, example.com, fake IDs.
# --------------------------------------------------------------------------

GOLIVE_ROWS = (
    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10", "B11", "B12", "B13", "B14", "B15", "B16", "B17", "B18",
    "L1", "L2", "L3", "L4", "L5", "L6",
    "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9",
    "D1", "D2", "D3", "D4", "D5", "D6", "D7",
    "ML10a", "ML10b", "ML10c", "ML10d",
    "T1", "T2", "T3", "T4", "T5", "T6",
    "V4", "V9", "V9b", "V9c", "V9d",
)
FAKE_OWNER_USER = "ownerfake01"
# Owner 30 Oct 2026 ("Use my login"): the live config logs in with the
# Owner's own AutoCount user. DEDICATED_KEYS is the other mode (V9 as built).
LIVE_KEYS = {
    "autocount_book": PROD_BOOK, "ac2_credential_target": "XB-AC2-MemberSync", "ac2_login_mode": "owner_shared",
    "ac2_create_member_type": "Default",
}
DEDICATED_KEYS = {**LIVE_KEYS, "ac2_login_mode": "dedicated", "ac2_refused_user_ids": [FAKE_OWNER_USER]}
V2_META_CHECK = "CHECK (autocount_book <> 'AED_XBOUNDARIES'));"


def live_config(**overrides) -> sync.SyncConfig:
    return make_config(**{**LIVE_KEYS, **overrides})


class GoLiveRowCoverageTests(unittest.TestCase):
    def test_every_closure_matrix_row_has_its_test(self):
        sources = (Path(__file__).read_text(encoding="utf-8") + (ROOT / "tests" / "test_member_sync_bridge_ps.py").read_text(encoding="utf-8"))
        found = re.findall(r"def test_row_([A-Za-z0-9]+)_", sources)
        self.assertEqual(sorted(set(found)), sorted(GOLIVE_ROWS))
        self.assertEqual(len(found), len(set(found)))  # one test per row


class GoLiveTestCase(SyncTestCase):
    """A live-book ledger (same engine, book = AED_XBOUNDARIES)."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-live-"))
        self.now = T0 + timedelta(days=1)
        self.ledger = sync.SyncLedger(self.temp, clock=lambda: self.now, check_custody=False)
        self.ledger.init_meta(autocount_book=PROD_BOOK, store_gid=STORE, shop_domain=DOMAIN)
        self.shopify = FakeShopify()
        self.ac2 = FakeAc2()
        with self.ledger.tx() as conn:
            self.ledger.set_watermark(conn, "initial_link_completed_at", dl.ts(CUTOVER))

    def engine(self, **overrides) -> sync.SyncEngine:
        settings = {**LIVE_KEYS, "ac2_create_enabled": True, "shopify_create_enabled": True, "changelog_xml_column": "Content"}
        settings.update(overrides)
        return sync.SyncEngine(self.ledger, make_config(**settings), shopify=self.shopify, ac2=self.ac2)

    def pending(self, n: int):
        row = self.ledger.conn.execute("SELECT state, code FROM shopify_pending WHERE customer_gid = ?", (gid(n),)).fetchone()
        return None if row is None else tuple(row)

    def signup(self, n: int, at: datetime = T0, **kwargs) -> sync.ShopifyCustomer:
        return self.shopify.add(replace(customer(n, registered=at, **kwargs), created_at=at))

    def private_root(self) -> Path:
        holder = Path(tempfile.mkdtemp(prefix="xb-member-sync-root-"))
        self.addCleanup(shutil.rmtree, holder, True)
        root = holder / "state"
        dl.prepare_state_root(root, environ={})
        return root

    def cli(self, *args: str, config: dict) -> tuple[int, dict]:
        path = self.temp / "cli-config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        out = io.StringIO()
        real = dl.validate_state_root
        # Temp folders sit under %LOCALAPPDATA% on Windows, which the location
        # rule refuses for a real root; the custody check itself still runs.
        with redirect_stdout(out), mock.patch.object(dl, "validate_state_root", side_effect=lambda path, environ=None: real(path, environ={})):
            code = sync.main([args[0], "--config", str(path), *args[1:]])
        return code, json.loads(out.getvalue())



class GoLiveMatrixTests(GoLiveTestCase):
    # -- book (Python layer) ---------------------------------------------------
    def test_row_B17_core_refuses_the_production_environment(self):
        self.assertEqual(dl.AUTHORISED_REAL_ENVIRONMENTS, frozenset({"development"}))
        ledger = dl.Ledger(self.private_root())
        try:
            with self.assertRaises(dl.DirectLocalError) as caught:
                dl.Processor(ledger).init_ledger(mode="connector", environment="production", store_gid=STORE, shop_domain=DOMAIN)
            self.assertEqual(caught.exception.code, "production_environment_not_authorised")
        finally:
            ledger.close()

    # -- ledger ------------------------------------------------------------------
    def test_row_L1_fresh_ledger_is_v3_and_holds_the_live_book(self):
        self.assertEqual(sync.SCHEMA_VERSION, 3)
        self.assertEqual(self.ledger.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.ledger.meta()["autocount_book"], PROD_BOOK)
        self.assertEqual(self.ledger.init_meta(autocount_book=PROD_BOOK, store_gid=STORE, shop_domain=DOMAIN), {"status": "existing"})

    def build_old_ledger(self, version: int) -> Path:
        import sqlite3

        root = Path(tempfile.mkdtemp(prefix=f"xb-member-sync-v{version}-"))
        self.addCleanup(shutil.rmtree, root, True)
        schema = sync.LEDGER_SCHEMA.replace("CHECK (autocount_book IN ('AED_TESTINGAPI', 'AED_XBOUNDARIES')));", V2_META_CHECK)
        self.assertIn(V2_META_CHECK, schema)
        if version == 1:
            schema = schema.replace(", intent_digest TEXT", "").replace("CREATE TABLE changelog_seen(id INTEGER PRIMARY KEY, seen_at TEXT NOT NULL);\n", "")
            schema = schema.replace("created_at, intent_digest ON writes", "created_at ON writes")
        conn = sqlite3.connect(root / sync.LEDGER_FILE)
        conn.executescript(schema)
        conn.execute("INSERT INTO meta(id, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at) VALUES (1, 'sync-old', ?, ?, ?, ?, 't')", (TEST_BOOK, STORE, DOMAIN, "a" * 64))
        conn.execute("INSERT INTO links VALUES (?, ?, ?, 'matched', 'phone_and_name_match', 't')", (gid(1), sg(1), sg(1)))
        conn.execute("INSERT INTO writes(write_id, direction, customer_gid, member_no, member_no_key, state, created_at, updated_at) VALUES ('write-1', 'shopify_to_ac2', ?, ?, ?, 'DONE', 't', 't')", (gid(2), sg(2), sg(2)))
        conn.execute("INSERT INTO watermarks VALUES ('ac2_changelog_id', '1000', 't')")
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
        conn.close()
        return root

    def dump_rows(self, ledger: sync.SyncLedger) -> dict:
        return {name: [tuple(r) for r in ledger.conn.execute(f"SELECT * FROM {name} ORDER BY 1")] for name in ("meta", "links", "writes", "watermarks")}

    def test_row_L2_v2_test_ledger_is_bumped_to_v3_rows_unchanged(self):
        import sqlite3

        root = self.build_old_ledger(2)
        conn = sqlite3.connect(root / sync.LEDGER_FILE)
        before = {name: [tuple(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY 1")] for name in ("meta", "links", "writes", "watermarks")}
        conn.close()
        ledger = sync.SyncLedger(root, check_custody=False)
        try:
            self.assertEqual(ledger.conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(self.dump_rows(ledger), before)
            self.assertIn(V2_META_CHECK.rstrip(";"), ledger.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'meta'").fetchone()[0])
            sync.SyncEngine(ledger, make_config(), shopify=FakeShopify(), ac2=FakeAc2())  # the test config still binds
        finally:
            ledger.close()

    def test_row_L3_v1_ledger_migrates_through_v2_to_v3(self):
        root = self.build_old_ledger(1)
        ledger = sync.SyncLedger(root, check_custody=False)
        try:
            self.assertEqual(ledger.conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNone(ledger.conn.execute("SELECT intent_digest FROM writes").fetchone()[0])
            self.assertEqual(ledger.conn.execute("SELECT COUNT(*) FROM changelog_seen").fetchone()[0], 0)
            self.assertEqual(ledger.link_for_gid(gid(1))["member_no"], sg(1))
        finally:
            ledger.close()

    def test_row_L4_live_config_and_test_ledger_never_meet(self):
        test_ledger_root = Path(tempfile.mkdtemp(prefix="xb-member-sync-test-ledger-"))
        self.addCleanup(shutil.rmtree, test_ledger_root, True)
        test_ledger = sync.SyncLedger(test_ledger_root, check_custody=False)
        try:
            test_ledger.init_meta(autocount_book=TEST_BOOK, store_gid=STORE, shop_domain=DOMAIN)
            for ledger, config in ((test_ledger, live_config()), (self.ledger, make_config())):
                with self.subTest(book=config.autocount_book), self.assertRaises(dl.DirectLocalError) as caught:
                    sync.SyncEngine(ledger, config, shopify=self.shopify, ac2=self.ac2)  # run and initial-link
                self.assertEqual(caught.exception.code, "sync_ledger_binding_mismatch")
        finally:
            test_ledger.close()
        root = self.private_root()
        ledger = sync.SyncLedger(root)
        ledger.init_meta(autocount_book=TEST_BOOK, store_gid=STORE, shop_domain=DOMAIN)
        ledger.close()
        for command in ("status", "digest"):
            with self.subTest(command=command):
                self.assertEqual(self.cli(command, config=config_value(**LIVE_KEYS, state_root=str(root))), (2, {"status": "refused", "code": "sync_ledger_binding_mismatch"}))
        self.assertEqual(self.cli("status", config=config_value(state_root=str(root)))[0], 0)  # its own config still reads it

    def test_row_L5_meta_links_and_writes_are_immutable(self):
        import sqlite3

        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(5), sg(5), "matched", "x")
            conn.execute("INSERT INTO writes(write_id, direction, member_no, member_no_key, state, created_at, updated_at) VALUES ('write-5', 'shopify_to_ac2', 'M5', 'm5', 'DONE', 't', 't')")
        for statement in (
            "UPDATE meta SET autocount_book = 'AED_TESTINGAPI'", "DELETE FROM meta", "UPDATE links SET member_no = 'X'", "DELETE FROM links", "DELETE FROM writes",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                self.ledger.conn.execute(statement)

    def test_row_L6_unknown_schema_version_is_refused(self):
        import sqlite3

        root = Path(tempfile.mkdtemp(prefix="xb-member-sync-v4-"))
        self.addCleanup(shutil.rmtree, root, True)
        conn = sqlite3.connect(root / sync.LEDGER_FILE)
        conn.execute("PRAGMA user_version=4")
        conn.close()
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.SyncLedger(root, check_custody=False)
        self.assertEqual(caught.exception.code, "sync_ledger_schema_unsupported")

    # -- switches and scope ------------------------------------------------------
    def test_row_S1_shadow_mode_links_and_counts_but_writes_nothing(self):
        shadow = {"ac2_create_enabled": False, "shopify_create_enabled": False}
        self.assertEqual(self.run_sync("ac2_to_shopify", **shadow)["counts"], {"changelog_watermark_initialised": 1})
        self.signup(1)  # past the cutoff, no AC2 member
        self.signup(3)
        self.ac2.add(sync.Ac2Member(sg(3), "SOMEONE", None, created_at=T0 + timedelta(minutes=2)), log=True)  # staff keyed
        self.ac2.add(sync.Ac2Member(sg(2), "Walk In", sg(2)), log=True)  # a walk-in: no Shopify customer
        first = self.run_sync("both", **shadow)
        second = self.run_sync("both", **shadow)
        self.assertEqual((self.ac2.creates, self.shopify.created), ([], []))
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 0)
        self.assertEqual(self.links(), {gid(3): sg(3)})
        for result in (first, second):
            self.assertEqual((result["writes"], result["counts"]["ac2_create_disabled"], result["counts"]["shopify_create_disabled"]), (0, 1, 1))

    def test_row_S2_ac2_create_on_past_cutoff_creates_once_with_the_live_type(self):
        self.signup(4)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual([(no, p["member_type"], p["profile"]) for no, p in self.ac2.creates], [(sg(4), "Default", "sync_p1")])
        self.assertEqual((self.links(), result["status"], result["alerts"]), ({gid(4): sg(4)}, "completed_with_alerts", ["created_after_wait"]))
        self.assertEqual(self.run_sync("shopify_to_ac2")["counts"].get("ac2_member_created", 0), 0)

    def test_row_S3_no_usable_phone_is_skipped_with_one_staff_alert(self):
        self.now = T0 + timedelta(hours=1)  # before the cutoff it waits for staff, as any sign-up
        self.signup(5, phone=None)
        self.signup(6, phone="+" + "1" * 3)  # invalid: treated as absent
        self.assertEqual(self.run_sync("shopify_to_ac2")["counts"]["waiting_for_staff"], 2)
        self.now = T0 + timedelta(days=1)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.ac2.creates, self.ac2.generator_calls), ([], 0))
        self.assertEqual((self.pending(5), self.pending(6)), (("SKIPPED", "no_phone_sign_up_needs_staff"),) * 2)
        self.assertEqual((result["alerts"], result["counts"]["no_phone_sign_up_needs_staff"]), (["no_phone_sign_up_needs_staff"], 2))
        flagged = [r for r in result["refs"] if r["code"] == "no_phone_sign_up_needs_staff"]
        self.assertEqual(sorted(r["ref"] for r in flagged), sorted([self.ledger.ref("c", gid(5)), self.ledger.ref("c", gid(6))]))
        self.assert_no_pii(result)
        self.now += timedelta(hours=1)
        self.assertEqual(self.run_sync("shopify_to_ac2")["alerts"], [])  # SKIPPED: raised once

    def test_row_S4_create_without_phone_is_refused_on_the_live_book(self):
        with self.assertRaises(dl.DirectLocalError) as caught:
            live_config(ac2_create_without_phone=True)
        self.assertEqual(caught.exception.code, "sync_config_ac2_create_without_phone_live_refused")
        self.assertEqual(self.cli("check-config", config=config_value(**LIVE_KEYS, ac2_create_without_phone=True)), (2, {"status": "refused", "code": "sync_config_ac2_create_without_phone_live_refused"}))
        self.assertFalse(live_config().ac2_create_without_phone)
        self.assertFalse(live_config(ac2_create_without_phone=False).ac2_create_without_phone)
        self.assertTrue(make_config().ac2_create_without_phone)  # the test book keeps phase 1
        self.assertFalse(make_config(ac2_create_without_phone=False).ac2_create_without_phone)
        with self.assertRaises(dl.DirectLocalError):
            make_config(ac2_create_without_phone="yes")

    def scoped_client(self, scopes, *, create_enabled):
        session = {"shop": {"id": STORE, "myshopifyDomain": DOMAIN}, "currentAppInstallation": {"accessScopes": [{"handle": s} for s in scopes]}}
        responses = [_graphql(session)]
        return sync.SyncShopifyClient(
            create_enabled=create_enabled, shop_domain=DOMAIN, api_version="2026-07", token_provider=direct.StaticTokenProvider("shpat_synthetic_token_value_0001"),
            transport=lambda url, body, headers, timeout: responses.pop(0), sleep=lambda s: None,
        )

    def test_row_S5_creates_off_with_write_scope_refuses_every_run(self):
        with self.assertRaises(direct.ShopifyDirectError) as caught:
            self.scoped_client(("read_customers", "write_customers"), create_enabled=False).verify_session(store_gid=STORE)
        self.assertEqual(caught.exception.code, "shopify_token_scope_not_expected")

    def test_row_S6_creates_on_with_read_scope_only_is_refused(self):
        with self.assertRaises(direct.ShopifyDirectError) as caught:
            self.scoped_client(("read_customers",), create_enabled=True).verify_session(store_gid=STORE)
        self.assertEqual(caught.exception.code, "shopify_token_scope_not_expected")
        self.scoped_client(("read_customers", "write_customers"), create_enabled=True).verify_session(store_gid=STORE)

    def test_row_S7_write_budget_reached_counts_and_writes_no_more(self):
        for n in (7, 8, 9):
            self.signup(n)
        result = self.run_sync("shopify_to_ac2", max_writes_per_run=1)
        self.assertEqual((len(self.ac2.creates), result["writes"], result["counts"]["max_writes_per_run_reached"]), (1, 1, 2))

    def test_row_S8_initial_link_apply_needs_its_switch_before_any_login(self):
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.engine().initial_link(apply=True)
        self.assertEqual(caught.exception.code, "initial_link_disabled")
        missing = config_value(**LIVE_KEYS, state_root=str(self.temp / "no-such-root"))
        self.assertEqual(self.cli("initial-link", "--apply", config=missing), (2, {"status": "refused", "code": "initial_link_disabled"}))
        self.assertEqual(self.links(), {})

    def test_row_S9_no_sign_up_is_read_until_the_initial_link_completes(self):
        with self.ledger.tx() as conn:
            conn.execute("DELETE FROM watermarks WHERE name = 'initial_link_completed_at'")
        self.signup(10)
        result = self.run_sync("shopify_to_ac2")
        self.assertIn("initial_link_not_complete", result["alerts"])
        self.assertEqual((self.ac2.lookups, self.ac2.creates, self.shopify.searches), (0, [], []))

    # -- decisions on the live book -----------------------------------------------
    def test_row_D1_member_no_and_created_time_within_the_window_links(self):
        self.signup(11)
        self.ac2.add(sync.Ac2Member(sg(11), "STAFF TYPED", None, created_at=T0 + timedelta(minutes=59)))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates, result["counts"]["memberno_and_created_time_match"]), ({gid(11): sg(11)}, [], 1))

    def test_row_D2_one_millisecond_outside_the_window_alerts_never_creates(self):
        self.signup(12)
        self.ac2.add(sync.Ac2Member(sg(12), "STAFF TYPED", None, created_at=T0 + timedelta(minutes=60, milliseconds=1)))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates), ({}, []))
        self.assertIn("memberno_exists_outside_window", result["alerts"])
        self.assertEqual(self.pending(12), ("SKIPPED", "memberno_exists_outside_window"))

    def test_row_D3_both_number_forms_are_a_conflict_both_ways(self):
        self.signup(13)
        self.ac2.add(sync.Ac2Member(sg(13), "STAFF ONE", None, created_at=T0))
        self.ac2.add(sync.Ac2Member(local(13), "STAFF TWO", None, created_at=T0))
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), self.ac2.creates, self.pending(13)), ({}, [], ("SKIPPED", "memberno_conflict")))
        self.assertIn("memberno_conflict", result["alerts"])
        self.run_sync("ac2_to_shopify")  # watermark
        self.ac2.add(sync.Ac2Member(sg(14), "STAFF ONE", sg(14), created_at=T0), log=True)
        self.ac2.add(sync.Ac2Member(local(14), "STAFF TWO", None, created_at=T0))
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual((self.shopify.created, result["counts"].get("memberno_conflict")), ([], 1))
        self.assertNotIn(sg(14), self.links().values())

    def test_row_D4_email_alone_never_links(self):
        self.signup(15, email="alpha@example.com")
        self.ac2.add(sync.Ac2Member("TILLX15", "Alpha Tester", None, "alpha@example.com"))
        self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), [no for no, _ in self.ac2.creates]), ({gid(15): sg(15)}, [sg(15)]))
        self.run_sync("ac2_to_shopify")  # watermark
        self.shopify.add(customer(16, email="bravo@example.com", tags=()))
        self.ac2.add(sync.Ac2Member("TILLX16", "Alpha Tester", None, "bravo@example.com"), log=True)
        result = self.run_sync("ac2_to_shopify")
        self.assertEqual((result["counts"].get("ac2_member_without_phone"), self.shopify.created), (1, []))
        self.assertNotIn("TILLX16", self.links().values())

    def test_row_D5_open_uncertain_write_holds_the_customer(self):
        now = dl.ts(self.now)
        with self.ledger.tx() as conn:
            conn.execute(
                "INSERT INTO writes(write_id, direction, customer_gid, member_no, member_no_key, state, code, created_at, updated_at, intent_digest) "
                "VALUES ('write-d5', 'shopify_to_ac2', ?, 'M000901', 'm000901', 'UNCERTAIN', 'writer_timeout', ?, ?, 'sha256:x')", (gid(17), now, now),
            )
        self.signup(17)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.ac2.creates, self.pending(17)), ([], ("SKIPPED", "customer_held_by_open_write")))
        self.assertIn("customer_held_by_open_write", result["alerts"])

    def uncertain_create(self, n: int, found_name: str) -> None:
        def taken_by(member_no, payload, *, write_id):
            self.ac2.creates.append((member_no, dict(payload)))
            self.ac2.add(sync.Ac2Member(member_no, found_name, payload["mobile_phone"] if found_name == payload["name"] else None))
            return "WRITE_OUTCOME_UNCERTAIN", "writer_failed_after_claim"

        self.ac2.create_member = taken_by
        self.signup(n)
        self.run_sync("shopify_to_ac2")
        del self.ac2.create_member

    def test_row_D6_uncertain_write_found_with_equal_digest_resolves_and_links(self):
        self.uncertain_create(18, "Alpha Tester")
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), result["counts"]["uncertain_write_resolved_linked"]), ({gid(18): sg(18)}, 1))
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "RESOLVED")
        self.assertEqual(len(self.ac2.creates), 1)

    def test_row_D7_uncertain_write_found_with_another_digest_stays_uncertain(self):
        self.uncertain_create(19, "Kilo Other")
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual((self.links(), result["counts"]["uncertain_write_identity_mismatch"]), ({}, 1))
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "UNCERTAIN")
        self.assertEqual(len(self.ac2.creates), 1)

    # -- L10 -----------------------------------------------------------------------
    def walk_ins(self, numbers) -> None:
        for n in numbers:
            self.ac2.add(sync.Ac2Member(sg(n), "Walk In", sg(n)), log=True)

    def test_row_ML10a_a_linkable_row_is_decided_behind_twenty_would_create_rows(self):
        off = {"shopify_create_enabled": False}
        self.run_sync("ac2_to_shopify", **off)  # watermark
        self.walk_ins(range(1, 21))
        self.assertEqual(self.run_sync("ac2_to_shopify", **off)["counts"]["shopify_create_disabled"], 20)
        self.shopify.add(customer(21, tags=()))
        self.ac2.add(sync.Ac2Member("TILL21", "Alpha Tester", local(21)), log=True)
        result = self.run_sync("ac2_to_shopify", **off)
        self.assertEqual(self.links(), {gid(21): "TILL21"})
        self.assertEqual((result["counts"]["linked_existing_shopify_customer"], self.shopify.created), (1, []))

    def test_row_ML10b_a_full_batch_of_would_create_rows_alerts_once_per_run(self):
        off = {"shopify_create_enabled": False}
        self.run_sync("ac2_to_shopify", **off)  # watermark
        self.walk_ins(range(1, 26))
        self.run_sync("ac2_to_shopify", **off)
        self.run_sync("ac2_to_shopify", **off)  # the last 5 are read: all 25 wait for the switch
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM ac2_pending WHERE state = 'PENDING' AND code = 'would_create'").fetchone()[0], 25)
        for _ in range(2):
            result = self.run_sync("ac2_to_shopify", **off)
            self.assertEqual((result["alerts"], result["counts"]["ac2_pending_would_create_backlog"]), (["ac2_pending_would_create_backlog"], 1))
            self.assertEqual((result["writes"], self.shopify.created), (0, []))

    def would_create_backlog(self, count: int) -> None:
        off = {"shopify_create_enabled": False}
        self.run_sync("ac2_to_shopify", **off)  # watermark
        self.walk_ins(range(1, count + 1))
        self.run_sync("ac2_to_shopify", **off)
        self.run_sync("ac2_to_shopify", **off)

    def test_row_ML10c_creates_turned_on_drain_the_backlog_oldest_first_within_the_cap(self):
        self.would_create_backlog(25)
        for run in range(1, 6):
            result = self.run_sync("ac2_to_shopify", max_writes_per_run=5)
            self.assertEqual(result["writes"], 5)
            self.assertEqual([c["phone"] for c in self.shopify.created], [plus(sg(n)) for n in range(1, 5 * run + 1)])
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM ac2_pending WHERE state = 'PENDING'").fetchone()[0], 0)
        self.assertEqual(self.run_sync("ac2_to_shopify", max_writes_per_run=5)["writes"], 0)  # each created exactly once

    def test_row_ML10d_creates_on_a_new_walk_in_waits_behind_the_older_backlog(self):
        # G2 #281 LATER-1: 25 would_create rows + 1 new walk-in after the
        # switch: the whole queue is created oldest first, the new one last.
        self.would_create_backlog(25)
        self.walk_ins([26])
        for _ in range(6):
            self.run_sync("ac2_to_shopify", max_writes_per_run=5)
        self.assertEqual([c["phone"] for c in self.shopify.created], [plus(sg(n)) for n in range(1, 27)])

    # -- task ----------------------------------------------------------------------
    def test_row_T4_a_second_runner_is_refused_by_the_ledger_lock(self):
        runs = self.ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.SyncLedger(self.temp, check_custody=False)
        self.assertEqual(caught.exception.code, "ledger_locked_by_another_owner")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], runs)

    def test_row_T5_digest_is_read_only_masked_and_exits_0(self):
        import hashlib

        root = self.private_root()
        self.ledger.close()
        self.ledger = sync.SyncLedger(root, clock=lambda: self.now)
        self.ledger.init_meta(autocount_book=PROD_BOOK, store_gid=STORE, shop_domain=DOMAIN)
        with self.ledger.tx() as conn:
            self.ledger.set_watermark(conn, "initial_link_completed_at", dl.ts(CUTOVER))
        self.signup(20, phone=None)
        self.signup(22)
        self.ac2.add(sync.Ac2Member(sg(22), "STAFF TYPED", None, created_at=T0 + timedelta(hours=3)))
        self.run_sync("shopify_to_ac2", ac2_create_enabled=False)
        self.run_sync("shopify_to_ac2", ac2_create_enabled=False)
        self.ledger.close()
        self.ledger = sync.SyncLedger(self.temp, check_custody=False)  # tearDown closes this one

        def fingerprint():
            # The database file itself, plus any write-ahead log content (a
            # read-only reader may leave SQLite's empty -wal/-shm files).
            wal = root / (sync.LEDGER_FILE + "-wal")
            return hashlib.sha256((root / sync.LEDGER_FILE).read_bytes()).hexdigest(), (wal.stat().st_size if wal.exists() else 0)

        before = fingerprint()
        config = config_value(**LIVE_KEYS, state_root=str(root))
        with mock.patch.object(dl, "utc_now", return_value=self.now + timedelta(minutes=1)):
            code, out = self.cli("digest", "--hours", "24", config=config)
        self.assertEqual(code, 0)
        self.assertEqual(fingerprint(), before)
        self.assertEqual(before[1], 0)
        self.assertEqual((out["schema"], out["status"], out["autocount_book"], out["runs"]["completed"], out["runs"]["unfinished"]), (sync.DIGEST_SCHEMA, "ok", PROD_BOOK, 2, 0))
        self.assertEqual(out["alerts"], {"memberno_exists_outside_window": 1, "no_phone_sign_up_needs_staff": 1})
        self.assertEqual(out["open"]["uncertain_writes"], 0)
        refs = {(r["code"], r.get("member_no_masked")) for r in out["alert_refs"]}
        self.assertEqual(refs, {("no_phone_sign_up_needs_staff", None), ("memberno_exists_outside_window", "*******" + sg(22)[-3:])})
        self.assertTrue(all(re.fullmatch(r"c-[0-9a-f]+", r["ref"]) for r in out["alert_refs"]))
        self.assert_no_pii(out)
        self.assertNotIn("gid://", json.dumps(out))
        for bad in ("0", "721"):
            self.assertEqual(self.cli("digest", "--hours", bad, config=config), (2, {"status": "refused", "code": "digest_hours_invalid"}))

    # -- VM facts ------------------------------------------------------------------
    def test_row_V4_initial_link_never_links_a_phone_held_by_two_members(self):
        with self.ledger.tx() as conn:
            conn.execute("DELETE FROM watermarks WHERE name = 'initial_link_completed_at'")
        made = CUTOVER - timedelta(days=30)
        self.shopify.add(replace(customer(23, tags=()), created_at=made, updated_at=made, registration_at=None))
        self.ac2.add(sync.Ac2Member(sg(23), "ONE", None))
        self.ac2.add(sync.Ac2Member(local(23), "TWO", None))  # the same phone in its 8-digit form
        for apply in (False, True):
            with self.subTest(apply=apply):
                result = self.engine(initial_link_enabled=True).initial_link(apply=apply)
                self.assertEqual(result["alerts"], ["initial_link_member_no_conflict"])
                flagged = [r for r in result["refs"] if r["code"] == "initial_link_member_no_conflict"]
                self.assertEqual(flagged, [{"ref": self.ledger.ref("c", gid(23)), "action": "skip", "code": "initial_link_member_no_conflict", "member_no_masked": sync.mask_last3(sg(23))}])
                self.assertEqual(self.links(), {})
                self.assert_no_pii(result)

    def test_row_V9_the_live_book_logs_in_only_as_the_sync_user(self):
        # The ``dedicated`` login mode (a separate sync user).
        for overrides, code in (
            ({"ac2_credential_target": None}, "sync_config_ac2_credential_target_live_required"),
            ({"ac2_credential_target": "XB-AC2-Owner"}, "sync_config_ac2_credential_target_live_required"),
            ({"ac2_refused_user_ids": []}, "sync_config_ac2_refused_user_ids_live_required"),
            ({"ac2_refused_user_ids": ["bad user"]}, "sync_config_ac2_refused_user_ids_invalid"),
        ):
            with self.subTest(code=code, overrides=overrides), self.assertRaises(dl.DirectLocalError) as caught:
                live_config(**{**DEDICATED_KEYS, **overrides})
            self.assertEqual(caught.exception.code, code)
        secret = "fake-secret-value-9f2c"
        for user in (FAKE_OWNER_USER, FAKE_OWNER_USER.upper()):
            with self.subTest(user=user), self.assertRaises(dl.DirectLocalError) as caught:
                sync.load_ac2_credential("XB-AC2-MemberSync", refused_user_ids=live_config(**DEDICATED_KEYS).ac2_refused_user_ids, cred_read=lambda target: (user, secret))
            self.assertEqual(caught.exception.code, "ac2_credential_user_refused")
            self.assertNotIn(secret, str(caught.exception))
        with mock.patch.object(sync, "_win_cred_read", return_value=(FAKE_OWNER_USER, secret)):
            self.assertEqual(self.cli("check-credential", config=config_value(**DEDICATED_KEYS)), (2, {"status": "refused", "code": "ac2_credential_user_refused"}))
        with mock.patch.object(sync, "_win_cred_read", return_value=("syncfake02", secret)):
            self.assertEqual(self.cli("check-credential", config=config_value(**DEDICATED_KEYS)), (0, {"status": "ok", "user_id": "*******e02", "secret_present": True}))
        built = []
        with mock.patch.object(sync, "_win_cred_read", return_value=(FAKE_OWNER_USER, secret)), \
                mock.patch.object(sync.SyncShopifyClient, "verify_session", lambda self, **kw: None), \
                mock.patch.object(sync, "SyncBridgeClient", side_effect=lambda *a, **kw: built.append(1)):
            with self.assertRaises(dl.DirectLocalError) as caught:
                sync.build_real_engine(live_config(**DEDICATED_KEYS), self.ledger, environ={"XB_SHOPIFY_SYNC_TOKEN": "shpat_synthetic_token_value_0001"})
        self.assertEqual((caught.exception.code, built), ("ac2_credential_user_refused", []))  # no bridge, so no login

    def test_row_V9b_the_live_credential_target_is_used_only_by_the_live_config(self):
        # G2 #281 LATER-3: the test book has its own target; any spelling of
        # the live target is refused there (Credential Manager ignores case).
        for target in (sync.LIVE_CREDENTIAL_TARGET, "xb-ac2-membersync", "XB-AC2-MEMBERSYNC", "XB-AC2-MemberSync "):
            with self.subTest(target=target), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(ac2_credential_target=target)
            self.assertEqual(caught.exception.code, "sync_config_ac2_credential_target_live_only")
        secret = "fake-secret-value-9f2c"
        read = []
        with mock.patch.object(sync, "_win_cred_read", side_effect=lambda target: read.append(target) or ("syncfake02", secret)):
            refused = self.cli("check-credential", config=config_value(ac2_credential_target=sync.LIVE_CREDENTIAL_TARGET))
            accepted = self.cli("check-credential", config=config_value(ac2_credential_target=sync.TEST_CREDENTIAL_TARGET))
        self.assertEqual(refused, (2, {"status": "refused", "code": "sync_config_ac2_credential_target_live_only"}))
        self.assertEqual(accepted, (0, {"status": "ok", "user_id": "*******e02", "secret_present": True}))
        self.assertEqual(read, [sync.TEST_CREDENTIAL_TARGET])  # the refused config never read a credential
        # The other way: the live config still needs exactly the live target,
        # and the test book keeps its other options (null, any other target).
        with self.assertRaises(dl.DirectLocalError) as caught:
            live_config(ac2_credential_target=sync.TEST_CREDENTIAL_TARGET)
        self.assertEqual(caught.exception.code, "sync_config_ac2_credential_target_live_required")
        self.assertEqual(live_config().ac2_credential_target, sync.LIVE_CREDENTIAL_TARGET)
        self.assertIsNone(make_config(ac2_credential_target=None).ac2_credential_target)
        self.assertEqual(make_config(ac2_credential_target=sync.TEST_CREDENTIAL_TARGET).ac2_credential_target, "XB-AC2-MemberSync-Test")

    def test_row_V9c_the_live_config_states_its_login_mode_and_owner_shared_uses_the_owners_login(self):
        # Owner 30 Oct 2026 ("Use my login"): no silent default either way.
        for overrides, code in (
            ({"ac2_login_mode": None}, "sync_config_ac2_login_mode_live_required"),
            ({"ac2_login_mode": "Owner_Shared"}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": "owner_shared "}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": "shared"}, "sync_config_ac2_login_mode_invalid"),
            # G2 #283 L2: non-ASCII lookalikes (a Cyrillic letter; fullwidth forms).
            ({"ac2_login_mode": "owner_\u0455hared"}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": "\u043ewner_shared"}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": "\uff4f\uff57\uff4e\uff45\uff52\uff3f\uff53\uff48\uff41\uff52\uff45\uff44"}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": "dedicate\uff44"}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": ""}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": True}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_login_mode": ["owner_shared"]}, "sync_config_ac2_login_mode_invalid"),
            ({"ac2_refused_user_ids": [FAKE_OWNER_USER]}, "sync_config_ac2_refused_user_ids_owner_shared_refused"),
            ({"ac2_credential_target": None}, "sync_config_ac2_credential_target_live_required"),
            ({"ac2_create_without_phone": True}, "sync_config_ac2_create_without_phone_live_refused"),
        ):
            with self.subTest(overrides=overrides), self.assertRaises(dl.DirectLocalError) as caught:
                live_config(**overrides)
            self.assertEqual(caught.exception.code, code)
        missing = config_value(**LIVE_KEYS)
        del missing["ac2_login_mode"]
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.parse_config(missing)
        self.assertEqual(caught.exception.code, "sync_config_ac2_login_mode_live_required")
        config = live_config()
        self.assertEqual((config.ac2_login_mode, config.ac2_refused_user_ids), ("owner_shared", ()))
        self.assertEqual(config.summary()["ac2_login_mode"], "owner_shared")
        self.assertEqual(live_config(**DEDICATED_KEYS).summary()["ac2_login_mode"], "dedicated")
        # The test book: the mode may be left out (UAT as merged) or stated;
        # owner_shared with a refused list is a contradiction on either book.
        self.assertIsNone(make_config().ac2_login_mode)
        self.assertEqual(make_config(ac2_login_mode="owner_shared").ac2_login_mode, "owner_shared")
        self.assertEqual(make_config(ac2_login_mode="dedicated", ac2_refused_user_ids=[FAKE_OWNER_USER]).ac2_refused_user_ids, (FAKE_OWNER_USER,))
        for overrides, code in (({"ac2_login_mode": "other"}, "sync_config_ac2_login_mode_invalid"),
                                ({"ac2_login_mode": "owner_shared", "ac2_refused_user_ids": [FAKE_OWNER_USER]}, "sync_config_ac2_refused_user_ids_owner_shared_refused")):
            with self.subTest(book=TEST_BOOK, overrides=overrides), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(**overrides)
            self.assertEqual(caught.exception.code, code)
        # owner_shared: the Owner's own user is loaded and handed to the
        # bridge child; sa stays refused and the secret is never printed.
        secret = "fake-secret-value-9f2c"
        with mock.patch.object(sync, "_win_cred_read", return_value=(FAKE_OWNER_USER, secret)):
            self.assertEqual(self.cli("check-credential", config=config_value(**LIVE_KEYS)), (0, {"status": "ok", "user_id": "********e01", "secret_present": True}))
        code, out = self.cli("check-config", config=config_value(**LIVE_KEYS))
        self.assertEqual((code, out["ac2_login_mode"], out["ac2_refused_user_ids_count"]), (0, "owner_shared", 0))
        with mock.patch.object(sync, "_win_cred_read", return_value=("SA", secret)):
            self.assertEqual(self.cli("check-credential", config=config_value(**LIVE_KEYS)), (2, {"status": "refused", "code": "ac2_credential_sa_refused"}))
        built = []
        with mock.patch.object(sync, "_win_cred_read", return_value=(FAKE_OWNER_USER, secret)), \
                mock.patch.object(sync.SyncShopifyClient, "verify_session", lambda self, **kw: None), \
                mock.patch.object(sync, "SyncBridgeClient", side_effect=lambda *a, **kw: built.append(kw["env"]) or object()):
            sync.build_real_engine(live_config(), self.ledger, environ={"XB_SHOPIFY_SYNC_TOKEN": "shpat_synthetic_token_value_0001"})
        self.assertEqual(len(built), 1)
        self.assertEqual((built[0]["XB_AC2_USER_ID"], built[0][sync.AC2_PASSWORD_ENV]), (FAKE_OWNER_USER, secret))

    def test_row_V9d_owner_shared_fails_closed_when_autocount_refuses_the_login(self):
        # Owner 30 Oct 2026: the Owner's user may be refused by AutoCount
        # (signed in elsewhere in a way AutoCount rejects, no free licence,
        # wrong password). Same codes as before the decision; nothing written.
        self.assertEqual(live_config().ac2_login_mode, "owner_shared")
        claims = self.temp / "claims"
        claims.mkdir()
        for child_code in ("autocount_login_rejected", "autocount_authentication_failed", "autocount_session_initialization_failed"):
            refused = b"vendor noise\n" + json.dumps({"error": child_code}).encode()

            class Process:
                returncode = 1
                pid = 1

                def communicate(self, data, timeout=None):
                    return (refused, b"")

            client = sync.SyncBridgeClient(["powershell.exe"], autocount_book=PROD_BOOK, claims=claims, env={}, popen=lambda args, **kwargs: Process())
            with self.subTest(child_code=child_code, stage="read"), self.assertRaises(dl.BridgeError) as caught:
                client.lookup(sg(25), [])
            self.assertEqual(caught.exception.code, f"bridge_read_failed:{child_code}")
            # The run stops at its first AutoCount read: no write either side.
            self.signup(25)
            with self.subTest(child_code=child_code, stage="run"), \
                    mock.patch.object(self.ac2, "lookup", side_effect=dl.BridgeError(f"bridge_read_failed:{child_code}")), \
                    self.assertRaises(dl.BridgeError) as caught:
                self.engine().run(direction="both")
            self.assertEqual(caught.exception.code, f"bridge_read_failed:{child_code}")
            self.assertEqual((self.ac2.creates, self.shopify.created, self.links()), ([], [], {}))
            self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 0)
            # A create whose child is refused at login stops before its claim:
            # proven not written, under the same code as today.
            with mock.patch.object(client, "write_once", return_value=dl.WriteRun(started=True, exit_code=1, stdout=refused.decode())):
                state = client.create_member(sg(25), {"name": "Alpha"}, write_id="write-0001")
            self.assertEqual(state, ("NOT_WRITTEN_PROVEN", "writer_failed_before_claim"))
            self.assertEqual(list(claims.iterdir()), [])


class GoLiveEngineExtrasTests(GoLiveTestCase):
    """Go-live behaviour that is not its own matrix row."""

    def test_member_type_unknown_is_not_written_and_alerts(self):
        self.ac2.create_result = ("NOT_WRITTEN_PROVEN", "member_type_unknown")
        self.signup(24)
        result = self.run_sync("shopify_to_ac2")
        self.assertIn("member_type_unknown", result["alerts"])
        self.assertEqual((self.links(), self.pending(24)), ({}, ("PENDING", "ac2_not_written")))
        self.assertEqual(self.ledger.conn.execute("SELECT state, code FROM writes").fetchone()[:], ("NOT_WRITTEN", "member_type_unknown"))

    def test_digest_prints_the_longest_and_median_run_time(self):
        # G2 #281 LATER-2: S4 and S7 compare duration_seconds_max with the
        # 600 s execution limit, so the digest must print it.
        root = self.private_root()
        ledger = sync.SyncLedger(root, clock=lambda: self.now)
        try:
            ledger.init_meta(autocount_book=PROD_BOOK, store_gid=STORE, shop_domain=DOMAIN)
            with ledger.tx() as conn:
                for n, seconds in enumerate((40, 595, 310)):
                    start = self.now - timedelta(hours=3, minutes=20 * n)
                    conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES (?, ?, ?, '{}')",
                                 (f"run-{n}", dl.ts(start), dl.ts(start + timedelta(seconds=seconds))))
                conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('run-stopped', ?, NULL, NULL)", (dl.ts(self.now - timedelta(hours=1)),))
        finally:
            ledger.close()
        with mock.patch.object(dl, "utc_now", return_value=self.now):
            code, out = self.cli("digest", "--hours", "24", config=config_value(**LIVE_KEYS, state_root=str(root)))
        self.assertEqual(code, 0)
        self.assertEqual(out["runs"], {"completed": 3, "unfinished": 1, "duration_seconds_max": 595, "duration_seconds_median": 310})

    def test_generator_is_never_used_on_the_live_book(self):
        self.assertEqual(self.engine()._allocate_generator(), (None, "member_no_generator_not_authorised"))
        self.assertEqual(self.ac2.generator_calls, 0)

    def test_bridge_refusal_before_the_claim_is_named(self):
        self.assertEqual(sync._bridge_refusal('vendor noise\n{"error":"member_type_unknown"}\n'), "member_type_unknown")
        self.assertIsNone(sync._bridge_refusal('{"error":"Login failed for user"}'))
        self.assertIsNone(sync._bridge_refusal("not json"))

    def test_live_example_config_fails_until_its_placeholders_are_set(self):
        example = json.loads((ROOT / "config" / "member_sync.live.example.json").read_text(encoding="utf-8"))
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.parse_config(example)
        self.assertEqual(caught.exception.code, "sync_config_cutover_invalid")
        example["shopify_cutover_utc"] = dl.ts(CUTOVER)
        config = sync.parse_config(example)
        # Owner 30 Oct 2026 ("Use my login"): the example states that decision.
        self.assertEqual((config.ac2_login_mode, config.ac2_refused_user_ids), ("owner_shared", ()))
        self.assertEqual((config.autocount_book, config.ac2_create_member_type, config.ac2_credential_target), (PROD_BOOK, "Default", "XB-AC2-MemberSync"))
        self.assertFalse(config.ac2_create_enabled or config.shopify_create_enabled or config.initial_link_enabled or config.ac2_create_without_phone)
        self.assertIsNone(config.changelog_xml_column)
        test_example = sync.parse_config(json.loads((ROOT / "config" / "member_sync.example.json").read_text(encoding="utf-8")))
        self.assertNotEqual(config.state_root, test_example.state_root)


# --------------------------------------------------------------------------
# #155 P1 + P2 (G1 immediate create; G0 r2 #155:6610257893 and the owner
# decisions #155:6619639133). P1: the immediate-create timing mode, its caps
# and the wall-clock holds, all off by default. P2: the sign-up probe. Fake
# data only: reserved phones built at runtime, example.com, fake IDs.
# --------------------------------------------------------------------------

import hashlib  # noqa: E402

IMMEDIATE = {"immediate_create_enabled": True}
# The new keys written out at their defaults (and other values that must not
# change a run while the immediate path is off).
NEW_KEYS_OFF = {"immediate_create_enabled": False, "immediate_create_window_local": "all_day", "max_creates_per_hour": 6,
                "max_creates_per_day": 144, "probe_wake_enabled": False, "probe_max_wakes_per_day": 60}
NEW_KEYS_OFF_ODD = {"immediate_create_enabled": False, "immediate_create_window_local": "23:00-01:00", "max_creates_per_hour": 1,
                    "max_creates_per_day": 1, "probe_wake_enabled": True, "probe_max_wakes_per_day": 1}
# The defaults-off golden trace: ``defaults_off_trace(sync)`` run on the
# 32e9b31 source of directlocal/xb_member_sync.py (that file loaded in place
# of this one; xb_directlocal and xb_shopify_direct are unchanged since). Only
# its SHA-256 and run count are kept: no data file (tests/ holds no JSON
# fixtures, test_member_intake_validate guardrail).
GOLDEN_SOURCE_COMMIT = "32e9b31"
GOLDEN_TRACE_SHA256 = "4bcb98f229cc481c4792326cffb10e1b3d0a1bb39a377242c19a1a8ac2d37ac6"
GOLDEN_TRACE_RUNS = 50
PROBE_TOKEN_ENV = {"XB_SHOPIFY_SYNC_TOKEN": "shpat_synthetic_token_value_0001"}


def _probe_module():
    import xb_member_sync_probe as probe

    return probe


# -- defaults-off equivalence: one deterministic trace over the fixtures above --

class _EquivalenceWorld:
    """One ledger, the in-memory fakes and a fake clock; every run's result
    is kept. The ledger ref key is fixed so refs and digests repeat."""

    def __init__(self, module, temp: Path, book: str, settings: dict):
        self.module = module
        self.book = book
        self.settings = settings
        self.now = T0 + timedelta(hours=1)
        self.ledger = module.SyncLedger(temp, clock=lambda: self.now, check_custody=False)
        with mock.patch.object(module.secrets, "token_hex", return_value="5a" * 32):
            self.ledger.init_meta(autocount_book=book, store_gid=STORE, shop_domain=DOMAIN)
        self.shopify, self.ac2 = FakeShopify(), FakeAc2()
        self.results: list[dict] = []
        with self.ledger.tx() as conn:
            self.ledger.set_watermark(conn, "initial_link_completed_at", dl.ts(CUTOVER))

    def run(self, direction: str = "both", **overrides) -> dict:
        settings = {"ac2_create_enabled": True, "shopify_create_enabled": True, "changelog_xml_column": "Content"}
        if self.book == PROD_BOOK:
            settings.update(LIVE_KEYS)
        settings.update(self.settings)
        settings.update(overrides)
        config = self.module.parse_config(config_value(**settings))
        result = self.module.SyncEngine(self.ledger, config, shopify=self.shopify, ac2=self.ac2).run(direction=direction)
        self.results.append(result)
        return result

    def dump(self) -> dict:
        tables = {
            "links": "SELECT customer_gid, member_no, member_no_key, origin, code, created_at FROM links",
            "writes": "SELECT direction, customer_gid, member_no, member_no_key, state, code, created_at, updated_at, intent_digest FROM writes",
            "shopify_pending": "SELECT * FROM shopify_pending", "ac2_pending": "SELECT * FROM ac2_pending", "watermarks": "SELECT * FROM watermarks",
            "member_snapshots": "SELECT * FROM member_snapshots", "changelog_seen": "SELECT * FROM changelog_seen",
            "runs": "SELECT started_at, finished_at, summary FROM runs",
        }
        return {name: sorted(([*row] for row in self.ledger.conn.execute(sql)), key=json.dumps) for name, sql in tables.items()}


def _scenario_staff_first(w: _EquivalenceWorld) -> None:
    w.run("ac2_to_shopify")  # ChangeLog watermark
    w.shopify.add(replace(customer(40), created_at=T0))
    w.shopify.add(replace(customer(41, phone=None), created_at=T0))
    w.shopify.add(replace(customer(42, first="Mary Ann", last="Lee"), created_at=T0))
    w.ac2.add(sync.Ac2Member("STAFF43", "Mary A Lee", local(43)))
    w.shopify.add(replace(customer(43, first="Mary Ann", last="Lee"), created_at=T0))
    w.run("shopify_to_ac2")
    w.ac2.add(sync.Ac2Member(sg(42), "MARY A LEE", None, created_at=T0 + timedelta(minutes=4)), log=True)
    w.now = T0 + timedelta(hours=3)
    w.run()
    w.now = T0 + timedelta(days=1)
    w.run()
    w.run()
    w.now = T0 + timedelta(days=2)
    w.run()


def _scenario_holds(w: _EquivalenceWorld) -> None:
    w.run("ac2_to_shopify")
    w.shopify.add(replace(customer(60), created_at=T0, registration_at=None, registration_value=None))
    for minute in (0, 0, 1, 1, 2, 3):
        w.now = T0 + timedelta(hours=1, minutes=minute)
        w.run("shopify_to_ac2")
    w.shopify.add(replace(customer(61), created_at=T0, registration_at=None, registration_value=None))
    w.ac2.add(sync.Ac2Member(sg(61), "SOMEONE ELSE", None, created_at=T0 + timedelta(minutes=1)), log=True)
    for minute in (4, 4, 5, 5, 6, 7):
        w.now = T0 + timedelta(hours=1, minutes=minute)
        w.run("ac2_to_shopify")


def _scenario_walk_ins(w: _EquivalenceWorld) -> None:
    w.run("ac2_to_shopify")
    for n in range(75, 80):
        w.shopify.add(replace(customer(n), created_at=T0))
    w.run("shopify_to_ac2")
    w.ac2.add(sync.Ac2Member(sg(30), "Walk In Person", None, created_at=T0 + timedelta(minutes=1)), log=True)
    for _ in range(6):
        w.run("ac2_to_shopify", max_pending_rechecks_per_run=3)
    w.run("ac2_to_shopify")
    w.ac2.add(sync.Ac2Member("T0099", "Bravo Walker", sg(31)), log=True)
    w.shopify.add(replace(customer(32, first="Charlie", last="Day"), tags=()))
    w.ac2.add(sync.Ac2Member("T0100", "Charlie Day", sg(32)), log=True)
    w.ac2.add(sync.Ac2Member("T0101", "Other Person", sg(32)), log=True)
    w.run("ac2_to_shopify")
    w.now += timedelta(minutes=15)
    w.run()


def _scenario_writes(w: _EquivalenceWorld) -> None:
    w.run("ac2_to_shopify")
    for n in range(1, 9):
        w.shopify.add(replace(customer(n), created_at=T0))
    w.ac2.add(sync.Ac2Member("STAFF05", "Someone Else", local(5)))
    w.now = T0 + timedelta(days=1)
    w.run(max_writes_per_run=3)
    w.ac2.create_result = ("WRITE_OUTCOME_UNCERTAIN", "writer_timeout")
    w.run(max_writes_per_run=1)
    w.ac2.create_result = ("NOT_WRITTEN_PROVEN", "member_no_taken")
    w.run(max_writes_per_run=1)
    w.ac2.create_result = ("CREATED_VERIFIED", None)
    w.now += timedelta(minutes=15)
    w.run()
    w.run()
    w.shopify.add(replace(customer(9), created_at=T0))
    w.now = T0 + timedelta(days=1, hours=1)
    w.run(ac2_create_enabled=False)


EQUIVALENCE_SCENARIOS = (
    ("staff_first_test_book", TEST_BOOK, _scenario_staff_first), ("staff_first_live_book", PROD_BOOK, _scenario_staff_first),
    ("holds_test_book", TEST_BOOK, _scenario_holds), ("walk_ins_test_book", TEST_BOOK, _scenario_walk_ins),
    ("writes_test_book", TEST_BOOK, _scenario_writes), ("writes_live_book", PROD_BOOK, _scenario_writes),
)


def defaults_off_trace(module, settings: dict | None = None) -> dict:
    """Every run result, the final ledger rows (random IDs left out) and the
    fakes' writes of each scenario, for ``module`` (this source, or the
    32e9b31 source when the golden digest is made)."""

    trace = {}
    for name, book, scenario in EQUIVALENCE_SCENARIOS:
        temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-equivalence-"))
        try:
            world = _EquivalenceWorld(module, temp, book, dict(settings or {}))
            try:
                scenario(world)
                trace[name] = {"runs": world.results, "ledger": world.dump(), "ac2_creates": [[m, p] for m, p in world.ac2.creates],
                               "shopify_created": world.shopify.created, "generator_calls": world.ac2.generator_calls}
            finally:
                world.ledger.close()
        finally:
            shutil.rmtree(temp, ignore_errors=True)
    return trace


def trace_digest(trace: dict) -> str:
    return hashlib.sha256(json.dumps(trace, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def trace_outline(trace: dict) -> dict:
    """Counts, alerts and writes per run (printed when the digest differs)."""

    return {name: [{"counts": r["counts"], "alerts": r["alerts"], "writes": r["writes"]} for r in item["runs"]] for name, item in trace.items()}


class DefaultsOffEquivalenceTests(unittest.TestCase):
    """Sequencing (owner, 4 Nov 2026): with the new switches off this source
    decides every run exactly as 32e9b31 did: the trace's SHA-256 equals the
    one made on the 32e9b31 source (GOLDEN_TRACE_SHA256)."""

    def assert_matches_golden(self, settings: dict | None) -> None:
        self.assertEqual(GOLDEN_SOURCE_COMMIT, "32e9b31")
        trace = defaults_off_trace(sync, settings)
        self.assertEqual(sum(len(item["runs"]) for item in trace.values()), GOLDEN_TRACE_RUNS)
        self.assertEqual(trace_digest(trace), GOLDEN_TRACE_SHA256, json.dumps(trace_outline(trace), sort_keys=True))

    def test_new_keys_absent(self):
        self.assert_matches_golden(None)

    def test_new_keys_written_out_at_their_defaults(self):
        self.assert_matches_golden(NEW_KEYS_OFF)

    def test_other_values_change_nothing_while_the_immediate_path_is_off(self):
        self.assert_matches_golden(NEW_KEYS_OFF_ODD)

    def test_the_trace_covers_the_decisions_that_matter(self):
        # The digest is only as strong as what the scenarios exercise.
        trace = defaults_off_trace(sync)
        codes = {code for item in trace.values() for run in item["runs"] for code in run["counts"]}
        for code in ("waiting_for_staff", "created_after_wait", "memberno_and_created_time_match", "no_phone_sign_up_needs_staff",
                     "no_phone_generator_member_no", "shopify_registration_event_pending", "ac2_member_held_for_sign_up", "ac2_member_hold_stuck",
                     "ac2_member_hold_requeued", "ac2_create_not_written", "ac2_create_uncertain", "uncertain_write_resolved_linked",
                     "max_writes_per_run_reached", "shared_phone_name_fallback", "ac2_insert_linked_or_own_write", "shopify_customer_created",
                     "linked_existing_shopify_customer", "shopify_separate_customer_created_without_phone", "sign_up_past_cutoff_create_disabled"):
            self.assertIn(code, codes)
        self.assertEqual({book for _, book, _ in EQUIVALENCE_SCENARIOS}, {TEST_BOOK, PROD_BOOK})


# -- configuration ---------------------------------------------------------------

class ImmediateCreateConfigTests(unittest.TestCase):
    def test_defaults_are_off_with_the_owner_caps(self):
        config = make_config()
        self.assertEqual(
            (config.immediate_create_enabled, config.immediate_create_window_local, config.max_creates_per_hour, config.max_creates_per_day,
             config.probe_wake_enabled, config.probe_max_wakes_per_day),
            (False, "all_day", 6, 144, False, 60))
        self.assertFalse(config.immediate_path_active)
        self.assertFalse(make_config(immediate_create_enabled=True).immediate_path_active)  # creates still off
        self.assertTrue(make_config(immediate_create_enabled=True, ac2_create_enabled=True).immediate_path_active)
        summary = make_config(**NEW_KEYS_OFF).summary()
        for key, value in NEW_KEYS_OFF.items():
            self.assertEqual(summary[key], value)

    def test_window_values_and_lookalikes(self):
        for good in ("all_day", "10:00-22:00", "22:00-06:00", "00:00-23:59", "06:00-00:00"):
            self.assertEqual(make_config(immediate_create_window_local=good).immediate_create_window_local, good)
        for bad in ("All_Day", "ALL_DAY", "all-day", "allday", "all_day ", " all_day", "10:00-10:00", "00:00-24:00", "24:00-01:00", "10:00 - 22:00",
                    "10:00-22:00 ", "1000-2200", "10:00–22:00", "10:00-22:0", "9:00-22:00", "10:00-22:00\n", "", None, 1, True, ["10:00", "22:00"]):
            with self.subTest(window=ascii(bad)), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(immediate_create_window_local=bad)
            self.assertEqual(caught.exception.code, "sync_config_immediate_create_window_local_invalid")

    def test_switches_are_strict_booleans(self):
        for key in ("immediate_create_enabled", "probe_wake_enabled"):
            for bad in (1, 0, "true", "false", None, "True"):
                with self.subTest(key=key, value=bad), self.assertRaises(dl.DirectLocalError) as caught:
                    make_config(**{key: bad})
                self.assertEqual(caught.exception.code, f"sync_config_{key}_invalid")

    def test_caps_and_wake_cap_bounds(self):
        for key, low, high in (("max_creates_per_hour", 1, 30), ("max_creates_per_day", 1, 720), ("probe_max_wakes_per_day", 1, 240)):
            for good in (low, high):
                extra = {"max_creates_per_hour": 1} if key == "max_creates_per_day" else {}
                self.assertEqual(getattr(make_config(**{key: good}, **extra), key), good)
            for bad in (low - 1, -1, high + 1, "6", 6.0, True, None):
                with self.subTest(key=key, value=bad), self.assertRaises(dl.DirectLocalError) as caught:
                    make_config(**{key: bad})
                self.assertEqual(caught.exception.code, f"sync_config_{key}_invalid")
        with self.assertRaises(dl.DirectLocalError) as caught:
            make_config(max_creates_per_hour=7, max_creates_per_day=6)
        self.assertEqual(caught.exception.code, "sync_config_max_creates_per_day_invalid")
        self.assertEqual(make_config(max_creates_per_hour=6, max_creates_per_day=6).max_creates_per_day, 6)

    def test_unknown_and_lookalike_keys_are_refused(self):
        for key in ("immediate_create", "immediate_create_enabled ", "Immediate_create_enabled", "max_create_per_hour", "probe_enabled", "immediate_create_window"):
            with self.subTest(key=key), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(**{key: False})
            self.assertEqual(caught.exception.code, "sync_config_keys_invalid")

    def test_live_example_config_allows_all_hours_with_every_switch_off(self):
        example = json.loads((ROOT / "config" / "member_sync.live.example.json").read_text(encoding="utf-8"))
        example["shopify_cutover_utc"] = dl.ts(CUTOVER)
        config = sync.parse_config(example)
        self.assertEqual((config.immediate_create_window_local, config.max_creates_per_hour, config.max_creates_per_day), ("all_day", 6, 144))
        self.assertFalse(config.immediate_create_enabled or config.probe_wake_enabled or config.ac2_create_enabled)
        self.assertEqual(config.ac2_create_member_type, "Default")  # owner D2: never a qualified type
        self.assertEqual({k: example[k] for k in NEW_KEYS_OFF}, NEW_KEYS_OFF)

    def test_check_config_prints_the_new_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(config_value(**IMMEDIATE, immediate_create_window_local="10:00-22:00")), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = sync.main(["check-config", "--config", str(path)])
        value = json.loads(out.getvalue())
        self.assertEqual((code, value["immediate_create_enabled"], value["immediate_create_window_local"], value["max_creates_per_hour"]), (0, True, "10:00-22:00", 6))


class CreateWindowTests(unittest.TestCase):
    def test_all_day_includes_midnight_and_the_last_second(self):
        for moment in (sgt(21, 0, 0), sgt(21, 23, 59) + timedelta(seconds=59), sgt(21, 6, 0), sgt(21, 12, 0)):
            self.assertTrue(sync.in_create_window(moment, "all_day"))

    def test_day_window_start_included_end_excluded(self):
        window = "10:00-22:00"
        self.assertFalse(sync.in_create_window(sgt(21, 9, 59) + timedelta(seconds=59), window))
        self.assertTrue(sync.in_create_window(sgt(21, 10, 0), window))
        self.assertTrue(sync.in_create_window(sgt(21, 21, 59) + timedelta(seconds=59), window))
        self.assertFalse(sync.in_create_window(sgt(21, 22, 0), window))
        self.assertFalse(sync.in_create_window(sgt(21, 0, 0), window))

    def test_window_over_midnight(self):
        window = "22:00-06:00"
        self.assertTrue(sync.in_create_window(sgt(21, 22, 0), window))
        self.assertTrue(sync.in_create_window(sgt(21, 23, 59) + timedelta(seconds=59), window))
        self.assertTrue(sync.in_create_window(sgt(22, 0, 0), window))
        self.assertTrue(sync.in_create_window(sgt(22, 5, 59) + timedelta(seconds=59), window))
        self.assertFalse(sync.in_create_window(sgt(22, 6, 0), window))
        self.assertFalse(sync.in_create_window(sgt(21, 21, 59), window))
        self.assertFalse(sync.in_create_window(sgt(22, 0, 0), "06:00-00:00"))
        self.assertTrue(sync.in_create_window(sgt(21, 23, 59), "06:00-00:00"))

    def test_utc_is_read_in_singapore_time(self):
        # 14:30 UTC is 22:30 in Singapore: outside 10:00-22:00, inside 22:00-06:00.
        moment = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)
        self.assertFalse(sync.in_create_window(moment, "10:00-22:00"))
        self.assertTrue(sync.in_create_window(moment, "22:00-06:00"))


# -- the immediate path ------------------------------------------------------------

class ImmediateCreateTests(GoLiveTestCase):
    """Owner D1 (all hours), D2 (member type Default), D3 (staff keying stays),
    D4 (no phone unchanged), on the live book (owner_shared, max 1 write)."""

    def setUp(self):
        super().setUp()
        self.now = T0 + timedelta(minutes=2)  # 10:02 Singapore time; the 06:00 cutoff is tomorrow

    def immediate(self, direction="shopify_to_ac2", **overrides):
        return self.run_sync(direction, **{**IMMEDIATE, "max_writes_per_run": 1, **overrides})

    def test_an_unmatched_sign_up_is_created_at_once_with_a_count_not_an_alert(self):
        self.signup(50)
        result = self.immediate()
        self.assertEqual([(m, p["member_type"]) for m, p in self.ac2.creates], [(sg(50), "Default")])
        self.assertEqual((result["counts"]["ac2_member_created"], result["counts"]["created_immediately"], result["counts"]["new_member_phone"]), (1, 1, 1))
        self.assertNotIn("created_after_wait", result["counts"])
        self.assertEqual((result["alerts"], result["status"]), ([], "completed"))
        self.assertEqual(result["create_latency_seconds"], [120])
        self.assertEqual((self.links(), self.pending(50)), ({gid(50): sg(50)}, ("DONE", "new_member_phone")))
        self.assert_no_pii(result)

    def test_at_any_hour_by_default(self):
        for n, hour in ((51, 0), (52, 3), (53, 23)):
            at = sgt(21, hour, 1)
            self.signup(n, at=at.astimezone(timezone.utc))
            self.now = at + timedelta(minutes=1)
            self.assertEqual(self.immediate()["counts"]["created_immediately"], 1)
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(51), sg(52), sg(53)])

    def test_outside_the_window_it_waits_for_six_as_today(self):
        self.signup(54)
        result = self.immediate(immediate_create_window_local="11:00-12:00")
        self.assertEqual((result["counts"]["waiting_for_staff"], self.ac2.creates, self.pending(54)), (1, [], ("PENDING", "waiting_for_staff")))
        self.now = sgt(21, 11, 0).astimezone(timezone.utc)
        self.assertEqual(self.immediate(immediate_create_window_local="11:00-12:00")["counts"]["created_immediately"], 1)

    def test_switch_off_or_creates_off_is_todays_rule(self):
        self.signup(55)
        for overrides in ({"immediate_create_enabled": False}, {"ac2_create_enabled": False}):
            with self.subTest(**overrides):
                result = self.immediate(**overrides)
                self.assertEqual((result["counts"]["waiting_for_staff"], result["alerts"], self.ac2.creates), (1, [], []))
                self.assertNotIn("sign_up_past_cutoff_create_disabled", result["counts"])

    def test_created_after_wait_only_on_the_six_oclock_path(self):
        self.signup(56)
        self.immediate(immediate_create_window_local="11:00-12:00")  # waits (outside the window)
        self.now = sgt(22, 6, 0).astimezone(timezone.utc)  # the cutoff, still outside the window
        result = self.immediate(immediate_create_window_local="11:00-12:00")
        self.assertEqual((result["counts"]["created_after_wait"], result["alerts"]), (1, ["created_after_wait"]))
        self.assertNotIn("created_immediately", result["counts"])
        # Latency is recorded on both paths while the switch is on ...
        self.assertEqual(result["create_latency_seconds"], [int((sgt(22, 6, 0) - sgt(21, 10, 0)).total_seconds())])
        # ... and never with it off (today's output, unchanged).
        self.signup(57)
        off = self.run_sync("shopify_to_ac2")
        self.assertEqual(off["counts"]["created_after_wait"], 1)
        self.assertNotIn("create_latency_seconds", off)

    def test_staff_key_at_hq_before_the_lookup_links_with_no_write(self):
        # D3: staff keep keying by hand. The member saved at HQ before the
        # sync looks is linked by MemberNo + CreatedTime; nothing is written.
        self.ac2.add(sync.Ac2Member(sg(58), "SOMEONE TYPED", None, created_at=T0 + timedelta(minutes=1)))
        self.signup(58)
        result = self.immediate()
        self.assertEqual((self.links(), self.ac2.creates, result["counts"]["memberno_and_created_time_match"]), ({gid(58): sg(58)}, [], 1))
        self.assertNotIn("created_immediately", result["counts"])

    def test_staff_save_between_lookup_and_save_is_proven_not_written_then_linked(self):
        # D3 / G0 r2 race 2: staff save the same MemberNo at HQ after the
        # sync's lookup and before its save. The bridge refuses the save
        # before the claim (member_no_taken, proven not written); the next
        # run finds the staff member and links it. Never created twice.
        class StaffSavesFirst(FakeAc2):
            def create_member(inner, member_no, payload, *, write_id):
                inner.creates.append((member_no, dict(payload)))
                inner.add(sync.Ac2Member(member_no, "SOMEONE TYPED", None, created_at=T0 + timedelta(minutes=1)), log=True)
                return "NOT_WRITTEN_PROVEN", "member_no_taken"

        self.ac2 = StaffSavesFirst()
        self.signup(59)
        first = self.immediate()
        self.assertEqual((len(self.ac2.creates), first["counts"]["ac2_create_not_written"], self.pending(59)), (1, 1, ("PENDING", "ac2_not_written")))
        self.assertEqual(self.ledger.conn.execute("SELECT state, code FROM writes").fetchall()[0][:], ("NOT_WRITTEN", "member_no_taken"))
        self.assertEqual(self.links(), {})
        self.now += timedelta(minutes=1)
        second = self.immediate()
        self.assertEqual((self.links(), len(self.ac2.creates), second["counts"]["memberno_and_created_time_match"]), ({gid(59): sg(59)}, 1, 1))
        self.assertEqual(self.pending(59), ("DONE", "memberno_and_created_time_match"))

    def test_staff_key_after_the_sync_created_is_a_possible_duplicate_alert(self):
        self.signup(60, first="Mary Ann", last="Lee")
        self.immediate()
        self.ac2.add(sync.Ac2Member("TILL60", "MARY ANN LEE", local(60), created_at=T0 + timedelta(minutes=5)))
        self.now += timedelta(minutes=10)
        self.assertIn("possible_duplicate_member", self.immediate()["alerts"])
        self.assertEqual(len(self.ac2.creates), 1)

    def test_no_phone_sign_up_is_unchanged(self):
        # D4: never GetNextMemberNo on the live book; with the switch on the
        # staff alert is raised at once instead of after the cutoff.
        self.signup(61, phone=None)
        result = self.immediate()
        self.assertEqual((result["alerts"], self.ac2.creates, self.ac2.generator_calls), (["no_phone_sign_up_needs_staff"], [], 0))
        self.assertEqual(self.pending(61), ("SKIPPED", "no_phone_sign_up_needs_staff"))
        self.signup(62, phone=None)
        off = self.run_sync("shopify_to_ac2")  # switch off: waits, as today
        self.assertEqual((off["counts"]["waiting_for_staff"], self.pending(62)), (1, ("PENDING", "waiting_for_staff")))

    def test_ac2_to_shopify_never_sends_the_sync_created_member(self):
        self.run_sync("ac2_to_shopify")  # ChangeLog watermark
        self.signup(63)
        result = self.immediate("both")
        self.assertEqual((len(self.ac2.creates), self.shopify.created, result["counts"]["ac2_insert_linked_or_own_write"]), (1, [], 1))
        self.now += timedelta(minutes=1)
        again = self.immediate("both")
        self.assertEqual((len(self.ac2.creates), self.shopify.created), (1, []))
        self.assertNotIn("shopify_customer_created", again["counts"])

    def test_pending_sign_ups_from_before_the_switch_are_created_on_the_next_run(self):
        for n in (64, 65):
            self.signup(n)
        self.run_sync("shopify_to_ac2")  # switch off: both wait
        result = self.immediate(max_writes_per_run=5)
        self.assertEqual((result["counts"]["created_immediately"], sorted(m for m, _ in self.ac2.creates)), (2, [sg(64), sg(65)]))


class CreateCapTests(GoLiveTestCase):
    """Owner D5: caps per hour and per day, both directions together, counted
    from the ledger's writes rows; over a cap the sign-up is held and falls
    back to the 06:00 rule, with one alert per hour."""

    def setUp(self):
        super().setUp()
        self.now = T0 + timedelta(minutes=2)

    def immediate(self, **overrides):
        return self.run_sync("shopify_to_ac2", **{**IMMEDIATE, "max_writes_per_run": 5, "max_creates_per_hour": 2, "max_creates_per_day": 3, **overrides})

    def restart(self) -> None:
        self.ledger.close()
        self.ledger = sync.SyncLedger(self.temp, clock=lambda: self.now, check_custody=False)

    def test_the_owner_defaults(self):
        config = live_config(**IMMEDIATE)
        self.assertEqual((config.max_creates_per_hour, config.max_creates_per_day), (6, 144))
        for n in range(1, 9):
            self.signup(n)
        result = self.run_sync("shopify_to_ac2", **IMMEDIATE, max_writes_per_run=5)
        self.assertEqual(result["counts"]["created_immediately"], 5)  # the per-run limit first
        result = self.run_sync("shopify_to_ac2", **IMMEDIATE, max_writes_per_run=5)
        self.assertEqual((result["counts"]["created_immediately"], result["counts"]["create_rate_capped"]), (1, 2))
        self.assertIn("create_rate_capped", result["alerts"])

    def test_over_the_hourly_cap_held_then_falls_back_to_six(self):
        for n in range(1, 5):
            self.signup(n)
        result = self.immediate()
        self.assertEqual((result["counts"]["created_immediately"], result["counts"]["create_rate_capped"]), (2, 2))
        self.assertEqual(result["alerts"], ["create_rate_capped"])  # once, with the run's first capped ref
        self.assertEqual([r["code"] for r in result["refs"]].count("create_rate_capped"), 2)
        self.assertEqual((self.pending(3), self.pending(4)), (("PENDING", "create_rate_capped"),) * 2)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 2)  # no write for the held ones
        # The 06:00 rule creates them at the cutoff, caps or not.
        self.now = sgt(22, 6, 0).astimezone(timezone.utc)
        late = self.immediate(max_creates_per_hour=1, max_creates_per_day=1)
        self.assertEqual((late["counts"]["created_after_wait"], sorted(m for m, _ in self.ac2.creates)), (2, [sg(n) for n in range(1, 5)]))
        self.assertNotIn("create_rate_capped", late["counts"])

    def test_caps_survive_a_restart(self):
        for n in range(1, 4):
            self.signup(n)
        self.immediate()
        self.restart()  # a new process: the counts come from the ledger
        self.now = T0 + timedelta(minutes=30)
        result = self.immediate()
        self.assertEqual((result["counts"]["create_rate_capped"], len(self.ac2.creates)), (1, 2))
        self.assertEqual(result["alerts"], [])  # alerted 28 minutes ago, in the ledger
        self.restart()
        self.now = T0 + timedelta(minutes=63)  # the first writes left the hour
        result = self.immediate()
        self.assertEqual((result["counts"]["created_immediately"], len(self.ac2.creates)), (1, 3))
        self.signup(4)
        self.restart()
        self.now = T0 + timedelta(hours=3)  # hour free; the day cap (3) is reached
        result = self.immediate()
        self.assertEqual((result["counts"]["create_rate_capped"], len(self.ac2.creates), result["alerts"]), (1, 3, ["create_rate_capped"]))
        self.now = T0 + timedelta(hours=24, minutes=3)  # the day is rolling 24 hours
        self.signup(5, at=T0 + timedelta(hours=24))
        self.restart()
        self.assertEqual(self.immediate()["counts"]["created_immediately"], 1)

    def test_the_hourly_cap_is_a_rolling_sixty_minutes(self):
        # G2 #285 L1: a write 59 minutes old counts, one 61 minutes old does not.
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO writes(write_id, direction, member_no, member_no_key, state, created_at, updated_at) "
                         "VALUES ('write-edge', 'shopify_to_ac2', 'E1', 'e1', 'DONE', ?, ?)", (dl.ts(self.now - timedelta(minutes=59)),) * 2)
        self.signup(1)
        result = self.immediate(max_creates_per_hour=1, max_creates_per_day=10)
        self.assertEqual((result["counts"].get("create_rate_capped"), self.ac2.creates), (1, []))
        self.now += timedelta(minutes=2)  # the write is now 61 minutes old
        result = self.immediate(max_creates_per_hour=1, max_creates_per_day=10)
        self.assertEqual((result["counts"].get("created_immediately"), len(self.ac2.creates)), (1, 1))

    def test_one_cap_alert_per_hour(self):
        for n in range(1, 5):
            self.signup(n)
        self.assertEqual(self.immediate()["alerts"], ["create_rate_capped"])
        self.now = T0 + timedelta(minutes=61)
        self.assertEqual(self.immediate(max_creates_per_hour=1, max_creates_per_day=2)["alerts"], [])  # 59 minutes since the alert
        self.now = T0 + timedelta(minutes=62)
        self.assertEqual(self.immediate(max_creates_per_hour=1, max_creates_per_day=2)["alerts"], ["create_rate_capped"])
        self.assertEqual(self.ledger.watermark("create_rate_capped_alerted_at"), dl.ts(T0 + timedelta(minutes=62)))

    def test_both_directions_count(self):
        with self.ledger.tx() as conn:
            for n in (1, 2):
                conn.execute("INSERT INTO writes(write_id, direction, customer_gid, member_no, member_no_key, state, created_at, updated_at) "
                             "VALUES (?, 'ac2_to_shopify', ?, ?, ?, 'DONE', ?, ?)", (f"write-{n}", gid(90 + n), f"T{n}", f"t{n}", dl.ts(self.now), dl.ts(self.now)))
        self.signup(6)
        result = self.immediate()
        self.assertEqual((result["counts"]["create_rate_capped"], self.ac2.creates), (1, []))

    def test_a_capped_sign_up_is_created_once_the_hour_frees(self):
        for n in range(1, 4):
            self.signup(n)
        self.immediate()
        self.now = T0 + timedelta(minutes=63)
        self.assertEqual(self.immediate()["counts"]["created_immediately"], 1)
        self.assertEqual(self.pending(3), ("DONE", "new_member_phone"))


class WallClockHoldTests(GoLiveTestCase):
    """G0 r2 section 6: woken runs would shorten run-count holds, so with the
    switch on they are wall-clock: today's 5 runs at 15 minutes (60 minutes
    from the first hold). With it off they stay run counts."""

    def setUp(self):
        super().setUp()
        self.now = T0 + timedelta(minutes=2)

    def late_metafield(self, n: int) -> None:
        self.shopify.add(replace(customer(n), created_at=T0, registration_at=None, registration_value=None))

    def test_off_the_sign_up_time_hold_counts_runs_as_today(self):
        self.late_metafield(70)
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS - 1):
            self.assertEqual(self.run_sync("shopify_to_ac2")["counts"]["shopify_registration_event_pending"], 1)
        result = self.run_sync("shopify_to_ac2")  # 5th run, same minute: the wait is spent
        self.assertEqual(result["counts"]["waiting_for_staff"], 1)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM watermarks WHERE name LIKE 'hold_since:%'").fetchone()[0], 0)

    def test_on_the_sign_up_time_hold_is_sixty_minutes_whatever_the_run_count(self):
        self.assertEqual(sync.REGISTRATION_WAIT, timedelta(minutes=60))
        self.late_metafield(71)
        for minute in [2] * 8 + [30, 61]:
            self.now = T0 + timedelta(minutes=minute)
            self.assertEqual(self.run_sync("shopify_to_ac2", **IMMEDIATE)["counts"]["shopify_registration_event_pending"], 1, minute)
        self.assertEqual(self.ac2.creates, [])
        self.now = T0 + timedelta(minutes=62)  # 60 minutes after the first hold
        result = self.run_sync("shopify_to_ac2", **IMMEDIATE)
        self.assertEqual((result["counts"]["created_immediately"], [m for m, _ in self.ac2.creates]), (1, [sg(71)]))
        self.assertIsNone(self.ledger.watermark(f"hold_since:shopify:{gid(71)}"))

    def test_on_the_hold_clock_survives_a_restart(self):
        self.late_metafield(72)
        self.run_sync("shopify_to_ac2", **IMMEDIATE)
        self.ledger.close()
        self.ledger = sync.SyncLedger(self.temp, clock=lambda: self.now, check_custody=False)
        self.now = T0 + timedelta(minutes=61)
        self.assertEqual(self.run_sync("shopify_to_ac2", **IMMEDIATE)["counts"]["shopify_registration_event_pending"], 1)
        self.now = T0 + timedelta(minutes=62)
        self.assertEqual(self.run_sync("shopify_to_ac2", **IMMEDIATE)["counts"]["created_immediately"], 1)

    def setup_ac2_hold(self, n: int) -> None:
        self.run_sync("ac2_to_shopify")  # ChangeLog watermark
        self.late_metafield(n)
        self.ac2.add(sync.Ac2Member(sg(n), "SOMEONE TYPED", None, created_at=T0 + timedelta(minutes=1)), log=True)

    def test_off_the_ac2_hold_counts_runs_as_today(self):
        self.setup_ac2_hold(73)
        for _ in range(sync.REGISTRATION_WAIT_MAX_CHECKS - 1):
            self.assertIn("ac2_member_held_for_sign_up", self.run_sync("ac2_to_shopify")["alerts"])
        self.run_sync("ac2_to_shopify")
        self.assertEqual(self.links(), {gid(73): sg(73)})  # Customer.createdAt stands in, within the window

    def test_on_the_ac2_hold_is_sixty_minutes_whatever_the_run_count(self):
        self.setup_ac2_hold(74)
        for minute in [2] * 8 + [61]:
            self.now = T0 + timedelta(minutes=minute)
            self.assertIn("ac2_member_held_for_sign_up", self.run_sync("ac2_to_shopify", **IMMEDIATE)["alerts"])
        self.assertEqual(self.links(), {})
        self.now = T0 + timedelta(minutes=62)
        self.run_sync("ac2_to_shopify", **IMMEDIATE)
        self.assertEqual((self.links(), self.shopify.created), ({gid(74): sg(74)}, []))

    def test_on_the_stuck_hold_is_sixty_minutes(self):
        # More sign-ups pending than one run reads (bound 3): the walk-in is
        # held, then stuck after 60 minutes, never created while stuck.
        self.run_sync("ac2_to_shopify")
        for n in range(75, 80):
            self.signup(n)
        self.run_sync("shopify_to_ac2", **IMMEDIATE, immediate_create_window_local="23:00-23:30")  # 5 waiting (outside the window)
        self.ac2.add(sync.Ac2Member(sg(30), "Walk In Person", None, created_at=T0 + timedelta(minutes=1)), log=True)
        settings = {**IMMEDIATE, "max_pending_rechecks_per_run": 3, "immediate_create_window_local": "23:00-23:30"}
        for minute in [3] * 8 + [62]:
            self.now = T0 + timedelta(minutes=minute)
            self.assertIn("ac2_member_held_for_sign_up", self.run_sync("ac2_to_shopify", **settings)["alerts"])
        self.now = T0 + timedelta(minutes=63)
        self.assertIn("ac2_member_hold_stuck", self.run_sync("ac2_to_shopify", **settings)["alerts"])
        self.assertEqual(self.shopify.created, [])

    def test_hold_clocks_of_items_no_longer_held_are_pruned(self):
        self.late_metafield(76)
        self.run_sync("shopify_to_ac2", **IMMEDIATE)
        self.assertIsNotNone(self.ledger.watermark(f"hold_since:shopify:{gid(76)}"))
        self.shopify.customers[gid(76)] = replace(self.shopify.customers[gid(76)], registration_at=T0)  # the metafield arrives
        self.run_sync("shopify_to_ac2", **IMMEDIATE)
        self.run_sync("shopify_to_ac2", **IMMEDIATE)
        self.assertIsNone(self.ledger.watermark(f"hold_since:shopify:{gid(76)}"))


class DigestImmediateTests(GoLiveTestCase):
    def test_digest_prints_latency_writes_per_hour_caps_and_probe_days(self):
        root = self.private_root()
        self.ledger.close()
        self.temp, self.ledger = root, sync.SyncLedger(root, clock=lambda: self.now)
        self.ledger.init_meta(autocount_book=PROD_BOOK, store_gid=STORE, shop_domain=DOMAIN)
        with self.ledger.tx() as conn:
            self.ledger.set_watermark(conn, "initial_link_completed_at", dl.ts(CUTOVER))
        for n, minutes in ((1, 1), (2, 3), (3, 9)):
            self.signup(n)
            self.now = T0 + timedelta(minutes=minutes)
            self.run_sync("shopify_to_ac2", **IMMEDIATE, max_creates_per_hour=2, max_creates_per_day=2)
        config = config_value(**LIVE_KEYS, state_root=str(root))
        with mock.patch.object(dl, "utc_now", return_value=self.now):
            code, out = self.cli("digest", "--hours", "24", config=config)
        self.assertEqual(code, 0)
        self.assertEqual(out["create_latency_seconds"], {"count": 2, "p50": 60, "p95": 180, "max": 180})
        self.assertEqual((out["creates"]["created_immediately"], out["creates"]["writes_per_hour_max"], out["creates"]["writes_per_hour"]),
                         (2, 2, {"2026-10-21T10": 2}))
        self.assertEqual(out["caps"], {"create_rate_capped": 1, "create_rate_capped_alerts": 1})
        self.assertIsNone(out["probe"])
        probe = _probe_module()
        status = probe._default_status()
        status["days"] = {"2026-10-20": {"checks": 5, "wakes": 9, "would_wakes": 0, "reasons": {}},
                          "2026-10-21": {"checks": 7, "wakes": 2, "would_wakes": 1, "reasons": {"new_sign_up": 2}}}
        status["last_check_at"], status["last_decision"], status["last_reason"] = dl.ts(self.now), "no_work", "no_new_sign_up"
        probe.save_status(root, status)
        with mock.patch.object(dl, "utc_now", return_value=self.now):
            _, out = self.cli("digest", "--hours", "6", config=config)
        self.assertEqual(out["probe"]["days"], {"2026-10-21": {"checks": 7, "wakes": 2, "would_wakes": 1}})
        (root / probe.PROBE_STATUS_FILE).write_text("{not json", encoding="utf-8")
        with mock.patch.object(dl, "utc_now", return_value=self.now):
            _, out = self.cli("digest", "--hours", "6", config=config)
        self.assertEqual(out["probe"], {"status": "unreadable"})
        self.assert_no_pii(out)

    def test_nearest_rank_percentiles(self):
        self.assertEqual([sync._nearest_rank(list(range(1, 101)), p) for p in (50, 95, 100)], [50, 95, 100])
        self.assertEqual((sync._nearest_rank([7], 95), sync._nearest_rank([], 50)), (7, None))


# -- P2: the sign-up probe -----------------------------------------------------------

SESSION_READ = {"shop": {"id": STORE, "myshopifyDomain": DOMAIN}, "currentAppInstallation": {"accessScopes": [{"handle": "read_customers"}]}}


def probe_node(n: int, updated: datetime, tags=("member-mg",)) -> dict:
    return {"id": gid(n), "tags": list(tags), "updatedAt": dl.ts(updated)}


class ProbeTestCase(GoLiveTestCase):
    """The probe against a real (temporary) live-book ledger that a SyncLedger
    keeps open, so it holds the run's lock the whole time: the probe must
    work without it."""

    def setUp(self):
        super().setUp()
        self.probe = _probe_module()
        self.now = T0 + timedelta(minutes=2)
        self.nodes: list[dict] = []
        self.sent: list[dict] = []
        self.starts = 0
        self.start_ok = True

    def transport(self, url, body, headers, timeout):
        request = json.loads(body)
        self.sent.append(request)
        if request["query"] == direct.SESSION_QUERY:
            return _graphql(SESSION_READ)
        return _graphql({"customers": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.nodes}})

    def starter(self) -> bool:
        self.starts += 1
        return self.start_ok

    def tick(self, **overrides) -> dict:
        config = live_config(**overrides)
        return self.probe.run_probe(config, self.temp, environ=PROBE_TOKEN_ENV, transport=self.transport, clock=lambda: self.now, starter=self.starter)

    def wake_settings(self, **overrides) -> dict:
        return {"probe_wake_enabled": True, "immediate_create_enabled": True, "ac2_create_enabled": True, **overrides}

    def add_run(self, started: datetime, finished: datetime | None) -> None:
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO runs(run_id, started_at, finished_at) VALUES (?, ?, ?)", (f"run-{started.timestamp()}", dl.ts(started), dl.ts(finished) if finished else None))

    def status(self) -> dict:
        return json.loads((self.temp / self.probe.PROBE_STATUS_FILE).read_text(encoding="utf-8"))


class ProbeDecisionTests(ProbeTestCase):
    def test_no_work_when_every_sign_up_is_known(self):
        self.signup(1)
        self.run_sync("shopify_to_ac2")  # waiting_for_staff: a pending row
        self.nodes = [probe_node(1, T0), probe_node(2, T0, tags=("other",))]
        result = self.tick(**self.wake_settings())
        self.assertEqual((result["decision"], result["reason"], self.starts), ("no_work", "no_new_sign_up", 0))
        self.assertEqual(result["counts"], {"sign_ups_seen": 2, "new_sign_ups": 0, "known_sign_ups": 1, "search_complete": 1})

    def test_observe_mode_records_would_wake_once_per_piece_of_work(self):
        self.nodes = [probe_node(3, T0)]
        result = self.tick()
        self.assertEqual((result["decision"], result["reason"], self.starts, result["mode"]), ("would_wake", "observe_mode", 0, "observe"))
        self.now += timedelta(seconds=90)
        self.assertEqual(self.tick()["reason"], "work_already_woken")
        self.nodes.append(probe_node(4, T0 + timedelta(minutes=1)))
        self.assertEqual(self.tick()["decision"], "would_wake")
        self.assertEqual(self.status()["days"]["2026-10-21"]["would_wakes"], 2)

    def test_wake_starts_the_main_task_once_per_piece_of_work(self):
        self.nodes = [probe_node(5, T0)]
        result = self.tick(**self.wake_settings())
        self.assertEqual((result["decision"], result["reason"], self.starts, result["wakes_today"]), ("wake", "new_sign_up", 1, 1))
        self.now += timedelta(seconds=90)
        self.assertEqual((self.tick(**self.wake_settings())["reason"], self.starts), ("work_already_woken", 1))

    def test_debounce_sixty_seconds_after_the_last_start_or_wake(self):
        self.add_run(self.now - timedelta(seconds=59), self.now - timedelta(seconds=50))
        self.nodes = [probe_node(6, T0)]
        self.assertEqual(self.tick(**self.wake_settings())["reason"], "debounce_recent_start")
        self.now += timedelta(seconds=1)  # 60 s after the run start
        self.assertEqual(self.tick(**self.wake_settings())["decision"], "wake")
        self.nodes.append(probe_node(7, T0 + timedelta(minutes=1)))
        self.now += timedelta(seconds=59)
        self.assertEqual((self.tick(**self.wake_settings())["reason"], self.starts), ("debounce_recent_start", 1))
        self.now += timedelta(seconds=1)
        self.assertEqual((self.tick(**self.wake_settings())["decision"], self.starts), ("wake", 2))

    def test_no_wake_while_a_run_is_unfinished_or_just_hard_stopped(self):
        self.nodes = [probe_node(8, T0)]
        started = self.now - timedelta(minutes=9, seconds=59)
        self.add_run(started, None)
        self.assertEqual(self.tick(**self.wake_settings())["reason"], "run_unfinished")
        self.now = started + timedelta(minutes=10)  # Task Scheduler stops it at PT10M
        self.assertEqual(self.tick(**self.wake_settings())["reason"], "run_hard_stopped_recently")
        self.now = started + timedelta(minutes=11, seconds=59)
        self.assertEqual(self.tick(**self.wake_settings())["reason"], "run_hard_stopped_recently")
        self.now = started + timedelta(minutes=12)  # 120 s after the hard stop
        self.assertEqual((self.tick(**self.wake_settings())["decision"], self.starts), ("wake", 1))

    def test_daily_wake_cap_per_singapore_day(self):
        settings = self.wake_settings(probe_max_wakes_per_day=1)
        self.nodes = [probe_node(9, T0)]
        self.assertEqual(self.tick(**settings)["decision"], "wake")
        self.nodes.append(probe_node(10, T0 + timedelta(minutes=1)))
        self.now += timedelta(minutes=2)
        self.assertEqual((self.tick(**settings)["reason"], self.starts), ("wake_cap_reached", 1))
        self.now = sgt(22, 0, 0).astimezone(timezone.utc)  # a new Singapore day
        self.assertEqual((self.tick(**settings)["decision"], self.starts), ("wake", 2))
        self.assertEqual({day: entry["wakes"] for day, entry in self.status()["days"].items()}, {"2026-10-21": 1, "2026-10-22": 1})

    def test_wake_needs_the_immediate_path_its_window_and_room_under_the_caps(self):
        self.nodes = [probe_node(11, T0)]
        self.assertEqual(self.tick(**self.wake_settings(immediate_create_enabled=False))["reason"], "immediate_create_off")
        self.nodes = [probe_node(12, T0 + timedelta(minutes=1))]
        self.now += timedelta(minutes=2)
        self.assertEqual(self.tick(**self.wake_settings(ac2_create_enabled=False))["reason"], "immediate_create_off")
        self.nodes = [probe_node(13, T0 + timedelta(minutes=2))]
        self.now += timedelta(minutes=2)
        self.assertEqual(self.tick(**self.wake_settings(immediate_create_window_local="22:00-23:00"))["reason"], "outside_create_window")
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO writes(write_id, direction, member_no, member_no_key, state, created_at, updated_at) VALUES ('write-x', 'shopify_to_ac2', 'X1', 'x1', 'DONE', ?, ?)",
                         (dl.ts(self.now), dl.ts(self.now)))
        self.assertEqual(self.tick(**self.wake_settings(max_creates_per_hour=1, max_creates_per_day=1))["reason"], "create_rate_capped")
        self.assertEqual(self.starts, 0)

    def test_writes_last_hour_is_a_rolling_sixty_minutes(self):
        # G2 #285 L1: a write 59 minutes old counts, one 61 minutes old does not.
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO writes(write_id, direction, member_no, member_no_key, state, created_at, updated_at) "
                         "VALUES ('write-edge', 'shopify_to_ac2', 'E1', 'e1', 'DONE', ?, ?)", (dl.ts(self.now - timedelta(minutes=59)),) * 2)
        conn = self.probe._open_read_only(self.temp, live_config())
        try:
            facts = self.probe.ledger_facts(conn, live_config(), [], self.now)
            self.assertEqual((facts.writes_last_hour, facts.writes_last_day), (1, 1))
            facts = self.probe.ledger_facts(conn, live_config(), [], self.now + timedelta(minutes=2))
            self.assertEqual((facts.writes_last_hour, facts.writes_last_day), (0, 1))
        finally:
            conn.close()
        settings = self.wake_settings(max_creates_per_hour=1, max_creates_per_day=10)
        self.nodes = [probe_node(19, T0)]
        self.assertEqual((self.tick(**settings)["reason"], self.starts), ("create_rate_capped", 0))
        self.now += timedelta(minutes=2)  # the write is now 61 minutes old
        self.assertEqual((self.tick(**settings)["decision"], self.starts), ("wake", 1))

    def test_newest_sign_ups_come_first_past_two_pages(self):
        # G2 #285 L3: 150 sign-ups match (3 pages). A store that honours the
        # query's order serves them newest first only for reverse: true, so
        # the newest is seen on page 1; at most 2 pages of 50 are read.
        signups = [probe_node(n, T0 + timedelta(minutes=n)) for n in range(1, 151)]

        def store(url, body, headers, timeout):
            request = json.loads(body)
            self.sent.append(request)
            if request["query"] == direct.SESSION_QUERY:
                return _graphql(SESSION_READ)
            newest_first = re.search(r"customers\([^)]*\breverse: true\b", request["query"]) is not None
            ordered = sorted(signups, key=lambda node: node["updatedAt"], reverse=newest_first)
            page = {None: 0, "cursor-1": 1, "cursor-2": 2}[request["variables"]["after"]]
            first = request["variables"]["first"]
            nodes = ordered[page * first:(page + 1) * first]
            return _graphql({"customers": {"pageInfo": {"hasNextPage": (page + 1) * first < len(ordered), "endCursor": f"cursor-{page + 1}"}, "nodes": nodes}})

        result = self.probe.run_probe(live_config(), self.temp, environ=PROBE_TOKEN_ENV, transport=store, clock=lambda: self.now, starter=self.starter)
        self.assertEqual((result["decision"], result["counts"]["sign_ups_seen"], result["counts"]["search_complete"]), ("would_wake", 100, 0))
        self.assertEqual(self.status()["work_marker"], dl.ts(T0 + timedelta(minutes=150)))  # the newest sign-up
        self.assertEqual(len(self.sent), 3)  # the session check and 2 pages of 50, no more

    def test_nothing_before_the_initial_link_completes(self):
        with self.ledger.tx() as conn:
            conn.execute("DELETE FROM watermarks WHERE name = 'initial_link_completed_at'")
        self.nodes = [probe_node(14, T0)]
        self.assertEqual(self.tick(**self.wake_settings())["reason"], "initial_link_not_complete")

    def test_a_failed_task_start_is_recorded_and_exits_three(self):
        self.start_ok = False
        self.nodes = [probe_node(15, T0)]
        result = self.tick(**self.wake_settings())
        self.assertEqual((result["decision"], result["reason"], result["wakes_today"]), ("wake_failed", "task_start_failed", 0))
        self.assertIsNone(self.status()["work_marker"])  # tried again on the next tick
        with mock.patch.object(self.probe, "run_probe", return_value=result):
            code, out = self.cli("probe", config=config_value(**LIVE_KEYS, **self.wake_settings(), state_root=str(self.private_root())))
        self.assertEqual((code, out["decision"]), (3, "wake_failed"))

    def test_the_search_starts_at_the_sync_watermark(self):
        with self.ledger.tx() as conn:
            self.ledger.set_watermark(conn, "shopify_updated_at", "2026-10-21T01:45:00Z")
        self.tick()
        self.assertEqual([r["query"] for r in self.sent], [direct.SESSION_QUERY, self.probe.PROBE_QUERY])
        self.assertEqual(self.sent[1]["variables"], {"first": 50, "after": None, "search": "tag:'member-mg' AND updated_at:>='2026-10-21T01:45:00Z'"})


class ProbeStatusTests(ProbeTestCase):
    def test_status_file_holds_counts_times_and_codes_only(self):
        self.signup(16)
        self.nodes = [probe_node(16, T0), probe_node(17, T0)]
        self.tick(**self.wake_settings())
        status = self.status()
        self.assertEqual(set(status), {"schema", "mode", "last_check_at", "last_decision", "last_reason", "last_wake_at", "last_would_wake_at", "work_marker", "last_counts", "days"})
        text = json.dumps(status)
        self.assertNotIn("Customer/", text)
        self.assertNotIn(str(700000 + 16), text)
        self.assert_no_pii(status)
        self.assertTrue(self.probe._valid_status(status))

    def test_an_unreadable_status_file_fails_closed_for_the_day(self):
        (self.temp / self.probe.PROBE_STATUS_FILE).write_text("[]", encoding="utf-8")
        self.nodes = [probe_node(18, T0)]
        result = self.tick(**self.wake_settings())
        self.assertEqual((result["reason"], self.starts), ("wake_cap_reached", 0))
        self.assertEqual(self.status()["days"]["2026-10-21"]["reasons"]["probe_status_reset"], 1)
        self.now = sgt(22, 0, 1).astimezone(timezone.utc)
        self.assertEqual(self.tick(**self.wake_settings())["decision"], "wake")

    def test_a_refusal_is_recorded_then_raised(self):
        def broken(url, body, headers, timeout):
            return direct.HttpResponse(401, {}, b"")

        with self.assertRaises(dl.DirectLocalError) as caught:
            self.probe.run_probe(live_config(), self.temp, environ=PROBE_TOKEN_ENV, transport=broken, clock=lambda: self.now, starter=self.starter)
        status = self.status()
        self.assertEqual((status["last_decision"], status["last_reason"]), ("refused", caught.exception.code))
        self.assertEqual(self.starts, 0)

    def test_a_corrupt_ledger_is_refused_as_digest_refuses_it(self):
        # G2 #285 L2: exit 2, the status file records the reason, no wake.
        root = self.private_root()
        (root / sync.LEDGER_FILE).write_bytes(b"not a sqlite database, synthetic " * 64)
        config = config_value(**LIVE_KEYS, **self.wake_settings(), state_root=str(root))
        probe = _probe_module()
        with mock.patch.object(probe, "start_main_task", side_effect=AssertionError("no wake")), mock.patch.object(dl, "utc_now", return_value=self.now):
            code, out = self.cli("probe", config=config)
            digest_code, digest_out = self.cli("digest", "--hours", "24", config=config)
        self.assertEqual((digest_code, digest_out), (2, {"status": "refused", "code": "sync_ledger_unreadable"}))
        self.assertEqual((code, out), (digest_code, digest_out))
        status = json.loads((root / probe.PROBE_STATUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual((status["last_decision"], status["last_reason"], status["last_wake_at"]), ("refused", "sync_ledger_unreadable", None))

    def test_old_days_are_dropped(self):
        for day in range(1, 20):
            self.now = datetime(2026, 10, day, 4, 0, tzinfo=timezone.utc)
            self.tick()
        self.assertEqual(len(self.status()["days"]), self.probe.STATUS_DAYS_KEPT)


class ProbeSafetyTests(ProbeTestCase):
    """Pass bar (G0 r2 P2): the probe never starts the bridge, never loads
    AutoCount, has no write path, takes no ledger lock and reads no name,
    email or phone field."""

    SOURCE = ROOT / "directlocal" / "xb_member_sync_probe.py"

    def code_only(self) -> str:
        """The probe's source with every comment and docstring blanked out
        (everything else, string constants included, kept as written)."""

        import ast
        import io as _io
        import tokenize

        source = self.SOURCE.read_text(encoding="utf-8")
        docstrings = {(node.body[0].value.lineno, node.body[0].value.col_offset) for node in ast.walk(ast.parse(source))
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)}
        lines = [list(line) for line in source.splitlines(keepends=True)]
        for token in tokenize.generate_tokens(_io.StringIO(source).readline):
            if token.type == tokenize.COMMENT or (token.type == tokenize.STRING and token.start in docstrings):
                (row, col), (end_row, end_col) = token.start, token.end
                for r in range(row, end_row + 1):
                    first = col if r == row else 0
                    last = end_col if r == end_row else len(lines[r - 1])
                    for c in range(first, last):
                        if lines[r - 1][c] not in "\r\n":
                            lines[r - 1][c] = " "
        return "".join("".join(line) for line in lines)

    def test_static_no_bridge_no_autocount_no_write_no_lock(self):
        code = self.code_only()
        forbidden = ("BridgeClient", "default_bridge_command", "write_once", "create_member", "next_member_no", "powershell", ".ps1", "AutoCount",
                     "sqlcmd", "SyncLedger", "SyncEngine", "_OwnerLock", "member_sync.lock", ".tx(", "BEGIN", "INSERT", "UPDATE ", "DELETE",
                     "customerCreate", "CUSTOMER_CREATE_MUTATION", "create_customer", "mutation", "build_real_engine", "load_ac2_credential", "Popen")
        self.assertEqual([word for word in forbidden if word in code], [])
        self.assertEqual(code.count("sqlite3.connect("), 1)
        self.assertIn('"?mode=ro"', code)
        self.assertIn('"PRAGMA query_only=ON"', code)
        self.assertEqual(code.count("subprocess.run("), 1)
        self.assertEqual(self.probe.MAIN_TASK, "\\XB\\XB-MemberSync")

    def test_static_reads_no_contact_field(self):
        import re as _re

        # (The ledger's own watermarks.name column is the only "name" in the code.)
        self.assertEqual(_re.findall(r"(?i)phone|e-?mail|first_?name|last_?name|display_?name|address|metafield|birthday", self.code_only()), [])
        selection = _re.search(r"nodes \{ ([^}]*) \}", self.probe.PROBE_QUERY).group(1).split()
        self.assertEqual(selection, ["id", "tags", "updatedAt"])
        # G2 #285 L3: newest first, the same 2 pages of 50 at most.
        self.assertIn("customers(first: $first, after: $after, sortKey: UPDATED_AT, reverse: true, query: $search)", self.probe.PROBE_QUERY)
        self.assertEqual((self.probe.PROBE_PAGE_SIZE, self.probe.PROBE_MAX_PAGES), (50, 2))
        self.assertEqual(self.probe.PROBE_NODE_KEYS, frozenset(selection))

    def test_static_imports(self):
        import ast

        tree = ast.parse(self.SOURCE.read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "json", "os", "sqlite3", "subprocess", "time", "dataclasses", "datetime", "pathlib", "typing",
                                    "xb_directlocal", "xb_member_sync", "xb_shopify_direct", "xb_shopify_poller"})

    def test_client_allows_only_the_session_check_and_the_probe_query(self):
        client = self.probe.ProbeShopifyClient(create_enabled=True, shop_domain=DOMAIN, api_version="2026-07",
                                               token_provider=direct.StaticTokenProvider("shpat_synthetic_token_value_0001"), transport=lambda *a: None)
        self.assertEqual(self.probe.PROBE_QUERIES, frozenset({direct.SESSION_QUERY, self.probe.PROBE_QUERY}))
        for query in (sync.CUSTOMER_CREATE_MUTATION, sync.REGISTRATIONS_QUERY, sync.CUSTOMER_QUERY, sync.PHONE_LOOKUP_QUERY):
            with self.subTest(query=query[:40]), self.assertRaises(direct.ShopifyDirectError) as caught:
                client.execute(query, {})
            self.assertEqual(caught.exception.code, "shopify_query_not_pinned")
        self.assertFalse(hasattr(client, "create_customer") or hasattr(client, "registrations") or hasattr(client, "find_by_phone"))

    def test_a_contact_field_in_the_answer_is_refused(self):
        self.nodes = [{**probe_node(19, T0), "displayName": "Alpha Tester"}]
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.tick()
        self.assertEqual(caught.exception.code, "shopify_response_shape_invalid")

    def test_runtime_no_lock_no_ledger_write_no_bridge_no_process(self):
        # The SyncLedger of this test holds the run's lock the whole time.
        self.signup(20)
        self.run_sync("shopify_to_ac2")
        self.now += timedelta(minutes=2)  # past the 60 s debounce of that run
        self.ledger.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ledger_file = self.temp / sync.LEDGER_FILE
        before = hashlib.sha256(ledger_file.read_bytes()).hexdigest()
        self.nodes = [probe_node(20, T0), probe_node(21, T0)]

        def refuse(*args, **kwargs):
            raise AssertionError("the probe must not reach this")

        with mock.patch.object(dl, "BridgeClient", side_effect=refuse), mock.patch.object(dl, "_OwnerLock", side_effect=refuse), \
                mock.patch.object(sync, "SyncLedger", side_effect=refuse), mock.patch.object(sync, "SyncEngine", side_effect=refuse), \
                mock.patch("subprocess.Popen", side_effect=refuse), mock.patch("subprocess.run", side_effect=refuse):
            result = self.tick(**self.wake_settings())
        self.assertEqual((result["decision"], self.starts), ("wake", 1))
        self.assertEqual(hashlib.sha256(ledger_file.read_bytes()).hexdigest(), before)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)

    def test_the_task_start_command_is_pinned(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "System32" / "schtasks.exe"
            exe.parent.mkdir()
            exe.write_bytes(b"")
            self.assertEqual(self.probe.task_start_command({"SystemRoot": folder}), [str(exe), "/Run", "/TN", "\\XB\\XB-MemberSync"])
            calls = []
            with mock.patch("subprocess.run", side_effect=lambda argv, **kw: calls.append((argv, kw)) or mock.Mock(returncode=0)):
                self.assertTrue(self.probe.start_main_task({"SystemRoot": folder}))
            self.assertEqual(calls[0][0], [str(exe), "/Run", "/TN", "\\XB\\XB-MemberSync"])
            self.assertEqual((calls[0][1]["capture_output"], calls[0][1]["check"]), (True, False))
        for environ in ({}, {"SystemRoot": ""}, {"SystemRoot": "relative"}):
            with self.subTest(environ=environ), self.assertRaises(dl.DirectLocalError) as caught:
                self.probe.task_start_command(environ)
            self.assertEqual(caught.exception.code, "probe_task_starter_missing")



# -- #155 slice BF1: older sign-ups into AutoCount (backfill) and coverage ----------

import sqlite3  # noqa: E402

BF_OLD = CUTOVER - timedelta(days=30)  # a sign-up from before the cutover
BF_KEYS = {"backfill_registrations_enabled": True, "ac2_create_member_type": "GUEST"}
GUEST = "GUEST"


class BackfillShopify(FakeShopify):
    """The backfill and coverage read customers by one exact tag."""

    def __init__(self):
        super().__init__()
        self.incomplete: set[str] = set()
        self.loose = False  # a search that also returns customers without the tag

    def registrations(self, search, max_pages, birthday):
        match = re.fullmatch(r"tag:'([a-z-]+)'", search)
        if match is None:
            return super().registrations(search, max_pages, birthday)  # the live run's searches
        self.searches.append(search)
        tag = match.group(1)
        return [c for c in self.customers.values() if self.loose or tag in c.tags], tag not in self.incomplete


def old_sign_up(n: int, *, first="Alpha", last="Tester", phone="sg", tags=("member-mg",), birthday=None, made=None) -> sync.ShopifyCustomer:
    """A sign-up from before the cutover: no ``membership.start_date`` (made
    at the cutover), so the sign-up time is Customer.createdAt."""

    item = customer(n, first=first, last=last, phone=phone, tags=tags, birthday=birthday)
    made = BF_OLD if made is None else made
    return replace(item, created_at=made, updated_at=made, registration_at=None, registration_value=None)


def make_member_core_ledger(root: Path, *, claims=(), book=TEST_BOOK, tool="xb268_member_benefits") -> None:
    """A member core ledger with the real #268 schema, written directly."""

    import xb_member_benefits as mb

    conn = sqlite3.connect(root / mb.LEDGER_FILE)
    try:
        conn.executescript(mb.LEDGER_SCHEMA.replace("CHECK (tool = 'xb268_member_benefits')", "") if tool != "xb268_member_benefits" else mb.LEDGER_SCHEMA)
        conn.execute("INSERT INTO meta(id, tool, ledger_id, autocount_book, store_gid, shop_domain, ref_key, created_at) VALUES (1, ?, 'b-1', ?, ?, ?, ?, ?)",
                     (tool, book, STORE, DOMAIN, "6b" * 32, dl.ts(T0)))
        for customer_gid, member_no in claims:
            conn.execute("INSERT INTO claim_links(customer_gid, member_no, member_no_key, source, code, created_at) VALUES (?, ?, ?, 'order', 'claim_test', ?)",
                         (customer_gid, member_no, sync.member_no_key(member_no), dl.ts(T0)))
        conn.commit()
    finally:
        conn.close()


class BackfillTestCase(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.shopify = BackfillShopify()

    def member(self, member_no: str, name: str = "Alpha Tester", mobile: str | None = None) -> sync.Ac2Member:
        item = sync.Ac2Member(member_no, name, mobile, created_at=T0 - timedelta(days=900))
        self.ac2.add(item)
        return item

    def backfill(self, *, apply: bool = False, max_creates: int = 5, claims=None, **overrides) -> dict:
        settings = {**BF_KEYS, **overrides}
        return self.engine(**settings).backfill_registrations(apply=apply, max_creates=max_creates, claims=claims)

    def link_rows(self) -> dict:
        return {row[0]: (row[1], row[2], row[3]) for row in self.ledger.conn.execute("SELECT customer_gid, member_no, origin, code FROM links")}

    def dump(self) -> dict:
        tables = [row[0] for row in self.ledger.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: sorted(tuple(row) for row in self.ledger.conn.execute(f"SELECT * FROM {name}")) for name in tables}

    def assert_private(self, value) -> None:
        self.assert_no_pii(value)
        text = json.dumps(value)
        self.assertNotIn("gid://shopify/Customer", text)
        # No customer ID (not inside a keyed ref's hex, so no chance match).
        self.assertIsNone(re.search(r"(?<![0-9a-f])7000[0-9]{2}(?![0-9a-f])", text))
        for n in range(1, 100):
            self.assertNotIn(sg(n), text)


class BackfillRuleTests(BackfillTestCase):
    """Every row of the G0 tables 2.1 and 2.2, and Controller decisions 1-3."""

    def decide(self, item, members):
        return sync.decide_backfill(item, sync.phone_member_no(item.phone)[0], members)

    def test_mg_sign_up_with_no_member_is_created_as_guest_with_the_phone_member_no(self):
        self.shopify.add(old_sign_up(1, birthday="7"))
        result = self.backfill(apply=True)
        self.assertEqual(len(self.ac2.creates), 1)
        member_no, payload = self.ac2.creates[0]
        self.assertEqual(member_no, sg(1))
        self.assertEqual(payload["member_type"], GUEST)
        self.assertEqual(payload["profile"], "sync_p1")
        self.assertNotIn("expiry_date", payload)  # no expiry: the write-once child sets none for sync_p1
        self.assertEqual(payload["register_date"], BF_OLD.astimezone(SGT).strftime("%Y-%m-%d"))  # the sign-up date
        self.assertEqual(payload["dob"], "2000-07-01")  # the configured birthday field, as the live sync
        self.assertEqual(self.link_rows(), {gid(1): (sg(1), "created_in_ac2", "backfill_new_member_phone")})
        self.assertEqual(result["counts"]["backfill_ac2_member_created"], 1)
        self.assertEqual(self.ledger.conn.execute("SELECT state, code FROM writes").fetchall()[0][:], ("DONE", "created"))

    def test_register_date_prefers_the_start_date_value(self):
        today = lambda: "2000-01-01"  # noqa: E731
        item = replace(customer(1), registration_value="2026-10-19")  # signed up on the 21st, the form's date is the 19th
        self.assertEqual(sync.sync_p1_payload(item, "Alpha Tester", sg(1), GUEST, None, today)["register_date"], "2026-10-19")
        for bad in ("19/10/2026", "2026-10-19T00:00:00", None, 20261019):
            self.assertEqual(sync.sync_p1_payload(replace(item, registration_value=bad), "A B", None, GUEST, None, today)["register_date"], "2026-10-21")
        self.assertEqual(sync.sync_p1_payload(replace(item, registration_value=None, registration_at=None, created_at=None), "A B", None, GUEST, 3, today),
                         {"profile": "sync_p1", "member_type": GUEST, "name": "A B", "mobile_phone": None, "email_address": None,
                          "register_date": "2000-01-01", "dob": "2000-03-01"})

    def test_foreign_number_is_created_with_the_full_number(self):
        self.shopify.add(old_sign_up(2, phone=plus(FOREIGN_DIGITS)))
        self.backfill(apply=True)
        self.assertEqual([m for m, _ in self.ac2.creates], [FOREIGN_DIGITS])

    def test_exact_member_no_links_with_no_time_window_and_no_name_rule(self):
        self.shopify.add(old_sign_up(3))
        self.member(sg(3), "SOMEONE ELSE")  # made 900 days before: far outside any window
        result = self.backfill(apply=True)
        self.assertEqual(self.link_rows(), {gid(3): (sg(3), "matched", "backfill_member_no")})
        self.assertEqual((self.ac2.creates, result["counts"]["backfill_linked"]), ([], 1))

    def test_the_eight_digit_member_no_is_the_same_number(self):
        self.shopify.add(old_sign_up(4))
        self.member(local(4))
        self.backfill(apply=True)
        self.assertEqual(self.link_rows(), {gid(4): (local(4), "matched", "backfill_member_no")})

    def test_phone_in_mobile_phone_with_the_same_name_links_not_creates(self):
        self.shopify.add(old_sign_up(5))
        self.member("TILL5", "ALPHA TESTER", mobile=plus(sg(5)))
        self.backfill(apply=True)
        self.assertEqual(self.link_rows(), {gid(5): ("TILL5", "matched", "backfill_phone_and_name")})
        self.assertEqual(self.ac2.creates, [])

    def test_phone_inside_a_longer_member_no_with_the_same_name_links(self):
        self.shopify.add(old_sign_up(6))
        self.member(sg(6) + "AT", "Alpha Tester")
        self.backfill(apply=True)
        self.assertEqual(self.link_rows(), {gid(6): (sg(6) + "AT", "matched", "backfill_phone_and_name")})

    def test_shared_phone_under_another_name_is_held_for_a_person(self):
        self.shopify.add(old_sign_up(7))
        self.member("TILL7", "BRAVO OTHER", mobile=local(7))
        result = self.backfill(apply=True)
        self.assertEqual((self.link_rows(), self.ac2.creates), ({}, []))
        self.assertEqual(result["counts"]["backfill_shared_phone"], 1)
        self.assertEqual([r["code"] for r in result["refs"]], ["backfill_shared_phone"])

    def test_two_same_name_records_on_the_phone_are_held(self):
        item = old_sign_up(8)
        members = [sync.Ac2Member("TILL8A", "Alpha Tester", local(8)), sync.Ac2Member("TILL8B", "Alpha Tester", local(8))]
        self.assertEqual(self.decide(item, members), sync.Decision("HOLD", "backfill_phone_on_multiple_records"))

    def test_rows_that_do_not_hold_the_phone_are_never_used(self):
        # The lookup is only a candidate list: a row that holds neither the
        # phone nor its 8 digits never links, holds or blocks a create.
        item = old_sign_up(26)
        stranger = sync.Ac2Member("TILL26", "Alpha Tester", plus(sg(27)))
        self.assertEqual(self.decide(item, [stranger]), sync.Decision("CREATE", "backfill_new_member_phone", sg(26)))
        other_name = sync.Ac2Member(sg(27), "BRAVO OTHER", None)
        self.assertEqual(self.decide(item, [other_name]).action, "CREATE")

    def test_both_forms_of_the_number_are_held_never_linked_or_created(self):
        self.shopify.add(old_sign_up(9))
        self.member(sg(9))
        self.member(local(9))
        result = self.backfill(apply=True)
        self.assertEqual((self.link_rows(), self.ac2.creates), ({}, []))
        self.assertEqual(result["counts"]["backfill_memberno_conflict"], 1)
        self.assertEqual(result["refs"][0]["member_no_masked"][-3:], sg(9)[-3:])

    def test_legacy_customer_with_a_member_links_and_without_one_is_never_created(self):
        self.shopify.add(old_sign_up(10, tags=("member-mg", "member-legacy")))
        self.member(sg(10))
        self.shopify.add(old_sign_up(11, tags=("member-mg", "member-legacy")))
        result = self.backfill(apply=True)
        self.assertEqual(self.link_rows(), {gid(10): (sg(10), "matched", "backfill_member_no")})
        self.assertEqual((self.ac2.creates, result["counts"]["backfill_legacy_needs_person"]), ([], 1))

    def test_legacy_only_customers_are_not_read_by_the_backfill(self):
        self.shopify.add(old_sign_up(12, tags=("member-legacy",)))
        result = self.backfill(apply=True)
        self.assertEqual((self.ac2.lookups, self.ac2.creates, result["population"]["tagged"]), (0, [], 0))

    def test_a_loose_search_result_without_the_sign_up_tag_is_never_decided(self):
        self.shopify.loose = True  # the search is only a candidate list
        self.shopify.add(old_sign_up(27, tags=("member-legacy",)))
        self.shopify.add(old_sign_up(28, tags=()))
        result = self.backfill(apply=True)
        self.assertEqual((self.ac2.lookups, self.ac2.creates, result["population"]["tagged"]), (0, [], 0))

    def test_no_phone_malformed_phone_and_no_name_are_held_before_any_lookup(self):
        self.shopify.add(old_sign_up(13, phone=None))
        self.shopify.add(old_sign_up(14, phone="12345"))
        self.shopify.add(old_sign_up(15, first="  ", last="-"))
        result = self.backfill(apply=True)
        self.assertEqual((self.ac2.lookups, self.ac2.creates, self.link_rows()), (0, [], {}))
        self.assertEqual({k: result["counts"][k] for k in ("backfill_no_phone", "backfill_phone_invalid", "backfill_no_name")},
                         {"backfill_no_phone": 1, "backfill_phone_invalid": 1, "backfill_no_name": 1})
        self.assertEqual(len(result["refs"]), 3)

    def test_precheck_order_and_sign_up_time(self):
        cut = CUTOVER
        self.assertEqual(sync.backfill_precheck(old_sign_up(16), cut), None)
        self.assertEqual(sync.backfill_precheck(old_sign_up(16, made=cut), cut), "backfill_signed_up_after_cutover")
        self.assertEqual(sync.backfill_precheck(replace(old_sign_up(16), registration_at=cut + timedelta(minutes=1)), cut), "backfill_signed_up_after_cutover")
        self.assertEqual(sync.backfill_precheck(replace(old_sign_up(16), created_at=None), cut), "backfill_signup_time_missing")
        self.assertEqual(sync.backfill_precheck(old_sign_up(16, first=None, last=None, phone=None), cut), "backfill_no_name")

    def test_signed_up_after_the_cutover_is_left_to_the_live_sync(self):
        self.shopify.add(customer(17))  # signed up at T0, after the cutover
        result = self.backfill(apply=True)
        self.assertEqual((self.ac2.lookups, result["counts"]["backfill_signed_up_after_cutover"]), (0, 1))

    def test_a_customer_already_linked_is_skipped_without_a_lookup(self):
        self.shopify.add(old_sign_up(18))
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(18), sg(18), "matched", "initial_link_member_no")
        result = self.backfill(apply=True)
        self.assertEqual((self.ac2.lookups, result["counts"]["backfill_already_linked"]), (0, 1))

    def test_member_already_linked_to_another_customer_is_held(self):
        self.shopify.add(old_sign_up(19))
        self.member(sg(19))
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(90), sg(19), "matched", "initial_link_member_no")
        result = self.backfill(apply=True)
        self.assertEqual(result["counts"]["backfill_member_linked_elsewhere"], 1)
        self.assertNotIn(gid(19), self.link_rows())

    def test_two_customers_on_one_new_phone_make_one_member(self):
        self.shopify.add(old_sign_up(20))
        self.shopify.add(replace(old_sign_up(20), gid=gid(21)))
        result = self.backfill(apply=True)
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(20)])
        self.assertEqual(result["counts"]["backfill_member_linked_elsewhere"], 1)
        dry = self.backfill(apply=False)
        self.assertEqual(dry["counts"].get("backfill_would_create"), None)

    def test_dry_run_holds_the_second_customer_on_one_new_phone_too(self):
        self.shopify.add(old_sign_up(22))
        self.shopify.add(replace(old_sign_up(22), gid=gid(23)))
        result = self.backfill(apply=False)
        self.assertEqual((result["counts"]["backfill_would_create"], result["counts"]["backfill_member_linked_elsewhere"]), (1, 1))

    def test_member_with_an_open_write_is_held(self):
        self.shopify.add(old_sign_up(24))
        engine = self.engine(**BF_KEYS)
        engine._intent("shopify_to_ac2", sg(24), gid(91))
        result = engine.backfill_registrations(apply=True, max_creates=5)
        self.assertEqual((result["counts"]["backfill_member_held_by_open_write"], self.ac2.creates), (1, []))

    def test_unstorable_text_is_held(self):
        self.shopify.add(old_sign_up(25, first="Alpha" * 30))
        result = self.backfill(apply=True)
        self.assertEqual((result["counts"]["backfill_text_unstorable"], self.ac2.creates), (1, []))


class BackfillSafetyTests(BackfillTestCase):
    def test_dry_run_writes_nothing_but_its_own_cursor(self):
        for n in (30, 31, 32):
            self.shopify.add(old_sign_up(n))
        self.member(sg(31))
        before = self.dump()
        result = self.backfill(apply=False, max_shopify_customers_per_run=2)
        after = self.dump()
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual((result["counts"]["backfill_would_create"], result["counts"]["backfill_would_link"]), (1, 1))
        changed = {name for name in before if before[name] != after[name]}
        self.assertEqual(changed, {"watermarks"})
        marks = {row[0]: row[1] for row in after["watermarks"]}
        self.assertEqual(set(marks) - {row[0] for row in before["watermarks"]}, {"backfill_registrations_dry_run_cursor"})
        self.assertEqual((self.ac2.creates, self.shopify.created), ([], []))
        self.assertIsNone(self.ledger.watermark("backfill_registrations_cursor"))

    def test_a_finished_dry_run_clears_its_cursor_and_marks_nothing_done(self):
        self.shopify.add(old_sign_up(29))
        before = self.dump()
        result = self.backfill(apply=False)
        self.assertTrue(result["complete"])
        self.assertEqual(self.dump(), before)  # no cursor left, no done mark, nothing else
        self.assertIsNone(self.ledger.watermark("backfill_registrations_completed_at"))

    def test_apply_is_refused_while_the_switch_is_off(self):
        self.shopify.add(old_sign_up(33))
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.backfill(apply=True, backfill_registrations_enabled=False)
        self.assertEqual(caught.exception.code, "backfill_registrations_disabled")
        self.assertEqual((self.ac2.lookups, self.shopify.searches, self.link_rows()), (0, [], {}))
        self.assertEqual(self.backfill(apply=False, backfill_registrations_enabled=False)["mode"], "dry_run")  # a dry run needs no switch

    def test_max_is_bounded_by_the_live_cap(self):
        for bad in (0, -1, 6, True, "1", 1.0):
            with self.subTest(bad=bad), self.assertRaises(dl.DirectLocalError) as caught:
                self.backfill(max_creates=bad)
            self.assertEqual(caught.exception.code, "backfill_max_invalid")
        with self.assertRaises(dl.DirectLocalError):
            self.backfill(max_creates=3, max_writes_per_run=2)
        self.assertEqual(self.backfill(max_creates=2, max_writes_per_run=2)["max_creates"], 2)

    def test_at_most_max_creates_a_call_then_resumes(self):
        for n in range(34, 38):
            self.shopify.add(old_sign_up(n))
        first = self.backfill(apply=True, max_creates=3)
        self.assertEqual((len(self.ac2.creates), first["stopped"], first["complete"]), (3, "backfill_max_reached", False))
        self.assertEqual(self.ledger.watermark("backfill_registrations_cursor"), str(700036))
        second = self.backfill(apply=True, max_creates=3)
        self.assertEqual((len(self.ac2.creates), second["stopped"], second["complete"]), (4, None, True))
        self.assertIsNone(self.ledger.watermark("backfill_registrations_cursor"))
        self.assertIsNotNone(self.ledger.watermark("backfill_registrations_completed_at"))

    def test_never_more_than_max_writes_per_run(self):
        for n in range(38, 42):
            self.shopify.add(old_sign_up(n))
        result = self.backfill(apply=True, max_creates=2, max_writes_per_run=2)
        self.assertEqual((len(self.ac2.creates), result["writes"], result["stopped"]), (2, 2, "backfill_max_reached"))

    def test_a_rolling_hour_stays_within_the_daily_summary_allowance(self):
        self.shopify.add(old_sign_up(42))
        engine = self.engine(**BF_KEYS, max_writes_per_run=2)
        for n in range(8):  # 4 runs an hour x 2 writes
            engine._intent("ac2_to_shopify", f"HOURCAP{n}", None)
        result = engine.backfill_registrations(apply=True, max_creates=1)
        self.assertEqual((result["stopped"], self.ac2.creates), ("backfill_hour_cap_reached", []))
        self.now += timedelta(minutes=45)  # still inside the 60 minutes
        self.assertEqual(self.backfill(apply=True, max_creates=1, max_writes_per_run=2)["stopped"], "backfill_hour_cap_reached")
        self.now += timedelta(minutes=16)
        self.assertEqual(self.backfill(apply=True, max_creates=1, max_writes_per_run=2)["counts"]["backfill_ac2_member_created"], 1)

    def test_seven_writes_in_the_hour_still_allow_the_eighth(self):
        self.shopify.add(old_sign_up(43))
        engine = self.engine(**BF_KEYS, max_writes_per_run=2)
        for n in range(7):
            engine._intent("ac2_to_shopify", f"HOURCAP{n}", None)
        self.assertEqual(engine.backfill_registrations(apply=True, max_creates=1)["counts"]["backfill_ac2_member_created"], 1)

    def test_the_stop_file_stops_before_the_first_customer(self):
        self.shopify.add(old_sign_up(44))
        (self.temp / sync.STOP_FILE).write_text("", encoding="utf-8")
        result = self.backfill(apply=True)
        self.assertEqual((result["stopped"], self.ac2.lookups, self.ac2.creates), ("stop_file_present", 0, []))
        self.assertIsNone(self.ledger.watermark("backfill_registrations_cursor"))

    def test_the_stop_file_stops_before_the_next_customer(self):
        for n in (45, 46):
            self.shopify.add(old_sign_up(n))
        real = self.ac2.create_member

        def create_then_stop(*args, **kwargs):
            outcome = real(*args, **kwargs)
            (self.temp / sync.STOP_FILE).write_text("", encoding="utf-8")
            return outcome

        self.ac2.create_member = create_then_stop
        result = self.backfill(apply=True)
        self.assertEqual(([m for m, _ in self.ac2.creates], result["stopped"]), ([sg(45)], "stop_file_present"))
        self.assertEqual(self.ledger.watermark("backfill_registrations_cursor"), str(700045))

    def test_lookups_a_call_are_bounded_and_the_cursor_resumes(self):
        for n in (47, 48, 49):
            self.shopify.add(old_sign_up(n))
            self.member(sg(n))
        first = self.backfill(apply=True, max_shopify_customers_per_run=2)
        self.assertEqual((self.ac2.lookups, first["stopped"], len(self.link_rows())), (2, "backfill_lookup_budget_reached", 2))
        second = self.backfill(apply=True, max_shopify_customers_per_run=2)
        self.assertEqual((self.ac2.lookups, second["complete"], len(self.link_rows())), (3, True, 3))

    def test_a_held_customer_at_the_cursor_is_not_looked_up_again(self):
        self.shopify.add(old_sign_up(72))
        self.member("TILL72", "BRAVO OTHER", local(72))  # held: shared phone
        self.shopify.add(old_sign_up(73))
        first = self.backfill(apply=True, max_shopify_customers_per_run=1)
        self.assertEqual((first["stopped"], self.ledger.watermark("backfill_registrations_cursor")), ("backfill_lookup_budget_reached", str(700072)))
        second = self.backfill(apply=True, max_shopify_customers_per_run=1)
        self.assertEqual((self.ac2.lookups, [m for m, _ in self.ac2.creates], second["complete"]), (2, [sg(73)], True))

    def test_a_second_pass_does_not_create_twice(self):
        self.shopify.add(old_sign_up(50))
        self.backfill(apply=True)
        result = self.backfill(apply=True)
        self.assertEqual((len(self.ac2.creates), result["counts"]["backfill_already_linked"]), (1, 1))

    def test_an_interrupted_create_is_never_sent_again(self):
        self.shopify.add(old_sign_up(51))

        def die(*args, **kwargs):
            raise SimulatedParentDeath()

        self.ac2.create_member = die
        with self.assertRaises(SimulatedParentDeath):
            self.backfill(apply=True)
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "INTENT")
        unfinished = self.ledger.conn.execute("SELECT finished_at FROM runs WHERE run_id LIKE 'backfill-%'").fetchone()[0]
        self.assertIsNone(unfinished)  # the daily summary sees an unfinished run
        self.ac2 = FakeAc2()
        result = self.backfill(apply=True)
        self.assertEqual((self.ac2.creates, result["counts"]["backfill_held_by_open_write"]), ([], 1))

    def test_an_uncertain_create_stops_the_call_and_is_never_sent_again(self):
        for n in (52, 53):
            self.shopify.add(old_sign_up(n))
        self.ac2.create_result = ("WRITE_OUTCOME_UNCERTAIN", "writer_timeout")
        result = self.backfill(apply=True)
        self.assertEqual((len(self.ac2.creates), result["stopped"]), (1, "backfill_create_uncertain"))
        self.assertEqual(self.ledger.conn.execute("SELECT state FROM writes").fetchone()[0], "UNCERTAIN")
        self.assertEqual(self.link_rows(), {})
        self.ac2.create_result = ("CREATED_VERIFIED", None)
        again = self.backfill(apply=True)
        self.assertEqual(again["counts"]["backfill_held_by_open_write"], 1)
        self.assertEqual([m for m, _ in self.ac2.creates], [sg(52), sg(53)])  # 52 once only

    def test_a_create_proven_not_written_stops_the_call(self):
        for n in (54, 55):
            self.shopify.add(old_sign_up(n))
        self.ac2.create_result = ("NOT_WRITTEN_PROVEN", "member_type_unknown")
        result = self.backfill(apply=True)
        self.assertEqual((len(self.ac2.creates), result["stopped"], result["counts"]["member_type_unknown"]), (1, "backfill_create_not_written", 1))
        self.assertEqual(result["alerts"], [])
        self.assertIsNone(self.ledger.watermark("backfill_registrations_cursor"))

    def test_creates_stay_off_while_the_live_create_switch_is_off(self):
        self.shopify.add(old_sign_up(56))
        result = self.backfill(apply=True, ac2_create_enabled=False)
        self.assertEqual((result["stopped"], self.ac2.creates, result["writes"]), ("ac2_create_disabled", [], 0))

    def test_a_partial_shopify_read_decides_nothing(self):
        self.shopify.add(old_sign_up(57))
        self.shopify.incomplete.add("member-mg")
        result = self.backfill(apply=True)
        self.assertEqual((result["stopped"], result["complete"], self.ac2.lookups, self.ac2.creates), ("backfill_read_incomplete", False, 0, []))

    def test_member_core_claim_links_count_as_linked(self):
        self.shopify.add(old_sign_up(58))
        self.shopify.add(old_sign_up(59))
        claims = sync.ClaimLinks({gid(58): sync.member_no_key("CLAIM58"), gid(92): sync.member_no_key(sg(59))},
                                 {sync.member_no_key("CLAIM58"): gid(58), sync.member_no_key(sg(59)): gid(92)})
        result = self.backfill(apply=True, claims=claims)
        self.assertEqual(result["counts"]["backfill_claim_linked"], 1)
        self.assertEqual(result["counts"]["backfill_member_linked_elsewhere"], 1)  # its new MemberNo is a claimed member
        self.assertEqual((self.ac2.creates, self.link_rows(), result["member_core_ledger"]), ([], {}, "read"))
        self.assertEqual(self.backfill(apply=False)["member_core_ledger"], "not_configured")

    def test_no_shopify_write_and_no_alert(self):
        for n in (60, 61, 62):
            self.shopify.add(old_sign_up(n))
        self.member("TILL61", "BRAVO OTHER", local(61))
        result = self.backfill(apply=True)
        self.assertEqual((self.shopify.created, result["alerts"]), ([], []))

    def test_an_applied_call_is_stored_as_its_own_run_kind_and_the_digest_reads_it(self):
        self.shopify.add(old_sign_up(63))
        self.shopify.add(old_sign_up(64, phone=None))
        self.backfill(apply=True)
        self.backfill(apply=False)  # a dry run stores no run
        rows = self.ledger.conn.execute("SELECT run_id, finished_at, summary FROM runs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][0].startswith("backfill-"))
        stored = json.loads(rows[0][2])
        self.assertEqual((stored["kind"], stored["alerts"]), ("backfill_registrations", []))
        self.assert_private(stored)
        digest = sync.ledger_digest(self.temp, hours=48, now=self.now, config=make_config())
        self.assertEqual((digest["alerts"], digest["counts"]["backfill_ac2_member_created"], digest["runs"]["completed"]), ({}, 1, 1))

    def test_outputs_are_private(self):
        self.shopify.add(old_sign_up(65))
        self.shopify.add(old_sign_up(66))
        self.member(sg(66))
        self.shopify.add(old_sign_up(67))
        self.member("TILL67", "BRAVO OTHER", local(67))
        self.shopify.add(old_sign_up(68, phone=None, first="Bravo"))
        self.shopify.add(old_sign_up(74))
        with self.ledger.tx() as conn:
            self.ledger.add_link(conn, gid(74), sg(74), "matched", "initial_link_member_no")  # already linked
        self.shopify.add(customer(75))  # signed up after the cutover
        self.shopify.add(old_sign_up(76))  # claim-linked in the member core
        claims = sync.ClaimLinks({gid(76): "claim76"}, {"claim76": gid(76)})
        for apply in (False, True):
            result = self.backfill(apply=apply, claims=claims)
            for code in ("backfill_already_linked", "backfill_signed_up_after_cutover", "backfill_claim_linked"):
                self.assertEqual(result["counts"][code], 1)
            self.assert_private(result)
            self.assertTrue(all(set(r) <= {"ref", "action", "code", "member_no_masked"} for r in result["refs"]))
        for (summary,) in self.ledger.conn.execute("SELECT summary FROM runs"):
            self.assert_private(json.loads(summary))

    def test_the_ledger_schema_is_unchanged(self):
        self.shopify.add(old_sign_up(69))
        self.shopify.add(old_sign_up(70))
        self.member(sg(70))
        self.backfill(apply=False)
        self.backfill(apply=True)
        fresh = Path(tempfile.mkdtemp(prefix="xb-member-sync-fresh-"))
        self.addCleanup(shutil.rmtree, fresh, True)
        other = sync.SyncLedger(fresh, check_custody=False)
        try:
            schema = "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            self.assertEqual(self.ledger.conn.execute(schema).fetchall(), other.conn.execute(schema).fetchall())
            self.assertEqual(self.ledger.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        finally:
            other.close()
        self.assertEqual(sync.SCHEMA_VERSION, 3)

    def test_the_live_run_reads_backfill_links_as_any_link(self):
        self.shopify.add(old_sign_up(71))
        self.backfill(apply=True)
        result = self.run_sync("shopify_to_ac2")
        self.assertEqual(result["counts"].get("shopify_already_linked"), 1)
        self.assertEqual(len(self.ac2.creates), 1)


class BackfillCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="xb-member-sync-bf-cli-"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def main(self, args, **overrides) -> tuple[int, dict]:
        path = self.temp / "config.json"
        path.write_text(json.dumps(config_value(state_root=str(self.temp / "missing-root"), **overrides)), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = sync.main([args[0], "--config", str(path), *args[1:]])
        return code, json.loads(out.getvalue())

    def test_apply_and_max_are_refused_before_any_login_read_or_lock(self):
        with mock.patch.object(sync, "build_real_engine", side_effect=AssertionError("no login")), \
                mock.patch.object(sync, "SyncLedger", side_effect=AssertionError("no lock")), \
                mock.patch.object(sync, "load_claim_links", side_effect=AssertionError("no read")):
            self.assertEqual(self.main(["backfill-registrations", "--apply"]), (2, {"status": "refused", "code": "backfill_registrations_disabled"}))
            self.assertEqual(self.main(["backfill-registrations", "--apply", "--max", "1"], ac2_create_enabled=True),
                             (2, {"status": "refused", "code": "backfill_registrations_disabled"}))
            for bad in ("0", "6", "-1"):
                self.assertEqual(self.main(["backfill-registrations", "--max", bad]), (2, {"status": "refused", "code": "backfill_max_invalid"}))
            self.assertEqual(self.main(["backfill-registrations", "--max", "2"], max_writes_per_run=1),
                             (2, {"status": "refused", "code": "backfill_max_invalid"}))

    def test_cli_runs_a_dry_run_unless_apply_is_given_and_never_writes_to_shopify(self):
        engine = mock.Mock()
        engine.backfill_registrations.return_value = {"stopped": None}
        with mock.patch.object(sync, "_state_root", return_value=self.temp), mock.patch.object(sync, "SyncLedger"), \
                mock.patch.object(sync, "build_real_engine", return_value=engine):
            code, _ = self.main(["backfill-registrations"])
            self.assertEqual(code, 0)
            engine.backfill_registrations.assert_called_once_with(apply=False, max_creates=1, claims=None)
            self.assertIs(engine.shopify.create_enabled, False)
            code, _ = self.main(["backfill-registrations", "--apply", "--max", "3"], backfill_registrations_enabled=True)
            self.assertEqual(code, 0)
            engine.backfill_registrations.assert_called_with(apply=True, max_creates=3, claims=None)
            for stopped in ("backfill_create_not_written", "backfill_create_uncertain", "backfill_read_incomplete"):
                engine.backfill_registrations.return_value = {"stopped": stopped}
                self.assertEqual(self.main(["backfill-registrations"])[0], 3)
            for stopped in ("stop_file_present", "backfill_max_reached", "backfill_lookup_budget_reached", "backfill_hour_cap_reached"):
                engine.backfill_registrations.return_value = {"stopped": stopped}
                self.assertEqual(self.main(["backfill-registrations"])[0], 0)

    def test_coverage_opens_no_lock_and_no_autocount_session(self):
        report = {"schema": sync.COVERAGE_SCHEMA}
        with mock.patch.object(sync.dl, "validate_state_root", return_value=self.temp), mock.patch.object(sync.dl, "verify_private_state"), \
                mock.patch.object(sync, "build_read_only_shopify", return_value=mock.Mock()) as shop, \
                mock.patch.object(sync, "coverage_report", return_value=report) as cover, \
                mock.patch.object(sync, "build_real_engine", side_effect=AssertionError("no AutoCount")), \
                mock.patch.object(sync, "SyncLedger", side_effect=AssertionError("no lock")):
            self.assertEqual(self.main(["coverage"]), (0, report))
        self.assertIsNone(cover.call_args.kwargs["claims"])
        shop.assert_called_once()

    def test_the_read_only_shopify_client_refuses_every_write(self):
        import xb_shopify_poller as poller

        with mock.patch.object(poller, "token_provider", return_value=lambda: "shpat_synthetic"),                 mock.patch.object(sync.SyncShopifyClient, "verify_session") as verify:
            client = sync.build_read_only_shopify(make_config(shopify_create_enabled=True), self.temp, environ={})
        verify.assert_called_once_with(store_gid=STORE)
        self.assertIs(client.create_enabled, False)
        self.assertFalse(client._query_allowed(sync.CUSTOMER_CREATE_MUTATION))
        with self.assertRaises(sync.WriteRefused):
            client.create_customer({"firstName": "Alpha"})


class BackfillConfigTests(unittest.TestCase):
    def test_defaults_are_off(self):
        config = make_config()
        self.assertEqual((config.backfill_registrations_enabled, config.member_core_state_root), (False, None))

    def test_switch_is_a_strict_boolean_and_the_root_is_another_root(self):
        for bad in (1, 0, "true", None):
            with self.subTest(bad=bad), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(backfill_registrations_enabled=bad)
            self.assertEqual(caught.exception.code, "sync_config_backfill_registrations_enabled_invalid")
        for bad in ("", 1, True, ["C:\\XB\\core"], "C:\\XB\\xb155-member-sync"):
            with self.subTest(bad=bad), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(member_core_state_root=bad)
            self.assertEqual(caught.exception.code, "sync_config_member_core_state_root_invalid")
        self.assertEqual(make_config(member_core_state_root="C:\\XB\\xb268-member-core").member_core_state_root, "C:\\XB\\xb268-member-core")

    def test_lookalike_keys_are_refused(self):
        for key in ("backfill_enabled", "backfill_registrations_enabled ", "Backfill_registrations_enabled", "member_core_root"):
            with self.subTest(key=key), self.assertRaises(dl.DirectLocalError) as caught:
                make_config(**{key: False})
            self.assertEqual(caught.exception.code, "sync_config_keys_invalid")

    def check_config(self, **overrides) -> dict:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(config_value(**overrides)), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(sync.main(["check-config", "--config", str(path)]), 0)
        return json.loads(out.getvalue())

    def test_check_config_prints_exactly_as_before_with_the_keys_absent_or_off(self):
        before = self.check_config()
        self.assertEqual(self.check_config(backfill_registrations_enabled=False, member_core_state_root=None), before)
        on = self.check_config(backfill_registrations_enabled=True, member_core_state_root="C:\\XB\\xb268-member-core")
        self.assertEqual({k: v for k, v in on.items() if k not in before}, {"backfill_registrations_enabled": True, "member_core_state_root_set": True})
        self.assertNotIn("xb268", json.dumps(on))  # the path itself is never printed

    def test_live_example_config_is_unchanged_and_still_parses(self):
        example = json.loads((ROOT / "config" / "member_sync.live.example.json").read_text(encoding="utf-8"))
        self.assertNotIn("backfill_registrations_enabled", example)
        example["shopify_cutover_utc"] = dl.ts(CUTOVER)
        self.assertFalse(sync.parse_config(example).backfill_registrations_enabled)

    def test_member_core_names_match_the_member_core(self):
        import xb_member_benefits as mb

        self.assertEqual((sync.MEMBER_CORE_LEDGER_FILE, sync.MEMBER_CORE_TOOL), (mb.LEDGER_FILE, "xb268_member_benefits"))
        self.assertIn("tool = 'xb268_member_benefits'", mb.LEDGER_SCHEMA)


class BackfillRunUnchangedTests(unittest.TestCase):
    """With the BF1 keys on, every scheduled run decides exactly as before
    (the defaults-off golden trace): the switch gates only the backfill."""

    def test_the_golden_trace_with_the_backfill_keys_on(self):
        trace = defaults_off_trace(sync, {"backfill_registrations_enabled": True, "member_core_state_root": "C:\\XB\\xb268-member-core"})
        self.assertEqual(trace_digest(trace), GOLDEN_TRACE_SHA256)


class CoverageTests(BackfillTestCase):
    def setUp(self):
        super().setUp()
        self.core = Path(tempfile.mkdtemp(prefix="xb-member-core-"))
        self.addCleanup(shutil.rmtree, self.core, True)

    def every_class_world(self) -> sync.ClaimLinks:
        """At least one customer in every Shopify class and one member in
        every AC2 class."""

        add = self.shopify.add
        add(old_sign_up(10))                                  # held: backfill_shared_phone (from an applied run)
        self.member("TILL10", "BRAVO OTHER", local(10))
        self.backfill(apply=True)
        add(old_sign_up(1))                                   # linked (sync ledger), also claimed: linked_both
        add(old_sign_up(2))                                   # linked_claim_only
        add(old_sign_up(3))                                   # open_write
        add(old_sign_up(4, tags=("member-legacy",)))          # legacy_not_linked
        add(customer(5))                                      # after_cutover_not_linked
        add(old_sign_up(6, phone=None))                       # backfill_no_phone
        add(old_sign_up(7, phone="12345"))                    # backfill_phone_invalid
        add(old_sign_up(8, first=None, last=None))            # backfill_no_name
        add(replace(old_sign_up(9), created_at=None))         # backfill_signup_time_missing
        add(old_sign_up(11))                                  # gap
        add(old_sign_up(12))                                  # linked, member claimed by another customer: linked_conflict
        add(old_sign_up(13))                                  # linked, and claimed to another member
        add(sync.ShopifyCustomer(gid(14), "Bravo", "Other", None, None, BF_OLD, BF_OLD, ("other",), None, None, None))  # neither tag: not counted
        with self.ledger.tx() as conn:
            for n in (1, 12, 13):
                self.ledger.add_link(conn, gid(n), sg(n), "matched", "initial_link_member_no")
        self.engine()._intent("shopify_to_ac2", sg(3), gid(3))
        self.shopify.loose = True
        claims = [(gid(1), sg(1)), (gid(2), "CLAIM2"), (gid(93), sg(12)), (gid(13), "CLAIM13X"), (gid(94), "CLAIMONLY94")]
        make_member_core_ledger(self.core, claims=claims)
        return sync.read_claim_links(self.core, make_config())

    def report(self, claims) -> dict:
        return sync.coverage_report(self.temp, make_config(), shopify=self.shopify, claims=claims)

    def test_every_class_present_and_both_sides_add_up(self):
        claims = self.every_class_world()
        result = self.report(claims)
        shop = result["shopify"]
        self.assertEqual(shop["classes"], {
            "after_cutover_not_linked": 1, "backfill_no_name": 1, "backfill_no_phone": 1, "backfill_phone_invalid": 1, "backfill_shared_phone": 1,
            "backfill_signup_time_missing": 1, "gap": 1, "legacy_not_linked": 1, "linked": 3, "linked_claim_only": 1, "open_write": 1})
        self.assertEqual(sum(shop["classes"].values()), shop["tagged_total"])
        self.assertEqual(shop["tagged_total"], 13)
        self.assertEqual((shop["gap"], shop["after_cutover_not_linked"], shop["needs_person"]), (1, 1, 6))
        ac2 = result["ac2"]
        self.assertEqual(ac2["classes"], {"linked_both": 1, "linked_claim_only": 3, "linked_conflict": 1, "linked_sync_only": 1})
        self.assertEqual(sum(ac2["classes"].values()), ac2["linked_members_total"])
        self.assertEqual((ac2["customers_linked_to_two_members"], ac2["members_in_book"]), (1, "not_read"))
        self.assertFalse(result["complete"])
        self.assertEqual(result["member_core_ledger"], "read")
        self.assert_private(result)

    def test_coverage_writes_nothing_and_needs_no_lock(self):
        claims = self.every_class_world()
        core_file = self.core / sync.MEMBER_CORE_LEDGER_FILE
        before, core_before = self.dump(), core_file.read_bytes()
        self.report(claims)  # the test's own ledger holds the lock throughout
        self.assertEqual((self.dump(), core_file.read_bytes()), (before, core_before))
        self.assertEqual(self.shopify.created, [])
        self.assertEqual(self.shopify.searches[-2:], ["tag:'member-mg'", "tag:'member-legacy'"])

    def test_refs_match_the_ledger_refs(self):
        self.shopify.add(old_sign_up(20, phone=None))
        result = self.report(None)
        self.assertEqual(result["refs"], [{"ref": self.ledger.ref("c", gid(20)), "code": "backfill_no_phone"}])
        self.assertEqual(result["member_core_ledger"], "not_configured")

    def test_a_later_link_clears_a_hold_and_complete_needs_no_gap(self):
        self.shopify.add(old_sign_up(21))
        self.member("TILL21", "BRAVO OTHER", local(21))
        self.backfill(apply=True)
        self.assertEqual(self.report(None)["shopify"]["classes"], {"backfill_shared_phone": 1})
        self.ac2.members = [sync.Ac2Member(sg(21), "Alpha Tester", None)]
        self.backfill(apply=True)
        result = self.report(None)
        self.assertEqual((result["shopify"]["classes"], result["complete"]), ({"linked": 1}, True))

    def test_the_read_only_open_refuses_writes_even_without_query_only(self):
        conn = sync._open_read_only(self.temp / sync.LEDGER_FILE, "missing", "unreadable")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO watermarks(name, value, updated_at) VALUES ('x', 'y', 'z')")
            conn.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):  # the read-only open itself
                conn.execute("INSERT INTO watermarks(name, value, updated_at) VALUES ('x', 'y', 'z')")
        finally:
            conn.close()
        self.assertIsNone(self.ledger.watermark("x"))

    def test_an_open_write_alone_is_not_complete(self):
        self.shopify.add(old_sign_up(25))
        self.engine()._intent("shopify_to_ac2", sg(25), gid(25))
        result = self.report(None)
        self.assertEqual((result["shopify"]["classes"], result["complete"]), ({"open_write": 1}, False))

    def test_only_backfill_runs_of_their_own_kind_give_holds(self):
        self.shopify.add(old_sign_up(26))
        ref = self.ledger.ref("c", gid(26))
        forged = json.dumps({"counts": {}, "alerts": [], "refs": [{"ref": ref, "action": "hold", "code": "backfill_shared_phone"}]})
        with self.ledger.tx() as conn:
            conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('backfill-x', ?, ?, ?)", (dl.ts(T0), dl.ts(T0), forged))
            conn.execute("INSERT INTO runs(run_id, started_at, finished_at, summary) VALUES ('run-x', ?, ?, ?)",
                         (dl.ts(T0), dl.ts(T0), json.dumps({"kind": "backfill_registrations", "refs": json.loads(forged)["refs"]})))
        self.assertEqual(self.report(None)["shopify"]["classes"], {"gap": 1})

    def test_a_customer_with_both_tags_follows_the_sign_up_path(self):
        self.shopify.add(old_sign_up(23, tags=("member-mg", "member-legacy")))
        self.shopify.add(old_sign_up(24, tags=("member-legacy",)))
        self.assertEqual(self.report(None)["shopify"]["classes"], {"gap": 1, "legacy_not_linked": 1})

    def test_a_later_outcome_that_is_not_a_hold_clears_the_hold(self):
        self.shopify.add(old_sign_up(22))
        self.member("TILL22", "BRAVO OTHER", local(22))
        self.backfill(apply=True)
        self.assertEqual(self.report(None)["shopify"]["classes"], {"backfill_shared_phone": 1})
        self.ac2.members = []  # staff fixed the other record; the create is then proven not written
        self.ac2.create_result = ("NOT_WRITTEN_PROVEN", "member_no_taken")
        self.backfill(apply=True)
        self.assertEqual(self.report(None)["shopify"]["classes"], {"gap": 1})

    def test_a_partial_read_or_another_ledger_is_refused(self):
        self.shopify.incomplete.add("member-legacy")
        with self.assertRaises(dl.DirectLocalError) as caught:
            self.report(None)
        self.assertEqual(caught.exception.code, "coverage_read_incomplete")
        self.shopify.incomplete.clear()
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.coverage_report(self.temp, live_config(), shopify=self.shopify, claims=None)
        self.assertEqual(caught.exception.code, "sync_ledger_binding_mismatch")

    def test_member_core_ledger_is_read_only_and_bound(self):
        make_member_core_ledger(self.core, claims=[(gid(30), "CLAIM30")])
        claims = sync.read_claim_links(self.core, make_config())
        self.assertEqual((dict(claims.by_gid), repr(claims)), ({gid(30): "claim30"}, "ClaimLinks()"))
        other = Path(tempfile.mkdtemp(prefix="xb-member-core-other-"))
        self.addCleanup(shutil.rmtree, other, True)
        make_member_core_ledger(other, book=PROD_BOOK)
        for root, code in ((other, "member_core_ledger_binding_mismatch"), (self.temp, "member_core_ledger_missing")):
            with self.subTest(code=code), self.assertRaises(dl.DirectLocalError) as caught:
                sync.read_claim_links(root, make_config())
            self.assertEqual(caught.exception.code, code)
        wrong_tool = Path(tempfile.mkdtemp(prefix="xb-member-core-tool-"))
        self.addCleanup(shutil.rmtree, wrong_tool, True)
        make_member_core_ledger(wrong_tool, tool="xb_other_tool")
        with self.assertRaises(dl.DirectLocalError) as caught:
            sync.read_claim_links(wrong_tool, make_config())
        self.assertEqual(caught.exception.code, "member_core_ledger_binding_mismatch")
        self.assertIsNone(sync.load_claim_links(make_config()))


if __name__ == "__main__":
    unittest.main()
