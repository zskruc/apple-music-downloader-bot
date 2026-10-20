from __future__ import annotations

import re

from .text import normalize_text


_QUALIFIER_RE = re.compile(r"[\(（\[【].*?[\)）\]】]")
_SINGLE_RE = re.compile(r"(?:\s*[-–—]\s*)?single$", re.IGNORECASE)
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:/|、|&|,|，|feat\.?|ft\.?)\s*", re.IGNORECASE)
_SOUNDTRACK_NOTE_RE = re.compile(r"电视剧|電視劇|电影|電影|主题曲|主題曲|片头曲|片頭曲|片尾曲|插曲|推广曲|推廣曲")
_RECORDING_NOTE_RE = re.compile(
    r"\blive\b|remix|instrumental|acoustic|version|现场|現場|演唱会|演唱會|伴奏|纯音乐|純音樂|重录|重錄",
    re.IGNORECASE,
)

# Explicit aliases verified against the label's artist discography. Do not
# transliterate arbitrary artist names: that would merge unrelated performers.
# https://www.sonymusic.co.jp/Music/Info/junkoyamamoto/akaitoridisc.html
_VERIFIED_ARTIST_NAMES = (("The Red Birds", "赤い鳥"),)


def artist_search_names(value: str) -> tuple[str, ...]:
    names = [value]
    normalized = normalize_text(value)
    for group in _VERIFIED_ARTIST_NAMES:
        if normalized in {normalize_text(name) for name in group}:
            names.extend(name for name in group if name not in names)
    return tuple(names)


def canonical_title(value: str) -> str:
    stripped = _SINGLE_RE.sub("", _QUALIFIER_RE.sub("", value)).strip()
    return normalize_text(stripped)


def title_search_names(value: str) -> tuple[str, ...]:
    """Try soundtrack notes separately from the title; keep recording versions.

    These are discovery queries only. Candidates must still pass the original
    title, artist, duration, provenance and acoustic validation gates.
    """
    simplified = _QUALIFIER_RE.sub(
        lambda match: (
            "" if _SOUNDTRACK_NOTE_RE.search(match.group())
            and not _RECORDING_NOTE_RE.search(match.group()) else match.group()
        ),
        value,
    )
    simplified = " ".join(simplified.split())
    if simplified != value and len(normalize_text(simplified)) >= 2:
        return value, simplified
    return (value,)


def title_matches(left: str, right: str) -> bool:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if normalized_left and normalized_left == normalized_right:
        return True
    canonical_left = canonical_title(left)
    canonical_right = canonical_title(right)
    return (
        len(canonical_left) >= 2
        and len(canonical_right) >= 2
        and canonical_left == canonical_right
    )


def artist_components(value: str) -> set[str]:
    output = {
        normalize_text(name)
        for component in _ARTIST_SPLIT_RE.split(value)
        for name in artist_search_names(component)
        if normalize_text(name)
    }
    normalized = normalize_text(value)
    if normalized:
        output.add(normalized)
    return output


def artist_matches(left: str, right: str) -> bool:
    left_components = artist_components(left)
    right_components = artist_components(right)
    if not left_components or not right_components:
        return False
    return bool(left_components & right_components) or any(
        min(len(left_item), len(right_item)) >= 3
        and (left_item in right_item or right_item in left_item)
        for left_item in left_components
        for right_item in right_components
    )
