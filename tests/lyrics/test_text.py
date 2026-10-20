from __future__ import annotations

import unittest

from word_lyrics_automation.text import display_units, normalize_text


class JapaneseTextTests(unittest.TestCase):
    def test_normalization_preserves_kana_and_distinguishes_lyrics(self) -> None:
        self.assertEqual(normalize_text(" あさ、ガラス。"), "あさガラス")
        self.assertEqual(normalize_text("ｶﾞﾗｽ"), "ガラス")
        self.assertNotEqual(normalize_text("子もなくし"), normalize_text("子もなかし"))

    def test_kana_only_line_has_display_units_without_losing_text(self) -> None:
        text = "あさ のうた。"
        units = display_units(text)
        self.assertEqual(len(units), 5)
        self.assertEqual("".join(units), text)


if __name__ == "__main__":
    unittest.main()
