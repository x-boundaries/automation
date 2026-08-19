"""Tests for the repository-only AutoCount 2.0 migration preflight contract.

Every fixture is synthetic. No vendor sample row is reused as a validation
fixture, no test contacts AutoCount, production SQL, a network provider or any
private business data, and no test performs or simulates an import.
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import autocount_migration_preflight as cli  # noqa: E402
import autocount_migration_preflight_contract as contract  # noqa: E402

SCHEMA_DIR = ROOT / "schemas"
MANIFEST_SCHEMA_PATH = SCHEMA_DIR / "autocount_migration_preflight_manifest.schema.json"
ROUTING_SCHEMA_PATH = SCHEMA_DIR / "autocount_migration_routing_evidence.schema.json"
EXECUTION_CONTEXT_SCHEMA_PATH = SCHEMA_DIR / "autocount_migration_execution_context_evidence.schema.json"

OPERATION_ID = "imop_" + "a1" * 16
OTHER_OPERATION_ID = "imop_" + "b2" * 16
BOOK_FINGERPRINT = contract.account_book_fingerprint("xb-synthetic-test-book")
OTHER_BOOK_FINGERPRINT = contract.account_book_fingerprint("xb-synthetic-other-book")


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #


def _authorities(*extra):
    keys = set(contract.COMMON_REQUIRED_AUTHORITIES) | set(extra)
    return {
        key: {"state": contract.AUTHORITY_RESOLVED, "resolved_by": "vendor", "evidence_ref": f"synthetic-evidence-{key}"}
        for key in sorted(keys)
    }


def stock_item_dataset():
    dataset = {
        "schema_version": contract.DATASET_SCHEMA_VERSION,
        "template_id": "xb_stock_item_v1",
        "template_identity": "synthetic_stock_item_template",
        "columns": ["PrimarySKU", "Description", "BaseUOM"],
        "rows": [
            {"sheet_row": 2, "values": {"PrimarySKU": "SKU-A", "Description": "Synthetic A", "BaseUOM": "UNIT"}},
            {"sheet_row": 3, "values": {"PrimarySKU": "SKU-B", "Description": "Synthetic B", "BaseUOM": "UNIT"}},
        ],
        "sku_records": [
            {
                "primary_sku": "SKU-A",
                "status": "current",
                "aliases": [{"namespace": "barcode", "value": "9990000000001", "uom": "UNIT"}],
            },
            {
                "primary_sku": "SKU-B",
                "status": "current",
                "aliases": [{"namespace": "barcode", "value": "9990000000002", "uom": "UNIT"}],
            },
        ],
        "alias_lookups": [{"namespace": "barcode", "value": "9990000000001", "uom": "UNIT"}],
    }
    return dataset


def stock_item_manifest(dataset=None):
    dataset = dataset or stock_item_dataset()
    return {
        "schema_version": contract.MANIFEST_SCHEMA_VERSION,
        "design_lock": contract.DESIGN_LOCK,
        "import_operation_id": OPERATION_ID,
        "target": {"surface": "stock_item", "account_book_fingerprint": BOOK_FINGERPRINT},
        "source": {
            "dataset_id": "synthetic-stock-item-001",
            "template_id": "xb_stock_item_v1",
            "template_identity": "synthetic_stock_item_template",
            "content_sha256": contract.dataset_content_sha256(dataset),
            "row_count": len(dataset["rows"]),
            "control_totals": {},
        },
        "paste_range": {
            "worksheet": "Sheet1",
            "header_row": 1,
            "first_row": 2,
            "last_row": 3,
            "first_column": "A",
            "last_column": "C",
        },
        "field_mappings": [
            {"source": "xb_stock_item_v1.PrimarySKU", "target": "stock_item.ItemCode"},
            {"source": "xb_stock_item_v1.Description", "target": "stock_item.Description"},
            {"source": "xb_stock_item_v1.BaseUOM", "target": "stock_item.BaseUOM"},
        ],
        "duplicate_item_code_action": {
            "state": "selected",
            "action": "OverWrite",
            "evidence_ref": "synthetic-duplicate-action-note",
        },
        "location_reference": {"state": contract.LOCATION_UNKNOWN},
        "conditional_authorities": _authorities(*contract.SURFACE_REQUIRED_AUTHORITIES["stock_item"]),
        "sku_identity": {
            "primary_sku_target": "stock_item.ItemCode",
            "require_unique_primary_sku": True,
            "require_alias_resolution": True,
        },
    }


def opening_dataset():
    return {
        "schema_version": contract.DATASET_SCHEMA_VERSION,
        "template_id": "xb_stock_open_v1",
        "template_identity": "synthetic_stock_open_template",
        "columns": ["ItemCode", "UOM", "Location", "BatchNo", "Seq", "Qty"],
        "rows": [
            {
                "sheet_row": 2,
                "values": {"ItemCode": "SKU-A", "UOM": "UNIT", "Location": "LOC1", "BatchNo": "", "Seq": "1", "Qty": "5"},
            },
            {
                "sheet_row": 3,
                "values": {"ItemCode": "SKU-B", "UOM": "UNIT", "Location": "LOC2", "BatchNo": "", "Seq": "1", "Qty": "4"},
            },
        ],
    }


def opening_manifest(dataset=None):
    dataset = dataset or opening_dataset()
    return {
        "schema_version": contract.MANIFEST_SCHEMA_VERSION,
        "design_lock": contract.DESIGN_LOCK,
        "import_operation_id": OPERATION_ID,
        "target": {"surface": "stock_item_opening", "account_book_fingerprint": BOOK_FINGERPRINT},
        "source": {
            "dataset_id": "synthetic-stock-open-001",
            "template_id": "xb_stock_open_v1",
            "template_identity": "synthetic_stock_open_template",
            "content_sha256": contract.dataset_content_sha256(dataset),
            "row_count": len(dataset["rows"]),
            "control_totals": {"stock_item_opening.Qty": "9"},
        },
        "paste_range": {
            "worksheet": "Sheet1",
            "header_row": 1,
            "first_row": 2,
            "last_row": 3,
            "first_column": "A",
            "last_column": "F",
        },
        "field_mappings": [
            {"source": "xb_stock_open_v1.ItemCode", "target": "stock_item_opening.ItemCode"},
            {"source": "xb_stock_open_v1.UOM", "target": "stock_item_opening.UOM"},
            {"source": "xb_stock_open_v1.Location", "target": "stock_item_opening.Location"},
            {"source": "xb_stock_open_v1.BatchNo", "target": "stock_item_opening.BatchNo"},
            {"source": "xb_stock_open_v1.Seq", "target": "stock_item_opening.Seq"},
            {"source": "xb_stock_open_v1.Qty", "target": "stock_item_opening.Qty"},
        ],
        "duplicate_item_code_action": {
            "state": "selected",
            "action": "Expand",
            "evidence_ref": "synthetic-duplicate-action-note",
        },
        "location_reference": {
            "state": contract.LOCATION_CONFIGURED,
            "source_ref": "synthetic-configured-location-set",
            "codes": ["LOC1", "LOC2"],
        },
        "conditional_authorities": _authorities(*contract.SURFACE_REQUIRED_AUTHORITIES["stock_item_opening"]),
        "item_opening": {
            "logical_key": list(contract.ITEM_OPENING_LOGICAL_KEY),
            "quantity_target": "stock_item_opening.Qty",
        },
    }


def ar_dataset():
    def row(sheet_row, doc, row_type):
        return {"sheet_row": sheet_row, "values": {"DocNo": doc, "RowType": row_type, "AccNo": "300-SYN", "Amount": "10"}}

    return {
        "schema_version": contract.DATASET_SCHEMA_VERSION,
        "template_id": "xb_ar_invoice_v1",
        "template_identity": "synthetic_ar_invoice_template",
        "columns": ["DocNo", "RowType", "AccNo", "Amount"],
        "rows": [
            row(2, "SYN-1", "header"),
            row(3, "SYN-1", "detail"),
            row(4, "SYN-2", "header"),
            row(5, "SYN-2", "detail"),
        ],
    }


def ar_manifest(dataset=None):
    dataset = dataset or ar_dataset()
    return {
        "schema_version": contract.MANIFEST_SCHEMA_VERSION,
        "design_lock": contract.DESIGN_LOCK,
        "import_operation_id": OPERATION_ID,
        "target": {"surface": "ar_invoice", "account_book_fingerprint": BOOK_FINGERPRINT},
        "source": {
            "dataset_id": "synthetic-ar-invoice-001",
            "template_id": "xb_ar_invoice_v1",
            "template_identity": "synthetic_ar_invoice_template",
            "content_sha256": contract.dataset_content_sha256(dataset),
            "row_count": len(dataset["rows"]),
            "control_totals": {},
        },
        "paste_range": {
            "worksheet": "Sheet1",
            "header_row": 1,
            "first_row": 2,
            "last_row": 5,
            "first_column": "A",
            "last_column": "D",
        },
        "field_mappings": [
            {"source": "xb_ar_invoice_v1.DocNo", "target": "ar_invoice.DocNo"},
            {"source": "xb_ar_invoice_v1.AccNo", "target": "ar_invoice.AccNo"},
            {"source": "xb_ar_invoice_v1.Amount", "target": "ar_invoice.Amount"},
        ],
        "duplicate_item_code_action": {"state": contract.AUTHORITY_UNKNOWN},
        "location_reference": {"state": contract.LOCATION_UNKNOWN},
        "conditional_authorities": _authorities(*contract.SURFACE_REQUIRED_AUTHORITIES["ar_invoice"]),
        "arap_ordering": {
            "document_column": "DocNo",
            "row_type_column": "RowType",
            "header_value": "header",
            "detail_value": "detail",
        },
    }


def routing_evidence(manifest, state=contract.ROUTING_ENABLED, operation_id=None):
    evidence = {
        "schema_version": contract.ROUTING_EVIDENCE_SCHEMA_VERSION,
        "import_operation_id": operation_id or manifest["import_operation_id"],
        "state": state,
    }
    if state != contract.ROUTING_UNKNOWN:
        evidence["observation_ref"] = "synthetic-routing-observation"
    if state == contract.ROUTING_ENABLED:
        evidence["mappings"] = [
            {"source": mapping["source"], "destination": mapping["target"]} for mapping in manifest["field_mappings"]
        ]
    return evidence


def execution_context_evidence(manifest, operation_id=None, account_book=True, import_surface=True):
    evidence = {
        "schema_version": contract.EXECUTION_CONTEXT_SCHEMA_VERSION,
        "import_operation_id": operation_id or manifest["import_operation_id"],
    }
    if account_book:
        evidence["account_book"] = {
            "state": contract.ATTESTATION_ATTESTED,
            "actual_fingerprint": manifest["target"]["account_book_fingerprint"],
            "attestation_ref": "synthetic-account-book-attestation",
        }
    if import_surface:
        evidence["import_surface"] = {
            "state": contract.ATTESTATION_ATTESTED,
            "actual_surface": manifest["target"]["surface"],
            "attestation_ref": "synthetic-import-surface-attestation",
        }
    return evidence


def preflight(manifest, dataset, routing=None, execution=None, complete_evidence=True):
    """Run the preflight, defaulting to complete positive evidence."""
    if complete_evidence:
        routing = routing_evidence(manifest) if routing is None else routing
        execution = execution_context_evidence(manifest) if execution is None else execution
    return contract.run_preflight(manifest, dataset, routing, execution)


def gate_state(result, gate):
    for entry in result["canonical"]["gates"]:
        if entry["gate"] == gate:
            return entry["state"]
    raise AssertionError(f"gate not present: {gate}")


def codes(result, gate=None):
    return {
        item["code"]
        for item in result["canonical"]["findings"]
        if gate is None or item["gate"] == gate
    }


class PreflightAssertions(unittest.TestCase):
    def assertBlocked(self, result):
        self.assertEqual(result["canonical"]["preflight_status"], contract.PREFLIGHT_BLOCKED)
        self.assertEqual(result["canonical"]["import_readiness"], contract.NOT_IMPORT_READY)

    def assertPassed(self, result):
        self.assertEqual(
            result["canonical"]["preflight_status"],
            contract.PREFLIGHT_PASS,
            msg=sorted(
                (item["gate"], item["code"])
                for item in result["canonical"]["findings"]
                if item["state"] in contract.BLOCKING_STATES
            ),
        )
        self.assertEqual(result["canonical"]["import_readiness"], contract.REPOSITORY_PREFLIGHT_SATISFIED)


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


class HappyPathTests(PreflightAssertions):
    def test_stock_item_fixture_passes_every_gate(self):
        dataset = stock_item_dataset()
        self.assertPassed(preflight(stock_item_manifest(dataset), dataset))

    def test_stock_item_opening_fixture_passes_every_gate(self):
        dataset = opening_dataset()
        self.assertPassed(preflight(opening_manifest(dataset), dataset))

    def test_ar_invoice_fixture_passes_every_gate(self):
        dataset = ar_dataset()
        self.assertPassed(preflight(ar_manifest(dataset), dataset))


# --------------------------------------------------------------------------- #
# Determinism and source authority
# --------------------------------------------------------------------------- #


class DeterminismTests(PreflightAssertions):
    def test_identical_input_and_evidence_produce_identical_canonical_content(self):
        first_dataset = stock_item_dataset()
        second_dataset = stock_item_dataset()
        first = preflight(stock_item_manifest(first_dataset), first_dataset)
        second = preflight(stock_item_manifest(second_dataset), second_dataset)
        self.assertEqual(contract.canonical_json(first["canonical"]), contract.canonical_json(second["canonical"]))
        self.assertEqual(first["canonical_hash"], second["canonical_hash"])

    def test_canonical_content_carries_no_timestamp_or_run_metadata(self):
        dataset = stock_item_dataset()
        serialized = contract.canonical_json(preflight(stock_item_manifest(dataset), dataset)["canonical"])
        for volatile in ("generated_at", "timestamp", "run_id", "uuid"):
            self.assertNotIn(volatile, serialized)

    def test_source_content_hash_mismatch_blocks(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        dataset["rows"][0]["values"]["Description"] = "Edited after freeze"
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("source_content_hash_mismatch", codes(result, contract.GATE_SOURCE_AUTHORITY))

    def test_template_identity_mismatch_blocks(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["source"]["template_identity"] = "synthetic_other_template"
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("template_identity_mismatch", codes(result, contract.GATE_SOURCE_AUTHORITY))

    def test_row_count_mismatch_blocks(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["source"]["row_count"] = 99
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("row_count_mismatch", codes(result, contract.GATE_SOURCE_AUTHORITY))

    def test_control_total_mismatch_blocks(self):
        dataset = opening_dataset()
        manifest = opening_manifest(dataset)
        manifest["source"]["control_totals"]["stock_item_opening.Qty"] = "8"
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("control_total_mismatch", codes(result, contract.GATE_SOURCE_AUTHORITY))


# --------------------------------------------------------------------------- #
# Columns
# --------------------------------------------------------------------------- #


class ColumnTests(PreflightAssertions):
    def test_one_unknown_extra_column_blocks_and_is_not_dropped(self):
        dataset = stock_item_dataset()
        dataset["columns"].append("VendorExtensionUdf1")
        for row in dataset["rows"]:
            row["values"]["VendorExtensionUdf1"] = "unclassified"
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_COLUMNS), contract.STATE_UNKNOWN)
        self.assertIn("unrecognised_input_column", codes(result, contract.GATE_COLUMNS))
        subjects = {
            item["subject"]
            for item in result["canonical"]["findings"]
            if item["code"] == "unrecognised_input_column"
        }
        self.assertEqual(subjects, {"xb_stock_item_v1.VendorExtensionUdf1"})

    def test_declared_contract_column_absent_from_dataset_blocks(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["field_mappings"].append({"source": "xb_stock_item_v1.Missing", "target": "stock_item.Missing"})
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("declared_contract_column_absent", codes(result, contract.GATE_COLUMNS))

    def test_arap_row_type_column_is_explicitly_classified_not_silently_dropped(self):
        dataset = ar_dataset()
        result = preflight(ar_manifest(dataset), dataset)
        self.assertPassed(result)
        self.assertIn("structural_control_column_classified", codes(result, contract.GATE_COLUMNS))


# --------------------------------------------------------------------------- #
# Paste range
# --------------------------------------------------------------------------- #


class PasteRangeTests(PreflightAssertions):
    def test_absent_paste_range_is_refused_and_reported_as_blocked(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        del manifest["paste_range"]
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)
        result = contract.run_preflight_safe(manifest, dataset, routing_evidence(manifest), execution_context_evidence(manifest))
        self.assertBlocked(result)
        self.assertTrue(result["canonical"]["structural_refusal"])

    def test_malformed_paste_range_is_refused(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["paste_range"]["last_row"] = 1
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)

    def test_range_inconsistent_with_validated_rows_blocks(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        # A whole-sheet style over-wide range would sweep in blank or vendor rows.
        manifest["paste_range"]["last_row"] = 1000
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("paste_range_row_span_mismatch", codes(result, contract.GATE_PASTE_RANGE))

    def test_row_outside_declared_range_blocks(self):
        dataset = stock_item_dataset()
        dataset["rows"][1]["sheet_row"] = 40
        manifest = stock_item_manifest(dataset)
        manifest["paste_range"]["last_row"] = 3
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("row_outside_declared_paste_range", codes(result, contract.GATE_PASTE_RANGE))


# --------------------------------------------------------------------------- #
# Qualified field identity
# --------------------------------------------------------------------------- #


class QualifiedIdentityTests(PreflightAssertions):
    def test_bare_field_name_is_never_an_identity(self):
        for bare in ("AccNo", "Qty", "UOM", "Location", "ExpiryDate", "Seq"):
            with self.subTest(bare=bare):
                self.assertFalse(contract.is_qualified(bare))
                with self.assertRaises(contract.ContractError):
                    contract.split_qualified(bare)

    def test_same_spelling_on_two_surfaces_yields_two_distinct_identities(self):
        self.assertNotEqual(
            contract.qualify("ar_invoice", "AccNo"),
            contract.qualify("creditor", "AccNo"),
        )
        self.assertNotEqual(
            contract.qualify("stock_item", "Qty"),
            contract.qualify("stock_item_opening", "Qty"),
        )

    def test_manifest_with_bare_mapping_identity_is_refused(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["field_mappings"][0]["target"] = "ItemCode"
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)

    def test_mapping_targeting_another_surface_blocks(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["field_mappings"][1]["target"] = "debtor.Description"
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("target_owned_by_other_surface", codes(result, contract.GATE_FIELD_IDENTITY))

    def test_migration_surfaces_do_not_include_the_member_write_subsystem(self):
        # A same-named member field must not acquire a migration identity.
        self.assertNotIn("member", contract.TARGET_SURFACES)
        self.assertTrue(contract.is_qualified(contract.qualify("ar_invoice", "ExpiryDate")))
        self.assertNotEqual(contract.qualify("ar_invoice", "ExpiryDate"), "ExpiryDate")


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


class RoutingTests(PreflightAssertions):
    def setUp(self):
        self.dataset = stock_item_dataset()
        self.manifest = stock_item_manifest(self.dataset)

    def _run(self, routing):
        return contract.run_preflight(
            self.manifest, self.dataset, routing, execution_context_evidence(self.manifest)
        )

    def test_absent_routing_evidence_blocks_and_is_not_read_as_disabled(self):
        result = self._run(None)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_ROUTING), contract.STATE_UNKNOWN)
        self.assertIn("routing_evidence_absent", codes(result, contract.GATE_ROUTING))
        self.assertNotIn("routing_positively_not_applicable", codes(result, contract.GATE_ROUTING))

    def test_routing_state_unknown_blocks(self):
        result = self._run(routing_evidence(self.manifest, contract.ROUTING_UNKNOWN))
        self.assertBlocked(result)
        self.assertIn("routing_state_unknown", codes(result, contract.GATE_ROUTING))

    def test_enabled_complete_matching_mapping_passes_its_gate(self):
        result = self._run(routing_evidence(self.manifest, contract.ROUTING_ENABLED))
        self.assertPassed(result)
        self.assertEqual(gate_state(result, contract.GATE_ROUTING), contract.STATE_PASS)

    def test_enabled_partial_mapping_blocks(self):
        routing = routing_evidence(self.manifest, contract.ROUTING_ENABLED)
        routing["mappings"].pop()
        result = self._run(routing)
        self.assertBlocked(result)
        self.assertIn("routing_mapping_incomplete", codes(result, contract.GATE_ROUTING))

    def test_enabled_destination_mismatch_blocks(self):
        routing = routing_evidence(self.manifest, contract.ROUTING_ENABLED)
        routing["mappings"][0]["destination"] = "stock_item.Description2"
        result = self._run(routing)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_ROUTING), contract.STATE_MISMATCH)
        self.assertIn("routing_destination_mismatch", codes(result, contract.GATE_ROUTING))

    def test_enabled_mapping_of_undeclared_source_blocks(self):
        routing = routing_evidence(self.manifest, contract.ROUTING_ENABLED)
        routing["mappings"].append({"source": "xb_stock_item_v1.Rogue", "destination": "stock_item.Rogue"})
        result = self._run(routing)
        self.assertBlocked(result)
        self.assertIn("routing_maps_undeclared_source", codes(result, contract.GATE_ROUTING))

    def test_positively_disabled_evidence_passes_its_gate(self):
        result = self._run(routing_evidence(self.manifest, contract.ROUTING_DISABLED))
        self.assertPassed(result)
        self.assertIn("routing_positively_not_applicable", codes(result, contract.GATE_ROUTING))

    def test_positively_unavailable_evidence_passes_its_gate(self):
        result = self._run(routing_evidence(self.manifest, contract.ROUTING_UNAVAILABLE))
        self.assertPassed(result)

    def test_prior_saved_mapping_is_not_standing_authority(self):
        routing = routing_evidence(self.manifest, contract.ROUTING_ENABLED, operation_id=OTHER_OPERATION_ID)
        result = self._run(routing)
        self.assertBlocked(result)
        self.assertIn("routing_evidence_not_current", codes(result, contract.GATE_ROUTING))


# --------------------------------------------------------------------------- #
# Execution context
# --------------------------------------------------------------------------- #


class ExecutionContextTests(PreflightAssertions):
    def setUp(self):
        self.dataset = stock_item_dataset()
        self.manifest = stock_item_manifest(self.dataset)

    def _run(self, execution):
        return contract.run_preflight(self.manifest, self.dataset, routing_evidence(self.manifest), execution)

    def test_no_attestation_blocks_both_dimensions(self):
        result = self._run(None)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_EXECUTION_CONTEXT), contract.STATE_UNKNOWN)
        self.assertIn("account_book_attestation_absent", codes(result, contract.GATE_EXECUTION_CONTEXT))
        self.assertIn("import_surface_attestation_absent", codes(result, contract.GATE_EXECUTION_CONTEXT))

    def test_account_book_only_blocks(self):
        result = self._run(execution_context_evidence(self.manifest, import_surface=False))
        self.assertBlocked(result)
        self.assertIn("import_surface_attestation_absent", codes(result, contract.GATE_EXECUTION_CONTEXT))

    def test_import_surface_only_blocks(self):
        result = self._run(execution_context_evidence(self.manifest, account_book=False))
        self.assertBlocked(result)
        self.assertIn("account_book_attestation_absent", codes(result, contract.GATE_EXECUTION_CONTEXT))

    def test_account_book_mismatch_blocks(self):
        evidence = execution_context_evidence(self.manifest)
        evidence["account_book"]["actual_fingerprint"] = OTHER_BOOK_FINGERPRINT
        result = self._run(evidence)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_EXECUTION_CONTEXT), contract.STATE_MISMATCH)
        self.assertIn("account_book_mismatch", codes(result, contract.GATE_EXECUTION_CONTEXT))

    def test_import_surface_mismatch_blocks(self):
        evidence = execution_context_evidence(self.manifest)
        evidence["import_surface"]["actual_surface"] = "debtor"
        result = self._run(evidence)
        self.assertBlocked(result)
        self.assertIn("import_surface_mismatch", codes(result, contract.GATE_EXECUTION_CONTEXT))

    def test_both_current_matching_attestations_pass_that_gate(self):
        result = self._run(execution_context_evidence(self.manifest))
        self.assertPassed(result)
        self.assertEqual(gate_state(result, contract.GATE_EXECUTION_CONTEXT), contract.STATE_PASS)

    def test_prior_attestation_is_not_standing_authority(self):
        result = self._run(execution_context_evidence(self.manifest, operation_id=OTHER_OPERATION_ID))
        self.assertBlocked(result)
        self.assertIn("execution_context_not_current", codes(result, contract.GATE_EXECUTION_CONTEXT))

    def test_unverifiable_state_blocks(self):
        evidence = execution_context_evidence(self.manifest)
        evidence["account_book"] = {"state": contract.ATTESTATION_UNKNOWN}
        result = self._run(evidence)
        self.assertBlocked(result)
        self.assertIn("account_book_attestation_absent", codes(result, contract.GATE_EXECUTION_CONTEXT))

    def test_account_book_target_is_a_non_secret_fingerprint(self):
        self.assertRegex(BOOK_FINGERPRINT, r"^acctbk_[0-9a-f]{64}$")
        self.assertNotIn("xb-synthetic-test-book", BOOK_FINGERPRINT)


# --------------------------------------------------------------------------- #
# Duplicate Item Code Action
# --------------------------------------------------------------------------- #


class DuplicateItemCodeActionTests(PreflightAssertions):
    def test_absent_selected_action_blocks_with_no_assumed_default(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["duplicate_item_code_action"] = {"state": contract.AUTHORITY_UNKNOWN}
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("duplicate_item_code_action_unknown", codes(result, contract.GATE_DUPLICATE_ITEM_CODE_ACTION))

    def test_only_official_option_names_are_accepted(self):
        self.assertEqual(contract.DUPLICATE_ITEM_CODE_ACTIONS, ("OverWrite", "Expand"))
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["duplicate_item_code_action"]["action"] = "Skip"
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)

    def test_not_applicable_on_a_non_item_code_surface(self):
        dataset = ar_dataset()
        result = preflight(ar_manifest(dataset), dataset)
        self.assertPassed(result)
        self.assertIn(
            "duplicate_item_code_action_not_applicable",
            codes(result, contract.GATE_DUPLICATE_ITEM_CODE_ACTION),
        )


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #


class LocationTests(PreflightAssertions):
    def test_absent_reference_set_blocks_when_location_is_mapped(self):
        dataset = opening_dataset()
        manifest = opening_manifest(dataset)
        manifest["location_reference"] = {"state": contract.LOCATION_UNKNOWN}
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("location_reference_set_absent", codes(result, contract.GATE_LOCATION))

    def test_location_outside_configured_set_blocks(self):
        dataset = opening_dataset()
        dataset["rows"][0]["values"]["Location"] = "LOC-NOT-CONFIGURED"
        manifest = opening_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_LOCATION), contract.STATE_MISMATCH)
        self.assertIn("location_outside_configured_set", codes(result, contract.GATE_LOCATION))

    def test_configured_location_passes(self):
        dataset = opening_dataset()
        result = preflight(opening_manifest(dataset), dataset)
        self.assertPassed(result)
        self.assertEqual(gate_state(result, contract.GATE_LOCATION), contract.STATE_PASS)

    def test_no_historical_location_code_is_hardcoded(self):
        source = (SCRIPTS / "autocount_migration_preflight_contract.py").read_text(encoding="utf-8")
        for historical in ("XB01", "XB02", "XB03", "XB04", "XB05"):
            self.assertNotIn(historical, source)


# --------------------------------------------------------------------------- #
# SKU and alias identity
# --------------------------------------------------------------------------- #


class SkuAliasTests(PreflightAssertions):
    def test_valid_unique_mapping_passes(self):
        dataset = stock_item_dataset()
        result = preflight(stock_item_manifest(dataset), dataset)
        self.assertPassed(result)
        self.assertEqual(gate_state(result, contract.GATE_SKU_ALIAS), contract.STATE_PASS)

    def test_missing_primary_sku_blocks(self):
        dataset = stock_item_dataset()
        dataset["sku_records"][0]["primary_sku"] = ""
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("primary_sku_missing", codes(result, contract.GATE_SKU_ALIAS))

    def test_duplicate_current_primary_sku_blocks(self):
        dataset = stock_item_dataset()
        dataset["sku_records"][1]["primary_sku"] = "SKU-A"
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("duplicate_current_primary_sku", codes(result, contract.GATE_SKU_ALIAS))

    def test_alias_resolving_to_zero_current_skus_blocks(self):
        dataset = stock_item_dataset()
        dataset["alias_lookups"] = [{"namespace": "barcode", "value": "9990000009999", "uom": "UNIT"}]
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("alias_resolves_to_no_current_sku", codes(result, contract.GATE_SKU_ALIAS))

    def test_alias_resolving_to_more_than_one_current_sku_blocks(self):
        dataset = stock_item_dataset()
        # The same barcode alias, at the same UOM, claimed by two different
        # current SKUs: both the duplication and the ambiguous resolution must
        # surface, because either one alone would understate the defect.
        dataset["sku_records"][1]["aliases"] = [{"namespace": "barcode", "value": "9990000000001", "uom": "UNIT"}]
        dataset["alias_lookups"] = [{"namespace": "barcode", "value": "9990000000001", "uom": "UNIT"}]
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("alias_resolves_to_multiple_current_skus", codes(result, contract.GATE_SKU_ALIAS))
        self.assertIn("duplicate_alias_in_namespace", codes(result, contract.GATE_SKU_ALIAS))

    def test_uom_qualification_separates_two_otherwise_identical_barcodes(self):
        # BarCode is UOM-scoped, so the same barcode value at two different UOMs
        # is two distinct aliases and resolves unambiguously at each UOM.
        dataset = stock_item_dataset()
        dataset["sku_records"][1]["aliases"] = [{"namespace": "barcode", "value": "9990000000001", "uom": "PACK"}]
        dataset["alias_lookups"] = [
            {"namespace": "barcode", "value": "9990000000001", "uom": "UNIT"},
            {"namespace": "barcode", "value": "9990000000001", "uom": "PACK"},
        ]
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertPassed(result)

    def test_conflicting_duplicate_alias_in_one_namespace_blocks(self):
        dataset = stock_item_dataset()
        dataset["sku_records"][1]["aliases"] = [{"namespace": "barcode", "value": "9990000000001", "uom": "UNIT"}]
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("duplicate_alias_in_namespace", codes(result, contract.GATE_SKU_ALIAS))

    def test_barcode_alias_without_uom_qualification_blocks(self):
        dataset = stock_item_dataset()
        del dataset["sku_records"][0]["aliases"][0]["uom"]
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("alias_uom_qualification_missing", codes(result, contract.GATE_SKU_ALIAS))

    def test_barcode_lookup_without_uom_qualification_blocks(self):
        dataset = stock_item_dataset()
        del dataset["alias_lookups"][0]["uom"]
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("alias_lookup_uom_qualification_missing", codes(result, contract.GATE_SKU_ALIAS))

    def test_row_primary_sku_must_resolve_to_a_current_record(self):
        dataset = stock_item_dataset()
        dataset["rows"][0]["values"]["PrimarySKU"] = "SKU-UNKNOWN"
        manifest = stock_item_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("row_primary_sku_not_current", codes(result, contract.GATE_SKU_ALIAS))

    def test_internal_product_id_and_sku_history_stay_out_of_the_contract(self):
        source = (SCRIPTS / "autocount_migration_preflight_contract.py").read_text(encoding="utf-8")
        self.assertNotIn("InternalProductID", source.split('"""', 2)[2])
        self.assertNotIn("SKU_History", source.split('"""', 2)[2])


# --------------------------------------------------------------------------- #
# Quantity
# --------------------------------------------------------------------------- #


class QuantityTests(PreflightAssertions):
    def test_stock_item_unresolved_qty_use_blocks(self):
        dataset = stock_item_dataset()
        dataset["columns"].append("Qty")
        for row in dataset["rows"]:
            row["values"]["Qty"] = "1"
        manifest = stock_item_manifest(dataset)
        manifest["field_mappings"].append({"source": "xb_stock_item_v1.Qty", "target": "stock_item.Qty"})
        manifest["paste_range"]["last_column"] = "D"
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_QUANTITY), contract.STATE_UNKNOWN)
        self.assertIn("stock_item_unresolved_field_used", codes(result, contract.GATE_QUANTITY))

    def test_stock_item_qty_is_not_reinterpreted_as_opening_quantity(self):
        # The unresolved Stock Item field is a distinct identity from the opening
        # quantity, and no gate silently substitutes one for the other.
        self.assertNotEqual(
            contract.qualify("stock_item", "Qty"),
            contract.qualify("stock_item_opening", "Qty"),
        )

    def test_zero_opening_quantity_surfaces_a_blocking_finding(self):
        dataset = opening_dataset()
        dataset["rows"][0]["values"]["Qty"] = "0"
        manifest = opening_manifest(dataset)
        manifest["source"]["control_totals"]["stock_item_opening.Qty"] = "4"
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("opening_quantity_zero_requires_disposition", codes(result, contract.GATE_QUANTITY))

    def test_negative_opening_quantity_surfaces_a_blocking_finding(self):
        dataset = opening_dataset()
        dataset["rows"][0]["values"]["Qty"] = "-5"
        manifest = opening_manifest(dataset)
        manifest["source"]["control_totals"]["stock_item_opening.Qty"] = "-1"
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("opening_quantity_negative_requires_disposition", codes(result, contract.GATE_QUANTITY))


# --------------------------------------------------------------------------- #
# Item Opening grain
# --------------------------------------------------------------------------- #


class ItemOpeningKeyTests(PreflightAssertions):
    def test_documented_key_includes_seq_and_excludes_serialno(self):
        self.assertEqual(contract.ITEM_OPENING_LOGICAL_KEY, ("ItemCode", "UOM", "Location", "BatchNo", "Seq"))
        self.assertNotIn("SerialNo", contract.ITEM_OPENING_LOGICAL_KEY)

    def test_key_without_seq_blocks(self):
        dataset = opening_dataset()
        manifest = opening_manifest(dataset)
        manifest["item_opening"]["logical_key"] = ["ItemCode", "UOM", "Location", "BatchNo"]
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("item_opening_key_mismatch", codes(result, contract.GATE_ITEM_OPENING_KEY))

    def test_serialno_is_not_silently_promoted_into_the_key(self):
        dataset = opening_dataset()
        manifest = opening_manifest(dataset)
        manifest["item_opening"]["logical_key"] = ["ItemCode", "UOM", "Location", "BatchNo", "Seq", "SerialNo"]
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("excluded_field_promoted_into_key", codes(result, contract.GATE_ITEM_OPENING_KEY))

    def test_missing_excel_seq_authority_remains_fail_closed(self):
        dataset = opening_dataset()
        manifest = opening_manifest(dataset)
        manifest["conditional_authorities"]["item_opening_excel_seq"] = {"state": contract.AUTHORITY_UNKNOWN}
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("item_opening_excel_seq_authority_absent", codes(result, contract.GATE_ITEM_OPENING_KEY))
        self.assertIn("conditional_authority_unresolved", codes(result, contract.GATE_CONDITIONAL_AUTHORITY))


# --------------------------------------------------------------------------- #
# AR/AP ordering
# --------------------------------------------------------------------------- #


class ArApOrderingTests(PreflightAssertions):
    def test_valid_continuation_order_passes(self):
        dataset = ar_dataset()
        result = preflight(ar_manifest(dataset), dataset)
        self.assertPassed(result)
        self.assertEqual(gate_state(result, contract.GATE_ARAP_ORDERING), contract.STATE_PASS)

    def test_reordered_documents_break_the_continuation_contract(self):
        dataset = ar_dataset()
        rows = dataset["rows"]
        # Sort by document value, which interleaves the two document groups.
        dataset["rows"] = [rows[0], rows[2], rows[1], rows[3]]
        for index, row in enumerate(dataset["rows"]):
            row["sheet_row"] = index + 2
        manifest = ar_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("arap_document_rows_not_contiguous", codes(result, contract.GATE_ARAP_ORDERING))

    def test_detail_row_before_its_header_blocks(self):
        dataset = ar_dataset()
        dataset["rows"][0]["values"]["RowType"] = "detail"
        dataset["rows"][1]["values"]["RowType"] = "header"
        manifest = ar_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("arap_detail_precedes_header", codes(result, contract.GATE_ARAP_ORDERING))

    def test_broken_input_row_order_blocks(self):
        dataset = ar_dataset()
        dataset["rows"][1]["sheet_row"] = 2
        dataset["rows"][0]["sheet_row"] = 3
        manifest = ar_manifest(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("arap_input_order_broken", codes(result, contract.GATE_ARAP_ORDERING))

    def test_document_without_a_detail_row_blocks(self):
        dataset = ar_dataset()
        dataset["rows"] = dataset["rows"][:1] + dataset["rows"][2:]
        for index, row in enumerate(dataset["rows"]):
            row["sheet_row"] = index + 2
        manifest = ar_manifest(dataset)
        manifest["paste_range"]["last_row"] = 4
        manifest["source"]["row_count"] = 3
        manifest["source"]["content_sha256"] = contract.dataset_content_sha256(dataset)
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertIn("arap_document_without_detail", codes(result, contract.GATE_ARAP_ORDERING))

    def test_debtor_master_surface_is_distinct_from_ar_openings(self):
        self.assertIn("debtor", contract.TARGET_SURFACES)
        self.assertIn("ar_invoice", contract.TARGET_SURFACES)
        self.assertNotIn("debtor", contract.ARAP_SURFACES)


# --------------------------------------------------------------------------- #
# Conditional authority
# --------------------------------------------------------------------------- #


class ConditionalAuthorityTests(PreflightAssertions):
    def test_required_unresolved_authority_remains_not_import_ready(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["conditional_authorities"]["mandatory_columns"] = {"state": contract.AUTHORITY_UNKNOWN}
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        self.assertEqual(gate_state(result, contract.GATE_CONDITIONAL_AUTHORITY), contract.STATE_UNKNOWN)

    def test_omitted_authority_is_unresolved_rather_than_assumed(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        del manifest["conditional_authorities"]["sandbox_test_company_evidence"]
        result = preflight(manifest, dataset)
        self.assertBlocked(result)
        subjects = {
            item["subject"]
            for item in result["canonical"]["findings"]
            if item["code"] == "conditional_authority_unresolved"
        }
        self.assertIn("sandbox_test_company_evidence", subjects)

    def test_resolved_authority_requires_a_resolver_and_evidence_reference(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["conditional_authorities"]["costing"] = {"state": contract.AUTHORITY_RESOLVED}
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)

    def test_authority_outside_the_locked_register_is_refused(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["conditional_authorities"]["invented_authority"] = {"state": contract.AUTHORITY_UNKNOWN}
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)

    def test_every_surface_requires_the_common_authorities(self):
        for surface in contract.TARGET_SURFACES:
            with self.subTest(surface=surface):
                required = contract.required_authorities(surface)
                for key in contract.COMMON_REQUIRED_AUTHORITIES:
                    self.assertIn(key, required)


# --------------------------------------------------------------------------- #
# Result semantics and public safety
# --------------------------------------------------------------------------- #


class ResultSemanticsTests(PreflightAssertions):
    def test_result_never_claims_an_import_occurred(self):
        dataset = stock_item_dataset()
        result = preflight(stock_item_manifest(dataset), dataset)
        canonical = result["canonical"]
        self.assertFalse(canonical["import_performed"])
        self.assertFalse(canonical["live_autocount_contacted"])
        self.assertEqual(canonical["production_sql_writes"], 0)
        serialized = contract.canonical_json(canonical)
        for misleading in ("IMPORTED", "PRODUCTION_READY", "CUTOVER_COMPLETE"):
            self.assertNotIn(misleading, serialized)

    def test_pass_is_repository_preflight_only_not_import_approval(self):
        self.assertEqual(contract.PREFLIGHT_PASS, "PREPARED_VALIDATION_PASS")
        self.assertEqual(contract.REPOSITORY_PREFLIGHT_SATISFIED, "REPOSITORY_PREFLIGHT_SATISFIED")

    def test_findings_carry_no_row_cell_values(self):
        dataset = opening_dataset()
        dataset["rows"][0]["values"]["Location"] = "LOC-NOT-CONFIGURED"
        manifest = opening_manifest(dataset)
        result = preflight(manifest, dataset)
        serialized = contract.canonical_json(result["canonical"])
        self.assertNotIn("LOC-NOT-CONFIGURED", serialized)

    def test_forbidden_credential_key_is_refused_anywhere_in_a_manifest(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["source"]["connection_string"] = "[REDACTED]"
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)

    def test_forbidden_credential_key_is_refused_in_evidence(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        evidence = execution_context_evidence(manifest)
        evidence["account_book"]["api_token"] = "[REDACTED]"
        with self.assertRaises(contract.ContractError):
            contract.validate_execution_context_evidence(evidence)

    def test_unknown_manifest_field_is_refused(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        manifest["extra_field"] = True
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest)

    def test_gate_order_covers_every_declared_gate(self):
        self.assertEqual(len(set(contract.GATE_ORDER)), len(contract.GATE_ORDER))
        dataset = stock_item_dataset()
        result = preflight(stock_item_manifest(dataset), dataset)
        self.assertEqual([entry["gate"] for entry in result["canonical"]["gates"]], list(contract.GATE_ORDER))


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class SchemaTests(unittest.TestCase):
    def _schema(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_schema_files_are_valid_json_and_reject_unknown_fields(self):
        for path in (MANIFEST_SCHEMA_PATH, ROUTING_SCHEMA_PATH, EXECUTION_CONTEXT_SCHEMA_PATH):
            with self.subTest(schema=path.name):
                schema = self._schema(path)
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertFalse(schema["additionalProperties"])

    def test_schema_versions_match_the_python_contract(self):
        self.assertEqual(
            self._schema(MANIFEST_SCHEMA_PATH)["properties"]["schema_version"]["const"],
            contract.MANIFEST_SCHEMA_VERSION,
        )
        self.assertEqual(
            self._schema(ROUTING_SCHEMA_PATH)["properties"]["schema_version"]["const"],
            contract.ROUTING_EVIDENCE_SCHEMA_VERSION,
        )
        self.assertEqual(
            self._schema(EXECUTION_CONTEXT_SCHEMA_PATH)["properties"]["schema_version"]["const"],
            contract.EXECUTION_CONTEXT_SCHEMA_VERSION,
        )

    def test_schema_surface_vocabulary_matches_the_python_contract(self):
        schema = self._schema(MANIFEST_SCHEMA_PATH)
        self.assertEqual(
            tuple(schema["properties"]["target"]["properties"]["surface"]["enum"]),
            contract.TARGET_SURFACES,
        )

    def test_schema_authority_register_matches_the_python_contract(self):
        schema = self._schema(MANIFEST_SCHEMA_PATH)
        register = schema["properties"]["conditional_authorities"]["propertyNames"]["enum"]
        self.assertEqual(tuple(sorted(register)), contract.CONDITIONAL_AUTHORITY_KEYS)

    def test_schema_routing_states_match_the_python_contract(self):
        schema = self._schema(ROUTING_SCHEMA_PATH)
        self.assertEqual(tuple(schema["properties"]["state"]["enum"]), contract.ROUTING_STATES)

    def test_manifest_fixture_validates_against_the_real_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        dataset = stock_item_dataset()
        jsonschema.validate(stock_item_manifest(dataset), self._schema(MANIFEST_SCHEMA_PATH))
        jsonschema.validate(opening_manifest(opening_dataset()), self._schema(MANIFEST_SCHEMA_PATH))
        jsonschema.validate(ar_manifest(ar_dataset()), self._schema(MANIFEST_SCHEMA_PATH))

    def test_unknown_evidence_field_fails_the_real_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        routing = routing_evidence(manifest)
        jsonschema.validate(routing, self._schema(ROUTING_SCHEMA_PATH))
        bad = copy.deepcopy(routing)
        bad["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, self._schema(ROUTING_SCHEMA_PATH))

    def test_enabled_routing_without_mappings_fails_the_real_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        bad = {
            "schema_version": contract.ROUTING_EVIDENCE_SCHEMA_VERSION,
            "import_operation_id": OPERATION_ID,
            "state": "enabled",
            "observation_ref": "synthetic-routing-observation",
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, self._schema(ROUTING_SCHEMA_PATH))

    def test_attested_execution_context_without_evidence_fails_the_real_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        bad = {
            "schema_version": contract.EXECUTION_CONTEXT_SCHEMA_VERSION,
            "import_operation_id": OPERATION_ID,
            "account_book": {"state": "ATTESTED"},
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, self._schema(EXECUTION_CONTEXT_SCHEMA_PATH))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class CliTests(PreflightAssertions):
    def _write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)

    def test_hash_dataset_matches_the_contract_helper(self):
        dataset = stock_item_dataset()
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = self._write(tmp, "dataset.json", dataset)
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "autocount_migration_preflight.py"), "--hash-dataset", dataset_path],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), contract.dataset_content_sha256(dataset))

    def test_cli_run_is_deterministic_and_reports_blocked_without_evidence(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        with tempfile.TemporaryDirectory() as tmp:
            args = cli.build_parser().parse_args(
                [
                    "--manifest",
                    self._write(tmp, "manifest.json", manifest),
                    "--dataset",
                    self._write(tmp, "dataset.json", dataset),
                ]
            )
            first = cli.run(args)
            second = cli.run(args)
        self.assertBlocked(first)
        self.assertEqual(first["canonical_hash"], second["canonical_hash"])
        self.assertNotIn("generated_at", contract.canonical_json(first["canonical"]))
        self.assertIn("generated_at", first["volatile"])

    def test_cli_writes_result_and_report_and_exits_zero_when_every_gate_passes(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "--manifest",
                self._write(tmp, "manifest.json", manifest),
                "--dataset",
                self._write(tmp, "dataset.json", dataset),
                "--routing-evidence",
                self._write(tmp, "routing.json", routing_evidence(manifest)),
                "--execution-context-evidence",
                self._write(tmp, "execution.json", execution_context_evidence(manifest)),
                "--output-dir",
                str(Path(tmp) / "run"),
            ]
            exit_code = cli.main(argv)
            result_path = Path(tmp) / "run" / cli.RESULT_FILENAME
            report_path = Path(tmp) / "run" / cli.REPORT_FILENAME
            self.assertEqual(exit_code, 0)
            self.assertTrue(result_path.exists())
            report = report_path.read_text(encoding="utf-8")
        self.assertIn("REPOSITORY_PREFLIGHT_SATISFIED", report)
        self.assertIn("not production import approval", report)

    def test_cli_exits_non_zero_when_blocked(self):
        dataset = stock_item_dataset()
        manifest = stock_item_manifest(dataset)
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "--manifest",
                self._write(tmp, "manifest.json", manifest),
                "--dataset",
                self._write(tmp, "dataset.json", dataset),
                "--output-dir",
                str(Path(tmp) / "run"),
            ]
            self.assertEqual(cli.main(argv), 1)

    def test_cli_refuses_a_missing_input_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = stock_item_dataset()
            argv = [
                "--manifest",
                str(Path(tmp) / "absent.json"),
                "--dataset",
                self._write(tmp, "dataset.json", dataset),
            ]
            self.assertEqual(cli.main(argv), 2)


if __name__ == "__main__":
    unittest.main()
