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

With ``--skip-member-gateway-targeted`` (used only by the member-gateway workflow's
``full-offline-regression`` job, which runs in parallel with the targeted jobs) the
modules that workflow's targeted windows-latest jobs already run on the same asserted
exact head are skipped as whole modules. ``tests/test_member_gateway_ci.py`` proves
that ``MEMBER_GATEWAY_TARGETED_JOBS`` equals those jobs' module lists and that the
targeted shard plus the remainder is exactly the accepted test set. Without the flag
the behaviour is unchanged.
"""

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
EXCLUDED_MODULES = {"test_import_n8n_workflows_live_dry_run"}
SKIP_MEMBER_GATEWAY_TARGETED_FLAG = "--skip-member-gateway-targeted"
# Module lists of the windows-latest targeted jobs in member-gateway-tests.yml.
MEMBER_GATEWAY_TARGETED_JOBS = {
    "powershell-static": (
        "test_member_gateway_worker_static",
        "test_member_gateway_worker_deployment",
        "test_member_gateway_adapter_ps",
        "test_member_gateway_worker_cycle_ps",
        "test_member_gateway_shopify_ps",
        "test_directlocal_bridge_ps",
        "test_directlocal_realbridge_ps",
        "test_member_directlocal_realcore",
        "test_member_directlocal_poller",
        "test_directlocal_e7_testbook_ps",
        "test_directlocal_code_custody_ps",
    ),
    "bounded-import-security": (
        "test_member_gateway_bounded_import_security",
        "test_member_welcome_email_bounded_import_security",
    ),
}
MEMBER_GATEWAY_TARGETED_MODULES = frozenset(
    module for modules in MEMBER_GATEWAY_TARGETED_JOBS.values() for module in modules
)


def _flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _flatten(test)
        else:
            yield test


def module_name(test):
    """Top-level module of a discovered test id (import failures stay under ``unittest``)."""
    return test.id().split(".", 1)[0]


def accepted_tests(discovered):
    """The accepted CI set: every discovered test outside the environment-gated module."""
    return [test for test in _flatten(discovered) if not any(module in test.id() for module in EXCLUDED_MODULES)]


def partition(tests, skip_modules):
    """Split tests into (kept, skipped) by exact top-level module name."""
    kept, skipped = [], []
    for test in tests:
        (skipped if module_name(test) in skip_modules else kept).append(test)
    return kept, skipped


def main(argv):
    unknown = [arg for arg in argv if arg != SKIP_MEMBER_GATEWAY_TARGETED_FLAG]
    if unknown:
        print(f"CI suite: unknown argument(s) {unknown}")
        return 2
    all_tests = list(_flatten(unittest.TestLoader().discover(str(TESTS_DIR))))
    tests = accepted_tests(all_tests)
    print(f"CI suite: excluded {len(all_tests) - len(tests)} environment-gated test(s) from {sorted(EXCLUDED_MODULES)}")
    skip_modules = MEMBER_GATEWAY_TARGETED_MODULES if SKIP_MEMBER_GATEWAY_TARGETED_FLAG in argv else frozenset()
    kept, skipped = partition(tests, skip_modules)
    missing = sorted(skip_modules - {module_name(test) for test in skipped})
    if missing:
        # A skip-listed module that contributes no test means the list drifted; fail closed.
        print(f"CI suite: targeted module(s) not discovered: {missing}")
        return 2
    if skip_modules:
        print(
            f"CI suite: skipped {len(skipped)} test(s) in {len(skip_modules)} module(s) already run by the "
            f"member-gateway targeted Windows jobs on this exact head: {sorted(skip_modules)}"
        )
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(kept))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
