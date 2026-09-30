# Member gateway database backup and restore (pg_dump)

Procedure for the gateway PostgreSQL database behind
`member_gateway/deploy/compose.example.yaml`. It supports live steps L5
(backup and restore rehearsal), L9 (pre-migration dump) and L10 rollback in
W-G2-149 section 9, and the W-ACT precondition of a backup less than 24 hours
old. Every step below touches a live system and needs its own current-turn
approval naming the host, target and operation. Nothing here is run by the
repository, the tests or CI.

All `REPLACE_WITH_*` values are private deployment values. Do not put them in
Git, chat, tickets or logs.

## What a dump contains

The dump holds customer personal data (canonical form payloads, welcome
recipients), private member identifiers (`member_outcomes`,
`job_resolutions`) and the audit history. Treat every dump file as
confidential:

- Write it only to the approved private backup location on the gateway host,
  on an encrypted volume, readable by the operator account only.
- Never copy it to a developer laptop, a shared drive, e-mail, chat or a
  ticket.
- Keep it for the approved retention period only, then delete it with the
  approved method and record the deletion.

## 1. Take a backup

Use the custom format so the restore can be listed and verified.

```text
docker compose -f REPLACE_WITH_PRIVATE_COMPOSE_FILE exec -T gateway-db \
  pg_dump --format=custom --no-owner --no-privileges \
          --username=REPLACE_WITH_DATABASE_OWNER_ROLE \
          --dbname=REPLACE_WITH_DATABASE_NAME \
          --schema=xb_member_gateway \
  > REPLACE_WITH_PRIVATE_BACKUP_DIRECTORY/xb_member_gateway_<UTC-timestamp>.dump
```

Then record, without printing any row content:

1. The SHA-256 of the dump file.
2. `pg_restore --list` of the file succeeds (structure is readable).
3. The applied migration versions:
   `SELECT version FROM xb_member_gateway.schema_migrations ORDER BY version;`
4. Safe counts per table (jobs by state, `member_outcomes`,
   `welcome_email_outbox` by state, `job_resolutions`, `source_handling_receipts`).
5. The control flags (`production_activation_enabled`, `kill_switch_enabled`).

## 2. Pre-migration dump (before 0006)

Before applying `0006_member_write_v2.sql` (live step L10):

1. Keep the kill switch on and activation off; the worker task stays disabled.
2. Confirm zero jobs outside `CREATED_VERIFIED`, `REJECTED_VALIDATION`,
   `CONFIRMED_NOT_CREATED`, `CREATED_READBACK_MISMATCH`, `MANUAL_REVIEW`,
   `DEAD_LETTER`, and zero `writer_execution_holds` rows that are not
   `CLEARED`. The migration guard refuses otherwise; do not "fix" rows to get
   past it without a separately reviewed decision.
3. Take and verify a backup as in section 1. Keep its checksum with the change
   record.

## 3. Restore rehearsal (into a disposable database)

Never restore over the live database to rehearse.

1. Create an empty disposable database on an internal-only instance with no
   host port.
2. Restore:

   ```text
   pg_restore --no-owner --no-privileges --exit-on-error \
              --username=REPLACE_WITH_DISPOSABLE_ROLE \
              --dbname=REPLACE_WITH_DISPOSABLE_DATABASE \
              REPLACE_WITH_PRIVATE_BACKUP_DIRECTORY/xb_member_gateway_<UTC-timestamp>.dump
   ```

3. Compare the migration versions, safe counts and control flags with the
   values recorded in section 1. Any difference is a failed rehearsal.
4. Point a dark gateway (activation off, kill switch on) at the restored
   database and confirm bootstrap readiness passes and `GET /readyz` shows
   `dispatch_enabled:false`.
5. Drop the disposable database and record the rehearsal result.

## 4. Rollback of a migration or release

There is no down-migration. To roll back 0006 or a v2 release:

1. Engage the kill switch; disable the worker task and the n8n schedules.
2. Stop the gateway (`restart: "no"` keeps it stopped).
3. Restore the pre-change dump into a fresh database, verify it as in
   section 3, and repoint the previous gateway image at it.
4. Anything recorded after the dump (new jobs, outcomes, outbox rows) is not
   in the restored copy. Reconcile it from the preserved live database before
   it is retired; never discard it silently.

## Not covered here

Continuous archiving or point-in-time recovery, off-host replication and
automated schedules are not set up by this repository. Choosing them is a
separate owner decision.
