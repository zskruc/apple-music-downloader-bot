"""Durable, request-scoped Telegram queue cards; no Telegram credentials here."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

from .notifications import notification_request_key


class LyricMessageStore:
    def __init__(self, database_path: Path, *, clock: Callable[[], float] = time.time):
        self.database_path, self.clock = database_path, clock
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS lyric_batch_messages (
                    request_key TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, paths_json TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}',
                    collecting INTEGER NOT NULL DEFAULT 1,
                    message_id TEXT NOT NULL DEFAULT '', sent_text TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'active',
                    lease_token TEXT NOT NULL DEFAULT '', lease_expires_at REAL NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lyric_batch_errors (
                    request_key TEXT NOT NULL, media_path TEXT NOT NULL,
                    reason TEXT NOT NULL, delivered_at REAL NOT NULL DEFAULT 0,
                    message_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(request_key, media_path)
                );
            ''')
            if 'context_json' not in {row['name'] for row in db.execute('PRAGMA table_info(lyric_batch_messages)')}:
                db.execute("ALTER TABLE lyric_batch_messages ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, context: dict[str, Any], paths: Iterable[str | Path]) -> str:
        key = notification_request_key(context)
        if not key or str(context.get('source')) != 'telegram':
            return ''
        paths = sorted({str(Path(path).resolve()) for path in paths})
        if not paths:
            return ''
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM lyric_batch_messages WHERE request_key=?', (key,)).fetchone()
            if old:
                if json.loads(old['paths_json']) != paths or old['chat_id'] != str(context['chat_id']):
                    raise ValueError('lyric batch identity changed')
                return key
            db.execute('''INSERT INTO lyric_batch_messages
                (request_key,chat_id,task_id,paths_json,context_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)''', (key, str(context['chat_id']), str(context.get('task_id') or ''),
                    json.dumps(paths, ensure_ascii=False), json.dumps(context, ensure_ascii=False), now, now))
        return key

    def finish(self, key: str):
        if key:
            with self.connect() as db:
                db.execute('UPDATE lyric_batch_messages SET collecting=0,updated_at=? WHERE request_key=?', (self.clock(), key))

    def registration_failed(self, key: str, media_path: Path, error: Exception):
        # Exception strings can contain provider URLs or credentials. The
        # human-readable stage plus exception class is sufficient here.
        if key:
            reason = f'后台任务登记失败（{type(error).__name__}）；已保留现有音频和歌词。'
            with self.connect() as db:
                db.execute('''INSERT INTO lyric_batch_errors(request_key,media_path,reason)
                    VALUES(?,?,?) ON CONFLICT(request_key,media_path) DO UPDATE SET
                    reason=CASE WHEN lyric_batch_errors.delivered_at=0 THEN excluded.reason ELSE lyric_batch_errors.reason END''',
                           (key, str(media_path.resolve()), reason))

    def recover_interrupted(self, notifications, *, before: float, enqueue):
        """Resume only collecting batches from an earlier bot process.

        Re-enqueue missing registrations through the ordinary, idempotent
        producer. Existing jobs/receipts are never reset or claimed here.
        """
        with self.connect() as db:
            batches = [dict(row) for row in db.execute('''SELECT * FROM lyric_batch_messages
                WHERE state='active' AND collecting=1 AND created_at<?''', (before,))]
        for batch in batches:
            rows = notifications.request_jobs([batch['request_key']]).get(batch['request_key'], [])
            found = {str(row['media_path']) for row in rows}
            context = json.loads(batch['context_json'])
            for name in set(json.loads(batch['paths_json'])) - found:
                try:
                    enqueue(Path(name), context)
                    latest = notifications.request_jobs([batch['request_key']]).get(batch['request_key'], [])
                    if name not in {str(row['media_path']) for row in latest}:
                        raise RuntimeError('lyric_registration_not_persisted')
                except Exception as error:
                    self.registration_failed(batch['request_key'], Path(name), error)
            self.finish(batch['request_key'])

    def _claim(self):
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT * FROM lyric_batch_messages WHERE state='active'
                AND next_attempt_at<=? AND lease_expires_at<=? ORDER BY updated_at LIMIT 1''', (now, now)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(24)
            db.execute('UPDATE lyric_batch_messages SET lease_token=?,lease_expires_at=? WHERE request_key=?',
                       (token, now + 300, row['request_key']))
            return {**dict(row), 'lease_token': token}

    def _save(self, batch, **updates):
        allowed = {'message_id', 'sent_text', 'state', 'next_attempt_at', 'attempts', 'last_error'}
        assert updates.keys() <= allowed
        updates.update(updated_at=self.clock(), lease_token='', lease_expires_at=0)
        with self.connect() as db:
            cursor = db.execute('UPDATE lyric_batch_messages SET ' + ','.join(name + '=?' for name in updates)
                                + ' WHERE request_key=? AND lease_token=?',
                                (*updates.values(), batch['request_key'], batch['lease_token']))
            if cursor.rowcount != 1:
                raise RuntimeError('lyric queue-message lease expired')

    def _snapshot(self, batch, notifications):
        rows = notifications.request_jobs([batch['request_key']]).get(batch['request_key'], [])
        expected = set(json.loads(batch['paths_json']))
        rows = [row for row in rows if str(row['media_path']) in expected]
        with self.connect() as db:
            current = db.execute('SELECT collecting FROM lyric_batch_messages WHERE request_key=?', (batch['request_key'],)).fetchone()
            errors = [dict(row) for row in db.execute('SELECT * FROM lyric_batch_errors WHERE request_key=?', (batch['request_key'],))]
        found = {str(row['media_path']) for row in rows}
        # A retry may have successfully registered the file after the first
        # error. Its real job now owns the outcome; keep old receipts only as
        # history, without emitting or counting a stale registration failure.
        errors = [row for row in errors if row['media_path'] not in found]
        failures = {row['media_path'] for row in errors}
        unknown = expected - found - failures
        pending = [row for row in rows if row['pending']]
        needs_notice = [row for row in rows if row['needs_notice']]
        return {'pending': pending, 'unknown': unknown, 'errors': errors,
                'completed': sum(bool(row['completed']) for row in rows),
                'failed': sum(bool(row['failed']) for row in rows) + len(errors),
                'notice_pending': len(needs_notice) + sum(not row['delivered_at'] for row in errors),
                'remaining': len(pending) + len(unknown),
                # With a fixed path set, all registrations are committed once
                # every member has a job or an explicit entry error. A failed
                # producer finish marker must not pin the queue card forever.
                'collecting': bool(current['collecting']) and bool(unknown)}

    @staticmethod
    def _text(snapshot):
        lines = [f"逐词歌词队列：剩余 {snapshot['remaining']} 首 ⏳",
                 f"已完成 {snapshot['completed']} 首，失败 {snapshot['failed']} 首。"]
        if snapshot['notice_pending']:
            lines.append(f"正在发送结果通知：{snapshot['notice_pending']} 首。")
        labels = {'waiting_source': '等待歌词来源', 'awaiting_mac_consent': '等待确认',
                  'leased_alignment': '分析中', 'retry_alignment': '等待重试',
                  'candidate_pending': '校验中', 'leased_validation': '校验中',
                  'scan_pending': '刷新播放器', 'leased_scan': '刷新播放器'}
        for row in snapshot['pending'][:12]:
            title = ' '.join(str(row.get('title') or Path(row['media_path']).stem).split())[:100]
            artist = ' '.join(str(row.get('artist') or '').split())[:70]
            lines.append(f"• {artist + ' - ' if artist else ''}{title}（{labels.get(row['status'], '排队中')}）")
        if len(snapshot['pending']) > 12:
            lines.append(f"另有 {len(snapshot['pending']) - 12} 首等待处理。")
        if snapshot['unknown']:
            lines.append(f"正在登记：{len(snapshot['unknown'])} 首。")
        return '\n'.join(lines)

    def reconcile(self, notifications, *, send, edit, delete, limit=8):
        """Only this observer changes queue cards; processors only record intent.

        Successful per-song notices are persisted before a queue card can be
        removed. A failed edit/delete retries without resending those notices.
        Every deletion uses a real send receipt belonging to this one batch.
        """
        actions = []
        for _ in range(limit):
            batch = self._claim()
            if batch is None:
                break
            try:
                snapshot = self._snapshot(batch, notifications)
                for error in snapshot['errors']:
                    if error['delivered_at']:
                        continue
                    text = ('逐词歌词处理失败 ❌\n标题：' + Path(error['media_path']).stem[:160]
                            + '\n原因：' + error['reason'])
                    result = send(batch['chat_id'], text)
                    message_id = str((result or {}).get('message_id') or '')
                    if not message_id.isdigit() or int(message_id) <= 0:
                        raise RuntimeError('lyric_failure_notice_send_failed')
                    with self.connect() as db:
                        db.execute('UPDATE lyric_batch_errors SET delivered_at=?,message_id=? WHERE request_key=? AND media_path=?',
                                   (self.clock(), message_id, batch['request_key'], error['media_path']))
                snapshot = self._snapshot(batch, notifications)
                done = not snapshot['collecting'] and not snapshot['remaining'] and not snapshot['notice_pending']
                if done:
                    if batch['message_id'] and not delete(batch['chat_id'], batch['message_id']):
                        raise RuntimeError('lyric_queue_delete_failed')
                    self._save(batch, state='deleted', last_error='', attempts=0)
                    actions.append('deleted' if batch['message_id'] else 'finished_without_queue_card')
                    continue
                text = self._text(snapshot)
                if not batch['message_id']:
                    result = send(batch['chat_id'], text)
                    message_id = str((result or {}).get('message_id') or '')
                    if not message_id.isdigit() or int(message_id) <= 0:
                        raise RuntimeError('lyric_queue_send_failed')
                    self._save(batch, message_id=message_id, sent_text=text, next_attempt_at=self.clock() + 5,
                               last_error='', attempts=0)
                    actions.append('sent')
                elif text != batch['sent_text']:
                    if not edit(batch['chat_id'], batch['message_id'], text):
                        raise RuntimeError('lyric_queue_edit_failed')
                    self._save(batch, sent_text=text, next_attempt_at=self.clock() + 5, last_error='', attempts=0)
                    actions.append('edited')
                else:
                    self._save(batch, next_attempt_at=self.clock() + 5, last_error='', attempts=0)
            except Exception as error:
                attempts = int(batch['attempts']) + 1
                self._save(batch, attempts=attempts, last_error=type(error).__name__,
                           next_attempt_at=self.clock() + min(300, 5 * 2 ** min(attempts - 1, 6)))
                actions.append('retry')
        return actions
