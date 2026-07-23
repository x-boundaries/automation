"""CI helper: run the repository unittest suite, excluding modules that require a
runtime environment a stock CI runner does not provide.

Underscore-prefixed so unittest discovery (pattern ``test*.py``) never collects it.

Only ``test_import_n8n_workflows_live_dry_run`` is excluded: those tests drive the
n8n live-import helper against a Docker/n8n environment (container listing, import
confirmation gate, prepared-dir isolation) that is not available on a stock GitHub
runner and must not be simulated by running Docker in CI. That module, and the
complete suite including it, are validated locally on the dev/VM host (where Docker
and the Windows timezone database are present). Every other test, including all
create-UAT tests and the repository static guardrails, runs here.
"""

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
EXCLUDED_MODULES = {"test_import_n8n_workflows_live_dry_run"}


def _flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _flatten(test)
        else:
            yield test


def main():
    loader = unittest.TestLoader()
    discovered = loader.discover(str(TESTS_DIR))
    kept = unittest.TestSuite()
    excluded = 0
    for test in _flatten(discovered):
        if any(module in test.id() for module in EXCLUDED_MODULES):
            excluded += 1
            continue
        kept.addTest(test)
    print(f"CI suite: excluded {excluded} environment-gated test(s) from {sorted(EXCLUDED_MODULES)}")
    result = unittest.TextTestRunner(verbosity=1).run(kept)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
