"""#226 production hardening (Web #226:6186622535): P-2, P-3, P-4, P-5, P-7 and H-1..H-6.

Synthetic data and fake providers only: a FakeNativeHost plays Drive and Gmail, the LOCAL_SYNC
folder is a temporary directory, and no network, real bill, credential or host setting is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_native_bridge as base
import test_tenant_source as tenant_tests
from energygrid_bill_downloader import native_bridge as bridge
from energygrid_bill_downloader import native_notify as notify
from energygrid_bill_downloader import state as state_module
from energygrid_bill_downloader import tenant_source as tenant
from energygrid_bill_downloader.errors import ConfigError, OfflineV4Error, SourceContractError
from energygrid_bill_downloader.native_bridge import NativeRefusal
from energygrid_bill_downloader.state import StateV4Store

EB, TN, FLAT = base.EB, base.TN, base.FLAT
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
NATIVE_HOST = PACKAGE_ROOT / "runtime" / "native_host"
DAY_MS = 86_400_000
# 2026-10-15T00:00:00Z
OCT15 = 1_792_022_400_000


def eb_only() -> dict:
    return {EB: [("EB Invoice 0001.pdf", "2026-08-01"), ("EB Invoice 0002.pdf", "2026-09-01")]}


class SyncBase(base.NativeBridgeBase):
    """LOCAL_SYNC into one flat folder, with helpers that drive one step at a time."""

    folders = {EB: FLAT, TN: FLAT}

    def make_config(self, **changes) -> bridge.NativeConfig:
        sync = self.root / "sync"
        sync.mkdir(exist_ok=True)
        drive = bridge.NativeDriveSettings(account="uat@example.com", folder_ids=dict(self.folders),
                                           mode=bridge.DRIVE_LOCAL_SYNC, sync_folders={EB: sync, TN: sync})
        return super().make_config(drive=drive, **changes)

    @property
    def sync_dir(self) -> Path:
        return self.root / "sync"

    def sync(self) -> None:
        known = {row["title"] for row in self.host.drive.values() if row["parent"] == FLAT}
        for path in self.sync_dir.iterdir():
            if path.name not in known and not path.name.startswith(bridge.STAGE_PREFIX):
                self.host.put(FLAT, path.name, path.read_bytes())

    def precheck(self, step: dict) -> dict:
        return {"folder_id": step["folder_id"], "title": step["display_filename"], "complete": True,
                "query": step["precheck_query"], "matches": self.host.search(FLAT, step["display_filename"])}

    def seal(self, step: dict) -> dict:
        return self.call(bridge.drive_seal, step["intent_id"], self.precheck(step))

    def record_found(self, intent_id: str, title: str) -> dict:
        found = self.host.search(FLAT, title)
        return self.call(bridge.drive_record, intent_id, {"outcome": "CREATED", "file": self.host.metadata(found[0]["id"])})

    def status_now(self) -> dict:
        with StateV4Store(self.cfg.state_path) as state:
            return bridge.status(state, self.cfg, self.clock)

    def next_until(self, wanted: tuple[str, ...], limit: int = 12) -> dict:
        """Run `next`, completing Drive steps and notifications on the way, until an outcome in `wanted`."""
        for _ in range(limit):
            step = self.next()
            if step["outcome"] in wanted:
                return step
            if step["outcome"] == "DRIVE_PRECHECK":
                sealed = self.seal(step)
                if sealed["outcome"] == "SEALED":
                    self.sync()
                    self.record_found(step["intent_id"], step["display_filename"])
            elif step["outcome"] == "NOTIFY_DRAFT":
                self.host.notify_step(step)
            elif step["outcome"] != "HOLD":
                return step
        raise AssertionError(f"no outcome in {wanted}")

    def state_of(self, intent_id: str) -> str:
        with StateV4Store(self.cfg.state_path) as state:
            return state.intent(intent_id)["state"]

    def dispatches_now(self) -> int:
        with StateV4Store(self.cfg.state_path) as state:
            return state.dispatch_count("DRIVE_UPLOAD")

    def notification_rows(self) -> list[dict]:
        with notify.NotificationStore(notify.store_path(self.cfg)) as store:
            return store.rows()


# ---------------------------------------------------------------------------
# P-2: missing synced folder
# ---------------------------------------------------------------------------


class SyncFolderUnavailableTests(SyncBase):
    def hide_sync(self) -> Path:
        hidden = self.root / "sync-unmounted"
        self.sync_dir.rename(hidden)
        return hidden

    def test_missing_folder_at_seal_retries_later_without_sealing_effects_or_hold(self) -> None:
        self.acquire(self.adapters(eb_only()))
        step = self.next()
        self.assertEqual("DRIVE_PRECHECK", step["outcome"])
        hidden = self.hide_sync()
        sealed = self.seal(step)
        self.assertEqual(("RETRY_LATER", bridge.EXIT_RETRY, "EG_NATIVE_SYNC_FOLDER_UNAVAILABLE"),
                         (sealed["outcome"], sealed["exit_code"], sealed["support_ref"]))
        self.assertEqual("PENDING", self.state_of(step["intent_id"]))
        self.assertEqual(0, self.dispatches_now())
        self.assertFalse(any(row["kind"] == "FAILURE" and "DRIVE" in row["status_text"] for row in self.notification_rows()))
        # `next` also refuses to plan a precheck while the folder is missing, with no state change.
        retry = self.next()
        self.assertEqual(("RETRY_LATER", "EG_NATIVE_SYNC_FOLDER_UNAVAILABLE"), (retry["outcome"], retry["support_ref"]))
        # Folder back: one seal, one write, VERIFIED.
        hidden.rename(self.sync_dir)
        step = self.next()
        self.assertEqual("SEALED", self.seal(step)["outcome"])
        self.sync()
        self.assertEqual("VERIFIED", self.record_found(step["intent_id"], step["display_filename"])["outcome"])
        self.assertEqual(1, self.dispatches_now())
        self.assertEqual([step["display_filename"]], [p.name for p in self.sync_dir.iterdir()])

    def test_a_file_in_place_of_the_folder_is_not_written_through(self) -> None:
        self.acquire(self.adapters(eb_only()))
        step = self.next()
        self.hide_sync()
        self.sync_dir.write_bytes(b"not a folder")
        sealed = self.seal(step)
        self.assertEqual("RETRY_LATER", sealed["outcome"])
        self.assertEqual(b"not a folder", self.sync_dir.read_bytes())
        self.assertEqual(0, self.dispatches_now())

    def test_a_junction_in_place_of_the_folder_is_refused_and_nothing_escapes(self) -> None:
        self.acquire(self.adapters(eb_only()))
        step = self.next()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.sync_dir.rmdir()
        if not base.make_junction(self.sync_dir, elsewhere):
            self.skipTest("junctions unavailable")
        sealed = self.seal(step)
        self.assertEqual(("RETRY_LATER", "EG_NATIVE_SYNC_FOLDER_UNSAFE"), (sealed["outcome"], sealed["support_ref"]))
        self.assertEqual([], list(elsewhere.iterdir()))
        self.assertEqual(0, self.dispatches_now())


# ---------------------------------------------------------------------------
# H-4: staged, atomic, no-replace publication
# ---------------------------------------------------------------------------


class StagedPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_publication_is_whole_and_leaves_no_staging_file(self) -> None:
        bridge.publish_staged(self.folder, "bill.pdf", b"%PDF-whole")
        self.assertEqual(["bill.pdf"], [p.name for p in self.folder.iterdir()])
        self.assertEqual(b"%PDF-whole", (self.folder / "bill.pdf").read_bytes())

    def test_an_existing_name_is_never_replaced(self) -> None:
        (self.folder / "bill.pdf").write_bytes(b"owner file")
        with self.assertRaises(Exception):
            bridge.publish_staged(self.folder, "bill.pdf", b"%PDF-new")
        self.assertEqual(b"owner file", (self.folder / "bill.pdf").read_bytes())
        self.assertEqual(["bill.pdf"], [p.name for p in self.folder.iterdir()])

    def test_a_failure_while_publishing_leaves_no_partial_final_file(self) -> None:
        from energygrid_bill_downloader import publication

        with mock.patch.object(publication, "publish_no_replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                bridge.publish_staged(self.folder, "bill.pdf", b"%PDF-x")
        self.assertEqual([], list(self.folder.iterdir()))

    def test_a_failure_while_writing_the_stage_leaves_nothing(self) -> None:
        real_open = open

        class Broken:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def write(self, data):
                self.handle.write(data[:3])
                raise OSError("device full")

        def fake_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            return Broken(handle) if mode == "xb" else handle

        with mock.patch("builtins.open", side_effect=fake_open):
            with self.assertRaises(OSError):
                bridge.publish_staged(self.folder, "bill.pdf", b"%PDF-partial-bytes")
        self.assertEqual([], list(self.folder.iterdir()))

    def test_a_cross_volume_publication_is_refused(self) -> None:
        from energygrid_bill_downloader import publication

        with mock.patch.object(publication, "volume_identity", side_effect=[(1, "c:"), (2, "d:")]):
            with self.assertRaises(ConfigError):
                bridge.publish_staged(self.folder, "bill.pdf", b"%PDF-x")
        self.assertEqual([], list(self.folder.iterdir()))

    def test_crash_leftovers_are_removed_by_exact_name_only(self) -> None:
        stale = self.folder / f"{bridge.STAGE_PREFIX}{'a' * 32}{bridge.STAGE_SUFFIX}"
        stale.write_bytes(b"%PDF-trunc")
        keep = [".eg-stage-short.part", "notes.part", f"{bridge.STAGE_PREFIX}{'b' * 32}.pdf", "bill.pdf"]
        for name in keep:
            (self.folder / name).write_bytes(b"x")
        self.assertEqual(1, bridge.clean_stale_stages(self.folder))
        self.assertEqual(sorted(keep), sorted(p.name for p in self.folder.iterdir()))


class SealCleansStagesTests(SyncBase):
    def test_a_stage_left_by_a_crashed_run_is_cleaned_at_the_next_seal(self) -> None:
        self.acquire(self.adapters(eb_only()))
        stale = self.sync_dir / f"{bridge.STAGE_PREFIX}{'c' * 32}{bridge.STAGE_SUFFIX}"
        stale.write_bytes(b"%PDF-truncated")
        step = self.next()
        self.assertEqual("SEALED", self.seal(step)["outcome"])
        self.assertEqual([step["display_filename"]], [p.name for p in self.sync_dir.iterdir()])


# ---------------------------------------------------------------------------
# P-3: sealed but never recorded
# ---------------------------------------------------------------------------


class ReconcilePendingTests(SyncBase):
    def seal_without_record(self) -> dict:
        self.acquire(self.adapters(eb_only()))
        step = self.next()
        self.assertEqual("SEALED", self.seal(step)["outcome"])
        return step

    def test_a_late_sync_is_reconciled_by_a_later_run_with_no_second_write_or_email(self) -> None:
        step = self.seal_without_record()
        self.sync()
        offer = self.next_until(("DRIVE_RECONCILE_PENDING",))
        self.assertEqual(step["intent_id"], offer["intent_id"])
        self.assertEqual("UNKNOWN", offer["intent_state"])
        self.assertEqual({"title": step["display_filename"], "parentId": FLAT, "fileSize": str(step["byte_size"])},
                         offer["await_file"])
        recorded = self.record_found(offer["intent_id"], offer["display_filename"])
        self.assertEqual("VERIFIED", recorded["outcome"])
        self.assertEqual(1, self.dispatches_now())
        self.assertEqual([step["display_filename"]], [p.name for p in self.sync_dir.iterdir()])
        self.assertEqual(1, sum(1 for row in self.host.drive.values() if row["title"] == step["display_filename"]))
        self.assertEqual("IDLE", self.next_until(("IDLE",))["outcome"])
        successes = [row for row in self.notification_rows() if row["kind"] == "SUCCESS"]
        self.assertEqual(1, len(successes))
        self.assertEqual(bridge.HASH_LOCAL_MIRROR,
                         self.status_now()["streams"][EB]["invoices"][0]["drive_upload"]["hash_provenance"])

    def test_without_the_local_mirror_nothing_is_offered_and_the_incident_is_not_repeated(self) -> None:
        step = self.seal_without_record()
        (self.sync_dir / step["display_filename"]).unlink()
        for _ in range(2):
            outcome = self.next_until(("IDLE", "DRIVE_RECONCILE_PENDING", "CEILING_REACHED", "NOTIFY_CEILING_REACHED"))
            self.assertNotEqual("DRIVE_RECONCILE_PENDING", outcome["outcome"])
        failures = [row for row in self.notification_rows() if row["status_text"] == "DRIVE_UPLOAD_UNKNOWN"]
        self.assertEqual(1, len(failures))
        report = self.status_now()["streams"][EB]
        self.assertEqual(("BLOCKED", "EG_V4_DISPATCH_OUTCOME_UNKNOWN"), (report["state"], report["support_ref"]))

    def test_a_different_local_mirror_or_changed_archive_is_never_offered(self) -> None:
        step = self.seal_without_record()
        (self.sync_dir / step["display_filename"]).write_bytes(b"%PDF-someone else")
        self.assertNotEqual("DRIVE_RECONCILE_PENDING", self.next_until(("IDLE", "DRIVE_RECONCILE_PENDING"))["outcome"])

    def test_a_changed_archive_is_never_offered(self) -> None:
        self.seal_without_record()
        self.sync()
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(EB)[0]
        archive = self.cfg.archive_root / invoice["archive_relpath"]
        archive.write_bytes(archive.read_bytes() + b"\n%tampered")
        self.assertNotEqual("DRIVE_RECONCILE_PENDING", self.next_until(("IDLE", "DRIVE_RECONCILE_PENDING"))["outcome"])

    def test_a_loose_or_wrong_file_is_never_verified(self) -> None:
        step = self.seal_without_record()
        offer = self.next_until(("DRIVE_RECONCILE_PENDING",))
        other = self.host.put(FLAT, "SEP 2026 - EnergyGrid - Tenant Bill - other.pdf", b"%PDF-other")
        recorded = self.call(bridge.drive_record, offer["intent_id"], {"outcome": "CREATED", "file": self.host.metadata(other)})
        self.assertNotEqual("VERIFIED", recorded["outcome"])
        self.assertNotEqual("VERIFIED", self.state_of(step["intent_id"]))
        self.assertEqual(1, self.dispatches_now())

    def test_a_hold_ahead_in_the_stream_is_never_skipped(self) -> None:
        first = {EB: [("EB Invoice 0001.pdf", "2026-08-01")]}
        self.acquire(self.adapters(first))
        self.next_until(("IDLE",))
        self.acquire(self.adapters(eb_only()))
        later = self.next()
        self.assertEqual("DRIVE_PRECHECK", later["outcome"])
        self.assertEqual("SEALED", self.seal(later)["outcome"])
        self.sync()
        with StateV4Store(self.db) as state:
            older = state.intents_for_invoice(state.invoices_for_stream(EB)[0]["invoice_key"])["DRIVE_UPLOAD"]
        # A conflicting outcome for the older, VERIFIED upload holds it (v4 rule).
        self.call(bridge.drive_record, older["intent_id"], {"outcome": "DENIED"})
        self.assertEqual("HOLD", self.state_of(older["intent_id"]))
        outcome = self.next_until(("IDLE", "DRIVE_RECONCILE_PENDING", "NOTIFY_CEILING_REACHED", "CEILING_REACHED"))
        self.assertNotEqual("DRIVE_RECONCILE_PENDING", outcome["outcome"])
        self.assertNotEqual("VERIFIED", self.state_of(later["intent_id"]))

    def test_native_upload_mode_never_offers_a_reconcile(self) -> None:
        cfg = base.NativeBridgeBase.make_config(self)
        with StateV4Store(self.db) as state:
            self.assertIsNone(bridge.reconcile_candidate(state, cfg, EB))


# ---------------------------------------------------------------------------
# P-4: effect ceilings
# ---------------------------------------------------------------------------


class CeilingConfigTests(unittest.TestCase):
    def raw(self, root: Path, ceiling) -> dict:
        raw = base.ConfigTests().raw(root)
        raw["effect_ceiling"] = ceiling
        return raw

    def test_lifetime_is_the_default_and_per_period_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            load = lambda c: bridge.load_native_config(self.raw(root, c), checkout_root=base.REPO_ROOT)  # noqa: E731
            self.assertEqual(("LIFETIME", None), (load({"drive_uploads": 2, "gmail_sends": 4}).ceiling_mode, None))
            self.assertEqual("LIFETIME", load({"mode": "LIFETIME", "drive_uploads": 2, "gmail_sends": 4}).ceiling_mode)
            per = load({"mode": "PER_PERIOD", "period_days": 7, "drive_uploads": 2, "gmail_sends": 4})
            self.assertEqual(("PER_PERIOD", 7), (per.ceiling_mode, per.ceiling_period_days))
            for bad in (
                {"mode": "PER_PERIOD", "period_days": 0, "drive_uploads": 2, "gmail_sends": 4},
                {"mode": "PER_PERIOD", "period_days": 32, "drive_uploads": 2, "gmail_sends": 4},
                {"mode": "PER_PERIOD", "period_days": "7", "drive_uploads": 2, "gmail_sends": 4},
                {"mode": "PER_PERIOD", "drive_uploads": 2, "gmail_sends": 4},
                {"mode": "PER_PERIOD", "period_days": 7, "drive_uploads": 2, "gmail_sends": 4, "extra": 1},
                {"mode": "DAILY", "drive_uploads": 2, "gmail_sends": 4},
                {"mode": "PER_PERIOD", "period_days": 7, "drive_uploads": 21, "gmail_sends": 4},
                {"drive_uploads": 2},
            ):
                with self.subTest(bad=bad), self.assertRaises(ConfigError):
                    load(bad)

    def test_periods_are_whole_utc_day_buckets_from_the_epoch(self) -> None:
        cfg = mock.Mock(ceiling_mode="PER_PERIOD", ceiling_period_days=1)
        self.assertEqual(("P1D-2026-10-15", "2026-10-15T00:00:00Z"), bridge.ceiling_period(cfg, OCT15 + DAY_MS - 1000))
        self.assertEqual(("P1D-2026-10-16", "2026-10-16T00:00:00Z"), bridge.ceiling_period(cfg, OCT15 + DAY_MS))
        week = mock.Mock(ceiling_mode="PER_PERIOD", ceiling_period_days=7)
        key, start = bridge.ceiling_period(week, OCT15)
        self.assertEqual(0, (bridge.ms_from_iso(start) // DAY_MS) % 7)
        self.assertEqual(("LIFETIME", None), bridge.ceiling_period(mock.Mock(ceiling_mode="LIFETIME"), OCT15))


class PerPeriodCeilingTests(SyncBase):
    ceilings = (1, 1)

    def make_config(self, **changes) -> bridge.NativeConfig:
        return super().make_config(ceiling_mode="PER_PERIOD", ceiling_period_days=1, **changes)

    def setUp(self) -> None:
        super().setUp()
        self.clock = OCT15 + DAY_MS - 120_000  # 2026-10-15T23:58:00Z

    def test_drive_cap_holds_within_the_day_and_releases_in_the_next(self) -> None:
        self.acquire()
        first = self.next()
        self.assertEqual("SEALED", self.seal(first)["outcome"])
        self.sync()
        self.record_found(first["intent_id"], first["display_filename"])
        blocked = self.next_until(("CEILING_REACHED", "NOTIFY_CEILING_REACHED"))
        self.assertIn(blocked["outcome"], ("CEILING_REACHED", "NOTIFY_CEILING_REACHED"))
        other = TN if first["stream"] == EB else EB
        report = self.status_now()
        self.assertEqual(("BLOCKED", "EG_NATIVE_EFFECT_CEILING_REACHED"),
                         (report["streams"][other]["state"], report["streams"][other]["support_ref"]))
        self.assertEqual({"used": 1, "limit": 1, "reached": True}, report["ceiling"]["drive_uploads"])
        incidents = [row for row in self.notification_rows() if row["status_text"] == "DRIVE_CEILING_REACHED"]
        self.assertEqual([other], [row["stream"] for row in incidents])
        self.next()  # same period: no second incident
        self.assertEqual(1, sum(1 for row in self.notification_rows() if row["status_text"] == "DRIVE_CEILING_REACHED"))
        self.clock = OCT15 + DAY_MS + 60_000  # next UTC day
        step = self.next_until(("DRIVE_PRECHECK",))
        self.assertEqual(other, step["stream"])
        self.assertEqual(2, self.dispatches_now() + 1)

    def test_a_sealed_dispatch_counts_even_when_its_outcome_is_unknown(self) -> None:
        self.acquire()
        first = self.next()
        self.seal(first)
        self.call(bridge.drive_record, first["intent_id"], {"outcome": "NO_RESPONSE"})
        with StateV4Store(self.db) as state:
            self.assertEqual(1, bridge.drive_dispatches_counted(state, self.cfg, self.clock))
            self.assertTrue(bridge._at_ceiling(state, self.cfg, "DRIVE_UPLOAD", self.clock))

    def test_gmail_cap_per_period_never_notifies_about_itself(self) -> None:
        self.cfg = self.make_config(max_drive_uploads=4)
        self.acquire()
        sent = 0
        for _ in range(12):
            step = self.next()
            if step["outcome"] == "DRIVE_PRECHECK":
                self.seal(step)
                self.sync()
                self.record_found(step["intent_id"], step["display_filename"])
            elif step["outcome"] == "NOTIFY_DRAFT":
                self.host.notify_step(step)
                sent += 1
            else:
                break
        self.assertEqual(("NOTIFY_CEILING_REACHED", 1), (step["outcome"], sent))
        self.assertFalse(any("GMAIL" in row["status_text"] or "CEILING" in row["status_text"]
                             for row in self.notification_rows()))
        self.assertTrue(self.status_now()["ceiling"]["gmail_sends"]["reached"])
        self.clock = OCT15 + DAY_MS + 60_000
        self.assertEqual("NOTIFY_DRAFT", self.next()["outcome"])

    def test_a_send_sealed_yesterday_but_recorded_today_counts_for_yesterday(self) -> None:
        self.cfg = self.make_config(max_drive_uploads=4)
        self.acquire(self.adapters(eb_only()))
        step = self.next_until(("NOTIFY_DRAFT",))
        nid = step["notification_id"]
        draft_id = self.host.create_draft(step)
        self.notify(lambda store: notify.draft_evidence(store, self.cfg, nid, draft_id,
                                                        self.raw_file(draft_id, self.host.raw(draft_id))))
        self.clock = OCT15 + DAY_MS - 5_000  # sealed at 23:59:55 on 15 Oct
        sealed = self.notify(lambda store: notify.seal(store, self.cfg, nid, draft_id, self.now_iso()))
        self.clock = OCT15 + DAY_MS + 60_000  # recorded after midnight
        message_id = self.host.send_draft(sealed["send_draft_id"])
        self.notify(lambda store: notify.record(
            store, nid, {"outcome": "SENT", "draft_id": draft_id, "message_id": message_id}, self.now_iso()))
        with notify.NotificationStore(notify.store_path(self.cfg)) as store:
            self.assertEqual(0, notify.sends_counted(store, self.cfg, bridge._iso(self.clock)))
            self.assertEqual(1, notify.sends_counted(store, self.cfg, "2026-10-15T23:59:59Z"))

    def test_a_legacy_seal_without_ledger_time_counts_unless_provably_older(self) -> None:
        self.acquire()
        start = bridge.ceiling_period(self.cfg, self.clock)[1]
        with notify.NotificationStore(notify.store_path(self.cfg)) as store, store.write() as conn:
            for key, updated in (("legacy-in", start), ("legacy-old", "2026-10-14T10:00:00Z")):
                store._insert(conn, key, updated, kind="SUCCESS", stream=EB, status_text="PROCESSED")
                conn.execute("UPDATE notification SET state='SENT', draft_id='D1', message_id='M1', updated_at=? "
                             "WHERE dedup_key=?", (updated, key))
        with notify.NotificationStore(notify.store_path(self.cfg)) as store:
            self.assertEqual(1, notify.sends_counted(store, self.cfg, bridge._iso(self.clock)))


# ---------------------------------------------------------------------------
# P-7: audited HOLD release on a version-5 copy
# ---------------------------------------------------------------------------


class HoldReleaseTests(SyncBase):
    def migrate(self) -> Path:
        """The documented same-filename switch: the configuration is never repointed."""
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = state_module.switch_hold_recovery_v5(self.db, now_ms=self.tick(), history=bridge.switch_history(self.cfg))
        self.assertEqual(("SWITCHED_V5", before), (result["status"], result["v4_sha256"]))
        backup = self.db.with_name(result["v4_backup"])
        self.assertEqual(before, hashlib.sha256(backup.read_bytes()).hexdigest(), "the v4 bytes are kept unchanged")
        return backup

    def release(self, intent_id: str, evidence: dict, reason: str) -> dict:
        with StateV4Store(self.db) as state:
            return bridge.hold_release(state, self.cfg, intent_id, evidence, reason_code=reason, operator="ops-synthetic",
                                       run_id=bridge.new_run_id(), now_ms=self.tick())

    def named_conflict_hold(self) -> dict:
        display = "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf"
        conflict = self.cfg.named_archive_root / "EB Bill" / display
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b"someone else's file")
        self.acquire(self.adapters(eb_only()))
        with StateV4Store(self.db) as state:
            invoice = state.invoices_for_stream(EB)[0]
            intent = state.intents_for_invoice(invoice["invoice_key"])["DRIVE_UPLOAD"]
        self.assertEqual(("HOLD", "EG_NATIVE_NAMED_ARCHIVE_CONFLICT"), (intent["state"], intent["support_ref"]))
        conflict.unlink()
        return intent

    def dispatched_hold(self) -> tuple[dict, dict]:
        self.acquire(self.adapters(eb_only()))
        step = self.next()
        self.seal(step)
        self.sync()
        found = self.host.search(FLAT, step["display_filename"])[0]
        wrong = dict(self.host.metadata(found["id"]), fileSize="1")
        self.call(bridge.drive_record, step["intent_id"], {"outcome": "CREATED", "file": wrong})
        self.assertEqual("HOLD", self.state_of(step["intent_id"]))
        return step, self.host.metadata(found["id"])

    def test_an_undispatched_hold_returns_to_pending_once_then_delivers_once(self) -> None:
        intent = self.named_conflict_hold()
        self.migrate()
        released = self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        self.assertEqual(("PENDING", 0), (released["outcome"], released["exit_code"]))
        with StateV4Store(self.db) as state:
            audit = state.hold_releases(intent["intent_id"])
        self.assertEqual([("PENDING", "NAMED_ARCHIVE_CONFLICT_CLEARED", "ops-synthetic")],
                         [(row["to_state"], row["reason_code"], row["operator"]) for row in audit])
        with self.assertRaises(NativeRefusal) as again:
            self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        self.assertEqual("EG_V5_RELEASE_NOT_HOLD", again.exception.support_ref)
        self.next_until(("IDLE",))
        self.assertEqual("VERIFIED", self.state_of(intent["intent_id"]))
        self.assertEqual(1, self.dispatches_now())

    def notices(self) -> list[dict]:
        return [row for row in self.notification_rows() if row["status_text"] == notify.HOLD_RELEASED_STATUS]

    def released_mail(self) -> list[dict]:
        return [message for message in self.host.sent if " hold released - " in message["subject"]]

    def test_a_no_dispatch_release_stages_exactly_one_owner_notice(self) -> None:
        intent = self.named_conflict_hold()
        self.migrate()
        sent_before = len(self.host.sent)
        self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        self.assertEqual([("PENDING", EB, notify.HOLD_RELEASED_REF)],
                         [(row["state"], row["stream"], row["support_ref"]) for row in self.notices()])
        self.assertEqual(sent_before, len(self.host.sent), "staged only: the operator command never sends")
        with self.assertRaises(NativeRefusal):
            self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        self.assertEqual(1, len(self.notices()))
        self.next_until(("IDLE",))
        mail = self.released_mail()
        self.assertEqual(1, len(mail))
        self.assertEqual(([base.RECIPIENT], [], "SEP 2026 - EnergyGrid - EB Bill - EB Invoice 0002.pdf"),
                         (mail[0]["to"], mail[0]["cc"], mail[0]["body"].split("File: ")[1].split("\n")[0]))
        self.assertEqual(1, self.dispatches_now())
        sent = len(self.host.sent)
        self.next_until(("IDLE",))  # restarts and repeats: no second notice, send or upload
        self.assertEqual((sent, 1, 1), (len(self.host.sent), len(self.notices()), self.dispatches_now()))

    def test_a_crash_right_after_the_release_is_healed_once_by_the_next_run(self) -> None:
        intent = self.named_conflict_hold()
        self.migrate()
        with mock.patch.object(bridge, "_sweep_notifications", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        self.assertEqual(("PENDING", []), (self.state_of(intent["intent_id"]), self.notices()))
        self.next_until(("IDLE",))
        self.next_until(("IDLE",))
        self.assertEqual((1, 1), (len(self.notices()), len(self.released_mail())))

    def test_an_uncertain_notice_send_is_never_resent(self) -> None:
        intent = self.named_conflict_hold()
        self.migrate()
        self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        for _ in range(12):
            step = self.next()
            if step["outcome"] == "NOTIFY_DRAFT" and step["notification_id"] == self.notices()[0]["notification_id"]:
                self.host.notify_step(step, outcome="NO_RESPONSE")
                break
            if step["outcome"] == "DRIVE_PRECHECK":
                if self.seal(step)["outcome"] == "SEALED":
                    self.sync()
                    self.record_found(step["intent_id"], step["display_filename"])
            elif step["outcome"] == "NOTIFY_DRAFT":
                self.host.notify_step(step)
        self.assertEqual("UNKNOWN", self.notices()[0]["state"])
        self.next_until(("IDLE",))
        self.assertEqual(("UNKNOWN", 0), (self.notices()[0]["state"], len(self.released_mail())))

    def test_a_rollback_never_erases_a_release_audit(self) -> None:
        intent = self.named_conflict_hold()
        backup = self.migrate()
        self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        v5_bytes = self.db.read_bytes()
        with self.assertRaises(OfflineV4Error) as refused:
            state_module.rollback_hold_recovery_v5(self.db, backup, now_ms=self.tick())
        self.assertEqual("EG_V5_ROLLBACK_AFTER_RELEASE", refused.exception.support_ref)
        self.assertEqual(v5_bytes, self.db.read_bytes())

    def test_a_dispatched_hold_never_returns_to_pending(self) -> None:
        step, _metadata = self.dispatched_hold()
        self.migrate()
        with self.assertRaises(NativeRefusal) as refused:
            self.release(step["intent_id"], {"mode": "NO_DISPATCH"}, "OPERATOR_REVIEWED_NO_EFFECT")
        self.assertEqual("EG_V5_RELEASE_AFTER_DISPATCH", refused.exception.support_ref)
        self.assertEqual("HOLD", self.state_of(step["intent_id"]))

    def test_a_dispatched_hold_becomes_verified_only_with_exact_evidence_and_no_new_effect(self) -> None:
        step, metadata = self.dispatched_hold()
        self.migrate()
        for bad in (dict(metadata, title=metadata["title"].lower()), dict(metadata, parentId="FOLDER_OTHER_00000"),
                    dict(metadata, mimeType="text/plain"), dict(metadata, fileSize="2")):
            with self.subTest(bad=bad), self.assertRaises(NativeRefusal):
                self.release(step["intent_id"], {"mode": "LATE_EVIDENCE", "file": bad}, "LATE_EVIDENCE_VERIFIED")
        with self.assertRaises(NativeRefusal):
            self.release(step["intent_id"], {"mode": "LATE_EVIDENCE", "file": metadata}, "OPERATOR_REVIEWED_NO_EFFECT")
        self.assertEqual("HOLD", self.state_of(step["intent_id"]))
        released = self.release(step["intent_id"], {"mode": "LATE_EVIDENCE", "file": metadata}, "LATE_EVIDENCE_VERIFIED")
        self.assertEqual("VERIFIED", released["outcome"])
        self.assertEqual(1, self.dispatches_now())
        report = self.status_now()["streams"][EB]["invoices"][0]["drive_upload"]
        self.assertEqual(("VERIFIED", bridge.HASH_LOCAL_MIRROR), (report["state"], report["hash_provenance"]))
        self.assertEqual([], self.notices(), "late evidence is notified as the normal success, not a release notice")

    def test_late_evidence_needs_the_exact_local_mirror(self) -> None:
        step, metadata = self.dispatched_hold()
        (self.sync_dir / step["display_filename"]).unlink()
        self.migrate()
        with self.assertRaises(NativeRefusal) as refused:
            self.release(step["intent_id"], {"mode": "LATE_EVIDENCE", "file": metadata}, "LATE_EVIDENCE_VERIFIED")
        self.assertEqual("EG_NATIVE_DRIVE_READBACK_REQUIRED", refused.exception.support_ref)

    def test_a_version_4_database_cannot_release(self) -> None:
        intent = self.named_conflict_hold()
        with self.assertRaises(NativeRefusal) as refused:
            self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        self.assertEqual("EG_V5_SCHEMA_REQUIRED", refused.exception.support_ref)

    def test_direct_sql_cannot_bypass_the_audit_or_the_proof(self) -> None:
        step, _metadata = self.dispatched_hold()
        self.migrate()
        connection = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            with self.assertRaisesRegex(sqlite3.Error, "EG_V4_ILLEGAL_TRANSITION"):
                connection.execute("UPDATE energygrid_v4_intent_status SET state='PENDING', version=version+1 "
                                   "WHERE intent_id=?", (step["intent_id"],))
            version = connection.execute("SELECT version FROM energygrid_v4_intent_status WHERE intent_id=?",
                                         (step["intent_id"],)).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.Error, "EG_V5_RELEASE_AFTER_DISPATCH"):
                connection.execute(
                    "INSERT INTO energygrid_v4_hold_release (intent_id, from_version, to_state, reason_code, operator, "
                    "evidence_sha256, run_id, at_utc) VALUES (?,?,?,?,?,?,?,?)",
                    (step["intent_id"], version, "PENDING", "OPERATOR_REVIEWED_NO_EFFECT", "x", "0" * 64, "r" * 36, "t"))
        finally:
            connection.close()
        self.assertEqual("HOLD", self.state_of(step["intent_id"]))

    def test_audit_rows_are_append_only(self) -> None:
        intent = self.named_conflict_hold()
        self.migrate()
        self.release(intent["intent_id"], {"mode": "NO_DISPATCH"}, "NAMED_ARCHIVE_CONFLICT_CLEARED")
        connection = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            for statement in ("UPDATE energygrid_v4_hold_release SET operator='other'",
                              "DELETE FROM energygrid_v4_hold_release"):
                with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.Error, "EG_V4_IMMUTABLE_ROW"):
                    connection.execute(statement)
        finally:
            connection.close()


class HoldRecoveryMigrationTests(base.NativeBridgeBase):
    def test_refusals_leave_the_source_untouched(self) -> None:
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        other_dir = self.root / "other"
        other_dir.mkdir()
        existing = self.db.with_name("exists.sqlite")
        existing.write_bytes(b"keep me")
        for destination, ref in ((other_dir / "v5.sqlite", "EG_V5_MIGRATION_PATH_INVALID"),
                                 (existing, "EG_V5_MIGRATION_PATH_INVALID"),
                                 (self.db, "EG_V5_MIGRATION_PATH_INVALID")):
            with self.subTest(destination=destination.name), self.assertRaises(OfflineV4Error) as refused:
                state_module.migrate_hold_recovery_copy(self.db, destination, now_ms=self.tick())
            self.assertEqual(ref, refused.exception.support_ref)
        self.assertEqual(b"keep me", existing.read_bytes())
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        with StateV4Store(self.db) as state:
            state.acquire_lease("held-by-run", now_ms=self.clock, ttl_ms=600_000)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()  # the simulated run's own lease write
        with self.assertRaises(OfflineV4Error) as held:
            state_module.migrate_hold_recovery_copy(self.db, self.db.with_name("v5.sqlite"), now_ms=self.clock + 1000)
        self.assertEqual(("EG_V5_SOURCE_LEASE_HELD", 10), (held.exception.support_ref, held.exception.exit_code))
        self.assertFalse(self.db.with_name("v5.sqlite").exists())
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_a_copy_is_v5_with_identical_rows_and_cannot_be_migrated_again(self) -> None:
        self.acquire()
        self.host.drain()
        destination = self.db.with_name("v5.sqlite")
        result = state_module.migrate_hold_recovery_copy(self.db, destination, now_ms=self.tick())
        connection = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual("COMPLETE_V5", state_module._inspect_v4_database(connection))
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM energygrid_v4_dispatch").fetchone()[0])
        finally:
            connection.close()
        # A bare copy binds no state filename or history, so it is never a usable switch-over candidate.
        with self.assertRaises(OfflineV4Error) as unbound:
            StateV4Store(destination).__enter__()
        self.assertEqual("EG_V5_HISTORY_BINDING_MISSING", unbound.exception.support_ref)
        with StateV4Store(self.db) as state:
            self.assertEqual(4, state.schema_version)
        with self.assertRaises(OfflineV4Error) as again:
            state_module.migrate_hold_recovery_copy(destination, destination.with_name("v6.sqlite"), now_ms=self.tick())
        self.assertEqual("EG_V5_ALREADY_MIGRATED", again.exception.support_ref)
        self.assertEqual(64, len(result["data_digest"]))

    def test_a_source_change_during_the_copy_leaves_no_migrated_destination(self) -> None:
        """B-1b: EG_V5_SOURCE_CHANGED removes only this attempt's destination; nothing else is touched."""
        destination = self.db.with_name("v5.sqlite")
        bystander = self.db.with_name("bystander.sqlite")
        bystander.write_bytes(b"pre-existing")
        folder = sorted(path.name for path in self.db.parent.iterdir())
        apply = state_module._apply_v5_migration

        def external_write(connection) -> None:
            with self.db.open("ab") as handle:  # a non-SQLite writer the write lock cannot stop
                handle.write(b"\0")
            apply(connection)

        with mock.patch.object(state_module, "_apply_v5_migration", side_effect=external_write):
            with self.assertRaises(OfflineV4Error) as changed:
                state_module.migrate_hold_recovery_copy(self.db, destination, now_ms=self.tick())
        self.assertEqual("EG_V5_SOURCE_CHANGED", changed.exception.support_ref)
        self.assertFalse(destination.exists(), "no stale migrated destination")
        self.assertEqual(b"pre-existing", bystander.read_bytes())
        self.assertTrue(self.db.read_bytes().endswith(b"\0"), "the source is preserved as found, never reverted")
        self.assertEqual(folder, sorted(path.name for path in self.db.parent.iterdir()), "no file added or removed")

    def test_the_copy_holds_the_source_write_lock_throughout(self) -> None:
        """Deterministic lock proof (no timeout): a second writer is refused at once during the copy."""
        observed = []
        apply = state_module._apply_v5_migration

        def competing_writer(connection) -> None:
            other = sqlite3.connect(str(self.db), timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                observed.append("acquired")
            except sqlite3.OperationalError as exc:
                observed.append(str(exc))
            finally:
                other.close()
            apply(connection)

        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with mock.patch.object(state_module, "_apply_v5_migration", side_effect=competing_writer):
            state_module.migrate_hold_recovery_copy(self.db, self.db.with_name("v5.sqlite"), now_ms=self.tick())
        # Called for the migration and again for the schema reference check: locked both times.
        self.assertTrue(observed)
        self.assertEqual({"database is locked"}, set(observed))
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        # Positive control: after the copy the lock is released and the source is writable again.
        with StateV4Store(self.db) as state:
            state.acquire_lease("after-copy", now_ms=self.tick(), ttl_ms=1000)

    def test_version_and_schema_must_agree(self) -> None:
        for version, migrate_schema in ((5, False), (4, True)):
            with self.subTest(version=version):
                copy = self.db.with_name(f"mixed-{version}.sqlite")
                copy.write_bytes(self.db.read_bytes())
                connection = sqlite3.connect(str(copy), isolation_level=None)
                try:
                    if migrate_schema:
                        state_module._apply_v5_migration(connection)
                    connection.execute(f"PRAGMA user_version={version}")
                    self.assertEqual("INCOMPATIBLE_V4", state_module._inspect_v4_database(connection))
                finally:
                    connection.close()


# ---------------------------------------------------------------------------
# P-7 switch-over (G3, Web #226:6190091444): B-1 dependent-store history, B-1b, rollback
# ---------------------------------------------------------------------------


class HoldRecoverySwitchTests(base.NativeBridgeBase):
    """The v4 -> v5 switch keeps the state filename, so the stores named after it keep their history."""

    def history(self) -> None:
        """Both streams delivered and notified (two sends), with provenance and one backfill row."""
        self.acquire()
        self.host.drain()
        from energygrid_bill_downloader.native_backfill import BackfillLedger, ledger_path

        with BackfillLedger(ledger_path(self.cfg)) as ledger, ledger.write() as conn:
            conn.execute(
                "INSERT INTO backfill (display_key, stream, display_filename, invoice_date, sha256, byte_size, state, "
                "drive_file_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("jul 2026 - energygrid - eb bill - old.pdf", EB, "JUL 2026 - EnergyGrid - EB Bill - old.pdf",
                 "2026-07-01", "a" * 64, 10, "VERIFIED", "FILE_BACKFILL_SYNTHETIC_1", self.now_iso(), self.now_iso()))

    def snapshot(self) -> dict:
        status = self.status()
        from energygrid_bill_downloader.native_backfill import BackfillLedger, ledger_path

        with BackfillLedger(ledger_path(self.cfg)) as ledger:
            backfill = [(row["display_key"], row["state"]) for row in ledger.rows()]
        return {
            "sent": len(self.host.sent), "drafts": len(self.host.drafts),
            "effects": status["effects"], "gmail_used": status["ceiling"]["gmail_sends"]["used"],
            "notifications": status["notifications"]["counts"],
            "provenance": sorted(item["drive_upload"]["hash_provenance"]
                                 for stream in status["streams"].values() for item in stream["invoices"]),
            "backfill": backfill,
        }

    def switch(self) -> dict:
        return state_module.switch_hold_recovery_v5(self.db, now_ms=self.tick(), history=bridge.switch_history(self.cfg))

    def folder(self) -> list[str]:
        return sorted(path.name for path in self.db.parent.iterdir())

    def test_the_switch_keeps_every_dependent_history_and_the_next_run_is_idle(self) -> None:
        self.history()
        before = self.snapshot()
        self.assertEqual((2, 2, 2), (before["sent"], before["effects"]["gmail_dispatches"], before["gmail_used"]))
        self.assertEqual([bridge.HASH_PROVIDER_READBACK] * 2, before["provenance"])
        result = self.switch()
        self.assertEqual("SWITCHED_V5", result["status"])
        with StateV4Store(self.db) as state:
            self.assertEqual(5, state.schema_version)
            bound = state.history_bindings()
        self.assertEqual({"STATE", "V4_BACKUP", "NOTIFICATIONS", "PROVENANCE", "BACKFILL"}, set(bound))
        self.assertEqual((1, 2, 2), (bound["NOTIFICATIONS"]["present"], bound["NOTIFICATIONS"]["row_count"],
                                     bound["NOTIFICATIONS"]["effect_count"]))
        self.assertEqual(before, self.snapshot(), "notification, ceiling, provenance and backfill history unchanged")
        self.assertEqual("IDLE", self.next()["outcome"])
        self.host.drain()
        self.assertEqual(before, self.snapshot(), "zero additional drafts, sends or uploads")
        backup = self.db.with_name(result["v4_backup"])
        self.assertFalse(os.access(backup, os.W_OK), "the v4 backup is read-only")
        self.assertEqual(result["v4_sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        self.assertFalse([name for name in self.folder() if name.endswith(".tmp")], "no switch leftovers")

    def test_store_names_must_derive_from_the_bound_state_filename(self) -> None:
        """Equivalent bridge paths: a config naming another state file beside the right database, or a
        changed store-name derivation, would read or start other stores: both refused."""
        self.history()
        self.switch()
        other = self.make_config(state_path=self.db.with_name("other.sqlite"))
        with StateV4Store(self.db) as state, self.assertRaises(NativeRefusal) as mismatched:
            bridge.next_step(state, other, run_id=bridge.new_run_id(), now_ms=self.tick())
        self.assertEqual("EG_V5_STATE_REPOINTED", mismatched.exception.support_ref)
        renamed = lambda cfg: cfg.state_path.with_name(cfg.state_path.stem + ".notify-v2.sqlite")  # noqa: E731
        with mock.patch.object(notify, "store_path", side_effect=renamed), self.assertRaises(NativeRefusal) as derived:
            self.next()
        self.assertEqual("EG_V5_STATE_REPOINTED", derived.exception.support_ref)
        self.assertFalse(renamed(self.cfg).exists())
        self.assertEqual("IDLE", self.next()["outcome"])

    def test_a_stale_store_where_a_sent_email_is_pending_again_is_refused(self) -> None:
        """An older copy of the notification store (same rows, before the send) would resend: refused."""
        self.history()
        self.switch()
        before = self.snapshot()
        with sqlite3.connect(str(notify.store_path(self.cfg))) as connection:
            connection.execute("UPDATE notification SET state='PENDING', draft_id=NULL, message_id=NULL "
                               "WHERE seq=(SELECT MIN(seq) FROM notification)")
        connection.close()
        with self.assertRaises(NativeRefusal) as refused:
            self.next()
        self.assertEqual("EG_V5_HISTORY_MISSING", refused.exception.support_ref)
        self.assertEqual((before["sent"], before["drafts"]), (len(self.host.sent), len(self.host.drafts)))

    def test_a_journal_left_beside_a_store_does_not_block_later_runs(self) -> None:
        self.history()
        self.switch()
        for path in bridge.history_paths(self.cfg).values():
            path.with_name(path.name + "-journal").write_bytes(b"")  # crash leftover, not a live transaction
        self.assertEqual("IDLE", self.next()["outcome"])

    def test_the_former_copy_and_repoint_procedure_is_refused_with_zero_effects(self) -> None:
        """Negative control for B-1: the reviewer's counterexample now refuses instead of resending."""
        self.history()
        before = self.snapshot()
        self.switch()
        renamed = self.db.with_name("state.v5.sqlite")
        renamed.write_bytes(self.db.read_bytes())
        repointed = self.make_config(state_path=renamed)
        with self.assertRaises(OfflineV4Error) as refused:
            StateV4Store(renamed).__enter__()
        self.assertEqual(("EG_V5_STATE_REPOINTED", 64), (refused.exception.support_ref, refused.exception.exit_code))
        for path in bridge.history_paths(repointed).values():
            self.assertFalse(path.exists(), "no empty store is started under the new name")
        # The same name in another folder without its stores: refused before any effect.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / self.db.name).write_bytes(self.db.read_bytes())
        moved = self.make_config(state_path=elsewhere / self.db.name)
        with StateV4Store(moved.state_path) as state, self.assertRaises(NativeRefusal) as missing:
            bridge.next_step(state, moved, run_id=bridge.new_run_id(), now_ms=self.tick())
        self.assertEqual("EG_V5_HISTORY_MISSING", missing.exception.support_ref)
        self.assertEqual([self.db.name], sorted(path.name for path in elsewhere.iterdir()))
        self.assertEqual(before, self.snapshot())

    def test_a_missing_or_emptied_store_after_the_switch_is_refused_with_zero_effects(self) -> None:
        self.history()
        self.switch()
        before = self.snapshot()
        for store, path in bridge.history_paths(self.cfg).items():
            aside = path.with_name(path.name + ".aside")
            with self.subTest(store=store, case="missing"):
                path.rename(aside)
                for command in (self.next, self.status, lambda: self.acquire()):
                    with self.assertRaises(NativeRefusal) as refused:
                        command()
                    self.assertEqual("EG_V5_HISTORY_MISSING", refused.exception.support_ref)
                with self.assertRaises(NativeRefusal):
                    self.notify(lambda _store: None)
                self.assertFalse(path.exists(), "a missing store is never recreated empty")
            with self.subTest(store=store, case="emptied"):
                sqlite3.connect(str(path)).close()
                with self.assertRaises(NativeRefusal) as refused:
                    self.next()
                self.assertEqual("EG_V5_HISTORY_MISSING", refused.exception.support_ref)
                path.unlink()
            aside.rename(path)
        self.assertEqual("IDLE", self.next()["outcome"], "positive control: restored stores pass")
        self.host.drain()
        self.assertEqual(before, self.snapshot())

    def test_the_switch_refuses_missing_or_foreign_history_and_in_flight_effects(self) -> None:
        self.history()
        paths = bridge.history_paths(self.cfg)
        source = self.db.read_bytes()

        def refused_with(reference: str) -> None:
            with self.assertRaises(OfflineV4Error) as refused:
                self.switch()
            self.assertEqual(reference, refused.exception.support_ref)
            self.assertEqual(source, self.db.read_bytes(), "the v4 source is untouched")
            self.assertFalse([n for n in self.folder() if ".v5-switch-" in n or ".v4-backup-" in n], "no leftovers")

        aside = paths["NOTIFICATIONS"].with_name("aside")
        paths["NOTIFICATIONS"].rename(aside)
        refused_with("EG_V5_HISTORY_MISSING")
        aside.rename(paths["NOTIFICATIONS"])
        with sqlite3.connect(str(paths["PROVENANCE"])) as connection:
            connection.execute("INSERT INTO drive_hash_provenance VALUES (?,?,?,?,?)",
                               ("egt4-" + "f" * 32, bridge.HASH_LOCAL_MIRROR, "b" * 64, "gdrive:FOREIGN", "t"))
        connection.close()
        refused_with("EG_V5_HISTORY_MISMATCH")

    def test_a_sealed_upload_with_no_outcome_refuses_the_switch(self) -> None:
        self.acquire()
        self.host.drive_step(self.next(), crash_after_seal=True)  # DISPATCHED with no outcome
        source = self.db.read_bytes()
        with self.assertRaises(OfflineV4Error) as dispatched:
            self.switch()
        self.assertEqual(("EG_V5_OPERATION_IN_FLIGHT", 10), (dispatched.exception.support_ref,
                                                             dispatched.exception.exit_code))
        self.assertEqual(source, self.db.read_bytes())
        self.next()  # a normal run records it as UNKNOWN (never resent); the switch is then allowed
        self.assertEqual("SWITCHED_V5", self.switch()["status"])

    def test_a_sealed_owner_email_with_no_outcome_refuses_the_switch(self) -> None:
        self.acquire()
        self.host.notify_step(self.drive_until_notification(), crash_after_seal=True)
        with self.assertRaises(OfflineV4Error) as sealed:
            self.switch()
        self.assertEqual("EG_V5_OPERATION_IN_FLIGHT", sealed.exception.support_ref)
        self.assertFalse([n for n in self.folder() if ".v5-switch-" in n or ".v4-backup-" in n])

    def test_absent_legacy_provenance_is_legitimate_and_never_relabelled(self) -> None:
        self.history()
        bridge.provenance_path(self.cfg).unlink()  # a pre-H-3 install never had the store
        self.switch()
        status = self.status()
        self.assertEqual([bridge.HASH_UNRECORDED] * 2, sorted(
            item["drive_upload"]["hash_provenance"] for stream in status["streams"].values() for item in stream["invoices"]))
        newer = {EB: [("EB Invoice 0002.pdf", "2026-09-01"), ("EB Invoice 0003.pdf", "2026-10-01")],
                 TN: [("TN Invoice 0001.pdf", "2026-08-03"), ("TN Invoice 0002.pdf", "2026-09-03")]}
        self.acquire(self.adapters(newer))
        self.host.drain()
        labels = [(item["display_filename"], item["drive_upload"]["hash_provenance"])
                  for item in self.status()["streams"][EB]["invoices"]]
        self.assertEqual([bridge.HASH_UNRECORDED, bridge.HASH_PROVIDER_READBACK], [label for _name, label in labels])

    def test_a_second_switch_and_a_held_lease_are_refused_without_leftovers(self) -> None:
        self.history()
        with StateV4Store(self.db) as state:
            state.acquire_lease("held-by-run", now_ms=self.clock, ttl_ms=600_000)
        held_bytes = self.db.read_bytes()
        with self.assertRaises(OfflineV4Error) as held:
            self.switch()
        self.assertEqual(("EG_V5_SOURCE_LEASE_HELD", 10), (held.exception.support_ref, held.exception.exit_code))
        self.assertEqual(held_bytes, self.db.read_bytes())
        self.clock += 700_000
        self.switch()
        files = self.folder()
        with self.assertRaises(OfflineV4Error) as again:
            self.switch()
        self.assertEqual("EG_V5_ALREADY_MIGRATED", again.exception.support_ref)
        self.assertEqual(files, self.folder())

    @unittest.skipUnless(os.name == "nt", "Windows refuses to rename a file another process has open")
    def test_a_state_file_open_elsewhere_is_never_switched(self) -> None:
        self.history()
        source = self.db.read_bytes()
        other = sqlite3.connect(str(self.db))  # e.g. a run that has the file open
        try:
            other.execute("SELECT COUNT(*) FROM energygrid_v4_invoice").fetchone()
            with self.assertRaises(OfflineV4Error) as busy:
                self.switch()
        finally:
            other.close()
        self.assertEqual(("EG_V5_SOURCE_IN_USE", 10), (busy.exception.support_ref, busy.exception.exit_code))
        self.assertEqual(source, self.db.read_bytes())
        self.assertFalse([n for n in self.folder() if ".v5-switch-" in n or ".v4-backup-" in n])
        self.switch()  # positive control once the file is closed

    def test_a_change_between_the_copy_and_the_rename_is_put_back_unchanged(self) -> None:
        self.history()
        move = state_module._move_no_replace
        calls = []

        def write_then_move(source: Path, destination: Path) -> None:
            if not calls:
                with source.open("ab") as handle:
                    handle.write(b"\0")
            calls.append(destination.name)
            move(source, destination)

        with mock.patch.object(state_module, "_move_no_replace", side_effect=write_then_move):
            with self.assertRaises(OfflineV4Error) as changed:
                self.switch()
        self.assertEqual("EG_V5_SOURCE_CHANGED", changed.exception.support_ref)
        self.assertEqual(self.db.name, calls[-1], "the changed v4 file is put back at its name")
        self.assertTrue(self.db.read_bytes().endswith(b"\0"))
        self.assertFalse([n for n in self.folder() if ".v5-switch-" in n or ".v4-backup-" in n])

    def test_a_crash_after_the_v4_rename_is_fail_closed_and_rolls_back(self) -> None:
        self.history()
        before = self.snapshot()
        # The exact files a switch leaves if the process dies after step 2 (v4 renamed, v5 not in place).
        temporary = self.db.with_name(f"{self.db.name}.v5-switch-crash.tmp")
        backup = self.db.with_name(f"{self.db.name}.v4-backup-crash.bak")
        history = bridge.switch_history(self.cfg)
        state_module.migrate_hold_recovery_copy(
            self.db, temporary, now_ms=self.tick(),
            bindings=lambda reader, sha: [("STATE", self.db.name, 1, 0, 0, sha), ("V4_BACKUP", backup.name, 1, 0, 0, sha),
                                          *history(reader)])
        state_module._move_no_replace(self.db, backup)
        backups = [backup.name]
        self.assertFalse(self.db.exists())
        with self.assertRaises(OfflineV4Error):
            StateV4Store(temporary).__enter__()  # the leftover copy is never usable under its own name
        with self.assertRaises(OfflineV4Error):
            self.next()  # no state file: refused, nothing created, no effect
        self.assertFalse(self.db.exists())
        result = state_module.rollback_hold_recovery_v5(self.db, self.db.with_name(backups[0]), now_ms=self.tick())
        self.assertEqual("ROLLED_BACK_V4", result["status"])
        self.assertEqual("IDLE", self.next()["outcome"])
        self.host.drain()
        self.assertEqual(before, self.snapshot())

    def test_rollback_restores_v4_only_when_no_effect_happened_since_the_switch(self) -> None:
        self.history()
        before = self.snapshot()
        result = self.switch()
        backup = self.db.with_name(result["v4_backup"])
        with self.assertRaises(OfflineV4Error) as wrong:
            state_module.rollback_hold_recovery_v5(self.db, self.db.with_name("state.sqlite.v4-backup-x.bak"),
                                                   now_ms=self.tick())
        self.assertEqual("EG_V5_MIGRATION_PATH_INVALID", wrong.exception.support_ref)
        self.assertEqual("IDLE", self.next()["outcome"])
        rolled = state_module.rollback_hold_recovery_v5(self.db, backup, now_ms=self.tick())
        self.assertEqual(("ROLLED_BACK_V4", result["v4_sha256"]), (rolled["status"], rolled["v4_sha256"]))
        with StateV4Store(self.db) as state:
            self.assertEqual(4, state.schema_version)
        self.assertEqual(result["v4_sha256"], hashlib.sha256(backup.read_bytes()).hexdigest(), "backup untouched")
        self.assertEqual("IDLE", self.next()["outcome"])
        self.assertEqual(before, self.snapshot())
        # After a new effect on v5, a rollback would forget it: refused.
        second = self.switch()
        newer = {EB: [("EB Invoice 0002.pdf", "2026-09-01"), ("EB Invoice 0003.pdf", "2026-10-01")],
                 TN: [("TN Invoice 0001.pdf", "2026-08-03"), ("TN Invoice 0002.pdf", "2026-09-03")]}
        self.acquire(self.adapters(newer))
        self.host.drain()
        v5_bytes = self.db.read_bytes()
        with self.assertRaises(OfflineV4Error) as after:
            state_module.rollback_hold_recovery_v5(self.db, self.db.with_name(second["v4_backup"]), now_ms=self.tick())
        self.assertEqual("EG_V5_ROLLBACK_AFTER_DISPATCH", after.exception.support_ref)
        self.assertEqual(v5_bytes, self.db.read_bytes())
        with self.assertRaises(OfflineV4Error) as mismatch:
            state_module.rollback_hold_recovery_v5(self.db, backup, now_ms=self.tick())
        self.assertEqual("EG_V5_ROLLBACK_BACKUP_MISMATCH", mismatch.exception.support_ref)

    def test_the_binding_rows_are_immutable_to_direct_sql(self) -> None:
        self.history()
        self.switch()
        connection = sqlite3.connect(str(self.db), isolation_level=None)
        try:
            for statement in ("UPDATE energygrid_v5_history_binding SET filename='other.sqlite' WHERE store='STATE'",
                              "DELETE FROM energygrid_v5_history_binding WHERE store='NOTIFICATIONS'"):
                with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.Error, "EG_V4_IMMUTABLE_ROW"):
                    connection.execute(statement)
        finally:
            connection.close()
        self.assertEqual("IDLE", self.next()["outcome"])

    def test_the_operator_cli_switches_and_rolls_back_under_the_same_filename(self) -> None:
        import contextlib
        import io

        from energygrid_bill_downloader import native_cli

        self.history()
        raw = {
            "schema": bridge.NATIVE_CONFIG_SCHEMA, "mode": bridge.MODE_UAT_SYNTHETIC, "state_path": str(self.db),
            "archive_root": str(self.root / "archive"), "named_archive_root": str(self.root / "bills"),
            "temp_root": str(self.root / "temp"), "outbox_root": str(self.root / "outbox"),
            "synthetic_root": str(self.root / "portal"),
            "drive": {"account": "uat@example.com", "folder_ids": dict(base.FOLDERS), "mode": "NATIVE_UPLOAD"},
            "gmail": {"account": base.SENDER, "to": [base.RECIPIENT], "subject_prefix": "EnergyGrid UAT",
                      "attach_pdf": False},
            "effect_ceiling": {"drive_uploads": self.ceilings[0], "gmail_sends": self.ceilings[1]},
        }
        config = self.root / "native.json"
        config.write_text(json.dumps(raw), encoding="utf-8")

        def cli(*argv: str) -> tuple[int, dict]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = native_cli.main(list(argv))
            return code, json.loads(out.getvalue())

        code, switched = cli("migrate-hold-recovery", "--config", str(config))
        self.assertEqual((0, "SWITCHED_V5"), (code, switched["outcome"]))
        code, status = cli("status", "--config", str(config))
        self.assertEqual((0, 2), (code, status["effects"]["gmail_dispatches"]))
        code, rolled = cli("migrate-hold-recovery", "--config", str(config),
                           "--rollback", str(self.db.with_name(switched["v4_backup"])))
        self.assertEqual((0, "ROLLED_BACK_V4"), (code, rolled["outcome"]))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            native_cli.build_parser().parse_args(["migrate-hold-recovery", "--config", str(config), "--dest", "x"])


# ---------------------------------------------------------------------------
# H-1: exact Gmail body
# ---------------------------------------------------------------------------


class ExactDraftBodyTests(base.NativeBridgeBase):
    def notification_step(self) -> dict:
        self.acquire()
        return self.drive_until_notification()

    def test_appended_or_prepended_text_is_refused(self) -> None:
        for name, change in (("appended", lambda body: body + "\nP.S. click here"),
                             ("prepended", lambda body: "Note\n" + body),
                             ("inner", lambda body: body.replace("Google Drive:", "Google Drive (moved):"))):
            with self.subTest(name=name):
                self.setUp()
                step = self.notification_step()
                with self.assertRaises(NativeRefusal) as refused:
                    self.host.notify_step(step, body=change(step["body"]))
                self.assertEqual("EG_NATIVE_DRAFT_BODY_MISMATCH", refused.exception.support_ref)
                self.tearDown()

    def test_the_exact_body_with_gmail_link_wrapping_and_whitespace_passes(self) -> None:
        step = self.notification_step()
        nid = step["notification_id"]
        draft_id = self.host.create_draft(step, body=step["body"].replace("\n", "\r\n  "))
        self.notify(lambda store: notify.draft_evidence(
            store, self.cfg, nid, draft_id, self.raw_file(draft_id, self.host.raw(draft_id, gmail_wraps_links=True))))
        sealed = self.notify(lambda store: notify.seal(store, self.cfg, nid, draft_id, self.now_iso()))
        self.assertEqual("SEALED", sealed["outcome"])


# ---------------------------------------------------------------------------
# H-2: token-bound Invoice No.
# ---------------------------------------------------------------------------


class InvoiceTokenTests(unittest.TestCase):
    LABEL = tenant_tests.LABEL_NEW

    def test_the_invoice_number_must_stand_alone(self) -> None:
        pdf = tenant_tests.tounicode_pdf
        self.assertTrue(tenant.pdf_shows_invoice(pdf("Invoice No.", self.LABEL), self.LABEL))
        self.assertTrue(tenant.pdf_shows_invoice(pdf("Invoice No. " + self.LABEL + " dated"), self.LABEL))
        for shown in (self.LABEL + "1", "X" + self.LABEL, self.LABEL + "-2", self.LABEL + "_A"):
            with self.subTest(shown=shown):
                self.assertFalse(tenant.pdf_shows_invoice(pdf(shown), self.LABEL))

    def test_separate_text_operators_never_join_into_a_match(self) -> None:
        half = len(self.LABEL) // 2
        data = tenant_tests.tounicode_pdf(self.LABEL[:half], self.LABEL[half:])
        self.assertFalse(tenant.pdf_shows_invoice(data, self.LABEL))

    def test_an_invalid_label_shape_never_matches(self) -> None:
        data = tenant_tests.tounicode_pdf("a.b")
        self.assertFalse(tenant.pdf_shows_invoice(data, "a.b"))
        self.assertFalse(tenant.pdf_shows_invoice(data, ""))


# ---------------------------------------------------------------------------
# H-3: hash provenance
# ---------------------------------------------------------------------------


class NativeUploadProvenanceTests(base.NativeBridgeBase):
    def test_native_upload_is_labelled_provider_readback_and_old_receipts_are_not_relabelled(self) -> None:
        self.acquire()
        self.host.drain()
        with StateV4Store(self.db) as state:
            streams = bridge.status(state, self.cfg, self.tick())["streams"]
        methods = {row["drive_upload"]["hash_provenance"] for stream in streams.values() for row in stream["invoices"]}
        self.assertEqual({bridge.HASH_PROVIDER_READBACK}, methods)
        bridge.provenance_path(self.cfg).unlink()
        with StateV4Store(self.db) as state:
            streams = bridge.status(state, self.cfg, self.tick())["streams"]
        methods = {row["drive_upload"]["hash_provenance"] for stream in streams.values() for row in stream["invoices"]}
        self.assertEqual({bridge.HASH_UNRECORDED}, methods)

    def test_provenance_rows_are_immutable(self) -> None:
        self.acquire()
        self.host.drain()
        connection = sqlite3.connect(str(bridge.provenance_path(self.cfg)), isolation_level=None)
        try:
            for statement in ("UPDATE drive_hash_provenance SET method='PROVIDER_READBACK_SHA256'",
                              "DELETE FROM drive_hash_provenance"):
                with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.Error, "IMMUTABLE"):
                    connection.execute(statement)
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# H-5 and H-6: PowerShell and HTTPS
# ---------------------------------------------------------------------------


class PowerShellPathTests(unittest.TestCase):
    def test_powershell_comes_from_the_system_directory_not_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            system = Path(directory)
            exe = system / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            self.assertEqual(exe, tenant.powershell_executable(lambda: system))
            calls = []

            class Completed:
                returncode = 0
                stdout = json.dumps({"u": "user", "p": "SYNTHETIC"}).encode()

            def runner(command, **kwargs):
                calls.append(command[0])
                return Completed()

            with mock.patch.dict(os.environ, {"PATH": str(Path(directory) / "evil")}):
                tenant.dpapi_credentials(Path("C:/x.clixml"), runner=runner, executable=lambda: exe)()
            self.assertEqual([str(exe)], calls)
            exe.unlink()
            with self.assertRaises(OSError):
                tenant.powershell_executable(lambda: system)

    def test_a_missing_powershell_fails_closed_without_running_anything(self) -> None:
        runner = mock.Mock()

        def missing():
            raise OSError("not found")

        with self.assertRaises(SourceContractError) as refused:
            tenant.dpapi_credentials(Path("C:/x.clixml"), runner=runner, executable=missing)()
        self.assertEqual(tenant.REF_CREDENTIAL_UNAVAILABLE, refused.exception.support_ref)
        runner.assert_not_called()

    def test_the_real_resolver_returns_an_absolute_system_path(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows only")
        path = tenant.powershell_executable()
        self.assertTrue(path.is_absolute())
        self.assertEqual("powershell.exe", path.name.lower())


class HttpsOnlyTests(unittest.TestCase):
    def test_plain_http_tenant_endpoints_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key in ("login_url", "list_url"):
                with self.subTest(key=key), self.assertRaisesRegex(ConfigError, "https"):
                    tenant.load_tenant_settings(tenant_tests.raw_config(root, **{key: "http://service.invalid/x"}),
                                                private_path=lambda value, _label: Path(value))

    def test_a_plain_http_browser_portal_is_refused_before_any_sign_in(self) -> None:
        from energygrid_bill_downloader import native_cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = base.TenantBindingTests().config(root)
            browser = root / "browser.json"
            raw = json.loads(browser.read_text(encoding="utf-8"))
            raw["portal_url"] = "http://portal.invalid/"
            browser.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "https"):
                native_cli._tenant_adapter(cfg.tenant_config_path)


# ---------------------------------------------------------------------------
# P-5: host policy templates
# ---------------------------------------------------------------------------

LOOP_SUBCOMMANDS = ("new-run", "wait", "acquire", "next", "status", "drive-seal", "drive-record", "drive-adopt",
                    "notify-draft-evidence", "notify-seal", "notify-record")
OPERATOR_SUBCOMMANDS = ("backfill-run", "backfill-record", "init-uat", "make-synthetic-fixture",
                        "migrate-hold-recovery", "hold-release", "drive-readback")


class HostPolicyTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = json.loads((NATIVE_HOST / "settings.template.json").read_text(encoding="utf-8"))
        self.allow = self.settings["permissions"]["allow"]
        self.deny = self.settings["permissions"]["deny"]

    def test_unlisted_tools_are_refused_not_prompted(self) -> None:
        self.assertEqual("dontAsk", self.settings["permissions"]["defaultMode"])

    def test_launcher_rules_name_only_exact_loop_subcommands(self) -> None:
        launcher_rules = [rule for rule in self.allow if rule.startswith("PowerShell(")]
        for rule in launcher_rules:
            match = re.fullmatch(r"PowerShell\(\{\{LAUNCHER\}\} ([a-z-]+)(?: --[a-z-]+ \*)?\)", rule)
            self.assertIsNotNone(match, rule)
            self.assertIn(match.group(1), LOOP_SUBCOMMANDS)
        self.assertEqual(sorted(LOOP_SUBCOMMANDS), sorted(re.match(r"PowerShell\(\{\{LAUNCHER\}\} ([a-z-]+)", rule).group(1)
                                                         for rule in launcher_rules))
        self.assertNotIn("PowerShell({{LAUNCHER}} *)", self.allow)
        self.assertFalse(any(rule in ("Bash", "PowerShell") or rule.startswith("Bash(") for rule in self.allow))

    def test_operator_commands_and_drive_writes_are_explicitly_denied(self) -> None:
        for sub in OPERATOR_SUBCOMMANDS:
            self.assertIn(f"PowerShell({{{{LAUNCHER}}}} {sub} *)", self.deny)
        for tool in ("create_file", "update_file", "copy_file", "trash_file", "download_file_content"):
            self.assertIn(f"mcp__{{{{DRIVE_MCP}}}}__{tool}", self.deny)
            self.assertNotIn(f"mcp__{{{{DRIVE_MCP}}}}__{tool}", self.allow)
        self.assertIn("Bash", self.deny)

    def test_templates_carry_no_private_values(self) -> None:
        for path in NATIVE_HOST.iterdir():
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", text))
                # Drive and Gmail ids look like long mixed-case alphanumeric tokens; placeholders do not.
                for token in re.findall(r"[0-9A-Za-z_-]{25,}", text):
                    self.assertFalse(re.search(r"[0-9]", token) and re.search(r"[A-Z]", token)
                                     and re.search(r"[a-z]", token) and "__" not in token, token)
                self.assertNotIn("C:\\XB", text)

    def test_host_prompt_carries_the_exact_poll_and_the_new_outcomes(self) -> None:
        prompt = (NATIVE_HOST / "host_prompt.template.md").read_text(encoding="utf-8")
        for needle in ("Rule P1", "character for character", "AND parentId equal to EXPECT_PARENT",
                       "2 or more exact matches: STOP", "exactly once", "DRIVE_RECONCILE_PENDING", "RETRY_LATER"):
            self.assertIn(needle, prompt)
        for sub in OPERATOR_SUBCOMMANDS:
            self.assertNotIn(f"CLI `{sub}", prompt)


@unittest.skipUnless(os.name == "nt", "the launcher is a Windows batch file")
class LauncherTemplateTests(unittest.TestCase):
    """The launcher refuses every non-loop subcommand before Python starts (exact, case-sensitive)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        text = (NATIVE_HOST / "egnative.cmd.template").read_text(encoding="ascii")
        text = text.replace("{{EG_HOME}}", str(root)).replace("{{PINNED_COMMIT}}", "synthetic")
        # Point the launcher at this interpreter and package so an admitted call really reaches the CLI.
        text = text.replace('set "EG_PY=%EG_HOME%\\runtime\\venv\\Scripts\\python.exe"', f'set "EG_PY={sys.executable}"')
        text = text.replace('set "EG_CODE=%EG_HOME%\\app\\synthetic\\energygrid-bill-downloader"',
                            f'set "EG_CODE={PACKAGE_ROOT}"')
        self.launcher = root / "egnative.cmd"
        self.launcher.write_text(text.replace("\r\n", "\n").replace("\n", "\r\n"), encoding="ascii")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_launcher(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["cmd.exe", "/d", "/c", str(self.launcher), *args], capture_output=True, text=True,
                              timeout=120)

    def test_operator_and_lookalike_subcommands_are_refused_before_python(self) -> None:
        for sub in (*OPERATOR_SUBCOMMANDS, "Acquire", "STATUS", "acquire2", "", "--help", "backfill-run;next"):
            with self.subTest(sub=sub):
                completed = self.run_launcher(sub) if sub else self.run_launcher()
                self.assertEqual(64, completed.returncode, completed.stdout)
                self.assertIn("EG_HOST_SUBCOMMAND_DENIED", completed.stdout)
                self.assertNotIn("EXIT=", completed.stdout)

    def test_separator_smuggling_after_a_loop_subcommand_is_refused_by_the_cli(self) -> None:
        # cmd splits `next;backfill-run` into two arguments: the launcher admits `next`, and the CLI's
        # argument parser refuses the extra word before any command runs (exit 2, no effect).
        completed = self.run_launcher("next;backfill-run")
        self.assertIn("EXIT=2", completed.stdout)
        self.assertNotIn('"command"', completed.stdout)

    def test_a_loop_subcommand_reaches_the_cli(self) -> None:
        completed = self.run_launcher("status")
        self.assertIn("EXIT=", completed.stdout)
        self.assertNotIn("EG_HOST_SUBCOMMAND_DENIED", completed.stdout)
        self.assertIn('"command": "status"', completed.stdout)


if __name__ == "__main__":
    unittest.main()
