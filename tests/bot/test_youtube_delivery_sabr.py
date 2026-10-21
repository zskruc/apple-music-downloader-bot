from __future__ import annotations

import contextlib
import hashlib
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


BOT_ROOT = Path(__file__).resolve().parents[2] / "bot"
sys.path.insert(0, str(BOT_ROOT))
mutagen = types.ModuleType("mutagen")
mutagen.File = lambda *_args, **_kwargs: None
mutagen_mp4 = types.ModuleType("mutagen.mp4")
mutagen_mp4.MP4 = object
sys.modules.setdefault("mutagen", mutagen)
sys.modules.setdefault("mutagen.mp4", mutagen_mp4)
youtube = importlib.import_module("youtube_ingest")
runner = importlib.import_module("yt_dlp_readonly_cookies")
auth = importlib.import_module("nas_youtube_auth")


class YouTubeSabrDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tool = self.root / "synthetic-tool"
        self.cookie_path = self.root / "cookies.txt"
        self.cookie_path.write_text("synthetic-session", encoding="utf-8")

    def test_disabled_or_missing_tool_does_not_require_sabr_dependency(self) -> None:
        self.tool.write_bytes(b"not a trusted tool")
        with (
            mock.patch.object(youtube, "SABR_TOOL_PATH", self.tool),
            mock.patch.object(youtube, "SABR_FALLBACK_ENABLED", False),
        ):
            self.assertIsNone(youtube._sabr_tool_path())
        self.tool.unlink()
        with (
            mock.patch.object(youtube, "SABR_TOOL_PATH", self.tool),
            mock.patch.object(youtube, "SABR_FALLBACK_ENABLED", True),
        ):
            self.assertIsNone(youtube._sabr_tool_path())

    def test_wrong_tool_digest_is_rejected_by_engine_and_runner(self) -> None:
        self.tool.write_bytes(b"not a trusted tool")
        module = types.ModuleType("yt_dlp")
        module.main = mock.Mock()
        with (
            mock.patch.object(youtube, "SABR_TOOL_PATH", self.tool),
            mock.patch.object(youtube, "SABR_FALLBACK_ENABLED", True),
            mock.patch.dict(sys.modules, {"yt_dlp": module}),
        ):
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "完整性校验失败"):
                youtube._sabr_tool_path()
            with self.assertRaisesRegex(SystemExit, "完整性校验失败"):
                runner.main(["--sabr-tool", str(self.tool), "--version"])
        module.main.assert_not_called()
        self.assertEqual(youtube.SABR_TOOL_SHA256, runner.SABR_TOOL_SHA256)

    def test_missing_runner_tool_is_rejected_before_importing_engine(self) -> None:
        module = types.ModuleType("yt_dlp")
        module.main = mock.Mock()
        with mock.patch.dict(sys.modules, {"yt_dlp": module}):
            with self.assertRaisesRegex(SystemExit, "完整性校验失败"):
                runner.main(["--sabr-tool", str(self.tool), "--version"])
            with self.assertRaisesRegex(SystemExit, "缺少"):
                runner.main(["--sabr-tool"])
        module.main.assert_not_called()

    def test_enabled_verified_tool_adds_one_conditional_cookie_strategy(self) -> None:
        self.tool.write_bytes(b"synthetic verified archive")
        digest = hashlib.sha256(self.tool.read_bytes()).hexdigest()
        with (
            mock.patch.object(youtube, "SABR_TOOL_PATH", self.tool),
            mock.patch.object(youtube, "SABR_TOOL_SHA256", digest),
            mock.patch.object(youtube, "SABR_FALLBACK_ENABLED", True),
            mock.patch.object(youtube, "COOKIE_PATH", self.cookie_path),
            mock.patch.object(youtube, "PO_TOKEN_ENABLED", True),
            mock.patch.object(youtube, "PO_TOKEN_PROVIDER_URL", "http://provider.example:4416"),
            mock.patch.object(youtube, "_cookie_health", return_value=({"SID"}, set())),
        ):
            strategies = youtube._youtube_auth_strategies()
        self.assertEqual(
            [strategy.name for strategy in strategies],
            ["cookies-po-token", "cookies-embedded", "cookies-sabr", "anonymous-embedded", "anonymous-po-token"],
        )
        strategy = strategies[2]
        self.assertTrue(strategy.use_cookies and strategy.use_po_token and strategy.use_sabr)
        self.assertEqual(strategy.player_client, "web")

    def test_missing_cookie_or_disabled_po_does_not_consult_sabr_tool(self) -> None:
        for cookie_ready, po_enabled in ((False, True), (True, False)):
            with (
                self.subTest(cookie_ready=cookie_ready, po_enabled=po_enabled),
                mock.patch.object(youtube, "COOKIE_PATH", self.cookie_path),
                mock.patch.object(youtube, "PO_TOKEN_ENABLED", po_enabled),
                mock.patch.object(youtube, "_cookie_health", return_value=({"SID"}, set() if cookie_ready else {"SSID"})),
                mock.patch.object(youtube, "_sabr_tool_path") as check,
            ):
                strategies = youtube._youtube_auth_strategies()
            check.assert_not_called()
            self.assertNotIn("cookies-sabr", [strategy.name for strategy in strategies])

    def test_sabr_command_keeps_cookie_safe_runner_and_high_quality_client(self) -> None:
        strategy = youtube.YouTubeAuthStrategy("cookies-sabr", True, True, "web", True)
        with (
            mock.patch.object(youtube, "_sabr_tool_path", return_value=self.tool),
            mock.patch.object(youtube, "PROXY_URL", "http://proxy.example:7890"),
            mock.patch.object(youtube, "LARGE_DOWNLOAD_PROXY_URL", ""),
        ):
            command = youtube._common_yt_dlp_args(strategy)
        self.assertEqual(Path(command[1]).name, "yt_dlp_readonly_cookies.py")
        self.assertEqual(command[2:4], ["--sabr-tool", str(self.tool)])
        self.assertIn("youtube:player_client=web;webpage_client=web;formats=duplicate", command)
        self.assertEqual(command[command.index("--proxy") + 1], "http://proxy.example:7890")
        self.assertIn("--cookies", command)
        with mock.patch.object(youtube, "_sabr_tool_path", return_value=None):
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "不可用"):
                youtube._common_yt_dlp_args(strategy)

    def test_two_retry_cycles_attempt_sabr_media_only_once(self) -> None:
        commands: list[list[str]] = []

        class FailedProcess:
            def __init__(self, command: list[str], **_kwargs: object) -> None:
                commands.append(command)
                self.stdout = iter(["ERROR: Requested format is not available\n"])

            def poll(self) -> int:
                return 1

            def wait(self) -> int:
                return 1

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                pass

        strategies = [
            youtube.YouTubeAuthStrategy("cookies-po-token", True, True),
            youtube.YouTubeAuthStrategy("cookies-sabr", True, True, "web", True),
            youtube.YouTubeAuthStrategy("anonymous-embedded", False, False),
        ]
        probe = youtube.ProbeResult("https://www.youtube.com/watch?v=example", "Example", "Channel", 1, False)
        with contextlib.ExitStack() as stack:
            for name, suffix in (("STATE_DIR", "state"), ("TEMP_ROOT", "tmp"), ("AUDIO_ROOT", "audio"), ("VIDEO_ROOT", "video")):
                stack.enter_context(mock.patch.object(youtube, name, self.root / suffix))
            stack.enter_context(mock.patch.object(youtube, "HIGH_QUALITY_RETRY_MAX_CYCLES", 2))
            stack.enter_context(mock.patch.object(youtube, "VIDEO_MAX_HEIGHT", 0))
            stack.enter_context(mock.patch.object(youtube, "probe_url", return_value=probe))
            stack.enter_context(mock.patch.object(youtube, "_ordered_auth_strategies", return_value=strategies))
            stack.enter_context(mock.patch.object(youtube, "_sabr_tool_path", return_value=self.tool))
            stack.enter_context(mock.patch.object(youtube, "_ensure_po_token_provider"))
            stack.enter_context(mock.patch.object(youtube, "_estimate_download_bytes", return_value=1))
            stack.enter_context(mock.patch.object(youtube, "_wait_retry"))
            stack.enter_context(mock.patch.object(youtube.subprocess, "Popen", side_effect=FailedProcess))
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "2 轮尝试失败"):
                youtube._download_once("youtube_video", probe.url, "sabr-once", self.root / "logs")
        self.assertEqual(len(commands), 5)
        self.assertEqual(sum("--sabr-tool" in command for command in commands), 1)
        self.assertTrue(all(command[command.index("-f") + 1] == "bestvideo+bestaudio" for command in commands))

    def test_candidate_probe_selects_only_strict_cookie_strategy_and_restores_settings(self) -> None:
        old_cookie = youtube.COOKIE_PATH
        old_refresh = youtube.AUTH_AUTO_REFRESH_DISABLED
        for po_enabled, tool, expected in (
            (True, self.tool, "cookies-sabr"),
            (True, None, "cookies-po-token"),
            (False, self.tool, "cookies-standard"),
        ):
            with (
                self.subTest(expected=expected),
                mock.patch.object(youtube, "PO_TOKEN_ENABLED", po_enabled),
                mock.patch.object(youtube, "_sabr_tool_path", return_value=tool),
                mock.patch.object(youtube, "probe_url") as probe,
            ):
                def check(_url: str, **options: object) -> object:
                    self.assertEqual(youtube.COOKIE_PATH, self.cookie_path)
                    self.assertTrue(youtube.AUTH_AUTO_REFRESH_DISABLED)
                    self.assertEqual(options, {"preferred_auth_strategy": expected, "allow_auth_fallback": False})
                    return object()
                probe.side_effect = check
                auth.probe_candidate(self.cookie_path, "https://youtu.be/example")
                probe.assert_called_once()
            self.assertEqual(youtube.COOKIE_PATH, old_cookie)
            self.assertEqual(youtube.AUTH_AUTO_REFRESH_DISABLED, old_refresh)

    def test_candidate_tool_integrity_failure_never_falls_back_to_anonymous(self) -> None:
        old_cookie = youtube.COOKIE_PATH
        old_refresh = youtube.AUTH_AUTO_REFRESH_DISABLED
        with (
            mock.patch.object(youtube, "PO_TOKEN_ENABLED", True),
            mock.patch.object(youtube, "_sabr_tool_path", side_effect=youtube.YouTubeIngestError("完整性校验失败")),
            mock.patch.object(youtube, "probe_url") as probe,
        ):
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "完整性校验失败"):
                auth.probe_candidate(self.cookie_path, "https://youtu.be/example")
        probe.assert_not_called()
        self.assertEqual(youtube.COOKIE_PATH, old_cookie)
        self.assertEqual(youtube.AUTH_AUTO_REFRESH_DISABLED, old_refresh)


if __name__ == "__main__":
    unittest.main()
