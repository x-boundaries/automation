"""Loopback n8n client (#226 V3 G1): offline tests against a throwaway HTTP server on 127.0.0.1.

No n8n instance is contacted. The server records what the client actually sends, so the tests prove the
fixed host and paths, the Header Auth, no proxy, no redirect, one request per call and bounded answers.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from energygrid_bill_downloader import n8n_loopback as loopback

TOKEN = "A" * 40
HEADER = "X-EnergyGrid-Monitor-Key"


class Recorder(BaseHTTPRequestHandler):
    behaviour = "OK"
    requests: list[dict] = []

    def log_message(self, *args) -> None:  # keep test output quiet
        pass

    def _reply(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        Recorder.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
        self._reply(200 if self.path == "/healthz" else 404, b'{"status":"ok"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        Recorder.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers),
                                  "body": json.loads(body)})
        behaviour = Recorder.behaviour
        if behaviour == "REDIRECT":
            self._reply(302, b"", {"Location": "http://example.com/elsewhere"})
        elif behaviour == "LARGE":
            self._reply(200, b"[" + b"0," * (loopback.MAX_ANSWER_BYTES // 2) + b"0]")
        elif behaviour == "SLOW":
            time.sleep(2)
            self._reply(200, b"{}")
        elif behaviour == "422":
            self._reply(422, b'{"outcome":"REFUSED"}')
        else:
            self._reply(200, b'{"outcome":"SENT"}')


class LoopbackClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        Recorder.requests = []
        Recorder.behaviour = "OK"
        self._tmp = tempfile.TemporaryDirectory()
        self.token = Path(self._tmp.name) / "token.txt"
        self.token.write_text(TOKEN + "\n", encoding="utf-8")
        self.client = loopback.LoopbackClient(self.port, self.token, HEADER)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_authenticated_post_to_the_fixed_loopback_path(self) -> None:
        answer = self.client.post(loopback.EMAIL_PATH, {"b": 1, "a": 2}, 5)
        self.assertEqual((loopback.OK, 200, {"outcome": "SENT"}), (answer.outcome, answer.status, answer.body))
        self.assertEqual(1, len(Recorder.requests))
        sent = Recorder.requests[0]
        self.assertEqual(("POST", loopback.EMAIL_PATH), (sent["method"], sent["path"]))
        # HTTP header names are case-insensitive; urllib normalises their case on the wire.
        headers = {key.lower(): value for key, value in sent["headers"].items()}
        self.assertEqual(TOKEN, headers[HEADER.lower()])
        self.assertEqual("application/json", headers["content-type"])
        self.assertEqual({"a": 2, "b": 1}, sent["body"])

    def test_only_the_two_fixed_paths_are_allowed(self) -> None:
        with self.assertRaises(ValueError):
            self.client.post("/webhook/anything-else", {}, 5)
        self.assertEqual([], Recorder.requests)

    def test_environment_proxies_are_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
                                          "NO_PROXY": "", "no_proxy": ""}):
            client = loopback.LoopbackClient(self.port, self.token, HEADER)
            answer = client.post(loopback.VERIFY_PATH, {}, 5)
        self.assertEqual(loopback.OK, answer.outcome)
        self.assertEqual(1, len(Recorder.requests))

    def test_a_redirect_is_never_followed(self) -> None:
        Recorder.behaviour = "REDIRECT"
        answer = self.client.post(loopback.VERIFY_PATH, {}, 5)
        self.assertEqual((loopback.HTTP_STATUS, 302), (answer.outcome, answer.status))
        self.assertEqual(1, len(Recorder.requests))

    def test_non_2xx_is_an_answer_not_an_uncertainty(self) -> None:
        Recorder.behaviour = "422"
        answer = self.client.post(loopback.EMAIL_PATH, {}, 5)
        self.assertEqual((loopback.HTTP_STATUS, 422, {"outcome": "REFUSED"}), (answer.outcome, answer.status, answer.body))

    def test_an_oversized_answer_is_bounded(self) -> None:
        Recorder.behaviour = "LARGE"
        answer = self.client.post(loopback.VERIFY_PATH, {}, 5)
        self.assertEqual(("EG_N8N_ANSWER_TOO_LARGE", None), (answer.support_ref, answer.body))

    def test_a_timeout_is_uncertain_and_never_retried(self) -> None:
        Recorder.behaviour = "SLOW"
        answer = self.client.post(loopback.EMAIL_PATH, {}, 1)
        self.assertEqual(loopback.SENT_UNCERTAIN, answer.outcome)
        self.assertEqual(1, len(Recorder.requests))

    def test_connection_refused_is_not_sent(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        # Windows retries a refused loopback connect for about two seconds; within a short timeout that is
        # (conservatively) uncertain, and given time it is a definite refusal.
        answer = loopback.LoopbackClient(free_port, self.token, HEADER).post(loopback.EMAIL_PATH, {}, 15)
        self.assertEqual((loopback.NOT_SENT, "EG_N8N_UNREACHABLE"), (answer.outcome, answer.support_ref))

    def test_a_missing_or_malformed_token_sends_nothing(self) -> None:
        for content in (None, "short", "has space " + "x" * 40):
            with self.subTest(content=content):
                Recorder.requests = []
                if content is None:
                    self.token.unlink(missing_ok=True)
                else:
                    self.token.write_text(content, encoding="utf-8")
                answer = self.client.post(loopback.EMAIL_PATH, {}, 5)
                self.assertEqual((loopback.NOT_SENT, "EG_N8N_TOKEN_UNAVAILABLE"), (answer.outcome, answer.support_ref))
                self.assertEqual([], Recorder.requests)

    def test_health_is_an_unauthenticated_get(self) -> None:
        self.assertTrue(self.client.health())
        self.assertEqual(("GET", "/healthz"), (Recorder.requests[0]["method"], Recorder.requests[0]["path"]))
        self.assertNotIn(HEADER.lower(), {key.lower() for key in Recorder.requests[0]["headers"]})

    def test_the_host_is_fixed_to_ipv4_loopback(self) -> None:
        self.assertEqual("127.0.0.1", loopback.HOST)
        with self.assertRaises(ValueError):
            loopback.LoopbackClient(0, self.token, HEADER)


if __name__ == "__main__":
    unittest.main()
