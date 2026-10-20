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
  `bounded-import-security`, `n8n-offline`, `existing-member-regression`, and
  `full-offline-regression`; no PostgreSQL service container is introduced.
- `full-offline-regression` runs in parallel with the targeted jobs (#211). It
  runs `tests/_run_ci_full_suite.py --skip-member-gateway-targeted`, which
  skips only the whole modules that the windows-latest `powershell-static` and
  `bounded-import-security` jobs run on the same asserted exact head (same OS
  and Python). Modules run only by the ubuntu jobs stay in the Windows full
  suite. `tests/test_member_gateway_ci.py` proves the skip list equals those
  jobs' module lists and that the targeted shard plus the remainder equals the
  accepted test set. The member-create-uat workflow calls the runner without
  the flag and still runs the whole accepted set.

- Shopify M1 (#155 G3) adds the four Shopify schemas, the Shopify example
  config, migration 0006 and `tests/test_member_gateway_shopify_ps.py` to the
  path filters. `gateway-tests` installs exactly `cryptography==46.0.7` (the
  protected-payload AEAD); it is the only permitted install besides tzdata.
  The real-PostgreSQL migration tests remain local-only and skip in hosted CI.

This status file records CI shape only; it is not a deployment approval or a
claim that a remote check has passed before the branch is published.
