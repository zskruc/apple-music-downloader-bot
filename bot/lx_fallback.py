from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable


NO_DOWNLOADABLE_MEDIA = "no_downloadable_media"
CATALOG_API_3076 = "catalog_api_3076"
TERMINAL_QUEUE_STATUSES = {"finished", "exists", "error"}
FLAC_QUALITY_PRIORITY = ("master", "hires", "flac24bit", "flac")
FLAC_QUALITY_SCORE = {
    quality: len(FLAC_QUALITY_PRIORITY) - index
    for index, quality in enumerate(FLAC_QUALITY_PRIORITY)
}
MP3_320_QUALITY = "320k"
DOWNLOAD_QUALITY_PRIORITY = (*FLAC_QUALITY_PRIORITY, MP3_320_QUALITY)
DOWNLOAD_QUALITY_SCORE = {
    quality: len(DOWNLOAD_QUALITY_PRIORITY) - index
    for index, quality in enumerate(DOWNLOAD_QUALITY_PRIORITY)
}


class LxFallbackError(RuntimeError):
    pass


class LxFallbackStopped(LxFallbackError):
    pass


@dataclass(frozen=True)
class AppleTrack:
    track_id: str
    title: str
    artist: str
    album: str = ""
    duration_seconds: float | None = None


@dataclass(frozen=True)
class LxCandidate:
    song_info: dict[str, Any]
    source: str
    title: str
    artist: str
    album: str
    duration_seconds: float | None
    score: int
    quality: str = "flac"


@dataclass(frozen=True)
class LxSearchItem:
    song_info: dict[str, Any]
    source: str
    songmid: str
    title: str
    artist: str
    album: str
    interval: str
    quality: str = "flac"
    artwork_url: str = ""


def classify_apple_failure(
    return_code: int,
    failure_text: str,
    *,
    allow_catalog_api_3076: bool = True,
) -> str | None:
    """Return a deterministic Apple-unavailable signature, never a transient one."""
    if return_code == 66 and "No downloadable media found" in failure_text:
        return NO_DOWNLOADABLE_MEDIA
    if return_code == 72 and allow_catalog_api_3076:
        unavailable = re.search(
            r"[\"']?error[\"']?\s*:\s*[\"']playback_unavailable[\"']",
            failure_text,
            re.IGNORECASE,
        )
        failure_type = re.search(
            r"[\"']?failureType[\"']?\s*:\s*[\"']?3076[\"']?",
            failure_text,
            re.IGNORECASE,
        )
        if unavailable and failure_type:
            return CATALOG_API_3076
    if return_code != 67 or not allow_catalog_api_3076:
        return None
    if "Skipping " not in failure_text or "API Error" not in failure_text:
        return None
    failure_type = re.search(
        r"[\"']?failureType[\"']?\s*:\s*[\"']?3076[\"']?",
        failure_text,
        re.IGNORECASE,
    )
    if failure_type and "暂时无法收听此歌曲" in failure_text:
        return CATALOG_API_3076
    return None


def fallback_is_eligible(
    *,
    enabled: bool,
    mode: str,
    pending_url_count: int,
    track_id: str,
    return_code: int,
    failure_text: str,
    allow_catalog_api_3076: bool = True,
) -> str | None:
    if not enabled or mode != "alac" or pending_url_count != 1 or not track_id:
        return None
    return classify_apple_failure(
        return_code,
        failure_text,
        allow_catalog_api_3076=allow_catalog_api_3076,
    )


def normalize_match_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def parse_duration_seconds(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        number = float(text)
        return number if number > 0 else None
    parts = text.split(":")
    if not all(part.isdigit() for part in parts) or len(parts) not in {2, 3}:
        return None
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        return float(numbers[0] * 60 + numbers[1])
    return float(numbers[0] * 3600 + numbers[1] * 60 + numbers[2])


def item_qualities(item: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    containers = [
        item.get("types"),
        item.get("qualitys"),
        item.get("_types"),
        item.get("_qualitys"),
    ]
    meta = item.get("meta")
    if isinstance(meta, dict):
        containers.extend(
            [
                meta.get("types"),
                meta.get("qualitys"),
                meta.get("_types"),
                meta.get("_qualitys"),
            ]
        )
    for container in containers:
        if isinstance(container, dict):
            entries = container.items()
        elif isinstance(container, (list, tuple)):
            entries = ((None, entry) for entry in container)
        else:
            continue
        for key, value in entries:
            if key is not None and (value is None or value is False):
                continue
            if isinstance(value, dict):
                if value.get("isPlatformQuality") is True:
                    continue
                # LX search adapters often keep unavailable platform qualities
                # as placeholders such as {"type": "master", "size": null}.
                # Treat an explicitly empty size as unavailable instead of
                # submitting a download that can never resolve.
                if "size" in value:
                    size = value.get("size")
                    if size is None or size is False or size == 0 or str(size).strip() in {"", "0"}:
                        continue
            if key is not None:
                quality = str(key or "").strip().casefold()
            elif isinstance(value, dict):
                quality = str(
                    value.get("type") or value.get("name") or ""
                ).strip().casefold()
            else:
                quality = str(value or "").strip().casefold()
            if quality and quality not in values:
                values.append(quality)
    return tuple(values)


def item_has_quality(item: dict[str, Any], quality: str) -> bool:
    return quality.casefold() in item_qualities(item)


def best_flac_quality(item: dict[str, Any]) -> str | None:
    available = set(item_qualities(item))
    return next(
        (quality for quality in FLAC_QUALITY_PRIORITY if quality in available),
        None,
    )


def best_download_quality(
    item: dict[str, Any],
    *,
    allow_320: bool = True,
) -> str | None:
    available = set(item_qualities(item))
    priority = DOWNLOAD_QUALITY_PRIORITY if allow_320 else FLAC_QUALITY_PRIORITY
    return next((quality for quality in priority if quality in available), None)


def is_lossless_quality(quality: str) -> bool:
    return quality.casefold() in FLAC_QUALITY_PRIORITY


def _artist_score(expected: str, actual: str) -> int:
    left = normalize_match_text(expected)
    right = normalize_match_text(actual)
    if not left or not right:
        return 0
    if left == right:
        return 60
    if min(len(left), len(right)) >= 2 and (left in right or right in left):
        return 42
    # Apple can localize a Chinese artist name with traditional characters
    # while mainland music sources return the simplified spelling. Keep this
    # conservative: only one same-position character may differ, and the
    # title plus duration still have to pass their independent strict checks.
    if (
        len(left) == len(right)
        and len(left) >= 3
        and sum(a != b for a, b in zip(left, right, strict=True)) == 1
    ):
        return 34
    return 0


def candidate_from_item(
    track: AppleTrack,
    item: dict[str, Any],
    *,
    quality: str,
    source_rank: int,
) -> LxCandidate | None:
    title = str(item.get("name") or "").strip()
    artist = str(item.get("singer") or "").strip()
    album = str(item.get("albumName") or "").strip()
    source = str(item.get("source") or "").strip()
    songmid = str(item.get("songmid") or item.get("songId") or "").strip()
    if not title or not artist or not source or not songmid:
        return None
    if normalize_match_text(track.title) != normalize_match_text(title):
        return None
    artist_score = _artist_score(track.artist, artist)
    if not artist_score or not item_has_quality(item, quality):
        return None
    if artist_score == 34 and (
        not track.album
        or not album
        or normalize_match_text(track.album) != normalize_match_text(album)
    ):
        return None

    expected_duration = track.duration_seconds
    actual_duration = parse_duration_seconds(item.get("interval"))
    duration_score = 0
    if expected_duration is not None and actual_duration is None:
        return None
    if expected_duration is not None and actual_duration is not None:
        tolerance = max(8.0, expected_duration * 0.03)
        if abs(expected_duration - actual_duration) > tolerance:
            return None
        duration_score = 30
    elif expected_duration is None:
        duration_score = 4

    album_score = 0
    if track.album and album:
        if normalize_match_text(track.album) == normalize_match_text(album):
            album_score = 18

    score = 100 + artist_score + duration_score + album_score - source_rank
    return LxCandidate(
        song_info=dict(item),
        source=source,
        title=title,
        artist=artist,
        album=album,
        duration_seconds=actual_duration,
        score=score,
        quality=quality.casefold(),
    )


def resolve_queue_candidate(
    track: AppleTrack,
    requested: LxCandidate,
    task: dict[str, Any],
) -> tuple[LxCandidate | None, LxCandidate]:
    """Validate LXServer source substitution and select a safe cleanup key.

    LXServer may transparently replace a requested source after enqueueing. A
    strict same-track replacement at the requested quality is acceptable. If
    the replacement only advertises a lower quality, return it solely as the
    cleanup key so the caller can remove that newly-created rejected output.
    """
    song_info = task.get("songInfo")
    if not isinstance(song_info, dict):
        return requested, requested

    requested_songmid = str(
        requested.song_info.get("songmid")
        or requested.song_info.get("songId")
        or ""
    )
    resolved_songmid = str(
        song_info.get("songmid") or song_info.get("songId") or ""
    )
    requested_identity = (requested.source, requested_songmid)
    resolved_identity = (str(song_info.get("source") or ""), resolved_songmid)
    if requested_identity == resolved_identity:
        return requested, requested

    resolved = candidate_from_item(
        track,
        song_info,
        quality=requested.quality,
        source_rank=0,
    )
    if resolved is not None:
        return resolved, resolved

    cleanup_candidate = requested
    actual_quality = best_download_quality(song_info, allow_320=True)
    if actual_quality:
        downgraded = candidate_from_item(
            track,
            song_info,
            quality=actual_quality,
            source_rank=0,
        )
        if downgraded is not None:
            cleanup_candidate = downgraded
    return None, cleanup_candidate


def select_candidate(
    track: AppleTrack,
    source_items: list[tuple[int, dict[str, Any]]],
    *,
    quality: str = "flac",
) -> LxCandidate | None:
    candidates = [
        candidate
        for source_rank, item in source_items
        if (
            candidate := candidate_from_item(
                track,
                item,
                quality=quality,
                source_rank=source_rank,
            )
        )
        is not None
    ]
    return max(candidates, key=lambda item: item.score, default=None)


def rank_candidates(
    track: AppleTrack,
    source_items: list[tuple[int, dict[str, Any]]],
    *,
    quality: str = "flac",
) -> list[LxCandidate]:
    candidates: list[LxCandidate] = []
    seen: set[tuple[str, str]] = set()
    for source_rank, item in source_items:
        candidate = candidate_from_item(
            track,
            item,
            quality=quality,
            source_rank=source_rank,
        )
        if candidate is None:
            continue
        songmid = str(
            candidate.song_info.get("songmid")
            or candidate.song_info.get("songId")
            or ""
        )
        key = (candidate.source, songmid)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return sorted(candidates, key=lambda item: item.score, reverse=True)


def rank_lossless_candidates(
    track: AppleTrack,
    source_items: list[tuple[int, dict[str, Any]]],
) -> list[LxCandidate]:
    candidates: list[LxCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for source_rank, item in source_items:
        for quality in FLAC_QUALITY_PRIORITY:
            candidate = candidate_from_item(
                track,
                item,
                quality=quality,
                source_rank=source_rank,
            )
            if candidate is None:
                continue
            songmid = str(
                candidate.song_info.get("songmid")
                or candidate.song_info.get("songId")
                or ""
            )
            key = (candidate.source, songmid, quality)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (FLAC_QUALITY_SCORE.get(item.quality, 0), item.score),
        reverse=True,
    )


def rank_download_candidates(
    track: AppleTrack,
    source_items: list[tuple[int, dict[str, Any]]],
    *,
    allow_320: bool = True,
) -> list[LxCandidate]:
    lossless = rank_lossless_candidates(track, source_items)
    if not allow_320:
        return lossless
    lossy_320 = rank_candidates(track, source_items, quality=MP3_320_QUALITY)
    return [*lossless, *lossy_320]


def safe_archive_component(value: object, fallback: str) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = text.replace("/", "／").replace("\\", "＼")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        text = fallback
    while len(text.encode("utf-8")) > 180:
        text = text[:-1]
    return text or fallback


def candidate_archive_subpath(candidate: LxCandidate) -> PurePosixPath:
    return PurePosixPath(
        safe_archive_component(candidate.artist, "Unknown Artist"),
        safe_archive_component(candidate.album, "Unknown Album"),
    )


def map_server_music_path(
    server_path: object,
    output_root: Path,
    server_music_root: object = "/server/music",
) -> Path:
    raw = PurePosixPath(str(server_path or ""))
    server_root = PurePosixPath(str(server_music_root or "/server/music"))
    if not server_root.is_absolute() or ".." in server_root.parts:
        raise LxFallbackError("LXServer music root must be an absolute safe path")
    try:
        relative = raw.relative_to(server_root)
    except ValueError as exc:
        raise LxFallbackError(
            f"LXServer returned a path outside {server_root}"
        ) from exc
    root = output_root.resolve(strict=False)
    mapped = (root / Path(*relative.parts)).resolve(strict=False)
    if not mapped.is_relative_to(root):
        raise LxFallbackError("LXServer returned an unsafe music path")
    return mapped


class LxFallbackClient:
    def __init__(
        self,
        base_url: str,
        *,
        output_root: Path,
        server_music_root: object = "/server/music",
        frontend_auth_file: Path | None = None,
        request_timeout_seconds: float = 30,
        queue_timeout_seconds: float = 1800,
        poll_seconds: float = 2,
        opener: urllib.request.OpenerDirector | None = None,
        logger: Callable[[object], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LX fallback base URL must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self._frontend_auth = ""
        if frontend_auth_file is not None:
            try:
                self._frontend_auth = Path(frontend_auth_file).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise LxFallbackError("LXServer authentication file is unavailable") from None
            if not self._frontend_auth or any(c in self._frontend_auth for c in "\r\n"):
                raise LxFallbackError("LXServer authentication file is empty or invalid")
        self.output_root = output_root
        self.server_music_root = PurePosixPath(
            str(server_music_root or "/server/music")
        )
        if not self.server_music_root.is_absolute() or ".." in self.server_music_root.parts:
            raise ValueError("LX fallback server music root must be an absolute safe path")
        self.request_timeout_seconds = max(1.0, request_timeout_seconds)
        self.queue_timeout_seconds = max(30.0, queue_timeout_seconds)
        self.poll_seconds = max(0.2, poll_seconds)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.logger = logger or (lambda _message: None)
        self.sleep = sleep
        self.monotonic = monotonic

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "AppleMusicIngest-LXFallback/1.0",
                # LXServer's queue routes map both values to the public user,
                # while /cache/check recognizes `default` but rejects `_open`.
                "x-user-name": "default",
            },
            method=method,
        )
        if self._frontend_auth:
            # Authenticate this service while keeping downloads in the existing public library.
            # Unredirected headers prevent forwarding the credential to a redirected URL.
            request.add_unredirected_header("x-frontend-auth", self._frontend_auth)
        try:
            with self.opener.open(request, timeout=self.request_timeout_seconds) as response:
                body = response.read().decode("utf-8")
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            if exc.code in {401, 403}:
                detail += " (check LXServer service authentication and download permission)"
            exc.close()
            raise LxFallbackError(
                f"LXServer {method} {urllib.parse.urlparse(path).path} failed: {detail}"
            ) from None
        except Exception as exc:
            raise LxFallbackError(
                f"LXServer {method} {urllib.parse.urlparse(path).path} failed: "
                f"{type(exc).__name__}"
            ) from exc

    def find_candidates(
        self,
        track: AppleTrack,
        *,
        sources: tuple[str, ...],
        quality: str = "flac",
    ) -> list[LxCandidate]:
        query = f"{track.title} {track.artist}".strip()
        source_items: list[tuple[int, dict[str, Any]]] = []
        errors: list[str] = []
        for source_rank, source in enumerate(sources):
            params = urllib.parse.urlencode(
                {
                    "name": query,
                    "singer": track.artist,
                    "source": source,
                    "type": "song",
                    "limit": "20",
                    "page": "1",
                    "pages": "1",
                }
            )
            try:
                payload = self._request_json(f"/api/music/search?{params}")
            except LxFallbackError as exc:
                errors.append(f"{source}:{exc}")
                continue
            if not isinstance(payload, list):
                errors.append(f"{source}:invalid search response")
                continue
            source_items.extend(
                (source_rank, item) for item in payload if isinstance(item, dict)
            )
        candidates = rank_candidates(track, source_items, quality=quality)
        if not candidates:
            suffix = f" ({'; '.join(errors[:3])})" if errors else ""
            raise LxFallbackError(
                f"no strict {quality.upper()} match for {track.artist} - {track.title}{suffix}"
            )
        return candidates

    def find_lossless_candidates(
        self,
        track: AppleTrack,
        *,
        sources: tuple[str, ...],
    ) -> list[LxCandidate]:
        query = f"{track.title} {track.artist}".strip()
        source_items: list[tuple[int, dict[str, Any]]] = []
        errors: list[str] = []
        for source_rank, source in enumerate(sources):
            params = urllib.parse.urlencode(
                {
                    "name": query,
                    "singer": track.artist,
                    "source": source,
                    "type": "song",
                    "limit": "20",
                    "page": "1",
                    "pages": "1",
                }
            )
            try:
                payload = self._request_json(f"/api/music/search?{params}")
            except LxFallbackError as exc:
                errors.append(f"{source}:{exc}")
                continue
            if not isinstance(payload, list):
                errors.append(f"{source}:invalid search response")
                continue
            source_items.extend(
                (source_rank, item) for item in payload if isinstance(item, dict)
            )
        candidates = rank_lossless_candidates(track, source_items)
        if not candidates:
            suffix = f" ({'; '.join(errors[:3])})" if errors else ""
            raise LxFallbackError(
                f"no strict lossless match for {track.artist} - {track.title}{suffix}"
            )
        return candidates

    def find_download_candidates(
        self,
        track: AppleTrack,
        *,
        sources: tuple[str, ...],
        allow_320: bool = True,
    ) -> list[LxCandidate]:
        query = f"{track.title} {track.artist}".strip()
        source_items: list[tuple[int, dict[str, Any]]] = []
        errors: list[str] = []
        for source_rank, source in enumerate(sources):
            params = urllib.parse.urlencode(
                {
                    "name": query,
                    "singer": track.artist,
                    "source": source,
                    "type": "song",
                    "limit": "20",
                    "page": "1",
                    "pages": "1",
                }
            )
            try:
                payload = self._request_json(f"/api/music/search?{params}")
            except LxFallbackError as exc:
                errors.append(f"{source}:{exc}")
                continue
            if not isinstance(payload, list):
                errors.append(f"{source}:invalid search response")
                continue
            source_items.extend(
                (source_rank, item) for item in payload if isinstance(item, dict)
            )
        candidates = rank_download_candidates(
            track,
            source_items,
            allow_320=allow_320,
        )
        if not candidates:
            suffix = f" ({'; '.join(errors[:3])})" if errors else ""
            quality_text = "lossless or verified 320k" if allow_320 else "lossless"
            raise LxFallbackError(
                f"no strict {quality_text} match for {track.artist} - {track.title}{suffix}"
            )
        return candidates

    def search_downloads(
        self,
        query: str,
        *,
        sources: tuple[str, ...],
        quality: str = "flac",
        allow_320: bool = True,
        max_results: int = 80,
    ) -> list[LxSearchItem]:
        query = " ".join(query.split()).strip()
        if not query:
            return []
        query_normalized = normalize_match_text(query)
        query_terms = [
            normalized
            for term in query.split()
            if (normalized := normalize_match_text(term))
        ]
        ranked: list[tuple[int, int, int, int, LxSearchItem]] = []
        seen: set[tuple[str, str]] = set()
        errors: list[str] = []
        successful_sources = 0
        for source_rank, source in enumerate(sources):
            params = urllib.parse.urlencode(
                {
                    "name": query,
                    "source": source,
                    "type": "song",
                    "limit": "20",
                    "page": "1",
                    "pages": "1",
                }
            )
            try:
                payload = self._request_json(f"/api/music/search?{params}")
            except LxFallbackError as exc:
                errors.append(f"{source}:{exc}")
                continue
            if not isinstance(payload, list):
                errors.append(f"{source}:invalid search response")
                continue
            successful_sources += 1
            for result_rank, raw in enumerate(payload):
                if not isinstance(raw, dict):
                    continue
                selected_quality = best_download_quality(raw, allow_320=allow_320)
                if selected_quality is None:
                    continue
                title = str(raw.get("name") or "").strip()
                artist = str(raw.get("singer") or "").strip()
                album = str(raw.get("albumName") or "").strip()
                item_source = str(raw.get("source") or source).strip().lower()
                songmid = str(raw.get("songmid") or raw.get("songId") or "").strip()
                if not title or not artist or not item_source or not songmid:
                    continue
                key = (item_source, songmid)
                if key in seen:
                    continue
                seen.add(key)
                title_normalized = normalize_match_text(title)
                haystack = normalize_match_text(f"{title} {artist} {album}")
                relevance = 0
                if query_normalized == title_normalized:
                    relevance += 240
                elif query_normalized and query_normalized in haystack:
                    relevance += 160
                relevance += 45 * sum(term in haystack for term in query_terms)
                if title_normalized and title_normalized in query_normalized:
                    relevance += 30
                artwork = str(raw.get("img") or "").strip()
                if not artwork.startswith(("http://", "https://")):
                    artwork = ""
                ranked.append(
                    (
                        -relevance,
                        -DOWNLOAD_QUALITY_SCORE[selected_quality],
                        source_rank,
                        result_rank,
                        LxSearchItem(
                            song_info=dict(raw),
                            source=item_source,
                            songmid=songmid,
                            title=title,
                            artist=artist,
                            album=album,
                            interval=str(raw.get("interval") or "").strip(),
                            quality=selected_quality,
                            artwork_url=artwork,
                        ),
                    )
                )
        if successful_sources == 0 and errors:
            raise LxFallbackError("all LX search sources failed: " + "; ".join(errors[:3]))
        ranked.sort(key=lambda entry: entry[:4])
        return [entry[4] for entry in ranked[: max(1, max_results)]]

    def search_flac(
        self,
        query: str,
        *,
        sources: tuple[str, ...],
        quality: str = "flac",
        max_results: int = 80,
    ) -> list[LxSearchItem]:
        return self.search_downloads(
            query,
            sources=sources,
            quality=quality,
            allow_320=False,
            max_results=max_results,
        )

    def find_candidate(
        self,
        track: AppleTrack,
        *,
        sources: tuple[str, ...],
        quality: str = "flac",
    ) -> LxCandidate:
        return self.find_candidates(track, sources=sources, quality=quality)[0]

    def _queue_tasks(self) -> list[dict[str, Any]]:
        payload = self._request_json("/api/music/cache/queue")
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise LxFallbackError("LXServer returned an invalid queue response")
        tasks = payload.get("data") or []
        if not isinstance(tasks, list):
            raise LxFallbackError("LXServer returned an invalid queue task list")
        return [task for task in tasks if isinstance(task, dict)]

    def _stop_queue_task(self, queue_id: str) -> None:
        try:
            self._request_json(
                "/api/music/cache/stop",
                method="POST",
                payload={"queueId": queue_id},
            )
        except LxFallbackError as exc:
            self.logger(f"could not pause LX fallback queue task {queue_id}: {exc}")

    def enqueue_and_wait(
        self,
        queue_id: str,
        candidate: LxCandidate,
        *,
        quality: str = "flac",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "concurrency": 1,
            "tasks": [
                {
                    "id": queue_id,
                    "songInfo": candidate.song_info,
                    "quality": quality,
                    "enableOnlyDownloadMode": True,
                    "cacheLyric": True,
                    "embedLyric": True,
                }
            ],
        }
        response = self._request_json(
            "/api/music/cache/queue",
            method="POST",
            payload=payload,
        )
        if not isinstance(response, dict) or response.get("success") is not True:
            raise LxFallbackError("LXServer rejected the persistent queue task")

        deadline = self.monotonic() + self.queue_timeout_seconds
        while self.monotonic() < deadline:
            if stop_requested is not None and stop_requested():
                self._stop_queue_task(queue_id)
                raise LxFallbackStopped("LX fallback stopped with the parent task")
            task = next(
                (item for item in self._queue_tasks() if str(item.get("id") or "") == queue_id),
                None,
            )
            if task is not None:
                if progress_callback is not None:
                    progress_callback(task)
                status = str(task.get("status") or "")
                if status in TERMINAL_QUEUE_STATUSES:
                    if status == "error":
                        message = str(task.get("errorMsg") or "LXServer download failed")
                        raise LxFallbackError(message[:500])
                    return task
            self.sleep(self.poll_seconds)

        self._stop_queue_task(queue_id)
        raise LxFallbackError("LXServer persistent queue timed out and was paused")

    def cache_entry(
        self,
        candidate: LxCandidate,
        *,
        quality: str | None = None,
        exact_quality: bool = False,
    ) -> dict[str, Any] | None:
        song_info = candidate.song_info
        values = {
            "name": candidate.title,
            "singer": candidate.artist,
            "source": candidate.source,
            "songmid": str(song_info.get("songmid") or song_info.get("songId") or ""),
        }
        if quality:
            values["quality"] = quality
        if exact_quality:
            values["exactQuality"] = "1"
        params = urllib.parse.urlencode(values)
        payload = self._request_json(f"/api/music/cache/check?{params}")
        if not isinstance(payload, dict):
            raise LxFallbackError("LXServer returned an invalid cache index response")
        return payload if payload.get("exists") is True else None

    def cached_path(self, candidate: LxCandidate, *, quality: str = "flac") -> Path:
        payload = self.cache_entry(
            candidate,
            quality=quality,
            exact_quality=True,
        )
        if payload is None:
            raise LxFallbackError("LXServer queue finished but the exact-quality cache index is missing")
        if str(payload.get("folder") or "") != "music":
            raise LxFallbackError("LXServer fallback file is not in the permanent music folder")
        path = map_server_music_path(
            payload.get("path"),
            self.output_root,
            self.server_music_root,
        )
        expected_suffix = ".mp3" if quality.casefold() == MP3_320_QUALITY else ".flac"
        if path.suffix.casefold() != expected_suffix:
            raise LxFallbackError(
                f"LXServer fallback output for {quality} is not {expected_suffix}"
            )
        return path

    def organize_cached_path(
        self,
        candidate: LxCandidate,
        *,
        quality: str = "flac",
    ) -> Path:
        payload = self.cache_entry(candidate, quality=quality, exact_quality=True)
        if payload is None:
            raise LxFallbackError("LXServer cache entry disappeared before organization")
        source = self.cached_path(candidate, quality=quality)
        indexed_name = PurePosixPath(str(payload.get("filename") or ""))
        if (
            not indexed_name.parts
            or indexed_name.is_absolute()
            or ".." in indexed_name.parts
        ):
            raise LxFallbackError("LXServer returned an unsafe indexed filename")

        subpath = candidate_archive_subpath(candidate)
        root = self.output_root.resolve(strict=False)
        destination = (
            root / Path(*subpath.parts) / source.name
        ).resolve(strict=False)
        if not destination.is_relative_to(root):
            raise LxFallbackError("LX archive destination escaped the output root")
        if source == destination:
            return source
        if destination.exists():
            raise LxFallbackError(
                f"LX archive destination already exists: {destination.name}"
            )

        source_lyric = source.with_suffix(".lrc")
        lyric_expected = source_lyric.exists()
        response_error: Exception | None = None
        try:
            result = self._request_json(
                "/api/music/cache/categorize",
                method="POST",
                payload={
                    "filenames": [indexed_name.as_posix()],
                    "subPath": subpath.as_posix(),
                },
            )
            if (
                not isinstance(result, dict)
                or result.get("success") is not True
                or int(result.get("successCount") or 0) != 1
                or int(result.get("failCount") or 0) != 0
            ):
                raise LxFallbackError("LXServer could not organize the downloaded file")
        except Exception as exc:
            response_error = exc

        if response_error is not None and destination.exists():
            try:
                self.sync_index()
            except Exception:
                pass
        elif response_error is not None:
            raise response_error

        organized = self.cached_path(candidate, quality=quality)
        if organized != destination or not organized.is_file():
            raise LxFallbackError("LXServer index did not retain the organized path")
        if lyric_expected and not organized.with_suffix(".lrc").is_file():
            raise LxFallbackError("LXServer did not move the companion lyric file")
        return organized

    def sync_index(self) -> None:
        payload = self._request_json(
            "/api/music/cache/sync",
            method="POST",
            payload={},
        )
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise LxFallbackError("LXServer music index sync failed")
