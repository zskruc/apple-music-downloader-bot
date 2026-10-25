from __future__ import annotations

import copy
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
    key: str | None = None
    agent: str | None = None
    metadata: str | None = None


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


def _native_metadata(root: ET.Element) -> str | None:
    element = root.find(f"{{{TT_NS}}}head/{{{TT_NS}}}metadata")
    if element is None:
        return None
    # Tail whitespace belongs to <head>, not the metadata payload. Omitting
    # it makes read/render/read identity checks stable for pretty native XML.
    standalone = copy.deepcopy(element)
    standalone.tail = None
    return ET.tostring(standalone, encoding="unicode")


def parse_line_references(ttml: str) -> list[LineReference]:
    root = ET.fromstring(ttml)
    metadata = _native_metadata(root)
    references: list[LineReference] = []

    def visit(element: ET.Element, inherited_agent: str | None = None) -> None:
        agent = element.get(f"{{{TTM_NS}}}agent", inherited_agent)
        if local_name(str(element.tag)) == "p":
            begin = parse_time_expression(element.attrib.get("begin"))
            end = parse_time_expression(element.attrib.get("end"))
            text = "".join(element.itertext()).strip()
            if text and begin is not None and end is not None and end > begin:
                references.append(LineReference(text, begin, end,
                    key=element.get(f"{{{ITUNES_NS}}}key"), agent=agent, metadata=metadata))
            return
        for child in element:
            visit(child, agent)

    visit(root)
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
    native_metadata = {line.metadata for line in materialized if line.metadata is not None}
    if len(native_metadata) > 1:
        raise ValueError("lyric lines have inconsistent native metadata")
    if native_metadata:
        metadata = ET.fromstring(next(iter(native_metadata)))
        if metadata.tag != f"{{{TT_NS}}}metadata":
            raise ValueError("native lyric metadata is invalid")
        head.append(metadata)
    else:
        metadata = ET.SubElement(head, f"{{{TT_NS}}}metadata")
        title_element = ET.SubElement(metadata, f"{{{TTM_NS}}}title")
        title_element.text = title
    declared_agents = {element.get(f"{{{XML_NS}}}id")
        for element in metadata.iter(f"{{{TTM_NS}}}agent")}
    line_agents = [line.agent if native_metadata else line.agent or "v1" for line in materialized]
    needed_agents = dict.fromkeys(agent_id for agent in line_agents
        for agent_id in (agent or "").split())
    for agent_id in needed_agents:
        if agent_id in declared_agents:
            continue
        if native_metadata:
            raise ValueError("lyric line references an undeclared native agent")
        agent = ET.SubElement(metadata, f"{{{TTM_NS}}}agent",
            {"type": "person" if agent_id == "v1" else "other", f"{{{XML_NS}}}id": agent_id})
        if agent_id == "v1":
            name = ET.SubElement(agent, f"{{{TTM_NS}}}name", {"type": "full"})
            name.text = artist
    keys = [line.key or f"L{index}" for index, line in enumerate(materialized, start=1)]
    if len(keys) != len(set(keys)):
        raise ValueError("lyric line keys are not unique")
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
                f"{{{ITUNES_NS}}}key": keys[index - 1],
            },
        )
        if line_agents[index - 1] is not None:
            paragraph.set(f"{{{TTM_NS}}}agent", line_agents[index - 1])
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
    metadata = _native_metadata(root)

    def visit(element: ET.Element, parent_begin: float, inherited_agent: str | None = None) -> None:
        agent = element.get(f"{{{TTM_NS}}}agent", inherited_agent)
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
                lines.append(LyricLine(text, element_begin, line_end, tuple(tokens),
                    key=element.get(f"{{{ITUNES_NS}}}key"), agent=agent, metadata=metadata))
            return
        for child in element:
            visit(child, element_begin, agent)

    visit(root, 0.0)
    return lines
