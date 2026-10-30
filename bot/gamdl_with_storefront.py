from __future__ import annotations

import os
import re
import sys
import urllib.parse
from pathlib import Path

from apple_music_library import (
    DEDUP_ENABLED,
    find_existing_track,
    index_paths,
    isrc_from_xid,
    refresh_index,
    track_fingerprint,
)
from apple_music_results import format_verified_existing_media


STORE_FRONT_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)
APPLE_MUSIC_HOSTS = {
    "music.apple.com",
    "beta.music.apple.com",
    "geo.music.apple.com",
}


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def storefront_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in APPLE_MUSIC_HOSTS:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    candidate = parts[0].lower()
    if STORE_FRONT_RE.fullmatch(candidate):
        return candidate
    return ""


def storefront_from_args(args: list[str]) -> str:
    for arg in args:
        storefront = storefront_from_url(arg)
        if storefront:
            return storefront
    return ""


def patch_gamdl_storefront(storefront: str) -> None:
    from gamdl.api import AppleMusicApi

    original = AppleMusicApi.create_from_wrapper.__func__

    async def create_from_wrapper_with_storefront(cls, *args, **kwargs):
        api = await original(cls, *args, **kwargs)
        api.storefront = storefront
        print(f"[apple-music-ingest] using Apple Music storefront: {storefront}", flush=True)
        return api

    AppleMusicApi.create_from_wrapper = classmethod(create_from_wrapper_with_storefront)


def patch_gamdl_library_dedup(mode: str) -> None:
    from gamdl.downloader import (
        AppleMusicDownloader,
        GamdlDownloaderMediaFileExistsError,
    )

    refresh_stats = refresh_index()
    print(
        "[apple-music-ingest] library index ready: "
        f"{refresh_stats['tracks']} tagged track(s), "
        f"{refresh_stats['parsed']} changed file(s), "
        f"{refresh_stats.get('isrc_conflicts', 0)} conflicting ISRC(s)",
        flush=True,
    )
    original = AppleMusicDownloader.download

    async def download_with_library_dedup(self, item):
        metadata = item.media.media_metadata or {}
        song_id = str(metadata.get("id") or "")
        media_type = str(metadata.get("type") or "")
        if not self.synced_lyrics_only and media_type in {"songs", "library-songs"}:
            tags = item.media.tags
            isrc = isrc_from_xid(getattr(tags, "xid", "") if tags else "")
            fingerprint = track_fingerprint(
                getattr(tags, "title", "") if tags else "",
                getattr(tags, "artist", "") if tags else "",
                getattr(tags, "album", "") if tags else "",
            )
            existing = find_existing_track(
                song_id,
                mode,
                isrc=isrc,
                fingerprint=fingerprint,
            )
            if existing is not None:
                print(
                    format_verified_existing_media(
                        path=existing.path,
                        song_id=existing.song_id,
                        matched_by=existing.matched_by,
                    ),
                    flush=True,
                )
                print(
                    "[apple-music-ingest] library duplicate skipped: "
                    f"song_id={song_id} mode={mode} "
                    f"matched_by={existing.matched_by} path={existing.display_path}",
                    flush=True,
                )
                raise GamdlDownloaderMediaFileExistsError(str(existing.path))
        result = await original(self, item)
        final_path = Path(str(item.final_path or ""))
        if final_path.is_file():
            index_paths([final_path])
        return result

    AppleMusicDownloader.download = download_with_library_dedup


def main() -> int:
    # Import the click command before monkey patching. gamdl inspects the original
    # create_from_wrapper signature while importing its CLI config.
    from gamdl.cli.cli import main as gamdl_main
    from apple_account_network import install_account_network_patch

    install_account_network_patch()

    storefront = os.environ.get("GAMDL_STOREFRONT_OVERRIDE", "").strip().lower()
    if not storefront and env_flag("GAMDL_USE_URL_STOREFRONT", True):
        storefront = storefront_from_args(sys.argv[1:])
    if storefront:
        patch_gamdl_storefront(storefront)
    dedup_mode = os.environ.get("GAMDL_LIBRARY_DEDUP_MODE", "").strip().lower()
    if (
        DEDUP_ENABLED
        and env_flag("GAMDL_LIBRARY_DEDUP_ENABLED", True)
        and dedup_mode in {"alac", "atmos"}
        and "--synced-lyrics-only" not in sys.argv
    ):
        patch_gamdl_library_dedup(dedup_mode)

    return gamdl_main() or 0


if __name__ == "__main__":
    sys.exit(main())
