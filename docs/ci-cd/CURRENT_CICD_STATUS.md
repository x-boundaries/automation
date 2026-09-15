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
- The five named hosted jobs remain `gateway-tests`, `powershell-static`,
  `n8n-offline`, `existing-member-regression`, and dependent
  `full-offline-regression`; no PostgreSQL service container is introduced.

This status file records CI shape only; it is not a deployment approval or a
claim that a remote check has passed before the branch is published.
