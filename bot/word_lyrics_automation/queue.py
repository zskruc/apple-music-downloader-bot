from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .fingerprint import MP4_AUDIO_IDENTITY_METHOD, media_fingerprint, mp4_audio_identity
from .notifications import (initialize_notification_tables, register_notification_subscription,
                            copy_notification_subscriptions)
from .text import normalize_text


SCHEMA_VERSION = 1
TERMINAL_STATUSES = {"completed", "dead_letter", "superseded"}
LEASED_TO_RETRY = {
    "leased_source": "retry_source",
    "leased_alignment": "retry_alignment",
    "leased_mac_alignment": "retry_mac_alignment",
    "leased_validation": "retry_validation",
    "leased_scan": "retry_scan",
}
STAGE_ELIGIBLE = {
    "source": ("queued_source", "retry_source", "waiting_source"),
    "alignment": ("needs_alignment", "retry_alignment"),
    "mac_alignment": ("needs_mac_alignment", "retry_mac_alignment"),
    "validation": ("candidate_pending", "retry_validation"),
    "scan": ("scan_pending", "retry_scan"),
}
STAGE_LEASED = {stage: f"leased_{stage}" for stage in STAGE_ELIGIBLE}


@dataclass(frozen=True, slots=True)
class QueueJob:
    id: int
    media_path: str
    fingerprint: str
    fingerprint_data: dict[str, Any]
    source_url: str
    metadata: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: float
    lease_owner: str
    lease_token: str
    lease_expires_at: float
    payload: dict[str, Any]
    result: dict[str, Any]
    last_error: str
    created_at: float
    updated_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "media_path": self.media_path,
            "fingerprint": self.fingerprint,
            "fingerprint_data": self.fingerprint_data,
            "source_url": self.source_url,
            "metadata": self.metadata,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_attempt_at": self.next_attempt_at,
            "lease_owner": self.lease_owner,
            "lease_token": self.lease_token,
            "lease_expires_at": self.lease_expires_at,
            "payload": self.payload,
            "result": self.result,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job: QueueJob
    created: bool


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint_key(value: dict[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class QueueStore:
    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
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
            database.execute("PRAGMA synchronous=FULL")
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS queue_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    fingerprint_json TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 8,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(media_path, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS jobs_status_ready
                    ON jobs(status, next_attempt_at, created_at);
                CREATE INDEX IF NOT EXISTS jobs_media_path
                    ON jobs(media_path, updated_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_job_id ON events(job_id, id);
                """
            )
            database.execute(
                "INSERT OR REPLACE INTO queue_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            initialize_notification_tables(database)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> QueueJob:
        return QueueJob(
            id=int(row["id"]),
            media_path=str(row["media_path"]),
            fingerprint=str(row["fingerprint"]),
            fingerprint_data=json.loads(row["fingerprint_json"] or "{}"),
            source_url=str(row["source_url"] or ""),
            metadata=json.loads(row["metadata_json"] or "{}"),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            lease_owner=str(row["lease_owner"] or ""),
            lease_token=str(row["lease_token"] or ""),
            lease_expires_at=float(row["lease_expires_at"]),
            payload=json.loads(row["payload_json"] or "{}"),
            result=json.loads(row["result_json"] or "{}"),
            last_error=str(row["last_error"] or ""),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def _event(
        self,
        database: sqlite3.Connection,
        job_id: int,
        event: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        database.execute(
            "INSERT INTO events(job_id, event, detail_json, created_at) VALUES(?, ?, ?, ?)",
            (job_id, event, _json(detail or {}), self.clock()),
        )

    @staticmethod
    def _stage_attempts(database: sqlite3.Connection, row: sqlite3.Row, stage: str) -> int:
        # Keep jobs.attempts compatible with existing progress consumers. Only
        # source waiting resets that counter; later stages have independent
        # budgets reconstructed from the already-durable claim events. This
        # also makes old jobs stranded by a cumulative limit runnable again.
        if stage == "source":
            return int(row["attempts"])
        claims = database.execute(
            "SELECT detail_json FROM events WHERE job_id=? AND event='claimed'",
            (int(row["id"]),),
        ).fetchall()
        return sum(json.loads(claim["detail_json"]).get("stage") == stage for claim in claims)

    @staticmethod
    def _stage_limit(row: sqlite3.Row, stage: str, attempts: int) -> int:
        maximum = int(row["max_attempts"])
        if stage == "mac_alignment":
            # Consent historically grants at least three remaining claims by
            # extending the cumulative maximum. Preserve that exact grant.
            maximum -= max(0, int(row["attempts"]) - attempts)
        return max(1, maximum)

    def _exhausted_retry(
        self, database: sqlite3.Connection, row: sqlite3.Row, *, stage: str, error: str,
    ) -> str:
        """Finish an exhausted retry explicitly, rather than leaving it active."""
        job_id = int(row["id"])
        now = self.clock()
        payload = json.loads(row["payload_json"] or "{}")
        if (stage == "source" and not normalize_text(str(payload.get("reference_text") or ""))
                and not payload.get("reference_lines")):
            # Discovery with no trusted text keeps its existing durable slow
            # recheck policy, including when the final worker lease crashed.
            history = dict(payload.get("source_recovery") or {})
            history.update(wait_cycles=int(history.get("wait_cycles") or 0) + 1,
                total_claims=int(history.get("total_claims") or 0) + int(row["attempts"]),
                first_wait_at=history.get("first_wait_at") or now,
                last_wait_at=now, last_error=error)
            payload["source_recovery"] = history
            next_at = now + 21600
            database.execute("""UPDATE jobs SET status='waiting_source', attempts=0,
                next_attempt_at=?, lease_owner='', lease_token='', lease_expires_at=0,
                last_error=?, payload_json=?, updated_at=? WHERE id=?""",
                (next_at, error, _json(payload), now, job_id))
            self._event(database, job_id, "source_recheck_scheduled", {
                "error": error, "next_attempt_at": next_at, "wait_cycles": history["wait_cycles"]})
            return "waiting_source"
        database.execute("""UPDATE jobs SET status='dead_letter', next_attempt_at=0,
            lease_owner='', lease_token='', lease_expires_at=0, last_error=?, updated_at=? WHERE id=?""",
            (error, now, job_id))
        self._event(database, job_id, "failed", {"stage": stage, "error": error, "next_attempt_at": 0})
        return "dead_letter"

    def enqueue(
        self,
        media_path: Path,
        *,
        source_url: str = "",
        metadata: dict[str, Any] | None = None,
        initial_status: str = "",
        initial_reason: str = "",
        max_attempts: int = 8,
        fingerprint_data: dict[str, Any] | None = None,
        force: bool = False,
    ) -> EnqueueResult:
        media_path = media_path.resolve(strict=True)
        fingerprint_data = fingerprint_data or media_fingerprint(media_path)
        fingerprint = _fingerprint_key(fingerprint_data)
        audio_identity = mp4_audio_identity(media_path)
        now = self.clock()
        metadata = dict(metadata or {})
        metadata.setdefault("initial_status", initial_status)
        metadata.setdefault("initial_reason", initial_reason)
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                "SELECT * FROM jobs WHERE media_path=? AND (fingerprint=? OR fingerprint_json=?) ORDER BY id DESC LIMIT 1",
                (str(media_path), fingerprint, _json(fingerprint_data)),
            ).fetchone()
            if existing is not None and not force:
                original_metadata = json.loads(existing["metadata_json"] or "{}")
                original_context = original_metadata.get("notification")
                incoming_context = metadata.get("notification")
                if original_context:
                    register_notification_subscription(database,int(existing["id"]),original_context,now,
                        legacy_origin=bool(incoming_context))
                register_notification_subscription(database,int(existing["id"]),incoming_context,now)
                if original_context:
                    metadata["notification"] = original_context
                database.execute(
                    """
                    UPDATE jobs SET
                        source_url=CASE WHEN ? <> '' THEN ? ELSE source_url END,
                        metadata_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (source_url, source_url, _json(metadata), now, int(existing["id"])),
                )
                row = database.execute(
                    "SELECT * FROM jobs WHERE id=?", (int(existing["id"]),)
                ).fetchone()
                assert row is not None
                return EnqueueResult(self._row_to_job(row), False)
            if existing is not None:
                register_notification_subscription(database,int(existing["id"]),
                    json.loads(existing["metadata_json"] or "{}").get("notification"),now,legacy_origin=True)
                database.execute(
                    """
                    UPDATE jobs SET status='superseded', lease_owner='', lease_token='',
                        lease_expires_at=0, updated_at=? WHERE id=?
                    """,
                    (now, int(existing["id"])),
                )
                self._event(database, int(existing["id"]), "forced_reenqueue")
                # The nonce exists only to make a new idempotency key.  It is
                # not part of the immutable media fingerprint: storing it in
                # fingerprint_json makes later byte-for-byte validation report
                # a false audio_changed_since_enqueue failure.
                fingerprint_key_data = dict(fingerprint_data)
                fingerprint_key_data["forced_nonce"] = secrets.token_hex(8)
                fingerprint = _fingerprint_key(fingerprint_key_data)
            active_rows = database.execute(
                "SELECT * FROM jobs WHERE media_path=? AND status NOT IN ('completed','dead_letter','superseded') ORDER BY id DESC",
                (str(media_path),),
            ).fetchall()
            source_progress = None
            if audio_identity and not force:
                for active in active_rows:
                    previous = self._row_to_job(active)
                    if (previous.status in {"queued_source", "retry_source", "waiting_source"}
                            and previous.payload.get("audio_identity") == audio_identity
                            and not normalize_text(str(previous.payload.get("reference_text") or ""))
                            and not previous.payload.get("reference_lines")
                            and all(previous.metadata.get(key) == metadata.get(key) for key in
                                    ("title", "artist", "album", "duration", "song_id", "isrc"))):
                        source_progress = previous
                        break
            for active in active_rows:
                active_id = int(active["id"])
                register_notification_subscription(database,active_id,
                    json.loads(active["metadata_json"] or "{}").get("notification"),now,legacy_origin=True)
                if (active["status"] in {"scan_pending", "retry_scan", "leased_scan"}
                        and json.loads(active["fingerprint_json"]) != fingerprint_data):
                    publication = json.loads(active["result_json"] or "{}")
                    sidecar_path = str(publication.get("sidecar_path") or "")
                    candidate_sha256 = str(publication.get("candidate_sha256") or "")
                    allowed_sidecars = {media_path.with_suffix(suffix)
                        for suffix in (".ttml", ".lrc", ".elrc")}
                    if sidecar_path and candidate_sha256 and Path(sidecar_path).resolve() in allowed_sidecars:
                        from .resolver import _rollback_sidecar

                        # A crashed scan may have left a derived character
                        # lyric. Retract its verified layer before retracting
                        # the original publication; the replacement source job
                        # must never inherit either old recording's candidate.
                        intent = publication.get("character_adaptation_intent") or {}
                        if (isinstance(intent, dict)
                                and intent.get("sidecar_path") == sidecar_path
                                and intent.get("source_candidate_sha256") == candidate_sha256
                                and intent.get("backup_path") and intent.get("candidate_sha256")):
                            backup = Path(intent["backup_path"])
                            if (backup.is_file() and hashlib.sha256(backup.read_bytes()).hexdigest()
                                    == candidate_sha256):
                                _rollback_sidecar(sidecar_path, str(backup), intent["candidate_sha256"])
                        if candidate_sha256 != publication.get("existing_sidecar_sha256"):
                            _rollback_sidecar(sidecar_path, publication.get("backup_path", ""), candidate_sha256)
                database.execute(
                    """
                    UPDATE jobs SET status='superseded', lease_owner='', lease_token='',
                        lease_expires_at=0, updated_at=? WHERE id=?
                    """,
                    (now, active_id),
                )
                self._event(database, active_id, "superseded_by_new_media")
            cursor = database.execute(
                """
                INSERT INTO jobs(
                    media_path, fingerprint, fingerprint_json, source_url,
                    metadata_json, status, max_attempts, next_attempt_at,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'queued_source', ?, ?, ?, ?)
                """,
                (
                    str(media_path),
                    fingerprint,
                    _json(fingerprint_data),
                    source_url,
                    _json(metadata),
                    max(1, max_attempts),
                    now,
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid)
            register_notification_subscription(database,job_id,metadata.get("notification"),now)
            predecessors = {int(active["id"]) for active in active_rows}
            if existing is not None:
                predecessors.add(int(existing["id"]))
            for previous_id in predecessors:
                copy_notification_subscriptions(database,previous_id,job_id,now,
                    inherit_wait=source_progress is not None and previous_id == source_progress.id)
            payload = {"audio_identity": audio_identity} if audio_identity else {}
            if source_progress is not None:
                payload.update(source_progress.payload)
                payload.update(audio_identity=audio_identity, source_fingerprint=fingerprint_data,
                               source_history_from_job=source_progress.id,
                               workflow_created_at=source_progress.payload.get("workflow_created_at")
                                   or source_progress.created_at)
                database.execute("""UPDATE jobs SET status=?, attempts=?, next_attempt_at=?,
                    payload_json=?, last_error=? WHERE id=?""",
                    (source_progress.status, source_progress.attempts, source_progress.next_attempt_at,
                     _json(payload), source_progress.last_error, job_id))
                self._event(database, job_id, "tag_only_source_progress_preserved", {
                    "previous_job_id": source_progress.id, "status": source_progress.status,
                    "next_attempt_at": source_progress.next_attempt_at})
                if (source_progress.metadata.get("notification") == metadata.get("notification")
                        and database.execute("SELECT 1 FROM sqlite_master WHERE name='source_wait_deliveries'").fetchone()):
                    database.execute("""INSERT INTO source_wait_deliveries(
                        job_id,state,attempts,created_at,updated_at,delivered_at)
                        SELECT ?, 'inherited', attempts, created_at, ?, delivered_at
                        FROM source_wait_deliveries WHERE job_id=? AND state IN ('delivered','inherited')""",
                        (job_id, now, source_progress.id))
            elif payload:
                database.execute("UPDATE jobs SET payload_json=? WHERE id=?", (_json(payload), job_id))
            self._event(database, job_id, "enqueued", {"source_url": source_url})
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return EnqueueResult(self._row_to_job(row), True)

    def reclaim_expired(self, database: sqlite3.Connection | None = None) -> int:
        own_connection = database is None
        connection = database or self.connect()
        now = self.clock()
        try:
            if own_connection:
                connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status LIKE 'leased_%' AND lease_expires_at > 0 AND lease_expires_at <= ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                retry_status = LEASED_TO_RETRY.get(str(row["status"]), "retry_source")
                stage = retry_status.removeprefix("retry_")
                attempts = self._stage_attempts(connection, row, stage)
                if attempts >= self._stage_limit(row, stage, attempts):
                    status = self._exhausted_retry(connection, row, stage=stage,
                        error="lease_expired_retry_budget_exhausted")
                    self._event(connection, job_id, "lease_reclaimed", {"status": status})
                    continue
                connection.execute(
                    """
                    UPDATE jobs SET status=?, next_attempt_at=?, lease_owner='',
                        lease_token='', lease_expires_at=0,
                        last_error='lease_expired', updated_at=? WHERE id=?
                    """,
                    (retry_status, now, now, job_id),
                )
                self._event(connection, job_id, "lease_reclaimed", {"status": retry_status})
            if own_connection:
                connection.commit()
            return len(rows)
        finally:
            if own_connection:
                connection.close()

    def claim(
        self,
        stage: str,
        *,
        owner: str,
        lease_seconds: float = 2700,
    ) -> QueueJob | None:
        if stage not in STAGE_ELIGIBLE:
            raise ValueError(f"unsupported queue stage: {stage}")
        now = self.clock()
        statuses = STAGE_ELIGIBLE[stage]
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self.reclaim_expired(database)
            while True:
                row = database.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE status IN ({placeholders}) AND next_attempt_at <= ?
                    ORDER BY next_attempt_at, created_at, id LIMIT 1
                    """,
                    (*statuses, now),
                ).fetchone()
                if row is None:
                    return None
                attempts = self._stage_attempts(database, row, stage)
                if attempts < self._stage_limit(row, stage, attempts):
                    break
                self._exhausted_retry(database, row, stage=stage, error="retry_budget_exhausted")
            job_id = int(row["id"])
            token = secrets.token_urlsafe(32)
            leased_status = STAGE_LEASED[stage]
            database.execute(
                """
                UPDATE jobs SET status=?, attempts=attempts+1, lease_owner=?,
                    lease_token=?, lease_expires_at=?, updated_at=? WHERE id=?
                """,
                (
                    leased_status,
                    owner,
                    token,
                    now + max(30.0, lease_seconds),
                    now,
                    job_id,
                ),
            )
            self._event(database, job_id, "claimed", {"stage": stage, "owner": owner})
            claimed = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert claimed is not None
            return self._row_to_job(claimed)

    def _leased_row(
        self,
        database: sqlite3.Connection,
        job_id: int,
        token: str,
        *,
        expected: Iterable[str] | None = None,
    ) -> sqlite3.Row:
        row = database.execute(
            "SELECT * FROM jobs WHERE id=? AND lease_token=?",
            (job_id, token),
        ).fetchone()
        if row is None:
            raise RuntimeError("job lease is absent or no longer owned by this worker")
        if float(row["lease_expires_at"] or 0) <= self.clock():
            raise RuntimeError("job lease has expired")
        if expected is not None and str(row["status"]) not in set(expected):
            raise RuntimeError(f"job has unexpected status: {row['status']}")
        return row

    def heartbeat(
        self,
        job_id: int,
        token: str,
        *,
        lease_seconds: float = 2700,
    ) -> QueueJob:
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._leased_row(database, job_id, token)
            database.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE id=?",
                (now + max(30.0, lease_seconds), now, job_id),
            )
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return self._row_to_job(row)

    def defer_to_alignment(
        self,
        job_id: int,
        token: str,
        *,
        payload: dict[str, Any],
        reason: str,
    ) -> QueueJob:
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._leased_row(database, job_id, token, expected={"leased_source"})
            database.execute(
                """
                UPDATE jobs SET status='needs_alignment', payload_json=?,
                    result_json=?, next_attempt_at=?, lease_owner='', lease_token='',
                    lease_expires_at=0, last_error='', updated_at=? WHERE id=?
                """,
                (_json(payload), _json({"source_resolution": reason}), now, now, job_id),
            )
            self._event(database, job_id, "needs_alignment", {"reason": reason})
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return self._row_to_job(row)

    def submit_candidate(
        self,
        job_id: int,
        token: str,
        *,
        candidate_path: Path,
        report: dict[str, Any],
        stage: str = "alignment",
    ) -> QueueJob:
        if stage not in {"alignment", "mac_alignment"}:
            raise ValueError(f"unsupported candidate stage: {stage}")
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = self._leased_row(
                database,
                job_id,
                token,
                expected={STAGE_LEASED[stage]},
            )
            payload = json.loads(row["payload_json"] or "{}")
            payload.update(
                {
                    "candidate_path": str(candidate_path),
                    "candidate_report": report,
                }
            )
            database.execute(
                """
                UPDATE jobs SET status='candidate_pending', payload_json=?,
                    next_attempt_at=?, lease_owner='', lease_token='',
                    lease_expires_at=0, last_error='', updated_at=? WHERE id=?
                """,
                (_json(payload), now, now, job_id),
            )
            self._event(
                database,
                job_id,
                "candidate_submitted",
                {"stage": stage, **report},
            )
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return self._row_to_job(row)

    def transition_to_scan(
        self,
        job_id: int,
        token: str,
        *,
        result: dict[str, Any],
        expected_statuses: Iterable[str],
    ) -> QueueJob:
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._leased_row(database, job_id, token, expected=set(expected_statuses))
            database.execute(
                """
                UPDATE jobs SET status='scan_pending', result_json=?, next_attempt_at=?,
                    lease_owner='', lease_token='', lease_expires_at=0,
                    last_error='', updated_at=? WHERE id=?
                """,
                (_json(result), now, now, job_id),
            )
            self._event(database, job_id, "sidecar_published", result)
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return self._row_to_job(row)

    def checkpoint_scan_result(
        self,
        job_id: int,
        token: str,
        *,
        result: dict[str, Any],
    ) -> QueueJob:
        """Durably retain publication recovery intent under the current scan lease."""
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._leased_row(database, job_id, token, expected={"leased_scan"})
            database.execute(
                "UPDATE jobs SET result_json=?, updated_at=? WHERE id=?",
                (_json(result), now, job_id),
            )
            self._event(database, job_id, "scan_publication_checkpoint", result)
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return self._row_to_job(row)

    def complete(
        self,
        job_id: int,
        token: str,
        *,
        result: dict[str, Any],
        expected_statuses: Iterable[str],
    ) -> QueueJob:
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._leased_row(database, job_id, token, expected=set(expected_statuses))
            database.execute(
                """
                UPDATE jobs SET status='completed', result_json=?, lease_owner='',
                    lease_token='', lease_expires_at=0, last_error='', updated_at=?
                WHERE id=?
                """,
                (_json(result), now, job_id),
            )
            self._event(database, job_id, "completed", result)
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert row is not None
            return self._row_to_job(row)

    def wait_for_source(self, job_id: int, token: str, *, error: str,
                        diagnostic_payload: dict[str, Any], delay_seconds: float = 21600) -> QueueJob:
        """Persist a slow source recheck without consuming acoustic retry limits.

        A source can appear tomorrow. Keep the job nonterminal and retry it in
        the existing worker, with one source cycle every six hours after the
        initial attempts. Every cycle and diagnostic survives process restarts.
        """
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = self._leased_row(database, job_id, token, expected={"leased_source"})
            previous = json.loads(row["payload_json"] or "{}")
            history = dict(previous.get("source_recovery") or {})
            history.update(wait_cycles=int(history.get("wait_cycles") or 0) + 1,
                           total_claims=int(history.get("total_claims") or 0) + int(row["attempts"]),
                           first_wait_at=history.get("first_wait_at") or now,
                           last_wait_at=now, last_error=str(error)[:2000])
            payload = {**diagnostic_payload, "source_recovery": history}
            next_at = now + max(3600, float(delay_seconds))
            database.execute("""UPDATE jobs SET status='waiting_source', attempts=0,
                next_attempt_at=?, lease_owner='', lease_token='', lease_expires_at=0,
                last_error=?, payload_json=?, updated_at=? WHERE id=?""",
                (next_at, str(error)[:2000], _json(payload), now, job_id))
            self._event(database, job_id, "source_recheck_scheduled", {"error": str(error)[:2000],
                "next_attempt_at": next_at, "wait_cycles": history["wait_cycles"]})
            return self._row_to_job(database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def recover_missing_source_jobs(self) -> int:
        """Once, resume legacy missing-reference failures in the normal worker."""
        now = self.clock()
        recovered = 0
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute("""SELECT * FROM jobs WHERE status='dead_letter'
                AND last_error='no_trusted_lyric_reference' AND NOT EXISTS (
                  SELECT 1 FROM jobs newer WHERE newer.media_path=jobs.media_path AND newer.id>jobs.id)
                AND NOT EXISTS (SELECT 1 FROM notification_subscriptions s WHERE s.job_id=jobs.id)
                """).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"] or "{}")
                if normalize_text(str(payload.get("reference_text") or "")):
                    continue
                payload["source_recovery"] = {"legacy_failure_recovered_at": now,
                    "legacy_attempts": int(row["attempts"]), "legacy_error": row["last_error"]}
                database.execute("""UPDATE jobs SET status='waiting_source', attempts=0,
                    next_attempt_at=?, last_error='', payload_json=?, result_json='{}',
                    lease_owner='',lease_token='',lease_expires_at=0,updated_at=? WHERE id=?""",
                    (now, _json(payload), now, int(row["id"])))
                # The old failure receipt belongs to the previous lifecycle.
                # The normal bot may deliver one corrected completion later.
                if database.execute("SELECT 1 FROM sqlite_master WHERE name='notification_deliveries'").fetchone():
                    database.execute("DELETE FROM notification_deliveries WHERE job_id=?", (int(row["id"]),))
                self._event(database, int(row["id"]), "legacy_missing_source_recovered", {
                    "previous_attempts": int(row["attempts"]), "next_attempt_at": now})
                recovered += 1
        return recovered

    def request_source_recheck(self, job_id: int, *, reason: str) -> QueueJob:
        """Admin-trigger one normal source pass without resetting its history."""
        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in {"waiting_source", "retry_source"}:
                raise RuntimeError("only an unleased source-wait job can be rechecked")
            database.execute("UPDATE jobs SET next_attempt_at=?,updated_at=? WHERE id=?", (now, now, job_id))
            self._event(database, job_id, "source_recheck_requested", {"reason": str(reason)[:500]})
            return self._row_to_job(database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def fail(
        self,
        job_id: int,
        token: str,
        *,
        stage: str,
        error: str,
        retryable: bool = True,
        offer_mac_on_terminal: bool = False,
        base_delay_seconds: float = 60,
        maximum_delay_seconds: float = 21600,
        diagnostic_payload: dict[str, Any] | None = None,
    ) -> QueueJob:
        if stage not in STAGE_ELIGIBLE:
            raise ValueError(f"unsupported queue stage: {stage}")
        now = self.clock()
        leased_status = STAGE_LEASED[stage]
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = self._leased_row(database, job_id, token, expected={leased_status})
            attempts = self._stage_attempts(database, row, stage)
            max_attempts = self._stage_limit(row, stage, attempts)
            if not retryable or attempts >= max_attempts:
                status = (
                    "awaiting_mac_consent"
                    if offer_mac_on_terminal and stage == "alignment"
                    else "dead_letter"
                )
                next_at = 0.0
            else:
                status = f"retry_{stage}"
                delay = min(maximum_delay_seconds, base_delay_seconds * 2 ** max(0, attempts - 1))
                next_at = now + delay
            safe_error = str(error)[:2000]
            database.execute(
                """
                UPDATE jobs SET status=?, next_attempt_at=?, lease_owner='',
                    lease_token='', lease_expires_at=0, last_error=?, updated_at=?
                WHERE id=?
                """,
                (status, next_at, safe_error, now, job_id),
            )
            if diagnostic_payload is not None:
                database.execute(
                    "UPDATE jobs SET payload_json=? WHERE id=?",
                    (_json(diagnostic_payload), job_id),
                )
            self._event(
                database,
                job_id,
                (
                    "mac_consent_requested"
                    if status == "awaiting_mac_consent"
                    else "failed"
                    if status == "dead_letter"
                    else "retry_scheduled"
                ),
                {"stage": stage, "error": safe_error, "next_attempt_at": next_at},
            )
            updated = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert updated is not None
            return self._row_to_job(updated)

    def set_mac_consent(
        self,
        job_id: int,
        *,
        approved: bool,
        actor: str,
    ) -> QueueJob:
        """Record the human decision before any Mac can lease the audio."""

        now = self.clock()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise RuntimeError("word lyric job does not exist")
            status = str(row["status"])
            if status != "awaiting_mac_consent":
                approved_statuses = {
                    "needs_mac_alignment",
                    "retry_mac_alignment",
                    "leased_mac_alignment",
                }
                if approved and status in approved_statuses:
                    return self._row_to_job(row)
                if (
                    not approved
                    and status == "dead_letter"
                    and str(row["last_error"] or "") == "mac_fallback_declined_by_user"
                ):
                    return self._row_to_job(row)
                raise RuntimeError(f"Mac authorization is no longer pending: {status}")

            if approved:
                next_status = "needs_mac_alignment"
                last_error = ""
                maximum_attempts = max(
                    int(row["max_attempts"]), int(row["attempts"]) + 3
                )
                event = "mac_alignment_approved"
            else:
                next_status = "dead_letter"
                last_error = "mac_fallback_declined_by_user"
                maximum_attempts = int(row["max_attempts"])
                event = "mac_alignment_declined"
            registered = register_notification_subscription(database,job_id,
                json.loads(row["metadata_json"] or "{}").get("notification"),now,legacy_origin=True)
            database.execute(
                """
                UPDATE jobs SET status=?, max_attempts=?, next_attempt_at=?,
                    lease_owner='', lease_token='', lease_expires_at=0,
                    last_error=?, updated_at=? WHERE id=?
                """,
                (
                    next_status,
                    maximum_attempts,
                    now if approved else 0.0,
                    last_error,
                    now,
                    job_id,
                ),
            )
            delivery_table = database.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='notification_deliveries'
                """
            ).fetchone()
            if approved and registered is None and delivery_table is not None:
                database.execute(
                    "DELETE FROM notification_deliveries WHERE job_id=?",
                    (job_id,),
                )
            self._event(database, job_id, event, {"actor": str(actor)[:200]})
            updated = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert updated is not None
            return self._row_to_job(updated)

    def get(self, job_id: int) -> QueueJob | None:
        with self.connect() as database:
            row = database.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    def active_jobs(self) -> list[QueueJob]:
        with self.connect() as database:
            rows = database.execute(
                """
                SELECT * FROM jobs
                WHERE status NOT IN ('completed','dead_letter','superseded')
                ORDER BY created_at, id
                """
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def refresh_changed_active_jobs(self) -> list[EnqueueResult]:
        """Replace active jobs whose media file was safely rewritten in place.

        ReplayGain and other taggers may change an M4A's bytes after the bot has
        queued it.  Retaining the old SHA-256 would make every later Mac retry
        fail forever.  A cheap size/mtime check avoids hashing unchanged files;
        changed files get a fresh immutable job while the old lease is
        superseded atomically by ``enqueue``.
        """

        refreshed: list[EnqueueResult] = []
        for job in self.active_jobs():
            path = Path(job.media_path)
            try:
                stat = path.stat()
            except OSError:
                continue
            expected_size = int(job.fingerprint_data.get("size") or -1)
            expected_mtime_ns = int(job.fingerprint_data.get("mtime_ns") or -1)
            if stat.st_size == expected_size and stat.st_mtime_ns == expected_mtime_ns:
                recorded_identity = job.payload.get("audio_identity")
                if (not isinstance(recorded_identity, dict)
                        or recorded_identity.get("method") != MP4_AUDIO_IDENTITY_METHOD):
                    identity = mp4_audio_identity(path)
                    if identity and media_fingerprint(path) == job.fingerprint_data:
                        with self.connect() as database:
                            database.execute("BEGIN IMMEDIATE")
                            current = database.execute("SELECT * FROM jobs WHERE id=?", (job.id,)).fetchone()
                            if (current and current["fingerprint"] == job.fingerprint
                                    and current["status"] not in TERMINAL_STATUSES):
                                payload = json.loads(current["payload_json"] or "{}")
                                recorded = payload.get("audio_identity")
                                if (not isinstance(recorded, dict)
                                        or recorded.get("method") != MP4_AUDIO_IDENTITY_METHOD):
                                    payload["audio_identity"] = identity
                                database.execute("UPDATE jobs SET payload_json=? WHERE id=?", (_json(payload), job.id))
                continue
            try:
                current_fingerprint = media_fingerprint(path)
            except OSError:
                continue
            if current_fingerprint == job.fingerprint_data:
                continue
            result = self.enqueue(
                path,
                source_url=job.source_url,
                metadata=job.metadata,
                initial_status=str(job.metadata.get("initial_status") or ""),
                initial_reason=str(job.metadata.get("initial_reason") or ""),
                max_attempts=job.max_attempts,
                fingerprint_data=current_fingerprint,
            )
            if result.created:
                refreshed.append(result)
        return refreshed

    def verify_lease(self, job_id: int, token: str, *, stage: str) -> QueueJob:
        with self.connect() as database:
            row = self._leased_row(
                database,
                job_id,
                token,
                expected={STAGE_LEASED[stage]},
            )
            return self._row_to_job(row)

    def stats(self) -> dict[str, Any]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status"
            ).fetchall()
            quick_check = str(database.execute("PRAGMA quick_check").fetchone()[0])
            oldest = database.execute(
                """
                SELECT MIN(created_at) FROM jobs
                WHERE status NOT IN ('completed','dead_letter','superseded')
                """
            ).fetchone()[0]
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "database": str(self.database_path),
            "quick_check": quick_check,
            "counts": counts,
            "active": sum(
                count for status, count in counts.items() if status not in TERMINAL_STATUSES
            ),
            "oldest_active_created_at": float(oldest) if oldest is not None else None,
        }

    def events(self, job_id: int) -> list[dict[str, Any]]:
        with self.connect() as database:
            rows = database.execute(
                "SELECT id, event, detail_json, created_at FROM events WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "event": str(row["event"]),
                "detail": json.loads(row["detail_json"] or "{}"),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]


def enqueue_media_job(
    media_path: str | Path,
    *,
    state_dir: str | Path,
    source_url: str = "",
    metadata: dict[str, Any] | None = None,
    initial_status: str = "",
    initial_reason: str = "",
) -> EnqueueResult:
    state_path = Path(state_dir)
    store = QueueStore(state_path / "automation" / "queue.sqlite3")
    return store.enqueue(
        Path(media_path),
        source_url=source_url,
        metadata=metadata,
        initial_status=initial_status,
        initial_reason=initial_reason,
    )
