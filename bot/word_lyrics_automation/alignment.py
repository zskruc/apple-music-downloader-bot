from __future__ import annotations

import difflib
import math
import statistics
import zlib
from dataclasses import dataclass
from typing import Any, Iterable

from .models import LyricLine, TimedToken
from .nonlexical import split_nonlexical, restore_nonlexical
from .text import display_units, normalize_text
from .ttml import render_word_ttml


_WHISPER_LANGUAGES = frozenset((
    "auto en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms "
    "cs ro da hu ta no th ur hr bg lt la mi ml cy sk te fa lv bn sr az sl kn et "
    "mk br eu is hy ne mn bs kk sq sw gl mr pa si km sn yo so af oc ka be tg "
    "sd gu am yi lo uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha ba "
    "jw su yue"
).split())


@dataclass(frozen=True, slots=True)
class ASRWord:
    text: str
    begin: float
    end: float
    probability: float


def validate_reference_payload(payload: dict[str, Any], *, duration: float | None = None) -> None:
    """Reject damaged references before inference or silent line filtering.

    Structured lines remain the display authority. The flat prompt may differ
    only by the existing text normalization (layout, case and script folding).
    """
    def text(value: Any, name: str) -> str:
        if not isinstance(value, str):
            raise RuntimeError(f"invalid alignment reference: {name} must be text")
        if any(not (character in "\t\n\r" or 0x20 <= ord(character) <= 0xD7FF
                    or 0xE000 <= ord(character) <= 0xFFFD
                    or 0x10000 <= ord(character) <= 0x10FFFF)
               for character in value):
            raise RuntimeError(f"invalid alignment reference: {name} contains illegal XML text")
        return value

    def number(value: Any, name: str) -> float:
        try:
            if isinstance(value, bool):
                raise ValueError("boolean time")
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(f"reference windows are invalid: {name}") from exc
        if not math.isfinite(parsed):
            raise RuntimeError(f"reference windows are invalid: {name} is not finite")
        return parsed

    track = payload.get("track") or {}
    if not isinstance(track, dict):
        raise RuntimeError("invalid alignment reference: track must be an object")
    for name in ("title", "artist"):
        if track.get(name):
            text(str(track[name]), name)
    if duration is not None:
        limit = number(duration, "duration")
        if limit <= 0:
            raise RuntimeError("reference windows are invalid: duration is not positive")
    else:
        raw_duration = track.get("duration")
        limit = number(raw_duration, "duration") if raw_duration is not None else 0.0
        if limit < 0:
            raise RuntimeError("reference windows are invalid: duration is negative")
    raw_reference = payload.get("reference_text")
    reference = text("" if raw_reference is None else raw_reference, "reference_text")
    raw_lines = payload.get("reference_lines")
    if raw_lines is None:
        return
    if not isinstance(raw_lines, list):
        raise RuntimeError("invalid alignment reference: reference_lines must be an array")
    last_begins: dict[str | None, float] = {}
    keys: set[str] = set()
    contents: list[str] = []
    for index, line in enumerate(raw_lines, 1):
        if not isinstance(line, dict):
            raise RuntimeError(f"invalid alignment reference: line {index} must be an object")
        contents.append(text(line.get("text"), f"line {index}"))
        if "alignment_language" in line:
            language = line["alignment_language"]
            if not isinstance(language, str) or language not in _WHISPER_LANGUAGES:
                raise RuntimeError(f"invalid alignment reference: line {index} alignment_language must be a whisper language code")
        # TTML document order groups lead/backing phrases for display. Its
        # starts need only advance within the same vocal agent; another
        # agent may enter earlier while the preceding phrase is still sung.
        agent = line.get("agent")
        if agent is not None:
            agent = text(agent, f"line {index} agent")
            if not agent.strip():
                raise RuntimeError(f"invalid alignment reference: line {index} agent is empty")
        key = line.get("key")
        if key is not None:
            key = text(key, f"line {index} key")
            if not key.strip() or key in keys:
                raise RuntimeError(f"invalid alignment reference: line {index} key is empty or duplicated")
            keys.add(key)
        if line.get("metadata") is not None:
            text(line["metadata"], f"line {index} metadata")
        begin = number(line.get("begin"), f"line {index} begin")
        if begin < 0 or begin < last_begins.get(agent, -1.0) or (limit > 0 and begin >= limit):
            raise RuntimeError(f"reference windows are invalid: line {index} begin")
        last_begins[agent] = begin
        if line.get("end") is not None:
            end = number(line["end"], f"line {index} end")
            if end <= begin or (limit > 0 and end > limit + 0.5):
                raise RuntimeError(f"reference windows are invalid: line {index} end")
    if (raw_lines and "reference_text" in payload
            and normalize_text(reference) != normalize_text("".join(contents))):
        raise RuntimeError("invalid alignment reference: reference_text and reference_lines disagree")


def extract_asr_words(result: dict[str, Any]) -> list[ASRWord]:
    output: list[ASRWord] = []
    for segment in result.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for raw_word in segment.get("words") or []:
            if not isinstance(raw_word, dict):
                continue
            text = str(raw_word.get("word") or "")
            try:
                begin = float(raw_word.get("start"))
                end = float(raw_word.get("end"))
                probability = float(raw_word.get("probability") or 0)
            except (TypeError, ValueError):
                continue
            if normalize_text(text) and begin >= 0 and end > begin:
                if output and begin < output[-1].end:
                    begin = output[-1].end
                if end > begin:
                    output.append(ASRWord(text, begin, end, probability))
    return output


def compression_ratio(value: str) -> float:
    payload = normalize_text(value).encode("utf-8")
    if not payload:
        return math.inf
    return len(payload) / max(1, len(zlib.compress(payload, level=9)))


def duplicate_token_fraction(words: Iterable[ASRWord]) -> float:
    tokens = [normalize_text(word.text) for word in words if normalize_text(word.text)]
    if not tokens:
        return 1.0
    repeated = sum(
        1
        for index, token in enumerate(tokens)
        if index and token == tokens[index - 1]
    )
    return repeated / len(tokens)


def _observed_characters(words: list[ASRWord]) -> tuple[str, list[tuple[float, float]]]:
    characters: list[str] = []
    timings: list[tuple[float, float]] = []
    for word in words:
        normalized = normalize_text(word.text)
        if not normalized:
            continue
        duration = max(0.001, word.end - word.begin)
        for index, character in enumerate(normalized):
            begin = word.begin + duration * index / len(normalized)
            end = word.begin + duration * (index + 1) / len(normalized)
            characters.append(character)
            timings.append((begin, end))
    return "".join(characters), timings


def _reference_lines(payload: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    validate_reference_payload(payload, duration=duration)
    raw_lines = payload.get("reference_lines")
    lines: list[dict[str, Any]] = []
    if isinstance(raw_lines, list):
        for raw in raw_lines:
            if not isinstance(raw, dict) or not normalize_text(raw.get("text")):
                continue
            try:
                begin = float(raw.get("begin"))
                end_value = raw.get("end")
                end = float(end_value) if end_value is not None else 0.0
            except (TypeError, ValueError):
                continue
            lines.append({**raw, "text": str(raw["text"]), "begin": begin, "end": end})
    if not lines:
        text = str(payload.get("reference_text") or "").strip()
        if text:
            source = [line.strip() for line in text.splitlines() if normalize_text(line)]
            if not source:
                source = [text]
            span = max(0.1, duration / len(source))
            lines = [
                {"text": line, "begin": index * span, "end": (index + 1) * span}
                for index, line in enumerate(source)
            ]
    for index, line in enumerate(lines):
        if line["end"] <= line["begin"]:
            line["end"] = next(
                (following["begin"] for following in lines[index + 1:]
                 if following.get("agent") == line.get("agent")
                 and following["begin"] > line["begin"]),
                duration,
            )
        line["end"] = min(duration, max(line["begin"] + 0.02, line["end"]))
    return lines


def _parallel_agent_pairs(lines: list[dict[str, Any]]) -> list[tuple[int, int]]:
    return [(earlier, current)
            for current, line in enumerate(lines)
            for earlier, other in enumerate(lines[:current])
            if line.get("agent") != other.get("agent")
            and max(line["begin"], other["begin"]) < min(line["end"], other["end"])]


def _repeated_parallel_indices(lines: list[dict[str, Any]]) -> set[int]:
    return {index for pair in _parallel_agent_pairs(lines)
            if normalize_text(lines[pair[0]]["text"]) == normalize_text(lines[pair[1]]["text"])
            for index in pair}


def reference_crop_bounds(lines: list[dict[str, Any]], index: int, duration: float) -> tuple[float, float]:
    """One crop policy shared by inference planning and stable row lookup."""
    lead_in = .10 if index in _repeated_parallel_indices(lines) else 1.25
    return max(0.0, lines[index]["begin"] - lead_in), min(duration, lines[index]["end"] + .75)


def align_reference(
    words: list[ASRWord],
    payload: dict[str, Any],
    *,
    duration: float,
    title: str,
    artist: str,
    window_words: list[list[ASRWord]] | None = None,
) -> tuple[str, dict[str, Any]]:
    lines = _reference_lines(payload, duration)
    if not lines:
        raise RuntimeError("no reference text is available for alignment")
    reference = "".join(normalize_text(line["text"]) for line in lines)
    if not reference or not words:
        raise RuntimeError("reference or acoustic transcript is empty")
    if window_words is not None and len(window_words) != len(lines):
        raise RuntimeError("reference window transcript count mismatch")
    parallel_pairs = _parallel_agent_pairs(lines)
    if parallel_pairs and window_words is None:
        # A single mixed transcript cannot independently verify simultaneous
        # voices, especially when they sing the same words. Route these rows
        # through the established independent crop inference and quality gates.
        raise RuntimeError("acoustic/reference mismatch: overlapping vocal agents require independent line windows")

    # Match each authoritative line only against nearby acoustic words.  A
    # whole-song SequenceMatcher is ambiguous when a chorus repeats: correct
    # words from the fourth chorus can otherwise be assigned to the first.
    # The supplied line envelopes are used only as search windows; karaoke
    # unit timings still come from the acoustic transcript and interpolation
    # between acoustically matched anchors.
    timings_by_line: list[list[tuple[float, float] | None]] = []
    matched_anchors_by_line: list[list[tuple[float, float]]] = []
    matched = 0
    weighted_similarity = 0.0
    line_coverages: list[float] = []
    line_similarities: list[float] = []
    for line_index, raw_line in enumerate(lines):
        line_reference = normalize_text(raw_line["text"])
        line_begin = float(raw_line["begin"])
        line_end = float(raw_line["end"])
        padding = max(1.25, min(2.5, (line_end - line_begin) * 0.4))
        nearby_words = [
            word
            for word in (window_words[line_index] if window_words is not None else words)
            if word.begin >= line_begin - padding and word.end <= line_end + padding
        ]
        line_observed, line_observed_timings = _observed_characters(nearby_words)
        line_timings: list[tuple[float, float] | None] = [None] * len(
            line_reference
        )
        matched_anchors: list[tuple[float, float]] = []
        if line_observed:
            matcher = difflib.SequenceMatcher(
                None,
                line_reference,
                line_observed,
                autojunk=False,
            )
            line_matched = 0
            for block in matcher.get_matching_blocks():
                for offset in range(block.size):
                    reference_index = block.a + offset
                    observed_index = block.b + offset
                    line_timings[reference_index] = line_observed_timings[
                        observed_index
                    ]
                    matched_anchors.append(line_observed_timings[observed_index])
                    line_matched += 1
            # A homophone/substitution still has an observed acoustic span.
            # Use that span for timing without counting it as a text match.
            # Large insertions usually belong to an adjacent line's padding,
            # so leave those to bounded interpolation instead.
            for tag, ref_start, ref_stop, obs_start, obs_stop in matcher.get_opcodes():
                ref_count, obs_count = ref_stop - ref_start, obs_stop - obs_start
                if tag != "replace" or not 0.5 <= ref_count / obs_count <= 2.0:
                    continue
                if ref_count == obs_count:
                    line_timings[ref_start:ref_stop] = line_observed_timings[obs_start:obs_stop]
                else:
                    begin = line_observed_timings[obs_start][0]
                    end = line_observed_timings[obs_stop - 1][1]
                    for offset in range(ref_count):
                        line_timings[ref_start + offset] = (
                            begin + (end - begin) * offset / ref_count,
                            begin + (end - begin) * (offset + 1) / ref_count,
                        )
            line_similarity = matcher.ratio()
        else:
            line_matched = 0
            line_similarity = 0.0
        line_coverage = line_matched / len(line_reference)
        matched += line_matched
        weighted_similarity += line_similarity * len(line_reference)
        line_coverages.append(line_coverage)
        line_similarities.append(line_similarity)
        timings_by_line.append(line_timings)
        matched_anchors_by_line.append(matched_anchors)

    coverage = matched / len(reference)
    similarity = weighted_similarity / len(reference)
    unverified_line_fraction = sum(
        value < 0.25 for value in line_coverages
    ) / len(line_coverages)
    if coverage < 0.70 or similarity < 0.60:
        raise RuntimeError(
            f"acoustic/reference mismatch: coverage={coverage:.3f}, similarity={similarity:.3f}"
        )
    if unverified_line_fraction > 0.10:
        raise RuntimeError(
            "acoustic/reference mismatch: "
            f"unverified_line_fraction={unverified_line_fraction:.3f}"
        )

    # Distinct overlapping voices repeating one phrase must be anchored in
    # their own native entry, before any missing-character interpolation.
    # Rehearing the earlier entry with a slightly different DTW clock is not
    # independent evidence for the later echo.
    repeated_anchor_fractions: dict[str, float] = {}
    for index in _repeated_parallel_indices(lines):
        line = lines[index]
        anchors = matched_anchors_by_line[index]
        acoustic_span = sum(end - begin for begin, end in anchors)
        native_span = sum(max(0.0, min(end, line["end"]) - max(begin, line["begin"]))
                          for begin, end in anchors)
        fraction = native_span / acoustic_span if acoustic_span else 0.0
        repeated_anchor_fractions[str(line.get("key") or index + 1)] = round(fraction, 6)
        if fraction + 1e-9 < .50:
            raise RuntimeError("acoustic/reference mismatch: repeated parallel entry "
                               f"{line.get('key') or index + 1} native_anchor_fraction={fraction:.3f} below 0.500")

    output_lines: list[LyricLine] = []
    for line_index, (raw_line, line_times) in enumerate(zip(lines, timings_by_line, strict=True)):
        line_text = str(raw_line["text"])
        line_normalized = normalize_text(line_text)
        line_begin = float(raw_line["begin"])
        line_end = float(raw_line["end"])
        known = [timing for timing in line_times if timing is not None]
        acoustic_begin = min((timing[0] for timing in known), default=line_begin)
        acoustic_end = max((timing[1] for timing in known), default=line_end)
        # Each voice owns an independent clock. Preserve document order and
        # constrain only earlier phrases of this same voice. This retains the
        # established nonoverlap guard for ordinary single-voice references
        # without flattening a backing entry onto a later lead phrase.
        earlier_same_agent = [
            (earlier, output)
            for earlier, output in zip(lines[:line_index], output_lines, strict=True)
            if earlier.get("agent") == raw_line.get("agent")
        ]
        order_floor = max(
            [earlier_same_agent[-1][1].begin if earlier_same_agent else 0.0]
            + [
                output.end
                for earlier, output in earlier_same_agent
                if float(earlier["end"]) <= line_begin
            ]
        )
        envelope_begin = max(
            order_floor,
            0.0,
            min(line_begin, acoustic_begin),
        )
        envelope_end = min(duration, max(line_end, acoustic_end))
        minimum_span = max(0.02, len(line_normalized) * 0.005)
        envelope_end = min(duration, max(envelope_end, envelope_begin + minimum_span))
        if envelope_end <= envelope_begin:
            raise RuntimeError("alignment produced a non-positive line envelope")
        # Fill unmatched characters monotonically inside the verified line envelope.
        filled: list[tuple[float, float]] = []
        cursor = envelope_begin
        minimum_character_span = min(0.02, (envelope_end - envelope_begin) / (2 * len(line_times)))
        for index, timing in enumerate(line_times):
            # Reserve space for trailing omitted characters before assigning
            # an anchor that reaches the line end. Previously the anchor
            # consumed the entire envelope and the next character got 0 ms.
            end_limit = envelope_end - (len(line_times) - index - 1) * minimum_character_span
            if timing is None:
                next_known_index = next(
                    (
                        candidate_index
                        for candidate_index in range(index + 1, len(line_times))
                        if line_times[candidate_index] is not None
                    ),
                    len(line_times),
                )
                next_known = (
                    line_times[next_known_index]
                    if next_known_index < len(line_times)
                    else None
                )
                boundary = next_known[0] if next_known else envelope_end
                slots = max(1, next_known_index - index)
                end = cursor + max(minimum_character_span, (boundary - cursor) / slots)
            else:
                begin = max(cursor, min(timing[0], end_limit - minimum_character_span))
                end = max(begin + minimum_character_span, timing[1])
                cursor = begin
            end = min(end_limit, max(cursor + minimum_character_span, end))
            if end <= cursor:
                raise RuntimeError("alignment compressed a reference character to zero")
            filled.append((cursor, end))
            cursor = end
        units = display_units(line_text)
        tokens: list[TimedToken] = []
        char_offset = 0
        for unit in units:
            normalized = normalize_text(unit)
            if not normalized:
                continue
            unit_times = filled[char_offset : char_offset + len(normalized)]
            char_offset += len(normalized)
            if not unit_times:
                continue
            begin = unit_times[0][0]
            end = unit_times[-1][1]
            if tokens and begin < tokens[-1].end:
                begin = tokens[-1].end
            if end <= begin:
                end = min(envelope_end, begin + 0.005)
            if end <= begin:
                raise RuntimeError("alignment produced a zero-duration karaoke unit")
            tokens.append(TimedToken(unit, begin, end))
        if not tokens or char_offset != len(line_normalized):
            raise RuntimeError("alignment did not preserve every reference character")
        output_lines.append(
            LyricLine(line_text, tokens[0].begin, tokens[-1].end, tuple(tokens),
                      key=raw_line.get("key"), agent=raw_line.get("agent"),
                      metadata=raw_line.get("metadata"))
        )
    if not output_lines:
        raise RuntimeError("alignment produced no timed reference lines")
    for first, second in parallel_pairs:
        earlier, current = lines[first], lines[second]
        if normalize_text(earlier["text"]) != normalize_text(current["text"]):
            continue
        # Distinct native entries of the same phrase cannot both be certified
        # by a crop that repeatedly hears just one entry. Allow one DTW frame
        # of quantization when identifying an indistinguishable observed clock.
        if (abs(earlier["begin"] - current["begin"]) <= .020
                and abs(earlier["end"] - current["end"]) <= .020):
            continue
        left, right = output_lines[first], output_lines[second]
        if (len(left.tokens) == len(right.tokens)
                and all(abs(a.begin - b.begin) <= .020 + 1e-9
                        and abs(a.end - b.end) <= .020 + 1e-9
                        for a, b in zip(left.tokens, right.tokens, strict=True))):
            raise RuntimeError("acoustic/reference mismatch: distinct parallel entries share one acoustic clock")
    ttml = render_word_ttml(
        output_lines,
        duration=duration,
        title=title,
        artist=artist,
    )
    report = {
        "mode": "reference_aligned",
        "reference_text_similarity": 1.0,
        "matched_reference_character_coverage": round(coverage, 6),
        "raw_transcript_text_similarity": round(similarity, 6),
        "alignment_strategy": "line_windowed_acoustic",
        "minimum_line_character_coverage": round(min(line_coverages), 6),
        "median_line_character_coverage": round(
            statistics.median(line_coverages), 6
        ),
        "minimum_line_text_similarity": round(min(line_similarities), 6),
        "unverified_line_fraction": round(unverified_line_fraction, 6),
        "repeated_parallel_native_anchor_fractions": repeated_anchor_fractions,
        "cue_count": sum(len(line.tokens) for line in output_lines),
        "average_probability": round(
            statistics.fmean(word.probability for word in words), 6
        ),
        "duplicate_token_fraction": round(duplicate_token_fraction(words), 6),
        "compression_ratio": round(compression_ratio("".join(word.text for word in words)), 6),
    }
    return ttml, report


def free_transcription(
    words: list[ASRWord],
    *,
    duration: float,
    title: str,
    artist: str,
) -> tuple[str, dict[str, Any]]:
    if not words:
        raise RuntimeError("ASR returned no timed words")
    lines: list[LyricLine] = []
    current: list[ASRWord] = []
    for word in words:
        if current and (
            len(current) >= 10
            or word.end - current[0].begin > 8.0
            or current[-1].text.rstrip().endswith((".", "!", "?", "。", "！", "？"))
        ):
            tokens = tuple(TimedToken(item.text, item.begin, item.end) for item in current)
            lines.append(
                LyricLine("".join(item.text for item in current), tokens[0].begin, tokens[-1].end, tokens)
            )
            current = []
        current.append(word)
    if current:
        tokens = tuple(TimedToken(item.text, item.begin, item.end) for item in current)
        lines.append(
            LyricLine("".join(item.text for item in current), tokens[0].begin, tokens[-1].end, tokens)
        )
    text = "".join(word.text for word in words)
    report = {
        "mode": "free_transcription",
        "cue_count": len(words),
        "average_probability": round(statistics.fmean(word.probability for word in words), 6),
        "duplicate_token_fraction": round(duplicate_token_fraction(words), 6),
        "compression_ratio": round(compression_ratio(text), 6),
        "free_transcription_without_reference": True,
    }
    return render_word_ttml(lines, duration=duration, title=title, artist=artist), report


def build_candidate(
    result: dict[str, Any],
    payload: dict[str, Any],
    *,
    reference_results: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    validate_reference_payload(payload)
    # Overlapping crops are separate acoustic observations. Concatenating
    # them before monotonic token cleanup discards correct words from the
    # next crop when the preceding crop also hears its opening syllables.
    window_words = (
        [extract_asr_words(window) for window in reference_results]
        if reference_results is not None else None
    )
    words = (
        [word for window in window_words for word in window]
        if window_words is not None else extract_asr_words(result)
    )
    track = payload.get("track") or {}
    duration = float(track.get("duration") or 0)
    if duration <= 0:
        duration = max((word.end for word in words), default=0)
    if duration <= 0:
        raise RuntimeError("audio duration is unavailable")
    kwargs = {
        "duration": duration,
        "title": str(track.get("title") or "Unknown"),
        "artist": str(track.get("artist") or "Unknown"),
    }
    if normalize_text(payload.get("reference_text") or ""):
        # Independent crops can perform worse than the full recording. Reuse
        # the latter only if BOTH text metrics improve, within this line's
        # acoustic window; never move words between repeated choruses.
        references = _reference_lines(payload, duration)
        if window_words is not None:
            if len(window_words) != len(references):
                raise RuntimeError('reference window transcript count mismatch')
            whole_words = extract_asr_words(result)
            parallel_indices = {index for pair in _parallel_agent_pairs(references) for index in pair}
            for index, reference in enumerate(references):
                # Whole-song rescue would undo the independent observation
                # requirement and can lend the lead's words to a backing row.
                if index in parallel_indices:
                    continue
                lead = split_nonlexical(reference['text'])[0]
                if not lead:
                    continue
                padding = max(1.25, min(2.5, (reference['end'] - reference['begin']) * .4))
                nearby = [word for word in whole_words
                          if word.begin >= reference['begin'] - padding
                          and word.end <= reference['end'] + padding]
                def quality(observed):
                    matcher = difflib.SequenceMatcher(None, normalize_text(lead),
                        _observed_characters(observed)[0], autojunk=False)
                    return sum(block.size for block in matcher.get_matching_blocks()), matcher.ratio()
                if nearby and all(a >= b for a, b in zip(quality(nearby), quality(window_words[index]))):
                    window_words[index] = nearby
            words = [word for window in window_words for word in window]
        try:
            return align_reference(words, payload, window_words=window_words, **kwargs)
        except RuntimeError as exc:
            if not str(exc).startswith(('acoustic/reference mismatch', 'reference or acoustic transcript is empty')):
                raise
            if not any(split_nonlexical(line['text'])[1] for line in references):
                raise
        selected = [index for index, line in enumerate(references) if split_nonlexical(line['text'])[0]]
        if not selected:
            raise RuntimeError('no lexical reference available for word alignment')
        lexical_lines = [{**references[index], 'text': split_nonlexical(references[index]['text'])[0]}
                         for index in selected]
        lexical_payload = {**payload, 'reference_lines': lexical_lines,
                           'reference_text': ''.join(line['text'] for line in lexical_lines)}
        lexical_windows = [window_words[index] for index in selected] if window_words is not None else None
        lexical_words = [word for window in lexical_windows for word in window] if lexical_windows is not None else words
        ttml, report = align_reference(lexical_words, lexical_payload, window_words=lexical_windows, **kwargs)
        from .ttml import parse_word_ttml
        restored, counts = restore_nonlexical(references, parse_word_ttml(ttml))
        report.update(counts, quality_scope='lexical_lyrics', nonlexical_timing='native_line_timing')
        return render_word_ttml(restored, **kwargs), report
    raise RuntimeError(
        "trusted reference text is required; free transcription publication is disabled"
    )
