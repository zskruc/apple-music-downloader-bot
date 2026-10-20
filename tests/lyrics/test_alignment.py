from __future__ import annotations

import unittest

from word_lyrics_automation.alignment import build_candidate
from word_lyrics_automation.text import normalize_text
from word_lyrics_automation.ttml import parse_word_ttml


class AlignmentTests(unittest.TestCase):
    def test_reference_alignment_preserves_exact_reference_text(self) -> None:
        result = {
            "language": "zh",
            "segments": [
                {
                    "words": [
                        {"word": character, "start": 1 + index * 0.3, "end": 1.25 + index * 0.3, "probability": 0.95}
                        for index, character in enumerate("红豆生南国春来发几枝")
                    ]
                }
            ],
        }
        payload = {
            "track": {"title": "相思", "artist": "毛阿敏", "duration": 10},
            "reference_text": "红豆生南国春来发几枝",
            "reference_lines": [
                {"text": "红豆生南国", "begin": 1.0, "end": 3.0},
                {"text": "春来发几枝", "begin": 3.0, "end": 5.5},
            ],
        }
        ttml, report = build_candidate(result, payload)
        lines = parse_word_ttml(ttml)
        self.assertEqual(report["mode"], "reference_aligned")
        self.assertEqual(report["reference_text_similarity"], 1.0)
        self.assertGreaterEqual(report["matched_reference_character_coverage"], 0.99)
        self.assertEqual(
            normalize_text("".join(line.text for line in lines)),
            normalize_text(payload["reference_text"]),
        )
        self.assertEqual(sum(len(line.tokens) for line in lines), 10)

    def test_mismatched_reference_is_rejected(self) -> None:
        result = {
            "segments": [
                {
                    "words": [
                        {"word": "completely", "start": 1.0, "end": 1.5, "probability": 0.9},
                        {"word": "different", "start": 1.5, "end": 2.0, "probability": 0.9},
                    ]
                }
            ]
        }
        payload = {
            "track": {"title": "Song", "artist": "Artist", "duration": 5},
            "reference_text": "红豆生南国",
            "reference_lines": [{"text": "红豆生南国", "begin": 1, "end": 3}],
        }
        with self.assertRaisesRegex(RuntimeError, "mismatch"):
            build_candidate(result, payload)

    def test_repeated_chorus_is_matched_in_its_local_time_window(self) -> None:
        chorus = "你给的温柔犹在"
        words = []
        for line_start in (10.0, 40.0):
            words.extend(
                {
                    "word": character,
                    "start": line_start + index * 0.25,
                    "end": line_start + index * 0.25 + 0.2,
                    "probability": 0.98,
                }
                for index, character in enumerate(chorus)
            )
        result = {"language": "zh", "segments": [{"words": words}]}
        payload = {
            "track": {"title": "Song", "artist": "Artist", "duration": 60},
            "reference_text": chorus + chorus,
            "reference_lines": [
                {"text": chorus, "begin": 10.0, "end": 12.0},
                {"text": chorus, "begin": 40.0, "end": 42.0},
            ],
        }

        ttml, report = build_candidate(result, payload)
        lines = parse_word_ttml(ttml)

        self.assertEqual(report["alignment_strategy"], "line_windowed_acoustic")
        self.assertGreaterEqual(report["matched_reference_character_coverage"], 0.99)
        self.assertLess(lines[0].begin, 15.0)
        self.assertGreater(lines[1].begin, 35.0)

    def test_free_transcription_without_reference_is_never_publishable(self) -> None:
        result = {
            "segments": [
                {
                    "words": [
                        {
                            "word": "识别结果",
                            "start": 1.0,
                            "end": 2.0,
                            "probability": 0.99,
                        }
                    ]
                }
            ]
        }
        payload = {
            "track": {"title": "Song", "artist": "Artist", "duration": 5},
            "reference_text": "",
            "reference_lines": [],
        }
        with self.assertRaisesRegex(RuntimeError, "trusted reference text is required"):
            build_candidate(result, payload)

    def test_reference_window_count_must_match_the_authoritative_lines(self):
        payload = {'track': {'duration': 10}, 'reference_text': '春来秋去',
                   'reference_lines': [{'text': '春来', 'begin': 1, 'end': 3},
                                       {'text': '秋去', 'begin': 4, 'end': 6}]}
        observation = {'segments': [{'words': [{'word': '春来', 'start': 1, 'end': 2, 'probability': .9}]}]}
        with self.assertRaisesRegex(RuntimeError, 'window transcript count mismatch'):
            build_candidate({}, payload, reference_results=[observation])

    def test_acoustic_word_must_fit_completely_inside_its_reference_window(self):
        payload = {'track': {'duration': 30}, 'reference_text': '春来',
                   'reference_lines': [{'text': '春来', 'begin': 10, 'end': 14}]}
        for begin, end in [(0, 12), (12, 25), (0, 25)]:
            observation = {'segments': [{'words': [
                {'word': '春来', 'start': begin, 'end': end, 'probability': .99}
            ]}]}
            with self.subTest(begin=begin, end=end):
                with self.assertRaisesRegex(RuntimeError, 'acoustic/reference mismatch'):
                    build_candidate(observation, payload)

    def test_acoustic_word_within_padding_retains_its_measured_timing(self):
        payload = {'track': {'duration': 30}, 'reference_text': '春来',
                   'reference_lines': [{'text': '春来', 'begin': 10, 'end': 14}]}
        observation = {'segments': [{'words': [
            {'word': '春来', 'start': 8.4, 'end': 15.6, 'probability': .99}
        ]}]}
        ttml, report = build_candidate(observation, payload)
        line = parse_word_ttml(ttml)[0]
        self.assertEqual((line.begin, line.end), (8.4, 15.6))
        self.assertEqual(report['matched_reference_character_coverage'], 1)

    def test_trailing_omitted_characters_keep_positive_time_after_last_anchor(self):
        text = '一二三四五六七八九十'
        payload = {'track': {'duration': 20}, 'reference_text': text,
                   'reference_lines': [{'text': text, 'begin': 1, 'end': 5}]}
        result = {'segments': [{'words': [
            {'word': char, 'start': 1 + i * .5, 'end': 1.5 + i * .5, 'probability': .9}
            for i, char in enumerate(text[:8])
        ]}]}
        ttml, report = build_candidate(result, payload)
        line = parse_word_ttml(ttml)[0]
        self.assertEqual(line.text, text)
        self.assertEqual(report['matched_reference_character_coverage'], .8)
        self.assertTrue(all(token.end > token.begin for token in line.tokens))
        self.assertTrue(all(a.end <= b.begin for a, b in zip(line.tokens, line.tokens[1:])))
        self.assertLessEqual(line.end, 5)

    def test_substitution_timing_does_not_inflate_text_match_confidence(self):
        text = '一二三四五六七八九十'
        payload = {'track': {'duration': 20}, 'reference_text': text,
                   'reference_lines': [{'text': text, 'begin': 1, 'end': 6}]}
        words = [{'word': char, 'start': 1 + i * .5, 'end': 1.5 + i * .5, 'probability': .9}
                 for i, char in enumerate(text[:8])]
        words.append({'word': '错', 'start': 5, 'end': 6, 'probability': .8})
        ttml, report = build_candidate({'segments': [{'words': words}]}, payload)
        tokens = parse_word_ttml(ttml)[0].tokens
        self.assertEqual(report['matched_reference_character_coverage'], .8)
        self.assertAlmostEqual(report['raw_transcript_text_similarity'], 16 / 19, places=6)
        self.assertEqual([(t.begin, t.end) for t in tokens[-2:]], [(5, 5.5), (5.5, 6)])


if __name__ == "__main__":
    unittest.main()
