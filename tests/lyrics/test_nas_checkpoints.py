import copy
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.alignment_backend_whispercpp import run_alignment
from word_lyrics_automation.nas_alignment_worker import NasAlignmentWorker
from whispercpp_test_fixtures import dtw_document, model_bytes


class NasCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / 'input.wav'
        self.model = self.root / 'model.bin'
        self.binary = self.root / 'whisper-cli'
        self.model.write_bytes(model_bytes(suffix=b'model version one'))
        self.binary.write_bytes(b'binary version one')
        self.calls = []
        self.failure = None
        self.configure(2)

    def configure(self, count):
        duration = 20 + count * 7
        with wave.open(str(self.audio), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b'\0\0' * duration * 16000)
        lines = [{'text': ['あさのうた', 'よるのうた'][i % 2], 'begin': 10 + i * 7, 'end': 15 + i * 7} for i in range(count)]
        self.payload = {'track': {'duration': duration}, 'reference_text': ''.join(line['text'] for line in lines), 'reference_lines': lines}

    def prepare(self, arguments, **kwargs):
        shutil.copyfile(self.audio, arguments[-1])

    def infer(self, arguments, *, timeout_seconds, progress):
        prefix = Path(arguments[arguments.index('-of') + 1])
        unit = prefix.name
        self.calls.append((unit, timeout_seconds))
        if unit == self.failure:
            # Simulate a process dying after writing incomplete raw output.
            prefix.with_suffix('.json').write_text('{"transcription":')
            raise RuntimeError('simulated interrupted inference')
        text = 'wrong' if unit == 'whisper-result' else arguments[arguments.index('--prompt') + 1]
        prefix.with_suffix('.json').write_text(json.dumps(dtw_document(text)))
        progress()

    def run_alignment(self):
        with patch('word_lyrics_automation.alignment_backend_whispercpp.subprocess.run', side_effect=self.prepare), patch('word_lyrics_automation.alignment_backend_whispercpp._run_with_progress', side_effect=self.infer):
            return run_alignment(audio=self.audio, payload=self.payload, output=self.root / 'result.ttml', report_path=self.root / 'report.json', binary=self.binary, model=self.model, work_dir=self.root / 'work', threads=4, timeout_seconds=4000)

    def test_41_lines_do_not_share_the_whole_song_deadline(self):
        self.configure(41)
        # The old shared clock would exhaust its 4000-second budget before
        # the first line. Each inference now gets its own bounded budget.
        with patch('word_lyrics_automation.alignment_backend_whispercpp.time.monotonic', side_effect=lambda: len(self.calls) * 4001):
            report = self.run_alignment()
        self.assertEqual(len(self.calls), 42)
        self.assertEqual(report['retried_windows'], 41)
        self.assertEqual(report['cue_count'], 205)
        self.assertTrue(all(budget >= 1800 for _, budget in self.calls[1:]))

    def test_interruption_resumes_after_completed_windows_with_identical_result(self):
        self.failure = 'line-2'
        with self.assertRaisesRegex(RuntimeError, 'simulated interrupted'):
            self.run_alignment()
        self.assertEqual([unit for unit, _ in self.calls], ['whisper-result', 'line-1', 'line-2'])
        self.failure = None
        self.calls.clear()
        report = self.run_alignment()
        self.assertEqual([unit for unit, _ in self.calls], ['line-2'])
        self.assertEqual(report['reused_acoustic_units'], 2)
        resumed = (self.root / 'result.ttml').read_text()
        shutil.rmtree(self.root / 'work')
        self.run_alignment()
        self.assertEqual((self.root / 'result.ttml').read_text(), resumed)

    def test_changed_audio_reference_model_or_binary_cannot_reuse_old_timestamps(self):
        self.run_alignment()
        changes = [
            lambda: self.model.write_bytes(model_bytes(suffix=b'model version two')),
            lambda: self.binary.write_bytes(b'binary version two'),
            lambda: self.audio.touch(),
            lambda: self.payload['reference_lines'][0].update(end=15.1),
        ]
        for change in changes:
            change()
            self.calls.clear()
            report = self.run_alignment()
            self.assertEqual(len(self.calls), 3)
            self.assertEqual(report['reused_acoustic_units'], 0)

    def test_corrupt_checkpoint_recomputes_only_the_damaged_unit(self):
        self.run_alignment()
        (self.root / 'work/line-1.checkpoint.json').write_text('{')
        self.calls.clear()
        report = self.run_alignment()
        self.assertEqual([unit for unit, _ in self.calls], ['line-1'])
        self.assertEqual(report['reused_acoustic_units'], 2)

    def test_resumed_results_still_pass_the_unchanged_quality_gate(self):
        self.run_alignment()
        saved = self.root / 'work/line-2.checkpoint.json'
        checkpoint = json.loads(saved.read_text())
        for segment in checkpoint['result']['segments']:
            for word in segment['words']:
                word['word'] = 'x'
        saved.write_text(json.dumps(checkpoint))
        with self.assertRaisesRegex(RuntimeError, 'acoustic/reference mismatch'):
            self.run_alignment()

    def test_worker_failure_preserves_checkpoint_directory_for_retry(self):
        with patch.dict('os.environ', {'AMDL_WORD_LYRICS_STATE_DIR': str(self.root / 'state')}):
            worker = NasAlignmentWorker()
        worker.store.enqueue(self.audio)
        source = worker.store.claim('source', owner='test')
        worker.store.defer_to_alignment(source.id, source.lease_token, payload=copy.deepcopy(self.payload), reason='test')
        job = worker.store.claim('alignment', owner='test')

        def interrupted(**kwargs):
            (kwargs['work_dir'] / 'whole-song.checkpoint.json').write_text('retained')
            raise RuntimeError('whisper.cpp alignment timed out')

        with patch('word_lyrics_automation.nas_alignment_worker.run_alignment', side_effect=interrupted):
            worker.process(job)
        self.assertEqual(worker.store.get(job.id).status, 'retry_alignment')
        self.assertEqual((worker.temp_root / job.fingerprint_data['sha256'] / 'whole-song.checkpoint.json').read_text(), 'retained')


if __name__ == '__main__':
    unittest.main()
