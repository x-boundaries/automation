import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_readonly_login_validate as validator


class FakeValidationSource:
    def __init__(self, permission_rows=None):
        self.permission_rows = permission_rows or []
        self.surface_checks = []

    def fetch_context(self):
        return {
            "current_database": "AED_XBOUNDARIES",
            "current_login": "svc_ac2_readonly",
            "current_user_name": "svc_ac2_readonly",
        }

    def check_surface(self, surface):
        self.surface_checks.append(surface)
        return {
            "surface": f"{surface['schema_name']}.{surface['object_name']}",
            "exists": True,
            "metadata_select_top_0": True,
            "object_type": "USER_TABLE",
        }

    def fetch_permission_advisory(self, surfaces, permissions):
        return list(self.permission_rows)


class RecordingSqlValidationSource(validator.SqlServerReadonlyValidationSource):
    def __init__(self):
        super().__init__("Driver={ODBC Driver};Server=test;")
        self.queries = []

    def query(self, sql, params=None):
        self.queries.append({"sql": sql, "params": list(params or [])})
        if "IS_ROLEMEMBER" in sql:
            return [{"member_name": "svc_ac2_readonly", "source": "IS_ROLEMEMBER", "is_member": 0}]
        if "IS_SRVROLEMEMBER" in sql:
            return [{"member_name": "svc_ac2_readonly", "source": "IS_SRVROLEMEMBER", "is_member": 0}]
        return [{"has_permission": 0}]


class AutoCountReadonlyLoginValidateTests(unittest.TestCase):
    def test_load_config_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "autocount_readonly_login_validate.local.json"
            path.write_text("\ufeff" + json.dumps(base_config(tmpdir)), encoding="utf-8")

            loaded = validator.load_config(path)

            self.assertEqual(loaded["job"], "autocount_readonly_login_validate")
            self.assertEqual(loaded["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")

    def test_example_config_contract_is_secret_free(self):
        config_path = ROOT / "config" / "autocount_readonly_login_validate.example.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\probe\readonly_login")
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")
        self.assertEqual(
            [f"{surface['schema_name']}.{surface['object_name']}" for surface in config["smoke_surfaces"]],
            ["dbo.Item", "dbo.ItemUOM", "dbo.vItemBalQty", "dbo.StockDTL"],
        )

    def test_manifest_contains_safe_summaries_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validator.run_validation(
                base_config(tmpdir),
                source=FakeValidationSource(),
                now=datetime.fromisoformat("2026-06-12T09:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads((run_path / "readonly_login_validation_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(saved_manifest["status"], "success")
            self.assertTrue(saved_manifest["read_capability"]["all_configured_surfaces_readable"])
            self.assertFalse(saved_manifest["write_permission_advisory"]["write_like_permission_detected"])
            self.assertEqual(saved_manifest["exception_count"], 0)
            manifest_text = json.dumps(saved_manifest)
            self.assertNotIn('"rows"', manifest_text.lower())
            self.assertNotIn('"data"', manifest_text.lower())
            self.assertNotIn("SKU-001", manifest_text)
            self.assertNotIn("Driver=", manifest_text)
            self.assertNotIn("Password=", manifest_text)

    def test_secret_redaction(self):
        text = "Password=secret; PWD=other; token=abc; api_key=xyz; Server=prod;"

        redacted = validator.sanitize_text(text)

        self.assertNotIn("secret", redacted)
        self.assertNotIn("other", redacted)
        self.assertNotIn("abc", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertIn("<redacted>", redacted)

    def test_permission_evaluator_flags_write_like_permission(self):
        result = validator.evaluate_permission_advisory(
            [
                {"scope": "database", "permission_name": "INSERT", "has_permission": True},
                {"scope": "object", "surface": "dbo.Item", "permission_name": "SELECT", "has_permission": True},
            ]
        )

        self.assertTrue(result["write_like_permission_detected"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["flagged_permissions"][0]["permission_name"], "INSERT")

    def test_permission_evaluator_flags_dangerous_database_role_membership(self):
        result = validator.evaluate_permission_advisory(
            [
                {"scope": "database_role", "role_name": "db_securityadmin", "is_member": True},
                {"scope": "database_role", "role_name": "db_datareader", "is_member": True},
            ]
        )

        self.assertTrue(result["write_like_permission_detected"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["flagged_permissions"][0]["scope"], "database_role")
        self.assertEqual(result["flagged_permissions"][0]["role_name"], "db_securityadmin")

    def test_permission_evaluator_flags_dangerous_server_role_membership(self):
        result = validator.evaluate_permission_advisory(
            [
                {"scope": "server_role", "role_name": "sysadmin", "is_member": 1},
                {"scope": "server_role", "role_name": "public", "is_member": 1},
            ]
        )

        self.assertTrue(result["write_like_permission_detected"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["flagged_permissions"][0]["scope"], "server_role")
        self.assertEqual(result["flagged_permissions"][0]["role_name"], "sysadmin")

    def test_validation_fails_when_write_like_permission_is_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validator.run_validation(
                base_config(tmpdir),
                source=FakeValidationSource(
                    permission_rows=[
                        {
                            "scope": "object",
                            "surface": "dbo.StockDTL",
                            "permission_name": "UPDATE",
                            "has_permission": True,
                        }
                    ]
                ),
            )

            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(manifest["write_permission_advisory"]["write_like_permission_detected"])
            self.assertEqual(manifest["exception_count"], 0)

    def test_validation_fails_when_dangerous_role_membership_is_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validator.run_validation(
                base_config(tmpdir),
                source=FakeValidationSource(
                    permission_rows=[
                        {
                            "scope": "database_role",
                            "role_name": "db_owner",
                            "is_member": True,
                        }
                    ]
                ),
            )

            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(manifest["write_permission_advisory"]["write_like_permission_detected"])
            self.assertEqual(manifest["write_permission_advisory"]["flagged_permissions"][0]["role_name"], "db_owner")
            self.assertEqual(manifest["exception_count"], 0)

    def test_sql_source_fetches_fixed_role_membership_advisories(self):
        source = RecordingSqlValidationSource()

        rows = source.fetch_permission_advisory(
            [{"schema_name": "dbo", "object_name": "Item"}],
            ["INSERT"],
        )

        database_role_names = {row["role_name"] for row in rows if row["scope"] == "database_role"}
        server_role_names = {row["role_name"] for row in rows if row["scope"] == "server_role"}
        self.assertIn("db_owner", database_role_names)
        self.assertIn("db_securityadmin", database_role_names)
        self.assertIn("sysadmin", server_role_names)
        self.assertIn("securityadmin", server_role_names)

    def test_check_sql_definitions_do_not_contain_dml_or_ddl(self):
        sql_text = "\n".join(query["sql"] for query in validator.build_check_definitions())

        self.assertNotRegex(
            sql_text,
            r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b",
        )

    def test_check_sql_definitions_include_role_membership_advisories(self):
        names = {query["name"] for query in validator.build_check_definitions()}

        self.assertIn("database_role_advisory", names)
        self.assertIn("server_role_advisory", names)


    def test_database_scope_covers_create_table_and_execute(self):
        for permission in (
            "INSERT",
            "UPDATE",
            "DELETE",
            "ALTER",
            "CONTROL",
            "TAKE OWNERSHIP",
            "CREATE TABLE",
            "EXECUTE",
        ):
            self.assertIn(permission, validator.DATABASE_WRITE_LIKE_PERMISSIONS, permission)
        # The evaluator flags on the flat set, so both additions must be recognised there too.
        self.assertIn("CREATE TABLE", validator.WRITE_LIKE_PERMISSIONS)
        self.assertIn("EXECUTE", validator.WRITE_LIKE_PERMISSIONS)

    def test_object_scope_preserves_write_control_checks_without_invalid_create_table(self):
        # CREATE TABLE is not an object-level securable permission. Probing it there returns
        # NULL, which would read as a clean result rather than an unsupported combination.
        self.assertNotIn("CREATE TABLE", validator.OBJECT_WRITE_LIKE_PERMISSIONS)
        for permission in ("INSERT", "UPDATE", "DELETE", "ALTER", "CONTROL", "TAKE OWNERSHIP"):
            self.assertIn(permission, validator.OBJECT_WRITE_LIKE_PERMISSIONS, permission)

    def test_schema_scope_checks_broad_execute(self):
        self.assertEqual(validator.SCHEMA_WRITE_LIKE_PERMISSIONS, ["EXECUTE"])

    def test_permission_evaluator_flags_database_create_table(self):
        result = validator.evaluate_permission_advisory(
            [{"scope": "database", "permission_name": "CREATE TABLE", "has_permission": True}]
        )

        self.assertTrue(result["write_like_permission_detected"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["flagged_permissions"][0]["permission_name"], "CREATE TABLE")

    def test_permission_evaluator_flags_database_execute(self):
        result = validator.evaluate_permission_advisory(
            [{"scope": "database", "permission_name": "EXECUTE", "has_permission": True}]
        )

        self.assertTrue(result["write_like_permission_detected"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["flagged_permissions"][0]["permission_name"], "EXECUTE")

    def test_permission_evaluator_flags_schema_execute(self):
        result = validator.evaluate_permission_advisory(
            [{"scope": "schema", "surface": "dbo", "permission_name": "EXECUTE", "has_permission": True}]
        )

        self.assertTrue(result["write_like_permission_detected"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["flagged_permissions"][0]["scope"], "schema")
        self.assertEqual(result["flagged_permissions"][0]["surface"], "dbo")
        self.assertEqual(result["flagged_permissions"][0]["permission_name"], "EXECUTE")

    def test_permission_evaluator_ignores_unsupported_null_results(self):
        # HAS_PERMS_BY_NAME returns NULL for a permission that does not apply to the securable.
        result = validator.evaluate_permission_advisory(
            [
                {"scope": "object", "surface": "dbo.Item", "permission_name": "EXECUTE", "has_permission": None},
                {"scope": "schema", "surface": "dbo", "permission_name": "EXECUTE", "has_permission": None},
            ]
        )

        self.assertFalse(result["write_like_permission_detected"])
        self.assertEqual(result["flagged_permissions"], [])

    def test_validation_fails_when_database_create_table_is_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validator.run_validation(
                base_config(tmpdir),
                source=FakeValidationSource(
                    permission_rows=[
                        {"scope": "database", "permission_name": "CREATE TABLE", "has_permission": True}
                    ]
                ),
            )

            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(manifest["write_permission_advisory"]["write_like_permission_detected"])
            self.assertEqual(manifest["exception_count"], 0)

    def test_validation_fails_when_schema_execute_is_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = validator.run_validation(
                base_config(tmpdir),
                source=FakeValidationSource(
                    permission_rows=[
                        {"scope": "schema", "surface": "dbo", "permission_name": "EXECUTE", "has_permission": True}
                    ]
                ),
            )

            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(manifest["write_permission_advisory"]["write_like_permission_detected"])
            self.assertEqual(manifest["write_permission_advisory"]["flagged_permissions"][0]["scope"], "schema")
            self.assertEqual(manifest["exception_count"], 0)

    def test_distinct_schema_names_deduplicates_configured_schema_set(self):
        surfaces = [
            {"schema_name": "dbo", "object_name": "Item"},
            {"schema_name": "dbo", "object_name": "ItemUOM"},
            {"schema_name": "rpt", "object_name": "Balance"},
        ]

        self.assertEqual(validator.distinct_schema_names(surfaces), ["dbo", "rpt"])

    def test_sql_source_probes_each_scope_with_valid_permission_combinations(self):
        source = RecordingSqlValidationSource()
        surfaces = [
            {"schema_name": "dbo", "object_name": "Item"},
            {"schema_name": "dbo", "object_name": "StockDTL"},
            {"schema_name": "rpt", "object_name": "Balance"},
        ]

        rows = source.fetch_permission_advisory(surfaces, validator.DATABASE_WRITE_LIKE_PERMISSIONS)

        database_rows = [row for row in rows if row["scope"] == "database"]
        self.assertEqual(
            [row["permission_name"] for row in database_rows],
            validator.DATABASE_WRITE_LIKE_PERMISSIONS,
        )

        object_rows = [row for row in rows if row["scope"] == "object"]
        self.assertEqual(len(object_rows), len(surfaces) * len(validator.OBJECT_WRITE_LIKE_PERMISSIONS))
        self.assertNotIn("CREATE TABLE", {row["permission_name"] for row in object_rows})

        schema_rows = [row for row in rows if row["scope"] == "schema"]
        # One probe per DISTINCT configured schema, not one per surface.
        self.assertEqual([row["surface"] for row in schema_rows], ["dbo", "rpt"])
        self.assertEqual({row["permission_name"] for row in schema_rows}, {"EXECUTE"})

        schema_queries = [query for query in source.queries if "'SCHEMA'" in query["sql"]]
        self.assertEqual(len(schema_queries), 2)

    def test_sql_source_preserves_every_dangerous_fixed_role_check(self):
        source = RecordingSqlValidationSource()

        rows = source.fetch_permission_advisory(
            [{"schema_name": "dbo", "object_name": "Item"}],
            validator.DATABASE_WRITE_LIKE_PERMISSIONS,
        )

        self.assertEqual(
            {row["role_name"] for row in rows if row["scope"] == "database_role"},
            set(validator.DANGEROUS_DATABASE_ROLES),
        )
        self.assertEqual(
            {row["role_name"] for row in rows if row["scope"] == "server_role"},
            set(validator.DANGEROUS_SERVER_ROLES),
        )

    def test_check_sql_definitions_include_every_permission_scope(self):
        names = {query["name"] for query in validator.build_check_definitions()}

        self.assertIn("database_permission_advisory", names)
        self.assertIn("object_permission_advisory", names)
        self.assertIn("schema_permission_advisory", names)


def base_config(output_root):
    return {
        "job": "autocount_readonly_login_validate",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "target_label": "AC2 dedicated read-only login validation",
        "smoke_surfaces": [
            {"schema_name": "dbo", "object_name": "Item"},
            {"schema_name": "dbo", "object_name": "ItemUOM"},
            {"schema_name": "dbo", "object_name": "vItemBalQty"},
            {"schema_name": "dbo", "object_name": "StockDTL"},
        ],
    }


if __name__ == "__main__":
    unittest.main()
