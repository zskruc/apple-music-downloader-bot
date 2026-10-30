import json
import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.notifications import NotificationStore
from word_lyrics_automation.queue import QueueStore


class UnattendedNotificationTests(unittest.TestCase):
    def test_attention_deduplicates_across_restart_and_does_not_swallow_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);media=path/'song.flac';media.write_bytes(b'fixture')
            clock=[1000.0];queue=QueueStore(path/'q.sqlite3',clock=lambda:clock[0])
            job=queue.enqueue(media,metadata={'notification':{'source':'telegram','chat_id':'test','task_id':1}}).job
            with queue.connect() as db:
                db.execute("UPDATE jobs SET status='needs_attention',payload_json=? WHERE id=?",
                    (json.dumps({'source_state':{'category':'media_integrity','code':'flac_sample_count_mismatch','signature':'same-media'}}),job.id))
            store=NotificationStore(path/'q.sqlite3',clock=lambda:clock[0])
            first=store.claim(owner='bot');self.assertEqual(first.terminal_status,'needs_attention')
            store.delivered(job.id,first.lease_token)
            store=NotificationStore(path/'q.sqlite3',clock=lambda:clock[0])
            self.assertIsNone(store.claim(owner='restart'))
            with queue.connect() as db:db.execute("UPDATE jobs SET status='queued_source',payload_json='{}' WHERE id=?",(job.id,))
            lease=queue.claim('source',owner='test');queue.complete(job.id,lease.lease_token,result={},expected_statuses={'leased_source'})
            final=store.claim(owner='bot');self.assertEqual(final.terminal_status,'completed')
            store.delivered(job.id,final.lease_token);self.assertIsNone(store.claim(owner='bot'))

    def test_recovery_notice_is_once_per_generation_and_transport_failure_backs_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);media=path/'song.flac';media.write_bytes(b'fixture')
            clock=[1000.0];queue=QueueStore(path/'q.sqlite3',clock=lambda:clock[0])
            job=queue.enqueue(media,metadata={'notification':{'source':'telegram','chat_id':'test','task_id':1}}).job
            with queue.connect() as db:db.execute("UPDATE jobs SET status='needs_alignment',payload_json=? WHERE id=?",
                (json.dumps({'source_recovery':{'last_recovered_at':1000,'recovery_generation':1}}),job.id))
            store=NotificationStore(path/'q.sqlite3',clock=lambda:clock[0])
            first=store.claim(owner='bot');self.assertEqual(first.terminal_status,'source_recovered')
            store.failed(job.id,first.lease_token,'temporary')
            self.assertIsNone(store.claim(owner='bot'))
            clock[0]+=31;second=store.claim(owner='restart');store.delivered(job.id,second.lease_token)
            self.assertIsNone(store.claim(owner='bot'))


if __name__=='__main__':unittest.main()
