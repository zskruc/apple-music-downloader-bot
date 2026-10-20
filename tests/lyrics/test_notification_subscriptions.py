from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.notifications import NotificationStore, notification_request_key
from word_lyrics_automation.queue import QueueStore


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.audio = self.root / "song.m4a"
        self.audio.write_bytes(b"fixture audio")
        self.now = [1000.]
        self.q = QueueStore(self.root / "queue.sqlite3", clock=lambda:self.now[0])
        self.n = NotificationStore(self.root / "queue.sqlite3", clock=lambda:self.now[0])

    def context(self, chat="A", task=1, batch="batch"):
        return {"source":"telegram","chat_id":chat,"task_id":task,"batch_id":batch}

    def metadata(self, **kwargs):
        return {"title":"Song","artist":"Artist","notification":self.context(**kwargs)}

    def enqueue(self, **kwargs):
        return self.q.enqueue(self.audio,metadata=self.metadata(**kwargs)).job

    def complete(self):
        claim=self.q.claim("source",owner="test");self.assertIsNotNone(claim)
        self.q.complete(claim.id,claim.lease_token,result={"provider":"fixture"},expected_statuses={"leased_source"})

    def test_keys_include_chat_task_or_origin_and_batch(self):
        key=notification_request_key(self.context())
        self.assertEqual(key,notification_request_key(self.context(task="1")))
        for context in (self.context(chat="B"),self.context(task=2),self.context(batch="second")):
            self.assertNotEqual(key,notification_request_key(context))
        origin={"source":"telegram","chat_id":"A","origin_message_id":7}
        self.assertEqual(notification_request_key(origin),notification_request_key({**origin,"task_id":0}))
        self.assertNotEqual(key,notification_request_key(origin))
        self.assertEqual("",notification_request_key({"source":"telegram","chat_id":"A"}))
        self.assertEqual("",notification_request_key({"source":"telegram","chat_id":"A","task_id":True}))

    def test_same_job_fans_out_without_overwriting_original_metadata(self):
        a=self.enqueue(); b=self.enqueue(chat="B",task=2)
        self.assertEqual(a.id,b.id)
        self.assertEqual(self.q.get(a.id).metadata["notification"],self.context())
        self.complete();delivered=[]
        for _ in range(2):
            notice=self.n.claim(owner="bot");self.assertIsNotNone(notice)
            delivered.append(notice.context["chat_id"])
            self.n.delivered(notice.job_id,notice.lease_token,message_id=10,chat_id=notice.context["chat_id"])
        self.assertEqual(delivered,["A","B"])
        self.assertIsNone(self.n.claim(owner="bot"))

    def test_later_subscriber_gets_own_notice_without_redelivering_first(self):
        self.enqueue();self.complete();a=self.n.claim(owner="bot")
        self.n.delivered(a.job_id,a.lease_token)
        self.enqueue(chat="B",task=2);b=self.n.claim(owner="bot")
        self.assertEqual(b.context["chat_id"],"B")
        self.n.delivered(b.job_id,b.lease_token)
        self.assertIsNone(self.n.claim(owner="bot"))

    def make_legacy(self):
        job=self.enqueue()
        with self.q.connect() as db:db.execute("DELETE FROM notification_subscriptions WHERE job_id=?",(job.id,))
        self.complete();return job

    def test_legacy_delivered_origin_is_inherited_but_other_chat_is_not(self):
        job=self.make_legacy();a=self.n.claim(owner="old-bot")
        self.assertIsNone(a.subscription_id);self.n.delivered(job.id,a.lease_token)
        self.enqueue(chat="B",task=2)
        b=self.n.claim(owner="new-bot");self.assertEqual(b.context["chat_id"],"B")
        self.n.delivered(b.job_id,b.lease_token)
        self.assertIsNone(self.n.claim(owner="bot"))
        with self.q.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM notification_deliveries WHERE job_id=?",(job.id,)).fetchone()[0],"delivered")

    def test_inflight_legacy_lease_migrates_without_duplicate_send(self):
        job=self.make_legacy();a=self.n.claim(owner="old-bot")
        self.enqueue(chat="B",task=2)
        b=self.n.claim(owner="new-bot");self.assertEqual(b.context["chat_id"],"B")
        self.n.delivered(b.job_id,b.lease_token)
        self.assertIsNone(self.n.claim(owner="bot"))
        self.n.delivered(a.job_id,a.lease_token,message_id=11,chat_id="A")
        self.assertIsNone(self.n.claim(owner="bot"))
        with self.q.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM notification_deliveries WHERE job_id=?",(job.id,)).fetchone()[0],"delivered")

    def test_failed_send_and_restart_are_independent_per_subscriber(self):
        self.enqueue();self.enqueue(chat="B",task=2);self.complete()
        a=self.n.claim(owner="bot");self.n.failed(a.job_id,a.lease_token,"network")
        b=self.n.claim(owner="bot");self.assertEqual(b.context["chat_id"],"B")
        self.n.delivered(b.job_id,b.lease_token)
        self.n=NotificationStore(self.root/"queue.sqlite3",clock=lambda:self.now[0])
        self.assertIsNone(self.n.claim(owner="restarted"));self.now[0]+=30
        retry=self.n.claim(owner="restarted");self.assertEqual(retry.subscription_id,a.subscription_id)
        self.assertNotEqual(retry.lease_token,a.lease_token)
        self.n.delivered(retry.job_id,retry.lease_token)
        self.assertIsNone(self.n.claim(owner="bot"))

    def test_expired_lease_reclaims_only_unsent_subscription(self):
        self.enqueue();self.complete();first=self.n.claim(owner="dead",lease_seconds=30)
        self.now[0]+=31;retry=self.n.claim(owner="new",lease_seconds=30)
        self.assertEqual(first.subscription_id,retry.subscription_id)
        with self.assertRaises(RuntimeError):self.n.delivered(first.job_id,first.lease_token)
        self.n.delivered(retry.job_id,retry.lease_token)

    def test_response_chat_and_real_message_id_are_checked(self):
        self.enqueue();self.complete();notice=self.n.claim(owner="bot")
        with self.assertRaises(ValueError):self.n.delivered(notice.job_id,notice.lease_token,message_id=3,chat_id="B")
        for value in ("cb-not-a-message-id",True,-1,0):
            with self.assertRaises(ValueError):self.n.delivered(notice.job_id,notice.lease_token,message_id=value)
        self.n.delivered(notice.job_id,notice.lease_token,message_id=123,chat_id="A")
        item=self.n.request_jobs([notice.request_key])[notice.request_key][0]
        self.assertEqual(item["notice_message_id"],"123");self.assertEqual(item["notice_chat_id"],"A")

    def test_legacy_skipped_job_with_new_valid_request_can_notify(self):
        self.q.enqueue(self.audio,metadata={"title":"Song"});self.complete()
        self.assertIsNone(self.n.claim(owner="bot"))
        self.enqueue();notice=self.n.claim(owner="bot")
        self.assertIsNotNone(notice.subscription_id)

    def test_source_wait_and_consent_do_not_consume_terminal_receipt(self):
        job=self.enqueue();claim=self.q.claim("source",owner="source")
        self.q.wait_for_source(job.id,claim.lease_token,error="not found",diagnostic_payload={})
        wait=self.n.claim(owner="bot");self.assertEqual(wait.notification_kind,"source_wait")
        self.n.delivered(job.id,wait.lease_token)
        self.now[0]+=21601;source=self.q.claim("source",owner="source")
        self.q.defer_to_alignment(job.id,source.lease_token,payload={"reference_text":"trusted"},reason="test")
        align=self.q.claim("alignment",owner="nas")
        self.q.fail(job.id,align.lease_token,stage="alignment",error="quality",retryable=False,offer_mac_on_terminal=True)
        consent=self.n.claim(owner="bot");self.assertEqual(consent.notification_kind,"consent")
        self.n.delivered(job.id,consent.lease_token)
        self.q.set_mac_consent(job.id,approved=True,actor="test")
        mac=self.q.claim("mac_alignment",owner="mac")
        self.q.complete(job.id,mac.lease_token,result={},expected_statuses={"leased_mac_alignment"})
        final=self.n.claim(owner="bot");self.assertEqual(final.notification_kind,"terminal")
        self.assertEqual(final.terminal_status,"completed")
        self.n.delivered(job.id,final.lease_token);self.assertIsNone(self.n.claim(owner="bot"))

    def test_subscribed_decline_has_distinct_failure_notification(self):
        job=self.enqueue();source=self.q.claim("source",owner="source")
        self.q.defer_to_alignment(job.id,source.lease_token,payload={},reason="test")
        align=self.q.claim("alignment",owner="nas")
        self.q.fail(job.id,align.lease_token,stage="alignment",error="quality",retryable=False,offer_mac_on_terminal=True)
        consent=self.n.claim(owner="bot");self.n.delivered(job.id,consent.lease_token)
        self.q.set_mac_consent(job.id,approved=False,actor="test")
        final=self.n.claim(owner="bot");self.assertEqual(final.terminal_status,"dead_letter")
        self.assertEqual(final.notification_kind,"terminal")

    def test_preupgrade_declined_legacy_consent_is_not_inherited_as_terminal(self):
        job=self.enqueue()
        with self.q.connect() as db:db.execute("DELETE FROM notification_subscriptions WHERE job_id=?",(job.id,))
        source=self.q.claim("source",owner="source")
        self.q.defer_to_alignment(job.id,source.lease_token,payload={},reason="test")
        align=self.q.claim("alignment",owner="nas")
        self.q.fail(job.id,align.lease_token,stage="alignment",error="quality",retryable=False,offer_mac_on_terminal=True)
        consent=self.n.claim(owner="old-bot");self.assertIsNone(consent.subscription_id)
        self.n.delivered(job.id,consent.lease_token)
        # This fixture is the old pre-upgrade declined state, without the new
        # set_mac_consent migration. Its only old receipt was the consent card.
        with self.q.connect() as db:
            db.execute("UPDATE jobs SET status='dead_letter',last_error='mac_fallback_declined_by_user' WHERE id=?",(job.id,))
            db.execute("INSERT INTO events(job_id,event,detail_json,created_at) VALUES(?,'mac_alignment_declined','{}',?)",(job.id,self.now[0]))
        self.enqueue(chat="B",task=2)
        key=notification_request_key(self.context())
        self.assertTrue(self.n.request_jobs([key])[key][0]["needs_notice"])
        receivers=[]
        for _ in range(2):
            notice=self.n.claim(owner="new-bot");self.assertEqual(notice.notification_kind,"terminal")
            receivers.append(notice.context["chat_id"]);self.n.delivered(job.id,notice.lease_token)
        self.assertEqual(receivers,["A","B"])

    def test_batch_view_counts_unique_latest_media_and_keeps_tasks_separate(self):
        first=self.enqueue();other=self.root/"other.m4a";other.write_bytes(b"other")
        self.q.enqueue(other,metadata=self.metadata())
        self.enqueue(task=2);self.complete()
        key=notification_request_key(self.context());second_key=notification_request_key(self.context(task=2))
        view=self.n.request_jobs([key,second_key])
        self.assertEqual(len(view[key]),2);self.assertEqual(len(view[second_key]),1)
        self.assertEqual(sum(item["pending"] for item in view[key]),1)
        done=next(item for item in view[key] if item["job_id"]==first.id)
        self.assertTrue(done["completed"]);self.assertTrue(done["needs_notice"])
        self.assertEqual((done["title"],done["artist"]),("Song","Artist"))

    def test_replacement_carries_all_origins_and_latest_view_follows_it(self):
        first=self.enqueue();self.enqueue(chat="B",task=2)
        self.audio.write_bytes(b"new audio")
        replacement=self.q.enqueue(self.audio,metadata=self.metadata(chat="B",task=2)).job
        self.assertEqual(self.q.get(first.id).status,"superseded")
        for context in (self.context(),self.context(chat="B",task=2)):
            key=notification_request_key(context);view=self.n.request_jobs([key])[key]
            self.assertEqual(len(view),1);self.assertEqual(view[0]["job_id"],replacement.id)
            self.assertTrue(view[0]["pending"])
        self.complete();origins=[]
        for _ in range(2):
            notice=self.n.claim(owner="bot");origins.append(notice.context["chat_id"])
            self.n.delivered(notice.job_id,notice.lease_token)
        self.assertEqual(set(origins),{"A","B"})

    def test_forced_job_keeps_prior_receipt_and_duplicate_enqueue_finds_latest(self):
        old=self.enqueue();self.complete();notice=self.n.claim(owner="bot")
        self.n.delivered(old.id,notice.lease_token)
        forced=self.q.enqueue(self.audio,metadata=self.metadata(),force=True).job
        duplicate=self.enqueue();self.assertEqual(duplicate.id,forced.id)
        self.complete();final=self.n.claim(owner="bot");self.assertEqual(final.job_id,forced.id)
        self.assertEqual(self.q.get(old.id).status,"superseded")
        with self.q.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM notification_subscription_deliveries WHERE subscription_id=? AND kind='terminal'",(notice.subscription_id,)).fetchone()[0],"delivered")

    def test_other_request_force_does_not_reopen_closed_origin(self):
        old=self.enqueue();self.complete();notice=self.n.claim(owner="bot")
        self.n.delivered(old.id,notice.lease_token)
        replacement=self.q.enqueue(self.audio,metadata=self.metadata(chat="B",task=2),force=True).job
        a_key=notification_request_key(self.context());a_view=self.n.request_jobs([a_key])[a_key]
        self.assertEqual(a_view[0]["job_id"],old.id)
        self.assertTrue(a_view[0]["terminal_delivered"])
        self.assertTrue(a_view[0]["completed"]);self.assertFalse(a_view[0]["pending"])
        self.complete();final=self.n.claim(owner="bot")
        self.assertEqual(final.context["chat_id"],"B");self.assertEqual(final.job_id,replacement.id)
        self.n.delivered(final.job_id,final.lease_token)
        self.assertIsNone(self.n.claim(owner="bot"))

    def test_modern_terminal_missing_source_is_not_legacy_auto_recovered(self):
        job=self.enqueue();source=self.q.claim("source",owner="source")
        self.q.fail(job.id,source.lease_token,stage="source",error="no_trusted_lyric_reference",retryable=False)
        final=self.n.claim(owner="bot");self.n.delivered(job.id,final.lease_token)
        self.assertEqual(self.q.recover_missing_source_jobs(),0)
        self.assertEqual(self.q.get(job.id).status,"dead_letter")
        self.assertIsNone(self.n.claim(owner="bot"))

    def test_enqueue_subscription_registration_rolls_back_with_job_transaction(self):
        from unittest.mock import patch
        with patch("word_lyrics_automation.queue.register_notification_subscription",side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):self.enqueue()
        self.assertEqual(self.q.active_jobs(),[])


class ExistingSidecarQueueTests(unittest.TestCase):
    def test_changed_audio_keeps_preexisting_sidecar_and_retracts_only_owned_derivative(self):
        for adapted in (False,True):
            with self.subTest(adapted=adapted),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);audio=root/"song.m4a";audio.write_bytes(b"old audio")
                q=QueueStore(root/"queue.sqlite3");job=q.enqueue(audio).job
                source=q.claim("source",owner="test");sidecar=audio.with_suffix(".ttml")
                original=b"original preexisting TTML";original_sha=hashlib.sha256(original).hexdigest()
                sidecar.write_bytes(original)
                result={"sidecar_path":str(sidecar),"candidate_sha256":original_sha,
                        "backup_path":"","existing_sidecar_sha256":original_sha}
                if adapted:
                    backup=root/"before-character.ttml";backup.write_bytes(original)
                    derived=b"owned character TTML";sidecar.write_bytes(derived)
                    result["character_adaptation_intent"]={"sidecar_path":str(sidecar),
                        "source_candidate_sha256":original_sha,"backup_path":str(backup),
                        "candidate_sha256":hashlib.sha256(derived).hexdigest()}
                q.transition_to_scan(job.id,source.lease_token,result=result,expected_statuses={"leased_source"})
                audio.write_bytes(b"new changed audio");q.enqueue(audio)
                self.assertEqual(sidecar.read_bytes(),original)
                self.assertEqual(q.get(job.id).status,"superseded")


if __name__=="__main__":unittest.main()
