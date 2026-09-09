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
from .portal import (
    AUTHENTICATION_OUTCOMES,
    AUTHENTICATION_UNPROVED,
    LOGIN_DIAGNOSTIC_CLASSIFICATIONS,
    SUBMIT_NOT_DISPATCHED,
    PlaywrightPortal,
    unobserved_login_witnesses,
)
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

# DL-XB-141-RUNTIME-005-SOURCE-DURABILITY-A1: the bounded diagnostic operation.
# The launcher reaches it as the fixed `login-diagnostic` command; headed
# execution is an implicit, non-overridable property of that one operation and
# never a generic switch.
LOGIN_DIAGNOSTIC_COMMAND = "login-diagnostic"

# DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001. The active schema is v2: the
# document now carries the authentication verdict and the invariant
# never-tested navigation status alongside the existing bounded observations.
LOGIN_DIAGNOSTIC_SCHEMA = "energygrid.login_diagnostic.v2"

# The superseded identifier, kept so a document emitted by an earlier build
# stays readable as what it was. No build emits it: it is documentation and
# historical evidence only, never the active schema.
HISTORICAL_LOGIN_DIAGNOSTIC_SCHEMAS = ("energygrid.login_diagnostic.v1",)

DIAGNOSTIC_COMPLETE = "DIAGNOSTIC_COMPLETE"

# The diagnostic observes authentication and stops. It never tests business
# navigation, so this is an invariant of the document rather than a result.
NAVIGATION_NOT_TESTED = "NOT_TESTED"

# The bounded reference for a diagnostic that ran to its deadline without
# establishing an authorised classification. It is a fail-closed outcome, not a
# failure of a named step, so it carries its own reference rather than
# borrowing one that names a step that did not fail.
DIAGNOSTIC_UNCLASSIFIED_SUPPORT_REF = "EG_LOGIN_DIAGNOSTIC_UNCLASSIFIED"

# Every message the pre-auth login path can raise, mapped to a bounded ASCII
# reference. portal.py owns the wording, so a change there fails the reachability
# tests instead of silently degrading a known failure to generic. The one coarse
# submit message is retained below only for historical evidence.
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
    "login submit control did not appear": "EG_LOGIN_SUBMIT_NOT_APPEAR",
    "login submit control is missing or ambiguous": "EG_LOGIN_SUBMIT_AMBIGUOUS",
    "login submit control is hidden or disabled": "EG_LOGIN_SUBMIT_NOT_READY",
    "login submit control could not be resolved": "EG_LOGIN_SUBMIT_UNRESOLVED",
    "login submit dispatch outcome uncertain": "EG_LOGIN_SUBMIT_DISPATCH_UNCERTAIN",
    "login submission did not complete": "EG_LOGIN_SUBMIT_FAILED",
    "Billing Manager entry did not appear after login": "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED",
    "required login control is missing or ambiguous": "EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED",
    "runtime credentials are unavailable": "EG_LOGIN_CREDENTIALS_UNAVAILABLE",
    "portal rejected the login": "EG_LOGIN_PORTAL_REJECTED",
    "authenticated landing was not proven after login": "EG_LOGIN_AUTHENTICATION_UNPROVED",
    # Business navigation, not authentication. Billing Manager becoming
    # unusable after a proven authenticated landing is a navigation failure and
    # carries a navigation reference, so an operator can tell the two apart.
    "Billing Manager navigation control is not ready": "EG_NAV_BILLING_MANAGER_NOT_READY",
    "Billing Manager navigation dispatch outcome uncertain": "EG_NAV_BILLING_MANAGER_DISPATCH_UNCERTAIN",
    "EB Bill navigation control is not ready": "EG_NAV_EB_BILL_NOT_READY",
    "EB Bill navigation dispatch outcome uncertain": "EG_NAV_EB_BILL_DISPATCH_UNCERTAIN",
    "EB Bill results route was not proven": "EG_NAV_RESULTS_ROUTE_UNPROVED",
}

# The business-navigation half of the vocabulary, declared rather than inferred
# from a prefix. It is reachable only from `_open_verified_results()`, so the
# pre-auth login reachability contract is stated over the login half alone.
NAVIGATION_SUPPORT_REFS = frozenset(
    {
        "EG_NAV_BILLING_MANAGER_NOT_READY",
        "EG_NAV_BILLING_MANAGER_DISPATCH_UNCERTAIN",
        "EG_NAV_EB_BILL_NOT_READY",
        "EG_NAV_EB_BILL_DISPATCH_UNCERTAIN",
        "EG_NAV_RESULTS_ROUTE_UNPROVED",
    }
)

# Kept so evidence written by an earlier build stays readable, not because any
# step can still raise it: every operation that once shared this one coarse
# reference now reports its own. Retiring a reference means moving it here, so
# the reachability tests can require the live vocabulary to be fully reachable
# and a retired one to be unreachable.
RETIRED_SUPPORT_REFS = frozenset(
    {
        "EG_LOGIN_REQUIRED_CONTROL_UNRESOLVED",
        "EG_LOGIN_SUBMIT_FAILED",
        # DL-XB-141-AUTH-LANDING-NAV-SEPARATION-G2-001. Billing Manager is no
        # longer an authentication oracle, so no login step waits on it and
        # nothing can raise this again. The mapping stays so evidence written
        # by an earlier build still reads; a login that cannot prove the
        # authenticated landing now records
        # `EG_LOGIN_AUTHENTICATION_UNPROVED`, and a Billing Manager that never
        # becomes usable records a navigation reference instead.
        "EG_LOGIN_BILLING_MANAGER_WAIT_FAILED",
    }
)


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
    # Deliberately NOT built from the loop above. The diagnostic accepts
    # `--config` and nothing else: no `--headed`, no archive, state, temp or log
    # override, no timeout or attempt override, and no additional argument. Its
    # headed mode and its bounds are fixed properties of the operation.
    diagnostic = subparsers.add_parser(LOGIN_DIAGNOSTIC_COMMAND)
    diagnostic.add_argument("--config", required=True, type=Path)
    return parser


def login_diagnostic_document(
    *,
    status: str,
    classification: str | None,
    submit_dispatched: bool,
    submit_outcome: str,
    pre_submit: dict[str, Any],
    post_submit: dict[str, Any],
    authentication_outcome: str = AUTHENTICATION_UNPROVED,
    support_ref: str | None = None,
) -> dict[str, Any]:
    """Build the one closed, public-safe diagnostic document.

    Every value is a fixed identifier, a boolean, a count, or null. No exception
    text, URL, filesystem path, account identity, host identity, security
    identifier, credential, credential derivative, page text, or business datum
    can reach it, because none of them is ever a field.

    `authentication_outcome` is closed over the three authorised values, and an
    unauthorised one is reported as unproved rather than emitted, so the
    vocabulary stays closed even against a future portal value this document
    does not authorise. `navigation_status` is an invariant: the diagnostic
    never tests business navigation, so it can only ever be `NOT_TESTED`.
    """

    if authentication_outcome not in AUTHENTICATION_OUTCOMES:
        authentication_outcome = AUTHENTICATION_UNPROVED
    document: dict[str, Any] = {
        "schema": LOGIN_DIAGNOSTIC_SCHEMA,
        "status": status,
        "classification": classification,
        "authentication_outcome": authentication_outcome,
        "navigation_status": NAVIGATION_NOT_TESTED,
        "submit_dispatched": submit_dispatched,
        "submit_outcome": submit_outcome,
        "pre_submit": pre_submit,
        "post_submit": post_submit,
    }
    if support_ref is not None:
        document["support_ref"] = support_ref
    return document


def emit_login_diagnostic(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True))


def run_login_diagnostic(config_path: Path) -> int:
    """Run the bounded diagnostic and emit exactly one result document.

    The configuration is loaded and validated, and nothing else on the run path
    is touched: `config.preflight()` is not called, no `SafeLogger` is built, no
    stale-temp cleanup runs, no `StateStore` is opened, and inventory
    reconciliation is never reached. Nothing is created, modified, or deleted on
    disk.
    """

    unobserved_pre = unobserved_login_witnesses()
    unobserved_post = unobserved_login_witnesses(include_url=True)

    def action_required(support_ref: str) -> dict[str, Any]:
        return login_diagnostic_document(
            status=ACTION_REQUIRED,
            classification=None,
            submit_dispatched=False,
            submit_outcome=SUBMIT_NOT_DISPATCHED,
            pre_submit=unobserved_pre,
            post_submit=unobserved_post,
            support_ref=support_ref,
        )

    try:
        config = load_runtime_config(load_config_file(config_path))
    except (ConfigError, DependencyError) as exc:
        emit_login_diagnostic(action_required(support_ref_for(exc)))
        return 64

    try:
        # Headed unconditionally: it is what the operation is for, and there is
        # no argument by which a caller could ask for anything else.
        with PlaywrightPortal(config, headed=True) as portal:
            result = portal.login_diagnostic()
    except (ConfigError, DependencyError) as exc:
        emit_login_diagnostic(action_required(support_ref_for(exc)))
        return 64
    except AppError as exc:
        emit_login_diagnostic(action_required(support_ref_for(exc)))
        return 20
    except Exception:
        # The diagnostic's own fail-closed boundary for an unexpected ordinary
        # exception that escapes before a truthful submit-dispatched result
        # exists. The portal keeps its own post-submit envelope, so nothing that
        # has already dispatched Login can arrive here and be reported as never
        # dispatched. Exactly one closed document is still emitted, with the
        # unobserved witness shapes and a bounded reference; the exception
        # itself is discarded rather than described, so no traceback or
        # free-form text reaches stdout or stderr. `BaseException` is
        # deliberately excluded: `KeyboardInterrupt` and `SystemExit` are
        # process control, not a diagnostic outcome, and must propagate.
        emit_login_diagnostic(action_required(UNCLASSIFIED_SUPPORT_REF))
        return 20

    if result.failure is not None:
        emit_login_diagnostic(
            login_diagnostic_document(
                status=ACTION_REQUIRED,
                classification=None,
                submit_dispatched=result.submit_dispatched,
                submit_outcome=result.submit_outcome,
                pre_submit=result.pre_submit,
                post_submit=result.post_submit,
                authentication_outcome=result.authentication_outcome,
                support_ref=support_ref_for(result.failure),
            )
        )
        return 20

    # An unrecognised classification is treated as none at all, so the emitted
    # vocabulary stays closed even if the portal layer ever grows a new one
    # without this document being updated to authorise it.
    classification = result.classification
    if classification not in LOGIN_DIAGNOSTIC_CLASSIFICATIONS:
        classification = None
    if classification is None:
        emit_login_diagnostic(
            login_diagnostic_document(
                status=ACTION_REQUIRED,
                classification=None,
                submit_dispatched=result.submit_dispatched,
                submit_outcome=result.submit_outcome,
                pre_submit=result.pre_submit,
                post_submit=result.post_submit,
                authentication_outcome=result.authentication_outcome,
                support_ref=DIAGNOSTIC_UNCLASSIFIED_SUPPORT_REF,
            )
        )
        return 20

    emit_login_diagnostic(
        login_diagnostic_document(
            status=DIAGNOSTIC_COMPLETE,
            classification=classification,
            submit_dispatched=result.submit_dispatched,
            submit_outcome=result.submit_outcome,
            pre_submit=result.pre_submit,
            post_submit=result.post_submit,
            authentication_outcome=result.authentication_outcome,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    logger: SafeLogger | None = None
    try:
        args = parser.parse_args(argv)
        if args.command == LOGIN_DIAGNOSTIC_COMMAND:
            # The diagnostic owns its whole result contract, including its own
            # exit codes. It never reaches the run path below, so no state, log,
            # temp or archive artefact can be created on its behalf.
            return run_login_diagnostic(args.config)
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
