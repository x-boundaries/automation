import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows/member_forms_gateway_ingest.workflow.json"
MAILER = ROOT / "n8n-workflows/member_welcome_email_outbox.workflow.json"
FIXTURE = ROOT / "tests/fixtures/member_forms_google_v1.fixture"
PAGE_TOKEN_EXPRESSION = "={{ $json.request_page_token || undefined }}"
CUTOVER = "2026-08-30T00:00:00Z"
CONFIG = {
    "activation_enabled": True, "form_alias": "member_registration", "mapping_version": "member-intake.v1",
    "forms_api_url": "https://forms.example.com/v1/forms/GOOGLE_FORM_ID_PLACEHOLDER/responses",
    "gateway_origin": "https://gateway.example.com", "max_pages_per_run": 20,
}

# Executes one exported Code node with the n8n globals it uses: $json,
# $runIndex, require('crypto') and $('<node>').first()/last().
NODE_RUNNER = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const localRequire = name => name === 'crypto' ? require('crypto') : require(name);
  const selector = name => {
    if (!(name in payload.nodes)) throw new Error('unexpected_node_reference:' + name);
    const item = {json: payload.nodes[name]};
    return {first: () => item, last: () => item};
  };
  const run = new Function('$json', '$runIndex', 'require', '$', payload.code);
  const result = run(payload.json, payload.run_index || 0, localRequire, selector);
  process.stdout.write(JSON.stringify({ok: true, result}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error && error.message ? error.message : error)}));
}
"""

EXPRESSION_RUNNER = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const evaluate = new Function('$json', 'return (' + payload.expression + ');');
  const value = evaluate(payload.json);
  process.stdout.write(JSON.stringify({ok: true, type: typeof value, value: value === undefined ? null : value}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error && error.message ? error.message : error)}));
}
"""


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def cursor_v2(**changes):
    value = {
        "schema_version": "xb.member.gateway.source_cursor.v2", "source_system": "google_forms",
        "form_alias": "member_registration", "mapping_version": "member-intake.v1",
        "production_cutover_exact": CUTOVER, "filter_exact": f"timestamp >= {CUTOVER}",
        "admission_mode": "first_member", "page_size": 1, "cursor_state_version": 0,
        "accepted_member_count": 0, "accepted_member_allowance_remaining": 1,
        "active_epoch": {"epoch_id": "epoch-" + "a" * 32, "status": "ACTIVE", "admission_mode": "first_member", "epoch_state_version": 0, "current_page_token": None, "page_ordinal": 0, "predecessor_epoch_id": None, "restart_reason": None, "open_page": None},
    }
    value.update(changes)
    return value


def page_state(**changes):
    value = {"epoch_id": "epoch-" + "a" * 32, "expected_epoch_state_version": 0, "request_page_token": None, "filter_exact": f"timestamp >= {CUTOVER}", "admission_mode": "first_member", "pages_fetched": 0, "max_pages": 20, "action": "fetch"}
    value.update(changes)
    return value


@unittest.skipUnless(shutil.which("node"), "node executable unavailable")
class MemberGatewayN8nTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        self.fixtures = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.nodes = {node["name"]: node for node in self.workflow["nodes"]}
        self.forms_http = self.nodes["Google Forms single page (configured outside repo)"]

    def run_code(self, name, json_input, *, nodes=None, run_index=0, workflow=None):
        node = {item["name"]: item for item in (workflow or self.workflow)["nodes"]}[name]
        payload = {"code": node["parameters"]["jsCode"], "json": json_input, "nodes": nodes or {}, "run_index": run_index}
        proc = subprocess.run(["node", "-e", NODE_RUNNER], input=json.dumps(payload), capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def canonicalize(self, page, *, status=200, state=None):
        return self.run_code(
            "Canonicalize source page", {"statusCode": status, "body": page},
            nodes={"Prepare page request": state or page_state(), "Repository-safe source configuration": CONFIG},
        )

    def one_response_page(self, **answer_values):
        page = copy.deepcopy(self.fixtures["one_page"]["pages"][0])
        for question, supplied in answer_values.items():
            page["responses"][0]["answers"][question]["textAnswers"]["answers"][0]["value"] = supplied
        return page

    def canonical_phone_from_adapter(self, supplied):
        result = self.canonicalize(self.one_response_page(QUESTION_ID_PHONE_PLACEHOLDER=supplied))
        self.assertTrue(result["ok"], result)
        action = result["result"][0]["json"]["action"]
        self.assertEqual(action["kind"], "member", action)
        return action["body"]["payload"]["phone"]

    # --- canonicalization parity with the gateway -------------------------

    def test_adapter_phone_is_opaque_digits_with_no_country_inference(self):
        self.assertEqual(self.canonical_phone_from_adapter("91234567"), "91234567")
        self.assertEqual(self.canonical_phone_from_adapter("6591234567"), "6591234567")
        self.assertEqual(self.canonical_phone_from_adapter("+44 7700 900123"), "447700900123")
        self.assertEqual(self.canonical_phone_from_adapter("(65) 9123-4567"), "6591234567")
        self.assertEqual(self.canonical_phone_from_adapter("65.9123.4567"), "6591234567")
        self.assertEqual(self.canonical_phone_from_adapter("0912345"), "0912345")
        self.assertEqual(self.canonical_phone_from_adapter("1" * 6), "1" * 6)
        self.assertEqual(self.canonical_phone_from_adapter("1" * 15), "1" * 15)

    def test_customer_invalid_input_becomes_a_pii_free_rejection_with_gateway_codes(self):
        sys.path.insert(0, str(ROOT / "member_gateway/src"))
        try:
            from xb_member_gateway.canonical import CanonicalizationError, CUSTOMER_REJECTION_CODES, canonical_payload_from_fields
        finally:
            sys.path.pop(0)
        base = {"name": "Synthetic Alpha", "phone": "8123 4567", "email": "alpha@example.test", "birthday_month": "March", "marketing_consent": "No", "pdpa_acknowledged": "I agree"}
        questions = {"name": "QUESTION_ID_NAME_PLACEHOLDER", "phone": "QUESTION_ID_PHONE_PLACEHOLDER", "email": "QUESTION_ID_EMAIL_PLACEHOLDER", "birthday_month": "QUESTION_ID_BIRTHDAY_MONTH_PLACEHOLDER", "marketing_consent": "QUESTION_ID_MARKETING_CONSENT_PLACEHOLDER"}
        cases = (
            {"phone": "91234567 ext123"}, {"phone": "91234567x12"}, {"phone": "91234567#12"}, {"phone": "++6591234567"},
            {"phone": "65+91234567"}, {"phone": "9123\t4567"}, {"phone": "９１２３４５６７"},
            {"phone": "-- --"}, {"phone": "1" * 5}, {"phone": "1" * 16}, {"phone": "   "},
            {"email": "not-an-email"}, {"birthday_month": "Smarch"}, {"marketing_consent": "maybe"}, {"name": "   "},
            {"marketing_consent": "maybe", "phone": "bad"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                supplied = {**base, **changes}
                try:
                    canonical_payload_from_fields(**supplied)
                    self.fail("gateway accepted invalid input")
                except CanonicalizationError as exc:
                    expected = exc.code
                self.assertIn(expected, CUSTOMER_REJECTION_CODES)
                page = self.one_response_page(**{questions[field]: value for field, value in changes.items()})
                result = self.canonicalize(page)
                self.assertTrue(result["ok"], result)
                output = result["result"][0]["json"]
                action = output["action"]
                self.assertEqual((action["kind"], action["body"]["error_code"]), ("rejection", expected))
                body = action["body"]
                self.assertEqual(set(body), {"schema_version", "source_system", "form_alias", "response_id", "create_time", "mapping_version", "request_id", "payload_hash", "error_code", "operation"})
                serialized = json.dumps(body, ensure_ascii=False)
                for private in ("Synthetic Alpha", "alpha@example.test", "8123"):
                    self.assertNotIn(private, serialized)
                self.assertEqual(output["open"]["items"], [{"response_id": body["response_id"], "create_time": body["create_time"], "payload_hash": body["payload_hash"]}])

    def test_adapter_and_gateway_canonicalizers_agree_on_valid_input(self):
        sys.path.insert(0, str(ROOT / "member_gateway/src"))
        try:
            from xb_member_gateway.canonical import canonical_phone, canonicalize_source_event
        finally:
            sys.path.pop(0)
        for supplied in ("91234567", "6591234567", "+44 7700 900123", "0912345", "1" * 15):
            with self.subTest(supplied=supplied):
                self.assertEqual(self.canonical_phone_from_adapter(supplied), canonical_phone(supplied))
        # Actual adapter output is accepted unchanged by the gateway canonicalizer.
        result = self.canonicalize(self.one_response_page())
        event = result["result"][0]["json"]["action"]["body"]
        sys.path.insert(0, str(ROOT / "member_gateway/src"))
        try:
            canonical = canonicalize_source_event(event)
        finally:
            sys.path.pop(0)
        self.assertEqual((canonical.create_time, canonical.payload_hash), (event["create_time"], event["payload_hash"]))

    # --- verbatim createTime / no ordering authority ----------------------

    def test_create_time_is_forwarded_verbatim_for_every_google_width(self):
        for created in ("2026-08-30T01:02:03Z", "2026-08-30T01:02:03.000Z", "2026-08-30T01:02:03.123456Z", "2026-08-30T01:02:03.123456789Z"):
            with self.subTest(created=created):
                page = self.one_response_page()
                page["responses"][0]["createTime"] = created
                output = self.canonicalize(page)["result"][0]["json"]
                self.assertEqual(output["action"]["body"]["create_time"], created)
                self.assertEqual(output["open"]["items"][0]["create_time"], created)
        for created in ("2026-08-30T01:02:03.1Z", "2026-08-30T01:02:03.1234567Z", "2026-08-30T01:02:03+00:00", "2026-08-30T01:02:03"):
            with self.subTest(created=created):
                page = self.one_response_page()
                page["responses"][0]["createTime"] = created
                result = self.canonicalize(page)
                self.assertFalse(result["ok"])
                self.assertIn("forms_create_time_invalid", result["error"])

    def test_no_sorting_ordering_assertion_or_date_reserialisation_remains(self):
        code = "\n".join(node["parameters"].get("jsCode", "") for node in self.workflow["nodes"])
        for forbidden in ("forms_page_unsorted", "new Date(", "Date.parse", "toISOString", "responses].sort", "staticData", "$getWorkflowStaticData", "lastSubmittedTime", "localeCompare"):
            self.assertNotIn(forbidden, code)
        self.assertIsNone(self.workflow["staticData"])

    def test_one_page_opens_with_item_identity_and_member_event(self):
        output = self.canonicalize(self.one_response_page())["result"][0]["json"]
        event = output["action"]["body"]
        self.assertEqual((event["response_id"], event["create_time"]), ("forms-resp-one-001", "2026-08-30T01:02:03.000Z"))
        canonical = json.dumps(event["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(event["payload_hash"], "sha256:" + hashlib.sha256(canonical.encode()).hexdigest())
        self.assertEqual(output["open"], {"expected_epoch_state_version": 0, "request_page_token": None, "next_page_token": None, "terminal": True, "items": [{"response_id": event["response_id"], "create_time": event["create_time"], "payload_hash": event["payload_hash"]}]})

    def test_empty_terminal_page_and_continuation_token(self):
        output = self.canonicalize({})["result"][0]["json"]
        self.assertEqual((output["action"], output["open"]["items"], output["open"]["terminal"]), (None, [], True))
        page = self.fixtures["multiple_pages"]["pages"][0]
        output = self.canonicalize(page)["result"][0]["json"]
        self.assertEqual((output["open"]["next_page_token"], output["open"]["terminal"]), ("page-token-002", False))
        state = page_state(request_page_token="page-token-002", pages_fetched=1)
        second = copy.deepcopy(self.fixtures["multiple_pages"]["pages"][1])
        # The fixture's "+00:00" offset is not a Google-issued createTime form.
        self.assertIn("forms_create_time_invalid", self.canonicalize(second, state=state)["error"])
        second["responses"][0]["createTime"] = "2026-08-30T01:11:00Z"
        output = self.canonicalize(second, state=state)["result"][0]["json"]
        self.assertEqual((output["open"]["request_page_token"], output["open"]["terminal"]), ("page-token-002", True))

    def test_repeated_malformed_and_oversized_pages_fail_closed(self):
        repeated = self.fixtures["repeated_token"]["pages"][1]
        result = self.canonicalize(repeated, state=page_state(request_page_token=repeated["nextPageToken"]))
        self.assertIn("forms_page_token_repeated", result["error"])
        self.assertIn("forms_page_token_invalid", self.canonicalize(self.fixtures["malformed_token"]["pages"][0])["error"])
        oversized = self.one_response_page()
        oversized["responses"].append(copy.deepcopy(oversized["responses"][0]))
        self.assertIn("forms_page_invalid", self.canonicalize(oversized)["error"])
        self.assertIn("forms_answer_value_invalid", self.canonicalize(self.fixtures["missing_mapping"]["pages"][0])["error"])
        self.assertIn("forms_question_mapping_unknown", self.canonicalize(self.fixtures["unknown_mapping"]["pages"][0])["error"])
        bad_id = self.one_response_page()
        bad_id["responses"][0]["responseId"] = "bad id!"
        self.assertIn("forms_response_id_invalid", self.canonicalize(bad_id)["error"])

    def test_only_positively_classified_token_invalidation_restarts(self):
        invalid = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid page token."}}
        restart = self.canonicalize(invalid, status=400, state=page_state(request_page_token="stale"))
        self.assertEqual(restart["result"][0]["json"]["kind"], "token_invalidated")
        for status, body, state in (
            (400, invalid, page_state()),
            (400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid filter."}}, page_state(request_page_token="stale")),
            (401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "page token"}}, page_state(request_page_token="stale")),
            (500, {}, page_state(request_page_token="stale")),
        ):
            with self.subTest(status=status, body=body):
                result = self.canonicalize(body, status=status, state=state)
                self.assertFalse(result["ok"])
                self.assertIn("forms_api_request_failed", result["error"])

    # --- epoch planning / bounded loop -----------------------------------

    def test_plan_epoch_attempt_validates_v2_and_routes_restart_stop_fetch(self):
        config = {"Repository-safe source configuration": CONFIG}
        fetch = self.run_code("Plan epoch attempt", cursor_v2(), nodes=config)["result"][0]["json"]
        self.assertEqual((fetch["action"], fetch["filter_exact"], fetch["request_page_token"]), ("fetch", f"timestamp >= {CUTOVER}", None))
        crashed = cursor_v2()
        crashed["active_epoch"]["open_page"] = {"page_id": "page-" + "b" * 32}
        self.assertEqual(self.run_code("Plan epoch attempt", crashed, nodes=config)["result"][0]["json"]["restart_reason"], "ambiguous_crashed_attempt")
        consumed = cursor_v2(accepted_member_count=1, accepted_member_allowance_remaining=0)
        self.assertEqual(self.run_code("Plan epoch attempt", consumed, nodes=config)["result"][0]["json"]["action"], "stop")
        continuous = cursor_v2(admission_mode="continuous", accepted_member_allowance_remaining=None)
        self.assertEqual(self.run_code("Plan epoch attempt", continuous, nodes=config)["result"][0]["json"]["action"], "fetch")
        for label, value, code in (
            ("v1", {"schema_version": "xb.member.gateway.source_cursor.v1"}, "source_cursor_invalid"),
            ("filter", cursor_v2(filter_exact="timestamp >= 2026-08-29T00:00:00Z"), "source_cutover_invalid"),
            ("width", cursor_v2(production_cutover_exact="2026-08-30T00:00:00.1Z", filter_exact="timestamp >= 2026-08-30T00:00:00.1Z"), "source_cutover_invalid"),
            ("page-size", cursor_v2(page_size=50), "source_page_size_invalid"),
            ("no-epoch", cursor_v2(active_epoch=None), "source_epoch_invalid"),
        ):
            with self.subTest(label=label):
                self.assertIn(code, self.run_code("Plan epoch attempt", value, nodes=config)["error"])
        self.assertIn("source_epoch_restart_loop_bounded", self.run_code("Plan epoch attempt", cursor_v2(), nodes=config, run_index=3)["error"])

    def test_plan_next_page_stops_on_completion_first_member_and_budget(self):
        def plan(commit, *, action=None, state=None):
            nodes = {"Canonicalize source page": {"state": state or page_state(), "action": action}}
            return self.run_code("Plan next page", commit, nodes=nodes)["result"][0]["json"]
        commit = {"page_state": "COMMITTED", "epoch_id": "epoch-" + "a" * 32, "epoch_state_version": 2, "epoch_status": "ACTIVE", "current_page_token": "page-token-002"}
        self.assertEqual(plan({**commit, "epoch_status": "COMPLETED", "current_page_token": None})["stop_reason"], "epoch_completed")
        self.assertEqual(plan(commit, action={"kind": "member"})["stop_reason"], "first_member_page_committed")
        self.assertEqual(plan(commit, state=page_state(max_pages=1))["stop_reason"], "page_budget_reached")
        continuing = plan(commit, action={"kind": "rejection"})
        self.assertEqual((continuing["continue_paging"], continuing["request_page_token"], continuing["expected_epoch_state_version"], continuing["pages_fetched"]), (True, "page-token-002", 2, 1))
        self.assertTrue(plan(commit, action={"kind": "member"}, state=page_state(admission_mode="continuous"))["continue_paging"])
        result = self.run_code("Plan next page", {**commit, "page_state": "OPEN"}, nodes={"Canonicalize source page": {"state": page_state(), "action": None}})
        self.assertIn("source_page_commit_invalid", result["error"])

    def test_page_token_expression_resolves_first_page_and_continuation(self):
        page_token = next(item for item in self.forms_http["parameters"]["queryParameters"]["parameters"] if item["name"] == "pageToken")
        self.assertEqual(page_token["value"], PAGE_TOKEN_EXPRESSION)
        expression = page_token["value"][len("={{"):-len("}}")]
        for token, expected_type, expected_value in ((None, "undefined", None), ("", "undefined", None), ("page-token-002", "string", "page-token-002")):
            with self.subTest(token=token):
                payload = {"expression": expression, "json": {"request_page_token": token}}
                proc = subprocess.run(["node", "-e", EXPRESSION_RUNNER], input=json.dumps(payload), capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                self.assertEqual((result["type"], result["value"]), (expected_type, expected_value))

    # --- mailer producer/consumer compatibility ---------------------------

    def test_mailer_accepts_actual_gateway_claim_and_rejects_tampering(self):
        sys.path.insert(0, str(ROOT / "member_gateway/src"))
        try:
            from xb_member_gateway.models import WelcomeEmailOutbox, WelcomeEmailState
            from xb_member_gateway.notifications import build_welcome_message, claim_envelope, welcome_message_hash
        finally:
            sys.path.pop(0)
        message = build_welcome_message("mailer-synthetic@example.test")
        outbox = WelcomeEmailOutbox(
            outbox_id="welcome-" + "1" * 32, job_id="job-" + "2" * 32, response_id="private-response", source_response_ref="hmac-v1:" + "3" * 64,
            template_id="welcome_v1", recipient=message["to"], message_hash=welcome_message_hash(message), state=WelcomeEmailState.LEASED,
            state_version=1, attempt=1, max_attempts=3, lease_id="lease-" + "4" * 32, lease_expires_at="2026-09-20T01:04:00Z",
            next_attempt_at=None, send_intent_at=None, last_error_code=None, created_at="2026-09-20T01:00:00Z", updated_at="2026-09-20T01:02:00Z",
        )
        claim = {"schema_version": "xb.member.welcome_email.job.v1", "claimed": True, "job": claim_envelope(outbox, message)}
        mailer = json.loads(MAILER.read_text(encoding="utf-8"))
        accepted = self.run_code("Validate claimed welcome message", claim, workflow=mailer)
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["result"][0]["json"]["message"], message)
        idle = self.run_code("Validate claimed welcome message", {"schema_version": "xb.member.welcome_email.job.v1", "claimed": False, "job": None}, workflow=mailer)
        self.assertEqual(idle["result"][0]["json"], {"has_job": False})
        for label, mutate, code in (
            ("recipient", lambda job: job["message"].update({"to": "attacker@example.test"}), "welcome_message_hash_mismatch"),
            ("subject", lambda job: job["message"].update({"subject": "Changed"}), "welcome_message_hash_mismatch"),
            ("reply-to", lambda job: job["message"].update({"reply_to": "reply@example.test"}), "welcome_message_binding_invalid"),
            ("extra-field", lambda job: job["message"].update({"bcc": "x@example.test"}), "welcome_message_shape_invalid"),
            ("header-injection", lambda job: job["message"].update({"subject": "Hi\r\nBcc: x@example.test"}), "welcome_message_field_invalid"),
            ("template", lambda job: job.update({"template_id": "welcome_v2"}), "welcome_job_invalid"),
        ):
            with self.subTest(label=label):
                tampered = copy.deepcopy(claim)
                mutate(tampered["job"])
                result = self.run_code("Validate claimed welcome message", tampered, workflow=mailer)
                self.assertFalse(result["ok"])
                self.assertIn(code, result["error"])


class MemberGatewayN8nCanonicalContractTests(unittest.TestCase):
    """Graph, retry, timeout, MCP-exposure and fail-closed regressions. No node runtime required."""

    HTTP_NODES = {
        "Begin or resume scan epoch": (3, 1000),
        "Restart crashed scan epoch": None,
        "Google Forms single page (configured outside repo)": (3, 2000),
        "Restart invalidated scan epoch": None,
        "Open page durably": None,
        "Protected XB Gateway ingest (configured outside repo)": (2, 2000),
        "Record PII-free source rejection": (2, 2000),
        "Commit durable page checkpoint": None,
    }
    RETRY_FIELDS = ("retryOnFail", "maxTries", "waitBetweenTries")
    DOMAIN_PATTERN = re.compile(r"\b[a-z0-9-]+\.(?:com|sg|net|org|io|dev|app|googleapis)\b", re.IGNORECASE)
    OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{25,}")
    SETTINGS = {"executionOrder": "v1", "saveManualExecutions": False, "saveDataErrorExecution": "none", "saveDataSuccessExecution": "none", "availableInMCP": False}

    def setUp(self):
        self.workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        self.nodes = {node["name"]: node for node in self.workflow["nodes"]}
        self.mailer = json.loads(MAILER.read_text(encoding="utf-8"))
        self.mailer_nodes = {node["name"]: node for node in self.mailer["nodes"]}

    def edges(self, workflow, name, output=0):
        return [edge["node"] for edge in workflow["connections"].get(name, {}).get("main", [[]] * (output + 1))[output]]

    def test_http_nodes_timeouts_and_retry_policy(self):
        http_names = {node["name"] for node in self.workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"}
        self.assertEqual(http_names, set(self.HTTP_NODES))
        for name, retry in self.HTTP_NODES.items():
            with self.subTest(node=name):
                node = self.nodes[name]
                self.assertEqual(node["parameters"]["options"]["timeout"], 30000)
                if retry is None:
                    # CAS restart/open/commit never retry automatically.
                    for field in self.RETRY_FIELDS:
                        self.assertNotIn(field, node)
                else:
                    self.assertEqual((node["retryOnFail"], node["maxTries"], node["waitBetweenTries"]), (True, *retry))

    def test_bounded_manual_page_loop_graph(self):
        wf = self.workflow
        self.assertEqual(self.edges(wf, "Controlled activation gate", 0), ["Begin or resume scan epoch"])
        self.assertEqual(self.edges(wf, "Controlled activation gate", 1), ["Blocked until separate activation"])
        self.assertEqual(self.edges(wf, "Begin or resume scan epoch"), ["Plan epoch attempt"])
        self.assertEqual(self.edges(wf, "Crashed page requires epoch restart", 0), ["Restart crashed scan epoch"])
        self.assertEqual(self.edges(wf, "Restart crashed scan epoch"), ["Plan epoch attempt"])
        self.assertEqual(self.edges(wf, "Epoch may fetch a page", 0), ["Prepare page request"])
        self.assertEqual(self.edges(wf, "Prepare page request"), ["Google Forms single page (configured outside repo)"])
        self.assertEqual(self.edges(wf, "Google Forms single page (configured outside repo)"), ["Canonicalize source page"])
        self.assertEqual(self.edges(wf, "Page token invalidated", 0), ["Restart invalidated scan epoch"])
        self.assertEqual(self.edges(wf, "Page token invalidated", 1), ["Open page durably"])
        self.assertEqual(self.edges(wf, "Restart invalidated scan epoch"), ["Plan epoch attempt"])
        # Every item path reaches COMMIT only after OPEN and the item's receipt call.
        self.assertEqual(self.edges(wf, "Open page durably"), ["Page item is a valid member"])
        self.assertEqual(self.edges(wf, "Page item is a valid member", 0), ["Protected XB Gateway ingest (configured outside repo)"])
        self.assertEqual(self.edges(wf, "Protected XB Gateway ingest (configured outside repo)"), ["Commit durable page checkpoint"])
        self.assertEqual(self.edges(wf, "Page item is a customer rejection", 0), ["Record PII-free source rejection"])
        self.assertEqual(self.edges(wf, "Page item is a customer rejection", 1), ["Commit durable page checkpoint"])
        self.assertEqual(self.edges(wf, "Record PII-free source rejection"), ["Commit durable page checkpoint"])
        self.assertEqual(self.edges(wf, "Commit durable page checkpoint"), ["Plan next page"])
        self.assertEqual(self.edges(wf, "Continue bounded paging", 0), ["Prepare page request"])
        self.assertEqual(self.edges(wf, "Continue bounded paging", 1), ["Source run finished"])
        for source, targets in (("Canonicalize source page", ["Page token invalidated"]),):
            self.assertEqual(self.edges(wf, source), targets)
        self.assertNotIn("pagination", self.nodes["Google Forms single page (configured outside repo)"]["parameters"]["options"])

    def test_forms_query_uses_the_verbatim_fixed_filter_and_page_size_one(self):
        forms = self.nodes["Google Forms single page (configured outside repo)"]
        parameters = {item["name"]: item["value"] for item in forms["parameters"]["queryParameters"]["parameters"]}
        self.assertEqual(parameters, {"pageSize": "1", "filter": "={{ $json.filter_exact }}", "pageToken": PAGE_TOKEN_EXPRESSION})
        self.assertIs(forms["parameters"]["options"]["response"]["response"]["neverError"], True)
        self.assertIs(forms["parameters"]["options"]["response"]["response"]["fullResponse"], True)
        begin = self.nodes["Begin or resume scan epoch"]["parameters"]
        self.assertIn("/v1/source/epochs/begin", begin["url"])
        self.assertIn("/pages/open", self.nodes["Open page durably"]["parameters"]["url"])
        self.assertIn("/commit", self.nodes["Commit durable page checkpoint"]["parameters"]["url"])
        self.assertIn("ambiguous_crashed_attempt", self.nodes["Restart crashed scan epoch"]["parameters"]["jsonBody"])
        self.assertIn("token_invalidated", self.nodes["Restart invalidated scan epoch"]["parameters"]["jsonBody"])

    def test_exports_are_inactive_manual_credential_free_and_mcp_disabled(self):
        for label, workflow, activation_node in (("forms", self.workflow, "Repository-safe source configuration"), ("mailer", self.mailer, "Repository-safe mailer configuration")):
            with self.subTest(workflow=label):
                self.assertIs(workflow["active"], False)
                self.assertIsNone(workflow["staticData"])
                self.assertNotIn("pinData", workflow)
                self.assertEqual(workflow["settings"], self.SETTINGS)
                triggers = {node["type"] for node in workflow["nodes"] if node["type"].endswith("Trigger")}
                self.assertEqual(triggers, {"n8n-nodes-base.manualTrigger"})
                for item in walk(workflow):
                    self.assertNotIn("webhookId", item)
                    self.assertNotIn("credentials", item)
                    self.assertNotIn("authentication", item)
                node = {item["name"]: item for item in workflow["nodes"]}[activation_node]
                activation = next(item for item in node["parameters"]["assignments"]["assignments"] if item["name"] == "activation_enabled")
                self.assertIs(activation["value"], False)
        raw = WORKFLOW.read_text(encoding="utf-8")
        for placeholder in ("GOOGLE_FORM_ID_PLACEHOLDER", "QUESTION_ID_NAME_PLACEHOLDER", "QUESTION_ID_PHONE_PLACEHOLDER"):
            self.assertIn(placeholder, raw)
        self.assertNotIn("81234567", raw)
        self.assertNotIn("@gmail", raw)

    def test_blocked_activation_branches_reach_no_network_node(self):
        for workflow, gate, blocked_name in ((self.workflow, "Controlled activation gate", "Blocked until separate activation"), (self.mailer, "Controlled mailer activation gate", "Blocked until separate mailer activation")):
            nodes = {node["name"]: node for node in workflow["nodes"]}
            blocked = workflow["connections"][gate]["main"][1]
            self.assertEqual([edge["node"] for edge in blocked], [blocked_name])
            self.assertNotIn(blocked_name, workflow["connections"])
            self.assertNotEqual(nodes[blocked_name]["type"], "n8n-nodes-base.httpRequest")

    def test_descriptions_state_do_not_activate_without_live_identifiers(self):
        for workflow in (self.workflow, self.mailer):
            description = workflow["description"]
            self.assertIn("DO NOT ACTIVATE", description)
            self.assertNotIn("http", description.lower())
            self.assertNotIn("@", description)
            self.assertEqual(self.DOMAIN_PATTERN.findall(description), [])
            self.assertEqual(self.OPAQUE_ID_PATTERN.findall(description), [])
            for marker in ("bearer", "api_key", "apikey", "password", "private key", "token="):
                self.assertNotIn(marker, description.lower())

    def test_mailer_send_email_has_no_retry_attribution_reply_to_or_literal_sender(self):
        send = self.mailer_nodes["Send welcome email (SMTP configured outside repo)"]
        self.assertEqual(send["type"], "n8n-nodes-base.emailSend")
        self.assertIs(send["retryOnFail"], False)
        self.assertNotIn("maxTries", send)
        self.assertEqual(send["onError"], "continueErrorOutput")
        self.assertEqual(send["parameters"]["options"], {"appendAttribution": False})
        self.assertNotIn("replyTo", json.dumps(send))
        self.assertEqual(send["parameters"]["emailFormat"], "text")
        for field in ("fromEmail", "toEmail", "subject", "text"):
            self.assertTrue(send["parameters"][field].startswith("={{"), field)
        raw = MAILER.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", raw))
        self.assertNotIn("x-boundaries.com", raw)
        # Success records SMTP acceptance; the error output records an
        # uncertain outcome. Neither path can resend.
        self.assertEqual(self.edges(self.mailer, "Send welcome email (SMTP configured outside repo)", 0), ["Record SMTP acceptance"])
        self.assertEqual(self.edges(self.mailer, "Send welcome email (SMTP configured outside repo)", 1), ["Record uncertain delivery outcome"])
        self.assertIn("smtp_accepted", self.mailer_nodes["Record SMTP acceptance"]["parameters"]["jsonBody"])
        self.assertIn("delivery_outcome_uncertain", self.mailer_nodes["Record uncertain delivery outcome"]["parameters"]["jsonBody"])
        self.assertEqual(self.edges(self.mailer, "Record welcome send intent"), ["Send welcome email (SMTP configured outside repo)"])
        self.assertEqual(self.edges(self.mailer, "Welcome email claimed", 0), ["Record welcome send intent"])
        for name in ("Claim one welcome email", "Record welcome send intent", "Record SMTP acceptance", "Record uncertain delivery outcome"):
            node = self.mailer_nodes[name]
            self.assertEqual((node["retryOnFail"], node["maxTries"], node["waitBetweenTries"]), (True, 3, 5000))
            self.assertEqual(node["parameters"]["options"]["timeout"], 30000)
        http_names = {node["name"] for node in self.mailer["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"}
        self.assertEqual(http_names, {"Claim one welcome email", "Record welcome send intent", "Record SMTP acceptance", "Record uncertain delivery outcome"})
        for node in self.mailer["nodes"]:
            if node["type"] == "n8n-nodes-base.httpRequest":
                self.assertIn("/v1/welcome-emails/", node["parameters"]["url"])

    def test_connections_target_declared_nodes(self):
        for workflow in (self.workflow, self.mailer):
            declared = {node["name"] for node in workflow["nodes"]}
            for branches in workflow["connections"].values():
                for branch in branches.get("main", []):
                    for edge in branch:
                        self.assertIn(edge["node"], declared)


if __name__ == "__main__":
    unittest.main()
