"""Keep yt-dlp history aligned with successfully imported media only."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Iterable


_LOCK = threading.RLock()


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_attempt_archive(persistent: Path, temporary_directory: Path) -> Path:
    """A downloader can mark its staging output complete before library import."""
    attempt = temporary_directory / "download-archive.txt"
    with _LOCK:
        if not attempt.exists():
            _write_lines(attempt, _read_lines(persistent))
    return attempt


def commit_imported_media(persistent: Path, attempt: Path, media_id: str) -> None:
    """Commit this exact imported ID; leave failed collection members uncommitted."""
    with _LOCK:
        additions = [
            line for line in _read_lines(attempt)
            if line.strip().partition(" ")[2] == media_id
        ]
        existing = _read_lines(persistent)
        seen = {line.strip() for line in existing}
        additions = [line for line in additions if line.strip() not in seen]
        if additions:
            _write_lines(persistent, [*existing, *additions])


def remove_deleted_media_history(
    *,
    database_path: Path,
    kind: str,
    id_column: str,
    archive_path: Path,
    archive_prefix: str,
    paths: Iterable[Path],
) -> int:
    """Remove only records beneath explicitly deleted targets with absent output.

    Archive removal precedes the database delete. If either step fails, retained
    database rows let a repeated deletion finish the same targeted cleanup.
    Callers serialize this with online-media downloads/deletion.
    """
    if id_column not in {"video_id", "media_id"}:
        raise ValueError("unsupported media identity column")
    if not database_path.exists():
        return 0
    targets = [path.resolve(strict=False) for path in paths]
    if not targets:
        return 0
    with _LOCK, sqlite3.connect(database_path, timeout=10) as database:
        rows = database.execute(
            f"SELECT {id_column}, output_path FROM downloads WHERE kind=?", (kind,)
        ).fetchall()
        deleted = []
        for media_id, output_path in rows:
            output = Path(output_path).resolve(strict=False)
            if not any(output == target or target in output.parents for target in targets):
                continue
            try:
                output.stat()
            except FileNotFoundError:
                deleted.append((str(media_id), str(output_path)))
            # Permission and other I/O errors must not be mistaken for deletion.
        if not deleted:
            return 0
        archive_entries = {f"{archive_prefix} {media_id}" for media_id, _ in deleted}
        lines = _read_lines(archive_path)
        kept = [line for line in lines if line.strip() not in archive_entries]
        if kept != lines:
            _write_lines(archive_path, kept)
        database.executemany(
            f"DELETE FROM downloads WHERE kind=? AND {id_column}=? AND output_path=?",
            [(kind, media_id, output) for media_id, output in deleted],
        )
        database.commit()
        return len(deleted)
