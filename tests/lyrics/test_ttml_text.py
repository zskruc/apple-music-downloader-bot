from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from word_lyrics_automation.models import LyricLine, TimedToken
from word_lyrics_automation.ttml import TT_NS, parse_word_ttml, render_word_ttml


def render(line: LyricLine) -> str:
    return render_word_ttml([line], duration=20, title="Test", artist="Test")


class TtmlTextContractTests(unittest.TestCase):
    def test_punctuation_and_whitespace_only_lines_preserve_text_without_cues(self) -> None:
        for text in (" （ ） …，！ ", " \t\n "):
            with self.subTest(text=text):
                output = render(LyricLine(text, 1, 5))
                self.assertEqual(parse_word_ttml(output)[0].text, text)
                self.assertEqual(parse_word_ttml(output)[0].tokens, ())
                self.assertEqual(len(ET.fromstring(output).findall(f".//{{{TT_NS}}}span")), 0)

    def test_exact_original_text_including_spaces_and_punctuation_roundtrips(self) -> None:
        text = " 風， (雲)！ "
        line = LyricLine(text, 1, 5, (
            TimedToken(" 風， (", 1, 2), TimedToken("雲)！ ", 2, 5),
        ))
        actual = parse_word_ttml(render(line))[0]
        self.assertEqual(actual.text, text)
        self.assertEqual(actual.tokens, line.tokens)

    def test_untimed_suffix_remains_plain_text_with_no_invented_cue(self) -> None:
        line = LyricLine("風 (啦啦啦)！ ", 1, 5, (TimedToken("風", 1, 2),))
        output = render(line)
        actual = parse_word_ttml(output)[0]
        self.assertEqual(actual.text, line.text)
        self.assertEqual(actual.tokens, line.tokens)
        span = ET.fromstring(output).find(f".//{{{TT_NS}}}span")
        self.assertEqual(span.tail, " (啦啦啦)！ ")

    def test_script_equivalence_does_not_relax_exact_original_prefix(self) -> None:
        for original, token in (("風雲", "风云"), ("ABC", "abc"), ("唱 (開心)", "唱( 開心)")):
            with self.subTest(original=original, token=token):
                line = LyricLine(original, 1, 5, (TimedToken(token, 1, 5),))
                with self.assertRaisesRegex(ValueError, "exact prefix"):
                    render(line)

    def test_extra_or_reordered_timed_text_is_rejected(self) -> None:
        for token in ("甲乙丙", "乙甲", "甲，乙"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "exact prefix"):
                    render(LyricLine("甲乙", 1, 5, (TimedToken(token, 1, 5),)))


if __name__ == "__main__":
    unittest.main()
