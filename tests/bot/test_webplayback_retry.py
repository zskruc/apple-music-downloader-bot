from __future__ import annotations

import ast
import copy
import os
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch


class HTTPError(Exception):
    pass


class TransportError(HTTPError):
    pass


class ConnectError(TransportError):
    pass


class ReadTimeout(TransportError):
    pass


class ProxyError(TransportError):
    pass


class HTTPStatusError(HTTPError):
    def __init__(self, response):
        super().__init__("response body and URL credentials must not be logged")
        self.response = response


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = {"songList": [{"id": "1440863510"}]} if payload is None else payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPStatusError(self)

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return copy.deepcopy(self.payload)


class WebPlaybackRetryTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.clients = []
        self.requests = []
        self.client_options = []
        self.sleep = Mock()
        self.log = Mock()
        self.m3u8 = Mock(return_value="https://audio.example.test/playlist.m3u8")
        self.auth = Mock(return_value=("music-secret", "developer-secret"))
        test = self

        class Client:
            def __init__(self, **options):
                self.closed = False
                test.clients.append(self)
                test.client_options.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True
                return False

            def post(self, url, **options):
                test.requests.append((url, options))
                event = test.events.pop(0)
                if isinstance(event, BaseException):
                    raise event
                return event

        source = Path(__file__).resolve().parents[2] / "bot" / "wrapper_v2_adapter.py"
        tree = ast.parse(source.read_text())
        selected = [
            node for node in tree.body
            if getattr(node, "name", None) in {
                "PlaybackUnavailableError", "playback_unavailable_error",
                "get_webplayback", "combined_playback",
            }
        ]
        httpx = types.SimpleNamespace(
            Client=Client, Timeout=lambda value: value,
            HTTPError=HTTPError, TransportError=TransportError, HTTPStatusError=HTTPStatusError,
        )
        self.namespace = {
            "Any": Any, "httpx": httpx,
            "time": types.SimpleNamespace(sleep=self.sleep), "log": self.log,
            "APPLE_MUSIC_HOME": "https://music.apple.com",
            "APPLE_LANGUAGE": "zh-CN",
            "WEBPLAYBACK_URL": "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback",
            "get_legacy_m3u8": self.m3u8, "get_auth_tokens": self.auth,
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), self.namespace)
        self.get_webplayback = self.namespace["get_webplayback"]
        self.combined_playback = self.namespace["combined_playback"]
        self.unavailable_error = self.namespace["PlaybackUnavailableError"]

    def call(self, *events):
        self.events.extend(events)
        return self.get_webplayback("1440863510", "music-secret", "developer-secret")

    def test_connect_error_then_success_retries_only_http_and_closes_clients(self):
        result = self.call(ConnectError("connection failed"), Response())
        self.assertEqual(result["songList"][0]["id"], "1440863510")
        self.assertEqual(len(self.requests), 2)
        self.sleep.assert_called_once_with(2)
        self.assertTrue(all(client.closed for client in self.clients))
        self.m3u8.assert_not_called()

    def test_read_timeout_and_other_transport_errors_are_bounded(self):
        for exception_type in (ReadTimeout, ProxyError, TransportError):
            with self.subTest(exception=exception_type.__name__):
                self.events = [exception_type(), exception_type(), exception_type()]
                before = len(self.requests)
                self.sleep.reset_mock()
                with self.assertRaises(exception_type):
                    self.get_webplayback("1440863510", "secret", "secret")
                self.assertEqual(len(self.requests) - before, 3)
                self.assertEqual([call.args for call in self.sleep.call_args_list], [(2,), (4,)])

    def test_retryable_http_statuses_recover(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.events = [Response(status), Response()]
                before = len(self.requests)
                self.sleep.reset_mock()
                self.get_webplayback("1440863510", "secret", "secret")
                self.assertEqual(len(self.requests) - before, 2)
                self.sleep.assert_called_once_with(2)

    def test_http_retry_exhaustion_preserves_final_status_and_exception(self):
        with self.assertRaises(HTTPStatusError) as caught:
            self.call(Response(503), Response(502), Response(504))
        self.assertEqual(caught.exception.response.status_code, 504)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([call.args for call in self.sleep.call_args_list], [(2,), (4,)])

    def test_authentication_and_other_client_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 451):
            with self.subTest(status=status):
                self.events = [Response(status)]
                before = len(self.requests)
                self.sleep.reset_mock()
                with self.assertRaises(HTTPStatusError) as caught:
                    self.get_webplayback("1440863510", "secret", "secret")
                self.assertEqual(caught.exception.response.status_code, status)
                self.assertEqual(len(self.requests) - before, 1)
                self.sleep.assert_not_called()

    def test_3076_unavailable_semantics_are_preserved_without_retry(self):
        payload = {"failureType": "3076", "m-allowed": False, "customerMessage": "unavailable"}
        with self.assertRaises(self.unavailable_error) as caught:
            self.call(Response(payload=payload))
        self.assertEqual(caught.exception.failure_type, "3076")
        self.assertEqual(len(self.requests), 1)
        self.sleep.assert_not_called()

    def test_dialog_and_json_errors_are_not_retried(self):
        for payload in ({"dialog": {"message": "sign in"}}, ["invalid root"], ValueError("invalid JSON")):
            with self.subTest(payload_type=type(payload).__name__):
                self.events = [Response(payload=payload)]
                before = len(self.requests)
                self.sleep.reset_mock()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.get_webplayback("1440863510", "secret", "secret")
                self.assertEqual(len(self.requests) - before, 1)
                self.sleep.assert_not_called()

    def test_combined_playback_calls_m3u8_once_after_webplayback_recovers(self):
        self.events = [ConnectError(), ReadTimeout(), Response()]
        result = self.combined_playback("1440863510")
        self.assertEqual(len(self.requests), 3)
        self.auth.assert_called_once_with()
        self.m3u8.assert_called_once_with("1440863510")
        self.assertEqual(result["songList"][0]["hls-playlist-url"], self.m3u8.return_value)

    def test_m3u8_failure_does_not_repeat_webplayback_or_multiply_m3u8_attempts(self):
        self.events = [Response()]
        self.m3u8.side_effect = RuntimeError("legacy M3U8 retries already exhausted")
        with self.assertRaisesRegex(RuntimeError, "already exhausted"):
            self.combined_playback("1440863510")
        self.assertEqual(len(self.requests), 1)
        self.m3u8.assert_called_once_with("1440863510")
        self.sleep.assert_not_called()

    def test_missing_songs_and_missing_auth_do_not_trigger_retries_or_m3u8(self):
        self.events = [Response(payload={"songList": []})]
        with self.assertRaisesRegex(RuntimeError, "no songs"):
            self.combined_playback("1440863510")
        self.m3u8.assert_not_called()
        self.sleep.assert_not_called()
        self.auth.return_value = ("", "")
        before = len(self.requests)
        with self.assertRaises(PermissionError):
            self.combined_playback("1440863510")
        self.assertEqual(len(self.requests), before)
        self.sleep.assert_not_called()

    def test_direct_requests_ignore_proxy_environment_and_do_not_modify_it(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://unused-proxy:7890", "HTTPS_PROXY": "http://unused-proxy:7890"}):
            before = dict(os.environ)
            self.call(ConnectError(), Response())
            self.assertEqual(dict(os.environ), before)
        for options in self.client_options:
            self.assertEqual(options, {"trust_env": False, "timeout": 60.0})
        self.assertTrue(all("proxy" not in options and "proxies" not in options for options in self.client_options))

    def test_logs_never_include_tokens_exception_text_response_body_or_credentials(self):
        self.call(
            ConnectError("music-secret developer-secret http://user:password@example.test"),
            Response(503, {"secret response body": True}),
            Response(),
        )
        rendered = "\n".join(str(call.args[0]) for call in self.log.call_args_list)
        for secret in ("music-secret", "developer-secret", "password", "response body", "http://", "authorization", "cookie"):
            self.assertNotIn(secret, rendered)
        self.assertIn("error=ConnectError", rendered)
        self.assertIn("status=503", rendered)


if __name__ == "__main__":
    unittest.main()
