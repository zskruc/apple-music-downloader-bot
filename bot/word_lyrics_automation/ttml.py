from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

from .models import LyricLine, TimedToken


TT_NS = "http://www.w3.org/ns/ttml"
TTM_NS = "http://www.w3.org/ns/ttml#metadata"
ITUNES_NS = "http://music.apple.com/lyric-ttml-internal"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("", TT_NS)
ET.register_namespace("ttm", TTM_NS)
ET.register_namespace("itunes", ITUNES_NS)


@dataclass(frozen=True, slots=True)
class LineReference:
    text: str
    begin: float
    end: float


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_time_expression(value: str | None) -> float | None:
    if value is None:
        return None
    raw = value.strip().removesuffix("s")
    if not raw:
        return None
    if ":" not in raw:
        return float(raw)
    parts = [float(part) for part in raw.split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60.0 + part
    return seconds


def format_seconds(value: float) -> str:
    rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    rendered = rendered if "." in rendered else f"{rendered}.0"
    # The deployed resolver accepts TTML clock times and explicit offset
    # times, but older releases do not accept unitless seconds below 60s.
    # The `s` suffix is valid TTML and works in both Navidrome and the bot.
    return f"{rendered}s"


def format_clock_time(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def time_expression_is_offset(value: str | None) -> bool:
    raw = str(value or "").strip().casefold()
    return bool(raw) and ":" not in raw and raw.endswith(("h", "m", "s", "ms"))


def parse_line_references(ttml: str) -> list[LineReference]:
    root = ET.fromstring(ttml)
    references: list[LineReference] = []
    for element in root.iter():
        if local_name(str(element.tag)) != "p":
            continue
        begin = parse_time_expression(element.attrib.get("begin"))
        end = parse_time_expression(element.attrib.get("end"))
        text = "".join(element.itertext()).strip()
        if not text or begin is None or end is None or end <= begin:
            continue
        references.append(LineReference(text=text, begin=begin, end=end))
    return references


def render_word_ttml(
    lines: Iterable[LyricLine],
    *,
    duration: float,
    title: str,
    artist: str,
    language: str = "zh-Hans",
) -> str:
    materialized = list(lines)
    if not materialized:
        raise ValueError("cannot render an empty lyric document")
    root = ET.Element(
        f"{{{TT_NS}}}tt",
        {
            f"{{{ITUNES_NS}}}timing": "Word",
            f"{{{XML_NS}}}lang": language,
            f"{{{XML_NS}}}space": "preserve",
        },
    )
    head = ET.SubElement(root, f"{{{TT_NS}}}head")
    metadata = ET.SubElement(head, f"{{{TT_NS}}}metadata")
    title_element = ET.SubElement(metadata, f"{{{TTM_NS}}}title")
    title_element.text = title
    agent = ET.SubElement(
        metadata,
        f"{{{TTM_NS}}}agent",
        {"type": "person", f"{{{XML_NS}}}id": "v1"},
    )
    name = ET.SubElement(agent, f"{{{TTM_NS}}}name", {"type": "full"})
    name.text = artist
    body = ET.SubElement(root, f"{{{TT_NS}}}body", {"dur": format_seconds(duration)})
    division = ET.SubElement(
        body,
        f"{{{TT_NS}}}div",
        {
            # Clock-time expressions are media-absolute.  Do not use the `s`
            # suffix here: TTML and Navidrome correctly treat it as an offset
            # from the enclosing element, which caused repeated additions.
            "begin": format_clock_time(min(line.begin for line in materialized)),
            "end": format_clock_time(max(line.end for line in materialized)),
        },
    )
    for index, line in enumerate(materialized, start=1):
        paragraph = ET.SubElement(
            division,
            f"{{{TT_NS}}}p",
            {
                "begin": format_clock_time(line.begin),
                "end": format_clock_time(line.end),
                f"{{{ITUNES_NS}}}key": f"L{index}",
                f"{{{TTM_NS}}}agent": "v1",
            },
        )
        for token in line.tokens:
            span = ET.SubElement(
                paragraph,
                f"{{{TT_NS}}}span",
                {
                    "begin": format_clock_time(token.begin),
                    "end": format_clock_time(token.end),
                },
            )
            span.text = token.text
        timed_text = "".join(token.text for token in line.tokens)
        if not line.text.startswith(timed_text):
            raise ValueError("timed text must be an exact prefix of the reference")
        remainder = line.text[len(timed_text):]
        if line.tokens:
            paragraph[-1].tail = remainder or None
        else:
            # Native line timing remains useful for non-lexical singing.
            # Plain text has no invented karaoke cue in Navidrome.
            paragraph.text = remainder
    # Do not pretty-print a document whose root declares xml:space=preserve.
    # Indentation becomes literal lyric whitespace in conforming parsers and
    # used to insert a fake space between every Chinese character.
    document = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return document + ("" if document.endswith("\n") else "\n")


def parse_word_ttml(ttml: str) -> list[LyricLine]:
    root = ET.fromstring(ttml)
    lines: list[LyricLine] = []

    def visit(element: ET.Element, parent_begin: float) -> None:
        begin_expression = element.attrib.get("begin")
        raw_begin = parse_time_expression(begin_expression)
        if raw_begin is None:
            element_begin = parent_begin
        elif time_expression_is_offset(begin_expression):
            element_begin = parent_begin + raw_begin
        else:
            element_begin = raw_begin
        name = local_name(str(element.tag))
        if name == "p":
            end_expression = element.attrib.get("end")
            raw_end = parse_time_expression(end_expression)
            if raw_end is None:
                return
            line_end = (
                element_begin + raw_end
                if time_expression_is_offset(end_expression)
                else raw_end
            )
            tokens: list[TimedToken] = []
            for span in element:
                if local_name(str(span.tag)) != "span":
                    continue
                token_begin_expression = span.attrib.get("begin")
                token_end_expression = span.attrib.get("end")
                token_begin = parse_time_expression(token_begin_expression)
                token_end = parse_time_expression(token_end_expression)
                token_text = "".join(span.itertext())
                if token_text and token_begin is not None and token_end is not None:
                    absolute_begin = (
                        element_begin + token_begin
                        if time_expression_is_offset(token_begin_expression)
                        else token_begin
                    )
                    absolute_end = (
                        absolute_begin + token_end
                        if time_expression_is_offset(token_end_expression)
                        else token_end
                    )
                    tokens.append(
                        TimedToken(
                            token_text,
                            absolute_begin,
                            absolute_end,
                        )
                    )
            text = "".join(element.itertext())
            if text:
                lines.append(LyricLine(text, element_begin, line_end, tuple(tokens)))
            return
        for child in element:
            visit(child, element_begin)

    visit(root, 0.0)
    return lines
