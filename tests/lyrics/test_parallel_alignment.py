from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from word_lyrics_automation.alignment import build_candidate
from word_lyrics_automation.models import LyricLine, TimedToken
from word_lyrics_automation.resolver import CandidateRejected, LegacyGateway
from word_lyrics_automation.ttml import parse_time_expression, parse_word_ttml, render_word_ttml


# Native 27-line reference timing retained from the 2026-10-17 failure.
# Text below is synthetic; two repeated pairs retain the chorus ambiguity.
NATIVE_INTERVALS = (
    (19.823, 27.187), (28.126, 35.793), (36.404, 43.755), (44.655, 52.388),
    (52.913, 61.489), (60.605, 65.319), (65.482, 69.047), (69.057, 72.947),
    (73.656, 81.153), (81.899, 89.820), (94.160, 98.317), (98.327, 102.467),
    (102.477, 106.648), (106.678, 110.707), (110.846, 114.873), (114.883, 118.929),
    (119.009, 123.089), (123.169, 127.551), (131.569, 138.682), (139.784, 147.539),
    (148.011, 156.626), (155.682, 160.397), (160.578, 164.195), (164.205, 169.738),
    (170.689, 178.137), (178.953, 186.527), (187.313, 202.206),
)


def reference_payload(intervals, texts=None, *, duration=212):
    if texts is None:
        texts = ["样本" + chr(0x4E00 + index) for index in range(len(intervals))]
    return {
        "track": {"title": "Test", "artist": "Test", "duration": duration},
        "reference_text": "".join(texts),
        "reference_lines": [
            {"text": text, "begin": begin, "end": end}
            for text, (begin, end) in zip(texts, intervals, strict=True)
        ],
    }


def observation(text, begin, end):
    return {"segments": [{"words": [
        {"word": character, "start": begin + (end - begin) * index / len(text),
         "end": begin + (end - begin) * (index + 1) / len(text), "probability": .99}
        for index, character in enumerate(text)
    ]}]}


def timed_line(text, begin, end):
    return LyricLine(text, begin, end, (TimedToken(text, begin, end),))


class ParallelReferenceAlignmentTests(unittest.TestCase):
    def native_candidate(self):
        texts = ["样本" + chr(0x4E00 + index) for index in range(27)]
        texts[20:22] = texts[4:6]
        payload = reference_payload(NATIVE_INTERVALS, texts)
        windows = [observation(line["text"], line["begin"], line["end"])
                   for line in payload["reference_lines"]]
        ttml, report = build_candidate({}, payload, reference_results=windows)
        return payload, windows, ttml, report

    def test_native_27_line_parallel_choruses_keep_their_actual_clock(self):
        payload, _, ttml, report = self.native_candidate()
        lines = parse_word_ttml(ttml)
        self.assertEqual(len(lines), 27)
        self.assertEqual([line.text for line in lines],
                         [reference["text"] for reference in payload["reference_lines"]])
        for line, (begin, end) in zip(lines, NATIVE_INTERVALS, strict=True):
            self.assertAlmostEqual(line.begin, begin, places=3)
            self.assertAlmostEqual(line.end, end, places=3)
            self.assertTrue(all(first.end <= second.begin
                                for first, second in zip(line.tokens, line.tokens[1:])))
        self.assertLess(lines[5].begin, lines[4].end)
        self.assertLess(lines[21].begin, lines[20].end)
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["raw_transcript_text_similarity"], 1)
        LegacyGateway._validate_alignment_reference(lines, payload, 212)

    def test_parallel_reference_does_not_allow_cross_chorus_or_outside_window(self):
        payload, windows, ttml, _ = self.native_candidate()
        lines = parse_word_ttml(ttml)
        for shift in (-5, 95.098):
            with self.subTest(shift=shift):
                bad = list(lines)
                line = lines[4]
                bad[4] = timed_line(line.text, line.begin + shift, line.end + shift)
                with self.assertRaisesRegex(CandidateRejected, "alignment_reference_time_window_mismatch"):
                    LegacyGateway._validate_alignment_reference(bad, payload, 212)
        # Identical later-chorus text cannot serve as acoustic evidence for
        # the first chorus, even though its transcription is an exact match.
        windows[3:6] = windows[19:22]
        with self.assertRaisesRegex(RuntimeError, "acoustic/reference mismatch"):
            build_candidate({}, payload, reference_results=windows)

    def test_nonoverlap_references_still_serialize_observed_padding(self):
        payload = reference_payload(((10, 14), (15, 19)), ("春来", "秋去"), duration=30)
        windows = [observation("春来", 10, 15), observation("秋去", 14.5, 19)]
        ttml, _ = build_candidate({}, payload, reference_results=windows)
        lines = parse_word_ttml(ttml)
        self.assertEqual(lines[0].end, 15)
        self.assertGreaterEqual(lines[1].begin, lines[0].end)
        LegacyGateway._validate_alignment_reference(lines, payload, 30)
        bad = [timed_line("春来", 10, 15), timed_line("秋去", 14.5, 19)]
        with self.assertRaisesRegex(CandidateRejected, "alignment_reference_time_window_mismatch"):
            LegacyGateway._validate_alignment_reference(bad, payload, 30)

    def test_nested_long_line_preserves_short_line_boundaries_and_div_envelope(self):
        payload = reference_payload(((10, 30), (12, 15), (16, 20)),
                                    ("长音", "短句", "后句"), duration=40)
        windows = [observation(line["text"], line["begin"], line["end"])
                   for line in payload["reference_lines"]]
        ttml, _ = build_candidate({}, payload, reference_results=windows)
        lines = parse_word_ttml(ttml)
        self.assertEqual([(line.begin, line.end) for line in lines],
                         [(10, 30), (12, 15), (16, 20)])
        LegacyGateway._validate_alignment_reference(lines, payload, 40)
        root = ET.fromstring(ttml)
        division = root.find("{*}body/{*}div")
        self.assertEqual(parse_time_expression(division.attrib["end"]), 30)
        # Sharing a long overlapping line does not authorize the two short
        # lines to overlap each other when their native intervals do not.
        bad = [lines[0], timed_line("短句", 12, 16.5), lines[2]]
        with self.assertRaisesRegex(CandidateRejected, "alignment_reference_time_window_mismatch"):
            LegacyGateway._validate_alignment_reference(bad, payload, 40)

    def test_reference_overlap_does_not_allow_output_start_reordering(self):
        payload = reference_payload(((10, 18), (12, 20)), ("长音", "和声"), duration=30)
        reordered = [timed_line("长音", 12, 18), timed_line("和声", 11, 20)]
        with self.assertRaisesRegex(CandidateRejected, "alignment_reference_time_window_mismatch"):
            LegacyGateway._validate_alignment_reference(reordered, payload, 30)

    def test_intervening_long_overlap_does_not_erase_an_earlier_nonoverlap_boundary(self):
        payload = reference_payload(((10, 15), (12, 30), (16, 20)),
                                    ("前句", "长音", "后句"), duration=40)
        windows = [observation("前句", 10, 16.5), observation("长音", 12, 30),
                   observation("后句", 16, 20)]
        ttml, _ = build_candidate({}, payload, reference_results=windows)
        lines = parse_word_ttml(ttml)
        self.assertEqual(lines[0].end, 16.5)
        self.assertGreaterEqual(lines[2].begin, lines[0].end)
        LegacyGateway._validate_alignment_reference(lines, payload, 40)
        bad = [lines[0], lines[1], timed_line("后句", 16, 20)]
        with self.assertRaisesRegex(CandidateRejected, "alignment_reference_time_window_mismatch"):
            LegacyGateway._validate_alignment_reference(bad, payload, 40)

    def test_render_envelope_covers_all_materialized_lines(self):
        lines = [timed_line("短句", 12, 15), timed_line("长音", 10, 30), timed_line("后句", 16, 20)]
        ttml = render_word_ttml(lines, duration=40, title="Test", artist="Test")
        division = ET.fromstring(ttml).find("{*}body/{*}div")
        self.assertEqual(parse_time_expression(division.attrib["begin"]), 10)
        self.assertEqual(parse_time_expression(division.attrib["end"]), 30)
        self.assertEqual([line.text for line in parse_word_ttml(ttml)], [line.text for line in lines])


if __name__ == "__main__":
    unittest.main()
