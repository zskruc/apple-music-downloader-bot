"""Keep trusted humming/backs in the text without inventing word timings."""
from __future__ import annotations

import re

from .models import LyricLine, TimedToken
from .text import normalize_text


_SYLLABLES = {"na", "la", "da", "du", "lu", "doo", "dum", "ba", "ah", "oh", "ooh",
              "woah", "hey", "ay", "yay", "ha", "mm", "hmm"}
_BRACKET_END = re.compile(r"\s*([（(][^()（）]*[)）])\s*$")


def is_nonlexical(text: str) -> bool:
    # This is deliberately a closed vocabulary, requiring repetition. An
    # ordinary English line or a parenthesized sung sentence is never omitted.
    words = re.findall(r"[A-Za-z]+|[\u3400-\u9fff]", text.casefold())
    if normalize_text("".join(words)) != normalize_text(text) or len(words) < 3:
        return False
    return (all(word in _SYLLABLES for word in words)
            or all(word in "啦嘟噜呜啊哦" for word in words)) and len(set(words)) < len(words)


def split_nonlexical(text: str) -> tuple[str, str]:
    if is_nonlexical(text):
        return "", text
    match = _BRACKET_END.search(text)
    if match and is_nonlexical(match.group(1)) and normalize_text(text[:match.start()]):
        return text[:match.start()], text[match.start():]
    return text, ""


def restore_nonlexical(references: list[dict], lexical: list[LyricLine]) -> tuple[list[LyricLine], dict]:
    output = []
    cursor = 0
    humming = background = 0
    for reference in references:
        text = str(reference['text'])
        lead, tail = split_nonlexical(text)
        begin, end = float(reference['begin']), float(reference['end'])
        if not lead:
            output.append(LyricLine(text, begin, end))
            humming += 1
            continue
        if cursor >= len(lexical):
            raise RuntimeError('missing lexical reference line')
        line = lexical[cursor]
        cursor += 1
        if line.text != lead or not line.tokens:
            raise RuntimeError('lexical reference text was not preserved')
        # Humming/backs keep their native clock, so lexical envelopes must
        # also stay on that clock. Preserve word proportions, not crop padding
        # which can otherwise overlap the next native humming row.
        scale = (end - begin) / (line.end - line.begin)
        if not .4 <= scale <= 2.5:
            raise RuntimeError('lexical timing differs from native line envelope')
        tokens = tuple(TimedToken(token.text, begin + (token.begin - line.begin) * scale,
                                  begin + (token.end - line.begin) * scale) for token in line.tokens)
        output.append(LyricLine(text, begin, end, tokens))
        background += bool(tail)
    if cursor != len(lexical):
        raise RuntimeError('extra lexical reference lines')
    return output, {'word_timed_lines': len(lexical),
                    'line_timed_nonlexical_lines': humming,
                    'line_timed_background_lines': background}


def validate_text_coverage(lines: list[LyricLine]) -> None:
    for line in lines:
        actual = normalize_text(''.join(token.text for token in line.tokens))
        if actual == normalize_text(line.text):
            continue
        lead, tail = split_nonlexical(line.text)
        if not tail or actual != normalize_text(lead):
            raise ValueError('untimed_lexical_text')
