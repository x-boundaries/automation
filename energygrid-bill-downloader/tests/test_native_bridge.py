"""Native Claude connector bridge (#226 V3 G1): offline protocol, recovery and refusal tests.

A `FakeNativeHost` plays the Claude host and the claude.ai Drive and Gmail connectors
locally. It can lose answers, crash between steps and corrupt the base64 it re-emits,
which is the real fidelity risk of a model-carried payload. Synthetic data only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from energygrid_bill_downloader import native_bridge as bridge
from energygrid_bill_downloader.errors import ConfigError, OfflineV4Error
from energygrid_bill_downloader.native_bridge import NativeRefusal
from energygrid_bill_downloader.state import StateV4Store, migrate_state_database_v4
from fixtures.synthetic_dual_stream import SyntheticAdapter, candidate, create_v3_database, snapshot

EB, TN = "EB_BILL", "TENANT_BILL"
FOLDERS = {EB: "FOLDER_EB_SYNTHETIC_0001", TN: "FOLDER_TN_SYNTHETIC_0002"}
RECIPIENT = "recipient@example.com"
REPO_ROOT = Path(__file__).resolve().parents[2]


def pdf(label: str) -> bytes:
    return bridge.synthetic_pdf(label)


def corrupt(encoded: str) -> str:
    """Same length, different bytes: a size check alone cannot see it."""
    index = len(encoded) // 2
    replacement = "B" if encoded[index] != "B" else "C"
    return encoded[:index] + replacement + encoded[index + 1:]


class FakeNativeHost:
    """The Claude host with in-memory Drive and Gmail connectors."""

    def __init__(self, test: "NativeBridgeBase") -> None:
        self.test = test
        self.drive: dict[str, dict] = {}
        self.drafts: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.corrupt_upload = False
        self.corrupt_attachment = False
        self.counter = 0

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter:012d}"

    # -- connectors ----------------------------------------------------
    def search(self, folder: str, title: str) -> list[dict]:
        return [{"id": key, "title": row["title"], "parentId": row["parent"]}
                for key, row in self.drive.items() if row["parent"] == folder and row["title"] == title]

    def create_file(self, call: dict) -> dict:
        encoded = Path(call["base64Content_path"]).read_text()
        if self.corrupt_upload:
            encoded = corrupt(encoded)
        data = base64.b64decode(encoded)
        file_id = self._id("DRIVEFILE")
        self.drive[file_id] = {"title": call["title"], "parent": call["parentId"], "bytes": data}
        return {"id": file_id, "title": call["title"], "parentId": call["parentId"],
                "mimeType": call["contentMimeType"], "fileSize": str(len(data))}

    def download(self, file_id: str) -> str:
        return base64.b64encode(self.drive[file_id]["bytes"]).decode()

    def create_draft(self, step: dict, **overrides) -> str:
        encoded = Path(step["payload_base64_path"]).read_text()
        if self.corrupt_attachment:
            encoded = corrupt(encoded)
        draft_id = self._id("DRAFT")
        self.drafts[draft_id] = {
            "from": overrides.get("sender", step["gmail_account"]),
            "to": overrides.get("to", step["to"]), "cc": overrides.get("cc", []), "bcc": [],
            "subject": overrides.get("subject", step["subject"]),
            "attachments": overrides.get("attachments", [
                {"filename": step["display_filename"], "mimeType": "application/pdf",
                 "bytes": base64.b64decode(encoded)}]),
        }
        return draft_id

    def draft_evidence(self, draft_id: str) -> dict:
        draft = self.drafts[draft_id]
        return {"draft_id": draft_id, "from": draft["from"], "to": draft["to"], "cc": draft["cc"], "bcc": draft["bcc"],
                "subject": draft["subject"],
                "attachments": [{"filename": item["filename"], "mimeType": item["mimeType"],
                                 "size": len(item["bytes"])} for item in draft["attachments"]]}

    def send_draft(self, draft_id: str) -> str:
        message_id = self._id("MSG")
        self.sent.append({"message_id": message_id, **self.drafts.pop(draft_id)})
        return message_id

    # -- one complete protocol step ------------------------------------
    def drive_step(self, step: dict, *, crash_after_seal=False, crash_after_create=False) -> dict:
        t = self.test
        intent = step["intent_id"]
        evidence = {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"],
                    "matches": self.search(step["folder_id"], step["display_filename"])}
        sealed = t.call(bridge.drive_seal, intent, evidence)
        if sealed["outcome"] != "SEALED" or crash_after_seal:
            return sealed
        created = self.create_file(sealed["create_file"])
        if crash_after_create:
            return {"outcome": "CRASHED", "file": created}
        t.readback("drive", intent, created["id"], self.download(created["id"]))
        return t.call(bridge.drive_record, intent, {"outcome": "CREATED", "file": created})

    def gmail_step(self, step: dict, *, crash_after_seal=False, **draft_overrides) -> dict:
        t = self.test
        intent = step["intent_id"]
        draft_id = self.create_draft(step, **draft_overrides)
        attachments = self.drafts[draft_id]["attachments"]
        if attachments:
            t.readback("gmail", intent, draft_id, base64.b64encode(attachments[0]["bytes"]).decode())
        sealed = t.call(bridge.gmail_seal, intent, self.draft_evidence(draft_id))
        if crash_after_seal:
            return sealed
        message_id = self.send_draft(sealed["send_draft_id"])
        return t.call(bridge.gmail_record, intent,
                      {"outcome": "SENT", "draft_id": sealed["send_draft_id"], "message_id": message_id})

    def drain(self, limit: int = 12) -> list[dict]:
        results = []
        for _ in range(limit):
            step = self.test.next()
            if step["outcome"] == "HOLD":
                # The Core held that step and stopped its stream; plan again.
                results.append(step)
                continue
            if step["outcome"] not in ("DRIVE_PRECHECK", "GMAIL_DRAFT"):
                results.append(step)
                break
            results.append(self.drive_step(step) if step["outcome"] == "DRIVE_PRECHECK" else self.gmail_step(step))
        return results


class NativeBridgeBase(unittest.TestCase):
    readback_required = True
    ceilings = (4, 4)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.db = root / "state" / "state.sqlite"
        self.db.parent.mkdir()
        self.cfg = self.make_config()
        for path in (self.cfg.archive_root, self.cfg.temp_root, self.cfg.outbox_root):
            path.mkdir()
        self.clock = 1_000_000
        self.host = FakeNativeHost(self)
        self.prepare_database()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_config(self, **changes) -> bridge.NativeConfig:
        values = dict(
            mode=bridge.MODE_UAT_SYNTHETIC, state_path=self.db, archive_root=self.root / "archive",
            named_archive_root=self.root / "bills",
            temp_root=self.root / "temp", outbox_root=self.root / "outbox",
            drive=bridge.NativeDriveSettings(account="uat@example.com", folder_ids=dict(FOLDERS)),
            gmail=bridge.NativeGmailSettings(
                account="uat@example.com", to=(RECIPIENT,), subject_template="EnergyGrid bill: {display_filename}",
                body_template="Attached {display_filename} ({stream_label}).",
                require_attachment_readback=self.readback_required),
            max_drive_uploads=self.ceilings[0], max_gmail_sends=self.ceilings[1], synthetic_root=self.root / "portal",
        )
        values.update(changes)
        return bridge.NativeConfig(**values)

    def prepare_database(self) -> None:
        create_v3_database(self.db)
        migrate_state_database_v4(self.db, apply=True)

    def adapters(self, listing: dict | None = None) -> dict:
        from energygrid_bill_downloader.invoice import Stream

        listing = listing or {EB: [("EB Invoice 0001.pdf", "2026-08-01"), ("EB Invoice 0002.pdf", "2026-09-01")],
                              TN: [("TN Invoice 0001.pdf", "2026-08-03"), ("TN Invoice 0002.pdf", "2026-09-03")]}
        self.portal = {}
        for stream, items in listing.items():
            files = {name: pdf(f"{stream} {name}") for name, _day in items}
            self.portal[stream] = SyntheticAdapter(
                snapshot(Stream(stream), tuple(candidate(Stream(stream), name=name, invoice_date=day) for name, day in items)),
                files,
            )
        return self.portal

    # -- bridge calls ------------------------------------------------------
    def tick(self) -> int:
        self.clock += 1000
        return self.clock

    def call(self, function, intent_id: str, evidence: dict, cfg=None) -> dict:
        with StateV4Store(self.db) as state:
            return function(state, cfg or self.cfg, intent_id, evidence, run_id=bridge.new_run_id(), now_ms=self.tick())

    def acquire(self, adapters=None) -> dict:
        with StateV4Store(self.db) as state:
            return bridge.acquire(state, self.cfg, adapters if adapters is not None else self.adapters(),
                                  run_id=bridge.new_run_id(), now_ms=self.tick())

    def next(self, cfg=None) -> dict:
        with StateV4Store(self.db) as state:
            return bridge.next_step(state, cfg or self.cfg, run_id=bridge.new_run_id(), now_ms=self.tick())

    def readback(self, kind: str, intent: str, provider_id: str, encoded: str) -> dict:
        path = self.root / f"readback-{kind}-{provider_id}.b64"
        path.write_text(encoded)
        with StateV4Store(self.db) as state:
            function = bridge.drive_readback if kind == "drive" else bridge.gmail_readback
            return function(state, self.cfg, intent, provider_id, path)

    def status(self) -> dict:
        with StateV4Store(self.db) as state:
            return bridge.status(state, self.cfg)

    def intent_state(self, intent_id: str) -> str:
        with StateV4Store(self.db) as state:
            return state.intent(intent_id)["state"]

    def dispatches(self) -> tuple[int, int]:
        with StateV4Store(self.db) as state:
            return state.dispatch_count("DRIVE_UPLOAD"), state.dispatch_count("GMAIL_SEND")


class CompleteJourneyTests(NativeBridgeBase):
    def test_both_streams_deliver_latest_once_with_one_prefix_and_no_backfill(self) -> None:
        acquired = self.acquire()
        self.assertEqual({EB: "ADMITTED", TN: "ADMITTED"},
                         {stream: row["status"] for stream, row in acquired["acquisition"].items()})
        results = self.host.drain()
        self.assertEqual(["VERIFIED"] * 4 + ["IDLE"], [row["outcome"] for row in results])
        # Fair interleaving: Drive for each stream before either Gmail, Drive before Gmail per invoice.
        self.assertEqual(
            sorted(["SEP 2026 - EB Invoice 0002.pdf", "SEP 2026 - TN Invoice 0002.pdf"]),
            sorted(row["title"] for row in self.host.drive.values()),
        )
        self.assertEqual({FOLDERS[EB], FOLDERS[TN]}, {row["parent"] for row in self.host.drive.values()})
        self.assertEqual(2, len(self.host.sent))
        for message in self.host.sent:
            self.assertEqual([RECIPIENT], message["to"])
            (attachment,) = message["attachments"]
            self.assertEqual(1, attachment["filename"].count(" 2026 - "))
            self.assertTrue(attachment["filename"].startswith("SEP 2026 - "))
            self.assertEqual(attachment["bytes"], pdf(f"{'EB_BILL' if 'EB' in attachment['filename'] else 'TENANT_BILL'} "
                                                      f"{attachment['filename'][len('SEP 2026 - '):]}"))
            self.assertEqual(f"EnergyGrid bill: {attachment['filename']}", message["subject"])
        # Only the latest invoice was ever fetched; the older one is never a backfill candidate.
        self.assertEqual(["EB Invoice 0002.pdf"], self.portal[EB].acquire_calls)
        self.assertEqual(["TN Invoice 0002.pdf"], self.portal[TN].acquire_calls)
        status = self.status()
        self.assertEqual("DRAINED", status["outcome"])
        self.assertEqual({"drive_dispatches": 2, "gmail_dispatches": 2}, status["effects"])
        (eb_invoice,) = status["streams"][EB]["invoices"]
        self.assertEqual("VERIFIED", eb_invoice["drive_upload"]["state"])
        self.assertTrue(eb_invoice["drive_upload"]["receipt"].startswith("gdrive:DRIVEFILE"))
        self.assertRegex(eb_invoice["gmail_send"]["receipt"], r"^gmail:MSG\d+;draft:DRAFT\d+$")
        self.assertFalse(eb_invoice["adopted_from_v3"])

    def test_immediate_second_run_has_zero_fetches_and_zero_effects(self) -> None:
        self.acquire()
        self.host.drain()
        again = self.acquire()
        self.assertEqual({EB: "ALREADY_ADMITTED", TN: "ALREADY_ADMITTED"},
                         {stream: row["status"] for stream, row in again["acquisition"].items()})
        self.assertEqual([], self.portal[EB].acquire_calls + self.portal[TN].acquire_calls)
        self.assertEqual("IDLE", self.next()["outcome"])
        self.assertEqual((2, 2), self.dispatches())
        self.assertEqual(2, len(self.host.sent))

    def test_a_newer_invoice_on_a_later_run_is_delivered_alone(self) -> None:
        self.acquire()
        self.host.drain()
        newer = {EB: [("EB Invoice 0002.pdf", "2026-09-01"), ("EB Invoice 0003.pdf", "2026-10-01")],
                 TN: [("TN Invoice 0002.pdf", "2026-09-03")]}
        result = self.acquire(self.adapters(newer))
        self.assertEqual("ADMITTED", result["acquisition"][EB]["status"])
        self.assertEqual("ALREADY_ADMITTED", result["acquisition"][TN]["status"])
        self.host.drain()
        self.assertEqual(3, len(self.host.sent))
        self.assertEqual("OCT 2026 - EB Invoice 0003.pdf", self.host.sent[-1]["attachments"][0]["filename"])

    def test_payload_file_is_the_exact_archive_bytes(self) -> None:
        self.acquire()
        step = self.next()
        raw = base64.b64decode(Path(step["payload_base64_path"]).read_text())
        self.assertEqual(step["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(step["byte_size"], len(raw))


class DriveSafetyTests(NativeBridgeBase):
    def test_occupied_drive_name_holds_without_seal_and_the_other_stream_continues(self) -> None:
        self.acquire()
        step = self.next()
        self.host.drive["FOREIGNFILE0001"] = {"title": step["display_filename"], "parent": step["folder_id"], "bytes": b"x"}
        result = self.host.drive_step(step)
        self.assertEqual(("HOLD", "EG_NATIVE_DRIVE_NAME_OCCUPIED"), (result["outcome"], result["support_ref"]))
        self.assertEqual((0, 0), self.dispatches())
        self.host.drain()
        status = self.status()["streams"]
        self.assertEqual("BLOCKED", status[step["stream"]]["state"])
        other = TN if step["stream"] == EB else EB
        self.assertEqual("DRAINED", status[other]["state"])
        self.assertEqual(1, len(self.host.sent))

    def test_crash_after_seal_becomes_unknown_is_never_resent_and_a_late_receipt_resolves_it(self) -> None:
        self.acquire()
        step = self.next()
        intent = step["intent_id"]
        crashed = self.host.drive_step(step, crash_after_create=True)
        self.assertEqual("CRASHED", crashed["outcome"])
        later = self.next()
        self.assertEqual(1, later["recovered_unknown"])
        self.assertEqual("UNKNOWN", self.intent_state(intent))
        self.assertNotEqual(intent, later["intent_id"])
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_seal, intent, {"folder_id": step["folder_id"], "title": step["display_filename"],
                                                   "query": step["precheck_query"], "complete": True, "matches": []})
        self.assertEqual("EG_NATIVE_INTENT_NOT_PENDING", refused.exception.support_ref)
        self.assertEqual(1, len(self.host.drive))
        # The operator (host) later finds the file it created and proves its bytes.
        file = crashed["file"]
        self.readback("drive", intent, file["id"], self.host.download(file["id"]))
        late = self.call(bridge.drive_record, intent, {"outcome": "CREATED", "file": file})
        self.assertEqual("VERIFIED", late["outcome"])
        self.host.drive_step(later)
        self.host.drain()
        self.assertEqual(2, len(self.host.drive))
        self.assertEqual(2, len(self.host.sent))

    def test_lost_answer_is_unknown_and_blocks_only_its_stream(self) -> None:
        self.acquire()
        step = self.next()
        sealed = self.host.drive_step(step, crash_after_seal=True)
        self.assertEqual("SEALED", sealed["outcome"])
        self.assertEqual("UNKNOWN", self.call(bridge.drive_record, step["intent_id"], {"outcome": "NO_RESPONSE"})["outcome"])
        self.host.drain()
        streams = self.status()["streams"]
        self.assertEqual(("BLOCKED", "EG_V4_PROVIDER_UNKNOWN"),
                         (streams[step["stream"]]["state"], streams[step["stream"]]["support_ref"]))
        self.assertEqual(1, len(self.host.sent))

    def test_size_preserving_upload_corruption_is_caught_by_readback_and_held(self) -> None:
        self.acquire()
        step = self.next()
        self.host.corrupt_upload = True
        result = self.host.drive_step(step)
        self.assertEqual("HOLD", result["outcome"])
        self.assertEqual("HOLD", self.intent_state(step["intent_id"]))
        self.host.corrupt_upload = False
        self.host.drain()
        # No email is ever sent for the invoice whose Drive copy is wrong.
        self.assertNotIn(step["display_filename"],
                         [message["attachments"][0]["filename"] for message in self.host.sent])

    def test_record_without_readback_is_refused_and_writes_nothing(self) -> None:
        self.acquire()
        step = self.next()
        sealed = self.host.drive_step(step, crash_after_seal=True)
        created = self.host.create_file(sealed["create_file"])
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_record, step["intent_id"], {"outcome": "CREATED", "file": created})
        self.assertEqual("EG_NATIVE_DRIVE_READBACK_REQUIRED", refused.exception.support_ref)
        self.assertEqual("DISPATCHED", self.intent_state(step["intent_id"]))

    def test_metadata_mismatch_is_rejected_and_held(self) -> None:
        for field, value in (("parentId", "FOLDER_SOMEWHERE_ELSE_01"), ("title", "renamed.pdf"),
                             ("mimeType", "application/vnd.google-apps.document"), ("fileSize", "1")):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self.acquire()
                step = self.next()
                sealed = self.host.drive_step(step, crash_after_seal=True)
                created = self.host.create_file(sealed["create_file"]) | {field: value}
                self.readback("drive", step["intent_id"], created["id"], self.host.download(created["id"]))
                result = self.call(bridge.drive_record, step["intent_id"], {"outcome": "CREATED", "file": created})
                self.assertEqual(("HOLD", "EG_NATIVE_DRIVE_METADATA_MISMATCH"), (result["outcome"], result["support_ref"]))

    def test_denied_native_call_holds_and_error_is_unknown(self) -> None:
        for outcome, expected in (("DENIED", "HOLD"), ("ERROR", "UNKNOWN")):
            with self.subTest(outcome=outcome):
                self.tearDown()
                self.setUp()
                self.acquire()
                step = self.next()
                self.host.drive_step(step, crash_after_seal=True)
                result = self.call(bridge.drive_record, step["intent_id"], {"outcome": outcome})
                self.assertEqual(expected, result["outcome"])
                self.assertEqual(expected, self.intent_state(step["intent_id"]))

    def test_two_hosts_racing_the_same_step_seal_once(self) -> None:
        self.acquire()
        step = self.next()
        evidence = {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"], "matches": []}
        self.assertEqual("SEALED", self.call(bridge.drive_seal, step["intent_id"], evidence)["outcome"])
        with self.assertRaises(NativeRefusal):
            self.call(bridge.drive_seal, step["intent_id"], evidence)
        self.assertEqual((1, 0), self.dispatches())

    def test_incomplete_or_foreign_precheck_is_refused(self) -> None:
        self.acquire()
        step = self.next()
        base = {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                "query": step["precheck_query"], "matches": []}
        for change in ({"complete": False}, {"folder_id": "FOLDER_SOMEWHERE_ELSE_01"}, {"title": "x.pdf"},
                       {"matches": None}, {"query": "parentId = 'x'"}):
            with self.subTest(change=change), self.assertRaises(NativeRefusal) as refused:
                self.call(bridge.drive_seal, step["intent_id"], base | change)
            self.assertEqual("EG_NATIVE_PRECHECK_EVIDENCE_INVALID", refused.exception.support_ref)
        self.assertEqual((0, 0), self.dispatches())

    def test_tampered_archive_holds_before_any_seal(self) -> None:
        self.acquire()
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(EB)[0]
        (self.cfg.archive_root / invoice["archive_relpath"]).write_bytes(pdf("tampered"))
        results = self.host.drain()
        self.assertIn("HOLD", [row["outcome"] for row in results])
        streams = self.status()["streams"]
        self.assertEqual(("BLOCKED", "EG_V4_ARCHIVE_IDENTITY_CHANGED"),
                         (streams[EB]["state"], streams[EB]["support_ref"]))
        self.assertEqual("DRAINED", streams[TN]["state"])


def make_junction(link: Path, target: Path) -> bool:
    """Create a Windows directory junction (no elevation needed). False where unsupported."""
    import os
    import subprocess

    if os.name != "nt":
        return False
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    return result.returncode == 0


class JunctionSafetyTests(NativeBridgeBase):
    """O1: a reparse point in a private path is refused at config load and HOLDs at send time."""

    def test_config_refuses_a_junction_in_a_private_path(self) -> None:
        target = self.root / "real-outbox"
        target.mkdir()
        if not make_junction(self.root / "junction-outbox", target):
            self.skipTest("directory junctions are unavailable on this host")
        raw = ConfigTests().raw(self.root)
        raw["outbox_root"] = str(self.root / "junction-outbox" / "o")
        with self.assertRaises(ConfigError):
            bridge.load_native_config(raw, checkout_root=REPO_ROOT)

    def test_a_junction_planted_in_the_archive_after_admission_holds_only_that_stream(self) -> None:
        self.acquire()
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(EB)[0]
        folder = (self.cfg.archive_root / invoice["archive_relpath"]).parent
        moved = self.root / "moved-eb"
        folder.rename(moved)
        if not make_junction(folder, moved):
            self.skipTest("directory junctions are unavailable on this host")
        self.host.drain()
        streams = self.status()["streams"]
        self.assertEqual(("BLOCKED", "EG_V4_ARCHIVE_UNREADABLE"), (streams[EB]["state"], streams[EB]["support_ref"]))
        self.assertEqual("DRAINED", streams[TN]["state"])
        self.assertEqual(0, sum(1 for row in self.host.drive.values() if row["parent"] == FOLDERS[EB]))


class GmailSafetyTests(NativeBridgeBase):
    def drive_done(self) -> dict:
        self.acquire()
        while True:
            step = self.next()
            if step["outcome"] == "GMAIL_DRAFT":
                return step
            self.host.drive_step(step)

    def test_wrong_draft_is_refused_without_seal_and_a_correct_draft_then_seals(self) -> None:
        step = self.drive_done()
        wrong = (
            {"to": ["someone.else@example.com"]},
            {"to": [RECIPIENT, "extra@example.com"]},
            {"cc": ["cc@example.com"]},
            {"subject": "Something else"},
            {"attachments": [{"filename": "invoice.pdf", "mimeType": "application/pdf", "bytes": pdf("x")}]},
            {"attachments": []},
            {"sender": "someone.else@example.com"},
        )
        for overrides in wrong:
            with self.subTest(overrides=list(overrides)), self.assertRaises(NativeRefusal):
                self.host.gmail_step(step, **overrides)
        self.assertEqual("PENDING", self.intent_state(step["intent_id"]))
        self.assertEqual(0, self.dispatches()[1])
        self.assertEqual("VERIFIED", self.host.gmail_step(step)["outcome"])
        self.assertEqual(1, len(self.host.sent))

    def test_size_preserving_attachment_corruption_is_refused_before_send(self) -> None:
        step = self.drive_done()
        self.host.corrupt_attachment = True
        with self.assertRaises(NativeRefusal) as refused:
            self.host.gmail_step(step)
        self.assertEqual("EG_NATIVE_DRAFT_BYTES_MISMATCH", refused.exception.support_ref)
        self.assertEqual([], self.host.sent)
        self.host.corrupt_attachment = False
        self.assertEqual("VERIFIED", self.host.gmail_step(step)["outcome"])

    def test_seal_requires_readback_when_configured(self) -> None:
        step = self.drive_done()
        draft = self.host.create_draft(step)
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.gmail_seal, step["intent_id"], self.host.draft_evidence(draft))
        self.assertEqual("EG_NATIVE_GMAIL_READBACK_REQUIRED", refused.exception.support_ref)

    def test_only_the_bound_draft_is_recorded_and_replay_is_a_no_op(self) -> None:
        step = self.drive_done()
        sealed = self.host.gmail_step(step, crash_after_seal=True)
        other = self.host.create_draft(step)
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.gmail_record, step["intent_id"], {"outcome": "SENT", "draft_id": other, "message_id": "MSG1"})
        self.assertEqual("EG_NATIVE_DRAFT_NOT_BOUND", refused.exception.support_ref)
        message = self.host.send_draft(sealed["send_draft_id"])
        evidence = {"outcome": "SENT", "draft_id": sealed["send_draft_id"], "message_id": message}
        self.assertEqual("VERIFIED", self.call(bridge.gmail_record, step["intent_id"], evidence)["outcome"])
        self.assertEqual("REPLAY", self.call(bridge.gmail_record, step["intent_id"], evidence)["outcome"])

    def test_crash_after_gmail_seal_is_unknown_and_never_sent_again(self) -> None:
        step = self.drive_done()
        self.host.gmail_step(step, crash_after_seal=True)
        nxt = self.next()
        self.assertEqual(1, nxt["recovered_unknown"])
        self.assertEqual("UNKNOWN", self.intent_state(step["intent_id"]))
        self.host.drain()
        self.assertNotIn(step["display_filename"], [m["attachments"][0]["filename"] for m in self.host.sent])
        with self.assertRaises(NativeRefusal):
            self.host.gmail_step(step)

    def test_gmail_cannot_be_sealed_before_its_drive_upload_is_verified(self) -> None:
        self.acquire()
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(EB)[0]
            gmail = state.intents_for_invoice(invoice["invoice_key"])["GMAIL_SEND"]
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.gmail_seal, gmail["intent_id"], {"draft_id": "DRAFT1", "to": [RECIPIENT], "cc": [],
                                                             "bcc": [], "subject": "x", "attachments": []})
        self.assertEqual("EG_NATIVE_STEP_NOT_PLANNED", refused.exception.support_ref)


class GmailWithoutReadbackTests(NativeBridgeBase):
    readback_required = False

    def test_owner_policy_without_attachment_readback_seals_on_envelope_evidence(self) -> None:
        self.acquire()
        self.host.drain()
        self.assertEqual(2, len(self.host.sent))


class EffectCeilingTests(NativeBridgeBase):
    ceilings = (1, 1)

    def test_a_stream_at_its_ceiling_never_starves_the_other_action(self) -> None:
        self.acquire()
        results = self.host.drain()
        # EB Drive, then EB Gmail (the Tenant Drive step is at the Drive ceiling and is skipped).
        self.assertEqual(["VERIFIED", "VERIFIED", "CEILING_REACHED"], [row["outcome"] for row in results])
        self.assertEqual(0, results[-1]["exit_code"])
        self.assertEqual((1, 1), self.dispatches())
        self.assertEqual(1, len(self.host.sent))
        streams = self.status()["streams"]
        self.assertEqual("DRAINED", streams[EB]["state"])
        self.assertEqual(("PENDING", "DRIVE_UPLOAD"), (streams[TN]["state"], streams[TN]["next_action"]))

    def test_a_seal_at_the_ceiling_is_refused_without_state_change(self) -> None:
        self.acquire()
        self.host.drain()
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(TN)[0]
            drive = state.intents_for_invoice(invoice["invoice_key"])["DRIVE_UPLOAD"]
        evidence = {"folder_id": FOLDERS[TN], "title": invoice["display_filename"], "complete": True,
                    "query": bridge.drive_title_query(FOLDERS[TN], invoice["display_filename"]), "matches": []}
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_seal, drive["intent_id"], evidence)
        self.assertEqual("EG_NATIVE_EFFECT_CEILING_REACHED", refused.exception.support_ref)
        self.assertEqual("PENDING", self.intent_state(drive["intent_id"]))


class ZeroCeilingTests(NativeBridgeBase):
    ceilings = (0, 0)

    def test_zero_ceiling_plans_nothing_and_seals_nothing(self) -> None:
        self.acquire()
        step = self.next()
        self.assertEqual(("CEILING_REACHED", 0), (step["outcome"], step["exit_code"]))
        self.assertEqual((0, 0), self.dispatches())


class ReviewFindingTests(NativeBridgeBase):
    """Regressions for the Builder-side adversarial findings on 9d3a772."""

    def test_readback_for_a_case_variant_file_id_does_not_verify(self) -> None:
        self.acquire()
        step = self.next()
        sealed = self.host.drive_step(step, crash_after_seal=True)
        created = self.host.create_file(sealed["create_file"])
        self.readback("drive", step["intent_id"], created["id"], self.host.download(created["id"]))
        variant = created | {"id": created["id"].lower()}
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_record, step["intent_id"], {"outcome": "CREATED", "file": variant})
        self.assertEqual("EG_NATIVE_DRIVE_READBACK_REQUIRED", refused.exception.support_ref)
        self.assertEqual("DISPATCHED", self.intent_state(step["intent_id"]))

    def test_status_shows_no_receipt_for_a_held_intent(self) -> None:
        self.acquire()
        while True:
            step = self.next()
            if step["outcome"] == "GMAIL_DRAFT":
                break
            self.host.drive_step(step)
        sealed = self.host.gmail_step(step, crash_after_seal=True)
        self.assertEqual("HOLD", self.call(bridge.gmail_record, step["intent_id"], {"outcome": "DENIED"})["outcome"])
        late = {"outcome": "SENT", "draft_id": sealed["send_draft_id"], "message_id": "MSG000000000099"}
        self.call(bridge.gmail_record, step["intent_id"], late)
        self.assertEqual("HOLD", self.intent_state(step["intent_id"]))
        invoice = self.status()["streams"][step["stream"]]["invoices"][0]
        self.assertEqual(("HOLD", None), (invoice["gmail_send"]["state"], invoice["gmail_send"]["receipt"]))

    def test_precheck_query_escapes_quotes_and_backslashes(self) -> None:
        title = "O'Brien \\ x.pdf"
        expected = "parentId = 'F1' and title = 'O\\'Brien \\\\ x.pdf'"
        self.assertEqual(expected, bridge.drive_title_query("F1", title))

    def test_a_display_name_with_an_apostrophe_runs_end_to_end(self) -> None:
        self.acquire(self.adapters({EB: [("O'Brien EB Invoice.pdf", "2026-09-01")], TN: []}))
        step = self.next()
        self.assertIn("title = 'SEP 2026 - O\\'Brien EB Invoice.pdf'", step["precheck_query"])
        self.host.drain()
        self.assertEqual(["SEP 2026 - O'Brien EB Invoice.pdf"],
                         [message["attachments"][0]["filename"] for message in self.host.sent])

    def test_synthetic_fixture_refuses_path_like_filenames(self) -> None:
        for name in ("../escape.pdf", "sub/dir.pdf", "..", ""):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                bridge.write_synthetic_fixture(self.root / "portal-x", {EB: [(name, "2026-09-01")]})


class NamedArchiveTests(NativeBridgeBase):
    """Owner requirement: the user-facing archive, Drive and email all use `MMM YYYY - <original>.pdf`."""

    def named(self, stream: str, name: str) -> Path:
        return self.cfg.named_archive_root / bridge.STREAM_LABELS[stream] / name

    def test_user_facing_archive_uses_the_display_name_and_internal_identity_stays_date_only(self) -> None:
        acquired = self.acquire()
        self.assertEqual({EB: ["PRESENT"], TN: ["PRESENT"]},
                         {k: [row["named_archive"] for row in v] for k, v in acquired["named_archive"].items()})
        self.host.drain()
        for stream, original, day in ((EB, "EB Invoice 0002.pdf", "2026-09-01"), (TN, "TN Invoice 0002.pdf", "2026-09-03")):
            with self.subTest(stream=stream):
                named = self.named(stream, f"SEP 2026 - {original}")
                self.assertEqual(pdf(f"{stream} {original}"), named.read_bytes())
                # Exactly one file per stream: the older invoice is never backfilled, prefix applied once.
                self.assertEqual([named.name], [item.name for item in named.parent.iterdir()])
                internal = self.cfg.archive_root / bridge.STREAM_LABELS[stream] / f"{day}.pdf"
                self.assertEqual(named.read_bytes(), internal.read_bytes())
        names = {row["title"] for row in self.host.drive.values()}
        attachments = {message["attachments"][0]["filename"] for message in self.host.sent}
        self.assertEqual(names, attachments)
        self.assertEqual({"SEP 2026 - EB Invoice 0002.pdf", "SEP 2026 - TN Invoice 0002.pdf"}, names)
        report = self.status()["streams"]
        self.assertEqual(["PRESENT"], [row["named_archive"] for row in report[EB]["invoices"]])

    def test_existing_identical_copy_is_kept_and_never_rewritten(self) -> None:
        target = self.named(EB, "SEP 2026 - EB Invoice 0002.pdf")
        target.parent.mkdir(parents=True)
        target.write_bytes(pdf("EB_BILL EB Invoice 0002.pdf"))
        before = target.stat().st_mtime_ns
        self.acquire()
        self.host.drain()
        self.assertEqual(before, target.stat().st_mtime_ns)
        self.assertEqual(2, len(self.host.sent))

    def test_a_different_file_at_the_name_holds_that_invoice_and_is_never_overwritten(self) -> None:
        target = self.named(EB, "SEP 2026 - EB Invoice 0002.pdf")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"someone else's file")
        acquired = self.acquire()
        self.assertEqual(("HOLD", "EG_NATIVE_NAMED_ARCHIVE_CONFLICT"),
                         (acquired["named_archive"][EB][0]["named_archive"], acquired["named_archive"][EB][0]["support_ref"]))
        self.host.drain()
        self.assertEqual(b"someone else's file", target.read_bytes())
        streams = self.status()["streams"]
        self.assertEqual(("BLOCKED", "EG_NATIVE_NAMED_ARCHIVE_CONFLICT"), (streams[EB]["state"], streams[EB]["support_ref"]))
        self.assertEqual("DRAINED", streams[TN]["state"])
        self.assertEqual({FOLDERS[TN]}, {row["parent"] for row in self.host.drive.values()})
        self.assertEqual(1, len(self.host.sent))

    def test_a_removed_copy_is_republished_before_the_drive_step(self) -> None:
        self.acquire()
        self.named(EB, "SEP 2026 - EB Invoice 0002.pdf").unlink()
        self.host.drain()
        self.assertTrue(self.named(EB, "SEP 2026 - EB Invoice 0002.pdf").is_file())
        self.assertEqual(2, len(self.host.sent))

    def test_a_transient_publication_failure_is_a_retry_with_no_seal(self) -> None:
        from unittest import mock

        with mock.patch("energygrid_bill_downloader.publication.publish_no_replace", side_effect=OSError("disk")):
            acquired = self.acquire()
            self.assertEqual("UNAVAILABLE", acquired["named_archive"][EB][0]["named_archive"])
            step = self.next()
            self.assertEqual(("RETRY_LATER", 10, "EG_NATIVE_NAMED_ARCHIVE_UNAVAILABLE"),
                             (step["outcome"], step["exit_code"], step["support_ref"]))
            self.assertEqual("PENDING", self.intent_state(step["intent_id"]))
        self.assertEqual((0, 0), self.dispatches())
        self.assertEqual([], list(self.cfg.named_archive_root.rglob("*.tmp")))
        self.host.drain()
        self.assertEqual(2, len(self.host.sent))

    def test_seal_requires_the_named_copy(self) -> None:
        from unittest import mock

        self.acquire()
        step = self.next()
        self.named(step["stream"], step["display_filename"]).unlink()
        evidence = {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"], "matches": []}
        with mock.patch("energygrid_bill_downloader.publication.publish_no_replace", side_effect=OSError("disk")), \
                self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_seal, step["intent_id"], evidence)
        self.assertEqual("EG_NATIVE_NAMED_ARCHIVE_UNAVAILABLE", refused.exception.support_ref)
        self.assertEqual((0, 0), self.dispatches())


class LeaseTests(NativeBridgeBase):
    def test_a_live_lease_held_by_another_session_refuses_with_no_mutation(self) -> None:
        self.acquire()
        with StateV4Store(self.db) as other:
            other.acquire_lease("another-session", now_ms=self.clock + 5000, ttl_ms=600_000)
            with self.assertRaises(OfflineV4Error) as held:
                self.next()
            self.assertEqual("EG_V4_LEASE_HELD", held.exception.support_ref)
            self.assertEqual(10, held.exception.exit_code)
            other.release_lease("another-session")
        self.assertEqual((0, 0), self.dispatches())


class PredecessorCutoverTests(NativeBridgeBase):
    def prepare_database(self) -> None:
        from test_state_v4 import build_predecessor_v3

        build_predecessor_v3(self.db, self.cfg.archive_root, {EB: "ADOPTABLE", TN: "DELIVERED"})
        migrate_state_database_v4(self.db, apply=True)

    def test_v3_archived_undelivered_latest_is_adopted_without_fetch_and_delivered_once(self) -> None:
        from energygrid_bill_downloader.invoice import Stream
        from test_state_v4 import LATEST_SOURCE, predecessor_pdf

        adapters = {}
        for stream in (EB, TN):
            name, day = LATEST_SOURCE[stream]
            adapters[stream] = SyntheticAdapter(snapshot(Stream(stream), (candidate(Stream(stream), name=name, invoice_date=day),)),
                                                {name: predecessor_pdf(name)})
        result = self.acquire(adapters)
        self.assertEqual("ADOPTED", result["acquisition"][EB]["predecessor"])
        self.assertEqual("PREDECESSOR_DELIVERED", result["acquisition"][TN]["status"])
        self.assertEqual([], adapters[EB].acquire_calls + adapters[TN].acquire_calls)
        self.host.drain()
        self.assertEqual(1, len(self.host.sent))
        self.assertEqual("SEP 2026 - Invoice EB Sep.pdf", self.host.sent[0]["attachments"][0]["filename"])
        named = self.cfg.named_archive_root / "EB Bill" / "SEP 2026 - Invoice EB Sep.pdf"
        self.assertEqual(predecessor_pdf("Invoice EB Sep.pdf"), named.read_bytes())
        self.assertEqual("IDLE", self.next()["outcome"])


class ConfigTests(unittest.TestCase):
    def raw(self, root: Path) -> dict:
        return {
            "schema": bridge.NATIVE_CONFIG_SCHEMA, "mode": bridge.MODE_UAT_SYNTHETIC,
            "state_path": str(root / "s.sqlite"), "archive_root": str(root / "a"),
            "named_archive_root": str(root / "n"), "temp_root": str(root / "t"),
            "outbox_root": str(root / "o"), "synthetic_root": str(root / "p"),
            "drive": {"account": "uat@example.com", "folder_ids": dict(FOLDERS)},
            "gmail": {"account": "uat@example.com", "to": [RECIPIENT], "subject_template": "Bill {display_filename}",
                      "body_template": "Attached.", "require_attachment_readback": True},
            "effect_ceiling": {"drive_uploads": 2, "gmail_sends": 2},
        }

    def test_valid_config_loads_and_repr_hides_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = bridge.load_native_config(self.raw(Path(directory)), checkout_root=REPO_ROOT)
        self.assertEqual((RECIPIENT,), cfg.gmail.to)
        self.assertNotIn(RECIPIENT, repr(cfg))
        self.assertNotIn(FOLDERS[EB], repr(cfg))

    def test_committed_example_config_loads(self) -> None:
        example = REPO_ROOT / "energygrid-bill-downloader" / "config" / "energygrid.native_uat.example.json"
        cfg = bridge.load_native_config(json.loads(example.read_text(encoding="utf-8")), checkout_root=REPO_ROOT)
        self.assertEqual(bridge.MODE_UAT_SYNTHETIC, cfg.mode)
        self.assertTrue(cfg.gmail.require_attachment_readback)

    def test_invalid_configs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "production mode": lambda r: r.update(mode="PRODUCTION"),
                "extra key": lambda r: r.update(extra=1),
                "missing key": lambda r: r.pop("effect_ceiling"),
                "inside checkout": lambda r: r.update(outbox_root=str(REPO_ROOT / "energygrid-bill-downloader" / "x")),
                "unc path": lambda r: r.update(archive_root="\\\\server\\share\\archive"),
                "relative path": lambda r: r.update(temp_root="temp"),
                "same folder": lambda r: r["drive"]["folder_ids"].update(TENANT_BILL=FOLDERS[EB]),
                "bad recipient": lambda r: r["gmail"].update(to=["not-an-email"]),
                "no recipients": lambda r: r["gmail"].update(to=[]),
                "duplicate recipient": lambda r: r["gmail"].update(to=[RECIPIENT, RECIPIENT.upper()]),
                "subject without name": lambda r: r["gmail"].update(subject_template="Bill"),
                "readback not bool": lambda r: r["gmail"].update(require_attachment_readback="yes"),
                "ceiling too high": lambda r: r["effect_ceiling"].update(drive_uploads=21),
                "ceiling not int": lambda r: r["effect_ceiling"].update(gmail_sends=True),
                "portal key in synthetic mode": lambda r: r.update(portal_config_path=str(root / "c.json")),
                "named root missing": lambda r: r.pop("named_archive_root"),
                "named root equals archive": lambda r: r.update(named_archive_root=r["archive_root"]),
                "named root inside archive": lambda r: r.update(named_archive_root=str(Path(r["archive_root"]) / "x")),
                "archive inside named root": lambda r: r.update(archive_root=str(Path(r["named_archive_root"]) / "x")),
            }
            for name, change in cases.items():
                raw = json.loads(json.dumps(self.raw(root)))
                change(raw)
                with self.subTest(case=name), self.assertRaises(ConfigError):
                    bridge.load_native_config(raw, checkout_root=REPO_ROOT)


class CliSafetyTests(unittest.TestCase):
    def write_config(self, root: Path) -> Path:
        raw = ConfigTests().raw(root)
        path = root / "native.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def run_cli(self, *argv: str) -> tuple[int, dict, str]:
        import contextlib
        import io

        from energygrid_bill_downloader import native_cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = native_cli.main(list(argv))
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_init_uat_refuses_a_non_empty_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root)
            (root / "o").mkdir()
            (root / "o" / "stale-evidence.json").write_text("{}")
            code, result, _ = self.run_cli("init-uat", "--config", str(config))
            self.assertEqual((64, "EG_NATIVE_UAT_DIRECTORY_NOT_EMPTY"), (code, result["support_ref"]))
            self.assertFalse((root / "s.sqlite").exists())

    def test_unexpected_failure_is_closed_json_never_a_traceback(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root)
            with mock.patch("energygrid_bill_downloader.native_cli._dispatch",
                            side_effect=FileExistsError(str(root / "secret-path.json"))):
                code, result, printed = self.run_cli("status", "--config", str(config))
        self.assertEqual((20, "FAILED", "EG_NATIVE_UNEXPECTED_FAILURE"),
                         (code, result["outcome"], result["support_ref"]))
        self.assertNotIn("secret-path", printed)

    def test_checkout_roots_include_the_main_checkout_of_a_worktree(self) -> None:
        from energygrid_bill_downloader import native_cli

        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "main"
            worktree = main / ".claude" / "worktrees" / "w1"
            (main / ".git" / "worktrees" / "w1").mkdir(parents=True)
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'w1'}\n", encoding="utf-8")
            self.assertEqual((worktree, main), native_cli.checkout_roots(worktree))
            raw = ConfigTests().raw(Path(directory) / "outside")
            raw["state_path"] = str(main / "private" / "s.sqlite")
            with self.assertRaises(ConfigError):
                bridge.load_native_config(raw, checkout_root=native_cli.checkout_roots(worktree))


class BoundaryTests(unittest.TestCase):
    def test_bridge_imports_no_network_or_process_module(self) -> None:
        import ast

        source = (REPO_ROOT / "energygrid-bill-downloader" / "energygrid_bill_downloader" / "native_bridge.py").read_text()
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
        self.assertFalse(modules & {"socket", "urllib", "http", "subprocess", "smtplib", "requests", "ssl",
                                    "playwright", "googleapiclient"})

    def test_cli_refusal_output_is_closed_vocabulary(self) -> None:
        import contextlib
        import io

        from energygrid_bill_downloader import native_cli

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "private-name-should-not-leak.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = native_cli.main(["status", "--config", str(missing)])
        self.assertEqual(64, code)
        printed = out.getvalue()
        self.assertNotIn(str(missing), printed)
        self.assertNotIn("private-name-should-not-leak", printed)
        self.assertEqual("REFUSED", json.loads(printed)["outcome"])


if __name__ == "__main__":
    unittest.main()
