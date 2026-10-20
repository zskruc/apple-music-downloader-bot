from __future__ import annotations

import ast
import contextlib
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


class YouTubeRecoveryDeliveryTests(unittest.TestCase):
    def test_retry_wait_observes_cancellation(self) -> None:
        with mock.patch.object(youtube.time, "sleep") as sleep:
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "取消"):
                youtube._wait_retry(60, lambda: True)
        sleep.assert_not_called()

    def test_cancelled_job_does_not_probe_or_spawn_process(self) -> None:
        with (
            mock.patch.object(youtube, "probe_url") as probe,
            mock.patch.object(youtube.subprocess, "Popen") as process,
        ):
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "取消"):
                youtube.download(
                    "youtube_video",
                    "https://www.youtube.com/watch?v=example",
                    "cancelled",
                    Path("unused"),
                    cancel_observer=lambda: True,
                )
        probe.assert_not_called()
        process.assert_not_called()

    def test_two_failed_quality_cycles_end_without_format_downgrade(self) -> None:
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
            youtube.YouTubeAuthStrategy("anonymous-embedded", False, False),
        ]
        probe = youtube.ProbeResult(
            "https://www.youtube.com/watch?v=example", "Example", "Channel", 1, False
        )
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root = Path(temporary)
            for name, suffix in (
                ("STATE_DIR", "state"),
                ("TEMP_ROOT", "tmp"),
                ("AUDIO_ROOT", "audio"),
                ("VIDEO_ROOT", "video"),
            ):
                stack.enter_context(mock.patch.object(youtube, name, root / suffix))
            stack.enter_context(mock.patch.object(youtube, "HIGH_QUALITY_RETRY_MAX_CYCLES", 2))
            stack.enter_context(mock.patch.object(youtube, "VIDEO_MAX_HEIGHT", 0))
            stack.enter_context(mock.patch.object(youtube, "probe_url", return_value=probe))
            stack.enter_context(mock.patch.object(youtube, "_ordered_auth_strategies", return_value=strategies))
            stack.enter_context(mock.patch.object(youtube, "_ensure_po_token_provider"))
            stack.enter_context(mock.patch.object(youtube, "_estimate_download_bytes", return_value=1))
            stack.enter_context(mock.patch.object(youtube, "_wait_retry"))
            stack.enter_context(mock.patch.object(youtube.subprocess, "Popen", side_effect=FailedProcess))
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "2 轮尝试失败"):
                youtube._download_once("youtube_video", probe.url, "bounded", root / "logs")
            self.assertEqual(list((root / "tmp").iterdir()), [])
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertEqual(command[command.index("-f") + 1], "bestvideo+bestaudio")
            self.assertNotIn("--recode-video", command)
            self.assertNotIn("--sabr-tool", command)

    def test_one_cookie_refresh_and_one_retry_then_failure_is_returned(self) -> None:
        failure = youtube.YouTubeIngestError("Requested format is not available")
        with (
            mock.patch.object(youtube, "AUTH_AUTO_REFRESH_DISABLED", False),
            mock.patch.object(youtube, "_download_once", side_effect=failure) as download,
            mock.patch.object(youtube, "_refresh_youtube_auth", return_value=(True, "refreshed")) as refresh,
        ):
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "后续队列可继续"):
                youtube.download(
                    "youtube_video",
                    "https://www.youtube.com/watch?v=example",
                    "recovery",
                    Path("unused"),
                )
        self.assertEqual(download.call_count, 2)
        self.assertEqual(refresh.call_count, 1)

    def test_cancellation_after_browser_refresh_prevents_new_download(self) -> None:
        cancelled = False

        def refresh(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            nonlocal cancelled
            cancelled = True
            return True, "refreshed"

        with (
            mock.patch.object(youtube, "AUTH_AUTO_REFRESH_DISABLED", False),
            mock.patch.object(
                youtube,
                "_download_once",
                side_effect=youtube.YouTubeIngestError("Sign in to confirm you are not a bot"),
            ) as download,
            mock.patch.object(youtube, "_refresh_youtube_auth", side_effect=refresh),
        ):
            with self.assertRaisesRegex(youtube.YouTubeIngestError, "取消"):
                youtube.download(
                    "youtube_video",
                    "https://www.youtube.com/watch?v=example",
                    "cancelled-after-refresh",
                    Path("unused"),
                    cancel_observer=lambda: cancelled,
                )
        self.assertEqual(download.call_count, 1)

    def test_no_sabr_tool_is_required_for_standard_strategy(self) -> None:
        strategy = youtube.YouTubeAuthStrategy("anonymous-standard", False, False)
        command = youtube._common_yt_dlp_args(strategy, "")
        self.assertEqual(Path(command[1]).name, "yt_dlp_readonly_cookies.py")
        self.assertNotIn("--sabr-tool", command)
        self.assertFalse(any("vendor/" in argument for argument in command))

    def test_audio_and_video_keep_their_separate_format_policies(self) -> None:
        audio = youtube._format_selection_args("youtube_audio")
        with mock.patch.object(youtube, "VIDEO_MAX_HEIGHT", 1080):
            video = youtube._format_selection_args("youtube_video")
        self.assertEqual(audio, ["-f", "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio"])
        self.assertEqual(video[1], "bestvideo[height<=?1080]+bestaudio")
        self.assertIn("--format-sort-force", video)

    def test_proxy_credentials_are_removed_from_command_and_upstream_error_text(self) -> None:
        text = (
            "--proxy http://sample-user:sample-pass@proxy.example:7890 "
            "ERROR: Unable to connect to socks5h://another-user:p%40ss@proxy.example:7891 "
            "http://third-user:raw@password@proxy.example:7892"
        )
        redacted = youtube._redact_proxy_credentials(text)
        self.assertNotIn("sample-user", redacted)
        self.assertNotIn("sample-pass", redacted)
        self.assertNotIn("another-user", redacted)
        self.assertNotIn("p%40ss", redacted)
        self.assertNotIn("raw@password", redacted)
        self.assertIn("proxy.example:7890", redacted)
        self.assertIn("proxy.example:7891", redacted)
        self.assertIn("proxy.example:7892", redacted)
        self.assertEqual(youtube._last_error(text), redacted)


class YouTubeBotCancellationWiringTests(unittest.TestCase):
    def test_online_job_cancellation_reaches_youtube_only(self) -> None:
        source = ast.parse((BOT_ROOT / "ingest_bot.py").read_text())
        function = next(
            node for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name == "process_online_media_job"
        )
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias("annotations")], level=0),
            function,
        ], type_ignores=[])
        code = compile(ast.fix_missing_locations(module), str(BOT_ROOT / "ingest_bot.py"), "exec")
        for kind in ("youtube_audio", "youtube_video", "bilibili_audio", "bilibili_video"):
            with self.subTest(kind=kind):
                state = {"shutdown": False, "task_stop": False}
                captured: list[dict[str, object]] = []
                observed_cancellation: list[bool] = []

                def downloader(*_args: object, **options: object) -> None:
                    captured.append(options)
                    if kind.startswith("youtube_"):
                        cancellation = options["cancel_observer"]
                        observed_cancellation.append(cancellation())
                        state["shutdown"] = True
                        observed_cancellation.append(cancellation())
                        state["shutdown"] = False
                        state["task_stop"] = True
                        observed_cancellation.append(cancellation())
                    raise RuntimeError("synthetic cancelled job")

                namespace = {
                    "download_youtube": downloader,
                    "download_bilibili": downloader,
                    "display_youtube_path": mock.Mock(),
                    "display_bilibili_path": mock.Mock(),
                    "online_process_observer": mock.Mock(),
                    "online_progress_observer": mock.Mock(),
                    "online_video_library_lock": contextlib.nullcontext(),
                    "LOG_DIR": Path("unused"),
                    "stop_event": types.SimpleNamespace(is_set=lambda: state["shutdown"]),
                    "task_should_stop": lambda _job: state["task_stop"],
                    "clear_active_task_process": mock.Mock(),
                    "task_status": lambda _job: "stopped",
                    "update_task_card": mock.Mock(),
                    "time": types.SimpleNamespace(time=lambda: 0),
                }
                exec(code, namespace)
                job = types.SimpleNamespace(kind=kind, urls=["https://invalid.example/"], label="example", task_id=1)
                namespace["process_online_media_job"](job)
                self.assertEqual(len(captured), 1)
                self.assertEqual("cancel_observer" in captured[0], kind.startswith("youtube_"))
                self.assertEqual(observed_cancellation, [False, True, True] if kind.startswith("youtube_") else [])


if __name__ == "__main__":
    unittest.main()
