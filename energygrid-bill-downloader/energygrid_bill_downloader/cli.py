from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_config_file, load_runtime_config
from .errors import ACTION_REQUIRED, AppError, ConfigError, DependencyError, exit_code_for
from .publication import cleanup_stale_owned_temp
from .portal import PlaywrightPortal
from .reconcile import reconcile_inventory
from .state import StateStore


ALLOWED_LOG_FIELDS = {
    "inventory_count",
    "downloaded_count",
    "present_count",
    "failure_count",
    "attempt",
    "duration_ms",
    "support_ref",
}

RUN_FAILED_PHASE = "run_failed"
UNCLASSIFIED_SUPPORT_REF = "APP_ERROR_UNCLASSIFIED"

# Every message the pre-auth login path can currently raise, mapped to a bounded
# ASCII reference. portal.py owns the wording, so a change there fails the
# reachability tests instead of silently degrading a known failure to generic.
SUPPORT_REFS_BY_MESSAGE = {
    "Flutter semantics activation control did not appear": "EG_LOGIN_SEMANTICS_ACTIVATION_NOT_APPEAR",
    "Flutter semantics activation control could not be resolved": "EG_LOGIN_SEMANTICS_ACTIVATION_UNRESOLVED",
    "Flutter semantics activation control is missing or ambiguous": "EG_LOGIN_SEMANTICS_ACTIVATION_AMBIGUOUS",
    "Flutter semantics activation control is hidden or disabled": "EG_LOGIN_SEMANTICS_ACTIVATION_NOT_READY",
    "Flutter semantics placeholder remained after activation": "EG_LOGIN_SEMANTICS_PLACEHOLDER_REMAINS",
    "post-activation Login control did not appear": "EG_LOGIN_POST_ACTIVATION_NOT_APPEAR",
    "post-activation Login control could not be resolved": "EG_LOGIN_POST_ACTIVATION_UNRESOLVED",
    "post-activation Login control is missing or ambiguous": "EG_LOGIN_POST_ACTIVATION_AMBIGUOUS",
    "post-activation Login control is hidden or disabled": "EG_LOGIN_POST_ACTIVATION_NOT_READY",
    "portal navigation did not complete": "EG_LOGIN_NAVIGATION_FAILED",
    "Flutter semantics activation dispatch did not complete": "EG_LOGIN_SEMANTICS_ACTIVATION_DISPATCH_FAILED",
    "post-activation Login control click did not complete": "EG_LOGIN_ENTRY_CLICK_FAILED",
    "login username entry did not complete": "EG_LOGIN_USERNAME_FILL_FAILED",
    "login password entry did not complete": "EG_LOGIN_PASSWORD_FILL_FAILED",
    "login submission did not complete": "EG_LOGIN_SUBMIT_FAILED",
    "Billing Manager entry did not appear after login": "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED",
    "required login control is missing or ambiguous": "EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED",
    "runtime credentials are unavailable": "EG_LOGIN_CREDENTIALS_UNAVAILABLE",
    "portal rejected the login": "EG_LOGIN_PORTAL_REJECTED",
}

# Kept so evidence written by an earlier build stays readable, not because any
# step can still raise it: every operation that once shared this one coarse
# reference now reports its own. Retiring a reference means moving it here, so
# the reachability tests can require the live vocabulary to be fully reachable
# and a retired one to be unreachable.
RETIRED_SUPPORT_REFS = frozenset({"EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED"})


def support_ref_for(error: AppError) -> str:
    """Return the stable public-safe reference for a failure.

    An unrecognised message - a future portal contract, or any text this build
    does not know - yields the generic reference. The message is only ever a
    lookup key, so nothing it carries can reach an output surface.
    """

    return SUPPORT_REFS_BY_MESSAGE.get(error.message, UNCLASSIFIED_SUPPORT_REF)


def redact_sensitive(message: str) -> str:
    result = str(message)
    for name in ("ENERGYGRID_USERNAME", "ENERGYGRID_PASSWORD"):
        value = os.environ.get(name)
        if value:
            result = result.replace(value, "<redacted>")
    result = re.sub(
        r"(?i)(password|passwd|token|authorization|cookie|set-cookie)\s*[:=]\s*[^,;\s]+",
        r"\1=<redacted>",
        result,
    )
    return result


class ContractArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ConfigError("invalid command-line arguments")


class SafeLogger:
    def __init__(self, log_root: Path, run_id: str) -> None:
        self.run_id = run_id
        self.log_path = log_root / f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, phase: str, status: str | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "phase": phase,
        }
        if status is not None:
            payload["status"] = status
        for key in ALLOWED_LOG_FIELDS:
            if key in fields:
                payload[key] = fields[key]
        payload = {key: redact_sensitive(str(value)) if isinstance(value, str) else value for key, value in payload.items()}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n")


def log_terminal_failure(logger: SafeLogger | None, error: AppError) -> None:
    """Append the one terminal failure event, when a logger already exists.

    This is evidence, not a result. A failure raised before the logger was built
    has no established log root to write to, and a write that fails must not
    change what the caller returns, so both cases leave the canonical status and
    exit code untouched.
    """

    if logger is None:
        return
    try:
        logger.event(
            RUN_FAILED_PHASE,
            status=error.status,
            support_ref=support_ref_for(error),
        )
    except Exception:
        # Losing the evidence line is strictly less harmful than converting a
        # known failure into a different result or surfacing raw exception text.
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = ContractArgumentParser(description="Reconcile synthetic or approved Energy@Grid bill inventories.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "list"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", required=True, type=Path)
        subparser.add_argument("--archive-root", type=Path)
        subparser.add_argument("--state-path", type=Path)
        subparser.add_argument("--temp-root", type=Path)
        subparser.add_argument("--log-root", type=Path)
        subparser.add_argument("--timeout-seconds", type=int)
        subparser.add_argument("--max-attempts", type=int)
        subparser.add_argument("--headed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    logger: SafeLogger | None = None
    try:
        args = parser.parse_args(argv)
        raw = load_config_file(args.config)
        config = load_runtime_config(raw)
        config = config.with_overrides(
            {
                "archive_root": args.archive_root,
                "state_path": args.state_path,
                "temp_root": args.temp_root,
                "log_root": args.log_root,
                "timeout_seconds": args.timeout_seconds,
                "max_attempts": args.max_attempts,
            }
        )
        config.preflight(require_archive=True)
        run_id = str(uuid.uuid4())
        logger = SafeLogger(config.log_root, run_id)
        cleanup_stale_owned_temp(config.temp_root)
        with StateStore(config.state_path) as state:
            with PlaywrightPortal(config, headed=args.headed) as portal:
                logger.event("login_start")
                portal.login()
                logger.event("login_complete")
                summary = reconcile_inventory(
                    config=config,
                    portal=portal,
                    state=state,
                    logger=logger,
                    run_id=run_id,
                    list_only=args.command == "list",
                )
        logger.event(
            "run_complete",
            status=summary.status,
            inventory_count=summary.inventory_count,
            downloaded_count=summary.downloaded_count,
            present_count=summary.present_count,
            failure_count=summary.failure_count,
        )
        print(json.dumps(summary.as_dict(), sort_keys=True))
        return summary.exit_code
    except argparse.ArgumentError:
        return 64
    except (ConfigError, DependencyError) as exc:
        log_terminal_failure(logger, exc)
        print(json.dumps({"status": ACTION_REQUIRED, "error_class": "CONFIG_OR_DEPENDENCY"}, sort_keys=True))
        return 64
    except AppError as exc:
        log_terminal_failure(logger, exc)
        print(json.dumps({"status": exc.status, "error_class": exc.status}, sort_keys=True))
        return exc.exit_code or exit_code_for(exc.status)
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": ACTION_REQUIRED, "error_class": "RUNTIME_FAILURE"}, sort_keys=True))
        return 20


if __name__ == "__main__":
    sys.exit(main())
