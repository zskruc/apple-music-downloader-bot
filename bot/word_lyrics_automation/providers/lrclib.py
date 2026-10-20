"""Read exact LRCLIB signatures; an absent record is not a transport failure."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def get_record(*, title: str, artist: str, album: str, duration: float) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({
        "track_name": title, "artist_name": artist, "album_name": album,
        "duration": round(duration, 3),
    })
    request = urllib.request.Request("https://lrclib.net/api/get?" + query, headers={
        "User-Agent": "WordLyrics/1.0 (personal NAS lyric resolver)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        # The existing worker backs off; it never loops immediately on 429.
        raise
    if not isinstance(payload, dict) or not payload.get("id"):
        raise RuntimeError("LRCLIB returned an invalid record")
    return payload
