import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "autocount2-automation"
README = ROOT / "README.md"
WORKFLOW_DOC = DOCS / "member_intake_n8n_dry_run_workflow.md"
UAT_PLAN = DOCS / "member_intake_n8n_lookup_bridge_uat_plan.md"
UAT_SETUP_RUNBOOK = DOCS / "member_intake_n8n_uat_setup_runbook.md"
GATE2_RUNBOOK = DOCS / "member_intake_n8n_gate2_dummy_rehearsal_runbook.md"
NODE_CONTRACT = DOCS / "member_intake_n8n_node_contract.md"
DIRECT_RUNBOOK = DOCS / "member_intake_n8n_direct_lookup_runbook.md"
LOOKUP_RUNBOOK = DOCS / "member_lookup_review_runbook.md"
BRIDGE_DESIGN = DOCS / "member_intake_local_bridge_design.md"
BRIDGE_RUNBOOK = DOCS / "member_intake_local_lookup_bridge_runbook.md"
DECISION_RUNBOOK = DOCS / "member_intake_decision_review_runbook.md"
TEMPLATE_PATH = DOCS / "templates" / "member_intake_n8n_dry_run_lookup.reference.json"


DRY_RUN_DOCS = [WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK, UAT_SETUP_RUNBOOK, GATE2_RUNBOOK]
FORBIDDEN_WRITE_TOKENS = [
    "Save" + "Member",
    "New" + "Member",
    "Delete" + "Member",
    "GetNext" + "MemberNo",
]


class MemberIntakeN8nDryRunDocsTests(unittest.TestCase):
    def read(self, path):
        return path.read_text(encoding="utf-8")

    def combined(self, paths):
        return "\n".join(self.read(path) for path in paths)

    def gate3_recorded_evidence(self):
        setup = self.read(UAT_SETUP_RUNBOOK)
        match = re.search(
            r"#### Recorded Gate 3 Sanitized Evidence.*?```text\n(?P<body>.*?)\n```",
            setup,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        evidence = {}
        for line in body.splitlines():
            key, value = line.split(" = ", 1)
            evidence[key] = value
        return body, evidence

    def test_new_docs_exist_and_are_linked_from_readme(self):
        readme = self.read(README)

        for path in [WORKFLOW_DOC, UAT_PLAN, UAT_SETUP_RUNBOOK, GATE2_RUNBOOK, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK]:
            self.assertTrue(path.exists(), path)
            self.assertIn(path.name, readme)

    def test_docs_state_cloud_or_non_ac2_n8n_plus_windows_bridge_architecture(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)cloud/VPS/non-AC2 n8n|cloud, VPS, or another non-AC2")
        self.assertRegex(combined, r"(?i)Windows AC2 lookup bridge|local Windows AC2 lookup bridge")
        self.assertRegex(combined, r"(?i)only component allowed to load AutoCount assemblies")
        self.assertRegex(combined, r"(?i)not the final architecture|not the long-term n8n hosting architecture")
        self.assertNotRegex(combined, r"(?i)current runtime direction is local self-hosted n8n")
        self.assertNotRegex(combined, r"(?i)main dry-run runtime path for local self-hosted n8n")

    def test_cloud_n8n_direct_execute_command_to_local_ac2_is_blocked(self):
        combined = self.combined([WORKFLOW_DOC, UAT_PLAN, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)Cloud n8n cannot directly run local AC2 PowerShell")
        self.assertRegex(combined, r"(?i)n8n Execute Command runs on the n8n host/container")
        self.assertRegex(combined, r"(?i)invalid for local AC2 lookup")
        self.assertNotRegex(combined, r"(?i)cloud n8n can directly call local AC2 PowerShell")

    def test_outbound_polling_bridge_is_preferred_and_public_inbound_is_not(self):
        combined = self.combined([WORKFLOW_DOC, UAT_PLAN, NODE_CONTRACT, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)outbound polling|polls outbound|poll outbound")
        self.assertRegex(combined, r"(?i)preferred")
        self.assertRegex(combined, r"(?i)public inbound webhook.*not recommended|not recommended.*public inbound webhook")
        self.assertRegex(combined, r"(?i)separate approval|separately approved")

    def test_cloudflared_is_allowed_only_for_protected_queue_api(self):
        combined = self.combined([UAT_PLAN, NODE_CONTRACT, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        for phrase in [
            "Cloudflare Tunnel / reverse proxy may be used for development and likely integration only for a narrow protected queue/API surface over HTTPS",
            "the queue API may run on the operator local dev PC behind `cloudflared`",
            "expose only sanitized queue/result API operations",
            "must not expose AC2, AutoCount, PowerShell, SQL, RDP, or member create/update/delete/write paths",
            "direct tunnel access to AC2, AutoCount, PowerShell, SQL, RDP, or member write paths remains forbidden",
            "Cloudflare Access/service-token or equivalent machine authentication",
            "rate limits",
            "audit logging",
            "least-privilege request/response schema",
            "documented rollback/disable procedure",
        ]:
            self.assertIn(phrase, combined)

    def test_local_bridge_design_has_no_obsolete_direct_post_or_sync_flow(self):
        bridge_design = self.read(BRIDGE_DESIGN)

        for phrase in [
            "Current Queue/Polling Request Flow",
            "n8n writes sanitized PENDING_LOOKUP jobs to the protected queue/API",
            "AC2 bridge polls the queue/API outbound over HTTPS",
            "AC2 bridge claims one job with state/lease fields",
            "AC2 bridge runs read-only AutoCount lookup locally",
            "AC2 bridge posts sanitized result back",
            "n8n reads/routes sanitized result",
            "PR #90 does not approve direct POST to the AC2 bridge",
            "a `/sync` write endpoint",
        ]:
            self.assertIn(phrase, bridge_design)

        for stale_phrase in [
            "HTTP POST /member-intake/dry-run",
            "HTTP POST /member-intake/sync",
            "/member-intake/dry-run",
            "/member-intake/sync",
            "live sync endpoint is a future design placeholder",
        ]:
            self.assertNotIn(stale_phrase, bridge_design)

    def test_queue_api_outbound_polling_claims_leases_and_cadence_are_documented(self):
        combined = self.combined([UAT_PLAN, NODE_CONTRACT, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        for phrase in [
            "n8n writes sanitized PENDING_LOOKUP jobs to the queue API over HTTPS",
            "AC2 bridge polls the queue API outbound over HTTPS",
            "AC2 bridge claims one job at a time with state/lease fields",
            "AC2 bridge runs read-only AutoCount lookup locally",
            "AC2 bridge posts sanitized result back to the queue API over HTTPS",
            "n8n reads/routes the sanitized result",
            "PENDING_LOOKUP",
            "LOOKUP_IN_PROGRESS",
            "If the `LOOKUP_IN_PROGRESS` lease expires before a result is posted",
            "Retry exhaustion routes to `LOOKUP_ERROR_REVIEW`",
            "Windows Task Scheduler every 1 minute",
            "long-running Windows service/worker polling every 15-60 seconds with idle backoff",
            "Hourly polling is too slow",
        ]:
            self.assertIn(phrase, combined)

    def test_gate3b_still_has_no_queue_api_cloudflared_or_hosted_service_requirement(self):
        bridge_runbook = self.read(BRIDGE_RUNBOOK)

        for phrase in [
            "Gate 3B does not require or use n8n, Google Sheets, hosted n8n, a queue API, Cloudflare Tunnel, `cloudflared`, a hosted service",
            "This is not Gate 4A",
            "not n8n evidence",
            "not Google Sheets evidence",
            "does not approve Gate 4A",
        ]:
            self.assertIn(phrase, bridge_runbook)

    def test_docs_require_base64_member_input_and_explain_it_is_not_secret(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_RUNBOOK])

        self.assertIn("MemberNoBase64Utf8", combined)
        self.assertIn("submitted_member_no_base64_utf8", combined)
        self.assertRegex(combined, r"(?i)not raw `MemberNo`")
        self.assertRegex(combined, r"(?i)Base64 is not encryption")
        self.assertRegex(combined, r"(?i)not secret")
        self.assertRegex(combined, r"(?i)interpolation risk|quoting")
        self.assertRegex(combined, r"\^\[A-Za-z0-9\+/]\+\=\{0,2\}\$")

    def test_docs_require_password_as_local_runtime_secret_not_queue_payload(self):
        combined = self.combined([NODE_CONTRACT, DIRECT_RUNBOOK, LOOKUP_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertIn("AC2_PROBE_PASSWORD", combined)
        self.assertRegex(combined, r"(?i)runtime-only local configuration|local environment secret")
        self.assertRegex(combined, r"(?i)Do not add real server, database, user, password")
        self.assertRegex(combined, r"(?i)must not be passed as a command argument|Do not pass it in command arguments")

    def test_docs_define_node_by_node_queue_workflow_outline(self):
        workflow = self.read(WORKFLOW_DOC)

        for phrase in [
            "Google Sheets new-row trigger or poller",
            "UTF-8 base64 encode step",
            "Base64 allowlist step",
            "Queue request step",
            "Wait/poll result step",
            "JSON/schema guard",
            "Route decision step",
        ]:
            self.assertIn(phrase, workflow)

    def test_queue_contract_defines_allowed_fields_for_request_response_and_states(self):
        contract = self.read(NODE_CONTRACT)

        for field in [
            "job_id",
            "intake_source",
            "source_reference",
            "source_row_ref",
            "row_number",
            "intake_id",
            "state",
            "submitted_member_no_base64_utf8",
            "consent_status",
            "pdpa_status",
            "payload_hash",
            "normalized_member_no_length",
            "member_exists",
            "manual_review_required",
            "dry_run_only",
            "final_write_automation",
        ]:
            self.assertIn(field, contract)

        for state in [
            "PENDING_LOOKUP",
            "LOOKUP_IN_PROGRESS",
            "LOOKUP_ERROR_REVIEW",
            "MANUAL_REVIEW_REQUIRED",
            "EXISTING_MEMBER_REVIEW",
            "READY_FOR_CREATE_REVIEW",
        ]:
            self.assertIn(state, contract)

        self.assertRegex(contract, r"(?i)Retry exhaustion routes to `LOOKUP_ERROR_REVIEW`")
        self.assertRegex(contract, r"(?i)Same idempotency key plus same payload hash")

    def test_queue_contract_defines_gate4a_real_queue_write_preparation(self):
        contract = self.read(NODE_CONTRACT)

        for phrase in [
            "For Gate 4A real queue-write preparation, the first approved source batch is exactly one row",
            "The source row must contain `Name`, the submitted phone/member number, `Email`, birthday when the current source includes birthday, and `PDPA Acknowledged = Yes`",
            "must not copy names, emails, raw phone numbers, birthday values, or raw submitted member values into the lookup queue",
            "The Gate 4A real queue-write row must populate",
            "It must not be a dummy Gate 2 rehearsal row",
            "`submitted_member_no_base64_utf8` must not be blank",
            "`queue_row_count = 1`",
            "`queue_base64_decode_ok_count = 1`",
            "`queue_base64_decode_fail_count = 0`",
            "`queue_decoded_blank_count = 0`",
            "`queue_decoded_looks_dummy_count = 0`",
            "These counters are queue-write prechecks only",
            "they are not Gate 4A lookup evidence",
            "do not authorize AC2 writes",
            "decoded value must equal the submitted phone/member number that maps to AutoCount `MemberNo`",
            "AutoCount `MobilePhone` is intentionally unused",
            "must never be committed, pasted, logged, or added to PR evidence",
        ]:
            self.assertIn(phrase, contract)

        for field in [
            "`job_id`",
            "`intake_source`",
            "`source_reference`",
            "`source_row_ref`",
            "`row_number`",
            "`intake_id`",
            "`state`",
            "`submitted_member_no_base64_utf8`",
            "`consent_status`",
            "`pdpa_status`",
            "`payload_hash`",
            "`attempt`",
            "`max_attempts`",
            "`created_at`",
            "`updated_at`",
            "`timeout_at`",
        ]:
            self.assertIn(field, contract)

    def test_docs_route_sanitized_json_outcomes(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK])

        expected_routes = {
            "LOOKUP_ERROR_REVIEW": [
                "process failure",
                "invalid JSON",
                "missing fields",
                "status != ok",
                "unexpected shape",
            ],
            "MANUAL_REVIEW_REQUIRED": ["manual_review_required = true"],
            "EXISTING_MEMBER_REVIEW": ["member_exists = true"],
            "READY_FOR_CREATE_REVIEW": ["member_exists = false", "warning_count = 0"],
        }
        for review_code, phrases in expected_routes.items():
            self.assertIn(review_code, combined)
            for phrase in phrases:
                self.assertIn(phrase, combined)

    def test_ready_for_create_review_is_not_final_create_approval(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, DECISION_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)READY_FOR_CREATE_REVIEW.*not approval to create")
        self.assertRegex(combined, r"(?i)separate PR")
        self.assertRegex(combined, r"(?i)explicit business approval")
        self.assertRegex(combined, r"(?i)idempotency")
        self.assertRegex(combined, r"(?i)consent/audit")
        self.assertRegex(combined, r"(?i)write guardrails")

    def test_docs_state_no_autocount_writes_and_no_final_write_automation(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, DECISION_RUNBOOK, BRIDGE_DESIGN, BRIDGE_RUNBOOK])

        self.assertRegex(combined, r"(?i)No AutoCount writes|AutoCount member writes")
        self.assertRegex(combined, r"(?i)not final write automation|does not authorize final write automation")
        self.assertRegex(combined, r"(?i)does not add an n8n workflow export|not a workflow export")
        self.assertRegex(combined, r"(?i)final write automation remains blocked|Real create/update automation remains blocked")

    def test_dry_run_docs_have_no_forbidden_write_api_tokens_or_sql_examples(self):
        combined = self.combined(DRY_RUN_DOCS)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, combined, token)
        self.assertNotRegex(
            combined,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )

    def test_docs_avoid_logging_raw_inputs_encoded_values_commands_and_stderr(self):
        combined = self.combined([WORKFLOW_DOC, NODE_CONTRACT, DIRECT_RUNBOOK, BRIDGE_RUNBOOK])

        for phrase in [
            "raw form values",
            "encoded values",
            "full command arguments",
            "credentials",
            "unsanitized stderr",
        ]:
            self.assertRegex(combined, re.escape(phrase), phrase)

    def test_template_is_intentionally_skipped(self):
        workflow = self.read(WORKFLOW_DOC)

        self.assertFalse(TEMPLATE_PATH.exists())
        self.assertIn("No n8n workflow template is included in this PR.", workflow)
        self.assertRegex(workflow, r"(?i)importable workflow artifact would create unnecessary activation risk")

    def test_uat_plan_documents_n8n_skills_and_official_references_checked(self):
        plan = self.read(UAT_PLAN)
        setup = self.read(UAT_SETUP_RUNBOOK)
        combined = plan + "\n" + setup

        for skill in [
            "n8n-skills:using-n8n-skills",
            "n8n-skills:n8n-workflow-lifecycle",
            "n8n-skills:n8n-node-configuration",
            "n8n-skills:n8n-credentials-and-security",
            "n8n-skills:n8n-data-tables",
            "n8n-skills:n8n-error-handling",
            "n8n-skills:n8n-loops",
            "n8n-skills:n8n-expressions",
        ]:
            self.assertIn(skill, combined)

        for reference in [
            "Official n8n Google Sheets Trigger docs",
            "Official n8n Google Sheets node docs",
            "Official n8n Data Table docs",
            "Official n8n Wait node docs",
            "Official n8n Schedule Trigger docs",
            "Official n8n execution data and redaction docs",
            "n8n-skills plugin and official n8n documentation are the current planning source",
            "`n8n-skills` live tooling reference",
        ]:
            self.assertIn(reference, plan)

        self.assertRegex(combined, r"(?i)n8n-skills-backed")
        self.assertRegex(plan, r"(?i)not a blocker for this UAT-only plan")
        self.assertRegex(combined, r"(?i)future live-instance verification")
        self.assertRegex(combined, r"(?i)exact live n8n node parameter shapes")
        live_tooling_label = "M" + "CP"
        self.assertNotRegex(plan, rf"(?i)n8n {live_tooling_label}-backed")
        self.assertNotRegex(plan, rf"(?i)n8n {live_tooling_label} / skills")
        self.assertIn("get_node_types", plan)
        self.assertIn("validate_workflow", plan)
        self.assertIn("get_workflow_details", plan)
        self.assertIn("ai-agent-toolkit:n8n-agent-rules", setup)
        self.assertIn("ai-agent-toolkit:n8n-local-setup", setup)

    def test_uat_plan_compares_queue_options_and_recommends_sheets_uat_only(self):
        plan = self.read(UAT_PLAN)

        for option in [
            "Google Sheets queue tab",
            "n8n Data Table / internal storage",
            "Lightweight external queue/API",
            "Local file drop only for fixture mode",
        ]:
            self.assertIn(option, plan)

        self.assertRegex(plan, r"(?i)Recommended UAT approach: use a Google Sheets queue tab")
        self.assertRegex(plan, r"(?i)explicitly UAT-only")
        self.assertRegex(plan, r"(?i)disabled or replaced before production activation")
        self.assertRegex(plan, r"(?i)Candidate for a later controlled UAT")

    def test_uat_setup_runbook_selects_hosted_dummy_sheets_first_step(self):
        setup = self.read(UAT_SETUP_RUNBOOK)
        readme = self.read(README)

        self.assertIn("member_intake_n8n_uat_setup_runbook.md", readme)
        self.assertIn("member_intake_n8n_gate2_dummy_rehearsal_runbook.md", readme)
        self.assertIn("member_intake_n8n_gate2_dummy_rehearsal_runbook.md", setup)
        self.assertRegex(setup, r"(?i)hosted/VPS/non-AC2 n8n")
        self.assertRegex(setup, r"(?i)Google Sheets UAT queue tab")
        self.assertRegex(setup, r"(?i)dummy fixture data first")
        self.assertRegex(setup, r"(?i)minimum next runnable n8n step")
        self.assertRegex(setup, r"(?i)manual, inactive hosted/VPS/non-AC2")
        self.assertRegex(setup, r"(?i)does not touch AC2")
        self.assertRegex(setup, r"(?i)n8n-skills plugin is a Codex-side planning and build aid only")
        self.assertRegex(setup, r"(?i)not the hosted n8n runtime")
        self.assertRegex(setup, r"(?i)Do not use local n8n on the AutoCount host as the long-term path")
        self.assertRegex(setup, r"(?i)The Google Sheets queue is UAT-only")
        self.assertRegex(setup, r"(?i)disabled or replaced before production activation")

    def test_uat_setup_runbook_orders_dummy_rehearsal_before_powershell_preflight(self):
        setup = self.read(UAT_SETUP_RUNBOOK)
        plan = self.read(UAT_PLAN)
        combined = setup + "\n" + plan

        for phrase in [
            "Gate 2: Hosted n8n Dummy Queue Rehearsal",
            "Gate 2 may proceed before the PowerShell lookup preflight",
            "dummy n8n rehearsal may proceed before the PowerShell lookup preflight",
            "does not touch AC2",
            "no bridge call to AC2",
            "Gate 3: Required Local PowerShell Lookup Preflight",
            "Before any real n8n queue UAT is allowed to touch AC2 lookup",
            "Gate 3 does not block Gate 2",
            "Gate 3 must pass before Gate 4",
            "Gate 4: Real Queue UAT Touching AC2 Lookup",
            "--queue-mode fixture",
            "--lookup-mode powershell",
            "--enable-powershell-lookup",
            "-EnableMemberLookupReview",
            "-MemberNoBase64Utf8",
            "safe synthetic input or one manually approved lookup input",
            "aggregate sanitized evidence only",
            "dry_run_only",
            "final_write_automation",
        ]:
            self.assertIn(phrase, combined)

        self.assertLess(
            setup.index("Gate 2: Hosted n8n Dummy Queue Rehearsal"),
            setup.index("Gate 3: Required Local PowerShell Lookup Preflight"),
        )
        self.assertLess(
            setup.index("Gate 3: Required Local PowerShell Lookup Preflight"),
            setup.index("Gate 4: Real Queue UAT Touching AC2 Lookup"),
        )

        for forbidden_phrase in [
            "raw fixture rows",
            "raw result rows",
            "encoded submitted values",
            "normalized values",
            "command transcripts",
            "stderr/stdout",
            "row-level output",
        ]:
            self.assertIn(forbidden_phrase, combined)

        self.assertRegex(combined, r"(?i)No state authorizes member creation")
        self.assertRegex(combined, r"(?i)Gate 4.*review-only")
        self.assertRegex(combined, r"(?i)Gate 4.*dry-run-only")
        self.assertRegex(combined, r"(?i)cannot create or update AutoCount members")
        self.assertRegex(combined, r"(?i)PDPA Acknowledged = Imported.*valid consent|Imported.*valid consent")

    def test_gate3_powershell_preflight_is_read_only_aggregate_only_and_blocks_gate4(self):
        setup = self.read(UAT_SETUP_RUNBOOK)
        bridge_runbook = self.read(BRIDGE_RUNBOOK)
        combined = setup + "\n" + bridge_runbook

        for phrase in [
            "gate = gate3_local_powershell_lookup_preflight",
            "runtime_location = local_windows_ac2_lookup_environment",
            "execution_mode = manual_read_only_preflight",
            "autocount_session_bootstrap_available = <true/false>",
            "member_command_found = <true/false>",
            "get_member_found = <true/false>",
            "lookup_attempt_count = <aggregate-count-only>",
            "lookup_success_count = <aggregate-count-only>",
            "lookup_manual_review_count = <aggregate-count-only>",
            "lookup_error_count = <aggregate-count-only>",
            "member_create_or_update_invoked = false",
            "autocount_write_attempted = false",
            "direct_sql_write_attempted = false",
            "n8n_involved = false",
            "bridge_called_by_n8n = false",
            "final_write_automation = false",
            "local Windows AC2 lookup environment",
            "MemberCommand.GetMember",
            "read-only",
            "aggregate booleans and counts",
            "Do not paste the bridge worker stdout directly as Gate 3 evidence",
            "does not authorize Gate 4",
            "Only after Gates 1, 2/2A, and 3 pass",
        ]:
            self.assertIn(phrase, combined)

        for forbidden_phrase in [
            "raw member values",
            "encoded member values",
            "normalized member values",
            "command transcripts",
            "stderr/stdout",
            "row-level output",
            "names, emails, phone numbers",
        ]:
            self.assertIn(forbidden_phrase, combined)

        self.assertRegex(combined, r"(?i)Do not involve n8n in Gate 3|n8n is not involved")
        self.assertRegex(combined, r"(?i)no member create/update|no member create/update/delete")
        self.assertRegex(combined, r"(?i)no AutoCount write|No AutoCount writes")
        self.assertRegex(combined, r"(?i)no direct SQL write|direct SQL write is attempted")
        self.assertRegex(combined, r"(?i)No state authorizes member creation|not approval to create")
        self.assertNotRegex(combined, r"(?i)paste back raw member")
        self.assertNotRegex(combined, r"(?i)paste back encoded member")
        self.assertNotRegex(combined, r"(?i)paste back normalized member")
        self.assertNotRegex(combined, r"(?i)paste back command transcript")

    def test_gate3_pass_evidence_is_recorded_as_sanitized_aggregate_only_status(self):
        body, evidence = self.gate3_recorded_evidence()

        expected = {
            "status": "ok",
            "gate": "gate3_local_powershell_lookup_preflight",
            "runtime_location": "local_windows_ac2_lookup_environment",
            "execution_mode": "manual_read_only_preflight",
            "autocount_session_bootstrap_available": "true",
            "member_command_found": "true",
            "get_member_found": "true",
            "lookup_attempt_count": "1",
            "lookup_success_count": "1",
            "lookup_manual_review_count": "1",
            "lookup_error_count": "0",
            "member_create_or_update_invoked": "false",
            "autocount_write_attempted": "false",
            "direct_sql_write_attempted": "false",
            "n8n_involved": "false",
            "bridge_called_by_n8n": "false",
            "final_write_automation": "false",
        }
        for key, value in expected.items():
            self.assertEqual(evidence[key], value, key)

        for count_key in [
            "lookup_attempt_count",
            "lookup_success_count",
            "lookup_manual_review_count",
            "lookup_error_count",
        ]:
            self.assertRegex(evidence[count_key], r"^\d+$")

        self.assertIn("No credentials, connection strings, Sheet IDs/URLs", evidence["sanitized_note"])
        self.assertIn("command transcripts, stderr/stdout", evidence["sanitized_note"])
        self.assertNotIn("<aggregate-count-only>", body)
        self.assertNotIn("<true/false>", body)
        self.assertNotIn("result_state_counts", body)
        self.assertNotIn("row_number", body)
        self.assertNotIn("source_reference", body)
        self.assertNotIn("source_row_ref", body)
        self.assertNotIn("submitted_member_no_base64_utf8", body)
        self.assertNotRegex(body, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(body, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(body, r"(?i)\b(?:\+?65)?[689]\d{7}\b")
        self.assertNotRegex(body, r"(?i)\b(credential|sheet|member|phone|email|name)_?id\s*=")
        self.assertNotRegex(body, r"(?i)\b(stdin|stdout|stderr)\s*=")
        self.assertNotRegex(body, r"(?m)^PS [A-Z]:\\")

    def test_gate3_pass_scope_and_gate4_boundary_are_explicit(self):
        setup = self.read(UAT_SETUP_RUNBOOK)

        for phrase in [
            "Gate 3 local Windows PowerShell lookup preflight has passed",
            "local Windows AC2 lookup environment was available",
            "AutoCount session/auth bootstrap was available",
            "`MemberCommand` was found",
            "`MemberCommand.GetMember` was found",
            "one lookup attempt succeeded",
            "manual-review rather than an error",
            "no member create/update/write/direct SQL/n8n/final automation path was invoked",
            "does not prove production automation",
            "does not authorize member create/update",
            "does not authorize AutoCount writes",
            "does not by itself prove hosted/VPS n8n runtime readiness",
            "Gate 4 remains plan-only in the current reviewed PR",
            "member_intake_n8n_lookup_bridge_uat_plan.md",
        ]:
            self.assertIn(phrase, setup)

    def test_gate4_real_queue_uat_plan_is_plan_only_and_lookup_only(self):
        plan = self.read(UAT_PLAN)
        setup = self.read(UAT_SETUP_RUNBOOK)
        combined = plan + "\n" + setup

        for phrase in [
            "Status: Gate 4 real queue UAT plan only",
            "does not run Gate 4",
            "does not touch real queue data from this PR",
            "Gate 4 Real Queue UAT Plan",
            "real queue UAT touching AC2 lookup only",
            "read-only and review-only",
            "no member create/update/delete path",
            "no AutoCount write path",
            "no direct SQL write path",
            "no final write automation",
            "Passing Gate 4 means only that read-only lookup UAT passed",
            "still does not authorize member create/update",
            "separate reviewed PR and explicit business approval",
        ]:
            self.assertIn(phrase, combined)

        self.assertRegex(combined, r"(?i)This PR defines the plan only")
        self.assertRegex(combined, r"(?i)No create/update action is available in the flow")
        self.assertRegex(combined, r"(?i)READY_FOR_CREATE_REVIEW.*review state only")
        self.assertRegex(combined, r"(?i)does not activate n8n")
        self.assertRegex(combined, r"(?i)does not call AC2")
        self.assertRegex(combined, r"(?i)does not authorize scheduler or production activation")

    def test_gate4_runtime_boundary_blocks_ac2_host_and_activation_surface(self):
        plan = self.read(UAT_PLAN)

        for phrase in [
            "n8n runtime for Gate 4 must be outside the AC2 host",
            "local_operator_pc_non_ac2_n8n_stack",
            "must not claim hosted/VPS readiness",
            "hosted_or_vps_non_ac2",
            "hosted/VPS readiness must be separately evidenced",
            "n8n must remain inactive and manually run",
            "No scheduler is enabled",
            "No public inbound webhook, callback, tunnel, or reverse proxy may be exposed on the AC2 host",
            "Hosted/cloud/VPS n8n must not use Execute Command for AC2 lookup",
        ]:
            self.assertIn(phrase, plan)

        self.assertNotRegex(plan, r"(?i)public inbound webhook.*allowed")
        self.assertNotRegex(plan, r"(?i)scheduler_enabled = true")
        self.assertNotRegex(plan, r"(?i)workflow_activation = active")

    def test_gate4_queue_flow_and_stop_conditions_are_review_only(self):
        plan = self.read(UAT_PLAN)

        for phrase in [
            "Google Sheets UAT-only queue and review tabs are allowed for this temporary UAT only",
            "real Sheet URLs, Sheet IDs, credential IDs, and resource locator values kept outside Git",
            "read only a tiny approved batch of real UAT rows explicitly marked for lookup",
            "write only sanitized `PENDING_LOOKUP` queue rows",
            "first real queue-write preparation batch must be exactly one approved source row",
            "append exactly one non-dummy `PENDING_LOOKUP` queue row",
            "source row must include `Name`, the submitted phone/member number, `Email`, birthday when applicable to the current source, and `PDPA Acknowledged = Yes`",
            "Current live/form consent is `PDPA Acknowledged = Yes`, normalized to `pdpa_status = yes`",
            "n8n must encode the submitted phone/member number into `submitted_member_no_base64_utf8`",
            "maps to AutoCount `MemberNo`",
            "AutoCount `MobilePhone` is intentionally unused",
            "operator must confirm aggregate-only queue prechecks",
            "`queue_row_count = 1`",
            "`queue_base64_decode_ok_count = 1`",
            "`queue_base64_decode_fail_count = 0`",
            "`queue_decoded_blank_count = 0`",
            "`queue_decoded_looks_dummy_count = 0`",
            "lookup queue contains only dummy Gate 2 rehearsal rows",
            "must not be recorded as Gate 4 or Gate 4A pass evidence",
            "local Windows bridge may read only approved `PENDING_LOOKUP` queue rows",
            "write only sanitized review result rows",
            "map sanitized results back only to review/status fields",
            "Google Sheets remains temporary and must be disabled or replaced before production activation",
            "bridge calls only the read-only PowerShell lookup path",
            "Stop the UAT immediately",
            "lookup error spike or unexpected result shape",
            "any member create/update/delete path reference",
            "any direct SQL write indication",
            "any final write automation indication",
            "any workflow activation or scheduler enablement",
        ]:
            self.assertIn(phrase, plan)

        for forbidden_phrase in [
            "unexpected field",
            "raw value",
            "encoded value",
            "normalized value",
            "credential",
            "Sheet ID",
            "Sheet URL",
            "credential ID",
            "command transcript",
            "stdout/stderr",
            "PII",
            "webhook/tunnel exposure",
            "AutoCount write attempt",
        ]:
            self.assertIn(forbidden_phrase, plan)

    def test_gate4_evidence_shape_is_aggregate_only_and_secret_free(self):
        plan = self.read(UAT_PLAN)
        match = re.search(
            r"Suggested safe paste-back shape:\n\n```text\n(?P<body>.*?)\n```",
            plan,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")

        expected_fields = [
            "status = <ok/needs_fix>",
            "gate = gate4_real_queue_uat_ac2_lookup_only",
            "runtime_location = <local_operator_pc_non_ac2_n8n_stack OR hosted_or_vps_non_ac2>",
            "execution_mode = manual_inactive_review_only_uat",
            "approved_batch_size = <aggregate-count-only>",
            "queue_rows_read_count = <aggregate-count-only>",
            "queue_rows_written_count = <aggregate-count-only>",
            "lookup_attempt_count = <aggregate-count-only>",
            "lookup_success_count = <aggregate-count-only>",
            "lookup_existing_member_review_count = <aggregate-count-only>",
            "lookup_manual_review_count = <aggregate-count-only>",
            "lookup_error_count = <aggregate-count-only>",
            "review_rows_written_count = <aggregate-count-only>",
            "form_or_source_rows_updated_count = <aggregate-count-only>",
            "member_create_or_update_invoked = false",
            "autocount_write_attempted = false",
            "direct_sql_write_attempted = false",
            "workflow_activation = inactive",
            "scheduler_enabled = false",
            "public_inbound_to_ac2_host = false",
            "final_write_automation = false",
        ]
        for field in expected_fields:
            self.assertIn(field, body)

        self.assertIn("No credentials, connection strings, Sheet IDs/URLs", body)
        self.assertIn("raw/encoded/normalized member values", body)
        self.assertIn("command transcripts, stderr/stdout", body)
        self.assertIn("node raw input/output dumps", body)
        self.assertIn("PII are pasted", body)
        self.assertIn("All count fields are aggregate-count-only", plan)
        self.assertIn("Do not include result rows, fixture rows, queue rows, source rows", plan)
        self.assertNotRegex(body, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(body, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(body, r"(?i)\b(?:\+?65)?[689]\d{7}\b")
        self.assertNotRegex(body, r"submitted_member_no_base64_utf8")
        self.assertNotRegex(body, r"(?i)\b(stdin|stdout|stderr)\s*=")

    def test_gate4_plan_has_no_workflow_artifact_or_write_path_literals(self):
        plan = self.read(UAT_PLAN)
        setup = self.read(UAT_SETUP_RUNBOOK)
        combined = plan + "\n" + setup

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, combined, token)
        self.assertNotRegex(
            combined,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )
        self.assertNotRegex(combined, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(combined, r"(?i)(client_email|private_key|service_account|-----BEGIN PRIVATE KEY-----)")
        self.assertNotRegex(combined, r"(?i)credential_id\s*=")
        self.assertNotRegex(combined, r"(?i)workflow export file")
        self.assertRegex(combined, r"(?i)does not add a workflow export")
        self.assertRegex(combined, r"(?i)does not activate n8n")
        self.assertRegex(combined, r"(?i)does not authorize AutoCount writes")

    def test_uat_setup_records_local_operator_n8n_rehearsal_without_hosted_runtime_claim(self):
        setup = self.read(UAT_SETUP_RUNBOOK)

        self.assertIn("Recorded Local n8n Dummy Wiring Rehearsal", setup)
        self.assertIn("n8n_runtime_location = local_operator_pc_non_ac2_n8n_stack", setup)
        self.assertIn("dummy_rows_read_count = 3", setup)
        self.assertIn("queue_rows_appended_count = 1", setup)
        self.assertIn("result_rows_read_count = 4", setup)
        self.assertIn("review_rows_updated_count = 4", setup)
        self.assertIn(
            "result_state_counts = READY_FOR_CREATE_REVIEW=1, EXISTING_MEMBER_REVIEW=1, MANUAL_REVIEW_REQUIRED=1, LOOKUP_ERROR_REVIEW=1",
            setup,
        )
        self.assertIn("ac2_touched = false", setup)
        self.assertIn("bridge_called = false", setup)
        self.assertIn("final_write_automation = false", setup)
        self.assertIn("This was not hosted/VPS n8n", setup)
        self.assertIn("must not be reported as `hosted_or_vps_non_ac2`", setup)
        self.assertIn("does not prove hosted/VPS runtime readiness", setup)
        self.assertIn("does not authorize Gate 4", setup)
        self.assertIn("The next gate is Gate 3: local Windows PowerShell lookup preflight", setup)
        self.assertIn("read-only, fixture-based or dummy-only, aggregate-evidence-only", setup)
        self.assertIn("no member create/update, no AutoCount writes", setup)
        self.assertIn("no raw, encoded, or normalized member values or PII pasted", setup)
        self.assertNotRegex(setup, r"n8n_runtime_location = hosted_or_vps_non_ac2")
        self.assertNotRegex(setup, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(setup, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")

    def test_gate2_dummy_rehearsal_runbook_defines_safe_operator_steps(self):
        gate2 = self.read(GATE2_RUNBOOK)
        setup = self.read(UAT_SETUP_RUNBOOK)
        combined = gate2 + "\n" + setup

        for phrase in [
            "Gate 2 operator steps/spec only",
            "hosted/VPS/non-AC2 n8n",
            "workflow_activation = inactive",
            "execution_mode = manual_dummy_rehearsal",
            "does not call the bridge",
            "does not run PowerShell",
            "does not touch AC2",
            "AC2 credentials: not required",
            "Form Responses UAT",
            "Lookup Queue UAT",
            "Lookup Results UAT",
            "UAT Audit Summary",
            "Read Dummy UAT Rows Marked For Lookup",
            "Block Already Queued Rows",
            "Apply Intake And PDPA Guards",
            "Build Allowed Queue Job",
            "Validate Encoded Member Value",
            "Append Dummy UAT Queue Job",
            "Mark Dummy Form Row Queued",
            "Read Dummy Bridge Results Ready For n8n",
            "Validate Result Schema",
            "Map Result To Review State",
            "Update Dummy Form Review Fields",
            "Mark Dummy Result Applied",
            "ac2_touched = false",
            "bridge_called = false",
            "final_write_automation = false",
            "Gate 3 PowerShell lookup preflight must still pass before any real n8n queue UAT touches AC2 lookup",
        ]:
            self.assertIn(phrase, combined)

        self.assertRegex(gate2, r"(?i)No workflow export is committed")
        self.assertRegex(gate2, r"(?i)no import-ready artifact")
        self.assertRegex(gate2, r"(?i)Manual Trigger")
        self.assertRegex(gate2, r"(?i)disabled `Schedule Trigger`")
        self.assertRegex(gate2, r"(?i)PDPA.*imported.*blocked")
        self.assertRegex(gate2, r"(?i)READY_FOR_CREATE_REVIEW.*review-only")

    def test_gate2_dummy_rehearsal_evidence_is_aggregate_only(self):
        gate2 = self.read(GATE2_RUNBOOK)

        for phrase in [
            "dummy_rows_read_count",
            "queue_rows_appended_count",
            "result_rows_read_count",
            "review_rows_updated_count",
            "result_state_counts",
            "Aggregate count only",
            "No real Sheet IDs/URLs, credentials, row-level output",
            "raw/encoded/normalized values",
            "node raw input/output dumps",
            "Stop condition",
            "do not paste it",
        ]:
            self.assertIn(phrase, gate2)

        for forbidden in [
            "Sheet URLs or Sheet IDs",
            "credential IDs",
            "screenshots with row-level data",
            "full execution payloads",
            "node raw input or output dumps",
            "raw member values",
            "encoded member values",
            "normalized member values",
            "names, emails, or phone numbers",
        ]:
            self.assertIn(forbidden, gate2)

    def test_gate2_dummy_rehearsal_runbook_has_no_sensitive_literals_or_workflow_artifacts(self):
        gate2 = self.read(GATE2_RUNBOOK)

        for placeholder in [
            "<local-placeholder-generated-outside-repo>",
            "<aggregate-count-only>",
            "<aggregate-counts-only>",
        ]:
            self.assertIn(placeholder, gate2)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, gate2, token)
        self.assertNotRegex(gate2, r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b")
        self.assertNotRegex(gate2, r"https://docs\.google\.com/spreadsheets/d/")
        service_account_markers = "|".join([
            "client" + "_email",
            "private" + "_key",
            "service" + "_account",
            "-----BEGIN PRIVATE " + "KEY-----",
        ])
        self.assertNotRegex(gate2, rf"(?i)({service_account_markers})")
        self.assertNotRegex(gate2, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(gate2, r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"")
        self.assertNotRegex(gate2, r"(?i)workflow export file")
        self.assertNotRegex(gate2, r"(?i)activate a schedule without a future approval")

    def test_pdpa_current_form_value_is_yes_and_normalized_queue_status_is_yes(self):
        combined = self.combined([WORKFLOW_DOC, UAT_PLAN, GATE2_RUNBOOK, NODE_CONTRACT, BRIDGE_RUNBOOK])

        self.assertIn("PDPA Acknowledged = Yes", combined)
        self.assertIn("queue value `yes`", combined)
        self.assertIn("pdpa_status = yes", combined)
        self.assertRegex(combined, r"(?i)Imported.*blocked|imported.*blocked")
        self.assertRegex(combined, r"(?i)consent_status.*not.*PDPA|not a PDPA override")
        self.assertNotIn("PDPA Acknowledged = I agree", combined)
        self.assertNotIn("pdpa_status = i_agree", combined)
        self.assertNotIn("`i_agree`", combined)
        self.assertNotRegex(combined, r"(?i)current live form value.*I agree")


    def test_uat_plan_defines_exact_node_level_polling_workflow_shape(self):
        plan = self.read(UAT_PLAN)

        for phrase in [
            "Workflow A: Queue Lookup Jobs",
            "Schedule Trigger",
            "Google Sheets",
            "Read UAT Rows Marked For Lookup",
            "Apply Intake And PDPA Guards",
            "Build Allowed Queue Job",
            "Validate Encoded Member Value",
            "Append UAT Queue Job",
            "Workflow B: Apply Sanitized Results",
            "Read Bridge Results Ready For n8n",
            "Validate Result Schema",
            "Map Result To Review State",
            "Update Form Review Fields",
            "Workflow C: Timeout And Retry Sweep",
            "Sweep UAT Lookup Timeouts",
        ]:
            self.assertIn(phrase, plan)

        self.assertRegex(plan, r"(?i)Do not use the Wait node")
        self.assertRegex(plan, r"(?i)scheduled result polling")
        self.assertRegex(plan, r"(?i)no execution waits with raw form context")

    def test_uat_plan_defines_allowed_queue_result_and_reviewer_fields(self):
        plan = self.read(UAT_PLAN)

        for field in [
            "job_id",
            "row_number",
            "intake_id",
            "state",
            "submitted_member_no_base64_utf8",
            "payload_hash",
            "attempt",
            "max_attempts",
            "lease_owner",
            "lease_expires_at",
            "timeout_at",
            "normalized_member_no_length",
            "member_exists",
            "manual_review_required",
            "warning_count",
            "dry_run_only",
            "final_write_automation",
            "uat_lookup_job_id",
            "reviewer_status",
            "reviewer_decision_code",
        ]:
            self.assertIn(field, plan)

        for state in [
            "PENDING_LOOKUP",
            "LOOKUP_IN_PROGRESS",
            "LOOKUP_ERROR_REVIEW",
            "MANUAL_REVIEW_REQUIRED",
            "EXISTING_MEMBER_REVIEW",
            "READY_FOR_CREATE_REVIEW",
        ]:
            self.assertIn(state, plan)

        self.assertRegex(plan, r"(?i)Free-text reviewer notes are out of scope")
        self.assertRegex(plan, r"(?i)No state authorizes member creation")

    def test_uat_plan_forbids_pii_secrets_sheet_ids_urls_and_direct_write_paths(self):
        plan = self.read(UAT_PLAN)
        setup = self.read(UAT_SETUP_RUNBOOK)
        combined = plan + "\n" + setup

        for phrase in [
            "raw member numbers",
            "normalized member numbers",
            "names",
            "emails",
            "raw phone numbers",
            "Sheet URLs",
            "Sheet IDs",
            "credentials",
            "connection strings",
            "stderr/stdout",
            "PII",
        ]:
            self.assertRegex(combined, re.escape(phrase), phrase)

        for token in FORBIDDEN_WRITE_TOKENS:
            self.assertNotIn(token, combined, token)
        self.assertNotRegex(
            combined,
            r"(?i)\b(SELECT\s+\*|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MERGE\s+INTO)\b",
        )
        self.assertNotRegex(combined, r"https://docs\.google\.com/spreadsheets/d/")
        self.assertNotRegex(combined, r"(?i)(password|secret|token)\s*[:=]\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(combined, r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
        self.assertNotRegex(combined, r"submitted_member_no_base64_utf8\"\s*:\s*\"[A-Za-z0-9+/]+=*\"")
        self.assertRegex(combined, r"(?i)No production activation is allowed")
        self.assertRegex(combined, r"(?i)Real create/update automation remains blocked")

if __name__ == "__main__":
    unittest.main()
