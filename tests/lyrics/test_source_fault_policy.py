from __future__ import annotations

import json
import hashlib
import socket
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from word_lyrics_automation.faults import (LyricFault, classify_exception, retry_delay,
                                          source_context_key)
from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.resolver import (Candidate, CandidateRejected, Resolution,
                                            SourceResolutionEngine)
from word_lyrics_automation.source_worker import SourceWorker


class SourceFaultPolicyTests(unittest.TestCase):
    def test_http_statuses_and_invalid_json_are_not_all_network_failures(self):
        for status, category, retry in [(401, "authentication", False), (403, "permission", False),
                                         (404, "no_source", False), (429, "transport", True),
                                         (500, "transport", True), (503, "transport", True),
                                         (400, "response_semantics", False)]:
            with self.subTest(status=status):
                fault = classify_exception(urllib.error.HTTPError(
                    "https://provider.invalid/?token=private", status, "private-body", {}, None))
                self.assertEqual((fault.category, fault.retryable), (category, retry))
                self.assertNotIn("private", json.dumps(fault.as_dict()))
        fault = classify_exception(json.JSONDecodeError("private response", "private", 0))
        self.assertEqual((fault.category, fault.retryable), ("parse", False))
        self.assertEqual(classify_exception(RuntimeError("provider unavailable")).category, "internal")

    def test_only_confirmed_transport_evidence_retries(self):
        for error in [socket.gaierror(-2, "name lookup failed"), TimeoutError(),
                      ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING"), ConnectionResetError()]:
            with self.subTest(error=type(error).__name__):
                self.assertTrue(classify_exception(error).retryable)
        self.assertFalse(classify_exception(ssl.SSLCertVerificationError(1, "bad cert")).retryable)
        semantic = classify_exception(RuntimeError("NetEase search returned unrelated results"))
        self.assertEqual(semantic.category, "response_semantics")
        self.assertFalse(semantic.retryable)
        wrapped = classify_exception(RuntimeError("NetEase search endpoints unavailable: HTTPError:HTTP Error 503"))
        self.assertEqual((wrapped.http_status, wrapped.retryable), (503, True))

    def test_retry_jitter_is_bounded_and_retry_after_is_honored(self):
        self.assertEqual(retry_delay(1, random_value=0), 300)
        self.assertEqual(retry_delay(1, random_value=1), 360)
        self.assertEqual(retry_delay(30, random_value=1), 3600)
        self.assertEqual(retry_delay(1, retry_after=900, random_value=0), 900)

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        media = root / "song.flac"
        media.write_bytes(b"same immutable media")
        now = [1000.0]
        store = QueueStore(root / "queue.sqlite3", clock=lambda: now[0])
        job = store.enqueue(media, metadata={"title": "Fixture", "artist": "Artist", "album": "Album",
            "notification": {"source": "telegram", "chat_id": "test", "task_id": "42"}}).job
        worker = object.__new__(SourceWorker)
        worker.store = store
        worker.gateway = SimpleNamespace()
        return root, media, now, store, job, worker

    def test_disconnection_recovers_original_job_after_restart_without_erasing_evidence(self):
        root, media, now, store, job, worker = self.fixture()
        worker.engine = SimpleNamespace(resolve=lambda **_: (_ for _ in ()).throw(socket.gaierror(-2, "offline")))
        worker.process_source(store.claim("source", owner="first-process"))
        failed = store.get(job.id)
        signature = failed.payload["source_state"]["signature"]
        self.assertEqual(failed.status, "retry_source")
        self.assertGreaterEqual(failed.next_attempt_at, now[0] + 300)
        now[0] = failed.next_attempt_at + 1
        worker.store = QueueStore(root / "queue.sqlite3", clock=lambda: now[0])
        worker.engine = SimpleNamespace(resolve=lambda **_: Resolution(
            alignment_payload={"reference_text": "invented fixture verse", "reference_lines": []},
            reason="trusted_reference_needs_acoustic_alignment"))
        worker.process_source(worker.store.claim("source", owner="restarted-process"))
        recovered = worker.store.get(job.id)
        self.assertEqual(recovered.status, "needs_alignment")
        self.assertEqual(recovered.payload["source_recovery"]["recovered_from"]["signature"], signature)
        self.assertEqual(recovered.payload["source_recovery"]["last_recovered_at"], now[0])
        self.assertEqual(len(recovered.payload["source_fault_history"]), 1)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_subscriptions").fetchone()[0], 1)

    def test_auth_and_bad_document_enter_attention_without_timed_retries(self):
        for error in [urllib.error.HTTPError("", 401, "", {}, None),
                      json.JSONDecodeError("bad response", "x", 0)]:
            root, media, now, store, job, worker = self.fixture()
            worker.engine = SimpleNamespace(resolve=lambda **_: (_ for _ in ()).throw(error))
            worker.process_source(store.claim("source", owner="source"))
            held = store.get(job.id)
            self.assertEqual(held.status, "needs_attention")
            self.assertEqual(held.next_attempt_at, 0)
            now[0] += 100 * 86400
            self.assertIsNone(store.claim("source", owner="later"))

    def test_temporary_outage_has_a_durable_attempt_limit(self):
        root, media, now, store, job, worker = self.fixture()
        context = source_context_key(media, job.metadata)
        fault = classify_exception(TimeoutError()).as_dict()
        for number in range(2):
            claim = store.claim("source", owner="bounded")
            saved = store.source_fault(claim.id, claim.lease_token, faults=[fault],
                diagnostic_payload={}, context_key=context, max_temporary_attempts=2)
            now[0] = saved.next_attempt_at + 1 if saved.next_attempt_at else now[0] + 1
        self.assertEqual(saved.status, "needs_attention")
        self.assertTrue(saved.payload["source_state"]["budget_exhausted"])
        self.assertEqual(saved.payload["source_state"]["failure_count"], 2)
        restarted = QueueStore(root / "queue.sqlite3", clock=lambda: now[0])
        self.assertIsNone(restarted.claim("source", owner="restart"))

    def test_missing_source_slow_rechecks_are_bounded(self):
        root, media, now, store, job, worker = self.fixture()
        fault = LyricFault("no_source", "no_trusted_lyric_reference", action="recheck").as_dict()
        context = source_context_key(media, job.metadata)
        for number in range(10):
            claim = store.claim("source", owner="missing")
            if claim is None:
                break
            saved = store.source_fault(claim.id, claim.lease_token, faults=[fault],
                diagnostic_payload={}, context_key=context, max_slow_rechecks=2)
            if saved.status == "needs_attention":
                break
            now[0] = saved.next_attempt_at + 1
        self.assertEqual(saved.status, "needs_attention")
        self.assertEqual(saved.payload["source_recovery"]["wait_cycles"], 3)
        self.assertLessEqual(saved.payload["source_state"]["failure_count"], 6)
        self.assertIsNone(store.claim("source", owner="must-not-spin"))

    def test_reference_change_resumes_identity_hold_but_does_not_resume_bad_media(self):
        for category in ("identity", "media_integrity"):
            root, media, now, store, job, worker = self.fixture()
            context = source_context_key(media, job.metadata)
            claim = store.claim("source", owner="hold")
            store.source_fault(claim.id, claim.lease_token,
                faults=[LyricFault(category, "fixture_input_required").as_dict()],
                diagnostic_payload={}, context_key=context)
            self.assertEqual(store.reconcile_source_holds(), [])
            media.with_suffix(".lrc").write_text("[00:01]new independently supplied fixture verse")
            resumed = store.reconcile_source_holds()
            self.assertEqual(resumed, [job.id] if category == "identity" else [])
            if category == "identity":
                self.assertEqual(store.get(job.id).payload["source_state"]["generation"], 1)
            else:
                with self.assertRaises(RuntimeError):
                    store.resume_source_after_change(job.id, context_key="new-policy", reason="policy")
                self.assertEqual(store.get(job.id).status, "needs_attention")

    def engine(self, *, exception=None, candidate_rejection=False):
        track = SimpleNamespace(path=Path("/fixture/song.flac"), title="Fixture", artist="Artist",
                                album="Album", duration=60, song_id="", isrc="")
        gateway = SimpleNamespace(inspect=lambda *_: track, existing_word=lambda *_: None,
            references=lambda *_: [], alignment_payload=lambda *_: {"reference_text": "", "reference_lines": []})
        def search(*args, **kwargs):
            if exception is not None:
                raise exception
            return []
        engine = SourceResolutionEngine(gateway,
            amll_client=SimpleNamespace(get=lambda **_: None, search=lambda **_: []),
            netease_search_function=search, kugou_search_function=lambda **_: [])
        if candidate_rejection:
            gateway.publish = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                CandidateRejected("no_reference_for_fuzzy_identity"))
            engine._amll_candidates = lambda _: iter([Candidate("amll", "fixture", "", False, "strict_metadata", {})])
        return engine

    def test_real_resolution_classifies_bad_json_as_parse_and_identity_as_input_required(self):
        parsed = self.engine(exception=json.JSONDecodeError("private", "x", 0)).resolve(
            media_path=Path("/fixture/song.flac"), metadata={}, expected_fingerprint={})
        self.assertEqual(parsed.reason, "source_attention_required")
        self.assertEqual(parsed.faults[0]["category"], "parse")
        identity = self.engine(candidate_rejection=True).resolve(
            media_path=Path("/fixture/song.flac"), metadata={}, expected_fingerprint={})
        self.assertEqual(identity.reason, "source_attention_required")
        self.assertEqual(identity.faults[0]["category"], "identity")

    def test_media_validation_hook_runs_before_any_provider_request(self):
        root, media, now, store, job, worker = self.fixture()
        class MediaIntegrityError(RuntimeError):
            code, retryable, details = "sample_count_mismatch", False, {}
        worker.gateway.validate_media = lambda *_args, **_kwargs: (_ for _ in ()).throw(MediaIntegrityError())
        worker.engine = SimpleNamespace(resolve=lambda **_: self.fail("provider must not run for corrupt media"))
        worker.process_source(store.claim("source", owner="guard"))
        held = store.get(job.id)
        self.assertEqual(held.status, "needs_attention")
        self.assertEqual(held.payload["source_state"]["category"], "media_integrity")
        self.assertEqual(media.read_bytes(), b"same immutable media")

    def test_alignment_integrity_hold_does_not_offer_model_fallback_or_drop_reference(self):
        root, media, now, store, job, worker = self.fixture()
        claim = store.claim("source", owner="source")
        store.defer_to_alignment(job.id, claim.lease_token,
            payload={"reference_text": "invented fixture lyric", "source_marker": "preserve"}, reason="fixture")
        alignment = store.claim("alignment", owner="nas")
        held = store.hold_job(job.id, alignment.lease_token, stage="alignment",
            fault=LyricFault("media_integrity", "flac_sample_count_mismatch").as_dict(),
            diagnostic_payload={"integrity_check": "failed"})
        self.assertEqual(held.status, "needs_attention")
        self.assertEqual(held.payload["source_marker"], "preserve")
        self.assertEqual(held.payload["reference_text"], "invented fixture lyric")
        self.assertEqual(held.payload["source_state"]["stage"], "alignment")
        self.assertEqual((held.lease_token, held.lease_owner, held.lease_expires_at), ("", "", 0))
        self.assertIsNone(store.claim("alignment", owner="must-not-reinfer"))
        self.assertIsNone(store.claim("mac_alignment", owner="must-not-fallback"))
        with self.assertRaises(RuntimeError):
            store.hold_job(job.id, alignment.lease_token, stage="alignment",
                           fault=LyricFault("media_integrity", "stale_lease").as_dict())

    def scan_hold(self):
        root, media, now, store, job, worker = self.fixture()
        source = store.claim("source", owner="source")
        sidecar = media.with_suffix(".ttml")
        sidecar.write_text("durable verified candidate")
        publication = {"sidecar_path": str(sidecar),
                       "candidate_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()}
        store.transition_to_scan(job.id, source.lease_token, result=publication,
                                  expected_statuses={"leased_source"})
        with store.connect() as db:
            db.execute("UPDATE jobs SET max_attempts=1 WHERE id=?", (job.id,))
        scan = store.claim("scan", owner="first-player-check")
        store.hold_job(job.id, scan.lease_token, stage="scan", budget_exhausted=True,
            fault=LyricFault("playback", "player_unavailable", True, "retry", "navidrome").as_dict())
        return root, media, now, store, job, publication

    def recovery_evidence(self, now, *, capability="player", provider="navidrome", generation=1):
        return {capability: {"ok": True, "status": "ok", "capability": capability,
            "provider": provider, "name": provider, "context_key": "persistent-probe-context",
            "last_failure_at": now - 20, "recovered_at": now - 1,
            "checked_at": now, "recovery_generation": generation}}

    def test_scan_hold_never_auto_restarts_source_on_sidecar_or_policy_change(self):
        root, media, now, store, job, publication = self.scan_hold()
        media.with_suffix(".lrc").write_text("[00:01]different fixture reference")
        with patch.dict("os.environ", {"WORD_LYRICS_SOURCE_POLICY_REVISION": "new-policy"}):
            self.assertEqual(store.reconcile_source_holds(), [])
            changed = source_context_key(media, job.metadata)
            resumed = store.resume_source_after_change(job.id, context_key=changed, reason="explicit policy recheck")
        self.assertEqual(resumed.status, "scan_pending")
        self.assertEqual(resumed.result, publication)
        self.assertIsNone(store.claim("source", owner="must-not-discover"))
        self.assertIsNone(store.claim("alignment", owner="must-not-recompute"))
        retry = store.claim("scan", owner="recheck-exact-publication")
        self.assertIsNotNone(retry)  # Lifetime claim count must not exhaust the new cycle.
        self.assertEqual(retry.result, publication)

    def test_fresh_player_recovery_resumes_scan_once_per_context_generation(self):
        root, media, now, store, job, publication = self.scan_hold()
        now[0] += 60
        evidence = self.recovery_evidence(now[0])
        self.assertEqual(store.resume_dependency_holds(evidence), [job.id])
        resumed = store.get(job.id)
        self.assertEqual(resumed.status, "scan_pending")
        self.assertEqual(resumed.result, publication)
        self.assertIsNone(store.claim("source", owner="must-not-bypass-player"))
        retry = store.claim("scan", owner="player-after-recovery")
        self.assertIsNotNone(retry)
        store.hold_for_attention(retry.id, retry.lease_token, stage="scan", budget_exhausted=True,
            fault=LyricFault("playback", "player_unavailable", True, "retry", "navidrome").as_dict())
        now[0] += 60
        # New checked_at alone must not make the same recovered generation new.
        same = self.recovery_evidence(now[0])
        self.assertEqual(store.resume_dependency_holds(same), [])
        self.assertEqual(store.get(job.id).status, "needs_attention")
        self.assertEqual(store.resume_dependency_holds(self.recovery_evidence(now[0], generation=2)), [job.id])
        verified = store.claim("scan", owner="verified-player")
        completed = store.complete(verified.id, verified.lease_token, result=publication,
                                   expected_statuses={"leased_scan"})
        self.assertEqual(completed.payload["source_state"]["action"], "recovered")
        self.assertEqual(completed.payload["source_recovery"]["last_recovered_at"], now[0])
        self.assertEqual(completed.payload["source_recovery"]["recovered_from"]["stage"], "scan")

    def test_heartbeat_first_success_and_stale_success_cannot_resume_dependency_hold(self):
        root, media, now, store, job, publication = self.scan_hold()
        now[0] += 60
        for bad in [{"updated_at": now[0], "stage": "idle"},
                    {"player": {"ok": True, "checked_at": now[0]}},
                    self.recovery_evidence(999),
                    self.recovery_evidence(now[0], generation=0),
                    self.recovery_evidence(now[0], capability="source", provider="kugou")]:
            self.assertEqual(store.resume_dependency_holds(bad), [])
        self.assertEqual(store.get(job.id).status, "needs_attention")

    def test_unused_recovery_proof_after_hold_still_expires_after_one_hour(self):
        root, media, now, store, job, publication = self.scan_hold()
        # This genuine-looking transition is later than the hold and its
        # generation has never been consumed. Age alone makes it unusable.
        evidence = self.recovery_evidence(now[0] + 60)
        now[0] += 3661
        self.assertEqual(store.resume_dependency_holds(evidence), [])
        self.assertEqual(store.get(job.id).status, "needs_attention")
        self.assertNotIn("dependency_recoveries", store.get(job.id).payload)
        with self.assertRaises(ValueError):
            store.resume_dependency_holds(evidence, max_evidence_age_seconds=86400)

    def test_long_source_outage_recovers_original_job_only_on_matching_probe_transition(self):
        root, media, now, store, job, worker = self.fixture()
        claim = store.claim("source", owner="offline")
        fault = LyricFault("transport", "tls_eof", True, "retry", "kugou_official_reference").as_dict()
        store.source_fault(claim.id, claim.lease_token, faults=[fault], diagnostic_payload={},
            context_key=source_context_key(media, job.metadata), max_temporary_attempts=1)
        now[0] += 60
        self.assertEqual(store.resume_dependency_holds(
            self.recovery_evidence(now[0], capability="source", provider="amll")), [])
        evidence = self.recovery_evidence(now[0], capability="source", provider="kugou")
        dynamic_key = {"source_kugou": evidence["source"]}
        self.assertEqual(store.resume_dependency_holds(dynamic_key), [job.id])
        worker.engine = SimpleNamespace(resolve=lambda **_: Resolution(
            alignment_payload={"reference_text": "recovered fixture verse"}, reason="reference available"))
        worker.process_source(store.claim("source", owner="online"))
        self.assertEqual(store.get(job.id).status, "needs_alignment")
        self.assertEqual(store.get(job.id).payload["source_recovery"]["recovery_generation"], 1)

    def test_source_claim_and_expired_lease_exhaustion_cannot_turn_structured_fault_into_dead_letter(self):
        for mode in ("claim", "lease"):
            root, media, now, store, job, worker = self.fixture()
            with store.connect() as db:
                db.execute("UPDATE jobs SET max_attempts=2 WHERE id=?", (job.id,))
            claim = store.claim("source", owner="first")
            waited = store.source_fault(claim.id, claim.lease_token,
                faults=[classify_exception(TimeoutError()).as_dict()], diagnostic_payload={},
                context_key=source_context_key(media, job.metadata))
            now[0] = waited.next_attempt_at + 1
            if mode == "claim":
                with store.connect() as db:
                    db.execute("UPDATE jobs SET attempts=2 WHERE id=?", (job.id,))
                self.assertIsNone(store.claim("source", owner="must-hold"))
            else:
                last = store.claim("source", owner="dies", lease_seconds=30)
                now[0] = last.lease_expires_at + 1
                store.reclaim_expired()
            self.assertEqual(store.get(job.id).status, "needs_attention")

    def test_custom_fault_fields_cannot_leak_urls_or_credentials_to_state_or_events(self):
        root, media, now, store, job, worker = self.fixture()
        claim = store.claim("source", owner="test")
        held = store.hold_job(job.id, claim.lease_token, stage="source", fault={
            "category": "parse", "code": "https://private.invalid/?token=secret-token",
            "provider": "https://private.invalid", "url": "secret-token", "details": {"key": "secret-token"}})
        with store.connect() as db:
            encoded = json.dumps([held.payload, held.last_error,
                [dict(row) for row in db.execute("SELECT * FROM events WHERE job_id=?", (job.id,))]])
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("https://private.invalid", encoded)


if __name__ == "__main__":
    unittest.main()
