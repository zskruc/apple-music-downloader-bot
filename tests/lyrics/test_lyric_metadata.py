from __future__ import annotations

import unittest
from types import SimpleNamespace

from word_lyrics_automation.providers.kugou import KugouLyricCandidate, parse_krc
from word_lyrics_automation.providers.netease import NeteaseSong
from word_lyrics_automation.resolver import LegacyGateway, SourceResolutionEngine
from word_lyrics_automation.text import is_credit_line, is_lyric_metadata_line


# Original synthetic text retains header, sung-title and overlapping-window
# cases; it contains no recording lyrics or production runtime data.
LIVE_ROWS = (
    (0, 1180, "测试曲 (Live) - 测试组合303测试歌手"),
    (1180, 2360, "原唱：原唱样例"),
    (5919, 7192, "测试曲"),
    (8335, 11569, "原创测试正文第2行"),
    (11913, 18069, "原创测试正文第3行"),
    (20457, 23250, "原创测试正文第4行"),
    (23528, 26809, "原创测试正文第5行"),
    (26809, 29406, "原创测试正文第6行"),
    (29406, 32532, "原创测试正文第7行"),
    (32531, 35411, "原创测试正文第8行"),
    (35411, 38469, "原创测试正文第9行"),
    (38470, 40089, "原创测试正文第10行"),
    (40088, 45106, "原创测试正文第11行"),
)


def live_krc() -> str:
    return "\n".join(
        f"[{begin},{end - begin}]<0,{end - begin},0>{text}"
        for begin, end, text in LIVE_ROWS
    )


class LyricMetadataTests(unittest.TestCase):
    def test_original_singer_credits_require_an_anchored_label_and_colon(self):
        for text in ("原唱：原唱样例", " 原唱 : 原唱样例", "Original artist: Test", "词：Test"):
            with self.subTest(text=text):
                self.assertTrue(is_credit_line(text))
        for text in ("原唱", "我听原唱：原唱样例", "(原唱：原唱样例)", "和声：啦啦啦", "原唱也是我喜欢的歌"):
            with self.subTest(text=text):
                self.assertFalse(is_credit_line(text))

    def test_live_header_uses_full_title_and_numbered_group_credit(self):
        for title in ("测试曲", "测试曲 (Live)"):
            with self.subTest(title=title):
                self.assertTrue(is_lyric_metadata_line(
                    LIVE_ROWS[0][2], title=title, artist="测试歌手", begin=0,
                ))
        self.assertTrue(is_lyric_metadata_line(
            "朝の例 - 試験隊 (しけんたい)", title="朝の例", artist="試験隊", begin=1,
        ))

    def test_sung_titles_artist_mentions_backing_lyrics_and_later_rows_survive(self):
        for text in (
            "测试曲", "我测试曲", "测试曲 (测试曲)", "测试曲 - 我真的很测试曲",
            "测试曲 - 测试歌手的歌", "测试曲 - 我想听测试歌手", "更测试曲 - 测试歌手",
            "测试曲 - 另一位歌手", "(I like you)  我测试曲", "啦啦  (La la)",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_lyric_metadata_line(text, title="测试曲", artist="测试歌手", begin=0))
        for begin in (None, -1, 20, 35):
            with self.subTest(begin=begin):
                self.assertFalse(is_lyric_metadata_line(
                    LIVE_ROWS[0][2], title="测试曲", artist="测试歌手", begin=begin,
                ))

    def test_krc_drops_only_metadata_and_keeps_all_11_sung_windows(self):
        lines = parse_krc(live_krc(), title="测试曲", artist="测试歌手")
        self.assertEqual(len(lines), 11)
        self.assertEqual(
            [(line.begin, line.end, line.text) for line in lines],
            [(begin / 1000, end / 1000, text) for begin, end, text in LIVE_ROWS[2:]],
        )
        self.assertEqual(lines[0].text, "测试曲")
        for line in lines:
            self.assertEqual("".join(token.text for token in line.tokens), line.text)

    def test_krc_retains_original_spaces_punctuation_and_backing_words(self):
        text = "  I like you  (La la)  "
        lines = parse_krc(f"[5000,2000]<0,900,0>{text[:12]}<900,1100,0>{text[12:]}",
                          title="I like you", artist="Test")
        self.assertEqual(lines[0].text, text)
        self.assertEqual("".join(token.text for token in lines[0].tokens), text)

    def test_official_krc_reference_contains_no_credit_only_acoustic_windows(self):
        candidate = KugouLyricCandidate(
            "live-fixture", "fixture-key", "测试曲", "测试歌手", 45635,
            "官方推荐歌词", 60, 7,
        )
        engine = SourceResolutionEngine(
            SimpleNamespace(), amll_client=SimpleNamespace(search=lambda **_: []),
            kugou_search_function=lambda **_: [candidate], kugou_fetch_function=lambda _: live_krc(),
        )
        result = engine._kugou_reference(SimpleNamespace(title="测试曲 (Live)", artist="测试歌手", duration=45.6))
        self.assertIsNotNone(result)
        self.assertEqual(result["reference_lines"][0], {"text": "测试曲", "begin": 5.919, "end": 7.192})
        self.assertEqual(len(result["reference_lines"]), 11)
        self.assertEqual(result["reference_text"], "".join(text for _, _, text in LIVE_ROWS[2:]))

    def test_trusted_netease_reference_uses_the_same_metadata_filter(self):
        gateway = object.__new__(LegacyGateway)
        gateway.legacy = SimpleNamespace(parse_lrc=lambda _: SimpleNamespace(lines=[
            SimpleNamespace(text=text, begin=begin / 1000, end=end / 1000)
            for begin, end, text in LIVE_ROWS
        ]))
        result = gateway.netease_reference(
            SimpleNamespace(duration=45.6), NeteaseSong("live", "测试曲", "测试歌手", "Test", 45600),
            {"lrc": {"lyric": "fixture"}},
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(result["reference_lines"]), 11)
        self.assertEqual(result["reference_text"], "".join(text for _, _, text in LIVE_ROWS[2:]))


if __name__ == "__main__":
    unittest.main()
