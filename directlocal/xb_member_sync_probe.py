"""#155 P2 (G0 r2, slice P2): the sign-up probe.

Run every minute by the scheduled task ``\\XB\\XB-MemberSync-Probe``
(``scripts/install_member_sync_task.ps1 -Probe``). One small Shopify search
for ``member-mg`` customers updated since the sync's own watermark, reading
only the customer ID, tags and ``updatedAt`` (never a contact field). It
compares them with the sync ledger, opened read-only with no lock exactly as
``digest`` does, and when a sign-up is neither linked nor known to the ledger
it starts the existing task ``\\XB\\XB-MemberSync`` on demand.

Never here: the AutoCount bridge, an AutoCount sign-in, SQL Server, a ledger
lock or a ledger write, any Shopify mutation. The only process it may start
is ``schtasks.exe /Run /TN \\XB\\XB-MemberSync`` (pinned); Task Scheduler then
applies the main task's own ``IgnoreNew``, ``PT10M`` and the ledger lock.

Wake rules (owner decisions 4 Nov 2026, G0 r2 section 6):
* only for new work (a sign-up the ledger has never seen), once per piece of
  work (the newest ``updatedAt`` already woken for is kept as a marker);
* no start within 60 s of the last run start or wake, none while a run is
  unfinished (started less than the 10-minute limit ago), none within 120 s
  after a run was hard-stopped at that limit (an orphan write-once child may
  still be running);
* only with ``probe_wake_enabled`` (otherwise "would wake", for observe),
  with the immediate path on, inside its window and under the caps;
* at most ``probe_max_wakes_per_day`` wakes per Singapore day.

The status file (``member_sync_probe_status.json`` in the state root) holds
counts, timestamps and reason codes only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

import xb_directlocal as dl
import xb_member_sync as sync
import xb_shopify_direct as direct

PROBE_SCHEMA = "xb.directlocal.member_sync.probe.v1"
PROBE_STATUS_SCHEMA = "xb.directlocal.member_sync.probe_status.v1"
PROBE_STATUS_FILE = "member_sync_probe_status.json"
PROBE_STATUS_MAX_BYTES = 65_536
STATUS_DAYS_KEPT = 14
MAIN_TASK = "\\XB\\XB-MemberSync"
START_DEBOUNCE = timedelta(seconds=60)
RUN_TIME_LIMIT = timedelta(minutes=10)  # the main task's PT10M
HARD_STOP_GRACE = timedelta(seconds=120)  # orphan write-once child deadline
PROBE_PAGE_SIZE = 50
PROBE_MAX_PAGES = 2
SHOPIFY_DEADLINE_SECONDS = 40  # well inside the probe task's PT2M limit
TASK_START_TIMEOUT_SECONDS = 30

PROBE_QUERY = """query XbMemberSyncSignUpProbe($first: Int!, $after: String, $search: String!) {
  customers(first: $first, after: $after, sortKey: UPDATED_AT, reverse: true, query: $search) {
    pageInfo { hasNextPage endCursor }
    nodes { id tags updatedAt }
  }
}"""
PROBE_NODE_KEYS = frozenset({"id", "tags", "updatedAt"})
PROBE_QUERIES = frozenset({direct.SESSION_QUERY, PROBE_QUERY})
DECISIONS = ("wake", "would_wake", "no_wake", "no_work", "wake_failed", "refused")


@dataclass(frozen=True)
class SignUp:
    gid: str
    tags: tuple[str, ...]
    updated_at: datetime

    def __repr__(self) -> str:
        return "SignUp()"


class ProbeShopifyClient(direct.ShopifyDirectClient):
    """Exactly two read queries: the session check and ``PROBE_QUERY``."""

    # The sync's own store and scope check (the scope set it expects follows
    # shopify_create_enabled, as for the run); no other method is reused.
    verify_session = sync.SyncShopifyClient.verify_session

    def __init__(self, *, create_enabled: bool, **kwargs: Any):
        super().__init__(**kwargs)
        self.create_enabled = create_enabled  # scope expectation only

    def __repr__(self) -> str:
        return "ProbeShopifyClient()"

    def _query_allowed(self, query: str) -> bool:
        return query in PROBE_QUERIES

    def sign_ups(self, search: str, max_pages: int) -> tuple[list[SignUp], bool]:
        found: list[SignUp] = []
        after = None
        for _ in range(max_pages):
            data = self.execute(PROBE_QUERY, {"first": PROBE_PAGE_SIZE, "after": after, "search": search})
            page = data.get("customers")
            if not isinstance(page, dict) or not isinstance(page.get("nodes"), list) or not isinstance(page.get("pageInfo"), dict):
                raise direct.ShopifyDirectError("shopify_response_shape_invalid")
            found.extend(_sign_up_from_node(node) for node in page["nodes"])
            if not page["pageInfo"].get("hasNextPage"):
                return found, True
            after = page["pageInfo"].get("endCursor")
            if not isinstance(after, str) or not after:
                raise direct.ShopifyDirectError("shopify_response_shape_invalid")
        return found, False


def _sign_up_from_node(node: Any) -> SignUp:
    if not isinstance(node, dict) or not set(node) <= PROBE_NODE_KEYS or not isinstance(node.get("id"), str) or not dl.GID_RE.fullmatch(node["id"]):
        raise direct.ShopifyDirectError("shopify_response_shape_invalid")
    tags = node.get("tags") if isinstance(node.get("tags"), list) else []
    updated = dl.parse_created_at(node.get("updatedAt"))
    if updated is None:
        raise direct.ShopifyDirectError("shopify_response_shape_invalid")
    return SignUp(node["id"], tuple(t for t in tags if isinstance(t, str)), updated)


# --------------------------------------------------------------------------
# The ledger, read-only (no lock, no migration, no write), as digest
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LedgerFacts:
    initial_link_done: bool
    since: str
    known: frozenset[str]  # linked, or with any pending row: the run has seen them
    last_run_started: datetime | None
    last_run_finished: bool
    writes_last_hour: int
    writes_last_day: int


def _open_read_only(root: Path, config: sync.SyncConfig) -> sqlite3.Connection:
    path = Path(root) / sync.LEDGER_FILE
    if not path.is_file():
        raise dl.DirectLocalError("sync_ledger_missing")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        raise dl.DirectLocalError("sync_ledger_unreadable") from None
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] not in (2, sync.SCHEMA_VERSION):
            raise dl.DirectLocalError("sync_ledger_schema_unsupported")
        meta = conn.execute("SELECT autocount_book, store_gid, shop_domain FROM meta WHERE id = 1").fetchone()
        if meta is None:
            raise dl.DirectLocalError("sync_ledger_not_initialised")
        sync.check_binding({"autocount_book": meta[0], "store_gid": meta[1], "shop_domain": meta[2]}, config)
    except BaseException:
        conn.close()
        raise
    return conn


def _watermark(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT value FROM watermarks WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def ledger_facts(conn: sqlite3.Connection, config: sync.SyncConfig, gids: list[str], now: datetime) -> LedgerFacts:
    since = _watermark(conn, "shopify_updated_at") or dl.ts(config.shopify_cutover_utc)
    known = set()
    for gid in gids:
        if conn.execute("SELECT 1 FROM links WHERE customer_gid = ? UNION ALL SELECT 1 FROM shopify_pending WHERE customer_gid = ?", (gid, gid)).fetchone():
            known.add(gid)
    last = conn.execute("SELECT started_at, finished_at FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()

    def writes_since(span: timedelta) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM writes WHERE created_at >= ?", (dl.ts(now - span),)).fetchone()[0])

    return LedgerFacts(
        initial_link_done=_watermark(conn, sync.INITIAL_LINK_DONE_MARK) is not None, since=since, known=frozenset(known),
        last_run_started=dl.parse_created_at(last[0]) if last else None, last_run_finished=bool(last and last[1]),
        writes_last_hour=writes_since(timedelta(hours=1)), writes_last_day=writes_since(timedelta(hours=24)),
    )


def search_text(since: str) -> str:
    return f"tag:'{sync.REGISTRATION_TAG}' AND updated_at:>='{since}'"


# --------------------------------------------------------------------------
# Status file (counts, timestamps and reason codes only)
# --------------------------------------------------------------------------

def sgt_day(moment: datetime) -> str:
    return moment.astimezone(sync.SGT).strftime("%Y-%m-%d")


def _default_status() -> dict[str, Any]:
    return {"schema": PROBE_STATUS_SCHEMA, "mode": "observe", "last_check_at": None, "last_decision": None, "last_reason": None,
            "last_wake_at": None, "last_would_wake_at": None, "work_marker": None, "last_counts": {}, "days": {}}


def _count_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and dl.CODE_RE.fullmatch(k) and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000_000 for k, v in value.items())


def _valid_status(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(_default_status()) or value["schema"] != PROBE_STATUS_SCHEMA or value["mode"] not in ("observe", "wake"):
        return False
    if any(value[k] is not None and (not isinstance(value[k], str) or dl.parse_created_at(value[k]) is None)
           for k in ("last_check_at", "last_wake_at", "last_would_wake_at", "work_marker")):
        return False
    if any(value[k] is not None and (not isinstance(value[k], str) or not dl.CODE_RE.fullmatch(value[k])) for k in ("last_decision", "last_reason")):
        return False
    days = value["days"]
    return _count_map(value["last_counts"]) and isinstance(days, dict) and len(days) <= STATUS_DAYS_KEPT + 1 and all(
        isinstance(day, str) and sync.DATE_RE.fullmatch(day) and isinstance(entry, dict) and set(entry) == {"checks", "wakes", "would_wakes", "reasons"}
        and _count_map({k: entry[k] for k in ("checks", "wakes", "would_wakes")}) and _count_map(entry["reasons"])
        for day, entry in days.items())


def read_status(root: Path) -> tuple[dict[str, Any] | None, bool]:
    """``(status, valid)``; ``(None, True)`` when there is no file yet."""

    path = Path(root) / PROBE_STATUS_FILE
    try:
        if not path.exists():
            return None, True
        raw = path.read_bytes()
        if len(raw) > PROBE_STATUS_MAX_BYTES:
            return None, False
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None, False
    return (value, True) if _valid_status(value) else (None, False)


def load_status(root: Path, now: datetime, wake_cap: int) -> dict[str, Any]:
    """A missing file starts empty. An unreadable one fails closed: today's
    wakes count as spent (no wake until the next Singapore day)."""

    value, valid = read_status(root)
    if value is not None:
        return value
    status = _default_status()
    if not valid:
        status["days"][sgt_day(now)] = {"checks": 0, "wakes": wake_cap, "would_wakes": 0, "reasons": {"probe_status_reset": 1}}
    return status


def save_status(root: Path, status: dict[str, Any]) -> None:
    path = Path(root) / PROBE_STATUS_FILE
    temporary = Path(root) / f".{PROBE_STATUS_FILE}.{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(status, sort_keys=True, separators=(",", ":")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise dl.DirectLocalError("probe_status_unwritable") from None


def _today(status: dict[str, Any], now: datetime) -> dict[str, Any]:
    day = sgt_day(now)
    entry = status["days"].setdefault(day, {"checks": 0, "wakes": 0, "would_wakes": 0, "reasons": {}})
    for old in sorted(status["days"])[:-STATUS_DAYS_KEPT]:
        del status["days"][old]
    return entry


# --------------------------------------------------------------------------
# Decision (pure)
# --------------------------------------------------------------------------

def decide(*, now: datetime, config: sync.SyncConfig, facts: LedgerFacts, sign_ups: list[SignUp], status: dict[str, Any]) -> tuple[str, str, str | None]:
    """``(decision, reason, work marker)``. Only ``wake`` starts the task."""

    if not facts.initial_link_done:
        return "no_work", "initial_link_not_complete", None  # the run reads no sign-up yet
    new = [s for s in sign_ups if sync.REGISTRATION_TAG in s.tags and s.gid not in facts.known]
    if not new:
        return "no_work", "no_new_sign_up", None
    marker = dl.ts(max(s.updated_at for s in new))
    if status["work_marker"] is not None and marker <= status["work_marker"]:
        return "no_wake", "work_already_woken", None
    if facts.last_run_started is not None and not facts.last_run_finished:
        age = now - facts.last_run_started
        if age < RUN_TIME_LIMIT:
            return "no_wake", "run_unfinished", None
        if age < RUN_TIME_LIMIT + HARD_STOP_GRACE:
            return "no_wake", "run_hard_stopped_recently", None
    observe = not config.probe_wake_enabled
    starts = [facts.last_run_started, *(dl.parse_created_at(status[k]) for k in ("last_wake_at", "last_would_wake_at") if status[k] and (observe or k == "last_wake_at"))]
    latest = max((s for s in starts if s is not None), default=None)
    if latest is not None and now - latest < START_DEBOUNCE:
        return "no_wake", "debounce_recent_start", None
    if observe:
        return "would_wake", "observe_mode", marker
    if not config.immediate_path_active:
        return "would_wake", "immediate_create_off", marker
    if not sync.in_create_window(now, config.immediate_create_window_local):
        return "no_wake", "outside_create_window", None
    if facts.writes_last_hour >= config.max_creates_per_hour or facts.writes_last_day >= config.max_creates_per_day:
        return "no_wake", "create_rate_capped", None
    if _today(status, now)["wakes"] >= config.probe_max_wakes_per_day:
        return "no_wake", "wake_cap_reached", None
    return "wake", "new_sign_up", marker


# --------------------------------------------------------------------------
# Starting the main task (the only process the probe may start)
# --------------------------------------------------------------------------

def task_start_command(environ: Mapping[str, str]) -> list[str]:
    root = environ.get("SystemRoot") or ""
    exe = Path(root) / "System32" / "schtasks.exe"
    if not root or not exe.is_absolute() or not exe.is_file():
        raise dl.DirectLocalError("probe_task_starter_missing")
    return [str(exe), "/Run", "/TN", MAIN_TASK]


def start_main_task(environ: Mapping[str, str]) -> bool:
    """``schtasks /Run`` of the main task; its output is never echoed."""

    try:
        completed = subprocess.run(task_start_command(environ), capture_output=True, timeout=TASK_START_TIMEOUT_SECONDS, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# --------------------------------------------------------------------------
# One probe tick
# --------------------------------------------------------------------------

def run_probe(config: sync.SyncConfig, root: Path, *, environ: Mapping[str, str], transport: Any = None, clock: Callable[[], datetime] = dl.utc_now,
              starter: Callable[[], bool] | None = None, protector: Any = None, monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    import xb_shopify_poller as poller

    now = clock()
    status = load_status(root, now, config.probe_max_wakes_per_day)
    status["mode"] = "wake" if config.probe_wake_enabled else "observe"
    today = _today(status, now)
    today["checks"] += 1
    counts: dict[str, int] = {}
    try:
        conn = _open_read_only(root, config)
        try:
            since = _watermark(conn, "shopify_updated_at") or dl.ts(config.shopify_cutover_utc)
            transport = transport if transport is not None else direct.UrllibTransport()
            client = ProbeShopifyClient(
                create_enabled=config.shopify_create_enabled, shop_domain=config.shopify.shop_domain, api_version=config.shopify.api_version,
                token_provider=poller.token_provider(config.shopify, environ, transport, clock, root=Path(root), protector=protector), transport=transport,
                timeout=config.http_timeout_seconds, monotonic=monotonic, deadline=monotonic() + SHOPIFY_DEADLINE_SECONDS,
            )
            client.verify_session(store_gid=config.shopify.store_gid)
            sign_ups, complete = client.sign_ups(search_text(since), PROBE_MAX_PAGES)
            facts = ledger_facts(conn, config, [s.gid for s in sign_ups], now)
        finally:
            conn.close()
        counts = {"sign_ups_seen": len(sign_ups), "new_sign_ups": sum(1 for s in sign_ups if sync.REGISTRATION_TAG in s.tags and s.gid not in facts.known),
                  "known_sign_ups": sum(1 for s in sign_ups if s.gid in facts.known), "search_complete": int(complete)}
        decision, reason, marker = decide(now=now, config=config, facts=facts, sign_ups=sign_ups, status=status)
        if decision == "wake":
            if (starter or (lambda: start_main_task(environ)))():
                today["wakes"] += 1
                status["last_wake_at"] = dl.ts(now)
            else:
                decision, reason = "wake_failed", "task_start_failed"
        elif decision == "would_wake":
            today["would_wakes"] += 1
            status["last_would_wake_at"] = dl.ts(now)
        if marker is not None and decision in ("wake", "would_wake"):
            status["work_marker"] = marker
    except sqlite3.Error:
        # Only the ledger is SQLite in this try; digest gives the same refusal.
        _record(status, today, now, "refused", "sync_ledger_unreadable", counts)
        save_status(root, status)
        raise dl.DirectLocalError("sync_ledger_unreadable") from None
    except dl.DirectLocalError as exc:
        decision, reason = "refused", exc.code if dl.CODE_RE.fullmatch(exc.code or "") else "probe_failed"
        _record(status, today, now, decision, reason, counts)
        save_status(root, status)
        raise
    _record(status, today, now, decision, reason, counts)
    save_status(root, status)
    return {"schema": PROBE_SCHEMA, "status": "ok", "mode": status["mode"], "decision": decision, "reason": reason, "counts": counts,
            "wakes_today": today["wakes"], "would_wakes_today": today["would_wakes"], "probe_max_wakes_per_day": config.probe_max_wakes_per_day}


def _record(status: dict[str, Any], today: dict[str, Any], now: datetime, decision: str, reason: str, counts: Mapping[str, int]) -> None:
    status["last_check_at"] = dl.ts(now)
    status["last_decision"] = decision
    status["last_reason"] = reason
    status["last_counts"] = dict(counts)
    today["reasons"][reason] = today["reasons"].get(reason, 0) + 1


def probe_digest(root: Path, *, since: datetime) -> dict[str, Any] | None:
    """For ``digest``: the probe's days from ``since`` (Singapore dates) and
    its last check. None without a status file; never writes."""

    value, valid = read_status(root)
    if not valid:
        return {"status": "unreadable"}
    if value is None:
        return None
    first = sgt_day(since)
    return {
        "status": "ok", "mode": value["mode"], "last_check_at": value["last_check_at"], "last_decision": value["last_decision"],
        "last_reason": value["last_reason"], "last_wake_at": value["last_wake_at"],
        "days": {day: {k: entry[k] for k in ("checks", "wakes", "would_wakes")} for day, entry in sorted(value["days"].items()) if day >= first},
    }
