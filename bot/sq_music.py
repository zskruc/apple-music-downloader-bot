"""Private SQ Music Plus adapter for single-track, FLAC-only ingestion.

SQ's enqueue response means accepted, not completed.  The small companion
patch exposes an authenticated task endpoint and records the *actual* path
only after tag writing finishes.  Never infer files from the display name.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from lx_fallback import AppleTrack
from unified_music import RecordingCandidate, strict_match


class SqMusicError(RuntimeError):
    pass


class SqMusicStopped(SqMusicError):
    pass


@dataclass(frozen=True)
class SqSearchItem:
    song_info: dict[str, Any]
    source: str
    song_id: str
    title: str
    artist: str
    album: str
    duration_seconds: float | None
    quality: str
    artwork_url: str = ""


@dataclass(frozen=True)
class SqCandidate(SqSearchItem):
    score: int = 0


def _quality_id(value: object, source: str) -> str | None:
    # SQ's Jackson default represents PlugBrType as enum names; some builds
    # return the object's id. Accept exactly known FLAC enum IDs for source.
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() != "flac":
            return None
        name = str(value.get("id") or "")
    else:
        name = str(value or "")
    name = name.casefold()
    if not re.fullmatch(re.escape(source) + r"_flac_(?:\d+|(?:res_)?lossless|hires)", name):
        return None
    return name


def _quality_rank(value: str) -> int:
    tail = value.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else (2800 if "res_" in value else 1411)


def item_from_record(record: dict[str, Any], source: str = "") -> SqSearchItem | None:
    source = str(record.get("plugName") or source).strip().casefold()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,24}", source):
        return None
    choices = [quality for raw in record.get("brTypes", [])
               if (quality := _quality_id(raw, source)) is not None]
    if not choices:
        return None
    song_id = str(record.get("id") or "").strip()
    title = str(record.get("name") or "").strip()
    names = record.get("artistName")
    if isinstance(names, list):
        artist = " & ".join(str(name).strip() for name in names if str(name).strip())
    else:
        artist = str(names or "").strip()
    if not song_id or not title or not artist:
        return None
    try:
        # Upstream PlugSearchMusicResult documents duration in milliseconds.
        duration = float(record.get("duration") or 0) / 1000
    except (TypeError, ValueError):
        duration = 0
    return SqSearchItem(
        song_info=dict(record), source=source, song_id=song_id,
        title=title, artist=artist, album=str(record.get("albumName") or "").strip(),
        duration_seconds=duration if duration > 0 else None,
        quality=max(choices, key=_quality_rank), artwork_url=str(record.get("pic") or ""),
    )


def select_candidate(track: AppleTrack, items: list[SqSearchItem]) -> SqCandidate | None:
    """Conservatively match a recording; never match by title alone."""
    expected = RecordingCandidate("apple", "apple", track.track_id or "expected",
                                  track.title, track.artist, track.album, track.duration_seconds)
    candidates: list[SqCandidate] = []
    for rank, item in enumerate(items):
        actual = RecordingCandidate("sq", item.source, item.song_id, item.title,
                                    item.artist, item.album, item.duration_seconds)
        if not strict_match(expected, actual):
            continue
        album_equal = bool(track.album and track.album.casefold() == item.album.casefold())
        duration_equal = track.duration_seconds is not None and item.duration_seconds is not None
        candidates.append(SqCandidate(**item.__dict__, score=200 + 20 * album_equal + 10 * duration_equal - rank))
    return max(candidates, key=lambda item: item.score, default=None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SqMusicClient:
    def __init__(
        self, base_url: str, username: str, password: str, *,
        sources: tuple[str, ...] = ("kw", "netease", "qqvip"),
        staging_root: Path = Path("/sq-staging"), sq_music_root: str = "/music",
        timeout_seconds: float = 30, poll_interval_seconds: float = 2,
        download_timeout_seconds: float = 600,
        opener: Any = None, sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("SQ URL must be an absolute HTTP(S) service URL")
        root = PurePosixPath(sq_music_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("SQ music root must be an absolute safe path")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.sources = tuple(dict.fromkeys(source for source in sources if re.fullmatch(r"[a-z][a-z0-9_]{0,24}", source)))
        self.staging_root = Path(staging_root)
        self.sq_music_root = root
        self.timeout_seconds = max(.1, float(timeout_seconds))
        self.poll_interval_seconds = max(.1, float(poll_interval_seconds))
        self.download_timeout_seconds = max(.1, float(download_timeout_seconds))
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.sleep, self.monotonic = sleep, monotonic
        self._token = ""
        self._login_lock = threading.Lock()

    def _raw_request(self, path: str, *, method: str = "GET", payload: Any = None, token: str = "") -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, method=method, data=data,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": "AppleMusicIngest-SQ/1.0"})
        if token:
            request.add_unredirected_header("Authorization", "Bearer " + token)
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise SqMusicError("SQ response exceeds allowed size")
            result = json.loads(body)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            # No response bodies, credentials, cookies or exception URLs in logs.
            raise SqMusicError(f"SQ HTTP {code}") from None
        except SqMusicError:
            raise
        except Exception as exc:
            raise SqMusicError(f"SQ request failed ({type(exc).__name__})") from None
        if not isinstance(result, dict):
            raise SqMusicError("SQ returned an invalid response")
        return result

    def _login(self, stale_token: str = "") -> str:
        with self._login_lock:
            if self._token and self._token != stale_token:
                return self._token
            response = self._raw_request("/api/config/login", method="POST", payload={
                "username": self.username, "password": self.password, "device": "apple-music-bot"})
            data = response.get("data")
            token = (data.get("tokenValue") or data.get("token")) if isinstance(data, dict) else None
            if response.get("code") != 200 or not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
                raise SqMusicError("SQ authentication failed")
            self._token = token
            return token

    def _request_json(self, path: str, *, method: str = "GET", payload: Any = None) -> Any:
        token = self._token or self._login()
        for attempt in range(2):
            try:
                result = self._raw_request(path, method=method, payload=payload, token=token)
            except SqMusicError as exc:
                if attempt == 0 and str(exc) in {"SQ HTTP 401", "SQ HTTP 403"}:
                    token = self._login(token)
                    continue
                raise
            if result.get("code") in {401, 403} and attempt == 0:
                token = self._login(token)
                continue
            if result.get("code") != 200:
                raise SqMusicError(f"SQ operation failed (code {result.get('code', 'unknown')})")
            return result.get("data")
        raise SqMusicError("SQ authentication failed")

    def _search_source(self, query: str, source: str, limit: int) -> list[SqSearchItem]:
        params = urllib.parse.urlencode({"keyword": query, "plugName": source, "pageIndex": 1, "pageSize": limit})
        data = self._request_json("/api/music/searchSong?" + params)
        records = data.get("records", []) if isinstance(data, dict) else []
        return [item for record in records if isinstance(record, dict)
                and (item := item_from_record(record, source)) is not None]

    def search_lossless(self, query: str, limit: int = 12) -> list[SqSearchItem]:
        if not query.strip() or not self.sources:
            return []
        limit = max(1, min(30, int(limit)))
        self._token or self._login()
        items: list[SqSearchItem] = []
        failures = 0
        # Read-only searches are independent; one unavailable source must not
        # suppress results from another. Preserve configured source order.
        with ThreadPoolExecutor(max_workers=min(3, len(self.sources))) as pool:
            futures = [pool.submit(self._search_source, query, source, limit) for source in self.sources]
            for future in futures:
                try:
                    items.extend(future.result())
                except SqMusicError:
                    failures += 1
        if failures == len(self.sources):
            raise SqMusicError("All SQ search sources are unavailable")
        seen: set[tuple[str, str]] = set()
        unique = []
        # Round-robin avoids the first provider consuming the complete list.
        grouped = [[item for item in items if item.source == source] for source in self.sources]
        for index in range(limit):
            for group in grouped:
                if index < len(group):
                    item = group[index]
                    key = item.source, item.song_id
                    if key not in seen:
                        unique.append(item)
                        seen.add(key)
        return unique[:limit]

    def match_track(self, track: AppleTrack) -> SqCandidate | None:
        return select_candidate(track, self.search_lossless(f"{track.title} {track.artist}", limit=30))

    def _task_file(self, task: dict[str, Any], item: SqSearchItem, task_id: int) -> Path:
        if (str(task.get("id")) != str(task_id) or str(task.get("downloadMusicId")) != item.song_id or
                task.get("downloadPlugName") != item.source or
                str(task.get("downloadBrType", "")).casefold() != item.quality):
            raise SqMusicError("SQ completed task identity or quality does not match request")
        raw = str(task.get("downloadFile") or "")
        remote = PurePosixPath(raw)
        if not remote.is_absolute() or ".." in remote.parts or "\\" in raw:
            raise SqMusicError("SQ did not record a safe actual file path; patched backend required")
        try:
            relative = remote.relative_to(self.sq_music_root)
        except ValueError:
            raise SqMusicError("SQ output is outside private staging") from None
        # Task-scoped template prevents a successful task adopting another
        # task's existing/partially-written file. Fail closed on legacy paths.
        if not relative.parts or relative.parts[0] != str(task_id):
            raise SqMusicError("SQ output is not in its private task directory")
        root = self.staging_root.resolve()
        path = self.staging_root.joinpath(*relative.parts)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            component = self.staging_root
            for part in relative.parts:
                component = component / part
                if component.is_symlink():
                    raise SqMusicError("SQ output contains a symbolic link")
            if not resolved.is_file() or resolved.suffix.casefold() != ".flac" or resolved.stat().st_size < 42:
                raise SqMusicError("SQ output is not a complete FLAC file")
            with resolved.open("rb") as handle:
                if handle.read(4) != b"fLaC":
                    raise SqMusicError("SQ output is not a FLAC stream")
        except SqMusicError:
            raise
        except (OSError, ValueError):
            raise SqMusicError("SQ output is missing or outside private staging") from None
        return resolved

    def download_candidate(
        self, item: SqSearchItem, *, timeout_seconds: float | None = None,
        stop_requested: Callable[[], bool] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> Path:
        if _quality_id(item.quality, item.source) != item.quality:
            raise SqMusicError("Only explicit SQ FLAC qualities are allowed")
        metadata = item_from_record(item.song_info, item.source)
        if metadata is None or any(getattr(metadata, name) != getattr(item, name) for name in
                                   ("source", "song_id", "title", "artist", "album", "duration_seconds")):
            raise SqMusicError("SQ candidate metadata does not match its source record")
        if stop_requested is not None and stop_requested():
            raise SqMusicStopped("SQ download stopped before enqueue")
        # Retain the enum representation supplied by the server, not a guessed
        # spelling, because Java enum names contain mixed case (QQVIP_Flac).
        raw_quality = next((raw for raw in item.song_info.get("brTypes", [])
                            if _quality_id(raw, item.source) == item.quality), None)
        if raw_quality is None:
            raise SqMusicError("SQ candidate no longer advertises its FLAC quality")
        payload = dict(item.song_info)
        payload.update({"id": item.song_id, "plugName": item.source,
                        "brType": raw_quality, "downloadFormat": "flac"})
        queued = self._request_json("/api/download/downloadSong", method="POST", payload=payload)
        if not isinstance(queued, dict) or not str(queued.get("id", "")).isdigit():
            raise SqMusicError("SQ did not return a task ID")
        task_id = int(queued["id"])
        deadline = self.monotonic() + (self.download_timeout_seconds if timeout_seconds is None else max(.1, timeout_seconds))
        previous = ""
        while True:
            if stop_requested is not None and stop_requested():
                # Do not delete queued SQ jobs or files: cancellation must not
                # race another consumer. Output remains private in staging.
                raise SqMusicStopped("SQ download stopped")
            if self.monotonic() >= deadline:
                raise SqMusicError("SQ download timed out; output remains in private staging")
            result = self._request_json(f"/api/task/bot/{task_id}")
            task = result.get("task") if isinstance(result, dict) else None
            if not isinstance(task, dict):
                raise SqMusicError("SQ task details unavailable; patched backend required")
            state = str(task.get("downloadStatus") or "unknown")
            if state != previous and on_progress is not None:
                on_progress(state)
            previous = state
            if state == "success":
                return self._task_file(task, item, task_id)
            if state in {"error", "supplement_success"}:
                raise SqMusicError("SQ task failed or unexpectedly switched source")
            if state not in {"waiting", "loading", "unknown"}:
                raise SqMusicError("SQ returned an unexpected task state")
            self.sleep(min(self.poll_interval_seconds, max(0, deadline - self.monotonic())))
