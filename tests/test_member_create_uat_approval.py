import builtins
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import member_create_uat_approval as approval  # noqa: E402
import member_create_uat_contract as contract  # noqa: E402
import member_create_uat_decision_store as decisions  # noqa: E402
import _create_uat_fixtures as fx  # noqa: E402


def run(argv):
    """Run the CLI, capturing stdout and the exit code."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = approval.main(argv)
    return code, buf.getvalue()


def iso(moment):
    return moment.isoformat(timespec="seconds")


class _Drop:
    """Sentinel meaning "remove this field entirely" in an audit-record fixture override."""

    def __repr__(self):
        return "<drop>"


_DROP = _Drop()


class _DecisionStoreFixtureMixin:
    """Helpers for building synthetic decision-store states.

    Every helper goes through the store module's own real insert API, so the canonical record
    hashes, CHECK constraints and append-only triggers apply exactly as they do in production.
    History is never updated or deleted - it cannot be, the triggers forbid it - so a state
    such as "an expired approval" is built by INSERTING a decision whose recorded expiry is
    already in the past, which is what an aged approval really looks like.
    """

    def _store_path(self):
        return decisions.store_path_for(self.ledger)

    def _raw(self):
        """A RAW sqlite3 connection: no pragmas, so hostile states can be constructed exactly
        the way a foreign writer would create them, and writes that the locked paths forbid can
        still be attempted in a test. ``row_factory`` is a client-side convenience and changes
        nothing about how the database is written."""
        conn = sqlite3.connect(str(self._store_path()))
        conn.row_factory = sqlite3.Row
        return conn

    def _sidecar_paths(self):
        return [Path(str(self._store_path()) + suffix)
                for suffix in decisions.STORE_SIDECAR_SUFFIXES]

    def _store_snapshot(self):
        """Everything a refusal must leave untouched: the main file's bytes and identity, every
        exact sidecar's bytes and type, and the bounded fixture directory's entry list.

        Amendment 8 widens the Amendment 7 snapshot, which compared the main file's bytes only
        and so could not have detected a created `-wal`, a removed `-journal` or a replaced
        file of identical length.
        """
        snapshot = {"dirents": sorted(os.listdir(self.tmp))}
        for path in [self._store_path()] + self._sidecar_paths():
            key = path.name
            if not os.path.lexists(path):
                snapshot[key] = None
                continue
            info = os.lstat(path)
            snapshot[key] = {
                "mode": stat.S_IFMT(info.st_mode),
                "identity": (info.st_dev, info.st_ino),
                "bytes": path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
            }
        return snapshot

    def _create_store_for_fixture(self):
        """Create the store when absent, WITHOUT re-validating an existing one.

        Amendment 9 makes ``ensure_store`` require an existing store to be operationally
        admitted, which is exactly right for production and exactly wrong for a fixture that is
        deliberately building a hostile store step by step: the second step would be refused by
        the state the first step just created. Fixtures therefore create directly and leave
        ``ensure_store``'s own contract to the tests that exist to exercise it.
        """
        if not os.path.lexists(self._store_path()):
            decisions.create_store_exclusively(self._store_path())
        return self._store_path()

    def _read_store(self, read, *, create=False):
        """Read through the LOCKED read-only path, creating the store first when asked.

        Amendment 8 removed the "open and hand back a connection" entry point: every read now
        runs inside one validated read transaction and proves the file untouched afterwards,
        so fixtures exercise exactly the production path.
        """
        if create:
            self._create_store_for_fixture()
        return decisions.read_validated(self._store_path(), read)

    def _write_store(self, write, *, create=False):
        """Run one write through the LOCKED trusted-writer sequence and commit once."""
        if create:
            self._create_store_for_fixture()
        conn, _triage = decisions.begin_write(self._store_path())
        try:
            result = write(conn)
            decisions._commit(conn)
        except Exception:
            decisions._rollback_quietly(conn)
            raise
        finally:
            conn.close()
        return result

    def _stored_decisions(self):
        return self._read_store(
            lambda conn: conn.execute(
                "SELECT d.*, a.activation_sequence AS activation_sequence "
                "FROM decision d LEFT JOIN decision_activation a "
                "ON a.decision_id = d.decision_id ORDER BY d.sequence"
            ).fetchall()
        )

    def _activation_count(self):
        return self._read_store(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM decision_activation"
            ).fetchone()[0]
        )

    def _record(self, decision_type, *, srid, fingerprint, reviewer="digital",
                recorded_at=None, expires_at=None, approval_id=None, decision_id=None):
        """A canonical sanitised decision record ready to insert."""
        now = recorded_at or iso(datetime.now(timezone.utc))
        record = {
            "decision_id": decision_id or ("dec_" + uuid.uuid4().hex),
            "decision_type": decision_type,
            "reviewer_id": reviewer,
            "recorded_at": now,
            "approval_id": None,
            "approved_at": None,
            "expires_at": None,
            "source_record_id": srid,
            "source_fingerprint": fingerprint,
            "schema_version": decisions.SCHEMA_VERSION,
        }
        if decision_type == "approved":
            record["approval_id"] = approval_id or ("appr_" + uuid.uuid4().hex)
            record["approved_at"] = now
            record["expires_at"] = expires_at or iso(
                datetime.now(timezone.utc) + timedelta(hours=1)
            )
        record["record_hash"] = decisions.decision_record_hash(record)
        return record

    def _insert_pending(self, record, *, create=True):
        self._write_store(
            lambda conn: decisions.insert_pending_decision(conn, record), create=create
        )
        return record

    def _activate(self, record, *, activated_at=None, activation_hash=None):
        moment = activated_at or iso(datetime.now(timezone.utc))
        self._write_store(
            lambda conn: decisions.insert_activation(
                conn,
                record["decision_id"],
                moment,
                activation_hash or decisions.activation_record_hash(
                    record["decision_id"], moment, record["record_hash"]
                ),
            )
        )
        return record

    def _insert_activated(self, decision_type, **kw):
        return self._activate(self._insert_pending(self._record(decision_type, **kw)))

    # ---- hostile fixtures, written the way a FOREIGN writer would write them ---------- #
    # Amendment 8's global validator refuses to open a write transaction against a store that
    # already holds a malformed row, so a multi-step hostile fixture cannot be built through
    # the production writer at all - which is precisely the property under test. These helpers
    # therefore write directly, exactly as something other than this tool would.

    def _hostile_pending(self, record, *, create=True):
        if create:
            self._create_store_for_fixture()
        raw = self._raw()
        try:
            decisions.insert_pending_decision(raw, record)
            raw.commit()
        finally:
            raw.close()
        return record

    def _hostile_activate(self, record, *, activated_at=None, activation_hash=None):
        moment = activated_at or iso(datetime.now(timezone.utc))
        raw = self._raw()
        try:
            decisions.insert_activation(
                raw,
                record["decision_id"],
                moment,
                activation_hash or decisions.activation_record_hash(
                    record["decision_id"], moment, record["record_hash"]
                ),
            )
            raw.commit()
        finally:
            raw.close()
        return record

    def _hostile_activated(self, decision_type, **kw):
        return self._hostile_activate(
            self._hostile_pending(self._record(decision_type, **kw))
        )

    def _fixture_identity(self):
        """The (source_record_id, source_fingerprint) the synthetic form fixture resolves to."""
        _payload, _desired, srid, fingerprint = approval.resolve_source_row(
            self.form, self.rows, 2
        )
        return srid, fingerprint


class ApprovalCliTests(_DecisionStoreFixtureMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.form = self.tmp / "form.csv"
        self.rows = self.tmp / "decision_rows.csv"
        self.ledger = self.tmp / "member_create_uat_ledger.jsonl"
        self.package = self.tmp / "member_create_uat_package.json"
        fx.write_form_csv(self.form)
        fx.write_decision_rows(self.rows)

    def _approve(self, **kw):
        return run(
            ["approve", "--reviewer", "digital", "--input", str(self.form),
             "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)]
        )

    def _build(self, extra=None):
        argv = ["build-package", "--input", str(self.form), "--decision-rows", str(self.rows),
                "--row-number", "2", "--ledger", str(self.ledger), "--package-out", str(self.package)]
        return run(argv + (extra or []))

    def test_approve_then_build_produces_valid_package(self):
        code, out = self._approve()
        self.assertEqual(code, 0, out)
        code, out = self._build()
        self.assertEqual(code, 0, out)
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        ok, reasons = contract.validate_package(pkg)
        self.assertTrue(ok, reasons)

    def test_console_output_is_pii_free(self):
        _, approve_out = self._approve()
        _, build_out = self._build()
        combined = approve_out + build_out
        # Raw member number, canonical member number, name, email never printed.
        self.assertNotIn("90000001", combined)
        self.assertNotIn("6590000001", combined)
        self.assertNotIn("Synthetic Alpha", combined)
        self.assertNotIn("synthetic.alpha@example.invalid", combined)

    def test_reject_blocks_build(self):
        code, _ = run(["reject", "--reviewer", "digital", "--input", str(self.form),
                       "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)])
        self.assertEqual(code, 0)
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_not_approved")
        self.assertEqual(summary["decision_authority"], decisions.AuthorityState.REJECTED)
        self.assertIs(summary["fresh_approval_required"], True)

    def test_hold_blocks_build(self):
        run(["hold", "--reviewer", "digital", "--input", str(self.form),
             "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)])
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_not_approved")
        self.assertEqual(summary["decision_authority"], decisions.AuthorityState.HOLD)

    def test_non_ready_decision_state_refused(self):
        fx.write_decision_rows(self.rows, decision="EXISTING_MEMBER_REVIEW")
        code, out = self._approve()
        self.assertEqual(code, 2)
        self.assertIn("READY_FOR_CREATE_REVIEW", out)

    def test_source_change_after_approval_invalidates(self):
        self._approve()
        # Operator edits the form name after approval -> fingerprint drift.
        fx.write_form_csv(self.form, name="Changed Name")
        code, out = self._build()
        self.assertEqual(code, 2)
        self.assertIn("fingerprint mismatch", out)

    def test_expired_approval_refused(self):
        # Expiry is now read from the transactional store, so hand-editing the JSONL audit
        # line cannot change it. An aged approval is modelled by inserting an ACTIVATED
        # approval whose recorded expiry is already in the past - the store is append-only,
        # so history is never rewritten to produce this state.
        srid, fingerprint = self._fixture_identity()
        self._insert_activated(
            "approved", srid=srid, fingerprint=fingerprint,
            recorded_at="2000-01-01T00:00:00+00:00",
            expires_at="2000-01-04T00:00:00+00:00",
        )
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertIn("expired", out)
        self.assertFalse(self.package.exists())

    def test_hand_edited_audit_expiry_cannot_extend_a_stored_approval(self):
        # The reverse direction: the JSONL audit record is not authority, so editing its
        # expiry far into the future cannot resurrect an approval the store records as expired.
        srid, fingerprint = self._fixture_identity()
        self._insert_activated(
            "approved", srid=srid, fingerprint=fingerprint,
            recorded_at="2000-01-01T00:00:00+00:00",
            expires_at="2000-01-04T00:00:00+00:00",
        )
        approval.append_ledger(self.ledger, {
            "event": "decision", "recorded_at": "2000-01-01T00:00:00+00:00",
            "reviewer_id": "digital", "decision": "approved", "source_record_id": srid,
            "source_fingerprint": fingerprint, "row_number_hint": 2,
            "approval_id": "appr_" + ("1" * 32),
            "approved_at": "2000-01-01T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
        })
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertIn("expired", out)
        self.assertFalse(self.package.exists())

    def test_second_build_after_success_is_terminally_refused(self):
        # Amendment 5: a completed build leaves a reservation, and a reservation terminally
        # consumes its approval. The second plain build is refused by that gate.
        self._approve()
        self.assertEqual(self._build()[0], 0)
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "approval_consumed")
        self.assertEqual(summary["reservation"], "terminally_consumed")
        self.assertIs(summary["fresh_approval_required"], True)

    def test_ttl_cannot_exceed_contract_default(self):
        code, out = run(
            ["approve", "--reviewer", "digital", "--input", str(self.form), "--decision-rows", str(self.rows),
             "--row-number", "2", "--ledger", str(self.ledger), "--ttl-hours", str(contract.DEFAULT_APPROVAL_TTL_HOURS + 1)]
        )
        self.assertEqual(code, 2)

    def test_reviewer_id_must_be_non_secret_handle(self):
        # argparse accepts the string; contract pattern is enforced in the built package.
        self._approve()
        self._build()
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        self.assertRegex(pkg["approval"]["reviewer_id"], r"^[a-z0-9_-]{2,32}$")

    def test_validate_package_for_write_passes_now_business_confirmed(self):
        # All four business confirmations are now recorded, so the laptop-side for-write
        # audit reports write-ready (DRY_RUN_VALIDATED). The actual irreversible write
        # still requires the five VM switches and the separate operator step.
        self._approve()
        self._build()
        code, out = run(["validate-package", "--package", str(self.package), "--for-write",
                         "--business-config", str(ROOT / "config" / "member_create_uat_business_confirmation.json")])
        self.assertEqual(code, 0, out)
        self.assertIn("for_write_ok = true", out)
        self.assertIn("DRY_RUN_VALIDATED", out)

    def test_validate_package_for_write_blocked_when_business_unconfirmed(self):
        # A config with any confirmation false still blocks fail-closed.
        self._approve()
        self._build()
        unconfirmed = self.tmp / "unconfirmed_business.json"
        unconfirmed.write_text(json.dumps({
            "schema_version": contract.BUSINESS_CONFIRMATION_SCHEMA_VERSION,
            "confirmations": {
                "MemberType": {"confirmed": True, "reason": "x"},
                "RegisterDate": {"confirmed": True, "reason": "x"},
                "ExpiryDate": {"confirmed": False, "reason": "x"},
                "OpeningPoints": {"confirmed": True, "reason": "x"},
            },
        }), encoding="utf-8")
        code, out = run(["validate-package", "--package", str(self.package), "--for-write",
                         "--business-config", str(unconfirmed)])
        self.assertEqual(code, 2)
        self.assertIn("OPERATOR_CONFIG_REQUIRED", out)

    def test_completed_package_is_single_object(self):
        # Truthful claim: the COMPLETED published file is one JSON object. Atomic
        # visibility is proven separately in AtomicPublicationTests.
        self._approve()
        self._build()
        text = self.package.read_text(encoding="utf-8")
        obj = json.loads(text)
        self.assertIsInstance(obj, dict)
        self.assertEqual(obj["approval"]["decision"], "approved")

    # ---- Finding 3: build output must not claim an AutoCount assignment ---- #
    def test_build_summary_does_not_claim_autocount_assignment(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        # The laptop builder only records package payload state, never an assignment.
        self.assertNotIn("expiry_date_assigned", summary)
        self.assertNotIn("expiry_date_assigned", out)
        self.assertIs(summary["expiry_date_in_payload"], True)

    # ---- Finding 2: package output is strictly no-clobber ---- #
    def _ledger_build_events(self):
        if not self.ledger.exists():
            return []
        entries = [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [e for e in entries if e.get("event") == "build"]

    def test_build_refuses_when_output_file_exists_and_preserves_it(self):
        self._approve()
        self.package.write_text("SENTINEL-DO-NOT-OVERWRITE\n", encoding="utf-8")
        before = self.package.read_bytes()
        builds_before = len(self._ledger_build_events())
        code, out = self._build()
        self.assertEqual(code, 2, out)
        # The pre-existing file is byte-for-byte unchanged.
        self.assertEqual(self.package.read_bytes(), before)
        # No build ledger event was appended after the output-path collision.
        self.assertEqual(len(self._ledger_build_events()), builds_before)

    def test_build_refuses_when_output_is_directory(self):
        self._approve()
        self.package.mkdir()
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertTrue(self.package.is_dir())
        self.assertEqual(self._ledger_build_events(), [])

    def test_build_refuses_when_output_is_symlink(self):
        self._approve()
        target = self.tmp / "sometarget.txt"
        target.write_text("x", encoding="utf-8")
        try:
            self.package.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symlink on this platform/privilege")
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertEqual(self._ledger_build_events(), [])

    def test_rebuild_is_retired_and_cannot_overwrite_existing_package(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)
        before = self.package.read_bytes()
        builds_before = len(self._ledger_build_events())
        # --rebuild to the SAME (existing) path is refused, and refused as a retired flag.
        code, out = self._build(extra=["--rebuild"])
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        self.assertEqual(json.loads(out)["status"], "rebuild_requires_fresh_approval")
        self.assertEqual(self.package.read_bytes(), before)
        self.assertEqual(len(self._ledger_build_events()), builds_before)

    def test_rebuild_to_new_absent_path_is_now_refused(self):
        # DELIBERATE SAFETY-CONTRACT CHANGE (Amendment 5). Same-approval --rebuild used to
        # succeed at a fresh, absent output path. It is retired: a reservation terminally
        # consumes its approval, so a distinct output pathname no longer authorises a second
        # package. A further package needs a fresh reviewer decision and a new approval id.
        self._approve()
        self.assertEqual(self._build()[0], 0)
        first = self.package.read_bytes()
        alt = self.tmp / "member_create_uat_package_v2b.json"
        # A trailing --package-out overrides the helper's default (argparse last-wins).
        code, out = self._build(extra=["--rebuild", "--package-out", str(alt)])
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "rebuild_requires_fresh_approval")
        self.assertIs(summary["rebuild_supported"], False)
        self.assertFalse(alt.exists(), "no second package may be minted at a fresh path")
        # The earlier package and its single build event are untouched.
        self.assertEqual(self.package.read_bytes(), first)
        self.assertEqual(len(self._ledger_build_events()), 1)

    # ---- Additional verification: a v1 decision cannot mint a v2 package ---- #
    def test_v1_decision_entry_cannot_build_v2_package(self):
        # A decision recorded under the previous schema version binds a v1-derived
        # source_record_id. Building a v2 package recomputes source_record_id with the
        # current (v2) SCHEMA_VERSION, so the v1 decision never matches and the build is
        # refused. This proves the schema bump mechanically forces a fresh decision.
        canonical_member_no = "6590000001"  # canonical form of the fixture's 90000001
        v2_srid = contract.source_record_id(canonical_member_no)
        v1_srid = "srcrec_" + contract.sha256_hex(
            f"member_create_uat_package/v1|{canonical_member_no}"
        )
        self.assertNotEqual(v1_srid, v2_srid)
        entry = {
            "event": "decision", "recorded_at": "2026-07-24T00:00:00+00:00",
            "reviewer_id": "digital", "decision": "approved",
            "source_record_id": v1_srid, "source_fingerprint": "fp_" + ("0" * 64),
            "row_number_hint": 2, "approval_id": "appr_" + ("0" * 32),
            "approved_at": "2026-07-24T00:00:00+00:00", "expires_at": "2026-07-30T00:00:00+00:00",
        }
        self.ledger.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertEqual(json.loads(out)["status"], "decision_store_missing")

    def test_legacy_jsonl_only_approval_is_not_authority(self):
        # DELIBERATE COMPATIBILITY DECISION (Amendment 6). A perfectly well-formed, current
        # JSONL approval line for the CORRECT source record - exactly what a pre-Amendment-6
        # tool wrote - grants nothing, because no transactional store and no activation row
        # exist. After merge a fresh reviewer decision is required before a v2 package.
        srid, fingerprint = self._fixture_identity()
        approval.append_ledger(self.ledger, {
            "event": "decision", "recorded_at": iso(datetime.now(timezone.utc)),
            "reviewer_id": "digital", "decision": "approved", "source_record_id": srid,
            "source_fingerprint": fingerprint, "row_number_hint": 2,
            "approval_id": "appr_" + ("2" * 32),
            "approved_at": iso(datetime.now(timezone.utc)),
            "expires_at": iso(datetime.now(timezone.utc) + timedelta(hours=48)),
        })
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_missing")
        self.assertIs(summary["legacy_ledger_approval_accepted"], False)
        self.assertIs(summary["fresh_approval_required"], True)
        # Nothing was created: build-package never manufactures an empty decision store.
        self.assertFalse(self._store_path().exists())
        self.assertFalse(self.package.exists())


class AtomicPublicationTests(unittest.TestCase):
    """Amendment 2/3: the builder publishes the package atomically (a reader never sees a
    partial file at the final path, a crash cannot leave a partial final package), remains
    strictly no-clobber, AND is truthful about temporary-file cleanup - a failed temp
    unlink is never swallowed. Pre-publication and post-publication cleanup failures are
    handled separately."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.form = self.tmp / "form.csv"
        self.rows = self.tmp / "decision_rows.csv"
        self.ledger = self.tmp / "member_create_uat_ledger.jsonl"
        self.package = self.tmp / "member_create_uat_package_v2.json"
        fx.write_form_csv(self.form)
        fx.write_decision_rows(self.rows)

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = approval.main(argv)
        return code, buf.getvalue()

    def _approve(self):
        return self._run(["approve", "--reviewer", "digital", "--input", str(self.form),
                          "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)])

    def _build(self, extra=None):
        argv = ["build-package", "--input", str(self.form), "--decision-rows", str(self.rows),
                "--row-number", "2", "--ledger", str(self.ledger), "--package-out", str(self.package)]
        return self._run(argv + (extra or []))

    def _events_of(self, event):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l).get("event") == event]

    def _build_events(self):
        return self._events_of("build")

    def _cleanup_incomplete_events(self):
        return self._events_of("build_cleanup_incomplete")

    def _stray_temps(self):
        return [f for f in os.listdir(self.tmp) if "mcuat_pkg" in f]

    def _reservations(self):
        return sorted(f for f in os.listdir(self.tmp) if f.endswith(approval.RESERVATION_SUFFIX))

    def _approval_id(self):
        entries = [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        approved = [e for e in entries if e.get("event") == "decision" and e.get("approval_id")]
        return approved[-1]["approval_id"]

    def test_publication_is_atomic_final_absent_and_temp_complete_before_link(self):
        # At the moment of publication (os.link), the final path must not yet exist and
        # the temporary file must already contain the complete, valid single JSON object.
        self._approve()
        captured = {}
        real_link = os.link

        def spy_link(src, dst):
            captured["final_absent"] = not os.path.lexists(dst)
            captured["temp_obj"] = json.loads(Path(src).read_text(encoding="utf-8"))
            return real_link(src, dst)

        with mock.patch("os.link", spy_link):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertTrue(captured["final_absent"], "final path must be absent until atomic publication")
        self.assertIsInstance(captured["temp_obj"], dict)
        self.assertEqual(captured["temp_obj"]["approval"]["decision"], "approved")
        self.assertTrue(self.package.is_file(), "publication exposes the complete package atomically")
        self.assertEqual(self._stray_temps(), [], "no stale temporary package after success")
        self.assertEqual(len(self._build_events()), 1)

    def test_temp_write_failure_leaves_no_final_no_temp_no_ledger(self):
        self._approve()
        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            code, out = self._build()
        # Amendment 7: this failure happens AFTER the committed build claim, so it is a
        # controlled consumed-approval outcome rather than an ordinary retryable error.
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "post_claim_publication_failed")
        self.assertEqual(summary["failure_stage"], "package_temporary_write")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertIs(summary["do_not_retry"], True)
        self.assertFalse(self.package.exists(), "final path must be absent on temp-write failure")
        self.assertEqual(self._stray_temps(), [], "temporary file must be cleaned")
        self.assertEqual(self._build_events(), [], "no build ledger event on failure")

    def test_publication_failure_after_reservation_blocks_approval(self):
        # Amendment 4 case 3: publication now happens strictly AFTER the durable reservation
        # is confirmed, so a publication failure publishes nothing yet permanently blocks the
        # approval. It must never be reported as an ordinary retryable error.
        self._approve()
        with mock.patch("os.link", side_effect=OSError("simulated publication failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_failed_after_reservation")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(summary["reservation"], "confirmed_durable")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertFalse(self.package.exists(), "final path must be absent when no competitor exists")
        self.assertEqual(self._stray_temps(), [], "temporary file must be cleaned")
        self.assertEqual(self._build_events(), [], "no build ledger event on failure")
        # The durable reservation survives and is unmatched, so it keeps the approval closed.
        self.assertEqual(len(self._reservations()), 1)

    def test_final_path_race_preserves_competitor_and_blocks_approval(self):
        self._approve()
        real_link = os.link
        sentinel = "COMPETING-SENTINEL-DO-NOT-OVERWRITE\n"

        def racing_link(src, dst):
            # A competitor creates the final path immediately before our publication.
            with open(dst, "x", encoding="utf-8") as fh:
                fh.write(sentinel)
            return real_link(src, dst)  # now fails FileExistsError against the competitor

        with mock.patch("os.link", racing_link):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        self.assertEqual(json.loads(out)["publication"], "not_published")
        self.assertEqual(self.package.read_text(encoding="utf-8"), sentinel,
                         "the competing final file must be preserved byte-for-byte")
        self.assertEqual(self._stray_temps(), [], "only the builder's temporary file is removed")
        self.assertEqual(self._build_events(), [], "no build ledger event on a publication collision")

    def test_completed_package_has_restrictive_permissions_where_supported(self):
        if os.name != "posix":
            self.skipTest("POSIX permission bits are not applicable on this platform")
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertEqual(oct(self.package.stat().st_mode & 0o777), "0o600")

    # ---- Amendment 3: pre-publication failure PLUS a forced temp-cleanup failure ---- #
    def test_temp_write_failure_with_forced_unlink_failure_is_cleanup_incomplete(self):
        self._approve()
        with mock.patch("os.fsync", side_effect=OSError("simulated write failure")), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "post_claim_publication_failed")
        self.assertNotEqual(summary["status"], "ok")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(summary["temp_cleanup"], "failed")
        # The original write failure stays distinguishable from the cleanup failure.
        self.assertEqual(summary["failure_stage"], "package_temporary_write")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertTrue(summary["stale_temp_basename"].endswith(".tmp"))
        self.assertFalse(self.package.exists(), "no final on pre-publication failure")
        self.assertTrue(self._stray_temps(), "the operation-owned temp remains")
        self.assertEqual(self._build_events(), [], "no successful build event")
        self.assertEqual(self._cleanup_incomplete_events(), [], "not published => no publication event")

    def test_publication_failure_with_forced_unlink_failure_blocks_and_reports_both(self):
        self._approve()
        with mock.patch("os.link", side_effect=OSError("simulated publication failure")), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_failed_after_reservation")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(summary["temp_cleanup"], "failed")
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertIs(summary["approval_blocked"], True)
        self.assertFalse(self.package.exists(), "the builder created no final path")
        self.assertTrue(self._stray_temps(), "the complete-but-unpublished temp remains")
        self.assertEqual(self._build_events(), [])
        self.assertEqual(self._cleanup_incomplete_events(), [])

    def test_race_with_forced_unlink_failure_preserves_competitor_and_blocks_approval(self):
        self._approve()
        real_link = os.link
        sentinel = "COMPETING-SENTINEL-DO-NOT-OVERWRITE\n"

        def racing_link(src, dst):
            with open(dst, "x", encoding="utf-8") as fh:
                fh.write(sentinel)
            return real_link(src, dst)  # FileExistsError against the competitor

        with mock.patch("os.link", racing_link), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_failed_after_reservation")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(self.package.read_text(encoding="utf-8"), sentinel,
                         "the competing final file must be preserved byte-for-byte, not deleted or mutated")
        self.assertTrue(self._stray_temps(), "the builder's own temp remains")
        self.assertEqual(self._build_events(), [])
        self.assertEqual(self._cleanup_incomplete_events(), [])

    # ---- Amendment 3: post-publication cleanup failure (committed boundary crossed) ---- #
    def test_post_publication_unlink_failure_publishes_and_ledgers_cleanup_incomplete(self):
        self._approve()
        with mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_CLEANUP_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertNotEqual(summary["status"], "ok", "must not report ordinary success")
        self.assertEqual(summary["status"], "cleanup_incomplete")
        self.assertEqual(summary["publication"], "succeeded")
        self.assertEqual(summary["temp_cleanup"], "failed")
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertIs(summary["do_not_retry"], True)
        # The final package exists, complete and valid.
        self.assertTrue(self.package.is_file(), "the published final package is preserved")
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        valid, reasons = contract.validate_package(pkg)
        self.assertTrue(valid, reasons)
        # The temporary link remains (cleanup failed).
        self.assertTrue(self._stray_temps(), "the temporary link remains on a cleanup failure")
        # Exactly one durable cleanup-incomplete publication event; no normal build event.
        ci = self._cleanup_incomplete_events()
        self.assertEqual(len(ci), 1)
        self.assertEqual(self._build_events(), [])
        # The event binds the exact published package and approval transaction.
        event = ci[0]
        self.assertEqual(event["operation_id"], pkg["operation_id"])
        self.assertEqual(event["bound_package_payload_hash"], pkg["payload_hash"])
        self.assertEqual(event["package_file_name"], self.package.name)
        self.assertEqual(event["source_record_id"], pkg["source_record_id"])
        self.assertIs(event.get("cleanup_incomplete"), True)

    def test_second_build_after_cleanup_incomplete_fails_closed(self):
        self._approve()
        with mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            code1, out1 = self._build()
        self.assertEqual(code1, approval.EXIT_CLEANUP_INCOMPLETE, out1)
        self.assertEqual(len(self._cleanup_incomplete_events()), 1)
        first_bytes = self.package.read_bytes()

        # A second plain build with the same approval must fail closed. Amendment 5: the
        # terminal reservation gate answers first, and it also tells the operator that a
        # stray temporary still needs manual removal.
        code2, out2 = self._build()
        self.assertEqual(code2, approval.EXIT_APPROVAL_CONSUMED, out2)
        summary2 = json.loads(out2)
        self.assertEqual(summary2["status"], "approval_consumed")
        self.assertEqual(
            [r["reservation_status"] for r in summary2["consumed_reservations"]],
            ["reconciled_cleanup_incomplete"],
        )
        self.assertIs(summary2["manual_temp_cleanup_required"], True)

        # The retired --rebuild flag cannot bypass the published-but-unclean state either.
        code3, out3 = self._build(extra=["--rebuild"])
        self.assertEqual(code3, approval.EXIT_APPROVAL_CONSUMED, out3)
        self.assertEqual(json.loads(out3)["status"], "rebuild_requires_fresh_approval")

        # No second package minted, no new build event, first package unchanged.
        self.assertEqual(len(self._cleanup_incomplete_events()), 1)
        self.assertEqual(self._build_events(), [])
        self.assertEqual(self.package.read_bytes(), first_bytes)

    # ---- Amendment 3: marker failure is not a package cleanup failure ---- #
    def test_marker_failure_after_clean_publication_is_still_success(self):
        self._approve()
        with mock.patch.object(approval, "_write_private_marker", side_effect=OSError("marker failure")):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["temp_cleanup"], "complete")
        self.assertTrue(self.package.is_file())
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        valid, reasons = contract.validate_package(pkg)
        self.assertTrue(valid, reasons)
        # A marker failure never becomes a temporary-package cleanup failure.
        self.assertEqual(self._stray_temps(), [], "temp cleanup still completes despite marker failure")
        self.assertEqual(len(self._build_events()), 1)
        self.assertEqual(self._cleanup_incomplete_events(), [])

    # ---- Amendment 3: no cleanup path ever sweeps unrelated temporaries ---- #
    def test_unrelated_temp_files_are_never_swept(self):
        self._approve()
        unrelated = self.tmp / ".mcuat_pkg_unrelated_sentinel.tmp"
        unrelated.write_text("UNRELATED-DO-NOT-SWEEP\n", encoding="utf-8")
        code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertTrue(unrelated.is_file(), "an unrelated temporary must never be swept")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "UNRELATED-DO-NOT-SWEEP\n")


class _LostAppendHandle:
    """Stand-in for the ledger append handle that fails at exactly one persistence stage and
    never lets the appended bytes reach the file.

    Modelling a LOST append - rather than one that happens to land anyway - is deliberate:
    it is the unsafe direction the durable single-use guarantee has to cover. Opening in
    append mode truncates nothing, so closing an untouched handle leaves the ledger
    byte-identical.
    """

    def __init__(self, handle, stage):
        self._handle = handle
        self._stage = stage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._handle.close()
        except OSError:
            pass
        return False

    def write(self, data):
        if self._stage == "write":
            raise OSError("simulated ledger write failure")
        return len(data)  # accepted, but never handed to the real file

    def flush(self):
        if self._stage == "flush":
            raise OSError("simulated ledger flush failure")
        return None

    def fileno(self):
        return self._handle.fileno()


class _LedgerAppendFailure:
    """Force a real ledger-persistence failure at exactly one stage of ``append_ledger``.

    Only the ledger's own append handle is affected: the package temporary file, the durable
    reservation, the private marker and every other file operation run normally, so the
    failure is produced at the true stage rather than by mocking an aggregate result.
    """

    STAGES = ("open", "write", "flush", "fsync")

    def __init__(self, ledger_path, stage):
        assert stage in self.STAGES
        self.ledger_path = str(ledger_path)
        self.stage = stage
        self._patches = []

    def __enter__(self):
        real_open = builtins.open
        real_fsync = os.fsync
        ledger_fds = set()

        def guarded_open(file, mode="r", *args, **kwargs):
            if str(file) != self.ledger_path or "a" not in str(mode):
                return real_open(file, mode, *args, **kwargs)
            if self.stage == "open":
                raise OSError("simulated ledger open failure")
            handle = real_open(file, mode, *args, **kwargs)
            ledger_fds.add(handle.fileno())
            if self.stage == "fsync":
                # write/flush are neutralised so the bytes never land, then fsync fails.
                return _LostAppendHandle(handle, "fsync")
            return _LostAppendHandle(handle, self.stage)

        def guarded_fsync(fd):
            if self.stage == "fsync" and fd in ledger_fds:
                raise OSError("simulated ledger fsync failure")
            return real_fsync(fd)

        self._patches = [
            mock.patch("builtins.open", guarded_open),
            mock.patch("os.fsync", guarded_fsync),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class _LandedAppendHandle:
    """Ledger append handle whose bytes REALLY REACH the REAL ledger file and become readable,
    and which only then fails at ``flush()`` or ``os.fsync()``.

    This is the deliberate opposite of ``_LostAppendHandle`` and the exact state Amendment 5
    exists for: a COMPLETE, perfectly parseable JSON record can be present and readable while
    its durability was never confirmed. Nothing here suppresses the ledger write - the record
    is genuinely on disk afterwards, which is the whole point.
    """

    def __init__(self, handle, stage):
        self._handle = handle
        self._stage = stage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._handle.close()
        except OSError:
            pass
        return False

    def write(self, data):
        written = self._handle.write(data)
        self._handle.flush()  # the complete record lands and is readable from here on
        return written

    def flush(self):
        if self._stage == "flush":
            raise OSError("simulated ledger flush failure after the record landed")
        return self._handle.flush()

    def fileno(self):
        return self._handle.fileno()


class _PartialAppendHandle:
    """Ledger append handle that lands only a PREFIX of the record in the REAL ledger file,
    leaves those torn bytes in place, and then fails at ``write()`` or ``flush()``.

    The prefix is cut mid-JSON, so the file is left ending in an unterminated, unparseable
    record - what a genuinely interrupted append leaves behind.
    """

    def __init__(self, handle, stage):
        self._handle = handle
        self._stage = stage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._handle.close()
        except OSError:
            pass
        return False

    def write(self, data):
        self._handle.write(data[: max(1, len(data) // 2)])
        self._handle.flush()  # the torn bytes are real and stay on disk
        if self._stage == "write":
            raise OSError("simulated ledger write failure after a partial record landed")
        return len(data)

    def flush(self):
        if self._stage == "flush":
            raise OSError("simulated ledger flush failure after a partial record landed")
        return self._handle.flush()

    def fileno(self):
        return self._handle.fileno()


class _RealBytesAppendFailure:
    """Force a ledger-persistence failure at one real stage of ``append_ledger`` while letting
    the bytes the handle accepts genuinely reach the real ledger file.

    Only the ledger's own append handle is intercepted: the package temporary, the durable
    reservation, the private marker and every other file operation run normally. The ledger fd
    is recorded at open time, so the selective ``os.fsync`` failure cannot hit the package or
    reservation fsyncs that already completed earlier in the build.
    """

    HANDLE = None   # subclass supplies the wrapper handle class
    STAGES = ()     # subclass supplies its supported failure stages

    def __init__(self, ledger_path, stage):
        assert stage in self.STAGES
        self.ledger_path = str(ledger_path)
        self.stage = stage
        self._patches = []

    def __enter__(self):
        real_open = builtins.open
        real_fsync = os.fsync
        ledger_fds = set()

        def guarded_open(file, mode="r", *args, **kwargs):
            if str(file) != self.ledger_path or "a" not in str(mode):
                return real_open(file, mode, *args, **kwargs)
            handle = real_open(file, mode, *args, **kwargs)
            ledger_fds.add(handle.fileno())
            return self.HANDLE(handle, self.stage)

        def guarded_fsync(fd):
            if self.stage == "fsync" and fd in ledger_fds:
                raise OSError("simulated ledger fsync failure after the record landed")
            return real_fsync(fd)

        self._patches = [
            mock.patch("builtins.open", guarded_open),
            mock.patch("os.fsync", guarded_fsync),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


class _VisibleLedgerAppendFailure(_RealBytesAppendFailure):
    """The COMPLETE record lands in the real ledger and becomes readable; then flush or fsync
    fails, so its durability is never confirmed."""

    HANDLE = _LandedAppendHandle
    STAGES = ("flush", "fsync")


class _PartialLedgerAppendFailure(_RealBytesAppendFailure):
    """Only a PREFIX of the record lands in the real ledger and stays there; then write or
    flush fails, leaving malformed JSONL behind."""

    HANDLE = _PartialAppendHandle
    STAGES = ("write", "flush")


class _CreateUatBuildHarness(_DecisionStoreFixtureMixin, unittest.TestCase):
    """Disposable synthetic fixture plus the shared build/inspect helpers.

    Every derived suite works only on a throwaway temporary directory of synthetic fixtures.
    No test touches AutoCount, the AutoCount VM, live n8n, Google Sheets, SMB or shared-folder
    state, an approval ledger or package outside its own temporary directory, real member
    data, capability-probe evidence, or any credential.
    """

    def setUp(self):
        self._reset()

    def _reset(self):
        """Start over on a brand-new disposable fixture directory."""
        self.tmp = Path(tempfile.mkdtemp())
        self.form = self.tmp / "form.csv"
        self.rows = self.tmp / "decision_rows.csv"
        self.ledger = self.tmp / "member_create_uat_ledger.jsonl"
        self.package = self.tmp / "member_create_uat_package_v2.json"
        fx.write_form_csv(self.form)
        fx.write_decision_rows(self.rows)

    # ---- helpers ---- #
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = approval.main(argv)
        return code, buf.getvalue()

    def _approve(self):
        return self._run(["approve", "--reviewer", "digital", "--input", str(self.form),
                          "--decision-rows", str(self.rows), "--row-number", "2",
                          "--ledger", str(self.ledger)])

    def _build(self, extra=None):
        argv = ["build-package", "--input", str(self.form), "--decision-rows", str(self.rows),
                "--row-number", "2", "--ledger", str(self.ledger), "--package-out", str(self.package)]
        return self._run(argv + (extra or []))

    def _entries(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _events_of(self, event):
        return [e for e in self._entries() if e.get("event") == event]

    def _reservations(self):
        return sorted(f for f in os.listdir(self.tmp) if f.endswith(approval.RESERVATION_SUFFIX))

    def _stray_temps(self):
        return sorted(f for f in os.listdir(self.tmp) if "mcuat_pkg" in f)

    def _approval_id(self):
        approved = [e for e in self._entries()
                    if e.get("event") == "decision" and e.get("approval_id")]
        return approved[-1]["approval_id"]

    def _slot(self, attempt=1):
        return approval.reservation_path(self.tmp, self._approval_id(), attempt)

    def _fresh_out(self, name):
        return ["--package-out", str(self.tmp / name)]

    # ---- shared terminal-state assertions (Amendment 5) ---- #
    def _assert_approval_consumed(self, code, out, *, statuses):
        """Assert the terminal reservation gate refused this build, reporting the given
        reservation statuses as diagnostics only."""
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "approval_consumed")
        self.assertEqual(summary["reservation"], "terminally_consumed")
        self.assertEqual(summary["publication"], "not_attempted")
        self.assertEqual(summary["event"], "none")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["rebuild_blocked"], True)
        self.assertIs(summary["rebuild_supported"], False)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertEqual(
            [r["reservation_status"] for r in summary["consumed_reservations"]], statuses
        )
        return summary

    def _assert_rebuild_retired(self, code, out):
        """Assert --rebuild was refused as a retired flag, before anything was created."""
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "rebuild_requires_fresh_approval")
        self.assertEqual(summary["publication"], "not_attempted")
        self.assertEqual(summary["reservation"], "not_attempted")
        self.assertEqual(summary["event"], "none")
        self.assertIs(summary["rebuild_supported"], False)
        self.assertIs(summary["fresh_approval_required"], True)
        return summary

    def _assert_ledger_integrity_uncertain(self, code, out):
        """Assert the ledger integrity gate refused, with a sanitised state and no traceback."""
        self.assertEqual(code, approval.EXIT_LEDGER_INTEGRITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertIsInstance(summary, dict, "exactly one sanitised JSON summary is printed")
        self.assertEqual(summary["status"], "ledger_integrity_uncertain")
        self.assertEqual(summary["publication"], "not_attempted")
        self.assertEqual(summary["reservation"], "not_attempted")
        self.assertEqual(summary["event"], "none")
        self.assertIn(summary["ledger_integrity"], approval.LEDGER_INTEGRITY_REASONS)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["controlled_recovery_required"], True)
        self.assertIs(summary["ledger_modified"], False)
        return summary


class DurableReservationTests(_CreateUatBuildHarness):
    """Amendment 4: a durable, exclusive publication reservation is confirmed BEFORE any
    final package is published, so the single-use approval boundary survives a ledger
    persistence failure.

    Every failure below is forced at its real stage. No test asserts an aggregate mock
    result, and no test touches AutoCount, the VM, n8n, SMB, real member data or any
    credential.
    """

    # ------------------------------------------------------------------ #
    # 1-4. Reservation creation and durability failures: nothing is published
    # ------------------------------------------------------------------ #
    def test_reservation_exclusive_create_failure_publishes_nothing(self):
        self._approve()
        real_os_open = os.open

        def guarded(path, flags, *args, **kwargs):
            if str(path).endswith(approval.RESERVATION_SUFFIX):
                raise OSError("simulated reservation exclusive-create failure")
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", guarded):
            code, out = self._build()
        # Amendment 7: the reservation is no longer the authorisation point, so a reservation
        # failure after the committed claim leaves the approval CONSUMED, not retryable.
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "reservation_failed_after_claim")
        self.assertEqual(summary["reservation"], "not_created")
        self.assertEqual(summary["reservation_durability"], "unconfirmed")
        self.assertEqual(summary["publication"], "not_published")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        # Nothing published, nothing reserved, no build event, temp truthfully cleaned.
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(self._stray_temps(), [])

    def test_reservation_lost_to_concurrent_owner_consumes_the_approval(self):
        # A competing attempt wins the slot inside the race window (between slot survey and
        # exclusive create), so O_EXCL fails closed and no package is published.
        #
        # Amendment 6 corrects the classification: FileExistsError was reported as
        # `not_created` (retryable), which was false - the competing reservation DEFINITELY
        # exists and terminally consumed the approval, so retry guidance must never be given.
        self._approve()
        slot = self._slot()
        competitor = '{"competing-owner": true}\n'
        real_chmod = os.chmod

        def racing_chmod(path, mode, *args, **kwargs):
            if not slot.exists():
                slot.write_text(competitor, encoding="utf-8")  # concurrent owner takes the slot
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch("os.chmod", racing_chmod):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "reservation_failed_after_claim")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertEqual(summary["reservation"], approval.ReservationError.CONSUMED)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertIs(summary["competing_reservation_preserved"], True)
        # No retry guidance of any kind is emitted for a consumed approval.
        self.assertNotIn("then_retry", summary["recovery"])
        self.assertEqual(summary["recovery"], "fresh_approval_or_controlled_recovery")
        self.assertFalse(self.package.exists(), "the loser of the reservation race publishes nothing")
        self.assertEqual(self._events_of("build"), [])
        # The competitor's reservation is byte-for-byte untouched, and no extra slot exists.
        self.assertEqual(slot.read_text(encoding="utf-8"), competitor)
        self.assertEqual(self._reservations(), [slot.name])

    def test_reservation_oserror_with_existing_slot_consumes_the_approval(self):
        # A non-FileExists create failure where a non-following existence check PROVES an
        # object occupies the slot must also be classified consumed, never retryable.
        self._approve()
        slot = self._slot()
        competitor = '{"pre-existing-owner": true}\n'
        slot.write_text(competitor, encoding="utf-8")
        real_os_open = os.open

        def guarded(path, flags, *args, **kwargs):
            if str(path).endswith(approval.RESERVATION_SUFFIX):
                raise OSError("simulated non-FileExists reservation create failure")
            return real_os_open(path, flags, *args, **kwargs)

        # The terminal gate would normally catch the pre-existing slot first, so the survey is
        # neutralised to drive the writer's own create-failure classification directly.
        with mock.patch.object(approval, "survey_reservations", lambda *a, **k: []), \
                mock.patch("os.open", guarded):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "reservation_failed_after_claim")
        self.assertEqual(summary["reservation"], approval.ReservationError.CONSUMED)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertFalse(self.package.exists())
        self.assertEqual(slot.read_text(encoding="utf-8"), competitor)

    def test_reservation_oserror_with_absent_slot_still_consumes_after_claim(self):
        # The mirror case. `not_created` is still the correct RESERVATION classification when
        # lexists positively proves the slot is empty - but under Amendment 7 that no longer
        # makes the BUILD retryable, because the committed claim already consumed the approval.
        self._approve()
        real_os_open = os.open

        def guarded(path, flags, *args, **kwargs):
            if str(path).endswith(approval.RESERVATION_SUFFIX):
                raise OSError("simulated reservation create failure with no slot created")
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch("os.open", guarded):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "reservation_failed_after_claim")
        self.assertEqual(summary["reservation"], approval.ReservationError.NOT_CREATED)
        self.assertEqual(summary["build_claim"], "committed")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertNotIn("then_retry", summary["recovery"])
        self.assertEqual(self._reservations(), [])
        # A second build at a fresh path is blocked by the claim.
        code, out = self._build(extra=self._fresh_out("after_reservation_gap.json"))
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        self.assertEqual(json.loads(out)["build_claim"], "already_committed")

    def test_reservation_write_failure_is_uncertain_and_publishes_nothing(self):
        self._approve()
        real_fdopen = os.fdopen
        calls = {"n": 0}

        def guarded_fdopen(fd, *args, **kwargs):
            calls["n"] += 1
            handle = real_fdopen(fd, *args, **kwargs)
            # Call 1 is the package temporary; call 2 is the reservation.
            return _LostAppendHandle(handle, "write") if calls["n"] == 2 else handle

        with mock.patch("os.fdopen", guarded_fdopen):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["reservation"], "uncertain")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertEqual(summary["publication"], "not_published")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._events_of("build"), [])
        # The uncertain entry is neither deleted nor recreated: it keeps the approval closed.
        self.assertEqual(len(self._reservations()), 1)

    def test_reservation_fsync_failure_is_uncertain_and_publishes_nothing(self):
        self._approve()
        real_fsync = os.fsync
        calls = {"n": 0}

        def guarded_fsync(fd):
            calls["n"] += 1
            # Call 1 is the package temporary's fsync; call 2 is the reservation's.
            if calls["n"] == 2:
                raise OSError("simulated reservation fsync failure")
            return real_fsync(fd)

        with mock.patch("os.fsync", guarded_fsync):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        self.assertEqual(json.loads(out)["reservation"], "uncertain")
        self.assertEqual(json.loads(out)["build_claim"], "committed")
        self.assertFalse(self.package.exists(), "no package may be published without a durable reservation")
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(len(self._reservations()), 1)

    def test_reservation_parent_directory_durability_failure_publishes_nothing(self):
        # Exercises the POSIX directory-entry fsync branch inside the REAL
        # `approval._fsync_parent_directory`. os.O_DIRECTORY is supplied where the platform lacks
        # it so the same real code path runs everywhere.
        #
        # Amendment 9: the decision store's trusted-parent walk also opens directories, so a
        # blanket "any O_DIRECTORY open fails" injection would break store admission and the run
        # would refuse with store_parent_untrusted long before reaching the reservation stage
        # under test. The injection is therefore scoped to the EXACT reservation call site - a
        # PATHNAME open of the reservation state directory itself, carrying no directory
        # descriptor. The store's anchor open ("/") and its descriptor-relative component opens
        # both fall outside that predicate and run normally, and `fired` proves the reservation
        # durability call is the one that actually failed.
        self._approve()
        dir_flag = getattr(os, "O_DIRECTORY", 0x10000)
        real_os_open = os.open
        reservation_dir = os.path.abspath(str(self.tmp))
        fired = {"n": 0}

        def guarded(path, flags, *args, **kwargs):
            if (flags & dir_flag
                    and kwargs.get("dir_fd") is None
                    and os.path.abspath(str(path)) == reservation_dir):
                fired["n"] += 1
                raise OSError("simulated reservation directory fsync failure")
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "O_DIRECTORY", dir_flag, create=True), \
                mock.patch("os.open", guarded):
            code, out = self._build()
        self.assertEqual(fired["n"], 1,
                         "the reservation directory durability call must be the one that failed")
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        self.assertEqual(json.loads(out)["reservation"], "uncertain")
        self.assertEqual(json.loads(out)["build_claim"], "committed")
        self.assertFalse(self.package.exists())
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(self._stray_temps(), [], "no unrelated path is left behind")

    def test_unsafe_reservation_path_is_a_not_created_failure(self):
        # An unsafe reservation path must surface as a typed reservation failure, not as a
        # bare contract error that would escape the publication writer's handlers and leave
        # the operation-owned temporary file uncleaned.
        with self.assertRaises(approval.ReservationError) as caught:
            approval.write_reservation("relative_not_absolute.reservation", {})
        self.assertEqual(caught.exception.state, "not_created")

    def test_reparse_point_reservation_slot_publishes_nothing_and_cleans_temp(self):
        # The real path-safety rejection inside write_reservation runs, and the real
        # ContractError -> ReservationError conversion with it. Only the reparse-point
        # DETECTOR is forced, so the test is deterministic on platforms where an
        # unprivileged process cannot create a symlink.
        self._approve()
        slot = self._slot()
        real_detector = contract.is_reparse_point

        def detector(path):
            return str(path) == str(slot) or real_detector(path)

        with mock.patch.object(contract, "is_reparse_point", detector):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)
        summary = json.loads(out)
        self.assertEqual(summary["reservation"], "not_created")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertFalse(self.package.exists(), "nothing may be published without a safe reservation")
        self.assertEqual(self._events_of("build"), [])
        # The temporary file is still cleaned truthfully: the conversion keeps the failure
        # inside the publication writer's typed handling instead of escaping it.
        self.assertEqual(summary["temp_cleanup"], "complete")
        self.assertEqual(self._stray_temps(), [], "the operation-owned temporary is still cleaned")
        self.assertEqual(self._reservations(), [], "no reservation entry was created")

    def test_reservation_durability_mode_is_reported_not_assumed(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        expected = "file_and_directory_fsync" if hasattr(os, "O_DIRECTORY") else "file_fsync_only"
        self.assertEqual(json.loads(out)["reservation_durability"], expected)

    # ------------------------------------------------------------------ #
    # 5-8. Clean publication, then each ledger persistence stage fails
    # ------------------------------------------------------------------ #
    def _assert_published_but_ledger_incomplete(self, out, code, *, cleanup):
        summary = json.loads(out)
        self.assertEqual(code, approval.EXIT_LEDGER_RECORD_INCOMPLETE, out)
        self.assertEqual(summary["status"], "ledger_record_incomplete")
        self.assertNotEqual(summary["status"], "ok")
        self.assertEqual(summary["event"], "none", "no build event may be claimed")
        self.assertEqual(summary["publication"], "succeeded")
        self.assertEqual(summary["ledger_record"], "incomplete")
        self.assertEqual(summary["reservation"], "confirmed_durable")
        self.assertEqual(summary["temp_cleanup"], cleanup)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        # The published package is complete, valid and recorded by a durable reservation.
        self.assertTrue(self.package.is_file(), "the published final package is preserved")
        pkg = json.loads(self.package.read_text(encoding="utf-8"))
        valid, reasons = contract.validate_package(pkg)
        self.assertTrue(valid, reasons)
        self.assertEqual(len(self._reservations()), 1)
        # No ledger build event of either kind exists.
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(self._events_of("build_cleanup_incomplete"), [])
        return summary

    def test_clean_publication_then_ledger_open_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "open"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")
        self.assertEqual(self._stray_temps(), [], "temporary cleanup completed and is reported so")

    def test_clean_publication_then_ledger_write_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "write"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")

    def test_clean_publication_then_ledger_flush_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "flush"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")

    def test_clean_publication_then_ledger_fsync_failure(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "fsync"):
            code, out = self._build()
        self._assert_published_but_ledger_incomplete(out, code, cleanup="complete")

    # ------------------------------------------------------------------ #
    # 9. Published + cleanup incomplete + each ledger persistence failure
    # ------------------------------------------------------------------ #
    def _published_cleanup_incomplete_with_ledger_failure(self, stage):
        self._approve()
        with _LedgerAppendFailure(self.ledger, stage), \
                mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            return self._build()

    def test_published_cleanup_incomplete_then_ledger_open_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("open")
        summary = self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")
        # All three facts stay distinguishable.
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertTrue(summary["stale_temp_basename"].endswith(".tmp"))
        self.assertTrue(self._stray_temps(), "the operation-owned temporary alias remains")

    def test_published_cleanup_incomplete_then_ledger_write_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("write")
        summary = self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")
        self.assertIs(summary["manual_cleanup_required"], True)

    def test_published_cleanup_incomplete_then_ledger_flush_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("flush")
        self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")

    def test_published_cleanup_incomplete_then_ledger_fsync_failure(self):
        code, out = self._published_cleanup_incomplete_with_ledger_failure("fsync")
        self._assert_published_but_ledger_incomplete(out, code, cleanup="failed")

    # ------------------------------------------------------------------ #
    # 10-13. The unmatched reservation blocks every second build
    # ------------------------------------------------------------------ #
    def _publish_then_lose_ledger(self):
        """Reach the case-4 state: published cleanly, durable build event lost."""
        self._approve()
        with _LedgerAppendFailure(self.ledger, "write"):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_LEDGER_RECORD_INCOMPLETE, out)
        self.assertEqual(self._events_of("build"), [])
        return self.package.read_bytes()

    def test_second_plain_build_after_unmatched_reservation_fails_closed(self):
        published = self._publish_then_lose_ledger()
        fresh = self.tmp / "member_create_uat_package_v2_second.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self._assert_approval_consumed(code, out, statuses=["unmatched"])
        self.assertFalse(fresh.exists(), "no second package may be minted at a fresh absent path")
        self.assertEqual(self.package.read_bytes(), published, "published bytes are unchanged")

    def test_second_rebuild_after_unmatched_reservation_fails_closed(self):
        published = self._publish_then_lose_ledger()
        fresh = self.tmp / "member_create_uat_package_v2_rebuild.json"
        code, out = self._build(extra=["--rebuild"] + self._fresh_out(fresh.name))
        self._assert_rebuild_retired(code, out)
        self.assertFalse(fresh.exists(), "--rebuild must not bypass an unmatched reservation")
        self.assertEqual(self.package.read_bytes(), published)

    def test_blocked_approval_cannot_build_at_any_fresh_path(self):
        published = self._publish_then_lose_ledger()
        fresh = self.tmp / "member_create_uat_package_v2_alt0.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self._assert_approval_consumed(code, out, statuses=["unmatched"])
        self.assertFalse(fresh.exists())
        rebuilt = self.tmp / "member_create_uat_package_v2_alt1.json"
        code, out = self._build(extra=["--rebuild"] + self._fresh_out(rebuilt.name))
        self._assert_rebuild_retired(code, out)
        self.assertFalse(rebuilt.exists())
        self.assertEqual(self.package.read_bytes(), published)
        self.assertEqual(self._events_of("build"), [])
        self.assertEqual(len(self._reservations()), 1, "no extra reservation slot was consumed")

    def test_uncertain_reservation_also_blocks_plain_build_and_rebuild(self):
        # An uncertain (possibly partial) reservation is malformed on re-read, and must block
        # just as firmly as an unmatched one - it is never repaired or ignored.
        self._approve()
        real_fdopen = os.fdopen
        calls = {"n": 0}

        def guarded_fdopen(fd, *args, **kwargs):
            calls["n"] += 1
            handle = real_fdopen(fd, *args, **kwargs)
            return _LostAppendHandle(handle, "write") if calls["n"] == 2 else handle

        with mock.patch("os.fdopen", guarded_fdopen):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_BLOCKED, out)

        fresh = self.tmp / "member_create_uat_package_v2_unc0.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self._assert_approval_consumed(code, out, statuses=["malformed"])
        self.assertFalse(fresh.exists())
        rebuilt = self.tmp / "member_create_uat_package_v2_unc1.json"
        code, out = self._build(extra=["--rebuild"] + self._fresh_out(rebuilt.name))
        self._assert_rebuild_retired(code, out)
        self.assertFalse(rebuilt.exists())

    def test_foreign_reservation_blocks_the_approval(self):
        # A well-formed reservation bound to a different approval/source record must never be
        # treated as this approval's own reconciled state.
        self._approve()
        record = {
            "schema_version": approval.RESERVATION_SCHEMA_VERSION,
            "reserved_at": "2026-07-25T00:00:00+00:00",
            "attempt": 1,
            "approval_id": "appr_" + ("0" * 32),
            "source_record_id": "srcrec_" + ("0" * 64),
            "source_fingerprint": "fp_" + ("0" * 64),
            "operation_id": "mcuat_" + ("0" * 32),
            "bound_package_payload_hash": "sha256:" + ("0" * 64),
            "package_file_name": "someone_else.json",
        }
        self._slot().write_text(json.dumps(record), encoding="utf-8")
        code, out = self._build()
        self._assert_approval_consumed(code, out, statuses=["foreign"])
        self.assertFalse(self.package.exists())

    # ------------------------------------------------------------------ #
    # 14. Competing and historical packages are never touched
    # ------------------------------------------------------------------ #
    def test_historical_and_competing_packages_unchanged_after_ledger_failure(self):
        historical = self.tmp / "member_create_uat_package_v1.json"
        historical.write_text("HISTORICAL-V1-EVIDENCE-DO-NOT-TOUCH\n", encoding="utf-8")
        competing = self.tmp / "member_create_uat_package_v2_other.json"
        competing.write_text("COMPETING-DO-NOT-TOUCH\n", encoding="utf-8")
        before = (historical.read_bytes(), competing.read_bytes())

        published = self._publish_then_lose_ledger()

        self.assertEqual((historical.read_bytes(), competing.read_bytes()), before)
        self.assertEqual(self.package.read_bytes(), published)
        # A blocked retry also leaves them untouched.
        self._build(extra=self._fresh_out("member_create_uat_package_v2_retry.json"))
        self.assertEqual((historical.read_bytes(), competing.read_bytes()), before)

    # ------------------------------------------------------------------ #
    # 15. No broad reservation, state-directory or temporary-file sweep
    # ------------------------------------------------------------------ #
    def test_no_directory_sweep_occurs_on_any_path(self):
        # Reservation slots are found by direct, bounded path lookup. Any directory listing,
        # scan or glob anywhere in the build would raise here.
        self._approve()
        other_reservation = self.tmp / (
            approval.RESERVATION_PREFIX + "appr_" + ("f" * 32) + ".1" + approval.RESERVATION_SUFFIX
        )
        other_reservation.write_text("UNRELATED-RESERVATION-DO-NOT-TOUCH\n", encoding="utf-8")
        unrelated_temp = self.tmp / ".mcuat_pkg_unrelated_sentinel.tmp"
        unrelated_temp.write_text("UNRELATED-DO-NOT-SWEEP\n", encoding="utf-8")

        def no_sweep(*args, **kwargs):
            raise AssertionError("the build must never list, scan or glob a directory")

        with mock.patch("os.listdir", no_sweep), mock.patch("os.scandir", no_sweep), \
                mock.patch("glob.glob", no_sweep), \
                mock.patch.object(Path, "iterdir", no_sweep), \
                mock.patch.object(Path, "glob", no_sweep):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        # Unrelated reservation and temporary are byte-for-byte untouched.
        self.assertEqual(other_reservation.read_text(encoding="utf-8"),
                         "UNRELATED-RESERVATION-DO-NOT-TOUCH\n")
        self.assertEqual(unrelated_temp.read_text(encoding="utf-8"), "UNRELATED-DO-NOT-SWEEP\n")

    # ------------------------------------------------------------------ #
    # 16-17. Clean success, and the retirement of clean reconciled --rebuild
    # ------------------------------------------------------------------ #
    def test_clean_publication_emits_exactly_one_build_event_and_exits_zero(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["event"], "build")
        self.assertEqual(summary["temp_cleanup"], "complete")
        self.assertEqual(summary["ledger_record"], "complete")
        self.assertEqual(summary["reservation"], "confirmed_durable")
        builds = self._events_of("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(self._events_of("build_cleanup_incomplete"), [])
        self.assertEqual(self._stray_temps(), [])
        # Exactly one reservation, reconciled to that build event by every binding field.
        self.assertEqual(len(self._reservations()), 1)
        record = json.loads(self._slot().read_text(encoding="utf-8"))
        for field in approval.RESERVATION_BINDING_FIELDS:
            self.assertEqual(record[field], builds[0][field], field)
        self.assertEqual(builds[0]["reservation_file_name"], self._slot().name)
        self.assertEqual(
            approval.reservation_reconciliation(
                self._entries(), record, self._approval_id(), record["source_record_id"]
            ),
            approval._ReservationStatus.RECONCILED_BUILD,
        )

    def test_clean_reconciled_rebuild_is_retired(self):
        # THE EXACT BEHAVIOUR AMENDMENT 5 RETIRES. Amendment 4 allowed a second package here
        # because the reservation was perfectly reconciled to a readable `build` event. That
        # is unsound: readability is not durability, so a matching event proves nothing about
        # whether the first attempt finished. The perfectly reconciled state now blocks too.
        self._approve()
        self.assertEqual(self._build()[0], 0)
        first = self.package.read_bytes()
        record = json.loads(self._slot().read_text(encoding="utf-8"))
        self.assertEqual(
            approval.reservation_reconciliation(
                self._entries(), record, self._approval_id(), record["source_record_id"]
            ),
            approval._ReservationStatus.RECONCILED_BUILD,
            "the reservation really is cleanly reconciled - and must still block",
        )
        alt = self.tmp / "member_create_uat_package_v2b.json"
        code, out = self._build(extra=["--rebuild"] + self._fresh_out(alt.name))
        self._assert_rebuild_retired(code, out)
        self.assertFalse(alt.exists(), "no second package may be minted from one approval")
        self.assertEqual(self.package.read_bytes(), first, "the earlier package is untouched")
        # Still exactly one build event and one reservation slot.
        self.assertEqual(len(self._events_of("build")), 1)
        self.assertEqual(self._reservations(), [self._slot(1).name])

    def test_reservation_contains_no_member_data_or_credentials(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)
        text = self._slot().read_text(encoding="utf-8")
        for secret in ("90000001", "6590000001", "Synthetic Alpha",
                       "synthetic.alpha@example.invalid", "2000-01-01"):
            self.assertNotIn(secret, text)
        record = json.loads(text)
        self.assertEqual(record["schema_version"], approval.RESERVATION_SCHEMA_VERSION)
        self.assertEqual(sorted(record), sorted(
            ("schema_version", "reserved_at", "attempt") + approval.RESERVATION_BINDING_FIELDS
        ))
        # No absolute path is stored: the binding is the output BASENAME only.
        self.assertEqual(record["package_file_name"], self.package.name)
        self.assertNotIn(str(self.tmp), text)

    # ------------------------------------------------------------------ #
    # 18. A fresh approval is mechanically distinguishable from the blocked one
    # ------------------------------------------------------------------ #
    def test_fresh_approval_is_distinguishable_from_blocked_prior_approval(self):
        published = self._publish_then_lose_ledger()
        blocked_id = self._approval_id()
        blocked_slot = self._slot()
        blocked_bytes = blocked_slot.read_bytes()

        # A deliberate fresh reviewer decision mints a distinct approval id, so it reserves a
        # distinct slot and is not blocked by the prior approval's unmatched reservation.
        self.assertEqual(self._approve()[0], 0)
        fresh_id = self._approval_id()
        self.assertNotEqual(fresh_id, blocked_id)

        fresh = self.tmp / "member_create_uat_package_v2_fresh.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self.assertEqual(code, 0, out)
        self.assertTrue(fresh.is_file())
        # The blocked approval's reservation is untouched, and the two are distinct slots.
        self.assertEqual(blocked_slot.read_bytes(), blocked_bytes)
        self.assertNotEqual(blocked_slot.name, self._slot().name)
        self.assertEqual(self.package.read_bytes(), published, "the first package is preserved")
        # The new build event binds the fresh approval only.
        builds = self._events_of("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["approval_id"], fresh_id)


class VisibleButUnconfirmedLedgerTests(_CreateUatBuildHarness):
    """Amendment 5, the exact P1: a COMPLETE and READABLE ledger build event whose durability
    was never confirmed must not authorise another package from the same approval.

    A real flush/fsync failure can leave the record fully present on disk, so on restart it is
    indistinguishable from a properly persisted event. Amendment 4 accepted it as proof of a
    finished build and let ``--rebuild`` mint a second package. Under the terminal reservation
    rule the reservation blocks regardless.

    Each test drives TWO invocations: the first is interrupted at a real persistence stage with
    its bytes allowed to land, and the second is a genuinely fresh invocation that reads only
    the files the first process left behind. No helper suppresses the ledger write.
    """

    def _publish_with_visible_unconfirmed_event(self, stage, *, cleanup_fails):
        """First invocation: publish, land the complete build record, then fail durability."""
        self._approve()
        patches = [_VisibleLedgerAppendFailure(self.ledger, stage)]
        if cleanup_fails:
            patches.append(mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")))
        with patches[0]:
            if cleanup_fails:
                with patches[1]:
                    code, out = self._build()
            else:
                code, out = self._build()

        # Step 4: the explicit ledger-record-incomplete terminal state, never a clean success.
        self.assertEqual(code, approval.EXIT_LEDGER_RECORD_INCOMPLETE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "ledger_record_incomplete")
        self.assertNotEqual(summary["status"], "ok")
        self.assertEqual(summary["event"], "none")
        self.assertEqual(summary["publication"], "succeeded")
        self.assertEqual(summary["ledger_record"], "incomplete")
        self.assertEqual(summary["temp_cleanup"], "failed" if cleanup_fails else "complete")
        self.assertIs(summary["approval_blocked"], True)

        # Steps 1-2: the COMPLETE record really did land and is readable, exactly as a real
        # flush/fsync failure leaves it. This is the state that must NOT re-open the approval.
        expected_event = "build_cleanup_incomplete" if cleanup_fails else "build"
        landed = self._events_of(expected_event)
        self.assertEqual(len(landed), 1, "the complete build record must be present on disk")
        self.assertEqual(landed[0]["approval_id"], self._approval_id())
        self.assertTrue(self.ledger.read_text(encoding="utf-8").endswith("\n"),
                        "the landed record is newline-terminated, i.e. structurally intact")
        return expected_event

    def _assert_readable_event_does_not_reopen_the_approval(self, expected_event):
        """Steps 5-9: a fresh invocation is still blocked, and nothing is added or changed."""
        published = self.package.read_bytes()
        reservations_before = self._reservations()
        temps_before = self._stray_temps()
        self.assertEqual(len(reservations_before), 1)

        # The readable event genuinely reconciles against the reservation - and must still not
        # release it. That equality is what made the Amendment 4 loophole look safe.
        record = json.loads(self._slot().read_text(encoding="utf-8"))
        expected_status = (
            approval._ReservationStatus.RECONCILED_BUILD if expected_event == "build"
            else approval._ReservationStatus.RECONCILED_CLEANUP_INCOMPLETE
        )
        self.assertEqual(
            approval.reservation_reconciliation(
                self._entries(), record, self._approval_id(), record["source_record_id"]
            ),
            expected_status,
            "the readable event does match the reservation - and must still block",
        )

        # Step 6: a fresh plain build invocation is blocked.
        plain = self.tmp / "member_create_uat_package_v2_plain.json"
        self._assert_approval_consumed(
            *self._build(extra=self._fresh_out(plain.name)), statuses=[expected_status]
        )
        self.assertFalse(plain.exists(), "no second package at a fresh absent path")

        # Step 7: --rebuild to a distinct fresh absent path is blocked.
        rebuilt = self.tmp / "member_create_uat_package_v2_rebuild.json"
        self._assert_rebuild_retired(*self._build(extra=["--rebuild"] + self._fresh_out(rebuilt.name)))
        self.assertFalse(rebuilt.exists(), "--rebuild mints nothing at a fresh absent path")

        # Step 8: no additional reservation slot was consumed.
        self.assertEqual(self._reservations(), reservations_before)
        # Step 9: the published final package is byte-for-byte unchanged.
        self.assertEqual(self.package.read_bytes(), published)
        # No new temporary was created, and no pre-existing one was swept.
        self.assertEqual(self._stray_temps(), temps_before)
        # No further ledger event of either kind was appended.
        self.assertEqual(len(self._events_of(expected_event)), 1)

    def test_clean_publication_visible_flush_failure_still_blocks_reuse(self):
        event = self._publish_with_visible_unconfirmed_event("flush", cleanup_fails=False)
        self._assert_readable_event_does_not_reopen_the_approval(event)

    def test_clean_publication_visible_fsync_failure_still_blocks_reuse(self):
        event = self._publish_with_visible_unconfirmed_event("fsync", cleanup_fails=False)
        self._assert_readable_event_does_not_reopen_the_approval(event)

    def test_cleanup_incomplete_publication_visible_flush_failure_still_blocks_reuse(self):
        event = self._publish_with_visible_unconfirmed_event("flush", cleanup_fails=True)
        self._assert_readable_event_does_not_reopen_the_approval(event)

    def test_cleanup_incomplete_publication_visible_fsync_failure_still_blocks_reuse(self):
        event = self._publish_with_visible_unconfirmed_event("fsync", cleanup_fails=True)
        self._assert_readable_event_does_not_reopen_the_approval(event)


class PartialLedgerAppendTests(_CreateUatBuildHarness):
    """Amendment 5: a torn (partial) ledger append leaves malformed JSONL. Reading it must fail
    closed with an explicit sanitised state - never an uncontrolled JSON decoding traceback -
    and must not repair, truncate, rewrite or replace the file."""

    def _publish_with_partial_ledger_append(self, stage):
        self._approve()
        approval_id = self._approval_id()
        slot = self._slot()
        with _PartialLedgerAppendFailure(self.ledger, stage):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_LEDGER_RECORD_INCOMPLETE, out)
        self.assertEqual(json.loads(out)["status"], "ledger_record_incomplete")
        # The torn bytes are genuinely present: the ledger no longer ends with a terminated
        # record, so it can no longer be read as an intact append-only log.
        raw = self.ledger.read_text(encoding="utf-8")
        self.assertFalse(raw.endswith("\n"), "a torn append leaves an unterminated tail")
        self.assertTrue(self.package.is_file(), "the package was published before the append")
        self.assertTrue(slot.is_file(), "the reservation was created before publication")
        return raw, approval_id, slot

    def _assert_everything_blocked_and_unchanged(self, raw, slot):
        published = self.package.read_bytes()
        reservation_bytes = slot.read_bytes()
        reservations_before = self._reservations()
        temps_before = self._stray_temps()

        created_temps = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            created_temps.append(name)
            return fd, name

        with mock.patch("tempfile.mkstemp", spy_mkstemp):
            # A plain build reports the explicit sanitised ledger-integrity state.
            plain = self.tmp / "member_create_uat_package_v2_plain.json"
            self._assert_ledger_integrity_uncertain(*self._build(extra=self._fresh_out(plain.name)))
            # --rebuild is refused as retired, equally without a traceback.
            rebuilt = self.tmp / "member_create_uat_package_v2_rebuild.json"
            code, out = self._build(extra=["--rebuild"] + self._fresh_out(rebuilt.name))
            self._assert_rebuild_retired(code, out)
            self.assertNotIn("Traceback", out)

        # No package, no temporary and no new reservation were created.
        self.assertFalse(plain.exists())
        self.assertFalse(rebuilt.exists())
        self.assertEqual(created_temps, [], "no temporary package may be created")
        self.assertEqual(self._reservations(), reservations_before)
        self.assertEqual(self._stray_temps(), temps_before)
        # The existing final package, the reservation and the torn ledger are all unchanged:
        # the malformed record is neither discarded, repaired, truncated nor replaced.
        self.assertEqual(self.package.read_bytes(), published)
        self.assertEqual(slot.read_bytes(), reservation_bytes)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), raw)

    def test_partial_append_on_write_failure_blocks_fail_closed(self):
        raw, _approval_id, slot = self._publish_with_partial_ledger_append("write")
        self._assert_everything_blocked_and_unchanged(raw, slot)

    def test_partial_append_on_flush_failure_blocks_fail_closed(self):
        raw, _approval_id, slot = self._publish_with_partial_ledger_append("flush")
        self._assert_everything_blocked_and_unchanged(raw, slot)


class LedgerIntegrityTests(_CreateUatBuildHarness):
    """Amendment 5: every way the ledger can be structurally untrustworthy produces the same
    explicit sanitised non-success - JSONDecodeError, a short final record, an invalid record
    shape, and read/decode failures - with no traceback and no repair."""

    def _append_raw(self, text):
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write(text)

    def test_malformed_json_record_is_sanitised_non_success(self):
        self._approve()
        self._append_raw('{"event": "build", NOT VALID JSON}\n')
        summary = self._assert_ledger_integrity_uncertain(*self._build())
        self.assertEqual(summary["ledger_integrity"], "malformed_json_record")
        self.assertFalse(self.package.exists())

    def test_short_final_record_is_sanitised_non_success(self):
        # A truncated tail is rejected as a torn append even though the earlier records parse.
        self._approve()
        self._append_raw('{"event": "build", "approval_id": "appr_')
        summary = self._assert_ledger_integrity_uncertain(*self._build())
        self.assertEqual(summary["ledger_integrity"], "torn_final_record")
        self.assertFalse(self.package.exists())

    def test_invalid_record_shape_is_sanitised_non_success(self):
        # Parseable JSON that is not an object cannot be a ledger record.
        self._approve()
        self._append_raw('["event", "build"]\n')
        summary = self._assert_ledger_integrity_uncertain(*self._build())
        self.assertEqual(summary["ledger_integrity"], "invalid_record_shape")
        self.assertFalse(self.package.exists())

    def test_undecodable_ledger_is_sanitised_non_success(self):
        self._approve()
        with open(self.ledger, "ab") as handle:
            handle.write(b"\xff\xfe not utf-8 \xff\n")
        summary = self._assert_ledger_integrity_uncertain(*self._build())
        self.assertEqual(summary["ledger_integrity"], "undecodable_text")
        self.assertFalse(self.package.exists())

    def test_unreadable_ledger_is_sanitised_non_success(self):
        # Only the ledger's own read is forced to fail, so the real OSError arm runs.
        self._approve()
        real_read_text = Path.read_text

        def guarded(path_self, *args, **kwargs):
            if str(path_self) == str(self.ledger):
                raise OSError("simulated ledger read failure")
            return real_read_text(path_self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded):
            code, out = self._build()
        summary = self._assert_ledger_integrity_uncertain(code, out)
        self.assertEqual(summary["ledger_integrity"], "unreadable")
        self.assertFalse(self.package.exists())

    def test_malformed_ledger_output_leaks_no_content_pii_or_private_path(self):
        self._approve()
        # A malformed record stuffed with member-shaped and credential-shaped SYNTHETIC
        # sentinels. None of these is a real value; they exist only to be searched for in the
        # output, proving the handler reports the defect's shape and never its content.
        self._append_raw(
            '{"event": "build", "Name": "Synthetic Alpha", '
            '"EmailAddress": "synthetic.alpha@example.invalid", '
            '"MemberNo": "6590000001", "secret": "SYNTHETIC-SECRET-SENTINEL", BROKEN}\n'
        )
        code, out = self._build()
        self._assert_ledger_integrity_uncertain(code, out)
        for leaked in ("Synthetic Alpha", "synthetic.alpha@example.invalid", "6590000001",
                       "90000001", "SYNTHETIC-SECRET-SENTINEL", "BROKEN",
                       str(self.tmp), str(self.ledger)):
            self.assertNotIn(leaked, out, f"the output must not contain {leaked!r}")

    def test_decision_refuses_to_append_onto_a_torn_ledger(self):
        # Appending a decision onto an unterminated record would splice it into those bytes and
        # destroy the partial record's recoverability, so the decision is refused instead.
        self._approve()
        self._append_raw('{"event": "decision", "reviewer_id": "dig')
        before = self.ledger.read_bytes()
        code, out = self._run(
            ["approve", "--reviewer", "digital", "--input", str(self.form),
             "--decision-rows", str(self.rows), "--row-number", "2", "--ledger", str(self.ledger)]
        )
        summary = self._assert_ledger_integrity_uncertain(code, out)
        self.assertEqual(summary["ledger_integrity"], "torn_final_record")
        self.assertEqual(self.ledger.read_bytes(), before, "the ledger is left exactly as found")

    def test_missing_ledger_is_legitimately_empty_not_an_integrity_failure(self):
        # The absence of an audit ledger is not a defect; it simply carries no audit records.
        # The build is refused by the decision-authority gate instead, because no store exists.
        self.assertFalse(self.ledger.exists())
        self.assertEqual(approval.read_ledger(self.ledger), [])
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertEqual(json.loads(out)["status"], "decision_store_missing")


class TerminalReservationContractTests(_CreateUatBuildHarness):
    """Amendment 5: the terminal reservation rule as a contract - one build per approval, the
    full reservation-state matrix, the fresh-approval recovery path, and the preservation and
    privacy guarantees that must survive every blocked path."""

    # ---- normal success is still single-use ---- #
    def test_normal_success_is_single_use_for_plain_build_and_rebuild(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        published = self.package.read_bytes()
        builds_before = self._events_of("build")
        reservations_before = self._reservations()
        self.assertEqual(len(builds_before), 1)
        self.assertEqual(len(reservations_before), 1)

        created_temps = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            created_temps.append(name)
            return fd, name

        with mock.patch("tempfile.mkstemp", spy_mkstemp):
            second = self.tmp / "member_create_uat_package_v2_second.json"
            self._assert_approval_consumed(
                *self._build(extra=self._fresh_out(second.name)), statuses=["reconciled_build"]
            )
            third = self.tmp / "member_create_uat_package_v2_third.json"
            self._assert_rebuild_retired(
                *self._build(extra=["--rebuild"] + self._fresh_out(third.name))
            )

        # Both were refused BEFORE any temporary file could be created.
        self.assertEqual(created_temps, [], "blocked before temporary creation")
        self.assertFalse(second.exists())
        self.assertFalse(third.exists())
        # No second reservation slot and no second build event.
        self.assertEqual(self._reservations(), reservations_before)
        self.assertEqual(self._events_of("build"), builds_before)
        self.assertEqual(self.package.read_bytes(), published)

    def test_legacy_ledger_build_event_without_a_reservation_still_blocks(self):
        # Defence in depth for legacy state: a ledger build event with NO build claim and NO
        # reservation must still refuse. Claims are append-only, so this state is built directly
        # rather than by deleting a claim (which the immutability triggers correctly forbid).
        srid, fp = self._fixture_identity()
        record = self._insert_activated("approved", srid=srid, fingerprint=fp)
        approval.append_ledger(self.ledger, {
            "event": "build",
            "recorded_at": iso(datetime.now(timezone.utc)),
            "source_record_id": srid,
            "source_fingerprint": fp,
            "approval_id": record["approval_id"],
            "operation_id": "mcuat_" + ("4" * 32),
            "bound_package_payload_hash": "sha256:" + ("5" * 64),
            "package_file_name": "member_create_uat_package_v2_legacy_prior.json",
        })
        self.assertEqual(self._reservations(), [])
        fresh = self.tmp / "member_create_uat_package_v2_legacy.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self.assertEqual(code, 2, out)
        self.assertIn("already built", json.loads(out)["error"])
        self.assertFalse(fresh.exists())

    # ---- the fresh-approval recovery path ---- #
    def test_fresh_approval_builds_once_then_is_consumed_itself(self):
        self._approve()
        old_id = self._approval_id()
        self.assertEqual(self._build()[0], 0)
        old_slot = self._slot()
        old_slot_bytes = old_slot.read_bytes()
        first_published = self.package.read_bytes()
        srid = json.loads(self.package.read_text(encoding="utf-8"))["source_record_id"]

        # A genuinely fresh reviewer decision mints a different approval id.
        self.assertEqual(self._approve()[0], 0)
        fresh_id = self._approval_id()
        self.assertNotEqual(fresh_id, old_id)

        # The old approval remains blocked: its reservation still terminally consumes it.
        self.assertNotEqual(
            approval.survey_reservations(self.tmp, old_id, srid, self._entries()), [],
            "the superseded approval stays consumed",
        )

        # The fresh approval performs exactly one build, at a fresh path.
        fresh_out = self.tmp / "member_create_uat_package_v2_fresh.json"
        code, out = self._build(extra=self._fresh_out(fresh_out.name))
        self.assertEqual(code, 0, out)
        self.assertTrue(fresh_out.is_file())
        self.assertEqual(json.loads(out)["status"], "ok")
        self.assertEqual(len(self._reservations()), 2, "the fresh approval used its own slot")
        self.assertEqual(self._events_of("build")[-1]["approval_id"], fresh_id)

        # It is then permanently consumed itself, by either invocation form.
        again = self.tmp / "member_create_uat_package_v2_fresh_again.json"
        self._assert_approval_consumed(
            *self._build(extra=self._fresh_out(again.name)), statuses=["reconciled_build"]
        )
        self.assertFalse(again.exists())
        once_more = self.tmp / "member_create_uat_package_v2_fresh_rebuild.json"
        self._assert_rebuild_retired(
            *self._build(extra=["--rebuild"] + self._fresh_out(once_more.name))
        )
        self.assertFalse(once_more.exists())

        # Both packages and the superseded approval's reservation are preserved.
        self.assertEqual(self.package.read_bytes(), first_published)
        self.assertEqual(old_slot.read_bytes(), old_slot_bytes)
        self.assertEqual(len(self._reservations()), 2, "no third slot was consumed")

    # ---- the reservation-state matrix ---- #
    def _state_confirmed_with_build_event(self):
        self._approve()
        self.assertEqual(self._build()[0], 0)

    def _state_confirmed_with_cleanup_incomplete_event(self):
        self._approve()
        with mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            self.assertEqual(self._build()[0], approval.EXIT_CLEANUP_INCOMPLETE)

    def _state_unmatched(self):
        self._approve()
        with _LedgerAppendFailure(self.ledger, "write"):
            self.assertEqual(self._build()[0], approval.EXIT_LEDGER_RECORD_INCOMPLETE)

    def _state_uncertain(self):
        self._approve()
        real_fdopen = os.fdopen
        calls = {"n": 0}

        def guarded_fdopen(fd, *args, **kwargs):
            calls["n"] += 1
            handle = real_fdopen(fd, *args, **kwargs)
            # Call 1 is the package temporary; call 2 is the reservation.
            return _LostAppendHandle(handle, "write") if calls["n"] == 2 else handle

        with mock.patch("os.fdopen", guarded_fdopen):
            self.assertEqual(self._build()[0], approval.EXIT_PUBLICATION_BLOCKED)

    def _state_malformed(self):
        self._approve()
        self._slot().write_text("NOT-A-RESERVATION-RECORD\n", encoding="utf-8")

    def _state_foreign(self):
        self._approve()
        self._slot().write_text(json.dumps({
            "schema_version": approval.RESERVATION_SCHEMA_VERSION,
            "reserved_at": "2026-07-25T00:00:00+00:00",
            "attempt": 1,
            "approval_id": "appr_" + ("0" * 32),
            "source_record_id": "srcrec_" + ("0" * 64),
            "source_fingerprint": "fp_" + ("0" * 64),
            "operation_id": "mcuat_" + ("0" * 32),
            "bound_package_payload_hash": "sha256:" + ("0" * 64),
            "package_file_name": "someone_else.json",
        }), encoding="utf-8")

    def _state_visible_unconfirmed_ledger_line(self):
        self._approve()
        with _VisibleLedgerAppendFailure(self.ledger, "fsync"):
            self.assertEqual(self._build()[0], approval.EXIT_LEDGER_RECORD_INCOMPLETE)
        self.assertEqual(len(self._events_of("build")), 1, "the full record really landed")

    def _state_partial_ledger_line(self):
        self._approve()
        with _PartialLedgerAppendFailure(self.ledger, "write"):
            self.assertEqual(self._build()[0], approval.EXIT_LEDGER_RECORD_INCOMPLETE)

    def test_every_reservation_state_blocks_further_use_of_the_approval(self):
        # Whatever a reservation's reconciliation status, its existence terminally consumes the
        # approval. The eighth case cannot even be classified - the ledger itself is torn - so
        # it blocks one layer earlier, at the ledger integrity gate.
        cases = (
            ("confirmed_with_build_event",
             self._state_confirmed_with_build_event, ["reconciled_build"]),
            ("confirmed_with_cleanup_incomplete_event",
             self._state_confirmed_with_cleanup_incomplete_event, ["reconciled_cleanup_incomplete"]),
            ("unmatched", self._state_unmatched, ["unmatched"]),
            ("uncertain", self._state_uncertain, ["malformed"]),
            ("malformed", self._state_malformed, ["malformed"]),
            ("foreign", self._state_foreign, ["foreign"]),
            ("reservation_plus_full_readable_line_after_fsync_failure",
             self._state_visible_unconfirmed_ledger_line, ["reconciled_build"]),
            ("reservation_plus_partial_line", self._state_partial_ledger_line, None),
        )
        for name, prepare, statuses in cases:
            with self.subTest(reservation_state=name):
                self._reset()
                prepare()
                self.assertEqual(len(self._reservations()), 1, "a reservation object exists")
                plain = self.tmp / f"matrix_{name}.json"
                code, out = self._build(extra=self._fresh_out(plain.name))
                if statuses is None:
                    self._assert_ledger_integrity_uncertain(code, out)
                else:
                    self._assert_approval_consumed(code, out, statuses=statuses)
                self.assertFalse(plain.exists(), "no package at a fresh absent path")
                rebuilt = self.tmp / f"matrix_{name}_rebuild.json"
                self._assert_rebuild_retired(
                    *self._build(extra=["--rebuild"] + self._fresh_out(rebuilt.name))
                )
                self.assertFalse(rebuilt.exists())
                self.assertEqual(len(self._reservations()), 1, "no extra slot was consumed")

    # ---- preservation and privacy across every blocked path ---- #
    def test_blocked_paths_preserve_neighbours_sweep_nothing_and_leak_nothing(self):
        historical = self.tmp / "member_create_uat_package_v1.json"
        historical.write_text("HISTORICAL-V1-EVIDENCE-DO-NOT-TOUCH\n", encoding="utf-8")
        competing = self.tmp / "member_create_uat_package_v2_other.json"
        competing.write_text("COMPETING-DO-NOT-TOUCH\n", encoding="utf-8")
        unrelated_temp = self.tmp / ".mcuat_pkg_unrelated_sentinel.tmp"
        unrelated_temp.write_text("UNRELATED-DO-NOT-SWEEP\n", encoding="utf-8")
        other_reservation = self.tmp / (
            approval.RESERVATION_PREFIX + "appr_" + ("f" * 32) + ".1" + approval.RESERVATION_SUFFIX
        )
        other_reservation.write_text("UNRELATED-RESERVATION-DO-NOT-TOUCH\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in (historical, competing, unrelated_temp, other_reservation)}

        self._approve()

        unlinked = []
        created_temps = []
        real_unlink = os.unlink
        real_mkstemp = tempfile.mkstemp

        def spy_unlink(path, *args, **kwargs):
            unlinked.append(str(path))
            return real_unlink(path, *args, **kwargs)

        def spy_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            created_temps.append(name)
            return fd, name

        def no_sweep(*args, **kwargs):
            raise AssertionError("no build path may list, scan or glob a directory")

        with mock.patch("os.unlink", spy_unlink), mock.patch("tempfile.mkstemp", spy_mkstemp), \
                mock.patch("os.listdir", no_sweep), mock.patch("os.scandir", no_sweep), \
                mock.patch("glob.glob", no_sweep), \
                mock.patch.object(Path, "iterdir", no_sweep), \
                mock.patch.object(Path, "glob", no_sweep):
            # One successful build, then every blocked path in turn.
            code, out = self._build()
            self.assertEqual(code, 0, out)
            outputs = [out]
            blocked_plain = self.tmp / "member_create_uat_package_v2_blocked.json"
            code, out = self._build(extra=self._fresh_out(blocked_plain.name))
            self._assert_approval_consumed(code, out, statuses=["reconciled_build"])
            outputs.append(out)
            blocked_rebuild = self.tmp / "member_create_uat_package_v2_blocked_rebuild.json"
            code, out = self._build(extra=["--rebuild"] + self._fresh_out(blocked_rebuild.name))
            self._assert_rebuild_retired(code, out)
            outputs.append(out)
            # And the ledger-integrity path, on a torn tail carrying a distinctive sentinel so
            # the assertions below can prove no ledger CONTENT reaches the output. The fixed
            # shape classifier `torn_final_record` is not content and is reported.
            with open(self.ledger, "a", encoding="utf-8") as handle:
                handle.write('{"event": "build", "LEDGER-CONTENT-SENTINEL')
            code, out = self._build(extra=self._fresh_out("member_create_uat_package_v2_torn.json"))
            self._assert_ledger_integrity_uncertain(code, out)
            outputs.append(out)

        # Only the ONE operation-owned temporary of the successful build was ever unlinked.
        self.assertEqual(len(created_temps), 1, "only the successful build creates a temporary")
        self.assertEqual(unlinked, [created_temps[0]],
                         "exactly the operation-owned temporary is targeted, nothing else")
        # Every neighbour is byte-for-byte unchanged.
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original, f"{path.name} must be untouched")
        self.assertFalse(blocked_plain.exists())
        self.assertFalse(blocked_rebuild.exists())
        # No raw member value, credential-shaped value, absolute private path or ledger content
        # appears in any output.
        combined = "".join(outputs)
        for leaked in ("90000001", "6590000001", "Synthetic Alpha",
                       "synthetic.alpha@example.invalid", "2000-01-01", "AC2_PROBE",
                       str(self.tmp), "HISTORICAL-V1-EVIDENCE", "LEDGER-CONTENT-SENTINEL"):
            self.assertNotIn(leaked, combined, f"the output must not contain {leaked!r}")


class PendingDecisionAuthorityTests(_CreateUatBuildHarness):
    """Amendment 6 P1: a COMPLETE, READABLE JSONL approval line is not authority.

    ``append_ledger`` writes the whole line before ``flush()`` and ``os.fsync()`` return, so a
    real persistence failure can leave a perfectly readable ``approved`` line behind even
    though the command reported failure. Authority now requires a committed ACTIVATION row in
    the transactional store, which that line can never supply.

    Each test drives two invocations: the decision command is interrupted at a real
    persistence stage with its bytes allowed to land, then a genuinely fresh build invocation
    reads only what the first process left behind.
    """

    def _decide(self, decision, extra=None):
        argv = [decision, "--reviewer", "digital", "--input", str(self.form),
                "--decision-rows", str(self.rows), "--row-number", "2",
                "--ledger", str(self.ledger)]
        return self._run(argv + (extra or []))

    def _decide_with_visible_audit_failure(self, decision, stage):
        """Step 1 commits the pending decision; step 2 lands the complete real audit line and
        then fails at the real flush/fsync stage; step 3 never runs."""
        with _VisibleLedgerAppendFailure(self.ledger, stage):
            code, out = self._decide(decision)
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_audit_incomplete")
        self.assertEqual(summary["decision_authority"], "pending")
        self.assertIs(summary["decision_activated"], False)
        self.assertIs(summary["decision_recorded"], True)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertEqual(summary["audit_append"], "unconfirmed")

        # The COMPLETE audit line really landed and is readable, exactly as a real flush/fsync
        # failure leaves it - this is the state that must NOT confer authority.
        audit = [e for e in self._entries() if e.get("event") == "decision"]
        self.assertTrue(audit, "the complete audit line must be present and readable")
        expected = {"approve": "approved", "reject": "rejected", "hold": "hold"}[decision]
        self.assertEqual(audit[-1]["decision"], expected)
        self.assertTrue(self.ledger.read_text(encoding="utf-8").endswith("\n"))

        # The pending decision committed, and NO activation row exists.
        rows = self._stored_decisions()
        self.assertEqual(rows[-1]["decision_id"], summary["decision_id"])
        self.assertIsNone(rows[-1]["activation_sequence"], "no activation row may exist")
        return summary

    def _assert_build_refused_as_pending(self, *, expected_activated):
        created_temps = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            created_temps.append(name)
            return fd, name

        with mock.patch("tempfile.mkstemp", spy_mkstemp):
            code, out = self._build(extra=self._fresh_out("pending_block.json"))
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_pending_or_uncertain")
        self.assertEqual(summary["decision_authority"], decisions.AuthorityState.PENDING_NEWER)
        self.assertGreaterEqual(summary["newer_pending_decisions"], 1)
        self.assertIs(summary["legacy_ledger_approval_accepted"], False)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["decision_activated"], expected_activated)
        # Nothing was created at all.
        self.assertEqual(created_temps, [], "no temporary package may be created")
        self.assertFalse((self.tmp / "pending_block.json").exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._stray_temps(), [])
        return summary

    # ---- A: visible-but-unconfirmed approval audit line ---- #
    def test_visible_approval_audit_line_after_flush_failure_is_not_authority(self):
        self._decide_with_visible_audit_failure("approve", "flush")
        self._assert_build_refused_as_pending(expected_activated=False)

    def test_visible_approval_audit_line_after_fsync_failure_is_not_authority(self):
        self._decide_with_visible_audit_failure("approve", "fsync")
        self._assert_build_refused_as_pending(expected_activated=False)

    # ---- A (ordering): a newer PENDING hold/reject never falls back to an older approval ---- #
    def test_pending_hold_after_activated_approval_blocks_the_build(self):
        self.assertEqual(self._approve()[0], 0, "the first approval activates normally")
        self._decide_with_visible_audit_failure("hold", "fsync")
        summary = self._assert_build_refused_as_pending(expected_activated=True)
        # The older approval IS activated, and is still not used.
        self.assertIsNotNone(summary["decision_authority"])

    def test_pending_reject_after_activated_approval_blocks_the_build(self):
        self.assertEqual(self._approve()[0], 0)
        self._decide_with_visible_audit_failure("reject", "flush")
        self._assert_build_refused_as_pending(expected_activated=True)

    # ---- B: partial decision-audit append ---- #
    def _decide_with_partial_audit_append(self, decision, stage):
        with _PartialLedgerAppendFailure(self.ledger, stage):
            code, out = self._decide(decision)
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertEqual(json.loads(out)["status"], "decision_audit_incomplete")
        raw = self.ledger.read_text(encoding="utf-8")
        self.assertFalse(raw.endswith("\n"), "a torn append leaves an unterminated tail")
        rows = self._stored_decisions()
        self.assertIsNone(rows[-1]["activation_sequence"], "no activation row may exist")
        return raw

    def _assert_partial_audit_blocks_everything(self, raw):
        store_bytes = self._store_path().read_bytes()
        created_temps = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            created_temps.append(name)
            return fd, name

        with mock.patch("tempfile.mkstemp", spy_mkstemp):
            code, out = self._build(extra=self._fresh_out("torn_block.json"))
        summary = self._assert_ledger_integrity_uncertain(code, out)
        self.assertNotIn("Traceback", out)
        # Nothing retried, repaired, created or mutated.
        self.assertEqual(created_temps, [])
        self.assertFalse((self.tmp / "torn_block.json").exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), raw,
                         "the partial bytes remain exactly as found")
        self.assertEqual(self._store_path().read_bytes(), store_bytes,
                         "the decision store is not mutated")
        # No raw ledger content reaches the output.
        self.assertNotIn("digital", out)
        self.assertNotIn("srcrec_", out)
        return summary

    def test_partial_approval_audit_write_failure_blocks_fail_closed(self):
        raw = self._decide_with_partial_audit_append("approve", "write")
        self._assert_partial_audit_blocks_everything(raw)

    def test_partial_approval_audit_flush_failure_blocks_fail_closed(self):
        raw = self._decide_with_partial_audit_append("approve", "flush")
        self._assert_partial_audit_blocks_everything(raw)


class DecisionTransactionRecoveryTests(_CreateUatBuildHarness):
    """Amendment 6 section C: every commit failure is resolved by REOPENING the database and
    looking for the exact row, never by inferring the outcome from the exception.

    ``_commit`` is the seam, so a failure can be forced before the commit (nothing committed)
    or immediately AFTER a commit that really succeeded - the case that makes reopen-and-look
    mandatory. Recovery assertions always use a freshly opened connection.

    Amendment 9 inserts one more commit into first-use creation - the ADMISSION fact, written
    after publication - so the commit ordinals are named rather than hard-coded:

      1. canonical schema creation;
      2. the store ADMISSION fact;
      3. the PENDING reviewer decision;
      4. the decision ACTIVATION.
    """

    COMMIT_SCHEMA = 1
    COMMIT_ADMISSION = 2
    COMMIT_PENDING_DECISION = 3
    COMMIT_ACTIVATION = 4

    def _approve_cli(self):
        return self._run(["approve", "--reviewer", "digital", "--input", str(self.form),
                          "--decision-rows", str(self.rows), "--row-number", "2",
                          "--ledger", str(self.ledger)])

    def _commit_after_n(self, failures_after):
        """Return a ``_commit`` replacement that really commits, then raises, on call N."""
        real_commit = decisions._commit
        calls = {"n": 0}

        def wrapper(conn):
            calls["n"] += 1
            if calls["n"] == failures_after:
                real_commit(conn)          # the transaction genuinely commits ...
                raise sqlite3.OperationalError("simulated failure after a successful commit")
            return real_commit(conn)

        return wrapper, calls

    def _commit_before_n(self, fail_on):
        """Return a ``_commit`` replacement that raises WITHOUT committing on call N."""
        real_commit = decisions._commit
        calls = {"n": 0}

        def wrapper(conn):
            calls["n"] += 1
            if calls["n"] == fail_on:
                raise sqlite3.OperationalError("simulated failure before commit")
            return real_commit(conn)

        return wrapper, calls

    # ---- pending-decision commit ---- #
    def test_pending_commit_failure_before_commit_leaves_no_row(self):
        wrapper, _calls = self._commit_before_n(self.COMMIT_PENDING_DECISION)
        with mock.patch.object(decisions, "_commit", wrapper):
            code, out = self._approve_cli()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_not_recorded")
        self.assertIs(summary["decision_recorded"], False)
        self.assertIs(summary["approval_blocked"], False, "a clean retry is safe")
        self.assertIs(summary["do_not_retry"], False)
        self.assertEqual(summary["audit_append"], "not_attempted")
        # A FRESH connection proves no decision row and no audit line exist.
        self.assertEqual(self._stored_decisions(), [])
        self.assertEqual([e for e in self._entries() if e.get("event") == "decision"], [])

    def test_pending_commit_succeeds_then_raises_is_recovered_as_committed(self):
        wrapper, _calls = self._commit_after_n(self.COMMIT_PENDING_DECISION)
        with mock.patch.object(decisions, "_commit", wrapper):
            code, out = self._approve_cli()
        # The exception said "failed", but reopening found the exact row, so the decision is
        # correctly treated as recorded and the state machine continues to activation.
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "ok")
        self.assertIs(summary["decision_recorded"], True)
        self.assertIs(summary["decision_activated"], True)
        rows = self._stored_decisions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision_id"], summary["decision_id"])
        self.assertIsNotNone(rows[0]["activation_sequence"])

    def test_pending_commit_recovery_that_cannot_read_fails_closed(self):
        wrapper, _calls = self._commit_after_n(self.COMMIT_PENDING_DECISION)
        with mock.patch.object(decisions, "_commit", wrapper), \
                mock.patch.object(decisions, "fetch_decision",
                                  side_effect=sqlite3.DatabaseError("simulated unreadable store")):
            code, out = self._approve_cli()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"], "commit_state_uncertain")
        self.assertIs(summary["controlled_recovery_required"], True)
        self.assertNotIn("Traceback", out)

    def test_pending_commit_recovery_reopens_rather_than_trusting_the_exception(self):
        # Proof that recovery genuinely REOPENS: the recovery connection is counted.
        wrapper, _calls = self._commit_after_n(self.COMMIT_PENDING_DECISION)
        opens = {"n": 0}
        real_connect = decisions._connect_uri

        def counting_open(path, query):
            opens["n"] += 1
            return real_connect(path, query)

        with mock.patch.object(decisions, "_commit", wrapper), \
                mock.patch.object(decisions, "_connect_uri", counting_open):
            code, out = self._approve_cli()
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(opens["n"], 2, "the store is reopened for recovery")

    # ---- activation commit ---- #
    def test_activation_commit_succeeds_then_raises_is_recovered_as_committed(self):
        wrapper, _calls = self._commit_after_n(self.COMMIT_ACTIVATION)
        with mock.patch.object(decisions, "_commit", wrapper):
            code, out = self._approve_cli()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertIs(summary["decision_activated"], True)
        self.assertEqual(summary["decision_authority"], "activated")
        self.assertEqual(self._activation_count(), 1)
        # The recovered activation really is authority: a build now proceeds.
        self.assertEqual(self._build()[0], 0)

    def test_activation_absent_after_exception_leaves_the_decision_pending(self):
        wrapper, _calls = self._commit_before_n(self.COMMIT_ACTIVATION)
        with mock.patch.object(decisions, "_commit", wrapper):
            code, out = self._approve_cli()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_not_activated")
        self.assertEqual(summary["decision_authority"], "pending")
        self.assertIs(summary["decision_activated"], False)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertEqual(summary["audit_append"], "confirmed")
        # Fresh connection: the decision is recorded, unactivated, and grants nothing.
        self.assertEqual(self._activation_count(), 0)
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertEqual(json.loads(out)["status"], "decision_pending_or_uncertain")

    def test_activation_state_unreadable_fails_closed(self):
        wrapper, _calls = self._commit_after_n(self.COMMIT_ACTIVATION)
        with mock.patch.object(decisions, "_commit", wrapper), \
                mock.patch.object(decisions, "fetch_activation",
                                  side_effect=sqlite3.DatabaseError("simulated unreadable store")):
            code, out = self._approve_cli()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"], "activation_state_uncertain")
        self.assertNotIn("Traceback", out)

    def test_a_write_lock_held_by_another_connection_fails_closed_without_retrying(self):
        # A concurrent writer holding the write lock past the bounded busy timeout must fail
        # closed after exactly ONE attempt - never an automatic retry loop.
        self.assertEqual(self._approve_cli()[0], 0)   # establish the store
        blocker = self._raw()
        attempts = {"n": 0}
        real_insert = decisions.insert_pending_decision

        def counting_insert(conn, record):
            attempts["n"] += 1
            return real_insert(conn, record)

        try:
            # RESERVED is held but no page has been written yet, so no rollback journal exists
            # and the contention is decided by the bounded busy timeout rather than by
            # pre-open triage.
            blocker.execute("BEGIN IMMEDIATE")
            self.assertFalse(
                any(os.path.lexists(p) for p in self._sidecar_paths()),
                "BEGIN IMMEDIATE alone must not create a rollback journal",
            )
            with mock.patch.object(decisions, "BUSY_TIMEOUT_MS", 200), \
                    mock.patch.object(decisions, "insert_pending_decision", counting_insert):
                code, out = self._approve_cli()
        finally:
            decisions._rollback_quietly(blocker)
            blocker.close()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        # Nothing was written and no filesystem object changed, so lock contention keeps the
        # explicit-retry boundary rather than becoming a terminal integrity failure.
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_not_recorded")
        self.assertIs(summary["do_not_retry"], False)
        self.assertEqual(attempts["n"], 0, "the insert is never reached: no retry loop")
        self.assertEqual(len(self._stored_decisions()), 1, "only the first decision exists")

    def test_a_concurrent_writers_journal_is_refused_without_being_touched(self):
        # Amendment 8: a concurrent writer that has actually written a page leaves a rollback
        # journal, and pure pre-open triage refuses on it BEFORE SQLite is opened. Such a store
        # is controlled-recovery-only: the journal is never rolled back, deleted or renamed.
        self.assertEqual(self._approve_cli()[0], 0)
        blocker = self._raw()
        try:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('lock_probe', 'held')"
            )
            journal = Path(str(self._store_path()) + "-journal")
            self.assertTrue(journal.exists(), "a written transaction leaves a journal")
            before = journal.read_bytes()
            code, out = self._approve_cli()
            self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
            summary = json.loads(out)
            self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
            self.assertEqual(summary["decision_store_integrity"], "store_sidecar_present")
            self.assertIs(summary["decision_store_modified"], False)
            self.assertIs(summary["do_not_retry"], True)
            self.assertNotIn("Traceback", out)
            self.assertTrue(journal.exists(), "the journal is never removed")
            self.assertEqual(journal.read_bytes(), before, "the journal is never altered")
        finally:
            decisions._rollback_quietly(blocker)
            blocker.close()
        self.assertEqual(len(self._stored_decisions()), 1, "no second decision was recorded")


class DecisionOrderingTests(_CreateUatBuildHarness):
    """Amendment 6 section D: the newest ACTIVATED decision is authoritative, and a newer
    committed-but-unactivated decision blocks rather than falling back to an older approval."""

    def _identity(self):
        return self._fixture_identity()

    def test_activated_approval_then_activated_hold_refuses(self):
        srid, fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        self._insert_activated("hold", srid=srid, fingerprint=fp)
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_not_approved")
        self.assertEqual(summary["decision_authority"], decisions.AuthorityState.HOLD)
        self.assertFalse(self.package.exists())

    def test_activated_approval_then_activated_reject_refuses(self):
        srid, fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        self._insert_activated("rejected", srid=srid, fingerprint=fp)
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertEqual(json.loads(out)["decision_authority"],
                         decisions.AuthorityState.REJECTED)

    def test_activated_approval_then_pending_hold_refuses(self):
        srid, fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        self._insert_pending(self._record("hold", srid=srid, fingerprint=fp))
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_pending_or_uncertain")
        self.assertEqual(summary["newer_pending_decisions"], 1)
        self.assertIs(summary["decision_activated"], True, "an older activated approval exists")
        self.assertFalse(self.package.exists(), "the older approval must not be used")

    def test_activated_approval_then_pending_reject_refuses(self):
        srid, fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        self._insert_pending(self._record("rejected", srid=srid, fingerprint=fp))
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertEqual(json.loads(out)["status"], "decision_pending_or_uncertain")
        self.assertFalse(self.package.exists())

    def test_expired_activated_approval_refuses(self):
        srid, fp = self._identity()
        self._insert_activated(
            "approved", srid=srid, fingerprint=fp,
            recorded_at="2000-01-01T00:00:00+00:00",
            expires_at="2000-01-04T00:00:00+00:00",
        )
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertIn("expired", out)

    def test_source_fingerprint_drift_refuses(self):
        srid, _fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint="fp_" + ("7" * 64))
        code, out = self._build()
        self.assertEqual(code, 2, out)
        self.assertIn("fingerprint mismatch", out)
        self.assertFalse(self.package.exists())

    def test_activated_approval_alone_proceeds_to_the_reservation_gate(self):
        srid, fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertTrue(self.package.is_file())

    def test_fresh_activated_approval_after_a_consumed_one_proceeds(self):
        srid, fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        self.assertEqual(self._build()[0], 0)
        first = self.package.read_bytes()
        # The prior approval is terminally consumed by its reservation; a genuinely fresh
        # ACTIVATED approval mints a new approval id and can build once at a fresh path.
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        fresh = self.tmp / "member_create_uat_package_v2_fresh.json"
        code, out = self._build(extra=self._fresh_out(fresh.name))
        self.assertEqual(code, 0, out)
        self.assertTrue(fresh.is_file())
        self.assertEqual(self.package.read_bytes(), first, "the first package is preserved")
        self.assertEqual(len(self._reservations()), 2)

    def test_all_decision_types_share_one_monotonic_sequence(self):
        srid, fp = self._identity()
        self._insert_activated("approved", srid=srid, fingerprint=fp)
        self._insert_activated("hold", srid=srid, fingerprint=fp)
        self._insert_activated("rejected", srid=srid, fingerprint=fp)
        sequences = [row["sequence"] for row in self._stored_decisions()]
        self.assertEqual(sequences, [1, 2, 3])
        types = [row["decision_type"] for row in self._stored_decisions()]
        self.assertEqual(types, ["approved", "hold", "rejected"])


class DecisionStoreIntegrityTests(_CreateUatBuildHarness):
    """Amendment 6 section E: every way the decision store can be untrustworthy produces the
    same explicit sanitised non-success - never a traceback, never a mutation."""

    def _assert_store_refusal(self, code, out, reason):
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"], reason)
        self.assertIn(reason, decisions.STORE_INTEGRITY_REASONS)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["controlled_recovery_required"], True)
        self.assertIs(summary["decision_store_modified"], False)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        return summary

    def _prepare_activated_approval(self):
        srid, fp = self._fixture_identity()
        return self._insert_activated("approved", srid=srid, fingerprint=fp)

    def test_missing_store_during_build_requires_a_fresh_decision(self):
        self.assertFalse(self._store_path().exists())
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_missing")
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertFalse(self._store_path().exists(),
                         "build-package must never manufacture an empty store")

    def test_wrong_schema_version_refuses(self):
        self._prepare_activated_approval()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            # Rewriting the recorded version in place (schema_meta is metadata, not
            # append-only history) isolates the version check from the schema-shape checks.
            raw.execute("UPDATE schema_meta SET value = 'member_create_uat_decisions/v1' "
                        "WHERE key = 'schema_version'")
            raw.commit()
        finally:
            raw.close()
        self._assert_store_refusal(*self._build(), reason="schema_version_mismatch")

    def test_missing_schema_version_row_refuses(self):
        self._prepare_activated_approval()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("DELETE FROM schema_meta")
            raw.commit()
        finally:
            raw.close()
        self._assert_store_refusal(*self._build(), reason="schema_version_missing")

    def test_missing_table_refuses(self):
        self._prepare_activated_approval()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("DROP TABLE decision_activation")
            raw.commit()
        finally:
            raw.close()
        self._assert_store_refusal(*self._build(), reason="missing_object")

    def test_missing_immutability_trigger_refuses(self):
        self._prepare_activated_approval()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("DROP TRIGGER decision_block_delete")
            raw.commit()
        finally:
            raw.close()
        self._assert_store_refusal(*self._build(), reason="missing_object")

    def test_missing_index_refuses(self):
        self._prepare_activated_approval()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("DROP INDEX idx_decision_source_sequence")
            raw.commit()
        finally:
            raw.close()
        self._assert_store_refusal(*self._build(), reason="missing_object")

    def test_unexpected_column_refuses(self):
        self._prepare_activated_approval()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("ALTER TABLE decision ADD COLUMN smuggled TEXT")
            raw.commit()
        finally:
            raw.close()
        # An added column changes the stored canonical DDL text, which the canonical-definition
        # comparison catches before the narrower column check runs.
        self._assert_store_refusal(*self._build(), reason="schema_object_mismatch")

    def test_failed_integrity_check_refuses(self):
        self._prepare_activated_approval()
        with mock.patch.object(decisions, "_integrity_check",
                               return_value="*** in database main *** simulated page error"):
            code, out = self._build()
        self._assert_store_refusal(code, out, reason="integrity_check_failed")

    def test_corrupted_store_refuses(self):
        # Overwritten with non-database bytes: pre-open triage sees no SQLite magic and refuses
        # before opening, so the reason names the header rather than a SQLite read error.
        self._prepare_activated_approval()
        self._store_path().write_bytes(b"this is definitely not a SQLite database\n" * 8)
        code, out = self._build()
        self._assert_store_refusal(code, out, reason="store_header_invalid")

    def test_malformed_canonical_record_hash_refuses(self):
        # A decision row whose canonical hash does not recompute was altered outside this
        # tool. The record hash is supplied by the caller, so a tampered value is inserted
        # through the real API without ever updating history.
        srid, fp = self._fixture_identity()
        record = self._record("approved", srid=srid, fingerprint=fp)
        record["record_hash"] = "sha256:" + ("0" * 64)
        self._hostile_activate(self._hostile_pending(record))
        self._assert_store_refusal(*self._build(), reason="record_hash_mismatch")

    def test_malformed_field_value_refuses_as_invalid_field_type(self):
        # A value of the right storage type but the wrong FORMAT survives the round trip
        # intact, so the format check is what catches it.
        srid, fp = self._fixture_identity()
        record = self._record("approved", srid=srid, fingerprint=fp)
        record["reviewer_id"] = "NOT A REVIEWER HANDLE"   # fails REVIEWER_ID_RE
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_activate(self._hostile_pending(record))
        self._assert_store_refusal(*self._build(), reason="invalid_field_type")

    def test_wrong_field_type_refuses(self):
        # A genuinely wrong TYPE cannot survive a TEXT column: SQLite's column affinity
        # coerces the integer to text, so the round-tripped value no longer matches the
        # canonical hash computed over the integer. Either way the row is refused fail-closed,
        # and the reason reported is the one that actually applies.
        srid, fp = self._fixture_identity()
        record = self._record("approved", srid=srid, fingerprint=fp)
        record["reviewer_id"] = 12345
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_activate(self._hostile_pending(record))
        summary = self._assert_store_refusal(*self._build(), reason="record_hash_mismatch")
        self.assertIs(summary["approval_blocked"], True)

    def test_decision_activation_mismatch_refuses(self):
        # An activation whose hash does not bind the exact decision content it claims to
        # activate cannot vouch for it.
        srid, fp = self._fixture_identity()
        record = self._insert_pending(self._record("approved", srid=srid, fingerprint=fp))
        self._activate(record, activation_hash="sha256:" + ("f" * 64))
        self._assert_store_refusal(*self._build(), reason="activation_mismatch")

    def test_store_history_cannot_be_updated_or_deleted(self):
        self._prepare_activated_approval()
        conn = self._raw()
        try:
            for statement in (
                "UPDATE decision SET reviewer_id = 'tampered'",
                "DELETE FROM decision",
                "UPDATE decision_activation SET activated_at = 'tampered'",
                "DELETE FROM decision_activation",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(statement)
        finally:
            conn.close()
        # History survives every attempt, and the build still proceeds normally.
        self.assertEqual(len(self._stored_decisions()), 1)
        self.assertEqual(self._activation_count(), 1)
        self.assertEqual(self._build()[0], 0)

    def test_store_rows_contain_no_member_data_or_credentials(self):
        self._prepare_activated_approval()
        conn = self._raw()
        try:
            dumped = "\n".join(
                "|".join("" if value is None else str(value) for value in row)
                for table in ("decision", "decision_activation", "schema_meta")
                for row in conn.execute(f"SELECT * FROM {table}")
            )
        finally:
            conn.close()
        for secret in ("90000001", "6590000001", "Synthetic Alpha",
                       "synthetic.alpha@example.invalid", "2000-01-01", "AC2_PROBE",
                       str(self.tmp)):
            self.assertNotIn(secret, dumped, f"the store must not contain {secret!r}")


class ConcurrentDecisionTests(_CreateUatBuildHarness):
    """Amendment 6 section F: two concurrent processes recording decisions for the same source
    serialize through the transaction, each committed decision gets a unique monotonic
    sequence, and no identifier is duplicated."""

    def _decide_script(self, decision):
        return textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(SCRIPTS)!r})
            import member_create_uat_approval as approval
            raise SystemExit(approval.main([
                {decision!r}, "--reviewer", "digital",
                "--input", {str(self.form)!r},
                "--decision-rows", {str(self.rows)!r},
                "--row-number", "2",
                "--ledger", {str(self.ledger)!r},
            ]))
            """
        )

    # Amendment 8 changed what "safe under concurrency" MEANS for these processes. Pre-open
    # triage refuses any store carrying a sidecar, and a peer that is mid-transaction has a
    # rollback journal, so a concurrent reviewer decision now either commits cleanly or fails
    # CLOSED - it no longer waits on the busy timeout and then commits. Both outcomes are
    # safe; what these tests pin down is that every process ends in exactly one of them, that
    # a refusal writes nothing at all, and that whatever does commit stays unique, ordered and
    # globally valid.

    # The three ways a peer that is mid-operation is observed, all fail-closed and all leaving
    # the store exactly as found:
    #   * store_sidecar_present - the peer is inside a write transaction, so its rollback
    #     journal exists and the store is controlled-recovery-only;
    #   * store_locked          - the peer holds RESERVED but has written no page yet, so the
    #     bounded busy timeout expires;
    #   * store_unreadable      - on Windows the peer's in-flight no-replace publication can
    #     make the just-appeared path briefly unopenable;
    #   * store_multiple_links  - on POSIX, first-use publication links the completed temporary
    #     to the final name and only then unlinks the temporary, so for that brief window the
    #     store legitimately has two names. A concurrent process that triages it mid-window
    #     correctly refuses. The window exists only during creation; afterwards the link count
    #     is one for good.
    #   * store_not_admitted    - Amendment 9. The loser of the creation race cleans up its own
    #     temporary and then requires the WINNER's store to be operationally admitted. Between
    #     the winner's publication and its admission commit, it is not - so the loser fails
    #     closed instead of proceeding against a store whose admission nobody has proven. This
    #     is the sticky-uncertainty property observed under real concurrency, and it leaves the
    #     winner's store byte-for-byte intact (`decision_store_modified` is false).
    #
    # Failing closed is correct in every case; retrying automatically is exactly what the
    # contract forbids.
    _CLEAN_CONTENTION_REFUSALS = (
        "store_sidecar_present",
        "store_locked",
        "store_unreadable",
        "store_multiple_links",
        "store_not_admitted",
    )

    def _assert_contention_outcome(self, returncode, out, err):
        """One concurrent process either fully succeeded or fully refused.

        Returns the process's own ``decision_id`` when it succeeded, else None. A refusal is
        never allowed to be a traceback, an unclassified error, or a partially authoritative
        decision.
        """
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        if returncode == 0:
            self.assertEqual(summary["status"], "ok")
            self.assertIs(summary["decision_activated"], True)
            return summary["decision_id"]
        self.assertEqual(returncode, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out + err)
        if summary["status"] == "decision_store_integrity_uncertain":
            self.assertIn(summary["decision_store_integrity"],
                          self._CLEAN_CONTENTION_REFUSALS, out)
            # Amendment 9: whether a new store exists is reported from the explicit final-path
            # classifier, and the two answers must agree. A blanket "nothing was modified"
            # assertion here would have been wrong: a process that legitimately publishes and
            # admits, then cannot complete its final verification because a peer is
            # mid-transaction, really did create a file and says so.
            state = summary.get("decision_store_final_path_state")
            if state is None:
                self.assertIs(summary["decision_store_modified"], False,
                              "a process that created nothing must report nothing modified")
            else:
                self.assertIn(state, decisions.FINAL_PATH_STATES, out)
                self.assertIs(
                    summary["decision_store_modified"],
                    state != decisions.COMPETITOR_PUBLISHED_UNTOUCHED,
                    "the modified flag must agree with the final-path classifier",
                )
        else:
            # The pending row committed but a later stage met the peer mid-operation. The
            # decision stays recorded-and-non-authoritative; it is never silently promoted.
            self.assertIn(summary["status"],
                          ("decision_not_recorded", "decision_not_activated",
                           "decision_audit_incomplete"), out)
            self.assertIs(summary["decision_activated"], False)
        return None

    def _assert_store_is_globally_valid(self):
        decisions.inspect_store(self._store_path())

    def _surviving_rows(self, committed):
        """The decision rows that exist, having first established that they may not.

        Liveness under contention is deliberately NOT guaranteed: because a sidecar is refused
        unconditionally, two processes that interleave badly enough can both fail closed and
        leave no store at all. What is guaranteed - and what these tests pin down - is that no
        outcome is ever partially authoritative, corrupt or ambiguous.
        """
        if not os.path.lexists(self._store_path()):
            self.assertEqual(committed, [],
                             "a reported success must leave a store behind")
            return []
        self._assert_store_is_globally_valid()
        return self._stored_decisions()

    def _assert_successes_are_activated(self, rows, committed_ids):
        """Every process that reported success owns exactly one ACTIVATED row.

        A process that refused may still have left a committed-but-unactivated pending row -
        that is the honest outcome when the peer was met after step 1 - so the row count is
        bounded, not fixed.
        """
        by_id = {row["decision_id"]: row for row in rows}
        for decision_id in committed_ids:
            self.assertIn(decision_id, by_id, "a reported success must be in the store")
            self.assertIsNotNone(by_id[decision_id]["activation_sequence"],
                                 "a reported success must be activated")
        self.assertGreaterEqual(len(rows), len(committed_ids))

    def test_two_concurrent_decision_processes_serialize_and_stay_unique(self):
        first = subprocess.Popen(
            [sys.executable, "-c", self._decide_script("approve")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        second = subprocess.Popen(
            [sys.executable, "-c", self._decide_script("hold")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        first_out, first_err = first.communicate(timeout=120)
        second_out, second_err = second.communicate(timeout=120)
        committed = [
            self._assert_contention_outcome(first.returncode, first_out, first_err),
            self._assert_contention_outcome(second.returncode, second_out, second_err),
        ]
        committed = [decision_id for decision_id in committed if decision_id is not None]

        # Whatever committed is unique, monotonically sequenced and globally canonical.
        rows = self._surviving_rows(committed)
        self._assert_successes_are_activated(rows, committed)
        sequences = [row["sequence"] for row in rows]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(set(sequences)), len(sequences), "sequences stay unique")
        self.assertEqual(len(set(row["decision_id"] for row in rows)), len(rows))
        approval_ids = [row["approval_id"] for row in rows if row["approval_id"] is not None]
        self.assertEqual(len(approval_ids), len(set(approval_ids)))
        activated = [row for row in rows if row["activation_sequence"] is not None]
        if not activated:
            # Every process failed closed. Nothing may authorise a build.
            code, out = self._build()
            self.assertNotEqual(code, 0, out)
            self.assertNotIn("Traceback", out)
            return

        # The newest ACTIVATED decision wins, and the build must agree with that ordering.
        newest = activated[-1]
        pending_after = [row for row in rows
                         if row["activation_sequence"] is None
                         and row["sequence"] > newest["sequence"]]
        code, out = self._build()
        if pending_after:
            self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        elif newest["decision_type"] == "hold":
            self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
            self.assertEqual(json.loads(out)["decision_authority"],
                             decisions.AuthorityState.HOLD)
        else:
            self.assertEqual(code, 0, out)

    def test_two_concurrent_approvals_produce_distinct_approval_ids(self):
        procs = [
            subprocess.Popen([sys.executable, "-c", self._decide_script("approve")],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        outputs = [proc.communicate(timeout=120) for proc in procs]
        committed = [
            self._assert_contention_outcome(proc.returncode, out, err)
            for proc, (out, err) in zip(procs, outputs)
        ]
        committed = [decision_id for decision_id in committed if decision_id is not None]
        rows = self._surviving_rows(committed)
        self._assert_successes_are_activated(rows, committed)
        self.assertLessEqual(len(rows), len(procs), "no process contributes two rows")
        self.assertEqual(len({row["approval_id"] for row in rows}), len(rows),
                         "no approval identifier is ever reused")
        self.assertEqual(len({row["decision_id"] for row in rows}), len(rows))
        self.assertEqual([row["sequence"] for row in rows],
                         sorted(row["sequence"] for row in rows))


class LedgerAuditSchemaTests(_CreateUatBuildHarness):
    """Amendment 6 section H: the JSONL audit ledger validates EXACT supported event schemas.

    Rejecting only malformed JSON and non-object JSON left arbitrary dictionaries trusted, so
    a malformed decision or publication dictionary reached downstream timestamp parsing and
    field lookups. Every malformed shape below now fails closed through
    ``ledger_integrity_uncertain`` with no traceback and no printed record.
    """

    def _decision_audit(self, **overrides):
        record = {
            "event": "decision",
            "recorded_at": "2026-07-25T00:00:00+00:00",
            "reviewer_id": "digital",
            "decision": "approved",
            "source_record_id": "srcrec_" + ("a" * 64),
            "source_fingerprint": "fp_" + ("b" * 64),
            "row_number_hint": 2,
            "approval_id": "appr_" + ("c" * 32),
            "approved_at": "2026-07-25T00:00:00+00:00",
            "expires_at": "2026-07-28T00:00:00+00:00",
        }
        record.update(overrides)
        for key in [k for k, v in overrides.items() if v is _DROP]:
            record.pop(key, None)
        return record

    def _build_audit(self, **overrides):
        record = {
            "event": "build",
            "recorded_at": "2026-07-25T00:00:00+00:00",
            "source_record_id": "srcrec_" + ("a" * 64),
            "source_fingerprint": "fp_" + ("b" * 64),
            "approval_id": "appr_" + ("c" * 32),
            "operation_id": "mcuat_" + ("d" * 32),
            "bound_package_payload_hash": "sha256:" + ("e" * 64),
            "package_file_name": "member_create_uat_package_v2.json",
            "reservation_file_name": "member_create_uat_reservation_appr_"
                                    + ("c" * 32) + ".1.reservation",
        }
        record.update(overrides)
        for key in [k for k, v in overrides.items() if v is _DROP]:
            record.pop(key, None)
        return record

    def _append_record(self, record):
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _assert_refused(self, record, reason):
        self._append_record(record)
        before = self.ledger.read_bytes()
        code, out = self._build()
        summary = self._assert_ledger_integrity_uncertain(code, out)
        self.assertEqual(summary["ledger_integrity"], reason)
        self.assertEqual(self.ledger.read_bytes(), before, "the ledger is never repaired")
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        return summary

    # ---- decision audit records ---- #
    def test_invalid_expires_at_refused(self):
        self._assert_refused(self._decision_audit(expires_at="not-a-timestamp"),
                             "decision_field_invalid")

    def test_invalid_approved_at_refused(self):
        self._assert_refused(self._decision_audit(approved_at="2026-13-45T99:99:99+00:00"),
                             "decision_field_invalid")

    def test_invalid_recorded_at_refused(self):
        self._assert_refused(self._decision_audit(recorded_at="yesterday"),
                             "decision_field_invalid")

    def test_missing_approval_id_on_approval_refused(self):
        self._assert_refused(self._decision_audit(approval_id=None),
                             "decision_field_invalid")

    def test_malformed_approval_id_refused(self):
        self._assert_refused(self._decision_audit(approval_id="appr_not_hex"),
                             "decision_field_invalid")

    def test_wrong_decision_value_refused(self):
        self._assert_refused(self._decision_audit(decision="maybe"),
                             "decision_field_invalid")

    def test_wrong_reviewer_type_refused(self):
        self._assert_refused(self._decision_audit(reviewer_id=12345),
                             "decision_field_invalid")

    def test_wrong_row_number_type_refused(self):
        self._assert_refused(self._decision_audit(row_number_hint="2"),
                             "decision_field_invalid")

    def test_boolean_row_number_refused(self):
        # bool is an int subclass, so it must be excluded explicitly.
        self._assert_refused(self._decision_audit(row_number_hint=True),
                             "decision_field_invalid")

    def test_missing_source_fingerprint_refused(self):
        self._assert_refused(self._decision_audit(source_fingerprint=_DROP),
                             "decision_field_set_mismatch")

    def test_malformed_source_fingerprint_refused(self):
        self._assert_refused(self._decision_audit(source_fingerprint="fp_short"),
                             "decision_field_invalid")

    def test_extra_undeclared_decision_field_refused(self):
        self._assert_refused(self._decision_audit(smuggled="payload"),
                             "decision_field_set_mismatch")

    def test_rejection_carrying_approval_fields_refused(self):
        self._assert_refused(
            self._decision_audit(decision="rejected", approved_at=None, expires_at=None),
            "decision_field_invalid",
        )

    # ---- publication audit records ---- #
    def test_unknown_event_refused(self):
        self._assert_refused(self._decision_audit(event="something_else"),
                             "unknown_event_type")

    def test_missing_event_refused(self):
        self._assert_refused(self._decision_audit(event=_DROP), "unknown_event_type")

    def test_missing_build_binding_refused(self):
        self._assert_refused(self._build_audit(operation_id=_DROP),
                             "publication_field_set_mismatch")

    def test_malformed_payload_hash_refused(self):
        self._assert_refused(self._build_audit(bound_package_payload_hash="sha256:zzz"),
                             "publication_field_invalid")

    def test_extra_undeclared_build_field_refused(self):
        self._assert_refused(self._build_audit(smuggled="payload"),
                             "publication_field_set_mismatch")

    def test_malformed_package_file_name_refused(self):
        self._assert_refused(self._build_audit(package_file_name="../escape.json"),
                             "publication_field_invalid")

    def test_cleanup_incomplete_missing_extra_fields_refused(self):
        self._assert_refused(self._build_audit(event="build_cleanup_incomplete"),
                             "publication_field_set_mismatch")

    def test_cleanup_incomplete_false_flag_refused(self):
        self._assert_refused(
            self._build_audit(event="build_cleanup_incomplete", cleanup_incomplete=False,
                              stale_temp_basename=".mcuat_pkg_x.tmp"),
            "publication_field_invalid",
        )

    # ---- the historical shapes must still be accepted ---- #
    def test_historical_build_record_without_reservation_name_is_accepted(self):
        # Pre-Amendment-4 `build` records carry no reservation_file_name. They remain valid.
        self._append_record(self._build_audit(reservation_file_name=_DROP))
        self.assertEqual(len(approval.read_ledger(self.ledger)), 1)

    def test_current_and_cleanup_incomplete_shapes_are_accepted(self):
        self._append_record(self._build_audit())
        self._append_record(self._build_audit(
            event="build_cleanup_incomplete", cleanup_incomplete=True,
            stale_temp_basename=".mcuat_pkg_abc.tmp",
        ))
        self._append_record(self._decision_audit())
        self._append_record(self._decision_audit(
            decision="hold", approval_id=None, approved_at=None, expires_at=None))
        self._append_record(self._decision_audit(
            decision="rejected", approval_id=None, approved_at=None, expires_at=None))
        self.assertEqual(len(approval.read_ledger(self.ledger)), 5)

    def test_a_real_end_to_end_run_produces_only_valid_audit_records(self):
        # The strongest schema check: everything the tool itself writes must validate.
        self.assertEqual(self._approve()[0], 0)
        self.assertEqual(self._build()[0], 0)
        with mock.patch("os.unlink", side_effect=OSError("simulated unlink failure")):
            srid, fp = self._fixture_identity()
            self._insert_activated("approved", srid=srid, fingerprint=fp)
            self._build(extra=self._fresh_out("member_create_uat_package_v2_ci.json"))
        entries = approval.read_ledger(self.ledger)
        self.assertEqual(
            sorted({e["event"] for e in entries}),
            ["build", "build_cleanup_incomplete", "decision"],
        )


class DecisionStorePreservationTests(_CreateUatBuildHarness):
    """Amendment 6 section I: every blocked path preserves its neighbours, sweeps nothing and
    leaks nothing - now including the decision store."""

    def test_blocked_paths_preserve_everything_and_leak_nothing(self):
        historical = self.tmp / "member_create_uat_package_v1.json"
        historical.write_text("HISTORICAL-V1-EVIDENCE-DO-NOT-TOUCH\n", encoding="utf-8")
        competing = self.tmp / "member_create_uat_package_v2_other.json"
        competing.write_text("COMPETING-DO-NOT-TOUCH\n", encoding="utf-8")
        unrelated_temp = self.tmp / ".mcuat_pkg_unrelated_sentinel.tmp"
        unrelated_temp.write_text("UNRELATED-DO-NOT-SWEEP\n", encoding="utf-8")
        other_reservation = self.tmp / (
            approval.RESERVATION_PREFIX + "appr_" + ("f" * 32) + ".1" + approval.RESERVATION_SUFFIX
        )
        other_reservation.write_text("UNRELATED-RESERVATION-DO-NOT-TOUCH\n", encoding="utf-8")

        self.assertEqual(self._approve()[0], 0)
        self.assertEqual(self._build()[0], 0)
        existing_v2 = self.package.read_bytes()
        ledger_bytes = self.ledger.read_bytes()
        store_bytes = self._store_path().read_bytes()
        before = {p: p.read_bytes()
                  for p in (historical, competing, unrelated_temp, other_reservation)}

        def no_sweep(*args, **kwargs):
            raise AssertionError("no build path may list, scan or glob a directory")

        outputs = []

        def blocked_build(name, expected_code, extra=None):
            """Run one blocked build and prove it mutated neither the store nor the ledger.

            The snapshot is taken immediately before each build so that state this TEST sets
            up between builds is never mistaken for a mutation by the tool.
            """
            store_before = self._store_path().read_bytes()
            ledger_before = self.ledger.read_bytes()
            code, out = self._build(extra=(extra or []) + self._fresh_out(name))
            self.assertEqual(code, expected_code, out)
            self.assertEqual(self._store_path().read_bytes(), store_before,
                             "a blocked build must not mutate the decision store")
            self.assertEqual(self.ledger.read_bytes(), ledger_before,
                             "a blocked build must not touch the audit ledger")
            outputs.append(out)

        with mock.patch("os.listdir", no_sweep), mock.patch("os.scandir", no_sweep), \
                mock.patch("glob.glob", no_sweep), \
                mock.patch.object(Path, "iterdir", no_sweep), \
                mock.patch.object(Path, "glob", no_sweep):
            # Terminally consumed approval.
            blocked_build("blocked_consumed.json", approval.EXIT_APPROVAL_CONSUMED)
            # Retired --rebuild.
            blocked_build("blocked_rebuild.json", approval.EXIT_APPROVAL_CONSUMED,
                          extra=["--rebuild"])
            # Newer pending decision (set up by the test, outside the assertions above).
            srid, fp = self._fixture_identity()
            self._insert_pending(self._record("hold", srid=srid, fingerprint=fp))
            blocked_build("blocked_pending.json", approval.EXIT_DECISION_NOT_AUTHORITATIVE)

        # Every neighbour, the existing v2 package and the audit ledger are byte-for-byte
        # unchanged across the whole sequence.
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original, f"{path.name} must be untouched")
        self.assertEqual(self.package.read_bytes(), existing_v2)
        self.assertEqual(self.ledger.read_bytes(), ledger_bytes,
                         "the audit ledger is never truncated, rewritten or replaced")
        # The store grew only by the pending row this test inserted; its committed history is
        # append-only and no earlier row changed.
        self.assertGreaterEqual(len(self._stored_decisions()), 2)
        self.assertNotEqual(self._store_path().read_bytes(), store_bytes,
                            "the test's own pending insert is the only store change")
        for name in ("blocked_consumed.json", "blocked_rebuild.json", "blocked_pending.json"):
            self.assertFalse((self.tmp / name).exists())
        combined = "".join(outputs)
        for leaked in ("90000001", "6590000001", "Synthetic Alpha",
                       "synthetic.alpha@example.invalid", "2000-01-01", "AC2_PROBE",
                       str(self.tmp), str(self._store_path()), "HISTORICAL-V1-EVIDENCE"):
            self.assertNotIn(leaked, combined, f"the output must not contain {leaked!r}")


class _ClaimHarness(_CreateUatBuildHarness):
    """Shared helpers for the Amendment 7 build-claim suites."""

    def _claims(self):
        return self._read_store(
            lambda conn: conn.execute(
                "SELECT * FROM build_claim ORDER BY claim_sequence"
            ).fetchall()
        )

    def _claim_count(self):
        return len(self._claims())

    def _canonical_store(self):
        return self._create_store_for_fixture()

    def _assert_refused_untouched(self, reason=None, *, decision_command=True):
        """Every hostile existing store is refused, and the WHOLE fixture is preserved.

        Amendment 8 asserts the complete snapshot - main-file bytes and identity, every exact
        sidecar's bytes and type, and the bounded fixture directory's entries - through the
        read-only inspection path, a reviewer decision command, `build-package` and all three
        COMMIT-recovery entry points.
        """
        path = self._store_path()
        before = self._store_snapshot()

        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.inspect_store(path)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertIn(caught.exception.reason, decisions.STORE_INTEGRITY_REASONS)
        self.assertEqual(self._store_snapshot(), before, "inspection must change nothing")

        # Every COMMIT-recovery entry point must refuse the same way, reporting UNCERTAIN
        # rather than a false ABSENT that would wrongly permit a retry.
        for recover, args in (
            (decisions.recover_decision_commit, ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
            (decisions.recover_activation_commit, ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
            (decisions.recover_claim_commit, ("claim_" + ("0" * 32), "sha256:" + ("0" * 64))),
        ):
            with self.subTest(recovery=recover.__name__):
                state, row = recover(path, *args)
                self.assertEqual(state, decisions.CommitState.UNCERTAIN)
                self.assertIsNone(row)
                self.assertEqual(self._store_snapshot(), before,
                                 "recovery must change nothing")

        if decision_command:
            code, out = self._approve()
            self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
            self.assertNotIn("Traceback", out)
            self.assertEqual(json.loads(out)["status"], "decision_store_integrity_uncertain")
            self.assertEqual(self._store_snapshot(), before,
                             "a reviewer decision must change nothing")

        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertIs(summary["decision_store_modified"], False)
        self.assertEqual(self._store_snapshot(), before, "a build must change nothing")
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._stray_temps(), [])

    def _raw_claim_count(self):
        """Claim count on a RAW connection, so a deliberately invalid store can still be
        inspected without tripping canonical validation."""
        raw = self._raw()
        try:
            return raw.execute("SELECT COUNT(*) FROM build_claim").fetchone()[0]
        finally:
            raw.close()

    def _store_approval_id(self):
        """The newest approval id from the STORE. These fixtures seed the store directly, so the
        JSONL ledger may hold no decision line at all."""
        approved = [row for row in self._stored_decisions() if row["approval_id"]]
        return approved[-1]["approval_id"]

    def _store_slot(self, attempt=1):
        return approval.reservation_path(self.tmp, self._store_approval_id(), attempt)

    def _prepare_activated_approval(self):
        srid, fingerprint = self._fixture_identity()
        return self._insert_activated("approved", srid=srid, fingerprint=fingerprint)

    def _assert_nothing_created(self, *names):
        self.assertEqual(self._claim_count(), 0, "no build claim may exist")
        self.assertEqual(self._reservations(), [], "no reservation may exist")
        self.assertEqual(self._stray_temps(), [], "no temporary may exist")
        self.assertEqual(self._events_of("build"), [], "no build audit event may exist")
        self.assertEqual(self._events_of("build_cleanup_incomplete"), [])
        for name in names:
            self.assertFalse((self.tmp / name).exists(), f"{name} must not exist")


class BuildVersusReviewerDecisionTests(_ClaimHarness):
    """Amendment 7 P1: the build-authorisation time-of-check/time-of-use window is closed.

    The build re-resolves authority and inserts its exclusive claim inside ONE
    ``BEGIN IMMEDIATE``, on the same serialisation boundary the decision writers use. These
    tests pause the build at ``_pre_claim_barrier`` - after its whole non-mutating preflight,
    immediately before the claim transaction - and commit a reviewer decision at that instant.
    """

    def _build_with_barrier(self, inject, out_name="toctou.json"):
        fired = {"n": 0}

        def barrier():
            fired["n"] += 1
            inject()

        with mock.patch.object(approval, "_pre_claim_barrier", barrier):
            code, out = self._build(extra=self._fresh_out(out_name))
        self.assertEqual(fired["n"], 1, "the barrier must be reached exactly once")
        return code, out

    def _assert_superseded(self, code, out):
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_superseded_before_claim")
        self.assertEqual(summary["build_claim"], "not_created")
        self.assertEqual(summary["publication"], "not_attempted")
        self.assertEqual(summary["reservation"], "not_attempted")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        return summary

    def test_activated_hold_committed_at_the_barrier_blocks_the_build(self):
        srid, fingerprint = self._fixture_identity()
        self._prepare_activated_approval()
        code, out = self._build_with_barrier(
            lambda: self._insert_activated("hold", srid=srid, fingerprint=fingerprint)
        )
        summary = self._assert_superseded(code, out)
        self.assertEqual(summary["decision_authority"], decisions.AuthorityState.HOLD)
        self._assert_nothing_created("toctou.json")

    def test_activated_rejection_committed_at_the_barrier_blocks_the_build(self):
        srid, fingerprint = self._fixture_identity()
        self._prepare_activated_approval()
        code, out = self._build_with_barrier(
            lambda: self._insert_activated("rejected", srid=srid, fingerprint=fingerprint)
        )
        summary = self._assert_superseded(code, out)
        self.assertEqual(summary["decision_authority"], decisions.AuthorityState.REJECTED)
        self._assert_nothing_created("toctou.json")

    def test_pending_hold_committed_at_the_barrier_blocks_the_build(self):
        srid, fingerprint = self._fixture_identity()
        self._prepare_activated_approval()
        code, out = self._build_with_barrier(
            lambda: self._insert_pending(self._record("hold", srid=srid, fingerprint=fingerprint))
        )
        summary = self._assert_superseded(code, out)
        self.assertEqual(summary["decision_authority"], decisions.AuthorityState.PENDING_NEWER)
        self.assertGreaterEqual(summary["newer_pending_decisions"], 1)
        self._assert_nothing_created("toctou.json")

    def test_pending_rejection_committed_at_the_barrier_blocks_the_build(self):
        srid, fingerprint = self._fixture_identity()
        self._prepare_activated_approval()
        code, out = self._build_with_barrier(
            lambda: self._insert_pending(
                self._record("rejected", srid=srid, fingerprint=fingerprint)
            )
        )
        self._assert_superseded(code, out)
        self._assert_nothing_created("toctou.json")

    def test_no_state_beyond_the_injected_decision_is_mutated(self):
        srid, fingerprint = self._fixture_identity()
        self._prepare_activated_approval()
        ledger_before = self.ledger.read_bytes() if self.ledger.exists() else None
        code, _out = self._build_with_barrier(
            lambda: self._insert_activated("hold", srid=srid, fingerprint=fingerprint)
        )
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE)
        after = self.ledger.read_bytes() if self.ledger.exists() else None
        self.assertEqual(after, ledger_before,
                         "the audit ledger is untouched by a superseded build")
        self.assertEqual(len(self._stored_decisions()), 2)
        self.assertEqual(self._activation_count(), 2)
        self.assertEqual(self._claim_count(), 0)

    # ---- reverse order: the claim wins, and a later decision cannot cancel it ---- #
    def test_claim_first_then_activated_hold_leaves_the_claim_and_blocks_later_builds(self):
        srid, fingerprint = self._fixture_identity()
        self._prepare_activated_approval()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        published = self.package.read_bytes()
        claims = self._claims()
        self.assertEqual(len(claims), 1)
        claim_id = claims[0]["claim_id"]

        # A reviewer hold committed AFTER the claim governs subsequent work only. It does not
        # retroactively release or cancel the committed claim, and it deletes nothing.
        self._insert_activated("hold", srid=srid, fingerprint=fingerprint)
        self.assertEqual(self._claim_count(), 1)
        self.assertEqual(self._claims()[0]["claim_id"], claim_id)
        self.assertEqual(self.package.read_bytes(), published)

        code, out = self._build(extra=self._fresh_out("after_hold.json"))
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertEqual(json.loads(out)["decision_authority"], decisions.AuthorityState.HOLD)
        self.assertFalse((self.tmp / "after_hold.json").exists())
        self.assertEqual(self._claim_count(), 1)

    def test_claim_first_then_activated_rejection_cannot_cancel_the_claim(self):
        srid, fingerprint = self._fixture_identity()
        self._prepare_activated_approval()
        self.assertEqual(self._build()[0], 0)
        claim_before = dict(self._claims()[0])
        self._insert_activated("rejected", srid=srid, fingerprint=fingerprint)
        self.assertEqual(dict(self._claims()[0]), claim_before,
                         "the committed claim is immutable and is never retroactively cancelled")
        code, out = self._build(extra=self._fresh_out("after_reject.json"))
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertFalse((self.tmp / "after_reject.json").exists())

    def test_the_claimed_attempt_completes_exactly_once(self):
        self._prepare_activated_approval()
        self.assertEqual(self._build()[0], 0)
        self.assertEqual(len(self._events_of("build")), 1)
        code, out = self._build(extra=self._fresh_out("second.json"))
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        self.assertEqual(json.loads(out)["build_claim"], "already_committed")
        self.assertFalse((self.tmp / "second.json").exists())
        self.assertEqual(self._claim_count(), 1)
        self.assertEqual(len(self._events_of("build")), 1)


class CompetingBuildClaimTests(_ClaimHarness):
    """Amendment 7: exactly one build claim may commit, and only the winner may publish."""

    def _build_script(self, out_name):
        return textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(SCRIPTS)!r})
            import member_create_uat_approval as approval
            raise SystemExit(approval.main([
                "build-package",
                "--input", {str(self.form)!r},
                "--decision-rows", {str(self.rows)!r},
                "--row-number", "2",
                "--ledger", {str(self.ledger)!r},
                "--package-out", {str(self.tmp / out_name)!r},
            ]))
            """
        )

    def test_two_real_processes_race_and_exactly_one_claim_commits(self):
        self._prepare_activated_approval()
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", self._build_script(f"race_{index}.json")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for index in range(2)
        ]
        results = [proc.communicate(timeout=180) for proc in procs]
        codes = [proc.returncode for proc in procs]
        for out, err in results:
            self.assertNotIn("Traceback", err, out + err)

        self.assertEqual(self._claim_count(), 1, f"codes={codes}")
        packages = [n for n in (f"race_{i}.json" for i in range(2)) if (self.tmp / n).exists()]
        self.assertLessEqual(len(packages), 1, "at most one package may be published")
        self.assertEqual(len(self._events_of("build")), len(packages))
        self.assertEqual(len(self._reservations()), len(packages))
        self.assertEqual(self._stray_temps(), [])
        self.assertIn(0, codes, f"one process must win: codes={codes}")
        loser = [code for code in codes if code != 0]
        self.assertEqual(len(loser), 1)
        # Amendment 8 adds a third safe way for the loser to be turned away. Under the locked
        # sidecar rule, meeting the winner's rollback journal refuses the loser on pure
        # pre-open triage - before it can claim, reserve, publish or write anything - which is
        # exit 9 rather than the claim-time exits 7 and 10. The invariants above already
        # proved the loser created nothing.
        self.assertIn(
            loser[0],
            (approval.EXIT_APPROVAL_CONSUMED,
             approval.EXIT_DECISION_NOT_AUTHORITATIVE,
             approval.EXIT_DECISION_AUTHORITY_UNCERTAIN),
            f"codes={codes}",
        )
        if loser[0] == approval.EXIT_DECISION_AUTHORITY_UNCERTAIN:
            refusals = [json.loads(out) for out, _err in results
                        if out.strip().startswith("{")
                        and json.loads(out).get("status") != "ok"]
            self.assertEqual(len(refusals), 1, f"codes={codes}")
            summary = refusals[0]
            self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
            self.assertIn(summary["decision_store_integrity"],
                          ("store_sidecar_present", "store_locked", "store_unreadable"))
            self.assertIs(summary["decision_store_modified"], False)

    def test_no_approval_can_carry_two_claims_and_no_operation_id_is_reused(self):
        self._prepare_activated_approval()
        self.assertEqual(self._build()[0], 0)
        claim = self._claims()[0]
        conn = self._raw()
        try:
            for column in ("approval_id", "claim_id", "decision_id", "operation_id"):
                with self.subTest(duplicate=column):
                    record = {key: claim[key] for key in decisions.CLAIM_HASH_FIELDS}
                    if column != "claim_id":
                        record["claim_id"] = "claim_" + uuid.uuid4().hex
                    if column != "operation_id":
                        record["operation_id"] = "mcuat_" + uuid.uuid4().hex
                    record[column] = claim[column]
                    record["record_hash"] = decisions.claim_record_hash(record)
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute("BEGIN IMMEDIATE")
                        decisions.insert_build_claim(conn, record)
                    decisions._rollback_quietly(conn)
        finally:
            conn.close()
        self.assertEqual(self._claim_count(), 1)

    def test_no_directory_listing_globbing_or_sweep_occurs_during_a_claimed_build(self):
        self._prepare_activated_approval()

        def no_sweep(*args, **kwargs):
            raise AssertionError("a build must never list, scan or glob a directory")

        with mock.patch("os.listdir", no_sweep), mock.patch("os.scandir", no_sweep), \
                mock.patch("glob.glob", no_sweep), \
                mock.patch.object(Path, "iterdir", no_sweep), \
                mock.patch.object(Path, "glob", no_sweep):
            code, out = self._build()
        self.assertEqual(code, 0, out)


class ClaimTransactionRecoveryTests(_ClaimHarness):
    """Amendment 7: a claim ``COMMIT`` exception is resolved by REOPENING the database.

    ``build-package`` performs exactly one ``decisions._commit`` - the claim's - so the seam is
    unambiguous. The exception is never used to infer the outcome.
    """

    def _commit_before(self):
        def wrapper(conn):
            raise sqlite3.OperationalError("simulated failure before the claim commit")
        return wrapper

    def _commit_then_raise(self):
        real_commit = decisions._commit

        def wrapper(conn):
            real_commit(conn)  # the claim genuinely commits ...
            raise sqlite3.OperationalError("simulated failure after a successful claim commit")
        return wrapper

    def test_commit_raises_before_committing_leaves_no_claim_and_stays_retryable(self):
        self._prepare_activated_approval()
        with mock.patch.object(decisions, "_commit", self._commit_before()):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_BUILD_CLAIM_NOT_RECORDED, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "build_claim_not_recorded")
        self.assertEqual(summary["build_claim"], "not_created")
        self.assertIs(summary["approval_blocked"], False)
        self.assertIs(summary["do_not_retry"], False)
        self._assert_nothing_created()
        self.assertFalse(self.package.exists())
        # A later EXPLICIT retry is allowed and succeeds.
        code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertEqual(self._claim_count(), 1)

    def test_commit_succeeds_then_raises_is_recovered_as_committed(self):
        self._prepare_activated_approval()
        with mock.patch.object(decisions, "_commit", self._commit_then_raise()):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["build_claim"], "committed")
        self.assertTrue(self.package.is_file())
        self.assertEqual(self._claim_count(), 1)
        self.assertEqual(self._claims()[0]["claim_id"], summary["claim_id"])

    def test_recovery_uses_a_new_connection(self):
        self._prepare_activated_approval()
        opens = {"n": 0}
        real_connect = decisions._connect_uri

        def counting_open(path, query):
            opens["n"] += 1
            return real_connect(path, query)

        with mock.patch.object(decisions, "_commit", self._commit_then_raise()), \
                mock.patch.object(decisions, "_connect_uri", counting_open):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(opens["n"], 3, "the store is reopened for claim recovery")

    def test_recovery_that_cannot_read_the_database_fails_closed(self):
        self._prepare_activated_approval()
        with mock.patch.object(decisions, "_commit", self._commit_then_raise()), \
                mock.patch.object(decisions, "fetch_claim",
                                  side_effect=sqlite3.DatabaseError("simulated unreadable store")):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "build_claim_uncertain")
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["controlled_recovery_required"], True)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._stray_temps(), [])

    def test_recovery_finding_a_mismatched_canonical_hash_fails_closed(self):
        self._prepare_activated_approval()

        def mismatched(conn, claim_id):
            return {"record_hash": "sha256:" + ("0" * 64)}

        with mock.patch.object(decisions, "_commit", self._commit_then_raise()), \
                mock.patch.object(decisions, "fetch_claim", mismatched):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertEqual(json.loads(out)["status"], "build_claim_uncertain")
        self.assertFalse(self.package.exists())

    def test_recovery_finding_a_binding_mismatch_fails_closed(self):
        self._prepare_activated_approval()
        real_fetch = decisions.fetch_claim

        def tampered(conn, claim_id):
            row = real_fetch(conn, claim_id)
            if row is None:
                return None
            # The canonical hash still matches, so recovery accepts the row as committed; the
            # explicit per-field binding re-check is what catches the substitution.
            record = {key: row[key] for key in decisions.CLAIM_HASH_FIELDS}
            record["record_hash"] = row["record_hash"]
            record["operation_id"] = "mcuat_" + ("e" * 32)
            return record

        with mock.patch.object(decisions, "_commit", self._commit_then_raise()), \
                mock.patch.object(decisions, "fetch_claim", tampered):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "build_claim_uncertain")
        self.assertEqual(summary["claim_detail"], "claim_binding_mismatch")
        self.assertFalse(self.package.exists())

    def test_lock_contention_makes_exactly_one_bounded_attempt(self):
        self._prepare_activated_approval()
        attempts = {"n": 0}
        real_insert = decisions.insert_build_claim

        def counting_insert(conn, record):
            attempts["n"] += 1
            return real_insert(conn, record)

        blocker = self._raw()
        try:
            # RESERVED held with no page written: no rollback journal, so the bounded busy
            # timeout - not pre-open triage - decides the contention.
            blocker.execute("BEGIN IMMEDIATE")
            self.assertFalse(any(os.path.lexists(p) for p in self._sidecar_paths()))
            with mock.patch.object(decisions, "BUSY_TIMEOUT_MS", 200), \
                    mock.patch.object(decisions, "insert_build_claim", counting_insert):
                code, out = self._build()
        finally:
            decisions._rollback_quietly(blocker)
            blocker.close()
        self.assertEqual(code, approval.EXIT_BUILD_CLAIM_NOT_RECORDED, out)
        self.assertEqual(json.loads(out)["claim_detail"], "lock_contended")
        self.assertEqual(attempts["n"], 0, "the claim insert is never reached")
        self._assert_nothing_created()
        self.assertFalse(self.package.exists())

    def test_a_concurrent_writers_journal_blocks_the_build_before_sqlite_opens(self):
        # Amendment 8: once a concurrent writer has written a page, its rollback journal makes
        # the store controlled-recovery-only. The build refuses on pure triage, claims nothing,
        # and leaves the journal byte-for-byte intact.
        self._prepare_activated_approval()
        blocker = self._raw()
        try:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('lock_probe', 'held')"
            )
            journal = Path(str(self._store_path()) + "-journal")
            self.assertTrue(journal.exists())
            before = journal.read_bytes()
            code, out = self._build()
            self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
            summary = json.loads(out)
            self.assertEqual(summary["decision_store_integrity"], "store_sidecar_present")
            self.assertIs(summary["decision_store_modified"], False)
            self.assertNotIn("Traceback", out)
            self.assertEqual(journal.read_bytes(), before)
        finally:
            decisions._rollback_quietly(blocker)
            blocker.close()
        self.assertEqual(self._raw_claim_count(), 0)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._stray_temps(), [])


class PostClaimFailureTests(_ClaimHarness):
    """Amendment 7: EVERY failure after the claim commits leaves the approval consumed.

    The claim - not the reservation, not the ledger - is the terminal consumption fact, so no
    post-claim failure at any stage may release the approval or permit a second package.
    """

    STAGES = (
        "mkstemp", "temp_write", "temp_flush", "temp_fsync",
        "reservation_create", "reservation_write", "reservation_flush", "reservation_fsync",
        "reservation_directory_durability", "reservation_path_safety",
        "publication", "temp_cleanup",
        "ledger_open", "ledger_write", "ledger_flush", "ledger_fsync",
        "ledger_visible_then_unconfirmed", "ledger_partial_append",
    )

    def _stage_contexts(self, stage):
        """Real failure injections, one per materially different post-claim stage.

        Returns ``(contexts, expected_exit, published)``.
        """
        real_fdopen = os.fdopen
        real_fsync = os.fsync
        real_open = os.open

        def nth_fdopen(target, mode):
            calls = {"n": 0}

            def guarded(fd, *args, **kwargs):
                calls["n"] += 1
                handle = real_fdopen(fd, *args, **kwargs)
                return _LostAppendHandle(handle, mode) if calls["n"] == target else handle

            return mock.patch("os.fdopen", guarded)

        def nth_fsync(target):
            calls = {"n": 0}

            def guarded(fd):
                calls["n"] += 1
                if calls["n"] == target:
                    raise OSError(f"simulated fsync failure at call {target}")
                return real_fsync(fd)

            return mock.patch("os.fsync", guarded)

        def reservation_create_failure():
            def guarded(path, flags, *args, **kwargs):
                if str(path).endswith(approval.RESERVATION_SUFFIX):
                    raise OSError("simulated reservation create failure")
                return real_open(path, flags, *args, **kwargs)

            return (mock.patch("os.open", guarded),)

        def directory_durability_failure():
            # Amendment 9: scoped to the EXACT reservation call site - a PATHNAME open of the
            # reservation state directory carrying no directory descriptor - so the decision
            # store's trusted-parent traversal, exclusive creation, publication and admission all
            # run normally. A blanket O_DIRECTORY injection would instead refuse with
            # store_parent_untrusted before this stage was ever reached.
            dir_flag = getattr(os, "O_DIRECTORY", 0x10000)
            reservation_dir = os.path.abspath(str(self.tmp))

            def guarded(path, flags, *args, **kwargs):
                if (flags & dir_flag
                        and kwargs.get("dir_fd") is None
                        and os.path.abspath(str(path)) == reservation_dir):
                    raise OSError("simulated reservation directory durability failure")
                return real_open(path, flags, *args, **kwargs)

            return (
                mock.patch.object(os, "O_DIRECTORY", dir_flag, create=True),
                mock.patch("os.open", guarded),
            )

        def reservation_path_unsafe():
            slot = self._store_slot()
            real_detector = contract.is_reparse_point

            def detector(path):
                return str(path) == str(slot) or real_detector(path)

            return (mock.patch.object(contract, "is_reparse_point", detector),)

        blocked = approval.EXIT_PUBLICATION_BLOCKED
        ledger_incomplete = approval.EXIT_LEDGER_RECORD_INCOMPLETE
        # Lazy: only the requested stage is constructed, so one stage's setup can never disturb
        # (or depend on state absent for) another.
        table = {
            "mkstemp": lambda: ((mock.patch(
                "tempfile.mkstemp", side_effect=OSError("simulated mkstemp failure")),),
                blocked, False),
            "temp_write": lambda: ((nth_fdopen(1, "write"),), blocked, False),
            "temp_flush": lambda: ((nth_fdopen(1, "flush"),), blocked, False),
            "temp_fsync": lambda: ((nth_fsync(1),), blocked, False),
            "reservation_create": lambda: (reservation_create_failure(), blocked, False),
            "reservation_write": lambda: ((nth_fdopen(2, "write"),), blocked, False),
            "reservation_flush": lambda: ((nth_fdopen(2, "flush"),), blocked, False),
            "reservation_fsync": lambda: ((nth_fsync(2),), blocked, False),
            "reservation_directory_durability": lambda: (
                directory_durability_failure(), blocked, False),
            "reservation_path_safety": lambda: (reservation_path_unsafe(), blocked, False),
            "publication": lambda: ((mock.patch(
                "os.link", side_effect=OSError("simulated publication failure")),), blocked, False),
            "temp_cleanup": lambda: ((mock.patch(
                "os.unlink", side_effect=OSError("simulated unlink failure")),),
                approval.EXIT_CLEANUP_INCOMPLETE, True),
            "ledger_open": lambda: (
                (_LedgerAppendFailure(self.ledger, "open"),), ledger_incomplete, True),
            "ledger_write": lambda: (
                (_LedgerAppendFailure(self.ledger, "write"),), ledger_incomplete, True),
            "ledger_flush": lambda: (
                (_LedgerAppendFailure(self.ledger, "flush"),), ledger_incomplete, True),
            "ledger_fsync": lambda: (
                (_LedgerAppendFailure(self.ledger, "fsync"),), ledger_incomplete, True),
            "ledger_visible_then_unconfirmed": lambda: (
                (_VisibleLedgerAppendFailure(self.ledger, "fsync"),), ledger_incomplete, True),
            "ledger_partial_append": lambda: (
                (_PartialLedgerAppendFailure(self.ledger, "write"),), ledger_incomplete, True),
        }
        return table[stage]()

    def test_every_post_claim_failure_permanently_consumes_the_approval(self):
        import contextlib

        for stage in self.STAGES:
            with self.subTest(stage=stage):
                self._reset()
                historical = self.tmp / "member_create_uat_package_v1.json"
                historical.write_text("HISTORICAL-V1-DO-NOT-TOUCH\n", encoding="utf-8")
                historical_bytes = historical.read_bytes()
                self._prepare_activated_approval()

                contexts, expected_exit, published = self._stage_contexts(stage)
                with contextlib.ExitStack() as stack:
                    for context in contexts:
                        stack.enter_context(context)
                    code, out = self._build()
                self.assertEqual(code, expected_exit, f"{stage}: {out}")
                self.assertNotIn("Traceback", out)

                # The claim survives and the approval is consumed.
                self.assertEqual(self._claim_count(), 1, f"{stage}: the claim must survive")
                claim_before = dict(self._claims()[0])
                self.assertEqual(self.package.is_file(), published, stage)
                package_bytes = self.package.read_bytes() if published else None

                # A second build at a FRESH path is blocked and adds no claim.
                second = f"second_{stage}.json"
                code2, out2 = self._build(extra=self._fresh_out(second))
                self.assertIn(
                    code2,
                    (approval.EXIT_APPROVAL_CONSUMED, approval.EXIT_LEDGER_INTEGRITY_UNCERTAIN),
                    f"{stage}: {out2}",
                )
                self.assertNotIn("Traceback", out2)
                self.assertFalse((self.tmp / second).exists(), stage)
                self.assertEqual(self._claim_count(), 1, f"{stage}: no second claim")
                self.assertEqual(dict(self._claims()[0]), claim_before,
                                 f"{stage}: the claim is immutable")
                self.assertEqual(historical.read_bytes(), historical_bytes, stage)
                if published:
                    self.assertEqual(self.package.read_bytes(), package_bytes, stage)


class ExistingStoreHostilityTests(_ClaimHarness):
    """Amendment 7: an existing store is NEVER given DDL, augmented, migrated or repaired.

    Every case is refused byte-for-byte untouched, with a sanitised reason and no traceback, and
    nothing is created.
    """

    def test_zero_byte_file_is_refused_untouched(self):
        # Amendment 8 decides this from the bytes: there is no 100-byte SQLite header, so the
        # file is refused before SQLite is opened at all. (SQLite itself would have opened a
        # zero-byte file happily and even reported `integrity_check = ok`.)
        self._store_path().write_bytes(b"")
        self._assert_refused_untouched(reason="store_header_invalid")

    def test_empty_sqlite_database_is_refused_untouched(self):
        # A connect-and-close leaves a zero-byte file with no header, so pre-open triage
        # refuses it. A database with a real header but no canonical objects is covered by
        # test_headered_empty_database_is_refused_untouched below.
        sqlite3.connect(str(self._store_path())).close()
        self._assert_refused_untouched(reason="store_header_invalid")

    def test_headered_empty_database_is_refused_untouched(self):
        # A genuinely initialised but object-less database DOES pass triage, so the canonical
        # object-set check is what refuses it - proving triage did not mask that stage.
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("PRAGMA journal_mode=DELETE")
            raw.execute("CREATE TABLE placeholder (id INTEGER PRIMARY KEY)")
            raw.execute("DROP TABLE placeholder")
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched(reason="missing_object")

    def test_v1_store_is_refused_untouched(self):
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY NOT NULL, "
                        "value TEXT NOT NULL)")
            raw.execute("INSERT INTO schema_meta (key, value) VALUES "
                        "('schema_version', 'member_create_uat_decisions/v1')")
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched()

    def test_missing_schema_meta_is_refused_untouched(self):
        self._canonical_store()
        raw = self._raw()
        try:
            raw.execute("DROP TABLE schema_meta")
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched(reason="missing_object")

    def test_partial_schema_is_refused_untouched(self):
        self._canonical_store()
        raw = self._raw()
        try:
            raw.execute("DROP TABLE build_claim")
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched(reason="missing_object")

    def test_foreign_schema_is_refused_untouched(self):
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("CREATE TABLE unrelated_service (id INTEGER PRIMARY KEY, blob TEXT)")
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched()

    def _replace_object(self, drop, create):
        self._canonical_store()
        raw = self._raw()
        try:
            raw.execute(drop)
            raw.execute(create)
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched(reason="schema_object_mismatch")

    def test_no_op_expected_name_trigger_is_refused(self):
        self._replace_object(
            "DROP TRIGGER decision_block_update",
            "CREATE TRIGGER decision_block_update BEFORE UPDATE ON decision BEGIN SELECT 1; END",
        )

    def test_trigger_on_the_wrong_table_is_refused(self):
        self._replace_object(
            "DROP TRIGGER decision_block_update",
            "CREATE TRIGGER decision_block_update BEFORE UPDATE ON decision_activation "
            "BEGIN SELECT RAISE(ABORT, 'x'); END",
        )

    def test_trigger_with_the_wrong_event_is_refused(self):
        self._replace_object(
            "DROP TRIGGER decision_block_delete",
            "CREATE TRIGGER decision_block_delete BEFORE INSERT ON decision "
            "BEGIN SELECT RAISE(ABORT, 'x'); END",
        )

    def test_trigger_with_the_wrong_timing_is_refused(self):
        self._replace_object(
            "DROP TRIGGER build_claim_block_update",
            "CREATE TRIGGER build_claim_block_update AFTER UPDATE ON build_claim "
            "BEGIN SELECT RAISE(ABORT, 'x'); END",
        )

    def test_trigger_with_the_wrong_body_is_refused(self):
        self._replace_object(
            "DROP TRIGGER build_claim_require_exact_activated_approval",
            "CREATE TRIGGER build_claim_require_exact_activated_approval "
            "BEFORE INSERT ON build_claim BEGIN SELECT 1; END",
        )

    def test_index_with_wrong_columns_is_refused(self):
        self._replace_object(
            "DROP INDEX idx_decision_source_sequence",
            "CREATE INDEX idx_decision_source_sequence ON decision (decision_id)",
        )

    def test_index_with_wrong_uniqueness_is_refused(self):
        self._replace_object(
            "DROP INDEX idx_build_claim_source_sequence",
            "CREATE UNIQUE INDEX idx_build_claim_source_sequence ON build_claim "
            "(source_record_id, claim_sequence)",
        )

    def test_table_missing_unique_check_or_foreign_key_is_refused(self):
        # Rebuilding build_claim without its UNIQUE, CHECK and foreign-key protections changes
        # the stored canonical definition, so the whole class of weakened redefinitions refuses.
        self._canonical_store()
        raw = self._raw()
        try:
            raw.execute("DROP TABLE build_claim")
            raw.execute(
                "CREATE TABLE build_claim ("
                " claim_sequence INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL,"
                " decision_sequence INTEGER NOT NULL, decision_id TEXT NOT NULL,"
                " decision_record_hash TEXT NOT NULL, approval_id TEXT NOT NULL,"
                " source_record_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL,"
                " operation_id TEXT NOT NULL, package_payload_hash TEXT NOT NULL,"
                " package_file_name TEXT NOT NULL, claimed_at TEXT NOT NULL,"
                " schema_version TEXT NOT NULL, record_hash TEXT NOT NULL)"
            )
            # DROP TABLE also drops its index and triggers, so every canonical build_claim
            # object EXCEPT the table itself is restored verbatim: the weakened TABLE definition
            # is then the only remaining difference.
            for statement in decisions._SCHEMA_STATEMENTS:
                normalised = " ".join(statement.split())
                if "build_claim" in normalised and not normalised.startswith("CREATE TABLE"):
                    raw.execute(statement)
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched(reason="schema_object_mismatch")

    def _add_object(self, statement):
        self._canonical_store()
        raw = self._raw()
        try:
            raw.execute(statement)
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched(reason="unexpected_object")

    def test_extra_table_is_refused(self):
        self._add_object("CREATE TABLE smuggled (x TEXT)")

    def test_extra_view_is_refused(self):
        self._add_object("CREATE VIEW smuggled_view AS SELECT 1 AS x")

    def test_extra_trigger_is_refused(self):
        self._add_object(
            "CREATE TRIGGER smuggled_trigger AFTER INSERT ON decision BEGIN SELECT 1; END"
        )

    def test_extra_index_is_refused(self):
        self._add_object("CREATE INDEX smuggled_index ON decision (reviewer_id)")

    def test_orphan_activation_is_refused(self):
        # A raw writer has foreign_keys OFF, so it can insert an activation pointing nowhere.
        # PRAGMA foreign_key_check catches it before any authority is read.
        self._canonical_store()
        raw = self._raw()
        try:
            raw.execute(
                "INSERT INTO decision_activation (decision_id, activated_at, record_hash) "
                "VALUES (?, ?, ?)",
                ("dec_" + ("f" * 32), "2026-07-25T00:00:00+00:00", "sha256:" + ("0" * 64)),
            )
            raw.commit()
        finally:
            raw.close()
        self._assert_refused_untouched(reason="foreign_key_check_failed")

    def test_a_claim_on_a_non_approved_or_unactivated_decision_is_mechanically_impossible(self):
        # This state cannot be constructed while the canonical schema is intact: the BEFORE
        # INSERT trigger fires even for a raw foreign writer with foreign keys off.
        self._canonical_store()
        srid, fingerprint = self._fixture_identity()
        held = self._insert_activated("hold", srid=srid, fingerprint=fingerprint)
        pending = self._insert_pending(self._record("approved", srid=srid, fingerprint=fingerprint))
        raw = self._raw()
        try:
            for label, decision in (("non_approved", held), ("unactivated", pending)):
                with self.subTest(case=label):
                    with self.assertRaises(sqlite3.IntegrityError):
                        raw.execute(
                            "INSERT INTO build_claim (claim_id, decision_sequence, decision_id,"
                            " decision_record_hash, approval_id, source_record_id,"
                            " source_fingerprint, operation_id, package_payload_hash,"
                            " package_file_name, claimed_at, schema_version, record_hash)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                "claim_" + uuid.uuid4().hex, 1, decision["decision_id"],
                                decision["record_hash"],
                                decision["approval_id"] or ("appr_" + ("0" * 32)),
                                srid, fingerprint, "mcuat_" + uuid.uuid4().hex,
                                "sha256:" + ("1" * 64), "p.json",
                                "2026-07-25T00:00:00+00:00", decisions.SCHEMA_VERSION,
                                "sha256:" + ("2" * 64),
                            ),
                        )
                    raw.rollback()
        finally:
            raw.close()
        self.assertEqual(self._claim_count(), 0)

    def test_claim_record_hash_mismatch_is_refused(self):
        # The trigger enforces the BINDINGS; the canonical row hash is validated in Python, so a
        # claim with correct bindings but a tampered hash is still refused.
        record = self._prepare_activated_approval()
        conn = self._raw()
        try:
            claim = {
                "claim_id": "claim_" + uuid.uuid4().hex,
                "decision_sequence": decisions.decision_sequence(conn, record["decision_id"]),
                "decision_id": record["decision_id"],
                "decision_record_hash": record["record_hash"],
                "approval_id": record["approval_id"],
                "source_record_id": record["source_record_id"],
                "source_fingerprint": record["source_fingerprint"],
                "operation_id": "mcuat_" + uuid.uuid4().hex,
                "package_payload_hash": "sha256:" + ("3" * 64),
                "package_file_name": "p.json",
                "claimed_at": iso(datetime.now(timezone.utc)),
                "schema_version": decisions.SCHEMA_VERSION,
                "record_hash": "sha256:" + ("0" * 64),
            }
            conn.execute("BEGIN IMMEDIATE")
            decisions.insert_build_claim(conn, claim)
            decisions._commit(conn)
        finally:
            conn.close()
        self._assert_refused_untouched(reason="record_hash_mismatch")

    def test_integrity_check_failure_is_refused(self):
        self._prepare_activated_approval()
        path = self._store_path()
        before = path.read_bytes()
        with mock.patch.object(decisions, "_integrity_check",
                               return_value="*** in database main *** simulated page error"):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertEqual(json.loads(out)["decision_store_integrity"], "integrity_check_failed")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.package.exists())

    def test_foreign_key_check_failure_is_refused(self):
        self._prepare_activated_approval()
        path = self._store_path()
        before = path.read_bytes()
        with mock.patch.object(decisions, "_foreign_key_check",
                               return_value=[("build_claim", 1, "decision", 0)]):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertEqual(json.loads(out)["decision_store_integrity"], "foreign_key_check_failed")
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_database_is_refused_untouched(self):
        path = self._store_path()
        path.write_bytes(b"definitely not a SQLite database\n" * 8)
        before = path.read_bytes()
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertEqual(json.loads(out)["decision_store_integrity"], "store_header_invalid")
        self.assertEqual(path.read_bytes(), before)

    def test_headered_but_malformed_database_is_refused_as_corrupt(self):
        # A file that DOES carry a valid rollback-format header but whose pages are truncated
        # passes triage and is refused by SQLite's own structural read, proving that triage
        # does not shadow the `store_corrupt` / `integrity_check_failed` classifications.
        self._canonical_store()
        path = self._store_path()
        intact = path.read_bytes()
        path.write_bytes(intact[: len(intact) // 3])
        before = path.read_bytes()
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertIn(json.loads(out)["decision_store_integrity"],
                      ("store_corrupt", "integrity_check_failed"))
        self.assertEqual(path.read_bytes(), before)

    def test_a_decision_command_never_runs_ddl_against_an_existing_store(self):
        # The strongest form of the guarantee: the canonical schema-creation path is never even
        # entered when the path already exists, whatever its contents. (``sqlite3.Connection`` is
        # a C type and cannot be patched, so the DDL entry point itself is the assertion point.)
        self._store_path().write_bytes(b"")
        before = self._store_path().read_bytes()

        def forbidden(_conn):
            raise AssertionError("no schema DDL may run against an existing store")

        with mock.patch.object(decisions, "_create_canonical_schema", forbidden):
            code, _out = self._run(
                ["approve", "--reviewer", "digital", "--input", str(self.form),
                 "--decision-rows", str(self.rows), "--row-number", "2",
                 "--ledger", str(self.ledger)]
            )
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN)
        self.assertEqual(self._store_path().read_bytes(), before)

    def test_a_genuinely_absent_store_is_created_atomically_and_validated(self):
        # The positive control: creation is allowed exactly once, at an absent path, and the
        # final path only ever appears fully formed.
        self.assertFalse(self._store_path().exists())
        code, _out = self._run(
            ["approve", "--reviewer", "digital", "--input", str(self.form),
             "--decision-rows", str(self.rows), "--row-number", "2",
             "--ledger", str(self.ledger)]
        )
        self.assertEqual(code, 0)
        self.assertTrue(self._store_path().is_file())
        self._read_store(lambda conn: (
            self.assertEqual(decisions._integrity_check(conn), "ok"),
            self.assertEqual(decisions._foreign_key_check(conn), []),
        ))
        self.assertEqual([f for f in os.listdir(self.tmp) if "mcuat_decisions" in f], [],
                         "no operation-owned store temporary is left behind")


class TimestampHostilityTests(_ClaimHarness):
    """Amendment 7: every authority and audit timestamp must parse AND carry a UTC offset.

    A naive value parses happily through ``datetime.fromisoformat`` but comparing it to an aware
    "now" raises an uncontrolled ``TypeError``. One central parser owns the rule.
    """

    NAIVE = "2026-07-28T00:00:00"

    def test_the_central_parser_rejects_naive_and_invalid_values(self):
        for bad in (self.NAIVE, "not-a-timestamp", None, 12345, "2026-07-28T00:00:00 UTC", ""):
            with self.subTest(value=bad):
                self.assertIsNone(decisions.parse_aware_timestamp(bad))
        for good in ("2026-07-28T00:00:00+00:00", "2026-07-28T00:00:00Z",
                     "2026-07-28T08:00:00+08:00"):
            with self.subTest(value=good):
                parsed = decisions.parse_aware_timestamp(good)
                self.assertIsNotNone(parsed)
                self.assertIsNotNone(parsed.utcoffset())

    def _assert_store_timestamp_refusal(self, reason, expected_claims=0):
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"], reason)
        self.assertNotIn(self.NAIVE, out, "the offending value is never exposed")
        self.assertEqual(self._raw_claim_count(), expected_claims)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._stray_temps(), [])

    def test_naive_recorded_at_is_refused(self):
        srid, fingerprint = self._fixture_identity()
        self._hostile_activated("approved", srid=srid, fingerprint=fingerprint,
                                recorded_at=self.NAIVE)
        self._assert_store_timestamp_refusal("naive_timestamp")

    def test_naive_expires_at_is_refused(self):
        srid, fingerprint = self._fixture_identity()
        self._hostile_activated("approved", srid=srid, fingerprint=fingerprint,
                                expires_at=self.NAIVE)
        self._assert_store_timestamp_refusal("naive_timestamp")

    def test_naive_approved_at_is_refused(self):
        srid, fingerprint = self._fixture_identity()
        record = self._record("approved", srid=srid, fingerprint=fingerprint)
        record["approved_at"] = self.NAIVE
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_activate(self._hostile_pending(record))
        self._assert_store_timestamp_refusal("naive_timestamp")

    def test_naive_activated_at_is_refused(self):
        srid, fingerprint = self._fixture_identity()
        record = self._insert_pending(self._record("approved", srid=srid, fingerprint=fingerprint))
        self._activate(record, activated_at=self.NAIVE)
        self._assert_store_timestamp_refusal("naive_timestamp")

    def test_naive_claimed_at_is_refused(self):
        record = self._prepare_activated_approval()
        conn = self._raw()
        try:
            claim = {
                "claim_id": "claim_" + uuid.uuid4().hex,
                "decision_sequence": decisions.decision_sequence(conn, record["decision_id"]),
                "decision_id": record["decision_id"],
                "decision_record_hash": record["record_hash"],
                "approval_id": record["approval_id"],
                "source_record_id": record["source_record_id"],
                "source_fingerprint": record["source_fingerprint"],
                "operation_id": "mcuat_" + uuid.uuid4().hex,
                "package_payload_hash": "sha256:" + ("3" * 64),
                "package_file_name": "p.json",
                "claimed_at": self.NAIVE,
                "schema_version": decisions.SCHEMA_VERSION,
            }
            claim["record_hash"] = decisions.claim_record_hash(claim)
            conn.execute("BEGIN IMMEDIATE")
            decisions.insert_build_claim(conn, claim)
            decisions._commit(conn)
        finally:
            conn.close()
        # The pre-existing invalid claim is the state under test, so it is expected to remain.
        self._assert_store_timestamp_refusal("naive_timestamp", expected_claims=1)

    def test_wrong_types_and_unsafe_characters_are_refused(self):
        for value in ("2026-07-28 00:00:00 UTC", "not-a-timestamp", "2026-13-45T99:99:99+00:00"):
            with self.subTest(value=value):
                self._reset()
                srid, fingerprint = self._fixture_identity()
                self._hostile_activated("approved", srid=srid, fingerprint=fingerprint,
                                        expires_at=value)
                code, out = self._build()
                self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
                self.assertEqual(
                    json.loads(out)["decision_store_integrity"], "timestamp_invalid"
                )
                self.assertNotIn(value, out)

    def test_expiry_before_approval_is_refused(self):
        srid, fingerprint = self._fixture_identity()
        self._hostile_activated(
            "approved", srid=srid, fingerprint=fingerprint,
            recorded_at="2026-07-25T12:00:00+00:00", expires_at="2026-07-25T11:00:00+00:00",
        )
        self._assert_store_timestamp_refusal("timestamp_order_invalid")

    def test_activation_before_recording_is_refused(self):
        srid, fingerprint = self._fixture_identity()
        record = self._insert_pending(self._record(
            "approved", srid=srid, fingerprint=fingerprint,
            recorded_at="2026-07-25T12:00:00+00:00", expires_at="2026-07-28T12:00:00+00:00",
        ))
        self._activate(record, activated_at="2026-07-25T11:00:00+00:00")
        self._assert_store_timestamp_refusal("timestamp_order_invalid")

    def test_naive_timestamps_in_every_jsonl_decision_field_are_refused(self):
        for field in ("recorded_at", "approved_at", "expires_at"):
            with self.subTest(field=field):
                self._reset()
                record = {
                    "event": "decision", "recorded_at": "2026-07-25T00:00:00+00:00",
                    "reviewer_id": "digital", "decision": "approved",
                    "source_record_id": "srcrec_" + ("a" * 64),
                    "source_fingerprint": "fp_" + ("b" * 64), "row_number_hint": 2,
                    "approval_id": "appr_" + ("c" * 32),
                    "approved_at": "2026-07-25T00:00:00+00:00",
                    "expires_at": "2026-07-28T00:00:00+00:00",
                }
                record[field] = self.NAIVE
                with open(self.ledger, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True) + "\n")
                code, out = self._build()
                summary = self._assert_ledger_integrity_uncertain(code, out)
                self.assertEqual(summary["ledger_integrity"], "decision_field_invalid")
                self.assertNotIn(self.NAIVE, out)

    def test_naive_timestamp_in_a_build_audit_event_is_refused(self):
        record = {
            "event": "build", "recorded_at": self.NAIVE,
            "source_record_id": "srcrec_" + ("a" * 64),
            "source_fingerprint": "fp_" + ("b" * 64),
            "approval_id": "appr_" + ("c" * 32),
            "operation_id": "mcuat_" + ("d" * 32),
            "bound_package_payload_hash": "sha256:" + ("e" * 64),
            "package_file_name": "p.json",
        }
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        code, out = self._build()
        summary = self._assert_ledger_integrity_uncertain(code, out)
        self.assertEqual(summary["ledger_integrity"], "publication_field_invalid")
        self.assertNotIn(self.NAIVE, out)

    def test_naive_timestamp_in_a_cleanup_incomplete_event_is_refused(self):
        record = {
            "event": "build_cleanup_incomplete", "recorded_at": self.NAIVE,
            "source_record_id": "srcrec_" + ("a" * 64),
            "source_fingerprint": "fp_" + ("b" * 64),
            "approval_id": "appr_" + ("c" * 32),
            "operation_id": "mcuat_" + ("d" * 32),
            "bound_package_payload_hash": "sha256:" + ("e" * 64),
            "package_file_name": "p.json",
            "cleanup_incomplete": True, "stale_temp_basename": ".mcuat_pkg_x.tmp",
        }
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        code, out = self._build()
        self.assertEqual(
            self._assert_ledger_integrity_uncertain(code, out)["ledger_integrity"],
            "publication_field_invalid",
        )


# =========================================================================== #
# Amendment 8 - design lock DL-113-A8-001
# =========================================================================== #
class _RecordingConnection:
    """A pass-through SQLite connection that records every statement it is asked to run.

    Used to prove behaviourally - not by reading the source - that no persistent database
    property is ever assigned to a store this operation did not just create.
    """

    def __init__(self, conn, log):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_log", log)

    def execute(self, sql, *args):
        self._log.append(sql)
        return self._conn.execute(sql, *args)

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


class _TriageHarness(_ClaimHarness):
    """Shared fixtures for the Amendment 8 pre-open triage and publication suites."""

    def _wal_header_store(self):
        """A canonical store flipped to WAL format, with NO sidecars left behind.

        Closing the last connection checkpoints and removes `-wal`/`-shm`, so what remains is
        the hardest case: a file that still looks canonical but whose header says WAL.
        """
        self._canonical_store()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("PRAGMA journal_mode=WAL")
        finally:
            raw.close()
        return self._store_path()

    def _header_bytes(self):
        with open(self._store_path(), "rb") as handle:
            return handle.read(100)

    def _assert_wal_header(self):
        raw = self._header_bytes()
        self.assertEqual((raw[18], raw[19]), (2, 2), "the fixture must be WAL format")

    def _write_sidecar(self, suffix, payload=b"synthetic sidecar residue\n"):
        path = Path(str(self._store_path()) + suffix)
        path.write_bytes(payload)
        return path

    def _assert_no_sqlite_connect(self, call):
        """Prove a refusal is decided by pure triage: SQLite is never opened at all."""
        def forbidden(*args, **kwargs):
            raise AssertionError(
                "no SQLite connection may be opened for a pre-triage refusal"
            )

        with mock.patch.object(sqlite3, "connect", forbidden):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                call()
        return caught.exception

    def _second_canonical_store(self, name="other"):
        """A DIFFERENT canonical store, built in its own directory."""
        other_dir = self.tmp / name
        other_dir.mkdir(exist_ok=True)
        other_path = other_dir / decisions.DECISION_STORE_NAME
        decisions.create_store_exclusively(other_path)
        return other_path

    def _no_sweep(self):
        """Patches that fail the test if any code path lists, scans or globs a directory."""
        def forbidden(*args, **kwargs):
            raise AssertionError("no store path may list, scan or glob a directory")

        return (
            mock.patch("os.listdir", forbidden),
            mock.patch("os.scandir", forbidden),
            mock.patch("glob.glob", forbidden),
            mock.patch.object(Path, "iterdir", forbidden),
            mock.patch.object(Path, "glob", forbidden),
        )


class PreOpenTriageTests(_TriageHarness):
    """Lock section 2: a WAL header or any sidecar is refused BEFORE SQLite is opened.

    Amendment 7 opened the file first and applied a persistent `journal_mode=DELETE`, so a
    WAL-mode database was rewritten - and its `-wal`/`-shm` removed - before the code decided
    to refuse it. Removing a write-ahead log is the documented way to destroy crash recovery,
    so the decision has to be taken from the file's own bytes, with no SQLite involvement.
    """

    def test_wal_header_without_sidecars_is_refused_before_sqlite_opens(self):
        self._wal_header_store()
        self._assert_wal_header()
        before = self._store_snapshot()
        error = self._assert_no_sqlite_connect(
            lambda: decisions.inspect_store(self._store_path())
        )
        self.assertEqual(error.reason, "store_journal_mode_unsupported")
        self.assertEqual(self._store_snapshot(), before)
        self._assert_refused_untouched(reason="store_journal_mode_unsupported")

    def test_a_live_wal_and_shm_are_refused_and_left_intact(self):
        # A genuine, uncheckpointed write-ahead log: the case where Amendment 7 would have
        # deleted committed data belonging to another writer.
        self._canonical_store()
        holder = sqlite3.connect(str(self._store_path()))
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("PRAGMA wal_autocheckpoint=0")
            holder.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('wal_probe', 'resident')"
            )
            holder.commit()
            wal = Path(str(self._store_path()) + "-wal")
            self.assertTrue(wal.exists(), "the fixture must have a live -wal")
            before = self._store_snapshot()
            error = self._assert_no_sqlite_connect(
                lambda: decisions.inspect_store(self._store_path())
            )
            # Sidecar presence is decided before the header is even consulted.
            self.assertIn(error.reason,
                          ("store_sidecar_present", "store_journal_mode_unsupported"))
            self.assertEqual(self._store_snapshot(), before,
                             "the write-ahead log must survive byte for byte")
            code, out = self._build()
            self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
            self.assertNotIn("Traceback", out)
            self.assertEqual(self._store_snapshot(), before)
        finally:
            holder.close()

    def test_each_sidecar_alone_is_refused_untouched(self):
        for suffix in decisions.STORE_SIDECAR_SUFFIXES:
            with self.subTest(sidecar=suffix):
                self._reset()
                self._canonical_store()
                self._write_sidecar(suffix)
                before = self._store_snapshot()
                error = self._assert_no_sqlite_connect(
                    lambda: decisions.inspect_store(self._store_path())
                )
                self.assertEqual(error.reason, "store_sidecar_present")
                self.assertEqual(self._store_snapshot(), before)

    def test_stale_and_mixed_sidecar_combinations_are_refused_untouched(self):
        combinations = (
            ("-wal", "-shm"),
            ("-journal", "-wal"),
            ("-journal", "-wal", "-shm"),
        )
        for suffixes in combinations:
            with self.subTest(sidecars=suffixes):
                self._reset()
                self._canonical_store()
                for suffix in suffixes:
                    self._write_sidecar(suffix, payload=b"stale " + suffix.encode())
                before = self._store_snapshot()
                error = self._assert_no_sqlite_connect(
                    lambda: decisions.inspect_store(self._store_path())
                )
                self.assertEqual(error.reason, "store_sidecar_present")
                self.assertEqual(self._store_snapshot(), before)

    def test_a_sidecar_path_occupied_by_a_directory_is_refused(self):
        self._canonical_store()
        os.mkdir(str(self._store_path()) + "-wal")
        before = self._store_snapshot()
        error = self._assert_no_sqlite_connect(
            lambda: decisions.inspect_store(self._store_path())
        )
        self.assertEqual(error.reason, "store_sidecar_present")
        self.assertEqual(self._store_snapshot(), before)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_a_sidecar_path_occupied_by_a_symlink_is_refused(self):
        self._canonical_store()
        link = Path(str(self._store_path()) + "-journal")
        target = self.tmp / "symlink_target.bin"
        target.write_bytes(b"target\n")
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {type(error).__name__}")
        before = self._store_snapshot()
        caught = self._assert_no_sqlite_connect(
            lambda: decisions.inspect_store(self._store_path())
        )
        self.assertEqual(caught.reason, "store_sidecar_present")
        self.assertEqual(self._store_snapshot(), before)
        self.assertTrue(os.path.lexists(link), "the symlink is never removed")

    def test_a_truncated_header_is_refused_before_sqlite_opens(self):
        self._store_path().write_bytes(b"SQLite format 3\x00" + b"\x00" * 20)
        before = self._store_snapshot()
        error = self._assert_no_sqlite_connect(
            lambda: decisions.inspect_store(self._store_path())
        )
        self.assertEqual(error.reason, "store_header_invalid")
        self.assertEqual(self._store_snapshot(), before)

    def test_an_implausible_page_size_is_refused_before_sqlite_opens(self):
        header = bytearray(b"SQLite format 3\x00" + b"\x00" * 84)
        header[16:18] = (777).to_bytes(2, "big")     # not a supported page size
        header[18] = 1
        header[19] = 1
        self._store_path().write_bytes(bytes(header))
        error = self._assert_no_sqlite_connect(
            lambda: decisions.inspect_store(self._store_path())
        )
        self.assertEqual(error.reason, "store_header_invalid")

    def test_a_multiply_linked_store_is_refused_before_sqlite_opens(self):
        self._canonical_store()
        alias = self.tmp / "store_alias.sqlite3"
        try:
            os.link(str(self._store_path()), str(alias))
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"hard links unavailable: {type(error).__name__}")
        before = self._store_snapshot()
        error = self._assert_no_sqlite_connect(
            lambda: decisions.inspect_store(self._store_path())
        )
        self.assertEqual(error.reason, "store_multiple_links")
        self.assertEqual(self._store_snapshot(), before)
        self.assertTrue(alias.exists(), "the competing name is never removed")

    def test_a_foreign_wal_database_is_refused_untouched(self):
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("PRAGMA journal_mode=WAL")
            raw.execute("CREATE TABLE unrelated_service (id INTEGER PRIMARY KEY)")
            raw.commit()
        finally:
            raw.close()
        self._assert_wal_header()
        self._assert_refused_untouched(reason="store_journal_mode_unsupported")

    def test_a_wal_mode_partial_canonical_store_is_refused_untouched(self):
        self._canonical_store()
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("DROP TABLE build_claim")
            raw.execute("PRAGMA journal_mode=WAL")
            raw.commit()
        finally:
            raw.close()
        # The header decides first: a WAL-format file is never opened to discover that it is
        # also structurally wrong.
        self._assert_refused_untouched(reason="store_journal_mode_unsupported")

    def test_every_recovery_entry_point_refuses_a_wal_header_without_opening_sqlite(self):
        self._wal_header_store()
        before = self._store_snapshot()
        for recover, args in (
            (decisions.recover_decision_commit,
             ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
            (decisions.recover_activation_commit,
             ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
            (decisions.recover_claim_commit,
             ("claim_" + ("0" * 32), "sha256:" + ("0" * 64))),
        ):
            with self.subTest(recovery=recover.__name__):
                def forbidden(*a, **kw):
                    raise AssertionError("recovery must not open SQLite for a WAL header")

                with mock.patch.object(sqlite3, "connect", forbidden):
                    state, row = recover(self._store_path(), *args)
                self.assertEqual(state, decisions.CommitState.UNCERTAIN)
                self.assertIsNone(row)
                self.assertEqual(self._store_snapshot(), before)


class RollbackModePositiveControlTests(_TriageHarness):
    """Lock section 8: the locked paths must still do real work on a valid store."""

    def test_inspection_of_a_canonical_store_changes_nothing(self):
        self._prepare_activated_approval()
        before = self._store_snapshot()
        decisions.inspect_store(self._store_path())
        self.assertEqual(self._store_snapshot(), before,
                         "read-only inspection changes no byte and creates no sidecar")
        for path in self._sidecar_paths():
            self.assertFalse(os.path.lexists(path))

    def test_a_canonical_store_supports_decision_activation_and_claim(self):
        self.assertEqual(self._approve()[0], 0)
        rows = self._stored_decisions()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["activation_sequence"])
        self.assertEqual(self._build()[0], 0)
        self.assertEqual(self._claim_count(), 1)
        decisions.inspect_store(self._store_path())
        header = self._header_bytes()
        self.assertEqual((header[18], header[19]), (1, 1),
                         "the store stays rollback format throughout")
        for path in self._sidecar_paths():
            self.assertFalse(os.path.lexists(path), "no sidecar survives an operation")

    def test_a_blocked_build_leaves_the_store_byte_for_byte_identical(self):
        # Amendment 8 runs the whole build preflight read-only, so a refusal cannot touch the
        # store at all - a strictly stronger property than Amendment 7 could offer.
        self._insert_activated("hold", *[], **dict(zip(
            ("srid", "fingerprint"), self._fixture_identity())))
        before = self._store_snapshot()
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, out)
        self.assertEqual(self._store_snapshot(), before)

    def test_the_read_only_session_is_genuinely_read_only(self):
        # `query_only` is defence in depth ONLY - it does not block a persistent journal-mode
        # assignment - so the guarantee has to come from the session itself. Turning the pragma
        # off and still being refused is what proves `mode=ro` is doing the work.
        self._canonical_store()
        captured = {}
        real_connect = decisions._connect_uri

        def capturing(path, query):
            captured["query"] = query
            return real_connect(path, query)

        with mock.patch.object(decisions, "_connect_uri", capturing):
            decisions.inspect_store(self._store_path())
        self.assertIn("mode=ro", captured["query"])
        self.assertIn("cache=private", captured["query"])

        before = self._store_snapshot()
        conn, _triage = decisions.open_readonly(self._store_path())
        try:
            conn.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES ('probe', 'written')"
                )
        finally:
            conn.close()
        self.assertEqual(self._store_snapshot(), before)
        decisions.inspect_store(self._store_path())

    def test_the_writer_verifies_journal_mode_and_never_assigns_it(self):
        # Every statement issued against an EXISTING store is recorded. `journal_mode` may be
        # QUERIED but never assigned: the one assignment in the module happens on the brand-new
        # temporary this operation exclusively created, and never afterwards.
        self._canonical_store()
        executed = []
        real_connect = decisions._connect_uri

        def recording_connect(path, query):
            return _RecordingConnection(real_connect(path, query), executed)

        with mock.patch.object(decisions, "_connect_uri", recording_connect):
            self.assertEqual(self._approve()[0], 0)
            self.assertEqual(self._build()[0], 0)

        self.assertTrue(executed, "the recording proxy must have seen real statements")
        self.assertIn("PRAGMA journal_mode", executed, "the mode is verified, not assumed")
        self.assertEqual(
            [sql for sql in executed if "journal_mode" in sql.lower() and "=" in sql],
            [], "no journal_mode assignment may reach an existing store",
        )
        self.assertEqual(
            [sql for sql in executed if "locking_mode" in sql.lower()],
            [], "no locking-mode setting may reach an existing store",
        )


class CrossSourceHostilityTests(_TriageHarness):
    """Lock section 5: a malformed row under ANY source blocks EVERY operation.

    Amendment 7 validated decision and activation content only inside `resolve_authority`,
    whose query is filtered to the requested source record, so corruption under an unrelated
    source survived into an authorised build.
    """

    OTHER_SRID = "srcrec_" + ("b" * 64)
    OTHER_FP = "fp_" + ("c" * 64)

    def _valid_source_a(self):
        srid, fingerprint = self._fixture_identity()
        return self._insert_activated("approved", srid=srid, fingerprint=fingerprint)

    def _other_record(self, decision_type="hold", **kw):
        return self._record(decision_type, srid=self.OTHER_SRID,
                            fingerprint=self.OTHER_FP, **kw)

    def _assert_source_a_is_blocked(self, reason):
        before = self._store_snapshot()
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"], reason)
        self.assertIs(summary["decision_store_modified"], False)
        self.assertEqual(self._store_snapshot(), before,
                         "the whole fixture is preserved")
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._stray_temps(), [])
        self.assertEqual(self._raw_claim_count(), 0)

    def test_naive_decision_timestamp_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._other_record(recorded_at="2026-07-20T00:00:00")
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_pending(record)
        self._assert_source_a_is_blocked("naive_timestamp")

    def test_invalid_decision_timestamp_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._other_record(recorded_at="not-a-timestamp")
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_pending(record)
        self._assert_source_a_is_blocked("timestamp_invalid")

    def test_approval_before_recording_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._record("approved", srid=self.OTHER_SRID, fingerprint=self.OTHER_FP,
                              recorded_at="2026-07-25T12:00:00+00:00")
        record["approved_at"] = "2026-07-25T09:00:00+00:00"     # earlier than recorded_at
        record["expires_at"] = "2026-07-27T12:00:00+00:00"
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_pending(record)
        self._assert_source_a_is_blocked("timestamp_order_invalid")

    def test_expiry_before_approval_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._record("approved", srid=self.OTHER_SRID, fingerprint=self.OTHER_FP,
                              recorded_at="2026-07-25T12:00:00+00:00",
                              expires_at="2026-07-25T11:00:00+00:00")
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_pending(record)
        self._assert_source_a_is_blocked("timestamp_order_invalid")

    def test_wrong_decision_hash_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._other_record()
        record["record_hash"] = "sha256:" + ("d" * 64)
        self._hostile_pending(record)
        self._assert_source_a_is_blocked("record_hash_mismatch")

    def test_invalid_approved_field_combination_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._other_record()
        record["reviewer_id"] = "NOT A HANDLE"
        record["record_hash"] = decisions.decision_record_hash(record)
        self._hostile_pending(record)
        self._assert_source_a_is_blocked("invalid_field_type")

    def test_malformed_activation_timestamp_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._hostile_pending(self._other_record())
        self._hostile_activate(record, activated_at="2026-07-20T00:00:00")
        self._assert_source_a_is_blocked("naive_timestamp")

    def test_activation_before_recording_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._hostile_pending(
            self._other_record(recorded_at="2026-07-25T12:00:00+00:00")
        )
        self._hostile_activate(record, activated_at="2026-07-25T09:00:00+00:00")
        self._assert_source_a_is_blocked("timestamp_order_invalid")

    def test_wrong_activation_hash_under_source_b_blocks_source_a(self):
        self._valid_source_a()
        record = self._hostile_pending(self._other_record())
        self._hostile_activate(record, activation_hash="sha256:" + ("e" * 64))
        self._assert_source_a_is_blocked("activation_mismatch")

    def test_activation_bound_to_the_wrong_decision_blocks_source_a(self):
        self._valid_source_a()
        first = self._hostile_pending(self._other_record())
        second = self._hostile_pending(self._other_record())
        # An activation for `second` carrying the canonical hash of `first`.
        moment = iso(datetime.now(timezone.utc))
        self._hostile_activate(
            second,
            activated_at=moment,
            activation_hash=decisions.activation_record_hash(
                second["decision_id"], moment, first["record_hash"]
            ),
        )
        self._assert_source_a_is_blocked("activation_mismatch")

    def test_a_hostile_activation_recorded_first_still_blocks_source_a(self):
        # The hostile activation carries the LOWEST activation sequence, so a validator that
        # skipped any single row rather than checking them all would miss exactly this one.
        hostile = self._hostile_pending(self._other_record())
        self._hostile_activate(hostile, activation_hash="sha256:" + ("e" * 64))
        srid, fingerprint = self._fixture_identity()
        good = self._hostile_pending(
            self._record("approved", srid=srid, fingerprint=fingerprint)
        )
        self._hostile_activate(good)
        self._assert_source_a_is_blocked("activation_mismatch")

    def test_a_hostile_decision_recorded_last_still_blocks_source_a(self):
        # The mirror case: the hostile DECISION carries the highest sequence, so a validator
        # that skipped the first row rather than all of them would still have to catch it.
        self._valid_source_a()
        record = self._other_record()
        record["record_hash"] = "sha256:" + ("d" * 64)
        self._hostile_pending(record)
        self._assert_source_a_is_blocked("record_hash_mismatch")

    def test_an_extra_metadata_row_after_the_schema_version_key_is_refused(self):
        # Sorting AFTER `schema_version` means the first row still looks perfect, so only an
        # exact cardinality check can catch it.
        self._valid_source_a()
        raw = self._raw()
        try:
            raw.execute("INSERT INTO schema_meta (key, value) VALUES ('zz_injected', 'yes')")
            raw.commit()
        finally:
            raw.close()
        self._assert_source_a_is_blocked("metadata_row_set_invalid")

    def test_an_extra_metadata_row_blocks_every_operation(self):
        self._valid_source_a()
        raw = self._raw()
        try:
            raw.execute("INSERT INTO schema_meta (key, value) VALUES ('injected', 'yes')")
            raw.commit()
        finally:
            raw.close()
        self._assert_source_a_is_blocked("metadata_row_set_invalid")

    def test_a_missing_metadata_row_blocks_every_operation(self):
        self._valid_source_a()
        raw = self._raw()
        try:
            raw.execute("DELETE FROM schema_meta")
            raw.commit()
        finally:
            raw.close()
        self._assert_source_a_is_blocked("schema_version_missing")

    def test_a_wrong_metadata_value_blocks_every_operation(self):
        self._valid_source_a()
        raw = self._raw()
        try:
            raw.execute("DELETE FROM schema_meta")
            raw.execute("INSERT INTO schema_meta (key, value) VALUES "
                        "('schema_version', 'member_create_uat_decisions/v1')")
            raw.commit()
        finally:
            raw.close()
        self._assert_source_a_is_blocked("schema_version_mismatch")

    def test_a_wrong_metadata_key_blocks_every_operation(self):
        self._valid_source_a()
        raw = self._raw()
        try:
            raw.execute("DELETE FROM schema_meta")
            raw.execute("INSERT INTO schema_meta (key, value) VALUES "
                        "('version', 'member_create_uat_decisions/v2')")
            raw.commit()
        finally:
            raw.close()
        self._assert_source_a_is_blocked("metadata_row_set_invalid")


class ClaimChronologyTests(_TriageHarness):
    """Lock sections 5 and 6: a claim must follow the approval AND activation it binds.

    Amendment 7 proved only that `claimed_at` was an aware timestamp and that the canonical
    claim hash recomputed, so a correctly hashed claim stamped before its own authority was
    accepted by both the insert trigger and the validator.
    """

    def _insert_claim(self, record, *, claimed_at):
        """Insert a correctly hashed claim through a RAW connection at an arbitrary instant."""
        conn = self._raw()
        try:
            claim = {
                "claim_id": "claim_" + uuid.uuid4().hex,
                "decision_sequence": decisions.decision_sequence(
                    conn, record["decision_id"]
                ),
                "decision_id": record["decision_id"],
                "decision_record_hash": record["record_hash"],
                "approval_id": record["approval_id"],
                "source_record_id": record["source_record_id"],
                "source_fingerprint": record["source_fingerprint"],
                "operation_id": "mcuat_" + uuid.uuid4().hex,
                "package_payload_hash": "sha256:" + ("3" * 64),
                "package_file_name": "member_create_uat_package_v2.json",
                "claimed_at": claimed_at,
                "schema_version": decisions.SCHEMA_VERSION,
            }
            claim["record_hash"] = decisions.claim_record_hash(claim)
            conn.execute("BEGIN IMMEDIATE")
            decisions.insert_build_claim(conn, claim)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return claim

    def _approved_at(self, hours_ago):
        moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return iso(moment)

    def _activated_approval(self, *, approved_hours_ago=2, activated_hours_ago=1):
        srid, fingerprint = self._fixture_identity()
        record = self._record(
            "approved", srid=srid, fingerprint=fingerprint,
            recorded_at=self._approved_at(approved_hours_ago),
            expires_at=iso(datetime.now(timezone.utc) + timedelta(hours=48)),
        )
        self._insert_pending(record)
        self._activate(record, activated_at=self._approved_at(activated_hours_ago))
        return record

    def _assert_claim_order_refused(self):
        before = self._store_snapshot()
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["decision_store_integrity"], "timestamp_order_invalid")
        self.assertIs(summary["decision_store_modified"], False)
        self.assertEqual(self._store_snapshot(), before)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._stray_temps(), [])

    def test_a_correctly_hashed_claim_before_its_approval_is_refused(self):
        record = self._activated_approval()
        claim = self._insert_claim(record, claimed_at=self._approved_at(5))
        # The insert trigger accepts it: it compares bindings, never instants.
        self.assertEqual(self._raw_claim_count(), 1)
        self.assertIsNone(decisions.claim_chronology_problem(
            claim["claimed_at"], claim["claimed_at"], claim["claimed_at"]
        ))
        self._assert_claim_order_refused()

    def test_a_correctly_hashed_claim_before_its_activation_is_refused(self):
        # Later than the approval, earlier than the activation: the second ordering is what
        # catches it, so this kills a fix that only compared against `approved_at`.
        record = self._activated_approval(approved_hours_ago=4, activated_hours_ago=2)
        self._insert_claim(record, claimed_at=self._approved_at(3))
        self.assertEqual(self._raw_claim_count(), 1)
        self._assert_claim_order_refused()

    def test_a_claim_at_the_activation_instant_is_accepted(self):
        record = self._activated_approval(approved_hours_ago=2, activated_hours_ago=1)
        self._insert_claim(record, claimed_at=self._approved_at(1))
        decisions.inspect_store(self._store_path())

    def test_the_pre_insert_check_refuses_a_backdated_claim_inside_the_transaction(self):
        # Lock section 6: the newly minted claim is checked against the RE-RESOLVED authority
        # inside the very `BEGIN IMMEDIATE` that would insert it.
        self._prepare_activated_approval()
        real_now = decisions.utc_now
        stale = datetime.now(timezone.utc) - timedelta(hours=6)

        def backdated():
            return stale

        with mock.patch.object(approval, "utc_now_iso", lambda: iso(stale)), \
                mock.patch.object(decisions, "utc_now", real_now):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "claim_timestamp_order_invalid")
        self.assertEqual(summary["claim_detail"], "timestamp_order_invalid")
        self.assertEqual(summary["build_claim"], "not_created")
        self.assertIs(summary["do_not_retry"], True)
        self.assertNotIn("Traceback", out)
        self._assert_nothing_created()
        self.assertFalse(self.package.exists())


class OffsetChronologyTests(_TriageHarness):
    """Lock section 5: chronology compares INSTANTS, never text.

    Every pair below is chosen so that lexical ordering and chronological ordering disagree,
    which is what makes a SQLite `TEXT` comparison the wrong authority.
    """

    # (earlier instant, later instant) whose STRING order is reversed.
    MISLEADING = (
        ("2026-07-26T01:00:00+08:00", "2026-07-25T18:00:00+00:00"),
        ("2026-07-26T00:30:00+08:00", "2026-07-25T17:00:00+00:00"),
    )

    def test_the_fixtures_really_are_lexically_misleading(self):
        for earlier, later in self.MISLEADING:
            with self.subTest(pair=(earlier, later)):
                self.assertGreater(earlier, later, "string order must be reversed")
                self.assertLess(
                    decisions.parse_aware_timestamp(earlier),
                    decisions.parse_aware_timestamp(later),
                    "instant order must be the opposite",
                )

    def test_activation_ordering_uses_instants_not_text(self):
        earlier, later = self.MISLEADING[0]
        srid, fingerprint = self._fixture_identity()
        # recorded at the EARLIER instant, activated at the LATER instant: valid in time,
        # but a lexical comparison would call the activation "before" the decision.
        record = self._record("approved", srid=srid, fingerprint=fingerprint,
                              recorded_at=earlier,
                              expires_at=iso(datetime.now(timezone.utc) + timedelta(hours=24)))
        self._insert_pending(record)
        self._activate(record, activated_at=later)
        decisions.inspect_store(self._store_path())

    def test_a_chronologically_invalid_activation_is_refused_despite_lexical_order(self):
        earlier, later = self.MISLEADING[0]
        srid, fingerprint = self._fixture_identity()
        # recorded at the LATER instant, activated at the EARLIER one: invalid in time even
        # though the strings sort the other way.
        record = self._record("approved", srid=srid, fingerprint=fingerprint,
                              recorded_at=later,
                              expires_at=iso(datetime.now(timezone.utc) + timedelta(hours=24)))
        self._hostile_pending(record)
        self._hostile_activate(record, activated_at=earlier)
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertEqual(json.loads(out)["decision_store_integrity"],
                         "timestamp_order_invalid")
        self.assertNotIn(earlier, out, "the offending value is never exposed")

    def test_claim_chronology_uses_instants_across_offsets(self):
        earlier, later = self.MISLEADING[0]
        self.assertIsNone(
            decisions.claim_chronology_problem(later, earlier, earlier),
            "a claim after both, expressed in a different offset, is valid",
        )
        self.assertEqual(
            decisions.claim_chronology_problem(earlier, later, later),
            "timestamp_order_invalid",
            "a claim before both is invalid however the strings sort",
        )

    def test_equal_instants_in_different_offsets_are_accepted(self):
        self.assertIsNone(decisions.claim_chronology_problem(
            "2026-07-26T09:00:00+08:00", "2026-07-26T01:00:00+00:00",
            "2026-07-25T21:00:00-04:00",
        ))


class IdentityAndReplacementSeamTests(_TriageHarness):
    """Lock section 1: ordinary path replacement is DETECTED by ordered identity checks.

    The supported boundary is a stable, local, operator-controlled directory and cooperating
    tool processes. These seams prove the ordered checks fire. They do not - and the contract
    does not - claim atomicity against a privileged, non-cooperating process able to substitute
    a path component during an open system call.
    """

    def _swap(self, replacement):
        """Replace the store pathname with a DIFFERENT canonical store.

        Returns True when the substitution actually happened. Windows refuses to replace a
        file that is currently open, which is a stronger guarantee than detection - the
        substitution cannot occur at all - so callers assert whichever outcome the platform
        produced rather than assuming one of them.
        """
        try:
            os.replace(str(replacement), str(self._store_path()))
            return True
        except OSError:
            return False

    def test_replacement_between_identity_capture_and_header_read_is_detected(self):
        self._canonical_store()
        replacement = self._second_canonical_store()
        original = os.lstat(self._store_path())
        real_header = decisions._read_store_header

        def swapping_header(path):
            self._swap(replacement)
            return real_header(path)

        with mock.patch.object(decisions, "_read_store_header", swapping_header):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.inspect_store(self._store_path())
        self.assertEqual(caught.exception.reason, "store_identity_changed")
        current = os.lstat(self._store_path())
        self.assertNotEqual((current.st_dev, current.st_ino),
                            (original.st_dev, original.st_ino),
                            "the fixture really did replace the file")

    def test_replacement_between_header_read_and_sqlite_open_is_detected(self):
        self._prepare_activated_approval()
        replacement = self._second_canonical_store()
        real_connect = decisions._connect_uri

        def swapping_connect(path, query):
            self._swap(replacement)
            return real_connect(path, query)

        with mock.patch.object(decisions, "_connect_uri", swapping_connect):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.inspect_store(self._store_path())
        # No handle is open yet at this seam, so the substitution succeeds on every platform.
        # Amendment 9 catches it STRICTLY EARLIER than Amendment 8's post-close preservation
        # proof did: the replacement is itself a canonical admitted store, but its admission
        # fact binds a different file identity than the one triage just proved, so the
        # operational validator refuses inside the read transaction. Either classifier is a
        # correct refusal; the admission binding is the one that fires first.
        self.assertEqual(caught.exception.reason, "store_admission_invalid")
        # The substituted file is left exactly as the substitution left it: refusing never
        # deletes, repairs or re-admits whatever now occupies the path.
        self.assertTrue(os.path.lexists(self._store_path()))

    def test_replacement_during_read_only_inspection_is_detected_or_prevented(self):
        self._prepare_activated_approval()
        replacement = self._second_canonical_store()
        swapped = {"done": False}

        def swap_during_read(conn):
            swapped["done"] = self._swap(replacement)
            return "read-completed"

        if_swapped_reason = None
        try:
            value = decisions.read_validated(self._store_path(), swap_during_read)
        except decisions.DecisionStoreError as error:
            if_swapped_reason = error.reason
            value = None
        if swapped["done"]:
            self.assertEqual(if_swapped_reason, "store_identity_changed",
                             "a substitution under an open session must be detected")
        else:
            # The platform refused the substitution outright while the file was open.
            self.assertIsNone(if_swapped_reason)
            self.assertEqual(value, "read-completed")
            self.assertTrue(replacement.exists(), "the replacement was never consumed")

    def test_replacement_between_preflight_and_writer_open_is_detected(self):
        self._prepare_activated_approval()
        replacement = self._second_canonical_store()
        swapped = {"done": False}
        real_begin = decisions.begin_write

        def swapping_begin(path):
            if not swapped["done"]:
                swapped["done"] = True
                self._swap(replacement)
            return real_begin(path)

        with mock.patch.object(decisions, "begin_write", swapping_begin):
            code, out = self._build()
        # The replacement store is canonical but holds no approval for this source, so the
        # build refuses rather than claiming against a store it never validated.
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])

    def test_a_store_that_gains_a_sidecar_mid_session_fails_closed(self):
        self._prepare_activated_approval()

        def add_sidecar(conn):
            Path(str(self._store_path()) + "-journal").write_bytes(b"appeared\n")
            return None

        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.read_validated(self._store_path(), add_sidecar)
        self.assertEqual(caught.exception.reason, "store_sidecar_present")
        self.assertTrue(Path(str(self._store_path()) + "-journal").exists(),
                        "the sidecar is never removed")

    def test_a_competing_first_store_creator_leaves_the_winner_untouched(self):
        # The loser's temporary is removed and the winner's store is byte-for-byte intact.
        winner = self._second_canonical_store(name="winner")
        real_publish = (decisions._publish_windows if decisions.IS_WINDOWS
                        else decisions._publish_posix)

        def publish_after_a_competitor_wins(temp_name, safe, identity, parent):
            if not os.path.lexists(safe):
                shutil.copyfile(str(winner), str(safe))
            return real_publish(temp_name, safe, identity, parent)

        name = "_publish_windows" if decisions.IS_WINDOWS else "_publish_posix"
        with mock.patch.object(decisions, name, publish_after_a_competitor_wins):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_not_absent")
        self.assertEqual(self._store_path().read_bytes(), winner.read_bytes(),
                         "the competitor's store is never altered")
        self.assertEqual(
            [f for f in os.listdir(self.tmp) if f.startswith(".mcuat_decisions_")], [],
            "only this operation's own temporary is removed",
        )


class _PublicationHarness(_TriageHarness):
    """Shared assertions for first-use store creation and publication."""

    def _link_target(self, name):
        """Resolve a name the production link handed us into an absolute path.

        Amendment 9 publishes descriptor-relatively, so ``os.link`` receives BASENAMES plus
        ``src_dir_fd``/``dst_dir_fd`` rather than absolute pathnames. A test double that wants to
        inspect, copy or probe the object therefore has to resolve the name against the admitted
        state parent - resolving it against the process working directory would silently address a
        different file. The production call is never weakened to suit the double.
        """
        text = str(name)
        return Path(text) if os.path.isabs(text) else self.tmp / text

    def _create(self):
        return decisions.create_store_exclusively(self._store_path())

    def _assert_final_is_single_named_and_canonical(self):
        info = os.lstat(self._store_path())
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(getattr(info, "st_nlink", 1), 1,
                         "the published store must have exactly one name")
        self.assertEqual(
            [f for f in os.listdir(self.tmp) if f.startswith(".mcuat_decisions_")], [],
            "no operation-owned temporary may survive a successful publication",
        )
        for path in self._sidecar_paths():
            self.assertFalse(os.path.lexists(path))
        decisions.inspect_store(self._store_path())

    def _temporaries(self):
        return sorted(f for f in os.listdir(self.tmp) if f.startswith(".mcuat_decisions_"))


class StoreCreationPublicationTests(_PublicationHarness):
    """Lock section 7: creation is absent-path-only and publication is no-replace.

    Platform-independent properties. The Windows and POSIX suites below cover each platform's
    own publication primitive and its own failure modes.
    """

    def test_creation_publishes_a_complete_canonical_store(self):
        result = self._create()
        self.assertIn(result.durability,
                      ("windows_move_write_through", "posix_link_and_directory_fsync"))
        self.assertIn(result.temp_cleanup, ("not_required", "unlinked"))
        self._assert_final_is_single_named_and_canonical()

    def test_creation_never_lists_or_sweeps_a_directory(self):
        patches = self._no_sweep()
        for patch in patches:
            patch.start()
        try:
            self._create()
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(self._temporaries(), [])

    def test_creation_is_refused_when_anything_occupies_the_final_path(self):
        self._store_path().write_bytes(b"occupied\n")
        before = self._store_snapshot()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._create()
        self.assertEqual(caught.exception.reason, "store_not_absent")
        self.assertEqual(self._store_snapshot(), before)
        self.assertEqual(self._temporaries(), [], "no temporary is created either")

    def test_a_failed_schema_transaction_leaves_the_final_path_absent(self):
        def failing_schema(conn):
            raise decisions.DecisionStoreError(
                "synthetic schema failure", reason="store_create_failed"
            )

        with mock.patch.object(decisions, "_create_canonical_schema", failing_schema):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_create_failed")
        self.assertFalse(os.path.lexists(self._store_path()),
                         "the final path is never created")
        self.assertEqual(len(self._temporaries()), 1,
                         "the temporary is deliberately preserved as evidence")

    def test_a_temporary_that_gains_a_sidecar_is_refused_before_publication(self):
        real_fsync = decisions._fsync_file

        def sidecar_then_fsync(path, **kwargs):
            Path(str(path) + "-wal").write_bytes(b"injected\n")
            return real_fsync(path, **kwargs)

        with mock.patch.object(decisions, "_fsync_file", sidecar_then_fsync):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_sidecar_present")
        self.assertFalse(os.path.lexists(self._store_path()))

    def test_a_temporary_fsync_failure_refuses_before_publication(self):
        def failing_fsync(path, **kwargs):
            raise OSError(5, "synthetic fsync failure")

        with mock.patch.object(decisions, "_fsync_file", failing_fsync):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_create_failed")
        self.assertFalse(os.path.lexists(self._store_path()))

    def test_the_temporary_never_holds_authoritative_or_private_rows(self):
        seen = {}

        real_validate = decisions.validate_store

        def capturing_validate(conn):
            if "tables" not in seen:
                seen["tables"] = {
                    table: conn.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("schema_meta", "decision", "decision_activation",
                                  "build_claim")
                }
            return real_validate(conn)

        with mock.patch.object(decisions, "validate_store", capturing_validate):
            self._create()
        self.assertEqual(len(seen["tables"]["schema_meta"]), 1)
        for table in ("decision", "decision_activation", "build_claim"):
            self.assertEqual(seen["tables"][table], [],
                             f"a creation temporary must never contain {table} rows")

    def test_every_cleanup_outcome_is_explicit_and_never_suppressed(self):
        """Amendment 9 replaces the ``required=`` quiet-cleanup contract entirely.

        Amendment 8's ``required=False`` swallowed a real unlink failure on the lost-race path,
        so a surviving temporary was invisible to the operator at exactly the moment a competitor
        had become the authority. There is now ONE helper, it reports every outcome, and the
        assertions below cover all five states on every platform - so the guarantee is not left
        to whichever publication primitive the host happens to use.
        """
        temp = self.tmp / ".mcuat_decisions_probe.tmp"
        temp.write_bytes(b"payload\n")
        identity = decisions._file_identity(os.lstat(temp))
        real_unlink = os.unlink

        def refusing_unlink(path, *args, **kwargs):
            if os.path.basename(str(path)) == temp.name:
                raise OSError(13, "synthetic unlink failure")
            return real_unlink(path, *args, **kwargs)

        # 1. A failure is REPORTED, not swallowed, and the file survives.
        with mock.patch("os.unlink", refusing_unlink):
            state = decisions.cleanup_own_temporary(str(temp), identity=identity)
        self.assertEqual(state, decisions.TempCleanup.FAILED)
        self.assertTrue(temp.exists(), "the temporary is never silently discarded")

        # 2. A pathname that now holds a DIFFERENT object is never unlinked.
        other = self.tmp / ".mcuat_decisions_other.tmp"
        other.write_bytes(b"someone else\n")
        other_identity = decisions._file_identity(os.lstat(other))
        self.assertNotEqual(other_identity, identity)
        state = decisions.cleanup_own_temporary(str(other), identity=identity)
        self.assertEqual(state, decisions.TempCleanup.IDENTITY_CHANGED)
        self.assertTrue(other.exists(), "a replacement object is never removed")

        # 3. A directory at the pathname is never removed either.
        directory = self.tmp / ".mcuat_decisions_dir.tmp"
        directory.mkdir()
        state = decisions.cleanup_own_temporary(str(directory), identity=identity)
        self.assertEqual(state, decisions.TempCleanup.NOT_REGULAR)
        self.assertTrue(directory.is_dir())

        # 4. Exactly our own file is removed, and only that one.
        state = decisions.cleanup_own_temporary(str(temp), identity=identity)
        self.assertEqual(state, decisions.TempCleanup.UNLINKED)
        self.assertFalse(temp.exists())
        self.assertTrue(other.exists(), "no unrelated entry is touched")

        # 5. An already-absent pathname is reported truthfully, not as a failure.
        state = decisions.cleanup_own_temporary(str(temp), identity=identity)
        self.assertEqual(state, decisions.TempCleanup.ALREADY_ABSENT)

    def test_cleanup_never_lists_globs_or_sweeps_a_directory(self):
        temp = self.tmp / ".mcuat_decisions_sweep_probe.tmp"
        temp.write_bytes(b"payload\n")
        identity = decisions._file_identity(os.lstat(temp))
        patches = self._no_sweep()
        for patch in patches:
            patch.start()
        try:
            state = decisions.cleanup_own_temporary(str(temp), identity=identity)
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(state, decisions.TempCleanup.UNLINKED)

    def test_the_publication_identity_check_refuses_a_multiply_linked_file(self):
        target = self.tmp / "published_probe.bin"
        target.write_bytes(b"payload\n")
        alias = self.tmp / "published_probe_alias.bin"
        try:
            os.link(str(target), str(alias))
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"hard links unavailable: {type(error).__name__}")
        identity = decisions._file_identity(os.lstat(target))
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions._assert_published_identity(target, identity)
        self.assertEqual(caught.exception.reason, "store_multiple_links")
        self.assertTrue(alias.exists(), "the alias is never removed by the check")

    def test_a_decision_command_reports_what_creation_actually_achieved(self):
        code, out = self._approve()
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertIs(summary["decision_store_created"], True)
        self.assertIn(summary["decision_store_durability"],
                      ("windows_move_write_through", "posix_link_and_directory_fsync"))
        self.assertIn(summary["decision_store_temp_cleanup"], ("not_required", "unlinked"))
        # A second decision finds the store already there and says so.
        code, out = self._approve()
        self.assertEqual(code, 0, out)
        self.assertIs(json.loads(out)["decision_store_created"], False)


@unittest.skipUnless(decisions.IS_WINDOWS, "Windows publication contract")
class WindowsPublicationTests(_PublicationHarness):
    """Lock section 7, Windows: a same-volume no-replace, write-through move.

    A move leaves exactly one name, so the multiply-linked-database hazard never arises here.
    Windows exposes no directory-handle fsync, so `MOVEFILE_WRITE_THROUGH` is the strongest
    durability primitive available and is the only one reported.
    """

    def test_a_successful_move_leaves_one_name_and_no_alias(self):
        result = self._create()
        self.assertEqual(result.durability, "windows_move_write_through")
        self.assertEqual(result.temp_cleanup, "not_required")
        self._assert_final_is_single_named_and_canonical()

    def test_the_real_windows_move_refuses_an_occupied_destination(self):
        # Exercises the real MoveFileExW wrapper, not a stand-in.
        source = self.tmp / "move_source.bin"
        destination = self.tmp / "move_destination.bin"
        source.write_bytes(b"source\n")
        destination.write_bytes(b"destination\n")
        with self.assertRaises(OSError) as caught:
            decisions._windows_no_replace_move(source, destination)
        self.assertTrue(decisions._is_collision(caught.exception))
        self.assertEqual(destination.read_bytes(), b"destination\n",
                         "an occupied destination is never overwritten")
        self.assertTrue(source.exists(), "the source survives a refused move")

    def test_the_real_windows_move_publishes_and_removes_the_source(self):
        source = self.tmp / "move_source.bin"
        destination = self.tmp / "move_destination.bin"
        source.write_bytes(b"payload\n")
        decisions._windows_no_replace_move(source, destination)
        self.assertFalse(source.exists(), "a move leaves no second name")
        self.assertEqual(destination.read_bytes(), b"payload\n")
        self.assertEqual(getattr(os.lstat(destination), "st_nlink", 1), 1)

    def test_a_competing_destination_is_left_byte_for_byte_untouched(self):
        winner = self._second_canonical_store(name="winner")
        expected = winner.read_bytes()

        real_move = decisions._windows_no_replace_move

        def move_after_a_competitor_wins(source, destination):
            if not os.path.lexists(destination):
                shutil.copyfile(str(winner), str(destination))
            return real_move(source, destination)

        with mock.patch.object(decisions, "_windows_no_replace_move",
                               move_after_a_competitor_wins):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_not_absent")
        self.assertEqual(self._store_path().read_bytes(), expected)
        self.assertEqual(self._temporaries(), [], "only our own temporary is removed")

    def test_a_vanished_source_is_reported_as_a_creation_failure(self):
        def vanished(source, destination):
            raise FileNotFoundError(2, "synthetic missing source")

        with mock.patch.object(decisions, "_windows_no_replace_move", vanished):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_create_failed")
        self.assertFalse(os.path.lexists(self._store_path()))

    def test_a_generic_move_failure_leaves_the_final_path_absent(self):
        def failing(source, destination):
            raise OSError(1234, "synthetic move failure")

        with mock.patch.object(decisions, "_windows_no_replace_move", failing):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_create_failed")
        self.assertFalse(os.path.lexists(self._store_path()))

    def test_a_published_file_with_a_different_identity_is_refused(self):
        def copying_move(source, destination):
            # A copy, not a move: the destination is a DIFFERENT file, and the source alias
            # survives - exactly what the identity and link-count checks exist to catch.
            shutil.copyfile(str(source), str(destination))

        with mock.patch.object(decisions, "_windows_no_replace_move", copying_move):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_identity_changed")

    def test_a_surviving_temporary_alias_is_reported_not_ignored(self):
        real_move = decisions._windows_no_replace_move

        def move_then_recreate_the_source(source, destination):
            real_move(source, destination)
            Path(source).write_bytes(b"resurrected alias\n")

        with mock.patch.object(decisions, "_windows_no_replace_move",
                               move_then_recreate_the_source):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_temp_cleanup_incomplete")
        self.assertIsNotNone(caught.exception.temp_basename)
        self.assertTrue(caught.exception.temp_basename.startswith(".mcuat_decisions_"))
        self.assertNotIn(str(self.tmp), caught.exception.temp_basename,
                         "only a basename is ever surfaced")


@unittest.skipIf(decisions.IS_WINDOWS, "POSIX publication contract")
class PosixPublicationTests(_PublicationHarness):
    """Lock section 7, POSIX: no-replace hard link, both directory fsyncs, one final name."""

    def test_a_successful_publication_reports_both_directory_fsyncs(self):
        result = self._create()
        self.assertEqual(result.durability, "posix_link_and_directory_fsync")
        self.assertEqual(result.temp_cleanup, "unlinked")
        self._assert_final_is_single_named_and_canonical()

    def test_no_sqlite_connection_is_open_during_link_publication(self):
        real_link = os.link
        proved = {"exclusive": False}

        def link_with_lock_proof(source, destination, *args, **kwargs):
            # Descriptor-relative publication passes basenames plus src_dir_fd/dst_dir_fd, so the
            # temporary is resolved against the state parent and every argument is forwarded.
            temp = self._link_target(source)
            # If any connection still held the temporary, BEGIN EXCLUSIVE would fail.
            probe = sqlite3.connect(str(temp), timeout=0.2, isolation_level=None)
            try:
                probe.execute("BEGIN EXCLUSIVE")
                probe.execute("ROLLBACK")
                proved["exclusive"] = True
            finally:
                probe.close()
            for suffix in decisions.STORE_SIDECAR_SUFFIXES:
                self.assertFalse(os.path.lexists(str(temp) + suffix))
            return real_link(source, destination, *args, **kwargs)

        with mock.patch("os.link", link_with_lock_proof):
            self._create()
        self.assertTrue(proved["exclusive"],
                        "the temporary must be unlocked when it is published")

    def test_an_occupied_destination_is_preserved_byte_for_byte(self):
        winner = self._second_canonical_store(name="winner")
        expected = winner.read_bytes()
        real_link = os.link

        def link_after_a_competitor_wins(source, destination, *args, **kwargs):
            # The destination arrives as a basename under descriptor-relative publication, so the
            # competitor's store is planted at the resolved final path before the real link runs
            # and correctly refuses an occupied destination.
            final = self._link_target(destination)
            if not os.path.lexists(final):
                shutil.copyfile(str(winner), str(final))
            return real_link(source, destination, *args, **kwargs)

        with mock.patch("os.link", link_after_a_competitor_wins):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_not_absent")
        self.assertEqual(self._store_path().read_bytes(), expected)
        self.assertEqual(self._temporaries(), [])

    def test_a_first_directory_fsync_failure_is_durability_uncertain(self):
        calls = {"n": 0}
        real_fsync = decisions._fsync_directory

        def failing_first(directory):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(5, "synthetic directory fsync failure")
            return real_fsync(directory)

        with mock.patch.object(decisions, "_fsync_directory", failing_first):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_publication_uncertain")
        self.assertTrue(os.path.lexists(self._store_path()),
                        "the published store is never rolled back or deleted")
        self.assertEqual(len(self._temporaries()), 1,
                         "the temporary is left for controlled recovery")

    def test_a_second_directory_fsync_failure_is_durability_uncertain(self):
        calls = {"n": 0}
        real_fsync = decisions._fsync_directory

        def failing_second(directory):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(5, "synthetic directory fsync failure")
            return real_fsync(directory)

        with mock.patch.object(decisions, "_fsync_directory", failing_second):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_publication_uncertain")
        self.assertTrue(os.path.lexists(self._store_path()))
        self.assertEqual(self._temporaries(), [], "the temporary was already removed")

    def test_a_temporary_unlink_failure_is_reported_not_suppressed(self):
        real_unlink = os.unlink

        def refusing_unlink(path, *args, **kwargs):
            if os.path.basename(str(path)).startswith(".mcuat_decisions_"):
                raise OSError(13, "synthetic unlink failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch("os.unlink", refusing_unlink):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_temp_cleanup_incomplete")
        self.assertIsNotNone(caught.exception.temp_basename)
        self.assertTrue(caught.exception.temp_basename.startswith(".mcuat_decisions_"))
        self.assertNotIn("/", caught.exception.temp_basename)
        self.assertTrue(os.path.lexists(self._store_path()),
                        "the published store is never deleted to tidy up")
        # The store must not be usable while it is reachable under two names.
        with self.assertRaises(decisions.DecisionStoreError) as reuse:
            decisions.inspect_store(self._store_path())
        self.assertEqual(reuse.exception.reason, "store_multiple_links")

    def test_a_multiply_linked_final_store_is_refused_after_publication(self):
        self._create()
        alias = self.tmp / "second_name.sqlite3"
        os.link(str(self._store_path()), str(alias))
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.inspect_store(self._store_path())
        self.assertEqual(caught.exception.reason, "store_multiple_links")
        self.assertTrue(alias.exists(), "the alias is never removed by the tool")

    def test_a_published_file_with_a_different_identity_is_refused(self):
        def copying_link(source, destination, *args, **kwargs):
            # The injected fault IS "copy instead of link", so the real os.link is deliberately
            # NOT called: forwarding to it would create a genuine hard link, the identity would
            # match, and the property under test would evaporate. The signature accepts and
            # ignores src_dir_fd/dst_dir_fd; both names are resolved against the state parent
            # because descriptor-relative publication supplies basenames.
            shutil.copyfile(str(self._link_target(source)),
                            str(self._link_target(destination)))

        with mock.patch("os.link", copying_link):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._create()
        self.assertEqual(caught.exception.reason, "store_identity_changed")

    def test_publication_never_lists_or_sweeps_a_directory(self):
        patches = self._no_sweep()
        for patch in patches:
            patch.start()
        try:
            self._create()
        finally:
            for patch in patches:
                patch.stop()
        self._assert_final_is_single_named_and_canonical()


class CommitRecoveryContractTests(_TriageHarness):
    """Lock sections 3 and 4: every COMMIT recovery goes through the locked read-only path."""

    def test_recovery_returns_committed_only_for_the_exact_row_and_hash(self):
        record = self._prepare_activated_approval()
        state, row = decisions.recover_decision_commit(
            self._store_path(), record["decision_id"], record["record_hash"]
        )
        self.assertEqual(state, decisions.CommitState.COMMITTED)
        self.assertEqual(row["decision_id"], record["decision_id"])

        state, row = decisions.recover_decision_commit(
            self._store_path(), record["decision_id"], "sha256:" + ("0" * 64)
        )
        self.assertEqual(state, decisions.CommitState.UNCERTAIN,
                         "a hash mismatch is never reported as committed or absent")
        self.assertIsNone(row)

    def test_recovery_returns_absent_only_when_definitively_absent(self):
        self._prepare_activated_approval()
        state, row = decisions.recover_claim_commit(
            self._store_path(), "claim_" + ("a" * 32), "sha256:" + ("1" * 64)
        )
        self.assertEqual(state, decisions.CommitState.ABSENT)
        self.assertIsNone(row)

        # A store that does not exist at all is also a definite negative.
        state, row = decisions.recover_claim_commit(
            self.tmp / "absent_dir" / decisions.DECISION_STORE_NAME,
            "claim_" + ("a" * 32), "sha256:" + ("1" * 64),
        )
        self.assertEqual(state, decisions.CommitState.ABSENT)

    def test_recovery_validates_globally_before_concluding(self):
        # A malformed row under an UNRELATED source must turn a lookup that would otherwise
        # have said "absent" into "uncertain" - so it can never authorise a retry.
        self._prepare_activated_approval()
        other = self._record("hold", srid="srcrec_" + ("b" * 64),
                             fingerprint="fp_" + ("c" * 64))
        other["record_hash"] = "sha256:" + ("d" * 64)
        self._hostile_pending(other)
        before = self._store_snapshot()
        state, row = decisions.recover_claim_commit(
            self._store_path(), "claim_" + ("a" * 32), "sha256:" + ("1" * 64)
        )
        self.assertEqual(state, decisions.CommitState.UNCERTAIN)
        self.assertIsNone(row)
        self.assertEqual(self._store_snapshot(), before)

    def test_recovery_preserves_a_sidecar_bearing_store_byte_for_byte(self):
        self._prepare_activated_approval()
        self._write_sidecar("-journal", payload=b"crash residue\n")
        before = self._store_snapshot()
        for recover, args in (
            (decisions.recover_decision_commit,
             ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
            (decisions.recover_activation_commit,
             ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
            (decisions.recover_claim_commit,
             ("claim_" + ("0" * 32), "sha256:" + ("0" * 64))),
        ):
            with self.subTest(recovery=recover.__name__):
                state, row = recover(self._store_path(), *args)
                self.assertEqual(state, decisions.CommitState.UNCERTAIN)
                self.assertIsNone(row)
                self.assertEqual(self._store_snapshot(), before)


# =========================================================================== #
# Amendment 9 (design lock DL-113-A9-001)
#
# Three defects survived Amendment 8:
#   1. publication uncertainty was not mechanically STICKY across process restart - a store that
#      was published but never proven durable looked ordinary to the next process;
#   2. a lost first-creator's cleanup failure was SILENTLY SUPPRESSED by `required=False`;
#   3. first-use creation did not establish a TRUSTED PRE-EXISTING PARENT before mutating, and
#      recursively created whatever chain of directories was missing.
#
# The suites below are the evidence for the fix. Every fixture is synthetic and disposable; no
# test touches AutoCount, the AutoCount VM, live n8n, Google Sheets, SMB or shared-folder state,
# a private ledger or store, real member data, probe evidence, or any credential.
# =========================================================================== #
class _StatShim:
    """A mutable stand-in for one ``os.stat_result``, so a single field can be forced.

    ``os.stat_result`` is immutable, and the properties under test - a device transition, a
    reparse attribute, a link count - are ones a synthetic fixture cannot always produce for
    real on both platforms. Copying the real values and overriding exactly one keeps every
    other check honest.
    """

    _FIELDS = ("st_mode", "st_dev", "st_ino", "st_nlink", "st_size", "st_file_attributes")

    def __init__(self, real, **overrides):
        for field in self._FIELDS:
            setattr(self, field, getattr(real, field, 0))
        for field, value in overrides.items():
            setattr(self, field, value)


class _AdmissionHarness(_TriageHarness):
    """Shared helpers for the Amendment 9 admission, restart and reconciliation suites."""

    # Every classifier that means "this store may not be operated on". A restart consumer is
    # allowed to report any of them; it is never allowed to report success.
    NON_OPERATIONAL_REFUSALS = (
        "store_not_admitted",
        "store_admission_invalid",
        "store_admission_uncertain",
        "store_multiple_links",
        "store_sidecar_present",
        "store_identity_changed",
        "store_temp_identity_changed",
    )

    def _admission_rows(self):
        """Admission rows read on a RAW connection, so a non-admitted store can be inspected."""
        raw = self._raw()
        try:
            return raw.execute(
                "SELECT * FROM store_admission ORDER BY singleton"
            ).fetchall()
        finally:
            raw.close()

    def _admission_row(self):
        rows = self._admission_rows()
        self.assertEqual(len(rows), 1, "exactly one admission row is expected here")
        return rows[0]

    def _canonical_admission_record(self, **overrides):
        """A canonical admission record bound to the store file that currently exists."""
        info = os.lstat(self._store_path())
        record = {
            "admission_id": "adm_" + uuid.uuid4().hex,
            "operation_id": "sop_" + uuid.uuid4().hex,
            "admission_mode": decisions.ADMISSION_MODE_CREATED,
            "admitted_at": iso(datetime.now(timezone.utc)),
            "schema_version": decisions.SCHEMA_VERSION,
            "durability": (decisions.DURABILITY_WINDOWS_CREATE if decisions.IS_WINDOWS
                           else decisions.DURABILITY_POSIX_CREATE),
            "volume_identity": decisions.normalised_volume_identity(info),
            "file_identity": decisions.normalised_file_identity(info),
        }
        record.update(overrides)
        record.setdefault("record_hash", decisions.admission_record_hash(record))
        return record

    def _strip_admission(self):
        """Remove the admission row the way only a FOREIGN writer could.

        The immutable triggers forbid DELETE, so the fixture drops and restores the trigger -
        which is exactly what "written by something other than this tool" means, and is the
        state a crashed pre-admission creation leaves behind.
        """
        restore = next(
            statement for statement in decisions._SCHEMA_STATEMENTS
            if "TRIGGER store_admission_block_delete" in statement
        )
        raw = self._raw()
        try:
            raw.execute("DROP TRIGGER store_admission_block_delete")
            raw.execute("DELETE FROM store_admission")
            raw.execute(restore.strip())
            raw.commit()
        finally:
            raw.close()
        self.assertEqual(self._admission_rows(), [])
        return self._store_path()

    def _replace_admission(self, **overrides):
        """Replace the admission row with a canonically hashed one carrying ``overrides``."""
        self._strip_admission()
        record = self._canonical_admission_record(**overrides)
        raw = self._raw()
        try:
            decisions.insert_admission(raw, record)
            raw.commit()
        finally:
            raw.close()
        return record

    def _non_admitted_store(self):
        """A complete, readable, canonical store carrying NO admission fact."""
        self._create_store_for_fixture()
        return self._strip_admission()

    def _assert_readable_and_canonical(self):
        """Prove the store really is readable and structurally canonical.

        This is the whole point of the admission fact: readability, a valid header, a canonical
        schema, an intact version row, one hard link and no sidecars are ALL true here, and none
        of them grants any authority.
        """
        raw = self._raw()
        try:
            self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                raw.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0],
                decisions.SCHEMA_VERSION,
            )
            names = {row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            self.assertIn("store_admission", names)
        finally:
            raw.close()
        info = os.lstat(self._store_path())
        self.assertTrue(stat.S_ISREG(info.st_mode))
        for sidecar in self._sidecar_paths():
            self.assertFalse(os.path.lexists(sidecar))


class StoreAdmissionSchemaTests(_AdmissionHarness):
    """Lock section 2: one canonical, append-only, singleton admission table."""

    def test_a_new_store_has_the_exact_admission_table_and_triggers(self):
        self._create_store_for_fixture()
        raw = self._raw()
        try:
            columns = tuple(
                row["name"] for row in raw.execute("PRAGMA table_info(store_admission)")
            )
            triggers = {
                row[0] for row in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                    "AND tbl_name = 'store_admission'"
                )
            }
        finally:
            raw.close()
        self.assertEqual(columns, decisions.ADMISSION_COLUMNS)
        self.assertEqual(
            triggers, {"store_admission_block_update", "store_admission_block_delete"}
        )
        self.assertIn("store_admission", decisions._REQUIRED_TABLES)
        for trigger in triggers:
            self.assertIn(trigger, decisions._REQUIRED_TRIGGERS)

    def test_the_canonical_hash_covers_every_authority_bearing_field(self):
        # The singleton key and the hash itself are the only excluded columns, so no authority
        # field can be altered without the hash ceasing to recompute.
        self.assertEqual(
            set(decisions.ADMISSION_COLUMNS) - set(decisions.ADMISSION_HASH_FIELDS),
            {"singleton", "record_hash"},
        )
        # And changing any one of them really does change the hash.
        self._create_store_for_fixture()
        base = self._canonical_admission_record()
        for field, altered in (
            ("admission_id", "adm_" + ("1" * 32)),
            ("operation_id", "sop_" + ("2" * 32)),
            ("admission_mode", decisions.ADMISSION_MODE_RECONCILED),
            ("admitted_at", "2020-01-01T00:00:00+00:00"),
            ("durability", decisions.DURABILITY_POSIX_RECONCILE),
            ("volume_identity", "dev:abc"),
            ("file_identity", "ino:abc"),
        ):
            with self.subTest(field=field):
                variant = dict(base, **{field: altered})
                variant.pop("record_hash")
                self.assertNotEqual(
                    decisions.admission_record_hash(variant), base["record_hash"]
                )

    def test_creation_writes_exactly_one_canonical_admission_row(self):
        result = decisions.create_store_exclusively(self._store_path())
        self.assertEqual(result.admission, decisions.ADMISSION_MODE_CREATED)
        row = self._admission_row()
        self.assertEqual(row["singleton"], 1)
        self.assertEqual(row["admission_mode"], decisions.ADMISSION_MODE_CREATED)
        self.assertEqual(row["operation_id"], result.operation_id)
        self.assertEqual(row["schema_version"], decisions.SCHEMA_VERSION)
        self.assertIn(row["durability"], decisions.ADMISSION_DURABILITY_PRIMITIVES)
        self.assertEqual(row["durability"], result.durability)
        self.assertEqual(row["record_hash"], decisions.admission_record_hash(row))
        self.assertTrue(decisions.ADMISSION_ID_RE.fullmatch(row["admission_id"]))
        self.assertTrue(decisions.STORE_OPERATION_ID_RE.fullmatch(row["operation_id"]))
        self.assertIsNotNone(decisions.parse_aware_timestamp(row["admitted_at"]))
        # The binding really names the published file, not some other object.
        info = os.lstat(self._store_path())
        self.assertEqual(row["volume_identity"], decisions.normalised_volume_identity(info))
        self.assertEqual(row["file_identity"], decisions.normalised_file_identity(info))

    def test_the_admission_row_is_immutable(self):
        self._create_store_for_fixture()
        before = self._store_snapshot()
        raw = self._raw()
        try:
            for sql in (
                "UPDATE store_admission SET admission_mode = 'reconciled'",
                "UPDATE store_admission SET record_hash = 'sha256:' || substr(record_hash, 8)",
                "DELETE FROM store_admission",
            ):
                with self.subTest(sql=sql.split()[0]):
                    with self.assertRaises(sqlite3.DatabaseError):
                        raw.execute(sql)
        finally:
            raw.close()
        self.assertEqual(self._store_snapshot(), before)

    def test_a_second_admission_row_is_mechanically_impossible(self):
        self._create_store_for_fixture()
        record = self._canonical_admission_record()
        raw = self._raw()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                decisions.insert_admission(raw, record)
            raw.rollback()
            # Even bypassing the module's own INSERT, the singleton CHECK refuses any other key.
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT INTO store_admission (singleton, admission_id, operation_id,"
                    " admission_mode, admitted_at, schema_version, durability,"
                    " volume_identity, file_identity, record_hash)"
                    " VALUES (2, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record["admission_id"], record["operation_id"],
                     record["admission_mode"], record["admitted_at"],
                     record["schema_version"], record["durability"],
                     record["volume_identity"], record["file_identity"],
                     record["record_hash"]),
                )
        finally:
            raw.close()
        self.assertEqual(len(self._admission_rows()), 1)

    def test_a_previous_draft_v2_store_without_admission_is_refused_never_augmented(self):
        """A store written by the Amendment 8 draft of this same v2 version.

        It carries the identical ``schema_version`` value, so the version row cannot distinguish
        it. Only the exact canonical DDL can - and the answer must be refusal, not augmentation:
        adding the admission table to somebody else's database is precisely the migration this
        contract forbids.
        """
        omitted = ("store_admission", "store_admission_block_update",
                   "store_admission_block_delete")
        statements = [
            statement for statement in decisions._SCHEMA_STATEMENTS
            if not any(f"TABLE {name}" in statement or f"TRIGGER {name}" in statement
                       for name in omitted)
        ]
        self.assertEqual(len(statements), len(decisions._SCHEMA_STATEMENTS) - 3)
        raw = sqlite3.connect(str(self._store_path()))
        try:
            raw.execute("PRAGMA journal_mode=DELETE")
            for statement in statements:
                raw.execute(statement)
            raw.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (decisions.SCHEMA_VERSION,),
            )
            raw.commit()
        finally:
            raw.close()
        before = self._store_snapshot()
        self._assert_refused_untouched("missing_object")
        self.assertEqual(self._store_snapshot(), before)
        # Nothing was added: the table still does not exist.
        raw = self._raw()
        try:
            names = {row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
        finally:
            raw.close()
        self.assertNotIn("store_admission", names,
                         "an old store is never augmented with the admission table")


class AdmissionValidationModeTests(_AdmissionHarness):
    """Lock section 3: two explicit modes, and no circular admission trust."""

    def _validate(self, mode, *, require_history_empty=False):
        """Run one validation MODE through the real locked read-only session."""
        conn, triage = decisions.open_readonly(self._store_path())
        try:
            if mode == "structural":
                return decisions.validate_structural_zero_admission(
                    conn, require_history_empty=require_history_empty
                )
            return decisions.validate_operational_admission(
                conn, identity=triage.normalised
            )
        finally:
            conn.close()

    def test_structural_mode_accepts_a_newly_created_store_with_no_admission(self):
        self._non_admitted_store()
        self._validate("structural", require_history_empty=True)  # must not raise

    def test_operational_mode_refuses_a_store_with_zero_admission_rows(self):
        self._non_admitted_store()
        self._assert_readable_and_canonical()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._validate("operational")
        self.assertEqual(caught.exception.reason, "store_not_admitted")

    def test_operational_mode_accepts_exactly_one_valid_admission_row(self):
        self._create_store_for_fixture()
        row = self._validate("operational")
        self.assertEqual(row["admission_mode"], decisions.ADMISSION_MODE_CREATED)

    def test_structural_mode_refuses_an_already_admitted_store(self):
        self._create_store_for_fixture()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._validate("structural")
        self.assertEqual(caught.exception.reason, "store_admission_invalid")

    def test_structural_history_mode_refuses_a_store_that_already_has_history(self):
        self._prepare_activated_approval()
        self._strip_admission()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._validate("structural", require_history_empty=True)
        self.assertEqual(caught.exception.reason, "store_reconciliation_history_present")

    def test_more_than_one_admission_row_fails_operational_validation(self):
        # The database makes two rows impossible, so the CARDINALITY BRANCH is exercised by
        # forcing the reader to report two - proving the validator would not accept them even
        # if some future schema change let them exist.
        self._create_store_for_fixture()
        real = self._admission_row()
        with mock.patch.object(decisions, "_admission_rows", lambda conn: [real, real]):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._validate("operational")
        self.assertEqual(caught.exception.reason, "store_admission_invalid")

    def test_a_wrong_admission_hash_fails(self):
        self._create_store_for_fixture()
        self._replace_admission(record_hash="sha256:" + ("a" * 64))
        self._assert_refused_untouched("store_admission_invalid")

    def test_a_wrong_identity_binding_fails(self):
        self._create_store_for_fixture()
        self._replace_admission(file_identity="ino:" + "f" * 12)
        self._assert_refused_untouched("store_admission_invalid")

    def test_a_wrong_volume_binding_fails(self):
        self._create_store_for_fixture()
        self._replace_admission(volume_identity="dev:" + "e" * 12)
        self._assert_refused_untouched("store_admission_invalid")

    def test_an_unsupported_durability_primitive_fails(self):
        self._create_store_for_fixture()
        # The CHECK constraint refuses an unknown value outright, which is the stronger result.
        with self.assertRaises(sqlite3.IntegrityError):
            self._replace_admission(durability="assumed_durable")

    def test_a_durability_value_the_validator_does_not_allow_fails(self):
        # The closed Python enum is enforced independently of the CHECK constraint, so a store
        # whose CHECK was weakened by a foreign writer is still refused.
        self._create_store_for_fixture()
        row = self._admission_row()
        record = {field: row[field] for field in decisions.ADMISSION_HASH_FIELDS}
        record["durability"] = "assumed_durable"
        record["record_hash"] = decisions.admission_record_hash(record)
        self.assertIsNotNone(decisions._validate_admission_row(dict(record, singleton=1)))
        self.assertEqual(
            decisions._validate_admission_row(dict(record, singleton=1)),
            "store_admission_invalid",
        )

    def test_a_naive_admission_timestamp_fails(self):
        self._create_store_for_fixture()
        self._replace_admission(admitted_at="2026-07-28T00:00:00")
        self._assert_refused_untouched("store_admission_invalid")

    def test_a_malformed_admission_timestamp_fails(self):
        self._create_store_for_fixture()
        self._replace_admission(admitted_at="not-a-time")
        self._assert_refused_untouched("store_admission_invalid")

    def test_an_unknown_admission_mode_is_refused_by_the_check_constraint(self):
        self._create_store_for_fixture()
        with self.assertRaises(sqlite3.IntegrityError):
            self._replace_admission(admission_mode="assumed")

    def test_a_malformed_admission_identifier_is_refused(self):
        self._create_store_for_fixture()
        # Length 36 satisfies the CHECK, so only the format check can catch this.
        self._replace_admission(admission_id="adm_" + ("z" * 32))
        self._assert_refused_untouched("store_admission_invalid")

    def test_ordinary_operations_never_reach_the_zero_admission_mode(self):
        """Behavioural proof, not a source reading: the structural mode is never called.

        A reviewer decision, a build preflight and all three COMMIT recoveries run against an
        admitted store with the internal zero-admission validator replaced by a tripwire.
        """
        self._prepare_activated_approval()

        def tripwire(*args, **kwargs):
            raise AssertionError(
                "an ordinary operation must never use zero-admission structural validation"
            )

        with mock.patch.object(
            decisions, "validate_structural_zero_admission", tripwire
        ):
            code, out = self._approve()
            self.assertEqual(code, 0, out)
            code, out = self._build()
            self.assertEqual(code, 0, out)
            for recover, args in (
                (decisions.recover_decision_commit,
                 ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
                (decisions.recover_activation_commit,
                 ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
                (decisions.recover_claim_commit,
                 ("claim_" + ("0" * 32), "sha256:" + ("0" * 64))),
            ):
                with self.subTest(recovery=recover.__name__):
                    recover(self._store_path(), *args)


class AdmissionEnforcementCallSiteTests(_AdmissionHarness):
    """Lock section 6: EVERY operational entry point requires the admission fact.

    One test per call site, so a validator that were skipped at any single one of them is killed
    independently rather than being masked by the others.
    """

    def test_a_reviewer_decision_refuses_a_non_admitted_store(self):
        self._non_admitted_store()
        before = self._store_snapshot()
        code, out = self._approve()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"], "store_not_admitted")
        self.assertIs(summary["decision_store_modified"], False)
        self.assertIs(summary["approval_blocked"], True)
        self.assertEqual(self._store_snapshot(), before)
        self.assertEqual(self._entries(), [], "no audit line is appended")

    def test_a_build_preflight_refuses_a_non_admitted_store(self):
        self._prepare_activated_approval()
        self._strip_admission()
        before = self._store_snapshot()
        code, out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["decision_store_integrity"], "store_not_admitted")
        self.assertEqual(self._store_snapshot(), before)
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._raw_claim_count(), 0)

    def test_an_authority_read_refuses_a_non_admitted_store(self):
        self._prepare_activated_approval()
        self._strip_admission()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.read_validated(
                self._store_path(),
                lambda conn: decisions.resolve_authority(conn, "srcrec_" + ("a" * 64)),
            )
        self.assertEqual(caught.exception.reason, "store_not_admitted")

    def test_the_writer_path_refuses_a_non_admitted_store(self):
        self._non_admitted_store()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.begin_write(self._store_path())
        self.assertEqual(caught.exception.reason, "store_not_admitted")

    def test_every_commit_recovery_refuses_a_non_admitted_store(self):
        self._prepare_activated_approval()
        record = self._stored_decisions()[-1]
        self._strip_admission()
        before = self._store_snapshot()
        for recover, args in (
            (decisions.recover_decision_commit,
             (record["decision_id"], record["record_hash"])),
            (decisions.recover_activation_commit,
             (record["decision_id"], "sha256:" + ("0" * 64))),
            (decisions.recover_claim_commit,
             ("claim_" + ("0" * 32), "sha256:" + ("0" * 64))),
        ):
            with self.subTest(recovery=recover.__name__):
                state, row = recover(self._store_path(), *args)
                self.assertEqual(
                    state, decisions.CommitState.UNCERTAIN,
                    "a non-admitted store must never yield a definite recovery answer",
                )
                self.assertIsNone(row)
                self.assertEqual(self._store_snapshot(), before)

    def test_ensure_store_reports_a_non_admitted_existing_store(self):
        self._non_admitted_store()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.ensure_store(self._store_path())
        self.assertEqual(caught.exception.reason, "store_not_admitted")

    def test_ensure_store_reports_an_existing_admitted_store(self):
        self._create_store_for_fixture()
        self.assertIsNone(decisions.ensure_store(self._store_path()))

    def test_readability_alone_never_grants_authority(self):
        """The single clearest statement of the Amendment 9 fix.

        Every property Amendment 8 relied on is true here - the file is readable, its header is
        valid, its schema is exactly canonical, its version row is correct, it has one hard link
        and no sidecars - and every operational entry point still refuses.
        """
        self._non_admitted_store()
        self._assert_readable_and_canonical()
        for label, call in (
            ("inspect", lambda: decisions.inspect_store(self._store_path())),
            ("read", lambda: decisions.read_validated(self._store_path(), lambda c: None)),
            ("write", lambda: decisions.begin_write(self._store_path())),
            ("ensure", lambda: decisions.ensure_store(self._store_path())),
        ):
            with self.subTest(entry=label):
                with self.assertRaises(decisions.DecisionStoreError) as caught:
                    call()
                self.assertEqual(caught.exception.reason, "store_not_admitted")


class AdmissionCommitRecoveryTests(_AdmissionHarness):
    """Lock section 5 step 9: the admission COMMIT is resolved by exact lookup, never inference."""

    def test_the_exact_row_and_hash_resolve_as_committed(self):
        decisions.create_store_exclusively(self._store_path())
        row = self._admission_row()
        self.assertEqual(
            decisions.recover_admission_commit(
                self._store_path(), row["admission_id"], row["record_hash"]
            ),
            decisions.CommitState.COMMITTED,
        )

    def test_a_structurally_valid_zero_admission_store_resolves_as_absent(self):
        self._non_admitted_store()
        self.assertEqual(
            decisions.recover_admission_commit(
                self._store_path(), "adm_" + ("0" * 32), "sha256:" + ("0" * 64)
            ),
            decisions.CommitState.ABSENT,
        )

    def test_a_different_admission_row_resolves_as_uncertain(self):
        decisions.create_store_exclusively(self._store_path())
        row = self._admission_row()
        for label, args in (
            ("other id", ("adm_" + ("0" * 32), row["record_hash"])),
            ("other hash", (row["admission_id"], "sha256:" + ("0" * 64))),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    decisions.recover_admission_commit(self._store_path(), *args),
                    decisions.CommitState.UNCERTAIN,
                )

    def test_sidecar_residue_resolves_as_uncertain(self):
        decisions.create_store_exclusively(self._store_path())
        row = self._admission_row()
        self._write_sidecar("-journal", payload=b"crash residue\n")
        before = self._store_snapshot()
        self.assertEqual(
            decisions.recover_admission_commit(
                self._store_path(), row["admission_id"], row["record_hash"]
            ),
            decisions.CommitState.UNCERTAIN,
        )
        self.assertEqual(self._store_snapshot(), before)

    def test_an_invalid_admission_row_resolves_as_uncertain(self):
        self._create_store_for_fixture()
        record = self._replace_admission(record_hash="sha256:" + ("b" * 64))
        self.assertEqual(
            decisions.recover_admission_commit(
                self._store_path(), record["admission_id"], record["record_hash"]
            ),
            decisions.CommitState.UNCERTAIN,
        )

    def test_an_identity_mismatch_resolves_as_uncertain(self):
        self._create_store_for_fixture()
        record = self._replace_admission(file_identity="ino:" + "d" * 10)
        self.assertEqual(
            decisions.recover_admission_commit(
                self._store_path(), record["admission_id"], record["record_hash"]
            ),
            decisions.CommitState.UNCERTAIN,
        )

    def test_an_unreadable_store_resolves_as_uncertain_not_absent(self):
        decisions.create_store_exclusively(self._store_path())
        row = self._admission_row()

        def failing_header(path):
            raise decisions.DecisionStoreError(
                "synthetic unreadable header", reason="store_unreadable"
            )

        with mock.patch.object(decisions, "_read_store_header", failing_header):
            self.assertEqual(
                decisions.recover_admission_commit(
                    self._store_path(), row["admission_id"], row["record_hash"]
                ),
                decisions.CommitState.UNCERTAIN,
            )

    def test_a_missing_store_resolves_as_uncertain_not_absent(self):
        # A publication that already succeeded followed by a vanished store is NOT a definite
        # negative: the admission question cannot be answered about a store that is gone.
        self._create_store_for_fixture()
        os.unlink(self._store_path())
        self.assertEqual(
            decisions.recover_admission_commit(
                self._store_path(), "adm_" + ("0" * 32), "sha256:" + ("0" * 64)
            ),
            decisions.CommitState.UNCERTAIN,
        )

    def test_recovery_never_retries_automatically(self):
        decisions.create_store_exclusively(self._store_path())
        row = self._admission_row()
        opens = {"n": 0}
        real_connect = decisions._connect_uri

        def counting(path, query):
            opens["n"] += 1
            return real_connect(path, query)

        with mock.patch.object(decisions, "_connect_uri", counting):
            decisions.recover_admission_commit(
                self._store_path(), row["admission_id"], row["record_hash"]
            )
        self.assertEqual(opens["n"], 1, "exactly one bounded attempt, never a retry loop")

    def test_a_verification_failure_after_a_proven_admission_says_so_truthfully(self):
        """The final operational proof can fail AFTER the admission row is committed.

        Under real concurrency this is reached when a peer, having just seen the admission appear,
        opens its own write transaction and its rollback journal is observed here. The operation
        still fails closed - but reporting `published_not_admitted` would be FALSE: the admission
        fact is committed, so a later process finds an operational store and controlled
        reconciliation is not required. Forced deterministically here rather than left to a race.
        """
        real_inspect = decisions.inspect_store
        calls = {"n": 0}

        def failing_final_inspect(path):
            calls["n"] += 1
            if calls["n"] == 1:          # creation's own final operational proof
                raise decisions.DecisionStoreError(
                    "synthetic peer rollback journal observed",
                    reason="store_sidecar_present",
                )
            return real_inspect(path)

        with mock.patch.object(decisions, "inspect_store", failing_final_inspect):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_sidecar_present")
        self.assertEqual(caught.exception.final_path_state,
                         decisions.PUBLISHED_AND_ADMITTED)
        self.assertNotEqual(caught.exception.final_path_state,
                            decisions.PUBLISHED_NOT_ADMITTED,
                            "an admitted store must never be reported as non-admitted")
        # The admission row really is committed, so the store IS operational to anyone else.
        self.assertEqual(len(self._admission_rows()), 1)
        decisions.inspect_store(self._store_path())
        code, out = self._approve()
        self.assertEqual(code, 0, out)
        self.assertIs(json.loads(out)["decision_store_created"], False,
                      "the already-admitted store is reused, never recreated")

    def test_a_pre_admission_verification_failure_is_reported_as_not_admitted(self):
        """The mirror case, so the two classifiers cannot be confused for one another."""
        def failing_structural(*args, **kwargs):
            raise decisions.DecisionStoreError(
                "synthetic post-publication inspection failure", reason="store_corrupt"
            )

        with mock.patch.object(decisions, "_read_structural", failing_structural):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.final_path_state,
                         decisions.PUBLISHED_NOT_ADMITTED)
        self.assertEqual(self._admission_rows(), [], "no admission fact exists")

    def test_recovery_never_infers_the_result_from_the_exception(self):
        """A raised COMMIT that really committed is resolved as COMMITTED, and vice versa."""
        real_commit = decisions._commit
        calls = {"n": 0}

        def commit_then_raise(conn):
            calls["n"] += 1
            if calls["n"] == 2:            # the admission commit
                real_commit(conn)          # ... which genuinely succeeds
                raise sqlite3.OperationalError("simulated failure after a real commit")
            return real_commit(conn)

        with mock.patch.object(decisions, "_commit", commit_then_raise):
            result = decisions.create_store_exclusively(self._store_path())
        self.assertEqual(result.admission, decisions.ADMISSION_MODE_CREATED)
        self.assertEqual(len(self._admission_rows()), 1)
        decisions.inspect_store(self._store_path())


class StickyCreationRestartTests(_AdmissionHarness):
    """Lock section 4 and 11: publication uncertainty is MECHANICALLY STICKY across restart.

    This is the Amendment 8 defect stated as evidence. For every way creation can fail after the
    final path becomes visible, a REAL NEW PROCESS then attempts a reviewer decision, a build
    preflight and all three COMMIT recoveries. Every ordinary operation must refuse, and every
    recovery must report UNCERTAIN - because the visible store carries no admission fact, and
    nothing about being readable, canonical, correctly versioned, singly linked or sidecar-free
    is allowed to substitute for one.

    Both the injector and the consumers run as separate interpreters, so no in-process patch,
    cached module state or surviving object can be doing the work.
    """

    # Stages that run on every platform.
    PORTABLE_STAGES = (
        "publication_then_abort",
        "post_publication_inspection",
        "before_admission_transaction",
        "admission_insert",
        "admission_commit_before_write",
    )
    POSIX_STAGES = (
        "first_parent_fsync",
        "temp_unlink",
        "second_parent_fsync",
    )
    WINDOWS_STAGES = (
        "move_then_identity_check",
        "move_then_temp_path_check",
    )

    def _stages(self):
        return self.PORTABLE_STAGES + (
            self.WINDOWS_STAGES if decisions.IS_WINDOWS else self.POSIX_STAGES
        )

    def _injector_script(self):
        """A child process that creates the store with ONE failure injected at ``sys.argv[1]``."""
        return textwrap.dedent(
            f"""
            import json, os, sqlite3, sys
            from pathlib import Path
            from unittest import mock
            sys.path.insert(0, {str(SCRIPTS)!r})
            import member_create_uat_decision_store as decisions

            stage = sys.argv[1]
            store = {str(self._store_path())!r}
            patches = []

            publish_name = ("_publish_windows" if decisions.IS_WINDOWS
                            else "_publish_posix")
            real_publish = getattr(decisions, publish_name)

            if stage == "publication_then_abort":
                # Publication genuinely succeeds; the process then dies before admission.
                def publish_then_abort(*a, **k):
                    real_publish(*a, **k)
                    raise decisions.DecisionStoreError(
                        "synthetic abort immediately after publication",
                        reason="store_create_failed",
                    )
                patches.append(mock.patch.object(decisions, publish_name,
                                                 publish_then_abort))
            elif stage == "post_publication_inspection":
                def failing_structural(*a, **k):
                    raise decisions.DecisionStoreError(
                        "synthetic post-publication inspection failure",
                        reason="store_corrupt",
                    )
                patches.append(mock.patch.object(decisions, "_read_structural",
                                                 failing_structural))
            elif stage == "before_admission_transaction":
                def failing_admit(*a, **k):
                    raise decisions.DecisionStoreError(
                        "synthetic failure before the admission transaction",
                        reason="store_admission_uncertain",
                    )
                patches.append(mock.patch.object(decisions, "_admit_store", failing_admit))
            elif stage == "admission_insert":
                def failing_insert(conn, record):
                    raise sqlite3.OperationalError("synthetic admission insert failure")
                patches.append(mock.patch.object(decisions, "insert_admission",
                                                 failing_insert))
            elif stage == "admission_commit_before_write":
                real_commit = decisions._commit
                calls = {{"n": 0}}
                def commit_before(conn):
                    calls["n"] += 1
                    if calls["n"] == 2:      # 1 = schema, 2 = admission
                        raise sqlite3.OperationalError(
                            "synthetic admission commit failure before write"
                        )
                    return real_commit(conn)
                patches.append(mock.patch.object(decisions, "_commit", commit_before))
            elif stage == "first_parent_fsync":
                real_fsync = decisions._fsync_directory
                calls = {{"n": 0}}
                def failing_first(parent):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise OSError(5, "synthetic first directory fsync failure")
                    return real_fsync(parent)
                patches.append(mock.patch.object(decisions, "_fsync_directory",
                                                 failing_first))
            elif stage == "second_parent_fsync":
                real_fsync = decisions._fsync_directory
                calls = {{"n": 0}}
                def failing_second(parent):
                    calls["n"] += 1
                    if calls["n"] == 2:
                        raise OSError(5, "synthetic second directory fsync failure")
                    return real_fsync(parent)
                patches.append(mock.patch.object(decisions, "_fsync_directory",
                                                 failing_second))
            elif stage == "temp_unlink":
                real_unlink = os.unlink
                def refusing_unlink(path, *a, **k):
                    if os.path.basename(str(path)).startswith(".mcuat_decisions_"):
                        raise OSError(13, "synthetic unlink failure")
                    return real_unlink(path, *a, **k)
                patches.append(mock.patch("os.unlink", refusing_unlink))
            elif stage == "move_then_identity_check":
                def failing_identity(final, expected):
                    raise decisions.DecisionStoreError(
                        "synthetic post-move identity failure",
                        reason="store_identity_changed",
                    )
                patches.append(mock.patch.object(decisions, "_assert_published_identity",
                                                 failing_identity))
            elif stage == "move_then_temp_path_check":
                real_move = decisions._windows_no_replace_move
                def move_then_resurrect(source, destination):
                    real_move(source, destination)
                    Path(source).write_bytes(b"resurrected alias\\n")
                patches.append(mock.patch.object(decisions, "_windows_no_replace_move",
                                                 move_then_resurrect))
            else:
                raise SystemExit("unknown stage: " + stage)

            result = {{"stage": stage}}
            for patch in patches:
                patch.start()
            try:
                created = decisions.create_store_exclusively(store)
                result["outcome"] = "created"
                result["admission"] = created.admission
            except decisions.DecisionStoreError as error:
                result["outcome"] = "refused"
                result["reason"] = error.reason
                result["final_path_state"] = error.final_path_state
                result["temp_basename"] = error.temp_basename
            finally:
                for patch in reversed(patches):
                    patch.stop()
            result["store_visible"] = os.path.lexists(store)
            print(json.dumps(result, sort_keys=True))
            """
        )

    def _consumer_script(self):
        """A FRESH process that attempts every ordinary operation and every recovery."""
        return textwrap.dedent(
            f"""
            import io, json, os, sys
            from contextlib import redirect_stdout
            sys.path.insert(0, {str(SCRIPTS)!r})
            import member_create_uat_approval as approval
            import member_create_uat_decision_store as decisions

            store = {str(self._store_path())!r}
            common = ["--input", {str(self.form)!r},
                      "--decision-rows", {str(self.rows)!r},
                      "--row-number", "2", "--ledger", {str(self.ledger)!r}]

            def run(argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = approval.main(argv)
                return code, buf.getvalue()

            result = {{}}
            code, out = run(["approve", "--reviewer", "digital"] + common)
            result["decision"] = {{"code": code, "summary": json.loads(out)}}
            code, out = run(["build-package"] + common
                            + ["--package-out", {str(self.package)!r}])
            result["build"] = {{"code": code, "summary": json.loads(out)}}
            for label, recover, args in (
                ("decision_recovery", decisions.recover_decision_commit,
                 ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
                ("activation_recovery", decisions.recover_activation_commit,
                 ("dec_" + ("0" * 32), "sha256:" + ("0" * 64))),
                ("claim_recovery", decisions.recover_claim_commit,
                 ("claim_" + ("0" * 32), "sha256:" + ("0" * 64))),
            ):
                state, _row = recover(store, *args)
                result[label] = state
            result["store_visible"] = os.path.lexists(store)
            print(json.dumps(result, sort_keys=True))
            """
        )

    def _run_child(self, script, *args):
        proc = subprocess.run(
            [sys.executable, "-c", script, *args],
            capture_output=True, text=True, timeout=180,
        )
        self.assertNotIn("Traceback", proc.stderr, proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def _assert_blocked_everywhere(self, consumed):
        """A fresh process refused every ordinary operation and resolved nothing definitely."""
        for entry in ("decision", "build"):
            with self.subTest(operation=entry):
                report = consumed[entry]
                self.assertNotEqual(report["code"], 0, report)
                self.assertEqual(report["summary"]["status"],
                                 "decision_store_integrity_uncertain", report)
                self.assertIn(report["summary"]["decision_store_integrity"],
                              self.NON_OPERATIONAL_REFUSALS, report)
                self.assertIs(report["summary"]["approval_blocked"], True)
        for entry in ("decision_recovery", "activation_recovery", "claim_recovery"):
            with self.subTest(recovery=entry):
                self.assertEqual(consumed[entry], decisions.CommitState.UNCERTAIN,
                                 "a non-admitted store must never resolve a commit")
        # Nothing was published, reserved or claimed by the refusing process.
        self.assertFalse(self.package.exists())
        self.assertEqual(self._reservations(), [])
        self.assertEqual(self._entries(), [], "no audit line is appended")

    def test_every_post_publication_failure_stays_blocked_in_a_new_process(self):
        for stage in self._stages():
            with self.subTest(stage=stage):
                self._reset()
                injected = self._run_child(self._injector_script(), stage)
                self.assertEqual(injected["outcome"], "refused", injected)
                self.assertIs(injected["store_visible"], True,
                              "this stage must leave the final path visible")
                self.assertEqual(self._admission_rows(), [],
                                 "no admission fact may exist")
                # The visible store really is readable and canonical - and still worthless.
                if injected["reason"] != "store_temp_cleanup_incomplete":
                    self._assert_readable_and_canonical()
                consumed = self._run_child(self._consumer_script())
                self._assert_blocked_everywhere(consumed)

    def test_an_admission_commit_that_really_succeeded_is_recovered_in_process(self):
        """The one exception: a raised COMMIT whose row is genuinely there resolves COMMITTED.

        Injected in a REAL new process, so the resolution is proven to come from reopening the
        database rather than from anything the failing process still held.
        """
        script = textwrap.dedent(
            f"""
            import json, os, sqlite3, sys
            from unittest import mock
            sys.path.insert(0, {str(SCRIPTS)!r})
            import member_create_uat_decision_store as decisions

            real_commit = decisions._commit
            calls = {{"n": 0}}

            def commit_then_raise(conn):
                calls["n"] += 1
                if calls["n"] == 2:            # the admission commit
                    real_commit(conn)          # ... which genuinely succeeds
                    raise sqlite3.OperationalError("synthetic failure after a real commit")
                return real_commit(conn)

            result = {{}}
            with mock.patch.object(decisions, "_commit", commit_then_raise):
                try:
                    created = decisions.create_store_exclusively(
                        {str(self._store_path())!r}
                    )
                    result["outcome"] = "created"
                    result["admission"] = created.admission
                except decisions.DecisionStoreError as error:
                    result["outcome"] = "refused"
                    result["reason"] = error.reason
            print(json.dumps(result, sort_keys=True))
            """
        )
        injected = self._run_child(script)
        self.assertEqual(injected["outcome"], "created", injected)
        self.assertEqual(injected["admission"], decisions.ADMISSION_MODE_CREATED)
        self.assertEqual(len(self._admission_rows()), 1)
        # A FRESH process now finds a fully operational store and proceeds normally.
        code, out = self._approve()
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["decision_store_created"], False)
        self.assertEqual(self._build()[0], 0)

    def test_the_second_directory_fsync_failure_blocks_real_restart_consumers(self):
        """Amendment 8 left this store looking ordinary to the next process. It no longer does."""
        if decisions.IS_WINDOWS:
            self.skipTest("POSIX directory-fsync contract")
        injected = self._run_child(self._injector_script(), "second_parent_fsync")
        self.assertEqual(injected["reason"], "store_publication_uncertain")
        self.assertEqual(injected["final_path_state"], decisions.PUBLISHED_NOT_ADMITTED)
        self.assertIs(injected["store_visible"], True)
        self.assertEqual(
            [f for f in os.listdir(self.tmp) if f.startswith(".mcuat_decisions_")], [],
            "the temporary was already removed at this stage",
        )
        self._assert_readable_and_canonical()
        self._assert_blocked_everywhere(self._run_child(self._consumer_script()))

    def test_a_published_not_admitted_store_reports_the_final_path_state(self):
        injected = self._run_child(self._injector_script(), "before_admission_transaction")
        self.assertEqual(injected["final_path_state"], decisions.PUBLISHED_NOT_ADMITTED)
        # The operator report says a new file exists rather than claiming nothing changed.
        code, out = self._run(["approve", "--reviewer", "digital", "--input", str(self.form),
                               "--decision-rows", str(self.rows), "--row-number", "2",
                               "--ledger", str(self.ledger)])
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        summary = json.loads(out)
        self.assertEqual(summary["decision_store_integrity"], "store_not_admitted")
        # The refusing decision command touched nothing, so IT reports no modification; the
        # creating process is the one that reported the published-not-admitted state.
        self.assertIs(summary["decision_store_modified"], False)


class _ParentAdmissionHarness(_AdmissionHarness):
    """Fixtures whose approval-ledger directory is a NESTED state directory.

    The state parent has to be replaceable, retypeable and removable for these tests, which the
    top-level fixture directory (holding the synthetic form and decision rows) is not.
    """

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.ledger = self.state / "member_create_uat_ledger.jsonl"

    def _replace_parent(self):
        """Make the parent PATHNAME resolve to a different directory."""
        displaced = self.tmp / ("displaced_" + uuid.uuid4().hex)
        os.rename(str(self.state), str(displaced))
        os.mkdir(str(self.state))
        return displaced

    def _remove_parent(self):
        shutil.rmtree(str(self.state))

    def _assert_nothing_created_at_all(self, call, reason):
        """A rejected parent produces ZERO of everything, and never opens SQLite."""
        def forbidden_connect(*args, **kwargs):
            raise AssertionError(
                "no SQLite connection may be opened for a rejected state parent"
            )

        before = sorted(os.listdir(self.tmp))
        with mock.patch.object(sqlite3, "connect", forbidden_connect):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                call()
        self.assertEqual(caught.exception.reason, reason)
        self.assertIn(caught.exception.reason, decisions.STORE_INTEGRITY_REASONS)
        self.assertFalse(os.path.lexists(self._store_path()),
                         "zero store files are created")
        self.assertFalse(self.ledger.exists(), "zero audit appends happen")
        self.assertEqual(sorted(os.listdir(self.tmp)), before,
                         "zero directories or files are created")
        if os.path.isdir(self.state):
            self.assertEqual(
                [f for f in os.listdir(self.state) if f.startswith(".mcuat_decisions_")],
                [], "zero temporaries are created",
            )
        return caught.exception

    def _make_symlink(self, link, target, *, directory=True):
        try:
            os.symlink(str(target), str(link), target_is_directory=directory)
        except (OSError, NotImplementedError, AttributeError) as error:
            self.skipTest(f"symlinks unavailable: {type(error).__name__}")

    def _make_junction(self, link, target):
        if not decisions.IS_WINDOWS:
            self.skipTest("junctions are a Windows construct")
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            self.skipTest("junction creation unavailable in this environment")


class TrustedParentAdmissionTests(_ParentAdmissionHarness):
    """Lock section 9: the state parent must ALREADY EXIST and is admitted before any mutation.

    Amendment 8 called ``mkdir(parents=True, exist_ok=True)`` and asked its questions afterwards,
    so a whole chain of directories could be materialised and a pre-existing redirected component
    was followed by every subsequent open. Nothing is created here until the parent is admitted.
    """

    def test_a_missing_parent_creates_absolutely_nothing(self):
        self._remove_parent()
        self._assert_nothing_created_at_all(
            lambda: decisions.create_store_exclusively(self._store_path()),
            "store_parent_missing",
        )
        self.assertFalse(os.path.lexists(self.state),
                         "the required state directory is NEVER created")

    def test_a_missing_intermediate_component_creates_absolutely_nothing(self):
        deeper = self.state / "a" / "b"
        ledger = deeper / "member_create_uat_ledger.jsonl"
        self.assertFalse(deeper.exists())
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.create_store_exclusively(decisions.store_path_for(ledger))
        self.assertEqual(caught.exception.reason, "store_parent_missing")
        self.assertFalse(os.path.lexists(self.state / "a"),
                         "no directory chain is ever created recursively")

    def test_a_reviewer_decision_refuses_a_missing_parent_without_creating_it(self):
        self._remove_parent()
        code, out = self._approve()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["decision_store_integrity"], "store_parent_missing")
        self.assertIs(summary["decision_store_modified"], False)
        self.assertFalse(os.path.lexists(self.state))
        self.assertFalse(self.ledger.exists())

    def test_a_parent_that_is_a_plain_file_creates_absolutely_nothing(self):
        self._remove_parent()
        self.state.write_bytes(b"not a directory\n")
        self._assert_nothing_created_at_all(
            lambda: decisions.create_store_exclusively(self._store_path()),
            "store_parent_untrusted",
        )
        self.assertEqual(self.state.read_bytes(), b"not a directory\n",
                         "the occupying file is never altered or removed")

    def test_a_symlinked_final_parent_is_refused_without_being_followed(self):
        real = self.tmp / "elsewhere"
        real.mkdir()
        self._remove_parent()
        self._make_symlink(self.state, real)
        self._assert_nothing_created_at_all(
            lambda: decisions.create_store_exclusively(self._store_path()),
            "store_parent_untrusted",
        )
        self.assertEqual(os.listdir(real), [],
                         "the redirection target is never written into")

    def test_a_symlinked_intermediate_component_is_refused(self):
        real = self.tmp / "elsewhere"
        (real / "inner").mkdir(parents=True)
        link = self.tmp / "hop"
        self._make_symlink(link, real)
        ledger = link / "inner" / "member_create_uat_ledger.jsonl"
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.create_store_exclusively(decisions.store_path_for(ledger))
        self.assertEqual(caught.exception.reason, "store_parent_untrusted")
        self.assertEqual(os.listdir(real / "inner"), [])

    def test_a_junctioned_final_parent_is_refused(self):
        real = self.tmp / "elsewhere"
        real.mkdir()
        self._remove_parent()
        self._make_junction(self.state, real)
        self._assert_nothing_created_at_all(
            lambda: decisions.create_store_exclusively(self._store_path()),
            "store_parent_untrusted",
        )
        self.assertEqual(os.listdir(real), [])

    def test_a_junctioned_intermediate_component_is_refused(self):
        real = self.tmp / "elsewhere"
        (real / "inner").mkdir(parents=True)
        link = self.tmp / "hop"
        self._make_junction(link, real)
        ledger = link / "inner" / "member_create_uat_ledger.jsonl"
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.create_store_exclusively(decisions.store_path_for(ledger))
        self.assertEqual(caught.exception.reason, "store_parent_untrusted")
        self.assertEqual(os.listdir(real / "inner"), [])

    def test_a_generic_reparse_point_component_is_refused(self):
        """Any reparse TAG is refused, not just the symlink and junction tags.

        Forced through the classification seam because the reparse tag space is open-ended and a
        synthetic fixture cannot create every kind for real on both platforms.
        """
        real_lstat = contract.lstat_no_follow
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

        def reparse_state(path, *, dir_fd=None):
            info = real_lstat(path, dir_fd=dir_fd)
            if info is not None and os.path.basename(str(path)) == self.state.name:
                return _StatShim(info, st_file_attributes=reparse)
            return info

        with mock.patch.object(contract, "lstat_no_follow", reparse_state):
            self._assert_nothing_created_at_all(
                lambda: decisions.create_store_exclusively(self._store_path()),
                "store_parent_untrusted",
            )

    def test_a_component_classification_error_fails_closed(self):
        real_lstat = contract.lstat_no_follow

        def refusing(path, *, dir_fd=None):
            if os.path.basename(str(path)) == self.state.name:
                raise PermissionError(13, "synthetic classification failure")
            return real_lstat(path, dir_fd=dir_fd)

        with mock.patch.object(contract, "lstat_no_follow", refusing):
            self._assert_nothing_created_at_all(
                lambda: decisions.create_store_exclusively(self._store_path()),
                "store_parent_untrusted",
            )

    def test_a_generic_classification_oserror_fails_closed(self):
        real_lstat = contract.lstat_no_follow

        def refusing(path, *, dir_fd=None):
            if os.path.basename(str(path)) == self.state.name:
                raise OSError(5, "synthetic I/O failure")
            return real_lstat(path, dir_fd=dir_fd)

        with mock.patch.object(contract, "lstat_no_follow", refusing):
            self._assert_nothing_created_at_all(
                lambda: decisions.create_store_exclusively(self._store_path()),
                "store_parent_untrusted",
            )

    def test_a_device_or_volume_transition_is_refused(self):
        real_lstat = contract.lstat_no_follow

        def other_device(path, *, dir_fd=None):
            info = real_lstat(path, dir_fd=dir_fd)
            if info is not None and os.path.basename(str(path)) == self.state.name:
                return _StatShim(info, st_dev=info.st_dev ^ 0xABCD)
            return info

        with mock.patch.object(contract, "lstat_no_follow", other_device):
            self._assert_nothing_created_at_all(
                lambda: decisions.create_store_exclusively(self._store_path()),
                "store_parent_unsupported",
            )

    def test_a_relative_component_is_refused_by_the_walker(self):
        # The store path check refuses these first, so the WALKER's own refusal is asserted
        # directly - a defence-in-depth boundary must be tested where it lives.
        for component in (".", ".."):
            with self.subTest(component=component):
                with self.assertRaises(decisions.DecisionStoreError) as caught:
                    decisions._classify_component(component, path=self.state)
                self.assertEqual(caught.exception.reason, "store_parent_untrusted")

    def test_a_store_path_containing_relative_components_is_refused(self):
        for suffix in (os.path.join(".", "x"), os.path.join("..", "x")):
            with self.subTest(suffix=suffix):
                ledger = Path(str(self.state) + os.sep + suffix) / "l.jsonl"
                with self.assertRaises(decisions.DecisionStoreError) as caught:
                    decisions.create_store_exclusively(
                        Path(str(ledger.parent / decisions.DECISION_STORE_NAME))
                    )
                self.assertIn(caught.exception.reason,
                              ("store_path_unsafe", "store_parent_untrusted",
                               "store_parent_missing"))

    def test_the_parent_is_rechecked_at_all_four_locked_points(self):
        real = decisions.TrustedParent.recheck
        calls = {"n": 0}

        def counting(inner_self):
            calls["n"] += 1
            return real(inner_self)

        with mock.patch.object(decisions.TrustedParent, "recheck", counting):
            decisions.create_store_exclusively(self._store_path())
        self.assertGreaterEqual(
            calls["n"], 4,
            "the parent must be rechecked before and after temporary creation, and before "
            "and after publication",
        )

    def _swap_at_recheck(self, ordinal):
        """Replace the parent immediately BEFORE the Nth recheck, then run the real recheck."""
        real = decisions.TrustedParent.recheck
        calls = {"n": 0}
        harness = self

        def wrapper(inner_self):
            calls["n"] += 1
            if calls["n"] == ordinal:
                self.displaced = harness._replace_parent()
            return real(inner_self)

        return mock.patch.object(decisions.TrustedParent, "recheck", wrapper)

    def test_a_parent_replaced_before_temporary_creation_is_detected(self):
        with self._swap_at_recheck(1):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_parent_identity_changed")
        self.assertFalse(os.path.lexists(self._store_path()))
        self.assertEqual(
            [f for f in os.listdir(self.state) if f.startswith(".mcuat_decisions_")], [],
            "no temporary was created at all",
        )

    def test_a_parent_replaced_after_temporary_creation_is_detected(self):
        with self._swap_at_recheck(2):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_parent_identity_changed")
        self.assertFalse(os.path.lexists(self._store_path()),
                         "the final path is never created")

    def test_a_parent_replaced_before_publication_is_detected(self):
        with self._swap_at_recheck(3):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_parent_identity_changed")
        self.assertFalse(os.path.lexists(self._store_path()))

    def test_a_parent_replaced_after_publication_is_detected_and_not_admitted(self):
        with self._swap_at_recheck(4):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_parent_identity_changed")
        self.assertEqual(caught.exception.final_path_state,
                         decisions.PUBLISHED_NOT_ADMITTED)
        # The publication went with the directory that was moved away. It is left exactly as it
        # is - never deleted to tidy up - and it carries no admission fact, so it can never
        # become operational without an explicit controlled reconciliation.
        stranded = self.displaced / decisions.DECISION_STORE_NAME
        self.assertTrue(stranded.exists(), "the published store is never deleted")
        raw = sqlite3.connect(str(stranded))
        try:
            self.assertEqual(
                raw.execute("SELECT COUNT(*) FROM store_admission").fetchone()[0], 0
            )
        finally:
            raw.close()
        with self.assertRaises(decisions.DecisionStoreError) as reuse:
            decisions.inspect_store(stranded)
        self.assertEqual(reuse.exception.reason, "store_not_admitted")

    def test_recheck_detects_every_way_a_parent_can_stop_being_itself(self):
        parent = decisions.establish_trusted_parent(self._store_path())
        try:
            parent.recheck()                      # the baseline holds
            displaced = self._replace_parent()
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                parent.recheck()
            self.assertEqual(caught.exception.reason, "store_parent_identity_changed")
            # Restore, then remove it entirely.
            os.rmdir(str(self.state))
            os.rename(str(displaced), str(self.state))
            parent.recheck()
            shutil.rmtree(str(self.state))
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                parent.recheck()
            self.assertEqual(caught.exception.reason, "store_parent_identity_changed")
            # A file at the pathname is untrusted rather than merely changed.
            self.state.write_bytes(b"not a directory\n")
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                parent.recheck()
            self.assertEqual(caught.exception.reason, "store_parent_untrusted")
        finally:
            parent.close()

    def test_a_rejected_parent_never_reaches_a_reviewer_decision_or_the_ledger(self):
        self._remove_parent()
        for argv, expected in (
            (["approve", "--reviewer", "digital"], approval.EXIT_DECISION_AUTHORITY_UNCERTAIN),
            (["hold", "--reviewer", "digital"], approval.EXIT_DECISION_AUTHORITY_UNCERTAIN),
        ):
            with self.subTest(command=argv[0]):
                code, out = self._run(argv + [
                    "--input", str(self.form), "--decision-rows", str(self.rows),
                    "--row-number", "2", "--ledger", str(self.ledger),
                ])
                self.assertEqual(code, expected, out)
                self.assertNotIn("Traceback", out)
                summary = json.loads(out)
                self.assertEqual(summary["decision_store_integrity"], "store_parent_missing")
                self.assertIs(summary["decision_activated"] if "decision_activated" in summary
                              else False, False)
                self.assertFalse(self.ledger.exists())
                self.assertFalse(os.path.lexists(self.state))

    def test_no_store_path_lists_globs_or_sweeps_the_state_directory(self):
        patches = self._no_sweep()
        for patch in patches:
            patch.start()
        try:
            decisions.create_store_exclusively(self._store_path())
            decisions.inspect_store(self._store_path())
        finally:
            for patch in patches:
                patch.stop()

    def test_a_relative_final_path_is_refused(self):
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.establish_trusted_parent(Path("relative") / "x.sqlite3")
        self.assertEqual(caught.exception.reason, "store_parent_unsupported")


@unittest.skipUnless(decisions.IS_WINDOWS, "Windows platform boundary")
class WindowsParentPlatformTests(_ParentAdmissionHarness):
    """Lock section 9, Windows: fixed local NTFS only, pathname-based, no dir_fd claim."""

    def test_the_real_volume_seams_report_a_fixed_ntfs_drive(self):
        anchor = Path(self.state.drive + "\\")
        self.assertEqual(decisions._windows_drive_type(anchor),
                         decisions.WINDOWS_DRIVE_FIXED)
        self.assertEqual(decisions._windows_filesystem_name(anchor).upper(),
                         decisions.WINDOWS_REQUIRED_FILESYSTEM)

    def test_the_admitted_parent_reports_no_directory_descriptor(self):
        parent = decisions.establish_trusted_parent(self._store_path())
        try:
            self.assertIsNone(parent.dir_fd,
                              "Windows must never claim POSIX dir_fd guarantees")
            self.assertEqual(parent.filesystem, decisions.WINDOWS_REQUIRED_FILESYSTEM)
        finally:
            parent.close()

    def test_a_unc_path_is_refused_by_shape_before_any_volume_call(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("a UNC path must be refused before any volume query")

        with mock.patch.object(decisions, "_windows_drive_type", forbidden):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.establish_trusted_parent(
                    Path(r"\\synthetic-server\synthetic-share\state\x.sqlite3")
                )
        self.assertEqual(caught.exception.reason, "store_parent_unsupported")

    def test_a_mapped_or_remote_drive_is_refused(self):
        for label, drive_type in (("remote or mapped", 4), ("removable", 2),
                                  ("cd-rom", 5), ("ramdisk", 6), ("unknown", 0)):
            with self.subTest(drive=label):
                with mock.patch.object(decisions, "_windows_drive_type",
                                       lambda root, value=drive_type: value):
                    self._assert_nothing_created_at_all(
                        lambda: decisions.create_store_exclusively(self._store_path()),
                        "store_parent_unsupported",
                    )

    def test_a_fixed_but_non_ntfs_volume_is_refused(self):
        for filesystem in ("FAT32", "exFAT", "ReFS", ""):
            with self.subTest(filesystem=filesystem or "empty"):
                with mock.patch.object(decisions, "_windows_filesystem_name",
                                       lambda root, value=filesystem: value):
                    self._assert_nothing_created_at_all(
                        lambda: decisions.create_store_exclusively(self._store_path()),
                        "store_parent_unsupported",
                    )

    def test_a_volume_query_failure_fails_closed(self):
        def failing(root):
            raise OSError(1, "synthetic volume query failure")

        for seam in ("_windows_drive_type", "_windows_filesystem_name"):
            with self.subTest(seam=seam):
                with mock.patch.object(decisions, seam, failing):
                    self._assert_nothing_created_at_all(
                        lambda: decisions.create_store_exclusively(self._store_path()),
                        "store_parent_unsupported",
                    )


@unittest.skipIf(decisions.IS_WINDOWS, "POSIX platform boundary")
class PosixParentPlatformTests(_ParentAdmissionHarness):
    """Lock section 9, POSIX: descriptor-relative walking on a supported local filesystem."""

    def test_the_admitted_parent_holds_a_directory_descriptor(self):
        parent = decisions.establish_trusted_parent(self._store_path())
        try:
            self.assertIsNotNone(parent.dir_fd)
            info = os.fstat(parent.dir_fd)
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual(decisions._identity_pair(info), parent.identity)
            self.assertIn(parent.filesystem, decisions.POSIX_SUPPORTED_FILESYSTEMS)
        finally:
            parent.close()

    def test_creation_and_publication_are_descriptor_relative(self):
        """The temporary is created, linked and unlinked RELATIVE to the verified descriptor.

        Not a source reading: ``os.open``, ``os.link`` and ``os.unlink`` are wrapped and the
        recorded calls are asserted to carry a directory descriptor and a BASENAME rather than a
        reconstructed absolute pathname.
        """
        seen = {"open": [], "link": [], "unlink": []}
        real_open, real_link, real_unlink = os.open, os.link, os.unlink

        def recording_open(path, flags, *a, **k):
            if str(path).startswith(".mcuat_decisions_") or ".mcuat_decisions_" in str(path):
                seen["open"].append((str(path), k.get("dir_fd")))
            return real_open(path, flags, *a, **k)

        def recording_link(src, dst, **k):
            seen["link"].append((str(src), str(dst), k.get("src_dir_fd"),
                                 k.get("dst_dir_fd")))
            return real_link(src, dst, **k)

        def recording_unlink(path, **k):
            seen["unlink"].append((str(path), k.get("dir_fd")))
            return real_unlink(path, **k)

        with mock.patch("os.open", recording_open), \
                mock.patch("os.link", recording_link), \
                mock.patch("os.unlink", recording_unlink):
            result = decisions.create_store_exclusively(self._store_path())
        self.assertEqual(result.durability, decisions.DURABILITY_POSIX_CREATE)

        creates = [entry for entry in seen["open"] if entry[1] is not None]
        self.assertTrue(creates, "the temporary must be created relative to the descriptor")
        for name, _fd in creates:
            self.assertNotIn(os.sep, name, "a descriptor-relative name is a basename")

        self.assertEqual(len(seen["link"]), 1)
        src, dst, src_fd, dst_fd = seen["link"][0]
        self.assertIsNotNone(src_fd)
        self.assertIsNotNone(dst_fd)
        self.assertNotIn(os.sep, src)
        self.assertNotIn(os.sep, dst)

        temp_unlinks = [entry for entry in seen["unlink"]
                        if ".mcuat_decisions_" in entry[0]]
        self.assertEqual(len(temp_unlinks), 1)
        self.assertIsNotNone(temp_unlinks[0][1],
                             "the temporary is unlinked relative to the descriptor")
        self.assertNotIn(os.sep, temp_unlinks[0][0])

    def test_sqlite_is_never_claimed_to_be_descriptor_relative(self):
        """Python's ``sqlite3`` takes a PATHNAME. The contract says so and must not pretend.

        Every recorded connection target must be an ordinary absolute pathname, and must NOT go
        through ``/proc/self/fd`` or any other descriptor-indirection trick — the lock forbids
        those explicitly, and using one would silently convert a documented residual race into an
        undocumented dependency on procfs semantics.
        """
        seen = []
        real_connect = sqlite3.connect

        def recording_connect(target, *a, **k):
            seen.append(str(target))
            return real_connect(target, *a, **k)

        with mock.patch.object(sqlite3, "connect", recording_connect):
            decisions.create_store_exclusively(self._store_path())
        self.assertTrue(seen)
        for target in seen:
            self.assertTrue(target.startswith("/") or target.startswith("file:/"), target)
            self.assertNotIn("/proc/", target,
                             "SQLite must never be opened through a descriptor indirection")
            self.assertNotIn("/dev/fd/", target, target)

    def test_component_traversal_never_follows_a_redirection(self):
        """``O_NOFOLLOW`` is asserted on the traversal open itself.

        The ordered classification already refuses a symlink component, so this flag has no
        separately observable behaviour: it narrows the classify-to-open window that the threat
        model documents rather than eliminates. Asserting the flag on the call is therefore the
        only faithful way to pin it.
        """
        seen = []
        real_open = os.open

        def recording_open(path, flags, *a, **k):
            if k.get("dir_fd") is not None and flags & os.O_DIRECTORY:
                seen.append(flags)
            return real_open(path, flags, *a, **k)

        with mock.patch("os.open", recording_open):
            parent = decisions.establish_trusted_parent(self._store_path())
            parent.close()
        self.assertTrue(seen, "component traversal must open relative to a descriptor")
        for flags in seen:
            self.assertTrue(flags & os.O_NOFOLLOW,
                            "every traversal open must refuse to follow a redirection")

    def test_a_known_remote_filesystem_is_refused(self):
        for fstype in sorted(decisions.POSIX_KNOWN_REMOTE_FILESYSTEMS):
            with self.subTest(filesystem=fstype):
                with mock.patch.object(decisions, "_posix_filesystem_type",
                                       lambda path, value=fstype: value):
                    self._assert_nothing_created_at_all(
                        lambda: decisions.create_store_exclusively(self._store_path()),
                        "store_parent_unsupported",
                    )

    def test_an_unprovable_filesystem_fails_closed(self):
        with mock.patch.object(decisions, "_posix_filesystem_type", lambda path: None):
            self._assert_nothing_created_at_all(
                lambda: decisions.create_store_exclusively(self._store_path()),
                "store_parent_unsupported",
            )

    def test_an_unrecognised_filesystem_fails_closed(self):
        with mock.patch.object(decisions, "_posix_filesystem_type",
                               lambda path: "somethingnobodyhasheardof"):
            self._assert_nothing_created_at_all(
                lambda: decisions.create_store_exclusively(self._store_path()),
                "store_parent_unsupported",
            )

    def test_the_real_filesystem_classifier_proves_a_supported_local_filesystem(self):
        fstype = decisions._posix_filesystem_type(self.state)
        self.assertIsNotNone(fstype, "the fixture directory's filesystem must be provable")
        self.assertIn(fstype.lower(), decisions.POSIX_SUPPORTED_FILESYSTEMS, fstype)

    def test_the_fixture_directory_satisfies_the_anchor_device_invariant(self):
        """A diagnostic, so an environment problem is never mistaken for a contract failure.

        The contract refuses a device or mount transition between ``/`` and the state parent. If
        this environment places the temporary root on a separate mount, every POSIX test below
        fails for an environment reason, so the required action is named here explicitly.
        """
        anchor = os.stat("/").st_dev
        fixture = os.stat(str(self.state)).st_dev
        self.assertEqual(
            fixture, anchor,
            "the synthetic fixture root is on a different mount from '/', so the POSIX "
            "trusted-parent contract correctly refuses it. Point TMPDIR at a directory on the "
            "root filesystem (the CI job sets TMPDIR for exactly this reason).",
        )

    def test_descriptor_relative_operations_are_required(self):
        """Capability is proven BY USE, so an unavailable ``dir_fd`` refuses at the call itself.

        Consulting ``os.supports_dir_fd`` would be the wrong test: it reports the interpreter's
        advertised support rather than whether the call works here, and it stops describing
        reality as soon as a caller legitimately replaces one of those functions - which several
        suites in this file do. Python signals a genuinely unsupported ``dir_fd`` with
        ``NotImplementedError``, so that is what is forced here.
        """
        real_open = os.open

        def no_dir_fd(path, flags, *a, **k):
            if k.get("dir_fd") is not None:
                raise NotImplementedError("dir_fd unavailable on this platform")
            return real_open(path, flags, *a, **k)

        with mock.patch("os.open", no_dir_fd):
            self._assert_nothing_created_at_all(
                lambda: decisions.create_store_exclusively(self._store_path()),
                "store_parent_unsupported",
            )

    def test_publication_refuses_rather_than_falling_back_to_a_pathname_link(self):
        """An unavailable descriptor-relative link must NOT silently publish by pathname."""
        def no_dir_fd_link(src, dst, **kwargs):
            if kwargs.get("src_dir_fd") is not None:
                raise NotImplementedError("dir_fd unavailable on this platform")
            raise AssertionError("publication must never fall back to a pathname link")

        with mock.patch("os.link", no_dir_fd_link):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_parent_unsupported")
        self.assertFalse(os.path.lexists(self._store_path()),
                         "the final path is never created by a fallback")


class MountinfoClassificationTests(unittest.TestCase):
    """The mountinfo parser is pure, so its rules are pinned by fixtures on every platform."""

    ROOT = ("36 35 98:0 / / rw,relatime shared:1 - ext4 /dev/sda1 rw")
    NESTED = ("41 36 0:35 / /srv/state rw,relatime shared:2 - nfs4 "
              "server:/export rw,vers=4.2")
    ESCAPED = ("42 36 0:36 / /srv/with\\040space rw,relatime shared:3 - xfs /dev/sdb1 rw")

    def test_the_longest_matching_mount_point_wins(self):
        lines = [self.ROOT, self.NESTED]
        self.assertEqual(
            decisions.mountinfo_filesystem_type(lines, "/srv/state/store.sqlite3"), "nfs4"
        )
        self.assertEqual(
            decisions.mountinfo_filesystem_type(lines, "/home/x/store.sqlite3"), "ext4"
        )

    def test_a_mount_point_prefix_must_be_a_whole_component(self):
        lines = [self.ROOT, self.NESTED]
        # `/srv/statement` must NOT match the `/srv/state` mount point.
        self.assertEqual(
            decisions.mountinfo_filesystem_type(lines, "/srv/statement/x"), "ext4"
        )

    def test_the_exact_mount_point_itself_matches(self):
        self.assertEqual(
            decisions.mountinfo_filesystem_type([self.ROOT, self.NESTED], "/srv/state"),
            "nfs4",
        )

    def test_octal_escapes_in_a_mount_point_are_decoded(self):
        self.assertEqual(
            decisions.mountinfo_filesystem_type(
                [self.ROOT, self.ESCAPED], "/srv/with space/store.sqlite3"
            ),
            "xfs",
        )

    def test_unparsable_and_unmatched_input_yields_none(self):
        for label, lines in (
            ("empty", []),
            ("no separator", ["36 35 98:0 / / rw ext4 /dev/sda1 rw"]),
            ("truncated", ["36 35 98:0 - ext4"]),
            ("no filesystem", ["36 35 98:0 / / rw shared:1 - "]),
        ):
            with self.subTest(case=label):
                self.assertIsNone(
                    decisions.mountinfo_filesystem_type(lines, "/srv/state/x")
                )

    def test_no_remote_filesystem_is_in_the_supported_allowlist(self):
        self.assertEqual(
            decisions.POSIX_SUPPORTED_FILESYSTEMS
            & decisions.POSIX_KNOWN_REMOTE_FILESYSTEMS,
            frozenset(),
        )


class _ReconciliationHarness(_AdmissionHarness):
    """Shared helpers for the controlled-reconciliation suites.

    Kept separate from the test classes so the two platform-specific durability suites do not
    re-run the whole portable suite under a different name.
    """

    ALL_TABLES = ("schema_meta", "store_admission", "decision", "decision_activation",
                  "build_claim")

    def _dump(self):
        """Every row of every table, plus the file identity and the bounded directory listing."""
        raw = self._raw()
        try:
            content = {
                table: [tuple(row) for row in raw.execute(f"SELECT * FROM {table}")]
                for table in self.ALL_TABLES
            }
        finally:
            raw.close()
        info = os.lstat(self._store_path())
        content["__identity__"] = decisions._file_identity(info)
        content["__dirents__"] = sorted(os.listdir(self.tmp))
        content["__links__"] = getattr(info, "st_nlink", 1)
        content["__page_count__"] = None
        return content

    def _reconcile(self, *, confirmed=True):
        return decisions.reconcile_store_admission(
            self._store_path(), confirmed=confirmed
        )

    def _cli(self, *extra):
        return self._run(["reconcile-store-admission", "--ledger", str(self.ledger), *extra])


class ControlledReconciliationTests(_ReconciliationHarness):
    """Lock section 7: the ONE explicit way out of a permanently blocked store.

    Never automatic, never reachable from an ordinary command, gated on a confirmation switch,
    refused outright if any history exists, and mutating ADMISSION STATE ONLY. Every fixture here
    is synthetic and disposable; nothing runs against a real or private store.
    """

    def test_the_confirmation_switch_is_required(self):
        self._non_admitted_store()
        before = self._store_snapshot()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._reconcile(confirmed=False)
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")
        self.assertEqual(self._store_snapshot(), before, "nothing is changed")
        self.assertEqual(self._admission_rows(), [])

    def test_the_cli_refuses_without_the_confirmation_switch(self):
        self._non_admitted_store()
        before = self._store_snapshot()
        code, out = self._cli()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(json.loads(out)["decision_store_integrity"],
                         "store_admission_uncertain")
        self.assertEqual(self._store_snapshot(), before)

    def test_a_valid_zero_history_non_admitted_store_is_admitted(self):
        self._non_admitted_store()
        before = self._dump()
        result = self._reconcile()
        self.assertEqual(result.admission, decisions.ADMISSION_MODE_RECONCILED)
        self.assertEqual(result.temp_cleanup, "not_applicable")
        expected = (decisions.DURABILITY_WINDOWS_RECONCILE if decisions.IS_WINDOWS
                    else decisions.DURABILITY_POSIX_RECONCILE)
        self.assertEqual(result.durability, expected)
        after = self._dump()
        # ONLY the admission row changed. Same file, same links, same directory, same history.
        self.assertEqual(before["__identity__"], after["__identity__"])
        self.assertEqual(before["__dirents__"], after["__dirents__"])
        self.assertEqual(before["__links__"], after["__links__"])
        for table in self.ALL_TABLES:
            if table == "store_admission":
                continue
            self.assertEqual(before[table], after[table], f"{table} must be untouched")
        self.assertEqual(before["store_admission"], [])
        self.assertEqual(len(after["store_admission"]), 1)
        for sidecar in self._sidecar_paths():
            self.assertFalse(os.path.lexists(sidecar))
        decisions.inspect_store(self._store_path())

    def test_the_cli_reports_what_reconciliation_achieved(self):
        self._non_admitted_store()
        code, out = self._cli("--confirm-controlled-reconciliation")
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["operation"], "reconcile_store_admission")
        self.assertEqual(summary["decision_store_admission"],
                         decisions.ADMISSION_MODE_RECONCILED)
        self.assertIn(summary["decision_store_durability"],
                      decisions.ADMISSION_DURABILITY_PRIMITIVES)
        self.assertIs(summary["decision_store_image_modified"], False)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertEqual(summary["decision_authority"], "none",
                         "reconciliation grants no reviewer authority whatsoever")

    def test_reconciliation_is_refused_when_any_history_exists(self):
        cases = {}

        # A committed but unactivated decision.
        self._reset()
        srid, fingerprint = self._fixture_identity()
        self._insert_pending(self._record("hold", srid=srid, fingerprint=fingerprint))
        self._strip_admission()
        cases["decision"] = self._store_path()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.reason, "store_reconciliation_history_present")
        self.assertEqual(self._admission_rows(), [])

        # An activated decision.
        self._reset()
        self._prepare_activated_approval()
        self._strip_admission()
        before = self._store_snapshot()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.reason, "store_reconciliation_history_present")
        self.assertEqual(self._store_snapshot(), before)

        # A committed build claim.
        self._reset()
        self.assertEqual(self._approve()[0], 0)
        self.assertEqual(self._build()[0], 0)
        self.assertEqual(self._raw_claim_count(), 1)
        self._strip_admission()
        before = self._store_snapshot()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.reason, "store_reconciliation_history_present")
        self.assertEqual(self._store_snapshot(), before)
        self.assertEqual(self._admission_rows(), [])

    def test_reconciliation_refuses_a_sidecar_bearing_store(self):
        self._non_admitted_store()
        self._write_sidecar("-journal", payload=b"crash residue\n")
        before = self._store_snapshot()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.reason, "store_sidecar_present")
        self.assertEqual(self._store_snapshot(), before,
                         "the journal is never deleted, rolled back or checkpointed")
        self.assertEqual(self._admission_rows(), [])

    def test_reconciliation_refuses_an_already_admitted_store(self):
        self._create_store_for_fixture()
        before = self._store_snapshot()
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.reason, "store_admission_invalid")
        self.assertEqual(self._store_snapshot(), before)

    def test_reconciliation_refuses_a_missing_store(self):
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.reason, "store_missing")

    def test_an_identity_change_during_preconditions_is_refused(self):
        self._non_admitted_store()
        replacement = self._second_canonical_store()
        real_structural = decisions._read_structural

        def swap_then_validate(path, *a, **k):
            value = real_structural(path, *a, **k)
            os.replace(str(replacement), str(self._store_path()))
            return value

        with mock.patch.object(decisions, "_read_structural", swap_then_validate):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertIn(caught.exception.reason,
                      ("store_identity_changed", "store_admission_invalid"))

    def test_an_identity_change_while_re_establishing_durability_is_refused(self):
        self._non_admitted_store()
        replacement = self._second_canonical_store()
        real_fsync = decisions._fsync_file

        def fsync_then_swap(path, **kwargs):
            value = real_fsync(path, **kwargs)
            os.replace(str(replacement), str(self._store_path()))
            return value

        with mock.patch.object(decisions, "_fsync_file", fsync_then_swap):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertIn(caught.exception.reason,
                      ("store_identity_changed", "store_admission_invalid"))

    def test_the_posix_durability_branch_requires_both_fsyncs(self):
        """The POSIX reconciliation durability branch, exercised on EITHER platform.

        `_reestablish_durability` selects its POSIX branch purely on whether the admitted parent
        carries a directory descriptor, and inside that branch it only calls the two seams. On
        Windows the branch is unreachable through a real parent, so a stand-in parent drives it
        directly. That is what makes "reconcile without re-establishing POSIX directory
        durability" a mutation this suite can kill anywhere, rather than only on the platform the
        branch happens to run on.
        """
        self._non_admitted_store()

        class _ParentWithDescriptor:
            """Reports a descriptor without owning one; the seams below never dereference it."""

            dir_fd = -1

        parent = _ParentWithDescriptor()
        store = self._store_path()

        def quiet_file_fsync(path, **kwargs):
            return None

        # A raised directory fsync must refuse before admission.
        with mock.patch.object(decisions, "_fsync_file", quiet_file_fsync), \
                mock.patch.object(decisions, "_fsync_directory",
                                  mock.Mock(side_effect=OSError(5, "synthetic"))):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions._reestablish_durability(store, parent)
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")

        # A directory fsync that silently did NOTHING is equally fatal: "we did not perform it"
        # is not "it is durable".
        with mock.patch.object(decisions, "_fsync_file", quiet_file_fsync), \
                mock.patch.object(decisions, "_fsync_directory", lambda p: False):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions._reestablish_durability(store, parent)
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")

        # Both succeeding records the POSIX primitive, never the Windows one.
        with mock.patch.object(decisions, "_fsync_file", quiet_file_fsync), \
                mock.patch.object(decisions, "_fsync_directory", lambda p: True):
            self.assertEqual(
                decisions._reestablish_durability(store, parent),
                decisions.DURABILITY_POSIX_RECONCILE,
            )
        # Nothing above wrote an admission row: durability is re-established first.
        self.assertEqual(self._admission_rows(), [])

    def test_a_file_durability_failure_refuses_before_admission(self):
        self._non_admitted_store()

        def failing(path, **kwargs):
            raise OSError(5, "synthetic file flush failure")

        before = self._store_snapshot()
        with mock.patch.object(decisions, "_fsync_file", failing):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")
        self.assertEqual(self._store_snapshot(), before)
        self.assertEqual(self._admission_rows(), [],
                         "admission is never written on unproven durability")

    def test_an_admission_commit_that_did_not_write_leaves_the_store_blocked(self):
        self._non_admitted_store()

        def failing_commit(conn):
            raise sqlite3.OperationalError("synthetic admission commit failure")

        with mock.patch.object(decisions, "_commit", failing_commit):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")
        self.assertEqual(self._admission_rows(), [])
        # Still blocked: ordinary operations refuse exactly as before.
        self._assert_readable_and_canonical()
        with self.assertRaises(decisions.DecisionStoreError) as reuse:
            decisions.inspect_store(self._store_path())
        self.assertEqual(reuse.exception.reason, "store_not_admitted")

    def test_an_admission_commit_that_really_wrote_then_raised_is_resolved(self):
        self._non_admitted_store()
        real_commit = decisions._commit

        def commit_then_raise(conn):
            real_commit(conn)
            raise sqlite3.OperationalError("synthetic failure after a real commit")

        with mock.patch.object(decisions, "_commit", commit_then_raise):
            result = self._reconcile()
        self.assertEqual(result.admission, decisions.ADMISSION_MODE_RECONCILED)
        self.assertEqual(len(self._admission_rows()), 1)
        decisions.inspect_store(self._store_path())

    def test_an_unresolved_admission_commit_keeps_the_store_blocked(self):
        self._non_admitted_store()
        real_commit = decisions._commit

        def commit_then_raise(conn):
            real_commit(conn)
            raise sqlite3.OperationalError("synthetic failure after a real commit")

        with mock.patch.object(decisions, "_commit", commit_then_raise), \
                mock.patch.object(decisions, "recover_admission_commit",
                                  lambda *a, **k: decisions.CommitState.UNCERTAIN):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")

    def test_reconciliation_refuses_a_missing_or_untrusted_parent(self):
        self._non_admitted_store()
        moved = self.tmp / "moved_state"
        moved.mkdir()
        os.replace(str(self._store_path()), str(moved / decisions.DECISION_STORE_NAME))
        missing = self.tmp / "gone" / decisions.DECISION_STORE_NAME
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions.reconcile_store_admission(missing, confirmed=True)
        self.assertEqual(caught.exception.reason, "store_parent_missing")
        self.assertFalse(os.path.lexists(self.tmp / "gone"))

    def test_a_fresh_process_operates_normally_after_successful_reconciliation(self):
        self._non_admitted_store()
        self._reconcile()
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(
                f"""
                import io, json, sys
                from contextlib import redirect_stdout
                sys.path.insert(0, {str(SCRIPTS)!r})
                import member_create_uat_approval as approval
                common = ["--input", {str(self.form)!r},
                          "--decision-rows", {str(self.rows)!r},
                          "--row-number", "2", "--ledger", {str(self.ledger)!r}]
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = approval.main(["approve", "--reviewer", "digital"] + common)
                first = json.loads(buf.getvalue())
                buf = io.StringIO()
                with redirect_stdout(buf):
                    build = approval.main(["build-package"] + common
                                          + ["--package-out", {str(self.package)!r}])
                print(json.dumps({{"decision": code, "build": build,
                                   "created": first["decision_store_created"]}}))
                """
            )],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(report["decision"], 0, proc.stdout)
        self.assertEqual(report["build"], 0, proc.stdout)
        self.assertIs(report["created"], False,
                      "the reconciled store is reused, never recreated")
        self.assertTrue(self.package.exists())

    def test_a_fresh_process_stays_blocked_after_uncertain_reconciliation(self):
        self._non_admitted_store()

        def failing(path, **kwargs):
            raise OSError(5, "synthetic file flush failure")

        with mock.patch.object(decisions, "_fsync_file", failing):
            with self.assertRaises(decisions.DecisionStoreError):
                self._reconcile()
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(
                f"""
                import io, json, sys
                from contextlib import redirect_stdout
                sys.path.insert(0, {str(SCRIPTS)!r})
                import member_create_uat_approval as approval
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = approval.main([
                        "approve", "--reviewer", "digital",
                        "--input", {str(self.form)!r},
                        "--decision-rows", {str(self.rows)!r},
                        "--row-number", "2", "--ledger", {str(self.ledger)!r},
                    ])
                print(json.dumps({{"code": code, "summary": json.loads(buf.getvalue())}}))
                """
            )],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(report["code"], approval.EXIT_DECISION_AUTHORITY_UNCERTAIN)
        self.assertEqual(report["summary"]["decision_store_integrity"],
                         "store_not_admitted")

    def test_reconciliation_is_never_invoked_by_an_ordinary_command(self):
        def tripwire(*args, **kwargs):
            raise AssertionError("no ordinary command may reconcile a store")

        with mock.patch.object(decisions, "reconcile_store_admission", tripwire):
            self._non_admitted_store()
            self.assertEqual(self._approve()[0],
                             approval.EXIT_DECISION_AUTHORITY_UNCERTAIN)
            self.assertEqual(self._build()[0],
                             approval.EXIT_DECISION_AUTHORITY_UNCERTAIN)

    def test_reconciliation_never_repairs_migrates_or_replaces_the_image(self):
        """It must not checkpoint, truncate, rewrite, rename or replace the database."""
        self._non_admitted_store()
        forbidden_pragmas = ("journal_mode", "wal_checkpoint", "vacuum")
        seen = []
        real_connect = decisions._connect_uri

        def recording(path, query):
            return _RecordingConnection(real_connect(path, query), seen)

        real_replace, real_rename = os.replace, os.rename

        def forbid_replace(*args, **kwargs):
            raise AssertionError("reconciliation must never replace the database image")

        def forbid_rename(*args, **kwargs):
            raise AssertionError("reconciliation must never rename the database image")

        with mock.patch.object(decisions, "_connect_uri", recording), \
                mock.patch("os.replace", forbid_replace), \
                mock.patch("os.rename", forbid_rename):
            self._reconcile()
        del real_replace, real_rename
        assignments = [sql for sql in seen if "=" in sql and "PRAGMA" in sql.upper()]
        for sql in assignments:
            self.assertNotIn("journal_mode", sql.lower(),
                             "journal mode is verified, never assigned")
        for sql in seen:
            lowered = sql.lower()
            for forbidden in forbidden_pragmas:
                if forbidden == "journal_mode":
                    continue
                self.assertNotIn(forbidden, lowered, sql)
            self.assertNotIn("drop ", lowered, sql)
            self.assertNotIn("alter ", lowered, sql)


@unittest.skipIf(decisions.IS_WINDOWS, "POSIX reconciliation durability")
class PosixReconciliationDurabilityTests(_ReconciliationHarness):
    """Lock section 7, POSIX: fsync the final file AND the verified parent descriptor."""

    def test_a_parent_directory_fsync_failure_refuses_before_admission(self):
        self._non_admitted_store()

        def failing(parent):
            raise OSError(5, "synthetic parent directory fsync failure")

        before = self._store_snapshot()
        with mock.patch.object(decisions, "_fsync_directory", failing):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")
        self.assertEqual(self._store_snapshot(), before)
        self.assertEqual(self._admission_rows(), [])

    def test_a_parent_directory_fsync_that_was_not_performed_refuses(self):
        # A silent "did nothing" must be as fatal as a raised error.
        self._non_admitted_store()
        with mock.patch.object(decisions, "_fsync_directory", lambda parent: False):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertEqual(caught.exception.reason, "store_admission_uncertain")
        self.assertEqual(self._admission_rows(), [])

    def test_the_recorded_primitive_names_both_fsyncs(self):
        self._non_admitted_store()
        result = self._reconcile()
        self.assertEqual(result.durability, decisions.DURABILITY_POSIX_RECONCILE)
        self.assertEqual(self._admission_row()["durability"],
                         decisions.DURABILITY_POSIX_RECONCILE)


@unittest.skipUnless(decisions.IS_WINDOWS, "Windows reconciliation durability")
class WindowsReconciliationDurabilityTests(_ReconciliationHarness):
    """Lock section 7, Windows: a file flush only, honestly named as the weaker primitive."""

    def test_the_recorded_primitive_states_there_is_no_directory_fsync(self):
        self._non_admitted_store()
        result = self._reconcile()
        self.assertEqual(result.durability, decisions.DURABILITY_WINDOWS_RECONCILE)
        self.assertIn("no_directory_fsync", result.durability,
                      "the weaker Windows primitive must be named for what it is")
        self.assertEqual(self._admission_row()["durability"],
                         decisions.DURABILITY_WINDOWS_RECONCILE)
        # And it must never claim the POSIX primitive.
        self.assertNotEqual(result.durability, decisions.DURABILITY_POSIX_RECONCILE)

    def test_no_directory_fsync_is_attempted_on_windows(self):
        self._non_admitted_store()

        def forbidden(parent):
            raise AssertionError(
                "Windows must not claim a directory fsync it cannot perform"
            )

        with mock.patch.object(decisions, "_fsync_directory", forbidden):
            self._reconcile()

    def test_a_non_ntfs_volume_is_refused_before_any_admission(self):
        self._non_admitted_store()
        before = self._store_snapshot()
        with mock.patch.object(decisions, "_windows_filesystem_name",
                               lambda root: "FAT32"):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertEqual(caught.exception.reason, "store_parent_unsupported")
        self.assertEqual(self._store_snapshot(), before)
        self.assertEqual(self._admission_rows(), [])

    def test_a_non_fixed_volume_is_refused_before_any_admission(self):
        self._non_admitted_store()
        with mock.patch.object(decisions, "_windows_drive_type", lambda root: 4):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                self._reconcile()
        self.assertEqual(caught.exception.reason, "store_parent_unsupported")
        self.assertEqual(self._admission_rows(), [])


class LostRaceCleanupTests(_AdmissionHarness):
    """Lock section 8: every lost-race cleanup outcome is EXPLICIT and truthful.

    Amendment 8's ``required=False`` swallowed a real unlink failure on exactly this path, so a
    surviving temporary was invisible to the operator at the moment a competitor had become the
    authority. The competing final store is preserved byte-for-byte in every case below, and no
    reviewer decision is ever allowed to follow a cleanup failure.
    """

    PUBLISH_SEAM = "_windows_no_replace_move" if decisions.IS_WINDOWS else "os.link"

    def setUp(self):
        super().setUp()
        # A sentinel unrelated entry, so "no unrelated entry changed" is a real assertion.
        self.sentinel = self.tmp / "unrelated_sentinel.bin"
        self.sentinel.write_bytes(b"unrelated\n")
        self.winner = self._second_canonical_store(name="winner")
        self.winner_bytes = self.winner.read_bytes()

    def _patch_publish(self, hook):
        if decisions.IS_WINDOWS:
            return mock.patch.object(decisions, "_windows_no_replace_move", hook)
        return mock.patch("os.link", hook)

    def _competitor_wins(self, *, then=None):
        """A hook that lets a competitor publish first, optionally doing ``then`` afterwards."""
        real = (decisions._windows_no_replace_move if decisions.IS_WINDOWS else os.link)

        if decisions.IS_WINDOWS:
            def hook(source, destination):
                if not os.path.lexists(destination):
                    shutil.copyfile(str(self.winner), str(destination))
                if then is not None:
                    then(str(source))
                return real(source, destination)
        else:
            def hook(src, dst, **kwargs):
                final = str(self._store_path())
                if not os.path.lexists(final):
                    shutil.copyfile(str(self.winner), final)
                if then is not None:
                    then(str(self.tmp / src) if not os.path.isabs(src) else str(src))
                return real(src, dst, **kwargs)

        return hook

    def _temporaries(self):
        return sorted(f for f in os.listdir(self.tmp) if f.startswith(".mcuat_decisions_"))

    def _assert_competitor_preserved(self):
        self.assertEqual(self._store_path().read_bytes(), self.winner_bytes,
                         "the competing published store is preserved byte-for-byte")
        self.assertEqual(self.sentinel.read_bytes(), b"unrelated\n",
                         "no unrelated entry is touched")

    def test_a_lost_race_with_clean_cleanup_reports_store_not_absent(self):
        with self._patch_publish(self._competitor_wins()):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_not_absent")
        self.assertEqual(caught.exception.final_path_state,
                         decisions.COMPETITOR_PUBLISHED_UNTOUCHED)
        self.assertIsNone(caught.exception.temp_basename)
        self.assertEqual(self._temporaries(), [], "only our own temporary is removed")
        self._assert_competitor_preserved()

    def test_a_lost_race_with_a_failed_cleanup_is_reported_not_suppressed(self):
        real_unlink = os.unlink

        def refusing_unlink(path, *args, **kwargs):
            if os.path.basename(str(path)).startswith(".mcuat_decisions_"):
                raise OSError(13, "synthetic unlink failure")
            return real_unlink(path, *args, **kwargs)

        with self._patch_publish(self._competitor_wins()), \
                mock.patch("os.unlink", refusing_unlink):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_temp_cleanup_incomplete")
        self.assertEqual(caught.exception.final_path_state,
                         decisions.COMPETITOR_PUBLISHED_UNTOUCHED)
        # Only a BASENAME escapes - never a directory or an absolute path.
        basename = caught.exception.temp_basename
        self.assertIsNotNone(basename)
        self.assertTrue(basename.startswith(".mcuat_decisions_"))
        self.assertNotIn(os.sep, basename)
        self.assertNotIn("/", basename)
        self.assertNotIn(str(self.tmp), basename)
        self.assertEqual(self._temporaries(), [basename],
                         "the temporary really did survive, and is named exactly")
        self._assert_competitor_preserved()

    REPLACEMENT_BYTES = b"a different object entirely\n"

    def _stage_replacement(self, temp_path):
        """Rebind the temporary's NAME to a genuinely different inode, deterministically.

        Unlinking and recreating in place is not reliable: ext4 readily hands the just-freed inode
        straight back, so the pathname can end up holding the SAME identity the operation captured
        and the production check would be right to say nothing changed. Instead the replacement is
        created as a separate file while the original still exists, its identity is proven
        different, and ``os.replace`` rebinds the name onto that pre-existing inode.

        Returns the original, replacement and post-replacement identities so the test can prove the
        substitution really happened rather than assuming it.
        """
        original = decisions._file_identity(os.lstat(temp_path))
        staging = self.tmp / "replacement_staging.bin"
        staging.write_bytes(self.REPLACEMENT_BYTES)
        replacement = decisions._file_identity(os.lstat(staging))
        self.assertNotEqual(replacement, original,
                            "the replacement must be a genuinely different object")
        os.replace(str(staging), temp_path)
        return {
            "original": original,
            "replacement": replacement,
            "after": decisions._file_identity(os.lstat(temp_path)),
        }

    def test_a_lost_race_with_a_replaced_temporary_never_unlinks_it(self):
        observed = {}

        def replace_temp(temp_path):
            observed.update(self._stage_replacement(temp_path))

        with self._patch_publish(self._competitor_wins(then=replace_temp)):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())

        # The substitution is proven, not assumed: the pathname now holds the replacement inode.
        self.assertEqual(observed["after"], observed["replacement"])
        self.assertNotEqual(observed["after"], observed["original"])
        # The production identity comparison is exercised unweakened and reports the mismatch.
        self.assertEqual(caught.exception.reason, "store_temp_identity_changed")
        self.assertEqual(caught.exception.final_path_state,
                         decisions.COMPETITOR_PUBLISHED_UNTOUCHED)
        self.assertIsNotNone(caught.exception.temp_basename)
        surviving = self._temporaries()
        self.assertEqual(len(surviving), 1)
        self.assertEqual((self.tmp / surviving[0]).read_bytes(), self.REPLACEMENT_BYTES,
                         "a replacement object is never unlinked")
        self.assertEqual(decisions._file_identity(os.lstat(self.tmp / surviving[0])),
                         observed["replacement"],
                         "the surviving object is the replacement, untouched")
        self.assertFalse((self.tmp / "replacement_staging.bin").exists(),
                         "os.replace consumed the staging name")
        self.assertFalse(self.ledger.exists(),
                         "no reviewer decision or audit append follows")
        self._assert_competitor_preserved()

    def test_no_reviewer_decision_follows_a_replaced_temporary(self):
        """Requirement 5 of the replaced-temporary contract, through the real CLI."""
        def replace_temp(temp_path):
            self._stage_replacement(temp_path)

        with self._patch_publish(self._competitor_wins(then=replace_temp)):
            code, out = self._approve()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"], "store_temp_identity_changed")
        self.assertEqual(summary["decision_store_final_path_state"],
                         decisions.COMPETITOR_PUBLISHED_UNTOUCHED)
        # The competitor's store is intact, so THIS operation modified no store at all.
        self.assertIs(summary["decision_store_modified"], False)
        self.assertIs(summary["manual_temp_cleanup_required"], True)
        self.assertEqual(summary["event"], "none")
        self.assertEqual(self._entries(), [], "no audit line is appended")
        self._assert_competitor_preserved()

    def test_a_lost_race_with_an_already_absent_temporary_reports_not_absent(self):
        # Forced at the lost-race handler itself. Removing the temporary before publication is
        # attempted would make the publication fail for a DIFFERENT reason (a vanished source),
        # so the "nothing left to clean up" branch is exercised exactly where it lives.
        temp = self.tmp / ".mcuat_decisions_already_gone.tmp"
        temp.write_bytes(b"payload\n")
        identity = decisions._file_identity(os.lstat(temp))
        os.unlink(temp)
        with self.assertRaises(decisions.DecisionStoreError) as caught:
            decisions._raise_lost_race(str(temp), identity=identity)
        self.assertEqual(caught.exception.reason, "store_not_absent")
        self.assertIsNone(caught.exception.temp_basename,
                          "no manual cleanup is requested when nothing survived")
        self.assertEqual(caught.exception.final_path_state,
                         decisions.COMPETITOR_PUBLISHED_UNTOUCHED)

    def test_a_vanished_temporary_before_publication_is_a_creation_failure(self):
        # The distinct, honestly-classified neighbour of the case above.
        def remove_temp(temp_path):
            os.unlink(temp_path)

        with self._patch_publish(self._competitor_wins(then=remove_temp)):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_create_failed")
        self.assertEqual(self._temporaries(), [])
        self._assert_competitor_preserved()

    def test_a_lost_race_never_lists_globs_or_sweeps(self):
        patches = self._no_sweep()
        for patch in patches:
            patch.start()
        try:
            with self._patch_publish(self._competitor_wins()):
                with self.assertRaises(decisions.DecisionStoreError) as caught:
                    decisions.create_store_exclusively(self._store_path())
        finally:
            for patch in patches:
                patch.stop()
        self.assertEqual(caught.exception.reason, "store_not_absent")

    def test_no_reviewer_decision_follows_a_cleanup_failure(self):
        real_unlink = os.unlink

        def refusing_unlink(path, *args, **kwargs):
            if os.path.basename(str(path)).startswith(".mcuat_decisions_"):
                raise OSError(13, "synthetic unlink failure")
            return real_unlink(path, *args, **kwargs)

        with self._patch_publish(self._competitor_wins()), \
                mock.patch("os.unlink", refusing_unlink):
            code, out = self._approve()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_store_integrity_uncertain")
        self.assertEqual(summary["decision_store_integrity"],
                         "store_temp_cleanup_incomplete")
        self.assertIs(summary["manual_temp_cleanup_required"], True)
        self.assertEqual(summary["decision_store_final_path_state"],
                         decisions.COMPETITOR_PUBLISHED_UNTOUCHED)
        # The competitor's store is intact, so THIS operation modified no store at all.
        self.assertIs(summary["decision_store_modified"], False)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        # No reviewer decision, no activation and no audit line exist.
        self.assertEqual(self._entries(), [])
        self.assertNotIn("decision_id", json.loads(out).get("event", ""))
        self.assertEqual(summary["event"], "none")
        self._assert_competitor_preserved()
        # The loser's stale temporary is operator HYGIENE, not a global authority block: it was
        # never a second name for the competing store, so once it is removed the competing store
        # passes pre-open triage - link count, sidecars and identity all intact.
        for name in self._temporaries():
            os.unlink(self.tmp / name)
        triage = decisions.triage_existing_store(self._store_path())
        self.assertEqual(triage.path, self._store_path())

    def test_a_publication_cleanup_failure_blocks_the_store_under_two_names(self):
        """The OTHER cleanup case: our OWN publication left the store doubly named.

        Distinct from a lost race - here the surviving temporary is a second name for OUR store,
        which SQLite documents as undefined behaviour, so the store itself must be refused until
        an operator removes exactly that one file.
        """
        if decisions.IS_WINDOWS:
            self.skipTest("POSIX link-then-unlink publication contract")
        real_unlink = os.unlink

        def refusing_unlink(path, *args, **kwargs):
            if os.path.basename(str(path)).startswith(".mcuat_decisions_"):
                raise OSError(13, "synthetic unlink failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch("os.unlink", refusing_unlink):
            with self.assertRaises(decisions.DecisionStoreError) as caught:
                decisions.create_store_exclusively(self._store_path())
        self.assertEqual(caught.exception.reason, "store_temp_cleanup_incomplete")
        self.assertEqual(caught.exception.final_path_state,
                         decisions.PUBLISHED_NOT_ADMITTED)
        self.assertTrue(os.path.lexists(self._store_path()),
                        "the published store is never deleted to tidy up")
        with self.assertRaises(decisions.DecisionStoreError) as reuse:
            decisions.inspect_store(self._store_path())
        self.assertEqual(reuse.exception.reason, "store_multiple_links")

    def test_the_quiet_cleanup_contract_no_longer_exists(self):
        """The Amendment 8 suppression path is GONE, not merely unused."""
        self.assertFalse(hasattr(decisions, "_unlink_own_temporary"))
        import inspect

        signature = inspect.signature(decisions.cleanup_own_temporary)
        self.assertNotIn("required", signature.parameters)
        self.assertIn("identity", signature.parameters,
                      "cleanup is identity-bound, not name-bound")
        # Every state the helper can report is a named, explicit outcome.
        states = {
            value for name, value in vars(decisions.TempCleanup).items()
            if not name.startswith("_")
        }
        self.assertEqual(
            states,
            {"already_absent", "unlinked", "failed", "identity_changed", "not_regular"},
        )


class VolatileFilesystemRefusalTests(unittest.TestCase):
    """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWR.

    `tmpfs` and `ramfs` were in the supported POSIX allowlist for state that claims RESTART
    durability. `fsync` there can return successfully while the whole filesystem disappears on
    reboot, so a restart could erase activated decisions, build claims and reservations while a
    package published to a persistent filesystem survived - defeating the documented restart-safe
    single-use boundary.

    Portable: the classifier seam is a pure function, so these controls do not depend on what the
    host happens to have mounted, and they run identically on Windows and POSIX.
    """

    VOLATILE = ("tmpfs", "ramfs")

    def test_the_volatile_class_is_named_and_is_exactly_the_two_memory_filesystems(self):
        self.assertEqual(decisions.POSIX_VOLATILE_FILESYSTEMS, frozenset(self.VOLATILE))

    def test_no_volatile_filesystem_remains_in_the_supported_allowlist(self):
        self.assertEqual(
            decisions.POSIX_SUPPORTED_FILESYSTEMS & decisions.POSIX_VOLATILE_FILESYSTEMS,
            frozenset(),
            "a memory-backed filesystem can never support the durability claim",
        )
        for name in self.VOLATILE:
            with self.subTest(filesystem=name):
                self.assertNotIn(name, decisions.POSIX_SUPPORTED_FILESYSTEMS)

    def test_each_volatile_filesystem_refuses_with_the_existing_classification(self):
        """The refusal reuses `store_parent_unsupported`; no new classification was invented."""
        for name in self.VOLATILE:
            with self.subTest(filesystem=name):
                with mock.patch.object(decisions, "_posix_filesystem_type",
                                       lambda path, _n=name: _n):
                    with self.assertRaises(decisions.DecisionStoreError) as caught:
                        decisions._require_supported_posix_filesystem("/synthetic/state")
                self.assertEqual(caught.exception.reason, "store_parent_unsupported")

    def test_case_and_whitespace_variants_of_the_volatile_names_also_refuse(self):
        for name in ("TMPFS", "Tmpfs", "RamFS"):
            with self.subTest(filesystem=name):
                with mock.patch.object(decisions, "_posix_filesystem_type",
                                       lambda path, _n=name: _n):
                    with self.assertRaises(decisions.DecisionStoreError) as caught:
                        decisions._require_supported_posix_filesystem("/synthetic/state")
                self.assertEqual(caught.exception.reason, "store_parent_unsupported")

    def test_every_currently_supported_persistent_filesystem_is_still_admitted(self):
        """The narrow-change control: nothing else was removed from the allowlist."""
        expected = {
            "ext2", "ext3", "ext4", "ext4dev", "xfs", "btrfs", "zfs", "f2fs", "jfs",
            "reiserfs", "bcachefs", "ubifs", "overlay", "overlayfs",
        }
        self.assertEqual(set(decisions.POSIX_SUPPORTED_FILESYSTEMS), expected)
        for name in sorted(expected):
            with self.subTest(filesystem=name):
                with mock.patch.object(decisions, "_posix_filesystem_type",
                                       lambda path, _n=name: _n):
                    self.assertEqual(
                        decisions._require_supported_posix_filesystem("/synthetic/state"), name
                    )

    def test_the_volatile_class_is_disjoint_from_the_named_remote_class(self):
        self.assertEqual(
            decisions.POSIX_VOLATILE_FILESYSTEMS & decisions.POSIX_KNOWN_REMOTE_FILESYSTEMS,
            frozenset(),
        )


@unittest.skipIf(decisions.IS_WINDOWS, "POSIX platform boundary")
class PosixVolatileFilesystemParentTests(_ParentAdmissionHarness):
    """The same refusal at the real admission call site: nothing at all is created."""

    def test_a_volatile_state_parent_creates_nothing_at_all(self):
        for name in ("tmpfs", "ramfs"):
            with self.subTest(filesystem=name):
                with mock.patch.object(decisions, "_posix_filesystem_type",
                                       lambda path, _n=name: _n):
                    self._assert_nothing_created_at_all(
                        lambda: decisions.create_store_exclusively(self._store_path()),
                        "store_parent_unsupported",
                    )


class PrivatePackageTemporaryIgnoreTests(unittest.TestCase):
    """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWV.

    `.mcuat_pkg_*.tmp` holds the COMPLETE package - member number, name, email and DOB - and a
    crash or a failed cleanup deliberately leaves it in place. Outside the wholly-ignored
    `autocount_outputs/` tree it was unignored, so an approved `--package-out` inside the
    checkout exposed that private artifact to accidental staging.
    """

    PATTERNS = (".mcuat_pkg_*.tmp", "autocount_outputs/**/.mcuat_pkg_*.tmp")
    GITIGNORE = ROOT / ".gitignore"

    @classmethod
    def setUpClass(cls):
        cls.text = cls.GITIGNORE.read_text(encoding="utf-8")

    def _git(self, *args, stdin=None):
        git = shutil.which("git")
        if not git:
            raise unittest.SkipTest("git is required to evaluate real ignore rules")
        return subprocess.run([git, "-C", str(ROOT), *args], input=stdin,
                              text=True, capture_output=True)

    def test_the_narrow_patterns_are_declared(self):
        for pattern in self.PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, self.text)

    def test_no_broad_temporary_pattern_was_introduced(self):
        lines = [line.strip() for line in self.text.splitlines()]
        for broad in ("*.tmp", "**/*.tmp", "*.json", "*"):
            self.assertNotIn(broad, lines,
                             "the private-temporary rule must stay narrow")

    def test_the_package_temporary_is_ignored_at_the_repository_root(self):
        done = self._git("check-ignore", "-v", "--no-index", ".mcuat_pkg_abcd1234.tmp")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(".mcuat_pkg_*.tmp", done.stdout)

    def test_the_package_temporary_is_ignored_in_nested_output_locations(self):
        """An approved `--package-out` may legitimately sit in a subdirectory of the checkout."""
        for rel in (
            "scripts/.mcuat_pkg_abcd1234.tmp",
            "docs/autocount2-automation/.mcuat_pkg_abcd1234.tmp",
            "tests/nested/deeper/.mcuat_pkg_abcd1234.tmp",
            "autocount_outputs/uat/.mcuat_pkg_abcd1234.tmp",
        ):
            with self.subTest(path=rel):
                done = self._git("check-ignore", "-v", "--no-index", rel)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_an_unrelated_temporary_name_is_not_ignored_by_these_rules(self):
        """Proof the rule is narrow: neighbouring temporary names stay visible."""
        for rel in (
            "unrelated.tmp",
            "scripts/build_output.tmp",
            "mcuat_pkg_no_leading_dot.tmp",
            ".mcuat_pkg_abcd1234.json",
            ".mcuat_pkg_abcd1234.tmp.bak",
        ):
            with self.subTest(path=rel):
                done = self._git("check-ignore", "-v", "--no-index", rel)
                matched = [line for line in done.stdout.splitlines()
                           if any(pattern in line for pattern in self.PATTERNS)]
                self.assertEqual(matched, [], "the new rules must not match %s" % rel)

    def test_no_tracked_repository_file_is_hidden_by_these_rules(self):
        """The decisive control: the new patterns can never mask a committed file."""
        listed = self._git("ls-files")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        tracked = [line for line in listed.stdout.splitlines() if line.strip()]
        self.assertGreater(len(tracked), 50, "the tracked-file listing must be real")
        checked = self._git("check-ignore", "-v", "--stdin", stdin="\n".join(tracked) + "\n")
        hidden = [line for line in checked.stdout.splitlines()
                  if any(pattern in line for pattern in self.PATTERNS)]
        self.assertEqual(hidden, [], "no tracked file may be ignored by the new patterns")


class NewLedgerDirectoryDurabilityTests(_CreateUatBuildHarness):
    """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWd.

    On the FIRST decision in a fresh state directory, `append_ledger` created and fsynced the
    ledger file but never committed its new parent-directory ENTRY, then reported the append as
    confirmed and the writer committed the authoritative activation. A power loss in that window
    could remove the new ledger entry while leaving the SQLite activation durable, so a restarted
    build could use an approved decision whose mandatory audit event no longer existed.
    """

    def _entry(self):
        return {
            "event": "decision", "recorded_at": "2026-08-14T00:00:00+00:00",
            "reviewer_id": "digital", "decision": "approved",
            "source_record_id": "srcrec_" + "0" * 64,
            "source_fingerprint": "fp_" + "0" * 64, "row_number_hint": 2,
            "approval_id": "appr_" + "0" * 32,
            "approved_at": "2026-08-14T00:00:00+00:00",
            "expires_at": "2026-08-17T00:00:00+00:00",
        }

    def test_a_first_append_commits_the_new_directory_entry(self):
        ledger = self.tmp / "fresh" / "member_create_uat_ledger.jsonl"
        seen = []
        real = approval._fsync_parent_directory

        def recording(directory):
            seen.append(str(directory))
            return real(directory)

        with mock.patch.object(approval, "_fsync_parent_directory", recording):
            approval.append_ledger(ledger, self._entry())
        self.assertTrue(ledger.is_file())
        self.assertIn(str(ledger.parent), seen,
                      "the new ledger's own parent entry must be committed")
        self.assertIn(str(ledger.parent.parent), seen,
                      "a directory this call created is only as durable as its own entry")

    def test_a_later_append_does_not_repeat_the_directory_commit(self):
        """The entry already exists, so only the bytes need committing."""
        ledger = self.tmp / "member_create_uat_ledger.jsonl"
        approval.append_ledger(ledger, self._entry())
        seen = []
        real = approval._fsync_parent_directory
        with mock.patch.object(approval, "_fsync_parent_directory",
                               lambda d: seen.append(str(d)) or real(d)):
            approval.append_ledger(ledger, self._entry())
        self.assertEqual(seen, [])
        self.assertEqual(len(self._entries()), 2, "the append stays append-only")

    def test_a_directory_durability_failure_propagates_rather_than_claiming_success(self):
        ledger = self.tmp / "fresh" / "member_create_uat_ledger.jsonl"
        with mock.patch.object(approval, "_fsync_parent_directory",
                               mock.Mock(side_effect=OSError(5, "synthetic dir fsync failure"))):
            with self.assertRaises(OSError):
                approval.append_ledger(ledger, self._entry())

    def test_no_activation_is_committed_when_the_new_ledger_entry_is_not_durable(self):
        """The required fault injection: file write/flush/fsync succeed, directory commit fails.

        The reviewer decision must stay pending and non-authoritative, with an explicit sanitised
        non-success, no traceback and no ledger content leaked.
        """
        with mock.patch.object(approval, "_fsync_parent_directory",
                               mock.Mock(side_effect=OSError(5, "synthetic dir fsync failure"))):
            code, out = self._approve()
        self.assertEqual(code, approval.EXIT_DECISION_AUTHORITY_UNCERTAIN, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "decision_audit_incomplete")
        self.assertEqual(summary["decision_authority"], "pending")
        self.assertIs(summary["decision_activated"], False)
        self.assertEqual(summary["audit_append"], "unconfirmed")
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertIs(summary["controlled_recovery_required"], True)
        # No private value and no ledger content reaches the operator-visible summary.
        for forbidden in ("synthetic dir fsync failure", "90000001", "synthetic.alpha"):
            self.assertNotIn(forbidden, out)
        # And the decisive consequence: nothing downstream can treat the decision as authority.
        code, build_out = self._build()
        self.assertEqual(code, approval.EXIT_DECISION_NOT_AUTHORITATIVE, build_out)
        self.assertFalse(self.package.exists(), "no package may be built from a pending decision")

    def test_the_ledger_is_never_truncated_or_repaired_by_the_failure(self):
        with mock.patch.object(approval, "_fsync_parent_directory",
                               mock.Mock(side_effect=OSError(5, "synthetic dir fsync failure"))):
            self._approve()
        # The line the failed append had already written is left EXACTLY as found: no repair,
        # no rewrite, no truncation, and no automatic retry loop.
        self.assertTrue(self.ledger.is_file())
        text = self.ledger.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(len(self._events_of("decision")), 1)


class PublishedPackageDirectoryDurabilityTests(_CreateUatBuildHarness):
    """Closed-PR #113 finding PRRT_kwDOSbJI_s6UdzWm.

    The publication fsynced the complete temporary and then used no-replace `os.link`, but never
    committed the OUTPUT-DIRECTORY entry for the final name or for the later temporary removal. A
    crash immediately after an exit-0 build could therefore lose the supposedly published package
    even though its reservation and claim had permanently consumed the approval.
    """

    def setUp(self):
        super().setUp()
        # A competing/historical artifact that must be preserved byte-for-byte throughout.
        self.competitor = self.tmp / "member_create_uat_package_v1.json"
        self.competitor.write_text('{"historical": true}\n', encoding="utf-8")
        self.competitor_bytes = self.competitor.read_bytes()

    def _temps(self):
        return sorted(f for f in os.listdir(self.tmp) if "mcuat_pkg" in f)

    def _fail_when(self, predicate):
        """Raise from the PACKAGE durability barrier only at the call the predicate identifies.

        Order-independent on purpose: the arm is selected by observable filesystem state rather
        than a call index. DL-XB-127-001-A1 injects at `_commit_package_publication`, the single
        package-side entry point on both platforms - POSIX fsyncs the output directory handle
        through it, Windows flushes the package path itself - so this fault injection stays
        platform-correct instead of only reaching the POSIX helper.
        """
        real = approval._commit_package_publication

        def hook(out_path):
            if predicate():
                raise OSError(5, "synthetic package publication durability failure")
            return real(out_path)

        return mock.patch.object(approval, "_commit_package_publication", hook)

    def _after_link(self):
        return self.package.exists() and bool(self._temps())

    def _after_cleanup(self):
        return self.package.exists() and not self._temps()

    def test_a_clean_build_reports_the_achieved_publication_durability(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        expected = (approval.PACKAGE_DURABILITY_POSIX if approval._supports_directory_fsync()
                    else approval.PACKAGE_DURABILITY_WINDOWS)
        self.assertEqual(json.loads(out)["publication_durability"], expected)

    def test_a_clean_build_commits_the_output_directory_after_link_and_after_cleanup(self):
        self._approve()
        reservation_commits = []
        package_commits = []
        real_helper = approval._fsync_parent_directory
        real_package = approval._commit_package_publication
        with mock.patch.object(approval, "_fsync_parent_directory",
                               lambda d: reservation_commits.append(str(d)) or real_helper(d)),                 mock.patch.object(approval, "_commit_package_publication",
                                  lambda p: package_commits.append(str(p)) or real_package(p)):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(
            reservation_commits.count(str(self.package.parent)), 1,
            "the reservation's own directory entry must still be committed",
        )
        self.assertEqual(
            package_commits, [str(self.package)] * 2,
            "the final link and the temporary removal must each commit the package output",
        )

    def test_a_post_link_durability_failure_is_an_explicit_non_success(self):
        """The required fault injection: complete temp durable, link succeeds, directory fails."""
        self._approve()
        with self._fail_when(self._after_link):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_DURABILITY_UNCONFIRMED, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_durability_unconfirmed")
        self.assertEqual(summary["publication"], "published_durability_unconfirmed")
        self.assertEqual(summary["publication_durability"], "unconfirmed")
        self.assertEqual(summary["reservation"], "confirmed_durable")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertEqual(summary["ledger_record"], "not_attempted")
        self.assertEqual(summary["failure_stage"], "published_package_directory_durability")
        self.assertIs(summary["published_package_preserved"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertIs(summary["controlled_recovery_required"], True)
        self.assertNotIn("synthetic package publication durability failure", out)

    def test_no_clean_build_event_claims_the_unconfirmed_publication(self):
        self._approve()
        with self._fail_when(self._after_link):
            self._build()
        self.assertEqual(self._events_of("build"), [],
                         "an unconfirmed publication must never record a clean build event")
        self.assertEqual(self._events_of("build_cleanup_incomplete"), [])

    def test_the_published_package_is_preserved_and_never_rolled_back(self):
        self._approve()
        with self._fail_when(self._after_link):
            self._build()
        self.assertTrue(self.package.is_file(),
                        "the published package is never deleted to tidy up")
        published = json.loads(self.package.read_text(encoding="utf-8"))
        self.assertEqual(published["schema_version"], contract.SCHEMA_VERSION)
        ok, reasons = contract.validate_package(published)
        self.assertTrue(ok, reasons)
        self.assertEqual(self.competitor.read_bytes(), self.competitor_bytes,
                         "a competing/historical package is never deleted or modified")

    def test_the_consumed_claim_cannot_mint_another_package(self):
        """The approval stays terminally consumed; a fresh output path is refused too."""
        self._approve()
        with self._fail_when(self._after_link):
            self._build()
        second = self.tmp / "member_create_uat_package_second.json"
        code, out = self._build(["--package-out", str(second)])
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        self.assertFalse(second.exists(), "no second package may be minted from the same claim")
        self.assertEqual(self._reservations(), [self._slot().name],
                         "the reservation is neither removed nor recreated")

    def test_the_residual_temporary_state_is_reported_truthfully(self):
        self._approve()
        real_unlink = os.unlink

        def refusing_unlink(path, *args, **kwargs):
            if "mcuat_pkg" in str(path):
                raise OSError(13, "synthetic unlink refusal")
            return real_unlink(path, *args, **kwargs)

        with mock.patch("os.unlink", refusing_unlink):
            with self._fail_when(self._after_link):
                code, out = self._build()
        self.assertEqual(code, approval.EXIT_PUBLICATION_DURABILITY_UNCONFIRMED, out)
        summary = json.loads(out)
        self.assertEqual(summary["temp_cleanup"], "failed")
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertIn("mcuat_pkg", summary["stale_temp_basename"])
        # The reported basename is the real residual temporary, and only ONE remains.
        self.assertEqual(self._temps(), [summary["stale_temp_basename"]])
        self.assertNotIn("synthetic unlink refusal", out)

    def test_a_post_cleanup_durability_failure_is_not_reported_as_a_clean_build(self):
        """The removal may not survive a restart, so exit-0 must not claim a tidy directory."""
        self._approve()
        with self._fail_when(self._after_cleanup):
            code, out = self._build()
        self.assertEqual(code, approval.EXIT_CLEANUP_INCOMPLETE, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "cleanup_incomplete")
        self.assertEqual(summary["temp_cleanup"], "failed")
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertTrue(self.package.is_file())
        self.assertEqual(len(self._events_of("build_cleanup_incomplete")), 1)
        self.assertEqual(self._events_of("build"), [])

    def test_no_directory_sweep_or_glob_cleanup_was_introduced(self):
        """Only the ONE operation-owned temporary is ever touched."""
        unrelated = self.tmp / ".mcuat_pkg_unrelated_sentinel.tmp"
        unrelated.write_text("unrelated\n", encoding="utf-8")
        self._approve()
        with self._fail_when(self._after_link):
            self._build()
        self.assertTrue(unrelated.is_file(),
                        "an unrelated temporary is never swept by the publication writer")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "unrelated\n")

    def test_the_publish_state_vocabulary_names_the_new_outcome_exactly_once(self):
        states = {value for name, value in vars(approval._PublishState).items()
                  if not name.startswith("_")}
        self.assertEqual(
            states,
            {"not_published", "reservation_failed", "reserved_not_published",
             "published_durability_unconfirmed", "published_cleanup_complete",
             "published_cleanup_incomplete"},
        )
        self.assertEqual(approval.EXIT_PUBLICATION_DURABILITY_UNCONFIRMED, 12)


class WindowsPackageVolumeDurabilityTests(_CreateUatBuildHarness):
    """DL-XB-127-001-A1: the residual WINDOWS half of finding PRRT_kwDOSbJI_s6UdzWm.

    `_fsync_parent_directory` performs no directory I/O at all on Windows - it returns
    ``file_fsync_only`` immediately - so before this amendment a Windows build could reach a
    clean exit 0 having never flushed anything on the package output's OWN volume. The only
    real flush after `os.link` was the later approval-ledger fsync, and that cannot stand in
    for it: `contract.assert_safe_local_path` validates one path at a time and imposes no
    same-volume relation between `--ledger` and `--package-out`, so the two may legitimately
    sit on different fixed local NTFS volumes.

    The amendment therefore commits the package output's own path, on the package volume, both
    after the final link and after the operation-owned temporary is removed, and fails closed
    when that barrier cannot be established.

    Every test below works only on a throwaway temporary directory of synthetic fixtures.
    Nothing here touches AutoCount, the AutoCount VM, live n8n, Google, SMB/shared-folder
    state, a real approval ledger or package, real member data, or any credential.
    """

    def setUp(self):
        super().setUp()
        # A competing/historical artifact that must stay byte-identical throughout.
        self.competitor = self.tmp / "member_create_uat_package_v1.json"
        self.competitor.write_text('{"historical": true}\n', encoding="utf-8")
        self.competitor_bytes = self.competitor.read_bytes()

    def _temps(self):
        return sorted(f for f in os.listdir(self.tmp) if "mcuat_pkg" in f)

    def _after_link(self):
        return self.package.exists() and bool(self._temps())

    def _after_cleanup(self):
        return self.package.exists() and not self._temps()

    def _windows_mode(self):
        """Force the Windows package-durability branch deterministically on any host."""
        return mock.patch.object(approval, "_supports_directory_fsync", lambda: False)

    def _barrier(self, seen=None, fail_when=None):
        """Substitute the Windows package barrier, recording targets and forcing failures.

        The real ctypes primitive is exercised separately on Windows only; here the arm is
        selected by observable filesystem state so the assertion is order-independent.
        """
        def hook(out_path):
            if seen is not None:
                seen.append(str(out_path))
            if fail_when is not None and fail_when():
                raise OSError(5, "synthetic package-volume durability barrier failure")
            return approval.PACKAGE_DURABILITY_WINDOWS

        return mock.patch.object(approval, "_flush_windows_package_path", hook)

    # ---------------------------------------------------------------- #
    # A. the barrier exists, runs on the package volume, and runs twice
    # ---------------------------------------------------------------- #
    def test_a_clean_build_never_reports_the_no_io_windows_placeholder(self):
        """Behavioural root-cause proof: `file_fsync_only` performs no I/O, so it can never be
        the durability mode of a CLEAN package publication."""
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertNotEqual(
            json.loads(out)["publication_durability"], "file_fsync_only",
            "a clean publication must never report the no-I/O Windows placeholder mode",
        )

    def test_a_clean_build_reports_the_platform_package_barrier_actually_achieved(self):
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        expected = (approval.PACKAGE_DURABILITY_POSIX if approval._supports_directory_fsync()
                    else approval.PACKAGE_DURABILITY_WINDOWS)
        self.assertEqual(json.loads(out)["publication_durability"], expected)

    def test_the_windows_barrier_is_invoked_after_the_final_link(self):
        self._approve()
        seen = []
        after_link = []
        real_link = os.link

        def watched_link(src, dst):
            result = real_link(src, dst)
            after_link.append(len(seen))
            return result

        with self._windows_mode(), self._barrier(seen=seen), \
                mock.patch.object(os, "link", watched_link):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertEqual(after_link, [0],
                         "no package barrier may run before the final link exists")
        self.assertGreaterEqual(len(seen), 1, "the package barrier must run after the link")

    def test_the_windows_barrier_targets_the_package_path_not_the_ledger(self):
        self._approve()
        seen = []
        with self._windows_mode(), self._barrier(seen=seen):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertTrue(seen, "the package barrier must be invoked")
        for target in seen:
            self.assertEqual(
                Path(target), self.package,
                "the barrier target must be derived from the package output, never the ledger",
            )
            self.assertNotEqual(Path(target), self.ledger)

    def test_the_windows_barrier_is_invoked_again_after_the_temporary_unlink(self):
        self._approve()
        seen = []
        stages = []
        real_unlink = os.unlink

        def watched_unlink(path):
            result = real_unlink(path)
            if "mcuat_pkg" in os.path.basename(str(path)):
                stages.append(len(seen))
            return result

        with self._windows_mode(), self._barrier(seen=seen), \
                mock.patch.object(os, "unlink", watched_unlink):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(stages), 1, "exactly one operation-owned temporary is removed")
        self.assertGreaterEqual(stages[0], 1, "the post-link barrier runs before the unlink")
        self.assertGreater(
            len(seen), stages[0],
            "the package barrier must run AGAIN after the temporary removal, before success",
        )

    # ---------------------------------------------------------------- #
    # B. post-link barrier failure: explicit non-success, nothing rolled back
    # ---------------------------------------------------------------- #
    def _post_link_failure(self):
        self._approve()
        with self._windows_mode(), self._barrier(fail_when=self._after_link):
            return self._build()

    def test_a_post_link_barrier_failure_cannot_reach_clean_success(self):
        code, out = self._post_link_failure()
        self.assertEqual(code, approval.EXIT_PUBLICATION_DURABILITY_UNCONFIRMED, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "publication_durability_unconfirmed")
        self.assertEqual(summary["publication"], "published_durability_unconfirmed")
        self.assertEqual(summary["publication_durability"], "unconfirmed")
        self.assertEqual(summary["failure_stage"], "published_package_directory_durability")
        self.assertEqual(summary["build_claim"], "committed")
        self.assertIs(summary["published_package_preserved"], True)
        self.assertIs(summary["approval_blocked"], True)
        self.assertIs(summary["do_not_retry"], True)
        self.assertIs(summary["fresh_approval_required"], True)
        self.assertNotIn("synthetic package-volume durability barrier failure", out)

    def test_a_post_link_barrier_failure_emits_no_clean_build_event(self):
        self._post_link_failure()
        self.assertEqual(self._events_of("build"), [],
                         "an unconfirmed package publication never records a clean build event")
        self.assertEqual(self._events_of("build_cleanup_incomplete"), [])

    def test_a_post_link_barrier_failure_preserves_the_published_package(self):
        self._post_link_failure()
        self.assertTrue(self.package.is_file(),
                        "the published package is never deleted, rolled back or renamed")
        published = json.loads(self.package.read_text(encoding="utf-8"))
        ok, reasons = contract.validate_package(published)
        self.assertTrue(ok, reasons)

    def test_a_post_link_barrier_failure_cannot_mint_a_second_package(self):
        self._post_link_failure()
        second = self.tmp / "member_create_uat_package_second.json"
        code, out = self._build(["--package-out", str(second)])
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        self.assertFalse(second.exists(), "a consumed claim can never mint a second package")
        self.assertEqual(self._reservations(), [self._slot().name],
                         "the consumed reservation is neither removed nor recreated")

    # ---------------------------------------------------------------- #
    # C. post-unlink barrier failure: truthful cleanup/durability uncertainty
    # ---------------------------------------------------------------- #
    def _post_unlink_failure(self):
        self._approve()
        with self._windows_mode(), self._barrier(fail_when=self._after_cleanup):
            return self._build()

    def test_a_post_unlink_barrier_failure_cannot_reach_exit_zero(self):
        code, out = self._post_unlink_failure()
        self.assertNotEqual(code, 0, "durability uncertainty is never reported as success")
        self.assertEqual(code, approval.EXIT_CLEANUP_INCOMPLETE, out)
        self.assertNotIn("Traceback", out)
        summary = json.loads(out)
        self.assertEqual(summary["status"], "cleanup_incomplete")
        self.assertEqual(summary["temp_cleanup"], "failed")
        self.assertIs(summary["manual_cleanup_required"], True)
        self.assertEqual(summary["event"], "build_cleanup_incomplete")
        self.assertTrue(self.package.is_file(),
                        "the published package is preserved through a cleanup/durability doubt")
        self.assertNotIn("synthetic package-volume durability barrier failure", out)

    def test_a_post_unlink_barrier_failure_records_no_clean_build_event(self):
        self._post_unlink_failure()
        self.assertEqual(self._events_of("build"), [],
                         "a cleanup/durability-uncertain publication is never a clean build")
        self.assertEqual(len(self._events_of("build_cleanup_incomplete")), 1)

    def test_a_post_unlink_barrier_failure_cannot_mint_a_second_package(self):
        self._post_unlink_failure()
        second = self.tmp / "member_create_uat_package_second.json"
        code, out = self._build(["--package-out", str(second)])
        self.assertEqual(code, approval.EXIT_APPROVAL_CONSUMED, out)
        self.assertFalse(second.exists(), "a consumed claim can never mint a second package")

    # ---------------------------------------------------------------- #
    # D. mandatory cross-volume negative control
    # ---------------------------------------------------------------- #
    def test_flushing_only_the_ledger_volume_cannot_obtain_clean_success(self):
        """Ledger/state on synthetic volume A, package output on synthetic volume B.

        Volume A's own durability calls - the reservation directory helper and the real ledger
        append fsync - are left fully working. Only volume B's package barrier is unavailable.
        Clean success must still be impossible, which is exactly what a later ledger-volume
        flush could never prove.
        """
        self._approve()
        volume_a = []
        real_helper = approval._fsync_parent_directory
        real_fsync = os.fsync
        ledger_flushes = []

        def volume_a_helper(directory):
            volume_a.append(str(directory))
            return real_helper(directory)

        def counted_fsync(fd):
            ledger_flushes.append(fd)
            return real_fsync(fd)

        with self._windows_mode(), \
                self._barrier(fail_when=self._after_link), \
                mock.patch.object(approval, "_fsync_parent_directory", volume_a_helper), \
                mock.patch.object(os, "fsync", counted_fsync):
            code, out = self._build()

        self.assertTrue(volume_a, "the ledger/state volume helper must still have been used")
        self.assertTrue(ledger_flushes, "real volume-A file flushes must still have succeeded")
        self.assertEqual(
            code, approval.EXIT_PUBLICATION_DURABILITY_UNCONFIRMED,
            "a working ledger volume can never substitute for the package volume barrier: "
            + out,
        )
        self.assertEqual(json.loads(out)["publication_durability"], "unconfirmed")
        self.assertEqual(self._events_of("build"), [])

    def test_the_package_barrier_target_is_independent_of_the_ledger_location(self):
        """The barrier target is derived from --package-out even when --ledger is elsewhere."""
        self._approve()
        other_dir = self.tmp / "volume_b"
        other_dir.mkdir()
        elsewhere = other_dir / "member_create_uat_package_v2.json"
        seen = []
        with self._windows_mode(), self._barrier(seen=seen):
            code, out = self._build(["--package-out", str(elsewhere)])
        self.assertEqual(code, 0, out)
        self.assertTrue(seen)
        for target in seen:
            self.assertEqual(Path(target), elsewhere)
            self.assertNotEqual(Path(target).parent, self.ledger.parent)

    # ---------------------------------------------------------------- #
    # E. nothing else is touched
    # ---------------------------------------------------------------- #
    def test_a_competing_historical_package_stays_byte_identical(self):
        self._post_link_failure()
        self.assertEqual(self.competitor.read_bytes(), self.competitor_bytes,
                         "a competing/historical package is never deleted or modified")

    def test_an_unrelated_temporary_is_never_swept(self):
        unrelated = self.tmp / ".mcuat_pkg_unrelated.tmp"
        unrelated.write_text("unrelated\n", encoding="utf-8")
        self._post_link_failure()
        self.assertTrue(unrelated.is_file(),
                        "the publication writer never sweeps or globs other temporaries")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "unrelated\n")

    # ---------------------------------------------------------------- #
    # F. POSIX non-regression and reservation/ledger preservation
    # ---------------------------------------------------------------- #
    @unittest.skipUnless(hasattr(os, "O_DIRECTORY"), "POSIX directory fsync only")
    def test_posix_publication_durability_is_unchanged(self):
        self._approve()
        seen = []
        real_helper = approval._fsync_parent_directory
        with mock.patch.object(approval, "_fsync_parent_directory",
                               lambda d: seen.append(str(d)) or real_helper(d)):
            code, out = self._build()
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["publication_durability"], "file_and_directory_fsync")
        self.assertGreaterEqual(
            seen.count(str(self.package.parent)), 3,
            "POSIX still commits the reservation, the final link and the temporary removal",
        )

    def test_the_reservation_durability_reporting_is_preserved(self):
        """Toolkit #342: the shared reservation/ledger helper's behaviour is untouched."""
        self._approve()
        code, out = self._build()
        self.assertEqual(code, 0, out)
        expected = ("file_and_directory_fsync" if hasattr(os, "O_DIRECTORY")
                    else "file_fsync_only")
        self.assertEqual(json.loads(out)["reservation_durability"], expected,
                         "reservation durability reporting is deliberately untouched")

    @unittest.skipUnless(os.name == "nt", "the real Windows primitive requires Windows")
    def test_the_real_windows_primitive_commits_the_package_path(self):
        """Exercise the actual documented primitive against a real NTFS package file."""
        target = self.tmp / "real_barrier_probe.json"
        target.write_text('{"probe": true}\n', encoding="utf-8")
        self.assertEqual(approval._flush_windows_package_path(target),
                         approval.PACKAGE_DURABILITY_WINDOWS)
        self.assertEqual(target.read_text(encoding="utf-8"), '{"probe": true}\n',
                         "the barrier never truncates or modifies the package")

    @unittest.skipUnless(os.name == "nt", "the real Windows primitive requires Windows")
    def test_the_real_windows_primitive_fails_closed_on_a_missing_path(self):
        missing = self.tmp / "absent_package.json"
        with self.assertRaises(OSError):
            approval._flush_windows_package_path(missing)

if __name__ == "__main__":
    unittest.main()
