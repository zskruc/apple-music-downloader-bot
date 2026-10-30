from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ..faults import classify_exception, retry_delay


@dataclass(frozen=True, slots=True)
class AMLLLyric:
    lyric_id: str
    filename: str
    music_names: tuple[str, ...]
    artist_names: tuple[str, ...]
    album_names: tuple[str, ...]
    ncm_ids: tuple[str, ...]
    qq_ids: tuple[str, ...]
    apple_ids: tuple[str, ...]
    spotify_ids: tuple[str, ...]
    isrcs: tuple[str, ...]
    lyrics: str


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item or "").strip())


def _parse_item(value: object) -> AMLLLyric | None:
    if not isinstance(value, dict):
        return None
    return AMLLLyric(
        lyric_id=str(value.get("id") or ""),
        filename=str(value.get("filename") or ""),
        music_names=_strings(value.get("musicNames")),
        artist_names=_strings(value.get("artistNames")),
        album_names=_strings(value.get("albumNames")),
        ncm_ids=_strings(value.get("ncmMusicIds")),
        qq_ids=_strings(value.get("qqMusicIds")),
        apple_ids=_strings(value.get("appleMusicIds")),
        spotify_ids=_strings(value.get("spotifyIds")),
        isrcs=tuple(item.upper() for item in _strings(value.get("isrcs"))),
        lyrics=str(value.get("lyrics") or ""),
    )


class AMLLClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.amll.dev",
        timeout: float = 25,
        attempts: int = 4,
        sleeper: Callable[[float], None] = time.sleep,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = max(5.0, timeout)
        self.attempts = max(1, attempts)
        self.sleeper = sleeper
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def _request_json(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        query = urllib.parse.urlencode(params, doseq=True)
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "IcekyWordLyrics/1.0 (+https://github.com/amll-dev/amll-ttml-api)",
            },
        )
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise RuntimeError("AMLL response exceeded 8 MiB")
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise RuntimeError("AMLL returned a non-object response")
                return payload
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    exc.close()
                    return None
                last_error = exc
                fault = classify_exception(exc, provider="amll")
                exc.close()
                if not fault.retryable:
                    raise
                # Long server cooldowns belong to the durable job scheduler,
                # rather than tying up this provider call or ignoring them.
                if (fault.retry_after_seconds or 0) > 30:
                    raise
                delay = retry_delay(attempt, base=1, maximum=30,
                                    retry_after=fault.retry_after_seconds)
            except (OSError, TimeoutError) as exc:
                fault = classify_exception(exc, provider="amll")
                if not fault.retryable or fault.category != "transport":
                    raise
                last_error = exc
                delay = retry_delay(attempt, base=1, maximum=30)
            if attempt < self.attempts:
                self.sleeper(min(30.0, delay))
        # Preserve HTTP status/Retry-After and the exact typed transport cause
        # for the durable fault classifier. JSON/schema failures are never
        # caught above as network errors and therefore are not retried here.
        assert last_error is not None
        raise last_error

    def get(
        self,
        *,
        lyric_id: str = "",
        apple_id: str = "",
        ncm_id: str = "",
        isrc: str = "",
    ) -> AMLLLyric | None:
        params: dict[str, Any] = {}
        if lyric_id:
            params["id"] = lyric_id
        elif apple_id:
            params["appleMusicId"] = apple_id
        elif ncm_id:
            params["ncmMusicId"] = ncm_id
        elif isrc:
            params["isrc"] = isrc
        else:
            raise ValueError("AMLL get requires an identifier")
        payload = self._request_json("/v1/lyrics/get", params)
        return _parse_item(payload.get("data")) if payload else None

    def search(
        self,
        *,
        title: str,
        artist: str = "",
        album: str = "",
        limit: int = 20,
    ) -> list[AMLLLyric]:
        params: dict[str, Any] = {
            "musicName": title,
            "page": 1,
            "pageSize": max(1, min(100, limit)),
        }
        if artist:
            params["artistName"] = artist
        if album:
            params["albumName"] = album
        payload = self._request_json("/v1/lyrics/search", params)
        data = payload.get("data") if payload else None
        items = data.get("items") if isinstance(data, dict) else None
        output: list[AMLLLyric] = []
        if not isinstance(items, list):
            return output
        for item in items:
            parsed = _parse_item(item)
            if parsed is not None:
                output.append(parsed)
        return output

    def fetch(self, item: AMLLLyric) -> AMLLLyric | None:
        if item.lyrics:
            return item
        return self.get(lyric_id=item.lyric_id)
