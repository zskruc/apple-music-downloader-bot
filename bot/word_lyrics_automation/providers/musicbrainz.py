"""Use an ISRC lookup to independently confirm a work has no linguistic text."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def get_isrc(isrc: str) -> dict[str, Any] | None:
    code = isrc.upper()
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", code):
        return None
    query = urllib.parse.urlencode({"inc": "artists work-rels", "fmt": "json"})
    request = urllib.request.Request("https://musicbrainz.org/ws/2/isrc/" + code + "?" + query, headers={
        "User-Agent": "WordLyrics/1.0 (personal NAS lyric resolver)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(payload, dict) or str(payload.get("isrc") or "").upper() != code:
        raise RuntimeError("MusicBrainz returned a different ISRC")
    return payload
