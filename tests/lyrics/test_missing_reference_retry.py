from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.resolver import Resolution
from word_lyrics_automation.source_worker import SourceWorker


class MissingReferenceRetryTests(unittest.TestCase):
    def test_missing_reference_is_retried_and_diagnostics_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "audio.m4a"
            media.write_bytes(b"test audio")
            now = [1000.0]
            store = QueueStore(root / "queue.sqlite3", clock=lambda: now[0])
            job_id = store.enqueue(media).job.id
            worker = object.__new__(SourceWorker)
            worker.store = store
            worker.engine = SimpleNamespace(resolve=lambda **_: Resolution(
                alignment_payload={"reference_text": "", "source_rejections": [{"provider": "test", "reason": "empty"}]},
                reason="no_trusted_lyric_reference",
            ))
            for attempt in range(1, 5):
                job = store.claim("source", owner="test", lease_seconds=100)
                self.assertIsNotNone(job)
                worker.process_source(job)
                saved = QueueStore(root / "queue.sqlite3", clock=lambda: now[0]).get(job_id)
                self.assertEqual(saved.payload["source_rejections"][0]["reason"], "empty")
                self.assertEqual(saved.status, "retry_source" if attempt < 4 else "waiting_source")
                if attempt < 4:
                    self.assertGreaterEqual(saved.next_attempt_at - now[0], 300)
                    now[0] = saved.next_attempt_at + 1
            self.assertEqual(saved.attempts, 0)
            self.assertEqual(saved.next_attempt_at - now[0], 21600)
            self.assertEqual(saved.payload["source_recovery"]["total_claims"], 4)
            self.assertIsNone(store.claim("source", owner="test", lease_seconds=100))
            now[0] = saved.next_attempt_at + 1
            worker.store = QueueStore(root / "queue.sqlite3", clock=lambda: now[0])
            worker.process_source(worker.store.claim("source", owner="restart", lease_seconds=100))
            saved = worker.store.get(job_id)
            self.assertEqual(saved.status, "waiting_source")
            self.assertEqual(saved.payload["source_recovery"]["wait_cycles"], 2)
            self.assertEqual(saved.payload["source_recovery"]["total_claims"], 5)
            self.assertEqual(saved.next_attempt_at - now[0], 21600)
