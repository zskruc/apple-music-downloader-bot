from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.notifications import NotificationStore
from word_lyrics_automation.queue import QueueStore


class SourceWaitNotificationTests(unittest.TestCase):
    def test_explicit_recheck_keeps_history_and_rejects_an_active_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);media = root / "song.m4a";media.write_bytes(b"audio")
            clock = [1000.0];q = QueueStore(root / "queue.sqlite3", clock=lambda: clock[0])
            job = q.enqueue(media).job;claim = q.claim("source", owner="source")
            with self.assertRaises(RuntimeError):q.request_source_recheck(job.id, reason="repair")
            waited = q.wait_for_source(job.id, claim.lease_token, error="empty", diagnostic_payload={"kept": True})
            clock[0] += 10
            retried = q.request_source_recheck(job.id, reason="repair")
            self.assertEqual(retried.payload, waited.payload)
            self.assertEqual(retried.next_attempt_at, clock[0])
            self.assertIsNotNone(q.claim("source", owner="source"))

    def test_wait_notice_survives_restart_and_does_not_swallow_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);media = root / "song.m4a";media.write_bytes(b"audio")
            clock = [1000.0]
            q = QueueStore(root / "queue.sqlite3", clock=lambda: clock[0])
            job = q.enqueue(media, metadata={"notification": {"source": "telegram", "chat_id": "test"}}).job
            claim = q.claim("source", owner="source")
            q.wait_for_source(job.id, claim.lease_token, error="no_trusted_lyric_reference",
                diagnostic_payload={"source_observations": [{"instrumental_flag": True}]})
            n = NotificationStore(root / "queue.sqlite3", clock=lambda: clock[0])
            notice = n.claim(owner="bot")
            self.assertEqual(notice.terminal_status, "waiting_source")
            self.assertEqual(notice.next_attempt_at, clock[0] + 21600)
            self.assertTrue(notice.payload["source_observations"][0]["instrumental_flag"])
            n.delivered(job.id, notice.lease_token)
            n = NotificationStore(root / "queue.sqlite3", clock=lambda: clock[0])
            self.assertIsNone(n.claim(owner="restarted-bot"))
            clock[0] = q.get(job.id).next_attempt_at + 1
            claim = q.claim("source", owner="source")
            q.wait_for_source(job.id, claim.lease_token, error="no_trusted_lyric_reference", diagnostic_payload={})
            self.assertIsNone(n.claim(owner="bot"))
            clock[0] = q.get(job.id).next_attempt_at + 1
            claim = q.claim("source", owner="source")
            q.complete(job.id, claim.lease_token, result={"lyric_status": "not_required"}, expected_statuses={"leased_source"})
            final = n.claim(owner="bot")
            self.assertEqual(final.terminal_status, "completed")
            n.delivered(job.id, final.lease_token)
            self.assertIsNone(n.claim(owner="bot"))
            self.assertEqual(n.stats()["delivered"], 1)
            self.assertEqual(n.stats()["source_wait_delivered"], 1)

    def test_wait_notice_delivery_failure_has_real_backoff_and_no_stale_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);media = root / "song.m4a";media.write_bytes(b"audio")
            clock = [1000.0]
            q = QueueStore(root / "queue.sqlite3", clock=lambda: clock[0])
            job = q.enqueue(media, metadata={"notification": {"source": "telegram", "chat_id": "test"}}).job
            claim = q.claim("source", owner="source")
            q.wait_for_source(job.id, claim.lease_token, error="empty", diagnostic_payload={})
            n = NotificationStore(root / "queue.sqlite3", clock=lambda: clock[0])
            first = n.claim(owner="bot");n.failed(job.id, first.lease_token, "network")
            self.assertIsNone(n.claim(owner="bot"))
            clock[0] += 30
            second = n.claim(owner="bot");self.assertNotEqual(first.lease_token, second.lease_token)
            n.failed(job.id, second.lease_token, "network")
            clock[0] = q.get(job.id).next_attempt_at + 1
            claim = q.claim("source", owner="source")
            q.complete(job.id, claim.lease_token, result={}, expected_statuses={"leased_source"})
            final = n.claim(owner="bot");self.assertEqual(final.terminal_status, "completed")
            n.delivered(job.id, final.lease_token)
            self.assertIsNone(n.claim(owner="bot"))


if __name__ == "__main__":
    unittest.main()
