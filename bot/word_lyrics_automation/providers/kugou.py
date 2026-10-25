from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

from ..models import LyricLine, TimedToken
from ..text import is_lyric_metadata_line, normalize_text


_KRC_KEY = bytes(
    (64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105)
)


@dataclass(frozen=True, slots=True)
class KugouLyricCandidate:
    lyric_id: str
    access_key: str
    title: str
    artist: str
    duration_ms: int
    product_from: str
    score: int
    hit_layer: int

    @property
    def is_official(self) -> bool:
        return "官方" in self.product_from


def _request_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://www.kugou.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Kugou returned a non-object response")
    return payload


def search(
    *,
    title: str,
    artist: str,
    duration_ms: int,
) -> list[KugouLyricCandidate]:
    params = urllib.parse.urlencode(
        {
            "ver": 1,
            "man": "yes",
            "client": "pc",
            "keyword": f"{artist} - {title}",
            "duration": max(0, int(duration_ms)),
            "hash": "",
        }
    )
    payload = _request_json(f"https://lyrics.kugou.com/search?{params}")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        return []
    output: list[KugouLyricCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        lyric_id = str(raw.get("id") or raw.get("download_id") or "").strip()
        access_key = str(raw.get("accesskey") or "").strip()
        if not lyric_id or not access_key:
            continue
        output.append(
            KugouLyricCandidate(
                lyric_id=lyric_id,
                access_key=access_key,
                title=str(raw.get("song") or "").strip(),
                artist=str(raw.get("singer") or "").strip(),
                duration_ms=int(raw.get("duration") or 0),
                product_from=str(raw.get("product_from") or "").strip(),
                score=int(raw.get("score") or 0),
                hit_layer=int(raw.get("hitlayer") or 0),
            )
        )
    return output


def fetch_krc(candidate: KugouLyricCandidate) -> str:
    params = urllib.parse.urlencode(
        {
            "ver": 1,
            "client": "pc",
            "id": candidate.lyric_id,
            "accesskey": candidate.access_key,
            "fmt": "krc",
            "charset": "utf8",
        }
    )
    payload = _request_json(f"https://lyrics.kugou.com/download?{params}")
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise RuntimeError("Kugou returned no KRC content")
    encrypted = base64.b64decode(content, validate=True)
    if not encrypted.startswith(b"krc1"):
        raise RuntimeError("Kugou returned an invalid KRC header")
    compressed = bytes(
        value ^ _KRC_KEY[index % len(_KRC_KEY)]
        for index, value in enumerate(encrypted[4:])
    )
    return zlib.decompress(compressed).decode("utf-8", "strict")


def _markers(payload: str) -> list[tuple[int, int, int, int]]:
    output: list[tuple[int, int, int, int]] = []
    cursor = 0
    while cursor < len(payload):
        left = payload.find("<", cursor)
        if left < 0:
            break
        right = payload.find(">", left + 1)
        if right < 0:
            break
        fields = payload[left + 1 : right].split(",")
        if len(fields) >= 2:
            try:
                output.append((left, right + 1, int(fields[0]), int(fields[1])))
            except ValueError:
                pass
        cursor = right + 1
    return output


def parse_krc(
    krc: str,
    *,
    title: str = "",
    artist: str = "",
) -> list[LyricLine]:
    output: list[LyricLine] = []
    for raw_line in krc.splitlines():
        raw_line = raw_line.lstrip()
        if not raw_line.startswith("[") or "]" not in raw_line:
            continue
        envelope, payload = raw_line[1:].split("]", 1)
        fields = envelope.split(",")
        if len(fields) != 2:
            continue
        try:
            line_start_ms = int(fields[0])
            line_duration_ms = int(fields[1])
        except ValueError:
            continue
        markers = _markers(payload)
        tokens: list[TimedToken] = []
        pending_text = ""
        for index, (_left, right, offset_ms, duration_ms) in enumerate(markers):
            text_end = markers[index + 1][0] if index + 1 < len(markers) else len(payload)
            text = payload[right:text_end]
            if not text:
                continue
            if duration_ms <= 0:
                # KRC commonly gives spaces and credit colons zero duration.
                # Retain their text without inventing an acoustic interval.
                pending_text += text
                continue
            begin = (line_start_ms + offset_ms) / 1000.0
            end = (line_start_ms + offset_ms + duration_ms) / 1000.0
            # KRC integer offsets can round one millisecond before the prior
            # token's end. Snap only that quantization error; larger overlaps
            # still fail LyricLine's strict timing validation.
            if tokens and tokens[-1].end - 0.001001 <= begin < tokens[-1].end:
                begin = tokens[-1].end
            tokens.append(TimedToken(text=pending_text + text, begin=begin, end=end))
            pending_text = ""
        if pending_text and tokens:
            last = tokens[-1]
            tokens[-1] = TimedToken(last.text + pending_text, last.begin, last.end)
        text = "".join(token.text for token in tokens)
        normalized = normalize_text(text)
        if not normalized or not tokens:
            continue
        if is_lyric_metadata_line(text, title=title, artist=artist, begin=line_start_ms / 1000.0):
            continue
        line_begin = min(line_start_ms / 1000.0, tokens[0].begin)
        line_end = max(
            (line_start_ms + line_duration_ms) / 1000.0,
            tokens[-1].end,
        )
        output.append(LyricLine(text, line_begin, line_end, tuple(tokens)))
    return output
