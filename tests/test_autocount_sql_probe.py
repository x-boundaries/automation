import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_sql_probe as probe


class FakeProbeSource:
    def __init__(self):
        self.sample_calls = []
        self.row_count_calls = []

    def fetch_server_info(self, database_name=None):
        return {
            "sql_server_version": "Microsoft SQL Server 2019",
            "edition": "Developer Edition",
            "current_database": database_name or "AutoCountSandbox",
            "current_login": "XB\\svc_autocount_probe",
            "current_user": "svc_autocount_probe",
        }

    def fetch_schemas(self, schemas=None):
        return [{"schema_name": "dbo"}, {"schema_name": "report"}]

    def fetch_objects(self, schemas=None):
        return [
            {
                "schema_name": "dbo",
                "object_name": "Item",
                "object_type": "USER_TABLE",
                "create_date": "2026-01-01T00:00:00",
                "modify_date": "2026-02-01T00:00:00",
            },
            {
                "schema_name": "dbo",
                "object_name": "StockBalanceView",
                "object_type": "VIEW",
                "create_date": "2026-01-02T00:00:00",
                "modify_date": "2026-02-02T00:00:00",
            },
            {
                "schema_name": "dbo",
                "object_name": "GLJournal",
                "object_type": "USER_TABLE",
                "create_date": "2026-01-03T00:00:00",
                "modify_date": "2026-02-03T00:00:00",
            },
        ]

    def fetch_columns(self, schemas=None):
        return [
            {
                "object_schema": "dbo",
                "object_name": "Item",
                "column_name": "ItemCode",
                "data_type": "nvarchar",
                "max_length": 40,
                "is_nullable": False,
            },
            {
                "object_schema": "dbo",
                "object_name": "StockBalanceView",
                "column_name": "BalanceQty",
                "data_type": "decimal",
                "max_length": 9,
                "is_nullable": True,
            },
            {
                "object_schema": "dbo",
                "object_name": "GLJournal",
                "column_name": "JournalNo",
                "data_type": "nvarchar",
                "max_length": 40,
                "is_nullable": False,
            },
        ]

    def fetch_indexes(self, schemas=None):
        return [
            {
                "object_schema": "dbo",
                "object_name": "Item",
                "index_name": "PK_Item",
                "is_primary_key": True,
                "is_unique": True,
                "column_name": "ItemCode",
                "key_ordinal": 1,
            }
        ]

    def fetch_permissions(self):
        return [
            {"permission_name": "SELECT", "class_desc": "OBJECT_OR_COLUMN", "object_schema": "dbo"},
            {"permission_name": "UPDATE", "class_desc": "OBJECT_OR_COLUMN", "object_schema": "dbo"},
            {"permission_name": "EXECUTE", "class_desc": "SCHEMA", "schema_name": "dbo"},
        ]

    def fetch_role_memberships(self):
        return [
            {
                "role_name": "db_owner",
                "member_name": "svc_autocount_probe",
                "source": "database_role_members",
                "is_member": True,
            },
            {
                "role_name": "db_datawriter",
                "member_name": "svc_autocount_probe",
                "source": "is_rolemember",
                "is_member": True,
            },
            {
                "role_name": "db_ddladmin",
                "member_name": "svc_autocount_probe",
                "source": "is_rolemember",
                "is_member": True,
            },
            {
                "role_name": "db_datareader",
                "member_name": "svc_autocount_probe",
                "source": "database_role_members",
                "is_member": True,
            },
        ]

    def fetch_row_counts(self, objects):
        self.row_count_calls.append(objects)
        return [{"object_schema": "dbo", "object_name": "Item", "row_count": 10}]

    def fetch_sample_rows(self, object_ref, limit):
        self.sample_calls.append((object_ref, limit))
        return [{"ItemCode": "SKU-001", "Description": "Sample"}]


class AutoCountSqlProbeTests(unittest.TestCase):
    def test_candidate_keyword_grouping_is_heuristic(self):
        source = FakeProbeSource()

        candidates = probe.match_candidate_objects(source.fetch_objects(), source.fetch_columns())

        self.assertIn("dbo.Item", object_ids(candidates["item_product_stock_master"]))
        self.assertIn("dbo.StockBalanceView", object_ids(candidates["stock_balance_status"]))
        self.assertIn("dbo.GLJournal", object_ids(candidates["gl_account_journal"]))
        self.assertTrue(candidates["item_product_stock_master"][0]["heuristic"])
        self.assertIn("ItemCode", candidates["item_product_stock_master"][0]["matched_columns"])

    def test_run_probe_writes_manifest_and_metadata_without_samples_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = FakeProbeSource()

            manifest = probe.run_probe(
                base_config(tmpdir),
                source=source,
                now=datetime.fromisoformat("2026-06-06T09:30:00+08:00"),
            )

            probe_path = Path(manifest["storage"]["probe_path"])
            saved_manifest = json.loads((probe_path / "probe_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(saved_manifest["status"], "success")
            self.assertRegex(saved_manifest["run_id"], r"^[0-9a-f-]{36}$")
            self.assertEqual(saved_manifest["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
            self.assertNotIn('"rows":', json.dumps(saved_manifest).lower())
            self.assertNotIn("SKU-001", json.dumps(saved_manifest))
            self.assertEqual(saved_manifest["sample_limit"], 0)
            self.assertFalse(saved_manifest["samples_enabled"])
            self.assertEqual(source.sample_calls, [])
            self.assertFalse((probe_path / "samples").exists())
            self.assertTrue((probe_path / "objects.csv").exists())
            self.assertTrue((probe_path / "columns.csv").exists())
            self.assertTrue((probe_path / "candidates.csv").exists())
            self.assertTrue((probe_path / "role_memberships.csv").exists())
            self.assertTrue((probe_path / "role_risks.csv").exists())
            self.assertTrue((probe_path / "probe_report.md").exists())
            self.assertEqual(saved_manifest["counts"]["role_memberships"], 4)
            self.assertEqual(saved_manifest["counts"]["role_risks"], 3)
            self.assertIn("role_risks", saved_manifest)
            report = (probe_path / "probe_report.md").read_text(encoding="utf-8")
            self.assertIn("## Role Membership Risk Flags", report)
            self.assertIn("db_owner", report)

    def test_output_root_must_stay_outside_repo(self):
        with self.assertRaises(ValueError):
            probe.resolve_output_root(ROOT / "outputs" / "autocount_sql_probe", repo_root=ROOT)

    def test_sample_limit_zero_skips_sample_output_even_when_sample_objects_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = FakeProbeSource()
            config = base_config(tmpdir)
            config["sample_objects"] = [{"schema_name": "dbo", "object_name": "Item"}]

            manifest = probe.run_probe(config, source=source)

            self.assertEqual(manifest["sample_limit"], 0)
            self.assertEqual(source.sample_calls, [])
            self.assertEqual(manifest["storage"]["sample_files"], [])

    def test_detects_risky_permissions_best_effort(self):
        risks = probe.detect_risky_permissions(FakeProbeSource().fetch_permissions())

        risk_names = {risk["permission_name"] for risk in risks}
        self.assertIn("UPDATE", risk_names)
        self.assertIn("EXECUTE", risk_names)
        self.assertTrue(any("broad schema" in risk["reason"] for risk in risks))

    def test_detects_risky_database_roles_without_flagging_datareader_only(self):
        risks = probe.detect_risky_roles(FakeProbeSource().fetch_role_memberships())

        role_names = {risk["role_name"] for risk in risks}
        self.assertIn("db_owner", role_names)
        self.assertIn("db_datawriter", role_names)
        self.assertIn("db_ddladmin", role_names)
        self.assertNotIn("db_datareader", role_names)
        self.assertTrue(all(risk["best_effort"] for risk in risks))

    def test_redacts_secrets_from_error_text(self):
        text = "Login failed; Password=super-secret; PWD=another; token=abc123; Api_Key=xyz"

        redacted = probe.sanitize_text(text)

        self.assertNotIn("super-secret", redacted)
        self.assertNotIn("another", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertIn("<redacted>", redacted)


def base_config(output_root):
    return {
        "job": "autocount_sql_probe",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "database_name": "AutoCountSandbox",
        "schemas": ["dbo"],
        "sample_limit": 0,
        "include_row_counts": False,
        "sample_objects": [],
    }


def object_ids(records):
    return {record["object_id"] for record in records}


if __name__ == "__main__":
    unittest.main()
