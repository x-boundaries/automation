"""Native Claude connector bridge (#226 V3 G1): offline protocol, recovery and refusal tests.

A `FakeNativeHost` plays the Claude host and the claude.ai Drive and Gmail connectors
locally. It can lose answers, crash between steps and corrupt the base64 it re-emits,
which is the real fidelity risk of a model-carried payload. Synthetic data only.

Core success is "archived and verified in Drive" (Web #226:6168033136). Email is an
optional owner notification (native_notify): once per processed bill and once per
actionable failure incident, never a gate on Drive work (Web #226:6168238127).
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from energygrid_bill_downloader import native_bridge as bridge
from energygrid_bill_downloader import native_notify as notify
from energygrid_bill_downloader.errors import ConfigError, OfflineV4Error, SourceTransportError
from energygrid_bill_downloader.native_bridge import NativeRefusal
from energygrid_bill_downloader.state import StateV4Store, migrate_state_database_v4
from fixtures.synthetic_dual_stream import SyntheticAdapter, candidate, create_v3_database, snapshot

EB, TN = "EB_BILL", "TENANT_BILL"
FOLDERS = {EB: "FOLDER_EB_SYNTHETIC_0001", TN: "FOLDER_TN_SYNTHETIC_0002"}
FLAT = "FOLDER_FLAT_SYNTHETIC_0003"
SENDER = "sender@example.com"
RECIPIENT = "recipient@example.com"
REPO_ROOT = Path(__file__).resolve().parents[2]


def pdf(label: str) -> bytes:
    return bridge.synthetic_pdf(label)


def corrupt(encoded: str) -> str:
    """Same length, different bytes: a size check alone cannot see it."""
    index = len(encoded) // 2
    replacement = "B" if encoded[index] != "B" else "C"
    return encoded[:index] + replacement + encoded[index + 1:]


class FailingAdapter:
    """A portal that cannot list. Used for acquisition-failure notifications."""

    def inventory(self, ceiling: int):
        raise SourceTransportError("EG_SYNTHETIC_PORTAL_DOWN")

    def acquire(self, item, destination: Path):
        raise SourceTransportError("EG_SYNTHETIC_PORTAL_DOWN")


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

    # -- Drive connector -----------------------------------------------
    def search(self, folder: str, title: str) -> list[dict]:
        return [{"id": key, "title": row["title"], "parentId": row["parent"]}
                for key, row in self.drive.items() if row["parent"] == folder and row["title"] == title]

    def put(self, folder: str, title: str, data: bytes) -> str:
        file_id = self._id("DRIVEFILE")
        self.drive[file_id] = {"title": title, "parent": folder, "bytes": data}
        return file_id

    def metadata(self, file_id: str) -> dict:
        row = self.drive[file_id]
        return {"id": file_id, "title": row["title"], "parentId": row["parent"], "mimeType": "application/pdf",
                "fileSize": str(len(row["bytes"]))}

    def create_file(self, call: dict) -> dict:
        encoded = Path(call["base64Content_path"]).read_text()
        if self.corrupt_upload:
            encoded = corrupt(encoded)
        return self.metadata(self.put(call["parentId"], call["title"], base64.b64decode(encoded)))

    def download(self, file_id: str) -> str:
        return base64.b64encode(self.drive[file_id]["bytes"]).decode()

    # -- Gmail connector -----------------------------------------------
    def create_draft(self, step: dict, **overrides) -> str:
        attachments = []
        if step["attach_pdf"]:
            encoded = Path(step["payload_base64_path"]).read_text()
            if self.corrupt_attachment:
                encoded = corrupt(encoded)
            attachments = [{"filename": step["attachment_filename"], "mimeType": "application/pdf",
                            "bytes": base64.b64decode(encoded)}]
        draft_id = self._id("DRAFT")
        self.drafts[draft_id] = {
            "from": overrides.get("sender", step["gmail_account"]),
            "to": overrides.get("to", step["to"]), "cc": overrides.get("cc", []),
            "subject": overrides.get("subject", step["subject"]),
            "body": overrides.get("body", step["body"]),
            "attachments": overrides.get("attachments", attachments),
        }
        return draft_id

    def raw(self, draft_id: str, *, gmail_wraps_links: bool = False) -> str:
        """The draft as get_draft RAW returns it: a base64url RFC 822 message. Real Gmail stores links
        wrapped as https://www.google.com/url?q=<link>&source=gmail&ust=...&sa=E."""
        import re
        from email.message import EmailMessage

        draft = dict(self.drafts[draft_id])
        if gmail_wraps_links:
            draft["body"] = re.sub(
                r"https://drive\.google\.com/\S+",
                lambda match: f"https://www.google.com/url?q={match.group(0)}&source=gmail&ust=1792080171266000&sa=E",
                draft["body"])
        message = EmailMessage()
        message["From"] = f'"UAT Sender" <{draft["from"]}>'
        message["To"] = ", ".join(draft["to"])
        if draft["cc"]:
            message["Cc"] = ", ".join(draft["cc"])
        message["Subject"] = draft["subject"]
        message.set_content(draft["body"])
        for item in draft["attachments"]:
            maintype, subtype = item["mimeType"].split("/")
            message.add_attachment(item["bytes"], maintype=maintype, subtype=subtype, filename=item["filename"])
        return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")

    def send_draft(self, draft_id: str) -> str:
        message_id = self._id("MSG")
        self.sent.append({"message_id": message_id, **self.drafts.pop(draft_id)})
        return message_id

    # -- protocol steps ------------------------------------------------
    def drive_step(self, step: dict, *, crash_after_seal=False, crash_after_create=False) -> dict:
        t = self.test
        intent = step["intent_id"]
        evidence = {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"], "matches": self.search(step["folder_id"], step["display_filename"])}
        sealed = t.call(bridge.drive_seal, intent, evidence)
        if sealed["outcome"] == "RECONCILE":
            file_id = sealed["reconcile_file_id"]
            t.readback(intent, file_id, self.download(file_id))
            return t.call(bridge.drive_adopt, intent, {"file": self.metadata(file_id)})
        if sealed["outcome"] != "SEALED" or crash_after_seal:
            return sealed
        created = self.create_file(sealed["create_file"])
        if crash_after_create:
            return {"outcome": "CRASHED", "file": created}
        t.readback(intent, created["id"], self.download(created["id"]))
        return t.call(bridge.drive_record, intent, {"outcome": "CREATED", "file": created})

    def notify_step(self, step: dict, *, crash_after_seal=False, outcome="SENT", **overrides) -> dict:
        t = self.test
        nid = step["notification_id"]
        draft_id = self.create_draft(step, **overrides)
        t.notify(lambda store: notify.draft_evidence(store, t.cfg, nid, draft_id, t.raw_file(draft_id, self.raw(draft_id))))
        sealed = t.notify(lambda store: notify.seal(store, t.cfg, nid, draft_id, t.now_iso()))
        if crash_after_seal:
            return sealed
        if outcome != "SENT":
            return t.notify(lambda store: notify.record(store, nid, {"outcome": outcome}, t.now_iso()))
        message_id = self.send_draft(sealed["send_draft_id"])
        return t.notify(lambda store: notify.record(
            store, nid, {"outcome": "SENT", "draft_id": sealed["send_draft_id"], "message_id": message_id}, t.now_iso()))

    def drain(self, limit: int = 16) -> list[dict]:
        results = []
        for _ in range(limit):
            step = self.test.next()
            if step["outcome"] == "HOLD":
                results.append(step)
                continue
            if step["outcome"] == "DRIVE_PRECHECK":
                results.append(self.drive_step(step))
            elif step["outcome"] == "NOTIFY_DRAFT":
                results.append(self.notify_step(step))
            else:
                results.append(step)
                break
        return results

    def successes(self) -> list[dict]:
        return [message for message in self.sent if " processed - " in message["subject"]]

    def failures(self) -> list[dict]:
        return [message for message in self.sent if " needs attention - " in message["subject"]]


class NativeBridgeBase(unittest.TestCase):
    ceilings = (4, 8)
    attach_pdf = False
    folders = FOLDERS

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
            named_archive_root=self.root / "bills", temp_root=self.root / "temp", outbox_root=self.root / "outbox",
            drive=bridge.NativeDriveSettings(account="uat@example.com", folder_ids=dict(self.folders)),
            gmail=bridge.NativeGmailSettings(account=SENDER, to=(RECIPIENT,), subject_prefix="EnergyGrid UAT",
                                             attach_pdf=self.attach_pdf),
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

    def now_iso(self) -> str:
        return bridge._iso(self.tick())

    def call(self, function, intent_id: str, evidence: dict) -> dict:
        with StateV4Store(self.db) as state:
            return function(state, self.cfg, intent_id, evidence, run_id=bridge.new_run_id(), now_ms=self.tick())

    def notify(self, action):
        with StateV4Store(self.db) as state, bridge._session(state, self.tick()), \
                notify.NotificationStore(notify.store_path(self.cfg)) as store:
            return action(store)

    def raw_file(self, draft_id: str, raw: str) -> Path:
        path = self.root / f"raw-{draft_id}.b64url"
        path.write_text(raw)
        return path

    def acquire(self, adapters=None) -> dict:
        with StateV4Store(self.db) as state:
            return bridge.acquire(state, self.cfg, adapters if adapters is not None else self.adapters(),
                                  run_id=bridge.new_run_id(), now_ms=self.tick())

    def next(self) -> dict:
        with StateV4Store(self.db) as state:
            return bridge.next_step(state, self.cfg, run_id=bridge.new_run_id(), now_ms=self.tick())

    def readback(self, intent: str, file_id: str, encoded: str) -> dict:
        path = self.root / f"readback-{file_id}.b64"
        path.write_text(encoded)
        with StateV4Store(self.db) as state:
            return bridge.drive_readback(state, self.cfg, intent, file_id, path)

    def status(self) -> dict:
        with StateV4Store(self.db) as state:
            return bridge.status(state, self.cfg)

    def intent_state(self, intent_id: str) -> str:
        with StateV4Store(self.db) as state:
            return state.intent(intent_id)["state"]

    def notification_states(self) -> list[tuple[str, str, str]]:
        return [(item["kind"], item["stream"], item["state"]) for item in self.status()["notifications"]["items"]]

    def dispatches(self) -> tuple[int, int]:
        effects = self.status()["effects"]
        return effects["drive_dispatches"], effects["gmail_dispatches"]

    def drive_until_notification(self) -> dict:
        while True:
            step = self.next()
            if step["outcome"] == "NOTIFY_DRAFT":
                return step
            self.host.drive_step(step)


class CompleteJourneyTests(NativeBridgeBase):
    def test_both_streams_archive_and_verify_in_drive_then_notify_once_each(self) -> None:
        acquired = self.acquire()
        self.assertEqual({EB: "ADMITTED", TN: "ADMITTED"},
                         {stream: row["status"] for stream, row in acquired["acquisition"].items()})
        self.host.drain()
        self.assertEqual({"SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", "SEP 2026 - EnergyGrid - Tenant Bill - TN Invoice 0002.pdf"},
                         {row["title"] for row in self.host.drive.values()})
        self.assertEqual({FOLDERS[EB], FOLDERS[TN]}, {row["parent"] for row in self.host.drive.values()})
        # Only the latest invoice was fetched; the older one is never a backfill candidate.
        self.assertEqual(["EB Invoice 0002.pdf"], self.portal[EB].acquire_calls)
        self.assertEqual(["TN Invoice 0002.pdf"], self.portal[TN].acquire_calls)
        by_title = {row["title"]: file_id for file_id, row in self.host.drive.items()}
        self.assertEqual(2, len(self.host.successes()))
        self.assertEqual([], self.host.failures())
        for message in self.host.sent:
            self.assertEqual((SENDER, [RECIPIENT], [], []), (message["from"], message["to"], message["cc"],
                                                            message["attachments"]))
            name = message["subject"].split(" processed - ", 1)[1]
            self.assertIn(f"https://drive.google.com/file/d/{by_title[name]}/view", message["body"])
            self.assertEqual(1, name.count(" 2026 - "))
        status = self.status()
        self.assertEqual("DRAINED", status["outcome"])
        self.assertEqual({"drive_dispatches": 2, "gmail_dispatches": 2}, status["effects"])
        self.assertEqual(2, status["notifications"]["counts"]["SENT"])
        (eb_invoice,) = status["streams"][EB]["invoices"]
        self.assertEqual("VERIFIED", eb_invoice["drive_upload"]["state"])
        self.assertTrue(eb_invoice["drive_upload"]["receipt"].startswith("gdrive:DRIVEFILE"))

    def test_immediate_second_run_has_zero_fetches_uploads_and_emails(self) -> None:
        self.acquire()
        self.host.drain()
        again = self.acquire()
        self.assertEqual({EB: "ALREADY_ADMITTED", TN: "ALREADY_ADMITTED"},
                         {stream: row["status"] for stream, row in again["acquisition"].items()})
        self.assertEqual(0, again["notifications_created"])
        self.assertEqual([], self.portal[EB].acquire_calls + self.portal[TN].acquire_calls)
        self.assertEqual("IDLE", self.next()["outcome"])
        self.assertEqual((2, 2), self.dispatches())
        self.assertEqual(2, len(self.host.sent))

    def test_a_newer_invoice_on_a_later_run_is_uploaded_and_notified_alone(self) -> None:
        self.acquire()
        self.host.drain()
        newer = {EB: [("EB Invoice 0002.pdf", "2026-09-01"), ("EB Invoice 0003.pdf", "2026-10-01")],
                 TN: [("TN Invoice 0002.pdf", "2026-09-03")]}
        result = self.acquire(self.adapters(newer))
        self.assertEqual("ADMITTED", result["acquisition"][EB]["status"])
        self.host.drain()
        self.assertEqual(3, len(self.host.drive))
        self.assertEqual(3, len(self.host.successes()))
        self.assertIn("OCT 2026 - EnergyGrid - EB Bill - EB Invoice 0003.pdf", self.host.sent[-1]["subject"])

    def test_payload_file_is_the_exact_archive_bytes(self) -> None:
        self.acquire()
        step = self.next()
        raw = base64.b64decode(Path(step["payload_base64_path"]).read_text())
        self.assertEqual(step["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(step["byte_size"], len(raw))


class CoreSuccessIndependenceTests(NativeBridgeBase):
    """Email is optional: it never gates Drive success, other streams or later invoices."""

    def test_drive_success_is_core_success_before_any_email(self) -> None:
        self.acquire()
        self.host.drive_step(self.next())
        self.host.drive_step(self.next())
        self.assertEqual("DRAINED", self.status()["outcome"])
        self.assertEqual([("SUCCESS", EB, "PENDING"), ("SUCCESS", TN, "PENDING")], sorted(self.notification_states()))

    def test_denied_or_uncertain_email_never_blocks_drive_or_resends_or_notifies_itself(self) -> None:
        self.acquire()
        first = self.drive_until_notification()
        self.assertEqual("HOLD", self.host.notify_step(first, outcome="DENIED")["outcome"])
        second = self.next()
        self.assertEqual("NOTIFY_DRAFT", second["outcome"])
        self.host.notify_step(second, crash_after_seal=True)
        recovered = self.next()
        self.assertEqual(1, recovered["recovered_notifications"])
        self.assertEqual("IDLE", recovered["outcome"])
        newer = {EB: [("EB Invoice 0002.pdf", "2026-09-01"), ("EB Invoice 0003.pdf", "2026-10-01")],
                 TN: [("TN Invoice 0002.pdf", "2026-09-03")]}
        self.acquire(self.adapters(newer))
        self.host.drain()
        self.assertEqual(3, len(self.host.drive))
        self.assertEqual("DRAINED", self.status()["outcome"])
        states = self.notification_states()
        self.assertIn(("SUCCESS", first["stream"], "HOLD"), states)
        self.assertIn(("SUCCESS", second["stream"], "UNKNOWN"), states)
        self.assertEqual([], self.host.failures())
        self.assertEqual(3, len(states))
        self.assertIn(("SUCCESS", EB, "SENT"), states)

    def test_an_uncertain_send_is_resolved_only_by_a_late_sent_for_the_bound_draft(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        sealed = self.host.notify_step(step, crash_after_seal=True)
        self.next()
        nid = step["notification_id"]
        with self.assertRaises(NativeRefusal):
            self.host.notify_step(step)
        message = self.host.send_draft(sealed["send_draft_id"])
        late = {"outcome": "SENT", "draft_id": sealed["send_draft_id"], "message_id": message}
        self.assertEqual("SENT", self.notify(lambda store: notify.record(store, nid, late, self.now_iso()))["outcome"])
        self.assertEqual("REPLAY", self.notify(lambda store: notify.record(store, nid, late, self.now_iso()))["outcome"])


class FailureNotificationTests(NativeBridgeBase):
    def test_portal_failure_is_notified_once_per_incident(self) -> None:
        failing = {EB: FailingAdapter(), TN: self.adapters()[TN]}
        self.assertEqual("PORTAL_ACQUISITION_FAILED", self.acquire(failing)["acquisition"][EB]["status"])
        self.host.drain()
        self.assertEqual(1, len(self.host.failures()))
        self.assertIn("EB Bill needs attention", self.host.failures()[0]["subject"])
        self.acquire({EB: FailingAdapter(), TN: self.portal[TN]})
        self.host.drain()
        self.assertEqual(1, len(self.host.failures()))
        self.acquire(self.adapters())
        self.host.drain()
        self.acquire({EB: FailingAdapter(), TN: self.portal[TN]})
        self.host.drain()
        self.assertEqual(2, len(self.host.failures()))

    def test_drive_hold_is_notified_once_with_non_sensitive_content(self) -> None:
        self.acquire()
        step = self.next()
        for _ in range(2):
            self.host.drive[self.host._id("FOREIGN")] = {"title": step["display_filename"], "parent": step["folder_id"],
                                                         "bytes": b"x"}
        held = self.host.drive_step(step)
        self.assertEqual(("HOLD", "EG_NATIVE_DRIVE_NAME_OCCUPIED"), (held["outcome"], held["support_ref"]))
        self.host.drain()
        self.acquire()
        self.host.drain()
        (failure,) = self.host.failures()
        self.assertIn("EG_NATIVE_DRIVE_NAME_OCCUPIED", failure["subject"])
        self.assertNotIn(step["display_filename"], failure["subject"] + failure["body"])
        self.assertNotIn("drive.google.com", failure["body"])
        other = TN if step["stream"] == EB else EB
        self.assertEqual("DRAINED", self.status()["streams"][other]["state"])
        self.assertEqual(1, len(self.host.successes()))

    def test_lost_drive_answer_is_unknown_blocks_only_its_stream_and_is_notified(self) -> None:
        self.acquire()
        step = self.next()
        self.host.drive_step(step, crash_after_seal=True)
        self.assertEqual("UNKNOWN",
                         self.call(bridge.drive_record, step["intent_id"], {"outcome": "NO_RESPONSE"})["outcome"])
        self.host.drain()
        streams = self.status()["streams"]
        self.assertEqual(("BLOCKED", "EG_V4_PROVIDER_UNKNOWN"),
                         (streams[step["stream"]]["state"], streams[step["stream"]]["support_ref"]))
        (failure,) = self.host.failures()
        self.assertIn("DRIVE_UPLOAD_UNKNOWN", failure["body"])


class FlatFolderTests(NativeBridgeBase):
    """Both streams upload directly into one existing folder (Web #226:6168238127)."""

    folders = {EB: FLAT, TN: FLAT}

    def test_both_streams_share_the_flat_folder(self) -> None:
        self.acquire()
        self.host.drain()
        self.assertEqual({FLAT}, {row["parent"] for row in self.host.drive.values()})
        self.assertEqual(2, len(self.host.drive))
        self.assertEqual(2, len(self.host.successes()))

    def test_an_identical_file_already_filed_by_hand_is_adopted_without_a_second_upload(self) -> None:
        manual = self.host.put(FLAT, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", pdf("EB_BILL EB Invoice 0002.pdf"))
        self.acquire()
        self.host.drain()
        self.assertEqual(2, len(self.host.drive))
        streams = self.status()["streams"]
        self.assertEqual(f"gdrive:{manual}", streams[EB]["invoices"][0]["drive_upload"]["receipt"])
        eb_success = [m for m in self.host.successes() if "EB Bill" in m["subject"]][0]
        self.assertIn(f"https://drive.google.com/file/d/{manual}/view", eb_success["body"])

    def test_a_different_file_with_the_same_name_is_never_overwritten_or_duplicated(self) -> None:
        foreign = self.host.put(FLAT, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", pdf("someone else's bill"))
        self.acquire()
        self.host.drain()
        self.assertEqual(pdf("someone else's bill"), self.host.drive[foreign]["bytes"])
        self.assertEqual(1, sum(1 for row in self.host.drive.values() if row["title"].startswith("SEP 2026 - EnergyGrid - EB Bill - EB")))
        self.assertEqual(("BLOCKED", "EG_NATIVE_DRIVE_NAME_OCCUPIED"),
                         (self.status()["streams"][EB]["state"], self.status()["streams"][EB]["support_ref"]))
        self.assertEqual(1, len(self.host.failures()))
        self.assertEqual("DRAINED", self.status()["streams"][TN]["state"])

    def test_a_same_size_file_with_different_bytes_is_not_adopted(self) -> None:
        genuine = bytearray(pdf("EB_BILL EB Invoice 0002.pdf"))
        genuine[len(genuine) // 2] ^= 0x01
        foreign = self.host.put(FLAT, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", bytes(genuine))
        self.acquire()
        self.host.drain()
        self.assertEqual(bytes(genuine), self.host.drive[foreign]["bytes"])
        self.assertEqual(("BLOCKED", "EG_NATIVE_DRIVE_NAME_OCCUPIED"),
                         (self.status()["streams"][EB]["state"], self.status()["streams"][EB]["support_ref"]))

    def test_several_same_name_files_hold_even_if_one_is_identical(self) -> None:
        self.host.put(FLAT, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", pdf("EB_BILL EB Invoice 0002.pdf"))
        self.host.put(FLAT, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", pdf("another copy"))
        self.acquire()
        self.host.drain()
        self.assertEqual(("BLOCKED", "EG_NATIVE_DRIVE_NAME_OCCUPIED"),
                         (self.status()["streams"][EB]["state"], self.status()["streams"][EB]["support_ref"]))
        self.assertEqual("HOLD", self.status()["streams"][EB]["invoices"][0]["drive_upload"]["state"])

    def test_the_same_source_name_on_both_streams_gets_two_distinct_names_in_the_flat_folder(self) -> None:
        # The stream label in the display name keeps the two streams apart in one flat folder.
        same = {EB: [("Invoice.pdf", "2026-09-01")], TN: [("Invoice.pdf", "2026-09-03")]}
        self.acquire(self.adapters(same))
        self.host.drain()
        self.assertEqual({"SEP 2026 - EnergyGrid - EB Bill - Invoice.pdf", "SEP 2026 - EnergyGrid - Tenant Bill - Invoice.pdf"},
                         {row["title"] for row in self.host.drive.values()})
        self.assertEqual("DRAINED", self.status()["outcome"])
        self.assertEqual(2, len(self.host.successes()))
        self.assertEqual(0, len(self.host.failures()))

    def _precheck_with(self, extra_titles: list[str]) -> dict:
        """Seal EB with Drive's loose `title =` answer: the exact matches plus other named files."""
        self.acquire()
        step = self.next()
        while step["stream"] != EB:
            self.host.drive_step(step)
            step = self.next()
        loose = self.host.search(FLAT, step["display_filename"])
        for title in extra_titles:
            file_id = self.host.put(FLAT, title, pdf(title))
            loose.append({"id": file_id, "title": title, "parentId": FLAT})
        evidence = {"folder_id": FLAT, "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"], "matches": loose}
        return self.call(bridge.drive_seal, step["intent_id"], evidence)

    def test_a_loose_drive_match_with_another_name_does_not_occupy_the_name(self) -> None:
        # Observed live: Drive returned the EB bill for the Tenant bill's exact-title query.
        sealed = self._precheck_with(["SEP 2026 - EnergyGrid - Tenant Bill - TN Invoice 0002.pdf"])
        self.assertEqual("SEALED", sealed["outcome"])

    def test_the_same_name_in_another_letter_case_still_occupies_the_name(self) -> None:
        sealed = self._precheck_with(["sep 2026 - energygrid - eb bill - eb invoice 0002.pdf"])
        self.assertEqual("RECONCILE", sealed["outcome"])

    def test_adoption_requires_the_core_readback_of_that_file(self) -> None:
        self.host.put(FLAT, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", pdf("EB_BILL EB Invoice 0002.pdf"))
        self.acquire()
        step = self.next()
        while step["stream"] != EB:
            self.host.drive_step(step)
            step = self.next()
        evidence = {"folder_id": FLAT, "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"], "matches": self.host.search(FLAT, step["display_filename"])}
        sealed = self.call(bridge.drive_seal, step["intent_id"], evidence)
        self.assertEqual("RECONCILE", sealed["outcome"])
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_adopt, step["intent_id"], {"file": self.host.metadata(sealed["reconcile_file_id"])})
        self.assertEqual("EG_NATIVE_DRIVE_READBACK_REQUIRED", refused.exception.support_ref)
        self.assertEqual("PENDING", self.intent_state(step["intent_id"]))


class DriveSafetyTests(NativeBridgeBase):
    def test_crash_after_seal_becomes_unknown_is_never_resent_and_a_late_receipt_resolves_it(self) -> None:
        self.acquire()
        step = self.next()
        intent = step["intent_id"]
        crashed = self.host.drive_step(step, crash_after_create=True)
        later = self.next()
        self.assertEqual(1, later["recovered_unknown"])
        self.assertEqual("UNKNOWN", self.intent_state(intent))
        self.assertNotEqual(intent, later.get("intent_id"))
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_seal, intent, {"folder_id": step["folder_id"], "title": step["display_filename"],
                                                   "query": step["precheck_query"], "complete": True, "matches": []})
        self.assertEqual("EG_NATIVE_INTENT_NOT_PENDING", refused.exception.support_ref)
        file = crashed["file"]
        self.readback(intent, file["id"], self.host.download(file["id"]))
        self.assertEqual("VERIFIED", self.call(bridge.drive_record, intent, {"outcome": "CREATED", "file": file})["outcome"])
        self.host.drive_step(later)
        self.host.drain()
        self.assertEqual(2, len(self.host.drive))
        self.assertEqual("DRAINED", self.status()["outcome"])

    def test_size_preserving_upload_corruption_is_caught_by_readback_and_held(self) -> None:
        self.acquire()
        step = self.next()
        self.host.corrupt_upload = True
        self.assertEqual("HOLD", self.host.drive_step(step)["outcome"])
        self.host.corrupt_upload = False
        self.host.drain()
        self.assertNotIn(step["display_filename"], " ".join(m["subject"] for m in self.host.successes()))
        self.assertEqual(1, len(self.host.failures()))

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
                self.readback(step["intent_id"], created["id"], self.host.download(created["id"]))
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
                self.assertEqual(expected, self.call(bridge.drive_record, step["intent_id"], {"outcome": outcome})["outcome"])
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
        self.host.drain()
        streams = self.status()["streams"]
        self.assertEqual(("BLOCKED", "EG_V4_ARCHIVE_IDENTITY_CHANGED"), (streams[EB]["state"], streams[EB]["support_ref"]))
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


class NotificationDraftTests(NativeBridgeBase):
    def test_wrong_draft_is_refused_without_seal_and_a_correct_draft_then_seals(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        wrong = (
            {"to": ["someone.else@example.com"]},
            {"to": [RECIPIENT, "extra@example.com"]},
            {"cc": ["cc@example.com"]},
            {"subject": "Something else"},
            {"sender": "someone.else@example.com"},
            {"body": "Processed, but without the Drive link."},
            {"attachments": [{"filename": "x.pdf", "mimeType": "application/pdf", "bytes": pdf("x")}]},
        )
        for overrides in wrong:
            with self.subTest(overrides=list(overrides)), self.assertRaises(NativeRefusal):
                self.host.notify_step(step, **overrides)
        self.assertEqual(0, self.dispatches()[1])
        self.assertEqual("SENT", self.host.notify_step(step)["outcome"])

    def test_gmail_redirect_wrapped_link_is_accepted_only_for_the_right_file(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        nid = step["notification_id"]
        wrong = self.host.create_draft(step, body=step["body"].replace("/file/d/", "/file/d/X"))
        self.notify(lambda store: notify.draft_evidence(
            store, self.cfg, nid, wrong, self.raw_file(wrong, self.host.raw(wrong, gmail_wraps_links=True))))
        with self.assertRaises(NativeRefusal) as refused:
            self.notify(lambda store: notify.seal(store, self.cfg, nid, wrong, self.now_iso()))
        self.assertEqual("EG_NATIVE_DRAFT_BODY_MISMATCH", refused.exception.support_ref)
        right = self.host.create_draft(step)
        self.notify(lambda store: notify.draft_evidence(
            store, self.cfg, nid, right, self.raw_file(right, self.host.raw(right, gmail_wraps_links=True))))
        sealed = self.notify(lambda store: notify.seal(store, self.cfg, nid, right, self.now_iso()))
        self.assertEqual("SEALED", sealed["outcome"])

    def test_only_the_exact_google_redirect_wrapper_is_unwrapped(self) -> None:
        link = "https://drive.google.com/file/d/ABC123/view"
        self.assertEqual(link, notify._unwrap_google_redirects(
            f"https://www.google.com/url?q={link}&source=gmail&ust=1&sa=E"))
        self.assertEqual(link, notify._unwrap_google_redirects(
            "https://www.google.com/url?q=https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2FABC123%2Fview&sa=E"))
        for other in (f"https://evil.example/url?q={link}", f"http://www.google.com/url?q={link}",
                      f"https://www.google.com.evil/url?q={link}"):
            self.assertNotEqual(link, notify._unwrap_google_redirects(other))

    def test_a_body_linking_another_drive_file_is_refused(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        wrong = step["body"].replace("/file/d/", "/file/d/X")
        with self.assertRaises(NativeRefusal) as refused:
            self.host.notify_step(step, body=wrong)
        self.assertEqual("EG_NATIVE_DRAFT_BODY_MISMATCH", refused.exception.support_ref)

    def test_seal_needs_core_parsed_evidence(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        draft = self.host.create_draft(step)
        with self.assertRaises(NativeRefusal) as refused:
            self.notify(lambda store: notify.seal(store, self.cfg, step["notification_id"], draft, self.now_iso()))
        self.assertEqual("EG_NATIVE_DRAFT_EVIDENCE_REQUIRED", refused.exception.support_ref)

    def test_only_the_bound_draft_is_recorded(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        sealed = self.host.notify_step(step, crash_after_seal=True)
        other = self.host.create_draft(step)
        with self.assertRaises(NativeRefusal) as refused:
            self.notify(lambda store: notify.record(store, step["notification_id"],
                                                    {"outcome": "SENT", "draft_id": other, "message_id": "MSG1"},
                                                    self.now_iso()))
        self.assertEqual("EG_NATIVE_DRAFT_NOT_BOUND", refused.exception.support_ref)
        self.assertEqual(sealed["send_draft_id"], self.host.drafts and sealed["send_draft_id"])

    def test_undecodable_raw_draft_is_refused(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        path = self.raw_file("garbage", "not*base64*at*all")
        with self.assertRaises(NativeRefusal) as refused:
            self.notify(lambda store: notify.draft_evidence(store, self.cfg, step["notification_id"],
                                                            "DRAFT000000000001", path))
        self.assertEqual("EG_NATIVE_DRAFT_RAW_UNDECODABLE", refused.exception.support_ref)


class NotificationAttachmentTests(NativeBridgeBase):
    attach_pdf = True

    def test_optional_attachment_is_byte_checked_before_send(self) -> None:
        self.acquire()
        step = self.drive_until_notification()
        self.assertTrue(step["attach_pdf"])
        self.host.corrupt_attachment = True
        with self.assertRaises(NativeRefusal) as refused:
            self.host.notify_step(step)
        self.assertEqual("EG_NATIVE_DRAFT_BYTES_MISMATCH", refused.exception.support_ref)
        self.host.corrupt_attachment = False
        with self.assertRaises(NativeRefusal):
            self.host.notify_step(step, attachments=[])
        self.assertEqual("SENT", self.host.notify_step(step)["outcome"])
        (attachment,) = self.host.sent[0]["attachments"]
        self.assertEqual(step["attachment_filename"], attachment["filename"])


class EffectCeilingTests(NativeBridgeBase):
    ceilings = (1, 1)

    def test_drive_ceiling_never_starves_notifications_and_seal_at_ceiling_is_refused(self) -> None:
        self.acquire()
        results = self.host.drain()
        self.assertEqual(["VERIFIED", "SENT", "CEILING_REACHED"], [row["outcome"] for row in results])
        self.assertEqual((1, 1), self.dispatches())
        streams = self.status()["streams"]
        # P-4: a stream held back by the ceiling is reported BLOCKED with the ceiling reference, not PENDING.
        pending = [stream for stream, row in streams.items()
                   if (row["state"], row["support_ref"]) == ("BLOCKED", "EG_NATIVE_EFFECT_CEILING_REACHED")]
        self.assertEqual(1, len(pending))
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(pending[0])[0]
            drive = state.intents_for_invoice(invoice["invoice_key"])["DRIVE_UPLOAD"]
        folder = FOLDERS[pending[0]]
        evidence = {"folder_id": folder, "title": invoice["display_filename"], "complete": True,
                    "query": bridge.drive_title_query(folder, invoice["display_filename"]), "matches": []}
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_seal, drive["intent_id"], evidence)
        self.assertEqual("EG_NATIVE_EFFECT_CEILING_REACHED", refused.exception.support_ref)


class ZeroCeilingTests(NativeBridgeBase):
    ceilings = (0, 0)

    def test_zero_ceiling_plans_nothing_and_seals_nothing(self) -> None:
        self.acquire()
        step = self.next()
        self.assertEqual(("CEILING_REACHED", 0), (step["outcome"], step["exit_code"]))
        self.assertEqual((0, 0), self.dispatches())


class GmailSealCeilingTests(NativeBridgeBase):
    ceilings = (4, 1)

    def test_a_notification_seal_at_the_email_ceiling_is_refused(self) -> None:
        self.acquire()
        self.host.drain()
        self.assertEqual(1, len(self.host.sent))
        pending = [row for row in self.notify(lambda store: store.rows("state='PENDING'"))]
        self.assertEqual(1, len(pending))
        nid = pending[0]["notification_id"]
        subject, body = self.notify(lambda store: notify.message_for(self.cfg, store.row(nid)))
        step = {"notification_id": nid, "gmail_account": SENDER, "to": [RECIPIENT], "subject": subject, "body": body,
                "attach_pdf": False}
        draft = self.host.create_draft(step)
        self.notify(lambda store: notify.draft_evidence(store, self.cfg, nid, draft, self.raw_file(draft, self.host.raw(draft))))
        with self.assertRaises(NativeRefusal) as refused:
            self.notify(lambda store: notify.seal(store, self.cfg, nid, draft, self.now_iso()))
        self.assertEqual("EG_NATIVE_EFFECT_CEILING_REACHED", refused.exception.support_ref)


class GmailCeilingTests(NativeBridgeBase):
    ceilings = (4, 0)

    def test_email_ceiling_never_blocks_drive_success(self) -> None:
        self.acquire()
        results = self.host.drain()
        self.assertEqual("NOTIFY_CEILING_REACHED", results[-1]["outcome"])
        self.assertEqual("DRAINED", self.status()["outcome"])
        self.assertEqual((2, 0), self.dispatches())


class ReviewFindingTests(NativeBridgeBase):
    def test_readback_for_a_case_variant_file_id_does_not_verify(self) -> None:
        self.acquire()
        step = self.next()
        sealed = self.host.drive_step(step, crash_after_seal=True)
        created = self.host.create_file(sealed["create_file"])
        self.readback(step["intent_id"], created["id"], self.host.download(created["id"]))
        variant = created | {"id": created["id"].lower()}
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_record, step["intent_id"], {"outcome": "CREATED", "file": variant})
        self.assertEqual("EG_NATIVE_DRIVE_READBACK_REQUIRED", refused.exception.support_ref)

    def test_status_shows_no_receipt_for_a_held_intent(self) -> None:
        self.acquire()
        step = self.next()
        sealed = self.host.drive_step(step, crash_after_seal=True)
        self.assertEqual("HOLD", self.call(bridge.drive_record, step["intent_id"], {"outcome": "DENIED"})["outcome"])
        created = self.host.create_file(sealed["create_file"])
        self.readback(step["intent_id"], created["id"], self.host.download(created["id"]))
        self.call(bridge.drive_record, step["intent_id"], {"outcome": "CREATED", "file": created})
        self.assertEqual("HOLD", self.intent_state(step["intent_id"]))
        invoice = self.status()["streams"][step["stream"]]["invoices"][0]
        self.assertEqual(("HOLD", None), (invoice["drive_upload"]["state"], invoice["drive_upload"]["receipt"]))

    def test_precheck_query_escapes_quotes_and_backslashes(self) -> None:
        self.assertEqual("parentId = 'F1' and title = 'O\\'Brien \\\\ x.pdf'",
                         bridge.drive_title_query("F1", "O'Brien \\ x.pdf"))

    def test_a_display_name_with_an_apostrophe_runs_end_to_end(self) -> None:
        self.acquire(self.adapters({EB: [("O'Brien EB Invoice.pdf", "2026-09-01")], TN: []}))
        step = self.next()
        self.assertIn("title = 'SEP 2026 - EnergyGrid - EB Bill - O\\'Brien EB Invoice.pdf'", step["precheck_query"])
        self.host.drain()
        self.assertEqual(["SEP 2026 - EnergyGrid - EB Bill - O'Brien EB Invoice.pdf"], [row["title"] for row in self.host.drive.values()])

    def test_synthetic_fixture_refuses_path_like_filenames(self) -> None:
        for name in ("../escape.pdf", "sub/dir.pdf", "..", ""):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                bridge.write_synthetic_fixture(self.root / "portal-x", {EB: [(name, "2026-09-01")]})


class NamedArchiveTests(NativeBridgeBase):
    """Owner requirement: the user-facing archive and Drive use `MMM YYYY - EnergyGrid - <stream> - <original>.pdf`."""

    def named(self, stream: str, name: str) -> Path:
        return self.cfg.named_archive_root / bridge.STREAM_LABELS[stream] / name

    def test_user_facing_archive_uses_the_display_name_and_internal_identity_stays_date_only(self) -> None:
        acquired = self.acquire()
        self.assertEqual({EB: ["PRESENT"], TN: ["PRESENT"]},
                         {k: [row["named_archive"] for row in v] for k, v in acquired["named_archive"].items()})
        self.host.drain()
        for stream, original, day in ((EB, "EB Invoice 0002.pdf", "2026-09-01"), (TN, "TN Invoice 0002.pdf", "2026-09-03")):
            with self.subTest(stream=stream):
                named = self.named(stream, f"SEP 2026 - EnergyGrid - {bridge.STREAM_LABELS[stream]} - {original}")
                self.assertEqual(pdf(f"{stream} {original}"), named.read_bytes())
                self.assertEqual([named.name], [item.name for item in named.parent.iterdir()])
                internal = self.cfg.archive_root / bridge.STREAM_LABELS[stream] / f"{day}.pdf"
                self.assertEqual(named.read_bytes(), internal.read_bytes())
        self.assertEqual({"SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", "SEP 2026 - EnergyGrid - Tenant Bill - TN Invoice 0002.pdf"},
                         {row["title"] for row in self.host.drive.values()})
        self.assertEqual(["PRESENT"], [row["named_archive"] for row in self.status()["streams"][EB]["invoices"]])

    def test_existing_identical_copy_is_kept_and_never_rewritten(self) -> None:
        target = self.named(EB, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf")
        target.parent.mkdir(parents=True)
        target.write_bytes(pdf("EB_BILL EB Invoice 0002.pdf"))
        before = target.stat().st_mtime_ns
        self.acquire()
        self.host.drain()
        self.assertEqual(before, target.stat().st_mtime_ns)
        self.assertEqual(2, len(self.host.drive))

    def test_a_different_file_at_the_name_holds_that_invoice_and_is_never_overwritten(self) -> None:
        target = self.named(EB, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf")
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

    def test_a_removed_copy_is_republished_before_the_drive_step(self) -> None:
        self.acquire()
        self.named(EB, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf").unlink()
        self.host.drain()
        self.assertTrue(self.named(EB, "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf").is_file())
        self.assertEqual(2, len(self.host.drive))

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
        self.assertEqual(2, len(self.host.drive))

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


class LocalSyncTests(NativeBridgeBase):
    """Google Drive for desktop: the Core writes into the synced folder; no PDF bytes pass the host."""

    folders = {EB: FLAT, TN: FLAT}

    def make_config(self, **changes) -> bridge.NativeConfig:
        sync = self.root / "sync"
        sync.mkdir(exist_ok=True)
        drive = bridge.NativeDriveSettings(account="uat@example.com", folder_ids=dict(self.folders),
                                           mode=bridge.DRIVE_LOCAL_SYNC, sync_folders={EB: sync, TN: sync})
        return super().make_config(drive=drive, **changes)

    def sync(self) -> None:
        """Drive for desktop: upload any local file Drive does not have yet."""
        known = {row["title"] for row in self.host.drive.values() if row["parent"] == FLAT}
        for path in (self.root / "sync").iterdir():
            if path.name not in known:
                self.host.put(FLAT, path.name, path.read_bytes())

    def sync_step(self, step: dict, *, synced: bool = True) -> dict:
        evidence = {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"], "matches": self.host.search(FLAT, step["display_filename"])}
        sealed = self.call(bridge.drive_seal, step["intent_id"], evidence)
        if sealed["outcome"] == "RECONCILE":
            return self.call(bridge.drive_adopt, step["intent_id"],
                             {"file": self.host.metadata(sealed["reconcile_file_id"])})
        if sealed["outcome"] != "SEALED":
            return sealed
        self.assertTrue(sealed["sync_written"])
        self.assertNotIn("create_file", sealed)
        if synced:
            self.sync()
        found = self.host.search(FLAT, step["display_filename"])
        if not found:
            return self.call(bridge.drive_record, step["intent_id"], {"outcome": "NO_RESPONSE"})
        return self.call(bridge.drive_record, step["intent_id"],
                         {"outcome": "CREATED", "file": self.host.metadata(found[0]["id"])})

    def drain_sync(self) -> list[dict]:
        results = []
        for _ in range(16):
            step = self.next()
            if step["outcome"] == "DRIVE_PRECHECK":
                self.assertNotIn("payload_base64_path", step)
                results.append(self.sync_step(step))
            elif step["outcome"] == "NOTIFY_DRAFT":
                results.append(self.host.notify_step(step))
            elif step["outcome"] == "HOLD":
                results.append(step)
            else:
                results.append(step)
                break
        return results

    def test_both_streams_upload_through_the_synced_folder_and_verify(self) -> None:
        self.acquire()
        self.drain_sync()
        self.assertEqual({"SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf", "SEP 2026 - EnergyGrid - Tenant Bill - TN Invoice 0002.pdf"},
                         {row["title"] for row in self.host.drive.values()})
        for row in self.host.drive.values():
            stream = EB if "EB" in row["title"] else TN
            prefix = f"SEP 2026 - EnergyGrid - {bridge.STREAM_LABELS[stream]} - "
            self.assertEqual(pdf(f"{stream} {row['title'][len(prefix):]}"), row["bytes"])
        self.assertEqual("DRAINED", self.status()["outcome"])
        self.assertEqual(2, len(self.host.successes()))
        self.assertEqual([], list(self.cfg.outbox_root.glob("*.pdf.b64")))

    def test_second_run_writes_nothing_new(self) -> None:
        self.acquire()
        self.drain_sync()
        before = sorted(path.name for path in (self.root / "sync").iterdir())
        self.acquire()
        self.assertEqual("IDLE", self.next()["outcome"])
        self.assertEqual(before, sorted(path.name for path in (self.root / "sync").iterdir()))
        self.assertEqual(2, len(self.host.drive))

    def test_an_unsynced_upload_is_unknown_never_rewritten_and_resolves_late(self) -> None:
        self.acquire()
        step = self.next()
        self.assertEqual("UNKNOWN", self.sync_step(step, synced=False)["outcome"])
        sync_file = self.root / "sync" / step["display_filename"]
        self.assertTrue(sync_file.is_file())
        mtime = sync_file.stat().st_mtime_ns
        self.drain_sync()
        self.assertEqual(mtime, sync_file.stat().st_mtime_ns)
        found = self.host.search(FLAT, step["display_filename"])
        late = self.call(bridge.drive_record, step["intent_id"],
                         {"outcome": "CREATED", "file": self.host.metadata(found[0]["id"])})
        self.assertEqual("VERIFIED", late["outcome"])

    def test_record_before_the_mirror_exists_is_refused_without_state_change(self) -> None:
        self.acquire()
        step = self.next()
        evidence = {"folder_id": FLAT, "title": step["display_filename"], "complete": True,
                    "query": step["precheck_query"], "matches": []}
        self.call(bridge.drive_seal, step["intent_id"], evidence)
        self.sync()
        found = self.host.search(FLAT, step["display_filename"])
        (self.root / "sync" / step["display_filename"]).unlink()
        with self.assertRaises(NativeRefusal) as refused:
            self.call(bridge.drive_record, step["intent_id"],
                      {"outcome": "CREATED", "file": self.host.metadata(found[0]["id"])})
        self.assertEqual("EG_NATIVE_DRIVE_READBACK_REQUIRED", refused.exception.support_ref)
        self.assertEqual("DISPATCHED", self.intent_state(step["intent_id"]))

    def test_a_different_local_file_is_never_overwritten(self) -> None:
        self.acquire()
        step = self.next()
        target = self.root / "sync" / step["display_filename"]
        target.write_bytes(b"another bill")
        held = self.sync_step(step)
        self.assertEqual(("HOLD", "EG_NATIVE_DRIVE_NAME_OCCUPIED"), (held["outcome"], held["support_ref"]))
        self.assertEqual(b"another bill", target.read_bytes())

    def test_an_identical_file_already_in_drive_is_adopted_through_its_mirror(self) -> None:
        name = "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf"
        manual = self.host.put(FLAT, name, pdf("EB_BILL EB Invoice 0002.pdf"))
        (self.root / "sync" / name).write_bytes(pdf("EB_BILL EB Invoice 0002.pdf"))
        self.acquire()
        self.drain_sync()
        self.assertEqual(2, len(self.host.drive))
        self.assertEqual(f"gdrive:{manual}", self.status()["streams"][EB]["invoices"][0]["drive_upload"]["receipt"])

    def test_a_same_name_drive_file_with_different_bytes_is_held(self) -> None:
        name = "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf"
        genuine = bytearray(pdf("EB_BILL EB Invoice 0002.pdf"))
        genuine[len(genuine) // 2] ^= 0x01
        self.host.put(FLAT, name, bytes(genuine))
        (self.root / "sync" / name).write_bytes(bytes(genuine))
        self.acquire()
        self.drain_sync()
        self.assertEqual(("BLOCKED", "EG_NATIVE_DRIVE_NAME_OCCUPIED"),
                         (self.status()["streams"][EB]["state"], self.status()["streams"][EB]["support_ref"]))
        self.assertEqual(bytes(genuine), (self.root / "sync" / name).read_bytes())


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


class PredecessorCutoverTests(NativeBridgeBase):
    def prepare_database(self) -> None:
        from test_state_v4 import build_predecessor_v3

        build_predecessor_v3(self.db, self.cfg.archive_root, {EB: "ADOPTABLE", TN: "DELIVERED"})
        migrate_state_database_v4(self.db, apply=True)

    def test_v3_archived_undelivered_latest_is_adopted_without_fetch_and_uploaded_once(self) -> None:
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
        self.assertEqual(["SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf"], [row["title"] for row in self.host.drive.values()])
        self.assertEqual(1, len(self.host.successes()))
        named = self.cfg.named_archive_root / "EB Bill" / "SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf"
        self.assertEqual(predecessor_pdf("Invoice EB Sep.pdf"), named.read_bytes())
        self.assertEqual("IDLE", self.next()["outcome"])


class ConfigTests(unittest.TestCase):
    def raw(self, root: Path) -> dict:
        return {
            "schema": bridge.NATIVE_CONFIG_SCHEMA, "mode": bridge.MODE_UAT_SYNTHETIC,
            "state_path": str(root / "s.sqlite"), "archive_root": str(root / "a"),
            "named_archive_root": str(root / "n"), "temp_root": str(root / "t"),
            "outbox_root": str(root / "o"), "synthetic_root": str(root / "p"),
            "drive": {"account": "uat@example.com", "folder_ids": {EB: FLAT, TN: FLAT}, "mode": "NATIVE_UPLOAD"},
            "gmail": {"account": SENDER, "to": [RECIPIENT], "subject_prefix": "EnergyGrid", "attach_pdf": False},
            "effect_ceiling": {"drive_uploads": 2, "gmail_sends": 2},
        }

    def test_valid_flat_config_loads_and_repr_hides_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = bridge.load_native_config(self.raw(Path(directory)), checkout_root=REPO_ROOT)
        self.assertEqual((RECIPIENT,), cfg.gmail.to)
        self.assertEqual(cfg.drive.folder_ids[EB], cfg.drive.folder_ids[TN])
        for secret in (RECIPIENT, SENDER, FLAT):
            self.assertNotIn(secret, repr(cfg))

    def test_committed_example_config_loads(self) -> None:
        example = REPO_ROOT / "energygrid-bill-downloader" / "config" / "energygrid.native_uat.example.json"
        cfg = bridge.load_native_config(json.loads(example.read_text(encoding="utf-8")), checkout_root=REPO_ROOT)
        self.assertEqual(bridge.MODE_UAT_SYNTHETIC, cfg.mode)
        self.assertFalse(cfg.gmail.attach_pdf)

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
                "bad recipient": lambda r: r["gmail"].update(to=["not-an-email"]),
                "no recipients": lambda r: r["gmail"].update(to=[]),
                "two recipients": lambda r: r["gmail"].update(to=[RECIPIENT, "other@example.com"]),
                "old template keys": lambda r: r["gmail"].update(subject_template="x"),
                "attach not bool": lambda r: r["gmail"].update(attach_pdf="yes"),
                "empty prefix": lambda r: r["gmail"].update(subject_prefix=""),
                "ceiling too high": lambda r: r["effect_ceiling"].update(drive_uploads=21),
                "ceiling not int": lambda r: r["effect_ceiling"].update(gmail_sends=True),
                "portal key in synthetic mode": lambda r: r.update(portal_config_path=str(root / "c.json")),
                "named root missing": lambda r: r.pop("named_archive_root"),
                "drive mode missing": lambda r: r["drive"].pop("mode"),
                "unknown drive mode": lambda r: r["drive"].update(mode="SMTP"),
                "sync folders without sync mode": lambda r: r["drive"].update(
                    sync_folders={EB: str(root / "s"), TN: str(root / "s")}),
                "sync mode without folders": lambda r: r["drive"].update(mode="LOCAL_SYNC"),
                "sync folders not one to one": lambda r: r["drive"].update(
                    mode="LOCAL_SYNC", sync_folders={EB: str(root / "s1"), TN: str(root / "s2")}),
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
        path = root / "native.json"
        path.write_text(json.dumps(ConfigTests().raw(root)), encoding="utf-8")
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
        self.assertEqual((20, "FAILED", "EG_NATIVE_UNEXPECTED_FAILURE"), (code, result["outcome"], result["support_ref"]))
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


class SingleStreamPortalTests(unittest.TestCase):
    """The installed host's single-stream direct-HTTP config: EB Bill bound, Tenant Bill unbound."""

    def config(self, root: Path) -> bridge.NativeConfig:
        portal = root / "portal.json"
        portal.write_text(json.dumps({
            "source": "direct_http",
            "direct_http": {"list_url": "https://portal.invalid/list", "fetch_url": "https://portal.invalid/fetch",
                            "tenant_id": "SYNTHETIC-TENANT"},
            "archive_root": str(root / "v1" / "archive"), "state_path": str(root / "v1" / "state" / "v1.sqlite"),
            "temp_root": str(root / "v1" / "temp"), "log_root": str(root / "v1" / "logs"),
            "browser_cache_path": str(root / "v1" / "cache"), "timeout_seconds": 30, "max_attempts": 2,
            "inventory_safety_ceiling": 200,
        }), encoding="utf-8")
        raw = ConfigTests().raw(root)
        raw.pop("synthetic_root")
        raw.update(mode=bridge.MODE_UAT_PORTAL, portal_config_path=str(portal))
        return bridge.load_native_config(raw, checkout_root=REPO_ROOT)

    def test_eb_bill_gets_the_reviewed_direct_http_adapter_and_tenant_stays_unbound(self) -> None:
        from energygrid_bill_downloader import native_cli
        from energygrid_bill_downloader.invoice import DirectHttpAdapter

        with tempfile.TemporaryDirectory() as directory:
            cfg = self.config(Path(directory))
            adapters = native_cli._adapters(cfg)
            self.assertEqual(["EB_BILL"], list(adapters))
            self.assertIsInstance(adapters["EB_BILL"], DirectHttpAdapter)
            entries = native_cli._stream_entries(cfg)
            self.assertEqual(("BOUND", "UNBOUND"), (entries["EB_BILL"].admission, entries["TENANT_BILL"].admission))
            self.assertEqual(native_cli.SINGLE_STREAM_NAMESPACE, entries["EB_BILL"].source_namespace)

    def test_init_uat_builds_an_isolated_database_for_the_single_stream_portal(self) -> None:
        from energygrid_bill_downloader import native_cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = self.config(root)
            result = native_cli._init_uat(cfg, None)
            self.assertEqual("INITIALISED", result["outcome"])
            with StateV4Store(cfg.state_path) as state:
                self.assertEqual([], state.invoices_for_stream(EB))


class TenantBindingTests(unittest.TestCase):
    """The optional private Tenant source binds Tenant Bill next to the single-stream EB config."""

    def config(self, root: Path, *, tenant_path: str | None = None) -> bridge.NativeConfig:
        browser = root / "browser.json"
        browser.write_text(json.dumps({
            "portal_url": "https://portal.invalid/", "account_identity": "synthetic",
            "archive_root": str(root / "v1" / "archive"), "state_path": str(root / "v1" / "state" / "v1.sqlite"),
            "temp_root": str(root / "v1" / "temp"), "log_root": str(root / "v1" / "logs"),
            "browser_cache_path": str(root / "v1" / "cache"), "timeout_seconds": 30, "max_attempts": 2,
            "inventory_safety_ceiling": 200,
        }), encoding="utf-8")
        tenant_config = root / "tenant.json"
        tenant_config.write_text(json.dumps({
            "schema": "energygrid.tenant.v1", "login_url": "https://login.invalid/login",
            "list_url": "https://service.invalid/list", "portal_type": "synthetic-portal",
            "list_body": {"svcClaimDto": {"user_id": 1, "username": "synthetic", "role_id": 2},
                          "request": {"item_kind": "bill", "filter_value_info": {"tenant_id": "SYNTHETIC"}}},
            "service_headers": {}, "render_request_path": "/synthetic/bill", "credential_path": str(root / "cred.clixml"), "browser_config_path": str(browser),
        }), encoding="utf-8")
        raw = SingleStreamPortalTests().config(root)
        values = {name: getattr(raw, name) for name in raw.__dataclass_fields__}
        values["tenant_config_path"] = Path(tenant_path or tenant_config)
        return bridge.NativeConfig(**values)

    def test_the_tenant_source_binds_tenant_bill_without_touching_eb(self) -> None:
        from energygrid_bill_downloader import native_cli
        from energygrid_bill_downloader.invoice import DirectHttpAdapter
        from energygrid_bill_downloader.tenant_source import TENANT_NAMESPACE, TenantBillAdapter

        with tempfile.TemporaryDirectory() as directory:
            cfg = self.config(Path(directory))
            adapters = native_cli._adapters(cfg)
            self.assertIsInstance(adapters["EB_BILL"], DirectHttpAdapter)
            self.assertIsInstance(adapters["TENANT_BILL"], TenantBillAdapter)
            entries = native_cli._stream_entries(cfg)
            self.assertEqual(("BOUND", "BOUND"), (entries["EB_BILL"].admission, entries["TENANT_BILL"].admission))
            self.assertEqual(TENANT_NAMESPACE, entries["TENANT_BILL"].source_namespace)

    def test_the_tenant_key_is_optional_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = ConfigTests().raw(root)
            raw.pop("synthetic_root")
            raw.update(mode=bridge.MODE_UAT_PORTAL, portal_config_path=str(root / "portal.json"))
            self.assertIsNone(bridge.load_native_config(dict(raw), checkout_root=REPO_ROOT).tenant_config_path)
            loaded = bridge.load_native_config(dict(raw, tenant_config_path=str(root / "t.json")), checkout_root=REPO_ROOT)
            self.assertEqual(root / "t.json", loaded.tenant_config_path)
            inside = str(REPO_ROOT / "tenant.json")
            with self.assertRaises(ConfigError):
                bridge.load_native_config(dict(raw, tenant_config_path=inside), checkout_root=REPO_ROOT)
            synthetic = ConfigTests().raw(root)
            with self.assertRaises(ConfigError):
                bridge.load_native_config(dict(synthetic, tenant_config_path=str(root / "t.json")), checkout_root=REPO_ROOT)


class BoundaryTests(unittest.TestCase):
    def test_bridge_modules_import_no_network_or_process_module(self) -> None:
        import ast

        for module in ("native_bridge.py", "native_notify.py"):
            source = (REPO_ROOT / "energygrid-bill-downloader" / "energygrid_bill_downloader" / module).read_text()
            modules = set()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    modules.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules.add(node.module.split(".")[0])
            with self.subTest(module=module):
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
        self.assertNotIn("private-name-should-not-leak", printed)
        self.assertEqual("REFUSED", json.loads(printed)["outcome"])


if __name__ == "__main__":
    unittest.main()
