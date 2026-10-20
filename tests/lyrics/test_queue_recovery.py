from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.queue import QueueStore


class MutableClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.audio = self.root / "song.m4a"
        self.audio.write_bytes(b"audio-content" * 2048)
        self.clock = MutableClock()
        self.store = QueueStore(self.root / "queue.sqlite3", clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_duplicate_enqueue_is_idempotent(self) -> None:
        first = self.store.enqueue(self.audio, metadata={"title": "Song"})
        second = self.store.enqueue(self.audio, metadata={"title": "Song"})
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.job.id, second.job.id)
        self.assertEqual(self.store.stats()["counts"], {"queued_source": 1})

    def test_forced_reenqueue_keeps_the_real_media_fingerprint(self) -> None:
        first = self.store.enqueue(self.audio).job
        forced = self.store.enqueue(self.audio, force=True).job

        self.assertNotEqual(first.id, forced.id)
        self.assertNotEqual(first.fingerprint, forced.fingerprint)
        self.assertEqual(first.fingerprint_data, forced.fingerprint_data)
        self.assertNotIn("forced_nonce", forced.fingerprint_data)

    def test_expired_lease_is_reclaimed_with_a_new_token(self) -> None:
        job = self.store.enqueue(self.audio).job
        first = self.store.claim("source", owner="worker-a", lease_seconds=30)
        self.assertIsNotNone(first)
        self.clock.value += 31
        second = self.store.claim("source", owner="worker-b", lease_seconds=30)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(second.id, job.id)
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertEqual(second.attempts, 2)
        self.assertIn("lease_reclaimed", [event["event"] for event in self.store.events(job.id)])

    def test_alignment_upload_requires_current_lease(self) -> None:
        job = self.store.enqueue(self.audio).job
        source = self.store.claim("source", owner="source")
        assert source is not None
        self.store.defer_to_alignment(
            source.id,
            source.lease_token,
            payload={"reference_text": "hello"},
            reason="source_miss",
        )
        alignment = self.store.claim("alignment", owner="mac")
        assert alignment is not None
        with self.assertRaises(RuntimeError):
            self.store.submit_candidate(
                alignment.id,
                "wrong-token",
                candidate_path=self.root / "candidate.ttml",
                report={},
            )
        candidate = self.root / "candidate.ttml"
        candidate.write_text("<tt/>", encoding="utf-8")
        submitted = self.store.submit_candidate(
            alignment.id,
            alignment.lease_token,
            candidate_path=candidate,
            report={"mode": "reference_aligned"},
        )
        self.assertEqual(submitted.status, "candidate_pending")
        self.assertEqual(submitted.payload["reference_text"], "hello")
        self.assertEqual(submitted.payload["candidate_report"]["mode"], "reference_aligned")

    def test_retry_backoff_and_dead_letter(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=2).job
        first = self.store.claim("source", owner="source")
        assert first is not None
        retry = self.store.fail(
            first.id,
            first.lease_token,
            stage="source",
            error="timeout",
            base_delay_seconds=10,
        )
        self.assertEqual(retry.status, "retry_source")
        self.assertEqual(retry.next_attempt_at, self.clock.value + 10)
        self.clock.value += 10
        second = self.store.claim("source", owner="source")
        assert second is not None
        dead = self.store.fail(
            second.id,
            second.lease_token,
            stage="source",
            error="timeout-again",
        )
        self.assertEqual(dead.status, "dead_letter")
        self.assertEqual(self.store.stats()["quick_check"], "ok")
        self.assertEqual(dead.id, queued.id)

    def test_scan_checkpoint_persists_result_without_changing_lease_or_budget(self) -> None:
        self.store.enqueue(self.audio)
        source = self.store.claim("source", owner="source")
        self.store.transition_to_scan(source.id, source.lease_token,
            result={"candidate_sha256": "original"}, expected_statuses={"leased_source"})
        scan = self.store.claim("scan", owner="source", lease_seconds=30)
        result = {**scan.result, "character_adaptation_intent": {
            "candidate_sha256": "derived", "backup_path": "grouped.ttml"}}
        saved = self.store.checkpoint_scan_result(scan.id, scan.lease_token, result=result)
        self.assertEqual(saved.result, result)
        self.assertEqual(saved.status, scan.status)
        self.assertEqual(saved.attempts, scan.attempts)
        self.assertEqual(saved.next_attempt_at, scan.next_attempt_at)
        self.assertEqual(saved.lease_owner, scan.lease_owner)
        self.assertEqual(saved.lease_token, scan.lease_token)
        self.assertEqual(saved.lease_expires_at, scan.lease_expires_at)
        restarted = QueueStore(self.root / "queue.sqlite3", clock=self.clock)
        self.assertEqual(restarted.get(scan.id).result, result)
        checkpoint = self.store.events(scan.id)[-1]
        self.assertEqual(checkpoint["event"], "scan_publication_checkpoint")
        self.assertEqual(checkpoint["detail"], result)

    def test_scan_checkpoint_refuses_wrong_stage_expired_and_reclaimed_tokens(self) -> None:
        self.store.enqueue(self.audio)
        source = self.store.claim("source", owner="source")
        with self.assertRaisesRegex(RuntimeError, "unexpected status"):
            self.store.checkpoint_scan_result(source.id, source.lease_token, result={"wrong": True})
        self.store.transition_to_scan(source.id, source.lease_token,
            result={"candidate_sha256": "original"}, expected_statuses={"leased_source"})
        scan = self.store.claim("scan", owner="source", lease_seconds=30)
        self.clock.value += 31
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.store.checkpoint_scan_result(scan.id, scan.lease_token, result={"wrong": True})
        reclaimed = self.store.claim("scan", owner="restart", lease_seconds=30)
        for stale_token in (scan.lease_token, "wrong-token"):
            with self.assertRaisesRegex(RuntimeError, "no longer owned"):
                self.store.checkpoint_scan_result(reclaimed.id, stale_token, result={"wrong": True})
        self.assertEqual(self.store.get(scan.id).result, {"candidate_sha256": "original"})
        self.assertNotIn("scan_publication_checkpoint",
            [event["event"] for event in self.store.events(scan.id)])

    def prepare_interrupted_scan_publication(self, *, layered=True, suffix=".ttml", backup=True):
        sidecar = self.audio.with_suffix(suffix)
        sidecar.write_text("published grouped lyric")
        prior = self.root / "prior.lyric"
        prior.write_text("original line lyric")
        publication = {"sidecar_path": str(sidecar),
            "backup_path": str(prior) if backup else "",
            "candidate_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()}
        if layered:
            grouped = self.root / "grouped.lyric"
            grouped.write_bytes(sidecar.read_bytes())
            derived = "derived character lyric"
            publication["character_adaptation_intent"] = {
                "sidecar_path": str(sidecar), "backup_path": str(grouped),
                "source_candidate_sha256": publication["candidate_sha256"],
                "candidate_sha256": hashlib.sha256(derived.encode()).hexdigest()}
        job = self.store.enqueue(self.audio).job
        source = self.store.claim("source", owner="source")
        self.store.transition_to_scan(source.id, source.lease_token,
            result=publication, expected_statuses={"leased_source"})
        scan = self.store.claim("scan", owner="crashed")
        if layered:
            sidecar.write_text(derived)
        return scan, sidecar, prior

    def test_changed_audio_supersession_rolls_back_interrupted_character_and_publication_layers(self):
        scan, sidecar, _ = self.prepare_interrupted_scan_publication()
        self.audio.write_bytes(b"a replacement recording")
        replacement = self.store.enqueue(self.audio).job
        self.assertEqual(sidecar.read_text(), "original line lyric")
        self.assertEqual(self.store.get(scan.id).status, "superseded")
        self.assertEqual(replacement.status, "queued_source")
        self.assertNotEqual(replacement.fingerprint_data, scan.fingerprint_data)

    def test_changed_audio_supersession_removes_unbacked_publication(self):
        scan, sidecar, _ = self.prepare_interrupted_scan_publication(layered=False, suffix=".lrc", backup=False)
        self.audio.write_bytes(b"a replacement recording")
        replacement = self.store.enqueue(self.audio).job
        self.assertFalse(sidecar.exists())
        self.assertEqual(self.store.get(scan.id).status, "superseded")
        self.assertEqual(replacement.status, "queued_source")

    def test_changed_audio_supersession_preserves_other_writer_sidecar(self):
        scan, sidecar, _ = self.prepare_interrupted_scan_publication()
        sidecar.write_text("another writer")
        self.audio.write_bytes(b"a replacement recording")
        self.store.enqueue(self.audio)
        self.assertEqual(sidecar.read_text(), "another writer")
        self.assertEqual(self.store.get(scan.id).status, "superseded")

    def test_supersession_rollback_cannot_target_an_unrelated_path(self):
        scan, sidecar, _ = self.prepare_interrupted_scan_publication(layered=False)
        unrelated = self.root / "unrelated.ttml"
        unrelated.write_bytes(sidecar.read_bytes())
        self.store.checkpoint_scan_result(scan.id, scan.lease_token,
            result={**scan.result, "sidecar_path": str(unrelated)})
        self.audio.write_bytes(b"a replacement recording")
        self.store.enqueue(self.audio)
        self.assertEqual(unrelated.read_text(), "published grouped lyric")

    def test_identical_audio_reenqueue_retains_current_scan_publication(self):
        scan, sidecar, _ = self.prepare_interrupted_scan_publication()
        duplicate = self.store.enqueue(self.audio)
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.job.id, scan.id)
        self.assertEqual(sidecar.read_text(), "derived character lyric")
        self.assertEqual(duplicate.job.status, "leased_scan")

    def test_success_at_each_stage_limit_keeps_later_stages_runnable(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=1).job
        source = self.store.claim("source", owner="source")
        self.store.defer_to_alignment(source.id, source.lease_token,
            payload={"reference_text": "trusted text"}, reason="reference")
        aligned = self.store.claim("alignment", owner="nas")
        self.assertIsNotNone(aligned)
        candidate = self.root / "candidate.ttml"
        candidate.write_text("candidate")
        self.store.submit_candidate(aligned.id, aligned.lease_token,
            candidate_path=candidate, report={})
        validation = self.store.claim("validation", owner="source")
        self.assertIsNotNone(validation)
        self.store.transition_to_scan(validation.id, validation.lease_token,
            result={"sidecar_path": str(candidate)}, expected_statuses={"leased_validation"})
        scan = self.store.claim("scan", owner="source")
        self.assertIsNotNone(scan)
        completed = self.store.complete(scan.id, scan.lease_token, result=scan.result,
            expected_statuses={"leased_scan"})
        self.assertEqual(completed.id, queued.id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.attempts, 4)

    def test_validation_retry_uses_its_own_budget_and_backoff(self) -> None:
        self.store.enqueue(self.audio, max_attempts=2)
        source = self.store.claim("source", owner="source")
        self.store.defer_to_alignment(source.id, source.lease_token,
            payload={"reference_text": "trusted text"}, reason="reference")
        aligned = self.store.claim("alignment", owner="nas")
        self.store.submit_candidate(aligned.id, aligned.lease_token,
            candidate_path=self.root / "candidate.ttml", report={})
        first = self.store.claim("validation", owner="source")
        retry = self.store.fail(first.id, first.lease_token, stage="validation",
            error="temporary I/O failure", base_delay_seconds=10)
        self.assertEqual(retry.status, "retry_validation")
        self.assertEqual(retry.next_attempt_at, self.clock.value + 10)
        self.assertEqual(retry.attempts, 3)
        self.clock.value += 10
        second = self.store.claim("validation", owner="source")
        self.store.transition_to_scan(second.id, second.lease_token,
            result={}, expected_statuses={"leased_validation"})
        self.assertIsNotNone(self.store.claim("scan", owner="source"))

    def test_final_source_lease_expiry_keeps_durable_rechecks(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=1).job
        self.store.claim("source", owner="crashed", lease_seconds=30)
        self.clock.value += 31
        self.assertEqual(self.store.reclaim_expired(), 1)
        waiting = self.store.get(queued.id)
        self.assertEqual(waiting.status, "waiting_source")
        self.assertEqual(waiting.attempts, 0)
        self.assertEqual(waiting.payload["source_recovery"]["total_claims"], 1)
        self.assertEqual(waiting.last_error, "lease_expired_retry_budget_exhausted")
        self.assertIsNone(self.store.claim("source", owner="restart"))
        self.clock.value = waiting.next_attempt_at
        restarted = QueueStore(self.root / "queue.sqlite3", clock=self.clock)
        recheck = restarted.claim("source", owner="restart", lease_seconds=30)
        self.assertIsNotNone(recheck)
        restarted.wait_for_source(recheck.id, recheck.lease_token, error="still missing",
            diagnostic_payload=recheck.payload)
        self.assertEqual(restarted.get(queued.id).payload["source_recovery"]["total_claims"], 2)

    def test_final_alignment_validation_and_scan_lease_expiry_is_terminal(self) -> None:
        for stage in ("alignment", "validation", "scan"):
            with self.subTest(stage=stage):
                audio = self.root / (stage + ".m4a")
                audio.write_bytes(b"audio")
                queued = self.store.enqueue(audio, max_attempts=1).job
                source = self.store.claim("source", owner="source")
                self.store.defer_to_alignment(source.id, source.lease_token,
                    payload={"reference_text": "trusted text"}, reason="reference")
                job = self.store.claim("alignment", owner="nas", lease_seconds=30)
                if stage != "alignment":
                    self.store.submit_candidate(job.id, job.lease_token,
                        candidate_path=self.root / "candidate.ttml", report={"retained": True})
                    job = self.store.claim("validation", owner="source", lease_seconds=30)
                if stage == "scan":
                    self.store.transition_to_scan(job.id, job.lease_token,
                        result={"sidecar_path": "published.ttml"}, expected_statuses={"leased_validation"})
                    job = self.store.claim("scan", owner="source", lease_seconds=30)
                self.clock.value += 31
                self.assertEqual(self.store.reclaim_expired(), 1)
                saved = self.store.get(queued.id)
                self.assertEqual(saved.status, "dead_letter")
                self.assertEqual(saved.last_error, "lease_expired_retry_budget_exhausted")
                self.assertIsNone(self.store.claim(stage, owner="restart"))
                self.assertEqual(self.store.stats()["active"], 0)
                if stage == "validation":
                    self.assertEqual(saved.payload["candidate_report"], {"retained": True})
                if stage == "scan":
                    self.assertEqual(saved.result["sidecar_path"], "published.ttml")

    def test_legacy_scan_stranded_by_total_budget_recovers_without_resetting_history(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=2).job
        source = self.store.claim("source", owner="source")
        self.store.fail(source.id, source.lease_token, stage="source", error="temporary", base_delay_seconds=0)
        source = self.store.claim("source", owner="source")
        self.store.transition_to_scan(source.id, source.lease_token,
            result={"sidecar_path": "published.ttml"}, expected_statuses={"leased_source"})
        self.assertEqual(self.store.get(queued.id).attempts, 2)
        restarted = QueueStore(self.root / "queue.sqlite3", clock=self.clock)
        scan = restarted.claim("scan", owner="restart")
        self.assertIsNotNone(scan)
        self.assertEqual(scan.attempts, 3)
        self.assertEqual(scan.result["sidecar_path"], "published.ttml")

    def test_mac_consent_preserves_the_granted_retry_budget(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=2).job
        source = self.store.claim("source", owner="source")
        self.store.defer_to_alignment(source.id, source.lease_token,
            payload={"reference_text": "trusted text"}, reason="reference")
        nas = self.store.claim("alignment", owner="nas")
        self.store.fail(nas.id, nas.lease_token, stage="alignment", error="nas quality limit",
            retryable=False, offer_mac_on_terminal=True)
        self.store.set_mac_consent(queued.id, approved=True, actor="test")
        for attempt in range(3):
            mac = self.store.claim("mac_alignment", owner="mac")
            self.assertIsNotNone(mac)
            failed = self.store.fail(mac.id, mac.lease_token, stage="mac_alignment",
                error="temporary", base_delay_seconds=0)
            self.assertEqual(failed.status, "retry_mac_alignment" if attempt < 2 else "dead_letter")
        self.assertIsNone(self.store.claim("mac_alignment", owner="mac"))

    def test_reclaimed_final_mac_lease_is_terminal(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=2).job
        source = self.store.claim("source", owner="source")
        self.store.defer_to_alignment(source.id, source.lease_token,
            payload={"reference_text": "trusted text"}, reason="reference")
        nas = self.store.claim("alignment", owner="nas")
        self.store.fail(nas.id, nas.lease_token, stage="alignment", error="nas quality limit",
            retryable=False, offer_mac_on_terminal=True)
        self.store.set_mac_consent(queued.id, approved=True, actor="test")
        for _ in range(2):
            mac = self.store.claim("mac_alignment", owner="mac")
            self.store.fail(mac.id, mac.lease_token, stage="mac_alignment", error="temporary", base_delay_seconds=0)
        self.store.claim("mac_alignment", owner="crashed", lease_seconds=30)
        self.clock.value += 31
        self.store.reclaim_expired()
        saved = self.store.get(queued.id)
        self.assertEqual(saved.status, "dead_letter")
        self.assertEqual(saved.last_error, "lease_expired_retry_budget_exhausted")
        self.assertEqual(saved.attempts, 5)

    def test_mac_cannot_claim_until_nas_failure_is_explicitly_approved(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=2).job
        source = self.store.claim("source", owner="source")
        assert source is not None
        self.store.defer_to_alignment(
            source.id,
            source.lease_token,
            payload={"reference_text": "trusted text"},
            reason="trusted_reference_needs_alignment",
        )
        nas = self.store.claim("alignment", owner="nas")
        assert nas is not None
        awaiting = self.store.fail(
            nas.id,
            nas.lease_token,
            stage="alignment",
            error="nas_cpu_alignment_failed",
            retryable=False,
            offer_mac_on_terminal=True,
        )
        self.assertEqual(awaiting.status, "awaiting_mac_consent")
        self.assertIsNone(self.store.claim("mac_alignment", owner="mac"))

        approved = self.store.set_mac_consent(
            queued.id,
            approved=True,
            actor="telegram:-1001",
        )
        self.assertEqual(approved.status, "needs_mac_alignment")
        mac = self.store.claim("mac_alignment", owner="mac")
        self.assertIsNotNone(mac)
        assert mac is not None
        self.assertEqual(mac.status, "leased_mac_alignment")

    def test_declining_mac_fallback_is_terminal_and_idempotent(self) -> None:
        queued = self.store.enqueue(self.audio, max_attempts=3).job
        source = self.store.claim("source", owner="source")
        assert source is not None
        self.store.defer_to_alignment(
            source.id,
            source.lease_token,
            payload={"reference_text": "trusted text"},
            reason="trusted_reference_needs_alignment",
        )
        nas = self.store.claim("alignment", owner="nas")
        assert nas is not None
        self.store.fail(
            nas.id,
            nas.lease_token,
            stage="alignment",
            error="nas_failed",
            retryable=False,
            offer_mac_on_terminal=True,
        )
        declined = self.store.set_mac_consent(
            queued.id,
            approved=False,
            actor="telegram:-1001",
        )
        self.assertEqual(declined.status, "dead_letter")
        self.assertEqual(declined.last_error, "mac_fallback_declined_by_user")
        repeated = self.store.set_mac_consent(
            queued.id,
            approved=False,
            actor="telegram:-1001",
        )
        self.assertEqual(repeated.status, "dead_letter")

    def test_in_place_tag_rewrite_supersedes_stale_fingerprint(self) -> None:
        original = self.store.enqueue(
            self.audio,
            source_url="https://music.example/song",
            metadata={"title": "Song", "artist": "Artist"},
        ).job
        source = self.store.claim("source", owner="source")
        assert source is not None
        self.store.defer_to_alignment(
            source.id,
            source.lease_token,
            payload={"reference_text": ""},
            reason="source_miss",
        )
        before = self.audio.stat()
        self.audio.write_bytes(b"video-content" * 2048)
        os.utime(
            self.audio,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )

        refreshed = self.store.refresh_changed_active_jobs()

        self.assertEqual(len(refreshed), 1)
        replacement = refreshed[0].job
        self.assertNotEqual(replacement.id, original.id)
        self.assertEqual(replacement.status, "queued_source")
        self.assertEqual(replacement.source_url, "https://music.example/song")
        self.assertEqual(replacement.metadata["title"], "Song")
        stale = self.store.get(original.id)
        assert stale is not None
        self.assertEqual(stale.status, "superseded")
        self.assertEqual(self.store.stats()["counts"], {"queued_source": 1, "superseded": 1})


if __name__ == "__main__":
    unittest.main()
