from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import test_native_entry_guards as fixtures
from word_lyrics_automation.lyric_messages import LyricMessageStore
from word_lyrics_automation.notifications import NotificationStore, notification_request_key
from word_lyrics_automation.queue import QueueStore

word_lyrics = fixtures.word_lyrics


class InlineLyricLifecycleTests(unittest.TestCase):
    def setUp(self):
        fixtures.NativeEntryGuardTests.setUp(self)
        self.audio = self.audio.resolve()
        self.context = {'source': 'telegram', 'chat_id': '-1001', 'task_id': 100,
                        'message_id': 'opaque-callback-not-a-delete-id'}
        self.database = self.service.state_dir / 'automation/queue.sqlite3'

    def sync(self, paths):
        with patch.object(word_lyrics, 'service_from_environment', return_value=self.service):
            return word_lyrics.sync_word_lyrics_for_paths(paths, notification_context=self.context)

    def test_native_success_enters_normal_queue_and_single_track_notice(self):
        self.assertEqual(self.sync([self.audio]), {'accepted': 1})
        queue = QueueStore(self.database)
        job = queue.active_jobs()[0]
        self.assertEqual(job.metadata['initial_status'], 'accepted')
        self.assertTrue(job.metadata['notification']['batch_id'])
        notifications = NotificationStore(self.database)
        self.assertIsNone(notifications.claim(owner='bot'))
        claim = queue.claim('source', owner='source')
        queue.complete(claim.id, claim.lease_token, result={'provider': 'apple', 'timed_words': 4},
                       expected_statuses={'leased_source'})
        notice = notifications.claim(owner='bot')
        self.assertEqual(notice.terminal_status, 'completed')
        self.assertEqual(notice.context['chat_id'], '-1001')
        notifications.delivered(notice.job_id, notice.lease_token, message_id=77, chat_id='-1001')
        projection = notifications.request_jobs([notice.request_key])[notice.request_key][0]
        self.assertTrue(projection['terminal_delivered'])
        self.assertEqual(projection['notice_message_id'], '77')
        self.assertIsNone(notifications.claim(owner='bot'))
        self.assertEqual(self.audio.read_bytes(), b'immutable audio fixture')

    def test_cached_success_and_repeated_same_batch_do_not_duplicate(self):
        self.audio.with_suffix('.ttml').write_text(fixtures.NATIVE)
        self.assertEqual(self.sync([self.audio]), {'cached': 1})
        self.assertEqual(self.sync([self.audio]), {'cached': 1})
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notification_subscriptions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lyric_batch_messages').fetchone()[0], 1)

    def test_one_track_exception_keeps_other_tracks_and_background_recovery(self):
        other = (self.root / 'other.m4a').resolve()
        other.write_bytes(b'other original audio')
        actual = self.service.process
        def process(track, **kwargs):
            if track.path == self.audio:
                raise ValueError('broken provider response')
            return actual(track, **kwargs)
        with patch.object(self.service, 'process', side_effect=process):
            summary = self.sync([self.audio, other])
        self.assertEqual(summary['error'], 1)
        queue = QueueStore(self.database)
        self.assertEqual({j.media_path for j in queue.active_jobs()}, {str(self.audio), str(other)})
        self.assertEqual(len({j.metadata['notification']['batch_id'] for j in queue.active_jobs()}), 1)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT collecting FROM lyric_batch_messages').fetchone()[0], 0)

    def test_failed_registration_gets_durable_redacted_failure_notice(self):
        secret = 'https://api.telegram.org/botVERY_SECRET_TOKEN/sendMessage'
        with patch.object(QueueStore, 'enqueue', side_effect=sqlite3.OperationalError(secret)):
            self.sync([self.audio])
        notifications = NotificationStore(self.database)
        sent = []
        def send(chat, text):
            sent.append((chat, text))
            return {'message_id': 81}
        actions = LyricMessageStore(self.database).reconcile(
            notifications, send=send, edit=lambda *_: self.fail('nothing to edit'),
            delete=lambda *_: self.fail('no known queue card to delete'))
        self.assertEqual(len(sent), 1)
        self.assertIn('失败', sent[0][1])
        self.assertIn('OperationalError', sent[0][1])
        self.assertNotIn('VERY_SECRET_TOKEN', sent[0][1])
        self.assertIn('finished_without_queue_card', actions)
        LyricMessageStore(self.database).reconcile(notifications, send=lambda *_: self.fail('duplicate'),
            edit=lambda *_: False, delete=lambda *_: False)

    def test_two_fixed_path_batches_in_one_task_have_separate_queue_cards(self):
        other = (self.root / 'other.m4a').resolve()
        other.write_bytes(b'other original audio')
        self.sync([self.audio])
        self.sync([other])
        with sqlite3.connect(self.database) as db:
            rows = db.execute('SELECT request_key,paths_json FROM lyric_batch_messages').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({tuple(json.loads(row[1])) for row in rows}, {(str(self.audio),), (str(other),)})
        self.assertEqual(len({row[0] for row in rows}), 2)

    def test_committed_jobs_survive_finish_marker_error_without_false_failure(self):
        with patch.object(LyricMessageStore, 'finish', side_effect=OSError('marker unavailable')):
            self.assertEqual(self.sync([self.audio]), {'accepted': 1})
        queue = QueueStore(self.database)
        claim = queue.claim('source', owner='fixture')
        queue.complete(claim.id, claim.lease_token, result={'provider': 'apple'},
                       expected_statuses={'leased_source'})
        notifications = NotificationStore(self.database)
        notice = notifications.claim(owner='bot')
        notifications.delivered(notice.job_id, notice.lease_token, message_id=83, chat_id='-1001')
        actions = LyricMessageStore(self.database).reconcile(notifications,
            send=lambda *_: self.fail('no fake failure or unnecessary queue message'),
            edit=lambda *_: False, delete=lambda *_: False)
        self.assertIn('finished_without_queue_card', actions)

    def test_logger_error_after_enqueue_does_not_change_song_outcome(self):
        with patch.object(word_lyrics, 'service_from_environment', return_value=self.service):
            summary = word_lyrics.sync_word_lyrics_for_paths([self.audio], notification_context=self.context,
                logger=lambda _: (_ for _ in ()).throw(OSError('log disk unavailable')))
        self.assertEqual(summary, {'accepted': 1})
        self.assertEqual(len(QueueStore(self.database).active_jobs()), 1)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lyric_batch_errors').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
