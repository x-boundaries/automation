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

With ``--shard NAME`` (used only by the member-create-uat workflow's
``windows-full-suite`` matrix legs) the accepted set is split into whole-module
shards: the named groups in ``CREATE_UAT_SHARD_GROUPS`` plus ``rest``, the complement
of every named group, so a new module lands in ``rest`` automatically. An unknown
shard, a second argument, or a named-group module that contributes no test fails
closed. ``tests/test_member_create_uat_ci.py`` proves the matrix legs equal
``CREATE_UAT_SHARDS`` and that the shards partition the accepted set test by test.
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
    "powershell-worker-tooling": ("test_member_gateway_worker_deployment",),
    "bounded-import-security": (
        "test_member_gateway_bounded_import_security",
        "test_member_welcome_email_bounded_import_security",
    ),
}
MEMBER_GATEWAY_TARGETED_MODULES = frozenset(
    module for modules in MEMBER_GATEWAY_TARGETED_JOBS.values() for module in modules
)
SHARD_FLAG = "--shard"
# Named whole-module groups of the windows-full-suite matrix in member-create-uat-tests.yml.
CREATE_UAT_SHARD_GROUPS = {
    "focused": (
        "test_member_create_uat_contract",
        "test_member_create_uat_approval",
        "test_member_create_uat_result_precheck",
        "test_member_create_uat_runner_ps",
        "test_ac2_member_expiry_capability_probe",
    ),
    "mg-deployment": ("test_member_gateway_worker_deployment",),
    "bounded-import": (
        "test_member_gateway_bounded_import_security",
        "test_member_welcome_email_bounded_import_security",
    ),
}
REST_SHARD = "rest"
CREATE_UAT_SHARDS = (*CREATE_UAT_SHARD_GROUPS, REST_SHARD)


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


def shard_tests(tests, shard):
    """(selected tests, modules the shard must contribute) for one create-UAT shard."""
    if shard == REST_SHARD:
        named = frozenset(module for modules in CREATE_UAT_SHARD_GROUPS.values() for module in modules)
        return partition(tests, named)[0], frozenset()
    modules = frozenset(CREATE_UAT_SHARD_GROUPS[shard])
    return partition(tests, modules)[1], modules


def run_shard(argv):
    if len(argv) != 2 or argv[1] not in CREATE_UAT_SHARDS:
        print(f"CI suite: {SHARD_FLAG} takes exactly one of {list(CREATE_UAT_SHARDS)}; got {argv[1:]}")
        return 2
    shard = argv[1]
    loader = unittest.TestLoader()
    all_tests = list(_flatten(loader.discover(str(TESTS_DIR))))
    tests = accepted_tests(all_tests)
    print(f"CI suite: excluded {len(all_tests) - len(tests)} environment-gated test(s) from {sorted(EXCLUDED_MODULES)}")
    selected, required = shard_tests(tests, shard)
    # A group module that contributes no test (import failure or drifted list), or an
    # empty shard, fails closed after the shard has run so that log stays useful.
    missing = sorted(required - {module_name(test) for test in selected})
    if missing:
        print(f"CI suite: shard module(s) contributed no test and will fail this run: {missing}")
        for error in loader.errors:
            print(error)
    print(
        f"CI suite: shard {shard!r} runs {len(selected)} of {len(tests)} accepted test(s) in "
        f"{len({module_name(test) for test in selected})} module(s); the other shards run the rest on this exact head"
    )
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(selected))
    if missing or not selected:
        print(f"CI suite: failing closed; shard {shard!r} is empty or missing module(s): {missing}")
        return 2
    return 0 if result.wasSuccessful() else 1


def main(argv):
    if argv[:1] == [SHARD_FLAG]:
        return run_shard(argv)
    unknown = [arg for arg in argv if arg != SKIP_MEMBER_GATEWAY_TARGETED_FLAG]
    if unknown:
        print(f"CI suite: unknown argument(s) {unknown}")
        return 2
    loader = unittest.TestLoader()
    all_tests = list(_flatten(loader.discover(str(TESTS_DIR))))
    tests = accepted_tests(all_tests)
    print(f"CI suite: excluded {len(all_tests) - len(tests)} environment-gated test(s) from {sorted(EXCLUDED_MODULES)}")
    skip_modules = MEMBER_GATEWAY_TARGETED_MODULES if SKIP_MEMBER_GATEWAY_TARGETED_FLAG in argv else frozenset()
    kept, skipped = partition(tests, skip_modules)
    # A skip-listed module that contributes no test (import failure or drifted list)
    # fails closed, after the remainder has run so that log stays useful.
    missing = sorted(skip_modules - {module_name(test) for test in skipped})
    if missing:
        print(f"CI suite: targeted module(s) contributed no test and will fail this run: {missing}")
        for error in loader.errors:
            print(error)
    if skip_modules:
        print(
            f"CI suite: skipped {len(skipped)} test(s) in {len(skip_modules) - len(missing)} module(s) already run by "
            f"the member-gateway targeted Windows jobs on this exact head: {sorted(set(skip_modules) - set(missing))}"
        )
    result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(kept))
    if missing:
        print(f"CI suite: failing closed; targeted module(s) contributed no test: {missing}")
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
