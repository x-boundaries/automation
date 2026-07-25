"""Transactional, append-only reviewer-decision AUTHORITY store for the single-member
creation UAT.

WHY THIS EXISTS
---------------
The JSONL approval ledger was previously both the audit record AND the authorization
record. That is unsound, because ``append_ledger`` writes the complete JSON line before
``flush()`` and ``os.fsync()`` return: a real flush/fsync failure can leave a complete,
perfectly readable ``approved`` line on disk even though the command reported failure and
durability was never confirmed. A later process read that line back, accepted it as a
reviewer decision, and could reserve and publish a package from an approval that was never
durably granted.

Chaining more marker files (intent, completion, acknowledgement) cannot fix this: every
extra file needs its own acknowledgement, indefinitely. The fix is to put the decision
boundary inside a real transaction:

    A reviewer decision grants authority ONLY when a committed activation row exists for it
    in this store. A readable JSONL line never grants authority.

The JSONL ledger remains an append-only AUDIT record only.

The one gap a transaction cannot close by itself is the commit's own return path: if
``COMMIT`` raises, the transaction may nevertheless have committed. The exception proves
only that the client did not hear the answer. So every commit failure is resolved by
closing the connection, reopening the database, and looking for the exact row - never by
inferring the outcome from the exception.

PRIVACY
-------
This store holds sanitised decision metadata only: identifiers, one-way hashes, operator
handles and timestamps. It never holds a member number, name, mobile number, email address,
birthday, credential, or absolute private path. It is local private operational state and is
git-ignored by exact name.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import member_create_uat_contract as contract  # noqa: E402

# Bumped whenever the tables, columns, indexes or immutability triggers change. A store
# recorded under any other version is refused, never migrated in place or recreated.
SCHEMA_VERSION = "member_create_uat_decisions/v1"

# The store lives beside the approval ledger (the approval-state home) under this exact
# name, so no extra operator flag is required and there is one source of truth.
DECISION_STORE_NAME = "member_create_uat_decisions.sqlite3"

DECISION_TYPES = ("approved", "rejected", "hold")

# A bounded wait, then fail closed. A concurrent writer holding the write lock must never
# turn into an unbounded block or an automatic retry loop.
BUSY_TIMEOUT_MS = 5000

# Exact expected column order per table. Any difference - missing, extra or reordered -
# refuses the store rather than guessing at compatibility.
DECISION_COLUMNS = (
    "sequence",
    "decision_id",
    "decision_type",
    "reviewer_id",
    "recorded_at",
    "approval_id",
    "approved_at",
    "expires_at",
    "source_record_id",
    "source_fingerprint",
    "schema_version",
    "record_hash",
)
ACTIVATION_COLUMNS = (
    "activation_sequence",
    "decision_id",
    "activated_at",
    "record_hash",
)
SCHEMA_META_COLUMNS = ("key", "value")

REQUIRED_TABLES = ("schema_meta", "decision", "decision_activation")
REQUIRED_TRIGGERS = (
    "decision_block_update",
    "decision_block_delete",
    "decision_activation_block_update",
    "decision_activation_block_delete",
)
REQUIRED_INDEXES = ("idx_decision_source_sequence", "idx_activation_decision_id")

# The fields covered by a decision's canonical record hash: everything that defines the
# decision except the database-assigned sequence and the hash itself. A row whose hash does
# not recompute has been altered outside this tool and is refused.
DECISION_HASH_FIELDS = (
    "decision_id",
    "decision_type",
    "reviewer_id",
    "recorded_at",
    "approval_id",
    "approved_at",
    "expires_at",
    "source_record_id",
    "source_fingerprint",
    "schema_version",
)

# Every sanitised classifier this module can report. Each names the SHAPE of the defect
# only: no row content, no member value, no credential and no absolute path is ever derived
# from the store for reporting.
STORE_INTEGRITY_REASONS = (
    "store_missing",              # no transactional decision store exists at all
    "store_path_unsafe",          # the derived path failed local path safety
    "store_unreadable",           # the file exists but could not be opened
    "store_corrupt",              # not a database, or structurally unreadable
    "integrity_check_failed",     # PRAGMA integrity_check did not report ok
    "schema_version_missing",     # no schema_version row
    "schema_version_mismatch",    # recorded under a different, unsupported version
    "missing_table",              # a required table is absent
    "column_mismatch",            # missing, extra or reordered columns
    "missing_trigger",            # an append-only immutability trigger is absent
    "missing_index",              # a required index is absent
    "record_hash_mismatch",       # a decision row does not recompute to its stored hash
    "invalid_field_type",         # a row field is not the declared sanitised type/format
    "activation_mismatch",        # an activation does not bind its exact decision content
    "store_locked",               # a concurrent writer held the lock past the busy timeout
    "commit_state_uncertain",     # a pending-decision commit outcome could not be resolved
    "activation_state_uncertain",  # an activation commit outcome could not be resolved
)


class DecisionStoreError(ValueError):
    """The transactional decision store could not be trusted, or an outcome is unresolved.

    Deliberately NOT an ``ApprovalError`` subclass: the caller handles it in its own explicit
    arm so a store problem can never be reported as an ordinary, retryable approval refusal.

    ``reason`` is one of ``STORE_INTEGRITY_REASONS`` and carries no row content. Nothing in
    this module ever recreates, replaces, repairs, truncates, updates or deletes a store in
    response to one of these failures.
    """

    def __init__(self, message, *, reason):
        super().__init__(message)
        self.reason = reason


class CommitState:
    """The resolved outcome of one transaction whose ``COMMIT`` raised.

    Determined by reopening the database and looking for the exact row - never inferred from
    the exception, which proves only that the client did not hear the answer.
    """

    COMMITTED = "committed"   # the exact row is present: the transaction did commit
    ABSENT = "absent"         # no row: the transaction did not commit
    UNCERTAIN = "uncertain"   # the database could not be re-read: fail closed


class AuthorityState:
    """How the store answers "may this source record build a package right now?"."""

    NONE = "no_activated_decision"          # no store rows, or nothing activated yet
    PENDING_NEWER = "pending_or_uncertain"  # a committed-but-unactivated decision is newest
    APPROVED = "activated_approved"         # newest activated decision is an approval
    REJECTED = "activated_rejected"         # newest activated decision is a rejection
    HOLD = "activated_hold"                 # newest activated decision is a hold


# --------------------------------------------------------------------------- #
# Schema
#
# Append-only by construction:
#   * history rows are only ever INSERTed;
#   * BEFORE UPDATE / BEFORE DELETE triggers ABORT on both authoritative tables, so even a
#     direct sqlite3 session cannot rewrite or erase decision history;
#   * activation is a SEPARATE table, so "recorded" and "authoritative" are distinct
#     committed facts and a pending decision can never be silently promoted.
#
# The CHECK constraints make the approved/non-approved field discipline a database
# invariant rather than a convention: an approval must carry an approval id, an approved
# timestamp and an expiry, and a rejection or hold must carry none of them.
# --------------------------------------------------------------------------- #
# Held as individual statements rather than one script: ``executescript`` issues an implicit
# COMMIT before it runs, which would silently discard the enclosing explicit transaction and
# leave schema creation non-atomic. SQLite's DDL is itself transactional, so executing these
# inside one BEGIN IMMEDIATE gives genuine all-or-nothing schema creation.
_SCHEMA_STATEMENTS = (
    """
CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
)
""",
    """
CREATE TABLE decision (
    sequence           INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id        TEXT NOT NULL UNIQUE,
    decision_type      TEXT NOT NULL CHECK (decision_type IN ('approved', 'rejected', 'hold')),
    reviewer_id        TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    approval_id        TEXT UNIQUE,
    approved_at        TEXT,
    expires_at         TEXT,
    source_record_id   TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    schema_version     TEXT NOT NULL,
    record_hash        TEXT NOT NULL,
    CHECK ((decision_type = 'approved') = (approval_id IS NOT NULL)),
    CHECK ((decision_type = 'approved') = (approved_at IS NOT NULL)),
    CHECK ((decision_type = 'approved') = (expires_at IS NOT NULL))
)
""",
    """
CREATE TABLE decision_activation (
    activation_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id         TEXT NOT NULL UNIQUE
                             REFERENCES decision (decision_id) ON DELETE RESTRICT,
    activated_at        TEXT NOT NULL,
    record_hash         TEXT NOT NULL
)
""",
    "CREATE INDEX idx_decision_source_sequence ON decision (source_record_id, sequence)",
    "CREATE INDEX idx_activation_decision_id ON decision_activation (decision_id)",
    """
CREATE TRIGGER decision_block_update BEFORE UPDATE ON decision
BEGIN
    SELECT RAISE(ABORT, 'member_create_uat decision history is append-only');
END
""",
    """
CREATE TRIGGER decision_block_delete BEFORE DELETE ON decision
BEGIN
    SELECT RAISE(ABORT, 'member_create_uat decision history is append-only');
END
""",
    """
CREATE TRIGGER decision_activation_block_update BEFORE UPDATE ON decision_activation
BEGIN
    SELECT RAISE(ABORT, 'member_create_uat decision activation is append-only');
END
""",
    """
CREATE TRIGGER decision_activation_block_delete BEFORE DELETE ON decision_activation
BEGIN
    SELECT RAISE(ABORT, 'member_create_uat decision activation is append-only');
END
""",
)


# --------------------------------------------------------------------------- #
# Canonical hashes
# --------------------------------------------------------------------------- #
def _canonical(mapping, fields):
    return json.dumps(
        {field: mapping[field] if field in mapping.keys() else None for field in fields},
        sort_keys=True,
        separators=(",", ":"),
    )


def decision_record_hash(record):
    """The canonical hash of one decision's sanitised identity fields."""
    return "sha256:" + contract.sha256_hex(_canonical(record, DECISION_HASH_FIELDS))


def activation_record_hash(decision_id, activated_at, decision_hash):
    """The canonical hash binding one activation to the EXACT decision content it activates.

    Including the decision's own record hash means an activation cannot be made to vouch for
    a different decision row, even one carrying the same decision id.
    """
    canonical = json.dumps(
        {
            "decision_id": decision_id,
            "activated_at": activated_at,
            "decision_record_hash": decision_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + contract.sha256_hex(canonical)


def store_path_for(ledger_path):
    """The decision store path derived from the approval ledger's directory."""
    return Path(ledger_path).parent / DECISION_STORE_NAME


# --------------------------------------------------------------------------- #
# Connection, creation and validation
# --------------------------------------------------------------------------- #
def _integrity_check(conn):
    """Return SQLite's integrity verdict. Isolated as a seam so a failing verdict can be
    exercised deterministically on every platform."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] if row else "missing"


def _commit(conn):
    """Commit the open transaction.

    Isolated as a seam so a test can force a failure at exactly this stage - including a
    failure raised AFTER the commit really succeeded, which is the case that makes
    reopen-and-look mandatory.
    """
    conn.execute("COMMIT")


def _connect(path, *, create):
    """Open the store with durable local-state settings, or fail closed.

    ``create=False`` refuses a missing file rather than letting sqlite3 helpfully create an
    empty database: an absent store means no transactional approval authority exists, which
    must be reported, never silently manufactured.
    """
    try:
        safe = contract.assert_safe_local_path(path)
    except contract.ContractError as error:
        raise DecisionStoreError(
            "The decision store path is not a safe local path; refuse fail-closed.",
            reason="store_path_unsafe",
        ) from error
    if not create and not os.path.lexists(safe):
        raise DecisionStoreError(
            "No transactional reviewer-decision store exists, so no approval authority "
            "exists; a fresh reviewer decision is required.",
            reason="store_missing",
        )
    if create:
        safe.parent.mkdir(parents=True, exist_ok=True)
    try:
        # isolation_level=None disables the driver's implicit transaction handling so every
        # write below runs inside an explicit BEGIN IMMEDIATE ... COMMIT.
        conn = sqlite3.connect(
            str(safe), timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
        )
    except sqlite3.Error as error:
        raise DecisionStoreError(
            "The decision store could not be opened; refuse fail-closed and leave it "
            "untouched.",
            reason="store_unreadable",
        ) from error
    conn.row_factory = sqlite3.Row
    try:
        # DELETE journalling keeps the store a single file with a rollback journal that is
        # removed on commit, which is the simplest durable mode for small local state and
        # needs no shared-memory file. FULL synchronous fsyncs on every commit, so a commit
        # that returns has reached the platform's durable storage.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except sqlite3.Error as error:
        conn.close()
        raise DecisionStoreError(
            "The decision store rejected its durability settings; refuse fail-closed.",
            reason="store_corrupt",
        ) from error
    return conn


def _table_names(conn, kind):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    )}


def validate_store(conn):
    """Confirm the expected schema, indexes and immutability triggers before any authority
    is read from or written to the store.

    Raises ``DecisionStoreError`` with a sanitised reason. Never repairs, migrates,
    recreates or replaces anything.
    """
    try:
        verdict = _integrity_check(conn)
        if verdict != "ok":
            raise DecisionStoreError(
                "The decision store failed its integrity check; refuse fail-closed and "
                "leave it untouched for controlled recovery.",
                reason="integrity_check_failed",
            )
        tables = _table_names(conn, "table")
        for table in REQUIRED_TABLES:
            if table not in tables:
                raise DecisionStoreError(
                    "The decision store is missing a required table; refuse fail-closed.",
                    reason="missing_table",
                )
        for table, expected in (
            ("schema_meta", SCHEMA_META_COLUMNS),
            ("decision", DECISION_COLUMNS),
            ("decision_activation", ACTIVATION_COLUMNS),
        ):
            found = tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
            if found != expected:
                raise DecisionStoreError(
                    "The decision store schema does not match the expected columns; refuse "
                    "fail-closed.",
                    reason="column_mismatch",
                )
        triggers = _table_names(conn, "trigger")
        for trigger in REQUIRED_TRIGGERS:
            if trigger not in triggers:
                raise DecisionStoreError(
                    "The decision store is missing an append-only immutability trigger, so "
                    "history could be rewritten; refuse fail-closed.",
                    reason="missing_trigger",
                )
        indexes = _table_names(conn, "index")
        for index in REQUIRED_INDEXES:
            if index not in indexes:
                raise DecisionStoreError(
                    "The decision store is missing a required index; refuse fail-closed.",
                    reason="missing_index",
                )
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError as error:
        raise DecisionStoreError(
            "The decision store could not be read as a database; refuse fail-closed and "
            "leave it untouched for controlled recovery.",
            reason="store_corrupt",
        ) from error
    if row is None:
        raise DecisionStoreError(
            "The decision store records no schema version; refuse fail-closed.",
            reason="schema_version_missing",
        )
    if row[0] != SCHEMA_VERSION:
        raise DecisionStoreError(
            "The decision store was recorded under a different, unsupported schema version; "
            "refuse fail-closed rather than migrating or recreating it.",
            reason="schema_version_mismatch",
        )


def _create_schema(conn):
    """Create the full schema in one transaction, or leave the database untouched.

    A concurrent creator can win the race: its tables already exist when this transaction
    runs its DDL. That narrow case rolls back and falls through to validation, which is a
    correctness path for concurrent first use - not a compatibility fallback. Any other
    failure refuses.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        _commit(conn)
        return True
    except sqlite3.OperationalError as error:
        _rollback_quietly(conn)
        if "already exists" in str(error).lower():
            return False
        raise DecisionStoreError(
            "The decision store could not be created; refuse fail-closed.",
            reason="store_unreadable",
        ) from error
    except sqlite3.Error as error:
        _rollback_quietly(conn)
        raise DecisionStoreError(
            "The decision store could not be created; refuse fail-closed.",
            reason="store_unreadable",
        ) from error


def _rollback_quietly(conn):
    """Abandon an open transaction. A rollback failure never masks the original outcome."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def open_store(path, *, create):
    """Return a validated connection, creating the schema first when ``create`` is set.

    ``create=True`` is for the reviewer decision commands, which legitimately establish the
    store on first use. ``create=False`` is for ``build-package``, which must never
    manufacture an empty store: a missing store means no approval authority exists.
    """
    conn = _connect(path, create=create)
    try:
        if create:
            tables = _table_names(conn, "table")
            if "schema_meta" not in tables:
                _create_schema(conn)
        validate_store(conn)
    except Exception:
        conn.close()
        raise
    return conn


# --------------------------------------------------------------------------- #
# Append-only writes
# --------------------------------------------------------------------------- #
def insert_pending_decision(conn, record):
    """Insert exactly one PENDING decision inside an explicit transaction and commit it.

    A committed row here means the decision attempt is durably recorded. It does NOT make
    the decision authoritative: that requires a separate committed activation row, inserted
    only after the JSONL audit append has returned confirmed success.

    Raises ``sqlite3.Error`` on failure. The caller must close the connection, reopen the
    database and resolve the outcome with ``recover_decision_commit`` - never infer it from
    the exception.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO decision ("
            " decision_id, decision_type, reviewer_id, recorded_at, approval_id,"
            " approved_at, expires_at, source_record_id, source_fingerprint,"
            " schema_version, record_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["decision_id"],
                record["decision_type"],
                record["reviewer_id"],
                record["recorded_at"],
                record["approval_id"],
                record["approved_at"],
                record["expires_at"],
                record["source_record_id"],
                record["source_fingerprint"],
                record["schema_version"],
                record["record_hash"],
            ),
        )
    except sqlite3.Error:
        _rollback_quietly(conn)
        raise
    _commit(conn)


def insert_activation(conn, decision_id, activated_at, activation_hash):
    """Insert the separate append-only ACTIVATION row that makes a decision authoritative.

    Raises ``sqlite3.Error`` on failure; the caller resolves the outcome by reopening the
    database, never from the exception.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO decision_activation (decision_id, activated_at, record_hash) "
            "VALUES (?, ?, ?)",
            (decision_id, activated_at, activation_hash),
        )
    except sqlite3.Error:
        _rollback_quietly(conn)
        raise
    _commit(conn)


# --------------------------------------------------------------------------- #
# Reads and row validation
# --------------------------------------------------------------------------- #
def fetch_decision(conn, decision_id):
    """One decision row by id, or None."""
    return conn.execute(
        "SELECT * FROM decision WHERE decision_id = ?", (decision_id,)
    ).fetchone()


def fetch_activation(conn, decision_id):
    """One activation row by decision id, or None."""
    return conn.execute(
        "SELECT * FROM decision_activation WHERE decision_id = ?", (decision_id,)
    ).fetchone()


def decision_sequence(conn, decision_id):
    row = fetch_decision(conn, decision_id)
    return None if row is None else row["sequence"]


def _validate_decision_row(row):
    """Confirm one stored decision row is exactly the sanitised shape this tool writes.

    SQLite is dynamically typed, so a row inserted by anything else can hold values of the
    wrong type. Every field is therefore re-checked against the same formats the rest of the
    contract enforces, and the canonical record hash must recompute.
    """
    def _text(value):
        return isinstance(value, str) and bool(value)

    if row["decision_type"] not in DECISION_TYPES:
        return "invalid_field_type"
    if not (_text(row["decision_id"]) and row["decision_id"].startswith("dec_")):
        return "invalid_field_type"
    if not (_text(row["reviewer_id"]) and contract.REVIEWER_ID_RE.fullmatch(row["reviewer_id"])):
        return "invalid_field_type"
    if not (_text(row["source_record_id"])
            and contract.SOURCE_RECORD_ID_RE.fullmatch(row["source_record_id"])):
        return "invalid_field_type"
    if not (_text(row["source_fingerprint"])
            and contract.SOURCE_FINGERPRINT_RE.fullmatch(row["source_fingerprint"])):
        return "invalid_field_type"
    if row["schema_version"] != SCHEMA_VERSION:
        return "invalid_field_type"
    if not _parses_as_timestamp(row["recorded_at"]):
        return "invalid_field_type"
    if row["decision_type"] == "approved":
        if not (_text(row["approval_id"])
                and contract.APPROVAL_ID_RE.fullmatch(row["approval_id"])):
            return "invalid_field_type"
        if not (_parses_as_timestamp(row["approved_at"])
                and _parses_as_timestamp(row["expires_at"])):
            return "invalid_field_type"
    else:
        if row["approval_id"] is not None or row["approved_at"] is not None \
                or row["expires_at"] is not None:
            return "invalid_field_type"
    if not (_text(row["record_hash"]) and contract.PAYLOAD_HASH_RE.fullmatch(row["record_hash"])):
        return "invalid_field_type"
    if decision_record_hash(row) != row["record_hash"]:
        return "record_hash_mismatch"
    return None


def _parses_as_timestamp(value):
    if not isinstance(value, str) or not contract.SAFE_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Commit-failure recovery
# --------------------------------------------------------------------------- #
def recover_decision_commit(path, decision_id, expected_record_hash):
    """Resolve a pending-decision commit whose ``COMMIT`` raised, by REOPENING the database.

    Returns one of ``CommitState``. The exception that interrupted the commit is never used
    to infer the outcome: only the presence of the exact row, with the exact expected
    canonical hash, proves the transaction committed.
    """
    return _recover(
        path,
        lambda conn: fetch_decision(conn, decision_id),
        expected_record_hash,
    )


def recover_activation_commit(path, decision_id, expected_activation_hash):
    """Resolve an activation commit whose ``COMMIT`` raised, by REOPENING the database."""
    return _recover(
        path,
        lambda conn: fetch_activation(conn, decision_id),
        expected_activation_hash,
    )


def _recover(path, fetch, expected_hash):
    try:
        conn = _connect(path, create=False)
    except DecisionStoreError as error:
        # An absent store cannot hold a committed row, so this is a definite negative. Any
        # other open failure leaves the outcome unknown and must fail closed.
        if error.reason == "store_missing":
            return CommitState.ABSENT, None
        return CommitState.UNCERTAIN, None
    try:
        validate_store(conn)
        row = fetch(conn)
        if row is None:
            return CommitState.ABSENT, None
        if row["record_hash"] != expected_hash:
            # A row exists but is not the one this operation was committing; the outcome
            # cannot be claimed either way.
            return CommitState.UNCERTAIN, None
        return CommitState.COMMITTED, row
    except (DecisionStoreError, sqlite3.Error):
        return CommitState.UNCERTAIN, None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Authority resolution
# --------------------------------------------------------------------------- #
class Authority:
    """The store's answer about one source record's package-building authority.

    ``state`` is an ``AuthorityState``. ``decision`` is the authoritative activated row when
    ``state`` names an activated decision, otherwise None. ``pending_sequences`` lists the
    committed-but-unactivated decision sequences that are NEWER than the newest activated
    decision - any of them blocks a build, because a pending decision may have been an
    attempted hold or rejection.
    """

    def __init__(self, state, decision=None, pending_sequences=(), activated_sequence=None):
        self.state = state
        self.decision = decision
        self.pending_sequences = tuple(pending_sequences)
        self.activated_sequence = activated_sequence


def resolve_authority(conn, source_record_id):
    """Resolve which decision, if any, currently authorises a package for this source record.

    The newest ACTIVATED decision is authoritative. A committed-but-unactivated decision that
    is newer than it blocks the build instead of silently falling back to the older approval:
    the pending decision may have been an attempted hold or rejection, and treating "we could
    not confirm the reviewer's latest instruction" as "use the previous approval" is exactly
    the unsafe direction.
    """
    try:
        rows = conn.execute(
            "SELECT d.*,"
            "       a.activation_sequence AS activation_sequence,"
            "       a.activated_at        AS activated_at,"
            "       a.record_hash         AS activation_hash "
            "FROM decision d "
            "LEFT JOIN decision_activation a ON a.decision_id = d.decision_id "
            "WHERE d.source_record_id = ? "
            "ORDER BY d.sequence",
            (source_record_id,),
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise DecisionStoreError(
            "The decision store could not be queried; refuse fail-closed.",
            reason="store_corrupt",
        ) from error

    if not rows:
        return Authority(AuthorityState.NONE)

    for row in rows:
        problem = _validate_decision_row(row)
        if problem is not None:
            raise DecisionStoreError(
                "A stored reviewer decision is not the exact sanitised shape this tool "
                "writes; refuse fail-closed and leave the store untouched.",
                reason=problem,
            )
        if row["activation_sequence"] is not None:
            expected = activation_record_hash(
                row["decision_id"], row["activated_at"], row["record_hash"]
            )
            if row["activation_hash"] != expected:
                raise DecisionStoreError(
                    "A decision activation does not bind the exact decision it claims to "
                    "activate; refuse fail-closed and leave the store untouched.",
                    reason="activation_mismatch",
                )

    activated = [row for row in rows if row["activation_sequence"] is not None]
    if not activated:
        # Every decision for this record is recorded but unactivated: there is no authority,
        # and the newest attempt is pending, which is the more informative report.
        return Authority(
            AuthorityState.PENDING_NEWER,
            pending_sequences=[row["sequence"] for row in rows],
        )

    newest = activated[-1]
    newer_pending = [
        row["sequence"]
        for row in rows
        if row["activation_sequence"] is None and row["sequence"] > newest["sequence"]
    ]
    if newer_pending:
        return Authority(
            AuthorityState.PENDING_NEWER,
            pending_sequences=newer_pending,
            activated_sequence=newest["sequence"],
        )
    state = {
        "approved": AuthorityState.APPROVED,
        "rejected": AuthorityState.REJECTED,
        "hold": AuthorityState.HOLD,
    }[newest["decision_type"]]
    return Authority(state, decision=newest, activated_sequence=newest["sequence"])
