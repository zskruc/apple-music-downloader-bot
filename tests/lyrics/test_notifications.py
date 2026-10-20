from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.notifications import NotificationStore
from word_lyrics_automation.queue import QueueStore


class MutableClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audio = self.root / "song.m4a"
        self.audio.write_bytes(b"audio" * 4096)
        self.clock = MutableClock()
        self.queue = QueueStore(self.root / "queue.sqlite3", clock=self.clock)
        self.notifications = NotificationStore(
            self.root / "queue.sqlite3", clock=self.clock
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def completed_job(self) -> int:
        queued = self.queue.enqueue(
            self.audio,
            metadata={
                "title": "Song",
                "artist": "Artist",
                "notification": {
                    "source": "telegram",
                    "chat_id": "-1001",
                    "message_id": "9",
                },
            },
        ).job
        source = self.queue.claim("source", owner="resolver")
        assert source is not None
        self.queue.complete(
            source.id,
            source.lease_token,
            result={"provider": "amll", "metrics": {"timed_words": 42}},
            expected_statuses={"leased_source"},
        )
        return queued.id

    def test_successful_delivery_is_not_claimed_twice(self) -> None:
        job_id = self.completed_job()
        notification = self.notifications.claim(owner="bot")
        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertEqual(notification.job_id, job_id)
        self.assertEqual(notification.context["chat_id"], "-1001")
        self.notifications.delivered(job_id, notification.lease_token)
        self.assertIsNone(self.notifications.claim(owner="bot"))
        self.assertEqual(self.notifications.stats()["delivered"], 1)

    def test_failed_delivery_retries_after_backoff(self) -> None:
        job_id = self.completed_job()
        first = self.notifications.claim(owner="bot")
        assert first is not None
        self.notifications.failed(job_id, first.lease_token, "network")
        self.assertIsNone(self.notifications.claim(owner="bot"))
        self.clock.value += 30
        second = self.notifications.claim(owner="bot")
        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotEqual(first.lease_token, second.lease_token)

    def test_non_telegram_job_is_marked_skipped(self) -> None:
        queued = self.queue.enqueue(self.audio, metadata={"title": "Song"}).job
        source = self.queue.claim("source", owner="resolver")
        assert source is not None
        self.queue.complete(
            source.id,
            source.lease_token,
            result={},
            expected_statuses={"leased_source"},
        )
        self.assertIsNone(self.notifications.claim(owner="bot"))
        self.assertEqual(self.notifications.stats()["skipped"], 1)
        self.assertEqual(queued.id, source.id)

    def test_declined_mac_consent_prompt_is_not_duplicated(self) -> None:
        queued = self.queue.enqueue(
            self.audio,
            metadata={
                "title": "Song",
                "notification": {"source": "telegram", "chat_id": "-1001"},
            },
            max_attempts=2,
        ).job
        source = self.queue.claim("source", owner="source")
        assert source is not None
        self.queue.defer_to_alignment(
            source.id,
            source.lease_token,
            payload={"reference_text": "trusted"},
            reason="needs_alignment",
        )
        nas = self.queue.claim("alignment", owner="nas")
        assert nas is not None
        self.queue.fail(
            nas.id,
            nas.lease_token,
            stage="alignment",
            error="nas_failed",
            retryable=False,
            offer_mac_on_terminal=True,
        )
        prompt = self.notifications.claim(owner="bot")
        assert prompt is not None
        self.assertEqual(prompt.terminal_status, "awaiting_mac_consent")
        self.notifications.delivered(prompt.job_id, prompt.lease_token)
        self.assertIsNone(self.notifications.claim(owner="bot"))

        self.queue.set_mac_consent(
            queued.id,
            approved=False,
            actor="telegram:-1001",
        )
        final = self.notifications.claim(owner="bot")
        self.assertIsNotNone(final)
        self.assertEqual(final.terminal_status, "dead_letter")
        self.assertEqual(final.notification_kind, "terminal")
        self.notifications.delivered(final.job_id, final.lease_token)
        self.assertIsNone(self.notifications.claim(owner="bot"))


if __name__ == "__main__":
    unittest.main()
