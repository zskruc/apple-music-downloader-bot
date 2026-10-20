from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.lyric_messages import LyricMessageStore


class FakeNotificationStore:
    """The documented request_jobs projection, with no imaginary queue APIs."""
    def __init__(self):
        self.requests = {}

    def request_jobs(self, request_keys):
        return {key: copy.deepcopy(self.requests.get(key, [])) for key in request_keys}

    def set_jobs(self, key, paths, statuses=None, delivered=None):
        statuses = statuses or ['queued_source'] * len(paths)
        delivered = delivered or [False] * len(paths)
        self.requests[key] = [
            {'job_id': index + 1, 'subscription_id': index + 1, 'request_key': key,
             'media_path': str(path.resolve()), 'title': path.stem, 'artist': 'Artist',
             'status': status, 'pending': status not in ('completed', 'dead_letter'),
             'completed': status == 'completed', 'failed': status == 'dead_letter',
             'needs_notice': status in ('completed', 'dead_letter') and not sent,
             'terminal_delivered': sent, 'notice_message_id': '500' if sent else '',
             'notice_chat_id': '', 'notice_delivered_at': 1000 if sent else 0,
             'last_error': ''}
            for index, (path, status, sent) in enumerate(zip(paths, statuses, delivered, strict=True))
        ]


class FakeTelegram:
    def __init__(self):
        self.next_id = 100
        self.sent = []
        self.edited = []
        self.deleted = []
        self.send_results = []
        self.edit_results = []
        self.delete_results = []

    @staticmethod
    def outcome(queue, default):
        value = queue.pop(0) if queue else default
        if isinstance(value, Exception):
            raise value
        return value

    def send(self, chat, text):
        result = self.outcome(self.send_results, {'message_id': self.next_id})
        self.next_id += 1
        if result and str(result.get('message_id', '')).isdigit():
            self.sent.append((str(chat), str(result['message_id']), text))
        return result

    def edit(self, chat, message, text):
        if not self.outcome(self.edit_results, True):
            return False
        self.edited.append((str(chat), str(message), text))
        return True

    def delete(self, chat, message):
        if not self.outcome(self.delete_results, True):
            return False
        self.deleted.append((str(chat), str(message)))
        return True


class LyricMessageStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'queue.sqlite3'
        self.now = 1000.0
        self.store = LyricMessageStore(self.database, clock=lambda: self.now)
        self.notifications = FakeNotificationStore()
        self.telegram = FakeTelegram()
        self.paths = [self.root / (name + '.m4a') for name in ['First', 'Second', 'Third']]

    def batch(self, paths=None, *, chat='chat-a', task=1, finish=True, origin='origin-a'):
        paths = self.paths if paths is None else paths
        key = self.store.prepare({'source': 'telegram', 'chat_id': chat, 'task_id': task,
                                  'origin_message_id': origin}, paths)
        self.assertTrue(key)
        if finish:
            self.store.finish(key)
        self.notifications.set_jobs(key, paths)
        return key

    def reconcile(self):
        return self.store.reconcile(self.notifications, send=self.telegram.send,
                                    edit=self.telegram.edit, delete=self.telegram.delete)

    def advance(self):
        self.now += 6

    def row(self, key):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM lyric_batch_messages WHERE request_key=?', (key,)).fetchone())

    def test_three_song_progress_counts_three_two_one_zero_then_deletes_after_notices(self):
        key = self.batch()
        self.reconcile()
        for completed in (1, 2):
            self.notifications.set_jobs(key, self.paths,
                ['completed'] * completed + ['queued_source'] * (3 - completed),
                [True] * completed + [False] * (3 - completed))
            self.advance()
            self.reconcile()
            self.assertEqual(self.telegram.deleted, [])
        self.notifications.set_jobs(key, self.paths, ['completed'] * 3, [True, True, False])
        self.advance()
        self.reconcile()
        texts = [self.telegram.sent[0][2]] + [row[2] for row in self.telegram.edited]
        self.assertEqual([text.splitlines()[0] for text in texts],
                         [f'逐词歌词队列：剩余 {count} 首 ⏳' for count in (3, 2, 1, 0)])
        self.assertIn('正在发送结果通知：1 首', texts[-1])
        self.assertEqual(self.telegram.deleted, [])
        self.notifications.set_jobs(key, self.paths, ['completed'] * 3, [True] * 3)
        self.advance()
        self.reconcile()
        self.assertEqual(self.telegram.deleted, [('chat-a', self.telegram.sent[0][1])])
        self.assertEqual(self.row(key)['state'], 'deleted')

    def test_restart_keeps_same_card_and_pending_second_song_after_first_completed(self):
        key = self.batch(self.paths[:2])
        self.reconcile()
        receipt = self.telegram.sent[0][1]
        self.store = LyricMessageStore(self.database, clock=lambda: self.now)
        self.notifications.set_jobs(key, self.paths[:2], ['completed', 'leased_alignment'], [True, False])
        self.advance()
        self.reconcile()
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.telegram.deleted, [])
        self.assertIn('剩余 1 首', self.telegram.edited[-1][2])
        self.assertIn('Second', self.telegram.edited[-1][2])
        self.assertNotIn('First（', self.telegram.edited[-1][2])
        self.assertEqual(self.row(key)['message_id'], receipt)

    def test_completed_work_waits_for_actual_terminal_delivery_before_removing_card(self):
        key = self.batch(self.paths[:1])
        self.reconcile()
        self.notifications.set_jobs(key, self.paths[:1], ['completed'], [False])
        for _ in range(2):
            self.advance()
            self.reconcile()
        self.assertEqual(self.telegram.deleted, [])
        self.assertEqual(len(self.telegram.sent), 1)  # Ledger never invents the per-job completion notice.
        self.assertIn('正在发送结果通知：1 首', self.row(key)['sent_text'])
        self.notifications.set_jobs(key, self.paths[:1], ['completed'], [True])
        self.advance()
        self.reconcile()
        self.assertEqual(len(self.telegram.deleted), 1)

    def test_send_failure_retries_without_persisting_an_invented_message_id(self):
        key = self.batch(self.paths[:1])
        self.telegram.send_results.append(None)
        self.assertEqual(self.reconcile(), ['retry'])
        self.assertEqual(self.row(key)['message_id'], '')
        self.assertEqual(self.telegram.deleted, [])
        self.advance()
        self.assertEqual(self.reconcile(), ['sent'])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.row(key)['message_id'], self.telegram.sent[0][1])

    def test_edit_and_delete_failures_retry_same_receipt_across_restart(self):
        key = self.batch(self.paths[:2])
        self.reconcile()
        receipt = self.telegram.sent[0][1]
        self.notifications.set_jobs(key, self.paths[:2], ['completed', 'queued_source'], [True, False])
        self.telegram.edit_results.append(False)
        self.advance()
        self.assertEqual(self.reconcile(), ['retry'])
        self.assertIn('剩余 2 首', self.row(key)['sent_text'])
        self.advance()
        self.assertEqual(self.reconcile(), ['edited'])
        self.notifications.set_jobs(key, self.paths[:2], ['completed'] * 2, [True] * 2)
        self.telegram.delete_results.append(False)
        self.advance()
        self.assertEqual(self.reconcile(), ['retry'])
        self.store = LyricMessageStore(self.database, clock=lambda: self.now)
        self.advance()
        self.assertEqual(self.reconcile(), ['deleted'])
        self.assertEqual(self.telegram.deleted, [('chat-a', receipt)])
        self.assertEqual(len(self.telegram.sent), 1)

    def test_registration_failure_notice_is_not_resent_after_edit_or_delete_retry(self):
        key = self.batch(self.paths[:2])
        self.notifications.set_jobs(key, self.paths[1:2])
        self.store.registration_failed(key, self.paths[0], RuntimeError('do not disclose URL'))
        self.reconcile()
        failures = [row for row in self.telegram.sent if row[2].startswith('逐词歌词处理失败')]
        self.assertEqual(len(failures), 1)
        self.assertNotIn('do not disclose URL', failures[0][2])
        self.notifications.set_jobs(key, self.paths[1:2], ['completed'], [True])
        self.telegram.delete_results.append(False)
        self.advance()
        self.reconcile()
        self.store = LyricMessageStore(self.database, clock=lambda: self.now)
        self.advance()
        self.reconcile()
        self.assertEqual(len([row for row in self.telegram.sent if row[2].startswith('逐词歌词处理失败')]), 1)
        self.assertEqual(self.row(key)['state'], 'deleted')

    def test_cross_chat_batches_for_same_song_only_delete_their_own_receipts(self):
        first = self.batch(self.paths[:1], chat='chat-a', task=1)
        second = self.batch(self.paths[:1], chat='chat-b', task=1)
        self.reconcile()
        receipts = {chat: message for chat, message, _ in self.telegram.sent}
        self.notifications.set_jobs(first, self.paths[:1], ['completed'], [True])
        self.notifications.set_jobs(second, self.paths[:1], ['completed'], [False])
        self.advance()
        self.reconcile()
        self.assertEqual(self.telegram.deleted, [('chat-a', receipts['chat-a'])])
        self.assertEqual(self.row(second)['state'], 'active')
        self.notifications.set_jobs(second, self.paths[:1], ['completed'], [True])
        self.advance()
        self.reconcile()
        self.assertEqual(self.telegram.deleted[-1], ('chat-b', receipts['chat-b']))

    def test_opaque_callback_identity_never_becomes_a_delete_message_id(self):
        key = self.batch(self.paths[:1], task='callback:opaque', origin='callback:opaque')
        self.telegram.send_results.append({'message_id': 'callback:opaque'})
        self.assertEqual(self.reconcile(), ['retry'])
        self.assertEqual(self.row(key)['message_id'], '')
        self.notifications.set_jobs(key, self.paths[:1], ['completed'], [True])
        self.advance()
        self.reconcile()
        self.assertEqual(self.telegram.deleted, [])
        self.assertEqual(self.row(key)['state'], 'deleted')
        other = self.batch(self.paths[:1], task='other:opaque', origin='other:opaque')
        self.advance()
        self.reconcile()
        actual_receipt = self.telegram.sent[-1][1]
        self.notifications.set_jobs(other, self.paths[:1], ['completed'], [True])
        self.advance()
        self.reconcile()
        self.assertEqual(self.telegram.deleted, [('chat-a', actual_receipt)])

    def test_collecting_batch_does_not_disappear_before_registration_finishes(self):
        key = self.batch(self.paths[:2], finish=False)
        self.notifications.set_jobs(key, self.paths[:1], ['completed'], [True])
        self.reconcile()
        self.assertEqual(self.telegram.deleted, [])
        self.assertEqual(self.row(key)['state'], 'active')
        self.assertIn('剩余 1 首', self.row(key)['sent_text'])
        self.assertIn('正在登记：1 首', self.row(key)['sent_text'])
        # The fixed member set is now fully registered and acknowledged. A
        # failed finish-marker write must not keep an otherwise done card.
        self.notifications.set_jobs(key, self.paths[:2], ['completed'] * 2, [True] * 2)
        self.advance()
        self.reconcile()
        self.assertEqual(self.row(key)['state'], 'deleted')
        self.assertEqual(self.telegram.deleted, [('chat-a', self.telegram.sent[0][1])])

    def test_existing_registered_job_suppresses_obsolete_undelivered_registration_error(self):
        key = self.batch(self.paths[:1])
        self.store.registration_failed(key, self.paths[0], RuntimeError('temporary commit acknowledgement failure'))
        self.reconcile()
        self.assertFalse(any(row[2].startswith('逐词歌词处理失败') for row in self.telegram.sent))
        self.assertIn('失败 0 首', self.row(key)['sent_text'])

    def test_transport_exception_does_not_persist_bot_url_or_token(self):
        key = self.batch(self.paths[:1])
        self.telegram.send_results.append(RuntimeError('https://api.telegram.org/bot-secret-token/sendMessage'))
        self.reconcile()
        error = self.row(key)['last_error']
        self.assertIn('RuntimeError', error)
        self.assertNotIn('secret-token', error)
        self.assertNotIn('api.telegram.org', error)


    def test_startup_recovery_only_enqueues_missing_paths_from_the_old_process(self):
        old = self.batch(self.paths, finish=False)
        self.notifications.set_jobs(old, self.paths[:1], ['completed'], [True])
        existing = copy.deepcopy(self.notifications.requests[old][0])
        self.now = 1002
        current = self.batch(self.paths[:1], task=2, finish=False)
        self.notifications.requests[current] = []
        calls = []
        saved_context = json.loads(self.row(old)['context_json'])
        def enqueue(path, context):
            calls.append((str(path), copy.deepcopy(context)))
            self.assertEqual(context, saved_context)
            previous = self.notifications.requests[old]
            self.notifications.set_jobs(old, [path])
            self.notifications.requests[old] = previous + self.notifications.requests[old]
        restarted = LyricMessageStore(self.database, clock=lambda: self.now)
        restarted.recover_interrupted(self.notifications, before=1001, enqueue=enqueue)
        self.assertEqual({path for path, _ in calls}, {str(path.resolve()) for path in self.paths[1:]})
        self.assertEqual(self.notifications.requests[old][0], existing)
        self.assertEqual(self.row(old)['collecting'], 0)
        self.assertEqual(self.row(current)['collecting'], 1)
        restarted.recover_interrupted(self.notifications, before=1001, enqueue=lambda *_: self.fail('re-enqueued registered batch'))
        self.store = restarted
        self.reconcile()
        old_card = next(row for row in self.telegram.sent if '剩余 2 首' in row[2])
        self.assertNotIn('First（', old_card[2])

    def test_startup_recovery_finishes_an_already_registered_batch_without_new_jobs(self):
        key = self.batch(self.paths[:1], finish=False)
        self.notifications.set_jobs(key, self.paths[:1], ['completed'], [True])
        before = copy.deepcopy(self.notifications.requests[key])
        self.advance()
        self.store.recover_interrupted(self.notifications, before=self.now,
            enqueue=lambda *_: self.fail('completed registered job must not be enqueued'))
        self.assertEqual(self.notifications.requests[key], before)
        self.assertEqual(self.row(key)['collecting'], 0)
        self.assertEqual(self.reconcile(), ['finished_without_queue_card'])
        self.assertEqual(self.telegram.sent, [])

    def test_recovery_missing_registration_becomes_an_explicit_durable_failure(self):
        for raises in (True, False):
            with self.subTest(raises=raises):
                key = self.batch(self.paths[:1], task=10 + int(raises), finish=False)
                self.notifications.requests[key] = []
                self.advance()
                def enqueue(path, context):
                    if raises:
                        raise OSError('private provider details')
                    return None  # No durable subscription despite a non-raising producer.
                self.store.recover_interrupted(self.notifications, before=self.now, enqueue=enqueue)
                self.assertEqual(self.row(key)['collecting'], 0)
                self.reconcile()
                self.assertEqual(self.row(key)['state'], 'deleted')
                matching = [row for row in self.telegram.sent if row[2].startswith('逐词歌词处理失败')]
                self.assertEqual(len(matching), 1 + int(not raises))
                self.assertNotIn('private provider details', matching[-1][2])

    def test_recovery_crash_after_one_enqueue_resumes_only_still_unregistered_paths(self):
        key = self.batch(self.paths[:2], finish=False)
        self.notifications.requests[key] = []
        self.advance()
        before = self.now
        calls = []
        def first_enqueue(path, context):
            calls.append(str(path))
            self.notifications.set_jobs(key, [path])
            raise KeyboardInterrupt('process exited after ordinary registration committed')
        with self.assertRaises(KeyboardInterrupt):
            self.store.recover_interrupted(self.notifications, before=before, enqueue=first_enqueue)
        self.assertEqual(self.row(key)['collecting'], 1)
        committed = self.notifications.requests[key][0]['media_path']
        self.store = LyricMessageStore(self.database, clock=lambda: self.now)
        def resumed_enqueue(path, context):
            calls.append(str(path))
            previous = self.notifications.requests[key]
            self.notifications.set_jobs(key, [path])
            self.notifications.requests[key] = previous + self.notifications.requests[key]
        self.store.recover_interrupted(self.notifications, before=before, enqueue=resumed_enqueue)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls.count(committed), 1)
        self.assertEqual(self.row(key)['collecting'], 0)
        self.assertEqual({row['media_path'] for row in self.notifications.requests[key]},
                         {str(path.resolve()) for path in self.paths[:2]})

    def test_failed_registration_notice_send_retries_then_survives_restart_without_duplicates(self):
        key = self.batch(self.paths[:2])
        self.notifications.set_jobs(key, self.paths[1:2])
        self.store.registration_failed(key, self.paths[0], RuntimeError('registration failed'))
        self.telegram.send_results.append(OSError('temporary transport outage'))
        self.assertEqual(self.reconcile(), ['retry'])
        self.assertEqual(self.telegram.sent, [])
        self.advance()
        self.reconcile()
        failures = [row for row in self.telegram.sent if row[2].startswith('逐词歌词处理失败')]
        self.assertEqual(len(failures), 1)
        self.store = LyricMessageStore(self.database, clock=lambda: self.now)
        self.advance()
        self.reconcile()
        self.assertEqual(len([row for row in self.telegram.sent if row[2].startswith('逐词歌词处理失败')]), 1)
        self.assertEqual(self.row(key)['state'], 'active')


    def test_distinct_batches_in_one_chat_do_not_remove_each_others_card(self):
        first = self.batch(self.paths[:1], chat='same-chat', task=1)
        second = self.batch(self.paths[1:2], chat='same-chat', task=2)
        self.reconcile()
        first_receipt = self.row(first)['message_id']
        second_receipt = self.row(second)['message_id']
        self.assertNotEqual(first_receipt, second_receipt)
        self.notifications.set_jobs(first, self.paths[:1], ['completed'], [True])
        self.advance()
        self.reconcile()
        self.assertEqual(self.telegram.deleted, [('same-chat', first_receipt)])
        self.assertEqual(self.row(second)['state'], 'active')
        self.assertEqual(self.row(second)['message_id'], second_receipt)


if __name__ == '__main__':
    unittest.main()
