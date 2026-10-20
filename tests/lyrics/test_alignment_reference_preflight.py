from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.alignment import build_candidate, validate_reference_payload
from word_lyrics_automation.alignment_backend_whispercpp import run_alignment
from word_lyrics_automation.alignment_retry import build_with_reference_retry
from word_lyrics_automation.text import normalize_text
from word_lyrics_automation.ttml import parse_word_ttml


class AlignmentReferencePreflightTests(unittest.TestCase):
    def payload(self):
        return {'track': {'duration': 10, 'title': 'Test', 'artist': 'Test'},
                'reference_text': '春来秋去',
                'reference_lines': [{'text': '春来', 'begin': 1, 'end': 3},
                                    {'text': '秋去', 'begin': 4, 'end': 6}]}

    def observation(self, payload):
        words = []
        for row in payload['reference_lines']:
            words.extend({'word': char, 'start': row['begin'] + index * .3,
                          'end': row['begin'] + (index + 1) * .3, 'probability': .95}
                         for index, char in enumerate(normalize_text(row['text'])))
        return {'segments': [{'words': words}]}

    def invalid_cases(self):
        for field, value in [('begin', 'bad'), ('begin', float('nan')),
                             ('begin', -1), ('begin', True), ('end', float('inf')),
                             ('end', 4), ('end', 100)]:
            payload = self.payload()
            payload['reference_lines'][1][field] = value
            yield payload
        for text in ['春来', '秋去春来', '春来错词']:
            payload = self.payload()
            payload['reference_text'] = text
            yield payload
        for char in ['\x00', '\x0b', '\ud800', '\uffff']:
            payload = self.payload()
            payload['reference_lines'][1]['text'] += char
            payload['reference_text'] += char
            yield payload

    def test_damaged_reference_never_silently_drops_a_lexical_line(self):
        observation = self.observation(self.payload())
        for payload in self.invalid_cases():
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                build_candidate(observation, payload)

    def test_invalid_reference_stops_before_fingerprint_audio_preparation_or_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / 'must-not-be-created'
            with patch('word_lyrics_automation.alignment_backend_whispercpp.media_fingerprint') as fingerprint, \
                 patch('word_lyrics_automation.alignment_backend_whispercpp.subprocess.run') as prepare, \
                 patch('word_lyrics_automation.alignment_backend_whispercpp._run_with_progress') as infer:
                for payload in self.invalid_cases():
                    with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                        run_alignment(audio=Path('/not-read'), payload=payload, output=Path('/not-written'),
                                      report_path=Path('/not-written'), binary=Path('/not-read'),
                                      model=Path('/not-read'), work_dir=work, threads=1, timeout_seconds=30)
                fingerprint.assert_not_called()
                prepare.assert_not_called()
                infer.assert_not_called()
                self.assertFalse(work.exists())

    def test_invalid_reference_never_starts_a_retry_crop(self):
        for payload in self.invalid_cases():
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                build_with_reference_retry({'segments': []}, payload,
                                           lambda *_: self.fail('a damaged reference started inference'))

    def test_normalized_prompt_layout_may_differ_but_original_lines_are_rendered(self):
        payload = self.payload()
        payload['reference_lines'][0]['text'] = '風 與 雲'
        payload['reference_lines'][1]['text'] = 'Hello! (World)'
        payload['reference_text'] = '风与云\nHELLO WORLD'
        ttml, report = build_candidate(self.observation(payload), payload)
        lines = parse_word_ttml(ttml)
        self.assertEqual([line.text for line in lines],
                         [row['text'] for row in payload['reference_lines']])
        self.assertEqual(report['matched_reference_character_coverage'], 1)

    def test_missing_lrc_endpoints_and_structured_only_validation_remain_supported(self):
        payload = self.payload()
        for row in payload['reference_lines']:
            row['end'] = None
        validate_reference_payload(payload)
        calls = []
        def transcribe(begin, end, text, language):
            calls.append((begin, end))
            return {'segments': [{'words': [{'word': text, 'start': 1.25, 'end': 2.25,
                                            'probability': .95}]}]}
        ttml, _ = build_with_reference_retry({'segments': []}, payload, transcribe)
        self.assertEqual(calls, [(0, 4.75), (2.75, 10)])
        self.assertEqual([line.text for line in parse_word_ttml(ttml)], ['春来', '秋去'])
        payload.pop('reference_text')
        validate_reference_payload(payload)

    def test_xml_legal_multilingual_text_and_metadata_are_preserved(self):
        payload = self.payload()
        payload['track'].update(title='Title 🎵', artist='歌手 & Artist')
        payload['reference_lines'][0]['text'] = '\t你好 （World 😀）\n'
        payload['reference_text'] = ''.join(row['text'] for row in payload['reference_lines'])
        validate_reference_payload(payload)
        for name in ('title', 'artist'):
            invalid = copy.deepcopy(payload)
            invalid['track'][name] += '\x00'
            with self.assertRaisesRegex(RuntimeError, 'illegal XML'):
                validate_reference_payload(invalid)

    def test_bad_reference_shapes_and_nonfinite_duration_fail_with_context(self):
        for mutation in [lambda p: p.update(reference_lines='wrong'),
                         lambda p: p['reference_lines'].append(None),
                         lambda p: p['reference_lines'][0].update(text=None),
                         lambda p: p.update(reference_text=['春来']),
                         lambda p: p['track'].update(duration=float('nan')),
                         lambda p: p['track'].update(duration=-1)]:
            payload = self.payload()
            mutation(payload)
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                validate_reference_payload(payload)


if __name__ == '__main__':
    unittest.main()
