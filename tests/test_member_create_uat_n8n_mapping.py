import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "n8n-workflows" / "member_create_uat_result_mapping.workflow.json"
for _p in (str(ROOT / "scripts"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import member_create_uat_contract as contract  # noqa: E402
import _create_uat_fixtures as fx  # noqa: E402

# Executes the workflow's Code node exactly as the repository's other n8n mapping suites do, so
# the n8n validation surface is proved by RUNNING it rather than by reading its text. No n8n
# instance, container, credential, Google or AutoCount access is involved: this is `node -e` over
# the committed jsCode with a synthetic in-memory item.
NODE_WRAPPER = r"""
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
try {
  const run = new Function('$input', '$', 'Buffer', payload.code);
  const items = payload.items.map((json) => ({ json }));
  const result = run({ all: () => items, first: () => items[0] }, () => ({}), Buffer);
  console.log(JSON.stringify({ ok: true, result }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: String(error && error.message ? error.message : error) }));
}
"""


def workflow_code(node_name):
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    node = next(n for n in workflow["nodes"] if n["name"] == node_name)
    return node["parameters"]["jsCode"]


def run_validate_node(result):
    node_exe = shutil.which("node")
    if not node_exe:
        raise unittest.SkipTest("node executable is required to execute the n8n Code node")
    completed = subprocess.run(
        [node_exe, "-e", NODE_WRAPPER],
        input=json.dumps(
            {
                "code": workflow_code("Validate Sanitized Terminal Result Strictly"),
                "items": [{"data": json.dumps(result)}],
            }
        ),
        text=True, capture_output=True, check=True,
    )
    return json.loads(completed.stdout)


def sample_result(**overrides):
    """A clean CREATED_VERIFIED sanitized result, synthetic and disposable throughout."""
    pkg = fx.build_valid_package()
    result = {
        "mode": "write", "runtime_location": "autocount_vm", "terminal_code": "CREATED_VERIFIED",
        "package_structural_valid": True, "package_fingerprint_problem": False,
        "approval_not_expired": True, "write_confirmed": True, "business_confirmed": True,
        "lock_acquired": True, "recovery_state": "none", "execution_error": False,
        "authentication_success": True, "member_command_found": True, "get_member_found": True,
        "member_exists_initial": False, "new_member_success": True, "assignment_success": True,
        "assigned_field_count": 11, "expiry_date_assigned": True, "member_exists_recheck": False,
        "write_intent_recorded": True, "consumed_marker_written": True,
        "save_member_attempted": True, "save_member_confirmed": True, "save_outcome": "confirmed",
        "readback_found": True, "readback_match": True, "masked_member_no": "65***1",
        "operation_id": pkg["operation_id"], "source_record_id": pkg["source_record_id"],
        "source_fingerprint": pkg["source_fingerprint"], "error": None,
    }
    result.update(overrides)
    return result


class N8nMappingStaticTests(unittest.TestCase):
    """Static assertions (finding 5) proving operation_id is the single-use mapping
    key and the workflow stays inactive, credential-free, and mapping-only."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.wf = json.loads(cls.text)
        cls.nodes = {n["name"]: n for n in cls.wf["nodes"]}

    def test_inactive_and_credential_free(self):
        self.assertFalse(self.wf["active"])
        self.assertFalse(self.wf["meta"]["templateCredsSetupCompleted"])
        self.assertNotIn("webhookId", self.text)
        self.assertNotIn('"credentials"', self.text)

    def test_shared_yes_marker_is_gone(self):
        self.assertNotIn("MemberCreateUatApprovedForMapping", self.text)

    def test_read_node_filters_by_operation_id(self):
        read = self.nodes["Read Row By Operation Id"]
        flt = read["parameters"]["filtersUI"]["values"][0]
        self.assertEqual(flt["lookupColumn"], "uat_create_operation_id")
        self.assertIn("operation_id", flt["lookupValue"])

    def test_update_node_matches_on_operation_id(self):
        upd = self.nodes["Update Review Fields By Operation Id"]
        self.assertEqual(upd["parameters"]["columns"]["matchingColumns"], ["uat_create_operation_id"])
        self.assertEqual(upd["parameters"]["operation"], "update")
        self.assertEqual(upd["parameters"]["options"]["cellFormat"], "RAW")

    def test_no_forbidden_action_nodes(self):
        types = {n["type"] for n in self.wf["nodes"]}
        for forbidden in (
            "n8n-nodes-base.httpRequest",
            "n8n-nodes-base.executeCommand",
            "n8n-nodes-base.webhook",
            "n8n-nodes-base.scheduleTrigger",
            "@n8n/n8n-nodes-langchain.agent",
        ):
            self.assertNotIn(forbidden, types)

    def test_validate_node_recomputes_terminal_state(self):
        code = self.nodes["Validate Sanitized Terminal Result Strictly"]["parameters"]["jsCode"]
        # The canonical recompute-and-compare (finding 4) must be present.
        self.assertIn("recompute", code)
        self.assertIn("create_uat_terminal_code_does_not_match_recomputed", code)
        self.assertIn("create_uat_state_contradiction", code)
        # The ExpiryDate activation guards must be mirrored into the canonical table so
        # the n8n validator agrees with the Python and PowerShell contradiction checks.
        self.assertIn("expiry_date_not_assigned", code)
        self.assertIn("assigned_field_count_stale", code)
        self.assertIn("EXPECTED_ASSIGNED_FIELD_COUNT", code)

    def test_verify_node_rejects_blank_or_conflicting_operation_id(self):
        code = self.nodes["Verify Identity And Decide Mapping"]["parameters"]["jsCode"]
        self.assertIn("create_uat_row_operation_id_blank_or_malformed", code)
        self.assertIn("create_uat_row_operation_id_conflict", code)
        self.assertIn("create_uat_requires_exactly_one_row_for_operation_id", code)
        # Identity + fingerprint revalidation retained.
        self.assertIn("create_uat_source_record_id_mismatch", code)
        self.assertIn("create_uat_source_fingerprint_mismatch", code)

    def test_google_sheets_nodes_have_retry(self):
        for name in ("Read Row By Operation Id", "Update Review Fields By Operation Id"):
            self.assertTrue(self.nodes[name]["retryOnFail"])

    def test_validate_node_declares_the_strict_boolean_contract(self):
        """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWO, statically.

        The n8n boolean subset must be the SAME identities the Python contract declares, so the
        two validation surfaces cannot drift apart.
        """
        code = self.nodes["Validate Sanitized Terminal Result Strictly"]["parameters"]["jsCode"]
        self.assertIn("booleanTypeViolations", code)
        self.assertIn("create_uat_boolean_flag_type_invalid", code)
        self.assertIn("typeof f[n] !== 'boolean'", code)
        for name in contract.TERMINAL_STATE_BOOLEAN_FLAGS:
            with self.subTest(flag=name):
                self.assertIn("'%s'" % name, code)
        # The type gate must be applied before the truthiness-based table can recompute.
        self.assertLess(code.index("booleanTypeViolations(result)"), code.index("recompute(result)"))


class N8nStrictBooleanExecutionTests(unittest.TestCase):
    """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWO, at the executed n8n validation surface.

    JavaScript `!!'false'` is true, so the validator previously PASSED a tampered result whose
    ExpiryDate-assignment proof was the string "false". Each control below runs the committed
    Code node and asserts the refusal by its own error name.
    """

    def test_a_clean_boolean_result_still_passes(self):
        outcome = run_validate_node(sample_result())
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["result"][0]["json"]["terminal_code"], "CREATED_VERIFIED")

    def test_the_originating_expiry_date_assigned_string_is_refused(self):
        outcome = run_validate_node(sample_result(expiry_date_assigned="false"))
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual(outcome["error"], "create_uat_boolean_flag_type_invalid")

    def test_every_substitute_type_is_refused(self):
        for substitute in ("false", "true", "", 0, 1, 0.0, None, [], {}):
            with self.subTest(substitute=repr(substitute)):
                outcome = run_validate_node(sample_result(expiry_date_assigned=substitute))
                self.assertFalse(outcome["ok"], outcome)
                self.assertEqual(outcome["error"], "create_uat_boolean_flag_type_invalid")

    def test_every_same_root_boolean_flag_is_refused(self):
        for name in contract.TERMINAL_STATE_BOOLEAN_FLAGS:
            with self.subTest(flag=name):
                outcome = run_validate_node(sample_result(**{name: "false"}))
                self.assertFalse(outcome["ok"], outcome)
                self.assertEqual(outcome["error"], "create_uat_boolean_flag_type_invalid")

    def test_the_two_validation_surfaces_agree_on_every_substitute(self):
        """Python and n8n must reach the same verdict, which is what keeps them one contract."""
        for name in contract.TERMINAL_STATE_BOOLEAN_FLAGS:
            for substitute in ("false", 1, None):
                with self.subTest(flag=name, substitute=repr(substitute)):
                    result = sample_result(**{name: substitute})
                    flags = {k: result[k] for k in contract.TERMINAL_STATE_FLAGS if k in result}
                    self.assertTrue(contract.terminal_state_boolean_type_violations(flags))
                    self.assertFalse(run_validate_node(result)["ok"])

    def test_a_real_boolean_false_is_still_evaluated_by_the_table(self):
        """Not "reject everything falsy": a genuine False remains an ordinary table input."""
        outcome = run_validate_node(sample_result(expiry_date_assigned=False))
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual(outcome["error"], "create_uat_state_contradiction",
                         "a real False must reach the contradiction table, not the type gate")

    def test_no_flag_value_is_echoed_in_the_refusal(self):
        outcome = run_validate_node(
            sample_result(expiry_date_assigned="MEMBER-90000001-SYNTHETIC")
        )
        self.assertFalse(outcome["ok"], outcome)
        self.assertNotIn("MEMBER-90000001-SYNTHETIC", outcome["error"])


if __name__ == "__main__":
    unittest.main()
