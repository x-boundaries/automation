"""Run the EnergyGrid project test suite with named test modules left out.

CI applicability routing (#226): the hosted supervisor custody tests in
test_one_shot_supervisor.py bind an exact approved candidate branch, so they run only
when a pull request changes the supervisor or its protected harness boundary. Every
other module is discovered exactly as `python -m unittest discover -s tests` would.
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def module_names(start: Path = TESTS_DIR) -> list[str]:
    return sorted(path.stem for path in start.glob("test_*.py"))


def build_suite(excluded: set[str], start: Path = TESTS_DIR) -> unittest.TestSuite:
    available = module_names(start)
    unknown = sorted(excluded.difference(available))
    if unknown:
        raise SystemExit("unknown test module(s) to exclude: " + ", ".join(unknown))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in available:
        if name not in excluded:
            # Per-module discovery keeps discover's semantics, including import failures
            # reported as failing tests instead of being skipped.
            suite.addTests(loader.discover(str(start), pattern=name + ".py"))
    return suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exclude-module", action="append", default=[], required=True)
    args = parser.parse_args(argv)
    # Run as a script, Python puts tests/ first on sys.path; `python -m unittest` from the
    # project folder puts the project folder there instead, which the package imports need.
    sys.path.insert(0, str(TESTS_DIR.parent))
    result =unittest.TextTestRunner(verbosity=2).run(build_suite(set(args.exclude_module)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
