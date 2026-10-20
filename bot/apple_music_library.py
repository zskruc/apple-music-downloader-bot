from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import urllib.parse
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from mutagen.mp4 import MP4


APPLE_MUSIC_MEDIA_SUFFIXES = {".m4a", ".mp4", ".m4v"}
SKIP_PATH_PARTS = {"@eaDir", "#recycle"}
SONG_ID_RE = re.compile(r"\d{6,}")
ISRC_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", re.IGNORECASE)
INDEX_LOCK = threading.Lock()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


LIBRARY_ROOT = Path(env("AMDL_LIBRARY_ROOT", "/music"))
INDEX_DB = Path(
    env(
        "AMDL_LIBRARY_INDEX_DB",
        "/state/apple-music-library-index.sqlite3",
    )
)
DEDUP_ENABLED = env_flag("AMDL_LIBRARY_DEDUP_ENABLED", True)


@dataclass(frozen=True)
class ExistingTrack:
    song_id: str
    mode: str
    path: Path
    title: str
    matched_by: str = "song_id"

    @property
    def display_path(self) -> str:
        try:
            return str(self.path.relative_to(LIBRARY_ROOT))
        except ValueError:
            return str(self.path)


def clean_song_id(value: object) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    match = SONG_ID_RE.search(str(value or ""))
    return match.group(0) if match else ""


def first_tag_value(tags: object, key: str) -> str:
    if not hasattr(tags, "get"):
        return ""
    value = tags.get(key)  # type: ignore[union-attr]
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "").strip()


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def track_fingerprint(title: object, artist: object, album: object) -> str:
    normalized_title = normalize_text(title)
    normalized_artist = normalize_text(artist)
    normalized_album = normalize_text(album)
    if not normalized_title or not normalized_artist or not normalized_album:
        return ""
    return "|".join((normalized_title, normalized_artist, normalized_album))


def isrc_from_xid(value: object) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    match = ISRC_RE.search(str(value or ""))
    return match.group(0).upper() if match else ""


def song_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    song_id = clean_song_id((query.get("i") or [""])[-1])
    if song_id:
        return song_id
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2].lower() in {
        "song",
        "music-video",
        "music-videos",
    }:
        return clean_song_id(parts[-1])
    return ""


def media_mode(media: MP4, path: Path) -> str:
    codec = str(getattr(media.info, "codec", "") or "").lower()
    codec_description = str(
        getattr(media.info, "codec_description", "") or ""
    ).lower()
    if codec == "alac" or "alac" in codec_description:
        return "alac"
    if (
        codec in {"ec-3", "ec3", "eac3"}
        or "e-ac-3" in codec_description
        or "dolby atmos" in codec_description
        or any(part.lower() == "atmos" for part in path.parts)
    ):
        return "atmos"
    return codec or "other"


def read_media_identity(path: Path) -> tuple[str, str, str, str, str]:
    try:
        media = MP4(path)
    except Exception:
        return "", "other", "", "", ""
    tags = media.tags or {}
    song_id = clean_song_id(first_tag_value(tags, "cnID"))
    title = first_tag_value(tags, "©nam")
    artist = first_tag_value(tags, "©ART") or first_tag_value(tags, "aART")
    album = first_tag_value(tags, "©alb")
    isrc = isrc_from_xid(first_tag_value(tags, "xid "))
    fingerprint = track_fingerprint(title, artist, album)
    return song_id, media_mode(media, path), title, isrc, fingerprint


def candidate_media_files() -> Iterable[Path]:
    if not LIBRARY_ROOT.exists():
        return
    for path in LIBRARY_ROOT.rglob("*"):
        if (
            not path.is_file()
            or path.name.startswith("._")
            or path.suffix.lower() not in APPLE_MUSIC_MEDIA_SUFFIXES
            or any(part in SKIP_PATH_PARTS for part in path.parts)
        ):
            continue
        yield path


def connect_index() -> sqlite3.Connection:
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(INDEX_DB, timeout=30)
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA synchronous=NORMAL")
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS media_files (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            song_id TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'other',
            title TEXT NOT NULL DEFAULT '',
            isrc TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL DEFAULT '',
            schema_version INTEGER NOT NULL DEFAULT 2,
            indexed_at REAL NOT NULL
        )
        """
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS media_files_song_mode "
        "ON media_files(song_id, mode)"
    )
    columns = {
        str(row[1])
        for row in database.execute("PRAGMA table_info(media_files)")
    }
    for column, definition in (
        ("isrc", "TEXT NOT NULL DEFAULT ''"),
        ("fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if column not in columns:
            database.execute(
                f"ALTER TABLE media_files ADD COLUMN {column} {definition}"
            )
    database.execute(
        "CREATE INDEX IF NOT EXISTS media_files_isrc_mode "
        "ON media_files(isrc, mode)"
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS media_files_fingerprint_mode "
        "ON media_files(fingerprint, mode)"
    )
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    return database


def isrc_conflict_count(database: sqlite3.Connection) -> int:
    row = database.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT mode, isrc
            FROM media_files
            WHERE isrc != '' AND fingerprint != ''
            GROUP BY mode, isrc
            HAVING COUNT(DISTINCT fingerprint) > 1
        )
        """
    ).fetchone()
    return int(row[0] if row else 0)


def refresh_index() -> dict[str, int]:
    if not DEDUP_ENABLED:
        return {
            "files": 0,
            "parsed": 0,
            "removed": 0,
            "tracks": 0,
            "isrc_conflicts": 0,
        }
    with INDEX_LOCK:
        with closing(connect_index()) as database:
            existing = {
                row[0]: (int(row[1]), int(row[2]), int(row[3]))
                for row in database.execute(
                    "SELECT path, size, mtime_ns, schema_version FROM media_files"
                )
            }
            seen: set[str] = set()
            parsed = 0
            files = 0
            now = time.time()
            for path in candidate_media_files():
                files += 1
                path_text = str(path)
                seen.add(path_text)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                signature = (stat.st_size, stat.st_mtime_ns)
                indexed = existing.get(path_text)
                if (
                    stat.st_size <= 0
                    or (
                        indexed is not None
                        and indexed[:2] == signature
                        and indexed[2] >= 2
                    )
                ):
                    continue
                song_id, mode, title, isrc, fingerprint = read_media_identity(path)
                database.execute(
                    """
                    INSERT INTO media_files(
                        path, size, mtime_ns, song_id, mode, title,
                        isrc, fingerprint, schema_version, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 2, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        song_id = excluded.song_id,
                        mode = excluded.mode,
                        title = excluded.title,
                        isrc = excluded.isrc,
                        fingerprint = excluded.fingerprint,
                        schema_version = excluded.schema_version,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        path_text,
                        stat.st_size,
                        stat.st_mtime_ns,
                        song_id,
                        mode,
                        title,
                        isrc,
                        fingerprint,
                        now,
                    ),
                )
                parsed += 1
            stale = set(existing) - seen
            if stale:
                database.executemany(
                    "DELETE FROM media_files WHERE path = ?",
                    ((path,) for path in stale),
                )
            database.execute(
                """
                INSERT INTO metadata(key, value) VALUES('last_scan', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(now),),
            )
            tracks = int(
                database.execute(
                    "SELECT COUNT(*) FROM media_files WHERE song_id != ''"
                ).fetchone()[0]
            )
            isrc_conflicts = isrc_conflict_count(database)
            database.commit()
            return {
                "files": files,
                "parsed": parsed,
                "removed": len(stale),
                "tracks": tracks,
                "isrc_conflicts": isrc_conflicts,
            }


def index_paths(paths: Iterable[Path]) -> None:
    if not DEDUP_ENABLED:
        return
    with INDEX_LOCK:
        with closing(connect_index()) as database:
            now = time.time()
            for path in paths:
                if (
                    path.name.startswith("._")
                    or path.suffix.lower() not in APPLE_MUSIC_MEDIA_SUFFIXES
                ):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    database.execute(
                        "DELETE FROM media_files WHERE path = ?",
                        (str(path),),
                    )
                    continue
                song_id, mode, title, isrc, fingerprint = read_media_identity(path)
                database.execute(
                    """
                    INSERT INTO media_files(
                        path, size, mtime_ns, song_id, mode, title,
                        isrc, fingerprint, schema_version, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 2, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        song_id = excluded.song_id,
                        mode = excluded.mode,
                        title = excluded.title,
                        isrc = excluded.isrc,
                        fingerprint = excluded.fingerprint,
                        schema_version = excluded.schema_version,
                        indexed_at = excluded.indexed_at
                    """,
                    (
                        str(path),
                        stat.st_size,
                        stat.st_mtime_ns,
                        song_id,
                        mode,
                        title,
                        isrc,
                        fingerprint,
                        now,
                    ),
                )
            database.commit()


def find_existing_track(
    song_id: str,
    mode: str,
    *,
    isrc: str = "",
    fingerprint: str = "",
) -> ExistingTrack | None:
    song_id = clean_song_id(song_id)
    isrc = isrc_from_xid(isrc)
    fingerprint = str(fingerprint or "")
    if (
        not DEDUP_ENABLED
        or mode not in {"alac", "atmos"}
        or not any((song_id, isrc, fingerprint))
    ):
        return None
    with INDEX_LOCK:
        with closing(connect_index()) as database:
            lookups = [
                (
                    "song_id",
                    "song_id = ?",
                    (song_id,),
                    bool(song_id),
                ),
                (
                    "isrc+metadata",
                    "isrc = ? AND fingerprint = ?",
                    (isrc, fingerprint),
                    bool(isrc and fingerprint),
                ),
                (
                    "metadata",
                    "fingerprint = ?",
                    (fingerprint,),
                    bool(fingerprint),
                ),
            ]
            seen_paths: set[str] = set()
            for matched_by, predicate, parameters, enabled in lookups:
                if not enabled:
                    continue
                rows = database.execute(
                    f"""
                    SELECT path, title, song_id
                    FROM media_files
                    WHERE {predicate} AND mode = ?
                    ORDER BY path
                    """,
                    (*parameters, mode),
                ).fetchall()
                for path_text, title, indexed_song_id in rows:
                    if path_text in seen_paths:
                        continue
                    seen_paths.add(path_text)
                    path = Path(path_text)
                    try:
                        if path.is_file() and path.stat().st_size > 0:
                            return ExistingTrack(
                                song_id=clean_song_id(indexed_song_id) or song_id,
                                mode=mode,
                                path=path,
                                title=str(title or ""),
                                matched_by=matched_by,
                            )
                    except OSError:
                        pass
                    database.execute(
                        "DELETE FROM media_files WHERE path = ?",
                        (path_text,),
                    )
            database.commit()
    return None
