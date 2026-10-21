from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bot"))
import auth_auto_refresh as refresh
import nas_youtube_auth as auth

spec = importlib.util.spec_from_file_location("youtube_auth_operations", Path(__file__).with_name("youtube_auth.py"))
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)


def cookie(name: str, value: str, domain: str = ".youtube.com") -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
        path="/", path_specified=True, secure=True, expires=None,
        discard=True, comment=None, comment_url=None, rest={}, rfc2109=False,
    )


def write_cookies(path: Path, value: str) -> None:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    for name in auth.REQUIRED_NAMES:
        jar.set_cookie(cookie(name, value))
    jar.save(ignore_discard=True, ignore_expires=True)
    path.chmod(0o600)


class CookiePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.destination = self.root / "cookies.txt"
        self.previous = self.root / "cookies.txt.previous"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(auth, "DOCKER_SOCKET", self.root / "no-docker.sock").start()

    def source(self, value="synthetic-new"):
        return [cookie(name, value) for name in auth.REQUIRED_NAMES] + [cookie("OTHER", "unrelated", ".example.test")]

    def test_export_requires_confirmed_stop_without_socket(self):
        with self.assertRaisesRegex(RuntimeError, "Docker socket"):
            auth.export_profile(self.profile, self.destination, "https://youtu.be/example")

    def test_mounted_socket_running_wins_over_external_assertion(self):
        socket_path = self.root / "docker.sock"
        socket_path.touch()
        with mock.patch.object(auth, "DOCKER_SOCKET", socket_path), \
                mock.patch.object(auth, "auth_browser_running", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "仍在运行"):
                auth.export_profile(self.profile, self.destination, "https://youtu.be/example", browser_stopped=True)

    def test_normal_export_requires_probe_link(self):
        with self.assertRaisesRegex(RuntimeError, "probe-url"):
            auth.export_profile(self.profile, self.destination, browser_stopped=True)
        self.assertFalse(self.destination.exists())

    def test_validated_export_filters_domain_and_preserves_private_previous(self):
        write_cookies(self.destination, "synthetic-old")
        before = self.destination.read_bytes()
        observed = []
        def probe(candidate, url):
            observed.append((candidate != self.destination, candidate.stat().st_mode & 0o777,
                             self.destination.read_bytes() == before))
            return SimpleNamespace(auth_strategy="cookies-standard")
        with mock.patch("yt_dlp.cookies.load_cookies", return_value=self.source()), \
                mock.patch.object(auth, "probe_candidate", side_effect=probe):
            names, _ = auth.export_profile(self.profile, self.destination, "https://youtu.be/example", browser_stopped=True)
        self.assertEqual(observed, [(True, 0o600, True)])
        self.assertEqual(names, auth.REQUIRED_NAMES)
        self.assertNotIn("example.test", self.destination.read_text())
        self.assertEqual(self.previous.read_bytes(), before)
        self.assertIn("synthetic-new", self.destination.read_text())
        for path in (self.destination, self.previous):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_probe_preserves_both_current_and_previous_without_raw_errors(self):
        write_cookies(self.destination, "synthetic-current")
        write_cookies(self.previous, "synthetic-previous")
        before = (self.destination.read_bytes(), self.previous.read_bytes())
        with mock.patch("yt_dlp.cookies.load_cookies", return_value=self.source()), \
                mock.patch.object(auth, "probe_candidate", side_effect=RuntimeError("sensitive=synthetic-secret")):
            with self.assertRaises(RuntimeError) as raised:
                auth.export_profile(self.profile, self.destination, "https://youtu.be/example", browser_stopped=True)
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertEqual((self.destination.read_bytes(), self.previous.read_bytes()), before)
        self.assertEqual(list(self.root.glob(".cookies.txt.*")), [self.root / ".cookies.txt.lock"])

    def test_incomplete_session_never_probes_or_replaces(self):
        write_cookies(self.destination, "synthetic-current")
        before = self.destination.read_bytes()
        with mock.patch("yt_dlp.cookies.load_cookies", return_value=[cookie("SID", "partial")]), \
                mock.patch.object(auth, "probe_candidate") as probe:
            with self.assertRaisesRegex(RuntimeError, "会话不完整"):
                auth.export_profile(self.profile, self.destination, "https://youtu.be/example", browser_stopped=True)
        probe.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), before)

    def test_cancel_during_candidate_probe_preserves_current_and_previous(self):
        write_cookies(self.destination, "synthetic-current")
        write_cookies(self.previous, "synthetic-previous")
        before = (self.destination.read_bytes(), self.previous.read_bytes())
        with mock.patch("yt_dlp.cookies.load_cookies", return_value=self.source()), \
                mock.patch.object(auth, "probe_candidate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                auth.export_profile(self.profile, self.destination, "https://youtu.be/example", browser_stopped=True)
        self.assertEqual((self.destination.read_bytes(), self.previous.read_bytes()), before)
        self.assertEqual(list(self.root.glob(".cookies.txt.*")), [self.root / ".cookies.txt.lock"])

    def test_sabr_integrity_failure_keeps_current_and_previous_without_anonymous_probe(self):
        write_cookies(self.destination, "synthetic-current")
        write_cookies(self.previous, "synthetic-previous")
        before = (self.destination.read_bytes(), self.previous.read_bytes())
        with mock.patch("yt_dlp.cookies.load_cookies", return_value=self.source()), \
                mock.patch("youtube_ingest.PO_TOKEN_ENABLED", True), \
                mock.patch("youtube_ingest._sabr_tool_path", side_effect=RuntimeError("完整性校验失败")), \
                mock.patch("youtube_ingest.probe_url") as probe:
            with self.assertRaisesRegex(RuntimeError, "保留现有文件"):
                auth.export_profile(self.profile, self.destination, "https://youtu.be/example", browser_stopped=True)
        probe.assert_not_called()
        self.assertEqual((self.destination.read_bytes(), self.previous.read_bytes()), before)

    def test_validated_rollback_swaps_private_current_and_previous(self):
        write_cookies(self.destination, "synthetic-current")
        write_cookies(self.previous, "synthetic-previous")
        before = (self.destination.read_bytes(), self.previous.read_bytes())
        with mock.patch.object(auth, "probe_candidate", return_value=object()):
            auth.rollback(self.destination, "https://youtu.be/example", browser_stopped=True)
        self.assertEqual((self.destination.read_bytes(), self.previous.read_bytes()), before[::-1])
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)

    def test_rollback_failure_keeps_files(self):
        write_cookies(self.destination, "synthetic-current")
        write_cookies(self.previous, "synthetic-previous")
        before = (self.destination.read_bytes(), self.previous.read_bytes())
        with mock.patch.object(auth, "probe_candidate", side_effect=RuntimeError("probe failed")):
            with self.assertRaises(RuntimeError):
                auth.rollback(self.destination, "https://youtu.be/example", browser_stopped=True)
        self.assertEqual((self.destination.read_bytes(), self.previous.read_bytes()), before)

    def test_metadata_never_serializes_cookie_values(self):
        write_cookies(self.destination, "synthetic-private-value")
        data = auth.status(self.profile, self.destination)
        self.assertEqual(data["missing_cookie_names"], [])
        self.assertEqual(data["mode"], "0600")
        self.assertIsNone(data["browser_running"])
        self.assertNotIn("synthetic-private-value", json.dumps(data))

    def test_concurrent_writer_refused(self):
        with auth._destination_lock(self.destination):
            with self.assertRaisesRegex(RuntimeError, "正在运行"):
                with auth._destination_lock(self.destination):
                    self.fail("second writer acquired lock")


class AuthenticationRecoveryTests(unittest.TestCase):
    def test_state_private_and_cooldown_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(refresh, "STATE_ROOT", Path(directory)):
            refresh._save_state("youtube", False, "synthetic failure")
            self.assertEqual(refresh._state_path("youtube").stat().st_mode & 0o777, 0o600)
            recent = refresh._recent_result("youtube")
            self.assertFalse(recent.attempted)
            self.assertFalse(recent.success)

    def test_subprocess_output_not_exposed(self):
        result = subprocess.CompletedProcess([], 1, "Cookie: synthetic-private", "https://user:password@example.test")
        self.assertEqual(refresh._short_output(result), "导出程序退出码 1")

    def test_preexisting_browser_is_not_stopped_or_counted_as_failure(self):
        with mock.patch.object(refresh, "ENABLED", True), \
                mock.patch.object(refresh, "_recent_result", return_value=None), \
                mock.patch.object(refresh, "_container_state", return_value={"Running": True}), \
                mock.patch.object(refresh, "_container_action") as action, \
                mock.patch.object(refresh, "_save_state") as save:
            result = refresh.refresh_auth("youtube", "https://youtu.be/example")
        self.assertFalse(result.attempted)
        action.assert_not_called()
        save.assert_not_called()

    def test_cancel_before_start_never_rewrites_state_or_cookies(self):
        with mock.patch.object(refresh, "ENABLED", True), \
                mock.patch.object(refresh, "_container_action") as action, \
                mock.patch.object(refresh, "_save_state") as save:
            with self.assertRaises(refresh.AuthRefreshCancelled):
                refresh.refresh_auth("youtube", "https://youtu.be/example", cancel_observer=lambda: True)
        action.assert_not_called()
        save.assert_not_called()

    def test_cancel_owned_browser_stops_only_that_session(self):
        states = [{"Running": False}, {"Running": True, "StartedAt": "owned"},
                  {"Running": True, "StartedAt": "owned"}]
        with mock.patch.object(refresh, "ENABLED", True), \
                mock.patch.object(refresh, "_recent_result", return_value=None), \
                mock.patch.object(refresh, "_container_state", side_effect=states), \
                mock.patch.object(refresh, "_container_action", return_value=204) as action, \
                mock.patch.object(refresh, "_wait_container_state"), \
                mock.patch.object(refresh, "_wait_browser_port", side_effect=refresh.AuthRefreshCancelled), \
                mock.patch.object(refresh, "_save_state") as save:
            with self.assertRaises(refresh.AuthRefreshCancelled):
                refresh.refresh_auth("youtube", "https://youtu.be/example")
        self.assertEqual([call.args[1] for call in action.call_args_list], ["start", "stop"])
        save.assert_not_called()

    def test_cancellable_exporter_terminates_its_process_group(self):
        checks = iter([False, False, True])
        with self.assertRaises(refresh.AuthRefreshCancelled):
            refresh._run_exporter([sys.executable, "-c", "import time; time.sleep(20)"],
                                  os.environ.copy(), lambda: next(checks, True))


class HostOperatorTests(unittest.TestCase):
    def test_stack_inherits_active_images_and_auth_overlay(self):
        from ops import stackctl
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            active = state / "ops/images.compose.json"
            active.parent.mkdir()
            active.write_text('{"services":{"bot":{"image":"example:approved"}}}')
            config = {"name": "synthetic-project", "services": {"bot": {"volumes": [
                {"target": "/state", "source": str(state)},
            ]}}}
            args = SimpleNamespace(self_heal=False, auth_refresh=True, profile=["auth"])
            with mock.patch.object(stackctl, "run", return_value=json.dumps(config)):
                stack = operations.Stack(args)
        self.assertIn(str(ROOT / "compose.auth-refresh.yaml"), stack.command)
        self.assertEqual(stack.command[-2:], ["-f", str(active)])

    def test_export_stops_and_verifies_before_passing_assertion(self):
        stack = mock.Mock()
        stack.inventory.side_effect = [{operations.BROWSER: {"running": True}},
                                       {operations.BROWSER: {"running": False}}]
        operations.operate(stack, "export", "https://youtu.be/example")
        self.assertEqual(stack.compose.call_args_list, [
            mock.call("stop", "--timeout", "30", operations.BROWSER),
            mock.call("exec", "-T", "bot", "python", "/app/nas_youtube_auth.py", "--browser-stopped",
                      "--probe-url", "https://youtu.be/example"),
        ])

    def test_browser_failure_blocks_export(self):
        stack = mock.Mock()
        stack.inventory.return_value = {operations.BROWSER: {"running": True}}
        with self.assertRaisesRegex(RuntimeError, "Cannot confirm"):
            operations.operate(stack, "export", "https://youtu.be/example")
        self.assertEqual(stack.compose.call_count, 1)

    def test_status_is_read_only(self):
        stack = mock.Mock()
        operations.operate(stack, "status", None)
        stack.inventory.assert_not_called()
        stack.compose.assert_called_once_with("exec", "-T", "bot", "python", "/app/nas_youtube_auth.py", "--status")

    def test_probe_link_is_public_https_youtube(self):
        import argparse
        for url in ("http://youtu.be/example", "https://example.test/", "https://user:pass@youtube.com/", "https://youtube.com:8443/"):
            with self.assertRaises(argparse.ArgumentTypeError):
                operations.public_probe_url(url)


if __name__ == "__main__":
    unittest.main()
