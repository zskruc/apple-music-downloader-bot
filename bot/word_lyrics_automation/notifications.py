from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


def notification_request_key(context: dict[str, Any] | None) -> str:
    """Identify one request/batch; callback IDs are identities, never delete IDs."""
    if not isinstance(context, dict) or context.get("source") != "telegram":
        return ""
    chat = str(context.get("chat_id") or "").strip()
    if not chat:
        return ""
    task = context.get("task_id")
    task_id = int(task) if (type(task) is int or isinstance(task,str) and len(task.strip()) <= 32 and task.strip().isdigit()) else 0
    if task_id > 0:
        origin = ["task", task_id]
    else:
        message = context.get("origin_message_id") or context.get("message_id")
        if not isinstance(message, (str, int)) or isinstance(message, bool) or not str(message).strip():
            return ""
        origin = ["origin", str(message).strip()]
    identity = ["telegram", chat, origin, str(context.get("batch_id") or "")]
    return "telegram:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def initialize_notification_tables(database: sqlite3.Connection) -> None:
    """Add durable subscriptions without deleting or rewriting legacy receipts."""
    for table in ("notification_deliveries", "source_wait_deliveries"):
        database.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            job_id INTEGER PRIMARY KEY, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
            lease_owner TEXT NOT NULL DEFAULT '', lease_token TEXT NOT NULL DEFAULT '',
            lease_expires_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            delivered_at REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE)""")
        database.execute(f"CREATE INDEX IF NOT EXISTS {table}_ready ON {table}(state,next_attempt_at,updated_at)")
    database.execute("""CREATE TABLE IF NOT EXISTS notification_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
        request_key TEXT NOT NULL, context_json TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(job_id,request_key), FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE)""")
    database.execute("CREATE INDEX IF NOT EXISTS notification_subscriptions_request ON notification_subscriptions(request_key,job_id)")
    database.execute("""CREATE TABLE IF NOT EXISTS notification_subscription_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('terminal','source_wait','consent')),
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        lease_owner TEXT NOT NULL DEFAULT '', lease_token TEXT NOT NULL DEFAULT '',
        lease_expires_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        delivered_at REAL NOT NULL DEFAULT 0,
        chat_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '',
        event_status TEXT NOT NULL DEFAULT '',
        UNIQUE(subscription_id,kind),
        FOREIGN KEY(subscription_id) REFERENCES notification_subscriptions(id) ON DELETE CASCADE)""")
    if "event_status" not in {row[1] for row in database.execute("PRAGMA table_info(notification_subscription_deliveries)")}:
        database.execute("ALTER TABLE notification_subscription_deliveries ADD COLUMN event_status TEXT NOT NULL DEFAULT ''")
    # Real send responses for legacy jobs are separate from their unchanged
    # legacy delivery rows. No Telegram message identifier is inferred.
    database.execute("""CREATE TABLE IF NOT EXISTS notification_legacy_message_receipts (
        job_id INTEGER NOT NULL, kind TEXT NOT NULL, chat_id TEXT NOT NULL,
        message_id TEXT NOT NULL, delivered_at REAL NOT NULL,
        PRIMARY KEY(job_id,kind), FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE)""")

    database.execute("""CREATE TABLE IF NOT EXISTS notification_lifecycle_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
        recipient_key TEXT NOT NULL, context_json TEXT NOT NULL, event_key TEXT NOT NULL,
        event_status TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0, lease_owner TEXT NOT NULL DEFAULT '',
        lease_token TEXT NOT NULL DEFAULT '', lease_expires_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
        delivered_at REAL NOT NULL DEFAULT 0, chat_id TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '',
        UNIQUE(job_id,recipient_key,event_key),
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE)""")


def _legacy_receipt_status(database: sqlite3.Connection, job_id: int, receipt: sqlite3.Row,
                           fallback: str) -> str:
    event = database.execute("""SELECT event FROM events WHERE job_id=? AND created_at<=?
        AND event IN ('mac_consent_requested','completed','failed') ORDER BY id DESC LIMIT 1""",
        (job_id,receipt["created_at"])).fetchone()
    return {"mac_consent_requested":"awaiting_mac_consent","completed":"completed","failed":"dead_letter"}.get(event[0] if event else "",fallback)


def register_notification_subscription(database: sqlite3.Connection, job_id: int,
        context: dict[str, Any] | None, now: float, *, legacy_origin: bool = False) -> int | None:
    """Called inside enqueue's transaction, including reused jobs."""
    key = notification_request_key(context)
    if not key and legacy_origin and isinstance(context, dict) and context.get("source") == "telegram" and context.get("chat_id"):
        key = "legacy:" + hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    if not key:
        return None
    already_registered = database.execute("SELECT id FROM notification_subscriptions WHERE job_id=? AND request_key=?", (job_id,key)).fetchone()
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    database.execute("""INSERT INTO notification_subscriptions(job_id,request_key,context_json,created_at,updated_at)
        VALUES(?,?,?,?,?) ON CONFLICT(job_id,request_key) DO UPDATE SET
        context_json=excluded.context_json,updated_at=excluded.updated_at""", (job_id,key,encoded,now,now))
    subscription = database.execute("SELECT id FROM notification_subscriptions WHERE job_id=? AND request_key=?", (job_id,key)).fetchone()
    subscription_id = int(subscription[0])
    if already_registered is not None:
        return subscription_id
    row = database.execute("SELECT metadata_json,status FROM jobs WHERE id=?", (job_id,)).fetchone()
    original = json.loads(row[0] or "{}").get("notification")
    # Only the original request can inherit a legacy receipt. Other chats or
    # tasks receive their own notice even if this acoustic job was reused.
    if original == context or (notification_request_key(original) and notification_request_key(original) == key):
        for table, kind in (("notification_deliveries", "terminal"),
                            ("source_wait_deliveries", "source_wait")):
            old = database.execute(f"SELECT * FROM {table} WHERE job_id=?", (job_id,)).fetchone()
            if old is not None and old["state"] in {"leased", "retry", "delivered", "inherited"}:
                event_status = "waiting_source" if kind == "source_wait" else _legacy_receipt_status(database,job_id,old,row[1])
                if event_status == "awaiting_mac_consent":
                    kind = "consent"
                database.execute("""INSERT OR IGNORE INTO notification_subscription_deliveries
                    (subscription_id,kind,state,attempts,next_attempt_at,lease_owner,lease_token,
                     lease_expires_at,last_error,created_at,updated_at,delivered_at,chat_id,event_status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (subscription_id,kind,
                    "delivered" if old["state"] == "inherited" else old["state"],
                    old["attempts"],old["next_attempt_at"],old["lease_owner"],old["lease_token"],
                    old["lease_expires_at"],old["last_error"],old["created_at"],now,old["delivered_at"],str(context.get("chat_id") or ""),event_status))
    return subscription_id


def copy_notification_subscriptions(database: sqlite3.Connection, old_job_id: int,
        new_job_id: int, now: float, *, inherit_wait: bool = False) -> None:
    old = database.execute("SELECT metadata_json FROM jobs WHERE id=?", (old_job_id,)).fetchone()
    if old is not None:
        register_notification_subscription(database, old_job_id,
            json.loads(old[0] or "{}").get("notification"), now, legacy_origin=True)
    for subscription in database.execute("SELECT id,context_json FROM notification_subscriptions WHERE job_id=?", (old_job_id,)).fetchall():
        if database.execute("SELECT 1 FROM notification_subscription_deliveries WHERE subscription_id=? AND kind='terminal' AND state='delivered'", (subscription[0],)).fetchone():
            # A different request must not reopen an already notified task.
            # An explicit retry by this same origin is registered separately
            # from the incoming context on the new job before this copy.
            continue
        context = json.loads(subscription[1])
        new_id = register_notification_subscription(database,new_job_id,context,now,legacy_origin=True)
        if inherit_wait and new_id:
            receipt = database.execute("SELECT attempts,created_at,delivered_at FROM notification_subscription_deliveries WHERE subscription_id=? AND kind='source_wait' AND state='delivered'", (subscription[0],)).fetchone()
            if receipt:
                database.execute("""INSERT OR IGNORE INTO notification_subscription_deliveries
                    (subscription_id,kind,state,attempts,created_at,updated_at,delivered_at,chat_id)
                    VALUES(?,'source_wait','delivered',?,?,?,?,?)""", (new_id,receipt[0],receipt[1],now,receipt[2],str(context.get("chat_id") or "")))


@dataclass(frozen=True, slots=True)
class WordLyricNotification:
    job_id: int
    terminal_status: str
    media_path: str
    metadata: dict[str, Any]
    result: dict[str, Any]
    last_error: str
    attempts: int
    lease_token: str
    next_attempt_at: float = 0
    payload: dict[str, Any] = field(default_factory=dict)
    subscription_id: int | None = None
    request_key: str = ""
    notification_kind: str = "terminal"

    @property
    def context(self) -> dict[str, Any]:
        value = self.metadata.get("notification")
        return value if isinstance(value, dict) else {}


class NotificationStore:
    """Crash-safe, exactly-once-ish delivery queue for lyric lifecycle messages.

    The acoustic pipeline deliberately has no Telegram credentials.  The bot
    observes terminal jobs in the shared durable queue and leases their
    notifications here.  A crash before acknowledgement merely expires the
    delivery lease; a successful send is persisted so normal restarts do not
    notify twice.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = database_path
        self.clock = clock
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database_path, timeout=30)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=30000")
        database.execute("PRAGMA foreign_keys=ON")
        return database

    def _initialize(self) -> None:
        with self.connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("BEGIN IMMEDIATE")
            initialize_notification_tables(database)

    @staticmethod
    def _decode(row: sqlite3.Row, token: str, *, subscription_id: int | None = None,
                request_key: str = "", context: dict[str, Any] | None = None,
                kind: str = "terminal") -> WordLyricNotification:
        metadata = json.loads(row["metadata_json"] or "{}")
        if context is not None:
            metadata["notification"] = context
        return WordLyricNotification(
            job_id=int(row["id"]),
            terminal_status=str(row["status"]),
            media_path=str(row["media_path"]),
            metadata=metadata,
            result=json.loads(row["result_json"] or "{}"),
            last_error=str(row["last_error"] or ""),
            attempts=int(row["attempts"]),
            lease_token=token,
            next_attempt_at=float(row["next_attempt_at"]),
            payload=json.loads(row["payload_json"] or "{}"),
            subscription_id=subscription_id, request_key=request_key,
            notification_kind=kind,
        )

    @staticmethod
    def _valid_context(metadata: dict[str, Any]) -> bool:
        context = metadata.get("notification")
        return bool(
            isinstance(context, dict)
            and str(context.get("source") or "") == "telegram"
            and str(context.get("chat_id") or "")
        )

    def claim(
        self,
        *,
        owner: str,
        lease_seconds: float = 300,
    ) -> WordLyricNotification | None:
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            for table in ("notification_deliveries", "source_wait_deliveries", "notification_subscription_deliveries", "notification_lifecycle_deliveries"):
                database.execute(
                    f"""
                UPDATE {table}
                SET state='retry', next_attempt_at=?, lease_owner='',
                    lease_token='', lease_expires_at=0, updated_at=?
                WHERE state='leased' AND lease_expires_at > 0
                  AND lease_expires_at <= ?
                    """,
                    (now, now, now),
                )
            lifecycle = self._claim_lifecycle(database, now, owner, lease_seconds)
            if lifecycle is not None:
                return lifecycle
            for kind, statuses in (("terminal", "'completed','dead_letter'"),
                                   ("consent", "'awaiting_mac_consent'"),
                                   ("source_wait", "'waiting_source'")):
                selected = database.execute(f"""SELECT j.*,s.id AS subscription_id,s.request_key,s.context_json
                    FROM jobs j JOIN notification_subscriptions s ON s.job_id=j.id
                    LEFT JOIN notification_subscription_deliveries d ON d.subscription_id=s.id AND d.kind=?
                    WHERE j.status IN ({statuses}) AND (d.id IS NULL OR (d.state='retry' AND d.next_attempt_at<=?))
                    ORDER BY j.updated_at,j.id,s.id LIMIT 1""", (kind,now)).fetchone()
                if selected is not None:
                    token = secrets.token_urlsafe(32)
                    database.execute("""INSERT INTO notification_subscription_deliveries
                        (subscription_id,kind,state,attempts,lease_owner,lease_token,lease_expires_at,created_at,updated_at,event_status)
                        VALUES(?,?,'leased',1,?,?,?,?,?,?) ON CONFLICT(subscription_id,kind) DO UPDATE SET
                        state='leased',attempts=notification_subscription_deliveries.attempts+1,
                        lease_owner=excluded.lease_owner,lease_token=excluded.lease_token,
                        lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at""",
                        (selected["subscription_id"],kind,owner,token,now+max(30.,lease_seconds),now,now,selected["status"]))
                    return self._decode(selected,token,subscription_id=selected["subscription_id"],
                        request_key=selected["request_key"],context=json.loads(selected["context_json"]),kind=kind)
            selected, table = self._select(database, now)
            if selected is None:
                return None
            job_id = int(selected["id"])
            token = secrets.token_urlsafe(32)
            database.execute(
                f"""
                INSERT INTO {table}(
                    job_id, state, attempts, lease_owner, lease_token,
                    lease_expires_at, created_at, updated_at
                ) VALUES(?, 'leased', 1, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    state='leased', attempts={table}.attempts+1,
                    lease_owner=excluded.lease_owner,
                    lease_token=excluded.lease_token,
                    lease_expires_at=excluded.lease_expires_at,
                    updated_at=excluded.updated_at
                """,
                (job_id, owner, token, now + max(30.0, lease_seconds), now, now),
            )
            return self._decode(selected, token, kind="source_wait" if table == "source_wait_deliveries" else "consent" if selected["status"] == "awaiting_mac_consent" else "terminal")

    def _claim_lifecycle(self, database, now, owner, lease_seconds):
        rows = database.execute("""SELECT * FROM jobs
            WHERE status NOT IN ('completed','dead_letter','superseded')
            ORDER BY updated_at,id LIMIT 1000""").fetchall()
        for row in rows:
            payload = json.loads(row['payload_json'] or '{}')
            state = payload.get('source_state') or {}
            recovered = payload.get('source_recovery') or {}
            if row['status'] == 'needs_attention':
                status = 'needs_attention'
                signature = state.get('signature') or hashlib.sha256(json.dumps(
                    {k:state.get(k) for k in ('category','code','provider','generation')},sort_keys=True).encode()).hexdigest()
                event_key = 'attention:' + str(signature)
            elif recovered.get('last_recovered_at') and row['status'] not in ('waiting_source','retry_source','leased_source'):
                status = 'source_recovered'
                event_key = 'recovered:' + str(recovered.get('recovery_generation', recovered['last_recovered_at']))
            else:
                continue
            subscriptions = database.execute('SELECT request_key,context_json FROM notification_subscriptions WHERE job_id=?',(row['id'],)).fetchall()
            contexts = [(s[0],json.loads(s[1])) for s in subscriptions]
            if not contexts:
                context=json.loads(row['metadata_json'] or '{}').get('notification') or {}
                if self._valid_context({'notification':context}):
                    contexts=[('legacy',context)]
            for recipient_key,context in contexts:
                receipt=database.execute('SELECT * FROM notification_lifecycle_deliveries WHERE job_id=? AND recipient_key=? AND event_key=?',
                    (row['id'],recipient_key,event_key)).fetchone()
                if receipt and not (receipt['state']=='retry' and receipt['next_attempt_at']<=now):continue
                token=secrets.token_urlsafe(32)
                database.execute("""INSERT INTO notification_lifecycle_deliveries
                    (job_id,recipient_key,context_json,event_key,event_status,state,attempts,lease_owner,lease_token,lease_expires_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,'leased',1,?,?,?,?,?) ON CONFLICT(job_id,recipient_key,event_key) DO UPDATE SET
                    state='leased',attempts=notification_lifecycle_deliveries.attempts+1,
                    lease_owner=excluded.lease_owner,lease_token=excluded.lease_token,
                    lease_expires_at=excluded.lease_expires_at,updated_at=excluded.updated_at""",
                    (row['id'],recipient_key,json.dumps(context),event_key,status,owner,token,now+max(30.,lease_seconds),now,now))
                decoded=self._decode(row,token,request_key=recipient_key,context=context,kind='lifecycle')
                from dataclasses import replace
                return replace(decoded,terminal_status=status)
        return None

    def _select(self, database: sqlite3.Connection, now: float) -> tuple[sqlite3.Row | None, str]:
        for table, statuses in (
            ("notification_deliveries", "'completed', 'dead_letter', 'awaiting_mac_consent'"),
            ("source_wait_deliveries", "'waiting_source'"),
        ):
            rows = database.execute(
                f"""
                SELECT j.*
                FROM jobs AS j
                LEFT JOIN {table} AS d ON d.job_id=j.id
                WHERE j.status IN ({statuses})
                  AND NOT EXISTS (SELECT 1 FROM notification_subscriptions s WHERE s.job_id=j.id)
                  AND (
                    d.job_id IS NULL OR
                    (d.state='retry' AND d.next_attempt_at <= ?)
                  )
                ORDER BY j.updated_at, j.id
                LIMIT 100
                """,
                (now,),
            ).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"] or "{}")
                if self._valid_context(metadata):
                    return row, table
                database.execute(
                    f"""
                    INSERT INTO {table}(
                        job_id, state, created_at, updated_at
                    ) VALUES(?, 'skipped', ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        state='skipped', updated_at=excluded.updated_at
                    """,
                    (int(row["id"]), now, now),
                )
        return None, ""

    @staticmethod
    def _leased_table(database: sqlite3.Connection, job_id: int, token: str) -> str:
        for table in ("notification_deliveries", "source_wait_deliveries"):
            if database.execute(
                f"SELECT 1 FROM {table} WHERE job_id=? AND state='leased' AND lease_token=?",
                (job_id, token),
            ).fetchone():
                return table
        raise RuntimeError("notification lease is absent or expired")

    def delivered(self, job_id: int, token: str, *, message_id: int | str | None = None,
                  chat_id: int | str | None = None) -> None:
        if message_id is not None:
            if (isinstance(message_id,bool) or not isinstance(message_id,(int,str))
                    or not str(message_id).isdigit() or len(str(message_id)) > 32 or int(message_id) <= 0):
                raise ValueError("notification message_id must be a real positive Telegram identifier")
            message_id = str(int(message_id))
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            lifecycle=database.execute("SELECT * FROM notification_lifecycle_deliveries WHERE job_id=? AND state='leased' AND lease_token=?",(job_id,token)).fetchone()
            if lifecycle:
                expected_chat=str(json.loads(lifecycle['context_json']).get('chat_id') or '')
                if chat_id is not None and str(chat_id)!=expected_chat:
                    raise ValueError('notification response chat does not match its request')
                database.execute("""UPDATE notification_lifecycle_deliveries SET state='delivered',
                    lease_owner='',lease_token='',lease_expires_at=0,last_error='',delivered_at=?,updated_at=?,chat_id=?,message_id=? WHERE id=?""",
                    (now,now,expected_chat,str(message_id or ''),lifecycle['id']))
                return
            subscribed = database.execute("""SELECT d.id,s.context_json,d.kind FROM notification_subscription_deliveries d
                JOIN notification_subscriptions s ON s.id=d.subscription_id
                WHERE s.job_id=? AND d.state='leased' AND d.lease_token=?""", (job_id,token)).fetchone()
            if subscribed:
                expected_chat = str(json.loads(subscribed[1]).get("chat_id") or "")
                actual_chat = str(chat_id) if chat_id is not None else expected_chat
                if actual_chat != expected_chat:
                    raise ValueError("notification response chat does not match its subscription")
                database.execute("""UPDATE notification_subscription_deliveries SET state='delivered',
                    lease_owner='',lease_token='',lease_expires_at=0,last_error='',
                    delivered_at=?,updated_at=?,chat_id=?,message_id=? WHERE id=?""",
                    (now,now,actual_chat,str(message_id) if message_id is not None else "",subscribed[0]))
                legacy_table = "source_wait_deliveries" if subscribed[2] == "source_wait" else "notification_deliveries"
                database.execute(f"""UPDATE {legacy_table} SET state='delivered',lease_owner='',lease_token='',
                    lease_expires_at=0,last_error='',delivered_at=?,updated_at=?
                    WHERE job_id=? AND state='leased' AND lease_token=?""",(now,now,job_id,token))
                return
            table = self._leased_table(database, job_id, token)
            cursor = database.execute(
                f"""
                UPDATE {table}
                SET state='delivered', lease_owner='', lease_token='',
                    lease_expires_at=0, last_error='', delivered_at=?, updated_at=?
                WHERE job_id=? AND state='leased' AND lease_token=?
                """,
                (now, now, job_id, token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("notification lease is absent or expired")
            if message_id is not None:
                row = database.execute("SELECT metadata_json,status FROM jobs WHERE id=?", (job_id,)).fetchone()
                context = json.loads(row[0] or "{}").get("notification") or {}
                expected_chat = str(context.get("chat_id") or "")
                actual_chat = str(chat_id) if chat_id is not None else expected_chat
                if actual_chat != expected_chat:
                    raise ValueError("notification response chat does not match its request")
                kind = "source_wait" if table == "source_wait_deliveries" else "consent" if row[1] == "awaiting_mac_consent" else "terminal"
                database.execute("INSERT OR REPLACE INTO notification_legacy_message_receipts VALUES(?,?,?,?,?)", (job_id,kind,actual_chat,str(message_id),now))

    def failed(
        self,
        job_id: int,
        token: str,
        error: str,
        *,
        maximum_delay_seconds: float = 1800,
    ) -> None:
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            lifecycle=database.execute("SELECT * FROM notification_lifecycle_deliveries WHERE job_id=? AND state='leased' AND lease_token=?",(job_id,token)).fetchone()
            if lifecycle:
                delay=min(maximum_delay_seconds,30*2**min(30,max(0,lifecycle['attempts']-1)))
                database.execute("""UPDATE notification_lifecycle_deliveries SET state='retry',next_attempt_at=?,
                    lease_owner='',lease_token='',lease_expires_at=0,last_error=?,updated_at=? WHERE id=?""",
                    (now+delay,str(error)[:1000],now,lifecycle['id']))
                return
            subscribed = database.execute("""SELECT d.id,d.attempts,d.kind FROM notification_subscription_deliveries d
                JOIN notification_subscriptions s ON s.id=d.subscription_id
                WHERE s.job_id=? AND d.state='leased' AND d.lease_token=?""", (job_id,token)).fetchone()
            if subscribed:
                delay = min(maximum_delay_seconds,30 * 2 ** min(30,max(0,int(subscribed[1])-1)))
                database.execute("""UPDATE notification_subscription_deliveries SET state='retry',
                    next_attempt_at=?,lease_owner='',lease_token='',lease_expires_at=0,last_error=?,updated_at=? WHERE id=?""",
                    (now+delay,str(error)[:1000],now,subscribed[0]))
                legacy_table = "source_wait_deliveries" if subscribed[2] == "source_wait" else "notification_deliveries"
                database.execute(f"""UPDATE {legacy_table} SET state='retry',next_attempt_at=?,lease_owner='',
                    lease_token='',lease_expires_at=0,last_error=?,updated_at=?
                    WHERE job_id=? AND state='leased' AND lease_token=?""",(now+delay,str(error)[:1000],now,job_id,token))
                return
            table = self._leased_table(database, job_id, token)
            row = database.execute(
                f"""
                SELECT attempts FROM {table}
                WHERE job_id=? AND state='leased' AND lease_token=?
                """,
                (job_id, token),
            ).fetchone()
            if row is None:
                raise RuntimeError("notification lease is absent or expired")
            delay = min(maximum_delay_seconds, 30 * 2 ** min(30,max(0, int(row["attempts"]) - 1)))
            database.execute(
                f"""
                UPDATE {table}
                SET state='retry', next_attempt_at=?, lease_owner='',
                    lease_token='', lease_expires_at=0, last_error=?, updated_at=?
                WHERE job_id=?
                """,
                (now + delay, str(error)[:1000], now, job_id),
            )

    def stats(self) -> dict[str, int | str]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT state, COUNT(*) AS count FROM notification_deliveries d WHERE NOT EXISTS (SELECT 1 FROM notification_subscriptions s WHERE s.job_id=d.job_id) GROUP BY state"
            ).fetchall()
            quick_check = str(database.execute("PRAGMA quick_check").fetchone()[0])
            waits = database.execute("SELECT state, COUNT(*) AS count FROM source_wait_deliveries d WHERE NOT EXISTS (SELECT 1 FROM notification_subscriptions s WHERE s.job_id=d.job_id) GROUP BY state").fetchall()
            subscribed = database.execute("SELECT kind,state,COUNT(*) FROM notification_subscription_deliveries GROUP BY kind,state").fetchall()
        result: dict[str,int | str] = {"quick_check":quick_check}
        for prefix,values in (("",rows),("source_wait_",waits)):
            for row in values:
                result[prefix+str(row[0])] = int(row[1])
        for kind,state,count in subscribed:
            key = ("" if kind == "terminal" else kind+"_") + state
            result[key] = int(result.get(key,0)) + int(count)
        return result

    def request_jobs(self, request_keys: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Batch queue-card view, scoped to exact request identities, not chats.

        A replacement job inherits subscriptions in the same enqueue transaction.
        Only the newest subscribed job for each media path appears here. Work
        completion and acknowledgement are deliberately separate facts.
        """
        keys = sorted({key for key in request_keys if isinstance(key,str) and key})
        output: dict[str,list[dict[str,Any]]] = {key:[] for key in keys}
        if not keys:
            return output
        with self.connect() as database:
            database.execute("BEGIN")
            # SQLite parameter limits differ across installed versions.
            for start in range(0,len(keys),400):
                batch = keys[start:start+400]
                rows = database.execute("""SELECT j.*,s.id AS subscription_id,s.request_key,s.context_json,
                    d.state AS notice_state,d.chat_id AS notice_chat_id,d.message_id AS notice_message_id,
                    d.delivered_at AS notice_delivered_at,d.event_status AS notice_event_status
                    FROM notification_subscriptions s JOIN jobs j ON j.id=s.job_id
                    LEFT JOIN notification_subscription_deliveries d ON d.subscription_id=s.id AND d.kind='terminal'
                    WHERE s.request_key IN (""" + ",".join("?" for _ in batch) + ") ORDER BY j.id DESC,s.id DESC",batch).fetchall()
                seen: set[tuple[str,str]] = set()
                for row in rows:
                    identity = (row["request_key"],row["media_path"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    metadata = json.loads(row["metadata_json"] or "{}")
                    status = str(row["status"])
                    raw_status = status
                    if status == "superseded" and row["notice_state"] == "delivered" and row["notice_event_status"] in {"completed","dead_letter"}:
                        status = row["notice_event_status"]
                    completed,failed = status == "completed",status == "dead_letter"
                    delivered = row["notice_state"] == "delivered"
                    output[row["request_key"]].append({
                        "job_id":int(row["id"]),"subscription_id":int(row["subscription_id"]),
                        "request_key":row["request_key"],"media_path":row["media_path"],
                        "title":str(metadata.get("title") or ""),"artist":str(metadata.get("artist") or ""),
                        "status":status,"job_status":raw_status,"pending":not (completed or failed),
                        "completed":completed,"failed":failed,
                        "needs_notice":(completed or failed) and not delivered,
                        "terminal_delivered":delivered,
                        "notice_message_id":row["notice_message_id"] or "",
                        "notice_chat_id":row["notice_chat_id"] or "",
                        "notice_delivered_at":row["notice_delivered_at"] or 0,
                        "last_error":str(row["last_error"] or ""),
                    })
        return output
