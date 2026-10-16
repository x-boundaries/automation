from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from energygrid_bill_downloader.config import OFFLINE_FAKE_PROVIDER_PROFILE
from energygrid_bill_downloader.connector_actions import (
    ACTION_DRIVE_UPLOAD,
    ACTION_GMAIL_SEND,
    FakeDriveConnector,
    FakeGmailConnector,
    FakeProviderLedger,
    SimulatedCrash,
)
from energygrid_bill_downloader.errors import OfflineV4Error, SourceTransportError
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.naming import display_filename
from energygrid_bill_downloader.publication import filename_key
from energygrid_bill_downloader.orchestration import run_offline_dual_stream_v4
from energygrid_bill_downloader.state import StateV4Store, migrate_state_database_v4
from fixtures.synthetic_dual_stream import SYNTHETIC_NAMESPACE, SyntheticAdapter, candidate, create_v3_database, snapshot
from test_state_v4 import (
    LATEST_SOURCE,
    build_predecessor_v3,
    create_predecessor,
    insert_predecessor,
    predecessor_pdf,
)


EB = "EB_BILL"
TN = "TENANT_BILL"
DRIVE = ACTION_DRIVE_UPLOAD
GMAIL = ACTION_GMAIL_SEND
SECOND = 1_000_000  # run clock step; well beyond the default lease TTL


def pdf(tag: str) -> bytes:
    return f"%PDF-1.4\n{tag}\n%%EOF\n".encode("ascii")


def run_id(n: int) -> str:
    return f"11111111-2222-4333-8444-{n:012d}"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def crash_at(point: str, *, nth: int = 1):
    seen = {"count": 0}

    def hook(name: str) -> None:
        if name == point:
            seen["count"] += 1
            if seen["count"] == nth:
                raise SimulatedCrash(point)

    return hook


class OfflineFakeAdapter(SyntheticAdapter):
    """A test double declared as an offline fake. The offline core accepts only these."""

    OFFLINE_FAKE = True


class FailingAdapter:
    """A portal that cannot list or fetch. Used for the acquisition-failure scenario."""

    OFFLINE_FAKE = True

    def inventory(self, ceiling: int):
        raise SourceTransportError("EG_SYNTHETIC_PORTAL_DOWN")

    def acquire(self, item, destination: Path):
        raise SourceTransportError("EG_SYNTHETIC_PORTAL_DOWN")


class OfflineV4RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "state.sqlite"
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.create_database()
        migrate_state_database_v4(self.db, apply=True)
        self.temp = self.root / "temp"
        self.temp.mkdir()
        self.ledger = FakeProviderLedger()
        self.drive = FakeDriveConnector(self.ledger)
        self.gmail = FakeGmailConnector(self.ledger)
        self.clock = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def adapter(self, stream: str, *items: tuple[str, str, bytes]) -> SyntheticAdapter:
        """items are (source filename, invoice date, bytes) for one stream."""
        source = Stream(stream)
        cands = tuple(candidate(source, name=name, invoice_date=day) for name, day, _data in items)
        files = {name: data for name, _day, data in items}
        return OfflineFakeAdapter(snapshot(source, cands), files)

    def create_database(self) -> None:
        create_predecessor(self.db)

    def run_once(self, n: int, adapters: dict, *, checkpoint=None, drive=None, gmail=None,
                 owner: str = "session-a", now_ms: int | None = None) -> dict:
        if now_ms is None:
            self.clock = self.clock + SECOND
            now_ms = self.clock
        connectors = {DRIVE: drive or self.drive, GMAIL: gmail or self.gmail}
        with StateV4Store(self.db) as store:
            return run_offline_dual_stream_v4(
                state=store,
                adapters=adapters,
                connectors=connectors,
                archive_root=self.archive,
                temp_root=self.temp,
                run_id=run_id(n),
                owner_token=owner,
                provider_profile=OFFLINE_FAKE_PROVIDER_PROFILE,
                now_ms=now_ms,
                checkpoint=checkpoint,
            )

    def effects(self, action: str) -> list[dict]:
        return [entry for entry in self.ledger.effects if entry["action"] == action]

    def steps(self, result: dict) -> list[tuple[str, str, str]]:
        return result["drain"]["steps"]

    def intent_state(self, stream_invoice_day: str, action: str) -> str:
        with StateV4Store(self.db) as store:
            invoices = [inv for stream in V4_STREAMS_ORDER for inv in store.invoices_for_stream(stream)]
            matches = [inv for inv in invoices if inv["invoice_date"] == stream_invoice_day]
            self.assertEqual(1, len(matches))
            return store.intents_for_invoice(matches[0]["invoice_key"])[action]["state"]

    def standard_adapters(self) -> dict:
        return {
            EB: self.adapter(EB, ("Invoice EB.pdf", "2026-10-01", pdf("eb-1"))),
            TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1"))),
        }


V4_STREAMS_ORDER = (EB, TN)


class HappyPathTests(OfflineV4RunTests):
    def test_both_streams_drain_fairly_with_drive_before_gmail_for_each_invoice(self) -> None:
        result = self.run_once(1, self.standard_adapters())
        self.assertEqual(
            [(EB, DRIVE, "VERIFIED"), (TN, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED"), (TN, GMAIL, "VERIFIED")],
            self.steps(result),
        )
        self.assertEqual(4, len(self.ledger.effects))
        self.assertEqual({"DRIVE": 2, "GMAIL": 2}, {"DRIVE": len(self.effects(DRIVE)), "GMAIL": len(self.effects(GMAIL))})
        self.assertEqual({"DRAINED"}, {row["state"] for row in result["drain"]["streams"].values()})

    def test_archive_sha_identity_is_the_same_on_disk_in_the_drive_copy_and_in_the_attachment(self) -> None:
        data = pdf("eb-1")
        self.run_once(1, self.standard_adapters())
        archived = self.archive / "EB Bill" / "2026-10-01.pdf"
        self.assertEqual(data, archived.read_bytes())
        with StateV4Store(self.db) as store:
            invoice = store.invoice_by_identity(EB, SYNTHETIC_NAMESPACE, filename_key("Invoice EB.pdf"))
            self.assertIsNotNone(invoice)
            self.assertEqual(sha(data), invoice["sha256"])
            self.assertEqual("OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf", invoice["display_filename"])
            self.assertEqual("EB Bill/2026-10-01.pdf", invoice["archive_relpath"])
        drive_copy = next(entry for entry in self.effects(DRIVE) if entry["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf")
        attachment = next(entry for entry in self.effects(GMAIL) if entry["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf")
        self.assertEqual(sha(data), drive_copy["sha256"])
        self.assertEqual(sha(data), attachment["sha256"])
        self.assertEqual(display_filename("Invoice EB.pdf", date(2026, 10, 1), stream=EB), drive_copy["display_filename"])

    def test_an_immediate_second_run_creates_zero_uploads_and_zero_emails(self) -> None:
        first = self.run_once(1, self.standard_adapters())
        self.assertEqual(4, len(self.ledger.effects))
        second = self.run_once(2, self.standard_adapters())
        self.assertEqual([], self.steps(second))
        self.assertEqual(4, len(self.ledger.effects))
        self.assertEqual({"ALREADY_ADMITTED"}, {row["status"] for row in second["acquisition"].values()})
        self.assertEqual({"DRAINED"}, {row["state"] for row in second["drain"]["streams"].values()})
        self.assertEqual(first["acquisition"][EB]["status"], "ADMITTED")

    def test_an_empty_stream_is_idle_and_does_not_block_the_other_stream(self) -> None:
        adapters = {EB: self.adapter(EB), TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))}
        result = self.run_once(1, adapters)
        self.assertEqual("EMPTY", result["acquisition"][EB]["status"])
        self.assertEqual(2, len(self.ledger.effects))
        self.assertEqual("DRAINED", result["drain"]["streams"][EB]["state"])

    def test_an_ambiguous_latest_selection_admits_nothing_for_that_stream(self) -> None:
        adapters = {
            EB: self.adapter(EB, ("first.pdf", "2026-10-01", pdf("a")), ("second.pdf", "2026-10-01", pdf("b"))),
            TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1"))),
        }
        result = self.run_once(1, adapters)
        self.assertEqual("LATEST_AMBIGUOUS", result["acquisition"][EB]["status"])
        self.assertEqual(0, len([e for e in self.ledger.effects if e["display_filename"].endswith("first.pdf")]))
        self.assertEqual(2, len(self.ledger.effects))


class CatchUpAndFairnessTests(OfflineV4RunTests):
    def test_pending_invoices_admitted_earlier_drain_before_newer_invoices_in_both_streams(self) -> None:
        # Run 1 admits and archives the first invoice of each stream, then dies before any dispatch.
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        self.assertEqual(0, len(self.ledger.effects))
        newer = {
            EB: self.adapter(EB, ("Invoice EB.pdf", "2026-10-01", pdf("eb-1")), ("Invoice EB 5.pdf", "2026-10-05", pdf("eb-5"))),
            TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")), ("Invoice TN 6.pdf", "2026-10-06", pdf("tn-6"))),
        }
        result = self.run_once(2, newer)
        self.assertEqual(
            [
                (EB, DRIVE, "VERIFIED"), (TN, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED"), (TN, GMAIL, "VERIFIED"),
                (EB, DRIVE, "VERIFIED"), (TN, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED"), (TN, GMAIL, "VERIFIED"),
            ],
            self.steps(result),
        )
        drive_names = [entry["display_filename"] for entry in self.effects(DRIVE)]
        self.assertEqual(
            ["OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf", "OCT 2026 - EnergyGrid - Tenant Bill - Invoice TN.pdf", "OCT 2026 - EnergyGrid - EB Bill - Invoice EB 5.pdf", "OCT 2026 - EnergyGrid - Tenant Bill - Invoice TN 6.pdf"],
            drive_names,
        )
        self.assertEqual(8, len(self.ledger.effects))

    def test_a_stream_with_more_pending_work_never_starves_the_other_stream(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        newer = {
            EB: self.adapter(EB, ("Invoice EB 3.pdf", "2026-10-03", pdf("eb-3"))),
            TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1"))),
        }
        result = self.run_once(2, newer)
        first_tenant_step = next(index for index, step in enumerate(self.steps(result)) if step[0] == TN)
        self.assertLessEqual(first_tenant_step, 1)


class CrashAndRestartTests(OfflineV4RunTests):
    def test_crash_after_fetch_leaves_no_archive_and_no_admission(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("after_fetch"))
        self.assertFalse((self.archive / "EB Bill" / "2026-10-01.pdf").exists())
        with StateV4Store(self.db) as store:
            self.assertEqual(0, store.count("energygrid_v4_invoice"))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual({"ADMITTED"}, {row["status"] for row in result["acquisition"].values()})
        self.assertEqual(4, len(self.ledger.effects))

    def test_crash_after_acquisition_drains_the_admitted_work_on_the_next_run(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("after_acquisition"))
        self.assertEqual(0, len(self.ledger.effects))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual(
            {EB: "ALREADY_ADMITTED", TN: "ALREADY_ADMITTED"},
            {key: row["status"] for key, row in result["acquisition"].items()},
        )
        self.assertEqual(4, len(self.steps(result)))
        self.assertEqual(4, len(self.ledger.effects))

    def test_a_run_whose_lease_was_taken_over_stops_before_calling_the_provider(self) -> None:
        state = {"taken": False}

        def takeover(name: str) -> None:
            # Another session starts after this run's lease has expired, and drives the stream to completion.
            if name == "after_seal" and not state["taken"]:
                state["taken"] = True
                self.run_once(2, self.standard_adapters(), owner="session-b", now_ms=self.clock + 2 * SECOND)

        with self.assertRaises(OfflineV4Error) as caught:
            self.run_once(1, self.standard_adapters(), checkpoint=takeover)
        self.assertEqual("EG_V4_LEASE_LOST", caught.exception.support_ref)
        self.assertEqual(0, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))
        self.assertEqual("UNKNOWN", self.intent_state("2026-10-01", DRIVE))
        # Only the second session's Tenant Bill upload and email; the stale session made no call.
        self.assertEqual(2, len(self.ledger.effects))

    def test_a_takeover_before_a_fetch_means_the_stale_session_fetches_nothing(self) -> None:
        state = {"taken": False}
        stale = self.standard_adapters()

        def takeover(name: str) -> None:
            if name == "before_fetch" and not state["taken"]:
                state["taken"] = True
                self.run_once(2, self.standard_adapters(), owner="session-b", now_ms=self.clock + 2 * SECOND)

        with self.assertRaises(OfflineV4Error) as caught:
            self.run_once(1, stale, checkpoint=takeover)
        self.assertEqual("EG_V4_LEASE_LOST", caught.exception.support_ref)
        self.assertEqual([], stale[EB].acquire_calls)
        self.assertEqual(4, len(self.ledger.effects))

    def test_a_takeover_between_streams_stops_before_the_other_stream_is_listed(self) -> None:
        state = {"taken": False}
        stale = self.standard_adapters()

        def takeover(name: str) -> None:
            if name == "after_admission" and not state["taken"]:
                state["taken"] = True
                self.run_once(2, self.standard_adapters(), owner="session-b", now_ms=self.clock + 2 * SECOND)

        with self.assertRaises(OfflineV4Error) as caught:
            self.run_once(1, stale, checkpoint=takeover)
        self.assertEqual("EG_V4_LEASE_LOST", caught.exception.support_ref)
        self.assertEqual(0, stale[TN].inventory_calls)
        self.assertEqual([], stale[TN].acquire_calls)
        self.assertEqual(4, len(self.ledger.effects))

    def test_a_takeover_during_admission_means_the_stale_session_publishes_nothing(self) -> None:
        state = {"taken": False}

        def takeover(name: str) -> None:
            if name == "after_fetch" and not state["taken"]:
                state["taken"] = True
                adapters = {EB: self.adapter(EB), TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))}
                self.run_once(2, adapters, owner="session-b", now_ms=self.clock + 2 * SECOND)

        with self.assertRaises(OfflineV4Error) as caught:
            self.run_once(1, self.standard_adapters(), checkpoint=takeover)
        self.assertEqual("EG_V4_LEASE_LOST", caught.exception.support_ref)
        self.assertFalse((self.archive / "EB Bill" / "2026-10-01.pdf").exists())
        with StateV4Store(self.db) as store:
            self.assertEqual(1, store.count("energygrid_v4_invoice"))
        self.assertEqual(2, len(self.ledger.effects))

    def test_crash_after_archive_before_admission_reuses_the_identical_file_without_a_second_copy(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("after_archive"))
        self.assertEqual([], [p.name for p in (self.archive / "EB Bill").iterdir() if p.name != "2026-10-01.pdf"])
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual({"ADMITTED"}, {row["status"] for row in result["acquisition"].values()})
        self.assertEqual(sorted(["2026-10-01.pdf"]), sorted(p.name for p in (self.archive / "EB Bill").iterdir()))
        self.assertEqual(4, len(self.ledger.effects))

    def test_crash_after_admission_is_not_readmitted_and_each_invoice_dispatches_once(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("after_admission"))
        self.assertEqual(0, len(self.ledger.effects))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual({EB: "ALREADY_ADMITTED", TN: "ADMITTED"}, {k: v["status"] for k, v in result["acquisition"].items()})
        with StateV4Store(self.db) as store:
            self.assertEqual(2, store.count("energygrid_v4_invoice"))
        self.assertEqual(4, len(self.ledger.effects))

    def test_crash_before_seal_leaves_intents_pending_and_the_next_run_dispatches_each_once(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        self.assertEqual(0, len(self.ledger.effects))
        self.assertEqual("PENDING", self.intent_state("2026-10-01", DRIVE))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual(4, len(self.ledger.effects))
        self.assertEqual(4, len(self.steps(result)))

    def test_crash_after_seal_before_the_provider_call_is_unknown_and_never_resent(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("after_seal"))
        self.assertEqual(0, len(self.ledger.effects))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual(1, result["recovered_unknown"])
        self.assertEqual("UNKNOWN", self.intent_state("2026-10-01", DRIVE))
        self.assertEqual({"BLOCKED", "DRAINED"}, {row["state"] for row in result["drain"]["streams"].values()})
        self.assertEqual("EG_V4_DISPATCH_OUTCOME_UNKNOWN", result["drain"]["streams"][EB]["support_ref"])
        self.assertEqual([(TN, DRIVE, "VERIFIED"), (TN, GMAIL, "VERIFIED")], self.steps(result))
        self.assertEqual(0, len([e for e in self.ledger.effects if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))
        again = self.run_once(3, self.standard_adapters())
        self.assertEqual([], self.steps(again))
        self.assertEqual(2, len(self.ledger.effects))

    def test_crash_after_the_provider_effect_but_before_its_receipt_is_unknown_with_no_duplicate(self) -> None:
        crashing_drive = FakeDriveConnector(self.ledger, crash_after_effect=True)
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), drive=crashing_drive)
        self.assertEqual(1, len(self.effects(DRIVE)))
        fresh_drive = FakeDriveConnector(self.ledger)
        result = self.run_once(2, self.standard_adapters(), drive=fresh_drive)
        self.assertEqual("UNKNOWN", self.intent_state("2026-10-01", DRIVE))
        # Only TENANT_BILL's upload reaches the provider; the held EB upload is never resent.
        self.assertEqual(1, fresh_drive.calls)
        self.assertEqual(1, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))
        self.assertEqual("BLOCKED", result["drain"]["streams"][EB]["state"])

    def test_crash_after_a_verified_receipt_resumes_at_gmail_without_repeating_drive(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("after_receipt"))
        self.assertEqual("VERIFIED", self.intent_state("2026-10-01", DRIVE))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual(EB, self.steps(result)[0][0])
        self.assertEqual(GMAIL, self.steps(result)[0][1])
        self.assertEqual(1, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))
        self.assertEqual(4, len(self.ledger.effects))

    def test_a_lost_provider_response_after_the_effect_holds_only_that_stream(self) -> None:
        lost_drive = FakeDriveConnector(self.ledger, fail_after_effect=True)
        result = self.run_once(1, self.standard_adapters(), drive=lost_drive)
        self.assertEqual("UNKNOWN", self.intent_state("2026-10-01", DRIVE))
        self.assertEqual("BLOCKED", result["drain"]["streams"][EB]["state"])
        self.assertEqual("EG_V4_PROVIDER_UNKNOWN", result["drain"]["streams"][EB]["support_ref"])
        self.assertEqual("DRAINED", result["drain"]["streams"][TN]["state"])
        self.assertEqual(1, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))
        self.assertEqual(0, len([e for e in self.effects(GMAIL) if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))

    def test_an_altered_archive_holds_the_pending_intent_and_sends_nothing_for_it(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        (self.archive / "EB Bill" / "2026-10-01.pdf").write_bytes(pdf("eb-2"))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual("HOLD", self.intent_state("2026-10-01", DRIVE))
        self.assertEqual("EG_V4_ARCHIVE_IDENTITY_CHANGED", result["drain"]["streams"][EB]["support_ref"])
        self.assertEqual(2, len(self.ledger.effects))
        self.assertEqual(0, len([e for e in self.ledger.effects if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))


class PortalAndAdmissionTests(OfflineV4RunTests):
    def test_portal_failure_for_one_stream_does_not_stop_its_pending_invoice(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        adapters = {EB: FailingAdapter(), TN: self.adapter(TN, ("Invoice TN 6.pdf", "2026-10-06", pdf("tn-6")))}
        result = self.run_once(2, adapters)
        self.assertEqual("PORTAL_ACQUISITION_FAILED", result["acquisition"][EB]["status"])
        self.assertEqual("EG_SYNTHETIC_PORTAL_DOWN", result["acquisition"][EB]["support_ref"])
        self.assertEqual("ADMITTED", result["acquisition"][TN]["status"])
        eb_steps = [step for step in self.steps(result) if step[0] == EB]
        self.assertEqual([(EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")], eb_steps)
        self.assertEqual({EB: "DRAINED", TN: "DRAINED"}, {k: v["state"] for k, v in result["drain"]["streams"].items()})

    def test_a_display_name_collision_is_a_hold_before_any_fetch_and_the_older_drain_continues(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        eb_next = self.adapter(EB, ("OCT 2026 - Invoice EB.pdf", "2026-10-20", pdf("eb-20")))
        adapters = {EB: eb_next, TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))}
        result = self.run_once(2, adapters)
        self.assertEqual("HOLD", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_DISPLAY_NAME_COLLISION", result["acquisition"][EB]["support_ref"])
        self.assertEqual([], eb_next.acquire_calls)
        self.assertFalse((self.archive / "EB Bill" / "2026-10-20.pdf").exists())
        self.assertEqual(4, len(self.ledger.effects))
        # The older admitted invoice drained; drain truth is kept and the outcome is the visible HOLD.
        self.assertEqual("DRAINED", result["drain"]["streams"][EB]["state"])
        self.assertEqual("HOLD", result["streams"][EB]["outcome"])
        self.assertEqual("HOLD", result["overall"])

    def test_a_second_session_is_refused_while_a_run_holds_the_lease_and_mutates_nothing(self) -> None:
        with StateV4Store(self.db) as holder:
            holder.acquire_lease("session-a", now_ms=5_000, ttl_ms=600_000)
            with self.assertRaises(OfflineV4Error) as caught:
                self.run_once(1, self.standard_adapters(), owner="session-b", now_ms=6_000)
            self.assertEqual("EG_V4_LEASE_HELD", caught.exception.support_ref)
            with StateV4Store(self.db) as view:
                self.assertEqual(0, view.count("energygrid_v4_invoice"))
            holder.release_lease("session-a")
        self.assertEqual(0, len(self.ledger.effects))
        result = self.run_once(2, self.standard_adapters(), owner="session-b", now_ms=7_000)
        self.assertEqual(4, len(self.ledger.effects))
        self.assertEqual(4, len(self.steps(result)))

    def test_the_lease_is_released_after_an_ordinary_error(self) -> None:
        def boom(name: str) -> None:
            if name == "after_acquisition":
                raise RuntimeError("EG_TEST_ORDINARY_FAILURE")

        with self.assertRaises(RuntimeError):
            self.run_once(1, self.standard_adapters(), checkpoint=boom, now_ms=1)
        with StateV4Store(self.db) as store:
            store.acquire_lease("session-b", now_ms=2, ttl_ms=600_000)

    def test_invalid_run_identity_and_live_or_non_fake_connectors_are_refused_before_any_work(self) -> None:
        with StateV4Store(self.db) as store:
            with self.assertRaises(OfflineV4Error) as caught:
                run_offline_dual_stream_v4(
                    state=store, adapters=self.standard_adapters(),
                    connectors={DRIVE: self.drive, GMAIL: self.gmail},
                    archive_root=self.archive, temp_root=self.temp, run_id="not-a-uuid",
                    owner_token="s", provider_profile=OFFLINE_FAKE_PROVIDER_PROFILE, now_ms=1,
                )
            self.assertEqual("EG_V4_RUN_ID_INVALID", caught.exception.support_ref)
            with self.assertRaises(OfflineV4Error) as caught:
                run_offline_dual_stream_v4(
                    state=store, adapters=self.standard_adapters(),
                    connectors={DRIVE: self.drive, GMAIL: self.gmail},
                    archive_root=self.archive, temp_root=self.temp, run_id=run_id(9),
                    owner_token="s", provider_profile="LIVE", now_ms=1,
                )
            self.assertEqual("EG_V4_LIVE_PROFILE_REFUSED", caught.exception.support_ref)
            with self.assertRaises(OfflineV4Error) as caught:
                run_offline_dual_stream_v4(
                    state=store, adapters=self.standard_adapters(),
                    connectors={DRIVE: mock.Mock(), GMAIL: self.gmail},
                    archive_root=self.archive, temp_root=self.temp, run_id=run_id(9),
                    owner_token="s", provider_profile=OFFLINE_FAKE_PROVIDER_PROFILE, now_ms=1,
                )
            self.assertEqual("EG_V4_CONNECTOR_NOT_FAKE", caught.exception.support_ref)
            self.assertEqual(0, store.count("energygrid_v4_invoice"))
        self.assertEqual(0, len(self.ledger.effects))


class SentBytesAreHashedTests(OfflineV4RunTests):
    def test_the_bytes_sent_are_hashed_not_only_the_validated_read(self) -> None:
        from energygrid_bill_downloader import orchestration
        from energygrid_bill_downloader.publication import FileInfo

        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        original = pdf("eb-1")
        (self.archive / "EB Bill" / "2026-10-01.pdf").write_bytes(pdf("eb-2"))
        real_validate = orchestration.validate_pdf

        def validate_as_original(path):
            if "Tenant" in str(path):
                return real_validate(path)
            return FileInfo(byte_size=len(original), sha256=sha(original))

        with mock.patch.object(orchestration, "validate_pdf", side_effect=validate_as_original):
            result = self.run_once(2, self.standard_adapters())
        self.assertEqual("HOLD", self.intent_state("2026-10-01", DRIVE))
        self.assertEqual("EG_V4_ARCHIVE_IDENTITY_CHANGED", result["drain"]["streams"][EB]["support_ref"])
        self.assertEqual(0, len([e for e in self.ledger.effects if e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf"]))


class PortalRegressionAndBoundaryTests(OfflineV4RunTests):
    def test_a_portal_latest_that_goes_backwards_is_refused_and_the_older_invoice_is_not_sent(self) -> None:
        newer = {
            EB: self.adapter(EB, ("Invoice EB 5.pdf", "2026-10-05", pdf("eb-5"))),
            TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1"))),
        }
        self.run_once(1, newer)
        self.assertEqual(4, len(self.ledger.effects))
        older = {
            EB: self.adapter(EB, ("Invoice EB.pdf", "2026-10-01", pdf("eb-1"))),
            TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1"))),
        }
        result = self.run_once(2, older)
        self.assertEqual("ADMISSION_REFUSED", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_SOURCE_REGRESSION", result["acquisition"][EB]["support_ref"])
        self.assertEqual(4, len(self.ledger.effects))
        self.assertFalse(any(e["display_filename"] == "OCT 2026 - EnergyGrid - EB Bill - Invoice EB.pdf" for e in self.ledger.effects))

    def test_a_plain_portal_adapter_is_refused_before_any_work_or_lease(self) -> None:
        plain = SyntheticAdapter(
            snapshot(Stream.EB_BILL, (candidate(Stream.EB_BILL, name="Invoice EB.pdf", invoice_date="2026-10-01"),)),
            {"Invoice EB.pdf": pdf("eb-1")},
        )
        with self.assertRaises(OfflineV4Error) as caught:
            self.run_once(1, {EB: plain, TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))})
        self.assertEqual("EG_V4_ADAPTER_NOT_FAKE", caught.exception.support_ref)
        self.assertEqual(0, len(self.ledger.effects))
        with StateV4Store(self.db) as store:
            self.assertEqual(0, store.count("energygrid_v4_invoice"))
            store.acquire_lease("after-refusal", now_ms=1, ttl_ms=10)


class PredecessorCutoverTests(OfflineV4RunTests):
    """A v3 database that already holds the portal's latest invoice must not see it re-delivered."""

    def create_database(self) -> None:
        create_v3_database(self.db)

    def setUp(self) -> None:
        super().setUp()
        insert_predecessor(self.db, source_filename="Invoice EB.pdf", day="2026-10-01")

    def test_an_uncommitted_predecessor_latest_is_held_and_never_sent(self) -> None:
        result = self.run_once(1, self.standard_adapters())
        self.assertEqual("HOLD", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_PREDECESSOR_ARCHIVE_NOT_COMMITTED", result["acquisition"][EB]["support_ref"])
        self.assertEqual([(TN, DRIVE, "VERIFIED"), (TN, GMAIL, "VERIFIED")], self.steps(result))
        self.assertEqual(2, len(self.ledger.effects))
        self.assertFalse((self.archive / "EB Bill" / "2026-10-01.pdf").exists())


class _PredecessorRunBase(OfflineV4RunTests):
    """Realistic v3 predecessor rows, built through the frozen v3 store API (see build_predecessor_v3)."""

    SCENARIOS = {EB: "ADOPTABLE", TN: "DELIVERED"}

    def create_database(self) -> None:
        self.built = build_predecessor_v3(self.db, self.archive, self.SCENARIOS)

    def portal_latest(self, stream: str, *, failing: bool = False):
        if failing:
            return FailingAdapter()
        name, day = LATEST_SOURCE[stream]
        return self.adapter(stream, (name, day, predecessor_pdf(name)))

    def rebuild(self, label: str, scenarios: dict, prepare=None) -> None:
        base = self.root / label
        base.mkdir()
        self.db = base / "state.sqlite"
        self.archive = base / "archive"
        self.archive.mkdir()
        self.temp = base / "temp"
        self.temp.mkdir()
        self.built = build_predecessor_v3(self.db, self.archive, scenarios)
        if prepare is not None:
            prepare(self.db)
        migrate_state_database_v4(self.db, apply=True)
        self.clock = 0
        self.ledger = FakeProviderLedger()
        self.drive = FakeDriveConnector(self.ledger)
        self.gmail = FakeGmailConnector(self.ledger)

class PredecessorScenarioTests(_PredecessorRunBase):
    """Cutover adoption, delivered no-action, typed predecessor HOLDs and no backfill."""

    def test_an_archived_undelivered_latest_is_adopted_once_with_zero_fetch_then_drive_then_gmail(self) -> None:
        adapters = {EB: self.portal_latest(EB), TN: self.portal_latest(TN)}
        result = self.run_once(1, adapters)
        self.assertEqual("ADOPTED", result["acquisition"][EB]["predecessor"])
        self.assertEqual([], adapters[EB].acquire_calls)
        self.assertEqual([(EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")], self.steps(result))
        self.assertEqual(["SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf"], [e["display_filename"] for e in self.effects(DRIVE)])
        self.assertEqual(hashlib.sha256(predecessor_pdf("Invoice EB Sep.pdf")).hexdigest(), self.effects(DRIVE)[0]["sha256"])
        self.assertEqual(2, len(self.ledger.effects))
        self.assertEqual("PREDECESSOR_DELIVERED", result["acquisition"][TN]["status"])
        self.assertEqual("DELIVERED", result["acquisition"][TN]["predecessor"])

    def test_a_rerun_after_the_cutover_creates_zero_further_effects(self) -> None:
        self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        again = self.run_once(2, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual([], self.steps(again))
        self.assertEqual("ADOPTED_EARLIER", again["acquisition"][EB]["predecessor"])
        self.assertEqual(2, len(self.ledger.effects))

    def test_the_predecessor_tables_are_unchanged_across_the_cutover(self) -> None:
        from energygrid_bill_downloader.state import predecessor_digest

        before = predecessor_digest(self.db)
        self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual(before, predecessor_digest(self.db))
        self.run_once(2, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual(before, predecessor_digest(self.db))

    def test_a_delivered_latest_is_no_action_and_a_newer_invoice_is_admitted(self) -> None:
        self.rebuild("delivered", {EB: "DELIVERED", TN: "DELIVERED"})
        newer = ("Invoice TN Oct.pdf", "2026-10-05", predecessor_pdf("Invoice TN Oct.pdf"))
        result = self.run_once(1, {EB: self.portal_latest(EB), TN: self.adapter(TN, newer)})
        self.assertEqual("PREDECESSOR_DELIVERED", result["acquisition"][EB]["status"])
        self.assertEqual([(TN, DRIVE, "VERIFIED"), (TN, GMAIL, "VERIFIED")], self.steps(result))
        self.assertEqual(["OCT 2026 - EnergyGrid - Tenant Bill - Invoice TN Oct.pdf"], [e["display_filename"] for e in self.effects(DRIVE)])

    def test_drive_partial_prior_dispatch_holds_only_that_stream_with_zero_fake_writes(self) -> None:
        self.rebuild("partial", {EB: "DRIVE_PARTIAL", TN: "ADOPTABLE"})
        adapters = {EB: self.portal_latest(EB), TN: self.portal_latest(TN)}
        result = self.run_once(1, adapters)
        self.assertEqual("HOLD", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_PREDECESSOR_DRIVE_PARTIAL", result["acquisition"][EB]["support_ref"])
        self.assertEqual("DRAINED", result["drain"]["streams"][EB]["state"])
        self.assertEqual("HOLD", result["streams"][EB]["outcome"])
        self.assertEqual("EG_V4_PREDECESSOR_DRIVE_PARTIAL", result["streams"][EB]["acquisition_ref"])
        self.assertEqual([], adapters[EB].acquire_calls)
        self.assertEqual([], [e for e in self.ledger.effects if e["display_filename"].endswith("Invoice EB Sep.pdf")])
        self.assertEqual([(TN, DRIVE, "VERIFIED"), (TN, GMAIL, "VERIFIED")], self.steps(result))

    def test_each_unsettled_provider_state_is_a_typed_hold_with_no_fake_write(self) -> None:
        cases = (
            ("DELIVERY_UNCERTAIN", "EG_V4_PREDECESSOR_DELIVERY_UNCERTAIN"),
            ("DELIVERY_REJECTED", "EG_V4_PREDECESSOR_DELIVERY_REJECTED"),
            ("DRIVE_VERIFIED_NOT_SENT", "EG_V4_PREDECESSOR_DRIVE_VERIFIED_NOT_SENT"),
            ("ARCHIVE_NOT_COMMITTED", "EG_V4_PREDECESSOR_ARCHIVE_NOT_COMMITTED"),
        )
        for index, (scenario, support_ref) in enumerate(cases):
            with self.subTest(scenario=scenario):
                self.rebuild(f"hold-{index}", {EB: scenario, TN: "ADOPTABLE"})
                result = self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
                self.assertEqual("HOLD", result["acquisition"][EB]["status"])
                self.assertEqual(support_ref, result["acquisition"][EB]["support_ref"])
                self.assertEqual(0, len([e for e in self.ledger.effects if "EB" in e["display_filename"]]))
                self.assertEqual(2, len(self.ledger.effects))

    def test_an_older_archived_invoice_is_never_backfilled_and_is_refused_if_offered(self) -> None:
        older = ("Invoice EB Aug.pdf", "2026-08-01", predecessor_pdf("Invoice EB Aug.pdf"))
        result = self.run_once(1, {EB: self.adapter(EB, older), TN: self.portal_latest(TN)})
        self.assertEqual("ADOPTED", result["acquisition"][EB]["predecessor"])
        self.assertEqual("EG_V4_PREDECESSOR_IDENTITY_PRESENT", result["acquisition"][EB]["support_ref"])
        self.assertFalse(any("AUG" in e["display_filename"] for e in self.ledger.effects))
        self.assertEqual(2, len(self.ledger.effects))

    def test_a_tampered_predecessor_archive_holds_the_stream_with_zero_effects(self) -> None:
        self.rebuild("tampered", {EB: "ADOPTABLE", TN: "ADOPTABLE"})
        archived = self.archive / "EB Bill" / "2026-09-01.pdf"
        archived.write_bytes(predecessor_pdf("Invoice EB Sep X"))
        result = self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual("HOLD", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_PREDECESSOR_ARCHIVE_IDENTITY_MISMATCH", result["acquisition"][EB]["support_ref"])
        self.assertEqual(2, len(self.ledger.effects))

    def test_a_missing_predecessor_archive_holds_the_stream_with_zero_effects(self) -> None:
        self.rebuild("missing", {EB: "ADOPTABLE", TN: "ADOPTABLE"})
        (self.archive / "EB Bill" / "2026-09-01.pdf").unlink()
        result = self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual("HOLD", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_PREDECESSOR_ARCHIVE_UNREADABLE", result["acquisition"][EB]["support_ref"])
        self.assertEqual(2, len(self.ledger.effects))

    def test_an_adopted_pending_invoice_survives_a_portal_failure_and_a_newer_invoice(self) -> None:
        first = self.run_once(1, {EB: self.portal_latest(EB, failing=True), TN: self.portal_latest(TN)})
        self.assertEqual("PORTAL_ACQUISITION_FAILED", first["acquisition"][EB]["status"])
        self.assertEqual("ADOPTED", first["acquisition"][EB]["predecessor"])
        self.assertEqual(2, len(self.ledger.effects))
        newer = ("Invoice EB Oct.pdf", "2026-10-05", predecessor_pdf("Invoice EB Oct.pdf"))
        second = self.run_once(2, {EB: self.adapter(EB, newer), TN: self.portal_latest(TN)})
        self.assertEqual([(EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")], self.steps(second))
        self.assertEqual(
            ["SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf", "OCT 2026 - EnergyGrid - EB Bill - Invoice EB Oct.pdf"],
            [e["display_filename"] for e in self.effects(DRIVE)],
        )

    def test_a_lost_lease_during_adoption_ends_the_run_instead_of_holding_the_stream(self) -> None:
        from energygrid_bill_downloader.reconcile import admit_latest_offline_v4

        with StateV4Store(self.db) as stale, StateV4Store(self.db) as fresh:
            stale.acquire_lease("stale-session", now_ms=1_000, ttl_ms=10)
            fresh.acquire_lease("fresh-session", now_ms=100_000, ttl_ms=10)
            with self.assertRaises(OfflineV4Error) as caught:
                admit_latest_offline_v4(
                    adapters={EB: self.portal_latest(EB)}, state=stale,
                    archive_root=self.archive, temp_root=self.temp, run_id=run_id(7),
                    now_iso="2026-10-13T00:00:00Z", inventory_ceiling=200,
                )
            self.assertEqual("EG_V4_LEASE_LOST", caught.exception.support_ref)
        with StateV4Store(self.db) as view:
            self.assertEqual(0, view.count("energygrid_v4_invoice"))
        self.assertEqual(0, len(self.ledger.effects))

    def test_two_adopted_streams_drain_with_fair_alternation(self) -> None:
        self.rebuild("both", {EB: "ADOPTABLE", TN: "ADOPTABLE"})
        result = self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual(
            [(EB, DRIVE, "VERIFIED"), (TN, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED"), (TN, GMAIL, "VERIFIED")],
            self.steps(result),
        )
        self.assertEqual(4, len(self.ledger.effects))

    def test_an_adopted_identity_is_never_adopted_twice(self) -> None:
        self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        with StateV4Store(self.db) as store:
            self.assertEqual(1, len([row for row in store.invoices_for_stream(EB)]))
            self.assertEqual(1, len([row for row in store.invoices_for_stream(EB) if row["predecessor_invoice_id"]]))


def eb_sep_effects(ledger: FakeProviderLedger) -> list[tuple[str, str]]:
    return [(e["action"], e["display_filename"]) for e in ledger.effects if e["display_filename"].endswith("Invoice EB Sep.pdf")]


class PredecessorTransitionTests(_PredecessorRunBase):
    """#226 v4 correction: M1-M3 at the run boundary, M10 and M11 adoption liveness and send-time integrity."""

    OCTOBER = ("Invoice EB Oct.pdf", "2026-10-05", predecessor_pdf("Invoice EB Oct.pdf"))

    def assert_eb_held_before_inventory(self, result: dict, adapters: dict, support_ref: str) -> None:
        self.assertEqual({"status": "HOLD", "support_ref": support_ref, "predecessor": "HOLD"}, result["acquisition"][EB])
        self.assertEqual(0, adapters[EB].inventory_calls)
        self.assertEqual([], adapters[EB].acquire_calls)
        with StateV4Store(self.db) as store:
            self.assertEqual([], store.invoices_for_stream(EB))
        self.assertEqual([], eb_sep_effects(self.ledger))
        self.assertEqual("HOLD", result["streams"][EB]["outcome"])

    def test_m1_v2_drive_stage_or_drive_state_evidence_holds_with_no_inventory_rows_or_effects(self) -> None:
        from energygrid_bill_downloader.state import predecessor_digest
        from test_state_v4 import add_v2_drive_stage, set_v2_drive_state

        cases = (
            ("stage", lambda db: add_v2_drive_stage(db, self._latest_id(db), "COMMITTED")),
            ("state", lambda db: set_v2_drive_state(db, self._latest_id(db), "PREPARED")),
        )
        for label, prepare in cases:
            with self.subTest(case=label):
                self.rebuild(f"m1-{label}", {EB: "ADOPTABLE", TN: "ADOPTABLE"}, prepare=prepare)
                before = predecessor_digest(self.db)
                adapters = {EB: self.portal_latest(EB), TN: self.portal_latest(TN)}
                result = self.run_once(1, adapters)
                self.assert_eb_held_before_inventory(result, adapters, "EG_V4_PREDECESSOR_DRIVE_EVIDENCE_PRESENT")
                self.assertEqual([(TN, DRIVE, "VERIFIED"), (TN, GMAIL, "VERIFIED")], self.steps(result))
                self.assertEqual(before, predecessor_digest(self.db))

    def _latest_id(self, db: Path) -> str:
        from test_state_v4 import _predecessor_rows

        return _predecessor_rows(db, "SELECT invoice_id FROM energygrid_invoice_v2 WHERE stream='EB_BILL' "
                                     "AND classification='CLASSIFIED' ORDER BY day_ordinal DESC LIMIT 1")[0]["invoice_id"]

    def test_m1_positive_a_clean_adoptable_latest_gets_drive_and_gmail_once(self) -> None:
        self.rebuild("m1-clean", {EB: "ADOPTABLE", TN: "DELIVERED"})
        result = self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual([(DRIVE, "SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf"), (GMAIL, "SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf")],
                         eb_sep_effects(self.ledger))
        self.assertEqual("DRAINED", result["overall"])

    def test_m2_a_latest_day_tie_holds_with_no_inventory_rows_or_effects(self) -> None:
        from energygrid_bill_downloader.state import predecessor_digest
        from test_state_v4 import insert_predecessor_on_day

        insert_predecessor_on_day(self.db, source_filename="Invoice EB Sep B.pdf", bill_date="2026-08-31",
                                  day_ordinal=date(2026, 9, 1).toordinal())
        before = predecessor_digest(self.db)
        adapters = {EB: self.portal_latest(EB), TN: self.portal_latest(TN)}
        result = self.run_once(1, adapters)
        self.assert_eb_held_before_inventory(result, adapters, "EG_V4_PREDECESSOR_LATEST_AMBIGUOUS")
        self.assertEqual(before, predecessor_digest(self.db))

    def test_m3_a_dispatched_pending_send_holds_with_its_exact_reference(self) -> None:
        self.rebuild("m3", {EB: "DELIVERY_DISPATCHED", TN: "DELIVERED"})
        adapters = {EB: self.portal_latest(EB), TN: self.portal_latest(TN)}
        result = self.run_once(1, adapters)
        self.assert_eb_held_before_inventory(result, adapters, "EG_V4_PREDECESSOR_DELIVERY_DISPATCHED")
        self.assertEqual([], self.ledger.effects)

    # -- M10: an adopted and delivered predecessor is never re-read --------------------------------

    def _deliver_september_then_change_archive(self, change) -> None:
        first = self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        self.assertEqual("ADOPTED", first["acquisition"][EB]["predecessor"])
        self.assertEqual(2, len(eb_sep_effects(self.ledger)))
        change(self.archive / "EB Bill" / "2026-09-01.pdf")

    def test_m10_a_changed_or_missing_delivered_archive_does_not_block_october(self) -> None:
        def changed(path: Path) -> None:
            path.write_bytes(predecessor_pdf("Invoice EB Sep resaved"))

        def deleted(path: Path) -> None:
            path.unlink()

        def unreadable(path: Path) -> None:
            path.unlink()
            path.mkdir()

        for label, change in (("changed", changed), ("deleted", deleted), ("unreadable", unreadable)):
            with self.subTest(change=label):
                self.rebuild(f"m10-{label}", {EB: "ADOPTABLE", TN: "DELIVERED"})
                self._deliver_september_then_change_archive(change)
                result = self.run_once(2, {EB: self.adapter(EB, self.OCTOBER), TN: self.portal_latest(TN)})
                self.assertEqual({"status": "ADMITTED", "support_ref": None, "predecessor": "ADOPTED_EARLIER"},
                                 result["acquisition"][EB])
                self.assertEqual([(EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")], self.steps(result))
                self.assertEqual(2, len(eb_sep_effects(self.ledger)))
                self.assertEqual(["OCT 2026 - EnergyGrid - EB Bill - Invoice EB Oct.pdf"] * 2,
                                 [e["display_filename"] for e in self.ledger.effects if "Oct" in e["display_filename"]])
                self.assertEqual(4, len(self.ledger.effects))
                self.assertEqual("DRAINED", result["overall"])
                again = self.run_once(3, {EB: self.adapter(EB, self.OCTOBER), TN: self.portal_latest(TN)})
                self.assertEqual([], self.steps(again))
                self.assertEqual(4, len(self.ledger.effects))

    def test_m10_positive_a_first_time_adoption_of_a_changed_archive_still_holds(self) -> None:
        self.rebuild("m10-first", {EB: "ADOPTABLE", TN: "DELIVERED"})
        (self.archive / "EB Bill" / "2026-09-01.pdf").write_bytes(predecessor_pdf("Invoice EB Sep resaved"))
        adapters = {EB: self.adapter(EB, self.OCTOBER), TN: self.portal_latest(TN)}
        result = self.run_once(1, adapters)
        self.assertEqual("EG_V4_PREDECESSOR_ARCHIVE_IDENTITY_MISMATCH", result["acquisition"][EB]["support_ref"])
        self.assertEqual(0, adapters[EB].inventory_calls)
        self.assertEqual([], self.ledger.effects)

    # -- M11: a pending adopted delivery is checked at send time and never overtaken ----------------

    def _september_pending(self, checkpoint) -> None:
        self.rebuild(f"m11-{len(list(self.root.iterdir()))}", {EB: "ADOPTABLE", TN: "DELIVERED"})
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)}, checkpoint=checkpoint)

    def _eb_intents(self) -> dict[str, dict]:
        with StateV4Store(self.db) as store:
            return {
                row["invoice_date"]: {
                    action: {**intent, "sealed": store.dispatch_row(intent["intent_id"]) is not None}
                    for action, intent in store.intents_for_invoice(row["invoice_key"]).items()
                }
                for row in store.invoices_for_stream(EB)
            }

    def test_m11_a_tampered_pending_adoption_holds_before_seal_and_october_waits(self) -> None:
        cases = (
            ("pre-drive", crash_at("after_acquisition"), DRIVE, 0),
            ("gmail-pending", crash_at("before_seal", nth=2), GMAIL, 1),
        )
        for label, checkpoint, held_action, sep_effects in cases:
            with self.subTest(case=label):
                self._september_pending(checkpoint)
                self.assertEqual(sep_effects, len(eb_sep_effects(self.ledger)))
                (self.archive / "EB Bill" / "2026-09-01.pdf").write_bytes(predecessor_pdf("Invoice EB Sep X"))
                result = self.run_once(2, {EB: self.adapter(EB, self.OCTOBER), TN: self.portal_latest(TN)})
                self.assertEqual({"status": "ADMITTED", "support_ref": None, "predecessor": "ADOPTED_EARLIER"},
                                 result["acquisition"][EB])
                self.assertEqual([(EB, held_action, "HOLD")], self.steps(result))
                intents = self._eb_intents()
                self.assertEqual(("HOLD", "EG_V4_ARCHIVE_IDENTITY_CHANGED", False),
                                 tuple(intents["2026-09-01"][held_action][k] for k in ("state", "support_ref", "sealed")))
                self.assertEqual({"PENDING"}, {row["state"] for row in intents["2026-10-05"].values()})
                self.assertEqual({False}, {row["sealed"] for row in intents["2026-10-05"].values()})
                self.assertEqual(sep_effects, len(self.ledger.effects))
                self.assertEqual(("BLOCKED", "EG_V4_ARCHIVE_IDENTITY_CHANGED", "ADMITTED"),
                                 tuple(result["streams"][EB][k] for k in ("outcome", "drain_ref", "acquisition_status")))
                again = self.run_once(3, {EB: self.adapter(EB, self.OCTOBER), TN: self.portal_latest(TN)})
                self.assertEqual([], self.steps(again))
                self.assertEqual(sep_effects, len(self.ledger.effects))
                self.assertEqual("BLOCKED", again["streams"][EB]["outcome"])

    def test_m11_positive_intact_pending_september_and_october_drain_in_fifo_order(self) -> None:
        cases = (
            ("pre-drive", crash_at("after_acquisition"),
             [(EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED"), (EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")]),
            ("gmail-pending", crash_at("before_seal", nth=2),
             [(EB, GMAIL, "VERIFIED"), (EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")]),
        )
        for label, checkpoint, expected_steps in cases:
            with self.subTest(case=label):
                self._september_pending(checkpoint)
                result = self.run_once(2, {EB: self.adapter(EB, self.OCTOBER), TN: self.portal_latest(TN)})
                self.assertEqual(expected_steps, self.steps(result))
                names = [(e["action"], e["display_filename"]) for e in self.ledger.effects]
                self.assertEqual([
                    (DRIVE, "SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf"), (GMAIL, "SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf"),
                    (DRIVE, "OCT 2026 - EnergyGrid - EB Bill - Invoice EB Oct.pdf"), (GMAIL, "OCT 2026 - EnergyGrid - EB Bill - Invoice EB Oct.pdf"),
                ], names)
                self.assertEqual("DRAINED", result["overall"])
                again = self.run_once(3, {EB: self.adapter(EB, self.OCTOBER), TN: self.portal_latest(TN)})
                self.assertEqual([], self.steps(again))
                self.assertEqual(4, len(self.ledger.effects))

    def test_pending_adopted_september_drains_while_the_newer_portal_acquisition_fails(self) -> None:
        self._september_pending(crash_at("after_acquisition"))
        result = self.run_once(2, {EB: self.portal_latest(EB, failing=True), TN: self.portal_latest(TN)})
        self.assertEqual("PORTAL_ACQUISITION_FAILED", result["acquisition"][EB]["status"])
        self.assertEqual("ADOPTED_EARLIER", result["acquisition"][EB]["predecessor"])
        self.assertEqual([(EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")], self.steps(result))
        self.assertEqual("DRAINED", result["drain"]["streams"][EB]["state"])
        self.assertEqual("ACQUISITION_FAILED", result["streams"][EB]["outcome"])
        self.assertEqual("ACQUISITION_FAILED", result["overall"])

    def test_m13_an_older_candidate_with_a_bad_name_is_still_a_source_regression(self) -> None:
        for name in ("Rechnung \u00f6.pdf", "OCT2026-x.pdf", "a\x7fb.pdf", "Invoice EB Aug 15.pdf"):
            with self.subTest(name=name):
                self.rebuild(f"m13-{len(list(self.root.iterdir()))}", {EB: "ADOPTABLE", TN: "DELIVERED"})
                adapters = {EB: self.adapter(EB, (name, "2026-08-15", predecessor_pdf("older"))), TN: self.portal_latest(TN)}
                result = self.run_once(1, adapters)
                self.assertEqual({"status": "ADMISSION_REFUSED", "support_ref": "EG_V4_SOURCE_REGRESSION",
                                  "predecessor": "ADOPTED"}, result["acquisition"][EB])
                self.assertEqual([], adapters[EB].acquire_calls)
                self.assertEqual("REFUSED", result["streams"][EB]["outcome"])


class DisplayHoldTests(OfflineV4RunTests):
    """M12-M14 at the run boundary: a current candidate's display fault is a typed, non-sticky stream HOLD."""

    def run_with_eb(self, n: int, name: str, day: str = "2026-10-01", **kwargs):
        eb = self.adapter(EB, (name, day, pdf(name.encode("unicode_escape").decode("ascii"))))
        tn = self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))
        return eb, self.run_once(n, {EB: eb, TN: tn}, **kwargs)

    def test_m12_m14_current_display_faults_hold_with_zero_fetch_archive_admission_and_effects(self) -> None:
        cases = (
            ("Rechnung \u00f6.pdf", "EG_V4_DISPLAY_SHAPE_UNSUPPORTED"),
            ("a\x7fb.pdf", "EG_V4_DISPLAY_NAME_INVALID"),
            (" lead.pdf", "EG_V4_DISPLAY_NAME_INVALID"),
            ("OCT2026-invoice.pdf", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
            ("Invoice OCT 2026 - EB.pdf", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
            ("x OCT 2026 - y.pdf", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
            ("OCT 2026 invoice.pdf", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
            ("OCTOBER 2026 - x.pdf", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
            ("SEPT 2026 - x.pdf", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
            ("OCT 2026 - OCT2026 x.pdf", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
        )
        for name, support_ref in cases:
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                eb, result = self.run_with_eb(1, name)
                self.assertEqual({"status": "HOLD", "support_ref": support_ref, "predecessor": "NONE"},
                                 result["acquisition"][EB])
                self.assertEqual(1, eb.inventory_calls)
                self.assertEqual([], eb.acquire_calls)
                self.assertFalse((self.archive / "EB Bill" / "2026-10-01.pdf").exists())
                with StateV4Store(self.db) as store:
                    self.assertEqual([], store.invoices_for_stream(EB))
                self.assertEqual([(TN, DRIVE, "VERIFIED"), (TN, GMAIL, "VERIFIED")], self.steps(result))
                self.assertEqual(("HOLD", support_ref, "DRAINED"),
                                 tuple(result["streams"][EB][k] for k in ("outcome", "acquisition_ref", "drain_state")))
                self.assertEqual("HOLD", result["overall"])

    def test_m12_positive_a_plain_name_gets_one_prefix_and_a_hold_is_not_sticky(self) -> None:
        _eb, held = self.run_with_eb(1, "OCT2026-invoice.pdf")
        self.assertEqual("HOLD", held["acquisition"][EB]["status"])
        eb, result = self.run_with_eb(2, "invoice.pdf", day="2026-10-05")
        self.assertEqual("ADMITTED", result["acquisition"][EB]["status"])
        self.assertEqual(["invoice.pdf"], eb.acquire_calls)
        self.assertEqual(["OCT 2026 - EnergyGrid - EB Bill - invoice.pdf"] * 2,
                         [e["display_filename"] for e in self.ledger.effects if "invoice.pdf" in e["display_filename"]])
        self.assertEqual("DRAINED", result["overall"])

    def test_m13_older_candidates_with_bad_names_are_regressions_not_display_holds(self) -> None:
        self.run_with_eb(1, "Invoice EB 5.pdf", day="2026-10-05")
        for n, name in enumerate(("Rechnung \u00f6.pdf", "OCT2026-x.pdf", " lead.pdf", "Invoice EB 1.pdf"), start=2):
            with self.subTest(name=name):
                eb, result = self.run_with_eb(n, name, day="2026-10-01")
                self.assertEqual({"status": "ADMISSION_REFUSED", "support_ref": "EG_V4_SOURCE_REGRESSION",
                                  "predecessor": "NONE"}, result["acquisition"][EB])
                self.assertEqual([], eb.acquire_calls)
                self.assertEqual("REFUSED", result["streams"][EB]["outcome"])
        self.assertEqual(4, len(self.ledger.effects))


def lost_first_drive(ledger: FakeProviderLedger) -> FakeDriveConnector:
    """A Drive fake whose first call makes the effect and then loses the response (UNKNOWN)."""
    return FakeDriveConnector(ledger, fail_after_effect=True)


class TruthfulReportingTests(OfflineV4RunTests):
    """M15: drain truth and acquisition truth are both visible; DRAINED is never claimed falsely."""

    def test_m15_an_unknown_drive_stays_blocked_when_a_new_candidate_also_holds(self) -> None:
        self.run_once(1, self.standard_adapters(), drive=lost_first_drive(self.ledger))
        eb = self.adapter(EB, ("OCT2026-x.pdf", "2026-10-05", pdf("eb-5")))
        result = self.run_once(2, {EB: eb, TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))})
        self.assertEqual({"state": "BLOCKED", "support_ref": "EG_V4_PROVIDER_UNKNOWN"}, result["drain"]["streams"][EB])
        self.assertEqual({
            "outcome": "BLOCKED", "acquisition_status": "HOLD", "acquisition_ref": "EG_V4_DISPLAY_PREFIX_CONFLICT",
            "drain_state": "BLOCKED", "drain_ref": "EG_V4_PROVIDER_UNKNOWN",
        }, result["streams"][EB])
        self.assertEqual("DRAINED", result["streams"][TN]["outcome"])
        self.assertEqual("BLOCKED", result["overall"])
        self.assertEqual("EG_V4_PROVIDER_UNKNOWN", result["drain"]["blocked"][EB])

    def test_m15_acquisition_failures_are_never_reported_drained(self) -> None:
        class FetchFails(OfflineFakeAdapter):
            pass

        def fetch_failed():
            source = Stream(EB)
            return FetchFails(snapshot(source, (candidate(source, name="Invoice EB.pdf", invoice_date="2026-10-01"),)), {})

        def archive_conflict():
            folder = self.archive / "EB Bill"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "2026-10-01.pdf").write_bytes(pdf("someone else"))
            return self.adapter(EB, ("Invoice EB.pdf", "2026-10-01", pdf("eb-1")))

        cases = (
            ("FETCH_FAILED", fetch_failed, "EG_SYNTHETIC_ARTIFACT_MISSING"),
            ("PORTAL_ACQUISITION_FAILED", FailingAdapter, "EG_SYNTHETIC_PORTAL_DOWN"),
            ("ARCHIVE_CONFLICT", archive_conflict, "EG_V4_ARCHIVE_CONFLICT"),
        )
        for status, make, support_ref in cases:
            with self.subTest(status=status):
                self.tearDown()
                self.setUp()
                result = self.run_once(1, {EB: make(), TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))})
                self.assertEqual((status, support_ref), (result["acquisition"][EB]["status"], result["acquisition"][EB]["support_ref"]))
                self.assertEqual("DRAINED", result["drain"]["streams"][EB]["state"])
                self.assertEqual("ACQUISITION_FAILED", result["streams"][EB]["outcome"])
                self.assertEqual("DRAINED", result["streams"][TN]["outcome"])
                self.assertEqual("ACQUISITION_FAILED", result["overall"])

    def test_m15_positive_fully_handled_streams_report_drained(self) -> None:
        result = self.run_once(1, self.standard_adapters())
        self.assertEqual({EB: "DRAINED", TN: "DRAINED"}, {k: v["outcome"] for k, v in result["streams"].items()})
        self.assertEqual("DRAINED", result["overall"])
        again = self.run_once(2, self.standard_adapters())
        self.assertEqual("DRAINED", again["overall"])
        empty = self.run_once(3, {EB: self.adapter(EB), TN: self.adapter(TN)})
        self.assertEqual("DRAINED", empty["overall"])

    def test_m15_outcome_precedence_table(self) -> None:
        from energygrid_bill_downloader.orchestration import _v4_overall, _v4_stream_outcome

        def acq(status, ref=None):
            return {"status": status, "support_ref": ref}

        def drain(state, ref=None):
            return {"state": state, "support_ref": ref}

        table = (
            (acq("HOLD", "EG_V4_DISPLAY_NAME_INVALID"), drain("BLOCKED", "EG_V4_PROVIDER_UNKNOWN"), "BLOCKED"),
            (acq("FETCH_FAILED", "EG_X"), drain("BLOCKED", "EG_V4_PROVIDER_UNKNOWN"), "BLOCKED"),
            (acq("HOLD", "EG_V4_PREDECESSOR_DRIVE_PARTIAL"), drain("PENDING"), "HOLD"),
            (acq("HOLD", "EG_V4_DISPLAY_NAME_INVALID"), drain("DRAINED"), "HOLD"),
            (acq("FETCH_FAILED", "EG_X"), drain("PENDING"), "PENDING"),
            (acq("ADMISSION_REFUSED", "EG_V4_SOURCE_REGRESSION"), drain("PENDING"), "PENDING"),
            (acq("ADMISSION_REFUSED", "EG_V4_SOURCE_REGRESSION"), drain("DRAINED"), "REFUSED"),
            (acq("LATEST_AMBIGUOUS", "EG_V4_LATEST_AMBIGUOUS"), drain("DRAINED"), "ACQUISITION_FAILED"),
            (acq("INVENTORY_INVALID", "EG_V4_INVENTORY_INVALID"), drain("DRAINED"), "ACQUISITION_FAILED"),
            (acq("ADAPTER_UNAVAILABLE", "EG_V4_ADAPTER_UNAVAILABLE"), drain("DRAINED"), "ACQUISITION_FAILED"),
            (acq("ADMITTED"), drain("DRAINED"), "DRAINED"),
            (acq("ALREADY_ADMITTED"), drain("DRAINED"), "DRAINED"),
            (acq("PREDECESSOR_DELIVERED"), drain("DRAINED"), "DRAINED"),
            (acq("EMPTY"), drain("DRAINED"), "DRAINED"),
        )
        for acquisition, drained, expected in table:
            with self.subTest(acquisition=acquisition, drain=drained):
                row = _v4_stream_outcome(acquisition, drained)
                self.assertEqual(expected, row["outcome"])
                self.assertEqual((acquisition["support_ref"], drained["support_ref"]), (row["acquisition_ref"], row["drain_ref"]))
        drained = {"outcome": "DRAINED"}
        self.assertEqual("DRAINED", _v4_overall({EB: drained, TN: drained}))
        for other in ("BLOCKED", "HOLD", "PENDING", "ACQUISITION_FAILED", "REFUSED"):
            self.assertEqual(other, _v4_overall({EB: drained, TN: {"outcome": other}}))
        self.assertEqual("BLOCKED", _v4_overall({EB: {"outcome": "HOLD"}, TN: {"outcome": "BLOCKED"}}))


class DualStreamIsolationTests(_PredecessorRunBase):
    """Both streams together: a HOLD or BLOCKED stream never suspends the other, FIFO, no backfill, no duplicates."""

    OCT = {EB: ("Invoice EB Oct.pdf", "2026-10-05"), TN: ("Invoice TN Oct.pdf", "2026-10-06")}

    def october(self, stream: str):
        name, day = self.OCT[stream]
        return self.adapter(stream, (name, day, predecessor_pdf(name)))

    def _held_stream_does_not_stop_the_adopting_stream(self, held: str, scenario: str, support_ref: str) -> None:
        other = TN if held == EB else EB
        self.rebuild(f"iso-{held}", {held: scenario, other: "ADOPTABLE"})
        for n, other_adapter in ((1, self.portal_latest(other)), (2, self.october(other))):
            held_adapter = self.portal_latest(held)
            result = self.run_once(n, {held: held_adapter, other: other_adapter})
            self.assertEqual(("HOLD", support_ref), (result["acquisition"][held]["status"], result["acquisition"][held]["support_ref"]))
            self.assertEqual(0, held_adapter.inventory_calls)
            self.assertEqual([(other, DRIVE, "VERIFIED"), (other, GMAIL, "VERIFIED")], self.steps(result))
            self.assertEqual("DRAINED", result["streams"][other]["outcome"])
            self.assertEqual("HOLD", result["streams"][held]["outcome"])
        shown = [e["display_filename"] for e in self.ledger.effects]
        prefix = "EB Bill - Invoice EB" if other == EB else "Tenant Bill - Invoice TN"
        self.assertEqual([f"SEP 2026 - EnergyGrid - {prefix} Sep.pdf"] * 2 + [f"OCT 2026 - EnergyGrid - {prefix} Oct.pdf"] * 2, shown)
        self.assertFalse(any(("Invoice EB" if held == EB else "Invoice TN") in name for name in shown))

    def test_eb_predecessor_hold_while_tenant_adopts_september_and_admits_october(self) -> None:
        self._held_stream_does_not_stop_the_adopting_stream(EB, "DRIVE_PARTIAL", "EG_V4_PREDECESSOR_DRIVE_PARTIAL")

    def test_tenant_predecessor_hold_while_eb_adopts_september_and_admits_october(self) -> None:
        self._held_stream_does_not_stop_the_adopting_stream(TN, "DELIVERY_UNCERTAIN", "EG_V4_PREDECESSOR_DELIVERY_UNCERTAIN")

    def test_eb_display_hold_with_older_eb_pending_while_tenant_is_unknown(self) -> None:
        self.rebuild("iso-unknown", {EB: "DELIVERED", TN: "ADOPTABLE"})
        # Run 1: Tenant's adopted September Drive upload loses its response: UNKNOWN, never resent.
        first = self.run_once(1, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)},
                              drive=lost_first_drive(self.ledger))
        self.assertEqual("BLOCKED", first["streams"][TN]["outcome"])
        # Run 2: EB admits October, then the run dies before its seal: EB October is pending.
        with self.assertRaises(SimulatedCrash):
            self.run_once(2, {EB: self.october(EB), TN: self.portal_latest(TN)}, checkpoint=crash_at("before_seal"))
        # Run 3: EB's next candidate has an ambiguous name. EB's older pending work still drains.
        bad = self.adapter(EB, ("OCT2026-x.pdf", "2026-10-20", predecessor_pdf("bad")))
        result = self.run_once(3, {EB: bad, TN: self.portal_latest(TN)})
        self.assertEqual(("HOLD", "EG_V4_DISPLAY_PREFIX_CONFLICT"),
                         (result["acquisition"][EB]["status"], result["acquisition"][EB]["support_ref"]))
        self.assertEqual([(EB, DRIVE, "VERIFIED"), (EB, GMAIL, "VERIFIED")], self.steps(result))
        self.assertEqual([], bad.acquire_calls)
        self.assertEqual(("HOLD", "DRAINED"), (result["streams"][EB]["outcome"], result["streams"][EB]["drain_state"]))
        self.assertEqual(("BLOCKED", "EG_V4_PROVIDER_UNKNOWN"),
                         (result["streams"][TN]["outcome"], result["streams"][TN]["drain_ref"]))
        self.assertEqual("BLOCKED", result["overall"])
        self.assertEqual(1, len([e for e in self.ledger.effects if "Invoice TN Sep" in e["display_filename"]]))
        self.assertEqual(2, len([e for e in self.ledger.effects if "Invoice EB Oct" in e["display_filename"]]))

    def test_older_history_is_never_backfilled_across_both_streams_and_reruns(self) -> None:
        self.rebuild("iso-history", {EB: "ADOPTABLE", TN: "ADOPTABLE"})
        for n in (1, 2, 3):
            self.run_once(n, {EB: self.portal_latest(EB), TN: self.portal_latest(TN)})
        shown = sorted(e["display_filename"] for e in self.ledger.effects)
        self.assertEqual(sorted(["SEP 2026 - EnergyGrid - EB Bill - Invoice EB Sep.pdf"] * 2 + ["SEP 2026 - EnergyGrid - Tenant Bill - Invoice TN Sep.pdf"] * 2), shown)
        self.assertFalse(any("Aug" in name for name in shown))


class PredecessorIsolationTests(OfflineV4RunTests):
    def test_the_run_never_rewrites_a_predecessor_row_or_schema_object(self) -> None:
        from energygrid_bill_downloader.state import predecessor_digest

        before = predecessor_digest(self.db)
        self.run_once(1, self.standard_adapters())
        self.assertEqual(before, predecessor_digest(self.db))


if __name__ == "__main__":
    unittest.main()
