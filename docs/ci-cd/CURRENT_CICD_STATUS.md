# Current CI/CD status

The member gateway G3 implementation adds a narrow offline GitHub Actions
workflow at `.github/workflows/member-gateway-tests.yml`.

- Pull requests and pushes are limited to the member-gateway pathset, the three
  new cursor/operator schemas, migration 0004, and exact supporting
  test/documentation files.
- The workflow checks out the exact pull-request head and asserts that SHA
  before running checks.
- Python, PowerShell/static, JSON/schema/migration, n8n-export, regression, and
  privacy checks use synthetic/offline data only.
- The workflow has read-only repository permission and contains no deployment,
  Docker, credential, live Google, live n8n, live AutoCount, live PostgreSQL, or
  customer-data step.
- Production activation is disabled in the committed example configuration.
- The hosted jobs are `gateway-tests`, `powershell-static`,
  `powershell-worker-tooling`, `bounded-import-security`, `n8n-offline`,
  `existing-member-regression`, and `full-offline-regression`; no PostgreSQL
  service container is introduced. `powershell-worker-tooling` runs only
  `tests.test_member_gateway_worker_deployment`, the largest single module, in
  parallel with `powershell-static` (#211).
- `full-offline-regression` runs in parallel with the targeted jobs (#211). It
  runs `tests/_run_ci_full_suite.py --skip-member-gateway-targeted`, which
  skips only the whole modules that the windows-latest `powershell-static`,
  `powershell-worker-tooling` and `bounded-import-security` jobs run on the
  same asserted exact head (same OS and Python). Modules run only by the ubuntu
  jobs stay in the Windows full suite. `tests/test_member_gateway_ci.py` proves
  the skip list equals those jobs' module lists and that the targeted shard
  plus the remainder equals the accepted test set.
- `tests/_run_ci_full_suite.py` is in both member-gateway path filters, so a
  runner change triggers the workflow (#211). The workflow also runs every job
  weekly on main (`cron: "17 14 * * 0"`, Sunday 22:17 SGT) and on manual
  dispatch, so modules outside the path filters still get a full run.
  `tests/test_member_gateway_ci.py` pins the path entries and the single cron.
- The member-create-uat workflow's `windows-full-suite` job is a four-leg
  `fail-fast: false` matrix (#211): `focused`, `mg-deployment`,
  `bounded-import` and `rest`, each running
  `tests/_run_ci_full_suite.py --shard <leg>` on the same asserted exact head.
  `rest` is the complement of the named whole-module groups, so new modules
  land there automatically; an unknown shard or a group module that
  contributes no test fails closed. The focused create-UAT modules run once, in
  the `focused` leg, instead of twice. `tests/test_member_create_uat_ci.py`
  pins the matrix and the exact run line, rejects narrowing, conditions,
  exit-code masking and matrix include/exclude, and proves test by test
  against a plain discover that the four legs partition the accepted set.

- Shopify M1 (#155 G3) adds the four Shopify schemas, the Shopify example
  config, migration 0006 and `tests/test_member_gateway_shopify_ps.py` to the
  path filters. `gateway-tests` installs exactly `cryptography==46.0.7` (the
  protected-payload AEAD); it is the only permitted install besides tzdata.
  The real-PostgreSQL migration tests remain local-only and skip in hosted CI.

- Personal-data guard (#211 CI hygiene) adds `.github/workflows/pii-guard.yml`.
  It runs on every pull request with no path filter, checks out the literal
  head with full history and runs `scripts/pii_guard.py --range
  "${BASE_SHA}..${HEAD_SHA}"`, scanning each commit in the range (merge commits
  contribute only lines new to every parent), plus each commit's message (not
  author or committer identities), for likely email addresses and phone numbers outside
  `config/pii_guard_allowlist.json`. Diffs are forced to text; binary files are
  never skipped whole (printable text runs always, BOM text decoded, zips
  found by signature and expanded up to three levels, zips appended to a
  reviewed binary expanded, Office XML runs joined, PDF FlateDecode streams
  decompressed, legacy `.xls` numeric cells decoded, binary types checked
  against their signature, other archives and anything unrecognised fail
  closed; #270). It fails
  closed on a hit, a missing or empty range, an unknown commit or a parse error,
  and it redacts matched values in the log. Scope is email addresses and phone
  numbers only; merge-time commits on `main` and commit identities are not
  scanned. `tests/test_pii_guard.py` pins the trigger,
  checkout, exact run lines and allowlist, and rejects `continue-on-error`,
  `|| true`, `if:` conditions, path filters, a dropped or narrowed range and
  report-only or value-printing flags. The tracked `.githooks/pre-commit` hook
  needs the one-time `git config core.hooksPath .githooks` per clone; see
  `docs/data_policy.md`.

This status file records CI shape only; it is not a deployment approval or a
claim that a remote check has passed before the branch is published.
