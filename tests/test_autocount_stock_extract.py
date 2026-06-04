import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_stock_extract as extract


class FakeSource:
    def __init__(self, rows_by_dataset):
        self.rows_by_dataset = rows_by_dataset
        self.calls = []

    def fetch_dataset(self, dataset_name, dataset_config, context):
        self.calls.append((dataset_name, dataset_config, context))
        return list(self.rows_by_dataset.get(dataset_name, []))


class FakeNotifier:
    def __init__(self):
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)


def base_config(archive_root):
    return {
        "source": "autocount_ac2",
        "job": "daily_stock_extract",
        "timezone": "Asia/Singapore",
        "overlap_days": 3,
        "archive_root": str(archive_root),
        "datasets": {
            "stock_master": {"mode": "full", "query": "SELECT * FROM item"},
            "stock_balance": {"mode": "snapshot", "query": "SELECT * FROM balance"},
            "stock_movement": {"mode": "window", "query": "SELECT * FROM movement"},
        },
    }


class AutoCountStockExtractTests(unittest.TestCase):
    def test_build_run_context_uses_overlap_window(self):
        context = extract.build_run_context(
            business_date="2026-06-04",
            overlap_days=3,
            timezone_name="Asia/Singapore",
            now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
        )

        self.assertEqual(context["business_date"], "2026-06-04")
        self.assertEqual(context["storage_batch_id"], "ac2_stock_2026-06-04")
        self.assertEqual(context["movement_from"], "2026-06-01T00:00:00+08:00")
        self.assertEqual(context["movement_to"], "2026-06-05T00:00:00+08:00")
        self.assertEqual(context["snapshot_as_at"], "2026-06-04T23:59:59+08:00")

    def test_successful_run_archives_csvs_and_manifest_without_raw_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rows = {
                "stock_master": [{"ItemCode": "SKU-001", "IsActive": "T"}],
                "stock_balance": [{"ItemCode": "SKU-001", "Location": "HQ", "BalQty": 5}],
                "stock_movement": [{"DocNo": "ADJ-001", "ItemCode": "SKU-001", "Qty": 1}],
            }
            notifier = FakeNotifier()

            manifest = extract.run_extraction(
                base_config(Path(tmpdir)),
                source=FakeSource(rows),
                notifier=notifier,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )

            batch_dir = Path(tmpdir) / "ac2_stock_2026-06-04"
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["row_counts"]["stock_master"], 1)
            self.assertEqual(manifest["row_counts"]["stock_balance"], 1)
            self.assertEqual(manifest["row_counts"]["stock_movement"], 1)
            self.assertTrue((batch_dir / "stock_master.csv").exists())
            self.assertTrue((batch_dir / "stock_balance.csv").exists())
            self.assertTrue((batch_dir / "stock_movement.csv").exists())

            saved_manifest = json.loads((batch_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest["storage_batch_id"], "ac2_stock_2026-06-04")
            self.assertNotIn("rows", saved_manifest)
            self.assertNotIn("data", saved_manifest)
            self.assertEqual(notifier.payloads, [saved_manifest])

    def test_rerun_same_business_date_replaces_dataset_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = base_config(Path(tmpdir))
            first_source = FakeSource({"stock_master": [{"ItemCode": "SKU-001"}]})
            second_source = FakeSource({"stock_master": [{"ItemCode": "SKU-002"}]})

            extract.run_extraction(
                config,
                source=first_source,
                notifier=None,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )
            extract.run_extraction(
                config,
                source=second_source,
                notifier=None,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:10:00+08:00"),
            )

            csv_path = Path(tmpdir) / "ac2_stock_2026-06-04" / "stock_master.csv"
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(rows, [{"ItemCode": "SKU-002"}])

    def test_failed_run_writes_failure_manifest_and_notifies_without_secret_config(self):
        class FailingSource(FakeSource):
            def fetch_dataset(self, dataset_name, dataset_config, context):
                if dataset_name == "stock_movement":
                    raise RuntimeError("database unavailable")
                return super().fetch_dataset(dataset_name, dataset_config, context)

        with tempfile.TemporaryDirectory() as tmpdir:
            config = base_config(Path(tmpdir))
            config["notification"] = {"webhook_url": "https://example.invalid/secret-path"}
            notifier = FakeNotifier()

            manifest = extract.run_extraction(
                config,
                source=FailingSource({"stock_master": [{"ItemCode": "SKU-001"}]}),
                notifier=notifier,
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )

            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["exception_count"], 1)
            self.assertIn("stock_movement", manifest["exceptions"][0]["dataset"])
            self.assertNotIn("webhook_url", json.dumps(manifest))
            self.assertEqual(notifier.payloads, [manifest])

    def test_notification_failure_does_not_fail_successful_archive(self):
        class FailingNotifier:
            def send(self, payload):
                raise RuntimeError("webhook receiver unavailable")

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = extract.run_extraction(
                base_config(Path(tmpdir)),
                source=FakeSource({"stock_master": [{"ItemCode": "SKU-001"}]}),
                notifier=FailingNotifier(),
                business_date="2026-06-04",
                now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
            )

            saved_manifest = json.loads(
                (Path(tmpdir) / "ac2_stock_2026-06-04" / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(saved_manifest["status"], "success")
            self.assertIn("notification_error", manifest)

    def test_sql_context_parameters_are_converted_to_datetime_objects(self):
        context = extract.build_run_context(
            business_date="2026-06-04",
            overlap_days=3,
            timezone_name="Asia/Singapore",
            now=datetime.fromisoformat("2026-06-05T02:00:00+08:00"),
        )

        params = extract.resolve_query_parameters(["movement_from", "movement_to", "snapshot_as_at"], context)

        self.assertEqual(params[0], datetime.fromisoformat("2026-06-01T00:00:00"))
        self.assertEqual(params[1], datetime.fromisoformat("2026-06-05T00:00:00"))
        self.assertEqual(params[2], datetime.fromisoformat("2026-06-04T23:59:59"))


if __name__ == "__main__":
    unittest.main()
