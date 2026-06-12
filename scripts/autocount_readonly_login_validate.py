import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4


DEFAULT_CONFIG_PATH = Path("config/autocount_readonly_login_validate.example.json")
DEFAULT_CONNECTION_STRING_ENV = "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"
DEFAULT_OUTPUT_ROOT = r"C:\XB\autocount_outputs\probe\readonly_login"
DEFAULT_SMOKE_SURFACES = [
    {"schema_name": "dbo", "object_name": "Item"},
    {"schema_name": "dbo", "object_name": "ItemUOM"},
    {"schema_name": "dbo", "object_name": "vItemBalQty"},
    {"schema_name": "dbo", "object_name": "StockDTL"},
]
WRITE_LIKE_PERMISSIONS = ["INSERT", "UPDATE", "DELETE", "ALTER", "CONTROL", "TAKE OWNERSHIP"]
SECRET_PATTERN = re.compile(
    r"(?i)\b(password|pwd|token|secret|api[_-]?key|access[_-]?key)\s*=\s*[^;\s]+"
)


def load_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run_validation(config, source=None, now=None):
    plan = build_run_plan(config, now=now)
    run_path = Path(plan["run_path"])
    run_path.mkdir(parents=True, exist_ok=False)

    surfaces = normalize_surfaces(config.get("smoke_surfaces") or DEFAULT_SMOKE_SURFACES)
    source = source or create_source(config)
    exceptions = []
    context = {}
    surface_results = []
    permission_rows = []
    permission_advisory = empty_permission_advisory()

    try:
        context = source.fetch_context()
        surface_results = [source.check_surface(surface) for surface in surfaces]
        permission_rows = source.fetch_permission_advisory(surfaces, WRITE_LIKE_PERMISSIONS)
        permission_advisory = evaluate_permission_advisory(permission_rows)
    except Exception as exc:  # noqa: BLE001 - validation manifest should capture safe failure detail.
        exceptions.append(sanitize_text(str(exc)))

    readable = bool(surface_results) and all(
        result.get("exists") and result.get("metadata_select_top_0") for result in surface_results
    )
    status = "success"
    if exceptions or not readable or permission_advisory["write_like_permission_detected"]:
        status = "failed"

    manifest = {
        "job": config.get("job", "autocount_readonly_login_validate"),
        "status": status,
        "run_id": plan["run_id"],
        "started_at": plan["started_at"],
        "finished_at": datetime.now().astimezone().isoformat(),
        "target_label": sanitize_text(config.get("target_label", "")),
        "connection_string_env": config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV),
        "checked_surfaces": [surface_id(surface) for surface in surfaces],
        "context": sanitize_context(context),
        "read_capability": {
            "all_configured_surfaces_readable": readable,
            "surface_results": surface_results,
        },
        "write_permission_advisory": permission_advisory,
        "exception_count": len(exceptions),
        "exceptions": exceptions,
        "storage": {
            "output_root": plan["output_root"],
            "run_path": plan["run_path"],
            "manifest": str(run_path / "readonly_login_validation_manifest.json"),
        },
        "notes": [
            "Permission checks are advisory metadata checks only.",
            "This validator does not attempt writes and does not prove final production approval.",
            "Final approval still requires DBA/admin and operator sign-off before scheduling or scope expansion.",
        ],
    }

    manifest_path = run_path / "readonly_login_validation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def build_run_plan(config, now=None):
    started_at = _coerce_datetime(now) if now else datetime.now().astimezone()
    run_id = str(uuid4())
    output_root = resolve_output_root(config.get("output_root") or DEFAULT_OUTPUT_ROOT)
    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "output_root": str(output_root),
        "run_path": str(output_root / f"readonly_login_validation_{started_at.strftime('%Y%m%d_%H%M%S')}_{run_id[:8]}"),
    }


def resolve_output_root(output_root, repo_root=None):
    path = Path(output_root).expanduser()
    resolved = path.resolve(strict=False)
    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve(strict=False)
    if _is_relative_to(resolved, root):
        raise ValueError(f"Output root must be outside the repository: {resolved}")
    return resolved


def create_source(config):
    env_name = config.get("connection_string_env", DEFAULT_CONNECTION_STRING_ENV)
    connection_string = os.environ.get(env_name)
    if not connection_string:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return SqlServerReadonlyValidationSource(connection_string)


class SqlServerReadonlyValidationSource:
    def __init__(self, connection_string):
        self.connection_string = connection_string

    def fetch_context(self):
        rows = self.query(
            """
            SELECT
              DB_NAME() AS current_database,
              SUSER_SNAME() AS current_login,
              USER_NAME() AS current_user_name
            """
        )
        return rows[0] if rows else {}

    def check_surface(self, surface):
        rows = self.query(
            """
            SELECT
              s.name AS schema_name,
              o.name AS object_name,
              o.type_desc AS object_type
            FROM sys.objects AS o
            INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            WHERE s.name = ? AND o.name = ? AND o.type IN ('U', 'V')
            """,
            [surface["schema_name"], surface["object_name"]],
        )
        exists = bool(rows)
        top_0_ok = False
        if exists:
            self.query(f"SELECT TOP (0) * FROM {quote_identifier(surface['schema_name'])}.{quote_identifier(surface['object_name'])}")
            top_0_ok = True
        return {
            "surface": surface_id(surface),
            "exists": exists,
            "metadata_select_top_0": top_0_ok,
            "object_type": rows[0].get("object_type", "") if rows else "",
        }

    def fetch_permission_advisory(self, surfaces, permissions):
        rows = []
        for permission in permissions:
            result = self.query(
                "SELECT CAST(? AS nvarchar(128)) AS permission_name, HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', ?) AS has_permission",
                [permission, permission],
            )
            rows.append(
                {
                    "scope": "database",
                    "surface": "",
                    "permission_name": permission,
                    "has_permission": result[0].get("has_permission") if result else None,
                }
            )
        for surface in surfaces:
            entity_name = f"{quote_identifier(surface['schema_name'])}.{quote_identifier(surface['object_name'])}"
            for permission in permissions:
                result = self.query(
                    "SELECT CAST(? AS nvarchar(256)) AS surface, CAST(? AS nvarchar(128)) AS permission_name, HAS_PERMS_BY_NAME(?, 'OBJECT', ?) AS has_permission",
                    [surface_id(surface), permission, entity_name, permission],
                )
                rows.append(
                    {
                        "scope": "object",
                        "surface": surface_id(surface),
                        "permission_name": permission,
                        "has_permission": result[0].get("has_permission") if result else None,
                    }
                )
        return rows

    def query(self, sql, params=None):
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install pyodbc on the AutoCount VM before validating the read-only SQL login") from exc

        with pyodbc.connect(self.connection_string, autocommit=True) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params or [])
            columns = [column[0] for column in cursor.description or []]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


def evaluate_permission_advisory(permission_rows):
    flagged = []
    for row in permission_rows:
        permission_name = str(row.get("permission_name", "")).upper()
        if permission_name in WRITE_LIKE_PERMISSIONS and is_positive(row.get("has_permission")):
            flagged.append(
                {
                    "scope": row.get("scope", ""),
                    "surface": row.get("surface", ""),
                    "permission_name": permission_name,
                    "has_permission": True,
                }
            )
    return {
        "status": "failed" if flagged else "success",
        "write_like_permission_detected": bool(flagged),
        "flagged_permissions": flagged,
        "checked_permission_count": len(permission_rows),
        "advisory_only": True,
    }


def empty_permission_advisory():
    return {
        "status": "not_run",
        "write_like_permission_detected": False,
        "flagged_permissions": [],
        "checked_permission_count": 0,
        "advisory_only": True,
    }


def build_check_definitions():
    return [
        {
            "name": "context",
            "sql": "SELECT DB_NAME() AS current_database, SUSER_SNAME() AS current_login, USER_NAME() AS current_user_name",
        },
        {
            "name": "surface_presence",
            "sql": "SELECT s.name AS schema_name, o.name AS object_name, o.type_desc AS object_type FROM sys.objects AS o INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id WHERE s.name = ? AND o.name = ? AND o.type IN ('U', 'V')",
        },
        {
            "name": "surface_metadata_select",
            "sql": "SELECT TOP (0) * FROM [schema].[object]",
        },
        {
            "name": "database_permission_advisory",
            "sql": "SELECT CAST(? AS nvarchar(128)) AS permission_name, HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', ?) AS has_permission",
        },
        {
            "name": "object_permission_advisory",
            "sql": "SELECT CAST(? AS nvarchar(256)) AS surface, CAST(? AS nvarchar(128)) AS permission_name, HAS_PERMS_BY_NAME(?, 'OBJECT', ?) AS has_permission",
        },
    ]


def normalize_surfaces(surfaces):
    normalized = []
    for surface in surfaces:
        if isinstance(surface, str):
            parts = surface.split(".", 1)
            if len(parts) != 2:
                raise ValueError(f"Smoke surface must use schema.object format: {surface}")
            normalized.append({"schema_name": parts[0], "object_name": parts[1]})
        else:
            normalized.append({"schema_name": surface["schema_name"], "object_name": surface["object_name"]})
    return normalized


def surface_id(surface):
    return f"{surface['schema_name']}.{surface['object_name']}"


def quote_identifier(value):
    return "[" + str(value).replace("]", "]]") + "]"


def sanitize_context(context):
    return {
        "current_database": sanitize_text(context.get("current_database", "")),
        "current_login": sanitize_text(context.get("current_login", "")),
        "current_user_name": sanitize_text(context.get("current_user_name", "")),
    }


def sanitize_text(text):
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", str(text))


def is_positive(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _coerce_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Validate a dedicated AutoCount read-only SQL login using metadata-only checks.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to secret-free validation config JSON.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    manifest = run_validation(load_config(args.config))
    print(json.dumps({"status": manifest["status"], "run_path": manifest["storage"]["run_path"]}, indent=2))
    return 0 if manifest["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
