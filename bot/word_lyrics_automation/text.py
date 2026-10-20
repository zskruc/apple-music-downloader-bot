from __future__ import annotations

import re
import unicodedata

from .chinese_text import fold_chinese_script


_CREDIT_RE = re.compile(
    r"^\s*(?:作词|作曲|编曲|制作人|监制|混音|母带|词|曲)\s*[:：]",
    re.IGNORECASE,
)
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
