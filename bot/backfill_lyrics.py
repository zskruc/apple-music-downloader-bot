from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog
from mutagen.mp4 import MP4

from gamdl.api import AppleMusicApi, WrapperApi
from gamdl.interface import AppleMusicSongInterface, SyncedLyricsFormat


MEDIA_SUFFIXES = {".m4a", ".mp4", ".m4v"}
VALID_FORMATS = {"lrc", "srt", "ttml"}
SKIP_PATH_PARTS = {"@eaDir", "#recycle"}
STOREFRONT_IDS = {
    "CN": "143465-19,32",
    "HK": "143463-45,32",
    "TW": "143470-18,32",
    "US": "143441-1,32",
    "JP": "143462-9,32",
}
REVERSE_STOREFRONT_IDS = {
    str(value): key.lower()
    for key, value in STOREFRONT_IDS.items()
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


BACKFILL_NETWORK_ATTEMPTS = max(1, int(env("AMDL_BACKFILL_NETWORK_ATTEMPTS", "4") or "4"))
BACKFILL_NETWORK_RETRY_DELAY = float(env("AMDL_BACKFILL_NETWORK_RETRY_DELAY", "5") or "5")


def parse_formats(raw: str) -> list[str]:
    formats: list[str] = []
    for item in raw.split(","):
        lyric_format = item.strip().lower()
        if not lyric_format:
            continue
        if lyric_format not in VALID_FORMATS:
            raise ValueError(f"Unsupported lyrics format: {lyric_format}")
        if lyric_format not in formats:
            formats.append(lyric_format)
    if not formats:
        raise ValueError("At least one lyrics format is required")
    return formats


def parse_storefronts(raw: str) -> list[str]:
    storefronts: list[str] = []
    for item in raw.split(","):
        storefront = item.strip().lower()
        if not storefront:
            continue
        if storefront in REVERSE_STOREFRONT_IDS:
            storefront = REVERSE_STOREFRONT_IDS[storefront]
        if not (len(storefront) == 2 and storefront.isalpha()):
            raise ValueError(f"Unsupported storefront: {storefront}")
        if storefront not in storefronts:
            storefronts.append(storefront)
    return storefronts


def first_value(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def decode_value(value: Any) -> str:
    value = first_value(value)
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def tag_value(tags: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in tags:
            value = decode_value(tags[key])
            if value:
                return value
    return ""


def tag_value_by_hint(tags: dict[str, Any], *hints: str) -> str:
    lowered_hints = tuple(hint.lower() for hint in hints)
    for key, value in tags.items():
        lowered_key = key.lower()
        if any(hint in lowered_key for hint in lowered_hints):
            decoded = decode_value(value)
            if decoded:
                return decoded
    return ""


def clean_song_id(raw: str) -> str:
    song_id = "".join(ch for ch in raw if ch.isdigit())
    return song_id if len(song_id) >= 6 else ""


def storefront_from_tag(raw: str, fallback: str) -> str:
    storefront = (raw or "").strip().lower()
    if len(storefront) == 2 and storefront.isalpha():
        return storefront
    if storefront in REVERSE_STOREFRONT_IDS:
        return REVERSE_STOREFRONT_IDS[storefront]
    storefront_id_prefix = storefront.split("-", 1)[0].split(",", 1)[0]
    if storefront_id_prefix:
        for storefront_id, country in REVERSE_STOREFRONT_IDS.items():
            if storefront_id.split("-", 1)[0].split(",", 1)[0] == storefront_id_prefix:
                return country
    return fallback


def read_track_identity(path: Path, fallback_storefront: str) -> tuple[str, str]:
    audio = MP4(path)
    tags = audio.tags or {}

    song_id = clean_song_id(tag_value(tags, "cnID"))
    if not song_id:
        song_id = clean_song_id(
            tag_value_by_hint(tags, "song_id", "song id", "apple music id", "cnid")
        )

    storefront_raw = tag_value(tags, "sfID")
    if not storefront_raw:
        storefront_raw = tag_value_by_hint(tags, "storefront", "country")
    storefront = storefront_from_tag(storefront_raw, fallback_storefront)

    return song_id, storefront


def iter_media(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in MEDIA_SUFFIXES
        and not path.name.startswith("._")
        and not any(part in SKIP_PATH_PARTS for part in path.parts)
    )


def is_apple_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "api error 404" in text
        or "resource not found" in text
        or '"status":"404"' in text
        or "'status':'404'" in text
    )


def is_transient_network_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    return (
        "timeouterror" in name
        or "connecterror" in name
        or "readerror" in name
        or "networkerror" in name
        or "remoteprotocolerror" in name
        or "timeout" in text
        or "connection reset" in text
        or "connection aborted" in text
        or "temporarily unavailable" in text
        or "unexpected_eof" in text
        or "tlsv1_alert" in text
    )


class LyricsFetcher:
    def __init__(
        self,
        *,
        wrapper_account_url: str,
        language: str,
        network_mode: str,
        proxy_url: str,
    ) -> None:
        self.wrapper_account_url = wrapper_account_url
        self.language = language
        self.network_mode = network_mode
        self.proxy_url = proxy_url
        self.api: AppleMusicApi | None = None
        self.wrapper_api: WrapperApi | None = None

    async def __aenter__(self) -> "LyricsFetcher":
        # Imported API helpers otherwise default to DEBUG and include the
        # wrapper account payload. Keep maintenance output useful without
        # ever logging Apple Music session tokens.
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.KeyValueRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        )
        last_exc: Exception | None = None
        for attempt in range(1, BACKFILL_NETWORK_ATTEMPTS + 1):
            try:
                self.wrapper_api = await WrapperApi.create(
                    base_url=self.wrapper_account_url,
                    decrypt_host=env("WRAPPER_DECRYPT_HOST", "wrapper"),
                    decrypt_port=int(env("WRAPPER_DECRYPT_PORT", "10020") or "10020"),
                )
                self.api = await AppleMusicApi.create_from_wrapper(
                    self.wrapper_api,
                    language=self.language,
                )
                break
            except Exception as exc:
                last_exc = exc
                if self.wrapper_api is not None:
                    await self.wrapper_api.client.aclose()
                    self.wrapper_api = None
                if attempt >= BACKFILL_NETWORK_ATTEMPTS or not is_transient_network_error(exc):
                    raise
                print(
                    "Apple Music API init failed "
                    f"attempt={attempt}/{BACKFILL_NETWORK_ATTEMPTS}: {exc}; "
                    f"retrying in {BACKFILL_NETWORK_RETRY_DELAY:g}s"
                )
                await asyncio.sleep(max(0.0, BACKFILL_NETWORK_RETRY_DELAY))
        if self.api is None:
            assert last_exc is not None
            raise last_exc
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self.api is not None:
            await self.api.client.aclose()
        if self.wrapper_api is not None:
            await self.wrapper_api.client.aclose()

    async def fetch(self, song_id: str, storefront: str, lyric_format: str) -> str | None:
        if self.api is None:
            raise RuntimeError("LyricsFetcher is not initialized")
        self.api.storefront = storefront
        song = await self.api.get_song(song_id)
        if not song or not song.get("data"):
            return None
        song_interface = AppleMusicSongInterface(
            SimpleNamespace(apple_music_api=self.api),
            synced_lyrics_format=SyncedLyricsFormat(lyric_format),
        )
        lyrics = await song_interface.get_lyrics(song["data"][0])
        if lyrics and lyrics.synced:
            return lyrics.synced
        return None

    async def fetch_any(
        self,
        song_id: str,
        storefronts: list[str],
        lyric_format: str,
    ) -> tuple[str | None, str]:
        last_storefront = storefronts[0] if storefronts else ""
        for storefront in storefronts:
            last_storefront = storefront
            for attempt in range(1, BACKFILL_NETWORK_ATTEMPTS + 1):
                try:
                    lyrics = await self.fetch(song_id, storefront, lyric_format)
                    break
                except Exception as exc:
                    if is_apple_not_found_error(exc):
                        lyrics = None
                        break
                    if attempt < BACKFILL_NETWORK_ATTEMPTS and is_transient_network_error(exc):
                        print(
                            f"{lyric_format} fetch failed song={song_id} storefront={storefront} "
                            f"attempt={attempt}/{BACKFILL_NETWORK_ATTEMPTS}: {exc}; "
                            f"retrying in {BACKFILL_NETWORK_RETRY_DELAY:g}s"
                        )
                        await asyncio.sleep(max(0.0, BACKFILL_NETWORK_RETRY_DELAY))
                        continue
                    if storefront == storefronts[-1]:
                        raise
                    lyrics = None
                    break
            if lyrics:
                return lyrics, storefront
        return None, last_storefront


async def backfill(args: argparse.Namespace) -> int:
    formats = parse_formats(args.formats)
    storefront_fallbacks = parse_storefronts(args.storefront_fallbacks)
    root = Path(args.root)
    if not root.exists():
        print(f"Music root does not exist: {root}", file=sys.stderr)
        return 2

    media_files = iter_media(root)
    if args.limit:
        media_files = media_files[: args.limit]

    print(f"Scanning {len(media_files)} media file(s) under {root}")
    written = 0
    skipped_existing = 0
    skipped_no_id = 0
    skipped_no_lyrics = 0
    failed = 0

    async with LyricsFetcher(
        wrapper_account_url=args.wrapper_account_url,
        language=args.language,
        network_mode=args.network_mode,
        proxy_url=args.proxy_url,
    ) as fetcher:
        fallback_storefront = args.storefront or (fetcher.api.storefront if fetcher.api else "us")
        fallback_storefront = fallback_storefront.lower()

        for index, media_path in enumerate(media_files, start=1):
            pending_formats = [
                lyric_format
                for lyric_format in formats
                if args.overwrite
                or not media_path.with_suffix(f".{lyric_format}").exists()
            ]
            if not pending_formats:
                skipped_existing += 1
                continue

            try:
                song_id, storefront = read_track_identity(media_path, fallback_storefront)
            except Exception as exc:
                failed += 1
                print(f"[{index}/{len(media_files)}] tag read failed: {media_path}: {exc}")
                continue

            if not song_id:
                skipped_no_id += 1
                print(f"[{index}/{len(media_files)}] skipped no Apple Music cnID: {media_path}")
                continue

            storefronts_to_try = [
                item
                for item in [storefront, *storefront_fallbacks, fallback_storefront]
                if item
            ]
            storefronts_to_try = list(dict.fromkeys(storefronts_to_try))

            for lyric_format in pending_formats:
                sidecar_path = media_path.with_suffix(f".{lyric_format}")
                try:
                    lyrics, used_storefront = await fetcher.fetch_any(
                        song_id,
                        storefronts_to_try,
                        lyric_format,
                    )
                except Exception as exc:
                    failed += 1
                    print(
                        f"[{index}/{len(media_files)}] {lyric_format} failed "
                        f"song={song_id} storefronts={','.join(storefronts_to_try)}: "
                        f"{media_path}: {exc}"
                    )
                    continue

                if not lyrics:
                    skipped_no_lyrics += 1
                    print(
                        f"[{index}/{len(media_files)}] no {lyric_format} lyrics "
                        f"song={song_id} storefronts={','.join(storefronts_to_try)}: "
                        f"{media_path}"
                    )
                    continue

                if args.dry_run:
                    print(f"[{index}/{len(media_files)}] would write {sidecar_path}")
                else:
                    sidecar_path.write_text(lyrics, encoding="utf-8")
                    print(
                        f"[{index}/{len(media_files)}] wrote {sidecar_path} "
                        f"storefront={used_storefront}"
                    )
                written += 1

                if args.delay > 0:
                    await asyncio.sleep(args.delay)

            if args.progress_every and index % args.progress_every == 0:
                print(
                    "progress: "
                    f"seen={index} written={written} existing={skipped_existing} "
                    f"no_id={skipped_no_id} no_lyrics={skipped_no_lyrics} failed={failed}"
                )

    print(
        "done: "
        f"written={written} existing={skipped_existing} no_id={skipped_no_id} "
        f"no_lyrics={skipped_no_lyrics} failed={failed}"
    )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill Apple Music LRC/TTML sidecar lyrics next to existing files.",
    )
    parser.add_argument("--root", default=env("AMDL_BACKFILL_ROOT", "/music"))
    parser.add_argument("--formats", default=env("AMDL_BACKFILL_LYRIC_FORMATS", "ttml"))
    parser.add_argument("--storefront", default=env("AMDL_BACKFILL_STOREFRONT", ""))
    parser.add_argument(
        "--storefront-fallbacks",
        default=env("AMDL_BACKFILL_STOREFRONT_FALLBACKS", "cn,hk,tw,us,jp"),
    )
    parser.add_argument("--language", default=env("AMDL_LANGUAGE", "zh-CN"))
    parser.add_argument(
        "--wrapper-account-url",
        default=env("WRAPPER_URL", env("WRAPPER_ACCOUNT_URL", "http://wrapper")),
    )
    parser.add_argument("--network-mode", default=env("AMDL_NETWORK_MODE", "auto"))
    parser.add_argument("--proxy-url", default=env("AMDL_DOWNLOAD_PROXY_URL", ""))
    parser.add_argument("--overwrite", action="store_true", default=env_flag("AMDL_BACKFILL_OVERWRITE"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=float(env("AMDL_BACKFILL_DELAY", "0.2") or "0.2"))
    parser.add_argument(
        "--progress-every",
        type=int,
        default=int(env("AMDL_BACKFILL_PROGRESS_EVERY", "100") or "100"),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    started_at = time.monotonic()
    try:
        return asyncio.run(backfill(args))
    finally:
        elapsed = time.monotonic() - started_at
        print(f"elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    raise SystemExit(main())
