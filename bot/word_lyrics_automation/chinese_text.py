from __future__ import annotations

from ._opencc_t2s import TRADITIONAL_TO_SIMPLIFIED


def fold_chinese_script(value: str) -> str:
    """Fold unambiguous Chinese script variants for comparison only.

    Each replacement is one code point long. Callers must keep the original
    lyric text for rendering and publication; this function is not a display
    converter and never guesses homophones or context-dependent forms.
    """

    return value.translate(TRADITIONAL_TO_SIMPLIFIED)
