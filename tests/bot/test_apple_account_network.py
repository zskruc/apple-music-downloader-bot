from __future__ import annotations

import asyncio
import ast
import contextlib
import inspect
import io
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bot"))
import apple_account_network as network


UPSTREAM_SOURCE = '''
@staticmethod
async def get_account_info(token: str, media_user_token: str, meta: str = "subscription") -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            APPLE_MUSIC_AMP_API_URL + APPLE_MUSIC_ACCOUNT_INFO_API_URI,
            params={"meta": meta},
            headers={"authorization": f"Bearer {token}",
                     "cookie": f"media-user-token={media_user_token}"},
        )
        response.raise_for_status()
        account_info = response.json()
    except httpx.HTTPError:
        raise GamdlApiResponseError("Error fetching account info")
    return account_info
'''

CREATE_SOURCE = '''
@classmethod
async def create(cls, storefront="us", language="en-US", token=None, media_user_token=None):
    account_info = await cls.get_account_info(token, media_user_token) if media_user_token else None
    client = httpx.AsyncClient(
        headers={"authorization": f"Bearer {token}", "origin": APPLE_MUSIC_HOMEPAGE_URL},
        transport=RetryTransport(retry=Retry(total=6, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])),
    )
    return cls(client=client)
'''


class HTTPError(Exception):
    pass


class TimeoutException(HTTPError):
    pass


class NetworkError(HTTPError):
    pass


class RemoteProtocolError(HTTPError):
    pass


class ProxyError(HTTPError):
    pass


class StatusError(HTTPError):
    pass


class GamdlApiResponseError(Exception):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = {"data": [], "meta": {"subscription": {"active": True}}} if payload is None else payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise StatusError("upstream response body must not be logged")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class AccountNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.options = []
        self.requests = []
        self.output = io.StringIO()
        self.sleep = AsyncMock()
        self.created_transports = []
        self.created_clients = []
        self.api_mounts = {}
        test = self

        class AsyncHTTPTransport:
            def __init__(self, proxy=None):
                self.proxy = proxy
                self.closed = False

            async def aclose(self):
                self.closed = True

        class Retry:
            def __init__(self, **options):
                self.options = options

        class RetryTransport:
            def __init__(self, transport=None, retry=None):
                self.retry = retry or Retry()
                self._async_transport = transport or AsyncHTTPTransport()
                self.closed = False
                test.created_transports.append(self)

            async def aclose(self):
                self.closed = True
                await self._async_transport.aclose()

        class Client:
            def __init__(self, **options):
                test.options.append(options)
                self._transport = options.get("transport") or AsyncHTTPTransport()
                self._mounts = dict(test.api_mounts) if "transport" in options else {}
                self.headers = dict(options.get("headers", {}))
                self.timeout = options.get("timeout", "upstream-default-timeout")
                self.closed = False
                test.created_clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                await self.aclose()
                return False

            async def aclose(self):
                self.closed = True
                await self._transport.aclose()

            async def get(self, url, **options):
                test.requests.append((url, options))
                event = test.events.pop(0)
                if isinstance(event, BaseException):
                    raise event
                return event

        class Api:
            def __init__(self, *, client, account_info=None):
                self.client = client
                self.account_info = account_info

            @staticmethod
            async def get_account_info(token: str, media_user_token: str, meta: str = "subscription") -> dict:
                raise AssertionError("unpatched account method called")

            @classmethod
            async def create(cls, storefront="us", language="en-US", token=None, media_user_token=None):
                account_info = await cls.get_account_info(token, media_user_token) if media_user_token else None
                client = Client(
                    headers={"authorization": f"Bearer {token}", "origin": "https://music.apple.com"},
                    transport=RetryTransport(retry=Retry(total=6, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])),
                )
                if media_user_token:
                    client.headers.update({"cookie": f"media-user-token={media_user_token}"})
                return cls(client=client, account_info=account_info)

        self.api = Api
        self.retry_transport_class = RetryTransport
        self.async_transport_class = AsyncHTTPTransport
        httpx = types.SimpleNamespace(
            AsyncClient=Client, Timeout=lambda **values: values,
            AsyncHTTPTransport=AsyncHTTPTransport,
            HTTPError=HTTPError, TimeoutException=TimeoutException,
            NetworkError=NetworkError, RemoteProtocolError=RemoteProtocolError,
            ProxyError=ProxyError,
        )
        upstream = types.ModuleType("gamdl.api.apple_music")
        upstream.AppleMusicApi = Api
        upstream.httpx = httpx
        upstream.GamdlApiResponseError = GamdlApiResponseError
        upstream.RetryTransport = RetryTransport
        upstream.APPLE_MUSIC_AMP_API_URL = "https://amp-api.music.apple.com"
        upstream.APPLE_MUSIC_ACCOUNT_INFO_API_URI = "/v1/me/account"
        upstream.APPLE_MUSIC_HOMEPAGE_URL = "https://music.apple.com"
        package = types.ModuleType("gamdl.api")
        package.apple_music = upstream
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(sys.modules, {"gamdl": types.ModuleType("gamdl"), "gamdl.api": package, "gamdl.api.apple_music": upstream}))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.version = self.stack.enter_context(patch.object(network.importlib.metadata, "version", return_value="3.9.1"))
        self.source = self.stack.enter_context(patch.object(network.inspect, "getsource", side_effect=lambda function: CREATE_SOURCE if function.__name__ == "create" else UPSTREAM_SOURCE))
        self.stack.enter_context(patch.object(network.asyncio, "sleep", self.sleep))
        self.stack.enter_context(contextlib.redirect_stdout(self.output))

    async def call(self, *events):
        self.events.extend(events)
        network.install_account_network_patch()
        return await self.api.get_account_info("developer-secret", "media-secret")

    async def test_primary_success_preserves_environment_and_sets_explicit_timeouts(self):
        os.environ.update({"HTTPS_PROXY": "http://primary:7890", "NO_PROXY": "wrapper,localhost", "no_proxy": "wrapper,localhost"})
        before = dict(os.environ)
        result = await self.call(Response())
        self.assertTrue(result["meta"]["subscription"]["active"])
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.options, [{"timeout": {"connect": 15., "read": 30., "write": 30., "pool": 15.}, "trust_env": True}])
        self.sleep.assert_not_awaited()
        self.assertEqual(self.requests[0][1]["params"], {"meta": "subscription"})

    async def test_timeout_switches_to_fallback_and_persists_only_proxy_variables(self):
        proxy = "http://host.docker.internal:7892"
        os.environ.update({network.FALLBACK_PROXIES_ENV: '["'+proxy+'"]', "HTTPS_PROXY": "http://primary:7890", "NO_PROXY": "wrapper,legacy-wrapper", "no_proxy": "wrapper,legacy-wrapper", "ALL_PROXY": "http://unchanged:7891"})
        result = await self.call(TimeoutException("sensitive transport text"), Response())
        self.assertEqual(len(self.options), 2)
        self.assertTrue(self.options[0]["trust_env"])
        self.assertEqual(self.options[1]["proxy"], proxy)
        self.assertFalse(self.options[1]["trust_env"])
        self.assertEqual(self.sleep.await_args_list[0].args, (2.,))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(os.environ[name], proxy)
        self.assertEqual(os.environ["NO_PROXY"], "wrapper,legacy-wrapper")
        self.assertEqual(os.environ["no_proxy"], "wrapper,legacy-wrapper")
        self.assertEqual(os.environ["ALL_PROXY"], "http://unchanged:7891")
        self.assertIn("data", result)

    async def test_distinct_fallbacks_are_used_in_order(self):
        os.environ[network.FALLBACK_PROXIES_ENV] = '["http://backup-a:7892","http://backup-b:7893"]'
        await self.call(NetworkError(), RemoteProtocolError(), Response())
        self.assertEqual([item.get("proxy") for item in self.options], [None, "http://backup-a:7892", "http://backup-b:7893"])
        self.assertEqual([call.args for call in self.sleep.await_args_list], [(2.,), (4.,)])

    async def test_proxy_connect_error_switches_to_fallback(self):
        os.environ[network.FALLBACK_PROXIES_ENV] = '["http://backup:7892"]'
        await self.call(ProxyError("CONNECT failed"), Response())
        self.assertEqual(len(self.options), 2)
        self.assertEqual(self.options[1]["proxy"], "http://backup:7892")
        self.sleep.assert_awaited_once_with(2.)

    async def test_all_network_failures_are_bounded_and_preserve_upstream_exception(self):
        os.environ[network.FALLBACK_PROXIES_ENV] = '["http://backup:7892"]'
        with self.assertRaises(GamdlApiResponseError) as caught:
            await self.call(TimeoutException(), NetworkError(), RemoteProtocolError())
        self.assertEqual(str(caught.exception), "Error fetching account info")
        self.assertIsNone(caught.exception.status_code)
        self.assertEqual(len(self.options), 3)
        self.assertEqual([call.args for call in self.sleep.await_args_list], [(2.,), (4.,)])
        self.assertNotIn("HTTP_PROXY", os.environ)

    async def test_authentication_errors_fail_immediately_without_switching(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.events = [Response(status)]
                before = len(self.options)
                network.install_account_network_patch()
                with self.assertRaises(GamdlApiResponseError) as caught:
                    await self.api.get_account_info("secret", "secret")
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(len(self.options), before + 1)
        self.sleep.assert_not_awaited()

    async def test_retryable_http_statuses_recover(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.events = [Response(status), Response()]
                network.install_account_network_patch()
                before = len(self.options)
                await self.api.get_account_info("secret", "secret")
                self.assertEqual(len(self.options), before + 2)

    async def test_non_retryable_status_and_invalid_json_are_not_retried(self):
        with self.assertRaises(GamdlApiResponseError) as caught:
            await self.call(Response(404))
        self.assertEqual(caught.exception.status_code, 404)
        self.events = [Response(payload=ValueError("invalid JSON"))]
        with self.assertRaises(ValueError):
            await self.api.get_account_info("secret", "secret")
        self.sleep.assert_not_awaited()

    async def test_inactive_subscription_is_returned_for_gamdl_to_reject(self):
        payload = {"meta": {"subscription": {"active": False}}}
        self.assertEqual(await self.call(Response(payload=payload)), payload)
        self.sleep.assert_not_awaited()

    async def test_cancellation_propagates_immediately(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.call(asyncio.CancelledError())
        self.assertEqual(len(self.options), 1)
        self.sleep.assert_not_awaited()

    async def test_logs_do_not_contain_auth_proxy_or_response_secrets(self):
        os.environ[network.FALLBACK_PROXIES_ENV] = '["http://proxy-user:proxy-password@backup:7892"]'
        await self.call(TimeoutException("proxy-password media-secret developer-secret"), Response(payload={"secret-body": True}))
        output = self.output.getvalue()
        for forbidden in ("proxy-user", "proxy-password", "media-secret", "developer-secret", "secret-body", "http://"):
            self.assertNotIn(forbidden, output)
        self.assertIn("route=fallback-1", output)

    async def test_invalid_proxy_configuration_fails_safely_before_network(self):
        for value in ('{}', '[123]', '["socks5://secret@proxy:7892"]', '["http://user:password@proxy:999999"]', '["http://proxy/path"]', 'invalid'):
            with self.subTest(config=value):
                os.environ[network.FALLBACK_PROXIES_ENV] = value
                with self.assertRaises(RuntimeError) as caught:
                    network.install_account_network_patch()
                self.assertNotIn("password", str(caught.exception))
        self.assertEqual(self.options, [])

    async def test_unreviewed_gamdl_version_is_rejected(self):
        self.version.return_value = "3.9.2"
        with self.assertRaisesRegex(RuntimeError, "requires reviewed GAMDL 3.9.1"):
            network.install_account_network_patch()

    async def test_changed_upstream_signature_is_rejected(self):
        async def changed(token, media_user_token, meta="subscription", extra=None):
            pass
        self.api.get_account_info = staticmethod(changed)
        with self.assertRaisesRegex(RuntimeError, "signature"):
            network.install_account_network_patch()

    async def test_changed_upstream_source_is_rejected(self):
        self.source.side_effect = None
        self.source.return_value = "async def get_account_info(*args): pass"
        with self.assertRaisesRegex(RuntimeError, "implementation"):
            network.install_account_network_patch()

    async def test_install_is_idempotent(self):
        network.install_account_network_patch()
        installed = self.api.get_account_info
        installed_create = self.api.create.__func__
        network.install_account_network_patch()
        self.assertIs(self.api.get_account_info, installed)
        self.assertIs(self.api.create.__func__, installed_create)

    async def test_factory_binds_successful_fallback_preserving_client_retry_and_headers(self):
        proxy = "http://backup:7892"
        os.environ[network.FALLBACK_PROXIES_ENV] = '["'+proxy+'"]'
        self.events = [TimeoutException(), Response()]
        network.install_account_network_patch()
        api = await self.api.create(token="developer-secret", media_user_token="media-secret")
        previous, replacement = self.created_transports
        self.assertIs(api.client, self.created_clients[-1])
        self.assertIs(api.client._transport, replacement)
        self.assertTrue(previous.closed)
        self.assertTrue(previous._async_transport.closed)
        self.assertIs(replacement.retry, previous.retry)
        self.assertEqual(replacement.retry.options, {"total": 6, "backoff_factor": 1, "status_forcelist": [429, 500, 502, 503, 504]})
        self.assertIsInstance(replacement._async_transport, self.async_transport_class)
        self.assertEqual(replacement._async_transport.proxy, proxy)
        self.assertEqual(api.client.headers, {"authorization": "Bearer developer-secret", "origin": "https://music.apple.com", "cookie": "media-user-token=media-secret"})
        self.assertEqual(api.client.timeout, "upstream-default-timeout")
        self.assertFalse(api.client.closed)

    async def test_factory_binds_already_configured_environment_proxy(self):
        os.environ["HTTPS_PROXY"] = "http://already-selected:7892"
        self.events = [Response()]
        network.install_account_network_patch()
        api = await self.api.create(token="secret", media_user_token="secret")
        self.assertEqual(api.client._transport._async_transport.proxy, "http://already-selected:7892")
        self.assertTrue(self.created_transports[0].closed)

    async def test_factory_keeps_primary_direct_transport_and_honors_no_proxy(self):
        os.environ.update({"HTTPS_PROXY": "http://excluded:7892", "NO_PROXY": ".music.apple.com,wrapper"})
        self.events = [Response()]
        network.install_account_network_patch()
        api = await self.api.create(token="secret", media_user_token="secret")
        self.assertEqual(len(self.created_transports), 1)
        self.assertIs(api.client._transport, self.created_transports[0])
        self.assertIsNone(api.client._transport._async_transport.proxy)
        self.assertFalse(api.client._transport.closed)

    async def test_factory_rejects_unreviewed_mounts_and_closes_client(self):
        os.environ["HTTPS_PROXY"] = "http://backup:7892"
        self.api_mounts = {"https://": object()}
        self.events = [Response()]
        network.install_account_network_patch()
        with self.assertRaisesRegex(RuntimeError, "transport layout"):
            await self.api.create(token="secret", media_user_token="secret")
        self.assertTrue(self.created_clients[-1].closed)
        self.assertTrue(self.created_transports[0].closed)

    async def test_factory_signature_is_preserved_for_cli_inspection(self):
        before = inspect.signature(self.api.create)
        network.install_account_network_patch()
        self.assertEqual(inspect.signature(self.api.create), before)

    async def test_changed_factory_signature_is_rejected_before_either_patch_is_installed(self):
        async def changed(cls, token=None):
            pass
        self.api.create = classmethod(changed)
        original_account = self.api.get_account_info
        with self.assertRaisesRegex(RuntimeError, "create signature"):
            network.install_account_network_patch()
        self.assertIs(self.api.get_account_info, original_account)

    async def test_changed_factory_retry_configuration_is_rejected(self):
        self.source.side_effect = lambda function: CREATE_SOURCE.replace("total=6", "total=7") if function.__name__ == "create" else UPSTREAM_SOURCE
        with self.assertRaisesRegex(RuntimeError, "create implementation"):
            network.install_account_network_patch()


class OuterTaskRetryTests(unittest.TestCase):
    """Exercise the actual task-level predicate without importing the live Bot."""

    def setUp(self):
        source = Path(__file__).resolve().parents[2] / "bot" / "ingest_bot.py"
        tree = ast.parse(source.read_text())
        names = {"log_has_pattern", "is_transient_gamdl_failure", "is_wrapper_m3u8_failure"}
        selected = [
            node for node in tree.body
            if getattr(node, "name", None) in names
            or isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in {"TRANSIENT_GAMDL_FAILURE_RE", "WRAPPER_M3U8_FAILURE_RE"} for target in node.targets)
        ]
        namespace = {"Path": Path, "re": re, "log": lambda *_args: None}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
        self.predicate = namespace["is_transient_gamdl_failure"]
        self.wrapper_predicate = namespace["is_wrapper_m3u8_failure"]
        self.directory = tempfile.TemporaryDirectory(prefix="account-retry-test-")
        self.addCleanup(self.directory.cleanup)
        self.log_file = Path(self.directory.name) / "gamdl.log"

    def retryable(self, content, tail=None):
        self.log_file.write_text(content)
        return self.predicate(self.log_file, content.splitlines()[-30:] if tail is None else tail)

    def test_latest_structured_401_or_403_blocks_whole_task_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                text = (
                    "$ gamdl current-run\nStarting Gamdl 3.9.1\n"
                    f"[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status={status}\n"
                    "Error fetching account info\n"
                )
                self.assertFalse(self.retryable(text))

    def test_older_auth_failure_does_not_hide_success_then_network_failure(self):
        text = (
            "$ gamdl older-run\n"
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=401\n"
            "$ gamdl new-run\nStarting Gamdl 3.9.1\n"
            "[apple-music-ingest] account request succeeded: attempt=1/3 route=environment\n"
            "httpx.ConnectTimeout\n"
        )
        self.assertTrue(self.retryable(text))

    def test_later_success_within_same_run_supersedes_old_auth_failure(self):
        text = (
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=403\n"
            "[apple-music-ingest] account request succeeded: attempt=2/3 route=fallback-1\n"
            "httpx.ReadTimeout\n"
        )
        self.assertTrue(self.retryable(text))

    def test_new_legacy_run_without_structured_status_retains_old_retry_policy(self):
        text = (
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=401\n"
            "$ gamdl legacy-run\nStarting Gamdl 3.9.1\nError fetching account info\n"
        )
        self.assertTrue(self.retryable(text))

    def test_later_transient_account_failure_supersedes_old_auth_failure(self):
        text = (
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=401\n"
            "[apple-music-ingest] account request failed: attempt=2/3 route=fallback-1 error=ConnectTimeout status=None\n"
            "Error fetching account info\n"
        )
        self.assertTrue(self.retryable(text))

    def test_unstructured_legacy_error_and_retryable_http_status_remain_retryable(self):
        self.assertTrue(self.retryable("Error fetching account info\n"))
        self.assertTrue(self.retryable(
            "[apple-music-ingest] account request failed: attempt=3/3 route=fallback-1 error=HTTPStatusError status=503\n"
            "Error fetching account info\n"
        ))

    def test_auth_status_can_be_found_when_traceback_displaces_it_from_tail(self):
        text = (
            "$ gamdl current-run\n"
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=401\n"
            + "traceback frame\n" * 60
            + "Error fetching account info\n"
        )
        self.assertFalse(self.retryable(text))

    def test_bounded_read_does_not_reuse_ancient_auth_failure(self):
        text = (
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=401\n"
            + "old log padding\n" * 20000
            + "Error fetching account info\n"
        )
        self.assertTrue(self.retryable(text))

    def test_missing_log_still_uses_current_tail_for_auth_failure(self):
        tail = [
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=403",
            "Error fetching account info",
        ]
        self.assertFalse(self.predicate(self.log_file, tail))

    def test_old_wrapper_failure_does_not_retry_current_account_auth_rejection(self):
        text = (
            "$ gamdl older-run\n"
            "Wrapper m3u8 request 1708304289 failed after 3 attempts\n"
            "$ gamdl current-run\nStarting Gamdl 3.9.1\n"
            "[apple-music-ingest] account request failed: attempt=1/3 route=environment error=HTTPStatusError status=401\n"
            "Error fetching account info\n"
        )
        self.log_file.write_text(text)
        tail = text.splitlines()[-30:]
        self.assertFalse(self.predicate(self.log_file, tail))
        self.assertFalse(self.wrapper_predicate(self.log_file, tail))


if __name__ == "__main__":
    unittest.main()
