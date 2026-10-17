"""Claude-free daily run for the EnergyGrid native Core (#226 V3 G1, Web #226:6233858196).

    python -m energygrid_bill_downloader.native_cli daily --config <private native config with `daily`>

The run drives the existing Core functions in the order the Claude host used to (host protocol,
docs/native_connector_bridge.md section 4), with no model in the loop:

    new run folder -> acquire -> acquire.json
    bounded loop of `next`:
        DRIVE_PRECHECK           Drive Verify listing -> drive-seal (the Core writes the synced file)
                                 -> poll Drive Verify -> drive-record once Google's SHA-256 matches
        RECONCILE (from seal)    drive-adopt with the same listing (exact bytes only)
        DRIVE_RECONCILE_PENDING  the same bounded poll for a sealed-but-unrecorded upload
        NOTIFY_DRAFT             one PROBE, then seal_n8n -> one POST -> record
    status -> status.json -> daily.json, and one result line

Every decision stays in the Core: this module never marks anything VERIFIED or SENT itself, never
retries a seal or a send, and never reads the v4 GMAIL_SEND intents. Drive Verify answers become
evidence only through `native_bridge.record_remote_listing`.

Failure visibility (C6, docs/native_daily_claude_free.md section 5): the result line says
`monitor_report: SUBMIT` only when every FAILURE notification this run called for was delivered and the
run itself completed. Otherwise it says WITHHELD, the scheduler script does not run the unchanged monitor
reporter, and the unchanged n8n monitor raises its one missed-report alert at 14:00 SGT.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import native_bridge as bridge
from . import native_notify as notify
from . import n8n_loopback as loopback
from .errors import AppError

DAILY_RESULT_SCHEMA = "energygrid.native_daily.result.v1"
DAILY_EVIDENCE_SCHEMA = "energygrid.native_daily.run.v1"
PREFLIGHT_SCHEMA = "energygrid.native_daily.preflight.v1"
RUN_DIR_FORMAT = "%Y%m%d-%H%M%S"
MAX_STEPS = 40
SUBMIT, WITHHELD = "SUBMIT", "WITHHELD"
# drive-record / drive-adopt refusals that mean "not yet": the poll waits and asks again.
_RETRY_REFS = frozenset({
    "EG_DRIVE_REMOTE_FILE_NOT_LISTED", "EG_DRIVE_REMOTE_SHA256_UNAVAILABLE", "EG_NATIVE_DRIVE_READBACK_REQUIRED",
    "EG_DRIVE_LISTING_BEFORE_SEAL", "EG_DRIVE_LISTING_STALE",
})


class Clock:
    """Real time. Tests inject a fake whose sleep advances the clock."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def local_now(self) -> datetime:
        return datetime.now()  # the same naive local clock as `egnative.cmd new-run` and the monitor

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def client_for(cfg: bridge.NativeConfig) -> loopback.LoopbackClient:
    assert cfg.daily is not None
    return loopback.LoopbackClient(cfg.daily.n8n_port, cfg.daily.token_file, cfg.daily.header_name)


def _line(result: dict) -> bytes:
    return (json.dumps(result, sort_keys=True) + "\n").encode("utf-8")


def _write_once(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()


def _refusal(command: str, exc: AppError) -> dict:
    support_ref = getattr(exc, "support_ref", None) or "EG_NATIVE_REFUSED"
    exit_code = exc.exit_code if exc.exit_code in (10, 20, 64) else 64
    return bridge._result(command, "REFUSED" if exit_code == 64 else exc.status, exit_code, support_ref=support_ref,
                          error_class=type(exc).__name__)


class DailyRun:
    """One daily run. Holds no lease between Core commands; each Core command takes its own."""

    def __init__(self, cfg: bridge.NativeConfig, state, client: Any, clock: Clock) -> None:
        assert cfg.daily is not None
        self.cfg, self.state, self.client, self.clock = cfg, state, client, clock
        self.run_id = bridge.new_run_id()
        self.deadline_ms = clock.now_ms() + cfg.daily.run_deadline_minutes * 60_000
        self.steps: list[dict] = []
        self.withheld: list[str] = []
        self.email: dict[str, Any] = {"probe": None, "sent": 0, "uncertain": 0, "held": 0}
        self.email_ready: bool | None = None
        self.tried_reconcile: set[str] = set()
        self.tried_precheck: set[str] = set()

    # -- small helpers -----------------------------------------------------

    def now(self) -> int:
        return self.clock.now_ms()

    def note(self, kind: str, outcome: str, **fields: Any) -> None:
        self.steps.append({"step": kind, "outcome": outcome, **{k: v for k, v in fields.items() if v is not None}})

    def withhold(self, reason: str) -> None:
        if reason not in self.withheld:
            self.withheld.append(reason)

    def core(self, command: str, function: Callable[..., dict], *args: Any, **kwargs: Any) -> dict:
        """Run one Core command; a closed refusal comes back as its CLI-shaped result."""
        try:
            return function(*args, **kwargs)
        except AppError as exc:
            return _refusal(command, exc)

    # -- Drive Verify --------------------------------------------------------

    def listing(self) -> dict | None:
        """One Drive Verify answer recorded as Core evidence, or None (unavailable or refused)."""
        nonce = bridge.new_nonce()
        answer = self.client.post(loopback.VERIFY_PATH, {"schema": bridge.LISTING_REQUEST_SCHEMA, "nonce": nonce},
                                  loopback.VERIFY_TIMEOUT_SECONDS)
        if answer.outcome != loopback.OK:
            self.note("drive-verify", "UNAVAILABLE", support_ref=answer.support_ref or "EG_DAILY_VERIFY_UNAVAILABLE",
                      http_status=answer.status)
            return None
        try:
            return bridge.record_remote_listing(self.cfg, nonce, answer.body, bridge._iso(self.now()))
        except AppError as exc:
            self.note("drive-verify", "REFUSED", support_ref=getattr(exc, "support_ref", None))
            return None

    def await_verified(self, intent_id: str, title: str, stream: str) -> bool:
        """Bounded poll after a seal (or for a sealed-but-unrecorded upload): record once Google's SHA-256
        and the local mirror match. Returns False when it ends without a record (duplicates, or the window
        ended): the caller then stops the run, because the next `next` would treat the still-DISPATCHED
        intent as a crash and make it UNKNOWN (host rule P1 stopped the loop for the same reason)."""
        window_end = min(self.deadline_ms, self.now() + self.cfg.daily.verify_window_minutes * 60_000)
        polls = 0
        while True:
            polls += 1
            listing = self.listing()
            if listing is not None:
                occupants = bridge.name_occupants(listing, title)
                if len(occupants) > 1:
                    # Host rule P1: never record, adopt or touch anything; an operator decides.
                    self.note("drive-await", "DUPLICATE_NAME", stream=stream, intent_id=intent_id,
                              support_ref="EG_DAILY_DRIVE_DUPLICATE_NAME", polls=polls)
                    return False
                if occupants:
                    result = self.core("drive-record", bridge.drive_record, self.state, self.cfg, intent_id,
                                       {"outcome": "CREATED", "listing_nonce": listing["nonce"]},
                                       run_id=self.run_id, now_ms=self.now())
                    if result.get("support_ref") not in _RETRY_REFS:
                        self.note("drive-record", result["outcome"], stream=stream, intent_id=intent_id,
                                  support_ref=result.get("support_ref"), polls=polls)
                        return result["exit_code"] != bridge.EXIT_REFUSED
            if self.now() + self.cfg.daily.verify_poll_seconds * 1000 > window_end:
                # Late sync: the intent stays as it is; the next run offers DRIVE_RECONCILE_PENDING.
                self.note("drive-await", "SYNC_PENDING", stream=stream, intent_id=intent_id,
                          support_ref="EG_DAILY_DRIVE_SYNC_PENDING", polls=polls)
                return False
            self.clock.sleep(self.cfg.daily.verify_poll_seconds)

    def precheck(self, step: dict) -> bool:
        """DRIVE_PRECHECK. Returns False when the loop must stop."""
        intent_id, stream, title = step["intent_id"], step["stream"], step["display_filename"]
        listing = self.listing()
        if listing is None:
            return False
        evidence = {"listing_nonce": listing["nonce"]}
        sealed = self.core("drive-seal", bridge.drive_seal, self.state, self.cfg, intent_id, evidence,
                           run_id=self.run_id, now_ms=self.now())
        outcome = sealed["outcome"]
        self.note("drive-seal", outcome, stream=stream, intent_id=intent_id, support_ref=sealed.get("support_ref"))
        if outcome == "SEALED":
            return self.await_verified(intent_id, title, stream)
        if outcome == "RECONCILE":
            adopted = self.core("drive-adopt", bridge.drive_adopt, self.state, self.cfg, intent_id, evidence,
                                run_id=self.run_id, now_ms=self.now())
            self.note("drive-adopt", adopted["outcome"], stream=stream, intent_id=intent_id,
                      support_ref=adopted.get("support_ref"))
            return adopted["exit_code"] != bridge.EXIT_REFUSED
        return outcome == "HOLD"

    # -- owner email ---------------------------------------------------------

    def probe(self) -> bool:
        """One PROBE per run (no send)."""
        verdict = probe_email(self.cfg, self.client)
        self.email["probe"] = verdict
        return verdict == "READY"

    def send(self, notification_id: str) -> None:
        """Seal, one POST, record: inside one lease session, exactly as the CLI notify commands run."""
        from .native_notify import NotificationStore, store_path

        now_iso = bridge._iso(self.now())
        try:
            with bridge._session(self.state, self.now(), self.cfg), NotificationStore(store_path(self.cfg)) as store:
                request = notify.seal_n8n(store, self.cfg, notification_id, now_iso)
                answer = self.client.post(loopback.EMAIL_PATH, request, loopback.EMAIL_TIMEOUT_SECONDS)
                evidence = email_evidence(request, answer)
                recorded = notify.record(store, notification_id, evidence, bridge._iso(self.now()))
        except AppError as exc:
            result = _refusal("notify-n8n", exc)
            self.note("notify", result["outcome"], notification_id=notification_id, support_ref=result["support_ref"])
            if result["support_ref"] == "EG_DAILY_EMAIL_DISPATCH_MARKER_EXISTS":
                self.email["held"] += 1
            return
        key = {"SENT": "sent", "UNKNOWN": "uncertain", "HOLD": "held"}.get(recorded["outcome"])
        if key:
            self.email[key] += 1
        self.note("notify", recorded["outcome"], notification_id=notification_id,
                  support_ref=evidence.get("support_ref"))

    # -- the loop ------------------------------------------------------------

    def loop(self) -> None:
        for _ in range(MAX_STEPS):
            if self.now() >= self.deadline_ms:
                self.withhold("EG_DAILY_DEADLINE_REACHED")
                return
            step = self.core("next", bridge.next_step, self.state, self.cfg, run_id=self.run_id, now_ms=self.now(),
                             plan_notifications=self.email_ready is not False)
            outcome = step["outcome"]
            if outcome == "DRIVE_PRECHECK":
                if step["intent_id"] in self.tried_precheck:
                    return  # one attempt per intent per run (for example Google's SHA-256 not ready yet)
                self.tried_precheck.add(step["intent_id"])
                if not self.precheck(step):
                    return
            elif outcome == "DRIVE_RECONCILE_PENDING":
                intent_id = step["intent_id"]
                if intent_id in self.tried_reconcile:
                    return  # once per run, as the host did; the next run offers it again
                self.tried_reconcile.add(intent_id)
                if not self.await_verified(intent_id, step["display_filename"], step["stream"]):
                    return
            elif outcome == "NOTIFY_DRAFT":
                if self.email_ready is None:
                    self.email_ready = self.probe()
                if self.email_ready:
                    self.send(step["notification_id"])
                # Not ready: the next `next` leaves notifications PENDING and plans Drive work only.
            elif outcome == "HOLD":
                self.note("next", "HOLD", stream=step.get("stream"), support_ref=step.get("support_ref"))
            else:
                # IDLE, CEILING_REACHED, NOTIFY_CEILING_REACHED, RETRY_LATER, a refusal: stop this run.
                self.note("next", outcome, stream=step.get("stream"), support_ref=step.get("support_ref"))
                if outcome == "REFUSED" or step.get("exit_code") == bridge.EXIT_REFUSED:
                    self.withhold("EG_DAILY_CORE_REFUSED")
                return
        self.withhold("EG_DAILY_STEP_LIMIT_REACHED")


def probe_email(cfg: bridge.NativeConfig, client: Any) -> str:
    """READY only when n8n is healthy, the Owner Email workflow answers the PROBE (no send) with the echoed
    nonce, its sender credential's account equals `gmail.account` and its fixed recipient hashes to the
    configured one. Otherwise a closed reference; nothing is sealed."""
    if not client.health():
        return "EG_DAILY_N8N_UNHEALTHY"
    nonce = bridge.new_nonce()
    answer = client.post(loopback.EMAIL_PATH, {"schema": notify.EMAIL_REQUEST_SCHEMA, "mode": "PROBE", "nonce": nonce},
                         loopback.EMAIL_TIMEOUT_SECONDS)
    body = answer.body
    recipient = hashlib.sha256(cfg.gmail.to[0].lower().encode("utf-8")).hexdigest()
    ok = (answer.outcome == loopback.OK and type(body) is dict
          and body.get("schema") == notify.EMAIL_RESPONSE_SCHEMA and body.get("mode") == "PROBE"
          and body.get("nonce") == nonce and body.get("ready") is True
          and type(body.get("sender_account")) is str and body["sender_account"].lower() == cfg.gmail.account.lower()
          and body.get("recipient_sha256") == recipient)
    return "READY" if ok else "EG_DAILY_EMAIL_PROBE_FAILED"


def _failure_states(cfg: bridge.NativeConfig) -> dict[str, str]:
    from .native_notify import KIND_FAILURE, NotificationStore, store_path

    with NotificationStore(store_path(cfg)) as store:
        return {row["notification_id"]: row["state"] for row in store.rows("kind=?", (KIND_FAILURE,))}


def email_evidence(request: dict, answer: loopback.Answer) -> dict:
    """Map the one n8n answer to typed record evidence. Only an exact SENT echo is SENT; a closed refusal
    before any send, or no request at all, is DENIED (HOLD); everything else is uncertain (UNKNOWN)."""
    base = {"transport": notify.TRANSPORT_N8N, "draft_id": request["dispatch_id"]}
    body = answer.body if type(answer.body) is dict else {}
    echoed = (body.get("schema") == notify.EMAIL_RESPONSE_SCHEMA and body.get("mode") == "SEND"
              and body.get("notification_id") == request["notification_id"]
              and body.get("dispatch_id") == request["dispatch_id"])
    message_id = body.get("message_id")
    if (answer.outcome == loopback.OK and echoed and body.get("outcome") == "SENT" and type(message_id) is str
            and bridge._PROVIDER_ID_RE.fullmatch(message_id)):
        return {**base, "outcome": "SENT", "message_id": message_id}
    if answer.outcome == loopback.NOT_SENT:
        return {**base, "outcome": "DENIED", "support_ref": answer.support_ref}
    if answer.status == 422 and body.get("outcome") == "REFUSED" and body.get("schema") == notify.EMAIL_RESPONSE_SCHEMA:
        return {**base, "outcome": "DENIED", "support_ref": "EG_DAILY_EMAIL_REFUSED"}
    if answer.status in (403, 404):
        # n8n refused the request before running the workflow (Header Auth or no active webhook).
        return {**base, "outcome": "DENIED", "support_ref": "EG_DAILY_EMAIL_WEBHOOK_REFUSED"}
    if answer.outcome == loopback.SENT_UNCERTAIN:
        return {**base, "outcome": "NO_RESPONSE", "support_ref": answer.support_ref}
    return {**base, "outcome": "ERROR", "support_ref": "EG_DAILY_EMAIL_OUTCOME_UNCERTAIN"}


def _new_run_dir(root: Path, now: datetime) -> Path:
    from .publication import ensure_no_reparse_components

    ensure_no_reparse_components(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / now.strftime(RUN_DIR_FORMAT)
    path.mkdir()  # never reuse a folder: one run, one report
    return path


def run(cfg: bridge.NativeConfig, adapters: Callable[[], dict], *, client: Any = None, clock: Clock | None = None) -> dict:
    """The whole daily run. `adapters` builds the portal adapters (called once, inside the run)."""
    from .state import StateV4Store

    if cfg.daily is None:
        raise bridge.NativeRefusal("EG_DAILY_CONFIG_REQUIRED")
    clock = clock or Clock()
    client = client or client_for(cfg)
    run_dir = _new_run_dir(cfg.daily.runs_root, clock.local_now())
    started = bridge._iso(clock.now_ms())
    bridge.latch_remote_policy(cfg, started)
    acquired: dict = {}
    status: dict = {}
    with StateV4Store(cfg.state_path) as state:
        daily = DailyRun(cfg, state, client, clock)
        before = _failure_states(cfg)
        try:
            acquired = bridge.acquire(state, cfg, adapters(), run_id=daily.run_id, now_ms=clock.now_ms())
        except AppError as exc:
            acquired = _refusal("acquire", exc)
        _write_once(run_dir / "acquire.json", _line(acquired))
        if acquired.get("outcome") == "ACQUIRED":
            daily.loop()
        else:
            daily.withhold("EG_DAILY_ACQUIRE_REFUSED")
        try:
            status = bridge.status(state, cfg, clock.now_ms())
        except AppError as exc:
            status = _refusal("status", exc)
            daily.withhold("EG_DAILY_STATUS_REFUSED")
        _write_once(run_dir / "status.json", _line(status))
    after = _failure_states(cfg)
    if any(state == "PENDING" for state in after.values()):
        daily.withhold("EG_DAILY_FAILURE_NOTICE_UNDELIVERED")
    if any(state in ("UNKNOWN", "HOLD", "SEALED") and before.get(nid) != state for nid, state in after.items()):
        daily.withhold("EG_DAILY_FAILURE_NOTICE_UNCERTAIN")
    report = WITHHELD if daily.withheld else SUBMIT
    evidence = {
        "schema": DAILY_EVIDENCE_SCHEMA, "run_id": daily.run_id, "started_at": started,
        "finished_at": bridge._iso(clock.now_ms()), "acquire_outcome": acquired.get("outcome"),
        "status_outcome": status.get("outcome"), "steps": daily.steps, "email": daily.email,
        "monitor_report": report, "withheld_reasons": daily.withheld,
    }
    _write_once(run_dir / "daily.json", (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    exit_code = bridge.EXIT_OK if report == SUBMIT else bridge.EXIT_HOLD
    return bridge._result("daily", "COMPLETED" if report == SUBMIT else "WITHHELD", exit_code,
                          run_dir=str(run_dir), monitor_report=report, withheld_reasons=daily.withheld,
                          status_outcome=status.get("outcome"))


def preflight(cfg: bridge.NativeConfig, *, client: Any = None, now_ms: int | None = None) -> dict:
    """Read-only cutover check (docs/native_daily_claude_free.md section 8). Writes only listing evidence."""
    import sqlite3

    from .native_notify import store_path
    from .state import StateV4Store

    if cfg.daily is None:
        raise bridge.NativeRefusal("EG_DAILY_CONFIG_REQUIRED")
    client = client or client_for(cfg)
    now_ms = now_ms if now_ms is not None else Clock().now_ms()
    checks: dict[str, str] = {}
    with StateV4Store(cfg.state_path) as state:
        verified = []
        for stream in bridge.STREAM_LABELS:
            rows = [row for row in state.invoices_for_stream(stream)
                    if state.intents_for_invoice(row["invoice_key"])["DRIVE_UPLOAD"]["state"] == "VERIFIED"]
            verified.extend(rows)
            checks[f"latest_verified_{stream}"] = "PRESENT" if rows else "NONE"
        latest = {stream: [row for row in verified if row["stream"] == stream][-1:] for stream in bridge.STREAM_LABELS}
        notifications_path = store_path(cfg)
        if not notifications_path.exists():
            checks["notification_history"] = "HOLD_MISSING" if verified else "PASS_EMPTY"
        else:
            connection = sqlite3.connect(notifications_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                counts = dict(connection.execute("SELECT state, COUNT(*) FROM notification GROUP BY state").fetchall())
                keys = {row[0] for row in connection.execute("SELECT dedup_key FROM notification")}
            finally:
                connection.close()
            in_flight = counts.get("PENDING", 0) + counts.get("SEALED", 0)
            missing_success = [row for row in verified if f"SUCCESS|{row['invoice_key']}" not in keys]
            checks["notification_in_flight"] = "PASS" if in_flight == 0 else "HOLD_PENDING_OR_SEALED"
            checks["notification_success_history"] = "PASS" if not missing_success else "HOLD_UNNOTIFIED_VERIFIED"
            checks["notification_sent"] = str(counts.get("SENT", 0))
        checks["legacy_gmail_send_pending"] = str(sum(
            1 for stream in bridge.STREAM_LABELS for row in state.invoices_for_stream(stream)
            if state.intents_for_invoice(row["invoice_key"]).get("GMAIL_SEND", {}).get("state") == "PENDING"))
    checks["remote_policy_latched"] = "YES" if bridge.remote_policy_latched(cfg) else "NO"
    checks["n8n_health"] = "PASS" if client.health() else "HOLD_UNHEALTHY"
    verdict = probe_email(cfg, client)
    checks["owner_email_probe"] = "PASS" if verdict == "READY" else "HOLD_" + verdict
    nonce = bridge.new_nonce()
    answer = client.post(loopback.VERIFY_PATH, {"schema": bridge.LISTING_REQUEST_SCHEMA, "nonce": nonce},
                         loopback.VERIFY_TIMEOUT_SECONDS)
    listing = None
    if answer.outcome == loopback.OK:
        try:
            listing = bridge.record_remote_listing(cfg, nonce, answer.body, bridge._iso(now_ms))
        except AppError as exc:
            checks["drive_verify"] = "HOLD_" + str(getattr(exc, "support_ref", "EG_DRIVE_LISTING_INVALID"))
    else:
        checks["drive_verify"] = "HOLD_" + str(answer.support_ref or answer.status)
    if listing is not None:
        checks["drive_verify"] = "PASS"
        for stream, rows in latest.items():
            if not rows:
                continue
            occupants = bridge.name_occupants(listing, rows[0]["display_filename"])
            verdict = bridge.remote_verdict(cfg, rows[0], occupants[0]) if len(occupants) == 1 else "NOT_UNIQUE"
            checks[f"google_sha256_{stream}"] = "PASS" if verdict == "MATCH" else "HOLD_" + verdict
    holds = sorted(name for name, value in checks.items() if value.startswith("HOLD"))
    return bridge._result("daily-preflight", "PASS" if not holds else "HOLD",
                          bridge.EXIT_OK if not holds else bridge.EXIT_HOLD, schema_detail=PREFLIGHT_SCHEMA,
                          checks=checks, holds=holds, listing_nonce=listing["nonce"] if listing else None)
