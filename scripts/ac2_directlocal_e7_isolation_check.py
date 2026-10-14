"""#155 E7 outbound-isolation evidence for the AED_TESTINGAPI test book.

Read-only. SQL statements are fixed SELECTs passed through a SELECT-only guard
under READ UNCOMMITTED with a lock timeout; host checks are listings. Output
is PII-free (counts, flags, process names, table names) and carries a verdict
that scripts/ac2_directlocal_e7_testbook_create.ps1 re-validates field by field.

Fail closed: every inspection records ok/code in ``collection``. A failed,
partial or untrustworthy inspection (non-elevated host inventory, non-zero
tool exit, unparseable or implausibly small listings, unattributed sessions,
SQL errors) makes the evidence incomplete and the verdict NOT_PROVEN.

AutoCountServerService qualification (2026-10-14): its only statements in the
book's context read ``sys`` metadata and poll ``MailQueue`` (its mail sender);
it never reads Member, ChangeLog or ChangeCount. The E7 write path
(MemberCommand.Create/GetNextMemberNo/GetMember/NewMember/SaveMember plus
MemberEntity accessors; IL closure of 529 methods in the pinned assemblies)
references no MailQueue, EInvoiceQueue, Notification, SMTP or MailKit code;
Member triggers only write ChangeLog/ChangeCount. Hence the pins below: the
static proof holds only for these exact assembly bytes.
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "xb155-e7-isolation-v2"
BOOK = "AED_TESTINGAPI"
HOST = "DESKTOP-4I042L6"
SAMPLES = 6
# Local AutoCount vendor processes observed on the book: the interactive
# desktop client and the AutoCount application server. Any other process,
# any remote client or any unattributed session touching the book blocks.
ALLOWED_LOCAL_PROCESSES = frozenset({"Accounting.exe", "AutoCountServerService.exe"})
# Tables AutoCountServerService may reference in the book's context.
ACSERVER_ALLOWED_TABLES = frozenset({"sys", "mailqueue"})
AC_ROOT = Path(r"C:\Program Files\AutoCount\Accounting 2.2")
QUALIFIED_ASSEMBLIES = {
    "AutoCount.dll": "8e86273018c01c715e440c7975f759951f4a0e979fc3edb0c281db6696090f93",
    "AutoCount.Invoicing.dll": "5c748b71e54f13313968276530fe110412a4d97a3c9b44d8f43100379849967f",
}
INSPECTIONS = ("host", "processes", "sessions", "sql_capabilities", "book_channels", "scheduled_tasks", "services", "assemblies")
ZERO_COUNTS = (
    "sql_agent_jobs_for_book", "book_user_assemblies", "book_database_triggers", "scheduled_tasks_mentioning_book",
    "pos_registrations_active_after_clone", "plugins_allowing_sync", "mail_queue_rows", "einvoice_queue_rows",
    "user_script_rows", "do_email_smtp_configured", "onerewards_members",
)
INSTANCE_FLAGS = ("linked_servers", "xp_cmdshell", "server_triggers", "book_broker_enabled", "database_mail_xps")
WRITE_WORDS = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|backup|restore|dbcc|kill|shutdown|into|openrowset|opendatasource|bulk)\b", re.I)
EXTERNAL_MARKERS = re.compile(r"sp_send_dbmail|xp_cmdshell|sp_OA|openrowset|opendatasource|openquery|send\s+on\s+conversation|sp_start_job|http", re.I)
TABLE_REFS = re.compile(r"(?i)\b(?:from|join|update|into)\s+(?:\[?dbo\]?\.)?\[?([A-Za-z_]\w*)\]?")


class InspectionError(Exception):
    """A bounded, PII-free inspection failure code."""


def guard(sql: str) -> str:
    stripped = re.sub(r"'[^']*'", "''", sql)
    if not re.match(r"^\s*(WITH|SELECT)\b", stripped, re.I) or WRITE_WORDS.search(stripped) or ";" in stripped.strip().rstrip(";"):
        raise SystemExit("read-only guard refused a statement")
    return sql


def classify(evidence: Mapping[str, Any]) -> list[str]:
    """Return blocker codes; an empty list means ISOLATED. Missing data blocks."""

    blockers: list[str] = []
    collection = evidence.get("collection")
    if not isinstance(collection, Mapping) or any(not isinstance(collection.get(n), Mapping) or collection[n].get("ok") is not True for n in INSPECTIONS):
        blockers.append("collection_incomplete")
    try:
        if evidence.get("schema") != SCHEMA or evidence.get("book") != BOOK or str(evidence.get("host", "")).upper() != HOST:
            blockers.append("evidence_identity_wrong")
        if evidence["samples"] < SAMPLES or evidence["sample_failures"] != 0:
            blockers.append("collection_incomplete")
        for item in evidence["touching_processes"]:
            if item["process"] not in ALLOWED_LOCAL_PROCESSES or item["client"] != "<local machine>":
                blockers.append("unexpected_process_touches_book")
                break
        if set(evidence["acserver_book_tables"]) - ACSERVER_ALLOWED_TABLES:
            blockers.append("acserver_unqualified_book_activity")
        if any(t["external_markers"] or t["writes_tables"] not in (["ChangeLog"], ["ChangeCount"]) for t in evidence["member_triggers"]):
            blockers.append("member_trigger_not_internal_only")
        if any(evidence["instance"][flag] != 0 for flag in INSTANCE_FLAGS):
            blockers.append("instance_external_capability_present")
        if evidence["book_user_assemblies"] or evidence["book_database_triggers"]:
            blockers.append("book_external_capability_present")
        if evidence["sql_agent_jobs_for_book"]:
            blockers.append("sql_agent_jobs_reference_book")
        if evidence["scheduled_tasks_mentioning_book"] or evidence["services_mentioning_book"]:
            blockers.append("host_job_references_book")
        if evidence["pos_registrations_active_after_clone"]:
            blockers.append("pos_sync_active_on_book")
        if evidence["plugins_allowing_sync"]:
            blockers.append("plugin_sync_enabled")
        if evidence["mail_queue_rows"] or evidence["einvoice_queue_rows"] or evidence["do_email_smtp_configured"] or evidence["onerewards_members"]:
            blockers.append("mail_or_rewards_channel_active")
        if evidence["user_script_rows"]:
            blockers.append("user_script_present")
        if evidence["assemblies"] != QUALIFIED_ASSEMBLIES:
            blockers.append("autocount_build_not_qualified")
    except (KeyError, TypeError):
        blockers.append("evidence_field_missing")
    return sorted(set(blockers))


def _run(args: list[str], timeout: int) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError(f"{Path(args[0]).stem}_failed") from exc
    if proc.returncode != 0:
        raise InspectionError(f"{Path(args[0]).stem}_exit_nonzero")
    return proc.stdout


def processes() -> dict[int, str]:
    rows = [r for r in csv.reader(io.StringIO(_run(["tasklist", "/fo", "csv", "/nh"], 60))) if r]
    table = {int(r[1]): r[0] for r in rows if len(r) > 1 and r[1].isdigit()}
    # Trustworthy only when every row parsed and this process is listed.
    if len(table) != len(rows) or len(table) < 20 or os.getpid() not in table:
        raise InspectionError("process_listing_untrustworthy")
    return table


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def scheduled_task_hits() -> int:
    # Non-elevated listings silently omit tasks the user cannot read.
    if not is_elevated():
        raise InspectionError("task_listing_requires_elevation")
    rows = [r for r in csv.reader(io.StringIO(_run(["schtasks", "/query", "/fo", "csv", "/v", "/nh"], 180))) if r]
    if len(rows) < 20 or any(len(r) < 10 for r in rows):
        raise InspectionError("task_listing_untrustworthy")
    return sum(1 for r in rows if any(BOOK.lower() in cell.lower() for cell in r))


def service_hits() -> list[str]:
    if not is_elevated():
        raise InspectionError("service_listing_requires_elevation")
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Service -ErrorAction Stop | Select-Object Name, PathName | ConvertTo-Json -Compress"], 180)
    try:
        services = json.loads(raw)
    except ValueError as exc:
        raise InspectionError("service_listing_unparseable") from exc
    if not isinstance(services, list) or len(services) < 50 or any(not s.get("Name") or not s.get("PathName") for s in services):
        raise InspectionError("service_listing_untrustworthy")
    return sorted(s["Name"] for s in services if BOOK.lower() in (s["Name"] + "|" + s["PathName"]).lower())


def assembly_hashes(root: Path = AC_ROOT) -> dict[str, str]:
    try:
        return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in QUALIFIED_ASSEMBLIES}
    except OSError as exc:
        raise InspectionError("assembly_unreadable") from exc


def _inspect(evidence: dict[str, Any], name: str, step: Callable[[], None]) -> None:
    try:
        step()
        evidence["collection"][name] = {"ok": True}
    except InspectionError as exc:
        evidence["collection"][name] = {"ok": False, "code": str(exc)}
    except Exception as exc:  # noqa: BLE001 - any failure is recorded, never trusted
        evidence["collection"][name] = {"ok": False, "code": f"{type(exc).__name__.lower()}_raised"}


def collect(samples: int = SAMPLES, interval: float = 10.0) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema": SCHEMA, "book": BOOK, "host": os.environ.get("COMPUTERNAME", ""), "run_at": datetime.now(timezone.utc).isoformat(),
        "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "elevated": is_elevated(),
        "samples": 0, "sample_failures": 0, "collection": {},
    }

    def host() -> None:
        if evidence["host"].upper() != HOST:
            raise InspectionError("host_not_authorised")

    _inspect(evidence, "host", host)
    _inspect(evidence, "processes", lambda: processes())
    conn = None
    try:
        import pyodbc  # VM-only dependency

        conn = pyodbc.connect(os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"], autocommit=True, timeout=15)
        cur = conn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        cur.execute("SET LOCK_TIMEOUT 3000")
    except Exception:  # noqa: BLE001
        conn = None

    def q(sql: str, *params) -> list[dict[str, Any]]:
        if conn is None:
            raise InspectionError("sql_connection_failed")
        cur.execute(guard(sql), params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def sessions() -> None:
        me = q("SELECT @@SPID AS spid")[0]["spid"]
        touching: dict[tuple, int] = {}
        acserver_tables: set[str] = set()
        for index in range(samples):
            try:
                procs = processes()
                rows = q(
                    "SELECT s.host_process_id, c.client_net_address, t.text AS last_sql FROM sys.dm_exec_sessions s "
                    "LEFT JOIN sys.dm_exec_connections c ON c.session_id = s.session_id OUTER APPLY sys.dm_exec_sql_text(c.most_recent_sql_handle) t "
                    "WHERE s.is_user_process = 1 AND s.session_id <> ? "
                    "AND (s.database_id = DB_ID(?) OR s.session_id IN (SELECT l.request_session_id FROM sys.dm_tran_locks l WHERE l.resource_database_id = DB_ID(?)))",
                    me, BOOK, BOOK)
                for row in rows:
                    name = procs.get(row["host_process_id"] or -1, "unattributed")
                    key = (name, row["client_net_address"] or "unknown")
                    touching[key] = touching.get(key, 0) + 1
                    if name == "AutoCountServerService.exe":
                        acserver_tables.update(n.lower() for n in TABLE_REFS.findall(row["last_sql"] or ""))
                evidence["samples"] += 1
            except InspectionError:
                evidence["sample_failures"] += 1
            if index + 1 < samples:
                time.sleep(interval)
        evidence["touching_processes"] = [{"process": k[0], "client": k[1], "observations": v} for k, v in sorted(touching.items())]
        evidence["acserver_book_tables"] = sorted(acserver_tables)
        if evidence["sample_failures"] or evidence["samples"] != samples:
            raise InspectionError("session_sampling_incomplete")

    def sql_capabilities() -> None:
        evidence["instance"] = q(
            "SELECT (SELECT COUNT(*) FROM sys.servers WHERE is_linked = 1) AS linked_servers, "
            "(SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name = 'xp_cmdshell') AS xp_cmdshell, "
            "(SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name = 'Database Mail XPs') AS database_mail_xps, "
            "(SELECT COUNT(*) FROM sys.server_triggers) AS server_triggers, "
            "(SELECT CAST(is_broker_enabled AS int) FROM sys.databases WHERE name = ?) AS book_broker_enabled", BOOK)[0]
        if any(evidence["instance"][flag] is None for flag in INSTANCE_FLAGS):
            raise InspectionError("instance_flags_incomplete")
        evidence["sql_agent_jobs_for_book"] = q("SELECT COUNT(*) AS n FROM msdb.dbo.sysjobsteps WHERE database_name = ? OR command LIKE ?", BOOK, f"%{BOOK}%")[0]["n"]

    def book_channels() -> None:
        db = f"[{BOOK}]"
        triggers = q(f"SELECT tr.name, m.definition FROM {db}.sys.triggers tr JOIN {db}.sys.sql_modules m ON m.object_id = tr.object_id WHERE tr.parent_id = OBJECT_ID('{BOOK}.dbo.Member') AND tr.is_disabled = 0")
        if any(t["definition"] is None for t in triggers):
            raise InspectionError("trigger_definition_unreadable")
        evidence["member_triggers"] = [{
            "name": t["name"], "definition_sha12": hashlib.sha256(t["definition"].encode()).hexdigest()[:12],
            "external_markers": sorted({m.group(0).lower() for m in EXTERNAL_MARKERS.finditer(t["definition"])}),
            "writes_tables": sorted(set(re.findall(r"(?i)\b(?:insert\s+into|update)\s+(?:dbo\.)?\[?(\w+)\]?", t["definition"]))),
        } for t in triggers]
        evidence["book_user_assemblies"] = q(f"SELECT COUNT(*) AS n FROM {db}.sys.assemblies WHERE is_user_defined = 1")[0]["n"]
        evidence["book_database_triggers"] = q(f"SELECT COUNT(*) AS n FROM {db}.sys.triggers WHERE parent_class = 0")[0]["n"]
        created = q("SELECT create_date FROM sys.databases WHERE name = ?", BOOK)[0]["create_date"]
        evidence["book_create_date"] = str(created)
        evidence["pos_registrations_active_after_clone"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.SyncRegistration WHERE LastAliveTime >= ? OR LastFinishSyncTime >= ?", created, created)[0]["n"]
        evidence["plugins_allowing_sync"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.PlugIn WHERE AllowSync = 'T'")[0]["n"]
        evidence["mail_queue_rows"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.MailQueue")[0]["n"]
        evidence["einvoice_queue_rows"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.EInvoiceQueue")[0]["n"]
        evidence["user_script_rows"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.UserScript")[0]["n"]
        evidence["do_email_smtp_configured"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.TD_DO_EmailSetting WHERE ISNULL(SMTPServer, '') <> ''")[0]["n"]
        evidence["onerewards_members"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.OneRewards_Member")[0]["n"]

    def tasks() -> None:
        evidence["scheduled_tasks_mentioning_book"] = scheduled_task_hits()

    def services() -> None:
        evidence["services_mentioning_book"] = service_hits()

    def assemblies() -> None:
        evidence["assemblies"] = assembly_hashes()

    _inspect(evidence, "sessions", sessions)
    _inspect(evidence, "sql_capabilities", sql_capabilities)
    _inspect(evidence, "book_channels", book_channels)
    if conn is not None:
        conn.close()
    _inspect(evidence, "scheduled_tasks", tasks)
    _inspect(evidence, "services", services)
    _inspect(evidence, "assemblies", assemblies)
    evidence["completed_at"] = datetime.now(timezone.utc).isoformat()
    evidence["blockers"] = classify(evidence)
    evidence["verdict"] = "ISOLATED" if not evidence["blockers"] else "NOT_PROVEN"
    return evidence


def write_evidence(out_dir: Path, evidence: Mapping[str, Any]) -> tuple[Path, str]:
    """Write once (exclusive create) and return the path and the file's SHA-256."""

    data = json.dumps(evidence, indent=2, default=str, sort_keys=True).encode("utf-8")
    path = out_dir / f"isolation_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return path, hashlib.sha256(data).hexdigest()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: ac2_directlocal_e7_isolation_check.py <evidence-dir>\n")
        return 2
    out_dir = Path(argv[0])
    if not out_dir.is_dir():
        sys.stderr.write("evidence directory missing\n")
        return 2
    evidence = collect()
    path, digest = write_evidence(out_dir, evidence)
    failed = sorted(n for n, v in evidence["collection"].items() if not v.get("ok"))
    sys.stdout.write(json.dumps({"verdict": evidence["verdict"], "blockers": evidence["blockers"], "failed_inspections": failed,
                                 "evidence": str(path), "sha256": digest}) + "\n")
    return 0 if evidence["verdict"] == "ISOLATED" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
