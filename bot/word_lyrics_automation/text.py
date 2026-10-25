from __future__ import annotations

import re
import unicodedata

from .chinese_text import fold_chinese_script


_CREDIT_RE = re.compile(
    r"^\s*(?:原唱|原曲|作词|作曲|编曲|制作人|监制|混音|母带|词|曲"
    r"|original\s+(?:artist|singer)|lyrics\s+by|composed\s+by)\s*[:：]",
    re.IGNORECASE,
)
_HEADER_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")
_HEADER_NOTE_RE = re.compile(r"[（(][^()（）]*[）)]")
_CREDIT_SEPARATOR_RE = re.compile(r"\s*(?:/|、|&|,|，)\s*")
_CJK_RE = re.compile(
    r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff\u3400-\u9fff]"
)
_LATIN_WORD_RE = re.compile(r"[0-9A-Za-z]+(?:['’][0-9A-Za-z]+)*")


def normalize_text(value: str) -> str:
    """Normalize lyric text for identity checks, not for display."""

    value = fold_chinese_script(unicodedata.normalize("NFKC", value).casefold())
    return "".join(
        character for character in value
        if unicodedata.category(character)[0] in {"L", "N", "M"}
    )


def is_credit_line(value: str) -> bool:
    return bool(_CREDIT_RE.match(value))


def is_lyric_metadata_line(
    value: str, *, title: str = "", artist: str = "", begin: float | None = None,
) -> bool:
    """Recognize credits and early provider title/artist headers, never bare titles.

    Providers may append a recording note or an artist reading in parentheses.
    Numbered group credits such as ``测试组合303测试歌手`` also occur when the
    catalog artist is just ``测试歌手``. Only this bounded decoration is accepted;
    an arbitrary mention of the artist or title is not a metadata match. The
    caller retains the original text and timing for every remaining lyric.
    """
    if is_credit_line(value):
        return True
    if begin is None or not 0 <= begin < 20 or not title or not artist:
        return False

    def identity(text: str) -> str:
        return normalize_text(_HEADER_NOTE_RE.sub("", text))

    expected_title = identity(title)
    expected_artists = {identity(name) for name in _CREDIT_SEPARATOR_RE.split(artist)} - {""}
    if not expected_title or not expected_artists:
        return False
    # Require a full title followed by a spaced header separator and a known
    # artist credit. A sung title, a lyric containing the title, and subsequent
    # reprises must not disappear simply because they resemble catalog text.
    header = unicodedata.normalize("NFKC", value)
    for separator in _HEADER_SEPARATOR_RE.finditer(header):
        if identity(header[:separator.start()]) != expected_title:
            continue
        credits = _CREDIT_SEPARATOR_RE.split(header[separator.end():])
        for credit in credits:
            actual = identity(credit)
            if actual in expected_artists:
                return True
            for expected in expected_artists:
                if len(expected) < 3 or not actual.endswith(expected):
                    continue
                group = actual[:-len(expected)]
                if group and group[-1].isdigit() and any(letter.isalpha() for letter in group):
                    return True
    return False


def display_units(text: str) -> list[str]:
    """Split display text into karaoke units while preserving real spaces."""

    units: list[str] = []
    pending = ""
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            pending += character
            index += 1
            continue
        if _CJK_RE.match(character):
            units.append(pending + character)
            pending = ""
            index += 1
            continue
        match = _LATIN_WORD_RE.match(text, index)
        if match:
            units.append(pending + match.group(0))
            pending = ""
            index = match.end()
            continue
        if units:
            # Pending whitespace preceded this punctuation in the reference.
            # Keep that order when attaching punctuation to the prior unit;
            # carrying the whitespace to the next word would move it across
            # the punctuation ("word (next)" -> "word( next)").
            units[-1] += pending + character
            pending = ""
        else:
            pending += character
        index += 1
    if pending:
        if units:
            units[-1] += pending
        elif normalize_text(pending):
            units.append(pending)
    return [unit for unit in units if normalize_text(unit)]
