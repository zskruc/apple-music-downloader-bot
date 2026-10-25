from __future__ import annotations

import copy
import math
import unicodedata
from typing import Any, Callable

from .alignment import (_WHISPER_LANGUAGES, _reference_lines, build_candidate,
                        reference_crop_bounds, validate_reference_payload)
from .text import normalize_text
from .nonlexical import split_nonlexical


def reference_language(payload: dict[str, Any]) -> str | None:
    if "alignment_language" in payload:
        language = payload["alignment_language"]
        if not isinstance(language, str) or language not in _WHISPER_LANGUAGES:
            raise ValueError("unsupported alignment_language; expected a whisper language code")
        return language
    text = str(payload.get("reference_text") or "")
    return "ja" if any("\u3041" <= character <= "\u30ff" for character in text) else None


def _script_family(character: str) -> str | None:
    if not character.isalpha():
        return None
    if ("\u3400" <= character <= "\u9fff"
            or "\u3041" <= character <= "\u30ff"):
        return "cjk"
    if unicodedata.name(character, "").startswith("LATIN"):
        return "latin"
    return "other"


def reference_window_languages(payload: dict[str, Any], detected: str | None) -> list[str | None]:
    """Keep legacy monolingual hints; never force a bilingual row to that hint.

    Script identifies a change of language family, not the language of Latin
    words. Such rows and mixed/unknown rows use Whisper's auto detection.
    An explicit caller override remains authoritative for every crop.
    """
    rows = payload.get("reference_lines") or []
    texts = [str(row.get("text") or "") for row in rows]
    language = reference_language(payload) or detected
    if "alignment_language" in payload:
        return [language] * len(rows)
    for index, row in enumerate(rows, 1):
        if "alignment_language" in row:
            hint = row["alignment_language"]
            if not isinstance(hint, str) or hint not in _WHISPER_LANGUAGES:
                raise RuntimeError(f"invalid alignment reference: line {index} alignment_language must be a whisper language code")
    families = {_script_family(character) for text in texts for character in text} - {None}
    if len(families) <= 1:
        return [row.get("alignment_language", language) for row in rows]
    languages: list[str | None] = []
    for row, text in zip(rows, texts, strict=True):
        if "alignment_language" in row:
            languages.append(row["alignment_language"])
            continue
        row_families = {_script_family(character) for character in text} - {None}
        if row_families == {"cjk"}:
            languages.append("ja" if any("\u3041" <= character <= "\u30ff" for character in text) else "zh")
        else:
            languages.append("auto")
    return languages


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
    duration = float((payload.get("track") or {}).get("duration") or 0)
    raw_lines = payload.get("reference_lines") or []
    if not normalize_text(str(payload.get("reference_text") or "")) or not 1 <= len(raw_lines) <= 200:
        raise RuntimeError(initial_error)
    windows = []
    # Resolve missing endpoints within their vocal agent exactly as the
    # aligner does, retaining document order for the returned observations.
    references = _reference_lines(payload, duration)
    for index, line in enumerate(references):
        try:
            begin = float(line["begin"])
            end = float(line["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(initial_error + "; reference windows are invalid") from exc
        text = str(line.get("text") or "")
        if (
            not all(math.isfinite(value) for value in (begin, end, duration))
            or begin < 0 or end <= begin
            or end > duration + 0.5 or end - begin > 45
            or not normalize_text(text)
        ):
            raise RuntimeError(initial_error + "; reference windows are invalid")
        windows.append((*reference_crop_bounds(references, index, duration), text))
    if sum(end - begin for begin, end, _ in windows) > duration * 2 + 60:
        raise RuntimeError(initial_error + "; reference windows exceed retry budget")
    language = reference_language(payload) or result.get("language")
    window_languages = reference_window_languages(
        {**payload, "reference_lines": references}, result.get("language"))
    merged: dict[str, Any] = {"language": language, "segments": []}
    reference_results: list[dict[str, Any]] = []
    retried = 0
    for (begin, end, text), window_language in zip(windows, window_languages, strict=True):
        if not split_nonlexical(text)[0]:
            reference_results.append({'language': window_language, 'segments': []})
            continue
        window = transcribe_window(begin, end, text, window_language)
        retried += 1
        shifted: dict[str, Any] = {"language": window_language, "segments": []}
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
        reference_window_languages=window_languages,
    )
    return ttml, report
