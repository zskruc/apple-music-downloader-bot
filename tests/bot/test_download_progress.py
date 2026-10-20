from __future__ import annotations

import sys
import unittest
from pathlib import Path


BOT_ROOT = Path(__file__).resolve().parents[2] / "bot"
sys.path.insert(0, str(BOT_ROOT))

from download_progress import (  # noqa: E402
    format_iec_size,
    parse_gamdl_track,
    parse_gamdl_transfer,
    progress_bar,
)


class GamdlProgressParserTests(unittest.TestCase):
    def test_parses_ansi_track_line(self) -> None:
        line = (
            "\x1b[32m[INFO     16:11:19]\x1b[0m\x1b[2m "
            '[Track   3/12  ]\x1b[0m Downloading "A Song"'
        )
        track = parse_gamdl_track(line)

        self.assertIsNotNone(track)
        assert track is not None
        self.assertEqual(track.index, 3)
        self.assertEqual(track.total, 12)
        self.assertEqual(track.title, "A Song")

    def test_parses_live_transfer_line(self) -> None:
        transfer = parse_gamdl_transfer(
            "[download]  45.3% of ~  19.00MiB at "
            "5.76MiB/s ETA 00:01 (frag 5/11)"
        )

        self.assertIsNotNone(transfer)
        assert transfer is not None
        self.assertEqual(transfer.percent, 45.3)
        self.assertEqual(transfer.total_bytes, round(19 * 1024 * 1024))
        self.assertTrue(transfer.total_estimated)
        self.assertEqual(
            transfer.speed_bytes_per_second,
            5.76 * 1024 * 1024,
        )
        self.assertEqual(transfer.eta, "00:01")
        self.assertEqual((transfer.fragment, transfer.fragments), (5, 11))

    def test_parses_completed_transfer_line(self) -> None:
        transfer = parse_gamdl_transfer(
            "[download] 100% of 16.66MiB in 00:00:02 at 6.22MiB/s"
        )

        self.assertIsNotNone(transfer)
        assert transfer is not None
        self.assertEqual(transfer.percent, 100.0)
        self.assertFalse(transfer.total_estimated)
        self.assertEqual(transfer.eta, "00:00")

    def test_formats_bar_and_sizes(self) -> None:
        self.assertEqual(progress_bar(50, width=10), "█████░░░░░")
        self.assertEqual(progress_bar(101, width=4), "████")
        self.assertEqual(format_iec_size(5.76 * 1024 * 1024), "5.76 MiB")


if __name__ == "__main__":
    unittest.main()
