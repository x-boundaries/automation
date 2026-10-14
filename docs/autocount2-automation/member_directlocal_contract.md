# Member DirectLocal Contract (#155, synthetic core)

Status: source-only synthetic core. No real Shopify read, AutoCount login,
`GetNextMemberNo` or `SaveMember` against any account book is authorised by
this document. The previous gateway/n8n/PostgreSQL path is not used and its
files are unchanged.

## Data path

```text
Attended Claude session (dedicated folder, generated from the settings template)
  -> connector graphql_query, pinned queries only (PreToolUse guard)
  -> PostToolUse hook writes hook-delivered JSON to <state_root>\inbox
python -m xb_member_gateway.directlocal  (one OS-locked owner process)
  -> strict capture checks -> SQLite ledger <state_root>\ledger.sqlite3
  -> reused shopify_admission.capture_member_mg_baseline / evaluate_profile
  -> powershell.exe -File scripts\ac2_directlocal_member_bridge.ps1 (one op per child)
       -> scripts\ac2_member_gateway_autocount_adapter.ps1 (unchanged, shopify_m1)
```

| File | Role |
|---|---|
| `member_gateway/src/xb_member_gateway/directlocal.py` | Capture parsing, phone keys, ledger, baseline/discovery, admission, allocation, fence, recovery, reconcile, CLI |
| `scripts/ac2_directlocal_member_bridge.ps1` | Child bridge: `legacy-precheck`, `next-member-no`, `probe`, `reconcile`, `write-once` |
| `scripts/directlocal_connector_hook.py` | `guard` (PreToolUse default-deny for MCP) and `capture` (PostToolUse inbox writer) |
| `config/directlocal_claude_settings.template.json` | Dedicated-session settings template (deny list + hooks); placeholders only |
| `member_gateway/tests/test_directlocal.py`, `tests/test_directlocal_bridge_ps.py` | Synthetic tests |

## Capture provenance and the fail-closed boundary

- A capture record is the structured JSON Claude Code hands to a PostToolUse
  hook (`provenance = claude_code_posttooluse_hook_delivered_json`). It is not
  the original Shopify wire bytes and is never described as such.
- Every capture must use a pinned query (`sha256` match), `first = 50` in both
  the tool input and variables, matching `after`, a GID-shaped profile `id`,
  exact response keys, and `shop { id myshopifyDomain }` equal to the ledger's
  immutable store identity.
- Only the `synthetic` response shape (`{"data": {...}}`) is qualified. A ledger
  can only be initialised in `synthetic` mode with a `*.synthetic.invalid`
  domain; any other mode or shape fails with `capture_response_shape_unqualified`.
  Gate E1 must qualify the real hook-delivered shape before this changes.
- The hook is not tamper-proof against an agent running as the same Windows
  user; this residual risk is acceptable only for attended operation.

## Cutover and baseline

- `begin-baseline` fixes `C` (whole-second UTC) before any page is requested.
  There is exactly one baseline run per ledger; `cutover_at` is immutable.
- `ingest-run` chains unconsumed page captures from `after = null` and runs the
  reused exhaustive `capture_member_mg_baseline` (all customers, `sortKey: ID`,
  no tag search). A missing, conflicting, repeated, duplicated or unchained page,
  or a capture taken before the run started, never seals.
- Sealed: `member-mg` GIDs with `createdAt < C` become the immutable historical
  baseline; `createdAt >= C` become `DISCOVERED` customers. Discovery runs repeat
  the same full scan with the same `C`; pre-cutover GIDs missing from the
  baseline are surfaced and `evaluate_profile` sends them to review.

## Admission, duplicates and MemberNo

- `process --capture <profile>` consumes a fresh (<= 900 s) unconsumed profile
  capture for a discovered GID and applies `evaluate_profile` unchanged
  (`member-mg`, legacy tags, exact Shopify dates, bounded wait).
  Captures are recorded by hash and `tool_use_id`, so a replay is refused.
  The profile file is deleted after use (best-effort, not a secure erase).
- Writing requires `--enable-autocount-write` **and** an unexpired, unused
  approval from `approve-write --gid <gid> --confirm-suffix <trailing digits>`.
- Duplicate precheck: keys from `duplicate_phone_keys` (`65xxxxxxxx` <->
  `xxxxxxxx`), one unchanged adapter lookup per key plus exact email. Any hit,
  failed lookup or ambiguous result is `MANUAL_REVIEW`. The stored MobilePhone
  stays the unchanged `canonical_phone` value.
- MemberNo: at most 3 `GetNextMemberNo` attempts per customer, ever; each
  attempt is committed before the call. Occupied or ledger-held candidates
  advance; a repeated, invalid or unprobeable candidate goes to review. The bound
  candidate is re-probed and never replaced. `GetNextMemberNo` is treated as
  possibly consuming a number until gate E6 proves otherwise.

## Write fence and recovery

| Step | Durable state |
|---|---|
| Approval consumed, fence inserted | `FENCED` (one per customer, one per MemberNo) |
| Before the child starts | `DISPATCHED` + deadline |
| Child: digest + fence check, exclusive `<fence>.claim`, `SaveMember`, read-back, `<fence>.outcome` | claim/outcome files |
| Parent classifies | `CREATED_VERIFIED`, `CREATED_READBACK_MISMATCH`, `WRITE_OUTCOME_UNCERTAIN`, `NOT_WRITTEN_PROVEN` or `WRITER_TERMINATION_UNCONFIRMED` |

- No transition leads back to `FENCED`/`DISPATCHED` (SQLite triggers), so no
  crash, replay, timeout or retry can dispatch a second write.
- `NOT_WRITTEN_PROVEN` requires the claim file to be absent (the claim always
  precedes `SaveMember`). It is still terminal; there is no automatic retry.
- Every command first runs recovery: an orphaned `FENCED` fence becomes
  `MANUAL_REVIEW`; a `DISPATCHED` fence holds (exit 75) until its deadline plus
  grace has passed and the recorded child is gone, then becomes uncertain (or
  not-written if no claim exists).
- `reconcile --capture <fresh profile>` re-derives the payload, requires the
  salted digest to match the fence, and compares read-only with the same bound
  MemberNo. It never calls `SaveMember`.
- The ledger holds GIDs, MemberNos, counts, codes and salted digests only.
  Events are hash-chained and append-only; a second process cannot open the
  ledger (OS lock) and a stale owner epoch cannot commit.

## State root

`--state-root` must be an existing absolute local path that is not UNC, not
under `%LOCALAPPDATA%` (MSIX redirection) and not inside the repository. It is
chosen and its permissions are qualified at gate E0; nothing is hardcoded.

## Synthetic command sequence

```powershell
$env:PYTHONPATH = "member_gateway/src"
python -m xb_member_gateway.directlocal --state-root <root> init-ledger --mode synthetic --store-gid gid://shopify/Shop/900000001 --shop-domain xb-test.synthetic.invalid
python -m xb_member_gateway.directlocal --state-root <root> begin-baseline
python -m xb_member_gateway.directlocal --state-root <root> ingest-run
python -m xb_member_gateway.directlocal --state-root <root> process --capture <root>\inbox\<id>.json
python -m xb_member_gateway.directlocal --state-root <root> status
```

## Remaining effect gates (each needs separate explicit approval)

| Gate | Effect |
|---|---|
| E0 | Choose and permission-qualify `state_root`; create it |
| E1 | Materialise the dedicated session settings/hooks; one no-PII identity read; qualify the hook-delivered response shape and pin the real store identity |
| E2 | Prove deny/guard enforcement in the desktop Code tab without calling any write tool (denied read-only tool; unpinned harmless query) |
| E3 | Attended baseline capture of customer GIDs, `createdAt`, tags; clock check |
| E4 | Real profile reads (model-mediated PII), retention and consent |
| E5 | AutoCount login and read-only bridge ops on the test book |
| E6 | `GetNextMemberNo` side-effect qualification on the test book |
| E7 | One synthetic `SaveMember` on the test book |
| E8 / E9 | Scheduling; production book |
