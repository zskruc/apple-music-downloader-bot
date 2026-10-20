from __future__ import annotations

import json
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.alignment_backend_whispercpp import (
    read_dtw_model_metadata, run_alignment, validate_dtw_result_clock,
)
from whispercpp_test_fixtures import dtw_document, model_bytes


class WhisperDtwModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_header_architecture_selects_preset_independently_of_filename(self):
        for layers, expected, name in [(4, 'large.v3.turbo', 'large-v3.bin'),
                                       (32, 'large.v3', 'turbo.bin')]:
            with self.subTest(layers=layers):
                path = self.root / name
                path.write_bytes(model_bytes(text_layers=layers))
                metadata = read_dtw_model_metadata(path)
                self.assertEqual(metadata['preset'], expected)
                self.assertEqual(metadata['hyperparameters']['text_layer'], layers)
                self.assertEqual(metadata['hyperparameters']['mels'], 128)

    def test_unknown_ambiguous_or_corrupt_models_never_guess_a_preset(self):
        header = list(struct.unpack('<12I', model_bytes()))
        variants = [b'', b'not a model', b'BAD!' + model_bytes()[4:]]
        for index, value in [(1, 51865), (3, 1024), (9, 16), (10, 80)]:
            changed = header.copy()
            changed[index] = value
            variants.append(struct.pack('<12I', *changed))
        path = self.root / 'ggml-large-v3-turbo-q5_0.bin'
        for raw in variants:
            with self.subTest(raw=raw.hex()):
                path.write_bytes(raw)
                with self.assertRaisesRegex(RuntimeError, 'unsupported whisper DTW model'):
                    read_dtw_model_metadata(path)
        with self.assertRaisesRegex(RuntimeError, 'cannot read whisper DTW model header'):
            read_dtw_model_metadata(self.root / 'missing.bin')


class WhisperDtwRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / 'audio.wav'
        with wave.open(str(self.audio), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b'\0\0' * 10 * 16000)
        self.model = self.root / 'deliberately-uninformative-name.bin'
        self.model.write_bytes(model_bytes())
        self.binary = self.root / 'whisper-cli'
        self.binary.write_bytes(b'fixed binary')
        self.calls = []
        self.payload = {'track': {'duration': 10}, 'reference_text': '春来',
                        'reference_lines': [{'text': '春来', 'begin': 2, 'end': 4}]}
        self.document = dtw_document('春来', language='zh', start_ms=2000, step_ms=500)
        # Heuristic offsets deliberately contradict the actual DTW anchors.
        for token in self.document['transcription'][0]['tokens']:
            token['offsets'] = {'from': 0, 'to': 9900}

    def prepare(self, arguments, **kwargs):
        shutil.copyfile(self.audio, arguments[-1])

    def infer(self, arguments, **kwargs):
        self.calls.append(arguments)
        prefix = Path(arguments[arguments.index('-of') + 1])
        prefix.with_suffix('.json').write_text(json.dumps(self.document))

    def align(self):
        with patch('word_lyrics_automation.alignment_backend_whispercpp.subprocess.run', side_effect=self.prepare), \
             patch('word_lyrics_automation.alignment_backend_whispercpp._run_with_progress', side_effect=self.infer):
            return run_alignment(audio=self.audio, payload=self.payload, output=self.root / 'candidate.ttml',
                                 report_path=self.root / 'report.json', binary=self.binary, model=self.model,
                                 work_dir=self.root / 'work', threads=2, timeout_seconds=300)

    def test_production_forces_verified_dtw_and_records_clock_semantics(self):
        report = self.align()
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertIn('-nfa', argv)
        self.assertNotIn('-fa', argv)
        self.assertEqual(argv[argv.index('-dtw') + 1], 'large.v3.turbo')
        self.assertEqual(argv[argv.index('-mc') + 1], '128')
        self.assertIn('-np', argv)
        self.assertEqual(report['clock_method'], 'whisper_dtw_group_boundaries_v1')
        self.assertTrue(report['clock_group_end_derived'])
        self.assertEqual(report['reference_interpolation'], 'estimated_within_verified_native_windows')
        saved = json.loads((self.root / 'work/whole-song.checkpoint.json').read_text())
        self.assertEqual(saved['result']['segments'][0]['words'][0]['start'], 2)
        self.assertEqual(saved['result']['segments'][0]['words'][0]['end'], 2.5)
        context = json.loads((self.root / 'work/checkpoint-context.json').read_text())
        self.assertEqual(context['version'], 2)
        self.assertEqual(context['dtw_model']['preset'], 'large.v3.turbo')
        self.assertEqual(report['matched_reference_character_coverage'], 1)
        self.calls.clear()
        resumed = self.align()
        self.assertEqual(self.calls, [])
        self.assertEqual(resumed['reused_acoustic_units'], 1)
        self.assertEqual(resumed['clock_diagnostics'], report['clock_diagnostics'])

    def test_missing_or_negative_dtw_never_falls_back_to_valid_offsets(self):
        for value in (None, -1):
            with self.subTest(value=value):
                token = self.document['transcription'][0]['tokens'][0]
                if value is None:
                    token.pop('t_dtw', None)
                else:
                    token['t_dtw'] = value
                with self.assertRaisesRegex(RuntimeError, 'DTW'):
                    self.align()
                self.assertFalse((self.root / 'candidate.ttml').exists())
                self.assertFalse((self.root / 'work/whole-song.checkpoint.json').exists())

    def test_runtime_model_architecture_must_match_actual_ggml_header(self):
        self.document['model']['text']['layer'] = 32
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            self.align()
        self.assertFalse((self.root / 'candidate.ttml').exists())

    def test_legacy_result_without_dtw_diagnostics_cannot_resume_even_with_same_key(self):
        self.align()
        path = self.root / 'work/whole-song.checkpoint.json'
        saved = json.loads(path.read_text())
        saved['result'].pop('clock_diagnostics')
        path.write_text(json.dumps(saved))
        self.calls.clear()
        report = self.align()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(report['reused_acoustic_units'], 0)

    def test_header_preset_change_cannot_reuse_turbo_checkpoint(self):
        primary = self.align()
        self.model.write_bytes(model_bytes(text_layers=32))
        self.document['model']['text']['layer'] = 32
        self.calls.clear()
        full = self.align()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][self.calls[0].index('-dtw') + 1], 'large.v3')
        self.assertNotEqual(primary['checkpoint_key'], full['checkpoint_key'])
        self.assertEqual(full['reused_acoustic_units'], 0)

    def test_terminal_unknown_is_reported_and_excluded_from_acoustic_matches(self):
        self.payload.update(reference_text='春风又绿', reference_lines=[{'text': '春风又绿', 'begin': 2, 'end': 4}])
        self.document = dtw_document('春风又绿', language='zh', start_ms=2000, step_ms=400)
        self.document['transcription'][0]['tokens'].pop()  # No measured following boundary.
        report = self.align()
        self.assertEqual(report['matched_reference_character_coverage'], .75)
        diagnostics = report['clock_diagnostics']
        self.assertEqual(diagnostics['untimed_unit_count'], 1)
        self.assertEqual(diagnostics['untimed_character_count'], 1)
        self.assertEqual(diagnostics['units']['whole-song']['timed_character_count'], 3)
        self.assertEqual(report['cue_count'], 4)  # Existing bounded reference interpolation remains estimated.

    def test_fresh_globally_overlapping_segments_fail_before_matching(self):
        additional = dtw_document('风', language='zh', start_ms=2750, step_ms=500)
        self.document['transcription'].extend(additional['transcription'])
        # Each raw segment is internally monotonic, but the second starts
        # before the first segment's last timed word ends at 3.0 seconds.
        with self.assertRaisesRegex(RuntimeError, 'overlaps or reverses'):
            self.align()
        self.assertFalse((self.root / 'candidate.ttml').exists())
        self.assertFalse((self.root / 'work/whole-song.checkpoint.json').exists())

    def test_fresh_clock_is_bounded_by_actual_wav_not_track_metadata(self):
        self.payload['track']['duration'] = 100
        self.document = dtw_document('春来', language='zh', start_ms=9700, step_ms=200)
        with self.assertRaisesRegex(RuntimeError, 'exceeds its actual audio crop'):
            self.align()
        self.assertFalse((self.root / 'candidate.ttml').exists())
        boundary = {'segments': [{'words': [{'start': 9.99, 'end': 10.02}]}]}
        validate_dtw_result_clock(boundary, self.audio)  # Exactly one DTW frame is allowed.
        boundary['segments'][0]['words'][0]['end'] = 10.0201
        with self.assertRaisesRegex(RuntimeError, 'exceeds its actual audio crop'):
            validate_dtw_result_clock(boundary, self.audio)

    def test_invalid_cached_word_bounds_are_recomputed_before_matching(self):
        self.align()
        path = self.root / 'work/whole-song.checkpoint.json'
        original = json.loads(path.read_text())
        mutations = [{'start': float('nan')}, {'end': float('inf')},
                     {'start': -0.01}, {'start': 2.5, 'end': 2.5},
                     {'end': 10.04}, {'start': 2.4}]
        for index, changes in enumerate(mutations):
            with self.subTest(changes=changes):
                saved = json.loads(json.dumps(original))
                words = saved['result']['segments'][0]['words']
                words[1 if index == len(mutations) - 1 else 0].update(changes)
                path.write_text(json.dumps(saved))
                self.calls.clear()
                report = self.align()
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(report['reused_acoustic_units'], 0)


if __name__ == '__main__':
    unittest.main()
