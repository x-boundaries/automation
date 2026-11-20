"""Repo-wide guard (#332 T4): no "Always Output Data" on a node with more than one main output.

n8n 3.0 changes Always Output Data on a multi-output node: it adds the empty item only when every
output is empty. On 2.x the empty item lands on output 0 even when the error output has items, so a
failed lookup can run both branches (#332 G0, section 1). A node that is single-output behaves the
same on both versions, which is the repo pattern: `onError: continueRegularOutput` with
`alwaysOutputData: true`, then a Code or If node that classifies the result.

A node counts as multi-output when it has `onError: continueErrorOutput`, is a type with more than one
main output (If, Switch, Filter, Loop Over Items, Remove Duplicates, Compare Datasets), or has more
than one main output wired. Offline: files only.
"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "n8n-workflows"
MULTI_OUTPUT_TYPES = {
    "n8n-nodes-base.if", "n8n-nodes-base.switch", "n8n-nodes-base.filter", "n8n-nodes-base.splitInBatches",
    "n8n-nodes-base.removeDuplicates", "n8n-nodes-base.compareDatasets",
}

# Exact file names only, each with its reason (owner, #332 Q1b, 20 Nov 2026).
EXEMPT = {
    "energygrid_invoice_delivery.workflow.json":
        "archived live (Q1b); remove the exemption if restored",
}


def workflow_exports():
    found = []
    for path in sorted(EXPORTS.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            found.append((path.relative_to(EXPORTS).as_posix(), data))
    return found


def multi_output_always_output_nodes(name, workflow):
    """Every node of one export with alwaysOutputData and more than one main output."""

    findings = []
    connections = workflow.get("connections") or {}
    for node in workflow["nodes"]:
        if node.get("alwaysOutputData") is not True:
            continue
        wired = len((connections.get(node.get("name")) or {}).get("main") or [])
        if node.get("onError") == "continueErrorOutput" or node.get("type") in MULTI_OUTPUT_TYPES or wired > 1:
            findings.append("%s: %s" % (name, node.get("name")))
    return findings


class AlwaysOutputDataGuardTests(unittest.TestCase):
    def test_no_multi_output_node_has_always_output_data(self):
        findings = []
        for name, workflow in workflow_exports():
            if name not in EXEMPT:
                findings += multi_output_always_output_nodes(name, workflow)
        self.assertEqual(findings, [], "use onError: continueRegularOutput with alwaysOutputData (single output)")

    def test_exemptions_are_exact_files_with_a_reason(self):
        self.assertEqual(set(EXEMPT), {"energygrid_invoice_delivery.workflow.json"})
        for name, reason in EXEMPT.items():
            self.assertNotIn("*", name)
            self.assertNotIn("/", name)
            self.assertIn("remove the exemption if restored", reason)

    def test_the_guard_catches_each_multi_output_shape(self):
        # Mutation proof: a clean single-output read of the daily health export, turned multi-output in
        # each of the three ways, must be reported; the repo pattern itself must pass.
        workflow = json.loads((EXPORTS / "xb_daily_health.workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(multi_output_always_output_nodes("x", workflow), [])

        def changed(mutate):
            copy = json.loads(json.dumps(workflow))
            node = next(n for n in copy["nodes"] if n["name"] == "Read Health Runs")
            self.assertEqual((node["onError"], node["alwaysOutputData"]), ("continueRegularOutput", True))
            mutate(copy, node)
            return multi_output_always_output_nodes("x", copy)

        self.assertEqual(changed(lambda w, n: n.update(onError="continueErrorOutput")), ["x: Read Health Runs"])
        self.assertEqual(changed(lambda w, n: n.update(type="n8n-nodes-base.if")), ["x: Read Health Runs"])
        self.assertEqual(changed(lambda w, n: w["connections"]["Read Health Runs"]["main"].append(
            [{"node": "Email Plan", "type": "main", "index": 0}])), ["x: Read Health Runs"])
        self.assertEqual(changed(lambda w, n: n.update(onError="continueErrorOutput", alwaysOutputData=False)), [])

    def test_the_exempt_export_is_the_known_case(self):
        # The archived invoice delivery export still holds the pattern; the guard sees it, the exemption hides it.
        # A stale exemption fails instead of skipping (#334 G2 L3): when the export is removed or no longer
        # holds the pattern, remove its EXEMPT entry in the same change.
        for name in EXEMPT:
            path = EXPORTS / name
            with self.subTest(name=name):
                self.assertTrue(path.is_file(), "%s is gone: remove its EXEMPT entry" % name)
                found = multi_output_always_output_nodes(name, json.loads(path.read_text(encoding="utf-8")))
                self.assertGreaterEqual(len(found), 1, "%s no longer needs the exemption: remove it" % name)


if __name__ == "__main__":
    unittest.main()
