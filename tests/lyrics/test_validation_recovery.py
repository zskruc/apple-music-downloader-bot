from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from word_lyrics_automation.nas_alignment_worker import NasAlignmentWorker
from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.resolver import CandidateRejected
from word_lyrics_automation.source_worker import SourceWorker


class ValidationRecoveryTests(unittest.TestCase):
    def test_candidate_cleanup_failure_after_scan_handoff_keeps_committed_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'song.m4a'
            audio.write_bytes(b'audio')
            store = QueueStore(root / 'queue.sqlite3')
            store.enqueue(audio)
            source = store.claim('source', owner='test')
            store.defer_to_alignment(source.id, source.lease_token,
                payload={'reference_text': 'text'}, reason='test')
            aligned = store.claim('alignment', owner='test')
            inbox = root / 'inbox'
            inbox.mkdir()
            candidate = inbox / 'candidate.ttml'
            candidate.write_text('retained candidate')
            store.submit_candidate(aligned.id, aligned.lease_token, candidate_path=candidate,
                report={'source_fingerprint': aligned.fingerprint_data})
            worker = object.__new__(SourceWorker)
            worker.store, worker.inbox_dir = store, inbox
            publication = {'sidecar_path': str(audio.with_suffix('.ttml')), 'candidate_sha256': 'digest'}
            worker.engine = SimpleNamespace(validate_mac_candidate=Mock(
                return_value=SimpleNamespace(as_dict=lambda: publication)))
            job = store.claim('validation', owner='test')
            real_unlink = Path.unlink
            def cannot_remove(path, *args, **kwargs):
                if path == candidate:
                    raise PermissionError('inbox file is owned by another uploader')
                return real_unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', autospec=True, side_effect=cannot_remove), \
                    self.assertLogs('word_lyrics_automation.source_worker', level='WARNING') as logs:
                worker.process_validation(job)
            self.assertIn('PermissionError', logs.output[0])
            self.assertEqual(store.get(job.id).status, 'scan_pending')
            self.assertTrue(candidate.is_file())
            self.assertIsNotNone(store.claim('scan', owner='scan-after-cleanup-failure'))

    def test_terminal_validation_keeps_candidate_report_and_rejection_metrics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'song.m4a'
            audio.write_bytes(b'audio')
            store = QueueStore(root / 'queue.sqlite3')
            store.enqueue(audio)
            source = store.claim('source', owner='test')
            store.defer_to_alignment(source.id, source.lease_token, payload={'reference_text': 'text'}, reason='test')
            aligned = store.claim('alignment', owner='test')
            inbox = root / 'inbox'
            inbox.mkdir()
            candidate = inbox / 'candidate.ttml'
            candidate.write_text('candidate retained for diagnosis')
            store.submit_candidate(aligned.id, aligned.lease_token, candidate_path=candidate, report={'source_fingerprint': aligned.fingerprint_data})
            worker = object.__new__(SourceWorker)
            worker.store, worker.inbox_dir = store, inbox
            error = CandidateRejected('existing_lyrics_mismatch', metrics={'median_absolute_delta_seconds': 1.1})
            worker.engine = SimpleNamespace(validate_mac_candidate=Mock(side_effect=error))
            job = store.claim('validation', owner='test')
            worker.process_validation(job)
            saved = store.get(job.id)
            self.assertEqual(saved.status, 'dead_letter')
            self.assertTrue(candidate.is_file())
            self.assertEqual(saved.payload['validation_failure']['metrics'], error.metrics)
            self.assertEqual(saved.payload['candidate_report']['source_fingerprint'], aligned.fingerprint_data)

    def test_nas_only_policy_never_offers_mac_after_terminal_alignment_failure(self):
        for fallback, expected in [('false', 'dead_letter'), ('true', 'awaiting_mac_consent')]:
            with self.subTest(fallback=fallback), TemporaryDirectory() as directory:
                with patch.dict('os.environ', {'AMDL_WORD_LYRICS_STATE_DIR': directory, 'WORD_LYRICS_MAC_FALLBACK_ENABLED': fallback}):
                    worker = NasAlignmentWorker()
                audio = Path(directory) / 'audio.m4a'
                audio.write_bytes(b'audio')
                worker.store.enqueue(audio)
                source = worker.store.claim('source', owner='test')
                worker.store.defer_to_alignment(source.id, source.lease_token, payload={'track': {'duration': 60}}, reason='test')
                job = worker.store.claim('alignment', owner='test')
                with patch('word_lyrics_automation.nas_alignment_worker.run_alignment', side_effect=RuntimeError('acoustic/reference mismatch')):
                    worker.process(job)
                self.assertEqual(worker.store.get(job.id).status, expected)
