from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..identity import artist_matches
from ..io import atomic_write_json


class AppleArtistIdentity:
    """Resolve regional names through the same catalog song and artist IDs."""

    def __init__(self, cache_dir: Path, client_factory: Any) -> None:
        self.cache_dir = cache_dir
        self.client_factory = client_factory

    def resolve(self, track: Any) -> dict[str, Any]:
        song_id = str(track.song_id or "")
        if not song_id.isdecimal() or not track.artist or track.duration <= 0:
            return {"names": [], "evidence": []}
        key = hashlib.sha256(
            f"{song_id}|{track.isrc}|{track.artist}|{track.duration:.2f}".encode()
        ).hexdigest()
        cache = self.cache_dir / f"{key}.json"
        try:
            cached = json.loads(cache.read_text())
            if float(cached.get("expires_at", 0)) > time.time():
                return cached
        except (OSError, ValueError, TypeError):
            pass
        client = self.client_factory()
        base = "https://amp-api.music.apple.com/v1/catalog"
        artist_ids: list[str] = []
        evidence: list[dict[str, str]] = []
        for storefront in ("us", "jp", "cn"):
            url = f"{base}/{storefront}/songs/{song_id}"
            status, payload = client.request_json(url)
            if status not in (200, 404):
                raise RuntimeError(f"Apple identity catalog HTTP {status}")
            for song in payload.get("data") or []:
                attributes = song.get("attributes") or {}
                if (
                    str(song.get("id")) != song_id
                    or not artist_matches(track.artist, str(attributes.get("artistName") or ""))
                    or abs(float(attributes.get("durationInMillis") or 0) / 1000 - track.duration) > 0.75
                    or (track.isrc and str(attributes.get("isrc") or "").upper() != track.isrc.upper())
                ):
                    continue
                artists = (song.get("relationships") or {}).get("artists") or {}
                artist_ids = [
                    str(item["id"]) for item in artists.get("data") or []
                    if str(item.get("id") or "").isdecimal() and item.get("type") == "artists"
                ][:3]
                if artist_ids:
                    evidence.append({"song_id": song_id, "url": url})
                    break
            if artist_ids:
                break
        names: list[str] = []
        for artist_id in artist_ids:
            for storefront, language in (("jp", "ja-JP"), ("cn", "zh-Hans-CN"), ("us", "en-US")):
                url = f"{base}/{storefront}/artists/{artist_id}?l={language}"
                status, payload = client.request_json(url)
                if status not in (200, 404):
                    raise RuntimeError(f"Apple artist catalog HTTP {status}")
                for item in payload.get("data") or []:
                    name = str((item.get("attributes") or {}).get("name") or "").strip()
                    if str(item.get("id")) == artist_id and item.get("type") == "artists" and 0 < len(name) <= 200:
                        if name not in names:
                            names.append(name)
                            evidence.append({"artist_id": artist_id, "name": name, "url": url})
        result = {
            "names": names,
            "evidence": evidence,
            "expires_at": time.time() + (30 * 86400 if names else 3600),
        }
        atomic_write_json(cache, result)
        return result
