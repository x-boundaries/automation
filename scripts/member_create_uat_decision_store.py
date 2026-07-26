"""Transactional, append-only reviewer-decision AUTHORITY store and BUILD-CLAIM store for
the single-member creation UAT.

WHY THIS EXISTS
---------------
The JSONL approval ledger was once both the audit record AND the authorization record. That
is unsound, because ``append_ledger`` writes the complete JSON line before ``flush()`` and
``os.fsync()`` return: a real durability failure can leave a complete, perfectly readable
``approved`` line on disk even though the command reported failure. A later process read that
line back and could publish a package from an approval that was never durably granted.

Chaining more marker files (intent, completion, acknowledgement) cannot fix this: every extra
file needs its own acknowledgement, indefinitely. So the decision boundary lives inside a real
transaction:

    A reviewer decision grants authority ONLY when a committed activation row exists for it in
    this store. A readable JSONL line never grants authority.

The JSONL ledger remains an append-only AUDIT record only.

WHY THE BUILD CLAIM EXISTS (Amendment 7)
----------------------------------------
Resolving authority and then closing the connection left a time-of-check/time-of-use window:
a concurrent reviewer could commit an activated hold, an activated rejection, or a pending
hold/rejection AFTER the build read its authority but BEFORE the build reserved or published.
The build then proceeded on a stale approval snapshot.

Checking harder cannot close that window. The check and the irreversible effect must share one
atomic boundary. So a build now re-resolves authority and inserts an exclusive, append-only
BUILD CLAIM inside a single ``BEGIN IMMEDIATE`` transaction - the same serialisation boundary
the decision writers use. A concurrent reviewer either loses the write lock (so the build's
re-resolve observes its decision) or wins it (so the build's re-resolve observes it). There is
no interleaving in which a stale approval can authorise publication.

The committed claim is the PRIMARY and TERMINAL approval-consumption fact. The filesystem
reservation remains a crash and publication backstop, not the authorisation point.

The one gap a transaction cannot close by itself is the commit's own return path: if ``COMMIT``
raises, the transaction may nevertheless have committed. The exception proves only that the
client did not hear the answer. So every commit failure is resolved by closing the connection,
reopening the database, and looking for the exact row - never by inferring from the exception.

PRIVACY
-------
This store holds sanitised metadata only: identifiers, one-way hashes, operator handles,
timestamps and an operator-chosen output basename. It never holds a member number, name,
mobile number, email address, birthday, credential, or absolute private path. It is local
private operational state and is git-ignored by exact name.
"""

import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

IS_WINDOWS = os.name == "nt"

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import member_create_uat_contract as contract  # noqa: E402

# Bumped whenever the tables, columns, indexes, constraints or triggers change. Adding
# transactional build claims materially changes the authority model, so it is a NEW version
# rather than a disguised v1. A store recorded under any other version - including v1 - is
# refused untouched. There is no migration, in place or otherwise.
SCHEMA_VERSION = "member_create_uat_decisions/v2"

# The store lives beside the approval ledger (the approval-state home) under this exact name,
# so no extra operator flag is required and there is one source of truth.
DECISION_STORE_NAME = "member_create_uat_decisions.sqlite3"

DECISION_TYPES = ("approved", "rejected", "hold")

# A bounded wait, then fail closed. A concurrent writer holding the write lock must never turn
# into an unbounded block or an automatic retry loop.
BUSY_TIMEOUT_MS = 5000

CLAIM_ID_RE = re.compile(r"^claim_[0-9a-f]{32}$")
DECISION_ID_RE = re.compile(r"^dec_[0-9a-f]{32}$")

# --------------------------------------------------------------------------- #
# Exact expected column order per table. Any difference - missing, extra or reordered -
# refuses the store rather than guessing at compatibility.
# --------------------------------------------------------------------------- #
SCHEMA_META_COLUMNS = ("key", "value")
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
BUILD_CLAIM_COLUMNS = (
    "claim_sequence",
    "claim_id",
    "decision_sequence",
    "decision_id",
    "decision_record_hash",
    "approval_id",
    "source_record_id",
    "source_fingerprint",
    "operation_id",
    "package_payload_hash",
    "package_file_name",
    "claimed_at",
    "schema_version",
    "record_hash",
)

# The fields covered by each canonical row hash: everything defining the row except the
# database-assigned sequence and the hash itself. A row whose hash does not recompute was
# altered outside this tool and is refused.
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
CLAIM_HASH_FIELDS = (
    "claim_id",
    "decision_sequence",
    "decision_id",
    "decision_record_hash",
    "approval_id",
    "source_record_id",
    "source_fingerprint",
    "operation_id",
    "package_payload_hash",
    "package_file_name",
    "claimed_at",
    "schema_version",
)

# Every sanitised classifier this module can report. Each names the SHAPE of the defect only:
# no row content, no member value, no credential, no absolute path and no malformed value is
# ever derived from the store for reporting.
STORE_INTEGRITY_REASONS = (
    "store_missing",              # no transactional decision store exists at all
    "store_path_unsafe",          # the derived path failed non-following path safety
    "store_not_absent",           # exclusive creation found an object already at the path
    "store_identity_changed",     # the path stopped referencing the file we exclusively made
    "store_unreadable",           # the file exists but could not be opened
    "store_corrupt",              # not a database, or structurally unreadable
    "store_create_failed",        # exclusive creation or canonical schema setup failed
    # ---- Amendment 8: pure pre-open triage, decided WITHOUT any SQLite call --------- #
    "store_header_invalid",       # absent/short/non-SQLite header, or an implausible page size
    "store_journal_mode_unsupported",  # header is WAL format, or a live connection is not DELETE
    "store_sidecar_present",      # a -journal, -wal or -shm object occupies its exact path
    "store_multiple_links",       # the file is reachable under more than one name
    "metadata_row_set_invalid",   # schema_meta holds anything but the exact single row
    "sequence_order_invalid",     # a history sequence is non-integer, non-positive or unordered
    "store_publication_uncertain",     # a new store was linked/moved but durability is unproven
    "store_temp_cleanup_incomplete",   # the operation-owned creation temporary could not be removed
    "integrity_check_failed",     # PRAGMA integrity_check did not report ok
    "foreign_key_check_failed",   # PRAGMA foreign_key_check reported violations
    "schema_version_missing",     # no schema_version row
    "schema_version_mismatch",    # recorded under a different, unsupported version (incl. v1)
    "missing_object",             # a required table, index or trigger is absent
    "unexpected_object",          # an extra application table, view, index or trigger exists
    "schema_object_mismatch",     # an object's canonical definition text differs
    "column_mismatch",            # missing, extra, reordered or redefined columns
    "index_mismatch",             # index uniqueness, columns or order differ
    "foreign_key_mismatch",       # foreign-key columns, target or actions differ
    "record_hash_mismatch",       # a stored row does not recompute to its canonical hash
    "invalid_field_type",         # a row field is not the declared sanitised type/format
    "naive_timestamp",            # a timestamp parsed but carries no UTC offset
    "timestamp_invalid",          # a timestamp is not a supported ISO-8601 string
    "timestamp_order_invalid",    # timestamps violate a required ordering
    "activation_mismatch",        # an activation does not bind its exact decision content
    "claim_binding_mismatch",     # a claim does not bind its exact approved decision
    "orphan_row",                 # an activation or claim references a missing decision
    "store_locked",               # a concurrent writer held the lock past the busy timeout
    "commit_state_uncertain",     # a pending-decision commit outcome could not be resolved
    "activation_state_uncertain",  # an activation commit outcome could not be resolved
    "claim_state_uncertain",      # a build-claim commit outcome could not be resolved
)


class DecisionStoreError(ValueError):
    """The store could not be trusted, or an outcome is unresolved.

    Deliberately NOT an ``ApprovalError`` subclass: the caller handles it in its own explicit
    arm so a store problem can never be reported as an ordinary, retryable approval refusal.

    ``reason`` is one of ``STORE_INTEGRITY_REASONS`` and carries no row content. Nothing in
    this module ever recreates, replaces, repairs, truncates, augments, updates or deletes a
    store in response to one of these failures.
    """

    def __init__(self, message, *, reason, temp_basename=None):
        super().__init__(message)
        self.reason = reason
        # Only ever an operation-owned temporary's BASENAME, never a directory or private
        # path, so an operator can remove exactly one file by hand after a cleanup failure.
        self.temp_basename = temp_basename


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
# Canonical schema
#
# Append-only by construction:
#   * history rows are only ever INSERTed;
#   * BEFORE UPDATE / BEFORE DELETE triggers ABORT on all three authoritative tables, so even
#     a direct sqlite3 session cannot rewrite or erase decision, activation or claim history;
#   * activation is a SEPARATE table, so "recorded" and "authoritative" are distinct committed
#     facts and a pending decision can never be silently promoted;
#   * build_claim is a THIRD table, so "authorised to build exactly once" is its own committed
#     fact, enforced by UNIQUE constraints rather than by Python.
#
# The CHECK constraints make field discipline a database invariant, not a convention. Note the
# load-bearing interaction that lets SQLite - not Python - reject a claim on a non-approved
# decision: `decision` enforces `(decision_type = 'approved') = (approval_id IS NOT NULL)`, so
# any row with a non-null approval_id IS approved; `build_claim.approval_id` is NOT NULL and
# has a foreign key onto `decision.approval_id`; therefore a claim can only ever reference an
# approved decision. The BEFORE INSERT trigger then additionally requires that the claim's
# decision_id, sequence, canonical hash, source id and fingerprint all describe that SAME
# decision, and that the decision is activated.
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
    """
CREATE TABLE build_claim (
    claim_sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id             TEXT NOT NULL UNIQUE,
    decision_sequence    INTEGER NOT NULL,
    decision_id          TEXT NOT NULL UNIQUE
                              REFERENCES decision (decision_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    decision_record_hash TEXT NOT NULL,
    approval_id          TEXT NOT NULL UNIQUE
                              REFERENCES decision (approval_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_record_id     TEXT NOT NULL,
    source_fingerprint   TEXT NOT NULL,
    operation_id         TEXT NOT NULL UNIQUE,
    package_payload_hash TEXT NOT NULL,
    package_file_name    TEXT NOT NULL,
    claimed_at           TEXT NOT NULL,
    schema_version       TEXT NOT NULL,
    record_hash          TEXT NOT NULL,
    CHECK (decision_sequence > 0),
    CHECK (length(claim_id) = 38),
    CHECK (length(operation_id) = 38),
    CHECK (schema_version = 'member_create_uat_decisions/v2')
)
""",
    "CREATE INDEX idx_decision_source_sequence ON decision (source_record_id, sequence)",
    "CREATE INDEX idx_activation_decision_id ON decision_activation (decision_id)",
    "CREATE INDEX idx_build_claim_source_sequence ON build_claim (source_record_id, claim_sequence)",
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
    """
CREATE TRIGGER build_claim_block_update BEFORE UPDATE ON build_claim
BEGIN
    SELECT RAISE(ABORT, 'member_create_uat build claims are append-only');
END
""",
    """
CREATE TRIGGER build_claim_block_delete BEFORE DELETE ON build_claim
BEGIN
    SELECT RAISE(ABORT, 'member_create_uat build claims are append-only');
END
""",
    """
CREATE TRIGGER build_claim_require_exact_activated_approval BEFORE INSERT ON build_claim
BEGIN
    SELECT RAISE(ABORT, 'member_create_uat build claim must bind its exact activated approved decision')
    WHERE NOT EXISTS (
        SELECT 1
        FROM decision d
        JOIN decision_activation a ON a.decision_id = d.decision_id
        WHERE d.decision_id         = NEW.decision_id
          AND d.approval_id         = NEW.approval_id
          AND d.sequence            = NEW.decision_sequence
          AND d.record_hash         = NEW.decision_record_hash
          AND d.source_record_id    = NEW.source_record_id
          AND d.source_fingerprint  = NEW.source_fingerprint
          AND d.decision_type       = 'approved'
    );
END
""",
)

# Object name -> kind, used for the required/permitted application-object set. Autoindexes
# (``sqlite_autoindex_*``, whose ``sql`` is NULL) and SQLite's own ``sqlite_sequence`` table
# are internal and tolerated; nothing else may be present.
_REQUIRED_TABLES = ("schema_meta", "decision", "decision_activation", "build_claim")
_REQUIRED_INDEXES = (
    "idx_decision_source_sequence",
    "idx_activation_decision_id",
    "idx_build_claim_source_sequence",
)
_REQUIRED_TRIGGERS = (
    "decision_block_update",
    "decision_block_delete",
    "decision_activation_block_update",
    "decision_activation_block_delete",
    "build_claim_block_update",
    "build_claim_block_delete",
    "build_claim_require_exact_activated_approval",
)
_TOLERATED_INTERNAL_OBJECTS = ("sqlite_sequence",)


def _normalise_sql(text):
    """Collapse whitespace so canonical DDL comparison is formatting-insensitive but
    semantics-exact. Everything meaningful - declared types, NOT NULL, defaults, primary keys,
    AUTOINCREMENT, UNIQUE, CHECK bodies, foreign-key columns and actions, index columns and
    order, index uniqueness, trigger timing, event, target table and body - survives."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _canonical_definitions():
    """The exact canonical ``sqlite_schema.sql`` text for every application object.

    SQLite stores the ORIGINAL DDL text verbatim, so comparing it against the statements this
    module creates validates the complete schema meaning in one operation.
    """
    definitions = {}
    for statement in _SCHEMA_STATEMENTS:
        normalised = _normalise_sql(statement)
        match = re.match(
            r"^CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER|VIEW) ([A-Za-z_][A-Za-z0-9_]*)",
            normalised,
        )
        if match is None:  # pragma: no cover - the canonical statements are fixed
            raise DecisionStoreError(
                "The canonical schema statement set is malformed.",
                reason="store_create_failed",
            )
        definitions[match.group(2)] = normalised
    return definitions


_CANONICAL_DEFINITIONS = _canonical_definitions()


# --------------------------------------------------------------------------- #
# Central timestamp parsing and validation
# --------------------------------------------------------------------------- #
def parse_aware_timestamp(value):
    """Return an aware ``datetime``, or None when ``value`` is not a supported one.

    Amendment 7: a naive value such as ``2026-07-28T00:00:00`` parses happily through
    ``datetime.fromisoformat`` but has no UTC offset, so comparing it to an aware "now" raises
    an uncontrolled ``TypeError``. Requiring a non-null ``utcoffset()`` is therefore part of
    validity, not a nicety - it is what keeps every comparison inside the controlled path.

    Never raises, and never returns or reports the offending value.
    """
    if not isinstance(value, str):
        return None
    if not contract.SAFE_TIMESTAMP_RE.fullmatch(value):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    return moment


def _timestamp_problem(value):
    """A sanitised classifier for one timestamp, or None when it is valid and aware."""
    if not isinstance(value, str) or not contract.SAFE_TIMESTAMP_RE.fullmatch(value):
        return "timestamp_invalid"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return "timestamp_invalid"
    if moment.utcoffset() is None:
        return "naive_timestamp"
    return None


def utc_now():
    """The aware current UTC instant, so every comparison in this module is aware-to-aware."""
    return datetime.now(timezone.utc)


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

    Including the decision's own record hash means an activation cannot be made to vouch for a
    different decision row, even one carrying the same decision id.
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


def claim_record_hash(record):
    """The canonical hash binding one build claim to its exact approved decision and package."""
    return "sha256:" + contract.sha256_hex(_canonical(record, CLAIM_HASH_FIELDS))


def store_path_for(ledger_path):
    """The decision store path derived from the approval ledger's directory."""
    return Path(ledger_path).parent / DECISION_STORE_NAME


# --------------------------------------------------------------------------- #
# Connection, exclusive creation and validation
# --------------------------------------------------------------------------- #
def _integrity_check(conn):
    """SQLite's integrity verdict. A seam, so a failing verdict is deterministically testable."""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] if row else "missing"


def _foreign_key_check(conn):
    """Rows describing foreign-key violations; empty means clean. Also a testable seam."""
    return conn.execute("PRAGMA foreign_key_check").fetchall()


def _commit(conn):
    """Commit the open transaction.

    Isolated as a seam so a test can force a failure at exactly this stage - including a
    failure raised AFTER the commit really succeeded, which is the case that makes
    reopen-and-look mandatory.
    """
    conn.execute("COMMIT")


def _safe_store_path(path):
    try:
        return contract.assert_safe_local_path(path)
    except contract.ContractError as error:
        raise DecisionStoreError(
            "The decision store path is not a safe local path; refuse fail-closed.",
            reason="store_path_unsafe",
        ) from error


def _file_identity(stat_result):
    return (stat_result.st_dev, stat_result.st_ino)


# --------------------------------------------------------------------------- #
# Amendment 8: pure operating-system triage, performed BEFORE any SQLite call
#
# Amendment 7 opened an existing file with SQLite and only then decided whether it was a
# canonical store. That ordering is unsound, because the very first pragma it applied -
# `journal_mode=DELETE` - is PERSISTENT: against a WAL-mode database it rewrites the header's
# format bytes and removes the `-wal`/`-shm` companions, so a store the tool then refused had
# already been modified. Removing a hot journal or write-ahead log is also exactly the action
# SQLite documents as destroying crash recovery.
#
# The fix is to decide the WAL and sidecar questions from the file itself, using ordinary
# non-following filesystem calls, before SQLite is allowed anywhere near the path. A SQLite
# database header is a fixed 100-byte prefix: offset 18 is the file-format WRITE version and
# offset 19 the READ version, both `1` for rollback journalling and `2` for WAL. Reading those
# bytes with a plain file handle cannot create a `-wal`, cannot create a `-shm`, and cannot
# roll a journal back.
# --------------------------------------------------------------------------- #
SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
SQLITE_HEADER_BYTES = 100
HEADER_WRITE_VERSION_OFFSET = 18
HEADER_READ_VERSION_OFFSET = 19
HEADER_ROLLBACK_FORMAT = 1
# A page size is a power of two from 512 to 32768, or the value 1 meaning 65536.
VALID_PAGE_SIZES = frozenset({1, 512, 1024, 2048, 4096, 8192, 16384, 32768})
STORE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
REQUIRED_JOURNAL_MODE = "delete"

# Read-only inspection and the trusted writer differ ONLY in the access mode. `cache=private`
# keeps each connection's page cache to itself so no shared-cache interaction can leak state
# between an untrusted inspection and a later trusted transaction. `rwc` is never used: no
# path in this module may bring a database into existence except the creation path, which owns
# its own temporary.
READONLY_URI_QUERY = "mode=ro&cache=private"
WRITER_URI_QUERY = "mode=rw&cache=private"


class StoreTriage:
    """The proven pre-open facts about one existing store file.

    ``identity`` is the (device, inode) pair on POSIX and the equivalent volume/file-index
    pair Python reports on Windows. It is re-checked after the header read, after a read-only
    session closes, and again immediately before a writer opens, so an ordinary replacement of
    the file is detected between every stage.
    """

    def __init__(self, path, identity, size):
        self.path = path
        self.identity = identity
        self.size = size


def _sidecar_paths(safe):
    """The three exact companion pathnames. Never a pattern, never a directory listing."""
    return tuple(Path(str(safe) + suffix) for suffix in STORE_SIDECAR_SUFFIXES)


def _assert_no_sidecars(safe):
    """Refuse if ANY object occupies a companion path - file, directory or symlink alike.

    A sidecar means the store is mid-transaction, crash-interrupted, WAL-mode or mispaired.
    All four are controlled-recovery-only: this tool never checkpoints, rolls back, deletes,
    renames, recreates or repairs one, and never opens the database while one exists.
    """
    for sidecar in _sidecar_paths(safe):
        if os.path.lexists(sidecar):
            raise DecisionStoreError(
                "A decision store journal, write-ahead log or shared-memory companion is "
                "present; refuse fail-closed without opening, checkpointing, rolling back, "
                "deleting or repairing it, and require controlled recovery.",
                reason="store_sidecar_present",
            )


def _stat_plain_single_link(safe):
    """Non-following stat requiring a regular file reachable under exactly one name.

    Exactly one hard link matters twice over: SQLite documents a multiply-named database as
    undefined behaviour because each name derives its own journal path, and a second name is
    also how a stale creation temporary would survive publication.
    """
    try:
        info = os.lstat(safe)
    except OSError as error:
        raise DecisionStoreError(
            "The decision store could not be inspected; refuse fail-closed and leave it "
            "untouched.",
            reason="store_unreadable",
        ) from error
    if not stat.S_ISREG(info.st_mode):
        raise DecisionStoreError(
            "The decision store path is not a plain regular file; refuse fail-closed.",
            reason="store_path_unsafe",
        )
    if getattr(info, "st_nlink", 1) != 1:
        raise DecisionStoreError(
            "The decision store file is reachable under more than one name; refuse "
            "fail-closed rather than opening a multiply-named database.",
            reason="store_multiple_links",
        )
    return info


def _read_store_header(safe):
    """Read and validate the 100-byte SQLite header with a plain file handle.

    Returns the header bytes. Raises before SQLite is ever involved, so a WAL-mode or
    malformed file is refused without a single sidecar being created.
    """
    try:
        with open(safe, "rb") as handle:
            raw = handle.read(SQLITE_HEADER_BYTES)
    except OSError as error:
        raise DecisionStoreError(
            "The decision store header could not be read; refuse fail-closed and leave the "
            "file untouched.",
            reason="store_unreadable",
        ) from error
    if len(raw) < SQLITE_HEADER_BYTES or not raw.startswith(SQLITE_HEADER_MAGIC):
        raise DecisionStoreError(
            "The decision store does not begin with a complete SQLite database header; "
            "refuse fail-closed and leave it untouched.",
            reason="store_header_invalid",
        )
    if int.from_bytes(raw[16:18], "big") not in VALID_PAGE_SIZES:
        raise DecisionStoreError(
            "The decision store header declares an implausible page size; refuse "
            "fail-closed and leave it untouched.",
            reason="store_header_invalid",
        )
    if (raw[HEADER_WRITE_VERSION_OFFSET] != HEADER_ROLLBACK_FORMAT
            or raw[HEADER_READ_VERSION_OFFSET] != HEADER_ROLLBACK_FORMAT):
        raise DecisionStoreError(
            "The decision store header is not rollback-journal format; refuse fail-closed "
            "without opening it, and require controlled recovery.",
            reason="store_journal_mode_unsupported",
        )
    return raw


def triage_existing_store(path):
    """The single authoritative pre-open classifier for an EXISTING store.

    Runs before every SQLite connection to an existing store, on every entry point: reviewer
    decisions, build preflight, the build claim, decision-sequence reporting and all three
    COMMIT-recovery lookups. Performs no SQLite call whatsoever.

    Threat-model boundary: the ordered identity checks detect an ordinary replacement of the
    store file by a cooperating or careless process. They do NOT make the sequence atomic
    against a privileged, non-cooperating process able to substitute a path component during
    an open system call; that is out of the supported model and is documented as such.
    """
    safe = _safe_store_path(path)
    if not os.path.lexists(safe):
        raise DecisionStoreError(
            "No transactional reviewer-decision store exists, so no approval authority "
            "exists; a fresh reviewer decision is required.",
            reason="store_missing",
        )
    if contract.is_reparse_point(safe):
        raise DecisionStoreError(
            "The decision store path is a reparse point or symlink; refuse fail-closed.",
            reason="store_path_unsafe",
        )
    before = _stat_plain_single_link(safe)
    identity = _file_identity(before)
    _read_store_header(safe)
    _assert_no_sidecars(safe)
    after = _stat_plain_single_link(safe)
    if _file_identity(after) != identity:
        raise DecisionStoreError(
            "The decision store file changed identity during inspection; refuse fail-closed.",
            reason="store_identity_changed",
        )
    return StoreTriage(safe, identity, after.st_size)


def assert_still_preserved(triage):
    """Re-prove, after a session closes, that we neither replaced nor sidecar-ed the store.

    A read-only session cannot create a sidecar for a rollback-mode database, so this is a
    proof rather than a repair: if it cannot be proven, the outcome is reported as uncertain
    instead of as success.
    """
    if not os.path.lexists(triage.path):
        raise DecisionStoreError(
            "The decision store disappeared during inspection; refuse fail-closed.",
            reason="store_identity_changed",
        )
    if contract.is_reparse_point(triage.path):
        raise DecisionStoreError(
            "The decision store path became a reparse point or symlink; refuse fail-closed.",
            reason="store_path_unsafe",
        )
    info = _stat_plain_single_link(triage.path)
    if _file_identity(info) != triage.identity:
        raise DecisionStoreError(
            "The decision store file changed identity during inspection; refuse fail-closed.",
            reason="store_identity_changed",
        )
    _assert_no_sidecars(triage.path)


# --------------------------------------------------------------------------- #
# Locked connection paths
# --------------------------------------------------------------------------- #
def _store_uri(safe, query):
    """A file: URI for the exact path. ``Path.as_uri`` percent-encodes reserved characters,
    and the basename has already passed the contract's safe-basename check."""
    return safe.as_uri() + "?" + query


def _connect_uri(safe, query):
    try:
        conn = sqlite3.connect(
            _store_uri(safe, query),
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
    except sqlite3.Error as error:
        raise DecisionStoreError(
            "The decision store could not be opened; refuse fail-closed and leave it "
            "untouched.",
            reason="store_unreadable",
        ) from error
    conn.row_factory = sqlite3.Row
    return conn


def _apply_readonly_pragmas(conn):
    """Connection-local settings only. Nothing here touches a persistent database property.

    ``query_only`` is DEFENCE IN DEPTH, not the guarantee: it blocks CREATE/DELETE/DROP/
    INSERT/UPDATE but does NOT block a persistent `journal_mode` assignment, so it cannot be
    the non-mutation boundary. The boundary is `mode=ro` plus the pre-open triage that already
    refused every file for which a read-only open could have created a companion.
    """
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA query_only=ON")


def _apply_writer_pragmas(conn):
    """Connection-local settings only. `journal_mode` is never assigned to an existing store.

    FULL synchronous fsyncs on every commit, so a commit that returns has reached durable
    storage; foreign_keys=ON is required for the claim's referential guarantees to bite. Both
    are per-connection and leave no on-disk trace.
    """
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")


def _require_rollback_journal_mode(conn):
    """VERIFY the journal mode; never assign it.

    `PRAGMA journal_mode` with no argument is a query and changes nothing. The pre-open header
    check already proved rollback format, so this is the second of two independent mechanisms.
    """
    row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = (row[0] if row else "").lower()
    if mode != REQUIRED_JOURNAL_MODE:
        raise DecisionStoreError(
            "The decision store is not in rollback journal mode; refuse fail-closed without "
            "changing it.",
            reason="store_journal_mode_unsupported",
        )


def open_readonly(path):
    """Triage, then open the store strictly read-only. Never creates, never runs DDL."""
    triage = triage_existing_store(path)
    conn = _connect_uri(triage.path, READONLY_URI_QUERY)
    try:
        _apply_readonly_pragmas(conn)
        _require_rollback_journal_mode(conn)
    except sqlite3.Error as error:
        conn.close()
        raise DecisionStoreError(
            "The decision store rejected its read-only session settings; refuse fail-closed.",
            reason="store_corrupt",
        ) from error
    except Exception:
        conn.close()
        raise
    return conn, triage


def _end_read_transaction(conn):
    """Release a read transaction. A read COMMIT writes nothing; failure never masks a result."""
    try:
        conn.execute("COMMIT")
    except sqlite3.Error:
        _rollback_quietly(conn)


def read_validated(path, read=None):
    """The locked read-only inspection path.

    Triage, open `mode=ro`, run the COMPLETE global validator and the bounded read inside ONE
    read transaction so a concurrent commit cannot produce a torn view mid-scan, end the
    transaction, close, and only then prove the store is still exactly the file we inspected.

    Used by build preflight, authority reads, decision-sequence reporting and all three COMMIT
    recovery lookups. On refusal the original sanitised reason propagates unchanged: the
    preservation proof runs on the success path, where "we changed nothing" is the claim being
    made, and never overwrites the reason a refusal already established.
    """
    conn, triage = open_readonly(path)
    try:
        conn.execute("BEGIN DEFERRED")
        try:
            validate_store(conn)
            value = None if read is None else read(conn)
        finally:
            _end_read_transaction(conn)
    finally:
        conn.close()
    assert_still_preserved(triage)
    return value


def inspect_store(path):
    """Prove an existing store is canonical and untouched, reading nothing from it."""
    read_validated(path)


def begin_write(path):
    """The locked trusted-writer path: triage, `mode=rw`, BEGIN IMMEDIATE, global validation.

    Returns an open connection inside a write transaction whose complete store has ALREADY
    been globally validated. The caller performs its operation-specific resolution and its one
    INSERT inside that same transaction and commits once, so there is no check-then-close-then-
    write gap for a concurrent decision to slip through.
    """
    triage = triage_existing_store(path)
    conn = _connect_uri(triage.path, WRITER_URI_QUERY)
    try:
        _apply_writer_pragmas(conn)
        _require_rollback_journal_mode(conn)
    except sqlite3.Error as error:
        conn.close()
        raise DecisionStoreError(
            "The decision store rejected its writer session settings; refuse fail-closed.",
            reason="store_corrupt",
        ) from error
    except Exception:
        conn.close()
        raise
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        conn.close()
        raise DecisionStoreError(
            "A concurrent writer held the decision store past the bounded busy timeout; "
            "refuse fail-closed without retrying.",
            reason="store_locked",
        ) from error
    try:
        validate_store(conn)
    except Exception:
        _rollback_quietly(conn)
        conn.close()
        raise
    return conn, triage


class CreationResult:
    """What a first-use store creation actually achieved, never what it assumed.

    ``durability`` names the primitive genuinely confirmed on this platform. ``temp_cleanup``
    names the fate of the operation-owned temporary. Neither is ever reported optimistically:
    an unproven publication raises instead of returning a success this object could describe.
    """

    def __init__(self, durability, temp_cleanup):
        self.durability = durability
        self.temp_cleanup = temp_cleanup


def _fsync_directory(directory):
    """POSIX: make a directory ENTRY durable. Returns True only when actually performed.

    Windows exposes no portable directory-handle fsync, so this returns False there and the
    Windows publication contract relies on a write-through move instead. Isolated as a seam so
    a test can force either fsync stage to fail deterministically.
    """
    if not hasattr(os, "O_DIRECTORY"):
        return False
    dir_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return True


def _fsync_file(path):
    """Flush one regular file's contents to durable storage.

    The schema transaction already commits under ``synchronous=FULL``, which SQLite documents
    as a real sync. This removes the remaining dependency on SQLite's internal behaviour for a
    file we are about to publish under a different name.
    """
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _windows_no_replace_move(source, destination):
    """Same-volume Windows rename that FAILS rather than replacing an occupied destination.

    ``MOVEFILE_REPLACE_EXISTING`` is deliberately not set, so a competing creator's store can
    never be overwritten. ``MOVEFILE_WRITE_THROUGH`` asks the system not to return until the
    move has actually reached the disk, which is the strongest durability primitive available
    here - Windows offers no directory-handle fsync. A move leaves no second name behind, so
    the multiply-linked-database hazard never arises on this platform.
    """
    import ctypes
    from ctypes import wintypes

    movefile_write_through = 0x00000008
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    if not move_file(str(source), str(destination), movefile_write_through):
        raise ctypes.WinError(ctypes.get_last_error())


_WINDOWS_COLLISION_ERRORS = (80, 183)  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS


def _is_collision(error):
    if isinstance(error, FileExistsError):
        return True
    return getattr(error, "winerror", None) in _WINDOWS_COLLISION_ERRORS


def _assert_published_identity(final, expected_identity):
    """The published path must be a plain single-link regular file with the expected identity."""
    info = _stat_plain_single_link(final)
    if _file_identity(info) != expected_identity:
        raise DecisionStoreError(
            "The published decision store does not reference the store this operation "
            "created; refuse fail-closed and leave every path as it is.",
            reason="store_identity_changed",
        )


def _publish_windows(temp_name, safe, identity):
    """No-replace, write-through move. Success leaves exactly one name and no temporary."""
    try:
        _windows_no_replace_move(temp_name, safe)
    except OSError as error:
        if _is_collision(error):
            _unlink_own_temporary(temp_name, required=False)
            raise DecisionStoreError(
                "A concurrent operation published the decision store first; refuse "
                "fail-closed and leave the existing store untouched.",
                reason="store_not_absent",
            ) from error
        raise DecisionStoreError(
            "The completed decision store could not be published; refuse fail-closed and "
            "leave the final path as it is.",
            reason="store_create_failed",
        ) from error
    # A move re-parents the same file, so identity is preserved and no alias can remain.
    _assert_published_identity(safe, identity)
    if os.path.lexists(temp_name):
        raise DecisionStoreError(
            "The decision store temporary still exists after a successful move; refuse "
            "fail-closed rather than using a multiply-named store.",
            reason="store_temp_cleanup_incomplete",
            temp_basename=os.path.basename(temp_name),
        )
    return CreationResult("windows_move_write_through", "not_required")


def _publish_posix(temp_name, safe, identity):
    """No-replace hard link, then both directory fsyncs, then exactly one surviving name.

    Every SQLite connection is already closed before this runs, so the transient two-name
    window cannot be observed by an open database handle.
    """
    try:
        os.link(temp_name, str(safe))
    except FileExistsError as error:
        _unlink_own_temporary(temp_name, required=False)
        raise DecisionStoreError(
            "A concurrent operation published the decision store first; refuse fail-closed "
            "and leave the existing store untouched.",
            reason="store_not_absent",
        ) from error
    except OSError as error:
        raise DecisionStoreError(
            "The completed decision store could not be atomically published; refuse "
            "fail-closed and leave the path as it is.",
            reason="store_create_failed",
        ) from error

    # From here the final path EXISTS and is complete. Nothing below may delete or alter it.
    try:
        _fsync_directory(safe.parent)
    except OSError as error:
        raise DecisionStoreError(
            "The published decision store's directory entry could not be made durable; the "
            "store exists but its durability is unproven, so refuse fail-closed and require "
            "controlled recovery without deleting anything.",
            reason="store_publication_uncertain",
            temp_basename=os.path.basename(temp_name),
        ) from error

    _unlink_own_temporary(temp_name, required=True)

    try:
        _fsync_directory(safe.parent)
    except OSError as error:
        raise DecisionStoreError(
            "The decision store directory could not be made durable after temporary "
            "removal; the store exists but its durability is unproven, so refuse fail-closed "
            "without rolling back or deleting the published path.",
            reason="store_publication_uncertain",
        ) from error

    # Exactly one surviving name, and it is the file we created.
    _assert_published_identity(safe, identity)
    return CreationResult("posix_link_and_directory_fsync", "unlinked")


def create_store_exclusively(path):
    """Create the canonical v2 store, but only at a positively ABSENT path we exclusively own.

    Schema creation is the one operation in this module that writes DDL and the one place
    ``journal_mode`` is ever assigned, so it is gated on proving the file was ours to create -
    and, just as importantly, on never letting any other process observe a partially
    initialised store:

      1. non-following path-safety check on the final path and a plain-directory parent check;
      2. refuse immediately if anything already occupies the final path;
      3. exclusively create an operation-owned temporary in the SAME directory (``mkstemp``);
      4. confirm it is a regular single-link file and record its identity;
      5. open SQLite on it AS ITS OWNED CREATOR - deliberately NOT through the untrusted
         existing-store classifier, which would refuse a legitimately empty new file;
      6. set rollback journalling and durability, create the canonical schema and the single
         metadata row in ONE explicit transaction, and validate the complete result;
      7. close every connection, prove no temporary sidecar exists, re-check identity and
         link count, and flush the file durably;
      8. publish under the platform contract, then re-inspect through the locked read-only
         path before the store may be used.

    Step 8 is why the temporary exists. Exclusively creating the FINAL path and then running
    DDL on it would leave a real window in which a concurrent process opens a zero-byte file
    and correctly concludes it is not a canonical store. Publishing an already-complete store
    removes that window entirely: the final path only ever appears fully formed.

    No pre-existing database is ever opened for DDL, augmented, migrated, repaired or
    replaced. If a competitor wins the race, their store is left untouched and only this
    operation's own temporary is removed. If setup fails, the final path is never created and
    the operation-owned temporary is deliberately LEFT in place as evidence.
    """
    safe = _safe_store_path(path)
    if contract.is_reparse_point(safe):
        raise DecisionStoreError(
            "The decision store path is a reparse point or symlink; refuse fail-closed.",
            reason="store_path_unsafe",
        )
    if os.path.lexists(safe):
        raise DecisionStoreError(
            "An object already occupies the decision store path, so it was not ours to "
            "create; refuse fail-closed and execute no schema DDL.",
            reason="store_not_absent",
        )
    safe.parent.mkdir(parents=True, exist_ok=True)
    if contract.is_reparse_point(safe.parent) or not os.path.isdir(safe.parent):
        raise DecisionStoreError(
            "The decision store parent directory is not a plain local directory; refuse "
            "fail-closed.",
            reason="store_path_unsafe",
        )
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=str(safe.parent), prefix=".mcuat_decisions_", suffix=".tmp"
        )
    except OSError as error:
        raise DecisionStoreError(
            "The decision store could not be exclusively created; refuse fail-closed.",
            reason="store_create_failed",
        ) from error
    try:
        created = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(created.st_mode):
        raise DecisionStoreError(
            "The exclusively created decision store file is not a regular file; refuse "
            "fail-closed and leave it untouched.",
            reason="store_identity_changed",
        )
    identity = _file_identity(created)

    # The temporary is trusted because THIS operation exclusively created it moments ago. It is
    # opened directly rather than through `triage_existing_store`, which would correctly refuse
    # a zero-byte file that has no SQLite header yet.
    conn = _open_owned_temporary(temp_name)
    try:
        if _file_identity(os.lstat(temp_name)) != identity:
            raise DecisionStoreError(
                "The decision store file was replaced between exclusive creation and open; "
                "refuse fail-closed and leave it untouched.",
                reason="store_identity_changed",
            )
        _create_canonical_schema(conn)
        validate_store(conn)
    except Exception:
        # Setup failed: the final path was never created, and the operation-owned temporary is
        # deliberately left in place rather than deleted, so nothing is silently recreated.
        conn.close()
        raise
    conn.close()

    # Every SQLite connection is now closed. Prove the temporary is one durable single-named
    # file with no companions before it is allowed to become the published store.
    _assert_no_sidecars(Path(temp_name))
    temp_info = _stat_plain_single_link(Path(temp_name))
    if _file_identity(temp_info) != identity:
        raise DecisionStoreError(
            "The decision store temporary changed identity before publication; refuse "
            "fail-closed and leave every path as it is.",
            reason="store_identity_changed",
        )
    try:
        _fsync_file(temp_name)
    except OSError as error:
        raise DecisionStoreError(
            "The completed decision store could not be flushed durably before publication; "
            "refuse fail-closed and leave the final path absent.",
            reason="store_create_failed",
        ) from error
    _assert_no_sidecars(Path(temp_name))

    if IS_WINDOWS:
        result = _publish_windows(temp_name, safe, identity)
    else:
        result = _publish_posix(temp_name, safe, identity)

    # The published store must pass the SAME locked read-only inspection every other caller
    # uses before it may be treated as authorised operational state.
    inspect_store(safe)
    return result


def _unlink_own_temporary(temp_name, *, required):
    """Remove exactly this operation's own temporary. Never sweeps, never touches anything else.

    ``required=True`` is the publication path, where a surviving temporary means the store is
    reachable under two names - which SQLite documents as undefined behaviour - so the failure
    is reported rather than swallowed and the store must not be used until an operator removes
    the named file. ``required=False`` is the lost-race path, where the final store belongs to
    a competitor and our temporary is merely litter.
    """
    try:
        os.unlink(temp_name)
    except OSError as error:
        if required:
            raise DecisionStoreError(
                "The operation-owned decision store temporary could not be removed, so the "
                "published store is still reachable under a second name; refuse fail-closed "
                "and require manual removal of exactly that temporary.",
                reason="store_temp_cleanup_incomplete",
                temp_basename=os.path.basename(temp_name),
            ) from error


def _open_owned_temporary(temp_name):
    """Open the temporary this operation exclusively created, as its owning creator.

    This is the ONLY place ``journal_mode`` is ever assigned. The database is brand new, empty
    and owned by this process, so setting rollback journalling here cannot modify anybody
    else's state - and it is what makes every later connection able to VERIFY, rather than
    impose, the required mode.
    """
    try:
        conn = sqlite3.connect(
            str(temp_name), timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
        )
    except sqlite3.Error as error:
        raise DecisionStoreError(
            "The new decision store temporary could not be opened; refuse fail-closed.",
            reason="store_create_failed",
        ) from error
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except sqlite3.Error as error:
        conn.close()
        raise DecisionStoreError(
            "The new decision store temporary rejected its durability settings; refuse "
            "fail-closed.",
            reason="store_create_failed",
        ) from error
    return conn


def _create_canonical_schema(conn):
    """Create the complete canonical schema in one explicit all-or-nothing transaction."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        _commit(conn)
    except sqlite3.Error as error:
        _rollback_quietly(conn)
        raise DecisionStoreError(
            "The canonical decision store schema could not be created; refuse fail-closed and "
            "leave the path exactly as it is.",
            reason="store_create_failed",
        ) from error


def _rollback_quietly(conn):
    """Abandon an open transaction. A rollback failure never masks the original outcome."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def ensure_store(path):
    """Create the store on first use; return what creation achieved, or None if it existed.

    Only the reviewer decision commands may establish a store. ``build-package`` never calls
    this: a missing store means no transactional approval authority exists.

    A concurrent first-use creator can win between our absence check and our own attempt. That
    is a correctness path for concurrent first use, not a compatibility fallback: we fall
    through to using THEIR store and still execute no DDL against it.
    """
    safe = _safe_store_path(path)
    if not os.path.lexists(safe):
        try:
            return create_store_exclusively(safe)
        except DecisionStoreError as error:
            if error.reason != "store_not_absent":
                raise
    return None


# --------------------------------------------------------------------------- #
# Canonical schema validation
# --------------------------------------------------------------------------- #
def validate_store(conn):
    """Validate the complete canonical v2 schema, integrity and referential state.

    Validates schema MEANING, not merely object names. Two independent mechanisms run, so a
    defect is caught even if one has a blind spot:

      * canonical definition text - SQLite stores the original DDL verbatim, so comparing it
        against the statements this module creates validates declared types, NOT NULL,
        defaults, primary keys, AUTOINCREMENT, UNIQUE, CHECK bodies, foreign-key columns and
        actions, index columns/order/uniqueness and trigger timing/event/target/body at once;
      * pragma-derived semantics - ``table_info``, ``index_list``, ``index_info`` and
        ``foreign_key_list`` are compared field by field.

    Then ``integrity_check``, ``foreign_key_check``, the permitted-object set, the exact
    metadata row set, EVERY decision, EVERY activation and EVERY claim row, and the
    cross-table orphan and binding checks.

    Amendment 8 makes this STORE-GLOBAL. Amendment 7 validated structure plus claims, and left
    decision and activation content to ``resolve_authority``, whose query is filtered to one
    source record - so a malformed or tampered decision under an unrelated source survived
    into an authorised build. A source-local authority query is never a substitute for global
    store trust, so every row of every authoritative table is validated here, in deterministic
    order, before any authority read, any write and any recovery conclusion.

    Structure is validated before content because a structural defect makes content
    interpretation meaningless; within a stage the lowest sequence or key wins, so the reported
    reason is reproducible for a given store.

    Raises ``DecisionStoreError`` with a sanitised reason. Never repairs, migrates, recreates,
    augments or replaces anything.
    """
    try:
        _validate_integrity(conn)                            # 1 integrity, 2 foreign keys
        _validate_object_set(conn)                           # 3 object set, 4 canonical DDL
        _validate_columns(conn)                              # 5 columns ...
        _validate_indexes(conn)                              #   ... indexes and constraints
        _validate_foreign_keys(conn)                         #   ... referential actions
        _validate_metadata(conn)                             # 6 exact metadata cardinality
        by_id = _validate_all_decisions(conn)                # 7 every decision, in sequence
        _validate_all_activations(conn, by_id)               # 8 every activation, in sequence
        _validate_all_claims(conn, by_id)                    # 9 every claim, in sequence
        _validate_referential_state(conn)                    # 10 orphan and binding checks
    except sqlite3.DatabaseError as error:
        raise DecisionStoreError(
            "The decision store could not be read as a database; refuse fail-closed and "
            "leave it untouched for controlled recovery.",
            reason="store_corrupt",
        ) from error


def _validate_integrity(conn):
    if _integrity_check(conn) != "ok":
        raise DecisionStoreError(
            "The decision store failed its integrity check; refuse fail-closed and leave it "
            "untouched for controlled recovery.",
            reason="integrity_check_failed",
        )
    if _foreign_key_check(conn):
        raise DecisionStoreError(
            "The decision store failed its foreign-key check; refuse fail-closed and leave it "
            "untouched for controlled recovery.",
            reason="foreign_key_check_failed",
        )


def _validate_object_set(conn):
    """Exactly the canonical application objects, with exactly the canonical definitions."""
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
    ).fetchall()
    found = {}
    for row in rows:
        name = row["name"]
        if name in _TOLERATED_INTERNAL_OBJECTS:
            continue
        if name.startswith("sqlite_") and row["sql"] is None:
            continue  # an implicit autoindex backing a UNIQUE/PK constraint
        found[name] = row

    expected = set(_CANONICAL_DEFINITIONS)
    for name in sorted(expected - set(found)):
        raise DecisionStoreError(
            "The decision store is missing a required schema object; refuse fail-closed.",
            reason="missing_object",
        )
    for name in sorted(set(found) - expected):
        raise DecisionStoreError(
            "The decision store contains an unexpected schema object; refuse fail-closed and "
            "leave it untouched.",
            reason="unexpected_object",
        )
    for name in sorted(expected):
        if _normalise_sql(found[name]["sql"]) != _CANONICAL_DEFINITIONS[name]:
            raise DecisionStoreError(
                "A decision store schema object does not match its canonical definition; "
                "refuse fail-closed and leave it untouched.",
                reason="schema_object_mismatch",
            )


# (name, declared type, notnull, default, pk position) per table, in exact column order.
_EXPECTED_TABLE_INFO = {
    "schema_meta": (
        ("key", "TEXT", 1, None, 1),
        ("value", "TEXT", 1, None, 0),
    ),
    "decision": (
        ("sequence", "INTEGER", 0, None, 1),
        ("decision_id", "TEXT", 1, None, 0),
        ("decision_type", "TEXT", 1, None, 0),
        ("reviewer_id", "TEXT", 1, None, 0),
        ("recorded_at", "TEXT", 1, None, 0),
        ("approval_id", "TEXT", 0, None, 0),
        ("approved_at", "TEXT", 0, None, 0),
        ("expires_at", "TEXT", 0, None, 0),
        ("source_record_id", "TEXT", 1, None, 0),
        ("source_fingerprint", "TEXT", 1, None, 0),
        ("schema_version", "TEXT", 1, None, 0),
        ("record_hash", "TEXT", 1, None, 0),
    ),
    "decision_activation": (
        ("activation_sequence", "INTEGER", 0, None, 1),
        ("decision_id", "TEXT", 1, None, 0),
        ("activated_at", "TEXT", 1, None, 0),
        ("record_hash", "TEXT", 1, None, 0),
    ),
    "build_claim": (
        ("claim_sequence", "INTEGER", 0, None, 1),
        ("claim_id", "TEXT", 1, None, 0),
        ("decision_sequence", "INTEGER", 1, None, 0),
        ("decision_id", "TEXT", 1, None, 0),
        ("decision_record_hash", "TEXT", 1, None, 0),
        ("approval_id", "TEXT", 1, None, 0),
        ("source_record_id", "TEXT", 1, None, 0),
        ("source_fingerprint", "TEXT", 1, None, 0),
        ("operation_id", "TEXT", 1, None, 0),
        ("package_payload_hash", "TEXT", 1, None, 0),
        ("package_file_name", "TEXT", 1, None, 0),
        ("claimed_at", "TEXT", 1, None, 0),
        ("schema_version", "TEXT", 1, None, 0),
        ("record_hash", "TEXT", 1, None, 0),
    ),
}


def _validate_columns(conn):
    """Exact column order, declared type affinity, nullability, defaults and PK positions."""
    for table, expected in _EXPECTED_TABLE_INFO.items():
        found = tuple(
            (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
            for row in conn.execute(f"PRAGMA table_info({table})")
        )
        if found != expected:
            raise DecisionStoreError(
                "The decision store schema does not match the expected column definitions; "
                "refuse fail-closed.",
                reason="column_mismatch",
            )


# Explicit named indexes: (name, unique, exact column order).
_EXPECTED_NAMED_INDEXES = {
    "decision": (("idx_decision_source_sequence", 0, ("source_record_id", "sequence")),),
    "decision_activation": (("idx_activation_decision_id", 0, ("decision_id",)),),
    "build_claim": (("idx_build_claim_source_sequence", 0, ("source_record_id", "claim_sequence")),),
}

# Columns that MUST be backed by a unique index (declared UNIQUE), per table.
_EXPECTED_UNIQUE_COLUMNS = {
    "decision": (("decision_id",), ("approval_id",)),
    "decision_activation": (("decision_id",),),
    "build_claim": (("claim_id",), ("decision_id",), ("approval_id",), ("operation_id",)),
}


def _index_columns(conn, index_name):
    return tuple(
        row["name"]
        for row in conn.execute(f"PRAGMA index_info({index_name})")
    )


def _validate_indexes(conn):
    """Named-index columns/order/uniqueness, plus the presence of every required UNIQUE."""
    for table, expected in _EXPECTED_NAMED_INDEXES.items():
        listed = {
            row["name"]: row
            for row in conn.execute(f"PRAGMA index_list({table})")
        }
        for name, unique, columns in expected:
            row = listed.get(name)
            if row is None:
                raise DecisionStoreError(
                    "The decision store is missing a required index; refuse fail-closed.",
                    reason="missing_object",
                )
            if row["unique"] != unique or _index_columns(conn, name) != columns:
                raise DecisionStoreError(
                    "A decision store index does not match its required uniqueness or "
                    "columns; refuse fail-closed.",
                    reason="index_mismatch",
                )
    for table, unique_sets in _EXPECTED_UNIQUE_COLUMNS.items():
        present = set()
        for row in conn.execute(f"PRAGMA index_list({table})"):
            if row["unique"]:
                present.add(_index_columns(conn, row["name"]))
        for columns in unique_sets:
            if columns not in present:
                raise DecisionStoreError(
                    "The decision store is missing a required UNIQUE constraint; refuse "
                    "fail-closed.",
                    reason="index_mismatch",
                )


# (child column, parent table, parent column, on_update, on_delete) sets per table.
_EXPECTED_FOREIGN_KEYS = {
    "schema_meta": frozenset(),
    "decision": frozenset(),
    "decision_activation": frozenset({
        ("decision_id", "decision", "decision_id", "NO ACTION", "RESTRICT"),
    }),
    "build_claim": frozenset({
        ("decision_id", "decision", "decision_id", "RESTRICT", "RESTRICT"),
        ("approval_id", "decision", "approval_id", "RESTRICT", "RESTRICT"),
    }),
}


def _validate_foreign_keys(conn):
    """Exact foreign-key source and target columns plus update and delete actions."""
    for table, expected in _EXPECTED_FOREIGN_KEYS.items():
        found = frozenset(
            (row["from"], row["table"], row["to"], row["on_update"], row["on_delete"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        )
        if found != expected:
            raise DecisionStoreError(
                "A decision store foreign key does not match its required target or "
                "referential actions; refuse fail-closed.",
                reason="foreign_key_mismatch",
            )


def _validate_metadata(conn):
    """The EXACT permitted metadata row set - not merely a lookup that finds one match.

    Amendment 7 asked ``WHERE key = 'schema_version'`` and accepted whatever else the table
    held, so injected rows passed unnoticed. The canonical store has exactly one metadata row;
    anything else means the file was written by something other than this tool.

    A duplicate ``schema_version`` key is mechanically impossible because ``key`` is the TEXT
    PRIMARY KEY, and the backing unique index is itself asserted by the column and index
    checks above - so only extra keys, a missing row and a wrong value need enforcing here.
    """
    rows = conn.execute("SELECT key, value FROM schema_meta ORDER BY key").fetchall()
    if not rows:
        raise DecisionStoreError(
            "The decision store records no schema version; refuse fail-closed.",
            reason="schema_version_missing",
        )
    if len(rows) != 1:
        raise DecisionStoreError(
            "The decision store metadata table holds rows this tool never writes; refuse "
            "fail-closed and leave it untouched.",
            reason="metadata_row_set_invalid",
        )
    key, value = rows[0]["key"], rows[0]["value"]
    if not isinstance(key, str) or not isinstance(value, str):
        raise DecisionStoreError(
            "The decision store metadata row is not the sanitised text shape this tool "
            "writes; refuse fail-closed.",
            reason="invalid_field_type",
        )
    if key != "schema_version":
        raise DecisionStoreError(
            "The decision store metadata table holds a key this tool never writes; refuse "
            "fail-closed and leave it untouched.",
            reason="metadata_row_set_invalid",
        )
    if value != SCHEMA_VERSION:
        raise DecisionStoreError(
            "The decision store was recorded under a different, unsupported schema version; "
            "refuse fail-closed rather than migrating, augmenting or recreating it.",
            reason="schema_version_mismatch",
        )


def _is_positive_int(value):
    """SQLite is dynamically typed, so a foreign writer can store anything in any column."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_ordered_sequence(value, previous):
    """History sequences are positive integers that strictly increase in scan order.

    AUTOINCREMENT guarantees monotonicity but not contiguity - a rolled-back insert legally
    leaves a gap - so gaplessness is deliberately NOT required.
    """
    if not _is_positive_int(value) or value <= previous:
        raise DecisionStoreError(
            "A decision store history sequence is not a strictly increasing positive "
            "integer; refuse fail-closed and leave the store untouched.",
            reason="sequence_order_invalid",
        )
    return value


def _validate_all_decisions(conn):
    """Validate EVERY decision row, in sequence order. Returns them keyed by decision id."""
    previous = 0
    by_id = {}
    seen_approvals = set()
    for row in conn.execute("SELECT * FROM decision ORDER BY sequence"):
        previous = _require_ordered_sequence(row["sequence"], previous)
        problem = _validate_decision_row(row)
        if problem is not None:
            raise DecisionStoreError(
                "A stored reviewer decision is not the exact sanitised shape this tool "
                "writes; refuse fail-closed and leave the store untouched.",
                reason=problem,
            )
        if row["decision_id"] in by_id:
            raise DecisionStoreError(
                "The decision store holds a duplicate decision identifier; refuse "
                "fail-closed and leave it untouched.",
                reason="invalid_field_type",
            )
        if row["approval_id"] is not None:
            if row["approval_id"] in seen_approvals:
                raise DecisionStoreError(
                    "The decision store holds a duplicate approval identifier; refuse "
                    "fail-closed and leave it untouched.",
                    reason="invalid_field_type",
                )
            seen_approvals.add(row["approval_id"])
        by_id[row["decision_id"]] = row
    return by_id


def _validate_all_activations(conn, decisions_by_id):
    """Validate EVERY activation row, in activation-sequence order.

    Approved, rejected and hold decisions may ALL be activated - activation is what makes any
    decision authoritative, and an activated hold or rejection is precisely what blocks a
    build. Only an activated APPROVED decision may be claimed, which the claim table's
    referential design and trigger enforce separately.
    """
    previous = 0
    seen = set()
    for row in conn.execute(
        "SELECT * FROM decision_activation ORDER BY activation_sequence"
    ):
        previous = _require_ordered_sequence(row["activation_sequence"], previous)
        if not (_text(row["decision_id"]) and DECISION_ID_RE.fullmatch(row["decision_id"])):
            raise DecisionStoreError(
                "A stored decision activation is not the exact sanitised shape this tool "
                "writes; refuse fail-closed and leave the store untouched.",
                reason="invalid_field_type",
            )
        if not (_text(row["record_hash"])
                and contract.PAYLOAD_HASH_RE.fullmatch(row["record_hash"])):
            raise DecisionStoreError(
                "A stored decision activation hash is not the exact sanitised shape this "
                "tool writes; refuse fail-closed and leave the store untouched.",
                reason="invalid_field_type",
            )
        problem = _timestamp_problem(row["activated_at"])
        if problem is not None:
            raise DecisionStoreError(
                "A decision activation timestamp is not a valid aware timestamp; refuse "
                "fail-closed and leave the store untouched.",
                reason=problem,
            )
        decision = decisions_by_id.get(row["decision_id"])
        if decision is None:
            raise DecisionStoreError(
                "The decision store contains an activation for a decision that does not "
                "exist; refuse fail-closed and leave it untouched.",
                reason="orphan_row",
            )
        if row["decision_id"] in seen:
            raise DecisionStoreError(
                "The decision store activates one decision more than once; refuse "
                "fail-closed and leave it untouched.",
                reason="activation_mismatch",
            )
        seen.add(row["decision_id"])
        activated = parse_aware_timestamp(row["activated_at"])
        # An activation cannot precede the decision it activates, nor its approval.
        if activated < parse_aware_timestamp(decision["recorded_at"]):
            raise DecisionStoreError(
                "A decision activation precedes the decision it activates; refuse "
                "fail-closed and leave the store untouched.",
                reason="timestamp_order_invalid",
            )
        if decision["decision_type"] == "approved" and activated < parse_aware_timestamp(
            decision["approved_at"]
        ):
            raise DecisionStoreError(
                "A decision activation precedes the approval it activates; refuse "
                "fail-closed and leave the store untouched.",
                reason="timestamp_order_invalid",
            )
        expected = activation_record_hash(
            row["decision_id"], row["activated_at"], decision["record_hash"]
        )
        if row["record_hash"] != expected:
            raise DecisionStoreError(
                "A decision activation does not bind the exact decision it claims to "
                "activate; refuse fail-closed and leave the store untouched.",
                reason="activation_mismatch",
            )


def _validate_all_claims(conn, decisions_by_id):
    """Validate EVERY claim row, in claim-sequence order, INCLUDING its chronology.

    Chronology is inherently a join: a claim row carries neither ``approved_at`` nor
    ``activated_at``, and the canonical claim hash deliberately does not cover them, so the
    row validator alone can never prove that a claim followed the approval and activation it
    binds. Both orderings are compared as parsed aware INSTANTS - never as text, because two
    valid timestamps in different UTC offsets order differently as strings than in time.
    """
    previous = 0
    for row in conn.execute(
        "SELECT c.*,"
        "       d.approved_at  AS bound_approved_at,"
        "       a.activated_at AS bound_activated_at "
        "FROM build_claim c "
        "LEFT JOIN decision d            ON d.decision_id = c.decision_id "
        "LEFT JOIN decision_activation a ON a.decision_id = c.decision_id "
        "ORDER BY c.claim_sequence"
    ):
        previous = _require_ordered_sequence(row["claim_sequence"], previous)
        problem = _validate_claim_row(row)
        if problem is not None:
            raise DecisionStoreError(
                "A stored build claim is not the exact sanitised shape this tool writes; "
                "refuse fail-closed and leave the store untouched.",
                reason=problem,
            )
        if (row["decision_id"] not in decisions_by_id
                or row["bound_approved_at"] is None
                or row["bound_activated_at"] is None):
            raise DecisionStoreError(
                "The decision store contains a build claim that is not bound to an "
                "activated approved decision; refuse fail-closed and leave it untouched.",
                reason="orphan_row",
            )
        problem = claim_chronology_problem(
            row["claimed_at"], row["bound_approved_at"], row["bound_activated_at"]
        )
        if problem is not None:
            raise DecisionStoreError(
                "A stored build claim is not ordered after the approval and activation it "
                "binds; refuse fail-closed and leave the store untouched.",
                reason=problem,
            )


def claim_chronology_problem(claimed_at, approved_at, activated_at):
    """None when a claim follows both its approval and its activation, else a sanitised reason.

    Shared by the global validator and by the immediate pre-insert check inside the build
    claim's own ``BEGIN IMMEDIATE``, so stored history and newly minted claims are judged by
    one definition. Every value goes through the single aware parser and the comparison is
    between real instants; the offending value is never returned, logged or printed.
    """
    claimed = parse_aware_timestamp(claimed_at)
    approved = parse_aware_timestamp(approved_at)
    activated = parse_aware_timestamp(activated_at)
    if claimed is None or approved is None or activated is None:
        return "timestamp_invalid"
    if claimed < approved or claimed < activated:
        return "timestamp_order_invalid"
    return None


def _validate_referential_state(conn):
    """No orphan activations or claims, and every claim binds an activated approved decision."""
    orphan_activation = conn.execute(
        "SELECT 1 FROM decision_activation a "
        "LEFT JOIN decision d ON d.decision_id = a.decision_id "
        "WHERE d.decision_id IS NULL LIMIT 1"
    ).fetchone()
    orphan_claim = conn.execute(
        "SELECT 1 FROM build_claim c "
        "LEFT JOIN decision d ON d.decision_id = c.decision_id "
        "WHERE d.decision_id IS NULL LIMIT 1"
    ).fetchone()
    if orphan_activation is not None or orphan_claim is not None:
        raise DecisionStoreError(
            "The decision store contains an orphan activation or build claim; refuse "
            "fail-closed and leave it untouched.",
            reason="orphan_row",
        )
    mismatched_claim = conn.execute(
        "SELECT 1 FROM build_claim c "
        "JOIN decision d ON d.decision_id = c.decision_id "
        "LEFT JOIN decision_activation a ON a.decision_id = c.decision_id "
        "WHERE d.decision_type <> 'approved' "
        "   OR a.decision_id IS NULL "
        "   OR c.approval_id <> d.approval_id "
        "   OR c.decision_sequence <> d.sequence "
        "   OR c.decision_record_hash <> d.record_hash "
        "   OR c.source_record_id <> d.source_record_id "
        "   OR c.source_fingerprint <> d.source_fingerprint "
        "LIMIT 1"
    ).fetchone()
    if mismatched_claim is not None:
        raise DecisionStoreError(
            "A build claim does not bind its exact activated approved decision; refuse "
            "fail-closed and leave the store untouched.",
            reason="claim_binding_mismatch",
        )


# --------------------------------------------------------------------------- #
# Append-only writes
# --------------------------------------------------------------------------- #
def insert_pending_decision(conn, record):
    """Insert exactly one PENDING decision. The caller owns the enclosing transaction.

    A committed row here means the decision attempt is durably recorded. It does NOT make the
    decision authoritative: that requires a separate committed activation row, inserted only
    after the JSONL audit append has returned confirmed success.

    Amendment 8: like the build claim, this deliberately does NOT open or commit a
    transaction. The insert must happen inside the same ``BEGIN IMMEDIATE`` in which
    ``begin_write`` already globally validated the store, so validation and the irreversible
    effect share one atomic boundary.

    Raises ``sqlite3.Error`` on failure. The caller must close the connection, reopen the
    database and resolve the outcome with ``recover_decision_commit`` - never infer it from the
    exception.
    """
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


def insert_activation(conn, decision_id, activated_at, activation_hash):
    """Insert the separate append-only ACTIVATION row that makes a decision authoritative.

    Amendment 8: the caller owns the enclosing ``BEGIN IMMEDIATE``, in which ``begin_write``
    has already globally validated the store, so an activation can never be committed against
    a store that was only trusted before the transaction started.

    Raises ``sqlite3.Error`` on failure; the caller resolves the outcome by reopening the
    database, never from the exception.
    """
    conn.execute(
        "INSERT INTO decision_activation (decision_id, activated_at, record_hash) "
        "VALUES (?, ?, ?)",
        (decision_id, activated_at, activation_hash),
    )


def insert_build_claim(conn, record):
    """Insert one exclusive build claim. The caller owns the enclosing transaction.

    Deliberately does NOT open or commit a transaction: the claim must be inserted inside the
    SAME ``BEGIN IMMEDIATE`` that re-resolved authority, so the check and the irreversible
    effect share one atomic boundary. Raises ``sqlite3.Error``.
    """
    conn.execute(
        "INSERT INTO build_claim ("
        " claim_id, decision_sequence, decision_id, decision_record_hash, approval_id,"
        " source_record_id, source_fingerprint, operation_id, package_payload_hash,"
        " package_file_name, claimed_at, schema_version, record_hash"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["claim_id"],
            record["decision_sequence"],
            record["decision_id"],
            record["decision_record_hash"],
            record["approval_id"],
            record["source_record_id"],
            record["source_fingerprint"],
            record["operation_id"],
            record["package_payload_hash"],
            record["package_file_name"],
            record["claimed_at"],
            record["schema_version"],
            record["record_hash"],
        ),
    )


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


def fetch_claim(conn, claim_id):
    """One build claim by claim id, or None."""
    return conn.execute(
        "SELECT * FROM build_claim WHERE claim_id = ?", (claim_id,)
    ).fetchone()


def fetch_claim_for_approval(conn, approval_id):
    """The single build claim for an approval, or None."""
    return conn.execute(
        "SELECT * FROM build_claim WHERE approval_id = ?", (approval_id,)
    ).fetchone()


def operation_id_claimed(conn, operation_id):
    """True when this operation id already appears on a committed claim."""
    return conn.execute(
        "SELECT 1 FROM build_claim WHERE operation_id = ? LIMIT 1", (operation_id,)
    ).fetchone() is not None


def decision_sequence(conn, decision_id):
    row = fetch_decision(conn, decision_id)
    return None if row is None else row["sequence"]


def _text(value):
    return isinstance(value, str) and bool(value)


def _validate_decision_row(row):
    """Confirm one stored decision row is exactly the sanitised shape this tool writes.

    SQLite is dynamically typed, so a row inserted by anything else can hold values of the
    wrong type. Every field is re-checked against the same formats the rest of the contract
    enforces, every timestamp must be a valid AWARE ISO-8601 value, the required orderings must
    hold, and the canonical record hash must recompute.
    """
    if row["decision_type"] not in DECISION_TYPES:
        return "invalid_field_type"
    if not (_text(row["decision_id"]) and DECISION_ID_RE.fullmatch(row["decision_id"])):
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
    problem = _timestamp_problem(row["recorded_at"])
    if problem is not None:
        return problem
    if row["decision_type"] == "approved":
        if not (_text(row["approval_id"])
                and contract.APPROVAL_ID_RE.fullmatch(row["approval_id"])):
            return "invalid_field_type"
        for field in ("approved_at", "expires_at"):
            problem = _timestamp_problem(row[field])
            if problem is not None:
                return problem
        approved = parse_aware_timestamp(row["approved_at"])
        # An approval cannot predate the decision that carries it. This tool writes the two
        # from one instant, so equality is the normal case and anything earlier is backdating.
        if approved < parse_aware_timestamp(row["recorded_at"]):
            return "timestamp_order_invalid"
        # An expiry that precedes its approval is not a usable lifetime.
        if parse_aware_timestamp(row["expires_at"]) < approved:
            return "timestamp_order_invalid"
    elif (row["approval_id"] is not None or row["approved_at"] is not None
          or row["expires_at"] is not None):
        return "invalid_field_type"
    if not (_text(row["record_hash"]) and contract.PAYLOAD_HASH_RE.fullmatch(row["record_hash"])):
        return "invalid_field_type"
    if decision_record_hash(row) != row["record_hash"]:
        return "record_hash_mismatch"
    return None


def _validate_claim_row(row):
    """Confirm one stored build claim is exactly the sanitised shape this tool writes."""
    if not (_text(row["claim_id"]) and CLAIM_ID_RE.fullmatch(row["claim_id"])):
        return "invalid_field_type"
    if not (_text(row["decision_id"]) and DECISION_ID_RE.fullmatch(row["decision_id"])):
        return "invalid_field_type"
    if not (_text(row["approval_id"]) and contract.APPROVAL_ID_RE.fullmatch(row["approval_id"])):
        return "invalid_field_type"
    if not (_text(row["operation_id"]) and contract.OPERATION_ID_RE.fullmatch(row["operation_id"])):
        return "invalid_field_type"
    if not (_text(row["source_record_id"])
            and contract.SOURCE_RECORD_ID_RE.fullmatch(row["source_record_id"])):
        return "invalid_field_type"
    if not (_text(row["source_fingerprint"])
            and contract.SOURCE_FINGERPRINT_RE.fullmatch(row["source_fingerprint"])):
        return "invalid_field_type"
    for field in ("decision_record_hash", "package_payload_hash", "record_hash"):
        if not (_text(row[field]) and contract.PAYLOAD_HASH_RE.fullmatch(row[field])):
            return "invalid_field_type"
    if not (_text(row["package_file_name"])
            and contract.SAFE_BASENAME_RE.fullmatch(row["package_file_name"])):
        return "invalid_field_type"
    if not isinstance(row["decision_sequence"], int) or isinstance(row["decision_sequence"], bool):
        return "invalid_field_type"
    if row["schema_version"] != SCHEMA_VERSION:
        return "invalid_field_type"
    problem = _timestamp_problem(row["claimed_at"])
    if problem is not None:
        return problem
    if claim_record_hash(row) != row["record_hash"]:
        return "record_hash_mismatch"
    return None


# --------------------------------------------------------------------------- #
# Commit-failure recovery
# --------------------------------------------------------------------------- #
def recover_decision_commit(path, decision_id, expected_record_hash):
    """Resolve a pending-decision commit whose ``COMMIT`` raised, by REOPENING the database.

    Returns one of ``CommitState``. The exception that interrupted the commit is never used to
    infer the outcome: only the presence of the exact row, with the exact expected canonical
    hash, proves the transaction committed.
    """
    return _recover(path, lambda conn: fetch_decision(conn, decision_id), expected_record_hash)


def recover_activation_commit(path, decision_id, expected_activation_hash):
    """Resolve an activation commit whose ``COMMIT`` raised, by REOPENING the database."""
    return _recover(
        path, lambda conn: fetch_activation(conn, decision_id), expected_activation_hash
    )


def recover_claim_commit(path, claim_id, expected_claim_hash):
    """Resolve a build-claim commit whose ``COMMIT`` raised, by REOPENING the database.

    ``COMMITTED`` means the approval is consumed and the authorised attempt may continue.
    ``ABSENT`` means nothing was claimed, so no package temporary or reservation may exist and
    a later explicit retry is permissible. ``UNCERTAIN`` fails closed.
    """
    return _recover(path, lambda conn: fetch_claim(conn, claim_id), expected_claim_hash)


def _recover(path, fetch, expected_hash):
    """Resolve a raised COMMIT through the LOCKED READ-ONLY path, never by inference.

    Amendment 8: recovery reopens through exactly the same pre-open triage, ``mode=ro``
    session and complete global validation every other read uses. A store that acquired a WAL
    header or a sidecar since the write is therefore refused WITHOUT being opened, and the
    outcome is reported uncertain rather than as a false negative that would wrongly permit a
    retry. Only the exact row carrying the exact expected canonical hash proves a commit.
    """
    try:
        row = read_validated(path, fetch)
    except DecisionStoreError as error:
        # An absent store cannot hold a committed row, so this is a definite negative. Any
        # other refusal - unreadable, sidecar-bearing, WAL-headed, replaced or globally
        # invalid - leaves the outcome unknown and must fail closed.
        if error.reason == "store_missing":
            return CommitState.ABSENT, None
        return CommitState.UNCERTAIN, None
    except sqlite3.Error:
        return CommitState.UNCERTAIN, None
    if row is None:
        return CommitState.ABSENT, None
    if row["record_hash"] != expected_hash:
        # A row exists but is not the one this operation was committing; the outcome cannot
        # be claimed either way.
        return CommitState.UNCERTAIN, None
    return CommitState.COMMITTED, row


# --------------------------------------------------------------------------- #
# Authority resolution
# --------------------------------------------------------------------------- #
class Authority:
    """The store's answer about one source record's package-building authority.

    ``state`` is an ``AuthorityState``. ``decision`` is the authoritative activated row when
    ``state`` names an activated decision, otherwise None. ``pending_sequences`` lists the
    committed-but-unactivated decision sequences NEWER than the newest activated decision -
    any of them blocks a build, because a pending decision may have been an attempted hold or
    rejection.
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

    Amendment 7: ``build-package`` calls this INSIDE its claim transaction, so the resolution
    and the claim insert share one atomic boundary and no concurrent decision can slip between
    them.
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
            problem = _timestamp_problem(row["activated_at"])
            if problem is not None:
                raise DecisionStoreError(
                    "A decision activation timestamp is not a valid aware timestamp; refuse "
                    "fail-closed and leave the store untouched.",
                    reason=problem,
                )
            # An activation cannot precede the decision it activates.
            if parse_aware_timestamp(row["activated_at"]) < parse_aware_timestamp(
                row["recorded_at"]
            ):
                raise DecisionStoreError(
                    "A decision activation precedes the decision it activates; refuse "
                    "fail-closed and leave the store untouched.",
                    reason="timestamp_order_invalid",
                )
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
