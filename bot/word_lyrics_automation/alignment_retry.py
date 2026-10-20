from __future__ import annotations

import copy
import math
from typing import Any, Callable

from .alignment import build_candidate, validate_reference_payload
from .text import normalize_text
from .nonlexical import split_nonlexical


_WHISPER_LANGUAGES = frozenset((
    "auto en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms "
    "cs ro da hu ta no th ur hr bg lt la mi ml cy sk te fa lv bn sr az sl kn et "
    "mk br eu is hy ne mn bs kk sq sw gl mr pa si km sn yo so af oc ka be tg "
    "sd gu am yi lo uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha ba "
    "jw su yue"
).split())


def reference_language(payload: dict[str, Any]) -> str | None:
    if "alignment_language" in payload:
        language = payload["alignment_language"]
        if not isinstance(language, str) or language not in _WHISPER_LANGUAGES:
            raise ValueError("unsupported alignment_language; expected a whisper language code")
        return language
    text = str(payload.get("reference_text") or "")
    return "ja" if any("\u3041" <= character <= "\u30ff" for character in text) else None


def build_with_reference_retry(
    result: dict[str, Any],
    payload: dict[str, Any],
    transcribe_window: Callable[[float, float, str, str | None], dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Retry verified line windows once, with the same publication gates."""
    validate_reference_payload(payload)
    try:
        return build_candidate(result, payload)
    except RuntimeError as exc:
        initial_error = str(exc)
        if not initial_error.startswith((
            "acoustic/reference mismatch", "alignment produced",
            "reference or acoustic transcript is empty",
        )):
            raise
    raw_lines = payload.get("reference_lines") or []
    duration = float((payload.get("track") or {}).get("duration") or 0)
    if not normalize_text(str(payload.get("reference_text") or "")) or not 1 <= len(raw_lines) <= 200:
        raise RuntimeError(initial_error)
    windows = []
    last_begin = -1.0
    for index, line in enumerate(raw_lines):
        try:
            begin = float(line["begin"])
            # LRC timestamps specify starts only. Use the same envelope as
            # the whole-song aligner instead of crashing on float(None).
            end_value = line.get("end")
            if end_value is None:
                end_value = raw_lines[index + 1]["begin"] if index + 1 < len(raw_lines) else duration
            end = float(end_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(initial_error + "; reference windows are invalid") from exc
        text = str(line.get("text") or "")
        if (
            not all(math.isfinite(value) for value in (begin, end, duration))
            or begin < 0 or begin < last_begin or end <= begin
            or end > duration + 0.5 or end - begin > 45
            or not normalize_text(text)
        ):
            raise RuntimeError(initial_error + "; reference windows are invalid")
        windows.append((max(0, begin - 1.25), min(duration, end + 0.75), text))
        last_begin = begin
    if sum(end - begin for begin, end, _ in windows) > duration * 2 + 60:
        raise RuntimeError(initial_error + "; reference windows exceed retry budget")
    language = reference_language(payload) or result.get("language")
    merged: dict[str, Any] = {"language": language, "segments": []}
    reference_results: list[dict[str, Any]] = []
    retried = 0
    for begin, end, text in windows:
        if not split_nonlexical(text)[0]:
            reference_results.append({'language': language, 'segments': []})
            continue
        window = transcribe_window(begin, end, text, language)
        retried += 1
        shifted: dict[str, Any] = {"language": language, "segments": []}
        for original in window.get("segments") or []:
            segment = copy.deepcopy(original)
            for key in ("start", "end"):
                if key in segment:
                    segment[key] = float(segment[key]) + begin
            for word in segment.get("words") or []:
                word["start"] = float(word["start"]) + begin
                word["end"] = float(word["end"]) + begin
                if word["start"] < begin or word["end"] > end + 0.05:
                    raise RuntimeError("line-window transcript exceeds its audio crop")
            merged["segments"].append(segment)
            shifted["segments"].append(segment)
        reference_results.append(shifted)
    ttml, report = build_candidate(result, payload, reference_results=reference_results)
    report.update(
        alignment_strategy="reference_line_windows",
        initial_alignment_error=initial_error,
        retried_windows=retried,
        alignment_input_scope="per_reference_window",
    )
    return ttml, report
