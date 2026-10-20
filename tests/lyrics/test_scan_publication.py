from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.source_worker import SourceWorker

try:
    import word_lyrics
except ImportError:
    source = Path(__file__).resolve().parents[2] / 'bot/word_lyrics.py'
    spec = importlib.util.spec_from_file_location('word_lyrics', source)
    word_lyrics = importlib.util.module_from_spec(spec)
    sys.modules['word_lyrics'] = word_lyrics
    spec.loader.exec_module(word_lyrics)


class ScanPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / 'song.m4a'
        self.audio.write_bytes(b'original recording')
        self.sidecar = self.audio.with_suffix('.ttml')
        self.sidecar.write_text('published candidate')
        self.backup = self.root / 'prior.ttml'
        self.backup.write_text('original line lyric')
        self.store = QueueStore(self.root / 'queue.sqlite3')
        job = self.store.enqueue(self.audio).job
        source = self.store.claim('source', owner='scan-test')
        publication = {'sidecar_path': str(self.sidecar), 'backup_path': str(self.backup),
            'candidate_sha256': hashlib.sha256(self.sidecar.read_bytes()).hexdigest()}
        self.store.transition_to_scan(job.id, source.lease_token, result=publication,
            expected_statuses={'leased_source'})
        self.job = self.store.claim('scan', owner='scan-test')
        self.worker = object.__new__(SourceWorker)
        self.worker.store = self.store
        self.worker.gateway = SimpleNamespace(service=SimpleNamespace(
            normalize_character_sidecar=lambda _: None))
        self.scan = Mock(return_value=True)
        module = SimpleNamespace(trigger_navidrome_scan=self.scan)
        self.modules = patch.dict(sys.modules, {'youtube_ingest': module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_current_publication_completes_after_scan(self):
        self.worker.process_scan(self.job)
        self.assertEqual(self.store.get(self.job.id).status, 'completed')
        self.scan.assert_called_once()

    def test_audio_change_before_scan_restores_prior_lyric(self):
        self.audio.write_bytes(b'another recording')
        self.worker.process_scan(self.job)
        self.assertEqual(self.sidecar.read_text(), 'original line lyric')
        self.scan.assert_not_called()
        self.assertEqual(self.store.get(self.job.id).status, 'retry_scan')
        replacement = self.store.refresh_changed_active_jobs()[0].job
        self.assertEqual(replacement.status, 'queued_source')
        self.assertEqual(self.store.get(self.job.id).status, 'superseded')

    def test_audio_change_during_scan_cannot_complete(self):
        def change_audio():
            self.audio.write_bytes(b'another recording')
            return True
        self.scan.side_effect = change_audio
        self.worker.process_scan(self.job)
        self.assertEqual(self.store.get(self.job.id).status, 'retry_scan')
        self.assertEqual(self.sidecar.read_text(), 'original line lyric')

    def test_other_writer_sidecar_is_retained_and_not_completed(self):
        self.sidecar.write_text('another writer')
        self.worker.process_scan(self.job)
        self.scan.assert_not_called()
        self.assertEqual(self.store.get(self.job.id).status, 'retry_scan')
        self.assertEqual(self.sidecar.read_text(), 'another writer')

    def test_audio_change_after_character_adaptation_rolls_back_both_writes(self):
        character_backup = self.root / 'grouped.ttml'
        character_backup.write_bytes(self.sidecar.read_bytes())
        def adapt(_):
            self.sidecar.write_text('character candidate')
            self.audio.write_bytes(b'another recording')
            return {'sidecar_path': str(self.sidecar), 'backup_path': str(character_backup),
                'candidate_sha256': hashlib.sha256(self.sidecar.read_bytes()).hexdigest(),
                'source_candidate_sha256': self.job.result['candidate_sha256']}
        self.worker.gateway.service.normalize_character_sidecar = adapt
        self.worker.process_scan(self.job)
        self.scan.assert_not_called()
        self.assertEqual(self.store.get(self.job.id).status, 'retry_scan')
        self.assertEqual(self.sidecar.read_text(), 'original line lyric')

    def test_scan_outage_keeps_valid_publication_for_retry(self):
        self.scan.return_value = False
        self.worker.process_scan(self.job)
        self.assertEqual(self.store.get(self.job.id).status, 'retry_scan')
        self.assertEqual(self.sidecar.read_text(), 'published candidate')

    def test_character_adaptation_and_scan_outage_can_retry_successfully(self):
        character_backup = self.root / 'grouped.ttml'
        def adapt(_):
            character_backup.write_bytes(self.sidecar.read_bytes())
            self.sidecar.write_text('character candidate')
            return {'sidecar_path': str(self.sidecar), 'backup_path': str(character_backup),
                'candidate_sha256': hashlib.sha256(self.sidecar.read_bytes()).hexdigest(),
                'source_candidate_sha256': self.job.result['candidate_sha256']}
        self.worker.gateway.service.normalize_character_sidecar = adapt
        self.scan.return_value = False
        self.worker.process_scan(self.job)
        self.assertEqual(self.sidecar.read_text(), 'published candidate')
        retry = self.store.get(self.job.id)
        self.store.clock = lambda: retry.next_attempt_at + 1
        claimed = self.store.claim('scan', owner='scan-retry')
        self.scan.return_value = True
        self.worker.process_scan(claimed)
        self.assertEqual(self.store.get(self.job.id).status, 'completed')
        self.assertEqual(self.scan.call_count, 2)
        self.assertEqual(self.sidecar.read_text(), 'character candidate')

    def prepare_real_character_adaptation(self):
        import word_lyrics
        self.sidecar.write_text('<tt xmlns="http://www.w3.org/ns/ttml" '
            'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:timing="Word">'
            '<body><p begin="10" end="15"><span begin="10" end="12">甲乙</span>'
            '<span begin="12" end="15">丙丁</span></p></body></tt>')
        result = {**self.job.result,
            'candidate_sha256': hashlib.sha256(self.sidecar.read_bytes()).hexdigest()}
        self.job = self.store.checkpoint_scan_result(self.job.id, self.job.lease_token, result=result)
        service = object.__new__(word_lyrics.WordLyricsService)
        service.state_dir = self.root / 'state'
        self.worker.gateway.service = service
        environment = patch.dict(os.environ, {'AMDL_HAN_CHARACTER_TIMING_ENABLED': 'true'})
        environment.start()
        self.addCleanup(environment.stop)
        self.scan.side_effect = SystemExit('simulated process crash after derived write')
        with self.assertRaises(SystemExit):
            self.worker.process_scan(self.job)
        interrupted = self.store.get(self.job.id)
        intent = interrupted.result['character_adaptation_intent']
        self.assertEqual(hashlib.sha256(self.sidecar.read_bytes()).hexdigest(), intent['candidate_sha256'])
        self.assertEqual(intent['source_candidate_sha256'], interrupted.result['candidate_sha256'])
        self.store.clock = lambda: interrupted.lease_expires_at + 1
        self.store.reclaim_expired()
        self.scan.side_effect = None
        self.scan.return_value = True
        return self.store.claim('scan', owner='scan-after-crash')

    def test_hard_crash_after_character_write_recovers_via_durable_intent(self):
        retry = self.prepare_real_character_adaptation()
        self.worker.process_scan(retry)
        completed = self.store.get(self.job.id)
        self.assertEqual(completed.status, 'completed')
        self.assertEqual(hashlib.sha256(self.sidecar.read_bytes()).hexdigest(), completed.result['candidate_sha256'])
        self.assertEqual(self.scan.call_count, 2)

    def test_hard_crash_recovery_does_not_replace_other_writer(self):
        retry = self.prepare_real_character_adaptation()
        self.sidecar.write_text('another writer')
        self.worker.process_scan(retry)
        self.assertEqual(self.sidecar.read_text(), 'another writer')
        self.assertEqual(self.store.get(self.job.id).status, 'retry_scan')
        self.assertEqual(self.scan.call_count, 1)

    def test_hard_crash_and_audio_replacement_restore_original_publication_backup(self):
        retry = self.prepare_real_character_adaptation()
        self.audio.write_bytes(b'another recording')
        self.worker.process_scan(retry)
        self.assertEqual(self.sidecar.read_text(), 'original line lyric')
        self.assertEqual(self.store.get(self.job.id).status, 'retry_scan')
        self.assertEqual(self.scan.call_count, 1)

    def test_normal_worker_reconciliation_with_changed_audio_retracts_crashed_publication(self):
        self.prepare_real_character_adaptation()
        self.audio.write_bytes(b'another recording')
        self.worker.last_reconcile_at = 0
        self.worker.reconcile_interval = 0
        self.worker.owner = 'real-cycle-after-crash'
        self.worker.lease_seconds = 900
        self.worker.health = Mock()
        self.worker.process_source = Mock()
        self.assertTrue(self.worker.one_cycle())
        self.assertEqual(self.store.get(self.job.id).status, 'superseded')
        self.assertEqual(self.sidecar.read_text(), 'original line lyric')
        self.assertIsNone(self.worker.gateway.service.existing_word_sidecar(self.audio))
        replacement = self.worker.process_source.call_args.args[0]
        self.assertEqual(replacement.status, 'leased_source')
        self.assertNotEqual(replacement.id, self.job.id)

    def test_worker_reconciliation_with_changed_audio_preserves_other_writer(self):
        self.prepare_real_character_adaptation()
        self.sidecar.write_text('another writer')
        self.audio.write_bytes(b'another recording')
        self.worker.last_reconcile_at = 0
        self.worker.reconcile_interval = 0
        self.worker.owner = 'real-cycle-after-crash'
        self.worker.lease_seconds = 900
        self.worker.health = Mock()
        self.worker.process_source = Mock()
        self.assertTrue(self.worker.one_cycle())
        self.assertEqual(self.sidecar.read_text(), 'another writer')


if __name__ == '__main__':
    unittest.main()
