from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ..models import LyricLine, TimedToken
from ..text import is_credit_line, normalize_text


_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_TOKEN_RE = re.compile(r"\((\d+),(\d+),(?:\d+)\)")


@dataclass(frozen=True, slots=True)
class NeteaseSong:
    song_id: str
    title: str
    artist: str
    album: str
    duration_ms: int


def _request_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Referer": "https://music.163.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("NetEase returned a non-object response")
    if payload.get("code") not in (None, 200):
        raise RuntimeError(f"NetEase API error {payload.get('code')}")
    return payload


def _query_association(query: str, songs: list[object]) -> bool:
    """Detect a successful HTTP response that ignored its search query.

    This is a transport sanity check, not recording identification. A related
    title OR credited artist is enough; the resolver still checks the exact
    title, artist, duration and lyric before accepting any candidate.
    """
    chunks = []
    current = []
    for character in unicodedata.normalize("NFKC", query):
        if unicodedata.category(character)[0] in {"L", "N", "M"}:
            current.append(character)
        elif current:
            chunks.append(normalize_text("".join(current)))
            current = []
    if current:
        chunks.append(normalize_text("".join(current)))
    normalized_query = "".join(chunks)
    boundaries = {0}
    cursor = 0
    for chunk in chunks:
        cursor += len(chunk)
        boundaries.add(cursor)

    def ideographic(character: str) -> bool:
        codepoint = ord(character)
        return (0x3040 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF
                or 0x3400 <= codepoint <= 0x9FFF or 0x20000 <= codepoint <= 0x3134F)

    def mentioned(value: object, *, artist: bool = False) -> bool:
        normalized = normalize_text(str(value or ""))
        if not normalized or (artist and len(normalized) < 2):
            return False
        start = normalized_query.find(normalized)
        while start >= 0:
            end = start + len(normalized)
            # A one-character song title is legitimate, but it must be a
            # complete query term. Latin/numeric fragments must likewise not
            # match inside another word or a year in an unrelated query.
            left_boundary = len(normalized) == 1 or not ideographic(normalized[0])
            right_boundary = len(normalized) == 1 or not ideographic(normalized[-1])
            if ((not left_boundary or start in boundaries)
                    and (not right_boundary or end in boundaries)):
                return True
            start = normalized_query.find(normalized, start + 1)
        return False

    for song in songs:
        if not isinstance(song, dict) or not song.get("id"):
            continue
        if mentioned(song.get("name")):
            return True
        artists = song.get("artists") or song.get("ar")
        if isinstance(artists, list) and any(
            isinstance(item, dict) and mentioned(item.get("name"), artist=True)
            for item in artists
        ):
            return True
    return False


def search(query: str, *, limit: int = 20) -> list[NeteaseSong]:
    params = urllib.parse.urlencode(
        {"s": query, "type": 1, "limit": limit, "offset": 0}
    )
    # The old music.163.com/api/search/get endpoint now returns unrelated
    # fixed search results. The interface endpoint can also intermittently
    # return unrelated HTTP-200 data, so validate query association before
    # accepting a nonempty response. The native schemas differ
    # (artists/album/duration vs ar/al/dt).
    songs = None
    failures = []
    for endpoint in ("https://interface.music.163.com/api/search/get",
                     "https://music.163.com/api/cloudsearch/pc"):
        try:
            payload = _request_json(f"{endpoint}?{params}")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("NetEase returned an invalid search result")
            songs = result.get("songs")
            if songs is None and result.get("songCount") == 0:
                songs = []
            if not isinstance(songs, list):
                raise RuntimeError("NetEase returned an invalid song list")
            if songs and not _query_association(query, songs):
                raise RuntimeError("NetEase search returned unrelated results: no title or artist query association")
            break
        except Exception as exc:
            failures.append(f"{type(exc).__name__}:{str(exc)[:180]}")
    else:
        raise RuntimeError("NetEase search endpoints unavailable: " + "; ".join(failures))
    output: list[NeteaseSong] = []
    for raw_song in songs:
        if not isinstance(raw_song, dict) or not raw_song.get("id"):
            continue
        raw_artists = raw_song.get("artists") or raw_song.get("ar")
        artists = [
            str(item.get("name") or "")
            for item in raw_artists
            if isinstance(item, dict)
        ] if isinstance(raw_artists, list) else []
        raw_album = raw_song.get("album") or raw_song.get("al")
        output.append(
            NeteaseSong(
                song_id=str(raw_song.get("id") or ""),
                title=str(raw_song.get("name") or ""),
                artist=" / ".join(name for name in artists if name),
                album=str(raw_album.get("name") or "")
                if isinstance(raw_album, dict)
                else "",
                duration_ms=int(raw_song.get("duration") or raw_song.get("dt") or 0),
            )
        )
    return output


def fetch_lyric_record(song_id: str) -> dict[str, object]:
    params = urllib.parse.urlencode(
        {
            "id": song_id,
            "lv": -1,
            "kv": -1,
            "tv": -1,
            "yv": -1,
            "rv": -1,
        }
    )
    return _request_json(f"https://music.163.com/api/song/lyric?{params}")


def fetch_yrc(song_id: str) -> str:
    payload = fetch_lyric_record(song_id)
    yrc = payload.get("yrc")
    lyric = yrc.get("lyric") if isinstance(yrc, dict) else None
    if not isinstance(lyric, str) or not lyric.strip():
        # Most line-only and instrumental records have no YRC. Keep searching
        # other matched recordings/providers; this is not a network outage.
        return ""
    return lyric


def parse_yrc(yrc: str) -> list[LyricLine]:
    lines: list[LyricLine] = []
    for raw_line in yrc.splitlines():
        line_match = _LINE_RE.match(raw_line.strip())
        if not line_match:
            continue
        line_start_ms = int(line_match.group(1))
        line_duration_ms = int(line_match.group(2))
        payload = line_match.group(3)
        matches = list(_TOKEN_RE.finditer(payload))
        tokens: list[TimedToken] = []
        for index, match in enumerate(matches):
            text_start = match.end()
            text_end = matches[index + 1].start() if index + 1 < len(matches) else len(payload)
            text = payload[text_start:text_end]
            start_ms = int(match.group(1))
            duration_ms = int(match.group(2))
            if not text or duration_ms <= 0:
                continue
            tokens.append(
                TimedToken(
                    text=text,
                    begin=start_ms / 1000.0,
                    end=(start_ms + duration_ms) / 1000.0,
                )
            )
        text = "".join(token.text for token in tokens).strip()
        if not text or not tokens or is_credit_line(text):
            continue
        line_begin = line_start_ms / 1000.0
        line_end = (line_start_ms + line_duration_ms) / 1000.0
        # Some providers round the line envelope a few milliseconds inside the
        # final token. Expanding to the actual tokens is lossless and monotonic.
        line_begin = min(line_begin, tokens[0].begin)
        line_end = max(line_end, tokens[-1].end)
        lines.append(LyricLine(text, line_begin, line_end, tuple(tokens)))
    return lines
