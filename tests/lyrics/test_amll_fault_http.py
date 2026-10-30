"""Actual local HTTP responses exercise the provider/queue classification seam."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from word_lyrics_automation.faults import classify_exception
from word_lyrics_automation.providers.amll import AMLLClient


class AMLLFaultHTTPTests(unittest.TestCase):
    def server(self, responses):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                index = min(len(calls), len(responses) - 1)
                status, body, headers = responses[index]
                calls.append(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        return "http://127.0.0.1:" + str(server.server_port), calls

    def test_real_503_is_preserved_for_durable_retry_then_200_succeeds(self):
        url, calls = self.server([(503, b"temporary", {}), (200, b'{"data":[]}', {})])
        client = AMLLClient(base_url=url, attempts=1)
        with self.assertRaises(urllib.error.HTTPError) as context:
            client._request_json("/fixture", {"accesskey": "private-test-key"})
        fault = classify_exception(context.exception, provider="amll")
        self.assertEqual((fault.category, fault.http_status, fault.retryable), ("transport", 503, True))
        self.assertNotIn("private-test-key", json.dumps(fault.as_dict()))
        self.assertEqual(client._request_json("/fixture", {}), {"data": []})
        self.assertEqual(len(calls), 2)

    def test_short_503_retry_is_finite_and_has_jitter_before_real_success(self):
        url, calls = self.server([(503, b"temporary", {}), (200, b'{"data":[]}', {})])
        sleeps = []
        client = AMLLClient(base_url=url, attempts=2, sleeper=sleeps.append)
        self.assertEqual(client._request_json("/fixture", {}), {"data": []})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(1 <= sleeps[0] <= 1.2)

    def test_real_401_does_not_retry_or_hide_authentication_status(self):
        url, calls = self.server([(401, b"unauthorized", {}), (200, b'{"data":[]}', {})])
        sleeps = []
        client = AMLLClient(base_url=url, attempts=4, sleeper=sleeps.append)
        with self.assertRaises(urllib.error.HTTPError) as context:
            client._request_json("/fixture", {})
        fault = classify_exception(context.exception, provider="amll")
        self.assertEqual((fault.category, fault.retryable), ("authentication", False))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_real_bad_json_is_not_retried_as_transport(self):
        url, calls = self.server([(200, b"{broken-json", {}), (200, b'{"data":[]}', {})])
        sleeps = []
        client = AMLLClient(base_url=url, attempts=4, sleeper=sleeps.append)
        with self.assertRaises(json.JSONDecodeError) as context:
            client._request_json("/fixture", {})
        self.assertEqual(classify_exception(context.exception).category, "parse")
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_long_retry_after_goes_to_durable_scheduler_without_early_provider_retry(self):
        url, calls = self.server([(429, b"limited", {"Retry-After": "120"})])
        sleeps = []
        client = AMLLClient(base_url=url, attempts=4, sleeper=sleeps.append)
        with self.assertRaises(urllib.error.HTTPError) as context:
            client._request_json("/fixture", {})
        self.assertEqual(classify_exception(context.exception).retry_after_seconds, 120)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
