# Member Intake n8n Lookup Bridge UAT Plan

Status: Gate 4 real queue UAT plan only. This document does not add a workflow export, does not activate n8n, does not run Gate 4, does not touch real queue data from this PR, does not expose the AutoCount host, and does not authorize AutoCount writes.

## Purpose

This runbook defines the review-only UAT path for cloud/VPS/non-AC2 n8n to orchestrate AC2 member duplicate-check routing through the local Windows AC2 lookup bridge.

Google Forms / Google Sheets are a temporary UAT intake surface only. The bridge queue contract should stay source-agnostic enough to survive a future custom web form or hosted intake API that can reject duplicate mobile/member numbers before submission. This PR does not implement that custom form, hosted intake API, or synchronous duplicate rejection.

AC2 / AutoCount 2.0 remains the source of truth. The Google Form mobile/member number maps to AutoCount `MemberNo`. AutoCount `MobilePhone` remains intentionally unused. Birthday Month maps to future AC2 `DOB` as `2000-MM-01`, but DOB is outside this lookup-only bridge step. `PDPA Acknowledged = Yes` is valid new-form consent. `PDPA Acknowledged = Imported` is a legacy/import marker only and remains blocked. `READY_FOR_CREATE_REVIEW` is review-only and is not approval to create.

## n8n-skills Plugin Evidence Checked

This plan is n8n-skills-backed. The current planning source is the n8n-skills plugin plus official n8n node/runtime documentation checked before writing this runbook. Live n8n instance tooling is a separate future verification path for exact workflow build details, not the source of this UAT plan.

| Source checked | Finding used in this UAT plan |
| --- | --- |
| `n8n-skills:using-n8n-skills` | Do not rely on remembered node shapes. Treat live instance tools such as `get_node_types`, validation, and post-save verification as future build verification, not current planning evidence. |
| `n8n-skills:n8n-workflow-lifecycle` | Keep this as a plan/spec because validation, verification, and test execution are required before publishing. Do not publish or activate from this PR. |
| `n8n-skills:n8n-node-configuration` | Exact node parameters must be verified with available live n8n tooling in a future build/export/activation PR if that tooling is exposed. This PR names node families and operations but does not claim final import-ready parameters. |
| `n8n-skills:n8n-credentials-and-security` | Google credentials and any queue/API credentials must live in n8n's credential system or local bridge configuration, never workflow text, docs, tests, or queue rows. |
| `n8n-skills:n8n-data-tables` | Data Tables are n8n-internal, light-to-moderate storage with primitive columns and system-managed `id`, `createdAt`, and `updatedAt`; they are not a cross-system source of truth. |
| `n8n-skills:n8n-error-handling` | Unattended scheduled/queue workflows need structured error routes, retries for transient upstream failures, and workflow-level error handling before production. |
| `n8n-skills:n8n-loops` | Do not add Loop Over Items just to process rows; n8n already processes items sequentially by default. Use explicit batching only for rate limits or controlled polling. |
| `n8n-skills:n8n-expressions` | Reference stable upstream nodes by name and avoid Set/Edit Fields nodes unless they define a reused, whitelisted boundary shape. |
| `n8n-skills` Data Table schema and dedup references | Use explicit columns, stable non-PII idempotency keys, payload hashes, lease fields, and same-shape branches for retries/results. |
| `n8n-skills` testing reference | `test_workflow` would pin triggers and credentialed nodes, but Data Tables, Wait, file operations, Execute Command, and Code run for real; do not run tests with side effects without a separate approval. |
| `n8n-skills` live tooling reference | The plugin describes optional live instance tools for future workflow builds, including node type discovery, validation, workflow detail verification, and execution inspection. Those tools were not exposed in this Codex thread, which limits import-ready build details but does not block this UAT plan. |
| Official n8n Google Sheets Trigger docs | Google Sheets Trigger supports row-added, row-updated, and row-added-or-updated events, but this UAT prefers a scheduled poller to avoid relying on trigger behavior that was not verified against a live n8n instance in this Codex session. |
| Official n8n Google Sheets node docs | The Sheet Within Document operations include Append Row, Append or Update Row, Get Row(s), and Update Row. Get Row(s) returns only the first match by default unless configured to return all matches. Update Row updates existing rows only. Append options can misalign data if a sheet has gaps. |
| Official n8n Data Table docs | Data tables can persist workflow-local state, support row operations, can be managed through the UI/node/API, have a default total storage limit, are project-visible, and are not directly accessible from Code node built-ins. |
| Official n8n Wait node docs | Wait pauses execution and offloads execution data to the n8n database. Wait can resume on a runtime webhook URL, but that pattern is not recommended here because the AC2 host must not expose a public inbound callback and UAT should minimize stored execution data. |
| Official n8n Schedule Trigger docs | Schedule Trigger supports seconds/minutes/hours/days/weeks/months/custom intervals; UAT pollers should set explicit cadence and workflow timezone before activation. |
| Official n8n execution data and redaction docs | n8n can reduce saved execution data, prune old execution data, and redact production/manual execution payloads. This UAT must enable the most restrictive available settings before any live run. |

## Future Live Workflow Verification Status

The n8n-skills plugin and official n8n documentation are the current planning source for this PR. Codex tool discovery did not expose live n8n instance tools such as `search_nodes`, `get_node_types`, `get_sdk_reference`, `validate_workflow`, `search_workflows`, `get_workflow_details`, `search_data_tables`, or `create_data_table`.

That is not a blocker for this UAT-only plan. It does mean this PR cannot claim live verification of node parameter schemas, credential IDs, resource locator values, workflow settings names, execution behavior, or Data Table table IDs. Those exact build details remain blockers for any future workflow build/export/activation PR.

A future workflow build/export/activation PR must verify exact node schemas, credentials, resource selectors, workflow settings, and execution behavior using available live n8n tooling and official n8n verification if exposed in that environment. That future verification should include, as available:

- `get_sdk_reference` for workflow SDK shape.
- `search_nodes` and `get_node_types` for Google Sheets, Schedule Trigger, If, Switch, Edit Fields, and any Data Table nodes.
- `list_credentials` and resource exploration for the exact Google Sheet and tabs.
- `validate_workflow` before saving.
- `get_workflow_details` after saving to verify connections.
- `prepare_test_pin_data`, then `test_workflow` only after side-effect approval.
- `get_execution` with truncated/redacted data only for reviewed nodes.

## UAT Architecture

```text
Google Form responses in Google Sheets
  -> cloud/VPS/non-AC2 n8n scheduled poller
  -> UAT-only Google Sheets queue tab with allowed fields only
  <- local Windows AC2 lookup bridge polls outbound
  <- sanitized result tab
  -> n8n result poller updates review/status fields only
```

The local Windows AC2 lookup bridge is the only component allowed to load AutoCount assemblies or run `scripts/ac2_member_lookup_review.ps1`. The bridge polls outbound. There is no public inbound webhook, tunnel, reverse proxy, or callback on the AutoCount host in the recommended UAT path.

Cloud/VPS/non-AC2 n8n direct Execute Command to local AC2 remains invalid because Execute Command would run on the n8n host/container, not on the AutoCount Windows host.

## Queue Option Comparison

| Option | Pros | Cons | UAT decision |
| --- | --- | --- | --- |
| Google Sheets queue tab | Fits the current Google Form/Sheets intake surface, is easy for reviewers to inspect, requires no new queue infrastructure, and lets the local bridge poll outbound with a dedicated credential. | Stores the encoded submitted member value in a spreadsheet cell, has weaker leasing/atomicity than a real queue, needs careful hidden/protected tabs, and can be changed manually by users with sheet access. | Recommended for this UAT only. It must be disabled or replaced before production activation. |
| n8n Data Table / internal storage | Official n8n docs support workflow-local persistent state, row operations, UI/API access, and idempotency metadata. This avoids adding queue columns to the intake spreadsheet. | The bridge must call n8n's DataTable API or another verified bridge-access path. Live n8n instance tooling and API auth behavior were not available in this session, Data Tables have storage limits, and Data Table writes run for real during tests. | Candidate for a later controlled UAT after future live-instance verification. Not the first UAT queue. |
| Lightweight external queue/API | Best long-term separation for leases, retries, audit, access control, and bridge outbound polling. | Requires new infrastructure, credential management, monitoring, rate limits, and an API contract review. It is more setup than needed for the dry-run duplicate-check UAT. | Preferred direction after UAT if this flow moves beyond review-only rehearsal. |
| Local file drop only for fixture mode | Safest for bridge worker unit tests and offline synthetic fixtures. No network or cloud dependency. | Not a cloud/VPS/non-AC2 n8n bridge, cannot prove cross-host polling, and should not be used as the runtime queue. | Fixture-only. Keep local under `C:\XB\autocount_outputs\review\...` and never commit row-level outputs. |

Recommended UAT approach: use a Google Sheets queue tab, explicitly UAT-only, with protected tabs and allowed columns only. This keeps the first end-to-end rehearsal close to the existing Google Form workflow while preserving the final architecture rule that n8n stays off the AutoCount host and the bridge polls outbound.

For the setup sequence and required preflight gates, see [member_intake_n8n_uat_setup_runbook.md](member_intake_n8n_uat_setup_runbook.md). The minimum next runnable n8n UAT step is a manual, inactive hosted/VPS/non-AC2 n8n rehearsal against dummy Google Sheets UAT tabs, with no AC2 lookup touch. That dummy n8n rehearsal may proceed before the PowerShell lookup preflight because it does not call AC2. Before any real n8n queue UAT is allowed to touch AC2 lookup, the local bridge worker PowerShell lookup mode must pass once with fixture input, safe synthetic or manually approved lookup input only, and aggregate sanitized paste-back evidence only.

Required preflight gate before real AC2-touching queue UAT:

- run locally on the approved Windows AC2 lookup bridge host,
- use `--queue-mode fixture`,
- use `--lookup-mode powershell` and `--enable-powershell-lookup`,
- call only the proven read-only lookup script with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8`,
- keep local target settings and `AC2_PROBE_PASSWORD` in runtime-only local configuration,
- paste back only aggregate sanitized status/count evidence,
- do not paste secrets, raw fixture rows, raw result rows, encoded submitted values, normalized values, names, emails, raw phone numbers, Sheet IDs, Sheet URLs, command transcripts, stderr/stdout, local target details, or row-level output.

## UAT Sheet Tabs

Use placeholder tab names in docs and tests; configure real spreadsheet IDs only in n8n credentials/resource selectors or local bridge configuration.

| Tab | Owner | Purpose |
| --- | --- | --- |
| `Form Responses UAT` | Google Form / reviewer | Source rows and review/status fields. Raw form fields stay here and are never copied into queue/result tabs except the transient encoded lookup field in the queue. |
| `Lookup Queue UAT` | n8n writes, bridge polls/leases | `PENDING_LOOKUP` jobs with the allowed request fields below. UAT-only. |
| `Lookup Results UAT` | bridge writes, n8n polls | Sanitized result metadata only. No raw, encoded, or normalized member values. |
| `UAT Audit Summary` | n8n writes | Aggregate counts and timestamps only. No row-level PII. |

## Allowed Queue Request Fields

`Lookup Queue UAT` may contain only:

| Field | Required | Notes |
| --- | --- | --- |
| `job_id` | Yes | Stable non-PII idempotency key. |
| `intake_source` | Yes | Safe source label such as `google_sheets_uat`; not a permanent Google Forms dependency. |
| `source_reference` | Yes | Safe source-agnostic reference for the intake row/submission. |
| `source_row_ref` | No | Optional source row/reference label. |
| `row_number` | No | Spreadsheet row number for the first Google Sheets UAT only. |
| `intake_id` | No | Internal non-PII intake identifier if one already exists. |
| `state` | Yes | Starts as `PENDING_LOOKUP`. |
| `submitted_member_no_base64_utf8` | Yes | Encoded submitted value for the bridge. Sensitive operational data; UAT-only; never copied to review/status fields. |
| `consent_status` | No | Separate sanitized consent/marketing category only; never a PDPA override. |
| `pdpa_status` | Yes | Mandatory duplicate-check gate. Must be a valid new-form PDPA acknowledgement; `Imported` is not valid consent and remains blocked. |
| `payload_hash` | Yes | Hash of allowed request fields used for idempotency. |
| `attempt` | Yes | Starts at `0`; increments on retry. |
| `max_attempts` | Yes | Small UAT retry cap configured outside the row values. |
| `created_at` | Yes | n8n queue timestamp. |
| `updated_at` | Yes | Last state update timestamp. |
| `lease_owner` | No | Non-PII bridge instance label. |
| `lease_expires_at` | No | Timestamp for retry sweep. |
| `timeout_at` | Yes | Time after which n8n routes to `LOOKUP_ERROR_REVIEW` if no sanitized result appears. |
| `last_error_code` | No | Sanitized category only. |

The queue must not contain raw member numbers, normalized member numbers, names, emails, raw phone numbers, DOB, address fields, AutoCount internal identifiers, local host/database/user values, credentials, connection strings, command text, stderr/stdout, arbitrary payload dumps, Sheet URLs, Sheet IDs, or reviewer free text.

## Allowed Result Fields

`Lookup Results UAT` may contain only:

| Field | Required | Notes |
| --- | --- | --- |
| `job_id` | Yes | Matches the queue job. |
| `intake_source` | Yes | Echoes safe source label only. |
| `source_reference` | Yes | Echoes safe source reference only. |
| `source_row_ref` | No | Echoes safe source row/reference label only. |
| `row_number` | No | Spreadsheet row metadata for the first Google Sheets UAT only. |
| `state` | Yes | One of the review states below. |
| `status` | Yes | Expected `ok` for successful lookup processing. |
| `authentication_success` | Yes | Boolean status only. |
| `user_session_available` | Yes | Boolean status only. |
| `member_command_found` | Yes | Boolean status only. |
| `get_member_found` | Yes | Boolean status only. |
| `submitted_member_no_status` | Yes | Shape/status label only. |
| `normalized_member_no_length` | Yes | Length only; never the normalized value. |
| `member_exists` | Yes | Duplicate-check outcome. |
| `member_found_by` | No | Sanitized method label only when found. |
| `manual_review_required` | Yes | Boolean routing flag. |
| `warning_count` | Yes | Any warning blocks ready-for-create review. |
| `error_code` | No | Sanitized category only. |
| `consent_status` | No | Sanitized category only; not used to satisfy PDPA. |
| `pdpa_status` | Yes | Sanitized PDPA category only. |
| `attempt` | Yes | Attempt that produced the result. |
| `dry_run_only` | Yes | Must be true. |
| `final_write_automation` | Yes | Must be false. |
| `result_created_at` | Yes | Bridge result timestamp. |
| `result_applied_at` | No | Set after n8n updates review/status fields. |

Forbidden result fields are the same as forbidden request fields. The result must not include `submitted_member_no_base64_utf8`.

## Fixture-First Bridge Poller Skeleton

`scripts/ac2_member_lookup_bridge_worker.py` models the future queue bridge as a local fixture-only poller in this PR. The first UAT fixture can mirror Google Sheets queue rows, but the queue fields include source-agnostic metadata such as `intake_source`, `source_reference`, and `source_row_ref`. It does not call Google APIs, does not use a network client, does not require credentials, does not read real Sheet IDs or Sheet URLs, and does not activate a production queue.

Fixture mode maps to the future tabs as follows:

| Future UAT tab | Fixture file role | Contract |
| --- | --- | --- |
| `Lookup Queue UAT` | `member_lookup_bridge_fixture_jobs.jsonl` | Local JSON/JSONL rows using only the allowed queue request fields above; Google Sheets row metadata is UAT-only. |
| `Lookup Results UAT` | `member_lookup_bridge_results.jsonl` | Local JSONL rows using only the allowed result fields above. |
| Local mock lookup control | `member_lookup_bridge_mock_results.jsonl` | Optional sanitized mock lookup outcomes keyed by non-PII `job_id`; this is not a future queue column source. |

Default invocation refuses. Fixture queue processing requires `--enable-local-lookup-bridge-review`, `--queue-mode fixture`, a local fixture input, and a local results output. Mock mode is the default lookup mode and returns sanitized result rows for review routing tests only.

`pdpa_status` is mandatory and must represent valid new-form PDPA acknowledgement before lookup can proceed. `consent_status` is separate optional sanitized metadata for non-PDPA consent or marketing categories; it must not rescue, override, or reinterpret an invalid, missing, or imported `pdpa_status`.

The fixture contract should still work if a future intake source is a custom web form or hosted intake API instead of Google Forms. That future intake can add pre-submit duplicate rejection later, but this PR intentionally does not implement it.

PowerShell lookup mode remains optional and local-only. It requires both `--lookup-mode powershell` and `--enable-powershell-lookup`; it may call only the proven read-only lookup script with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8`. `-AllowRootLogin` remains a separate local proof option when explicitly required. The skeleton never writes to AutoCount.

Runtime fixture and result files remain local under `C:\XB\autocount_outputs\review\member_lookup_bridge\...` and must not be committed.

## Job States

| State | Meaning |
| --- | --- |
| `PENDING_LOOKUP` | n8n queued a review-only lookup request. |
| `LOOKUP_IN_PROGRESS` | Bridge leased or started the job. |
| `LOOKUP_ERROR_REVIEW` | Timeout, process failure, invalid JSON, schema mismatch, sanitized lookup error, retry exhaustion, or unexpected shape. |
| `MANUAL_REVIEW_REQUIRED` | Lookup or normalization requires human review. |
| `EXISTING_MEMBER_REVIEW` | AC2 lookup found an existing member number. |
| `READY_FOR_CREATE_REVIEW` | Lookup did not find the member and emitted no warning; review-only and not approval to create. |

No state authorizes member creation.

## Reviewer Status Fields

n8n may update only these review/status fields on the form response tab:

| Field | Values / notes |
| --- | --- |
| `uat_lookup_job_id` | Non-PII job id. |
| `uat_lookup_state` | Final review state only. |
| `uat_lookup_status` | Sanitized status code only. |
| `uat_lookup_error_code` | Sanitized category only. |
| `uat_lookup_warning_count` | Count only. |
| `uat_lookup_attempt` | Attempt count only. |
| `uat_lookup_queued_at` | Timestamp. |
| `uat_lookup_completed_at` | Timestamp. |
| `reviewer_status` | `UNREVIEWED`, `IN_REVIEW`, or `REVIEWED_NO_CREATE_APPROVAL`. |
| `reviewer_decision_code` | Optional controlled code; no free text. |

Free-text reviewer notes are out of scope for this UAT because they can accidentally capture PII.

## Node-Level Workflow Shape

No workflow export is committed. The exact resource IDs, credential IDs, column selectors, and node parameter names must be verified through available live n8n tooling and official n8n verification before any build/export/activation.

### Workflow A: Queue Lookup Jobs

1. `Schedule Trigger` named `Poll Form Rows For Lookup UAT`
   - Runs on an explicit UAT cadence and workflow timezone.
   - Chosen over Google Sheets Trigger because the trigger's exact polling/update behavior was not live-verified in this Codex session.
2. `Google Sheets` node named `Read UAT Rows Marked For Lookup`
   - Resource: Sheet Within Document.
   - Operation: Get Row(s).
   - Reads rows with a helper/status column such as `uat_lookup_ready = QUEUE_ME`.
   - Configure return-all-matches behavior; official docs say the default filter result can return only the first match.
3. `If` node named `Block Already Queued Rows`
   - Skips rows that already have `uat_lookup_job_id` or a terminal `uat_lookup_state`.
4. `If` node named `Apply Intake And PDPA Guards`
   - Requires expected headers and valid form state.
   - Allows only `PDPA Acknowledged = Yes`.
   - Blocks `PDPA Acknowledged = Imported`.
5. `Edit Fields` or verified equivalent named `Build Allowed Queue Job`
   - Whitelists only allowed queue request fields.
   - Produces `job_id`, `payload_hash`, timestamps, retry fields, and `submitted_member_no_base64_utf8`.
   - Blocker: exact UTF-8 base64 implementation in n8n must be verified with live node/runtime docs before activation.
6. `If` node named `Validate Encoded Member Value`
   - Requires present value, length multiple of four, and `^[A-Za-z0-9+/]+={0,2}$`.
   - Invalid encoded values route to review/status update with `LOOKUP_ERROR_REVIEW`.
7. `Google Sheets` node named `Append UAT Queue Job`
   - Resource: Sheet Within Document.
   - Operation: Append Row.
   - Manual mapping only for allowed queue request columns.
   - Do not use auto-mapping that can add unexpected columns.
8. `Google Sheets` node named `Mark Form Row Queued`
   - Resource: Sheet Within Document.
   - Operation: Update Row.
   - Updates only `uat_lookup_job_id`, `uat_lookup_state`, `uat_lookup_status`, and queued timestamp fields.

### Workflow B: Apply Sanitized Results

1. `Schedule Trigger` named `Poll Lookup Results UAT`
   - Runs independently so no execution waits with raw form context in memory or stored execution data.
2. `Google Sheets` node named `Read Bridge Results Ready For n8n`
   - Resource: Sheet Within Document.
   - Operation: Get Row(s).
   - Reads result rows with a helper/status column such as `ready_for_n8n = READY`.
3. `If` or `Switch` node named `Validate Result Schema`
   - Requires exactly the allowed result fields and expected types.
   - Missing fields, invalid field types, invalid JSON shape, or unexpected values route to `LOOKUP_ERROR_REVIEW`.
4. `Switch` node named `Map Result To Review State`
   - `status != ok`, timeout, retry exhaustion, schema mismatch, process failure, or unexpected shape -> `LOOKUP_ERROR_REVIEW`.
   - `manual_review_required = true` or `warning_count > 0` -> `MANUAL_REVIEW_REQUIRED`.
   - `member_exists = true` -> `EXISTING_MEMBER_REVIEW`.
   - `member_exists = false` and `warning_count = 0` -> `READY_FOR_CREATE_REVIEW`.
5. `Google Sheets` node named `Update Form Review Fields`
   - Resource: Sheet Within Document.
   - Operation: Update Row.
   - Updates only the review/status fields listed above.
6. `Google Sheets` node named `Mark Result Applied`
   - Resource: Sheet Within Document.
   - Operation: Update Row.
   - Sets `result_applied_at` and a sanitized applied status.

### Workflow C: Timeout And Retry Sweep

1. `Schedule Trigger` named `Sweep UAT Lookup Timeouts`
2. `Google Sheets` node named `Read Expired Lookup Jobs`
   - Reads rows with helper columns computed in the sheet, such as `is_expired_for_n8n = TRUE`.
   - This avoids guessing whether the Google Sheets node supports date comparisons beyond documented equality-style filters.
3. `If` node named `Retry Or Error Review`
   - If `attempt < max_attempts`, set the job back to `PENDING_LOOKUP` and increment attempt metadata.
   - Otherwise route to `LOOKUP_ERROR_REVIEW`.
4. `Google Sheets` node named `Update Timeout Review Fields`
   - Updates queue and form review/status fields only.

### Bridge Poller

The bridge poller is outside n8n. It runs on the approved Windows AC2 host, polls `Lookup Queue UAT` outbound, leases one `PENDING_LOOKUP` job, calls the proven read-only lookup script with `-EnableMemberLookupReview`, `-AllowRootLogin` only when explicitly configured for the local proof, and `-MemberNoBase64Utf8`, then posts only allowed result fields to `Lookup Results UAT`.

The bridge must not return raw, encoded, or normalized member values. Runtime outputs remain local under `C:\XB\autocount_outputs\review\...` and must not be committed.

## Wait/Poll Decision

Do not use the Wait node for the recommended UAT result path.

Official n8n docs state that Wait pauses execution and offloads execution data to the database. Wait's webhook-resume mode also creates a runtime resume URL. That is not the default path here because:

- the AutoCount host must not receive a public inbound webhook,
- long waiting executions are not pruned while waiting,
- the waiting execution could retain sensitive form context,
- independent schedule pollers make retry, timeout, and execution-data minimization easier to review.

Use scheduled result polling instead.

## Execution Data And Log Exposure Controls

Before any UAT run:

- Enable n8n execution-data redaction for production executions, and manual executions too if UAT uses real or production-like form rows.
- Configure workflow-level saved execution data to keep the minimum available data, preferably error-only, and do not save node progress where the instance permits it.
- Enable pruning with a short UAT retention window approved by the operator.
- Do not set custom execution data with member, encoded, normalized, or row-level personal fields.
- Do not log raw form fields, encoded values, normalized values, command arguments, credentials, Sheet IDs, Sheet URLs, bridge stderr/stdout, or local target details.
- Do not run `test_workflow` against Data Table, file, Execute Command, Wait, or Code side effects without a separate explicit approval.

## Gate 4 Real Queue UAT Plan

Gate 4 is a future operator-run UAT for a tiny approved batch of real intake rows. This PR defines the plan only. It must not be used as evidence that Gate 4 has run, and it must not be packaged with workflow JSON, fixture/result JSONL files, Sheet URLs, Sheet IDs, credential IDs, OAuth details, service account JSON, execution payloads, node raw input/output dumps, row-level data, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, secrets, connection strings, or PII.

### Gate 4 Purpose

Gate 4 proves only that the temporary real queue path can perform AC2 lookup for review routing:

- real queue UAT touching AC2 lookup only,
- read-only and review-only,
- no member create/update/delete path,
- no AutoCount write path,
- no direct SQL write path,
- no final write automation.

Passing Gate 4 means only that read-only lookup UAT passed. Passing Gate 4 still does not authorize member create/update, production automation, scheduler activation, or write activation. Production automation or any write path requires a separate reviewed PR and explicit business approval.

### Gate 4 Runtime Boundary

The n8n runtime for Gate 4 must be outside the AC2 host.

- If Gate 4 uses the already rehearsed local operator PC n8n stack, evidence must label it exactly as `local_operator_pc_non_ac2_n8n_stack` and must not claim hosted/VPS readiness.
- If Gate 4 later uses hosted/VPS n8n, evidence must label it as `hosted_or_vps_non_ac2`, and hosted/VPS readiness must be separately evidenced before hosted/VPS real queue UAT.
- n8n must remain inactive and manually run for UAT evidence. No scheduler is enabled.
- No public inbound webhook, callback, tunnel, or reverse proxy may be exposed on the AC2 host.
- Hosted/cloud/VPS n8n must not use Execute Command for AC2 lookup because that would run on the n8n host/container, not on the Windows AC2 lookup bridge host.

### Gate 4 Queue Surface

Google Sheets UAT-only queue and review tabs are allowed for this temporary UAT only.

- Use only the preapproved UAT tabs described in this plan, with real Sheet URLs, Sheet IDs, credential IDs, and resource locator values kept outside Git and outside pasted PR evidence.
- n8n may read only a tiny approved batch of real UAT rows explicitly marked for lookup.
- n8n may write only sanitized `PENDING_LOOKUP` queue rows using the allowed queue request fields.
- The local Windows bridge may read only approved `PENDING_LOOKUP` queue rows and write only sanitized review result rows using the allowed result fields.
- n8n may map sanitized results back only to review/status fields.
- Google Sheets remains temporary and must be disabled or replaced before production activation.

### Gate 4 Flow

The Gate 4 flow is:

1. Operator confirms Gates 1, 2/2A, and 3 passed and confirms the Gate 4 batch size out of band.
2. n8n is manually run while inactive and reads only the tiny approved batch of real UAT rows marked for lookup.
3. n8n validates headers, PDPA status, prior queue state, and allowed field boundaries.
4. n8n writes sanitized `PENDING_LOOKUP` queue rows only.
5. The local Windows bridge polls/reads only approved queue rows.
6. The bridge calls only the read-only PowerShell lookup path with `-EnableMemberLookupReview` and `-MemberNoBase64Utf8`.
7. The bridge writes sanitized review result rows only.
8. n8n reads sanitized review results and maps them back to review/status fields only.
9. The operator reduces the run to aggregate-only evidence.

No create/update action is available in the flow. `READY_FOR_CREATE_REVIEW` remains a review state only and is not approval to create.

### Gate 4 Stop Conditions

Stop the UAT immediately if any of these appear:

- unexpected field, raw value, encoded value, normalized value, credential, Sheet ID, Sheet URL, credential ID, command transcript, stdout/stderr, PII, unknown state, write-path indication, n8n activation, scheduler, webhook/tunnel exposure, or AutoCount write attempt,
- lookup error spike or unexpected result shape,
- any member create/update/delete path reference,
- any direct SQL write indication,
- any final write automation indication,
- any workflow activation or scheduler enablement,
- any public inbound webhook, callback, tunnel, or reverse proxy exposure on the AC2 host,
- any evidence request that would require row-level data, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, Sheet IDs/URLs, credential IDs, secrets, connection strings, or PII.

If a stop condition occurs, do not continue the batch. Record only `status = needs_fix`, aggregate counts where safe, and the sanitized stop category.

### Gate 4 Evidence Shape

Evidence must be aggregate-only. It must not contain row-level data, raw/encoded/normalized member values, names, emails, phone numbers, Sheet IDs, Sheet URLs, credential IDs, credentials, command transcripts, stdout/stderr, execution payloads, node raw input/output dumps, local target details, secrets, connection strings, or PII.

Suggested safe paste-back shape:

```text
status = <ok/needs_fix>
gate = gate4_real_queue_uat_ac2_lookup_only
runtime_location = <local_operator_pc_non_ac2_n8n_stack OR hosted_or_vps_non_ac2>
execution_mode = manual_inactive_review_only_uat
approved_batch_size = <aggregate-count-only>
queue_rows_read_count = <aggregate-count-only>
queue_rows_written_count = <aggregate-count-only>
lookup_attempt_count = <aggregate-count-only>
lookup_success_count = <aggregate-count-only>
lookup_existing_member_review_count = <aggregate-count-only>
lookup_manual_review_count = <aggregate-count-only>
lookup_error_count = <aggregate-count-only>
review_rows_written_count = <aggregate-count-only>
form_or_source_rows_updated_count = <aggregate-count-only>
member_create_or_update_invoked = false
autocount_write_attempted = false
direct_sql_write_attempted = false
workflow_activation = inactive
scheduler_enabled = false
public_inbound_to_ac2_host = false
final_write_automation = false
sanitized_note = No credentials, connection strings, Sheet IDs/URLs, credential IDs, row-level output, raw/encoded/normalized member values, names, emails, phone numbers, command transcripts, stderr/stdout, execution payloads, node raw input/output dumps, or PII are pasted.
```

All count fields are aggregate-count-only. Do not include result rows, fixture rows, queue rows, source rows, node payloads, screenshots with row-level data, execution payloads, or command output.

### Gate 4 Completion Boundary

Gate 4 passing means read-only lookup UAT passed. It does not mean production readiness, hosted/VPS readiness unless that runtime was separately evidenced, or write authorization. Member create/update/delete, direct SQL writes, AutoCount writes, scheduler activation, production queue activation, and final write automation remain blocked until a separate reviewed PR and explicit business approval.

## Preconditions For UAT

- n8n runs in cloud/VPS/non-AC2 runtime.
- No n8n service is hosted long-term on the AutoCount Windows host.
- The local Windows bridge has only outbound network access for its queue poll.
- The UAT Sheet tabs are protected and visible only to the UAT operators who need them.
- Real spreadsheet IDs, Sheet URLs, credentials, local server/database values, and queue/API endpoints stay outside Git.
- The local bridge runtime secret remains local and is never passed through n8n or queue rows.
- The direct local PowerShell lookup remains read-only and proven with `-EnableMemberLookupReview`, explicit local auth settings, and `-MemberNoBase64Utf8`.

## Assumptions And Blockers

Assumptions:

- Google Sheets remains the temporary UAT intake/review surface.
- A dedicated Google credential can be created for n8n and a separate least-privilege credential can be created for the local bridge.
- UAT rows can be explicitly marked for queueing with a helper/status column.

Blockers before activation:

- Exact live n8n node parameter shapes and credential/resource selectors were not verified in this Codex session; they must be verified later before any build/export/activation.
- Exact UTF-8 base64 implementation in n8n must be verified before a workflow is built.
- The local bridge does not yet implement a real Google Sheets queue poller in this repo.
- Data Table queueing remains unselected until the n8n API/DataTable access path is verified.
- No production activation is allowed.

## Out Of Scope

- AutoCount member writes.
- Final create/update automation.
- Direct database writes or direct database examples.
- Production n8n workflow export.
- Enabled automation.
- Public inbound webhook or tunnel on the AutoCount host.
- Real credentials, Sheet IDs, Sheet URLs, local server/database details, connection values, row-level outputs, or PII.

Real create/update automation remains blocked pending a separate PR, explicit business approval, idempotency, consent/audit handling, write guardrails, activation guardrails, and a fresh n8n-skills-backed workflow build/review with future live-instance verification before activation.
