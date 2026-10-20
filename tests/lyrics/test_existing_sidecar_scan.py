from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.resolver import LegacyGateway, SourceResolutionEngine
from word_lyrics_automation.source_worker import SourceWorker


try:
    import word_lyrics
except ImportError:
    spec = importlib.util.spec_from_file_location('word_lyrics',
        Path(__file__).resolve().parents[2] / 'bot/word_lyrics.py')
    word_lyrics = importlib.util.module_from_spec(spec)
    sys.modules['word_lyrics'] = word_lyrics
    spec.loader.exec_module(word_lyrics)


GROUPED = ('<tt xmlns="http://www.w3.org/ns/ttml" '
           'xmlns:i="http://music.apple.com/lyric-ttml-internal" i:timing="Word">'
           '<body><p begin="1" end="5"><span begin="1" end="3">甲乙</span>'
           '<span begin="3" end="5">丙丁</span></p></body></tt>')


class ExistingSidecarScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / 'song.m4a'
        self.audio.write_bytes(b'original audio')
        self.sidecar = self.audio.with_suffix('.ttml')
        self.sidecar.write_text(GROUPED)
        self.original = self.sidecar.read_bytes()
        self.store = QueueStore(self.root / 'queue.sqlite3')
        self.store.enqueue(self.audio)
        self.source_job = self.store.claim('source', owner='source-test')
        self.gateway = LegacyGateway(self.root / 'state')
        track = word_lyrics.Track(path=self.audio, title='Test', artist='Test', duration=10)
        self.gateway.inspect = lambda *_: track
        self.worker = object.__new__(SourceWorker)
        self.worker.gateway = self.gateway
        self.worker.engine = SourceResolutionEngine(self.gateway)
        self.worker.store = self.store
        self.scan = Mock(return_value=True)
        modules = patch.dict(sys.modules, {'youtube_ingest': SimpleNamespace(trigger_navidrome_scan=self.scan)})
        modules.start()
        self.addCleanup(modules.stop)
        environment = patch.dict(os.environ, {'AMDL_HAN_CHARACTER_TIMING_ENABLED': 'true'})
        environment.start()
        self.addCleanup(environment.stop)

    def prepare_scan(self):
        self.worker.process_source(self.source_job)
        self.assertEqual(self.store.get(self.source_job.id).status, 'scan_pending')
        self.assertEqual(self.sidecar.read_bytes(), self.original)
        return self.store.claim('scan', owner='scan-test')

    def test_existing_sidecar_completes_only_after_scan_and_verified_adaptation(self):
        job = self.prepare_scan()
        self.scan.assert_not_called()
        self.worker.process_scan(job)
        completed = self.store.get(job.id)
        self.assertEqual(completed.status, 'completed')
        self.scan.assert_called_once()
        self.assertEqual(completed.result['metrics']['timed_words'], 4)
        self.assertNotEqual(completed.result['candidate_sha256'], completed.result['existing_sidecar_sha256'])

    def test_audio_change_before_scan_never_deletes_the_preexisting_file(self):
        job = self.prepare_scan()
        self.audio.write_bytes(b'replaced audio')
        self.worker.process_scan(job)
        self.assertEqual(self.store.get(job.id).status, 'retry_scan')
        self.assertEqual(self.sidecar.read_bytes(), self.original)
        self.scan.assert_not_called()

    def test_audio_change_after_adaptation_rolls_back_only_the_derived_layer(self):
        job = self.prepare_scan()
        def replace_audio():
            self.audio.write_bytes(b'replaced audio')
            return True
        self.scan.side_effect = replace_audio
        self.worker.process_scan(job)
        self.assertEqual(self.store.get(job.id).status, 'retry_scan')
        self.assertEqual(self.sidecar.read_bytes(), self.original)

    def test_scan_outage_restores_preexisting_content_and_can_retry(self):
        job = self.prepare_scan()
        self.scan.return_value = False
        self.worker.process_scan(job)
        self.assertEqual(self.sidecar.read_bytes(), self.original)
        retry = self.store.get(job.id)
        self.assertEqual(retry.status, 'retry_scan')
        self.store.clock = lambda: retry.next_attempt_at + 1
        claimed = self.store.claim('scan', owner='retry')
        self.scan.return_value = True
        self.worker.process_scan(claimed)
        self.assertEqual(self.store.get(job.id).status, 'completed')

    def test_concurrent_sidecar_replacement_after_resolution_is_rejected(self):
        real_resolve = self.worker.engine.resolve
        replacement = GROUPED.replace('甲', '戊')
        def replace_sidecar(**kwargs):
            resolution = real_resolve(**kwargs)
            self.sidecar.write_text(replacement)
            return resolution
        self.worker.engine.resolve = replace_sidecar
        self.worker.process_source(self.source_job)
        self.assertEqual(self.store.get(self.source_job.id).status, 'retry_source')
        self.assertEqual(self.sidecar.read_text(), replacement)
        self.scan.assert_not_called()

    def test_concurrent_sidecar_replacement_before_scan_is_preserved(self):
        job = self.prepare_scan()
        self.sidecar.write_text('another publisher')
        self.worker.process_scan(job)
        self.assertEqual(self.sidecar.read_text(), 'another publisher')
        self.assertEqual(self.store.get(job.id).status, 'retry_scan')
        self.scan.assert_not_called()

    def reconcile_changed_audio(self):
        self.audio.write_bytes(b'replaced audio')
        self.worker.last_reconcile_at = 0
        self.worker.reconcile_interval = 0
        self.worker.owner = 'reconcile-test'
        self.worker.lease_seconds = 900
        self.worker.health = Mock()
        self.worker.process_source = Mock()
        self.assertTrue(self.worker.one_cycle())
        self.assertEqual(self.store.get(self.source_job.id).status, 'superseded')
        self.assertEqual(self.sidecar.read_bytes(), self.original)

    def test_normal_cycle_audio_reconciliation_preserves_preexisting_sidecar(self):
        self.prepare_scan()
        self.reconcile_changed_audio()

    def test_normal_cycle_after_character_crash_retracts_only_our_derived_layer(self):
        job = self.prepare_scan()
        self.scan.side_effect = SystemExit('simulated crash after derived write')
        with self.assertRaises(SystemExit):
            self.worker.process_scan(job)
        self.assertNotEqual(self.sidecar.read_bytes(), self.original)
        self.reconcile_changed_audio()


if __name__ == '__main__':
    unittest.main()
