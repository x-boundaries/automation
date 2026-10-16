"""#155 DirectLocal real-operation Core (2026-10-15): registration-event
eligibility, connector ledgers, private state root, keyed references,
standing admission policy, POS-visibility latency evidence and retention.

Offline only: synthetic identities (``example.test``), a fake AutoCount bridge
and hook-shaped captures. The connector store identity is the qualified,
non-PII store pin; no Shopify or AutoCount call is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))
sys.path.insert(0, str(ROOT / "tests"))

import xb_directlocal as dl  # noqa: E402
import test_member_directlocal as fx  # noqa: E402

REAL_STORE, REAL_DOMAIN = next(iter(dl.QUALIFIED_CONNECTOR_STORES.items()))
BOOK = dl.ENVIRONMENT_BOOKS["development"]
OLD_CUSTOMER = "gid://shopify/Customer/9000000011"
T0 = fx.T0


def make_private_root() -> tuple[Path, Path]:
    """(holder, private root) prepared exactly as gate E0 would."""

    holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-real-"))
    root = holder / "state"
    dl.prepare_state_root(root, environ={})
    return holder, root


def connector_record(tool_use_id: str, tool_input: dict, data: dict | None, captured_at, *, response=None) -> dict:
    value = fx.record(tool_use_id, tool_input, {}, captured_at)
    value["tool_response"] = json.dumps({"data": data}, separators=(",", ":")) if response is None else response
    return value


def connector_page(nodes: list[dict], has_next: bool, cursor: str | None) -> dict:
    return fx.page_data(nodes, has_next, cursor, store=REAL_STORE, domain=REAL_DOMAIN)


def connector_profile(tool_use_id: str, customer: dict | None, gid: str, captured_at) -> dict:
    data = {"shop": {"id": REAL_STORE, "myshopifyDomain": REAL_DOMAIN}, "customer": customer}
    return connector_record(tool_use_id, {"query": dl.PROFILE_QUERY, "variables": {"id": gid}}, data, captured_at)


class RealBookFake(fx.FakeBridge):
    """Fake MemberCommand labelled as the real bridge bound to the test book."""

    autocount_target = dl.REAL_BRIDGE_TARGET
    autocount_book = BOOK


class _ExitedChild:
    def __init__(self, returncode: int, stdout: bytes):
        self.returncode = returncode
        self._stdout = stdout

    def communicate(self, _input=None, timeout=None):
        return self._stdout, b"vendor stderr: Login failed for user 'sa' on SERVER\\SQL"


class BridgeFailureCodeTests(unittest.TestCase):
    """A failing child's one-line envelope is surfaced only for fixed codes."""

    def read(self, returncode: int, stdout: bytes):
        client = dl.BridgeClient(["bridge"], autocount_book=BOOK, popen=lambda *a, **k: _ExitedChild(returncode, stdout))
        return client._read("book-check", {})

    def code(self, returncode: int, stdout: bytes) -> str:
        with self.assertRaises(dl.BridgeError) as caught:
            self.read(returncode, stdout)
        return caught.exception.code

    def test_allowlisted_child_codes_are_surfaced(self):
        for child in ("autocount_login_rejected", "autocount_sql_connection_failed", "autocount_session_initialization_failed", "book_identity_wrong", "bridge_failed"):
            with self.subTest(child=child):
                self.assertEqual(self.code(1, json.dumps({"error": child}).encode() + b"\r\n"), f"bridge_read_failed:{child}")
        self.assertEqual(self.code(3, b'noise\n{"error":"fence_already_claimed"}\n'), "bridge_read_failed:fence_already_claimed")

    def test_deeply_nested_json_fails_closed_without_recursion_error(self):
        # G2 O3 (R3): json.loads raises RecursionError, not JSONDecodeError.
        deep = b"[" * 200_000 + b"]" * 200_000
        self.assertEqual(self.code(1, deep), "bridge_read_failed:output_not_json:x00000001:l1")
        self.assertEqual(self.code(0, deep), "bridge_output_invalid")
        self.assertEqual(dl.classify_write(dl.WriteRun(started=True, exit_code=0, stdout=deep.decode()), claim_exists=True), ("WRITE_OUTCOME_UNCERTAIN", "writer_outcome_invalid"))
        for parse in (lambda: dl.parse_capture(deep, mode="synthetic", store_gid=fx.STORE, shop_domain=fx.DOMAIN), lambda: dl.inspect_identity_capture(deep), lambda: dl.connector_response_data(deep.decode())):
            with self.assertRaises(dl.CaptureRejected):
                parse()

    def test_unknown_malformed_or_vendor_output_is_generic(self):
        # Only computed values qualify a generic failure: shape, exit, lines.
        cases = (
            (1, b"", "output_empty:x00000001:l0"),
            (1, b"\r\n\n", "output_empty:x00000001:l0"),
            (1, b"not json", "output_not_json:x00000001:l1"),
            (1, b"\xff\xfe", "output_not_utf8:x00000001:l1"),
            (1, b"[]", "output_not_json:x00000001:l1"),
            (1, b'"autocount_login_rejected"', "output_not_json:x00000001:l1"),
            (1, b'{"error":"some_new_code"}', "code_unlisted:x00000001:l1"),
            (1, b'{"error":"Login failed for user XB"}', "code_unlisted:x00000001:l1"),
            (1, b'{"error":"AUTOCOUNT_LOGIN_REJECTED"}', "code_unlisted:x00000001:l1"),
            (1, b'{"error":7}', "envelope_invalid:x00000001:l1"),
            (1, b'{"error":"autocount_login_rejected","detail":"Server=HQ;User Id=XB"}', "envelope_invalid:x00000001:l1"),
            (1, b'{"error":"autocount_login_rejected"}\nLogin failed for user XB', "output_not_json:x00000001:l2"),
            (-532462766, b'{"book":"AED_TESTINGAPI","identity_verified":true,"outbound_quiet":true}\r\n', "result_on_failed_exit:xe0434352:l1"),
            (-1073741819, b"", "output_empty:xc0000005:l0"),
            (1, b"a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk", "output_not_json:x00000001:l9"),
        )
        for returncode, stdout, suffix in cases:
            with self.subTest(stdout=stdout):
                code = self.code(returncode, stdout)
                self.assertEqual(code, f"bridge_read_failed:{suffix}")
                self.assertRegex(code, dl.CODE_RE)
                self.assertNotIn("XB", code)
                self.assertNotIn("HQ", code)
        self.assertEqual(dl.bridge_failure_code(b""), "bridge_read_failed:output_empty:xnone:l0")

    def test_vendor_noise_before_the_envelope_cannot_hide_it(self):
        noisy = b"\x93vendor \xe9 banner\r\nLogin failed for user XB\r\n" + b'{"error":"autocount_login_rejected"}\r\n'
        self.assertEqual(self.code(1, noisy), "bridge_read_failed:autocount_login_rejected")
        self.assertEqual(self.read(0, b"\x93vendor banner\r\n" + b'{"book":"AED_TESTINGAPI"}\r\n'), {"book": "AED_TESTINGAPI"})
        self.assertEqual(self.code(0, b'{"book":"AED_TESTINGAPI"}\r\n\x93'), "bridge_output_invalid")

    def test_every_surfaced_code_is_a_valid_core_code(self):
        for child in dl.BRIDGE_CHILD_ERROR_CODES:
            self.assertRegex(f"bridge_read_failed:{child}", dl.CODE_RE)
        self.assertRegex(dl.bridge_failure_code(b'{"book":1}', -2147483648), dl.CODE_RE)

    def test_allowlist_covers_exactly_the_child_scripts_fixed_codes(self):
        # Drift guard: a new throw in the bridge, guard or adapter must be
        # reviewed into the allowlist (or stays generic by design).
        import re
        found = set()
        for name in ("ac2_directlocal_member_bridge.ps1", "ac2_directlocal_book_guard.ps1", "ac2_member_gateway_autocount_adapter.ps1"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            found |= set(re.findall(r'(?:throw|return)\s+"([a-z0-9_]{1,64})"', text))
        self.assertEqual(found, set(dl.BRIDGE_CHILD_ERROR_CODES))

    def test_success_path_unchanged(self):
        self.assertEqual(self.read(0, b'{"book":"AED_TESTINGAPI"}\n'), {"book": "AED_TESTINGAPI"})
        self.assertEqual(self.code(0, b"not json"), "bridge_output_invalid")


class SddlTests(unittest.TestCase):
    USER = "S-1-5-21-1-2-3-1001"

    def issue(self, sddl: str, protected: bool = True):
        return dl.sddl_custody_issue(sddl, user_sid=self.USER, require_protected=protected)

    def test_private_and_unsafe_descriptors(self):
        good = f"O:{self.USER}D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{self.USER})"
        self.assertIsNone(self.issue(good))
        self.assertIsNone(self.issue(good.replace("D:PAI", "D:AI"), protected=False))  # children may inherit
        self.assertIsNone(self.issue(good + "(D;OICI;FA;;;WD)"))  # deny entries are harmless
        cases = {
            "state_root_acl_inherited": good.replace("D:PAI", "D:AI"),
            "state_root_acl_unsafe": good + "(A;OICI;0x1200a9;;;AU)",
            "state_root_owner_unsafe": good.replace(f"O:{self.USER}", "O:S-1-5-21-9-9-9-500"),
            "state_root_acl_unverifiable": f"O:{self.USER}D:P",
        }
        for code, sddl in cases.items():
            with self.subTest(code=code):
                self.assertEqual(self.issue(sddl), code)
        for other in ("(A;OICIIO;FA;;;CO)", "(A;OICI;FA;;;S-1-3-4)", "(OA;;FA;;;SY)", "(A;OICI;FA;;;BU)"):
            with self.subTest(ace=other):
                self.assertEqual(self.issue(good + other), "state_root_acl_unsafe")
        self.assertEqual(self.issue("garbage"), "state_root_acl_unverifiable")

    def test_aliases_are_compared_as_canonical_sids(self):
        # Hosted Windows CI runs as the renamed built-in Administrator (RID
        # 500), which SDDL prints as "LA" (2026-10-16 CI failure).
        builtin_admin = "S-1-5-21-1-2-3-500"
        own = f"O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;LA)"

        def resolver(mapping):
            return lambda token: dl._static_sid(token) or mapping.get(token)

        is_user = resolver({"LA": builtin_admin})
        self.assertIsNone(dl.sddl_custody_issue(own, user_sid=builtin_admin, require_protected=True, resolve=is_user))
        self.assertIsNone(dl.sddl_custody_issue(own.replace("O:BA", "O:LA"), user_sid=builtin_admin, require_protected=True, resolve=is_user))
        # The same alias naming someone else, or an alias nobody can resolve, is never private.
        self.assertEqual(dl.sddl_custody_issue(own, user_sid=self.USER, require_protected=True, resolve=is_user), "state_root_acl_unsafe")
        self.assertEqual(dl.sddl_custody_issue(own, user_sid=builtin_admin, require_protected=True), "state_root_acl_unverifiable")
        self.assertEqual(dl.sddl_custody_issue(own.replace("O:BA", "O:LA"), user_sid=builtin_admin, require_protected=True), "state_root_acl_unverifiable")
        for broad in ("(A;OICI;FA;;;OW)", "(A;OICIIO;FA;;;CO)", "(A;OICI;FR;;;AU)", "(A;OICI;FR;;;BU)", "(A;OICI;FR;;;WD)", "(A;OICI;FR;;;S-1-5-21-1-2-3-1002)"):
            with self.subTest(ace=broad):
                self.assertEqual(dl.sddl_custody_issue(own + broad, user_sid=builtin_admin, require_protected=True, resolve=is_user), "state_root_acl_unsafe")

    def test_sanitised_classes_never_carry_sids(self):
        sddl = f"O:{self.USER}D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.USER})(A;OICI;FR;;;S-1-5-21-9-9-9-1005)(A;OICI;FR;;;OW)"
        classes = dl.sddl_custody_classes(sddl, user_sid=self.USER)
        self.assertEqual([a["principal"] for a in classes["aces"]], ["system(alias:SY)", "current_user", "other_account", "owner_rights(alias:OW)"])
        self.assertNotIn("S-1-5-21", json.dumps(classes))


@unittest.skipUnless(os.name == "nt", "Windows SID resolution")
class WindowsAclDiagnosticTests(unittest.TestCase):
    def test_windows_resolves_aliases(self):
        self.assertEqual((dl._win_resolve_sid("SY"), dl._win_resolve_sid("BU"), dl._win_resolve_sid("OW")), ("S-1-5-18", "S-1-5-32-545", "S-1-3-4"))
        self.assertRegex(dl._win_resolve_sid("LA") or "", r"^S-1-5-21-[0-9-]+-500$")
        self.assertIsNone(dl._win_resolve_sid("ZZ"))

    def test_exact_dacl_replaces_leftover_explicit_aces(self):
        # Hosted CI 2026-10-16: an explicit OWNER RIGHTS ACE survived icacls on
        # a new directory. The exact-DACL write must leave nothing but the
        # three allowed principals, and the check must still refuse otherwise.
        holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-dacl-"))
        try:
            target = holder / "state"
            target.mkdir()
            for grant in ("*S-1-3-4:(OI)(CI)F", "*S-1-5-32-545:(OI)(CI)R"):
                subprocess.run(["icacls", str(target), "/grant", grant], capture_output=True, check=True)
            with self.assertRaises(dl.DirectLocalError):
                dl.verify_private_state(target)
            dl._win_set_private_dacl(target)
            dl.verify_private_state(target)
            classes = dl.sddl_custody_classes(dl._win_sddl(target), user_sid=dl._win_current_user_sid(), resolve=dl._win_resolve_sid)
            self.assertEqual(sorted(a["principal"].split("(")[0] for a in classes["aces"]), ["administrators", "current_user", "system"])
            self.assertIn("P", classes["dacl_flags"])
        finally:
            shutil.rmtree(holder, ignore_errors=True)

    def test_prepare_under_python312_style_temp_parent(self):
        # Parent DACL as CPython >= 3.12.4 mkdtemp writes it: protected,
        # SYSTEM / Administrators / OWNER RIGHTS, inheritable.
        holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-312-"))
        try:
            subprocess.run(["icacls", str(holder), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "*S-1-3-4:(OI)(CI)F"], capture_output=True, check=True)
            root = holder / "state"
            self.assertEqual(dl.prepare_state_root(root, environ={}), {"status": "prepared", "private": True})
            self.assertEqual({row["verdict"] for row in dl.describe_private_state(root)}, {"private"})
        finally:
            subprocess.run(["icacls", str(holder), "/grant", f"*{dl._win_current_user_sid()}:(OI)(CI)F", "/T"], capture_output=True)
            shutil.rmtree(holder, ignore_errors=True)

    def test_prepared_root_is_private_with_sanitised_evidence(self):
        holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-acl-"))
        try:
            root = holder / "state"
            dl.prepare_state_root(root, environ={})
            rows = dl.describe_private_state(root)
            evidence = json.dumps(rows, separators=(",", ":"))
            if os.environ.get("GITHUB_ACTIONS") == "true":
                # Public check-run annotation: classes only, no SID, name or path.
                print(f"::notice title=xb155-acl-diagnostic::{evidence}")
            self.assertNotIn("S-1-5-21", evidence)
            self.assertEqual({row["role"]: row["verdict"] for row in rows}, {"root": "private", "inbox": "private", "claims": "private", "evidence": "private"}, evidence)
            self.assertIn("P", rows[0]["dacl_flags"])
            for grant in ("*S-1-1-0:(R)", "*S-1-3-4:(F)", "*S-1-5-11:(R)"):
                with self.subTest(grant=grant):
                    subprocess.run(["icacls", str(root), "/grant", grant], capture_output=True, check=True)
                    with self.assertRaises(dl.DirectLocalError) as raised:
                        dl.verify_private_state(root)
                    self.assertEqual(raised.exception.code, "state_root_acl_unsafe")
                    subprocess.run(["icacls", str(root), "/remove:g", grant.split(":")[0]], capture_output=True, check=True)
            dl.verify_private_state(root)  # restored to private
        finally:
            shutil.rmtree(holder, ignore_errors=True)


class PrepareStateRootTests(unittest.TestCase):
    def setUp(self):
        self.holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-root-"))

    def tearDown(self):
        shutil.rmtree(self.holder, ignore_errors=True)

    def test_prepare_creates_private_root_and_refuses_existing(self):
        root = self.holder / "state"
        self.assertEqual(dl.prepare_state_root(root, environ={}), {"status": "prepared", "private": True})
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["claims", "evidence", "inbox"])
        dl.verify_private_state(root)
        for path, code in ((root, "state_root_already_exists"), (self.holder / "absent" / "state", "state_root_parent_missing")):
            with self.subTest(code=code), self.assertRaises(dl.DirectLocalError) as raised:
                dl.prepare_state_root(path, environ={})
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(dl.DirectLocalError) as raised:
            dl.prepare_state_root(ROOT / "directlocal" / "state", environ={})
        self.assertEqual(raised.exception.code, "state_root_location_refused")

    def test_ordinary_directory_is_not_private(self):
        plain = self.holder / "plain"
        plain.mkdir()
        if os.name != "nt":
            os.chmod(plain, 0o755)
        with self.assertRaises(dl.DirectLocalError) as raised:
            dl.verify_private_state(plain)
        self.assertIn(raised.exception.code, ("state_root_acl_inherited", "state_root_acl_unsafe"))

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_broadened_acl_is_detected(self):
        root = self.holder / "state"
        dl.prepare_state_root(root, environ={})
        subprocess.run(["icacls", str(root / "inbox"), "/grant", "*S-1-5-32-545:(R)"], capture_output=True, check=True)
        with self.assertRaises(dl.DirectLocalError) as raised:
            dl.verify_private_state(root)
        self.assertEqual(raised.exception.code, "state_root_acl_unsafe")


class RegistrationEventTests(fx.DirectLocalTestCase):
    """The registration instant is membership.start_date's createdAt."""

    def seal(self, nodes: list[dict]) -> dict:
        self.processor.begin_run("BASELINE")
        self.put(fx.record("toolu_reg_page", fx.page_input(None), fx.page_data(nodes, False, None), T0 + timedelta(seconds=10)))
        self.clock.now = T0 + timedelta(minutes=10)
        return self.processor.ingest_run()

    def discover(self, nodes: list[dict], *, at) -> dict:
        self.clock.now = at
        self.processor.begin_run("DISCOVERY")
        self.put(fx.record(f"toolu_disc_{at.timestamp():.0f}", fx.page_input(None), fx.page_data(nodes, False, None), at + timedelta(seconds=1)))
        self.clock.now = at + timedelta(seconds=30)
        return self.processor.ingest_run()

    def read(self, gid: str, *, created, registered=None, **kwargs) -> dict:
        self.counter += 1
        customer = fx.profile(gid, created=created, registered=registered, **kwargs)
        return self.processor.process(self.put(fx.profile_record(f"toolu_reg_profile_{self.counter}", customer, gid, self.clock.now - timedelta(seconds=2))))

    def test_existing_customer_registering_after_cutover_is_admitted(self):
        old = T0 - timedelta(days=400)
        sealed = self.seal([fx.node(fx.HISTORICAL, T0 - timedelta(days=30), ["member-mg"]), fx.node(OLD_CUSTOMER, old, ["vip"], registered=None)])
        self.assertEqual((sealed["historical"], sealed["transition"]), (1, 0))
        registered = T0 + timedelta(hours=1)
        found = self.discover([fx.node(OLD_CUSTOMER, old, ["member-mg", "shopify-forms-1178308"], registered=registered)], at=T0 + timedelta(hours=1, minutes=1))
        self.assertEqual(found["discovered"], 1)
        result = self.read(OLD_CUSTOMER, created=old, registered=registered)
        self.assertEqual((result["customer_state"], result["code"]), ("ADMITTED", None))
        row = self.customer(OLD_CUSTOMER)
        self.assertEqual((row["registration_id"], row["registration_at"]), (fx.metafield_gid(OLD_CUSTOMER), "2026-10-14T01:00:00Z"))

    def discover_old_in_flight(self, tags: list[str]) -> datetime:
        """Old customer outside the baseline, tagged but without start_date yet."""

        old = T0 - timedelta(days=400)
        self.seal([fx.node(fx.HISTORICAL, T0 - timedelta(days=30), ["member-mg"]), fx.node(OLD_CUSTOMER, old, ["vip"], registered=None)])
        found = self.discover([fx.node(OLD_CUSTOMER, old, tags, registered=None)], at=T0 + timedelta(hours=1, minutes=1))
        self.assertEqual(found["discovered"], 1)
        return old

    def test_existing_customer_read_before_start_date_waits_then_is_admitted(self):
        # G2 A1 (R1): Forms tags member-mg ~3 s before writing start_date.
        old = self.discover_old_in_flight(["member-mg", "shopify-forms-1178308"])
        result = self.read(OLD_CUSTOMER, created=old, start=None)
        self.assertEqual((result["customer_state"], result["code"]), ("WAITING", "registration_event_pending"))
        self.assertEqual((self.customer(OLD_CUSTOMER)["registration_id"], self.customer(OLD_CUSTOMER)["check_count"]), (None, 1))
        registered = T0 + timedelta(hours=1, seconds=3)
        result = self.read(OLD_CUSTOMER, created=old, registered=registered)
        self.assertEqual((result["customer_state"], result["code"]), ("ADMITTED", None))
        self.assertEqual(self.customer(OLD_CUSTOMER)["registration_at"], "2026-10-14T01:00:03Z")

    def test_staff_tagged_old_customer_without_registration_is_bounded_to_review(self):
        old = self.discover_old_in_flight(["member-mg"])
        for check in range(1, dl.PROFILE_WAIT_MAX_CHECKS):
            result = self.read(OLD_CUSTOMER, created=old, start=None)
            self.assertEqual((result["customer_state"], result["code"]), ("WAITING", "registration_event_pending"), check)
        result = self.read(OLD_CUSTOMER, created=old, start=None)
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "preexisting_customer_not_new_signup"))
        self.assertEqual(self.customer(OLD_CUSTOMER)["check_count"], dl.PROFILE_WAIT_MAX_CHECKS)
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.read(OLD_CUSTOMER, created=old, registered=T0 + timedelta(hours=2))
        self.assertEqual(raised.exception.code, "customer_state_terminal")

    def test_old_customer_negatives_are_unchanged(self):
        # A pre-cutover registration, or a legacy tag, is still decided at once.
        old = self.discover_old_in_flight(["member-mg"])
        result = self.read(OLD_CUSTOMER, created=old, registered=T0 - timedelta(days=2))
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "preexisting_customer_not_new_signup"))
        legacy = "gid://shopify/Customer/9000000012"
        self.discover([fx.node(legacy, old, ["member-mg", "member-legacy"], registered=None)], at=T0 + timedelta(hours=3))
        result = self.read(legacy, created=old, start=None, tags=("member-mg", "member-legacy"))
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "legacy_tag_present"))

    def test_baseline_member_re_registering_stays_excluded(self):
        self.seal([fx.node(fx.HISTORICAL, T0 - timedelta(days=30), ["member-mg"])])
        found = self.discover([fx.node(fx.HISTORICAL, T0 - timedelta(days=30), ["member-mg"], registered=T0 - timedelta(days=30))], at=T0 + timedelta(hours=1))
        self.assertEqual(found["discovered"], 0)
        self.assertIsNone(self.customer(fx.HISTORICAL))

    def test_customer_without_registration_metafield_uses_customer_created_at(self):
        sealed = self.seal([fx.node(fx.HISTORICAL, T0 - timedelta(days=3), ["member-mg"], registered=None), fx.node(fx.NEW, T0 + timedelta(minutes=1), ["member-mg"], registered=None)])
        self.assertEqual((sealed["historical"], sealed["transition"]), (1, 1))
        # Registration still in flight: no start_date yet -> bounded wait, then admitted.
        self.assertEqual(self.read(fx.NEW, created=T0 + timedelta(minutes=1), start=None)["customer_state"], "WAITING")
        self.assertEqual(self.read(fx.NEW, created=T0 + timedelta(minutes=1), registered=T0 + timedelta(minutes=1, seconds=3))["customer_state"], "ADMITTED")

    def test_changed_registration_event_goes_to_review(self):
        self.seal([fx.node(fx.NEW, T0 + timedelta(minutes=1), ["member-mg"])])
        self.assertEqual(self.read(fx.NEW, created=T0 + timedelta(minutes=1), expiry=None)["customer_state"], "WAITING")
        result = self.read(fx.NEW, created=T0 + timedelta(minutes=1), registration_id="gid://shopify/Metafield/1234")
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "registration_event_changed"))
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.conn.execute("UPDATE customers SET registration_at = 'x' WHERE customer_gid = ?", (fx.NEW,))

    def test_registration_before_cutover_and_malformed_registration(self):
        self.seal([fx.node(fx.NEW, T0 + timedelta(minutes=1), ["member-mg"])])
        result = self.read(fx.NEW, created=T0 + timedelta(minutes=1), registered=T0 - timedelta(days=1))
        self.assertEqual((result["customer_state"], result["code"]), ("MANUAL_REVIEW", "preexisting_customer_not_new_signup"))
        bad = fx.node(fx.NEW_TWO, T0, ["member-mg"])
        bad["registration"]["id"] = "gid://shopify/Customer/1"
        self.clock.now = T0 + timedelta(hours=1)
        self.processor.begin_run("DISCOVERY")
        self.put(fx.record("toolu_bad_reg", fx.page_input(None), fx.page_data([bad], False, None), T0 + timedelta(hours=1, seconds=1)))
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.ingest_run()
        self.assertEqual(raised.exception.code, "run_not_sealed:baseline_created_at_invalid")


class RecentDiscoveryTests(fx.DirectLocalTestCase):
    """Incremental discovery by ``updated_at`` with a pinned search string."""

    def recent_input(self, search: str, after: str | None = None) -> dict:
        value = {"query": dl.RECENT_QUERY, "variables": {"first": dl.PAGE_SIZE, "after": after, "search": search}, "first": dl.PAGE_SIZE}
        if after is not None:
            value["after"] = after
        return value

    def test_recent_run_finds_existing_customer_registration_and_advances_watermark(self):
        self.seal_baseline()
        self.clock.now = fx.T0 + timedelta(hours=2)
        run = self.processor.begin_run("DISCOVERY", recent=True)
        self.assertEqual(run["search"], "updated_at:>='2026-10-14T00:00:00Z'")  # first watermark is the cutover
        request = self.processor.next_request()
        self.assertEqual((request["tool_input"]["query"], request["tool_input"]["variables"]["search"]), (dl.RECENT_QUERY, run["search"]))
        old = fx.T0 - timedelta(days=500)
        page = fx.page_data([fx.node(OLD_CUSTOMER, old, ["member-mg", "shopify-forms-1178308"], registered=fx.T0 + timedelta(hours=1))], False, None)
        # A baseline-form page and a page with another search never join this run.
        self.put(fx.record("toolu_wrong_form", fx.page_input(None), page, fx.T0 + timedelta(hours=2, seconds=1)))
        self.put(fx.record("toolu_recent", request["tool_input"], page, fx.T0 + timedelta(hours=2, seconds=2)))
        self.clock.now = fx.T0 + timedelta(hours=2, minutes=1)
        sealed = self.processor.ingest_run()
        self.assertEqual((sealed["pages"], sealed["discovered"]), (1, 1))
        self.assertEqual(self.customer(OLD_CUSTOMER)["state"], "DISCOVERED")
        self.clock.now = fx.T0 + timedelta(hours=5)
        self.processor.purge_inbox()
        later = self.processor.begin_run("DISCOVERY", recent=True)
        self.assertEqual(later["search"], "updated_at:>='2026-10-14T01:45:00Z'")  # previous start minus the 15-minute margin

    def test_recent_search_is_pinned_by_the_guard(self):
        good = self.recent_input("updated_at:>='2026-10-14T00:00:00Z'")
        self.assertEqual(dl.check_tool_input(good), "recent")
        for search in ("updated_at:>='2026-10-14'", "tag:member-mg", "updated_at:>='2026-10-14T00:00:00Z' OR email:*", ""):
            with self.subTest(search=search), self.assertRaises(dl.CaptureRejected) as raised:
                dl.check_tool_input(self.recent_input(search))
            self.assertEqual(raised.exception.code, "recent_search_invalid")
        with self.assertRaises(dl.CaptureRejected):
            dl.check_tool_input({**good, "variables": {**good["variables"], "extra": 1}})
        with self.assertRaises(dl.DirectLocalError):
            self.processor.begin_run("BASELINE", recent=True)


class ConnectorLedgerCase(unittest.TestCase):
    def setUp(self):
        self.holder, self.root = make_private_root()
        self.clock = fx.Clock(T0)
        self.ledger = dl.Ledger(self.root, clock=self.clock)
        self.processor = dl.Processor(self.ledger, writer_probe=lambda pid, created: "exited")
        self.processor.init_ledger(mode="connector", environment="development", store_gid=REAL_STORE, shop_domain=REAL_DOMAIN)
        self.ac = fx.FakeAutoCount()
        self.counter = 0

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.holder, ignore_errors=True)

    def put(self, value: dict) -> Path:
        path = self.root / "inbox" / f"{value['tool_use_id']}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def seal(self, *gids: str) -> dict:
        self.processor.begin_run("BASELINE")
        nodes = [fx.node(fx.HISTORICAL, T0 - timedelta(days=30), ["member-mg"])] + [fx.node(g, T0 + timedelta(minutes=5), ["member-mg"]) for g in gids]
        self.put(connector_record("toolu_c_page", fx.page_input(None), connector_page(nodes, False, None), T0 + timedelta(seconds=10)))
        self.clock.now = T0 + timedelta(minutes=10)
        return self.processor.ingest_run()

    def fresh(self, gid: str = fx.NEW, **kwargs) -> Path:
        self.counter += 1
        customer = fx.profile(gid, created=T0 + timedelta(minutes=5), **kwargs)
        return self.put(connector_profile(f"toolu_c_profile_{self.counter}", customer, gid, self.clock.now - timedelta(seconds=5)))

    def customer(self, gid: str = fx.NEW) -> sqlite3.Row:
        return self.ledger.conn.execute("SELECT * FROM customers WHERE customer_gid = ?", (gid,)).fetchone()


class ConnectorLedgerTests(ConnectorLedgerCase):
    def test_init_binding_rules(self):
        meta = self.ledger.meta()
        self.assertEqual((meta["mode"], meta["environment"], meta["autocount_book"]), ("connector", "development", "AED_TESTINGAPI"))
        for statement in ("UPDATE ledger_meta SET environment = 'production'", "UPDATE ledger_meta SET autocount_book = 'AED_XBOUNDARIES'"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                self.ledger.conn.execute(statement)
        holder, root = make_private_root()
        try:
            ledger = dl.Ledger(root, clock=self.clock)
            try:
                processor = dl.Processor(ledger)
                cases = [
                    ({"environment": "production", "store_gid": REAL_STORE, "shop_domain": REAL_DOMAIN}, "production_environment_not_authorised"),
                    ({"environment": "synthetic", "store_gid": REAL_STORE, "shop_domain": REAL_DOMAIN}, "environment_mode_mismatch"),
                    ({"environment": None, "store_gid": REAL_STORE, "shop_domain": REAL_DOMAIN}, "environment_mode_mismatch"),
                    ({"environment": "development", "store_gid": "gid://shopify/Shop/1", "shop_domain": REAL_DOMAIN}, "connector_store_identity_not_qualified"),
                    ({"environment": "development", "store_gid": REAL_STORE, "shop_domain": "other.myshopify.com"}, "connector_store_identity_not_qualified"),
                ]
                for kwargs, code in cases:
                    with self.subTest(code=code, env=kwargs["environment"]), self.assertRaises(dl.DirectLocalError) as raised:
                        processor.init_ledger(mode="connector", **kwargs)
                    self.assertEqual(raised.exception.code, code)
                with self.assertRaises(dl.DirectLocalError) as raised:
                    processor.init_ledger(mode="synthetic", environment="development", store_gid=fx.STORE, shop_domain=fx.DOMAIN)
                self.assertEqual(raised.exception.code, "environment_mode_mismatch")
            finally:
                ledger.close()
        finally:
            shutil.rmtree(holder, ignore_errors=True)

    def test_connector_ledger_refuses_a_non_private_root(self):
        holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-plain-"))
        try:
            plain = holder / "plain"
            plain.mkdir()
            if os.name != "nt":
                os.chmod(plain, 0o755)
            ledger = dl.Ledger(plain, clock=self.clock)
            try:
                with self.assertRaises(dl.DirectLocalError) as raised:
                    dl.Processor(ledger).init_ledger(mode="connector", environment="development", store_gid=REAL_STORE, shop_domain=REAL_DOMAIN)
                self.assertIn(raised.exception.code, ("state_root_acl_inherited", "state_root_acl_unsafe"))
            finally:
                ledger.close()
        finally:
            shutil.rmtree(holder, ignore_errors=True)

    def test_connector_responses_disconnected_expired_and_partial_fail_closed(self):
        self.processor.begin_run("BASELINE")
        page = connector_page([fx.node(fx.NEW, T0 + timedelta(minutes=5), ["member-mg"])], False, None)
        bad = {
            "object_not_text": {"data": page},
            "disconnected": "Error: the MCP server disconnected before responding",
            "auth_expired": json.dumps({"errors": [{"message": "Unauthorized"}]}),
            "partial_result": json.dumps({"data": page, "errors": [{"message": "Throttled", "path": ["customers"]}]}),
            "extensions": json.dumps({"data": page, "extensions": {"cost": {}}}),
            "empty": "",
        }
        for name, response in bad.items():
            with self.subTest(response=name), self.assertRaises(dl.CaptureRejected):
                dl.parse_capture(json.dumps(connector_record("toolu_x", fx.page_input(None), None, T0, response=response)).encode(), mode="connector", store_gid=REAL_STORE, shop_domain=REAL_DOMAIN)
        self.put(connector_record("toolu_expired", fx.page_input(None), None, T0 + timedelta(seconds=5), response=bad["auth_expired"]))
        with self.assertRaises(dl.CaptureRejected):
            self.processor.ingest_run()  # a rejected capture blocks sealing
        with self.assertRaises(dl.CaptureRejected):
            self.processor.next_request()
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM runs WHERE state = 'SEALED'").fetchone()[0], 0)
        purged = self.processor.purge_inbox()
        self.assertEqual((purged["rejected"], purged["kept"]), (1, 0))
        self.put(connector_record("toolu_ok", fx.page_input(None), page, T0 + timedelta(seconds=6)))
        self.clock.now = T0 + timedelta(minutes=1)
        self.assertEqual(self.processor.ingest_run()["discovered"], 1)

    def test_bridge_binding_and_custody(self):
        self.seal(fx.NEW)
        self.assertEqual(self.processor.process(self.fresh())["customer_state"], "ADMITTED")
        self.processor.approve_write(fx.NEW, confirm_suffix="000002")
        synthetic = fx.FakeBridge(self.ac)
        wrong_book = RealBookFake(self.ac)
        wrong_book.autocount_book = "AED_XBOUNDARIES"
        for bridge in (synthetic, wrong_book, None):
            with self.subTest(bridge=getattr(bridge, "autocount_book", None)), self.assertRaises(dl.DirectLocalError) as raised:
                self.processor.process(self.fresh(), bridge=bridge, enable_write=True)
            self.assertEqual(raised.exception.code, "bridge_book_binding_mismatch")
        if os.name == "nt":
            subprocess.run(["icacls", str(self.root / "claims"), "/grant", "*S-1-5-32-545:(R)"], capture_output=True, check=True)
            with self.assertRaises(dl.DirectLocalError) as raised:
                self.processor.process(self.fresh(), bridge=RealBookFake(self.ac), enable_write=True)
            self.assertEqual(raised.exception.code, "state_root_acl_unsafe")
            subprocess.run(["icacls", str(self.root / "claims"), "/remove:g", "*S-1-5-32-545"], capture_output=True, check=True)
        self.assertEqual(self.ac.saves, 0)
        result = self.processor.process(self.fresh(), bridge=RealBookFake(self.ac), enable_write=True)
        self.assertEqual(result["fence_state"], "CREATED_VERIFIED")
        self.assertEqual(self.ac.saves, 1)

    def test_member_refs_are_keyed_not_enumerable(self):
        self.seal(fx.NEW)
        self.processor.process(self.fresh())
        self.processor.approve_write(fx.NEW, confirm_suffix="000002")
        result = self.processor.process(self.fresh(), bridge=RealBookFake(self.ac), enable_write=True)
        member_no = self.ledger.conn.execute("SELECT member_no FROM fences").fetchone()[0]
        self.assertRegex(result["member_ref"], r"^m-[0-9a-f]{20}$")
        self.assertNotEqual(result["member_ref"][2:], hashlib.sha256(member_no.encode()).hexdigest()[:20])
        holder, root = make_private_root()
        try:
            other = dl.Ledger(root, clock=self.clock)
            try:
                processor = dl.Processor(other)
                processor.init_ledger(mode="connector", environment="development", store_gid=REAL_STORE, shop_domain=REAL_DOMAIN)
                self.assertNotEqual(processor._ref("m", member_no), result["member_ref"])
            finally:
                other.close()
        finally:
            shutil.rmtree(holder, ignore_errors=True)
        report = json.dumps(self.processor.uat_report())
        for value in (member_no, fx.NEW, "6580000001", "alpha@example.test", "Synthetic Member Alpha"):
            self.assertNotIn(value, report)

    def test_next_request_walks_pages_then_profiles(self):
        self.processor.begin_run("BASELINE")
        first = self.processor.next_request()
        self.assertEqual((first["purpose"], first["tool_name"], first["tool_input"]["variables"]["after"]), ("baseline_page", dl.CONNECTOR_TOOL, None))
        self.assertEqual(dl.check_tool_input(first["tool_input"]), "baseline")
        self.put(connector_record("toolu_p1", first["tool_input"], connector_page([fx.node(fx.NEW, T0 + timedelta(minutes=5), ["member-mg"])], True, "cursor-1"), T0 + timedelta(seconds=1)))
        second = self.processor.next_request()
        self.assertEqual((second["tool_input"]["variables"]["after"], second["tool_input"]["after"]), ("cursor-1", "cursor-1"))
        self.assertEqual(dl.check_tool_input(second["tool_input"]), "baseline")
        self.put(connector_record("toolu_p2", second["tool_input"], connector_page([], False, None), T0 + timedelta(seconds=2)))
        self.assertEqual(self.processor.next_request()["status"], "run_pages_complete")
        self.clock.now = T0 + timedelta(minutes=10)
        self.processor.ingest_run()
        request = self.processor.next_request()
        self.assertEqual((request["purpose"], request["tool_input"]["variables"]), ("profile", {"id": fx.NEW}))
        self.assertEqual(dl.check_tool_input(request["tool_input"]), "profile")

    def test_retention_purge(self):
        self.seal(fx.NEW)
        consumed = self.fresh()
        raw = consumed.read_bytes()
        self.processor.process(consumed)
        consumed.write_bytes(raw)  # a replayed copy left behind
        stale = self.fresh()
        self.clock.now += timedelta(seconds=dl.CAPTURE_MAX_AGE_SECONDS + 10)
        keep = self.fresh()
        orphan = self.put(connector_record("toolu_orphan", fx.page_input(None), connector_page([], False, None), T0 + timedelta(minutes=20)))
        counts = self.processor.purge_inbox()
        # consumed: the replayed profile copy and the sealed baseline page.
        self.assertEqual((counts["consumed"], counts["expired_profile"], counts["orphan_page"], counts["kept"]), (2, 1, 1, 1))
        self.assertFalse((self.root / "inbox" / "toolu_c_page.json").exists())
        self.assertEqual([p.exists() for p in (consumed, stale, keep, orphan)], [False, False, True, False])


class AdmissionPolicyTests(ConnectorLedgerCase):
    def activate(self, **kwargs) -> dict:
        values = {"confirm_book": BOOK, "max_creates": 2, "ttl_hours": 24, **kwargs}
        return self.processor.activate_policy(**values)

    def test_activation_bounds(self):
        for kwargs, code in (
            ({"confirm_book": "AED_XBOUNDARIES"}, "policy_book_confirmation_mismatch"),
            ({"max_creates": 0}, "policy_bounds_invalid"),
            ({"ttl_hours": dl.POLICY_MAX_TTL_HOURS + 1}, "policy_bounds_invalid"),
        ):
            with self.subTest(code=code), self.assertRaises(dl.DirectLocalError) as raised:
                self.activate(**kwargs)
            self.assertEqual(raised.exception.code, code)
        self.activate()
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.activate()
        self.assertEqual(raised.exception.code, "policy_already_active")
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.conn.execute("UPDATE admission_policies SET max_creates = 500")

    def test_synthetic_ledger_cannot_activate(self):
        case = fx.DirectLocalTestCase("run")
        case.setUp()
        try:
            with self.assertRaises(dl.DirectLocalError) as raised:
                case.processor.activate_policy(confirm_book=BOOK, max_creates=1, ttl_hours=1)
            self.assertEqual(raised.exception.code, "synthetic_environment_not_authorised")
        finally:
            case.tearDown()

    def test_policy_admits_each_registration_once_without_per_member_approval(self):
        self.seal(fx.NEW, fx.NEW_TWO, "gid://shopify/Customer/9000000005")
        self.activate(max_creates=2)
        bridge = RealBookFake(self.ac)
        first = self.processor.process(self.fresh(), bridge=bridge, enable_write=True)  # DISCOVERED -> created in one call
        self.assertEqual((first["customer_state"], first["fence_state"]), ("ADMITTED", "CREATED_VERIFIED"))
        with self.assertRaises(dl.DirectLocalError) as raised:
            self.processor.process(self.fresh(), bridge=bridge, enable_write=True)  # replayed registration
        self.assertEqual(raised.exception.code, "customer_state_terminal")
        self.ac.browse = [("8000 0002", None)]  # second registration is a legacy duplicate
        dup = self.processor.process(self.fresh(fx.NEW_TWO, phone="+65 8000 0002", email="beta@example.test"), bridge=bridge, enable_write=True)
        self.assertEqual((dup["customer_state"], dup["code"]), ("MANUAL_REVIEW", "legacy_candidate_found"))
        capped = self.processor.process(self.fresh("gid://shopify/Customer/9000000005", phone="+65 8000 0005", email="gamma@example.test"), bridge=bridge, enable_write=True)
        self.assertEqual((capped["customer_state"], capped["code"]), ("ADMITTED", "write_approval_missing"))  # cap of 2 approvals minted
        self.assertEqual((self.ac.saves, bridge.write_calls), (1, 1))
        events = [row[0] for row in self.ledger.conn.execute("SELECT type FROM events")]
        self.assertEqual(events.count("write_auto_approved"), 2)

    def test_revoked_or_expired_policy_mints_nothing(self):
        self.seal(fx.NEW, fx.NEW_TWO)
        self.activate(ttl_hours=1)
        self.processor.revoke_policy()
        result = self.processor.process(self.fresh(), bridge=RealBookFake(self.ac), enable_write=True)
        self.assertEqual(result["code"], "write_approval_missing")
        self.activate(ttl_hours=1)
        self.clock.now += timedelta(hours=2)
        result = self.processor.process(self.fresh(fx.NEW_TWO, phone="+65 8000 0002", email="beta@example.test"), bridge=RealBookFake(self.ac), enable_write=True)
        self.assertEqual(result["code"], "write_approval_missing")
        self.assertEqual(self.ac.saves, 0)

    def test_crash_after_save_under_policy_never_writes_twice(self):
        self.seal(fx.NEW)
        self.activate()
        bridge = RealBookFake(self.ac, write_mode="parent_crash_after_save")
        with self.assertRaises(fx.SimulatedCrash):
            self.processor.process(self.fresh(), bridge=bridge, enable_write=True)
        self.ledger.close()
        self.clock.now += timedelta(hours=1)
        self.ledger = dl.Ledger(self.root, clock=self.clock)
        self.processor = dl.Processor(self.ledger, writer_probe=lambda pid, created: "exited")
        self.assertEqual(self.processor.recover()["uncertain"], 1)
        with self.assertRaises(dl.DirectLocalError):
            self.processor.process(self.fresh(), bridge=RealBookFake(self.ac), enable_write=True)
        result = self.processor.reconcile(self.fresh(), bridge=RealBookFake(self.ac))
        self.assertEqual(result["fence_state"], "RECONCILED_VERIFIED")
        self.assertEqual(self.ac.saves, 1)


class PosLatencyTests(ConnectorLedgerCase):
    def test_pos_visibility_is_recorded_and_reported_as_latency(self):
        self.seal(fx.NEW)
        self.processor.activate_policy(confirm_book=BOOK, max_creates=1, ttl_hours=1)
        result = self.processor.process(self.fresh(), bridge=RealBookFake(self.ac), enable_write=True)
        ref = result["member_ref"]
        for kwargs, code in (
            ({"member_ref": "m-" + "0" * 20, "outlet": "XB01", "observed_at": "2026-10-14T00:12:00Z"}, "member_ref_unknown"),
            ({"member_ref": ref, "outlet": "xb01; drop", "observed_at": "2026-10-14T00:12:00Z"}, "outlet_invalid"),
            ({"member_ref": ref, "outlet": "XB01", "observed_at": "2026-10-13T00:00:00Z"}, "pos_observation_time_invalid"),
        ):
            with self.subTest(code=code), self.assertRaises(dl.DirectLocalError) as raised:
                self.processor.record_pos_visible(**kwargs)
            self.assertEqual(raised.exception.code, code)
        self.clock.now = T0 + timedelta(minutes=13)
        self.processor.record_pos_visible(member_ref=ref, outlet="XB01", observed_at="2026-10-14T00:12:30Z")
        latency = self.processor.uat_report()["fence_detail"][0]["latency_seconds"]
        # registration 00:05:00 (start_date metafield createdAt); discovered and fenced at 00:10:00.
        self.assertEqual((latency["registration_to_discovered"], latency["registration_to_fenced"], latency["registration_to_pos_visible"]), (300, 300, {"XB01": 450}))
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.conn.execute("DELETE FROM pos_observations")


class RealCoreCliTests(unittest.TestCase):
    def run_cli(self, root: Path, *args: str) -> tuple[int, dict]:
        env = {**os.environ, "LOCALAPPDATA": str(root.parent / "not-the-state-root")}
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, str(ROOT / "directlocal" / "xb_directlocal.py"), "--state-root", str(root), *args], capture_output=True, timeout=120, env=env, check=False)
        return proc.returncode, json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])

    def test_prepare_init_status_policy(self):
        holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-cli-"))
        try:
            root = holder / "state"
            self.assertEqual(self.run_cli(root, "prepare-state-root"), (0, {"private": True, "status": "prepared"}))
            self.assertEqual(self.run_cli(root, "prepare-state-root")[1]["code"], "state_root_already_exists")
            code, init = self.run_cli(root, "init-ledger", "--mode", "connector", "--environment", "development", "--store-gid", REAL_STORE, "--shop-domain", REAL_DOMAIN)
            self.assertEqual((code, init["autocount_book"]), (0, "AED_TESTINGAPI"))
            code, status = self.run_cli(root, "status")
            self.assertEqual((code, status["mode"], status["environment"], status["integrity"]), (0, "connector", "development", "ok"))
            self.assertEqual(self.run_cli(root, "activate-policy", "--confirm-book", "AED_XBOUNDARIES", "--max-creates", "1", "--ttl-hours", "1"), (2, {"status": "refused", "code": "policy_book_confirmation_mismatch"}))
            self.assertEqual(self.run_cli(root, "record-pos-visible", "--member-ref", "m-" + "1" * 20, "--outlet", "XB01", "--observed-at", "2026-10-14T00:00:00Z"), (2, {"status": "refused", "code": "member_ref_unknown"}))
        finally:
            shutil.rmtree(holder, ignore_errors=True)

    def test_v3_ledger_needs_attended_additive_migration(self):
        # G2 A3: a v3 ledger (the shape of the preserved UAT ledger) is refused
        # until migrate-ledger adds the two provenance columns; nothing else changes.
        holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-v3-"))
        try:
            root = holder / "state"
            self.assertEqual(self.run_cli(root, "prepare-state-root")[0], 0)
            self.assertEqual(self.run_cli(root, "init-ledger", "--mode", "connector", "--environment", "development", "--store-gid", REAL_STORE, "--shop-domain", REAL_DOMAIN)[0], 0)
            connection = sqlite3.connect(root / "ledger.sqlite3")
            connection.execute("INSERT INTO allocation_attempts(customer_gid, attempt_no, candidate, status, created_at) VALUES ('gid://shopify/Customer/1', 1, 'M000201', 'FREE_BOUND', '2026-10-16T00:00:00Z')")
            for column in ("derivation", "generator_preview"):
                connection.execute(f"ALTER TABLE allocation_attempts DROP COLUMN {column}")
            connection.execute("PRAGMA user_version=3")
            connection.commit()
            before = [tuple(row) for row in connection.execute("SELECT seq, type, hash FROM events ORDER BY seq")]
            connection.close()
            self.assertEqual(self.run_cli(root, "status"), (2, {"status": "refused", "code": "ledger_schema_migration_required"}))
            self.assertEqual(self.run_cli(root, "migrate-ledger"), (0, {"status": "migrated", "schema_version": 4}))
            self.assertEqual(self.run_cli(root, "migrate-ledger"), (0, {"status": "current", "schema_version": 4}))
            code, status = self.run_cli(root, "status")
            self.assertEqual((code, status["integrity"]), (0, "ok"))
            connection = sqlite3.connect(root / "ledger.sqlite3")
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
                row = connection.execute("SELECT candidate, status, generator_preview, derivation FROM allocation_attempts").fetchone()
                self.assertEqual(row, ("M000201", "FREE_BOUND", None, None))
                after = [tuple(row) for row in connection.execute("SELECT seq, type, hash FROM events ORDER BY seq")]
                self.assertEqual(after[:len(before)], before)
                self.assertEqual([event[1] for event in after[len(before):]], ["ledger_schema_migrated:v4"])
                self.assertEqual(status["events_verified"], len(after))  # hash chain intact
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO allocation_attempts(customer_gid, attempt_no, created_at, derivation) VALUES ('gid://shopify/Customer/2', 1, 'x', 'guessed')")
            finally:
                connection.close()
        finally:
            shutil.rmtree(holder, ignore_errors=True)

    def test_v2_ledger_is_refused_not_migrated(self):
        holder = Path(tempfile.mkdtemp(prefix="xb-directlocal-v2-"))
        try:
            connection = sqlite3.connect(holder / "ledger.sqlite3")
            connection.execute("PRAGMA user_version=2")
            connection.close()
            self.assertEqual(self.run_cli(holder, "status"), (2, {"status": "refused", "code": "ledger_schema_version_unsupported"}))
        finally:
            shutil.rmtree(holder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
