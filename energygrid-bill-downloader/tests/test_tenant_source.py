"""Tenant Bill source (#226 V3 G1, #227 binding): HTTP sign-in and list, render binding, PDF check.

Synthetic data only. No network, no browser and no real credential: the HTTP transport, the
credential reader and the renderer are replaced by local fakes.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import zlib
from pathlib import Path

from energygrid_bill_downloader import tenant_source as tenant
from energygrid_bill_downloader.errors import ConfigError, SourceContractError
from energygrid_bill_downloader.invoice import Stream
from energygrid_bill_downloader.naming import display_filename

TENANT_ID = "SYNTHETIC-TENANT-77"
LABEL_NEW = "TN_SYN-2026-08-00002"
LABEL_OLD = "TN_SYN-2026-07-00001"
REPO_ROOT = Path(__file__).resolve().parents[2]


def list_body() -> dict:
    return {
        "svcClaimDto": {"user_id": 501, "username": "synthetic.user", "role_id": 9, "role_name": "tp_user",
                        "perm_request_list": [{"res_label": "ems.billingManager.billList", "operation": "read"}]},
        "request": {"item_kind": "bill", "max_rows_per_page": "5", "current_page": "1", "get_count_only": "false",
                    "filter_value_info": {"tenant_id": TENANT_ID}},
    }


def raw_config(root: Path, **changes) -> dict:
    raw = {
        "schema": tenant.TENANT_CONFIG_SCHEMA,
        "login_url": "https://login.invalid/login",
        "list_url": "https://service.invalid/item/list",
        "portal_type": "synthetic-portal",
        "list_body": list_body(),
        "service_headers": {"X-Synthetic-Service": "synthetic"},
        "render_request_path": "/synthetic/bill-view",
        "credential_path": str(root / "cred.clixml"),
        "browser_config_path": str(root / "browser.json"),
    }
    raw.update(changes)
    return raw


def settings(root: Path) -> tenant.TenantSettings:
    return tenant.load_tenant_settings(raw_config(root), private_path=lambda value, _label: Path(value))


def row(label: str, cycle: str, *, row_id: str = "100", status: str = "released", tenant_id: str = TENANT_ID) -> dict:
    return {"id": row_id, "audit_label": label, "cycle_str": cycle, "lc_status": status, "tenant_id": tenant_id,
            "billed_total_amount": "1.00"}


def list_document(items: list[dict], count=None) -> dict:
    return {"success": True, "data": {"result_key": "item_list_result", "result": {"item_list_result": {
        "item_list": items, "count": str(len(items)) if count is None else count, "list_config": []}}}}


def login_document(*, user_id=501, username="synthetic.user", roles=("9",), error=None, enabled=True) -> dict:
    return {"token": "SYNTHETIC-TOKEN", "error": error, "user_id": None, "status_code": None,
            "user_info": {"id": user_id, "username": username, "enabled": enabled,
                          "role_list": [{"id": role, "name": "tp_user"} for role in roles]}}


class FakeService:
    """Records each POST and answers from a queue of (document, content type)."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, url, payload, *, accept, max_bytes, stage, oversize_ref, count, extra_headers=None):
        count()
        self.calls.append({"url": url, "payload": copy.deepcopy(payload), "stage": stage,
                           "headers": dict(extra_headers or {})})
        document, content_type = self.answers.pop(0)
        return json.dumps(document).encode("utf-8"), content_type


def source(root: Path, *answers, credentials=("synthetic.user", "SYNTHETIC-PASSWORD")):
    service = FakeService(*answers)
    http = tenant.TenantHttpSource(settings(root), lambda: credentials, 5, 1)
    http._post = service
    return http, service


def tounicode_pdf(*texts: str, unmapped_after_first: bool = False) -> bytes:
    """A small Identity-H style PDF: two-byte codes, one ToUnicode map (bfchar and bfrange).

    `unmapped_after_first` puts one code that is not in the map after the first character of each run.
    """
    chars = sorted({char for text in texts for char in text})
    codes = {char: index + 1 for index, char in enumerate(chars)}
    half = len(chars) // 2
    bfchar = "".join(f"<{codes[c]:04X}> <{ord(c):04X}>\n" for c in chars[:half])
    bfrange = "".join(f"<{codes[c]:04X}> <{codes[c]:04X}> <{ord(c):04X}>\n" for c in chars[half:])
    cmap = (f"/CIDInit /ProcSet findresource begin begincmap\n{len(chars[:half])} beginbfchar\n{bfchar}endbfchar\n"
            f"{len(chars[half:])} beginbfrange\n{bfrange}endbfrange\nendcmap end").encode("ascii")

    def run(text: str) -> bytes:
        hex_codes = [f"{codes[c]:04X}" for c in text]
        if unmapped_after_first:
            hex_codes.insert(1, "FFFE")
        return "".join(hex_codes).encode("ascii")

    content = b"BT /F1 9 Tf " + b" ".join(b"[<" + run(text) + b">] TJ" for text in texts) + b" ET"
    parts = [b"%PDF-1.5\n"]
    for number, data in ((1, cmap), (2, content)):
        packed = zlib.compress(data)
        parts.append(b"%d 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % (number, len(packed))
                     + packed + b"\nendstream\nendobj\n")
    parts.append(b"%%EOF\n")
    return b"".join(parts)


class ConfigTests(unittest.TestCase):
    def test_a_closed_config_loads_and_hides_its_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = settings(Path(directory))
            self.assertEqual("synthetic-portal", loaded.portal_type)
            self.assertNotIn("invalid", repr(loaded))
            self.assertNotIn(TENANT_ID, repr(loaded))

    def test_shape_drift_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body_without_tenant = list_body()
            body_without_tenant["request"]["filter_value_info"] = {}
            other_kind = list_body()
            other_kind["request"]["item_kind"] = "meter"
            bad = [
                raw_config(root, schema="energygrid.tenant.v0"),
                {**raw_config(root), "extra": 1},
                raw_config(root, list_body=body_without_tenant),
                raw_config(root, list_body=other_kind),
                raw_config(root, list_body={"request": {}}),
                raw_config(root, portal_type="PAG EMS"),
                raw_config(root, login_url="https://user:secret@login.invalid/login"),
                raw_config(root, service_headers={"Content-Type": "text/plain"}),
                raw_config(root, service_headers={"Bad Name": "x"}),
                raw_config(root, service_headers=["X-A"]),
                raw_config(root, render_request_path="https://host.invalid/x"),
            ]
            for raw in bad:
                with self.subTest(raw=sorted(raw)):
                    with self.assertRaises(ConfigError):
                        tenant.load_tenant_settings(raw, private_path=lambda value, _label: Path(value))

    def test_the_public_example_has_the_closed_shape_and_only_placeholders(self) -> None:
        example = Path(__file__).parents[1] / "config" / "energygrid.tenant.example.json"
        raw = json.loads(example.read_text(encoding="utf-8"))
        loaded = tenant.load_tenant_settings(raw, private_path=lambda value, _label: Path(value))
        self.assertEqual("bill", loaded.list_body["request"]["item_kind"])
        self.assertIn("REPLACE_WITH", loaded.login_url)
        self.assertIn("REPLACE_WITH", loaded.list_body["request"]["filter_value_info"]["tenant_id"])

    def test_private_paths_go_through_the_outside_git_check(self) -> None:
        from energygrid_bill_downloader import native_bridge as bridge

        raw = raw_config(REPO_ROOT / "energygrid-bill-downloader")
        with self.assertRaises(ConfigError):
            tenant.load_tenant_settings(raw, private_path=lambda value, label: bridge._private_path(value, label, REPO_ROOT))


class HttpSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_sign_in_then_one_list_returns_the_released_rows(self) -> None:
        items = [row(LABEL_NEW, "2026-08", row_id="2"), row(LABEL_OLD, "2026-07", row_id="1"),
                 row("TN_SYN-2026-09-00003", "2026-09", row_id="3", status="draft")]
        http, service = source(self.root, (login_document(), "application/json"),
                               (list_document(items), "application/json; charset=utf-8"))
        rows = http.inventory(50)
        self.assertEqual([(LABEL_NEW, "2026-08", "2"), (LABEL_OLD, "2026-07", "1")],
                         [(item.invoice_no, item.cycle, item.row_id) for item in rows])
        login, listing = service.calls
        self.assertEqual(("login", "list"), (login["stage"], listing["stage"]))
        # The password goes to the sign-in call only, and the list carries the portal's service header.
        self.assertEqual("SYNTHETIC-PASSWORD", login["payload"]["password"])
        self.assertEqual({"username", "password", "email", "authProvider", "portal_type"}, set(login["payload"]))
        self.assertNotIn("SYNTHETIC-PASSWORD", json.dumps(listing))
        self.assertEqual({}, login["headers"])
        self.assertEqual({"X-Synthetic-Service": "synthetic"}, listing["headers"])
        # One page large enough for the whole inventory; the count proves completeness.
        self.assertEqual(("1", "51", "false"), tuple(listing["payload"]["request"][key] for key in
                                                      ("current_page", "max_rows_per_page", "get_count_only")))
        self.assertEqual((1, 1), (http.login_calls, http.list_calls))
        self.assertNotIn(LABEL_NEW, repr(rows))

    def test_sign_in_failures_fail_closed_before_any_list(self) -> None:
        cases = [
            (login_document(error="bad credentials"), tenant.REF_LOGIN_REJECTED),
            (login_document(enabled=False), tenant.REF_LOGIN_REJECTED),
            ({**login_document(), "token": ""}, tenant.REF_LOGIN_REJECTED),
            ({**login_document(), "user_info": {"id": "501", "username": "x", "role_list": []}}, tenant.REF_LOGIN_SCHEMA),
            (login_document(user_id=502), tenant.REF_CLAIM_MISMATCH),
            (login_document(username="someone.else"), tenant.REF_CLAIM_MISMATCH),
            (login_document(roles=("8",)), tenant.REF_CLAIM_MISMATCH),
        ]
        for document, ref in cases:
            with self.subTest(ref=ref):
                http, service = source(self.root, (document, "application/json"))
                with self.assertRaises(SourceContractError) as caught:
                    http.inventory(50)
                self.assertEqual(ref, caught.exception.support_ref)
                self.assertEqual(1, len(service.calls), "no list call after a failed sign-in")
                self.assertNotIn("SYNTHETIC-PASSWORD", str(caught.exception))

    def test_list_contract_drift_fails_closed(self) -> None:
        good = row(LABEL_NEW, "2026-08")
        cases = [
            (list_document([good], count="2"), tenant.REF_LIST_INCOMPLETE),
            (list_document([good], count=None) | {"success": False}, tenant.REF_LIST_NOT_SUCCESS),
            ({"success": True, "data": {"result_key": "other", "result": {}}}, tenant.REF_LIST_SCHEMA),
            (list_document([good], count="many"), tenant.REF_LIST_SCHEMA),
            (list_document([row(LABEL_NEW, "2026-08", tenant_id="NEIGHBOUR")]), tenant.REF_LIST_TENANT_MISMATCH),
            (list_document([row("../escape", "2026-08")]), tenant.REF_LIST_ROW_SCHEMA),
            (list_document([row(LABEL_NEW, "2026-13")]), tenant.REF_LIST_ROW_SCHEMA),
            (list_document([row(LABEL_NEW, "2026-08", row_id="2a")]), tenant.REF_LIST_ROW_SCHEMA),
            (list_document([{"id": "1", "audit_label": LABEL_NEW}]), tenant.REF_LIST_ROW_SCHEMA),
            (list_document([]), tenant.REF_LIST_EMPTY),
            (list_document([row(LABEL_NEW, "2026-08", status="draft")]), tenant.REF_LIST_EMPTY),
            (list_document([good, good, good]), tenant.REF_LIST_CEILING),
        ]
        for document, ref in cases:
            with self.subTest(ref=ref):
                http, _service = source(self.root, (login_document(), "application/json"), (document, "application/json"))
                with self.assertRaises(SourceContractError) as caught:
                    http.inventory(2)
                self.assertEqual(ref, caught.exception.support_ref)

    def test_a_non_json_answer_is_refused(self) -> None:
        http, _service = source(self.root, (login_document(), "text/html"))
        with self.assertRaises(SourceContractError) as caught:
            http.inventory(5)
        self.assertEqual(tenant.REF_JSON_INVALID, caught.exception.support_ref)

    def test_the_transport_refuses_to_replace_a_fixed_header(self) -> None:
        http = tenant.TenantHttpSource(settings(self.root), lambda: ("u", "p"), 5, 1)
        with self.assertRaises(ValueError):
            http._attempt("https://service.invalid/x", b"{}", "application/json", 10, "list", "EG_X",
                          {"content-type": "text/plain"})


class FakeRenderer:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.rendered: list[str] = []

    def render(self, item, destination: Path) -> None:
        self.rendered.append(item.row_id)
        with destination.open("xb") as handle:
            handle.write(self.data)


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def adapter(self, pdf: bytes) -> tuple[tenant.TenantBillAdapter, FakeRenderer]:
        items = [row(LABEL_NEW, "2026-08", row_id="2"), row(LABEL_OLD, "2026-07", row_id="1")]
        http, _service = source(self.root, (login_document(), "application/json"), (list_document(items), "application/json"))
        renderer = FakeRenderer(pdf)
        return tenant.TenantBillAdapter(http, renderer), renderer

    def test_candidates_use_the_invoice_number_and_the_billing_month(self) -> None:
        adapter, renderer = self.adapter(tounicode_pdf(LABEL_NEW))
        snapshot = adapter.inventory(50)
        self.assertEqual(Stream.TENANT_BILL, snapshot.stream)
        newest = max(snapshot.candidates, key=lambda item: item.day_ordinal)
        self.assertEqual((f"{LABEL_NEW}.pdf", "2026-08-01"), (newest.source_filename, newest.invoice_date.isoformat()))
        self.assertEqual(f"AUG 2026 - EnergyGrid - Tenant Bill - {LABEL_NEW}.pdf",
                         display_filename(newest.source_filename, newest.invoice_date, stream="TENANT_BILL"))
        self.assertEqual([], renderer.rendered, "listing never renders")

    def test_acquire_renders_the_issued_row_and_checks_its_invoice_number(self) -> None:
        adapter, renderer = self.adapter(tounicode_pdf("TAX INVOICE", LABEL_NEW, "Balance Due"))
        snapshot = adapter.inventory(50)
        newest = max(snapshot.candidates, key=lambda item: item.day_ordinal)
        destination = self.root / "tenant.pdf"
        self.assertEqual(f"{LABEL_NEW}.pdf", adapter.acquire(newest, destination))
        self.assertEqual(["2"], renderer.rendered)

    def test_a_render_of_another_bill_is_refused(self) -> None:
        adapter, _renderer = self.adapter(tounicode_pdf("TAX INVOICE", LABEL_OLD))
        newest = max(adapter.inventory(50).candidates, key=lambda item: item.day_ordinal)
        with self.assertRaises(SourceContractError) as caught:
            adapter.acquire(newest, self.root / "tenant.pdf")
        self.assertEqual(tenant.REF_PDF_INVOICE_MISMATCH, caught.exception.support_ref)

    def test_a_non_pdf_download_is_refused(self) -> None:
        adapter, _renderer = self.adapter(b"<html>not a bill</html>")
        newest = adapter.inventory(50).candidates[0]
        with self.assertRaises(SourceContractError) as caught:
            adapter.acquire(newest, self.root / "tenant.pdf")
        self.assertEqual(tenant.REF_RENDER_DOWNLOAD_UNEXPECTED, caught.exception.support_ref)

    def test_a_handle_from_an_earlier_inventory_is_refused(self) -> None:
        from fixtures.synthetic_dual_stream import candidate

        adapter, renderer = self.adapter(tounicode_pdf(LABEL_NEW))
        adapter.inventory(50)
        foreign = candidate(Stream.TENANT_BILL, name=f"{LABEL_NEW}.pdf", invoice_date="2026-08-01")
        with self.assertRaises(SourceContractError) as caught:
            adapter.acquire(foreign, self.root / "tenant.pdf")
        self.assertEqual(tenant.REF_HANDLE_INVALID, caught.exception.support_ref)
        self.assertEqual([], renderer.rendered)


class ForeignHandleTests(AdapterTests):
    def test_a_row_handle_issued_by_another_inventory_is_refused(self) -> None:
        adapter, renderer = self.adapter(tounicode_pdf(LABEL_NEW))
        other, _other_renderer = self.adapter(tounicode_pdf(LABEL_NEW))
        adapter.inventory(50)
        foreign = max(other.inventory(50).candidates, key=lambda item: item.day_ordinal)
        with self.assertRaises(SourceContractError) as caught:
            adapter.acquire(foreign, self.root / "tenant.pdf")
        self.assertEqual(tenant.REF_HANDLE_INVALID, caught.exception.support_ref)
        self.assertEqual([], renderer.rendered)


class PdfTextTests(unittest.TestCase):
    def test_text_is_read_through_the_tounicode_map(self) -> None:
        data = tounicode_pdf("Invoice No.", LABEL_NEW)
        self.assertTrue(tenant.pdf_shows_invoice(data, LABEL_NEW))
        self.assertFalse(tenant.pdf_shows_invoice(data, LABEL_OLD))

    def test_an_unmapped_code_inside_the_label_breaks_the_match(self) -> None:
        self.assertTrue(tenant.pdf_shows_invoice(tounicode_pdf(LABEL_NEW), LABEL_NEW))
        self.assertFalse(tenant.pdf_shows_invoice(tounicode_pdf(LABEL_NEW, unmapped_after_first=True), LABEL_NEW))

    def test_raw_bytes_or_an_unknown_code_never_match(self) -> None:
        data = tounicode_pdf("X")
        # The label is in the file bytes but not in any decoded text run.
        self.assertFalse(tenant.pdf_shows_invoice(data + LABEL_NEW.encode("ascii"), LABEL_NEW))
        self.assertFalse(tenant.pdf_shows_invoice(b"%PDF-1.5\n%%EOF\n", LABEL_NEW))


class CredentialTests(unittest.TestCase):
    def test_the_pair_is_read_once_in_memory_and_the_path_is_not_on_the_command_line(self) -> None:
        calls = []

        class Completed:
            returncode = 0
            stdout = json.dumps({"u": "synthetic.user", "p": "SYNTHETIC-PASSWORD"}).encode("utf-8")

        def runner(command, **kwargs):
            calls.append((command, kwargs["env"]["EG_TENANT_CREDENTIAL_PATH"]))
            return Completed()

        load = tenant.dpapi_credentials(Path("C:/private/cred.clixml"), runner=runner)
        self.assertEqual(("synthetic.user", "SYNTHETIC-PASSWORD"), load())
        self.assertEqual(("synthetic.user", "SYNTHETIC-PASSWORD"), load())
        self.assertEqual(1, len(calls))
        command, path = calls[0]
        self.assertNotIn("private", " ".join(command))
        self.assertTrue(path.endswith("cred.clixml"))

    def test_a_failed_reader_is_one_closed_reference_without_its_output(self) -> None:
        class Failed:
            returncode = 1
            stdout = b"SECRET-OUTPUT"

        load = tenant.dpapi_credentials(Path("C:/private/cred.clixml"), runner=lambda command, **kwargs: Failed())
        with self.assertRaises(SourceContractError) as caught:
            load()
        self.assertEqual(tenant.REF_CREDENTIAL_UNAVAILABLE, caught.exception.support_ref)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("SECRET", str(caught.exception))


class NativeAdmissionTests(unittest.TestCase):
    """The Tenant adapter through the real v4 latest-only admission, on a database whose Tenant stream
    was initialised UNBOUND (the installed UAT database)."""

    def test_the_latest_released_bill_is_admitted_under_its_billing_month(self) -> None:
        from energygrid_bill_downloader.reconcile import admit_latest_offline_v4
        from energygrid_bill_downloader.state import StateV4Store, migrate_state_database_v4
        from fixtures.synthetic_dual_stream import create_v3_database

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state" / "state.sqlite"
            db.parent.mkdir()
            create_v3_database(db, bound=(Stream.EB_BILL,))
            migrate_state_database_v4(db, apply=True)
            for name in ("archive", "temp"):
                (root / name).mkdir()
            items = [row(LABEL_NEW, "2026-08", row_id="2"), row(LABEL_OLD, "2026-07", row_id="1")]
            http, _service = source(root, (login_document(), "application/json"), (list_document(items), "application/json"))
            renderer = FakeRenderer(tounicode_pdf(LABEL_NEW))
            adapters = {"TENANT_BILL": tenant.TenantBillAdapter(http, renderer)}
            from energygrid_bill_downloader import native_bridge as bridge

            with StateV4Store(db) as state, bridge._session(state, 1_000_000):
                result = admit_latest_offline_v4(adapters=adapters, state=state, archive_root=root / "archive",
                                                 temp_root=root / "temp", run_id=bridge.new_run_id(),
                                                 now_iso="2026-10-15T00:00:01Z", inventory_ceiling=50)
                invoices = state.invoices_for_stream("TENANT_BILL")
            self.assertEqual("ADMITTED", result["TENANT_BILL"]["status"], result)
            self.assertEqual(["2"], renderer.rendered, "only the latest bill is rendered")
            self.assertEqual([f"AUG 2026 - EnergyGrid - Tenant Bill - {LABEL_NEW}.pdf"],
                             [invoice["display_filename"] for invoice in invoices])


if __name__ == "__main__":
    unittest.main()
