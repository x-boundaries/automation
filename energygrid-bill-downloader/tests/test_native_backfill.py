"""Manual one-off backfill of older bills into the flat synced Drive folder (#226 V3 G1).

The scheduled Core stays latest-only; these tests cover the separate operator command only.
"""

from __future__ import annotations

import unittest

from energygrid_bill_downloader import native_backfill as backfill
from energygrid_bill_downloader import native_bridge as bridge
from energygrid_bill_downloader.native_bridge import NativeRefusal
from test_native_bridge import EB, FLAT, TN, NativeBridgeBase, pdf

EB_HISTORY = [("EB Invoice 0001.pdf", "2026-06-01"), ("EB Invoice 0002.pdf", "2026-07-01"),
              ("EB Invoice 0003.pdf", "2026-08-01"), ("EB Invoice 0004.pdf", "2026-09-01")]


def name(month: str, original: str, label: str = "EB Bill") -> str:
    return f"{month} 2026 - EnergyGrid - {label} - {original}"


class BackfillTests(NativeBridgeBase):
    folders = {EB: FLAT, TN: FLAT}

    def make_config(self, **changes) -> bridge.NativeConfig:
        sync = self.root / "sync"
        sync.mkdir(exist_ok=True)
        drive = bridge.NativeDriveSettings(account="uat@example.com", folder_ids=dict(self.folders),
                                           mode=bridge.DRIVE_LOCAL_SYNC, sync_folders={EB: sync, TN: sync})
        return super().make_config(drive=drive, **changes)

    def listing(self) -> dict:
        return {"folder_id": FLAT, "query": backfill.drive_query(FLAT), "complete": True,
                "files": [self.host.metadata(key) for key, row in self.host.drive.items()
                          if row["parent"] == FLAT and "EnergyGrid - " in row["title"]]}

    def sync(self) -> None:
        known = {row["title"] for row in self.host.drive.values() if row["parent"] == FLAT}
        for path in (self.root / "sync").iterdir():
            if path.name not in known:
                self.host.put(FLAT, path.name, path.read_bytes())

    def run_backfill(self, adapters: dict, *, max_files: int = 10) -> dict:
        with backfill.BackfillLedger(backfill.ledger_path(self.cfg)) as ledger:
            return backfill.run(self.cfg, ledger, adapters, self.listing(), max_files=max_files, now_iso=self.now_iso())

    def record(self) -> dict:
        with backfill.BackfillLedger(backfill.ledger_path(self.cfg)) as ledger:
            return backfill.record(self.cfg, ledger, self.listing(), now_iso=self.now_iso())

    def results(self, outcome: dict) -> dict:
        return {(item["stream"], item["invoice_date"]): item["result"] for item in outcome["items"]}

    def test_older_bills_are_written_with_the_owner_name_and_the_latest_is_left_to_the_scheduler(self) -> None:
        adapters = self.adapters({EB: EB_HISTORY})
        outcome = self.run_backfill(adapters)
        self.assertEqual(3, outcome["written"])
        self.assertEqual("SKIPPED_LATEST", self.results(outcome)[(EB, "2026-09-01")])
        self.assertNotIn("EB Invoice 0004.pdf", adapters[EB].acquire_calls)
        synced = sorted(path.name for path in (self.root / "sync").iterdir())
        self.assertEqual([name("AUG", "EB Invoice 0003.pdf"), name("JUL", "EB Invoice 0002.pdf"),
                          name("JUN", "EB Invoice 0001.pdf")], synced)
        for original, month in (("EB Invoice 0001.pdf", "JUN"), ("EB Invoice 0003.pdf", "AUG")):
            self.assertEqual(pdf(f"{EB} {original}"), (self.root / "sync" / name(month, original)).read_bytes())
            self.assertEqual(pdf(f"{EB} {original}"),
                             (self.cfg.named_archive_root / "EB Bill" / name(month, original)).read_bytes())
        self.assertEqual([], list(self.cfg.temp_root.iterdir()))

    def test_written_files_verify_after_drive_syncs_them(self) -> None:
        self.run_backfill(self.adapters({EB: EB_HISTORY}))
        before = self.record()
        self.assertEqual("BACKFILL_INCOMPLETE", before["outcome"])
        self.assertEqual({"AWAITING_SYNC"}, {item["result"] for item in before["items"]})
        self.sync()
        after = self.record()
        self.assertEqual("BACKFILL_VERIFIED", after["outcome"])
        self.assertEqual({"WRITTEN": 0, "VERIFIED": 3, "HOLD": 0}, after["ledger"])

    def test_a_second_run_fetches_nothing_again(self) -> None:
        self.run_backfill(self.adapters({EB: EB_HISTORY}))
        adapters = self.adapters({EB: EB_HISTORY})
        again = self.run_backfill(adapters)
        self.assertEqual(0, again["written"])
        self.assertEqual([], adapters[EB].acquire_calls)
        self.assertEqual({"LEDGER_WRITTEN"}, {r for k, r in self.results(again).items() if k[1] != "2026-09-01"})

    def test_a_bill_already_in_the_drive_folder_is_skipped_without_a_fetch(self) -> None:
        self.host.put(FLAT, name("JUL", "EB Invoice 0002.pdf"), pdf("uploaded by hand"))
        adapters = self.adapters({EB: EB_HISTORY})
        outcome = self.run_backfill(adapters)
        self.assertEqual("SKIPPED_IN_DRIVE", self.results(outcome)[(EB, "2026-07-01")])
        self.assertNotIn("EB Invoice 0002.pdf", adapters[EB].acquire_calls)
        self.assertEqual(2, outcome["written"])

    def test_the_cap_defers_the_rest_to_a_later_run(self) -> None:
        first = self.run_backfill(self.adapters({EB: EB_HISTORY}), max_files=2)
        self.assertEqual(2, first["written"])
        self.assertEqual("DEFERRED", self.results(first)[(EB, "2026-08-01")])
        second = self.run_backfill(self.adapters({EB: EB_HISTORY}), max_files=2)
        self.assertEqual(1, second["written"])
        self.assertEqual("WRITTEN", self.results(second)[(EB, "2026-08-01")])

    def test_a_bill_that_was_latest_earlier_is_picked_up_once_a_newer_one_exists(self) -> None:
        self.run_backfill(self.adapters({EB: EB_HISTORY}))
        newer = EB_HISTORY + [("EB Invoice 0005.pdf", "2026-10-01")]
        outcome = self.run_backfill(self.adapters({EB: newer}))
        self.assertEqual("WRITTEN", self.results(outcome)[(EB, "2026-09-01")])
        self.assertEqual("SKIPPED_LATEST", self.results(outcome)[(EB, "2026-10-01")])

    def test_a_different_local_file_at_the_name_is_never_overwritten(self) -> None:
        target = self.root / "sync" / name("JUN", "EB Invoice 0001.pdf")
        target.write_bytes(pdf("someone else's file"))
        outcome = self.run_backfill(self.adapters({EB: EB_HISTORY}))
        self.assertEqual("HOLD", self.results(outcome)[(EB, "2026-06-01")])
        self.assertEqual(pdf("someone else's file"), target.read_bytes())
        self.assertEqual({"WRITTEN": 2, "VERIFIED": 0, "HOLD": 1}, outcome["ledger"])

    def test_a_fetch_failure_holds_that_bill_only(self) -> None:
        adapters = self.adapters({EB: EB_HISTORY})
        del adapters[EB]._files["EB Invoice 0002.pdf"]
        outcome = self.run_backfill(adapters)
        self.assertEqual("HOLD", self.results(outcome)[(EB, "2026-07-01")])
        self.assertEqual(2, outcome["written"])
        self.assertEqual([], list(self.cfg.temp_root.iterdir()))

    def test_drive_duplicates_or_size_mismatch_hold_instead_of_verifying(self) -> None:
        self.run_backfill(self.adapters({EB: EB_HISTORY[:2]}))
        self.sync()
        self.host.put(FLAT, name("JUN", "EB Invoice 0001.pdf"), pdf(f"{EB} EB Invoice 0001.pdf"))
        outcome = self.record()
        self.assertEqual("BACKFILL_INCOMPLETE", outcome["outcome"])
        self.assertEqual("EG_BACKFILL_DRIVE_DUPLICATE", outcome["items"][0]["support_ref"])

    def test_both_streams_are_backfilled_into_the_one_flat_folder(self) -> None:
        listing = {EB: EB_HISTORY[:2], TN: [("TN Invoice 0001.pdf", "2026-06-03"), ("TN Invoice 0002.pdf", "2026-07-03")]}
        outcome = self.run_backfill(self.adapters(listing))
        self.assertEqual(2, outcome["written"])
        self.assertTrue((self.root / "sync" / name("JUN", "TN Invoice 0001.pdf", "Tenant Bill")).is_file())

    def test_drive_evidence_must_be_the_exact_complete_listing_of_the_configured_folder(self) -> None:
        adapters = self.adapters({EB: EB_HISTORY})
        for change in ({"folder_id": "OTHER_FOLDER_00001"}, {"query": "title contains 'x'"}, {"complete": False},
                       {"files": None}, {"files": [{"title": "x"}]}):
            evidence = {**self.listing(), **change}
            with self.subTest(change=change), self.assertRaises(NativeRefusal) as refused, \
                    backfill.BackfillLedger(backfill.ledger_path(self.cfg)) as ledger:
                backfill.run(self.cfg, ledger, adapters, evidence, max_files=5, now_iso=self.now_iso())
            self.assertEqual("EG_BACKFILL_DRIVE_EVIDENCE_INVALID", refused.exception.support_ref)
        self.assertEqual([], adapters[EB].acquire_calls)

    def test_max_files_is_bounded(self) -> None:
        for value in (0, -1, backfill.MAX_BACKFILL_FILES + 1, "5"):
            with self.subTest(value=value), self.assertRaises(NativeRefusal):
                self.run_backfill(self.adapters({EB: EB_HISTORY}), max_files=value)


class BackfillModeTests(NativeBridgeBase):
    def test_backfill_needs_the_local_sync_drive_mode(self) -> None:
        with backfill.BackfillLedger(backfill.ledger_path(self.cfg)) as ledger, self.assertRaises(NativeRefusal) as refused:
            backfill.run(self.cfg, ledger, self.adapters(), {}, max_files=1, now_iso=self.now_iso())
        self.assertEqual("EG_BACKFILL_LOCAL_SYNC_REQUIRED", refused.exception.support_ref)


if __name__ == "__main__":
    unittest.main()
