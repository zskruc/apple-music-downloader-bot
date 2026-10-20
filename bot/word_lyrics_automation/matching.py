from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .models import LyricLine, TimedToken
from .text import display_units, normalize_text
from .ttml import LineReference


@dataclass(frozen=True, slots=True)
class MatchReport:
    matched_lines: tuple[LyricLine, ...]
    skipped_source_indexes: tuple[int, ...]
    start_deltas: tuple[float, ...]
    end_deltas: tuple[float, ...]

    @property
    def median_start_delta(self) -> float:
        return statistics.median(self.start_deltas)

    @property
    def maximum_start_delta(self) -> float:
        return max(self.start_deltas, default=0.0)

    @property
    def median_end_delta(self) -> float:
        return statistics.median(self.end_deltas)


def retime_exact_lines_to_references(
    references: list[LineReference],
    source_lines: tuple[LyricLine, ...],
) -> tuple[LyricLine, ...]:
    """Fuse exact source token proportions with authoritative line envelopes."""

    if len(references) != len(source_lines):
        raise RuntimeError("reference/source line counts differ")
    output: list[LyricLine] = []
    for reference, source in zip(references, source_lines, strict=True):
        if normalize_text(reference.text) != normalize_text(source.text):
            raise RuntimeError("reference/source line text differs")
        units = display_units(reference.text)
        if len(units) != len(source.tokens):
            raise RuntimeError("reference/source karaoke unit counts differ")
        source_span = source.end - source.begin
        target_span = reference.end - reference.begin
        if source_span <= 0 or target_span <= 0:
            raise RuntimeError("reference/source line duration is invalid")
        tokens: list[TimedToken] = []
        for unit, source_token in zip(units, source.tokens, strict=True):
            if normalize_text(unit) != normalize_text(source_token.text):
                raise RuntimeError("reference/source karaoke unit text differs")
            relative_begin = (source_token.begin - source.begin) / source_span
            relative_end = (source_token.end - source.begin) / source_span
            begin = reference.begin + min(1.0, max(0.0, relative_begin)) * target_span
            end = reference.begin + min(1.0, max(0.0, relative_end)) * target_span
            if tokens and begin < tokens[-1].end:
                begin = tokens[-1].end
            if end <= begin:
                raise RuntimeError("retimed karaoke unit has no duration")
            tokens.append(TimedToken(unit, begin, end))
        if not tokens:
            raise RuntimeError("retimed line has no karaoke units")
        output.append(
            LyricLine(
                reference.text,
                reference.begin,
                reference.end,
                tuple(tokens),
            )
        )
    return tuple(output)


def match_exact_lines(
    references: list[LineReference], source_lines: list[LyricLine]
) -> MatchReport:
    """Globally map exact text while rejecting duplicated/compressed source rows."""

    reference_count = len(references)
    source_count = len(source_lines)
    infinity = math.inf
    costs = [[infinity] * (source_count + 1) for _ in range(reference_count + 1)]
    previous: list[list[tuple[int, int, bool] | None]] = [
        [None] * (source_count + 1) for _ in range(reference_count + 1)
    ]
    for source_index in range(source_count + 1):
        costs[0][source_index] = 0.0
        if source_index:
            previous[0][source_index] = (0, source_index - 1, False)
    for reference_index in range(reference_count + 1):
        for source_index in range(source_count):
            current = costs[reference_index][source_index]
            if current == infinity:
                continue
            if current < costs[reference_index][source_index + 1]:
                costs[reference_index][source_index + 1] = current
                previous[reference_index][source_index + 1] = (
                    reference_index,
                    source_index,
                    False,
                )
            if reference_index >= reference_count:
                continue
            reference = references[reference_index]
            source = source_lines[source_index]
            if normalize_text(reference.text) != normalize_text(source.text):
                continue
            timing_cost = abs(reference.begin - source.begin) + 0.25 * abs(
                reference.end - source.end
            )
            candidate = current + timing_cost
            if candidate < costs[reference_index + 1][source_index + 1]:
                costs[reference_index + 1][source_index + 1] = candidate
                previous[reference_index + 1][source_index + 1] = (
                    reference_index,
                    source_index,
                    True,
                )
    if costs[reference_count][source_count] == infinity:
        raise RuntimeError(
            f"only a partial exact-text match was possible: "
            f"references={reference_count}, source={source_count}"
        )
    selected_indexes: list[int] = []
    reference_index = reference_count
    source_index = source_count
    while reference_index or source_index:
        step = previous[reference_index][source_index]
        if step is None:
            raise RuntimeError("line-match backtracking failed")
        prior_reference, prior_source, selected = step
        if selected:
            selected_indexes.append(prior_source)
        reference_index, source_index = prior_reference, prior_source
    selected_indexes.reverse()
    if len(selected_indexes) != reference_count:
        raise RuntimeError("line-match coverage is incomplete")
    matched = tuple(source_lines[index] for index in selected_indexes)
    selected_set = set(selected_indexes)
    skipped = tuple(index for index in range(source_count) if index not in selected_set)
    start_deltas = tuple(
        abs(reference.begin - source.begin)
        for reference, source in zip(references, matched, strict=True)
    )
    end_deltas = tuple(
        abs(reference.end - source.end)
        for reference, source in zip(references, matched, strict=True)
    )
    return MatchReport(matched, skipped, start_deltas, end_deltas)
