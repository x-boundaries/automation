"""Offline security regressions for the bounded welcome-email mailer importer.

The mailer importer is derived from the reviewed forms importer and shares its
custody, ACL, canonical JSON, plan/apply identity, dispatch-ownership and
receipt machinery. This module therefore re-runs the complete generic forms
importer battery against the mailer importer (by subclassing it with the
mailer's script, export, template and private root), disables only the cases
that exercise the Forms cursor/endpoint contract, and adds mailer-specific
regressions: one mailer bearer role, one SMTP role, and the Send Email
no-retry / no-attribution / no-Reply-To / no-literal-address posture.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "_forms_bounded_import_security_base", Path(__file__).with_name("test_member_gateway_bounded_import_security.py")
)
_forms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_forms)
_write_json = _forms._write_json
_read_json = _forms._read_json

MAILER_NODES = ["Claim one welcome email", "Record welcome send intent", "Record SMTP acceptance", "Record uncertain delivery outcome"]
SEND = "Send welcome email (SMTP configured outside repo)"


class MemberWelcomeEmailBoundedImportSecurityTests(_forms.MemberGatewayBoundedImportSecurityTests):
    SCRIPT_PATH = ROOT / "n8n-workflows/scripts/import-member-welcome-email-bounded.ps1"
    WORKFLOW_PATH = ROOT / "n8n-workflows/member_welcome_email_outbox.workflow.json"
    TEMPLATE_PATH = ROOT / "config/member_welcome_email_bounded_import.v1.template.json"
    PRIVATE_ROOT = ".n8n-local/member-welcome-email-bounded-import"
    FIXTURE_SCHEMA = "xb.member.welcome_email.bounded_import.fixture.v1"
    WORKFLOW_NAME = "Member Gateway - welcome email outbox mailer (inactive)"
    CREDENTIAL_PROBE = (SEND, "smtp")
    EVIDENCE_STATE_FILE = "gateway-binding-state.json"
    IDENTITY_TAMPER = ("gateway_binding_digest", "0" * 64)
    SMTP_ID = "smtp-credential-id-private-4411"
    SMTP_NAME = "smtp_private_credential_name"

    def _change_preparation_input(self, manifest: dict[str, Any], label: str) -> None:
        manifest["credential_roles"]["smtp"]["credential_name"] = f"smtp-{label}-changed-002"

    def _make_manifest(self) -> dict[str, Any]:
        manifest = copy.deepcopy(_read_json(self.TEMPLATE_PATH))
        manifest["project"]["id"] = "project-security-001"
        manifest["workflow"]["id"] = "workflow-security-001"
        manifest["endpoints"]["gateway_origin"] = self.TEST_GATEWAY_ORIGIN
        manifest["security"]["approved_gateway_origin"] = self.TEST_GATEWAY_ORIGIN
        mailer = manifest["credential_roles"]["gateway_mailer_bearer"]
        mailer["credential_id"] = "mailer-credential-id-001"
        mailer["credential_name"] = "mailer_test_credential"
        smtp = manifest["credential_roles"]["smtp"]
        smtp["credential_id"] = self.SMTP_ID
        smtp["credential_name"] = self.SMTP_NAME
        return manifest

    def _make_cursor(self) -> None:
        # The mailer importer never reads the gateway; the fixture carries no cursor.
        return None

    # Forms-only cursor and endpoint contract cases do not apply to the mailer.
    test_cursor_identity_cutover_and_preimage_mismatch_block_before_dispatch = None
    test_cursor_digest_and_state_version_references_block_before_dispatch = None
    test_endpoint_and_credential_relationships_fail_closed = None
    test_prepared_transport_bindings_and_process_contract_are_explicit = None
    test_cursor_request_binds_required_parameters_without_overlap = None
    test_cursor_v2_shape_and_cutover_binding_fail_closed = None
    test_gateway_bearer_binds_exactly_the_seven_gateway_nodes = None
    test_private_tokens_and_response_ids_never_reach_evidence_or_output = None

    def _expect_plan_failure(self, label: str, expected: str, *, mutate_manifest=None, mutate_preimage=None) -> None:
        operation_id = self._operation_id(label)
        self.manifest = self._make_manifest()
        if mutate_manifest:
            mutate_manifest(self.manifest)
        _write_json(self.manifest_path, self.manifest)
        fixture = self._make_fixture()
        if mutate_preimage:
            mutate_preimage(fixture["workflow"])
            _write_json(self.fixture_path, fixture)
        completed = self._run("CapturePlan", operation_id, expect_success=False)
        self.assertIn(expected, completed.stderr)
        self.assertFalse((self._operation_path(operation_id) / "plan.json").exists())

    def test_preimage_and_gateway_binding_mismatch_block_before_dispatch(self) -> None:
        operation_id, fixture = self._capture("preimage")
        fixture["workflow"]["description"] = "tampered preimage"
        _write_json(self.fixture_path, fixture)
        self._run("Apply", operation_id, expect_success=False)
        self.assertFalse((self._operation_path(operation_id) / "dispatch-receipt.json").exists())

        operation_id, _ = self._capture("gateway-binding-digest")
        state_path = self._operation_path(operation_id) / self.EVIDENCE_STATE_FILE
        state = _read_json(state_path)
        state["gateway_binding_digest"] = "0" * 64
        _write_json(state_path, state)
        completed = self._run("Apply", operation_id, expect_success=False)
        self.assertIn("gateway_binding_digest", completed.stderr)
        self.assertFalse((self._operation_path(operation_id) / "dispatch-receipt.json").exists())

    def test_mailer_manifest_contract_fails_closed(self) -> None:
        def roles(manifest):
            return manifest["credential_roles"]
        cases = (
            ("smtp-type", lambda m: roles(m)["smtp"].update({"credential_type": "httpBearerAuth"}), "binding_credential_type_invalid"),
            ("bearer-type", lambda m: roles(m)["gateway_mailer_bearer"].update({"credential_type": "smtp"}), "binding_credential_type_invalid"),
            ("smtp-node", lambda m: roles(m)["smtp"].update({"node_names": ["Claim one welcome email"]}), "binding_credential_nodes_invalid"),
            ("bearer-missing-node", lambda m: roles(m)["gateway_mailer_bearer"]["node_names"].pop(), "binding_credential_nodes_invalid"),
            ("bearer-reordered", lambda m: roles(m)["gateway_mailer_bearer"]["node_names"].reverse(), "binding_credential_nodes_invalid"),
            ("bearer-includes-send", lambda m: roles(m)["gateway_mailer_bearer"]["node_names"].append(SEND), "binding_credential_nodes_invalid"),
            ("token-env", lambda m: m["security"].update({"mailer_token_env": "XB_MEMBER_GATEWAY_SOURCE_TOKEN"}), "binding_security_invalid"),
            ("origin-mismatch", lambda m: m["endpoints"].update({"gateway_origin": "https://other.gateway.internal:443"}), "binding_gateway_origin_invalid"),
            ("origin-path", lambda m: m["endpoints"].update({"gateway_origin": self.TEST_GATEWAY_ORIGIN + "/v1"}), "binding_endpoint_invalid"),
            ("forms-keys", lambda m: m.update({"question_mapping": {}}), "binding_shape_invalid"),
            ("schema", lambda m: m.update({"schema_version": "xb.member.gateway.bounded_import.binding.v2"}), "binding_schema_invalid"),
            ("smtp-unresolved", lambda m: roles(m)["smtp"].update({"credential_id": "SMTP_CREDENTIAL_ID_PLACEHOLDER"}), "binding_credential_reference_unresolved"),
            ("workflow-name", lambda m: m["workflow"].update({"name": "Member Gateway - welcome email outbox mailer (active)"}), "binding_workflow_name_invalid"),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                self._expect_plan_failure(label, expected, mutate_manifest=mutate)

    def test_send_email_and_trigger_posture_refusals(self) -> None:
        def send(workflow):
            return next(node for node in workflow["nodes"] if node["name"] == SEND)
        cases = (
            ("retry-enabled", lambda w: send(w).update({"retryOnFail": True, "maxTries": 3}), "send_email_retry_enabled"),
            ("retry-unset", lambda w: send(w).pop("retryOnFail"), "send_email_retry_enabled"),
            ("attribution", lambda w: send(w)["parameters"]["options"].update({"appendAttribution": True}), "send_email_attribution_enabled"),
            ("reply-to", lambda w: send(w)["parameters"]["options"].update({"replyTo": "={{ 'x' }}"}), "send_email_attribution_enabled"),
            ("literal-sender", lambda w: send(w)["parameters"].update({"fromEmail": "sender@example.test"}), "send_email_literal_message_field"),
            ("literal-address-expression", lambda w: send(w)["parameters"].update({"toEmail": "={{ 'someone@example.test' }}"}), "send_email_literal_address"),
            ("error-route", lambda w: send(w).pop("onError"), "send_email_error_route_invalid"),
            ("activation", lambda w: next(n for n in w["nodes"] if n["name"] == "Repository-safe mailer configuration")["parameters"]["assignments"]["assignments"][0].update({"value": True}), "activation_enabled"),
            ("active", lambda w: w.update({"active": True}), "workflow_active"),
            ("mcp", lambda w: w["settings"].update({"availableInMCP": True}), "mcp_exposure_enabled"),
            ("static-data", lambda w: w.update({"staticData": {"lastId": 1}}), "workflow_runtime_state_present"),
            ("schedule", lambda w: w["nodes"].append({"name": "Every minute", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}}), "workflow_trigger_posture_invalid"),
            ("webhook", lambda w: w["nodes"].append({"name": "Inbound", "type": "n8n-nodes-base.webhook", "webhookId": "hook", "parameters": {}}), "workflow_trigger_posture_invalid"),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                self._expect_plan_failure(label, expected, mutate_preimage=mutate)

    def test_prepared_mailer_bindings_are_explicit_and_inactive(self) -> None:
        operation_id, _ = self._capture("mailer-transport")
        prepared_path = self._operation_path(operation_id) / "prepared.workflow.json"
        prepared = _read_json(prepared_path)
        text = prepared_path.read_text(encoding="utf-8")
        self.assertIs(prepared["active"], False)
        self.assertNotIn("gateway.example.com", text)
        nodes = {node["name"]: node for node in prepared["nodes"]}
        config = {item["name"]: item["value"] for item in nodes["Repository-safe mailer configuration"]["parameters"]["assignments"]["assignments"]}
        self.assertEqual((config["gateway_origin"], config["activation_enabled"]), (self.TEST_GATEWAY_ORIGIN, False))
        for name in MAILER_NODES:
            self.assertEqual(nodes[name]["credentials"], {"httpBearerAuth": {"id": "mailer-credential-id-001", "name": "mailer_test_credential"}})
            self.assertEqual((nodes[name]["parameters"]["authentication"], nodes[name]["parameters"]["genericAuthType"]), ("genericCredentialType", "httpBearerAuth"))
        self.assertEqual(nodes[SEND]["credentials"], {"smtp": {"id": self.SMTP_ID, "name": self.SMTP_NAME}})
        self.assertIs(nodes[SEND]["retryOnFail"], False)
        self.assertEqual(nodes[SEND]["parameters"]["options"], {"appendAttribution": False})
        credentialed = {name for name, node in nodes.items() if "credentials" in node}
        self.assertEqual(credentialed, set(MAILER_NODES) | {SEND})
        binding_state = _read_json(self._operation_path(operation_id) / self.EVIDENCE_STATE_FILE)
        self.assertEqual(binding_state["gateway_binding"], {
            "schema_version": "xb.member.welcome_email.bounded_import.gateway-binding.v1",
            "gateway_origin": self.TEST_GATEWAY_ORIGIN, "mailer_token_env": "XB_MEMBER_GATEWAY_MAILER_TOKEN", "template_id": "welcome_v1",
        })
        source = self.SCRIPT_PATH.read_text(encoding="utf-8")
        for forbidden in ("Invoke-WebRequest", "/v1/welcome-emails/claim", "Send-MailMessage", "execute:workflow"):
            self.assertNotIn(forbidden, source)

    def test_credentials_and_addresses_never_reach_console_output(self) -> None:
        operation_id, _ = self._capture("mailer-non-disclosure")
        completed = self._run("Apply", operation_id)
        self.assertEqual(json.loads(completed.stdout.strip().splitlines()[-1])["status"], "applied_and_verified")
        streams = (completed.stdout or "") + (completed.stderr or "")
        for marker in (self.SMTP_ID, self.SMTP_NAME, "mailer-credential-id-001", "noreply@", "@x-boundaries"):
            self.assertNotIn(marker, streams)
        for path in self._operation_path(operation_id).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"x-boundaries.com", path.read_bytes(), path.name)


if __name__ == "__main__":
    import unittest

    unittest.main()
