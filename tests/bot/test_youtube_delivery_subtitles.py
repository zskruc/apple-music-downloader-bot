from __future__ import annotations

import importlib
import io
import json
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


class YouTubeSubtitleDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def info(self, **overrides: object) -> dict[str, object]:
        info: dict[str, object] = {"id": "example", "language": "en"}
        info.update(overrides)
        (self.root / "example.info.json").write_text(
            json.dumps(info), encoding="utf-8"
        )
        return info

    def caption(self, language: str, text: str) -> Path:
        path = self.root / f"example.{language}.srt"
        youtube._write_srt(path, [youtube.SubtitleCue(0, 2500, text)])
        return path

    def test_prefers_manual_chinese_and_original_language(self) -> None:
        self.info(
            language="ja",
            subtitles={"zh-Hans": [], "ja": [], "en": []},
            automatic_captions={"zh-Hans": [], "ja-orig": [], "en": []},
        )
        self.assertEqual(
            youtube._desired_subtitle_downloads(self.root),
            {"manual": ["zh-Hans", "ja"], "automatic": []},
        )

    def test_requests_automatic_chinese_when_manual_chinese_missing(self) -> None:
        self.info(
            subtitles={"en": []},
            automatic_captions={"zh-Hans": [], "en-orig": []},
        )
        self.assertEqual(
            youtube._desired_subtitle_downloads(self.root),
            {"manual": ["en"], "automatic": ["zh-Hans"]},
        )

    def test_original_track_identifies_source_when_language_is_unknown(self) -> None:
        self.info(
            language="",
            subtitles={"zh-Hans": []},
            automatic_captions={"fr-orig": [], "en": []},
        )
        self.assertEqual(
            youtube._desired_subtitle_downloads(self.root),
            {"manual": ["zh-Hans"], "automatic": ["fr-orig"]},
        )

    def test_missing_info_uses_configured_automatic_languages(self) -> None:
        with mock.patch.object(youtube, "AUTOMATIC_SUBTITLE_LANGUAGES", "zh-Hans,en-orig"):
            self.assertEqual(
                youtube._desired_subtitle_downloads(self.root),
                {"manual": [], "automatic": ["zh-Hans", "en-orig"]},
            )

    def test_chinese_video_keeps_one_default_srt(self) -> None:
        info = self.info(language="zh-CN")
        self.caption("zh-Hans", "这是中文字幕。")
        self.caption("en", "English translation.")
        youtube._prepare_video_subtitles(info, self.root, io.StringIO())
        self.assertEqual(
            sorted(path.name for path in self.root.glob("*.srt")),
            ["example.zh-CN.default.srt"],
        )
        self.assertEqual(list(self.root.glob("*.ass")), [])

    def test_foreign_video_gets_chinese_and_original_language_ass(self) -> None:
        info = self.info(language="ja")
        self.caption("zh-Hans", "这是中文字幕。")
        self.caption("ja", "日本語の字幕です。")
        self.caption("en", "English translation.")
        youtube._prepare_video_subtitles(info, self.root, io.StringIO())
        output = self.root / "example.zh-CN.default.ass"
        self.assertTrue(output.is_file())
        text = output.read_text(encoding="utf-8")
        self.assertIn("中文字幕", text)
        self.assertIn("日本語", text)
        self.assertNotIn("English translation", text)
        self.assertEqual(list(self.root.glob("*.srt")), [])

    def test_missing_original_language_keeps_chinese_without_wrong_translation(self) -> None:
        info = self.info(language="fr")
        self.caption("zh-Hans", "这是中文字幕。")
        self.caption("en", "English translation.")
        youtube._prepare_video_subtitles(info, self.root, io.StringIO())
        output = self.root / "example.zh-CN.default.srt"
        self.assertTrue(output.is_file())
        self.assertNotIn("English", output.read_text(encoding="utf-8"))
        self.assertEqual(list(self.root.glob("*.ass")), [])

    def test_foreign_only_captions_are_not_published_as_chinese_default(self) -> None:
        info = self.info()
        self.caption("en", "Foreign-only captions.")
        log = io.StringIO()
        youtube._prepare_video_subtitles(info, self.root, log)
        self.assertEqual(list(self.root.glob("*.srt")), [])
        self.assertEqual(list(self.root.glob("*.ass")), [])
        self.assertIn("no usable Chinese captions", log.getvalue())

    def test_ass_cues_are_clipped_before_next_cue_without_invalid_duration(self) -> None:
        cues, clipped, dropped = youtube._synchronize_bilingual_cues(
            [
                youtube.BilingualSubtitleCue(0, 3000, "First", "第一句"),
                youtube.BilingualSubtitleCue(2000, 4000, "Second", "第二句"),
            ]
        )
        self.assertEqual((clipped, dropped), (1, 0))
        self.assertLess(cues[0].end_ms, cues[1].start_ms)
        self.assertTrue(all(cue.end_ms > cue.start_ms for cue in cues))

    def test_caption_429_retries_then_succeeds_without_fetching_media(self) -> None:
        self.info(subtitles={"zh-Hans": [], "en": []})
        strategy = youtube.YouTubeAuthStrategy("anonymous-standard", False, False)
        failure = types.SimpleNamespace(returncode=1, stdout="HTTP Error 429")
        success = types.SimpleNamespace(returncode=0, stdout="")
        with (
            mock.patch.object(youtube, "_ordered_auth_strategies", return_value=[strategy]),
            mock.patch.object(youtube.subprocess, "run", side_effect=[failure, success]) as run,
            mock.patch.object(youtube.time, "sleep") as sleep,
        ):
            outcomes = youtube._download_video_subtitles(
                "https://www.youtube.com/watch?v=example",
                self.root,
                False,
                strategy,
                io.StringIO(),
            )
        self.assertEqual(outcomes, {"manual": True, "automatic": True})
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(youtube.SUBTITLE_RATE_LIMIT_RETRY_SECONDS)
        for call in run.call_args_list:
            self.assertIn("--skip-download", call.args[0])
            self.assertIn("--no-write-auto-subs", call.args[0])

    def test_caption_timeout_returns_failed_track_and_leaves_media_processing_possible(self) -> None:
        self.info(subtitles={"zh-Hans": []})
        strategy = youtube.YouTubeAuthStrategy("anonymous-standard", False, False)
        with (
            mock.patch.object(youtube, "_ordered_auth_strategies", return_value=[strategy]),
            mock.patch.object(
                youtube.subprocess,
                "run",
                side_effect=youtube.subprocess.TimeoutExpired("yt-dlp", 1),
            ),
        ):
            outcomes = youtube._download_video_subtitles(
                "https://www.youtube.com/watch?v=example",
                self.root,
                False,
                strategy,
                io.StringIO(),
            )
        self.assertEqual(outcomes, {"manual": False, "automatic": True})


if __name__ == "__main__":
    unittest.main()
