import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import autocount_broader_surface_discovery as discovery


REQUIRED_GROUPS = {
    "debtor_customer",
    "creditor_supplier",
    "chart_of_accounts_gl",
    "ar_ap_opening",
    "locations",
    "payment_methods",
    "purchase_order_outstanding_po",
    "stock_in_transit_candidates",
    "stock_reference_followup",
}


class FakeDiscoverySource:
    def __init__(self):
        self.row_count_calls = []

    def fetch_context(self):
        return {
            "current_database": "AED_XBOUNDARIES",
            "current_login": "xb_ac2_readonly",
            "current_user_name": "xb_ac2_readonly",
        }

    def fetch_objects(self, schemas=None):
        return [
            object_record("dbo", "Debtor", "USER_TABLE"),
            object_record("dbo", "vDebtor", "VIEW"),
            object_record("dbo", "DebtorType", "USER_TABLE"),
            object_record("dbo", "ARInvoice", "USER_TABLE"),
            object_record("dbo", "ARInvoiceDTL", "USER_TABLE"),
            object_record("dbo", "CustomerPayment", "USER_TABLE"),
            object_record("dbo", "Creditor", "USER_TABLE"),
            object_record("dbo", "vCreditor", "VIEW"),
            object_record("dbo", "CreditorType", "USER_TABLE"),
            object_record("dbo", "APInvoice", "USER_TABLE"),
            object_record("dbo", "APInvoiceDTL", "USER_TABLE"),
            object_record("dbo", "GLAccount", "USER_TABLE"),
            object_record("dbo", "GLDTL", "USER_TABLE"),
            object_record("dbo", "ARAPOpening", "USER_TABLE"),
            object_record("dbo", "vCashBookImportedGoodsDTL", "VIEW"),
            object_record("dbo", "Branch", "USER_TABLE"),
            object_record("dbo", "vBranch", "VIEW"),
            object_record("dbo", "InvoiceBranchAudit", "USER_TABLE"),
            object_record("dbo", "PaymentMethod", "USER_TABLE"),
            object_record("dbo", "PO", "USER_TABLE"),
            object_record("dbo", "PODTL", "USER_TABLE"),
            object_record("dbo", "vPurchaseOrder", "VIEW"),
            object_record("dbo", "Pos", "USER_TABLE"),
            object_record("dbo", "PosOrder", "USER_TABLE"),
            object_record("dbo", "POColumnLock", "USER_TABLE"),
            object_record("dbo", "POBonusPoint", "USER_TABLE"),
            object_record("dbo", "SupportTicket", "USER_TABLE"),
            object_record("dbo", "StockTransfer", "VIEW"),
            object_record("dbo", "Item", "USER_TABLE"),
        ]

    def fetch_columns(self, schemas=None):
        return [
            column_record("dbo", "Debtor", "DebtorCode", "nvarchar"),
            column_record("dbo", "Debtor", "CompanyName", "nvarchar"),
            column_record("dbo", "vDebtor", "DebtorCode", "nvarchar"),
            column_record("dbo", "DebtorType", "DebtorType", "nvarchar"),
            column_record("dbo", "ARInvoice", "DebtorCode", "nvarchar"),
            column_record("dbo", "ARInvoiceDTL", "DebtorCode", "nvarchar"),
            column_record("dbo", "CustomerPayment", "CustomerCode", "nvarchar"),
            column_record("dbo", "Creditor", "CreditorCode", "nvarchar"),
            column_record("dbo", "vCreditor", "CreditorCode", "nvarchar"),
            column_record("dbo", "CreditorType", "CreditorType", "nvarchar"),
            column_record("dbo", "APInvoice", "CreditorCode", "nvarchar"),
            column_record("dbo", "APInvoiceDTL", "CreditorCode", "nvarchar"),
            column_record("dbo", "GLAccount", "AccNo", "nvarchar"),
            column_record("dbo", "GLDTL", "AccNo", "nvarchar"),
            column_record("dbo", "GLDTL", "JournalNo", "nvarchar"),
            column_record("dbo", "ARAPOpening", "OpeningBalance", "decimal"),
            column_record("dbo", "vCashBookImportedGoodsDTL", "APInvoiceNo", "nvarchar"),
            column_record("dbo", "Branch", "BranchCode", "nvarchar"),
            column_record("dbo", "vBranch", "BranchCode", "nvarchar"),
            column_record("dbo", "InvoiceBranchAudit", "BranchCode", "nvarchar"),
            column_record("dbo", "PaymentMethod", "PaymentMethod", "nvarchar"),
            column_record("dbo", "PO", "DocNo", "nvarchar"),
            column_record("dbo", "PODTL", "PONo", "nvarchar"),
            column_record("dbo", "PODTL", "OutstandingQty", "decimal"),
            column_record("dbo", "vPurchaseOrder", "PONo", "nvarchar"),
            column_record("dbo", "Pos", "PosNo", "nvarchar"),
            column_record("dbo", "PosOrder", "PosOrderNo", "nvarchar"),
            column_record("dbo", "POColumnLock", "PONo", "nvarchar"),
            column_record("dbo", "POBonusPoint", "PONo", "nvarchar"),
            column_record("dbo", "StockTransfer", "FromLocation", "nvarchar"),
            column_record("dbo", "StockTransfer", "ToLocation", "nvarchar"),
            column_record("dbo", "Item", "ItemCode", "nvarchar"),
        ]

    def fetch_row_counts(self, objects):
        self.row_count_calls.append(list(objects))
        return [
            {"object_schema": "dbo", "object_name": "Debtor", "approximate_row_count": 12},
            {"object_schema": "dbo", "object_name": "Creditor", "approximate_row_count": 7},
        ]


class EmptyDiscoverySource:
    def fetch_context(self):
        return {"current_database": "AED_XBOUNDARIES"}

    def fetch_objects(self, schemas=None):
        return []

    def fetch_columns(self, schemas=None):
        return []

    def fetch_row_counts(self, objects):
        return []


class AutoCountBroaderSurfaceDiscoveryTests(unittest.TestCase):
    def test_load_config_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "autocount_broader_surface_discovery.local.json"
            path.write_text("\ufeff" + json.dumps(base_config(tmpdir)), encoding="utf-8")

            loaded = discovery.load_config(path)

            self.assertEqual(loaded["job"], "autocount_broader_surface_discovery")
            self.assertEqual(loaded["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")

    def test_example_config_contract_is_secret_free_and_complete(self):
        config_path = ROOT / "config" / "autocount_broader_surface_discovery.example.json"

        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        self.assertEqual(config["output_root"], r"C:\XB\autocount_outputs\probe\broader_surfaces")
        self.assertEqual(set(config["discovery_groups"]), REQUIRED_GROUPS)
        self.assertEqual(config["candidate_scoring"]["enabled"], True)
        self.assertEqual(config["candidate_scoring"]["top_n_per_group"], 15)
        self.assertTrue(config["candidate_scoring"]["exact_name_boosts"]["debtor_customer"])
        self.assertTrue(config["candidate_scoring"]["weak_match_penalties"])
        self.assertTrue(config["candidate_scoring"]["false_positive_patterns"])
        self.assertTrue(config["candidate_scoring"]["known_header_detail_pairs"])
        self.assertTrue(config["candidate_scoring"]["intent_shortlists"])
        self.assertNotIn("connection_string", config)
        self.assertNotRegex(json.dumps(config), r"(?i)(Driver=|Server=|Password=|PWD=|Trusted_Connection=)")
        for group_config in config["discovery_groups"].values():
            self.assertEqual(set(group_config), {"keyword_hints"})
            self.assertTrue(group_config["keyword_hints"])
        self.assertIn("stock_smoke_reference_surfaces", config)
        self.assertTrue(all(surface["status"] == "reference_only_not_final_mapping" for surface in config["stock_smoke_reference_surfaces"]))

    def test_create_source_resolves_connection_string_only_from_configured_env_var(self):
        original = os.environ.get("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
        os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = "Driver={local};Password=secret;"
        try:
            source = discovery.create_source(base_config(tempfile.gettempdir()))
        finally:
            if original is None:
                os.environ.pop("AUTOCOUNT_READONLY_SQL_CONNECTION_STRING", None)
            else:
                os.environ["AUTOCOUNT_READONLY_SQL_CONNECTION_STRING"] = original

        self.assertEqual(source.connection_string, "Driver={local};Password=secret;")

    def test_check_sql_definitions_do_not_contain_writes_or_select_star(self):
        sql_text = "\n".join(query["sql"] for query in discovery.build_check_definitions())

        self.assertNotRegex(sql_text, r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b")
        self.assertNotRegex(sql_text, r"(?i)\bSELECT\s+\*")

    def test_run_discovery_writes_safe_manifest_and_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=FakeDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )
            run_path = Path(manifest["storage"]["run_path"])
            saved_manifest = json.loads((run_path / "broader_surface_discovery_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(saved_manifest["status"], "success")
            self.assertEqual(saved_manifest["job"], "autocount_broader_surface_discovery")
            self.assertEqual(saved_manifest["connection_string_env"], "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING")
            self.assertEqual(saved_manifest["candidate_groups"]["debtor_customer"]["decision"], "Needs reconciliation")
            self.assertIn("dbo.Debtor", object_ids(saved_manifest["matched_objects_by_group"]["debtor_customer"]))
            self.assertIn("dbo.PaymentMethod", object_ids(saved_manifest["matched_objects_by_group"]["payment_methods"]))
            self.assertIn("dbo.StockTransfer", object_ids(saved_manifest["matched_objects_by_group"]["stock_in_transit_candidates"]))
            self.assertTrue(saved_manifest["matched_columns_by_group"]["purchase_order_outstanding_po"])
            self.assertGreaterEqual(saved_manifest["object_counts_by_group"]["stock_reference_followup"], 1)
            self.assertIn("top_candidates", saved_manifest["candidate_groups"]["debtor_customer"])
            self.assertTrue(saved_manifest["candidate_groups"]["debtor_customer"]["top_candidates"])
            self.assert_sequential_ranks(saved_manifest["candidate_groups"]["debtor_customer"]["top_candidates"])
            self.assertIn("master_candidates", saved_manifest["candidate_groups"]["debtor_customer"])
            self.assertTrue(all(
                candidate["decision"] == "Needs reconciliation"
                for group in saved_manifest["candidate_groups"].values()
                for candidate in all_group_candidates(group)
            ))
            self.assertTrue(all(
                candidate["final_production_selected"] is False
                for group in saved_manifest["candidate_groups"].values()
                for candidate in all_group_candidates(group)
            ))
            self.assert_no_raw_payload_keys(saved_manifest)
            self.assertTrue((run_path / "broader_surface_discovery_report.md").exists())
            self.assertIn("AutoCount UI/report reconciliation", "\n".join(saved_manifest["notes"]))

    def test_scoring_ranks_exact_master_candidates_above_transaction_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=FakeDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )

            self.assert_top_candidate(manifest, "debtor_customer", "dbo.Debtor")
            self.assert_ranked_above(manifest, "debtor_customer", "dbo.Debtor", "dbo.ARInvoice")
            self.assert_top_candidate(manifest, "creditor_supplier", "dbo.Creditor")
            self.assert_ranked_above(manifest, "creditor_supplier", "dbo.Creditor", "dbo.APInvoice")
            self.assert_top_candidate(manifest, "locations", "dbo.Branch")
            self.assert_ranked_above(manifest, "locations", "dbo.Branch", "dbo.InvoiceBranchAudit")

    def test_intent_shortlists_surface_master_candidates_before_transactions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=FakeDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )

            debtor_master = manifest["candidate_groups"]["debtor_customer"]["master_candidates"]
            creditor_master = manifest["candidate_groups"]["creditor_supplier"]["master_candidates"]
            location_master = manifest["candidate_groups"]["locations"]["master_candidates"]
            payment_master = manifest["candidate_groups"]["payment_methods"]["master_candidates"]

            self.assertIn(debtor_master[0]["object_id"], {"dbo.Debtor", "dbo.vDebtor"})
            self.assertIn("dbo.DebtorType", object_ids(debtor_master))
            self.assertIn(creditor_master[0]["object_id"], {"dbo.Creditor", "dbo.vCreditor"})
            self.assertIn("dbo.CreditorType", object_ids(creditor_master))
            self.assertIn(location_master[0]["object_id"], {"dbo.Branch", "dbo.vBranch"})
            self.assertEqual(payment_master[0]["object_id"], "dbo.PaymentMethod")
            self.assertNotIn("dbo.ARInvoice", object_ids(debtor_master[:3]))
            self.assertNotIn("dbo.APInvoice", object_ids(creditor_master[:3]))

    def test_intent_shortlists_separate_opening_po_and_gl_review_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=FakeDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )

            ar_opening = manifest["candidate_groups"]["ar_ap_opening"]["ar_opening_candidates"]
            ap_opening = manifest["candidate_groups"]["ar_ap_opening"]["ap_opening_candidates"]
            po_header = manifest["candidate_groups"]["purchase_order_outstanding_po"]["po_header_candidates"]
            po_detail = manifest["candidate_groups"]["purchase_order_outstanding_po"]["po_detail_candidates"]
            account_master = manifest["candidate_groups"]["chart_of_accounts_gl"]["account_master_candidates"]
            gl_transactions = manifest["candidate_groups"]["chart_of_accounts_gl"]["gl_transaction_candidates"]

            self.assertIn("dbo.ARInvoice", object_ids(ar_opening))
            self.assertIn("dbo.ARInvoiceDTL", object_ids(ar_opening))
            self.assertIn("dbo.APInvoice", object_ids(ap_opening))
            self.assertIn("dbo.APInvoiceDTL", object_ids(ap_opening))
            self.assertEqual(po_header[0]["object_id"], "dbo.PO")
            self.assertIn("dbo.vPurchaseOrder", object_ids(po_header))
            self.assertIn("dbo.PODTL", object_ids(po_detail))
            self.assertEqual(account_master[0]["object_id"], "dbo.GLAccount")
            self.assertNotEqual(account_master[0]["object_id"], "dbo.GLDTL")
            self.assertIn("dbo.GLDTL", object_ids(gl_transactions))

    def test_scoring_detects_header_detail_pairs_and_penalizes_false_positives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=FakeDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )
            po_candidates = manifest["candidate_groups"]["purchase_order_outstanding_po"]["top_candidates"]
            po_detail = candidate_by_id(po_candidates, "dbo.PODTL")
            column_lock = candidate_by_id(po_candidates, "dbo.POColumnLock")
            bonus_point = candidate_by_id(po_candidates, "dbo.POBonusPoint")

            self.assertIsNotNone(po_detail)
            self.assertTrue(any("detail pair" in reason.lower() for reason in po_detail["score_reasons"]))
            self.assertLess(column_lock["score"], po_detail["score"])
            self.assertLess(bonus_point["score"], po_detail["score"])

    def test_scoring_limits_top_candidates_and_keeps_pos_from_dominating_po(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = base_config(tmpdir)
            config["candidate_scoring"]["top_n_per_group"] = 15
            manifest = discovery.run_discovery(
                config,
                source=FakeDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )
            po_candidates = manifest["candidate_groups"]["purchase_order_outstanding_po"]["top_candidates"]
            po = candidate_by_id(po_candidates, "dbo.PO")
            pos = candidate_by_id(po_candidates, "dbo.Pos")
            pos_order = candidate_by_id(po_candidates, "dbo.PosOrder")

            self.assertLessEqual(len(po_candidates), 15)
            self.assertIn("dbo.PO", [candidate["object_id"] for candidate in po_candidates])
            self.assertNotIn("dbo.SupportTicket", [candidate["object_id"] for candidate in po_candidates])
            self.assert_ranked_above(manifest, "purchase_order_outstanding_po", "dbo.PO", "dbo.Pos")
            self.assert_ranked_above(manifest, "purchase_order_outstanding_po", "dbo.PO", "dbo.PosOrder")
            self.assertTrue(any("pos" in reason.lower() for reason in pos["score_reasons"]))
            self.assertTrue(any("pos" in reason.lower() for reason in pos_order["score_reasons"]))
            self.assertTrue(all(candidate["decision"] == "Needs reconciliation" for candidate in po_candidates))
            self.assertTrue(all(candidate["final_production_selected"] is False for candidate in po_candidates))
            self.assertIsNotNone(po)

    def test_empty_no_match_metadata_is_safe_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = discovery.run_discovery(
                base_config(tmpdir),
                source=EmptyDiscoverySource(),
                now=datetime.fromisoformat("2026-06-15T09:30:00+08:00"),
            )

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["counts"]["objects"], 0)
            self.assertEqual(manifest["counts"]["columns"], 0)
            self.assertTrue(all(count == 0 for count in manifest["object_counts_by_group"].values()))
            self.assertTrue(all(group["decision"] == "Needs reconciliation" for group in manifest["candidate_groups"].values()))
            self.assert_no_raw_payload_keys(manifest)

    def test_secret_redaction_covers_password_pwd_token_and_api_key_fragments(self):
        text = "Login failed; Password=super-secret; PWD=other; token=abc123; api key: xyz; Api-Key=hidden"

        redacted = discovery.sanitize_text(text)

        self.assertNotIn("super-secret", redacted)
        self.assertNotIn("other", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("xyz", redacted)
        self.assertNotIn("hidden", redacted)
        self.assertIn("<redacted>", redacted)

    def assert_no_raw_payload_keys(self, value):
        if isinstance(value, dict):
            forbidden = {"rows", "data"}
            self.assertFalse(forbidden.intersection(value), f"raw payload-like key present in {value.keys()}")
            for nested in value.values():
                self.assert_no_raw_payload_keys(nested)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_raw_payload_keys(item)

    def assert_top_candidate(self, manifest, group_name, object_id):
        candidates = manifest["candidate_groups"][group_name]["top_candidates"]
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["object_id"], object_id)

    def assert_ranked_above(self, manifest, group_name, higher_object_id, lower_object_id):
        candidates = manifest["candidate_groups"][group_name]["top_candidates"]
        positions = {candidate["object_id"]: index for index, candidate in enumerate(candidates)}
        self.assertLess(positions[higher_object_id], positions[lower_object_id])

    def assert_sequential_ranks(self, candidates):
        self.assertEqual([candidate["rank"] for candidate in candidates], list(range(1, len(candidates) + 1)))


def base_config(output_root):
    return {
        "job": "autocount_broader_surface_discovery",
        "connection_string_env": "AUTOCOUNT_READONLY_SQL_CONNECTION_STRING",
        "output_root": str(output_root),
        "schemas": ["dbo"],
        "include_row_counts": False,
        "discovery_groups": {
            "debtor_customer": {"keyword_hints": ["debtor", "customer", "cust"]},
            "creditor_supplier": {"keyword_hints": ["creditor", "supplier", "vendor"]},
            "chart_of_accounts_gl": {"keyword_hints": ["account", "gl", "ledger", "coa", "accno"]},
            "ar_ap_opening": {"keyword_hints": ["ar", "ap", "opening", "receivable", "payable"]},
            "locations": {"keyword_hints": ["location", "warehouse", "store", "branch"]},
            "payment_methods": {"keyword_hints": ["payment", "paymethod", "cash", "bank", "cheque"]},
            "purchase_order_outstanding_po": {"keyword_hints": ["purchase", "po", "order", "outstanding"]},
            "stock_in_transit_candidates": {"keyword_hints": ["transit", "transfer", "fromlocation", "tolocation"]},
            "stock_reference_followup": {"keyword_hints": ["item", "stock", "uom", "balance", "movement"]},
        },
        "candidate_scoring": {
            "enabled": True,
            "top_n_per_group": 15,
            "exact_name_boosts": {
                "debtor_customer": ["Debtor", "vDebtor", "DebtorType"],
                "creditor_supplier": ["Creditor", "vCreditor", "CreditorType"],
                "chart_of_accounts_gl": ["GLAccount"],
                "locations": ["Branch", "vBranch"],
                "payment_methods": ["PaymentMethod"],
                "purchase_order_outstanding_po": ["PO", "vPurchaseOrder"],
                "ar_ap_opening": ["ARInvoice", "ARInvoiceDTL", "APInvoice", "APInvoiceDTL"],
            },
            "weak_match_penalties": {
                "object_name_weak_substring": -8
            },
            "false_positive_patterns": [
                {"pattern": "ColumnLock", "penalty": -25, "unless_group_contains": []},
                {"pattern": "BonusPoint", "penalty": -20, "unless_group_contains": ["loyalty", "member"]},
                {"pattern": "^Pos(Order)?$", "penalty": -45, "groups": ["purchase_order_outstanding_po"]},
                {"pattern": "CashBook.*ImportedGoods.*DTL", "penalty": -35, "groups": ["ar_ap_opening"]},
            ],
            "known_header_detail_pairs": [
                {"group": "purchase_order_outstanding_po", "header": "PO", "detail": "PODTL", "boost": 20},
                {"group": "ar_ap_opening", "header": "ARInvoice", "detail": "ARInvoiceDTL", "boost": 20},
                {"group": "ar_ap_opening", "header": "APInvoice", "detail": "APInvoiceDTL", "boost": 20},
            ],
            "intent_shortlists": {
                "debtor_customer": {
                    "master_candidates": {
                        "include_patterns": ["^v?Debtor$", "^DebtorType$"],
                        "boost_patterns": ["^v?Debtor$", "^DebtorType$"],
                        "penalty_patterns": ["Invoice", "Payment"]
                    },
                    "transaction_candidates": {
                        "include_patterns": ["Invoice", "Payment", "CreditNote", "DebitNote"]
                    }
                },
                "creditor_supplier": {
                    "master_candidates": {
                        "include_patterns": ["^v?Creditor$", "^CreditorType$"],
                        "boost_patterns": ["^v?Creditor$", "^CreditorType$"],
                        "penalty_patterns": ["Invoice", "Payment"]
                    },
                    "transaction_candidates": {
                        "include_patterns": ["Invoice", "Payment", "CreditNote", "DebitNote", "GoodsReceived"]
                    }
                },
                "chart_of_accounts_gl": {
                    "account_master_candidates": {
                        "include_patterns": ["Account", "COA", "Chart"],
                        "boost_patterns": ["GLAccount", "Account"],
                        "penalty_patterns": ["DTL", "Journal"]
                    },
                    "gl_transaction_candidates": {
                        "include_patterns": ["GLDTL", "Journal", "Ledger", "DTL"]
                    }
                },
                "ar_ap_opening": {
                    "ar_opening_candidates": {
                        "include_patterns": ["ARInvoice", "AR.*Opening"],
                        "boost_patterns": ["ARInvoice", "ARInvoiceDTL"],
                        "penalty_patterns": ["CashBook.*ImportedGoods"]
                    },
                    "ap_opening_candidates": {
                        "include_patterns": ["APInvoice", "AP.*Opening"],
                        "boost_patterns": ["APInvoice", "APInvoiceDTL"],
                        "penalty_patterns": ["CashBook.*ImportedGoods"]
                    }
                },
                "purchase_order_outstanding_po": {
                    "po_header_candidates": {
                        "include_patterns": ["^PO$", "PurchaseOrder"],
                        "boost_patterns": ["^PO$", "vPurchaseOrder"],
                        "penalty_patterns": ["^Pos(Order)?$"]
                    },
                    "po_detail_candidates": {
                        "include_patterns": ["PODTL", "PurchaseOrder.*DTL"],
                        "boost_patterns": ["PODTL"],
                        "penalty_patterns": ["^Pos(Order)?$"]
                    }
                },
                "locations": {
                    "master_candidates": {
                        "include_patterns": ["^v?Branch$", "Location", "Warehouse"],
                        "boost_patterns": ["^v?Branch$"],
                        "penalty_patterns": ["Invoice", "PO", "SO"]
                    },
                    "transaction_location_candidates": {
                        "include_patterns": ["Invoice", "PO", "SO", "Branch"]
                    }
                },
                "payment_methods": {
                    "master_candidates": {
                        "include_patterns": ["PaymentMethod", "PayMethod"],
                        "boost_patterns": ["^PaymentMethod$"],
                        "penalty_patterns": ["DTL", "Refund", "Invoice"]
                    }
                }
            },
        },
    }


def object_record(schema_name, object_name, object_type):
    return {
        "schema_name": schema_name,
        "object_name": object_name,
        "object_type": object_type,
        "create_date": "2026-01-01T00:00:00",
        "modify_date": "2026-02-01T00:00:00",
    }


def column_record(schema_name, object_name, column_name, data_type):
    return {
        "object_schema": schema_name,
        "object_name": object_name,
        "column_name": column_name,
        "data_type": data_type,
        "max_length": 40,
        "precision": 18,
        "scale": 2,
        "is_nullable": True,
    }


def object_ids(records):
    return {record["object_id"] for record in records}


def candidate_by_id(candidates, object_id):
    for candidate in candidates:
        if candidate["object_id"] == object_id:
            return candidate
    return None


def all_group_candidates(group):
    candidates = list(group.get("top_candidates", []))
    for key, value in group.items():
        if key.endswith("_candidates") and isinstance(value, list):
            candidates.extend(value)
    return candidates


if __name__ == "__main__":
    unittest.main()
