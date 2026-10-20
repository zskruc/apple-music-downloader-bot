"""Positive, independent no-lyrics evidence; missing text never proves this."""
from __future__ import annotations

import math
import re
from typing import Any

from .text import is_credit_line, normalize_text


INSTRUMENTAL_DISPLAY = "纯音乐 / 无词配乐（无需逐词歌词）"
_MARKERS = {normalize_text(text) for text in (
    INSTRUMENTAL_DISPLAY, "纯音乐", "純音樂", "纯音乐，请欣赏", "純音樂，請欣賞",
    "纯音乐，请您欣赏", "純音樂，請您欣賞", "Instrumental", "[Instrumental]",
    "此歌曲为没有填词的纯音乐，请您欣赏", "此歌曲为没有填词的纯音乐，请欣赏",
)}


def is_instrumental_marker(text: str) -> bool:
    return normalize_text(text) in _MARKERS


def lyric_content(text: str) -> list[str]:
    """Remove LRC clocks and metadata, retaining every substantive text row."""
    lines = []
    for raw in str(text or "").splitlines():
        if re.match(r"^\[(?:ti|ar|al|by|offset|length):", raw, re.I):
            continue
        value = re.sub(r"^(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+", "", raw).strip()
        if normalize_text(value) and not is_credit_line(value):
            lines.append(value)
    return lines


def exact_netease_record(track: Any, artist_names: list[str], song: Any) -> bool:
    # No substring artist matches or stripped recording qualifiers here.
    names = {normalize_text(name) for name in [track.artist, *artist_names] if name}
    split = re.compile(r"\s*(?:/|&|、|,|，)\s*")
    original = {normalize_text(name) for name in split.split(track.artist) if normalize_text(name)}
    names.update(original)
    credits = {normalize_text(name) for name in split.split(song.artist) if normalize_text(name)}
    try:
        duration = float(track.duration)
        delta = abs(duration - float(song.duration_ms) / 1000)
    except (ValueError, TypeError):
        return False
    return bool(
        normalize_text(track.title) and normalize_text(track.album)
        and normalize_text(song.title) == normalize_text(track.title)
        and normalize_text(song.album) == normalize_text(track.album)
        and credits and credits <= names and len(credits) >= len(original)
        and math.isfinite(duration) and duration > 0 and delta <= .75
    )


def netease_instrumental(record: dict[str, Any]) -> bool:
    if record.get("pureMusic") is not True and record.get("nolyric") is not True:
        return False
    for key in ("lrc", "yrc", "klyric", "tlyric", "romalrc"):
        field = record.get(key) or {}
        text = field.get("lyric") if isinstance(field, dict) else ""
        if key in {"yrc", "klyric"} and str(text or "").strip():
            return False
        if any(not is_instrumental_marker(line) for line in lyric_content(str(text or ""))):
            return False
    return True


def confirm_catalog_instrumental(track: Any, artist_names: list[str], song: Any,
                                 lyric_record: dict[str, Any],
                                 catalog_record: dict[str, Any]) -> dict[str, Any] | None:
    if not exact_netease_record(track, artist_names, song) or not netease_instrumental(lyric_record):
        return None
    credits = {normalize_text(name) for name in re.split(r"\s*(?:/|&|、|,|，)\s*", song.artist)}
    try:
        delta = abs(float(track.duration) - float(catalog_record.get("duration") or 0))
    except (ValueError, TypeError):
        return None
    url = str(catalog_record.get("url") or "")
    if (catalog_record.get("provider") != "musiksammler"
            or catalog_record.get("instrumental") is not True
            or not re.fullmatch(r"https://www\.musik-sammler\.de/release/(?:[a-z0-9-]+-)?\d+/", url)
            or normalize_text(str(catalog_record.get("title") or "")) != normalize_text(track.title)
            or normalize_text(str(catalog_record.get("album") or "")) != normalize_text(track.album)
            or normalize_text(str(catalog_record.get("artist") or "")) not in credits
            or not math.isfinite(delta) or delta > .75):
        return None
    return {
        "classification": "instrumental_or_wordless", "provider": "netease_musiksammler",
        "independent_sources": 2, "sources": [
            {"provider": "netease", "id": str(song.song_id),
             "url": "https://music.163.com/song?id=" + str(song.song_id),
             "title": song.title, "artist": song.artist, "album": song.album,
             "duration_ms": song.duration_ms, "pureMusic": lyric_record.get("pureMusic"),
             "nolyric": lyric_record.get("nolyric")},
            catalog_record,
        ],
    }


def confirm_instrumental(track: Any, artist_names: list[str], record: dict[str, Any],
                         isrc_record: dict[str, Any]) -> dict[str, Any] | None:
    """Require a complete exact signature AND an independently matched ISRC/work.

    Equality deliberately retains recording qualifiers and full artist names.
    A title match with a different live/remix version, an empty lyric response,
    a composer credit, or a single crowdsourced instrumental flag is insufficient.
    """
    names = {normalize_text(name) for name in [track.artist, *artist_names] if name}
    try:
        duration = float(track.duration)
        source_duration = float(record.get("duration") or 0)
        if (not math.isfinite(duration) or not math.isfinite(source_duration)
                or duration <= 0 or source_duration <= 0
                or abs(duration - source_duration) > .75):
            return None
    except (TypeError, ValueError):
        return None
    if (record.get("instrumental") is not True or not record.get("id")
            or not normalize_text(track.album)
            or normalize_text(str(record.get("trackName") or "")) != normalize_text(track.title)
            or normalize_text(str(record.get("artistName") or "")) not in names
            or normalize_text(str(record.get("albumName") or "")) != normalize_text(track.album)
            or str(record.get("plainLyrics") or "").strip()
            or str(record.get("syncedLyrics") or "").strip()
            or not track.isrc
            or str(isrc_record.get("isrc") or "").upper() != track.isrc.upper()):
        return None
    lyricsfile = record.get("lyricsfile")
    if lyricsfile and (not isinstance(lyricsfile, str)
                       or not re.search(r"(?m)^lines:\s*\[\]\s*$", lyricsfile)):
        return None
    for recording in isrc_record.get("recordings") or []:
        if not isinstance(recording, dict):
            continue
        credits = recording.get("artist-credit") or []
        recording_names = {normalize_text(str(credit.get("name") or
                            (credit.get("artist") or {}).get("name") or ""))
                           for credit in credits if isinstance(credit, dict)}
        try:
            milliseconds = float(recording.get("length") or 0)
            if not math.isfinite(milliseconds) or milliseconds <= 0 or abs(milliseconds / 1000 - duration) > .75:
                continue
        except (ValueError, TypeError):
            continue
        if (normalize_text(str(recording.get("title") or "")) != normalize_text(track.title)
                or not recording_names or not recording_names <= names or not recording.get("id")):
            continue
        performances = [relation for relation in recording.get("relations") or []
                        if isinstance(relation, dict) and relation.get("type") == "performance"
                        and relation.get("target-type") == "work"]
        if not performances:
            continue
        works = []
        for relation in performances:
            work = relation.get("work") or {}
            languages = set(work.get("languages") or ([work["language"]] if work.get("language") else []))
            attributes = set(relation.get("attributes") or [])
            if (not work.get("id") or (languages and languages != {"zxx"})
                    or not (languages == {"zxx"} or "instrumental" in attributes)):
                break
            works.append({"work_id": work["id"], "languages": sorted(languages),
                          "attributes": sorted(attributes)})
        else:
            return {
                "classification": "instrumental_or_wordless", "lrclib_id": str(record["id"]),
                "lrclib_signature": {key: record.get(key) for key in
                    ("trackName", "artistName", "albumName", "duration", "instrumental")},
                "lrclib_url": "https://lrclib.net/api/get/" + str(record["id"]),
                "musicbrainz_recording_id": recording["id"], "isrc": track.isrc.upper(),
                "recording_duration_ms": milliseconds,
                "musicbrainz_url": "https://musicbrainz.org/recording/" + recording["id"],
                "works": works, "duration_delta_seconds": round(abs(duration - source_duration), 6),
            }
    return None
