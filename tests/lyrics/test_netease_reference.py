from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import dataclass

from word_lyrics_automation.providers.netease import NeteaseSong
from word_lyrics_automation.resolver import LegacyGateway, SourceResolutionEngine


@dataclass
class Track:
    path: Path
    title: str = "Test Song"
    artist: str = "Local Artist"
    album: str = "Test Release"
    duration: float = 60
    song_id: str = ""
    isrc: str = ""


class NeteaseReferenceTests(unittest.TestCase):
    def gateway(self, media):
        track = Track(media)
        gateway = object.__new__(LegacyGateway)
        gateway.inspect = lambda *_: track
        gateway.references = lambda *_: []
        gateway.existing_word = lambda *_: None
        gateway.alignment_payload = lambda *_: {"reference_text": "", "reference_lines": []}
        gateway.localized_artist_identity = lambda *_: {"names": ["Verified Artist"], "evidence": []}
        gateway.instrumental_result = lambda *_, **__: None
        gateway.legacy = SimpleNamespace(parse_lrc=lambda _: SimpleNamespace(lines=[
            SimpleNamespace(text="作曲 : Test", begin=0, end=None),
            *(SimpleNamespace(text=text, begin=index * 10, end=None) for index, text in enumerate([
                "This is our first synthetic lyric line", "A second line from this test fixture",
                "The third line has no copyrighted text", "The last line ends our invented verse"], 1))]))
        return gateway

    def engine(self, gateway, *, album="Test Release", record=None):
        song = NeteaseSong("test", "Test Song", "Verified Artist", album, 60000)
        amll = SimpleNamespace(get=lambda **_: None, search=lambda **_: [])
        return SourceResolutionEngine(gateway, amll_client=amll,
            netease_search_function=lambda *_, **__: [song],
            netease_record_function=lambda _: record or {"lrc": {"lyric": "fixture"}},
            kugou_search_function=lambda **_: [])

    def test_exact_native_line_text_reaches_acoustic_alignment_and_preserves_every_line(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "song.m4a";media.write_bytes(b"audio")
            gateway = self.gateway(media)
            result = self.engine(gateway).resolve(media_path=media, metadata={}, expected_fingerprint={})
            self.assertEqual(result.alignment_payload["reference_provider"], "netease_exact_lrc")
            self.assertEqual(len(result.alignment_payload["reference_lines"]), 4)
            self.assertNotIn("作曲", result.alignment_payload["reference_text"])
            self.assertEqual(result.reason, "trusted_reference_needs_acoustic_alignment")

    def test_album_mismatch_and_instrumental_flag_do_not_become_sung_references(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "song.m4a";media.write_bytes(b"audio")
            gateway = self.gateway(media)
            for kwargs in [{"album": "Different Release"}, {"record": {"pureMusic": True}}]:
                with self.subTest(kwargs=kwargs):
                    result = self.engine(gateway, **kwargs).resolve(media_path=media, metadata={}, expected_fingerprint={})
                    self.assertFalse(result.alignment_payload["reference_text"])
                    self.assertEqual(result.reason, "no_trusted_lyric_reference")

    def test_source_budget_stops_additional_requests_and_retains_a_retry_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "song.m4a";media.write_bytes(b"audio")
            gateway = self.gateway(media);clock = [1.0];calls = []
            gateway.inspect = lambda *_: Track(media, song_id="catalog-id")
            engine = self.engine(gateway)
            def slow_get(**kwargs):
                calls.append("amll");clock[0] += 200;return None
            engine.amll.get = slow_get
            engine.netease_search = lambda *_, **__: self.fail("budget should prevent a new request")
            with patch("word_lyrics_automation.resolver.time.monotonic", side_effect=lambda: clock[0]):
                result = engine.resolve(media_path=media, metadata={}, expected_fingerprint={})
            self.assertEqual(calls, ["amll"])
            self.assertIn("source_discovery_budget_exhausted", result.retryable_error)


if __name__ == "__main__":
    unittest.main()
