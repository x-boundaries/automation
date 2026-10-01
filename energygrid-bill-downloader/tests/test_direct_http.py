"""Direct-HTTP MVP source, reconciliation, lock and alert (DL-XB-199 G3-101).

Every test runs against the loopback synthetic bill service. No real endpoint,
tenant identifier, credential or bill is used; the canary values the fixture
plants are searched for on every output surface.
"""

from __future__ import annotations

import contextlib
import builtins
import io
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from energygrid_bill_downloader import cli, http_source, reconcile
from energygrid_bill_downloader.config import (
    DirectHttpSettings,
    load_runtime_config,
)
from energygrid_bill_downloader.errors import ConfigError, SourceContractError, SourceTransportError
from energygrid_bill_downloader.http_source import DirectHttpSource, ListedBill
from energygrid_bill_downloader.notify import ALERT_KEYS, build_alert_payload
from energygrid_bill_downloader.publication import filename_key, validate_pdf
from energygrid_bill_downloader.run_lock import LOCK_FILENAME, RunLock
from energygrid_bill_downloader.state import StateStore
from tests.fixtures.synthetic_http_source import (
    CANARY_FETCH_PATH,
    CANARY_FILENAME_MARK,
    CANARY_LIST_PATH,
    CANARY_OTHER_TENANT,
    CANARY_TENANT,
    AlertSink,
    ServiceState,
    SyntheticBillService,
    bill_name,
    close_delimited,
    default_files,
    drop_connection,
    json_document,
    pdf_bytes,
    raw_body,
    raw_response,
    short_body,
    slow,
    status,
)
from tests.fixtures.synthetic_portal import synthetic_pdf
from tests.test_state import FIXTURE_SCHEMA, create_state_fixture, state_snapshot


CANARIES = (CANARY_TENANT, CANARY_OTHER_TENANT, CANARY_LIST_PATH, CANARY_FETCH_PATH, CANARY_FILENAME_MARK, "canary-private")


def setUpModule() -> None:
    # Retries are real, but the back-off between them is not worth waiting for.
    patcher = mock.patch.object(http_source, "RETRY_DELAY_SECONDS", 0.0)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def chunked_body(payload: bytes) -> bytes:
    return f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n0\r\n\r\n"


def exact_wire(wire: bytes, *, fragmented: bool = False):
    """Transport-only fixture: no production admission code decides outcomes."""
    def send(handler):
        try:
            for part in (bytes([byte]) for byte in wire) if fragmented else (wire,):
                handler.wfile.write(part)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Refusing a malformed header can close before all bytes arrive.
        finally:
            handler.close_connection = True
    return send


def chunked_wire(
    payload: bytes,
    *,
    first_delimiter: bytes = b"\r\n",
    trailer_fields: bytes = b"",
    trailer_terminator: bytes = b"\r\n",
    first_extension: bytes = b"",
) -> bytes:
    split = max(1, len(payload) // 2)
    if split == len(payload):
        split -= 1
    first = payload[:split]
    second = payload[split:]
    return (
        f"{len(first):X}".encode("ascii")
        + first_extension
        + b"\r\n"
        + first
        + first_delimiter
        + f"{len(second):X}\r\n".encode("ascii")
        + second
        + b"\r\n0\r\n"
        + trailer_fields
        + trailer_terminator
    )


class Deployment:
    """One private deployment: archive, state, temp, log roots and a config."""

    def __init__(self, tmp: Path, service: SyntheticBillService, **overrides) -> None:
        tmp = tmp.resolve()
        self.tmp = tmp
        self.archive = tmp / "archive"
        self.archive.mkdir()
        self.state_path = tmp / "state" / "state.sqlite3"
        self.temp_root = tmp / "temp"
        self.log_root = tmp / "logs"
        raw = {
            "source": "direct_http",
            "direct_http": {
                "list_url": service.list_url,
                "fetch_url": service.fetch_url,
                "tenant_id": CANARY_TENANT,
            },
            "archive_root": str(self.archive),
            "state_path": str(self.state_path),
            "temp_root": str(self.temp_root),
            "log_root": str(self.log_root),
            "timeout_seconds": 5,
            "max_attempts": 2,
            "inventory_safety_ceiling": 1000,
        }
        raw.update(overrides)
        self.raw = raw
        self.config_path = tmp / "private.json"
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")

    def run(self, command: str = "run") -> tuple[int, dict]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main([command, "--config", str(self.config_path)])
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        self.last_stdout = stdout.getvalue()
        return code, json.loads(lines[-1])

    def archived(self) -> list[str]:
        return sorted(path.name for path in self.archive.iterdir())

    def state_rows(self) -> list:
        if not self.state_path.exists():
            return []
        with StateStore(self.state_path) as state:
            return list(state.records())

    def log_text(self) -> str:
        if not self.log_root.exists():
            return ""
        return "".join(path.read_text(encoding="utf-8") for path in sorted(self.log_root.glob("*.jsonl")))

    def log_events(self) -> list[dict]:
        return [json.loads(line) for line in self.log_text().splitlines() if line.strip()]

    def temp_entries(self) -> list[str]:
        return sorted(path.name for path in self.temp_root.iterdir()) if self.temp_root.exists() else []


class DirectHttpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.service_state = ServiceState()
        self.service = SyntheticBillService(self.service_state)
        self.service.__enter__()

    def tearDown(self) -> None:
        self.service.__exit__(None, None, None)
        self._tmp.cleanup()

    def deployment(self, **overrides) -> Deployment:
        return Deployment(self.tmp, self.service, **overrides)

    def assert_no_canary(self, *texts: str) -> None:
        for text in texts:
            for canary in CANARIES:
                self.assertNotIn(canary, text, "a private value reached an output surface")

    def assert_fail_closed(self, deployment: Deployment, code: int, document: dict, support_ref: str, exit_code: int = 20) -> None:
        self.assertEqual(exit_code, code, document)
        self.assertEqual(support_ref, document.get("support_ref"), document)
        self.assertEqual([], deployment.archived(), "nothing may be published")
        self.assertEqual([], deployment.state_rows(), "nothing may be recorded")
        self.assertEqual([], deployment.temp_entries(), "no temp residue may remain")
        failed = [event for event in deployment.log_events() if event["phase"] == "run_failed"]
        self.assertEqual(1, len(failed))
        self.assertEqual(support_ref, failed[0]["support_ref"])
        self.assert_no_canary(deployment.last_stdout, deployment.log_text())


# --------------------------------------------------------------------------
# Positive controls
# --------------------------------------------------------------------------


class PositiveControls(DirectHttpCase):
    def test_normal_four_row_inventory_publishes_every_bill_once(self) -> None:
        deployment = self.deployment()
        code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual("DOWNLOADED", document["status"])
        self.assertEqual(4, document["inventory_count"])
        self.assertEqual(4, document["downloaded_count"])
        self.assertEqual([bill_name(index) for index in range(4)], deployment.archived())
        for index in range(4):
            self.assertEqual(
                synthetic_pdf(bill_name(index).encode("ascii")),
                (deployment.archive / bill_name(index)).read_bytes(),
            )
        rows = deployment.state_rows()
        self.assertEqual({"ARCHIVED"}, {row.status for row in rows})
        self.assertEqual(1, len(self.service_state.list_requests))
        self.assertEqual(4, self.service_state.fetch_count)
        self.assertEqual([], deployment.temp_entries())
        self.assert_no_canary(deployment.last_stdout, deployment.log_text())

    def test_request_bodies_carry_only_the_configured_tenant_and_listed_filename(self) -> None:
        deployment = self.deployment()
        deployment.run()
        self.assertEqual([{"tenant_id": CANARY_TENANT}], self.service_state.list_requests)
        self.assertEqual(
            [{"filename": bill_name(index), "tenant_id": CANARY_TENANT} for index in range(4)],
            self.service_state.fetch_requests,
        )
        # No enumeration, discovery or any other operation is ever attempted.
        self.assertEqual([], self.service_state.other_requests)

    def test_immediate_rerun_is_one_list_zero_fetch_zero_publication(self) -> None:
        deployment = self.deployment()
        self.assertEqual(0, deployment.run()[0])
        before = {name: (deployment.archive / name).stat().st_mtime_ns for name in deployment.archived()}
        list_before = len(self.service_state.list_requests)
        fetch_before = self.service_state.fetch_count

        code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual("ALREADY_PRESENT", document["status"])
        self.assertEqual(4, document["present_count"])
        self.assertEqual(0, document["downloaded_count"])
        self.assertEqual(list_before + 1, len(self.service_state.list_requests))
        self.assertEqual(fetch_before, self.service_state.fetch_count, "no FETCH for archived bills")
        after = {name: (deployment.archive / name).stat().st_mtime_ns for name in deployment.archived()}
        self.assertEqual(before, after, "no archive file is rewritten")
        self.assertEqual({"ARCHIVED"}, {row.status for row in deployment.state_rows()})

    def test_one_new_bill_fetches_and_publishes_only_that_bill(self) -> None:
        deployment = self.deployment()
        deployment.run()
        self.service_state.files.append({"date": "2026-05-01", "filename": bill_name(4), "tenant_id": CANARY_TENANT})
        fetch_before = self.service_state.fetch_count
        code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual("DOWNLOADED", document["status"])
        self.assertEqual(1, document["downloaded_count"])
        self.assertEqual(4, document["present_count"])
        self.assertEqual(fetch_before + 1, self.service_state.fetch_count)
        self.assertEqual({"filename": bill_name(4), "tenant_id": CANARY_TENANT}, self.service_state.fetch_requests[-1])
        self.assertEqual(5, len(deployment.archived()))

    def test_transient_list_failure_then_success(self) -> None:
        self.service_state.list_queue = [status(503)]
        deployment = self.deployment()
        code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual(2, len(self.service_state.list_requests))
        self.assertEqual(4, len(deployment.archived()))

    def test_retryable_fetch_statuses_then_success_publish_exactly_once(self) -> None:
        for code_value in (429, 500, 502, 503, 504):
            with self.subTest(status=code_value):
                self.tearDown()
                self.setUp()
                self.service_state.fetch_queue = {bill_name(1): [status(code_value)]}
                deployment = self.deployment()
                code, document = deployment.run()
                self.assertEqual(0, code, document)
                self.assertEqual(5, self.service_state.fetch_count)
                self.assertEqual(4, len(deployment.archived()))
                self.assertEqual(4, len(deployment.state_rows()))

    def test_connection_drop_then_success_is_retried(self) -> None:
        self.service_state.fetch_queue = {bill_name(0): [drop_connection()]}
        deployment = self.deployment()
        code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual(4, len(deployment.archived()))

    def test_list_command_reports_pending_without_fetch_or_write(self) -> None:
        deployment = self.deployment()
        create_state_fixture(deployment.state_path)
        before = deployment.state_path.read_bytes()
        code, document = deployment.run("list")
        self.assertEqual(20, code)
        self.assertEqual("ACTION_REQUIRED", document["status"])
        self.assertEqual(4, document["pending_count"])
        self.assertEqual(0, self.service_state.fetch_count)
        self.assertEqual([], deployment.archived())
        self.assertEqual(before, deployment.state_path.read_bytes())
        deployment.run()
        code, document = deployment.run("list")
        self.assertEqual(0, code)
        self.assertEqual(0, document["pending_count"])

    def test_preexisting_archive_file_is_reconciled_without_fetch(self) -> None:
        deployment = self.deployment()
        (deployment.archive / bill_name(2)).write_bytes(synthetic_pdf(b"operator-copy"))
        code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual(3, document["downloaded_count"])
        self.assertEqual(1, document["present_count"])
        self.assertNotIn(bill_name(2), [request["filename"] for request in self.service_state.fetch_requests])
        statuses = {row.portal_filename: row.status for row in deployment.state_rows()}
        self.assertEqual("PRESENT_RECONCILED", statuses[bill_name(2)])


# --------------------------------------------------------------------------
# LIST contract negatives: every one fails closed before any FETCH
# --------------------------------------------------------------------------


class ListContractNegatives(DirectHttpCase):
    def run_list_case(self, behaviour, support_ref: str, exit_code: int = 20, **overrides) -> Deployment:
        if behaviour is not None:
            self.service_state.list_queue = [behaviour]
        deployment = self.deployment(**overrides)
        code, document = deployment.run()
        self.assert_fail_closed(deployment, code, document, support_ref, exit_code)
        self.assertEqual(0, self.service_state.fetch_count, "no FETCH after a LIST failure")
        return deployment

    def test_message_only_invalid_tenant_shape(self) -> None:
        self.run_list_case(json_document({"message": "Invalid tenant"}), "EG_HTTP_LIST_REJECTED")

    def test_configured_tenant_unknown_to_the_service_is_rejected(self) -> None:
        self.service_state.tenant = CANARY_OTHER_TENANT
        self.run_list_case(None, "EG_HTTP_LIST_REJECTED")

    def test_success_false(self) -> None:
        self.run_list_case(json_document({"success": False, "files": []}), "EG_HTTP_LIST_NOT_SUCCESS")

    def test_success_must_be_boolean_true(self) -> None:
        for value in ("true", 1, None):
            with self.subTest(value=value):
                self.tearDown()
                self.setUp()
                self.run_list_case(json_document({"success": value, "files": default_files()}), "EG_HTTP_LIST_SCHEMA_DRIFT")

    def test_top_level_schema_drift(self) -> None:
        cases = [
            {"success": True, "files": default_files(), "total": 4},
            {"success": True},
            {"files": default_files()},
            {"success": True, "files": {"0": default_files()[0]}},
            [default_files()],
            {"success": True, "files": default_files(), "next": "cursor"},
        ]
        for document in cases:
            with self.subTest(document=str(document)[:40]):
                self.tearDown()
                self.setUp()
                self.run_list_case(json_document(document), "EG_HTTP_LIST_SCHEMA_DRIFT")

    def test_item_schema_drift(self) -> None:
        good = default_files(2)
        drifted = [
            {**good[0], "url": "x"},
            {"date": good[0]["date"], "filename": good[0]["filename"]},
            {**good[0], "date": 20260101},
            {**good[0], "filename": None},
            "a-string-row",
        ]
        for row in drifted:
            with self.subTest(row=str(row)[:40]):
                self.tearDown()
                self.setUp()
                self.run_list_case(
                    json_document({"success": True, "files": [row, good[1]]}), "EG_HTTP_LIST_ROW_SCHEMA_DRIFT"
                )

    def test_wrong_tenant_on_any_row(self) -> None:
        for mutated in (CANARY_OTHER_TENANT, CANARY_TENANT.lower(), CANARY_TENANT + " ", ""):
            with self.subTest(mutated=repr(mutated)):
                self.tearDown()
                self.setUp()
                files = default_files()
                files[3]["tenant_id"] = mutated
                self.run_list_case(json_document({"success": True, "files": files}), "EG_HTTP_LIST_TENANT_MISMATCH")

    def test_duplicate_normalised_filename(self) -> None:
        files = default_files(3)
        files[2]["filename"] = files[0]["filename"].upper().replace(".PDF", ".pdf")
        self.run_list_case(json_document({"success": True, "files": files}), "EG_INVENTORY_DUPLICATE_FILENAME")

    def test_unsafe_filename(self) -> None:
        for name in (
            "..\\escape.pdf",
            "../escape.pdf",
            "CON.pdf",
            "CON.extra.pdf",
            "NUL.extra.pdf",
            "COM1.extra.pdf",
            "COM¹.pdf",
            "LPT².pdf",
            "con.Extra.PDF",
            "nUl.extra.pdf",
            "com1.EXTRA.PDF",
            "lPt².PdF",
            "CONIN$.extra.pdf",
            "conout$.PDF",
            "CON .extra.pdf",
            "bill.exe",
            "bill.pdf.",
            "a:b.pdf",
            "",
            "bill\x01.pdf",
        ):
            with self.subTest(name=repr(name)):
                self.tearDown()
                self.setUp()
                files = default_files(2)
                files[1]["filename"] = name
                self.run_list_case(json_document({"success": True, "files": files}), "EG_INVENTORY_FILENAME_UNSAFE")

    def test_reserved_name_in_mixed_inventory_fails_in_phase_zero_without_state_writes(self) -> None:
        files = default_files(3)
        files[1]["filename"] = "CON.extra.pdf"
        self.service_state.files = files
        state_writes: list[str] = []

        def write_spy(name: str):
            original = getattr(StateStore, name)

            def record_write(state, *args, **kwargs):
                state_writes.append(name)
                return original(state, *args, **kwargs)

            return record_write

        deployment = self.deployment()
        with contextlib.ExitStack() as stack:
            for method_name in ("mark_seen", "record_archived", "record_failure"):
                stack.enter_context(mock.patch.object(StateStore, method_name, new=write_spy(method_name)))
            code, document = deployment.run()

        self.assert_fail_closed(deployment, code, document, "EG_INVENTORY_FILENAME_UNSAFE")
        self.assertEqual(1, len(self.service_state.list_requests))
        self.assertEqual(0, len(self.service_state.fetch_requests))
        self.assertEqual([], state_writes)

    def test_empty_inventory(self) -> None:
        self.run_list_case(json_document({"success": True, "files": []}), "EG_HTTP_LIST_EMPTY")

    def test_ceiling_exceeded(self) -> None:
        self.run_list_case(None, "EG_HTTP_LIST_CEILING", inventory_safety_ceiling=3)

    def test_malformed_json(self) -> None:
        for payload in (b"{not json", b"", b'{"success": true, "files": NaN}', b'{"success": true, "success": true, "files": []}', b"\xff\xfe"):
            with self.subTest(payload=payload[:20]):
                self.tearDown()
                self.setUp()
                self.run_list_case(raw_body(payload), "EG_HTTP_LIST_MALFORMED_JSON")

    def test_non_json_content_type(self) -> None:
        self.run_list_case(json_document({"success": True, "files": default_files()}, content_type="text/html"), "EG_HTTP_LIST_CONTENT_TYPE")

    def test_redirect_is_refused_and_never_followed(self) -> None:
        for code_value in (301, 302, 303, 307, 308):
            with self.subTest(status=code_value):
                self.tearDown()
                self.setUp()
                self.run_list_case(status(code_value, location=self.service.base + "/elsewhere"), "EG_HTTP_REDIRECT_REFUSED")
                self.assertEqual(1, len(self.service_state.list_requests), "3xx is never retried")
                self.assertEqual([], self.service_state.other_requests, "the redirect target is never contacted")

    def test_client_errors_are_terminal_and_not_retried(self) -> None:
        for code_value in (400, 401, 403, 404, 409, 422):
            with self.subTest(status=code_value):
                self.tearDown()
                self.setUp()
                self.run_list_case(status(code_value), "EG_HTTP_STATUS_UNEXPECTED")
                self.assertEqual(1, len(self.service_state.list_requests))

    def test_unselected_5xx_and_non_200_success_codes_are_terminal(self) -> None:
        for code_value in (501, 505, 201, 204):
            with self.subTest(status=code_value):
                self.tearDown()
                self.setUp()
                self.run_list_case(status(code_value), "EG_HTTP_STATUS_UNEXPECTED")
                self.assertEqual(1, len(self.service_state.list_requests))

    def test_retry_exhaustion(self) -> None:
        self.service_state.list_queue = [status(503), status(503), status(503)]
        deployment = self.deployment()
        code, document = deployment.run()
        self.assert_fail_closed(deployment, code, document, "EG_HTTP_TRANSPORT_EXHAUSTED", exit_code=10)
        self.assertEqual(2, len(self.service_state.list_requests), "attempts are bounded by max_attempts")

    def test_list_truncated_transfer(self) -> None:
        self.run_list_case(short_body(b'{"success": true', 200, "application/json"), "EG_HTTP_FRAMING_INCOMPLETE")

    def test_known_archived_bill_disappearing_fails_closed_before_fetch(self) -> None:
        deployment = self.deployment()
        self.assertEqual(0, deployment.run()[0])
        rows_before = deployment.state_rows()
        del self.service_state.files[1]
        self.service_state.files.append({"date": "2026-09-01", "filename": bill_name(9), "tenant_id": CANARY_TENANT})
        fetch_before = self.service_state.fetch_count
        code, document = deployment.run()
        self.assertEqual(20, code)
        self.assertEqual("EG_INVENTORY_KNOWN_BILL_MISSING", document["support_ref"])
        self.assertEqual(fetch_before, self.service_state.fetch_count)
        self.assertNotIn(bill_name(9), deployment.archived())
        self.assertEqual(
            [(row.filename_key, row.last_seen_at_utc) for row in rows_before],
            [(row.filename_key, row.last_seen_at_utc) for row in deployment.state_rows()],
            "a refused inventory writes no state",
        )


# --------------------------------------------------------------------------
# FETCH negatives
# --------------------------------------------------------------------------


class CompletenessAfterConflict(DirectHttpCase):
    def test_an_archived_bill_later_marked_conflict_still_may_not_disappear(self) -> None:
        deployment = self.deployment()
        self.assertEqual(0, deployment.run()[0])
        (deployment.archive / bill_name(1)).write_bytes(synthetic_pdf(b"tampered-bytes"))
        self.assertEqual(20, deployment.run()[0])
        statuses = {row.portal_filename: row.status for row in deployment.state_rows()}
        self.assertEqual("CONFLICT", statuses[bill_name(1)])
        del self.service_state.files[1]
        code, document = deployment.run()
        self.assertEqual(20, code)
        self.assertEqual("EG_INVENTORY_KNOWN_BILL_MISSING", document["support_ref"])


class ResultIsolation(DirectHttpCase):
    def test_a_failing_run_complete_log_write_does_not_change_a_success(self) -> None:
        deployment = self.deployment()
        original = cli.SafeLogger.event

        def failing(self, phase, status=None, **fields):
            if phase == "run_complete":
                raise OSError("disk full")
            return original(self, phase, status, **fields)

        with mock.patch.object(cli.SafeLogger, "event", failing):
            code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual("DOWNLOADED", document["status"])


class FetchNegatives(DirectHttpCase):
    def run_fetch_case(self, behaviours: list, status_value: str, exit_code: int, support_ref: str | None, row: int = 2) -> Deployment:
        self.service_state.fetch_queue = {bill_name(row): list(behaviours)}
        deployment = self.deployment()
        code, document = deployment.run()
        self.assertEqual(exit_code, code, document)
        self.assertEqual(status_value, document["status"])
        self.assertEqual([], deployment.archived(), "no partial publication")
        self.assertEqual([], deployment.state_rows(), "no partial state")
        self.assertEqual([], deployment.temp_entries())
        failures = [event for event in deployment.log_events() if event["phase"] == "invoice_failure"]
        self.assertEqual(1, len(failures))
        self.assertEqual(row, failures[0]["row_ordinal"])
        if support_ref is not None:
            self.assertEqual(support_ref, failures[0]["support_ref"])
        # Dispatch stops at the failing row: later rows are never fetched.
        fetched = [request["filename"] for request in self.service_state.fetch_requests]
        self.assertNotIn(bill_name(row + 1), fetched)
        self.assert_no_canary(deployment.last_stdout, deployment.log_text())
        return deployment

    def test_failure_mid_batch_publishes_nothing(self) -> None:
        self.run_fetch_case([status(404)], "PORTAL_LAYOUT_CHANGED", 20, "EG_HTTP_STATUS_UNEXPECTED")

    def test_fetch_redirect_refused(self) -> None:
        self.run_fetch_case([status(302, location=self.service.base + "/elsewhere")], "PORTAL_LAYOUT_CHANGED", 20, "EG_HTTP_REDIRECT_REFUSED")
        self.assertEqual([], self.service_state.other_requests)

    def test_fetch_retry_exhaustion(self) -> None:
        self.run_fetch_case([status(500), status(500)], "RETRYABLE_NETWORK_FAILURE", 10, "EG_HTTP_TRANSPORT_EXHAUSTED")
        self.assertEqual(2, [r["filename"] for r in self.service_state.fetch_requests].count(bill_name(2)))

    def test_empty_pdf(self) -> None:
        self.run_fetch_case([pdf_bytes(b"")], "PORTAL_LAYOUT_CHANGED", 20, "EG_HTTP_FETCH_EMPTY")

    def test_invalid_pdf(self) -> None:
        self.run_fetch_case([pdf_bytes(b"<html>not a pdf</html>")], "INVALID_PDF", 20, None)

    def test_truncated_pdf_without_eof_marker(self) -> None:
        self.run_fetch_case([pdf_bytes(synthetic_pdf()[:-8])], "INVALID_PDF", 20, None)

    def test_content_length_mismatch(self) -> None:
        payload = synthetic_pdf()
        self.run_fetch_case([short_body(payload[:40], len(payload))], "PORTAL_LAYOUT_CHANGED", 20, "EG_HTTP_FRAMING_INCOMPLETE")

    def test_close_delimited_body_is_unprovable(self) -> None:
        self.run_fetch_case([close_delimited(synthetic_pdf())], "PORTAL_LAYOUT_CHANGED", 20, "EG_HTTP_FRAMING_INCOMPLETE")

    def test_oversize_fetch(self) -> None:
        normal = len(synthetic_pdf(bill_name(0).encode("ascii")))
        big = synthetic_pdf(b"big") + b"%" * 64 + b"\n%%EOF\n"
        self.service_state.pdfs[bill_name(2)] = big
        with mock.patch.object(http_source, "MAX_PDF_BYTES", normal + 8):
            self.run_fetch_case([], "PORTAL_LAYOUT_CHANGED", 20, "EG_HTTP_FETCH_OVERSIZE")


class HttpFramingAdmission(DirectHttpCase):
    def _assert_framing_incomplete(self, stage: str, behaviour, *, failure_ordinal=0, rejected_body=None) -> None:
        list_before = len(self.service_state.list_requests)
        fetch_before = len(self.service_state.fetch_requests)
        with tempfile.TemporaryDirectory() as case_root:
            deployment = Deployment(Path(case_root), self.service)
            if stage == "list":
                self.service_state.list_queue = [behaviour]
                self.service_state.fetch_queue = {}
            else:
                self.service_state.list_queue = []
                self.service_state.fetch_queue = {bill_name(failure_ordinal): [behaviour]}
            with contextlib.ExitStack() as stack:
                guards = [
                    stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError(name)))
                    for owner, name in (
                        (StateStore, "mark_seen"), (StateStore, "record_archived"),
                        (StateStore, "record_failure"), (reconcile, "publish_no_replace"),
                    )
                ]
                if stage == "list":
                    guards.append(stack.enter_context(mock.patch.object(
                        reconcile, "create_run_directory", side_effect=AssertionError("acquisition"))))
                written = []
                real_open = Path.open
                binary_creates = []

                @contextlib.contextmanager
                def tracked_open(path, mode="r", *args, **kwargs):
                    with real_open(path, mode, *args, **kwargs) as stream:
                        if mode in ("xb", "wb", "ab"):
                            binary_creates.append(path)
                            writer = mock.Mock(wraps=stream)

                            def write(data):
                                written.append(bytes(data))
                                return stream.write(data)

                            writer.write.side_effect = write
                            yield writer
                        else:
                            yield stream

                stack.enter_context(mock.patch.object(Path, "open", tracked_open))
                code, document = deployment.run()
                for guard in guards:
                    guard.assert_not_called()
                if rejected_body is not None:
                    self.assertNotIn(rejected_body, written, "rejected bytes must never be written")
                self.assertEqual(0 if stage == "list" else failure_ordinal, len(binary_creates))

            self.assertEqual(20, code, document)
            self.assertEqual([], deployment.archived(), "framing failure must not publish")
            self.assertEqual([], deployment.state_rows(), "framing failure must not archive state")
            self.assertEqual([], deployment.temp_entries(), "framing failure must leave no temp residue")
            self.assert_no_canary(deployment.last_stdout, deployment.log_text())
            if stage == "list":
                self.assertEqual("EG_HTTP_FRAMING_INCOMPLETE", document.get("support_ref"), document)
                self.assertEqual(0, len(self.service_state.fetch_requests) - fetch_before)
            else:
                self.assertEqual("PORTAL_LAYOUT_CHANGED", document["status"])
                failures = [event for event in deployment.log_events() if event["phase"] == "invoice_failure"]
                self.assertEqual(1, len(failures))
                self.assertEqual("EG_HTTP_FRAMING_INCOMPLETE", failures[0].get("support_ref"))
                self.assertEqual(failure_ordinal + 1, len(self.service_state.fetch_requests) - fetch_before)
            self.assertEqual(1, len(self.service_state.list_requests) - list_before)

    def _assert_cases(self, stage: str, cases: list[tuple[str, object]]) -> None:
        for name, behaviour in cases:
            with self.subTest(stage=stage, case=name):
                self._assert_framing_incomplete(stage, behaviour)

    @staticmethod
    def _list_payload() -> bytes:
        return json.dumps({"success": True, "files": default_files()}).encode("utf-8")

    def _framing_cases(self, payload: bytes, content_type: str) -> dict[str, object]:
        content = [("Content-Type", content_type)]
        chunked = chunked_body(payload)
        length = str(len(payload))
        return {
            "notchunked": raw_response(content + [("Transfer-Encoding", "notchunked")], payload),
            "trailing vertical tab": raw_response(
                content + [("Transfer-Encoding", "chunked" + chr(11))], chunked
            ),
            "trailing form feed": raw_response(
                content + [("Transfer-Encoding", "chunked" + chr(12))], chunked
            ),
            "embedded whitespace": raw_response(
                content + [("Transfer-Encoding", "chun ked")], chunked
            ),
            "unsupported coding": raw_response(content + [("Transfer-Encoding", "gzip")], payload),
            "gzip then chunked": raw_response(content + [("Transfer-Encoding", "gzip, chunked")], chunked),
            "repeated transfer encoding": raw_response(
                content + [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")], chunked
            ),
            "conflicting duplicate content length": raw_response(
                content + [("Content-Length", length), ("Content-Length", str(len(payload) + 1))], payload
            ),
            "repeated content length": raw_response(
                content + [("Content-Length", length), ("Content-Length", length)], payload
            ),
            "comma combined content length": raw_response(
                content + [("Content-Length", f"{length}, {length}")], payload
            ),
            "mixed transfer encoding and content length": raw_response(
                content + [("Transfer-Encoding", "chunked"), ("Content-Length", length)], chunked
            ),
            "close delimited": close_delimited(payload, content_type),
            "truncated content length": short_body(payload[: max(1, len(payload) // 2)], len(payload), content_type),
            "malformed chunk size": raw_response(
                content + [("Transfer-Encoding", "chunked")], b"not-a-chunk-size\r\n"
            ),
            "incomplete chunk body": raw_response(
                content + [("Transfer-Encoding", "chunked")],
                f"{len(payload):X}\r\n".encode("ascii") + payload[:3],
            ),
        }

    def _strict_chunk_cases(self, payload: bytes, content_type: str) -> dict[str, object]:
        headers = [
            ("Content-Type", content_type),
            ("Transfer-Encoding", "chunked"),
        ]
        return {
            "wrong two-byte chunk delimiter": raw_response(
                headers,
                chunked_wire(payload, first_delimiter=b"XY"),
            ),
            "lone-LF chunk delimiter": raw_response(
                headers,
                chunked_wire(payload, first_delimiter=b"\n "),
            ),
            "zero chunk missing trailer terminator": raw_response(
                headers,
                chunked_wire(payload, trailer_terminator=b""),
            ),
            "trailer content missing terminator": raw_response(
                headers,
                chunked_wire(
                    payload,
                    trailer_fields=b"X-Synthetic: partial\r\n",
                    trailer_terminator=b"",
                ),
            ),
            "lone-LF trailer terminator": raw_response(
                headers,
                chunked_wire(
                    payload,
                    trailer_fields=b"X-Synthetic: value\n",
                    trailer_terminator=b"\n",
                ),
            ),
        }

    def _excessive_content_length(self, payload: bytes, content_type: str) -> object:
        get_digit_limit = getattr(sys, "get_int_max_str_digits", None)
        if get_digit_limit is None or get_digit_limit() == 0:
            self.skipTest("this Python runtime has no integer string conversion digit limit")
        length = "9" * (get_digit_limit() + 1)
        return raw_response(
            [("Content-Type", content_type), ("Content-Length", length)],
            payload,
        )

    def _non_ows_content_length_cases(self, payload: bytes, content_type: str) -> list[tuple[str, object]]:
        content = [("Content-Type", content_type)]
        length = str(len(payload))
        cases = []
        # These are non-OWS whitespace/control characters that Python's generic
        # str.strip() removes and that can be represented on the Latin-1 wire.
        for char in ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\xa0", "\r", "\n"):
            codepoint = f"U+{ord(char):04X}"
            for position, value in (("leading", char + length), ("trailing", length + char)):
                cases.append((
                    f"{position} {codepoint}",
                    raw_response(content + [("Content-Length", value)], payload),
                ))
        return cases

    def test_list_rejects_non_ows_content_length_whitespace_without_retry(self) -> None:
        self._assert_cases(
            "list",
            self._non_ows_content_length_cases(self._list_payload(), "application/json"),
        )

    def test_fetch_rejects_non_ows_content_length_whitespace_without_retry(self) -> None:
        self._assert_cases(
            "fetch",
            self._non_ows_content_length_cases(synthetic_pdf(b"framing-test"), "application/pdf"),
        )

    def test_parser_decoded_content_length_accepts_ascii_ows(self) -> None:
        lengths_and_payloads = (
            ("list", self._list_payload(), "application/json"),
            ("fetch", synthetic_pdf(bill_name(0).encode("ascii")), "application/pdf"),
        )
        for stage, payload, content_type in lengths_and_payloads:
            expected = str(len(payload))
            for value in (expected, f" {expected} ", f"\t{expected}\t"):
                with self.subTest(stage=stage, content_length=repr(value)):
                    list_before = len(self.service_state.list_requests)
                    fetch_before = len(self.service_state.fetch_requests)
                    behaviour = raw_response(
                        [("Content-Type", content_type), ("Content-Length", value)],
                        payload,
                    )
                    if stage == "list":
                        self.service_state.list_queue = [behaviour]
                        self.service_state.fetch_queue = {}
                    else:
                        self.service_state.list_queue = []
                        self.service_state.fetch_queue = {bill_name(0): [behaviour]}

                    with tempfile.TemporaryDirectory() as case_root:
                        deployment = Deployment(Path(case_root), self.service)
                        code, document = deployment.run()
                        self.assertEqual(0, code, document)
                        self.assertEqual(4, len(deployment.archived()))
                        self.assertEqual(4, len(deployment.state_rows()))
                        self.assertEqual([], deployment.temp_entries())
                    self.assertEqual(list_before + 1, len(self.service_state.list_requests))
                    self.assertEqual(fetch_before + 4, len(self.service_state.fetch_requests))

    def test_list_rejects_malformed_chunk_wire_delimiters_and_trailers(self) -> None:
        self._assert_cases(
            "list",
            list(self._strict_chunk_cases(self._list_payload(), "application/json").items()),
        )

    def test_fetch_rejects_malformed_chunk_wire_delimiters_and_trailers(self) -> None:
        self._assert_cases(
            "fetch",
            list(self._strict_chunk_cases(synthetic_pdf(b"framing-test"), "application/pdf").items()),
        )

    def test_list_rejects_excessive_content_length_without_retry(self) -> None:
        self._assert_framing_incomplete(
            "list",
            self._excessive_content_length(self._list_payload(), "application/json"),
        )

    def test_fetch_rejects_excessive_content_length_without_retry(self) -> None:
        self._assert_framing_incomplete(
            "fetch",
            self._excessive_content_length(synthetic_pdf(b"framing-test"), "application/pdf"),
        )

    def test_parser_decoded_chunked_response_accepts_valid_extensions_and_trailer(self) -> None:
        body = self._list_payload()
        self.service_state.list_queue = [
            raw_response(
                [("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")],
                chunked_wire(
                    body,
                    first_extension=b';name=token;quoted="a b"',
                    trailer_fields=b"X-Synthetic: accepted\r\n",
                ),
            )
        ]
        with tempfile.TemporaryDirectory() as case_root:
            deployment = Deployment(Path(case_root), self.service)
            code, document = deployment.run()
            self.assertEqual(0, code, document)
            self.assertEqual(4, len(deployment.archived()))
            self.assertEqual(1, len(self.service_state.list_requests))
            self.assertEqual(4, len(self.service_state.fetch_requests))

    def test_chunked_fetch_oversize_is_rejected_without_publication_or_state(self) -> None:
        payload = b"X" * 512
        wire = b"".join(b"1\r\nX\r\n" for _ in payload) + b"0\r\n\r\n"
        self.service_state.fetch_queue = {
            bill_name(0): [
                raw_response(
                    [("Content-Type", "application/pdf"), ("Transfer-Encoding", "chunked")],
                    wire,
                )
            ]
        }
        with tempfile.TemporaryDirectory() as case_root:
            deployment = Deployment(Path(case_root), self.service)
            with mock.patch.object(http_source, "MAX_PDF_BYTES", 64):
                code, document = deployment.run()

            self.assertEqual(20, code, document)
            failures = [event for event in deployment.log_events() if event["phase"] == "invoice_failure"]
            self.assertEqual(1, len(failures))
            self.assertEqual("EG_HTTP_FETCH_OVERSIZE", failures[0].get("support_ref"))
            self.assertEqual(1, len(self.service_state.list_requests))
            self.assertEqual(1, len(self.service_state.fetch_requests))
            self.assertEqual([], deployment.archived())
            self.assertEqual([], deployment.state_rows())
            self.assertEqual([], deployment.temp_entries())

    def test_list_rejects_unsupported_or_ambiguous_transfer_encoding(self) -> None:
        cases = self._framing_cases(self._list_payload(), "application/json")
        self._assert_cases("list", [(name, cases[name]) for name in (
            "notchunked",
            "trailing vertical tab",
            "trailing form feed",
            "embedded whitespace",
            "unsupported coding",
            "gzip then chunked",
            "repeated transfer encoding",
        )])

    def test_fetch_rejects_unsupported_or_ambiguous_transfer_encoding(self) -> None:
        cases = self._framing_cases(synthetic_pdf(b"framing-test"), "application/pdf")
        self._assert_cases("fetch", [(name, cases[name]) for name in (
            "notchunked",
            "trailing vertical tab",
            "trailing form feed",
            "embedded whitespace",
            "unsupported coding",
            "gzip then chunked",
            "repeated transfer encoding",
        )])

    def test_list_rejects_ambiguous_or_mixed_content_length(self) -> None:
        cases = self._framing_cases(self._list_payload(), "application/json")
        self._assert_cases("list", [(name, cases[name]) for name in (
            "conflicting duplicate content length",
            "repeated content length",
            "comma combined content length",
            "mixed transfer encoding and content length",
        )])

    def test_fetch_rejects_ambiguous_or_mixed_content_length(self) -> None:
        cases = self._framing_cases(synthetic_pdf(b"framing-test"), "application/pdf")
        self._assert_cases("fetch", [(name, cases[name]) for name in (
            "conflicting duplicate content length",
            "repeated content length",
            "comma combined content length",
            "mixed transfer encoding and content length",
        )])

    def test_list_rejects_close_delimited_and_incomplete_bodies(self) -> None:
        cases = self._framing_cases(self._list_payload(), "application/json")
        self._assert_cases("list", [(name, cases[name]) for name in (
            "close delimited",
            "truncated content length",
            "malformed chunk size",
            "incomplete chunk body",
        )])

    def test_fetch_rejects_close_delimited_and_incomplete_bodies(self) -> None:
        cases = self._framing_cases(synthetic_pdf(b"framing-test"), "application/pdf")
        self._assert_cases("fetch", [(name, cases[name]) for name in (
            "close delimited",
            "truncated content length",
            "malformed chunk size",
            "incomplete chunk body",
        )])

    def test_parser_decoded_chunked_list_is_accepted(self) -> None:
        body = self._list_payload()
        self.service_state.list_queue = [
            raw_response([("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")], chunked_body(body))
        ]
        with tempfile.TemporaryDirectory() as case_root:
            deployment = Deployment(Path(case_root), self.service)
            code, document = deployment.run()
            self.assertEqual(0, code, document)
            self.assertEqual(4, len(deployment.archived()))
            self.assertEqual(1, len(self.service_state.list_requests))
            self.assertEqual(4, len(self.service_state.fetch_requests))

    def test_parser_decoded_chunked_list_accepts_ascii_ows(self) -> None:
        body = self._list_payload()
        header_values = ("chunked", "chunked ", "chunked" + chr(9), chr(9) + "ChUnKeD " + chr(9))
        for header_value in header_values:
            with self.subTest(transfer_encoding=repr(header_value)):
                list_before = len(self.service_state.list_requests)
                fetch_before = len(self.service_state.fetch_requests)
                self.service_state.list_queue = [
                    raw_response(
                        [("Content-Type", "application/json"), ("Transfer-Encoding", header_value)],
                        chunked_body(body),
                    )
                ]
                with tempfile.TemporaryDirectory() as case_root:
                    deployment = Deployment(Path(case_root), self.service)
                    code, document = deployment.run()
                    self.assertEqual(0, code, document)
                    self.assertEqual(4, len(deployment.archived()))
                    self.assertEqual(4, len(deployment.state_rows()))
                    self.assertEqual([], deployment.temp_entries())
                self.assertEqual(list_before + 1, len(self.service_state.list_requests))
                self.assertEqual(fetch_before + 4, len(self.service_state.fetch_requests))

    def test_parser_decoded_chunked_fetch_accepts_ascii_ows(self) -> None:
        payload = synthetic_pdf(bill_name(0).encode("ascii"))
        header_values = ("chunked", "chunked ", "chunked" + chr(9), chr(9) + "ChUnKeD " + chr(9))
        for header_value in header_values:
            with self.subTest(transfer_encoding=repr(header_value)):
                fetch_before = len(self.service_state.fetch_requests)
                self.service_state.fetch_queue = {
                    bill_name(0): [
                        raw_response(
                            [("Content-Type", "application/pdf"), ("Transfer-Encoding", header_value)],
                            chunked_body(payload),
                        )
                    ]
                }
                with tempfile.TemporaryDirectory() as case_root:
                    deployment = Deployment(Path(case_root), self.service)
                    code, document = deployment.run()
                    self.assertEqual(0, code, document)
                    self.assertEqual(4, len(deployment.archived()))
                    self.assertEqual(4, len(deployment.state_rows()))
                    self.assertEqual([], deployment.temp_entries())
                self.assertEqual(fetch_before + 4, len(self.service_state.fetch_requests))

    def test_parser_decoded_chunked_fetch_is_accepted(self) -> None:
        payload = synthetic_pdf(bill_name(0).encode("ascii"))
        self.service_state.fetch_queue = {
            bill_name(0): [
                raw_response([("Content-Type", "application/pdf"), ("Transfer-Encoding", "chunked")], chunked_body(payload))
            ]
        }
        with tempfile.TemporaryDirectory() as case_root:
            deployment = Deployment(Path(case_root), self.service)
            code, document = deployment.run()
            self.assertEqual(0, code, document)
            self.assertEqual(4, len(deployment.archived()))
            self.assertEqual(4, len(deployment.state_rows()))
            self.assertEqual([], deployment.temp_entries())


    def test_whole_final_header_fixed_and_generated_rejections(self) -> None:
        # G4-106 exact witnesses plus independently constructed HTTP grammar
        # violations. Expected rejection comes from these fixed wire rules,
        # never from calling or importing the production validator.
        for stage in ("list", "fetch"):
            payload = self._list_payload() if stage == "list" else synthetic_pdf(b"rejected-final-header")
            length = str(len(payload)).encode("ascii")
            content_type = b"application/json" if stage == "list" else b"application/pdf"
            content = b"Content-Type: " + content_type + b"\r\n"
            cl = b"Content-Length: " + length + b"\r\n"
            te = b"Transfer-Encoding: chunked\r\n"
            cases = [
                ("G4-hidden-CL", b"X-Test: x\rContent-Length: " + length + b"\r\r\n", payload),
                ("G4-TE", b"Transfer-Encoding: chunked\r\r\n", chunked_body(payload)),
                ("G4-hidden-TE", b"X-Test: x\rTransfer-Encoding: chunked\r\r\n", chunked_body(payload)),
                ("bare-LF", cl + b"X-Test: x\n", payload),
                ("bare-CR", cl + b"X-Test: x\r", payload),
                ("CRCRLF", cl + b"X-Test: x\r\r\n", payload),
                ("folded-CL", cl + b"\tignored\r\n", payload),
                ("folded-TE", te + b" ignored\r\n", chunked_body(payload)),
                ("orphan", b"\tignored\r\n" + cl, payload),
                ("unrelated-fold", b"X-Test: x\r\n folded\r\n" + cl, payload),
                ("space-before-colon", b"Content-Length : " + length + b"\r\n", payload),
                ("colonless", b"Unrelated\r\n" + cl, payload),
                ("empty-name", b": x\r\n" + cl, payload),
                ("invalid-name", b"X(Test): x\r\n" + cl, payload),
                ("CL-TE", cl + te, chunked_body(payload)),
                ("TE-CL", te + cl, chunked_body(payload)),
                ("absent", b"X-Test: x\r\n", payload),
                ("equal-CL", cl + cl, payload),
                ("conflicting-CL", cl + b"Content-Length: 1\r\n", payload),
                ("equal-TE", te + te, chunked_body(payload)),
                ("conflicting-TE", te + b"Transfer-Encoding: gzip\r\n", chunked_body(payload)),
            ]
            for value in (b"", b"+1", b"-1", b"1.0", b"0x1", b"1 0", b"1\t0", b"1,1", b"\xb2"):
                cases.append(("numeric-" + repr(value), b"Content-Length: " + value + b"\r\n", payload))
            for value in (b"chunked,chunked", b"gzip, chunked", b"chunked; x=y", b"chunked gzip", b""):
                cases.append(("coding-" + repr(value), b"Transfer-Encoding: " + value + b"\r\n", chunked_body(payload)))
            # Mutate every forbidden control byte in an unrelated value/name.
            for byte in (*range(9), *range(10, 32), 127):
                for location in ("name", "value"):
                    field = b"X" + bytes([byte]) + b"-Test: x\r\n" if location == "name" else b"X-Test: x" + bytes([byte]) + b"y\r\n"
                    cases.append((f"control-{byte}-{location}", field + cl, payload))
            # Hidden framing must fail in first, middle and last positions,
            # regardless of whether the physical line starts with framing.
            for field, body in ((cl, payload), (te, chunked_body(payload))):
                for delimiter in (b"\r", b"\n", b"\r\r\n"):
                    hidden = b"X-Test: x" + delimiter + field
                    for position in range(3):
                        fields = [b"X-A: a\r\n", b"X-B: b\r\n"]
                        fields.insert(position, hidden)
                        cases.append((f"hidden-{field[:3]!r}-{delimiter!r}-{position}", b"".join(fields), body))
                for byte in (11, 12, 0x85, 0xa0):
                    name, value = field[:-2].split(b":", 1)
                    for changed in (bytes([byte]) + value, value + bytes([byte])):
                        cases.append((f"framing-ows-{byte}-{changed!r}", name + b":" + changed + b"\r\n", body))
            for label, fields, body in cases:
                with self.subTest(stage=stage, wire=label):
                    self._assert_framing_incomplete(
                        stage, exact_wire(b"HTTP/1.1 200 OK\r\n" + content + fields + b"\r\n" + body),
                        rejected_body=body,
                    )

    def test_final_terminators_eof_fragmentation_and_later_fetch_zero_effects(self) -> None:
        for stage in ("list", "fetch"):
            payload = self._list_payload() if stage == "list" else synthetic_pdf(b"rejected-fragmented")
            header = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n"
            for ending in (b"", b"\n", b"\r", b"\r\r\n", b" \r\n"):
                with self.subTest(stage=stage, terminator=ending):
                    # EOF alone is never an explicit empty CRLF.
                    self._assert_framing_incomplete(stage, exact_wire(header + ending), rejected_body=payload)
            for ordinal in (0, 2):
                with self.subTest(stage=stage, ordinal=ordinal):
                    self._assert_framing_incomplete(
                        stage,
                        exact_wire(header + b"X-Test: x\rTransfer-Encoding: chunked\r\r\n\r\n" + payload, fragmented=True),
                        failure_ordinal=ordinal if stage == "fetch" else 0, rejected_body=payload,
                    )

    def test_whole_final_header_positive_grammar_and_interim_controls(self) -> None:
        payload = self._list_payload()
        for chunked in (False, True):
            framing = b"tRaNsFeR-EnCoDiNg:\t ChUnKeD \t\r\n" if chunked else b"cOnTeNt-LeNgTh:\t 000" + str(len(payload)).encode() + b" \t\r\n"
            body = chunked_wire(payload, first_extension=b'; foo="x y"', trailer_fields=b"X-Trailer: \x85\xa0\r\n") if chunked else payload
            fields = [
                b"Content-Type: application/json\r\n",
                b"X-Repeat: first\r\n", b"X-Repeat:\t second \t\r\n",
                b"X-Opaque: " + bytes(range(128, 256)) + b"\r\n",
                b"X-Empty:\t \r\n",
            ]
            for position in (0, 2, len(fields)):
                for interim in (b"", b"HTTP/1.1\t100 Continue\r\nX-Test: x\rContent-Length: invalid\r\r\n\n"):
                    with self.subTest(chunked=chunked, position=position, interim=bool(interim)):
                        ordered = fields[:]
                        ordered.insert(position, framing)
                        self.service_state.list_queue = [exact_wire(
                            interim + b"HTTP/1.1\t200 OK\r\n" + b"".join(ordered) + b"\r\n" + body,
                            fragmented=True,
                        )]
                        source = DirectHttpSource(DirectHttpSettings(self.service.list_url, self.service.fetch_url, CANARY_TENANT), 5, 2)
                        self.assertEqual(4, len(source.inventory(1000)))
                        self.assertEqual(1, source.list_calls)

    def test_missing_admission_parsed_correspondence_and_decoder_tampering(self) -> None:
        base_begin = http.client.HTTPResponse.begin
        parse_headers = http.client.parse_headers
        mutations = {
            "added": lambda headers: headers.add_header("X-Added", "value"),
            "missing": lambda headers: headers.__delitem__("Content-Type"),
            "reordered": lambda headers: headers._headers.reverse(),
            "combined": lambda headers: headers.replace_header("Content-Type", "application/json, other"),
            "framing-value": lambda headers: headers.replace_header("Content-Length", "1"),
            "framing-cardinality": lambda headers: headers.add_header("Content-Length", headers["Content-Length"]),
        }
        with mock.patch.object(http_source._StrictFinalHeaderReader, "admit_final_headers", return_value=None):
            self._assert_framing_incomplete("list", raw_body(self._list_payload()))
        for label, mutate in mutations.items():
            def parse(fp, *args, **kwargs):
                headers = parse_headers(fp, *args, **kwargs)
                if isinstance(fp, http_source._StrictFinalHeaderReader):
                    mutate(headers)
                return headers
            with self.subTest(parsed=label), mock.patch.object(http.client, "parse_headers", parse):
                self._assert_framing_incomplete("list", raw_body(self._list_payload()))
        for chunked in (False, True):
            for attribute, value in (("chunked", None), ("length", 1), ("chunk_left", 3)):
                def begin(response):
                    base_begin(response)
                    setattr(response, attribute, value)
                with self.subTest(chunked=chunked, attribute=attribute), mock.patch.object(http.client.HTTPResponse, "begin", begin):
                    behaviour = raw_response([("Transfer-Encoding", "chunked")], chunked_body(self._list_payload())) if chunked else raw_body(self._list_payload())
                    self._assert_framing_incomplete("list", behaviour)

    def test_both_production_connection_classes_use_strict_response(self) -> None:
        self.assertIs(http_source._StrictHTTPConnection.response_class, http_source._StrictHTTPResponse)
        self.assertIs(http_source._StrictHTTPSConnection.response_class, http_source._StrictHTTPResponse)
        # Bypass only TLS encryption over the loopback fixture. urllib,
        # HTTPSConnection, HTTPResponse and the response parser remain real.
        context = mock.Mock()
        context.wrap_socket.side_effect = lambda sock, **kwargs: sock
        with mock.patch.object(http_source._StrictHTTPSHandler, "__init__", lambda handler: (
            http_source.urllib.request.HTTPSHandler.__init__(handler, context=context)
        )):
            settings = DirectHttpSettings(self.service.list_url.replace("http:", "https:"), self.service.fetch_url.replace("http:", "https:"), CANARY_TENANT)
            source = DirectHttpSource(settings, 5, 2)
            self.assertEqual(4, len(source.inventory(1000)))
            self.service_state.list_queue = [exact_wire(b"HTTP/1.1 200 OK\r\nX-Test: x\rContent-Length: 2\r\r\n\r\n{}")]
            with self.assertRaises(SourceContractError) as caught:
                source.inventory(1000)
            self.assertEqual("EG_HTTP_FRAMING_INCOMPLETE", caught.exception.support_ref)
            self.assertEqual(2, source.list_calls)
        self.assertEqual(2, context.wrap_socket.call_count)


    def test_additional_chunk_grammar_failures_and_list_ceiling(self) -> None:
        payload = self._list_payload()
        for body in (
            b"+2\r\n{}\r\n0\r\n\r\n", b"0x2\r\n{}\r\n0\r\n\r\n",
            b"2;bad=\"unterminated\r\n{}\r\n0\r\n\r\n",
            b"2;bad=\x00\r\n{}\r\n0\r\n\r\n", b"2\n{}\r\n0\r\n\r\n",
            b"2\r\n{}\r\n0\r\n folded\r\n\r\n",
            b"2\r\n{}\r\n0\r\nBad Name: x\r\n\r\n",
            b"2\r\n{}\r\n0\r\nX-Test: \x7f\r\n\r\n",
            b"2\r\n{}\r\n0\r\nContent-Length: 2\r\n\r\n",
            b"2\r\n{}\r\n0\r\nTransfer-Encoding: chunked\r\n\r\n",
        ):
            for stage in ("list", "fetch"):
                with self.subTest(stage=stage, body=body):
                    self._assert_framing_incomplete(stage, raw_response([("Transfer-Encoding", "chunked")], body))
        self.assertEqual(4 * 1024 * 1024, http_source.MAX_LIST_BYTES)
        self.assertEqual(32 * 1024 * 1024, http_source.MAX_PDF_BYTES)
        for behaviour in (
            raw_response([("Content-Length", str(http_source.MAX_LIST_BYTES + 1))], b""),
            raw_response([("Transfer-Encoding", "chunked")], chunked_body(b"x" * (http_source.MAX_LIST_BYTES + 1))),
        ):
            self.service_state.list_queue = [behaviour]
            source = DirectHttpSource(DirectHttpSettings(self.service.list_url, self.service.fetch_url, CANARY_TENANT), 5, 2)
            with self.assertRaises(SourceContractError) as caught:
                source.inventory(1000)
            self.assertEqual("EG_HTTP_LIST_OVERSIZE", caught.exception.support_ref)
            self.assertEqual(1, source.list_calls)


class ReadOnlyListCommand(DirectHttpCase):
    """Exercise real command entry with both durable and call-level oracles."""

    def assert_read_only_command(self, deployment, *, exit_code=20, status_name="ACTION_REQUIRED", list_calls=1, open_fault=False, sidecar_fault=False):
        # The documented lock exception can change its parent's entry/mtime.
        # Precreate the inert file so every other entry and directory mtime can
        # be compared exactly, including the state parent. Missing-parent cases
        # remain completely absent.
        if deployment.state_path.parent.is_dir():
            (deployment.state_path.parent / LOCK_FILENAME).touch(exist_ok=True)
        lock_metadata = frozenset({"state/" + LOCK_FILENAME})
        before = state_snapshot(deployment.tmp, metadata_only=lock_metadata)
        request_count = len(self.service_state.list_requests)
        fetch_count = len(self.service_state.fetch_requests)
        violations = []
        sql_violations = []
        real_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_mkdir = os.mkdir
        real_scandir = os.scandir
        real_connect = sqlite3.connect
        real_lstat = Path.lstat

        def log_path(path):
            return not isinstance(path, int) and Path(path).is_relative_to(deployment.log_root)

        def reject(label):
            violations.append(label)
            raise AssertionError(label)

        def guarded_open(original):
            def opened(path, mode="r", *args, **kwargs):
                if open_fault and not isinstance(path, int) and Path(path) == deployment.state_path:
                    raise PermissionError("synthetic unreadable database")
                if any(char in mode for char in "wax+") and not log_path(path):
                    reject("file write")
                return original(path, mode, *args, **kwargs)
            return opened

        def os_open(path, flags, *args, **kwargs):
            writable = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            allowed_lock = Path(path) == deployment.state_path.parent / LOCK_FILENAME and flags == os.O_RDWR | os.O_CREAT
            if writable and not allowed_lock and not log_path(path):
                reject("os.open write")
            return real_os_open(path, flags, *args, **kwargs)

        def mkdir(path, *args, **kwargs):
            if not log_path(path):
                reject("mkdir")
            return real_mkdir(path, *args, **kwargs)

        def scandir(path):
            if not isinstance(path, int) and Path(path).is_relative_to(deployment.temp_root):
                reject("temp traversal")
            return real_scandir(path)

        def lstat(path, *args, **kwargs):
            if sidecar_fault and path.name.endswith("-shm"):
                raise PermissionError("synthetic sidecar inspection error")
            return real_lstat(path, *args, **kwargs)

        def authorizer(action, arg1, arg2, _database, _trigger):
            if action in {
                sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
            } or (action == sqlite3.SQLITE_PRAGMA and arg2 is not None and
                  (arg1.lower(), arg2.lower()) not in {("query_only", "on"), ("temp_store", "memory"), ("table_xinfo", "bills")}):
                sql_violations.append((action, arg1, arg2))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def connect(database, **kwargs):
            if database != deployment.state_path.resolve().as_uri() + "?mode=ro" or kwargs.get("uri") is not True:
                reject("writable sqlite connection")
            connection = real_connect(database, **kwargs)
            connection.set_authorizer(authorizer)
            return connection

        with contextlib.ExitStack() as stack:
            guards = [
                stack.enter_context(mock.patch.object(owner, name, side_effect=lambda *a, _label=name, **k: reject(_label)))
                for owner, name in (
                    (cli, "cleanup_stale_owned_temp"), (cli, "notify_failure"),
                    (StateStore, "_initialize"), (StateStore, "_transaction"),
                    (StateStore, "mark_seen"), (StateStore, "record_archived"), (StateStore, "record_failure"),
                    (DirectHttpSource, "download"), (reconcile, "create_run_directory"),
                    (reconcile, "cleanup_run_directory"), (reconcile, "publish_no_replace"),
                    (os, "unlink"), (os, "remove"), (os, "rmdir"), (os, "rename"), (os, "replace"), (os, "link"),
                )
            ]
            stack.enter_context(mock.patch("builtins.open", guarded_open(real_open)))
            stack.enter_context(mock.patch.object(io, "open", guarded_open(real_io_open)))
            stack.enter_context(mock.patch.object(os, "open", os_open))
            stack.enter_context(mock.patch.object(os, "mkdir", mkdir))
            stack.enter_context(mock.patch.object(os, "scandir", scandir))
            stack.enter_context(mock.patch.object(Path, "lstat", lstat))
            stack.enter_context(mock.patch.object(sqlite3, "connect", connect))
            code, document = deployment.run("list")
            for guard in guards:
                guard.assert_not_called()
        self.assertEqual([], violations)
        self.assertEqual([], sql_violations)
        self.assertEqual(exit_code, code, document)
        self.assertEqual(status_name, document["status"], document)
        self.assertEqual(request_count + list_calls, len(self.service_state.list_requests))
        self.assertEqual(fetch_count, len(self.service_state.fetch_requests))
        after = state_snapshot(deployment.tmp, metadata_only=lock_metadata)
        # Only the approved log root and containing fixture directory's mtime
        # can change when the first log is created.
        def business(snapshot):
            return {key: value for key, value in snapshot.items() if key != "." and key != "logs" and not key.startswith("logs/")}
        self.assertEqual(business(before), business(after))
        self.assert_no_canary(deployment.last_stdout, deployment.log_text())
        return document

    def test_state_admission_matrix_at_command_entry(self) -> None:
        cases = ("missing-parent", "missing-db", "version-zero-empty", "old-schema", "newer",
                 "malformed", "view", "wrong-type", "corrupt", "wal-format", "-wal", "-shm", "-journal")
        for label in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                deployment = Deployment(Path(tmp), self.service)
                if label == "missing-db":
                    deployment.state_path.parent.mkdir()
                elif label != "missing-parent":
                    create_state_fixture(
                        deployment.state_path,
                        version=0 if label in ("version-zero-empty", "old-schema") else 2 if label == "newer" else 1,
                        schema="" if label == "version-zero-empty" else
                        "CREATE TABLE bills (filename_key TEXT PRIMARY KEY);" if label == "malformed" else
                        "CREATE VIEW bills AS SELECT 1 AS filename_key;" if label == "view" else
                        FIXTURE_SCHEMA.replace("byte_size INTEGER", "byte_size BLOB") if label == "wrong-type" else FIXTURE_SCHEMA,
                    )
                    if label == "corrupt":
                        deployment.state_path.write_bytes(b"corrupt")
                    elif label == "wal-format":
                        data = bytearray(deployment.state_path.read_bytes())
                        data[18:20] = b"\x02\x02"
                        deployment.state_path.write_bytes(data)
                    elif label in ("-wal", "-shm", "-journal"):
                        deployment.state_path.with_name(deployment.state_path.name + label).write_bytes(b"canary")
                self.assert_read_only_command(deployment, status_name="STATE_INCONSISTENT", list_calls=0)

    def test_unreadable_state_and_sidecar_inspection_errors_at_command_entry(self) -> None:
        deployment = self.deployment()
        create_state_fixture(deployment.state_path)
        self.assert_read_only_command(deployment, status_name="STATE_INCONSISTENT", list_calls=0, open_fault=True)
        self.assert_read_only_command(deployment, status_name="STATE_INCONSISTENT", list_calls=0, sidecar_fault=True)

    def test_current_empty_populated_pending_present_and_known_missing(self) -> None:
        for label in ("empty", "populated", "all-present", "known-missing"):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                deployment = Deployment(Path(tmp), self.service)
                create_state_fixture(deployment.state_path)
                if label != "empty":
                    name = "known-but-absent.pdf" if label == "known-missing" else bill_name(0)
                    connection = sqlite3.connect(deployment.state_path)
                    try:
                        connection.execute(
                            "INSERT INTO bills (filename_key, portal_filename, first_seen_at_utc, last_seen_at_utc, "
                            "status, archived_at_utc, sha256) VALUES (?, ?, 'synthetic', 'synthetic', 'ARCHIVED', 'synthetic', ?)",
                            (filename_key(name), name, "a" * 64),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                if label == "all-present":
                    for ordinal in range(4):
                        (deployment.archive / bill_name(ordinal)).write_bytes(synthetic_pdf(b"present"))
                document = self.assert_read_only_command(
                    deployment, exit_code=0 if label == "all-present" else 20,
                    status_name="NO_NEW_BILLS" if label == "all-present" else "PORTAL_LAYOUT_CHANGED" if label == "known-missing" else "ACTION_REQUIRED",
                )
                if label != "known-missing":
                    self.assertEqual(0 if label == "all-present" else 4, document["pending_count"])

    def test_stale_fresh_unowned_temp_and_missing_optional_directories(self) -> None:
        for temp_exists in (False, True):
            with self.subTest(temp_exists=temp_exists), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                deployment = Deployment(root, self.service, browser_cache_path=str(root / "browser-parent" / "cache"))
                create_state_fixture(deployment.state_path)
                if temp_exists:
                    for name, age in (
                        ("run-11111111-1111-4111-8111-111111111111", 172800),
                        ("run-22222222-2222-4222-8222-222222222222", 0),
                        ("unowned", 172800), ("run-reserved", 172800),
                    ):
                        path = deployment.temp_root / name
                        path.mkdir(parents=True)
                        (path / "canary").write_bytes(b"preserve every byte")
                        os.utime(path, (time.time() - age, time.time() - age))
                    (deployment.temp_root / "unowned.txt").write_bytes(b"preserve")
                self.assert_read_only_command(deployment)
                self.assertEqual(temp_exists, deployment.temp_root.exists())
                self.assertFalse((root / "browser-parent").exists())

    def test_failure_branches_never_notify_or_mutate(self) -> None:
        for label in ("framing", "transient", "planner", "unexpected"):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                deployment = Deployment(Path(tmp), self.service, alert={"url": "http://127.0.0.1:1/unused"})
                create_state_fixture(deployment.state_path)
                if label == "framing":
                    self.service_state.list_queue = [exact_wire(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\r\n\r\n{}")]
                elif label == "transient":
                    self.service_state.list_queue = [status(503), status(503)]
                elif label == "planner":
                    files = default_files()
                    files[0]["filename"] = "../unsafe.pdf"
                    self.service_state.list_queue = [json_document({"success": True, "files": files})]
                with contextlib.ExitStack() as stack:
                    if label == "unexpected":
                        stack.enter_context(mock.patch.object(cli, "reconcile_listed_inventory", side_effect=RuntimeError("private-canary")))
                    self.assert_read_only_command(
                        deployment, exit_code=10 if label == "transient" else 20,
                        status_name="RETRYABLE_NETWORK_FAILURE" if label == "transient" else "ACTION_REQUIRED" if label == "unexpected" else "PORTAL_LAYOUT_CHANGED",
                        list_calls=2 if label == "transient" else 0 if label == "unexpected" else 1,
                    )

    def test_lock_contention_and_process_death_release_at_command_entry(self) -> None:
        deployment = self.deployment()
        create_state_fixture(deployment.state_path)
        holder = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER, str(Path(__file__).resolve().parents[1]), str(deployment.state_path.parent)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual("held", holder.stdout.readline().strip())
            self.assert_read_only_command(deployment, exit_code=10, status_name="RUN_IN_PROGRESS", list_calls=0)
        finally:
            holder.kill()
            holder.wait(timeout=30)
            holder.stdout.close()
        self.assert_read_only_command(deployment)

    def test_normal_run_keeps_initialization_migration_and_owned_cleanup(self) -> None:
        for schema in (None, "", FIXTURE_SCHEMA):
            with self.subTest(schema=schema is None), tempfile.TemporaryDirectory() as tmp:
                deployment = Deployment(Path(tmp), self.service)
                if schema is not None:
                    create_state_fixture(deployment.state_path, version=0, schema=schema)
                stale = deployment.temp_root / "run-11111111-1111-4111-8111-111111111111"
                stale.mkdir(parents=True)
                (stale / "canary").write_bytes(b"stale owned")
                os.utime(stale, (time.time() - 172800, time.time() - 172800))
                unowned = deployment.temp_root / "run-reserved"
                unowned.mkdir()
                (unowned / "canary").write_bytes(b"unowned preserved")
                code, document = deployment.run()
                self.assertEqual(0, code, document)
                self.assertFalse(stale.exists())
                self.assertEqual(b"unowned preserved", (unowned / "canary").read_bytes())
                self.assertEqual(4, len(deployment.archived()))
                self.assertEqual(0, deployment.run()[0])


class ArchiveConflicts(DirectHttpCase):
    def test_same_name_different_bytes_in_archive_is_a_conflict_without_fetch(self) -> None:
        deployment = self.deployment()
        self.assertEqual(0, deployment.run()[0])
        target = deployment.archive / bill_name(1)
        target.write_bytes(synthetic_pdf(b"tampered-bytes"))
        fetch_before = self.service_state.fetch_count
        code, document = deployment.run()
        self.assertEqual(20, code)
        self.assertEqual("ARCHIVE_CONFLICT", document["status"])
        self.assertEqual(fetch_before, self.service_state.fetch_count)
        self.assertEqual(synthetic_pdf(b"tampered-bytes"), target.read_bytes(), "never overwritten")
        statuses = {row.portal_filename: row.status for row in deployment.state_rows()}
        self.assertEqual("CONFLICT", statuses[bill_name(1)])

    def test_repair_fetch_with_different_bytes_is_a_conflict_and_not_published(self) -> None:
        deployment = self.deployment()
        self.assertEqual(0, deployment.run()[0])
        (deployment.archive / bill_name(0)).unlink()
        self.service_state.pdfs[bill_name(0)] = synthetic_pdf(b"changed-upstream")
        code, document = deployment.run()
        self.assertEqual(20, code)
        self.assertEqual("ARCHIVE_CONFLICT", document["status"])
        self.assertNotIn(bill_name(0), deployment.archived())
        # The differing repair bytes are retained as evidence, as before.
        self.assertEqual(1, len(deployment.temp_entries()))

    def test_repair_fetch_with_identical_bytes_restores_the_file(self) -> None:
        deployment = self.deployment()
        self.assertEqual(0, deployment.run()[0])
        original = (deployment.archive / bill_name(3)).read_bytes()
        (deployment.archive / bill_name(3)).unlink()
        code, document = deployment.run()
        self.assertEqual(0, code, document)
        self.assertEqual(original, (deployment.archive / bill_name(3)).read_bytes())


# --------------------------------------------------------------------------
# Source-level transport behaviour
# --------------------------------------------------------------------------


class SourceTransport(DirectHttpCase):
    def source(self, timeout: float = 5, attempts: int = 2) -> DirectHttpSource:
        settings = DirectHttpSettings(list_url=self.service.list_url, fetch_url=self.service.fetch_url, tenant_id=CANARY_TENANT)
        return DirectHttpSource(settings, timeout, attempts, sleep=lambda _s: None)

    def test_timeout_is_retried_then_exhausted(self) -> None:
        self.service_state.list_queue = [slow(1.5), slow(1.5)]
        with self.assertRaises(SourceTransportError) as caught:
            self.source(timeout=0.3).inventory(1000)
        self.assertEqual("EG_HTTP_TRANSPORT_EXHAUSTED", caught.exception.support_ref)
        self.assertEqual(2, len(self.service_state.list_requests))

    def test_timeout_then_success(self) -> None:
        self.service_state.list_queue = [slow(1.5)]
        rows = self.source(timeout=0.3).inventory(1000)
        self.assertEqual(4, len(rows))

    def test_connection_refused_is_transient(self) -> None:
        settings = DirectHttpSettings(list_url="http://127.0.0.1:9/x", fetch_url="http://127.0.0.1:9/y", tenant_id=CANARY_TENANT)
        attempts: list[float] = []
        source = DirectHttpSource(settings, 1, 3, sleep=attempts.append)
        with self.assertRaises(SourceTransportError):
            source.inventory(1000)
        self.assertEqual(3, source.list_calls)
        self.assertEqual(2, len(attempts))

    def test_tls_failure_is_terminal_and_never_retried(self) -> None:
        settings = DirectHttpSettings(
            list_url=self.service.list_url.replace("http://", "https://"),
            fetch_url=self.service.fetch_url.replace("http://", "https://"),
            tenant_id=CANARY_TENANT,
        )
        source = DirectHttpSource(settings, 5, 3, sleep=lambda _s: None)
        with self.assertRaises(SourceContractError) as caught:
            source.inventory(1000)
        self.assertEqual("EG_HTTP_TLS_REFUSED", caught.exception.support_ref)
        self.assertEqual(1, source.list_calls)

    def test_rows_and_source_do_not_disclose_private_values_in_repr(self) -> None:
        source = self.source()
        rows = source.inventory(1000)
        self.assert_no_canary(repr(source), repr(rows), repr(source._settings))
        with self.assertRaises(SourceContractError) as caught:
            self.service_state.list_queue = [json_document({"message": CANARY_TENANT})]
            source.inventory(1000)
        self.assert_no_canary(str(caught.exception), repr(caught.exception))

    def test_ambient_proxy_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "NO_PROXY": ""}):
            rows = self.source(attempts=1).inventory(1000)
        self.assertEqual(4, len(rows))

    def test_download_refuses_a_row_it_did_not_issue(self) -> None:
        with self.assertRaises(TypeError):
            self.source().download(object(), self.tmp / "x.bin")

    def test_ceiling_equal_to_count_is_accepted(self) -> None:
        self.assertEqual(4, len(self.source().inventory(4)))


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------


class Alerting(DirectHttpCase):
    def test_terminal_failure_sends_one_privacy_minimal_alert(self) -> None:
        with AlertSink() as sink:
            self.service_state.list_queue = [json_document({"success": True, "files": default_files(2, CANARY_OTHER_TENANT)})]
            deployment = self.deployment(alert={"url": sink.url})
            code, document = deployment.run()
        self.assertEqual(20, code)
        self.assertEqual(1, len(sink.payloads))
        payload = sink.payloads[0]
        self.assertEqual(list(ALERT_KEYS), list(payload))
        self.assertEqual("energygrid_run_failed", payload["event"])
        self.assertEqual("list", payload["stage"])
        self.assertEqual("EG_HTTP_LIST_TENANT_MISMATCH", payload["support_ref"])
        self.assertEqual(20, payload["exit_code"])
        self.assertTrue(payload["attention_required"])
        self.assertRegex(payload["timestamp"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
        self.assert_no_canary(json.dumps(payload), json.dumps(sink.headers))
        self.assertIn("alert_delivered", [event["phase"] for event in deployment.log_events()])

    def test_fetch_stage_alert_carries_counts(self) -> None:
        with AlertSink() as sink:
            self.service_state.fetch_queue = {bill_name(1): [pdf_bytes(b"junk")]}
            deployment = self.deployment(alert={"url": sink.url})
            deployment.run()
        payload = sink.payloads[0]
        self.assertEqual("fetch", payload["stage"])
        self.assertEqual("INVALID_PDF", payload["status"])
        self.assertEqual({"inventory": 4, "downloaded": 0, "present": 0, "failure": 1}, payload["counts"])

    def test_unforeseen_exception_fails_closed_silently_with_an_alert(self) -> None:
        with AlertSink() as sink, mock.patch.object(
            cli, "reconcile_listed_inventory", side_effect=KeyError(CANARY_FILENAME_MARK + CANARY_TENANT)
        ):
            deployment = self.deployment(alert={"url": sink.url})
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code, document = deployment.run()
        self.assertEqual(20, code)
        self.assertEqual({"status": "ACTION_REQUIRED", "error_class": "RUNTIME_FAILURE"}, document)
        self.assertEqual("EG_RUNTIME_FAILURE", sink.payloads[0]["support_ref"])
        self.assert_no_canary(deployment.last_stdout, stderr.getvalue(), deployment.log_text(), json.dumps(sink.payloads))

    def test_success_sends_no_alert(self) -> None:
        with AlertSink() as sink:
            deployment = self.deployment(alert={"url": sink.url})
            self.assertEqual(0, deployment.run()[0])
            self.assertEqual(0, deployment.run()[0])
        self.assertEqual([], sink.payloads)

    def test_notifier_unavailable_never_changes_the_result(self) -> None:
        results = []
        for alert in (None, {"url": "http://127.0.0.1:9/hook"}):
            self.tearDown()
            self.setUp()
            self.service_state.list_queue = [json_document({"success": False, "files": []})]
            overrides = {"alert": alert} if alert else {}
            deployment = self.deployment(**overrides)
            code, document = deployment.run()
            results.append((code, document))
            phases = [event["phase"] for event in deployment.log_events()]
            if alert:
                self.assertIn("alert_failed", phases)
        self.assertEqual(results[0], results[1])

    def test_notifier_error_response_or_exception_never_changes_the_result(self) -> None:
        with AlertSink(respond=500) as sink:
            self.service_state.fetch_queue = {bill_name(0): [status(400)]}
            deployment = self.deployment(alert={"url": sink.url})
            code, _document = deployment.run()
        self.assertEqual(20, code)
        self.assertIn("alert_failed", [event["phase"] for event in deployment.log_events()])
        self.tearDown()
        self.setUp()
        with mock.patch.object(cli, "send_alert", side_effect=RuntimeError("boom")):
            self.service_state.fetch_queue = {bill_name(0): [status(400)]}
            deployment = self.deployment(alert={"url": "http://127.0.0.1:9/hook"})
            code, _document = deployment.run()
        self.assertEqual(20, code)

    def test_auth_header_comes_only_from_the_named_environment_variable(self) -> None:
        with AlertSink() as sink, mock.patch.dict(os.environ, {"EG_TEST_ALERT_TOKEN": "canary-private-token"}):
            self.service_state.list_queue = [status(403)]
            deployment = self.deployment(
                alert={"url": sink.url, "auth_header_name": "X-EG-Alert", "auth_token_env": "EG_TEST_ALERT_TOKEN"}
            )
            deployment.run()
        received = {key.lower(): value for key, value in sink.headers[0].items()}
        self.assertEqual("canary-private-token", received.get("x-eg-alert"))
        self.assertNotIn("canary-private-token", deployment.log_text())

    def test_missing_token_sends_nothing(self) -> None:
        with AlertSink() as sink, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EG_TEST_ALERT_TOKEN", None)
            self.service_state.list_queue = [status(403)]
            deployment = self.deployment(
                alert={"url": sink.url, "auth_header_name": "X-EG-Alert", "auth_token_env": "EG_TEST_ALERT_TOKEN"}
            )
            code, _document = deployment.run()
        self.assertEqual(20, code)
        self.assertEqual([], sink.payloads)

    def test_payload_builder_replaces_any_unvalidated_value(self) -> None:
        payload = build_alert_payload(
            run_id=CANARY_TENANT,
            stage=CANARY_FETCH_PATH,
            status=CANARY_FILENAME_MARK,
            support_ref="lower " + CANARY_TENANT,
            exit_code="20",
            counts={"inventory": CANARY_TENANT, "downloaded": -1, "extra": 5},
        )
        self.assertEqual(list(ALERT_KEYS), list(payload))
        self.assert_no_canary(json.dumps(payload))
        self.assertEqual({"inventory": 0, "downloaded": 0, "present": 0, "failure": 0}, payload["counts"])


# --------------------------------------------------------------------------
# Single-run exclusion
# --------------------------------------------------------------------------


LOCK_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from energygrid_bill_downloader.run_lock import RunLock
lock = RunLock(Path(sys.argv[2]))
lock.__enter__()
print("held", flush=True)
time.sleep(120)
"""


class SingleRun(DirectHttpCase):
    def test_overlapping_run_is_refused_before_touching_anything(self) -> None:
        deployment = self.deployment()
        deployment.state_path.parent.mkdir(parents=True)
        with RunLock(deployment.state_path.parent):
            code, document = deployment.run()
        self.assertEqual(10, code)
        self.assertEqual("RUN_IN_PROGRESS", document["status"])
        self.assertEqual("EG_RUN_ALREADY_ACTIVE", document["support_ref"])
        self.assertEqual([], self.service_state.list_requests)
        self.assertFalse(deployment.state_path.exists())
        # Released lock: the next run proceeds normally.
        self.assertEqual(0, deployment.run()[0])

    def test_lock_is_released_when_the_holder_process_dies(self) -> None:
        directory = self.tmp / "lockdir"
        directory.mkdir()
        package_root = str(Path(__file__).resolve().parents[1])
        holder = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER, package_root, str(directory)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("held", holder.stdout.readline().strip())
            with self.assertRaises(Exception) as caught:
                RunLock(directory).__enter__()
            self.assertEqual("RUN_IN_PROGRESS", caught.exception.status)
        finally:
            holder.kill()
            holder.wait(timeout=30)
            holder.stdout.close()
        # Process death released the kernel lock; the stale file is harmless.
        self.assertTrue((directory / LOCK_FILENAME).exists())
        with RunLock(directory):
            pass

    def test_overlap_alert_is_sent_without_attention_required(self) -> None:
        with AlertSink() as sink:
            deployment = self.deployment(alert={"url": sink.url})
            deployment.state_path.parent.mkdir(parents=True)
            with RunLock(deployment.state_path.parent):
                code, _document = deployment.run()
        self.assertEqual(10, code)
        self.assertEqual("lock", sink.payloads[0]["stage"])
        self.assertFalse(sink.payloads[0]["attention_required"])


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class DirectHttpConfiguration(unittest.TestCase):
    def base(self, tmp: Path, **overrides) -> dict:
        raw = {
            "source": "direct_http",
            "direct_http": {"list_url": "https://h.example/l", "fetch_url": "https://h.example/f", "tenant_id": "T-1"},
            "archive_root": str(tmp / "a"),
            "state_path": str(tmp / "s" / "state.sqlite3"),
            "temp_root": str(tmp / "t"),
            "log_root": str(tmp / "g"),
        }
        raw.update(overrides)
        return raw

    def test_valid_direct_http_config_needs_no_portal_or_account_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_runtime_config(self.base(Path(tmp)))
        self.assertEqual("direct_http", config.source)
        self.assertNotIn("T-1", repr(config))
        self.assertNotIn("h.example", repr(config))

    def test_committed_example_is_placeholder_only_and_loads(self) -> None:
        example = Path(__file__).parents[1] / "config" / "energygrid.direct_http.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        self.assertTrue(raw["direct_http"]["tenant_id"].startswith("REPLACE_WITH_"))
        for key in ("list_url", "fetch_url"):
            self.assertIn(".placeholder.invalid/REPLACE_WITH_", raw["direct_http"][key])
        self.assertIn("REPLACE_WITH_", raw["alert"]["url"])
        with tempfile.TemporaryDirectory() as tmp:
            for key in ("archive_root", "temp_root", "log_root"):
                raw[key] = str(Path(tmp) / key)
            raw["state_path"] = str(Path(tmp) / "state" / "state.sqlite3")
            config = load_runtime_config(raw)
        self.assertEqual("direct_http", config.source)

    def test_invalid_direct_http_blocks_are_refused(self) -> None:
        bad_blocks = [
            None,
            {"list_url": "https://h.example/l", "fetch_url": "https://h.example/f"},
            {"list_url": "https://h.example/l", "fetch_url": "https://h.example/f", "tenant_id": "T", "extra": 1},
            {"list_url": "ftp://h.example/l", "fetch_url": "https://h.example/f", "tenant_id": "T"},
            {"list_url": "https://u:p@h.example/l", "fetch_url": "https://h.example/f", "tenant_id": "T"},
            {"list_url": "https://h.example/l", "fetch_url": "https://h.example/f", "tenant_id": " T"},
            {"list_url": "https://h.example/l", "fetch_url": "https://h.example/f", "tenant_id": ""},
            {"list_url": "https://h.example/l", "fetch_url": "https://h.example/f", "tenant_id": 5},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for block in bad_blocks:
                with self.subTest(block=str(block)[:60]):
                    with self.assertRaises(ConfigError):
                        load_runtime_config(self.base(Path(tmp), direct_http=block))

    def test_alert_must_be_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for url in ("https://alerts.example/hook", "http://10.0.0.5/hook", "http://0.0.0.0/hook"):
                with self.subTest(url=url):
                    with self.assertRaises(ConfigError):
                        load_runtime_config(self.base(Path(tmp), alert={"url": url}))
            for url in ("http://127.0.0.1:5678/webhook/x", "http://localhost:5678/webhook/x", "http://[::1]:5678/x"):
                with self.subTest(url=url):
                    load_runtime_config(self.base(Path(tmp), alert={"url": url}))
            with self.assertRaises(ConfigError):
                load_runtime_config(self.base(Path(tmp), alert={"url": "http://127.0.0.1/x", "auth_header_name": "X-A"}))

    def test_source_selection_is_explicit_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = self.base(Path(tmp), source="browser", account_identity="x", portal_url="https://p.example/")
            with self.assertRaises(ConfigError):
                load_runtime_config(raw)
            with self.assertRaises(ConfigError):
                load_runtime_config(self.base(Path(tmp), source="auto"))

    def test_overrides_keep_the_private_source_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_runtime_config(self.base(Path(tmp), alert={"url": "http://127.0.0.1:1/x"}))
            changed = config.with_overrides({"timeout_seconds": 9})
        self.assertEqual(config.direct_http, changed.direct_http)
        self.assertEqual(config.alert, changed.alert)
        self.assertEqual("direct_http", changed.source)

    def test_headed_is_refused_for_direct_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            raw = self.base(Path(tmp))
            Path(raw["archive_root"]).mkdir()
            path.write_text(json.dumps(raw), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(64, cli.main(["run", "--config", str(path), "--headed"]))


class NoBrowserOnTheDirectPath(DirectHttpCase):
    def test_direct_http_run_never_constructs_the_browser_portal(self) -> None:
        with mock.patch.object(cli, "PlaywrightPortal", side_effect=AssertionError("browser reached")):
            deployment = self.deployment()
            code, _document = deployment.run()
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
