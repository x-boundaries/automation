"""Repo-wide guard (#288 T2): no n8n workflow export sends Telegram.

Owner, 19 Nov 2026: "remove all telegram notifications. stick purely to email notifications to me."
Every operations notice goes through the shared `XB Ops Alert Email` sub-workflow instead.

The guard reads every n8n workflow export under `n8n-workflows/` (any `*.json` holding a `nodes` list)
and fails on a Telegram node (any `n8n-nodes-base.telegram*` type) or a `telegramApi` credential
binding or reference anywhere in the file. Offline: files only, fake data only.
"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "n8n-workflows"

# Exact file names that may still hold Telegram, each with its reason. Empty on purpose: the SIMBA
# exports on main hold no Telegram node (checked 20 Nov 2026). The SIMBA workflows on the PR #265
# branch do; they are converted to email in that lane (#288 T3) before any activation, and this guard
# then covers them too.
EXEMPT = {}


def workflow_exports():
    """(relative path, parsed export, raw text) for every n8n workflow export in the repo."""

    found = []
    for path in sorted(EXPORTS.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            found.append((path.relative_to(EXPORTS).as_posix(), data, text))
    return found


def telegram_findings(name, workflow, text):
    """Every Telegram use in one export, as short strings (empty when clean)."""

    findings = []
    for node in workflow["nodes"]:
        if str(node.get("type", "")).lower().startswith("n8n-nodes-base.telegram"):
            findings.append("%s: node %r is %s" % (name, node.get("name"), node.get("type")))
        if "telegramApi" in (node.get("credentials") or {}):
            findings.append("%s: node %r binds telegramApi" % (name, node.get("name")))
    if "telegramApi" in text and not any("binds telegramApi" in f for f in findings):
        findings.append("%s: mentions telegramApi" % name)
    return findings


class NoTelegramTests(unittest.TestCase):
    def test_exports_were_found(self):
        names = [name for name, _, _ in workflow_exports()]
        self.assertIn("xb_daily_health.workflow.json", names)
        self.assertIn("ops_alert_email.workflow.json", names)
        self.assertGreaterEqual(len(names), 10)

    def test_no_export_holds_a_telegram_node_or_binding(self):
        findings = []
        for name, workflow, text in workflow_exports():
            if name not in EXEMPT:
                findings += telegram_findings(name, workflow, text)
        self.assertEqual(findings, [], "Telegram is retired (#288, email only); use XB Ops Alert Email")

    def test_exemptions_are_exact_files_with_a_reason(self):
        for name, reason in EXEMPT.items():
            self.assertTrue((EXPORTS / name).is_file(), name)
            self.assertNotIn("*", name)
            self.assertGreater(len(reason.strip()), 10, name)

    def test_the_guard_catches_a_telegram_node_and_a_binding(self):
        # Mutation proof: the clean daily health export with one Telegram node, or one telegramApi
        # binding on an existing node, must be reported.
        workflow = json.loads((EXPORTS / "xb_daily_health.workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(telegram_findings("x", workflow, json.dumps(workflow)), [])
        node = {"id": "00000000-0000-4000-8000-000000000001", "name": "Send Fake Alert",
                "type": "n8n-nodes-base.telegram", "typeVersion": 1.2, "position": [0, 0],
                "parameters": {"chatId": "REPLACE_WITH_FAKE_CHAT_ID", "text": "fake"}}
        mutated = dict(workflow, nodes=workflow["nodes"] + [node])
        self.assertEqual(telegram_findings("x", mutated, json.dumps(mutated)),
                         ["x: node 'Send Fake Alert' is n8n-nodes-base.telegram"])
        bound = json.loads(json.dumps(workflow))
        bound["nodes"][0]["credentials"] = {"telegramApi": {"id": "fakeCredential01", "name": "Fake"}}
        self.assertEqual(len(telegram_findings("x", bound, json.dumps(bound))), 1)
        trigger = dict(workflow, nodes=[dict(node, type="n8n-nodes-base.telegramTrigger")])
        self.assertEqual(len(telegram_findings("x", trigger, json.dumps(trigger))), 1)


if __name__ == "__main__":
    unittest.main()
