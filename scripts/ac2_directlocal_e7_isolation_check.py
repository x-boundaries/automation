"""#155 E7 outbound-isolation evidence for the AED_TESTINGAPI test book.

Read-only. SQL statements are fixed SELECTs passed through a SELECT-only guard
under READ UNCOMMITTED with a lock timeout; host checks are listings. Output
is PII-free (counts, flags, process names, hashed host names) and carries a
verdict consumed by scripts/ac2_directlocal_e7_testbook_create.ps1.

``classify`` is the pure decision: ISOLATED only when nothing that can carry
a Member change outward is present or active for the book.
"""

from __future__ import annotations

import csv
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
from typing import Any, Mapping

BOOK = "AED_TESTINGAPI"
# Local AutoCount vendor processes observed on the book: the interactive
# desktop client and the AutoCount application server. Neither is a
# Member-dispatch channel; any other process touching the book blocks.
ALLOWED_LOCAL_PROCESSES = frozenset({"Accounting.exe", "AutoCountServerService.exe"})
WRITE_WORDS = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|backup|restore|dbcc|kill|shutdown|into|openrowset|opendatasource|bulk)\b", re.I)
EXTERNAL_MARKERS = re.compile(r"sp_send_dbmail|xp_cmdshell|sp_OA|openrowset|opendatasource|openquery|send\s+on\s+conversation|sp_start_job|http", re.I)


def guard(sql: str) -> str:
    stripped = re.sub(r"'[^']*'", "''", sql)
    if not re.match(r"^\s*(WITH|SELECT)\b", stripped, re.I) or WRITE_WORDS.search(stripped) or ";" in stripped.strip().rstrip(";"):
        raise SystemExit("read-only guard refused a statement")
    return sql


def classify(evidence: Mapping[str, Any]) -> list[str]:
    """Return blocker codes; an empty list means ISOLATED."""

    blockers = []
    for item in evidence["touching_processes"]:
        if item["process"] not in ALLOWED_LOCAL_PROCESSES or item["client"] != "<local machine>":
            blockers.append("unexpected_process_touches_book")
            break
    if evidence["sql_agent_jobs_for_book"]:
        blockers.append("sql_agent_jobs_reference_book")
    if any(t["external_markers"] or t["writes_tables"] not in (["ChangeLog"], ["ChangeCount"]) for t in evidence["member_triggers"]):
        blockers.append("member_trigger_not_internal_only")
    instance = evidence["instance"]
    if instance["linked_servers"] or instance["xp_cmdshell"] or instance["server_triggers"] or instance["book_broker_enabled"] or instance["database_mail_xps"]:
        blockers.append("instance_external_capability_present")
    if evidence["book_user_assemblies"] or evidence["book_database_triggers"]:
        blockers.append("book_external_capability_present")
    if evidence["scheduled_tasks_mentioning_book"] or evidence["services_mentioning_book"]:
        blockers.append("host_job_references_book")
    if evidence["pos_registrations_active_after_clone"]:
        blockers.append("pos_sync_active_on_book")
    if evidence["plugins_allowing_sync"]:
        blockers.append("plugin_sync_enabled")
    if evidence["mail_queue_rows"] or evidence["do_email_smtp_configured"] or evidence["onerewards_members"]:
        blockers.append("mail_or_rewards_channel_active")
    return blockers


def _processes() -> dict[int, str]:
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=60).stdout
    return {int(row[1]): row[0] for row in csv.reader(io.StringIO(out)) if len(row) > 1 and row[1].isdigit()}


def collect(samples: int = 6, interval: float = 10.0) -> dict[str, Any]:
    import pyodbc  # VM-only dependency

    conn = pyodbc.connect(os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"], autocommit=True, timeout=15)
    cur = conn.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
    cur.execute("SET LOCK_TIMEOUT 3000")

    def q(sql: str, *params):
        cur.execute(guard(sql), params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    me = q("SELECT @@SPID AS spid")[0]["spid"]
    touching: dict[tuple, int] = {}
    for index in range(samples):
        procs = _processes()
        for row in q(
            "SELECT s.program_name, s.host_process_id, c.client_net_address FROM sys.dm_exec_sessions s "
            "LEFT JOIN sys.dm_exec_connections c ON c.session_id = s.session_id WHERE s.is_user_process = 1 AND s.session_id <> ? "
            "AND (s.database_id = DB_ID(?) OR s.session_id IN (SELECT l.request_session_id FROM sys.dm_tran_locks l WHERE l.resource_database_id = DB_ID(?)))",
            me, BOOK, BOOK):
            key = (procs.get(row["host_process_id"] or -1, "unknown"), row["client_net_address"])
            touching[key] = touching.get(key, 0) + 1
        if index + 1 < samples:
            time.sleep(interval)
    db = f"[{BOOK}]"
    evidence: dict[str, Any] = {
        "book": BOOK, "run_at": datetime.now(timezone.utc).isoformat(), "samples": samples,
        "touching_processes": [{"process": k[0], "client": k[1], "observations": v} for k, v in sorted(touching.items())],
    }
    evidence["instance"] = q(
        "SELECT (SELECT COUNT(*) FROM sys.servers WHERE is_linked = 1) AS linked_servers, "
        "(SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name = 'xp_cmdshell') AS xp_cmdshell, "
        "(SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name = 'Database Mail XPs') AS database_mail_xps, "
        "(SELECT COUNT(*) FROM sys.server_triggers) AS server_triggers, "
        "(SELECT CAST(is_broker_enabled AS int) FROM sys.databases WHERE name = ?) AS book_broker_enabled", BOOK)[0]
    evidence["sql_agent_jobs_for_book"] = q("SELECT COUNT(*) AS n FROM msdb.dbo.sysjobsteps WHERE database_name = ? OR command LIKE ?", BOOK, f"%{BOOK}%")[0]["n"]
    triggers = q(f"SELECT tr.name, m.definition FROM {db}.sys.triggers tr JOIN {db}.sys.sql_modules m ON m.object_id = tr.object_id WHERE tr.parent_id = OBJECT_ID('{BOOK}.dbo.Member') AND tr.is_disabled = 0")
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
    evidence["do_email_smtp_configured"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.TD_DO_EmailSetting WHERE ISNULL(SMTPServer, '') <> ''")[0]["n"]
    evidence["onerewards_members"] = q(f"SELECT COUNT(*) AS n FROM {db}.dbo.OneRewards_Member")[0]["n"]
    conn.close()
    tasks = subprocess.run(["schtasks", "/query", "/fo", "csv", "/v"], capture_output=True, text=True, timeout=120).stdout
    evidence["scheduled_tasks_mentioning_book"] = sum(1 for line in tasks.splitlines() if BOOK.lower() in line.lower())
    services = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Service | ForEach-Object { $_.Name + '|' + $_.PathName }"], capture_output=True, text=True, timeout=120).stdout
    evidence["services_mentioning_book"] = [line.split("|")[0] for line in services.splitlines() if BOOK.lower() in line.lower()]
    blockers = classify(evidence)
    evidence["blockers"] = blockers
    evidence["verdict"] = "ISOLATED" if not blockers else "NOT_PROVEN"
    return evidence


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: ac2_directlocal_e7_isolation_check.py <evidence-dir>\n")
        return 2
    out_dir = Path(argv[0])
    if not out_dir.is_dir():
        sys.stderr.write("evidence directory missing\n")
        return 2
    evidence = collect()
    path = out_dir / f"isolation_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    sys.stdout.write(json.dumps({"verdict": evidence["verdict"], "blockers": evidence["blockers"], "evidence": str(path)}) + "\n")
    return 0 if evidence["verdict"] == "ISOLATED" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
