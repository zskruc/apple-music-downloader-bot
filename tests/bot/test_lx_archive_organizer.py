import tempfile
import unittest
from pathlib import Path, PurePosixPath

from lx_fallback import (
    LxCandidate,
    LxFallbackClient,
    candidate_archive_subpath,
    map_server_music_path,
    safe_archive_component,
)


class FakeOrganizingClient(LxFallbackClient):
    def __init__(self, output_root: Path) -> None:
        super().__init__(
            "http://lxserver.invalid",
            output_root=output_root,
            server_music_root="/server/music/_open",
        )
        self.filename = "Track - Artist - flac - Album.flac"

    def cache_entry(self, candidate, *, quality=None, exact_quality=False):
        return {
            "exists": True,
            "folder": "music",
            "filename": self.filename,
            "path": f"/server/music/_open/{self.filename}",
        }

    def _request_json(self, path, *, method="GET", payload=None):
        if path != "/api/music/cache/categorize":
            raise AssertionError(path)
        self.assert_payload(payload)
        source = self.output_root / Path(*PurePosixPath(self.filename).parts)
        destination = (
            self.output_root
            / Path(*PurePosixPath(payload["subPath"]).parts)
            / source.name
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        source.with_suffix(".lrc").rename(destination.with_suffix(".lrc"))
        self.filename = destination.relative_to(self.output_root).as_posix()
        return {"success": True, "successCount": 1, "failCount": 0}

    @staticmethod
    def assert_payload(payload):
        if not isinstance(payload, dict):
            raise AssertionError("missing payload")
        if payload.get("filenames") != ["Track - Artist - flac - Album.flac"]:
            raise AssertionError(payload)
        if payload.get("subPath") != "AC／DC＼Guest/Album／Deluxe":
            raise AssertionError(payload)


class LxArchiveOrganizerTests(unittest.TestCase):
    def test_safe_component_blocks_path_traversal(self):
        self.assertEqual(safe_archive_component("../AC/DC", "Unknown"), "／AC／DC")
        self.assertEqual(safe_archive_component("..", "Unknown Album"), "Unknown Album")

    def test_map_server_music_path_supports_public_root_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapped = map_server_music_path(
                "/server/music/_open/Artist/Album/song.flac",
                root,
                "/server/music/_open",
            )
            self.assertEqual(
                mapped,
                root.resolve() / "Artist" / "Album" / "song.flac",
            )

    def test_organize_cached_path_moves_audio_and_lyrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Track - Artist - flac - Album.flac"
            source.write_bytes(b"audio")
            source.with_suffix(".lrc").write_text("lyrics", encoding="utf-8")
            candidate = LxCandidate(
                song_info={"songmid": "1"},
                source="kw",
                title="Track",
                artist="AC/DC\\Guest",
                album="Album/Deluxe",
                duration_seconds=1.0,
                score=1,
            )
            self.assertEqual(
                candidate_archive_subpath(candidate).as_posix(),
                "AC／DC＼Guest/Album／Deluxe",
            )
            client = FakeOrganizingClient(root)
            organized = client.organize_cached_path(candidate)
            self.assertEqual(
                organized,
                root.resolve()
                / "AC／DC＼Guest"
                / "Album／Deluxe"
                / "Track - Artist - flac - Album.flac",
            )
            self.assertEqual(organized.read_bytes(), b"audio")
            self.assertEqual(organized.with_suffix(".lrc").read_text(), "lyrics")
            self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
