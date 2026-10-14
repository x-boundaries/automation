"""Command line for the native Claude connector bridge (#226 V3 G1).

    python -m energygrid_bill_downloader.native_cli <command> --config <private native config> [options]

Every command prints exactly one line of JSON and exits 0 (done or nothing to do),
10 (lease held, retry later), 20 (HOLD or uncertainty recorded) or 64 (refused, no
state change). The config, state, archive and outbox live outside the Git checkout.
See docs/native_connector_bridge.md for the host protocol.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import native_bridge as bridge
from .errors import AppError, ConfigError

CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


def checkout_roots(root: Path = CHECKOUT_ROOT) -> tuple[Path, ...]:
    """This checkout and, for a linked worktree, the main checkout that owns it.

    A worktree's `.git` is a file `gitdir: <main>/.git/worktrees/<name>`. Private state must be
    outside both, because either may be committed and pushed.
    """
    roots = [root]
    marker = root / ".git"
    try:
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text[len("gitdir:"):].strip())
                if not gitdir.is_absolute():
                    gitdir = (root / gitdir)
                if gitdir.parent.name == "worktrees":
                    roots.append(gitdir.parent.parent.parent)
    except OSError:
        pass
    return tuple(roots)
DEFAULT_SYNTHETIC_LISTING = {
    # Two invoices per stream: only the latest is admitted, the older one is never backfilled.
    "EB_BILL": [("UAT-EB-0001 Synthetic.pdf", "2026-08-01"), ("UAT-EB-0002 Synthetic.pdf", "2026-09-01")],
    "TENANT_BILL": [("UAT-TN-0001 Synthetic.pdf", "2026-08-03"), ("UAT-TN-0002 Synthetic.pdf", "2026-09-03")],
}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _load_config(path: str) -> bridge.NativeConfig:
    from .config import load_config_file

    return bridge.load_native_config(load_config_file(Path(path)), checkout_root=checkout_roots())


def _portal_dual_config(cfg: bridge.NativeConfig):
    from .config import load_config_file, load_dual_stream_config

    assert cfg.portal_config_path is not None
    return load_dual_stream_config(load_config_file(cfg.portal_config_path), checkout_root=CHECKOUT_ROOT)


def _adapters(cfg: bridge.NativeConfig) -> dict[str, Any]:
    if cfg.mode == bridge.MODE_UAT_SYNTHETIC:
        assert cfg.synthetic_root is not None
        return {stream: bridge.SyntheticPortalAdapter(cfg.synthetic_root, stream) for stream in bridge.STREAM_LABELS}
    # Real EnergyGrid portal: the reviewed direct-HTTP adapters, built exactly as the v3 core builds them.
    from .cli import build_dual_adapters

    return build_dual_adapters(_portal_dual_config(cfg))


def _stream_entries(cfg: bridge.NativeConfig) -> dict:
    if cfg.mode == bridge.MODE_UAT_PORTAL:
        return _portal_dual_config(cfg).streams
    from .config import BOUND_ADMISSION, DualStreamEntry
    from .invoice import DATE_PROFILE_ISO_V1

    return {
        stream: DualStreamEntry(
            admission=BOUND_ADMISSION, source_namespace=bridge.SYNTHETIC_NAMESPACE, adapter_id="DIRECT_HTTP_V1",
            date_profile=DATE_PROFILE_ISO_V1, evidence_ref=bridge.SYNTHETIC_EVIDENCE,
        )
        for stream in bridge.STREAM_LABELS
    }


def _init_uat(cfg: bridge.NativeConfig, args) -> dict:
    """Create a fresh, isolated v3 -> v4 database for UAT. Refuses an existing state file."""
    from .state import migrate_state_database_v3, migrate_state_database_v4

    if cfg.state_path.exists():
        raise bridge.NativeRefusal("EG_NATIVE_UAT_STATE_EXISTS")
    # Outbox evidence and archive files are keyed by deterministic identities, so a fresh database
    # must never reuse an earlier environment's files.
    for directory in (cfg.outbox_root, cfg.archive_root, cfg.named_archive_root):
        if directory.exists() and any(directory.iterdir()):
            raise bridge.NativeRefusal("EG_NATIVE_UAT_DIRECTORY_NOT_EMPTY")
    for directory in (cfg.state_path.parent, cfg.archive_root, cfg.named_archive_root, cfg.temp_root,
                      cfg.outbox_root):
        directory.mkdir(parents=True, exist_ok=True)
    migrate_state_database_v3(cfg.state_path, apply=True, streams=_stream_entries(cfg))
    result = migrate_state_database_v4(cfg.state_path, apply=True)
    return bridge._result("init-uat", "INITIALISED", bridge.EXIT_OK, migration=result.get("status"))


def _make_fixture(cfg: bridge.NativeConfig, args) -> dict:
    if cfg.mode != bridge.MODE_UAT_SYNTHETIC or cfg.synthetic_root is None:
        raise bridge.NativeRefusal("EG_NATIVE_FIXTURE_NEEDS_SYNTHETIC_MODE")
    listing = DEFAULT_SYNTHETIC_LISTING
    if args.listing:
        listing = {key: [tuple(item) for item in value]
                   for key, value in json.loads(Path(args.listing).read_text(encoding="utf-8")).items()}
    bridge.write_synthetic_fixture(cfg.synthetic_root, listing)
    return bridge._result("make-synthetic-fixture", "WRITTEN", bridge.EXIT_OK,
                          invoices={key: len(value) for key, value in listing.items()})


def _with_state(cfg: bridge.NativeConfig, action: Callable[[Any], dict]) -> dict:
    from .state import StateV4Store

    with StateV4Store(cfg.state_path) as state:
        return action(state)


def _dispatch(args) -> dict:
    cfg = _load_config(args.config)
    command = args.command
    if command == "init-uat":
        return _init_uat(cfg, args)
    if command == "make-synthetic-fixture":
        return _make_fixture(cfg, args)
    run_id = bridge.new_run_id()
    now = _now_ms()
    if command == "acquire":
        adapters = _adapters(cfg)
        return _with_state(cfg, lambda s: bridge.acquire(s, cfg, adapters, run_id=run_id, now_ms=now))
    if command == "next":
        return _with_state(cfg, lambda s: bridge.next_step(s, cfg, run_id=run_id, now_ms=now))
    if command == "status":
        return _with_state(cfg, lambda s: bridge.status(s, cfg))
    if command in ("drive-seal", "drive-record", "gmail-seal", "gmail-record"):
        evidence = bridge.load_evidence(Path(args.evidence))
        function = {"drive-seal": bridge.drive_seal, "drive-record": bridge.drive_record,
                    "gmail-seal": bridge.gmail_seal, "gmail-record": bridge.gmail_record}[command]
        return _with_state(cfg, lambda s: function(s, cfg, args.intent, evidence, run_id=run_id, now_ms=now))
    if command == "drive-readback":
        return _with_state(cfg, lambda s: bridge.drive_readback(s, cfg, args.intent, args.file_id, Path(args.base64_file)))
    if command == "gmail-readback":
        return _with_state(cfg, lambda s: bridge.gmail_readback(s, cfg, args.intent, args.draft_id, Path(args.base64_file)))
    raise bridge.NativeRefusal("EG_NATIVE_COMMAND_UNKNOWN")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="energygrid-native", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init-uat", "acquire", "next", "status"):
        sub.add_parser(name).add_argument("--config", required=True)
    fixture = sub.add_parser("make-synthetic-fixture")
    fixture.add_argument("--config", required=True)
    fixture.add_argument("--listing")
    for name in ("drive-seal", "drive-record", "gmail-seal", "gmail-record"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--intent", required=True)
        command.add_argument("--evidence", required=True)
    for name, provider in (("drive-readback", "--file-id"), ("gmail-readback", "--draft-id")):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--intent", required=True)
        command.add_argument(provider, required=True)
        command.add_argument("--base64-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _dispatch(args)
    except AppError as exc:
        # Fixed vocabulary only: no path, filename, identity or provider text.
        support_ref = getattr(exc, "support_ref", None) or "EG_NATIVE_REFUSED"
        exit_code = exc.exit_code if exc.exit_code in (10, 20, 64) else 64
        result = bridge._result(args.command, "REFUSED" if exit_code == 64 else exc.status, exit_code,
                                support_ref=support_ref, error_class=type(exc).__name__)
        if isinstance(exc, ConfigError):
            # Config messages name the offending key only, never its value.
            result["detail"] = exc.message
    except Exception as exc:  # noqa: BLE001 - closed output; state may have changed, so never exit 64
        result = bridge._result(args.command, "FAILED", 20, support_ref="EG_NATIVE_UNEXPECTED_FAILURE",
                                error_class=type(exc).__name__)
    print(json.dumps(result, sort_keys=True))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
