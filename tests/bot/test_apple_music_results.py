from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_ROOT = PROJECT_ROOT / "bot"
sys.path.insert(0, str(BOT_ROOT))

from apple_music_results import (
    NO_MEDIA_OUTPUT_EXIT_CODE,
    effective_download_return_code,
    format_verified_existing_media,
    parse_verified_existing_media,
)


class AppleMusicResultTests(unittest.TestCase):
    def test_verified_existing_round_trip_supports_unicode_paths(self) -> None:
        path = Path("/music/ALAC/Compilations/琵琶相/02 琵琶语.m4a")
        line = format_verified_existing_media(
            path=path,
            song_id="698628225",
            matched_by="song_id",
        )

        parsed = parse_verified_existing_media(f"prefix {line}")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.path, path)
        self.assertEqual(parsed.song_id, "698628225")
        self.assertEqual(parsed.matched_by, "song_id")

    def test_unverified_or_relative_existing_path_is_rejected(self) -> None:
        self.assertIsNone(parse_verified_existing_media("Media file already exists"))
        self.assertIsNone(
            parse_verified_existing_media(
                '[apple-music-ingest] verified existing media: {"path":"relative.m4a"}'
            )
        )

    def test_zero_output_cannot_be_reported_as_success(self) -> None:
        self.assertEqual(
            effective_download_return_code(
                0,
                changed_media_count=0,
                verified_existing_count=0,
            ),
            NO_MEDIA_OUTPUT_EXIT_CODE,
        )
        self.assertEqual(
            effective_download_return_code(
                0,
                changed_media_count=1,
                verified_existing_count=0,
            ),
            0,
        )
        self.assertEqual(
            effective_download_return_code(
                0,
                changed_media_count=0,
                verified_existing_count=1,
            ),
            0,
        )
        self.assertEqual(
            effective_download_return_code(
                72,
                changed_media_count=1,
                verified_existing_count=1,
            ),
            72,
        )


if __name__ == "__main__":
    unittest.main()
