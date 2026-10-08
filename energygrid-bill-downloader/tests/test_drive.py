"""#226 G3 n8n Drive boundary: request contract, strict result parsing and the
core-owned receipt verdict. The filesystem DriveStager is retired; its earlier
behaviour remains only as historical v2 evidence (frozen by schema v3)."""

from __future__ import annotations

import hashlib
import json
import unittest

from energygrid_bill_downloader import drive as drive_module
from energygrid_bill_downloader.drive import (
    DriveClient,
    build_multipart,
    destination_request,
    file_md5,
    operation_request,
    parse_result,
    validate_request,
    verify_result,
)
from energygrid_bill_downloader.errors import StateError
from energygrid_bill_downloader.state import canonical_json, drive_app_properties, drive_binding_id

from fixtures.synthetic_dual_stream import SYNTHETIC_ACCOUNT, SYNTHETIC_FOLDERS, SYNTHETIC_ROOT, synthetic_drive_settings

PDF = b"%PDF-1.4\n% synthetic drive payload\n%%EOF\n"
OPERATION_ID = "11111111-2222-4333-8444-555555555555"
INVOICE_ID = "66666666-7777-5888-9999-000000000000"
RESERVED = "synthReservedFile00001"


def operation(**overrides) -> dict:
    binding_id = drive_binding_id("EB_BILL", SYNTHETIC_ACCOUNT, SYNTHETIC_ROOT, SYNTHETIC_FOLDERS["EB_BILL"])
    sha = hashlib.sha256(PDF).hexdigest()
    row = {
        "operation_id": OPERATION_ID, "invoice_id": INVOICE_ID, "binding_id": binding_id, "stream": "EB_BILL",
        "folder_id": SYNTHETIC_FOLDERS["EB_BILL"], "remote_name": "2026-10-01.pdf",
        "local_byte_size": len(PDF), "local_sha256": sha, "local_md5": file_md5(PDF),
        "app_properties_json": canonical_json(drive_app_properties(
            binding_id=binding_id, invoice_id=INVOICE_ID, operation_id=OPERATION_ID, local_sha256=sha, stream="EB_BILL")),
        "reserved_remote_file_id": RESERVED,
    }
    row.update(overrides)
    return row


def resource(op: dict, **overrides) -> dict:
    item = {
        "id": op["reserved_remote_file_id"], "name": op["remote_name"], "mimeType": "application/pdf",
        "parents": [op["folder_id"]], "size": str(op["local_byte_size"]), "md5Checksum": op["local_md5"],
        "sha256Checksum": op["local_sha256"], "appProperties": json.loads(op["app_properties_json"]), "trashed": False,
    }
    item.update(overrides)
    return item


def result(mode: str, outcome: str, *, candidates=(), lookup="FOUND", upload="NOT_ATTEMPTED", folder="PASS",
           complete=True, generated=None, account=None) -> dict:
    return {
        "schema": "energygrid.drive_result.v3", "mode": mode, "operation_id": OPERATION_ID, "outcome": outcome,
        "generated_id": generated, "folder_check": folder, "reserved_lookup": lookup, "search_complete": complete,
        "upload_result": upload, "candidates": list(candidates), "account_ref": account, "support_ref": "EG_DRIVE_RESULT",
    }


def encode(document: dict) -> bytes:
    return json.dumps(document).encode("utf-8")


class RequestContractTests(unittest.TestCase):
    def test_app_properties_are_exactly_seven_ascii_keys_within_google_limits(self) -> None:
        props = json.loads(operation()["app_properties_json"])
        self.assertEqual({"egApp", "egSchema", "egStream", "egInv", "egBnd", "egOp", "egSha"}, set(props))
        for key, value in props.items():
            self.assertTrue((key + value).isascii())
            self.assertLessEqual(len((key + value).encode("utf-8")), 124)

    def test_binding_id_is_deterministic_and_38_characters(self) -> None:
        value = drive_binding_id("EB_BILL", SYNTHETIC_ACCOUNT, SYNTHETIC_ROOT, SYNTHETIC_FOLDERS["EB_BILL"])
        self.assertEqual(38, len(value))
        self.assertTrue(value.startswith("egdb3-"))
        self.assertNotEqual(value, drive_binding_id("TENANT_BILL", SYNTHETIC_ACCOUNT, SYNTHETIC_ROOT, SYNTHETIC_FOLDERS["EB_BILL"]))

    def test_reserve_and_reconcile_never_carry_a_pdf_and_upload_carries_exact_bytes(self) -> None:
        op = operation()
        reserve = operation_request("RESERVE_ID", operation(reserved_remote_file_id=None), SYNTHETIC_ROOT)
        self.assertIsNone(reserve["reserved_file_id"])
        validate_request(reserve, None)
        with self.assertRaises(StateError):
            validate_request(reserve, PDF)
        reconcile = operation_request("RECONCILE", op, SYNTHETIC_ROOT)
        validate_request(reconcile, None)
        with self.assertRaises(StateError):
            validate_request(reconcile, PDF)
        upload = operation_request("UPLOAD_IF_ABSENT", op, SYNTHETIC_ROOT)
        self.assertEqual(RESERVED, upload["reserved_file_id"])
        validate_request(upload, PDF)
        for wrong in (PDF + b"x", PDF[:-1], b"not a pdf"):
            with self.subTest(size=len(wrong)), self.assertRaises(StateError):
                validate_request(upload, wrong)

    def test_upload_and_reconcile_require_the_reserved_id(self) -> None:
        with self.assertRaises(StateError):
            operation_request("UPLOAD_IF_ABSENT", operation(reserved_remote_file_id=None), SYNTHETIC_ROOT)
        with self.assertRaises(StateError):
            operation_request("RECONCILE", operation(reserved_remote_file_id=None), SYNTHETIC_ROOT)

    def test_tampered_request_fields_are_rejected(self) -> None:
        base = operation_request("UPLOAD_IF_ABSENT", operation(), SYNTHETIC_ROOT)
        mutations = {
            "schema": "energygrid.drive_request.v2", "mode": "DELETE", "stream": "OTHER",
            "folder_id": "../x", "reserved_file_id": "short", "local_byte_size": 0,
        }
        for key, value in mutations.items():
            with self.subTest(key=key), self.assertRaises(StateError):
                validate_request({**base, key: value}, PDF)
        props = dict(base["app_properties"], egOp="00000000-0000-4000-8000-000000000000")
        with self.assertRaises(StateError):
            validate_request({**base, "app_properties": props}, PDF)
        with self.assertRaises(StateError):
            validate_request({**base, "extra": 1}, PDF)

    def test_destination_request_is_read_only_shape(self) -> None:
        settings = synthetic_drive_settings()
        request = destination_request("EB_BILL", settings.bindings["EB_BILL"])
        validate_request(request, None)
        self.assertEqual("RESOLVE_DESTINATION", request["mode"])
        self.assertIsNone(request["reserved_file_id"])

    def test_multipart_contains_metadata_and_only_the_upload_pdf(self) -> None:
        upload = operation_request("UPLOAD_IF_ABSENT", operation(), SYNTHETIC_ROOT)
        body, content_type = build_multipart(upload, PDF)
        self.assertIn(b'name="metadata"', body)
        self.assertIn(b'name="pdf"', body)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        body, _ = build_multipart(operation_request("RECONCILE", operation(), SYNTHETIC_ROOT), None)
        self.assertNotIn(b'name="pdf"', body)


class ResultParsingTests(unittest.TestCase):
    def metadata(self, mode="UPLOAD_IF_ABSENT"):
        return operation_request(mode, operation(), SYNTHETIC_ROOT)

    def test_valid_result_parses_and_status_must_match_outcome(self) -> None:
        document = result("UPLOAD_IF_ABSENT", "UPLOADED", candidates=[resource(operation())], upload="CREATED")
        self.assertIsNotNone(parse_result(200, encode(document), self.metadata()))
        self.assertIsNone(parse_result(503, encode(document), self.metadata()))

    def test_malformed_results_are_never_parsed(self) -> None:
        good = result("UPLOAD_IF_ABSENT", "FOUND_EXACT", candidates=[resource(operation())])
        variants = {
            "extra key": {**good, "extra": 1},
            "wrong schema": {**good, "schema": "energygrid.drive_result.v2"},
            "wrong operation": {**good, "operation_id": "00000000-0000-4000-8000-000000000000"},
            "wrong mode": {**good, "mode": "RECONCILE"},
            "bad support": {**good, "support_ref": "private text"},
            "too many candidates": {**good, "candidates": [resource(operation(), id=f"synthCandidate{index:06d}") for index in range(6)]},
            "duplicate candidate": {**good, "candidates": [resource(operation()), resource(operation())]},
            "candidate extra key": {**good, "candidates": [{**resource(operation()), "webViewLink": "x"}]},
            "generated outside reserve": {**good, "generated_id": "synthGenerated000001"},
        }
        for name, document in variants.items():
            with self.subTest(name=name):
                self.assertIsNone(parse_result(200, encode(document), self.metadata()))
        self.assertIsNone(parse_result(200, b"{" + b" " * 9000 + b"}", self.metadata()))
        self.assertIsNone(parse_result(200, b'{"schema":1,"schema":2}', self.metadata()))

    def test_reconcile_result_claiming_an_upload_is_a_contract_violation(self) -> None:
        metadata = self.metadata("RECONCILE")
        for upload, outcome in (("CREATED", "FOUND_EXACT"), ("NOT_ATTEMPTED", "UPLOADED")):
            with self.subTest(upload=upload, outcome=outcome):
                document = result("RECONCILE", outcome, candidates=[resource(operation())], upload=upload)
                self.assertIsNone(parse_result(200, encode(document), metadata))

    def test_reserve_result_requires_a_valid_generated_id(self) -> None:
        metadata = operation_request("RESERVE_ID", operation(reserved_remote_file_id=None), SYNTHETIC_ROOT)
        ok = result("RESERVE_ID", "ID_RESERVED", lookup="NOT_REQUESTED", complete=False, generated="synthGenerated000001")
        self.assertIsNotNone(parse_result(200, encode(ok), metadata))
        for generated in (None, "bad id!", "x" * 200):
            with self.subTest(generated=generated):
                self.assertIsNone(parse_result(200, encode({**ok, "generated_id": generated}), metadata))


class VerdictTests(unittest.TestCase):
    def verdict(self, document):
        return verify_result(operation(), document)

    def test_exact_sha256_receipt_binds_the_reserved_id(self) -> None:
        op = operation()
        verdict = verify_result(op, result("UPLOAD_IF_ABSENT", "UPLOADED", candidates=[resource(op)], upload="CREATED"))
        self.assertEqual("FOUND_EXACT", verdict.kind)
        self.assertEqual(RESERVED, verdict.receipt["remote_file_id"])
        self.assertEqual("SHA256", verdict.receipt["verification_method"])

    def test_md5_size_fallback_and_never_size_only(self) -> None:
        op = operation()
        md5_only = resource(op, sha256Checksum=None)
        self.assertEqual("MD5_SIZE", verify_result(op, result("RECONCILE", "FOUND_EXACT", candidates=[md5_only])).receipt["verification_method"])
        neither = resource(op, sha256Checksum=None, md5Checksum=None)
        verdict = verify_result(op, result("RECONCILE", "CHECKSUM_UNAVAILABLE", candidates=[neither]))
        self.assertEqual(("CHECKSUM_UNAVAILABLE", "EG_DRIVE_VERIFICATION_UNAVAILABLE"), (verdict.kind, verdict.support_ref))
        self.assertIsNone(verdict.receipt)

    def test_conflict_matrix(self) -> None:
        op = operation()
        cases = {
            "EG_DRIVE_IDENTITY_BYTES_MISMATCH": [resource(op, size=str(len(PDF) + 1)), resource(op, sha256Checksum="0" * 64),
                                                 resource(op, md5Checksum="0" * 32), resource(op, size="01")],
            "EG_DRIVE_IDENTITY_TRASHED": [resource(op, trashed=True)],
            "EG_DRIVE_IDENTITY_MOVED": [resource(op, parents=["synthElsewhere000001"]), resource(op, parents=[op["folder_id"], "synthElsewhere000001"])],
            "EG_DRIVE_IDENTITY_OP_MISMATCH": [resource(op, appProperties={}), resource(op, appProperties=None)],
            "EG_DRIVE_NAME_MISMATCH": [resource(op, name="2026-10-01 (1).pdf")],
            "EG_DRIVE_MIME_MISMATCH": [resource(op, mimeType="application/x-pdf")],
        }
        for expected, items in cases.items():
            for item in items:
                with self.subTest(expected=expected, item=item):
                    verdict = verify_result(op, result("RECONCILE", "CONFLICT", candidates=[item]))
                    self.assertEqual(("CONFLICT", expected), (verdict.kind, verdict.support_ref))

    def test_any_other_file_id_is_a_conflict_never_adoption(self) -> None:
        op = operation()
        same_identity_other_id = resource(op, id="synthOtherFile000001")
        verdict = verify_result(op, result("RECONCILE", "CONFLICT", candidates=[same_identity_other_id], lookup="NOT_FOUND"))
        self.assertEqual(("CONFLICT", "EG_DRIVE_REMOTE_ID_MISMATCH"), (verdict.kind, verdict.support_ref))
        both = [resource(op), resource(op, id="synthOtherFile000001", appProperties={})]
        verdict = verify_result(op, result("RECONCILE", "CONFLICT", candidates=both))
        self.assertEqual(("CONFLICT", "EG_DRIVE_NAME_OCCUPIED"), (verdict.kind, verdict.support_ref))

    def test_not_found_requires_absent_reserved_id_complete_searches_and_folder_pass(self) -> None:
        op = operation()
        self.assertEqual("NOT_FOUND", verify_result(op, result("RECONCILE", "NOT_FOUND", lookup="NOT_FOUND")).kind)
        for kwargs in ({"complete": False}, {"folder": "UNAVAILABLE"}, {"lookup": "UNAVAILABLE"}):
            with self.subTest(kwargs=kwargs):
                outcome = "DRIVE_UNAVAILABLE"
                self.assertEqual("UNAVAILABLE", verify_result(op, result("RECONCILE", outcome, **{"lookup": "NOT_FOUND", **kwargs})).kind)
        self.assertEqual("DESTINATION_CHANGED", verify_result(op, result("RECONCILE", "DESTINATION_CHANGED", lookup="NOT_FOUND", folder="FAIL")).kind)

    def test_http_200_with_disagreeing_n8n_outcome_is_unavailable(self) -> None:
        op = operation()
        for outcome, candidates, lookup in (
            ("FOUND_EXACT", [resource(op, trashed=True)], "FOUND"),
            ("NOT_FOUND", [resource(op)], "FOUND"),
            ("UPLOADED", [], "NOT_FOUND"),
        ):
            with self.subTest(outcome=outcome):
                verdict = verify_result(op, result("UPLOAD_IF_ABSENT", outcome, candidates=candidates, lookup=lookup))
                self.assertEqual(("UNAVAILABLE", "EG_DRIVE_RESULT_DISAGREES"), (verdict.kind, verdict.support_ref))
        self.assertEqual("UNAVAILABLE", verify_result(op, None).kind)

    def test_reserved_lookup_found_without_the_reserved_candidate_is_inconsistent(self) -> None:
        op = operation()
        verdict = verify_result(op, result("RECONCILE", "FOUND_EXACT", candidates=[], lookup="FOUND"))
        self.assertEqual("UNAVAILABLE", verdict.kind)


class ClientTests(unittest.TestCase):
    def test_missing_or_injected_token_fails_before_any_post(self) -> None:
        calls = []
        settings = synthetic_drive_settings()
        for environ in ({}, {"ENERGYGRID_DRIVE_TOKEN": ""}, {"ENERGYGRID_DRIVE_TOKEN": "a\r\nX-Evil: 1"}):
            client = DriveClient(settings, post_once=lambda *a, **k: calls.append(a), environ=environ)
            with self.subTest(environ=list(environ)), self.assertRaises(StateError):
                client.prepare(operation_request("RECONCILE", operation(), SYNTHETIC_ROOT), None)
        self.assertEqual([], calls)

    def test_transport_exceptions_become_no_valid_result(self) -> None:
        def boom(*args, **kwargs):
            raise TimeoutError("synthetic")

        client = DriveClient(synthetic_drive_settings(), post_once=boom, environ={"ENERGYGRID_DRIVE_TOKEN": "t"})
        metadata = operation_request("RECONCILE", operation(), SYNTHETIC_ROOT)
        self.assertIsNone(client.send(client.prepare(metadata, None), metadata))

    def test_no_proxy_no_redirect_opener_and_no_google_credential_in_module(self) -> None:
        source = open(drive_module.__file__, encoding="utf-8").read()
        self.assertIn("ProxyHandler({})", source)
        self.assertIn("_NoRedirect", source)
        for forbidden in ("googleapis.com", "refresh_token", "client_secret", "access_token"):
            self.assertNotIn(forbidden, source.lower())

    def test_retired_filesystem_stager_is_gone(self) -> None:
        self.assertFalse(hasattr(drive_module, "DriveStager"))


if __name__ == "__main__":
    unittest.main()
