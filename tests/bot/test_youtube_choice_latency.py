from __future__ import annotations

import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_ROOT = PROJECT_ROOT / "bot"
sys.path.insert(0, str(BOT_ROOT))

IMPORT_STATE = tempfile.TemporaryDirectory()
os.environ.setdefault("AMDL_STATE_DIR", str(Path(IMPORT_STATE.name) / "state"))
os.environ.setdefault("AMDL_LOG_DIR", str(Path(IMPORT_STATE.name) / "logs"))
os.environ.setdefault("AMDL_TEMP_PATH", str(Path(IMPORT_STATE.name) / "tmp"))
os.environ.setdefault("YT_STATE_DIR", str(Path(IMPORT_STATE.name) / "youtube"))
os.environ.setdefault("BILI_STATE_DIR", str(Path(IMPORT_STATE.name) / "bilibili"))

mutagen = types.ModuleType("mutagen")
mutagen.File = lambda *_args, **_kwargs: None
mutagen_mp4 = types.ModuleType("mutagen.mp4")
mutagen_mp4.MP4 = object
sys.modules.setdefault("mutagen", mutagen)
sys.modules.setdefault("mutagen.mp4", mutagen_mp4)

ingest_bot = importlib.import_module("ingest_bot")


class YouTubeChoiceLatencyTests(unittest.TestCase):
    def setUp(self) -> None:
        with ingest_bot.youtube_choice_lock:
            ingest_bot.youtube_choice_cache.clear()

    def test_single_video_uses_quick_metadata_without_full_probe(self) -> None:
        url = "https://youtu.be/example123"
        with (
            mock.patch.object(ingest_bot, "validate_youtube_url", return_value=url),
            mock.patch.object(
                ingest_bot,
                "is_youtube_collection_hint",
                return_value=False,
            ),
            mock.patch.object(
                ingest_bot,
                "quick_youtube_metadata",
                return_value=("快速标题", "快速频道"),
            ),
            mock.patch.object(
                ingest_bot,
                "probe_youtube_url",
                side_effect=AssertionError("single video must not run full probe"),
            ),
        ):
            _key, choice = ingest_bot.cache_youtube_choice("-1001", "42", url)

        self.assertEqual(choice.title, "快速标题")
        self.assertEqual(choice.uploader, "快速频道")
        self.assertEqual(choice.item_count, 1)
        self.assertFalse(choice.is_collection)
        self.assertFalse(choice.truncated)

    def test_quick_metadata_failure_still_returns_immediate_choice(self) -> None:
        url = "https://youtu.be/example123"
        with (
            mock.patch.object(ingest_bot, "validate_youtube_url", return_value=url),
            mock.patch.object(
                ingest_bot,
                "is_youtube_collection_hint",
                return_value=False,
            ),
            mock.patch.object(
                ingest_bot,
                "quick_youtube_metadata",
                return_value=("", ""),
            ),
            mock.patch.object(
                ingest_bot,
                "probe_youtube_url",
                side_effect=AssertionError("fallback must remain non-blocking"),
            ),
        ):
            _key, choice = ingest_bot.cache_youtube_choice("-1001", "42", url)

        self.assertIn("标题将在下载时读取", choice.title)
        self.assertEqual(choice.item_count, 1)

    def test_collection_keeps_full_safety_probe(self) -> None:
        url = "https://www.youtube.com/playlist?list=example"
        probe = types.SimpleNamespace(
            url=url,
            title="播放列表",
            uploader="频道",
            item_count=12,
            is_collection=True,
            truncated=False,
        )
        with (
            mock.patch.object(ingest_bot, "validate_youtube_url", return_value=url),
            mock.patch.object(
                ingest_bot,
                "is_youtube_collection_hint",
                return_value=True,
            ),
            mock.patch.object(
                ingest_bot,
                "probe_youtube_url",
                return_value=probe,
            ) as full_probe,
            mock.patch.object(
                ingest_bot,
                "quick_youtube_metadata",
                side_effect=AssertionError("collections require full probe"),
            ),
        ):
            _key, choice = ingest_bot.cache_youtube_choice("-1001", "42", url)

        full_probe.assert_called_once_with(url)
        self.assertEqual(choice.item_count, 12)
        self.assertTrue(choice.is_collection)


if __name__ == "__main__":
    unittest.main()
