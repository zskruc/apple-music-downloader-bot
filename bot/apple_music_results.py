from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


VERIFIED_EXISTING_PREFIX = "[apple-music-ingest] verified existing media: "
NO_MEDIA_OUTPUT_EXIT_CODE = 71
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class VerifiedExistingMedia:
    path: Path
    song_id: str
    matched_by: str


def format_verified_existing_media(
    *,
    path: Path,
    song_id: str,
    matched_by: str,
) -> str:
    payload = {
        "path": str(path),
        "song_id": str(song_id or ""),
        "matched_by": str(matched_by or ""),
    }
    return VERIFIED_EXISTING_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_verified_existing_media(line: str) -> VerifiedExistingMedia | None:
    marker_index = line.find(VERIFIED_EXISTING_PREFIX)
    if marker_index < 0:
        return None
    payload_text = line[marker_index + len(VERIFIED_EXISTING_PREFIX) :]
    payload_text = ANSI_ESCAPE_RE.sub("", payload_text).strip()
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    path = Path(str(payload.get("path") or ""))
    if not path.is_absolute():
        return None
    return VerifiedExistingMedia(
        path=path,
        song_id=str(payload.get("song_id") or ""),
        matched_by=str(payload.get("matched_by") or ""),
    )


def effective_download_return_code(
    return_code: int,
    *,
    changed_media_count: int,
    verified_existing_count: int,
) -> int:
    if return_code != 0:
        return return_code
    if changed_media_count > 0 or verified_existing_count > 0:
        return 0
    return NO_MEDIA_OUTPUT_EXIT_CODE
