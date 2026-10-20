"""Conservative, source-independent recording identity for the single-track bot.

Search metadata is evidence, never a promise of downloadable audio. Network
failures are deliberately represented separately from an empty catalogue.
"""
from __future__ import annotations

import queue
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class RecordingCandidate:
    provider: str
    source: str
    source_id: str
    title: str
    artist: str
    album: str = ""
    duration_seconds: float | None = None
    isrc: str = ""
    url: str = ""
    artwork_url: str = ""
    quality: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "RecordingCandidate":
        if not isinstance(value, dict):
            raise ValueError("invalid recording candidate")
        provider = str(value.get("provider") or "")
        if provider not in {"apple", "lx", "sq"}:
            raise ValueError("unsupported recording provider")
        title, artist = str(value.get("title") or "").strip(), str(value.get("artist") or "").strip()
        source_id = str(value.get("source_id") or "").strip()
        if not title or not artist or not source_id:
            raise ValueError("recording metadata is incomplete")
        duration = value.get("duration_seconds")
        duration = float(duration) if duration is not None else None
        if duration is not None and not 0 < duration < 86400:
            raise ValueError("invalid recording duration")
        return cls(provider=provider, source=str(value.get("source") or provider),
                   source_id=source_id, title=title, artist=artist,
                   album=str(value.get("album") or ""), duration_seconds=duration,
                   isrc=str(value.get("isrc") or ""), url=str(value.get("url") or ""),
                   artwork_url=str(value.get("artwork_url") or ""),
                   quality=str(value.get("quality") or ""),
                   metadata=dict(value.get("metadata") or {}))


@dataclass(frozen=True)
class UnifiedRecording:
    candidates: tuple[RecordingCandidate, ...]

    @property
    def primary(self) -> RecordingCandidate:
        return self.candidates[0]

    def to_payload(self) -> dict[str, Any]:
        return {"schema": 1, "candidates": [asdict(item) for item in self.candidates]}

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "UnifiedRecording":
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("unsupported unified recording schema")
        raw = value.get("candidates")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
            raise ValueError("invalid unified recording candidates")
        candidates = tuple(RecordingCandidate.from_payload(item) for item in raw)
        if any(not strict_match(candidates[0], item) for item in candidates[1:]):
            raise ValueError("unified selection contains conflicting recordings")
        return cls(candidates)


def normalize(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


_VERSION_PATTERNS = {
    "live": r"\blive\b|现场|現場|演唱会|演唱會|ライブ|演奏会|演奏會",
    "remaster": r"\bremaster(?:ed)?\b|重制|重製|重录|重錄|再録",
    "acoustic": r"\bacoustic\b|不插电|不插電|原声版|原聲版",
    "instrumental": r"\binstrumental\b|\bkaraoke\b|伴奏|纯音乐|純音樂|off\s*vocal",
    "remix": r"\bremix\b|\bmix\b|混音",
    "demo": r"\bdemo\b|小样|小樣",
    "mono": r"\bmono\b|单声道|單聲道",
    "edit": r"\bradio\s*edit\b|\bsingle\s*edit\b|\bextended\b|剪辑版|剪輯版",
    "cover": r"\bcover\b|翻唱",
}


def version_fingerprint(candidate: RecordingCandidate) -> frozenset[str]:
    text = unicodedata.normalize("NFKC", f"{candidate.title} {candidate.album}").casefold()
    return frozenset(key for key, pattern in _VERSION_PATTERNS.items() if re.search(pattern, text))


def strict_match(left: RecordingCandidate, right: RecordingCandidate) -> bool:
    # ISRC cannot override title, artist, version or duration disagreements.
    if not normalize(left.title) or not normalize(left.artist):
        return False
    if normalize(left.title) != normalize(right.title) or normalize(left.artist) != normalize(right.artist):
        return False
    left_version, right_version = version_fingerprint(left), version_fingerprint(right)
    if left_version != right_version:
        return False
    # Live/remaster editions from different releases must not collapse just
    # because their duration and broad version category happen to agree.
    if left_version and (not normalize(left.album) or normalize(left.album) != normalize(right.album)):
        return False
    if left.isrc and right.isrc and normalize(left.isrc) != normalize(right.isrc):
        return False
    if left.duration_seconds is not None and right.duration_seconds is not None:
        if abs(left.duration_seconds - right.duration_seconds) > 2.0:
            return False
        return True
    # Without two measured/catalogue durations require a named matching album.
    return bool(normalize(left.album)) and normalize(left.album) == normalize(right.album)


def merge_candidates(candidates: list[RecordingCandidate]) -> list[UnifiedRecording]:
    priority = {"apple": 0, "lx": 1, "sq": 2}
    groups: list[list[RecordingCandidate]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in sorted(candidates, key=lambda item: priority.get(item.provider, 9)):
        key = candidate.provider, candidate.source, candidate.source_id
        if key in seen:
            continue
        seen.add(key)
        # Pairwise agreement prevents a weak candidate bridging different versions.
        group = next((group for group in groups if all(strict_match(item, candidate) for item in group)), None)
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)
    return [UnifiedRecording(tuple(group)) for group in groups]


_SEARCH_SLOTS = threading.BoundedSemaphore(3)


def bounded_search(searches: dict[str, Callable[[], list[Any]]], timeout: float = 20,
                   max_workers: int = 3) -> tuple[dict[str, list[Any]], dict[str, str]]:
    """Run bounded read-only searches without waiting for timed-out workers.

    A process-wide semaphore bounds hung source calls across simultaneous chats.
    Worker threads are daemonized; late results cannot alter a returned session.
    """
    completed: queue.Queue[tuple[str, list[Any], str]] = queue.Queue()
    pending: set[str] = set()
    errors: dict[str, str] = {}
    results: dict[str, list[Any]] = {}

    def run(name: str, callback: Callable[[], list[Any]]) -> None:
        try:
            completed.put((name, list(callback()), ""))
        except Exception as exc:
            # Callers may log sanitized diagnostics separately; no credentials here.
            completed.put((name, [], type(exc).__name__))
        finally:
            _SEARCH_SLOTS.release()

    for name, callback in searches.items():
        if len(pending) >= max_workers or not _SEARCH_SLOTS.acquire(blocking=False):
            errors[name] = "busy"
            continue
        pending.add(name)
        threading.Thread(target=run, args=(name, callback), daemon=True, name=f"music-search-{name}").start()
    deadline = time.monotonic() + max(0.01, timeout)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            name, items, error = completed.get(timeout=remaining)
        except queue.Empty:
            break
        pending.discard(name)
        if error:
            errors[name] = error
        else:
            results[name] = items
    errors.update({name: "timeout" for name in pending})
    return results, errors


def unified_eligible(enabled: bool, mode: str, urls: list[str]) -> bool:
    if not enabled or mode != "alac" or len(urls) != 1:
        return False
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(urls[0])
    if parsed.scheme == "unified":
        return True
    if parsed.scheme != "https" or parsed.hostname not in {"music.apple.com", "beta.music.apple.com", "geo.music.apple.com"}:
        return False
    return bool((parse_qs(parsed.query).get("i") or [""])[0].isdigit() or re.search(r"/song/(?:[^/]+/)?\d+(?:$|/)", parsed.path))
