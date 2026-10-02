"""X-CC: member-write v2 cross-contract round trip (W-G2-149 section 7.1).

claim v2 (gateway) -> primitive request -> primitive result -> result v2
(worker) -> gateway validator, state and welcome outbox.

The worker/primitive half is proven by the golden fixture
``tests/fixtures/ac2_member_primitive/cross_contract_cases.v1.fixture``, which the
PowerShell tests produce by running the real worker cycle and the real
primitive against the in-memory AutoCount double. This module checks both
halves against the two frozen wire schemas and then replays every fixture
result through the real gateway. Offline and synthetic only.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "member_gateway/src"))
sys.path.insert(0, str(ROOT / "member_gateway/tests"))

CLAIM_SCHEMA = json.loads((ROOT / "schemas/member_gateway_worker_claim.v2.schema.json").read_text(encoding="utf-8"))
RESULT_SCHEMA = json.loads((ROOT / "schemas/member_gateway_result.v2.schema.json").read_text(encoding="utf-8"))
FIXTURE = json.loads((ROOT / "tests/fixtures/ac2_member_primitive/cross_contract_cases.v1.fixture").read_text(encoding="utf-8"))
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")

# Expected gateway disposition per fixture case: (state, welcome outbox rows).
EXPECTED = {
    "R1_create_base": ("CREATED_VERIFIED", 1),
    "R4_create_name_appended": ("CREATED_VERIFIED", 1),
    "R2c_link_existing": ("LINKED_EXISTING", 0),
    "R0_prior_attempt": ("CREATED_VERIFIED", 1),
    "R0_final_proof_rejected": ("MANUAL_REVIEW", 0),
    "R0_final_field_mismatch": ("MANUAL_REVIEW", 0),
    "MUTEX_BUSY": ("RETRY_WAIT", 0),
}


def _type_ok(value, expected) -> bool:
    kinds = expected if isinstance(expected, list) else [expected]
    for kind in kinds:
        if kind == "null" and value is None:
            return True
        if kind == "boolean" and isinstance(value, bool):
            return True
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if kind == "string" and isinstance(value, str):
            return True
        if kind == "object" and isinstance(value, dict):
            return True
        if kind == "array" and isinstance(value, list):
            return True
    return False


def schema_errors(value, schema, path="$") -> list[str]:
    """Minimal JSON Schema (2020-12 subset) validator for the frozen schemas.

    Supports exactly the keywords those two files use; an unknown keyword is
    itself an error so the validator cannot silently under-check."""

    supported = {
        "$schema", "$id", "title", "description", "type", "const", "enum", "pattern", "minLength", "maxLength",
        "minimum", "required", "additionalProperties", "properties", "oneOf", "items", "uniqueItems", "maxItems",
        "format",
    }
    unknown = set(schema) - supported
    if unknown:
        return [f"{path}: unsupported schema keywords {sorted(unknown)}"]
    if "oneOf" in schema:
        matches = [option for option in schema["oneOf"] if not schema_errors(value, option, path)]
        return [] if len(matches) == 1 else [f"{path}: oneOf matched {len(matches)}"]
    errors: list[str] = []
    if "type" in schema and not _type_ok(value, schema["type"]):
        return [f"{path}: type {type(value).__name__} not {schema['type']}"]
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        errors.append(f"{path}: const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: enum")
    if isinstance(value, str):
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: pattern")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: maxLength")
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: minimum")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in set(value) - set(properties):
                errors.append(f"{path}: additional {key}")
        for key, sub in properties.items():
            if key in value:
                errors.extend(schema_errors(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: maxItems")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            errors.append(f"{path}: uniqueItems")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


class SchemaValidatorSelfTests(unittest.TestCase):
    """The minimal validator must reject the defects it exists to catch."""

    def test_rejects_extra_missing_enum_pattern_and_type_defects(self):
        case = FIXTURE["cases"][0]
        self.assertEqual(schema_errors(case["result"], RESULT_SCHEMA), [])
        for mutate in (
            lambda body: body.update(extra=1),
            lambda body: body.pop("rule"),
            lambda body: body.update(outcome="CREATED"),
            lambda body: body.update(lease_id="lease-XYZ"),
            lambda body: body.update(save_invocation_count=2),
            lambda body: body.update(save_invoked=1),
            lambda body: body.update(reason_code="not_a_reason"),
            lambda body: body.update(dq_flags=["email_seen_on_other_member", "email_seen_on_other_member"]),
            lambda body: body["primitive"].update(rule_version="XB-MN-2"),
        ):
            body = copy.deepcopy(case["result"])
            mutate(body)
            self.assertNotEqual(schema_errors(body, RESULT_SCHEMA), [], body)
        claim = copy.deepcopy(case["claim"])
        claim["request"]["base_member_no"] = "12345"
        self.assertNotEqual(schema_errors(claim, CLAIM_SCHEMA), [])


class FixtureSchemaTests(unittest.TestCase):
    def test_fixture_covers_the_required_rules(self):
        self.assertEqual(FIXTURE["schema_version"], "xb.ac2.member_primitive.cross_contract_cases.v1")
        self.assertEqual({case["name"] for case in FIXTURE["cases"]}, set(EXPECTED))

    def test_every_worker_claim_and_result_matches_the_frozen_schemas(self):
        from xb_member_gateway.results import validate_result_v2

        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(schema_errors(case["claim"], CLAIM_SCHEMA), [])
                self.assertEqual(schema_errors(case["result"], RESULT_SCHEMA), [])
                validate_result_v2(case["result"])
                # The result is bound to the claim the worker consumed.
                for key in ("job_id", "attempt_no", "lease_id", "state_version"):
                    self.assertEqual(case["result"][key], case["claim"][key])


class GatewayRoundTripTests(unittest.TestCase):
    """Gateway-produced claims feed the primitive contract; the primitive's
    results drive the gateway to the contract state and outbox."""

    def setUp(self):
        from v2_support import NOW, SESSION, headers, later, make_app, make_repository, make_service, outcome_body, policy, source_event

        self.NOW, self.SESSION, self.headers, self.later = NOW, SESSION, headers, later
        self.outcome_body, self.policy, self.source_event = outcome_body, policy, source_event
        self.repository = make_repository()
        self.service = make_service(self.repository)
        self.app = make_app(self.service)

    def _ingest_like(self, request: dict):
        month = MONTHS[int(request["DOB"][5:7]) - 1]
        event = self.source_event(
            "xcc-response-001", name=request["name"], email=request["email"], phone=request["phone"], birthday_month=month
        )
        return self.repository.ingest_source_event(event, policy=self.policy(), now=self.NOW, initial_window_max=None).job

    def _claim(self, at):
        self.service.clock = at
        claim = self.service.claim(self.SESSION)
        self.assertTrue(claim["claimed"], claim)
        return claim

    def _post(self, claim, body, at):
        self.service.clock = at
        return self.app.handle("POST", f"/v2/jobs/{claim['job_id']}/result", headers=self.headers("worker"), body=body)

    def test_idle_claim_matches_the_frozen_claim_schema(self):
        claim = self.service.claim(self.SESSION)
        self.assertFalse(claim["claimed"])
        self.assertEqual(schema_errors(claim, CLAIM_SCHEMA), [])

    def test_each_fixture_result_round_trips_through_the_gateway(self):
        from xb_member_gateway.models import JobState

        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                self.setUp()
                fixture_request = case["claim"]["request"]
                job = self._ingest_like(fixture_request)
                at = self.NOW
                claim = self._claim(at)
                if case["claim"]["attempt_no"] == 2:
                    first = self._post(claim, self.outcome_body(claim, "OUTCOME_UNCERTAIN"), at)
                    self.assertEqual(first.status, 200, first.body)
                    at = self.later(6)
                    claim = self._claim(at)
                self.assertEqual(claim["attempt_no"], case["claim"]["attempt_no"])
                # Gateway claim satisfies the frozen schema and carries the
                # exact identity the primitive was given (computed once).
                self.assertEqual(schema_errors(claim, CLAIM_SCHEMA), [])
                self.assertEqual(set(claim["request"]), set(fixture_request))
                for key in ("rule", "base_member_no", "name_component", "name", "email", "MemberType", "DOB", "OpeningPoints", "IsActive", "Individual"):
                    self.assertEqual(claim["request"][key], fixture_request[key], key)
                # The primitive checks digits(phone) == base; the gateway sends
                # the canonical digits.
                self.assertEqual(re.sub(r"[^0-9]", "", claim["request"]["phone"]), fixture_request["base_member_no"])
                body = copy.deepcopy(case["result"])
                for key in ("job_id", "attempt_no", "lease_id", "state_version"):
                    body[key] = claim[key]
                response = self._post(claim, body, at)
                self.assertEqual(response.status, 200, response.body)
                state, outbox_rows = EXPECTED[case["name"]]
                self.assertEqual(response.body["state"], state)
                stored = self.repository.get_job(job.job_id)
                self.assertEqual(stored.state, JobState(state))
                self.assertEqual(len(self.repository.welcome_outboxes()), outbox_rows)
                if case["name"].startswith("R0_final_"):
                    self.assertEqual(case["result"]["outcome"], "MANUAL_REVIEW")
                    self.assertEqual(case["result"]["reason_code"], "prior_attempt_ambiguous")
                    self.assertFalse(case["result"]["save_invoked"])
                    self.assertEqual(case["result"]["save_invocation_count"], 0)
                    self.assertIsNone(case["result"]["member_no"])
                    self.assertIsNone(case["result"]["member_guid"])
                    self.assertIsNone(case["result"]["readback"])
                    self.assertIsNone(stored.next_attempt_at)
                    self.assertIsNone(self.repository.member_outcome(job.job_id))
                    self.assertFalse(self.service.claim(self.SESSION)["claimed"])
                elif case["name"] == "R0_prior_attempt":
                    credited = self.repository.member_outcome(job.job_id)
                    self.assertIsNotNone(credited)
                    self.assertEqual(credited.outcome, "CREATED_VERIFIED")
                    self.assertEqual(credited.member_guid, case["result"]["member_guid"])
                    self.assertIsNotNone(self.repository.welcome_outbox_for_job(job.job_id))
                state_version = stored.state_version
                replay = self._post(claim, copy.deepcopy(body), at)
                self.assertEqual((replay.status, replay.body["replayed"]), (200, True))
                self.assertEqual(len(self.repository.welcome_outboxes()), outbox_rows)
                if case["name"].startswith("R0_final_"):
                    replayed_job = self.repository.get_job(job.job_id)
                    self.assertEqual(replayed_job.state, JobState.MANUAL_REVIEW)
                    self.assertEqual(replayed_job.state_version, state_version)
                    self.assertIsNone(replayed_job.next_attempt_at)

    def test_fixture_result_rebound_to_a_stale_lease_changes_nothing(self):
        case = FIXTURE["cases"][0]
        job = self._ingest_like(case["claim"]["request"])
        claim = self._claim(self.NOW)
        stale = copy.deepcopy(case["result"])  # still bound to the fixture lease
        response = self._post(claim, stale, self.NOW)
        self.assertIn(response.status, (400, 409))
        self.assertEqual(self.repository.get_job(job.job_id).state.value, "LEASED")
        self.assertEqual(len(self.repository.welcome_outboxes()), 0)


if __name__ == "__main__":
    unittest.main()
