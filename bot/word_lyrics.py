from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import statistics
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

try:
    from mutagen import File as MutagenFile
    from mutagen.mp4 import MP4
except ImportError:  # pragma: no cover - the production image always has mutagen
    MutagenFile = None  # type: ignore[assignment]
    MP4 = None  # type: ignore[assignment]


APPLE_API_BASE = "https://amp-api.music.apple.com/v1"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
AUDIO_SUFFIXES = {".m4a", ".mp4", ".m4v", ".mp3", ".flac", ".aac", ".ogg", ".opus"}
SONG_ID_RE = re.compile(r"\d{6,}")
ISRC_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", re.IGNORECASE)
LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\]")
ELRC_WORD_TIME_RE = re.compile(r"<(\d{1,3}):(\d{2}(?:\.\d{1,3})?)>")
INLINE_LRC_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{2,5}(?:\.\d{1,3})?)\]")
TTML_CLOCK_RE = re.compile(
    r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$"
)
TTML_OFFSET_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$", re.IGNORECASE)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_text(value: object) -> str:
    from word_lyrics_automation.chinese_text import fold_chinese_script

    value = fold_chinese_script(unicodedata.normalize("NFKC", str(value or "")).casefold())
    return "".join(character for character in value if character.isalnum())


def similarity(left: object, right: object) -> float:
    left_text = normalize_text(left)
    right_text = normalize_text(right)
    if not left_text or not right_text:
        return 0.0
    return difflib.SequenceMatcher(
        None,
        left_text,
        right_text,
        autojunk=False,
    ).ratio()


def parse_time_expression(value: str | None) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    clock = TTML_CLOCK_RE.match(value)
    if clock:
        hours = float(clock.group(1) or 0)
        minutes = float(clock.group(2))
        seconds = float(clock.group(3))
        return hours * 3600.0 + minutes * 60.0 + seconds
    # Apple line-timed TTML sometimes uses legal, unitless seconds (for
    # example ``begin="7.42"``).  Older releases silently discarded those
    # lines, weakening downstream reference validation.
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    offset = TTML_OFFSET_RE.match(value)
    if not offset:
        return None
    amount = float(offset.group(1))
    unit = offset.group(2).lower()
    return amount * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]


@dataclass(frozen=True)
class TimedLine:
    text: str
    begin: float
    end: float | None = None


@dataclass(frozen=True)
class LyricsDocument:
    kind: str
    timing: str
    lines: tuple[TimedLine, ...]
    timed_spans: int = 0
    total_spans: int = 0
    max_end: float | None = None

    @property
    def normalized_text(self) -> str:
        return "".join(normalize_text(line.text) for line in self.lines)

    @property
    def is_word_synced(self) -> bool:
        if self.kind == "elrc":
            return self.timed_spans > 0
        return self.timing.casefold() == "word" and self.timed_spans > 0


def parse_ttml(text: str) -> LyricsDocument:
    root = ET.fromstring(text)
    timing = next(
        (
            str(value)
            for key, value in root.attrib.items()
            if local_name(str(key)).casefold() == "timing"
        ),
        "",
    )
    lines: list[TimedLine] = []
    total_spans = 0
    timed_spans = 0
    ends: list[float] = []
    for element in root.iter():
        if local_name(str(element.tag)) != "span":
            continue
        total_spans += 1
        begin = parse_time_expression(element.attrib.get("begin"))
        end = parse_time_expression(element.attrib.get("end"))
        if begin is not None and end is not None:
            timed_spans += 1
            ends.append(end)
    for paragraph in root.iter():
        if local_name(str(paragraph.tag)) != "p":
            continue
        line_text = "".join(paragraph.itertext()).strip()
        if not normalize_text(line_text):
            continue
        begin = parse_time_expression(paragraph.attrib.get("begin"))
        end = parse_time_expression(paragraph.attrib.get("end"))
        if begin is None:
            child_begins = [
                parsed
                for child in paragraph.iter()
                if child is not paragraph
                for parsed in [parse_time_expression(child.attrib.get("begin"))]
                if parsed is not None
            ]
            begin = min(child_begins) if child_begins else None
        if end is None:
            child_ends = [
                parsed
                for child in paragraph.iter()
                if child is not paragraph
                for parsed in [parse_time_expression(child.attrib.get("end"))]
                if parsed is not None
            ]
            end = max(child_ends) if child_ends else None
        if begin is None:
            continue
        if end is not None:
            ends.append(end)
        lines.append(TimedLine(text=line_text, begin=begin, end=end))
    return LyricsDocument(
        kind="ttml",
        timing=timing,
        lines=tuple(lines),
        timed_spans=timed_spans,
        total_spans=total_spans,
        max_end=max(ends) if ends else None,
    )


def parse_lrc(text: str) -> LyricsDocument:
    lines: list[TimedLine] = []
    for raw_line in text.splitlines():
        matches = list(LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        line_text = LRC_TIME_RE.sub("", raw_line)
        line_text = ELRC_WORD_TIME_RE.sub("", line_text).strip()
        if not normalize_text(line_text):
            continue
        for match in matches:
            begin = float(match.group(1)) * 60.0 + float(match.group(2))
            lines.append(TimedLine(text=line_text, begin=begin))
    lines.sort(key=lambda line: line.begin)
    return LyricsDocument(kind="lrc", timing="Line", lines=tuple(lines))


def parse_elrc(text: str) -> LyricsDocument:
    lines: list[TimedLine] = []
    word_count = 0
    word_ends: list[float] = []
    for raw_line in text.splitlines():
        line_matches = list(LRC_TIME_RE.finditer(raw_line))
        if not line_matches:
            continue
        line_begin = (
            float(line_matches[0].group(1)) * 60.0
            + float(line_matches[0].group(2))
        )
        word_matches = list(ELRC_WORD_TIME_RE.finditer(raw_line))
        word_count += len(word_matches)
        word_ends.extend(
            float(match.group(1)) * 60.0 + float(match.group(2))
            for match in word_matches
        )
        line_text = LRC_TIME_RE.sub("", raw_line)
        line_text = ELRC_WORD_TIME_RE.sub("", line_text).strip()
        if normalize_text(line_text):
            lines.append(TimedLine(text=line_text, begin=line_begin))
    return LyricsDocument(
        kind="elrc",
        timing="Word",
        lines=tuple(lines),
        timed_spans=word_count,
        total_spans=word_count,
        max_end=max(word_ends) if word_ends else None,
    )


def read_lyrics_document(path: Path) -> LyricsDocument:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.casefold()
    if suffix == ".ttml":
        return parse_ttml(text)
    if suffix == ".elrc":
        return parse_elrc(text)
    return parse_lrc(text)


def validate_word_timing(
    text: str,
    document: LyricsDocument,
    *,
    duration: float = 0.0,
) -> None:
    """Apply the same structural word-clock checks to native and cached lyrics."""
    if not document.is_word_synced or not document.lines:
        raise ValueError("candidate_has_no_word_timing")
    if document.kind == "ttml":
        root = ET.fromstring(text)
        max_end = 0.0

        def absolute_time(expression: str | None) -> float | None:
            if expression is None:
                return None
            parsed = parse_time_expression(expression)
            if parsed is None or not math.isfinite(parsed):
                raise ValueError("invalid_word_timing_expression")
            # This native/legacy path has always read Apple's clock values and
            # historical unitless/offset-form values as media seconds. Keep
            # that interpretation; the acoustic renderer has its own strict
            # nested timing parser.
            return parsed

        def visit(element: ET.Element, parent_begin: float, parent_end: float | None) -> None:
            nonlocal max_end
            raw_begin = absolute_time(element.get("begin"))
            begin = parent_begin if raw_begin is None else raw_begin
            end = absolute_time(element.get("end"))
            explicit_end = end
            if end is None:
                if element.get("dur") is not None:
                    interval = parse_time_expression(element.get("dur"))
                    if interval is None or not math.isfinite(interval) or interval < 0:
                        raise ValueError("invalid_word_timing_duration")
                    end = begin + interval
                else:
                    end = parent_end
            if begin < 0 or (end is not None and end < begin):
                raise ValueError("word_timing_duration_is_not_positive")
            if (raw_begin is not None and explicit_end is not None and end == begin
                    and normalize_text("".join(element.itertext()))):
                raise ValueError("word_timing_duration_is_not_positive")
            # Apple's body duration and division boundary can differ slightly
            # after millisecond rounding. Word and paragraph clocks retain the
            # stricter envelope tolerance.
            envelope_tolerance = 0.020 if local_name(str(element.tag)) in {"body", "div"} else 0.001
            if (raw_begin is not None and begin < parent_begin - envelope_tolerance
                    or end is not None and parent_end is not None and end > parent_end + envelope_tolerance):
                raise ValueError("word_timing_exceeds_parent_envelope")
            if end is not None:
                max_end = max(max_end, end)
            # Native Apple documents have parallel agents and nested backing
            # vocals. Their clocks are valid independent branches, not one
            # globally ordered list of words.
            for child in element:
                visit(child, begin, end)

        visit(root, 0.0, None)
    elif document.kind == "elrc":
        max_end = 0.0
        for raw_line in text.splitlines():
            line_matches = list(LRC_TIME_RE.finditer(raw_line))
            word_matches = list(ELRC_WORD_TIME_RE.finditer(raw_line))
            if not word_matches:
                continue
            if not line_matches:
                raise ValueError("word_timing_has_no_line")
            previous = float(line_matches[0].group(1)) * 60 + float(line_matches[0].group(2))
            for match in word_matches:
                begin = float(match.group(1)) * 60 + float(match.group(2))
                if begin < previous:
                    raise ValueError("word_timing_is_not_monotonic")
                previous = begin
            max_end = max(max_end, previous)
    else:
        raise ValueError("unsupported_word_timing_format")
    if duration > 0 and max_end > duration + 5.0:
        raise ValueError("candidate_exceeds_audio_duration")


class NativeAudioChanged(RuntimeError):
    pass


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def align_lines(
    candidate: Sequence[TimedLine],
    reference: Sequence[TimedLine],
) -> list[tuple[TimedLine, TimedLine, float]]:
    left = [normalize_text(line.text) for line in candidate]
    right = [normalize_text(line.text) for line in reference]
    rows = len(left)
    columns = len(right)
    gap = -0.36
    scores = [[0.0] * (columns + 1) for _ in range(rows + 1)]
    moves = [[""] * (columns + 1) for _ in range(rows + 1)]
    for row in range(1, rows + 1):
        scores[row][0] = row * gap
        moves[row][0] = "up"
    for column in range(1, columns + 1):
        scores[0][column] = column * gap
        moves[0][column] = "left"
    similarity_cache: dict[tuple[int, int], float] = {}
    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            ratio = difflib.SequenceMatcher(
                None,
                left[row - 1],
                right[column - 1],
                autojunk=False,
            ).ratio()
            similarity_cache[(row - 1, column - 1)] = ratio
            match_score = scores[row - 1][column - 1] + (2.0 * ratio - 0.75)
            up_score = scores[row - 1][column] + gap
            left_score = scores[row][column - 1] + gap
            best = max(match_score, up_score, left_score)
            scores[row][column] = best
            moves[row][column] = (
                "diag"
                if best == match_score
                else ("up" if best == up_score else "left")
            )
    aligned: list[tuple[TimedLine, TimedLine, float]] = []
    row, column = rows, columns
    while row > 0 or column > 0:
        move = moves[row][column]
        if move == "diag":
            ratio = similarity_cache[(row - 1, column - 1)]
            if ratio >= 0.68:
                aligned.append((candidate[row - 1], reference[column - 1], ratio))
            row -= 1
            column -= 1
        elif move == "up":
            row -= 1
        else:
            column -= 1
    aligned.reverse()
    return aligned


def comparison_metrics(
    candidate: LyricsDocument,
    reference: LyricsDocument,
) -> dict[str, Any]:
    matches = align_lines(candidate.lines, reference.lines)
    deltas = [left.begin - right.begin for left, right, _ratio in matches]
    absolute_deltas = [abs(value) for value in deltas]
    offset = statistics.median(deltas) if deltas else None
    residuals = [abs(value - offset) for value in deltas] if offset is not None else []
    matched_characters = sum(
        int(min(len(normalize_text(left.text)), len(normalize_text(right.text))) * ratio)
        for left, right, ratio in matches
    )
    candidate_characters = len(candidate.normalized_text)
    reference_characters = len(reference.normalized_text)
    return {
        "text_similarity": round(
            difflib.SequenceMatcher(
                None,
                candidate.normalized_text,
                reference.normalized_text,
                autojunk=False,
            ).ratio()
            if candidate.normalized_text and reference.normalized_text
            else 0.0,
            6,
        ),
        "matched_lines": len(matches),
        "candidate_lines": len(candidate.lines),
        "reference_lines": len(reference.lines),
        "matched_character_coverage": round(
            matched_characters / max(1, min(candidate_characters, reference_characters)),
            6,
        ),
        "median_absolute_delta_seconds": (
            round(statistics.median(absolute_deltas), 6) if absolute_deltas else None
        ),
        "p95_absolute_delta_seconds": (
            round(percentile(absolute_deltas, 0.95) or 0.0, 6)
            if absolute_deltas
            else None
        ),
        "median_offset_seconds": round(offset, 6) if offset is not None else None,
        "p95_residual_seconds": (
            round(percentile(residuals, 0.95) or 0.0, 6) if residuals else None
        ),
        "absolute_delta_inlier_fraction_0_8": round(
            sum(value <= 0.80 for value in absolute_deltas) / max(1, len(absolute_deltas)),
            6,
        ),
        "residual_inlier_fraction_0_85": round(
            sum(value <= 0.85 for value in residuals) / max(1, len(residuals)),
            6,
        ),
    }


@dataclass(frozen=True)
class Validation:
    accepted: bool
    reason: str
    reference_path: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


def validate_candidate(
    candidate: LyricsDocument,
    references: Sequence[tuple[Path, LyricsDocument]],
    *,
    exact_identity: bool,
    identity_confidence: str,
    duration: float,
) -> Validation:
    if not candidate.is_word_synced:
        return Validation(False, "candidate_is_not_word_synced")
    if len(candidate.lines) < 2 or candidate.timed_spans < 2:
        return Validation(False, "candidate_has_too_few_timed_words")
    if (
        duration > 0
        and candidate.max_end is not None
        and candidate.max_end > duration + 5.0
    ):
        return Validation(
            False,
            "candidate_exceeds_audio_duration",
            metrics={
                "audio_duration_seconds": round(duration, 6),
                "lyrics_end_seconds": round(candidate.max_end, 6),
            },
        )
    comparisons: list[tuple[Path, dict[str, Any]]] = []
    for path, reference in references:
        if not reference.lines:
            continue
        comparisons.append((path, comparison_metrics(candidate, reference)))
    if not comparisons:
        if exact_identity or identity_confidence in {"exact_isrc", "strict_metadata"}:
            return Validation(
                True,
                "trusted_identity_without_timed_reference",
                metrics={
                    "identity_confidence": identity_confidence,
                    "audio_duration_seconds": round(duration, 6) if duration else None,
                    "lyrics_end_seconds": (
                        round(candidate.max_end, 6)
                        if candidate.max_end is not None
                        else None
                    ),
                },
            )
        return Validation(False, "no_reference_for_fuzzy_identity")
    comparisons.sort(
        key=lambda item: (
            float(item[1].get("text_similarity") or 0),
            float(item[1].get("matched_character_coverage") or 0),
            int(item[1].get("matched_lines") or 0),
        ),
        reverse=True,
    )
    path, metrics = comparisons[0]
    candidate_lines = int(metrics.get("candidate_lines") or 0)
    reference_lines = int(metrics.get("reference_lines") or 0)
    required_lines = min(3, candidate_lines, reference_lines)
    matched_lines = int(metrics.get("matched_lines") or 0)
    match_fraction = matched_lines / max(1, min(candidate_lines, reference_lines))
    metrics = dict(metrics)
    metrics["matched_line_fraction"] = round(match_fraction, 6)

    def metric_number(name: str, missing: float) -> float:
        value = metrics.get(name)
        return missing if value is None else float(value)

    text_matches = (
        metric_number("text_similarity", 0.0) >= 0.86
        or (
            metric_number("matched_character_coverage", 0.0) >= 0.92
            and match_fraction >= 0.80
        )
    )
    timing_matches = (
        metric_number("median_absolute_delta_seconds", 999.0) <= 0.80
        and (
            (
                metric_number("p95_absolute_delta_seconds", 999.0) <= 1.60
                and metric_number("p95_residual_seconds", 999.0) <= 0.85
            )
            or (
                metric_number("absolute_delta_inlier_fraction_0_8", 0.0) >= 0.80
                and metric_number("residual_inlier_fraction_0_85", 0.0) >= 0.80
            )
        )
    )
    strong_whole_text_match = (
        metric_number("text_similarity", 0.0) >= 0.95
        and matched_lines >= max(3, required_lines)
        and timing_matches
    )
    accepted = strong_whole_text_match or (
        text_matches
        and metric_number("matched_character_coverage", 0.0) >= 0.80
        and matched_lines >= required_lines
        and match_fraction >= 0.55
        and timing_matches
    )
    return Validation(
        accepted,
        "matched_existing_timed_lyrics" if accepted else "existing_lyrics_mismatch",
        reference_path=str(path),
        metrics=metrics,
    )


def first_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "").strip()


def value_from_tags(tags: object, keys: Sequence[str]) -> str:
    if not hasattr(tags, "get"):
        return ""
    for key in keys:
        try:
            value = tags.get(key)  # type: ignore[union-attr]
        except Exception:
            continue
        value_text = first_value(value)
        if value_text:
            return value_text
    return ""


@dataclass(frozen=True)
class Track:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    isrc: str = ""
    song_id: str = ""
    navidrome_id: str = ""
    library_id: int = 0
    embedded_lyrics: str = ""


def inspect_track(path: Path, seed: Track | None = None) -> Track:
    title = seed.title if seed else ""
    artist = seed.artist if seed else ""
    album = seed.album if seed else ""
    duration = seed.duration if seed else 0.0
    isrc = seed.isrc if seed else ""
    song_id = seed.song_id if seed else ""
    navidrome_id = seed.navidrome_id if seed else ""
    library_id = seed.library_id if seed else 0
    embedded_lyrics = seed.embedded_lyrics if seed else ""
    if path.suffix.casefold() in {".m4a", ".mp4", ".m4v"} and MP4 is not None:
        try:
            media = MP4(path)
            tags = media.tags or {}
            title = title or value_from_tags(tags, ["©nam"])
            artist = artist or value_from_tags(tags, ["©ART", "aART"])
            album = album or value_from_tags(tags, ["©alb"])
            song_id_value = value_from_tags(tags, ["cnID"])
            song_id_match = SONG_ID_RE.search(song_id_value)
            song_id = song_id or (song_id_match.group(0) if song_id_match else "")
            xid = value_from_tags(tags, ["xid "])
            isrc_match = ISRC_RE.search(xid)
            isrc = isrc or (isrc_match.group(0).upper() if isrc_match else "")
            duration = duration or float(getattr(media.info, "length", 0.0) or 0.0)
        except Exception:
            pass
    elif MutagenFile is not None:
        try:
            media = MutagenFile(path, easy=False)
            tags = getattr(media, "tags", None) or {}
            title = title or value_from_tags(tags, ["title", "TIT2"])
            artist = artist or value_from_tags(tags, ["artist", "TPE1"])
            album = album or value_from_tags(tags, ["album", "TALB"])
            raw_song_id = value_from_tags(
                tags,
                ["song_id", "songid", "cnid", "----:com.apple.iTunes:cnID"],
            )
            song_id_match = SONG_ID_RE.search(raw_song_id)
            song_id = song_id or (song_id_match.group(0) if song_id_match else "")
            raw_isrc = value_from_tags(
                tags,
                ["isrc", "TSRC", "----:com.apple.iTunes:ISRC"],
            )
            isrc_match = ISRC_RE.search(raw_isrc)
            isrc = isrc or (isrc_match.group(0).upper() if isrc_match else "")
            duration = duration or float(
                getattr(getattr(media, "info", None), "length", 0.0) or 0.0
            )
        except Exception:
            pass
    return Track(
        path=path,
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        isrc=isrc,
        song_id=song_id,
        navidrome_id=navidrome_id,
        library_id=library_id,
        embedded_lyrics=embedded_lyrics,
    )


def storefront_from_url(url: str) -> str:
    try:
        parts = [part for part in urllib.parse.urlparse(url).path.split("/") if part]
    except ValueError:
        return ""
    return parts[0].lower() if parts and re.fullmatch(r"[a-zA-Z]{2}", parts[0]) else ""


def song_id_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return ""
    query = urllib.parse.parse_qs(parsed.query)
    for value in reversed(query.get("i") or []):
        match = SONG_ID_RE.search(value)
        if match:
            return match.group(0)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2].casefold() in {"song", "music-video", "music-videos"}:
        match = SONG_ID_RE.search(parts[-1])
        return match.group(0) if match else ""
    return ""


class RateLimiter:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, interval_seconds)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval_seconds
        if delay:
            time.sleep(delay)


@dataclass(frozen=True)
class AppleMatch:
    song_id: str
    storefront: str
    confidence: str
    title_similarity: float = 0.0
    artist_similarity: float = 0.0
    duration_delta: float | None = None


class AppleMusicClient:
    def __init__(
        self,
        *,
        wrapper_url: str,
        storefronts: Sequence[str],
        timeout: float,
        request_interval: float,
    ) -> None:
        self.wrapper_url = wrapper_url.rstrip("/")
        self.storefronts = tuple(
            dict.fromkeys(
                storefront.lower()
                for storefront in storefronts
                if re.fullmatch(r"[a-zA-Z]{2}", storefront)
            )
        ) or ("cn", "us")
        self.timeout = max(5.0, timeout)
        self.limiter = RateLimiter(request_interval)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.auth_lock = threading.Lock()
        self.developer_token = ""
        self.media_user_token = ""
        self.load_auth()

    def load_auth(self) -> None:
        request = urllib.request.Request(
            f"{self.wrapper_url}/me",
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            payload = json.loads(response.read(1_048_577))
        auth = payload.get("auth") or {}
        developer_token = str(payload.get("dev_token") or auth.get("dev_token") or "")
        media_user_token = str(auth.get("music_user_token") or "")
        if not developer_token or not media_user_token:
            raise RuntimeError("wrapper account response is missing Apple Music credentials")
        with self.auth_lock:
            self.developer_token = developer_token
            self.media_user_token = media_user_token

    def headers(self) -> dict[str, str]:
        with self.auth_lock:
            developer_token = self.developer_token
            media_user_token = self.media_user_token
        return {
            "Authorization": f"Bearer {developer_token}",
            "media-user-token": media_user_token,
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "User-Agent": DEFAULT_USER_AGENT,
        }

    def request_json(self, url: str) -> tuple[int, dict[str, Any]]:
        last_error: Exception | None = None
        refreshed = False
        for attempt in range(1, 4):
            self.limiter.wait()
            request = urllib.request.Request(url, headers=self.headers())
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read(2_097_153)
                    if len(body) > 2_097_152:
                        raise RuntimeError("Apple Music response exceeded 2 MiB")
                    return int(response.status), json.loads(body)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                exc.read(2_097_153)
                if status in {401, 403} and not refreshed:
                    self.load_auth()
                    refreshed = True
                    continue
                if status == 404:
                    return status, {}
                if status != 429 and status < 500:
                    return status, {}
                last_error = exc
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < 3:
                time.sleep(float(2 ** (attempt - 1)))
        if last_error is not None:
            raise RuntimeError(f"Apple Music API request failed: {type(last_error).__name__}")
        return 599, {}

    def ordered_storefronts(self, hint: str = "") -> tuple[str, ...]:
        values = [hint.lower()] if re.fullmatch(r"[a-zA-Z]{2}", hint or "") else []
        values.extend(self.storefronts)
        return tuple(dict.fromkeys(values))

    def fetch_word_ttml(
        self,
        song_id: str,
        *,
        storefront_hint: str = "",
    ) -> tuple[str, str]:
        for storefront in self.ordered_storefronts(storefront_hint):
            url = (
                f"{APPLE_API_BASE}/catalog/{storefront}/songs/"
                f"{urllib.parse.quote(song_id)}/syllable-lyrics"
            )
            status, payload = self.request_json(url)
            if status == 404:
                continue
            if status != 200:
                continue
            data = payload.get("data") or []
            attributes = (data[0].get("attributes") or {}) if data else {}
            ttml = str(
                attributes.get("ttmlLocalizations")
                or attributes.get("ttml")
                or ""
            )
            if ttml.strip():
                return storefront, ttml
        return "", ""

    def search(self, track: Track, *, storefront_hint: str = "") -> AppleMatch | None:
        query = " ".join(value for value in (track.title, track.artist) if value).strip()
        if not query or not track.title:
            return None
        best: tuple[float, AppleMatch] | None = None
        for storefront in self.ordered_storefronts(storefront_hint)[:4]:
            params = urllib.parse.urlencode(
                {"types": "songs", "term": query, "limit": "25"}
            )
            status, payload = self.request_json(
                f"{APPLE_API_BASE}/catalog/{storefront}/search?{params}"
            )
            if status != 200:
                continue
            songs = (((payload.get("results") or {}).get("songs") or {}).get("data") or [])
            for song in songs:
                attributes = song.get("attributes") or {}
                if attributes.get("hasLyrics") is False:
                    continue
                candidate_id = str(song.get("id") or "")
                if not SONG_ID_RE.fullmatch(candidate_id):
                    continue
                title_ratio = similarity(track.title, attributes.get("name") or "")
                artist_ratio = similarity(track.artist, attributes.get("artistName") or "")
                duration_ms = attributes.get("durationInMillis")
                try:
                    duration_delta = (
                        abs(float(duration_ms) / 1000.0 - track.duration)
                        if duration_ms is not None and track.duration > 0
                        else None
                    )
                except (TypeError, ValueError):
                    duration_delta = None
                candidate_isrc = str(attributes.get("isrc") or "").upper()
                exact_isrc = bool(
                    track.isrc
                    and candidate_isrc
                    and track.isrc.upper() == candidate_isrc
                )
                strict_metadata = (
                    title_ratio >= 0.92
                    and artist_ratio >= 0.78
                    and duration_delta is not None
                    and duration_delta <= 3.0
                )
                if not exact_isrc and not strict_metadata:
                    continue
                confidence = "exact_isrc" if exact_isrc else "strict_metadata"
                score = (
                    (4.0 if exact_isrc else 0.0)
                    + title_ratio * 2.0
                    + artist_ratio
                    + (max(0.0, 3.0 - (duration_delta or 3.0)) / 3.0)
                )
                match = AppleMatch(
                    song_id=candidate_id,
                    storefront=storefront,
                    confidence=confidence,
                    title_similarity=round(title_ratio, 6),
                    artist_similarity=round(artist_ratio, 6),
                    duration_delta=(
                        round(duration_delta, 6) if duration_delta is not None else None
                    ),
                )
                if best is None or score > best[0]:
                    best = (score, match)
            if best is not None and best[1].confidence == "exact_isrc":
                break
        return best[1] if best else None


@dataclass(frozen=True)
class Result:
    media_path: str
    song_id: str
    status: str
    reason: str
    storefront: str = ""
    identity_confidence: str = ""
    sidecar_path: str = ""
    backup_path: str = ""
    reference_path: str = ""
    candidate_sha256: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class AuditStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "word-lyrics.sqlite3"
        self.lock = threading.Lock()
        with self.connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS lyric_results (
                    media_path TEXT PRIMARY KEY,
                    song_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    storefront TEXT NOT NULL DEFAULT '',
                    identity_confidence TEXT NOT NULL DEFAULT '',
                    sidecar_path TEXT NOT NULL DEFAULT '',
                    backup_path TEXT NOT NULL DEFAULT '',
                    reference_path TEXT NOT NULL DEFAULT '',
                    candidate_sha256 TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                )
                """
            )
            database.execute(
                "CREATE INDEX IF NOT EXISTS lyric_results_status ON lyric_results(status)"
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def record(self, result: Result) -> None:
        with self.lock, self.connect() as database:
            database.execute(
                """
                INSERT INTO lyric_results(
                    media_path, song_id, status, reason, storefront,
                    identity_confidence, sidecar_path, backup_path,
                    reference_path, candidate_sha256, metrics_json,
                    error, attempts, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(media_path) DO UPDATE SET
                    song_id=excluded.song_id,
                    status=excluded.status,
                    reason=excluded.reason,
                    storefront=excluded.storefront,
                    identity_confidence=excluded.identity_confidence,
                    sidecar_path=excluded.sidecar_path,
                    backup_path=excluded.backup_path,
                    reference_path=excluded.reference_path,
                    candidate_sha256=excluded.candidate_sha256,
                    metrics_json=excluded.metrics_json,
                    error=excluded.error,
                    attempts=lyric_results.attempts + 1,
                    updated_at=excluded.updated_at
                """,
                (
                    result.media_path,
                    result.song_id,
                    result.status,
                    result.reason,
                    result.storefront,
                    result.identity_confidence,
                    result.sidecar_path,
                    result.backup_path,
                    result.reference_path,
                    result.candidate_sha256,
                    json.dumps(result.metrics, ensure_ascii=False, sort_keys=True),
                    result.error,
                    time.time(),
                ),
            )

    def status_for(self, media_path: Path) -> str:
        with self.lock, self.connect() as database:
            row = database.execute(
                "SELECT status FROM lyric_results WHERE media_path = ?",
                (str(media_path),),
            ).fetchone()
        return str(row[0]) if row else ""

    def attempts_for(self, media_path: Path) -> int:
        with self.lock, self.connect() as database:
            row = database.execute(
                "SELECT attempts FROM lyric_results WHERE media_path = ?",
                (str(media_path),),
            ).fetchone()
        return int(row[0]) if row else 0

    def summary(self) -> dict[str, int]:
        with self.lock, self.connect() as database:
            rows = database.execute(
                "SELECT status, COUNT(*) FROM lyric_results GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}


class WordLyricsService:
    def __init__(
        self,
        *,
        state_dir: Path,
        wrapper_url: str,
        storefronts: Sequence[str],
        timeout: float,
        request_interval: float,
    ) -> None:
        self.state_dir = state_dir
        self.audit = AuditStore(state_dir)
        self.client_initialization_error = ""
        try:
            self.client = AppleMusicClient(
                wrapper_url=wrapper_url,
                storefronts=storefronts,
                timeout=timeout,
                request_interval=request_interval,
            )
        except Exception as exc:
            # Preserve local/cache processing and durable per-track fallback
            # when the account service is temporarily unavailable.
            self.client = None
            self.client_initialization_error = type(exc).__name__

    def reference_documents(self, media_path: Path) -> list[tuple[Path, LyricsDocument]]:
        documents: list[tuple[Path, LyricsDocument]] = []
        for suffix in (".lrc", ".ttml"):
            path = media_path.with_suffix(suffix)
            if not path.is_file():
                continue
            try:
                document = read_lyrics_document(path)
            except (OSError, ET.ParseError, ValueError):
                continue
            if document.is_word_synced:
                continue
            documents.append((path, document))
        return documents

    def existing_word_sidecar(self, media_path: Path, *, duration: float = 0.0) -> tuple[Path, LyricsDocument] | None:
        for suffix in (".ttml", ".elrc"):
            path = media_path.with_suffix(suffix)
            if not path.is_file():
                continue
            try:
                document = read_lyrics_document(path)
                if document.is_word_synced:
                    validate_word_timing(path.read_text(encoding="utf-8", errors="strict"), document, duration=duration)
            except (OSError, ET.ParseError, ValueError):
                continue
            if document.is_word_synced:
                return path, document
        return None

    def backup_existing(self, target: Path, media_path: Path) -> str:
        if not target.is_file():
            return ""
        content_hash = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
        path_hash = hashlib.sha256(str(media_path).encode()).hexdigest()[:16]
        destination = (
            self.state_dir
            / "backups"
            / path_hash
            / f"{target.stem}.line-{content_hash}{target.suffix}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(target, destination)
        return str(destination)

    def save_rejected(
        self,
        media_path: Path,
        song_id: str,
        text: str,
        *,
        suffix: str = ".ttml",
    ) -> str:
        path_hash = hashlib.sha256(str(media_path).encode()).hexdigest()[:16]
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        destination = (
            self.state_dir
            / "rejected"
            / f"{song_id or 'unknown'}-{path_hash}{suffix}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, destination)
        return str(destination)

    def write_text_sidecar(
        self,
        media_path: Path,
        text: str,
        *,
        suffix: str,
    ) -> tuple[str, str]:
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        target = media_path.with_suffix(suffix)
        backup_paths = [self.backup_existing(target, media_path)]
        temporary = target.with_name(f".{target.name}.word-lyrics.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o664)
        try:
            stat = media_path.stat()
            os.chown(temporary, stat.st_uid, stat.st_gid)
        except (OSError, PermissionError):
            pass
        os.replace(temporary, target)
        if suffix.casefold() == ".elrc":
            line_ttml = media_path.with_suffix(".ttml")
            if line_ttml.is_file():
                try:
                    document = read_lyrics_document(line_ttml)
                except (OSError, ET.ParseError, ValueError):
                    document = None
                if document is not None and not document.is_word_synced:
                    backup_paths.append(self.backup_existing(line_ttml, media_path))
                    line_ttml.unlink()
        return str(target), ";".join(path for path in backup_paths if path)

    def write_sidecar(self, media_path: Path, ttml: str) -> tuple[str, str]:
        return self.write_text_sidecar(media_path, ttml, suffix=".ttml")

    def rollback_sidecar(
        self,
        sidecar_path: str | Path,
        backup_path: str,
        *,
        expected_sha256: str,
    ) -> bool:
        """Restore a prior sidecar only while the current file is our write."""
        target = Path(sidecar_path)
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256:
            return False
        if not backup_path:
            target.unlink()
            return True
        descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.rollback-")
        os.close(descriptor)
        try:
            shutil.copy2(backup_path, temporary_name)
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256:
                return False
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return True

    def write_verified_native_sidecar(
        self,
        media_path: Path,
        ttml: str,
        expected_fingerprint: dict[str, Any],
    ) -> tuple[str, str]:
        from word_lyrics_automation.fingerprint import media_fingerprint

        if media_fingerprint(media_path) != expected_fingerprint:
            raise NativeAudioChanged("audio changed during native lyric retrieval")
        sidecar_path, backup_path = self.write_sidecar(media_path, ttml)
        try:
            if media_fingerprint(media_path) != expected_fingerprint:
                raise NativeAudioChanged("audio changed during native lyric publication")
        except Exception:
            # Restore only our own write; do not remove a concurrent publisher's
            # replacement. Original sidecar backups are retained as evidence.
            self.rollback_sidecar(sidecar_path, backup_path,
                expected_sha256=hashlib.sha256(ttml.encode("utf-8")).hexdigest())
            raise
        return sidecar_path, backup_path

    def normalize_character_sidecar(self, media_path: Path) -> dict[str, Any] | None:
        if not env_flag("AMDL_HAN_CHARACTER_TIMING_ENABLED", True):
            return None
        from word_lyrics_automation.character_timing import subdivide_han_word_cues
        from word_lyrics_automation.fingerprint import media_fingerprint
        from word_lyrics_automation.io import atomic_write_json

        target = media_path.with_suffix(".ttml")
        if not target.is_file():
            return None
        original_bytes = target.read_bytes()
        original = original_bytes.decode("utf-8-sig", errors="strict")
        if not parse_ttml(original).is_word_synced:
            return None
        normalized, report = subdivide_han_word_cues(original)
        if not report["changed_groups"]:
            return None
        fingerprint = media_fingerprint(media_path)
        original_digest = hashlib.sha256(original_bytes).hexdigest()
        if hashlib.sha256(target.read_bytes()).hexdigest() != original_digest:
            raise RuntimeError("lyric changed during character adaptation")
        sidecar_path, backup_path = self.write_sidecar(media_path, normalized)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        try:
            if media_fingerprint(media_path) != fingerprint:
                raise RuntimeError("audio changed during character adaptation")
            document = parse_ttml(normalized)
            evidence_path = self.state_dir / "automation" / "character-timing" / (digest + ".json")
            evidence = {**report, "source_fingerprint": fingerprint,
                        "original_sha256": original_digest, "candidate_sha256": digest,
                        "sidecar_path": sidecar_path, "backup_path": backup_path}
            atomic_write_json(evidence_path, evidence)
        except Exception:
            # Roll back just this derived lyric, retaining the original backup.
            self.rollback_sidecar(sidecar_path, backup_path, expected_sha256=digest)
            raise
        return {"sidecar_path": sidecar_path, "backup_path": backup_path,
                "candidate_sha256": digest, "source_candidate_sha256": original_digest,
                "character_timing_method": report["method"],
                "character_internal_boundaries_estimated": True,
                "character_changed_groups": report["changed_groups"],
                "character_added_cues": report["added_cues"],
                "character_skipped_groups": len(report["skipped"]),
                "character_timing_evidence_path": str(evidence_path),
                "timed_words": document.timed_spans, "timed_lines": len(document.lines)}

    def process(
        self,
        track: Track,
        *,
        source_url: str = "",
        force: bool = False,
    ) -> Result:
        media_path = track.path
        try:
            if not media_path.is_file():
                return Result(str(media_path), track.song_id, "missing", "media_file_missing")
            from word_lyrics_automation.fingerprint import media_fingerprint

            expected_fingerprint = media_fingerprint(media_path)
            track = inspect_track(media_path, track)
            existing_word = self.existing_word_sidecar(media_path, duration=track.duration)
            if existing_word is not None and not force:
                path, document = existing_word
                if media_fingerprint(media_path) != expected_fingerprint:
                    raise NativeAudioChanged("audio changed during cached lyric inspection")
                return Result(
                    str(media_path),
                    track.song_id,
                    "cached",
                    "valid_word_sidecar_already_exists",
                    sidecar_path=str(path),
                    metrics={
                        "timed_words": document.timed_spans,
                        "timed_lines": len(document.lines),
                    },
                )
            if self.client is None:
                return Result(
                    str(media_path), track.song_id, "error",
                    "apple_wrapper_initialization_failed",
                    error=self.client_initialization_error,
                )
            storefront_hint = storefront_from_url(source_url)
            candidate_id = track.song_id
            identity_confidence = "exact_catalog_id" if candidate_id else ""
            matched_storefront = storefront_hint
            match: AppleMatch | None = None
            if not candidate_id:
                match = self.client.search(track, storefront_hint=storefront_hint)
                if match is None:
                    return Result(
                        str(media_path),
                        "",
                        "not_available",
                        "no_strict_apple_music_match",
                    )
                candidate_id = match.song_id
                identity_confidence = match.confidence
                matched_storefront = match.storefront
            storefront, ttml = self.client.fetch_word_ttml(
                candidate_id,
                storefront_hint=matched_storefront,
            )
            if not ttml:
                return Result(
                    str(media_path),
                    candidate_id,
                    "not_available",
                    "apple_music_has_no_word_lyrics",
                    storefront=matched_storefront,
                    identity_confidence=identity_confidence,
                    metrics=(asdict(match) if match else {}),
                )
            digest = hashlib.sha256(ttml.encode()).hexdigest()
            try:
                candidate = parse_ttml(ttml)
            except (ET.ParseError, ValueError) as exc:
                return Result(
                    str(media_path),
                    candidate_id,
                    "error",
                    "invalid_apple_ttml",
                    storefront=storefront,
                    identity_confidence=identity_confidence,
                    candidate_sha256=digest,
                    error=type(exc).__name__,
                )
            if not candidate.is_word_synced or candidate.timed_spans < 2:
                return Result(
                    str(media_path),
                    candidate_id,
                    "not_available",
                    "apple_returned_no_word_timing",
                    storefront=storefront,
                    identity_confidence=identity_confidence,
                    candidate_sha256=digest,
                    metrics={
                        "timing": candidate.timing,
                        "timed_words": candidate.timed_spans,
                        "timed_lines": len(candidate.lines),
                    },
                )
            try:
                validate_word_timing(ttml, candidate, duration=track.duration)
            except (ET.ParseError, ValueError) as exc:
                rejected_path = self.save_rejected(media_path, candidate_id, ttml)
                return Result(
                    str(media_path), candidate_id, "rejected", "invalid_apple_word_timing",
                    storefront=storefront, identity_confidence=identity_confidence,
                    candidate_sha256=digest, metrics={"rejected_cache_path": rejected_path},
                    error=type(exc).__name__,
                )
            references = self.reference_documents(media_path)
            validation = validate_candidate(
                candidate,
                references,
                exact_identity=bool(track.song_id),
                identity_confidence=identity_confidence,
                duration=track.duration,
            )
            metrics = dict(validation.metrics)
            metrics.update(
                {
                    "timed_words": candidate.timed_spans,
                    "timed_lines": len(candidate.lines),
                    "identity": asdict(match) if match else {"confidence": identity_confidence},
                }
            )
            if not validation.accepted:
                rejected_path = self.save_rejected(media_path, candidate_id, ttml)
                metrics["rejected_cache_path"] = rejected_path
                return Result(
                    str(media_path),
                    candidate_id,
                    "rejected",
                    validation.reason,
                    storefront=storefront,
                    identity_confidence=identity_confidence,
                    reference_path=validation.reference_path,
                    candidate_sha256=digest,
                    metrics=metrics,
                )
            sidecar_path, backup_path = self.write_verified_native_sidecar(media_path, ttml, expected_fingerprint)
            return Result(
                str(media_path),
                candidate_id,
                "accepted",
                validation.reason,
                storefront=storefront,
                identity_confidence=identity_confidence,
                sidecar_path=sidecar_path,
                backup_path=backup_path,
                reference_path=validation.reference_path,
                candidate_sha256=digest,
                metrics=metrics,
            )
        except NativeAudioChanged as exc:
            return Result(
                str(media_path), track.song_id, "error", "audio_changed_during_native_lyrics",
                error=type(exc).__name__,
            )
        except Exception as exc:
            return Result(
                str(media_path),
                track.song_id,
                "error",
                "unexpected_error",
                error=type(exc).__name__,
            )


def service_from_environment() -> WordLyricsService:
    storefronts = [
        item.strip()
        for item in env(
            "AMDL_WORD_LYRICS_STOREFRONTS",
            "cn,hk,us,jp,tw,sg,gb",
        ).split(",")
        if item.strip()
    ]
    return WordLyricsService(
        state_dir=Path(env("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics")),
        wrapper_url=env("WRAPPER_URL", "http://wrapper"),
        storefronts=storefronts,
        timeout=float(env("AMDL_WORD_LYRICS_TIMEOUT_SECONDS", "30") or "30"),
        request_interval=float(
            env("AMDL_WORD_LYRICS_REQUEST_INTERVAL_SECONDS", "0.20") or "0.20"
        ),
    )


def enqueue_completion_job(
    media_path: str | Path,
    *,
    source_url: str = "",
    initial_status: str = "",
    initial_reason: str = "",
    notification_context: dict[str, Any] | None = None,
    require_notification: bool = False,
) -> bool:
    """Queue a non-destructive acoustic/source fallback for a lyric miss.

    The Telegram bot remains lightweight: it only records the path here.  A
    separate aligner service owns the Whisper model and consumes this queue.
    Completed and confirmed-instrumental rows are never reset accidentally by
    a repeated download notification.
    """
    path = Path(media_path)
    if not path.is_file():
        if require_notification:
            raise FileNotFoundError('lyric media is missing')
        return False
    state_dir = Path(env("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics"))
    if env_flag("AMDL_WORD_LYRICS_AUTOMATION_ENABLED", False):
        try:
            from word_lyrics_automation.queue import enqueue_media_job

            track = inspect_track(path)
            metadata: dict[str, Any] = {
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "duration": track.duration,
                "isrc": track.isrc,
                "song_id": track.song_id,
                "navidrome_id": track.navidrome_id,
            }
            if notification_context:
                metadata["notification"] = {
                    key: str(notification_context.get(key) or "")
                    for key in ("source", "chat_id", "message_id", "origin_message_id", "task_id", "batch_id")
                }
            queued = enqueue_media_job(
                path,
                state_dir=state_dir,
                source_url=source_url,
                metadata=metadata,
                initial_status=initial_status,
                initial_reason=initial_reason,
            )
            return queued.created or queued.job.status not in {"completed", "dead_letter", "superseded"}
        except Exception as exc:
            if require_notification:
                raise
            print(
                "[word-lyrics-automation] enqueue failed "
                f"path={path} error={type(exc).__name__}:{exc}",
                flush=True,
            )
            return False
    if not env_flag("AMDL_WORD_LYRICS_COMPLETION_ENABLED", False):
        return False
    state_dir.mkdir(parents=True, exist_ok=True)
    database_path = state_dir / "completion.sqlite3"
    now = time.time()
    with sqlite3.connect(database_path, timeout=30) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS completion_jobs (
                media_path TEXT PRIMARY KEY,
                source_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                initial_status TEXT NOT NULL DEFAULT '',
                initial_reason TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        database.execute(
            """
            INSERT INTO completion_jobs(
                media_path, source_url, status, initial_status,
                initial_reason, created_at, updated_at
            ) VALUES (?, ?, 'pending', ?, ?, ?, ?)
            ON CONFLICT(media_path) DO UPDATE SET
                source_url=CASE
                    WHEN excluded.source_url <> '' THEN excluded.source_url
                    ELSE completion_jobs.source_url
                END,
                initial_status=excluded.initial_status,
                initial_reason=excluded.initial_reason,
                status=CASE
                    WHEN completion_jobs.status IN ('generated', 'instrumental')
                    THEN completion_jobs.status
                    ELSE 'pending'
                END,
                last_error='',
                updated_at=excluded.updated_at
            """,
            (
                str(path),
                source_url,
                initial_status,
                initial_reason,
                now,
                now,
            ),
        )
    return True


def sync_word_lyrics_for_paths(
    paths: Iterable[str | Path],
    *,
    source_url: str = "",
    logger: Callable[[str], None] | None = None,
    notification_context: dict[str, Any] | None = None,
) -> dict[str, int]:
    if not env_flag("AMDL_WORD_LYRICS_ENABLED", True):
        return {"disabled": 1}
    paths = list(dict.fromkeys(str(Path(path).resolve()) for path in paths))
    context = dict(notification_context or {})
    message_store = None
    batch_key = ""
    if (paths and context.get("source") == "telegram" and context.get("chat_id")
            and env_flag("AMDL_WORD_LYRICS_AUTOMATION_ENABLED", False)):
        from word_lyrics_automation.lyric_messages import LyricMessageStore
        context["batch_id"] = hashlib.sha256(json.dumps(
            [source_url, sorted(paths)], ensure_ascii=False).encode()).hexdigest()
        message_store = LyricMessageStore(
            Path(env("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics")) / "automation/queue.sqlite3")
        batch_key = message_store.prepare(context, paths)
    def safe_log(message):
        if logger is not None:
            try:
                logger(message)
            except Exception:
                pass

    counts: dict[str, int] = {}
    queued = 0
    try:
        # Construct the service inside the per-track boundary: a setup or
        # provider failure must not silently discard the rest of an album.
        service = None
        for raw_path in paths:
            try:
                if service is None:
                    service = service_from_environment()
                result = service.process(Track(path=Path(raw_path)), source_url=source_url)
                if result.status in {"accepted", "cached"}:
                    try:
                        characters = service.normalize_character_sidecar(Path(raw_path))
                        if characters:
                            result = replace(result, sidecar_path=characters["sidecar_path"],
                                backup_path=";".join(p for p in (result.backup_path, characters["backup_path"]) if p),
                                candidate_sha256=characters["candidate_sha256"],
                                metrics={**result.metrics, **characters})
                            counts["character_normalized"] = counts.get("character_normalized", 0) + 1
                            counts["character_added_cues"] = counts.get("character_added_cues", 0) + characters["character_added_cues"]
                    except Exception as exc:
                        counts["character_errors"] = counts.get("character_errors", 0) + 1
                        if logger is not None:
                            safe_log("[apple-character-lyrics] adaptation failed " + type(exc).__name__)
                service.audit.record(result)
                counts[result.status] = counts.get(result.status, 0) + 1
                # Native and cached successes use the same durable completion
                # path. The source worker validates the existing sidecar and
                # scans it; no acoustic inference is requested for valid words.
                registered = enqueue_completion_job(
                    raw_path, source_url=source_url, initial_status=result.status,
                    initial_reason=result.reason, notification_context=context,
                    require_notification=bool(batch_key))
                if registered and result.status not in {"accepted", "cached", "missing"}:
                    queued += 1
                if logger is not None:
                    safe_log("[apple-word-lyrics] "
                        f"status={result.status} reason={result.reason} "
                        f"song_id={result.song_id or '-'} path={result.media_path}")
            except Exception as exc:
                # Preserve a real background recovery opportunity even when
                # the synchronous Apple service or its audit database failed.
                counts["error"] = counts.get("error", 0) + 1
                try:
                    registered = enqueue_completion_job(
                        raw_path, source_url=source_url, initial_status="error",
                        initial_reason="sync_entry_failed:" + type(exc).__name__,
                        notification_context=context, require_notification=bool(batch_key))
                    if registered:
                        queued += 1
                except Exception as enqueue_error:
                    if message_store and batch_key:
                        message_store.registration_failed(batch_key, Path(raw_path), enqueue_error)
                if logger is not None:
                    safe_log("[apple-word-lyrics] sync entry failed " + type(exc).__name__)
    finally:
        if message_store:
            try:
                message_store.finish(batch_key)
            except Exception as exc:
                # Every committed job still owns its real outcome. The
                # observer can infer collection completion from the fixed
                # registered path set; a queue-card marker failure is not a
                # per-song processing failure.
                safe_log("[apple-word-lyrics] queue marker deferred " + type(exc).__name__)
    if queued:
        counts["queued"] = queued
    return counts


def navidrome_tracks(database_path: Path) -> list[Track]:
    uri = f"file:{urllib.parse.quote(str(database_path))}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as database:
        rows = database.execute(
            """
            SELECT mf.id, mf.path, mf.title, mf.artist, mf.album, mf.duration,
                   mf.library_id, library.path, mf.lyrics
            FROM media_file AS mf
            JOIN library ON library.id = mf.library_id
            WHERE mf.missing = 0
            ORDER BY mf.library_id, mf.path
            """
        ).fetchall()
    tracks: list[Track] = []
    for (
        navidrome_id,
        relative_path,
        title,
        artist,
        album,
        duration,
        library_id,
        root,
        embedded_lyrics,
    ) in rows:
        path = Path(str(root)) / str(relative_path)
        if path.suffix.casefold() not in AUDIO_SUFFIXES:
            continue
        tracks.append(
            Track(
                path=path,
                title=str(title or ""),
                artist=str(artist or ""),
                album=str(album or ""),
                duration=float(duration or 0.0),
                navidrome_id=str(navidrome_id or ""),
                library_id=int(library_id or 0),
                embedded_lyrics=str(embedded_lyrics or "[]"),
            )
        )
    return tracks


def parse_navidrome_embedded_lyrics(
    raw: str,
) -> tuple[LyricsDocument | None, str]:
    try:
        entries = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, ""
    if not isinstance(entries, list):
        return None, ""
    all_text: list[str] = []
    best: LyricsDocument | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_lines = entry.get("line") or []
        if not isinstance(raw_lines, list):
            continue
        if not entry.get("synced"):
            all_text.extend(
                str(line.get("value") or "")
                for line in raw_lines
                if isinstance(line, dict)
            )
            continue
        timed_lines: list[TimedLine] = []
        for line in raw_lines:
            if not isinstance(line, dict) or not normalize_text(line.get("value")):
                continue
            try:
                begin = float(line.get("start")) / 1000.0
            except (TypeError, ValueError):
                continue
            value = str(line.get("value") or "")
            matches = list(INLINE_LRC_TIME_RE.finditer(value))
            cursor = 0
            current_begin = begin
            for match in matches:
                text = value[cursor : match.start()].strip()
                if normalize_text(text):
                    timed_lines.append(TimedLine(text=text, begin=current_begin))
                    all_text.append(text)
                current_begin = (
                    float(match.group(1)) * 60.0 + float(match.group(2))
                )
                cursor = match.end()
            text = value[cursor:].strip()
            if normalize_text(text):
                timed_lines.append(TimedLine(text=text, begin=current_begin))
                all_text.append(text)
        timed_lines.sort(key=lambda line: line.begin)
        if len(timed_lines) >= 2 and (best is None or len(timed_lines) > len(best.lines)):
            best = LyricsDocument(
                kind="embedded",
                timing="Line",
                lines=tuple(timed_lines),
            )
    return best, "".join(all_text)


class NavidromeClient:
    def __init__(self, *, base_url: str, username: str, password: str, timeout: float) -> None:
        if not username or not password:
            raise RuntimeError("Navidrome credentials are not configured")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = max(5.0, timeout)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request_lyrics(self, track_id: str) -> tuple[bool, str]:
        last_error = ""
        for attempt in range(1, 4):
            salt = secrets.token_hex(8)
            token = hashlib.md5(
                (self.password + salt).encode(),
                usedforsecurity=False,
            ).hexdigest()
            query = urllib.parse.urlencode(
                {
                    "u": self.username,
                    "t": token,
                    "s": salt,
                    "v": "1.16.1",
                    "c": "word-lyrics-backfill",
                    "f": "json",
                    "id": track_id,
                    "enhanced": "true",
                }
            )
            request = urllib.request.Request(
                f"{self.base_url}/rest/getLyricsBySongId.view?{query}",
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read(2_097_153)
                if len(body) > 2_097_152:
                    return False, "navidrome_response_too_large"
                payload = json.loads(body)
                subsonic = payload.get("subsonic-response") or {}
                if subsonic.get("status") == "ok":
                    return True, ""
                error = subsonic.get("error") or {}
                last_error = f"subsonic_error_{error.get('code', 'unknown')}"
            except urllib.error.HTTPError as exc:
                exc.read(2_097_153)
                last_error = f"http_{exc.code}"
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = type(exc).__name__
            if attempt < 3:
                time.sleep(float(2 ** (attempt - 1)))
        return False, last_error or "navidrome_request_failed"


def stage_candidates(staging_root: Path, track: Track) -> list[Path]:
    return [
        path
        for suffix in (".ttml", ".elrc")
        for path in [staging_root / f"{track.navidrome_id}{suffix}"]
        if path.is_file()
    ]


def stage_artifacts(staging_root: Path, track: Track) -> list[Path]:
    """Return every temporary sidecar nd-lyrics may create for a track."""
    return [
        path
        for suffix in (".ttml", ".elrc", ".lrc", ".srt", ".txt", ".yaml", ".yml")
        for path in [staging_root / f"{track.navidrome_id}{suffix}"]
        if path.is_file()
    ]


def remove_stage_artifacts(staging_root: Path, track: Track) -> None:
    for path in stage_artifacts(staging_root, track):
        try:
            path.unlink()
        except OSError:
            pass


def promote_staged_candidate(
    service: WordLyricsService,
    track: Track,
    candidates: Sequence[Path],
) -> Result:
    inspected = inspect_track(track.path, track)
    parsed: list[tuple[Path, LyricsDocument, str]] = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            document = read_lyrics_document(path)
        except (OSError, ET.ParseError, ValueError):
            continue
        if document.is_word_synced and document.timed_spans >= 2:
            parsed.append((path, document, text))
    if not parsed:
        return Result(
            str(track.path),
            inspected.song_id,
            "not_available",
            "fallback_returned_no_word_timing",
            identity_confidence="nd_lyrics_strict_metadata",
        )
    parsed.sort(
        key=lambda item: (
            0 if item[0].suffix.casefold() == ".ttml" else 1,
            -item[1].timed_spans,
        )
    )
    candidate_path, document, text = parsed[0]
    references = service.reference_documents(track.path)
    embedded_reference, embedded_text = parse_navidrome_embedded_lyrics(
        track.embedded_lyrics
    )
    if embedded_reference is not None:
        references.append(
            (Path(f"navidrome-embedded-{track.navidrome_id}"), embedded_reference)
        )
    validation = validate_candidate(
        document,
        references,
        exact_identity=False,
        identity_confidence="strict_metadata",
        duration=inspected.duration,
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    metrics = dict(validation.metrics)
    if embedded_text:
        metrics["embedded_text_similarity"] = round(
            difflib.SequenceMatcher(
                None,
                document.normalized_text,
                normalize_text(embedded_text),
                autojunk=False,
            ).ratio(),
            6,
        )
    metrics.update(
        {
            "provider": "nd-lyrics",
            "format": candidate_path.suffix.casefold().lstrip("."),
            "timed_words": document.timed_spans,
            "timed_lines": len(document.lines),
        }
    )
    if not validation.accepted:
        rejected_path = service.save_rejected(
            track.path,
            inspected.song_id or track.navidrome_id,
            text,
            suffix=candidate_path.suffix,
        )
        metrics["rejected_cache_path"] = rejected_path
        return Result(
            str(track.path),
            inspected.song_id,
            "rejected",
            f"nd_lyrics_{validation.reason}",
            identity_confidence="nd_lyrics_strict_metadata",
            reference_path=validation.reference_path,
            candidate_sha256=digest,
            metrics=metrics,
        )
    sidecar_path, backup_path = service.write_text_sidecar(
        track.path,
        text,
        suffix=candidate_path.suffix,
    )
    return Result(
        str(track.path),
        inspected.song_id,
        "accepted",
        f"nd_lyrics_{validation.reason}",
        identity_confidence="nd_lyrics_strict_metadata",
        sidecar_path=sidecar_path,
        backup_path=backup_path,
        reference_path=validation.reference_path,
        candidate_sha256=digest,
        metrics=metrics,
    )


def run_fallback(args: argparse.Namespace) -> int:
    service = service_from_environment()
    client = NavidromeClient(
        base_url=env("NAVIDROME_URL", "http://navidrome:4533"),
        username=env("NAVIDROME_USER"),
        password=env("NAVIDROME_PASSWORD"),
        timeout=float(env("AMDL_WORD_LYRICS_NAVIDROME_TIMEOUT_SECONDS", "120") or "120"),
    )
    staging_root = Path(args.staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)
    tracks = navidrome_tracks(Path(args.navidrome_db))
    if args.library_id:
        allowed = set(args.library_id)
        tracks = [track for track in tracks if track.library_id in allowed]
    if args.retry_status:
        retry_statuses = {
            status.strip()
            for raw_value in args.retry_status
            for status in raw_value.split(",")
            if status.strip()
        }
        tracks = [
            track
            for track in tracks
            if service.audit.status_for(track.path) in retry_statuses
        ]
    tracks = [
        track
        for track in tracks
        if service.existing_word_sidecar(track.path) is None
    ]
    if args.limit:
        tracks = tracks[: args.limit]
    total = len(tracks)
    counts: dict[str, int] = {}
    started = time.monotonic()

    def process(track: Track) -> Result:
        remove_stage_artifacts(staging_root, track)
        requested, error = client.request_lyrics(track.navidrome_id)
        candidates = stage_candidates(staging_root, track)
        try:
            # A provider can finish writing immediately after a timed-out API
            # request. Prefer a complete staged candidate if one exists.
            if candidates:
                return promote_staged_candidate(service, track, candidates)
            if not requested:
                return Result(
                    str(track.path),
                    track.song_id,
                    "error",
                    "fallback_request_failed",
                    error=error,
                )
            return Result(
                str(track.path),
                track.song_id,
                "not_available",
                "nd_lyrics_found_no_word_lyrics",
                identity_confidence="nd_lyrics_strict_metadata",
            )
        finally:
            remove_stage_artifacts(staging_root, track)

    workers = max(1, min(8, int(args.workers)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(process, track): track for track in tracks}
        completed = 0
        for future in concurrent.futures.as_completed(future_map):
            completed += 1
            track = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = Result(
                    str(track.path),
                    track.song_id,
                    "error",
                    "fallback_worker_failure",
                    error=type(exc).__name__,
                )
            service.audit.record(result)
            counts[result.status] = counts.get(result.status, 0) + 1
            if completed == 1 or completed % 25 == 0 or completed == total:
                elapsed = max(0.001, time.monotonic() - started)
                print(
                    json.dumps(
                        {
                            "progress": f"{completed}/{total}",
                            "rate_tracks_per_minute": round(completed / elapsed * 60.0, 2),
                            "counts": counts,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
    report = {
        "completed_at": time.time(),
        "selected_tracks": total,
        "run_counts": counts,
        "database_counts": service.audit.summary(),
    }
    report_path = service.state_dir / "latest-fallback-summary.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def run_revalidate(args: argparse.Namespace) -> int:
    service = object.__new__(WordLyricsService)
    service.state_dir = Path(env("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics"))
    service.audit = AuditStore(service.state_dir)
    tracks = {
        str(track.path): track
        for track in navidrome_tracks(Path(args.navidrome_db))
    }
    with service.audit.connect() as database:
        rows = database.execute(
            """
            SELECT media_path, song_id, reason, metrics_json
            FROM lyric_results
            WHERE status = 'rejected'
            ORDER BY media_path
            """
        ).fetchall()
    counts: dict[str, int] = {}
    total = len(rows)
    for completed, (media_path, song_id, previous_reason, raw_metrics) in enumerate(
        rows,
        start=1,
    ):
        track = tracks.get(str(media_path))
        if track is None:
            result = Result(
                str(media_path),
                str(song_id or ""),
                "missing",
                "revalidation_track_missing",
            )
        else:
            try:
                previous_metrics = json.loads(raw_metrics or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                previous_metrics = {}
            rejected_path = Path(str(previous_metrics.get("rejected_cache_path") or ""))
            if not rejected_path.is_file():
                result = Result(
                    str(media_path),
                    str(song_id or ""),
                    "error",
                    "revalidation_cache_missing",
                )
            else:
                try:
                    text = rejected_path.read_text(encoding="utf-8", errors="replace")
                    document = read_lyrics_document(rejected_path)
                    references = service.reference_documents(track.path)
                    embedded_reference, embedded_text = parse_navidrome_embedded_lyrics(
                        track.embedded_lyrics
                    )
                    if embedded_reference is not None:
                        references.append(
                            (
                                Path(f"navidrome-embedded-{track.navidrome_id}"),
                                embedded_reference,
                            )
                        )
                    is_fallback = str(previous_reason).startswith("nd_lyrics_")
                    validation = validate_candidate(
                        document,
                        references,
                        exact_identity=not is_fallback,
                        identity_confidence=(
                            "strict_metadata" if is_fallback else "exact_catalog_id"
                        ),
                        duration=track.duration,
                    )
                    metrics = dict(validation.metrics)
                    metrics.update(
                        {
                            "provider": "nd-lyrics" if is_fallback else "apple_music",
                            "format": rejected_path.suffix.casefold().lstrip("."),
                            "timed_words": document.timed_spans,
                            "timed_lines": len(document.lines),
                            "rejected_cache_path": str(rejected_path),
                            "previous_reason": previous_reason,
                        }
                    )
                    if embedded_text:
                        metrics["embedded_text_similarity"] = round(
                            difflib.SequenceMatcher(
                                None,
                                document.normalized_text,
                                normalize_text(embedded_text),
                                autojunk=False,
                            ).ratio(),
                            6,
                        )
                    if validation.accepted:
                        sidecar_path, backup_path = service.write_text_sidecar(
                            track.path,
                            text,
                            suffix=rejected_path.suffix,
                        )
                        result = Result(
                            str(media_path),
                            str(song_id or ""),
                            "accepted",
                            f"revalidated_{validation.reason}",
                            identity_confidence=(
                                "nd_lyrics_strict_metadata"
                                if is_fallback
                                else "exact_catalog_id"
                            ),
                            sidecar_path=sidecar_path,
                            backup_path=backup_path,
                            reference_path=validation.reference_path,
                            candidate_sha256=hashlib.sha256(text.encode()).hexdigest(),
                            metrics=metrics,
                        )
                    else:
                        result = Result(
                            str(media_path),
                            str(song_id or ""),
                            "rejected",
                            f"revalidated_{validation.reason}",
                            identity_confidence=(
                                "nd_lyrics_strict_metadata"
                                if is_fallback
                                else "exact_catalog_id"
                            ),
                            reference_path=validation.reference_path,
                            candidate_sha256=hashlib.sha256(text.encode()).hexdigest(),
                            metrics=metrics,
                        )
                except Exception as exc:
                    result = Result(
                        str(media_path),
                        str(song_id or ""),
                        "error",
                        "revalidation_failed",
                        error=type(exc).__name__,
                    )
        service.audit.record(result)
        counts[result.status] = counts.get(result.status, 0) + 1
        if completed == 1 or completed % 25 == 0 or completed == total:
            print(
                json.dumps(
                    {"progress": f"{completed}/{total}", "counts": counts},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
    print(
        json.dumps(
            {
                "selected_tracks": total,
                "run_counts": counts,
                "database_counts": service.audit.summary(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def run_backfill(args: argparse.Namespace) -> int:
    service = service_from_environment()
    tracks = navidrome_tracks(Path(args.navidrome_db))
    if args.library_id:
        allowed = set(args.library_id)
        tracks = [track for track in tracks if track.library_id in allowed]
    if args.retry_status:
        retry_statuses = {
            status.strip()
            for raw_value in args.retry_status
            for status in raw_value.split(",")
            if status.strip()
        }
        tracks = [
            track
            for track in tracks
            if service.audit.status_for(track.path) in retry_statuses
        ]
    if args.min_attempts:
        tracks = [
            track
            for track in tracks
            if service.audit.attempts_for(track.path) >= args.min_attempts
        ]
    if args.resume:
        tracks = [
            track
            for track in tracks
            if service.audit.status_for(track.path) not in {"accepted", "cached"}
            or service.existing_word_sidecar(track.path) is None
        ]
    if args.limit:
        tracks = tracks[: args.limit]
    total = len(tracks)
    counts: dict[str, int] = {}
    started = time.monotonic()

    def finish(result: Result, completed: int) -> None:
        service.audit.record(result)
        counts[result.status] = counts.get(result.status, 0) + 1
        if completed == 1 or completed % 25 == 0 or completed == total:
            elapsed = max(0.001, time.monotonic() - started)
            print(
                json.dumps(
                    {
                        "progress": f"{completed}/{total}",
                        "rate_tracks_per_minute": round(completed / elapsed * 60.0, 2),
                        "counts": counts,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )

    workers = max(1, min(12, int(args.workers)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(service.process, track, force=args.force): track
            for track in tracks
        }
        completed = 0
        for future in concurrent.futures.as_completed(future_map):
            completed += 1
            track = future_map[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - process already contains a guard
                result = Result(
                    str(track.path),
                    track.song_id,
                    "error",
                    "worker_failure",
                    error=type(exc).__name__,
                )
            finish(result, completed)
    final_summary = service.audit.summary()
    report = {
        "completed_at": time.time(),
        "selected_tracks": total,
        "run_counts": counts,
        "database_counts": final_summary,
    }
    report_path = service.state_dir / "latest-summary.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def run_sync(args: argparse.Namespace) -> int:
    service = service_from_environment()
    counts: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    for path_value in args.path:
        result = service.process(
            Track(path=Path(path_value)),
            source_url=args.source_url,
            force=args.force,
        )
        service.audit.record(result)
        counts[result.status] = counts.get(result.status, 0) + 1
        output.append(
            {
                "media_path": result.media_path,
                "song_id": result.song_id,
                "status": result.status,
                "reason": result.reason,
                "sidecar_path": result.sidecar_path,
            }
        )
    print(json.dumps({"counts": counts, "results": output}, ensure_ascii=False), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch, validate, cache, and audit Apple Music word-synced TTML lyrics."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--path", action="append", required=True)
    sync_parser.add_argument("--source-url", default="")
    sync_parser.add_argument("--force", action="store_true")
    sync_parser.set_defaults(handler=run_sync)
    backfill_parser = subparsers.add_parser("backfill")
    backfill_parser.add_argument(
        "--navidrome-db",
        default="/navidrome-data/navidrome.db",
    )
    backfill_parser.add_argument("--library-id", action="append", type=int)
    backfill_parser.add_argument("--workers", type=int, default=4)
    backfill_parser.add_argument("--limit", type=int, default=0)
    backfill_parser.add_argument("--resume", action="store_true")
    backfill_parser.add_argument("--retry-status", action="append")
    backfill_parser.add_argument("--min-attempts", type=int, default=0)
    backfill_parser.add_argument("--force", action="store_true")
    backfill_parser.set_defaults(handler=run_backfill)
    fallback_parser = subparsers.add_parser("fallback")
    fallback_parser.add_argument(
        "--navidrome-db",
        default="/navidrome-data/navidrome.db",
    )
    fallback_parser.add_argument(
        "--staging-root",
        required=True,
    )
    fallback_parser.add_argument("--library-id", action="append", type=int)
    fallback_parser.add_argument("--workers", type=int, default=3)
    fallback_parser.add_argument("--limit", type=int, default=0)
    fallback_parser.add_argument("--retry-status", action="append")
    fallback_parser.set_defaults(handler=run_fallback)
    revalidate_parser = subparsers.add_parser("revalidate")
    revalidate_parser.add_argument(
        "--navidrome-db",
        default="/navidrome-data/navidrome.db",
    )
    revalidate_parser.set_defaults(handler=run_revalidate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
