from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace

from word_lyrics_automation.providers.netease import NeteaseSong
from word_lyrics_automation.resolver import SourceResolutionEngine
from word_lyrics_automation.ttml import parse_word_ttml


class FailingAMLL:
    def search(self, **_: object) -> list[object]:
        raise TimeoutError("offline")


class Gateway:
    track = SimpleNamespace(
        path=Path("/music/song.m4a"),
        title="Song",
        artist="Artist",
        album="Album",
        duration=180.0,
        song_id="",
        isrc="",
    )

    def inspect(self, *_: object, **__: object) -> object:
        return self.track

    def existing_word(self, *_: object, **__: object) -> None:
        return None

    def references(self, *_: object, **__: object) -> list[object]:
        return []

    def alignment_payload(self, *_: object, **__: object) -> dict[str, object]:
        return {"track": {"title": "Song"}}


class SourceRetryTests(unittest.TestCase):
    def test_transient_provider_failure_is_marked_for_retry(self) -> None:
        engine = SourceResolutionEngine(
            Gateway(),
            amll_client=FailingAMLL(),
            netease_search_function=lambda *_args, **_kwargs: [],
            kugou_search_function=lambda **_kwargs: [],
        )
        result = engine.resolve(
            media_path=Path("/music/song.m4a"),
            metadata={},
            expected_fingerprint={"sha256": "0" * 64, "size": 1, "mtime_ns": 1},
        )
        self.assertEqual(result.reason, "online_source_temporarily_unavailable")
        self.assertIn("TimeoutError", result.retryable_error)
        self.assertIsNotNone(result.alignment_payload)

    def test_album_query_fuses_exact_yrc_into_apple_line_envelopes(self) -> None:
        apple_ttml = """<?xml version="1.0"?>
<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:timing="Line">
  <body dur="12.0"><div>
    <p begin="1.0" end="2.5">甲乙</p>
    <p begin="3.0" end="4.5">丙丁</p>
    <p begin="5.0" end="6.5">戊己</p>
    <p begin="7.0" end="8.5">庚辛</p>
    <p begin="9.0" end="10.5">壬癸</p>
  </div></body>
</tt>
"""
        yrc = "\n".join(
            (
                "[1020,400](1020,200,0)甲(1220,200,0)乙",
                "[3010,400](3010,200,0)丙(3210,200,0)丁",
                "[5030,400](5030,200,0)戊(5230,200,0)己",
                "[7000,400](7000,200,0)庚(7200,200,0)辛",
                "[12000,400](12000,200,0)壬(12200,200,0)癸",
            )
        )
        queries: list[str] = []

        with TemporaryDirectory() as temporary_directory:
            reference_path = Path(temporary_directory) / "song.ttml"
            reference_path.write_text(apple_ttml, encoding="utf-8")
            track = SimpleNamespace(
                path=Path("/music/song.m4a"),
                title="Song (自由版)",
                artist="Artist",
                album="Exact Album",
                duration=12.0,
                song_id="",
                isrc="",
            )

            class FusionGateway(Gateway):
                def references(self, *_: object, **__: object) -> list[object]:
                    return [(reference_path, object())]

            def search(query: str, *, limit: int) -> list[NeteaseSong]:
                queries.append(query)
                if query == "Exact Album Song 自由版 Artist":
                    return [
                        NeteaseSong(
                            "42",
                            "Song (自由版)",
                            "Artist",
                            "Exact Album",
                            12000,
                        )
                    ]
                return []

            engine = SourceResolutionEngine(
                FusionGateway(),
                netease_search_function=search,
                netease_fetch_function=lambda _song_id: yrc,
            )
            candidates = list(engine._netease_candidates(track))

        self.assertEqual(
            queries,
            [
                "Exact Album Song 自由版 Artist",
                "Song 自由版 Artist",
                "Exact Album Song (自由版) Artist",
                "Song (自由版) Artist",
            ],
        )
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.identity_reason, "exact_timed_reference_fusion")
        parsed = parse_word_ttml(candidate.ttml)
        self.assertEqual([line.begin for line in parsed], [1.0, 3.0, 5.0, 7.0, 9.0])
        self.assertEqual([line.end for line in parsed], [2.5, 4.5, 6.5, 8.5, 10.5])
        self.assertEqual([line.text for line in parsed], ["甲乙", "丙丁", "戊己", "庚辛", "壬癸"])


if __name__ == "__main__":
    unittest.main()
