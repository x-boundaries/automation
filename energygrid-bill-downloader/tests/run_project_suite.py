"""Run one CI shard of the EnergyGrid project test suite, optionally leaving out named modules.

CI applicability routing (#226): the hosted supervisor custody tests in
test_one_shot_supervisor.py bind an exact approved candidate branch, so they run only
when a pull request changes the supervisor or its protected harness boundary. Every
other module is discovered exactly as `python -m unittest discover -s tests` would.

CI sharding (#211): the synthetic-windows job is a matrix with one leg per shard. Each
named shard lists its modules; `rest` is every module no named shard lists, so a new test
module always lands in a leg. The named shards and `rest` partition the discovered modules.
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Balanced from per-module timings on a hosted Windows runner (about 8 min a leg).
NAMED_SHARDS: dict[str, tuple[str, ...]] = {
    "runtime-launcher": ("test_runtime_launcher",),
    "portal-hardening": ("test_portal_synthetic", "test_production_hardening"),
    "supervisor-http-daily": ("test_claude_supervisor", "test_direct_http", "test_native_daily"),
    "bridge-state-orchestration": (
        "test_native_bridge",
        "test_state_v4",
        "test_orchestration",
        "test_orchestration_v4",
        "test_run156_helper_static",
    ),
}
REST_SHARD = "rest"
SHARDS = (*NAMED_SHARDS, REST_SHARD)


def module_names(start: Path = TESTS_DIR) -> list[str]:
    return sorted(path.stem for path in start.glob("test_*.py"))


def shard_modules(shard: str, start: Path = TESTS_DIR) -> list[str]:
    """Return the modules one shard runs; fail closed on an unknown or drifted shard map."""
    if shard not in SHARDS:
        raise SystemExit("unknown shard: " + shard)
    available = module_names(start)
    claimed = [name for names in NAMED_SHARDS.values() for name in names]
    duplicated = sorted({name for name in claimed if claimed.count(name) > 1})
    if duplicated:
        raise SystemExit("test module(s) in more than one shard: " + ", ".join(duplicated))
    missing = sorted(set(claimed).difference(available))
    if missing:
        raise SystemExit("shard module(s) not discovered: " + ", ".join(missing))
    if shard == REST_SHARD:
        return [name for name in available if name not in claimed]
    return sorted(NAMED_SHARDS[shard])


def build_suite(
    excluded: set[str], start: Path = TESTS_DIR, shard: str | None = None
) -> unittest.TestSuite:
    available = module_names(start)
    unknown = sorted(excluded.difference(available))
    if unknown:
        raise SystemExit("unknown test module(s) to exclude: " + ", ".join(unknown))
    selected = available if shard is None else shard_modules(shard, start)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in selected:
        if name not in excluded:
            # Per-module discovery keeps discover's semantics, including import failures
            # reported as failing tests instead of being skipped.
            suite.addTests(loader.discover(str(start), pattern=name + ".py"))
    if shard is not None and suite.countTestCases() == 0:
        raise SystemExit("shard selected no tests: " + shard)
    return suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", choices=SHARDS, required=True)
    parser.add_argument("--exclude-module", action="append", default=[])
    args = parser.parse_args(argv)
    # Run as a script, Python puts tests/ first on sys.path; `python -m unittest` from the
    # project folder puts the project folder there instead, which the package imports need.
    sys.path.insert(0, str(TESTS_DIR.parent))
    result = unittest.TextTestRunner(verbosity=2).run(
        build_suite(set(args.exclude_module), shard=args.shard)
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
