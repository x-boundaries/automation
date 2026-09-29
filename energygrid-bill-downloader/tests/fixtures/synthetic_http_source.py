"""Local synthetic direct-HTTP bill service (DL-XB-199 G3-101).

A loopback `ThreadingHTTPServer` that models the frozen LIST / FETCH contract
for one tenant and can inject every drift and transport fault the tests need.
It contacts nothing, uses no real endpoint and carries no real identifier:
every private-looking value here is a synthetic canary the leakage tests
search for in logs, stdout, alerts and exception text.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from tests.fixtures.synthetic_portal import synthetic_pdf


CANARY_TENANT = "CANARY-TENANT-7Q3Z"
CANARY_OTHER_TENANT = "CANARY-TENANT-OTHER-9K1"
CANARY_LIST_PATH = "/canary-private-list-6c1d"
CANARY_FETCH_PATH = "/canary-private-fetch-2b8e"
CANARY_FILENAME_MARK = "CANARYBILL"


def bill_name(index: int) -> str:
    return f"{CANARY_FILENAME_MARK}-{index:03d}.pdf"


def default_files(count: int = 4, tenant: str = CANARY_TENANT) -> list[dict[str, str]]:
    return [
        {"date": f"2026-0{index + 1}-01", "filename": bill_name(index), "tenant_id": tenant}
        for index in range(count)
    ]


# A behaviour is one scripted HTTP response. `None` means the normal contract
# response. Queued behaviours are consumed one per request, then normal.
Behaviour = Callable[["_Handler"], None] | None


@dataclass
class ServiceState:
    tenant: str = CANARY_TENANT
    files: list[dict[str, Any]] = field(default_factory=default_files)
    pdfs: dict[str, bytes] = field(default_factory=dict)
    list_queue: list[Behaviour] = field(default_factory=list)
    fetch_queue: dict[str, list[Behaviour]] = field(default_factory=dict)
    list_requests: list[dict[str, Any]] = field(default_factory=list)
    fetch_requests: list[dict[str, Any]] = field(default_factory=list)
    other_requests: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def pdf_for(self, filename: str) -> bytes:
        return self.pdfs.get(filename) or synthetic_pdf(filename.encode("ascii", "replace"))

    @property
    def fetch_count(self) -> int:
        return len(self.fetch_requests)


class _Handler(BaseHTTPRequestHandler):
    server_version = "SyntheticBillService/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # silence stderr
        return

    @property
    def service(self) -> ServiceState:
        return self.server.service  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - http.server hook
        self.service.other_requests.append("GET " + self.path)
        self.send_bytes(405, b"", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - http.server hook
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {"_unparsable": True}
        service = self.service
        with service.lock:
            if self.path == CANARY_LIST_PATH:
                service.list_requests.append(body)
                behaviour = service.list_queue.pop(0) if service.list_queue else None
            elif self.path == CANARY_FETCH_PATH:
                service.fetch_requests.append(body)
                queue = service.fetch_queue.get(body.get("filename"), [])
                behaviour = queue.pop(0) if queue else None
            else:
                service.other_requests.append("POST " + self.path)
                behaviour = status(404)
        if behaviour is not None:
            behaviour(self)
            return
        if self.path == CANARY_LIST_PATH:
            if body != {"tenant_id": service.tenant}:
                self.send_json({"message": "Invalid tenant"})
                return
            self.send_json({"success": True, "files": service.files})
            return
        if set(body) != {"filename", "tenant_id"} or body["tenant_id"] != service.tenant:
            self.send_bytes(400, b"bad request", "text/plain")
            return
        if body["filename"] not in {row.get("filename") for row in service.files}:
            self.send_bytes(404, b"not found", "text/plain")
            return
        self.send_bytes(200, service.pdf_for(body["filename"]), "application/pdf")

    # ---------------------------------------------------------------- output

    def send_bytes(self, code: int, payload: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, document: Any, code: int = 200, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_bytes(code, json.dumps(document).encode("utf-8"), content_type)


# ------------------------------------------------------------ behaviours


def status(code: int, location: str | None = None) -> Behaviour:
    def act(handler: _Handler) -> None:
        extra = {"Location": location} if location else None
        handler.send_bytes(code, b"synthetic", "text/plain", extra)

    return act


def json_document(document: Any, content_type: str = "application/json") -> Behaviour:
    def act(handler: _Handler) -> None:
        handler.send_json(document, content_type=content_type)

    return act


def raw_body(payload: bytes, content_type: str = "application/json") -> Behaviour:
    def act(handler: _Handler) -> None:
        handler.send_bytes(200, payload, content_type)

    return act


def drop_connection() -> Behaviour:
    def act(handler: _Handler) -> None:
        handler.close_connection = True
        try:
            handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    return act


def slow(seconds: float) -> Behaviour:
    def act(handler: _Handler) -> None:
        time.sleep(seconds)
        handler.close_connection = True

    return act


def short_body(payload: bytes, declared: int, content_type: str = "application/pdf") -> Behaviour:
    """Declare `declared` bytes, send fewer, then close: a truncated transfer."""

    def act(handler: _Handler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(declared))
        handler.end_headers()
        handler.wfile.write(payload)
        handler.wfile.flush()
        handler.close_connection = True
        try:
            handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    return act


def close_delimited(payload: bytes, content_type: str = "application/pdf") -> Behaviour:
    """No Content-Length and no chunking: completeness is unprovable."""

    def act(handler: _Handler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(payload)
        handler.close_connection = True

    return act


def pdf_bytes(payload: bytes) -> Behaviour:
    def act(handler: _Handler) -> None:
        handler.send_bytes(200, payload, "application/pdf")

    return act


class SyntheticBillService:
    def __init__(self, state: ServiceState | None = None) -> None:
        self.state = state or ServiceState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.service = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "SyntheticBillService":
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def list_url(self) -> str:
        return self.base + CANARY_LIST_PATH

    @property
    def fetch_url(self) -> str:
        return self.base + CANARY_FETCH_PATH


class AlertSink:
    """Loopback alert ingress double: records every payload it receives."""

    def __init__(self, respond: int = 200) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.respond = respond
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                sink.payloads.append(json.loads(self.rfile.read(length)))
                sink.headers.append(dict(self.headers.items()))
                self.send_response(sink.respond)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "AlertSink":
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/canary-private-alert-hook-5f0a"
