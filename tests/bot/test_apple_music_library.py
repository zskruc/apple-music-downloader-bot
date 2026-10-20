from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_ROOT = PROJECT_ROOT / "bot"
sys.path.insert(0, str(BOT_ROOT))

import apple_music_library as library


class AppleMusicLibraryTests(unittest.TestCase):
    @contextmanager
    def isolated_index(self) -> Iterator[Path]:
        original_root = library.LIBRARY_ROOT
        original_database = library.INDEX_DB
        original_enabled = library.DEDUP_ENABLED
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "music"
            root.mkdir()
            library.LIBRARY_ROOT = root
            library.INDEX_DB = Path(temporary) / "index.sqlite3"
            library.DEDUP_ENABLED = True
            try:
                yield root
            finally:
                library.LIBRARY_ROOT = original_root
                library.INDEX_DB = original_database
                library.DEDUP_ENABLED = original_enabled

    def insert_track(
        self,
        *,
        root: Path,
        relative_path: str,
        song_id: str,
        title: str,
        artist: str,
        album: str,
        isrc: str,
    ) -> Path:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
        stat = path.stat()
        fingerprint = library.track_fingerprint(title, artist, album)
        with library.connect_index() as database:
            database.execute(
                """
                INSERT INTO media_files(
                    path, size, mtime_ns, song_id, mode, title,
                    isrc, fingerprint, schema_version, indexed_at
                ) VALUES (?, ?, ?, ?, 'alac', ?, ?, ?, 2, 1)
                """,
                (
                    str(path),
                    stat.st_size,
                    stat.st_mtime_ns,
                    song_id,
                    title,
                    isrc,
                    fingerprint,
                ),
            )
            database.commit()
        return path

    def seed_colliding_isrc(self, root: Path) -> dict[str, Path]:
        tracks = {
            "bella": self.insert_track(
                root=root,
                relative_path="Compilations/诗的沙漏/1-03 贝拉.m4a",
                song_id="1078595040",
                title="贝拉 [如诗的乐章]",
                artist="邁可霍普",
                album="诗的沙漏",
                isrc="ATION2005132",
            ),
            "earth": self.insert_track(
                root=root,
                relative_path="李星宇/鲸鱼马戏团/11 再见地球.m4a",
                song_id="1070748261",
                title="再见‧地球",
                artist="鲸鱼马戏团",
                album="鲸鱼马戏团",
                isrc="ATION2005132",
            ),
            "stars": self.insert_track(
                root=root,
                relative_path="林海/月光边境/12 星空.m4a",
                song_id="704065891",
                title="星空",
                artist="林海",
                album="月光边境",
                isrc="ATION2005132",
            ),
        }
        return tracks

    def test_conflicting_isrc_alone_does_not_skip_unrelated_track(self) -> None:
        with self.isolated_index() as root:
            self.seed_colliding_isrc(root)

            existing = library.find_existing_track(
                "698628225",
                "alac",
                isrc="ATION2005132",
                fingerprint=library.track_fingerprint("琵琶语", "林海", "琵琶相"),
            )

            self.assertIsNone(existing)
            with library.connect_index() as database:
                self.assertEqual(library.isrc_conflict_count(database), 1)

    def test_exact_song_id_still_skips_despite_isrc_collision(self) -> None:
        with self.isolated_index() as root:
            tracks = self.seed_colliding_isrc(root)

            existing = library.find_existing_track(
                "1078595040",
                "alac",
                isrc="ATION2005132",
                fingerprint=library.track_fingerprint("琵琶语", "林海", "琵琶相"),
            )

            self.assertIsNotNone(existing)
            self.assertEqual(existing.path, tracks["bella"])
            self.assertEqual(existing.matched_by, "song_id")

    def test_isrc_requires_matching_metadata_fingerprint(self) -> None:
        with self.isolated_index() as root:
            tracks = self.seed_colliding_isrc(root)

            existing = library.find_existing_track(
                "999999999",
                "alac",
                isrc="ATION2005132",
                fingerprint=library.track_fingerprint("星空", "林海", "月光边境"),
            )

            self.assertIsNotNone(existing)
            self.assertEqual(existing.path, tracks["stars"])
            self.assertEqual(existing.matched_by, "isrc+metadata")


if __name__ == "__main__":
    unittest.main()
