from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stat
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location("lx_service_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load("check")
initializer = load("initialize")


class InitializeTests(unittest.TestCase):
    def test_secret_and_config_match_and_existing_installation_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, auth = root / "state", root / "private/auth"
            initializer.initialize(state, auth)
            config = state / "data/config.js"
            settings = json.loads(config.read_text().removeprefix("module.exports = ").removesuffix(";\n"))
            secret = auth.read_text().strip()
            self.assertGreaterEqual(len(secret), 48)
            self.assertEqual(settings["frontend.password"], secret)
            self.assertEqual(settings["users"], [])
            self.assertTrue(settings["player.enableAuth"])
            self.assertEqual(stat.S_IMODE(auth.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            original = config.read_bytes(), auth.read_bytes()
            with self.assertRaises(ValueError):
                initializer.initialize(state, auth)
            self.assertEqual(original, (config.read_bytes(), auth.read_bytes()))

    def test_refuses_nonempty_data_without_leaving_an_auth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state/data").mkdir(parents=True)
            (root / "state/data/users.json").write_text("[]")
            with self.assertRaises(ValueError):
                initializer.initialize(root / "state", root / "auth")
            self.assertFalse((root / "auth").exists())


class CheckTests(unittest.TestCase):
    def run_server(self, *, redirect=False, tasks=None):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get("x-frontend-auth"), self.headers.get("x-user-name")))
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                    return
                if self.path == "/api/config" and self.headers.get("x-frontend-auth") != "test-only-credential":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"private failure body must not be exposed")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = {} if self.path.endswith("/config") else {"success": True, "data": tasks or []}
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", calls

    def test_authenticated_read_only_check_counts_busy_tasks_without_exposing_payloads(self):
        url, calls = self.run_server(tasks=[
            {"status": "finished", "name": "private song"},
            {"status": "error"}, {"status": "cancelled"},
            {"status": "downloading"}, {"status": "pending"},
        ])
        result = checker.check(url, "test-only-credential")
        self.assertEqual(result, {"api": "ready", "queue_tasks": 5, "busy_tasks": 2})
        self.assertEqual([call[0] for call in calls], ["/api/config", "/api/music/cache/queue"])
        self.assertTrue(all(call[1:] == ("test-only-credential", "default") for call in calls))

    def test_redirect_is_rejected_before_credential_can_be_forwarded(self):
        url, calls = self.run_server(redirect=True)
        with self.assertRaisesRegex(checker.CheckError, "redirected"):
            checker.check(url, "test-only-credential")
        self.assertEqual(len(calls), 1)

    def test_rejects_invalid_admin_secret_even_when_public_queue_would_be_readable(self):
        url, calls = self.run_server()
        with self.assertRaises(checker.CheckError) as caught:
            checker.check(url, "incorrect-test-secret")
        self.assertEqual(str(caught.exception), "/api/config: HTTP 401")
        self.assertEqual([call[0] for call in calls], ["/api/config"])

    def test_multiline_secret_and_url_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth"
            path.write_text("line-one\nline-two")
            with self.assertRaises(checker.CheckError):
                checker.read_auth(path)
        with self.assertRaises(checker.CheckError):
            checker.check("http://user:password@127.0.0.1:1", "test-only")


if __name__ == "__main__":
    unittest.main()
