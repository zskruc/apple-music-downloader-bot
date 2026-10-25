"""Verify a provider's word clock against every native lexical line."""
from __future__ import annotations

import difflib
import math
import statistics

from .models import LyricLine, TimedToken
from .nonlexical import split_nonlexical, restore_nonlexical
from .text import display_units, normalize_text
from .ttml import render_word_ttml


def fuse_reference_words(references: list[dict], source: list[LyricLine], *,
                         duration: float, title: str, artist: str) -> tuple[str, dict]:
    if not references or not math.isfinite(duration) or duration <= 0:
        raise ValueError('native timed reference is required')
    # Unlabelled third-party word clocks cannot establish which native
    # voice a token belongs to. Keep the complete native text for acoustic
    # alignment instead of flattening overlapping voices into one stream.
    if len({str(line.get('agent') or '') for line in references}) > 1:
        raise ValueError('provider parallel voice identity is missing')
    previous_end = 0.0
    for line in references:
        begin, end = float(line['begin']), float(line['end'])
        if (not all(math.isfinite(value) for value in (begin, end))
                or begin < previous_end - .001 or end <= begin or end > duration + .5):
            raise ValueError('native reference timing is invalid')
        previous_end = end
    lexical = [{**line, 'text': split_nonlexical(line['text'])[0]} for line in references]
    lexical = [line for line in lexical if normalize_text(line['text'])]
    reference_text = ''.join(normalize_text(line['text']) for line in lexical)
    if len(lexical) < 4 or len(reference_text) < 40:
        raise ValueError('insufficient independent reference anchors')
    source_text = ''
    timings = []
    for line in source:
        lead, _ = split_nonlexical(line.text)
        remaining = len(normalize_text(lead))
        for token in line.tokens:
            normalized = normalize_text(token.text)
            for index, character in enumerate(normalized):
                if remaining <= 0:
                    break
                begin = token.begin + (token.end - token.begin) * index / len(normalized)
                end = token.begin + (token.end - token.begin) * (index + 1) / len(normalized)
                if (not all(math.isfinite(value) for value in (begin, end))
                        or begin < 0 or end <= begin or end > duration + .5
                        or (timings and begin < timings[-1][1] - .001)):
                    raise ValueError('source token timing is invalid')
                source_text += character
                timings.append((begin, end))
                remaining -= 1
        if remaining:
            raise ValueError('provider has untimed lexical text')
    if len(source_text) != len(reference_text):
        raise ValueError('provider lexical coverage is incomplete')
    matcher = difflib.SequenceMatcher(None, reference_text, source_text, autojunk=False)
    substitutions = []
    for tag, left, right, source_left, source_right in matcher.get_opcodes():
        if tag == 'equal':
            continue
        # Catalogs sometimes spell one Han syllable differently. Preserve the
        # native text; transfer timing only with >97% body agreement, no
        # insertion/deletion/reordering, and every native line clock verified.
        if (tag != 'replace' or right - left != 1 or source_right - source_left != 1
                or left != source_left
                or not all('\u3400' <= char <= '\u9fff' for char in
                           (reference_text[left], source_text[source_left]))):
            raise ValueError('provider text has an insertion, omission or phrase change')
        substitutions.append(left)
    coverage = 1 - len(substitutions) / len(reference_text)
    if coverage < .97:
        raise ValueError('provider text agreement is insufficient')
    output = []
    cursor = 0
    start_deltas, end_deltas = [], []
    for reference in lexical:
        size = len(normalize_text(reference['text']))
        if sum(cursor <= position < cursor + size for position in substitutions) > 1:
            raise ValueError('multiple substitutions in one native line')
        observed = timings[cursor:cursor + size]
        cursor += size
        begin, end = float(reference['begin']), float(reference['end'])
        source_begin, source_end = observed[0][0], observed[-1][1]
        start_delta, end_delta = abs(begin - source_begin), abs(end - source_end)
        if (start_delta > .85 or end_delta > 1.25
                or not .5 <= (source_end - source_begin) / (end - begin) <= 1.5):
            raise ValueError('provider word clock differs from the native recording')
        start_deltas.append(start_delta)
        end_deltas.append(end_delta)
        scale = (end - begin) / (source_end - source_begin)
        tokens = []
        position = 0
        for unit in display_units(reference['text']):
            count = len(normalize_text(unit))
            first, last = observed[position][0], observed[position + count - 1][1]
            position += count
            tokens.append(TimedToken(unit, begin + (first - source_begin) * scale,
                                     begin + (last - source_begin) * scale))
        output.append(LyricLine(reference['text'], begin, end, tuple(tokens),
            key=reference.get('key'), agent=reference.get('agent'), metadata=reference.get('metadata')))
    if statistics.median(start_deltas) > .5:
        raise ValueError('provider median word start differs from the native recording')
    restored, counts = restore_nonlexical(references, output)
    report = {**counts, 'validation_method': 'native_reference_word_clock_fusion',
              'reference_text_similarity': 1.0, 'provider_lexical_text_similarity': coverage,
              'provider_substitution_count': len(substitutions),
              'native_lexical_line_coverage': 1.0,
              'median_start_delta_seconds': statistics.median(start_deltas),
              'maximum_start_delta_seconds': max(start_deltas),
              'maximum_end_delta_seconds': max(end_deltas),
              'nonlexical_timing': 'native_line_timing'}
    return render_word_ttml(restored, duration=duration, title=title, artist=artist), report
