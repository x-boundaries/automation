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

    def test_check_sql_definitions_do_not_contain_dml_or_ddl(self):
        sql_text = "\n".join(query["sql"] for query in validator.build_check_definitions())

        self.assertNotRegex(
            sql_text,
            r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b",
        )


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
