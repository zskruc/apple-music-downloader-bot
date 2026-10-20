from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from word_lyrics_automation.notifications import NotificationStore
from word_lyrics_automation.providers.netease import fetch_yrc
from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.resolver import Resolution
from word_lyrics_automation.source_worker import SourceWorker


class SourceRecoveryTests(unittest.TestCase):
    def test_missing_yrc_is_normal_absence(self):
        with patch('word_lyrics_automation.providers.netease._request_json', return_value={
            'code':200, 'lrc':{'lyric':'[00:00.00]纯音乐，请欣赏'}}):
            self.assertEqual(fetch_yrc('123'),'')

    def test_source_becomes_available_after_durable_wait_and_aligns_normally(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); audio=root/'song.m4a'; audio.write_bytes(b'audio')
            now=[1000.]; store=QueueStore(root/'queue.sqlite3',clock=lambda:now[0])
            job_id=store.enqueue(audio).job.id
            job=store.claim('source',owner='source')
            store.wait_for_source(job.id,job.lease_token,error='no_trusted_lyric_reference',
                diagnostic_payload={'reference_text':'','source_rejections':[{'reason':'not found'}]})
            notifications=NotificationStore(root/'queue.sqlite3',clock=lambda:now[0])
            self.assertIsNone(notifications.claim(owner='bot'))
            now[0]+=21601
            restarted=QueueStore(root/'queue.sqlite3',clock=lambda:now[0])
            worker=object.__new__(SourceWorker);worker.store=restarted
            worker.engine=SimpleNamespace(resolve=lambda **_: Resolution(
                alignment_payload={'reference_text':'真实歌词','reference_lines':[]},reason='trusted_reference_needs_acoustic_alignment'))
            worker.process_source(restarted.claim('source',owner='source-restarted'))
            saved=restarted.get(job_id)
            self.assertEqual(saved.status,'needs_alignment')
            self.assertEqual(saved.payload['source_recovery']['total_claims'],1)
            self.assertEqual(saved.payload['reference_text'],'真实歌词')
            self.assertIsNotNone(restarted.claim('alignment',owner='nas'))

    def test_legacy_missing_reference_failure_recovers_once_but_quality_failure_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); store=QueueStore(root/'queue.sqlite3'); ids=[]
            for name,error in [('missing','no_trusted_lyric_reference'),('quality','acoustic/reference mismatch')]:
                audio=root/(name+'.m4a');audio.write_bytes(name.encode())
                ids.append(store.enqueue(audio).job.id)
                job=store.claim('source',owner='test')
                store.fail(job.id,job.lease_token,stage='source',error=error,retryable=False,
                    diagnostic_payload={'reference_text':''})
            self.assertEqual(store.recover_missing_source_jobs(),1)
            self.assertEqual(store.recover_missing_source_jobs(),0)
            self.assertEqual(store.get(ids[0]).status,'waiting_source')
            self.assertEqual(store.get(ids[1]).status,'dead_letter')
            self.assertIsNotNone(store.claim('source',owner='recovered-worker'))

    def test_transport_failure_keeps_specific_reason_in_slow_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);audio=root/'song.m4a';audio.write_bytes(b'audio')
            store=QueueStore(root/'queue.sqlite3');job_id=store.enqueue(audio,max_attempts=1).job.id
            worker=object.__new__(SourceWorker);worker.store=store
            worker.engine=SimpleNamespace(resolve=lambda **_: Resolution(
                alignment_payload={'reference_text':'','source_rejections':[{'provider':'test','reason':'TimeoutError'}]},
                retryable_error='TimeoutError:provider unavailable'))
            worker.process_source(store.claim('source',owner='test'))
            saved=store.get(job_id)
            self.assertEqual(saved.status,'waiting_source')
            self.assertIn('TimeoutError',saved.last_error)
            self.assertEqual(saved.payload['source_rejections'][0]['reason'],'TimeoutError')


if __name__ == '__main__':
    unittest.main()
