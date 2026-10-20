from __future__ import annotations

import unittest
from dataclasses import make_dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from word_lyrics_automation.providers.kugou import KugouLyricCandidate, parse_krc
from word_lyrics_automation.identity import title_search_names
from word_lyrics_automation.resolver import SourceResolutionEngine


class EmptyAMLL:
    def search(self, **_: object) -> list[object]:
        return []


class Gateway:
    track = SimpleNamespace(
        path=Path("/music/song.m4a"),
        title="送你的独白",
        artist="许嵩",
        album="情侣装",
        duration=265.125,
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
        return {
            "track": {
                "title": self.track.title,
                "artist": self.track.artist,
                "duration": self.track.duration,
            },
            "reference_text": "",
            "reference_lines": [],
        }


def fixture_krc() -> str:
    return "\n".join(
        (
            "[0,1000]<0,500,0>送你的独白 - 许嵩",
            "[1000,1000]<0,500,0>词：Prince/Vae",
            "[20000,3000]<0,300,0>朋友<300,300,0>都在猜<600,400,0>我们的将来",
            "[30000,3000]<0,300,0>他们<300,300,0>怎会明白<600,400,0>一路是阻碍",
            "[40000,3000]<0,300,0>望着<300,300,0>夜发呆<600,400,0>泪流了下来",
            "[50000,3000]<0,300,0>你擅自离开<300,300,0>是重度伤害<600,400,0>不再回来",
            "[60000,3000]<0,300,0>说着送你的独白<300,300,0>眼泪跟着流下来<600,400,0>温柔犹在",
        )
    )


class KugouReferenceTests(unittest.TestCase):
    def test_one_millisecond_krc_rounding_preserves_monotonic_reference(self) -> None:
        lines = parse_krc("[30000,3000]<0,2400,0>甲<2399,330,0>乙<2730,270,0>丙")
        self.assertEqual(lines[0].text, "甲乙丙")
        self.assertEqual(lines[0].tokens[1].begin, lines[0].tokens[0].end)
        self.assertAlmostEqual(lines[0].tokens[1].end, 32.729)

    def test_larger_krc_timing_overlaps_are_still_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not monotonic"):
            parse_krc("[30000,1000]<0,500,0>甲<498,500,0>乙")

    def test_soundtrack_note_fallback_recovers_official_reference(self) -> None:
        gateway = Gateway()
        gateway.track = SimpleNamespace(**vars(Gateway.track))
        gateway.track.title = "送你的独白 (电视剧《示例》主题曲)"
        queried = []
        official = KugouLyricCandidate(
            lyric_id="soundtrack", access_key="key", title="送你的独白",
            artist=gateway.track.artist, duration_ms=265168,
            product_from="官方推荐歌词", score=60, hit_layer=7,
        )
        candidates = [official]

        def search(**kwargs):
            queried.append(kwargs["title"])
            return candidates if kwargs["title"] == "送你的独白" else []

        engine = SourceResolutionEngine(
            gateway, amll_client=EmptyAMLL(),
            netease_search_function=lambda *_args, **_kwargs: [],
            kugou_search_function=search,
            kugou_fetch_function=lambda _: fixture_krc(),
        )
        reference = engine._kugou_reference(gateway.track)
        self.assertEqual(queried, [gateway.track.title, "送你的独白"])
        self.assertIsNotNone(reference)
        self.assertEqual(reference["reference_provider_id"], "soundtrack")
        for wrong in (
            replace(official, artist="其他歌手"),
            replace(official, title="其他歌曲"),
            replace(official, duration_ms=270000),
            replace(official, product_from="用户上传"),
        ):
            candidates[:] = [wrong]
            self.assertIsNone(engine._kugou_reference(gateway.track))

    def test_soundtrack_queries_keep_live_and_other_recording_qualifiers(self) -> None:
        self.assertEqual(title_search_names("家园 (电视剧《闯关东》主题曲)"),
                         ("家园 (电视剧《闯关东》主题曲)", "家园"))
        for title in ["家园 (Live)", "家园 (Remix)", "家园 (演唱会)", "家园 (电影主题曲 Live)"]:
            self.assertEqual(title_search_names(title), (title,))
        self.assertEqual(title_search_names("家园 (Live) (电视剧《闯关东》主题曲)")[-1], "家园 (Live)")

    def test_catalog_localization_recovers_an_artist_without_a_static_alias(self) -> None:
        gateway = Gateway()
        values = {**vars(Gateway.track), "artist": "English Catalog Name"}
        track_class = make_dataclass("CatalogTrack", [(key, object) for key in values], frozen=True)
        gateway.track = track_class(**values)
        gateway.localized_artist_identity = lambda _: {
            "names": ["English Catalog Name", "公式名"],
            "evidence": [{"artist_id": "456", "name": "公式名"}],
        }
        candidate = KugouLyricCandidate(
            lyric_id="localized", access_key="key", title=gateway.track.title,
            artist="公式名", duration_ms=265168, product_from="官方推荐歌词", score=60, hit_layer=7,
        )
        engine = SourceResolutionEngine(
            gateway, amll_client=EmptyAMLL(),
            netease_search_function=lambda *_args, **_kwargs: [],
            kugou_search_function=lambda **kwargs: [candidate] if kwargs["artist"] == "公式名" else [],
            kugou_fetch_function=lambda _: fixture_krc().replace("许嵩", "公式名"),
        )
        result = engine.resolve(media_path=gateway.track.path, metadata={}, expected_fingerprint={})
        self.assertEqual(result.reason, "trusted_reference_needs_acoustic_alignment")
        self.assertEqual(result.alignment_payload["reference_identity"]["artist"], "公式名")
        self.assertEqual(result.alignment_payload["artist_identity_evidence"][0]["artist_id"], "456")

    def test_zero_duration_punctuation_keeps_credits_out_of_japanese_lyrics(self) -> None:
        lines = parse_krc(
            "[1000,1000]<0,1000,0>朝の歌 - 赤い鳥 (あかいとり<1000,0,0>)\n"
            "[2000,1000]<0,200,0>词<200,0,0>：<200,800,0>日本民謡\n"
            "[3000,1000]<0,200,0>曲<200,0,0>：<200,800,0>日本民謡\n"
            "[20000,1000]<0,400,0>あさ<400,0,0> <400,600,0>のうた<1000,0,0>。",
            title="朝の歌", artist="赤い鳥",
        )
        # The identity row uses a realistic split closing parenthesis.
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "あさ のうた。")
        self.assertEqual("".join(token.text for token in lines[0].tokens), lines[0].text)

    def test_localized_artist_search_preserves_identity_and_duration_gates(self) -> None:
        gateway = Gateway()
        gateway.track = SimpleNamespace(**vars(Gateway.track))
        gateway.track.artist = "The Red Birds"
        queried = []
        official = KugouLyricCandidate(
            lyric_id="localized", access_key="key", title=gateway.track.title,
            artist="赤い鳥", duration_ms=265168,
            product_from="官方推荐歌词", score=60, hit_layer=7,
        )
        candidates = [official]

        def search(**kwargs: object) -> list[KugouLyricCandidate]:
            queried.append(kwargs["artist"])
            return candidates if kwargs["artist"] == "赤い鳥" else []

        engine = SourceResolutionEngine(
            gateway, amll_client=EmptyAMLL(),
            netease_search_function=lambda *_args, **_kwargs: [],
            kugou_search_function=search,
            kugou_fetch_function=lambda _: fixture_krc().replace("许嵩", "赤い鳥"),
        )
        result = engine._kugou_reference(gateway.track)
        self.assertEqual(queried, ["The Red Birds", "赤い鳥"])
        self.assertIsNotNone(result)
        self.assertEqual(len(result["reference_lines"]), 5)
        self.assertEqual(result["reference_identity"]["artist"], "赤い鳥")
        for wrong in (
            replace(official, duration_ms=270000),
            replace(official, artist="赤い公園"),
            replace(official, product_from="用户上传"),
        ):
            candidates[:] = [wrong]
            self.assertIsNone(engine._kugou_reference(gateway.track))

    def test_parser_filters_identity_and_credit_lines(self) -> None:
        lines = parse_krc(fixture_krc(), title="送你的独白", artist="许嵩")
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0].begin, 20.0)
        self.assertTrue(lines[0].text.startswith("朋友"))
        self.assertGreater(sum(len(line.tokens) for line in lines), 10)

    def test_official_exact_duration_result_becomes_alignment_reference(self) -> None:
        official = KugouLyricCandidate(
            lyric_id="309266114",
            access_key="key",
            title="送你的独白",
            artist="许嵩",
            duration_ms=265168,
            product_from="官方推荐歌词",
            score=60,
            hit_layer=7,
        )
        engine = SourceResolutionEngine(
            Gateway(),
            amll_client=EmptyAMLL(),
            netease_search_function=lambda *_args, **_kwargs: [],
            kugou_search_function=lambda **_kwargs: [official],
            kugou_fetch_function=lambda _candidate: fixture_krc(),
        )
        result = engine.resolve(
            media_path=Path("/music/song.m4a"),
            metadata={},
            expected_fingerprint={"sha256": "0" * 64, "size": 1, "mtime_ns": 1},
        )
        self.assertEqual(result.reason, "trusted_reference_needs_acoustic_alignment")
        assert result.alignment_payload is not None
        self.assertEqual(
            result.alignment_payload["reference_provider"],
            "kugou_official_krc",
        )
        self.assertEqual(result.alignment_payload["reference_provider_id"], "309266114")
        self.assertTrue(result.alignment_payload["reference_text"])


if __name__ == "__main__":
    unittest.main()
