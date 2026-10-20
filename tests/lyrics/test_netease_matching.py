from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from word_lyrics_automation.matching import (
    match_exact_lines,
    retime_exact_lines_to_references,
)
from word_lyrics_automation.providers.netease import parse_yrc
from word_lyrics_automation.text import normalize_text
from word_lyrics_automation.ttml import (
    parse_line_references,
    parse_word_ttml,
    render_word_ttml,
)


APPLE_LINE_TTML = """<?xml version="1.0"?>
<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:timing="Line">
  <body dur="20.0">
    <div>
      <p begin="1.0" end="4.0">红豆生南国 是很遥远的事情</p>
      <p begin="6.0" end="9.0">相思算什么 早无人在意</p>
      <p begin="11.0" end="14.0">红豆生南国 是很遥远的事情</p>
    </div>
  </body>
</tt>
"""

YRC_WITH_COMPRESSED_DUPLICATE = """[1000,3000](1000,500,0)红(1500,500,0)豆(2000,500,0)生南国 (2500,500,0)是很遥远的事情
[6000,3000](6000,1000,0)相思算什么 (7000,2000,0)早无人在意
[9500,300](9500,30,0)红(9530,30,0)豆(9560,30,0)生南国 (9590,210,0)是很遥远的事情
[11000,3000](11000,500,0)红(11500,500,0)豆(12000,500,0)生南国 (12500,500,0)是很遥远的事情
"""


class NeteaseMatchingTests(unittest.TestCase):
    def test_global_match_skips_compressed_duplicate(self) -> None:
        references = parse_line_references(APPLE_LINE_TTML)
        source = parse_yrc(YRC_WITH_COMPRESSED_DUPLICATE)
        report = match_exact_lines(references, source)
        self.assertEqual(report.skipped_source_indexes, (2,))
        self.assertEqual(
            [line.begin for line in report.matched_lines],
            [1.0, 6.0, 11.0],
        )
        self.assertEqual(report.maximum_start_delta, 0.0)

    def test_rendered_ttml_is_word_synced_and_text_exact(self) -> None:
        references = parse_line_references(APPLE_LINE_TTML)
        report = match_exact_lines(
            references,
            parse_yrc(YRC_WITH_COMPRESSED_DUPLICATE),
        )
        ttml = render_word_ttml(
            report.matched_lines,
            duration=20.0,
            title="相思",
            artist="毛阿敏",
        )
        parsed = parse_word_ttml(ttml)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(sum(len(line.tokens) for line in parsed), 10)
        self.assertEqual(
            normalize_text("".join(line.text for line in parsed)),
            normalize_text("".join(reference.text for reference in references)),
        )

    def test_ttml_uses_absolute_clock_times_without_synthetic_spaces(self) -> None:
        source = parse_yrc(
            "[1000,2000](1000,500,0)红(1500,500,0)豆\n"
            "[4000,2000](4000,500,0)Hello(4500,500,0) world"
        )
        ttml = render_word_ttml(
            source,
            duration=10.0,
            title="Spacing",
            artist="Tester",
        )
        root = ET.fromstring(ttml)
        division = next(element for element in root.iter() if element.tag.endswith("}div"))
        paragraphs = [element for element in root.iter() if element.tag.endswith("}p")]
        self.assertEqual(division.attrib["begin"], "00:00:01.000")
        self.assertEqual(paragraphs[0].attrib["begin"], "00:00:01.000")
        first_spans = [child for child in paragraphs[0] if child.tag.endswith("}span")]
        self.assertEqual(first_spans[0].attrib["begin"], "00:00:01.000")
        self.assertEqual(first_spans[1].attrib["begin"], "00:00:01.500")
        self.assertEqual("".join(paragraphs[0].itertext()), "红豆")
        self.assertEqual("".join(paragraphs[1].itertext()), "Hello world")
        self.assertTrue(all(span.tail is None for span in first_spans))
        parsed = parse_word_ttml(ttml)
        self.assertEqual(parsed[0].begin, 1.0)
        self.assertEqual(parsed[0].tokens[0].begin, 1.0)
        self.assertEqual(parsed[0].tokens[1].begin, 1.5)
        self.assertEqual(parsed[1].text, "Hello world")

    def test_exact_reference_fusion_repairs_provider_line_envelope_outlier(self) -> None:
        references = parse_line_references(
            """<?xml version="1.0"?>
<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:timing="Line">
  <body dur="12.0"><div>
    <p begin="1.0" end="4.0">红豆</p>
    <p begin="6.0" end="9.0">相思</p>
  </div></body>
</tt>
"""
        )
        source = parse_yrc(
            "[10000,2000](10000,1000,0)红(11000,1000,0)豆\n"
            "[20000,3000](20000,1000,0)相(21000,2000,0)思"
        )
        report = match_exact_lines(references, source)
        fused = retime_exact_lines_to_references(references, report.matched_lines)
        self.assertEqual([line.text for line in fused], [item.text for item in references])
        self.assertEqual([line.begin for line in fused], [1.0, 6.0])
        self.assertEqual([line.end for line in fused], [4.0, 9.0])
        self.assertEqual(fused[0].tokens[0].begin, 1.0)
        self.assertEqual(fused[0].tokens[-1].end, 4.0)


if __name__ == "__main__":
    unittest.main()
