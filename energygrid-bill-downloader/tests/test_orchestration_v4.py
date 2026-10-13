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
from test_state_v4 import create_predecessor, insert_predecessor


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
        self.create_database()
        migrate_state_database_v4(self.db, apply=True)
        self.archive = self.root / "archive"
        self.archive.mkdir()
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
            self.assertEqual("OCT 2026 - Invoice EB.pdf", invoice["display_filename"])
            self.assertEqual("EB Bill/2026-10-01.pdf", invoice["archive_relpath"])
        drive_copy = next(entry for entry in self.effects(DRIVE) if entry["display_filename"] == "OCT 2026 - Invoice EB.pdf")
        attachment = next(entry for entry in self.effects(GMAIL) if entry["display_filename"] == "OCT 2026 - Invoice EB.pdf")
        self.assertEqual(sha(data), drive_copy["sha256"])
        self.assertEqual(sha(data), attachment["sha256"])
        self.assertEqual(display_filename("Invoice EB.pdf", date(2026, 10, 1)), drive_copy["display_filename"])

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
            ["OCT 2026 - Invoice EB.pdf", "OCT 2026 - Invoice TN.pdf", "OCT 2026 - Invoice EB 5.pdf", "OCT 2026 - Invoice TN 6.pdf"],
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
        self.assertEqual(0, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))
        self.assertEqual("UNKNOWN", self.intent_state("2026-10-01", DRIVE))
        # Only the second session's Tenant Bill upload and email; the stale session made no call.
        self.assertEqual(2, len(self.ledger.effects))

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
        self.assertEqual(0, len([e for e in self.ledger.effects if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))
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
        self.assertEqual(1, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))
        self.assertEqual("BLOCKED", result["drain"]["streams"][EB]["state"])

    def test_crash_after_a_verified_receipt_resumes_at_gmail_without_repeating_drive(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("after_receipt"))
        self.assertEqual("VERIFIED", self.intent_state("2026-10-01", DRIVE))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual(EB, self.steps(result)[0][0])
        self.assertEqual(GMAIL, self.steps(result)[0][1])
        self.assertEqual(1, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))
        self.assertEqual(4, len(self.ledger.effects))

    def test_a_lost_provider_response_after_the_effect_holds_only_that_stream(self) -> None:
        lost_drive = FakeDriveConnector(self.ledger, fail_after_effect=True)
        result = self.run_once(1, self.standard_adapters(), drive=lost_drive)
        self.assertEqual("UNKNOWN", self.intent_state("2026-10-01", DRIVE))
        self.assertEqual("BLOCKED", result["drain"]["streams"][EB]["state"])
        self.assertEqual("EG_V4_PROVIDER_UNKNOWN", result["drain"]["streams"][EB]["support_ref"])
        self.assertEqual("DRAINED", result["drain"]["streams"][TN]["state"])
        self.assertEqual(1, len([e for e in self.effects(DRIVE) if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))
        self.assertEqual(0, len([e for e in self.effects(GMAIL) if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))

    def test_an_altered_archive_holds_the_pending_intent_and_sends_nothing_for_it(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        (self.archive / "EB Bill" / "2026-10-01.pdf").write_bytes(pdf("eb-2"))
        result = self.run_once(2, self.standard_adapters())
        self.assertEqual("HOLD", self.intent_state("2026-10-01", DRIVE))
        self.assertEqual("EG_V4_ARCHIVE_IDENTITY_CHANGED", result["drain"]["streams"][EB]["support_ref"])
        self.assertEqual(2, len(self.ledger.effects))
        self.assertEqual(0, len([e for e in self.ledger.effects if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))


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

    def test_a_display_name_collision_is_refused_before_any_fetch_and_the_drain_continues(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_once(1, self.standard_adapters(), checkpoint=crash_at("before_seal"))
        eb_next = self.adapter(EB, ("OCT 2026 - Invoice EB.pdf", "2026-10-20", pdf("eb-20")))
        adapters = {EB: eb_next, TN: self.adapter(TN, ("Invoice TN.pdf", "2026-10-02", pdf("tn-1")))}
        result = self.run_once(2, adapters)
        self.assertEqual("ADMISSION_REFUSED", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_DISPLAY_NAME_COLLISION", result["acquisition"][EB]["support_ref"])
        self.assertEqual([], eb_next.acquire_calls)
        self.assertEqual(4, len(self.ledger.effects))
        self.assertEqual("DRAINED", result["drain"]["streams"][EB]["state"])

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
        self.assertEqual(0, len([e for e in self.ledger.effects if e["display_filename"] == "OCT 2026 - Invoice EB.pdf"]))


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
        self.assertFalse(any(e["display_filename"] == "OCT 2026 - Invoice EB.pdf" for e in self.ledger.effects))

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

    def test_a_latest_invoice_that_v3_already_holds_is_never_redelivered(self) -> None:
        result = self.run_once(1, self.standard_adapters())
        self.assertEqual("ADMISSION_REFUSED", result["acquisition"][EB]["status"])
        self.assertEqual("EG_V4_PREDECESSOR_IDENTITY_PRESENT", result["acquisition"][EB]["support_ref"])
        self.assertEqual([(TN, DRIVE, "VERIFIED"), (TN, GMAIL, "VERIFIED")], self.steps(result))
        self.assertEqual(2, len(self.ledger.effects))
        self.assertFalse((self.archive / "EB Bill" / "2026-10-01.pdf").exists())


class PredecessorIsolationTests(OfflineV4RunTests):
    def test_the_run_never_rewrites_a_predecessor_row_or_schema_object(self) -> None:
        from energygrid_bill_downloader.state import predecessor_digest

        before = predecessor_digest(self.db)
        self.run_once(1, self.standard_adapters())
        self.assertEqual(before, predecessor_digest(self.db))


if __name__ == "__main__":
    unittest.main()
