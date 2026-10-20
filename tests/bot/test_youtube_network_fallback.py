from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_ROOT = PROJECT_ROOT / "bot"
sys.path.insert(0, str(BOT_ROOT))

mutagen = types.ModuleType("mutagen")
mutagen.File = lambda *_args, **_kwargs: None
mutagen_mp4 = types.ModuleType("mutagen.mp4")
mutagen_mp4.MP4 = object
sys.modules.setdefault("mutagen", mutagen)
sys.modules.setdefault("mutagen.mp4", mutagen_mp4)

youtube_ingest = importlib.import_module("youtube_ingest")


class YouTubeNetworkFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        # Routing behavior is independent of the deployment's opt-in proxies.
        for name, value in (
            ("PROXY_URL", "http://proxy.example:7890"),
            ("LARGE_DOWNLOAD_PROXY_URL", "http://proxy.example:7891"),
        ):
            patcher = mock.patch.object(youtube_ingest, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_parses_machine_readable_yt_dlp_progress(self) -> None:
        progress = youtube_ingest._parse_yt_dlp_progress(
            "__AMDL_PROGRESS__52428800|104857600|NA|5242880|10"
        )
        self.assertEqual(
            progress,
            {
                "phase": "下载中",
                "downloaded_bytes": 52_428_800,
                "total_bytes": 104_857_600,
                "total_estimated": 0,
                "percent": 50.0,
                "speed_bps": 5_242_880.0,
                "eta": "00:10",
            },
        )

    def test_tls_eof_is_retryable_network_failure(self) -> None:
        detail = (
            "[download] Got error: [SSL: UNEXPECTED_EOF_WHILE_READING] "
            "EOF occurred in violation of protocol (_ssl.c:1010)"
        )
        self.assertTrue(
            youtube_ingest._is_retryable_youtube_network_error(detail)
        )

    def test_auth_error_is_not_misclassified_as_network_failure(self) -> None:
        self.assertFalse(
            youtube_ingest._is_retryable_youtube_network_error(
                "Sign in to confirm you’re not a bot"
            )
        )

    def test_large_proxy_falls_back_to_default_proxy(self) -> None:
        self.assertEqual(
            youtube_ingest._download_proxy_candidates(
                youtube_ingest.LARGE_DOWNLOAD_PROXY_URL
            ),
            [
                youtube_ingest.LARGE_DOWNLOAD_PROXY_URL,
                youtube_ingest.PROXY_URL,
            ],
        )

    def test_proxy_candidates_are_unique_when_routes_match(self) -> None:
        original_large_proxy = youtube_ingest.LARGE_DOWNLOAD_PROXY_URL
        try:
            youtube_ingest.LARGE_DOWNLOAD_PROXY_URL = youtube_ingest.PROXY_URL
            self.assertEqual(
                youtube_ingest._download_proxy_candidates(
                    youtube_ingest.PROXY_URL
                ),
                [youtube_ingest.PROXY_URL],
            )
        finally:
            youtube_ingest.LARGE_DOWNLOAD_PROXY_URL = original_large_proxy

    def test_download_retries_tls_eof_on_fallback_route_without_deleting_part(
        self,
    ) -> None:
        commands: list[list[str]] = []
        partial_was_preserved: list[bool] = []

        class FakeProcess:
            def __init__(self, command: list[str], **_kwargs: object) -> None:
                commands.append(command)
                output_template = Path(command[command.index("-o") + 1])
                partial_path = output_template.parent / "video.part"
                if len(commands) == 1:
                    partial_path.write_bytes(b"partial")
                else:
                    partial_was_preserved.append(partial_path.is_file())
                self.stdout = iter(
                    [
                        "[download] Got error: [SSL: "
                        "UNEXPECTED_EOF_WHILE_READING] EOF occurred in "
                        "violation of protocol\n"
                    ]
                )

            def poll(self) -> int:
                return 1

            def wait(self) -> int:
                return 1

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        strategy = youtube_ingest.YouTubeAuthStrategy(
            name="anonymous-standard",
            use_cookies=False,
            use_po_token=False,
        )
        probe = youtube_ingest.ProbeResult(
            url="https://www.youtube.com/watch?v=test",
            title="Test",
            uploader="",
            item_count=1,
            is_collection=False,
            auth_strategy=strategy.name,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch.object(youtube_ingest, "STATE_DIR", root / "state"),
                mock.patch.object(youtube_ingest, "TEMP_ROOT", root / "tmp"),
                mock.patch.object(youtube_ingest, "AUDIO_ROOT", root / "audio"),
                mock.patch.object(youtube_ingest, "VIDEO_ROOT", root / "video"),
                mock.patch.object(youtube_ingest, "probe_url", return_value=probe),
                mock.patch.object(
                    youtube_ingest,
                    "_ordered_auth_strategies",
                    return_value=[strategy],
                ),
                mock.patch.object(
                    youtube_ingest,
                    "_estimate_download_bytes",
                    return_value=youtube_ingest.LARGE_DOWNLOAD_THRESHOLD_BYTES + 1,
                ),
                mock.patch.object(
                    youtube_ingest.subprocess,
                    "Popen",
                    side_effect=FakeProcess,
                ),
            ):
                with self.assertRaises(youtube_ingest.YouTubeIngestError):
                    youtube_ingest._download_once(
                        "youtube_video",
                        probe.url,
                        "network-fallback-test",
                        root / "logs",
                    )

        self.assertEqual(len(commands), 2)
        first_proxy = commands[0][commands[0].index("--proxy") + 1]
        second_proxy = commands[1][commands[1].index("--proxy") + 1]
        self.assertEqual(first_proxy, youtube_ingest.LARGE_DOWNLOAD_PROXY_URL)
        self.assertEqual(second_proxy, youtube_ingest.PROXY_URL)
        self.assertEqual(partial_was_preserved, [True])


if __name__ == "__main__":
    unittest.main()
