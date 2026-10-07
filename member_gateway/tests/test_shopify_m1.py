"""Shopify-authoritative M1 synthetic scenarios (#155 G3). Offline only."""

import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from xb_member_gateway.bootstrap import BootstrapError
from xb_member_gateway.models import JobState, ShopifyAdmissionState
from xb_member_gateway.protected_payload import ProtectedPayloadCipher, ProtectedPayloadError, decode_key, envelope_digest
from xb_member_gateway.reconciliation import reconcile_uncertain_write
from xb_member_gateway.repository import InMemoryRepository, SourceConflict
from xb_member_gateway.results import build_shopify_member_record
from xb_member_gateway.shopify_admission import (
    BaselineCaptureError,
    ShopifyAdminClient,
    ShopifyReadError,
    baseline_digest,
    capture_member_mg_baseline,
    evaluate_profile,
)
from xb_member_gateway.shopify_receiver import capture_baseline
from xb_member_gateway.shopify_webhook import WEBHOOK_ROUTE, verify_shopify_hmac

try:
    from ._shopify_support import (
        API_VERSION, BASELINE_GID, CUTOVER, NEW_GID, NOW, PROOF_GID, SECOND_GID, SHOP, SYNTHETIC_EMAIL, SYNTHETIC_EMAIL_NORMALIZED,
        SYNTHETIC_PHONE, SYNTHETIC_PHONE_DIGITS, FakeReader, ShopifyHarness, customer, runtime_key, shopify_config,
        signed_headers, webhook_body,
    )
except ImportError:  # discovered as a top-level module
    from _shopify_support import (  # type: ignore
        API_VERSION, BASELINE_GID, CUTOVER, NEW_GID, NOW, PROOF_GID, SECOND_GID, SHOP, SYNTHETIC_EMAIL, SYNTHETIC_EMAIL_NORMALIZED,
        SYNTHETIC_PHONE, SYNTHETIC_PHONE_DIGITS, FakeReader, ShopifyHarness, customer, runtime_key, shopify_config,
        signed_headers, webhook_body,
    )

PII_VALUES = ("Synthetic", "Shopify Alpha", SYNTHETIC_EMAIL, SYNTHETIC_EMAIL_NORMALIZED, SYNTHETIC_PHONE, SYNTHETIC_PHONE_DIGITS)


class RecordingRepository(InMemoryRepository):
    """Proves the HMAC fence: counts every repository write attempt."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.webhook_writes = 0

    def record_shopify_webhook(self, receipt, *, now=None):
        self.webhook_writes += 1
        return super().record_shopify_webhook(receipt, now=now)


def assert_no_pii(test, value):
    rendered = repr(value)
    for item in PII_VALUES:
        test.assertNotIn(item, rendered)


class WebhookReceiverTests(unittest.TestCase):
    def setUp(self):
        self.repository = RecordingRepository()
        # Receiver behaviour is independent of the cutover baseline.
        self.harness = ShopifyHarness(self.repository, baseline=None)

    def test_valid_hmac_accepted_and_only_metadata_and_gid_persisted(self):
        response = self.harness.deliver()
        self.assertEqual((response.status, response.body["status"], response.body["replayed"]), (200, "accepted", False))
        receipt, received = self.repository._shopify_receipts["wh-00000000-0001"]
        self.assertEqual(receipt.customer_gid, NEW_GID)
        self.assertEqual(set(receipt.__slots__), {"webhook_id", "topic", "shop_domain", "api_version", "event_id", "triggered_at", "customer_gid"})
        assert_no_pii(self, self.repository.snapshot_state())
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.PENDING)

    def test_exact_raw_body_hmac_required(self):
        raw = webhook_body("customers/create", NEW_GID)
        headers = signed_headers(self.harness.secret, raw)
        # Same JSON semantics, different bytes -> rejected.
        reformatted = json.dumps(json.loads(raw)).encode("utf-8")
        self.assertNotEqual(raw, reformatted)
        self.assertEqual(self.harness.receiver.handle("POST", WEBHOOK_ROUTE, headers, reformatted).status, 401)
        self.assertTrue(verify_shopify_hmac(raw, headers["X-Shopify-Hmac-Sha256"], self.harness.secret))
        self.assertFalse(verify_shopify_hmac(raw + b" ", headers["X-Shopify-Hmac-Sha256"], self.harness.secret))

    def test_invalid_or_missing_hmac_rejected_before_parse_and_persistence(self):
        raw = b"{not-json-and-would-fail-parse"
        for hmac_value in ("", "not base64!", "AAAA", None):
            headers = signed_headers(self.harness.secret, raw)
            if hmac_value is None:
                headers.pop("X-Shopify-Hmac-Sha256")
            else:
                headers["X-Shopify-Hmac-Sha256"] = hmac_value
            with mock.patch("xb_member_gateway.shopify_webhook.json.loads", side_effect=AssertionError("parsed before HMAC")):
                response = self.harness.receiver.handle("POST", WEBHOOK_ROUTE, headers, raw)
            self.assertEqual((response.status, response.body["error_code"]), (401, "shopify_hmac_invalid"))
        self.assertEqual(self.repository.webhook_writes, 0)
        self.assertEqual(self.repository._shopify_receipts, {})
        self.assertEqual(self.repository._shopify_admissions, {})

    def test_wrong_secret_rejected(self):
        raw = webhook_body("customers/create", NEW_GID)
        headers = signed_headers(b"another-synthetic-secret-value!!", raw)
        self.assertEqual(self.harness.receiver.handle("POST", WEBHOOK_ROUTE, headers, raw).status, 401)
        self.assertEqual(self.repository.webhook_writes, 0)

    def test_duplicate_webhook_id_safe_and_conflicting_facts_fail_closed(self):
        self.assertFalse(self.harness.deliver().body["replayed"])
        self.assertTrue(self.harness.deliver().body["replayed"])
        conflict = self.harness.deliver(SECOND_GID)
        self.assertEqual((conflict.status, conflict.body["error_code"]), (409, "shopify_webhook_identity_conflict"))
        self.assertIsNone(self.repository.get_shopify_admission(SECOND_GID))
        self.assertEqual(len(self.repository._shopify_receipts), 1)

    def test_unsupported_topic_shop_and_version_rejected_without_persistence(self):
        for changes, code in (
            ({"topic": "orders/create"}, "shopify_topic_not_allowlisted"),
            ({"shop": "other-shop.myshopify.com"}, "shopify_shop_not_allowlisted"),
            ({"version": "2025-07"}, "shopify_api_version_not_allowlisted"),
        ):
            raw = webhook_body("customers/create", NEW_GID)
            headers = signed_headers(self.harness.secret, raw, **changes)
            response = self.harness.receiver.handle("POST", WEBHOOK_ROUTE, headers, raw)
            self.assertEqual((response.status, response.body["error_code"]), (422, code))
        self.assertEqual(self.repository._shopify_receipts, {})

    def test_single_route_size_bound_and_disabled_receiver(self):
        raw = webhook_body("customers/create", NEW_GID)
        headers = signed_headers(self.harness.secret, raw)
        self.assertEqual(self.harness.receiver.handle("POST", "/v1/source-events", headers, raw).status, 404)
        self.assertEqual(self.harness.receiver.handle("GET", WEBHOOK_ROUTE, headers, raw).status, 404)
        self.assertEqual(self.harness.receiver.handle("POST", WEBHOOK_ROUTE, headers, b"x" * 300_000).status, 413)
        disabled = ShopifyHarness(InMemoryRepository(), config=shopify_config(shopify_enabled=False), baseline=None)
        self.assertEqual(disabled.deliver().status, 503)

    def test_tags_added_payload_uses_customer_id_gid_and_identity_is_cross_checked(self):
        self.assertEqual(self.harness.deliver(topic="customer.tags_added", webhook_id="wh-00000000-0002").status, 200)
        raw = json.dumps({"id": 1, "admin_graphql_api_id": NEW_GID}).encode()
        response = self.harness.receiver.handle("POST", WEBHOOK_ROUTE, signed_headers(self.harness.secret, raw, webhook_id="wh-00000000-0003"), raw)
        self.assertEqual((response.status, response.body["error_code"]), (400, "shopify_customer_identity_inconsistent"))

    def test_responses_and_errors_never_echo_body_values(self):
        responses = [self.harness.deliver(), self.harness.deliver(SECOND_GID)]
        assert_no_pii(self, [item.body for item in responses])
        self.assertNotIn(NEW_GID, json.dumps([item.body for item in responses]))


class BaselineTests(unittest.TestCase):
    @staticmethod
    def pages(*pages):
        state = {"index": 0, "afters": []}

        def reader(after):
            state["afters"].append(after)
            page = pages[state["index"]]
            state["index"] += 1
            return page

        return reader, state

    @staticmethod
    def page(nodes, has_next, cursor=None):
        return {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}

    @staticmethod
    def node(gid, tags=("member-mg",), created_at=CUTOVER - timedelta(days=30)):
        value = created_at.isoformat().replace("+00:00", "Z") if hasattr(created_at, "isoformat") else created_at
        return {"id": gid, "createdAt": value, "tags": list(tags)}

    def capture(self, reader, **kwargs):
        return capture_member_mg_baseline(reader, cutover_at=CUTOVER, **kwargs)

    def test_zero_one_and_multi_page_exhaustive_capture(self):
        reader, _ = self.pages(self.page([], False))
        self.assertEqual(self.capture(reader), ((), ()))
        reader, _ = self.pages(self.page([self.node(BASELINE_GID), self.node(NEW_GID, tags=["vip"])], False))
        self.assertEqual(self.capture(reader), ((BASELINE_GID,), ()))
        reader, state = self.pages(
            self.page([self.node(SECOND_GID, tags=["Member-MG"])], True, "c1"),
            self.page([self.node(NEW_GID, tags=[])], True, "c2"),
            self.page([self.node(BASELINE_GID, tags=["member-mg", "x"])], False),
        )
        self.assertEqual(self.capture(reader), ((BASELINE_GID, SECOND_GID), ()))
        self.assertEqual(state["afters"], [None, "c1", "c2"])

    def test_created_at_partitions_historical_and_transition_with_equality_post_cutover(self):
        reader, _ = self.pages(
            self.page([self.node(BASELINE_GID, created_at=CUTOVER - timedelta(seconds=1)), self.node(SECOND_GID, created_at=CUTOVER)], True, "c1"),
            self.page([self.node(NEW_GID, created_at=CUTOVER + timedelta(seconds=5)), self.node("gid://shopify/Customer/9000000103", tags=[], created_at=CUTOVER + timedelta(seconds=6))], False),
        )
        self.assertEqual(self.capture(reader), ((BASELINE_GID,), (NEW_GID, SECOND_GID)))
        # Offsets are honoured: 19:00:00+08:00 == 11:00:00Z == CUTOVER (post-cutover).
        reader, _ = self.pages(self.page([
            self.node(NEW_GID, created_at="2026-10-06T19:00:00+08:00"),
            self.node(BASELINE_GID, created_at="2026-10-06T18:59:59.999999+08:00"),
        ], False))
        self.assertEqual(self.capture(reader), ((BASELINE_GID,), (NEW_GID,)))

    def test_missing_malformed_or_offset_less_created_at_fails_capture(self):
        for value in (None, "", "2026-10-06T11:00:00", "2026-10-06", "yesterday", "20261006T110000Z",
                      "2026-10-06 11:00:00Z", "2026-10-06T11:00:00.1234567Z", 1759748400):
            node = {"id": NEW_GID, "tags": ["vip"]}
            if value is not None:
                node["createdAt"] = value
            reader, _ = self.pages(self.page([self.node(BASELINE_GID), node], False))
            with self.assertRaises(BaselineCaptureError) as raised:
                self.capture(reader)
            self.assertEqual(raised.exception.code, "baseline_created_at_invalid", value)
        with self.assertRaises(BaselineCaptureError) as raised:
            capture_member_mg_baseline(self.pages(self.page([], False))[0], cutover_at=CUTOVER.replace(tzinfo=None))
        self.assertEqual(raised.exception.code, "baseline_cutover_invalid")

    def test_fail_closed_on_missing_cursor_duplicates_errors_and_malformed_pages(self):
        cases = (
            ((self.page([], True, None),), "baseline_cursor_missing"),
            ((self.page([], True, "c1"), self.page([], True, "c1")), "baseline_cursor_repeated"),
            ((self.page([self.node(NEW_GID, tags=[])], True, "c1"), self.page([self.node(NEW_GID, tags=[])], False)), "baseline_duplicate_gid"),
            (({"pageInfo": {"hasNextPage": "no"}, "nodes": []},), "baseline_page_info_invalid"),
            (({"nodes": []},), "baseline_page_invalid"),
            ((self.page([self.node("gid://shopify/Order/1", tags=[])], False),), "baseline_node_invalid"),
        )
        for pages, code in cases:
            reader, _ = self.pages(*pages)
            with self.assertRaises(BaselineCaptureError) as raised:
                self.capture(reader)
            self.assertEqual(raised.exception.code, code)

        def failing(after):
            raise ShopifyReadError("shopify_graphql_error")

        with self.assertRaises(BaselineCaptureError) as raised:
            self.capture(failing)
        self.assertEqual(raised.exception.code, "baseline_read_failed:shopify_graphql_error")
        reader, _ = self.pages(*[self.page([], True, f"c{i}") for i in range(3)])
        with self.assertRaises(BaselineCaptureError):
            self.capture(reader, max_pages=3)

    def test_seal_count_digest_readback_and_admission_gate(self):
        repository = InMemoryRepository()
        ShopifyHarness(repository, baseline=None).deliver_receiver_proof()
        sealed = repository.store_shopify_baseline([SECOND_GID, BASELINE_GID], shop_domain=SHOP, api_version="2026-10", capture_started_at=CUTOVER, now=NOW)
        self.assertEqual((sealed.member_count, sealed.member_digest), (2, baseline_digest([BASELINE_GID, SECOND_GID])))
        self.assertTrue(repository.verify_shopify_baseline(sealed.baseline_id))
        self.assertIsNone(repository.shopify_admission_gate())
        with self.assertRaisesRegex(SourceConflict, "shopify_baseline_already_sealed"):
            repository.store_shopify_baseline([], shop_domain=SHOP, api_version="2026-10", capture_started_at=CUTOVER)
        with self.assertRaisesRegex(SourceConflict, "shopify_baseline_duplicate_gid"):
            InMemoryRepository().store_shopify_baseline([NEW_GID, NEW_GID], shop_domain=SHOP, api_version="2026-10", capture_started_at=CUTOVER)
        with self.assertRaisesRegex(SourceConflict, "shopify_receiver_not_verified_before_cutover"):
            InMemoryRepository().store_shopify_baseline([NEW_GID], shop_domain=SHOP, api_version="2026-10", capture_started_at=CUTOVER)
        # Tampered rows can never enable admission.
        repository._shopify_baseline_members[sealed.baseline_id] = frozenset({BASELINE_GID})
        with self.assertRaisesRegex(SourceConflict, "shopify_admission_baseline_verification_failed"):
            repository.enable_shopify_admission(sealed.baseline_id, "synthetic-approval")
        with self.assertRaisesRegex(SourceConflict, "shopify_admission_baseline_verification_failed"):
            InMemoryRepository().enable_shopify_admission("baseline-" + "0" * 32, "synthetic-approval")


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository()
        self.harness = ShopifyHarness(self.repository)

    def test_new_post_cutover_member_mg_admitted_exactly_once(self):
        admission, counts = self.harness.admit()
        self.assertEqual((admission.state, counts["admitted"]), (ShopifyAdmissionState.ADMITTED, 1))
        job = self.repository.get_job(admission.job_id)
        self.assertEqual((job.source_system, job.response_id, job.member_payload, job.state), ("shopify", None, {}, JobState.QUEUED))
        self.assertTrue(job.source_response_ref.startswith("hmac-v1:"))
        self.assertTrue(self.repository.shopify_payload_present(job.job_id))
        # Replays and later topics never admit again.
        for topic, webhook in (("customers/update", "wh-00000000-0010"), ("customer.tags_added", "wh-00000000-0011")):
            self.assertEqual(self.harness.deliver(topic=topic, webhook_id=webhook).status, 200)
        self.assertTrue(self.harness.deliver().body["replayed"])
        self.harness.processor.run_once()
        self.assertEqual(len([item for item in self.repository.all_jobs() if item.is_shopify]), 1)
        assert_no_pii(self, self.repository.audit_events)

    def test_baseline_gid_excluded_and_legacy_tags_and_preexisting_reviewed(self):
        self.harness.reader.customers[BASELINE_GID] = customer(BASELINE_GID, created_at=CUTOVER - timedelta(days=400))
        self.harness.deliver(BASELINE_GID, topic="customers/update", webhook_id="wh-00000000-0020")
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID, tags=("member-mg", "member-legacy"))
        self.harness.deliver(NEW_GID, webhook_id="wh-00000000-0021")
        self.harness.reader.customers[SECOND_GID] = customer(SECOND_GID, created_at=CUTOVER - timedelta(days=30))
        self.harness.deliver(SECOND_GID, topic="customers/update", webhook_id="wh-00000000-0022")
        self.harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(BASELINE_GID).state, ShopifyAdmissionState.EXCLUDED_BASELINE)
        legacy = self.repository.get_shopify_admission(NEW_GID)
        preexisting = self.repository.get_shopify_admission(SECOND_GID)
        self.assertEqual((legacy.state, legacy.reason_code), (ShopifyAdmissionState.MANUAL_REVIEW, "legacy_tag_present"))
        self.assertEqual((preexisting.state, preexisting.reason_code), (ShopifyAdmissionState.MANUAL_REVIEW, "preexisting_customer_not_new_signup"))
        self.assertFalse(any(item.is_shopify for item in self.repository.all_jobs()))

    def test_xb_member_tag_and_baseline_created_at_conflict_reviewed_never_admitted(self):
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID, tags=("member-mg", "XB Member"))
        self.harness.deliver(NEW_GID, webhook_id="wh-00000000-0030")
        # A sealed historical GID whose authoritative re-read reports
        # createdAt >= C is a conflict, whatever topic or timing delivered it.
        self.harness.reader.customers[BASELINE_GID] = customer(BASELINE_GID, created_at=CUTOVER)
        self.harness.deliver(BASELINE_GID, topic="customers/update", webhook_id="wh-00000000-0031")
        self.harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).reason_code, "legacy_tag_present")
        conflict = self.repository.get_shopify_admission(BASELINE_GID)
        self.assertEqual((conflict.state, conflict.reason_code), (ShopifyAdmissionState.MANUAL_REVIEW, "baseline_created_at_conflict"))
        self.assertFalse(any(item.is_shopify for item in self.repository.all_jobs()))

    def test_non_member_not_admitted_and_becomes_re_evaluable(self):
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID, tags=())
        self.harness.deliver()
        self.harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.NOT_ELIGIBLE)
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID)
        self.harness.deliver(topic="customer.tags_added", webhook_id="wh-00000000-0040")
        self.harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.ADMITTED)

    def test_incomplete_profile_waits_boundedly_then_reviews(self):
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID, start=None)
        self.harness.deliver()
        for index in range(2):
            self.harness.processor.run_once()
            admission = self.repository.get_shopify_admission(NEW_GID)
            self.assertEqual((admission.state, admission.reason_code, admission.check_count), (ShopifyAdmissionState.PENDING, "membership_dates_missing", index + 1))
            self.harness.processor.clock = None
            self.harness.processor.clock = (lambda value=NOW + timedelta(minutes=2 * (index + 1)): value)
        self.harness.processor.run_once()
        admission = self.repository.get_shopify_admission(NEW_GID)
        self.assertEqual((admission.state, admission.reason_code), (ShopifyAdmissionState.MANUAL_REVIEW, "membership_dates_missing"))

    def test_profile_decisions_for_name_dates_contacts(self):
        base = dict(in_baseline=False, cutover_at=CUTOVER, profile_complete_required=True)
        cases = (
            (customer(first=None, last="  "), "MANUAL_REVIEW", "name_missing"),
            (customer(start="2026-13-01"), "MANUAL_REVIEW", "membership_date_malformed"),
            (customer(start_type="single_line_text_field"), "MANUAL_REVIEW", "membership_date_malformed"),
            (customer(start="2026-10-06", expiry="2026-10-05"), "MANUAL_REVIEW", "membership_expiry_before_start"),
            (customer(phone="call me"), "MANUAL_REVIEW", "phone_malformed"),
            (customer(email="not-an-email"), "MANUAL_REVIEW", "email_malformed"),
            (dict(customer(), createdAt="yesterday"), "MANUAL_REVIEW", "created_at_malformed"),
            (dict(customer(), createdAt="2026-10-06T11:30:00"), "MANUAL_REVIEW", "created_at_malformed"),
            (customer(created_at=CUTOVER - timedelta(seconds=1)), "MANUAL_REVIEW", "preexisting_customer_not_new_signup"),
            (None, "NOT_ELIGIBLE", "customer_not_found"),
        )
        for profile, outcome, code in cases:
            decision = evaluate_profile(profile, **base)
            self.assertEqual((decision.outcome, decision.code), (outcome, code))
        waiting = evaluate_profile(customer(first=None, last=None), **dict(base, profile_complete_required=False))
        self.assertEqual((waiting.outcome, waiting.code), ("WAIT", "name_missing"))
        self.assertEqual(evaluate_profile(customer(created_at=CUTOVER), **base).outcome, "ADMIT")
        historical = dict(base, in_baseline=True)
        self.assertEqual(evaluate_profile(customer(created_at=CUTOVER - timedelta(days=1)), **historical).outcome, "EXCLUDED_BASELINE")
        self.assertEqual(evaluate_profile(dict(customer(), createdAt=None), **historical).outcome, "EXCLUDED_BASELINE")
        conflict = evaluate_profile(customer(created_at=CUTOVER), **historical)
        self.assertEqual((conflict.outcome, conflict.code), ("MANUAL_REVIEW", "baseline_created_at_conflict"))
        admitted = evaluate_profile(customer(phone=None, email=None), **base)
        self.assertEqual(admitted.create_payload, {"email_address": None, "expiry_date": "2028-10-05", "mobile_phone": None, "name": "Synthetic Shopify Alpha", "register_date": "2026-10-06"})
        exact = evaluate_profile(customer(), **base).create_payload
        self.assertEqual((exact["mobile_phone"], exact["email_address"]), (SYNTHETIC_PHONE_DIGITS, SYNTHETIC_EMAIL_NORMALIZED))
        self.assertNotIn("AdmissionDecision(outcome='ADMIT', code=None, create_payload", repr(admitted))
        assert_no_pii(self, repr(admitted))

    def test_admission_disabled_or_unverified_baseline_never_admits(self):
        repository = InMemoryRepository()
        harness = ShopifyHarness(repository, enable=False)
        harness.reader.customers[NEW_GID] = customer()
        harness.deliver()
        self.assertEqual(harness.processor.run_once()["processed"], 0)
        self.assertEqual(repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.PENDING)
        repository.enable_shopify_admission(harness.baseline.baseline_id, "synthetic-approval")
        repository._shopify_baseline_members[harness.baseline.baseline_id] = frozenset()
        self.assertIsNone(repository.shopify_admission_gate())
        self.assertEqual(harness.processor.run_once()["admitted"], 0)
        no_baseline = ShopifyHarness(InMemoryRepository(), baseline=None)
        no_baseline.reader.customers[NEW_GID] = customer()
        no_baseline.deliver()
        self.assertEqual(no_baseline.processor.run_once()["processed"], 0)

    def test_read_errors_retry_or_review_without_admitting(self):
        self.harness.reader.error = ShopifyReadError("shopify_graphql_throttled", retryable=True)
        self.harness.deliver()
        # The harness receiver-proof GID is pending too and is deferred alike.
        self.assertEqual(self.harness.processor.run_once()["deferred"], 2)
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.PENDING)
        self.harness.reader.error = ShopifyReadError("shopify_graphql_error")
        self.harness.processor.clock = lambda: NOW + timedelta(hours=1)
        self.harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).reason_code, "profile_read_failed")

    def test_admin_client_is_read_only_and_bounded(self):
        calls = []

        def transport(url, body, headers, timeout):
            calls.append((url, json.loads(body), headers))
            return json.dumps({"data": {"customer": customer()}}).encode()

        client = ShopifyAdminClient(shopify_config(), "synthetic-admin-token-value", transport=transport)
        self.assertEqual(client.read_customer(NEW_GID)["id"], NEW_GID)
        url, body, headers = calls[0]
        self.assertEqual(url, f"https://{SHOP}/admin/api/2026-10/graphql.json")
        self.assertTrue(body["query"].startswith("query "))
        self.assertNotIn("mutation", body["query"])
        self.assertNotIn("synthetic-admin-token-value", repr(client))
        bad = ShopifyAdminClient(shopify_config(), "token", transport=lambda *a: b'{"errors":[{"extensions":{"code":"THROTTLED"}}]}')
        with self.assertRaises(ShopifyReadError) as raised:
            bad.read_customer(NEW_GID)
        self.assertTrue(raised.exception.retryable)
        mismatch = ShopifyAdminClient(shopify_config(), "token", transport=lambda *a: json.dumps({"data": {"customer": customer(SECOND_GID)}}).encode())
        with self.assertRaisesRegex(ShopifyReadError, "shopify_customer_identity_mismatch"):
            mismatch.read_customer(NEW_GID)


class ProtectedPayloadTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository()
        self.harness = ShopifyHarness(self.repository)

    def test_ciphertext_only_at_rest_and_bound_to_job(self):
        admission, _ = self.harness.admit()
        stored = self.repository._shopify_payloads[admission.job_id]
        self.assertTrue(stored["envelope"].startswith("xbpp1.k1."))
        self.assertEqual(self.repository.get_job(admission.job_id).payload_hash, envelope_digest(stored["envelope"]))
        assert_no_pii(self, self.repository.snapshot_state())
        payload = self.harness.cipher.decrypt(admission.job_id, stored["envelope"])
        self.assertEqual(payload["name"], "Synthetic Shopify Alpha")
        with self.assertRaisesRegex(ProtectedPayloadError, "protected_payload_decrypt_failed"):
            self.harness.cipher.decrypt("job-" + "f" * 32, stored["envelope"])
        tampered = stored["envelope"][:-2] + ("AA" if stored["envelope"][-2:] != "AA" else "BB")
        with self.assertRaises(ProtectedPayloadError):
            self.harness.cipher.decrypt(admission.job_id, tampered)
        other = ProtectedPayloadCipher.from_binding("k1", runtime_key())
        with self.assertRaisesRegex(ProtectedPayloadError, "protected_payload_decrypt_failed"):
            other.decrypt(admission.job_id, stored["envelope"])

    def test_key_absent_or_invalid_fails_closed_without_detail(self):
        for value, code in ((None, "protected_payload_key_missing"), ("", "protected_payload_key_missing"), ("short", "protected_payload_key_invalid"), ("A" * 43, "protected_payload_key_invalid")):
            with self.assertRaises(ProtectedPayloadError) as raised:
                decode_key(value)
            self.assertEqual(raised.exception.code, code)
        cipher = ProtectedPayloadCipher.from_binding("k1", runtime_key())
        self.assertNotIn("_key", repr(cipher).replace("key_id", ""))
        with self.assertRaisesRegex(ProtectedPayloadError, "protected_payload_shape_invalid"):
            cipher.encrypt("job-" + "a" * 32, {"name": "x", "mobile_phone": "", "email_address": None, "register_date": "2026-10-06", "expiry_date": "2027-10-05"})

    def test_claim_decrypts_in_memory_only_and_decrypt_failure_dead_letters(self):
        admission, _ = self.harness.admit()
        claimed = self.harness.call("POST", "/v1/worker/claim", {})
        job = claimed.body["job"]
        self.assertEqual(job["schema_version"], "xb.member.gateway.job.v3")
        self.assertEqual(set(job["create_payload"]), {"name", "mobile_phone", "email_address", "register_date", "expiry_date"})
        self.assertNotIn("member_payload", job)
        self.assertNotIn("customer_gid", json.dumps(job))
        self.assertNotIn(NEW_GID, json.dumps(job))
        status = self.harness.call("GET", f"/v1/jobs/{admission.job_id}/status")
        assert_no_pii(self, status.body)
        # A second repository job with a corrupt envelope is dead-lettered on claim.
        repository = InMemoryRepository()
        harness = ShopifyHarness(repository)
        second, _ = harness.admit()
        repository._shopify_payloads[second.job_id]["envelope"] = ProtectedPayloadCipher.from_binding("k1", runtime_key()).encrypt(second.job_id, {"name": "Other", "mobile_phone": None, "email_address": None, "register_date": "2026-10-06", "expiry_date": "2027-10-05"})
        failed = harness.call("POST", "/v1/worker/claim", {})
        self.assertEqual((failed.status, failed.body["error_code"]), (409, "protected_payload_decrypt_failed"))
        self.assertEqual(repository.get_job(second.job_id).state, JobState.DEAD_LETTER)
        self.assertFalse(repository.shopify_payload_present(second.job_id))
        status = harness.call("GET", "/v1/operator/shopify-status").body
        self.assertEqual(status["job_state_counts"]["DEAD_LETTER"], 1)

    def test_terminal_scrub_and_post_scrub_replay_does_not_recreate(self):
        admission, _ = self.harness.admit()
        job, member_no = self.harness.claim_through_allocation()
        fence, writer = self.harness.dispatch(job, member_no)
        self.assertTrue(self.repository.shopify_payload_present(job["job_id"]))
        self.harness.confirm(job, writer)
        result = self.harness.result(job, fence, member_no, "CREATED_VERIFIED", found=True, match=True)
        self.assertEqual(result.status, 200, result.body)
        self.assertFalse(self.repository.shopify_payload_present(job["job_id"]))
        self.assertEqual(self.repository.shopify_scrub_record(job["job_id"])["job_state"], "CREATED_VERIFIED")
        self.assertIsNone(self.repository.welcome_outbox_for_job(job["job_id"]))
        self.assertEqual(self.repository.protected_payload_leftover_count(), 0)
        assert_no_pii(self, self.repository.snapshot_state())
        self.assertEqual(self.harness.deliver(webhook_id="wh-00000000-0099").status, 200)
        self.harness.processor.run_once()
        self.assertEqual(len([item for item in self.repository.all_jobs() if item.is_shopify]), 1)
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).job_id, admission.job_id)

    def test_uncertainty_retains_payload_until_exact_reconciliation_then_scrubs(self):
        admission, _ = self.harness.admit()
        job, member_no = self.harness.claim_through_allocation()
        fence, writer = self.harness.dispatch(job, member_no)
        self.harness.confirm(job, writer)
        uncertain = self.harness.result(job, fence, member_no, "WRITE_OUTCOME_UNCERTAIN", found=False, match=False, error_code="save_outcome_uncertain")
        self.assertEqual(uncertain.status, 200, uncertain.body)
        self.assertTrue(self.repository.shopify_payload_present(job["job_id"]))
        self.assertEqual(self.repository.protected_payload_leftover_count(), 0)
        # Uncertain write never re-enters claim/allocation.
        self.assertFalse(self.harness.call("POST", "/v1/worker/claim", {}).body["claimed"])
        claim = self.harness.call("POST", "/v1/worker/reconcile/claim", {})
        self.assertTrue(claim.body["claimed"])
        self.assertEqual(claim.body["job"]["member_no"], member_no)
        expected = build_shopify_member_record(claim.body["job"]["create_payload"], member_no)
        self.assertIsNone(expected["DOB"])
        reconciled = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/reconcile", {"member_no": member_no, "lookup_status": "exact_match", "readback_found": True, "readback_match": True, "error_code": None})
        self.assertEqual((reconciled.status, reconciled.body["state"]), (200, "CREATED_VERIFIED"))
        self.assertFalse(self.repository.shopify_payload_present(job["job_id"]))
        self.assertIsNone(self.repository.welcome_outbox_for_job(job["job_id"]))
        # A late writer result after reconciliation cannot reopen or reallocate.
        duplicate = self.harness.result(job, fence, member_no, "CREATED_VERIFIED", found=True, match=True)
        self.assertEqual((duplicate.status, duplicate.body["duplicate"]), (200, True))
        late = self.harness.result(job, fence, member_no, "CREATED_READBACK_MISMATCH", found=True, match=False, error_code="readback_mismatch")
        self.assertEqual(late.status, 409)
        self.assertEqual(self.repository.get_job(job["job_id"]).state, JobState.CREATED_VERIFIED)
        self.assertEqual(self.repository.get_allocation(job["job_id"]).member_no, member_no)

    def test_absent_mismatch_and_ambiguous_reconciliation_paths(self):
        for lookup, found, match, state, retained in (
            ("absent", False, False, "CONFIRMED_NOT_CREATED", False),
            ("mismatch", True, False, "CREATED_READBACK_MISMATCH", False),
            ("ambiguous", False, False, "WRITE_OUTCOME_UNCERTAIN", True),
        ):
            repository = InMemoryRepository()
            harness = ShopifyHarness(repository)
            harness.admit()
            job, member_no = harness.claim_through_allocation()
            fence, writer = harness.dispatch(job, member_no)
            harness.confirm(job, writer)
            harness.result(job, fence, member_no, "WRITE_OUTCOME_UNCERTAIN", found=False, match=False, error_code="save_outcome_uncertain")
            response = harness.call("POST", f"/v1/jobs/{job['job_id']}/reconcile", {"member_no": member_no, "lookup_status": lookup, "readback_found": found, "readback_match": match, "error_code": None if lookup == "exact_match" else "reconciliation_manual"})
            self.assertEqual(response.status, 200, response.body)
            self.assertEqual(repository.get_job(job["job_id"]).state.value, state)
            self.assertEqual(repository.shopify_payload_present(job["job_id"]), retained)
            wrong = harness.call("POST", f"/v1/jobs/{job['job_id']}/reconcile", {"member_no": "M999999", "lookup_status": "exact_match", "readback_found": True, "readback_match": True, "error_code": None})
            self.assertEqual(wrong.status, 409)
            # A closed or ambiguous case is never re-offered for automatic reconciliation.
            self.assertFalse(harness.call("POST", "/v1/worker/reconcile/claim", {}).body["claimed"])

    def test_reconciliation_helper_uses_same_member_no_and_null_aware_record(self):
        admission, _ = self.harness.admit(profile=customer(phone=None, email=None))
        job, member_no = self.harness.claim_through_allocation()
        fence, writer = self.harness.dispatch(job, member_no)
        self.harness.confirm(job, writer)
        self.harness.result(job, fence, member_no, "WRITE_OUTCOME_UNCERTAIN", found=False, match=False, error_code="save_outcome_uncertain")
        payload = self.harness.cipher.decrypt(job["job_id"], self.repository.get_shopify_protected_payload(job["job_id"]))
        expected = build_shopify_member_record(payload, member_no)
        lookups = []

        class Adapter:
            def get_member(self, value):
                lookups.append(value)
                return dict(expected, MobilePhone="", EmailAddress="")

        result = reconcile_uncertain_write(
            job=self.repository.get_job(job["job_id"]), allocation=self.repository.get_allocation(job["job_id"]),
            fence=self.repository.get_dispatch_fence(job["job_id"]), adapter=Adapter(), repository=self.repository,
            now=NOW, expected=expected,
        )
        self.assertEqual(lookups, [member_no])
        self.assertEqual(result.status.value, "CREATED_READBACK_MISMATCH")

    def test_readiness_fails_closed_on_terminal_leftover_payload(self):
        self.assertNotIn("terminal_job_protected_payload_present", self.harness.service.readiness()["reasons"])
        admission, _ = self.harness.admit()
        self.assertNotIn("terminal_job_protected_payload_present", self.harness.service.readiness()["reasons"])
        self.repository._jobs[admission.job_id].state = JobState.MANUAL_REVIEW
        readiness = self.harness.service.readiness()
        self.assertFalse(readiness["ready"])
        self.assertIn("terminal_job_protected_payload_present", readiness["reasons"])


class AllocationAndPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRepository()
        self.harness = ShopifyHarness(self.repository)
        self.admission, _ = self.harness.admit()

    def claim(self):
        job = self.harness.call("POST", "/v1/worker/claim", {}).body["job"]
        self.harness.call("POST", f"/v1/jobs/{job['job_id']}/precheck", {})
        return job

    def precheck(self, job, outcome="NO_CANDIDATE", phone=False, mobile=False, email=False):
        return self.harness.call("POST", f"/v1/jobs/{job['job_id']}/shopify/legacy-precheck", {"outcome": outcome, "phone_member_no_hit": phone, "mobile_phone_hit": mobile, "email_hit": email})

    def test_any_candidate_or_lookup_failure_routes_to_manual_review_and_scrubs(self):
        for flags in ({"phone": True}, {"mobile": True}, {"email": True}):
            repository = InMemoryRepository()
            harness = ShopifyHarness(repository)
            harness.admit()
            self.harness, self.repository = harness, repository
            job = self.claim()
            response = self.precheck(job, "CANDIDATE_FOUND", **flags)
            self.assertEqual(response.body["state"], "MANUAL_REVIEW")
            self.assertEqual(repository.get_job(job["job_id"]).last_error_code, "legacy_member_candidate_review")
            self.assertFalse(repository.shopify_payload_present(job["job_id"]))
        repository = InMemoryRepository()
        harness = ShopifyHarness(repository)
        harness.admit()
        self.harness, self.repository = harness, repository
        job = self.claim()
        self.assertEqual(self.precheck(job, "LOOKUP_FAILED").body["state"], "MANUAL_REVIEW")
        self.assertEqual(repository.get_job(job["job_id"]).last_error_code, "legacy_lookup_failed_review")
        # Inconsistent evidence (hit without CANDIDATE_FOUND) is rejected.
        harness2 = ShopifyHarness(InMemoryRepository())
        harness2.admit()
        self.harness, self.repository = harness2, harness2.repository
        job = self.claim()
        self.assertEqual(self.precheck(job, "NO_CANDIDATE", phone=True).status, 409)

    def test_allocation_requires_clear_precheck_and_uses_generator_candidate(self):
        job = self.claim()
        blocked = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/candidate", {})
        self.assertEqual((blocked.status, blocked.body["error_code"]), (409, "shopify_legacy_precheck_required"))
        self.precheck(job)
        candidate = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/candidate", {})
        self.assertEqual(candidate.body, {"job_id": job["job_id"], "bound": False, "generator": "member_command_get_next_member_no", "candidates_remaining": 3})
        self.assertNotIn("candidate", candidate.body)
        phone_shaped = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": "M 1", "status": "FREE", "probe_reference": "probe-x"})
        self.assertEqual(phone_shaped.status, 400)

    def test_occupied_candidate_boundedly_reallocates_then_reviews(self):
        job = self.claim()
        self.precheck(job)
        for index, member_no in enumerate(("M000201", "M000202")):
            response = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": member_no, "status": "OCCUPIED", "probe_reference": f"probe-occ-{index}"})
            self.assertEqual(response.body["candidates_remaining"], 2 - index)
        exhausted = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": "M000203", "status": "OCCUPIED", "probe_reference": "probe-occ-2"})
        self.assertEqual(exhausted.status, 409)
        self.assertEqual(self.repository.get_job(job["job_id"]).state, JobState.MANUAL_REVIEW)
        self.assertIsNone(self.repository.get_allocation(job["job_id"]))

    def test_repeated_occupied_candidate_and_ambiguity_never_advance(self):
        job = self.claim()
        self.precheck(job)
        self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": "M000301", "status": "OCCUPIED", "probe_reference": "probe-a"})
        repeated = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": "M000301", "status": "FREE", "probe_reference": "probe-b"})
        self.assertEqual((repeated.status, repeated.body["error_code"]), (409, "member_no_generator_not_advancing"))
        self.assertIsNone(self.repository.get_allocation(job["job_id"]))
        harness = ShopifyHarness(InMemoryRepository())
        harness.admit()
        self.harness, self.repository = harness, harness.repository
        job = self.claim()
        self.precheck(job)
        ambiguous = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": "M000401", "status": "AMBIGUOUS", "probe_reference": "probe-c"})
        self.assertEqual(ambiguous.status, 409)
        self.assertEqual(self.repository.get_job(job["job_id"]).state, JobState.MANUAL_REVIEW)
        self.assertIsNone(self.repository.get_allocation(job["job_id"]))

    def test_mapping_and_member_no_uniqueness_and_no_post_fence_reallocation(self):
        job, member_no = self.harness.claim_through_allocation(member_no="M000501")
        # A second admitted GID cannot bind the same MemberNo.
        self.harness.admit(SECOND_GID, profile=customer(SECOND_GID), webhook_id="wh-00000000-0502")
        fence, writer = self.harness.dispatch(job, member_no)
        self.harness.confirm(job, writer)
        self.harness.result(job, fence, member_no, "CREATED_VERIFIED", found=True, match=True)
        second = self.harness.call("POST", "/v1/worker/claim", {}).body["job"]
        self.harness.call("POST", f"/v1/jobs/{second['job_id']}/precheck", {})
        self.precheck(second)
        race = self.harness.call("POST", f"/v1/jobs/{second['job_id']}/allocation/probe", {"candidate": member_no, "status": "FREE", "probe_reference": "probe-race"})
        self.assertEqual(race.body.get("bound"), False)
        self.assertIsNone(self.repository.get_allocation(second["job_id"]))
        self.assertNotEqual(self.repository.get_shopify_admission(NEW_GID).job_id, self.repository.get_shopify_admission(SECOND_GID).job_id)
        # Post-fence: the first job can never bind a different MemberNo.
        with self.assertRaises(Exception):
            self.repository.bind_allocation(job["job_id"], "M000999", "probe-late", "ws-" + "a" * 32, now=NOW)
        self.assertEqual(self.repository.get_allocation(job["job_id"]).member_no, member_no)

    # -- M1: gateway-bound MemberNo collision (settled G3 defect) ---------

    def _held_member_no_and_second_job(self, member_no="M000601"):
        """Job A fully creates ``member_no``; job B is claimed and prechecked."""
        job, bound = self.harness.claim_through_allocation(member_no=member_no)
        self.harness.admit(SECOND_GID, profile=customer(SECOND_GID), webhook_id="wh-00000000-0602")
        fence, writer = self.harness.dispatch(job, bound)
        self.harness.confirm(job, writer)
        self.harness.result(job, fence, bound, "CREATED_VERIFIED", found=True, match=True)
        second = self.claim()
        self.precheck(second)
        return bound, second

    def probe(self, job, member_no, status, reference):
        return self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/probe", {"candidate": member_no, "status": status, "probe_reference": reference})

    def test_repeated_gateway_held_collision_terminates_within_bound(self):
        held, second = self._held_member_no_and_second_job()
        first = self.probe(second, held, "FREE", "probe-gw-1")
        self.assertEqual(first.status, 200, first.body)
        self.assertEqual((first.body["bound"], first.body["gateway_bound_collision"], first.body["candidates_remaining"]), (False, True, 2))
        # GetNextMemberNo() re-offers the same gateway-held MemberNo: before
        # the correction this returned 200/unbound forever (PRECHECKING loop).
        repeated = self.probe(second, held, "FREE", "probe-gw-2")
        self.assertEqual((repeated.status, repeated.body["error_code"]), (409, "member_no_generator_not_advancing"))
        job = self.repository.get_job(second["job_id"])
        self.assertEqual((job.state, job.last_error_code), (JobState.MANUAL_REVIEW, "member_no_generator_not_advancing"))
        self.assertIsNone(self.repository.get_allocation(second["job_id"]))
        self.assertIsNone(self.repository.get_dispatch_fence(second["job_id"]))
        self.assertFalse(self.repository.shopify_payload_present(second["job_id"]))
        after = self.probe(second, "M000699", "FREE", "probe-gw-3")
        self.assertEqual(after.status, 409)
        self.assertIsNone(self.repository.get_allocation(second["job_id"]))

    def test_gateway_held_collision_consumes_the_accepted_occupied_bound(self):
        held, second = self._held_member_no_and_second_job()
        self.assertEqual(self.probe(second, "M000602", "OCCUPIED", "probe-occ-a").body["candidates_remaining"], 2)
        self.assertEqual(self.probe(second, "M000603", "OCCUPIED", "probe-occ-b").body["candidates_remaining"], 1)
        exhausted = self.probe(second, held, "FREE", "probe-gw-x")
        self.assertEqual((exhausted.status, exhausted.body["error_code"]), (409, "member_no_exhausted"))
        job = self.repository.get_job(second["job_id"])
        self.assertEqual((job.state, job.last_error_code), (JobState.MANUAL_REVIEW, "member_no_reallocation_exhausted"))
        self.assertIsNone(self.repository.get_allocation(second["job_id"]))
        self.assertFalse(self.repository.shopify_payload_present(second["job_id"]))

    def test_occupied_and_gateway_held_candidates_then_free_candidate_binds(self):
        held, second = self._held_member_no_and_second_job()
        collision = self.probe(second, held, "FREE", "probe-gw-p")
        self.assertEqual((collision.body["gateway_bound_collision"], collision.body["candidates_remaining"]), (True, 2))
        self.assertEqual(self.probe(second, "M000612", "OCCUPIED", "probe-occ-p").body["candidates_remaining"], 1)
        bound = self.probe(second, "M000613", "FREE", "probe-free-p")
        self.assertEqual((bound.status, bound.body["bound"], bound.body["member_no"]), (200, True, "M000613"))
        self.assertEqual(self.repository.get_allocation(second["job_id"]).member_no, "M000613")
        self.assertEqual(self.repository.member_no_owner(held), self.repository.get_shopify_admission(NEW_GID).job_id)
        self.assertEqual(self.repository.member_no_owner("M000613"), second["job_id"])

    def test_dispatch_requires_precheck_for_current_attempt(self):
        job, member_no = self.harness.claim_through_allocation()
        self.repository._shopify_prechecks.clear()
        recheck = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/allocation/recheck", {"status": "FREE", "probe_reference": "probe-" + "2" * 32})
        self.assertEqual(recheck.status, 200)
        intent = self.harness.call("POST", f"/v1/jobs/{job['job_id']}/write-intent", {"operation": "member.create", "member_no": member_no, "payload_hash": job["payload_hash"]})
        self.assertEqual((intent.status, intent.body["error_code"]), (409, "dispatch_ineligible"))

    def test_operator_status_is_counts_only(self):
        self.harness.claim_through_allocation()
        status = self.harness.call("GET", "/v1/operator/shopify-status")
        self.assertEqual(status.status, 200)
        rendered = json.dumps(status.body)
        assert_no_pii(self, status.body)
        self.assertNotIn("gid://", rendered)
        self.assertNotIn("M000101", rendered)
        self.assertEqual(status.body["admission_counts"]["ADMITTED"], 1)
        self.assertTrue(status.body["baseline_verified"])


class CutoverTests(unittest.TestCase):
    """#155 targeted G2 cutover contract through the real capture entry point."""

    TAGGED_LATER = "gid://shopify/Customer/9000000104"

    def setUp(self):
        self.repository = InMemoryRepository()
        self.harness = ShopifyHarness(self.repository, baseline=None)
        self.cutover = CUTOVER
        self.repository.shopify_cutover_now = lambda: self.cutover
        self.pages = []

    def node(self, gid, created_at, tags=("member-mg",)):
        value = created_at.isoformat().replace("+00:00", "Z") if hasattr(created_at, "isoformat") else created_at
        return {"id": gid, "createdAt": value, "tags": list(tags)}

    def set_pages(self, *pages):
        self.pages = [{"pageInfo": {"hasNextPage": index < len(pages) - 1, "endCursor": f"c{index + 1}" if index < len(pages) - 1 else None}, "nodes": list(nodes)} for index, nodes in enumerate(pages)]

    def baseline_page(self, after):
        if isinstance(self.pages, Exception):
            raise self.pages
        return self.pages.pop(0)

    def capture(self):
        composition = SimpleNamespace(
            repository=self.repository, client=SimpleNamespace(baseline_page=self.baseline_page),
            shopify_config=SimpleNamespace(shop_domain=SHOP, api_version=API_VERSION),
        )
        return capture_baseline(composition, now=lambda: self.cutover + timedelta(minutes=1))

    def enable(self, sealed):
        self.repository.enable_shopify_admission(sealed["baseline_id"], "synthetic-approval-155-g3-a2")

    def shopify_jobs(self):
        return [item for item in self.repository.all_jobs() if item.is_shopify]

    def test_post_cutover_last_page_gid_without_create_receipt_is_pending_then_admitted_once(self):
        self.harness.deliver_receiver_proof()
        self.set_pages(
            [self.node(BASELINE_GID, CUTOVER - timedelta(days=400))],
            [self.node(NEW_GID, CUTOVER + timedelta(seconds=5))],
        )
        sealed = self.capture()
        self.assertEqual((sealed["member_count"], sealed["transition_pending_count"], sealed["state"]), (1, 1, "SEALED"))
        self.assertFalse(self.repository.shopify_baseline_contains(sealed["baseline_id"], NEW_GID))
        self.assertTrue(self.repository.shopify_baseline_contains(sealed["baseline_id"], BASELINE_GID))
        pending = self.repository.get_shopify_admission(NEW_GID)
        self.assertEqual((pending.state, pending.job_id, pending.reason_code), (ShopifyAdmissionState.PENDING, None, None))
        self.assertFalse(any(receipt.customer_gid == NEW_GID for receipt, _ in self.repository._shopify_receipts.values()))
        self.assertEqual(self.harness.processor.run_once()["processed"], 0)  # admission still disabled
        self.enable(sealed)
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID, created_at=CUTOVER + timedelta(seconds=5))
        self.harness.reader.customers[BASELINE_GID] = customer(BASELINE_GID, created_at=CUTOVER - timedelta(days=400))
        self.harness.deliver(BASELINE_GID, topic="customers/update", webhook_id="wh-00000000-1001")
        counts = self.harness.processor.run_once()
        self.assertEqual(counts["admitted"], 1)
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.ADMITTED)
        self.assertEqual(self.repository.get_shopify_admission(BASELINE_GID).state, ShopifyAdmissionState.EXCLUDED_BASELINE)
        self.harness.deliver(NEW_GID, webhook_id="wh-00000000-1002")
        self.harness.processor.run_once()
        self.assertEqual(len(self.shopify_jobs()), 1)
        assert_no_pii(self, self.repository.snapshot_state())

    def test_created_at_equal_to_cutover_is_post_cutover(self):
        self.harness.deliver_receiver_proof()
        self.set_pages([self.node(NEW_GID, CUTOVER), self.node(BASELINE_GID, CUTOVER - timedelta(seconds=1))])
        sealed = self.capture()
        self.assertEqual((sealed["member_count"], sealed["transition_pending_count"]), (1, 1))
        self.assertFalse(self.repository.shopify_baseline_contains(sealed["baseline_id"], NEW_GID))
        self.enable(sealed)
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID, created_at=CUTOVER)
        self.harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.ADMITTED)

    def test_missing_or_late_or_wrong_binding_receiver_proof_rejects_capture_and_seals_nothing(self):
        def attempt():
            self.set_pages([self.node(BASELINE_GID, CUTOVER - timedelta(days=1))])
            with self.assertRaises(BootstrapError) as raised:
                self.capture()
            self.assertEqual(raised.exception.code, "shopify_receiver_not_verified_before_cutover")
            self.assertIsNone(self.repository.get_shopify_baseline())
            self.assertEqual(len(self.pages), 1, "no page may be read without receiver proof")

        attempt()
        self.harness.deliver_receiver_proof(at=CUTOVER)  # received_at == C is not before C
        attempt()
        other = ShopifyHarness(self.repository, baseline=None, config=shopify_config(api_version="2026-07"))
        other.deliver_receiver_proof(at=CUTOVER - timedelta(minutes=5), webhook_id="wh-00000000-0901")  # wrong API version binding
        attempt()
        self.assertIsNone(self.repository.get_shopify_admission(BASELINE_GID))

    def test_malformed_or_naive_created_at_rejects_capture(self):
        self.harness.deliver_receiver_proof()
        for value in ("2026-10-06T11:00:05", "2026-10-06", "not-a-time", None):
            node = self.node(NEW_GID, value) if value is not None else {"id": NEW_GID, "tags": ["member-mg"]}
            self.set_pages([self.node(BASELINE_GID, CUTOVER - timedelta(days=1))], [node])
            with self.assertRaises(BootstrapError) as raised:
                self.capture()
            self.assertEqual(raised.exception.code, "baseline_created_at_invalid")
            self.assertIsNone(self.repository.get_shopify_baseline())
            self.assertIsNone(self.repository.get_shopify_admission(NEW_GID))

    def test_pre_cutover_gid_tagged_only_after_scan_is_conservative_manual_review(self):
        self.harness.deliver_receiver_proof()
        self.set_pages([self.node(BASELINE_GID, CUTOVER - timedelta(days=9)), self.node(self.TAGGED_LATER, CUTOVER - timedelta(days=3), tags=())])
        sealed = self.capture()
        self.assertEqual((sealed["member_count"], sealed["transition_pending_count"]), (1, 0))
        self.enable(sealed)
        self.harness.reader.customers[self.TAGGED_LATER] = customer(self.TAGGED_LATER, created_at=CUTOVER - timedelta(days=3))
        self.harness.deliver(self.TAGGED_LATER, topic="customer.tags_added", webhook_id="wh-00000000-1101")
        self.harness.processor.run_once()
        review = self.repository.get_shopify_admission(self.TAGGED_LATER)
        self.assertEqual((review.state, review.reason_code), (ShopifyAdmissionState.MANUAL_REVIEW, "preexisting_customer_not_new_signup"))
        self.assertEqual(self.shopify_jobs(), [])

    def test_webhook_and_scan_for_same_post_cutover_gid_admit_one_job(self):
        self.harness.deliver_receiver_proof()
        self.harness.deliver(NEW_GID, webhook_id="wh-00000000-1201")
        before = self.repository.get_shopify_admission(NEW_GID)
        self.set_pages([self.node(NEW_GID, CUTOVER + timedelta(seconds=30))])
        sealed = self.capture()
        self.assertEqual(sealed["transition_pending_count"], 1)
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID), before)  # existing row authoritative
        self.enable(sealed)
        self.harness.reader.customers[NEW_GID] = customer(NEW_GID, created_at=CUTOVER + timedelta(seconds=30))
        self.harness.deliver(NEW_GID, topic="customers/update", webhook_id="wh-00000000-1202")
        self.harness.processor.run_once()
        self.harness.deliver(NEW_GID, topic="customer.tags_added", webhook_id="wh-00000000-1203")
        self.harness.processor.run_once()
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.ADMITTED)
        self.assertEqual(len(self.shopify_jobs()), 1)

    def test_seal_failure_leaves_no_historical_or_transition_state(self):
        self.harness.deliver_receiver_proof()
        for historical, transition, code in (
            ([BASELINE_GID], [BASELINE_GID], "shopify_baseline_transition_overlap"),
            ([BASELINE_GID], [NEW_GID, NEW_GID], "shopify_transition_duplicate_gid"),
            ([BASELINE_GID], ["gid://shopify/Order/1"], "shopify_transition_members_invalid"),
        ):
            with self.assertRaisesRegex(SourceConflict, code):
                self.repository.store_shopify_baseline(historical, transition_gids=transition, shop_domain=SHOP, api_version=API_VERSION, capture_started_at=CUTOVER, now=NOW)
        with self.assertRaisesRegex(SourceConflict, "shopify_cutover_not_whole_second"):
            self.repository.store_shopify_baseline([BASELINE_GID], transition_gids=[NEW_GID], shop_domain=SHOP, api_version=API_VERSION, capture_started_at=CUTOVER + timedelta(microseconds=1), now=NOW)
        # The seal recompute disagrees with the validated digest (second call only).
        real = baseline_digest([BASELINE_GID])
        with mock.patch("xb_member_gateway.repository.shopify_baseline_digest", side_effect=[real, "sha256:" + "0" * 64]):
            with self.assertRaisesRegex(SourceConflict, "shopify_baseline_seal_mismatch"):
                self.repository.store_shopify_baseline([BASELINE_GID], transition_gids=[NEW_GID], shop_domain=SHOP, api_version=API_VERSION, capture_started_at=CUTOVER, now=NOW)
        self.assertIsNone(self.repository.get_shopify_baseline())
        self.assertEqual(self.repository._shopify_baseline_members, {})
        self.assertIsNone(self.repository.get_shopify_admission(NEW_GID))

    def test_crash_before_seal_leaves_no_cutover_and_restart_gets_fresh_cutover(self):
        self.harness.deliver_receiver_proof()
        between = "gid://shopify/Customer/9000000105"
        self.pages = ShopifyReadError("shopify_transport_failed", retryable=True)
        with self.assertRaises(BootstrapError):
            self.capture()
        self.assertIsNone(self.repository.get_shopify_baseline())
        self.assertIsNone(self.repository.shopify_admission_gate())
        # Restart from scratch: a fresh C2 > C1; a member created between the
        # two attempts is historical under the sealed C2.
        self.cutover = CUTOVER + timedelta(minutes=30)
        self.set_pages([self.node(between, CUTOVER + timedelta(minutes=10)), self.node(NEW_GID, self.cutover + timedelta(seconds=1))])
        sealed = self.capture()
        baseline = self.repository.get_shopify_baseline()
        self.assertEqual(baseline.capture_started_at, (CUTOVER + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"))
        self.assertTrue(self.repository.shopify_baseline_contains(sealed["baseline_id"], between))
        self.assertEqual(self.repository.get_shopify_admission(NEW_GID).state, ShopifyAdmissionState.PENDING)
        self.enable(sealed)
        self.assertEqual(self.repository.shopify_admission_gate()[1], CUTOVER + timedelta(minutes=30))


class FormsIsolationTests(unittest.TestCase):
    def test_gateway_without_shopify_runtime_never_claims_shopify_jobs(self):
        repository = InMemoryRepository()
        harness = ShopifyHarness(repository)
        harness.admit()
        from xb_member_gateway.api import GatewayService
        forms_only = GatewayService(harness.service.config, repository, adapter_ready=True, clock=NOW)
        self.assertFalse(forms_only.claim("ws-" + "b" * 32)["claimed"])
        self.assertFalse(any(reason.startswith("shopify") or "protected_payload" in reason for reason in forms_only.readiness()["reasons"]))

    def test_shopify_control_and_routes_require_their_scopes(self):
        harness = ShopifyHarness(InMemoryRepository(), enable=False)
        denied = harness.call("POST", "/v1/control/shopify-admission/enable", {"baseline_id": harness.baseline.baseline_id, "approval_reference": "x"}, headers=harness.headers)
        self.assertEqual(denied.status, 403)
        enabled = harness.call("POST", "/v1/control/shopify-admission/enable", {"baseline_id": harness.baseline.baseline_id, "approval_reference": "synthetic-approval"})
        self.assertEqual(enabled.status, 200)
        self.assertEqual(harness.call("POST", "/v1/control/shopify-admission/disable", {}).status, 200)
        self.assertIsNone(harness.repository.shopify_admission_gate())


if __name__ == "__main__":
    unittest.main()
