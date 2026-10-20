import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

from word_lyrics_automation.alignment import build_candidate
from word_lyrics_automation.alignment_retry import build_with_reference_retry
from word_lyrics_automation.models import LyricLine, TimedToken
from word_lyrics_automation.nonlexical import is_nonlexical, split_nonlexical, validate_text_coverage
from word_lyrics_automation.reference_fusion import fuse_reference_words
from word_lyrics_automation.resolver import LegacyGateway, Candidate, CandidateRejected, SourceResolutionEngine
from word_lyrics_automation.providers.kugou import KugouLyricCandidate
from word_lyrics_automation.nas_alignment_worker import NasAlignmentWorker
from word_lyrics_automation.ttml import parse_word_ttml, render_word_ttml


class MixedLyricsTests(unittest.TestCase):
    def payload(self):
        lines = [{'text': '春来秋去花开花落日月星辰', 'begin': 10, 'end': 16},
                 {'text': 'Du, du, lu, lu, du, du', 'begin': 17, 'end': 23},
                 {'text': '山高水长云卷云舒春风细雨 (Na-na-na-na)', 'begin': 24, 'end': 30},
                 {'text': '日升月落星光照耀小桥流水', 'begin': 32, 'end': 38},
                 {'text': '春风吹来蝴蝶飞舞青山绿水', 'begin': 40, 'end': 46}]
        return {'track': {'duration': 50, 'title': 'Test', 'artist': 'Test'},
                'reference_text': ''.join(line['text'] for line in lines), 'reference_lines': lines}

    def observations(self, payload):
        words = []
        for line in payload['reference_lines']:
            text = split_nonlexical(line['text'])[0]
            words.extend({'word': char, 'start': line['begin'] + i * .4,
                          'end': line['begin'] + (i + 1) * .4, 'probability': .95}
                         for i, char in enumerate(text))
        return {'language': 'zh', 'segments': [{'words': words}]}

    def source(self, payload):
        output = []
        for line in payload['reference_lines']:
            text = split_nonlexical(line['text'])[0]
            if not text:
                continue
            step = 5.7 / len(text)
            tokens = tuple(TimedToken(char, line['begin'] + .2 + i * step,
                                      line['begin'] + .2 + (i + 1) * step) for i, char in enumerate(text))
            output.append(LyricLine(text, tokens[0].begin, tokens[-1].end, tokens))
        return output

    def test_only_repeated_known_vocables_are_nonlexical(self):
        for text in ['Du, du, lu, lu, du', '(Na-na-na-na)Yay, hey', '啦啦啦啦']:
            self.assertTrue(is_nonlexical(text))
        for text in ['Never gonna give you up', '(I love you)', 'Oh my love', 'na', '啊你在哪里啊', '123', '']:
            self.assertFalse(is_nonlexical(text))
        self.assertEqual(split_nonlexical('春来 (I love you)'), ('春来 (I love you)', ''))

    def test_acoustic_humming_does_not_reject_verified_lexical_words(self):
        payload = self.payload()
        ttml, report = build_candidate(self.observations(payload), payload)
        lines = parse_word_ttml(ttml)
        self.assertEqual([line.text for line in lines], [line['text'] for line in payload['reference_lines']])
        self.assertEqual(report['matched_reference_character_coverage'], 1)
        self.assertEqual(report['line_timed_nonlexical_lines'], 1)
        self.assertEqual(report['line_timed_background_lines'], 1)
        self.assertEqual(len(lines[1].tokens), 0)
        self.assertEqual(lines[1].begin, 17)
        self.assertNotIn('Na', ''.join(token.text for token in lines[2].tokens))
        validate_text_coverage(lines)

    def test_nonlexical_fallback_cannot_publish_wrong_lexical_audio(self):
        payload = self.payload()
        observation = self.observations(payload)
        for word in observation['segments'][0]['words']:
            word['word'] = '错'
        with self.assertRaisesRegex(RuntimeError, 'mismatch'):
            build_candidate(observation, payload)

    def test_retry_skips_humming_but_preserves_all_reference_rows(self):
        payload = self.payload()
        calls = []
        def transcribe(begin, end, text, language):
            calls.append(text)
            return self.observations({'reference_lines': [{'text': text, 'begin': 1.25}]})
        ttml, report = build_with_reference_retry({'segments': []}, payload, transcribe)
        self.assertEqual(len(calls), 4)
        self.assertEqual(report['retried_windows'], 4)
        self.assertEqual(len(parse_word_ttml(ttml)), 5)

    def test_untimed_ordinary_words_are_rejected(self):
        lines = [LyricLine('春风吹来', 1, 4, (TimedToken('春风', 1, 2),))]
        with self.assertRaisesRegex(ValueError, 'untimed_lexical_text'):
            validate_text_coverage(lines)

    def test_verified_word_source_keeps_every_native_line_and_background(self):
        payload = self.payload()
        ttml, report = fuse_reference_words(payload['reference_lines'], self.source(payload), **payload['track'])
        lines = parse_word_ttml(ttml)
        self.assertEqual([line.text for line in lines], [line['text'] for line in payload['reference_lines']])
        self.assertEqual(sum(len(line.tokens) for line in lines), 48)
        self.assertEqual(report['native_lexical_line_coverage'], 1)
        self.assertEqual(report['provider_lexical_text_similarity'], 1)
        self.assertEqual([line.begin for line in lines], [10, 17, 24, 32, 40])

    def test_provider_cannot_omit_add_or_move_a_verse(self):
        payload = self.payload()
        source = self.source(payload)
        for changed in [source[:-1], source + source[-1:], source[1:] + source[:1],
                        [replace(line, begin=line.begin + 2, end=line.end + 2,
                                 tokens=tuple(replace(token, begin=token.begin + 2, end=token.end + 2) for token in line.tokens)) for line in source]]:
            with self.assertRaises(ValueError):
                fuse_reference_words(payload['reference_lines'], changed, **payload['track'])

    def test_isolated_catalog_spelling_preserves_native_text_not_provider_text(self):
        payload = self.payload()
        source = self.source(payload)
        first = source[0]
        source[0] = replace(first, text='椿' + first.text[1:],
                            tokens=(replace(first.tokens[0], text='椿'), *first.tokens[1:]))
        ttml, report = fuse_reference_words(payload['reference_lines'], source, **payload['track'])
        self.assertEqual(parse_word_ttml(ttml)[0].text, payload['reference_lines'][0]['text'])
        self.assertEqual(report['provider_substitution_count'], 1)
        self.assertGreaterEqual(report['provider_lexical_text_similarity'], .97)
        source[0] = replace(source[0], text='椿莱' + first.text[2:],
                            tokens=(replace(first.tokens[0], text='椿'), replace(first.tokens[1], text='莱'), *first.tokens[2:]))
        with self.assertRaises(ValueError):
            fuse_reference_words(payload['reference_lines'], source, **payload['track'])

    def fixture_candidate(self):
        payload = self.payload()
        raw = []
        for line in self.source(payload):
            start = round(line.begin * 1000)
            raw.append(f'[{start},{round((line.end - line.begin) * 1000)}]' + ''.join(
                f'<{round(token.begin * 1000) - start},{round((token.end - token.begin) * 1000)},0>{token.text}'
                for token in line.tokens))
        item = KugouLyricCandidate('123', 'never persisted', 'Test', 'Test', 51800, '官方推荐歌词', 60, 7)
        gateway = SimpleNamespace(alignment_payload=lambda *_: payload)
        track = SimpleNamespace(**payload['track'])
        engine = SourceResolutionEngine(gateway, kugou_search_function=lambda **_: [item],
                                        kugou_fetch_function=lambda _: '\n'.join(raw))
        return payload, item, engine, track

    def test_native_clocks_allow_file_padding_but_not_wrong_identity(self):
        payload, item, engine, track = self.fixture_candidate()
        self.assertEqual(len(list(engine._kugou_candidates(track))), 1)
        for wrong in [replace(item, artist='Other'), replace(item, title='Test Live'),
                      replace(item, duration_ms=55000), replace(item, product_from='第三方歌词')]:
            engine.kugou_search = lambda **_: [wrong]
            self.assertEqual(list(engine._kugou_candidates(track)), [])

    def test_fusion_publication_recomputes_evidence_and_rejects_tampering(self):
        from test_same_file_validation import word_lyrics
        from word_lyrics_automation.fingerprint import media_fingerprint
        payload, item, engine, _ = self.fixture_candidate()
        candidate = list(engine._kugou_candidates(SimpleNamespace(**payload['track'])))[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / 'song.m4a'
            audio.write_bytes(b'unchanged audio')
            lines = [LyricLine(line['text'], line['begin'], line['end']) for line in payload['reference_lines']]
            native = render_word_ttml(lines, **payload['track'])
            audio.with_suffix('.ttml').write_text(native)
            gateway = LegacyGateway(root / 'state')
            track = word_lyrics.Track(path=audio, album='Test', **payload['track'])
            doc, metrics, _ = gateway._strict_validation(track, candidate)
            self.assertEqual(doc.timed_spans, 48)
            for wrong in [replace(candidate, ttml=candidate.ttml.replace('春', '错', 1)),
                          replace(candidate, details={})]:
                with self.assertRaises(CandidateRejected):
                    gateway._strict_validation(track, wrong)
            fingerprint = media_fingerprint(audio)
            result = gateway.publish(track, candidate, expected_fingerprint=fingerprint)
            self.assertEqual(media_fingerprint(audio), fingerprint)
            self.assertEqual(Path(result.backup_path).read_text(), native)

    def test_source_recovery_runs_before_terminal_notification(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'AMDL_WORD_LYRICS_STATE_DIR': tmp}):
            worker = NasAlignmentWorker()
            audio = Path(tmp) / 'song.wav'
            audio.write_bytes(b'unchanged')
            worker.store.enqueue(audio)
            source = worker.store.claim('source', owner='test')
            worker.store.defer_to_alignment(source.id, source.lease_token, payload=self.payload(), reason='test')
            job = worker.store.claim('alignment', owner='test')
            def recovered(job, error, diagnostic_payload=None):
                worker.store.transition_to_scan(job.id, job.lease_token, result={'provider': 'verified_source'}, expected_statuses={'leased_alignment'})
                return True
            with patch('word_lyrics_automation.nas_alignment_worker.run_alignment', side_effect=RuntimeError('acoustic/reference mismatch')), patch.object(worker, 'recover_from_sources', side_effect=recovered) as recover:
                worker.process(job)
            recover.assert_called_once()
            self.assertEqual(worker.store.get(job.id).status, 'scan_pending')


if __name__ == '__main__':
    unittest.main()
