"""Claude-free daily run (#226 V3 G1): offline end-to-end tests through the real Core boundaries.

`FakeN8n` plays the two loopback n8n workflows (Drive Verify and Owner Email) and Google: a cloud
folder that Drive for desktop fills from the synced folder when the run waits, Google's own
sha256Checksum (available or not yet), and a mailer with the workflow's Data Table claim. It renders
messages from its own copy of the templates, so a drift from native_notify.message_for is caught by the
render digest. `FakeClock` makes every wait instant. Synthetic data only; no network, n8n or Google.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from energygrid_bill_downloader import n8n_loopback as loopback
from energygrid_bill_downloader import native_bridge as bridge
from energygrid_bill_downloader import native_daily as daily
from energygrid_bill_downloader import native_notify as notify
from energygrid_bill_downloader.errors import ConfigError
from energygrid_bill_downloader.native_bridge import NativeRefusal
from energygrid_bill_downloader.state import StateV4Store

from test_native_bridge import EB, FLAT, RECIPIENT, REPO_ROOT, SENDER, TN, FailingAdapter, NativeBridgeBase

READER = "reader@example.com"
PREFIX = "EnergyGrid UAT"
LABELS = {EB: "EB Bill", TN: "Tenant Bill"}
MONITOR = REPO_ROOT / "n8n-workflows" / "energygrid_local_failure_monitor" / "energygrid_monitor_report.py"


def render(prefix: str, request: dict) -> tuple[str, str]:
    """The Owner Email workflow's own templates (independent of native_notify.message_for)."""
    label = LABELS[request["stream"]]
    if request["kind"] == "SUCCESS":
        return (f"{prefix}: {label} processed - {request['display_filename']}",
                f"EnergyGrid processed a new {label}.\nFile: {request['display_filename']}\n"
                f"Google Drive: https://drive.google.com/file/d/{request['drive_file_id']}/view\n")
    if request["status_text"] == "HOLD_RELEASED_NO_DISPATCH":
        return (f"{prefix}: {label} hold released - {request['support_ref']}",
                f"An operator released a held {label} upload that was never sent to Google Drive.\n"
                f"File: {request['display_filename']}\nStatus: {request['status_text']}\n"
                f"Reference: {request['support_ref']}\nThe next scheduled run tries it again; it is never uploaded "
                "twice. Check the EnergyGrid status report.\n")
    return (f"{prefix}: {label} needs attention - {request['support_ref']}",
            f"EnergyGrid could not complete the {label} stream.\nStatus: {request['status_text']}\n"
            f"Reference: {request['support_ref']}\nUncertain uploads are never retried automatically. "
            "Check the EnergyGrid status report.\n")


class FakeN8n:
    """Drive Verify + Owner Email + Google, in memory."""

    def __init__(self, test: "DailyBase") -> None:
        self.test = test
        self.cloud: dict[str, dict] = {}
        self.counter = 0
        self.healthy = True
        self.verify_up = True
        self.email_up = True
        self.sha_ready = True
        self.sync_enabled = True
        self.sync_after_waits = 1
        self.waits = 0
        self.reader = READER
        self.sender = SENDER
        self.recipient = RECIPIENT
        self.prefix = PREFIX
        self.email_mode = "OK"
        self.claims: dict[str, str] = {}
        self.sent: list[dict] = []
        self.verify_calls = 0
        self.tamper = None

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter:012d}"

    # -- Google Drive --------------------------------------------------------
    def put(self, name: str, data: bytes | None, **extra) -> str:
        file_id = self._id("CLOUDFILE")
        self.cloud[file_id] = {"name": name, "parents": [FLAT], "mimeType": "application/pdf", "bytes": data, **extra}
        return file_id

    def sync(self) -> None:
        """Drive for desktop: upload every final-named local file the cloud folder does not have yet."""
        names = {row["name"] for row in self.cloud.values()}
        for path in sorted((self.test.root / "sync").iterdir()):
            if path.name.startswith(".eg-stage-") or path.name in names:
                continue
            self.put(path.name, path.read_bytes())

    def on_wait(self) -> None:
        self.waits += 1
        if self.sync_enabled and self.waits >= self.sync_after_waits:
            self.sync()

    def _file(self, file_id: str, row: dict) -> dict:
        item = {"id": file_id, "name": row["name"], "mimeType": row["mimeType"], "parents": list(row["parents"]),
                "trashed": row.get("trashed", False)}
        if row.get("shortcut"):
            item["shortcutDetails"] = {"targetId": "TARGETFILE000001"}
        if row["bytes"] is not None:
            item["size"] = str(len(row["bytes"]))
            item["md5Checksum"] = hashlib.md5(row["bytes"]).hexdigest()
            if self.sha_ready and not row.get("no_sha"):
                item["sha256Checksum"] = hashlib.sha256(row["bytes"]).hexdigest()
        return item

    def verify(self, doc: dict) -> loopback.Answer:
        self.verify_calls += 1
        if not self.verify_up:
            return loopback.Answer(loopback.NOT_SENT, support_ref="EG_N8N_UNREACHABLE")
        if set(doc) != {"schema", "nonce"} or doc["schema"] != bridge.LISTING_REQUEST_SCHEMA:
            return loopback.Answer(loopback.HTTP_STATUS, status=422, body={"outcome": "REFUSED"})
        files = [self._file(key, row) for key, row in self.cloud.items()
                 if FLAT in row["parents"] and not row.get("trashed")]
        body = {"schema": bridge.LISTING_RESPONSE_SCHEMA, "nonce": doc["nonce"], "folder_id": FLAT,
                "reader_account": self.reader, "complete": True, "pages": 1, "incomplete_search": False,
                "next_page_token_present": False, "files": files}
        if self.tamper is not None:
            self.tamper(body)
        return loopback.Answer(loopback.OK, status=200, body=body)

    # -- Owner Email -----------------------------------------------------------
    def email(self, doc: dict) -> loopback.Answer:
        if not self.email_up:
            return loopback.Answer(loopback.NOT_SENT, support_ref="EG_N8N_UNREACHABLE")
        schema = notify.EMAIL_RESPONSE_SCHEMA
        if doc.get("mode") == "PROBE":
            return loopback.Answer(loopback.OK, status=200, body={
                "schema": schema, "mode": "PROBE", "nonce": doc["nonce"], "ready": True, "sender_account": self.sender,
                "recipient_sha256": hashlib.sha256(self.recipient.lower().encode()).hexdigest()})
        ids = {"notification_id": doc["notification_id"], "dispatch_id": doc["dispatch_id"]}
        subject, body = render(self.prefix, doc)
        if hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest() != doc["render_sha256"]:
            return loopback.Answer(loopback.HTTP_STATUS, status=422, body={
                "schema": schema, "mode": "SEND", **ids, "outcome": "REFUSED", "reason": "EG_EMAIL_RENDER_MISMATCH"})
        if doc["notification_id"] in self.claims:
            return loopback.Answer(loopback.HTTP_STATUS, status=409, body={
                "schema": schema, "mode": "SEND", **ids, "outcome": "DUPLICATE"})
        self.claims[doc["notification_id"]] = "CLAIMED"
        if self.email_mode == "GMAIL_ERROR":
            return loopback.Answer(loopback.HTTP_STATUS, status=502, body={
                "schema": schema, "mode": "SEND", **ids, "outcome": "SEND_UNCERTAIN"})
        message_id = self._id("MSG")
        self.sent.append({"to": self.recipient, "subject": subject, "body": body, "message_id": message_id})
        if self.email_mode == "TIMEOUT":
            return loopback.Answer(loopback.SENT_UNCERTAIN, support_ref="EG_N8N_TRANSPORT_UNCERTAIN")
        if self.email_mode == "CRASH":
            raise KeyboardInterrupt("simulated crash after the send")
        return loopback.Answer(loopback.OK, status=200, body={
            "schema": schema, "mode": "SEND", **ids, "outcome": "SENT", "message_id": message_id})

    # -- client interface --------------------------------------------------------
    def health(self) -> bool:
        return self.healthy

    def post(self, path: str, document: dict, timeout: int) -> loopback.Answer:
        document = json.loads(json.dumps(document))
        if path == loopback.VERIFY_PATH:
            return self.verify(document)
        if path == loopback.EMAIL_PATH:
            return self.email(document)
        raise AssertionError(path)

    def successes(self) -> list[dict]:
        return [m for m in self.sent if " processed - " in m["subject"]]

    def failures(self) -> list[dict]:
        return [m for m in self.sent if " needs attention - " in m["subject"]]


class FakeClock:
    def __init__(self, test: "DailyBase") -> None:
        self.test = test
        self.slept = 0.0

    def now_ms(self) -> int:
        return self.test.tick()

    def local_now(self) -> datetime:
        return datetime.fromtimestamp(self.test.clock / 1000)

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.test.clock += int(seconds * 1000)
        self.test.n8n.on_wait()


class DailyBase(NativeBridgeBase):
    folders = {EB: FLAT, TN: FLAT}
    window, poll, deadline = 20, 60, 75

    def make_config(self, **changes) -> bridge.NativeConfig:
        sync = self.root / "sync"
        sync.mkdir(exist_ok=True)
        token = self.root / "token.txt"
        token.write_text("t" * 40, encoding="utf-8")
        drive = bridge.NativeDriveSettings(account="uat@example.com", folder_ids=dict(self.folders),
                                           mode=bridge.DRIVE_LOCAL_SYNC, sync_folders={EB: sync, TN: sync})
        settings = bridge.NativeDailySettings(
            runs_root=self.root / "runs", drive_reader_account=READER, n8n_port=5678, token_file=token,
            header_name="X-EnergyGrid-Monitor-Key", verify_window_minutes=self.window,
            verify_poll_seconds=self.poll, run_deadline_minutes=self.deadline)
        values = dict(drive=drive, daily=settings)
        values.update(changes)
        return super().make_config(**values)

    def setUp(self) -> None:
        super().setUp()
        self.n8n = FakeN8n(self)
        self.fake_clock = FakeClock(self)
        # Each run is a later wall-clock run (the run folder is named after local time).
        self.clock = int(datetime(2026, 10, 20, 11, 30).timestamp() * 1000)

    def run_daily(self, adapters=None, cfg=None) -> dict:
        self.clock += 86_400_000
        portal = adapters if adapters is not None else self.adapters()
        return daily.run(cfg or self.cfg, lambda: portal, client=self.n8n, clock=self.fake_clock)

    def run_json(self, result: dict, name: str) -> dict:
        return json.loads((Path(result["run_dir"]) / name).read_text(encoding="utf-8"))

    def intents(self, action: str = "DRIVE_UPLOAD") -> list[dict]:
        with StateV4Store(self.db) as state:
            return [state.intents_for_invoice(row["invoice_key"])[action]
                    for stream in (EB, TN) for row in state.invoices_for_stream(stream)]

    def drive_states(self) -> list[str]:
        return [intent["state"] for intent in self.intents()]

    def classic_cfg(self) -> bridge.NativeConfig:
        """The same state opened by a config without `daily` (for example the Claude bridge config)."""
        return self.make_config(daily=None)


class DailyJourneyTests(DailyBase):
    def test_both_streams_filed_verified_by_google_sha256_and_emailed_once(self) -> None:
        result = self.run_daily()
        self.assertEqual(("COMPLETED", 0, daily.SUBMIT), (result["outcome"], result["exit_code"], result["monitor_report"]))
        self.assertEqual(["VERIFIED", "VERIFIED"], self.drive_states())
        names = sorted(row["name"] for row in self.n8n.cloud.values())
        self.assertEqual(["SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf",
                          "SEP 2026 - EnergyGrid - Tenant Bill - TN Invoice 0002.pdf"], names)
        # Latest only: the older invoice of each stream is never fetched.
        self.assertEqual(["EB Invoice 0002.pdf"], self.portal[EB].acquire_calls)
        status = self.run_json(result, "status.json")
        for stream in (EB, TN):
            upload = status["streams"][stream]["invoices"][0]["drive_upload"]
            self.assertEqual(("VERIFIED", "LOCAL_MIRROR_SHA256", "GOOGLE_SHA256"),
                             (upload["state"], upload["hash_provenance"], upload["remote_attestation"]))
            file_id = upload["receipt"][len("gdrive:"):]
            self.assertIn(file_id, self.n8n.cloud)
        self.assertTrue(status["remote_verify"]["required"])
        self.assertEqual(2, len(self.n8n.successes()))
        self.assertEqual([], self.n8n.failures())
        for message in self.n8n.successes():
            self.assertIn("https://drive.google.com/file/d/CLOUDFILE", message["body"])
        # Run evidence: acquire, status and the run record, in the monitor's folder convention.
        self.assertEqual({"acquire.json", "status.json", "daily.json"}, {p.name for p in Path(result["run_dir"]).iterdir()})
        self.assertEqual("ACQUIRED", self.run_json(result, "acquire.json")["outcome"])
        self.assertEqual(2, self.run_json(result, "daily.json")["email"]["sent"])

    def test_second_run_makes_zero_effects(self) -> None:
        self.run_daily()
        cloud, sent = dict(self.n8n.cloud), list(self.n8n.sent)
        drive, gmail = self.dispatches()
        result = self.run_daily()
        self.assertEqual(daily.SUBMIT, result["monitor_report"])
        self.assertEqual((cloud, sent, (drive, gmail)), (self.n8n.cloud, self.n8n.sent, self.dispatches()))
        steps = self.run_json(result, "daily.json")["steps"]
        self.assertEqual([{"step": "next", "outcome": "IDLE"}], steps)

    def test_newer_invoice_later_is_filed_once_and_older_never_backfilled(self) -> None:
        self.run_daily()
        listing = {EB: [("EB Invoice 0002.pdf", "2026-09-01"), ("EB Invoice 0003.pdf", "2026-10-01")],
                   TN: [("TN Invoice 0002.pdf", "2026-09-03")]}
        self.run_daily(self.adapters(listing))
        self.assertEqual(["EB Invoice 0003.pdf"], self.portal[EB].acquire_calls)
        self.assertEqual(3, len(self.n8n.successes()))
        self.assertEqual(["VERIFIED"] * 3, self.drive_states())

    def test_no_bill_day_is_idle_and_reports(self) -> None:
        result = self.run_daily(self.adapters({EB: [], TN: []}))
        self.assertEqual(daily.SUBMIT, result["monitor_report"])
        self.assertEqual("DRAINED", result["status_outcome"])
        self.assertEqual((0, 0), self.dispatches())

    def test_v4_gmail_send_intents_are_never_dispatched(self) -> None:
        self.run_daily()
        self.run_daily()
        self.assertEqual(["PENDING", "PENDING"], [intent["state"] for intent in self.intents("GMAIL_SEND")])
        with StateV4Store(self.db) as state:
            self.assertEqual(0, state.dispatch_count("GMAIL_SEND"))

    def test_report_files_are_accepted_by_the_unchanged_monitor_reporter(self) -> None:
        spec = importlib.util.spec_from_file_location("eg_monitor_report", MONITOR)
        reporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reporter)
        # The reporter compares the folder name with real file times, so this run uses real local time.
        self.fake_clock.local_now = datetime.now
        result = self.run_daily()
        acquire, status = reporter.bind_run_dir(Path(result["run_dir"]), self.root / "runs", now=datetime.now())
        report = reporter.summarise(acquire, status)
        self.assertEqual({EB: "OK", TN: "OK"}, {s: row["result"] for s, row in report["streams"].items()})


class GoogleVerificationTests(DailyBase):
    def test_late_sync_stays_pending_then_verifies_next_run_without_a_reseal(self) -> None:
        self.n8n.sync_enabled = False
        first = self.run_daily()
        self.assertEqual(["DISPATCHED", "PENDING"], self.drive_states())
        steps = self.run_json(first, "daily.json")["steps"]
        self.assertIn("EG_DAILY_DRIVE_SYNC_PENDING", [s.get("support_ref") for s in steps])
        # The poll window is bounded in minutes, well inside the deadline.
        self.assertLessEqual(self.fake_clock.slept, self.window * 60)
        # In flight is BLOCKED (as under the Claude host); the next run raises the UNKNOWN incident by email.
        self.assertEqual("BLOCKED", first["status_outcome"])
        self.n8n.sync_enabled = True
        self.n8n.sync()
        drive_before, _ = self.dispatches()
        second = self.run_daily()
        self.assertEqual(["VERIFIED", "VERIFIED"], self.drive_states())
        self.assertEqual(drive_before + 1, self.dispatches()[0], "only the Tenant bill is newly sealed")
        self.assertEqual(1, sum(1 for row in self.n8n.cloud.values() if " EB Bill " in row["name"]))
        # The earlier lost receipt raised one UNKNOWN incident (POST_SHIP noise), delivered once.
        self.assertEqual(1, len(self.n8n.failures()))
        self.assertEqual(2, len(self.n8n.successes()))
        self.assertEqual(daily.SUBMIT, second["monitor_report"])

    def test_missing_google_sha256_never_verifies_then_verifies_once_available(self) -> None:
        self.n8n.sha_ready = False
        self.run_daily()
        self.assertNotIn("VERIFIED", self.drive_states())
        self.assertEqual([], self.n8n.successes())
        self.n8n.sha_ready = True
        self.run_daily()
        self.assertEqual(["VERIFIED", "VERIFIED"], self.drive_states())

    def test_md5_and_size_alone_never_verify(self) -> None:
        def strip_sha(body: dict) -> None:
            for item in body["files"]:
                item.pop("sha256Checksum", None)
        self.n8n.tamper = strip_sha
        self.run_daily()
        self.assertNotIn("VERIFIED", self.drive_states())

    def test_google_sha256_mismatch_is_rejected_and_held(self) -> None:
        def wrong_sha(body: dict) -> None:
            for item in body["files"]:
                if "sha256Checksum" in item:
                    item["sha256Checksum"] = "0" * 64
        self.n8n.tamper = wrong_sha
        result = self.run_daily()
        self.assertEqual(["HOLD", "HOLD"], self.drive_states())
        self.assertEqual(2, len(self.n8n.failures()))
        self.assertEqual("BLOCKED", result["status_outcome"])
        # Both FAILURE notices were delivered, so the monitor can stay silent on FAILURE.
        self.assertEqual(daily.SUBMIT, result["monitor_report"])

    def test_duplicate_same_name_in_drive_holds_and_never_overwrites(self) -> None:
        name = "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf"
        self.n8n.put(name, b"%PDF-1.4 other one")
        self.n8n.put(name.upper(), b"%PDF-1.4 other two")
        self.run_daily()
        eb, tn = self.drive_states()
        self.assertEqual(("HOLD", "VERIFIED"), (eb, tn))
        self.assertFalse((self.root / "sync" / name).exists(), "nothing written for a held name")

    def test_identical_hand_filed_file_is_adopted_without_an_upload(self) -> None:
        self.acquire()
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(EB)[0]
        data = (self.cfg.archive_root / invoice["archive_relpath"]).read_bytes()
        (self.root / "sync" / invoice["display_filename"]).write_bytes(data)
        self.n8n.put(invoice["display_filename"], data)
        self.run_daily()
        self.assertEqual(["VERIFIED", "VERIFIED"], self.drive_states())
        self.assertEqual(1, sum(1 for row in self.n8n.cloud.values() if row["name"] == invoice["display_filename"]))

    def test_same_name_with_different_bytes_is_held(self) -> None:
        self.acquire()
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(EB)[0]
        (self.root / "sync" / invoice["display_filename"]).write_bytes(b"%PDF-1.4 different")
        self.n8n.put(invoice["display_filename"], b"%PDF-1.4 different")
        self.run_daily()
        self.assertEqual("HOLD", self.drive_states()[0])

    def test_unexpected_mime_type_or_shortcut_occupant_is_held(self) -> None:
        for extra in ({"mimeType": "application/vnd.google-apps.document"}, {"shortcut": True}):
            with self.subTest(extra=extra):
                self.tearDown()
                self.setUp()
                self.acquire()
                with StateV4Store(self.db) as state:
                    invoice = state.invoices_for_stream(EB)[0]
                data = (self.cfg.archive_root / invoice["archive_relpath"]).read_bytes()
                (self.root / "sync" / invoice["display_filename"]).write_bytes(data)
                self.n8n.put(invoice["display_filename"], data, **extra)
                self.run_daily()
                self.assertEqual("HOLD", self.drive_states()[0])

    def test_incomplete_or_foreign_listings_never_count(self) -> None:
        cases = {
            "incomplete": lambda b: b.update(complete=False),
            "next page token": lambda b: b.update(next_page_token_present=True),
            "incomplete search": lambda b: b.update(incomplete_search=True),
            "nonce replay": lambda b: b.update(nonce="0" * 32),
            "other reader": lambda b: b.update(reader_account="someone@example.com"),
            "other folder": lambda b: b.update(folder_id="FOLDER_OTHER_SYNTHETIC_09"),
            "extra key": lambda b: b.update(extra=True),
            "file outside folder": lambda b: [item.update(parents=["FOLDER_OTHER_SYNTHETIC_09"]) for item in b["files"]],
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.tearDown()
                self.setUp()
                self.n8n.tamper = change
                result = self.run_daily()
                self.assertNotIn("VERIFIED", self.drive_states())
                self.assertEqual([], self.n8n.sent if name != "file outside folder" else [])
                self.assertIn(result["status_outcome"], ("PENDING", "BLOCKED"))

    def test_verify_unavailable_seals_nothing(self) -> None:
        self.n8n.verify_up = False
        result = self.run_daily()
        self.assertEqual(["PENDING", "PENDING"], self.drive_states())
        self.assertEqual((0, 0), self.dispatches())
        self.assertEqual([], list((self.root / "sync").iterdir()))
        self.assertEqual("PENDING", result["status_outcome"])

    def test_sync_folder_unavailable_is_retry_later(self) -> None:
        shutil.rmtree(self.root / "sync")
        result = self.run_daily()
        self.assertEqual(["PENDING", "PENDING"], self.drive_states())
        steps = self.run_json(result, "daily.json")["steps"]
        self.assertIn("EG_NATIVE_SYNC_FOLDER_UNAVAILABLE", [s.get("support_ref") for s in steps])

    def test_crash_between_attestation_and_receipt_never_verifies_without_attestation(self) -> None:
        original = StateV4Store.record_receipt
        calls = {"n": 0}

        def crash(self_state, intent_id, **kwargs):
            if kwargs.get("outcome") == "VERIFIED" and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("simulated crash after the attestation")
            return original(self_state, intent_id, **kwargs)

        StateV4Store.record_receipt = crash
        try:
            with self.assertRaises(RuntimeError):
                self.run_daily()
        finally:
            StateV4Store.record_receipt = original
        with contextlib.closing(sqlite3.connect(bridge.provenance_path(self.cfg))) as connection:
            attested = {row[0] for row in connection.execute("SELECT intent_id FROM drive_remote_attestation")}
        verified = {i["intent_id"] for i in self.intents() if i["state"] == "VERIFIED"}
        self.assertTrue(verified <= attested)
        self.assertEqual(1, len(attested))
        self.run_daily()
        self.assertEqual(["VERIFIED", "VERIFIED"], self.drive_states())

    def test_tampered_listing_evidence_is_refused(self) -> None:
        self.acquire()
        listing = daily.DailyRun(self.cfg, None, self.n8n, self.fake_clock).listing()
        path = self.cfg.outbox_root / f"{listing['nonce']}.drive-listing.json"
        path.write_bytes(path.read_bytes().replace(b'"files":[]', b'"files":[ ]'))
        intent = self.intents()[0]["intent_id"]
        with self.assertRaises(NativeRefusal) as caught:
            self.call(bridge.drive_seal, intent, {"listing_nonce": listing["nonce"]})
        self.assertEqual("EG_DRIVE_LISTING_TAMPERED", caught.exception.support_ref)


class RemotePolicyTests(DailyBase):
    def test_host_attested_evidence_is_refused_under_the_daily_config(self) -> None:
        self.acquire()
        intent = self.intents()[0]["intent_id"]
        evidence = {"folder_id": FLAT, "title": "x.pdf", "query": "q", "complete": True, "matches": []}
        with self.assertRaises(NativeRefusal) as caught:
            self.call(bridge.drive_seal, intent, evidence)
        self.assertEqual("EG_DRIVE_REMOTE_VERIFY_REQUIRED", caught.exception.support_ref)

    def test_latched_state_refuses_the_claude_bridge_paths_under_any_config(self) -> None:
        self.run_daily(self.adapters({EB: [("EB Invoice 0001.pdf", "2026-08-01")], TN: []}))
        self.cfg = self.classic_cfg()
        self.assertTrue(bridge.remote_required(self.cfg))
        self.acquire(self.adapters({EB: [("EB Invoice 0002.pdf", "2026-09-01")], TN: []}))
        step = self.next()
        self.assertEqual("DRIVE_PRECHECK", step["outcome"])
        evidence = {"folder_id": FLAT, "title": step["display_filename"], "query": step["precheck_query"],
                    "complete": True, "matches": []}
        refusals = {
            "drive-seal": lambda: self.call(bridge.drive_seal, step["intent_id"], evidence),
            "drive-readback": lambda: self.readback(step["intent_id"], "DRIVEFILE000000000001", "QUJD"),
            "notify-seal": lambda: self.notify(lambda store: notify.seal(store, self.cfg, "egn-" + "1" * 32,
                                                                         "DRAFT1", self.now_iso())),
        }
        for name, action in refusals.items():
            with self.subTest(command=name), self.assertRaises(NativeRefusal) as caught:
                action()
            self.assertIn(caught.exception.support_ref, ("EG_DRIVE_REMOTE_VERIFY_REQUIRED", "EG_DAILY_READBACK_DISABLED",
                                                         "EG_DAILY_GMAIL_DRAFT_PATH_DISABLED"))

    def test_backfill_is_refused_once_latched(self) -> None:
        self.run_daily()
        path = self.root / "native.json"
        raw = {"schema": bridge.NATIVE_CONFIG_SCHEMA}
        from energygrid_bill_downloader import native_cli

        args = native_cli.build_parser().parse_args(["backfill-run", "--config", str(path), "--drive-evidence",
                                                     str(path), "--max", "1"])
        original = native_cli._load_config
        native_cli._load_config = lambda _path: self.classic_cfg()
        try:
            with self.assertRaises(NativeRefusal) as caught:
                native_cli._dispatch(args)
        finally:
            native_cli._load_config = original
        self.assertEqual("EG_DAILY_BACKFILL_DISABLED", caught.exception.support_ref)
        self.assertTrue(raw)


class PostLatchRecoveryTests(DailyBase):
    """Web #226:6235489184: once `daily` has latched a state, a bill found later is recovered only through the
    native route (an attended `daily` run once the cause is fixed). The Claude bridge route is refused and
    changes nothing; the latch, Google's SHA-256 requirement and the notification history stay as they are."""

    NEW = {EB: [("EB Invoice 0002.pdf", "2026-09-01"), ("EB Invoice 0003.pdf", "2026-10-01")],
           TN: [("TN Invoice 0002.pdf", "2026-09-03"), ("TN Invoice 0003.pdf", "2026-10-03")]}
    NEW_NAMES = ["OCT 2026 - EnergyGrid - EB Bill - EB Invoice 0003.pdf",
                 "OCT 2026 - EnergyGrid - Tenant Bill - TN Invoice 0003.pdf"]

    def effects(self) -> tuple:
        with notify.NotificationStore(notify.store_path(self.cfg)) as store:
            notices = sorted((row["notification_id"], row["state"], row["draft_id"]) for row in store.rows())
        return (sorted(self.drive_states()), self.dispatches(), sorted(p.name for p in (self.root / "sync").iterdir()),
                sorted(row["name"] for row in self.n8n.cloud.values()), len(self.n8n.sent), notices,
                sorted(p.name for p in self.cfg.outbox_root.iterdir() if not p.name.endswith(".drive-listing.json")))

    def test_new_bills_after_the_latch_wait_unfiled_then_recover_once_through_an_attended_daily_run(self) -> None:
        daily_cfg = self.cfg
        self.run_daily()
        self.assertTrue(bridge.remote_policy_latched(daily_cfg))
        self.assertEqual(2, len(self.n8n.successes()))

        # New EB and Tenant bills arrive while Drive Verify is down: acquired and archived, nothing filed or sent.
        self.n8n.verify_up = False
        outage = self.run_daily(self.adapters(self.NEW))
        self.assertEqual(["EB Invoice 0003.pdf"], self.portal[EB].acquire_calls)
        self.assertEqual(["TN Invoice 0003.pdf"], self.portal[TN].acquire_calls)
        self.assertEqual(["PENDING", "PENDING", "VERIFIED", "VERIFIED"], sorted(self.drive_states()))
        self.assertEqual("PENDING", outage["status_outcome"])  # the unchanged monitor alerts on a PENDING bill
        for name in self.NEW_NAMES:
            self.assertNotIn(name, [p.name for p in (self.root / "sync").iterdir()])
        self.assertEqual(2, len(self.n8n.sent))
        waiting = self.effects()

        # The Claude bridge route on the same state (classic config, host-attested evidence) is refused and
        # changes nothing: the latch holds under every config and is never cleared.
        self.cfg = self.classic_cfg()
        self.assertTrue(self.status()["remote_verify"]["required"])
        step = self.next()
        self.assertEqual("DRIVE_PRECHECK", step["outcome"])
        evidence = {"folder_id": FLAT, "title": step["display_filename"], "query": step["precheck_query"],
                    "complete": True, "matches": []}
        with self.assertRaises(NativeRefusal) as caught:
            self.call(bridge.drive_seal, step["intent_id"], evidence)
        self.assertEqual("EG_DRIVE_REMOTE_VERIFY_REQUIRED", caught.exception.support_ref)
        with self.assertRaises(NativeRefusal) as caught:
            self.notify(lambda store: notify.seal(store, self.cfg, "egn-" + "1" * 32, "DRAFT1", self.now_iso()))
        self.assertEqual("EG_DAILY_GMAIL_DRAFT_PATH_DISABLED", caught.exception.support_ref)
        with bridge._provenance(self.cfg, create=False) as connection, self.assertRaises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM remote_verify_policy")
        self.assertTrue(bridge.remote_policy_latched(self.cfg))
        self.assertEqual(waiting, self.effects())

        # Recovery: Drive Verify is back and the owner runs `daily` once by hand (the same entry point the
        # scheduler uses). Each new bill is filed once, VERIFIED by Google's SHA-256, and emailed once.
        self.cfg = daily_cfg
        self.n8n.verify_up = True
        recovered = self.run_daily(self.adapters(self.NEW))
        self.assertEqual(("COMPLETED", daily.SUBMIT, "DRAINED"),
                         (recovered["outcome"], recovered["monitor_report"], recovered["status_outcome"]))
        self.assertEqual(["VERIFIED"] * 4, self.drive_states())
        status = self.run_json(recovered, "status.json")
        new = [invoice["drive_upload"] for stream in (EB, TN) for invoice in status["streams"][stream]["invoices"]
               if invoice["display_filename"] in self.NEW_NAMES]
        self.assertEqual([("VERIFIED", "GOOGLE_SHA256")] * 2, [(u["state"], u["remote_attestation"]) for u in new])
        self.assertEqual(sorted(self.NEW_NAMES + [
            "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf",
            "SEP 2026 - EnergyGrid - Tenant Bill - TN Invoice 0002.pdf"]), sorted(r["name"] for r in self.n8n.cloud.values()))
        self.assertEqual(4, len(self.n8n.successes()))
        self.assertEqual(sorted(self.NEW_NAMES), sorted(m["subject"].split(" - ", 1)[1] for m in self.n8n.successes()[2:]))
        self.assertEqual([], self.n8n.failures())

        # A further run, scheduled or attended, makes zero effects: nothing is filed or sent twice.
        settled = self.effects()
        again = self.run_daily(self.adapters(self.NEW))
        self.assertEqual(daily.SUBMIT, again["monitor_report"])
        self.assertEqual(settled, self.effects())
        self.assertEqual([{"step": "next", "outcome": "IDLE"}], self.run_json(again, "daily.json")["steps"])

    def test_a_listing_without_googles_sha256_never_verifies_a_new_bill_after_the_latch(self) -> None:
        self.run_daily()
        self.n8n.sha_ready = False
        waiting = self.run_daily(self.adapters(self.NEW))
        # The first new upload is written and synced but not VERIFIED; the run stops there (host rule P1).
        self.assertEqual(["DISPATCHED", "PENDING", "VERIFIED", "VERIFIED"], sorted(self.drive_states()))
        self.assertEqual(2, len(self.n8n.successes()))
        self.assertIn("EG_DAILY_DRIVE_SYNC_PENDING",
                      [s.get("support_ref") for s in self.run_json(waiting, "daily.json")["steps"]])
        # md5 and size alone never verify; once Google reports its SHA-256 the next run records it, once.
        self.n8n.sha_ready = True
        self.run_daily(self.adapters(self.NEW))
        self.run_daily(self.adapters(self.NEW))
        self.assertEqual(["VERIFIED"] * 4, self.drive_states())
        self.assertEqual(4, len(self.n8n.successes()))


class OwnerEmailTests(DailyBase):
    def test_email_unavailable_never_blocks_drive_and_sends_later_once(self) -> None:
        self.n8n.email_up = False
        first = self.run_daily()
        self.assertEqual(["VERIFIED", "VERIFIED"], self.drive_states())
        self.assertEqual([], self.n8n.sent)
        self.assertEqual("EG_DAILY_N8N_UNHEALTHY" if not self.n8n.healthy else "EG_DAILY_EMAIL_PROBE_FAILED",
                         self.run_json(first, "daily.json")["email"]["probe"])
        # Only SUCCESS notices are waiting: the report is submitted (email is optional feedback).
        self.assertEqual(daily.SUBMIT, first["monitor_report"])
        self.n8n.email_up = True
        self.run_daily()
        self.run_daily()
        self.assertEqual(2, len(self.n8n.successes()))

    def test_undelivered_failure_notice_withholds_the_monitor_report(self) -> None:
        self.n8n.email_up = False
        portal = self.adapters()
        portal[TN] = FailingAdapter()
        result = self.run_daily(portal)
        self.assertEqual((daily.WITHHELD, 20), (result["monitor_report"], result["exit_code"]))
        self.assertIn("EG_DAILY_FAILURE_NOTICE_UNDELIVERED", result["withheld_reasons"])
        self.n8n.email_up = True
        portal = self.adapters()
        portal[TN] = FailingAdapter()
        healed = self.run_daily(portal)
        self.assertEqual(daily.SUBMIT, healed["monitor_report"])
        self.assertEqual(1, len(self.n8n.failures()), "one incident, delivered once")

    def test_tenant_failure_is_isolated_and_notified_once_per_incident(self) -> None:
        portal = self.adapters()
        portal[TN] = FailingAdapter()
        self.run_daily(portal)
        portal = self.adapters()
        portal[TN] = FailingAdapter()
        self.run_daily(portal)
        self.assertEqual("VERIFIED", self.drive_states()[0])
        self.assertEqual(1, len(self.n8n.successes()))
        self.assertEqual(1, len(self.n8n.failures()))

    def test_uncertain_send_is_never_resent(self) -> None:
        self.n8n.email_mode = "TIMEOUT"
        self.run_daily()
        self.n8n.email_mode = "OK"
        self.run_daily()
        self.run_daily()
        self.assertEqual(2, len(self.n8n.sent), "each notice sent exactly once despite the lost answers")
        self.assertEqual({"UNKNOWN"}, {state for _k, _s, state in self.notification_states()})

    def test_uncertain_failure_notice_withholds_the_report(self) -> None:
        self.n8n.email_mode = "GMAIL_ERROR"
        portal = self.adapters()
        portal[TN] = FailingAdapter()
        result = self.run_daily(portal)
        self.assertEqual(daily.WITHHELD, result["monitor_report"])
        self.assertIn("EG_DAILY_FAILURE_NOTICE_UNCERTAIN", result["withheld_reasons"])
        self.assertEqual([], self.n8n.sent)

    def test_render_mismatch_is_refused_before_any_send_and_held(self) -> None:
        self.n8n.prefix = "Another Prefix"
        self.run_daily()
        self.assertEqual([], self.n8n.sent)
        self.assertEqual({"HOLD"}, {state for _k, _s, state in self.notification_states()})

    def test_probe_must_bind_the_sender_and_recipient(self) -> None:
        for attribute, value in (("sender", "other@example.com"), ("recipient", "other@example.com")):
            with self.subTest(attribute=attribute):
                self.tearDown()
                self.setUp()
                setattr(self.n8n, attribute, value)
                result = self.run_daily()
                self.assertEqual("EG_DAILY_EMAIL_PROBE_FAILED", self.run_json(result, "daily.json")["email"]["probe"])
                self.assertEqual([], self.n8n.sent)

    def test_crash_after_the_send_is_unknown_and_never_resent(self) -> None:
        self.n8n.email_mode = "CRASH"
        with self.assertRaises(KeyboardInterrupt):
            self.run_daily()
        self.n8n.email_mode = "OK"
        self.run_daily()
        self.run_daily()
        subjects = [m["subject"] for m in self.n8n.sent]
        self.assertEqual(len(subjects), len(set(subjects)), "no message sent twice")
        self.assertIn(("SUCCESS", EB, "UNKNOWN"), self.notification_states())

    def test_restored_notification_store_never_sends_twice(self) -> None:
        store = notify.store_path(self.cfg)
        self.n8n.email_up = False
        self.run_daily()
        backup = store.read_bytes()
        self.n8n.email_up = True
        self.run_daily()
        self.assertEqual(2, len(self.n8n.sent))
        store.write_bytes(backup)  # a stale store: both notices PENDING again
        self.run_daily()
        self.assertEqual(2, len(self.n8n.sent), "the outbox dispatch marker stops a second send")
        self.assertEqual({"HOLD"}, {state for _k, _s, state in self.notification_states()})

    def test_restored_store_and_outbox_are_caught_by_the_workflow_claim(self) -> None:
        store = notify.store_path(self.cfg)
        self.n8n.email_up = False
        self.run_daily()
        backup = store.read_bytes()
        self.n8n.email_up = True
        self.run_daily()
        store.write_bytes(backup)
        for marker in self.cfg.outbox_root.glob("*.n8n-dispatch.json"):
            marker.unlink()
        self.run_daily()
        self.assertEqual(2, len(self.n8n.sent), "the n8n claim refuses a second send (409)")
        self.assertEqual({"UNKNOWN"}, {state for _k, _s, state in self.notification_states()})

    def test_historic_sent_notifications_are_never_resent_after_the_switch(self) -> None:
        # Classic Claude-bridge history first (Gmail drafts), then the daily run on the same state.
        classic = self.classic_cfg()
        self.cfg = classic
        self.acquire()
        host_cfg = self.make_config(daily=None)
        self.assertIsNone(host_cfg.daily)
        for _ in range(8):
            step = self.next()
            if step["outcome"] == "DRIVE_PRECHECK":
                evidence = {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                            "query": step["precheck_query"], "matches": []}
                sealed = self.call(bridge.drive_seal, step["intent_id"], evidence)
                self.assertEqual("SEALED", sealed["outcome"])
                self.n8n.sync()
                found = [k for k, r in self.n8n.cloud.items() if r["name"] == step["display_filename"]][0]
                meta = {"id": found, "title": step["display_filename"], "parentId": FLAT,
                        "mimeType": "application/pdf", "fileSize": str(step["byte_size"])}
                self.call(bridge.drive_record, step["intent_id"], {"outcome": "CREATED", "file": meta})
            elif step["outcome"] == "NOTIFY_DRAFT":
                self.host.notify_step(step)
            else:
                break
        self.assertEqual(2, len(self.host.sent))
        self.cfg = self.make_config()
        result = self.run_daily()
        self.assertEqual([], self.n8n.sent, "historic SENT notices are never sent again")
        self.assertEqual(daily.SUBMIT, result["monitor_report"])

    def test_n8n_transport_and_gmail_draft_transport_never_mix(self) -> None:
        self.run_daily()
        nid = [r for r in self.status()["notifications"]["items"]][0]
        with StateV4Store(self.db) as state:
            pass
        rows = []
        with notify.NotificationStore(notify.store_path(self.cfg)) as store:
            rows = store.rows()
        row = rows[0]
        self.assertTrue(row["draft_id"].startswith("egd-"))
        with self.assertRaises(NativeRefusal) as caught:
            self.notify(lambda store: notify.record(store, row["notification_id"], {
                "outcome": "SENT", "draft_id": row["draft_id"], "message_id": "MSG1"}, self.now_iso()))
        self.assertEqual("EG_NATIVE_TRANSPORT_MISMATCH", caught.exception.support_ref)
        self.assertTrue(nid)


class EmailEvidenceMappingTests(unittest.TestCase):
    """Every n8n answer maps to exactly one typed record outcome; only an exact SENT echo is SENT."""

    request = {"notification_id": "egn-" + "1" * 32, "dispatch_id": "egd-" + "2" * 32}

    def body(self, **fields) -> dict:
        return {"schema": notify.EMAIL_RESPONSE_SCHEMA, "mode": "SEND", **self.request, **fields}

    def test_mapping(self) -> None:
        A = loopback.Answer
        cases = [
            (A(loopback.OK, 200, self.body(outcome="SENT", message_id="18c0ffee")), "SENT"),
            (A(loopback.OK, 200, self.body(outcome="SENT")), "ERROR"),
            (A(loopback.OK, 200, self.body(outcome="SENT", message_id="bad id")), "ERROR"),
            (A(loopback.OK, 200, {**self.body(outcome="SENT", message_id="18c0ffee"), "dispatch_id": "egd-" + "3" * 32}),
             "ERROR"),
            (A(loopback.HTTP_STATUS, 422, self.body(outcome="REFUSED", reason="EG_EMAIL_RENDER_MISMATCH")), "DENIED"),
            (A(loopback.HTTP_STATUS, 409, self.body(outcome="DUPLICATE")), "ERROR"),
            (A(loopback.HTTP_STATUS, 502, self.body(outcome="SEND_UNCERTAIN")), "ERROR"),
            (A(loopback.HTTP_STATUS, 500, {"outcome": "FAILED"}), "ERROR"),
            (A(loopback.HTTP_STATUS, 401, None), "DENIED"),
            (A(loopback.HTTP_STATUS, 403, None), "DENIED"),
            (A(loopback.HTTP_STATUS, 404, None), "DENIED"),
            (A(loopback.NOT_SENT, support_ref="EG_N8N_UNREACHABLE"), "DENIED"),
            (A(loopback.SENT_UNCERTAIN, support_ref="EG_N8N_TRANSPORT_UNCERTAIN"), "NO_RESPONSE"),
        ]
        for answer, outcome in cases:
            with self.subTest(status=answer.status, outcome=answer.outcome):
                evidence = daily.email_evidence(self.request, answer)
                self.assertEqual((outcome, notify.TRANSPORT_N8N, self.request["dispatch_id"]),
                                 (evidence["outcome"], evidence["transport"], evidence["draft_id"]))


class LeaseAndCeilingTests(DailyBase):
    def test_lease_held_by_another_writer_withholds_and_changes_nothing(self) -> None:
        with StateV4Store(self.db) as other:
            other.acquire_lease("other-writer", now_ms=self.clock + 86_400_000 + 5000, ttl_ms=10 ** 9)
            result = self.run_daily()
        self.assertEqual(daily.WITHHELD, result["monitor_report"])
        self.assertIn("EG_DAILY_ACQUIRE_REFUSED", result["withheld_reasons"])
        self.assertEqual((0, 0), self.dispatches())

    def test_drive_ceiling_is_visible_and_bounded(self) -> None:
        self.ceilings = (1, 8)
        self.cfg = self.make_config(max_drive_uploads=1)
        result = self.run_daily()
        self.assertEqual(1, self.dispatches()[0])
        self.assertEqual("BLOCKED", result["status_outcome"])
        self.assertEqual(1, len(self.n8n.failures()), "one DRIVE_CEILING_REACHED notice")

    def test_deadline_bounds_the_run(self) -> None:
        self.n8n.sync_enabled = False
        self.run_daily()
        self.assertLess(self.fake_clock.slept, self.deadline * 60)


class PreflightTests(DailyBase):
    def test_preflight_passes_after_a_clean_run(self) -> None:
        self.run_daily()
        result = daily.preflight(self.cfg, client=self.n8n, now_ms=self.tick())
        self.assertEqual(("PASS", []), (result["outcome"], result["holds"]))
        self.assertEqual("PASS", result["checks"]["google_sha256_EB_BILL"])
        self.assertEqual("2", result["checks"]["legacy_gmail_send_pending"])

    def test_preflight_holds_on_pending_notices_or_unproven_google_hash(self) -> None:
        self.n8n.email_up = False
        self.run_daily()
        self.n8n.sha_ready = False
        result = daily.preflight(self.cfg, client=self.n8n, now_ms=self.tick())
        self.assertEqual("HOLD", result["outcome"])
        self.assertIn("notification_in_flight", result["holds"])
        self.assertIn("owner_email_probe", result["holds"])
        self.assertIn("google_sha256_EB_BILL", result["holds"])

    def test_preflight_holds_when_a_verified_bill_was_never_notified(self) -> None:
        self.run_daily()
        with contextlib.closing(sqlite3.connect(notify.store_path(self.cfg))) as connection, connection:
            connection.execute("DROP TRIGGER IF EXISTS gmail_seal_no_delete")
            connection.execute("DELETE FROM gmail_seal")
            connection.execute("DELETE FROM notification WHERE kind='SUCCESS' AND stream='EB_BILL'")
        result = daily.preflight(self.cfg, client=self.n8n, now_ms=self.tick())
        self.assertIn("notification_success_history", result["holds"])


class DailyConfigTests(unittest.TestCase):
    def raw(self, root: Path) -> dict:
        from test_native_bridge import ConfigTests

        raw = ConfigTests().raw(root)
        raw["drive"] = {"account": "uat@example.com", "folder_ids": {EB: FLAT, TN: FLAT}, "mode": "LOCAL_SYNC",
                        "sync_folders": {EB: str(root / "sync"), TN: str(root / "sync")}}
        raw["daily"] = {
            "schema": bridge.DAILY_SCHEMA, "runs_root": str(root / "runs"), "drive_reader_account": READER,
            "n8n": {"port": 5678, "token_file": str(root / "token.txt"), "header_name": "X-EnergyGrid-Monitor-Key"},
            "verify_window_minutes": 20, "verify_poll_seconds": 60, "run_deadline_minutes": 75,
        }
        return raw

    def test_daily_block_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = bridge.load_native_config(self.raw(Path(directory)), checkout_root=REPO_ROOT)
            self.assertEqual((5678, 20, 60, 75), (cfg.daily.n8n_port, cfg.daily.verify_window_minutes,
                                                  cfg.daily.verify_poll_seconds, cfg.daily.run_deadline_minutes))

    def test_committed_daily_example_loads(self) -> None:
        example = REPO_ROOT / "energygrid-bill-downloader" / "config" / "energygrid.native_daily.example.json"
        cfg = bridge.load_native_config(json.loads(example.read_text(encoding="utf-8")), checkout_root=REPO_ROOT)
        self.assertIsNotNone(cfg.daily)
        self.assertEqual(bridge.DRIVE_LOCAL_SYNC, cfg.drive.mode)

    def test_invalid_daily_blocks_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "extra key": lambda r: r["daily"].update(extra=1),
                "bad schema": lambda r: r["daily"].update(schema="x"),
                "native upload": lambda r: r.update(drive={"account": "uat@example.com", "mode": "NATIVE_UPLOAD",
                                                           "folder_ids": {EB: FLAT, TN: FLAT}}),
                "two folders": lambda r: r["drive"].update(folder_ids={EB: FLAT, TN: "FOLDER_TN_SYNTHETIC_0002"},
                                                          sync_folders={EB: str(root / "a"), TN: str(root / "b")}),
                "attach pdf": lambda r: r["gmail"].update(attach_pdf=True),
                "window too long": lambda r: r["daily"].update(verify_window_minutes=61),
                "window not inside deadline": lambda r: r["daily"].update(verify_window_minutes=60,
                                                                           run_deadline_minutes=60),
                "poll too fast": lambda r: r["daily"].update(verify_poll_seconds=5),
                "deadline too long": lambda r: r["daily"].update(run_deadline_minutes=151),
                "bad port": lambda r: r["daily"]["n8n"].update(port=0),
                "bad header": lambda r: r["daily"]["n8n"].update(header_name="Authorization"),
                "token in checkout": lambda r: r["daily"]["n8n"].update(token_file=str(REPO_ROOT / "token.txt")),
                "relative runs root": lambda r: r["daily"].update(runs_root="runs"),
                "bad reader": lambda r: r["daily"].update(drive_reader_account="nobody"),
                "missing n8n": lambda r: r["daily"].pop("n8n"),
            }
            for name, change in cases.items():
                raw = json.loads(json.dumps(self.raw(root)))
                change(raw)
                with self.subTest(case=name), self.assertRaises(ConfigError):
                    bridge.load_native_config(raw, checkout_root=REPO_ROOT)


class DailyCliTests(unittest.TestCase):
    def test_daily_without_a_daily_block_is_refused_with_no_change(self) -> None:
        from energygrid_bill_downloader import native_cli
        from test_native_bridge import ConfigTests

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "native.json"
            path.write_text(json.dumps(ConfigTests().raw(root)), encoding="utf-8")
            for command in ("daily", "daily-preflight", "drive-verify"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = native_cli.main([command, "--config", str(path)])
                result = json.loads(out.getvalue())
                with self.subTest(command=command):
                    self.assertEqual((64, "EG_DAILY_CONFIG_REQUIRED"), (code, result["support_ref"]))
            self.assertFalse((root / "runs").exists())


if __name__ == "__main__":
    unittest.main()
