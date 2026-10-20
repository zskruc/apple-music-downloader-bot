import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ingest_bot as bot
from word_lyrics_automation.notifications import NotificationStore, notification_request_key
from word_lyrics_automation.queue import QueueStore


class BotLyricLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / 'Song.m4a'
        self.media.write_bytes(b'original media fixture')
        self.database = self.root / 'queue.sqlite3'
        self.context = {'source': 'telegram', 'chat_id': '-1001', 'task_id': 71,
                        'message_id': 'callback-is-not-delete-id', 'batch_id': 'fixture'}
        queue = QueueStore(self.database)
        self.job = queue.enqueue(self.media, metadata={'title': 'Song', 'artist': 'Artist',
            'notification': self.context}).job
        claim = queue.claim('source', owner='fixture')
        queue.complete(claim.id, claim.lease_token, result={'provider': 'apple', 'timed_words': 42},
                       expected_statuses={'leased_source'})
        self.store = NotificationStore(self.database)

    def test_successful_send_persists_real_receipt_and_is_not_repeated(self):
        notice = self.store.claim(owner='bot')
        with patch.object(bot, 'send_message', return_value={'message_id': 910}) as send:
            self.assertTrue(bot.deliver_word_lyrics_notification(self.store, notice))
        self.assertIn('42', send.call_args.args[1])
        row = self.store.request_jobs([notice.request_key])[notice.request_key][0]
        self.assertTrue(row['terminal_delivered'])
        self.assertEqual(row['notice_message_id'], '910')
        self.assertEqual(row['notice_chat_id'], '-1001')
        self.assertIsNone(NotificationStore(self.database).claim(owner='restart'))

    def test_no_or_malformed_send_receipt_is_not_success(self):
        notice = self.store.claim(owner='bot')
        with patch.object(bot, 'send_message', return_value={}):
            self.assertFalse(bot.deliver_word_lyrics_notification(self.store, notice))
        row = self.store.request_jobs([notice.request_key])[notice.request_key][0]
        self.assertTrue(row['needs_notice'])
        self.assertFalse(row['terminal_delivered'])

    def test_delete_already_absent_message_is_success_without_changing_target(self):
        with patch.object(bot, 'telegram_api', side_effect=RuntimeError('message to delete not found')) as api:
            self.assertTrue(bot.delete_message('-1001', '910'))
        self.assertEqual(api.call_args.args[1], {'chat_id': '-1001', 'message_id': '910'})

    def test_entry_database_outage_reports_each_song_without_exposing_exception(self):
        with patch.object(bot, '_sync_word_lyrics_for_paths', side_effect=RuntimeError('BOT_SECRET_TOKEN')), \
                patch.object(bot, 'send_message', return_value={'message_id': 911}) as send:
            result = bot.sync_word_lyrics_for_paths([self.media, self.root / 'Other.m4a'],
                notification_context=self.context)
        self.assertEqual(result, {'error': 2})
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertIn('失败', call.args[1])
            self.assertIn('原因', call.args[1])
            self.assertNotIn('BOT_SECRET_TOKEN', call.args[1])

    def test_failure_notification_contains_song_and_reason(self):
        failure = bot.WordLyricNotification(job_id=91, terminal_status='dead_letter',
            media_path=str(self.media), metadata={'title': 'Song', 'artist': 'Artist'},
            result={}, last_error='ValueError:invalid_native_window', attempts=2, lease_token='x')
        text = bot.format_word_lyrics_notification(failure)
        for value in ['处理失败', 'Song', 'Artist', '原因', 'invalid_native_window']:
            self.assertIn(value, text)


if __name__ == '__main__':
    unittest.main()
