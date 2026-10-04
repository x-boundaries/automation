from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from energygrid_bill_downloader import cli
from energygrid_bill_downloader import portal as portal_module
from energygrid_bill_downloader.cli import SafeLogger, redact_sensitive
from energygrid_bill_downloader.config import MAX_INVENTORY_CEILING, RuntimeConfig
from energygrid_bill_downloader.reconcile import reconcile_inventory
from energygrid_bill_downloader.state import StateStore


class LoggingTests(unittest.TestCase):
    def test_sensitive_values_are_redacted_and_private_fields_are_not_logged(self) -> None:
        username = "synthetic-" + uuid.uuid4().hex
        password = "synthetic-" + uuid.uuid4().hex
        old_username = os.environ.get("ENERGYGRID_USERNAME")
        old_password = os.environ.get("ENERGYGRID_PASSWORD")
        os.environ["ENERGYGRID_USERNAME"] = username
        os.environ["ENERGYGRID_PASSWORD"] = password
        try:
            message = f"password={password}; cookie=synthetic-session; username={username}"
            redacted = redact_sensitive(message)
            self.assertNotIn(username, redacted)
            self.assertNotIn(password, redacted)
            self.assertNotIn("synthetic-session", redacted)
        finally:
            if old_username is None:
                os.environ.pop("ENERGYGRID_USERNAME", None)
            else:
                os.environ["ENERGYGRID_USERNAME"] = old_username

        with tempfile.TemporaryDirectory() as directory:
            logger = SafeLogger(Path(directory), str(uuid.uuid4()))
            logger.event(
                "run_complete",
                status="NO_NEW_BILLS",
                inventory_count=0,
                filename="private-filename.pdf",
                support_ref="EG_SYNTHETIC_REF",
            )
            content = logger.log_path.read_text(encoding="utf-8")
            self.assertNotIn("private-filename.pdf", content)
            self.assertIn("EG_SYNTHETIC_REF", content)
            self.assertNotIn("cookie", content.casefold())


class InvoiceFailureLogFieldTests(unittest.TestCase):
    """Bounded legacy enrichment plus the closed dual-stream summary fields."""

    def lines(self, logger: SafeLogger) -> list[dict]:
        return [json.loads(line) for line in logger.log_path.read_text(encoding="utf-8").splitlines()]

    def test_the_allowlist_is_closed_for_legacy_and_dual_stream_fields(self) -> None:
        self.assertEqual(
            cli.ALLOWED_LOG_FIELDS,
            {
                "inventory_count",
                "downloaded_count",
                "present_count",
                "failure_count",
                "attempt",
                "duration_ms",
                "support_ref",
                "row_ordinal",
                "preflight_reason",
                "preflight_checkpoint",
                "stream",
                "archive_reused_count",
                "archive_staged_count",
                "drive_staged_count",
                "delivered_count",
                "handled_count",
                "uncertain_count",
            },
        )

    def test_old_shape_events_are_written_exactly_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeLogger(Path(directory), "run-1")
            logger.event("invoice_failure", status="DOWNLOAD_FAILED")
            logger.event("inventory_complete", inventory_count=2)
            self.assertEqual(
                self.lines(logger),
                [
                    {"run_id": "run-1", "phase": "invoice_failure", "status": "DOWNLOAD_FAILED"},
                    {"run_id": "run-1", "phase": "inventory_complete", "inventory_count": 2},
                ],
            )

    def test_enriched_fields_are_written_and_private_fields_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeLogger(Path(directory), "run-2")
            logger.event(
                "invoice_failure",
                status="PORTAL_LAYOUT_CHANGED",
                row_ordinal=3,
                preflight_reason="WITNESS_ABSENT",
                preflight_checkpoint="ACCOUNT_WITNESS",
                filename="2026-PRIVATE.pdf",
                row_text="PRIVATE ROW",
                url="https://portal.example.invalid",
            )
            content = logger.log_path.read_text(encoding="utf-8")
            self.assertEqual(
                self.lines(logger),
                [
                    {
                        "run_id": "run-2",
                        "phase": "invoice_failure",
                        "status": "PORTAL_LAYOUT_CHANGED",
                        "row_ordinal": 3,
                        "preflight_reason": "WITNESS_ABSENT",
                        "preflight_checkpoint": "ACCOUNT_WITNESS",
                    }
                ],
            )
            for private in ("PRIVATE", "portal.example"):
                self.assertNotIn(private, content)

    def test_a_reconciled_preflight_failure_reaches_the_jsonl_closed_and_parseable(self) -> None:
        class PreflightFailingPortal:
            def inventory(self, safety_ceiling):
                return [portal_module.InvoiceRow(ordinal=index, binding=None) for index in range(2)]

            def download(self, row, destination):
                evidence = portal_module.DownloadPreflightEvidence(
                    row_ordinal=row.ordinal,
                    reason_code="SNAPSHOT_MISMATCH",
                    last_checkpoint="SNAPSHOT_COMPARE",
                    window_expired=False,
                    not_ready_looks=0,
                    elapsed_bucket="LT_250MS",
                )
                raise portal_module.DownloadPreflightError(portal_module.RESULTS_SURFACE_CHANGED_MESSAGE, evidence)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "archive").mkdir()
            config = RuntimeConfig(
                portal_url="http://127.0.0.1:1",
                archive_root=root / "archive",
                state_path=root / "state" / "state.sqlite3",
                temp_root=root / "temp",
                log_root=root / "logs",
            )
            config.preflight()
            logger = SafeLogger(config.log_root, "run-3")
            with StateStore(config.state_path) as state:
                summary = reconcile_inventory(config, PreflightFailingPortal(), state, logger, "run-3")
            events = self.lines(logger)
        self.assertEqual(summary.status, "PORTAL_LAYOUT_CHANGED")
        failures = [event for event in events if event["phase"] == "invoice_failure"]
        self.assertEqual(
            failures,
            [
                {
                    "run_id": "run-3",
                    "phase": "invoice_failure",
                    "status": "PORTAL_LAYOUT_CHANGED",
                    "row_ordinal": 0,
                    "preflight_reason": "SNAPSHOT_MISMATCH",
                    "preflight_checkpoint": "SNAPSHOT_COMPARE",
                }
            ],
        )
        for event in events:
            self.assertTrue(set(event) <= {"run_id", "phase", "status"} | cli.ALLOWED_LOG_FIELDS)

    def test_dual_stream_logs_keep_only_closed_streams_and_bounded_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeLogger(Path(directory), "run-4")
            logger.event(
                "stream_complete",
                status="DELIVERED",
                stream="EB_BILL",
                inventory_count=1,
                delivered_count=1,
                handled_count=1,
                drive_staged_count=MAX_INVENTORY_CEILING + 1,
                tenant_id="PRIVATE-TENANT",
                source_filename="PRIVATE.pdf",
            )
            event = self.lines(logger)[0]
        self.assertEqual("EB_BILL", event["stream"])
        self.assertEqual(1, event["delivered_count"])
        self.assertNotIn("drive_staged_count", event)
        self.assertNotIn("tenant_id", event)
        self.assertNotIn("source_filename", event)

    def test_stage_event_family_uses_only_safe_fields(self) -> None:
        phases = (
            "inventory_started", "inventory_result", "latest_selection", "selection_committed",
            "fetch_decision", "fetch_started", "fetch_completed", "archive_started", "archive_result",
            "drive_started", "drive_result", "delivery_intent", "dispatch_start", "delivery_no_send",
            "delivery_outcome", "stream_complete",
        )
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeLogger(Path(directory), "run-stage-family")
            for phase in phases:
                logger.event(
                    phase,
                    status="OBSERVED",
                    stream="EB_BILL",
                    inventory_count=2,
                    support_ref="EG_STAGE_TEST",
                    source_namespace="PRIVATE-NAMESPACE-CANARY",
                    source_key="PRIVATE-SOURCE-KEY-CANARY",
                    filename="PRIVATE-SOURCE-FILENAME-CANARY.pdf",
                    path="C:/PRIVATE/PATH-CANARY",
                    url="https://private.invalid/URL-CANARY",
                    recipient="PRIVATE-RECIPIENT-CANARY",
                    delivery_id="PRIVATE-DELIVERY-ID-CANARY",
                    operation_id="PRIVATE-OPERATION-ID-CANARY",
                    pdf_content="PRIVATE-PDF-CANARY",
                )
            rendered = logger.log_path.read_text(encoding="utf-8")
            events = self.lines(logger)
        self.assertEqual(list(phases), [event["phase"] for event in events])
        self.assertTrue(all(event["stream"] == "EB_BILL" and event["support_ref"] == "EG_STAGE_TEST" for event in events))
        self.assertTrue(all(set(event) <= {"run_id", "phase", "status"} | cli.ALLOWED_LOG_FIELDS for event in events))
        for private in (
            "PRIVATE-NAMESPACE-CANARY", "PRIVATE-SOURCE-KEY-CANARY", "PRIVATE-SOURCE-FILENAME-CANARY",
            "PRIVATE/PATH-CANARY", "URL-CANARY", "PRIVATE-RECIPIENT-CANARY",
            "PRIVATE-DELIVERY-ID-CANARY", "PRIVATE-OPERATION-ID-CANARY", "PRIVATE-PDF-CANARY",
        ):
            self.assertNotIn(private, rendered)


if __name__ == "__main__":
    unittest.main()
