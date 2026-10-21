from __future__ import annotations

import importlib
import hashlib
import os
import signal
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock


BOT_ROOT = Path(__file__).resolve().parents[2] / "bot"
sys.path.insert(0, str(BOT_ROOT))
runner = importlib.import_module("yt_dlp_readonly_cookies")


class YouTubePrivateCookieRunnerTests(unittest.TestCase):
    def test_late_writer_cannot_overwrite_new_browser_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "cookies.txt"
            source.write_text("synthetic-session-before-refresh", encoding="utf-8")
            paths: list[Path] = []

            def engine(arguments: list[str]) -> int:
                private = Path(arguments[arguments.index("--cookies") + 1])
                paths.append(private)
                self.assertNotEqual(private, source)
                self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o700)
                self.assertEqual(private.read_text(), "synthetic-session-before-refresh")
                source.write_text("synthetic-new-browser-export", encoding="utf-8")
                private.write_text("synthetic-old-download-updates", encoding="utf-8")
                return 23

            module = types.ModuleType("yt_dlp")
            module.main = engine
            with mock.patch.dict(sys.modules, {"yt_dlp": module}):
                code = runner.main(["--cookies", str(source), "--version"])
            self.assertEqual(code, 23)
            self.assertEqual(source.read_text(), "synthetic-new-browser-export")
            self.assertFalse(paths[0].parent.exists())

    def test_equals_cookie_option_uses_private_copy_and_cleans_on_systemexit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "cookies.txt"
            source.write_text("synthetic-cookie", encoding="utf-8")
            paths: list[Path] = []

            def engine(arguments: list[str]) -> None:
                self.assertEqual(arguments[1:], ["--no-playlist", "https://invalid.example/"])
                private = Path(arguments[0].partition("=")[2])
                paths.append(private)
                self.assertEqual(private.read_text(), "synthetic-cookie")
                raise SystemExit(17)

            module = types.ModuleType("yt_dlp")
            module.main = engine
            with mock.patch.dict(sys.modules, {"yt_dlp": module}):
                with self.assertRaises(SystemExit) as exited:
                    runner.main([f"--cookies={source}", "--no-playlist", "https://invalid.example/"])
            self.assertEqual(exited.exception.code, 17)
            self.assertFalse(paths[0].parent.exists())
            self.assertEqual(source.read_text(), "synthetic-cookie")

    def test_public_content_keeps_arguments_and_exit_behavior(self) -> None:
        module = types.ModuleType("yt_dlp")
        module.main = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"yt_dlp": module}):
            self.assertEqual(runner.main(["--version"]), 0)
        module.main.assert_called_once_with(["--version"])

    def test_sabr_archive_engine_uses_private_snapshot_and_preserves_browser_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cookies.txt"
            source.write_text("synthetic-old-session", encoding="utf-8")
            tool = root / "synthetic-yt-dlp.zip"
            engine = (
                "from pathlib import Path\n"
                "def main(arguments):\n"
                " cookie = Path(arguments[arguments.index('--cookies') + 1])\n"
                " source = Path(arguments[arguments.index('--fixture-source') + 1])\n"
                " assert cookie != source\n"
                " assert cookie.stat().st_mode & 0o777 == 0o600\n"
                " assert cookie.parent.stat().st_mode & 0o777 == 0o700\n"
                " assert cookie.read_text() == 'synthetic-old-session'\n"
                " source.write_text('synthetic-new-browser-session')\n"
                " cookie.write_text('synthetic-older-download-update')\n"
                " print(cookie.parent, flush=True)\n"
                " return 23\n"
            )
            with zipfile.ZipFile(tool, "w") as archive:
                archive.writestr("yt_dlp/__init__.py", engine)
            digest = hashlib.sha256(tool.read_bytes()).hexdigest()
            bootstrap = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import yt_dlp_readonly_cookies as runner; "
                "runner.SABR_TOOL_SHA256 = sys.argv[2]; "
                "raise SystemExit(runner.main(sys.argv[3:]))"
            )
            completed = subprocess.run(
                [sys.executable, "-c", bootstrap, str(BOT_ROOT), digest,
                 "--sabr-tool", str(tool), "--cookies", str(source), "--fixture-source", str(source)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
            )
            self.assertEqual(completed.returncode, 23, completed.stderr)
            self.assertEqual(source.read_text(), "synthetic-new-browser-session")
            self.assertFalse(Path(completed.stdout.strip()).exists())

    def test_sigterm_preserves_signal_exit_and_removes_private_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "yt_dlp"
            package.mkdir()
            (package / "__init__.py").write_text(
                "import time\n"
                "def main(arguments):\n"
                " print(arguments[arguments.index('--cookies') + 1], flush=True)\n"
                " time.sleep(30)\n",
                encoding="utf-8",
            )
            source = root / "cookies.txt"
            source.write_text("synthetic-cookie", encoding="utf-8")
            process = subprocess.Popen(
                [sys.executable, str(BOT_ROOT / "yt_dlp_readonly_cookies.py"), "--cookies", str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONPATH": str(root)},
            )
            try:
                assert process.stdout is not None
                private = Path(process.stdout.readline().strip())
                self.assertTrue(private.is_file())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, -signal.SIGTERM)
                self.assertFalse(private.parent.exists())
                self.assertEqual(source.read_text(), "synthetic-cookie")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
