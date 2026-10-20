from __future__ import annotations

import importlib.util
from dataclasses import replace
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.fingerprint import media_fingerprint
from word_lyrics_automation.models import LyricLine, TimedToken
from word_lyrics_automation.resolver import (Candidate, CandidateRejected, LegacyGateway,
                                            SourceResolutionEngine, _rollback_sidecar)
from word_lyrics_automation.ttml import render_word_ttml


# Production provides the deployed parser at /app/word_lyrics.py. Local tests
# exercise the same checked-out parser, not a mock of its timing comparison.
try:
    import word_lyrics
except ImportError:
    source = Path(__file__).resolve().parents[2] / 'bot/word_lyrics.py'
    spec = importlib.util.spec_from_file_location('word_lyrics', source)
    word_lyrics = importlib.util.module_from_spec(spec)
    sys.modules['word_lyrics'] = word_lyrics
    spec.loader.exec_module(word_lyrics)


class SameFileValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'song.m4a'
        self.audio.write_bytes(b'immutable test audio')
        self.audio.with_suffix('.lrc').write_text('[00:10.00]甲乙\n[00:20.00]丙丁\n[00:30.00]戊己\n')
        self.fingerprint = media_fingerprint(self.audio)
        self.gateway = LegacyGateway(self.root / 'state')
        self.track = word_lyrics.Track(path=self.audio, title='Test', artist='Test', album='Test', duration=60)
        self.gateway.inspect = lambda *_: self.track
        self.engine = SourceResolutionEngine(self.gateway)
        self.payload = self.gateway.alignment_payload(self.track, self.fingerprint)
        self.report = {
            'mode': 'reference_aligned', 'alignment_engine': 'whisper.cpp',
            'source_fingerprint': self.fingerprint, 'reference_text_similarity': 1,
            'matched_reference_character_coverage': .95,
            'raw_transcript_text_similarity': .9, 'unverified_line_fraction': 0,
        }
        self.candidate_path = self.root / 'candidate.ttml'
        self.make_candidate()

    def make_candidate(self, texts=('甲乙', '丙丁', '戊己'), shift=1.1):
        lines = []
        for i, text in enumerate(texts):
            begin = 10 + i * 10 + shift
            tokens = tuple(TimedToken(char, begin + j * .5, begin + (j + 1) * .5) for j, char in enumerate(text))
            lines.append(LyricLine(text, tokens[0].begin, tokens[-1].end, tokens))
        self.candidate_path.write_text(render_word_ttml(lines, duration=60, title='Test', artist='Test'))

    def validate(self):
        return self.engine.validate_mac_candidate(
            media_path=self.audio, metadata={}, expected_fingerprint=self.fingerprint,
            candidate_path=self.candidate_path, report=self.report, alignment_payload=self.payload,
        )

    def test_acoustic_refinement_passes_even_when_old_line_starts_differ(self):
        candidate = word_lyrics.parse_ttml(self.candidate_path.read_text())
        old = word_lyrics.validate_candidate(candidate, self.gateway.references(self.audio),
                                             exact_identity=True, identity_confidence='acoustic_same_file', duration=60)
        self.assertFalse(old.accepted)
        self.assertEqual(old.reason, 'existing_lyrics_mismatch')
        publication = self.validate()
        self.assertEqual(publication.provider, 'nas_whispercpp')
        self.assertEqual(publication.metrics['validation_method'], 'same_file_reference_alignment')
        self.assertEqual(media_fingerprint(self.audio), self.fingerprint)
        self.assertEqual(self.audio.with_suffix('.ttml').read_text(), self.candidate_path.read_text())

    def test_external_source_still_requires_recording_timing_match(self):
        candidate = Candidate('amll', '123', self.candidate_path.read_text(), True, 'exact_song_id', {})
        with self.assertRaisesRegex(CandidateRejected, 'existing_lyrics_mismatch') as failure:
            self.gateway.publish(self.track, candidate, expected_fingerprint=self.fingerprint)
        self.assertEqual(failure.exception.metrics['median_absolute_delta_seconds'], 1.1)

    def test_beijing_time_distribution_reproduces_old_false_rejection(self):
        # The retained NAS replay had 19/26 starts within 0.8 s, a 0.36 s
        # median difference and a 1.08 s p95 residual. Its text was identical.
        self.track = replace(self.track, duration=300)
        references, lines = [], []
        for i in range(26):
            begin = 10 + i * 10
            text = '甲' + chr(0x4e00 + i)
            references.append(f'[{begin // 60:02d}:{begin % 60:02d}.00]{text}')
            start = begin - (.36 if i < 19 else 1.44)
            tokens = (TimedToken(text[0], start, start + .5), TimedToken(text[1], start + .5, start + 1))
            lines.append(LyricLine(text, start, start + 1, tokens))
        self.audio.with_suffix('.lrc').write_text('\n'.join(references))
        self.payload = self.gateway.alignment_payload(self.track, self.fingerprint)
        self.candidate_path.write_text(render_word_ttml(lines, duration=300, title='Test', artist='Test'))
        old = word_lyrics.validate_candidate(word_lyrics.parse_ttml(self.candidate_path.read_text()),
                                             self.gateway.references(self.audio), exact_identity=True,
                                             identity_confidence='acoustic_same_file', duration=300)
        self.assertEqual(old.reason, 'existing_lyrics_mismatch')
        self.assertEqual(old.metrics['median_absolute_delta_seconds'], .36)
        self.assertEqual(old.metrics['p95_residual_seconds'], 1.08)
        self.assertEqual(self.validate().metrics['timed_lines'], 26)

    def test_reference_text_change_is_rejected(self):
        self.make_candidate(texts=('甲乙', '丙丁', '戊庚'))
        with self.assertRaisesRegex(CandidateRejected, 'candidate_text_not_exactly_equal'):
            self.validate()

    def test_equal_text_prefers_explicit_ttml_line_ends(self):
        self.audio.with_suffix('.ttml').write_text('''<tt xmlns="http://www.w3.org/ns/ttml"><body><div>
            <p begin="10s" end="14s">甲乙</p><p begin="20s" end="24s">丙丁</p><p begin="30s" end="34s">戊己</p>
            </div></body></tt>''')
        payload = self.gateway.alignment_payload(self.track, self.fingerprint)
        self.assertTrue(payload['reference_path'].endswith('.ttml'))
        self.assertEqual([line['end'] for line in payload['reference_lines']], [14, 24, 34])

    def test_shorter_ttml_cannot_replace_complete_lrc_text(self):
        self.audio.with_suffix('.ttml').write_text('''<tt xmlns="http://www.w3.org/ns/ttml"><body><div>
            <p begin="10s" end="14s">甲乙</p><p begin="20s" end="24s">丙丁</p>
            </div></body></tt>''')
        payload = self.gateway.alignment_payload(self.track, self.fingerprint)
        self.assertTrue(payload['reference_path'].endswith('.lrc'))
        self.assertEqual(len(payload['reference_lines']), 3)

    def test_same_whole_text_with_different_line_boundaries_is_rejected(self):
        self.make_candidate(texts=('甲', '乙丙丁', '戊己'))
        with self.assertRaisesRegex(CandidateRejected, 'alignment_reference_line_text_mismatch'):
            self.validate()

    def test_same_file_alignment_preserves_original_script_spaces_and_punctuation(self):
        for reference, changed in (("風與雲", "风与云"), ("甲 (乙)", "甲( 乙)"),
                                   ("甲，乙", "甲乙"), ("ＡＢ", "AB")):
            with self.subTest(reference=reference, changed=changed):
                payload = {"reference_text": reference,
                           "reference_lines": [{"text": reference, "begin": 10, "end": 14}]}
                token = TimedToken(changed, 10, 14)
                line = LyricLine(changed, 10, 14, (token,))
                with self.assertRaisesRegex(CandidateRejected, "alignment_reference_line_text_mismatch"):
                    self.gateway._validate_alignment_reference([line], payload, 60)
                original = LyricLine(reference, 10, 14, (TimedToken(reference, 10, 14),))
                self.gateway._validate_alignment_reference([original], payload, 60)

    def test_repeated_chorus_cannot_move_to_another_time_window(self):
        self.make_candidate(shift=15)
        with self.assertRaisesRegex(CandidateRejected, 'alignment_reference_time_window_mismatch'):
            self.validate()

    def test_changed_reference_on_disk_is_rejected(self):
        self.audio.with_suffix('.lrc').write_text('[00:10.00]甲乙\n[00:20.00]丙丁\n[00:30.00]戊庚\n')
        with self.assertRaisesRegex(CandidateRejected, 'candidate_text_not_exactly_equal_to_reference'):
            self.validate()

    def test_current_reference_spacing_change_cannot_be_hidden_by_normalization(self):
        self.audio.with_suffix('.lrc').write_text('[00:10.00]甲 乙\n[00:20.00]丙丁\n[00:30.00]戊己\n')
        with self.assertRaisesRegex(CandidateRejected, 'alignment_reference_line_text_mismatch'):
            self.validate()
        self.assertFalse(self.audio.with_suffix('.ttml').exists())

    def test_current_reference_window_change_is_rechecked_before_publication(self):
        self.audio.with_suffix('.lrc').write_text('[00:10.00]甲乙\n[00:40.00]丙丁\n[00:50.00]戊己\n')
        with self.assertRaisesRegex(CandidateRejected, 'alignment_reference_time_window_mismatch'):
            self.validate()
        self.assertFalse(self.audio.with_suffix('.ttml').exists())

    def test_new_longer_non_authoritative_sidecar_does_not_replace_original_authority(self):
        self.audio.with_suffix('.ttml').write_text('''<tt><body><p begin="1" end="50">
            This newly added translation has substantially more text than the native lyric.
            </p></body></tt>''')
        self.assertEqual(self.validate().provider, 'nas_whispercpp')

    def test_trusted_online_reference_without_local_authority_remains_valid(self):
        self.payload = {**self.payload, 'reference_path': '', 'reference_provider': 'netease_exact_lrc'}
        with patch.object(self.gateway, 'references', return_value=[]):
            self.assertEqual(self.validate().provider, 'nas_whispercpp')

    def test_changed_audio_is_rejected(self):
        self.audio.write_bytes(b'different recording')
        with self.assertRaisesRegex(CandidateRejected, 'audio_changed_since_enqueue'):
            self.validate()

    def test_audio_change_during_publication_removes_new_sidecar(self):
        write = self.gateway.service.write_sidecar

        def change_audio(*args):
            paths = write(*args)
            self.audio.write_bytes(b'changed while publishing')
            return paths

        with patch.object(self.gateway.service, 'write_sidecar', side_effect=change_audio), \
                patch.object(self.gateway.service.audit, 'record') as record:
            with self.assertRaisesRegex(CandidateRejected, 'audio_changed_while_publishing'):
                self.validate()
        self.assertFalse(self.audio.with_suffix('.ttml').exists())
        record.assert_not_called()

    def test_audio_change_during_publication_restores_existing_sidecar(self):
        target = self.audio.with_suffix('.ttml')
        original = '''<tt xmlns="http://www.w3.org/ns/ttml"><body><div>
            <p begin="10s" end="14s">甲乙</p><p begin="20s" end="24s">丙丁</p><p begin="30s" end="34s">戊己</p>
            </div></body></tt>'''
        target.write_text(original)
        self.payload = self.gateway.alignment_payload(self.track, self.fingerprint)
        write = self.gateway.service.write_sidecar

        def change_audio(*args):
            paths = write(*args)
            self.audio.write_bytes(b'changed while publishing')
            return paths

        with patch.object(self.gateway.service, 'write_sidecar', side_effect=change_audio):
            with self.assertRaisesRegex(CandidateRejected, 'audio_changed_while_publishing'):
                self.validate()
        self.assertEqual(target.read_text(), original)

    def test_publication_rollback_preserves_a_concurrently_replaced_sidecar(self):
        target = self.audio.with_suffix('.ttml')
        write = self.gateway.service.write_sidecar

        def replace_after_publication(*args):
            paths = write(*args)
            self.audio.write_bytes(b'changed while publishing')
            target.write_text('a newer publication from another writer')
            return paths

        with patch.object(self.gateway.service, 'write_sidecar', side_effect=replace_after_publication):
            with self.assertRaisesRegex(CandidateRejected, 'audio_changed_while_publishing'):
                self.validate()
        self.assertEqual(target.read_text(), 'a newer publication from another writer')

    def test_rollback_rechecks_current_sidecar_after_preparing_the_backup(self):
        target = self.audio.with_suffix('.ttml')
        target.write_text('this candidate')
        backup = self.root / 'backup.ttml'
        backup.write_text('previous lyric')
        copy = shutil.copy2

        def concurrent_publication(*args):
            result = copy(*args)
            target.write_text('a newer publication')
            return result

        with patch('word_lyrics_automation.resolver.shutil.copy2', side_effect=concurrent_publication):
            _rollback_sidecar(str(target), str(backup), hashlib.sha256(b'this candidate').hexdigest())
        self.assertEqual(target.read_text(), 'a newer publication')
        self.assertEqual(backup.read_text(), 'previous lyric')
        self.assertEqual(list(self.root.glob('.*.rollback.*')), [])

    def test_unreadable_audio_after_publication_rolls_back_the_sidecar(self):
        with patch('word_lyrics_automation.resolver.media_fingerprint',
                   side_effect=[self.fingerprint, OSError('audio became unreadable')]):
            with self.assertRaisesRegex(OSError, 'audio became unreadable'):
                self.validate()
        self.assertFalse(self.audio.with_suffix('.ttml').exists())

    def test_existing_word_shortcut_rejects_an_old_audio_fingerprint(self):
        self.validate()
        self.audio.write_bytes(b'a different recording with the same sidecar path')
        with self.assertRaisesRegex(CandidateRejected, 'audio_changed_since_enqueue'):
            self.engine.resolve(media_path=self.audio, metadata={}, expected_fingerprint=self.fingerprint)

    def test_existing_word_shortcut_accepts_the_unchanged_recording(self):
        self.validate()
        result = self.engine.resolve(media_path=self.audio, metadata={}, expected_fingerprint=self.fingerprint)
        self.assertEqual(result.reason, 'existing_word_sidecar')

    def test_existing_word_validation_uses_actual_audio_duration(self):
        self.make_candidate(shift=0)
        self.audio.with_suffix('.ttml').write_bytes(self.candidate_path.read_bytes())
        self.assertIsNotNone(self.gateway.existing_word(self.audio, 60))
        self.assertIsNone(self.gateway.existing_word(self.audio, 10))

    def test_existing_word_digest_binds_the_validated_bytes(self):
        self.validate()
        result = self.gateway.existing_word(self.audio, 60)
        self.assertEqual(result['candidate_sha256'],
            hashlib.sha256(self.audio.with_suffix('.ttml').read_bytes()).hexdigest())

    def test_alignment_line_cannot_merely_overlap_its_reference_window(self):
        payload = {'reference_text': '甲乙',
                   'reference_lines': [{'text': '甲乙', 'begin': 10, 'end': 14}]}
        for begin, end in [(0, 12), (12, 20), (0, 20)]:
            with self.subTest(begin=begin, end=end):
                tokens = (TimedToken('甲', begin, (begin + end) / 2),
                          TimedToken('乙', (begin + end) / 2, end))
                lines = [LyricLine('甲乙', begin, end, tokens)]
                with self.assertRaisesRegex(CandidateRejected, 'alignment_reference_time_window_mismatch'):
                    self.gateway._validate_alignment_reference(lines, payload, 60)

    def test_wrong_report_fingerprint_is_rejected(self):
        self.report['source_fingerprint'] = {}
        with self.assertRaisesRegex(CandidateRejected, 'report_fingerprint_mismatch'):
            self.validate()

    def test_low_or_nonfinite_quality_is_rejected(self):
        baseline = dict(self.report)
        for key, value in [('matched_reference_character_coverage', .69),
                           ('raw_transcript_text_similarity', .59),
                           ('unverified_line_fraction', .11),
                           ('reference_text_similarity', float('nan')),
                           ('matched_reference_character_coverage', float('inf'))]:
            with self.subTest(key=key, value=value):
                self.report = {**baseline, key: value}
                with self.assertRaisesRegex(CandidateRejected, 'quality_gate_failed'):
                    self.validate()
        self.assertFalse(self.audio.with_suffix('.ttml').exists())

    def test_free_transcription_is_not_publishable(self):
        self.report['mode'] = 'free_transcription'
        with self.assertRaisesRegex(CandidateRejected, 'free_transcription_without_reference'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
