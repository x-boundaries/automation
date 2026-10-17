"""#155 E8: Claude-free Shopify registration poller for the DirectLocal Core.

One short run per invocation (Windows Task Scheduler, every 2 minutes; no
daemon, no listener):

    Shopify Admin GraphQL (outbound HTTPS, read-only, pinned Core queries)
      -> direct-HTTP captures in <state_root>/inbox (own provenance)
      -> unchanged Core: discovery run sealed, registration event, admission
      -> AutoCount: NOT reachable from this module (no-write mode only)

Per run, holding the Core's OS ledger lock for the whole run (a second
invocation skips):

1. Read-only ledger recovery; refuse a HOLD, an unsealed baseline, a
   different store, or an attended hook capture in the inbox.
2. Abandon only a run this poller started that a crash left open (its
   watermark never advanced); refuse any other open run.
3. Session check: store identity and exactly ``read_customers``.
4. Discovery: the Core's ``recent`` run (watermark = last sealed discovery
   start minus 15 minutes, an index-lag overlap, not a wait), or an
   exhaustive run when the configured full-scan interval (default 24 h) has
   passed. A recent run that exceeds its page bound escalates to a full scan.
   The watermark advances only when the Core seals the run after every page
   was read, validated and durably written; any failure abandons the run.
5. Optional profile reads (``profile_reads_enabled``, default off) of
   DISCOVERED/WAITING customers through the Core's ``process`` without a
   bridge. WAITING customers are re-read on a later run (Forms writes the
   ``membership`` metafields a few seconds after the tag), within the Core's
   bounded wait budget.

Output: one JSON line (status, codes, counts, seconds) on stdout and in
``<state_root>/shopify-poller-runs.jsonl``; never a GID, cursor, name,
phone, email, token or provider message. Consecutive failures back off
(bounded, persisted in ``<state_root>/shopify-poller-state.json``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

_HERE = Path(__file__).resolve().parent
# Appended, never prepended (same rule as the Core's gateway path): nothing
# on this path may shadow a standard-library module.
if str(_HERE) not in sys.path:
    sys.path.append(str(_HERE))

import xb_directlocal as dl  # noqa: E402
import xb_shopify_direct as direct  # noqa: E402

CONFIG_SCHEMA = "xb.directlocal.shopify_poller.config.v1"
REPORT_SCHEMA = "xb.directlocal.shopify_poller.run.v1"
STATE_SCHEMA = "xb.directlocal.shopify_poller.state.v1"
STATE_FILE = "shopify-poller-state.json"
LOG_FILE = "shopify-poller-runs.jsonl"
LOG_MAX_BYTES = 1_048_576
AUTOCOUNT_WRITE_MODES = frozenset({"none"})  # the only mode this source has
AUTH_METHODS = frozenset({"client_credentials", "admin_api_access_token"})
ENV_NAME_RE = re.compile(r"^XB_SHOPIFY_[A-Z0-9_]{1,48}$")
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 1800
BACKOFF_OPERATOR_SECONDS = 3600
EXIT_OK, EXIT_FAILED = 0, 2

_BOUNDS = {
    "max_profile_reads_per_run": (0, 50, 10),
    "profile_retry_seconds": (60, 3600, 100),
    "full_scan_interval_hours": (1, 168, 24),
    "recent_max_pages": (1, 100, 40),
    "full_scan_max_pages": (1, 400, 200),
    "http_timeout_seconds": (5, 60, 30),
    "max_run_seconds": (60, 3000, 480),
}


class NoWriteProcessor(dl.Processor):
    """The Core processor with every AutoCount route removed. Admission still
    runs; no bridge can be bound, so no AutoCount process (login, number
    preview, probe or SaveMember) can be started from the poller."""

    def _unreachable(self, *args: Any, **kwargs: Any) -> Any:
        raise dl.DirectLocalError("autocount_writer_unreachable_no_write_mode")

    _require_bridge = _write = _allocate = book_check = pos_sync_status = reconcile = approve_write = activate_policy = _unreachable

    def process(self, capture_path: Path, *, bridge: Any = None, enable_write: bool = False) -> dict[str, Any]:
        if bridge is not None or enable_write:
            raise dl.DirectLocalError("autocount_writer_unreachable_no_write_mode")
        return super().process(capture_path, bridge=None, enable_write=False)


# --------------------------------------------------------------------------
# Configuration (names of environment variables only; never a secret value)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PollerConfig:
    state_root: str
    store_gid: str
    shop_domain: str
    api_version: str
    auth_method: str
    auth_env: Mapping[str, str]
    autocount_write_mode: str
    profile_reads_enabled: bool
    profile_discovered_since: datetime | None
    max_profile_reads_per_run: int
    profile_retry_seconds: int
    full_scan_interval_hours: int
    recent_max_pages: int
    full_scan_max_pages: int
    http_timeout_seconds: int
    max_run_seconds: int


def parse_config(value: Any) -> PollerConfig:
    if not isinstance(value, dict):
        raise dl.DirectLocalError("poller_config_invalid")
    required = {"schema", "state_root", "store_gid", "shop_domain", "api_version", "auth", "autocount_write_mode", "profile_reads_enabled"}
    optional = {"profile_discovered_since", *_BOUNDS}
    if not required <= set(value) or not set(value) <= required | optional:
        raise dl.DirectLocalError("poller_config_keys_invalid")
    if value["schema"] != CONFIG_SCHEMA:
        raise dl.DirectLocalError("poller_config_schema_invalid")
    if value["autocount_write_mode"] not in AUTOCOUNT_WRITE_MODES:
        raise dl.DirectLocalError("autocount_write_mode_not_available")
    if not isinstance(value["state_root"], str) or not value["state_root"]:
        raise dl.DirectLocalError("poller_config_state_root_invalid")
    if not isinstance(value["store_gid"], str) or not dl.SHOP_GID_RE.fullmatch(value["store_gid"]):
        raise dl.DirectLocalError("poller_config_store_invalid")
    if not isinstance(value["shop_domain"], str) or not (dl.SHOP_DOMAIN_RE.fullmatch(value["shop_domain"]) or dl.SYNTHETIC_DOMAIN_RE.fullmatch(value["shop_domain"])):
        raise dl.DirectLocalError("poller_config_store_invalid")
    if not isinstance(value["api_version"], str) or not dl.API_VERSION_RE.fullmatch(value["api_version"]):
        raise dl.DirectLocalError("shopify_api_version_invalid")
    if not isinstance(value["profile_reads_enabled"], bool):
        raise dl.DirectLocalError("poller_config_profile_reads_invalid")
    auth = value["auth"]
    method = auth.get("method") if isinstance(auth, dict) else None
    names = {"client_credentials": {"client_id_env", "client_secret_env"}, "admin_api_access_token": {"token_env"}}.get(method)
    if names is None or set(auth) != names | {"method"}:
        raise dl.DirectLocalError("poller_config_auth_invalid")
    env = {key: auth[key] for key in names}
    if any(not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name) for name in env.values()) or len(set(env.values())) != len(env):
        raise dl.DirectLocalError("poller_config_auth_invalid")
    since = None
    if value.get("profile_discovered_since") is not None:
        since = dl.parse_created_at(value["profile_discovered_since"])
        if since is None:
            raise dl.DirectLocalError("poller_config_profile_scope_invalid")
    numbers = {}
    for key, (low, high, default) in _BOUNDS.items():
        number = value.get(key, default)
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise dl.DirectLocalError(f"poller_config_{key}_invalid")
        numbers[key] = number
    return PollerConfig(
        state_root=value["state_root"], store_gid=value["store_gid"], shop_domain=value["shop_domain"], api_version=value["api_version"],
        auth_method=method, auth_env=env, autocount_write_mode=value["autocount_write_mode"],
        profile_reads_enabled=value["profile_reads_enabled"], profile_discovered_since=since, **numbers,
    )


def load_config(path: str | Path) -> PollerConfig:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise dl.DirectLocalError("poller_config_unreadable") from None
    if len(raw) > 65_536:
        raise dl.DirectLocalError("poller_config_invalid")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise dl.DirectLocalError("poller_config_invalid") from None
    return parse_config(value)


def token_provider(config: PollerConfig, environ: Mapping[str, str], transport: direct.Transport, clock: Callable[[], datetime]) -> Any:
    if config.auth_method == "admin_api_access_token":
        return direct.StaticTokenProvider(environ.get(config.auth_env["token_env"]))
    return direct.ClientCredentialsTokenProvider(
        config.shop_domain, environ.get(config.auth_env["client_id_env"]), environ.get(config.auth_env["client_secret_env"]),
        transport=transport, clock=clock, timeout=config.http_timeout_seconds,
    )


# --------------------------------------------------------------------------
# Backoff state and run log (PII-free; outside the hash-chained ledger)
# --------------------------------------------------------------------------

def _default_state() -> dict[str, Any]:
    return {"schema": STATE_SCHEMA, "consecutive_failures": 0, "last_code": None, "not_before": None, "last_success_at": None}


def load_state(root: Path) -> tuple[dict[str, Any], str | None]:
    """Backoff state, or the default plus ``poller_state_reset`` when the
    file is unreadable (pacing only; never a correctness input)."""

    path = root / STATE_FILE
    if not path.exists():
        return _default_state(), None
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
        valid = (
            isinstance(value, dict) and set(value) == set(_default_state()) and value["schema"] == STATE_SCHEMA
            and isinstance(value["consecutive_failures"], int) and not isinstance(value["consecutive_failures"], bool) and 0 <= value["consecutive_failures"] <= 1_000_000
            and (value["last_code"] is None or (isinstance(value["last_code"], str) and dl.CODE_RE.fullmatch(value["last_code"])))
            and all(value[key] is None or dl.parse_created_at(value[key]) is not None for key in ("not_before", "last_success_at"))
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        valid = False
    return (value, None) if valid else (_default_state(), "poller_state_reset")


def save_state(root: Path, state: Mapping[str, Any]) -> None:
    path = root / STATE_FILE
    temporary = root / f".{STATE_FILE}.{os.getpid()}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(json.dumps(dict(state), sort_keys=True).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def backoff_seconds(failures: int, code: str | None) -> int:
    if code in direct.OPERATOR_ACTION_CODES:
        return BACKOFF_OPERATOR_SECONDS
    return min(BACKOFF_BASE_SECONDS * 2 ** max(failures - 1, 0), BACKOFF_MAX_SECONDS)


def append_log(root: Path, report: Mapping[str, Any]) -> None:
    path = root / LOG_FILE
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, root / (LOG_FILE + ".1"))
        with open(path, "ab") as handle:
            handle.write((json.dumps(dict(report), sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        pass  # stdout still carries the report; the ledger is unaffected


# --------------------------------------------------------------------------
# Run-once
# --------------------------------------------------------------------------

class _PageLimitExceeded(dl.DirectLocalError):
    pass


class Poller:
    def __init__(self, config: PollerConfig, processor: NoWriteProcessor, client: direct.ShopifyDirectClient, *, clock: Callable[[], datetime]):
        self.config = config
        self.processor = processor
        self.ledger = processor.ledger
        self.client = client
        self.clock = clock
        self.report: dict[str, Any] = {"pages": 0, "discovered": 0, "profiles_read": 0, "decisions": {}, "profiles_out_of_scope": 0}

    # -- preconditions ------------------------------------------------------
    def _refuse_attended_inbox(self) -> None:
        """An unconsumed attended hook capture means an attended session is
        using this state root; the poller never mixes with it."""

        conn = self.ledger.conn
        for path in sorted(self.processor.inbox.glob("*.json")):
            try:
                raw = path.read_bytes()
                record = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                continue  # rejected either way; purge-inbox removes it
            if not isinstance(record, dict) or record.get("provenance") != dl.CAPTURE_PROVENANCE:
                continue
            if not conn.execute("SELECT 1 FROM captures WHERE capture_sha256 = ?", (dl._sha256(raw),)).fetchone():
                raise dl.DirectLocalError("attended_capture_in_inbox")

    def _retire_stale_run(self) -> None:
        conn = self.ledger.conn
        run = conn.execute("SELECT run_id, kind FROM runs WHERE state = 'CAPTURING'").fetchone()
        if run is None:
            return
        if run["kind"] != "DISCOVERY" or self.processor.run_origin(conn, run["run_id"]) != dl.DIRECT_RUN_ORIGIN:
            raise dl.DirectLocalError("open_run_not_poller_owned")
        self.processor.abandon_run(run["run_id"], confirm_suffix=run["run_id"][-8:])
        self.report["stale_run_abandoned"] = True

    def _full_scan_due(self) -> bool:
        row = self.ledger.conn.execute("SELECT MAX(started_at) AS s FROM runs WHERE state = 'SEALED' AND search IS NULL").fetchone()
        if row["s"] is None:
            return True
        return self.ledger.now() - dl.parse_ts(row["s"]) >= timedelta(hours=self.config.full_scan_interval_hours)

    # -- discovery ------------------------------------------------------------
    def _scan(self, *, recent: bool) -> dict[str, Any]:
        begun = self.processor.begin_run("DISCOVERY", recent=recent, origin=dl.DIRECT_RUN_ORIGIN)
        run_id = begun["run_id"]
        try:
            pages = self._read_pages(begun["search"], self.config.recent_max_pages if recent else self.config.full_scan_max_pages)
            sealed = self.processor.ingest_run()
        except Exception:
            # Nothing of this run counts; the watermark stays where it was.
            self.processor.abandon_run(run_id, confirm_suffix=run_id[-8:])
            raise
        self.report["pages"] += pages
        self.report["discovered"] += sealed["discovered"]
        return sealed

    def _read_pages(self, search: str | None, limit: int) -> int:
        query = dl.RECENT_QUERY if search is not None else dl.BASELINE_QUERY
        after: str | None = None
        seen: set[str] = set()
        pages = 0
        while True:
            if pages >= limit:
                raise _PageLimitExceeded("page_limit_exceeded")
            variables: dict[str, Any] = {"first": dl.PAGE_SIZE, "after": after}
            if search is not None:
                variables["search"] = search
            data = self.client.execute(query, variables)
            page = data.get("customers")
            info = page.get("pageInfo") if isinstance(page, dict) else None
            if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
                raise direct.ShopifyDirectError("shopify_response_shape_invalid")
            direct.write_capture(self.processor.inbox, query=query, variables=variables, data=data, shop_domain=self.client.shop_domain, api_version=self.client.api_version, captured_at=self.ledger.now())
            pages += 1
            if not info["hasNextPage"]:
                return pages
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor or len(cursor) > 1024 or cursor in seen:
                raise direct.ShopifyDirectError("shopify_cursor_invalid")
            seen.add(cursor)
            after = cursor

    def discover(self) -> None:
        if self._full_scan_due():
            self.report["scan"] = "full"
            self._scan(recent=False)
            return
        self.report["scan"] = "recent"
        try:
            self._scan(recent=True)
        except _PageLimitExceeded:
            # More updates than the bound since the watermark: the exhaustive
            # scan (sortKey ID, no search index) is the bounded backstop.
            self.report["scan"] = "recent_escalated_full"
            try:
                self._scan(recent=False)
            except _PageLimitExceeded:
                raise dl.DirectLocalError("full_scan_page_limit_exceeded") from None

    # -- profiles ---------------------------------------------------------------
    def profiles(self) -> None:
        if not self.config.profile_reads_enabled or self.config.max_profile_reads_per_run == 0:
            return
        now = self.ledger.now()
        rows = self.ledger.conn.execute(
            "SELECT customer_gid, state, updated_at, discovered_at FROM customers WHERE state IN ('DISCOVERED', 'WAITING') ORDER BY updated_at, customer_gid"
        ).fetchall()
        for row in rows:
            if self.report["profiles_read"] >= self.config.max_profile_reads_per_run:
                return
            since = self.config.profile_discovered_since
            if since is not None and (row["discovered_at"] is None or dl.parse_ts(row["discovered_at"]) < since):
                self.report["profiles_out_of_scope"] += 1
                continue
            if row["state"] == "WAITING" and now - dl.parse_ts(row["updated_at"]) < timedelta(seconds=self.config.profile_retry_seconds):
                continue  # Forms is still writing; read again on a later run
            variables = {"id": row["customer_gid"]}
            data = self.client.execute(dl.PROFILE_QUERY, variables)
            path = direct.write_capture(self.processor.inbox, query=dl.PROFILE_QUERY, variables=variables, data=data, shop_domain=self.client.shop_domain, api_version=self.client.api_version, captured_at=self.ledger.now())
            self.report["profiles_read"] += 1
            state = self.processor.process(path)["customer_state"]
            self.report["decisions"][state] = self.report["decisions"].get(state, 0) + 1


def _customer_counts(ledger: dl.Ledger) -> dict[str, int]:
    return {row[0]: row[1] for row in ledger.conn.execute("SELECT state, COUNT(*) FROM customers GROUP BY state ORDER BY state")}


def run_once(config: PollerConfig, *, environ: Mapping[str, str] | None = None, transport: direct.Transport | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] = dl.utc_now, monotonic: Callable[[], float] = time.monotonic, baseline: bool = False) -> tuple[int, dict[str, Any]]:
    """One bounded poller run. ``baseline`` (attended only) instead seals the
    ledger's first, exhaustive baseline, which fixes the cutover C."""

    environ = os.environ if environ is None else environ
    started = monotonic()
    report: dict[str, Any] = {"schema": REPORT_SCHEMA, "autocount_write_mode": config.autocount_write_mode, "status": None, "code": None}

    def done(code_exit: int, status: str, code: str | None = None) -> tuple[int, dict[str, Any]]:
        report.update(status=status, code=code, elapsed_seconds=int(monotonic() - started))
        return code_exit, report

    if config.autocount_write_mode not in AUTOCOUNT_WRITE_MODES:
        return done(EXIT_FAILED, "refused", "autocount_write_mode_not_available")
    try:
        root = dl.validate_state_root(config.state_root, environ=environ)
        ledger = dl.Ledger(root, clock=clock)
    except dl.DirectLocalError as exc:
        if exc.code == "ledger_locked_by_another_owner":
            return done(EXIT_OK, "skipped_instance_running", exc.code)
        return done(EXIT_FAILED, "refused", exc.code)
    try:
        processor = NoWriteProcessor(ledger)
        state, state_note = load_state(root)
        if state_note:
            report["state_note"] = state_note
        try:
            meta = ledger.meta()
            processor.check_custody()
            if (meta["store_gid"], meta["shop_domain"]) != (config.store_gid, config.shop_domain):
                raise dl.DirectLocalError("poller_store_binding_mismatch")
        except dl.DirectLocalError as exc:
            result = done(EXIT_FAILED, "refused", exc.code)
            append_log(root, report)
            return result
        not_before = dl.parse_created_at(state["not_before"]) if state["not_before"] else None
        if not baseline and not_before is not None and ledger.now() < not_before:
            report["consecutive_failures"] = state["consecutive_failures"]
            result = done(EXIT_OK, "skipped_backoff", state["last_code"])
            append_log(root, report)
            return result
        poller: Poller | None = None
        try:
            processor.recover()
            has_baseline = ledger.conn.execute("SELECT 1 FROM runs WHERE kind = 'BASELINE' AND state = 'SEALED'").fetchone() is not None
            if baseline == has_baseline:
                raise dl.DirectLocalError("baseline_already_exists" if baseline else "baseline_not_sealed")
            poller = Poller(config, processor, None, clock=clock)  # type: ignore[arg-type]
            poller._refuse_attended_inbox()
            poller._retire_stale_run()
            if any(processor.inbox.iterdir()):
                processor.purge_inbox()
            live = transport if transport is not None else direct.UrllibTransport()
            poller.client = direct.ShopifyDirectClient(
                shop_domain=config.shop_domain, api_version=config.api_version, token_provider=token_provider(config, environ, live, ledger.now),
                transport=live, sleep=sleep, monotonic=monotonic, deadline=started + config.max_run_seconds, timeout=config.http_timeout_seconds,
            )
            poller.client.verify_session(store_gid=meta["store_gid"])
            if baseline:
                report["scan"] = "baseline"
                begun = processor.begin_run("BASELINE", origin=dl.DIRECT_RUN_ORIGIN)
                try:
                    poller.report["pages"] = poller._read_pages(None, config.full_scan_max_pages)
                    poller.report["discovered"] = processor.ingest_run()["discovered"]
                except Exception:
                    processor.abandon_run(begun["run_id"], confirm_suffix=begun["run_id"][-8:])
                    raise
            else:
                poller.discover()
                poller.profiles()
            if any(processor.inbox.iterdir()):
                processor.purge_inbox()  # consumed pages and profiles leave the inbox now
            exit_code, status, code = EXIT_OK, "ok", None
        except dl.DirectLocalHold as exc:
            exit_code, status, code = dl.HOLD_EXIT_CODE, "hold", exc.code
        except dl.DirectLocalError as exc:
            exit_code, status, code = EXIT_FAILED, "failed", exc.code
        except sqlite3.IntegrityError:
            exit_code, status, code = EXIT_FAILED, "failed", "ledger_constraint_refused"
        except OSError:
            exit_code, status, code = EXIT_FAILED, "failed", "poller_local_io_failed"
        if poller is not None:
            report.update({key: value for key, value in poller.report.items()})
            if poller.client is not None:
                stats = poller.client.stats
                report["shopify"] = {"requests": stats.requests, "retries": stats.retries, "wait_seconds": stats.wait_seconds, "auth_refreshes": stats.auth_refreshes, "min_available": stats.min_available}
        report["customers"] = _customer_counts(ledger)
        now = ledger.now()
        if status == "ok":
            state = {**_default_state(), "last_success_at": dl.ts(now)}
        elif status == "failed":
            failures = state["consecutive_failures"] + 1
            state = {**state, "consecutive_failures": failures, "last_code": code, "not_before": dl.ts(now + timedelta(seconds=backoff_seconds(failures, code)))}
            report["consecutive_failures"] = failures
        try:
            save_state(root, state)
        except OSError:
            report["state_note"] = "poller_state_unsaved"
        result = done(exit_code, status, code)
        append_log(root, report)
        return result
    finally:
        ledger.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _emit(report: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(report), sort_keys=True) + "\n")


def check_config(config: PollerConfig, environ: Mapping[str, str]) -> dict[str, Any]:
    """Offline: config shape, state-root location and whether each named
    variable is set (values are never read out). No network, no ledger."""

    dl.validate_state_root(config.state_root, environ=environ)
    present = {name: bool(environ.get(name)) for name in config.auth_env.values()}
    return {"status": "config_valid", "auth_method": config.auth_method, "env_present": present, "autocount_write_mode": config.autocount_write_mode, "profile_reads_enabled": config.profile_reads_enabled}


def main(argv: list[str] | None = None) -> int:
    if Path(dl.__file__).resolve().parent != _HERE or Path(direct.__file__).resolve().parent != _HERE:
        _emit({"status": "refused", "code": "core_module_path_unexpected"})
        return EXIT_FAILED
    parser = argparse.ArgumentParser(prog="python directlocal/xb_shopify_poller.py", description="DirectLocal #155 Claude-free Shopify poller (no AutoCount writes)")
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config")
    for name in ("run-once", "baseline"):
        sub = commands.add_parser(name)
        sub.add_argument("--no-autocount-write", action="store_true", help="required: this source has no AutoCount write mode")
        if name == "baseline":
            sub.add_argument("--confirm-new-cutover", action="store_true", help="required: fixes the cutover C of a new ledger to now")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            _emit(check_config(config, os.environ))
            return EXIT_OK
    except dl.DirectLocalError as exc:
        _emit({"status": "refused", "code": exc.code})
        return EXIT_FAILED
    if not args.no_autocount_write:
        _emit({"status": "refused", "code": "autocount_write_mode_must_be_explicit"})
        return EXIT_FAILED
    if args.command == "baseline" and not args.confirm_new_cutover:
        _emit({"status": "refused", "code": "baseline_confirmation_required"})
        return EXIT_FAILED
    code, report = run_once(config, baseline=args.command == "baseline")
    _emit(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
