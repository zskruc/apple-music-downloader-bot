"""Read actual Navidrome lyric responses before acknowledging publication.

No scans or writes, no proxy environment, no credentials in diagnostics.
Selection uses recording metadata AND byte size/format to avoid picking an
older MP3 with the same title and album as a new FLAC.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .fingerprint import media_fingerprint
from .io import atomic_write_json
from .text import normalize_text
from .ttml import parse_word_ttml


class PlayerVerificationError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.fault = {"category": "player", "code": code,
                      "retryable": retryable,
                      "action": "retry" if retryable else "needs_attention"}


def request_from_environment() -> Callable[..., dict[str, Any]]:
    base = os.environ.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
    user = os.environ.get("NAVIDROME_USER", "")
    password = os.environ.get("NAVIDROME_PASSWORD", "")
    if not user or not password:
        raise PlayerVerificationError("player_credentials_missing", retryable=False)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(method: str, **arguments: Any) -> dict[str, Any]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((password + salt).encode(), usedforsecurity=False).hexdigest()
        params = {"u": user, "t": token, "s": salt, "v": "1.16.1",
                  "c": "word-lyrics-verifier", "f": "json", **arguments}
        url = base + "/rest/" + method + ".view?" + urllib.parse.urlencode(params)
        try:
            with opener.open(url, timeout=10) as response:
                payload = json.load(response)["subsonic-response"]
        except Exception as exc:
            # Retain classification, never the request URL with account token.
            from .faults import classify_exception
            fault = classify_exception(exc, provider="navidrome", stage="scan")
            error = PlayerVerificationError("player_request_failed", retryable=fault.retryable)
            error.fault = fault.as_dict()
            raise error from None
        if payload.get("status") != "ok":
            code = (payload.get("error") or {}).get("code")
            raise PlayerVerificationError("player_auth_rejected" if code in (40, 41, 50)
                                          else "player_api_rejected", retryable=code not in (40, 41, 50))
        return payload

    return request


def compare_word_lyrics(ttml: str, lyrics: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter
    from .player_xml import source_groups
    try:
        expected_display, expected_rows = source_groups(ttml)
    except Exception:
        raise PlayerVerificationError("sidecar_xml_not_supported", retryable=False) from None
    main = [item for item in lyrics if item.get("kind", "main") == "main"]
    display = [line for item in main for line in item.get("line", [])]
    actual = [line for item in main for line in item.get("cueLine", [])]
    if [{"start": row.get("start"), "value": row.get("value")} for row in display] != expected_display:
        raise PlayerVerificationError("player_lyric_text_or_clock_not_current")
    def key(row):
        return row.get("index"), row.get("agentId", "")
    if Counter(map(key, actual)) != Counter(map(key, expected_rows)):
        raise PlayerVerificationError("player_voice_groups_not_current")
    wanted = {key(row): row for row in expected_rows}
    count = 0
    for row in actual:
        expected = wanted[key(row)]
        if any(row.get(field, "" if field == "agentId" else None) != expected[field]
               for field in ("index", "start", "end", "value", "agentId")):
            raise PlayerVerificationError("player_lyric_text_or_clock_not_current")
        cues = [{field: cue.get(field) for field in ("value", "start", "end", "byteStart", "byteEnd")}
                for cue in row.get("cue", [])]
        if cues != expected["cue"]:
            raise PlayerVerificationError("player_word_text_clock_or_byte_range_not_current")
        count += len(cues)
    return {"lines": len(expected_display), "voice_groups": len(expected_rows), "cues": count}


def verify_publication(media_path: Path, metadata: dict[str, Any], result: dict[str, Any],
                       expected_fingerprint: dict[str, Any], *,
                       request: Callable[..., dict[str, Any]] | None = None,
                       evidence_dir: Path | None = None) -> dict[str, Any]:
    request = request or request_from_environment()
    if media_fingerprint(media_path) != expected_fingerprint:
        raise PlayerVerificationError("audio_changed_before_player_verification", retryable=False)
    sidecar = Path(result["sidecar_path"])
    text = sidecar.read_text(encoding="utf-8-sig")
    if hashlib.sha256(sidecar.read_bytes()).hexdigest() != result["candidate_sha256"]:
        raise PlayerVerificationError("sidecar_changed_before_player_verification", retryable=False)
    songs = request("search3", query=str(metadata.get("title") or media_path.stem),
                    songCount=500, artistCount=0, albumCount=0).get("searchResult3", {}).get("song", [])
    matched = [song for song in songs if
               all(normalize_text(str(song.get(key, ""))) == normalize_text(str(metadata.get(key, "")))
                   for key in ("title", "artist", "album"))
               and song.get("size") == expected_fingerprint["size"]
               and str(song.get("suffix", "")).casefold() == media_path.suffix.lstrip(".").casefold()]
    song_id = str(metadata.get("navidrome_id") or "")
    if song_id:
        matched = [song for song in matched if str(song.get("id")) == song_id]
    if len(matched) != 1:
        raise PlayerVerificationError("player_recording_not_indexed" if not matched
                                      else "player_recording_ambiguous", retryable=not matched)
    song_id = str(matched[0]["id"])
    lyrics = request("getLyricsBySongId", id=song_id, enhanced="true").get("lyricsList", {}).get("structuredLyrics", [])
    if sidecar.suffix.casefold() == ".ttml":
        metrics = compare_word_lyrics(text, lyrics)
    else:
        # The only non-word publication is an explicit reviewed no-lyrics marker.
        from .instrumental import INSTRUMENTAL_DISPLAY
        values = [line.get("value") for item in lyrics for line in item.get("line", [])]
        if result.get("lyric_status") != "not_required" or values != [INSTRUMENTAL_DISPLAY]:
            raise PlayerVerificationError("player_instrumental_marker_not_current")
        if any(item.get("cueLine") for item in lyrics):
            raise PlayerVerificationError("player_instrumental_has_word_cues")
        metrics = {"lines": 1, "cues": 0}
    if media_fingerprint(media_path) != expected_fingerprint:
        raise PlayerVerificationError("audio_changed_during_player_verification", retryable=False)
    if hashlib.sha256(sidecar.read_bytes()).hexdigest() != result["candidate_sha256"]:
        raise PlayerVerificationError("sidecar_changed_during_player_verification", retryable=False)
    report = {"passed": True, "song_id": song_id, "candidate_sha256": result["candidate_sha256"],
              "source_fingerprint": expected_fingerprint, **metrics}
    if evidence_dir is not None:
        destination = evidence_dir / (str(expected_fingerprint["sha256"]) + ".json")
        atomic_write_json(destination, report)
        report["evidence_path"] = str(destination)
    return report
