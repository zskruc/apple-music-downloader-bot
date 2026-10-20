from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from word_lyrics_automation.alignment_backend_whispercpp import run_alignment
from whispercpp_test_fixtures import dtw_document, model_bytes
from word_lyrics_automation.alignment_retry import reference_language
from word_lyrics_automation.nas_alignment_worker import NasAlignmentWorker


class NasQualityFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        with patch.dict('os.environ', {'AMDL_WORD_LYRICS_STATE_DIR': str(self.root / 'state')}):
            self.worker = NasAlignmentWorker()
        self.audio = self.root / 'song.wav'
        self.audio.write_bytes(b'unchanged recording')
        self.worker.model = self.root / 'turbo.bin'
        self.worker.model.write_bytes(b'primary model')
        self.worker.model_sha1 = hashlib.sha1(self.worker.model.read_bytes()).hexdigest()
        self.worker.fallback_model = self.root / 'large-v3.bin'
        self.worker.fallback_model.write_bytes(b'full large model')
        self.worker.expected_fallback_model_sha1 = hashlib.sha1(self.worker.fallback_model.read_bytes()).hexdigest()
        self.worker.store.enqueue(self.audio)
        source = self.worker.store.claim('source', owner='test')
        self.worker.store.defer_to_alignment(source.id, source.lease_token, payload={
            'track': {'duration': 10}, 'reference_text': '呢個要做大哥',
            'reference_lines': [{'text': '呢個要做大哥', 'begin': 1, 'end': 5}],
        }, reason='test')
        self.job = self.worker.store.claim('alignment', owner='test')
        self.calls = []
        recovery = patch.object(self.worker, 'recover_from_sources', return_value=False)
        self.recovery = recovery.start()
        self.addCleanup(recovery.stop)

    def successful_result(self, arguments):
        arguments['output'].write_text('candidate for existing NAS validation')
        return {'mode': 'reference_aligned', 'model': arguments['model'].name,
                'language': arguments['payload'].get('alignment_language', 'zh'),
                'matched_reference_character_coverage': .9,
                'raw_transcript_text_similarity': .8}

    def primary_quality_failure(self, arguments, detected='zh'):
        (arguments['work_dir'] / 'whole-song.checkpoint.json').write_text(json.dumps({
            'result': {'language': detected, 'segments': []}}))
        raise RuntimeError('acoustic/reference mismatch: coverage=0.536, similarity=0.484')

    def infer(self, **arguments):
        self.calls.append(arguments)
        if len(self.calls) == 1:
            self.primary_quality_failure(arguments)
        return self.successful_result(arguments)

    def process(self, inference=None):
        with patch('word_lyrics_automation.nas_alignment_worker.run_alignment',
                   side_effect=inference or self.infer):
            self.worker.process(self.job)
        return self.worker.store.get(self.job.id)

    def test_quality_failure_runs_one_verified_full_model_and_submits_for_existing_validation(self):
        saved = self.process()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([call['model'] for call in self.calls],
                         [self.worker.model, self.worker.fallback_model])
        self.assertNotIn('alignment_language', self.calls[0]['payload'])
        self.assertEqual(self.calls[1]['payload']['alignment_language'], 'yue')
        self.assertNotIn('alignment_language', self.job.payload)
        self.assertEqual(self.calls[1]['work_dir'], self.calls[0]['work_dir'] / 'quality-fallback')
        self.assertEqual(saved.status, 'candidate_pending')
        report = saved.payload['candidate_report']
        self.assertIn('coverage=0.536', report['primary_error'])
        self.assertEqual(report['fallback_model'], self.worker.fallback_model.name)
        self.assertEqual(report['fallback_language'], 'yue')
        self.assertEqual(report['model_sha1'], self.worker.expected_fallback_model_sha1)
        self.assertEqual(json.loads(self.calls[1]['report_path'].read_text()), report)
        self.recovery.assert_not_called()

    def test_second_quality_failure_rechecks_sources_without_recursing(self):
        def fail(**arguments):
            self.calls.append(arguments)
            self.primary_quality_failure(arguments)
        saved = self.process(fail)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(saved.status, 'dead_letter')
        self.assertEqual(saved.payload['nas_quality_fallback']['status'], 'failed')
        self.assertIn('primary_error', saved.payload['nas_quality_fallback'])
        self.recovery.assert_called_once()

    def test_missing_full_model_keeps_existing_source_recovery(self):
        self.worker.fallback_model.unlink()
        saved = self.process()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(saved.status, 'dead_letter')
        self.assertEqual(saved.payload['nas_quality_fallback']['status'], 'unavailable')
        self.recovery.assert_called_once()

    def test_corrupt_full_model_is_not_run_or_submitted(self):
        self.worker.fallback_model.write_bytes(b'corrupt model')
        saved = self.process()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(saved.status, 'dead_letter')
        self.assertIn('fallback acoustic model digest mismatch', saved.last_error)
        self.assertEqual(saved.payload['nas_quality_fallback']['status'], 'invalid_model')
        self.assertNotIn('candidate_report', saved.payload)
        self.recovery.assert_called_once()

    def test_primary_success_does_not_run_full_model(self):
        def success(**arguments):
            self.calls.append(arguments)
            return self.successful_result(arguments)
        saved = self.process(success)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(saved.status, 'candidate_pending')
        self.assertNotIn('fallback_model', saved.payload['candidate_report'])
        self.recovery.assert_not_called()

    def test_no_trusted_reference_does_not_run_full_model(self):
        self.job.payload['reference_text'] = ''
        saved = self.process()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(saved.status, 'dead_letter')
        self.assertNotIn('nas_quality_fallback', saved.payload)

    def test_transient_primary_timeout_does_not_trigger_quality_fallback(self):
        def timeout(**arguments):
            self.calls.append(arguments)
            raise RuntimeError('whisper.cpp alignment timed out')
        saved = self.process(timeout)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(saved.status, 'retry_alignment')
        self.recovery.assert_not_called()

    def test_audio_changed_after_primary_failure_cannot_start_fallback(self):
        def changed(**arguments):
            self.calls.append(arguments)
            self.audio.write_bytes(b'a different recording')
            self.primary_quality_failure(arguments)
        saved = self.process(changed)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('audio_changed_before_nas_quality_fallback', saved.last_error)
        self.assertNotIn('candidate_report', saved.payload)
        self.recovery.assert_not_called()

    def test_audio_changed_before_claimed_work_never_runs_either_model(self):
        self.audio.write_bytes(b'a different recording')
        saved = self.process()
        self.assertEqual(self.calls, [])
        self.assertIn('audio_changed_since_enqueue', saved.last_error)
        self.assertNotIn('candidate_report', saved.payload)
        self.recovery.assert_not_called()

    def test_audio_changed_during_full_model_cannot_submit_candidate(self):
        def changed(**arguments):
            self.calls.append(arguments)
            if len(self.calls) == 1:
                self.primary_quality_failure(arguments)
            report = self.successful_result(arguments)
            self.audio.write_bytes(b'a different recording')
            return report
        saved = self.process(changed)
        self.assertEqual(len(self.calls), 2)
        self.assertIn('audio_changed_during_nas_alignment', saved.last_error)
        self.assertNotIn('candidate_report', saved.payload)
        self.recovery.assert_not_called()

    def test_non_han_fallback_preserves_detected_language(self):
        self.job.payload['reference_text'] = 'the night is young'
        def infer(**arguments):
            self.calls.append(arguments)
            if len(self.calls) == 1:
                self.primary_quality_failure(arguments, detected='en')
            return self.successful_result(arguments)
        self.process(infer)
        self.assertEqual(self.calls[1]['payload']['alignment_language'], 'en')

    def test_explicit_language_and_japanese_hint_are_preserved(self):
        self.assertEqual(self.worker.fallback_language({'alignment_language': 'zh',
                                                       'reference_text': '中文'}, self.root), 'zh')
        self.assertEqual(self.worker.fallback_language({'reference_text': 'あさのうた'}, self.root), 'ja')

    def test_reusing_primary_model_as_fallback_does_not_repeat_inference(self):
        self.worker.fallback_model = self.worker.model
        saved = self.process()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(saved.status, 'dead_letter')
        self.assertIn('identical', saved.payload['nas_quality_fallback']['error'])

    def test_source_diagnostics_are_optional_and_do_not_block_verified_recovery(self):
        diagnostic = {}
        resolution = SimpleNamespace(publication=None, existing={'provider': 'verified_source'},
                                     reason='verified', payload={'source_rejections': ['retained']})
        engine = SimpleNamespace(resolve=Mock(return_value=resolution))
        # Call the implementation directly because other tests stub it to
        # avoid network requests while checking queue state transitions.
        with patch('word_lyrics_automation.resolver.SourceResolutionEngine', return_value=engine), \
                patch('word_lyrics_automation.resolver.LegacyGateway'):
            recovered = NasAlignmentWorker.recover_from_sources(self.worker, self.job,
                                                               'primary quality failure', diagnostic)
        self.assertTrue(recovered)
        self.assertEqual(self.worker.store.get(self.job.id).status, 'scan_pending')
        self.assertEqual(diagnostic['post_alignment_source_diagnostics']['observations'], [])
        self.assertEqual(diagnostic['post_alignment_source_diagnostics']['rejections'], ['retained'])

    def test_broken_diagnostics_never_block_verified_source_recovery(self):
        resolution = SimpleNamespace(publication=None, existing={'provider': 'verified_source'},
                                     reason='verified', alignment_payload=object())
        engine = SimpleNamespace(resolve=Mock(return_value=resolution),
                                 source_observations=Mock(side_effect=RuntimeError('diagnostic error')))
        with patch('word_lyrics_automation.resolver.SourceResolutionEngine', return_value=engine), \
                patch('word_lyrics_automation.resolver.LegacyGateway'):
            recovered = NasAlignmentWorker.recover_from_sources(self.worker, self.job, 'error', {})
        self.assertTrue(recovered)
        self.assertEqual(self.worker.store.get(self.job.id).status, 'scan_pending')


class ExplicitWhisperLanguageTests(unittest.TestCase):
    def test_explicit_language_codes_are_validated_without_allowing_extra_arguments(self):
        for language in ('auto', 'zh', 'yue', 'ja', 'en', 'ko', 'fr'):
            self.assertEqual(reference_language({'alignment_language': language}), language)
        for language in ('zh --model other.bin', '--help', '', None, ['zh'], 'mandarin'):
            with self.subTest(language=language), self.assertRaisesRegex(ValueError, 'alignment_language'):
                reference_language({'alignment_language': language})
        self.assertIsNone(reference_language({'reference_text': '中文'}))
        self.assertEqual(reference_language({'reference_text': 'あさ'}), 'ja')

    def test_default_checkpoints_resume_but_explicit_language_and_model_never_mix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'input.wav'
            with wave.open(str(audio), 'wb') as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(b'\0\0' * 10 * 16000)
            binary, model = root / 'whisper-cli', root / 'model.bin'
            binary.write_bytes(b'binary')
            model.write_bytes(model_bytes(suffix=b'turbo model'))
            payload = {'track': {'duration': 10}, 'reference_text': '春来',
                       'reference_lines': [{'text': '春来', 'begin': 2, 'end': 4}]}
            languages = []
            def prepare(arguments, **kwargs):
                shutil.copyfile(audio, arguments[-1])
            def infer(arguments, **kwargs):
                language = arguments[arguments.index('-l') + 1]
                languages.append(language)
                prefix = Path(arguments[arguments.index('-of') + 1])
                prefix.with_suffix('.json').write_text(json.dumps(dtw_document(
                    '春来', language='zh' if language == 'auto' else language,
                    start_ms=2000, step_ms=500,
                    text_layers=32 if model.read_bytes()[36:40] == (32).to_bytes(4, 'little') else 4)))
            def run():
                return run_alignment(audio=audio, payload=payload, output=root / 'candidate.ttml',
                                     report_path=root / 'report.json', binary=binary, model=model,
                                     work_dir=root / 'work', threads=1, timeout_seconds=300)
            with patch('word_lyrics_automation.alignment_backend_whispercpp.subprocess.run', side_effect=prepare), \
                    patch('word_lyrics_automation.alignment_backend_whispercpp._run_with_progress', side_effect=infer):
                default = run()
                context = json.loads((root / 'work/checkpoint-context.json').read_text())
                self.assertNotIn('alignment_language', context)
                resumed = run()
                self.assertEqual(languages, ['auto'])
                self.assertEqual(resumed['checkpoint_key'], default['checkpoint_key'])
                self.assertEqual(resumed['reused_acoustic_units'], 1)
                payload['alignment_language'] = 'yue'
                cantonese = run()
                self.assertEqual(languages, ['auto', 'yue'])
                self.assertNotEqual(cantonese['checkpoint_key'], default['checkpoint_key'])
                self.assertEqual(cantonese['reused_acoustic_units'], 0)
                self.assertEqual(run()['reused_acoustic_units'], 1)
                model.write_bytes(model_bytes(text_layers=32,suffix=b'full large model'))
                large = run()
                self.assertNotEqual(large['checkpoint_key'], cantonese['checkpoint_key'])
                self.assertEqual(languages, ['auto', 'yue', 'yue'])
                self.assertEqual(large['reused_acoustic_units'], 0)
                self.assertEqual(large['completed_acoustic_units'], 1)


if __name__ == '__main__':
    unittest.main()
