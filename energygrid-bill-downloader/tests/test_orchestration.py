"""#226 G3 deterministic core: plan/command contract, pre-generated Drive file
ID idempotency, receipt verification, Drive-before-email and no-resend, and
(#226 G2 fairness reclosure) per-stream run fairness driven by a harness loop
that transliterates the committed production prompt steps 3 and 4.

Synthetic only: the fake Drive service models the n8n Drive workflow and the
relevant Google semantics; nothing contacts Google, n8n, SMTP or EnergyGrid.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from typing import Callable

from energygrid_bill_downloader.config import RUNTIME_V3_SCHEMA, DeliverySettings, DualRuntimeConfig
from energygrid_bill_downloader.drive import DriveClient
from energygrid_bill_downloader.errors import AppError, RunLockedError, StateError
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.orchestration import (
    ALLOWED_COMMAND_LINES,
    CoreContext,
    compute_status,
    fallback_result,
    run_command,
)
from energygrid_bill_downloader.state import StateV3Store

from fixtures.synthetic_delivery import synthetic_pdf
from fixtures.synthetic_dual_stream import (
    SYNTHETIC_FOLDERS,
    FakeDelivery,
    FakeDriveService,
    SyntheticAdapter,
    bind_synthetic_drive,
    candidate,
    create_v3_database,
    snapshot,
    synthetic_drive_settings,
    test_stream_entries,
)


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def event(self, phase, status=None, **fields):
        self.events.append((phase, status, fields))


RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
PROMPT_PATH = RUNTIME / "claude" / "energygrid_orchestrator.prompt.md"
SUPERVISOR_PATH = RUNTIME / "claude_supervisor.ps1"
# The committed production prompt's loop rule, verbatim (steps 3 and 4).
# CoreHarness.loop below is a line-by-line transliteration of exactly this text.
PROMPT_LOOP_LINES = (
    "3. Loop, counting every command you run in it:",
    "   a. Run `egcore.cmd plan`. If it exits with a non-zero code, stop the loop.",
    "   b. Read the JSON field `next.argv`. If it is null, stop the loop.",
    "   c. Run exactly the command in `next.argv`.",
    "   d. If its exit code is 0, 10 or 20 and its one line of JSON has the field",
    "      `disposition` equal to `CONTINUE` or `STREAM_STOPPED`, go back to step a.",
    "      The core never plans a stopped stream again in this run.",
    "   e. Otherwise (`RUN_STOP`, any other value, no such field, other exit code,",
    "      or output that is not one line of JSON) stop the loop.",
    "   Never start a command after 15 commands have run in the loop; stop instead.",
    "4. After the loop, always run `egcore.cmd status` exactly once, even if the",
    "   loop stopped early.",
)
PROMPT_COMMAND_CEILING = 15


def _one_json_line(text: str):
    """Claude's reading of a command's stdout: exactly one line of JSON or nothing."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        return json.loads(lines[0])
    except ValueError:
        return None


def _supervisor_tables() -> tuple[dict[str, int], int]:
    supervisor = SUPERVISOR_PATH.read_text(encoding="utf-8")
    block = re.search(r"\$script:EgOutcomeExit = @\{(.*?)\n\}", supervisor, re.S).group(1)
    mapping = {name: int(code) for name, code in re.findall(r"'([A-Z_]+)' = (\d+)", block)}
    invalid = int(re.search(r"StatusInvalid = (\d+)", supervisor).group(1))
    return mapping, invalid


# Lines of the supervisor's Get-EgStatusExitCode that supervisor_status_exit
# transliterates; a static test pins each one.
SUPERVISOR_STATUS_LINES = (
    "if (-not (Test-EgStatusDocument -Status $Status)) { return $script:EgExit.StatusInvalid }",
    "if (-not $Status.terminal) { return $script:EgExit.StatusInvalid }",
    "$code = [int]$script:EgOutcomeExit[[string]$Status.business_outcome]",
    "if ($code -eq 0 -and $Status.uncertainty_outstanding) { return $script:EgExit.StatusInvalid }",
)


def supervisor_status_exit(status: dict | None) -> int:
    """The supervisor's final status -> exit code mapping (Get-EgStatusExitCode)."""
    mapping, invalid = _supervisor_tables()
    if not isinstance(status, dict) or status.get("schema") != "energygrid.core.status.v3":
        return invalid
    if type(status.get("terminal")) is not bool or type(status.get("uncertainty_outstanding")) is not bool:
        return invalid
    if status.get("business_outcome") not in mapping or not status["terminal"]:
        return invalid
    code = mapping[status["business_outcome"]]
    if code == 0 and status["uncertainty_outstanding"]:
        return invalid
    return code


class HeldLock:
    def __init__(self, _path):
        pass

    def __enter__(self):
        raise RunLockedError()

    def __exit__(self, *args):
        return None


class CoreHarness:
    """The deterministic core behind the exact production orchestration loop.

    `loop` is the only multi-command driver and transliterates the committed
    prompt steps 3 and 4; it runs at most once per run ID. `run` goes through
    the CLI's shared fallback helper, so escaped errors produce the same
    RUN_STOP documents as production."""

    def __init__(self, test: unittest.TestCase, *, bound=("EB_BILL", "TENANT_BILL"), drive_bound=("EB_BILL", "TENANT_BILL")) -> None:
        self.test = test
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.archive = root / "archive"
        self.archive.mkdir()
        self.state_path = root / "state" / "state.sqlite3"
        self.state_path.parent.mkdir()
        create_v3_database(self.state_path, bound=tuple(Stream(item) for item in bound))
        bind_synthetic_drive(self.state_path, drive_bound)
        self.config = DualRuntimeConfig(
            archive_root=self.archive, state_path=self.state_path, temp_root=root / "temp", log_root=root / "logs",
            streams=test_stream_entries(bound=tuple(Stream(item) for item in bound)),
            drive=synthetic_drive_settings(streams=drive_bound),
            delivery=DeliverySettings(url="http://127.0.0.1:5678/webhook/SYNTHETIC", auth_header_name="X-Synthetic",
                                      auth_token_env="ENERGYGRID_DELIVERY_TOKEN", max_pdf_bytes=1_000_000, timeout_seconds=5),
            schema=RUNTIME_V3_SCHEMA,
        )
        self.config.preflight()
        self.drive = FakeDriveService()
        self.delivery = FakeDelivery()
        self.eb = [candidate(Stream.EB_BILL, name="eb-old.pdf", invoice_date="2026-09-01"),
                   candidate(Stream.EB_BILL, name="eb-latest.pdf", invoice_date="2026-10-01")]
        self.tenant = [candidate(Stream.TENANT_BILL, name="tenant-latest.pdf", invoice_date="2026-10-02")]
        self.adapters = None
        self.outputs: list[str] = []
        self.logger = RecordingLogger()
        self.looped: dict[str, dict] = {}
        # Test-only switches: Drive auth per stream, and per exact allowlisted line.
        self.drive_auth_unavailable: set[str] = set()
        self.drive_client_missing = False
        self.locked_lines: set[str] = set()
        self.output_overrides: dict[str, tuple[str, int]] = {}
        self.after_line: dict[str, Callable[[], None]] = {}

    def new_adapters(self):
        payloads = {item.source_filename: synthetic_pdf(item.source_filename.encode()) for item in (*self.eb, *self.tenant)}
        self.adapters = {
            "EB_BILL": SyntheticAdapter(snapshot(Stream.EB_BILL, tuple(self.eb)), payloads),
            "TENANT_BILL": SyntheticAdapter(snapshot(Stream.TENANT_BILL, tuple(self.tenant)), payloads),
        }
        return self.adapters

    def core(self, run_id: str, stream: str | None = None, command: str | None = None) -> CoreContext:
        token = {} if stream in self.drive_auth_unavailable else {"ENERGYGRID_DRIVE_TOKEN": "synthetic-drive-token"}
        core = CoreContext(
            self.config, run_id=run_id, logger=self.logger, adapters_factory=self.new_adapters,
            drive_client=None if self.drive_client_missing else DriveClient(
                self.config.drive, post_once=self.drive.post_once, environ=token),
            delivery_client=self.delivery,
        )
        if command is not None and command_line_of(command, stream) in self.locked_lines:
            core.lock_factory = HeldLock
        return core

    def run(self, command: str, stream: str | None = None, *, run_id: str) -> tuple[dict, int]:
        try:
            document, code = run_command(command, stream, self.core(run_id, stream, command))
        except AppError as error:
            # Exactly what cli.run_core_command prints for an escaped error.
            document, code = fallback_result(command, stream, error)
        self.outputs.append(json.dumps(document, sort_keys=True))
        return document, code

    def execute(self, line: str, run_id: str) -> tuple[str, int]:
        """One Bash call as Claude sees it: an allowlisted line -> (stdout, exit code)."""
        self.test.assertIn(line, ALLOWED_COMMAND_LINES)
        if line in self.output_overrides:
            text, code = self.output_overrides.pop(line)
        else:
            parts = line.split()
            document, code = self.run(parts[1], parts[3] if len(parts) == 4 else None, run_id=run_id)
            text = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        hook = self.after_line.pop(line, None)
        if hook is not None:
            hook()
        return text, code

    def loop(self, run_id: str, *, ceiling: int = PROMPT_COMMAND_CEILING) -> list[tuple[str, int]]:
        """Prompt steps 3 and 4, transliterated. Returns the executed next.argv
        commands with exit codes; `self.looped[run_id]` keeps the full trace."""
        if run_id in self.looped:
            raise AssertionError("production runs exactly one loop per run ID")
        trace: dict = {"commands": [], "steps": [], "dispositions": [], "last_plan_action": None}
        self.looped[run_id] = trace
        commands = trace["commands"]
        while True:
            # "Never start a command after 15 commands have run in the loop; stop instead."
            if len(commands) >= ceiling:
                break
            # a. Run `egcore.cmd plan`. If it exits with a non-zero code, stop the loop.
            text, code = self.execute("egcore.cmd plan", run_id)
            commands.append("egcore.cmd plan")
            if code != 0:
                break
            # b. Read the JSON field `next.argv`. If it is null, stop the loop.
            plan = _one_json_line(text)
            if not isinstance(plan, dict) or not isinstance(plan.get("next"), dict):
                break
            trace["last_plan_action"] = plan["next"].get("action")
            argv = plan["next"].get("argv")
            if argv is None:
                break
            if len(commands) >= ceiling:
                break
            # c. Run exactly the command in `next.argv`.
            text, code = self.execute(argv, run_id)
            commands.append(argv)
            trace["steps"].append((argv, code))
            result = _one_json_line(text)
            disposition = result.get("disposition") if isinstance(result, dict) else None
            trace["dispositions"].append(disposition)
            # d. Exit 0, 10 or 20 with disposition CONTINUE or STREAM_STOPPED: back to a.
            if code in {0, 10, 20} and disposition in {"CONTINUE", "STREAM_STOPPED"}:
                continue
            # e. Otherwise stop the loop.
            break
        # 4. After the loop, always run `egcore.cmd status` exactly once.
        text, code = self.execute("egcore.cmd status", run_id)
        trace["status"] = _one_json_line(text) if code == 0 else None
        trace["supervisor_exit"] = supervisor_status_exit(trace["status"])
        return list(trace["steps"])

    def status(self, run_id: str) -> dict:
        document, code = self.run("status", run_id=run_id)
        self.test.assertEqual(0, code)
        return document

    def operation(self, stream: str) -> dict | None:
        with StateV3Store(self.state_path, read_only=True) as state:
            invoice_id = state.stream(stream)["watermark_invoice_id"]
            if invoice_id is None:
                return None
            binding = state.active_binding(stream)
            return state.drive_operation_for(invoice_id, binding["binding_id"])

    def dispatches(self, stream: str) -> list[dict]:
        operation = self.operation(stream)
        with StateV3Store(self.state_path, read_only=True) as state:
            return state.drive_dispatches(operation["operation_id"])

    def drive_until(self, run_id: str, stream: str, command: str) -> None:
        """Advance one stream until its planned action is `command` (not executing it)."""
        for _ in range(10):
            plan, _code = self.run("plan", run_id=run_id)
            action = plan["streams"][stream]["action"]
            mapping = {"ACQUIRE_LATEST": "acquire", "DRIVE_PREPARE": "drive-intent", "DRIVE_UPLOAD": "drive-upload",
                       "DRIVE_RECONCILE": "drive-reconcile", "EMAIL_DELIVER": "deliver", "EMAIL_RECONCILE": "deliver"}
            if mapping.get(action) == command:
                return
            next_command = mapping[action]
            self.test.assertEqual(0, self.run(next_command, None if next_command == "acquire" else stream, run_id=run_id)[1])
        raise AssertionError("did not reach the requested action")


def run_id() -> str:
    return str(uuid.uuid4())


def command_line_of(command: str, stream: str | None) -> str:
    return f"egcore.cmd {command}" + (f" --stream {stream}" if stream else "")


class CoreHappyPathTests(unittest.TestCase):
    def test_full_sequence_verifies_drive_before_one_email_per_stream_then_replay_is_no_work(self) -> None:
        harness = CoreHarness(self)
        first = run_id()
        steps = harness.loop(first)
        self.assertTrue(all(code == 0 for _argv, code in steps), steps)
        self.assertEqual([
            "egcore.cmd acquire",
            "egcore.cmd drive-intent --stream EB_BILL", "egcore.cmd drive-upload --stream EB_BILL",
            "egcore.cmd deliver --stream EB_BILL",
            "egcore.cmd drive-intent --stream TENANT_BILL", "egcore.cmd drive-upload --stream TENANT_BILL",
            "egcore.cmd deliver --stream TENANT_BILL",
        ], [argv for argv, _code in steps])
        self.assertEqual(["EB_BILL", "TENANT_BILL"], harness.delivery.sent)
        self.assertEqual(["eb-latest.pdf"], harness.adapters["EB_BILL"].acquire_calls)
        for stream in ("EB_BILL", "TENANT_BILL"):
            operation = harness.operation(stream)
            self.assertEqual("DRIVE_VERIFIED", operation["state"])
            self.assertEqual("SHA256", operation["verification_method"])
            self.assertEqual(operation["reserved_remote_file_id"], operation["remote_file_id"])
            self.assertEqual(1, operation["upload_attempt_count"])
            properties = json.loads(operation["app_properties_json"])
            self.assertEqual(["egApp", "egBnd", "egInv", "egOp", "egSchema", "egSha", "egStream"], sorted(properties))
        status = harness.status(first)
        self.assertEqual("COMPLETED", status["business_outcome"])
        self.assertTrue(status["terminal"])
        self.assertFalse(status["uncertainty_outstanding"])

        second = run_id()
        creates_before = list(harness.drive.creates)
        steps = harness.loop(second)
        self.assertEqual([("egcore.cmd acquire", 0)], steps)
        self.assertEqual([], harness.adapters["EB_BILL"].acquire_calls, "zero FETCH on replay")
        self.assertEqual(creates_before, harness.drive.creates, "zero upload on replay")
        self.assertEqual(["EB_BILL", "TENANT_BILL"], harness.delivery.sent, "zero resend on replay")
        status = harness.status(second)
        self.assertEqual("NO_WORK", status["business_outcome"])
        self.assertTrue(all(item["fully_handled"] for item in status["streams"].values()))

    def test_outputs_carry_no_ids_dates_paths_or_tokens(self) -> None:
        harness = CoreHarness(self)
        harness.loop(run_id())
        joined = "\n".join(harness.outputs)
        forbidden = [*SYNTHETIC_FOLDERS.values(), "synthDriveFile", "2026-", "eb-latest", str(harness.archive),
                     "synthetic-drive-token", "egdb3-", "egmail-v1-", "SYNTHETIC-ENERGYGRID-NAMESPACE"]
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, joined)
        operation = harness.operation("EB_BILL")
        self.assertNotIn(operation["operation_id"], joined)
        self.assertNotIn(operation["invoice_id"], joined)


class PregeneratedIdTests(unittest.TestCase):
    def test_reservation_happens_once_and_is_frozen_before_the_dispatch_marker(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        observed: list[tuple] = []
        original = harness.drive.post_once

        def spy(url, authorization, body, timeout, *, content_type):
            from fixtures.synthetic_dual_stream import parse_multipart

            metadata, _pdf = parse_multipart(body, content_type)
            if metadata["mode"] == "UPLOAD_IF_ABSENT":
                # At the moment the upload leaves the core, the reserved ID and the
                # dispatch marker carrying that same ID are already durable.
                operation = harness.operation("EB_BILL")
                observed.append((operation["reserved_remote_file_id"], [d["reserved_remote_file_id"] for d in harness.dispatches("EB_BILL")],
                                 metadata["reserved_file_id"]))
            return original(url, authorization, body, timeout, content_type=content_type)

        harness.drive.post_once = spy
        _document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual(0, code)
        self.assertEqual(["RESERVE_ID", "UPLOAD_IF_ABSENT"], harness.drive.calls)
        self.assertEqual(1, len(harness.drive.generated))
        reserved = harness.drive.generated[0]
        self.assertEqual([(reserved, [reserved], reserved)], observed)
        self.assertEqual([reserved], harness.drive.creates)

    def test_invalid_or_lost_reservation_uploads_nothing_and_a_later_call_may_reserve_again(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.drop_reservation_response = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((10, "RESERVATION_UNAVAILABLE", False), (code, document["outcome"], document["mutated"]))
        operation = harness.operation("EB_BILL")
        self.assertIsNone(operation["reserved_remote_file_id"])
        self.assertEqual(0, operation["upload_attempt_count"])
        self.assertEqual([], harness.dispatches("EB_BILL"))
        self.assertEqual({}, harness.drive.files)
        harness.drive.drop_reservation_response = False
        # #226 G2 R13: RESERVATION_UNAVAILABLE stops EB for this run; the later call is a later run.
        later = run_id()
        self.assertEqual(0, harness.run("acquire", run_id=later)[1])
        self.assertEqual(0, harness.run("drive-upload", "EB_BILL", run_id=later)[1])
        operation = harness.operation("EB_BILL")
        self.assertEqual(harness.drive.generated[1], operation["reserved_remote_file_id"])
        self.assertEqual("DRIVE_VERIFIED", operation["state"])

    def test_crash_after_reservation_reuses_the_same_frozen_id(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as state:
            self.assertTrue(state.reserve_drive_file_id(operation["operation_id"], "synthReservedBeforeCrash", current, "2026-10-06T00:00:00+00:00"))
        # Process dies here: no dispatch marker, no upload. The next run reuses the ID.
        later = run_id()
        harness.loop(later)
        operation = harness.operation("EB_BILL")
        self.assertEqual("synthReservedBeforeCrash", operation["reserved_remote_file_id"])
        self.assertEqual("synthReservedBeforeCrash", operation["remote_file_id"])
        self.assertEqual([], harness.drive.generated, "no second ID was generated")
        self.assertEqual(["synthReservedBeforeCrash"], harness.drive.creates)

    def test_crash_after_dispatch_marker_before_post_reconciles_then_retries_same_id_next_run(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        first = run_id()
        harness.drive_until(first, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as state:
            state.reserve_drive_file_id(operation["operation_id"], "synthReservedMarkerCrash", first, "2026-10-06T00:00:00+00:00")
            state.begin_drive_dispatch(operation["operation_id"], first, "2026-10-06T00:00:01+00:00")
        plan, _ = harness.run("plan", run_id=first)
        self.assertEqual("DRIVE_RECONCILE", plan["streams"]["EB_BILL"]["action"])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=first)
        self.assertEqual((20, "EG_DRIVE_RETRY_NEXT_RUN"), (code, document["support_ref"]))
        self.assertEqual("RECOVERED_MARKER", harness.dispatches("EB_BILL")[0]["outcome"])
        self.assertEqual("DRIVE_UPLOAD_UNCERTAIN", harness.operation("EB_BILL")["state"])
        self.assertEqual("DRIVE_UNCERTAIN", harness.status(first)["business_outcome"])
        second = run_id()
        steps = harness.loop(second)
        self.assertIn(("egcore.cmd drive-reconcile --stream EB_BILL", 0), steps)
        operation = harness.operation("EB_BILL")
        self.assertEqual(("DRIVE_VERIFIED", 2), (operation["state"], operation["upload_attempt_count"]))
        self.assertEqual(["synthReservedMarkerCrash", "synthReservedMarkerCrash"],
                         [item["reserved_remote_file_id"] for item in harness.dispatches("EB_BILL")])
        self.assertEqual(["synthReservedMarkerCrash"], harness.drive.creates)
        self.assertEqual(["EB_BILL"], harness.delivery.sent)

    def test_lost_upload_response_after_successful_create_reconciles_by_reserved_id(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.lose_response_after_create = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "DRIVE_UPLOAD_UNCERTAIN"), (code, document["outcome"]))
        self.assertEqual("NO_VALID_RESULT", harness.dispatches("EB_BILL")[0]["outcome"])
        self.assertEqual([], harness.delivery.sent, "no email while Drive is uncertain")
        harness.drive.lose_response_after_create = False
        # #226 G2 R13: DRIVE_UPLOAD_UNCERTAIN stops EB for this run; reconcile in a later run.
        later = run_id()
        self.assertEqual(0, harness.run("acquire", run_id=later)[1])
        steps = harness.loop(later)
        self.assertEqual("egcore.cmd drive-reconcile --stream EB_BILL", steps[0][0])
        operation = harness.operation("EB_BILL")
        self.assertEqual("DRIVE_VERIFIED", operation["state"])
        self.assertEqual(1, operation["upload_attempt_count"])
        self.assertEqual(1, len(harness.drive.creates))
        self.assertEqual(["EB_BILL"], harness.delivery.sent)

    def test_timeout_then_same_id_retry_answered_409_is_read_back_and_verified_without_duplicate(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        first = run_id()
        harness.drive_until(first, "EB_BILL", "drive-upload")
        harness.drive.lose_response_after_create = True
        harness.run("drive-upload", "EB_BILL", run_id=first)
        harness.drive.lose_response_after_create = False
        reserved = harness.operation("EB_BILL")["reserved_remote_file_id"]
        # Next run: an eventually-consistent lookup misses the object, so the
        # core sees a positively complete NOT_FOUND and authorises one retry.
        hidden = harness.drive.files.pop(reserved)
        second = run_id()
        self.assertEqual(0, harness.run("acquire", run_id=second)[1])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=second)
        self.assertEqual((0, "RETRY_AUTHORISED"), (code, document["outcome"]))
        harness.drive.files[reserved] = hidden
        harness.drive.stale_lookup_once = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=second)
        self.assertEqual((0, "DRIVE_VERIFIED"), (code, document["outcome"]))
        self.assertEqual(1, harness.drive.create_conflicts, "the same-ID create was answered 409")
        self.assertEqual([reserved], harness.drive.creates, "no second Drive file")
        self.assertEqual([reserved, reserved], [d["reserved_remote_file_id"] for d in harness.dispatches("EB_BILL")])
        self.assertEqual(reserved, harness.operation("EB_BILL")["remote_file_id"])

    def test_409_readback_with_conflicting_bytes_is_conflict_not_verified(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.mutate_after_create = lambda item: item.update(bytes=item["bytes"] + b"x", sha256="0" * 64)
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "DRIVE_CONFLICT", "EG_DRIVE_IDENTITY_BYTES_MISMATCH"), (code, document["outcome"], document["support_ref"]))
        self.assertEqual([], harness.delivery.sent)

    def test_reserved_object_metadata_conflicts_fail_closed(self) -> None:
        cases = {
            "EG_DRIVE_IDENTITY_OP_MISMATCH": lambda item: item["appProperties"].update(egOp="00000000-0000-4000-8000-000000000000"),
            "EG_DRIVE_IDENTITY_MOVED": lambda item: item.update(parents=["synthOtherFolder000001"]),
            "EG_DRIVE_IDENTITY_TRASHED": lambda item: item.update(trashed=True),
            "EG_DRIVE_MIME_MISMATCH": lambda item: item.update(mimeType="application/octet-stream"),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected=expected):
                harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
                current = run_id()
                harness.drive_until(current, "EB_BILL", "drive-upload")
                harness.drive.mutate_after_create = mutate
                document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
                self.assertEqual((20, "DRIVE_CONFLICT", expected), (code, document["outcome"], document["support_ref"]))
                self.assertEqual("DRIVE_CONFLICT", harness.operation("EB_BILL")["state"])
                harness.loop(current)
                self.assertEqual([], harness.delivery.sent)

    def test_remote_result_with_a_different_file_id_fails_closed(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.mint_different_id = True
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "DRIVE_CONFLICT", "EG_DRIVE_REMOTE_ID_MISMATCH"), (code, document["outcome"], document["support_ref"]))
        operation = harness.operation("EB_BILL")
        self.assertIsNone(operation["remote_file_id"])
        harness.loop(current)
        self.assertEqual([], harness.delivery.sent)

    def test_verified_receipt_cannot_bind_any_id_but_the_reserved_one(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        harness.loop(run_id())
        operation = harness.operation("EB_BILL")
        with closing(sqlite3.connect(harness.state_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE energygrid_drive_operation_v3 SET remote_file_id='synthOtherFile0000001' WHERE operation_id=?",
                                   (operation["operation_id"],))

    def test_two_concurrent_reservations_cannot_freeze_two_ids_and_frozen_id_cannot_be_replaced(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as first, StateV3Store(harness.state_path) as second:
            self.assertTrue(first.reserve_drive_file_id(operation["operation_id"], "synthConcurrentIdA001", current, "t"))
            self.assertFalse(second.reserve_drive_file_id(operation["operation_id"], "synthConcurrentIdB001", current, "t"))
        self.assertEqual("synthConcurrentIdA001", harness.operation("EB_BILL")["reserved_remote_file_id"])
        with closing(sqlite3.connect(harness.state_path)) as connection:
            for statement in (
                "UPDATE energygrid_drive_operation_v3 SET reserved_remote_file_id='synthConcurrentIdB001' WHERE operation_id=?",
                "UPDATE energygrid_drive_operation_v3 SET reserved_remote_file_id=NULL,reserved_run_id=NULL,reserved_at_utc=NULL WHERE operation_id=?",
            ):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(statement, (operation["operation_id"],))
        with StateV3Store(harness.state_path) as state:
            state.begin_drive_dispatch(operation["operation_id"], current, "t")
            with self.assertRaises(StateError):
                state.finish_drive_operation(operation["operation_id"], run_id=current, timestamp="t",
                                             new_state="DRIVE_UPLOAD_INTENT", authorise_retry=True)
        with closing(sqlite3.connect(harness.state_path)) as connection, self.assertRaises(sqlite3.DatabaseError):
            connection.execute("INSERT INTO energygrid_drive_dispatch_v3 (operation_id,attempt_no,reserved_remote_file_id,dispatch_run_id,dispatched_at_utc) VALUES (?,2,'synthConcurrentIdB001',?,'t')",
                               (operation["operation_id"], current))

    def test_dispatch_requires_a_reserved_id(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as state, self.assertRaises(StateError):
            state.begin_drive_dispatch(operation["operation_id"], current, "t")


class VerificationTests(unittest.TestCase):
    def upload(self, **switches) -> tuple[CoreHarness, dict, int, str]:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        for key, value in switches.items():
            setattr(harness.drive, key, value)
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        return harness, document, code, current

    def test_md5_and_size_fallback_when_sha256_is_absent(self) -> None:
        harness, document, code, _ = self.upload(report_sha256=False)
        self.assertEqual((0, "DRIVE_VERIFIED"), (code, document["outcome"]))
        operation = harness.operation("EB_BILL")
        self.assertEqual(("MD5_SIZE", None), (operation["verification_method"], operation["remote_sha256"]))

    def test_md5_mismatch_without_sha256_is_conflict(self) -> None:
        harness, document, code, _ = self.upload(report_sha256=False,
                                                 mutate_after_create=lambda item: item.update(md5="f" * 32))
        self.assertEqual((20, "EG_DRIVE_IDENTITY_BYTES_MISMATCH"), (code, document["support_ref"]))

    def test_sha256_mismatch_is_conflict_even_when_md5_matches(self) -> None:
        harness, document, code, _ = self.upload(mutate_after_create=lambda item: item.update(sha256="e" * 64))
        self.assertEqual((20, "DRIVE_CONFLICT"), (code, document["outcome"]))

    def test_no_checksum_holds_never_verifies_and_never_emails_then_later_reconcile_verifies(self) -> None:
        harness, document, code, _current = self.upload(report_sha256=False, report_md5=False)
        self.assertEqual((20, "HOLD", "EG_DRIVE_VERIFICATION_UNAVAILABLE"), (code, document["outcome"], document["support_ref"]))
        # #226 G2 R13: that HOLD stops EB for this run; its same-stream reconcile is a later run.
        middle = run_id()
        harness.loop(middle)
        self.assertEqual([], harness.delivery.sent, "size alone never authorises email")
        self.assertEqual("DRIVE_CONFLICT", harness.status(middle)["business_outcome"])
        harness.drive.report_md5 = True
        later = run_id()
        steps = harness.loop(later)
        self.assertIn(("egcore.cmd drive-reconcile --stream EB_BILL", 0), steps)
        self.assertEqual(("DRIVE_VERIFIED", "MD5_SIZE"), (harness.operation("EB_BILL")["state"], harness.operation("EB_BILL")["verification_method"]))
        self.assertEqual(["EB_BILL"], harness.delivery.sent)
        self.assertEqual(1, len(harness.drive.creates), "the HOLD path never re-uploads")

    def test_http_200_with_a_disagreeing_n8n_outcome_is_never_success(self) -> None:
        harness, document, code, _ = self.upload(n8n_outcome_override="FOUND_EXACT", mutate_after_create=lambda item: item.update(trashed=True))
        self.assertEqual((20, "DRIVE_UPLOAD_UNCERTAIN", "EG_DRIVE_RESULT_DISAGREES"), (code, document["outcome"], document["support_ref"]))

    def test_incomplete_search_is_never_not_found_and_never_authorises_retry(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        first = run_id()
        harness.drive_until(first, "EB_BILL", "drive-upload")
        harness.drive.fail_create_before_google = True
        self.assertEqual(20, harness.run("drive-upload", "EB_BILL", run_id=first)[1])
        harness.drive.fail_create_before_google = False
        harness.drive.incomplete_search = True
        second = run_id()
        self.assertEqual(0, harness.run("acquire", run_id=second)[1])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=second)
        self.assertEqual((20, "EG_DRIVE_RECONCILE_UNAVAILABLE"), (code, document["support_ref"]))
        operation = harness.operation("EB_BILL")
        self.assertEqual(("DRIVE_UPLOAD_UNCERTAIN", None), (operation["state"], operation["retry_authorised_run_id"]))
        plan, _ = harness.run("plan", run_id=second)
        self.assertEqual("HOLD", plan["streams"]["EB_BILL"]["action"], "one reconcile per operation per run")

    def test_name_occupied_by_unrelated_file_is_conflict_before_any_create(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.put_file(file_id="synthUnrelatedFile0001", name="2026-10-01.pdf", parent=SYNTHETIC_FOLDERS["EB_BILL"],
                               payload=b"%PDF-1.4 other", app_properties={})
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "EG_DRIVE_NAME_OCCUPIED"), (code, document["support_ref"]))
        self.assertEqual([], harness.drive.creates, "no overwrite, suffix or variant")

    def test_retry_limit_is_two_attempts_then_hold(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        runs = [run_id() for _ in range(3)]
        harness.drive_until(runs[0], "EB_BILL", "drive-upload")
        harness.drive.fail_create_before_google = True
        harness.run("drive-upload", "EB_BILL", run_id=runs[0])
        harness.run("acquire", run_id=runs[1])
        self.assertEqual("RETRY_AUTHORISED", harness.run("drive-reconcile", "EB_BILL", run_id=runs[1])[0]["outcome"])
        self.assertEqual(20, harness.run("drive-upload", "EB_BILL", run_id=runs[1])[1])
        harness.run("acquire", run_id=runs[2])
        document, code = harness.run("drive-reconcile", "EB_BILL", run_id=runs[2])
        self.assertEqual((20, "EG_DRIVE_RETRY_EXHAUSTED"), (code, document["support_ref"]))
        self.assertEqual(2, harness.operation("EB_BILL")["upload_attempt_count"])

    def test_destination_change_holds_without_upload(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        harness.drive.folder_state = "FAIL"
        document, code = harness.run("drive-upload", "EB_BILL", run_id=current)
        self.assertEqual((20, "EG_DRIVE_DESTINATION_CHANGED"), (code, document["support_ref"]))
        self.assertEqual([], harness.drive.creates)


class CommandContractTests(unittest.TestCase):
    def test_unplanned_and_malformed_commands_are_refused_without_mutation(self) -> None:
        harness = CoreHarness(self)
        current = run_id()
        for command, stream in (("drive-upload", "EB_BILL"), ("deliver", "EB_BILL"), ("drive-intent", "TENANT_BILL")):
            with self.subTest(command=command):
                document, code = harness.run(command, stream, run_id=current)
                self.assertEqual((64, "REFUSED", False), (code, document["outcome"], document["mutated"]))
        for command, stream in (("plan", "EB_BILL"), ("deliver", None), ("deliver", "OTHER"), ("submit-drive-result", None), ("migrate-state", None)):
            with self.subTest(command=command, stream=stream):
                self.assertEqual(64, harness.run(command, stream, run_id=current)[1])
        self.assertEqual(0, harness.run("acquire", run_id=current)[1])
        self.assertEqual(64, harness.run("acquire", run_id=current)[1], "acquire runs once per run")

    def test_drive_before_email_is_enforced_by_plan_and_by_sqlite(self) -> None:
        harness = CoreHarness(self)
        current = run_id()
        harness.run("acquire", run_id=current)
        self.assertEqual(64, harness.run("deliver", "EB_BILL", run_id=current)[1])
        with StateV3Store(harness.state_path) as state:
            invoice = state.invoice(state.stream("EB_BILL")["watermark_invoice_id"])
            metadata = {"schema": "energygrid.invoice_delivery.v1", "stream": "EB_BILL", "bill_date": invoice["bill_date"],
                        "attachment_name": invoice["canonical_filename"], "pdf_byte_size": invoice["byte_size"], "pdf_sha256": invoice["sha256"]}
            with self.assertRaises(StateError):
                state.prepare_delivery(invoice_id=invoice["invoice_id"], metadata=metadata, run_id=current,
                                       timestamp="t", delivery_id="egmail-v1-" + "0" * 32)
        self.assertEqual([], harness.delivery.sent)

    def test_email_uncertainty_never_resends_and_never_uploads(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        harness.delivery.outcome = "DELIVERY_OUTCOME_UNCERTAIN"
        current = run_id()
        harness.loop(current)
        creates = list(harness.drive.creates)
        self.assertEqual(["EB_BILL"], harness.delivery.sent)
        self.assertEqual("EMAIL_UNCERTAIN", harness.status(current)["business_outcome"])
        later = run_id()
        harness.loop(later)
        self.assertEqual(64, harness.run("deliver", "EB_BILL", run_id=later)[1])
        self.assertEqual(["EB_BILL"], harness.delivery.sent, "no resend")
        self.assertEqual(creates, harness.drive.creates, "email uncertainty never re-uploads")

    def test_drive_uncertainty_is_independent_of_the_other_stream(self) -> None:
        """#226 G2 R10 = R1(a): one production loop per run. Run 2's EB reconcile
        response is lost (stream-local, exit 20, STREAM_STOPPED); the same loop
        re-plans, delivers Tenant, never retries EB, and stays non-success."""
        harness = CoreHarness(self)
        tenant = harness.tenant
        harness.tenant = []
        first = run_id()
        harness.drive.stream_faults["EB_BILL"] = {"lose_response_after_create"}
        self.assertEqual([(ACQ, 0), (INTENT_EB, 0), (UPLOAD_EB, 20)], harness.loop(first))
        self.assertEqual(["CONTINUE", "CONTINUE", "STREAM_STOPPED"], harness.looped[first]["dispositions"])
        self.assertEqual("DRIVE_UPLOAD_UNCERTAIN", harness.operation("EB_BILL")["state"])

        harness.tenant = tenant
        harness.drive.stream_faults["EB_BILL"] = {"drop_reconcile_response"}
        second = run_id()
        mark = len(harness.drive.requests)
        steps = harness.loop(second)
        trace = harness.looped[second]
        self.assertEqual([(ACQ, 0), (RECONCILE_EB, 20), (INTENT_TENANT, 0), (UPLOAD_TENANT, 0), (DELIVER_TENANT, 0)], steps)
        self.assertEqual(["CONTINUE", "STREAM_STOPPED", "CONTINUE", "CONTINUE", "CONTINUE"], trace["dispositions"])
        # A fresh plan follows the stopped EB command, and the final plan is null.
        self.assertEqual([PLAN, ACQ, PLAN, RECONCILE_EB, PLAN, INTENT_TENANT, PLAN, UPLOAD_TENANT, PLAN, DELIVER_TENANT, PLAN],
                         trace["commands"])
        self.assertEqual("HOLD", trace["last_plan_action"])
        self.assertEqual([("RECONCILE", "EB_BILL")], [item for item in harness.drive.requests[mark:] if item[1] == "EB_BILL"],
                         "EB is reconciled once and never uploaded or retried in the run")
        self.assertEqual(["TENANT_BILL"], harness.delivery.sent)
        self.assertEqual(2, len(harness.drive.creates), "one EB file from run 1, one Tenant file")
        self.assertEqual(("DRIVE_UPLOAD_UNCERTAIN", "DRIVE_VERIFIED"),
                         (harness.operation("EB_BILL")["state"], harness.operation("TENANT_BILL")["state"]))
        status = trace["status"]
        self.assertEqual(("DRIVE_UNCERTAIN", True, True),
                         (status["business_outcome"], status["terminal"], status["uncertainty_outstanding"]))
        self.assertEqual((True, False), (status["streams"]["EB_BILL"]["stopped_for_run"],
                                         status["streams"]["TENANT_BILL"]["stopped_for_run"]))
        self.assertEqual(("COMPLETED", True), (status["streams"]["TENANT_BILL"]["outcome"],
                                               status["streams"]["TENANT_BILL"]["fully_handled"]))
        self.assertEqual(84, trace["supervisor_exit"], "attention required, not success")

    def test_binding_mismatch_holds_before_any_network(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",), bound=("EB_BILL",))
        from dataclasses import replace

        settings = harness.config.drive
        other = replace(settings.bindings["EB_BILL"], folder_id="synthDifferentFolder01")
        harness.config = replace(harness.config, drive=replace(settings, bindings={"EB_BILL": other, "TENANT_BILL": None}))
        current = run_id()
        harness.run("acquire", run_id=current)
        plan, _ = harness.run("plan", run_id=current)
        self.assertEqual(("HOLD", "EG_DRIVE_BINDING_MISMATCH"), (plan["streams"]["EB_BILL"]["action"], plan["streams"]["EB_BILL"]["support_ref"]))
        self.assertEqual([], harness.drive.calls)

    def test_unbound_drive_stream_holds_and_status_is_not_success(self) -> None:
        harness = CoreHarness(self, drive_bound=("EB_BILL",))
        current = run_id()
        harness.loop(current)
        status = harness.status(current)
        self.assertEqual("EG_DRIVE_BINDING_UNBOUND", status["streams"]["TENANT_BILL"]["support_ref"])
        self.assertEqual("HOLD", status["business_outcome"])
        self.assertTrue(status["streams"]["EB_BILL"]["fully_handled"])

    def test_run_lock_contention_is_retryable_without_mutation(self) -> None:
        harness = CoreHarness(self)

        class Held:
            def __init__(self, _path):
                pass

            def __enter__(self):
                raise RunLockedError()

            def __exit__(self, *args):
                return None

        core = harness.core(run_id())
        core.lock_factory = Held
        document, code = run_command("acquire", None, core)
        self.assertEqual((10, "RUN_IN_PROGRESS", False), (code, document["outcome"], document["mutated"]))

    def test_status_before_acquire_is_incomplete_not_success(self) -> None:
        harness = CoreHarness(self)
        status = harness.status(run_id())
        self.assertEqual(("INCOMPLETE", False), (status["business_outcome"], status["terminal"]))


PLAN = "egcore.cmd plan"
ACQ = "egcore.cmd acquire"
INTENT_EB = "egcore.cmd drive-intent --stream EB_BILL"
UPLOAD_EB = "egcore.cmd drive-upload --stream EB_BILL"
RECONCILE_EB = "egcore.cmd drive-reconcile --stream EB_BILL"
DELIVER_EB = "egcore.cmd deliver --stream EB_BILL"
INTENT_TENANT = "egcore.cmd drive-intent --stream TENANT_BILL"
UPLOAD_TENANT = "egcore.cmd drive-upload --stream TENANT_BILL"
RECONCILE_TENANT = "egcore.cmd drive-reconcile --stream TENANT_BILL"
DELIVER_TENANT = "egcore.cmd deliver --stream TENANT_BILL"
STREAM_LINES = {
    stream: tuple(line for line in ALLOWED_COMMAND_LINES if line.endswith(f"--stream {stream}"))
    for stream in ("EB_BILL", "TENANT_BILL")
}


def archive_path(harness: CoreHarness, stream: str) -> Path:
    from energygrid_bill_downloader.reconcile import _canonical_path

    with StateV3Store(harness.state_path, read_only=True) as state:
        invoice = state.invoice(state.stream(stream)["watermark_invoice_id"])
    return _canonical_path(harness.config.archive_root, stream, invoice["canonical_filename"])


def corrupt_archive(harness: CoreHarness, stream: str) -> Callable[[], None]:
    """Hook: after acquire, the stream's committed archive bytes change; returns a restorer."""
    saved: dict[str, bytes] = {}

    def corrupt() -> None:
        path = archive_path(harness, stream)
        saved["bytes"] = path.read_bytes()
        path.write_bytes(saved["bytes"] + b"\n%synthetic tamper\n")

    def restore() -> None:
        archive_path(harness, stream).write_bytes(saved["bytes"])

    harness.after_line[ACQ] = corrupt
    return restore


def stop_rows(harness: CoreHarness) -> list[tuple]:
    with closing(sqlite3.connect(harness.state_path)) as connection:
        return list(connection.execute("SELECT run_id,stream,command,outcome,exit_code FROM energygrid_run_stream_stop_v3 ORDER BY run_id,stream"))


def database_dump(harness: CoreHarness) -> str:
    with closing(sqlite3.connect(harness.state_path)) as connection:
        return "\n".join(connection.iterdump())


class DualStreamFairnessTests(unittest.TestCase):
    """#226 G2 fairness reclosure (G4-F1): a stream-local stop ends only that
    stream for the current run; the peer stream progresses in the same single
    production loop; the stopped stream is never retried in that run; the final
    supervisor status stays fail-closed; the next run reconsiders the stream."""

    def assert_stream_commands_stop_at_their_stop(self, harness: CoreHarness, run: str) -> None:
        trace = harness.looped[run]
        for stream, lines in STREAM_LINES.items():
            stopped_at = [index for index, ((argv, _code), disposition) in enumerate(zip(trace["steps"], trace["dispositions"]))
                          if argv in lines and disposition != "CONTINUE"]
            self.assertLessEqual(len(stopped_at), 1, f"{stream} has at most one non-continuation command")
            if stopped_at:
                later = [argv for argv, _code in trace["steps"][stopped_at[0] + 1:] if argv in lines]
                self.assertEqual([], later, f"no {stream} command follows its stop in the same run")

    # -- R1(b) / R2 / R3 / R4 -------------------------------------------------
    def test_eb_reconcile_with_drive_auth_unavailable_lets_tenant_complete_and_run_is_incomplete(self) -> None:
        harness = CoreHarness(self)
        tenant = harness.tenant
        harness.tenant = []
        harness.drive.stream_faults["EB_BILL"] = {"lose_response_after_create"}
        harness.loop(run_id())
        harness.drive.stream_faults.clear()
        harness.tenant = tenant
        harness.drive_auth_unavailable = {"EB_BILL"}
        second = run_id()
        mark = len(harness.drive.requests)
        steps = harness.loop(second)
        self.assertEqual([(ACQ, 0), (RECONCILE_EB, 20), (INTENT_TENANT, 0), (UPLOAD_TENANT, 0), (DELIVER_TENANT, 0)], steps)
        self.assertEqual([], [item for item in harness.drive.requests[mark:] if item[1] == "EB_BILL"], "no EB network")
        self.assertEqual(["TENANT_BILL"], harness.delivery.sent)
        status = harness.looped[second]["status"]
        self.assertEqual("DRIVE_RECONCILE", status["streams"]["EB_BILL"]["action"], "the stopped step is kept")
        self.assertEqual(("INCOMPLETE", False), (status["business_outcome"], status["terminal"]))
        self.assertEqual(89, harness.looped[second]["supervisor_exit"])

    def test_symmetry_tenant_drive_failures_let_eb_complete(self) -> None:
        for fault, expected_exit, expected_outcome in (("drop_reconcile_response", 84, "DRIVE_UNCERTAIN"),
                                                       ("auth", 89, "INCOMPLETE")):
            with self.subTest(fault=fault):
                harness = CoreHarness(self)
                eb = harness.eb
                harness.eb = []
                harness.drive.stream_faults["TENANT_BILL"] = {"lose_response_after_create"}
                first = run_id()
                self.assertEqual([(ACQ, 0), (INTENT_TENANT, 0), (UPLOAD_TENANT, 20)], harness.loop(first))
                harness.eb = eb
                harness.drive.stream_faults.clear()
                if fault == "auth":
                    harness.drive_auth_unavailable = {"TENANT_BILL"}
                else:
                    harness.drive.stream_faults["TENANT_BILL"] = {fault}
                second = run_id()
                steps = harness.loop(second)
                self.assertEqual([(ACQ, 0), (INTENT_EB, 0), (UPLOAD_EB, 0), (DELIVER_EB, 0), (RECONCILE_TENANT, 20)], steps)
                self.assertEqual(["EB_BILL"], harness.delivery.sent)
                self.assertEqual("HOLD", harness.looped[second]["last_plan_action"])
                status = harness.looped[second]["status"]
                self.assertEqual(expected_outcome, status["business_outcome"])
                self.assertTrue(status["streams"]["TENANT_BILL"]["stopped_for_run"])
                self.assertEqual(expected_exit, harness.looped[second]["supervisor_exit"])

    def test_archive_failure_at_drive_intent_stops_only_that_stream(self) -> None:
        for failing, healthy in (("EB_BILL", "TENANT_BILL"), ("TENANT_BILL", "EB_BILL")):
            with self.subTest(failing=failing):
                harness = CoreHarness(self)
                corrupt_archive(harness, failing)
                current = run_id()
                steps = harness.loop(current)
                intent_failing = f"egcore.cmd drive-intent --stream {failing}"
                self.assertEqual(1, [argv for argv, _code in steps].count(intent_failing))
                self.assertIn((intent_failing, 20), steps)
                self.assertEqual([healthy], harness.delivery.sent)
                self.assert_stream_commands_stop_at_their_stop(harness, current)
                trace = harness.looped[current]
                self.assertEqual(("INCOMPLETE", 89), (trace["status"]["business_outcome"], trace["supervisor_exit"]))
                self.assertEqual("DRIVE_PREPARE", trace["status"]["streams"][failing]["action"])
                self.assertEqual([(current, failing, "drive-intent", "HOLD", 20)], stop_rows(harness))
                # R3: a direct command for the stopped stream refuses without mutation or network.
                before = database_dump(harness)
                requests, sent = list(harness.drive.requests), list(harness.delivery.sent)
                for line in STREAM_LINES[failing]:
                    parts = line.split()
                    document, code = harness.run(parts[1], parts[3], run_id=current)
                    self.assertEqual((64, "REFUSED", "EG_CORE_STREAM_STOPPED_FOR_RUN", False, "RUN_STOP"),
                                     (code, document["outcome"], document["support_ref"], document["mutated"], document["disposition"]))
                self.assertEqual(before, database_dump(harness))
                self.assertEqual((requests, sent), (harness.drive.requests, harness.delivery.sent))

    def test_eb_delivery_preparation_failure_is_not_retried_and_tenant_is_delivered(self) -> None:
        harness = CoreHarness(self)
        harness.delivery.preparation_failure_streams = {"EB_BILL"}
        current = run_id()
        steps = harness.loop(current)
        self.assertEqual([(ACQ, 0), (INTENT_EB, 0), (UPLOAD_EB, 0), (DELIVER_EB, 20),
                          (INTENT_TENANT, 0), (UPLOAD_TENANT, 0), (DELIVER_TENANT, 0)], steps)
        self.assertEqual(["TENANT_BILL"], harness.delivery.sent)
        with StateV3Store(harness.state_path, read_only=True) as state:
            delivery = state.delivery_for_invoice(state.stream("EB_BILL")["watermark_invoice_id"])
        self.assertEqual(("PENDING_SEND", None), (delivery["state"], delivery["dispatch_started_at_utc"]), "no dispatch marker")
        trace = harness.looped[current]
        self.assertEqual(("INCOMPLETE", 89), (trace["status"]["business_outcome"], trace["supervisor_exit"]))
        self.assertEqual([(current, "EB_BILL", "deliver", "HOLD", 20)], stop_rows(harness))

    def test_eb_email_uncertainty_never_resends_and_tenant_is_delivered(self) -> None:
        harness = CoreHarness(self)
        harness.delivery.outcomes["EB_BILL"] = "DELIVERY_OUTCOME_UNCERTAIN"
        current = run_id()
        steps = harness.loop(current)
        self.assertIn((DELIVER_EB, 20), steps)
        self.assertEqual(["EB_BILL", "TENANT_BILL"], harness.delivery.sent)
        self.assertEqual(("EMAIL_UNCERTAIN", 86), (harness.looped[current]["status"]["business_outcome"],
                                                   harness.looped[current]["supervisor_exit"]))
        later = run_id()
        self.assertEqual([(ACQ, 0)], harness.loop(later))
        self.assertEqual(["EB_BILL", "TENANT_BILL"], harness.delivery.sent, "no resend in this run or the next")
        self.assertEqual(("EMAIL_UNCERTAIN", 86), (harness.looped[later]["status"]["business_outcome"],
                                                   harness.looped[later]["supervisor_exit"]))

    # -- R5 / R6 ----------------------------------------------------------------
    FAULTS = {
        # name: (expected stream outcome after the run, expected stop command or None)
        None: ("COMPLETED", None),
        "upload_uncertain": ("INCOMPLETE", "drive-upload"),
        "drive_conflict": ("DRIVE_CONFLICT", "drive-upload"),
        "reservation_lost": ("INCOMPLETE", "drive-upload"),
        "archive": ("INCOMPLETE", "drive-intent"),
        "email_uncertain": ("EMAIL_UNCERTAIN", "deliver"),
        "email_rejected": ("HOLD", "deliver"),
    }

    def apply_fault(self, harness: CoreHarness, stream: str, fault: str | None) -> None:
        if fault == "upload_uncertain":
            harness.drive.stream_faults.setdefault(stream, set()).add("lose_response_after_create")
        elif fault == "drive_conflict":
            harness.drive.stream_mutations[stream] = lambda item: item.update(trashed=True)
        elif fault == "reservation_lost":
            harness.drive.stream_faults.setdefault(stream, set()).add("drop_reservation_response")
        elif fault == "archive":
            hook = harness.after_line.get(ACQ)

            def corrupt(stream=stream, hook=hook) -> None:
                if hook is not None:
                    hook()
                path = archive_path(harness, stream)
                path.write_bytes(path.read_bytes() + b"\n%synthetic tamper\n")

            harness.after_line[ACQ] = corrupt
        elif fault == "email_uncertain":
            harness.delivery.outcomes[stream] = "DELIVERY_OUTCOME_UNCERTAIN"
        elif fault == "email_rejected":
            harness.delivery.outcomes[stream] = "REQUEST_REJECTED"

    def test_both_streams_fail_independently_across_a_fault_matrix(self) -> None:
        from energygrid_bill_downloader.orchestration import OUTCOME_SEVERITY

        for eb_fault in self.FAULTS:
            for tenant_fault in self.FAULTS:
                with self.subTest(eb=eb_fault, tenant=tenant_fault):
                    harness = CoreHarness(self)
                    self.apply_fault(harness, "EB_BILL", eb_fault)
                    self.apply_fault(harness, "TENANT_BILL", tenant_fault)
                    current = run_id()
                    harness.loop(current)
                    trace = harness.looped[current]
                    self.assertLessEqual(len(trace["commands"]), PROMPT_COMMAND_CEILING)
                    self.assertEqual(PLAN, trace["commands"][-1], "the loop ends on a null plan")
                    self.assert_stream_commands_stop_at_their_stop(harness, current)
                    status = trace["status"]
                    expected = {"EB_BILL": self.FAULTS[eb_fault][0], "TENANT_BILL": self.FAULTS[tenant_fault][0]}
                    self.assertEqual(expected, {name: item["outcome"] for name, item in status["streams"].items()})
                    overall = next(item for item in OUTCOME_SEVERITY if item in expected.values())
                    self.assertEqual(overall, status["business_outcome"])
                    rows = {(stream, command) for _run, stream, command, _outcome, _code in stop_rows(harness)}
                    expected_rows = {(stream, self.FAULTS[fault][1]) for stream, fault in
                                     (("EB_BILL", eb_fault), ("TENANT_BILL", tenant_fault)) if self.FAULTS[fault][1]}
                    self.assertEqual(expected_rows, rows)
                    if eb_fault is None and tenant_fault is None:
                        self.assertEqual(0, trace["supervisor_exit"])
                    else:
                        self.assertNotEqual(0, trace["supervisor_exit"], "never false success")

    def test_one_stream_succeeds_while_the_other_holds(self) -> None:
        harness = CoreHarness(self)
        self.apply_fault(harness, "EB_BILL", "drive_conflict")
        current = run_id()
        harness.loop(current)
        trace = harness.looped[current]
        self.assertEqual(("DRIVE_CONFLICT", True), (trace["status"]["business_outcome"], trace["status"]["terminal"]))
        self.assertTrue(trace["status"]["streams"]["TENANT_BILL"]["fully_handled"])
        self.assertEqual(85, trace["supervisor_exit"])
        # Production shape: Tenant UNBOUND every day.
        harness = CoreHarness(self, bound=("EB_BILL",), drive_bound=("EB_BILL",))
        current = run_id()
        harness.loop(current)
        self.assertEqual(("HOLD", 81), (harness.looped[current]["status"]["business_outcome"], harness.looped[current]["supervisor_exit"]))
        harness = CoreHarness(self, bound=("EB_BILL",), drive_bound=("EB_BILL",))
        harness.drive_auth_unavailable = {"EB_BILL"}
        current = run_id()
        self.assertEqual([(ACQ, 0), (INTENT_EB, 0), (UPLOAD_EB, 20)], harness.loop(current))
        self.assertEqual(("INCOMPLETE", 89), (harness.looped[current]["status"]["business_outcome"], harness.looped[current]["supervisor_exit"]),
                         "an unbound peer's HOLD never hides the stopped stream's unfinished step")

    # -- R7 -----------------------------------------------------------------------
    def test_next_run_reconsiders_the_stopped_stream_without_repeating_the_other(self) -> None:
        harness = CoreHarness(self)
        tenant = harness.tenant
        harness.tenant = []
        harness.drive.stream_faults["EB_BILL"] = {"lose_response_after_create"}
        harness.loop(run_id())
        harness.drive.stream_faults.clear()
        harness.tenant = tenant
        harness.drive_auth_unavailable = {"EB_BILL"}
        second = run_id()
        harness.loop(second)
        self.assertEqual(89, harness.looped[second]["supervisor_exit"])
        creates, sent = list(harness.drive.creates), list(harness.delivery.sent)
        harness.drive_auth_unavailable = set()
        third = run_id()
        steps = harness.loop(third)
        self.assertEqual([(ACQ, 0), (RECONCILE_EB, 0), (DELIVER_EB, 0)], steps)
        self.assertFalse(any(argv in STREAM_LINES["TENANT_BILL"] for argv, _code in steps), "Tenant gets zero commands")
        self.assertEqual(creates, harness.drive.creates, "no re-upload")
        self.assertEqual(sent + ["EB_BILL"], harness.delivery.sent, "Tenant is not re-sent")
        status = harness.looped[third]["status"]
        self.assertEqual(("COMPLETED", False), (status["business_outcome"], status["streams"]["EB_BILL"]["stopped_for_run"]))
        self.assertEqual(0, harness.looped[third]["supervisor_exit"])

    # -- R8 -----------------------------------------------------------------------
    def assert_global_stop(self, harness: CoreHarness, run: str, last: tuple[str, int], support_ref: str | None = None) -> None:
        trace = harness.looped[run]
        self.assertEqual(last, trace["steps"][-1])
        self.assertNotEqual(PLAN, trace["commands"][-1], "the loop stopped without re-planning")
        self.assertFalse(any(argv in STREAM_LINES["TENANT_BILL"] for argv in trace["commands"]), "Tenant gets zero commands")
        self.assertEqual([], harness.delivery.sent)
        self.assertEqual([], stop_rows(harness), "a global stop writes no stop row")
        self.assertIsNotNone(trace["status"], "status still runs exactly once")
        self.assertNotEqual(0, trace["supervisor_exit"])
        if support_ref is not None:
            self.assertIn(support_ref, harness.outputs[-2])

    def test_global_failures_stop_the_whole_run(self) -> None:
        from unittest import mock

        from energygrid_bill_downloader import orchestration
        from energygrid_bill_downloader.errors import SourceContractError

        with self.subTest(case="drive client missing"):
            harness = CoreHarness(self)
            harness.drive_client_missing = True
            current = run_id()
            harness.loop(current)
            self.assert_global_stop(harness, current, (UPLOAD_EB, 64), "EG_CORE_CONFIG_INVALID")
            self.assertEqual("RUN_STOP", harness.looped[current]["dispositions"][-1])
        with self.subTest(case="run-lock contention on a stream command"):
            harness = CoreHarness(self)
            harness.locked_lines = {INTENT_EB}
            current = run_id()
            harness.loop(current)
            self.assert_global_stop(harness, current, (INTENT_EB, 10), "EG_RUN_ALREADY_ACTIVE")
        with self.subTest(case="forced StateError"):
            harness = CoreHarness(self)
            current = run_id()
            with mock.patch.object(StateV3Store, "create_drive_intent", side_effect=StateError("forced")):
                harness.loop(current)
            self.assert_global_stop(harness, current, (INTENT_EB, 20), "EG_CORE_FAILURE")
            self.assertIn('"outcome": "FAILED"', harness.outputs[-2])
        with self.subTest(case="acquire failure"):
            harness = CoreHarness(self)

            def source_down():
                raise SourceContractError("EG_SYNTHETIC_SOURCE_DOWN")

            harness.new_adapters = source_down
            current = run_id()
            harness.loop(current)
            self.assert_global_stop(harness, current, (ACQ, 20), "EG_ACQUIRE_FAILED")
        with self.subTest(case="unknown classifier tuple"):
            harness = CoreHarness(self)
            reduced = orchestration.CONTINUE_RESULTS - {("drive-intent", "INTENT_CREATED", 0)}
            current = run_id()
            with mock.patch.object(orchestration, "CONTINUE_RESULTS", reduced):
                harness.loop(current)
            self.assert_global_stop(harness, current, (INTENT_EB, 20), "EG_CORE_FAILURE")
        supervisor = SUPERVISOR_PATH.read_text(encoding="utf-8")
        refusal = re.search(r"\$refusal = '([^']*)'", supervisor).group(1)
        for name, text, code in (
            ("supervisor CoreDispatch refusal literal", refusal + "\n", 64),
            ("JSON without disposition", '{"outcome":"INTENT_CREATED","schema":"energygrid.core.result.v3"}\n', 0),
            ("not JSON", "The system cannot find the path specified.\n", 0),
            ("two lines", '{"disposition":"CONTINUE"}\n{"disposition":"CONTINUE"}\n', 0),
            ("launcher exit", '{"disposition":"CONTINUE"}\n', 71),
        ):
            with self.subTest(case=name):
                harness = CoreHarness(self)
                harness.output_overrides[INTENT_EB] = (text, code)
                current = run_id()
                harness.loop(current)
                self.assert_global_stop(harness, current, (INTENT_EB, code))

    # -- R9 -----------------------------------------------------------------------
    def dual_recovery_harness(self) -> CoreHarness:
        harness = CoreHarness(self)
        harness.drive.fail_create_before_google = True
        first = run_id()
        self.assertEqual([(ACQ, 0), (INTENT_EB, 0), (UPLOAD_EB, 20), (INTENT_TENANT, 0), (UPLOAD_TENANT, 20)],
                         harness.loop(first), "both streams stop; neither starves the other")
        harness.drive.fail_create_before_google = False
        return harness

    def test_worst_case_dual_recovery_uses_exactly_the_ceiling_and_ends_on_a_null_plan(self) -> None:
        harness = self.dual_recovery_harness()
        second = run_id()
        steps = harness.loop(second)
        trace = harness.looped[second]
        self.assertEqual([(ACQ, 0), (RECONCILE_EB, 0), (UPLOAD_EB, 0), (DELIVER_EB, 0),
                          (RECONCILE_TENANT, 0), (UPLOAD_TENANT, 0), (DELIVER_TENANT, 0)], steps)
        self.assertEqual(PROMPT_COMMAND_CEILING, len(trace["commands"]))
        self.assertEqual((PLAN, "NOTHING_TO_DO"), (trace["commands"][-1], trace["last_plan_action"]))
        self.assertEqual(("COMPLETED", 0), (trace["status"]["business_outcome"], trace["supervisor_exit"]))

    def test_a_lower_ceiling_stops_the_loop_status_still_runs_and_the_run_is_incomplete(self) -> None:
        harness = self.dual_recovery_harness()
        second = run_id()
        statuses_before = sum(1 for item in harness.outputs if "energygrid.core.status.v3" in item)
        harness.loop(second, ceiling=12)
        trace = harness.looped[second]
        self.assertEqual(12, len(trace["commands"]))
        self.assertEqual(UPLOAD_TENANT, trace["commands"][-1])
        self.assertEqual(statuses_before + 1, sum(1 for item in harness.outputs if "energygrid.core.status.v3" in item))
        self.assertEqual(("INCOMPLETE", False), (trace["status"]["business_outcome"], trace["status"]["terminal"]))
        self.assertEqual(89, trace["supervisor_exit"])

    # -- R0 / R11 / R14 -----------------------------------------------------------
    def test_harness_loop_is_the_committed_prompt_and_runs_once_per_run(self) -> None:
        prompt = PROMPT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(1, prompt.count("\n".join(PROMPT_LOOP_LINES) + "\n"))
        self.assertIn(f"after {PROMPT_COMMAND_CEILING} commands", prompt)
        self.assertNotIn("If that command exits with a non-zero code, stop the loop.", prompt)
        supervisor = SUPERVISOR_PATH.read_text(encoding="utf-8")
        for line in SUPERVISOR_STATUS_LINES:
            self.assertEqual(1, supervisor.count(line))
        harness = CoreHarness(self)
        current = run_id()
        harness.loop(current)
        with self.assertRaisesRegex(AssertionError, "exactly one loop per run ID"):
            harness.loop(current)

    def test_disposition_table_is_closed_and_covers_every_result_site(self) -> None:
        import ast
        import inspect

        from energygrid_bill_downloader import orchestration
        from energygrid_bill_downloader.orchestration import (
            CONTINUE_RESULTS, DISPOSITIONS, RUN_STOP_ANY_COMMAND, RUN_STOP_RESULTS, STREAM_STOPPED_RESULTS,
            result_disposition,
        )
        from energygrid_bill_downloader.state import V3_SCHEMA_SQL

        self.assertEqual(("CONTINUE", "STREAM_STOPPED", "RUN_STOP"), DISPOSITIONS)
        self.assertFalse(CONTINUE_RESULTS & STREAM_STOPPED_RESULTS or CONTINUE_RESULTS & RUN_STOP_RESULTS
                         or STREAM_STOPPED_RESULTS & RUN_STOP_RESULTS)
        for command, outcome, code in CONTINUE_RESULTS | STREAM_STOPPED_RESULTS | RUN_STOP_RESULTS:
            self.assertNotIn((outcome, code), RUN_STOP_ANY_COMMAND)
        # The stop table's CHECK vocabulary is exactly the stream-stop class.
        ddl = next(item for item in V3_SCHEMA_SQL if item.startswith("CREATE TABLE energygrid_run_stream_stop_v3"))
        vocab = {name: set(re.findall(r"'([a-zA-Z_-]+)'", re.search(rf"{name} [A-Z ]+CHECK\({name} IN \(([^)]*)\)", ddl).group(1)))
                 for name in ("command", "outcome")}
        self.assertEqual({item[0] for item in STREAM_STOPPED_RESULTS}, vocab["command"])
        self.assertEqual({item[1] for item in STREAM_STOPPED_RESULTS}, vocab["outcome"])
        self.assertEqual({10, 20}, {item[2] for item in STREAM_STOPPED_RESULTS})
        with self.assertRaises(StateError):
            result_disposition("drive-intent", "DRIVE_VERIFIED", 0)
        with self.assertRaises(StateError):
            result_disposition("drive-upload", "RESERVATION_UNAVAILABLE", 20)

        # Every _finish/_result call site in the module maps to exactly one class.
        tree = ast.parse(inspect.getsource(orchestration))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

        def literal_values(node, function) -> set:
            if isinstance(node, ast.Constant):
                return {node.value}
            if isinstance(node, ast.IfExp):
                return literal_values(node.body, function) | literal_values(node.orelse, function)
            if isinstance(node, ast.Name):
                assigned = [item.value for item in ast.walk(function) if isinstance(item, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == node.id for target in item.targets)]
                if len(assigned) == 1:
                    return literal_values(assigned[0], function)
            raise AssertionError(f"unresolved result field at line {node.lineno}")

        verdict = functions["_apply_verdict"]
        upload_branch = next(item for item in ast.walk(verdict) if isinstance(item, ast.If)
                             and ast.unparse(item.test) == "command == 'drive-upload'")
        upload_lines = range(upload_branch.lineno, upload_branch.end_lineno + 1)
        delivery_states = {"DELIVERY_OUTCOME_UNCERTAIN", "REQUEST_REJECTED"}  # DeliveryOutcome states other than DELIVERED
        sites = 0
        for name, function in functions.items():
            for call in ast.walk(function):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in {"_finish", "_result"}):
                    continue
                if name in {"_finish", "fallback_result"} or (name == "run_command" and call.func.id == "_result"):
                    continue  # the helper itself / refusal sites, checked below
                offset = 1 if call.func.id == "_finish" else 0
                command_node, outcome_node, code_node = call.args[offset], call.args[offset + 2], call.args[offset + 5]
                if isinstance(command_node, ast.Name) and command_node.id == "command" and name == "_apply_verdict":
                    commands = ({"drive-upload"} if call.lineno in upload_lines
                                else {"drive-reconcile"} if call.lineno > upload_branch.end_lineno
                                else {"drive-upload", "drive-reconcile"})
                else:
                    commands = literal_values(command_node, function)
                if ast.unparse(outcome_node) == "outcome.state":
                    outcomes = delivery_states
                else:
                    outcomes = literal_values(outcome_node, function)
                codes = literal_values(code_node, function)
                for command in commands:
                    for outcome in outcomes:
                        for code in codes:
                            sites += 1
                            with self.subTest(line=call.lineno, command=command, outcome=outcome, code=code):
                                self.assertIn(result_disposition(command, outcome, code), DISPOSITIONS)
        self.assertGreaterEqual(sites, 30)
        # run_command's own refusal/lock documents and the CLI fallback are RUN_STOP.
        for outcome, code in (("REFUSED", 64), ("RUN_IN_PROGRESS", 10), ("FAILED", 10), ("FAILED", 20)):
            for command in ("plan", "status", "acquire", "drive-upload", "submit-drive-result"):
                self.assertEqual("RUN_STOP", result_disposition(command, outcome, code))

    def test_new_fields_carry_only_fixed_words_and_booleans(self) -> None:
        harness = CoreHarness(self)
        self.apply_fault(harness, "EB_BILL", "drive_conflict")
        current = run_id()
        harness.loop(current)
        harness.run("deliver", "EB_BILL", run_id=current)
        result_keys = {"schema", "command", "stream", "outcome", "support_ref", "mutated", "disposition"}
        for text in harness.outputs:
            document = json.loads(text)
            if document["schema"] == "energygrid.core.result.v3":
                self.assertEqual(result_keys, set(document))
                self.assertIn(document["disposition"], ("CONTINUE", "STREAM_STOPPED", "RUN_STOP"))
            else:
                self.assertNotIn("disposition", document, "plan and status keep their schemas")
                for item in document["streams"].values():
                    self.assertIs(type(item["stopped_for_run"]), bool)
        joined = "\n".join(harness.outputs)
        forbidden = [*SYNTHETIC_FOLDERS.values(), "synthDriveFile", "2026-", "eb-latest", str(harness.archive),
                     "synthetic-drive-token", "egdb3-", "egmail-v1-", "SYNTHETIC-ENERGYGRID-NAMESPACE", current]
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, joined)
        with StateV3Store(harness.state_path, read_only=True) as state:
            row = state._conn().execute("SELECT * FROM energygrid_run_stream_stop_v3").fetchone()
        self.assertEqual((current, "EB_BILL", "drive-upload", "DRIVE_CONFLICT", 20, "EG_DRIVE_IDENTITY_TRASHED"), tuple(row[:6]))


class StatusPrecedenceTests(unittest.TestCase):
    """#226 G3 M1: an unfinished run is INCOMPLETE, never the ordinary production
    HOLD that an unbound Tenant Bill stream produces every day."""

    def production_harness(self) -> CoreHarness:
        # Production shape: Tenant Bill stays UNBOUND, so it HOLDs on every run.
        return CoreHarness(self, bound=("EB_BILL",), drive_bound=("EB_BILL",))

    def assert_tenant_unbound_hold(self, status: dict) -> None:
        tenant = status["streams"]["TENANT_BILL"]
        self.assertEqual(("HOLD", "HOLD"), (tenant["action"], tenant["outcome"]))

    def test_ordinary_tenant_unbound_daily_hold_remains_hold(self) -> None:
        harness = self.production_harness()
        current = run_id()
        harness.loop(current)
        status = harness.status(current)
        self.assert_tenant_unbound_hold(status)
        self.assertTrue(status["streams"]["EB_BILL"]["fully_handled"])
        self.assertEqual(("HOLD", True, False),
                         (status["business_outcome"], status["terminal"], status["uncertainty_outstanding"]))

    def test_unfinished_run_with_tenant_unbound_is_incomplete_not_hold(self) -> None:
        for command in ("drive-intent", "drive-upload", "deliver"):
            with self.subTest(stopped_before=command):
                harness = self.production_harness()
                current = run_id()
                harness.drive_until(current, "EB_BILL", command)
                status = harness.status(current)
                self.assert_tenant_unbound_hold(status)
                self.assertEqual("INCOMPLETE", status["streams"]["EB_BILL"]["outcome"])
                self.assertEqual(("INCOMPLETE", False), (status["business_outcome"], status["terminal"]))

    def test_open_drive_dispatch_with_tenant_unbound_is_incomplete_with_uncertainty(self) -> None:
        harness = self.production_harness()
        current = run_id()
        harness.drive_until(current, "EB_BILL", "drive-upload")
        operation = harness.operation("EB_BILL")
        with StateV3Store(harness.state_path) as state:
            state.reserve_drive_file_id(operation["operation_id"], "synthReservedOpenDispatch", current, "2026-10-06T00:00:00+00:00")
            state.begin_drive_dispatch(operation["operation_id"], current, "2026-10-06T00:00:01+00:00")
        status = harness.status(current)
        self.assert_tenant_unbound_hold(status)
        self.assertEqual("DRIVE_RECONCILE", status["streams"]["EB_BILL"]["action"])
        self.assertEqual(("INCOMPLETE", False, True),
                         (status["business_outcome"], status["terminal"], status["uncertainty_outstanding"]))

    def test_open_email_dispatch_with_tenant_unbound_is_incomplete_with_uncertainty(self) -> None:
        from energygrid_bill_downloader.delivery import DELIVERY_SCHEMA

        harness = self.production_harness()
        current = run_id()
        harness.drive_until(current, "EB_BILL", "deliver")
        with StateV3Store(harness.state_path) as state:
            invoice = state.invoice(state.stream("EB_BILL")["watermark_invoice_id"])
            metadata = {
                "schema": DELIVERY_SCHEMA, "stream": invoice["stream"], "bill_date": invoice["bill_date"],
                "attachment_name": invoice["canonical_filename"], "pdf_byte_size": invoice["byte_size"],
                "pdf_sha256": invoice["sha256"],
            }
            delivery_id = "egmail-v1-" + uuid.uuid4().hex
            state.prepare_delivery(invoice_id=invoice["invoice_id"], metadata=metadata, run_id=current,
                                   timestamp="2026-10-06T00:00:03+00:00", delivery_id=delivery_id)
            self.assertTrue(state.claim_delivery_dispatch(delivery_id, current, "2026-10-06T00:00:04+00:00"))
        status = harness.status(current)
        self.assert_tenant_unbound_hold(status)
        self.assertEqual("EMAIL_RECONCILE", status["streams"]["EB_BILL"]["action"])
        self.assertEqual(("INCOMPLETE", False, True),
                         (status["business_outcome"], status["terminal"], status["uncertainty_outstanding"]))
        self.assertEqual([], harness.delivery.sent)

    def test_terminal_success_remains_success(self) -> None:
        harness = CoreHarness(self)
        current = run_id()
        harness.loop(current)
        status = harness.status(current)
        self.assertEqual(("COMPLETED", True, False),
                         (status["business_outcome"], status["terminal"], status["uncertainty_outstanding"]))

    def test_outcome_precedence_mirrors_supervisor_severity_and_incomplete_outranks_hold(self) -> None:
        import re

        from energygrid_bill_downloader.orchestration import BUSINESS_OUTCOMES, OUTCOME_SEVERITY

        self.assertEqual(sorted(BUSINESS_OUTCOMES), sorted(OUTCOME_SEVERITY))
        self.assertLess(OUTCOME_SEVERITY.index("INCOMPLETE"), OUTCOME_SEVERITY.index("HOLD"))
        supervisor = (Path(__file__).resolve().parents[1] / "runtime" / "claude_supervisor.ps1").read_text(encoding="utf-8")
        severity = [int(item) for item in re.search(r"\$script:EgSeverity = @\(([^)]*)\)", supervisor).group(1).split(",")]
        mapping_block = re.search(r"\$script:EgOutcomeExit = @\{(.*?)\n\}", supervisor, re.S).group(1)
        mapping = {name: int(code) for name, code in re.findall(r"'([A-Z_]+)' = (\d+)", mapping_block)}
        self.assertEqual(sorted(BUSINESS_OUTCOMES), sorted(mapping))
        ranks = [severity.index(mapping[outcome]) for outcome in OUTCOME_SEVERITY]
        self.assertEqual(sorted(ranks), ranks, "core precedence must follow the supervisor exit-code severity")


if __name__ == "__main__":
    unittest.main()
