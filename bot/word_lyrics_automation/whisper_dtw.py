"""Conservative acoustic groups from whisper.cpp's real DTW start anchors.

The next distinct text-token anchor is a *derived group boundary*.  It is not
the measured end of a pronunciation.  The final group in each raw segment has
no such boundary and stays untimed.  In particular, neither heuristic offsets
nor segment timestamps are used to manufacture an end.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


_METHOD = "whisper_dtw_group_boundaries_v1"


@dataclass
class _Group:
    anchor: float
    payload: bytearray
    probabilities: list[float]
    end: float | None = None


def _error(detail: str) -> RuntimeError:
    return RuntimeError(f"whisper.cpp DTW {detail}")


def _token_bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError as exc:
        raise _error("contains unsupported surrogate token text") from exc


def _complete_text(text: str) -> str:
    try:
        return _token_bytes(text).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error("contains unrecoverable UTF-8 segment text") from exc


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"has missing or invalid {name}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise _error(f"has invalid {name}") from exc
    if not math.isfinite(number):
        raise _error(f"has non-finite {name}")
    return number


def convert_dtw_document(document: dict[str, Any]) -> dict[str, Any]:
    """Convert full JSON without interpreting heuristic token ``offsets``.

    All id < EOT tokens, including whitespace, punctuation and UTF-8 byte
    fragments, establish boundaries before any text filtering.  Equal anchors
    form one acoustic group.  A complete decoded unit can span several groups;
    if any contributing group is untimed, the entire unit stays untimed.

    ``words`` contains only positive-duration units with lexical characters.
    Multi-character units remain intact: subsequent per-character subdivision
    is an interpolation estimate, not a measured character clock.  Segment
    ``text`` preserves the original text and ``untimed_text`` preserves all
    decoded text excluded because its group end is unknown.  Token/group
    counts include punctuation; unit/character counts describe lexical text.
    """
    if not isinstance(document, dict):
        raise _error("requires an object document")
    model = document.get("model")
    multilingual = model.get("multilingual") if isinstance(model, dict) else None
    if not isinstance(multilingual, bool):
        raise _error("requires boolean model.multilingual to determine EOT")
    eot = 50257 if multilingual else 50256
    raw_segments = document.get("transcription")
    if not isinstance(raw_segments, list):
        raise _error("requires a transcription array")

    diagnostics: dict[str, Any] = {
        "method": _METHOD,
        "derived_group_end": True,
        "end_semantics": "next_distinct_text_token_anchor_not_full_pronunciation",
        "character_subdivision": "interpolation_estimate_only",
        "raw_segment_count": len(raw_segments),
        "raw_text_token_count": 0,
        "raw_group_count": 0,
        "timed_group_count": 0,
        "untimed_group_count": 0,
        "timed_token_count": 0,
        "untimed_token_count": 0,
        "timed_unit_count": 0,
        "untimed_unit_count": 0,
        "timed_character_count": 0,
        "untimed_character_count": 0,
    }
    segments: list[dict[str, Any]] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise _error("contains a non-object segment")
        raw_tokens = raw_segment.get("tokens")
        if not isinstance(raw_tokens, list):
            raise _error("requires full JSON text tokens")
        groups: list[_Group] = []
        for token in raw_tokens:
            if not isinstance(token, dict):
                raise _error("contains a non-object token")
            token_id = token.get("id")
            if isinstance(token_id, bool) or not isinstance(token_id, int) or token_id < 0:
                raise _error("has missing or invalid token id")
            # The numeric vocabulary boundary is authoritative.  A control
            # token's -1 DTW value must never terminate a lexical group.
            if token_id >= eot:
                continue
            text = token.get("text")
            if not isinstance(text, str):
                raise _error("has missing or invalid text token")
            tick = _number(token.get("t_dtw"), "text-token t_dtw")
            if tick < 0:
                raise _error("has negative text-token t_dtw; DTW is unavailable")
            anchor = tick / 100.0  # whisper.cpp uses 10 ms ticks, not ms.
            probability = _number(token.get("p", 0.0), "token probability")
            if not 0 <= probability <= 1:
                raise _error("has out-of-range token probability")
            payload = _token_bytes(text)
            diagnostics["raw_text_token_count"] += 1
            if groups and anchor < groups[-1].anchor:
                raise _error("has non-monotonic text-token anchors")
            if not groups or anchor != groups[-1].anchor:
                if groups:
                    groups[-1].end = anchor
                groups.append(_Group(anchor, bytearray(), []))
            groups[-1].payload.extend(payload)
            groups[-1].probabilities.append(probability)

        diagnostics["raw_group_count"] += len(groups)
        diagnostics["timed_group_count"] += max(0, len(groups) - 1)
        diagnostics["untimed_group_count"] += bool(groups)
        words: list[dict[str, Any]] = []
        untimed: list[str] = []
        reconstructed: list[str] = []
        pending = bytearray()
        pending_groups: list[_Group] = []

        def flush_pending(*, final: bool = False) -> None:
            if not pending_groups:
                return
            try:
                decoded = bytes(pending).decode("utf-8")
            except UnicodeDecodeError as exc:
                if not final and exc.reason == "unexpected end of data":
                    return
                raise _error("contains unrecoverable split UTF-8 text") from exc
            reconstructed.append(decoded)
            known = all(group.end is not None for group in pending_groups)
            count = sum(len(group.probabilities) for group in pending_groups)
            lexical_count = sum(character.isalnum() for character in decoded)
            status = "timed" if known else "untimed"
            diagnostics[f"{status}_token_count"] += count
            diagnostics[f"{status}_character_count"] += lexical_count
            if lexical_count:
                diagnostics[f"{status}_unit_count"] += 1
            if not known:
                untimed.append(decoded)
            elif lexical_count:
                probabilities = [
                    probability
                    for group in pending_groups
                    for probability in group.probabilities
                ]
                words.append({
                    "word": decoded,
                    "start": pending_groups[0].anchor,
                    "end": pending_groups[-1].end,
                    "probability": sum(probabilities) / len(probabilities),
                })
            pending.clear()
            pending_groups.clear()

        for group in groups:
            pending.extend(group.payload)
            pending_groups.append(group)
            flush_pending()
        flush_pending(final=True)
        original_text = raw_segment.get("text")
        if original_text is None:
            original_text = "".join(reconstructed)
        elif not isinstance(original_text, str):
            raise _error("contains non-string segment text")
        segments.append({
            "text": _complete_text(original_text),
            "words": words,
            "untimed_text": "".join(untimed),
        })

    if not diagnostics["timed_unit_count"]:
        raise _error("returned no timed acoustic tokens with known group ends")
    result = document.get("result")
    language = str(result.get("language") or "") if isinstance(result, dict) else ""
    return {"language": language, "segments": segments, "clock_diagnostics": diagnostics}
