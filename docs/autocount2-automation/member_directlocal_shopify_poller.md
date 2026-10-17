# Member DirectLocal Shopify Poller (#155 E8)

Status (2026-10-17): source and offline tests only, built under the Owner
source approval #155:6234660572 and Web reconciliation #155:6234765119. This
document authorises no live effect. Not granted by this source: creating or
configuring a Shopify app, storing a credential, any live Shopify read,
registering or enabling the scheduled task, installing into the protected
runtime, any AutoCount login or write, production (`AED_XBOUNDARIES`, E9) and
POS changes. The Google Sheets fallback is unchanged: this source neither reads
nor writes the sheet.

Contract of the Core it feeds: [member_directlocal_contract.md](member_directlocal_contract.md).

## Data path

```text
Shopify Forms (form 1178308: member-mg tag, then membership.* metafields)
  -> Windows Task Scheduler, every 2 minutes (template, disabled)
  -> python -I -S -B directlocal/xb_shopify_poller.py --config <cfg> run-once --no-autocount-write
       -> xb_shopify_direct.ShopifyDirectClient: outbound HTTPS POST only,
          pinned Core queries, read_customers only
       -> direct-HTTP captures in <state_root>\inbox (own provenance)
  -> unchanged Core (xb_directlocal.py): discovery run sealed, registration
     event, admission, duplicate rules, one-write fence
  -> AutoCount: not reachable from the poller (no-write mode only)
```

No Claude session, n8n, gateway, PostgreSQL, webhook or inbound listener is
involved. Claude is a development tool only.

| File | Role |
|---|---|
| `directlocal/xb_shopify_direct.py` | Read-only Admin GraphQL client, token providers, response validation, throttle/retry, direct capture writer |
| `directlocal/xb_shopify_poller.py` | Config, run-once, no-write processor, backoff state, run log, CLI (`check-config`, `run-once`, `baseline`) |
| `directlocal/xb_directlocal.py` | Core; gains the direct-HTTP provenance and the run origin (below) |
| `config/directlocal_shopify_poller.example.json` | Operational configuration example (names of environment variables only) |
| `config/directlocal_shopify_poller.task.template.xml` | Disabled Task Scheduler template |
| `tests/test_member_directlocal_poller.py` | Offline tests (fake Shopify, sockets and child processes patched to fail) |

## Direct-HTTP capture provenance (Core change)

- Schema `xb.directlocal.capture.direct_http.v1`, provenance
  `shopify_admin_graphql_direct_http_v1`, request ids `xbdh-<32 hex>`. Exact
  keys: `captured_at`, `request_id`, `endpoint {host, api_version}`,
  `request {query, variables}`, `response {data}`.
- The request is checked by the same `check_tool_input` allowlist as the
  attended connector (same pinned query text, `first = 50`, cursor and search
  shape); the endpoint host must be the ledger's store; the response must be
  exactly `{"data": {...}}`. `errors` and `extensions` never reach a capture.
- The attended hook record is unchanged. It can no longer use the `xbdh-` id
  namespace, and a direct record cannot claim the hook provenance.
- `begin_run(..., origin="direct_http_poller_v1")` records the origin in the
  hash-chained `run_started` event (`code = <origin>:<run_id>`), in the same
  transaction as the run. `ingest-run` seals a run only when every page has
  one provenance and that provenance matches the origin (direct pages only in
  a poller run, hook pages only in an attended run):
  `run_not_sealed:mixed_capture_provenance` /
  `run_not_sealed:run_origin_provenance_mismatch`. A sealed poller run's
  `run_sealed` event carries the origin code.
- No schema change: the ledger stays v4, the existing UAT ledger needs no
  migration, and every existing attended command behaves as before. The
  `captures` table keeps the `xbdh-` ids, so provenance stays auditable.

## One run

The poller holds the Core's OS ledger lock for the whole run. A second
invocation (Task Scheduler overlap, a manual start, an attended console) gets
`skipped_instance_running` and changes nothing.

1. `recover` (ledger only); a writer HOLD stops the run (exit 75).
2. Refuse `baseline_not_sealed`, `poller_store_binding_mismatch`,
   `attended_capture_in_inbox` (an unconsumed hook capture: an attended
   session uses this root) and `open_run_not_poller_owned`. An open run that
   this poller started and a crash left open is abandoned; its watermark had
   not advanced.
3. Session check (one query, no customer object): `shop` must be the
   ledger's store and the token's access scopes exactly `read_customers`.
4. Discovery. Recent run (the Core's `UPDATED_AT` search from the last
   sealed discovery start minus 15 minutes, never before the cutover), or an
   exhaustive run (`sortKey: ID`, no search index) once
   `full_scan_interval_hours` (default 24) has passed since the last sealed
   exhaustive run. A recent run above `recent_max_pages` (default 40 pages,
   2,000 customers) is abandoned and escalates to an exhaustive run in the
   same invocation; above `full_scan_max_pages` it fails.
5. Every page is validated, then written durably (exclusive temporary file,
   fsync, no-overwrite link) before the next request. The Core seals the run
   only after the whole chain verifies; any failure abandons it. The
   watermark is the sealed run, so an uncertain read never advances it.
6. Profile reads, only when `profile_reads_enabled` (default `false`):
   `DISCOVERED` customers at once, `WAITING` customers again only after
   `profile_retry_seconds` (default 100, so the next 2-minute run), at most
   `max_profile_reads_per_run`. Each read goes through the Core's `process`
   without a bridge. `ADMITTED` customers are never re-read (they wait for a
   future write stage). `profile_discovered_since` keeps customers discovered
   earlier untouched (for example the nine unprofiled registrations in the
   A7 UAT ledger).
7. Inbox purge, backoff state, one JSON report line.

Delayed Shopify data: Forms writes `membership.start_date` about three
seconds after the tag. A customer read in between waits (`WAITING`,
`registration_event_pending` or `membership_dates_missing`) within the Core's
5-read budget, so at least about 8 minutes at the 2-minute cadence, then goes
to manual review. A changed registration metafield goes to review
(`registration_event_changed`). Index lag beyond the 15-minute overlap is
caught by the daily exhaustive run. Customers updated during a recent
pagination can repeat on a later page; the Core refuses the run
(`run_not_sealed:baseline_duplicate_gid`) and the next run retries.

## Shopify transport and failures

| Condition | Code | Retried in run | Backoff |
|---|---|---|---|
| GraphQL `THROTTLED` (HTTP 200) | `shopify_graphql_throttled` | Yes, wait from `extensions.cost` | - |
| HTTP 429 / 500 / 502 / 503 / 504, transport | `shopify_http_throttled`, `shopify_http_server_error`, `shopify_transport_failed` | Yes, Retry-After (<= 30 s) or 1, 2, 4 s | 60 s doubling, <= 30 min |
| HTTP 401 | `shopify_auth_rejected` | One token refresh (client credentials only) | 60 min |
| HTTP 403, GraphQL `ACCESS_DENIED` | `shopify_access_denied` | No | 60 min |
| Scopes other than exactly `read_customers` | `shopify_token_scope_not_minimal` | No | 60 min |
| Any other GraphQL `errors`, also next to partial `data` | `shopify_graphql_error` | No | 60 s doubling |
| Non-JSON, > 4 MiB, unexpected top-level key, missing `pageInfo`, bad or repeated cursor | `shopify_response_invalid`, `shopify_response_too_large`, `shopify_response_shape_invalid`, `shopify_cursor_invalid` | No | 60 s doubling |

Per run: at most 4 attempts per request, 12 retries and 90 s of waiting in
total, and `max_run_seconds` (default 480 s; the task limit is 10 minutes).
A query whose cost exceeds the bucket fails (`shopify_query_cost_exceeds_bucket`).
The client paces the next request when the previous `currentlyAvailable` is
below its cost. Redirects are refused, environment proxies are ignored, the
only accepted host is `https://<store>.myshopify.com` (no port), TLS uses the
system trust store, and only the Core's pinned queries plus the session
check can be sent (no GraphQL write operation exists in the client).

## Output and retention

- One JSON line per run on stdout and in `<state_root>\shopify-poller-runs.jsonl`
  (rotated at 1 MiB, one previous file kept): status, code, scan kind,
  pages, discovered, profiles read, decisions and customer counts by state,
  request/retry/wait counts, lowest bucket value, elapsed seconds. Never a
  GID, cursor, name, phone, email, token, header or provider message.
- `<state_root>\shopify-poller-state.json`: consecutive failures, last code,
  `not_before`, last success. Pacing only; an unreadable file resets with
  `state_note = poller_state_reset`.
- The ledger keeps only what the Core already keeps. Profile captures are
  deleted after processing; consumed captures are purged at the end of each
  run. Each run adds a few hash-chained events (run started/sealed, purge),
  about 2,000 a day at the 2-minute cadence.

| Exit | Status |
|---|---|
| 0 | `ok`, `skipped_instance_running`, `skipped_backoff` |
| 2 | `failed`, `refused` |
| 75 | `hold` (a write fence needs attended recovery) |

## No-write mode

`autocount_write_mode` accepts only `none`, and `run-once` and `baseline`
refuse without `--no-autocount-write`. `NoWriteProcessor` replaces every Core
route to AutoCount (`process` with a bridge or write flag, `_write`,
`_allocate`, `approve-write`, `activate-policy`, `book-check`,
`pos-sync-status`, `reconcile`) with `autocount_writer_unreachable_no_write_mode`.
The poller modules do not import `subprocess` and contain no bridge
construction. Tests assert all of this and patch `subprocess.Popen` and
sockets to fail for every test.

## Configuration

`config/directlocal_shopify_poller.example.json`. Unknown keys are refused;
the file holds only the **names** of environment variables
(`XB_SHOPIFY_<NAME>`), never a secret value. `check-config` reports whether
each named variable is set, without reading it out.

| Key | Default | Bound |
|---|---|---|
| `auth.method` | - | `client_credentials` (Dev Dashboard app, same organisation as the store; about 24 h tokens, refreshed in memory) or `admin_api_access_token` (existing custom app token; cannot refresh) |
| `api_version` | - | `YYYY-01/04/07/10`; the example `2026-07` must be confirmed against Shopify's supported versions at live setup |
| `profile_reads_enabled` | - | `false` in the example: discovery only (GIDs, timestamps, tags, metafield ids) |
| `full_scan_interval_hours` | 24 | 1-168 |
| `recent_max_pages` / `full_scan_max_pages` | 40 / 200 | 1-100 / 1-400 |
| `max_profile_reads_per_run` | 10 | 0-50 |
| `profile_retry_seconds` | 100 | 60-3600 |
| `max_run_seconds` | 480 | 60-3000 |

## Next approval: bounded live read-only Shopify connection

Owner actions and approvals needed before any live read (none is granted):

1. Create a Shopify app for this store in the Shopify Dev Dashboard (same
   organisation as the store), with the single Admin API scope
   `read_customers`, and install it on `xboundariessg.myshopify.com`. For
   profile reads, the app also needs protected customer data access for
   name, email and phone; without it Shopify returns `ACCESS_DENIED`. If a
   custom app created in the Shopify admin is used instead, its token must
   have only `read_customers`.
2. Decide where the client ID and secret (or token) live on
   `DESKTOP-4I042L6` and how the run receives them as the named environment
   variables. This source never writes a credential; a DPAPI-protected store
   or Credential Manager entry of the account that runs the task is the
   expected mechanism and needs its own review.
3. Use a fresh private state root (`prepare-state-root`, `init-ledger`)
   rather than the A7 UAT ledger, then `baseline --no-autocount-write
   --confirm-new-cutover` (about 90 exhaustive reads; fixes the cutover C).
4. Run `run-once --no-autocount-write` by hand with
   `profile_reads_enabled = false` (identity, scope and discovery only),
   then, with separate consent, with profile reads for a bounded test.
   Confirm the API version and the token endpoint's behaviour on that run.

Registering and enabling the scheduled task is a later approval, as are
unattended AutoCount writes.

## Scheduled task (template only; do not run under this source)

The template is `Enabled = false`, one action, `PT2M` repetition,
`MultipleInstancesPolicy = IgnoreNew`, `ExecutionTimeLimit = PT10M`,
`LeastPrivilege`, placeholders for the account SID, interpreter, runtime root
and config path. The interpreter is the protected all-users Python of the
installed runtime, launched with `-I -S -B`.

When an approval names it, registration would be (registers disabled):

```powershell
Register-ScheduledTask -TaskName 'XB-DirectLocal-Shopify-Poller' -TaskPath '\XB\' -Xml (Get-Content -Raw -LiteralPath <filled template>) -User <account> -Password <typed>
```

### Rollback

1. Stop new runs: `Disable-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-DirectLocal-Shopify-Poller'`
   (a running instance finishes within its 10-minute limit or can be ended
   with `Stop-ScheduledTask`).
2. Remove the task: `Unregister-ScheduledTask -TaskPath '\XB\' -TaskName 'XB-DirectLocal-Shopify-Poller' -Confirm:$false`.
3. Revoke Shopify access: uninstall the app or rotate its secret/token in
   Shopify; remove the stored credential.
4. Keep the state root: the ledger is evidence and is never deleted. An open
   poller run is abandoned by the next poller run or by the Core's attended
   `abandon-run`. Deleting `shopify-poller-state.json` only resets backoff.
5. Attended operation (connector session, Owner console) continues on any
   ledger; never run both against one state root at the same time.

## Future unattended AutoCount writes (separate activation; not in this source)

This PR adds no route from the poller to AutoCount. A later stage needs, each
with its own approval and qualification:

- A non-interactive AutoCount credential mechanism. The current bridge
  receives credentials only from the interactive, elevated Owner console. An
  unattended runner needs a dedicated Windows account, its credential
  protected for that account only (DPAPI or Credential Manager), the A2 code
  custody guard on a protected runtime, and a launcher that passes it only to
  the bridge child.
- An explicit poller write mode wired to the Core's standing policy
  (`activate-policy`, bounded creates and TTL), keeping the one-fence rule.
- E9 for production: `AED_XBOUNDARIES` stays refused by source until the
  member-number counter and collision behaviour and the outlet POS
  synchronisation are qualified.

## Residual risks

- Same-user tampering: any process running as the poller account can write
  the inbox or state files, as with the attended hook. The ledger's
  hash chain and single-owner lock do not stop a same-user attacker.
- Each run requests a new client-credentials token (about 720 a day); the
  token endpoint's limits are not documented in the page read on 2026-10-17
  and must be observed in the live read-only run.
- `UPDATED_AT` pagination under heavy churn can fail repeatedly with
  duplicate GIDs; the daily exhaustive run is the backstop.
- The POS latency is still set by the outlet's 15-minute sync interval.
