from __future__ import annotations

import unittest

from word_lyrics_automation.alignment_retry import build_with_reference_retry, reference_language
from word_lyrics_automation.ttml import parse_word_ttml


class AlignmentRetryTests(unittest.TestCase):
    def payload(self):
        return {
            "track": {"duration": 30, "title": "Test", "artist": "Artist"},
            "reference_text": "あさのうたよるのうた",
            "reference_lines": [
                {"text": "あさのうた", "begin": 10, "end": 15},
                {"text": "よるのうた", "begin": 20, "end": 25},
            ],
        }

    def test_failed_whole_song_recovers_from_actual_line_audio_windows(self):
        calls = []
        def transcribe(begin, end, text, language):
            calls.append((begin, end, language))
            return {"segments": [{"words": [
                {"word": char, "start": 1.25 + i * .7, "end": 1.95 + i * .7, "probability": .9}
                for i, char in enumerate(text)
            ]}]}
        ttml, report = build_with_reference_retry({"segments": []}, self.payload(), transcribe)
        self.assertEqual(calls, [(8.75, 15.75, "ja"), (18.75, 25.75, "ja")])
        lines = parse_word_ttml(ttml)
        self.assertEqual("".join(line.text for line in lines), self.payload()["reference_text"])
        self.assertEqual([line.begin for line in lines], [10, 20])
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["retried_windows"], 2)

    def test_invalid_reference_window_does_not_start_model_work(self):
        payload = self.payload()
        payload["reference_lines"][0]["end"] = 100
        with self.assertRaisesRegex(RuntimeError, "windows are invalid"):
            build_with_reference_retry({"segments": []}, payload, lambda *_: self.fail("model invoked"))

    def test_word_spanning_far_outside_the_reference_retries_actual_audio_window(self):
        payload = {'track': {'duration': 30}, 'reference_text': '春来',
                   'reference_lines': [{'text': '春来', 'begin': 10, 'end': 14}]}
        whole = {'segments': [{'words': [
            {'word': '春来', 'start': 0, 'end': 12, 'probability': .99}
        ]}]}
        calls = []

        def transcribe(begin, end, text, language):
            calls.append((begin, end, text))
            return {'segments': [{'words': [
                {'word': '春来', 'start': 1.25, 'end': 2.25, 'probability': .99}
            ]}]}

        ttml, report = build_with_reference_retry(whole, payload, transcribe)
        self.assertEqual(calls, [(8.75, 14.75, '春来')])
        self.assertEqual(report['retried_windows'], 1)
        self.assertTrue(report['initial_alignment_error'].startswith('acoustic/reference mismatch'))
        self.assertEqual(report['matched_reference_character_coverage'], 1)
        self.assertEqual(report['raw_transcript_text_similarity'], 1)
        line = parse_word_ttml(ttml)[0]
        self.assertEqual((line.begin, line.end), (10, 11))

    def test_lrc_missing_end_uses_next_start_and_audio_duration(self):
        payload = self.payload()
        for line in payload['reference_lines']:
            line['end'] = None
        calls = []

        def transcribe(begin, end, text, language):
            calls.append((begin, end))
            return {'segments': [{'words': [
                {'word': char, 'start': 1.25 + i * .7, 'end': 1.95 + i * .7, 'probability': .9}
                for i, char in enumerate(text)
            ]}]}

        ttml, report = build_with_reference_retry({'segments': []}, payload, transcribe)
        self.assertEqual(calls, [(8.75, 20.75), (18.75, 30)])
        self.assertEqual(''.join(line.text for line in parse_word_ttml(ttml)), payload['reference_text'])
        self.assertEqual(report['retried_windows'], 2)

    def test_invalid_next_start_is_rejected_before_cropping(self):
        payload = self.payload()
        payload['reference_lines'][0]['end'] = None
        payload['reference_lines'][1]['begin'] = None
        with self.assertRaisesRegex(RuntimeError, 'windows are invalid'):
            build_with_reference_retry({'segments': []}, payload, lambda *_: self.fail('model invoked'))

    def test_retry_does_not_relax_acoustic_validation(self):
        with self.assertRaisesRegex(RuntimeError, "acoustic/reference mismatch"):
            build_with_reference_retry({"segments": []}, self.payload(), lambda *_: {"segments": [{"words": [{"word": "unrelated", "start": 1.3, "end": 2, "probability": .9}]}]})

    def test_language_hint_requires_kana(self):
        self.assertEqual(reference_language(self.payload()), "ja")
        self.assertIsNone(reference_language({"reference_text": "中文测试"}))

    def test_overlapping_crops_keep_the_next_lines_correct_acoustic_words(self):
        payload = {
            'track': {'duration': 20}, 'reference_text': '春来秋去',
            'reference_lines': [{'text': '春来', 'begin': 10, 'end': 11},
                                {'text': '秋去', 'begin': 11.1, 'end': 12.1}],
        }

        def transcribe(begin, end, text, language):
            absolute = (
                [('春', 10, 10.3), ('来', 10.35, 10.65), ('啊', 11.25, 11.65)]
                if text == '春来' else [('秋', 11.1, 11.3), ('去', 11.35, 11.55)]
            )
            return {'segments': [{'words': [
                {'word': word, 'start': start - begin, 'end': stop - begin, 'probability': .95}
                for word, start, stop in absolute
            ]}]}

        ttml, report = build_with_reference_retry({'segments': []}, payload, transcribe)
        lines = parse_word_ttml(ttml)
        self.assertEqual(report['matched_reference_character_coverage'], 1)
        self.assertEqual(report['unverified_line_fraction'], 0)
        self.assertEqual(report['alignment_input_scope'], 'per_reference_window')
        self.assertEqual(''.join(line.text for line in lines), '春来秋去')
        self.assertLessEqual(lines[0].end, lines[1].begin)
        self.assertAlmostEqual(lines[1].tokens[0].begin, 11.1)
        self.assertAlmostEqual(lines[1].tokens[0].end, 11.3)
