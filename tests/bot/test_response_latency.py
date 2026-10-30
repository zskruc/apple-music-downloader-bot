"""Offline behavior checks for Telegram retries and search response latency."""
import ast
import http.client
import io
import json
import threading
from collections import deque
from pathlib import Path
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "bot" / "ingest_bot.py"
FUNCTIONS = {
    "retryable_network_error", "read_response_text", "telegram_http_error_description",
    "telegram_rate_limit_delay", "telegram_rate_limit_cooldown", "telegram_api", "send_message", "send_photo",
    "send_search_results", "unified_music_search", "apple_music_search",
}


class FakeTime:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response(io.BytesIO):
    def __init__(self, result=None):
        super().__init__(json.dumps({"ok": True, "result": result or {"message_id": 42}}).encode())


class Opener:
    def __init__(self, clock, outcomes):
        self.clock = clock
        self.resources = list(outcomes)
        self.outcomes = iter(self.resources)
        self.calls = []
        self.call_times = []

    def close(self):
        for resource in self.resources:
            if hasattr(resource, "close"):
                resource.close()

    def open(self, request, timeout):
        self.call_times.append(self.clock.monotonic())
        self.calls.append((request.full_url.rsplit("/", 1)[-1], timeout,
                           json.loads(request.data or b"{}")))
        outcome = next(self.outcomes)
        if isinstance(outcome, TimeoutError):
            self.clock.now += timeout
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def http_error(code, retry_after=None, header_after=None):
    payload = {"ok": False, "error_code": code, "description": f"telegram error {code}"}
    if retry_after is not None:
        payload["parameters"] = {"retry_after": retry_after}
    return urllib.error.HTTPError("https://api.telegram.org/test", code, "error",
                                  {"Retry-After": str(header_after)} if header_after is not None else {},
                                  io.BytesIO(json.dumps(payload).encode()))


class RetryBudgetTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text())
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
        self.clock = FakeTime()
        self.logs = []
        self.ns = {
            "Any": object, "SearchResult": object, "http": http, "urllib": urllib,
            "json": json, "time": self.clock, "TOKEN": "test-token",
            "TELEGRAM_API_BASE_URL": "https://api.telegram.org",
            "TELEGRAM_API_ATTEMPTS": 4, "TELEGRAM_SEND_ATTEMPTS": 5,
            "log": self.logs.append,
            "telegram_rate_limit_lock": threading.Lock(),
            "telegram_rate_limit_until": {}, "telegram_rate_limit_keys": deque(),
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), self.ns)

    def opener(self, outcomes):
        opener = Opener(self.clock, outcomes)
        self.addCleanup(opener.close)
        self.ns["TELEGRAM_OPENER"] = opener
        self.ns["TELEGRAM_POLL_OPENER"] = opener
        return opener

    def test_default_send_budget_is_two_and_explicit_overrides_are_preserved(self):
        tree = ast.parse(SOURCE.read_text())
        setting = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "TELEGRAM_SEND_ATTEMPTS"
                               for target in node.targets))
        for configured, expected in [(None, 2), ("", 2), ("1", 1), ("5", 5)]:
            with self.subTest(configured=configured):
                namespace = {"env": lambda name, default="": default if configured is None else configured}
                exec(compile(ast.Module(body=[setting], type_ignores=[]), str(SOURCE), "exec"), namespace)
                self.assertEqual(namespace["TELEGRAM_SEND_ATTEMPTS"], expected)

    def test_cover_timeouts_are_two_total_attempts_then_same_results_fall_back_to_text(self):
        opener = self.opener([TimeoutError("TLS timeout"), TimeoutError("TLS timeout"), Response()])
        keyboard = {"inline_keyboard": [[{"text": "1", "callback_data": "amdl:1"}]]}
        self.ns["cache_search_session"] = lambda *a: "session"
        self.ns["build_search_results_page"] = lambda *a: ("results", keyboard, "https://cover.test/image.jpg")
        self.ns["send_search_results"]("chat", "message", "sample song", "alac", [object()], "actor")
        self.assertEqual([(m, t) for m, t, _ in opener.calls], [("sendPhoto", 8), ("sendPhoto", 8), ("sendMessage", 30)])
        self.assertEqual(opener.calls[-1][2]["text"], "results")
        self.assertEqual(opener.calls[-1][2]["reply_markup"], keyboard)
        self.assertAlmostEqual(self.clock.now - 100, 17.5)
        self.assertTrue(any("format=text delivered=True" in line for line in self.logs))

    def test_cover_recovers_one_short_network_failure(self):
        opener = self.opener([urllib.error.URLError("TLS EOF"), Response()])
        self.assertTrue(self.ns["send_photo"]("chat", "cover", "caption"))
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.clock.sleeps, [1.5])

    def test_text_has_five_total_attempts_not_twenty(self):
        opener = self.opener([urllib.error.URLError("TLS EOF") for _ in range(5)])
        self.assertIsNone(self.ns["send_message"]("chat", "body"))
        self.assertEqual(len(opener.calls), 5)
        self.assertEqual(self.clock.sleeps, [1.5, 3, 4.5, 6])

    def test_configured_two_attempt_setting_limits_text_and_edits(self):
        self.ns["TELEGRAM_SEND_ATTEMPTS"] = 2
        self.ns["TELEGRAM_API_ATTEMPTS"] = 2
        opener = self.opener([ConnectionResetError("reset"), ConnectionResetError("reset")])
        self.assertIsNone(self.ns["send_message"]("chat", "body"))
        self.assertEqual(len(opener.calls), 2)
        opener = self.opener([ConnectionResetError("reset"), ConnectionResetError("reset")])
        with self.assertRaises(ConnectionResetError):
            self.ns["telegram_api"]("editMessageText", {}, timeout=30)
        self.assertEqual(len(opener.calls), 2)

    def test_one_attempt_setting_also_disables_cover_retry(self):
        self.ns["TELEGRAM_SEND_ATTEMPTS"] = 1
        opener = self.opener([ConnectionResetError("reset")])
        self.assertFalse(self.ns["send_photo"]("chat", "cover", "caption"))
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_text_recovers_short_network_failure_and_preserves_payload(self):
        opener = self.opener([ConnectionResetError("reset"), Response()])
        self.assertEqual(self.ns["send_message"]("chat", "body", {"x": 1}, "HTML"), {"message_id": 42})
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(opener.calls[-1][2]["parse_mode"], "HTML")

    def test_permanent_http_errors_never_retry(self):
        for code in [400, 401, 403, 404]:
            for function, args in [("send_photo", ("chat", "cover", "caption")), ("send_message", ("chat", "body"))]:
                with self.subTest(code=code, function=function):
                    opener = self.opener([http_error(code)])
                    self.ns[function](*args)
                    self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_getupdates_is_single_attempt_even_with_four_api_attempts(self):
        opener = self.opener([urllib.error.URLError("EOF")])
        with self.assertRaises(urllib.error.URLError):
            self.ns["telegram_api"]("getUpdates", {"offset": 42}, timeout=10)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_edits_still_use_api_budget(self):
        opener = self.opener([ConnectionResetError("reset") for _ in range(3)] + [Response()])
        self.assertEqual(self.ns["telegram_api"]("editMessageText", {}, timeout=30), {"message_id": 42})
        self.assertEqual(len(opener.calls), 4)

    def test_429_honors_short_server_delay(self):
        opener = self.opener([http_error(429, retry_after=2), Response()])
        self.assertTrue(self.ns["send_photo"]("chat", "cover", "caption"))
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.clock.sleeps, [2])

    def test_429_accepts_retry_after_header(self):
        opener = self.opener([http_error(429, header_after=4), Response()])
        self.assertEqual(self.ns["send_message"]("chat", "body"), {"message_id": 42})
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.clock.sleeps, [4])

    def test_long_429_never_retries_before_server_delay(self):
        for function, args in [("send_photo", ("chat", "cover", "caption")), ("send_message", ("chat", "body"))]:
            with self.subTest(function=function):
                self.ns["telegram_rate_limit_until"].clear()
                self.ns["telegram_rate_limit_keys"].clear()
                opener = self.opener([http_error(429, retry_after=60)])
                self.ns[function](*args)
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(self.clock.sleeps, [])
        self.assertTrue(any("retry_after=60s exceeds retry wait budget" in line for line in self.logs))
        self.assertTrue(any("telegram error 429" in line for line in self.logs))

    def search_reply(self):
        keyboard = {"inline_keyboard": [[{"text": "1", "callback_data": "amdl:1"}]]}
        self.ns["cache_search_session"] = lambda *a: "session"
        self.ns["build_search_results_page"] = lambda *a: ("results", keyboard, "https://cover.test/image.jpg")
        self.ns["send_search_results"]("chat", "message", "sample song", "alac", [object()], "actor")

    def test_cover_429_long_wait_also_blocks_immediate_text_fallback(self):
        opener = self.opener([http_error(429, retry_after=60)])
        self.search_reply()
        self.assertEqual([method for method, _, _ in opener.calls], ["sendPhoto"])
        self.assertEqual(self.clock.sleeps, [])
        self.assertTrue(any("format=text delivered=False" in line for line in self.logs))

    def test_cover_429_short_wait_is_respected_before_text_fallback(self):
        opener = self.opener([http_error(429, retry_after=5), Response()])
        self.search_reply()
        self.assertEqual([method for method, _, _ in opener.calls], ["sendPhoto", "sendMessage"])
        self.assertEqual(opener.call_times, [100, 105])
        self.assertEqual(self.clock.sleeps, [5])

    def test_final_cover_429_still_protects_following_text(self):
        opener = self.opener([ConnectionResetError("reset"), http_error(429, retry_after=60)])
        self.search_reply()
        self.assertEqual([method for method, _, _ in opener.calls], ["sendPhoto", "sendPhoto"])
        self.assertEqual(self.clock.sleeps, [1.5])

    def test_chat_cooldown_is_shared_across_methods_but_not_other_chats(self):
        opener = self.opener([http_error(429, retry_after=60), Response()])
        self.assertFalse(self.ns["send_photo"]("first-chat", "cover", "caption"))
        with self.assertRaisesRegex(RuntimeError, "rate limited"):
            self.ns["telegram_api"]("editMessageText", {"chat_id": "first-chat"})
        self.assertEqual(self.ns["send_message"]("second-chat", "body"), {"message_id": 42})
        self.assertEqual([payload["chat_id"] for _, _, payload in opener.calls], ["first-chat", "second-chat"])
        self.assertEqual(self.clock.sleeps, [])

    def test_chatless_429_creates_bot_cooldown_including_polling(self):
        self.ns["TELEGRAM_API_ATTEMPTS"] = 1
        opener = self.opener([http_error(429, retry_after=60)])
        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            self.ns["telegram_api"]("answerCallbackQuery", {"callback_query_id": "callback"})
        self.assertIsNone(self.ns["send_message"]("another-chat", "body"))
        with self.assertRaisesRegex(RuntimeError, "rate limited"):
            self.ns["telegram_api"]("getUpdates", {"offset": 42})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_long_finite_server_wait_is_never_discarded(self):
        opener = self.opener([http_error(429, retry_after=90000)])
        self.assertFalse(self.ns["send_photo"]("chat", "cover", "caption"))
        self.assertIsNone(self.ns["send_message"]("chat", "body"))
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.ns["telegram_rate_limit_cooldown"]("chat:chat"), 90000)

    def test_nonfinite_or_negative_retry_after_is_not_a_sleep_duration(self):
        for delay in (float("inf"), float("nan"), -1):
            with self.subTest(delay=delay):
                error = http_error(429, retry_after=delay)
                self.addCleanup(error.close)
                self.assertIsNone(self.ns["telegram_rate_limit_delay"](error))

    def test_shared_cooldown_expiry_allows_later_delivery(self):
        opener = self.opener([http_error(429, retry_after=60), Response()])
        self.assertFalse(self.ns["send_photo"]("chat", "cover", "caption"))
        self.clock.now += 60
        self.assertEqual(self.ns["send_message"]("chat", "body"), {"message_id": 42})
        self.assertEqual(opener.call_times, [100, 160])
        self.assertEqual(self.clock.sleeps, [])

    def test_rate_limit_waits_share_a_finite_budget_per_api_call(self):
        opener = self.opener([http_error(429, retry_after=20), http_error(429, retry_after=20)])
        self.assertIsNone(self.ns["send_message"]("chat", "body"))
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.clock.sleeps, [20])

    def test_extending_cooldown_during_sleep_never_sends_early(self):
        opener = self.opener([])
        self.ns["telegram_rate_limit_cooldown"]("chat:chat", 1)
        def extended_sleep(seconds):
            self.clock.sleep(seconds)
            self.ns["telegram_rate_limit_cooldown"]("chat:chat", 60)
        self.ns["time"] = type("Clock", (), {"monotonic": lambda _: self.clock.monotonic(),
                                             "sleep": lambda _, seconds: extended_sleep(seconds)})()
        self.assertFalse(self.ns["send_photo"]("chat", "cover", "caption"))
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.clock.sleeps, [1])

    def test_waiting_chat_does_not_hold_shared_lock_or_block_other_chat(self):
        waiting, release = threading.Event(), threading.Event()
        opener = self.opener([Response(), Response()])
        self.ns["telegram_rate_limit_cooldown"]("chat:first-chat", 1)
        failures, results = [], []
        def blocked_sleep(seconds):
            waiting.set()
            if not release.wait(2):
                raise AssertionError("unrelated chat was blocked by shared lock")
            self.clock.sleep(seconds)
        self.ns["time"] = type("Clock", (), {"monotonic": lambda _: self.clock.monotonic(),
                                             "sleep": lambda _, seconds: blocked_sleep(seconds)})()
        def worker():
            try:
                results.append(self.ns["send_message"]("first-chat", "body"))
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.assertEqual(self.ns["send_message"]("second-chat", "body"), {"message_id": 42})
            self.assertEqual(opener.calls[0][2]["chat_id"], "second-chat")
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [{"message_id": 42}])
        self.assertEqual([payload["chat_id"] for _, _, payload in opener.calls], ["second-chat", "first-chat"])

    def test_concurrent_cooldown_updates_preserve_longer_wait(self):
        barrier = threading.Barrier(3)
        def record(seconds):
            barrier.wait()
            self.ns["telegram_rate_limit_cooldown"]("chat:chat", seconds)
        threads = [threading.Thread(target=record, args=(seconds,)) for seconds in (10, 60)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(self.ns["telegram_rate_limit_cooldown"]("chat:chat"), 60)
        self.assertEqual(list(self.ns["telegram_rate_limit_keys"]), ["chat:chat"])

    def test_expired_cooldown_cleanup_is_bounded_and_eventually_complete(self):
        for number in range(70):
            self.ns["telegram_rate_limit_cooldown"]("chat:" + str(number), 1)
        self.clock.now += 2
        self.ns["telegram_rate_limit_cooldown"]("chat:unused")
        self.assertEqual(len(self.ns["telegram_rate_limit_until"]), 38)
        self.ns["telegram_rate_limit_cooldown"]("chat:unused")
        self.ns["telegram_rate_limit_cooldown"]("chat:unused")
        self.assertEqual(self.ns["telegram_rate_limit_until"], {})
        self.assertEqual(list(self.ns["telegram_rate_limit_keys"]), [])

    def test_two_http_5xx_attempts_can_exhaust_cover_budget(self):
        opener = self.opener([http_error(503), http_error(503)])
        self.assertFalse(self.ns["send_photo"]("chat", "cover", "caption"))
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(any("telegram error 503" in line for line in self.logs))

    def test_semantic_api_failure_is_not_retried_by_wrappers(self):
        response = io.BytesIO(b'{"ok": false, "description": "Bad Request"}')
        opener = self.opener([response])
        self.assertIsNone(self.ns["send_message"]("chat", "body"))
        self.assertEqual(len(opener.calls), 1)

    def test_existing_monkeypatch_signature_is_unchanged(self):
        calls = []
        def api(method, payload=None, timeout=70):
            calls.append((method, timeout))
            return {"message_id": 42}
        self.ns["telegram_api"] = api
        self.assertTrue(self.ns["send_photo"]("chat", "cover", "caption"))
        self.assertEqual(self.ns["send_message"]("chat", "body"), {"message_id": 42})
        self.assertEqual(calls, [("sendPhoto", 8), ("sendMessage", 30)])

    def test_unified_search_log_has_duration_and_source_counts(self):
        self.ns.update(LX_FALLBACK_ENABLED=False, SQ_MUSIC_ENABLED=False, UNIFIED_SEARCH_TIMEOUT_SECONDS=25,
                       bounded_search=lambda *a, **k: ({"apple": []}, {"sq": "timeout"}),
                       merge_candidates=lambda *a: [])
        results, errors = self.ns["unified_music_search"]("query")
        self.assertEqual(results, [])
        self.assertEqual(errors, {"sq": "timeout"})
        self.assertTrue(any("elapsed=0.000s results=0 sources=apple:0 failed=sq" in line for line in self.logs))

    def test_apple_search_log_includes_duration_without_changing_results(self):
        self.ns.update(SEARCH_LANGUAGE="zh_cn", SEARCH_LIMIT=32, SEARCH_COUNTRY="CN",
                       apple_music_search_token=lambda: "token",
                       apple_music_search_request=lambda *a, **k: {"results": {"songs": {"data": []}}})
        self.assertEqual(self.ns["apple_music_search"]("query"), [])
        self.assertTrue(any("Apple Music catalog search" in line and "elapsed=" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
