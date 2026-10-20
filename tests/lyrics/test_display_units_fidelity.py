from __future__ import annotations

import unittest

from word_lyrics_automation.alignment import build_candidate
from word_lyrics_automation.nonlexical import split_nonlexical, validate_text_coverage
from word_lyrics_automation.text import display_units, normalize_text
from word_lyrics_automation.ttml import parse_word_ttml


class DisplayUnitsFidelityTests(unittest.TestCase):
    def test_spaces_before_punctuation_keep_their_original_position(self):
        cases = [
            '春来 (I love you)', '個個飛車隊 (誰高最勁)',
            '春来 ！山高', 'Hello \t(世界)', '春来  ( 下一句 )  ',
            ' 春来 (Na-na-na-na)', '甲\n（乙）\t！丙',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(''.join(display_units(text)), text)

    def test_multilingual_punctuation_whitespace_and_emoji_round_trip_exactly(self):
        cases = [
            '  こんにちは （Hello, 世界！） \t',
            '“Hello” — 世界！  \n', '你好 🥳 （世界🌏） !',
            'مرحبا (بالعالم) 世界', 'Bonjour « le monde » 😀',
            'Café\u0301 \t“再见”', '𠮷野家 （ＡＢＣ）',
            '\u00a0Hello\u2003（世界）\u3000', '... \t😀Hello!!! 😀  ',
        ]
        for text in cases:
            with self.subTest(text=text):
                units = display_units(text)
                self.assertEqual(''.join(units), text)
                self.assertTrue(all(normalize_text(unit) for unit in units))

    def test_nonlexical_punctuation_alone_creates_no_karaoke_unit(self):
        for text in ['', ' \t\n', '！？ 😀', '（ ）—…', '😀\u3000🤔']:
            with self.subTest(text=text):
                self.assertEqual(display_units(text), [])

    def test_real_plain_backing_lines_preserve_exact_reference_after_alignment(self):
        # These three unique lines occur twice each in the failing 27-line
        # native reference. They contain ordinary sung words, not humming.
        cases = [
            '(江湖中 英雄漢) 開心唱 (開心 自由唱)',
            '誰是最高 最強 (誰高 最高 最響)',
            '我是誰 (不管你是誰)',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(split_nonlexical(text), (text, ''))
                payload = {'track': {'duration': 12, 'title': 'Test', 'artist': 'Test'},
                           'reference_text': text,
                           'reference_lines': [{'text': text, 'begin': 1, 'end': 10}]}
                words = [{'word': char, 'start': 1 + index * .25,
                          'end': 1.25 + index * .25, 'probability': .95}
                         for index, char in enumerate(normalize_text(text))]
                ttml, report = build_candidate({'segments': [{'words': words}]}, payload)
                lines = parse_word_ttml(ttml)
                self.assertEqual(len(lines), 1)
                self.assertEqual(lines[0].text, text)
                self.assertEqual(''.join(token.text for token in lines[0].tokens), text)
                self.assertEqual(report['matched_reference_character_coverage'], 1)
                self.assertNotIn('line_timed_background_lines', report)
                validate_text_coverage(lines)


if __name__ == '__main__':
    unittest.main()
